"""报告的对外形态：导出、分享、存档。

流水线内部用的是 dict（`assemble()` 的产物）。这个包只负责把它变成
**离开这个进程之后还能被读懂**的东西——Markdown 给人看，JSON 给机器读。
"""
from __future__ import annotations

from app.core.report.export import (
    citation_problems,
    to_json,
    to_markdown,
)

__all__ = ["citation_problems", "to_json", "to_markdown"]
