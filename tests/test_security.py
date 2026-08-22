# ============================================================
# 安全模块测试
# ============================================================
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from backend.auth.security import (
    hash_password, verify_password, validate_password_strength,
    create_access_token, create_refresh_token, decode_token,
    generate_api_key, encrypt_secret, decrypt_secret, mask_secret,
)


class TestPassword:
    def test_hash_and_verify(self):
        h = hash_password("Test1234!")
        assert h != "Test1234!"
        assert verify_password("Test1234!", h)

    def test_wrong_password_fails(self):
        h = hash_password("Test1234!")
        assert not verify_password("wrong", h)

    def test_salt_random(self):
        """同一密码两次哈希不同(盐随机)"""
        h1 = hash_password("Test1234!")
        h2 = hash_password("Test1234!")
        assert h1 != h2

    def test_strength_validation(self):
        ok, _ = validate_password_strength("Test1234!")
        assert ok is True
        ok, _ = validate_password_strength("short")
        assert ok is False
        ok, _ = validate_password_strength("alllowercase1")  # 无大写
        assert ok is False
        ok, _ = validate_password_strength("ALLUPPERCASE1")  # 无小写
        assert ok is False
        ok, _ = validate_password_strength("NoDigitsHere!")
        assert ok is False


class TestJWT:
    def test_create_and_decode(self):
        token = create_access_token(1)
        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == "1"
        assert payload["type"] == "access"

    def test_refresh_token(self):
        token = create_refresh_token(2)
        payload = decode_token(token)
        assert payload["type"] == "refresh"
        assert payload["sub"] == "2"

    def test_invalid_token(self):
        assert decode_token("garbage.token.here") is None
        assert decode_token("") is None

    def test_tampered_token(self):
        token = create_access_token(1)
        # 篡改 payload 里的 sub
        parts = token.split(".")
        parts[1] = "eyJzdWIiOiIyIn0"  # {"sub":"2"}
        tampered = ".".join(parts)
        assert decode_token(tampered) is None


class TestAPIKey:
    def test_generate(self):
        key = generate_api_key()
        assert key.startswith("sk-gw-")
        assert len(key) > 10
        # 两次生成不同
        key2 = generate_api_key()
        assert key != key2

    def test_mask(self):
        assert mask_secret("sk-gw-abcdefghijklmnopqrstuvwxyz123456") == "sk-gw-****3456"
        assert mask_secret("") == ""
        assert mask_secret("short") == "shor****"  # 短密钥也用前缀+掩码

    def test_mask_none(self):
        assert mask_secret(None) == ""

    def test_verify_invalid_hash(self):
        """损坏的哈希 → False 而非抛异常"""
        assert verify_password("Test1234!", "not-a-valid-bcrypt-hash") is False
        assert verify_password("Test1234!", "") is False

    def test_login_lock_and_failure(self):
        from backend.auth.security import check_login_lock, record_login_failure, reset_login_failures
        from datetime import datetime, timedelta

        # 解锁状态
        u = type("U", (), {"locked_until": None, "login_attempts": 0})()
        locked, msg = check_login_lock(u)
        assert locked is False
        assert msg == ""

        # 锁定 30 分钟
        u2 = type("U", (), {"locked_until": datetime.now() + timedelta(minutes=5), "login_attempts": 3})()
        locked, msg = check_login_lock(u2)
        assert locked is True
        assert "分钟" in msg

        # 达到阈值触发锁定
        u3 = type("U", (), {"locked_until": None, "login_attempts": 4})()
        locked = record_login_failure(u3)
        assert locked is True     # 达到最大值后锁定
        assert u3.locked_until is not None
        assert u3.login_attempts == 0  # 计数清零

        # 未达阈值只计数
        u4 = type("U", (), {"locked_until": None, "login_attempts": 1})()
        locked = record_login_failure(u4)
        assert locked is False
        assert u4.login_attempts == 2

        # 成功登录重置
        reset_login_failures(u4)
        assert u4.login_attempts == 0
        assert u4.locked_until is None


class TestEncryption:
    def test_encrypt_decrypt_roundtrip(self):
        secret = "nvapi-o4FbT3TkHBd6AUlapFEV8peCCH"
        blob = encrypt_secret(secret)
        assert blob != secret  # 加密后不同
        assert decrypt_secret(blob) == secret

    def test_encryption_salt_random(self):
        """同一明文两次加密不同(nonce 随机)"""
        secret = "same-secret-value"
        blob1 = encrypt_secret(secret)
        blob2 = encrypt_secret(secret)
        assert blob1 != blob2
        assert decrypt_secret(blob1) == secret
        assert decrypt_secret(blob2) == secret