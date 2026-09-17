#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""死模型(永久拉黑)API — 查看拉黑列表 / 手动平反 / 立即触发自动复检。"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth.routes import get_current_user_from_jwt
from backend.models import User, get_session

logger = logging.getLogger("gateway")

router = APIRouter(prefix="/api/dead-models", tags=["dead-models"])


class RecoverRequest(BaseModel):
    provider_name: str
    model: str


@router.get("")
async def list_dead_models(
    user: User = Depends(get_current_user_from_jwt),
):
    """返回当前用户全部永久拉黑记录(含渠道是否仍存在)。"""
    try:
        s = get_session()
        try:
            from backend.models import DeadModel, Provider
            rows = s.query(DeadModel).filter_by(user_id=user.id).all()
            pnames = {p.name for p in s.query(Provider).filter_by(user_id=user.id)}
        finally:
            s.close()
        return {
            "items": [
                {
                    "provider_name": r.provider_name,
                    "model": r.model,
                    "marked_at": r.marked_at.isoformat() if r.marked_at else None,
                    "provider_exists": r.provider_name in pnames,
                }
                for r in rows
            ]
        }
    except Exception as e:
        logger.exception("list_dead_models failed: %s", e)
        raise HTTPException(status_code=500, detail="查询拉黑列表失败")


@router.post("/sweep")
async def sweep_dead_models(
    user: User = Depends(get_current_user_from_jwt),
):
    """立即对当前用户的拉黑记录做一次真实探测复检(与每日定时同一套逻辑)。

    网络探测放后台线程, 不阻塞事件循环; 探测全部完成前接口不会返回。
    """
    try:
        import asyncio
        from backend.core.dead_model_recovery import recover_dead_models
        stats = await asyncio.to_thread(recover_dead_models, user.id)
    except Exception as e:
        logger.exception("sweep_dead_models failed: %s", e)
        raise HTTPException(status_code=500, detail="复检执行失败, 详情见日志")
    logger.info("user=%s 手动触发死模型复检: %s", user.id, {
        k: v for k, v in stats.items() if k != "revived"
    })
    return {"ok": True, **stats}


@router.post("/recover")
async def recover_dead_model(
    req: RecoverRequest,
    user: User = Depends(get_current_user_from_jwt),
):
    """手动平反某个拉黑模型(不探测, 用户意图优先): 解封 + 补回渠道/路由组。

    若模型其实仍不可用, 运行时会再次移除+拉黑(幂等, 与手动加回语义一致)。
    """
    try:
        from backend.core.dead_model_recovery import revive_model
        ok = revive_model(user.id, req.provider_name, req.model)
    except Exception as e:
        logger.exception("recover_dead_model failed: %s", e)
        raise HTTPException(status_code=500, detail="平反操作失败")
    if not ok:
        raise HTTPException(status_code=404, detail="渠道不存在或拉黑记录不存在")
    logger.info("user=%s manually recovered dead model %s/%s",
                user.id, req.provider_name, req.model)
    return {"ok": True, "provider_name": req.provider_name, "model": req.model}
