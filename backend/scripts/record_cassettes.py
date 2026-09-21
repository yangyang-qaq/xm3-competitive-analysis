"""录制黄金集 cassette —— 唯一一个**故意花钱**的脚本。

    python -m scripts.record_cassettes --dry-run          # 只估不跑
    python -m scripts.record_cassettes --yes              # 录全部
    python -m scripts.record_cassettes --only notion-vs-obsidian --yes
    python -m scripts.record_cassettes --limit 3 --yes

为什么需要一个专门的脚本，而不是"把 CASSETTE_MODE=record 然后跑测试"
--------------------------------------------------------------------
三条理由，每条都对应一个真实的坏结果：

1. **它会花钱，而花钱必须是一次主动行为。** 跑测试不该联网（`addopts` 默认
   排除 `live`，`CASSETTE_MODE` 默认 `off`，缺录制抛 `CassetteMiss`——
   整套设计的目的是让"跑测试 = 零成本"）。录制是这个体系里唯一的出口，
   所以出口要有人看着：先打印估算，`--yes` 才开跑。
2. **它要能续跑。** 录到第 7 条被打断是常事（限流、断网、看花了眼）。
   `RecordingProvider` 对已经录过的 key 直接复用、不再买一次，
   所以再跑一遍只补缺的那些——这一点由结尾的"本次联网新录 N 条"
   **报出来**，而不是让人猜。

   这一句原先是**先写下的愿望**，代码里并没有实现（见 `_record_counts` 上面
   那段注释里记的实测）。一条"文档说会报，实际不报"的说明，比没有说明更坏：
   它会让人以为自己已经看过了那个数。
3. **它要留下凭据。** 一份 cassette 是"哪一天、用哪家 provider、哪个模型、
   花了多少钱"录的，直接决定这份回放能证明什么。跑完写 `MANIFEST.json`。

估算的诚实程度
--------------
下面的估算**来自一次实测运行**（quick 档、报告 `RP-4583f574eab8`），
按档位的章节数与写作档位价格折算。它不是"拍一个数"，但也**不是保证**：
它只按章节数缩放，而 deep/expert 档的维度数、返工轮次都会让实际调用更多。
所以脚本在**跑完第一条之后会用实测值重新投影一次总量**——那才是可信的数，
一旦超出预算就停下来问。先估、再实测、再决定要不要继续，这三步是有顺序的。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

DEFAULT_GOLDEN = REPO_DIR / "eval" / "golden.yaml"
MANIFEST_NAME = "MANIFEST.json"

#: 一次**实测**的 quick 档运行（2026-09-15，deepseek + 博查，报告 RP-4583f574eab8）。
#: 把实测值写进代码而不是"估一个"：下面所有的推算都建立在这几个数上，
#: 它们错了的话，估算会错得很有说服力。
_BASELINE = {
    "mode": "quick",
    "llm_calls": 13,
    "prompt_tokens": 76_332,
    "completion_tokens": 12_135,
    "cost_usd": 0.01836,
}


def _force_utf8() -> None:
    """Windows 控制台默认 GBK，中文会变乱码——而乱码的日志比没有日志更糟。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            # 已重定向到文件/管道时可能抛错。编码没切成不算致命，
            # 但中文会乱码，所以这里只吞异常、不静默。
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


# ============================================================
# 黄金集
# ============================================================


@dataclass(frozen=True)
class GoldenQuery:
    name: str
    query: str
    mode: str
    tags: tuple[str, ...] = ()
    note: str = ""
    ambiguous: bool = False

    @property
    def label(self) -> str:
        """打点用的一行标签。`note` 是给人读的，不进日志。"""
        mark = "（歧义）" if self.ambiguous else ""
        return f"{self.name}{mark} · {self.mode}"


