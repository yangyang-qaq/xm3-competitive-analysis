"""阶段 1：需求理解。

一句话需求里通常缺少两样东西：**调研对象到底是哪个**，以及**要做多深**。
这一步把它们补上，并在信息真的不够时停下来问。

澄清的门槛是刻意设高的
----------------------
问三个问题会让用户多花一分钟，答完才发现其中一个问题靠搜索就能解决，
是很差的体验。所以启发式只在一句话**明显地**含糊时才触发
（"帮我看看那个笔记软件"），而"对比 Notion 与 Obsidian"这种
已经说清楚对象的需求不会被拦下来问。

判断错了的代价也不对称：该问而没问，最坏结果是报告偏题（读者能看出来）；
不该问而问了，用户会直接关掉页面。所以宁可少问。
"""
from __future__ import annotations

from app.core.pipeline.calls import chat_json
from app.core.pipeline.context import PipelineContext
from app.core.pipeline.prompts import clarify_prompt, scope_prompt
from app.core.schemas.base import as_list, as_str, pick
from app.providers.errors import ProviderError

#: 含糊表述。出现这些词说明用户在描述一个意图而不是一个对象。
_VAGUE_MARKERS = (
    "帮我看看", "了解一下", "调研一下", "看看", "研究一下",
    "对比一下", "有什么推荐", "怎么样", "哪个好", "值不值得",
)

#: 短于这个长度的需求几乎一定缺信息。
_MIN_QUERY_CHARS = 12


def needs_clarification(query: str) -> bool:
    """要不要先问几个问题。

    纯函数、可单测。放在这里而不是内联进 `run`，
    是因为它是这一步唯一有对错的决策，而调用模型的那部分没有。
    """
    text = (query or "").strip()
    if len(text) < _MIN_QUERY_CHARS:
        return True
    return any(marker in text for marker in _VAGUE_MARKERS)


def _clean_brands(raw: object, subject: str, limit: int) -> list[str]:
    """归一化候选品牌：去重、去空、截到档位上限。

    `subject` 排在最前——它是用户明确提到的对象，不该被模型给的
    候选顺序挤掉。参考实现直接信模型的顺序，于是用户问的产品
    有时会从对比里消失，而报告看起来依然完整。
    """
    brands: list[str] = []
    for item in as_list(raw):
        name = as_str(item)
        if name and name not in brands:
            brands.append(name)
    if subject and subject not in brands:
        brands.insert(0, subject)
    elif subject and brands and brands[0] != subject:
        brands.remove(subject)
        brands.insert(0, subject)
    return brands[:limit]


async def run(ctx: PipelineContext) -> None:
    ctx.begin_stage("intake")
    lead = ctx.lead_expert
    ctx.thought(lead, f"收到调研需求：「{ctx.query}」。先确认调研对象和范围。", stage="intake")

    payload, report = chat_json(
        ctx,
        scope_prompt(ctx.query),
        tier="fast",
        purpose="scope",
        max_tokens=1024,
    )
    ctx.coercion.merge(report)

    if not payload:
        raise ProviderError("需求理解阶段没有拿到可解析的结果，无法继续")

    ctx.subject = as_str(pick(payload, "subject", "调研对象"), default=ctx.query)
    ctx.domain = as_str(pick(payload, "domain", "领域"))
    ctx.category = as_str(pick(payload, "category", "品类"))
    ctx.scope_rationale = as_str(pick(payload, "rationale", "理由"))

    candidates = as_list(pick(payload, "candidateBrands", "candidate_brands", "候选竞品"))
    ctx.candidate_brands = [as_str(item) for item in candidates if as_str(item)]
    ctx.brands = _clean_brands(candidates, ctx.subject, ctx.mode.max_brands)

    if len(ctx.brands) < 2:
        # 只有一个品牌就谈不上"对比"。补上调研对象自身，
        # 让下游的阶段至少有两条腿——而不是让它们各自去处理单品牌特例。
        ctx.degrade("竞品识别", f"只识别出 {len(ctx.brands)} 个品牌，已退化为主品牌单点分析")

    ctx.thought(
        lead,
        f"调研对象：{ctx.subject}（{ctx.domain}／{ctx.category}）。"
        f"待对比品牌：{'、'.join(ctx.brands)}。{ctx.scope_rationale}",
        stage="intake",
    )

    # ---- 澄清 ----
    if needs_clarification(ctx.query) and not ctx.clarify_answers:
        questions_payload, question_report = chat_json(
            ctx,
            clarify_prompt(ctx.query, payload),
            tier="fast",
            purpose="clarify",
            max_tokens=1024,
            required=False,
        )
        ctx.coercion.merge(question_report)
        questions = as_list(pick(questions_payload, "questions", "问题"))
        ctx.clarify_questions = [
            {
                "id": as_str(pick(q, "id", "编号")) or f"q{index + 1}",
                "question": as_str(pick(q, "question", "问题", "text")),
                "kind": as_str(pick(q, "kind", "类型"), default="single"),
                "options": [as_str(o) for o in as_list(pick(q, "options", "选项"))],
                "recommended": as_str(pick(q, "recommended", "推荐")),
            }
            for index, q in enumerate(questions)
            if isinstance(q, dict)
        ]
        ctx.need_clarify = bool(ctx.clarify_questions)

        if ctx.need_clarify and ctx.auto_clarify:
            # 自动模式：采用推荐项并把这件事记进报告。
            # 不能静默采用——报告读者有权知道这次调研用了默认假设。
            ctx.clarify_answers = {
                q["id"]: q["recommended"] or (q["options"][0] if q["options"] else "")
                for q in ctx.clarify_questions
            }
            ctx.degrade(
                "需求澄清",
                "自动采用了推荐选项（未人工确认），共 "
                f"{len(ctx.clarify_answers)} 项",
            )
            ctx.thought(
                lead,
                "需求缺少若干关键信息，已按推荐项采用默认设置，并在报告中标注。",
                stage="intake",
            )

    ctx.finish_stage(
        "intake",
        detail={
            "subject": ctx.subject,
            "brands": ctx.brands,
            "needClarify": ctx.need_clarify,
        },
    )
