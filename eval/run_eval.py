"""黄金集评测：把「改完是变好了还是变坏了」变成一个能跑出来的数。

    python -m eval.run_eval --gate                       # 零成本，进 CI
    python -m eval.run_eval --only notion-vs-obsidian --gate
    python -m eval.run_eval --replay                     # 只跑录过的，不联网
    python -m eval.run_eval --live --repeat 3 --out eval/reports/live.json
    python -m eval.run_eval --from-db                    # 直接读库里已有的报告

四种取数方式，各自回答不同的问题
--------------------------------
| 方式 | 跑什么 | 回答的问题 | 花钱 |
|---|---|---|---|
| `--gate` | 黄金集 × **mock** provider | 代码改完，流水线的结构性行为有没有退步 | 0 |
| `--replay` | 有录制物的 query × cassette 回放 | **真实适配器那条路径**离线还能不能走通 | 0 |
| `--live` | 黄金集 × 真实 provider | 这套东西现在的质量到底是多少 | 有 |
| `--from-db` | 库里已有的报告 | 昨天那批报告长什么样（不重跑） | 0 |

**`--gate` 用 mock**，这是与计划书不同的一处（计划写的是「在 cassette 集上 `--gate`」），
理由和它的**能力边界**都要写清楚，否则这条闸门会被当成它做不到的东西：

cassette 是按**请求内容**做 key 的，录一条只覆盖一条。黄金集 22 条全录要真花钱，
而且录了之后 prompt 改一个字就全部 miss。所以拆成两条路：

- `--gate`（mock，零成本，每次改动都能跑）**只能证明结构性行为没退步**：
  维度覆盖算错、降级计数丢掉、返工池又合回一个、证据进不了图——
  这些都会让阈值破线。
- `--replay`（cassette，零成本）走**真实适配器**那条路径，
  在**真实输入**上量同一批指标。质量类的回归（模型输出变了、
  搜索源返回变差）只有这条能抓到。

**必须说清楚 mock 闸门抓不到什么。** mock 的三个 provider 都不看 query——
实测跑 `notion-vs-obsidian` / `su7-vs-zeekr007` / `vague-coffee-market` 三条，
指标逐项相同（216 条证据、33 个信源、4 个维度、覆盖率 1.00）。所以"跑了 22 条"
在 mock 下**不等于"覆盖了 22 种输入形态"**，它就是同一种输入跑了 22 遍。
黄金集那份"证据形态的差异"（公开参数表 / 财报 / UGC / 几乎没有公开信息）
只有 `--replay` / `--live` 能吃进去。

换句话说：**mock 闸门是回归网，不是质量尺。** 它的绿色只说明"没坏"，
不说明"好"。质量那句话只能由 `--replay` 和 `--live` 说。

两件事：闸门与基线
------------------
- **闸门**（`eval/thresholds.yaml`）是硬契约，写的是"不管哪条 query 都不许破"的线，
  比的是**最差/最贵的那一条**（见 `eval.metrics.gate_direction`），不是均值——
  均值会把"一条烂到底"摊薄成"整体还行"。破了就退出码 1。
- **基线**（`--baseline eval/baseline.json`）是漂移检测，比的是"和上次比
  有没有变差超过 X%"。它抓的是**慢慢变坏**：每一条都还在阈值内，
  但整体已经比上个月差了一截。这一步不阻断 CI，只报告。

不写真库
--------
评测跑 22 次流水线，每次都落库。落进 `backend/data/xm3.db` 的话，
一次评测就往"我的调研"里灌 22 份没人看过的报告——而那个库本来就已经被
测试污染过（见 `修补文档.md` D4b）。所以这里在**任何 Settings 被构造之前**
把 `DB_PATH` 指到一个临时文件，跑完删掉。

顺带一个好处：临时库每次是空的，所以评测结果不依赖"库里原来有什么"，
换个机器跑出来的数一样。
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_DIR / "backend"
for _p in (str(REPO_DIR), str(BACKEND_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DEFAULT_THRESHOLDS = REPO_DIR / "eval" / "thresholds.yaml"


def _harness_error() -> type[Exception]:
    """`HarnessError` 的惰性取用。

    这个文件里所有 `app.*` 的导入都在函数内：`sys.path` 是模块级刚拼好的，
    模块级导入 `app` 会让"从仓库根跑"和"从 backend 跑"出现两种行为
    （本文件要支持前者）。所以连一个异常类也照这个规矩来。
    """
    from app.providers.errors import HarnessError

    return HarnessError


def _force_utf8() -> None:
    """Windows 控制台默认 GBK，中文会乱码——而乱码的日志比没有日志更糟，
    你会以为程序输出的是垃圾，于是不去读它。必须在任何输出之前做。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


