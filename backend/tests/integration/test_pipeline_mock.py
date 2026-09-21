"""阶段 2 的验收：整条流水线对 Mock 跑通，并产出一份合规报告。

**这个文件不花一分钱**，所以它可以进 CI。走的是与 HTTP 接口完全相同的那条
路径（`ensure_runner` → 流水线 → 事件流 → 落库），区别只在 provider 实现。

一条断言背后是一条主张
--------------------
- 报告通过 `validate_report`：装配的键名是一份对外契约，前端、导出、
  评测脚本都读它。
- 每一章都带引用：这是铁律 1（无证据不立论）在正文层面的落地。
- 每个引用都指向真实存在的证据：铁律 2 的幻觉引用率必须为 0，
  而"正文里的角标点开是空的"是这条铁律唯一会让读者当场发现的失败。
- 事件 seq 连续且从 1 开始：前端的去重逻辑是 `seq <= lastSeq`，
  跳号与丢事件在它眼里无法区分。
- 同一个 task_id 第二次 `ensure_runner` 不重跑：这是整个 runner 模块
  存在的理由。
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.models import TaskRecord
from app.core.modes import get_mode
from app.core.observability.events import RESERVED_KEYS, journal_for
from app.core.pipeline import stages
from app.core.pipeline.runner import (
    TERMINAL_STATUSES,
    drop_runner,
    ensure_runner,
    load_task,
    new_task_id,
)
from app.db.repo import evidences as evidences_repo
from app.db.repo import reports as reports_repo
from app.db.repo import tasks as tasks_repo
from app.providers import registry
from scripts.run_pipeline_cli import parse_sse_frame
from tests.support import DEFAULT_MODE, DEFAULT_QUERY, Outcome

QUERY = DEFAULT_QUERY
MODE = DEFAULT_MODE


async def _drain(runner) -> list[tuple[str, dict]]:
    """消费完一条 SSE 流。`run_mock_pipeline` 用的是同一段逻辑，
    这里只用于"恢复出来的 runner"。"""
    return [parse_sse_frame(frame) async for frame in runner.stream(0)]


@pytest.fixture
async def outcome(run_mock_pipeline) -> Outcome:
    return await run_mock_pipeline()


# ============================================================
# 端到端
# ============================================================


async def test_run_completes_without_error(outcome: Outcome) -> None:
    assert outcome.result.error == ""
    assert outcome.result.ok is True
    assert outcome.runner.state.status == "done"


async def test_report_passes_the_assembled_schema(outcome: Outcome) -> None:
    """`problems` 为空即 `validate_report` 没有发现缺字段。

    装配处调一次校验，所有下游（导出、CLI、邮件推送）免费获得保证——
    每个出口各调一次的话，迟早有一个不调。
    """
    assert outcome.result.problems == []


async def test_report_carries_every_top_level_key(outcome: Outcome) -> None:
    body = outcome.body
    assert {
        "version", "taskId", "query", "brands", "mode", "generatedAt",
        "sections", "claims", "evidences", "charts", "matrix", "marketShare",
        "fiveForces", "trends", "featureTrees", "pricingModels", "personaSets",
        "sentiment", "team", "messages", "thoughts", "coercion", "degraded",
        "glossary", "evidenceStats", "audit", "metrics", "completeness",
        "quality", "gallery",
    } <= set(body)
    assert body["taskId"] == outcome.task_id
    assert body["mode"]["key"] == MODE


async def test_every_mode_section_is_written(outcome: Outcome) -> None:
    assert [section["key"] for section in outcome.body["sections"]] == list(
        get_mode(MODE).sections
    )


# ============================================================
# 铁律 1 / 2：无证据不立论、引用必须真实
# ============================================================


async def test_every_section_cites_at_least_one_evidence(outcome: Outcome) -> None:
    """有正文但一个引用都没有的章节是"裸写"的，必须被标成降级。

    这里断言的是"没有这种章节"——Mock 的正文模板刻意带真实角标，
    所以正文里的引用解析确实被执行过。模板不带角标的话，`resolve_citations`
    在 mock 下永远不会跑，而"引用解析是对的"就没有任何证据。
    """
    sections = outcome.body["sections"]
    assert sections
    for section in sections:
        assert section["evidenceIds"], f"{section['key']} 没有引用任何证据"
        assert section["degraded"] is False, f"{section['key']} 被标记为降级"


async def test_no_citation_points_at_a_nonexistent_evidence(outcome: Outcome) -> None:
    """铁律 2 在正文层面的唯一可见失败：角标点开是空的。

    幻觉率是 0 只说明"没有不存在的 id 被保留"，这条更进一步：
    把每个 id 拿去证据集合里查一遍。
    """
    known = {ev["evidenceId"] for ev in outcome.body["evidences"]}
    cited = {eid for section in outcome.body["sections"] for eid in section["evidenceIds"]}

    assert cited <= known
    assert outcome.body["metrics"]["hallucinationRate"] == 0.0


async def test_claims_carry_citations_and_a_confidence(outcome: Outcome) -> None:
    claims = outcome.body["claims"]
    assert claims
    for claim in claims:
        assert claim["evidenceIds"], claim["text"]
        assert claim["confidence"] in {"high", "medium", "low"}


# ============================================================
# 事件流契约
# ============================================================


async def test_event_seq_is_contiguous_from_one(outcome: Outcome) -> None:
    """前端靠 `seq <= lastSeq` 去重。跳号与丢事件在它眼里无法区分，
    所以服务端的序号必须是连续的——去重的正确性依赖这条不变量。"""
    seqs = [data["seq"] for _, data in outcome.events]
    assert seqs == list(range(1, len(seqs) + 1))


async def test_every_event_carries_the_envelope(outcome: Outcome) -> None:
    """信封字段摊平在每个事件上，前端因此能写出真正按 `type` 判别的联合类型。

    保留键在 `publish()` 里就拦住了载荷的占用，所以这里能直接断言
    "四个信封键一个不少"——它们是 journal 填的，不是埋点填的。
    """
    for type_, data in outcome.events:
        assert set(data) >= RESERVED_KEYS
        assert data["type"] == type_
        assert data["taskId"] == outcome.task_id
        assert data["createdAt"]


async def test_the_stream_ends_when_the_task_ends(outcome: Outcome) -> None:
    """`stream()` 必须正常结束，不能挂着。

    挂着的话浏览器看到的是一条**永远不结束的流**：不报错、不再有内容，
    前端于是既不显示"已完成"也不重连。
    """
    assert journal_for(outcome.task_id).closed is True
    assert outcome.types[-1] == "done"


async def test_the_expected_event_types_all_appear(outcome: Outcome) -> None:
    assert {
        "node_update", "thought", "evidence", "trace",
        "progress", "report_ready", "done",
    } <= set(outcome.types)
    assert "error" not in outcome.types


async def test_charts_are_streamed_before_the_report_lands(outcome: Outcome) -> None:
    """图表随流推，而不是等报告落库之后由前端拉一次全量。

    `chart` 曾经是一个**声明了但没有任何地方发**的事件类型：前端为它写了一个
    永远走不到的 `case`，而"图表能不能画"这件事没有任何测试碰得到。
    这里连同"每张图都带 evidenceIds"（铁律 1：图表也是论点）一起断言。
    """
    charts = outcome.payloads("chart")

    assert charts, "没有推任何图表事件"
    assert len(charts) == len(outcome.body["charts"])
    for payload in charts:
        chart = payload["chart"]
        assert chart["chartId"] and chart["kind"] and chart["title"]
        assert chart["evidenceIds"], f"{chart['chartId']} 没有证据链"
        assert "spec" in chart

    # 顺序：图表在 done 之前。它是"过程可见"的一部分，晚于 done 就没有意义了。
    assert outcome.types.index("chart") < outcome.types.index("done")


async def test_report_ready_announces_the_report_without_a_second_fetch(
    outcome: Outcome,
) -> None:
    """报告落库那一刻，工作台需要的东西要一次给全。

    少了 `query`/`subject`，前端就只能显示"报告好了"而不说是哪一份；
    少了 `sectionCount`/`evidenceCount`，横幅上就得写"若干章、若干条证据"。
    """
    payload = outcome.payload("report_ready")

    assert payload["reportId"] == outcome.result.report.report_id
    assert payload["query"] == QUERY
    assert payload["subject"] == outcome.body["subject"]
    assert payload["sectionCount"] == len(outcome.body["sections"])
    assert payload["evidenceCount"] == len(outcome.body["evidences"])
    assert payload["problems"] == outcome.result.problems


async def test_done_carries_the_metrics_so_the_workspace_need_not_refetch(
    outcome: Outcome,
) -> None:
    """终态事件带整份指标：工作台在这一刻要渲染成本面板与四条铁律的数字，
    让它为此再发一次请求，就是在已经拿到数据的那一刻多花一个往返。

    顺带断言它是**拷贝**：事件日志与报告正文共享同一个 dict 的话，
    将来任何一处对指标的就地补写都会悄悄改掉另一处。
    """
    payload = outcome.payload("done")

    assert payload["reportId"] == outcome.result.report.report_id
    assert payload["metrics"] == outcome.body["metrics"]
    assert payload["metrics"] is not outcome.body["metrics"]
    assert payload["problems"] == outcome.result.problems
    assert payload["degraded"] == outcome.body["degraded"]


# ============================================================
# 降级必须原样进报告
# ============================================================


#: 让这条流水线真的产生一条降级。含糊的需求会触发澄清，
#: `auto_clarify=True` 于是"自动采用了推荐选项（未人工确认）"——
#: 这是报告读者有权知道的一条假设。
DEGRADING_QUERY = "帮我看看那个笔记软件"


async def test_the_report_carries_every_degradation_the_run_recorded(
    mock_pipeline_db, run_mock_pipeline
) -> None:
    """报告的降级清单必须**等于**运行期间登记的全部降级，不是它的子集。

    为什么这条要用一个必然降级的需求来跑
    ----------------------------------
    干净跑完一条流水线时 `ctx.degraded_blocks` 和 `body["degraded"]` **都是空的**，
    于是 `==` 在装配把整份清单丢掉的情况下同样成立。实测过：
    默认需求跑完两边都是 `[]`。所以这个用例先确认清单非空，再比对——
    否则它就是一条"缺陷不可能出现"的测试。

    守的是「降级率」这个指标的可信度。降级被悄悄丢掉的话，
    报告会显得比实际运行更干净，而**静默降级正是最危险的那种失败**：
    读者无从知道某一块其实没做成。
    """
    outcome = await run_mock_pipeline(query=DEGRADING_QUERY)

    recorded = outcome.ctx.degraded_blocks
    assert recorded, "这条需求本该产生降级（自动采用推荐选项），夹具失效了"
    assert outcome.body["degraded"] == recorded, (
        f"报告里的降级与运行时登记的不是同一份：\n"
        f"  登记：{recorded}\n  报告：{outcome.body['degraded']}"
    )
    assert outcome.payload("done")["degraded"] == recorded


async def test_a_report_that_failed_its_own_validation_says_so(
    mock_pipeline_db, monkeypatch
) -> None:
    """**出库校验发现的问题，本身也必须是一条降级。**

    这是最该被读到的一条降级——"这份报告没通过自己的校验"。
    而它在代码里的位置很尴尬：`assemble()` 是在校验**之前**把
    `degraded` 抄进 body 的，所以校验之后才登记的这一条不在那份快照里。
    修法是在 `if problems:` 里补登记一次（`assemble.py` 里那段注释
    记着这件事），而在写这个用例之前，**那个补登记没有任何测试**。

    所以这里把校验结果换掉，逼出那条分支。校验器本身有自己的测试，
    这里测的是"发现问题之后做了什么"，不是"能不能发现问题"。
    """
    from app.core.pipeline import assemble as assemble_module

    problems = ["sections[0] 缺少 evidenceIds", "claims[2] 引用了不存在的证据"]
    monkeypatch.setattr(assemble_module, "validate_report", lambda body: list(problems))

    task_id = new_task_id()
    tasks_repo.create(
        TaskRecord(task_id=task_id, query=DEGRADING_QUERY, mode=MODE, status="pending")
    )
    runner = ensure_runner(task_id, DEGRADING_QUERY, mode_key=MODE, auto_clarify=True)
    async for _ in runner.stream(0):
        pass
    result = await runner.wait()

    assert result.problems == problems, "校验结果没被带出来"
    assert result.report is not None, "校验不通过也要落库，不然整次运行的产物全丢了"

    degraded = result.report.data["degraded"]
    assert any("报告合规校验" in item for item in degraded), (
        f"报告没通过自己的校验，却没有把这件事登记成降级：{degraded}"
    )
    assert degraded == runner.context.degraded_blocks
    # 落库那份和推出去的终态事件读的是同一份清单。
    assert result.report.data["degraded"] == list(runner.context.degraded_blocks)


async def test_streamed_thoughts_match_the_persisted_ones(outcome: Outcome) -> None:
    """思维流既在事件里推、又整份存进报告。

    两处内容必须一致，否则"实时看到的"与"事后回看的"会不一样，
    而决策回放读的是报告里那一份。
    """
    streamed = [p["thought"]["id"] for p in outcome.payloads("thought")]
    stored = [t["id"] for t in outcome.body["thoughts"]]

    assert streamed == stored
    assert streamed


async def test_thoughts_name_their_expert_and_stage(outcome: Outcome) -> None:
    """思维流要按专家分组显示、按层级配色、跟 DAG 联动，这些都需要
    结构化字段。参考实现只发一段文本，前端只能靠正则去猜——
    猜错了就静默显示错。"""
    for payload in outcome.payloads("thought"):
        thought = payload["thought"]
        assert thought["expertId"]
        assert thought["expertName"] != thought["expertId"], "名册没查到这位专家"
        assert thought["stage"] in stages.STAGE_ORDER
        assert thought["text"]


async def test_each_node_reports_running_then_a_terminal_state(outcome: Outcome) -> None:
    """一个节点至少有"开始"和"结束"两条事件，DAG 才能画出过程而不只是结果。"""
    by_node: dict[str, list[str]] = {}
    for payload in outcome.payloads("node_update"):
        by_node.setdefault(payload["stage"], []).append(payload["status"])

    assert by_node[stages.STAGE_ORDER[0]][0] == "running"
    for node, statuses in by_node.items():
        assert statuses[0] == "running", node
        assert statuses[-1] in {"done", "degraded", "error"}, node


async def test_rework_node_stays_pending_when_nothing_triggers_it(outcome: Outcome) -> None:
    """可选阶段没跑到就保持 pending，不能显示成 done。

    显示成 done 会让工作台说"返工过了"，而报告里的返工轮次是 0——
    两个数字打架时，人只会相信那个看起来更努力的那个。
    """
    nodes_touched = {p["stage"] for p in outcome.payloads("node_update")}

    assert outcome.runner.state.nodes["rework"] == "pending"
    assert outcome.body["metrics"]["reworkRounds"] == 0
    assert "rework" not in nodes_touched
    assert set(stages.STAGE_ORDER) - {"rework"} <= nodes_touched


async def test_stage_progress_never_goes_backwards(outcome: Outcome) -> None:
    """事件从多个线程发布，落库顺序与发生顺序不一定一致（采集是并发扇出的）。
    直接用最新值会让进度条来回跳，而"倒退的进度条"会让人以为任务重启了。"""
    progress = [
        p["progress"] for p in outcome.payloads("node_update") if "progress" in p
    ]
    assert progress == sorted(progress)


# ============================================================
# 四条铁律的量化指标
# ============================================================


async def test_metrics_cover_all_four_iron_rules(outcome: Outcome) -> None:
    metrics = outcome.body["metrics"]
    assert {
        "unsupportedClaimRate",   # 铁律 1 无证据不立论
        "hallucinationRate",      # 铁律 2 引用可核验
        "crossValidationRate",    # 铁律 2 交叉验证
        "dimensionCoverage",      # 铁律 1 的维度版本
        "reworkRounds",           # 铁律 3 返工闭环
        "totalCostUsd", "totalTokens", "llmCalls", "searchCalls",  # 铁律 4 全程可观测
        "qualityGatePassed",
    } <= set(metrics)


async def test_quality_gate_reads_deterministic_evidence_only(outcome: Outcome) -> None:
    """质量门只读确定性证据，不读模型自评。

    模型的分数可以被提示词影响，而"覆盖率 40%"不能。
    报告里两个都给，但**结论只由后者决定**。
    """
    quality = outcome.body["quality"]
    assert quality["passed"] is True
    assert quality["blockers"] == 0
    assert quality["coverage"] == outcome.body["metrics"]["dimensionCoverage"]
    assert "thresholds" in quality


async def test_cross_validation_rate_is_computable(outcome: Outcome) -> None:
    """论点只有 3 条时这个比率容易变成恒等式，所以这里只断言它在合法区间、
    且分母与论点总数对得上——具体的数值判定留给评测集。"""
    metrics = outcome.body["metrics"]
    assert 0.0 <= metrics["crossValidationRate"] <= 1.0
    assert metrics["claims"] == len(outcome.body["claims"])
    assert metrics["verifiedClaims"] <= metrics["claims"]


async def test_traces_record_every_provider_call(outcome: Outcome) -> None:
    """铁律 4：每一次调用都要留下痕迹。

    trace 事件的条数必须与真实调用次数对得上，否则成本表会偏小——
    而偏小的成本表看起来只是"这个系统很省钱"。
    """
    trace_events = outcome.payloads("trace")
    provider_calls = (
        len(registry.get_llm().calls)
        + len(registry.get_search().calls)
        + len(registry.get_fetcher().calls)
    )
    assert len(trace_events) >= provider_calls
    assert all(
        {"spanId", "kind", "durationMs", "costUsd"} <= set(p["span"])
        for p in trace_events
    )


# ============================================================
# 落库
# ============================================================


async def test_report_and_evidences_are_persisted_and_linked(outcome: Outcome) -> None:
    report = reports_repo.get_by_task(outcome.task_id)
    assert report is not None
    assert report.report_id == outcome.result.report.report_id

    stats = evidences_repo.stats(outcome.task_id)
    assert stats["total"] == len(outcome.body["evidences"])
    assert report.quality["passed"] is True


async def test_task_row_reaches_a_terminal_status(outcome: Outcome) -> None:
    record = tasks_repo.get(outcome.task_id)
    assert record.status in TERMINAL_STATUSES
    assert record.stage == stages.TERMINAL_STAGE
    assert record.progress == 1.0


# ============================================================
# 注册表（这是整个模块存在的理由）
# ============================================================


async def test_second_ensure_runner_during_a_run_returns_the_same_runner(
    mock_pipeline_db,
) -> None:
    """并发订阅只跑一次流水线。

    参考实现的 `GET /api/tasks/{id}/stream` 在请求处理器里直接调
    `run_pipeline`，于是 `EventSource` 断线重连、或开第二个标签页，
    就会对同一个 task_id 再跑一遍。这里用 provider 调用计数断言——
    只断言"返回了同一个对象"是不够的，一个共享 runner 也可能被启动两次。
    """
    task_id = new_task_id()
    tasks_repo.create(
        TaskRecord(task_id=task_id, query=QUERY, mode=MODE, status="pending")
    )
    first = ensure_runner(task_id, QUERY, mode_key=MODE, auto_clarify=True)
    second = ensure_runner(task_id, QUERY, mode_key=MODE, auto_clarify=True)

    assert first is second

    async for _ in first.stream(0):
        pass
    await first.wait()

    # 跑完之后再调一次：内存里有就不该新建，更不该重跑。
    calls_after_one_run = len(registry.get_llm().calls)
    third = ensure_runner(task_id, QUERY, mode_key=MODE, auto_clarify=True)
    await asyncio.sleep(0)

    assert third is first
    assert len(registry.get_llm().calls) == calls_after_one_run


async def test_a_finished_task_is_recovered_instead_of_rerun(mock_pipeline_db, run_mock_pipeline) -> None:
    """**回归用例。** 跑完之后重开工作台，曾经会重跑整条流水线。

    这个漏法比参考实现那个更难发现：证据不会明显翻倍（evidence_id 由
    URL 摘要生成、跨轮次稳定，`INSERT OR REPLACE` 会去重掉），
    能看见的只有账单翻倍和报告 id 悄悄换了一个。
    """
    first = await run_mock_pipeline()
    calls_after_first = len(registry.get_llm().calls)

    # 模拟"进程重启之后又打开了这个任务"：内存里的 runner 没了，
    # 但库里的任务行还在。
    drop_runner(first.task_id)
    reopened = ensure_runner(first.task_id, QUERY, mode_key=MODE)

    assert reopened.finished is True
    assert reopened.state.status in TERMINAL_STATUSES
    assert len(registry.get_llm().calls) == calls_after_first

    # 只回放：历史事件一条不少地补发出来，然后正常结束。
    replayed = [type_ for type_, _ in await _drain(reopened)]
    assert replayed == first.types

    # 报告没有被覆盖。
    assert reopened.state.report_id == first.result.report.report_id


async def test_recovered_task_rebuilds_the_dag_from_events(mock_pipeline_db, run_mock_pipeline) -> None:
    """刷新页面后 DAG 要能回到"哪些阶段跑完了"。

    之前这里给的是空 dict，工作台于是显示"还没开始"，
    而事实是"已经跑完了但过程不在内存里"。事件流里本来就有全部
    `node_update`，重建是免费的。
    """
    first = await run_mock_pipeline()
    drop_runner(first.task_id)
    snapshot = load_task(first.task_id)

    assert snapshot["status"] in TERMINAL_STATUSES
    assert snapshot["nodes"]["collect"] == "done"
    assert snapshot["nodes"]["rework"] == "pending"
    assert snapshot["progress"] == 1.0
    assert snapshot["reportId"] == first.result.report.report_id


async def test_load_task_is_read_only_and_never_starts_the_pipeline(mock_pipeline_db) -> None:
    """`GET /api/tasks/{id}` 每次打开页面都会调。

    它顺手把流水线开起来的话，"打开一个页面"就等于"执行一次任务"。
    这里用"没有任何 provider 调用、也没有为它建 journal"来断言。
    """
    task_id = new_task_id()
    tasks_repo.create(
        TaskRecord(task_id=task_id, query=QUERY, mode=MODE, status="pending")
    )

    snapshot = load_task(task_id)
    await asyncio.sleep(0)

    assert snapshot["status"] == "pending"
    assert snapshot["nodes"] == {}
    assert len(registry.get_llm().calls) == 0
    assert journal_for(task_id) is None


async def test_load_task_of_an_unknown_id_returns_empty(mock_pipeline_db) -> None:
    """不存在的任务返回空 dict，不是抛异常。

    前端会在路由参数被手改之后调它，为此让接口 500 只会让页面白屏。
    """
    assert load_task("TK-0") == {}


async def test_running_two_queries_does_not_mix_their_evidence(mock_pipeline_db, run_mock_pipeline) -> None:
    """并发任务是常态（两个浏览器标签页）。`evidences` 的主键是
    `(task_id, evidence_id)` 而不是单列，正是为了让两条流水线的
    同名证据各自留存。"""
    first, second = await asyncio.gather(
        run_mock_pipeline(QUERY), run_mock_pipeline("对比 Figma 与 Sketch")
    )

    assert first.task_id != second.task_id
    assert first.body["taskId"] == first.task_id
    assert second.body["taskId"] == second.task_id

    ids_a = {ev["evidenceId"] for ev in first.body["evidences"]}
    ids_b = {ev["evidenceId"] for ev in second.body["evidences"]}
    assert evidences_repo.stats(first.task_id)["total"] == len(ids_a)
    assert evidences_repo.stats(second.task_id)["total"] == len(ids_b)
