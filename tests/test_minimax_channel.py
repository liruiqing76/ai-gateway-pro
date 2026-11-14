"""MiniMax 渠道: 设备码登录 / 凭据 / 模型 / 签到 / 协议桥 单元测试。

真机契约(2026-10-24 实测):
  POST {account}/oauth2/device/code  (form) → device_code/user_code/verification_uri
  POST {account}/oauth2/token        (form) → access_token + 一次性 refresh_token
  GET  {api}/mavis/api/v1/models                → providers[].config.models
  GET  {api}/minimax-cloud/api/v1/signin/status?timezone_id=...
  POST {api}/minimax-cloud/api/v1/signin/claim  body "{}"
  POST {api}/mavis/api/v1/llm/v1/messages       → Anthropic Messages SSE
"""
import json

import pytest

from backend.core.channel_auth import minimax, minimax_wire


# ------------------------------------------------------------
# 设备码登录
# ------------------------------------------------------------

class TestDeviceLogin:
    def test_pkce_pair_shape(self):
        verifier, challenge = minimax.create_pkce()
        assert 40 <= len(verifier) <= 128
        assert "=" not in verifier and "+" not in verifier and "/" not in verifier
        assert "=" not in challenge
        # 同一 verifier 必须得到同一 challenge
        assert minimax.create_pkce  # 纯函数性由下一条验证
        import hashlib, base64
        expect = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
        assert challenge == expect

    def test_parse_device_authorization(self):
        auth = minimax.parse_device_authorization({
            "device_code": "dc-1", "user_code": "AB12-CD34",
            "verification_uri": "https://account.minimax.cn/device",
            "verification_uri_complete": "https://account.minimax.cn/device?user_code=AB12-CD34",
            "expires_in": 600, "interval": 5,
        })
        assert auth["device_code"] == "dc-1"
        assert auth["user_code"] == "AB12-CD34"
        assert auth["verification_uri_complete"].endswith("AB12-CD34")
        assert auth["interval"] == 5

    def test_parse_device_authorization_missing(self):
        assert minimax.parse_device_authorization({}) is None
        assert minimax.parse_device_authorization({"user_code": "X"}) is None
        assert minimax.parse_device_authorization(None) is None

    def test_parse_device_authorization_complete_fallback(self):
        """没有 verification_uri_complete 时用 verification_uri 兜底。"""
        auth = minimax.parse_device_authorization({
            "device_code": "d", "user_code": "U",
            "verification_uri": "https://x/device", "expires_in": 60,
        })
        assert auth["verification_uri_complete"] == "https://x/device"


class TestTokenGrant:
    def test_parse_grant_basic(self):
        g = minimax.parse_token_grant({
            "access_token": "mmoat_a", "refresh_token": "mmort_b",
            "expires_in": 3600, "token_type": "Bearer",
            "scope": "agent.default",
        })
        assert g["access_token"] == "mmoat_a"
        assert g["refresh_token"] == "mmort_b"
        assert g["token_type"] == "Bearer"
        assert int(g["expires_at"]) > 0
        assert "channel" not in g      # channel 由 credentials_package 落章

    def test_parse_grant_keeps_previous_refresh(self):
        """续期响应不回新 refresh_token 时保留旧的(但仍需回写)。"""
        g = minimax.parse_token_grant(
            {"access_token": "a2", "expires_in": 3600, "token_type": "Bearer"},
            previous_refresh="old-refresh")
        assert g["refresh_token"] == "old-refresh"

    def test_parse_grant_rejects_bad_token_type(self):
        assert minimax.parse_token_grant({
            "access_token": "a", "refresh_token": "b", "expires_in": 60,
            "token_type": "MAC"}) is None

    def test_parse_grant_rejects_missing_access(self):
        assert minimax.parse_token_grant({
            "refresh_token": "b", "expires_in": 60, "token_type": "Bearer"}) is None
        assert minimax.parse_token_grant({}) is None
        assert minimax.parse_token_grant(None) is None


# ------------------------------------------------------------
# 凭据包
# ------------------------------------------------------------

