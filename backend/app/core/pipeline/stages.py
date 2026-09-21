"""流水线阶段定义的**唯一真相源**。

顺序：
    intake → orchestrator → collect → analyze → audit →(rework→collect|analyze) → write → done

这个常量同时供给三处：
1. SSE 首个 `node_update` 事件与进度计算
2. 前端 DAG（`GET /api/pipeline/stages`）
3. **文档一致性测试** —— 解析 `docs/ARCHITECTURE.md` 的 mermaid 块，
   断言节点顺序与本文件一致

第三处是重点。参考项目的架构文档里，流水线顺序写的是
"分析 → 撰写 → 质检"，而代码里实际是"分析 → 质检 → 撰写"。
文档与代码脱节到这种程度时，读文档的人会得出错误的结论，
而且**没有任何机制会提醒他们**。一条测试就能永久解决它。

为什么质检在写作之前
--------------------
写作是最贵的一步（章节要逐节生成，每节都要带上证据上下文）。
质检如果排在写作之后，发现证据不足时的返工成本是"重写全部章节"；
排在之前，返工只是"补采几条 + 重跑一次分析"。
参考项目的文档写反了，但它代码里的实际顺序是对的——这里沿用正确的那个。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StageSpec:
    key: str
    label: str
    description: str
    #: 进度权重。不是"阶段数均分"：采集与分析占了绝大部分耗时，
    #: 均分会让进度条在最慢的阶段以五倍速爬行，然后长时间卡住不动，
    #: 用户会以为它挂了。
    weight: float
    #: 可选阶段：本次运行可能完全不经过它（比如不需要返工）。
    optional: bool = False


PIPELINE_STAGES: tuple[StageSpec, ...] = (
    StageSpec(
        "intake",
        "需求理解",
        "把一句话需求拆成调研对象、候选竞品与澄清问题",
        weight=6.0,
    ),
    StageSpec(
        "orchestrator",
        "专家调度",
        "按调研主题选择专家组队，规划调研维度与搜索角度",
        weight=8.0,
    ),
    StageSpec(
        "collect",
        "联网采集",
        "按维度与平台搜索并抓取正文，产出带可信度评分的证据",
        weight=34.0,
    ),
    StageSpec(
        "analyze",
        "分析研判",
        "从证据中提取论点、对比矩阵、功能树、定价与画像",
        weight=30.0,
    ),
    StageSpec(
        "audit",
        "质量审计",
        "检查证据密度与维度覆盖，产出可驱动返工的问题清单",
        weight=6.0,
    ),
    StageSpec(
        "rework",
        "返工补采",
        "针对质检问题补充采集或重做分析",
        weight=8.0,
        optional=True,
    ),
    StageSpec(
        "write",
        "报告撰写",
        "逐章撰写并挂载证据引用",
        weight=8.0,
    ),
)

#: 顺序化的 key 元组。任何需要"按顺序"的地方都读它。
STAGE_ORDER: tuple[str, ...] = tuple(stage.key for stage in PIPELINE_STAGES)

#: 终态。它不是阶段——没有耗时、没有进度，只是"任务不再变化了"。
TERMINAL_STAGE = "done"

_BY_KEY = {stage.key: stage for stage in PIPELINE_STAGES}
_TOTAL_WEIGHT = sum(stage.weight for stage in PIPELINE_STAGES)


def get_stage(key: str) -> StageSpec | None:
    return _BY_KEY.get(key)


def stage_label(key: str) -> str:
    stage = _BY_KEY.get(key)
    return stage.label if stage else key


def progress_at(stage_key: str, *, substep: float = 0.0) -> float:
    """算进度：已完成阶段权重之和 + 当前阶段的子进度。

    `substep` 是当前阶段内部的完成比例（0–1）。
    """
    substep = max(0.0, min(1.0, substep))
    if stage_key == TERMINAL_STAGE:
        return 1.0
    index = STAGE_ORDER.index(stage_key) if stage_key in STAGE_ORDER else -1
    if index < 0:
        return 0.0
    done = sum(stage.weight for stage in PIPELINE_STAGES[:index])
    return round((done + PIPELINE_STAGES[index].weight * substep) / _TOTAL_WEIGHT, 4)


def next_stage(stage_key: str) -> str:
    if stage_key not in STAGE_ORDER:
        return STAGE_ORDER[0]
    index = STAGE_ORDER.index(stage_key)
    return STAGE_ORDER[index + 1] if index + 1 < len(STAGE_ORDER) else TERMINAL_STAGE


def describe_stages() -> list[dict]:
    """给前端 DAG 用。带 `order` 与累计进度起点，省得前端自己推。"""
    return [
        {
            "key": stage.key,
            "label": stage.label,
            "description": stage.description,
            "order": index,
            "optional": stage.optional,
            "startProgress": progress_at(stage.key),
        }
        for index, stage in enumerate(PIPELINE_STAGES)
    ]
