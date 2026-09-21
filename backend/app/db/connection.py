"""SQLite 连接管理。

为什么是线程局部连接
--------------------
流水线把搜索与抓取扇出到线程池（`asyncio.to_thread` / `ThreadPoolExecutor`）。
`sqlite3.Connection` 默认禁止跨线程使用（`check_same_thread=True`），
而把它关掉换来的是一个更难查的问题：两个线程同时用一个连接，
事务边界会互相穿透，表现为"提交了但没写进去"。

线程局部连接是唯一同时满足两边的做法：每个线程一个连接、各自事务，
谁也不会踩到谁。代价是连接数 = 线程数，对单机应用完全可接受。

为什么是 WAL
------------
默认的 rollback journal 模式下，写操作会阻塞所有读操作——而这里
读（SSE 消费、前端轮询）和写（流水线落库）是同时发生的。
WAL 让读写不互相阻塞。`busy_timeout` 则是必要的补充：即使有 WAL，
两个写之间仍然会撞，撞上时等 5 秒重试远好于立刻抛
`database is locked`（参考实现的一个已知症状）。

一个 Windows 上的坑（写进 `问题记录.md`）
---------------------------------------
`uvicorn --reload` 会监视整个工作目录。数据库文件就在其中，
于是**每一次写库都会触发一次热重载**——表现为服务反复重启、请求卡死。
规避方式是限定 `--reload-dir app`，或者干脆别在这个项目里用 `--reload`。
"""
from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.core.config import get_settings

log = logging.getLogger(__name__)

#: 写锁等待时长（毫秒）。见模块 docstring。
_BUSY_TIMEOUT_MS = 5_000

_local = threading.local()
#: 记录所有创建过的连接，供 `close_all()` 关闭——
#: 测试里必须能彻底释放文件句柄，否则 Windows 上删不掉临时库文件。
_all_conns: list[sqlite3.Connection] = []
_all_lock = threading.Lock()


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    # `isolation_level=None` 关掉 sqlite3 模块的隐式事务管理，
    # 改由 `transaction()` 显式控制。隐式管理会在 `executescript` 之类
    # 的地方自动提交，让"原子迁移"变得不确定。
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    _configure(conn)
    with _all_lock:
        _all_conns.append(conn)
    return conn


def get_conn() -> sqlite3.Connection:
    """取当前线程的连接，没有就建一个。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect(get_settings().db_file)
        _local.conn = conn
    return conn


def reset_connections() -> None:
    """丢掉当前线程的连接。

    测试改 `db_path` 之后必须调用：不调用的话，上一个用例的连接
    还挂在 `threading.local` 上，新用例会写进旧库，
    表现为"数据莫名其妙地残留"。
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        with _all_lock:
            if conn in _all_conns:
                _all_conns.remove(conn)
        # 已经关掉的连接再关一次会抛 `sqlite3.Error`。这不是错误：
        # 清理路径重入是正常的（比如测试之间既关连接又重建）。
        with contextlib.suppress(sqlite3.Error):  # pragma: no cover
            conn.close()
        _local.conn = None


def close_all() -> None:
    with _all_lock:
        conns = list(_all_conns)
        _all_conns.clear()
    for conn in conns:
        # 同上：重复关闭不算错误，收尾路径要能承受重入。
        with contextlib.suppress(sqlite3.Error):  # pragma: no cover
            conn.close()
    _local.conn = None


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """显式事务。

    刻意不嵌套：SQLite 没有真正的嵌套事务（SAVEPOINT 有，但语义不同），
    假装支持嵌套会导致内层的 COMMIT 把外层一起提交掉。
    需要原子性的多步写入应该在一个 `transaction()` 里完成。
    """
    conn = get_conn()
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
