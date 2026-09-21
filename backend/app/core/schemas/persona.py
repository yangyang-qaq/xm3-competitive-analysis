"""用户画像卡。

一个容易做错的地方
------------------
画像卡读起来最像"分析"，实际上却是最容易被模型编出来的东西——
"效率型知识工作者，注重协作与检索"这种句子可以套在任何产品上，
而且它读起来很专业，所以最不容易被质疑。

对策不是不生成，而是要求**每条画像都挂证据**：如果一条画像没有任何
用户评价类的证据支撑（知乎讨论、应用商店评论、B站评测），
那它大概率是从产品定位反推出来的，而不是从用户嘴里听来的。
`evidence_backed_rate` 就是用来暴露这件事的。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.schemas.base import (
    CoercionReport,
    as_list,
    as_list_of_dicts,
    as_str,
    normalize_evidence_ids,
    pick,
)

#: 迁移成本档位。`unknown` 同样保留：没提到不等于低。
MIGRATION_COSTS = ("低", "中", "高", "unknown")

_COST_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("低", ("低", "容易", "简单", "low", "easy", "无缝")),
    ("中", ("中", "一般", "medium", "moderate")),
    ("高", ("高", "困难", "难", "high", "hard", "锁定", "lock-in")),
)


def normalize_migration_cost(value: object) -> str:
    text = as_str(value).strip().lower()
    if not text:
        return "unknown"
    for level, words in _COST_ALIASES:
        for word in words:
            if word in text:
                return level
    return "unknown"


@dataclass
class Persona:
    name: str
    segment: str = ""
    needs: list[str] = field(default_factory=list)
    scenarios: list[str] = field(default_factory=list)
    pain_points: list[str] = field(default_factory=list)
    decision_factors: list[str] = field(default_factory=list)
    migration_cost: str = "unknown"
    evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "segment": self.segment,
            "needs": list(self.needs),
            "scenarios": list(self.scenarios),
            "painPoints": list(self.pain_points),
            "decisionFactors": list(self.decision_factors),
            "migrationCost": self.migration_cost,
            "evidenceIds": list(self.evidence_ids),
        }


@dataclass
class PersonaSet:
    brand: str
    personas: list[Persona] = field(default_factory=list)
    degraded: bool = False

    @property
    def evidence_backed_rate(self) -> float:
        """有证据支撑的画像占比。这个数字低于 1.0 时，
        报告里的画像卡应当被当成"假设"而不是"发现"。"""
        if not self.personas:
            return 0.0
        backed = sum(1 for p in self.personas if p.evidence_ids)
        return round(backed / len(self.personas), 4)

    def to_dict(self) -> dict:
        return {
            "brand": self.brand,
            "personas": [p.to_dict() for p in self.personas],
            "evidenceBackedRate": self.evidence_backed_rate,
            "degraded": self.degraded,
        }


def _str_list(value: object) -> list[str]:
    return [text for text in (as_str(item) for item in as_list(value)) if text]


def parse_personas(
    payload: object,
    *,
    brand: str = "",
    known_ids: set[str] | None = None,
    report: CoercionReport | None = None,
) -> PersonaSet:
    """解析一个品牌的用户画像。永不抛错。"""
    report = report or CoercionReport()
    if not isinstance(payload, dict):
        report.note("用户画像不是对象，已丢弃")
        return PersonaSet(brand=brand, degraded=True)

    resolved_brand = as_str(pick(payload, "brand", "品牌", "product"), default=brand)
    raw_personas = as_list_of_dicts(
        pick(payload, "personas", "画像", "users", "用户", "items"),
        "personas", "items", "users",
    )

    personas: list[Persona] = []
    for raw in raw_personas:
        name = as_str(pick(raw, "name", "画像名", "title", "类型"))
        if not name:
            continue
        ids, _ = normalize_evidence_ids(
            pick(raw, "evidenceIds", "evidence_ids", "证据"), known_ids, report=report
        )
        personas.append(
            Persona(
                name=name,
                segment=as_str(pick(raw, "segment", "细分", "人群", "group")),
                needs=_str_list(pick(raw, "needs", "需求", "jobs")),
                scenarios=_str_list(pick(raw, "scenarios", "场景", "useCases")),
                pain_points=_str_list(pick(raw, "painPoints", "pain_points", "痛点", "抱怨")),
                decision_factors=_str_list(
                    pick(raw, "decisionFactors", "decision_factors", "决策因素")
                ),
                migration_cost=normalize_migration_cost(
                    pick(raw, "migrationCost", "migration_cost", "迁移成本")
                ),
                evidence_ids=ids,
            )
        )

    if not personas:
        report.note("用户画像为空")
        report.missing.append("userPersona.personas")
        return PersonaSet(brand=resolved_brand, degraded=True)

    return PersonaSet(brand=resolved_brand, personas=personas)
