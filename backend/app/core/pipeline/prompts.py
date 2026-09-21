"""提示词。

集中在一个文件里的理由：**引用规则只写一遍**。
"只能引用下面列出的 evidence_id"这句话必须出现在每一个产出论点的
提示词里，抄在五个地方的话，改一处漏四处，而漏掉的那处会开始
稳定地产生幻觉引用——且只在那个阶段出现，看起来像模型的问题。

行文用中文，因为调研语料与输出都是中文。用英文提示词再要求中文输出
会引入一次不必要的翻译，而翻译正是结构化字段最容易出偏差的环节。
"""
from __future__ import annotations

from collections.abc import Sequence

from app.core.models import Claim, Evidence

#: 所有提示词共享的系统前缀。
SYSTEM_BASE = (
    "你是一家专业竞争情报机构的分析师。你的输出会被直接写进一份要交付给客户的报告，"
    "因此必须满足三条硬性要求：\n"
    "1. **无证据不立论**：每一条结论都必须引用给定的 evidence_id，不得引用未列出的 id，"
    "也不得凭常识补充。\n"
    "2. **不猜**：材料里没有的信息，写「未获取到」而不是填一个看起来合理的值。\n"
    "3. **区分事实与推断**：事实直接陈述，推断要写明依据。\n"
)

#: 结构化输出的通用约束。
JSON_RULE = (
    "\n只输出一个 JSON 对象，不要任何解释性文字、不要 markdown 代码围栏。"
    "所有文本值使用中文。"
)


def evidence_block(
    evidences: Sequence[Evidence],
    *,
    max_chars: int = 14_000,
    max_full_text: int = 700,
    per_brand: bool = False,
) -> str:
    """把证据渲染成提示词里的材料块，带长度上限。

    **必须限长**，而且要按"每条截断"而不是"末尾丢弃"：
    末尾丢弃会让靠后的维度完全没有材料，模型于是对它们写"未获取到"——
    看起来像调研没找到，实际是我们没给它看。逐条截断则每个维度
    都至少有一部分材料，不足是均匀分布的，更接近真实情况。

    每条都带 `evidence_id`——这是模型唯一被允许引用的标识。
    """
    lines: list[str] = []
    used = 0
    for ev in evidences:
        header = f"[{ev.evidence_id}] {ev.title or '(无标题)'}"
        if per_brand and ev.brand:
            header = f"[{ev.evidence_id}] ({ev.brand}) {ev.title or '(无标题)'}"
        meta = f"    来源：{ev.site_name or ev.domain}｜类型：{ev.source_type}｜发布：{ev.published_at or '未知'}"
        body = (ev.full_text or ev.snippet or "").strip()
        if len(body) > max_full_text:
            body = body[:max_full_text] + "…"
        chunk = f"{header}\n{meta}\n    内容：{body}\n"
        if used + len(chunk) > max_chars:
            lines.append(f"…（另有 {len(evidences) - len(lines)} 条证据因长度上限未列出）")
            break
        lines.append(chunk)
        used += len(chunk)
    return "\n".join(lines)


def evidence_id_list(evidences: Sequence[Evidence], *, max_ids: int = 400) -> str:
    ids = [ev.evidence_id for ev in evidences][:max_ids]
    return "、".join(ids)


def _messages(system: str, user: str) -> list[dict]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ============================================================
# 阶段 1：需求理解
# ============================================================


def scope_prompt(query: str) -> list[dict]:
    return _messages(
        SYSTEM_BASE,
        f"用户的调研需求：{query}\n\n"
        "请把它拆解成一次可执行的竞品调研：\n"
        "- subject：调研对象（用户真正想了解的那个产品/品类）\n"
        "- domain：所属领域\n"
        "- category：所属品类，用于扩展候选竞品\n"
        "- candidateBrands：3–6 个值得对比的品牌（含调研对象自身）。"
        "选直接竞品，不要选只是沾边的。\n"
        "- rationale：一句话说明为什么是这几个\n\n"
        '输出格式：{"subject": "", "domain": "", "category": "", '
        '"candidateBrands": [], "rationale": ""}' + JSON_RULE,
    )


def clarify_prompt(query: str, scope: dict) -> list[dict]:
    return _messages(
        SYSTEM_BASE,
        f"调研需求：{query}\n"
        f"已初步识别：调研对象={scope.get('subject', '')}，"
        f"领域={scope.get('domain', '')}，候选竞品={scope.get('candidateBrands', [])}\n\n"
        "这个需求里还有哪些点会显著改变调研方向？给出 2–4 个最关键的问题。\n"
        "每个问题给出 2–4 个选项和一个推荐项（recommended）。"
        "如果问题只能自由作答，kind 用 text 且 options 为空数组。\n"
        "**不要问能靠公开信息查到的事**（比如某产品的价格）。"
        "要问的是意图与边界，比如聚焦哪个层面、读者是谁、有没有必须覆盖的维度。\n\n"
        '输出格式：{"questions": [{"id": "q1", "question": "", "kind": "single", '
        '"options": [], "recommended": ""}]}' + JSON_RULE,
    )


