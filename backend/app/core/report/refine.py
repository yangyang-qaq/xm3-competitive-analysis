"""「深化本节」：批注驱动的二次调研 + 重写。

为什么它要在报告**落库之后**还能跑
--------------------------------
流水线跑完之后，报告里最弱的那一章往往才看得出来——读者读完才知道
"定价这一节说了等于没说"。这时候唯一有用的动作是**针对那一章**
再搜一轮、再写一遍，而不是整份重跑（整份重跑要重花十几分钟和几毛钱，
而问题只在一章里）。

所以这里做的事和 `collect` / `write` 是同一套代码，区别只有两处：
材料池从**库里**取而不是从内存的 ctx 取，以及查询由批注生成
而不是由维度计划生成。

三个容易做错的点
--------------

1. **`rework` 预算池，不是首轮池。** 首轮池在首轮结束时就被
   `planned[:budget]` 花到见底了（见 `collect.build_queries` 的 docstring），
   从那里取的话深化一节**一次搜索都发不出去**，而症状会伪装成
   "补采没有新增证据"。深化走 `rework=True`。
2. **新证据必须并进报告正文的证据表。** 只更新 DB 的话，重写后的章节会
   引用一个正文里不存在的 id → 导出时渲染成 `[?]` → `citation_problems`
   报"正文引用了不存在的证据"。这条链子上的每一环都得跟上。
3. **指标不能留在旧值。** 证据多了、某章不再降级了，而 `metrics.evidences`
   和完整度还是旧的——那就是一份**自己和自己打架**的报告。所以重算。

重算到什么程度，以及不重算什么
--------------------------
`compute_metrics(ctx)` 对证据/维度/平台类指标是纯函数，可以照搬重算。
**但论点类指标（无证据立论率、交叉验证率、幻觉引用率）不重算**：
深化只重写一章正文，不改论点表，那几个数本来就是对的。
覆盖率的**分母**用报告里存下的 `dimensions`（`assemble` 写入的），
不拿 `dimensionCoverageDetail` 的键凑——那份明细**故意保留计划外的
维度**，拿它当分母会把覆盖率算低。
"""
from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime

from app.core.models import Claim, Evidence, Issue
from app.core.modes import get_mode
from app.core.observability.events import EventJournal
from app.core.observability.trace import Tracer
from app.core.pipeline import collect, write
from app.core.pipeline.context import PipelineContext
from app.core.schemas.completeness import assess_completeness
from app.core.schemas.report import ReportSection
from app.db.repo import evidences as evidences_repo
from app.providers.registry import get_fetcher, get_llm, get_search

#: 深化时最多发几次搜索。**比返工预算窄**：返工的靶子是审计给的
#: （"这个维度证据太薄"），而批注是自然语言，可能整句话都不是检索词。
#: 一次批注换 2 条查询，够用来补一个侧面；再多就是拿用户的钱赌
#: 他批注里的关键词恰好是好查询。
MAX_DEEPEN_QUERIES = 2

#: 批注里超过这个长度的词不当检索词用（多半是一整句话）。
_MAX_KEYWORD_CHARS = 12

#: 批注里能当检索词的部分：**以字母开头的一段 ASCII**。
#:
#: 用正则捞而不是按空格切，是因为中文批注里的英文词**通常和中文粘在一起**
#: （"看看 API，还有 SaaS 的价格"）——按空格切出来的 token 是
#: `"API，还有"`，`isascii()` 为假，整个词就丢了。
#: 而它恰恰是最该留下的那一类。
#:
#: 首字符限定成字母，顺带解决了纯数字：`"2024"` 匹配不上，
#: 而它本来就不该进查询文本（要"最新的"由 `freshness` 参数负责）。
_TERM = re.compile(r"[A-Za-z][A-Za-z0-9._+-]*")


