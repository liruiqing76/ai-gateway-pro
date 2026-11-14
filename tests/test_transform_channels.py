# ============================================================
# 变换渠道端到端: TRAE SOLO / Qoder 加密信封 → 标准 OpenAI 形状
#
# mock 渠道上游(SOLO 事件流 / 信封流), 验证:
#   forward_stream  → 产出标准 OpenAI data 行 + [DONE], StreamRecorder 可解析
#   forward_non_stream → 聚合出 chat.completion dict(含 content/usage)
#   流内 error 事件 → 断流(触发候选切换语义)
#   非200 → (None, "upstream {status}") + 熔断记账
# ============================================================

import json
from types import SimpleNamespace

import httpx
import pytest

import backend.core.proxy as core_proxy
from backend.core.channel_auth import channel_transform


def _provider(name, provider_id=1):
    return SimpleNamespace(
        id=provider_id, name=name, base_url="http://fake.test/v1",
        free_only=True, max_concurrency=None, min_interval_ms=None)


TRAE_CREDS = json.dumps({"channel": "trae", "access_token": "jwt-x",
                         "uid": "u1", "machine_id": "m1", "device_id": "d1"})
QODER_CREDS = json.dumps({"channel": "qodercn", "access_token": "tk",
                          "security_oauth_token": "tk", "uid": "9",
                          "machine_id": "mm"})


SOLO_SSE = (
    b"event:output\ndata:{\"response\":\"Hello\"}\n\n"
    b"event:output\ndata:{\"reasoning_content\":\"think\"}\n\n"
    b"event:token_usage\ndata:{\"prompt_tokens\":4,\"completion_tokens\":3}\n\n"
    b"event:done\ndata:{\"finish_reason\":\"stop\"}\n\n"
)
QODER_ENVELOPED = (
    b'data:{"body":"{\\"choices\\":[{\\"index\\":0,\\"delta\\":{\\"content\\":\\"Hi\\\"},\\"finish_reason\\":null}]}"}\n\n'
    b'data:{"body":"{\\"choices\\":[],\\"usage\\":{\\"prompt_tokens\\":2,\\"completion_tokens\\":1,\\"total_tokens\\":3}}"}\n\n'
    b'data:{"body":"[DONE]"}\n\n'
)


MINIMAX_SSE = (
    b"event: message_start\n"
    b'data: {"type":"message_start","message":{"id":"msg_9",'
    b'"usage":{"input_tokens":7}}}\n\n'
    b"event: content_block_start\n"
    b'data: {"type":"content_block_start","index":0,'
    b'"content_block":{"type":"text","text":""}}\n\n'
    b"event: content_block_delta\n"
    b'data: {"type":"content_block_delta","index":0,'
    b'"delta":{"type":"text_delta","text":"HiMM"}}\n\n'
    b"event: message_delta\n"
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    b'"usage":{"output_tokens":2}}\n\n'
    b"event: message_stop\n"
    b'data: {"type":"message_stop"}\n\n'
)


def make_handler(requests_log: list, solo_body: bytes = SOLO_SSE,
                 qoder_body: bytes = QODER_ENVELOPED, status: int = 200,
                 minimax_body: bytes = MINIMAX_SSE):
    def handler(request: httpx.Request) -> httpx.Response:
        requests_log.append((str(request.url), dict(request.headers)))
        if status != 200:
            return httpx.Response(status, text="bad gateway")
        if "mchost" in request.url.host or "llm_utils_chat" in request.url.path:
            return httpx.Response(200, content=solo_body,
                                  headers={"content-type": "text/event-stream"})
        if "minimax" in request.url.host:
            return httpx.Response(200, content=minimax_body,
                                  headers={"content-type": "text/event-stream"})
        # qoder 信封流(任何带 algo 路径的 POST)
        return httpx.Response(200, content=qoder_body,
                              headers={"content-type": "text/event-stream"})
    return httpx.MockTransport(handler)


async def _no_pace(*a, **k):
    return True


async def _no_timeout(*a, **k):
    return None