# ============================================================
# 阶段 2：专家调度
# ============================================================


def plan_prompt(query: str, scope: dict, answers: dict, roster: str) -> list[dict]:
    answers_text = "\n".join(f"- {k}: {v}" for k, v in answers.items()) or "（无补充）"
    return _messages(
        SYSTEM_BASE,
        f"调研需求：{query}\n"
        f"调研对象：{scope.get('subject', '')}\n"
        f"候选竞品：{scope.get('candidateBrands', [])}\n"
        f"用户的补充说明：\n{answers_text}\n\n"
        f"可调用的专家名册：\n{roster}\n\n"
        "请规划这次调研：\n"
        "- brands：最终要对比的品牌（2–5 个）\n"
        "- dimensions：调研维度（3–8 个）。维度要**互相独立且都能落到证据上**——"
        "「好用程度」这种维度没法用证据回答，不要写。\n"
        "- searchAngles：每个维度对应的搜索角度（4–8 个），"
        "要覆盖官方信息、第三方评测、用户社区三类来源。\n"
        "- rationale：一句话说明维度为什么是这些\n\n"
        '输出格式：{"subject": "", "brands": [], "dimensions": [], '
        '"searchAngles": [], "rationale": ""}' + JSON_RULE,
    )


def dispatch_prompt(query: str, dimensions: list[str], roster: str) -> list[dict]:
    return _messages(
        SYSTEM_BASE,
        f"调研需求：{query}\n调研维度：{dimensions}\n\n"
        f"可调用专家名册：\n{roster}\n\n"
        "请从名册中挑出这次调研的团队：\n"
        "- lead：1 位决策层专家（L3），负责定边界与把关结论\n"
        "- strategists：2–3 位战略层专家（L2），负责分析研判\n"
        "- executors：2–4 位执行层专家（L1），负责采集与专项调研\n\n"
        "**只能从名册里给出的 id 中挑选**，不要编造 id。\n"
        "id 只写编号本身（`L3-002`），**不要把名册里的岗位名一起抄进来**。\n\n"
        '输出格式：{"lead": "", "strategists": [], "executors": [], "rationale": ""}' + JSON_RULE,
    )


# ============================================================
# 阶段 3–4：分析与研判
# ============================================================


def claims_prompt(
    query: str, brands: list[str], dimensions: list[str], evidences: Sequence[Evidence]
) -> list[dict]:
    return _messages(
        SYSTEM_BASE,
        f"调研需求：{query}\n对比品牌：{brands}\n调研维度：{dimensions}\n\n"
        f"证据材料：\n{evidence_block(evidences, per_brand=True)}\n\n"
        f"可引用的 evidence_id 只有：{evidence_id_list(evidences)}\n\n"
        "请提炼 6–12 条论点。每条论点：\n"
        "- 只针对一个维度、一个品牌\n"
        "- 必须有 1–3 个 evidence_id 支撑，**id 必须来自上面列出的清单**\n"
        "- confidence：high 表示多个独立来源一致；medium 表示单一可靠来源；"
        "low 表示线索但不确定\n"
        "- 没有证据支撑的判断，**不要写进 claims**\n\n"
        '输出格式：{"claims": [{"text": "", "confidence": "high", "brand": "", '
        '"dimension": "", "evidenceIds": []}]}' + JSON_RULE,
    )


def comparison_prompt(
    query: str, brands: list[str], dimensions: list[str], evidences: Sequence[Evidence]
) -> list[dict]:
    return _messages(
        SYSTEM_BASE,
        f"调研需求：{query}\n对比品牌：{brands}\n调研维度：{dimensions}\n\n"
        f"证据材料：\n{evidence_block(evidences)}\n\n"
        f"可引用的 evidence_id 只有：{evidence_id_list(evidences)}\n\n"
        "请产出结构性分析。每一项都必须挂证据；没有证据的项**留空数组**而不是编。\n"
        "- matrix：功能评分矩阵。scores 是二维数组，顺序与 brands 一致，"
        "每行对应 dimensions 里的一个维度，取值 1–5\n"
        "- marketShare：市场份额。**只有找到明确数据才填**，"
        "basis 写清推算依据；没有就留空\n"
        "- fiveForces：波特五力，intensity 1–5\n"
        "- trends：趋势序列，points 里是 {period, value}\n\n"
        '输出格式：{"matrix": {"dimensions": [], "brands": [], "scores": [], "evidenceIds": []}, '
        '"marketShare": [], "fiveForces": [], "trends": []}' + JSON_RULE,
    )