def _context(record) -> PipelineContext:
    """用库里的报告重建一个够用的 ctx。

    **不订阅 journal、不落库事件。** 深化不是任务流的一部分：
    它没有 progress 轨、没有 DAG，把事件写进 `task_events` 只会让
    "决策回放"里多出一段谁也解释不了的尾巴。
    """
    mode = get_mode(record.mode)
    ctx = PipelineContext(
        task_id=record.task_id,
        query=record.query or record.subject,
        mode=mode,
        tracer=Tracer(record.task_id),
        journal=EventJournal(record.task_id),
        llm=get_llm(),
        search=get_search(),
        fetcher=get_fetcher(),
        auto_clarify=True,
    )
    ctx.subject = record.subject
    ctx.brands = list(record.brands)
    data = record.data or {}
    ctx.dimensions = [str(item) for item in (data.get("dimensions") or [])]
    ctx.evidences = evidences_repo.list_by_task(record.task_id)
    ctx.reindex()
    # 论点**要一起读回来**，不能留空。空列表会让 `quality_gate` 判定
    # "没有产出任何论点"——那是一个凭空造出来的 blocker。
    # （深化本身不改论点表，但质量门要看它。）
    ctx.claims = [
        Claim.from_dict(item) for item in (data.get("claims") or [])
    ]
    return ctx


def _annotation_terms(annotation: str) -> list[str]:
    """批注里**本身就是完整词**的那些片段。

    **不切中文。** 第一版按空格切，实测把

        这部分太笼统了 我想知道他们到底怎么收费的

    切成了 `['这部分太笼统了']`——7 个字的半句话，拿它去搜就是
    烧掉一次预算换一堆无关页面，而用户会以为搜索源不行。
    中文没有词边界，按空格切出来的从来不是词。

    所以这里只认 **ASCII** 词：`API`、`GPT-4`、`SaaS` 本身就是完整词，
    带上能提高精度。中文的关键词提取交给谁都不合适——
    真正的解法是给章节标题（见 `_targets`），而不是造一个假的切词器。
    """
    terms: list[str] = []
    for match in _TERM.finditer(annotation):
        # 只收句末的句点（`"SaaS."` → `"SaaS"`），**不收 `+` 和 `-`**：
        # 它们可能是词的一部分（`C++`），收掉之后 `C++` 会变成 `C`，
        # 然后因为太短被丢掉——一个字母的查询词比没有更糟。
        word = match.group(0).rstrip(".")
        if 1 < len(word) <= _MAX_KEYWORD_CHARS and word not in terms:
            terms.append(word)
    return terms


def _targets(record, section: ReportSection, annotation: str) -> list[dict]:
    """把批注翻译成采集目标。

    形状与审计给出的返工目标一致（`collect._rework_queries` 吃的就是这个），
    这样深化走的完全是返工那条已经测过的路。

    检索词以**章节标题**为主：它本来就是这一章的主题，天然是个好查询
    （"定价"、"功能对比"），而且一定存在、一定与这次深化相关。
    批注负责的是**重写**那一半——它整句传给模型，不经过任何切词。
    """
    terms = [*_annotation_terms(annotation), section.title]
    keyword = " ".join(terms)
    dimension = ""
    if ctx_dimensions := (record.data or {}).get("dimensions") or []:
        # 章节 key 与维度名不一定同名，所以这里不做匹配；
        # 只有刚好同名时才把维度带上，免得凭空发明一个维度。
        dimension = section.key if section.key in ctx_dimensions else ""
    brands = list(record.brands) or [record.subject]
    return [
        {"brand": brand, "dimension": dimension, "keyword": keyword}
        for brand in brands[:MAX_DEEPEN_QUERIES]
    ]


async def _second_round(
    ctx: PipelineContext, record, section: ReportSection, annotation: str
) -> tuple[list[Evidence], list[str]]:
    """针对这一章补采一轮。返回 `(新增证据, 这轮新增的降级说明)`。

    `collect.run(rework=True)` 会做完整的一轮：生成查询 → 搜索 → 抓正文 →
    打分 → 并进 `ctx.evidences`。所以"新增了哪些"只能靠**跑之前先记一份
    id 集合**再比出来——不能假定它只往末尾追加（它走的是 dict 合并，
    顺序会变）。
    """
    before = {ev.evidence_id for ev in ctx.evidences}
    degraded_before = len(ctx.degraded_blocks)

    await collect.run(ctx, targets=_targets(record, section, annotation), rework=True)

    fresh = [ev for ev in ctx.evidences if ev.evidence_id not in before]
    return fresh, ctx.degraded_blocks[degraded_before:]


