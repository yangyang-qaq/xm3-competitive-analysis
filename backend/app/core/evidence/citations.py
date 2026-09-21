"""引用校验：铁律一与铁律二的执行点。

不采信模型的自我陈述
--------------------
模型会在输出里写"我引用了 3 条证据"。这 3 条到底存不存在，
是**另一件事**。参考实现的做法是照单全收，于是幻觉引用会一路走到报告里，
读者点开引用链接发现什么都没有——这比没有引用更糟，因为它假装有证据。

这里只认一件事：这个 id 在本次任务的证据表里能不能查到。
查不到的就是幻觉，记进 `phantom_evidence_ids`，并从该论点的引用里剔除。

两个比率的意义不同，别混
------------------------
- **幻觉引用率** = 编造的 id ÷ 模型输出的 id 总数。衡量**模型的可靠性**。
- **无证据立论率** = 校验后仍无有效引用的论点 ÷ 论点总数。衡量**报告的可信度**。

前者高说明模型爱编，后者高说明这份报告不该发。
一个模型可能幻觉率很低但论点依然普遍没引用（它干脆不写引用），
所以两个数必须分开报。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from app.core.evidence.sourcetypes import independent_domain
from app.core.models import Claim, Evidence

#: 通过交叉验证需要的独立域名数。档位可以调高，但不能低于 2——
#: 「≥2 个独立信源」是铁律二的原文，降到 1 等于取消这条铁律。
MIN_INDEPENDENT_DOMAINS = 2


@dataclass
class CitationReport:
    """一次引用校验的结果。全部是确定性指标，不花任何 API 调用。"""

    total_claims: int = 0
    verified_claims: int = 0
    cross_validated_claims: int = 0
    #: 模型输出的 evidence_id 总数（含重复）
    produced_ids: int = 0
    #: 其中真实存在的（去重后）
    resolved_ids: int = 0
    #: 编造的 id（去重后，便于直接展示给用户）
    phantom_ids: list[str] = field(default_factory=list)
    #: 论点 → 它实际用到的独立域名数，供返工阶段定位问题
    domains_by_claim: dict[str, int] = field(default_factory=dict)

    @property
    def unsupported_claims(self) -> int:
        return self.total_claims - self.verified_claims

    @property
    def hallucination_rate(self) -> float:
        """幻觉引用率 = 1 − 有效 id ÷ 模型产出的 id。

        分母是 0 时返回 0 而不是 1：模型一条引用都没写，这属于
        "无证据立论"的问题，不属于"编造引用"，记成 1 会把两类错误混起来。
        """
        if self.produced_ids <= 0:
            return 0.0
        return round(1.0 - self.resolved_ids / self.produced_ids, 4)

    @property
    def unsupported_rate(self) -> float:
        if self.total_claims <= 0:
            return 0.0
        return round(self.unsupported_claims / self.total_claims, 4)

    @property
    def cross_validation_rate(self) -> float:
        if self.total_claims <= 0:
            return 0.0
        return round(self.cross_validated_claims / self.total_claims, 4)

    def to_dict(self) -> dict:
        return {
            "totalClaims": self.total_claims,
            "verifiedClaims": self.verified_claims,
            "crossValidatedClaims": self.cross_validated_claims,
            "unsupportedClaims": self.unsupported_claims,
            "producedIds": self.produced_ids,
            "resolvedIds": self.resolved_ids,
            "phantomIds": list(self.phantom_ids),
            "hallucinationRate": self.hallucination_rate,
            "unsupportedRate": self.unsupported_rate,
            "crossValidationRate": self.cross_validation_rate,
        }


def cross_validate(
    evidence_ids: Iterable[str],
    evidence_index: Mapping[str, Evidence],
    *,
    min_domains: int = MIN_INDEPENDENT_DOMAINS,
) -> tuple[int, bool]:
    """数一个论点背后的独立域名。

    域名取自 URL 主机名，不是站点名——站点名是搜索源给的字符串，
    同一个站点在不同搜索源里可能叫"知乎"或"Zhihu"，那样计数会被拆散。
    """
    domains = {
        independent_domain(evidence_index[eid].url)
        for eid in evidence_ids
        if eid in evidence_index
    }
    domains.discard("")
    count = len(domains)
    return count, count >= min_domains


def enforce_citations(
    claims: Sequence[Claim],
    evidence_index: Mapping[str, Evidence],
    *,
    min_domains: int = MIN_INDEPENDENT_DOMAINS,
) -> CitationReport:
    """就地校验并标注每个论点，返回整体报告。

    就地修改而不是返回新列表：论点对象接下来还要被写作阶段引用，
    换一批对象会让"我在报告里看到的这条"和"我校验过的那条"失去同一性。

    调用方负责**过滤**（`[c for c in claims if c.verified]`）。
    函数本身不丢东西——丢弃是策略，策略应该写在流水线的阶段代码里，
    而不是藏在一个叫"校验"的函数里。
    """
    report = CitationReport(total_claims=len(claims))
    seen_produced: set[str] = set()
    seen_resolved: set[str] = set()
    phantoms: list[str] = []

    for claim in claims:
        produced = [eid for eid in claim.evidence_ids if eid]
        resolved = [eid for eid in produced if eid in evidence_index]
        phantom = [eid for eid in produced if eid not in evidence_index]

        report.produced_ids += len(produced)
        seen_produced.update(produced)
        seen_resolved.update(resolved)
        for eid in phantom:
            if eid not in phantoms:
                phantoms.append(eid)

        claim.phantom_evidence_ids = phantom
        claim.evidence_ids = resolved

        domains, passed = cross_validate(resolved, evidence_index, min_domains=min_domains)
        claim.independent_domains = domains
        claim.cross_validated = passed
        claim.verified = bool(resolved)

        if claim.verified:
            report.verified_claims += 1
        if claim.cross_validated:
            report.cross_validated_claims += 1
        report.domains_by_claim[claim.claim_id] = domains

    report.phantom_ids = phantoms
    # 用去重后的数量算比率：同一个编造的 id 被 5 条论点引用，
    # 是"一个幻觉"而不是"5 个幻觉"，按后者算会让比率随论点数量虚高。
    report.produced_ids = len(seen_produced)
    report.resolved_ids = len(seen_resolved)
    return report


def prune_unverified(
    claims: Sequence[Claim], *, keep_uncited: bool = False
) -> tuple[list[Claim], list[Claim]]:
    """把论点分成"可入报告"和"被剔除"两堆。

    `keep_uncited=False`（默认）时无引用论点直接剔除：报告里一条
    "据我分析"式的断言，对读者的价值是负的——它占用注意力却不提供依据。

    `keep_uncited=True` 保留但它们带着 `verified=False` 的标记，
    用于返工前的对比：返工要看的正是"哪些论点还没有依据"。
    """
    kept = [c for c in claims if c.verified or keep_uncited]
    dropped = [c for c in claims if not c.verified and not keep_uncited]
    return kept, dropped
