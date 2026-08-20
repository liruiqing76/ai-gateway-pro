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

    def test_register_duplicate_username(self):
        client.post("/api/auth/register", json={
            "username": "dupname", "email": "dupname1@example.com", "password": "Test1234!"
        })
        resp = client.post("/api/auth/register", json={
            "username": "dupname", "email": "other@example.com", "password": "Test1234!"
        })
        assert resp.status_code == 400
        assert "用户名已被占用" in resp.json()["detail"]

    def test_register_with_invite_code(self):
        # 邀请人注册
        inviter = client.post("/api/auth/register", json={
            "username": "inviter1", "email": "inviter1@example.com", "password": "Test1234!"
        }).json()
        token = inviter["access_token"]

        # 从库中读取邀请码
        from backend.models import get_session, User
        s = get_session()
        try:
            inv = s.query(User).filter_by(username="inviter1").first()
            invite_code = inv.invite_code
        finally:
            s.close()

        # 被邀请人用邀请码注册
        resp = client.post("/api/auth/register", json={
            "username": "invitee1", "email": "invitee1@example.com", "password": "Test1234!",
            "invite_code": invite_code,
        })
        assert resp.status_code == 200

        # 从库中验证 invited_by 已关联到邀请人
        s = get_session()
        try:
            inv = s.query(User).filter_by(username="inviter1").first()
            inv_u = s.query(User).filter_by(username="invitee1").first()
            assert inv_u.invited_by == inv.id
        finally:
            s.close()

        # 邀请人额度 +500
        me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
        assert me["quota_daily"] == 1500

    def test_refresh_user_missing(self):
        """refresh token 指向已删除用户 → 401"""
        from backend.auth.security import create_refresh_token
        token = create_refresh_token(999999)
        resp = client.post("/api/auth/refresh", json={"refresh_token": token})
        assert resp.status_code == 401

    def test_change_password_wrong_old(self):
        reg = client.post("/api/auth/register", json={
            "username": "pwuser", "email": "pw@example.com", "password": "Test1234!"
        })
        token = reg.json()["access_token"]
        resp = client.post("/api/auth/change-password", json={
            "old_password": "WrongOld1!",
            "new_password": "NewPass123!",
        }, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 400
        assert "旧密码错误" in resp.json()["detail"]

    def test_change_password_success(self):
        reg = client.post("/api/auth/register", json={
            "username": "pwuser2", "email": "pw2@example.com", "password": "Test1234!"
        })
        token = reg.json()["access_token"]
        resp = client.post("/api/auth/change-password", json={
            "old_password": "Test1234!",
            "new_password": "NewPass123!",
        }, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200

        # 旧密码登录失败, 新密码登录成功
        assert client.post("/api/auth/login", json={
            "email": "pw2@example.com", "password": "Test1234!"
        }).status_code == 401
        assert client.post("/api/auth/login", json={
            "email": "pw2@example.com", "password": "NewPass123!"
        }).status_code == 200

    def test_refresh_token_flow(self):
        """refresh token 换取新 access token"""
        reg = client.post("/api/auth/register", json={
            "username": "refreshme", "email": "refreshme@example.com", "password": "Test1234!"
        })
        refresh_token = reg.json()["refresh_token"]
        resp = client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data

    def test_invalid_refresh_token_401(self):
        resp = client.post("/api/auth/refresh", json={"refresh_token": "garbage"})
        assert resp.status_code == 401

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


class TestMiddlewareBranches:
    """鉴权中间件的边界分支: Token 类型/禁用用户/管理员/额度重置"""

    def test_refresh_token_rejected_for_me(self):
        """refresh token 不能当 access 用 → 401 Token 类型错误"""
        from backend.auth.security import create_refresh_token
        refresh = create_refresh_token(1)
        resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {refresh}"})
        assert resp.status_code == 401
        assert "Token 类型错误" in resp.json()["detail"]

    def test_disabled_user_rejected(self):
        """is_active=False 的用户即使 token 有效也拒绝"""
        from backend.auth.security import create_access_token
        from backend.models import get_session, User
        reg = client.post("/api/auth/register", json={
            "username": "disuser", "email": "dis@example.com", "password": "Test1234!"
        })
        uid = reg.json()["user"]["id"]
        s = get_session()
        try:
            u = s.query(User).filter_by(id=uid).first()
            u.is_active = False
            s.commit()
        finally:
            s.close()

        token = create_access_token(uid)
        resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    def test_admin_required_403(self):
        """非管理员访问需要管理员权限的依赖 → 403"""
        from backend.auth.middleware import require_admin
        from fastapi import HTTPException
        import pytest as _pytest
        with _pytest.raises(HTTPException) as exc:
            require_admin(type("FakeUser", (), {"role": "user"})())
        assert exc.value.status_code == 403

    def test_admin_passes(self):
        from backend.auth.middleware import require_admin
        admin = type("FakeUser", (), {"role": "admin"})()
        assert require_admin(admin) is admin

    def test_quota_reset_after_date(self):
        """quota_reset_at 已过 → 自动重置当日已用额度"""
        from datetime import datetime, timedelta
        from backend.auth.middleware import check_quota
        reg = client.post("/api/auth/register", json={
            "username": "quotares", "email": "quotares@example.com", "password": "Test1234!"
        })
        uid = reg.json()["user"]["id"]
        from backend.models import get_session, User
        import asyncio
        s = get_session()
        try:
            u = s.query(User).filter_by(id=uid).first()
            u.quota_used_today = 50
            u.quota_reset_at = datetime.utcnow() - timedelta(hours=1)  # 已过
            s.commit()
            # 保持 session 打开, 让 u 仍绑定在 session 上
            try:
                asyncio.get_event_loop().run_until_complete(check_quota(u))
            except RuntimeError:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(check_quota(u))
                loop.close()
            # 重新查询验证已重置
            s.expire_all()
            u2 = s.query(User).filter_by(id=uid).first()
            assert u2.quota_used_today == 0
            assert u2.quota_reset_at > datetime.utcnow()
        finally:
            s.close()