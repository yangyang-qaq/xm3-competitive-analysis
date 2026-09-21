"""测试之间共享的断言工具。

**为什么它不在 `conftest.py` 里**：conftest 只放夹具。`Outcome` 是一个数据
形状，集成测试与契约测试都要用它来描述"这次运行产出了什么"，而 pytest 不允许
测试模块直接 import 另一个 conftest（同名模块会被导入两次，两边拿到的是
两个不同的类）。

这个模块不会被 pytest 收集（文件名不以 `test_` 开头）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_QUERY = "对比 Notion 与 Obsidian"
DEFAULT_MODE = "quick"


@dataclass
class Outcome:
    """一次运行的全部产物。

    "怎么把流水线跑起来并拿到它推出去的事件"最终只有这一处描述：
    集成测试与契约测试读的是同一批事件，否则两者会在"哪些事件算数"
    这个最不该有分歧的地方产生分歧。
    """

    task_id: str
    runner: Any
    result: Any
    events: list[tuple[str, dict]] = field(default_factory=list)

    @property
    def types(self) -> list[str]:
        return [type_ for type_, _ in self.events]

    def payloads(self, type_: str) -> list[dict]:
        return [data for event_type, data in self.events if event_type == type_]

    def payload(self, type_: str) -> dict:
        """取某种事件的**唯一**一条载荷。多条时直接失败——
        调用方要的是"那一条"，而"取第一条"会让多出来的那些静默消失。"""
        found = self.payloads(type_)
        assert len(found) == 1, f"{type_} 事件有 {len(found)} 条，预期 1 条"
        return found[0]

    def keys_of(self, type_: str) -> set[str]:
        """某种事件出现过的全部载荷键的并集。

        用并集而不是某一条：可选字段（`elapsedMs` / `detail`）只在部分事件上
        出现，只看一条会漏掉它们——而"漏掉的键"正是契约测试要找的东西。
        """
        merged: set[str] = set()
        for data in self.payloads(type_):
            merged |= set(data)
        return merged

    @property
    def body(self) -> dict:
        assert self.result.report is not None
        return self.result.report.data

    @property
    def ctx(self):
        """流水线上下文。跑完之后断言留在 ctx 里的中间产物用。"""
        return self.runner.context