# ============================================================
# 一次运行
# ============================================================


async def run_once(golden: Any, *, mode: str = "") -> dict[str, Any]:
    """跑一条黄金 query，返回它的报告正文。

    每条之前都 `reset_providers()` + `reset_runners()` + `reset_mock_state()`：
    22 条必须彼此独立。不重置的话，前一条采到的证据 id 会留在 mock 的
    证据池里，后一条的模型就可能"引用"到一条它从没检索过的证据——
    指标会好看，而它好看的原因是上一条的残留。测试夹具里也是这么做的。
    """
    from app.core.models import TaskRecord
    from app.core.modes import get_mode
    from app.core.pipeline.runner import ensure_runner, new_task_id, reset_runners
    from app.core.pipeline.stages import STAGE_ORDER
    from app.db.connection import get_conn
    from app.db.migrations import migrate
    from app.db.repo import tasks as tasks_repo
    from app.providers.mock import reset_mock_state
    from app.providers.registry import load_builtin_providers, reset_providers

    load_builtin_providers()
    reset_providers()
    reset_runners()
    reset_mock_state()
    migrate(get_conn())

    resolved = get_mode(mode or golden.mode)
    task_id = new_task_id()
    tasks_repo.create(
        TaskRecord(
            task_id=task_id,
            query=golden.query,
            mode=resolved.key,
            status="pending",
            stage=STAGE_ORDER[0],
        )
    )

    # 与 CLI / HTTP 走同一个入口。`auto_clarify=True`：
    # 歧义那条（`vague-coffee-market`）本来就该走澄清分支，而评测里
    # 没有人能回答问题，所以让它自动采纳推荐项——这**也是**要被测的路径。
    runner = ensure_runner(
        task_id, golden.query, mode_key=resolved.key, auto_clarify=True
    )

    async for _frame in runner.stream(0):
        pass  # 只为了把流水线推到底；帧本身不参与评测

    result = await runner.wait()
    if result.error:
        if result.harness_error:
            # 夹具的问题**不是**"这条 query 跑挂了"，两者要分开报。
            # 回放时少一条录制就属于这一类。
            raise _harness_error()(f"{golden.name}：{result.error}")
        raise RuntimeError(f"{golden.name}：流水线失败——{result.error}")

    report = result.report
    assert report is not None  # result.ok 保证的
    return report.data


# ============================================================
# 闸门
# ============================================================


def load_thresholds(path: Path) -> dict[str, dict[str, Any]]:
    import yaml

    if not path.exists():
        raise SystemExit(f"找不到阈值文件：{path}")
    data = yaml.safe_load(path.read_text("utf-8")) or {}
    raw = data.get("thresholds") or {}
    if not raw:
        raise SystemExit(f"阈值文件里没有 thresholds：{path}")
    return {str(k): dict(v) for k, v in raw.items()}


def check_gate(
    aggregate: dict[str, dict[str, float]],
    thresholds: dict[str, dict[str, Any]],
    *,
    ran: int,
) -> list[str]:
    """返回违反清单。空列表 = 通过。

    `ran` 是实际跑出来的报告数：**0 条不算通过**。
    一条都没跑的闸门和一条都没破的闸门退出码一样，这是最容易被糊弄过去的一种绿。
    """
    from eval.metrics import gate_direction

    if ran <= 0:
        return ["一条都没跑成——这不算通过"]

    failures: list[str] = []
    for key, spec in sorted(thresholds.items()):
        summary = aggregate.get(key)
        if summary is None:
            failures.append(f"{key}：报告里没有这个指标，无法判定（不是 0）")
            continue

        direction = gate_direction(key)
        # 反着写：`{min: x}` 要拿最小值去比，`{max: y}` 要拿最大值去比。
        bound = spec.get("max" if direction == "max" else "min")
        if bound is None:
            failures.append(
                f"{key}：阈值既没写 min 也没写 max（方向是 {direction}）"
            )
            continue

        # 比率类用**合并比率**判定，不用 min/max——理由见 `metrics.RATE_PAIRS`。
        # 计数类仍然看最差/最贵的那一条：对计数来说，"有没有哪一条特别糟"
        # 确实是要防的事，而对比率来说那只是小分母的噪声。
        what = direction
        if "pooled" in summary:
            actual = summary["pooled"]
            what = "合并比率"
        else:
            actual = summary[direction]

        broke = actual > float(bound) if direction == "max" else actual < float(bound)
        if broke:
            worst = spec.get("why") or ""
            failures.append(
                f"{key}：{what} = {actual:g}，要求 "
                f"{'≤' if direction == 'max' else '≥'} {float(bound):g}"
                f"{f'（{worst}）' if worst else ''}"
            )
    return failures


