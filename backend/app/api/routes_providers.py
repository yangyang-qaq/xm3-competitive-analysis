"""Provider 能力矩阵与连通性探测。

两个端点分工明确，**不要合并**：

    GET /api/providers         纯读配置，不产生任何外部调用，永远秒回。
                               回答"我有哪几家可选、各自支持什么、配没配密钥"。
    GET /api/providers/health  真的打一次 API，会花钱、会慢。
                               回答"现在这套配置真的能跑吗"。

分开的理由是缓存语义：能力矩阵可以随便调（页面每次加载都拉一次也无所谓），
而探测不能。合成一个端点的话，要么能力矩阵被探测拖慢，要么探测结果被缓存
而失去意义。

**为什么只看状态码不够**：`/health` 只能说明进程活着；密钥是否有效、配额是否
用尽、模型名对不对，全都要真的打一次才知道。参考实现的 provider 故障常常
表现为"搜索没结果"——那是 HTTP 200 加一个空数组。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query

from app.core.config import Settings, get_settings
from app.providers.base import ProbeResult
from app.providers.registry import describe_all, load_builtin_providers, try_build

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/providers", tags=["providers"])

#: 单次探测的墙钟上限。比 `LLM_TIMEOUT`（180s）短得多：探测是给人看的，
#: 一个卡住三分钟的页面没人会等，而它想证明的事（"通不通"）几秒内就有答案。
PROBE_TIMEOUT_SECONDS = 25.0

#: 探测覆盖的三种。与 `describe_all()` 列出的 kind 一致，两份输出能对上。
PROBE_KINDS = ("llm", "search", "fetch")


def _registry(kind: str) -> dict:
    from app.providers.registry import _REGISTRIES

    return _REGISTRIES[kind]


# ============================================================
# 能力矩阵
# ============================================================


@router.get("")
def list_providers() -> dict:
    """能力矩阵 + 配置状态 + 今日成本。**不发起任何外部调用。**"""
    settings = get_settings()
    load_builtin_providers()

    return {
        "active": _active_names(settings),
        "fallback": settings.llm_fallback or None,
        "cassetteMode": settings.cassette_mode,
        "providers": describe_all(),
        "cost": _today_cost(),
    }


def _active_names(settings: Settings) -> dict[str, str]:
    return {
        "llm": settings.llm_provider,
        "search": settings.search_provider,
        "fetch": settings.fetch_provider,
    }


def _today_cost() -> dict:
    """今日成本。DB 还没建表时返回零值而不是报错——

    `/api/providers` 是启动后最先被打开的页面之一，它不该因为"还没跑过任何任务"
    而返回 500。
    """
    from datetime import UTC, datetime

    from app.db.repo import traces as traces_repo

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    try:
        return traces_repo.cost_summary(since=today)
    except Exception as exc:  # noqa: BLE001 - 尚未迁移 / 空库
        log.warning("读取今日成本失败，按 0 返回：%s", exc)
        return {
            "since": today,
            "calls": 0,
            "totalTokens": 0,
            "totalCostUsd": 0.0,
            "byModel": [],
            "byKind": [],
        }


# ============================================================
# 连通性探测
# ============================================================


@router.get("/health")
async def providers_health(
    scope: str = Query(
        "active",
        pattern="^(active|all)$",
        description="active 只探当前生效的三个；all 探所有已注册的 provider",
    ),
) -> dict:
    """真实连通性探测。

    `scope=active` 是默认值：它是绝大多数时候想知道的，而且便宜——三次调用。
    它探的是 `registry.get_*()`，也就是**真正在服役的那个对象**：配了
    `LLM_FALLBACK` 的话拿到的是 `FallbackLLM`，它的探测会分别报告主备两家。
    绕过包装去探底层，得到的答案不能代表系统当前的状态。

    `scope=all` 用来在换 provider 之前体检：把 `LLM_PROVIDER` 从 A 改成 B 之前，
    先确认 B 是通的。没配密钥的那些**不探**——去探一个没有密钥的服务毫无意义，
    直接标 `configured=false` 返回，而且不算失败。

    并发探测：各探测彼此独立、各自等一次网络往返，串行做最坏是三者之和。
    """
    settings = get_settings()
    load_builtin_providers()

    targets = _targets(settings, scope)
    entries = list(
        await asyncio.gather(*(_probe_one(kind, name, provider) for kind, name, provider in targets))
    )

    # 整体成不成立，只看**真的探过的**那些。没配密钥的不算失败——
    # 一个没启用的可选 provider 把整页刷成红的，这页很快就没人看了。
    probed = [e for e in entries if e["probed"]]
    return {
        "scope": scope,
        "ok": bool(probed) and all(e["ok"] for e in probed),
        "cassetteMode": settings.cassette_mode,
        "probes": entries,
    }


def _targets(settings: Settings, scope: str) -> list[tuple[str, str, object]]:
    """`[(kind, 名字, provider 实例)]`。

    `scope=active` 走 `registry.get_*()`：那条路还包着录制与降级
    （配了 `LLM_FALLBACK` 拿到的是 `FallbackLLM`）。探测要回答的是
    "**系统现在**能不能用"，那就该探真正在服役的那个对象，
    而不是绕过包装去探底层那一个——后者会在主 provider 已经熔断、
    系统正跑在备用上时报绿。

    `scope=all` 用 `try_build` 逐个构造：能建出来的就探，建不出来的
    换成一个恒返回该错误的替身，**让它留在结果里**。
    一家 provider 构造失败不该让另外几家的结果一起丢失。
    """
    if scope == "all":
        pairs = [(kind, name) for kind in PROBE_KINDS for name in sorted(_registry(kind))]
    else:
        active = _active_names(settings)
        pairs = [(kind, active[kind]) for kind in PROBE_KINDS if active.get(kind)]

    out: list[tuple[str, str, object]] = []
    for kind, name in pairs:
        if scope == "active":
            from app.providers.registry import get_fetcher, get_llm, get_search

            out.append((kind, name, {"llm": get_llm, "search": get_search, "fetch": get_fetcher}[kind]()))
            continue

        outcome = try_build(kind, name, settings)
        if outcome.instance is None:
            log.info("跳过探测 %s:%s——%s", kind, name, outcome.error)
            out.append((kind, name, _Unavailable(outcome.error)))
        else:
            out.append((kind, name, outcome.instance))
    return out


class _Unavailable:
    """建不出来的 provider 的替身：探测它永远返回那条构造错误。

    它**没有发出任何真实请求**，这一点用 `probed=False` 明确说出来，
    否则"没探"会被记成"探了但失败"。
    """

    probed = False

    def __init__(self, error: str) -> None:
        self._error = error

    def probe(self) -> ProbeResult:
        return ProbeResult(ok=False, detail=self._error, error="ProviderNotConfigured")


async def _probe_one(kind: str, name: str, provider) -> dict:
    """探一个，并给结果套上它是谁。

    超时单独处理：`asyncio.wait_for` 抛的 `TimeoutError` 不是 provider 的错误，
    此时它根本没返回任何东西，所以走不了 `run_probe` 那条整理路径。

    `probed` 与 `ok` 是两件事：前者说"这次真的发出请求了吗"，后者说"通不通"。
    只用一个字段的话，"没配密钥"和"配了但连不上"会混成同一个值，
    而这两件事要做的处理完全不同。
    """
    probed = getattr(provider, "probed", True)
    try:
        result: ProbeResult = await asyncio.wait_for(
            asyncio.to_thread(provider.probe), timeout=PROBE_TIMEOUT_SECONDS
        )
    except TimeoutError:
        result = ProbeResult(
            ok=False,
            detail=f"超过 {PROBE_TIMEOUT_SECONDS:.0f} 秒没有响应",
            latency_ms=int(PROBE_TIMEOUT_SECONDS * 1000),
            error="Timeout",
        )
    except Exception as exc:  # noqa: BLE001 - 探测本身不该把端点打挂
        log.exception("探测 %s:%s 时抛出异常", kind, name)
        result = ProbeResult(ok=False, detail=str(exc), error=type(exc).__name__)

    return {
        "kind": kind,
        "name": name,
        "probed": probed,
        **result.as_dict(),
    }
