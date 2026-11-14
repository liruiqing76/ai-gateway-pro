# -*- coding: utf-8 -*-
"""OpenCode v2 免密车道: 指纹门 / session 铸造 / 转发层接管 / 工具名还原。"""

import json
import re
import sys

sys.path.insert(0, r"D:\zmzc-code\ai-gateway-pro")

import httpx
import pytest
from types import SimpleNamespace

from backend.core.channel_auth import opencode_free as of
from backend.core.channel_auth import channel_transform as ct


def _provider(name="opencode", provider_id=99):
    return SimpleNamespace(id=provider_id, name=name,
                           base_url="https://opencode.ai/zen/v1",
                           free_only=True, max_concurrency=None,
                           min_interval_ms=None)


class TestSessionMinting:
    def test_canonical_shape(self):
        s = of.canonicalize_session("abc")
        assert of.SESSION_RE.match(s), s
        assert of.SESSION_DESCENDING_RE.match(s[4:16]), s[4:16]
        # 同信号永远同 id(配额按会话计)
        assert of.canonicalize_session("abc") == s
        assert of.canonicalize_session("abd") != s

    def test_mint_shape(self):
        s = of.mint_session_id()
        assert of.SESSION_RE.match(s) and of.SESSION_DESCENDING_RE.match(s[4:16])

    def test_is_canonical_rejects_bad(self):
        assert not of.is_canonical_session("ses_zzzzzzzzzzzzzzzzzzzzzz")
        assert not of.is_canonical_session("random-string")
        good = of.canonicalize_session("x")
        assert of.is_canonical_session(good)
        # 升序编码(低 12 位超 0xfff 窗口的补码区)→ 拒绝
        assert not of.is_canonical_session("ses_" + "a" * 9 + "0" + "ab" + "X" * 14)

    def test_session_for_conversation(self):
        assert of.session_for_conversation(None).startswith("ses_")
        assert of.session_for_conversation("") != "ses_"  # canonical
        good = of.canonicalize_session("keep")
        assert of.session_for_conversation(good) == good  # 原样透传


class TestFingerprintGate:
    def test_injects_read_shell(self):
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        rename = of.apply_fingerprint(body)
        names = [t["function"]["name"] for t in body["tools"]]
        assert "read" in names and "shell" in names
        assert rename == {}
        # 官方定义逐字: description 非空 + schema 有 required
        read_tool = next(t for t in body["tools"] if t["function"]["name"] == "read")
        assert "1-based line number" in read_tool["function"]["description"]
        assert read_tool["function"]["parameters"]["required"] == ["path"]

    def test_rename_caller_conflict_and_note(self):
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {
                    "name": "read", "description": "mine",
                    "parameters": {"type": "object"}}}]}
        rename = of.apply_fingerprint(body)
        names = [t["function"]["name"] for t in body["tools"]]
        assert "read_client" in names and "read" in names
        assert rename == {"read_client": "read"}
        # 注入过 → system 提示词压住模型真调占位工具
        assert body["messages"][0]["role"] == "system"
        assert "placeholders that do nothing" in body["messages"][0]["content"]

    def test_official_definition_not_renamed(self):
        """调用方已带逐字官方定义 → 不重复注入/不改名"""
        d = of.FINGERPRINT_TOOL_DEFINITIONS["read"]
        body = {"model": "m", "messages": [{"role": "user", "content": "x"}],
                "tools": [{"type": "function", "function": {
                    "name": "read", "description": d["description"],
                    "parameters": d["parameters"]}}]}
        rename = of.apply_fingerprint(body)
        names = [t["function"]["name"] for t in body["tools"]]
        assert rename == {}
        assert names.count("read") == 1

    def test_tool_choice_none_never_emitted(self):
        body = {"model": "m", "messages": [{"role": "user", "content": "x"}],
                "tools": [{"type": "function", "function": {
                    "name": "t", "parameters": {}}}],
                "tool_choice": "none"}
        of.apply_fingerprint(body)
        assert "tool_choice" not in body

    def test_case_variant_passthrough(self):
        """调用方 Read/Shell(大写变体)原样保留, 不占闸门槽"""
        body = {"model": "m", "messages": [{"role": "user", "content": "x"}],
                "tools": [{"type": "function", "function": {
                    "name": "Read", "parameters": {}}}]}
        rename = of.apply_fingerprint(body)
        names = [t["function"]["name"] for t in body["tools"]]
        assert "Read" in names and "read" in names and rename == {}


