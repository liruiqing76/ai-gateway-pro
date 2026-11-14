# ============================================================
# 模型目录 API 测试: /api/models/catalog 聚合视图、/v1/models 裸名条目、
# 模型倍率 (PUT /api/models/rate) 与 UsageLog.cost 落库
# ============================================================
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离 DB + 清空全局内存态的测试客户端, 返回 (client, jwt_headers, uid)"""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("GW_DB_PATH", str(db_path))

    from sqlalchemy import create_engine
    from backend.models import Base, get_session, User
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr("backend.models._get_engine", lambda: engine)

    s = get_session()
    import bcrypt
    pw_hash = bcrypt.hashpw(b"Test1234!", bcrypt.gensalt()).decode()
    u = User(username="test", email="test@x.com", password_hash=pw_hash,
             tier="free", api_key="sk-test")
    s.add(u)
    s.commit()
    uid = u.id
    s.close()

    # 清空全局内存态, 避免用例间熔断/质量分串扰
    from backend.core.circuit import circuit_breaker
    from backend.core.scheduler import quality_tracker
    circuit_breaker.reset_all()
    quality_tracker.reset_all()

    from backend.main import app
    c = TestClient(app)

    from backend.auth.security import create_access_token
    headers = {"Authorization": f"Bearer {create_access_token(uid)}"}
    return c, headers, uid


def _add_provider(uid, name, models, free_only=False, disabled=None):
    from backend.models import get_session, Provider
    s = get_session()
    p = Provider(
        user_id=uid, name=name, base_url="http://upstream.test",
        api_key_encrypted="", models=models,
        disabled_models=disabled or [], free_only=free_only, enabled=True,
    )
    s.add(p)
    s.commit()
    s.close()
    return p


def _find(models, name):
    return next((m for m in models if m["model"] == name), None)


class TestModelCatalog:
    def test_catalog_empty(self, client):
        c, headers, _ = client
        resp = c.get("/api/models/catalog", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["models"] == []

    def test_bare_model_aggregation(self, client):
        """同名模型跨渠道聚合; 白嫖渠道标 free; 倍率缺省 1.0"""
        c, headers, uid = client
        _add_provider(uid, "trae", ["glm-5.2", "glm-4.7"])
        _add_provider(uid, "workbuddy", ["glm-5.2", "deepseek-v4.1-flash"])
        _add_provider(uid, "NVIDIA", ["deepseek-v3"])
        resp = c.get("/api/models/catalog", headers=headers)
        models = resp.json()["models"]
        glm = _find(models, "glm-5.2")
        assert glm is not None
        assert glm["total_count"] == 2
        assert glm["usable_count"] == 2
        providers = {e["provider"]: e for e in glm["providers"]}
        assert set(providers) == {"trae", "workbuddy"}
        # trae/workbuddy 均为白嫖渠道 → free
        assert providers["trae"]["free"] is True
        assert providers["trae"]["rate"] == 1.0
        # NVIDIA 非白嫖渠道 → free=False
        nvidia = _find(models, "deepseek-v3")
        assert nvidia["providers"][0]["free"] is False

    def test_disabled_and_dead_models_excluded(self, client):
        c, headers, uid = client
        _add_provider(uid, "trae", ["glm-5.2", "glm-4.7"], disabled=["glm-4.7"])
        _add_provider(uid, "NVIDIA", ["deepseek-v3", "dead-m"])
        from backend.core.dead_models import ban_model
        ban_model(uid, "NVIDIA", "dead-m")
        resp = c.get("/api/models/catalog", headers=headers)
        names = {m["model"] for m in resp.json()["models"]}
        assert names == {"glm-5.2", "deepseek-v3"}

    def test_rate_default_and_custom(self, client):
        c, headers, uid = client
        _add_provider(uid, "trae", ["glm-5.2"])
        resp = c.put("/api/models/rate", json={
            "provider": "trae", "model": "glm-5.2", "rate": 3.0,
        }, headers=headers)
        assert resp.status_code == 200
        catalog = c.get("/api/models/catalog", headers=headers).json()["models"]
        glm = _find(catalog, "glm-5.2")
        assert glm["providers"][0]["rate"] == 3.0
        assert glm["min_rate"] == 3.0
        # 非法倍率
        bad = c.put("/api/models/rate", json={
            "provider": "trae", "model": "glm-5.2", "rate": -1,
        }, headers=headers)
        assert bad.status_code == 400

    def test_circuit_open_reflects_in_catalog(self, client):
        c, headers, uid = client
        _add_provider(uid, "trae", ["glm-5.2", "glm-4.7"])
        from backend.core.circuit import circuit_breaker
        circuit_breaker.record_rate_limit("trae||glm-5.2")
        resp = c.get("/api/models/catalog", headers=headers)
        glm = _find(resp.json()["models"], "glm-5.2")
        entry = glm["providers"][0]
        assert entry["circuit_open"] is True
        assert entry["cooldown_seconds"] > 0
        assert entry["cooldown_until"]
        assert glm["usable_count"] == 0
        assert glm["total_count"] == 1

    def test_downed_model_reflects_in_catalog(self, client):
        c, headers, uid = client
        _add_provider(uid, "NVIDIA", ["deepseek-v3"])
        from backend.models import get_session, DownedModel
        s = get_session()
        s.add(DownedModel(
            user_id=uid, provider_name="NVIDIA", model="deepseek-v3",
            reason="rate_limit", recovers_at=datetime.now() + timedelta(hours=1),
        ))
        s.commit()
        s.close()
        resp = c.get("/api/models/catalog", headers=headers)
        dsv3 = _find(resp.json()["models"], "deepseek-v3")
        assert dsv3["providers"][0]["downed"]["reason"] == "rate_limit"
        assert dsv3["providers"][0]["downed"]["remaining_seconds"] > 0
        assert dsv3["usable_count"] == 0


class TestV1ModelsBareNames:
    def test_bare_names_listed(self, client):
        """/v1/models 暴露裸模型名(去重) + 渠道前缀条目"""
        c, headers, uid = client
        _add_provider(uid, "trae", ["glm-5.2"])
        _add_provider(uid, "workbuddy", ["glm-5.2"])
        resp = c.get("/v1/models", headers={"Authorization": "Bearer sk-test"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        ids = [m["id"] for m in data]
        # 裸名条目: 去重后一条, 带提供渠道列表
        bare = [m for m in data if m["id"] == "glm-5.2"]
        assert len(bare) == 1
        assert sorted(bare[0]["x-gw-providers"]) == ["trae", "workbuddy"]
        assert bare[0]["owned_by"] == "gateway"
        # 渠道前缀条目保留
        assert "trae-glm-5.2" in ids
        assert "workbuddy-glm-5.2" in ids

    def test_bare_names_exclude_disabled_and_dead(self, client):
        c, headers, uid = client
        _add_provider(uid, "trae", ["glm-5.2", "glm-4.7"], disabled=["glm-4.7"])
        _add_provider(uid, "NVIDIA", ["dead-m"])
        from backend.core.dead_models import ban_model
        ban_model(uid, "NVIDIA", "dead-m")
        data = c.get("/v1/models", headers={"Authorization": "Bearer sk-test"}).json()["data"]
        ids = {m["id"] for m in data}
        assert "glm-4.7" not in ids
        assert "dead-m" not in ids
        assert "NVIDIA-dead-m" not in ids
        assert "glm-5.2" in ids


class TestUsageCost:
    def test_usage_cost_uses_model_rate(self, client):
        """UsageLog.cost = 该渠道-模型倍率; 未设置默认 1.0"""
        c, headers, uid = client
        _add_provider(uid, "trae", ["glm-5.2"])
        c.put("/api/models/rate", json={
            "provider": "trae", "model": "glm-5.2", "rate": 2.5,
        }, headers=headers)

        from backend.models import get_session, User, UsageLog
        from backend.api.proxy import _log_usage_and_quota
        s = get_session()
        user = s.query(User).filter_by(id=uid).first()
        s.close()
        _log_usage_and_quota(user=user, provider_name="trae",
                             model_name="glm-5.2", pt=10, ct=20)
        _log_usage_and_quota(user=user, provider_name="trae",
                             model_name="unset-model", pt=1, ct=2)
        s = get_session()
        costs = {
            r.model_name: r.cost
            for r in s.query(UsageLog).filter_by(user_id=uid).all()
        }
        s.close()
        assert costs["glm-5.2"] == 2.5
        assert costs["unset-model"] == 1.0


class TestModelRateSync:
    """渠道登录时从上游目录自动同步倍率(source='auto'), 手填(source='manual')不覆盖"""

    def test_v1_models_x_gw_rate(self, client):
        """/v1/models 暴露 x-gw-rate: 渠道前缀条目带各自倍率, 裸名条目取最低"""
        c, headers, uid = client
        _add_provider(uid, "workbuddy", ["kimi-k3"])
        _add_provider(uid, "trae", ["kimi-k3", "glm-5.2"])
        from backend.models import get_session, ModelRate
        s = get_session()
        s.add(ModelRate(user_id=uid, provider_name="workbuddy",
                        model="kimi-k3", rate=2.0, source="auto"))
        s.add(ModelRate(user_id=uid, provider_name="trae",
                        model="kimi-k3", rate=0.0, source="manual"))
        s.commit()
        s.close()

        resp = c.get("/v1/models", headers={"Authorization": "Bearer sk-test"})
        assert resp.status_code == 200
        entries = {m["id"]: m for m in resp.json()["data"]}
        assert entries["workbuddy-kimi-k3"]["x-gw-rate"] == 2.0
        assert entries["trae-kimi-k3"]["x-gw-rate"] == 0.0
        assert entries["trae-glm-5.2"]["x-gw-rate"] == 1.0  # 未设置缺省 1.0
        assert entries["kimi-k3"]["x-gw-rate"] == 0.0       # 多渠道取最低

    def test_sync_model_rates_auto_insert_update_and_manual_guard(self, client):
        """auto: 缺则插/在则随上游变价; manual: 永不被覆盖"""
        from backend.core.channel_import import sync_model_rates
        from backend.models import get_session, ModelRate
        c, headers, uid = client
        s = get_session()
        s.add(ModelRate(user_id=uid, provider_name="workbuddy",
                        model="kimi-k3", rate=9.9, source="manual"))
        s.commit()
        s.close()

        sync_model_rates(uid, "workbuddy", [
            {"id": "kimi-k3", "name": "K", "rate": 2.0},
            {"id": "glm-5.1", "name": "G", "rate": 0.5},
        ])
        s = get_session()
        rows = {r.model: r for r in
                s.query(ModelRate).filter_by(user_id=uid, provider_name="workbuddy")}
        s.close()
        assert rows["kimi-k3"].rate == 9.9 and rows["kimi-k3"].source == "manual"
        assert rows["glm-5.1"].rate == 0.5 and rows["glm-5.1"].source == "auto"

        # 上游变价: auto 行跟随
        sync_model_rates(uid, "workbuddy", [{"id": "glm-5.1", "name": "G", "rate": 0.7}])
        s = get_session()
        row = s.query(ModelRate).filter_by(
            user_id=uid, provider_name="workbuddy", model="glm-5.1").first()
        s.close()
        assert row.rate == 0.7

    def test_upsert_channel_provider_syncs_rates(self, client):
        """渠道登录 upsert 全链路: 模型条目带的 rate 落到 model_rates"""
        from backend.api.channels import _upsert_channel_provider
        from backend.models import get_session, ModelRate
        c, headers, uid = client
        _upsert_channel_provider(uid, "workbuddy", "https://wb.test", "key", [
            {"id": "kimi-k3", "name": "Kimi", "rate": 1.5},
            {"id": "plain", "name": "P"},
        ])
        s = get_session()
        row = s.query(ModelRate).filter_by(
            user_id=uid, provider_name="workbuddy", model="kimi-k3").first()
        plain = s.query(ModelRate).filter_by(model="plain").first()
        s.close()
        assert row is not None and row.rate == 1.5 and row.source == "auto"
        assert plain is None  # 上游没给倍率的不编造

    def test_put_rate_marks_manual_and_catalog_source(self, client):
        """手填后 source=manual 且 catalog 带 rate_source; 上游同步不覆盖"""
        from backend.core.channel_import import sync_model_rates
        from backend.models import get_session, ModelRate
        c, headers, uid = client
        _add_provider(uid, "workbuddy", ["kimi-k3"])
        sync_model_rates(uid, "workbuddy", [{"id": "kimi-k3", "name": "K", "rate": 2.0}])

        resp = c.put("/api/models/rate", headers=headers, json={
            "provider": "workbuddy", "model": "kimi-k3", "rate": 3.0})
        assert resp.status_code == 200
        s = get_session()
        row = s.query(ModelRate).filter_by(
            user_id=uid, provider_name="workbuddy", model="kimi-k3").first()
        s.close()
        assert row.rate == 3.0 and row.source == "manual"

        sync_model_rates(uid, "workbuddy", [{"id": "kimi-k3", "name": "K", "rate": 2.0}])
        s = get_session()
        row = s.query(ModelRate).filter_by(
            user_id=uid, provider_name="workbuddy", model="kimi-k3").first()
        s.close()
        assert row.rate == 3.0  # 手填不被覆盖

        resp = c.get("/api/models/catalog", headers=headers)
        entry = _find(resp.json()["models"], "kimi-k3")["providers"][0]
        assert entry["rate"] == 3.0 and entry["rate_source"] == "manual"

    def test_refresh_models_channel_branch_syncs_rates(self, client, monkeypatch):
        """渠道 Provider 的「刷新模型」走渠道目录接口并同步倍率, 不走通用透传"""
        import httpx

        from backend.models import get_session, Provider, ModelRate
        c, headers, uid = client
        _add_provider(uid, "workbuddy", ["old-model"])
        s = get_session()
        p_id = s.query(Provider).filter_by(user_id=uid, name="workbuddy").first().id
        s.close()

        async def fake_fetch(client_, channel, package):
            assert channel == "workbuddy"
            return [
                {"id": "kimi-k3", "name": "Kimi-K3", "rate": 2.0},
                {"id": "plain", "name": "P"},
            ]

        monkeypatch.setattr(
            "backend.core.channel_import.fetch_channel_models", fake_fetch)
        resp = c.post(f"/api/providers/{p_id}/refresh-models", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True

        s = get_session()
        models = s.query(Provider).filter_by(id=p_id).first().models
        rows = {r.model: r for r in
                s.query(ModelRate).filter_by(user_id=uid, provider_name="workbuddy")}
        s.close()
        assert set(models) == {"kimi-k3", "plain"}
        assert rows["kimi-k3"].rate == 2.0 and rows["kimi-k3"].source == "auto"

    def test_refresh_models_channel_failure_keeps_models(self, client, monkeypatch):
        """渠道目录拉取失败(空清单)时保留现有模型不清空"""
        from backend.models import get_session, Provider
        c, headers, uid = client
        _add_provider(uid, "workbuddy", ["old-model"])
        s = get_session()
        p_id = s.query(Provider).filter_by(user_id=uid, name="workbuddy").first().id
        s.close()

        async def fake_fetch(client_, channel, package):
            return []

        monkeypatch.setattr(
            "backend.core.channel_import.fetch_channel_models", fake_fetch)
        resp = c.post(f"/api/providers/{p_id}/refresh-models", headers=headers)
        assert resp.status_code == 200
        s = get_session()
        models = s.query(Provider).filter_by(id=p_id).first().models
        s.close()
        assert models == ["old-model"]
