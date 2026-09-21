"""维度覆盖率：判据是"有没有证据锚定"，不是"模型有没有填过这个字段"。

这个文件守的是一个**判据**，不是一段逻辑
--------------------------------------
覆盖率是全系统最容易被算成 100% 的指标。两种算法都通得过 code review：

  甲、`len(模型返回的维度) / len(计划维度)` —— 模型总是会把字段填上，
      所以它恒为 100%。参考实现就是这么算的（`Evidence.matched_dimensions`
      的注释里记着这件事）。
  乙、`len(有证据的维度) / len(计划维度)`，但只看"有没有"不看"够不够"。

甲的危险不在于数字好看，而在于它会让"覆盖率"这个质量门永远通过——
一个永远通过的质量门等于没有质量门。所以这里的断言**同时钉住两个错答案**：
真算成 1.0 说明判据换成了甲，算成 0.5 说明门槛被忽略了。两种都要报出来，
否则后人只看到"数字不对"，不知道错在哪条路上。

证据条数的门槛来自档位（`min_evidence_per_dimension`），所以最后一组测试
用同一批证据在三个档位上算出三个不同的覆盖率——**门槛真的被读了**。
"""
from __future__ import annotations

import pytest

from app.core.analysis.metrics import compute_metrics
from app.core.models import Evidence
from app.core.modes import MODE_CONFIG, get_mode
from app.core.observability.events import EventJournal
from app.core.observability.trace import Tracer
from app.core.pipeline.collect import covered_dimensions, dimension_coverage
from app.core.pipeline.context import PipelineContext

DIMENSIONS = ["功能", "定价", "用户口碑", "协作"]
#: 不在计划里的维度。证据可以命中它（搜索会返回计划外的东西），
#: 但它不该参与覆盖率。
OFF_PLAN = "渠道"


def make_ctx(dimensions: list[str] | None = None, mode: str = "quick") -> PipelineContext:
    """一个够算覆盖率用的最小上下文。provider 全是 None：
    覆盖率是纯函数，碰 provider 会在这里炸成 AttributeError。"""
    task_id = "TK-coverage"
    ctx = PipelineContext(
        task_id=task_id,
        query="对比 Notion 与 Obsidian",
        mode=get_mode(mode),
        tracer=Tracer(task_id),
        journal=EventJournal(task_id),
        llm=None,
        search=None,
        fetcher=None,
    )
    ctx.dimensions = list(DIMENSIONS if dimensions is None else dimensions)
    return ctx


def anchor(ctx: PipelineContext, dimension: str, count: int) -> None:
    """往 ctx 里放 `count` 条**锚定到某个维度**的证据。

    域名刻意各不相同：覆盖率不看域名，但改写这条证据的来源分布
    不该改变覆盖率——分开的域名让这个前提在测试里也是真的。
    """
    for index in range(count):
        ctx.evidences.append(
            Evidence(
                evidence_id=f"EV-{dimension}-{index}",
                url=f"https://source-{dimension}-{index}.example.com/post",
                title=f"{dimension} 的第 {index} 条",
                matched_dimensions=[] if dimension == "" else [dimension],
            )
        )
    ctx.reindex()


def test_覆盖率的判据是有没有证据而不是模型有没有填() -> None:
    """本文件的核心断言，同时钉住两个错答案。"""
    ctx = make_ctx()
    threshold = ctx.mode.min_evidence_per_dimension
    anchor(ctx, "功能", threshold)      # 刚好够
    anchor(ctx, "定价", threshold - 1)  # 差一条，不算
    # 用户口碑 / 协作：一条都没有

    covered = covered_dimensions(ctx)
    coverage = compute_metrics(ctx)["dimensionCoverage"]

    assert covered == ["功能"], f"被算成覆盖的有：{covered}"
    assert coverage == 0.25, (
        f"覆盖率算成了 {coverage}。"
        f"1.0 = 它数的是『模型填过几个维度』，那个判据恒为 100%，质量门永远不会拦下任何东西；"
        f"0.5 = 它忽略了每条维度至少 {threshold} 条证据的门槛。"
    )


def test_门槛来自档位而不是写死的常数() -> None:
    """同一批证据，三个档位三个答案。

    门槛如果被写死（或干脆没读 `ctx.mode`），这三个数会一样——
    而"覆盖率 40%"在不同档位下本该是不同的严格程度。
    """
    results = {}
    for mode in ("quick", "deep", "expert"):
        ctx = make_ctx(mode=mode)
        anchor(ctx, "功能", 4)
        results[mode] = compute_metrics(ctx)["dimensionCoverage"]

    assert results["quick"] == 0.25, results        # 门槛 3，4 条够
    assert results["deep"] == 0.25, results         # 门槛 4，刚好够
    assert results["expert"] == 0.0, results        # 门槛 6，4 条不够
    assert results["quick"] == results["deep"] > results["expert"], results


