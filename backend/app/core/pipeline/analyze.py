"""阶段 4：分析研判。

**拆成四次独立调用，不合成一次巨调用**
------------------------------------
参考实现用一次调用（最大 9000 token）同时产出论点、矩阵、定价、份额、
五力、趋势。代价是：一次 JSON 畸形则全部回退，而且失败不可见——
报告带着空图表和一个看起来很自信的指标面板渲染出来，没人知道出过错。

拆开之后，每一块有自己的提示词、自己的容错记录、自己的降级标记：

| 调用 | 产出 | 失败后 |
|---|---|---|
| `analyze_claims` | 论点 | 报告没有论点，但矩阵与定价仍在 |
| `analyze_comparison` | 矩阵/份额/五力/趋势 | 图表缺失，其余不受影响 |
| `analyze_structured` | 功能树/定价/画像（每品牌一次） | 该品牌的结构化块缺失 |
| `sentiment` | 舆情标注 | 舆情节为空 |

代价是调用次数变多、总 token 略增。这个代价买的是"部分失败"，
而部分失败是这个系统必然会遇到的常态（某个品牌的材料就是很少）。

结构化分析按品牌并发
--------------------
每个品牌的调用互不依赖，串行跑等于把延迟乘以品牌数。
用 `asyncio.to_thread` 而非裸线程池：它复制上下文，
品牌级的埋点不会丢。`CoercionReport` 在并发里各写各的，
回到主协程再合并——并发 merge 同一个对象会让记账错乱。
"""
from __future__ import annotations

import asyncio

from app.core.analysis.charts import build_charts
from app.core.analysis.matrix import DIMENSION_MAJOR, UNKNOWN, orient
from app.core.analysis.sentiment import label_sentiment
from app.core.evidence.citations import enforce_citations, prune_unverified
from app.core.models import Claim, claim_id_for
from app.core.pipeline.calls import chat_json
from app.core.pipeline.context import PipelineContext
from app.core.pipeline.prompts import (
    claims_prompt,
    comparison_prompt,
    structured_prompt,
)
from app.core.schemas.base import (
    CoercionReport,
    as_float,
    as_list,
    as_list_of_dicts,
    as_str,
    normalize_evidence_ids,
    pick,
)
from app.core.schemas.feature_tree import parse_feature_tree
from app.core.schemas.persona import parse_personas
from app.core.schemas.pricing import parse_pricing

_VALID_CONFIDENCE = ("high", "medium", "low")


def parse_claims(
    payload: dict, ctx: PipelineContext, report: CoercionReport | None = None
) -> tuple[list[Claim], CoercionReport]:
    """把模型的 claims 数组读成领域对象。

    引用归一化走 `normalize_evidence_ids`——**幻觉引用率的唯一计数点**。
    在这里分散计数的话，同一批输出被两个阶段各解析一次，比率会翻倍，
    而翻倍不会有人发现。
    """
    report = report or CoercionReport()
    known = ctx.known_evidence_ids
    claims: list[Claim] = []

    for index, item in enumerate(as_list_of_dicts(pick(payload, "claims", "论点"), "claims", "items")):
        text = as_str(pick(item, "text", "论点", "claim", "content"))
        if not text:
            continue
        ids, _ = normalize_evidence_ids(
            pick(item, "evidenceIds", "evidence_ids", "证据"), known, report=report
        )
        confidence = as_str(pick(item, "confidence", "置信度"), default="medium").lower()
        claims.append(
            Claim(
                claim_id=claim_id_for(text, index),
                text=text,
                confidence=confidence if confidence in _VALID_CONFIDENCE else "medium",  # type: ignore[arg-type]
                evidence_ids=ids,
                brand=as_str(pick(item, "brand", "品牌")),
                dimension=as_str(pick(item, "dimension", "维度")),
            )
        )

    if not claims:
        report.note("论点列表为空")
        report.missing.append("claims")
    return claims, report


