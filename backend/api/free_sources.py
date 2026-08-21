#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""免费源同步 API — 查看源配置/手动触发同步"""
import logging

from fastapi import APIRouter, Depends

from backend.core.free_sources import (
    load_source_config,
    runtime_state,
    sync_free_sources,
)
from backend.auth.routes import get_current_user_from_jwt
from backend.models import User

logger = logging.getLogger("gateway")

router = APIRouter(prefix="/api/free-sources", tags=["free-sources"])


@router.get("")
async def get_free_sources(user: User = Depends(get_current_user_from_jwt)):
    """返回外部免费源配置 + 最近一次同步结果(内存态)。"""
    cfg = load_source_config()
    return {
        "sources": cfg.get("sources", []),
        "meta": cfg.get("meta", {}),
        "runtime": runtime_state(),
    }


@router.post("/sync")
async def run_sync(user: User = Depends(get_current_user_from_jwt)):
    """手动触发一次免费源同步(与定时任务同一函数)。"""
    try:
        result = sync_free_sources()
        return result
    except Exception as e:
        logger.exception("手动同步免费源失败")
        return {"ok": False, "error": str(e)}


# ---- OpenCode Zen 免费模型(自动发现 + 健康检测) ----
@router.get("/zen")
async def zen_status(user: User = Depends(get_current_user_from_jwt)):
    """opencode 免费模型同步/健康检测的最新状态。"""
    from backend.core.zen_sources import runtime_state
    return runtime_state()


@router.post("/zen/sync")
async def zen_sync(user: User = Depends(get_current_user_from_jwt)):
    """立即同步: 拉取 zen 免费模型 → 真实探测 → 更新 opencode 渠道。"""
    from backend.core.zen_sources import sync_zen_free_models
    try:
        return sync_zen_free_models()
    except Exception as e:
        logger.exception("opencode 免费模型同步失败")
        return {"ok": False, "error": str(e)}


@router.post("/zen/health")
async def zen_health(user: User = Depends(get_current_user_from_jwt)):
    """立即健康检测: 重探渠道模型, 连续失败模型自动移出。"""
    from backend.core.zen_sources import health_check_zen_models
    try:
        return health_check_zen_models()
    except Exception as e:
        logger.exception("opencode 健康检测失败")
        return {"ok": False, "error": str(e)}