def load_golden(path: Path) -> list[GoldenQuery]:
    """读黄金集。`queries` 与 `ambiguity` 两段合成一串——它们跑的是同一条流水线，
    只是后者走澄清分支，没有理由在脚本层分成两条代码路径。"""
    import yaml

    if not path.exists():
        raise SystemExit(f"找不到黄金集：{path}")

    data = yaml.safe_load(path.read_text("utf-8")) or {}
    out: list[GoldenQuery] = []
    for section, ambiguous in (("queries", False), ("ambiguity", True)):
        for raw in data.get(section) or []:
            out.append(
                GoldenQuery(
                    name=str(raw["name"]),
                    query=str(raw["query"]),
                    mode=str(raw.get("mode", "quick")),
                    tags=tuple(raw.get("tags") or ()),
                    note=str(raw.get("note") or ""),
                    ambiguous=ambiguous,
                )
            )
    if not out:
        raise SystemExit(f"黄金集是空的：{path}")
    names = [g.name for g in out]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        # 名字重复会让 `--only` 与 MANIFEST 的归属说不清，也会让报告文件名互撞。
        raise SystemExit(f"黄金集里有重名条目：{duplicates}")
    return out


# ============================================================
# 估算
# ============================================================


def _tier_price_ratio(llm: Any, tier: str, base_tier: str) -> tuple[float, str]:
    """两个档位的**输出**单价之比。拿不到定价时返回 1.0 并说明原因。

    只看输出价：一次 LLM 调用里输出 token 比输入 token 少了将近一个数量级
    （实测 12135 / 76332），但单价高一个数量级，两边对成本的贡献是同一量级。
    在这里做精细拆分换不来更准的估算——章节数才是主导项。
    """
    try:
        table = llm.pricing()
        now = table.get(llm.resolve_model(tier))
        base = table.get(llm.resolve_model(base_tier))
        if now is None or base is None or not base.output_per_mtok_usd:
            return 1.0, "该档模型没有定价，未折算"
        return now.output_per_mtok_usd / base.output_per_mtok_usd, ""
    except Exception as exc:  # noqa: BLE001 - 定价缺失不该让录制跑不起来
        return 1.0, f"取定价失败（{type(exc).__name__}），未折算"


def estimate(query: GoldenQuery, llm: Any) -> dict:
    """单条 query 的估算。返回的每一项都标了它是**估算**还是**上限**。"""
    from app.core.modes import MODE_CONFIG

    mode = MODE_CONFIG[query.mode]
    base_mode = MODE_CONFIG[_BASELINE["mode"]]
    section_ratio = len(mode.sections) / len(base_mode.sections)
    tier_ratio, tier_note = _tier_price_ratio(llm, mode.writing_tier, base_mode.writing_tier)

    # 向上取整：估少了会让人在跑的中途才发现超预算。
    calls = math.ceil(_BASELINE["llm_calls"] * section_ratio)
    return {
        "name": query.name,
        "mode": query.mode,
        "llmCalls": calls,
        "costUsd": round(_BASELINE["cost_usd"] * section_ratio * tier_ratio, 4),
        # 下面两个不是估算，是档位里的**硬上限**：预算控制靠它们，不靠"感觉差不多了"。
        "searchCallsMax": mode.max_search_calls,
        "fetchesMax": mode.max_fetches,
        "tierNote": tier_note,
    }


def print_estimate(estimates: list[dict], llm: Any) -> None:
    from app.core.modes import MODE_CONFIG

    total_cost = sum(e["costUsd"] for e in estimates)
    total_calls = sum(e["llmCalls"] for e in estimates)

    print("预估（按章节数从一次实测的 quick 档运行折算，不是保证）")
    print(f"  基准：{_BASELINE['mode']} 档 {_BASELINE['llm_calls']} 次 LLM 调用、"
          f"{_BASELINE['prompt_tokens']}+{_BASELINE['completion_tokens']} token、"
          f"${_BASELINE['cost_usd']:.5f}")
    print(f"  本次：{len(estimates)} 条 query，约 {total_calls} 次 LLM 调用，"
          f"约 ${total_cost:.3f}")
    print("  说明：只按章节数缩放，**未计**维度数与返工轮次带来的额外调用，"
          "所以偏乐观。")
    notes = sorted({e["tierNote"] for e in estimates if e["tierNote"]})
    for note in notes:
        print(f"  ⚠ {note}")

    by_mode: dict[str, list[dict]] = {}
    for e in estimates:
        by_mode.setdefault(e["mode"], []).append(e)
    for key, group in sorted(by_mode.items()):
        mode = MODE_CONFIG[key]
        print(f"  {key:<6} {len(group):>2} 条 · 搜索 ≤{mode.max_search_calls} 次/条 · "
              f"抓取 ≤{mode.max_fetches} 条/条")
    print()


# ============================================================
# 环境
# ============================================================


