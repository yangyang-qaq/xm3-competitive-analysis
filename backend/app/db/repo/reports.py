"""报告表与批注反馈。

`save()` 从报告对象内部取 `task_id`
----------------------------------
它**不接受** `task_id` 参数。参考实现的签名是 `save_report(rep, task_id="")`
且用 `INSERT OR REPLACE`——调用方少传一个参数，报告与任务的关联就被
静默抹掉了，而报告本身看起来完全正常（它照样能被列表页读出来，
只是永远点不进对应的任务）。

去掉这个参数之后，这个 bug 在结构上不可能发生：`ReportRecord` 自己
就带着 `task_id`，没有"忘了传"的位置。
"""
from __future__ import annotations

import uuid
from typing import Any

from app.core.models import ReportRecord
from app.db.codec import dump_json, load_dict, load_str_list
from app.db.connection import get_conn
from app.db.repo.tasks import now_iso

_REPORT_COLUMNS = frozenset({
    "query", "mode", "subject", "brands", "generated_at", "data", "metrics", "quality",
})
_JSON_COLUMNS = frozenset({"brands", "data", "metrics", "quality"})


def _row_to_record(row) -> ReportRecord:
    return ReportRecord(
        report_id=row["report_id"],
        task_id=row["task_id"],
        query=row["query"],
        mode=row["mode"],
        subject=row["subject"],
        brands=load_str_list(row["brands"]),
        generated_at=row["generated_at"],
        data=load_dict(row["data"]),
        metrics=load_dict(row["metrics"]),
        quality=load_dict(row["quality"]),
    )


