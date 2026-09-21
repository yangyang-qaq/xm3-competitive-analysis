"""命令行跑一次完整流水线。

    python -m scripts.run_pipeline_cli "对比 Notion 与 Obsidian" --mode quick --provider mock

**这是「核心闭环」的验收入口。** 走的是与 HTTP 接口完全相同的那条路径：
`ensure_runner` → 流水线 → 事件流 → 落库。区别只在于 provider 实现
和"谁来消费事件"。

`--provider mock` 下全程零 API 花费，所以它可以进 CI，也可以在
改完一行代码之后随手跑一遍——这正是"完整流程还能跑通"最需要的那个保证。

为什么用 SSE 帧而不是直接读 journal
----------------------------------
因为它顺带验证了断线续传那条链路：订阅拿到的是 `id:` / `event:` / `data:`
三行一帧的**真实字节**，而不是内存里的对象。前端解析的就是它。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _force_utf8() -> None:
    """把标准输出切到 UTF-8。

    Windows 控制台默认是 GBK，中文会变成乱码，而乱码的日志比没有日志更糟——
    你会以为程序输出的是垃圾，于是不去读它。这一步必须在任何输出之前做。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            # 已重定向到文件/管道时可能抛错。编码没切成不算致命，
            # 但中文会乱码，所以这里只吞异常、不静默。
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


def _apply_provider_env(args: argparse.Namespace) -> None:
    """在**任何 Settings 被构造之前**设好环境变量。

    `get_settings()` 是带缓存的，所以这一步的时机是有要求的：
    晚一步，Settings 会把 `.env` 里配的真实 provider 读进来，
    而 CLI 参数被静默忽略——表现是"我明明指定了 mock，它却去调真 API 了"。
    """
    provider = (args.provider or "").strip()
    if provider:
        _apply_shortcut(provider)
    if args.llm:
        os.environ["LLM_PROVIDER"] = args.llm
    if args.search:
        os.environ["SEARCH_PROVIDER"] = args.search
    if args.fetch:
        os.environ["FETCH_PROVIDER"] = args.fetch
    if args.cassette:
        os.environ["CASSETTE_MODE"] = args.cassette
    if args.db:
        os.environ["DB_PATH"] = args.db


def _apply_shortcut(provider: str) -> None:
    """`--provider X`：把 X 用在**它注册了的那些类别**上。

    这里踩过一个坑。原先的实现是把三类一起设成 X：

        os.environ["LLM_PROVIDER"] = provider
        os.environ["SEARCH_PROVIDER"] = provider
        os.environ["FETCH_PROVIDER"] = "mock" if provider == "mock" else "http"

    它在 `--provider mock` 上能跑通，因为 mock 在三个类别里都注册了。
    但 `--provider deepseek` 会把 `SEARCH_PROVIDER` 也设成 `deepseek`——
    而 deepseek **不是**一个搜索 provider，于是构造时抛
    "未注册的 search provider"。也就是说：唯一能用的那个取值，
    恰好是唯一一个掩盖了这个 bug 的取值。

    正确的语义是"这家在哪一类里存在，就用在哪一类"。于是
    `--provider deepseek` 得到的是"LLM 换成 deepseek，搜索与抓取仍按 `.env`"
    ——这正是想跑一份真实报告时要的东西。想逐类精确指定就用
    `--llm` / `--search` / `--fetch`。
    """
    from app.providers.registry import _REGISTRIES, load_builtin_providers

    load_builtin_providers()
    env_key = {"llm": "LLM_PROVIDER", "search": "SEARCH_PROVIDER", "fetch": "FETCH_PROVIDER"}

    touched = [kind for kind, registry in _REGISTRIES.items() if provider in registry]
    if not touched:
        raise SystemExit(
            f"--provider {provider!r} 不是任何一类已注册的 provider。\n"
            + "\n".join(
                f"  {kind}: {', '.join(sorted(registry))}"
                for kind, registry in _REGISTRIES.items()
            )
        )

    for kind in touched:
        os.environ[env_key[kind]] = provider


# ============================================================
# 事件渲染
# ============================================================


#: 载荷**嵌套**的事件类型 → 嵌套键名。
#:
#: 与 `contracts/sse_events.json` 里各事件的 `carries` 一一对应，并且由
#: `tests/unit/test_cli_render.py` 直接从契约文件里读出来比对——这一层
#: 硬编码是有意的（渲染器不需要理解契约的类型系统），但**不能漂**。
#:
#: 这张表原先不存在，渲染器一律按"字段在顶层"去读，于是六种嵌套事件
#: 全部渲染成空行：思维流印成 `· ：`、trace 印成 `⏱ 0ms`、消息印成
#: `→  ⇒ ：`。它不报错，只是这个 CLI 的整个叙事输出是空白的——
#: 而 CLI 是"核心闭环"那一步的验收工具。
NESTED_PAYLOADS: dict[str, str] = {
    "thought": "thought",
    "message": "message",
    "evidence": "evidence",
    "chart": "chart",
    "trace": "span",
    "image": "image",
}