@pytest.mark.parametrize("count", [0, 1, 2])
def test_不够门槛的维度不算覆盖(count: int) -> None:
    """门槛以下一律是"没覆盖"——包括"差一条"。

    差一条和一条都没有在覆盖率上不加区分，这是有意的：覆盖率回答的是
    "这个维度有没有足够的材料支撑结论"，而不是"有没有材料"。
    """
    ctx = make_ctx(dimensions=["功能"])
    anchor(ctx, "功能", count)

    assert covered_dimensions(ctx) == []
    assert compute_metrics(ctx)["dimensionCoverage"] == 0.0


def test_刚好够门槛的维度算覆盖() -> None:
    """下界是闭区间。写成 `>` 的话"刚好够"会被判成没覆盖，
    而那是所有档位里最常见的情况。"""
    ctx = make_ctx(dimensions=["功能"])
    anchor(ctx, "功能", ctx.mode.min_evidence_per_dimension)

    assert covered_dimensions(ctx) == ["功能"]
    assert compute_metrics(ctx)["dimensionCoverage"] == 1.0


def test_计划外的维度不进分子分母但留在明细里() -> None:
    """搜索会返回计划外的维度（这里锚定到「渠道」，它不在 `ctx.dimensions` 里）。

    **分子分母必须只认计划**：进分子的话覆盖率能超过 1.0；
    进分母的话"计划了 4 个维度"会变成一个会自己变大的数。

    **但明细里保留它**，这是有意的：明细是"证据锚到了哪些维度"的原始计数，
    把计划外的那些删掉，就等于丢掉了"这次调研发现了计划外的维度"
    这个信息——而发现漏掉的维度，本来就是这个系统该干的事之一。
    两件事不冲突，因为它们答的不是同一个问题：
    标量答"计划完成得怎么样"，明细答"证据落在哪儿"。

    代价记在这里：将来报告页照着明细渲染时，**计划外的那些要单独说**，
    不能混在计划的维度里显示成一个并列的条目。
    """
    ctx = make_ctx()
    anchor(ctx, OFF_PLAN, 10)

    metrics = compute_metrics(ctx)
    assert covered_dimensions(ctx) == []
    assert metrics["dimensionCoverage"] == 0.0
    assert metrics["dimensionsPlanned"] == len(DIMENSIONS)
    # 明细 = 计划内（哪怕是 0）+ 计划外（有证据的）。
    assert set(metrics["dimensionCoverageDetail"]) == {*DIMENSIONS, OFF_PLAN}
    assert metrics["dimensionCoverageDetail"][OFF_PLAN] == 10
    # 明细的合计**不**是覆盖率的分母：分母是 `dimensionsPlanned`。
    assert sum(metrics["dimensionCoverageDetail"].values()) == 10


def test_没有命中任何维度的证据不给任何维度计数() -> None:
    """一条证据可以谁都不属于（相关性过滤放它进来，但它没锚到任何维度）。
    把它算给任意一个维度都是编的。"""
    ctx = make_ctx(dimensions=["功能"])
    anchor(ctx, "", 5)

    assert covered_dimensions(ctx) == []
    assert dimension_coverage(ctx) == {"功能": 0}


def test_明细给每个计划维度一个数包括零() -> None:
    """`dimensionCoverageDetail` 要能直接渲染。

    缺键和 0 在界面上长得一样（都渲染成空白），但一个是"这个维度没搜到"
    另一个是"这个维度没被计划"——降级横幅要按前者说话。
    """
    ctx = make_ctx()
    anchor(ctx, "功能", 3)

    detail = compute_metrics(ctx)["dimensionCoverageDetail"]

    assert set(detail) == set(DIMENSIONS), f"明细漏了维度：{set(DIMENSIONS) - set(detail)}"
    assert detail["功能"] == 3
    assert detail["定价"] == 0


def test_覆盖率的分子分母在报告里都对得上() -> None:
    """报告里三个数是同一个事实的三种呈现，必须自洽。
    对不上的话，面试里被问一句"40% 是怎么算的"就答不出来了。"""
    ctx = make_ctx()
    anchor(ctx, "功能", 5)
    anchor(ctx, "定价", 3)

    metrics = compute_metrics(ctx)

    assert metrics["dimensionsPlanned"] == 4
    assert metrics["dimensionsCovered"] == 2
    assert metrics["dimensionCoverage"] == round(2 / 4, 4)
    assert sum(metrics["dimensionCoverageDetail"].values()) == 8


def test_一个维度都没计划时不会除以零() -> None:
    """`len(ctx.dimensions) or 1` 那个兜底要有测试。

    需求理解失败时 `dimensions` 可能是空的，而覆盖率是**每份报告都会算**的
    指标——这里抛 ZeroDivisionError 等于整份报告装配不出来。
    """
    ctx = make_ctx(dimensions=[])

    metrics = compute_metrics(ctx)

    assert metrics["dimensionCoverage"] == 0.0
    assert metrics["dimensionsPlanned"] == 0


def test_每个档位的门槛都为正() -> None:
    """门槛为 0 的话"有证据"的门槛消失，覆盖率退化回"有没有"——
    正是这个文件要挡的那个判据。"""
    for key, mode in MODE_CONFIG.items():
        assert mode.min_evidence_per_dimension >= 1, f"{key} 档的门槛是 {mode.min_evidence_per_dimension}"
