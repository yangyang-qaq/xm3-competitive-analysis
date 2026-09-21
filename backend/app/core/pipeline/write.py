"""阶段 7：报告撰写。

写作放在最后，因为它是**最贵的一步**：每一章一次调用，每次都要带一份
证据材料块。所以前面所有能提前发现问题的检查（审计、返工）都排在它之前。

章节并发，但引用校验回到主线程做
--------------------------------
章节之间互不依赖，串行写等于把延迟乘以章节数。并发跑，但**引用解析
回到主线程**：`ctx.coercion` 是一个共享的记账对象，多线程同时 merge
会让"幻觉引用数"这个指标随调度顺序变化——而它恰恰是要进 CI 做门禁的。

模型写的引用必须逐条核对
------------------------
提示词里写死了"只能引用清单里的 id"，但模型仍然会编。编出来的 id
如果原样留在正文里，报告就会展示一个**指向不存在证据的角标**——
读者点开是空的，或者更糟，指向别人的材料。

这里的处理是：解析正文里所有 `[证据: EV-xxxx]`，把不存在的从正文里
**删掉**（保留角标比没有角标更糟），把存在的记进 `section.evidence_ids`，
并把删除数计入幻觉引用率。
"""
from __future__ import annotations

import asyncio
import re

from app.core.evidence.sourcetypes import independent_domain
from app.core.models import Claim, Evidence
from app.core.pipeline.calls import chat_text
from app.core.pipeline.context import PipelineContext
from app.core.pipeline.prompts import refine_prompt, section_prompt
from app.core.schemas.base import CoercionReport
from app.core.schemas.report import SECTION_LABELS, ReportSection

#: 正文里的引用角标。容忍带方括号与不带方括号两种写法——
#: 提示词给的是前者，但模型有时会写成 `[EV-abc123]`。
#:
#: **必须 `IGNORECASE`**：id 的形状是 `EV-`（大写前缀）+ 小写十六进制，
#: 而模型会把大小写改掉（把整串转小写是最常见的一种"整理格式"）。
#: 正则若只认大写的 `EV-`，一个被写成 `ev-a1b2c3d4e5f6` 的合法引用就
#: **既匹配不上、也进不了幻觉计数**——它会原样留在正文里，看起来像引用，
#: 点开是空的。这正是 `resolve_citations` 的折叠表要修的那件事，
#: 而折叠表只有在正则两个方向都认的前提下才有意义。
_CITATION_RE = re.compile(
    r"\[?\s*(?:证据|evidence)?\s*[:：]?\s*(EV-[0-9a-fA-F]{6,})\s*\]?",
    re.IGNORECASE,
)

#: 每章最多带多少条论点进提示词。章节数量 × 全量论点很快撑爆上下文，
#: 而排在前面的那些本来就最相关（见 `_claims_for_section`）。
MAX_CLAIMS_PER_SECTION = 20


#: 章节 → 维度关键词。**这是字符串匹配，不是语义匹配**，它只用来
#: 把更相关的论点排到前面（避免每章都从同一批论点开头读起）；
#: 一条都没匹配上就退回全量。所以匹配错了也不会漏内容，只会少一点相关性。
_SECTION_DIMENSION_HINTS: dict[str, tuple[str, ...]] = {
    "market_overview": ("市场", "份额", "竞争", "格局", "规模"),
    "feature_comparison": ("功能", "能力", "特性", "集成", "生态", "性能"),
    "pricing": ("定价", "价格", "成本", "收费", "套餐"),
    "user_feedback": ("口碑", "评价", "反馈", "用户", "体验"),
    "trends": ("趋势", "增长", "变化", "前景"),
    "swot": ("优势", "劣势", "机会", "威胁", "壁垒"),
    "conclusion": ("结论", "建议", "定位", "差异"),
}


# ============================================================
# 引用解析
# ============================================================


