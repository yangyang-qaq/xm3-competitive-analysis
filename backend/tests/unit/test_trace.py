"""埋点测试。

这个模块守着两类**没有异常**的故障，它们都不是崩溃，而是数字变小：

1. **tracer 没被绑进上下文**。`span()` 在拿不到 tracer 时退化成空操作，
   于是每次调用照样跑，但一条埋点都不记：成本 0、token 0、trace 面板空。
   报告的每一页都正常渲染，只是"很便宜"。所以这里有一条用例专门断言
   `use_tracer` 之后 `asyncio.to_thread` 里也看得见它——那是流水线
   扇出的实际路径。
2. **`to_event()` 带了信封保留键**。它曾经带 `taskId`，而 journal 会
   为这个直接抛错，整个流水线在第一个阶段就崩。这条崩溃是好事
   （比静默覆盖一个别的任务的 taskId 好），但必须有测试守着，
   否则下次有人为了"信息更全"再把 taskId 加回去。
"""
from __future__ import annotations

import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.core.observability.events import RESERVED_KEYS, EventJournal
from app.core.observability.trace import (
    Span,
    Tracer,
    current_span_id,
    current_tracer,
    record_llm_usage,
    span,
    use_tracer,
)


def _camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(word.capitalize() for word in tail)


def _span(**overrides) -> Span:
    base = {"span_id": "SP-00001", "task_id": "TK-1", "kind": "llm", "name": "analyze_claims"}
    base.update(overrides)
    return Span(**base)


# ============================================================
# 序列化：两个方向，两份形状
# ============================================================


def test_to_event_carries_no_envelope_keys() -> None:
    """回归用例：`to_event()` 的键不能与信封保留键相交。

    它的载荷会被直接塞进 `journal.publish()`，而那里会抛错。
    """
    assert not (RESERVED_KEYS & set(_span().to_event()))


def test_to_event_can_always_be_published() -> None:
    """比"键集合不相交"更直接的断言：真的发一次。

    载荷是嵌在 `span` 下面发的，所以 `to_event()` 自己带不带保留键
    已经不影响能不能发出去。这条用例保的是**那个嵌套本身**：
    万一有人图省事把它摊平（`publish("trace", span.to_event())` 少一层），
    信封与载荷就挤在同一层上，将来 `to_event` 多一个字段就可能撞上
    `taskId`——而那正是最初流水线在第一个阶段就崩掉的原因。
    """
    journal = EventJournal("TK-1")
    event = journal.publish("trace", {"span": _span().to_event()})

    assert event.seq == 1
    assert event.payload()["span"]["kind"] == "llm"
    # 摊平的话 `kind` 会在最外层，与 envelope 同级
    assert "kind" not in event.payload()


def test_to_event_and_to_dict_describe_the_same_fields() -> None:
    """一份落库（snake_case）、一份推送（camelCase），键集合必须一一对应。

    `to_dict` 是从 `__dict__` 派生的、`to_event` 是手写的，所以新增一个
    字段时很容易只改一处。漏掉 `to_event` 的后果是前端拿不到它，
    漏掉 `to_dict` 的后果是刷新页面后那个字段消失——两者都不报错。

    两处刻意的差异：`to_event` 不含信封键 `taskId`（由 journal 填），
    `to_dict` 不含派生的 `totalTokens`（列里没这一列，由两个 token 列相加）。
    """
    sp = _span()
    row_keys = {_camel(key) for key in sp.to_dict() if not key.startswith("_")}

    assert set(sp.to_event()) | {"taskId"} == row_keys | {"totalTokens"}


def test_to_event_reports_total_tokens_as_a_derived_value() -> None:
    """前端只想知道"这条路花了多少 token"，不该自己去加两个数。"""
    event = _span(prompt_tokens=120, completion_tokens=30).to_event()

    assert event["totalTokens"] == 150
    assert event["costUsd"] == 0.0


def test_to_dict_keeps_the_private_monotonic_clock_out_of_the_row() -> None:
    """`_started_monotonic` 是下划线开的内部字段，不能进 `traces` 表。"""
    assert "_started_monotonic" not in _span().to_dict()


# ============================================================
# span 树
# ============================================================