def _section_of(data: dict, key: str) -> tuple[int, ReportSection]:
    """按 key 找章节。空 key 取第一章——批注挂在整份报告上时，
    "深化"落到哪一章没有别的答案，而报 422 让用户去猜是不友好的。"""
    sections = data.get("sections") or []
    if not sections:
        raise LookupError("这份报告没有章节")
    index = 0
    if key:
        for position, item in enumerate(sections):
            if str(item.get("key", "")) == key:
                index = position
                break
        else:
            raise KeyError(key)
    return index, ReportSection.from_dict(sections[index])


def _recompute(data: dict, ctx: PipelineContext) -> None:
    """把因为新增证据而**确实变了**的那些指标重算，就地写回 `data`。

    只碰证据/维度/平台类。论点类指标（无证据立论率、交叉验证率、
    幻觉引用率）不动：深化只重写一章正文，不改论点表，
    那几个数本来就是对的，重算只会引入新的错。

    正文引用编号（`citations`）是**第三个类别**，也要跟着重算——
    它不是"指标"，但它确实变了，理由见下面 `_renumber`。
    """
    # **在这里导入，不在模块顶部。** `metrics` 会链式拉到
    # `app.core.pipeline`（包 `__init__`）→ `analyze` → `analysis.charts`，
    # 而 `charts` 又要 `pipeline.context`；从 `report.refine` 这个入口
    # 先进 `metrics` 的话，`charts` 会以"半初始化"的状态被拿到，
    # 报 `ImportError: cannot import name 'build_charts'`。
    # 放函数里就不会被谁的 import 顺序改动弄坏。
    from app.core.analysis.metrics import compute_metrics
    from app.core.pipeline.audit import quality_gate

    fresh = compute_metrics(ctx)
    metrics = data.setdefault("metrics", {})
    for key in (
        "evidences", "degradedEvidences", "degradedRate",
        "independentDomains", "platformCount", "platforms",
        "dimensionsPlanned", "dimensionsCovered", "dimensionCoverage",
        "dimensionCoverageDetail",
    ):
        if key in fresh:
            metrics[key] = fresh[key]

    # ---- 质量门要跟着重判 ----
    # 补采之后覆盖率可能越过了下限，而 `failedBecause` 里还印着旧的那句
    # "维度覆盖率 50% 低于下限"。导出是**并排**印这两行的
    # （`export.to_markdown` 的"未通过的原因" + 维度覆盖那一行），
    # 于是报告自己和自己打架。
    #
    # 重判用的是**库里存下来的那份 issue 清单**（`audit.issues` 是落库的）：
    # 深化不解决已登记的 blocker，所以 `blockers` 那类原因保持原样，
    # 而覆盖率这类**纯由证据算出来**的原因会跟着新数字走。这正是要的。
    audit_blob = data.get("audit") or {}
    issues = [Issue.from_dict(item) for item in (audit_blob.get("issues") or [])]
    gate = quality_gate(ctx, issues, audit_blob.get("review") or {})
    quality = data.setdefault("quality", {})
    for key in (
        "passed", "coverage", "dimensionsPlanned", "dimensionsCovered",
        "uncoveredDimensions", "blockers", "major", "minor",
        "thresholds", "failedBecause",
    ):
        if key in gate:
            quality[key] = gate[key]

    # 完整度读的是整份 body，所以它得在章节写回**之后**再算，
    # 也要排在质量门之后——它会把质量门的结论一起看进去。
    completeness = assess_completeness(data)
    data["completeness"] = completeness.to_dict()
    quality["completeness"] = completeness.score
    quality["publishable"] = completeness.is_publishable

    _renumber(data)


