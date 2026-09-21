"""流水线编排。

    intake → orchestrator → collect → analyze → audit →(rework→collect|analyze) → write → done

**唯一真相源是 `stages.py` 的 `PIPELINE_STAGES`**，这个文件的职责只是
按那个顺序把阶段函数串起来，并把返工循环接进去。顺序不在这里定义，
是为了让"文档、前端 DAG、实际执行"三处不可能出现分歧。

返工循环接在哪个位置
--------------------
`audit` 之后、`write` 之前。这是整个设计里最值钱的一个位置选择：
写作是最贵的一步（每章一次调用），把质检放在它前面，返工的代价是
"补采几条 + 重跑一次分析"；放在它后面，代价是"重写全部章节"。

参考项目的架构文档把顺序写反了（它写"分析 → 撰写 → 质检"），
但它代码里的实际顺序是对的。这里以代码为准，并让测试守住。
"""
from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field

from app.core.models import ReportRecord
from app.core.pipeline import analyze, assemble, audit, collect, dispatch, intake, write
from app.core.pipeline.audit import AuditResult
from app.core.pipeline.context import PipelineContext
from app.core.pipeline.stages import TERMINAL_STAGE
from app.providers.errors import HarnessError, is_retryable


@dataclass
class RunResult:
    """一次完整运行的产出。"""

    report: ReportRecord | None = None
    problems: list[str] = field(default_factory=list)
    audit: AuditResult | None = None
    rework_rounds: int = 0
    error: str = ""
    #: 这次失败是不是**夹具/评测基础设施**的问题（`HarnessError`）。
    #:
    #: `error` 是一个字符串，异常的类型到了这里已经没了——而"回放缺一条录制"
    #: 和"某个 provider 挂了"在字符串上长得一样（都是 `CassetteMiss: ...`
    #: 这样的前缀加一段话）。想要区分它们的人只能去字符串匹配类型名，
    #: 而那正是本项目在别处批评过的写法（见 `_is_rate_limit` 那一类）。
    #:
    #: 所以这里把那个信息**结构化地留下来**：谁需要区分，谁读这个布尔值，
    #: 判据仍然是 `HarnessError` 这一份定义。
    #:
    #: 为什么不是让异常直接穿透（`raise`）而只是记一个标记：生产里
    #: cassette 是关的，这个分支根本不会走到；而在回放/评测里，
    #: 调用方需要的是"跳过这一条并说清楚"，不是让整个进程带着栈崩掉。
    harness_error: bool = False
    #: 停在澄清处：需求理解跑完了，但信息不够，等用户作答。
    #:
    #: **既不是成功也不是失败**，所以不能塞进 `error`——塞进去的话
    #: 任务行会被标成 `failed`，而用户只要答完问题就能跑完。
    #: 也不能不表态：调用方必须能把"暂停"和"跑完了但没报告"分开。
    paused: bool = False

    @property
    def ok(self) -> bool:
        return self.report is not None and not self.error


async def run_pipeline(
    ctx: PipelineContext,
    *,
    intake_done: bool = False,
    pause_on_clarify: bool = False,
    on_intake_settled: Callable[[bool], None] | None = None,
) -> RunResult:
    """跑完整条流水线。**不抛异常**——失败会变成 `error` 字段与 `error` 事件。

    不抛的理由：这条函数被 CLI、HTTP 请求处理器、压测脚本三处调用，
    而它们对"失败"的处理各不相同（CLI 要退出码、HTTP 要 500、
    压测要记账）。在这里抛，等于让三处各写一遍 try/except，
    而其中一处迟早会写成 `except Exception: pass`。

    `intake_done=True` 表示需求理解那一步已经**单独跑过**了——
    交互式澄清的流程是"先跑 intake 把问题交给用户、等答完再继续"。
    跳过而不是重跑，有两个理由：一次模型调用是钱；而且同样的输入再算一遍
    未必逐字相同，于是"澄清页上显示的品牌"和"报告里分析的品牌"
    可能不是同一批——用户对着 A 方案的问题作答，跑的却是 B 方案。

    `pause_on_clarify=True` 时，需求理解发现信息不够就**停下来**，
    返回一个 `paused=True` 的结果而不是继续跑。这个判断放在这里而不是
    调用方，是为了让"流水线的启动"仍然只有一条路径：`intake.run` 在
    别处单独调用的话，它抛出的异常就绕过了下面那个 `except`，
    于是同一类错误在两条路径上有两种表现。

    `on_intake_settled(需要澄清吗)` 在需求理解有结论的那一刻被调用，
    **早于后面六个阶段**。HTTP 建任务靠它把控制权交回前端：
    没有这个回调，等待方只能等整条流水线跑完，而这在真实配置下是几分钟，
    前端在此期间只有一个转圈的按钮——工作台本该在这时候开始显示它在想什么。
    """
    try:
        audit_result = await _run(
            ctx,
            intake_done=intake_done,
            pause_on_clarify=pause_on_clarify,
            on_intake_settled=on_intake_settled,
        )
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，见 docstring
        ctx.error = f"{type(exc).__name__}: {exc}"
        ctx.emit("error", {
            "stage": ctx.current_stage,
            "message": str(exc),
            "kind": type(exc).__name__,
            "retryable": is_retryable(exc),
        })
        ctx.finish_stage(ctx.current_stage, status="error", detail={"error": str(exc)})
        return RunResult(
            audit=None,
            rework_rounds=ctx.rework_rounds,
            error=ctx.error,
            harness_error=isinstance(exc, HarnessError),
        )

    if audit_result is None:  # 停在澄清处
        return RunResult(rework_rounds=ctx.rework_rounds, paused=True)

    return RunResult(
        report=ctx.report,
        problems=list(ctx.problems),
        audit=audit_result,
        rework_rounds=ctx.rework_rounds,
    )


