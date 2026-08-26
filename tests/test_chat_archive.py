# ============================================================
# 聊天原文归档(DB 瘦身)测试
# chat_archive 模块 + save_chat_log 外置链路 + 详情读回
# ============================================================
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离 DB + 隔离归档目录的测试客户端"""
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
    u = User(username="archive", email="archive@x.com", password_hash=pw_hash,
             tier="free", api_key="sk-archive", is_active=True)
    s.add(u)
    s.commit()
    uid = u.id  # close 后访问 detached 属性会抛 DetachedInstanceError
    s.close()

    from backend.auth.security import create_access_token
    from backend.main import app
    token = create_access_token(uid)
    return TestClient(app), token


class TestPreview:
    def test_short_text_unchanged(self):
        from backend.core.chat_archive import make_preview
        assert make_preview("hi") == "hi"

    def test_long_text_head_tail_marker(self):
        from backend.core.chat_archive import make_preview, PREVIEW_HEAD, PREVIEW_TAIL
        text = "A" * 100 + "B" * 10000 + "C" * 100
        pv = make_preview(text)
        assert pv.startswith("A" * 100)
        assert pv.endswith("C" * 100)
        assert "已归档省略" in pv
        assert len(pv) < PREVIEW_HEAD + PREVIEW_TAIL + 100

    def test_tail_of_preview(self):
        from backend.core.chat_archive import make_preview, tail_of
        text = "A" * 100 + "B" * 10000 + "最近的真实交互内容"
        pv = make_preview(text)
        tail = tail_of(pv)
        assert "最近的真实交互内容" in tail
        assert "A" * 100 not in tail
        # 短文本原样
        assert tail_of("short text") == "short text"


class TestArchiveRoundTrip:
    def test_append_read_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GW_DB_PATH", str(tmp_path / "x" / "gw.db"))
        from backend.core import chat_archive
        ts = datetime(2026, 8, 15, 10, 0, 0)
        text = ("你好" * 3000) + "\n结尾标记"
        assert chat_archive.append_prompt(7, ts, text)
        got = chat_archive.read_prompt(7, ts)
        assert got == text
        # 文件名按月份
        assert (chat_archive.archive_dir() / "2026-08.jsonl.gz").exists()
        # 无 gzip 头残留: 相邻 id 也可读
        assert chat_archive.append_prompt(8, ts, "另一条")
        assert chat_archive.read_prompt(8, ts) == "另一条"

    def test_month_file_per_record(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GW_DB_PATH", str(tmp_path / "gw.db"))
        from backend.core import chat_archive
        assert chat_archive.append_prompt(1, datetime(2026, 7, 1), "七月")
        assert chat_archive.append_prompt(2, datetime(2026, 8, 1), "八月")
        # id 相同月份不同也不串
        assert chat_archive.append_prompt(1, datetime(2026, 8, 5), "八月同 id")
        assert chat_archive.read_prompt(1, datetime(2026, 7, 1)) == "七月"
        assert chat_archive.read_prompt(1, datetime(2026, 8, 5)) == "八月同 id"

    def test_missing_id_and_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GW_DB_PATH", str(tmp_path / "gw.db"))
        from backend.core import chat_archive
        ts = datetime(2026, 8, 15)
        assert chat_archive.read_prompt(999, ts) is None          # id 不在文件
        assert chat_archive.read_prompt(1, datetime(2025, 1, 1)) is None  # 文件不存在

    def test_corrupted_line_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GW_DB_PATH", str(tmp_path / "gw.db"))
        from backend.core import chat_archive
        ts = datetime(2026, 8, 15)
        chat_archive.append_prompt(1, ts, "完整记录")
        # 人为追加一条撕裂行(坏 JSON)
        import gzip
        f = chat_archive.month_file(ts)
        with gzip.open(f, "ab") as fh:
            fh.write(b'{"id": 2, "t": "broken\n')
        chat_archive.append_prompt(3, ts, "后面的记录")
        assert chat_archive.read_prompt(1, ts) == "完整记录"
        assert chat_archive.read_prompt(3, ts) == "后面的记录"


class TestEnsureColumn:
    def test_adds_column_idempotent(self, client):
        import sqlalchemy as sa
        from backend.main import app
        from backend.core.chat_archive import ensure_archive_column
        c, _ = client
        # 启动测试时就 ensure 过(列天然存在) → 幂等
        assert ensure_archive_column() is True
        from backend.models import get_session
        s = get_session()
        try:
            cols = [r[1] for r in s.execute(sa.text("PRAGMA table_info(chat_logs)"))]
            assert "prompt_archive" in cols
        finally:
            s.close()


class TestSaveExternalize:
    def test_long_prompt_externalized(self, client, monkeypatch):
        """长 prompt: DB 只存预览+归档标记, 文件可读回原文, 详情接口返回全文"""
        from backend.core.chat_log import save_chat_log
        from backend.core import chat_archive
        from backend.models import get_session, ChatLog, User
        c, token = client
        s = get_session()
        uid = s.query(User.id).filter_by(username="archive").scalar()
        s.close()

        original = ("[system] 固定提示\x00词段" * 200) + "\n[user] 真实交互内容ABC\n结尾"
        rec = save_chat_log(
            user_id=uid, original_model="m1", prompt_text=original,
            completion_text="回复",
        )
        # save_chat_log 返回 detached 实例: 用新 session 拿 id, 不访问 rec 属性
        s = get_session()
        try:
            row = s.query(ChatLog).order_by(ChatLog.id.desc()).first()
            assert row.user_id == uid
            rec_id, rec_ts = row.id, row.created_at
            assert row.prompt_archive is not None
            assert "已归档省略" in row.prompt_text
            assert len(row.prompt_text) < 3000
        finally:
            s.close()
        # 文件读回原文
        full = chat_archive.read_prompt(rec_id, rec_ts)
        assert full == original
        # 详情接口返回全文(一致)
        r = c.get(f"/api/chats/{rec_id}", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["prompt_text"] == original
        assert r.json().get("prompt_archived") is not None

    def test_short_prompt_stays_in_db(self, client):
        """短 prompt 不归档, 直接入库"""
        from backend.core.chat_log import save_chat_log
        from backend.models import get_session, ChatLog, User
        c, token = client
        s = get_session()
        uid = s.query(User.id).filter_by(username="archive").scalar()
        s.close()
        rec = save_chat_log(user_id=uid, original_model="m1",
                            prompt_text="你好, 请介绍一下自己")
        s = get_session()
        try:
            row = s.query(ChatLog).order_by(ChatLog.id.desc()).first()
            rec_id = row.id
            assert row.prompt_archive is None
            assert row.prompt_text == "你好, 请介绍一下自己"
        finally:
            s.close()
        r = c.get(f"/api/chats/{rec_id}", headers={"Authorization": f"Bearer {token}"})
        assert r.json()["prompt_text"] == "你好, 请介绍一下自己"
        assert "prompt_archived" not in r.json()

    def test_archive_write_failure_falls_back(self, client, monkeypatch):
        """归档写入失败 → 原文留库, 不丢数据"""
        from backend.core.chat_log import save_chat_log
        from backend.core import chat_archive
        from backend.models import get_session, ChatLog, User
        monkeypatch.setattr(chat_archive, "append_prompt", lambda *a, **k: False)
        c, token = client
        s = get_session()
        uid = s.query(User.id).filter_by(username="archive").scalar()
        s.close()
        original = "X" * 5000
        rec = save_chat_log(user_id=uid, original_model="m1", prompt_text=original)
        s = get_session()
        try:
            row = s.query(ChatLog).order_by(ChatLog.id.desc()).first()
            assert row.prompt_archive is None
            assert row.prompt_text == original  # 原文仍在, 迁移脚本可补
        finally:
            s.close()


class TestExperienceSampling:
    def test_distill_input_uses_tail(self):
        """提炼采样取尾部而非头部: agent 上下文前几百字符全是固定 system 提示词"""
        from backend.core.experience import _distill_one_chunk
        import asyncio
        # 不真调 LLM: 只验证 transcript 构造路径不会崩(默认无 provider 返回 0)
        class FakeChat:
            id = 1
            original_model = "m"
            prompt_text = "A" * 100 + "\n...[中间5000字符已归档省略]...\n" + "用户最新真实交互"
            completion_text = "输出内容"
        calls = []
        async def fake_call_llm(messages, max_tokens=1200):
            calls.append(messages)
            return ""
        from unittest.mock import patch
        with patch("backend.core.experience._call_llm", fake_call_llm):
            asyncio.run(_distill_one_chunk(1, [FakeChat()]))
        assert calls, "LLM 应被调用一次"
        joined = "".join(str(m) for m in calls[0])
        assert "用户最新真实交互" in joined
        assert "A" * 100 not in joined