def _renumber(data: dict) -> None:
    """按重写后的正文重发引用编号。

    **为什么必须重发。** 编号从 2026-09-19 起是**存在报告里的**
    （`body["citations"]`，`assemble` 算的），页面与导出都读那一份。
    深化会重写一章正文——新写的这一章可能引到一条**刚刚才采到**的证据，
    于是存下来的那份编号里没有它。

    不重发的后果是**页面与导出各说各话**：导出的 `_CitationIndex`
    接在种子之后发新号（那条新证据拿到 `[3]`），而报告页读到
    `citations` 非空就不再现算，那条引用显示成 `[?]`。
    两边看起来都正常——正是这次改动要消灭的那种坏。

    而且**没有任何一道检查会报**：`citation_problems` 比对的是
    "正文里的 `[N]`"与"附录里的 `[N]`"，导出自己前后一致，
    所以它一路绿灯。这个缺陷是读代码读出来的，不是测出来的。

    整体重发而不是"给新证据补一个号"：重写后的这一章，引用的
    出现顺序可能整个变了，而编号的定义就是**首次出现的顺序**
    （见 `citation_index.build_citations`）。补号会让 `[1]` 不再是
    读者读到的第一条证据。

    用 `data` 而不是 `ctx` 取已知证据表：导出与报告页读的都是
    `data["evidences"]`，这里必须以同一份为准。
    """
    from app.core.report.citation_index import build_citations

    known = {
        str(item.get("evidenceId", ""))
        for item in (data.get("evidences") or [])
        if item.get("evidenceId")
    }
    data["citations"] = build_citations(data.get("sections") or [], known)


def _note(data: dict, section_key: str, annotation: str, added: int, degraded: list[str]) -> dict:
    """在报告里留一条深化的记录。

    必须留：不然同一份报告第二次打开时，没人知道"这一节什么时候被
    谁按什么意见改过"——而 `reworked=True` 只说改过，不说为什么。
    """
    entry = {
        "sectionKey": section_key,
        "annotation": annotation,
        "addedEvidences": added,
        "degraded": list(degraded),
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    data.setdefault("refinements", []).append(entry)
    return entry


async def refine(
    record,
    annotation: str,
    *,
    section_key: str = "",
    search: bool = True,
) -> dict:
    """按批注重写一章。就地改 `record.data`，由调用方落库。

    返回一份**给前端看的结果**（新章节 + 本轮发生了什么），
    而不是整份报告：报告页拿到结果后自己决定要不要重拉全量。
    """
    data = record.data or {}
    try:
        index, section = _section_of(data, section_key)
    except KeyError:
        # 指定的章节不存在。**必须在 `LookupError` 之前接住**：
        # `KeyError` 是 `LookupError` 的子类，先写 `except LookupError`
        # 的话这一支永远不会执行，所有错的 `sectionKey` 都会被
        # 一路翻译成"报告数据有问题"（422）——而报告好端端的，
        # 是请求指错了地方（404）。写这条时就是这么栽的。
        raise
    except LookupError as exc:
        # 报告里一章都没有。这是**数据**问题，不是请求问题。
        raise ValueError(str(exc)) from exc

    ctx = _context(record)

    fresh: list[Evidence] = []
    degraded: list[str] = []
    if search:
        fresh, degraded = await _second_round(ctx, record, section, annotation)

    # 重写。**同步函数**（它内部是一次阻塞的 LLM 调用），丢进线程。
    # `extra_evidences` 传空：新证据已经并进 `ctx.evidences` 了，
    # `refine_section` 自己会从那里取，再传一遍会重复。
    new_section, coercion = await asyncio.to_thread(
        write.refine_section, ctx, section, annotation
    )

    data["sections"][index] = new_section.to_dict()
    # 新证据要并进正文的证据表。只写库的话，重写后的章节会引用一个
    # 正文里不存在的 id —— 导出时渲染成 `[?]`，`citation_problems`
    # 立刻报"正文引用了不存在的证据"。
    known = {str(item.get("evidenceId", "")) for item in (data.get("evidences") or [])}
    data.setdefault("evidences", []).extend(
        ev.to_dict() for ev in fresh if ev.evidence_id not in known
    )

    _recompute(data, ctx)
    entry = _note(data, new_section.key, annotation, len(fresh), degraded)
    record.data = data

    evidences_repo.save_many(record.task_id, fresh, report_id=record.report_id)

    return {
        "reportId": record.report_id,
        "section": new_section.to_dict(),
        "sectionKey": new_section.key,
        "addedEvidences": len(fresh),
        "phantomCitations": list(coercion.phantom_ids),
        "degraded": degraded,
        "refinement": entry,
        "completeness": data["completeness"],
        "quality": data["quality"],
    }


__all__ = ["MAX_DEEPEN_QUERIES", "refine"]
