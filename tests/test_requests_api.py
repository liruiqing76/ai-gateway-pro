# ============================================================
# 合并视图 /api/requests 测试
# 覆盖三种行形态: 链路+聊天成对 / 只有链路(探测) / 只有聊天
# ============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
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
    u = User(username="req", email="req@x.com", password_hash=pw_hash, tier="free", api_key="sk-req")
    s.add(u)
    s.commit()
    from backend.auth.security import create_access_token
    token = create_access_token(u.id)
    s.close()

    from backend.main import app
    return TestClient(app), token


def _seed(client):
    """插入三种形态: 成对(a) + 仅链路探测(b) + 孤儿聊天(c, request_id=NULL)"""
    from backend.models import get_session, User, RequestTrace, ChatLog
    s = get_session()
    try:
        u = s.query(User).filter_by(email="req@x.com").first()
        rid = "abc123"
        s.add(RequestTrace(
            user_id=u.id, request_id=rid, original_model="high_quality",
            model_name="m1", provider_name="P1", total_tokens=100,
            latency_ms=800, success=True, request_type="real",
        ))
        s.add(ChatLog(
            user_id=u.id, request_id=rid, original_model="high_quality",
            model_name="m1", provider_name="P1", prompt_text="hello world",
            completion_text="hi", prompt_chars=11, completion_chars=2,
            total_chars=13, prompt_tokens=90, completion_tokens=10,
            total_tokens=100, success=True,
        ))
        s.add(RequestTrace(
            user_id=u.id, request_id="probe1", original_model="m2",
            model_name="m2", provider_name="P2", total_tokens=5,
            latency_ms=50, success=True, request_type="speed_test",
        ))
        s.add(ChatLog(
            user_id=u.id, request_id=None, original_model="legacy",
            model_name="m3", prompt_text="orphan", total_chars=6,
            total_tokens=7, success=True,
        ))
        s.commit()
    finally:
        s.close()


class TestRequestsList:
    def test_union_contains_all_shapes(self, client):
        c, token = client
        _seed(client)
        resp = c.get("/api/requests", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        # 3 行: 成对1 + 探测1 + 孤儿聊天1
        assert data["total"] == 3
        joined = [r for r in data["data"] if r["has_trace"] and r["has_chat"]]
        probe = [r for r in data["data"] if r["request_type"] == "speed_test"]
        orphan = [r for r in data["data"] if not r["has_trace"] and r["has_chat"]]
        assert len(joined) == 1 and joined[0]["total_tokens"] == 100
        assert len(probe) == 1 and probe[0]["trace_id"] is not None
        assert len(orphan) == 1 and orphan[0]["chat_id"] is not None

    def test_joined_row_has_content_preview(self, client):
        c, token = client
        _seed(client)
        resp = c.get("/api/requests", headers={"Authorization": f"Bearer {token}"})
        row = next(r for r in resp.json()["data"] if r["has_trace"] and r["has_chat"])
        assert (row["prompt_preview"] or "").startswith("hello")

    def test_filter_by_request_type(self, client):
        c, token = client
        _seed(client)
        resp = c.get("/api/requests?request_type=speed_test",
                     headers={"Authorization": f"Bearer {token}"})
        data = resp.json()
        assert data["total"] == 1
        assert data["data"][0]["request_type"] == "speed_test"

    def test_filter_pipeline_only(self, client):
        c, token = client
        _seed(client)
        resp = c.get("/api/requests?pipeline_only=true",
                     headers={"Authorization": f"Bearer {token}"})
        assert resp.json()["total"] == 0

    def test_pagination(self, client):
        c, token = client
        _seed(client)
        resp = c.get("/api/requests?limit=2&offset=0",
                     headers={"Authorization": f"Bearer {token}"})
        body = resp.json()
        assert body["limit"] == 2 and len(body["data"]) == 2

    def test_requires_auth(self, client):
        c, _ = client
        assert c.get("/api/requests").status_code == 401


class TestTraceDetail:
    def test_get_trace_detail(self, client):
        from backend.models import get_session, User, RequestTrace
        c, token = client
        _seed(client)
        s = get_session()
        try:
            u = s.query(User).filter_by(email="req@x.com").first()
            tid = s.query(RequestTrace).filter_by(user_id=u.id).first().id
        finally:
            s.close()
        resp = c.get(f"/api/traces/{tid}", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["request_id"]

    def test_get_missing_trace_404(self, client):
        c, token = client
        resp = c.get("/api/traces/999999", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 404
