"""图表规格生成。

**图表也是论点**，所以每个 chart spec 都必须带 `evidenceIds`。
参考实现的图表不带证据链，于是报告里最有说服力的部分
（那张雷达图）恰恰是唯一无法追溯的部分。读者看到"我们 4.3 分、对手 3.1 分"，
却不知道这个分数从哪来。

这里生成的只是**规格**（数据 + 类型 + 证据引用），不是渲染结果。
前端用 ECharts 按 spec 画图。这样后端不依赖任何图表库，
图表的样式改动不需要动 Python 代码，而数据部分仍然可以单测。
"""
from __future__ import annotations

from collections.abc import Sequence

from app.core.pipeline.context import PipelineContext

#: 品牌配色。与专家徽章用同一套色系，视觉上是一致的一套系统。
_BRAND_COLORS = (
    "#5B8FF9", "#5AD8A6", "#F6BD16", "#E8684A", "#6DC8EC", "#9270CA",
)


def _chart(
    chart_id: str,
    kind: str,
    title: str,
    *,
    evidence_ids: Sequence[str],
    **spec: object,
) -> dict:
    return {
        "chartId": chart_id,
        "kind": kind,
        "title": title,
        "evidenceIds": [eid for eid in evidence_ids if eid],
        "spec": spec,
    }


def build_charts(ctx: PipelineContext) -> list[dict]:
    """按当前已有的分析结果生成图表。没有数据的图不生成。

    出口处有一道**铁律一的收口**：没有证据链的图一律不生成，并记一条降级。

    为什么收在出口而不是每个 `_chart` 调用点上各自判：调用点有六处，
    各自判的话，新加一种图时很容易漏掉一处，而漏掉的表现是
    "报告里出现了一张没有引用依据的图"——它看起来完全正常，
    只有出库校验会以一句 `图表 chart-xxx 没有证据链（违反铁律一）` 记下来，
    而那句话在报告正文里谁也看不见。

    **实测发现的**：`vague-coffee-market`（黄金集里那条故意含糊的需求）
    跑出来的矩阵有维度、有评分，但 `matrix.evidenceIds` 是空的，
    于是两张矩阵图带着空证据链出了库。收口在这里之后，
    那种情况下报告少两张图、多一句说明——**少一张图远好过
    一张看起来有依据的图**。
    """
    charts: list[dict] = []

    charts.extend(_matrix_charts(ctx))
    charts.extend(_market_share_chart(ctx))
    charts.extend(_trend_chart(ctx))
    charts.extend(_sentiment_chart(ctx))
    charts.extend(_source_distribution_chart(ctx))

    backed = [chart for chart in charts if chart.get("evidenceIds")]
    dropped = [chart for chart in charts if not chart.get("evidenceIds")]
    if dropped:
        names = "、".join(
            str(chart.get("title") or chart.get("chartId")) for chart in dropped
        )
        ctx.degrade(
            "图表",
            f"{len(dropped)}/{len(charts)} 张图的数据没有证据链，已不生成（{names}）",
        )
    return backed


def _matrix_charts(ctx: PipelineContext) -> list[dict]:
    matrix = ctx.matrix or {}
    dimensions = matrix.get("dimensions") or []
    brands = matrix.get("brands") or []
    scores = matrix.get("scores") or []
    evidence_ids = matrix.get("evidenceIds") or []
    if not dimensions or not brands or not scores:
        return []

    # **形状闸：宽度必须等于维度数。**
    #
    # `analyze.orient()` 已经把"确定是转置"的翻回来了，但它是按形状猜的，
    # 而形状说不清的时候它原样返回。这时如果还照画，ECharts 不会报错——
    # 它画 7 个分类刻度、只画 5 根柱子，**缺的那两个维度看起来像"没数据"**，
    # 而不是像"这份矩阵坏了"。
    #
    # 所以这里宁可不画：一张没有的图旁边有降级说明，一张错位的图没有。
    if any(len(row) != len(dimensions) for row in scores):
        return []

    series = []
    for index, brand in enumerate(brands):
        values = scores[index] if index < len(scores) else []
        series.append(
            {
                "brand": brand,
                "color": _BRAND_COLORS[index % len(_BRAND_COLORS)],
                "values": [float(v) for v in values if isinstance(v, (int, float))],
            }
        )

    charts = [
        _chart(
            "chart-matrix-bar",
            "bar",
            "功能维度评分对比",
            evidence_ids=evidence_ids,
            categories=list(dimensions),
            series=series,
            yAxis={"min": 0, "max": 5, "name": "评分"},
        )
    ]

    # 雷达图需要至少 3 个维度才成形；两个维度画出来是一条线，
    # 那不是"图不好看"的问题，是会误导读者。
    if len(dimensions) >= 3:
        charts.append(
            _chart(
                "chart-matrix-radar",
                "radar",
                "能力雷达图",
                evidence_ids=evidence_ids,
                indicators=[{"name": name, "max": 5} for name in dimensions],
                series=series,
            )
        )
    return charts