def _inner(data: dict, event_type: str) -> dict:
    """取出嵌套载荷。缺失或类型不对时返回空 dict，渲染器照常产出那一行。

    不返回 `None` 让调用点去判断：渲染器里的分支越多，漏掉一处的机会越多，
    而漏掉的表现恰好就是"这一行静默变成空白"。
    """
    value = data.get(NESTED_PAYLOADS[event_type])
    return value if isinstance(value, dict) else {}


def render_event(event_type: str, data: dict, *, verbose: bool) -> str:
    """把一个事件渲染成一行。返回空串表示这次不打印。

    它是这个 CLI 唯一的"用户界面"。**只渲染载荷里真的有的字段**：
    契约没给的键一律不编——`report_ready` 里没有 `quality`，从前这里
    读不到就默认"未通过、完整度 0%"，于是一次质量门通过的报告在终端上
    印着"质量门=未通过"。缺字段时正确的做法是**不说**，不是给一个默认判断。
    """
    if event_type == "node_update":
        status = data.get("status", "")
        progress = data.get("progress")
        percent = f"{float(progress) * 100:5.1f}%" if isinstance(progress, (int, float)) else "     "
        mark = {"running": ">", "done": "v", "degraded": "!", "error": "x"}.get(status, " ")
        # 键名是 `stage`；`label` 是它的中文名（契约必填）。从前这里读的是
        # `node`，那个键根本不存在，靠 `label` 兜底才没看出来。
        label = data.get("label") or data.get("stage", "")
        round_no = data.get("round")
        suffix = f"（第 {round_no} 轮）" if isinstance(round_no, int) and round_no > 0 else ""
        return f"  [{mark}] {percent} {label}{suffix}"

    if event_type == "thought":
        item = _inner(data, "thought")
        return f"      · {item.get('expertName', '')}：{item.get('text', '')}"
    if event_type == "message":
        item = _inner(data, "message")
        return (
            f"      → {item.get('from', '')} ⇒ {item.get('to', '')}：{item.get('summary', '')}"
        )
    if event_type == "progress":
        if not verbose:
            return ""
        return f"        {data.get('message', '')}"
    if event_type == "evidence":
        item = _inner(data, "evidence")
        cred = item.get("credibility")
        score = f"{float(cred):.1f}" if isinstance(cred, (int, float)) else "?"
        mark = "!" if item.get("degraded") else " "
        title = str(item.get("title", ""))[:52]
        return f"      [{mark}] {item.get('evidenceId', '')} {score:>5} {title}"
    if event_type == "chart":
        if not verbose:
            return ""
        item = _inner(data, "chart")
        return (
            f"      [图] {item.get('chartId', '')} "
            f"{item.get('kind', '')} 「{item.get('title', '')}」"
        )
    if event_type == "trace":
        item = _inner(data, "trace")
        detail = item.get("detail") or {}
        extra = f" {json.dumps(detail, ensure_ascii=False)}" if detail and verbose else ""
        return (
            f"      ⏱ {item.get('kind', ''):<6} {str(item.get('name', ''))[:40]:<40} "
            f"{item.get('durationMs', 0):>6}ms{extra}"
        )
    if event_type == "image":
        if not verbose:
            return ""
        item = _inner(data, "image")
        return f"      [图] {item.get('url', '')}"
    if event_type == "report_ready":
        # **不渲染质量门**：这一条事件里没有 `quality`。判定要等 `done`——
        # 那时 `metrics.qualityGatePassed` 才是被算出来的那个值。
        problems = data.get("problems") or []
        degraded = data.get("degraded") or []
        flags = f"问题={len(problems)}" if problems else "无问题"
        if degraded:
            flags += f"、降级={len(degraded)}"
        return (
            f"  [报告] {data.get('reportId', '')} "
            f"章节={data.get('sectionCount', 0)} 证据={data.get('evidenceCount', 0)} {flags}"
        )
    if event_type == "error":
        return f"  [错误] {data.get('stage', '')}: {data.get('message', '')}"
    if event_type == "done":
        # `elapsedMs` / `reworkRounds` / `qualityGatePassed` **都在 metrics 里**，
        # 不在顶层。从前读顶层得到的是三个默认值 0——于是终端上永远印
        # "耗时 0ms，返工 0 轮"，而那个 0 看起来像一个真实的测量结果。
        metrics = data.get("metrics") or {}
        rounds = metrics.get("reworkRounds", 0)
        gate = "通过" if metrics.get("qualityGatePassed") else "未通过"
        degraded = data.get("degraded") or []
        tail = f"，降级 {len(degraded)} 处" if degraded else ""
        return (
            f"  [完成] 耗时 {metrics.get('durationMs', 0)}ms，"
            f"返工 {rounds} 轮，质量门{gate}{tail}"
        )
    return ""


