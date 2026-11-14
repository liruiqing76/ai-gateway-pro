# ============================================================
# OpenCode Zen 免费模型 自动同步/健康检测 测试
# ============================================================
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from backend.core import zen_sources

ZEN = zen_sources.ZEN_BASE_URL

FREE_MODELS = [
    "deepseek-v4-flash-free",
    "glm-5-free",
    "nemotron-3.5-lightning-free",
    "hy3-free",
]


@pytest.fixture
def setup_db():
    import os
    import backend.models as m
    m._engine = None
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp.name}"
    m.create_tables()
    yield m
    m._engine = None


def make_user(m, username="u1", email="u1@t.com"):
    from backend.models import User
    import backend.auth.security as sec
    s = m.get_session()
    u = User(username=username, email=email, password_hash=sec.hash_password("Aa123456!"),
             api_key=f"sk-test-{username}")
    s.add(u)
    s.commit()
    s.refresh(u)
    uid = u.id
    s.close()
    return uid


def test_fetch_zen_free_models_parses(monkeypatch):
    """从 zen 上游 /v1/models 目录提取候选: -free 后缀 + 官方白名单(big-pickle); 付费绝不收录。"""
    payload = {"object": "list", "data": [
        {"id": "deepseek-v4-flash-free", "object": "model"},
        {"id": "hy3-free", "object": "model"},
        {"id": "glm-5", "object": "model"},
        {"id": "claude-opus-5", "object": "model"},
        {"id": "big-pickle", "object": "model"},    # 官方免费模型白名单, 必须收
    ]}
    _patch_zen_get(monkeypatch, lambda *a, **k: _FakeResp(payload))
    got = zen_sources.fetch_zen_free_models()
    assert got == ["big-pickle", "deepseek-v4-flash-free", "hy3-free"]
    assert "glm-5" not in got and "claude-opus-5" not in got


def test_fetch_zen_free_models_upstream_down(monkeypatch):
    """上游目录拉取失败 → 空列表, 不抛异常(交由探测全灭保护兜底)。"""
    def boom(*a, **k):
        raise RuntimeError("connection refused")
    _patch_zen_get(monkeypatch, boom)
    assert zen_sources.fetch_zen_free_models() == []


def _patch_zen_get(monkeypatch, get_fn):
    """zen 拉目录走 upstream_http.make_sync 工厂 → 打这个缝。"""
    from backend.core import upstream_http

    class _Ctx:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k): return get_fn(*a, **k)

    monkeypatch.setattr(upstream_http, "make_sync", lambda **kw: _Ctx())


class _FakeResp:
    def __init__(self, data): self._d = data
    def raise_for_status(self): pass
    def json(self): return self._d


class _FakeProbe:
    """probe_zen_model 的假实现: 指定模型可用, 其余不可用。"""
    def __init__(self, ok_models, fail_reason="401 not supported"):
        self.ok = set(ok_models)
        self.reason = fail_reason
    def __call__(self, model_id, timeout=25.0):
        return (True, "ok") if model_id in self.ok else (False, self.reason)


def test_sync_creates_provider_with_usable_only(setup_db, monkeypatch):
    from backend.models import Provider
    make_user(setup_db)
    monkeypatch.setattr(zen_sources, "fetch_zen_free_models", lambda: FREE_MODELS)
    monkeypatch.setattr(zen_sources, "probe_zen_model", _FakeProbe(["nemotron-3.5-lightning-free", "hy3-free"]))

    r = zen_sources.sync_zen_free_models()
    assert r["ok"] is True
    assert r["usable"] == ["nemotron-3.5-lightning-free", "hy3-free"]
    assert "deepseek-v4-flash-free" in r["unusable"]

    m = setup_db
    s = m.get_session()
    prov = s.query(Provider).filter_by(base_url=ZEN).first()
    assert prov is not None
    assert prov.api_key_encrypted == ""       # 免 key
    assert prov.models == ["nemotron-3.5-lightning-free", "hy3-free"]
    from backend.models import Router
    r = s.query(Router).filter_by(name="opencode").first()
    assert r is not None                       # 自动维护路由组
    assert r.models == ["opencode-nemotron-3.5-lightning-free", "opencode-hy3-free"]
    s.close()


