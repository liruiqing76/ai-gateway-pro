# ============================================================
# Responses /v1/responses 端到端测试(带模拟上游)
#
# 与 test_anthropic_endpoint.py 同一套路: 用 httpx MockTransport 拦截
# 网关→上游的转发, 在 ASGI 层测:
#   鉴权(x-api-key) → 请求转换 → 候选循环 → 非流式/流式协议转换
#   → Responses 错误封套 → input_tokens 本地估算
# ============================================================

import json

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
    monkeypatch.setattr(models_mod, "_engine", None)
    monkeypatch.setattr(models_mod, "_get_engine", lambda: engine)
    monkeypatch.setattr(models_mod, "get_db_url",
                        lambda: f"sqlite:///{db_path.as_posix()}")

    from backend.auth.security import encrypt_secret
    from backend.core.circuit import circuit_breaker
    circuit_breaker._states.clear()
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
    import backend.api.responses as resp_mod
    from fastapi import FastAPI
    from fastapi.exceptions import HTTPException

    app = FastAPI()
    app.include_router(resp_mod.router)
    app.add_exception_handler(HTTPException, resp_mod.responses_http_exception_handler)

    mock_client = httpx.AsyncClient(transport=mock_transport, timeout=30)

    async def fake_get_http_client():
        return mock_client

    resp_mod.get_http_client = fake_get_http_client
    return app


UPSTREAM_NONSTREAM = {
    "id": "chatcmpl-1", "object": "chat.completion", "model": "mockprov · m1",
    "choices": [{
        "index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": "Hello from upstream!"},
    }],
    "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
}

UPSTREAM_TOOL_NONSTREAM = {
    "id": "chatcmpl-2", "object": "chat.completion", "model": "mockprov · m1",
    "choices": [{
        "index": 0, "finish_reason": "tool_calls",
        "message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_7", "type": "function",
             "function": {"name": "get_weather", "arguments": '{"city":"Beijing"}'}},
        ]},
    }],
    "usage": {"prompt_tokens": 20, "completion_tokens": 9, "total_tokens": 29},
}

UPSTREAM_STREAM_LINES = [
    b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Hel"},"finish_reason":null}]}\n\n',
    b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"lo!"},"finish_reason":null}]}\n\n',
    b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
    b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":6,"total_tokens":17}}\n\n',
    b'data: [DONE]\n\n',
]

UPSTREAM_TOOL_STREAM_LINES = [
    b'data: {"id":"c2","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_7","function":{"name":"get_weather","arguments":"{\\"city\\""}}]},"finish_reason":null}]}\n\n',
    b'data: {"id":"c2","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":":\\"Beijing\\"}"}}]},"finish_reason":null}]}\n\n',
    b'data: {"id":"c2","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
    b'data: [DONE]\n\n',
]

_seen_requests: list[httpx.Request] = []


def _mock_handler(request: httpx.Request) -> httpx.Response:
    """mock 上游: /chat/completions 按请求 stream 字段分流"""
    _seen_requests.append(request)
    body = json.loads(request.content.decode())
    has_tools = bool(body.get("tools"))
    if body.get("stream"):
        lines = UPSTREAM_TOOL_STREAM_LINES if has_tools else UPSTREAM_STREAM_LINES
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=b"".join(lines))
    return httpx.Response(200, json=UPSTREAM_TOOL_NONSTREAM if has_tools
                          else UPSTREAM_NONSTREAM)


AUTH = {"x-api-key": "sk-gw-test-key"}


