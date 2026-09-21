"""舆情标注。

参考实现最值得警惕的一处
------------------------
它在 LLM 漏标某条时静默回退到关键词规则函数，然后报告照样印出
「正面 62%」，读起来像是 62% 的样本被模型判断为正面。实际上其中
一部分是规则数出来的，而规则判"这家店太贵了，不推荐"为负面——
有时候对，有时候完全反。

这里的做法是**每条都带 `labeledBy`**，并在结果里给出混合比例。
报告因此可以说"本页 38 条标注中 25 条由模型完成、13 条由规则兜底"。
数字可能没那么漂亮，但它是真的。

规则兜底为什么还留着
--------------------
不留的话，模型调用失败时这一整块就是空的——而舆情恰恰是最容易
采到大量短文本的地方。留着的代价是多一层必须被披露的成分，
这个代价是划算的：披露是免费的，缺失不是。
"""
from __future__ import annotations

from collections.abc import Sequence

from app.core.models import Evidence
from app.core.pipeline.calls import chat_json
from app.core.pipeline.context import PipelineContext
from app.core.pipeline.prompts import sentiment_prompt
from app.core.schemas.base import as_int, as_list, as_str, pick

#: 只有这些来源类型算"用户评价"。官方文档里没有舆情可言——
#: 把官方介绍页拿去做情感分析，得到的一定是正面，而那条数据毫无意义。
_USER_REVIEW_TYPES = frozenset({"zhihu", "bilibili", "xiaohongshu", "douyin", "review"})

#: 单次标注的样本上限。多了会超出上下文，而且边际信息量迅速下降。
MAX_SAMPLES = 24

_POSITIVE_WORDS = (
    "好用", "推荐", "流畅", "强大", "喜欢", "方便", "值得", "优秀", "满意",
    "不错", "香", "效率高", "简洁", "值",
)
_NEGATIVE_WORDS = (
    "难用", "卡顿", "崩溃", "闪退", "贵", "不值", "垃圾", "坑", "慢",
    "复杂", "劝退", "失望", "涨价", "臃肿", "不稳定", "bug",
)

_VALID_LABELS = ("positive", "negative", "neutral")


def label_text_by_rules(text: str) -> tuple[str, str]:
    """关键词兜底。返回 `(标签, 理由)`。

    理由里带上命中的词——只写"规则判定"的话，读者无法判断这次兜底
    是否可信，而"命中了『贵』"至少给了他一个可以自行判断的依据。
    """
    lowered = (text or "").lower()
    hits_positive = [word for word in _POSITIVE_WORDS if word in lowered]
    hits_negative = [word for word in _NEGATIVE_WORDS if word in lowered]

    if hits_positive and not hits_negative:
        return "positive", f"规则命中正面词：{'、'.join(hits_positive[:3])}"
    if hits_negative and not hits_positive:
        return "negative", f"规则命中负面词：{'、'.join(hits_negative[:3])}"
    if hits_positive and hits_negative:
        # 褒贬同现：判中性。关键词法无法处理否定与转折，
        # 强行判一边的准确率低于抛硬币——而抛硬币的结果会被当成结论。
        return "neutral", "规则同时命中正负面词，无法判别"
    return "neutral", "规则未命中任何情感词"


def _sample_pool(evidences: Sequence[Evidence]) -> list[Evidence]:
    """挑出可标注的样本，优先取正文更长的。

    长正文更可能包含真实的评价，而短摘要多半是标题复述。
    这个偏好与"证据质量"是同一件事的另一面。
    """
    pool = [ev for ev in evidences if ev.source_type in _USER_REVIEW_TYPES]
    pool.sort(key=lambda ev: len(ev.full_text or ev.snippet or ""), reverse=True)
    return pool[:MAX_SAMPLES]


def label_sentiment(ctx: PipelineContext, brand: str = "") -> dict:
    """给用户评价类证据打情感标签。"""
    pool = _sample_pool(ctx.evidences)
    if not pool:
        return {
            "labels": [],
            "counts": {"positive": 0, "negative": 0, "neutral": 0},
            "labeledByLlm": 0,
            "labeledByRule": 0,
            "total": 0,
            "note": "没有采到用户评价类证据，本节为空",
        }

    payload, report = chat_json(
        ctx,
        sentiment_prompt(brand or ctx.subject, pool),
        tier=ctx.mode.analysis_tier,
        purpose="sentiment",
        max_tokens=2048,
        temperature=0.2,
        required=False,
    )

    # index → (label, reason)。只接受 0..len(pool)-1 范围内的下标，
    # 越界的丢掉：它要么是模型在乱标，要么是它在标不存在的样本。
    llm_labels: dict[int, tuple[str, str]] = {}
    for item in as_list(pick(payload, "labels", "标注")):
        if not isinstance(item, dict):
            continue
        index = as_int(pick(item, "index", "序号", "i"), default=-1)
        label = as_str(pick(item, "sentiment", "情感", "label")).strip().lower()
        if index < 0 or index >= len(pool) or label not in _VALID_LABELS:
            continue
        llm_labels[index] = (label, as_str(pick(item, "reason", "理由")))

    labels: list[dict] = []
    counts = {"positive": 0, "negative": 0, "neutral": 0}
    for index, ev in enumerate(pool):
        if index in llm_labels:
            label, reason = llm_labels[index]
            labeled_by = "llm"
        else:
            label, reason = label_text_by_rules(ev.full_text or ev.snippet)
            labeled_by = "rule"
        counts[label] += 1
        labels.append(
            {
                "evidenceId": ev.evidence_id,
                "brand": ev.brand,
                "sourceType": ev.source_type,
                "sentiment": label,
                "reason": reason,
                "labeledBy": labeled_by,
                "excerpt": (ev.title or ev.full_text or ev.snippet)[:120],
            }
        )

    by_rule = sum(1 for item in labels if item["labeledBy"] == "rule")
    by_llm = len(labels) - by_rule
    total = len(labels) or 1

    result = {
        "labels": labels,
        "counts": counts,
        "labeledByLlm": by_llm,
        "labeledByRule": by_rule,
        "total": len(labels),
        "llmRatio": round(by_llm / total, 4),
        "byBrand": _by_brand(labels),
        "note": (
            f"共标注 {len(labels)} 条，其中 {by_llm} 条由模型判断、"
            f"{by_rule} 条由关键词规则兜底"
            + ("（混合来源，比例见上）" if by_rule else "（全部由模型判断）")
        ),
        "coercion": report.to_dict(),
    }
    if by_rule:
        ctx.degrade("舆情标注", f"{by_rule}/{len(labels)} 条由关键词规则兜底（非模型判断）")
    return result


def _by_brand(labels: list[dict]) -> list[dict]:
    brands: dict[str, dict] = {}
    for item in labels:
        brand = item.get("brand") or "未标注品牌"
        row = brands.setdefault(
            brand, {"brand": brand, "positive": 0, "negative": 0, "neutral": 0, "total": 0}
        )
        row[item["sentiment"]] += 1
        row["total"] += 1
    for row in brands.values():
        row["positiveRate"] = round(row["positive"] / row["total"], 4) if row["total"] else 0.0
    return sorted(brands.values(), key=lambda row: row["total"], reverse=True)
