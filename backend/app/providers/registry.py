"""Provider 注册表。

接入一家新 provider 的完整步骤（业务代码一行都不用改）：

1. 写一个类，实现 `base.py` 里的 Protocol——**不需要继承任何东西**，结构匹配即可
2. 用 `@register_llm("名字")` / `@register_search("名字")` / `@register_fetcher("名字")` 标注
3. 把模块路径加进下面的 `_BUILTIN_MODULES`
4. 在 `.env` 里填 `<名字大写>_API_KEY`

选哪家由 `.env` 的 `LLM_PROVIDER` / `SEARCH_PROVIDER` 决定，这里只负责按名字构造。
"""
from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from app.core.config import Settings, get_settings
from app.providers.base import TIERS, Fetcher, LLMProvider, SearchProvider
from app.providers.errors import ProviderNotConfigured

log = logging.getLogger(__name__)

P = TypeVar("P")

_LLM: dict[str, Callable[[Settings], LLMProvider]] = {}
_SEARCH: dict[str, Callable[[Settings], SearchProvider]] = {}
_FETCH: dict[str, Callable[[Settings], Fetcher]] = {}

#: 内置 provider 模块。**没有 try/except**：模块导入失败必须立刻炸掉，
#: 而不是让这家 provider 悄悄从可选列表里消失。
_BUILTIN_MODULES: tuple[str, ...] = (
    "app.providers.mock",
    "app.providers.llm.deepseek",
    "app.providers.llm.zhipu",
    "app.providers.search.bocha",
    "app.providers.search.tavily",
    "app.providers.fetch.http",
)


def register_llm(name: str) -> Callable[[type], type]:
    def decorate(cls: type) -> type:
        _LLM[name] = cls  # type: ignore[assignment]
        return cls

    return decorate


def register_search(name: str) -> Callable[[type], type]:
    def decorate(cls: type) -> type:
        _SEARCH[name] = cls  # type: ignore[assignment]
        return cls

    return decorate


def register_fetcher(name: str) -> Callable[[type], type]:
    def decorate(cls: type) -> type:
        _FETCH[name] = cls  # type: ignore[assignment]
        return cls

    return decorate


_loaded = False


def load_builtin_providers() -> None:
    """导入内置 provider 模块，触发注册。幂等。"""
    global _loaded
    if _loaded:
        return
    _loaded = True
    for module in _BUILTIN_MODULES:
        importlib.import_module(module)


def available_llm() -> list[str]:
    load_builtin_providers()
    return sorted(_LLM)


def available_search() -> list[str]:
    load_builtin_providers()
    return sorted(_SEARCH)


#: 机器可读的 kind → 给人看的标签。
#: 两者必须分开：`kind` 会参与 cassette 文件名（`search.bocha.jsonl`），
#: 用它做展示会让文件名变成 `搜索.bocha.jsonl`，跨平台、跨编码都难受。
_KIND_LABELS = {"llm": "LLM", "search": "搜索", "fetch": "抓取"}

_REGISTRIES = {"llm": _LLM, "search": _SEARCH, "fetch": _FETCH}


@dataclass(frozen=True)
class BuildOutcome:
    """一次构造尝试的结果。"""

    instance: object | None = None
    #: 失败原因。成功时是空串。
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.instance is not None


def try_build(kind: str, name: str, settings: Settings) -> BuildOutcome:
    """"这家 provider 能不能用"的**唯一**判据：**构造得出来吗**。

    之前这里还有第二套判据——`credentials(name).configured`，也就是"有没有密钥"。
    两套判据必然会在某个 provider 上打架，实测打架的就是 mock：
    它没有也不需要密钥，于是被那套判据判成"未配置"，在 `/api/providers`
    上挂着一个红标签、在 `/api/providers/health` 上被直接跳过——
    而它其实跑得好好的。

    所以判据收敛到构造这一处：密钥缺失时构造函数自己会抛
    `ProviderNotConfigured`，结果一样是失败，而且**不需要在这里重复一遍
    "哪些 provider 需要密钥"这个知识**。

    不含密钥的 provider（mock）因此天然可用，正如它应该的那样。
    """
    load_builtin_providers()
    registry = _REGISTRIES.get(kind)
    if registry is None:
        return BuildOutcome(error=f"未知的 provider 类别：{kind}")
    cls = registry.get(name)
    if cls is None:
        return BuildOutcome(error=f"未注册的 provider：{name}")
    try:
        return BuildOutcome(instance=cls(settings))
    except Exception as exc:  # noqa: BLE001 - 失败要变成数据，不能中断整张列表
        return BuildOutcome(error=f"{type(exc).__name__}：{exc}")


