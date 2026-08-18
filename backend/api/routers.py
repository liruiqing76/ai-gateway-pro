# ============================================================
# AI 模型网关 Pro — 路由组 CRUD API
# 参照 model-gateway: 手动创建路由组, 勾选模型, 持久化到 DB
# 客户端 model 字段填组名即命中, 组内按质量分排序 fallback
# ============================================================
import json
import logging

from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import JSONResponse

from backend.auth.middleware import get_current_user_from_jwt
from backend.models import get_session, User, Router, Provider

logger = logging.getLogger("gateway.routers")
router = APIRouter(prefix="/api/routers", tags=["routers"])


@router.get("")
async def list_routers(user: User = Depends(get_current_user_from_jwt)):
    """列出用户的所有路由组"""
    session = get_session()
    try:
        rows = session.query(Router).filter_by(user_id=user.id).all()
        data = {r.name: r.models or [] for r in rows}
        return {"data": data}
    finally:
        session.close()


@router.post("")
async def save_router(request: Request, user: User = Depends(get_current_user_from_jwt)):
    """创建或更新一个路由组"""
    body = await request.json()
    name = body.get("name", "").strip()
    models = body.get("models", [])

    if not name:
        raise HTTPException(400, "路由名称必填")
    if not isinstance(models, list):
        raise HTTPException(400, "models 必须是数组")

    session = get_session()
    try:
        existing = session.query(Router).filter_by(user_id=user.id, name=name).first()
        if existing:
            existing.models = models
        else:
            r = Router(user_id=user.id, name=name, models=models, strategy="quality")
            session.add(r)
        session.commit()
        return {"ok": True, "name": name, "models": models}
    finally:
        session.close()


@router.delete("/{name}")
async def delete_router(name: str, user: User = Depends(get_current_user_from_jwt)):
    """删除路由组"""
    session = get_session()
    try:
        r = session.query(Router).filter_by(user_id=user.id, name=name).first()
        if not r:
            raise HTTPException(404, "路由组不存在")
        session.delete(r)
        session.commit()
        return {"ok": True, "deleted": name}
    finally:
        session.close()


@router.post("/init-defaults")
async def init_default_routers(user: User = Depends(get_current_user_from_jwt)):
    """根据当前渠道模型自动生成 quick / high_quality / vision 三组（覆盖已有）"""
    session = get_session()
    try:
        providers = session.query(Provider).filter_by(user_id=user.id, enabled=True).all()
        from backend.core.router_group import build_router_groups
        groups = build_router_groups(providers)
        created = []
        for name, models in groups.items():
            if not models:
                continue
            existing = session.query(Router).filter_by(user_id=user.id, name=name).first()
            if existing:
                existing.models = models
            else:
                session.add(Router(user_id=user.id, name=name, models=models, strategy="quality"))
            created.append(name)
        session.commit()
        return {"ok": True, "created": created}
    finally:
        session.close()