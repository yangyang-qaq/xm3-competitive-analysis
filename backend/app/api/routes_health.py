"""健康检查。

`/health` 只回答「进程活着吗 + 当前配置指向谁」，**不做真实的外部调用**——
它的语义必须是廉价且永远可用，否则不能用来做存活探针。
真正的连通性探测在 `/api/providers/health`（会实际打一次 API）。
"""
from __future__ import annotations

from fastapi import APIRouter

from app import __version__
from app.core.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        **settings.describe(),
    }
