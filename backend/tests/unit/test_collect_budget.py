"""采集预算：首轮和返工**各花各的池子**。

这个文件补的是一条结构性的漏测
------------------------------
`build_queries` 的 docstring 一直写着"纯函数（只读 ctx），可单测。
预算逻辑有明确对错，不该和网络调用混在一起测"——而在写这个文件之前，
**没有任何一个测试调用过它**。主张挂在 docstring 里，从来没被兑现。

后果是一个恒为 0 的指标：首轮的计划是 `planned[:budget]` 截断的，
所以首轮跑完时 `search_calls_made` **正好等于** `max_search_calls`。
返工如果从同一个池子里取余额，它拿到的永远是 0 次检索——于是

  1. 返工一次搜索都发不出去；
  2. `返工提升 Δ`（`metrics_after - metrics_before`）在结构上恒为 0；
  3. 而且失败会**伪装成结论**：编排层会打印"本轮补采没有新增任何证据，
     继续返工不会有新结论"——那句话读起来像搜索源的锅，实际上
     这里根本没发出过请求。

第 3 条是这三条里最坏的：报告里出现了一个**关于外部世界的断言**，
而真相是这个程序自己没去试。

断言盯的是"两个池子互不影响"这件事本身，而不是某次运行的数字。
"""
from __future__ import annotations

import pytest

from app.core.modes import MODE_CONFIG, get_mode
from app.core.observability.events import EventJournal
from app.core.observability.trace import Tracer
from app.core.pipeline.collect import build_queries
from app.core.pipeline.context import PipelineContext

DIMENSIONS = ["功能", "定价", "用户口碑"]

#: 审计给出的返工目标，形状与 `audit.decide_rework` 一致。
TARGETS = [
    {"brand": "Notion", "dimension": "定价", "keyword": "价格"},
    {"brand": "Notion", "dimension": "定价", "keyword": "套餐"},
]


def make_ctx(
    mode: str = "quick",
    *,
    brands: list[str] | None = None,
    search_calls_made: int = 0,
    rework_search_calls_made: int = 0,
    search_angles: list[str] | None = None,
) -> PipelineContext:
    """一个够 `build_queries` 用的最小上下文。

    provider 传 `None`：这个函数是纯的，一个 provider 都不碰
    （真碰了会在这里炸成 `AttributeError`，也算一种守卫）。
    """
    mode_config = get_mode(mode)
    ctx = PipelineContext(
        task_id="TK-budget",
        query="对比 Notion 与 Obsidian",
        mode=mode_config,
        tracer=Tracer("TK-budget"),
        journal=EventJournal("TK-budget"),
        llm=None,
        search=None,
        fetcher=None,
    )
    ctx.brands = list(brands if brands is not None else ["Notion", "Obsidian"])
    ctx.dimensions = list(DIMENSIONS)
    ctx.search_angles = list(search_angles or ["评测"])
    ctx.search_calls_made = search_calls_made
    ctx.rework_search_calls_made = rework_search_calls_made
    return ctx


# ============================================================
# 核心：返工不受首轮余额影响
# ============================================================


def test_首轮池见底时返工照样搜得出去() -> None:
    """**这条就是 A2 的守卫。**

    首轮池用完（`search_calls_made == max_search_calls`）是**常态**，
    不是边角：计划被 `planned[:budget]` 截断，花完才算正常收尾。
    所以这条断言的不是异常情况，而是每一次返工都会走到的路径。

    把池子合回去（`budget = max_search_calls - search_calls_made` 不分
    rework）这条就会红：`planned` 变成空列表。
    """
    mode = get_mode("quick")
    ctx = make_ctx("quick", search_calls_made=mode.max_search_calls)

    planned = build_queries(ctx, targets=TARGETS, rework=True)

    assert planned, "首轮池见底就搜不出返工查询——两个池子又被合回去了"
    assert all(item.kind == "rework" for item in planned)
    assert len(planned) == len(TARGETS)


def test_返工池是独立的一份预算() -> None:
    """返工能花的钱由 `rework_search_calls` 决定，与首轮池的大小无关。"""
    mode = get_mode("quick")
    assert mode.rework_search_calls > 0

    # 首轮一次都没搜过也一样：返工的额度不变。
    assert len(build_queries(make_ctx("quick"), targets=TARGETS, rework=True)) == len(
        TARGETS
    )
    # 首轮搜爆了也一样。
    assert len(
        build_queries(
            make_ctx("quick", search_calls_made=mode.max_search_calls),
            targets=TARGETS,
            rework=True,
        )
    ) == len(TARGETS)


def test_返工池花完就不再搜() -> None:
    mode = get_mode("quick")
    ctx = make_ctx(
        "quick",
        search_calls_made=mode.max_search_calls,
        rework_search_calls_made=mode.rework_search_calls,
    )
    assert build_queries(ctx, targets=TARGETS, rework=True) == []


def test_返工池按剩余额度截断而不是一次性给足() -> None:
    """多轮返工共用一个池：先跑的轮次先花，剩下的按余额截断。

    要是实现写成"每轮各给一份"，这里会返回全部 2 条而不是 1 条。
    """
    mode = get_mode("quick")
    ctx = make_ctx(
        "quick",
        search_calls_made=mode.max_search_calls,
        rework_search_calls_made=mode.rework_search_calls - 1,
    )
    planned = build_queries(ctx, targets=TARGETS, rework=True)
    assert len(planned) == 1
    assert planned[0].kind == "rework"