def _dimension_evidences(ctx: PipelineContext, brand: str = "") -> list:
    if not brand:
        return ctx.evidences
    scoped = [ev for ev in ctx.evidences if ev.brand == brand]
    # 该品牌的证据太少时退回全量：宁可让模型看到别的品牌的材料
    # （它会自己去分辨），也不要给它一个空材料块——空材料块会稳定地
    # 产出"未获取到"的结论，而那看起来像调研没找到。
    return scoped if len(scoped) >= 3 else ctx.evidences


def _analyze_claims(ctx: PipelineContext) -> None:
    payload, report = chat_json(
        ctx,
        claims_prompt(ctx.query, ctx.brands, ctx.dimensions, ctx.evidences),
        tier=ctx.mode.analysis_tier,
        purpose="analyze_claims",
        max_tokens=4096,
        required=False,
    )
    ctx.coercion.merge(report)

    claims, parse_report = parse_claims(payload, ctx)
    ctx.coercion.merge(parse_report)

    if not claims:
        ctx.degrade("论点提炼", "本次没有产出任何可用论点")
        ctx.thought(ctx.senior_expert, "证据不足以支撑明确论点，本次不产出结论性判断。")
        return

    citation = enforce_citations(
        claims, ctx.evidence_index, min_domains=ctx.mode.min_independent_sources
    )
    ctx.citation_report = citation

    kept, dropped = prune_unverified(claims)
    ctx.claims = kept

    if dropped:
        axis = ctx.senior_expert
        ctx.thought(
            axis,
            f"提炼出 {len(claims)} 条论点，其中 {len(dropped)} 条没有可核验的证据引用，"
            f"按「无证据不立论」剔除。",
        )
        ctx.degrade("论点提炼", f"{len(dropped)} 条论点因缺少可核验引用被剔除")

    if citation.phantom_ids:
        ctx.thought(
            ctx.reviewer_expert,
            f"发现 {len(citation.phantom_ids)} 个不存在的证据引用，已剔除："
            f"{'、'.join(citation.phantom_ids[:3])}",
        )