def _build(registry: dict, name: str, kind: str, settings: Settings):
    """按名字构造 provider。`kind` 取 llm / search / fetch。"""
    load_builtin_providers()
    if name not in registry:
        label = _KIND_LABELS.get(kind, kind)
        raise KeyError(
            f"未注册的 {label} provider：{name!r}。可用：{', '.join(sorted(registry)) or '（空）'}"
        )
    cls = registry[name]

    if settings.cassette_mode == "replay":
        # 回放**不构造真实适配器**：CI 里通常没有 API Key，构造会抛未配置，
        # 于是"离线回放"反倒变成必须先有密钥——自相矛盾。
        # cassette 收的是类而不是实例，正是为了这一点。
        from app.providers import cassette

        return cassette.replay(cls, kind=kind, settings=settings)

    return cls(settings)


# ------------------------------------------------------------------
# 构造
#
# 包装顺序（从内到外）：真实 provider → 录制 → 降级
#   - 录制贴着真实 provider，这样"是否真的发出了网络请求"完全由它决定
#   - 降级在录制之外：录下的是主 provider 的成功响应，回放时不会经过降级路径，
#     所以回放结果是确定的，不依赖"当时主 provider 有没有抽风"
# ------------------------------------------------------------------


def _maybe_record(provider: P, kind: str, settings: Settings) -> P:
    if settings.cassette_mode != "record":
        return provider
    # 延迟导入：cassette 只在需要时才加载，避免正常路径承担它的开销
    from app.providers import cassette

    return cassette.record(provider, kind=kind, settings=settings)  # type: ignore[return-value]


def _build_llm(settings: Settings) -> LLMProvider:
    # 回放模式下不挂降级：两条路都只会抛 CassetteMiss，
    # 让第二个 CassetteMiss 盖掉第一个只会让人更难看出缺的是哪条录制。
    if settings.cassette_mode == "replay":
        return _build(_LLM, settings.llm_provider, "llm", settings)

    secondary = None
    if settings.llm_fallback and settings.llm_fallback != settings.llm_provider:
        try:
            secondary = _build(_LLM, settings.llm_fallback, "llm", settings)
        except ProviderNotConfigured as exc:
            log.warning("备用 LLM %s 未配置，降级能力不可用：%s", settings.llm_fallback, exc)

    try:
        primary = _build(_LLM, settings.llm_provider, "llm", settings)
    except ProviderNotConfigured:
        # 主 provider 没配密钥但备用配了：直接用备用，而不是让整个服务起不来。
        # 这是"熔断"最该覆盖的场景——配了备用却因为主密钥缺失而全线不可用，很荒唐。
        if secondary is None:
            raise
        log.warning(
            "主 LLM %s 未配置，本次直接使用备用 %s", settings.llm_provider, settings.llm_fallback
        )
        return secondary

    primary = _maybe_record(primary, "llm", settings)
    if secondary is None:
        return primary

    from app.providers.fallback import FallbackLLM

    return FallbackLLM(primary=primary, secondary=secondary)


_cache: dict[str, object] = {}


def get_llm() -> LLMProvider:
    if "llm" not in _cache:
        _cache["llm"] = _build_llm(get_settings())
    return _cache["llm"]  # type: ignore[return-value]


def get_search() -> SearchProvider:
    if "search" not in _cache:
        settings = get_settings()
        provider = _build(_SEARCH, settings.search_provider, "search", settings)
        _cache["search"] = _maybe_record(provider, "search", settings)
    return _cache["search"]  # type: ignore[return-value]