#: 漂移比对**不比**的指标：计时。
#:
#: 不比的理由不是"它们不重要"，而是"这样比出来的变化不来自被测系统"。
#: 实测（这行常量的由来）：同一条 `--replay` 命令连跑两遍，`durationMs`
#: 从 266 抖到 405（+52%）、`firstEvidenceMs` 从 94 到 125（+33%）——
#: 两次跑的是同一份录制、同一台机器、同一份代码，唯一变的是当时
#: 有没有别的进程在抢 CPU。
#:
#: 而回放模式下的这两个数**连网络时间都不含**，它本来就不是"端到端耗时"，
#: 量的是本地进程调度。拿它做漂移检测的后果是：每次运行都亮一盏黄灯。
#: 常亮的警告等于没有警告——它还会把真正该看的那几条（独立信源数、
#: 已验证论点、幻觉率）淹在噪音里。
#:
#: 真要量耗时的变化，用 `--live` 或 `--from-db`：那里有网络、有模型延迟，
#: 抖动相对量级小得多，也才是用户感知的那个数。
DRIFT_EXCLUDED: frozenset[str] = frozenset({"durationMs", "firstEvidenceMs"})


def diff_baseline(
    aggregate: dict[str, dict[str, float]],
    baseline: dict[str, Any],
    *,
    tolerance: float,
) -> list[str]:
    """和基线比，哪些指标变差了。只报告，不阻断。

    比的是 min/median/max 里的**同一边**（`gate_direction` 那一边），
    因为另一边天然会抖。`tolerance` 是相对变化的容忍度（0.15 = 15%）。
    绝对值很小的指标（如 `phantomCitations` 常是 0）改用绝对增量判，
    否则 0 → 1 会被算成"变差无穷倍"，而它其实只是差了一条。

    计时项被排除在外，理由见 `DRIFT_EXCLUDED`。排除是**静默会误导**的
    那种排除，所以调用方要把这件事打出来（见 `run_gate`）。
    """
    from eval.metrics import gate_direction

    base_agg = baseline.get("aggregate") or {}
    drift: list[str] = []
    for key, summary in sorted(aggregate.items()):
        if key in DRIFT_EXCLUDED:
            continue
        old = base_agg.get(key)
        if not isinstance(old, dict):
            continue
        direction = gate_direction(key)
        if direction not in old:
            continue
        was, now = float(old[direction]), float(summary[direction])
        worse = now > was if direction == "max" else now < was
        if not worse:
            continue
        floor = max(abs(was), 1.0)
        if abs(now - was) / floor > tolerance:
            drift.append(
                f"{key}：{direction} {was:g} → {now:g}"
                f"（{(now - was) / floor * 100:+.1f}%）"
            )
    return drift


# ============================================================
# 输出
# ============================================================


def print_table(aggregate: dict[str, dict[str, float]], thresholds: dict[str, Any]) -> None:
    from eval.metrics import gate_direction

    # 「合并」一列只比率类才有。它和 min/max 并排显示是刻意的：
    # 判定用的是它，但 min 能暴露单条的异常——两个数一起看，
    # 才知道"合并比率掉了"是所有人都掉了，还是有一条掉到底了。
    print(f"{'指标':<26}{'min':>11}{'中位':>11}{'max':>11}{'合并':>11}   闸门")
    print("-" * 78)
    for key in sorted(aggregate):
        s = aggregate[key]
        spec = thresholds.get(key)
        mark = ""
        if spec:
            direction = gate_direction(key)
            bound = spec.get("max" if direction == "max" else "min")
            if bound is not None:
                mark = (
                    f"{'合并' if 'pooled' in s else direction}"
                    f" {'≤' if direction == 'max' else '≥'} {float(bound):g}"
                )
        pooled = f"{s['pooled']:>11.4g}" if "pooled" in s else f"{'—':>11}"
        print(
            f"{key:<26}{s['min']:>11.4g}{s['median']:>11.4g}{s['max']:>11.4g}"
            f"{pooled}   {mark}"
        )


