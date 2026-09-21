"""确定性指标。

全部由代码算出，**不读模型的自述，也不花任何 API 调用**。
这是报告里最该被相信的一部分：任何一个数字出问题都能一路追到某行代码，
而不是"模型说的"。

优先于 LLM-as-judge 指标
------------------------
引用忠实度（模型判模型）很有用，但它有噪声：同一个输入换一次采样
可能得到不同的判断。而幻觉引用率、维度覆盖率、返工提升这些数是
**确定性的**——同样的输入永远得到同样的值，所以它们可以进 CI 做回归门禁。
判官指标只能进文档。

一个定义上的坑：维度覆盖率
--------------------------
参考实现判"这个维度被覆盖了没有"的方式是看这个维度的字段填了没有，
于是覆盖率恒为 100%。这里的分母是**计划维度数**，分子是
**有证据锚定的维度数**，判据在 `collect.dimension_coverage()`——
只认证据，不认模型有没有写。
"""
from __future__ import annotations

from app.core.evidence.citations import MIN_INDEPENDENT_DOMAINS
from app.core.evidence.sourcetypes import independent_domain
from app.core.pipeline.calls import stats_of
from app.core.pipeline.collect import covered_dimensions, dimension_coverage
from app.core.pipeline.context import PipelineContext


def compute_metrics(ctx: PipelineContext) -> dict:
    """算出本次运行的确定性指标。"""
    evidences = ctx.evidences
    domains = {independent_domain(ev.url) for ev in evidences} - {""}
    platforms = {ev.source_type for ev in evidences if ev.source_type}

    coverage = dimension_coverage(ctx)
    covered = covered_dimensions(ctx)
    planned = len(ctx.dimensions) or 1

    claims = ctx.claims
    verified = [c for c in claims if c.verified]
    cross = [c for c in verified if c.cross_validated]

    citation = ctx.citation_report
    call_stats = stats_of(ctx)
    trace_metrics = ctx.tracer.metrics()

    degraded = [ev for ev in evidences if ev.degraded]

    return {
        # ---- 铁律一 ----
        "claims": len(claims),
        "verifiedClaims": len(verified),
        "unsupportedClaimRate": (
            round(1.0 - len(verified) / len(claims), 4) if claims else 0.0
        ),
        "hallucinationRate": getattr(citation, "hallucination_rate", 0.0),
        "phantomCitations": len(getattr(citation, "phantom_ids", []) or []),
        # ---- 铁律二 ----
        "crossValidatedClaims": len(cross),
        "crossValidationRate": (
            round(len(cross) / len(verified), 4) if verified else 0.0
        ),
        "independentDomains": len(domains),
        "minIndependentDomains": MIN_INDEPENDENT_DOMAINS,
        # ---- 证据与维度 ----
        "evidences": len(evidences),
        "degradedEvidences": len(degraded),
        "degradedRate": round(len(degraded) / len(evidences), 4) if evidences else 0.0,
        "platformCount": len(platforms),
        "platforms": sorted(platforms),
        "dimensionsPlanned": len(ctx.dimensions),
        "dimensionsCovered": len(covered),
        "dimensionCoverage": round(len(covered) / planned, 4),
        "dimensionCoverageDetail": coverage,
        # ---- 采集过程 ----
        "searchCalls": call_stats.search_calls,
        "searchErrors": call_stats.search_errors,
        "rawHits": call_stats.raw_hits,
        "filteredHits": ctx.filtered_hits,
        "siteFilterFolded": call_stats.site_filter_folded,
        "fetchedOk": ctx.fetch_outcome.ok,
        "fetchedDegraded": ctx.fetch_outcome.degraded,
        "fetchSkippedByBudget": ctx.fetch_outcome.skipped_budget,
        # ---- 成本与耗时 ----
        "llmCalls": call_stats.llm_calls,
        "llmOptionalFailures": call_stats.llm_optional_failures,
        "totalCostUsd": trace_metrics["totalCostUsd"],
        "totalTokens": trace_metrics["totalTokens"],
        "slowestCallMs": trace_metrics["slowestCallMs"],
        "durationMs": ctx.elapsed_ms(),
        "firstEvidenceMs": ctx.first_evidence_ms,
        # ---- 返工 ----
        "reworkRounds": ctx.rework_rounds,
        "issues": len(ctx.issues),
    }


def rework_delta(before: dict, after: dict) -> dict:
    """返工前后的指标对比。

    这是铁律三（返工闭环）唯一可量化的证据。没有它，
    "返工会带来提升"只是一句信念——而信念驱动的返工很容易变成
    "多跑几轮以求安心"，把成本花在没有收益的地方。

    `improved` 的判据是**证据与覆盖**这两组数，不是成本或耗时：
    返工一定会让花费上升，把成本算进"是否改善"会让结论永远是"没有"。
    """
    keys = (
        "dimensionsCovered", "evidences", "independentDomains",
        "crossValidationRate", "dimensionCoverage", "verifiedClaims",
    )
    changes = {
        key: {
            "before": before.get(key, 0),
            "after": after.get(key, 0),
            "delta": _delta(before.get(key, 0), after.get(key, 0)),
        }
        for key in keys
    }
    improved = (
        changes["dimensionsCovered"]["delta"] > 0
        or changes["dimensionCoverage"]["delta"] > 0
        or changes["independentDomains"]["delta"] > 0
    )
    return {
        "before": {key: before.get(key, 0) for key in keys},
        "after": {key: after.get(key, 0) for key in keys},
        "changes": changes,
        "costDelta": round(
            float(after.get("totalCostUsd", 0)) - float(before.get("totalCostUsd", 0)), 6
        ),
        "improved": improved,
    }


def _delta(before: object, after: object) -> float:
    try:
        return round(float(after) - float(before), 4)  # type: ignore[arg-type]
    except (TypeError, ValueError):  # pragma: no cover - 指标都是数值
        return 0.0
