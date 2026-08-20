# ============================================================
# 路由组可用性检测测试
# mock httpx 以避免真实网络请求
# ============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离 DB + 预置用户/渠道/路由组"""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("GW_DB_PATH", str(db_path))

    from sqlalchemy import create_engine
    from backend.models import Base, get_session, User, Provider, Router
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr("backend.models._get_engine", lambda: engine)

    s = get_session()
    import bcrypt
    from backend.auth.security import encrypt_secret, create_access_token
    pw_hash = bcrypt.hashpw(b"Test1234!", bcrypt.gensalt()).decode()
    u = User(username="checker", email="checker@x.com", password_hash=pw_hash, tier="free", api_key="sk-check")
    s.add(u)
    s.commit()

    p = Provider(
        user_id=u.id, name="GoodUpstream", base_url="https://good.example/v1",
        api_key_encrypted=encrypt_secret("key"),
        models=["model-a", "model-b"], disabled_models="[]", enabled=1,
    )
    s.add(p)
    s.add(Router(user_id=u.id, name="grp", models=["GoodUpstream-model-a", "GoodUpstream-model-b", "GhostModel-fake"], strategy="quality"))
    s.commit()

    token = create_access_token(u.id)
    s.close()

    from backend.main import app
    client = TestClient(app)
    yield client, token


class TestTestSingleModel:
    """单模型连通性检测"""

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient")
    async def test_returns_available_on_200(self, MockClient):
        from backend.api.router_check import test_single_model

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = ""
        MockClient.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)

        result = await test_single_model("https://x.example/v1", "key", "model-a")
        assert result.available is True
        assert result.latency_ms is not None

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient")
    async def test_returns_unavailable_on_500(self, MockClient):
        from backend.api.router_check import test_single_model

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "boom"
        MockClient.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)

        result = await test_single_model("https://x.example/v1", "key", "model-a")
        assert result.available is False
        assert "HTTP 500" in result.error

    @pytest.mark.asyncio
    @patch("httpx.AsyncClient")
    async def test_returns_unavailable_on_network_error(self, MockClient):
        from backend.api.router_check import test_single_model

        async def boom(*a, **k):
            raise Exception("connection refused")

        MockClient.return_value.__aenter__.return_value.post = AsyncMock(side_effect=boom)

        result = await test_single_model("https://x.example/v1", "key", "model-a")
        assert result.available is False
        assert "connection refused" in result.error


class TestCheckAvailabilityAPI:
    def test_no_routers_404(self, tmp_path, monkeypatch):
        db_path = tmp_path / "empty.db"
        monkeypatch.setenv("GW_DB_PATH", str(db_path))
        from sqlalchemy import create_engine
        from backend.models import Base, get_session, User
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        monkeypatch.setattr("backend.models._get_engine", lambda: engine)

        s = get_session()
        import bcrypt
        from backend.auth.security import create_access_token
        pw = bcrypt.hashpw(b"Test1234!", bcrypt.gensalt()).decode()
        u = User(username="empty", email="empty@x.com", password_hash=pw, tier="free", api_key="sk-e")
        s.add(u)
        s.commit()
        token = create_access_token(u.id)
        s.close()

        from backend.main import app
        c = TestClient(app)
        resp = c.post("/api/routers/check-availability", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 404

    @patch("backend.api.router_check.test_single_model")
    def test_checks_all_groups_with_provider_resolution(self, mock_test, env):
        """验证: 渠道前缀解析 + 未知渠道标记 + 结果聚合"""
        from backend.api.router_check import CheckResult

        side_effects = [
            CheckResult(model="GoodUpstream-model-a", provider="GoodUpstream", available=True, latency_ms=12.3),
            CheckResult(model="GoodUpstream-model-b", provider="GoodUpstream", available=False, error="HTTP 500"),
        ]
        mock_test.side_effect = side_effects

        c, token = env
        resp = c.post("/api/routers/check-availability", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True

        grp = body["groups"]["grp"]
        assert grp["total"] == 3
        assert grp["available"] == 1

        # 第三个模型渠道不存在 → 无法解析
        ghost = [r for r in grp["results"] if r["model"] == "GhostModel-fake"][0]
        assert ghost["available"] is False
        assert "渠道不存在" in ghost["error"]

        # 前两个模型正确调用了 test_single_model 且渠道解析成功
        good_a = [r for r in grp["results"] if r["model"] == "GoodUpstream-model-a"][0]
        assert good_a["available"] is True
        assert good_a["provider"] == "GoodUpstream"

    @patch("backend.api.router_check.test_single_model")
    def test_requires_auth_first(self, mock_test, env):
        c, _ = env
        resp = c.post("/api/routers/check-availability")
        assert resp.status_code == 401