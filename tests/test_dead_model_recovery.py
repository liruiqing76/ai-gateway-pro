# ============================================================
# 死模型自动复检(平反)测试
# ============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离 DB 的测试客户端(管理面板 JWT 认证)"""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("GW_DB_PATH", str(db_path))

    from sqlalchemy import create_engine
    from backend.models import Base, get_session, User, Provider, Router, DeadModel
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
        disabled_models=["deepseek-ai/ds-v4-flash"], free_only=1, enabled=1,
    )
    s.add(p)
    s.commit()
    # 预置一个含 NVIDIA 兄弟模型的路由组
    s.add(Router(user_id=u.id, name="high_quality", strategy="quality",
                 models=["NVIDIA-moonshotai/kimi-k2.6", "NVIDIA-z-ai/glm-5.2"]))
    s.commit()
    # 预置 3 条拉黑: 1 个待复活 / 1 个仍死 / 1 个用户手动 disabled 的
    s.add_all([
        DeadModel(user_id=u.id, provider_name="NVIDIA", model="moonshotai/kimi-k2.6"),
        DeadModel(user_id=u.id, provider_name="NVIDIA", model="somewhere/true-dead"),
        DeadModel(user_id=u.id, provider_name="NVIDIA", model="deepseek-ai/ds-v4-flash"),
    ])
    s.commit()
    token = create_access_token(u.id)
    s.close()

    from backend.main import app
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


class TestProbeClassification:
    """探测状态分类(httpx.MockTransport 模拟上游)"""

    def _run(self, monkeypatch, handler):
        import httpx
        import backend.core.dead_model_recovery as dmr
        transport = httpx.MockTransport(handler)
        orig_client = httpx.Client

        def fake_client(**kw):
            kw.pop("transport", None)
            return orig_client(transport=transport, **kw)
        monkeypatch.setattr(dmr.httpx, "Client", fake_client)
        return dmr.probe_model_online("https://example.com/v1", "k", "m", timeout=5)

    def test_online(self, monkeypatch):
        assert self._run(monkeypatch, lambda r: httpx.Response(
            200, json={"choices": []})) == "online"

    def test_dead_410(self, monkeypatch):
        assert self._run(monkeypatch, lambda r: httpx.Response(410)) == "dead"

    def test_dead_404_unknown(self, monkeypatch):
        assert self._run(monkeypatch, lambda r: httpx.Response(
            404, text='{"detail": "model m not found"}')) == "dead"

    def test_no_access_function_for_account(self, monkeypatch):
        """NVIDIA 权限类 404: 不算模型死, 单独归类等待官网开通"""
        assert self._run(monkeypatch, lambda r: httpx.Response(
            404, text="Function 'x': Not found for account 'acc'")) == "no_access"

    def test_uncertain_429_timeout(self, monkeypatch):
        import httpx
        import backend.core.dead_model_recovery as dmr
        assert self._run(monkeypatch, lambda r: httpx.Response(429)) == "uncertain"

        def hang(r):
            raise httpx.ConnectTimeout("slow")
        assert self._run(monkeypatch, hang) == "uncertain"

    def test_auth_401(self, monkeypatch):
        assert self._run(monkeypatch, lambda r: httpx.Response(401)) == "auth"