def structured_prompt(
    query: str, brand: str, dimensions: list[str], evidences: Sequence[Evidence]
) -> list[dict]:
    return _messages(
        SYSTEM_BASE,
        f"调研需求：{query}\n目标品牌：{brand}\n调研维度：{dimensions}\n\n"
        f"该品牌的证据材料：\n{evidence_block(evidences, per_brand=True)}\n\n"
        f"可引用的 evidence_id 只有：{evidence_id_list(evidences)}\n\n"
        f"请为「{brand}」产出三块结构化信息：\n"
        "- featureTree：功能树。support 只能取 full / partial / none / unknown 四值之一。"
        "**没有证据的能力填 unknown，不要猜**。\n"
        "- pricingModel：定价。price 用原文（如「68元/月」），"
        "免费版与按需报价照实写，没有数字就写文字\n"
        "- userPersona：用户画像。痛点必须来自用户评价类证据，不要从产品定位反推\n\n"
        '输出格式：{"featureTree": {"brand": "", "categories": [{"category": "", '
        '"features": [{"name": "", "support": "", "note": "", "evidenceIds": []}]}]}, '
        '"pricingModel": {"brand": "", "currency": "", "modelType": "", "freeTier": "", '
        '"tiers": [{"name": "", "price": "", "period": "", "unit": "", "targetUser": "", '
        '"includes": [], "evidenceIds": []}]}, '
        '"userPersona": {"brand": "", "personas": [{"name": "", "segment": "", "needs": [], '
        '"scenarios": [], "painPoints": [], "decisionFactors": [], "migrationCost": "", '
        '"evidenceIds": []}]}}' + JSON_RULE,
    )


def sentiment_prompt(brand: str, evidences: Sequence[Evidence]) -> list[dict]:
    items = "\n".join(
        f"{index}. {ev.title}｜{(ev.full_text or ev.snippet or '')[:200]}"
        for index, ev in enumerate(evidences)
    )
    return _messages(
        SYSTEM_BASE,
        f"以下是关于「{brand}」的用户评价类材料（编号从 0 开始）：\n{items}\n\n"
        "请逐条判断情感倾向。sentiment 只能取 positive / negative / neutral 之一，"
        "reason 用一句话说明依据。**只判断用户表达了什么，不要评价产品本身**。\n\n"
        '输出格式：{"labels": [{"index": 0, "sentiment": "", "reason": ""}]}' + JSON_RULE,
    )


# ============================================================
# 阶段 5：质检
# ============================================================


def review_prompt(
    query: str, brands: list[str], dimensions: list[str], summary: str
) -> list[dict]:
    """`summary` 是**已经排版好的文本**，不是 dict。

    直接插值一个 dict 会把 Python 的 repr 塞进提示词（`{'证据': 3}`，
    带引号和花括号），模型得先反解这层语法才能读到内容。
    排版交给调用方，提示词只管往里放。
    """
    return _messages(
        SYSTEM_BASE,
        f"调研需求：{query}\n对比品牌：{brands}\n调研维度：{dimensions}\n\n"
        f"本次调研的确定性统计：\n{summary}\n\n"
        "请对每个维度给出 1–5 分的质量评分与一句评语。评分依据："
        "证据条数、独立信源数、平均可信度、是否有交叉验证。\n"
        "**不要因为「维度写到了」就给高分**，要看证据本身。\n\n"
        '输出格式：{"dimensions": [{"dimension": "", "score": 3, "comment": ""}], '
        '"summary": ""}' + JSON_RULE,
    )


# ============================================================
# 阶段 6：报告撰写
# ============================================================


def section_prompt(
    section: str,
    title: str,
    query: str,
    brands: list[str],
    claims: Sequence[Claim],
    evidences: Sequence[Evidence],
) -> list[dict]:
    claim_lines = "\n".join(
        f"- [{c.claim_id}] ({c.brand}／{c.dimension}｜{c.confidence}) {c.text}"
        for c in claims
    ) or "（本节没有可用论点）"
    return _messages(
        SYSTEM_BASE,
        f"报告章节：{title}\n调研需求：{query}\n对比品牌：{brands}\n\n"
        f"可用论点：\n{claim_lines}\n\n"
        f"证据材料：\n{evidence_block(evidences, max_full_text=400)}\n\n"
        f"可引用的 evidence_id 只有：{evidence_id_list(evidences)}\n\n"
        "请写出这一章的正文：\n"
        "- 300–600 字，分 2–4 段\n"
        "- 每个判断后面用 `[证据: EV-xxxx]` 的形式标注来源，id 必须来自上面的清单\n"
        "- 与本节无关的论点不要硬塞进来\n"
        "- 材料不足的地方直接写「当前公开材料未覆盖」，不要用常识补\n"
        "- 不要写「本章将……」「综上所述」这类空转的过渡句\n\n"
        "只输出正文，不要标题、不要 markdown 标题符号。",
    )


def refine_prompt(
    title: str, content: str, annotation: str, evidences: Sequence[Evidence]
) -> list[dict]:
    return _messages(
        SYSTEM_BASE,
        f"报告章节：{title}\n\n当前正文：\n{content}\n\n"
        f"用户批注：{annotation}\n\n"
        f"补充证据材料：\n{evidence_block(evidences, max_full_text=500)}\n\n"
        f"可引用的 evidence_id 只有：{evidence_id_list(evidences)}\n\n"
        "请按批注重写这一章：\n"
        "- 保留原章节里仍然成立的内容，不要整段推倒\n"
        "- 新补的证据要真的用上，并标注 `[证据: EV-xxxx]`\n"
        "- 字数与原文相当\n\n"
        "只输出正文。",
    )
