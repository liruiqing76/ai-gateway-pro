# ============================================================
# Anthropic Messages 协议兼容层单元测试
#
# 覆盖 backend/core/anthropic_codec.py 的四个职责:
#   1. 请求转换: Anthropic body → OpenAI Chat Completions body
#   2. 流式渲染: OpenAI SSE 行 → Anthropic 事件帧(含工具/思考/降级收尾)
#   3. 非流式转换: OpenAI completion → Anthropic Message
#   4. count_tokens 本地估算
#
# 全部纯函数测试: 无网络、无 DB、无 FastAPI。
# 参考 dsh-our-free-model 插件 src/anthropic.js 的行为契约。
# ============================================================

import json

import pytest

from backend.core.anthropic_codec import (
    AnthropicStreamRenderer,
    count_tokens,
    from_anthropic_request,
    openai_completion_to_message,
)


# ------------------------------------------------------------
# 工具: 解析事件帧
# ------------------------------------------------------------

def parse_frames(frames: list[str]) -> list[tuple[str, dict]]:
    """['event: x\\ndata: {...}\\n\\n'] → [('x', {...})]"""
    out = []
    for f in frames:
        lines = f.strip().split("\n")
        event = lines[0][len("event: "):]
        data = json.loads(lines[1][len("data: "):])
        out.append((event, data))
    return out


