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