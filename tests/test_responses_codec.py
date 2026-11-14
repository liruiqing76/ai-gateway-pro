"""Responses API 编解码测试(/v1/responses 的协议转换层)。

覆盖三类最容易写错的地方:
  1. 请求侧: `instructions` / `input` 三种项形状(function_call、
     function_call_output)/ 扁平 tools / max_output_tokens / reasoning.effort
  2. 非流式响应: message 与 function_call 项、截断 → incomplete
  3. 流式渲染: 事件顺序、output_index 分配、文本+工具混合、失败帧
"""
import json

import pytest

from backend.core.responses_codec import (
    ResponsesStreamRenderer,
    estimate_input_tokens,
    from_responses_request,
    openai_completion_to_response,
)


# ============================================================
# 请求转换
# ============================================================

class TestFromResponsesRequest:
    def test_minimal_string_input(self):
        out = from_responses_request({"model": "m", "input": "hi"})
        assert out["model"] == "m"
        assert out["messages"] == [{"role": "user", "content": "hi"}]
        assert out["stream"] is False

    def test_instructions_become_system(self):
        out = from_responses_request(
            {"model": "m", "instructions": "be terse", "input": "hi"})
        assert out["messages"][0] == {"role": "system", "content": "be terse"}
        assert out["messages"][1]["role"] == "user"

    def test_empty_instructions_skipped(self):
        out = from_responses_request({"model": "m", "instructions": "", "input": "hi"})
        assert all(m["role"] != "system" for m in out["messages"])

    def test_message_items_with_text_parts(self):
        out = from_responses_request({
            "model": "m",
            "input": [
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "a"},
                             {"type": "input_text", "text": "b"}]},
            ],
        })
        content = out["messages"][0]["content"]
        assert content == [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]

    def test_single_text_part_flattened(self):
        out = from_responses_request({
            "model": "m",
            "input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": "only"}]}],
        })
        assert out["messages"][0]["content"] == "only"

    def test_developer_role_mapped_to_system(self):
        out = from_responses_request({
            "model": "m",
            "input": [{"type": "message", "role": "developer",
                       "content": [{"type": "input_text", "text": "rules"}]}],
        })
        assert out["messages"][0] == {"role": "system", "content": "rules"}

    def test_input_image_becomes_image_url(self):
        out = from_responses_request({
            "model": "m",
            "input": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "look"},
                {"type": "input_image", "image_url": "data:image/png;base64,AAA"},
            ]}],
        })
        parts = out["messages"][0]["content"]
        assert parts[0] == {"type": "text", "text": "look"}
        assert parts[1]["type"] == "image_url"
        assert parts[1]["image_url"]["url"].startswith("data:image/png")

    def test_function_call_history_pairs_with_output(self):
        """历史里的工具调用 + 结果必须成对进 OpenAI 历史, 否则第二轮丢上下文。"""
        out = from_responses_request({
            "model": "m",
            "input": [
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "weather?"}]},
                {"type": "function_call", "call_id": "call_1",
                 "name": "get_weather", "arguments": '{"city":"Beijing"}'},
                {"type": "function_call_output", "call_id": "call_1",
                 "output": "18C clear"},
            ],
        })
        roles = [m["role"] for m in out["messages"]]
        assert roles == ["user", "assistant", "tool"]
        assistant = out["messages"][1]
        assert assistant["tool_calls"][0]["id"] == "call_1"
        assert assistant["tool_calls"][0]["function"]["name"] == "get_weather"
        assert assistant["tool_calls"][0]["function"]["arguments"] == '{"city":"Beijing"}'
        tool = out["messages"][2]
        assert tool["tool_call_id"] == "call_1"
        assert tool["content"] == "18C clear"

    def test_function_call_arguments_as_object_stringified(self):
        out = from_responses_request({
            "model": "m",
            "input": [
                {"type": "function_call", "call_id": "c", "name": "f",
                 "arguments": {"a": 1}},
                {"type": "function_call_output", "call_id": "c", "output": "ok"},
            ],
        })
        args = out["messages"][0]["tool_calls"][0]["function"]["arguments"]
        assert json.loads(args) == {"a": 1}

    def test_reasoning_items_dropped(self):
        out = from_responses_request({
            "model": "m",
            "input": [
                {"type": "reasoning", "summary": []},
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "hi"}]},
            ],
        })
        assert len(out["messages"]) == 1
        assert out["messages"][0]["role"] == "user"

    def test_flat_tools_converted(self):
        out = from_responses_request({
            "model": "m", "input": "hi",
            "tools": [{"type": "function", "name": "get_weather",
                       "description": "d",
                       "parameters": {"type": "object", "properties": {}}}],
        })
        tool = out["tools"][0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "get_weather"
        assert tool["function"]["parameters"] == {"type": "object", "properties": {}}

    def test_hosted_tools_dropped(self):
        out = from_responses_request({
            "model": "m", "input": "hi",
            "tools": [{"type": "web_search"}, {"type": "function", "name": "keep"}],
        })
        assert [t["function"]["name"] for t in out["tools"]] == ["keep"]

    def test_tool_choice_function(self):
        out = from_responses_request({
            "model": "m", "input": "hi",
            "tools": [{"type": "function", "name": "f"}],
            "tool_choice": {"type": "function", "name": "f"},
        })
        assert out["tool_choice"] == {"type": "function", "function": {"name": "f"}}

    def test_tool_choice_string(self):
        out = from_responses_request({
            "model": "m", "input": "hi", "tools": [{"type": "function", "name": "f"}],
            "tool_choice": "required",
        })
        assert out["tool_choice"] == "required"

    def test_no_tools_means_no_tool_choice(self):
        out = from_responses_request({"model": "m", "input": "hi", "tool_choice": "auto"})
        assert "tool_choice" not in out

    def test_max_output_tokens_mapped(self):
        out = from_responses_request({"model": "m", "input": "hi", "max_output_tokens": 99})
        assert out["max_tokens"] == 99

    def test_reasoning_effort_flat_and_nested(self):
        assert from_responses_request(
            {"model": "m", "input": "hi", "reasoning_effort": "high"})["reasoning_effort"] == "high"
        assert from_responses_request(
            {"model": "m", "input": "hi", "reasoning": {"effort": "low"}})[
            "reasoning_effort"] == "low"

    def test_text_format_json_schema(self):
        out = from_responses_request({
            "model": "m", "input": "hi",
            "text": {"format": {"type": "json_schema", "name": "r",
                                "schema": {"type": "object"}}},
        })
        assert out["response_format"]["type"] == "json_schema"
        assert out["response_format"]["json_schema"]["schema"] == {"type": "object"}

    def test_stream_flag(self):
        assert from_responses_request(
            {"model": "m", "input": "hi", "stream": True})["stream"] is True

    def test_missing_model(self):
        assert "error" in from_responses_request({"input": "hi"})

    def test_missing_input(self):
        assert "error" in from_responses_request({"model": "m"})

    def test_not_object(self):
        assert "error" in from_responses_request(["nope"])


# ============================================================
# 非流式响应
# ============================================================

class TestNonStreamResponse:
    def _completion(self, content=None, tool_calls=None, finish="stop", usage=None):
        msg = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return {"choices": [{"message": msg, "finish_reason": finish}],
                "usage": usage or {"prompt_tokens": 5, "completion_tokens": 2,
                                   "total_tokens": 7}}

    def test_text_message(self):
        body = openai_completion_to_response(
            self._completion("hello"), "m", "resp_1", 123)
        assert body["object"] == "response"
        assert body["id"] == "resp_1"
        assert body["status"] == "completed"
        assert body["output"][0]["type"] == "message"
        assert body["output"][0]["content"][0]["text"] == "hello"
        assert body["usage"]["input_tokens"] == 5
        assert body["usage"]["output_tokens"] == 2

    def test_function_call_item(self):
        body = openai_completion_to_response(
            self._completion(None, [{"id": "call_9", "type": "function",
                                     "function": {"name": "f", "arguments": '{"x":1}'}}],
                             finish="tool_calls"),
            "m", "resp_1", 0)
        item = body["output"][0]
        assert item["type"] == "function_call"
        assert item["call_id"] == "call_9"
        assert item["name"] == "f"
        assert json.loads(item["arguments"]) == {"x": 1}
        assert body["status"] == "completed"

    def test_length_marks_incomplete(self):
        body = openai_completion_to_response(
            self._completion("cut", finish="length"), "m", "resp_1", 0)
        assert body["status"] == "incomplete"
        assert body["incomplete_details"] == {"reason": "max_output_tokens"}

    def test_text_and_tool_call_together(self):
        body = openai_completion_to_response(
            self._completion("thinking out loud",
                             [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}],
                             finish="tool_calls"),
            "m", "resp_1", 0)
        assert [o["type"] for o in body["output"]] == ["message", "function_call"]

    def test_empty_completion(self):
        body = openai_completion_to_response({}, "m", "resp_1", 0)
        assert body["output"] == []
        assert body["status"] == "completed"


