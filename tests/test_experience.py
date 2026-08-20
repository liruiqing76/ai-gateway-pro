# ============================================================
# 经验积累 / 每日总结 / 注入 测试
# ============================================================
import sys, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离 DB 测试客户端, 固定机器码, LLM 调用替换为确定性输出。"""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("GW_DB_PATH", str(db_path))
    from backend.config import settings as cfg
    monkeypatch.setattr(cfg, "LICENSE_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "EXPERIENCE_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "EXPERIENCE_INJECT_AUTO", True, raising=False)

    from sqlalchemy import create_engine
    from backend.models import Base, get_session, User
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr("backend.models._get_engine", lambda: engine)

    s = get_session()
    import bcrypt
    pw = bcrypt.hashpw(b"Test1234!", bcrypt.gensalt()).decode()
    u = User(username="expuser", email="exp@x.com", password_hash=pw,
             tier="free", api_key="sk-exp")
    s.add(u)
    s.commit()
    # 给测试用户建立一条聊天记录
    from backend.models import ChatLog
    s.add(ChatLog(user_id=u.id, original_model="gpt-4", model_name="gpt-4",
                  provider_name="N", prompt_text="今天部署失败了三次",
                  completion_text="先看日志再回滚", prompt_chars=8,
                  completion_chars=8, total_chars=16, success=True))
    s.commit()
    s.close()

    from backend.main import app
    return TestClient(app), "sk-exp"


def _mock_llm(monkeypatch, text):
    async def _fake(messages, max_tokens=1000):
        return text
    monkeypatch.setattr("backend.core.experience._call_llm", _fake)


AUTH = {"Authorization": "Bearer sk-exp"}


class TestExperienceAPI:
    def test_distill_saves_lessons(self, client, monkeypatch):
        _mock_llm(monkeypatch, json.dumps([
            {"kind": "lesson", "title": "部署先看日志",
             "content": "失败时应先看日志再回滚，避免重复提交。"},
            {"kind": "fact", "title": "SQLite 并发",
             "content": "SQLite 默认串行写，多进程写需注意锁。"},
        ], ensure_ascii=False))
        c, _ = client
        r = c.post("/api/experience/distill?limit=10", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["created"] == 2
        lst = c.get("/api/experience", headers=AUTH)
        assert lst.json()["count"] == 2

    def test_stats(self, client, monkeypatch):
        _mock_llm(monkeypatch, json.dumps([
            {"kind": "lesson", "title": "A", "content": "a"},
        ]))
        c, _ = client
        c.post("/api/experience/distill?limit=10", headers=AUTH)
        r = c.get("/api/experience/stats", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["total"] == 1
        assert r.json()["lessons"] == 1

    def test_summarize_rules_fallback(self, client, monkeypatch):
        # LLM 返回空 → 走规则兜底
        _mock_llm(monkeypatch, "")
        c, _ = client
        r = c.post("/api/experience/summarize", headers=AUTH)
        assert r.status_code == 200
        assert "调用" in r.json()["content"]

    def test_summaries_list(self, client, monkeypatch):
        _mock_llm(monkeypatch, "今日经验总结正文")
        c, _ = client
        c.post("/api/experience/summarize", headers=AUTH)
        r = c.get("/api/experience/summaries", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["count"] >= 1

    def test_injection_preview(self, client, monkeypatch):
        _mock_llm(monkeypatch, json.dumps([
            {"kind": "lesson", "title": "A教训", "content": "内容A"},
        ]))
        c, _ = client
        c.post("/api/experience/distill?limit=10", headers=AUTH)
        r = c.get("/api/experience/injection?model=gpt-4", headers=AUTH)
        assert r.status_code == 200
        assert "历史经验" in r.json()["block"]

    def test_feedback_and_delete(self, client, monkeypatch):
        _mock_llm(monkeypatch, json.dumps([
            {"kind": "fact", "title": "F", "content": "f"},
        ]))
        c, _ = client
        c.post("/api/experience/distill?limit=10", headers=AUTH)
        exp_id = c.get("/api/experience", headers=AUTH).json()["data"][0]["id"]
        r = c.post(f"/api/experience/{exp_id}/feedback?positive=true", headers=AUTH)
        assert r.status_code == 200
        r = c.delete(f"/api/experience/{exp_id}", headers=AUTH)
        assert r.status_code == 200
        assert c.get("/api/experience", headers=AUTH).json()["count"] == 0


class TestDataModel:
    def test_tables_created(self, tmp_path, monkeypatch):
        from sqlalchemy import create_engine
        from backend.models import Base, ExperienceLog, DailySummary
        engine = create_engine(f"sqlite:///{tmp_path/'x.db'}")
        Base.metadata.create_all(engine)
        from sqlalchemy import inspect
        tables = inspect(engine).get_table_names()
        assert "experience_logs" in tables
        assert "daily_summaries" in tables

    def test_distill_parsing_bad(self, client):
        from backend.core.experience import _extract_json_array
        assert _extract_json_array("hello") == []
        assert _extract_json_array('```json\n[{"kind":"fact","title":"T","content":"C"}]\n```') == [
            {"kind": "fact", "title": "T", "content": "C"}]