def resolve_citations(text: str, known: set[str]) -> tuple[str, list[str], list[str]]:
    """解析正文里的引用角标。

    返回 `(清洗后的正文, 有效 id, 不存在的 id)`。

    **不存在的角标会被从正文里删掉**，而不是留着。留着的话，报告里
    会出现一个指向空处的角标——它比"这里没有引用"更糟，因为它看起来
    是有据可查的。删除会让这一句在读者眼里回到"没有依据"的状态，
    而这个状态是诚实的。

    返回的 id 列表去重且保持出现顺序：同一份证据在一章里被引用三次
    只算一次，否则"引用了多少证据"这个数会随文风波动。

    大小写按**折叠表**处理，不能整体转小写
    --------------------------------------
    id 的形状是 `EV-`（大写前缀）+ 小写十六进制。整体转小写会得到
    `ev-xxxx`，而系统里的 id 是 `EV-xxxx`——于是每一次看起来合法的引用
    都会被判成幻觉引用，报告里所有章节的引用全部消失，而幻觉率显示 0%
    （因为被删掉的角标不计入"模型输出的引用"口径）。
    折叠成一张 `小写 → 规范写法` 的表，取回的是系统里的那个 id。
    """
    canonical = {eid.lower(): eid for eid in known}
    valid: list[str] = []
    phantom: list[str] = []
    seen_valid: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        resolved = canonical.get(match.group(1).lower())
        if resolved is None:
            raw = match.group(1)
            if raw not in phantom:
                phantom.append(raw)
            return ""
        if resolved not in seen_valid:
            seen_valid.add(resolved)
            valid.append(resolved)
        return match.group(0)

    cleaned = _CITATION_RE.sub(replace, text or "")
    # 删掉角标后可能留下多余空格与空行，收一下。
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, valid, phantom


def _known_ids(ctx: PipelineContext) -> set[str]:
    return ctx.known_evidence_ids


# ============================================================
# 论点筛选
# ============================================================


def _claims_for_section(ctx: PipelineContext, key: str) -> list[Claim]:
    """挑出与这一章最相关的论点，相关的排前面。"""
    claims = ctx.claims
    if not claims:
        return []
    hints = _SECTION_DIMENSION_HINTS.get(key, ())
    if not hints:
        return claims[:MAX_CLAIMS_PER_SECTION]

    def score(claim: Claim) -> int:
        haystack = f"{claim.dimension}{claim.brand}{claim.text}"
        return sum(1 for hint in hints if hint in haystack)

    ranked = sorted(claims, key=score, reverse=True)
    return ranked[:MAX_CLAIMS_PER_SECTION]


def _evidences_for_section(ctx: PipelineContext, key: str) -> list[Evidence]:
    """章节材料。结论类章节给全量（它要收口），其余章节给按可信度
    排序的前 N 条。"""
    if key in ("executive_summary", "conclusion", "swot"):
        return ctx.evidences
    return sorted(ctx.evidences, key=lambda ev: ev.credibility, reverse=True)[:40]


# ============================================================
# 单章写作
# ============================================================


def write_section(ctx: PipelineContext, key: str) -> tuple[ReportSection, CoercionReport]:
    """写一章。**同步函数，由调用方丢进线程里跑。**"""
    report = CoercionReport()
    title = SECTION_LABELS.get(key, key)
    evidences = _evidences_for_section(ctx, key)
    claims = _claims_for_section(ctx, key)

    text = chat_text(
        ctx,
        section_prompt(key, title, ctx.query, ctx.brands, claims, evidences),
        tier=ctx.mode.writing_tier,
        purpose=f"write:{key}",
        max_tokens=2048,
        temperature=0.5,
        required=False,
        report=report,
    )

    section = ReportSection(
        key=key,
        title=title,
        claim_ids=[claim.claim_id for claim in claims],
    )

    if not text.strip():
        section.degraded = True
        report.note(f"章节「{title}」写作调用没有返回内容")
        return section, report

    cleaned, valid, phantom = resolve_citations(text, _known_ids(ctx))
    section.content = cleaned
    section.evidence_ids = valid

    if phantom:
        # 幻觉引用率的分母来自这里与 `parse_claims` 两处，
        # 都走同一个 `CoercionReport`，保证只计一次。
        report.phantom_ids.extend(phantom)
        report.note(
            f"章节「{title}」里 {len(phantom)} 个引用指向不存在的证据，已从正文删除："
            f"{'、'.join(phantom[:3])}"
        )

    if not valid:
        # 有正文但一个有效引用都没有：这一章是"裸写"的。
        # 不判它 missing（有内容），但必须标 degraded——否则
        # 读者会以为这章的每个判断都有据可查。
        section.degraded = True
        report.note(f"章节「{title}」没有引用任何证据，已标记为降级")

    return section, report


