"""领域模型。

四个核心对象：`Evidence`（证据）、`Claim`（论点）、`Issue`（质检问题）、`Envelope`（专家间消息）。

关于 `full_text` 为什么必须是显式字段
------------------------------------
参考实现把网页正文挂在证据对象**之外**（一个 `_full_text` 属性），因为 dataclass 的
`asdict()` 只取字段，非字段属性会被静默丢掉。后果是：任何经过一次数据库往返的证据
都会**丢失正文**——而正文恰恰是判断"这条证据到底说了什么、引证是否忠实"的唯一依据。
这里它是字段，代价是序列化体积变大。这个代价是划算的：没有正文，
报告就只能展示摘要，而摘要支撑的论点和正文支撑的论点不是一个可信度等级。

关于 id 的生成
--------------
`evidence_id` 由 URL 摘要生成（`EV-<12 位十六进制>`），而不是自增序号。
同一个 URL 在任何任务、任何轮次里都得到同一个 id，于是去重、
"这条论点引用的证据和那条是不是同一份"都退化成字符串比较，
而不需要维护一张映射表。
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

#: 论点的置信度。三档而不是百分数：模型给出的百分数没有校准依据，
#: 而"高/中/低"是它真的能区分的东西。
Confidence = Literal["high", "medium", "low"]

#: 质检问题的严重程度。blocker 会触发返工，minor 只记录。
Severity = Literal["blocker", "major", "minor"]

#: 证据的来源类型。这是可信度评分的主要输入。
SourceType = Literal[
    "official", "news", "review", "zhihu", "bilibili", "xiaohongshu",
    "douyin", "web", "unknown",
]


def evidence_id_for(url: str) -> str:
    """由 URL 生成稳定的证据 id。

    不用自增序号的理由：序号依赖"采集顺序"，而采集是并发扇出的，
    顺序在不同运行之间不稳定。用 URL 摘要则同一份证据永远同名，
    去重、跨轮次复用、返工前后对比都变成字符串比较。
    """
    return f"EV-{hashlib.sha256(url.strip().encode('utf-8')).hexdigest()[:12]}"


def _either(data: dict, camel: str, snake: str, default: Any = None) -> Any:
    """先取 camelCase，退回 snake_case。

    序列化出去的是 camelCase，但这份 dict 会被写进 JSON 列落库，
    于是字段名同时是**持久化格式**。读得宽一点，改名就不需要迁移。
    """
    if camel in data:
        return data[camel]
    return data.get(snake, default)


def claim_id_for(text: str, index: int) -> str:
    """论点 id。文本 + 序号：同一批分析里文本相同的论点不该合并，
    因为它们可能来自不同维度。"""
    digest = hashlib.sha256(f"{index}:{text}".encode()).hexdigest()[:10]
    return f"CL-{digest}"


# ============================================================
# 证据
# ============================================================


@dataclass
class CredibilityBreakdown:
    """可信度的分项明细。

    参考实现只返回一个裸整数，于是"这条为什么 82 分"在界面和报告里都说不出来。
    把明细一起返回，等于把评分从"一个数字"变成"一个可以质疑的判断"。
    """

    source_type_score: float = 0.0
    freshness_score: float = 0.0
    content_score: float = 0.0
    cross_ref_score: float = 0.0
    penalties: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        """总分 = 四个分项之和 + 扣分项，钳制到 0–100。

        恒等式 `total == 各项之和` 有一条测试守着。它重要的原因：
        只要明细和总分可能对不上，明细就不再是"解释"，而只是
        "另一组数字"——读者没法用明细去复核总分，可解释性就落空了。
        """
        raw = (
            self.source_type_score
            + self.freshness_score
            + self.content_score
            + self.cross_ref_score
            + self.penalties
        )
        return round(max(0.0, min(100.0, raw)), 2)

    def to_dict(self) -> dict:
        # `total` 不是字段，但必须出现在序列化结果里：前端与报告都用它，
        # 让它们各自去求和是重复实现同一套策略，迟早会有一处漏改扣分项。
        return {
            "sourceTypeScore": self.source_type_score,
            "freshnessScore": self.freshness_score,
            "contentScore": self.content_score,
            "crossRefScore": self.cross_ref_score,
            "penalties": self.penalties,
            "notes": list(self.notes),
            "total": self.total,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> CredibilityBreakdown:
        """同时接受 camelCase 与 snake_case。

        兼容两种是因为数据库里存的是这份 dict 的历史版本：
        JSON 落库之后字段名就成了持久化格式，改名的代价是迁移。
        这里读得宽一点，存的那份就不用动。
        """
        data = data or {}
        return cls(
            source_type_score=float(data.get("sourceTypeScore", data.get("source_type_score", 0.0))),
            freshness_score=float(data.get("freshnessScore", data.get("freshness_score", 0.0))),
            content_score=float(data.get("contentScore", data.get("content_score", 0.0))),
            cross_ref_score=float(data.get("crossRefScore", data.get("cross_ref_score", 0.0))),
            penalties=float(data.get("penalties", 0.0)),
            notes=list(data.get("notes") or []),
        )


@dataclass
class Evidence:
    """一条采集到的证据。

    这是整个系统的地基：铁律一是"无证据不立论"，所以论点的价值上限
    就是它所依据的证据的质量。
    """

    evidence_id: str
    url: str
    title: str = ""
    snippet: str = ""
    #: 网页正文。显式字段，理由见模块 docstring。
    full_text: str = ""
    brand: str = ""
    source_type: str = "unknown"
    site_name: str = ""
    published_at: str = ""
    captured_at: str = ""

    #: 命中哪些调研维度。**这是维度覆盖率的唯一真相源。**
    #: 参考实现用"这个维度有没有字段被填过"来判断覆盖，
    #: 那个判据会让覆盖率恒为 100%——字段总是会被模型填上的。
    matched_dimensions: list[str] = field(default_factory=list)

    query: str = ""
    provider: str = ""
    rank: int = 0

    credibility: float = 0.0
    credibility_breakdown: CredibilityBreakdown = field(default_factory=CredibilityBreakdown)

    #: 正文抽取失败、只有摘要。它直接扣可信度，所以必须显式。
    degraded: bool = False
    images: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        """序列化成 camelCase。

        显式列出每个键而不是跑一个通用的 snake→camel 转换：前端类型
        （`types/domain.ts`）是按这份键名手写的，通用转换会让"新增一个
        字段"自动出现在接口里，而前端类型不会跟着变——两边就此开始漂移，
        且没有任何机制会提醒。
        """
        return {
            "evidenceId": self.evidence_id,
            "url": self.url,
            "title": self.title,
            "snippet": self.snippet,
            "fullText": self.full_text,
            "brand": self.brand,
            "sourceType": self.source_type,
            "siteName": self.site_name,
            "publishedAt": self.published_at,
            "capturedAt": self.captured_at,
            "matchedDimensions": list(self.matched_dimensions),
            "query": self.query,
            "provider": self.provider,
            "rank": self.rank,
            "credibility": self.credibility,
            "credibilityBreakdown": self.credibility_breakdown.to_dict(),
            "degraded": self.degraded,
            "images": list(self.images),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Evidence:
        """读 camelCase，同时兼容 snake_case。"""
        return cls(
            evidence_id=_either(data, "evidenceId", "evidence_id", ""),
            url=data.get("url", ""),
            title=data.get("title", ""),
            snippet=data.get("snippet", ""),
            full_text=_either(data, "fullText", "full_text", "") or "",
            brand=data.get("brand", ""),
            source_type=_either(data, "sourceType", "source_type", "unknown"),
            site_name=_either(data, "siteName", "site_name", "") or "",
            published_at=_either(data, "publishedAt", "published_at", "") or "",
            captured_at=_either(data, "capturedAt", "captured_at", "") or "",
            matched_dimensions=list(
                _either(data, "matchedDimensions", "matched_dimensions", []) or []
            ),
            query=data.get("query", ""),
            provider=data.get("provider", ""),
            rank=int(data.get("rank", 0) or 0),
            credibility=float(data.get("credibility", 0.0) or 0.0),
            credibility_breakdown=CredibilityBreakdown.from_dict(
                _either(data, "credibilityBreakdown", "credibility_breakdown")
            ),
            degraded=bool(data.get("degraded", False)),
            images=list(data.get("images", []) or []),
        )

    @property
    def domain(self) -> str:
        """独立信源的判据。铁律二要求"≥2 个独立域名"，
        所以跨平台计数之前必须先归一到域名。"""
        from urllib.parse import urlparse

        try:
            host = urlparse(self.url).hostname or ""
        except ValueError:  # pragma: no cover - 畸形 URL
            return ""
        return host[4:] if host.startswith("www.") else host


# ============================================================
# 论点
# ============================================================


@dataclass
class Claim:
    """一条论点。铁律一要求它必须挂证据。"""

    claim_id: str
    text: str
    confidence: Confidence = "medium"
    evidence_ids: list[str] = field(default_factory=list)
    brand: str = ""
    dimension: str = ""
    section: str = ""

    #: 下面两个字段由 `evidence/citations.py` 依据**实际存在的证据**计算，
    #: 不采信模型的自我陈述。模型说"我引用了 3 条"和这 3 条真的存在是两回事。
    verified: bool = False
    cross_validated: bool = False
    independent_domains: int = 0
    #: 模型输出里出现过、但系统里不存在的 evidence_id。这是幻觉引用率的分子。
    phantom_evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "claimId": self.claim_id,
            "text": self.text,
            "confidence": self.confidence,
            "evidenceIds": list(self.evidence_ids),
            "brand": self.brand,
            "dimension": self.dimension,
            "section": self.section,
            "verified": self.verified,
            "crossValidated": self.cross_validated,
            "independentDomains": self.independent_domains,
            "phantomEvidenceIds": list(self.phantom_evidence_ids),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Claim:
        return cls(
            claim_id=_either(data, "claimId", "claim_id", ""),
            text=data.get("text", ""),
            confidence=data.get("confidence", "medium"),
            evidence_ids=list(_either(data, "evidenceIds", "evidence_ids", []) or []),
            brand=data.get("brand", ""),
            dimension=data.get("dimension", ""),
            section=data.get("section", ""),
            verified=bool(data.get("verified", False)),
            cross_validated=bool(_either(data, "crossValidated", "cross_validated", False)),
            independent_domains=int(
                _either(data, "independentDomains", "independent_domains", 0) or 0
            ),
            phantom_evidence_ids=list(
                _either(data, "phantomEvidenceIds", "phantom_evidence_ids", []) or []
            ),
        )


# ============================================================
# 质检问题
# ============================================================

#: 质检问题类型。每种都对应一条明确的返工动作，否则问题清单就只是抱怨。
ISSUE_KINDS = {
    "thin_evidence": "某个维度的证据密度不足",
    "missing_dimension": "计划里的维度完全没有证据",
    "uncited_claim": "论点没有可核验的证据引用",
    "single_source": "论点只有一个独立信源，未通过交叉验证",
    "low_credibility": "该维度证据平均可信度过低",
    "phantom_citation": "论点引用了不存在的证据",
    "stale_evidence": "该维度证据过于陈旧",
    "empty_section": "章节没有实质内容",
    "degraded_source": "该维度证据以抓取失败后的摘要为主",
}


@dataclass
class Issue:
    """一条质检问题。

    与"日志里的一行警告"的区别：Issue 带 `suggestion` 与 `targets`，
    所以它可以驱动一次具体的返工——补采哪个维度的哪些 query。
    """

    issue_id: str
    kind: str
    severity: Severity = "minor"
    dimension: str = ""
    brand: str = ""
    detail: str = ""
    suggestion: str = ""
    #: 返工时要补的搜索角度
    targets: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    resolved: bool = False

    def to_dict(self) -> dict:
        return {
            "issueId": self.issue_id,
            "kind": self.kind,
            "kindLabel": ISSUE_KINDS.get(self.kind, self.kind),
            "severity": self.severity,
            "dimension": self.dimension,
            "brand": self.brand,
            "detail": self.detail,
            "suggestion": self.suggestion,
            "targets": list(self.targets),
            "evidenceIds": list(self.evidence_ids),
            "resolved": self.resolved,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Issue:
        return cls(
            issue_id=_either(data, "issueId", "issue_id", ""),
            kind=data.get("kind", ""),
            severity=data.get("severity", "minor"),
            dimension=data.get("dimension", ""),
            brand=data.get("brand", ""),
            detail=data.get("detail", ""),
            suggestion=data.get("suggestion", ""),
            targets=list(data.get("targets") or []),
            evidence_ids=list(_either(data, "evidenceIds", "evidence_ids", []) or []),
            resolved=bool(data.get("resolved", False)),
        )

    @property
    def is_blocking(self) -> bool:
        return self.severity == "blocker"


def make_issue(kind: str, **kwargs: Any) -> Issue:
    """构造 Issue 并生成 id。id 由 kind + 维度 + 细节摘要决定，
    这样同一轮里重复发现的问题不会被记成两条。"""
    seed = f"{kind}:{kwargs.get('dimension', '')}:{kwargs.get('brand', '')}:{kwargs.get('detail', '')}"
    issue_id = f"IS-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:10]}"
    return Issue(issue_id=issue_id, kind=kind, **kwargs)


# ============================================================
# 专家间消息
# ============================================================


@dataclass
class Envelope:
    """专家之间传递的一条消息。

    刻意保留 `sender` / `recipient` 而不只是内容：三层专家分工的可解释性
    依赖于"谁把什么交给了谁"，决策回放要画的就是这张图。
    """

    sender: str
    recipient: str
    kind: Literal["request", "result", "issue", "handoff"] = "result"
    payload: dict = field(default_factory=dict)
    trace_id: str = ""
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================
# 任务与报告
# ============================================================


@dataclass
class TaskRecord:
    task_id: str
    query: str
    mode: str = "quick"
    status: str = "pending"
    stage: str = ""
    progress: float = 0.0
    need_clarify: bool = False
    clarify_questions: list[dict] = field(default_factory=list)
    clarify_answers: dict = field(default_factory=dict)
    subject: str = ""
    brands: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "taskId": self.task_id,
            "query": self.query,
            "mode": self.mode,
            "status": self.status,
            "stage": self.stage,
            "progress": self.progress,
            "needClarify": self.need_clarify,
            "clarifyQuestions": list(self.clarify_questions),
            "clarifyAnswers": dict(self.clarify_answers),
            "subject": self.subject,
            "brands": list(self.brands),
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "error": self.error,
        }


@dataclass
class ReportRecord:
    """一份报告。

    `data` 里放整份报告 JSON，另加所有需要筛选排序的标量列。
    这是刻意的：一次访问只读一份报告，读放大无所谓；拆成十几张规范化表
    只会换来一堆 JOIN。代价（无法用 SQL 直接按报告内部字段筛选）写进技术栈文档的"已知代价"。
    """

    report_id: str
    task_id: str
    query: str = ""
    mode: str = "quick"
    subject: str = ""
    brands: list[str] = field(default_factory=list)
    generated_at: str = ""
    #: 整份报告正文
    data: dict = field(default_factory=dict)
    #: 确定性指标（不花 LLM 就能算出来的那些）
    metrics: dict = field(default_factory=dict)
    #: 质量门结果
    quality: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "reportId": self.report_id,
            "taskId": self.task_id,
            "query": self.query,
            "mode": self.mode,
            "subject": self.subject,
            "brands": list(self.brands),
            "generatedAt": self.generated_at,
            "data": dict(self.data),
            "metrics": dict(self.metrics),
            "quality": dict(self.quality),
        }
