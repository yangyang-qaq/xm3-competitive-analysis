"""事件日志测试。

三条主张各有对应用例：

1. **信封保留键在发射点就被拦住**——静默覆盖会让前端拿到一个 seq 错乱
   或 type 被改掉的事件，那比崩溃更难查（不报错，只是显示错）。
   这条曾经真的拦住过一次：span 的载荷里带了 `taskId`，整个流水线
   在第一个阶段就崩了。崩溃是好事，静默才是坏事。
2. **一个消费者出问题不能让另一个收不到**。journal 挂了两个消费者
   （落库 + 同步任务行），只留一个的话，任务行会停在 0%，或者事件不落库
   导致刷新丢历史——两者都不报错。
3. **seq 是按任务的**，不是全局的。全局计数器会让两个并发任务的事件
   交错，而决策回放的滑杆正是按 seq 排序的。
"""
from __future__ import annotations

import asyncio
import json
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.observability.events import (
    EVENT_TYPES,
    RESERVED_KEYS,
    EventJournal,
    make_journal,
    reset_journals,
)


@pytest.fixture(autouse=True)
def _clean_journals():
    reset_journals()
    yield
    reset_journals()


@pytest.fixture
def journal() -> EventJournal:
    return EventJournal("TK-test")


# ============================================================
# 信封
# ============================================================


@pytest.mark.parametrize("key", sorted(RESERVED_KEYS))
def test_reserved_key_in_payload_is_rejected(journal: EventJournal, key: str) -> None:
    with pytest.raises(ValueError, match=key):
        journal.publish("thought", {key: "占用"})


def test_reserved_key_rejection_names_every_conflicting_key(journal: EventJournal) -> None:
    """一次报出全部冲突键，而不是只报第一个。

    改埋点时通常是一次改好几个字段，只报一个会让人改一轮跑一轮。
    """
    with pytest.raises(ValueError) as excinfo:
        journal.publish("thought", {"taskId": "x", "seq": 1, "text": "正常"})

    message = str(excinfo.value)
    assert "seq" in message
    assert "taskId" in message


def test_reserved_key_rejection_does_not_consume_a_seq(journal: EventJournal) -> None:
    """被拦下的那次发布不能推进 seq。

    推进了的话，前端会看到一个跳号——而跳号与"丢事件"在 `seq <= lastSeq`
    去重逻辑下无法区分，于是每次改坏一个埋点都会让工作台显示一次
    "好像漏了点什么"。
    """
    with pytest.raises(ValueError):
        journal.publish("thought", {"seq": 1})

    assert journal.last_seq == 0


def test_envelope_keys_are_flat_not_nested(journal: EventJournal) -> None:
    """信封是摊平的：`{"seq":1,"type":"thought","taskId":"…","createdAt":"…", …}`。

    外面**不再套一层 `payload`**：套了的话 TypeScript 侧的 payload 只能是
    `unknown`，按 `type` 判别的联合类型当场失效，而那正是这一版相对参考
    实现的关键改进。

    注意"摊平"说的是**信封这一层**。载荷里的领域对象（thought/evidence/span…）
    是要嵌在自己的键下的，理由见 `test_events.py` 的模块 docstring 与
    `contracts/sse_events.json`；这一条只断言那一层 `payload` 不存在。
    """
    event = journal.publish("thought", {"thought": {"text": "看这里"}})
    body = event.payload()

    assert "payload" not in body
    assert body["thought"]["text"] == "看这里"
    assert body["seq"] == 1
    assert body["type"] == "thought"
    assert body["taskId"] == "TK-test"
    assert body["createdAt"]


def test_reserved_keys_are_only_checked_at_the_envelope_level(journal: EventJournal) -> None:
    """保留键只在**信封那一层**被拦。对象内部是自由的，且不会挤掉信封。

    这条边界就是"为什么非得嵌套"的答案本身：`Span` 的落库读取路径带
    `taskId`、决策回放要 `seq`，两者都是保留键。摊平的话 `publish()` 会
    直接抛错——这正是最初 `Tracer` 一绑上、流水线就在第一个阶段崩掉的
    原因。嵌一层之后它们只是对象里两个普通字段。

    同时确认**对象里的 `taskId` 不会覆盖信封里那一份**：载荷是用
    `{**envelope, **data}` 拼的，而 `data` 只有 `span` 这一个键。
    """
    event = journal.publish(
        "trace",
        {"span": {"spanId": "SP-00001", "taskId": "TK-别的任务", "seq": 3}},
    )
    body = event.payload()

    assert body["span"]["taskId"] == "TK-别的任务"
    assert body["span"]["seq"] == 3
    assert body["taskId"] == "TK-test"
    assert body["seq"] == 1

    with pytest.raises(ValueError, match="taskId"):
        journal.publish("trace", {"taskId": "TK-别的任务"})