def test_nested_spans_record_their_parent() -> None:
    """嵌套靠 `contextvars` 的 span 栈，不靠参数传递。

    靠参数传的话，每个函数签名都得带一个跟业务无关的参数，
    而任何一次重构忘了往下传，成本就静默消失了。
    """
    tracer = Tracer("TK-1")
    parents: list[tuple[str, str]] = []

    with tracer.span("stage", "collect"), tracer.span("search", "搜索") as child:
        parents.append((child.span_id, child.parent_id))

    assert parents == [("SP-00002", "SP-00001")]


def test_span_ids_are_sequential_from_one() -> None:
    """按任务从 1 编。决策回放的滑杆看到的是"这次任务的第 37 步"。"""
    tracer = Tracer("TK-1")
    with tracer.span("llm", "a"), tracer.span("llm", "b"):
        pass

    assert [sp.span_id for sp in tracer.spans()] == ["SP-00001", "SP-00002"]


def test_spans_come_back_in_start_order_even_when_nested() -> None:
    """收集是**完成**顺序，返回是**开始**顺序。

    一个嵌套的父 span 一定晚于它的子 span 结束。如果直接把收集顺序
    返回，`spans()[0]` 是不是"第 1 步"就取决于嵌套结构——
    而决策回放按步走，它要的是开始顺序。
    """
    tracer = Tracer("TK-1")
    # 写成一个 `with` 而不是嵌套两层：语义完全相同（先进父再进子、
    # 先出子再出父），而嵌套的写法会被 lint 判为可合并。嵌套结构本身
    # 在这条用例里是要表达的重点，所以用缩进和注释把它说明白。
    with (
        tracer.span("stage", "collect"),   # SP-00001，最后结束
        tracer.span("search", "先结束"),    # SP-00002
    ):
        pass

    assert [sp.span_id for sp in tracer.spans()] == ["SP-00001", "SP-00002"]
    assert [row["span_id"] for row in tracer.snapshot()] == ["SP-00001", "SP-00002"]


def test_spans_order_follows_span_id_even_when_timestamps_contradict_it() -> None:
    """**编号与时间戳矛盾时，以编号为准。**

    上一条用例（`..._even_when_nested`）其实**分辨不出排序键是哪一个**：
    它那两个 span 是在同一毫秒里开出来的（`_now_iso()` 只到毫秒），
    于是按 `started_at` 排会**并列**，而 Python 的排序是稳定的，
    退回原始（完成）顺序——恰好是错的，所以那条用例变红了。

    也就是说它的守卫力来自一个巧合：**时间戳粒度比 span 创建还粗**。
    哪天把 `_now_iso()` 提到微秒精度，两个 span 就会拿到严格递增的时间戳，
    "按时间排"与"按编号排"结果一致，上一条用例不再能分辨，
    而那时没有任何东西在守"编号 = 开始顺序"。

    这一条**直接构造矛盾**，把守卫从巧合变成断言：把第一条的
    `started_at` 改到晚于第二条，也就是并发下真会发生的那种交错
    （`span()` 里编号在锁内发、时间戳在锁外取，两道临界区之间有一道缝）。
    改排序键为 `started_at` 会让这一条稳定变红，与时间戳精度无关。
    """
    tracer = Tracer("TK-1")
    with tracer.span("llm", "a"), tracer.span("llm", "b"):
        pass

    first, second = tracer.spans()
    assert first.span_id == "SP-00001"
    first.started_at = "2099-01-01T00:00:00.000+00:00"  # 故意晚于 SP-00002

    assert [sp.span_id for sp in tracer.spans()] == ["SP-00001", "SP-00002"]
    assert [row["span_id"] for row in tracer.snapshot()] == ["SP-00001", "SP-00002"]


def test_span_records_the_error_before_reraising() -> None:
    """异常照常抛出，但**先记下错误**。

    span 是诊断"为什么这次失败"的唯一入口。吞掉异常会让失败无法解释；
    丢掉 span 会让失败无从查起。
    """
    tracer = Tracer("TK-1")

    with pytest.raises(ValueError, match="boom"), tracer.span("llm", "会炸的"):
        raise ValueError("boom")

    span_row = tracer.spans()[0]
    assert span_row.status == "error"
    assert "ValueError: boom" in span_row.error
    assert span_row.duration_ms >= 0


