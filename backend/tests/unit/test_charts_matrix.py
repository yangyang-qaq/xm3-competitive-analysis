"""矩阵图：宽度对不上维度数就**不出图**。

这是第二道闸。第一道在 `analyze.orient()`——它按形状把"确定是转置"的
矩阵翻回来。但形状说不清的时候它原样放行，所以 `_matrix_charts` 必须
自己再判一次。

为什么值得单独一条闸：ECharts **不会**因为值比分类少而报错。它画 7 个
分类刻度、只画 5 根柱子，而缺的那两根看起来是"这两个维度没数据"——
一个坏掉的矩阵伪装成了两个正常的空值。

库里有 14 份真实报告，其中一份就是转置的（7 维度 × 5 品牌存成 7 行 × 5 列）。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.core.analysis.charts import _matrix_charts, build_charts

DIMS = ["规模", "定价", "生态"]
BRANDS = ["甲", "乙"]

#: 品牌优先（约定）：2 行（品牌）× 3 列（维度）。
BRAND_FIRST = [[4.0, 3.0, 2.0], [1.0, 5.0, 3.0]]


def ctx_with(scores: list[list[float]], dimensions: list[str] = DIMS) -> SimpleNamespace:
    """`_matrix_charts` 只读 `ctx.matrix`，所以给个最小对象就够。"""
    return SimpleNamespace(
        matrix={
            "dimensions": dimensions,
            "brands": BRANDS,
            "scores": scores,
            "evidenceIds": ["EV-a"],
        }
    )


def bar_of(charts: list[dict]) -> dict | None:
    return next((c for c in charts if c["chartId"] == "chart-matrix-bar"), None)


# ============================================================
# 正常那份
# ============================================================


def test_行宽等于维度数时出图() -> None:
    bar = bar_of(_matrix_charts(ctx_with(BRAND_FIRST)))

    assert bar is not None
    assert bar["spec"]["categories"] == DIMS
    # 一个品牌一条系列，值就是它那一行。
    assert [s["brand"] for s in bar["spec"]["series"]] == BRANDS
    assert [s["values"] for s in bar["spec"]["series"]] == BRAND_FIRST


def test_维度不足三个时不出雷达图() -> None:
    """两个维度画出来是一条线——那不是"图不好看"，是会误导读者。"""
    charts = _matrix_charts(ctx_with([[1.0, 2.0], [3.0, 4.0]], dimensions=["规模", "定价"]))
    assert bar_of(charts) is not None
    assert not any(c["kind"] == "radar" for c in charts)


# ============================================================
# 坏掉那份
# ============================================================


def test_行宽对不上维度数时一张图都不出() -> None:
    """**本文件的重点。**

    7 个维度、每行只有 5 个数（真实那份转置报告的形状）。
    照画的话会是 7 个刻度、5 根柱子，而缺的两个看起来是"没数据"。
    """
    seven_dims = ["d1", "d2", "d3", "d4", "d5", "d6", "d7"]
    rows_of_five = [[1.0] * 5 for _ in range(5)]

    charts = _matrix_charts(ctx_with(rows_of_five, dimensions=seven_dims))

    assert charts == [], "宽度对不上维度数还是出了图：缺的那几个维度会伪装成空数据"


def test_只有部分行对不上时也不出图() -> None:
    """一行坏掉就够了——那张图会把坏的那一行画短，别的是好的，
    于是"这一行少了两根柱子"更看不出来。"""
    ragged = [[1.0, 2.0, 3.0], [1.0, 2.0]]

    assert _matrix_charts(ctx_with(ragged)) == []


def test_行数比品牌多时照样出图() -> None:
    """多出来的行被忽略，**不报错**。

    `scores[index] if index < len(scores)` 本来就防了下标越界，
    而"品牌比行多"是另一回事（那会画出空系列）。这里钉的是：
    多的行不影响已经对得上的那几行。
    """
    extra = [[1.0, 2.0, 3.0], [4.0, 5.0, 1.0], [9.0, 9.0, 9.0]]

    bar = bar_of(_matrix_charts(ctx_with(extra)))

    assert bar is not None
    assert [s["values"] for s in bar["spec"]["series"]] == extra[: len(BRANDS)]


def test_没有矩阵时一张图都没有() -> None:
    ctx = SimpleNamespace(
        matrix={"dimensions": [], "brands": [], "scores": [], "evidenceIds": []},
        market_share=[],
        trends=[],
        sentiment={},
        evidences=[],
    )
    assert _matrix_charts(ctx) == []
    assert build_charts(ctx) == []


# ============================================================
# 铁律一：没有证据链的图不出库
# ============================================================


def ctx_full(scores: list[list[float]], *, evidence_ids: list[str]) -> SimpleNamespace:
    """`build_charts` 会走完五个图构建器，也会调 `ctx.degrade`，
    所以要给一个完整的（最小的）上下文，并且记下降级说了什么。"""
    degraded: list[str] = []
    return SimpleNamespace(
        matrix={
            "dimensions": DIMS,
            "brands": BRANDS,
            "scores": scores,
            "evidenceIds": evidence_ids,
        },
        market_share=[],
        trends=[],
        sentiment={},
        evidences=[],
        degrade=lambda block, reason: degraded.append(f"{block}：{reason}"),
        degraded_blocks=degraded,
    )


def test_矩阵没有证据链时不出图并说明原因() -> None:
    """这条是拿真数据跑出来的，不是想出来的。

    黄金集里那条故意含糊的需求（`vague-coffee-market`）跑出来的矩阵
    **有维度、有评分，但 `evidenceIds` 是空的**，于是两张矩阵图带着
    空证据链出了库，只在出库校验里留下一句谁也不会去读的
    `图表 chart-matrix-bar 没有证据链（违反铁律一）`。
    """
    ctx = ctx_full(BRAND_FIRST, evidence_ids=[])

    charts = build_charts(ctx)

    assert bar_of(charts) is None, "没有证据链的图不该出库"
    assert ctx.degraded_blocks, "丢了两张图却一个字都没说"
    note = ctx.degraded_blocks[0]
    assert "2/2" in note or "没有证据链" in note
    # 说明里要能看出**丢的是哪两张**，否则读者只知道"图少了"。
    assert "功能维度评分对比" in note


def test_有证据链时照常出图且不记降级() -> None:
    """收口不能把正常情况也砍掉——那会让一个"更安全"的改动
    悄悄把报告变成没有图表的报告。"""
    ctx = ctx_full(BRAND_FIRST, evidence_ids=["EV-a", "EV-b"])

    charts = build_charts(ctx)

    bar = bar_of(charts)
    assert bar is not None
    assert bar["evidenceIds"] == ["EV-a", "EV-b"]
    assert ctx.degraded_blocks == []


def test_证据链的空字符串不算证据() -> None:
    """`_chart` 里有一句 `[eid for eid in evidence_ids if eid]`——
    空字符串会被它滤掉，所以 `[""]` 与 `[]` 是同一件事。
    收口必须和那句用同一个判据，否则会出现"图出库了、但它的
    evidenceIds 是空的"，也就是收口被绕过去了。
    """
    ctx = ctx_full(BRAND_FIRST, evidence_ids=[""])

    assert build_charts(ctx) == []
    assert ctx.degraded_blocks
