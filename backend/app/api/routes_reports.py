"""报告：列表、详情、导出、批注。

列表**不返回报告正文**
--------------------
一份 quick 模式的报告正文里有 216 条证据，每条都带 `fullText`——
实测整份 JSON 有几 MB。列表页要显示 50 份，照搬整份正文就是几百 MB，
而这个数字会随着使用时间**自然增长**：它不是某一天写错的代码，
是"用久了才会出现"的那种问题。

所以列表返回的是**摘要**：足以渲染一张卡片（标题、模式、时间、指标、
质量门），而正文只在详情接口里给。这也是为什么 `ReportRecord.data`
在列表路径上根本不被序列化——不是"少传几个字段"，是根本不读它。

导出走同一份渲染代码
------------------
`GET /api/reports/{id}/export` 不重新实现一遍渲染，而是调
`core/report/export.py` 的那两个纯函数。两份实现的话，
界面上看到的和导出得到的东西迟早会不一样，
而"导出的报告和页面上的不是一个东西"这件事没人会发现。
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.core.report.export import to_json, to_markdown
from app.core.report.refine import refine
from app.db.repo import reports as reports_repo

router = APIRouter(prefix="/api/reports", tags=["reports"])

#: 列表一次最多给多少份。前端做分页，但服务端也要有个上限——
#: 没有上限的话 `?limit=100000` 就是一个免费的拒绝服务入口。
MAX_LIMIT = 200


class FeedbackRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=4000, description="批注内容")
    sectionKey: str = Field(default="", description="批注挂在哪一节；空=整份报告")  # noqa: N815
    #: `annotation`（人工批注）/ `refine`（按批注深化）/ `rating`（评分）。
    #:
    #: **「人工修正率」不分 kind**：`correction_rate()` 数的是
    #: "有批注的报告数"，一次深化同样是一次人工介入，该算进去。
    #: 这个字段的作用是**事后能拆开看**——三类意见各占多少。
    kind: str = Field(default="annotation")


class RefineRequest(BaseModel):
    """深化一节的请求。

    `sectionKey` 空 = 整份报告的批注，落到第一章（见 `refine._section_of`）。
    """

    annotation: str = Field(..., min_length=1, max_length=2000, description="批注内容")
    sectionKey: str = Field(default="", description="要深化哪一节")  # noqa: N815
    search: bool = Field(default=True, description="是否再搜一轮新证据")


def _summary(record) -> dict:
    """卡片需要的字段。**刻意不含 `data`。**

    这条不是优化，是正确性：`data` 是整份报告正文，
    而列表接口的调用者（文库页、仪表盘）不需要它。
    """
    metrics = record.metrics or {}
    quality = record.quality or {}
    return {
        "reportId": record.report_id,
        "taskId": record.task_id,
        "query": record.query,
        "subject": record.subject,
        "brands": list(record.brands),
        "mode": record.mode,
        "generatedAt": record.generated_at,
        "metrics": metrics,
        "quality": {
            "passed": quality.get("passed"),
            "publishable": quality.get("publishable"),
            "blockers": quality.get("blockers"),
        },
        "evidenceCount": metrics.get("evidences", 0),
        "degradedCount": len(record.data.get("degraded") or []),
    }


def _disposition(subject: str, report_id: str, suffix: str) -> str:
    """下载文件名。**HTTP 头只能用 latin-1**，而报告主题是中文。

    直接拼进去的话，`starlette` 会在 `v.encode("latin-1")` 那里抛
    `UnicodeEncodeError`——也就是**每一份中文报告导出都 500**。
    这个错误只在导出路径上出现，而它恰恰是最不会被顺手点到的那个接口。

    按 RFC 6266 给两个名字：ASCII 兜底（老客户端）+ `filename*`
    （UTF-8 百分号编码，现代浏览器优先用它）。百分号编码顺带解决了
    引号与换行截断头部的问题——报告主题来自模型输出，
    不能假定它不含 `"` 或换行（写这条时第一版就是这么栽的）。
    """
    stem = "".join(
        ch for ch in subject if ch.isascii() and (ch.isalnum() or ch in " -_")
    ).strip()
    fallback = f"{stem or report_id}.{suffix}"
    encoded = quote(f"{subject or report_id}.{suffix}", safe="")
    return f"inline; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def _load(report_id: str):
    record = reports_repo.get(report_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"报告 {report_id} 不存在")
    return record


@router.get("")
def list_reports(
    limit: int = Query(default=20, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    mode: str = Query(default=""),
) -> dict:
    """报告列表。按生成时间倒序。"""
    records = reports_repo.list_recent(limit=limit, offset=offset, mode=mode)
    return {
        "items": [_summary(record) for record in records],
        "total": reports_repo.count(mode=mode),
        "limit": limit,
        "offset": offset,
    }


def _feedback_out(row: dict) -> dict:
    """批注行 → 接口形状。

    `repo.list_feedback` 返回的是**数据库的行**（`feedback_id` /
    `section_key`），而这一层往外发的一律是驼峰。直接透传的话，
    前端读 `feedbackId` 拿到 `undefined`，然后**什么都不显示**——
    不报错、不告警，界面上只是少了一块。

    这类不一致是"看起来没接上"的经典成因，所以在边界上一次性转掉。
    """
    return {
        "feedbackId": row.get("feedback_id", ""),
        "reportId": row.get("report_id", ""),
        "sectionKey": row.get("section_key", ""),
        "kind": row.get("kind", ""),
        "content": row.get("content", ""),
        "author": row.get("author", ""),
        "createdAt": row.get("created_at", ""),
    }


@router.get("/{report_id}")
def get_report(report_id: str) -> dict:
    """报告详情：正文 + 批注 + 修正率。

    批注**跟着详情一起给**，而不是让前端再发一个请求：报告页一打开
    就要渲染批注，分两次拿的话中间那一帧是"没有批注"的状态，
    而用户刚写完的批注会先消失再出现。
    """
    record = _load(report_id)
    feedback = [_feedback_out(row) for row in reports_repo.list_feedback(report_id)]
    return {
        "reportId": record.report_id,
        "taskId": record.task_id,
        "data": record.data,
        "feedback": feedback,
        "feedbackCount": len(feedback),
    }


@router.post("/{report_id}/feedback")
def add_feedback(report_id: str, payload: FeedbackRequest) -> dict:
    """新增一条批注。**永远新增，不覆盖**——理由见 `repo/reports.py`。"""
    _load(report_id)
    feedback_id = reports_repo.add_feedback(
        report_id,
        content=payload.content,
        section_key=payload.sectionKey,
        kind=payload.kind,
    )
    return {
        "feedbackId": feedback_id,
        "reportId": report_id,
        "count": reports_repo.count_feedback(report_id),
        "correctionRate": reports_repo.correction_rate(),
    }


@router.post("/{report_id}/refine")
async def refine_report(report_id: str, payload: RefineRequest) -> dict:
    """「深化本节」：按批注**再搜一轮、再写一遍**这一章。

    为什么不是整份重跑
    ----------------
    流水线跑完之后，最弱的那一章往往才看得出来——读者读完才知道
    "定价这一节说了等于没说"。整份重跑要重花十几分钟和几毛钱，
    而问题只在一章里。

    `search=False` 是"我只要换个说法，别再去搜"：那时只重写，
    一次搜索都不发。**默认 True**，因为 `deepen` 这个词承诺的就是
    拿到新证据，而不是把同一批材料再组织一遍。

    写回的是**整份报告**（一次 UPDATE），不是局部更新：
    `reports.data` 是一列 JSON，局部更新要读-改-写，和整体写回等价，
    还多一次竞态窗口。
    """
    record = _load(report_id)
    try:
        result = await refine(
            record,
            payload.annotation,
            section_key=payload.sectionKey,
            search=payload.search,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"报告里没有章节 {exc}") from exc
    except ValueError as exc:
        # 报告没有章节：这是数据问题，不是请求问题。
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    reports_repo.save(record)
    # 深化记一条 `refine` 批注：**批注是提出要求，深化是对它的执行**，
    # 两者都该在库里。只记批注的话，"这条意见到底有没有被处理"查不出来。
    reports_repo.add_feedback(
        report_id,
        content=payload.annotation,
        section_key=result["sectionKey"],
        kind="refine",
    )
    result["feedbackCount"] = reports_repo.count_feedback(report_id)
    result["correctionRate"] = reports_repo.correction_rate()
    return result


@router.get("/{report_id}/export")
def export_report(
    report_id: str,
    format: str = Query(default="md", pattern="^(md|markdown|json)$"),  # noqa: A002
    include_full_text: bool = Query(default=False),
) -> PlainTextResponse:
    """导出。Markdown 给人看，JSON 给机器读。

    返回 `text/plain` 而不是 `text/markdown`：后者的浏览器行为是**下载**，
    而演示时更常用的动作是在新标签页里直接看一遍。
    `Content-Disposition` 里给一个带报告名的文件名，
    这样用户右键另存时得到的不是一串 report_id。
    """
    record = _load(report_id)
    if format == "json":
        body = to_json(record.data, include_full_text=include_full_text)
        suffix = "json"
    else:
        body = to_markdown(record.data)
        suffix = "md"

    return PlainTextResponse(
        body,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": _disposition(record.subject, report_id, suffix)},
    )


__all__ = ["router"]
