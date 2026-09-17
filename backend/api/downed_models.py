#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时下架模型 API — 查看下架列表 / 手动恢复。"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth.routes import get_current_user_from_jwt
from backend.models import User
from backend.core import downed_models

logger = logging.getLogger("gateway")

router = APIRouter(prefix="/api/downed-models", tags=["downed-models"])


class RecoverRequest(BaseModel):
    provider_name: str
    model: str


@router.get("")
async def list_downed_models(
    user: User = Depends(get_current_user_from_jwt),
):
    """返回当前用户仍在临时下架期内的模型列表。"""
    try:
        return {"items": downed_models.list_downed(user.id)}
    except Exception as e:
        logger.exception("list_downed_models failed: %s", e)
        raise HTTPException(status_code=500, detail="查询下架列表失败")


@router.post("/recover")
async def recover_downed_model(
    req: RecoverRequest,
    user: User = Depends(get_current_user_from_jwt),
):
    """手动恢复某个下架模型, 使其立即重新参与路由。"""
    try:
        ok = downed_models.recover_model(user.id, req.provider_name, req.model)
    except Exception as e:
        logger.exception("recover_downed_model failed: %s", e)
        raise HTTPException(status_code=500, detail="恢复操作失败")
    if not ok:
        raise HTTPException(status_code=404, detail="未找到该下架记录, 可能已自动恢复")
    logger.info(
        "user=%s manually recovered downed model %s/%s",
        user.id, req.provider_name, req.model,
    )
    return {"ok": True, "provider_name": req.provider_name, "model": req.model}