# ============================================================
# 渠道管理路由测试
# ============================================================
import sys, os, tempfile, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.models import create_tables

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    """每个测试前重建表,用临时文件隔离"""
    import backend.models
    import os, tempfile
    backend.models._engine = None
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_db.name}"
    eng = create_tables()
    yield
    eng.dispose()
    backend.models._engine = None
    if "DATABASE_URL" in os.environ:
        del os.environ["DATABASE_URL"]
    try:
        os.unlink(tmp_db.name)
    except:
        pass


@pytest.fixture
def auth_headers():
    """注册用户并返回鉴权 headers"""
    resp = client.post("/api/auth/register", json={
        "username": "provideruser",
        "email": "provider@example.com",
        "password": "Test1234!",
    })
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class TestProviderCRUD:
    def test_create_provider(self, auth_headers):
        resp = client.post("/api/providers", json={
            "name": "NVIDIA",
            "base_url": "https://integrate.api.nvidia.com/v1",
            "api_key": "nvapi-test-key-12345",
            "models": ["model-a", "model-b"],
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "NVIDIA"
        assert "id" in data

    def test_duplicate_provider_name(self, auth_headers):
        client.post("/api/providers", json={
            "name": "Dup", "base_url": "https://a.com", "api_key": "key1",
            "auto_fetch": False,
        }, headers=auth_headers)
        resp = client.post("/api/providers", json={
            "name": "Dup", "base_url": "https://b.com", "api_key": "key2",
            "auto_fetch": False,
        }, headers=auth_headers)
        assert resp.status_code == 400

    def test_list_providers(self, auth_headers):
        for i in range(2):
            client.post("/api/providers", json={
                "name": f"Provider{i}", "base_url": f"https://p{i}.com",
                "api_key": f"key{i}", "auto_fetch": False,
            }, headers=auth_headers)
        resp = client.get("/api/providers", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_get_provider_decrypts_key(self, auth_headers):
        resp = client.post("/api/providers", json={
            "name": "SecretStore", "base_url": "https://s.com",
            "api_key": "SDKJFLSJDFLSJF-super-secret",
            "auto_fetch": False,
        }, headers=auth_headers)
        pid = resp.json()["id"]
        resp = client.get(f"/api/providers/{pid}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["api_key"] == "SDKJFLSJDFLSJF-super-secret"

    def test_api_key_stored_encrypted(self, auth_headers):
        """验证数据库里存的不是明文"""
        import backend.models
        from backend.models import get_session, Provider
        client.post("/api/providers", json={
            "name": "Encrypted", "base_url": "https://e.com",
            "api_key": "PLAIN-TEXT-KEY", "auto_fetch": False,
        }, headers=auth_headers)
        session = get_session()
        try:
            p = session.query(Provider).filter_by(name="Encrypted").first()
            assert p.api_key_encrypted != "PLAIN-TEXT-KEY"
            assert "PLAIN-TEXT-KEY" not in p.api_key_encrypted
        finally:
            session.close()

    def test_update_provider(self, auth_headers):
        resp = client.post("/api/providers", json={
            "name": "Updatable", "base_url": "https://old.com",
            "api_key": "old_key", "models": ["m1"]
        }, headers=auth_headers)
        pid = resp.json()["id"]
        resp = client.put(f"/api/providers/{pid}", json={
            "base_url": "https://new.com",
            "enabled": False,
        }, headers=auth_headers)
        assert resp.status_code == 200
        resp = client.get(f"/api/providers/{pid}", headers=auth_headers)
        assert resp.json()["base_url"] == "https://new.com"
        assert resp.json()["enabled"] is False

    def test_delete_provider(self, auth_headers):
        resp = client.post("/api/providers", json={
            "name": "ToDelete", "base_url": "https://d.com",
            "api_key": "key", "auto_fetch": False,
        }, headers=auth_headers)
        pid = resp.json()["id"]
        resp = client.delete(f"/api/providers/{pid}", headers=auth_headers)
        assert resp.status_code == 200
        resp = client.get(f"/api/providers/{pid}", headers=auth_headers)
        assert resp.status_code == 404

    def test_cannot_access_other_users_provider(self, auth_headers):
        """多租户隔离:用户 A 建渠道,用户 B 不能访问"""
        resp = client.post("/api/providers", json={
            "name": "Isolation", "base_url": "https://iso.com",
            "api_key": "secret", "auto_fetch": False,
        }, headers=auth_headers)
        pid = resp.json()["id"]

        resp2 = client.post("/api/auth/register", json={
            "username": "userb", "email": "userb@example.com", "password": "Test1234!"
        })
        token_b = resp2.json()["access_token"]
        headers_b = {"Authorization": f"Bearer {token_b}"}

        resp3 = client.get(f"/api/providers/{pid}", headers=headers_b)
        assert resp3.status_code == 404


class TestTemplates:
    def test_list_templates(self, auth_headers):
        resp = client.get("/api/providers/templates/all", headers=auth_headers)
        assert resp.status_code == 200
        templates = resp.json()
        assert len(templates) >= 4
        assert any(t["name"] == "NVIDIA" for t in templates)
        assert any(t["name"] == "魔搭 (ModelScope)" for t in templates)

    def test_apply_template(self, auth_headers):
        resp = client.post("/api/providers/apply-template", json={
            "template_name": "NVIDIA",
            "api_key": "nvapi-test-key",
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "NVIDIA"
        assert data["models_count"] > 0
        assert "配置成功" in data["msg"]

    def test_apply_unknown_template(self, auth_headers):
        resp = client.post("/api/providers/apply-template", json={
            "template_name": "不存在",
            "api_key": "key",
        }, headers=auth_headers)
        assert resp.status_code == 404

    def test_apply_template_then_list(self, auth_headers):
        client.post("/api/providers/apply-template", json={
            "template_name": "魔搭 (ModelScope)",
            "api_key": "ms-test-key",
        }, headers=auth_headers)
        resp = client.get("/api/providers", headers=auth_headers)
        providers = resp.json()
        assert len(providers) == 1
        assert providers[0]["name"] == "魔搭 (ModelScope)"
        assert len(providers[0]["models"]) > 10