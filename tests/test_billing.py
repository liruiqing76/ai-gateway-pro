# ============================================================
# 计费模块测试
# ============================================================
import sys, os, tempfile
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


def register_user(username="billinguser", email="billing@example.com"):
    resp = client.post("/api/auth/register", json={
        "username": username, "email": email, "password": "Test1234!",
    })
    data = resp.json()
    return data["access_token"], data["api_key"]


class TestPlans:
    def test_list_plans(self):
        resp = client.get("/api/billing/plans")
        assert resp.status_code == 200
        plans = resp.json()
        assert len(plans) >= 3
        names = {p["plan"] for p in plans}
        assert "free" in names and "pro" in names and "enterprise" in names

    def test_pro_plan_price(self):
        resp = client.get("/api/billing/plans")
        pro = next(p for p in resp.json() if p["plan"] == "pro")
        assert pro["price_cents"] == 2900  # ¥29

    def test_unknown_plan_defaults_zero(self):
        """未知计划名 → 返回 0 配额/渠道上限而非抛异常"""
        from backend.billing.plans import get_plan, get_plan_daily_quota, get_plan_max_providers
        assert get_plan("nonexistent") is None
        assert get_plan_daily_quota("nonexistent") == 0
        assert get_plan_max_providers("nonexistent") == 0
        # 已知计划正常
        assert get_plan_daily_quota("free") == 100
        assert get_plan_max_providers("pro") == 20


class TestBillingMe:
    def test_my_billing(self):
        token, _ = register_user()
        resp = client.get("/api/billing/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["tier"] == "free"
        assert data["quota_used_today"] == 0


class TestOrders:
    def test_place_pro_order(self):
        token, _ = register_user()
        resp = client.post("/api/billing/orders", json={
            "plan_name": "pro",
            "payment_method": "wechat",
        }, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["plan_name"] == "pro"
        assert data["amount_cents"] == 2900
        assert data["payment_status"] == "pending"
        assert len(data["order_no"]) > 10

    def test_place_invalid_plan(self):
        token, _ = register_user()
        resp = client.post("/api/billing/orders", json={
            "plan_name": "nonexistent",
        }, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 400

    def test_list_orders(self):
        token, _ = register_user()
        client.post("/api/billing/orders", json={"plan_name": "pro"},
                    headers={"Authorization": f"Bearer {token}"})
        client.post("/api/billing/orders", json={"plan_name": "enterprise"},
                    headers={"Authorization": f"Bearer {token}"})
        resp = client.get("/api/billing/orders", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_mark_paid_requires_admin(self):
        token, _ = register_user()
        order = client.post("/api/billing/orders", json={"plan_name": "pro"},
                            headers={"Authorization": f"Bearer {token}"}).json()
        resp = client.post("/api/billing/admin/mark-paid", json={"order_no": order["order_no"]},
                           headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403  # 非 admin 被拒

    def test_mark_paid_upgrades_user(self):
        # 注册普通用户并下单
        token, _ = register_user()
        order = client.post("/api/billing/orders", json={"plan_name": "pro"},
                            headers={"Authorization": f"Bearer {token}"}).json()

        # 注册管理员(直接改库)
        import backend.models
        from backend.models import get_session, User
        session = get_session()
        try:
            admin = session.query(User).filter_by(email="billing@example.com").first()
            admin.role = "admin"
            session.commit()
        finally:
            session.close()

        # admin token
        resp = client.post("/api/auth/login", json={
            "email": "billing@example.com", "password": "Test1234!",
        })
        admin_token = resp.json()["access_token"]

        # 标记支付
        resp = client.post("/api/billing/admin/mark-paid", json={"order_no": order["order_no"]},
                           headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200

        # 验证用户已升级
        resp = client.get("/api/billing/me", headers={"Authorization": f"Bearer {token}"})
        data = resp.json()
        assert data["tier"] == "pro"
        assert data["quota_daily"] == 99999