"""阶段 5：质量审计。

为什么审计排在写作**之前**
--------------------------
写作是最贵的一步：每一章都要单独调用一次，每次都带着一份证据材料块。
审计排在写作之后的话，发现"定价维度证据不足"的代价是重写全部章节；
排在之前，代价只是补采几条 + 重跑一次分析（分析结果本来就要重算）。

参考项目的架构文档把顺序写成了"分析 → 撰写 → 质检"，但它代码里的
实际顺序是"分析 → 质检 → 撰写"。这里沿用**正确的是代码而不是文档**，
并把顺序固化在 `stages.py`，由一致性测试守着。

两类检查，两种性质
------------------
**确定性检查**：证据密度、独立域名数、平均可信度、降级比例、幻觉引用。
全部由代码算出，同样的输入永远得到同样的结论。它们的价值在于可以
进 CI 做回归门禁——而模型判模型的结果不能。

**模型评审**：每个维度 1–5 分加一句评语。它提供的是取舍判断
（"3 条证据够不够撑起这个维度"），确定性规则给不了。它的分数只进
报告，不进质量门的 `passed` 判定——否则同一份报告会因为采样不同
而一会儿合格一会儿不合格。

返工只针对 blocker 与 major
---------------------------
minor 问题**只记录不返工**。不这么分的话，每次运行都会找到一堆
可以更好的地方，于是每次运行都会返工到上限——成本翻三倍，
而报告质量提升看不出来。返工要有门槛，门槛写在这里。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.evidence.citations import MIN_INDEPENDENT_DOMAINS
from app.core.evidence.sourcetypes import independent_domain
from app.core.models import Issue, make_issue
from app.core.pipeline.calls import chat_json
from app.core.pipeline.collect import (
    covered_dimensions,
    dimension_coverage,
    weakest_dimensions,
)
from app.core.pipeline.context import PipelineContext
from app.core.pipeline.prompts import review_prompt
from app.core.schemas.base import as_float, as_list, as_str, pick

#: 一个维度的平均可信度低于它就算"材料太弱"。40 分大致是
#: "非权威来源 + 摘要级内容 + 无交叉印证"的水平。
MIN_DIMENSION_CREDIBILITY = 40.0

#: 一个维度的证据里降级（只有摘要、正文没抓到）比例超过它，
#: 说明这个维度的结论建立在摘要上。
MAX_DEGRADED_RATIO = 0.5

#: 平均时效分低于它算陈旧。新鲜度满分 15，未知发布时间的兜底是 6，
#: 所以 5 意味着"确实老"，而不是"没写日期"。
MIN_FRESHNESS_SCORE = 5.0

#: 质量门的覆盖率下限。低于它说明有相当一部分计划维度没拿到证据。
MIN_PASS_COVERAGE = 0.6

#: 返工用的补充关键词。与维度拼在一起再搜一轮。
#: 用通用词而不是平台名：平台定向是舆情轮的事，返工要的是
#: "换个说法再找一遍"，不是"去某个站再找一遍"。
_REWORK_KEYWORDS: tuple[str, ...] = ("评测", "对比", "口碑")

#: 触发返工的严重程度。minor 只记录，理由见模块 docstring。
_REWORK_SEVERITIES = frozenset({"blocker", "major"})


@dataclass
class AuditResult:
    """一次审计的全部产出。

    `rework_targets` 为空 = 不返工。把决定**返回**而不是写进 ctx：
    编排层的职责是"决定要不要再跑一轮"，审计层的职责是"给出理由"，
    两者混在一起时，"为什么这轮返工了"就只能靠读代码回答。
    """

    issues: list[Issue] = field(default_factory=list)
    review: dict = field(default_factory=dict)
    quality: dict = field(default_factory=dict)
    rework_targets: list[dict] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "issues": [issue.to_dict() for issue in self.issues],
            "review": self.review,
            "quality": self.quality,
            "reworkTargets": self.rework_targets,
            "reworkReason": self.reason,
        }


# ============================================================
# 确定性检查
# ============================================================


def audit_dimensions(ctx: PipelineContext) -> list[Issue]:
    """逐个维度看证据够不够。"""
    issues: list[Issue] = []
    buckets = ctx.evidence_by_dimension()
    minimum = max(1, ctx.mode.min_evidence_per_dimension)

    for dimension in ctx.dimensions:
        items = buckets.get(dimension, [])

        if not items:
            issues.append(
                make_issue(
                    "missing_dimension",
                    severity="blocker",
                    dimension=dimension,
                    detail=f"计划内的维度「{dimension}」没有采集到任何证据",
                    suggestion=f"补采「{dimension}」：换关键词与来源类型再搜一轮",
                )
            )
            continue

        if len(items) < minimum:
            issues.append(
                make_issue(
                    "thin_evidence",
                    severity="major",
                    dimension=dimension,
                    detail=f"「{dimension}」只有 {len(items)} 条证据，低于档位要求的 {minimum} 条",
                    suggestion=f"补采「{dimension}」，目标再取 {minimum - len(items)} 条以上",
                    evidence_ids=[ev.evidence_id for ev in items],
                )
            )

        domains = {independent_domain(ev.url) for ev in items} - {""}
        if len(domains) < MIN_INDEPENDENT_DOMAINS:
            issues.append(
                make_issue(
                    "thin_evidence",
                    severity="major",
                    dimension=dimension,
                    detail=(
                        f"「{dimension}」的 {len(items)} 条证据只来自 "
                        f"{len(domains)} 个独立域名，无法交叉验证"
                    ),
                    suggestion=f"补采「{dimension}」，重点找与现有来源不同的站点",
                    evidence_ids=[ev.evidence_id for ev in items],
                )
            )

        average = sum(ev.credibility for ev in items) / len(items)
        if average < MIN_DIMENSION_CREDIBILITY:
            issues.append(
                make_issue(
                    "low_credibility",
                    severity="major",
                    dimension=dimension,
                    detail=f"「{dimension}」平均可信度 {average:.1f}，低于阈值 {MIN_DIMENSION_CREDIBILITY}",
                    suggestion=f"补采「{dimension}」时优先官方页与权威媒体",
                    evidence_ids=[ev.evidence_id for ev in items],
                )
            )

        degraded = [ev for ev in items if ev.degraded]
        if len(degraded) / len(items) > MAX_DEGRADED_RATIO:
            issues.append(
                make_issue(
                    "degraded_source",
                    severity="minor",
                    dimension=dimension,
                    detail=(
                        f"「{dimension}」有 {len(degraded)}/{len(items)} 条证据的正文没抓到，"
                        "结论建立在搜索摘要上"
                    ),
                    suggestion="换同维度的其他页面重抓",
                    evidence_ids=[ev.evidence_id for ev in degraded],
                )
            )

        fresh = [
            ev.credibility_breakdown.freshness_score
            for ev in items
            if ev.published_at
        ]
        if fresh and sum(fresh) / len(fresh) < MIN_FRESHNESS_SCORE:
            issues.append(
                make_issue(
                    "stale_evidence",
                    severity="minor",
                    dimension=dimension,
                    detail=f"「{dimension}」的证据平均时效分 {sum(fresh) / len(fresh):.1f}，偏陈旧",
                    suggestion="补采近一年的材料",
                    evidence_ids=[ev.evidence_id for ev in items],
                )
            )

    return issues


def audit_claims(ctx: PipelineContext) -> list[Issue]:
    """逐条论点看引用是否成立。这些判据全部来自 `citations.py` 的核算，
    不采信模型的自我陈述。"""
    issues: list[Issue] = []

    for claim in ctx.claims:
        if claim.phantom_evidence_ids:
            issues.append(
                make_issue(
                    "phantom_citation",
                    severity="blocker",
                    dimension=claim.dimension,
                    brand=claim.brand,
                    detail=(
                        f"论点 {claim.claim_id} 引用了不存在的证据："
                        f"{'、'.join(claim.phantom_evidence_ids)}"
                    ),
                    suggestion="重新提炼该论点，只允许引用材料清单里给出的 id",
                    evidence_ids=claim.phantom_evidence_ids,
                )
            )
            continue

        if not claim.verified:
            issues.append(
                make_issue(
                    "uncited_claim",
                    severity="major",
                    dimension=claim.dimension,
                    brand=claim.brand,
                    detail=f"论点 {claim.claim_id} 没有任何可核验的证据引用",
                    suggestion="要么补采证据，要么删掉这条论点",
                )
            )
            continue

        if not claim.cross_validated:
            issues.append(
                make_issue(
                    "single_source",
                    severity="minor",
                    dimension=claim.dimension,
                    brand=claim.brand,
                    detail=(
                        f"论点 {claim.claim_id} 只有 {claim.independent_domains} 个独立信源，"
                        f"未达到 {MIN_INDEPENDENT_DOMAINS} 个的交叉验证要求"
                    ),
                    suggestion="找第二个独立来源印证",
                    evidence_ids=claim.evidence_ids,
                )
            )

    return issues


def deterministic_audit(ctx: PipelineContext) -> list[Issue]:
    return audit_dimensions(ctx) + audit_claims(ctx)


# ============================================================
# 模型评审（可选）
# ============================================================


def build_review_summary(ctx: PipelineContext) -> str:
    """给模型评审看的统计。**只有代码算出来的数**——

    把模型的自我陈述再喂回给它评分，等于让它给自己打分，
    而它会稳定地给自己高分。
    """
    coverage = dimension_coverage(ctx)
    buckets = ctx.evidence_by_dimension()
    lines: list[str] = []

    for dimension in ctx.dimensions:
        items = buckets.get(dimension, [])
        domains = len({independent_domain(ev.url) for ev in items} - {""})
        average = sum(ev.credibility for ev in items) / len(items) if items else 0.0
        degraded = len([ev for ev in items if ev.degraded])
        lines.append(
            f"- {dimension}：证据 {coverage.get(dimension, 0)} 条，"
            f"独立域名 {domains} 个，平均可信度 {average:.1f}，"
            f"正文降级 {degraded} 条"
        )

    verified = [c for c in ctx.claims if c.verified]
    cross = [c for c in verified if c.cross_validated]
    lines.append(
        f"- 论点合计 {len(ctx.claims)} 条，其中可核验 {len(verified)} 条，"
        f"通过交叉验证 {len(cross)} 条"
    )
    lines.append(
        f"- 证据总计 {len(ctx.evidences)} 条，"
        f"独立域名 {len({independent_domain(ev.url) for ev in ctx.evidences} - {''})} 个，"
        f"抓取失败 {ctx.fetch_outcome.degraded} 条"
    )
    return "\n".join(lines)


def run_review(ctx: PipelineContext) -> dict:
    """模型评审。失败不影响这次审计——确定性检查已经给出了可返工的清单。"""
    payload, report = chat_json(
        ctx,
        review_prompt(ctx.query, ctx.brands, ctx.dimensions, build_review_summary(ctx)),
        tier=ctx.mode.analysis_tier,
        purpose="audit_review",
        max_tokens=1536,
        required=False,
    )
    ctx.coercion.merge(report)

    scores: list[dict] = []
    for item in as_list(pick(payload, "dimensions", "维度")):
        if not isinstance(item, dict):
            continue
        dimension = as_str(pick(item, "dimension", "维度"))
        if not dimension:
            continue
        scores.append(
            {
                "dimension": dimension,
                "score": max(1.0, min(5.0, as_float(pick(item, "score", "评分"), default=3.0))),
                "comment": as_str(pick(item, "comment", "评语")),
            }
        )

    if not scores:
        ctx.degrade("模型评审", "评审调用没有返回可用的维度评分")
        return {}

    weak = [row["dimension"] for row in scores if row["score"] <= 2.0]
    return {
        "dimensions": scores,
        "summary": as_str(pick(payload, "summary", "总结")),
        "weakDimensions": weak,
        "averageScore": round(sum(row["score"] for row in scores) / len(scores), 2),
    }


# ============================================================
# 返工决策
# ============================================================


def decide_rework(ctx: PipelineContext, issues: list[Issue]) -> tuple[list[dict], str]:
    """把问题清单翻译成一组具体的搜索目标。返回 `(目标, 理由)`。

    三条不返工的理由，每条都写清楚，因为它们都会被打印出来：
    没问题、问题都不够严重、返工轮次已用尽。第三种尤其要说——
    "到上限了但还是有问题"和"问题已经解决了"是完全不同的两件事，
    报告里必须能区分。
    """
    if ctx.rework_rounds >= ctx.mode.max_rework_rounds:
        return [], (
            f"返工轮次已达上限（{ctx.mode.max_rework_rounds} 轮），"
            "本轮发现的问题留档不返工"
        )

    actionable = [
        issue for issue in issues if issue.severity in _REWORK_SEVERITIES and not issue.resolved
    ]
    if not actionable:
        return [], "没有达到返工门槛的问题（minor 只记录）"

    targets: list[dict] = []
    seen: set[tuple[str, str]] = set()

    # 先处理明确指向某个维度的问题。
    dimensions = [issue.dimension for issue in actionable if issue.dimension]
    # 没有指向维度的问题（比如引用类的）交给证据最稀薄的几个维度去补。
    if not dimensions:
        dimensions = weakest_dimensions(ctx, limit=2)
    else:
        # 去重但保序：同一维度的多个问题只补一次，否则预算会被
        # 同一维度反复吃掉。
        dimensions = list(dict.fromkeys(dimensions))

    for dimension in dimensions:
        brand = next(
            (issue.brand for issue in actionable if issue.dimension == dimension and issue.brand),
            "",
        )
        for keyword in _REWORK_KEYWORDS[:2]:
            key = (brand, f"{dimension} {keyword}")
            if key in seen:
                continue
            seen.add(key)
            targets.append({"brand": brand, "dimension": dimension, "keyword": keyword})

    reason = (
        f"发现 {len(actionable)} 个达到返工门槛的问题"
        f"（blocker {sum(1 for i in actionable if i.is_blocking)} 个、"
        f"major {sum(1 for i in actionable if i.severity == 'major')} 个），"
        f"针对 {len(dimensions)} 个维度补采"
    )
    return targets, reason


# ============================================================
# 质量门
# ============================================================


def quality_gate(
    ctx: PipelineContext, issues: list[Issue], review: dict
) -> dict:
    """出闸判定。**只看确定性证据，不看模型评分。**

    模型评分进报告让人读，但不参与 `passed`：它的采样噪声会让
    同一份报告在不同的运行里一会儿合格一会儿不合格，而 CI 需要的是
    一个可复现的判定。
    """
    covered = covered_dimensions(ctx)
    planned = len(ctx.dimensions) or 1
    coverage = round(len(covered) / planned, 4)

    blockers = [issue for issue in issues if issue.is_blocking]
    major = [issue for issue in issues if issue.severity == "major"]
    minor = [issue for issue in issues if issue.severity == "minor"]

    reasons: list[str] = []
    if blockers:
        reasons.append(f"{len(blockers)} 个 blocker 问题未解决")
    if coverage < MIN_PASS_COVERAGE:
        reasons.append(f"维度覆盖率 {coverage:.0%} 低于下限 {MIN_PASS_COVERAGE:.0%}")
    if not ctx.claims:
        reasons.append("没有产出任何论点")

    return {
        "passed": not reasons,
        "coverage": coverage,
        "dimensionsPlanned": planned,
        "dimensionsCovered": len(covered),
        "uncoveredDimensions": [d for d in ctx.dimensions if d not in covered],
        "blockers": len(blockers),
        "major": len(major),
        "minor": len(minor),
        "thresholds": {
            "minCoverage": MIN_PASS_COVERAGE,
            "minEvidencePerDimension": ctx.mode.min_evidence_per_dimension,
            "minIndependentSources": ctx.mode.min_independent_sources,
            "minDimensionCredibility": MIN_DIMENSION_CREDIBILITY,
        },
        "review": review,
        "failedBecause": reasons,
    }


# ============================================================
# 阶段入口
# ============================================================


async def run(ctx: PipelineContext) -> AuditResult:
    """跑一次审计。返回值告诉编排层要不要返工、返什么。"""
    ctx.begin_stage("audit")

    # 先给确定性指标拍一张快照。返工之后要跟它比，
    # 而"有没有变好"必须有一个**相同的口径**做基准。
    from app.core.analysis.metrics import compute_metrics

    ctx.issues = deterministic_audit(ctx)
    if not ctx.metrics_before:
        ctx.metrics_before = compute_metrics(ctx)

    blockers = [issue for issue in ctx.issues if issue.is_blocking]
    majors = [issue for issue in ctx.issues if issue.severity == "major"]
    for issue in blockers[:3]:
        ctx.thought(ctx.reviewer_expert, f"[质检] {issue.detail}", stage="audit")
    if len(blockers) > 3:
        ctx.thought(
            ctx.reviewer_expert,
            f"[质检] 另有 {len(blockers) - 3} 个 blocker 问题，详见审计面板。",
            stage="audit",
        )

    ctx.report_progress("audit", 0.6, f"确定性检查发现 {len(ctx.issues)} 个问题")

    review = run_review(ctx)

    targets, reason = decide_rework(ctx, ctx.issues)
    quality = quality_gate(ctx, ctx.issues, review)

    ctx.thought(
        ctx.reviewer_expert,
        f"审计完成：evidence {len(ctx.evidences)} 条、覆盖率 "
        f"{quality['coverage']:.0%}、blocker {quality['blockers']} / "
        f"major {quality['major']} / minor {quality['minor']}。"
        + (f"质量门结论：{'通过' if quality['passed'] else '未通过——' + '；'.join(quality['failedBecause'])}"),
        stage="audit",
    )

    ctx.finish_stage(
        "audit",
        status="done" if quality["passed"] else "degraded",
        detail={
            "issues": len(ctx.issues),
            "blockers": len(blockers),
            "majors": len(majors),
            "coverage": quality["coverage"],
            "rework": bool(targets),
        },
    )
    return AuditResult(
        issues=list(ctx.issues), review=review, quality=quality,
        rework_targets=targets, reason=reason,
    )