class TestCredentials:
    def test_roundtrip(self):
        pkg = minimax.credentials_package({
            "access_token": "mmoat_x", "refresh_token": "mmort_y",
            "expires_at": 4102444800000, "token_type": "Bearer",
        })
        creds = minimax.parse_credentials_package(pkg)
        assert creds["access_token"] == "mmoat_x"
        assert creds["refresh_token"] == "mmort_y"
        assert creds["channel"] == minimax.CHANNEL_ID
        assert minimax.credential_expires_at_ms(creds) == 4102444800000

    def test_parse_rejects_other_channel(self):
        pkg = minimax.credentials_package({"access_token": "a", "refresh_token": "b"})
        assert minimax.parse_credentials_package(pkg, channel="trae") is None

    def test_parse_garbage(self):
        assert minimax.parse_credentials_package("") is None
        assert minimax.parse_credentials_package("not json") is None
        assert minimax.parse_credentials_package('{"access_token": ""}') is None

    def test_expiry_checks(self):
        fresh = {"access_token": "a", "expires_at": 4102444800000}
        assert minimax.is_expired(fresh) is False
        stale = {"access_token": "a", "expires_at": 1000}
        assert minimax.is_expired(stale) is True
        assert minimax.is_refreshable({"refresh_token": "r"}) is True
        assert minimax.is_refreshable({}) is False


# ------------------------------------------------------------
# 信封 / 模型 / 头
# ------------------------------------------------------------

class TestEnvelopeAndModels:
    def test_unwrap_ok(self):
        data, code, msg = minimax.unwrap_envelope(
            {"data": {"x": 1}, "base_resp": {"status_code": 0, "status_msg": "ok"}})
        assert data == {"x": 1} and code == 0 and msg == ""

    def test_unwrap_business_error(self):
        data, code, msg = minimax.unwrap_envelope(
            {"base_resp": {"status_code": 1004, "status_msg": "bad"}})
        assert data is None and code == 1004 and msg == "bad"

    def test_parse_models(self):
        payload = {"providers": [{"providerId": "minimax", "config": {
            "model_order": ["MiniMax-M3", "MiniMax-M2.7"],
            "models": {
                "MiniMax-M3": {
                    "name": "MiniMax-M3", "limit": {"context": 512000, "output": 128000},
                    "modalities": {"input": ["text", "image"]},
                    "thinking_config": {"mode": "switchable"},
                },
                "MiniMax-M2.7": {
                    "name": "MiniMax-M2.7", "limit": {"context": 200000, "output": 128000},
                    "modalities": {"input": ["text"]},
                    "thinking_config": {"mode": "forced_on"},
                },
            },
        }}]}
        models = minimax.parse_models_payload(payload)
        assert [m["id"] for m in models] == ["MiniMax-M3", "MiniMax-M2.7"]
        assert models[0]["vision"] is True
        assert models[1]["vision"] is False
        assert models[0]["context"] == 512000

    def test_parse_models_context_window_options(self):
        payload = {"providers": [{"providerId": "minimax", "config": {
            "model_order": ["M"],
            "models": {"M": {"limit": {"context": 1000},
                             "context_window_options": [2000, 4000]}},
        }}]}
        assert minimax.parse_models_payload(payload)[0]["context"] == 4000

    def test_parse_models_empty(self):
        assert minimax.parse_models_payload({}) == []
        assert minimax.parse_models_payload({"providers": []}) == []

    def test_headers(self):
        creds = {"access_token": "tok"}
        h = minimax.chat_headers(creds)
        assert h["Authorization"] == "Bearer tok"
        assert h["Accept"] == "text/event-stream"
        assert h["Content-Type"] == "application/json"
        a = minimax.api_headers(creds)
        assert a["Accept"] == "application/json"
        assert "Content-Type" not in a


# ------------------------------------------------------------
# 签到面板
# ------------------------------------------------------------

def _panel(today_status=3, claimable=0):
    days = []
    for n in range(1, 8):
        days.append({
            "day_no": n,
            "points": 400,
            "bonus_points": 0,
            "status": today_status if n == 1 else 1,
            "is_today": n == 1,
        })
    return {"scene": 2, "days": days}


