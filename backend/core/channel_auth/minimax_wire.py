# ============================================================
# MiniMax 推理面的协议桥
#
# MiniMax Code 的推理端点是 **Anthropic Messages 形状**(真机 200 实测:
# `POST https://agent.minimax.cn/mavis/api/v1/llm/v1/messages`, SSE 事件为
# message_start / content_block_start|delta|stop / message_delta / message_stop /
# ping / error), 而本网关的内部统一形状是 OpenAI Chat Completions。
#
# 所以这一层做双向桥:
#   to_anthropic_body(openai_body)   请求: OpenAI → Anthropic
#   AnthropicToOpenAISse             响应: Anthropic SSE 行 → OpenAI data 行
#
# 与 core/anthropic_codec.py 正好互为逆变换(那边是 Anthropic → OpenAI),
# 但两侧独立实现: 那边面向"对外提供 Anthropic 面", 这边面向"消费上游
# Anthropic 面", 字段容错策略不同(这边必须严格按上游真实事件流解析)。
# ============================================================

from __future__ import annotations

import json
from typing import Any

# OpenAI reasoning_effort → MiniMax output_config.effort
_EFFORT_MAP = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

_DEFAULT_MAX_TOKENS = 8192


def _is_obj(v: Any) -> bool:
    return isinstance(v, dict)


