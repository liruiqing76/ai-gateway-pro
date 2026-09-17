# ============================================================
# b.ai 免费模型 自动同步/健康检测 测试
# ============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from backend.core import bai_sources

BAI = bai_sources.B_AI_BASE_URL

FREE_MODELS = [
    "deepseek-v4-flash-free",
    "glm-5-free",
    "nemotron-3.5-sprout",
    "hy3-lite",
]


@pytest.fixture
def setup_db():
    import os
    import tempfile
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


class _FakeResp:
    def __init__(self, data):
        self._d = data
    def raise_for_status(self):
        pass
    def json(self):
        return self._d


def test_fetch_parses_models_dir(monkeypatch):
    """从 /v1/models 目录解析候选(OpenAI schema 与裸列表都支持)。"""
    payload = {"object": "list", "data": [
        {"id": "deepseek-v4-flash-free", "object": "model"},
        {"id": "hy3-lite", "object": "model"},
        {"id": "claude-opus-5", "object": "model"},
    ]}
    monkeypatch.setattr(bai_sources.httpx, "get", lambda *a, **k: _FakeResp(payload))
    got = bai_sources.fetch_bai_free_models()
    assert "deepseek-v4-flash-free" in got and "hy3-lite" in got
    assert "claude-opus-5" in got  # 未知定价 → 全部候选, 交给探测判定


def test_fetch_upstream_down_uses_configured_ids(setup_db, monkeypatch):
    """目录拉取失败 → 空列表, 不抛异常; 配置兜底列表可补位。"""
    def boom(*a, **k):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(bai_sources.httpx, "get", boom)
    assert bai_sources.fetch_bai_free_models() == []

    import backend.config as cfg
    monkeypatch.setattr(cfg.settings, "B_AI_FREE_MODEL_IDS", ["model-a-free", "model-b"])
    assert bai_sources.fetch_bai_free_models() == ["model-a-free", "model-b"]


class _FakeProbe:
    """probe_bai_model 假实现: 指定模型可用, 其余不可用。"""
    def __init__(self, ok_models, fail_reason="401 Missing API key"):
        self.ok = set(ok_models)
        self.reason = fail_reason
    def __call__(self, model_id, timeout=25.0):
        return (True, "ok") if model_id in self.ok else (False, self.reason)


def test_sync_creates_provider_with_usable_only(setup_db, monkeypatch):
    from backend.models import Provider
    make_user(setup_db)
    monkeypatch.setattr(bai_sources, "fetch_bai_free_models", lambda: FREE_MODELS)
    monkeypatch.setattr(bai_sources, "probe_bai_model", _FakeProbe(["nemotron-3.5-sprout", "hy3-lite"]))

    r = bai_sources.sync_bai_free_models()
    assert r["ok"] is True
    assert r["usable"] == ["nemotron-3.5-sprout", "hy3-lite"]
    assert "deepseek-v4-flash-free" in r["unusable"]

    m = setup_db
    s = m.get_session()
    prov = s.query(Provider).filter_by(base_url=BAI).first()
    assert prov is not None
    assert prov.api_key_encrypted == ""       # 匿名免 key
    assert prov.models == ["nemotron-3.5-sprout", "hy3-lite"]
    from backend.models import Router
    r = s.query(Router).filter_by(name="biai").first()
    assert r is not None                       # 自动维护路由组
    assert r.models == ["biai-nemotron-3.5-sprout", "biai-hy3-lite"]
    s.close()


def test_sync_all_dead_keeps_models(setup_db, monkeypatch):
    """全灭保护: 全部探测失败时不覆盖现有渠道。"""
    from backend.models import Provider
    make_user(setup_db)
    monkeypatch.setattr(bai_sources, "fetch_bai_free_models", lambda: FREE_MODELS)
    monkeypatch.setattr(bai_sources, "probe_bai_model", _FakeProbe(FREE_MODELS))
    bai_sources.sync_bai_free_models()

    monkeypatch.setattr(bai_sources, "probe_bai_model", _FakeProbe([], "500 upstream down"))
    r = bai_sources.sync_bai_free_models()
    assert r["ok"] is True
    assert r["usable"] == []
    assert r["skipped"]

    m = setup_db
    s = m.get_session()
    prov = s.query(Provider).filter_by(base_url=BAI).first()
    assert len(prov.models) == len(FREE_MODELS)   # 保留原样
    s.close()


def test_sync_keeps_429_quota_models(setup_db, monkeypatch):
    """429(免费额度当日用尽)模型保留在渠道, 不被当死模型排除。"""
    from backend.models import Provider

    class _QuotaProbe:
        def __call__(self, model_id, timeout=25.0):
            if model_id == "mimo-v2.5-free":
                return (False, "429 FreeUsageLimitError rate limited")
            if model_id == "glm-5":
                return (False, "401 Missing API key")
            return (True, "ok")

    make_user(setup_db)
    monkeypatch.setattr(bai_sources, "fetch_bai_free_models",
                        lambda: ["nemotron-3.5-sprout", "mimo-v2.5-free", "glm-5"])
    monkeypatch.setattr(bai_sources, "probe_bai_model", _QuotaProbe())

    r = bai_sources.sync_bai_free_models()
    assert "mimo-v2.5-free" in r["usable"]          # 429 额度型: 保留
    assert "glm-5" in r["unusable"]                 # 401: 排除

    m = setup_db
    s = m.get_session()
    prov = s.query(Provider).filter_by(base_url=BAI).first()
    assert "mimo-v2.5-free" in prov.models
    s.close()


