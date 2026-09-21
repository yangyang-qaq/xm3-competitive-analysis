"""事件日志表（SSE 断线续传的持久层）。

内存里的 `EventJournal` 只保留最近若干条，且随进程消失。这张表是
**真正的日志**：进程重启后，`journal.hydrate(repo.events.list_since(...))`
能把日志接回来，于是"跑到一半重启服务"不会让工作台丢掉前半段。

写入是同步的（在发布事件的线程里直接 INSERT）。代价是每次发布多一次
磁盘写；换来的是"事件一旦发出去，就一定在库里"——而 SSE 的正确性
完全建立在这个保证上。批量场景（一次发布几百条）目前不存在，
真出现了再改成异步 flush。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.core.observability.events import PipelineEvent
from app.db.codec import dump_json, load_dict
from app.db.connection import get_conn


def append(event: PipelineEvent) -> None:
    get_conn().execute(
        "INSERT OR REPLACE INTO task_events (task_id, seq, type, data, created_at) VALUES (?,?,?,?,?)",
        (event.task_id, event.seq, event.type, dump_json(event.data), event.created_at),
    )


def append_many(events: Sequence[PipelineEvent]) -> int:
    if not events:
        return 0
    get_conn().executemany(
        "INSERT OR REPLACE INTO task_events (task_id, seq, type, data, created_at) VALUES (?,?,?,?,?)",
        [
            (e.task_id, e.seq, e.type, dump_json(e.data), e.created_at)
            for e in events
        ],
    )
    return len(events)


def list_since(task_id: str, from_seq: int = 0, *, limit: int = 20_000) -> list[dict]:
    """取 seq 大于 `from_seq` 的事件，按 seq 升序。

    返回 dict 而不是 `PipelineEvent`：`EventJournal.hydrate` 接受的就是
    dict（它对应一行），直接给 dict 省掉一次来回转换。
    """
    rows = get_conn().execute(
        "SELECT task_id, seq, type, data, created_at FROM task_events "
        "WHERE task_id = ? AND seq > ? ORDER BY seq LIMIT ?",
        (task_id, from_seq, limit),
    ).fetchall()
    return [
        {
            "task_id": row["task_id"],
            "seq": int(row["seq"]),
            "type": row["type"],
            "data": load_dict(row["data"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def count(task_id: str) -> int:
    row = get_conn().execute(
        "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ?", (task_id,)
    ).fetchone()
    return int(row["n"]) if row else 0


def last_seq(task_id: str) -> int:
    row = get_conn().execute(
        "SELECT MAX(seq) AS n FROM task_events WHERE task_id = ?", (task_id,)
    ).fetchone()
    return int(row["n"]) if row and row["n"] is not None else 0


def purge(task_id: str) -> int:
    cursor = get_conn().execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
    return cursor.rowcount


def make_sink() -> Any:
    """给 `EventJournal` 用的落库回调。

    单独一个函数是为了让"事件发布 → 落库"这条连线只有一处，
    而不是在 runner、API、CLI 三处各接一次（那样迟早有一处漏接，
    表现为某个入口的事件无法续传，而且只在那个入口复现）。
    """

    def sink(event: PipelineEvent) -> None:
        append(event)

    return sink