def save(report: ReportRecord) -> str:
    """写入或整体替换一份报告。返回 report_id。

    `INSERT OR REPLACE` 在这里是安全的：主键是 report_id，
    而 report_id 是本次运行的唯一标识，替换的只会是自己。
    （参考实现里它不安全，是因为同时被用来维护 task_id 关联。）
    """
    if not report.report_id:
        raise ValueError("报告必须有 report_id")
    if not report.task_id:
        raise ValueError("报告必须有 task_id——它只能来自 ReportRecord 自身")
    report.generated_at = report.generated_at or now_iso()

    conn = get_conn()
    conn.execute(
        """
        INSERT OR REPLACE INTO reports
            (report_id, task_id, query, mode, subject, brands, generated_at, data, metrics, quality)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            report.report_id, report.task_id, report.query, report.mode, report.subject,
            dump_json(report.brands), report.generated_at,
            dump_json(report.data), dump_json(report.metrics), dump_json(report.quality),
        ),
    )
    _sync_team(report.report_id, report.data)
    return report.report_id


def _sync_team(report_id: str, data: dict) -> None:
    """把这份报告的队伍写进 `report_team`。

    **先删后插**，不是只插：`save()` 用的是 `INSERT OR REPLACE`，
    同一份 report_id 会被整体替换，队伍也可能跟着变（返工之后队伍会重派）。
    只插不删的话，旧的成员会留在表里，表现是**某个人的参与度永远降不下来**。

    队伍的定义仍然只有一处：`team_ids()`。这里只负责把它落到表里，
    不自己解析 `data["team"]`——各写一遍的话，队伍形状一变
    （比如加一个"复核人"角色）就会一处处地漏。
    """
    conn = get_conn()
    conn.execute("DELETE FROM report_team WHERE report_id = ?", (report_id,))
    rows = [(report_id, expert_id) for expert_id in sorted(team_ids(data))]
    if rows:
        conn.executemany(
            "INSERT OR IGNORE INTO report_team (report_id, expert_id) VALUES (?,?)", rows
        )


def get(report_id: str) -> ReportRecord | None:
    row = get_conn().execute(
        "SELECT * FROM reports WHERE report_id = ?", (report_id,)
    ).fetchone()
    return _row_to_record(row) if row else None


def get_by_task(task_id: str) -> ReportRecord | None:
    """取某个任务最新的报告。

    用 `ORDER BY generated_at DESC, rowid DESC` 而不是假定"一个任务一份报告"：
    返工驱动的二次调研会产生第二份，而"最新那份"比"报错说有多份"更有用。
    """
    row = get_conn().execute(
        "SELECT * FROM reports WHERE task_id = ? ORDER BY generated_at DESC, rowid DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return _row_to_record(row) if row else None


def list_recent(*, limit: int = 50, offset: int = 0, mode: str = "") -> list[ReportRecord]:
    sql = "SELECT * FROM reports"
    params: list[Any] = []
    if mode:
        sql += " WHERE mode = ?"
        params.append(mode)
    sql += " ORDER BY generated_at DESC, rowid DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return [_row_to_record(row) for row in get_conn().execute(sql, params).fetchall()]


def count(*, mode: str = "") -> int:
    if mode:
        row = get_conn().execute(
            "SELECT COUNT(*) AS n FROM reports WHERE mode = ?", (mode,)
        ).fetchone()
    else:
        row = get_conn().execute("SELECT COUNT(*) AS n FROM reports").fetchone()
    return int(row["n"]) if row else 0


def link_evidences(report_id: str, task_id: str) -> int:
    """把本次任务的证据挂到报告上，供证据库按报告筛选。"""
    cursor = get_conn().execute(
        "UPDATE evidences SET report_id = ? WHERE task_id = ?", (report_id, task_id)
    )
    return cursor.rowcount


# ============================================================
# 批注与反馈
# ============================================================


def add_feedback(
    report_id: str,
    *,
    content: str,
    section_key: str = "",
    kind: str = "annotation",
    author: str = "local",
) -> str:
    """新增一条批注。**永远新增，不覆盖**。

    「人工修正率」的分子是这张表的行数。用 `report_id` 做主键的话，
    用户改三次同一个章节只会留下一行，修正率永远是 0 或 1，
    看不出"这份报告被改了多少次"。
    """
    feedback_id = f"FB-{uuid.uuid4().hex[:12]}"
    get_conn().execute(
        """
        INSERT INTO report_feedback
            (feedback_id, report_id, section_key, kind, content, author, created_at)
        VALUES (?,?,?,?,?,?,?)
        """,
        (feedback_id, report_id, section_key, kind, content, author, now_iso()),
    )
    return feedback_id


def list_feedback(report_id: str) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM report_feedback WHERE report_id = ? ORDER BY created_at, rowid",
        (report_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def count_feedback(report_id: str, *, kind: str = "") -> int:
    if kind:
        row = get_conn().execute(
            "SELECT COUNT(*) AS n FROM report_feedback WHERE report_id = ? AND kind = ?",
            (report_id, kind),
        ).fetchone()
    else:
        row = get_conn().execute(
            "SELECT COUNT(*) AS n FROM report_feedback WHERE report_id = ?", (report_id,)
        ).fetchone()
    return int(row["n"]) if row else 0


def correction_rate() -> dict:
    """人工修正率：有批注的报告数 ÷ 报告总数。

    这是唯一一个把"用户的真实反应"变成数字的地方。它也是
    `report_feedback` 必须用代理主键的原因——见 `add_feedback`。
    """
    conn = get_conn()
    total = count()
    row = conn.execute(
        "SELECT COUNT(DISTINCT report_id) AS n FROM report_feedback"
    ).fetchone()
    annotated = int(row["n"]) if row else 0
    return {
        "reports": total,
        "annotatedReports": annotated,
        "correctionRate": round(annotated / total, 4) if total else 0.0,
        "totalFeedbacks": int(
            (conn.execute("SELECT COUNT(*) AS n FROM report_feedback").fetchone() or {"n": 0})["n"]
        ),
    }


# ============================================================
# 专家参与度
# ============================================================


def team_ids(data: dict) -> set[str]:
    """一份报告正文里被派到的所有专家 id。

    **只此一份实现。** `team_usage()`（数全员）和名册详情（数一个人）
    都要从同一个字段里读同一件事，各写一遍的话，等队伍的形状变
    （比如加一个"复核人"角色）就会一处处地漏，而漏掉的那处
    只会表现为"这个人参与次数偏少"——看起来像数据问题，不像代码问题。

    "一份报告里同一个人只算一次"也在这里定：他可能既被列进 lead 又被
    列进 strategists，按出现次数算会得到大于报告总数的参与数。
    """
    team = (data or {}).get("team")
    if not isinstance(team, dict):
        return set()
    # 队伍的形状是 `{"lead": [...], "strategists": [...], "executors": [...]}`
    # （见 `loader.DEFAULT_TEAM`）。**不写死这三个键**：多一个角色就该多算一份。
    return {
        str(member)
        for members in team.values()
        if isinstance(members, list)
        for member in members
    }


def team_usage() -> dict[str, int]:
    """每位专家被**派进过多少次调研**。

    读的是 `report_team` 关联表，**不读 `reports.data`**。
    这张表由 `save()` 通过 `_sync_team()` 维护，历史数据由迁移 v4 回填。

    以前的实现是把每份报告的正文整个读出来解析。它自己的 docstring 早就
    写明了这条路的天花板（"规模上去之后正确的做法是加一张
    `report_team(report_id, expert_id)` 关联表"），压测只是把那个天花板
    量了出来：233 份报告 / 41 MB 正文时 **950 ms**，而
    `/api/experts` 每次请求都要调它。现在是一次 `GROUP BY`。

    这里**故意不加缓存**。缓存能省下的那点时间远小于"缓存与表不同步"
    的代价——参与度错一个数不会报错、不会崩，只会让某个人显示
    "参与过 7 次"而实际是 9 次。真正省时间的办法只有一个：别去读 JSON。
    """
    return {
        row["expert_id"]: int(row["n"])
        for row in get_conn().execute(
            "SELECT expert_id, COUNT(*) AS n FROM report_team GROUP BY expert_id"
        ).fetchall()
    }


def reports_of_expert(expert_id: str, *, limit: int = 200) -> list[dict]:
    """某位专家参与过的报告，**只取元数据，不取正文**。

    名册详情页要的只是"他都参与过哪几次"（标题、时间、点进去的 id），
    而 `data` 平均 172 KB。原来的实现是 `list_recent(limit=200)` 之后
    逐份读正文再 `team_ids()` 过滤——为了回答一个人的问题，
    读了 200 份报告的全文。走关联表之后这个问题变成一次索引查询。

    返回 dict 而不是 `ReportRecord`：`ReportRecord` 带 `data` 字段，
    而这条查询的**全部意义就是不带它**。给一个 `data={}` 的 Record
    会让人以为正文真的空了。
    """
    rows = get_conn().execute(
        """
        SELECT r.report_id, r.task_id, r.query, r.subject, r.generated_at
          FROM report_team t JOIN reports r ON r.report_id = t.report_id
         WHERE t.expert_id = ?
         ORDER BY r.generated_at DESC, r.rowid DESC LIMIT ?
        """,
        (expert_id, limit),
    ).fetchall()
    return [
        {
            "reportId": row["report_id"],
            "taskId": row["task_id"],
            "query": row["query"],
            "subject": row["subject"],
            "generatedAt": row["generated_at"],
        }
        for row in rows
    ]


#: 聚合查询只需要的列。**`data` 不在里面，这是刻意的。**
#:
#: 仪表盘要的每个数字都在 `metrics` / `quality` / `brands` 这三个标量列里
#: （`metrics` 有 40 个键，证据数、论点数、交叉验证率、成本、耗时全在），
#: 而 `data` 是整份报告正文——每份几百 KB。走 `list_recent()` 的话，
#: 一个仪表盘要先把十几份报告正文从磁盘读出来再全丢掉。
#:
#: 这不是"以后会慢"的问题，是**每一次刷新都在做无用功**。
_AGGREGATE_COLUMNS = (
    "report_id, task_id, mode, query, subject, brands, generated_at, metrics, quality"
)


def for_aggregate(*, limit: int = 1000) -> list[dict]:
    """给仪表盘用的瘦查询：**没有正文**。

    返回 dict 而不是 `ReportRecord`：那个 dataclass 带 `data: dict` 字段，
    为了塞一个空 dict 进去而构造它，会让读代码的人以为正文只是碰巧为空。
    """
    rows = get_conn().execute(
        f"SELECT {_AGGREGATE_COLUMNS} FROM reports "
        "ORDER BY generated_at DESC, rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "reportId": row["report_id"],
            "taskId": row["task_id"],
            "mode": row["mode"],
            "query": row["query"],
            "subject": row["subject"],
            "brands": load_str_list(row["brands"]),
            "generatedAt": row["generated_at"],
            "metrics": load_dict(row["metrics"]),
            "quality": load_dict(row["quality"]),
        }
        for row in rows
    ]
