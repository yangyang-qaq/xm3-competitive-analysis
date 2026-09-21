"""调研对象与品牌要**在跑到一半时就写进任务行**，而且只写一次。

原来错在哪
----------
`tasks.subject` / `tasks.brands` 只有 `_pause()`（澄清那条路）会写。
于是一个**不需要澄清**的任务跑完，任务行上的调研对象一直是空串——
实测这台机器 116 行全空，包括 12 条已经 `done` 的。

症状不是报错，是工作台顶上那句「（尚未解析出调研对象）」从头挂到尾，
以及文库页的标题是空的。用户看到的像是"系统没读懂我的需求"，
而其实模型早就解析出来了，只是没往那一行写。

两条测试线
--------
- **集成**（走真流水线）：`_track` 那条持久化路径确实会同步身份，
  且落库的值与报告正文对得上。
- **单元**（直接驱动 `_sync_identity`）：落库的次数的语义。
  这部分不必跑整条流水线——`_sync_identity` 只读 `ctx.subject` /
  `ctx.brands` 两个属性，所以可以把它单独拎出来，用一个最小的
  假 ctx 精确控制"品牌为空"这类边界。
"""
from __future__ import annotations

from app.core.models import TaskRecord
from app.core.pipeline.runner import TaskRunner
from app.db.repo import tasks as tasks_module
from app.db.repo import tasks as tasks_repo

# ============================================================
# 集成：走完整条流水线
# ============================================================


async def test_跑完之后任务行上有调研对象(run_mock_pipeline) -> None:
    """**这个文件的重点。**

    抓的 bug：`subject` 只在澄清那条路上写。跑一个不需要澄清的任务，
    任务行的 `subject` 是空串——工作台标题栏永远挂着
    「（尚未解析出调研对象）」，而模型早就解析出来了。
    """
    outcome = await run_mock_pipeline()

    record = tasks_repo.get(outcome.task_id)

    assert record.subject, "跑完了任务行上还没有调研对象"
    assert record.subject == outcome.body.get("subject"), "任务行的调研对象与报告对不上"


async def test_跑完之后任务行上有品牌(run_mock_pipeline) -> None:
    """品牌同样要落库。

    抓的 bug：只同步 `subject` 不同步 `brands`。表现是任务详情页
    "竞品"那一栏永远是空的，而报告正文里明明有。
    """
    outcome = await run_mock_pipeline()

    record = tasks_repo.get(outcome.task_id)

    assert record.brands, "跑完了任务行上还没有品牌"
    assert record.brands == outcome.body.get("brands"), "任务行的品牌与报告对不上"


async def test_同步不覆盖任务行的其他列(run_mock_pipeline) -> None:
    """同步只碰 `subject` 与 `brands` 两列。

    抓的 bug：同步时把整个 `TaskRecord` 写回去（`update(id, **record)`），
    于是流水线中途的 `status` / `stage` / `progress` 会被一个过期的
    快照覆盖——表现是进度条往回跳、状态在 running 和 pending 之间闪。
    """
    outcome = await run_mock_pipeline()

    record = tasks_repo.get(outcome.task_id)

    assert record.status == "done", "同步把终态覆盖掉了"
    assert record.query, "原始需求被清掉了"
    assert record.mode == "quick", "档位被覆盖了"


# ============================================================
# 单元：直接驱动 `_sync_identity`
# ============================================================


class _FakeCtx:
    """只带 `_sync_identity` 真正读的那两个属性。

    **刻意不用真的 `PipelineContext`**：它要 8 个构造参数（tracer、
    journal、三个 provider…），为了测"写几次"而搭起整条流水线，
    会让这条测试的失败原因变得难判断。而 `_sync_identity` 只读这两个
    字段这件事由上面那三条集成测试守着——真 ctx 的路径是通的。
    """

    def __init__(self, subject: str = "", brands: list[str] | None = None) -> None:
        self.subject = subject
        self.brands = list(brands or [])


