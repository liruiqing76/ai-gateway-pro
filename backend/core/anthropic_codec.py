# ============================================================
# 智能网关 — Anthropic Messages 协议编解码器（纯函数, 无 I/O）
#
# 参考 DeepSeek Harness 插件 dsh-our-free-model (src/anthropic.js) 的设计:
# 协议转换层不含任何网络/时钟/文件操作, 可独立单元测试。
#
# 职责:
#   1. 请求:  Anthropic Messages body → OpenAI Chat Completions body
#   2. 流式:  OpenAI SSE chunk → Anthropic 事件帧 (message_start /
#             content_block_start|delta|stop / message_delta / message_stop / error)
#   3. 非流式: OpenAI completion → Anthropic Message 对象
#   4. count_tokens: 本地估算(不花一次上游请求)
#
# 让网关"原生说 Anthropic"后, Claude Code / Anthropic SDK / Cline 等
# 客户端无需任何转换垫片即可直连。
# ============================================================

from __future__ import annotations

import json
from typing import Any

# ---- 本地 token 估算参数(与插件口径一致) ----
CHARS_PER_TOKEN = 4        # 英文散文约 4 字符/token
IMAGE_TOKENS = 1024        # base64 载荷无尺寸可计价, 平价 1024
TOOL_FRAME_TOKENS = 8      # 每个工具的框架开销
BASE_TOKENS = 16           # 固定请求开销: 空请求也不回 0(防客户端除零)

# Anthropic thinking.budget_tokens → OpenAI reasoning_effort 的分档阈值。
# 参考插件的分桶: <4096 → light, ≥4096 → medium, ≥16384 → high。
_BALANCED_AT = 4096
_DEEP_AT = 16384


def _is_obj(value: Any) -> bool:
    return isinstance(value, dict)


def _num(value: Any):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# ============================================================
# 1) 请求转换: Anthropic Messages → OpenAI Chat Completions
# ============================================================

def _flatten_system(system: Any) -> str:
    """Anthropic system(字符串或 text 块列表) → 一个字符串。"""
    if isinstance(system, str):
        return system
    if not isinstance(system, list):
        return ""
    parts: list[str] = []
    for block in system:
        if isinstance(block, str):
            if block:
                parts.append(block)
        elif _is_obj(block) and block.get("type") == "text" and isinstance(block.get("text"), str):
            if block["text"]:
                parts.append(block["text"])
    return "\n\n".join(parts)


def _image_part(source: Any) -> dict | None:
    """Anthropic image 块的 source → OpenAI image_url 部件。"""
    if not _is_obj(source):
        return None
    if source.get("type") == "base64" and isinstance(source.get("data"), str) and source["data"]:
        media = source.get("media_type") or "image/png"
        return {"type": "image_url", "image_url": {"url": f"data:{media};base64,{source['data']}"}}
    if source.get("type") == "url" and isinstance(source.get("url"), str) and source["url"]:
        return {"type": "image_url", "image_url": {"url": source["url"]}}
    return None


def _blocks_of(content: Any) -> list:
    """消息 content(字符串或块列表) → 块列表。"""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        return [b for b in content if isinstance(b, (dict, str))]
    return []


def _tool_result_parts(content: Any) -> list:
    """tool_result 的 content(字符串或块列表) → OpenAI 文本部件列表。"""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return []
    parts = []
    for block in content:
        if isinstance(block, str) and block:
            parts.append({"type": "text", "text": block})
        elif _is_obj(block) and block.get("type") == "text" and isinstance(block.get("text"), str):
            if block["text"]:
                parts.append({"type": "text", "text": block["text"]})
        elif _is_obj(block) and block.get("type") == "image":
            image = _image_part(block.get("source"))
            if image:
                parts.append(image)
    return parts


