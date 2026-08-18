# ============================================================
# 路由组 CRUD API 测试
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

    # 建测试用户 + 渠道
    s = get_session()
    from backend.auth.security import encrypt_secret
    import bcrypt
    pw_hash = bcrypt.hashpw(b"Test1234!", bcrypt.gensalt()).decode()
    u = User(username="test", email="test@x.com", password_hash=pw_hash, tier="free", api_key="sk-test")
    s.add(u)
    s.commit()
    p = Provider(
        user_id=u.id, name="NVIDIA",
        base_url="https://example.com/v1",
        api_key_encrypted=encrypt_secret("fake-key"),
        models=["z-ai/glm-5.2", "qwen-vl-plus"],
        disabled_models="[]", free_only=1, is_template=0, enabled=1,
    )
    s.add(p)
    s.commit()
    s.close()

    from backend.main import app
    return TestClient(app)


def test_list_routers_empty(client):
    """初始状态无路由组"""
    r = client.get("/api/routers", headers={"Authorization": "Bearer sk-test"})
    assert r.status_code == 200
    data = r.json()
    assert "data" in data or isinstance(data, dict)
    # 空列表或空 dict
    routers = data.get("data", data) if isinstance(data, dict) else data
    assert routers == {} or routers == []


def test_create_router(client):
    """创建路由组"""
    body = {"name": "quick", "models": ["NVIDIA-z-ai/glm-5.2"]}
    r = client.post("/api/routers", json=body, headers={"Authorization": "Bearer sk-test"})
    assert r.status_code == 200
    # 验证能拉到
    r2 = client.get("/api/routers", headers={"Authorization": "Bearer sk-test"})
    data = r2.json()
    routers = data.get("data", data) if isinstance(data, dict) else data
    assert "quick" in routers
    assert "NVIDIA-z-ai/glm-5.2" in routers["quick"]


def test_update_router(client):
    """更新路由组成员"""
    client.post("/api/routers", json={"name": "vision", "models": ["NVIDIA-qwen-vl-plus"]},
                headers={"Authorization": "Bearer sk-test"})
    # 更新
    client.post("/api/routers", json={"name": "vision", "models": ["NVIDIA-z-ai/glm-5.2"]},
                headers={"Authorization": "Bearer sk-test"})
    r = client.get("/api/routers", headers={"Authorization": "Bearer sk-test"})
    data = r.json()
    routers = data.get("data", data) if isinstance(data, dict) else data
    assert "NVIDIA-z-ai/glm-5.2" in routers["vision"]


def test_delete_router(client):
    """删除路由组"""
    client.post("/api/routers", json={"name": "tmp", "models": []},
                headers={"Authorization": "Bearer sk-test"})
    r = client.delete("/api/routers/tmp", headers={"Authorization": "Bearer sk-test"})
    assert r.status_code == 200
    r2 = client.get("/api/routers", headers={"Authorization": "Bearer sk-test"})
    data = r2.json()
    routers = data.get("data", data) if isinstance(data, dict) else data
    assert "tmp" not in routers


def test_router_in_models_list(client):
    """路由组应出现在 /v1/models 输出列表"""
    client.post("/api/routers", json={"name": "my-group", "models": ["NVIDIA-z-ai/glm-5.2"]},
                headers={"Authorization": "Bearer sk-test"})
    r = client.get("/v1/models", headers={"Authorization": "Bearer sk-test"})
    assert r.status_code == 200
    ids = [m["id"] for m in r.json().get("data", [])]
    assert "my-group" in ids