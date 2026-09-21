"""返工闭环：一轮补采必须真的补得进来。

为什么这个文件必须存在
----------------------
`tests/unit/test_collect_budget.py` 守卫的是**算术**（两个池子分开）。
算术对了不等于闭环通了：从"审计判定要返工"到"新证据进了 ctx"中间还有
编排、计数、合并几步，任何一步断了，表现都是同一个——`返工提升 Δ` 为 0。
在写这个文件之前，**没有任何测试让返工真的跑起来过**：
mock 跑完整流水线时审计一次都不触发返工（三条档位都是
"没有达到返工门槛的问题"），所以整条返工路径从来没有被执行过。

这个夹具把返工**逼出来**，靠的是让首轮在一个维度上真的失败：
`MockSearchProvider.fail_queries` 让某条 query 抛 `Transient`
（等同于真实 provider 的 429/超时），那个维度于是没有任何证据，
审计给出 blocker、返工目标指向它。补采前把失败清掉，于是
"首轮失败 → 补采成功"这条路径走通了——这正是返工存在的理由。

`metrics_after > metrics_before` 就是铁律三。
"""
from __future__ import annotations

import pytest

from app.core.analysis.metrics import compute_metrics
from app.core.modes import get_mode
from app.core.observability.events import EventJournal
from app.core.observability.trace import Tracer
from app.core.pipeline import collect
from app.core.pipeline.audit import decide_rework, deterministic_audit
from app.core.pipeline.context import PipelineContext
from app.providers import registry

#: 首轮会失败的那个维度。用中文维度名，和真实计划一样。
BROKEN_DIMENSION = "定价"
HEALTHY_DIMENSION = "功能"
BRAND = "Notion"
#: 首轮里这个维度的查询文本。`build_queries` 拼的是 `品牌 维度`。
BROKEN_QUERY = f"{BRAND} {BROKEN_DIMENSION}"


def make_ctx(*, mode: str = "quick", search_calls_made: int | None = None) -> PipelineContext:
    """一个能真跑采集的上下文：三个 provider 都是 mock，零成本零网络。"""
    mode_config = get_mode(mode)
    ctx = PipelineContext(
        task_id="TK-rework",
        query=f"对比 {BRAND} 与竞品",
        mode=mode_config,
        tracer=Tracer("TK-rework"),
        journal=EventJournal("TK-rework"),
        llm=registry.get_llm(),
        search=registry.get_search(),
        fetcher=registry.get_fetcher(),
    )
    ctx.brands = [BRAND]
    ctx.dimensions = [HEALTHY_DIMENSION, BROKEN_DIMENSION]
    ctx.search_angles = []
    if search_calls_made is not None:
        ctx.search_calls_made = search_calls_made
    return ctx


async def _first_round_then_audit(ctx: PipelineContext):
    """跑首轮采集，然后审计。返回 `(审计结果, 返工目标)`。"""
    await collect.run(ctx)
    issues = deterministic_audit(ctx)
    targets, reason = decide_rework(ctx, issues)
    return issues, targets, reason


async def test_首轮失败的维度会被审计拎出来(mock_pipeline_db) -> None:
    """夹具本身要成立：不触发返工的话，下面每一条都测不到东西。"""
    ctx = make_ctx()
    ctx.search.fail_queries.add(BROKEN_QUERY)

    issues, targets, reason = await _first_round_then_audit(ctx)

    broken = [ev for ev in ctx.evidences if BROKEN_DIMENSION in ev.matched_dimensions]
    assert not broken, "这个维度本不该有证据，否则夹具没生效"
    assert any(
        issue.dimension == BROKEN_DIMENSION and issue.severity == "blocker"
        for issue in issues
    ), f"审计没有把缺失的维度标成 blocker：{[i.to_dict() for i in issues]}"
    assert targets, f"审计没有给出返工目标，这条测试就什么都没测：{reason}"
    assert any(t["dimension"] == BROKEN_DIMENSION for t in targets)


async def test_首轮池见底后返工仍然搜得出去(mock_pipeline_db) -> None:
    """**这就是 A2 的端到端守卫。**

    首轮池见底是常态（计划被 `planned[:budget]` 截断），所以
    "返工拿不到额度"不是边角情况，而是每一次返工都会撞上的墙——
    撞上之后的表现是"本轮补采没有新增证据"，一句关于搜索源的断言，
    而真相是这个程序一次请求都没发。
    """
    ctx = make_ctx()
    ctx.search.fail_queries.add(BROKEN_QUERY)
    _, targets, _ = await _first_round_then_audit(ctx)

    # 把首轮池花到见底，模拟"首轮的查询计划比预算长"这个常态。
    ctx.search_calls_made = ctx.mode.max_search_calls
    calls_before = len(ctx.search.calls)

    # 补采这轮修好了搜索源——返工应该拿到东西。
    ctx.search.fail_queries.clear()
    await collect.run(ctx, targets=targets, rework=True)

    assert ctx.rework_search_calls_made > 0, "返工一次检索都没发出去"
    assert len(ctx.search.calls) - calls_before == ctx.rework_search_calls_made
    # 返工的花销记在返工池上，同时**也**算进总调用数（它是子集）。
    assert ctx.search_calls_made == ctx.mode.max_search_calls + ctx.rework_search_calls_made


