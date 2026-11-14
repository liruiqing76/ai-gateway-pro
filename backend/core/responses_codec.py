"""OpenAI Responses API 编解码(网关第三协议面)。

为什么需要它: 网关原先只有 `/v1/chat/completions`(OpenAI) 与 `/v1/messages`
(Anthropic) 两个面。Codex CLI 与一部分 IDE 插件只讲 Responses
(`wire_api = "responses"`), 指向网关会直接 404 —— 这是"每个工具都能用"里
缺的最后一块。参考实现见 dsh-our-free-model `src/forward.js` 的
`responsesEndpoint`(同一套 `input` / `instructions` / `output` 拼装规则)。

本模块是纯函数 + 一个流式渲染器, 不碰 DB/网络, 便于单测:
  - `from_responses_request(body)`  Responses 请求 → OpenAI Chat 请求
  - `ResponsesStreamRenderer`       OpenAI SSE 行 → Responses 事件帧
  - `openai_completion_to_response(resp, model, response_id)`  非流式

协议要点(容易写错的地方, 逐条对应实现):
  - `instructions` 是 system prompt 的 Responses 拼法, `input` 是消息/工具历史;
    两者都要进 OpenAI body, 只取一个会让端点"看起来兼容"却静默丢参数。
  - `input` 项有三种形状: message(role + content 部件)、function_call(历史里
    助手调过的工具)、function_call_output(工具结果)。漏掉后两种, 多轮工具
    调用会在第二轮丢掉工具历史。
  - `tools[].type == "function"` 在 Responses 里是**扁平的**(name/parameters
    直接在顶层), 不是 chat 的 `function` 子对象。
  - 收尾事件必须是 `response.completed`; 输出被 max_output_tokens 截断时要
    `status="incomplete"` + `incomplete_details.reason="max_output_tokens"`
    (插件注释里专门记过: 只认 completed 会把截断误判成异常并白重试两次)。
"""

from __future__ import annotations

import json
from typing import Any


def _is_obj(value: Any) -> bool:
    return isinstance(value, dict)


def _num(value: Any):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


# ============================================================
# 1) 请求: Responses → OpenAI Chat Completions
# ============================================================

_TEXT_PART_TYPES = ("input_text", "output_text", "text", "summary_text")