def print_overview(overview: dict[str, Any]) -> None:
    print("-" * 78)
    print(
        f"跑了 {overview['reports']} 条 · 总成本 ${overview['totalCostUsd']:.4f} · "
        f"档位 {','.join(overview['modes']) or '—'}"
    )
    if overview["degraded"]:
        print(f"\n降级过 {len(overview['degraded'])} 条（指标好看也要看这个）：")
        for name, blocks in overview["degraded"].items():
            print(f"  {name}：{len(blocks)} 处")
            for block in blocks[:2]:
                print(f"    · {block}")
    if overview["reworked"]:
        print(f"\n返工有改善：{', '.join(overview['reworked'])}")
    if overview["qualityGateFailed"]:
        print(f"\n报告自带的质检门没过：{', '.join(overview['qualityGateFailed'])}")


# ============================================================
# 主流程
# ============================================================


def _isolate_db() -> Path:
    """把 DB_PATH 指到一个临时文件。**必须在构造 Settings 之前调用**——
    `get_settings()` 是带缓存的，晚一步，Settings 就会把 `.env` 里的真库路径读进来，
    而评测的 22 份报告会静默写进「我的调研」。"""
    tmp_dir = Path(tempfile.mkdtemp(prefix="xm3-eval-"))
    db_path = tmp_dir / "eval.db"
    os.environ["DB_PATH"] = str(db_path)
    return tmp_dir


def _select(golden: list[Any], args: argparse.Namespace) -> list[Any]:
    selected = golden
    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        unknown = wanted - {g.name for g in golden}
        if unknown:
            raise SystemExit(f"黄金集里没有这些条目：{sorted(unknown)}")
        selected = [g for g in selected if g.name in wanted]
    if args.limit:
        selected = selected[: args.limit]
    if not selected:
        raise SystemExit("没有选中任何 query（检查 --only / --limit）")
    return selected


