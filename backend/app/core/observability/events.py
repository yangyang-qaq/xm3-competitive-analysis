"""SSE 事件与事件日志。

两种消费者，一个来源
--------------------
工作台看的是"当下发生了什么"，断线重连看的是"我漏掉了什么"。
两者都需要同一批事件，所以事件必须先落进一个**有 seq 的日志**，
再谈怎么推出去。参考实现直接在请求处理器里往外 yield，
于是断线的那一刻事件就不存在了——刷新页面等于从头再来。

事件形状：信封摊平，领域对象嵌套
--------------------------------
信封的四个字段**摊平**在每个事件的最外层：
`{"seq": 12, "type": "thought", "taskId": "…", "createdAt": "…", "thought": {...}}`，
而不是再套一层 `payload`。套一层的话 TypeScript 侧的 `payload` 只能是 `unknown`，
按 `type` 判别的联合类型当场失效——那正是要避免的退化。

但**携带领域对象的那六种事件把对象嵌在自己的键下面**（`thought` / `evidence` /
`span` / `chart` / `image` / `message`）。两个理由，第二个是硬性的：

1. 这些对象在报告里、在接口里都有自己的类型（`Thought` / `Evidence` / …）。
   同一样东西在事件里摊平、在别处嵌套，就会出现两份描述它的类型。
2. **摊平有时根本做不到**。`RESERVED_KEYS` 是保留的，载荷里再出现 `taskId`
   或 `seq` 会被 `publish()` 抛错，而 `Span` 恰好两个都要（落库读取路径带
   `taskId`，决策回放排序靠 `seq`）。摊平没有位置放它们。

剩下五种是「事件自身的事实」（`node_update` / `progress` / `report_ready` /
`done` / `error`），它们摊平。

字段级契约在 `contracts/sse_events.json`
----------------------------------------
那份 JSON 是唯一真相源，两侧各有一条测试读它：后端的契约测试跑一遍 Mock
流水线，断言真实推出去的事件与它逐键一致；前端的契约测试断言
`types/events.ts` 里手写的样例与它一致。改字段名必须两边一起改，
否则至少一边会红——而不是等浏览器里少显示一块内容。

代价：payload 的键不能叫 `seq` / `type` / `taskId` / `createdAt`。
这个冲突在 `publish()` 里显式拦住并抛错，而不是让一个字段静默覆盖另一个。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: 11 种事件类型。**字段级契约在 `contracts/sse_events.json`**，两侧各有一条
#: 测试读它；改这里要连着改那份 JSON 与 `frontend/src/types/events.ts`。
#:
#: 这里不再声称"顺序"有任何含义——它曾经被注释成"前端 DAG 之外的事件分类顺序"，
#: 而那个顺序从来没有被谁读过。一个不描述任何行为的顺序，只会让人以为
#: 调整它会有什么后果。
EVENT_TYPES: tuple[str, ...] = (
    "node_update",   # 流水线节点状态变化（驱动 DAG）
    "progress",      # 阶段内进度 + 累计 token / 成本
    "thought",       # 专家思维流（携带 Thought）
    "message",       # 专家间消息（携带 MessageSummary）
    "evidence",      # 新采集到的证据（携带 Evidence）
    "trace",         # 一次调用结束时的埋点（携带 TraceSpan）
    "chart",         # 图表规格（携带 ChartSpec，带 evidenceIds）
    "image",         # 图集配图（携带 GalleryImage）
    "report_ready",  # 报告落库完成
    "done",          # 任务终态，带整份指标
    "error",         # 错误
)

#: 信封保留键。payload 用了这些键会抛错——静默覆盖会让前端拿到一个
#: seq 错乱或者 type 被改掉的事件，那是比崩溃更难查的故障。
#:
#: 公开（不是 `_` 前缀）是刻意的：测试要拿它断言"没有任何一处埋点
#: 用了保留键"，而各事件发射点也可能需要它来给域对象起名。
#: 复制一份字面量到别处，等于把"唯一真相源"变成两份。
RESERVED_KEYS = frozenset({"seq", "type", "taskId", "createdAt"})

log = logging.getLogger(__name__)

#: 内存里保留的事件条数上限。
#: 超出后丢弃最旧的：内存日志只是"最近发生了什么"的缓存，
#: 完整的续传靠 `task_events` 表（`hydrate` 从那里读）。
_DEFAULT_MAX_KEEP = 5000


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass
class PipelineEvent:
    seq: int
    task_id: str
    type: str
    data: dict = field(default_factory=dict)
    created_at: str = ""

    def payload(self) -> dict:
        """摊平后的事件体，就是 SSE `data:` 行的内容。"""
        return {
            "seq": self.seq,
            "taskId": self.task_id,
            "type": self.type,
            "createdAt": self.created_at,
            **self.data,
        }

    def to_sse(self) -> str:
        """渲染成一帧 SSE。

        `id:` 行是断线续传的全部秘密：浏览器把它记在 `Last-Event-ID` 上
        并在重连时自动带上，服务端据此补发。少了这一行，
        `EventSource` 依然会自动重连，但接上的是一个有洞的流——
        而且这个洞不会报错，只会让工作台上少几段思维。
        """
        body = json.dumps(self.payload(), ensure_ascii=False, separators=(",", ":"))
        return f"id: {self.seq}\nevent: {self.type}\ndata: {body}\n\n"

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "taskId": self.task_id,
            "type": self.type,
            "data": self.data,
            "createdAt": self.created_at,
        }

    @classmethod
    def from_row(cls, row: dict) -> PipelineEvent:
        return cls(
            seq=int(row["seq"]),
            task_id=row["task_id"],
            type=row["type"],
            data=row.get("data") or {},
            created_at=row.get("created_at") or "",
        )


class EventJournal:
    """一个任务的事件日志。

    线程安全：流水线把 provider 调用扇出到线程池，采集线程和在途的
    写作协程会同时发布事件。没有锁的话 seq 会撞号——而 seq 撞号
    直接破坏前端的去重逻辑（`seq <= lastSeq` 丢弃），表现是**事件随机丢失**。
    """

    def __init__(
        self,
        task_id: str,
        *,
        max_keep: int = _DEFAULT_MAX_KEEP,
        sink: Any = None,
    ) -> None:
        self.task_id = task_id
        self._events: list[PipelineEvent] = []
        self._seq = 0
        self._lock = threading.Lock()
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._closed = False
        self._max_keep = max_keep
        #: 落库回调列表，签名为 `sink(event: PipelineEvent) -> None`。
        #:
        #: 是**列表**而不是单个回调：同一个 journal 有两个互不相干的消费者——
        #: `events_repo.append` 负责持久化（断线续传靠它），
        #: `TaskRunner._track` 负责把事件里的进度同步到任务行。
        #: 只留一个的话，后接的那个会把先接的挤掉：要么任务行停在 0%，
        #: 要么事件不落库、刷新页面丢历史。两者都不会报错。
        self._sinks: list[Any] = [sink] if sink is not None else []

    def add_sink(self, sink: Any) -> None:
        """挂一个消费者。重复挂同一个会被忽略。"""
        with self._lock:
            if sink not in self._sinks:
                self._sinks.append(sink)

    # ---- 写入 ----

    def publish(self, type_: str, data: dict | None = None) -> PipelineEvent:
        """追加一条事件并推给所有订阅者。

        可以从任意线程调用：订阅者那边用 `call_soon_threadsafe` 唤醒，
        因为工作线程直接往 `asyncio.Queue` 里 put 是未定义行为。
        """
        payload = dict(data or {})
        conflicts = RESERVED_KEYS.intersection(payload)
        if conflicts:
            raise ValueError(
                f"事件 payload 占用了信封保留键 {sorted(conflicts)}；"
                f"请改名（这些键由 journal 统一填充）"
            )

        with self._lock:
            self._seq += 1
            event = PipelineEvent(
                seq=self._seq,
                task_id=self.task_id,
                type=type_,
                data=payload,
                created_at=_now_iso(),
            )
            self._events.append(event)
            if len(self._events) > self._max_keep:
                del self._events[: len(self._events) - self._max_keep]
            sinks = list(self._sinks)

            # **投递给订阅者这一步必须在锁内**，与 seq 的分配一起完成。
            #
            # 这里踩过一个只在时序巧合下出现的坑：入队原本在锁外，于是两个线程
            # 可以各自拿到 seq 31 与 32，然后按**相反的顺序**调
            # `call_soon_threadsafe`——队列里就成了 [32, 31]。而订阅端的去重是
            # 一个单调水位（`item.seq <= yield_from` 就跳过），收到 32 之后
            # 水位推到 32，随后的 31 被判成重复**静默丢掉**。
            #
            # 症状是 SSE 流里少一条事件，且只在线程池扇出（采集阶段）时
            # 偶发。更糟的是它不可恢复：前端的水位已经推到 32，
            # 重连时按 Last-Event-ID 补发的是 `since(32)`，31 永远拿不回来。
            #
            # `call_soon_threadsafe` 只是往目标循环的就绪队列里塞一项，不会阻塞，
            # 所以放在锁里是安全的。**消费者回调留在锁外**（见下）——
            # 那些是外部代码，可能很慢甚至阻塞，不能拿它们占着锁。
            #
            # 吞掉的 `RuntimeError` 是"订阅者所在的事件循环已经关了"（客户端断开、
            # 任务收尾）。这里不是错误：事件已经写进日志，对方没人在听而已。
            for loop, queue in self._subscribers:
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(queue.put_nowait, event)

        for sink in sinks:
            # 一个消费者出问题不该让另一个也收不到：任务行的更新失败
            # 不应该导致事件不落库。所以逐个 try，而不是整体一个 try。
            try:
                sink(event)
            except Exception:  # pragma: no cover - 见上
                log.exception("事件消费者抛出异常，已忽略：%s", event.type)
        return event

    def close(self) -> None:
        """通知所有订阅者流已结束。"""
        with self._lock:
            self._closed = True
            subscribers = list(self._subscribers)
            self._subscribers.clear()
        for loop, queue in subscribers:
            # 同上：循环已关闭时 `call_soon_threadsafe` 抛 RuntimeError。
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(queue.put_nowait, None)

    # ---- 读取 ----

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    @property
    def closed(self) -> bool:
        return self._closed

    def since(self, from_seq: int) -> list[PipelineEvent]:
        """取 seq 严格大于 `from_seq` 的事件。`from_seq=0` 即全量。"""
        with self._lock:
            return [e for e in self._events if e.seq > from_seq]

    def all(self) -> list[PipelineEvent]:
        with self._lock:
            return list(self._events)

    def oldest_seq(self) -> int:
        with self._lock:
            return self._events[0].seq if self._events else 0

    def hydrate(self, rows: Iterable[dict]) -> int:
        """从持久化的事件行恢复日志（进程重启后继续服务同一次任务）。

        seq 会接着已有的最大值往下走，而不是从 1 重新开始——
        从头编号会让前端的 `seq <= lastSeq` 去重把整段新事件当重复丢掉。
        """
        rows = sorted(rows, key=lambda r: int(r["seq"]))
        with self._lock:
            for row in rows:
                event = PipelineEvent.from_row(row)
                if event.task_id and event.task_id != self.task_id:
                    continue
                existing = {e.seq for e in self._events}
                if event.seq in existing:
                    continue
                self._events.append(event)
            self._seq = max((e.seq for e in self._events), default=0)
        return self._seq

    # ---- 订阅 ----

    async def subscribe(self, from_seq: int = 0) -> AsyncIterator[PipelineEvent]:
        """先补发历史，再接上实时推送。

        注册订阅者与读取历史必须在**同一次加锁**里完成，否则两者之间
        发布的事件会两头都不在：既不在刚读到的历史里，也不在队列里
        （因为那时还没注册）。这是一个只在特定时序下才出现的丢事件 bug。
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.append((loop, queue))
            backlog = [e for e in self._events if e.seq > from_seq]
            already_closed = self._closed

        try:
            yield_from = 0
            for event in backlog:
                yield_from = event.seq
                yield event
            if already_closed:
                return
            while True:
                item = await queue.get()
                if item is None:
                    return
                if item.seq <= yield_from:
                    continue
                yield_from = item.seq
                yield item
        finally:
            with self._lock:
                self._subscribers = [
                    (lp, q) for lp, q in self._subscribers if q is not queue
                ]

    def iter_sse(self, from_seq: int = 0) -> Iterator[str]:
        """把已有事件渲染成 SSE 帧序列。CLI 与测试用同步版本，
        免得为了一次性的打印去搭一个事件循环。"""
        for event in self.since(from_seq):
            yield event.to_sse()