class TestRecoverDeadModels:
    """自动复检主流程: 200 的复活, 死的/停用的/禁用的保持"""

    def _seed_bans(self, client):
        from backend.models import DeadModel, User, get_session
        s = get_session()
        try:
            u = s.query(User).filter_by(email="test@x.com").first()
        finally:
            s.close()
        return u.id

    def test_sweep_revives_online_keeps_dead(self, client, monkeypatch):
        import backend.core.dead_model_recovery as dmr
        from backend.models import User, Provider, Router, DeadModel, get_session
        import json as _json

        s = get_session()
        try:
            u = s.query(User).filter_by(email="test@x.com").first()
            uid = u.id
            pid = s.query(Provider).filter_by(user_id=uid).first().id
        finally:
            s.close()

        def fake_probe(base_url, key, model, timeout=None):
            if model == "moonshotai/kimi-k2.6":
                return "online"
            if model == "deepseek-ai/ds-v4-flash":
                return "online"  # disabled: 解封但不补回渠道/路由组
            return "dead"        # somewhere/true-dead 仍死
        monkeypatch.setattr(dmr, "probe_model_online", fake_probe)

        stats = dmr.recover_dead_models(uid)
        assert "NVIDIA-moonshotai/kimi-k2.6" in stats["revived"]
        assert stats["dead"] == 1

        s = get_session()
        try:
            u = s.query(User).filter_by(email="test@x.com").first()
            p = s.query(Provider).filter_by(user_id=u.id).first()
            # kimi-k2.6 复活: 拉黑记录删除, 仍在渠道模型表
            assert s.query(DeadModel).filter_by(
                user_id=u.id, provider_name="NVIDIA",
                model="moonshotai/kimi-k2.6").first() is None
            # somewhere/true-dead 保持拉黑
            assert s.query(DeadModel).filter_by(
                user_id=u.id, provider_name="NVIDIA",
                model="somewhere/true-dead").first() is not None
            # disabled 模型解封但不进渠道表
            assert s.query(DeadModel).filter_by(
                user_id=u.id, provider_name="NVIDIA",
                model="deepseek-ai/ds-v4-flash").first() is None
            assert "deepseek-ai/ds-v4-flash" not in (p.models or [])
            # 路由组: kimi-k2.6 回到兄弟模型所在组, 组内其他成员不受影响
            g = s.query(Router).filter_by(user_id=u.id, name="high_quality").first()
            ms = g.models
            assert "NVIDIA-moonshotai/kimi-k2.6" in ms
            assert "NVIDIA-z-ai/glm-5.2" in ms
            assert "NVIDIA-deepseek-ai/ds-v4-flash" not in ms
        finally:
            s.close()

    def test_sweep_skips_disabled_channel_and_purges_orphan(self, client, monkeypatch):
        import backend.core.dead_model_recovery as dmr
        from backend.models import User, Provider, DeadModel, get_session
        s = get_session()
        try:
            u = s.query(User).filter_by(email="test@x.com").first()
            uid = u.id
            # 停用一个渠道 + 造一个孤儿拉黑记录(渠道已删)
            prov = s.query(Provider).filter_by(user_id=uid).first()
            prov.enabled = False
            s.add(DeadModel(user_id=uid, provider_name="已删除渠道", model="x/y"))
            s.commit()
        finally:
            s.close()
        monkeypatch.setattr(dmr, "probe_model_online",
                            lambda *a, **k: "online")  # 不应被调用

        stats = dmr.recover_dead_models(uid)
        assert stats["skipped_disabled"] == 3  # 停用渠道的 3 条不探测
        assert stats["orphan_removed"] == 1
        assert not stats["revived"]
        s = get_session()
        try:
            assert s.query(DeadModel).filter_by(
                user_id=uid, provider_name="已删除渠道").first() is None
        finally:
            s.close()

    def test_api_list_and_manual_recover(self, client):
        from backend.models import User, Provider, Router, DeadModel, get_session
        s = get_session()
        try:
            u = s.query(User).filter_by(email="test@x.com").first()
            uid = u.id
        finally:
            s.close()
        # 列表接口
        r = client.get("/api/dead-models")
        assert r.status_code == 200
        names = {i["model"] for i in r.json()["items"]}
        assert "moonshotai/kimi-k2.6" in names and "somewhere/true-dead" in names

        # 手动平反一个"仍死"的模型(用户意图优先, 不探测)
        r = client.post("/api/dead-models/recover",
                        json={"provider_name": "NVIDIA", "model": "somewhere/true-dead"})
        assert r.status_code == 200
        s = get_session()
        try:
            u = s.query(User).filter_by(email="test@x.com").first()
            assert s.query(DeadModel).filter_by(
                user_id=u.id, model="somewhere/true-dead").first() is None
            p = s.query(Provider).filter_by(user_id=u.id).first()
            assert "somewhere/true-dead" in (p.models or [])
            g = s.query(Router).filter_by(user_id=u.id, name="high_quality").first()
            assert "NVIDIA-somewhere/true-dead" in (g.models or [])
        finally:
            s.close()
        # 重复平反 → 404(没有记录)
        r = client.post("/api/dead-models/recover",
                        json={"provider_name": "NVIDIA", "model": "somewhere/true-dead"})
        assert r.status_code == 404

    def test_api_sweep_endpoint(self, client, monkeypatch):
        """POST /api/dead-models/sweep 走真实复检主流程"""
        import backend.core.dead_model_recovery as dmr
        monkeypatch.setattr(dmr, "probe_model_online",
                            lambda *a, **k: "dead")
        r = client.post("/api/dead-models/sweep")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["probed"] >= 3 and not body["revived"]