def _num(v: Any) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def _text_of(content: Any) -> str:
    """OpenAI content(字符串或部件列表) → 纯文本(用于 tool_result 等)。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for p in content:
        if isinstance(p, str):
            if p:
                parts.append(p)
        elif _is_obj(p) and p.get("type") == "text" and isinstance(p.get("text"), str):
            if p["text"]:
                parts.append(p["text"])
    return "\n".join(parts)


def _content_blocks(content: Any) -> list[dict]:
    """OpenAI content → Anthropic content 块列表(text / image)。"""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return []
    out: list[dict] = []
    for part in content:
        if isinstance(part, str):
            if part:
                out.append({"type": "text", "text": part})
            continue
        if not _is_obj(part):
            continue
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str) and part["text"]:
            out.append({"type": "text", "text": part["text"]})
        elif kind == "image_url":
            block = _image_block(part.get("image_url"))
            if block:
                out.append(block)
    return out


def _image_block(image_url: Any) -> dict | None:
    """OpenAI image_url 部件 → Anthropic image 块。"""
    url = image_url.get("url") if _is_obj(image_url) else image_url
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("data:"):
        # data:image/png;base64,xxxx
        try:
            header, _, data = url.partition(",")
            media = header[5:].split(";")[0] or "image/png"
            if not data:
                return None
            return {"type": "image",
                    "source": {"type": "base64", "media_type": media, "data": data}}
        except Exception:
            return None
    return {"type": "image", "source": {"type": "url", "url": url}}


def _parse_arguments(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if _is_obj(parsed) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def to_anthropic_body(openai_body: dict, *, stream: bool = True) -> dict:
    """OpenAI Chat Completions 请求体 → MiniMax(Anthropic Messages)请求体。"""
    body = openai_body if _is_obj(openai_body) else {}
    model = body.get("model") if isinstance(body.get("model"), str) else ""

    system_parts: list[str] = []
    messages: list[dict] = []
    pending_tool_results: list[dict] = []

    def flush_tool_results() -> None:
        if pending_tool_results:
            messages.append({"role": "user",
                             "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for row in body.get("messages") or []:
        if not _is_obj(row):
            continue
        role = row.get("role")
        content = row.get("content")

        if role in ("system", "developer"):
            text = _text_of(content).strip()
            if text:
                system_parts.append(text)
            continue

        if role == "tool":
            call_id = row.get("tool_call_id")
            flush_assistant_pending = _text_of(content)
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": str(call_id or ""),
                "content": [{"type": "text", "text": flush_assistant_pending}]
                if flush_assistant_pending else "",
            })
            continue

        flush_tool_results()

        if role == "assistant":
            blocks: list[dict] = []
            blocks.extend(_content_blocks(content))
            for tc in row.get("tool_calls") or []:
                if not _is_obj(tc):
                    continue
                fn = tc.get("function") if _is_obj(tc.get("function")) else {}
                name = fn.get("name") if isinstance(fn.get("name"), str) else ""
                if not name:
                    continue
                blocks.append({
                    "type": "tool_use",
                    "id": str(tc.get("id") or ""),
                    "name": name,
                    "input": _parse_arguments(fn.get("arguments")),
                })
            if blocks:
                messages.append({"role": "assistant", "content": blocks})
            continue

        # user / 其它
        blocks = _content_blocks(content)
        if blocks:
            messages.append({"role": "user", "content": blocks})

    flush_tool_results()

    if not messages:
        messages = [{"role": "user", "content": [{"type": "text", "text": "…"}]}]

    out: dict = {"model": model, "messages": messages, "stream": bool(stream)}

    if system_parts:
        out["system"] = "\n\n".join(system_parts)

    max_tokens = _num(body.get("max_tokens"))
    out["max_tokens"] = int(max_tokens) if max_tokens and max_tokens > 0 else _DEFAULT_MAX_TOKENS

    temperature = _num(body.get("temperature"))
    if temperature is not None:
        out["temperature"] = temperature

    top_p = _num(body.get("top_p"))
    if top_p is not None:
        out["top_p"] = top_p

    stop = body.get("stop")
    if isinstance(stop, str) and stop:
        out["stop_sequences"] = [stop]
    elif isinstance(stop, list):
        seqs = [s for s in stop if isinstance(s, str) and s]
        if seqs:
            out["stop_sequences"] = seqs

    tools = []
    for tool in body.get("tools") or []:
        if not _is_obj(tool):
            continue
        fn = tool.get("function") if _is_obj(tool.get("function")) else tool
        name = fn.get("name") if isinstance(fn.get("name"), str) else ""
        if not name:
            continue
        schema = fn.get("parameters") if _is_obj(fn.get("parameters")) else {
            "type": "object", "properties": {}}
        tools.append({
            "name": name,
            "description": fn.get("description") if isinstance(fn.get("description"), str) else "",
            "input_schema": schema,
        })
    if tools:
        out["tools"] = tools
        tc = body.get("tool_choice")
        if isinstance(tc, str):
            if tc == "required":
                out["tool_choice"] = {"type": "any"}
            elif tc == "auto":
                out["tool_choice"] = {"type": "auto"}
            # "none": Anthropic 无对应值, 直接不发(上游默认 auto)
        elif _is_obj(tc) and tc.get("type") == "function":
            fn = tc.get("function") if _is_obj(tc.get("function")) else {}
            name = fn.get("name") if isinstance(fn.get("name"), str) else ""
            if name:
                out["tool_choice"] = {"type": "tool", "name": name}

    # reasoning_effort → thinking / output_config
    effort_raw = body.get("reasoning_effort")
    effort = _EFFORT_MAP.get(effort_raw) if isinstance(effort_raw, str) else None
    if effort:
        out["thinking"] = {"type": "adaptive"}
        out["output_config"] = {"effort": effort}

    return out


class AnthropicToOpenAISse:
    """把上游 Anthropic SSE 事件行翻译成标准 OpenAI chunk 的 `data:` 行。

    用法::

        conv = AnthropicToOpenAISse(model)
        for line in upstream_lines:      # 含空行(事件边界) 都要喂
            for out in conv.feed(line):
                yield out
        # 若上游没给 message_stop, finish() 会补 [DONE]

    产出行形如 ``data: {...}`` 与末尾 ``data: [DONE]``。
    """

    def __init__(self, model: str):
        self.model = model
        self.message_id = f"chatcmpl-mm{id(self):x}"
        self.created = 0
        self.started = False
        self.done = False
        self._event: str | None = None
        self._data: list[str] = []
        self._block_kinds: dict[int, str] = {}     # content block index → kind
        self._tool_index: dict[int, int] = {}      # block index → tool_calls index
        self._next_tool_index = 0
        self._finish_reason: str | None = None
        self._usage: dict = {}

    # ---- OpenAI chunk 原语 ----

    def _chunk(self, delta: dict, finish_reason: str | None = None,
               usage: dict | None = None) -> str:
        obj: dict = {
            "id": self.message_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if usage:
            obj["usage"] = usage
        return "data: " + json.dumps(obj, ensure_ascii=False)

    def _usage_chunk(self, usage: dict) -> str:
        obj = {
            "id": self.message_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [],
            "usage": usage,
        }
        return "data: " + json.dumps(obj, ensure_ascii=False)

    def _ensure_role(self, out: list[str]) -> None:
        if not self.started:
            self.started = True
            out.append(self._chunk({"role": "assistant", "content": ""}))

    # ---- 事件分派 ----

    def feed(self, line: Any) -> list[str]:
        """喂一行(含空行)。返回产出的 OpenAI data 行列表。"""
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        raw = line.rstrip("\r\n")

        if raw == "":
            # 空行 = 一个 SSE 事件结束, 派发累积的 event/data
            return self._dispatch()

        if raw.startswith(":"):
            return []          # 注释/心跳

        if raw.startswith("event:"):
            self._event = raw[6:].strip()
            return []

        if raw.startswith("data:"):
            self._data.append(raw[5:].strip())
            return []

        # 少数上游直接发裸 JSON 行
        if raw.startswith("{"):
            self._data.append(raw)
            return []
        return []

    def _dispatch(self) -> list[str]:
        if not self._data:
            self._event = None
            return []
        payload_text = "\n".join(self._data)
        self._data = []
        event = self._event
        self._event = None

        if payload_text.strip() == "[DONE]":
            return self._on_message_stop()

        try:
            obj = json.loads(payload_text)
        except (json.JSONDecodeError, ValueError):
            return []
        if not _is_obj(obj):
            return []

        kind = obj.get("type") or event or ""
        handler = {
            "message_start": self._on_message_start,
            "content_block_start": self._on_block_start,
            "content_block_delta": self._on_block_delta,
            "content_block_stop": self._on_block_stop,
            "message_delta": self._on_message_delta,
            "message_stop": self._on_message_stop,
            "error": self._on_error,
        }.get(kind)
        if handler is None:
            return []
        return handler(obj)

    # ---- 各事件 ----

    def _on_message_start(self, obj: dict) -> list[str]:
        msg = obj.get("message") if _is_obj(obj.get("message")) else {}
        mid = msg.get("id")
        if isinstance(mid, str) and mid:
            self.message_id = f"chatcmpl-{mid}"
        created = msg.get("created")
        if isinstance(created, (int, float)) and not isinstance(created, bool):
            self.created = int(created)
        usage = msg.get("usage") if _is_obj(msg.get("usage")) else {}
        pt = _num(usage.get("input_tokens"))
        if pt is not None:
            self._usage["prompt_tokens"] = int(pt)
        out: list[str] = []
        self._ensure_role(out)
        return out

    def _on_block_start(self, obj: dict) -> list[str]:
        index = obj.get("index")
        index = int(index) if isinstance(index, (int, float)) and not isinstance(index, bool) else 0
        block = obj.get("content_block") if _is_obj(obj.get("content_block")) else {}
        btype = block.get("type")
        self._block_kinds[index] = btype or "text"
        out: list[str] = []
        self._ensure_role(out)
        if btype == "tool_use":
            slot = self._next_tool_index
            self._next_tool_index += 1
            self._tool_index[index] = slot
            out.append(self._chunk({"tool_calls": [{
                "index": slot,
                "id": str(block.get("id") or ""),
                "type": "function",
                "function": {"name": str(block.get("name") or ""), "arguments": ""},
            }]}))
        return out

    def _on_block_delta(self, obj: dict) -> list[str]:
        index = obj.get("index")
        index = int(index) if isinstance(index, (int, float)) and not isinstance(index, bool) else 0
        delta = obj.get("delta") if _is_obj(obj.get("delta")) else {}
        dtype = delta.get("type")
        out: list[str] = []
        self._ensure_role(out)

        if dtype == "text_delta":
            text = delta.get("text")
            if isinstance(text, str) and text:
                out.append(self._chunk({"content": text}))
        elif dtype == "thinking_delta":
            thinking = delta.get("thinking")
            if isinstance(thinking, str) and thinking:
                out.append(self._chunk({"reasoning_content": thinking}))
        elif dtype == "input_json_delta":
            partial = delta.get("partial_json")
            if isinstance(partial, str) and partial:
                slot = self._tool_index.get(index)
                if slot is None:
                    slot = self._next_tool_index
                    self._next_tool_index += 1
                    self._tool_index[index] = slot
                out.append(self._chunk({"tool_calls": [{
                    "index": slot,
                    "function": {"arguments": partial},
                }]}))
        return out

    def _on_block_stop(self, obj: dict) -> list[str]:
        return []

    def _on_message_delta(self, obj: dict) -> list[str]:
        delta = obj.get("delta") if _is_obj(obj.get("delta")) else {}
        stop_reason = delta.get("stop_reason")
        self._finish_reason = {
            "end_turn": "stop",
            "stop_sequence": "stop",
            "max_tokens": "length",
            "tool_use": "tool_calls",
            "refusal": "content_filter",
        }.get(stop_reason, "stop") if isinstance(stop_reason, str) else None
        usage = obj.get("usage") if _is_obj(obj.get("usage")) else {}
        pt = _num(usage.get("input_tokens"))
        ot = _num(usage.get("output_tokens"))
        if pt is not None:
            self._usage["prompt_tokens"] = int(pt)
        if ot is not None:
            self._usage["completion_tokens"] = int(ot)
        return []

    def _on_message_stop(self, obj: dict | None = None) -> list[str]:
        if self.done:
            return []
        self.done = True
        out: list[str] = []
        self._ensure_role(out)
        reason = self._finish_reason or "stop"
        out.append(self._chunk({}, finish_reason=reason))
        if self._usage:
            total = (self._usage.get("prompt_tokens", 0)
                     + self._usage.get("completion_tokens", 0))
            usage = dict(self._usage)
            usage.setdefault("total_tokens", total)
            out.append(self._usage_chunk(usage))
        out.append("data: [DONE]")
        return out

    def _on_error(self, obj: dict) -> list[str]:
        err = obj.get("error") if _is_obj(obj.get("error")) else {}
        message = err.get("message") if isinstance(err.get("message"), str) else ""
        etype = err.get("type") if isinstance(err.get("type"), str) else "api_error"
        raise RuntimeError(f"minimax upstream error: {etype}: {message or 'unknown'}")

    # ---- 兜底 ----

    def finish(self) -> list[str]:
        """上游流意外结束(无 message_stop)时补收尾。"""
        if self.done:
            return []
        return self._on_message_stop()
