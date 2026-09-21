"""证据表。

这里的两个非标准列承载着两条铁律的可度量性：
- `credibility_breakdown` —— 让"为什么这条 74 分"可以被复核
- `matched_dimensions`   —— 让维度覆盖率有一个不依赖模型自述的判据
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.core.models import CredibilityBreakdown, Evidence
from app.db.codec import dump_json, load_dict_list, load_json, load_str_list
from app.db.connection import get_conn

_INSERT = """
INSERT OR REPLACE INTO evidences
    (evidence_id, task_id, report_id, url, title, snippet, full_text, brand, source_type,
     site_name, published_at, captured_at, matched_dimensions, query, provider, rank,
     credibility, credibility_breakdown, degraded, images)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def _record_to_row(ev: Evidence, task_id: str, report_id: str) -> tuple:
    return (
        ev.evidence_id, task_id, report_id, ev.url, ev.title, ev.snippet, ev.full_text,
        ev.brand, ev.source_type, ev.site_name, ev.published_at, ev.captured_at,
        dump_json(ev.matched_dimensions), ev.query, ev.provider, ev.rank,
        ev.credibility, dump_json(ev.credibility_breakdown.to_dict()),
        int(ev.degraded), dump_json(ev.images),
    )


def _row_to_record(row) -> Evidence:
    breakdown = load_json(row["credibility_breakdown"], {})
    # `to_dict()` 里多了一个派生的 `total`，构造 dataclass 前要去掉——
    # 它是算出来的，不是字段。不去掉会 `TypeError: unexpected keyword`。
    breakdown.pop("total", None)
    return Evidence(
        evidence_id=row["evidence_id"],
        url=row["url"],
        title=row["title"],
        snippet=row["snippet"],
        full_text=row["full_text"],
        brand=row["brand"],
        source_type=row["source_type"],
        site_name=row["site_name"],
        published_at=row["published_at"],
        captured_at=row["captured_at"],
        matched_dimensions=load_str_list(row["matched_dimensions"]),
        query=row["query"],
        provider=row["provider"],
        rank=int(row["rank"]),
        credibility=float(row["credibility"]),
        credibility_breakdown=CredibilityBreakdown.from_dict(breakdown),
        degraded=bool(row["degraded"]),
        images=load_dict_list(row["images"]),
    )


def save_many(
    task_id: str, evidences: Sequence[Evidence], *, report_id: str = ""
) -> int:
    """批量写入。同一个任务里重复的 evidence_id 会被覆盖——
    这是想要的行为：返工重新抓到同一条 URL 时，新的正文与评分应当胜出。"""
    if not evidences:
        return 0
    conn = get_conn()
    conn.executemany(_INSERT, [_record_to_row(ev, task_id, report_id) for ev in evidences])
    return len(evidences)


def list_by_task(task_id: str, *, limit: int = 2000) -> list[Evidence]:
    rows = get_conn().execute(
        "SELECT * FROM evidences WHERE task_id = ? ORDER BY credibility DESC, rank LIMIT ?",
        (task_id, limit),
    ).fetchall()
    return [_row_to_record(row) for row in rows]


def count_rows(
    *,
    brand: str = "",
    source_type: str = "",
    min_credibility: float = 0.0,
    degraded: bool | None = None,
    domain: str = "",
    text: str = "",
) -> int:
    """**按行数**（含同一来源被多次采到）。与 `count_library()` 是一对，
    区别只有 `COUNT(*)` 与 `COUNT(DISTINCT evidence_id)`。

    全系统"一共被引用了多少次"只有这一个实现。仪表盘的 `mentions`
    与知识库筛选面的 `mentions` 都调它——两处各写一遍 SQL 的话，
    改一处漏一处，而两页的 hint 写的是同一句话，
    分叉了用户没有任何办法判断该信哪个。

    （曾经有一个配对用的 `search()`：按行的筛选查询。接口改用
    `library()` 去重之后它就没有调用方了，已删。这里保留 `_filters`
    是因为 `library()` / `count_library()` 与它共用同一套筛选条件——
    删掉 `search()` 不等于筛选逻辑只剩一份，而是它本来就只剩一份。）
    """
    where, params = _filters(brand, source_type, min_credibility, degraded, domain, text)
    row = get_conn().execute(f"SELECT COUNT(*) AS n FROM evidences {where}", params).fetchone()
    return int(row["n"]) if row else 0