class TestLaneRequestAndHeaders:
    def test_headers_v2_shape(self):
        h = of.gateway_headers("ses_test", stream=True)
        assert h["authorization"] == "Bearer public"
        assert h["user-agent"] == "opencode/latest/2.0.26/cli"
        assert h["x-opencode-session"] == h["x-opencode-session-id"] == "ses_test"
        assert h["x-session-affinity"] == "ses_test" and h["x-session-id"] == "ses_test"
        assert h["accept"] == "text/event-stream"
        # ⚠ v2 禁带 x-opencode-request(v1 专用)
        assert "x-opencode-request" not in h

    def test_build_lane_request(self):
        url, headers, payload, rename = of.build_lane_request(
            {"model": "glm-5.3-flash-free",
             "messages": [{"role": "user", "content": "hi"}], "stream": True},
            "ses_x")
        assert url == "https://opencode.ai/inference/v1/chat/completions"
        body = json.loads(payload)
        assert {t["function"]["name"] for t in body["tools"]} >= {"read", "shell"}

    def test_endpoint_split(self):
        assert of.endpoint_for("muse-spark-1.3-contributor-free") == "/responses"
        assert of.endpoint_for("union-alpha") == "/messages"
        assert of.endpoint_for("big-pickle") == "/chat/completions"

    def test_converter_restores_tool_names(self):
        conv = of.make_lane_response_converter({"read_client": "read"})
        line = ('data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
                '"function":{"name":"read_client","arguments":"{}"}}]}}]}')
        out = conv(line)
        assert len(out) == 1 and '"read_client"' not in out[0]
        assert '"name": "read"' in out[0] or '"name":"read"' in out[0]
        assert conv("data: [DONE]") == ["data: [DONE]"]


@pytest.mark.asyncio
async def test_opencode_lane_through_forward_stream(monkeypatch):
    """forward_stream → opencode 车道: 上游收到指纹体, 响应侧还原改名。"""
    import backend.core.proxy as core_proxy
    from backend.core.circuit import circuit_breaker
    from backend.core.scheduler import quality_tracker
    circuit_breaker._states.clear()

    async def _tp(*a, **k):
        return True
    monkeypatch.setattr(core_proxy.traffic_shaping, "pace", _tp)
    monkeypatch.setattr(core_proxy.traffic_shaping, "acquire", _tp)
    monkeypatch.setattr(core_proxy.traffic_shaping, "release", lambda *a: None)

    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.content.decode())
        sse = ('data: {"choices":[{"index":0,"delta":{"content":"OK"}}]}\n\n'
               'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
               '"function":{"name":"read_client","arguments":"{}"}}]}}]}\n\n'
               "data: [DONE]\n\n")
        return httpx.Response(200, content=sse.encode(),
                              headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        # 调用方带一个与官方同名的 read 工具 → 门把它改名 read_client,
        # 响应侧靠还原表改回 read(真实场景: 上游回的是我们发出去的 read_client)
        gen, err = await core_proxy.forward_stream(
            client, _provider(), "", "big-pickle",
            {"model": "big-pickle",
             "messages": [{"role": "user", "content": "hi"}], "stream": True,
             "tools": [{"type": "function", "function": {
                 "name": "read", "description": "mine",
                 "parameters": {"type": "object"}}}]})
        assert err is None
        chunks = [c async for c in gen]
    finally:
        await client.aclose()

    joined = b"".join(chunks).decode()
    assert "data: [DONE]" in joined and '"OK"' in joined
    # 上游 URL/头/body 都是 v2 门形状
    assert captured["url"] == "https://opencode.ai/inference/v1/chat/completions"
    h = captured["headers"]
    assert h["user-agent"] == "opencode/latest/2.0.26/cli"
    assert h["authorization"] == "Bearer public"
    sess = h["x-opencode-session"]
    assert of.SESSION_RE.match(sess) and of.SESSION_DESCENDING_RE.match(sess[4:16])
    assert h["x-session-affinity"] == sess
    assert "x-opencode-request" not in h
    assert {t["function"]["name"] for t in captured["body"]["tools"]} >= {"read", "shell"}
    # 响应侧: read_client 还原成调用方原名 read
    assert '"read_client"' not in joined
    assert re.search(r'"name":\s*"read"', joined)


@pytest.mark.asyncio
async def test_opencode_lane_stable_session_per_channel(monkeypatch):
    """同渠道两次请求 → 同一 session(配额按会话计, 不能每请求新铸)。"""
    import backend.core.proxy as core_proxy
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(dict(req.headers)["x-opencode-session"])
        return httpx.Response(200, content=b"data: [DONE]\n\n",
                              headers={"content-type": "text/event-stream"})

    async def _tp(*a, **k):
        return True
    monkeypatch.setattr(core_proxy.traffic_shaping, "pace", _tp)
    monkeypatch.setattr(core_proxy.traffic_shaping, "acquire", _tp)
    monkeypatch.setattr(core_proxy.traffic_shaping, "release", lambda *a: None)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        for _ in range(2):
            gen, _ = await core_proxy.forward_stream(
                client, _provider(), "", "big-pickle",
                {"model": "big-pickle",
                 "messages": [{"role": "user", "content": "hi"}], "stream": True})
            async for _c in gen:
                pass
    finally:
        await client.aclose()
    assert len(seen) == 2 and seen[0] == seen[1]
