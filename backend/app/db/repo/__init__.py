"""各表的数据访问函数。

刻意不做 Repository 类。这些函数没有需要维护的状态——连接来自
线程局部的 `get_conn()`，SQL 是模块级常量。包成一堆类只会多出
`self`、`__init__` 和一层无意义的间接。

命名上有一个统一的约定：**`save_*` 接受领域对象，`get_*` / `list_*`
返回领域对象**，只有确实需要投影的查询（`stats`、`facets`、
`cost_summary`）才返回 dict。
"""
from app.db.repo import events, evidences, reports, tasks, traces

__all__ = ["events", "evidences", "reports", "tasks", "traces"]