def _market_share_chart(ctx: PipelineContext) -> list[dict]:
    items = ctx.market_share or []
    rows = [
        {
            "brand": str(item.get("brand", "")),
            "share": float(item.get("share", 0) or 0),
            "basis": str(item.get("basis", "")),
        }
        for item in items
        if isinstance(item, dict) and item.get("brand")
    ]
    if not rows:
        return []

    # **份额闸：`share` 不落在 (0, 100] 里就不画。**
    #
    # 这个名字叫"份额"的字段，模型有时候填的是**用户规模**。库里那份
    # 真实报告里它是一个 `2022.0`，而 basis 字段自己写着
    # "2022年 Notion 用户规模（3000万）"——模型把万人数填进了份额位。
    #
    # 画出来的后果：饼图只有一片，标注 `Notion：2022%`。**它不会报错**，
    # 也不会空——它就是画了一张看着有数据、单位完全错的图。而"份额"
    # 这个标题会让人相信那是个占比。
    #
    # 判据取 (0, 100]：单一占比不可能超过整体，所以越界必然是单位错了。
    # 这与矩阵那条形状闸是同一个思路——**宁可没有图，不要错的图**。
    #
    # 返回 `[]` 而**不是**造一个 `kind: "note"` 的"图"来承载这句话：
    # 那样一来前端要多认一种 kind，而它其实不是图；而且降级说明
    # 该走的是 `coercion` / `degraded` 那条已经存在的公开渠道
    # （`analyze._analyze_comparison` 已经在记了），不是塞进图表数组里。
    if any(not 0 < row["share"] <= 100 for row in rows):
        return []

    # **同一品牌两条份额 = 两个口径，不画。**
    #
    # 库里那份真报告里 `特来电` 有两行：41.0 与 27.4，`basis` 各写着各的出处
    # （中国充电联盟 / 三个皮匠报告）。两行都合法——不同机构的统计口径不同，
    # 而报告在表格里逐行保留了它们。但**合成一张饼图就是错的**：图上会出现
    # 两片"特来电"，同一个品牌被算了两遍，而读者只会读出"集中度很高"。
    #
    # 判据取"品牌名重复"，不取"份额之和是否超过 100"：后者要假定每一行都是
    # 同一时点、同一口径下的完整切分，而 `basis` 恰恰说明它们不是。
    # 与上面那道闸同一个取舍——**宁可没有图，不要错的图**。
    brands = [row["brand"] for row in rows]
    if len(set(brands)) != len(brands):
        # 冲突这件事本身由 `analyze._analyze_comparison` 记进 coercion
        # （它才是知道每行 basis 的地方，也是那道越界闸记账的地方）。
        # 这里只负责**不画**——沿用上面那条注释说的分工：
        # 披露走 coercion / degraded，不塞进图表数组。
        return []

    evidence_ids: list[str] = []
    for item in items:
        if isinstance(item, dict):
            evidence_ids.extend(item.get("evidenceIds") or [])

    return [
        _chart(
            "chart-market-share",
            "pie",
            "市场份额",
            evidence_ids=evidence_ids,
            data=rows,
            # 份额数据经常是推算的，把口径一起画进图里。读者看到
            # "23.5%" 时必须能同时看到"按调研样本推算"。
            note="份额为推算值，口径见各数据点的 basis 字段",
        )
    ]


def _trend_chart(ctx: PipelineContext) -> list[dict]:
    trends = ctx.trends or []
    series = []
    evidence_ids: list[str] = []
    for item in trends:
        if not isinstance(item, dict):
            continue
        points = [p for p in (item.get("points") or []) if isinstance(p, dict)]
        if not points:
            continue
        series.append(
            {
                "name": str(item.get("name", "")),
                "unit": str(item.get("unit", "")),
                "points": [
                    {"period": str(p.get("period", "")), "value": p.get("value")}
                    for p in points
                ],
            }
        )
        evidence_ids.extend(item.get("evidenceIds") or [])
    if not series:
        return []
    return [
        _chart("chart-trends", "line", "趋势", evidence_ids=evidence_ids, series=series)
    ]


def _sentiment_chart(ctx: PipelineContext) -> list[dict]:
    counts = (ctx.sentiment or {}).get("counts") or {}
    total = sum(counts.values())
    if not total:
        return []
    labels = {"positive": "正面", "negative": "负面", "neutral": "中性"}
    evidence_ids = [
        item.get("evidenceId", "") for item in (ctx.sentiment or {}).get("labels", [])
    ]
    return [
        _chart(
            "chart-sentiment",
            "pie",
            "用户评价情感分布",
            evidence_ids=evidence_ids,
            data=[
                {"brand": labels[key], "share": round(counts.get(key, 0) / total * 100, 2)}
                for key in ("positive", "neutral", "negative")
            ],
            # 混合比例直接写进图注：读者不该为了知道"这张图有多少是模型判的"
            # 而去翻另一节。
            note=str((ctx.sentiment or {}).get("note", "")),
        )
    ]


def _source_distribution_chart(ctx: PipelineContext) -> list[dict]:
    if not ctx.evidences:
        return []
    counts: dict[str, int] = {}
    for ev in ctx.evidences:
        counts[ev.source_type] = counts.get(ev.source_type, 0) + 1
    total = sum(counts.values())
    return [
        _chart(
            "chart-source-mix",
            "pie",
            "证据来源分布",
            evidence_ids=[ev.evidence_id for ev in ctx.evidences],
            data=[
                {"brand": key, "share": round(value / total * 100, 2)}
                for key, value in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
            ],
        )
    ]
