"""阶段 8：装配与落库。

装配是唯一一处把散落在 ctx 里的产物拼成报告正文的地方。放在单独的模块里，
理由很实际：**报告的键名是一份对外契约**——前端的 `types/api.ts`、
`validate_report` 的必填键表、导出 Markdown 的模板、评测脚本的读法，
全都依赖它。如果拼接逻辑散在 runner 或 CLI 里，"加一个字段要改四处"
会立刻变成"加了三处，漏了一处，而漏掉那处不报错"。

出库前跑 `validate_report`
--------------------------
它在装配的最后一步，而不是在导出接口里。理由：导出接口有好几个
（`/export?format=md|json`、CLI 的 `--out`、未来的邮件推送），
每个都调一次校验，迟早有一个不调。装配处调一次，所有下游免费获得保证。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.core.analysis.metrics import compute_metrics, rework_delta
from app.core.evidence.citations import MIN_INDEPENDENT_DOMAINS
from app.core.evidence.sourcetypes import independent_domain
from app.core.models import ReportRecord
from app.core.pipeline.audit import AuditResult
from app.core.pipeline.calls import llm_status, stats_of
from app.core.pipeline.context import PipelineContext
from app.core.report.citation_index import build_citations
from app.core.schemas.completeness import assess_completeness
from app.core.schemas.report import REPORT_SCHEMA_VERSION, validate_report
from app.db.repo import evidences as evidences_repo
from app.db.repo import reports as reports_repo

#: 报告里最多展示多少张图。图片是证据的附属物，几十张图会把
#: 报告页压成一条瀑布，而它们的信息密度远低于正文。
MAX_GALLERY = 24

#: 指标术语表。报告里出现了一堆比率，读者不该为了知道
#: "幻觉引用率"是怎么算的而去翻源码。**公式直接写出来**：
#: 一个能被读者自己复算的指标，才是一个可以被质疑的指标。
_GLOSSARY: tuple[tuple[str, str], ...] = (
    ("幻觉引用率", "模型输出中指向不存在证据的引用数 ÷ 模型输出的引用总数，由代码统计"),
    ("无证据立论率", "没有可核验证据引用的论点数 ÷ 论点总数"),
    (
        "交叉验证率",
        f"引用 ≥{MIN_INDEPENDENT_DOMAINS} 个独立域名的论点数 ÷ 通过可核验检查的论点数",
    ),
    ("维度覆盖率", "有证据锚定的维度数 ÷ 计划维度数。判据是证据，不是字段有没有被填"),
    ("降级率", "正文抓取失败、只能用搜索摘要的证据占比"),
    ("独立信源数", "所有证据去掉 www 后按域名去重的数量"),
    ("首证据耗时", "任务开始到第一条证据入库之间的毫秒数"),
    ("返工提升 Δ", "返工前后确定性指标之差。只有证据与覆盖参与判定，成本不计入"),
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def build_gallery(ctx: PipelineContext) -> list[dict]:
    """图集：证据里带出来的配图。"""
    gallery: list[dict] = []
    for ev in ctx.evidences:
        for image in ev.images or []:
            url = str(image.get("url", "")) if isinstance(image, dict) else str(image)
            if not url:
                continue
            gallery.append(
                {
                    "url": url,
                    "alt": str(image.get("alt", "")) if isinstance(image, dict) else "",
                    "evidenceId": ev.evidence_id,
                    "brand": ev.brand,
                    "sourceUrl": ev.url,
                    "siteName": ev.site_name,
                }
            )
            if len(gallery) >= MAX_GALLERY:
                return gallery
    return gallery


def build_evidence_stats(ctx: PipelineContext) -> dict:
    """证据统计。完整度检查读 `total`，报告的来源分布面板读 `bySourceType`。"""
    evidences = ctx.evidences
    if not evidences:
        return {"total": 0, "avgCredibility": 0.0, "degraded": 0,
                "bySourceType": [], "byBrand": [], "independentDomains": 0}

    by_source: dict[str, int] = {}
    by_brand: dict[str, int] = {}
    for ev in evidences:
        by_source[ev.source_type] = by_source.get(ev.source_type, 0) + 1
        if ev.brand:
            by_brand[ev.brand] = by_brand.get(ev.brand, 0) + 1

    return {
        "total": len(evidences),
        "avgCredibility": round(sum(ev.credibility for ev in evidences) / len(evidences), 2),
        "degraded": len([ev for ev in evidences if ev.degraded]),
        "bySourceType": [
            {"value": key, "count": value}
            for key, value in sorted(by_source.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "byBrand": [
            {"value": key, "count": value}
            for key, value in sorted(by_brand.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "independentDomains": len({independent_domain(ev.url) for ev in evidences} - {""}),
    }


def build_metrics(ctx: PipelineContext, audit: AuditResult) -> dict:
    """确定性指标 + 返工对比 + 调用统计。"""
    metrics = compute_metrics(ctx)
    metrics["byPurpose"] = stats_of(ctx).to_dict()["byPurpose"]
    metrics["llmFallback"] = llm_status(ctx)
    # 判据是"真的返工过没有"，不是"有没有基准"：审计阶段为了留一份
    # 可比对的基准，**总会**填 `metrics_before`。拿它当条件的话，
    # 每次运行都会带一张"Δ 全 0"的对比表，读者会以为返工跑了但没用——
    # 而事实是它根本没跑。
    if ctx.rework_rounds > 0 and ctx.metrics_before:
        metrics["rework"] = rework_delta(ctx.metrics_before, metrics)
        metrics["reworkLog"] = list(ctx.rework_log)
    metrics["qualityGatePassed"] = bool(audit.quality.get("passed"))
    return metrics


def emit_images(ctx: PipelineContext, gallery: list[dict]) -> None:
    for item in gallery:
        ctx.emit("image", {"image": item})


def emit_charts(ctx: PipelineContext, charts: list[dict]) -> None:
    """把图表规格推进事件流。

    图表在 `analyze` 阶段就已经成型，而写作阶段还要跑十几章。不推的话，
    读者在工作台上能看到的只有 DAG 在动；推了之后矩阵图与定价表可以
    在报告落库之前就画出来。
    """
    for spec in charts:
        ctx.emit_chart(spec)


#: 前端**拿品牌名当 React key** 的那几块（`ReportCatalog.tsx`）。
#: 一个块里同一个品牌出现两次时，React 会把其中一条丢掉，而且只在
#: 控制台说一句——页面上少一张卡，看起来像"这个品牌没采到"。
#:
#: `marketShare` **不在**名单里：它的两行同一个品牌是**合法的**
#: （不同机构的统计口径不同，`basis` 各写着出处），报告用表格逐条保留，
#: 只有饼图不画（`charts._market_share_chart`）。
_BRAND_KEYED_BLOCKS: tuple[tuple[str, str], ...] = (
    ("feature_trees", "功能对比"),
    ("pricing_models", "定价"),
    ("persona_sets", "用户画像"),
)


def _flag_duplicate_brands(ctx: PipelineContext) -> None:
    """同一块里一个品牌出现两次时，在报告里说出来。

    库里那份真报告就是这样：`featureTrees` 里 `特来电` 有两棵（覆盖率
    0.7969 与 0.8065，内容并不相同）、`星星充电` 一棵都没有
    （见 [`问题记录.md`](../../../问题记录.md) 的问题 54）。
    根因已经修掉了——调研对象不再被当成品牌问、归属对不上的回答直接丢弃。

    这里是一道**出口上的绊线**，不是一个修复：真又出现重复时，
    报告里多一句说明，而不是页面上悄悄少一张卡。**这个差别是全部意义**——
    重复的行为在前端是"少显示一份数据"，而少显示的东西没有任何痕迹。

    为什么绊线钉在出口而不是各块自己的产出点：这三块分别由三个 parser
    在并发 gather 里产出，而「它们共用同一个 key 空间」这件事**只有
    在装配的时候才看得见**。钉在产出点的话，下次新加一块（比如渠道分布）
    时不会有任何东西提醒你该给它也加一条。
    """
    for attr, label in _BRAND_KEYED_BLOCKS:
        brands = [item.brand for item in getattr(ctx, attr)]
        dupes = sorted({brand for brand in brands if brands.count(brand) > 1})
        if dupes:
            ctx.coercion.note(
                f"{label}里「{'、'.join(dupes)}」出现了不止一次（共 {len(brands)} 条）——"
                "同一块里一个品牌只该有一条，已按原样保留；前端按品牌名做 key，"
                "重复的那些会互相顶掉"
            )


def assemble(ctx: PipelineContext, audit: AuditResult) -> dict:
    """把 ctx 里的产物拼成报告正文。**纯装配，不产生新判断。**"""
    # 必须在建 body 之前调：它往 `ctx.coercion` / `ctx.degraded_blocks` 里记账，
    # 而这两份清单是**下面那个 dict 一次性读走**的。
    _flag_duplicate_brands(ctx)

    body: dict = {
        "version": REPORT_SCHEMA_VERSION,
        "taskId": ctx.task_id,
        "query": ctx.query,
        "subject": ctx.subject,
        "domain": ctx.domain,
        "category": ctx.category,
        "brands": list(ctx.brands),
        "mode": {"key": ctx.mode.key, "label": ctx.mode.label,
                 "description": ctx.mode.description},
        # **计划维度要存下来。** 覆盖率的分母是它，而报告页要单独显示
        # "计划了 6 个维度，覆盖 4 个"；更实际的是「深化本节」——它要在
        # 报告落库之后重算指标，没有这份清单就只剩 `dimensionCoverageDetail`
        # 的键，而那份明细**故意保留了计划外的维度**，拿它当分母会把
        # 覆盖率算低。
        "dimensions": list(ctx.dimensions),
        "generatedAt": _now_iso(),
        "durationMs": ctx.elapsed_ms(),

        "sections": [section.to_dict() for section in ctx.sections],
        "claims": [claim.to_dict() for claim in ctx.claims],
        "evidences": [ev.to_dict() for ev in ctx.evidences],
        "charts": list(ctx.charts),

        "matrix": dict(ctx.matrix),
        "marketShare": list(ctx.market_share),
        "fiveForces": list(ctx.five_forces),
        "trends": list(ctx.trends),
        "featureTrees": [tree.to_dict() for tree in ctx.feature_trees],
        "pricingModels": [model.to_dict() for model in ctx.pricing_models],
        "personaSets": [persona_set.to_dict() for persona_set in ctx.persona_sets],
        "sentiment": dict(ctx.sentiment),

        "team": dict(ctx.team),
        "messages": [envelope.to_dict() for envelope in ctx.messages],
        "thoughts": list(ctx.thoughts),
        "coercion": ctx.coercion.to_dict(),
        "degraded": list(ctx.degraded_blocks),
        "glossary": [{"term": term, "definition": text} for term, text in _GLOSSARY],
    }

    body["evidenceStats"] = build_evidence_stats(ctx)
    body["audit"] = audit.to_dict()
    body["metrics"] = build_metrics(ctx, audit)

    # 正文角标的编号**存进来**，而不是留给两个渲染器各算一遍。
    # 理由见 `report/citation_index.py`：两处各算一遍的话，
    # 编号只差一位时看起来完全正常，而读者会以为自己数错了。
    body["citations"] = build_citations(
        body["sections"], {ev.evidence_id for ev in ctx.evidences}
    )

    # 完整度要在 metrics 之后算：它读的是整份 body，
    # 而 metrics 是 body 的一部分（质量门的结论也会被它看到）。
    completeness = assess_completeness(body)
    body["completeness"] = completeness.to_dict()

    quality = dict(audit.quality)
    quality["completeness"] = completeness.score
    quality["publishable"] = completeness.is_publishable
    # `passed` 与 `publishable` 不是一回事，两个都要给：
    # passed 说的是"证据与覆盖达没达到门槛"，publishable 说的是
    # "有没有整块内容缺失"。一份 passed 但缺图表的报告是可以发出去的，
    # 一份 publishable 但覆盖率很低的报告则需要带警示发出。
    body["quality"] = quality

    body["gallery"] = build_gallery(ctx)
    return body


def persist(record: ReportRecord, ctx: PipelineContext) -> int:
    """落库：报告 + 证据 + 关联。返回挂到报告上的证据条数。"""
    reports_repo.save(record)
    evidences_repo.save_many(ctx.task_id, ctx.evidences, report_id=record.report_id)
    return reports_repo.link_evidences(record.report_id, ctx.task_id)


def run(ctx: PipelineContext, audit: AuditResult) -> tuple[ReportRecord, list[str]]:
    """装配 → 校验 → 落库。返回 `(报告记录, 合规问题列表)`。

    **校验不通过也照样落库。** 拦下来会丢掉整次运行的所有产物，
    而一份"有 3 处引用问题"的报告比"什么都没有"有用得多。
    问题列表往外传，由调用方决定是打印、告警还是让 CI 失败。

    这里负责关掉 `write` 节点：装配是撰写阶段的一部分，`write.py`
    只负责写正文、不关节点。分成两处关会让"节点什么时候算完成"
    有两个答案。
    """
    body = assemble(ctx, audit)
    problems = validate_report(body)

    record = ReportRecord(
        report_id=f"RP-{uuid.uuid4().hex[:12]}",
        task_id=ctx.task_id,
        query=ctx.query,
        mode=ctx.mode.key,
        subject=ctx.subject,
        brands=list(ctx.brands),
        generated_at=body["generatedAt"],
        data=body,
        metrics=body["metrics"],
        quality=body["quality"],
    )
    linked = persist(record, ctx)
    emit_images(ctx, body["gallery"])
    emit_charts(ctx, body["charts"])

    if problems:
        ctx.degrade("报告合规校验", f"{len(problems)} 处问题：{'；'.join(problems[:3])}")
        ctx.thought(
            ctx.reviewer_expert,
            f"出库校验发现 {len(problems)} 处问题，报告已落库但仍标记为不合格："
            f"{'；'.join(problems[:3])}",
            stage="write",
        )
        # **把刚登记的这条降级补回正文。** `assemble()` 里 `body["degraded"]`
        # 是校验之前抄的一份快照，于是"这份报告没通过自己的出库校验"——
        # 最该被读到的一条降级——恰恰不在报告的降级清单里。
        body["degraded"] = list(ctx.degraded_blocks)

    # 载荷里**不能带 `taskId`**：它是信封保留键，由 journal 统一填。
    # 这里再带一份会被 `publish()` 直接抛错拦住——这正是想要的行为，
    # 因为静默覆盖会让前端拿到一个 taskId 属于别的任务的事件。
    #
    # 不带 title：报告此刻还没有"标题"这个概念（`ReportRecord` 里没有这一列）。
    # 在事件层现编一个，等于给"这份报告叫什么"造出第二个真相源。
    ctx.emit("report_ready", {
        "reportId": record.report_id,
        "query": body["query"],
        "subject": body["subject"],
        "sectionCount": len(ctx.sections),
        "evidenceCount": len(ctx.evidences),
        "problems": problems,
        # 从 `ctx` 现读而不是从 `body` 抄：上面那条降级是校验之后才登记的。
        "degraded": list(ctx.degraded_blocks),
    })

    degraded_sections = len([s for s in ctx.sections if s.degraded])
    ctx.finish_stage(
        "write",
        status="degraded" if (problems or degraded_sections) else "done",
        detail={
            "reportId": record.report_id,
            "sections": len(ctx.sections),
            "evidences": len(ctx.evidences),
            "claimedLinked": linked,
            "problems": len(problems),
        },
    )
    return record, problems
