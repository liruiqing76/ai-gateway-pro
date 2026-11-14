# ============================================================
# 机器码(HWID)稳定性 + 历史绑定兼容测试
# ============================================================
# 事故背景(2026-10-24): 用户登录报「账号已绑定其他设备, 当前机器与账号不匹配,
# 已被锁定」。根因是旧 machine.py 用 wmic 取"第一行非空内容", Win11 移除 wmic 后
# 走 PowerShell 回退, 取到的字符串不同 → 同一台机器算出不同 HWID。更要命的是
# wmic 时代取到的其实是**表头**(ProcessorId / SerialNumber), 于是老账号绑定的
# 机器码 = hash(主机名 + 表头串), 新代码必须能把这类历史值认出来并静默迁移。
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import hashlib

import pytest

from backend.core import machine


def _digest(parts):
    return "HW-" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:40]


@pytest.fixture
def fake_hw(monkeypatch):
    """固定一套硬件信息, 避免测试依赖本机真实硬件。"""
    def _fake():
        return {
            "host": "LESLIE", "host_raw": "leslie",
            "cpu": ["178BFBFF00A40F41"], "raw_cpu": ["178BFBFF00A40F41"],
            "board": ["ARB20A103204S1492"], "raw_board": ["ARB20A103204S1492"],
            "disk": ["0000-0001"], "raw_disk": ["0000_0001"],
        }
    monkeypatch.setattr(machine, "_collect", _fake)
    return _fake


def test_machine_code_deterministic(fake_hw):
    a = machine.get_machine_code()
    b = machine.get_machine_code()
    assert a == b and a.startswith("HW-") and len(a) == 43


def test_machine_code_order_insensitive(monkeypatch):
    """多盘/多 CPU 时, 分量顺序不影响结果(旧实现取"第一行"会抖)。"""
    def _fake(order):
        def inner():
            return {"host": "LESLIE", "host_raw": "leslie",
                    "cpu": order, "raw_cpu": order,
                    "board": ["B1"], "raw_board": ["B1"],
                    "disk": order, "raw_disk": order}
        return inner

    monkeypatch.setattr(machine, "_collect", _fake(["D1", "D2"]))
    one = machine.get_machine_code()
    monkeypatch.setattr(machine, "_collect", _fake(["D2", "D1", "D1"]))
    assert machine.get_machine_code() == one


def test_candidates_cover_legacy_wmic_header(fake_hw):
    """wmic 时代绑定的机器码(表头串)必须被候选集覆盖。"""
    legacy = _digest(["leslie", "ProcessorId", "SerialNumber", "SerialNumber"])
    assert legacy in machine.machine_code_candidates()


def test_candidates_cover_missing_component(fake_hw):
    """采集抖动导致某分量为空的历史值也要能认出。"""
    partial = _digest(["leslie", "178BFBFF00A40F41", "ARB20A103204S1492", ""])
    assert partial in machine.machine_code_candidates()


def test_candidates_include_canonical(fake_hw):
    assert machine.get_machine_code() in machine.machine_code_candidates()


def test_candidates_never_host_only(fake_hw):
    """不允许"只有主机名"的机器码: 否则同主机名就能顶替设备身份。"""
    assert _digest(["leslie", "", "", ""]) not in machine.machine_code_candidates()


def test_collect_failure_is_graceful(monkeypatch):
    """PowerShell 不可用/超时 → 不抛异常, 退化为无硬件信息。"""
    monkeypatch.setattr(machine, "_run_json", lambda *a, **k: None)
    monkeypatch.setattr(machine.platform, "node", lambda: "leslie")
    code = machine.get_machine_code()
    assert code.startswith("HW-")
    assert machine.machine_code_available() is False


def test_machine_code_available_with_hw(fake_hw):
    assert machine.machine_code_available() is True


# ── 授权校验: 历史机器码命中即迁移, 陌生机器码仍然拒绝 ──
@pytest.fixture
def db(monkeypatch):
    import backend.models as m
    m._engine = None
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp.name}")
    monkeypatch.setenv("GW_STATE_DIR", tempfile.mkdtemp(prefix="lic_"))
    m.create_tables()
    yield m
    m._engine = None


def _mk_user(m, machine_code):
    from backend.auth import security as sec
    s = m.get_session()
    u = m.User(username="lic", email="lic@t.com",
               password_hash=sec.hash_password("Aa123456!"),
               api_key="sk-lic", machine_code=machine_code)
    s.add(u)
    s.commit()
    uid = u.id
    s.close()
    return uid


@pytest.mark.asyncio
async def test_legacy_code_accepted_and_migrated(db, fake_hw):
    from backend.core.license import verify_local_binding
    legacy = _digest(["leslie", "ProcessorId", "SerialNumber", "SerialNumber"])
    uid = _mk_user(db, legacy)
    canonical = machine.get_machine_code()

    r = await verify_local_binding(canonical, "lic@t.com")
    assert r.ok and not r.locked

    s = db.get_session()
    assert s.query(db.User).filter_by(id=uid).first().machine_code == canonical
    s.close()


@pytest.mark.asyncio
async def test_foreign_code_still_rejected(db, fake_hw):
    from backend.core.license import verify_local_binding
    _mk_user(db, "HW-" + "de" * 20)
    r = await verify_local_binding(machine.get_machine_code(), "lic@t.com")
    assert not r.ok and r.locked


@pytest.mark.asyncio
async def test_probe_failure_does_not_lock(db, monkeypatch):
    """硬件探针整体失败时放行, 不把用户锁在门外。"""
    from backend.core.license import verify_local_binding
    _mk_user(db, "HW-" + "ab" * 20)
    monkeypatch.setattr(machine, "_run_json", lambda *a, **k: None)
    monkeypatch.setattr(machine.platform, "node", lambda: "leslie")
    r = await verify_local_binding(machine.get_machine_code(), "lic@t.com")
    assert r.ok and not r.locked
