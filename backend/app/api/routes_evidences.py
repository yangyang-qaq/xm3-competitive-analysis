"""知识库：跨任务的证据来源。

为什么这一层**不直接暴露 `evidences` 表的行**
-------------------------------------------
一行 = "某次调研用了某条证据"。报告页的"本次用了 216 条证据"说的
就是行数，那个语义是对的。

但知识库问的是另一个问题：**我手里一共沉淀了多少条来源**。
实测这台机器 2426 行、去重后 266 条 URL——差的 9 倍全是同一个来源
被反复采到。照行数铺出来，用户看到的是同一篇文章的 11 份副本，
而"知识库"这个词承诺的恰恰是**沉淀**，不是流水账。

所以这一组接口一律走 `repo.evidences` 里那三个 `*_library*` 函数，
去重口径在那一处定义（列表、计数、筛选面必须同口径，否则会出现
"选项写着 24 条、点进去 3 条"）。

列表**不返回正文**
----------------
`fullText` 实测平均几 KB（真实抓回来的页面更长），一次 40 条就是
几百 KB，而列表卡片只显示 `snippet`。全文只在详情里给——但这张表
没有全局唯一的证据 id（主键是 `(evidence_id, task_id)`），所以
"按 id 取详情"这件事不存在：id 会跨任务重复，取哪一行都是赌。
要正文的场合是报告页的证据角标，那里走的是报告自己那份 `data`。
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.db.repo import evidences as evidences_repo

router = APIRouter(prefix="/api/evidences", tags=["evidences"])

#: 列表一次最多给多少条。分页由前端做，但服务端要有上限——
#: 没有上限的话 `?limit=100000` 就是一个免费的拒绝服务入口，
#: 而这个接口读的正是全表最大的那张表。
MAX_LIMIT = 200


def _card(row: dict) -> dict:
    """列表卡片。**刻意不含 `fullText`。**

    这不是省字段，是省一次全表读：`full_text` 是这张表里唯一的大列，
    带上它之后每次翻页都要把几百 KB 的正文从 SQLite 读出来再吐掉。
    """
    return {
        "evidenceId": row["evidenceId"],
        "url": row["url"],
        "title": row["title"],
        "snippet": row["snippet"],
        "brand": row["brand"],
        "sourceType": row["sourceType"],
        "siteName": row["siteName"],
        "publishedAt": row["publishedAt"],
        "capturedAt": row["capturedAt"],
        "credibility": row["credibility"],
        "degraded": row["degraded"],
        "taskCount": row["taskCount"],
    }


@router.get("")
def list_evidences(
    brand: str = Query(default=""),
    source_type: str = Query(default="", alias="sourceType"),
    min_credibility: float = Query(default=0.0, ge=0.0, le=100.0, alias="minCred"),
    domain: str = Query(default=""),
    q: str = Query(default="", description="按标题/摘要模糊匹配"),
    limit: int = Query(default=40, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """知识库列表。**一条来源一行**（去重后）。

    `total` 是去重后的来源数，`mentions` 是包含重复的总行数——
    两个都给，因为"266 条来源 / 被引用 2426 次"比单独一个数更说明问题。
    """
    rows = evidences_repo.library(
        brand=brand,
        source_type=source_type,
        min_credibility=min_credibility,
        domain=domain,
        text=q,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [_card(row) for row in rows],
        "total": evidences_repo.count_library(
            brand=brand,
            source_type=source_type,
            min_credibility=min_credibility,
            domain=domain,
            text=q,
        ),
        "limit": limit,
        "offset": offset,
    }


@router.get("/facets")
def evidence_facets() -> dict:
    """筛选面。与列表**同一次去重口径**——分开算就会出现
    "选项里有 24 条、点进去 3 条"的不一致。"""
    return evidences_repo.library_facets()


__all__ = ["router"]
