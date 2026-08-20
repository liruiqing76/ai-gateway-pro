# ============================================================
# 渠道管理路由测试
# ============================================================
import sys, os, tempfile, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi import HTTPException
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


class TestFetchModels:
    """自动拉取上游模型 / 刷新 / 连通性测试"""

    def test_fetch_upstream_models_success(self, auth_headers, monkeypatch):
        import backend.api.providers as pm
        async def fake_get(base_url, api_key):
            return ["b-model", "a-model"]
        monkeypatch.setattr(pm, "fetch_upstream_models", fake_get)

        resp = client.post("/api/providers/fetch-models", json={
            "base_url": "https://example.com/v1",
            "api_key": "key",
        }, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["models"] == ["b-model", "a-model"]
        assert resp.json()["count"] == 2

    def test_fetch_upstream_failure_returns_502(self, auth_headers, monkeypatch):
        import backend.api.providers as pm
        from fastapi import HTTPException

        async def fail_get(base_url, api_key):
            raise HTTPException(status_code=502, detail="拉取模型失败: HTTP 401")
        monkeypatch.setattr(pm, "fetch_upstream_models", fail_get)

        resp = client.post("/api/providers/fetch-models", json={
            "base_url": "https://example.com/v1",
            "api_key": "bad",
        }, headers=auth_headers)
        assert resp.status_code == 502

    def test_create_provider_auto_fetch_models(self, auth_headers, monkeypatch):
        import backend.api.providers as pm
        async def fake_get(base_url, api_key):
            return ["fetched-model-1"]
        monkeypatch.setattr(pm, "fetch_upstream_models", fake_get)

        resp = client.post("/api/providers", json={
            "name": "AutoFetch", "base_url": "https://auto.example/v1",
            "api_key": "key", "auto_fetch": True, "models": [],
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["models_count"] == 1
        assert data["models_fetched"] is True
        assert "已自动获取" in data["msg"]

    def test_create_provider_auto_fetch_failure_does_not_block(self, auth_headers, monkeypatch):
        """拉取失败不阻塞渠道创建"""
        import backend.api.providers as pm
        from fastapi import HTTPException

        async def fail_get(base_url, api_key):
            raise HTTPException(status_code=502, detail="拉取模型失败")
        monkeypatch.setattr(pm, "fetch_upstream_models", fail_get)

        resp = client.post("/api/providers", json={
            "name": "NoFetch", "base_url": "https://nofetch.example/v1",
            "api_key": "key", "auto_fetch": True, "models": [],
        }, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["models_count"] == 0

    def test_create_provider_auto_fetch_generates_routers(self, auth_headers, monkeypatch):
        """创建渠道后自动重建默认路由组(含中文组名 quick)"""
        import backend.api.providers as pm
        async def fake_get(base_url, api_key):
            return ["qwen-8b-fp8", "glm-5.2"]
        monkeypatch.setattr(pm, "fetch_upstream_models", fake_get)

        resp = client.post("/api/providers", json={
            "name": "RouterGen", "base_url": "https://gen.example/v1",
            "api_key": "key", "models": [],
        }, headers=auth_headers)
        assert resp.status_code == 200

        # 路由组已按三分类生成,请求 quick 组可达
        r = client.get("/api/routers", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()["data"]
        assert isinstance(data, dict)
        assert "quick" in data

    def test_refresh_models_endpoint(self, auth_headers, monkeypatch):
        import backend.api.providers as pm
        from fastapi import HTTPException

        # 先建一个渠道
        resp = client.post("/api/providers", json={
            "name": "Refresher", "base_url": "https://refresher.example/v1",
            "api_key": "key", "models": ["old-model"], "auto_fetch": False,
        }, headers=auth_headers)
        pid = resp.json()["id"]

        async def fake_get(base_url, api_key):
            return ["new-model-1", "new-model-2"]
        monkeypatch.setattr(pm, "fetch_upstream_models", fake_get)

        r = client.post(f"/api/providers/{pid}/refresh-models", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["models_count"] == 2

        # 库中模型已更新
        r = client.get(f"/api/providers/{pid}", headers=auth_headers)
        assert r.json()["models"] == ["new-model-1", "new-model-2"]

    def test_refresh_models_missing_provider_404(self, auth_headers, monkeypatch):
        import backend.api.providers as pm
        async def fake_get(base_url, api_key):
            return []
        monkeypatch.setattr(pm, "fetch_upstream_models", fake_get)

        r = client.post("/api/providers/99999/refresh-models", headers=auth_headers)
        assert r.status_code == 404

    def test_update_provider_changes_key_and_models(self, auth_headers):
        resp = client.post("/api/providers", json={
            "name": "Updater", "base_url": "https://updater.example/v1",
            "api_key": "old-key", "models": ["m1"], "auto_fetch": False,
        }, headers=auth_headers)
        pid = resp.json()["id"]

        r = client.put(f"/api/providers/{pid}", json={
            "name": "Updated", "api_key": "new-key", "models": ["m2", "m3"],
        }, headers=auth_headers)
        assert r.status_code == 200

        r = client.get(f"/api/providers/{pid}", headers=auth_headers)
        data = r.json()
        assert data["name"] == "Updated"
        assert data["api_key"] == "new-key"
        assert data["models"] == ["m2", "m3"]

    def test_update_provider_404(self, auth_headers):
        r = client.put("/api/providers/99999", json={"name": "X"}, headers=auth_headers)
        assert r.status_code == 404

    def test_test_provider_success(self, monkeypatch):
        import httpx
        import backend.api.providers as pm

        class FakeResp:
            status_code = 200
            text = ""

        async def fake_post(self, url, json, headers):
            return FakeResp()

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        r = client.post("/api/providers/test", json={
            "base_url": "https://test.example/v1", "api_key": "key",
        })
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_test_provider_http_error(self, monkeypatch):
        import httpx
        import backend.api.providers as pm

        class FakeResp:
            status_code = 401
            text = "unauthorized"

        async def fake_post(self, url, json, headers):
            return FakeResp()

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        r = client.post("/api/providers/test", json={
            "base_url": "https://test.example/v1", "api_key": "bad",
        })
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert "401" in r.json()["msg"]

    def test_test_provider_connection_error(self, monkeypatch):
        import httpx
        import backend.api.providers as pm

        async def fake_post(self, url, json, headers):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        r = client.post("/api/providers/test", json={
            "base_url": "https://test.example/v1", "api_key": "key",
        })
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert "连接失败" in r.json()["msg"]


class TestFetchUpstreamErrors:
    """上游模型拉取的真实 HTTP 分支: 非200/格式异常/空列表/网络错误"""

    def _run(self, coro):
        """在新事件循环中运行协程(兼容 py3.14 主线程无事件循环)"""
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _patch_httpx_get(self, monkeypatch, responses):
        """responses: list of (status, json) 依次返回"""
        import httpx
        import backend.api.providers as pm

        calls = {"n": 0}

        class FakeResp:
            def __init__(self, status, data):
                self.status_code = status
                self._json = data
                self.text = str(data)

            def json(self):
                return self._json

        async def fake_get(self, url, headers):
            idx = calls["n"]
            calls["n"] += 1
            return FakeResp(*responses[idx % len(responses)])

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    def test_fetch_http_401_all_fail(self, monkeypatch, auth_headers):
        """两个候选 URL 都非 200 → 502 拉取失败"""
        import backend.api.providers as pm
        self._patch_httpx_get(monkeypatch, [(401, {"error": "no"}), (401, {"error": "no"})])
        with pytest.raises(HTTPException) as ei:
            self._run(pm.fetch_upstream_models("https://x.com", "bad-key"))
        assert "HTTP 401" in str(ei.value)

    def test_fetch_non_list_response(self, monkeypatch, auth_headers):
        """返回了 JSON 但不是 list → 尝试下一个 URL"""
        import backend.api.providers as pm
        # base_url 不带 /v1 → 尝试两个 URL: /models 与 /v1/models
        self._patch_httpx_get(monkeypatch, [
            (200, {"data": "not-a-list"}),
            (200, {"data": [{"id": "ok-model"}, {"id": "beta-model"}]}),
        ])
        result = self._run(pm.fetch_upstream_models("https://x.com", "key"))
        assert result == ["beta-model", "ok-model"]  # 排序后的 ID 列表

    def test_fetch_empty_list_fails(self, monkeypatch, auth_headers):
        """两个 URL 都返回空列表 → 502"""
        import backend.api.providers as pm
        self._patch_httpx_get(monkeypatch, [(200, {"data": []}), (200, [])])
        with pytest.raises(HTTPException) as ei:
            self._run(pm.fetch_upstream_models("https://x.com/v1", "key"))
        assert "空模型列表" in str(ei.value)

    def test_fetch_network_error_fails(self, monkeypatch, auth_headers):
        """网络异常 → 502"""
        import httpx
        import backend.api.providers as pm

        async def fake_get(self, url, headers):
            raise httpx.ConnectError("timeout")

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        with pytest.raises(HTTPException) as ei:
            self._run(pm.fetch_upstream_models("https://x.com/v1", "key"))
        assert "timeout" in str(ei.value)

    def test_create_provider_fetch_error_not_blocking(self, auth_headers, monkeypatch):
        """创建渠道时自动拉取失败 → 渠道仍创建成功(空模型列表)"""
        import backend.api.providers as pm

        async def fail_get(base_url, api_key):
            raise HTTPException(status_code=502, detail="拉取模型失败")
        monkeypatch.setattr(pm, "fetch_upstream_models", fail_get)

        resp = client.post("/api/providers", json={
            "name": "BlkFail", "base_url": "https://blk.example/v1",
            "api_key": "key", "auto_fetch": True, "models": [],
        }, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["msg"].startswith("渠道添加成功")

    def test_apply_template_fetch_error_not_blocking(self, auth_headers, monkeypatch):
        """模板模型为空时拉取失败 → 模板仍应用(空模型列表)"""
        import backend.api.providers as pm
        import json
        from pathlib import Path

        # 用临时模板文件: 该模板无预设模型 → 触发自动拉取
        tmp_tpl = Path(__file__).parent / "tmp_tpl.json"
        tmp_tpl.write_text(json.dumps([
            {"name": "EmptyModelTPL", "base_url": "https://empty.example/v1",
             "models": [], "free_only": True}
        ]), encoding="utf-8")
        monkeypatch.setattr(pm, "TEMPLATES_FILE", tmp_tpl)

        async def fail_get(base_url, api_key):
            raise HTTPException(status_code=502, detail="拉取模型失败: 超时")
        monkeypatch.setattr(pm, "fetch_upstream_models", fail_get)

        resp = client.post("/api/providers/apply-template", json={
            "template_name": "EmptyModelTPL",
            "api_key": "key",
        }, headers=auth_headers)
        try:
            tmp_tpl.unlink()
        except OSError:
            pass
        assert resp.status_code == 200
        assert resp.json()["models_count"] == 0