# ============================================================
# 进程内的 journal 注册表
# ============================================================

_JOURNALS: dict[str, EventJournal] = {}
_JOURNALS_LOCK = threading.Lock()


def journal_for(task_id: str) -> EventJournal | None:
    with _JOURNALS_LOCK:
        return _JOURNALS.get(task_id)


def make_journal(task_id: str, *, sink: Any = None, max_keep: int = _DEFAULT_MAX_KEEP) -> EventJournal:
    """取（或新建）某个任务的 journal。

    幂等很重要：`GET /stream` 可能被两个标签页同时打开，
    它们必须拿到**同一个** journal。各自新建一个的话，后开的那个
    只有从它自己创建之后的事件，前一半思维流凭空消失。
    """
    with _JOURNALS_LOCK:
        existing = _JOURNALS.get(task_id)
        if existing is not None:
            if sink is not None:
                existing.add_sink(sink)
            return existing
        journal = EventJournal(task_id, max_keep=max_keep, sink=sink)
        _JOURNALS[task_id] = journal
        return journal


def drop_journal(task_id: str) -> None:
    with _JOURNALS_LOCK:
        _JOURNALS.pop(task_id, None)


def reset_journals() -> None:
    """清空注册表。测试之间必须调用，否则用例会互相看到对方的事件。"""
    with _JOURNALS_LOCK:
        _JOURNALS.clear()
