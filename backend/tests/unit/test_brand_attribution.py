"""结构化分析的**归属**：这块内容属于哪个品牌，以及该问哪些品牌。

这个文件补的是一条真实漏测
--------------------------
库里那份真报告的 `featureTrees` 里 `特来电` 出现了两次，
`pricingModels` / `personaSets` 各重复一次，而它们的内容**并不相同**
（覆盖率 0.7969 与 0.8065）。前端按品牌做 key，于是两张卡互相顶掉，
其中一份数据在页面上直接消失——**只在浏览器控制台里说了一句**
（见 `问题记录.md` 问题 54）。

两条根因，各对应下面一组用例：

1. **调研对象被当成了品牌。** `ctx.brands[0]` 是"调研对象"，而它经常
   不是品牌：`国内新能源汽车充电桩运营商`、
   `Notion 与 Obsidian 在团队协作场景下的差异`——用户问的那句话本身就是它。
   拿它去问功能树，模型只能拿证据里最响的那个品牌来答。
2. **模型答的品牌名直接覆盖了问的品牌名。** 三个 schema parser 都写着
   `pick(payload, "brand", ..., default=brand)`——payload 里有就用 payload 的。
   对"同一个品牌的两种写法"这是对的（`国家电网` / `国家电网有限公司`），
   对"答的是另一个品牌"就是**把 B 的内容挂在 A 名下**。

为什么原来没被发现：mock 对每个品牌都回同一份写着 `示例品牌` 的载荷，
于是"一个品牌两棵树"在 mock 里是**常态**；而 `e2e` 那份夹具是从真报告里
裁下来的，`featureTrees` 只剩一棵。两个仪器都问不出这个问题。
"""
from __future__ import annotations

from app.core.modes import get_mode
from app.core.observability.events import EventJournal
from app.core.observability.trace import Tracer
from app.core.pipeline.analyze import _same_brand, _structured_brands
from app.core.pipeline.context import PipelineContext


def make_ctx(
    *,
    subject: str = "",
    brands: list[str] | None = None,
    candidates: list[str] | None = None,
) -> PipelineContext:
    """只够 `_structured_brands` 用的最小上下文（不碰任何 provider）。"""
    ctx = PipelineContext(
        task_id="TK-attribution",
        query="对比甲与乙",
        mode=get_mode("quick"),
        tracer=Tracer("TK-attribution"),
        journal=EventJournal("TK-attribution"),
        llm=None,
        search=None,
        fetcher=None,
    )
    ctx.subject = subject
    ctx.brands = list(brands or [])
    ctx.candidate_brands = list(candidates or [])
    return ctx


# ============================================================
# 两个名字是不是同一个品牌
# ============================================================

#: （请求的名字，返回的名字，算不算同一个）。三条规则各自有正反例。
SAME_BRAND_CASES = [
    ("特来电", "特来电", True),
    ("  特来电 ", "特来电", True),
    ("Notion", "notion", True),
    # 子串：企业全名与简称。真实 provider 这两种写法都出现过。
    ("国家电网", "国家电网有限公司", True),
    ("星星充电", "星星充电（万帮数字能源）", True),
    ("Notion", "Notion Labs", True),
    # 三个字加了后缀，仍然是同一个：子串这条规则宁可放宽，
    # 代价只是"本该丢的留下了一条"（内容看得见、可核对），
    # 而收紧的代价是"本该留的丢了"（报告少一块，读者无从知道缺了什么）。
    ("特来电", "特来电新能源", True),
    # 真的不是同一个：库里那次就是这两行。
    ("国内新能源汽车充电桩运营商", "特来电", False),
    ("Notion", "Obsidian", False),
]


def test_同一个品牌的写法差异不算归属错() -> None:
    for requested, returned, expected in SAME_BRAND_CASES:
        got = _same_brand(requested, returned)
        assert got is expected, (
            f"「{requested}」与「{returned}」判成了 {got}，应该是 {expected}"
        )


def test_名字有一边是空的不拦() -> None:
    """没得比的时候不拦：拦截要有确凿的理由，不能靠猜。

    空的那边由 `completeness` 去说——它本来就是干这个的。
    """
    assert _same_brand("", "特来电") is True
    assert _same_brand("特来电", "") is True


def test_调研对象那句短语不会被当成同一个品牌() -> None:
    """库里那条真实的请求/返回。**这条用例就是那次缺陷的形状。**"""
    assert _same_brand("Notion 与 Obsidian 在团队协作场景下的差异", "Notion") is False


# ============================================================
# 该问哪些品牌
# ============================================================


def test_调研对象不在竞品清单里就不问它() -> None:
    """充电桩那份报告的形状：调研对象是行业名，竞品是三个公司。"""
    ctx = make_ctx(
        subject="国内新能源汽车充电桩运营商",
        brands=["国内新能源汽车充电桩运营商", "特来电", "星星充电", "国家电网"],
        candidates=["特来电", "星星充电", "国家电网"],
    )

    assert _structured_brands(ctx) == ["特来电", "星星充电", "国家电网"]


def test_调研对象本身就是竞品时照样问() -> None:
    """反向守卫：别写成"永远去掉第一个"。

    "对比 Notion 与 Obsidian" 这种问法里 subject 可能就是 Notion 本身，
    而它同时也在候选清单里——那就该问。
    """
    ctx = make_ctx(
        subject="Notion",
        brands=["Notion", "Obsidian", "Coda"],
        candidates=["Notion", "Obsidian"],
    )

    assert _structured_brands(ctx) == ["Notion", "Obsidian", "Coda"]


def test_没有候选清单时退回原列表() -> None:
    """模型没给候选品牌时（单品牌分析）不能把品牌筛空——
    筛空的后果是这一块整个不产出，而报告只会说"未产出"，
    读者看不出是"没问"还是"问了没答"。"""
    ctx = make_ctx(subject="Notion", brands=["Notion"], candidates=[])

    assert _structured_brands(ctx) == ["Notion"]


def test_空名字被丢掉() -> None:
    """空名字不能进这个循环：`_structured_one` 会拿它去问模型，
    而"问一个没名字的品牌"得到的是模型自由发挥。

    这里 subject 与 candidates 都点名了`甲`，所以**甲要留下**——
    这条同时是"别写成永远去掉第一个"的第二次守卫。
    """
    ctx = make_ctx(subject="甲", brands=["", "甲", "乙"], candidates=["甲", "乙"])
    assert _structured_brands(ctx) == ["甲", "乙"]