def test_error_message_is_truncated() -> None:
    """过长的错误串会撑爆 `traces.error` 这一列（也是前端面板的一行）。"""
    tracer = Tracer("TK-1")

    with pytest.raises(RuntimeError), tracer.span("llm", "很长的错误"):
        raise RuntimeError("x" * 5000)

    assert len(tracer.spans()[0].error) == 500


def test_on_span_fires_on_completion_not_on_start() -> None:
    """在**完成时**推一条 trace，不是开始时。

    一个 span 最有用的信息是它花了多久、花了多少钱，而这两样在开始时
    都还不存在。先推"开始了"再推"结束了"，会让前端收到两倍的事件
    去做一件一条就能做完的事。
    """
    fired: list[str] = []
    tracer = Tracer("TK-1", on_span=lambda sp: fired.append(sp.span_id))

    with tracer.span("llm", "一"):
        assert fired == []
    assert fired == ["SP-00001"]


def test_on_span_fires_for_failed_spans_too() -> None:
    """失败的那次调用同样要进 trace 面板——它往往是最需要看的一条。"""
    fired: list[Span] = []
    tracer = Tracer("TK-1", on_span=fired.append)

    with pytest.raises(ValueError), tracer.span("llm", "会炸的"):
        raise ValueError("boom")

    assert [sp.status for sp in fired] == ["error"]


# ============================================================
# 绑定
# ============================================================


def test_module_level_span_is_a_noop_without_a_tracer() -> None:
    """没有 tracer 时退化成空操作，并返回一个**游离的 span**。

    返回游离 span 而不是 `None` 是刻意的：调用点可以无条件写
    `sp.detail["hits"] = 3`，不必在每一处埋点前判断 `if sp is not None`。
    埋点代码里的分支越多，漏埋的概率越高。
    """
    assert current_tracer() is None

    with span("search", "搜索") as sp:
        sp.detail["hits"] = 3
        assert sp.span_id == ""

    assert sp.duration_ms >= 0


def test_use_tracer_binds_and_unbinds() -> None:
    tracer = Tracer("TK-1")

    with use_tracer(tracer):
        assert current_tracer() is tracer

    assert current_tracer() is None


def test_span_is_recorded_when_a_tracer_is_bound() -> None:
    tracer = Tracer("TK-1")

    with use_tracer(tracer), span("search", "搜索") as sp:
        sp.detail["hits"] = 7

    assert len(tracer.spans()) == 1
    assert tracer.spans()[0].detail == {"hits": 7}


async def test_tracer_survives_asyncio_to_thread() -> None:
    """**这是绑定方式之所以是 contextvars 的全部原因。**

    流水线把搜索与抓取扇出到线程池。`asyncio.to_thread` 会复制当前上下文，
    所以在工作线程里跑的 provider 调用自动看得见发起它的那个 tracer，
    父 span 也就自动接上了——不需要任何显式传递。

    这条用例失败意味着**流水线里的每一次扇出都没有埋点**：
    没有异常，只是成本表和 trace 面板是空的。
    """
    tracer = Tracer("TK-1")

    with use_tracer(tracer), tracer.span("stage", "collect"):
        seen = await asyncio.to_thread(current_tracer)
        parent_in_thread = await asyncio.to_thread(current_span_id)

    assert seen is tracer
    # 线程里看得见父 span，所以线程内开的 span 会正确挂到父节点下。
    assert parent_in_thread == "SP-00001"


async def test_thread_fanout_spans_land_in_the_same_tracer() -> None:
    tracer = Tracer("TK-1")

    def work(index: int) -> int:
        with span("search", f"搜索 {index}") as sp:
            sp.detail["hits"] = index
        return index

    with use_tracer(tracer):
        await asyncio.gather(*(asyncio.to_thread(work, i) for i in range(5)))

    assert len(tracer.spans()) == 5


def test_thread_pool_executor_submit_does_not_copy_the_context() -> None:
    """`ThreadPoolExecutor.submit` **不**复制上下文，`to_thread` 才复制。

    这条用例是把那个限制写下来，而不是断言它"应该"这样：将来若有人
    为了复用线程池把 `to_thread` 换成 `submit`，埋点会**整片消失**，
    而这是一个没有任何提示的改动。让它在这里红一次。

    修法是把 `contextvars.copy_context()` 显式传进去（下面第二段）。
    """
    tracer = Tracer("TK-1")

    with use_tracer(tracer), ThreadPoolExecutor(max_workers=1) as pool:
        without_copy = pool.submit(current_tracer).result()
        ctx = contextvars.copy_context()
        with_copy = pool.submit(ctx.run, current_tracer).result()

    assert without_copy is None
    assert with_copy is tracer