def sse_chunk(delta=None, finish_reason=None, usage=None, model="test"):
    """构造一个 OpenAI SSE 数据行(模拟上游 chunk)。"""
    obj = {"id": "c1", "object": "chat.completion.chunk", "model": model}
    if delta is not None:
        obj["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish_reason}]
    elif finish_reason is not None:
        obj["choices"] = [{"index": 0, "delta": {}, "finish_reason": finish_reason}]
    else:
        obj["choices"] = []
    if usage is not None:
        obj["usage"] = usage
    return f"data: {json.dumps(obj, ensure_ascii=False)}"


# ============================================================
# 1) 请求转换
# ============================================================

class TestFromAnthropicRequest:
    def test_minimal(self):
        body = {
            "model": "claude-sonnet-4-5",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "hi"}],
        }
        r = from_anthropic_request(body)
        assert "error" not in r
        assert r["model"] == "claude-sonnet-4-5"
        assert r["messages"] == [{"role": "user", "content": "hi"}]
        assert r["max_tokens"] == 1024

    def test_system_string(self):
        body = {
            "model": "m", "max_tokens": 10, "system": "You are helpful.",
            "messages": [{"role": "user", "content": "hi"}],
        }
        r = from_anthropic_request(body)
        assert r["messages"][0] == {"role": "system", "content": "You are helpful."}

    def test_system_blocks(self):
        body = {
            "model": "m", "max_tokens": 10,
            "system": [{"type": "text", "text": "Part 1."},
                       {"type": "text", "text": "Part 2."}],
            "messages": [{"role": "user", "content": "hi"}],
        }
        r = from_anthropic_request(body)
        assert r["messages"][0]["content"] == "Part 1.\n\nPart 2."

    def test_missing_model(self):
        r = from_anthropic_request({"max_tokens": 10, "messages": [
            {"role": "user", "content": "hi"}]})
        assert r["error"] == "`model` is required"

    def test_missing_messages(self):
        r = from_anthropic_request({"model": "m", "max_tokens": 10, "messages": []})
        assert r["error"] == "`messages` must be a non-empty array"

    def test_multimodal_image(self):
        body = {
            "model": "m", "max_tokens": 10,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "look"},
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png", "data": "AAAA"}},
            ]}],
        }
        r = from_anthropic_request(body)
        msg = r["messages"][0]
        assert msg["role"] == "user"
        assert isinstance(msg["content"], list)
        assert msg["content"][0] == {"type": "text", "text": "look"}
        assert msg["content"][1]["image_url"]["url"] == "data:image/png;base64,AAAA"

    def test_image_url_source(self):
        body = {
            "model": "m", "max_tokens": 10,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}},
            ]}],
        }
        r = from_anthropic_request(body)
        assert r["messages"][0]["content"][0]["image_url"]["url"] == "https://x/y.png"

    def test_tool_use_and_result_roundtrip(self):
        """assistant tool_use → tool_calls; user tool_result → role:tool 行"""
        body = {
            "model": "m", "max_tokens": 10,
            "messages": [
                {"role": "user", "content": "check weather"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
                     "input": {"city": "Beijing"}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1",
                     "content": "Sunny 25C"},
                ]},
            ],
        }
        r = from_anthropic_request(body)
        msgs = r["messages"]
        assert msgs[0] == {"role": "user", "content": "check weather"}
        # assistant 行
        a = msgs[1]
        assert a["role"] == "assistant"
        assert a["content"] is None or a["content"] is None or not a.get("content")
        tc = a["tool_calls"][0]
        assert tc["id"] == "toolu_1"
        assert tc["function"]["name"] == "get_weather"
        assert json.loads(tc["function"]["arguments"]) == {"city": "Beijing"}
        # tool 行
        t = msgs[2]
        assert t == {"role": "tool", "tool_call_id": "toolu_1", "content": "Sunny 25C"}

    def test_tool_result_image_goes_to_user_followup(self):
        body = {
            "model": "m", "max_tokens": 10,
            "messages": [
                {"role": "user", "content": "show"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "t1", "name": "read_image",
                     "input": {}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": [
                        {"type": "text", "text": "caption"},
                        {"type": "image", "source": {
                            "type": "base64", "media_type": "image/png", "data": "BBBB"}},
                    ]}]},
            ],
        }
        r = from_anthropic_request(body)
        # role:tool 文本行
        t = r["messages"][2]
        assert t["role"] == "tool" and t["tool_call_id"] == "t1"
        assert "caption" in t["content"]
        # 图片紧跟其后的 user 行
        u = r["messages"][3]
        assert u["role"] == "user"
        assert any(p.get("type") == "image_url" for p in u["content"])

    def test_thinking_blocks_dropped(self):
        """历史里的 thinking/redacted_thinking 不回传上游(OpenAI 面不吃)"""
        body = {
            "model": "m", "max_tokens": 10,
            "messages": [
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": "answer"}]},
                {"role": "user", "content": "next"},
            ],
        }
        r = from_anthropic_request(body)
        assert r["messages"][0]["content"] == "answer"

    def test_system_role_in_messages_hoisted(self):
        body = {
            "model": "m", "max_tokens": 10,
            "messages": [
                {"role": "system", "content": "be nice"},
                {"role": "user", "content": "hi"},
            ],
        }
        r = from_anthropic_request(body)
        roles = [m["role"] for m in r["messages"]]
        assert roles == ["system", "user"]

    def test_tools_mapping(self):
        body = {
            "model": "m", "max_tokens": 10,
            "messages": [{"role": "user", "content": "go"}],
            "tools": [{
                "name": "search",
                "description": "Search things",
                "input_schema": {"type": "object",
                                 "properties": {"q": {"type": "string"}}},
            }],
        }
        r = from_anthropic_request(body)
        tool = r["tools"][0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "search"
        assert tool["function"]["parameters"]["properties"]["q"]["type"] == "string"
        assert tool["function"]["description"] == "Search things"

    def test_tool_choice_mapping(self):
        base = {"model": "m", "max_tokens": 10,
                "messages": [{"role": "user", "content": "go"}],
                "tools": [{"name": "t", "description": "", "input_schema": {}}]}
        # specific tool
        b = dict(base)
        b["tool_choice"] = {"type": "tool", "name": "t"}
        r = from_anthropic_request(b)
        assert r["tool_choice"] == {"type": "function", "function": {"name": "t"}}
        # any
        b = dict(base)
        b["tool_choice"] = {"type": "any"}
        r = from_anthropic_request(b)
        assert r["tool_choice"] == "required"
        # auto
        b = dict(base)
        b["tool_choice"] = "auto"
        r = from_anthropic_request(b)
        assert r["tool_choice"] == "auto"
        # 不带 tool_choice: 不出现该键
        r = from_anthropic_request(base)
        assert "tool_choice" not in r

    def test_sampling_params(self):
        body = {
            "model": "m", "max_tokens": 100, "temperature": 0.7, "top_p": 0.9,
            "stop_sequences": ["END"],
            "messages": [{"role": "user", "content": "hi"}],
        }
        r = from_anthropic_request(body)
        assert r["max_tokens"] == 100
        assert r["temperature"] == 0.7
        assert r["top_p"] == 0.9
        assert r["stop"] == ["END"]

    def test_thinking_to_reasoning_effort(self):
        base = {"model": "m", "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}]}
        # 小预算 → low
        b = dict(base); b["thinking"] = {"type": "enabled", "budget_tokens": 2000}
        assert from_anthropic_request(b)["reasoning_effort"] == "low"
        # 中预算 → medium
        b = dict(base); b["thinking"] = {"type": "enabled", "budget_tokens": 8000}
        assert from_anthropic_request(b)["reasoning_effort"] == "medium"
        # 大预算 → high
        b = dict(base); b["thinking"] = {"type": "enabled", "budget_tokens": 32000}
        assert from_anthropic_request(b)["reasoning_effort"] == "high"
        # disabled → 不带该键
        b = dict(base); b["thinking"] = {"type": "disabled"}
        assert "reasoning_effort" not in from_anthropic_request(b)
        # 无 thinking → 不带该键
        assert "reasoning_effort" not in from_anthropic_request(base)

    def test_no_convertible_content(self):
        """只有 thinking 块的 assistant 历史 + 空 user → 拒绝"""
        r = from_anthropic_request({
            "model": "m", "max_tokens": 10,
            "messages": [{"role": "assistant", "content": [
                {"type": "thinking", "thinking": "only thinking"}]}],
        })
        assert r["error"] == "the request carried no convertible content"

    def test_stream_not_carried(self):
        """stream 由端点层决定, 不从 body 带入"""
        body = {"model": "m", "max_tokens": 10, "stream": True,
                "messages": [{"role": "user", "content": "hi"}]}
        r = from_anthropic_request(body)
        assert "stream" not in r


# ============================================================
# 2) 流式渲染
# ============================================================

class TestStreamRenderer:
    def test_text_stream(self):
        r = AnthropicStreamRenderer("msg_1", "m")
        frames = []
        frames += r.push_line(sse_chunk(delta={"content": "Hello"}))
        frames += r.push_line(sse_chunk(delta={"content": " world"}))
        frames += r.push_line(sse_chunk(finish_reason="stop"))
        frames += r.push_line("data: [DONE]")
        evs = parse_frames(frames)
        types = [e for e, _ in evs]
        assert types == [
            "message_start",              # 首个内容块前
            "content_block_start",        # text 块开
            "content_block_delta",        # Hello
            "content_block_delta",        # world
            "content_block_stop",         # [DONE] 收尾关块
            "message_delta",              # stop_reason end_turn
            "message_stop",
        ]
        start = evs[0][1]["message"]
        assert start["id"] == "msg_1" and start["model"] == "m"
        assert evs[1][1]["content_block"] == {"type": "text", "text": ""}
        assert evs[2][1]["delta"] == {"type": "text_delta", "text": "Hello"}
        assert evs[3][1]["delta"] == {"type": "text_delta", "text": " world"}
        assert evs[5][1]["delta"]["stop_reason"] == "end_turn"

    def test_thinking_then_text(self):
        r = AnthropicStreamRenderer("msg_1", "m")
        frames = []
        frames += r.push_line(sse_chunk(delta={"reasoning_content": "thinking..."}))
        frames += r.push_line(sse_chunk(delta={"content": "answer"}))
        frames += r.push_line("data: [DONE]")
        evs = parse_frames(frames)
        types = [e for e, _ in evs]
        # thinking 块开 → 关; text 块开 → …
        assert types == [
            "message_start",
            "content_block_start",       # thinking
            "content_block_delta",       # thinking_delta
            "content_block_stop",        # 切换前先关
            "content_block_start",       # text
            "content_block_delta",       # text_delta
            "content_block_stop",
            "message_delta",
            "message_stop",
        ]
        assert evs[1][1]["content_block"]["type"] == "thinking"
        assert evs[2][1]["delta"]["type"] == "thinking_delta"
        assert evs[4][1]["content_block"]["type"] == "text"
        # index 单调递增
        assert evs[1][1]["index"] == 0 and evs[4][1]["index"] == 1

    def test_reasoning_alias_field(self):
        """上游方言 reasoning(非 reasoning_content)也要识别"""
        r = AnthropicStreamRenderer("msg_1", "m")
        frames = r.push_line(sse_chunk(delta={"reasoning": "hmm"}))
        frames += r.push_line("data: [DONE]")
        evs = parse_frames(frames)
        assert evs[1][1]["content_block"]["type"] == "thinking"
        assert evs[2][1]["delta"] == {"type": "thinking_delta", "thinking": "hmm"}

    def test_tool_call_stream(self):
        r = AnthropicStreamRenderer("msg_1", "m")
        frames = []
        # 名字先到
        frames += r.push_line(sse_chunk(delta={"tool_calls": [{
            "index": 0, "id": "call_1", "type": "function",
            "function": {"name": "get_weather", "arguments": ""}}]}))
        # 参数分两片
        frames += r.push_line(sse_chunk(delta={"tool_calls": [{
            "index": 0, "function": {"arguments": '{"ci'}}]}))
        frames += r.push_line(sse_chunk(delta={"tool_calls": [{
            "index": 0, "function": {"arguments": 'ty":"BJ"}'}}]}))
        frames += r.push_line(sse_chunk(finish_reason="tool_calls"))
        frames += r.push_line("data: [DONE]")
        evs = parse_frames(frames)
        types = [e for e, _ in evs]
        assert types == [
            "message_start",
            "content_block_start",       # tool_use
            "content_block_delta",       # partial_json '{"ci'
            "content_block_delta",       # 'ty":"BJ"}'
            "content_block_stop",
            "message_delta",             # stop_reason tool_use
            "message_stop",
        ]
        cb = evs[1][1]["content_block"]
        assert cb == {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {}}
        assert evs[2][1]["delta"] == {"type": "input_json_delta", "partial_json": '{"ci'}
        assert evs[3][1]["delta"] == {"type": "input_json_delta", "partial_json": 'ty":"BJ"}'}
        assert evs[5][1]["delta"]["stop_reason"] == "tool_use"

    def test_tool_call_name_arrives_late(self):
        """参数先到、名字后到: 先攒参数, 宣告时一次吐出"""
        r = AnthropicStreamRenderer("msg_1", "m")
        frames = []
        frames += r.push_line(sse_chunk(delta={"tool_calls": [{
            "index": 0, "function": {"arguments": '{"a":'}}]}))
        frames += r.push_line(sse_chunk(delta={"tool_calls": [{
            "index": 0, "id": "call_9", "function": {"name": "tool_x",
                                                    "arguments": "1}"}}]}))
        frames += r.push_line("data: [DONE]")
        evs = parse_frames(frames)
        types = [e for e, _ in evs]
        # 名字未到不发任何帧
        assert types[0] == "message_start"
        assert types[1] == "content_block_start"   # 名字到了才宣告
        # 宣告时 held 的 '{"a":' 先于本次 '1}' 一起吐出
        deltas = [d["delta"] for e, d in evs if e == "content_block_delta"]
        assert deltas[0]["partial_json"] == '{"a":'
        assert deltas[1]["partial_json"] == "1}"
        cb = evs[1][1]["content_block"]
        assert cb["id"] == "call_9" and cb["name"] == "tool_x"
        # stop_reason: 有工具块 → tool_use
        md = [d for e, d in evs if e == "message_delta"][0]
        assert md["delta"]["stop_reason"] == "tool_use"

    def test_parallel_tool_calls(self):
        """两个 index 的并行调用各自独立开块"""
        r = AnthropicStreamRenderer("msg_1", "m")
        frames = []
        frames += r.push_line(sse_chunk(delta={"tool_calls": [{
            "index": 0, "id": "c1", "type": "function",
            "function": {"name": "a", "arguments": ""}}]}))
        frames += r.push_line(sse_chunk(delta={"tool_calls": [{
            "index": 1, "id": "c2", "type": "function",
            "function": {"name": "b", "arguments": ""}}]}))
        frames += r.push_line("data: [DONE]")
        evs = parse_frames(frames)
        starts = [d for e, d in evs if e == "content_block_start"]
        assert len(starts) == 2
        assert starts[0]["content_block"]["name"] == "a"
        assert starts[1]["content_block"]["name"] == "b"
        assert starts[0]["index"] != starts[1]["index"]

    def test_usage_reporting(self):
        r = AnthropicStreamRenderer("msg_1", "m")
        frames = []
        frames += r.push_line(sse_chunk(delta={"content": "hi"}))
        # 末块 usage(OpenAI stream_options.include_usage 形态)
        frames += r.push_line(sse_chunk(usage={
            "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}))
        frames += r.push_line("data: [DONE]")
        evs = parse_frames(frames)
        md = [d for e, d in evs if e == "message_delta"][0]
        assert md["usage"]["output_tokens"] == 5
        assert md["usage"]["input_tokens"] == 10

    def test_finish_reason_length(self):
        r = AnthropicStreamRenderer("msg_1", "m")
        frames = []
        frames += r.push_line(sse_chunk(delta={"content": "trunc"}))
        frames += r.push_line(sse_chunk(finish_reason="length"))
        frames += r.push_line("data: [DONE]")
        evs = parse_frames(frames)
        md = [d for e, d in evs if e == "message_delta"][0]
        assert md["delta"]["stop_reason"] == "max_tokens"

    def test_push_error_midstream(self):
        r = AnthropicStreamRenderer("msg_1", "m")
        frames = []
        frames += r.push_line(sse_chunk(delta={"content": "partial"}))
        frames += r.push_error("upstream died")
        evs = parse_frames(frames)
        types = [e for e, _ in evs]
        # error 后只有 message_stop, 没有 message_delta(不谎称正常结束)
        assert types == [
            "message_start", "content_block_start", "content_block_delta",
            "content_block_stop", "error", "message_stop",
        ]
        err = [d for e, d in evs if e == "error"][0]
        assert err["error"]["type"] == "api_error"
        assert err["error"]["message"] == "upstream died"
        assert not any(e == "message_delta" for e, _ in evs)

    def test_missing_done_uses_fallback_finish(self):
        """上游流耗尽没发 [DONE]: finish() 兜底收尾"""
        r = AnthropicStreamRenderer("msg_1", "m")
        frames = r.push_line(sse_chunk(delta={"content": "x"}))
        frames += r.finish()
        evs = parse_frames(frames)
        types = [e for e, _ in evs]
        assert "message_delta" in types and "message_stop" in types

    def test_double_done_idempotent(self):
        r = AnthropicStreamRenderer("msg_1", "m")
        frames = r.push_line("data: [DONE]")
        frames += r.push_line("data: [DONE]")
        frames += r.finish()
        evs = parse_frames(frames)
        stops = [e for e, _ in evs if e == "message_stop"]
        assert len(stops) == 1  # 不发第二个 message_stop

    def test_empty_stream_still_valid(self):
        """空流也走完整协议: message_start → message_delta → message_stop"""
        r = AnthropicStreamRenderer("msg_1", "m")
        frames = r.push_line("data: [DONE]")
        evs = parse_frames(frames)
        types = [e for e, _ in evs]
        assert types == ["message_start", "message_delta", "message_stop"]
        md = evs[1][1]
        assert md["delta"]["stop_reason"] == "end_turn"

    def test_non_data_lines_ignored(self):
        r = AnthropicStreamRenderer("msg_1", "m")
        assert r.push_line(": ping") == []
        assert r.push_line("") == []
        assert r.push_line(b"event: something") == []
        assert r.push_line("data: not-json") == []

    def test_bytes_input(self):
        r = AnthropicStreamRenderer("msg_1", "m")
        frames = r.push_line(
            sse_chunk(delta={"content": "b"}).encode("utf-8"))
        frames += r.push_line(b"data: [DONE]")
        evs = parse_frames(frames)
        assert evs[2][1]["delta"]["text"] == "b"


# ============================================================
# 3) 非流式转换
# ============================================================

class TestCompletionToMessage:
    def test_text_only(self):
        resp = {
            "id": "c1", "object": "chat.completion", "model": "prov · mdl",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "Hello!"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        m = openai_completion_to_message(resp, "requested-model", "msg_1")
        assert m["id"] == "msg_1"
        assert m["type"] == "message"
        assert m["role"] == "assistant"
        assert m["model"] == "requested-model"
        assert m["content"] == [{"type": "text", "text": "Hello!"}]
        assert m["stop_reason"] == "end_turn"
        assert m["stop_sequence"] is None
        assert m["usage"] == {"input_tokens": 10, "output_tokens": 5}

    def test_thinking_first_then_text(self):
        resp = {
            "choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": "ans",
                "reasoning_content": "think"}}],
            "usage": {},
        }
        m = openai_completion_to_message(resp, "m", "msg_1")
        assert m["content"][0] == {"type": "thinking", "thinking": "think"}
        assert m["content"][1] == {"type": "text", "text": "ans"}

    def test_tool_calls(self):
        resp = {
            "choices": [{"finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "get_weather",
                                 "arguments": '{"city": "BJ"}'}}]}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }
        m = openai_completion_to_message(resp, "m", "msg_1")
        tu = m["content"][0]
        assert tu["type"] == "tool_use"
        assert tu["id"] == "call_1"
        assert tu["name"] == "get_weather"
        assert tu["input"] == {"city": "BJ"}
        assert m["stop_reason"] == "tool_use"

    def test_tool_calls_bad_json_input(self):
        """参数坏 JSON → 空 dict(调用失败响亮, 不丢块)"""
        resp = {
            "choices": [{"finish_reason": "tool_calls", "message": {
                "tool_calls": [{"id": "c", "function": {
                    "name": "t", "arguments": "not-json"}}]}}],
        }
        m = openai_completion_to_message(resp, "m", "msg_1")
        assert m["content"][0]["input"] == {}

    def test_finish_length(self):
        resp = {"choices": [{"finish_reason": "length",
                             "message": {"content": "trunc"}}]}
        m = openai_completion_to_message(resp, "m", "msg_1")
        assert m["stop_reason"] == "max_tokens"

    def test_content_part_list(self):
        """上游 content 是部件列表的方言也支持"""
        resp = {
            "choices": [{"finish_reason": "stop", "message": {
                "content": [{"type": "text", "text": "a"},
                            {"type": "text", "text": "b"}]}}],
        }
        m = openai_completion_to_message(resp, "m", "msg_1")
        assert m["content"] == [{"type": "text", "text": "a"},
                                {"type": "text", "text": "b"}]

    def test_reasoning_alias(self):
        resp = {"choices": [{"finish_reason": "stop",
                             "message": {"content": "x", "reasoning": "r"}}]}
        m = openai_completion_to_message(resp, "m", "msg_1")
        assert m["content"][0] == {"type": "thinking", "thinking": "r"}

    def test_empty_usage(self):
        resp = {"choices": [{"message": {"content": "x"}}], "usage": None}
        m = openai_completion_to_message(resp, "m", "msg_1")
        assert m["usage"] == {"input_tokens": 0, "output_tokens": 0}


# ============================================================
# 4) count_tokens
# ============================================================

class TestCountTokens:
    def test_minimal_nonzero(self):
        r = count_tokens({"model": "m", "messages": [
            {"role": "user", "content": "hi"}]})
        assert r["input_tokens"] >= 16  # 基础开销保底非零

    def test_empty_body(self):
        r = count_tokens({})
        assert r["input_tokens"] == 16

    def test_system_counted(self):
        base = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
        short = count_tokens(base | {"system": "a" * 4})
        long_ = count_tokens(base | {"system": "a" * 40})
        assert long_["input_tokens"] > short["input_tokens"]

    def test_image_flat_cost(self):
        base = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
        base_tokens = count_tokens(base)["input_tokens"]
        with_img = count_tokens({"model": "m", "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "x"},
                {"type": "image", "source": {"type": "base64", "data": "A"*100}},
            ]}]})
        assert with_img["input_tokens"] >= base_tokens + 1024

    def test_tool_schema_counted(self):
        no_tools = count_tokens({"model": "m", "messages": [
            {"role": "user", "content": "x"}]})
        with_tools = count_tokens({
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"name": "search_tool", "description": "d" * 40,
                       "input_schema": {"type": "object", "properties": {
                           "q": {"type": "string"}}}}],
        })
        assert with_tools["input_tokens"] > no_tools["input_tokens"]

    def test_tool_use_and_result_counted(self):
        r = count_tokens({
            "model": "m",
            "messages": [
                {"role": "user", "content": [
                    {"type": "tool_use", "id": "t", "name": "name_tool",
                     "input": {"a": 1}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t",
                     "content": "result text"}]},
            ],
        })
        assert r["input_tokens"] > 16


