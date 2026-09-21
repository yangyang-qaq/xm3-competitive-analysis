"""竞争情报中心：把历史调研摊开成几个数。

这一页的每个数字都必须是**从库里算出来的**，不能有一个是撑门面的常量
--------------------------------------------------------------
作品集里最常见的失败是仪表盘上写着"效率提升 83×""节省 5 小时"，
而这些数**没有任何来源**——面试官问一句"这个 83 怎么来的"就露底了。
所以这里只放能指着 SQL 说清楚的东西，且每个数都附上它的口径。

三个口径上的坑，都在下面标了
--------------------------
1. **"多少条证据"有两个答案。** 2426 是行数（含重复采到的），
   266 是去重后的来源数。两个都对，问的是两件事。页面上必须分开写，
   混着用就会出现"证据库说 266、仪表盘说 2426"这种自己打自己。
   顺带一提：`mentions` 曾经写成"各报告自报证据数之和"，与真正的
   行数在这台机器上**恰好相等**（都是 2426），因为每一行都挂在报告上。
   现在按行数读表——见下面 `mentions` 那处的注释。
2. **"平均每篇报告"的分母是报告数，不是任务数。** 116 个任务里
   只有 12 个跑出了报告，用任务数当分母会得到"平均每篇 20 条"。
3. **交叉验证率是加权平均，不是平均值。** 每份报告的
   `crossValidationRate` 分母不同（有的 3 条论点、有的 18 条），
   直接平均会让一份 3 条论点的报告和一份 18 条的权重相同。
   所以这里用 `Σ交叉验证数 ÷ Σ论点数` 重算。
"""
from __future__ import annotations

from fastapi import APIRouter

from app.db.repo import evidences as evidences_repo
from app.db.repo import reports as reports_repo
from app.db.repo import tasks as tasks_repo

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _sum(rows: list[dict], key: str) -> float:
    return sum(float(row["metrics"].get(key) or 0) for row in rows)


def _ratio(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


@router.get("")
def dashboard() -> dict:
    """一次给全。这一页上的每个数字都来自同一次查询，不会互相不一致。"""
    reports = reports_repo.for_aggregate()
    report_count = len(reports)

    # ---- 覆盖 ----
    brands: set[str] = set()
    for row in reports:
        brands.update(brand for brand in row["brands"] if brand)

    claims = int(_sum(reports, "claims"))
    cross_validated = int(_sum(reports, "crossValidatedClaims"))
    sources = evidences_repo.count_library()
    # `mentions` 是 **evidences 表的行数**，与知识库页同源。
    #
    # 它原来写的是 `_sum(reports, "evidences")`（各报告自报证据数之和）。
    # 那两个数在这台机器上**恰好相等**（实测都是 2426），因为目前每一行
    # 证据都挂在一份报告上——但它们是两个不同的口径，只是在当前数据下
    # 没有分叉：一个任务如果采到证据却没跑出报告（失败、被中断），
    # 那些行会计入行数、不计入"各报告自报之和"。
    #
    # 之所以必须按下定义来：这一页的 hint 和知识库页写的是同一句话
    # （"累计被引用 N 次"）。两个页面各算各的、今天相等，
    # 等哪天真分叉了就是两页互相打脸，而那时没人会想到是这个原因。
    #
    # `count_rows()` 也是知识库筛选面算 `mentions` 用的那个函数——
    # 不只是定义一致，是**同一个实现**。只剩一份就不会分叉。
    mentions = evidences_repo.count_rows()

    # ---- 质量门 ----
    passed = sum(1 for row in reports if row["quality"].get("passed"))
    publishable = sum(1 for row in reports if row["quality"].get("publishable"))

    # ---- 按档位拆 ----
    by_mode: dict[str, dict] = {}
    for row in reports:
        bucket = by_mode.setdefault(
            row["mode"], {"mode": row["mode"], "reports": 0, "evidences": 0, "passed": 0}
        )
        bucket["reports"] += 1
        bucket["evidences"] += int(row["metrics"].get("evidences") or 0)
        bucket["passed"] += 1 if row["quality"].get("passed") else 0
    for bucket in by_mode.values():
        bucket["avgEvidences"] = round(bucket["evidences"] / bucket["reports"], 1)

    task_total = tasks_repo.count()
    running = tasks_repo.count(status="running")

    return {
        "coverage": {
            "brands": len(brands),
            "brandNames": sorted(brands),
            #: 去重后的来源数。侧边栏"已沉淀 N 条来源"用它。
            "sources": sources,
            #: 行数（含同一来源被多次调研采到）。报告里的"本次用了 N 条证据"
            #: 是这个口径，两者不能混。
            "mentions": mentions,
            "avgEvidencesPerReport": round(mentions / report_count, 1) if report_count else 0.0,
            "claims": claims,
            "crossValidatedClaims": cross_validated,
            #: **加权**：Σ交叉验证数 ÷ Σ论点数，不是逐份报告求平均。
            "crossValidationRate": _ratio(cross_validated, claims),
            "independentDomains": int(_sum(reports, "independentDomains")),
            "platformCount": max((int(r["metrics"].get("platformCount") or 0) for r in reports),
                                 default=0),
        },
        "runs": {
            "reports": report_count,
            "tasks": task_total,
            "runningTasks": running,
            "passed": passed,
            "publishable": publishable,
            #: 通过率。侧边栏那条进度条用它——**它是一个算出来的比例，
            #: 不是一个拍脑袋的目标值**，所以它不会永远停在 10%。
            "passRate": _ratio(passed, report_count),
            "totalCostUsd": round(_sum(reports, "totalCostUsd"), 6),
            "totalTokens": int(_sum(reports, "totalTokens")),
            "avgDurationMs": int(_sum(reports, "durationMs") / report_count) if report_count else 0,
        },
        "byMode": sorted(by_mode.values(), key=lambda item: item["mode"]),
        "correction": reports_repo.correction_rate(),
        "recent": [
            {
                "reportId": row["reportId"],
                "taskId": row["taskId"],
                "query": row["query"],
                "subject": row["subject"],
                "mode": row["mode"],
                "generatedAt": row["generatedAt"],
                "evidences": int(row["metrics"].get("evidences") or 0),
                "claims": int(row["metrics"].get("claims") or 0),
                "passed": bool(row["quality"].get("passed")),
                "publishable": bool(row["quality"].get("publishable")),
            }
            for row in reports[:10]
        ],
    }


__all__ = ["router"]
