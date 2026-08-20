# ============================================================
# 聊天记录 API 测试
# ============================================================
import sys
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
    s.close()

    from backend.main import app
    return TestClient(app), "sk-test"


class TestExtract:
    def test_extract_prompt_text_string(self):
        from backend.core.chat_log import extract_prompt_text
        body = {"messages": [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "你好"},
        ]}
        text = extract_prompt_text(body)
        assert "[system]" in text
        assert "[user]" in text
        assert "你是助手" in text
        assert "你好" in text

    def test_extract_prompt_text_multimodal(self):
        from backend.core.chat_log import extract_prompt_text
        body = {"messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "这张图是什么"},
                {"type": "image_url", "image_url": {"url": "http://x"}},
            ]},
        ]}
        text = extract_prompt_text(body)
        assert "这张图是什么" in text
        # 图片部分不应出现 url
        assert "http://x" not in text

    def test_extract_completion_text(self):
        from backend.core.chat_log import extract_completion_text
        resp = {"choices": [{"message": {"content": "回复内容"}}]}
        assert extract_completion_text(resp) == "回复内容"
        assert extract_completion_text({"choices": []}) == ""

    def test_extract_delta_text(self):
        from backend.core.chat_log import extract_delta_text
        chunk = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        assert extract_delta_text(chunk) == "hello"
        assert extract_delta_text(b'data: [DONE]\n\n') == ""
        assert extract_delta_text(b'invalid') == ""


class TestChatsAPI:
    def _insert(self, monkeypatch, db_path, **kw):
        from sqlalchemy import create_engine
        from backend.models import Base, get_session, ChatLog
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        monkeypatch.setattr("backend.models._get_engine", lambda: engine)
        s = get_session()
        defaults = dict(
            user_id=1, original_model="gpt-4", model_name="deepseek-v3",
            provider_name="NVIDIA", prompt_text="hello", completion_text="world",
            prompt_chars=5, completion_chars=5, total_chars=10,
            prompt_tokens=3, completion_tokens=3, total_tokens=6,
            success=True, is_stream=False, is_pipeline=False,
        )
        defaults.update(kw)
        s.add(ChatLog(**defaults))
        s.commit()
        s.close()

    def test_list_empty(self, client):
        c, k = client
        resp = c.get("/api/chats", headers={"Authorization": f"Bearer {k}"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_list_with_data(self, client, tmp_path, monkeypatch):
        self._insert(monkeypatch, tmp_path / "test.db")
        c, k = client
        resp = c.get("/api/chats", headers={"Authorization": f"Bearer {k}"})
        assert resp.json()["total"] == 1
        d = resp.json()["data"][0]
        assert d["original_model"] == "gpt-4"
        # 列表摘要不应含全文, 只含 preview
        assert "prompt_text" not in d
        assert "prompt_preview" in d
        assert d["total_chars"] == 10

    def test_detail_has_full_text(self, client, tmp_path, monkeypatch):
        self._insert(monkeypatch, tmp_path / "test.db", prompt_text="全文输入", completion_text="全文输出")
        c, k = client
        # 拿 id
        lst = c.get("/api/chats", headers={"Authorization": f"Bearer {k}"})
        cid = lst.json()["data"][0]["id"]
        resp = c.get(f"/api/chats/{cid}", headers={"Authorization": f"Bearer {k}"})
        assert resp.status_code == 200
        d = resp.json()
        assert d["prompt_text"] == "全文输入"
        assert d["completion_text"] == "全文输出"

    def test_filter_by_model(self, client, tmp_path, monkeypatch):
        self._insert(monkeypatch, tmp_path / "test.db", model_name="deepseek-v3")
        c, k = client
        assert c.get("/api/chats?model=deepseek", headers={"Authorization": f"Bearer {k}"}).json()["total"] == 1
        assert c.get("/api/chats?model=nonexist", headers={"Authorization": f"Bearer {k}"}).json()["total"] == 0

    def test_stats(self, client, tmp_path, monkeypatch):
        self._insert(monkeypatch, tmp_path / "test.db")
        c, k = client
        resp = c.get("/api/chats/stats?days=7", headers={"Authorization": f"Bearer {k}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_chats"] == 1
        assert data["success_count"] == 1
        assert data["avg_total_chars"] == 10
        assert data["sum_total_chars"] == 10

    def test_cleanup(self, client, tmp_path, monkeypatch):
        self._insert(monkeypatch, tmp_path / "test.db")
        c, k = client
        resp = c.delete("/api/chats/cleanup?days=1", headers={"Authorization": f"Bearer {k}"})
        assert resp.status_code == 200
        # 不满1天不应被删
        assert c.get("/api/chats", headers={"Authorization": f"Bearer {k}"}).json()["total"] == 1

    def test_auth_required(self, client):
        c, _ = client
        assert c.get("/api/chats").status_code == 401