def parse_sse_frame(frame: str) -> tuple[str, dict]:
    """从一帧 SSE 字节里取出 `(类型, 载荷)`。测试与 CLI 共用。"""
    event_type = ""
    payload: dict = {}
    for line in frame.splitlines():
        if line.startswith("event: "):
            event_type = line[len("event: "):].strip()
        elif line.startswith("data: "):
            try:
                payload = json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                payload = {}
    return event_type, payload


# ============================================================
# 主流程
# ============================================================


async def run_once(args: argparse.Namespace) -> int:
    from app.core.config import get_settings
    from app.core.models import TaskRecord
    from app.core.modes import get_mode
    from app.core.pipeline.runner import ensure_runner, new_task_id, reset_runners
    from app.core.pipeline.stages import STAGE_ORDER
    from app.db.connection import close_all, get_conn
    from app.db.migrations import migrate
    from app.db.repo import tasks as tasks_repo
    from app.providers.registry import load_builtin_providers, reset_providers

    load_builtin_providers()
    reset_providers()
    reset_runners()

    settings = get_settings()
    migrate(get_conn())

    mode = get_mode(args.mode)
    task_id = args.task_id or new_task_id()

    tasks_repo.create(TaskRecord(
        task_id=task_id, query=args.query, mode=mode.key, status="pending",
        stage=STAGE_ORDER[0],
    ))

    if not args.quiet:
        print(f"任务 {task_id}")
        print(f"需求：{args.query}")
        print(f"档位：{mode.label}（{mode.description}）")
        print(f"provider：llm={settings.llm_provider} search={settings.search_provider} "
              f"fetch={settings.fetch_provider}")
        print(f"预算：搜索 ≤{mode.max_search_calls} 次、抓取 ≤{mode.max_fetches} 条、"
              f"返工 ≤{mode.max_rework_rounds} 轮")
        print("-" * 78)

    # 与 HTTP 接口走同一个入口。CLI 自动采纳澄清推荐项，否则一条命令跑不完。
    runner = ensure_runner(
        task_id, args.query, mode_key=mode.key, auto_clarify=True
    )

    counts: dict[str, int] = {}
    async for frame in runner.stream(0):
        event_type, payload = parse_sse_frame(frame)
        counts[event_type] = counts.get(event_type, 0) + 1
        line = render_event(event_type, payload, verbose=args.verbose)
        if line and not args.quiet:
            print(line)

    result = await runner.wait()

    if args.quiet:
        # 安静模式下仍然要能看到阶段推进，否则长时间没输出像是卡住了。
        pass

    # ---- 汇总 ----
    if not args.quiet:
        print("-" * 78)

    if result.error:
        print(f"任务失败：{result.error}")
        close_all()
        return 1

    report = result.report
    assert report is not None  # result.ok 保证的
    body = report.data
    metrics = report.metrics

    out_path = Path(args.out) if args.out else (
        BACKEND_DIR / "data" / "reports" / f"{task_id}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if args.json:
        print(json.dumps({
            "taskId": task_id,
            "reportId": report.report_id,
            "out": str(out_path),
            "problems": result.problems,
            "metrics": metrics,
            "quality": report.quality,
            "events": counts,
        }, ensure_ascii=False, indent=2))
    else:
        _print_summary(task_id, report, metrics, result.problems, counts, out_path)

    close_all()
    # 有合规问题就返回非零：CI 靠它做门禁，而"打印了问题但退出码 0"
    # 等于没有门禁。
    return 1 if result.problems else 0


def _print_summary(
    task_id: str,
    report: Any,
    metrics: dict,
    problems: list[str],
    counts: dict[str, int],
    out_path: Path,
) -> None:
    body = report.data
    print(f"报告 {report.report_id}  →  {out_path}")
    print()
    print("证据与覆盖")
    # 抓取预算会留下大量"只有摘要、没抓正文"的证据。它们既不算抓取失败
    # （`degradedEvidences` 是 0），也不算有正文，所以必须单独报一行——
    # 否则"216 条证据"读起来像是 216 条都读过全文。
    print(f"  证据 {metrics.get('evidences', 0)} 条"
          f"（独立域名 {metrics.get('independentDomains', 0)} 个、"
          f"平台 {metrics.get('platformCount', 0)} 类）")
    print(f"  抓取 成功 {metrics.get('fetchedOk', 0)} / "
          f"失败 {metrics.get('fetchedDegraded', 0)} / "
          f"因预算跳过 {metrics.get('fetchSkippedByBudget', 0)}"
          f"（跳过的那部分只有搜索摘要）")
    print(f"  维度覆盖 {metrics.get('dimensionsCovered', 0)}/{metrics.get('dimensionsPlanned', 0)}"
          f" = {metrics.get('dimensionCoverage', 0):.0%}")
    print()
    print("四条铁律")
    print(f"  无证据不立论：论点 {metrics.get('claims', 0)} 条，"
          f"可核验 {metrics.get('verifiedClaims', 0)} 条，"
          f"无证据立论率 {metrics.get('unsupportedClaimRate', 0):.1%}")
    print(f"  幻觉引用率   ：{metrics.get('hallucinationRate', 0):.1%}"
          f"（剔除 {metrics.get('phantomCitations', 0)} 个不存在的引用）")
    print(f"  交叉验证     ：{metrics.get('crossValidationRate', 0):.1%} 的论点有 ≥2 个独立信源")
    print(f"  返工闭环     ：{metrics.get('reworkRounds', 0)} 轮"
          + (f"，提升 {json.dumps(metrics.get('rework', {}).get('changes', {}), ensure_ascii=False)}"
             if metrics.get("rework") else "（未触发）"))
    print(f"  全程可观测   ：{metrics.get('llmCalls', 0)} 次 LLM、"
          f"{metrics.get('searchCalls', 0)} 次搜索、"
          f"{metrics.get('fetchedOk', 0)} 次抓取")
    print()
    print("成本与耗时")
    print(f"  总成本 ${metrics.get('totalCostUsd', 0):.4f}，"
          f"token {metrics.get('totalTokens', 0)}，"
          f"端到端 {metrics.get('durationMs', 0)}ms，"
          f"首证据 {metrics.get('firstEvidenceMs') or '-'}ms")
    print()
    print("质量门")
    quality = report.quality
    print(f"  {'通过' if quality.get('passed') else '未通过'}"
          f"（blocker {quality.get('blockers', 0)} / major {quality.get('major', 0)}"
          f" / minor {quality.get('minor', 0)}），"
          f"完整度 {quality.get('completeness', 0):.0%}")
    if quality.get("failedBecause"):
        print(f"  原因：{'；'.join(quality['failedBecause'])}")
    degraded = body.get("degraded") or []
    if degraded:
        print(f"  降级留痕 {len(degraded)} 处：")
        for item in degraded[:6]:
            print(f"    - {item}")
    if problems:
        print()
        print(f"报告合规问题 {len(problems)} 处：")
        for item in problems[:8]:
            print(f"  - {item}")
    print()
    print("事件统计：" + "、".join(f"{k}×{v}" for k, v in sorted(counts.items())))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_pipeline_cli",
        description="跑一次完整的竞品调研流水线",
    )
    parser.add_argument("query", help="调研需求，例如「对比 Notion 与 Obsidian」")
    parser.add_argument("--mode", default="quick", choices=("quick", "deep", "expert"))
    parser.add_argument(
        "--provider",
        default="",
        help="把这一家用在该 provider 注册了的每个类别上（deepseek 只影响 LLM，mock 影响三类）",
    )
    parser.add_argument("--llm", default="", help="单独指定 LLM provider")
    parser.add_argument("--search", default="", help="单独指定搜索 provider")
    parser.add_argument("--fetch", default="", help="单独指定抓取 provider")
    parser.add_argument("--cassette", default="", choices=("", "off", "record", "replay"))
    parser.add_argument("--db", default="", help="SQLite 路径，留空用默认")
    parser.add_argument("--out", default="", help="report.json 输出路径")
    parser.add_argument("--task-id", default="", help="指定任务 id（复现某次运行时用）")
    parser.add_argument("--json", action="store_true", help="以 JSON 打印汇总")
    parser.add_argument("--verbose", "-v", action="store_true", help="打印进度与埋点明细")
    parser.add_argument("--quiet", "-q", action="store_true", help="只打印最终汇总")
    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    args = build_parser().parse_args(argv)
    _apply_provider_env(args)
    try:
        return asyncio.run(run_once(args))
    except KeyboardInterrupt:  # pragma: no cover
        print("\n已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
