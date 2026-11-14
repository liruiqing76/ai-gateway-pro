# ============================================================
# 智能网关 — 免登录模式(单机自用)开关
#
# 单机部署(监听 127.0.0.1、唯一真实用户)时, 管理面板每天输密码纯属负担。
# 开关打开后: 所有 JWT / API Key 鉴权依赖直接注入"默认用户", 面板与 /v1
# 调用均免凭证。状态持久化在状态目录 auth_state.json(与数据库同级),
# 切换立即生效, 无需重启。
#
# 注意: 网关若暴露到局域网/公网, 必须关闭此开关 —— 局域网内任何设备
# 都能以默认用户身份调用全部接口。
# ============================================================
from __future__ import annotations

import json
import logging
import threading

logger = logging.getLogger("gateway.auth")

_STATE_FILE = "auth_state.json"
_lock = threading.Lock()
# 进程内缓存: None=尚未从盘加载
_auth_disabled: bool | None = None


def _state_path():
    from backend.core.state_paths import state_dir
    return state_dir() / _STATE_FILE


def _load() -> bool:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return bool(data.get("auth_disabled", False))
    except Exception:
        return False


def _persist(disabled: bool) -> None:
    try:
        _state_path().write_text(
            json.dumps({"auth_disabled": disabled}, ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        logger.warning("免登录状态写入失败", exc_info=True)


def auth_disabled() -> bool:
    """免登录模式是否开启(带进程内缓存, 首次读盘)。"""
    global _auth_disabled
    if _auth_disabled is None:
        with _lock:
            if _auth_disabled is None:
                _auth_disabled = _load()
    return _auth_disabled


def set_auth_disabled(disabled: bool) -> None:
    """切换免登录模式并落盘(立即生效)。"""
    global _auth_disabled
    with _lock:
        _auth_disabled = bool(disabled)
        _persist(_auth_disabled)


def default_user():
    """免登录模式下注入的默认用户: 优先 admin, 否则 id 最小的启用用户。"""
    from backend.models import get_session, User
    session = get_session()
    try:
        return (session.query(User).filter_by(is_active=True)
                .order_by((User.role == "admin").desc(), User.id)
                .first())
    finally:
        session.close()
