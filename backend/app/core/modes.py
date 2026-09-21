"""三档调研模式。

为什么要有档位
--------------
"快速看一下"和"给我一份能拿去开会的报告"是两种需求，成本差一个数量级。
如果只有一个档位，用户要么等太久，要么拿到一份不够用的东西。
档位把"我愿意花多少"变成一个明确的、可以写进文档的选择。

每个档位是一组**具体数字**，不是一个模糊的"深浅"标签——
数字才能被测试、被估算成本、被写进报告。

关于 `platforms` 与 `search_angles`
----------------------------------
搜索调用数 ≈ 品牌数 × 维度数 × 平台数 × 角度数，很快就爆炸。
所以每个档位都设 `max_search_calls` 硬上限，采集阶段按它预算，
超了就按维度优先级截断——**宁可少采一个维度并如实报告，也不要超预算跑到一半被掐掉**。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModeConfig:
    key: str
    label: str
    description: str

    #: 最多对比几个品牌（含调研对象自身）
    max_brands: int
    #: 计划维度数上限
    max_dimensions: int
    #: 每个维度至少要有几条证据才算"覆盖住了"
    min_evidence_per_dimension: int
    #: 每个维度至少几个独立域名才算交叉验证通过
    min_independent_sources: int

    #: 是否做舆情标注（要额外 LLM 调用）
    enable_sentiment: bool
    #: 是否生成图表与结构化块（功能矩阵 / 定价表 / 画像卡）
    enable_structured: bool
    #: 报告章节
    sections: tuple[str, ...]

    #: 搜索调用硬上限。预算控制靠它，不靠"感觉差不多了"。
    #: **这是首轮的池子，不含返工**——返工另外算，见下。
    max_search_calls: int
    #: 返工轮共用的检索池（**所有返工轮加起来**，不是每轮各一份）。
    #:
    #: 为什么必须单独给：首轮的查询计划是按剩余预算截断的
    #: （`planned[:budget]`），所以首轮结束时首轮池基本**必然见底**。
    #: 返工若跟首轮共用一个池，它拿到的永远是 0 次——于是
    #: "返工提升 Δ"这个指标在结构上恒为 0，而失败还会伪装成
    #: "补采没有新增证据"，看起来像搜索源的问题。
    #:
    #: 为什么是**共用一个池**而不是每轮各一份：返工的收益是递减的
    #: （第一轮补的是最要命的缺口）。共用一个池时，先跑的轮次先拿钱，
    #: 后跑的轮次只能捡剩下的——这正好和收益递减同向。
    #: 换成"每轮各一份"的话，最坏情况的开销变成
    #: `max_search_calls + max_rework_rounds × 每轮`，一个后跑的、
    #: 收益最低的轮次反而拥有和第一轮一样多的预算。
    #:
    #: 取值约为首轮池的三分之一：返工要能补上缺口，但它是**修补**，
    #: 不该有能力重做一遍。
    rework_search_calls: int
    #: 单次任务最多返工几轮。返工是有成本的，而且收益递减。
    max_rework_rounds: int
    #: 抓取正文的条数上限
    max_fetches: int

    #: 分析用的模型档位。quick 用便宜档，expert 用最强档。
    analysis_tier: str = "aux"
    writing_tier: str = "aux"

    #: 优先覆盖的维度。超出 max_dimensions 时按这个顺序截断。
    priority_dimensions: tuple[str, ...] = field(default_factory=tuple)


_DEFAULT_SECTIONS = (
    "executive_summary",
    "market_overview",
    "feature_comparison",
    "pricing",
    "user_feedback",
    "conclusion",
)


MODE_CONFIG: dict[str, ModeConfig] = {
    "quick": ModeConfig(
        key="quick",
        label="快速",
        description="十几分钟出结论，适合先摸个底",
        max_brands=3,
        max_dimensions=4,
        min_evidence_per_dimension=3,
        min_independent_sources=2,
        enable_sentiment=False,
        enable_structured=False,
        sections=("executive_summary", "feature_comparison", "pricing", "conclusion"),
        max_search_calls=24,
        rework_search_calls=8,
        max_rework_rounds=1,
        max_fetches=12,
        analysis_tier="fast",
        writing_tier="fast",
    ),
    "deep": ModeConfig(
        key="deep",
        label="深度",
        description="完整维度与交叉验证，默认档位",
        max_brands=4,
        max_dimensions=6,
        min_evidence_per_dimension=4,
        min_independent_sources=2,
        enable_sentiment=True,
        enable_structured=True,
        sections=_DEFAULT_SECTIONS,
        max_search_calls=64,
        rework_search_calls=24,
        max_rework_rounds=2,
        max_fetches=40,
        analysis_tier="aux",
        writing_tier="aux",
    ),
    "expert": ModeConfig(
        key="expert",
        label="专家",
        description="全维度 + 多轮返工 + 深度分析，最贵也最慢",
        max_brands=6,
        max_dimensions=8,
        min_evidence_per_dimension=6,
        min_independent_sources=3,
        enable_sentiment=True,
        enable_structured=True,
        sections=_DEFAULT_SECTIONS + ("trends", "swot"),
        max_search_calls=160,
        rework_search_calls=48,
        max_rework_rounds=3,
        max_fetches=100,
        analysis_tier="core",
        writing_tier="core",
    ),
}

DEFAULT_MODE = "deep"


def get_mode(key: str) -> ModeConfig:
    """按 key 取档位。未知 key 退回默认档而不是报错——
    档位是用户输入，一个拼错的模式名不该让整个任务失败。"""
    return MODE_CONFIG.get((key or "").strip().lower(), MODE_CONFIG[DEFAULT_MODE])


def plan_dimensions(mode: ModeConfig, proposed: list[str]) -> list[str]:
    """把计划维度截断到档位上限。

    截断顺序不是任意的：`priority_dimensions` 里列出的维度优先保留，
    其余按原顺序。这样"砍掉哪个维度"是一个可解释的决定，而不是随机的。
    """
    if len(proposed) <= mode.max_dimensions:
        return list(proposed)

    priority = [d for d in mode.priority_dimensions if d in proposed]
    rest = [d for d in proposed if d not in priority]
    return (priority + rest)[: mode.max_dimensions]
