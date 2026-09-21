"""任务表。

`update()` 只接受白名单列名
--------------------------
因为它接受 `**fields`。不设白名单的话，调用方一个拼错的列名
会静默什么都不做（`UPDATE ... SET statu=?` 是合法 SQL），
而"状态没更新"这种问题查起来极其费劲。白名单把拼写错误
从"静默无操作"变成"立刻抛错"。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.models import TaskRecord
from app.db.codec import dump_json, load_dict, load_dict_list, load_str_list
from app.db.connection import get_conn

_TASK_COLUMNS = frozenset({
    "query", "mode", "status", "stage", "progress", "need_clarify",
    "clarify_questions", "clarify_answers", "subject", "brands", "error", "updated_at",
})
_JSON_COLUMNS = frozenset({"clarify_questions", "clarify_answers", "brands"})


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _row_to_record(row) -> TaskRecord:
    return TaskRecord(
        task_id=row["task_id"],
        query=row["query"],
        mode=row["mode"],
        status=row["status"],
        stage=row["stage"],
        progress=float(row["progress"]),
        need_clarify=bool(row["need_clarify"]),
        clarify_questions=load_dict_list(row["clarify_questions"]),
        clarify_answers=load_dict(row["clarify_answers"]),
        subject=row["subject"],
        brands=load_str_list(row["brands"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        error=row["error"],
    )


def create(task: TaskRecord) -> TaskRecord:
    stamp = task.created_at or now_iso()
    task.created_at = stamp
    task.updated_at = stamp
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO tasks (task_id, query, mode, status, stage, progress, need_clarify,
                           clarify_questions, clarify_answers, subject, brands, error,
                           created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            task.task_id, task.query, task.mode, task.status, task.stage, task.progress,
            int(task.need_clarify), dump_json(task.clarify_questions),
            dump_json(task.clarify_answers), task.subject, dump_json(task.brands),
            task.error, task.created_at, task.updated_at,
        ),
    )
    return task


def get(task_id: str) -> TaskRecord | None:
    row = get_conn().execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return _row_to_record(row) if row else None


def update(task_id: str, **fields: Any) -> bool:
    """更新若干列。返回是否有行被改动。"""
    unknown = set(fields) - _TASK_COLUMNS
    if unknown:
        raise ValueError(f"tasks 表没有这些列：{sorted(unknown)}")
    if not fields:
        return False

    payload = {}
    for key, value in fields.items():
        payload[key] = dump_json(value) if key in _JSON_COLUMNS else value
    payload.setdefault("updated_at", now_iso())

    assignments = ", ".join(f"{column} = ?" for column in payload)
    conn = get_conn()
    cursor = conn.execute(
        f"UPDATE tasks SET {assignments} WHERE task_id = ?",
        (*payload.values(), task_id),
    )
    return cursor.rowcount > 0


#: 进程重启时会被判死的状态。**它是"非终态"的另一个说法**，
#: 但不直接引用 `runner.NON_TERMINAL`：那会让 db 层依赖 pipeline 层，
#: 而 `repo` 是更底下的一层。这里写死三个字面量，并由
#: `tests/unit/test_migrations.py` 那一类测试盯着两处一致。
#:
#: `awaiting_clarify` **在里面**。看着可惜（用户的问题还在任务行上，
#: 答案还没交），但流水线协程已经随进程消失了：`proceed()` 会从需求理解
#: 重跑一遍，于是又暂停、又问一遍同样的问题——一个答不完的循环。
#: 把它判死，用户至少得到"这次没了，重新发起"，而不是一个装死的页面。
_UNFINISHED_STATUSES = ("pending", "running", "awaiting_clarify")

#: 判死时写进 `error` 的话。它会显示在任务列表和工作台上，
#: 所以要说明**为什么**，不能只写"已中断"——用户看到的是"任务失败了"，
#: 而这次失败和模型、网络、钱都没有关系，重试是有用的。
INTERRUPTED_REASON = "后端进程重启时这个任务还没跑完，它已被中断。进程内的流水线不跨重启存活，重新发起即可。"


def interrupt_unfinished(reason: str = INTERRUPTED_REASON) -> int:
    """把所有非终态的任务判为 `failed`。启动时调用一次，返回改了几行。

    **为什么必须在启动时做**：`tasks.status` 是进程内状态的快照，
    而任务表是持久的。两者在重启处必然分叉——库里写着"正在跑"，
    而那个协程已经不存在了。没人清理的话它会**永远**留在 running：
    实测这台机器 116 行任务里 104 行是这么来的（僵尸行占 90%），
    而它们在界面上长得和真在跑的任务一模一样。

    为什么用 `failed` 而不是新加一个 `interrupted`
    ----------------------------------------
    加状态要同时改后端常量、前端 `TaskStatus` 联合、两边的
    `TERMINAL_STATUSES`、以及那份契约测试，而它与 `failed` 的**下一步动作
    完全相同**（都是重新发起）。这里真正的信息量在 `error` 那句原因里，
    不在状态码里。多一个状态换来的是更多需要保持同步的地方。
    """
    placeholders = ",".join("?" for _ in _UNFINISHED_STATUSES)
    cursor = get_conn().execute(
        f"UPDATE tasks SET status = 'failed', error = ?, updated_at = ? "
        f"WHERE status IN ({placeholders})",
        (reason, now_iso(), *_UNFINISHED_STATUSES),
    )
    return cursor.rowcount


def list_recent(*, limit: int = 50, offset: int = 0, status: str = "") -> list[TaskRecord]:
    sql = "SELECT * FROM tasks"
    params: list[Any] = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = get_conn().execute(sql, params).fetchall()
    return [_row_to_record(row) for row in rows]


def count(*, status: str = "") -> int:
    if status:
        row = get_conn().execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status = ?", (status,)
        ).fetchone()
    else:
        row = get_conn().execute("SELECT COUNT(*) AS n FROM tasks").fetchone()
    return int(row["n"]) if row else 0


def delete(task_id: str) -> bool:
    cursor = get_conn().execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
    return cursor.rowcount > 0