def _reasoning_effort_for(thinking: Any) -> str | None:
    """Anthropic thinking 参数 → OpenAI reasoning_effort(近似分档)。

    上游是各免费平台的 OpenAI 兼容面, 对 reasoning_effort 支持参差;
    不认识的字段大多忽略, 认识的(DeepSeek/GLM 等)会真实生效。"""
    if not _is_obj(thinking):
        return None
    if thinking.get("type") == "disabled":
        return None
    if thinking.get("type") != "enabled":
        return None
    budget = _num(thinking.get("budget_tokens"))
    if budget is not None and budget >= _DEEP_AT:
        return "high"
    if budget is not None and budget >= _BALANCED_AT:
        return "medium"
    return "low"


def from_anthropic_request(body: Any) -> dict | None:
    """Anthropic Messages 请求 → OpenAI Chat Completions 请求体。

    返回 OpenAI body(成功) 或 {"error": str}(失败)。
    转换规则(逐块):
      | Anthropic              | OpenAI                              |
      | ---------------------- | ----------------------------------- |
      | system(字符串/块列表)  | 前置 {role:'system'} 行             |
      | text                   | content 文本                        |
      | image(base64/url)      | image_url 部件                      |
      | tool_use               | assistant tool_calls                |
      | tool_result            | {role:'tool', tool_call_id} 行      |
      | thinking / redacted_   | 丢弃(OpenAI 面不回传推理历史)       |
      | tools[].input_schema   | tools[].function.parameters        |
    """
    if not _is_obj(body):
        return {"error": "the request body must be a JSON object"}
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        return {"error": "`model` is required"}
    if not isinstance(body.get("messages"), list) or not body["messages"]:
        return {"error": "`messages` must be a non-empty array"}

    system_text = _flatten_system(body.get("system"))
    messages: list[dict] = []

    if system_text:
        messages.append({"role": "system", "content": system_text})

    for row in body["messages"]:
        if not _is_obj(row):
            continue
        blocks = _blocks_of(row.get("content"))
        role = row.get("role")

        # Anthropic 线上协议没有 system role; 调用方硬塞的提为 system
        if role in ("system", "developer"):
            text = "\n\n".join(
                b if isinstance(b, str) else (b.get("text") or "")
                for b in blocks if isinstance(b, str) or _is_obj(b)
            ).strip()
            if text:
                messages.append({"role": "system", "content": text})
            continue

        if role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict] = []
            for block in blocks:
                if isinstance(block, str):
                    if block:
                        text_parts.append(block)
                    continue
                if not _is_obj(block):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"]:
                    text_parts.append(block["text"])
                elif block.get("type") == "tool_use":
                    tool_calls.append({
                        "id": str(block.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(block.get("name") or ""),
                            "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                        },
                    })
                # thinking / redacted_thinking: 不回传上游
            if text_parts or tool_calls:
                entry: dict = {"role": "assistant", "content": "\n\n".join(text_parts) or None}
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                messages.append(entry)
            continue

        # user(及其它角色): text / image / tool_result
        parts: list[dict] = []
        for block in blocks:
            if isinstance(block, str):
                if block:
                    parts.append({"type": "text", "text": block})
                continue
            if not _is_obj(block):
                continue
            if block.get("type") == "tool_result":
                call_id = str(block.get("tool_use_id") or "")
                inner = _tool_result_parts(block.get("content"))
                text_of_inner = "\n".join(
                    p["text"] for p in inner if p.get("type") == "text" and p.get("text")
                )
                content_str = text_of_inner or ("(see attached image)" if any(p.get("type") == "image_url" for p in inner) else "(no output)")
                messages.append({"role": "tool", "tool_call_id": call_id, "content": content_str})
                # tool_result 里的图片走 tool 行的文本说明; OpenAI 的 role:tool 只吃文本,
                # 图片部件紧跟其后的 user 消息下发(见下方 flush)
                for p in inner:
                    if p.get("type") == "image_url":
                        parts.append({"type": "text", "text": f"(tool {call_id} returned an image)"})
                        parts.append(p)
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"]:
                parts.append({"type": "text", "text": block["text"]})
            elif block.get("type") == "image":
                image = _image_part(block.get("source"))
                if image:
                    parts.append(image)
        if parts:
            messages.append({
                "role": "user",
                "content": parts if len(parts) > 1 or parts[0].get("type") != "text" else parts[0]["text"],
            })

    # 没有任何可转换内容: 发空列表上游必 400, 在这里拒绝
    if not messages or all(m["role"] == "system" for m in messages):
        return {"error": "the request carried no convertible content"}

    openai_body: dict = {"model": model.strip(), "messages": messages}

    # tools: input_schema → function.parameters
    tools = []
    if isinstance(body.get("tools"), list):
        for tool in body["tools"]:
            if not _is_obj(tool):
                continue
            name = str(tool.get("name") or "").strip()
            if not name:
                continue
            schema = tool.get("input_schema") if _is_obj(tool.get("input_schema")) else {"type": "object", "properties": {}}
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(tool.get("description") or ""),
                    "parameters": schema,
                },
            })
    if tools:
        openai_body["tools"] = tools
        # Anthropic 默认强制工具时用 tool_choice: {type:'tool', name}; 映射为 OpenAI 拼法
        tc = body.get("tool_choice")
        if _is_obj(tc) and tc.get("type") == "tool" and isinstance(tc.get("name"), str) and tc["name"]:
            openai_body["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
        elif _is_obj(tc) and tc.get("type") == "any":
            openai_body["tool_choice"] = "required"
        elif tc == "auto":
            openai_body["tool_choice"] = "auto"

    # max_tokens(Anthropic 必填; OpenAI 可缺省 → 用模型侧默认)
    max_tokens = _num(body.get("max_tokens"))
    if max_tokens is not None and max_tokens > 0:
        openai_body["max_tokens"] = int(max_tokens)

    temperature = _num(body.get("temperature"))
    if temperature is not None:
        openai_body["temperature"] = temperature

    top_p = _num(body.get("top_p"))
    if top_p is not None:
        openai_body["top_p"] = top_p

    stop = body.get("stop_sequences")
    if isinstance(stop, list) and stop:
        openai_body["stop"] = [str(s) for s in stop]

    # thinking → reasoning_effort(近似); 上游不认识该字段会忽略
    effort = _reasoning_effort_for(body.get("thinking"))
    if effort:
        openai_body["reasoning_effort"] = effort

    # stream 由调用方决定(网关侧统一设置), 不从 body 带入
    openai_body.pop("stream", None)
    return openai_body


# ============================================================
# 2) 流式渲染: OpenAI SSE chunk → Anthropic 事件帧
# ============================================================

def _sse_frame(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


class AnthropicStreamRenderer:
    """把上游 OpenAI SSE 流逐行翻译成 Anthropic 事件帧。

    用法::

        r = AnthropicStreamRenderer(message_id="msg_x", model="m")
        async for line in openai_sse_lines:          # b"data: {...}" 或 str
            for frame in r.push_line(line):
                yield frame.encode()
        for frame in r.finish():                     # 兜底收尾(上游缺 [DONE] 时)
            yield frame.encode()

    帧纪律(与插件 anthropic.js 一致):
      - message_start 只发一次, 先于任何内容帧;
      - 同一时刻至多一个 text/thinking 块打开, 切换先 close 再 open;
      - tool_use 块按 slot 独立开闭, 支持并行工具调用;
      - usage 挂在 message_start(input) 与 message_delta(output) 上;
      - error 后只发 message_stop, 不发 message_delta(不谎称正常结束)。
    """

    def __init__(self, message_id: str, model: str):
        self.message_id = message_id
        self.model = model
        self.started = False
        self.finished = False
        self.block_index = 0
        self.open_block: str | None = None     # 'text' | 'thinking'
        self.open_index: int | None = None
        # OpenAI tool_calls index → {announced, block, id, name, held}
        self.tool_slots: dict[int, dict] = {}
        self.input_tokens = 0
        self.output_tokens = 0
        self.finish_reason: str | None = None
        self.saw_tool_block = False

    # ---- 帧原语 ----

    def _message_start(self) -> list[str]:
        return [_sse_frame("message_start", {
            "type": "message_start",
            "message": {
                "id": self.message_id,
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": self.input_tokens, "output_tokens": 0},
            },
        })]

    def _ensure_started(self) -> list[str]:
        if not self.started:
            self.started = True
            return self._message_start()
        return []

    def _close_stream_block(self, frames: list[str]) -> None:
        if self.open_block in ("text", "thinking"):
            frames.append(_sse_frame("content_block_stop", {
                "type": "content_block_stop", "index": self.open_index,
            }))
            self.open_block = None
            self.open_index = None

    def _open_stream_block(self, frames: list[str], kind: str) -> None:
        if self.open_block == kind:
            return
        self._close_stream_block(frames)
        index = self.block_index
        self.block_index += 1
        self.open_index = index
        self.open_block = kind
        content_block = {"type": "text", "text": ""} if kind == "text" else {"type": "thinking", "thinking": ""}
        frames.append(_sse_frame("content_block_start", {
            "type": "content_block_start", "index": index, "content_block": content_block,
        }))

    def _tool_slot(self, index: int) -> dict:
        if index not in self.tool_slots:
            self.tool_slots[index] = {"announced": False, "block": None, "id": "", "name": "", "held": ""}
        return self.tool_slots[index]

    def _open_tool_block(self, frames: list[str], slot: dict) -> None:
        self._close_stream_block(frames)
        index = self.block_index
        self.block_index += 1
        slot["announced"] = True
        slot["block"] = index
        frames.append(_sse_frame("content_block_start", {
            "type": "content_block_start", "index": index,
            "content_block": {"type": "tool_use", "id": slot["id"], "name": slot["name"], "input": {}},
        }))
        if slot["held"]:
            frames.append(_sse_frame("content_block_delta", {
                "type": "content_block_delta", "index": index,
                "delta": {"type": "input_json_delta", "partial_json": slot["held"]},
            }))
            slot["held"] = ""

    def _close_tool_block(self, frames: list[str], slot: dict) -> None:
        if not slot.get("announced"):
            return
        frames.append(_sse_frame("content_block_stop", {
            "type": "content_block_stop", "index": slot["block"],
        }))
        slot["announced"] = False

    def _close_all(self, frames: list[str]) -> None:
        self._close_stream_block(frames)
        for slot in self.tool_slots.values():
            self._close_tool_block(frames, slot)

    def _has_tool_block(self) -> bool:
        return any(s.get("block") is not None for s in self.tool_slots.values())

    # ---- 输入: 一行 SSE ----

    def push_line(self, line: Any) -> list[str]:
        """喂入一行(bytes/str)。返回该行产生的完整事件帧字符串列表。"""
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        line = line.strip()
        if not line:
            return []
        if not line.startswith("data:"):
            return []  # 注释帧/心跳等非数据行
        payload = line[5:].strip()
        if payload == "[DONE]":
            return self._on_done()

        try:
            chunk = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return []
        if not _is_obj(chunk):
            return []

        frames: list[str] = []
        usage = chunk.get("usage")
        if _is_obj(usage):
            pt = _num(usage.get("prompt_tokens"))
            ct = _num(usage.get("completion_tokens"))
            if pt is not None:
                self.input_tokens = int(pt)
            if ct is not None:
                self.output_tokens = int(ct)

        choices = chunk.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else None
        if _is_obj(choice):
            delta = choice.get("delta") if _is_obj(choice.get("delta")) else {}
            # 文本增量
            text = delta.get("content")
            if isinstance(text, str) and text:
                frames.extend(self._ensure_started())
                self._open_stream_block(frames, "text")
                frames.append(_sse_frame("content_block_delta", {
                    "type": "content_block_delta", "index": self.open_index,
                    "delta": {"type": "text_delta", "text": text},
                }))
            # 思考增量(上游方言: reasoning_content / reasoning)
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                frames.extend(self._ensure_started())
                self._open_stream_block(frames, "thinking")
                frames.append(_sse_frame("content_block_delta", {
                    "type": "content_block_delta", "index": self.open_index,
                    "delta": {"type": "thinking_delta", "thinking": reasoning},
                }))
            # 工具调用增量
            tool_calls = delta.get("tool_calls")
            if isinstance(tool_calls, list):
                frames.extend(self._ensure_started())
                for tc in tool_calls:
                    if not _is_obj(tc):
                        continue
                    idx = int(_num(tc.get("index")) or 0)
                    slot = self._tool_slot(idx)
                    fn = tc.get("function") if _is_obj(tc.get("function")) else {}
                    name = fn.get("name") if isinstance(fn.get("name"), str) else None
                    args = fn.get("arguments") if isinstance(fn.get("arguments"), str) else None
                    cid = tc.get("id") if isinstance(tc.get("id"), str) else None
                    if cid and not slot["id"]:
                        slot["id"] = cid
                    if name:
                        slot["name"] = name
                    if slot["announced"]:
                        if args:
                            frames.append(_sse_frame("content_block_delta", {
                                "type": "content_block_delta", "index": slot["block"],
                                "delta": {"type": "input_json_delta", "partial_json": args},
                            }))
                        continue
                    # 名字未到: 先攒参数(Anthropic 的 tool_use 块必须有 name 才能宣告)
                    if not slot["name"]:
                        if args:
                            slot["held"] += args
                        continue
                    self._open_tool_block(frames, slot)
                    self.saw_tool_block = True
                    if args:
                        frames.append(_sse_frame("content_block_delta", {
                            "type": "content_block_delta", "index": slot["block"],
                            "delta": {"type": "input_json_delta", "partial_json": args},
                        }))
            # finish_reason
            fr = choice.get("finish_reason")
            if isinstance(fr, str) and fr:
                self.finish_reason = fr

        return frames

    def _on_done(self) -> list[str]:
        """data: [DONE] 到达 → 收尾帧。"""
        frames: list[str] = []
        if self.finished:
            return frames
        self.finished = True
        if not self.started:
            # 空流也按协议走完: message_start → (无内容) → message_delta → stop
            frames.extend(self._ensure_started())
        self._close_all(frames)
        stop_reason = self._map_stop_reason()
        frames.append(_sse_frame("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": (
                {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}
                if self.input_tokens > 0 else {"output_tokens": self.output_tokens}
            ),
        }))
        frames.append(_sse_frame("message_stop", {"type": "message_stop"}))
        return frames

    def _map_stop_reason(self) -> str:
        fr = self.finish_reason
        if fr == "length":
            return "max_tokens"
        if fr == "tool_calls" or (fr in (None, "stop") and self._has_tool_block()):
            # 模型出了工具块但上游没给 tool_calls 收尾: 仍按 tool_use 结束,
            # 否则客户端会把带调用的回复当纯文本。
            return "tool_use"
        return "end_turn"

    def push_error(self, message: str) -> list[str]:
        """流中途失败 → error 事件 + message_stop(无 message_delta)。"""
        frames: list[str] = []
        if self.finished:
            return frames
        self.finished = True
        if not self.started:
            frames.extend(self._ensure_started())
        self._close_all(frames)
        frames.append(_sse_frame("error", {
            "type": "error", "error": {"type": "api_error", "message": str(message)},
        }))
        frames.append(_sse_frame("message_stop", {"type": "message_stop"}))
        return frames

    def finish(self) -> list[str]:
        """上游流意外结束(无 [DONE])的兜底收尾。"""
        if self.finished:
            return []
        return self._on_done()


# ============================================================
# 3) 非流式: OpenAI completion → Anthropic Message
# ============================================================

def _parse_tool_input(arguments: Any) -> dict:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
            return parsed if _is_obj(parsed) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def openai_completion_to_message(resp: Any, model: str, message_id: str) -> dict:
    """OpenAI ChatCompletion(dict) → Anthropic Message 对象。

    内容顺序遵循 Anthropic 规则: thinking 在前, text 居中, tool_use 在后。
    """
    resp = resp if _is_obj(resp) else {}
    choices = resp.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and _is_obj(choices[0]) else {}
    message = choice.get("message") if _is_obj(choice.get("message")) else {}

    content: list[dict] = []
    thinking = message.get("reasoning_content") or message.get("reasoning")
    if isinstance(thinking, str) and thinking:
        content.append({"type": "thinking", "thinking": thinking})
    text = message.get("content")
    if isinstance(text, str) and text:
        content.append({"type": "text", "text": text})
    elif isinstance(text, list):
        for part in text:
            if _is_obj(part) and part.get("type") == "text" and isinstance(part.get("text"), str):
                content.append({"type": "text", "text": part["text"]})

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if not _is_obj(tc):
                continue
            fn = tc.get("function") if _is_obj(tc.get("function")) else {}
            content.append({
                "type": "tool_use",
                "id": str(tc.get("id") or ""),
                "name": str(fn.get("name") or ""),
                "input": _parse_tool_input(fn.get("arguments")),
            })

    usage = resp.get("usage") if _is_obj(resp.get("usage")) else {}
    pt = int(_num(usage.get("prompt_tokens")) or 0)
    ct = int(_num(usage.get("completion_tokens")) or 0)

    fr = choice.get("finish_reason")
    if fr == "length":
        stop_reason = "max_tokens"
    elif fr == "tool_calls" or any(b["type"] == "tool_use" for b in content):
        stop_reason = "tool_use"
    else:
        stop_reason = "end_turn"

    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": pt, "output_tokens": ct},
    }