def _prepare_env(args: argparse.Namespace) -> None:
    """设好环境变量。**必须在任何 Settings 被构造之前调用。**

    与 `run_pipeline_cli` 的那套是同一个理由：`get_settings()` 每次新构造一个
    `Settings`，所以环境变量必须先落地；`reset_providers()` 负责让已经缓存的
    provider 实例作废。
    """
    os.environ["CASSETTE_MODE"] = "record"
    os.environ["CASSETTE_DIR"] = str(args.cassette_dir)
    # 录制写的是另一个库：黄金集跑一遍会落下几百条证据与 trace，
    # 混进开发用的 xm3.db 之后，"我昨天手动跑的那次"就找不出来了。
    os.environ["DB_PATH"] = str(args.db)

    if args.llm:
        os.environ["LLM_PROVIDER"] = args.llm
    if args.search:
        os.environ["SEARCH_PROVIDER"] = args.search
    if args.fetch:
        os.environ["FETCH_PROVIDER"] = args.fetch


def _guard_against_mock() -> None:
    """拒绝在 mock provider 上录制。

    mock 的响应是**编出来的**。录下来一份"mock 说过什么"的 cassette，
    回放当然永远绿——而它一个字都不能说明真实 provider 的行为。
    这种绿比红更坏，因为它会被当成"录制好了、CI 守住了"。

    这条守卫值得存在，是因为踩中它的方式是**默认值**：mock 是唯一零成本的
    provider，也就最容易被顺手选上。
    """
    from app.core.config import get_settings

    settings = get_settings()
    used = {
        "LLM": settings.llm_provider,
        "搜索": settings.search_provider,
        "抓取": settings.fetch_provider,
    }
    mocked = [kind for kind, name in used.items() if name == "mock"]
    if mocked:
        raise SystemExit(
            f"拒绝录制：{'、'.join(mocked)} 用的是 mock provider。\n"
            "mock 的响应是编出来的，录下来的 cassette 回放永远绿，\n"
            "但它证明不了真实 provider 的任何行为。\n"
            "请在 .env 里配好真实 provider，或用 --llm/--search/--fetch 指定。"
        )


# ============================================================
# 跑一条
# ============================================================


async def run_one(item: GoldenQuery, args: argparse.Namespace) -> dict:
    """跑完一条 query，返回这一条的实测数据。"""
    from app.core.models import TaskRecord
    from app.core.modes import get_mode
    from app.core.pipeline.runner import ensure_runner, new_task_id, reset_runners
    from app.db.repo import tasks as tasks_repo
    from app.db.repo import traces as traces_repo

    mode = get_mode(item.mode)
    task_id = new_task_id()
    reset_runners()

    tasks_repo.create(TaskRecord(
        task_id=task_id, query=item.query, mode=mode.key, status="pending",
        stage="intake",
    ))

    started = time.monotonic()
    # `auto_clarify=True`：CLI 自动采纳澄清推荐项，否则歧义用例会卡在等输入上，
    # 而这是一个无人值守的脚本。
    runner = ensure_runner(task_id, item.query, mode_key=mode.key, auto_clarify=True)
    async for _frame in runner.stream(0):
        pass
    result = await runner.wait()
    wall_ms = int((time.monotonic() - started) * 1000)

    spans = traces_repo.list_by_task(task_id)
    llm_spans = [s for s in spans if s.get("kind") == "llm"]
    cost = sum(float(s.get("costUsd") or 0.0) for s in spans)
    prompt = sum(int(s.get("promptTokens") or 0) for s in llm_spans)
    completion = sum(int(s.get("completionTokens") or 0) for s in llm_spans)
    cached = sum(int(s.get("cachedPromptTokens") or 0) for s in llm_spans)

    report = result.report
    return {
        "name": item.name,
        "query": item.query,
        "mode": item.mode,
        "tags": list(item.tags),
        "taskId": task_id,
        "reportId": report.report_id if report else "",
        "ok": not result.error and report is not None,
        "error": result.error,
        "wallMs": wall_ms,
        "llmCalls": len(llm_spans),
        "promptTokens": prompt,
        "completionTokens": completion,
        "cachedPromptTokens": cached,
        "costUsd": round(cost, 6),
        "problems": list(result.problems),
        # 降级要报出来而不是藏起来：录一份"带着 5 处降级的报告"当黄金集，
        # 回放时那些降级会一直存在，而没人会知道它们是录进来时就有的。
        # 降级块在报告正文的 `degraded` 里（`assemble.py` 从 `ctx.degraded_blocks` 灌进去），
        # 不是 `ReportRecord` 上的字段——它属于正文，不属于索引。
        "degradedCount": len(report.data.get("degraded") or []) if report else 0,
    }


