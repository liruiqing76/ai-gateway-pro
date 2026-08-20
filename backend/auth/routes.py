# ============================================================
# 智能网关 — 用户认证路由
# 注册 / 登录 / 刷新 / API Key 管理
# ============================================================

import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel, EmailStr, Field

from backend.auth.security import (
    hash_password, verify_password, validate_password_strength,
    generate_api_key, mask_secret,
    create_access_token, create_refresh_token, decode_token,
    check_login_lock, record_login_failure, reset_login_failures,
)
from backend.auth.middleware import get_current_user_from_jwt
from backend.models import get_session, User

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ============================================================
# Schema
# ============================================================
class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    email: EmailStr
    password: str = Field(min_length=8, max_length=64)
    invite_code: str | None = None


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str


# ============================================================
# 注册
# ============================================================
@router.post("/register")
async def register(data: RegisterIn):
    ok, msg = validate_password_strength(data.password)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    session = get_session()
    try:
        # 用户是否存在
        if session.query(User).filter_by(email=data.email).first():
            raise HTTPException(status_code=400, detail="邮箱已被注册")
        if session.query(User).filter_by(username=data.username).first():
            raise HTTPException(status_code=400, detail="用户名已被占用")

        # 创建用户
        user = User(
            username=data.username,
            email=data.email,
            password_hash=hash_password(data.password),
            api_key=generate_api_key(),
            invite_code=secrets.token_hex(4),
            invited_by=None,
            quota_daily=1000,
            quota_used_today=0,
            quota_reset_at=datetime.now() + timedelta(days=1),
        )

        # 邀请码处理
        if data.invite_code:
            inviter = session.query(User).filter_by(invite_code=data.invite_code).first()
            if inviter:
                user.invited_by = inviter.id
                # 给邀请人加额度
                inviter.quota_daily = (inviter.quota_daily or 1000) + 500

        session.add(user)
        session.commit()
        session.refresh(user)

        return {
            "user": user.to_dict(),
            "access_token": create_access_token(user.id, user.role),
            "refresh_token": create_refresh_token(user.id),
            "api_key": user.api_key,
        }
    finally:
        session.close()


# ============================================================
# 登录
# ============================================================
@router.post("/login")
async def login(data: LoginIn):
    session = get_session()
    try:
        user = session.query(User).filter_by(email=data.email).first()
        if not user:
            raise HTTPException(status_code=401, detail="邮箱或密码错误")

        # 锁定检查
        locked, msg = check_login_lock(user)
        if locked:
            raise HTTPException(status_code=423, detail=msg)

        if not verify_password(data.password, user.password_hash):
            record_login_failure(user)
            session.commit()
            raise HTTPException(status_code=401, detail="邮箱或密码错误")

        # 成功
        reset_login_failures(user)
        session.commit()

        return {
            "user": user.to_dict(),
            "access_token": create_access_token(user.id, user.role),
            "refresh_token": create_refresh_token(user.id),
            "api_key": user.api_key,
        }
    finally:
        session.close()


# ============================================================
# 刷新 token
# ============================================================
@router.post("/refresh")
async def refresh(data: RefreshIn):
    payload = decode_token(data.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="刷新 Token 无效")

    session = get_session()
    try:
        user = session.query(User).filter_by(id=int(payload["sub"]), is_active=True).first()
        if not user:
            raise HTTPException(status_code=401, detail="用户不存在")
        return {
            "access_token": create_access_token(user.id, user.role),
            "refresh_token": create_refresh_token(user.id),
        }
    finally:
        session.close()


# ============================================================
# 获取当前用户信息
# ============================================================
@router.get("/me")
async def me(user: User = Depends(get_current_user_from_jwt)):
    return {
        **user.to_dict(),
        "api_key_masked": mask_secret(user.api_key),
    }


# ============================================================
# 修改密码
# ============================================================
@router.post("/change-password")
async def change_password(
    data: ChangePasswordIn,
    user: User = Depends(get_current_user_from_jwt),
):
    if not verify_password(data.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="旧密码错误")
    ok, msg = validate_password_strength(data.new_password)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    session = get_session()
    try:
        # user 来自 JWT 依赖, 其原 session 已关闭 → 需在当前 session 重新加载再改密码
        u = session.query(User).filter_by(id=user.id).first()
        if not u:
            raise HTTPException(status_code=404, detail="用户不存在")
        u.password_hash = hash_password(data.new_password)
        session.commit()
        return {"ok": True, "msg": "密码已修改"}
    finally:
        session.close()


# ============================================================
# 轮换 API Key
# ============================================================
@router.post("/rotate-api-key")
async def rotate_api_key(user: User = Depends(get_current_user_from_jwt)):
    session = get_session()
    try:
        new_key = generate_api_key()
        user.api_key = new_key
        session.commit()
        return {"api_key": new_key, "msg": "API Key 已轮换,旧 Key 立即失效"}
    finally:
        session.close()