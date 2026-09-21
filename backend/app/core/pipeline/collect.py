"""阶段 3：联网采集。

搜索角度是按"轮次"展开的，不是一次铺满
--------------------------------------
查询数 ≈ 品牌 × 维度 × 角度，很快就爆炸。所以分三轮，每轮前一轮
铺完才进下一轮：

1. **维度轮**：`品牌 + 维度`，保证每个维度都有材料
2. **角度轮**：`品牌 + 角度`，补充不同来源类型
3. **平台轮**：定向到具体平台，只在需要舆情时跑

按轮次而不是按笛卡尔积展开，是为了让**预算被砍掉时砍掉的是冗余**。
如果一次铺满再截断，某个维度可能一条都没搜到——而那正是覆盖率算不出来的原因。

去重时合并维度，不是丢弃
------------------------
同一个 URL 经常同时命中两个维度（一篇评测同时讲了功能和定价）。
按"后来者丢弃"处理会让第二个维度失去这条证据，覆盖率被无谓拉低。
所以按 `evidence_id` 去重并**合并** `matched_dimensions`——
这也正是 `matched_dimensions` 必须是列表而不是单值的原因。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.evidence.citations import MIN_INDEPENDENT_DOMAINS
from app.core.evidence.credibility import score_batch
from app.core.evidence.relevance import judge_relevance
from app.core.evidence.sourcetypes import classify_source, independent_domain
from app.core.models import Evidence, evidence_id_for
from app.core.pipeline.calls import gather_fetch, gather_search
from app.core.pipeline.context import PipelineContext
from app.providers.base import SearchHit, SearchQuery

#: 舆情轮定向的平台。这些是**站点**不是搜索源——可信度评分关心
#: "材料来自哪里"，与"通过谁找到它"无关。
_PLATFORM_SITES: tuple[str, ...] = (
    "zhihu.com", "xiaohongshu.com", "douyin.com", "bilibili.com",
)

#: 每类查询取多少条结果。维度轮取多一些（要覆盖一个维度的多个侧面），
#: 平台轮取少一些（只是抽样看口碑倾向）。
_LIMIT_DIMENSION = 10
_LIMIT_ANGLE = 8
_LIMIT_PLATFORM = 6
_LIMIT_REWORK = 12


@dataclass(frozen=True)
class PlannedQuery:
    query: SearchQuery
    brand: str = ""
    dimension: str = ""
    angle: str = ""
    kind: str = "dimension"


def _rework_queries(targets: list[dict] | None) -> list[PlannedQuery]:
    """把审计给出的返工目标翻译成查询。

    目标和普通查询的区别只有一处：它们带着**上一轮明确发现的问题**
    （某个维度证据太薄、某个品牌没有独立信源），所以优先级最高。
    首轮里它们排在最前，返工轮里它们是全部。
    """
    planned: list[PlannedQuery] = []
    for target in targets or []:
        brand = str(target.get("brand", ""))
        dimension = str(target.get("dimension", ""))
        keyword = str(target.get("keyword", "")) or dimension
        text = " ".join(part for part in (brand, keyword) if part).strip()
        if not text:
            continue
        planned.append(
            PlannedQuery(
                query=SearchQuery(text=text, limit=_LIMIT_REWORK, freshness="year"),
                brand=brand,
                dimension=dimension,
                angle=keyword,
                kind="rework",
            )
        )
    return planned


def build_queries(
    ctx: PipelineContext,
    *,
    targets: list[dict] | None = None,
    rework: bool = False,
) -> list[PlannedQuery]:
    """按轮次生成查询计划，并按剩余预算截断。

    纯函数（只读 ctx），可单测。预算逻辑有明确对错，不该和网络调用混在一起测。

    两个池子，别混
    -------------
    **首轮**用 `max_search_calls - search_calls_made`。这个池子会被
    `planned[:budget]` 一次花到见底——首轮结束时它几乎必然是 0。

    **返工轮**（`rework=True`）用 `rework_search_calls - rework_search_calls_made`，
    和首轮完全无关。这不是"多给一点"，而是**修一个结构性错误**：
    共用池子时返工拿到的永远是 0 次检索，一轮都搜不出去，而症状会
    伪装成"补采没有新增证据"——报告里那句话读起来像搜索源的锅，
    实际上是这里根本没发出过请求。指标"返工提升 Δ"也因此恒为 0。
    """
    if rework:
        budget = max(0, ctx.mode.rework_search_calls - ctx.rework_search_calls_made)
        if budget <= 0:
            return []
        # 返工轮**只**跑指定目标。不复跑第 1~3 轮：那些查询文本和首轮
        # 一模一样，会返回同一批 URL，去重之后一条新证据都进不来，
        # 白白吃掉这个本来就小的池子——而且会让"Δ 是哪来的"说不清。
        return _rework_queries(targets)[:budget]

    budget = max(0, ctx.mode.max_search_calls - ctx.search_calls_made)
    if budget <= 0:
        return []

    planned: list[PlannedQuery] = _rework_queries(targets)

    # 第 1 轮：维度。每个品牌每个维度各一条。
    for brand in ctx.brands:
        for dimension in ctx.dimensions:
            planned.append(
                PlannedQuery(
                    query=SearchQuery(text=f"{brand} {dimension}".strip(), limit=_LIMIT_DIMENSION),
                    brand=brand,
                    dimension=dimension,
                    kind="dimension",
                )
            )

    # 第 2 轮：角度。补充不同来源类型（官方/评测/社区）。
    for brand in ctx.brands:
        for angle in ctx.search_angles:
            planned.append(
                PlannedQuery(
                    query=SearchQuery(text=f"{brand} {angle}".strip(), limit=_LIMIT_ANGLE),
                    brand=brand,
                    angle=angle,
                    kind="angle",
                )
            )

    # 第 3 轮：平台定向。只在需要舆情时跑。
    if ctx.mode.enable_sentiment:
        for brand in ctx.brands:
            for site in _PLATFORM_SITES:
                planned.append(
                    PlannedQuery(
                        query=SearchQuery(
                            text=f"{brand} 评价",
                            limit=_LIMIT_PLATFORM,
                            sites=(site,),
                            freshness="year",
                        ),
                        brand=brand,
                        dimension="用户口碑",
                        kind="platform",
                    )
                )

    return planned[:budget]


def build_evidences(
    ctx: PipelineContext,
    results: list[tuple[PlannedQuery, list[SearchHit]]],
) -> tuple[list[Evidence], int]:
    """把搜索结果转成去重后的证据。返回 `(证据, 被相关性过滤掉的数量)`。

    过滤在这里而不是在适配器里：适配器负责"翻译方言"，
    相关不相关是分析层的判断。放进适配器的话，换一家搜索源
    这段逻辑就静默消失了——而那正是适配层要防的事。
    """
    captured_at = datetime.now(UTC).isoformat(timespec="seconds")
    by_id: dict[str, Evidence] = {}
    filtered = 0

    for planned, hits in results:
        for hit in hits:
            verdict = judge_relevance(
                url=hit.url,
                title=hit.title,
                snippet=hit.snippet,
                brand=planned.brand,
                dimension=planned.dimension,
            )
            if not verdict.keep:
                filtered += 1
                continue

            eid = evidence_id_for(hit.url)
            existing = by_id.get(eid)
            if existing is not None:
                # 同一条 URL 又出现了：合并维度，取更好的排名。
                if planned.dimension and planned.dimension not in existing.matched_dimensions:
                    existing.matched_dimensions.append(planned.dimension)
                if hit.rank and (not existing.rank or hit.rank < existing.rank):
                    existing.rank = hit.rank
                continue

            by_id[eid] = Evidence(
                evidence_id=eid,
                url=hit.url,
                title=hit.title,
                snippet=hit.snippet,
                brand=planned.brand,
                source_type=classify_source(hit.url, hit.site_name, brand=planned.brand),
                site_name=hit.site_name or independent_domain(hit.url),
                published_at=hit.published_at,
                captured_at=captured_at,
                matched_dimensions=[planned.dimension] if planned.dimension else [],
                query=planned.query.text,
                provider=hit.provider or ctx.search.name,
                rank=hit.rank,
            )

    return list(by_id.values()), filtered


async def run(
    ctx: PipelineContext, *, targets: list[dict] | None = None, rework: bool = False
) -> None:
    """跑一轮采集。返工时用同一段代码，只是 targets 非空。

    返工轮 begin/finish 的都是 `rework` 节点而不是 `collect`：
    工作台的 DAG 上它们是两个格子，返工轮亮的是第二个。
    都用 `collect` 的话，用户看到的是采集节点又跑了一遍，
    而"这次是去补上一轮发现的问题"这层信息就丢了。
    """
    stage_name = "rework" if rework else "collect"
    ctx.begin_stage(stage_name)
    collector = ctx.collector_expert

    planned = build_queries(ctx, targets=targets, rework=rework)
    if not planned:
        # 说清是**哪个**预算用尽了。含混的一句"预算已用尽"会让人去加大
        # `max_search_calls`，而返工花的是另一个池子，改了没用。
        pool = (
            (ctx.mode.rework_search_calls, ctx.rework_search_calls_made, "返工")
            if rework
            else (ctx.mode.max_search_calls, ctx.search_calls_made, "首轮")
        )
        ctx.degrade(
            "联网采集",
            f"{pool[2]}搜索预算已用尽（{pool[1]}/{pool[0]}），本轮没有发起任何搜索",
        )
        ctx.finish_stage(stage_name, status="degraded", detail={"queries": 0})
        return

    used, cap = (
        (ctx.rework_search_calls_made, ctx.mode.rework_search_calls)
        if rework
        else (ctx.search_calls_made, ctx.mode.max_search_calls)
    )
    ctx.thought(
        collector,
        f"本轮计划 {len(planned)} 次检索，"
        f"覆盖 {len(ctx.brands)} 个品牌 × {len(ctx.dimensions)} 个维度。"
        f"已用{'返工' if rework else '首轮'}预算 {used}/{cap}。",
        stage=stage_name,
    )

    results = await gather_search(ctx, [item.query for item in planned], rework=rework)
    paired = [(planned[index], hits) for index, (_, hits) in enumerate(results)]

    found, filtered = build_evidences(ctx, paired)
    ctx.filtered_hits += filtered

    # 与已有证据合并（返工轮会走到这里）。
    merged: dict[str, Evidence] = {ev.evidence_id: ev for ev in ctx.evidences}
    for ev in found:
        existing = merged.get(ev.evidence_id)
        if existing is None:
            merged[ev.evidence_id] = ev
            continue
        for dimension in ev.matched_dimensions:
            if dimension and dimension not in existing.matched_dimensions:
                existing.matched_dimensions.append(dimension)
    ctx.evidences = list(merged.values())
    ctx.reindex()

    ctx.report_progress(stage_name, 0.55, f"命中 {ctx.raw_hits} 条，去重后 {len(ctx.evidences)} 条")

    # ---- 抓正文 ----
    # 返工轮只抓新证据，避免把预算重复花在已经抓过的页面上。
    budget = ctx.mode.max_fetches - ctx.fetch_outcome.attempted
    fresh = [ev for ev in ctx.evidences if not ev.full_text and not ev.degraded]
    outcome = await gather_fetch(ctx, fresh, budget=max(0, budget))
    ctx.fetch_outcome.considered += outcome.considered
    ctx.fetch_outcome.attempted += outcome.attempted
    ctx.fetch_outcome.ok += outcome.ok
    ctx.fetch_outcome.degraded += outcome.degraded
    ctx.fetch_outcome.skipped_budget += outcome.skipped_budget
    ctx.fetch_outcome.failures.extend(outcome.failures)
    ctx.fetch_outcome.boilerplate.update(outcome.boilerplate)

    # ---- 打分 ----
    score_batch(ctx.evidences, boilerplate=ctx.fetch_outcome.boilerplate)

    for ev in ctx.evidences:
        ctx.emit_evidence(ev)

    if ctx.fetch_outcome.degraded:
        ctx.degrade(
            "正文抓取",
            f"{ctx.fetch_outcome.degraded}/{ctx.fetch_outcome.attempted} 条正文抓取失败，"
            "这些证据只有搜索摘要",
        )

    ctx.thought(
        collector,
        f"采集完成：{len(ctx.evidences)} 条证据，"
        f"{len({independent_domain(ev.url) for ev in ctx.evidences})} 个独立域名，"
        f"其中 {ctx.fetch_outcome.degraded} 条正文降级。"
        f"相关性过滤掉了 {filtered} 条无关结果。",
        stage=stage_name,
    )

    ctx.finish_stage(
        stage_name,
        detail={"evidences": len(ctx.evidences), "queries": len(planned)},
    )


def dimension_coverage(ctx: PipelineContext) -> dict[str, int]:
    """每个维度有多少条证据。

    **这是维度覆盖率的唯一真相源**：判据是"有没有证据锚定到这个维度"，
    而不是"这个字段有没有被模型填过"。后者恒为 100%——模型总是会把
    字段填上的，填的是不是真的则是另一回事。
    """
    buckets = ctx.evidence_by_dimension()
    return {dimension: len(items) for dimension, items in buckets.items()}


def covered_dimensions(ctx: PipelineContext) -> list[str]:
    coverage = dimension_coverage(ctx)
    return [
        dimension
        for dimension in ctx.dimensions
        if coverage.get(dimension, 0) >= max(1, ctx.mode.min_evidence_per_dimension)
    ]


def weakest_dimensions(ctx: PipelineContext, *, limit: int = 3) -> list[str]:
    """证据最稀薄的维度。返工优先补它们。

    `min_independent_sources` 也参与排序：一个维度即使证据条数够，
    如果全来自同一个域名，它的交叉验证同样是失败的。
    """
    buckets = ctx.evidence_by_dimension()
    scored: list[tuple[float, str]] = []
    for dimension in ctx.dimensions:
        items = buckets.get(dimension, [])
        domains = len({independent_domain(ev.url) for ev in items} - {""})
        meets = 1.0 if domains >= MIN_INDEPENDENT_DOMAINS else 0.5
        score = (len(items) * meets) / max(1, ctx.mode.min_evidence_per_dimension)
        scored.append((score, dimension))
    scored.sort()
    return [dimension for _, dimension in scored[:limit]]
