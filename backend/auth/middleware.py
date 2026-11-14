# ============================================================
# 智能网关 — 鉴权中间件
# JWT for 管理面板, API Key for API 调用
# 免登录模式(单机自用)开启时全部依赖直接注入默认用户
# ============================================================

from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from backend.models import get_session, User
from backend.auth.security import decode_token

security = HTTPBearer(auto_error=False)


def _local_mode_user(request: Request) -> User | None:
    """免登录模式: 返回注入的默认用户; 用户不存在(未注册)返回 None。"""
    from backend.auth import local_mode
    user = local_mode.default_user()
    if user is not None:
        request.state.user = user
    return user


async def get_current_user_from_jwt(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> User:
    """JWT 鉴权(管理面板),从 Authorization header 获取用户"""
    from backend.auth import local_mode
    if local_mode.auth_disabled():
        user = _local_mode_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="请先注册用户")
        return user
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
    from backend.auth import local_mode
    if local_mode.auth_disabled():
        user = _local_mode_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="请先注册用户")
        return user
    token = credentials.credentials if credentials else None
    # Anthropic 系客户端(Claude Code / Anthropic SDK / Cline 等)默认发 x-api-key
    # 而非 Authorization: Bearer; 两个头都认, Bearer 优先。
    if not token:
        token = request.headers.get("x-api-key")
    if not token:
        raise HTTPException(status_code=401, detail="未提供 API Key")
    session = get_session()
    try:
        user = session.query(User).filter_by(
            api_key=token, is_active=True
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
    from backend.auth import local_mode
    if local_mode.auth_disabled():
        user = _local_mode_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="请先注册用户")
        return user
    token = credentials.credentials if credentials else None
    if not token:
        # 与 get_current_user_from_api_key 同理: Anthropic 系工具发 x-api-key
        token = request.headers.get("x-api-key")
    if not token:
        raise HTTPException(status_code=401, detail="未提供认证凭证")
    session = get_session()
    try:
        # 1) JWT
        payload = decode_token(token)
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
            api_key=token, is_active=True
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


def ensure_quota_window(user: User) -> bool:
    """自然日窗口校准(每日额度懒重置核心)。

    - 锚点缺失: 只补齐到明天零点, 不清零已用次数(避免给超额用户变相续命)
    - 锚点已过(跨零点): 清零当日用量, 锚点推进到明天零点
    返回是否发生变更。供 API 调用链(check_quota)与各展示接口复用,
    保证零点过后无需等待下一次真实调用, 读到的地方就能看到新一天额度。
    """
    from datetime import datetime, timedelta
    now = datetime.now()
    tomorrow_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    changed = False
    if not user.quota_reset_at:
        user.quota_reset_at = tomorrow_midnight
        changed = True
    elif user.quota_reset_at < now:
        user.quota_used_today = 0
        user.quota_reset_at = tomorrow_midnight
        changed = True
    return changed


def persist_quota_window(user: User) -> None:
    """把 ensure_quota_window 的变更写库(user 通常是已关闭 session 的游离实例)"""
    session = get_session()
    try:
        session.merge(user)
        session.commit()
    finally:
        session.close()


async def check_quota(user: User = Depends(get_current_user_from_api_key)) -> User:
    """检查用户额度。每日额度按自然日计: 每天零点(本地时间)重置"""
    if ensure_quota_window(user):
        persist_quota_window(user)
    if user.quota_used_today >= user.quota_daily:
        raise HTTPException(status_code=429, detail="今日额度已用完,请升级会员")
    return user