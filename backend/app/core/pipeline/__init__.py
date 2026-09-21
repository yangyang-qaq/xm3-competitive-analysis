"""流水线。

    intake → orchestrator → collect → analyze → audit →(rework→collect|analyze) → write → done

顺序的**唯一真相源**是 `stages.PIPELINE_STAGES`，`orchestrator` 只负责
按它把阶段串起来。这个包不再定义第二份顺序。

阶段的划分方式与参考实现有一个刻意的差别：**分析与写作都在内部拆成了
多次独立调用**（`analyze` 四次、`write` 每章一次），而不是各一次巨型调用。
拆开的代价是调用次数变多；买来的是"部分失败"——某个品牌的材料特别少时，
只有那一块降级，其余照常产出。合成一次调用的话，一次 JSON 畸形就
全部回退，而报告会带着空图表渲染出来，看起来像是"这次没什么可分析的"。
"""
from app.core.pipeline import (
    analyze,
    assemble,
    audit,
    collect,
    dispatch,
    intake,
    orchestrator,
    runner,
)
from app.core.pipeline.context import PipelineContext
from app.core.pipeline.orchestrator import RunResult, run_pipeline
from app.core.pipeline.runner import (
    TaskRunner,
    ensure_runner,
    get_runner,
    load_task,
    new_task_id,
    reset_runners,
)
from app.core.pipeline.stages import (
    PIPELINE_STAGES,
    STAGE_ORDER,
    TERMINAL_STAGE,
    describe_stages,
    progress_at,
    stage_label,
)

__all__ = [
    "PIPELINE_STAGES",
    "STAGE_ORDER",
    "TERMINAL_STAGE",
    "PipelineContext",
    "RunResult",
    "TaskRunner",
    "analyze",
    "assemble",
    "audit",
    "collect",
    "describe_stages",
    "dispatch",
    "ensure_runner",
    "get_runner",
    "intake",
    "load_task",
    "new_task_id",
    "orchestrator",
    "progress_at",
    "reset_runners",
    "run_pipeline",
    "runner",
    "stage_label",
]
