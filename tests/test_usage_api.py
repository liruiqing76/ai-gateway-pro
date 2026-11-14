# ============================================================
# 用量统计 API 测试
# ============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离 DB 的测试客户端(JWT 认证)"""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("GW_DB_PATH", str(db_path))

    from sqlalchemy import create_engine
    from backend.models import Base, get_session, User, UsageLog
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr("backend.models._get_engine", lambda: engine)

    s = get_session()
    import bcrypt
    pw_hash = bcrypt.hashpw(b"Test1234!", bcrypt.gensalt()).decode()
    u = User(username="usage", email="usage@x.com", password_hash=pw_hash, tier="free", api_key="sk-usage")
    s.add(u)
    s.commit()

    from backend.auth.security import create_access_token
    token = create_access_token(u.id)
    s.close()

    from backend.main import app
    return TestClient(app), token


def _insert_logs(user_id: int, n: int = 3, success: bool = True):
    from backend.models import get_session, UsageLog
    s = get_session()
    try:
        for i in range(n):
            s.add(UsageLog(
                user_id=user_id,
                provider_name="ProviderA",
                model_name="model-x",
                prompt_tokens=10 + i,
                completion_tokens=20 + i,
                total_tokens=30 + i * 2,
                cost=0.001,
                latency_ms=100 + i,
                success=success,
            ))
        s.commit()
    finally:
        s.close()


