"""证据处理：来源分类、可信度、相关性、正文质量、抓取编排、引用校验。

这个包是四条铁律里前两条的实现位置：
- 铁律一（无证据不立论）→ `citations.enforce_citations`
- 铁律二（交叉验证）    → `citations.cross_validate` + `sourcetypes.independent_domain`

另外三个模块（可信度、相关性、正文质量）本来是同一个问题的三个侧面：
**这条材料值不值得作为论点的依据**。分成三个文件是因为它们的失败模式不同——
相关性错了是"采到了无关的东西"，可信度错了是"采到了不可信的东西"，
正文质量错了是"采到了空壳"。混在一起时，一个错误会伪装成另一个。
"""
from app.core.evidence.citations import (
    CitationReport,
    cross_validate,
    enforce_citations,
)
from app.core.evidence.credibility import score_evidence
from app.core.evidence.fetch import FetchOutcome, fetch_many
from app.core.evidence.relevance import RelevanceVerdict, judge_relevance
from app.core.evidence.sourcetypes import (
    SOURCE_TYPE_BASE,
    classify_source,
    independent_domain,
)
from app.core.evidence.textquality import TextQuality, assess_text

__all__ = [
    "SOURCE_TYPE_BASE",
    "CitationReport",
    "FetchOutcome",
    "RelevanceVerdict",
    "TextQuality",
    "assess_text",
    "classify_source",
    "cross_validate",
    "enforce_citations",
    "fetch_many",
    "independent_domain",
    "judge_relevance",
    "score_evidence",
]
