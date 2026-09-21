"""模型把对比矩阵转置着给的时候，采集这一侧要当场翻回来并说出来。

**为什么这条链子值得一个端到端用例。** `analysis/matrix.py` 那组单元测试
钉的是判断规则本身，但"`analyze` 真的调了它、真的把结论记进了降级说明、
真的把翻好的矩阵写进了报告"这三件事，单元测试一条都覆盖不到——
把 `orient(...)` 那一行从 `analyze` 里删掉，那组单元测试**全绿**。

而缺陷的样子是：报告的表格和图表都正常渲染，每个数字都在 1–5 之间，
只是全部挂在错的东西上。没有一道别的检查会报。

真实数据里那份转置的报告（7 维度 × 5 品牌）就是这么来的：
模型的 `matrix.brands` 是 5 个、`dimensions` 是 7 个，而 `scores` 给了 7 行 × 5 列。
"""
from __future__ import annotations

import asyncio

from app.providers import mock

#: mock 原本给的那份（品牌优先，2 行品牌 × 4 列维度）。
EXPECTED_SCORES = [[4.0, 3.0, 4.0, 2.0], [3.0, 4.0, 3.0, 4.0]]


def _transposed_comparison() -> dict:
    """把 mock 的对比载荷转置一下，模拟模型给反了。"""
    payload = mock._comparison_payload()
    scores = payload["matrix"]["scores"]
    payload["matrix"]["scores"] = [list(column) for column in zip(*scores, strict=True)]
    return payload


def _run(run_mock_pipeline):
    return asyncio.run(run_mock_pipeline())


# ============================================================
# 转置的那份
# ============================================================


def test_转置的矩阵被翻回品牌优先(run_mock_pipeline, monkeypatch) -> None:
    monkeypatch.setitem(mock._FIXTURES, "analyze_comparison", _transposed_comparison)

    body = _run(run_mock_pipeline).body
    matrix = body["matrix"]

    assert len(matrix["scores"]) == len(matrix["brands"]), (
        "行数应该等于品牌数——不翻回来的话，每一行是一个维度，"
        "而下游（`charts` 与导出）都按品牌优先读它"
    )
    assert matrix["scores"] == EXPECTED_SCORES, "翻回来的矩阵应该与原本那份逐值相同"


def test_转置这件事记进了降级说明(run_mock_pipeline, monkeypatch) -> None:
    """**翻回来还不够。** 悄悄改掉的话，"报告里的数字和模型说的不一样"
    这件事在库里查不到——而这类改动恰恰是最需要留痕的：
    它改的是数据的含义，不是格式。"""
    monkeypatch.setitem(mock._FIXTURES, "analyze_comparison", _transposed_comparison)

    coercion = _run(run_mock_pipeline).body["coercion"]

    assert any("转置" in repair for repair in coercion["repairs"]), (
        f"降级说明里没有提到转置：{coercion['repairs']}"
    )
    assert coercion["degraded"] is True


def test_转置之后图表仍然是好的(run_mock_pipeline, monkeypatch) -> None:
    """末端守卫。翻回来这件事的**可见后果**在这里：柱状图的分类数是 4 个维度，
    每个品牌一条系列、每条 4 个值。

    不翻的话，`charts` 的形状闸会发现宽度对不上，于是**一张矩阵图都不出**——
    报告少两张图，而没有任何一句话解释为什么。
    """
    monkeypatch.setitem(mock._FIXTURES, "analyze_comparison", _transposed_comparison)

    charts = _run(run_mock_pipeline).body["charts"]
    bar = next((c for c in charts if c["chartId"] == "chart-matrix-bar"), None)

    assert bar is not None, "转置的矩阵被形状闸拦掉了，说明 `analyze` 那一翻没生效"
    assert len(bar["spec"]["categories"]) == 4
    assert [len(s["values"]) for s in bar["spec"]["series"]] == [4, 4]


# ============================================================
# 正常那份（对照组）
# ============================================================


def test_正常的矩阵不留下降级说明(run_mock_pipeline) -> None:
    """**这条是上面那两条的对照组。** 没有它的话，一个"无论如何都记一条
    降级"的实现也能让上面两条通过——而那种实现会把降级说明变成一句
    永远在场的噪声，真正的降级就再也看不见了。"""
    coercion = _run(run_mock_pipeline).body["coercion"]

    assert not any("矩阵" in repair for repair in coercion["repairs"]), (
        f"矩阵是好的，却报了降级：{coercion['repairs']}"
    )
