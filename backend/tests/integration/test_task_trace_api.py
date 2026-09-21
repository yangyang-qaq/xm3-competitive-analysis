"""`GET /api/tasks/{id}/trace` —— 决策回放页与 trace 面板的数据源。

这个接口在阶段 8 之前**一条测试都没有**，而它身上挂着两个会静默出错的地方。

一、树必须不重不漏
----------------
`span_tree()` 里有一句容易写错的兜底：父 span 不在（被截掉、或跨进程）
就**当成根节点**而不是丢掉。反过来写——`if parent is not None: append`
——代码照样跑、页面照样出，只是**整棵子树消失**。
少几个节点在树形控件上看起来和"这次跑得比较少"一模一样。

所以断言的不是"返回了 200"，而是**树展开后的 spanId 集合与扁平列表逐条相等**。

二、两个耗时字段都不是"任务跑了多久"
--------------------------------
`spanDurationMs` 是所有 span 耗时**相加**，`spanWindowMs` 是它们盖住的墙钟窗口。
并发采集时前者远大于后者（实测 4.3 倍），所以任何一个都不能被当成端到端耗时——
那个数在 `done.metrics.durationMs` 里（**不叫 `elapsedMs`**——
那个名字属于 `node_update` 与 `runner.snapshot()`）。

这条不变量（`concurrency == 总占用 ÷ 窗口`）必须钉住：一旦有人把
`spanDurationMs` 改名回 `durationMs` 并接到页面的"耗时"上，
用户看到的会是一个比真实耗时大四倍的数字，而它不报错。

三、`degraded` 不是失败
---------------------
真实库里 249 条 span 有 24 条 `degraded`、`error` **0 条**。
曾经这里叫 `failedCount` 且把两者一起数——面板会印"24 次失败"，
而真相是"24 个页面抓到了但正文太薄"，那是采集的正常形态。
`Tracer.metrics()` 一直是分开数的，这里必须同口径。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.api.routes_tasks import _span_window_ms, _summarize_spans
from app.main import app


@pytest.fixture
def client(mock_pipeline_db):
    """provider 全是 mock 的测试客户端。**零网络、零花费。**"""
    with TestClient(app) as c:
        yield c


def _flatten(nodes: list[dict]) -> list[str]:
    """把树摊回一串 spanId（前序）。子节点都挂在 `children` 下。"""
    out: list[str] = []
    for node in nodes:
        out.append(node["spanId"])
        out.extend(_flatten(node.get("children") or []))
    return out


def _span(**overrides) -> dict:
    """一条形状完整的 span（camelCase，与 `_row_to_span` 的产出一致）。"""
    base = {
        "spanId": "SP-00001",
        "parentId": "",
        "kind": "llm",
        "name": "analyze_claims",
        "startedAt": "2026-09-18T14:47:26.874+00:00",
        "endedAt": "2026-09-18T14:47:30.000+00:00",
        "durationMs": 3126,
        "costUsd": 0.0012,
        "totalTokens": 900,
        "cachedPromptTokens": 0,
        "status": "ok",
    }
    base.update(overrides)
    return base


def _row(**overrides) -> dict:
    """入库用的形状（snake_case —— `_span_to_row` 读的是这些键）。"""
    base = {
        "span_id": "SP-00001",
        "parent_id": "",
        "kind": "stage",
        "name": "collect",
        "started_at": "2026-09-18T14:47:26.874+00:00",
        "ended_at": "2026-09-18T14:47:30.000+00:00",
        "duration_ms": 3126,
    }
    base.update(overrides)
    return base


# ============================================================
# 一、树
# ============================================================


class TestSpanTree:
    """**真跑出来的数据里没有一个 span 有父亲。**

    实测：mock 流水线一次跑出 49 条 span，`parentId` 全是 `""`；
    库里 1036 行**全部**如此。原因不神秘——全仓库没有一处
    `with span(...)` 嵌在另一处里面，所以 `current_span_id()` 永远取不到东西。

    于是 `span_tree()` 在当前数据上是个恒等函数，而"树"这个词是白说的。
    **一条只在退化数据上通过的测试证明不了任何事**，所以这里不靠流水线
    造嵌套，而是**直接把带父亲的 span 写进库**。这样 `children` 那一段
    才真的被走到，"父不在就丢弃"这个 bug 才会被抓到。
    """

    def _seed(self, task_id: str, rows: list[dict]) -> None:
        from app.db.repo import traces as traces_repo

        for row in rows:
            traces_repo.save_many(task_id, [row])

    def _task(self) -> str:
        from app.core.models import TaskRecord
        from app.core.pipeline.runner import new_task_id
        from app.db.repo import tasks as tasks_repo

        task_id = new_task_id()
        tasks_repo.create(
            TaskRecord(task_id=task_id, query="对比 Notion 与 Obsidian", mode="quick")
        )
        return task_id

    def test_父子关系真的被装成树(self, client):
        """三层：根 → 子 → 孙。装错的话 `children` 会是空的。"""
        task_id = self._task()
        self._seed(task_id, [
            _row(span_id="SP-00001", parent_id="", name="collect"),
            _row(span_id="SP-00002", parent_id="SP-00001", name="search"),
            _row(span_id="SP-00003", parent_id="SP-00002", name="fetch"),
        ])

        spans = client.get(f"/api/tasks/{task_id}/trace").json()["spans"]
        assert len(spans) == 1, "三个节点只该有一个根"
        root = spans[0]
        assert root["spanId"] == "SP-00001"
        assert [c["spanId"] for c in root["children"]] == ["SP-00002"]
        assert [c["spanId"] for c in root["children"][0]["children"]] == ["SP-00003"]

    def test_父亲不在库里时它自己当根_而不是消失(self, client):
        """父 span 被环形上限截掉、或跨进程写进来时会这样。

        这里刻意只写子、不写父。写错成"父不在就丢掉"的话，
        `spans` 会是空数组 —— 界面上看起来就是"这次什么都没跑"。
        """
        task_id = self._task()
        self._seed(task_id, [_row(span_id="SP-00007", parent_id="SP-00999", name="孤儿")])

        body = client.get(f"/api/tasks/{task_id}/trace").json()
        assert [s["spanId"] for s in body["spans"]] == ["SP-00007"]
        assert body["summary"]["spanCount"] == 1

    def test_自己当自己的父亲不会死循环(self, client):
        """`parent_id == span_id` 是脏数据，但折叠进树里会无限递归。"""
        task_id = self._task()
        self._seed(task_id, [_row(span_id="SP-00001", parent_id="SP-00001")])

        body = client.get(f"/api/tasks/{task_id}/trace").json()
        assert [s["spanId"] for s in body["spans"]] == ["SP-00001"]

    def test_一次真跑的树把span一条不落地装进去(self, client, run_mock_pipeline):
        """`spanCount` 数的是**扁平**列表，`spans` 是**树**。

        两个数出自同一次查询的两条路径，所以"树把每一条都装进去了"
        就等价于它们相等。少挂一条，这里立刻红。
        """
        outcome = asyncio.run(run_mock_pipeline())
        body = client.get(f"/api/tasks/{outcome.task_id}/trace").json()
        summary = body["summary"]

        ids = _flatten(body["spans"])
        assert len(ids) == len(set(ids)), f"有 span 被挂了两次：{sorted(ids)}"
        assert len(ids) == summary["spanCount"], (
            f"树上有 {len(ids)} 条，库里有 {summary['spanCount']} 条——"
            "有 span 在组装树的时候被丢掉了"
        )

    def test_真跑一次确实产生了span(self, client, run_mock_pipeline):
        """否则上面那条断言是 `0 == 0` 的空转。"""
        outcome = asyncio.run(run_mock_pipeline())
        body = client.get(f"/api/tasks/{outcome.task_id}/trace").json()
        assert body["summary"]["spanCount"] > 0
        assert body["summary"]["byKind"], "byKind 空说明 kind 没写进去"

    def test_未知任务是404(self, client):
        assert client.get("/api/tasks/TK-nope/trace").status_code == 404

    def test_没有span的任务给空树而不是报错(self, client):
        """任务存在但从没跑过（残留行、或刚建好就重启了）。"""
        task_id = self._task()
        response = client.get(f"/api/tasks/{task_id}/trace")
        assert response.status_code == 200
        body = response.json()
        assert body["spans"] == []
        assert body["summary"]["spanCount"] == 0
        # 空的时候也要是完整形状，否则前端要写一堆 `?? 0`
        assert body["summary"]["byKind"] == []
        assert body["summary"]["concurrency"] == 0.0


# ============================================================
# 二、两个耗时字段
# ============================================================


class TestDurationAccounting:
    def test_三个时间是三个不同的数_实测四倍并发(self):
        """构造一个并发场景：三条 10 秒的 span，同时开始。

        串行实现会给出 窗口 == 总占用；这里刻意让窗口只有一条那么长。
        """
        spans = [
            _span(
                spanId=f"SP-{i:05d}",
                startedAt="2026-09-18T14:47:26.000+00:00",
                endedAt="2026-09-18T14:47:36.000+00:00",
                durationMs=10_000,
            )
            for i in (1, 2, 3)
        ]
        summary = _summarize_spans(spans)

        assert summary["spanDurationMs"] == 30_000, "总占用是三条相加"
        assert summary["spanWindowMs"] == 10_000, "窗口只等于其中一条"
        assert summary["slowestMs"] == 10_000
        # 三个数互不相等 —— 谁被当成"耗时"都能在断言上分开
        assert summary["concurrency"] == 3.0

    def test_串行时并发度是一(self):
        spans = [
            _span(spanId="SP-00001", durationMs=5_000,
                  startedAt="2026-09-18T14:00:00.000+00:00", endedAt="2026-09-18T14:00:05.000+00:00"),
            _span(spanId="SP-00002", durationMs=5_000,
                  startedAt="2026-09-18T14:00:05.000+00:00", endedAt="2026-09-18T14:00:10.000+00:00"),
        ]
        summary = _summarize_spans(spans)
        assert summary["spanDurationMs"] == 10_000
        assert summary["spanWindowMs"] == 10_000
        assert summary["concurrency"] == 1.0

    def test_没有叫durationMs的字段(self):
        """名字就是那道闸。

        字段一旦叫 `durationMs`，接线的人会把它接到"耗时"上，
        而它比真实耗时大好几倍。改回去这条就红。
        """
        summary = _summarize_spans([_span()])
        assert "durationMs" not in summary
        assert "spanDurationMs" in summary and "spanWindowMs" in summary

    def test_每个kind里也不叫durationMs(self):
        summary = _summarize_spans([_span()])
        assert "durationMs" not in summary["byKind"][0]

    def test_空列表不炸也不除以零(self):
        summary = _summarize_spans([])
        assert summary["spanWindowMs"] == 0
        assert summary["concurrency"] == 0.0
        assert summary["slowestMs"] == 0

    def test_时间戳坏掉的行被跳过而不是让整个接口挂掉(self):
        spans = [
            _span(spanId="SP-00001", startedAt="不是时间", endedAt=""),
            _span(spanId="SP-00002", durationMs=1_000,
                  startedAt="2026-09-18T14:00:00.000+00:00", endedAt="2026-09-18T14:00:01.000+00:00"),
        ]
        assert _span_window_ms(spans) == 1_000

    def test_带时区与不带时区混在一起不抛类型错(self):
        """`fromisoformat('…14:47:30')` 给的是 naive，而另一条是 aware。

        只做 `fromisoformat` 不补时区的话，`max()`/`min()` 比较两者会抛
        `TypeError: can't compare offset-naive and offset-aware datetimes`，
        整个接口 500。历史行是由不同脚本写进去的，混合很现实。
        """
        window = _span_window_ms([
            _span(spanId="SP-00001", startedAt="2026-09-18T14:00:00.000+00:00",
                  endedAt="2026-09-18T14:00:10.000+00:00"),
            _span(spanId="SP-00002", startedAt="2026-09-18T14:00:05.000",
                  endedAt="2026-09-18T14:00:20.000"),
        ])
        assert window == 20_000

    def test_时钟回拨不会给出负的窗口(self):
        """负窗口会让并发度变成负数，界面上像"比串行还串行"。"""
        assert _span_window_ms([
            _span(spanId="SP-00001", startedAt="2026-09-18T14:00:10.000+00:00",
                  endedAt="2026-09-18T14:00:00.000+00:00"),
        ]) == 0


# ============================================================
# 三、降级与失败分开
# ============================================================


class TestStatusCounting:
    def test_降级不算失败(self):
        summary = _summarize_spans([
            _span(spanId="SP-00001", status="ok"),
            _span(spanId="SP-00002", status="degraded"),
            _span(spanId="SP-00003", status="degraded"),
        ])
        assert summary["degradedCount"] == 2
        assert summary["errorCount"] == 0

    def test_失败也不算降级(self):
        summary = _summarize_spans([_span(status="error"), _span(status="degraded")])
        assert summary["errorCount"] == 1
        assert summary["degradedCount"] == 1

    def test_没有叫failedCount的字段(self):
        assert "failedCount" not in _summarize_spans([_span()])

    def test_空状态当成没问题(self):
        """老行可能 status 是空串。算成失败会凭空造出失败。"""
        summary = _summarize_spans([_span(status="")])
        assert summary["errorCount"] == 0
        assert summary["degradedCount"] == 0