# ============================================================
# 端点级冒烟: FastAPI app 能挂载 /v1/messages, 鉴权路径正确
# ============================================================

class TestEndpointMount:
    def test_router_paths(self):
        from backend.api.anthropic import router
        paths = {r.path for r in router.routes}
        # FastAPI 路由对象 path 已含 prefix
        assert "/v1/messages" in paths
        assert "/v1/messages/count_tokens" in paths
        assert router.prefix == "/v1"

    def test_error_shape_helper(self):
        from backend.api.anthropic import _anthropic_error
        resp = _anthropic_error(401, "no key")
        import json as _json
        body = _json.loads(resp.body.decode())
        assert body["type"] == "error"
        assert body["error"]["type"] == "authentication_error"
        assert body["error"]["message"] == "no key"

    def test_error_shape_helper_custom_type(self):
        from backend.api.anthropic import _anthropic_error
        resp = _anthropic_error(400, "bad", etype="invalid_request_error")
        import json as _json
        body = _json.loads(resp.body.decode())
        assert body["error"]["type"] == "invalid_request_error"

    def test_exception_handler_paths(self):
        """HTTPException 在 /v1/messages 路径渲染 Anthropic 形状, 其它路径保持 detail"""
        from fastapi.exceptions import HTTPException
        from backend.api.anthropic import anthropic_http_exception_handler

        class _FakeRequest:
            class _URL:
                def __init__(self, p):
                    self.path = p
            def __init__(self, p):
                self.url = self._URL(p)

        import json as _json
        exc = HTTPException(429, "quota", headers={"Retry-After": "30"})
        resp = asyncio_run(anthropic_http_exception_handler(_FakeRequest("/v1/messages"), exc))
        body = _json.loads(resp.body.decode())
        assert body["error"]["type"] == "rate_limit_error"
        assert resp.status_code == 429
        assert resp.headers["retry-after"] == "30"

        resp = asyncio_run(anthropic_http_exception_handler(_FakeRequest("/v1/chat/completions"), exc))
        body = _json.loads(resp.body.decode())
        assert body == {"detail": "quota"}


def asyncio_run(coro):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro) if hasattr(asyncio, "get_event_loop") else asyncio.run(coro)