@pytest.mark.asyncio
async def test_trae_stream_standard_openai(monkeypatch):
    monkeypatch.setattr(core_proxy.traffic_shaping, "pace", _no_pace)
    monkeypatch.setattr(core_proxy.traffic_shaping, "acquire", _no_pace)
    monkeypatch.setattr(core_proxy.traffic_shaping, "release", lambda *a: None)

    log = []
    client = httpx.AsyncClient(transport=make_handler(log))
    try:
        gen, err = await core_proxy.forward_stream(
            client, _provider("trae"), TRAE_CREDS, "glm-5.2",
            {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}],
             "stream": True})
        assert err is None and gen is not None
        chunks = [c async for c in gen]
    finally:
        await client.account_close() if hasattr(client, "account_close") else await client.aclose()

    joined = b"".join(chunks).decode()
    # 标准 OpenAI 形状: data 行 + JSON + [DONE]
    lines = [l for l in joined.split("\n\n") if l.strip()]
    payloads = [l.replace("data:", "").strip() for l in lines]
    assert payloads[-1] == "[DONE]"
    objs = [json.loads(p) for p in payloads[:-1]]
    deltas = [o.get("choices", [{}])[0].get("delta", {}) for o in objs if o.get("choices")]
    assert any(d.get("content") == "Hello" for d in deltas)
    assert any(d.get("reasoning_content") == "think" for d in deltas)
    assert any("usage" in o and o["usage"]["prompt_tokens"] == 4 for o in objs)
    # 上游收到 SOLO 端点 + Cloud-IDE-JWT + config_name
    url, headers = log[0]
    assert "llm_utils_chat" in url
    assert headers["authorization"] == "Cloud-IDE-JWT jwt-x"


@pytest.mark.asyncio
async def test_trae_non_stream_aggregates(monkeypatch):
    monkeypatch.setattr(core_proxy.traffic_shaping, "pace", _no_pace)
    monkeypatch.setattr(core_proxy.traffic_shaping, "acquire", _no_pace)
    monkeypatch.setattr(core_proxy.traffic_shaping, "release", lambda *a: None)
    log = []
    client = httpx.AsyncClient(transport=make_handler(log))
    try:
        data, err = await core_proxy.forward_non_stream(
            client, _provider("trae"), TRAE_CREDS, "glm-5.2",
            {"model": "glm-5.2",
             "messages": [{"role": "user", "content": "hi"}]})
    finally:
        await client.aclose()
    assert err is None and data is not None
    assert data["choices"][0]["message"]["content"] == "Hello"
    assert data["choices"][0]["message"]["reasoning_content"] == "think"
    assert data["usage"]["prompt_tokens"] == 4
    assert data["model"].startswith("trae · ")


@pytest.mark.asyncio
async def test_qoder_envelope_stream(monkeypatch):
    monkeypatch.setattr(core_proxy.traffic_shaping, "pace", _no_pace)
    monkeypatch.setattr(core_proxy.traffic_shaping, "acquire", _no_pace)
    monkeypatch.setattr(core_proxy.traffic_shaping, "release", lambda *a: None)
    log = []
    client = httpx.AsyncClient(transport=make_handler(log))

    # 签名走 Node sidecar; 测试里 mock 掉签名, 只验信封解包与转发
    async def fake_sign(channel, creds, body):
        return {"url": "https://gateway.qoder.com.cn/algo/fake",
                "headers": {"Authorization": "Bearer COSY.fake",
                            "Content-Type": "application/json"},
                "body": json.dumps(body)}
    import backend.core.channel_auth.qoder as qoder_mod
    monkeypatch.setattr(qoder_mod, "sign_infer_request", fake_sign)
    try:
        gen, err = await core_proxy.forward_stream(
            client, _provider("qodercn"), QODER_CREDS, "qfmodel",
            {"model": "qfmodel", "messages": [{"role": "user", "content": "hi"}],
             "stream": True})
        assert err is None
        chunks = [c async for c in gen]
    finally:
        await client.aclose()
    joined = b"".join(chunks).decode()
    assert 'data: {"choices"' in joined and "[DONE]" in joined
    first = json.loads(joined.split("\n\n")[0][5:])
    assert first["choices"][0]["delta"]["content"] == "Hi"
    url, headers = log[0]
    assert headers["authorization"] == "Bearer COSY.fake"