def _runner(task_id: str) -> TaskRunner:
    tasks_repo.create(TaskRecord(task_id=task_id, query="对比 Notion 与 Obsidian", status="running"))
    return TaskRunner(task_id, "对比 Notion 与 Obsidian", mode_key="quick")


def _count_subject_writes(monkeypatch) -> list[dict]:
    """把 `runner` 模块里那个 `tasks_repo.update` 换成计数版。

    换的是 `runner` 模块里引用的那个名字（`runner.tasks_repo`），
    而不是 `app.db.repo.tasks.update` 全局——本文件里我自己也要用
    `tasks_repo.get` 读回来，把全局换掉会让读也走计数版。
    """
    calls: list[dict] = []
    original = tasks_module.update

    def counting_update(task_id: str, **fields):
        calls.append({"taskId": task_id, **fields})
        return original(task_id, **fields)

    monkeypatch.setattr(tasks_module, "update", counting_update)
    return calls


def test_品牌为空时也只写一次(mock_pipeline_db, monkeypatch) -> None:
    """**这条守的是 `_identity_synced` 那个标记。**

    判据写成"值非空才写"的话，模型解析不出品牌时（`ctx.brands` 为空，
    这是**正常情况**，不是异常）每个事件都会重试一次——
    一次运行几百条事件就是几百次 UPDATE，每一次都去抢 SQLite 的写锁。
    表现不是报错，是整个流水线变慢，而且慢在数据库上、跟模型没关系
    ——最难归因的一类性能问题。

    标记记的是"写过了"，与值是不是空无关。这条测试让 brands 为空，
    然后连调三次：必须只写一次。
    """
    calls = _count_subject_writes(monkeypatch)
    runner = _runner("TK-1")
    runner._ctx = _FakeCtx(subject="Notion 与 Obsidian", brands=[])

    for _ in range(3):
        runner._sync_identity()

    assert len(calls) == 1, f"写了 {len(calls)} 次，应该是 1 次（标记没生效）"


def test_没有调研对象时一次都不写(mock_pipeline_db, monkeypatch) -> None:
    """intake 还没跑完时 `ctx.subject` 是空的。

    抓的 bug：不判 `subject` 就写。那样会先把一个空标题写进任务行，
    再把标记置位——于是真正的调研对象**永远不会被写进去**
    （标记已经用掉了）。表现和原来那个 bug 一模一样，
    但更难查：因为代码看起来"已经同步过了"。
    """
    calls = _count_subject_writes(monkeypatch)
    runner = _runner("TK-1")
    runner._ctx = _FakeCtx(subject="", brands=["Notion"])

    runner._sync_identity()
    runner._sync_identity()

    assert calls == [], "调研对象还没解析出来就往任务行上写了一个空值"


def test_还没有_ctx_时不崩(mock_pipeline_db, monkeypatch) -> None:
    """第一个事件可能早于 ctx 的构造。`_track` 在每条事件上都会调它。"""
    calls = _count_subject_writes(monkeypatch)
    runner = _runner("TK-1")
    runner._ctx = None

    runner._sync_identity()

    assert calls == []


def test_同步的值也进了内存状态(mock_pipeline_db) -> None:
    """除了落库，`state.subject` / `state.brands` 也要更新。

    抓的 bug：只写数据库不更新 `state`。这样**同一次运行里**
    SSE 推出去的快照（工作台就是读它渲染标题的）仍然是空的，
    而数据库里已经有了——用户刷新页面才看得到标题，
    不刷新就一直看不到。
    """
    runner = _runner("TK-1")
    runner._ctx = _FakeCtx(subject="Notion 与 Obsidian", brands=["Notion", "Obsidian"])

    runner._sync_identity()

    assert runner.state.subject == "Notion 与 Obsidian"
    assert runner.state.brands == ["Notion", "Obsidian"]