# ============================================================
# 4) count_tokens: 本地估算
# ============================================================

def count_tokens(body: Any) -> dict:
    """POST /v1/messages/count_tokens 的本地估算。

    不打一次上游: 文本 4 字符/token、每图 1024、工具 schema 计字符、
    固定 16 基础开销。空请求也回非零值(防客户端除零)。
    """
    body = body if _is_obj(body) else {}
    text_chars = len(_flatten_system(body.get("system")))
    images = 0

    for row in body.get("messages") or []:
        if not _is_obj(row):
            continue
        for block in _blocks_of(row.get("content")):
            if isinstance(block, str):
                text_chars += len(block)
                continue
            if not _is_obj(block):
                continue
            btype = block.get("type")
            if btype == "text" and isinstance(block.get("text"), str):
                text_chars += len(block["text"])
            elif btype == "image":
                images += 1
            elif btype == "tool_use":
                text_chars += len(str(block.get("name") or ""))
                try:
                    text_chars += len(json.dumps(block.get("input") or {}, ensure_ascii=False))
                except (TypeError, ValueError):
                    pass
            elif btype == "tool_result":
                inner = block.get("content")
                if isinstance(inner, str):
                    text_chars += len(inner)
                elif isinstance(inner, list):
                    for b2 in inner:
                        if _is_obj(b2) and b2.get("type") == "text" and isinstance(b2.get("text"), str):
                            text_chars += len(b2["text"])
                        elif _is_obj(b2) and b2.get("type") == "image":
                            images += 1
            elif btype == "thinking" and isinstance(block.get("thinking"), str):
                text_chars += len(block["thinking"])
            elif btype == "redacted_thinking" and isinstance(block.get("data"), str):
                text_chars += len(block["data"])

    tool_chars = 0
    tool_count = 0
    for tool in body.get("tools") or []:
        if not _is_obj(tool):
            continue
        tool_count += 1
        tool_chars += len(str(tool.get("name") or "")) + len(str(tool.get("description") or ""))
        try:
            tool_chars += len(json.dumps(tool.get("input_schema") or {}, ensure_ascii=False))
        except (TypeError, ValueError):
            pass

    tool_tokens = 0
    if tool_count:
        tool_tokens = -(-tool_chars // CHARS_PER_TOKEN) + tool_count * TOOL_FRAME_TOKENS

    return {
        "input_tokens": BASE_TOKENS + (-(-text_chars // CHARS_PER_TOKEN)) + images * IMAGE_TOKENS + tool_tokens,
    }
