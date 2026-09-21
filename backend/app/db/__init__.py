"""数据层：连接、迁移、各表 repo。

三个刻意的选择，都在 `connection.py` 与 `migrations.py` 的注释里展开：
线程局部连接 + WAL（因为流水线要扇出到线程池）、
`PRAGMA user_version` + 有序迁移（可复现，且不必引入 Alembic）、
repo 函数只接受白名单列名（避免 kwargs 变成 SQL 注入面）。
"""
from app.db.connection import (
    close_all,
    get_conn,
    reset_connections,
    transaction,
)
from app.db.migrations import MIGRATIONS, current_version, migrate

__all__ = [
    "MIGRATIONS",
    "close_all",
    "current_version",
    "get_conn",
    "migrate",
    "reset_connections",
    "transaction",
]
