"""可观测性：埋点（trace）与事件（events）。

铁律四是"全程可观测"，这个包是它的实现。两条独立的东西：

- `trace`：**内部**记录——每次 LLM / 搜索 / 抓取调用的 prompt、输出、token、成本、耗时。
  落 `traces` 表，喂给决策回放页与成本面板。
- `events`：**对外**推送——SSE 事件流，喂给工作台与断线续传。

分开的理由：trace 是给人事后分析的（量大、要落库、要能按 span 树查询），
event 是给人当下看的（要低延迟、要能补发）。混成一个流会让两边都难受：
思维流里混进几百条 token 统计，或者补发事件时把成本明细也重播一遍。
"""
from app.core.observability.events import (
    EVENT_TYPES,
    EventJournal,
    PipelineEvent,
    journal_for,
    make_journal,
)
from app.core.observability.trace import (
    Span,
    Tracer,
    current_span_id,
    current_tracer,
    span,
    use_tracer,
)

__all__ = [
    "EVENT_TYPES",
    "EventJournal",
    "PipelineEvent",
    "Span",
    "Tracer",
    "current_span_id",
    "current_tracer",
    "journal_for",
    "make_journal",
    "span",
    "use_tracer",
]
