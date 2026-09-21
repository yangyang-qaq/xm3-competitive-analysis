"""报告正文的结构与合规校验。

`validate_report` 的意义
-----------------------
它是**铁律一在系统边界上的最后一道闸**。论点在分析阶段挂了证据、
在写作阶段被引用了，但报告是一个会被导出、被转发、被别人读的文件——
一旦它离开这个进程，就没有任何东西能再保证里面每条论点的引用都真的存在。

所以出库前做一次全量校验：逐条论点查它的 `evidence_ids` 是否都能在
报告自己的证据表里找到。这一条如果过不了，报告就是不合格的，
无论它读起来多像样。

校验返回**问题列表**而不是布尔值
--------------------------------
"报告不合格"这句话没有可操作性。返回 `["claim CL-xxx 引用了不存在的证据 EV-yyy"]`
才能让人去修。CLI 把它打印出来，测试拿它做断言。
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: 报告结构版本。改了正文结构就升它——导出的 JSON 会被存档，
#: 没有版本号的话，半年后没人知道那份 JSON 该用哪版代码读。
REPORT_SCHEMA_VERSION = "1.0"

#: 章节 key → 中文标题。前端 TOC、导出 Markdown、校验都读这一张表，
#: 避免"章节标题"在三个地方各写一遍。
SECTION_LABELS: dict[str, str] = {
    "executive_summary": "执行摘要",
    "market_overview": "市场概览",
    "feature_comparison": "功能对比",
    "pricing": "定价分析",
    "user_feedback": "用户反馈",
    "conclusion": "结论与建议",
    "trends": "趋势观察",
    "swot": "SWOT 分析",
}

#: 没有正文就不该存在的章节。
_FORBIDDEN_EMPTY = frozenset({"executive_summary", "conclusion"})

#: 报告正文必须有的顶层键。
REQUIRED_KEYS: tuple[str, ...] = (
    "version",
    "subject",
    "brands",
    "mode",
    "generatedAt",
    "sections",
    "claims",
    "evidences",
    "metrics",
    "quality",
)


@dataclass
class ReportSection:
    key: str
    title: str = ""
    content: str = ""
    claim_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    degraded: bool = False
    #: 这一节是不是返工后重写的
    reworked: bool = False

    def resolved_title(self) -> str:
        return self.title or SECTION_LABELS.get(self.key, self.key)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.resolved_title(),
            "content": self.content,
            "claimIds": list(self.claim_ids),
            "evidenceIds": list(self.evidence_ids),
            "degraded": self.degraded,
            "reworked": self.reworked,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ReportSection:
        return cls(
            key=str(data.get("key", "")),
            title=str(data.get("title", "")),
            content=str(data.get("content", "")),
            claim_ids=list(data.get("claimIds") or []),
            evidence_ids=list(data.get("evidenceIds") or []),
            degraded=bool(data.get("degraded")),
            reworked=bool(data.get("reworked")),
        )


def validate_report(body: dict) -> list[str]:
    """校验一份报告正文，返回问题列表。空列表 = 合规。"""
    problems: list[str] = []

    if not isinstance(body, dict):
        return ["报告正文不是对象"]

    for key in REQUIRED_KEYS:
        if key not in body or body[key] in (None, "", [], {}):
            problems.append(f"缺少必填顶层字段 `{key}`")

    version = body.get("version")
    if version and version != REPORT_SCHEMA_VERSION:
        problems.append(
            f"报告结构版本为 {version}，当前代码期望 {REPORT_SCHEMA_VERSION}"
        )

    brands = body.get("brands") or []
    if not isinstance(brands, list):
        problems.append("`brands` 不是列表")

    # ---- 证据表 ----
    evidences = body.get("evidences") or []
    evidence_ids = {str(e.get("evidenceId") or e.get("evidence_id") or "") for e in evidences}
    evidence_ids.discard("")
    if not evidence_ids:
        problems.append("报告没有任何证据")

    # ---- 章节 ----
    sections = body.get("sections") or []
    seen_keys: set[str] = set()
    for index, section in enumerate(sections):
        key = str(section.get("key", ""))
        if not key:
            problems.append(f"第 {index + 1} 个章节缺少 key")
            continue
        if key in seen_keys:
            problems.append(f"章节 `{key}` 重复出现")
        seen_keys.add(key)
        if key not in SECTION_LABELS:
            problems.append(f"章节 `{key}` 不在已知章节表里")
        content = str(section.get("content", "")).strip()
        if not content and key in _FORBIDDEN_EMPTY:
            problems.append(f"章节「{SECTION_LABELS.get(key, key)}」正文为空")
        for eid in section.get("evidenceIds") or []:
            if eid not in evidence_ids:
                problems.append(f"章节 `{key}` 引用了不存在的证据 {eid}")

    # ---- 论点：铁律一 ----
    claims = body.get("claims") or []
    for claim in claims:
        claim_id = str(claim.get("claimId") or claim.get("claim_id") or "?")
        refs = claim.get("evidenceIds") or claim.get("evidence_ids") or []
        if not refs:
            problems.append(f"论点 {claim_id} 没有任何证据引用（违反铁律一）")
            continue
        for eid in refs:
            if eid not in evidence_ids:
                problems.append(f"论点 {claim_id} 引用了不存在的证据 {eid}")

    # ---- 图表：铁律一同样适用 ----
    for chart in body.get("charts") or []:
        chart_id = chart.get("chartId") or chart.get("id") or "?"
        if not chart.get("evidenceIds"):
            problems.append(f"图表 {chart_id} 没有证据链（违反铁律一）")

    # ---- 质量门 ----
    quality = body.get("quality") or {}
    if not isinstance(quality, dict):
        problems.append("`quality` 不是对象")
    elif "passed" not in quality:
        problems.append("`quality` 缺少 `passed` 判定")

    return problems


def summarize(body: dict) -> dict:
    """给出一份报告的概览数字。CLI 打印它，仪表盘也用它。"""
    claims = body.get("claims") or []
    evidences = body.get("evidences") or []
    sections = body.get("sections") or []
    verified = [c for c in claims if c.get("verified")]
    cross = [c for c in verified if c.get("crossValidated")]

    return {
        "subject": body.get("subject", ""),
        "brands": body.get("brands") or [],
        "mode": body.get("mode", ""),
        "sections": len(sections),
        "claims": len(claims),
        "verifiedClaims": len(verified),
        "crossValidatedClaims": len(cross),
        "evidences": len(evidences),
        "degradedEvidences": sum(1 for e in evidences if e.get("degraded")),
        "independentDomains": _count_domains(evidences),
        "charts": len(body.get("charts") or []),
        "issues": len((body.get("audit") or {}).get("issues") or []),
        "problems": len(validate_report(body)),
    }


def _count_domains(evidences: list[dict]) -> int:
    from app.core.evidence.sourcetypes import independent_domain

    return len({independent_domain(str(e.get("url", ""))) for e in evidences} - {""})