def _filters(
    brand: str, source_type: str, min_credibility: float, degraded: bool | None,
    domain: str, text: str,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if brand:
        clauses.append("brand = ?")
        params.append(brand)
    if source_type:
        clauses.append("source_type = ?")
        params.append(source_type)
    if min_credibility > 0:
        clauses.append("credibility >= ?")
        params.append(min_credibility)
    if degraded is not None:
        clauses.append("degraded = ?")
        params.append(int(degraded))
    if domain:
        # 按域名后缀匹配，用 `= ? OR LIKE '%.' || ?` 而不是 `LIKE '%?%'`：
        # 后者会让 `notion.so.evil.test` 命中 `notion.so`。
        clauses.append("(site_name = ? OR url LIKE ?)")
        params.extend([domain, f"%//%{domain}/%"])
    if text:
        clauses.append("(title LIKE ? OR snippet LIKE ?)")
        params.extend([f"%{text}%", f"%{text}%"])
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def facets() -> dict:
    """**按行数**的筛选面。

    ⚠️ 当前**没有调用方**。接口那层用的是 `library_facets()`（按去重来源），
    两者只差 `COUNT(*)` 与 `COUNT(DISTINCT evidence_id)`——正是
    `library_facets()` 的 docstring 说"不能混"的那两个口径。

    它和 `search()` 是同一对"按行"的实现，一起被去重的那一对取代了。
    `search()` 已删（它的唯一有据可查的用法就是 `test_evidences_api.py`
    里记录的那个 bug）。这个**暂时留着**：它里面的 `degraded` 分面
    是别处没有的，而仓库**还没有 git**——删掉找不回来。
    阶段 8 做知识库页面时一并定：要就接上，不要就删。

    一次查出来而不是让前端逐个请求：筛选面必须与列表来自同一时刻的数据，
    分多次查会出现"选项里有 8 条、点进去 0 条"的不一致。
    """
    conn = get_conn()
    by_source = [
        {"value": row["source_type"], "count": int(row["n"])}
        for row in conn.execute(
            "SELECT source_type, COUNT(*) AS n FROM evidences GROUP BY source_type ORDER BY n DESC"
        ).fetchall()
    ]
    by_brand = [
        {"value": row["brand"], "count": int(row["n"])}
        for row in conn.execute(
            "SELECT brand, COUNT(*) AS n FROM evidences WHERE brand <> '' "
            "GROUP BY brand ORDER BY n DESC LIMIT 50"
        ).fetchall()
    ]
    total = count_rows()
    degraded = int(
        (conn.execute("SELECT COUNT(*) AS n FROM evidences WHERE degraded = 1").fetchone() or {"n": 0})["n"]
    )
    return {
        "total": total,
        "degraded": degraded,
        "bySourceType": by_source,
        "byBrand": by_brand,
    }


def stats(task_id: str) -> dict:
    """某个任务的证据统计。报告的"证据来源分布"面板读它。"""
    row = get_conn().execute(
        """
        SELECT COUNT(*) AS total,
               AVG(credibility) AS avg_cred,
               SUM(degraded) AS degraded,
               COUNT(DISTINCT brand) AS brands
        FROM evidences WHERE task_id = ?
        """,
        (task_id,),
    ).fetchone()
    if not row or not row["total"]:
        return {"total": 0, "avgCredibility": 0.0, "degraded": 0, "brands": 0,
                "bySourceType": [], "independentDomains": 0}

    from app.core.evidence.sourcetypes import independent_domain

    urls = [
        r["url"]
        for r in get_conn().execute(
            "SELECT url FROM evidences WHERE task_id = ?", (task_id,)
        ).fetchall()
    ]
    by_source = [
        {"value": r["source_type"], "count": int(r["n"])}
        for r in get_conn().execute(
            "SELECT source_type, COUNT(*) AS n FROM evidences WHERE task_id = ? "
            "GROUP BY source_type ORDER BY n DESC",
            (task_id,),
        ).fetchall()
    ]
    return {
        "total": int(row["total"]),
        "avgCredibility": round(float(row["avg_cred"] or 0.0), 2),
        "degraded": int(row["degraded"] or 0),
        "brands": int(row["brands"] or 0),
        "bySourceType": by_source,
        "independentDomains": len({independent_domain(u) for u in urls} - {""}),
    }


# ============================================================
# 知识库：按来源去重的跨任务视图
# ============================================================
#
# 上面那些是**按行**的：一行 = "某次调研用过某条证据"。这是对的，
# 报告的"用了 216 条证据"说的是行数。
#
# 但知识库问的是另一个问题："我手里一共沉淀了多少**条来源**"。
# 实测这台机器：2426 行，去重后只有 266 条 URL —— 差的 9 倍全是
# **同一个来源被多次调研重复采到**。`evidence_id` 是 URL 的摘要
# （见 `models.evidence_id_for`），所以行数会随着调研次数线性膨胀，
# 而来源数只会缓慢增长。照行数铺出来，用户看到的是同一篇文章的 11 份副本。
#
# 去重是**安全的**：同一个 `evidence_id` 在这些列上实测完全一致
# （正文、评分、来源类型、域名、发布时间），因为它们都是从 URL 和
# 抓回来的正文算出来的，与哪次调研无关。所以聚合函数取哪个都行，
# 这里用 MIN 只是为了让 SQL 合法。
_LIBRARY_COLUMNS = (
    "evidence_id",
    "MIN(url) AS url", "MIN(title) AS title", "MIN(snippet) AS snippet",
    "MIN(full_text) AS full_text", "MIN(brand) AS brand",
    "MIN(source_type) AS source_type", "MIN(site_name) AS site_name",
    "MIN(published_at) AS published_at", "MAX(captured_at) AS captured_at",
    "MIN(matched_dimensions) AS matched_dimensions", "MIN(query) AS query",
    "MIN(provider) AS provider", "MIN(rank) AS rank",
    "MAX(credibility) AS credibility",
    "MIN(credibility_breakdown) AS credibility_breakdown",
    "MIN(degraded) AS degraded", "MIN(images) AS images",
    #: 被多少次调研采到过。
    #: `DISTINCT` 在这里是**防御，不是被测试守住的行为**：表的主键是
    #: `(task_id, evidence_id)`，所以"同一个任务里同一个来源两行"
    #: 是一个**不存在的状态**，`COUNT(DISTINCT task_id)` 恒等于 `COUNT(*)`。
    #: 突变校验实测：把 DISTINCT 去掉，全部测试照样绿——因为确实没区别。
    #: 留着它是因为这条 SQL 将来可能加上别的 JOIN（那时 DISTINCT 就有意义了），
    #: 但**不要为它写一条测试**：那条测试会永远绿。
    "COUNT(DISTINCT task_id) AS task_count",
)


#: 第一步只要这几列。**窄**是重点：不含 `full_text` / `images` 这些宽列。
_LIBRARY_RANK_COLUMNS = (
    "evidence_id",
    "COUNT(DISTINCT task_id) AS task_count",
    "MAX(credibility) AS credibility",
)


def _library_rows(
    *, brand: str, source_type: str, min_credibility: float, domain: str, text: str,
    limit: int, offset: int,
) -> list:
    """知识库列表的取数。**分两步，理由见下。**

    为什么不是一条 SQL
    ----------------
    一条 SQL 长这样：

        SELECT {_LIBRARY_COLUMNS} FROM evidences {where}
        GROUP BY evidence_id
        ORDER BY task_count DESC, credibility DESC, evidence_id LIMIT ? OFFSET ?

    它能跑对，但**读的是整张表**：`_LIBRARY_COLUMNS` 里有 `MIN(full_text)`
    和 `MIN(images)`，于是 48,384 行（224 个任务 × 每个任务 216 条来源）
    的正文全被读进来做了聚合——而最后只要 30 组，其余 186 组的正文
    读进来就扔了。

    实测（`data/loadtest.db`，48,384 行 / 216 个来源）：

        一条 SQL     338 ms / 535 ms（两次）
        两步         187 ms / 331 ms

    所以要选的是「先把 216 组排出来，再回表取那 30 组」。第一步只碰三列，
    第二步虽然仍是全量扫，但 `evidence_id IN (...)` 把范围收窄到 30 组。

    **一个反直觉的实测结果：给 `evidence_id` 加索引会更慢。**
    ------------
    看查询计划，直觉是"`GROUP BY evidence_id` 没有索引，加一个就好了"。
    实测相反：

        加索引前   357 ms   SCAN evidences + USE TEMP B-TREE FOR GROUP BY
        加索引后   648 ms   SCAN evidences USING INDEX idx_evidences_eid

    因为 `_LIBRARY_COLUMNS` 要的是整行（含 `full_text`），走索引扫描之后
    每一行都得**回表随机读一次**，而顺序扫一遍是纯顺序 I/O。
    省掉的那个 GROUP BY 临时 B 树远不值这些随机读。
    （那条实验索引已经删掉了。）**这一步的结论只来自实测**，
    换成另一张列窄一些的表，结论可能就反过来。

    第二步的聚合值与一条 SQL 必须逐列相同
    --------------------------------
    第二步仍然对**同一批行**做同样的 `MIN`/`MAX`：`where` 照旧传进去，
    再加上 `evidence_id IN (...)`。过滤条件只用来圈定"哪些行算数"，
    而这些行的全集没变（某个 id 的所有出现都在），所以每个聚合值都不变。
    `tests/unit/test_evidence_library.py` 里有一条用例是拿这个函数
    与朴素的一条 SQL 逐列比对的——**这条改写值不值，靠的是那条测试**。
    """
    conn = get_conn()
    where, params = _filters(brand, source_type, min_credibility, None, domain, text)

    ranked = conn.execute(
        f"SELECT {', '.join(_LIBRARY_RANK_COLUMNS)} FROM evidences {where} "
        # 按 **evidence_id** 分组，不是按 url。两者一一对应（id 就是 URL 的摘要），
        # 但证据 id 是系统里的真身份，按它分组才不会在"同一个 URL 抓了两次正文
        # 略有不同"时裂成两行。
        "GROUP BY evidence_id "
        "ORDER BY task_count DESC, credibility DESC, evidence_id LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()

    ids = [row["evidence_id"] for row in ranked]
    if not ids:
        return []

    marks = ",".join("?" * len(ids))
    picked = (f"{where} AND " if where else "WHERE ") + f"evidence_id IN ({marks})"
    rows = conn.execute(
        f"SELECT {', '.join(_LIBRARY_COLUMNS)} FROM evidences {picked} GROUP BY evidence_id",
        (*params, *ids),
    ).fetchall()

    # 第二步的 SQL 没有 ORDER BY（排序键在第一步已经算完了），
    # 所以这里按第一步的顺序摆回去。**不能省**：列表顺序是这个接口
    # 的一部分（"用过 11 次的排在用过 1 次的前面"），
    # 而 `IN (...)` 的返回顺序在 SQL 里是没有保证的。
    order = {evidence_id: index for index, evidence_id in enumerate(ids)}
    return sorted(rows, key=lambda row: order[row["evidence_id"]])


def library(
    *,
    brand: str = "",
    source_type: str = "",
    min_credibility: float = 0.0,
    domain: str = "",
    text: str = "",
    limit: int = 40,
    offset: int = 0,
) -> list[dict]:
    """知识库列表：**一条来源一行**，附带被多少次调研用过。

    排序按"用过几次"而不是按可信度：知识库的价值在**复用**，
    一条被 11 次调研都采到的来源比一条只有 95 分但没人用过的更该排在前面。
    """
    rows = _library_rows(
        brand=brand, source_type=source_type, min_credibility=min_credibility,
        domain=domain, text=text, limit=limit, offset=offset,
    )
    return [
        {
            "evidenceId": row["evidence_id"],
            "url": row["url"],
            "title": row["title"],
            "snippet": row["snippet"],
            "fullText": row["full_text"],
            "brand": row["brand"],
            "sourceType": row["source_type"],
            "siteName": row["site_name"],
            "publishedAt": row["published_at"],
            "capturedAt": row["captured_at"],
            "credibility": float(row["credibility"]),
            "degraded": bool(row["degraded"]),
            #: 被多少次调研采到过。这是知识库区别于证据流的那一列。
            "taskCount": int(row["task_count"]),
        }
        for row in rows
    ]


def count_library(
    *,
    brand: str = "",
    source_type: str = "",
    min_credibility: float = 0.0,
    domain: str = "",
    text: str = "",
) -> int:
    """去重后的来源总数。分页器要的是它，不是行数。"""
    where, params = _filters(brand, source_type, min_credibility, None, domain, text)
    row = get_conn().execute(
        f"SELECT COUNT(DISTINCT evidence_id) AS n FROM evidences {where}", params
    ).fetchone()
    return int(row["n"]) if row else 0


def library_facets() -> dict:
    """知识库的筛选面，**与 `library()` 同一套去重口径**。

    必须同口径：`facets()` 是按行数的，拿它当知识库的筛选面会出现
    "选项写着 24 条、点进去 3 条"——而这正是 `facets()` 自己的
    docstring 说不能发生的那件事。
    """
    conn = get_conn()
    by_source = [
        {"value": r["source_type"], "count": int(r["n"])}
        for r in conn.execute(
            "SELECT source_type, COUNT(DISTINCT evidence_id) AS n FROM evidences "
            "GROUP BY source_type ORDER BY n DESC"
        ).fetchall()
    ]
    by_brand = [
        {"value": r["brand"], "count": int(r["n"])}
        for r in conn.execute(
            "SELECT brand, COUNT(DISTINCT evidence_id) AS n FROM evidences "
            "WHERE brand <> '' GROUP BY brand ORDER BY n DESC LIMIT 50"
        ).fetchall()
    ]
    total = int(
        (conn.execute("SELECT COUNT(DISTINCT evidence_id) AS n FROM evidences").fetchone()
         or {"n": 0})["n"]
    )
    # 与仪表盘的 `coverage.mentions` **调同一个函数**：两页的 hint
    # 写的是同一句话（"266 条来源，被引用 2426 次"），
    # 这个数在全系统只能有一个实现。
    rows = count_rows()
    return {
        "total": total,
        #: 行数（含重复采到的）。页面上用它说明"266 条来源，被引用 2426 次"。
        "mentions": rows,
        "bySourceType": by_source,
        "byBrand": by_brand,
    }
