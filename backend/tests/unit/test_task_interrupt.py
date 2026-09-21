"""启动时的僵尸任务清理：`tasks_repo.interrupt_unfinished()`。

问题是什么
----------
`tasks.status` 是**进程内状态的快照**，而 `tasks` 表是持久化的。
两者在进程重启时必然分叉：一个任务跑到一半、进程被 Ctrl-C 掉，
库里那一行还是 `running`，而那个流水线对象已经不存在了。

实测这台机器：116 行任务里 104 行是这么来的（90%）。
它们的共同点是**报告 0 份、证据 0 行**——什么都没产出，
所以判成 `failed` 不丢任何东西。但界面上的表现是"有 104 个任务正在跑"，
用户会一直等它们。

为什么用 `failed` 而不是新加一个 `interrupted`
-------------------------------------------
加状态要同时改：后端的状态常量、前端的 `TaskStatus` 联合类型、
两边的 `TERMINAL_STATUSES`、以及那份契约测试。四处一起改，
漏一处的表现是"这个任务永远转圈"。而 `failed` 已经带 `error` 字段，
把原因写进去就够用户看懂了——**新状态的收益是零**。
"""
from __future__ import annotations

import pytest

from app.core.models import TaskRecord
from app.db.repo import tasks as tasks_repo


def _mk(task_id: str, status: str, **extra) -> TaskRecord:
    record = TaskRecord(task_id=task_id, query="对比 Notion 与 Obsidian", status=status, **extra)
    tasks_repo.create(record)
    return record


# ============================================================
# 判哪些
# ============================================================


@pytest.mark.parametrize("status", ["pending", "running", "awaiting_clarify"])
def test_三个非终态都被判掉(mock_pipeline_db, status: str) -> None:
    """`pending` / `running` / `awaiting_clarify` 都是"进程内还活着"的意思。

    抓的 bug：只判 `running`。那样**正卡在澄清问答上的任务**会在重启后
    永远停在"等待补充信息"——而那个等待的对象（进程内的 `Event`）
    已经不存在了，用户回答了也没有任何东西会收到。
    `awaiting_clarify` 是最该被清理的一个，恰恰最容易漏。
    """
    _mk("TK-1", status)

    assert tasks_repo.interrupt_unfinished() == 1
    assert tasks_repo.get("TK-1").status == "failed"


@pytest.mark.parametrize("status", ["done", "failed"])
def test_终态一个都不动(mock_pipeline_db, status: str) -> None:
    """已经跑完的和已经失败的都不该被改写。

    抓的 bug：`UPDATE tasks SET status='failed' WHERE status <> 'done'`。
    那样每重启一次，历史失败原因就被覆盖成"进程重启时没跑完"——
    而那条记录里的真实原因（比如"provider 鉴权失败"）是用户唯一的线索。
    """
    _mk("TK-1", status, error="原本的原因")

    assert tasks_repo.interrupt_unfinished() == 0
    record = tasks_repo.get("TK-1")
    assert record.status == status
    assert record.error == "原本的原因", "终态任务的原因被覆盖了"


def test_改动行数是返回的(mock_pipeline_db) -> None:
    """启动日志要打印"清理了几个"。数不出来就只能打一句"清理完成",
    而那句话在"一个都没清"和"清了 104 个"时一模一样。"""
    _mk("TK-1", "running")
    _mk("TK-2", "pending")
    _mk("TK-3", "done")

    assert tasks_repo.interrupt_unfinished() == 2


# ============================================================
# 写什么
# ============================================================


def test_原因写进_error_字段(mock_pipeline_db) -> None:
    """用户看到的必须是**一句能看懂的话**，而不是一个空原因。

    抓的 bug：只改 status 不写 error。那样任务详情页上是一个
    failed 的任务配一句空白——用户唯一的反应是"它为什么失败"，
    而答案本来就在手边。
    """
    _mk("TK-1", "running")
    tasks_repo.interrupt_unfinished()

    error = tasks_repo.get("TK-1").error
    assert error, "失败原因没写"
    assert "重启" in error, f"原因不像是在说这件事：{error}"


def test_可以自定义原因(mock_pipeline_db) -> None:
    """`reason` 有默认值但不写死——将来别的场合（比如手动清理）
    要判掉任务时，不该被迫复述"进程重启"这个不对的叙述。"""
    _mk("TK-1", "running")
    tasks_repo.interrupt_unfinished(reason="运维手动叫停")

    assert tasks_repo.get("TK-1").error == "运维手动叫停"


# ============================================================
# 幂等
# ============================================================


def test_再跑一次一个都不改(mock_pipeline_db) -> None:
    """**这条守的是"能不能在每次启动都无脑调用"。**

    抓的 bug：清理不幂等（比如把 `failed` 也当待清理）。
    那样第一次启动清 104 个、第二次再清 104 个，
    启动日志里那个数字失去意义——而它是判断"上次是不是异常退出"的唯一信号。
    """
    _mk("TK-1", "running")
    _mk("TK-2", "pending")

    assert tasks_repo.interrupt_unfinished() == 2
    assert tasks_repo.interrupt_unfinished() == 0


def test_空库不报错(mock_pipeline_db) -> None:
    """全新的库第一次启动就走这条路径（`main.py` 的 lifespan 里）。"""
    assert tasks_repo.interrupt_unfinished() == 0
