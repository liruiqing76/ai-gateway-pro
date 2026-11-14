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
from backend.auth.middleware import (
    get_current_user_from_jwt, get_current_user_any,
    ensure_quota_window, persist_quota_window,
)
from backend.models import get_session, User
from backend.config import settings

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

        # ── 商业化单机单账号限制: 本机机器码已被其他账号占用则禁止再注册 ──
        machine_code = ""
        if settings.LICENSE_ENABLED:
            from backend.core.machine import get_machine_code
            machine_code = get_machine_code()
            if machine_code:
                owner = session.query(User).filter(
                    User.machine_code == machine_code
                ).first()
                if owner:
                    raise HTTPException(
                        status_code=403,
                        detail=f"本机已绑定账号 {owner.username or owner.email}，一台设备只能注册一个账号。"
                               f"如需更换账号请联系客服解绑。",
                    )

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
            # 首次重置点 = 明天零点(自然日), 而不是注册时刻+24h
            quota_reset_at=datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1),
            machine_code=machine_code or None,  # 绑定额定机器码
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

        # ── 商业化设备绑定校验(登录时执行, 不一致即锁定) ──
        if settings.LICENSE_ENABLED:
            from backend.core.machine import get_machine_code
            from backend.core.license import full_verify, bind_machine_to_account
            hw = get_machine_code()
            res = await full_verify(hw, user.email)
            if not res.ok:
                session.rollback()
                raise HTTPException(
                    status_code=423 if res.locked else 403,
                    detail=res.message or "设备绑定校验失败",
                )
            bind_res = await bind_machine_to_account(user.email, hw)
            if not bind_res.ok:
                session.rollback()
                raise HTTPException(status_code=423, detail=bind_res.message)
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
    # 返回前先做自然日校准: 跨零点后即使还没有新调用, quota_used_today 也显示已重置
    if ensure_quota_window(user):
        persist_quota_window(user)
    return {
        **user.to_dict(),
        "api_key_masked": mask_secret(user.api_key),
    }


# ============================================================
# 免登录模式(单机自用)开关
# ============================================================
class LocalModeIn(BaseModel):
    auth_disabled: bool


@router.get("/local-mode")
async def get_local_mode():
    """查询免登录模式状态(无需凭证: 前端启动探测用, 不含敏感信息)"""
    from backend.auth import local_mode
    return {"auth_disabled": local_mode.auth_disabled()}


@router.post("/local-mode")
async def set_local_mode(
    data: LocalModeIn,
    user: User = Depends(get_current_user_any),
):
    """切换免登录模式。

    鉴权开启时须登录才能打开; 开启后关闭无需凭证(get_current_user_any
    在免登录态注入默认用户)—— 否则关掉登录后自己就被锁在门外了。
    仅建议本机 127.0.0.1 部署时开启, 暴露到局域网/公网必须关闭。
    """
    from backend.auth import local_mode
    local_mode.set_auth_disabled(data.auth_disabled)
    return {"auth_disabled": data.auth_disabled}


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