def test_返工轮不复跑首轮的通用查询() -> None:
    """返工只会跑指定目标。

    复跑第 1~3 轮的话查询文本与首轮**逐字相同**，会拿回同一批 URL，
    去重之后一条新证据都进不来，白白吃掉这个本来就小的池子——
    更糟的是让"Δ 是哪来的"说不清。
    """
    ctx = make_ctx("quick", search_calls_made=get_mode("quick").max_search_calls)
    planned = build_queries(ctx, targets=TARGETS, rework=True)

    kinds = {item.kind for item in planned}
    assert kinds == {"rework"}, f"返工轮混进了别的轮次：{kinds}"
    # 首轮会生成"品牌 维度"这种查询，返工轮一条都不该有。
    generic = {f"{brand} {dim}" for brand in ctx.brands for dim in ctx.dimensions}
    assert not (generic & {item.query.text for item in planned})


def test_返工目标为空时不生成任何查询() -> None:
    """审计说"不用返工"时，返工轮必须一次搜索都不发。"""
    ctx = make_ctx(
        "quick",
        search_calls_made=get_mode("quick").max_search_calls,
        rework_search_calls_made=0,
    )
    assert build_queries(ctx, targets=[], rework=True) == []
    assert build_queries(ctx, targets=None, rework=True) == []


# ============================================================
# 首轮：原行为不变
# ============================================================


def test_首轮按剩余预算截断() -> None:
    """计划比池子大时，截到池子大小，且截的是尾部。

    用 4 品牌 × 4 维度 × 3 角度 = 16 + 12 = 28 条，超过 quick 的 24。
    计划**没有**超过池子时（比如 2 品牌 3 维度），`planned[:budget]`
    一条都不截——所以"首轮必然花光预算"只在计划够大时成立，
    这条测试专门把那个条件摆出来。
    """
    mode = get_mode("quick")
    big = {"brands": ["A", "B", "C", "D"], "search_angles": ["评测", "价格", "口碑"]}
    full = build_queries(make_ctx("quick", **big))
    assert len(full) == mode.max_search_calls, "首轮计划没有被截到上限"

    # 已经花掉 10 次，就只剩 14 次的额度。
    partial = build_queries(make_ctx("quick", search_calls_made=10, **big))
    assert len(partial) == mode.max_search_calls - 10
    # 截断的是**尾部**（低优先级的那些轮次），前面的必须原样保留。
    assert [item.query.text for item in partial] == [
        item.query.text for item in full[: len(partial)]
    ]


def test_首轮池用完就不搜() -> None:
    mode = get_mode("quick")
    ctx = make_ctx("quick", search_calls_made=mode.max_search_calls)
    assert build_queries(ctx) == []


def test_首轮里返工目标排在最前() -> None:
    """带着上一轮明确问题的目标，优先级高于"再普遍搜一遍"。"""
    ctx = make_ctx("quick")
    planned = build_queries(ctx, targets=TARGETS)
    assert [item.kind for item in planned[: len(TARGETS)]] == ["rework"] * len(TARGETS)


def test_首轮不会因为返工目标而少搜普通查询() -> None:
    """返工目标插在前面，普通查询一条不少（额度够的时候）。

    首轮也会带 `targets`（恢复中断的任务时会走到），那时目标占的是
    **前排位置**，不是从别人的额度里抠出来的。
    """
    mode = get_mode("quick")
    with_targets = build_queries(make_ctx("quick"), targets=TARGETS)
    without = build_queries(make_ctx("quick"))

    assert len(with_targets) <= mode.max_search_calls
    plain_with = [item for item in with_targets if item.kind != "rework"]
    plain_without = [item for item in without if item.kind != "rework"]
    assert [item.query.text for item in plain_with] == [
        item.query.text for item in plain_without
    ]


# ============================================================
# 档位配置本身
# ============================================================


@pytest.mark.parametrize("key", sorted(MODE_CONFIG))
def test_每个档位的返工池都小于首轮池(key: str) -> None:
    """返工是**修补**，不该有能力重做一遍。

    上界也要成立：拿首轮池的一半以上去返工，等于把"首轮采得广"
    和"首轮采得深"两件事的取舍反过来——而返工的收益是递减的。
    """
    mode = MODE_CONFIG[key]
    assert 0 < mode.rework_search_calls < mode.max_search_calls


@pytest.mark.parametrize("key", sorted(MODE_CONFIG))
def test_返工池至少够跑一轮完整目标(key: str) -> None:
    """`decide_rework` 最多给 `维度数 × 2` 个目标（`_REWORK_KEYWORDS[:2]`）。

    池子小到这个数以下，就等于"返工永远只做一半"——而报告不会说
    它只做了一半，因为被截断这件事只体现在少了几个查询上。
    """
    mode = MODE_CONFIG[key]
    worst_round = mode.max_dimensions * 2
    assert mode.rework_search_calls >= worst_round, (
        f"{key}: 返工池 {mode.rework_search_calls} < 一轮最坏目标数 {worst_round}"
    )
