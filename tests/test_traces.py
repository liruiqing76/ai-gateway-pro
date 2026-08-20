# ============================================================
# 请求链路日志 + 流式编排测试
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


class TestTraceContext:
    def test_trace_context_basics(self):
        from backend.core.trace import TraceContext
        t = TraceContext(user_id=1, original_model="gpt-4")
        t.mark_selected("NVIDIA", "deepseek-v3", "quality strategy")
        t.mark_circuit_breaker(True)
        t.mark_tokens(100, 200)
        t.add_stage(0, "NVIDIA", "deepseek-v3", 1200, True)
        t.finish()

        d = t.to_dict()
        assert d["user_id"] == 1
        assert d["original_model"] == "gpt-4"
        assert d["provider_name"] == "NVIDIA"
        assert d["model_name"] == "deepseek-v3"
        assert d["circuit_breaker_hit"] is True
        assert d["prompt_tokens"] == 100
        assert d["total_tokens"] == 300
        assert d["latency_ms"] >= 0
        assert len(d["stages_detail"]) == 1

    def test_trace_error(self):
        from backend.core.trace import TraceContext
        t = TraceContext(user_id=1, original_model="gpt-4")
        t.mark_error("connection timeout")
        t.finish()
        d = t.to_dict()
        assert d["success"] is False
        assert d["error_msg"] == "connection timeout"


class TestTracesAPI:
    def _insert_trace(self, monkeypatch, db_path):
        """手动插入一条 trace 记录"""
        from sqlalchemy import create_engine
        from backend.models import Base, get_session, RequestTrace
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        monkeypatch.setattr("backend.models._get_engine", lambda: engine)
        s = get_session()
        trace = RequestTrace(
            user_id=1, request_id="abc123",
            original_model="gpt-4", resolved_model="high_quality",
            provider_name="NVIDIA", model_name="deepseek-v3",
            selection_strategy="quality", selection_reason="highest score",
            latency_ms=1500, success=True,
            prompt_tokens=100, completion_tokens=200, total_tokens=300,
        )
        s.add(trace)
        s.commit()
        s.close()

    def test_list_traces_empty(self, client):
        c, api_key = client
        resp = c.get("/api/traces", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_list_traces_with_data(self, client, tmp_path, monkeypatch):
        self._insert_trace(monkeypatch, tmp_path / "test.db")
        c, api_key = client
        resp = c.get("/api/traces", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.json()["total"] == 1
        data = resp.json()["data"][0]
        assert data["original_model"] == "gpt-4"
        assert data["provider_name"] == "NVIDIA"

    def test_filter_by_model(self, client, tmp_path, monkeypatch):
        self._insert_trace(monkeypatch, tmp_path / "test.db")
        c, api_key = client
        resp = c.get("/api/traces?model=deepseek", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.json()["total"] == 1
        resp = c.get("/api/traces?model=nonexistent", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.json()["total"] == 0

    def test_filter_by_success(self, client, tmp_path, monkeypatch):
        self._insert_trace(monkeypatch, tmp_path / "test.db")
        c, api_key = client
        resp = c.get("/api/traces?success=true", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.json()["total"] == 1
        resp = c.get("/api/traces?success=false", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.json()["total"] == 0

    def test_stats(self, client, tmp_path, monkeypatch):
        self._insert_trace(monkeypatch, tmp_path / "test.db")
        c, api_key = client
        resp = c.get("/api/traces/stats", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_requests"] == 1
        assert data["success_count"] == 1
        assert data["avg_latency_ms"] == 1500

    def test_cleanup(self, client, tmp_path, monkeypatch):
        self._insert_trace(monkeypatch, tmp_path / "test.db")
        c, api_key = client
        resp = c.delete("/api/traces/cleanup?days=1", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200
        # 刚插入的记录不会被清理(不到1天)
        resp = c.get("/api/traces", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.json()["total"] == 1

    def test_auth_required(self, client):
        c, _ = client
        resp = c.get("/api/traces")
        assert resp.status_code == 401


class TestOrchestrateStream:
    """测试流式编排的辅助函数"""

    def test_build_stage_body(self):
        from backend.core.orchestrate import build_stage_body
        body = {"messages": [{"role": "user", "content": "hello"}], "model": "test"}
        result = build_stage_body(body, 0, 2, "hello")
        assert result["messages"][-1]["content"] == "hello"

        result = build_stage_body(body, 1, 2, "hello", "previous output")
        # 第二级应该包含上下文交接
        has_handoff = any("接力" in str(m.get("content", "")) for m in result["messages"])
        assert has_handoff

    def test_parse_pipeline_stream(self):
        from backend.core.orchestrate import parse_pipeline
        assert parse_pipeline("relay:quick/high_quality") == ("relay", ["quick", "high_quality"])
        assert parse_pipeline("review:high_quality/vision") == ("review", ["high_quality", "vision"])
        assert parse_pipeline("relay:only-one") is None  # relay needs >= 2 stages
        assert parse_pipeline("gpt-4") is None  # not a pipeline