def test_sse_frame_carries_an_id_line(journal: EventJournal) -> None:
    """`id:` 行是断线续传的全部秘密。

    没有它，`EventSource` 照样会自动重连，但接上的是一个有洞的流——
    而且不报错，只是工作台上少几段思维。
    """
    frame = journal.publish("thought", {"text": "一"}).to_sse()

    assert frame.startswith("id: 1\nevent: thought\ndata: ")
    assert frame.endswith("\n\n")


def test_sse_data_line_is_valid_json_with_unicode_kept(journal: EventJournal) -> None:
    """中文不能被转义成 `\\uXXXX`：帧大小会膨胀三倍，而这是逐条推的流。"""
    frame = journal.publish("thought", {"text": "中文"}).to_sse()
    data_line = next(line for line in frame.splitlines() if line.startswith("data: "))

    assert "中文" in data_line
    assert json.loads(data_line[len("data: "):])["text"] == "中文"


def test_event_types_match_the_frontend_contract() -> None:
    """后端的事件类型集合必须与 `frontend/src/types/events.ts` 一致。

    这里断言集合相等而不是顺序相同：**两边现在的顺序确实不同**
    （后端把 `progress` 排在第二位，前端排在第七位）。集合不等说明
    有一边新增或删掉了一种事件，那一定会让另一边的 switch 走到
    未定义分支上。
    """
    frontend = {
        "node_update", "progress", "thought", "message", "evidence",
        "trace", "chart", "image", "report_ready", "done", "error",
    }
    assert set(EVENT_TYPES) == frontend
    assert len(EVENT_TYPES) == len(set(EVENT_TYPES))


# ============================================================
# 消费者
# ============================================================


def test_every_sink_receives_every_event() -> None:
    seen_a: list[int] = []
    seen_b: list[int] = []

    journal = EventJournal("TK-1", sink=lambda e: seen_a.append(e.seq))
    journal.add_sink(lambda e: seen_b.append(e.seq))
    journal.publish("thought", {"text": "一"})
    journal.publish("thought", {"text": "二"})

    assert seen_a == [1, 2]
    assert seen_b == [1, 2]


def test_one_failing_sink_does_not_starve_the_other() -> None:
    """落库失败不该导致任务行不更新，反之亦然。

    这是把 `_sink` 从单个回调改成回调**列表**的全部理由。
    """
    delivered: list[int] = []

    def broken(_event) -> None:
        raise RuntimeError("消费者炸了")

    journal = EventJournal("TK-1", sink=broken)
    journal.add_sink(lambda e: delivered.append(e.seq))

    event = journal.publish("thought", {"text": "一"})

    assert delivered == [1]
    assert event.seq == 1


def test_add_sink_is_idempotent() -> None:
    """同一个回调被挂两次时只算一次，否则事件会被重复处理。"""
    calls: list[int] = []

    def sink(event) -> None:
        calls.append(event.seq)

    journal = EventJournal("TK-1")
    journal.add_sink(sink)
    journal.add_sink(sink)
    journal.publish("thought", {"text": "一"})

    assert calls == [1]


def test_make_journal_returns_the_same_journal_for_one_task() -> None:
    """两个标签页打开同一个任务时，必须拿到**同一个** journal。

    各自新建一个的话，后开的那个只有从它创建之后的事件，
    前一半思维流凭空消失。这里断言的是行为（后挂的消费者也能收到
    此前发布的事件），而不是"返回了同一个对象"——后者在
    "新建了一个但把旧事件复制过去"的实现下也会通过。
    """
    first = make_journal("TK-1")
    received: list[int] = []
    second = make_journal("TK-1", sink=lambda e: received.append(e.seq))

    assert first is second
    second.publish("thought", {"text": "一"})

    assert received == [1]


def test_journals_are_independent_per_task() -> None:
    a = make_journal("TK-1")
    b = make_journal("TK-2")
    a.publish("thought", {"text": "属于 1"})
    b.publish("thought", {"text": "属于 2"})

    assert a.all()[0].task_id == "TK-1"
    assert b.all()[0].task_id == "TK-2"


# ============================================================
# seq
# ============================================================


def test_seq_starts_at_one_per_task_not_globally() -> None:
    """回归用例：seq 曾经是模块级全局计数器。

    两个并发任务的序号交错之后，"第 40 秒发生了什么"就不可回答了，
    而决策回放的滑杆恰恰按 seq 排序。
    """
    a = EventJournal("TK-1")
    b = EventJournal("TK-2")

    assert a.publish("thought", {}).seq == 1
    assert b.publish("thought", {}).seq == 1
    assert a.publish("thought", {}).seq == 2