# ============================================================
# 主流程
# ============================================================


async def main_async(args: argparse.Namespace) -> int:
    from app.core.config import get_settings
    from app.db.connection import close_all, get_conn, reset_connections
    from app.db.migrations import migrate
    from app.providers.registry import get_llm, load_builtin_providers, reset_providers

    golden = load_golden(Path(args.golden))
    selected = [g for g in golden if not args.only or g.name in set(args.only)]
    if args.limit:
        selected = selected[: args.limit]
    if not selected:
        raise SystemExit("没有任何 query 被选中（检查 --only / --limit）")

    _prepare_env(args)
    load_builtin_providers()
    reset_providers()
    reset_connections()
    settings = get_settings()
    _guard_against_mock()

    llm = get_llm()
    estimates = [estimate(g, llm) for g in selected]
    print_estimate(estimates, llm)

    print(f"provider：llm={settings.llm_provider} search={settings.search_provider} "
          f"fetch={settings.fetch_provider}")
    print(f"cassette：{settings.cassette_path}")
    print(f"数据库：{settings.db_file}（录制专用，不动开发库）")
    print()

    if args.dry_run:
        print("--dry-run：只估算，不发出任何请求。")
        return 0

    if not args.yes:
        # 读到 EOF（管道输入）时按"否"处理：一个自动化调用不该因为读不到回答
        # 就默认开始花钱。
        try:
            answer = input(f"确认开始录制？会真实调用 API，预估 ${sum(e['costUsd'] for e in estimates):.3f}。"
                           f"输入 yes 继续：").strip().lower()
        except EOFError:
            answer = ""
        if answer not in {"yes", "y"}:
            print("已取消。加上 --yes 可以跳过这一步。")
            return 1

    migrate(get_conn())

    print("-" * 78)
    results: list[dict] = []
    started_at = time.time()
    before = _record_counts(settings.cassette_path)
    aborted = ""

    for index, item in enumerate(selected, start=1):
        prefix = f"[{index}/{len(selected)}] {item.label}"
        print(f"{prefix} …", flush=True)
        try:
            record = await run_one(item, args)
        except Exception as exc:  # noqa: BLE001 - 一条失败不该毁掉已录的部分
            # 已经录进 cassette 的调用是**有效资产**：它们落盘了，下次不会重买。
            # 所以这里失败就记下来继续，最后用非零退出码报出来。
            record = {
                "name": item.name, "query": item.query, "mode": item.mode,
                "ok": False, "error": f"{type(exc).__name__}：{exc}",
                "llmCalls": 0, "costUsd": 0.0, "wallMs": 0,
            }
        results.append(record)

        if record["ok"]:
            print(f"    ✓ {record['reportId']} · {record['llmCalls']} 次调用 · "
                  f"${record['costUsd']:.5f} · {record['wallMs']} ms"
                  + (f" · {record['degradedCount']} 处降级" if record.get("degradedCount") else ""))
            if record.get("problems"):
                # 出库校验有问题的报告也录进来了——但要说清楚，因为回放时
                # 这些问题会一直在，不该被当成"回放环境的问题"。
                print(f"    ⚠ 出库校验 {len(record['problems'])} 项：{record['problems'][:2]}")
        else:
            print(f"    ✗ 失败：{record['error']}")

        # ---- 第一条跑完，用实测值重新投影 ----
        if index == 1 and len(selected) > 1 and record["ok"]:
            measured = sum(r["costUsd"] for r in results if r["ok"])
            projected = measured * len(selected)
            print(f"    ── 实测已花 ${measured:.4f}；按此投影全部 {len(selected)} 条 "
                  f"约 ${projected:.3f}（预估 ${sum(e['costUsd'] for e in estimates):.3f}）")
            if args.budget and projected > args.budget:
                aborted = (
                    f"实测投影 ${projected:.3f} 超过预算 ${args.budget:.3f}，"
                    f"已在第 1 条之后停下。"
                )
                print(f"    ⚠ {aborted}")
                print("    （已录的部分是有效的，续跑不会重复买；"
                      "要放宽就调 --budget。）")
        print()

        if aborted:
            break

    # 这次**到底**联网了几次。必须在写 manifest 之前算：它要进 manifest。
    #
    # 上面那个 `$` 是**整条流水线跑一遍的用量**，复用的部分也算在里面——
    # 因为 `record_llm_usage` 读的是响应里的 usage，而回放的响应照样带着
    # 当初录下的 token 数。它回答的是"这份报告值多少钱"，
    # **不是**"这次从你账上扣了多少"。
    #
    # 实测踩到过：改了一处提示词之后只补录 5 条，脚本却印出 `$0.133`
    # （与上一次全量录制几乎一样），而那次真实扣费不到一分钱。
    # 数字本身没错，错的是**它旁边没有任何东西说明它是哪个意思**。
    # 一个会被读成"我刚花了这么多"的数字，必须给出对应的事实。
    #
    # 判据取**文件新增的行数**而不是 provider 的 `reused` 计数：
    # 后者要在 llm/search/fetch 三个包装器上分别取，而文件行数是同一件事的
    # 事实来源，且它天然包含"哪些 kind 被联网了"这个更有用的信息。
    after = _record_counts(settings.cassette_path)
    fresh = {name: after.get(name, 0) - before.get(name, 0) for name in after}

    # ---- MANIFEST ----
    manifest = {
        "recordedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(started_at)),
        "golden": str(Path(args.golden).name),
        "cassetteDir": str(settings.cassette_path),
        "providers": {
            "llm": settings.llm_provider,
            "search": settings.search_provider,
            "fetch": settings.fetch_provider,
        },
        "models": {
            tier: _safe_model(llm, tier) for tier in ("core", "aux", "fast")
        },
        "estimate": {
            "basedOn": _BASELINE,
            "costUsd": round(sum(e["costUsd"] for e in estimates), 4),
        },
        "measured": {
            "queries": len(results),
            "ok": sum(1 for r in results if r["ok"]),
            "llmCalls": sum(r["llmCalls"] for r in results),
            "promptTokens": sum(r.get("promptTokens", 0) for r in results),
            "completionTokens": sum(r.get("completionTokens", 0) for r in results),
            "cachedPromptTokens": sum(r.get("cachedPromptTokens", 0) for r in results),
            "costUsd": round(sum(r["costUsd"] for r in results), 6),
            "wallSeconds": round(time.time() - started_at, 1),
        },
        "aborted": aborted,
        "queries": results,
    }
    # `freshRecords` 落进 manifest 而不只印在终端：manifest 是**跟着仓库走**
    # 的那份凭据，而终端输出会随会话消失。读到这份文件的下一件想知道的事就是
    # "这份录像是买的还是续的"——尤其 `costUsd` 在纯复用时依然是个正数。
    manifest["measured"]["freshRecords"] = fresh
    manifest["measured"]["costUsdNote"] = (
        "costUsd 是整条流水线的用量（含回放的调用），不是本次扣费；"
        "本次真正联网新录的条数见 freshRecords"
    )

    # `queries` / `costUsd` 这些描述的是**这一次运行**，而 `cassetteTotals`
    # 描述的是**这份 cassette 里现在有什么**。两者不是一回事，而原先只有一个：
    # 用 `--only X` 续录一次之后，manifest 会写着 `queries: 1`，
    # 而录着 5 条 query 的文件就在它旁边——一份"关于仓库里的资产"的凭据
    # 说出了与资产不符的话。所以两个都写，各自标明在说哪件事。
    manifest["cassetteTotals"] = after
    manifest["cassetteTotalsNote"] = (
        "上面 measured 描述的是**这一次运行**；这里描述的是 cassette 里"
        "**现在合计**有多少条记录"
    )
    manifest_path = settings.cassette_path / MANIFEST_NAME
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ---- 汇总 ----
    print("-" * 78)
    _print_files(settings.cassette_path)
    measured = manifest["measured"]
    print(f"实测：{measured['ok']}/{measured['queries']} 条成功 · "
          f"{measured['llmCalls']} 次 LLM 调用 · "
          f"{measured['promptTokens']}+{measured['completionTokens']} token"
          + (f"（命中 {measured['cachedPromptTokens']}）" if measured["cachedPromptTokens"] else "")
          + f" · ${measured['costUsd']:.5f} · {measured['wallSeconds']}s")
    if measured["promptTokens"]:
        hit = measured["cachedPromptTokens"] / measured["promptTokens"]
        print(f"      缓存命中率 {hit:.1%}")

    bought = sum(fresh.values())
    if bought:
        detail = "、".join(f"{name} +{n}" for name, n in fresh.items() if n)
        print(f"      本次联网新录 {bought} 条（{detail}），其余走已有录制（未联网、未计费）")
        print("      上方的 $ 是整条流水线的用量，不是本次的扣费")
    else:
        print("      本次全部命中已有录制：**未联网、未产生任何费用**")
    print(f"凭据：{manifest_path}")

    close_all()

    failed = [r for r in results if not r["ok"]]
    if aborted:
        return 2
    if failed:
        print(f"\n{len(failed)} 条失败：{[r['name'] for r in failed]}")
        # 非零退出：一份"缺了几条"的 cassette 会让 CI 在回放时抛 CassetteMiss，
        # 而那时离"是谁没录上"已经很远了。在这里就把话说清楚。
        return 1
    print("\n录制完成。CI 用 CASSETTE_MODE=replay 回放，不再联网。")
    return 0