class TestSignin:
    def test_parse_panel(self):
        p = minimax.parse_signin_panel(_panel())
        assert len(p["days"]) == 7
        assert p["scene"] == 2
        st = minimax.panel_to_status(p)
        assert st["today_checked_in"] is True
        assert st["streak_days"] == 1

    def test_parse_panel_rejects_bad_day_count(self):
        assert minimax.parse_signin_panel({"scene": 2, "days": []}) is None
        assert minimax.parse_signin_panel({"scene": 2, "days": [{}] * 6}) is None

    def test_parse_panel_rejects_bad_status(self):
        bad = _panel()
        bad["days"][0]["status"] = 9
        assert minimax.parse_signin_panel(bad) is None

    def test_parse_panel_rejects_duplicate_day_no(self):
        bad = _panel()
        bad["days"][1]["day_no"] = 1
        assert minimax.parse_signin_panel(bad) is None

    def test_panel_to_status(self):
        st = minimax.panel_to_status(minimax.parse_signin_panel(_panel()))
        assert st["today_checked_in"] is True
        assert st["daily_credit"] == 400

    def test_with_timezone(self):
        url = minimax._with_timezone(minimax.API_HOST, minimax.SIGNIN_STATUS_PATH)
        assert "timezone_id=" in url


# ------------------------------------------------------------
# 协议桥: OpenAI → Anthropic Messages
# ------------------------------------------------------------

class TestWireRequest:
    def test_system_hoisted(self):
        body = {"messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]}
        out = minimax_wire.to_anthropic_body(body)
        assert out["system"] == "sys"
        assert out["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]}]

    def test_tools_and_choice(self):
        body = {
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"type": "function", "function": {
                "name": "f", "description": "d",
                "parameters": {"type": "object", "properties": {}}}}],
            "tool_choice": "required",
        }
        out = minimax_wire.to_anthropic_body(body)
        assert out["tools"][0]["name"] == "f"
        assert out["tools"][0]["input_schema"] == {"type": "object", "properties": {}}
        assert out["tool_choice"] == {"type": "any"}

    def test_tool_choice_none_omitted(self):
        out = minimax_wire.to_anthropic_body({
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"type": "function", "function": {"name": "f"}}],
            "tool_choice": "none",
        })
        assert "tool_choice" not in out

    def test_reasoning_effort(self):
        out = minimax_wire.to_anthropic_body({
            "messages": [{"role": "user", "content": "x"}],
            "reasoning_effort": "high",
        })
        assert out["thinking"] == {"type": "adaptive"}
        assert out["output_config"] == {"effort": "high"}

    def test_default_max_tokens(self):
        out = minimax_wire.to_anthropic_body({"messages": [
            {"role": "user", "content": "x"}]})
        assert out["max_tokens"] == minimax_wire._DEFAULT_MAX_TOKENS

    def test_tool_result_mapping(self):
        body = {"messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "f", "arguments": '{"a":1}'}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ]}
        out = minimax_wire.to_anthropic_body(body)
        blocks = out["messages"][1]["content"]
        assert blocks[0]["type"] == "tool_use"
        assert blocks[0]["input"] == {"a": 1}
        last = out["messages"][-1]["content"][0]
        assert last["type"] == "tool_result"
        assert last["tool_use_id"] == "call_1"

    def test_image_data_url(self):
        out = minimax_wire.to_anthropic_body({"messages": [{
            "role": "user", "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64,AAAA"}},
            ]}]})
        parts = out["messages"][0]["content"]
        assert parts[1]["type"] == "image"
        assert parts[1]["source"]["media_type"] == "image/png"
        assert parts[1]["source"]["data"] == "AAAA"