async def _run(
    ctx: PipelineContext,
    *,
    intake_done: bool = False,
    pause_on_clarify: bool = False,
    on_intake_settled: Callable[[bool], None] | None = None,
) -> AuditResult | None:
    """跑到头，或者停在澄清处。**返回 None 表示停住了，不是失败。**

    不用异常表示"暂停"：暂停是**正常路径**，而异常在 `run_pipeline`
    那里已经被定义成"失败"（会发 `error` 事件、把任务标成 failed）。
    一个用户答完问题就能跑完的任务，不该在日志里留下一条错误。
    """
    # ---- 1. 需求理解 ----
    # 已经跑过就跳过，理由见 `run_pipeline` 的 docstring。
    paused = False
    try:
        if not intake_done:
            await intake.run(ctx)
            # `clarify_answers` 非空就说明已经有答案了（人工答过，或
            # `auto_clarify` 刚按推荐项填空），没有再停下来问一次的道理。
            if pause_on_clarify and ctx.need_clarify and not ctx.clarify_answers:
                paused = True
                return None
    finally:
        # **这个回调的位置就是它的全部意义。** 放在整条流水线的结尾
        # （比如调用方的 `finally`）也能跑，但它等的是"跑完了"而不是
        # "问完了"——建任务的 HTTP 请求于是要等几分钟才响应。
        # 放在 `finally` 而不是成功分支上：需求理解**失败**时等待方也得醒，
        # 否则它会一直等一条永远不会到来的流水线。
        #
        # 传的是"要不要暂停"而不是"有没有出错"：等待方要据此决定
        # 把用户送去澄清页还是工作台。放在 `return None` **之前**发出去
        # 也不行——那一刻 `paused` 还是 False，等待方会看到一个
        # "不需要澄清"的任务，然后把用户送去工作台，而问题其实已经生成好了。
        if on_intake_settled is not None:
            on_intake_settled(paused)

    # ---- 2. 专家调度 ----
    await dispatch.run(ctx)
    # 三层分工的证据是"谁把什么交给了谁"，所以每次交接都留一条消息。
    # 只在返工那一条路上发消息的话，工作台的消息流在**顺利完成**的任务上
    # 永远是空的——而那是最常见的情况，看起来像这个功能没做。
    ctx.send(
        ctx.lead_expert,
        ctx.senior_expert,
        {
            "summary": (
                f"队伍已就位：{len(ctx.team.get('executors') or [])} 位执行专家、"
                f"{len(ctx.team.get('strategists') or [])} 位战略专家，"
                f"覆盖 {len(ctx.dimensions)} 个调研维度"
            ),
            "dimensions": list(ctx.dimensions),
        },
        kind="handoff",
    )

    # ---- 3. 首次采集 ----
    await collect.run(ctx)
    ctx.send(
        ctx.collector_expert,
        ctx.senior_expert,
        {
            "summary": (
                f"采集完成：{len(ctx.evidences)} 条证据，"
                f"搜索命中 {ctx.raw_hits} 条、通过相关性过滤 {ctx.filtered_hits} 条"
            ),
            "evidenceCount": len(ctx.evidences),
        },
        kind="handoff",
    )

    # ---- 4. 分析研判 ----
    await analyze.run(ctx)

    # ---- 5. 质量审计 ----
    audit_result = await audit.run(ctx)

    # ---- 6. 返工循环 ----
    audit_result = await _rework_loop(ctx, audit_result)

    quality = audit_result.quality or {}
    ctx.send(
        ctx.reviewer_expert,
        ctx.senior_expert,
        {
            "summary": (
                f"质检结论：{'通过' if quality.get('passed') else '未通过'}，"
                f"blocker {quality.get('blockers', 0)} / "
                f"major {quality.get('majors', 0)} / "
                f"minor {quality.get('minors', 0)}"
            ),
            "issues": [issue.to_dict() for issue in ctx.issues],
        },
        kind="result",
    )

    # ---- 7. 撰写与装配 ----
    # 两步分开：`write` 逐章生成正文并把正文里的引用解析出来，
    # `assemble` 把它们拼成报告正文并落库。合成一步的话，
    # "写作失败"和"装配失败"会变成同一个错误，而它们的处理方式完全不同。
    await write.run(ctx)
    ctx.report, ctx.problems = assemble.run(ctx, audit_result)

    ctx.current_stage = TERMINAL_STAGE
    body = ctx.report.data if ctx.report is not None else {}
    ctx.emit("done", {
        # 不带 `taskId`：信封保留键，journal 会填。
        "reportId": ctx.report.report_id,
        # 整份指标随终态一起推。工作台在这一刻要渲染成本面板与四条铁律的
        # 数字，让它为此再发一次请求，就是在已经拿到数据的那一刻多花一个往返。
        # 深拷贝而不是共享引用：这一份要进事件日志（可能被重放、被落库），
        # 而那一份是报告正文的一部分——共享的话，将来任何一处对指标的
        # 就地补写都会悄悄改掉另一处。
        "metrics": copy.deepcopy(body.get("metrics") or {}),
        "degraded": list(ctx.degraded_blocks),
        "problems": list(ctx.problems),
    })
    return audit_result


