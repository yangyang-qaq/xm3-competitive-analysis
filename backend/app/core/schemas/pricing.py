"""定价模型。

为什么价格要同时存 `price_text` 和 `price_value`
------------------------------------------------
模型输出的是 `"68元/月"`、`"¥1,299/年"`、`"免费"`、`"按需报价"`。
下游要做三件不同的事：

- **展示**要原文（`price_text`）——把 `"¥1,299/年"` 显示成 `1299` 会丢掉单位。
- **比较**要数字（`price_value`）——算价格带、排中位数、画对比图。
- **诚实**要允许数字为空——`"按需报价"`、`"免费"` 没有数字，
  硬填 0 会让"免费"和"按需报价"在图上重合，而它们是两件完全不同的事。

只存一个的话，上面三件里必然坏掉一件。这是"一个字段服务三个消费者"
的典型失败，所以从一开始就分开。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.schemas.base import (
    CoercionReport,
    as_float,
    as_list,
    as_list_of_dicts,
    as_str,
    normalize_evidence_ids,
    pick,
)

#: 价格周期。归一化后用于把不同周期的价格折算成可比口径。
PERIODS = ("月", "年", "季", "周", "日", "一次性", "其余")

_PERIOD_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("月", ("月", "month", "mo", "/m", "每月")),
    ("年", ("年", "year", "yr", "/y", "每年", "annual")),
    ("季", ("季", "quarter", "季度")),
    ("周", ("周", "week")),
    ("日", ("日", "天", "day")),
    ("一次性", ("一次性", "买断", "永久", "one-time", "lifetime", "perpetual")),
)

#: 折算到"月"的乘数，用于算可比的月单价。
#: 一年按 12 个月算，不按 365/30.44——报价本来就是按月计的，
#: 用天文历法精度去折算一个"¥99/年"只会得到 8.25 这种没人用的数字。
_MONTHLY_FACTOR = {"月": 1.0, "年": 1.0 / 12, "季": 1.0 / 3, "周": 4.345, "日": 30.44}

#: 出现这些词说明价格不可比，`price_value` 必须留空。
_NON_NUMERIC_MARKERS = ("免费", "free", "按需", "定制", "面议", "咨询", "contact", "报价")


def normalize_period(value: object) -> str:
    text = as_str(value).strip().lower()
    if not text:
        return ""
    for period, words in _PERIOD_ALIASES:
        for word in words:
            if word in text:
                return period
    return ""


def _extract_price(raw: object, text: str) -> float | None:
    """取价格数字。识别"这根本不是个价"的情况后返回 None。"""
    lowered = text.lower()
    for marker in _NON_NUMERIC_MARKERS:
        if marker in lowered:
            # "免费" 是 0 吗？不是——它是"没有价格档位"。
            # 填 0 会让免费版和"0 元试用"在价格带上重合。
            return None
    return as_float(raw)


@dataclass
class PricingTier:
    name: str
    price_text: str = ""
    price_value: float | None = None
    currency: str = ""
    period: str = ""
    unit: str = ""
    target_user: str = ""
    includes: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)

    @property
    def monthly_value(self) -> float | None:
        """折算成月单价，用于跨周期比较。一次性买断不折算。"""
        if self.price_value is None or self.period not in _MONTHLY_FACTOR:
            return None
        return round(self.price_value * _MONTHLY_FACTOR[self.period], 2)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "priceText": self.price_text,
            "priceValue": self.price_value,
            "monthlyValue": self.monthly_value,
            "currency": self.currency,
            "period": self.period,
            "unit": self.unit,
            "targetUser": self.target_user,
            "includes": list(self.includes),
            "evidenceIds": list(self.evidence_ids),
        }


@dataclass
class PricingModel:
    brand: str
    currency: str = ""
    model_type: str = ""
    free_tier: str = ""
    tiers: list[PricingTier] = field(default_factory=list)
    degraded: bool = False

    @property
    def numeric_tiers(self) -> list[PricingTier]:
        return [t for t in self.tiers if t.price_value is not None]

    @property
    def entry_price(self) -> float | None:
        """最低的**付费**档月单价。免费档不参与——入口价问的是
        "想用起来最少要花多少"，免费档回答不了这个问题。"""
        values = [t.monthly_value for t in self.numeric_tiers if t.monthly_value]
        return min(values) if values else None

    @property
    def has_numeric(self) -> bool:
        return bool(self.numeric_tiers)

    def to_dict(self) -> dict:
        return {
            "brand": self.brand,
            "currency": self.currency,
            "modelType": self.model_type,
            "freeTier": self.free_tier,
            "tiers": [t.to_dict() for t in self.tiers],
            "entryPrice": self.entry_price,
            "degraded": self.degraded,
        }


def parse_pricing(
    payload: object,
    *,
    brand: str = "",
    known_ids: set[str] | None = None,
    report: CoercionReport | None = None,
) -> PricingModel:
    """解析一个品牌的定价模型。永不抛错。"""
    report = report or CoercionReport()
    if not isinstance(payload, dict):
        report.note("定价模型不是对象，已丢弃")
        return PricingModel(brand=brand, degraded=True)

    resolved_brand = as_str(pick(payload, "brand", "品牌", "product"), default=brand)
    currency = as_str(pick(payload, "currency", "币种", "货币"))
    model_type = as_str(pick(payload, "modelType", "model_type", "计费方式", "模式"))
    free_tier = as_str(pick(payload, "freeTier", "free_tier", "免费额度", "免费版"))

    raw_tiers = as_list_of_dicts(
        pick(payload, "tiers", "档位", "plans", "定价档位", "items"), "tiers", "plans", "items"
    )

    tiers: list[PricingTier] = []
    for raw in raw_tiers:
        name = as_str(pick(raw, "name", "档位", "plan", "tier", "版本"))
        if not name:
            continue
        price_text = as_str(pick(raw, "price", "价格", "priceText", "amount"))
        period = normalize_period(pick(raw, "period", "周期", "billing", "计价周期"))
        ids, _ = normalize_evidence_ids(
            pick(raw, "evidenceIds", "evidence_ids", "证据"), known_ids, report=report
        )
        tiers.append(
            PricingTier(
                name=name,
                price_text=price_text,
                price_value=_extract_price(pick(raw, "priceValue", "price", "价格"), price_text),
                currency=as_str(pick(raw, "currency", "币种"), default=currency),
                period=period,
                unit=as_str(pick(raw, "unit", "单位", "per")),
                target_user=as_str(pick(raw, "targetUser", "target_user", "target", "适用对象")),
                includes=[as_str(x) for x in as_list(pick(raw, "includes", "包含", "features")) if as_str(x)],
                evidence_ids=ids,
            )
        )

    if not tiers:
        report.note("定价档位为空")
        report.missing.append("pricingModel.tiers")
        return PricingModel(
            brand=resolved_brand, currency=currency, model_type=model_type,
            free_tier=free_tier, degraded=True,
        )

    return PricingModel(
        brand=resolved_brand,
        currency=currency,
        model_type=model_type,
        free_tier=free_tier,
        tiers=tiers,
    )


def compare_entry_prices(models: list[PricingModel]) -> dict:
    """跨品牌入口价对比。

    **只比较有数字的**，并在结果里如实报出"哪些品牌没有可比价格"。
    把 `"按需报价"` 当成 0 参与比较，会得到一个"某厂商是全场最低价"的
    结论——而这个结论完全来自我们自己的填充，不来自任何证据。
    """
    comparable = [m for m in models if m.has_numeric and m.entry_price is not None]
    incomparable = [m.brand for m in models if m.entry_price is None]
    prices = sorted((m.entry_price, m.brand) for m in comparable) if comparable else []
    return {
        "comparable": [{"brand": m.brand, "entryPrice": m.entry_price} for m in comparable],
        "notComparable": incomparable,
        "cheapest": prices[0][1] if prices else "",
        "mostExpensive": prices[-1][1] if prices else "",
        "note": (
            "仅统计能解析出数字的档位；"
            f"{len(incomparable)} 个品牌因价格为非数字（免费/按需报价）未参与比较"
            if incomparable else "全部品牌均有可比价格"
        ),
    }