async def run_gate(args: argparse.Namespace) -> int:
    from eval.metrics import MissingMetrics, overall, read_report
    from scripts.record_cassettes import DEFAULT_GOLDEN, load_golden

    golden = _select(load_golden(Path(args.golden) if args.golden else DEFAULT_GOLDEN), args)
    thresholds = load_thresholds(Path(args.thresholds) if args.thresholds else DEFAULT_THRESHOLDS)

    where = {"gate": "闸门", "replay": "回放", "live": "真跑"}[args.kind]
    providers = _runtime_providers()
    label = "/".join(dict.fromkeys(providers.values()))  # 三家同名时只印一次
    if args.kind == "replay":
        # 回放的 provider 名字和真跑时长得一样，但一个是"发请求"、
        # 一个是"查表"，所以这里必须写出区别——不写的话，表头
        # "provider=deepseek" 会让人以为这次真的联网了。
        print(f"{where}：{len(golden)} 条 · 真实适配器 {label} + cassette 回放 · 临时库")
    else:
        print(f"{where}：{len(golden)} 条 · provider={label} · 临时库")
    print("=" * 78)

    reports = []
    bodies: list[tuple[str, dict[str, Any]]] = []
    failures: list[str] = []
    unrecorded: list[str] = []
    started = time.monotonic()
    total = len(golden) * max(1, args.repeat)
    step = 0
    for g in golden:
        for rep in range(max(1, args.repeat)):
            step += 1
            t0 = time.monotonic()
            label = g.label if args.repeat <= 1 else f"{g.label} #{rep + 1}"
            try:
                body = await run_once(g, mode=args.mode)
            except Exception as exc:  # 一条跑挂不该让整批没有结果
                # 回放模式下"没录过这条"是**预期状态**，不是失败：黄金集 22 条里
                # 只录了几条是完全正常的。把它算成失败，会让 `--replay` 永远红，
                # 然后所有人都不看它了。所以单独数一列，最后明说跑到了几条。
                #
                # 判据是**类型**（`HarnessError`），不是 `type(exc).__name__ ==
                # "CassetteMiss"`。后者曾经写在这里，而它**从来没有成立过**：
                # 流水线把异常转成了 `RunResult.error` 字符串再抛出来，
                # 到这儿已经是 `RuntimeError`，于是这个分支是死代码，
                # `--replay` 一直是"17 条失败"而不是"17 条没录制"。
                # 一个从没被走到过的分支，和一个没写的分支一样，都是零信息。
                if args.kind == "replay" and isinstance(exc, _harness_error()):
                    unrecorded.append(g.name)
                    print(f"[{step:>2}/{total}] {label:<34} 没录制，跳过")
                    break  # 这条没录制，重跑几次都一样
                failures.append(f"{g.name}：跑不出来——{exc}")
                print(f"[{step:>2}/{total}] {label:<34} 失败：{exc}")
                continue
            try:
                metrics = read_report(g.name, body)
            except MissingMetrics as exc:
                failures.append(str(exc))
                print(f"[{step:>2}/{total}] {label:<34} 没有指标")
                continue
            reports.append(metrics)
            bodies.append((g.name, body))
            print(
                f"[{step:>2}/{total}] {label:<34} "
                f"覆盖 {metrics.get('dimensionCoverage'):.2f} · "
                f"证据 {metrics.get('evidences'):.0f} · "
                f"信源 {metrics.get('independentDomains'):.0f} · "
                f"${metrics.get('totalCostUsd'):.4f} · "
                f"{time.monotonic() - t0:.1f}s"
            )

    if unrecorded:
        print(
            f"\n{len(unrecorded)} 条没有录制物（{', '.join(unrecorded[:4])}"
            f"{'…' if len(unrecorded) > 4 else ''}）。"
            f"录制：python -m scripts.record_cassettes --only {unrecorded[0]} --yes"
        )

    if not reports:
        print("\n一条都没跑成。")
        return 2

    aggregate = aggregate_of(reports)
    print()
    print_table(aggregate, thresholds)
    print_overview(overall(reports))

    judge_results = run_judges(bodies) if args.judge else None

    failures += check_gate(aggregate, thresholds, ran=len(reports))

    if args.baseline:
        baseline_path = Path(args.baseline)
        if baseline_path.exists():
            baseline = json.loads(baseline_path.read_text("utf-8"))
            drift = diff_baseline(aggregate, baseline, tolerance=args.tolerance)
            print("-" * 78)
            # 排除项要**说出来**。不说的话，"durationMs 从来不漂移"
            # 会被读成"耗时一直很稳"——而事实是它抖得最厉害，只是没人比它。
            print(f"（{'、'.join(sorted(DRIFT_EXCLUDED))} 不参与漂移比对，"
                  f"理由见 DRIFT_EXCLUDED）")
            if drift:
                print(f"与基线相比变差（容忍 {args.tolerance:.0%}）：")
                for line in drift:
                    print(f"  · {line}")
            else:
                print(f"与基线相比没有变差（容忍 {args.tolerance:.0%}）。")
        else:
            print(f"\n（基线文件不存在，跳过漂移比对：{baseline_path}）")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(
                {
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    # `provider` 曾经写的是 `args.provider`——那是**命令行缺省**，
                    # 在 `--replay` 下与事实不符（回放走的是真适配器 + 录制，
                    # 表头却印 mock）。这份 JSON 是基线文件，会被当成
                    # "上次是什么条件下测的"存档，说错条件比不说更糟。
                    "kind": args.kind,
                    "requestedProvider": args.provider,
                    "runtimeProviders": providers,
                    "cassetteMode": os.environ.get("CASSETTE_MODE", "off"),
                    "wallSeconds": round(time.monotonic() - started, 1),
                    "ran": len(reports),
                    "repeat": max(1, args.repeat),
                    "aggregate": aggregate,
                    "overview": overall(reports),
                    "perQuery": {r.name: r.values for r in reports},
                    "judges": judge_results or {},
                    "failures": failures,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n结果写到 {out_path}")

    print("=" * 78)
    if failures:
        print(f"闸门未通过，{len(failures)} 项：")
        for line in failures:
            print(f"  ✗ {line}")
        return 1
    print(f"闸门通过：{len(reports)} 条，{len(thresholds)} 项契约全绿。")
    return 0


def run_judges(bodies: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """跑 LLM-as-judge。**只由 `--judge` 触发，且只配 `--live`。**

    为什么判官不能进闸门，见 `eval/judges/__init__.py`：它们有采样噪声，
    会让闸门随机变红，而随机红的闸门很快会被加 `continue-on-error`。

    判官结果**一律不参与退出码**：它进 `--out` 的 JSON，供写文档时引用，
    但不影响 CI 的绿红。这条边界要写死在代码里，否则"就这一次"
    接进闸门之后，它就会一直在里面。
    """
    from eval.judges import faithfulness as fa
    from eval.judges import rubric as rb

    print("-" * 78)
    print(f"判官指标（{len(bodies)} 份 · core 档 · 温度 0.0 · 不参与闸门）")

    faithful: list[Any] = []
    rubrics: list[Any] = []
    for name, body in bodies:
        try:
            f = fa.judge_report(body, name=name)
            r = rb.judge_report(body, name=name)
        except Exception as exc:  # 判官挂了不该让整批的指标白跑
            print(f"  {name}：判官失败——{exc}")
            continue
        faithful.append(f)
        rubrics.append(r)
        # 「判官没解析出来」**不能显示成「这份报告得了 0 分」**——那是两件事，
        # 而后者会被读成一句质量结论。`RubricResult.mean` 在没有 scores 时
        # 返回 0.0（那个默认值本身是对的，`没有就是没有`），
        # 但把 0.0 直接印成 `可用性 0.00/5` 就是把"没测到"说成了"很差"。
        rubric_text = (
            f"可用性 {r.mean:.2f}/5" if r.scores else "可用性 —（判官输出没解析出来）"
        )
        print(
            f"  {name:<32} 忠实度 {f.score:.2f}"
            f"（判 {f.judged}/{f.total_claims} 条）"
            f" · {rubric_text}"
        )

    if not faithful:
        return {}

    # **只对"判官真的答了"的那些求均值。**
    # 把解析失败的 0 分一起平均，得到的数既不是质量（它偏低了），
    # 也不是失败率（它没说是失败）——它是一个不存在的量，
    # 唯一的作用是让读到它的人以为体系质量是 2 分。
    ok_faithful = [f for f in faithful if f.judged]
    scores = [f.score for f in ok_faithful]
    parsed = [r for r in rubrics if r.scores]
    means = [r.mean for r in parsed]
    per_dim: dict[str, list[int]] = {}
    for r in parsed:
        for s in r.scores:
            per_dim.setdefault(s.dimension, []).append(s.score)

    print()
    print(
        f"  引用忠实度：均值 {_mean(scores):.3f} · "
        f"最低 {min(scores):.3f} · 离散 {_stdev(scores):.3f}"
        f"  （样本 {len(scores)} 份报告）"
    )
    if means:
        print(
            f"  可用性均分：{_mean(means):.2f}/5 · 离散 {_stdev(means):.2f}"
            f"  （解析成功 {len(parsed)}/{len(rubrics)} 份）"
        )
    for dim, series in sorted(per_dim.items()):
        print(f"    {dim:<18}{_mean(series):.2f}/5  （n={len(series)}）")
    repaired = [r for r in rubrics if r.repaired]
    retried = [r for r in rubrics if r.attempts > 1]
    if repaired or retried:
        print(
            f"  判官输出要修才读得出来：补括号 {len(repaired)}/{len(rubrics)} 份"
            f" · 重问 {len(retried)}/{len(rubrics)} 份"
        )
        print(
            "    这两项**不是**质量问题，是判官这个测量仪器自身的可靠性。"
            "它们是补出来的分与第二次才有的分，和一次答对的分数不该混为一谈。"
        )
    for label, answered, asked in (
        ("引用忠实度", len(ok_faithful), len(faithful)),
        ("可用性 rubric", len(parsed), len(rubrics)),
    ):
        if answered < asked:
            print(
                f"  **{label}有 {asked - answered}/{asked} 份判官输出没解析出来**——"
                "它们**不参与**上面的均值。解析失败不等于 0 分。"
                "没解析出来的原文（含 finish_reason、字数、括号计数）在 JSON 的 "
                "`unparsed` 里，失败率本身是要报的数。"
            )
    print(
        "  注意：判官有采样噪声，上表的离散度就是它有多不稳。"
        "写进文档时必须带上样本量、温度、裁判模型与 repeat 次数。"
    )

    return {
        "faithfulness": {
            "mean": round(_mean(scores), 4),
            "min": round(min(scores), 4),
            "stdev": round(_stdev(scores), 4),
            "reports": len(scores),
            # 判了几份 / 一共几份。只报前者的话，"判官三成没解析出来"
            # 在 JSON 里看不出来，而它是这张表唯一的可靠性说明。
            "asked": len(faithful),
            "detail": [f.as_dict() for f in faithful],
        },
        "rubric": {
            "mean": round(_mean(means), 3),
            "stdev": round(_stdev(means), 3),
            "byDimension": {
                k: round(_mean(v), 3) for k, v in sorted(per_dim.items())
            },
            "parsed": len(parsed),
            "asked": len(rubrics),
            # 判官这个仪器本身的可靠性，与报告质量是两件事，所以分开记：
            # `repairedBraces` 是补了括号才读出来的份数，`attempts>1` 是重问过的。
            "repairedBraces": len(repaired),
            "retried": len(retried),
            "detail": [r.as_dict() for r in rubrics],
        },
    }


def _mean(series: list[float]) -> float:
    return sum(series) / len(series) if series else 0.0


def _stdev(series: list[float]) -> float:
    import statistics

    return statistics.stdev(series) if len(series) > 1 else 0.0


def aggregate_of(reports: list[Any]) -> dict[str, dict[str, float]]:
    from eval.metrics import aggregate

    return aggregate(reports)


async def run_from_db(args: argparse.Namespace) -> int:
    """直接读库里已有的报告，不重跑流水线。

    **它不构成闸门**：库里有什么就跑什么，所以"全绿"可能只是因为
    库里只有两份报告。存在价值是把当前存量报告的质量印出来看看。
    """
    from app.core.config import get_settings
    from app.db.repo import reports as reports_repo
    from eval.metrics import MissingMetrics, overall, read_report

    settings = get_settings()
    print(f"读库：{settings.db_file}")
    rows = reports_repo.list_recent(limit=args.limit or 50)
    if not rows:
        print("库里没有报告。")
        return 2

    reports = []
    for row in rows:
        try:
            reports.append(read_report(row.report_id, row.data))
        except MissingMetrics as exc:
            print(f"跳过 {row.report_id}：{exc}")

    if not reports:
        print("没有一份报告带指标。")
        return 2

    thresholds = load_thresholds(
        Path(args.thresholds) if args.thresholds else DEFAULT_THRESHOLDS
    )
    aggregate = aggregate_of(reports)
    print()
    print_table(aggregate, thresholds)
    print_overview(overall(reports))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="黄金集评测",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--gate",
        action="store_true",
        help="黄金集 × mock provider，零成本，按阈值判定（CI 用这个）",
    )
    mode_group.add_argument(
        "--replay", action="store_true", help="cassette 回放，不联网（未实现时请用 --gate）"
    )
    mode_group.add_argument(
        "--live", action="store_true", help="真实 provider，会花钱"
    )
    parser.add_argument(
        "--from-db", action="store_true", help="读库里已有的报告，不重跑"
    )
    parser.add_argument("--only", default="", help="只跑这几条，逗号分隔")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条")
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="每条跑几遍（判官指标有采样噪声，写文档时用 3；确定性指标 1 遍就是定论）",
    )
    parser.add_argument(
        "--judge",
        action="store_true",
        help="跑 LLM-as-judge（引用忠实度 + 可用性 rubric）。花钱，且**不参与闸门**",
    )
    parser.add_argument("--mode", default="", help="覆盖黄金集里的档位")
    parser.add_argument(
        "--provider",
        default="",
        help="provider 快捷方式（mock / deepseek / …）。缺省：--gate 用 mock，--live 用 deepseek",
    )
    parser.add_argument("--golden", default="", help="黄金集路径")
    parser.add_argument("--thresholds", default="", help="阈值路径")
    parser.add_argument("--baseline", default="", help="基线 JSON，比漂移用")
    parser.add_argument(
        "--tolerance", type=float, default=0.15, help="漂移容忍度，默认 0.15"
    )
    parser.add_argument("--out", default="", help="把结果写成 JSON")
    args = parser.parse_args(argv)

    _force_utf8()

    if args.from_db:
        return asyncio.run(run_from_db(args))

    if not (args.gate or args.replay or args.live):
        parser.error("必须指定 --gate / --replay / --live / --from-db 之一")

    if args.gate or args.replay:
        args.kind = "replay" if args.replay else "gate"
    else:
        args.kind = "live"
    if not args.provider:
        # 缺省值随 kind 变，而不是写死一个：`--live` 的缺省若还是 mock，
        # 那条命令会**静默地零成本跑完**并打印一份看起来很像真的质量报告。
        args.provider = "deepseek" if args.kind == "live" else "mock"
    if args.kind == "gate" and args.provider != "mock":
        parser.error("--gate 只能用 mock：闸门要每次都跑同样的东西，接真实 provider 就不是闸门了")
    if args.judge and args.kind == "gate":
        # 判 mock 的输出等于让裁判读一份模板——它会给高分，
        # 而那个高分和这套系统好不好毫无关系。要判就判真实产出。
        parser.error("--judge 不能配 --gate：judge 要读真实产出，mock 的产出是模板")
    if args.repeat < 1:
        parser.error("--repeat 至少是 1")

    # 在构造任何 Settings 之前定好 provider 与库路径。
    # `get_settings()` 是带缓存的：晚一步，Settings 就会把 `.env` 里的
    # 真 provider 和真库路径读进来，而这里的设置被静默忽略——
    # 表现是"我明明指定了 mock，它却去调真 API 了"，
    # 以及"评测的 22 份报告全灌进了我的调研"。
    tmp_dir = _isolate_db()
    if args.kind == "gate":
        _set_provider_env(args.provider)
    elif args.kind == "replay":
        # 回放不覆盖 provider：要走的正是**真实适配器**那条路径，
        # 由 cassette 那一层按 `CASSETTE_MODE` 决定是发请求还是查表。
        # 这里覆盖成 mock 的话，回放就变成了"用 mock 跑一遍"，
        # 什么都证明不了。
        os.environ["CASSETTE_MODE"] = "replay"
    else:
        _set_provider_env(args.provider)
        # 「真跑」必须真的联网。`.env` 里若留着 `CASSETTE_MODE=replay`，
        # 这次运行会一边查表一边把结果标成"真跑"——表头、写出的 JSON、
        # 最终进文档的数字会一起错，而且没有任何迹象。
        # 在"花了钱"这条路径上，宁可在这里显式钉死成 off。
        os.environ["CASSETTE_MODE"] = "off"

    try:
        return asyncio.run(run_gate(args))
    finally:
        _cleanup(tmp_dir)


#: `--provider` 的取值里，只有这一个代表"整条链路都用假实现"。
#: 其余取值（`deepseek` / `zhipu` / …）都是 **LLM** 的 provider 名。
_ALL_MOCK = "mock"


def _set_provider_env(provider: str) -> None:
    """定 LLM provider；只有 `mock` 时才把另外两家也定成同一个值。

    这里原来是不管三七二十一三家一起填的，理由是"只覆盖 LLM 的话，
    搜索还会去打真 API——评测号称零成本，实际在花钱"。**那个理由对
    `--gate` 完全成立**（它的值恒为 `mock`，而 mock 三家都有）。

    对 `--live` 却是错的：`--live` 的缺省值是 **`deepseek`**，
    而 deepseek 只是 LLM provider 的名字——搜索只有
    `bocha / mock / tavily` 三个。于是 `SEARCH_PROVIDER=deepseek`、
    `FETCH_PROVIDER=deepseek`，每一次运行都死在

        未注册的 搜索 provider：'deepseek'。可用：bocha, mock, tavily

    一条都跑不成。**也就是说 `--live` 从来没跑通过**——它不花钱，
    因为它连第一次网络请求都没发出去。

    修法不是加一家 `deepseek` 搜索适配器（那没有意义），
    而是把这个参数的语义写清楚：**它是 LLM 的快捷方式**，
    外加一个特例——`mock` 表示"整条链路都用假实现"。
    这样 `--live` 不再覆盖 search / fetch，让它们按 `.env` 来。

    表头不受影响：它读的是 `_runtime_providers()`，即 Settings 的现值，
    而不是这个参数——所以修好之后它印的是 llm=deepseek / search=bocha，
    这才是事实。
    """
    os.environ["LLM_PROVIDER"] = provider
    if provider == _ALL_MOCK:
        os.environ["SEARCH_PROVIDER"] = provider
        os.environ["FETCH_PROVIDER"] = provider


def _runtime_providers() -> dict[str, str]:
    """**这次运行真正用的是哪三家 provider**，从 Settings 现读。

    为什么不能直接打印 `args.provider`（这行原来是这么写的）：
    那个值是 argparse 的缺省，只在 `--gate` / `--live` 下才等于事实。
    `--replay` **刻意**不覆盖 provider（理由见 `main()`），实际走的是
    `.env` 里的真适配器 + cassette 那一层，于是表头会印出

        回放：22 条 · provider=mock

    而每一份报告都是 deepseek 的录制跑出来的。表头是这份结果**唯一的
    自述**——数字被人引用时，那句 mock 会跟着一起被引用。

    Settings 是缓存单例，且 `main()` 里所有 env 改写都发生在它构造之前，
    所以这里读到的一定是本次运行的值，而不是别的什么东西。
    """
    from app.core.config import get_settings

    settings = get_settings()
    return {
        "llm": settings.llm_provider,
        "search": settings.search_provider,
        "fetch": settings.fetch_provider,
    }


def _cleanup(tmp_dir: Path) -> None:
    """关连接再删目录。Windows 上文件被占用时删不掉，所以删不掉只报告不抛——
    留一个临时目录比让一次已经跑完的评测以崩溃收场要好。"""
    import shutil

    from app.db.connection import close_all

    with contextlib.suppress(Exception):
        close_all()
    with contextlib.suppress(Exception):
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
