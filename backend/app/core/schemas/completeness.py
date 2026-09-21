"""报告完整度：区分"没做"和"做了但没做出来"。

为什么不是简单的"字段填了没有"
------------------------------
参考实现用"这个字段有没有被填过"判断完整度，于是完整度恒为 100%——
模型总是会把字段填上的，填的是不是真的则是另一回事。

这里每个模块有三种状态，而不是两种：

- `filled`   有内容，且内容通过了本职检查（比如定价表里至少有一个数字）
- `degraded` 有内容，但只是勉强可用（解析失败后回退、或全部档位都没有价格）
- `missing`  完全没有

三态的意义在于**它决定了报告顶部那条横幅说什么**。"市场格局章节缺失"
和"市场格局章节只有定性描述、没有数字"对读者的含义完全不同，
前者说我们没做，后者说我们做了但没得到，两者的可信度也不同。
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: 每个模块的判定函数：返回 "filled" | "degraded" | "missing"。
#: 判据刻意写得保守——宁可报 degraded，也不要让一个空壳冒充完整。
_BLOCKS: tuple[tuple[str, str, str], ...] = (
    ("sections", "报告章节", "章节正文"),
    ("claims", "论点", "带证据引用的论点"),
    ("matrix", "对比矩阵", "功能对比矩阵"),
    ("featureTrees", "功能矩阵", "各品牌功能树"),
    ("pricingModels", "定价表", "各品牌定价档位"),
    ("personaSets", "用户画像", "各品牌画像卡"),
    ("charts", "图表", "带证据链的图表"),
    ("sentiment", "舆情", "舆情标注"),
    ("evidenceStats", "证据统计", "证据来源分布"),
)


@dataclass
class CompletenessReport:
    blocks: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def score(self) -> float:
        """完整度 = 加权得分。degraded 记半分。

        记半分而不是 0 或 1：一份"有定价章节但没抽出数字"的报告，
        比完全没有定价章节强，比抽出了数字弱。用布尔量表达不了这个区别，
        而报告顶部只显示一个数的时候，那个数必须能表达它。
        """
        if not self.blocks:
            return 0.0
        weights = {"filled": 1.0, "degraded": 0.5, "missing": 0.0}
        total = sum(weights.get(state, 0.0) for state in self.blocks.values())
        return round(total / len(self.blocks), 4)

    @property
    def is_publishable(self) -> bool:
        """能不能发出去。

        门槛只卡"完全缺失"的块，不卡 degraded：有降级但完整的报告
        是有用的（读者能看到哪些部分弱），而缺章节的报告是误导的。
        """
        return not self.missing

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "blocks": dict(self.blocks),
            "labels": dict(self.labels),
            "missing": list(self.missing),
            "degraded": list(self.degraded),
            "isPublishable": self.is_publishable,
        }


def _state_for(key: str, body: dict) -> str:
    value = body.get(key)

    if key == "sections":
        sections = value or []
        if not sections:
            return "missing"
        # 全空章节（正文为空）等价于没有章节。
        filled = [s for s in sections if str(s.get("content", "")).strip()]
        if not filled:
            return "missing"
        return "degraded" if len(filled) < len(sections) else "filled"

    if key == "claims":
        claims = value or []
        if not claims:
            return "missing"
        verified = [c for c in claims if c.get("verified")]
        if not verified:
            return "degraded"
        # 过半论点未通过交叉验证时算降级：报告成立了，但依据偏薄。
        cross = [c for c in verified if c.get("crossValidated")]
        return "filled" if len(cross) * 2 >= len(verified) else "degraded"

    if key == "matrix":
        if not value:
            return "missing"
        return "filled" if value.get("dimensions") and value.get("brands") else "degraded"

    if key == "featureTrees":
        trees = [t for t in (value or []) if t.get("categories")]
        if not trees:
            return "missing"
        return "degraded" if any(t.get("degraded") for t in trees) else "filled"

    if key == "pricingModels":
        models = [m for m in (value or []) if m.get("tiers")]
        if not models:
            return "missing"
        # 有档位但一个数字都抽不出来 = 定价表只剩定性描述。
        return "filled" if any(m.get("entryPrice") is not None for m in models) else "degraded"

    if key == "personaSets":
        sets = [s for s in (value or []) if s.get("personas")]
        if not sets:
            return "missing"
        backed = [s for s in sets if (s.get("evidenceBackedRate") or 0) > 0]
        return "filled" if len(backed) == len(sets) else "degraded"

    if key == "charts":
        charts = value or []
        if not charts:
            return "missing"
        # 铁律一：图表也是论点，没有证据链的图表不算数。
        cited = [c for c in charts if c.get("evidenceIds")]
        if not cited:
            return "degraded"
        return "filled" if len(cited) == len(charts) else "degraded"

    if key == "sentiment":
        if not value:
            return "missing"
        labels = value.get("labels") or []
        if not labels:
            return "missing"
        # 全部由规则标注（而非 LLM）时算降级：报告要披露这个混合比例。
        by_llm = value.get("labeledByLlm") or 0
        return "filled" if by_llm else "degraded"

    if key == "evidenceStats":
        if not value:
            return "missing"
        return "filled" if value.get("total") else "missing"

    return "missing" if not value else "filled"


def assess_completeness(body: dict) -> CompletenessReport:
    """给一份报告正文做完整度体检。纯函数，不花任何调用。"""
    report = CompletenessReport()
    for key, label, _description in _BLOCKS:
        state = _state_for(key, body)
        report.blocks[key] = state
        report.labels[key] = label
        if state == "missing":
            report.missing.append(label)
        elif state == "degraded":
            report.degraded.append(label)
    return report