async def run(ctx: PipelineContext) -> None:
    ctx.begin_stage("write")
    writer = ctx.senior_expert
    keys = list(ctx.mode.sections)

    ctx.thought(
        writer,
        f"开始撰写报告，共 {len(keys)} 章："
        f"{'、'.join(SECTION_LABELS.get(key, key) for key in keys)}。"
        f"每章都会带上证据材料块，正文里的判断必须标注来源。",
        stage="write",
    )

    results = await asyncio.gather(
        *(asyncio.to_thread(write_section, ctx, key) for key in keys)
    )

    sections: list[ReportSection] = []
    for section, report in results:
        # 回到主线程合并：并发 merge 共享的 CoercionReport 会让
        # 幻觉引用计数随调度顺序波动，而它是要进 CI 的指标。
        ctx.coercion.merge(report)
        sections.append(section)

    ctx.sections = sections
    ctx.report_progress("write", 0.9, f"已撰写 {len(sections)} 章")

    degraded = [s for s in sections if s.degraded]
    if degraded:
        ctx.degrade(
            "报告撰写",
            f"{len(degraded)}/{len(sections)} 章存在降级（无引用或调用失败）："
            f"{'、'.join(s.title for s in degraded)}",
        )

    cited = sum(len(s.evidence_ids) for s in sections)
    ctx.thought(
        writer,
        f"报告正文撰写完成：{len(sections)} 章，正文共引用 {cited} 处证据，"
        f"其中 {len(degraded)} 章被标记降级。",
        stage="write",
    )
    # 不在这里关节点：装配（assemble）也是 write 阶段的一部分，
    # 由它统一关闭，避免"这个节点什么时候算完成"有两个答案。
    ctx.report_progress("write", 0.7, "章节撰写完成，开始装配报告正文")


# ============================================================
# 批注驱动的二次撰写（供 API 的「深化本节」调用）
# ============================================================


def refine_section(
    ctx: PipelineContext,
    section: ReportSection,
    annotation: str,
    extra_evidences: list[Evidence] | None = None,
) -> tuple[ReportSection, CoercionReport]:
    """按用户批注重写一章。

    **保留原文里的引用**：模型重写时很可能只引用了新材料而丢掉原有的
    引用，那会让这一章看起来"改完更没依据了"。所以重写后把
    原有引用合并回来——旧证据如果仍然成立，它的引用不该消失。
    """
    report = CoercionReport()
    pool = list(extra_evidences or []) + _evidences_for_section(ctx, section.key)

    text = chat_text(
        ctx,
        refine_prompt(section.title, section.content, annotation, pool),
        tier=ctx.mode.writing_tier,
        purpose=f"refine:{section.key}",
        max_tokens=2048,
        temperature=0.5,
        required=True,
        report=report,
    )

    cleaned, valid, phantom = resolve_citations(text, _known_ids(ctx))
    if phantom:
        report.phantom_ids.extend(phantom)

    merged = list(dict.fromkeys([*valid, *section.evidence_ids]))
    return (
        ReportSection(
            key=section.key,
            title=section.title,
            content=cleaned or section.content,
            claim_ids=list(section.claim_ids),
            evidence_ids=merged,
            degraded=section.degraded and not cleaned,
            reworked=True,
        ),
        report,
    )


def section_domains(section: ReportSection, index: dict[str, Evidence]) -> int:
    """一章引用了几个独立域名。图集页拿它做排序。"""
    urls = [index[eid].url for eid in section.evidence_ids if eid in index]
    return len({independent_domain(url) for url in urls} - {""})
