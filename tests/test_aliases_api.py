# ============================================================
# 模型别名 API 测试
# ============================================================
import sys, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离 DB 的测试客户端"""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("GW_DB_PATH", str(db_path))

    from sqlalchemy import create_engine
    from backend.models import Base, get_session, User, Provider
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr("backend.models._get_engine", lambda: engine)

    s = get_session()
    from backend.auth.security import encrypt_secret
    import bcrypt
    pw_hash = bcrypt.hashpw(b"Test1234!", bcrypt.gensalt()).decode()
    u = User(username="test", email="test@x.com", password_hash=pw_hash, tier="free", api_key="sk-test")
    s.add(u)
    s.commit()
    s.close()

    from backend.main import app
    return TestClient(app), "sk-test"


class TestAliasesCRUD:
    def test_list_empty(self, client):
        c, api_key = client
        resp = c.get("/api/aliases", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_create_alias(self, client):
        c, api_key = client
        resp = c.post("/api/aliases", json={
            "alias": "gpt-4",
            "target": "high_quality",
            "description": "GPT-4 映射到高质量组",
        }, headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["alias"] == "gpt-4"
        assert data["target"] == "high_quality"
        assert data["enabled"] is True

    def test_create_duplicate(self, client):
        c, api_key = client
        c.post("/api/aliases", json={"alias": "gpt-4", "target": "high_quality"}, headers={"Authorization": f"Bearer {api_key}"})
        resp = c.post("/api/aliases", json={"alias": "gpt-4", "target": "quick"}, headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 409

    def test_list_with_data(self, client):
        c, api_key = client
        c.post("/api/aliases", json={"alias": "gpt-4", "target": "high_quality"}, headers={"Authorization": f"Bearer {api_key}"})
        c.post("/api/aliases", json={"alias": "claude", "target": "NVIDIA-claude-3"}, headers={"Authorization": f"Bearer {api_key}"})
        resp = c.get("/api/aliases", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.json()["total"] == 2

    def test_update_alias(self, client):
        c, api_key = client
        r = c.post("/api/aliases", json={"alias": "gpt-4", "target": "high_quality"}, headers={"Authorization": f"Bearer {api_key}"})
        alias_id = r.json()["id"]
        resp = c.put(f"/api/aliases/{alias_id}", json={"target": "quick"}, headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200
        assert resp.json()["target"] == "quick"

    def test_toggle_enabled(self, client):
        c, api_key = client
        r = c.post("/api/aliases", json={"alias": "gpt-4", "target": "high_quality"}, headers={"Authorization": f"Bearer {api_key}"})
        alias_id = r.json()["id"]
        resp = c.put(f"/api/aliases/{alias_id}", json={"enabled": False}, headers={"Authorization": f"Bearer {api_key}"})
        assert resp.json()["enabled"] is False

    def test_delete_alias(self, client):
        c, api_key = client
        r = c.post("/api/aliases", json={"alias": "gpt-4", "target": "high_quality"}, headers={"Authorization": f"Bearer {api_key}"})
        alias_id = r.json()["id"]
        resp = c.delete(f"/api/aliases/{alias_id}", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # 验证已删除
        resp = c.get("/api/aliases", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.json()["total"] == 0

    def test_batch_create(self, client):
        c, api_key = client
        resp = c.post("/api/aliases/batch", json=[
            {"alias": "gpt-4", "target": "high_quality"},
            {"alias": "claude", "target": "NVIDIA-claude-3"},
            {"alias": "fast", "target": "quick"},
        ], headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200
        assert resp.json()["created"] == 3

    def test_auth_required(self, client):
        c, _ = client
        resp = c.get("/api/aliases")
        assert resp.status_code == 401

    def test_list_enabled_only(self, client):
        """enabled_only=true 只返回启用中的别名"""
        c, api_key = client
        r = c.post("/api/aliases", json={"alias": "gpt-4", "target": "high_quality"}, headers={"Authorization": f"Bearer {api_key}"})
        alias_id = r.json()["id"]
        c.put(f"/api/aliases/{alias_id}", json={"enabled": False}, headers={"Authorization": f"Bearer {api_key}"})
        c.post("/api/aliases", json={"alias": "claude", "target": "NVIDIA-claude-3"}, headers={"Authorization": f"Bearer {api_key}"})

        all_alias = c.get("/api/aliases", headers={"Authorization": f"Bearer {api_key}"})
        assert all_alias.json()["total"] == 2

        enabled = c.get("/api/aliases?enabled_only=true", headers={"Authorization": f"Bearer {api_key}"})
        assert enabled.json()["total"] == 1
        assert enabled.json()["data"][0]["alias"] == "claude"

    def test_update_missing_404(self, client):
        c, api_key = client
        resp = c.put("/api/aliases/999", json={"target": "quick"}, headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 404

    def test_delete_missing_404(self, client):
        c, api_key = client
        resp = c.delete("/api/aliases/999", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 404

    def test_batch_create_skips_duplicates(self, client):
        """批量创建遇到已存在别名 → 跳过并报告"""
        c, api_key = client
        c.post("/api/aliases", json={"alias": "gpt-4", "target": "high_quality"}, headers={"Authorization": f"Bearer {api_key}"})
        resp = c.post("/api/aliases/batch", json=[
            {"alias": "gpt-4", "target": "quick"},     # 已存在 → 跳过
            {"alias": "new-alias", "target": "NVIDIA-x"},
        ], headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200
        assert resp.json()["created"] == 1
        assert resp.json()["skipped"] == ["gpt-4"]

    def test_create_error_rolls_back(self, client, monkeypatch):
        """创建时数据库异常 → 回滚并返回 500"""
        c, api_key = client
        import backend.api.aliases as alias_mod

        class BoomSession:
            def add(self, *a, **k): pass
            def commit(self): raise RuntimeError("db broke")
            def rollback(self): pass
            def close(self): pass
            def refresh(self, *a, **k): pass
            def query(self, *a, **k):
                class Q:
                    def filter_by(self, **k): return self
                    def first(self): return None
                return Q()

        monkeypatch.setattr(alias_mod, "get_session", lambda: BoomSession())
        resp = c.post("/api/aliases", json={"alias": "gpt-4", "target": "high_quality"}, headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 500
        assert "db broke" in resp.json()["detail"]


class TestAliasResolution:
    """测试别名在代理转发中的解析"""

    def test_alias_resolves_in_pick_models(self, client):
        """别名应该被正确解析为目标模型/路由组"""
        from backend.core.proxy import pick_available_models
        from backend.models import Provider
        from backend.core.circuit import circuit_breaker
        from backend.core.scheduler import quality_tracker

        class FakeProvider:
            def __init__(self, id, name, models, disabled_models=None):
                self.id = id
                self.name = name
                self.models = models
                self.disabled_models = disabled_models or []

        providers = [FakeProvider(1, "NVIDIA", ["deepseek-v3", "glm-5.2"])]

        # 没有别名: gpt-4 不会匹配任何模型
        result = pick_available_models(providers, "gpt-4", {"high_quality": ["NVIDIA-deepseek-v3"]})
        assert len(result) == 0

        # 有别名: gpt-4 → high_quality
        result = pick_available_models(providers, "gpt-4", {"high_quality": ["NVIDIA-deepseek-v3"]}, user_aliases={"gpt-4": "high_quality"})
        assert len(result) == 1
        assert result[0][1] == "deepseek-v3"