def test_sync_lane_closed_keeps_models(setup_db, monkeypatch):
    """全部 403 FreeTierError = 车道对当前出口关闭(出口属性, 非模型裁决):
    对齐插件 probe.js 的 unknown 语义 —— 模型全部保留, lane_state=closed。"""
    from backend.models import Provider
    make_user(setup_db)
    monkeypatch.setattr(zen_sources, "fetch_zen_free_models", lambda: FREE_MODELS)
    monkeypatch.setattr(zen_sources, "probe_zen_model", _FakeProbe(
        [], fail_reason='403 {"type":"error","error":{"type":"FreeTierError",'
                        '"message":"OpenCode\'s free tier can only be used"}}'))
    r = zen_sources.sync_zen_free_models()
    assert r["ok"] is True
    assert r["lane_state"] == "closed"
    assert r["usable"] == FREE_MODELS          # 一个不踢
    assert r["unusable"] == {}
    s = setup_db.get_session()
    prov = s.query(Provider).filter_by(base_url=ZEN).first()
    assert prov.models == FREE_MODELS
    s.close()


def test_health_lane_closed_keeps_models(setup_db, monkeypatch):
    """健康检测遇车道关闭(全 403 FreeTier/Region) → 不累计失败不清理;
    真下架(400 named)仍按阈值移除。"""
    make_user(setup_db)
    monkeypatch.setattr(zen_sources, "fetch_zen_free_models", lambda: FREE_MODELS)
    monkeypatch.setattr(zen_sources, "probe_zen_model", _FakeProbe(FREE_MODELS))
    zen_sources.sync_zen_free_models()

    # 车道整体关闭: 全部 403 FreeTierError → 保留
    monkeypatch.setattr(zen_sources, "probe_zen_model", _FakeProbe(
        [], fail_reason='403 {"error":{"type":"FreeTierError"}}'))
    for _ in range(5):   # 远超失败阈值也不掉
        r = zen_sources.health_check_zen_models()
        assert r["removed"] == []

    # 点名不可用(非出口属性) → 按阈值(默认2次)移除
    monkeypatch.setattr(zen_sources, "probe_zen_model", _FakeProbe(
        ["hy3-free"], fail_reason="400 model not supported"))
    r = zen_sources.health_check_zen_models()      # 第1次失败: 未达阈值
    assert r["removed"] == []
    r = zen_sources.health_check_zen_models()      # 第2次: 达阈值移除
    assert set(r["removed"]) == {"deepseek-v4-flash-free", "glm-5-free",
                                 "nemotron-3.5-lightning-free"}


def test_health_removes_after_fail_limit(setup_db, monkeypatch):
    from backend.models import Provider
    make_user(setup_db)
    monkeypatch.setattr(zen_sources, "fetch_zen_free_models", lambda: FREE_MODELS)
    monkeypatch.setattr(zen_sources, "probe_zen_model", _FakeProbe(FREE_MODELS))
    zen_sources.sync_zen_free_models()

    # 第一次: 全部可用
    monkeypatch.setattr(zen_sources, "probe_zen_model", _FakeProbe(FREE_MODELS))
    r = zen_sources.health_check_zen_models()
    assert r["removed"] == []

    # 第二次: deepseek/glm 失败(连续1) → 未达阈值不移除
    monkeypatch.setattr(zen_sources, "probe_zen_model",
                        _FakeProbe(["nemotron-3.5-lightning-free", "hy3-free"]))
    r = zen_sources.health_check_zen_models()
    assert r["removed"] == []

    # 第三次: 连续第2次失败 → 移除
    monkeypatch.setattr(zen_sources, "probe_zen_model",
                        _FakeProbe(["nemotron-3.5-lightning-free", "hy3-free"]))
    r = zen_sources.health_check_zen_models()
    assert set(r["removed"]) == {"deepseek-v4-flash-free", "glm-5-free"}

    m = setup_db
    s = m.get_session()
    prov = s.query(Provider).filter_by(base_url=ZEN).first()
    assert prov.models == ["nemotron-3.5-lightning-free", "hy3-free"]
    s.close()


