# ============================================================
# 死模型拉黑(dead_models)测试
# ============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离 DB 的测试客户端(管理面板 JWT 认证)"""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("GW_DB_PATH", str(db_path))

    from sqlalchemy import create_engine
    from backend.models import Base, get_session, User, Provider
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr("backend.models._get_engine", lambda: engine)

    s = get_session()
    from backend.auth.security import encrypt_secret, create_access_token
    import bcrypt
    pw_hash = bcrypt.hashpw(b"Test1234!", bcrypt.gensalt()).decode()
    u = User(username="test", email="test@x.com", password_hash=pw_hash,
             tier="free", api_key="sk-test")
    s.add(u)
    s.commit()
    p = Provider(
        user_id=u.id, name="NVIDIA",
        base_url="https://example.com/v1",
        api_key_encrypted=encrypt_secret("fake-key"),
        models=["moonshotai/kimi-k2.6", "z-ai/glm-5.2"],
        disabled_models="[]", free_only=1, is_template=0, enabled=1,
    )
    s.add(p)
    s.commit()
    token = create_access_token(u.id)
    s.close()

    from backend.main import app
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


class TestDeadModelBan:
    def test_ban_and_check(self, client):
        """拉黑登记 + 查询幂等"""
        from backend.core.dead_models import ban_model, is_banned, banned_keys
        from backend.models import User, get_session
        s = get_session()
        try:
            u = s.query(User).filter_by(email="test@x.com").first()
            uid = u.id
        finally:
            s.close()
        assert not is_banned(uid, "NVIDIA", "moonshotai/kimi-k2.6")
        ban_model(uid, "NVIDIA", "moonshotai/kimi-k2.6")
        assert is_banned(uid, "NVIDIA", "moonshotai/kimi-k2.6")
        ban_model(uid, "NVIDIA", "moonshotai/kimi-k2.6")  # 幂等
        assert banned_keys(uid) == {"NVIDIA||moonshotai/kimi-k2.6"}
        # 其他用户/模型不受影响
        assert not is_banned(uid + 999, "NVIDIA", "moonshotai/kimi-k2.6")

    def test_build_groups_skip_banned(self, client):
        """自动分类/渠道重建: 拉黑模型不再生成进任何组"""
        from backend.core.dead_models import ban_model
        from backend.core.router_group import build_router_groups
        from backend.models import User, Provider, get_session
        s = get_session()
        try:
            u = s.query(User).filter_by(email="test@x.com").first()
            provs = s.query(Provider).filter_by(user_id=u.id).all()
            before = build_router_groups(provs)
            assert any("moonshotai/kimi-k2.6" in m for g in before.values() for m in g)
            ban_model(u.id, "NVIDIA", "moonshotai/kimi-k2.6")
            after = build_router_groups(provs, user_id=u.id)
            flat = [m for g in after.values() for m in g]
            assert not any("moonshotai/kimi-k2.6" in m for m in flat)
            assert any("z-ai/glm-5.2" in m for m in flat), "未拉黑模型不应被误删"
        finally:
            s.close()

    def test_refresh_models_filters_banned(self, client, monkeypatch):
        """刷新模型: 拉黑模型不落库(respawn 防护)"""
        import json
        import backend.api.providers as pm
        from backend.core.dead_models import ban_model
        from backend.models import User, Provider, get_session

        s = get_session()
        try:
            u = s.query(User).filter_by(email="test@x.com").first()
            pid = s.query(Provider).filter_by(user_id=u.id).first().id
            ban_model(u.id, "NVIDIA", "moonshotai/kimi-k2.6")
        finally:
            s.close()

        # 上游返回含拉黑模型的完整目录 → 落库时应被剔除
        async def fake_fetch(*a, **k):
            return ["moonshotai/kimi-k2.6", "z-ai/glm-5.2", "new-model-x"]
        monkeypatch.setattr(pm, "fetch_upstream_models", fake_fetch)
        r = client.post(f"/api/providers/{pid}/refresh-models")
        assert r.status_code == 200
        s = get_session()
        try:
            models = s.get(Provider, pid).models or []
        finally:
            s.close()
        assert "moonshotai/kimi-k2.6" not in models
        assert "z-ai/glm-5.2" in models and "new-model-x" in models

    def test_handle_upstream_404_removes_and_bans(self, client):
        """连续 3×404 → 从用户全部路由组移除 + 写入永久拉黑(幂等)"""
        from backend.api.proxy import _handle_upstream_404
        from backend.models import User, Router, get_session
        from backend.core.dead_models import is_banned

        # 用独立模型 key, 避免与其它测试共享全局熔断计数
        mdl = "fake/dead-test-model"
        entry = f"NVIDIA-{mdl}"
        s = get_session()
        try:
            u = s.query(User).filter_by(email="test@x.com").first()
            s.add(Router(user_id=u.id, name="t404", models=[entry]))
            s.add(Router(user_id=u.id, name="high_quality", models=[entry, "NVIDIA-z-ai/glm-5.2"]))
            s.commit()
            uid = u.id
        finally:
            s.close()

        class FakeProvider:
            name = "NVIDIA"

        for i in range(3):
            _handle_upstream_404(type("U", (), {"id": uid})(), FakeProvider(), mdl, "upstream 404")
            if i == 0:
                # 前 2 次只计数, 不应改动
                s = get_session()
                try:
                    assert entry in s.query(Router).filter_by(user_id=uid, name="t404").first().models
                finally:
                    s.close()

        s = get_session()
        try:
            t404 = s.query(Router).filter_by(user_id=uid, name="t404").first()
            hq = s.query(Router).filter_by(user_id=uid, name="high_quality").first()
            assert entry not in t404.models, "t404 组未移除死模型"
            assert entry not in hq.models, "high_quality 组未移除死模型"
            assert "NVIDIA-z-ai/glm-5.2" in hq.models, "误删了活模型"
        finally:
            s.close()
        assert is_banned(uid, "NVIDIA", mdl), "未写入永久拉黑"
        # 幂等: 再触发不报错, 组保持已移除
        _handle_upstream_404(type("U", (), {"id": uid})(), FakeProvider(), mdl, "upstream 404")