@pytest.mark.asyncio
async def test_nonstream_roundtrip(app_env):
    _seen_requests.clear()
    transport = httpx.MockTransport(_mock_handler)
    app = _build_app(transport)
    body = {
        "model": "m1",
        "instructions": "Be helpful.",
        "input": "hi",
        "max_output_tokens": 100,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/responses", json=body, headers=AUTH)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["object"] == "response"
    assert data["status"] == "completed"
    assert data["output"][0]["type"] == "message"
    assert data["output"][0]["content"][0]["text"] == "Hello from upstream!"
    assert data["usage"]["input_tokens"] == 12
    assert data["usage"]["output_tokens"] == 7

    # 上游收到的是 OpenAI Chat 形状: instructions→system, input→user, 上限字段改名
    sent = json.loads(_seen_requests[-1].content.decode())
    assert sent["messages"][0] == {"role": "system", "content": "Be helpful."}
    assert sent["messages"][1] == {"role": "user", "content": "hi"}
    assert sent["max_tokens"] == 100


@pytest.mark.asyncio
async def test_stream_roundtrip(app_env):
    _seen_requests.clear()
    transport = httpx.MockTransport(_mock_handler)
    app = _build_app(transport)
    body = {"model": "m1", "input": "hi", "stream": True}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/responses", json=body, headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    text = r.text
    events = [l[len("event: "):] for l in text.split("\n") if l.startswith("event: ")]
    assert events[0] == "response.created"
    assert "response.output_text.delta" in events
    assert events[-1] == "response.completed"
    assert "Hel" in text and "lo!" in text

    completed = [json.loads(l[len("data: "):]) for l in text.split("\n")
                 if l.startswith("data: ") and '"response.completed"' in l][0]
    assert completed["response"]["output"][0]["content"][0]["text"] == "Hello!"
    assert completed["response"]["usage"]["input_tokens"] == 11


@pytest.mark.asyncio
async def test_tool_call_nonstream(app_env):
    _seen_requests.clear()
    transport = httpx.MockTransport(_mock_handler)
    app = _build_app(transport)
    body = {
        "model": "m1", "input": "weather in Beijing?",
        "tools": [{"type": "function", "name": "get_weather",
                   "description": "get weather",
                   "parameters": {"type": "object",
                                  "properties": {"city": {"type": "string"}},
                                  "required": ["city"]}}],
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/responses", json=body, headers=AUTH)
    assert r.status_code == 200, r.text
    data = r.json()
    call = data["output"][0]
    assert call["type"] == "function_call"
    assert call["name"] == "get_weather"
    assert call["call_id"] == "call_7"
    assert json.loads(call["arguments"]) == {"city": "Beijing"}

    # 扁平工具定义被转成 chat 的 function 子对象
    sent = json.loads(_seen_requests[-1].content.decode())
    assert sent["tools"][0]["type"] == "function"
    assert sent["tools"][0]["function"]["name"] == "get_weather"


@pytest.mark.asyncio
async def test_tool_call_stream(app_env):
    _seen_requests.clear()
    transport = httpx.MockTransport(_mock_handler)
    app = _build_app(transport)
    body = {
        "model": "m1", "input": "weather?", "stream": True,
        "tools": [{"type": "function", "name": "get_weather"}],
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/responses", json=body, headers=AUTH)
    assert r.status_code == 200
    text = r.text
    events = [l[len("event: "):] for l in text.split("\n") if l.startswith("event: ")]
    assert "response.function_call_arguments.delta" in events
    assert "response.output_item.done" in events
    assert events[-1] == "response.completed"

    completed = [json.loads(l[len("data: "):]) for l in text.split("\n")
                 if l.startswith("data: ") and '"response.completed"' in l][0]
    item = completed["response"]["output"][0]
    assert item["type"] == "function_call"
    assert item["name"] == "get_weather"
    assert json.loads(item["arguments"]) == {"city": "Beijing"}


@pytest.mark.asyncio
async def test_tool_history_roundtrip(app_env):
    """多轮工具循环: 历史里的 function_call + output 必须原样进上游。"""
    _seen_requests.clear()
    transport = httpx.MockTransport(_mock_handler)
    app = _build_app(transport)
    body = {
        "model": "m1",
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "weather in Beijing?"}]},
            {"type": "function_call", "call_id": "call_7", "name": "get_weather",
             "arguments": '{"city":"Beijing"}'},
            {"type": "function_call_output", "call_id": "call_7",
             "output": "Beijing: 18C clear"},
        ],
        "tools": [{"type": "function", "name": "get_weather"}],
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/responses", json=body, headers=AUTH)
    assert r.status_code == 200, r.text
    sent = json.loads(_seen_requests[-1].content.decode())
    roles = [m["role"] for m in sent["messages"]]
    assert roles == ["user", "assistant", "tool"]
    assert sent["messages"][1]["tool_calls"][0]["id"] == "call_7"
    assert sent["messages"][2]["tool_call_id"] == "call_7"
    assert sent["messages"][2]["content"] == "Beijing: 18C clear"


@pytest.mark.asyncio
async def test_invalid_request_error_shape(app_env):
    transport = httpx.MockTransport(_mock_handler)
    app = _build_app(transport)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/responses", json={"input": "hi"}, headers=AUTH)
    assert r.status_code == 400
    data = r.json()
    assert "error" in data
    assert data["error"]["type"] == "invalid_request_error"
    assert "model" in data["error"]["message"]


@pytest.mark.asyncio
async def test_input_tokens_endpoint(app_env):
    transport = httpx.MockTransport(_mock_handler)
    app = _build_app(transport)
    body = {"model": "m1", "instructions": "You are a helpful assistant.",
            "input": "hello world"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/responses/input_tokens", json=body, headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert set(data.keys()) == {"input_tokens"}
    assert data["input_tokens"] >= 16


@pytest.mark.asyncio
async def test_no_provider_503(app_env):
    import backend.models as models_mod
    s = models_mod.get_session()
    try:
        for p in s.query(models_mod.Provider).all():
            s.delete(p)
        s.commit()
    finally:
        s.close()
    transport = httpx.MockTransport(_mock_handler)
    app = _build_app(transport)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/responses", json={"model": "m1", "input": "hi"},
                         headers=AUTH)
    assert r.status_code == 503
    data = r.json()
    assert data["error"]["type"] == "service_unavailable"


@pytest.mark.asyncio
async def test_bearer_auth_also_works(app_env):
    transport = httpx.MockTransport(_mock_handler)
    app = _build_app(transport)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/responses", json={"model": "m1", "input": "hi"},
                         headers={"Authorization": "Bearer sk-gw-test-key"})
    assert r.status_code == 200
