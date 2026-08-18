# ============================================================
# 认证路由测试
# ============================================================
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.models import create_tables, get_session, User, _get_engine, _engine as models_engine

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    """每个测试前重建表,用临时文件隔离"""
    import os, backend.models, tempfile
    # 重置全局引擎
    backend.models._engine = None
    # 用临时文件数据库(同一连接池共享)
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_db.name}"
    eng = create_tables()
    yield
    # 清理
    eng.dispose()
    backend.models._engine = None
    if "DATABASE_URL" in os.environ:
        del os.environ["DATABASE_URL"]
    try:
        os.unlink(tmp_db.name)
    except:
        pass


class TestRegister:
    def test_register_success(self):
        resp = client.post("/api/auth/register", json={
            "username": "testuser",
            "email": "test@example.com",
            "password": "Test1234!",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["user"]["username"] == "testuser"
        assert data["user"]["email"] == "test@example.com"
        assert data["user"]["tier"] == "free"
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["api_key"].startswith("sk-gw-")

    def test_register_duplicate_email(self):
        client.post("/api/auth/register", json={
            "username": "user1", "email": "dup@example.com", "password": "Test1234!"
        })
        resp = client.post("/api/auth/register", json={
            "username": "user2", "email": "dup@example.com", "password": "Test1234!"
        })
        assert resp.status_code == 400
        assert "已被注册" in resp.json()["detail"]

    def test_register_weak_password(self):
        # 太短会被 pydantic min_length=8 拦截(422); 长度够但不满足强度返回 400
        resp = client.post("/api/auth/register", json={
            "username": "weakpw", "email": "weak2@example.com", "password": "short5"
        })
        assert resp.status_code == 422  # pydantic 长度校验

        # 长度够但无大写
        resp = client.post("/api/auth/register", json={
            "username": "weakpw2", "email": "weak3@example.com", "password": "alllowercase123"
        })
        assert resp.status_code == 400  # 强度校验

    def test_register_invalid_username(self):
        resp = client.post("/api/auth/register", json={
            "username": "bad user!", "email": "bad@example.com", "password": "Test1234!"
        })
        assert resp.status_code == 422  # pydantic 校验


class TestLogin:
    def test_login_success(self):
        client.post("/api/auth/register", json={
            "username": "loginuser", "email": "login@example.com", "password": "Test1234!"
        })
        resp = client.post("/api/auth/login", json={
            "email": "login@example.com", "password": "Test1234!"
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data

    def test_login_wrong_password(self):
        client.post("/api/auth/register", json={
            "username": "wrongpw", "email": "wrongpw@example.com", "password": "Test1234!"
        })
        resp = client.post("/api/auth/login", json={
            "email": "wrongpw@example.com", "password": "WrongPass1!"
        })
        assert resp.status_code == 401

    def test_login_nonexistent(self):
        resp = client.post("/api/auth/login", json={
            "email": "nobody@example.com", "password": "Test1234!"
        })
        assert resp.status_code == 401


class TestAuthMiddleware:
    def test_me_endpoint(self):
        # 注册并取 token
        reg = client.post("/api/auth/register", json={
            "username": "meuser", "email": "me@example.com", "password": "Test1234!"
        })
        token = reg.json()["access_token"]

        # 用 token 访问 /me
        resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "meuser"

    def test_me_unauthorized(self):
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401

    def test_me_bad_token(self):
        resp = client.get("/api/auth/me", headers={"Authorization": "Bearer badtoken"})
        assert resp.status_code == 401

    def test_rotate_api_key(self):
        reg = client.post("/api/auth/register", json={
            "username": "rotateuser", "email": "rotate@example.com", "password": "Test1234!"
        })
        token = reg.json()["access_token"]
        old_key = reg.json()["api_key"]

        resp = client.post("/api/auth/rotate-api-key", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        new_key = resp.json()["api_key"]
        assert new_key != old_key
        assert new_key.startswith("sk-gw-")