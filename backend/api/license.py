# ============================================================
# 智能网关 — 授权/设备绑定管理 API
# 查看本机机器码、绑定状态、解绑、远程锁定开关
# ============================================================

import logging

from fastapi import APIRouter, HTTPException, Depends

from backend.config import settings
from backend.models import get_session, User
from backend.auth.middleware import get_current_user_any
from backend.auth.security import decode_token

logger = logging.getLogger("gateway.license_api")
router = APIRouter(prefix="/api/license", tags=["license"])


@router.get("/machine")
async def get_machine_info(user: User = Depends(get_current_user_any)):
    """返回本机机器码(供前端展示/绑定提示)。"""
    from backend.core.machine import get_machine_code
    hw = get_machine_code() if settings.LICENSE_ENABLED else ""
    return {
        "machine_code": hw,
        "enabled": bool(settings.LICENSE_ENABLED),
        "server_url": settings.LICENSE_SERVER_URL or "",
    }


@router.get("/status")
async def license_status(user: User = Depends(get_current_user_any)):
    """当前账号的设备绑定状态。"""
    from backend.core.machine import get_machine_code
    session = get_session()
    try:
        cur = session.query(User).filter_by(id=user.id).first()
        hw = get_machine_code() if settings.LICENSE_ENABLED else ""
        return {
            "machine_code": hw,
            "bound": bool(cur.machine_code) if cur else False,
            "bound_to": cur.machine_code if cur else None,
            "match": bool(cur and cur.machine_code and cur.machine_code == hw),
            "license_locked": bool(cur and cur.license_locked),
            "license_enabled": bool(settings.LICENSE_ENABLED),
        }
    finally:
        session.close()


@router.post("/unbind")
async def unbind_machine(user: User = Depends(get_current_user_any)):
    """解绑当前账号, 使其可在新设备上登录(需当前设备确认)。

    商业规则: 解绑后本机变成"未绑定", 可注册/绑定新账号。
    注: 此接口可用于自助换机, 但会解除设备绑定保护。
    """
    session = get_session()
    try:
        cur = session.query(User).filter_by(id=user.id).first()
        if cur:
            cur.machine_code = None
            session.commit()
        return {"ok": True, "message": "已解绑, 可在新设备上登录"}
    finally:
        session.close()


@router.post("/lock")
async def set_license_lock(
    lock: bool,
    user: User = Depends(get_current_user_any),
):
    """(管理员/本机) 设置账号锁定状态 — 用于远程锁定手动触发"""
    session = get_session()
    try:
        cur = session.query(User).filter_by(id=user.id, role="user").first()
        # 仅 admin 可锁定他人; 普通用户仅能操作自己(一般不会用)
        target = user
        if cur and user.role == "admin":
            target = cur
        target.license_locked = bool(lock)
        session.commit()
        return {"ok": True, "license_locked": bool(lock)}
    finally:
        session.close()
