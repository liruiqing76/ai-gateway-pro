# -*- coding: utf-8 -*-
"""上游"非流式却回 SSE" / "只吃流式"的聚合还原测试。

实测背景:
- codearts 200 的 body 是单帧 SSE `data:{...}` → 原来被误判 upstream non-json;
- workbuddy/buddy 对非流式直接 400 {"code":11101,"msg":"Non-stream chat request
  is currently not supported"} → 必须强制 stream:true 再把 SSE 聚合回来。
"""
import json

import pytest

from backend.core.proxy import STREAM_ONLY_CHANNELS, _aggregate_openai_sse


def _sse(*objs):
    return "\n\n".join("data: " + json.dumps(o) for o in objs) + "\n\ndata: [DONE]\n\n"


class TestAggregateSse:
    def test_codearts_single_frame(self):
        text = 'data:{"id":"x1","object":"chat.completion","model":"glm-5.2",' \
               '"choices":[{"index":0,"message":{"role":"assistant",' \
               '"content":"OK"},"finish_reason":"stop"}]}'
        out = _aggregate_openai_sse(text)
        assert out is not None
        assert out["choices"][0]["message"]["content"] == "OK"
        assert out["choices"][0]["finish_reason"] == "stop"
        assert out["model"] == "glm-5.2"

    def test_delta_stream_joined(self):
        out = _aggregate_openai_sse(_sse(
            {"id": "a", "choices": [{"index": 0, "delta": {"role": "assistant",
                                                          "content": "你"}}]},
            {"id": "a", "choices": [{"index": 0, "delta": {"content": "好"}}]},
            {"id": "a", "choices": [{"index": 0, "delta": {},
                                     "finish_reason": "stop"}]},
        ))
        assert out["choices"][0]["message"]["content"] == "你好"
        assert out["choices"][0]["finish_reason"] == "stop"

    def test_reasoning_only_is_not_empty(self):
        out = _aggregate_openai_sse(_sse(
            {"id": "a", "choices": [{"index": 0,
                                     "delta": {"reasoning_content": "想一下"}}]},
            {"id": "a", "choices": [{"index": 0, "delta": {},
                                     "finish_reason": "stop"}]},
        ))
        msg = out["choices"][0]["message"]
        assert msg["reasoning_content"] == "想一下"
        assert msg["content"] == ""

    def test_tool_calls_merged_by_index(self):
        out = _aggregate_openai_sse(_sse(
            {"id": "a", "choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "read", "arguments": "{\"pa"}}]}}]},
            {"id": "a", "choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": "th\":\"a\"}"}}]}}]},
            {"id": "a", "choices": [{"index": 0, "delta": {},
                                     "finish_reason": "tool_calls"}]},
        ))
        calls = out["choices"][0]["message"]["tool_calls"]
        assert len(calls) == 1
        assert calls[0]["id"] == "call_1"
        assert calls[0]["function"]["name"] == "read"
        assert json.loads(calls[0]["function"]["arguments"]) == {"path": "a"}
        assert out["choices"][0]["finish_reason"] == "tool_calls"

    def test_usage_carried(self):
        out = _aggregate_openai_sse(_sse(
            {"id": "a", "choices": [{"index": 0, "delta": {"content": "hi"}}]},
            {"id": "a", "choices": [], "usage": {"prompt_tokens": 3,
                                                 "completion_tokens": 1,
                                                 "total_tokens": 4}},
        ))
        assert out["usage"]["total_tokens"] == 4

    def test_empty_stream_returns_none(self):
        assert _aggregate_openai_sse("data: [DONE]\n\n") is None
        assert _aggregate_openai_sse("") is None
        assert _aggregate_openai_sse("not sse at all") is None

    def test_error_frame_raises(self):
        text = 'data: {"code":"10605","message":"isQueued"}'
        with pytest.raises(RuntimeError):
            _aggregate_openai_sse(text)

    def test_stream_only_channels(self):
        assert "workbuddy" in STREAM_ONLY_CHANNELS
        assert "buddy" in STREAM_ONLY_CHANNELS
