"""`/api/tasks` 生命周期与 SSE 事件流。

这个文件里最重要的不是"接口返回了 200"，而是**两条不可能靠读代码发现的规矩**：

1. `GET /api/tasks/{id}/stream` 永远不启动流水线。参考实现就是在这里
   把 `run_pipeline` 直接写进处理函数的，后果是断线自动重连＝重跑一次任务，
   而账单翻倍、日志干净。这条规矩必须被一条测试按住——
   将来有人为了"让 stream 也能自动开跑"而加一行 `ensure_runner(..., start=True)`，
   下面那条用例会立刻变红。
2. `Last-Event-ID` 优先于 `?from_seq=`。反直觉，但重连时浏览器给的
   是它记着的水位，而 URL 可能还带着 `from_seq=0`；查询参数优先的话，
   每次断线都要重传全部历史。
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.api.routes_tasks import _resume_from
from app.core.config import get_settings
from app.core.models import TaskRecord
from app.core.observability.events import PipelineEvent
from app.core.pipeline.runner import (
    AWAITING_CLARIFY,
    drop_runner,
    ensure_runner,
    get_runner,
    new_task_id,
)
from app.db.repo import events as events_repo
from app.db.repo import tasks as tasks_repo
from app.main import app

#: 明显含糊的一句话需求（`needs_clarification` 靠启发式判定）。
VAGUE_QUERY = "帮我看看那个笔记软件"

#: 说清楚了对象、也没有含糊措辞的需求。
CLEAR_QUERY = "对比 Notion 与 Obsidian 的定价与协作能力差异"


@pytest.fixture
def client(mock_pipeline_db):
    """三个 provider 都是 mock 的测试客户端。**零网络、零花费。**"""
    with TestClient(app) as c:
        yield c


def _make_row(query: str = CLEAR_QUERY, status: str = "running") -> str:
    """直接建一行任务，**不起 runner**。用来模拟"进程重启后的残局"。"""
    task_id = new_task_id()
    tasks_repo.create(TaskRecord(task_id=task_id, query=query, mode="quick", status=status))
    return task_id


# ============================================================
# SSE 是纯读接口
# ============================================================


class TestStreamNeverStartsThePipeline:
    def test_a_get_on_a_stale_task_does_not_start_it(self, client):
        """**这条守的是参考实现最严重的那个问题。**

        库里有一行"正在跑"的任务，但内存里没有对应的 runner——
        这正是服务重启之后的样子。此时一个 GET 不应该把它跑起来：
        `EventSource` 断线会自动重连，一次网络抖动就是一次重跑。
        """
        task_id = _make_row(status="running")

        response = client.get(f"/api/tasks/{task_id}/stream")

        assert response.status_code == 200
        # 没有 runner 被建出来 —— 也就没有流水线被启动。
        assert get_runner(task_id) is None
        # 事件一条都不该有：事件只可能由跑起来的流水线产生。
        assert events_repo.count(task_id) == 0

    def test_it_replays_what_is_already_in_the_database(self, client):
        """库里已经有的事件要能补发出来——这是"刷新页面不丢历史"的实现。"""
        task_id = _make_row(status="done")
        for index in range(3):
            events_repo.append(
                PipelineEvent(
                    seq=index + 1, task_id=task_id, type="progress",
                    data={"stage": "collect", "progress": 0.1 * (index + 1)},
                    created_at="2026-01-01T00:00:00.000+00:00",
                )
            )

        body = client.get(f"/api/tasks/{task_id}/stream").text

        assert "id: 1" in body and "id: 3" in body
        assert body.count("event: progress") == 3
        # 回放是纯读的：**依然**没有 runner。
        assert get_runner(task_id) is None

    def test_the_replay_honours_the_resume_point(self, client):
        """补发从水位之后开始，不是从头发。"""
        task_id = _make_row(status="done")
        for index in range(3):
            events_repo.append(
                PipelineEvent(
                    seq=index + 1, task_id=task_id, type="progress",
                    data={"progress": 0.1 * (index + 1)},
                    created_at="2026-01-01T00:00:00.000+00:00",
                )
            )

        body = client.get(f"/api/tasks/{task_id}/stream?from_seq=2").text

        assert "id: 1" not in body
        assert "id: 3" in body
        assert body.count("event: progress") == 1

    def test_an_unknown_task_is_404(self, client):
        assert client.get("/api/tasks/TK-nope/stream").status_code == 404


class TestResumePointPrecedence:
    """`_resume_from` 是纯函数，所以可以直接对着它断言。

    走 HTTP 测的话要构造"同时带头和查询参数"的请求，能测但绕；
    这条规则本身只有三个分支，直接测它更清楚。
    """

    def test_last_event_id_wins_over_from_seq(self):
        """浏览器重连时的真实情形：头里是水位，URL 里还挂着 `from_seq=0`。

        查询参数优先的话，每次断线都要重传一遍完整历史。
        """
        assert _resume_from("32", 0) == 32

    def test_no_header_falls_back_to_from_seq(self):
        assert _resume_from("", 7) == 7

    def test_a_malformed_header_falls_back_instead_of_restarting(self):
        """畸形水位退回查询参数，**不是**退回 0。

        退回 0 意味着重发全部历史，而一个读不懂的值更可能是
        "客户端记错了"，不是"客户端什么都要"。
        """
        assert _resume_from("abc", 5) == 5
        assert _resume_from("", 0) == 0

    def test_a_negative_header_is_clamped(self):
        assert _resume_from("-3", 0) == 0


# ============================================================
# 建任务
# ============================================================


class TestCreateTask:
    def test_a_clear_query_does_not_stop_for_clarification(self, client):
        body = client.post("/api/tasks", json={"query": CLEAR_QUERY, "mode": "quick"}).json()

        assert body["awaitingClarify"] is False
        assert body["needClarify"] is False
        assert body["clarifyQuestions"] == []
        # 必须是 `running` 而不是"随便哪个非澄清状态"。以前这里写的是
        # `("running", "done")`，于是"POST 等到整条流水线跑完才返回"
        # 这个缺陷从来没被看见——它每次都落进 `done` 那一支。
        assert body["status"] == "running"
        assert body["taskId"].startswith("TK-")

    def test_a_vague_query_stops_and_returns_questions(self, client):
        """含糊的需求要**停下来问**，而且问题要随响应一起给出来——
        不然澄清页无事可做。"""
        body = client.post("/api/tasks", json={"query": VAGUE_QUERY, "mode": "quick"}).json()

        assert body["status"] == AWAITING_CLARIFY
        assert body["awaitingClarify"] is True
        assert body["needClarify"] is True
        assert body["clarifyQuestions"], "停了却没有任何问题，澄清页会是一片空白"
        first = body["clarifyQuestions"][0]
        assert {"id", "question", "kind", "options", "recommended"} <= set(first)
        # 调研对象与候选品牌也一并给出来：用户要凭它判断"这问题问的是不是我要的东西"。
        assert body["subject"]
        assert body["brands"]

    def test_it_stops_before_collecting_anything(self, client):
        """停在澄清处时**一条证据都不该采**——这正是"澄清放在最前面"的意义：
        问错了再补采，钱已经花了。"""
        task_id = client.post(
            "/api/tasks", json={"query": VAGUE_QUERY, "mode": "quick"}
        ).json()["taskId"]

        assert get_runner(task_id).context.evidences == []
        types = {row["type"] for row in events_repo.list_since(task_id, 0)}
        assert "evidence" not in types
        # 而且这些事件**已经落库**了：刷新页面还能看到需求理解那一段。
        assert "thought" in types

    def test_auto_clarify_does_not_stop(self, client):
        """自动模式采用推荐项，于是不暂停——但它**必须留痕**，
        不能让人以为这次调研是人工确认过的。"""
        body = client.post(
            "/api/tasks",
            json={"query": VAGUE_QUERY, "mode": "quick", "autoClarify": True},
        ).json()

        assert body["awaitingClarify"] is False
        assert body["status"] in ("running", "done")
        runner = get_runner(body["taskId"])
        assert runner.context.clarify_answers, "自动模式应当填上推荐项"
        assert any("澄清" in block for block in runner.context.degraded_blocks)

    def test_an_unknown_mode_falls_back_to_the_default(self, client):
        """档位是用户输入，拼错一个词不该让整个任务失败。"""
        body = client.post(
            "/api/tasks", json={"query": CLEAR_QUERY, "mode": "quiick"}
        ).json()

        assert body["mode"] == "deep"

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_a_blank_query_is_rejected(self, client, bad):
        assert client.post("/api/tasks", json={"query": bad}).status_code == 422


# ============================================================
# 澄清
# ============================================================


class TestClarify:
    def test_answers_resume_the_task(self, client):
        created = client.post(
            "/api/tasks", json={"query": VAGUE_QUERY, "mode": "quick"}
        ).json()
        task_id = created["taskId"]
        questions = created["clarifyQuestions"]

        body = client.post(
            f"/api/tasks/{task_id}/clarify",
            json={"answers": {q["id"]: "随便选一个" for q in questions}},
        ).json()

        assert body["awaitingClarify"] is False
        assert body["status"] == "running"

    def test_the_answers_reach_the_pipeline(self, client):
        """答案要真的传下去。只把它记在响应里、不进上下文的话，
        界面显示"已按你的选择调研"，而跑的还是默认假设。"""
        created = client.post(
            "/api/tasks", json={"query": VAGUE_QUERY, "mode": "quick"}
        ).json()
        task_id = created["taskId"]
        answers = {q["id"]: "我的回答" for q in created["clarifyQuestions"]}

        client.post(f"/api/tasks/{task_id}/clarify", json={"answers": answers})

        assert get_runner(task_id).context.clarify_answers == answers

    def test_clarifying_a_running_task_is_a_conflict(self, client):
        """已经跑起来的任务收到答案时，正确答案是**报错**，
        不是"收下但不用"——后者会让人以为自己的选择生效了。"""
        task_id = client.post(
            "/api/tasks", json={"query": CLEAR_QUERY, "mode": "quick"}
        ).json()["taskId"]

        response = client.post(f"/api/tasks/{task_id}/clarify", json={"answers": {"q1": "x"}})

        assert response.status_code == 409
        assert "不在等澄清" in response.json()["detail"]

    def test_clarifying_an_unknown_task_is_404(self, client):
        response = client.post("/api/tasks/TK-nope/clarify", json={"answers": {}})

        assert response.status_code == 404

    def test_partial_answers_are_accepted(self, client):
        """用户对某个问题没有偏好时可以空着——空着比瞎选一个诚实。"""
        task_id = client.post(
            "/api/tasks", json={"query": VAGUE_QUERY, "mode": "quick"}
        ).json()["taskId"]

        response = client.post(f"/api/tasks/{task_id}/clarify", json={"answers": {}})

        assert response.status_code == 200


# ============================================================
# 快照：两个容易混的字段
# ============================================================


class TestSnapshot:
    def test_need_clarify_and_awaiting_clarify_are_different_things(self, client):
        """`needClarify` 是事实（这个需求信息不够），`awaitingClarify` 是状态
        （现在正停着等回答）。答完之后前者仍然为真，后者必须为假——
        混成一个字段的话，一个早就跑完的任务会在每次打开时把人拉回澄清页。
        """
        created = client.post(
            "/api/tasks", json={"query": VAGUE_QUERY, "mode": "quick"}
        ).json()
        task_id = created["taskId"]
        assert (created["needClarify"], created["awaitingClarify"]) == (True, True)

        client.post(f"/api/tasks/{task_id}/clarify", json={"answers": {"q1": "x"}})
        after = client.get(f"/api/tasks/{task_id}").json()

        assert after["needClarify"] is True
        assert after["awaitingClarify"] is False

    def test_a_refresh_still_sees_the_questions(self, client):
        """澄清页刷新之后要能原样重建那几个问题——
        否则任务停在那儿等一个用户已经看不到的问题。"""
        created = client.post(
            "/api/tasks", json={"query": VAGUE_QUERY, "mode": "quick"}
        ).json()

        after = client.get(f"/api/tasks/{created['taskId']}").json()

        assert after["awaitingClarify"] is True
        assert [q["id"] for q in after["clarifyQuestions"]] == [
            q["id"] for q in created["clarifyQuestions"]
        ]
        assert after["subject"] == created["subject"]

    def test_a_restart_still_sees_the_questions(self, client):
        """**进程重启过**的情况：内存里没有 runner，问题只能从任务行读。

        漏掉这条的症状是"任务说在等回答，但屏幕上一个问题都没有"——
        而它偏偏是最难碰到的那种情况（要重启一次才出现）。
        """
        created = client.post(
            "/api/tasks", json={"query": VAGUE_QUERY, "mode": "quick"}
        ).json()
        task_id = created["taskId"]
        drop_runner(task_id)  # 模拟进程重启
        assert get_runner(task_id) is None

        after = client.get(f"/api/tasks/{task_id}").json()

        assert after["awaitingClarify"] is True
        assert after["clarifyQuestions"], "重启之后问题丢了，澄清页无话可说"

    def test_an_unknown_task_is_404(self, client):
        assert client.get("/api/tasks/TK-nope").status_code == 404


# ============================================================
# 重启之后的 seq
# ============================================================


class TestSeqAfterRestart:
    """journal 的 seq 要从库里接着编，而不是从 1 重来。

    ⚠️ 这两条用例原来**一个夹具都没有**（问题 28）
    --------------------------------------------
    `_make_row()` 直接走 `tasks_repo`，而没有夹具时 `get_settings().db_file`
    是 `backend/data/xm3.db`——也就是**生产库**。所以这两条用例一直在干两件事：

    1. 往真库里插两条 `running` 的假任务（一问一对，同一个 `new_task_id()`
       格式、同一个 `CLEAR_QUERY`）。真库里那 100 多条"僵尸任务"
       有一大半就是这么来的，不是谁点出来的。
    2. 它们插进去的行又会被下一次跑测试时 lifespan 的
       `interrupt_unfinished()` 判死——于是真库里出现一批
       "创建于同一秒、失败于同一秒"的成对任务，失败原因写着
       "后端进程重启时这个任务还没跑完"。

    现在由 `conftest.py` 的 `_never_touch_production_db` 兜住；
    这里显式挂上 `mock_pipeline_db`，因为这两条**确实需要一张建好表的库**
    （`events_repo.append` 要 `task_events` 表）。
    """

    def test_the_journal_seq_continues_instead_of_restarting_at_one(self, mock_pipeline_db):
        """**这条守的是一个很安静的故障。**

        `task_events` 的主键是 `(task_id, seq)`。进程重启后新建的 journal
        从 0 开始编号，于是接下来的事件叫 1、2、3……直接与库里已有的行撞车。
        表现极其隐蔽：流水线照常跑、SSE 照常推，只有"刷新页面看历史"
        会少一截，因为那些事件根本没写进库。
        """
        task_id = _make_row(status="running")
        for index in range(3):
            events_repo.append(
                PipelineEvent(
                    seq=index + 1, task_id=task_id, type="progress",
                    data={"progress": 0.1}, created_at="2026-01-01T00:00:00.000+00:00",
                )
            )
        drop_runner(task_id)

        runner = ensure_runner(task_id, CLEAR_QUERY, mode_key="quick", start=False)

        assert runner.journal.last_seq == 3, "没有把 seq 接上库里的最大值"
        # 再发一条，落到 4——而不是 1。撞车的话这句会抛 IntegrityError。
        fresh = runner.journal.publish("progress", {"progress": 1.0})
        assert fresh.seq == 4
        events_repo.append(fresh)
        assert events_repo.count(task_id) == 4

    def test_a_task_with_no_history_is_not_touched(self, mock_pipeline_db):
        """没有历史事件时不该去读库——而且 seq 必须还是从 1 开始。"""
        task_id = _make_row(status="running")
        drop_runner(task_id)

        runner = ensure_runner(task_id, CLEAR_QUERY, mode_key="quick", start=False)

        assert runner.journal.last_seq == 0
        assert runner.journal.publish("progress", {}).seq == 1


# ============================================================
# 启动就绪：建表
# ============================================================


def _tables(db_path) -> set[str]:
    """直接开一条新连接读表名。

    刻意不用 `get_conn()`：那是线程局部且带缓存的，而这里要问的是
    "**库文件**里现在有什么"，不是"这条连接以为有什么"。
    """
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()


@pytest.fixture
def virgin_db(tmp_path, monkeypatch):
    """一个**没有跑过迁移**的空库，provider 全 mock。

    刻意不调 `migrate()`——这正是这个夹具存在的理由。别的夹具
    （`mock_pipeline_db`）自己建了表，所以测试全绿，而真实服务器
    在第一个读库的请求上 500。要复现那个状态，就得有一个不建表的夹具。
    """
    db_path = tmp_path / "virgin.db"
    for key, value in {
        "DB_PATH": str(db_path),
        "LLM_PROVIDER": "mock",
        "SEARCH_PROVIDER": "mock",
        "FETCH_PROVIDER": "mock",
        "CASSETTE_MODE": "off",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()

    from app.db.connection import close_all, reset_connections

    reset_connections()
    close_all()
    yield db_path
    close_all()
    reset_connections()


class TestStartupMigratesTheDatabase:
    """**应用启动时自己建表，不靠"开发者记得先跑一次迁移脚本"。**

    漏掉这一步的表现极具迷惑性：`/health` 是好的（它不碰库），日志干净，
    然后第一个真正读库的请求 500。`/api/providers` 更坏——它的今日成本
    查询吞掉异常返回 0，于是成本栏永远显示"今天 $0"，看起来一切正常。
    """

    def test_the_tables_exist_after_the_app_starts(self, virgin_db):
        # 先断言前置条件。不写这一句的话，将来有人往夹具里补一句
        # `migrate()`，这条用例会因为"表已经有了"而永远变绿，
        # 而它守的那个缺陷又回来了——一条只会绿、不会红的守卫比没有更坏。
        assert "tasks" not in _tables(virgin_db), "夹具自己不建表才有意义"

        with TestClient(app):
            pass

        assert {"tasks", "task_events", "reports", "evidences", "traces"} <= _tables(virgin_db)

    def test_a_task_can_be_created_on_a_fresh_database(self, virgin_db):
        """端到端的说法：全新库上，第一个真正读库的请求必须成功。"""
        with TestClient(app) as client:
            response = client.post(
                "/api/tasks", json={"query": CLEAR_QUERY, "mode": "quick"}
            )

        assert response.status_code == 201, response.text


# ============================================================
# 建任务在这一刻就返回
# ============================================================


class TestCreateReturnsWithoutWaitingForTheWholePipeline:
    """`POST /api/tasks` 等的是**需求理解**，不是整份报告。

    这条守的是一个"能跑，但你不会想用"的缺陷：唤醒事件原本只在整条
    流水线的结尾 `set()`，而它的 docstring 写的是"等需求理解结束"。
    于是建任务的请求要等到报告写完才响应——真实配置下是几分钟，
    用户盯着一个转圈的按钮，而工作台本该在这时候开始显示它在想什么。

    mock provider 跑完整条流水线只要几毫秒，所以测试看不出任何异常；
    这跟"注释和代码说的不是一回事"是同一个问题的两种表现。
    """

    def test_post_returns_while_the_pipeline_is_still_running(self, client, monkeypatch):
        gate = threading.Event()
        reached = threading.Event()

        async def _blocked_collect(ctx, **kwargs):
            # 卡在**需求理解之后**的第一个阶段上：此时"该不该暂停"已经
            # 有结论了，所以建任务请求没有任何理由再等下去。
            reached.set()
            # 5 秒足够说明问题：正常路径下这条请求几十毫秒就回来了，
            # 而缺陷在的时候它要等到这个超时为止。反证脚本会走那条路，
            # 所以这个数字同时是"失败一次"的代价。
            await asyncio.to_thread(gate.wait, 5.0)

        monkeypatch.setattr("app.core.pipeline.collect.run", _blocked_collect)

        try:
            body = client.post(
                "/api/tasks", json={"query": CLEAR_QUERY, "mode": "quick"}
            ).json()
        finally:
            gate.set()

        assert reached.is_set(), "流水线没跑到采集阶段，这个用例什么也没测到"
        # 关键断言：请求回来时任务**还在跑**。缺陷在的时候这里是 "done"。
        assert body["status"] == "running", body
        assert body["awaitingClarify"] is False

        # 别把还在跑的流水线留给下一个用例：它会在这个用例的夹具正在
        # 关连接的时候继续写库，而报错出现在**别的**用例的日志里。
        _wait_for_terminal(body["taskId"])

    def test_the_pause_reaches_the_database_before_the_caller_wakes(self, client):
        """停下来这件事要**同时**落在响应和库里。

        响应是对的、库里那一行却是 `running`，是很可能的写法（先返回、
        再异步补写库），而它的后果在刷新页面时才出现：`GET /api/tasks/{id}`
        读的是库，于是刚问过你问题的那一页刷新之后变成了一个空转的工作台。

        响应里的三个字段（`status` / `awaitingClarify` / `clarifyQuestions`）
        和库里那两行是同一件事的两种呈现，必须一起为真。
        """
        body = client.post(
            "/api/tasks", json={"query": VAGUE_QUERY, "mode": "quick"}
        ).json()

        assert body["status"] == AWAITING_CLARIFY
        assert body["awaitingClarify"] is True
        assert body["clarifyQuestions"], "停了却没有问题，澄清页是一片空白"
        # 库里那一行也要一致：刷新页面走的是它，不是内存里的 runner。
        record = tasks_repo.get(body["taskId"])
        assert record.status == AWAITING_CLARIFY
        assert record.need_clarify == 1
        assert record.clarify_questions == body["clarifyQuestions"]


def _wait_for_terminal(task_id: str, timeout: float = 15.0) -> str:
    """等后台那条流水线自己跑完，返回终态。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runner = get_runner(task_id)
        status = runner.snapshot()["status"] if runner is not None else ""
        if status in ("done", "failed", "cancelled"):
            return status
        time.sleep(0.02)
    raise AssertionError(f"任务 {task_id} 在 {timeout}s 内没有结束")
