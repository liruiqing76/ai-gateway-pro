# ============================================================
# 智能网关 — 鉴权中间件
# JWT for 管理面板, API Key for API 调用
# ============================================================

from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from backend.models import get_session, User
from backend.auth.security import decode_token

security = HTTPBearer(auto_error=False)


async def get_current_user_from_jwt(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> User:
    """JWT 鉴权(管理面板),从 Authorization header 获取用户"""
    if not credentials:
        raise HTTPException(status_code=401, detail="未提供认证凭证")
    payload = decode_token(credentials.credentials)
    if not payload:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Token 类型错误")
    user_id = int(payload.get("sub", 0))
    session = get_session()
    try:
        user = session.query(User).filter_by(id=user_id, is_active=True).first()
        if not user:
            raise HTTPException(status_code=401, detail="用户不存在或已禁用")
        _check_license_lock(user)
        request.state.user = user
        return user
    finally:
        session.close()


async def get_current_user_from_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> User:
    """API Key 鉴权(API 调用),从 Authorization header 获取用户"""
    if not credentials:
        raise HTTPException(status_code=401, detail="未提供 API Key")
    session = get_session()
    try:
        user = session.query(User).filter_by(
            api_key=credentials.credentials, is_active=True
        ).first()
        if not user:
            raise HTTPException(status_code=401, detail="API Key 无效")
        _check_license_lock(user)
        request.state.user = user
        return user
    finally:
        session.close()


def _check_license_lock(user: User) -> None:
    """整体锁定: 账号被远程/本地标记为 license_locked 时, 拒绝一切功能访问"""
    if getattr(user, "license_locked", False):
        raise HTTPException(status_code=423, detail="该账号已被锁定，请联系客服")


async def get_current_user_any(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> User:
    """双认证: 先按 JWT(管理面板), 失败再按 API Key(OpenAI 兼容客户端)"""
    if not credentials:
        raise HTTPException(status_code=401, detail="未提供认证凭证")
    session = get_session()
    try:
        # 1) JWT
        payload = decode_token(credentials.credentials)
        if payload and payload.get("type") == "access":
            user = session.query(User).filter_by(
                id=int(payload.get("sub", 0)), is_active=True
            ).first()
            if user:
                _check_license_lock(user)
                request.state.user = user
                return user
        # 2) API Key
        user = session.query(User).filter_by(
            api_key=credentials.credentials, is_active=True
        ).first()
        if user:
            _check_license_lock(user)
            request.state.user = user
            return user
        raise HTTPException(status_code=401, detail="凭证无效")
    finally:
        session.close()


def require_admin(user: User = Depends(get_current_user_from_jwt)) -> User:
    """管理员权限校验"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


async def check_quota(user: User = Depends(get_current_user_from_api_key)) -> User:
    """检查用户额度"""
    from datetime import datetime, timedelta
    now = datetime.now()
    # 如果 quota_reset_at 已过,重置
    if user.quota_reset_at and user.quota_reset_at < now:
        user.quota_used_today = 0
        user.quota_reset_at = now + timedelta(days=1)
        session = get_session()
        try:
            session.merge(user)
            session.commit()
        finally:
            session.close()
    if user.quota_used_today >= user.quota_daily:
        raise HTTPException(status_code=429, detail="今日额度已用完,请升级会员")
    return user