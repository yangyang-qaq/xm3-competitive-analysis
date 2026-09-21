"""功能矩阵：某产品有哪些能力、到什么程度。

为什么"支持程度"是一个枚举而不是布尔值
--------------------------------------
布尔值会强迫模型在所有模糊地带选边：一个"高级档位才有"的能力，
填"支持"是误导（免费用户得不到），填"不支持"也是误导（产品确实有）。
`partial` 让这种最常见的真实情况有个位置可放。

`unknown` 是必须存在的第五档。模型在被要求判断时会倾向于猜，
而"这条能力没有任何证据"与"这条能力不支持"在报告里必须能区分——
前者是调研的缺口，后者是产品的结论。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.schemas.base import (
    CoercionReport,
    as_list_of_dicts,
    as_str,
    normalize_evidence_ids,
    pick,
)

#: 支持程度的归一化词表。模型会用各种写法表达同一件事。
_SUPPORT_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("full", ("full", "yes", "supported", "支持", "完全支持", "是", "有", "具备",
              "✅", "✓", "true", "1", "完备", "完整")),
    ("partial", ("partial", "partly", "部分", "部分支持", "有限", "受限", "基础版",
                 "付费可用", "高级版", "⚠️", "~", "beta", "内测")),
    ("none", ("none", "no", "not supported", "不支持", "无", "没有", "否", "缺失",
              "❌", "✗", "false", "0", "未提供")),
)

SUPPORT_LEVELS = ("full", "partial", "none", "unknown")

#: 每个档位的得分，用于算覆盖率。`unknown` 不给分——
#: 这正是"维度覆盖率"修复后的口径：没有证据的能力不算覆盖。
_SUPPORT_SCORE = {"full": 1.0, "partial": 0.5, "none": 0.0, "unknown": 0.0}

#: 加权覆盖率里各档的权重。`none` 计入分母（问了并得到否定答案，
#: 是有效信息），`unknown` 不计入分母（没问到，不该拉低覆盖率也
#: 不该拉高）。这个区分是覆盖率不再恒为 100% 的关键。
_KNOWN_LEVELS = ("full", "partial", "none")


def normalize_support(value: object) -> str:
    text = as_str(value).strip().lower()
    if not text:
        return "unknown"
    for level, words in _SUPPORT_ALIASES:
        for word in words:
            if word in text:
                return level
    return "unknown"


@dataclass
class Feature:
    name: str
    support: str = "unknown"
    note: str = ""
    evidence_ids: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        return _SUPPORT_SCORE.get(self.support, 0.0)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "support": self.support,
            "note": self.note,
            "evidenceIds": list(self.evidence_ids),
        }


@dataclass
class FeatureCategory:
    category: str
    features: list[Feature] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"category": self.category, "features": [f.to_dict() for f in self.features]}


@dataclass
class FeatureTree:
    brand: str
    categories: list[FeatureCategory] = field(default_factory=list)
    degraded: bool = False

    def features(self) -> list[Feature]:
        return [f for cat in self.categories for f in cat.features]

    @property
    def coverage(self) -> float:
        """已确证的能力占"问到的能力"的比例。

        分母只算模型给出了明确判断的（full/partial/none），
        不算 unknown。否则一个什么都不确定的功能表也能报出 100%。
        """
        known = [f for f in self.features() if f.support in _KNOWN_LEVELS]
        if not known:
            return 0.0
        return round(sum(f.score for f in known) / len(known), 4)

    @property
    def unknown_count(self) -> int:
        return sum(1 for f in self.features() if f.support == "unknown")

    @property
    def unsupported_with_evidence(self) -> int:
        """被标为"不支持"却挂着证据的条目数。

        这几乎总是模型搞错了：证据一般是用来支持某个存在的功能的。
        不直接改数据（可能是真的"第三方评测说它没有"），只计数，
        让审计阶段能把它当成一条可疑信号报出来。
        """
        return sum(1 for f in self.features() if f.support == "none" and f.evidence_ids)

    def to_dict(self) -> dict:
        return {
            "brand": self.brand,
            "categories": [c.to_dict() for c in self.categories],
            "coverage": self.coverage,
            "unknownCount": self.unknown_count,
            "degraded": self.degraded,
        }


def parse_feature_tree(
    payload: object,
    *,
    brand: str = "",
    known_ids: set[str] | None = None,
    report: CoercionReport | None = None,
) -> FeatureTree:
    """解析一个品牌的功能矩阵。永不抛错。"""
    report = report or CoercionReport()
    if not isinstance(payload, dict):
        report.note("功能矩阵不是对象，已丢弃")
        return FeatureTree(brand=brand, degraded=True)

    resolved_brand = as_str(pick(payload, "brand", "品牌", "product", "产品"), default=brand)
    raw_categories = as_list_of_dicts(
        pick(payload, "categories", "功能分类", "groups", "tree", "items"),
        "categories", "items", "features",
    )

    categories: list[FeatureCategory] = []
    for raw_category in raw_categories:
        # 模型有时直接给一个扁平的功能列表，没有分类。把它们统一归到
        # "未分类"而不是丢掉——丢掉会让整棵树的覆盖率变成 0。
        category_name = as_str(pick(raw_category, "category", "name", "分类", "类别"))
        raw_features = as_list_of_dicts(
            pick(raw_category, "features", "功能", "items", "capabilities"),
            "features", "items",
        )
        if not raw_features and "features" not in raw_category and "功能" not in raw_category:
            raw_features = [raw_category]
            category_name = category_name or "未分类"

        features: list[Feature] = []
        for raw_feature in raw_features:
            name = as_str(pick(raw_feature, "name", "feature", "功能", "title"))
            if not name:
                continue
            ids, _ = normalize_evidence_ids(
                pick(raw_feature, "evidenceIds", "evidence_ids", "证据"), known_ids, report=report
            )
            features.append(
                Feature(
                    name=name,
                    support=normalize_support(
                        pick(raw_feature, "support", "支持", "level", "supported")
                    ),
                    note=as_str(pick(raw_feature, "note", "备注", "detail", "description")),
                    evidence_ids=ids,
                )
            )
        if features:
            categories.append(FeatureCategory(category_name or "未分类", features))

    if not categories:
        report.note("功能矩阵为空")
        report.missing.append("featureTree.categories")
        return FeatureTree(brand=resolved_brand, degraded=True)

    return FeatureTree(brand=resolved_brand, categories=categories)


def merge_feature_trees(trees: list[FeatureTree]) -> dict:
    """把多品牌的功能树并成一张对比表。

    对齐用的是功能名的**归一化字符串**：模型对不同品牌很可能给出
    "多视图"和"多视图（表格/看板）"两个名字，直接按原名对齐会得到
    一张几乎全是空格的表。归一化只去掉括号内容与空白，不做同义词归并——
    同义词归并需要语义理解，用一个规则表去猜会比不做更糟。
    """
    import re

    def align(name: str) -> str:
        return re.sub(r"[（(].*?[)）]|\s+", "", name).lower()

    rows: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for tree in trees:
        for feature in tree.features():
            key = align(feature.name)
            if key not in rows:
                rows[key] = {"feature": feature.name, "brands": {}}
                order.append(key)
            rows[key]["brands"][tree.brand] = feature.support

    brands = [tree.brand for tree in trees]
    return {
        "brands": brands,
        "features": [
            {"feature": rows[key]["feature"], "support": {b: rows[key]["brands"].get(b, "unknown") for b in brands}}
            for key in order
        ],
    }
