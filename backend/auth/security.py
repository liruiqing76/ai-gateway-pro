# ============================================================
# 智能网关 — 安全工具
# 密码哈希、JWT、API Key、AES-256-GCM 加密
# ============================================================

import base64
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from backend.config import settings


# ============================================================
# 密码哈希 (bcrypt)
# ============================================================
def hash_password(password: str) -> str:
    """bcrypt 密码哈希,自动加盐"""
    salt = bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """验证密码"""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def validate_password_strength(password: str) -> tuple[bool, str]:
    """密码强度校验: 8位+大小写+数字"""
    if len(password) < 8:
        return False, "密码至少 8 位"
    if not any(c.isupper() for c in password):
        return False, "密码需包含大写字母"
    if not any(c.islower() for c in password):
        return False, "密码需包含小写字母"
    if not any(c.isdigit() for c in password):
        return False, "密码需包含数字"
    return True, ""


# ============================================================
# JWT
# ============================================================
def create_access_token(user_id: int, role: str = "user") -> str:
    """创建访问 token(24小时)"""
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "exp": expire,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def create_refresh_token(user_id: int) -> str:
    """创建刷新 token(7天)"""
    expire = datetime.now(timezone.utc) + timedelta(days=settings.JWT_REFRESH_DAYS)
    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "exp": expire,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def decode_token(token: str) -> dict | None:
    """解码并验证 JWT"""
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


# ============================================================
# API Key
# ============================================================
def generate_api_key() -> str:
    """生成 API Key: sk-gw-<32位随机>"""
    return f"{settings.API_KEY_PREFIX}{secrets.token_urlsafe(settings.API_KEY_LENGTH)}"


def mask_secret(secret: str) -> str:
    """脱敏: sk-gw-****abcd"""
    if not secret:
        return ""
    if len(secret) <= 12:
        return secret[:4] + "****"
    return secret[:6] + "****" + secret[-4:]


# ============================================================
# AES-256-GCM 渠道 Key 加密
# ============================================================
def _derive_key() -> bytes:
    """从配置密钥派生 32 字节 AES 密钥"""
    return hashlib.sha256(settings.ENCRYPTION_KEY.encode("utf-8")).digest()


def encrypt_secret(plaintext: str) -> str:
    """AES-256-GCM 加密,返回 base64(nonce + ciphertext + tag)"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = _derive_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode("utf-8")


def decrypt_secret(blob: str) -> str:
    """AES-256-GCM 解密"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = _derive_key()
    aesgcm = AESGCM(key)
    raw = base64.b64decode(blob)
    nonce, ct = raw[:12], raw[12:]
    return aesgcm.decrypt(nonce, ct, None).decode("utf-8")


# ============================================================
# 登录失败锁定
# ============================================================
def check_login_lock(user) -> tuple[bool, str]:
    """检查用户是否被锁定,返回 (是否锁定, 提示)"""
    if user.locked_until and user.locked_until > datetime.utcnow():
        remain = (user.locked_until - datetime.utcnow()).seconds // 60
        return True, f"登录失败次数过多,请 {remain} 分钟后再试"
    return False, ""


def record_login_failure(user) -> bool:
    """记录登录失败,返回是否已锁定"""
    user.login_attempts = (user.login_attempts or 0) + 1
    if user.login_attempts >= settings.LOGIN_MAX_ATTEMPTS:
        user.locked_until = datetime.utcnow() + timedelta(minutes=settings.LOGIN_LOCK_MINUTES)
        user.login_attempts = 0
        return True
    return False


def reset_login_failures(user):
    """登录成功后重置"""
    user.login_attempts = 0
    user.locked_until = None
    user.last_login_at = datetime.utcnow()