def test_health_all_dead_keeps_models(setup_db, monkeypatch):
    """全灭保护: 全部失败时不清理, 防网络整体故障误杀。"""
    from backend.models import Provider
    make_user(setup_db)
    monkeypatch.setattr(zen_sources, "fetch_zen_free_models", lambda: FREE_MODELS)
    monkeypatch.setattr(zen_sources, "probe_zen_model", _FakeProbe(FREE_MODELS))
    zen_sources.sync_zen_free_models()

    monkeypatch.setattr(zen_sources, "probe_zen_model", _FakeProbe([], "500 upstream down"))
    zen_sources.sync_zen_free_models()  # 重置 fail_count? 同步会清零
    r = zen_sources.health_check_zen_models()
    assert r["removed"] == []

    m = setup_db
    s = m.get_session()
    prov = s.query(Provider).filter_by(base_url=ZEN).first()
    assert len(prov.models) == len(FREE_MODELS)
    s.close()


def test_sync_keeps_429_quota_models(setup_db, monkeypatch):
    """429(免费额度当日用尽)模型保留在渠道, 不被当死模型排除。"""
    from backend.models import Provider

    class _QuotaProbe:
        """200 或 429 都算可用; 其余算死。"""
        def __call__(self, model_id, timeout=25.0):
            if model_id == "mimo-v2.5-free":
                return (False, "429 FreeUsageLimitError rate limited")
            if model_id == "glm-5":
                return (False, "401 Missing API key")
            return (True, "ok")

    make_user(setup_db)
    monkeypatch.setattr(zen_sources, "fetch_zen_free_models",
                        lambda: ["nemotron-3.5-lightning-free", "mimo-v2.5-free", "glm-5"])
    monkeypatch.setattr(zen_sources, "probe_zen_model", _QuotaProbe())

    r = zen_sources.sync_zen_free_models()
    assert "mimo-v2.5-free" in r["usable"]          # 429 额度型: 保留
    assert "glm-5" in r["unusable"]                 # 401 付费档: 排除

    m = setup_db
    s = m.get_session()
    prov = s.query(Provider).filter_by(base_url=ZEN).first()
    assert "mimo-v2.5-free" in prov.models
    s.close()


def test_health_429_not_counted_as_failure(setup_db, monkeypatch):
    """健康检测: 429 额度限制不计连续失败, 模型不会被踢出。"""
    from backend.models import Provider
    make_user(setup_db)
    monkeypatch.setattr(zen_sources, "fetch_zen_free_models", lambda: FREE_MODELS)
    monkeypatch.setattr(zen_sources, "probe_zen_model", _FakeProbe(FREE_MODELS))
    zen_sources.sync_zen_free_models()

    # 多轮持续 429(额度整天用尽), 连续失败计数不得累计 → 永不移除
    for _ in range(3):
        monkeypatch.setattr(zen_sources, "probe_zen_model",
                            _FakeProbe(["nemotron-3.5-lightning-free"], "429 FreeUsageLimitError"))
        r = zen_sources.health_check_zen_models()
        assert r["removed"] == []

    m = setup_db
    s = m.get_session()
    prov = s.query(Provider).filter_by(base_url=ZEN).first()
    assert set(prov.models) == set(FREE_MODELS)
    s.close()


def test_sync_skips_unusable_models(setup_db, monkeypatch):
    """非免费档/死模型不进入渠道(旧行为回归保护: 401 付费档排除)。"""
    from backend.models import Provider
    make_user(setup_db)
    monkeypatch.setattr(zen_sources, "fetch_zen_free_models", lambda: FREE_MODELS)
    monkeypatch.setattr(zen_sources, "probe_zen_model",
                        _FakeProbe(FREE_MODELS, "401 Missing API key"))
    r = zen_sources.sync_zen_free_models()
    assert set(r["usable"]) == set(FREE_MODELS)
    assert set(r["unusable"]) == set()