def _content_to_parts(content: Any) -> list[dict]:
    """Responses 的 content(字符串或部件数组) → OpenAI content 部件数组。

    只保留 chat 能表达的: 文本与图片。其余(文件引用/音频)降级成一句文本,
    以免整条消息被静默丢掉。
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return []

    parts: list[dict] = []
    for part in content:
        if isinstance(part, str):
            if part:
                parts.append({"type": "text", "text": part})
            continue
        if not _is_obj(part):
            continue
        ptype = part.get("type")
        if ptype in _TEXT_PART_TYPES or (ptype is None and isinstance(part.get("text"), str)):
            text = part.get("text")
            if isinstance(text, str) and text:
                parts.append({"type": "text", "text": text})
            continue
        if ptype in ("input_image", "image_url"):
            url = part.get("image_url")
            if _is_obj(url):
                url = url.get("url")
            if not isinstance(url, str) or not url:
                continue
            image: dict = {"url": url}
            detail = part.get("detail")
            if isinstance(detail, str) and detail:
                image["detail"] = detail
            parts.append({"type": "image_url", "image_url": image})
            continue
        if ptype in ("input_file", "file"):
            name = part.get("filename") or part.get("file_id") or "file"
            parts.append({"type": "text", "text": f"[attached file: {name}]"})
            continue
        # reasoning / refusal / 未知类型: 不进 OpenAI 历史
    return parts


def _flatten_parts(parts: list[dict]) -> Any:
    """单文本部件的消息压成字符串(多数上游对纯文本更宽容)。"""
    if not parts:
        return ""
    if len(parts) == 1 and parts[0].get("type") == "text":
        return parts[0]["text"]
    return parts


def _message_from_item(item: dict) -> dict | None:
    role = item.get("role")
    if role == "developer":
        role = "system"
    if role not in ("system", "user", "assistant"):
        return None
    parts = _content_to_parts(item.get("content"))
    if role == "assistant":
        text = "\n\n".join(p["text"] for p in parts if p.get("type") == "text" and p.get("text"))
        return {"role": "assistant", "content": text or None}
    return {"role": role, "content": _flatten_parts(parts)}


def _tool_specs(tools: Any) -> list[dict]:
    """Responses 的扁平工具定义 → chat 的 `{type:function, function:{...}}`。"""
    if not isinstance(tools, list):
        return []
    out: list[dict] = []
    for tool in tools:
        if not _is_obj(tool):
            continue
        ttype = tool.get("type")
        if ttype not in (None, "function"):
            # web_search / file_search / computer_use 等托管工具: chat 面无法表达,
            # 直接丢弃(与插件对非 function 工具的处理一致)。
            continue
        fn = tool.get("function") if _is_obj(tool.get("function")) else None
        if fn is not None:
            spec = dict(fn)
        else:
            spec = {
                "name": tool.get("name"),
                "description": tool.get("description"),
                "parameters": tool.get("parameters"),
            }
        if not isinstance(spec.get("name"), str) or not spec["name"]:
            continue
        clean: dict = {"name": spec["name"]}
        if isinstance(spec.get("description"), str) and spec["description"]:
            clean["description"] = spec["description"]
        if _is_obj(spec.get("parameters")):
            clean["parameters"] = spec["parameters"]
        out.append({"type": "function", "function": clean})
    return out


def _tool_choice(choice: Any) -> Any:
    """Responses 的 tool_choice → chat 的 tool_choice。"""
    if isinstance(choice, str):
        return choice if choice in ("auto", "none", "required") else None
    if not _is_obj(choice):
        return None
    if choice.get("type") == "function" or "name" in choice:
        name = choice.get("name") or (_is_obj(choice.get("function")) and choice["function"].get("name"))
        if isinstance(name, str) and name:
            return {"type": "function", "function": {"name": name}}
    return None


def _reasoning_effort(body: dict) -> str | None:
    effort = body.get("reasoning_effort")
    if isinstance(effort, str) and effort:
        return effort
    reasoning = body.get("reasoning")
    if _is_obj(reasoning):
        effort = reasoning.get("effort")
        if isinstance(effort, str) and effort:
            return effort
    return None


def from_responses_request(body: Any) -> dict | None:
    """Responses 请求 → OpenAI Chat Completions 请求体。

    返回 OpenAI body(成功) 或 {"error": str}(失败)。
    转换规则:
      | Responses                    | OpenAI Chat                        |
      | ---------------------------- | ---------------------------------- |
      | instructions                 | 前置 {role:'system'}               |
      | input(字符串)                | 单条 user 消息                     |
      | input[](message)             | 对应 role 的消息                   |
      | input[](function_call)       | assistant tool_calls               |
      | input[](function_call_output)| {role:'tool', tool_call_id}        |
      | input[](reasoning)           | 丢弃(OpenAI 面不回传推理历史)      |
      | tools[](扁平 function)       | tools[].function                   |
      | max_output_tokens            | max_tokens                         |
      | reasoning.effort             | reasoning_effort                   |
    """
    if not _is_obj(body):
        return {"error": "the request body must be a JSON object"}
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        return {"error": "`model` is required"}

    messages: list[dict] = []

    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    raw_input = body.get("input")
    if isinstance(raw_input, str):
        if raw_input:
            messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        # 历史里同一轮可能先有若干 assistant tool_calls 再跟 tool 结果;
        # chat 面要求 tool 结果紧跟其 assistant 行, 所以顺序原样保留即可。
        pending_calls: list[dict] = []
        for item in raw_input:
            if isinstance(item, str):
                if item:
                    messages.append({"role": "user", "content": item})
                continue
            if not _is_obj(item):
                continue
            itype = item.get("type")

            if itype in ("function_call", "custom_tool_call"):
                name = item.get("name")
                if not isinstance(name, str) or not name:
                    continue
                args = item.get("arguments")
                if _is_obj(args):
                    args = json.dumps(args, ensure_ascii=False)
                if not isinstance(args, str):
                    args = "{}"
                pending_calls.append({
                    "id": str(item.get("call_id") or item.get("id") or ""),
                    "type": "function",
                    "function": {"name": name, "arguments": args},
                })
                continue

            if itype in ("function_call_output", "custom_tool_call_output"):
                if pending_calls:
                    messages.append({"role": "assistant", "content": None,
                                     "tool_calls": pending_calls})
                    pending_calls = []
                call_id = str(item.get("call_id") or "")
                output = item.get("output")
                if _is_obj(output) or isinstance(output, list):
                    parts = _content_to_parts(output if isinstance(output, list) else [output])
                    output = "\n".join(p["text"] for p in parts if p.get("type") == "text")
                if not isinstance(output, str):
                    output = "" if output is None else str(output)
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "content": output or "(no output)"})
                continue

            if itype == "reasoning":
                continue

            entry = _message_from_item(item)
            if entry is None:
                continue
            if pending_calls:
                messages.append({"role": "assistant", "content": None,
                                 "tool_calls": pending_calls})
                pending_calls = []
            messages.append(entry)
        if pending_calls:
            messages.append({"role": "assistant", "content": None,
                             "tool_calls": pending_calls})
    elif raw_input is not None:
        messages.append({"role": "user", "content": str(raw_input)})

    if not messages:
        return {"error": "`input` must be a non-empty string or array"}

    out: dict = {
        "model": model.strip(),
        "messages": messages,
        "stream": body.get("stream") is True,
    }

    max_out = _num(body.get("max_output_tokens"))
    if max_out is None:
        max_out = _num(body.get("max_tokens"))
    if max_out is not None and max_out > 0:
        out["max_tokens"] = int(max_out)

    for key in ("temperature", "top_p", "user", "seed"):
        if key in body and body[key] is not None:
            out[key] = body[key]

    effort = _reasoning_effort(body)
    if effort:
        out["reasoning_effort"] = effort

    tools = _tool_specs(body.get("tools"))
    if tools:
        out["tools"] = tools
        choice = _tool_choice(body.get("tool_choice"))
        if choice is not None:
            out["tool_choice"] = choice
        if body.get("parallel_tool_calls") is not None:
            out["parallel_tool_calls"] = bool(body["parallel_tool_calls"])

    text_cfg = body.get("text")
    if _is_obj(text_cfg) and _is_obj(text_cfg.get("format")):
        fmt = text_cfg["format"]
        if fmt.get("type") == "json_object":
            out["response_format"] = {"type": "json_object"}
        elif fmt.get("type") == "json_schema" and _is_obj(fmt.get("schema")):
            out["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": fmt.get("name") or "response",
                    "schema": fmt["schema"],
                    **({"strict": fmt["strict"]} if "strict" in fmt else {}),
                },
            }

    return out


# ============================================================
# 2) 响应: 非流式
# ============================================================

def _usage_block(usage: Any) -> dict:
    usage = usage if _is_obj(usage) else {}
    pt = int(_num(usage.get("prompt_tokens")) or 0)
    ct = int(_num(usage.get("completion_tokens")) or 0)
    total = int(_num(usage.get("total_tokens")) or 0) or (pt + ct)
    details_in = usage.get("prompt_tokens_details")
    details_out = usage.get("completion_tokens_details")
    return {
        "input_tokens": pt,
        "output_tokens": ct,
        "total_tokens": total,
        "input_tokens_details": {
            "cached_tokens": int(_num((details_in or {}).get("cached_tokens")) or 0),
        },
        "output_tokens_details": {
            "reasoning_tokens": int(_num((details_out or {}).get("reasoning_tokens")) or 0),
        },
    }


def _status_and_details(finish_reason: Any) -> tuple[str, dict | None]:
    if finish_reason == "length":
        return "incomplete", {"reason": "max_output_tokens"}
    return "completed", None


def openai_completion_to_response(resp: Any, model: str, response_id: str,
                                  created_at: int) -> dict:
    """OpenAI completion → Responses 非流式响应体。"""
    resp = resp if _is_obj(resp) else {}
    choices = resp.get("choices") if isinstance(resp.get("choices"), list) else []
    choice = choices[0] if choices and _is_obj(choices[0]) else {}
    message = choice.get("message") if _is_obj(choice.get("message")) else {}

    output: list[dict] = []
    text = message.get("content")
    if isinstance(text, str) and text:
        output.append({
            "type": "message", "id": "msg_0", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        })
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for i, call in enumerate(tool_calls):
            if not _is_obj(call):
                continue
            fn = call.get("function") if _is_obj(call.get("function")) else {}
            name = fn.get("name")
            if not isinstance(name, str) or not name:
                continue
            args = fn.get("arguments")
            if not isinstance(args, str):
                args = json.dumps(args or {}, ensure_ascii=False)
            output.append({
                "type": "function_call",
                "id": f"fc_{i}",
                "call_id": str(call.get("id") or f"call_{i}"),
                "name": name,
                "arguments": args,
                "status": "completed",
            })

    status, incomplete = _status_and_details(choice.get("finish_reason"))
    body = {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "model": model,
        "status": status,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": _usage_block(resp.get("usage")),
    }
    if incomplete is not None:
        body["incomplete_details"] = incomplete
    return body


# ============================================================
# 3) 响应: 流式渲染器
# ============================================================

_CHARS_PER_TOKEN = 4
_IMAGE_TOKENS = 1024
_TOOL_FRAME_TOKENS = 8
_BASE_TOKENS = 16


def estimate_input_tokens(openai_body: Any) -> dict:
    """`POST /v1/responses/input_tokens` 的本地估算(不打上游)。

    与 Anthropic 面 `count_tokens` 同一口径(4 字符/token、每图 1024、
    每个工具帧 8、固定 16 基础), 但输入是**已转换的 OpenAI body** ——
    直接复用 Anthropic 的估算函数会把 messages 里的 OpenAI 部件当成
    Anthropic 块来数, 图片与工具 schema 都会漏算。
    """
    body = openai_body if _is_obj(openai_body) else {}
    text_chars = 0
    images = 0

    for row in body.get("messages") or []:
        if not _is_obj(row):
            continue
        content = row.get("content")
        if isinstance(content, str):
            text_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, str):
                    text_chars += len(part)
                elif _is_obj(part):
                    if part.get("type") == "text" and isinstance(part.get("text"), str):
                        text_chars += len(part["text"])
                    elif part.get("type") == "image_url":
                        images += 1
        for call in row.get("tool_calls") or []:
            if not _is_obj(call):
                continue
            fn = call.get("function") if _is_obj(call.get("function")) else {}
            text_chars += len(str(fn.get("name") or ""))
            args = fn.get("arguments")
            if isinstance(args, str):
                text_chars += len(args)

    tool_chars = 0
    tool_count = 0
    for tool in body.get("tools") or []:
        if not _is_obj(tool):
            continue
        fn = tool.get("function") if _is_obj(tool.get("function")) else tool
        if not _is_obj(fn):
            continue
        tool_count += 1
        tool_chars += len(str(fn.get("name") or "")) + len(str(fn.get("description") or ""))
        try:
            tool_chars += len(json.dumps(fn.get("parameters") or {}, ensure_ascii=False))
        except (TypeError, ValueError):
            pass

    tool_tokens = 0
    if tool_count:
        tool_tokens = -(-tool_chars // _CHARS_PER_TOKEN) + tool_count * _TOOL_FRAME_TOKENS

    return {
        "input_tokens": (
            _BASE_TOKENS
            + (-(-text_chars // _CHARS_PER_TOKEN))
            + images * _IMAGE_TOKENS
            + tool_tokens
        ),
    }


def _event(name: str, payload: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


class ResponsesStreamRenderer:
    """把上游 OpenAI SSE 行翻译成 Responses 事件帧。

    用法::

        r = ResponsesStreamRenderer(response_id="resp_x", model="m", created_at=0)
        async for line in openai_sse_lines:
            for frame in r.push_line(line):
                yield frame.encode()
        for frame in r.finish():      # 上游缺 [DONE] 时兜底收尾
            yield frame.encode()

    帧纪律:
      - `response.created` 只发一次, 先于任何 output 事件;
      - 每个 output 项先 `response.output_item.added`, 增量帧随后,
        收尾时 `...output_item.done`(客户端按 output_index 累积);
      - 文本与工具调用各自占一个 output_index, 按宣告顺序递增
        (写死 0 会在"文本 + 工具调用"混合的一轮里互相覆盖);
      - 最后一定是 `response.completed`(截断时 status=incomplete),
        流内失败则 `response.failed` 且不再发 completed。
    """

    def __init__(self, response_id: str, model: str, created_at: int):
        self.response_id = response_id
        self.model = model
        self.created_at = created_at
        self.started = False
        self.finished = False
        self.failed = False

        self.next_output_index = 0
        self.message_index: int | None = None
        self.message_text = ""
        self.message_closed = False
        # OpenAI tool_calls index → slot
        self.call_slots: dict[int, dict] = {}
        self.call_order: list[int] = []

        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.reasoning_tokens = 0
        self.finish_reason: str | None = None

    # ---- 公共帧 ----

    def _response_obj(self, **over) -> dict:
        body = {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "model": self.model,
            "status": "in_progress",
            "output": [],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
        }
        body.update(over)
        return body

    def _ensure_created(self) -> list[str]:
        if self.started:
            return []
        self.started = True
        return [_event("response.created", {"type": "response.created",
                                           "response": self._response_obj()})]

    def _announce_message(self) -> list[str]:
        if self.message_index is not None:
            return []
        index = self.next_output_index
        self.next_output_index += 1
        self.message_index = index
        return [
            _event("response.output_item.added", {
                "type": "response.output_item.added", "output_index": index,
                "item": {"type": "message", "id": f"msg_{index}", "role": "assistant",
                         "status": "in_progress", "content": []},
            }),
            _event("response.content_part.added", {
                "type": "response.content_part.added", "item_id": f"msg_{index}",
                "output_index": index, "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            }),
        ]

    def _announce_call(self, slot: dict) -> list[str]:
        if slot["announced"]:
            return []
        index = self.next_output_index
        self.next_output_index += 1
        slot["announced"] = True
        slot["index"] = index
        return [_event("response.output_item.added", {
            "type": "response.output_item.added", "output_index": index,
            "item": {"type": "function_call", "id": slot["item_id"],
                     "call_id": slot["call_id"], "name": slot["name"],
                     "arguments": "", "status": "in_progress"},
        })]

    def _slot(self, index: int) -> dict:
        if index not in self.call_slots:
            self.call_slots[index] = {
                "announced": False, "index": None,
                "item_id": f"fc_{len(self.call_slots)}",
                "call_id": "", "name": "", "args": "",
            }
            self.call_order.append(index)
        return self.call_slots[index]

    # ---- 输入 ----

    def push_line(self, line: Any) -> list[str]:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        line = line.strip()
        if not line or not line.startswith("data:"):
            return []
        payload = line[5:].strip()
        if payload == "[DONE]":
            return self._finish_frames()
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
            details_in = usage.get("prompt_tokens_details")
            if _is_obj(details_in) and _num(details_in.get("cached_tokens")) is not None:
                self.cached_tokens = int(_num(details_in.get("cached_tokens")) or 0)
            details_out = usage.get("completion_tokens_details")
            if _is_obj(details_out) and _num(details_out.get("reasoning_tokens")) is not None:
                self.reasoning_tokens = int(_num(details_out.get("reasoning_tokens")) or 0)

        choices = chunk.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else None
        if _is_obj(choice):
            delta = choice.get("delta") if _is_obj(choice.get("delta")) else {}

            text = delta.get("content")
            if isinstance(text, str) and text:
                frames.extend(self._ensure_created())
                frames.extend(self._announce_message())
                self.message_text += text
                frames.append(_event("response.output_text.delta", {
                    "type": "response.output_text.delta", "item_id": f"msg_{self.message_index}",
                    "output_index": self.message_index, "content_index": 0, "delta": text,
                }))

            tool_calls = delta.get("tool_calls")
            if isinstance(tool_calls, list):
                frames.extend(self._ensure_created())
                for tc in tool_calls:
                    if not _is_obj(tc):
                        continue
                    idx = int(_num(tc.get("index")) or 0)
                    slot = self._slot(idx)
                    fn = tc.get("function") if _is_obj(tc.get("function")) else {}
                    cid = tc.get("id")
                    if isinstance(cid, str) and cid and not slot["call_id"]:
                        slot["call_id"] = cid
                    name = fn.get("name")
                    if isinstance(name, str) and name:
                        slot["name"] = name
                    args = fn.get("arguments")
                    if not isinstance(args, str):
                        args = None
                    # 没有名字就无法宣告 function_call 项(与 Anthropic 面的
                    # tool_use 块同理): 先把参数攒着, 名字到了再补发。
                    if not slot["name"]:
                        if args:
                            slot["args"] += args
                        continue
                    if not slot["call_id"]:
                        slot["call_id"] = f"call_{idx}"
                    frames.extend(self._announce_call(slot))
                    if args:
                        slot["args"] += args
                        frames.append(_event("response.function_call_arguments.delta", {
                            "type": "response.function_call_arguments.delta",
                            "item_id": slot["item_id"], "output_index": slot["index"],
                            "delta": args,
                        }))

            fr = choice.get("finish_reason")
            if isinstance(fr, str) and fr:
                self.finish_reason = fr

        return frames

    # ---- 收尾 ----

    def _close_items(self) -> tuple[list[str], list[dict]]:
        """发出各 output 项的 done 帧, 并返回最终的 output 数组。

        顺序以 `output_index`(即宣告顺序)为准, **不能**按"message 一定在前"
        排序: 模型完全可能先吐 tool_calls、再吐文本, 那时 message 的
        output_index 更大, 硬把 message 排前面会让 output 数组与
        `output_item.done` 的宣告顺序不一致, 客户端按 index 累积就会错位。
        """
        rows: list[tuple[int, dict]] = []

        if self.message_index is not None:
            item = {
                "type": "message", "id": f"msg_{self.message_index}",
                "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": self.message_text,
                             "annotations": []}],
            }
            rows.append((self.message_index, item))

        for idx in self.call_order:
            slot = self.call_slots.get(idx)
            if not slot or not slot["announced"]:
                continue
            rows.append((slot["index"], {
                "type": "function_call", "id": slot["item_id"],
                "call_id": slot["call_id"], "name": slot["name"],
                "arguments": slot["args"] or "{}", "status": "completed",
            }))

        rows.sort(key=lambda pair: pair[0])

        frames: list[str] = []
        output: list[dict] = []
        for index, item in rows:
            if item["type"] == "message":
                frames.append(_event("response.output_text.done", {
                    "type": "response.output_text.done", "item_id": item["id"],
                    "output_index": index, "content_index": 0,
                    "text": self.message_text,
                }))
                frames.append(_event("response.content_part.done", {
                    "type": "response.content_part.done", "item_id": item["id"],
                    "output_index": index, "content_index": 0,
                    "part": {"type": "output_text", "text": self.message_text,
                             "annotations": []},
                }))
            else:
                frames.append(_event("response.function_call_arguments.done", {
                    "type": "response.function_call_arguments.done",
                    "item_id": item["id"], "output_index": index,
                    "arguments": item["arguments"],
                }))
            frames.append(_event("response.output_item.done", {
                "type": "response.output_item.done", "output_index": index,
                "item": item,
            }))
            output.append(item)

        return frames, output

    def _finish_frames(self) -> list[str]:
        if self.finished:
            return []
        self.finished = True
        frames = self._ensure_created()
        close_frames, output = self._close_items()
        frames.extend(close_frames)
        status, incomplete = _status_and_details(self.finish_reason)
        body = self._response_obj(
            status=status, output=output,
            usage={
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
                "input_tokens_details": {"cached_tokens": self.cached_tokens},
                "output_tokens_details": {"reasoning_tokens": self.reasoning_tokens},
            },
        )
        if incomplete is not None:
            body["incomplete_details"] = incomplete
        frames.append(_event("response.completed", {"type": "response.completed",
                                                    "response": body}))
        return frames

    def push_error(self, message: str) -> list[str]:
        """流内失败: `response.failed`, 不再发 completed(不谎称正常结束)。"""
        if self.finished:
            return []
        self.finished = True
        self.failed = True
        frames = self._ensure_created()
        body = self._response_obj(status="failed",
                                 error={"code": "server_error", "message": str(message)})
        frames.append(_event("response.failed", {"type": "response.failed",
                                                 "response": body}))
        return frames

    def finish(self) -> list[str]:
        """上游流意外结束(无 [DONE])的兜底收尾。"""
        if self.finished:
            return []
        return self._finish_frames()