class TestUsageSummary:
    def test_empty_summary(self, client):
        c, token = client
        resp = c.get("/api/usage/summary", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json() == {"total": 0, "today": 0, "today_tokens": 0,
                               "total_tokens": 0, "failed": 0,
                               "today_chars": 0, "total_chars": 0,
                               "today_cost": 0.0, "total_cost": 0.0}

    def test_summary_with_data(self, client):
        c, token = client
        from backend.models import get_session, User, ChatLog
        s = get_session()
        try:
            u = s.query(User).filter_by(email="usage@x.com").first()
            _insert_logs(u.id, n=5, success=True)
            _insert_logs(u.id, n=2, success=False)
            # 失败数与"聊天记录"菜单同口径: 从 chat_logs 统计
            # (生产环境 usage_logs 只写成功调用)
            for i in range(2):
                s.add(ChatLog(
                    user_id=u.id,
                    original_model="model-x",
                    model_name="model-x",
                    prompt_text="hi",
                    total_tokens=10,
                    success=False,
                    error_msg="boom",
                ))
            s.commit()
        finally:
            s.close()

        resp = c.get("/api/usage/summary", headers={"Authorization": f"Bearer {token}"})
        data = resp.json()
        assert data["total"] == 7
        assert data["today"] == 7
        assert data["failed"] == 2
        assert data["today_tokens"] > 0
        assert data["total_tokens"] >= data["today_tokens"]

    def test_summary_requires_auth(self, client):
        c, _ = client
        resp = c.get("/api/usage/summary")
        assert resp.status_code == 401

    def test_monthly_aggregation(self, client):
        """按月聚合: 有数据的月计入调用与token, 空月份补零, 长度=请求月数"""
        c, token = client
        from backend.models import get_session, User
        s = get_session()
        try:
            u = s.query(User).filter_by(email="usage@x.com").first()
            _insert_logs(u.id, n=3)
        finally:
            s.close()

        resp = c.get("/api/usage/monthly?months=6", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 6
        # 列表从旧到新, 最后一个是当前月
        assert data[-1]["calls"] == 3
        assert data[-1]["tokens"] > 0
        assert all(d["calls"] == 0 for d in data[:-1])

    def test_monthly_requires_auth(self, client):
        c, _ = client
        resp = c.get("/api/usage/monthly")
        assert resp.status_code == 401


class TestUsageDailyMonth:
    def test_daily_month_returns_full_month(self, client):
        """month=YYYY-MM 返回该自然月每天(日历用), 无数据日补零"""
        c, token = client
        from datetime import datetime
        import calendar
        from backend.models import get_session, User
        s = get_session()
        try:
            u = s.query(User).filter_by(email="usage@x.com").first()
            _insert_logs(u.id, n=2)
        finally:
            s.close()

        ym = datetime.now().strftime("%Y-%m")
        month_days = calendar.monthrange(datetime.now().year, datetime.now().month)[1]
        resp = c.get(f"/api/usage/daily?month={ym}",
                     headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == month_days
        # 序列从月初 1 号开始, 连续无跳号
        assert data[0]["date"].endswith("-01")
        today = datetime.now().strftime("%Y-%m-%d")
        row = next(d for d in data if d["date"] == today)
        assert row["calls"] == 2
        assert row["tokens"] > 0
        assert all("calls" in d and "tokens" in d for d in data)

    def test_daily_month_bad_format_400(self, client):
        c, token = client
        resp = c.get("/api/usage/daily?month=2026-13",
                     headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 400

    def test_daily_month_future_400(self, client):
        c, token = client
        from datetime import datetime, timedelta
        future = (datetime.now() + timedelta(days=40)).strftime("%Y-%m")
        resp = c.get(f"/api/usage/daily?month={future}",
                     headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 400

    def test_daily_default_days_behavior(self, client):
        """不带 month 时保持默认最近 30 天行为"""
        c, token = client
        resp = c.get("/api/usage/daily",
                     headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 30


class TestUsageList:
    def test_list_empty(self, client):
        c, token = client
        resp = c.get("/api/usage", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_list_serialized_fields(self, client):
        c, token = client
        from backend.models import get_session, User
        s = get_session()
        try:
            u = s.query(User).filter_by(email="usage@x.com").first()
            _insert_logs(u.id, n=1)
        finally:
            s.close()

        resp = c.get("/api/usage", headers={"Authorization": f"Bearer {token}"})
        data = resp.json()
        assert data["count"] == 1
        row = data["data"][0]
        assert row["provider"] == "ProviderA"
        assert row["model"] == "model-x"
        assert row["tokens"] == 30          # total_tokens
        assert row["success"] is True
        assert row["latency_ms"] == 100
        assert "time" in row and row["time"]

    def test_list_limit_clamped(self, client):
        c, token = client
        from backend.models import get_session, User
        s = get_session()
        try:
            u = s.query(User).filter_by(email="usage@x.com").first()
            _insert_logs(u.id, n=10)
        finally:
            s.close()

        resp = c.get("/api/usage?limit=3", headers={"Authorization": f"Bearer {token}"})
        assert resp.json()["count"] == 3

        # 超范围 limit 被钳制
        resp = c.get("/api/usage?limit=99999", headers={"Authorization": f"Bearer {token}"})
        assert resp.json()["count"] == 10

    def test_list_requires_auth(self, client):
        c, _ = client
        resp = c.get("/api/usage")
        assert resp.status_code == 401

    def test_isolation_between_users(self, client, tmp_path, monkeypatch):
        """不同用户的用量互不可见"""
        c, token = client
        from backend.models import get_session, User
        s = get_session()
        try:
            u = s.query(User).filter_by(email="usage@x.com").first()
            _insert_logs(u.id, n=4)
            # 第二个用户
            import bcrypt
            pw = bcrypt.hashpw(b"Test1234!", bcrypt.gensalt()).decode()
            u2 = User(username="other", email="other@x.com", password_hash=pw, tier="free", api_key="sk-other")
            s.add(u2)
            s.commit()
            from backend.auth.security import create_access_token
            token2 = create_access_token(u2.id)
        finally:
            s.close()

        resp1 = c.get("/api/usage", headers={"Authorization": f"Bearer {token}"})
        assert resp1.json()["count"] == 4

        resp2 = c.get("/api/usage", headers={"Authorization": f"Bearer {token2}"})
        assert resp2.json()["count"] == 0