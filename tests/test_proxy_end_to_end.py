# ============================================================
# 代理链路端到端测试
# 用 mock 替换转发函数, 验证: 故障转移 / 别名解析 / trace 落库 / 用量记录
# ============================================================
import sys, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离 DB + 预置用户/渠道"""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("GW_DB_PATH", str(db_path))

    from sqlalchemy import create_engine
    from backend.models import Base, get_session, User, Provider, Router, ModelAlias
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr("backend.models._get_engine", lambda: engine)

    s = get_session()
    from backend.auth.security import encrypt_secret
    import bcrypt
    pw_hash = bcrypt.hashpw(b"Test1234!", bcrypt.gensalt()).decode()
    u = User(
        username="e2e", email="e2e@x.com", password_hash=pw_hash,
        tier="free", api_key="sk-e2e", quota_daily=1000,
    )
    s.add(u)
    s.commit()

    # 两个渠道: 渠道A 与 渠道B(应急)
    pa = Provider(
        user_id=u.id, name="ProviderA", base_url="https://a.example/v1",
        api_key_encrypted=encrypt_secret("key-a"),
        models=["model-a1", "model-a2"],
        disabled_models="[]", enabled=1,
    )
    pb = Provider(
        user_id=u.id, name="ProviderB", base_url="https://b.example/v1",
        api_key_encrypted=encrypt_secret("key-b"),
        models=["model-b1"],
        disabled_models="[]", enabled=1,
    )
    s.add_all([pa, pb])
    s.commit()

    # 路由组: group1 = [a1, b1], group2 = [a2]
    s.add_all([
        Router(user_id=u.id, name="group1", models=["model-a1", "model-b1"], strategy="quality"),
        Router(user_id=u.id, name="group2", models=["model-a2"], strategy="quality"),
        ModelAlias(user_id=u.id, alias="gpt-4", target="group1"),
    ])
    s.commit()
    s.close()

    from backend.main import app
    client = TestClient(app)
    # 清空质量分与熔断状态, 保证从干净状态开始
    from backend.core.scheduler import quality_tracker
    from backend.core.circuit import circuit_breaker
    quality_tracker.reset_all()
    circuit_breaker._states.clear()
    yield client, {"api_key": "sk-e2e", "db_path": db_path, "u": u}


def ok_resp(text="hello", model="ProviderA · model-a1"):
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
    }


def sse_chunks(text_chunks):
    """构造假 SSE 生成器"""
    async def gen():
        for chunk in text_chunks:
            yield json.dumps({
                "id": "chatcmpl-x",
                "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
            }, ensure_ascii=False).encode()
        yield b"data: [DONE]\n\n"
    return gen()


class TestFailoverE2E:
    """自动故障转移: 第一候选失败自动切第二个"""

    def test_non_stream_fallback_to_second(self, env, monkeypatch):
        client, ctx = env
        from backend import api
        import backend.api.proxy as proxy_mod

        call_order = []

        async def fake_forward(http_client, provider, api_key, model, body):
            key = f"{provider.name}|{model}"
            call_order.append(key)
            if model == "model-a1":
                return None, "upstream 500"   # 渠道A 第一个模型失败
            if model == "model-b1":
                return ok_resp(text="from B", model="ProviderB · model-b1"), None
            return ok_resp(), None

        monkeypatch.setattr(proxy_mod, "forward_non_stream", fake_forward)

        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-e2e"},
            json={"model": "group1", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        body = resp.json()
        # 失败后成功切到第二个候选
        assert "from B" in body["choices"][0]["message"]["content"]
        assert "ProviderA|model-a1" in call_order
        assert "ProviderB|model-b1" in call_order
        # model 字段带渠道标记
        assert "ProviderB" in body["model"]

    def test_all_fail_returns_502_with_trace(self, env, monkeypatch):
        client, ctx = env
        import backend.api.proxy as proxy_mod

        async def fake_forward(http_client, provider, api_key, model, body):
            return None, "upstream 500"

        monkeypatch.setattr(proxy_mod, "forward_non_stream", fake_forward)

        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-e2e"},
            json={"model": "group1", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 502

        # trace 落库且标记失败
        r = client.get("/api/traces?success=false", headers={"Authorization": "Bearer sk-e2e"})
        assert r.status_code == 200
        assert r.json()["total"] >= 1


class TestAliasE2E:
    """模型别名在代理请求中的解析"""

    def test_alias_routes_to_target_group(self, env, monkeypatch):
        client, ctx = env
        import backend.api.proxy as proxy_mod

        used_models = []

        async def fake_forward(http_client, provider, api_key, model, body):
            used_models.append(f"{provider.name}|{model}")
            return ok_resp(), None

        monkeypatch.setattr(proxy_mod, "forward_non_stream", fake_forward)

        # gpt-4 → group1 → [model-a1, model-b1] 按 quality 排序后 a1 优先
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-e2e"},
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        assert "ProviderA|model-a1" in used_models

    def test_alias_response_header_matches(self, env, monkeypatch):
        """解析后实际渠道应在响应中体现"""
        client, ctx = env
        import backend.api.proxy as proxy_mod

        async def fake_forward(http_client, provider, api_key, model, body):
            return ok_resp(), None

        monkeypatch.setattr(proxy_mod, "forward_non_stream", fake_forward)

        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-e2e"},
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.json()["model"].startswith("ProviderA")


class TestStreamE2E:
    """流式请求 + 流式故障转移"""

    def test_stream_success(self, env, monkeypatch):
        client, ctx = env
        import backend.api.proxy as proxy_mod

        async def fake_stream(http_client, provider, api_key, model, body):
            return sse_chunks(["你", "好"]), None

        monkeypatch.setattr(proxy_mod, "forward_stream", fake_stream)

        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-e2e"},
            json={"model": "group1", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        assert resp.headers.get("content-type", "").startswith("text/event-stream")
        assert "X-Provider" in resp.headers
        text = resp.text
        assert "你" in text and "好" in text
        assert "DONE" in text

    def test_stream_fallback_to_next_candidate(self, env, monkeypatch):
        client, ctx = env
        import backend.api.proxy as proxy_mod

        async def fake_stream(http_client, provider, api_key, model, body):
            if model == "model-a1":
                return None, "upstream 500"   # 流式第一个候选失败
            return sse_chunks(["fallback"]), None

        monkeypatch.setattr(proxy_mod, "forward_stream", fake_stream)

        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-e2e"},
            json={"model": "group1", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        assert "fallback" in resp.text
        assert resp.headers["X-Provider"] == "ProviderB"  # 实际用的是第二个渠道


class TestTraceRecording:
    """请求链路记录与统计"""

    def test_trace_fields_recorded(self, env, monkeypatch):
        client, ctx = env
        import backend.api.proxy as proxy_mod

        async def fake_forward(http_client, provider, api_key, model, body):
            return ok_resp(), None

        monkeypatch.setattr(proxy_mod, "forward_non_stream", fake_forward)

        client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-e2e"},
            json={"model": "group1", "messages": [{"role": "user", "content": "hi"}]},
        )

        resp = client.get("/api/traces", headers={"Authorization": "Bearer sk-e2e"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) >= 1
        trace = data[0]
        assert trace["original_model"] == "group1"
        assert trace["success"] is True
        assert trace["provider_name"] == "ProviderA"
        assert trace["model_name"] == "model-a1"
        assert trace["selection_strategy"] == "quality"
        assert trace["total_tokens"] == 12

    def test_stats_aggregates(self, env, monkeypatch):
        client, ctx = env
        import backend.api.proxy as proxy_mod
        from backend.config import settings

        # group1 有 a1/b1 两个候选, 轮询会让 3 次请求轮转到不同模型;
        # 本测试专注统计聚合, 关掉轮询让 3 次都落在 model-a1
        monkeypatch.setattr(settings, "ROUND_ROBIN_ENABLED", False)

        async def fake_forward(http_client, provider, api_key, model, body):
            return ok_resp(), None

        monkeypatch.setattr(proxy_mod, "forward_non_stream", fake_forward)
        for _ in range(3):
            client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer sk-e2e"},
                json={"model": "group1", "messages": [{"role": "user", "content": "hi"}]},
            )

        resp = client.get("/api/traces/stats?days=7", headers={"Authorization": "Bearer sk-e2e"})
        assert resp.status_code == 200
        stats = resp.json()
        assert stats["total_requests"] == 3
        assert stats["success_count"] == 3
        assert stats["success_rate"] == 100.0
        assert stats["avg_latency_ms"] >= 0
        assert stats["top_models"][0]["model"] == "model-a1"
        assert stats["top_models"][0]["count"] == 3


class TestProxyModels:
    """GET /v1/models — 模型列表(路由组 + 渠道模型)"""

    def test_lists_router_groups_and_models(self, env):
        client, ctx = env
        resp = client.get("/v1/models", headers={"Authorization": "Bearer sk-e2e"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "list"

        ids = {m["id"] for m in body["data"]}
        # 路由组作为模型
        assert "group1" in ids
        assert "group2" in ids
        # 渠道模型带前缀
        assert "ProviderA-model-a1" in ids
        assert "ProviderA-model-a2" in ids
        assert "ProviderB-model-b1" in ids
        # 全都有 OpenAI 兼容结构
        for m in body["data"]:
            assert m["object"] == "model"
            assert m["created"] == 0

    def test_disabled_models_excluded(self, env):
        client, ctx = env
        from backend.models import get_session, Provider
        s = get_session()
        try:
            p = s.query(Provider).filter_by(name="ProviderA").first()
            p.disabled_models = '["model-a2"]'
            s.commit()
        finally:
            s.close()

        resp = client.get("/v1/models", headers={"Authorization": "Bearer sk-e2e"})
        ids = {m["id"] for m in resp.json()["data"]}
        assert "ProviderA-model-a1" in ids
        assert "ProviderA-model-a2" not in ids

    def test_requires_auth(self, env):
        client, ctx = env
        resp = client.get("/v1/models")
        assert resp.status_code == 401

    def test_includes_context_length(self, env):
        client, ctx = env
        resp = client.get("/v1/models", headers={"Authorization": "Bearer sk-e2e"})
        by_id = {m["id"]: m for m in resp.json()["data"]}
        # 默认上下文长度 32768(未在 CONTEXT_LIMITS 中的模型)
        assert by_id["ProviderA-model-a1"]["context_length"] == 32768


class TestPipelineE2E:
    """编排流水线(relay/review)走 HTTP API 的整链路"""

    def test_relay_non_stream_success(self, env, monkeypatch):
        client, ctx = env
        import backend.api.proxy as proxy_mod

        async def fake_forward(http_client, provider, api_key, model, body):
            # 两级 relay: 每级返回一段内容
            texts = {"model-a1": "第一级", "model-a2": "第二级"}
            m = body.get("model", "")
            return ok_resp(text=texts.get(model, "fallback"), model=f"{provider.name} · {model}"), None

        monkeypatch.setattr(proxy_mod, "forward_non_stream", fake_forward)

        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-e2e"},
            json={"model": "relay:group1/group2", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        body = resp.json()
        # 最后一级输出透传
        assert body["choices"][0]["message"]["content"] == "第二级"

    def test_relay_stream_success(self, env, monkeypatch):
        client, ctx = env
        import backend.api.proxy as proxy_mod

        async def fake_forward(http_client, provider, api_key, model, body):
            return ok_resp(text="初稿", model=f"{provider.name} · {model}"), None

        async def fake_stream(http_client, provider, api_key, model, body):
            return sse_chunks(["流式"]), None

        monkeypatch.setattr(proxy_mod, "forward_non_stream", fake_forward)
        monkeypatch.setattr(proxy_mod, "forward_stream", fake_stream)

        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-e2e"},
            json={"model": "relay:group1/group2", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        assert resp.headers.get("content-type", "").startswith("text/event-stream")
        assert resp.headers["X-Pipeline"] == "relay%3Agroup1%2Fgroup2"  # 响应头 latin-1 安全编码
        assert "流式" in resp.text

    def test_review_stream_success(self, env, monkeypatch):
        client, ctx = env
        import backend.api.proxy as proxy_mod

        async def fake_forward(http_client, provider, api_key, model, body):
            return ok_resp(text="主模型初稿", model=f"{provider.name} · {model}"), None

        async def fake_stream(http_client, provider, api_key, model, body):
            return sse_chunks(["审阅修订"]), None

        monkeypatch.setattr(proxy_mod, "forward_non_stream", fake_forward)
        monkeypatch.setattr(proxy_mod, "forward_stream", fake_stream)

        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-e2e"},
            json={"model": "review:group1/group2", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        assert "审阅修订" in resp.text

    def test_pipeline_missing_user_message_400(self, env, monkeypatch):
        client, ctx = env
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-e2e"},
            json={"model": "relay:group1/group2", "messages": [{"role": "system", "content": "no user"}]},
        )
        assert resp.status_code == 400

    def test_pipeline_all_stages_fail_502(self, env, monkeypatch):
        client, ctx = env
        import backend.api.proxy as proxy_mod

        async def fake_forward(http_client, provider, api_key, model, body):
            return None, "upstream 500"

        monkeypatch.setattr(proxy_mod, "forward_non_stream", fake_forward)

        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-e2e"},
            json={"model": "relay:group1/group2", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 502

    def test_review_non_stream_draft_fallback(self, env, monkeypatch):
        """审阅模型失败时回退返回主模型初稿"""
        client, ctx = env
        import backend.api.proxy as proxy_mod

        async def fake_forward(http_client, provider, api_key, model, body):
            if model == "model-a2":
                return None, "upstream 500"   # 审阅阶段失败
            return ok_resp(text="仅初稿", model=f"{provider.name} · {model}"), None

        monkeypatch.setattr(proxy_mod, "forward_non_stream", fake_forward)

        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-e2e"},
            json={"model": "review:group1/group2", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "仅初稿"


class TestProxyEdgeCases:
    """代理路径的边界分支: 无渠道/无候选/空choices/解密失败/内置路由组"""

    def test_no_providers_returns_503(self, env):
        client, ctx = env
        from backend.models import get_session, Provider
        s = get_session()
        try:
            s.query(Provider).delete()
            s.commit()
        finally:
            s.close()

        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-e2e"},
            json={"model": "group1", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 503

    def test_no_candidates_returns_503(self, env, monkeypatch):
        client, ctx = env
        import backend.api.proxy as proxy_mod

        async def fake_forward(http_client, provider, api_key, model, body):
            return ok_resp(), None

        monkeypatch.setattr(proxy_mod, "forward_non_stream", fake_forward)

        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-e2e"},
            json={"model": "ghost-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 503

    def test_empty_choices_retries_next(self, env, monkeypatch):
        """上游返回 200 但 choices 为空 → 视为失败继续下一候选"""
        client, ctx = env
        import backend.api.proxy as proxy_mod

        async def fake_forward(http_client, provider, api_key, model, body):
            if model == "model-a1":
                return {"choices": [], "usage": {}}, None   # 空 choices
            return ok_resp(text="next", model=f"{provider.name} · {model}"), None

        monkeypatch.setattr(proxy_mod, "forward_non_stream", fake_forward)

        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-e2e"},
            json={"model": "group1", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        assert "next" in resp.json()["choices"][0]["message"]["content"]

    def test_decrypt_failure_skipped(self, env, monkeypatch):
        """API Key 解密失败 → 标记为不可用并跳过该渠道"""
        client, ctx = env
        import backend.api.proxy as proxy_mod
        from backend.models import get_session, Provider

        s = get_session()
        try:
            p = s.query(Provider).filter_by(name="ProviderA").first()
            p.api_key_encrypted = "!!!not-valid!!!"
            s.commit()
        finally:
            s.close()

        used_models = []

        async def fake_forward(http_client, provider, api_key, model, body):
            used_models.append(f"{provider.name}|{model}")
            return ok_resp(model=f"{provider.name} · {model}"), None

        monkeypatch.setattr(proxy_mod, "forward_non_stream", fake_forward)

        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-e2e"},
            json={"model": "group1", "messages": [{"role": "user", "content": "hi"}]},
        )
        # ProviderA 被跳过 → 只会用 ProviderB
        assert resp.status_code == 200
        assert used_models == ["ProviderB|model-b1"]

    def test_stream_orchestration_all_fail_502(self, env, monkeypatch):
        """流式编排所有阶段失败 → 502"""
        client, ctx = env
        import backend.api.proxy as proxy_mod

        async def fake_forward(http_client, provider, api_key, model, body):
            return None, "upstream 500"

        async def fake_stream(http_client, provider, api_key, model, body):
            return None, "upstream 500"

        monkeypatch.setattr(proxy_mod, "forward_non_stream", fake_forward)
        monkeypatch.setattr(proxy_mod, "forward_stream", fake_stream)

        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-e2e"},
            json={"model": "relay:group1/group2", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 502

    def test_auto_1m_router_and_builtin_groups(self, env, monkeypatch):
        """1M 上下文自动路由组 + 无手工路由组时内置三分类"""
        client, ctx = env
        import backend.api.proxy as proxy_mod
        from backend.models import get_session, Provider, Router

        # 删除手工路由组, 启用一个 1M 长上下文模型
        s = get_session()
        try:
            s.query(Router).delete()
            p = s.query(Provider).filter_by(name="ProviderA").first()
            p.models = ["glm-5.2", "qwen-8b-fp8"]
            s.commit()
        finally:
            s.close()

        used_models = []

        async def fake_forward(http_client, provider, api_key, model, body):
            used_models.append(model)
            return ok_resp(model=f"{provider.name} · {model}"), None

        monkeypatch.setattr(proxy_mod, "forward_non_stream", fake_forward)

        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-e2e"},
            json={"model": "auto-router-1m", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        assert "glm-5.2" in used_models

        # 内置三分类: quick 组(经 classify_model) 也能命中
        used_models.clear()
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-e2e"},
            json={"model": "quick", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        assert "qwen-8b-fp8" in used_models


class TestUsageE2E:
    """用量记录与额度扣减"""

    def test_usage_log_written(self, env, monkeypatch):
        client, ctx = env
        import backend.api.proxy as proxy_mod

        async def fake_forward(http_client, provider, api_key, model, body):
            return ok_resp(), None

        monkeypatch.setattr(proxy_mod, "forward_non_stream", fake_forward)

        client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-e2e"},
            json={"model": "group1", "messages": [{"role": "user", "content": "hi"}]},
        )

        # 额度 +1
        from backend.models import get_session, User
        s = get_session()
        try:
            u = s.query(User).filter_by(username="e2e").first()
            assert u.quota_used_today == 1
        finally:
            s.close()

    def test_quota_exceeded_rejected(self, env, monkeypatch):
        client, ctx = env
        from backend.models import get_session, User
        s = get_session()
        try:
            u = s.query(User).filter_by(username="e2e").first()
            u.quota_used_today = u.quota_daily  # 用满额度
            s.commit()
        finally:
            s.close()

        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-e2e"},
            json={"model": "group1", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code in (403, 429)