def _analyze_comparison(ctx: PipelineContext) -> None:
    payload, report = chat_json(
        ctx,
        comparison_prompt(ctx.query, ctx.brands, ctx.dimensions, ctx.evidences),
        tier=ctx.mode.analysis_tier,
        purpose="analyze_comparison",
        max_tokens=3072,
        required=False,
    )
    ctx.coercion.merge(report)

    known = ctx.known_evidence_ids

    matrix = pick(payload, "matrix", "矩阵")
    if isinstance(matrix, dict):
        matrix_ids, _ = normalize_evidence_ids(
            pick(matrix, "evidenceIds", "evidence_ids"), known, report=ctx.coercion
        )
        dimensions = [
            as_str(d) for d in as_list(pick(matrix, "dimensions", "维度")) if as_str(d)
        ]
        brands = [as_str(b) for b in as_list(pick(matrix, "brands", "品牌")) if as_str(b)]
        scores = [
            [as_float(v, default=0.0) for v in row]
            for row in as_list(pick(matrix, "scores", "评分"))
            if isinstance(row, list)
        ]
        # 模型偶尔把 scores 转置着给（见 `analysis/matrix.py`）。
        # 转置的矩阵**画出来完全正常**，只是每个数字都挂在错的东西上，
        # 所以这里当场翻回来，并记一条降级——报告页与导出都会披露它。
        scores, verdict = orient(dimensions, brands, scores)
        if verdict == DIMENSION_MAJOR:
            ctx.coercion.note(
                "对比矩阵是转置着给的（行是维度、列是品牌），已按品牌优先翻回来"
            )
        elif verdict == UNKNOWN and scores:
            ctx.coercion.note(
                f"对比矩阵的形状对不上：{len(scores)} 行 × "
                f"{len(scores[0])} 列，而 {len(dimensions)} 个维度 × {len(brands)} 个品牌"
                "——这一项不画图也不出表"
            )
        ctx.matrix = {
            "dimensions": dimensions,
            "brands": brands,
            "scores": scores,
            "evidenceIds": matrix_ids,
        }
    else:
        ctx.degrade("对比矩阵", "本次没有产出功能评分矩阵")

    share: list[dict] = []
    for item in as_list_of_dicts(pick(payload, "marketShare", "market_share", "市场份额")):
        brand = as_str(pick(item, "brand", "品牌"))
        if not brand:
            continue
        ids, _ = normalize_evidence_ids(
            pick(item, "evidenceIds", "evidence_ids"), known, report=ctx.coercion
        )
        share_value = as_float(pick(item, "share", "份额"), default=0.0)
        basis = as_str(pick(item, "basis", "依据", "口径"))
        # 份额越界 = 单位填错了（库里有一份是 `2022.0`，basis 里写的是
        # "Notion 用户规模 3000 万"）。**不改正、不丢弃**，只记下来：
        # 我们不知道正确的数是什么，替它猜一个占比就是编数据。
        # 但必须让报告说一句，否则读者会拿这个数当百分比读
        # （`charts.py` 的份额闸据此不出饼图）。
        if share_value and not 0 < share_value <= 100:
            ctx.coercion.note(
                f"市场份额「{brand}」的数值是 {share_value:g}，超出 0–100 的占比范围，"
                "疑似填成了用户规模——这一项不出图，表格里按原值显示"
            )
        share.append(
            {
                "brand": brand,
                "share": share_value,
                "basis": basis,
                "evidenceIds": ids,
            }
        )
    # 同一品牌两条不同口径的份额：**都留着，但说清楚**。
    #
    # 库里那份真报告里 `特来电` 是 41.0 与 27.4 两行，`basis` 各写着各的出处
    # （中国充电联盟 / 三个皮匠报告）。丢掉一条就是替读者选了一个口径，
    # 而两条都可能有道理；所以表格里两行都留（前端按 `品牌-数值` 做 key）。
    # 但饼图不能画——那会把同一个品牌算两遍，图上两片"特来电"，
    # 读者只会读出"集中度很高"（`charts._market_share_chart` 据此不出图）。
    duplicate_brands: list[str] = []
    for row in share:
        if row["brand"] in duplicate_brands:
            continue
        values = [f"{r['share']:g}" for r in share if r["brand"] == row["brand"]]
        if len(values) > 1:
            duplicate_brands.append(row["brand"])
            ctx.coercion.note(
                f"市场份额「{row['brand']}」有 {len(values)} 条不同口径的记录"
                f"（{'、'.join(values)}），这一项不出饼图，表格里逐条保留"
            )

    ctx.market_share = share

    forces: list[dict] = []
    for item in as_list_of_dicts(pick(payload, "fiveForces", "five_forces", "五力")):
        force = as_str(pick(item, "force", "name", "力"))
        if not force:
            continue
        ids, _ = normalize_evidence_ids(
            pick(item, "evidenceIds", "evidence_ids"), known, report=ctx.coercion
        )
        # `default=0.0` 而不是 3.0。**3.0 是一个假的判断。**
        #
        # intensity 是 1–5 分制，所以 0 只可能是"没给"。原先缺失时补 3.0，
        # 于是"这一段没判出强度"在报告里变成了"强度中等"——页面上画一根
        # 3 格的条、图表里落一个中间高度的柱，读起来是一个**判断**，
        # 而它是默认值。这与"舆情静默混用规则兜底"是同一类问题：
        # 缺省值伪装成结论，且事后无法从数据里分辨出来
        # （默认值一旦写进去，0 和"真的判了中等"就再也分不开了）。
        #
        # 补 0 之后前端能把它画成"未判定"，而且这是**可逆**的：
        # 想让读者知道，就在页面上写；不想写，也仍然知道它是缺的。
        intensity = as_float(pick(item, "intensity", "强度"), default=0.0)
        if intensity <= 0:
            ctx.coercion.note(f"五力「{force}」没有给出强度，按未判定处理")
        forces.append(
            {
                "force": force,
                "intensity": intensity,
                "analysis": as_str(pick(item, "analysis", "分析")),
                "evidenceIds": ids,
            }
        )
    ctx.five_forces = forces

    trends: list[dict] = []
    for item in as_list_of_dicts(pick(payload, "trends", "趋势")):
        name = as_str(pick(item, "name", "名称"))
        points = [
            {"period": as_str(pick(p, "period", "期")), "value": as_float(pick(p, "value", "值"))}
            for p in as_list(pick(item, "points", "数据点"))
            if isinstance(p, dict)
        ]
        if not name or not points:
            continue
        ids, _ = normalize_evidence_ids(
            pick(item, "evidenceIds", "evidence_ids"), known, report=ctx.coercion
        )
        trends.append({"name": name, "unit": as_str(pick(item, "unit", "单位")),
                       "points": points, "evidenceIds": ids})
    ctx.trends = trends

    produced = sum(
        [bool(ctx.matrix.get("dimensions")), bool(share), bool(forces), bool(trends)]
    )
    if produced < 2:
        ctx.degrade(
            "结构性分析",
            f"矩阵/份额/五力/趋势四项中只产出 {produced} 项，图表会偏少",
        )