def test_since_returns_events_strictly_after_the_cursor() -> None:
    journal = EventJournal("TK-1")
    for i in range(4):
        journal.publish("thought", {"text": str(i)})

    assert [e.seq for e in journal.since(2)] == [3, 4]
    assert [e.seq for e in journal.since(0)] == [1, 2, 3, 4]


class TestMaxKeep:
    def test_oldest_events_are_dropped(self) -> None:
        """内存日志只是"最近发生了什么"的缓存，完整的续传靠 `task_events` 表。"""
        journal = EventJournal("TK-1", max_keep=3)
        for i in range(5):
            journal.publish("thought", {"text": str(i)})

        assert [e.seq for e in journal.all()] == [3, 4, 5]
        assert journal.oldest_seq() == 3

    def test_last_seq_survives_the_trim(self) -> None:
        """seq 不能被裁剪回退：回退会让新事件的 seq 与已发过的重复，
        前端的去重逻辑会把它当旧事件丢掉。"""
        journal = EventJournal("TK-1", max_keep=2)
        for _ in range(5):
            journal.publish("thought", {})

        assert journal.last_seq == 5
        assert journal.publish("thought", {}).seq == 6


# ============================================================
# 恢复
# ============================================================


def _row(seq: int, task_id: str = "TK-1", type_: str = "thought") -> dict:
    return {
        "seq": seq,
        "task_id": task_id,
        "type": type_,
        "data": {"text": str(seq)},
        "created_at": "2026-01-01T00:00:00.000+00:00",
    }


class TestHydrate:
    def test_continues_numbering_from_the_persisted_maximum(self) -> None:
        """重启后 seq 必须接着最大值往下走，不能从 1 重新开始。

        从头编号会让前端的 `seq <= lastSeq` 去重把整段新事件当重复丢掉——
        表现是"重启之后工作台不再更新"，而服务端日志里一切正常。
        """
        journal = EventJournal("TK-1")
        journal.hydrate([_row(7), _row(8)])

        assert journal.last_seq == 8
        assert journal.publish("thought", {"text": "新的"}).seq == 9

    def test_is_idempotent(self) -> None:
        journal = EventJournal("TK-1")
        rows = [_row(1), _row(2)]
        journal.hydrate(rows)
        journal.hydrate(rows)

        assert [e.seq for e in journal.all()] == [1, 2]

    def test_accepts_rows_out_of_order(self) -> None:
        """`list_since` 的返回顺序不该被依赖：表里存的是 seq，
        而 SQL 没有 ORDER BY 时不保证顺序。"""
        journal = EventJournal("TK-1")
        journal.hydrate([_row(3), _row(1), _row(2)])

        assert [e.seq for e in journal.all()] == [1, 2, 3]

    def test_ignores_rows_belonging_to_another_task(self) -> None:
        """串任务的后果不是"多了一条事件"，而是**把别人的思维流
        混进这个任务的回放里**，看起来像是它自己产生的。"""
        journal = EventJournal("TK-1")
        journal.hydrate([_row(1), _row(2, task_id="TK-2")])

        assert [e.seq for e in journal.all()] == [1]

    def test_hydrated_events_are_served_to_subscribers(self) -> None:
        """恢复之后新订阅者要能拿到历史——这正是"刷新页面不丢状态"。"""
        journal = EventJournal("TK-1")
        journal.hydrate([_row(1), _row(2)])

        assert [f.splitlines()[0] for f in journal.iter_sse(0)] == ["id: 1", "id: 2"]


# ============================================================
# 订阅
# ============================================================


async def test_subscribe_replays_backlog_then_hands_over_to_live(journal: EventJournal) -> None:
    """先补发历史，再接上实时——断线续传的全部行为。

    混合阶段最容易出错：注册订阅者与读取历史之间若不在同一次加锁里，
    那一段发布的事件两头都不在（既不在读到的历史里，也不在队列里）。
    """
    for i in range(3):
        journal.publish("thought", {"text": str(i)})

    seen: list[int] = []
    async for event in journal.subscribe(from_seq=1):
        seen.append(event.seq)
        if event.seq == 3:
            journal.publish("thought", {"text": "实时"})
        if event.seq == 4:
            break

    assert seen == [2, 3, 4]


async def test_subscribe_from_the_tip_gets_no_backlog(journal: EventJournal) -> None:
    """`from_seq = 当前最大 seq` 时不补发历史。

    这是前端重连走的那条路：`Last-Event-ID` 就是它已经收到的最后一条，
    所以它要的是"从我断掉的地方往后"，而不是再收一遍全部。
    """
    journal.publish("thought", {"text": "旧"})  # seq 1

    async def feed() -> None:
        journal.publish("thought", {"text": "新"})  # seq 2

    feeder = asyncio.create_task(feed())
    seen: list[int] = []
    async for event in journal.subscribe(from_seq=1):
        seen.append(event.seq)
        if event.seq == 2:
            break
    await feeder

    assert seen == [2]