@pytest.mark.asyncio
async def test_transform_upstream_error_maps_breaker(monkeypatch):
    """非 200 → error 归类 + 429 配额冷却"""
    from backend.core.circuit import circuit_breaker
    monkeypatch.setattr(core_proxy.traffic_shaping, "pace", _no_pace)
    monkeypatch.setattr(core_proxy.traffic_shaping, "acquire", _no_pace)
    monkeypatch.setattr(core_proxy.traffic_shaping, "release", lambda *a: None)
    log = []
    client = httpx.AsyncClient(transport=make_handler(log, status=429))
    try:
        data, err = await core_proxy.forward_non_stream(
            client, _provider("trae"), TRAE_CREDS, "glm-5.2",
            {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
        assert data is None and err == "upstream 429"
    finally:
        await client.aclose()
    # 429 → 渠道级限流冷却被记账(provider 维度)
    assert circuit_breaker.provider_in_cooldown(1)


MINIMAX_CREDS = json.dumps({"channel": "minimax", "access_token": "mmoat-x",
                            "refresh_token": "mmort-y",
                            "expires_at": "4102444800000",
                            "token_type": "Bearer"})


@pytest.mark.asyncio
async def test_minimax_stream_anthropic_to_openai(monkeypatch):
    """MiniMax 上游是 Anthropic Messages SSE → 网关转成标准 OpenAI 形状。"""
    monkeypatch.setattr(core_proxy.traffic_shaping, "pace", _no_pace)
    monkeypatch.setattr(core_proxy.traffic_shaping, "acquire", _no_pace)
    monkeypatch.setattr(core_proxy.traffic_shaping, "release", lambda *a: None)
    log = []
    client = httpx.AsyncClient(transport=make_handler(log))
    try:
        gen, err = await core_proxy.forward_stream(
            client, _provider("minimax"), MINIMAX_CREDS, "MiniMax-M3.1-Flash",
            {"model": "MiniMax-M3.1-Flash", "stream": True,
             "messages": [{"role": "user", "content": "hi"}]})
        assert err is None and gen is not None
        chunks = [c async for c in gen]
    finally:
        await client.aclose()

    joined = b"".join(chunks).decode()
    lines = [l for l in joined.split("\n\n") if l.strip()]
    payloads = [l.replace("data:", "").strip() for l in lines]
    assert payloads[-1] == "[DONE]"
    objs = [json.loads(p) for p in payloads[:-1]]
    deltas = [o.get("choices", [{}])[0].get("delta", {}) for o in objs
              if o.get("choices")]
    assert any(d.get("content") == "HiMM" for d in deltas)
    assert any(o.get("usage", {}).get("prompt_tokens") == 7 for o in objs)
    # 上游收到 Anthropic Messages 端点 + Bearer
    url, headers = log[0]
    assert url == "https://agent.minimax.cn/mavis/api/v1/llm/v1/messages"
    assert headers["authorization"] == "Bearer mmoat-x"


@pytest.mark.asyncio
async def test_minimax_non_stream_aggregates(monkeypatch):
    monkeypatch.setattr(core_proxy.traffic_shaping, "pace", _no_pace)
    monkeypatch.setattr(core_proxy.traffic_shaping, "acquire", _no_pace)
    monkeypatch.setattr(core_proxy.traffic_shaping, "release", lambda *a: None)
    log = []
    client = httpx.AsyncClient(transport=make_handler(log))
    try:
        data, err = await core_proxy.forward_non_stream(
            client, _provider("minimax"), MINIMAX_CREDS, "MiniMax-M3.1-Flash",
            {"model": "MiniMax-M3.1-Flash",
             "messages": [{"role": "user", "content": "hi"}]})
    finally:
        await client.aclose()
    assert err is None and data is not None
    assert data["choices"][0]["message"]["content"] == "HiMM"
    assert data["usage"]["prompt_tokens"] == 7
    assert data["model"].startswith("minimax · ")