# ============================================================
# 流式渲染
# ============================================================

def _lines(*chunks):
    return [f"data: {json.dumps(c)}" for c in chunks]


def _events(frames):
    """帧字符串 → (event 名, payload) 列表。"""
    out = []
    for frame in frames:
        lines = [ln for ln in frame.split("\n") if ln]
        name = None
        payload = None
        for ln in lines:
            if ln.startswith("event: "):
                name = ln[7:]
            elif ln.startswith("data: "):
                payload = json.loads(ln[6:])
        out.append((name, payload))
    return out


class TestStreamRenderer:
    def _render(self, chunks, done=True):
        r = ResponsesStreamRenderer("resp_1", "m", 0)
        frames = []
        for line in _lines(*chunks):
            frames.extend(r.push_line(line))
        if done:
            frames.extend(r.push_line("data: [DONE]"))
        return frames, r

    def test_text_stream_event_order(self):
        frames, _ = self._render([
            {"choices": [{"delta": {"content": "he"}}]},
            {"choices": [{"delta": {"content": "llo"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ])
        names = [n for n, _ in _events(frames)]
        assert names[0] == "response.created"
        assert names.count("response.created") == 1
        assert "response.output_item.added" in names
        assert names.count("response.output_text.delta") == 2
        assert names[-1] == "response.completed"

    def test_text_accumulated_in_completed(self):
        frames, _ = self._render([
            {"choices": [{"delta": {"content": "he"}}]},
            {"choices": [{"delta": {"content": "llo"}}]},
        ])
        completed = [p for n, p in _events(frames) if n == "response.completed"][0]
        assert completed["response"]["output"][0]["content"][0]["text"] == "hello"
        assert completed["response"]["status"] == "completed"

    def test_single_output_index_for_message(self):
        frames, _ = self._render([
            {"choices": [{"delta": {"content": "a"}}]},
            {"choices": [{"delta": {"content": "b"}}]},
        ])
        added = [p for n, p in _events(frames) if n == "response.output_item.added"]
        assert len(added) == 1
        assert added[0]["output_index"] == 0

    def test_tool_call_stream(self):
        frames, _ = self._render([
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1",
                 "function": {"name": "get_weather", "arguments": '{"city"'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": ':"Beijing"}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ])
        evs = _events(frames)
        names = [n for n, _ in evs]
        assert "response.function_call_arguments.delta" in names
        done = [p for n, p in evs if n == "response.output_item.done"]
        assert done and done[0]["item"]["type"] == "function_call"
        assert done[0]["item"]["name"] == "get_weather"
        assert json.loads(done[0]["item"]["arguments"]) == {"city": "Beijing"}
        assert done[0]["item"]["call_id"] == "call_1"

    def test_mixed_text_and_tool_get_distinct_indexes(self):
        """写死 index=0 会让文本与工具调用互相覆盖。"""
        frames, _ = self._render([
            {"choices": [{"delta": {"content": "let me check"}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "function": {"name": "f", "arguments": "{}"}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ])
        added = [p for n, p in _events(frames) if n == "response.output_item.added"]
        assert len(added) == 2
        assert added[0]["output_index"] != added[1]["output_index"]

    def test_usage_reported(self):
        frames, _ = self._render([
            {"choices": [{"delta": {"content": "x"}}]},
            {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 3,
                                      "total_tokens": 14}},
        ])
        completed = [p for n, p in _events(frames) if n == "response.completed"][0]
        assert completed["response"]["usage"]["input_tokens"] == 11
        assert completed["response"]["usage"]["output_tokens"] == 3
        assert completed["response"]["usage"]["total_tokens"] == 14

    def test_length_incomplete(self):
        frames, _ = self._render([
            {"choices": [{"delta": {"content": "x"}}]},
            {"choices": [{"delta": {}, "finish_reason": "length"}]},
        ])
        completed = [p for n, p in _events(frames) if n == "response.completed"][0]
        assert completed["response"]["status"] == "incomplete"
        assert completed["response"]["incomplete_details"]["reason"] == "max_output_tokens"

    def test_done_idempotent(self):
        r = ResponsesStreamRenderer("resp_1", "m", 0)
        for line in _lines({"choices": [{"delta": {"content": "x"}}]}):
            r.push_line(line)
        first = r.push_line("data: [DONE]")
        second = r.push_line("data: [DONE]")
        assert first
        assert second == []

    def test_finish_without_done(self):
        r = ResponsesStreamRenderer("resp_1", "m", 0)
        for line in _lines({"choices": [{"delta": {"content": "x"}}]}):
            r.push_line(line)
        frames = r.finish()
        assert [n for n, _ in _events(frames)][-1] == "response.completed"

    def test_push_error_no_completed(self):
        r = ResponsesStreamRenderer("resp_1", "m", 0)
        frames = r.push_error("boom")
        names = [n for n, _ in _events(frames)]
        assert "response.failed" in names
        assert "response.completed" not in names

    def test_comment_and_garbage_ignored(self):
        r = ResponsesStreamRenderer("resp_1", "m", 0)
        assert r.push_line(": heartbeat") == []
        assert r.push_line("data: not-json") == []
        assert r.push_line("") == []

    def test_tool_args_held_until_name(self):
        """名字晚到: 参数先攒着, 名字到了再宣告并补发。"""
        r = ResponsesStreamRenderer("resp_1", "m", 0)
        frames = []
        frames.extend(r.push_line('data: {"choices":[{"delta":{"tool_calls":'
                                  '[{"index":0,"function":{"arguments":"{\\"a\\""}}]}}]}'))
        announced = [p for n, p in _events(frames) if n == "response.output_item.added"]
        assert announced == []          # 没名字就不宣告
        frames.extend(r.push_line('data: {"choices":[{"delta":{"tool_calls":'
                                  '[{"index":0,"function":{"name":"f","arguments":":1}"}}]}}]}'))
        done = r.push_line("data: [DONE]")
        items = [p for n, p in _events(frames + done) if n == "response.output_item.done"]
        assert items and items[0]["item"]["name"] == "f"
        assert json.loads(items[0]["item"]["arguments"]) == {"a": 1}

    def test_output_order_follows_announcement_not_type(self):
        """先工具调用、后文本时, output 数组必须按 output_index 排, 不是 message 优先。"""
        frames, _ = self._render([
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "function": {"name": "f", "arguments": "{}"}}]}}]},
            {"choices": [{"delta": {"content": "after the call"}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ])
        completed = [p for n, p in _events(frames) if n == "response.completed"][0]
        types = [row["type"] for row in completed["response"]["output"]]
        assert types == ["function_call", "message"]

        # done 帧的 output_index 也必须递增, 与 output 数组一一对应
        done_indexes = [p["output_index"] for n, p in _events(frames)
                        if n == "response.output_item.done"]
        assert done_indexes == sorted(done_indexes)

    def test_output_item_done_order_matches_output_array(self):
        frames, _ = self._render([
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "function": {"name": "f", "arguments": "{}"}}]}}]},
            {"choices": [{"delta": {"content": "text"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ])
        evs = _events(frames)
        done_items = [p["item"]["type"] for n, p in evs if n == "response.output_item.done"]
        completed = [p for n, p in evs if n == "response.completed"][0]
        assert done_items == [row["type"] for row in completed["response"]["output"]]


# ============================================================
# 本地 token 估算
# ============================================================

class TestEstimateInputTokens:
    def test_returns_positive(self):
        out = estimate_input_tokens({"messages": [{"role": "user", "content": "hello"}]})
        assert out["input_tokens"] > 0

    def test_longer_text_more_tokens(self):
        short = estimate_input_tokens({"messages": [{"role": "user", "content": "hi"}]})
        long = estimate_input_tokens(
            {"messages": [{"role": "user", "content": "hi" * 400}]})
        assert long["input_tokens"] > short["input_tokens"]

    def test_image_counted(self):
        plain = estimate_input_tokens({"messages": [{"role": "user", "content": "x"}]})
        with_img = estimate_input_tokens({"messages": [{"role": "user", "content": [
            {"type": "text", "text": "x"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
        ]}]})
        assert with_img["input_tokens"] - plain["input_tokens"] >= 1024

    def test_tools_counted(self):
        plain = estimate_input_tokens({"messages": [{"role": "user", "content": "x"}]})
        with_tools = estimate_input_tokens({
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"type": "function",
                       "function": {"name": "f", "description": "d" * 100,
                                    "parameters": {"type": "object"}}}],
        })
        assert with_tools["input_tokens"] > plain["input_tokens"]

    def test_empty_is_nonzero(self):
        assert estimate_input_tokens({})["input_tokens"] > 0

    def test_from_responses_end_to_end(self):
        converted = from_responses_request({"model": "m", "input": "hello world"})
        assert estimate_input_tokens(converted)["input_tokens"] > 0