#: 出现在"长名字多出来的那段"里，就说明它不是后缀，而是另一句话。
#:
#: `Notion Labs` 多出来的是 `Labs`、`国家电网有限公司` 多出来的是`有限公司`——
#: 它们仍然是**一个名字**的一部分。而
#: `Notion 与 Obsidian 在团队协作场景下的差异` 多出来的那截里有一个`与`：
#: 那是**两个实体加一段描述**，整个字符串是一句话，不是名字。
_TAIL_BREAKERS = ("与", "和", "或", "及", "、", ",", "，", "/", "|", "vs")


def _same_brand(requested: str, returned: str) -> bool:
    """两个名字指的是不是同一个品牌。

    只认"同一个品牌的两种写法"：忽略空白与大小写后相等，或者
    一个是另一个的前缀、且**多出来的那截里没有连接词**。
    `国家电网` / `国家电网有限公司`、`星星充电` / `星星充电（万帮数字能源）`、
    `Notion` / `Notion Labs` 都过；真实 provider 对这些企业名两种写法都给过。

    **两道判据都是被用例逼出来的，不是想出来的**（问题 54）：

    1. 一开始用"包含"。它把 `Notion` 判成
       `Notion 与 Obsidian 在团队协作场景下的差异` 的同一个品牌——
       后者是前者的子串，于是一句"调研对象不是品牌"的短语被收下。
    2. 改成"前缀"只堵住了"返回的更短"这一半。上例里被问的那句更长，
       前缀照样成立。真正分出名字与句子的不是长度，是**多出来的那截长什么样**：
       后缀（`有限公司`）是名字的一部分，连接词（`与`）说明后面还有另一个实体。
    3. 最后那半句判据也是量出来的：`星星充电（万帮数字能源）` 的尾巴
       长 8 个字，比 `Notion Labs` 长得多——按长度卡阈值会误伤它，
       按连接词卡不会。**同一个行业的别名写法，尾巴长度没有上界。**

    刻意不做模糊匹配（编辑距离之类）：这里判错的代价是单向的——
    把别的品牌的内容挂到它名下，而报告读者没有任何办法看出来。
    """
    left = "".join((requested or "").split()).casefold()
    right = "".join((returned or "").split()).casefold()
    if not left or not right:
        # 有一边是空的就没得比。空的那边由 `completeness` 去说，
        # 这里不拦：拦截要有确凿的理由，不能靠猜。
        return True
    if left == right:
        return True
    longer, shorter = (left, right) if len(left) > len(right) else (right, left)
    if not longer.startswith(shorter):
        return False
    return not any(token in longer[len(shorter):] for token in _TAIL_BREAKERS)