def test_detached_span_can_still_be_written_to() -> None:
    """游离 span 的字段赋值不能抛异常——埋点代码不该为此写分支。"""
    with span("fetch", "抓取") as sp:
        sp.status = "degraded"
        sp.detail["reason"] = "超时"

    assert sp.status == "degraded"


# ============================================================
# 用量
# ============================================================


def _response(prompt: int, completion: int, cost: float, model: str = "mock-core") -> SimpleNamespace:
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(
            prompt_tokens=prompt, completion_tokens=completion, cost_usd=cost
        ),
    )


def test_record_llm_usage_writes_tokens_and_cost() -> None:
    """漏记 token 是最容易发生也最昂贵的埋点失误：它不报错，
    只让成本表偏小。所有 LLM 调用点走同一个收口函数就不存在漏记。"""
    sp = _span()

    record_llm_usage(sp, _response(120, 30, 0.0042), "mock")

    assert sp.prompt_tokens == 120
    assert sp.completion_tokens == 30
    assert sp.cost_usd == pytest.approx(0.0042)
    assert sp.provider == "mock"
    assert sp.model == "mock-core"
    assert sp.usage["totalTokens"] == 150


def test_record_llm_usage_tolerates_a_response_without_usage() -> None:
    """响应里没有 usage 时只记下 provider/model，不抛异常。

    真实的 OpenAI 兼容端点在某些错误分支下确实会返回没有 usage 的体，
    为此让整次流水线失败是不划算的。
    """
    sp = _span()

    record_llm_usage(sp, SimpleNamespace(model="m", usage=None), "acme")

    assert sp.prompt_tokens == 0
    assert sp.model == "m"


def test_record_llm_usage_is_safe_on_a_detached_span() -> None:
    record_llm_usage(None, _response(1, 1, 0.0), "mock")  # 不该抛


# ============================================================
# 聚合
# ============================================================


def test_metrics_aggregate_calls_tokens_and_cost() -> None:
    """这些数字是报告的"成本与耗时"面板的数据源，
    也是返工前后对比（after − before）的被比较对象。"""
    tracer = Tracer("TK-1")
    with tracer.span("llm", "a") as sp:
        record_llm_usage(sp, _response(100, 50, 0.01), "mock")
    with tracer.span("search", "b") as sp:
        sp.detail["hits"] = 3
    with tracer.span("fetch", "c") as sp:
        sp.status = "degraded"

    metrics = tracer.metrics()

    assert metrics["endpointCount"] == {"llm": 1, "search": 1, "fetch": 1, "stage": 0}
    assert metrics["totalCalls"] == 3
    assert metrics["totalTokens"] == 150
    assert metrics["totalCostUsd"] == pytest.approx(0.01)
    assert metrics["degradedCalls"] == 1
    assert metrics["errorCalls"] == 0
    assert metrics["taskId"] == "TK-1"


def test_cost_by_model_separates_the_fallback_model() -> None:
    """降级到备用 provider 时，同一次任务会花在两个模型上。

    不分模型统计的话，"这次花了多少钱"是一个无法归因的数字，
    降级也就成了隐形的成本泄漏。
    """
    tracer = Tracer("TK-1")
    with tracer.span("llm", "主模型") as sp:
        record_llm_usage(sp, _response(100, 0, 0.01, model="primary-core"), "primary")
    with tracer.span("llm", "备用模型") as sp:
        record_llm_usage(sp, _response(50, 0, 0.002, model="fb-fast"), "fallback")

    table = tracer.cost_by_model()

    assert set(table) == {"fallback:fb-fast", "primary:primary-core"}
    assert table["primary:primary-core"]["calls"] == 1
    assert table["primary:primary-core"]["costUsd"] == pytest.approx(0.01)
    assert table["fallback:fb-fast"]["costUsd"] == pytest.approx(0.002)


def test_snapshot_returns_rows_in_the_traces_table_shape() -> None:
    tracer = Tracer("TK-1")
    with tracer.span("llm", "a"):
        pass

    row = tracer.snapshot()[0]

    assert row["span_id"] == "SP-00001"
    assert row["task_id"] == "TK-1"
    assert "duration_ms" in row
