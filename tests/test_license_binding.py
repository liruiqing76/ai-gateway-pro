# ============================================================
# 商业化设备绑定测试
# 单机单账号 + 机器码一致性锁定 + 远程锁定字段
# ============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离 DB 的测试客户端, 开启 license 校验并固定机器码"""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("GW_DB_PATH", str(db_path))
    # 强制 license 模块的 settings 单例对象开启校验(env 仅影响首次加载)
    from backend.config import settings as cfg_settings
    monkeypatch.setattr(cfg_settings, "LICENSE_ENABLED", True, raising=False)
    monkeypatch.setattr(
        "backend.core.machine.get_machine_code",
        lambda: "HW-TESTFIXED",
        raising=False,
    )
    monkeypatch.setattr(
        "backend.core.machine.machine_code_available",
        lambda: True,
        raising=False,
    )

    from sqlalchemy import create_engine
    from backend.models import Base, get_session, User
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr("backend.models._get_engine", lambda: engine)

    from backend.main import app
    return TestClient(app)


def _reg(c, username="user1", email="u1@x.com", password="Passw0rd!"):
    return c.post("/api/auth/register", json={
        "username": username, "email": email, "password": password,
    })


class TestSingleAccountPerMachine:
    def test_first_register_binds_machine(self, client):
        c = client
        r = _reg(c)
        assert r.status_code == 200, r.text
        # 账号应绑定固定机器码
        from backend.models import get_session, User
        s = get_session()
        try:
            u = s.query(User).filter_by(email="u1@x.com").first()
            assert u.machine_code == "HW-TESTFIXED"
        finally:
            s.close()

    def test_second_register_on_same_machine_rejected(self, client):
        c = client
        assert _reg(c).status_code == 200
        r = _reg(c, username="user2", email="u2@x.com")
        assert r.status_code == 403
        assert "已绑定" in r.json()["detail"]

    def test_register_different_machine_allowed(self, client, monkeypatch):
        # 机器码采集器改为返回另一台机的码
        monkeypatch.setattr("backend.core.machine.get_machine_code",
                            lambda: "HW-OTHER")
        c = client
        assert _reg(c).status_code == 200


class TestLoginMachineCheck:
    def _mk_user(self, client, machine_code):
        from backend.models import get_session, User
        r = _reg(client, username="machine_check", email="mc@x.com")
        assert r.status_code == 200
        s = get_session()
        try:
            u = s.query(User).filter_by(email="mc@x.com").first()
            u.machine_code = machine_code
            s.commit()
        finally:
            s.close()

    def _login(self, c, email="mc@x.com", pw="Passw0rd!"):
        return c.post("/api/auth/login", json={"email": email, "password": pw})

    def test_login_mismatch_locked(self, client):
        # 账号绑定另一台机器, 当前机器码不一致 → 锁定 423
        self._mk_user(client, "HW-OTHERMACHINE")
        c = client
        r = self._login(c)
        assert r.status_code == 423
        assert "不匹配" in r.json()["detail"] or "锁定" in r.json()["detail"]

    def test_login_match_ok(self, client):
        # 账号绑定当前机器码一致 → 成功
        self._mk_user(client, "HW-TESTFIXED")
        c = client
        r = self._login(c)
        assert r.status_code == 200
        assert r.json()["access_token"]

    def test_login_auto_binds_on_first(self, client):
        # 账号尚未绑机器(手动清除), 登录成功后自动绑定当前机
        self._mk_user(client, None)
        c = client
        assert self._login(c).status_code == 200
        from backend.models import get_session, User
        s = get_session()
        try:
            u = s.query(User).filter_by(email="mc@x.com").first()
            assert u.machine_code == "HW-TESTFIXED"
        finally:
            s.close()


class TestRemoteLock:
    def test_license_locked_blocks_login(self, client):
        _reg(client, username="lockeduser", email="lu@x.com")
        from backend.models import get_session, User
        s = get_session()
        try:
            u = s.query(User).filter_by(email="lu@x.com").first()
            u.license_locked = True
            s.commit()
        finally:
            s.close()
        r = client.post("/api/auth/login", json={
            "email": "lu@x.com", "password": "Passw0rd!"})
        assert r.status_code == 423

    def test_license_locked_blocks_api(self, client):
        reg = _reg(client, username="apiuser", email="au@x.com")
        api_key = reg.json()["api_key"]
        from backend.models import get_session, User
        s = get_session()
        try:
            u = s.query(User).filter_by(email="au@x.com").first()
            u.license_locked = True
            s.commit()
        finally:
            s.close()
        # 访问受保护接口应 423
        r = client.get("/api/license/machine",
                       headers={"Authorization": f"Bearer {api_key}"})
        assert r.status_code == 423


class TestLicenseAPI:
    def test_machine_info(self, client):
        _reg(client)
        from backend.models import get_session, User
        s = get_session()
        try:
            u = s.query(User).filter_by(email="u1@x.com").first()
            api_key = u.api_key
        finally:
            s.close()
        r = client.get("/api/license/machine",
                       headers={"Authorization": f"Bearer {api_key}"})
        assert r.status_code == 200
        assert r.json()["machine_code"] == "HW-TESTFIXED"

    def test_status_match(self, client):
        _reg(client)
        from backend.models import get_session, User
        s = get_session()
        try:
            u = s.query(User).filter_by(email="u1@x.com").first()
            api_key = u.api_key
        finally:
            s.close()
        r = client.get("/api/license/status",
                       headers={"Authorization": f"Bearer {api_key}"})
        assert r.status_code == 200
        d = r.json()
        assert d["match"] is True
        assert d["bound"] is True

    def test_unbind_clears(self, client):
        _reg(client, username="ubuser", email="ub@x.com")
        from backend.models import get_session, User
        s = get_session()
        try:
            u = s.query(User).filter_by(email="ub@x.com").first()
            api_key = u.api_key
        finally:
            s.close()
        r = client.post("/api/license/unbind",
                        headers={"Authorization": f"Bearer {api_key}"})
        assert r.status_code == 200
        s = get_session()
        try:
            u = s.query(User).filter_by(email="ub@x.com").first()
            assert u.machine_code is None
        finally:
            s.close()
