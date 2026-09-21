"""FastAPI 应用入口。

与 Verda 的差异：**单一后端**。不再维护 backend/ 与 api/ 两份需要手工同步的副本
（见其 docs/DEPLOYMENT.md）——那份约定唯一的作用是适配 Vercel 的目录要求，
代价是每次改动都要记得同步两遍，个人项目没有理由承担。
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes_dashboard import router as dashboard_router
from app.api.routes_evidences import router as evidences_router
from app.api.routes_experts import router as experts_router
from app.api.routes_health import router as health_router
from app.api.routes_providers import router as providers_router
from app.api.routes_reports import router as reports_router
from app.api.routes_tasks import router as tasks_router
from app.core.config import get_settings
from app.data.loader import roster_size
from app.db.connection import close_all, get_conn
from app.db.migrations import current_version, migrate
from app.db.repo import tasks as tasks_repo

log = logging.getLogger("xm3")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    log.info("xm3 backend v%s starting", __version__)
    for key, value in settings.describe().items():
        log.info("  %-18s %s", key, value)

    # 建表放在启动时，不靠"开发者记得先跑一次迁移脚本"。
    #
    # 这一步漏掉的表现极具迷惑性：`/health` 是好的（它不碰库），
    # 服务日志干干净净，然后**第一个真正读库的请求** 500，
    # 报 `no such table: tasks`。而 `/api/providers` 更坏——它的今日成本
    # 查询吞掉异常返回 0，于是成本栏永远显示"今天 $0"，看起来一切正常。
    #
    # 测试也盖不住：夹具自己调了 `migrate()`，所以开发环境全绿，
    # 只有真的起一个干净数据库的服务才会撞上。
    applied = migrate(get_conn())
    if applied:
        log.info("数据库迁到 v%d，本次应用 %d 个迁移", current_version(get_conn()), len(applied))
    else:
        log.info("数据库已是最新（v%d）", current_version(get_conn()))

    # 上一次进程留下的"正在跑"的任务，此刻全是僵尸：协程已经随进程消失了，
    # 而任务行还写着 running。不清理的话它们永远留在那里，
    # 且在任务列表上和真在跑的一模一样——实测本机 116 行里 104 行是这么来的。
    #
    # 迁移之后、任何请求之前，这是唯一一次可以做这件事的时机：
    # 再晚一点，新进程可能已经建出了**真的在跑**的任务，一起判死就错了。
    stale = tasks_repo.interrupt_unfinished()
    if stale:
        log.warning("清理了 %d 个上次进程遗留的未完成任务（已判为 failed）", stale)

    # 预热专家名册。它是个 `lru_cache`，第一次调用要读 YAML、构建 48 个
    # 对象、算一次摘要。放在这里而不是留给第一个请求：否则"第一个建任务的
    # 用户"要额外等这一下，而它完全可以提前发生。
    # 顺带把"名册文件坏了"变成**启动即失败**——流水线跑到组队那一步才炸，
    # 用户已经等了两分钟。
    log.info("专家名册已就绪：%d 人", roster_size())

    try:
        yield
    finally:
        # 线程局部连接不会自己关。不关的后果在 Windows 上很具体：
        # 进程退出后 SQLite 文件仍被占用，删库/换库的那条命令会失败，
        # 而报错信息指向文件而不是连接。
        close_all()
        log.info("xm3 backend stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="xm3 · 多 Agent 竞品分析系统",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health_router)
    app.include_router(providers_router)
    app.include_router(tasks_router)
    app.include_router(reports_router)
    # 侧边栏那三个入口各有自己的路由模块。**不加在 reports 里**：
    # 证据库读的是全表最大的那张表、名册读的是文件而不是库、
    # 仪表盘做的是聚合——三件事和"某一份报告"没有关系，
    # 塞进 reports 只会让那个文件变成一个什么都能往里放的口袋。
    app.include_router(evidences_router)
    app.include_router(experts_router)
    app.include_router(dashboard_router)
    return app


app = create_app()