def _safe_model(llm: Any, tier: str) -> str:
    try:
        return llm.resolve_model(tier)
    except Exception:  # noqa: BLE001 - 未配置的档位留空即可
        return ""


def _record_counts(cassette_dir: Path) -> dict[str, int]:
    """每个 cassette 文件里有多少条记录。

    数**行数**而不是 JSON 条数：这个数只用来做前后差分（"这次新录了几条"），
    而写入是"一行一条、写完就 flush"。为了差分去解析几 MB 的 JSON
    没有意义，读坏一行也只会让差分偏 1，不会让谁做出错误决定。
    """
    counts: dict[str, int] = {}
    for path in sorted(cassette_dir.glob("*.jsonl")):
        try:
            with path.open("r", encoding="utf-8") as handle:
                counts[path.name] = sum(1 for line in handle if line.strip())
        except OSError:
            continue
    return counts


def _print_files(cassette_dir: Path) -> None:
    """报出每个 cassette 文件的大小。

    体量是要报出来的：抓回的网页正文全在里面，几 MB 是正常的，
    几十 MB 说明黄金集该缩了——而这只有量出来才知道。
    """
    files = sorted(cassette_dir.glob("*.jsonl"))
    if not files:
        print("（还没有 cassette 文件）")
        return
    total = 0
    for path in files:
        size = path.stat().st_size
        total += size
        print(f"  {path.name:<32} {size / 1024:>8.1f} KB")
    print(f"  {'合计':<32} {total / 1024:>8.1f} KB")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="record_cassettes",
        description="录制黄金集 cassette（会真实调用 API，会花钱）",
    )
    parser.add_argument("--golden", default=str(DEFAULT_GOLDEN),
                        help=f"黄金集路径（默认 {DEFAULT_GOLDEN}）")
    parser.add_argument("--only", action="append", default=[],
                        help="只录这几条（按 name，可重复）")
    parser.add_argument("--limit", type=int, default=0, help="只录前 N 条")
    parser.add_argument("--yes", action="store_true", help="跳过确认")
    parser.add_argument("--dry-run", action="store_true", help="只估算，不发请求")
    parser.add_argument("--budget", type=float, default=1.0,
                        help="实测投影超过这个美元数就在第一条之后停下（默认 1.0）")
    parser.add_argument("--cassette-dir", default="tests/fixtures/cassettes",
                        help="cassette 输出目录（相对 backend/）")
    parser.add_argument("--db", default="data/golden.db",
                        help="录制专用的数据库文件（相对 backend/），不动开发库")
    parser.add_argument("--llm", default="", help="覆盖 LLM_PROVIDER")
    parser.add_argument("--search", default="", help="覆盖 SEARCH_PROVIDER")
    parser.add_argument("--fetch", default="", help="覆盖 FETCH_PROVIDER")
    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        # 中断是正常操作（录到一半不想录了）。已经落盘的那些是有效资产，
        # 所以要说明这一点，而不是只留一个 traceback。
        print("\n已中断。已录进 cassette 的调用不会重买，重跑只补缺的那些。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
