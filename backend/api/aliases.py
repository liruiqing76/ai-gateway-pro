# ============================================================
# 智能网关 — 模型别名 API
# 用户自定义别名映射: gpt-4 → high_quality
# ============================================================

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel, Field

from backend.auth.middleware import get_current_user_any
from backend.models import get_session, User, ModelAlias

logger = logging.getLogger("gateway.aliases")
router = APIRouter(prefix="/api/aliases", tags=["aliases"])


# ============================================================
# 请求/响应模型
# ============================================================
class AliasCreate(BaseModel):
    alias: str = Field(..., min_length=1, max_length=128, description="别名, 如 gpt-4")
    target: str = Field(..., min_length=1, max_length=128, description="目标模型/路由组")
    description: Optional[str] = Field(None, max_length=256, description="描述")


class AliasUpdate(BaseModel):
    target: Optional[str] = Field(None, min_length=1, max_length=128)
    description: Optional[str] = Field(None, max_length=256)
    enabled: Optional[bool] = None


class AliasResponse(BaseModel):
    id: int
    alias: str
    target: str
    description: Optional[str]
    enabled: bool
    created_at: str
    updated_at: str


# ============================================================
# CRUD API
# ============================================================
@router.get("")
async def list_aliases(
    user: User = Depends(get_current_user_any),
    enabled_only: bool = Query(False, description="只返回启用的别名"),
):
    """获取用户的所有模型别名"""
    session = get_session()
    try:
        query = session.query(ModelAlias).filter_by(user_id=user.id)
        if enabled_only:
            query = query.filter_by(enabled=True)
        aliases = query.order_by(ModelAlias.created_at.desc()).all()

        return {
            "total": len(aliases),
            "data": [
                {
                    "id": a.id,
                    "alias": a.alias,
                    "target": a.target,
                    "description": a.description,
                    "enabled": a.enabled,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                    "updated_at": a.updated_at.isoformat() if a.updated_at else None,
                }
                for a in aliases
            ],
        }
    finally:
        session.close()


@router.post("")
async def create_alias(
    body: AliasCreate,
    user: User = Depends(get_current_user_any),
):
    """创建模型别名"""
    session = get_session()
    try:
        # 检查别名是否已存在
        existing = session.query(ModelAlias).filter_by(
            user_id=user.id, alias=body.alias
        ).first()
        if existing:
            raise HTTPException(409, f"别名 '{body.alias}' 已存在")

        alias = ModelAlias(
            user_id=user.id,
            alias=body.alias.strip(),
            target=body.target.strip(),
            description=body.description,
        )
        session.add(alias)
        session.commit()
        session.refresh(alias)

        logger.info("user %d created alias: %s -> %s", user.id, alias.alias, alias.target)
        return {
            "id": alias.id,
            "alias": alias.alias,
            "target": alias.target,
            "description": alias.description,
            "enabled": alias.enabled,
            "created_at": alias.created_at.isoformat() if alias.created_at else None,
            "updated_at": alias.updated_at.isoformat() if alias.updated_at else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("create alias failed")
        raise HTTPException(500, f"创建失败: {e}")
    finally:
        session.close()


@router.put("/{alias_id}")
async def update_alias(
    alias_id: int,
    body: AliasUpdate,
    user: User = Depends(get_current_user_any),
):
    """更新模型别名"""
    session = get_session()
    try:
        alias = session.query(ModelAlias).filter_by(
            id=alias_id, user_id=user.id
        ).first()
        if not alias:
            raise HTTPException(404, "别名不存在")

        if body.target is not None:
            alias.target = body.target.strip()
        if body.description is not None:
            alias.description = body.description
        if body.enabled is not None:
            alias.enabled = body.enabled

        session.commit()
        session.refresh(alias)

        logger.info("user %d updated alias %d: %s -> %s", user.id, alias.id, alias.alias, alias.target)
        return {
            "id": alias.id,
            "alias": alias.alias,
            "target": alias.target,
            "description": alias.description,
            "enabled": alias.enabled,
            "created_at": alias.created_at.isoformat() if alias.created_at else None,
            "updated_at": alias.updated_at.isoformat() if alias.updated_at else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("update alias failed")
        raise HTTPException(500, f"更新失败: {e}")
    finally:
        session.close()


@router.delete("/{alias_id}")
async def delete_alias(
    alias_id: int,
    user: User = Depends(get_current_user_any),
):
    """删除模型别名"""
    session = get_session()
    try:
        alias = session.query(ModelAlias).filter_by(
            id=alias_id, user_id=user.id
        ).first()
        if not alias:
            raise HTTPException(404, "别名不存在")

        alias_str = alias.alias
        session.delete(alias)
        session.commit()

        logger.info("user %d deleted alias %d: %s", user.id, alias_id, alias_str)
        return {"ok": True, "deleted": alias_str}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("delete alias failed")
        raise HTTPException(500, f"删除失败: {e}")
    finally:
        session.close()


@router.post("/batch")
async def batch_create_aliases(
    aliases: list[AliasCreate],
    user: User = Depends(get_current_user_any),
):
    """批量创建别名"""
    session = get_session()
    try:
        created = []
        skipped = []
        for a in aliases:
            existing = session.query(ModelAlias).filter_by(
                user_id=user.id, alias=a.alias
            ).first()
            if existing:
                skipped.append(a.alias)
                continue

            alias = ModelAlias(
                user_id=user.id,
                alias=a.alias.strip(),
                target=a.target.strip(),
                description=a.description,
            )
            session.add(alias)
            created.append(a.alias)

        session.commit()

        logger.info("user %d batch created %d aliases, skipped %d", user.id, len(created), len(skipped))
        return {"created": len(created), "skipped": skipped}
    except Exception as e:
        session.rollback()
        logger.exception("batch create aliases failed")
        raise HTTPException(500, f"批量创建失败: {e}")
    finally:
        session.close()
