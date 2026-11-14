# ============================================================
# opencode 免费额度守卫: 定时同步不得对"已知可用"模型重复发真实 chat 请求
# ============================================================
# 背景(2026-10-24 实测踩到): 车道是公共匿名池(无账号, 按出口/会话计免费额度),
# 而原实现每次同步/体检都对渠道里每个模型发一次真实 chat 请求 —— 9 模型 ×
# 每小时体检 = 每天 200+ 次, 用户自己还没用就把额度烧光了。
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from backend.core import zen_sources

ZEN = zen_sources.ZEN_BASE_URL
FREE = ["deepseek-v4-flash-free", "glm-5-free", "nemotron-3.5-lightning-free"]


@pytest.fixture
def setup_db(monkeypatch):
    import backend.models as m
    m._engine = None
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    state = tempfile.mkdtemp(prefix="zenstate_")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp.name}")
    monkeypatch.setenv("GW_STATE_DIR", state)
    m.create_tables()

    from backend.auth import security as sec
    s = m.get_session()
    u = m.User(username="u1", email="u1@t.com",
               password_hash=sec.hash_password("Aa123456!"), api_key="sk-test-u1")
    s.add(u)
    s.commit()
    s.close()
    yield m
    m._engine = None


class _Probe:
    """记录被探测过的模型; 默认全部可用。"""

    def __init__(self, dead=(), reason="ok"):
        self.calls = []
        self.dead = set(dead)
        self.reason = reason

    def __call__(self, model_id, timeout=25.0):
        self.calls.append(model_id)
        if model_id in self.dead:
            return (False, self.reason)
        return (True, "ok")


def _install(monkeypatch, models, probe):
    monkeypatch.setattr(zen_sources, "fetch_zen_free_models", lambda: list(models))
    monkeypatch.setattr(zen_sources, "probe_zen_model", probe)


def test_first_sync_probes_all(setup_db, monkeypatch):
    """首次同步(渠道里什么都没有): 全量探测, 计数如实上报。"""
    probe = _Probe()
    _install(monkeypatch, FREE, probe)
    r = zen_sources.sync_zen_free_models()
    assert r["probed"] == len(FREE)
    assert set(probe.calls) == set(FREE)
    assert set(r["usable"]) == set(FREE)


def test_second_sync_probes_nothing(setup_db, monkeypatch):
    """第二轮同步: 模型全部已知可用 → 一次探测都不发(不烧额度)。"""
    _install(monkeypatch, FREE, _Probe())
    zen_sources.sync_zen_free_models()

    probe = _Probe()
    _install(monkeypatch, FREE, probe)
    r = zen_sources.sync_zen_free_models()
    assert probe.calls == []            # 关键: 零真实请求
    assert r["probed"] == 0
    assert set(r["usable"]) == set(FREE)  # 模型仍然在渠道里


def test_only_new_model_is_probed(setup_db, monkeypatch):
    """上游新增模型时只探新的那一个, 老模型沿用。"""
    _install(monkeypatch, FREE, _Probe())
    zen_sources.sync_zen_free_models()

    new_models = FREE + ["brand-new-free"]
    probe = _Probe()
    _install(monkeypatch, new_models, probe)
    r = zen_sources.sync_zen_free_models()
    assert probe.calls == ["brand-new-free"]
    assert set(r["usable"]) == set(new_models)


def test_new_model_dead_is_rejected(setup_db, monkeypatch):
    """新增模型探测失败(非额度型) → 不进渠道, 老模型不受影响。"""
    _install(monkeypatch, FREE, _Probe())
    zen_sources.sync_zen_free_models()

    probe = _Probe(dead=["broken-free"], reason="404 model not found")
    _install(monkeypatch, FREE + ["broken-free"], probe)
    r = zen_sources.sync_zen_free_models()
    assert "broken-free" in r["unusable"]
    assert "broken-free" not in r["usable"]
    assert set(FREE) <= set(r["usable"])


def test_manual_probe_known_forces_full(setup_db, monkeypatch):
    """手动「立即同步」(probe_known=True): 全量重探, 用于出口恢复后立刻验证。"""
    _install(monkeypatch, FREE, _Probe())
    zen_sources.sync_zen_free_models()

    probe = _Probe()
    _install(monkeypatch, FREE, probe)
    r = zen_sources.sync_zen_free_models(probe_known=True)
    assert set(probe.calls) == set(FREE)
    assert r["probed"] == len(FREE)


def test_lane_state_carried_when_not_probed(setup_db, monkeypatch):
    """本轮零探测时沿用落盘的车道结论, 界面徽标不会凭空消失。"""
    from backend.core import lane_state
    lane_state.save("opencode", "open", synced_at="2026-10-24 20:00:00", usable=3)

    _install(monkeypatch, FREE, _Probe())
    zen_sources.sync_zen_free_models()
    r = zen_sources.sync_zen_free_models()   # 第二轮: 零探测
    assert r["probed"] == 0
    assert r["lane_state"] == "open"


def test_quota_429_known_model_kept(setup_db, monkeypatch):
    """已知模型遇到 429 也不会被踢(额度会重置), 且不计入死模型。"""
    _install(monkeypatch, FREE, _Probe())
    zen_sources.sync_zen_free_models()

    probe = _Probe(reason="429 FreeUsageLimitError rate limited")
    _install(monkeypatch, FREE, probe)
    r = zen_sources.sync_zen_free_models(probe_known=True)
    assert set(r["usable"]) == set(FREE)


def test_health_check_disabled_by_default():
    """默认配置关闭定时体检(否则每小时全量探测烧额度)。"""
    from backend.config import settings
    assert settings.ZEN_HEALTH_CHECK_MINUTES == 0