def get_fetcher() -> Fetcher:
    if "fetch" not in _cache:
        settings = get_settings()
        provider = _build(_FETCH, settings.fetch_provider, "fetch", settings)
        _cache["fetch"] = _maybe_record(provider, "fetch", settings)
    return _cache["fetch"]  # type: ignore[return-value]


def reset_providers() -> None:
    """清空缓存。测试与配置热更新用。"""
    _cache.clear()


def describe_all() -> list[dict]:
    """列出**所有已注册**的 provider 及其能力，供 `/api/providers` 展示。

    不只列当前生效的那个——"我有哪几家可选、各自支持什么、哪家还没配密钥"
    才是这张表的价值所在。

    抓取也列进来。它不是可有可无的一环：抓取失败是这套系统**最主要的降级来源**
    （取不回正文的证据只剩摘要、可信度被扣分），只列 LLM 与搜索会让这张表
    在最需要它的时候缺一块。
    """
    load_builtin_providers()
    settings = get_settings()
    out: list[dict] = []

    for kind in ("llm", "search", "fetch"):
        active_name = getattr(settings, f"{kind}_provider")
        for name in sorted(_REGISTRIES[kind]):
            outcome = try_build(kind, name, settings)
            cred = settings.credentials(name)
            instance = outcome.instance

            if instance is None:
                out.append(_row(kind, name, active_name, cred, {}, None, {}, outcome.error))
                continue

            # 档位逐个查、逐个容错。整体 try 住的话，只是没配某个档位模型名，
            # 就会连能力矩阵一起变成空的——而能力是已知的，没理由不显示。
            models: dict[str, str] | None = None
            if kind == "llm":
                models = {}
                for tier in TIERS:
                    try:
                        models[tier] = instance.resolve_model(tier)
                    except Exception:  # noqa: BLE001 - 未配置的档位留空即可
                        models[tier] = ""

            out.append(
                _row(
                    kind,
                    name,
                    active_name,
                    cred,
                    _dataclass_dict(getattr(instance, "capabilities", None)),
                    models,
                    _pricing_dict(instance) if kind == "llm" else {},
                    "",
                )
            )
    return out


def _pricing_dict(instance) -> dict:
    """定价表 → camelCase。没有定价就返回空字典。

    刻意**不返回 null**：空字典的含义是"这家没告诉我们单价，成本记为 0"，
    前端据此显示"未配置定价"；返回 null 只会让消费点多一次空值判断。
    """
    try:
        table = instance.pricing()
    except Exception as exc:  # noqa: BLE001 - 定价缺失不该拖垮整张表
        log.warning("读取 %s 的定价表失败：%s", instance.name, exc)
        return {}
    return {
        str(model): {
            "inputPerMtokUsd": round(price.input_per_mtok_usd, 6),
            "outputPerMtokUsd": round(price.output_per_mtok_usd, 6),
            # null 而不是 0：这家没区分缓存命中价。"缓存不计费"与"没告诉我们"
            # 是两件事，前端据此决定显示"未配置"还是"¥0"。
            "cachedInputPerMtokUsd": (
                None
                if price.cached_input_per_mtok_usd is None
                else round(price.cached_input_per_mtok_usd, 6)
            ),
            "sourceCurrency": price.source_currency,
            "note": price.note,
        }
        for model, price in sorted(table.items())
    }


def _row(
    kind: str,
    name: str,
    active_name: str,
    cred,
    caps: dict,
    models: dict | None,
    pricing: dict,
    error: str,
) -> dict:
    """一行能力矩阵。

    `configured` 就是"构造成功了"。`error` 非空时说明为什么没成功——
    区分"没配密钥"和"配了但构造不出来"靠读这句话，不靠再开一个布尔字段：
    两个布尔字段迟早会出现 `configured=true, broken=true` 这种没人定义过的组合。
    """
    return {
        "kind": kind,
        "name": name,
        "active": name == active_name,
        "configured": not error,
        "error": error,
        "baseUrl": cred.base_url,
        "models": models,
        "capabilities": caps,
        "pricing": pricing,
    }


def _dataclass_dict(obj: object) -> dict:
    from dataclasses import asdict, is_dataclass

    return asdict(obj) if is_dataclass(obj) and not isinstance(obj, type) else {}
