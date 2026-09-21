"""对比矩阵的**朝向**。

问题的形状
--------
`matrix.scores` 是一个二维数组，但 JSON 里没有任何东西说明哪个轴是品牌、
哪个轴是维度——`dimensions` 和 `brands` 是两个平铺的列表。
约定是**品牌优先**：`scores[品牌下标][维度下标]`。这个约定写在
`charts.py`（`scores[brand_index]`）和 `export.py`（`scores[brand_index]`）
两处，但**没有任何东西检查过它**。

模型偶尔会反着给。库里 14 份报告里有一份就是：7 个维度 × 5 个品牌，
存成了 7 行 × 5 列——正好是转置。后果不是"表格画反了"，而是
**画错之后看起来完全正常**：

- `charts._matrix_charts` 按品牌优先读，于是把"第 0 个维度上各品牌的分数"
  当成了"Notion 在各维度上的分数"，图例写着品牌名，柱子的含义是维度。
- 那一行只有 5 个数，而分类轴有 7 个维度名。ECharts 不会报错，
  它画 7 个刻度、只画前 5 根柱子——**后两个维度看起来是"没数据"**。

这正是本项目反复在防的那类失败：一个错位的数组和一个正确的数组，
在界面上长得一模一样。

怎么判
-----
只看形状，因为形状是**唯一**的线索：

- 行数 == 品牌数 且 列数 == 维度数 → 品牌优先，正常。
- 行数 == 维度数 且 列数 == 品牌数 → 转置了，翻回来。
- 都不是 → **说不清**。这时不猜：报 `unknown`，由调用方决定
  （采集侧记一条降级，渲染侧拒绝画表格）。

`brands == dimensions` 时两种情况同时成立（方阵），
**按正常处理**——转置一个方阵在形状上不可证伪，猜错就是把一份
本来正确的矩阵转坏。所以方阵永远算正常。

为什么放在 `analysis/` 而不是 `schemas/`
--------------------------------------
它不是"把畸形 JSON 掰回形状"（那是 coercer 的活），
它是**推断这份数据的语义**。所以它返回的是"翻没翻"这个判断，
而不是一个被悄悄改好的对象——调用方要能把这件事记进降级说明里。
"""
from __future__ import annotations

#: 朝向的三种结论。
BRAND_MAJOR = "brandMajor"
DIMENSION_MAJOR = "dimensionMajor"
UNKNOWN = "unknown"


def orientation(
    dimensions: list[str], brands: list[str], scores: list[list[float]]
) -> str:
    """按形状判断 `scores` 是哪个轴的。

    返回 `BRAND_MAJOR` / `DIMENSION_MAJOR` / `UNKNOWN` 之一。
    """
    if not dimensions or not brands or not scores:
        return UNKNOWN

    n_dims = len(dimensions)
    n_brands = len(brands)
    n_rows = len(scores)
    widths = {len(row) for row in scores}

    # 每一行长度必须一致，否则连"几行几列"都说不清。
    if len(widths) != 1:
        return UNKNOWN
    n_cols = widths.pop()

    # **方阵先判。** 两个条件同时成立时先返回品牌优先：
    # 转置一个方阵在形状上不可证伪，猜错会把一份本来正确的矩阵转坏。
    if n_rows == n_brands and n_cols == n_dims:
        return BRAND_MAJOR
    if n_rows == n_dims and n_cols == n_brands:
        return DIMENSION_MAJOR
    return UNKNOWN


def orient(
    dimensions: list[str], brands: list[str], scores: list[list[float]]
) -> tuple[list[list[float]], str]:
    """返回 `(品牌优先的 scores, 朝向结论)`。

    只有确定是转置时才翻。说不清时**原样返回**——`charts.py` 那边还有
    一道形状闸（列数对不上维度数就不出图），所以原样返回不会画出错位的图，
    它只会让这张图不出现。
    """
    verdict = orientation(dimensions, brands, scores)
    if verdict != DIMENSION_MAJOR:
        return scores, verdict
    return [list(column) for column in zip(*scores, strict=False)], verdict


__all__ = ["BRAND_MAJOR", "DIMENSION_MAJOR", "UNKNOWN", "orient", "orientation"]