def _reject_if_relabelled(requested: str, artifact, label: str, report: CoercionReport):
    """模型给结构化块标的品牌和请求的对不上时，**丢掉它**。

    这不是格式问题，是**归属**问题。这个缺陷真的发生过（问题 54）：
    请求"国内新能源汽车充电桩运营商"（一个行业，不是品牌），
    模型拿证据里最响的那个品牌回答，于是库里那份报告的 `featureTrees`
    里 `特来电` 出现两次、`pricingModels` / `personaSets` 各重复一次，
    而前端按品牌做 key——两张卡互相顶掉，其中一份数据直接消失。

    丢掉而不是改名：改成被问的那个品牌，等于把 B 的能力挂到 A 名下，
    那是**替它编内容**。丢掉的后果是这一块缺失，而缺失会被上面那条
    degrade 和 `completeness` 明说——这是这个项目一贯的取舍。
    """
    if artifact is None:
        return None
    returned = getattr(artifact, "brand", "")
    if _same_brand(requested, returned):
        return artifact
    report.note(
        f"{label}的归属对不上：问的是「{requested}」，答的却标着「{returned}」，已丢弃"
        "（挂到被问的那个品牌名下等于替它编内容）"
    )
    return None


def _structured_brands(ctx: PipelineContext) -> list[str]:
    """该对哪些名字做"功能树 / 定价 / 画像"这三块结构化分析。

    `ctx.brands` 的第一项是**调研对象**，而调研对象不一定是品牌：
    库里那两份真报告里它是「国内新能源汽车充电桩运营商」和
    「Notion 与 Obsidian 在团队协作场景下的差异」——用户问的那句话本身就是它。
    拿这种短语去问功能树，模型只能拿证据里最响的品牌来答，
    于是那个品牌会拿到两棵树（见问题 54）。

    判据是"它在不在候选竞品清单里"，而不是"它长得像不像品牌"：
    后者要么是一张永远补不全的关键词表，要么是又一次模型调用。
    而 `candidate_brands` 本来就在上下文里，记的正是"模型认为要对比哪几个"。
    """
    candidates = set(ctx.candidate_brands)
    kept = [brand for brand in ctx.brands if brand and (brand != ctx.subject or brand in candidates)]
    # 全被筛掉时退回原列表：那说明这份上下文里根本没有竞品清单
    # （单品牌分析），让它照旧拿到"至少一条腿"，而不是空转。
    return kept or [brand for brand in ctx.brands if brand]


def _structured_one(ctx: PipelineContext, brand: str):
    """单个品牌的结构化分析。**同步函数，由调用方丢进线程里跑。**"""
    report = CoercionReport()
    payload, parse_report = chat_json(
        ctx,
        structured_prompt(ctx.query, brand, ctx.dimensions, _dimension_evidences(ctx, brand)),
        tier=ctx.mode.analysis_tier,
        purpose=f"analyze_structured:{brand}",
        max_tokens=4096,
        required=False,
    )
    report.merge(parse_report)
    if not payload:
        return brand, None, None, None, report

    known = ctx.known_evidence_ids
    tree_payload = pick(payload, "featureTree", "feature_tree", "功能树")
    pricing_payload = pick(payload, "pricingModel", "pricing_model", "定价")
    persona_payload = pick(payload, "userPersona", "user_persona", "用户画像")

    tree = (
        parse_feature_tree(tree_payload, brand=brand, known_ids=known, report=report)
        if isinstance(tree_payload, dict) else None
    )
    pricing = (
        parse_pricing(pricing_payload, brand=brand, known_ids=known, report=report)
        if isinstance(pricing_payload, dict) else None
    )
    personas = (
        parse_personas(persona_payload, brand=brand, known_ids=known, report=report)
        if isinstance(persona_payload, dict) else None
    )

    # 归属校验放在这里而不是 schema 层：schema 只负责"把这个结构解出来"，
    # 而"解出来的东西属于谁"取决于**调用方问了谁**——那是流水线才知道的事。
    tree = _reject_if_relabelled(brand, tree, "功能树", report)
    pricing = _reject_if_relabelled(brand, pricing, "定价", report)
    personas = _reject_if_relabelled(brand, personas, "画像", report)

    # 解析成功但四个能力全是 unknown，等于什么都没说。
    # 这种空壳要标成降级，否则完整度检查会把它算成"已填充"。
    if tree is not None and tree.categories and not tree.features():
        tree.degraded = True
    return brand, tree, pricing, personas, report


