# ============================================================
# 智能网关 — 商业授权 / 设备绑定校验
# 核心规则:
#   1. 一台机器(机器码)只能绑定一个账号(users.machine_code 全局唯一)
#   2. 登录/启动时, 本机机器码必须与账号绑定的一致, 否则整体锁定
#   3. 已绑定账号的机器不能再注册新账号
#   4. 预留远程授权服务器( LICENSE_SERVER_URL ), 可远程封禁
# ============================================================

import asyncio
import logging
from typing import Optional

from backend.config import settings

logger = logging.getLogger("gateway.license")


# ─────────────────────────────────────────────
# 远程授权服务器(可选)
# ─────────────────────────────────────────────

async def _remote_verify(machine_code: str, email: str) -> Optional[dict]:
    """向远程授权服务器校验绑定与封禁状态。

    返回:
      None            — 未配置 LICENSE_SERVER_URL 或请求失败(不阻断, 走本地校验)
      {"ok":True...}  — 服务器认可
      {"ok":False, "locked":True, "message":..} — 服务器判定锁定/封禁
    """
    url = (settings.LICENSE_SERVER_URL or "").rstrip("/")
    if not url:
        return None
    endpoint = f"{url}/verify"
    import httpx
    try:
        async with httpx.AsyncClient(timeout=settings.LICENSE_SERVER_TIMEOUT) as client:
            resp = await client.post(endpoint, json={
                "machine_code": machine_code,
                "email": email,
                "version": settings.APP_VERSION,
            })
            if resp.status_code == 200:
                return resp.json()
            logger.warning("license server returned %d", resp.status_code)
            return {"ok": False, "locked": True,
                    "message": "授权服务器校验失败"}
    except Exception as e:
        logger.warning("license server unreachable: %s", e)
        # 远程不可达: 默认放行(避免断网无法使用), 保留本地校验
        return None


# ─────────────────────────────────────────────
# 本地校验结果
# ─────────────────────────────────────────────

class LicenseResult:
    def __init__(self, ok: bool, message: str = "", locked: bool = False):
        self.ok = ok
        self.message = message
        self.locked = locked


async def verify_local_binding(machine_code: str, email: str) -> LicenseResult:
    """本地校验: 账号 machine_code 与当前机器码是否一致; 账号是否被 license 锁定。

    规则:
      - machine_code 为空(未采集到硬件) → 放行(仅当 LICENSE_ENABLED=False 或采集不到)
      - 账号未绑定机器码 → 放行(首次登录前尚未绑定)
      - 账号已绑定且与当前机器码不同 → 锁定(拒绝)
      - 账号 license_locked=True → 锁定(远程下发)
    """
    from backend.models import get_session, User
    session = get_session()
    try:
        user = session.query(User).filter_by(email=email).first()
        if not user:
            return LicenseResult(False, "账号不存在", locked=True)

        if user.license_locked:
            return LicenseResult(False, "账号已被远程锁定", locked=True)

        # 本机未采集到机器码(非 Windows/无 wmic)且未启用: 放行
        if not machine_code:
            if settings.LICENSE_ENABLED and getattr(settings, "REQUIRE_HARDWARE", False):
                return LicenseResult(False, "无法采集本机机器码, 设备绑定校验失败", locked=True)
            return LicenseResult(True, "")

        # 账号已绑定机器码 → 必须一致
        if user.machine_code:
            if user.machine_code != machine_code:
                return LicenseResult(
                    False,
                    "账号已绑定其他设备, 当前机器与账号不匹配, 已被锁定",
                    locked=True,
                )
            return LicenseResult(True, "")

        # 账号尚未绑定: 若本机机器码已被其他账号占用 → 拒绝(单机单账号)
        other = session.query(User).filter(
            User.machine_code == machine_code, User.id != user.id
        ).first()
        if other:
            return LicenseResult(
                False,
                f"本机已绑定账号 {other.username or other.email}, 一台设备只能绑定一个账号",
                locked=True,
            )
        return LicenseResult(True, "未绑定, 登录时将自动绑定")
    finally:
        session.close()


async def bind_machine_to_account(email: str, machine_code: str) -> LicenseResult:
    """登录/注册成功后, 若账号尚未绑定机器码, 则绑定当前机器码。

    若该机器码已被其他账号占用则拒绝(单机单账号硬约束), 防止抢占。
    """
    from backend.models import get_session, User
    if not machine_code:
        return LicenseResult(True, "")
    session = get_session()
    try:
        other = session.query(User).filter(
            User.machine_code == machine_code
        ).first()
        if other and other.email != email:
            return LicenseResult(
                False,
                f"本机已绑定账号 {other.username or other.email}, 请使用该账号登录",
                locked=True,
            )
        user = session.query(User).filter_by(email=email).first()
        if user and not user.machine_code:
            user.machine_code = machine_code
            session.commit()
        elif user and user.machine_code and user.machine_code != machine_code:
            return LicenseResult(False, "账号已绑定其他设备", locked=True)
        return LicenseResult(True, "")
    finally:
        session.close()


async def full_verify(machine_code: str, email: str) -> LicenseResult:
    """完整校验: 远程(若有) + 本地。本地为主, 远程为辅(可封禁)。"""
    # 远程校验(可选): 服务器明确锁定则直接锁定
    remote = await _remote_verify(machine_code, email)
    if remote is not None:
        if remote.get("locked"):
            return LicenseResult(False,
                                 remote.get("message") or "账号已被远程锁定",
                                 locked=True)
        # 服务器认可(ok=True) → 通过; ok=False 且未 locked → 看本地
        if remote.get("ok"):
            return LicenseResult(True, "")
        # ok=False 未 locked: 服务器存疑, 继续本地校验(保守本地判定)
    return await verify_local_binding(machine_code, email)
