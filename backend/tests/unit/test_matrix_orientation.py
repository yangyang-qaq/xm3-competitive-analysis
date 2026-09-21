"""对比矩阵的朝向：按形状判，判不出来就说不清。

这一组的价值在于**它守的是一个看不出来的缺陷**。转置的矩阵与正确的矩阵
在界面上长得一模一样——表格有行列、图有柱子、数字都在 1–5 之间。
唯一的差别是每个数字挂在错的东西上。

真实数据里那份转置的报告（7 维度 × 5 品牌，存成 7 行 × 5 列）就是这么
被发现后加的这道闸：它的柱状图有 7 个分类刻度、只有 5 根柱子，
而缺的那两根看起来是"这两个维度没数据"。
"""
from __future__ import annotations

import pytest

from app.core.analysis.matrix import (
    BRAND_MAJOR,
    DIMENSION_MAJOR,
    UNKNOWN,
    orient,
    orientation,
)

DIMS = ["规模", "定价", "生态"]
BRANDS = ["甲", "乙"]

#: 品牌优先（约定）：2 行（品牌）× 3 列（维度）。
BRAND_FIRST = [[4.0, 3.0, 2.0], [1.0, 5.0, 3.0]]

#: 同一个矩阵转置着给：3 行（维度）× 2 列（品牌）。
DIMENSION_FIRST = [[4.0, 1.0], [3.0, 5.0], [2.0, 3.0]]


# ============================================================
# 判朝向
# ============================================================


def test_行列数与品牌维度对得上时是品牌优先() -> None:
    assert orientation(DIMS, BRANDS, BRAND_FIRST) == BRAND_MAJOR


def test_行列数正好反过来时是转置() -> None:
    assert orientation(DIMS, BRANDS, DIMENSION_FIRST) == DIMENSION_MAJOR


def test_方阵一律算正常() -> None:
    """2 个品牌 × 2 个维度时，两个条件同时成立。

    这时**不能**判成转置：转置一个方阵在形状上不可证伪，
    猜错就是把一份本来正确的矩阵转坏了。所以方阵永远走正常那条路。
    """
    square = [[4.0, 3.0], [1.0, 5.0]]
    assert orientation(["规模", "定价"], ["甲", "乙"], square) == BRAND_MAJOR


def test_每行长度不一时说不清() -> None:
    """连"几行几列"都说不清的东西不能猜。"""
    ragged = [[4.0, 3.0, 2.0], [1.0, 5.0]]
    assert orientation(DIMS, BRANDS, ragged) == UNKNOWN


def test_行列数都对不上时说不清() -> None:
    five_by_five = [[1.0] * 5 for _ in range(5)]
    assert orientation(DIMS, BRANDS, five_by_five) == UNKNOWN


@pytest.mark.parametrize(
    "dimensions,brands,scores",
    [
        ([], BRANDS, BRAND_FIRST),
        (DIMS, [], BRAND_FIRST),
        (DIMS, BRANDS, []),
    ],
)
def test_缺一边就说不清(dimensions, brands, scores) -> None:
    """没有维度名或没有品牌名时，形状再好看也没有意义——
    表格的列标题会是一片空白。"""
    assert orientation(dimensions, brands, scores) == UNKNOWN


# ============================================================
# 翻回来
# ============================================================


def test_品牌优先的原样返回() -> None:
    scores, verdict = orient(DIMS, BRANDS, BRAND_FIRST)
    assert verdict == BRAND_MAJOR
    assert scores == BRAND_FIRST


def test_转置的翻回品牌优先() -> None:
    """翻回来之后，`scores[品牌下标][维度下标]` 与直接写的那个矩阵相同——
    这就是"翻对了"的定义。"""
    scores, verdict = orient(DIMS, BRANDS, DIMENSION_FIRST)
    assert verdict == DIMENSION_MAJOR
    assert scores == BRAND_FIRST


def test_说不清时原样返回() -> None:
    """**不猜。** 调用方（`analyze` 记降级、`charts` 拦下不画）按
    `UNKNOWN` 这个结论决定怎么做；在这里擅自转一下，就把
    "这份矩阵坏了"变成了"这份矩阵被悄悄改过"，而后者查不出来。"""
    ragged = [[4.0, 3.0, 2.0], [1.0, 5.0]]
    scores, verdict = orient(DIMS, BRANDS, ragged)
    assert verdict == UNKNOWN
    assert scores == ragged


def test_空输入不炸() -> None:
    assert orient([], [], []) == ([], UNKNOWN)