def test_health_removes_after_fail_limit(setup_db, monkeypatch):
    from backend.models import Provider
    make_user(setup_db)
    monkeypatch.setattr(bai_sources, "fetch_bai_free_models", lambda: FREE_MODELS)
    monkeypatch.setattr(bai_sources, "probe_bai_model", _FakeProbe(FREE_MODELS))
    bai_sources.sync_bai_free_models()

    monkeypatch.setattr(bai_sources, "probe_bai_model", _FakeProbe(FREE_MODELS))
    assert bai_sources.health_check_bai_models()["removed"] == []

    # 连续 2 次失败 → 达到阈值(2) → 移除
    monkeypatch.setattr(bai_sources, "probe_bai_model",
                        _FakeProbe(["nemotron-3.5-sprout", "hy3-lite"]))
    assert bai_sources.health_check_bai_models()["removed"] == []
    monkeypatch.setattr(bai_sources, "probe_bai_model",
                        _FakeProbe(["nemotron-3.5-sprout", "hy3-lite"]))
    r = bai_sources.health_check_bai_models()
    assert set(r["removed"]) == {"deepseek-v4-flash-free", "glm-5-free"}

    m = setup_db
    s = m.get_session()
    prov = s.query(Provider).filter_by(base_url=BAI).first()
    assert prov.models == ["nemotron-3.5-sprout", "hy3-lite"]
    s.close()


def test_health_all_dead_keeps_models(setup_db, monkeypatch):
    """健康检测全灭保护: 全部失败时不清理, 防网络整体故障误杀。"""
    from backend.models import Provider
    make_user(setup_db)
    monkeypatch.setattr(bai_sources, "fetch_bai_free_models", lambda: FREE_MODELS)
    monkeypatch.setattr(bai_sources, "probe_bai_model", _FakeProbe(FREE_MODELS))
    bai_sources.sync_bai_free_models()

    monkeypatch.setattr(bai_sources, "probe_bai_model",
                        _FakeProbe([], "500 upstream down"))
    r = bai_sources.health_check_bai_models()
    assert r["removed"] == []

    m = setup_db
    s = m.get_session()
    prov = s.query(Provider).filter_by(base_url=BAI).first()
    assert len(prov.models) == len(FREE_MODELS)
    s.close()


# ============================================================
# 真实 HTTP 端到端: 本地 mock 一个 OpenAI 兼容服务,
# 验证 目录拉取 → 真实探测 → 渠道落地 全链路走通
# ============================================================
def test_e2e_real_http_sync(setup_db, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    import json as _json

    USABLE = {"free-a", "free-b"}
    QUOTA = {"free-quota"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def _send(self, code, body):
            data = _json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            if self.path.endswith("/models"):
                self._send(200, {"object": "list", "data": [
                    {"id": m, "object": "model"} for m in sorted(USABLE | QUOTA | {"paid-x"})
                ]})
            else:
                self._send(404, {"error": "not found"})
        def do_POST(self):
            if not self.path.endswith("/chat/completions"):
                return self._send(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length", 0))
            body = _json.loads(self.rfile.read(length) or b"{}")
            mid = (body or {}).get("model", "")
            if mid in USABLE:
                self._send(200, {"choices": [{"message": {"content": "ok"}}]})
            elif mid in QUOTA:
                self._send(429, {"error": {"code": "FreeUsageLimitError", "message": "rate limited"}})
            else:
                self._send(401, {"error": {"message": "Missing API key"}})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}/v1"
    monkeypatch.setattr(bai_sources, "B_AI_BASE_URL", base)
    monkeypatch.setattr(bai_sources, "B_AI_MODELS_URL", base + "/models")
    monkeypatch.setattr(bai_sources, "B_AI_CHAT_URL", base + "/chat/completions")
    monkeypatch.setattr(bai_sources, "B_AI_API_KEY", "")
    try:
        make_user(setup_db)
        r = bai_sources.sync_bai_free_models(probe_timeout=5.0)
        assert r["ok"] is True
        assert set(r["usable"]) == USABLE | QUOTA     # 200 + 429 都入渠道
        assert "paid-x" in r["unusable"]              # 401 付费档剔除

        m = setup_db
        s = m.get_session()
        from backend.models import Provider, Router
        prov = s.query(Provider).filter_by(base_url=base).first()
        assert prov is not None and set(prov.models) == USABLE | QUOTA
        rt = s.query(Router).filter_by(name="biai").first()
        assert rt is not None and len(rt.models) == len(USABLE | QUOTA)
        s.close()

        # 健康检测: 让 free-b 与 free-quota 挂掉并达阈值 → 移出
        def probe_hard(model_id, timeout=25.0):
            if model_id == "free-a":
                return (True, "ok")
            if model_id == "free-quota":
                return (False, "429 FreeUsageLimitError")
            return (False, "500 upstream down")
        monkeypatch.setattr(bai_sources, "probe_bai_model", probe_hard)
        bai_sources.health_check_bai_models()
        monkeypatch.setattr(bai_sources, "probe_bai_model", probe_hard)
        r2 = bai_sources.health_check_bai_models()
        assert set(r2["removed"]) == {"free-b"}
        # 429 免费额度型从未被计数失败
        s = m.get_session()
        prov = s.query(Provider).filter_by(base_url=base).first()
        assert set(prov.models) == {"free-a", "free-quota"}
        s.close()
    finally:
        srv.shutdown()
        srv.server_close()