async def _rework_loop(ctx: PipelineContext, audit_result: AuditResult) -> AuditResult:
    """返工直到没有问题、或轮次用尽。

    每轮都会**重新审计**，而不是复用第一轮的问题清单：补采之后
    原来的问题可能已经解决，也可能暴露出新的（比如某个维度补到了
    证据但都是同一个域名）。复用旧清单会让第二轮对着已经解决的问题
    继续补采，而新问题一个都没碰。
    """
    while audit_result.rework_targets and ctx.rework_rounds < ctx.mode.max_rework_rounds:
        ctx.rework_rounds += 1
        before = len(ctx.evidences)
        targets = audit_result.rework_targets

        # 报的是**返工池**的余额。写首轮池的余额会几乎永远是 0
        # （首轮按预算截断，跑完就见底），于是每一轮返工都显示
        # "预算剩余 0 次"，而它其实马上要去搜——一条自相矛盾的状态。
        ctx.thought(
            ctx.lead_expert,
            f"第 {ctx.rework_rounds} 轮返工：{audit_result.reason}。"
            f"返工预算剩余 "
            f"{max(0, ctx.mode.rework_search_calls - ctx.rework_search_calls_made)} 次检索。",
            stage="rework",
        )
        ctx.send(
            ctx.reviewer_expert,
            ctx.collector_expert,
            {"summary": audit_result.reason, "targets": targets},
            kind="request",
        )

        await collect.run(ctx, targets=targets, rework=True)
        added = len(ctx.evidences) - before

        if added <= 0:
            # 补采一无所获。再重跑分析不会有新结论，只会再花一次钱，
            # 所以直接停——但**要说清楚是停了**，而不是悄悄退出循环。
            ctx.thought(
                ctx.collector_expert,
                "本轮补采没有新增任何证据，继续返工不会有新结论，停止返工。",
                stage="rework",
            )
            ctx.degrade("返工补采", "补采未新增证据，返工提前结束")
            ctx.rework_log.append({
                "round": ctx.rework_rounds,
                "targets": len(targets),
                "added": 0,
                "stopped": "no-new-evidence",
            })
            break

        # 有新证据才值得重跑分析。
        await analyze.run(ctx)
        audit_result = await audit.run(ctx)

        ctx.rework_log.append({
            "round": ctx.rework_rounds,
            "targets": len(targets),
            "added": added,
            "reason": audit_result.reason,
            "qualityPassed": bool(audit_result.quality.get("passed")),
        })

        ctx.thought(
            ctx.lead_expert,
            f"第 {ctx.rework_rounds} 轮返工完成：新增 {added} 条证据，"
            f"覆盖率 {audit_result.quality.get('coverage', 0):.0%}，"
            + ("质量门通过。" if audit_result.quality.get("passed") else "仍有未解决的问题。"),
            stage="rework",
        )

    if ctx.rework_rounds >= ctx.mode.max_rework_rounds and audit_result.rework_targets:
        ctx.degrade(
            "返工补采",
            f"已达返工上限 {ctx.mode.max_rework_rounds} 轮，仍有 {len(audit_result.rework_targets)} 项待补",
        )

    return audit_result
