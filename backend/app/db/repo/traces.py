"""埋点表。

落库的是 `Span.to_dict()` 的结果，读出来也还是 dict——刻意**不在
数据层重建 `Span` 对象**。理由：消费方（trace 页、决策回放、成本面板）
都只需要读，而重建对象会带来"写入时用 span_id、读取时用 evidence_id"
这类字段名不一致的风险。dict 一路到底，字段名只在一处定义（`Span` 的字段）。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.db.codec import dump_json, load_dict
from app.db.connection import get_conn

_INSERT = """
INSERT OR REPLACE INTO traces
    (span_id, task_id, parent_id, kind, name, purpose, provider, model, started_at, ended_at,
     duration_ms, prompt_tokens, completion_tokens, cost_usd, cached_prompt_tokens,
     status, error, detail)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def _span_to_row(span: dict, task_id: str) -> tuple:
    return (
        span.get("span_id", ""), task_id, span.get("parent_id", ""), span.get("kind", ""),
        span.get("name", ""), span.get("purpose", ""), span.get("provider", ""),
        span.get("model", ""), span.get("started_at", ""), span.get("ended_at", ""),
        int(span.get("duration_ms", 0) or 0), int(span.get("prompt_tokens", 0) or 0),
        int(span.get("completion_tokens", 0) or 0), float(span.get("cost_usd", 0.0) or 0.0),
        int(span.get("cached_prompt_tokens", 0) or 0),
        span.get("status", "ok"), span.get("error", ""), dump_json(span.get("detail") or {}),
    )


def save_many(task_id: str, spans: Sequence[dict]) -> int:
    if not spans:
        return 0
    rows = [_span_to_row(span, task_id) for span in spans if span.get("span_id")]
    if not rows:
        return 0
    get_conn().executemany(_INSERT, rows)
    return len(rows)


def list_by_task(task_id: str, *, limit: int = 5000) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM traces WHERE task_id = ? ORDER BY started_at, rowid LIMIT ?",
        (task_id, limit),
    ).fetchall()
    return [_row_to_span(row) for row in rows]


def get_span(task_id: str, span_id: str) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM traces WHERE task_id = ? AND span_id = ?", (task_id, span_id)
    ).fetchone()
    return _row_to_span(row) if row else None


def _row_to_span(row) -> dict:
    return {
        "spanId": row["span_id"],
        "taskId": row["task_id"],
        "parentId": row["parent_id"],
        "kind": row["kind"],
        "name": row["name"],
        "purpose": row["purpose"],
        "provider": row["provider"],
        "model": row["model"],
        "startedAt": row["started_at"],
        "endedAt": row["ended_at"],
        "durationMs": int(row["duration_ms"]),
        "promptTokens": int(row["prompt_tokens"]),
        "completionTokens": int(row["completion_tokens"]),
        "totalTokens": int(row["prompt_tokens"]) + int(row["completion_tokens"]),
        "costUsd": float(row["cost_usd"]),
        "cachedPromptTokens": int(row["cached_prompt_tokens"]),
        "status": row["status"],
        "error": row["error"],
        "detail": load_dict(row["detail"]),
    }


def span_tree(task_id: str) -> list[dict]:
    """把扁平 span 列表组装成树。

    在**服务端**组装而不是让前端做：父子关系靠 `parent_id`，
    而前端要做的是"拖滑杆时高亮当前 span 并展开它的祖先"，
    那需要树；让它每次自己 fold 一遍是纯浪费。
    组装逻辑本身也该被测试，放在这里就能测。
    """
    spans = list_by_task(task_id)
    by_id = {span["spanId"]: {**span, "children": []} for span in spans}
    roots: list[dict] = []
    for span in spans:
        node = by_id[span["spanId"]]
        parent_id = span.get("parentId") or ""
        parent = by_id.get(parent_id)
        if parent is not None and parent_id != span["spanId"]:
            parent["children"].append(node)
        else:
            # 父 span 不在（被环形上限截掉、或跨进程），当成根节点——
            # 丢掉它会让整棵子树消失。
            roots.append(node)
    return roots


def cost_summary(*, since: str = "") -> dict:
    """成本汇总。`/api/dashboard/cost` 与 `/api/providers` 的成本栏读它。"""
    conn = get_conn()
    where = "WHERE started_at >= ?" if since else ""
    params: tuple[Any, ...] = (since,) if since else ()
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS calls,
               COALESCE(SUM(cost_usd), 0) AS cost,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(cached_prompt_tokens), 0) AS cached_prompt_tokens
        FROM traces {where}
        """,
        params,
    ).fetchone()
    by_model = [
        {
            "model": r["model"],
            "provider": r["provider"],
            "calls": int(r["calls"]),
            "costUsd": round(float(r["cost"]), 6),
        }
        for r in conn.execute(
            f"""
            SELECT model, provider, COUNT(*) AS calls, SUM(cost_usd) AS cost
            FROM traces {where} {"AND" if where else "WHERE"} kind = 'llm'
            GROUP BY model, provider ORDER BY cost DESC
            """,
            params,
        ).fetchall()
    ]
    by_kind = [
        {"kind": r["kind"], "calls": int(r["calls"]), "costUsd": round(float(r["cost"]), 6)}
        for r in conn.execute(
            f"SELECT kind, COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost "
            f"FROM traces {where} GROUP BY kind ORDER BY calls DESC",
            params,
        ).fetchall()
    ]
    prompt_tokens = int(row["prompt_tokens"])
    cached_tokens = int(row["cached_prompt_tokens"])
    return {
        "calls": int(row["calls"]),
        "totalCostUsd": round(float(row["cost"]), 6),
        "promptTokens": prompt_tokens,
        "completionTokens": int(row["completion_tokens"]),
        "cachedPromptTokens": cached_tokens,
        "cacheHitRate": round(cached_tokens / prompt_tokens, 4) if prompt_tokens else 0.0,
        "byModel": by_model,
        "byKind": by_kind,
    }