async def test_close_ends_every_subscription(journal: EventJournal) -> None:
    """`close()` 必须在最后调：调晚了 SSE 连接会一直挂着，
    浏览器看到的是一条**永远不结束的流**——它不报错，只是不再有内容，
    前端于是既不显示"已完成"也不重连。"""
    journal.publish("thought", {"text": "一"})
    journal.close()

    assert [e.seq async for e in journal.subscribe(0)] == [1]
    assert journal.closed is True


async def test_subscribing_after_close_still_replays_history(journal: EventJournal) -> None:
    """跑完之后才打开页面（看历史报告）也要能拿到事件，不能卡住。"""
    journal.publish("thought", {"text": "一"})
    journal.close()

    seen: list[int] = []
    async for event in journal.subscribe(0):
        seen.append(event.seq)

    assert seen == [1]


async def _drain(journal: EventJournal) -> list[int]:
    """把一条订阅读到流结束。

    读到结束而不是读到某个条数：按条数读的写法要假设"发布一定发生在
    两个订阅者都注册之后"，那个假设只能靠 `sleep` 去凑，而靠 sleep
    凑出来的顺序在慢机器上会变成偶发失败——偶发失败的测试最后一定会
    被跳过。这里改由发布方负责关闭流，读到结束是确定性的。
    """
    return [event.seq async for event in journal.subscribe(0)]


async def test_two_subscribers_both_see_the_same_stream(journal: EventJournal) -> None:
    """工作台开两个标签页时，两边的思维流必须一致。

    每个订阅者一个 `asyncio.Queue`，扇出不能是"谁先抢到算谁的"。
    """
    async def feed() -> None:
        journal.publish("thought", {"text": "一"})
        journal.publish("thought", {"text": "二"})
        journal.close()

    feeder = asyncio.create_task(feed())
    first, second = await asyncio.gather(_drain(journal), _drain(journal))
    await feeder

    assert first == second == [1, 2]


async def test_concurrent_publishers_never_punch_a_hole_in_the_stream(
    journal: EventJournal,
) -> None:
    """多线程发布时，订阅者收到的 seq 必须是**连续的**，一个都不能少。

    这是踩过的一个坑，也是这个文件里最不显然的一条。订阅端的去重是一个
    单调水位（`seq <= 水位` 就跳过），它假设投递是单调的。而入队这一步
    原本在锁外：两个线程可以各自拿到 seq 31 与 32，再按**相反的顺序**
    调 `call_soon_threadsafe`，队列里就成了 [32, 31]。订阅者收到 32 之后
    水位推到 32，随后的 31 被判成重复**静默丢掉**。

    为什么这个丢法特别坏：它不可恢复。前端的水位已经推到 32，
    重连时按 `Last-Event-ID: 32` 补发的是 `since(32)`，31 永远拿不回来。
    而"跑到一半断网 10 秒，恢复后无缺口"正是这套东西的验收条件之一。

    为什么这里要动 `setswitchinterval`
    ----------------------------------
    第一版这条测试**抓不住**这个 bug：8 个线程各发 25 条，对着锁外那份
    实现跑 5 次全绿。原因是 CPython 默认 5ms 才切一次线程，而
    "释放锁"到"入队"之间只有两条语句——线程一口气就能发完 25 条，
    那个窗口压根轮不上被抢占。**一条抓不住 bug 的测试比没有测试更坏**：
    它会让人以为这块已经被守住了。

    把切换间隔压到 1 微秒，抢占就落在那两条语句之间的概率大幅上升，
    窗口才真的被打开。这不是"为了让它红而调参"：跑在**修好的**实现上时，
    这个加严只让锁竞争更激烈，而锁内的不变量在任何调度下都成立。

    注意这里断言的是**丢了没丢**，不断言顺序。顺序断言是恒真的——
    水位去重会把任何回退的条目丢掉，于是 `seen` 必然递增。**"看不到乱序"
    恰恰是这个 bug 静默的原因**：乱序不表现成乱序，表现成缺失。
    """
    async def feed() -> None:
        def burst() -> None:
            for i in range(25):
                journal.publish("thought", {"text": str(i)})

        # 线程池而不是 `to_thread` 逐条来：要的就是多条同时压在 publish 上。
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: burst(), range(8)))
        journal.close()

    original = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        feeder = asyncio.create_task(feed())
        seen = await _drain(journal)
        await feeder
    finally:
        sys.setswitchinterval(original)

    missing = sorted(set(range(1, 201)) - set(seen))
    assert not missing, (
        f"订阅者少收了 {len(missing)} 条事件，前几个是 {missing[:5]}。"
        f"这几乎可以肯定是入队跑到了锁外：seq 的分配与入队不再原子，"
        f"两个线程就能按相反顺序入队，而水位去重会静默吃掉回退的那条。"
    )