async def test_返工把缺的那个维度补上了(mock_pipeline_db) -> None:
    """补采之后那个维度要有证据，且指标的 `before/after` 确实变好。

    这是铁律三（返工必须可量化）在代码里的落地：`metrics_after >
    metrics_before` 不是一句设计意图，而是一条断言。
    """
    ctx = make_ctx()
    ctx.search.fail_queries.add(BROKEN_QUERY)
    _, targets, _ = await _first_round_then_audit(ctx)

    before = compute_metrics(ctx)
    evidence_before = len(ctx.evidences)

    ctx.search_calls_made = ctx.mode.max_search_calls
    ctx.search.fail_queries.clear()
    await collect.run(ctx, targets=targets, rework=True)

    after = compute_metrics(ctx)
    assert len(ctx.evidences) > evidence_before, "补采没有新增任何证据"
    assert any(
        BROKEN_DIMENSION in ev.matched_dimensions for ev in ctx.evidences
    ), "缺的那个维度还是空的"
    assert after["dimensionCoverage"] > before["dimensionCoverage"]
    assert after["evidences"] > before["evidences"]


async def test_返工花光自己的池子后就停下(mock_pipeline_db) -> None:
    """返工池有上限，且这个上限是**它自己的**。

    超发会让成本失控，而这个错误在报告里只表现为"这轮贵了一点"——
    没有任何一处会变红。所以上限要在这里被钉住。

    注意一轮返工**花不完**这个池子：一轮的目标数由审计决定
    （这里是 1 个维度 × 2 个关键词 = 2 条），而池子是"所有返工轮
    加起来"的额度。所以"见底"是个**跨轮次**的状态，得反复跑才到得了。
    """
    ctx = make_ctx()
    ctx.search.fail_queries.add(BROKEN_QUERY)
    _, targets, _ = await _first_round_then_audit(ctx)

    ctx.search_calls_made = ctx.mode.max_search_calls
    ctx.search.fail_queries.clear()

    # 反复返工。上限给得比需要的轮数宽松，见底是靠池子而不是靠循环次数。
    for _ in range(20):
        before = len(ctx.search.calls)
        await collect.run(ctx, targets=targets, rework=True)
        if len(ctx.search.calls) == before:
            break

    assert 0 < ctx.rework_search_calls_made <= ctx.mode.rework_search_calls, (
        f"返工花了 {ctx.rework_search_calls_made} 次，池子是 "
        f"{ctx.mode.rework_search_calls} 次"
    )
    assert ctx.rework_search_calls_made == ctx.mode.rework_search_calls, (
        "循环应该在池子见底时才退出——没见底就停说明别处挡了它"
    )
    assert any("返工搜索预算已用尽" in d for d in ctx.degraded_blocks), (
        f"返工池用尽没有如实记降级：{ctx.degraded_blocks}"
    )

    # 见底之后一条都不该再发。
    calls_before = len(ctx.search.calls)
    await collect.run(ctx, targets=targets, rework=True)
    assert len(ctx.search.calls) == calls_before


async def test_降级文案说的是返工池不是首轮池(mock_pipeline_db) -> None:
    """含混的一句"搜索预算已用尽"会让人去加大 `max_search_calls`。

    而返工花的是另一个池子，改那个数一点用都没有——报错必须指对旋钮。
    """
    ctx = make_ctx()
    ctx.search.fail_queries.add(BROKEN_QUERY)
    _, targets, _ = await _first_round_then_audit(ctx)
    ctx.rework_search_calls_made = ctx.mode.rework_search_calls

    await collect.run(ctx, targets=targets, rework=True)

    joined = " ".join(ctx.degraded_blocks)
    assert "返工搜索预算已用尽" in joined
    assert f"{ctx.mode.rework_search_calls}/{ctx.mode.rework_search_calls}" in joined


async def test_返工花的是另一个池子不动首轮余额(mock_pipeline_db) -> None:
    """首轮池的账不能因为返工而变。

    两个池子共用一本来记账的话，"首轮花了多少"这个数会在返工之后
    被改大，而报告里的成本归因正是按它算的。
    """
    ctx = make_ctx()
    ctx.search.fail_queries.add(BROKEN_QUERY)
    _, targets, _ = await _first_round_then_audit(ctx)

    first_round_calls = ctx.search_calls_made
    ctx.search.fail_queries.clear()
    await collect.run(ctx, targets=targets, rework=True)

    total = ctx.search_calls_made
    assert ctx.rework_search_calls_made == total - first_round_calls
    # 返工没把首轮池"用掉"：首轮池的余额仍是原样。
    remaining = ctx.mode.max_search_calls - first_round_calls
    assert remaining == ctx.mode.max_search_calls - (total - ctx.rework_search_calls_made)


@pytest.mark.parametrize("mode", ["quick", "deep", "expert"])
async def test_三档都能走通补采(mock_pipeline_db, mode: str) -> None:
    """档位只该改数字，不该改行为。返工池是每个档位各自的配置，
    而"补上缺失维度"这件事在三档里都该成立。"""
    ctx = make_ctx(mode=mode)
    ctx.search.fail_queries.add(BROKEN_QUERY)
    _, targets, _ = await _first_round_then_audit(ctx)
    assert targets, f"{mode} 档没有触发返工"

    evidence_before = len(ctx.evidences)
    ctx.search_calls_made = ctx.mode.max_search_calls
    ctx.search.fail_queries.clear()
    await collect.run(ctx, targets=targets, rework=True)

    assert len(ctx.evidences) > evidence_before
