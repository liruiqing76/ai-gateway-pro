# ============================================================
# Anthropic /v1/messages 端到端测试(带模拟上游)
#
# 用 httpx MockTransport 拦截网关→上游的转发, 在 ASGI 层测:
#   鉴权(x-api-key) → 请求转换 → 候选循环 → 非流式/流式协议转换
#   → Anthropic 错误封套 → count_tokens
# ============================================================

import json
from typing import Awaitable, Callable

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from backend.models import get_session, User


@pytest.fixture(autouse=True)
def app_env(tmp_path, monkeypatch):
    """每测试独立: 临时数据库 + 已知用户 + 一个 OpenAI 兼容上游渠道"""
    db_path = tmp_path / "gw.db"
    from sqlalchemy import create_engine
    import backend.models as models_mod
    from backend.models import Base
    engine = create_engine(f"sqlite:///{db_path.as_posix()}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    # 项目既有测试模式: monkeypatch _get_engine + 重置单例
    monkeypatch.setattr(models_mod, "_engine", None)
    monkeypatch.setattr(models_mod, "_get_engine", lambda: engine)
    monkeypatch.setattr(models_mod, "get_db_url",
                        lambda: f"sqlite:///{db_path.as_posix()}")

    # 建用户 + 渠道
    from backend.auth.security import encrypt_secret
    from backend.core.circuit import circuit_breaker
    from backend.core.scheduler import quality_tracker
    circuit_breaker._states.clear()   # 跨测试重置熔断状态
    s = models_mod.get_session()
    try:
        u = models_mod.User(username="t1", email="t1@test.local", password_hash="x",
                            api_key="sk-gw-test-key", role="user", is_active=True,
                            quota_daily=100, quota_used_today=0)
        s.add(u)
        s.commit()
        s.refresh(u)
        prov = models_mod.Provider(
            user_id=u.id, name="mockprov",
            base_url="http://upstream.test/v1",
            api_key_encrypted=encrypt_secret("up-key"),
            models=["m1", "m2"], disabled_models=[],
            enabled=True,
        )
        s.add(prov)
        s.commit()
        yield {"user_id": u.id}
    finally:
        s.close()


def _build_app(mock_transport):
    """构建带 mock 上游的 app(替换全局 httpx client)"""
    import backend.api.proxy as proxy_mod
    import backend.api.anthropic as anth_mod
    from fastapi import FastAPI
    from fastapi.exceptions import HTTPException

    app = FastAPI()
    app.include_router(anth_mod.router)
    app.add_exception_handler(HTTPException, anth_mod.anthropic_http_exception_handler)

    # 替换共享 http client 为 mock
    mock_client = httpx.AsyncClient(transport=mock_transport, timeout=30)

    async def fake_get_http_client():
        return mock_client

    monkey_holder = {}
    import backend.api.anthropic as am
    am.get_http_client = fake_get_http_client
    return app


UPSTREAM_NONSTREAM = {
    "id": "chatcmpl-1", "object": "chat.completion", "model": "mockprov · m1",
    "choices": [{
        "index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": "Hello from upstream!",
                    "reasoning_content": "thinking hard"},
    }],
    "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
}

UPSTREAM_STREAM_LINES = [
    b'data: {"id":"c1","object":"chat.completion.chunk","model":"m","choices":[{"index":0,"delta":{"reasoning_content":"deep thought"},"finish_reason":null}]}\n\n',
    b'data: {"id":"c1","object":"chat.completion.chunk","model":"m","choices":[{"index":0,"delta":{"content":"Hel"},"finish_reason":null}]}\n\n',
    b'data: {"id":"c1","object":"chat.completion.chunk","model":"m","choices":[{"index":0,"delta":{"content":"lo!"},"finish_reason":null}]}\n\n',
    b'data: {"id":"c1","object":"chat.completion.chunk","model":"m","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
    b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":6,"total_tokens":17}}\n\n',
    b'data: [DONE]\n\n',
]


def _mock_handler(request: httpx.Request) -> httpx.Response:
    """mock 上游: /chat/completions 按请求 stream 字段分流"""
    body = json.loads(request.content.decode())
    if body.get("stream"):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=b"".join(UPSTREAM_STREAM_LINES))
    return httpx.Response(200, json=UPSTREAM_NONSTREAM)