async def _analyze_structured(ctx: PipelineContext) -> None:
    brands = _structured_brands(ctx)
    skipped = [brand for brand in ctx.brands if brand and brand not in brands]
    if skipped:
        # 跳过要说出来。不说的话，读者看到对比矩阵里有调研对象、
        # 功能矩阵里没有它，只能猜是漏了还是故意的。
        ctx.thought(
            ctx.senior_expert,
            f"「{'、'.join(skipped)}」是调研对象而不是竞品，"
            "不拿它去问功能矩阵 / 定价 / 画像。",
            stage="analyze",
        )

    results = await asyncio.gather(
        *(asyncio.to_thread(_structured_one, ctx, brand) for brand in brands)
    )

    missing: list[str] = []
    for brand, tree, pricing, personas, report in results:
        # 回到主协程再合并：并发 merge 同一个 CoercionReport 会让记账错乱。
        ctx.coercion.merge(report)
        if tree is not None:
            ctx.feature_trees.append(tree)
        if pricing is not None:
            ctx.pricing_models.append(pricing)
        if personas is not None:
            ctx.persona_sets.append(personas)
        if tree is None and pricing is None and personas is None:
            missing.append(brand)

    if missing:
        ctx.degrade("结构化分析", f"{'、'.join(missing)} 未产出功能树/定价/画像")
    if ctx.mode.enable_structured and not ctx.pricing_models:
        ctx.degrade("定价分析", "没有解析出任何品牌的定价档位")


def reset_analysis_state(ctx: PipelineContext) -> None:
    """清空上一轮的分析产物。

    返工会**再跑一次分析**，而分析是往 ctx 上"追加"的
    （`feature_trees.append(...)`、`ctx.claims = kept`）。不清空的话，
    第二轮的结果会和第一轮叠在一起：同一棵树出现两次、图表里
    同一个品牌画两条线，而报告看不出来这是重复——它只会显得"数据很丰富"。

    清空而不是"保留并去重"：第二轮拿到的证据是第一轮的超集，
    它的分析结论本来就该整体取代第一轮的结论。
    """
    ctx.claims = []
    ctx.matrix = {}
    ctx.market_share = []
    ctx.five_forces = []
    ctx.trends = []
    ctx.feature_trees = []
    ctx.pricing_models = []
    ctx.persona_sets = []
    ctx.sentiment = {}
    ctx.charts = []
    ctx.citation_report = None
    ctx.issues = []


async def run(ctx: PipelineContext) -> None:
    ctx.begin_stage("analyze")
    reset_analysis_state(ctx)

    _analyze_claims(ctx)
    ctx.report_progress("analyze", 0.4, f"已提炼 {len(ctx.claims)} 条有效论点")

    _analyze_comparison(ctx)
    ctx.report_progress("analyze", 0.7, "对比矩阵与趋势分析完成")

    await _analyze_structured(ctx)

    if ctx.mode.enable_sentiment:
        ctx.sentiment = label_sentiment(ctx)
    else:
        ctx.sentiment = {
            "labels": [], "counts": {"positive": 0, "negative": 0, "neutral": 0},
            "labeledByLlm": 0, "labeledByRule": 0, "total": 0,
            "note": f"{ctx.mode.label}模式不做舆情标注",
        }

    ctx.charts = build_charts(ctx)

    ctx.finish_stage(
        "analyze",
        detail={
            "claims": len(ctx.claims),
            "charts": len(ctx.charts),
            "featureTrees": len(ctx.feature_trees),
            "pricingModels": len(ctx.pricing_models),
        },
    )
