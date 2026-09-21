"""无侵入埋点。

为什么用 contextvars 而不是"到处传一个 tracer 参数"
--------------------------------------------------
流水线要扇出到线程（`asyncio.to_thread`），如果 tracer 靠参数传递，
那么每一个函数签名都得带一个跟业务无关的参数，而且任何一次重构忘了往下传，
那次调用的成本就**静默消失**了——成本表少一笔比成本表多一笔危险得多。

`contextvars` 的语义恰好对上：`asyncio.to_thread` **会复制当前上下文**，
所以在线程池里跑的 provider 调用自动看得见发起它的那个 tracer 与 span 栈，
父 span 也就自动接上了，不需要任何显式传递。

一个必须说清的限制：contextvars 是单向的。在子线程里 push 的 span 不会
传回父协程的栈——这正是我们要的（同一个 span 不该出现在两条时间线上），
但意味着**跨线程的父子关系靠的是"复制时的快照"**，子线程里再开新协程不受影响。

seq 是按任务的，不是全局的
--------------------------
参考实现用一个全局 `_SEQ` 计数器。两个并发任务的事件序号会交错，
而决策回放的滑杆恰恰按 seq 排序——交错之后"第 40 秒发生了什么"就不可回答了。
这里每个 `Tracer` 自带计数器，event journal 也各自独立。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _new_span_id(counter: int) -> str:
    return f"SP-{counter:05d}"


@dataclass
class Span:
    """一次被观测的调用。

    字段刻意分成三组：**谁**（kind/name/provider/model）、**多久**（时间戳与耗时）、
    **花了多少**（token 与成本）。这三组分别回答三个不同的问题：
    "这一步是谁做的"、"它慢在哪"、"它贵在哪"。
    """

    span_id: str
    task_id: str
    kind: str  # llm | search | fetch | stage
    name: str
    parent_id: str = ""
    purpose: str = ""

    provider: str = ""
    model: str = ""

    started_at: str = ""
    ended_at: str = ""
    duration_ms: int = 0

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    #: `prompt_tokens` 里命中前缀缓存的那部分（子集，不是增量）。
    #: 落库是为了让"成本为什么这么低"可追溯——命中价与未命中价差 50 倍，
    #: 只看 cost_usd 一个数，没法判断它是真便宜还是定价表配错了。
    cached_prompt_tokens: int = 0

    status: str = "ok"  # ok | error | degraded
    error: str = ""
    #: 这一次调用产出了什么。刻意是自由 dict：搜索记命中数，分析记论点数，
    #: 写作记字符数——强行统一成一个 schema 只会得到一堆恒为 0 的字段。
    detail: dict = field(default_factory=dict)

    _started_monotonic: float = 0.0

    def to_dict(self) -> dict:
        """snake_case。**这是 `traces` 表的行形状**，不是对外接口形状。

        落库用一份、推送用另一份，看起来像重复，但两者读的是不同的源：
        这里从对象取，`traces._row_to_span` 从 SQL 列取（列名是 snake_case，
        而且缺少几个派生字段）。强行统一只会让其中一边绕一圈再绕回来。
        两边的键集合必须一致，有一条测试守着。
        """
        data = {
            k: v for k, v in self.__dict__.items() if not k.startswith("_")
        }
        return data

    def to_event(self) -> dict:
        """camelCase。SSE `trace` 事件的载荷。

        **不含 `taskId`**——它是信封保留键，由 journal 统一填。这里再带一份
        会被 `publish()` 直接抛错拦住（这正是想要的行为：静默覆盖会让前端
        拿到一个属于别的任务的 trace）。落库读取路径 `traces._row_to_span`
        带 `taskId`，因为它直接从 SQL 列来、不经过信封，两者差这一个键。
        """
        return {
            "spanId": self.span_id,
            "parentId": self.parent_id,
            "kind": self.kind,
            "name": self.name,
            "purpose": self.purpose,
            "provider": self.provider,
            "model": self.model,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "durationMs": self.duration_ms,
            "promptTokens": self.prompt_tokens,
            "completionTokens": self.completion_tokens,
            "totalTokens": self.prompt_tokens + self.completion_tokens,
            "costUsd": round(self.cost_usd, 6),
            "cachedPromptTokens": self.cached_prompt_tokens,
            "status": self.status,
            "error": self.error,
            "detail": dict(self.detail),
        }

    @property
    def usage(self) -> dict:
        return {
            "promptTokens": self.prompt_tokens,
            "completionTokens": self.completion_tokens,
            "totalTokens": self.prompt_tokens + self.completion_tokens,
            "costUsd": round(self.cost_usd, 6),
            "cachedPromptTokens": self.cached_prompt_tokens,
        }


_current: ContextVar[Tracer | None] = ContextVar("xm3_tracer", default=None)
_stack: ContextVar[tuple[str, ...]] = ContextVar("xm3_span_stack", default=())


def current_tracer() -> Tracer | None:
    return _current.get()


def current_span_id() -> str:
    stack = _stack.get()
    return stack[-1] if stack else ""


class Tracer:
    """一个任务的 span 收集器。

    线程安全：采集阶段会把搜索并发扇出到线程池，多个线程同时 append。
    没有锁的话，CPython 之外的列表操作不保证原子——而成本统计错一条
    比整张表都错更难发现。
    """

    def __init__(self, task_id: str, *, on_span: Callable[[Span], None] | None = None) -> None:
        self.task_id = task_id
        #: span 结束时的回调。用来把 trace 事件推进 SSE 流——
        #: 没有它，工作台的悬浮 trace 面板只能等任务跑完再拉一次全量，
        #: 于是它显示的是"刚才发生了什么"而不是"正在发生什么"。
        self._on_span = on_span
        self._spans: list[Span] = []
        self._lock = threading.Lock()
        self._counter = 0
        self._started_monotonic = time.monotonic()
        self.started_at = _now_iso()

    # ---- 采集 ----

    def _next_span_id(self) -> str:
        with self._lock:
            self._counter += 1
            return _new_span_id(self._counter)

    def _append(self, sp: Span) -> None:
        with self._lock:
            self._spans.append(sp)

    @contextmanager
    def span(
        self,
        kind: str,
        name: str,
        *,
        purpose: str = "",
        detail: dict | None = None,
    ) -> Iterator[Span]:
        """开一个 span。异常会被照常抛出，但**先记下错误再抛**——
        span 是诊断"为什么这次失败"的唯一入口，把异常吞掉或让 span 丢失
        都会让失败变成不可解释的。"""
        parent = current_span_id()
        sp = Span(
            span_id=self._next_span_id(),
            task_id=self.task_id,
            kind=kind,
            name=name,
            parent_id=parent,
            purpose=purpose,
            detail=dict(detail or {}),
            started_at=_now_iso(),
            _started_monotonic=time.monotonic(),
        )
        token = _stack.set((*_stack.get(), sp.span_id))
        try:
            yield sp
        except Exception as exc:
            sp.status = "error"
            sp.error = f"{type(exc).__name__}: {exc}"[:500]
            self._finish(sp)
            raise
        else:
            self._finish(sp)
        finally:
            _stack.reset(token)

    def _finish(self, sp: Span) -> None:
        sp.ended_at = _now_iso()
        sp.duration_ms = int((time.monotonic() - sp._started_monotonic) * 1000)
        # **先落进收集器，再通知订阅者。** 少了这一句 `_spans` 永远是空的，
        # 而这不是崩溃：`trace` 事件照常推送（走 `_on_span`），工作台的悬浮
        # 面板看得见每一次调用，但 `snapshot()` 是空的、`traces` 表一条不写、
        # `metrics()` 全为 0——报告里于是印着"token 0 / $0.0000"，
        # 读起来像是"这个系统很省钱"，而不是"埋点没记上"。
        # 顺序也有讲究：订阅者拿到 span 时它应该已经在收集器里了。
        self._append(sp)
        if self._on_span is not None:
            self._on_span(sp)

    @staticmethod
    def _sequence(span_id: str) -> int:
        """`SP-00007` → 7。取不出数字的（游离 span）排在最前。"""
        suffix = span_id[3:]
        return int(suffix) if suffix.isdigit() else 0

    def snapshot(self) -> list[dict]:
        """按**开始顺序**返回，与 `span_id` 的编号一致。

        收集的顺序是**完成**顺序（`_finish` 时才 append），而一个嵌套的
        父 span 一定晚于它的子 span 完成。两种顺序混在一起的话，
        `spans()[0]` 到底是不是"第 1 步"就取决于嵌套结构——
        而决策回放的滑杆正是按步走的，它按开始顺序走。
        """
        return [sp.to_dict() for sp in self.spans()]

    # ---- 聚合 ----

    def spans(self) -> list[Span]:
        """按开始顺序返回。见 `snapshot` 里对两种顺序的说明。"""
        with self._lock:
            ordered = sorted(self._spans, key=lambda sp: self._sequence(sp.span_id))
        return ordered

    def metrics(self) -> dict:
        """确定性指标：全部从 span 算出来，不需要任何额外调用。

        这些数字是报告的"成本与耗时"面板的数据源，也是返工前后对比
        （`metrics_after - metrics_before`）的被比较对象。
        """
        spans = self.spans()
        by_kind: dict[str, int] = {}
        cost = 0.0
        prompt_tokens = 0
        completion_tokens = 0
        cached_prompt_tokens = 0
        errors = 0
        degraded = 0
        slowest = 0

        for sp in spans:
            by_kind[sp.kind] = by_kind.get(sp.kind, 0) + 1
            cost += sp.cost_usd
            prompt_tokens += sp.prompt_tokens
            completion_tokens += sp.completion_tokens
            cached_prompt_tokens += sp.cached_prompt_tokens
            if sp.status == "error":
                errors += 1
            elif sp.status == "degraded":
                degraded += 1
            slowest = max(slowest, sp.duration_ms)

        return {
            "taskId": self.task_id,
            "startedAt": self.started_at,
            "endpointCount": {"llm": by_kind.get("llm", 0), "search": by_kind.get("search", 0),
                              "fetch": by_kind.get("fetch", 0), "stage": by_kind.get("stage", 0)},
            "totalCalls": len(spans),
            "totalCostUsd": round(cost, 6),
            "promptTokens": prompt_tokens,
            "completionTokens": completion_tokens,
            "totalTokens": prompt_tokens + completion_tokens,
            "cachedPromptTokens": cached_prompt_tokens,
            # 命中率与成本一起报。单看成本无法判断"便宜"是因为便宜还是因为算错了，
            # 而这两个数放在一起就能对上：命中率高则成本必然低。
            "cacheHitRate": round(cached_prompt_tokens / prompt_tokens, 4) if prompt_tokens else 0.0,
            "errorCalls": errors,
            "degradedCalls": degraded,
            "slowestCallMs": slowest,
        }

    def cost_by_model(self) -> dict[str, dict]:
        """按模型聚合成本。

        降级到备用 provider 时，同一次任务会花在两个模型上。不分模型统计的话，
        "这次花了多少钱"是一个无法归因的数字，降级也就成了隐形的成本泄漏。
        """
        table: dict[str, dict] = {}
        for sp in self.spans():
            if sp.kind != "llm":
                continue
            key = f"{sp.provider}:{sp.model}" if sp.provider else sp.model
            row = table.setdefault(
                key, {"model": sp.model, "provider": sp.provider, "calls": 0, "costUsd": 0.0,
                      "tokens": 0}
            )
            row["calls"] += 1
            row["costUsd"] = round(row["costUsd"] + sp.cost_usd, 6)
            row["tokens"] += sp.prompt_tokens + sp.completion_tokens
        return table


@contextmanager
def use_tracer(tracer: Tracer | None) -> Iterator[Tracer | None]:
    """把 tracer 绑定到当前上下文。`asyncio.to_thread` 会把它带进线程池。"""
    token = _current.set(tracer)
    try:
        yield tracer
    finally:
        _current.reset(token)


@contextmanager
def span(
    kind: str, name: str, *, purpose: str = "", detail: dict | None = None
) -> Iterator[Span | None]:
    """模块级便捷入口：没有 tracer 时退化成空操作，并返回一个游离的 span。

    返回游离 span（而不是 None）是刻意的：调用点可以无条件写
    `sp.detail["hits"] = 3`，不必在每一处埋点前判断 `if sp is not None`。
    埋点代码里的分支越多，漏埋的概率越高。
    """
    tracer = current_tracer()
    if tracer is None:
        yield _detached_span(kind, name, purpose)
        return
    with tracer.span(kind, name, purpose=purpose, detail=detail) as sp:
        yield sp


def _detached_span(kind: str, name: str, purpose: str) -> Span:
    return Span(
        span_id="",
        task_id="",
        kind=kind,
        name=name,
        purpose=purpose,
        started_at=_now_iso(),
        _started_monotonic=time.monotonic(),
    )


def record_llm_usage(sp: Span | None, response: Any, provider: str) -> None:
    """把一次 LLM 响应的用量写进 span。

    独立成函数是因为"漏记 token"是最容易发生也最昂贵的埋点失误：
    它不会报错，只会让成本表偏小。所有 LLM 调用点走同一个收口函数，
    就不存在"某个分支忘了记"的可能。
    """
    if sp is None:
        return
    usage = getattr(response, "usage", None)
    sp.provider = provider
    sp.model = getattr(response, "model", "") or sp.model
    if usage is None:
        return
    sp.prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    sp.completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    sp.cost_usd = float(getattr(usage, "cost_usd", 0.0) or 0.0)
    sp.cached_prompt_tokens = int(getattr(usage, "cached_prompt_tokens", 0) or 0)