AUTH = {"x-api-key": "sk-gw-test-key"}


@pytest.mark.asyncio
async def test_nonstream_roundtrip(app_env):
    transport = httpx.MockTransport(_mock_handler)
    app = _build_app(transport)
    body = {
        "model": "m1", "max_tokens": 100,
        "system": "Be helpful.",
        "messages": [{"role": "user", "content": "hi"}],
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/messages", json=body, headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert data["type"] == "message"
    assert data["role"] == "assistant"
    assert data["model"] == "m1"
    # thinking 块在前, text 在后
    assert data["content"][0] == {"type": "thinking", "thinking": "thinking hard"}
    assert data["content"][1] == {"type": "text", "text": "Hello from upstream!"}
    assert data["stop_reason"] == "end_turn"
    assert data["usage"] == {"input_tokens": 12, "output_tokens": 7}
    # 上游收到的是 OpenAI 形状(拿不到 request, 只能靠断言响应推断; 请求形状由 codec 单测覆盖)


@pytest.mark.asyncio
async def test_stream_roundtrip(app_env):
    transport = httpx.MockTransport(_mock_handler)
    app = _build_app(transport)
    body = {
        "model": "m1", "max_tokens": 100, "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/messages", json=body, headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    text = r.text
    # 事件序列断言
    events = [l[len("event: "):] for l in text.split("\n") if l.startswith("event: ")]
    assert events[0] == "message_start"
    assert "content_block_start" in events
    assert "content_block_delta" in events
    assert "message_delta" in events
    assert events[-1] == "message_stop"
    # thinking 增量 + 文本增量都在
    assert "deep thought" in text
    assert "Hel" in text and "lo!" in text
    # stop_reason 正确
    md = [json.loads(l[len("data: "):]) for l in text.split("\n")
          if l.startswith("data: ") and '"message_delta"' in l][0]
    assert md["delta"]["stop_reason"] == "end_turn"
    assert md["usage"]["output_tokens"] == 6


@pytest.mark.asyncio
async def test_invalid_request_error_shape(app_env):
    transport = httpx.MockTransport(_mock_handler)
    app = _build_app(transport)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/messages", json={"max_tokens": 10, "messages": []},
                         headers=AUTH)
    assert r.status_code == 400
    assert r.json()["type"] == "error"
    assert r.json()["error"]["type"] == "invalid_request_error"
    assert "model" in r.json()["error"]["message"]


@pytest.mark.asyncio
async def test_count_tokens_endpoint(app_env):
    transport = httpx.MockTransport(_mock_handler)
    app = _build_app(transport)
    body = {
        "model": "m1", "max_tokens": 10,
        "system": "You are a helpful assistant.",
        "messages": [{"role": "user", "content": "hello world"}],
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/messages/count_tokens", json=body, headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert set(data.keys()) == {"input_tokens"}
    assert data["input_tokens"] >= 16


@pytest.mark.asyncio
async def test_no_provider_503(app_env):
    """删掉渠道 → 503 Anthropic 形状; 测完恢复渠道"""
    import backend.models as models_mod
    from backend.auth.security import encrypt_secret
    s = models_mod.get_session()
    try:
        user_id = app_env["user_id"]
        for p in s.query(models_mod.Provider).all():
            s.delete(p)
        s.commit()
    finally:
        s.close()
    transport = httpx.MockTransport(_mock_handler)
    app = _build_app(transport)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/v1/messages", json={
                "model": "m1", "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}]}, headers=AUTH)
        assert r.status_code == 503
        assert r.json()["type"] == "error"
        assert r.json()["error"]["type"] == "overloaded_error"
    finally:
        # 恢复渠道供后续测试使用
        s = models_mod.get_session()
        try:
            if not s.query(models_mod.Provider).filter_by(user_id=user_id).first():
                s.add(models_mod.Provider(
                    user_id=user_id, name="mockprov",
                    base_url="http://upstream.test/v1",
                    api_key_encrypted=encrypt_secret("up-key"),
                    models=["m1", "m2"], disabled_models=[], enabled=True))
                s.commit()
        finally:
            s.close()