class TestWireResponse:
    SSE = [
        ("event: message_start",
         'data: {"type":"message_start","message":{"id":"msg_1",'
         '"usage":{"input_tokens":11}}}'),
        ("event: content_block_start",
         'data: {"type":"content_block_start","index":0,'
         '"content_block":{"type":"text","text":""}}'),
        ("event: content_block_delta",
         'data: {"type":"content_block_delta","index":0,'
         '"delta":{"type":"text_delta","text":"收到"}}'),
        ("event: message_delta",
         'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
         '"usage":{"output_tokens":2}}'),
        ("event: message_stop", 'data: {"type":"message_stop"}'),
    ]

    def _run(self):
        conv = minimax_wire.AnthropicToOpenAISse("MiniMax-M3")
        out = []
        for ev, data in self.SSE:
            out.extend(conv.feed(ev))
            out.extend(conv.feed(data))
            out.extend(conv.feed(""))
        out.extend(conv.finish())
        return out

    def test_text_and_usage_and_done(self):
        frames = self._run()
        assert frames[-1] == "data: [DONE]"
        payloads = [json.loads(f[6:]) for f in frames if f.startswith("data: {")]
        text = "".join(
            c["choices"][0]["delta"].get("content", "")
            for c in payloads if c["choices"])
        assert text == "收到"
        usage = [c for c in payloads if c.get("usage")]
        assert usage[0]["usage"]["prompt_tokens"] == 11
        assert usage[0]["usage"]["completion_tokens"] == 2

    def test_stop_reason_mapping(self):
        frames = self._run()
        payloads = [json.loads(f[6:]) for f in frames if f.startswith("data: {")]
        fr = [c["choices"][0].get("finish_reason")
              for c in payloads if c["choices"]]
        assert "stop" in fr

    def test_done_once(self):
        conv = minimax_wire.AnthropicToOpenAISse("m")
        out = []
        for ev, data in self.SSE:
            out.extend(conv.feed(ev))
            out.extend(conv.feed(data))
            out.extend(conv.feed(""))
        out.extend(conv.finish())
        out.extend(conv.finish())
        assert out.count("data: [DONE]") == 1

    def test_thinking_delta(self):
        conv = minimax_wire.AnthropicToOpenAISse("m")
        frames = []
        frames += conv.feed('data: {"type":"message_start","message":'
                            '{"id":"x","usage":{"input_tokens":1}}}')
        frames += conv.feed("")
        frames += conv.feed('data: {"type":"content_block_delta","index":0,'
                            '"delta":{"type":"thinking_delta","thinking":"想"}}')
        frames += conv.feed("")
        payloads = [json.loads(f[6:]) for f in frames if f.startswith("data: {")]
        reasoning = [c["choices"][0]["delta"]["reasoning_content"]
                     for c in payloads if c["choices"]
                     and "reasoning_content" in c["choices"][0]["delta"]]
        assert reasoning == ["想"]

    def test_error_raises(self):
        conv = minimax_wire.AnthropicToOpenAISse("m")
        conv.feed('data: {"type":"error","error":{"type":"overloaded_error",'
                  '"message":"busy"}}')
        with pytest.raises(RuntimeError, match="minimax upstream error"):
            conv.feed("")

    def test_tool_use_block(self):
        conv = minimax_wire.AnthropicToOpenAISse("m")
        frames = []
        frames += conv.feed('data: {"type":"message_start","message":'
                            '{"id":"x","usage":{"input_tokens":1}}}')
        frames += conv.feed("")
        frames += conv.feed('data: {"type":"content_block_start","index":0,'
                            '"content_block":{"type":"tool_use","id":"tu_1",'
                            '"name":"get_time","input":{}}}')
        frames += conv.feed("")
        frames += conv.feed('data: {"type":"content_block_delta","index":0,'
                            '"delta":{"type":"input_json_delta",'
                            '"partial_json":"{\\"a\\":1}"}}')
        frames += conv.feed("")
        frames += conv.feed('data: {"type":"message_delta","delta":'
                            '{"stop_reason":"tool_use"}}')
        frames += conv.feed("")
        frames += conv.feed('data: {"type":"message_stop"}')
        frames += conv.feed("")
        payloads = [json.loads(f[6:]) for f in frames if f.startswith("data: {")]
        calls = [c["choices"][0]["delta"]["tool_calls"]
                 for c in payloads if c["choices"]
                 and "tool_calls" in c["choices"][0]["delta"]]
        assert calls[0][0]["function"]["name"] == "get_time"
        args = "".join(x[0]["function"].get("arguments", "") for x in calls)
        assert json.loads(args) == {"a": 1}
        fr = [c["choices"][0].get("finish_reason")
              for c in payloads if c["choices"]]
        assert "tool_calls" in fr
