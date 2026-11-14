# ============================================================
# TRAE SOLO 自定义协议 ⇄ OpenAI 形状 转换层
#
# 提取自 dsh-our-free-model pack.js trae-adapter.ts:
#   请求(transformToSOLOBody L39531):
#     OpenAI body + {stream:true, function:通道名} ;
#     assistant.tool_calls[].function → function_call ;
#     字符串 content → [{type:'text',text}] ;
#     model → config_name(同名双写) ;
#     tool_choice 归一(none→删+删tools / required→'required'字符串 /
#       function→裸工具名字符串) ;
#     tools[].function.parameters → JSON 字符串(SOLO 特有!)
#
#   响应(parseTraeSSELine L39646): SOLO 用 **event: 命名事件**, 与 OpenAI 不同:
#     event:output      data:{"response","reasoning_content","tool_calls"}
#     event:token_usage data:{"prompt_tokens","completion_tokens","reasoning_tokens"}
#     event:done        data:{"finish_reason"}
#     event:error       data:{"code","message"}  (如 4008 配额/4011 限流)
#
#   本模块把 SOLO SSE 转回标准 OpenAI SSE chunk 行, 让网关全部现有设施
#   (_stream_with_fallback / StreamRecorder / Anthropic 渲染器)零改动复用。
# ============================================================

from __future__ import annotations

import json
from typing import Iterator

FUNCTION_DEFAULT = "solo_work_lite"
TRAE_MAX_OUTPUT_TOKENS = 64_000

# 错误码语义(照抄 trae-errors.ts): 配额耗尽 vs 软限流
QUOTA_CODES = {4008}
RATE_CODES = {4011}


# ============================================================
# 请求: OpenAI body → SOLO body
# ============================================================

def _transform_message(msg: dict) -> dict:
    result = dict(msg)
    if result.get("role") == "assistant" and isinstance(result.get("tool_calls"), list):
        kept = []
        for tc in result["tool_calls"]:
            if not isinstance(tc, dict):
                continue
            t = dict(tc)
            fn = t.pop("function", None)
            if isinstance(fn, dict):
                t["function_call"] = fn
            fc = t.get("function_call")
            if not isinstance(fc, dict) or not str(fc.get("name") or "").strip():
                continue
            kept.append(t)
        if kept:
            result["tool_calls"] = kept
        else:
            result.pop("tool_calls", None)
    content = result.get("content")
    if isinstance(content, str):
        result["content"] = [{"type": "text", "text": content}]
    return result


def _normalize_tool_choice(body: dict) -> None:
    tc = body.get("tool_choice")
    if tc is None:
        return
    def suppress():
        body.pop("tools", None)
        body.pop("functions", None)
    if isinstance(tc, str):
        if tc.strip().lower() == "none":
            body.pop("tool_choice")
            suppress()
        return
    if isinstance(tc, dict):
        typ = str(tc.get("type") or "").strip().lower()
        if typ == "none":
            body.pop("tool_choice")
            suppress()
        elif typ in ("auto", "required"):
            body["tool_choice"] = typ
        elif typ == "function":
            name = ""
            fn = tc.get("function")
            if isinstance(fn, dict):
                name = str(fn.get("name") or "")
            if not name.strip():
                name = str(tc.get("name") or "")
            body["tool_choice"] = name.strip() if name.strip() else "auto"
        else:
            body.pop("tool_choice")
        return
    body.pop("tool_choice")


def _normalize_tools(body: dict) -> None:
    raw = body.get("tools")
    if not isinstance(raw, list) or not raw:
        return
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        fn = item.get("function")
        if fn is None:
            continue
        if isinstance(fn.get("parameters"), dict):
            fn = dict(fn)
            fn["parameters"] = json.dumps(fn["parameters"], ensure_ascii=False)
            item = {**item, "function": fn}
        out.append(item)
    body["tools"] = out


def to_solo_body(openai_body: dict, channel: str = "",
                 max_context: int = 1_000_000,
                 output_max: int | None = None) -> dict:
    """OpenAI chat body → SOLO llm_utils_chat body(照抄 transformToSOLOBody + max mode)。"""
    body = json.loads(json.dumps(openai_body, ensure_ascii=False))
    body["stream"] = True
    body["function"] = channel or FUNCTION_DEFAULT

    msgs = body.get("messages")
    if isinstance(msgs, list):
        body["messages"] = [_transform_message(m) for m in msgs if isinstance(m, dict)]

    model = str(body.get("model") or "")
    base_model = model.split("__")[0] if "__" in model else model
    config_name = base_model or "glm-5.2"
    body["config_name"] = config_name
    body["model"] = config_name

    _normalize_tool_choice(body)
    _normalize_tools(body)

    # max mode 字段(照抄 traeMaxModeFields): 显式大窗口 + 输出上限
    body.update({
        "model_auto_selection": {"strategy": "max",
                                 "fallback_to_advance_model": None,
                                 "entitlement_id": None},
        "model_selection_strategy": "max",
        "mode_type": 1,
        "context_window_size": max_context if max_context > 0 else 1_000_000,
        "prompt_max_tokens": 936_000,
        "max_tokens": (output_max if output_max and output_max > 0
                       else min(int(body.get("max_tokens") or 0) or TRAE_MAX_OUTPUT_TOKENS,
                                TRAE_MAX_OUTPUT_TOKENS)),
    })
    if isinstance(body.get("max_tokens"), int) and body["max_tokens"] > TRAE_MAX_OUTPUT_TOKENS:
        body["max_tokens"] = TRAE_MAX_OUTPUT_TOKENS
    return body


# ============================================================
# 响应: SOLO 命名事件 SSE → OpenAI chunk 行
# ============================================================

class SoloToOpenAISse:
    """状态机: 喂 SOLO 事件文本行, 吐 OpenAI `data: {...}` 行(不含尾部空行)。

    - 首个 output 事件 → role delta chunk(等价 OpenAI 首块);
    - output.response → delta.content; output.reasoning_content → delta.reasoning_content;
    - output.tool_calls → delta.tool_calls(补 id: 缺 id 用 call_{index},
      function_call 形态归一成 function 形态);
    - token_usage → usage chunk(choices:[]);
    - done → finish chunk + 由调用方补 [DONE];
    - error → 抛 TraeStreamError(上层断流触发候选切换, 与 core/proxy 行为一致)。
    """

    def __init__(self, model_name: str):
        self.model = model_name
        self.msg_id = f"chatcmpl-trae-{id(self) & 0xffffffff:08x}"
        self._event = ""
        self._data_lines: list[str] = []
        self._tool_ids: dict[int, str] = {}
        self.done = False

    def feed(self, raw_line: str) -> Iterator[str]:
        """喂入一行(SOLO SSE 原始行); 完整事件到达时 yield OpenAI data 行。"""
        line = raw_line.rstrip("\r\n")
        if line == "":
            # 空行 = 事件边界
            if self._event:
                yield from self._dispatch(self._event, "\n".join(self._data_lines))
                self._event = ""
                self._data_lines = []
            return
        if line.startswith(":"):
            return  # 注释/心跳
        if line.startswith("event:"):
            self._event = line[6:].strip()
            return
        if line.startswith("data:"):
            self._data_lines.append(line[5:].strip())
            return
        # 其它行(未知字段)忽略

    def _chunk(self, delta: dict, finish: str | None = None, usage: dict | None = None) -> str:
        obj: dict = {
            "id": self.msg_id, "object": "chat.completion.chunk",
            "model": self.model,
        }
        if usage is not None:
            obj["choices"] = []
            obj["usage"] = usage
        else:
            obj["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish}]
        return f"data: {json.dumps(obj, ensure_ascii=False)}"

    def _dispatch(self, event: str, data: str) -> Iterator[str]:
        if event == "error":
            payload = _try_json(data)
            code = payload.get("code") if isinstance(payload, dict) else None
            message = payload.get("message", data[:200]) if isinstance(payload, dict) else data[:200]
            raise TraeStreamError(int(code) if isinstance(code, (int, float)) else 0,
                                  str(message))
        payload = _try_json(data)
        if not isinstance(payload, dict):
            return
        if event == "output":
            delta: dict = {}
            if isinstance(payload.get("response"), str) and payload["response"]:
                delta["content"] = payload["response"]
            if isinstance(payload.get("reasoning_content"), str) and payload["reasoning_content"]:
                delta["reasoning_content"] = payload["reasoning_content"]
            calls = payload.get("tool_calls")
            if isinstance(calls, list) and calls:
                delta["tool_calls"] = [self._norm_call(c) for c in calls if isinstance(c, dict)]
            if delta:
                yield self._chunk(delta)
        elif event == "token_usage":
            usage = {"prompt_tokens": int(payload.get("prompt_tokens") or 0),
                     "completion_tokens": int(payload.get("completion_tokens") or 0)}
            total = usage["prompt_tokens"] + usage["completion_tokens"]
            usage["total_tokens"] = int(payload.get("total_tokens") or total)
            if isinstance(payload.get("reasoning_tokens"), (int, float)):
                usage["completion_tokens_details"] = {
                    "reasoning_tokens": int(payload["reasoning_tokens"])}
            yield self._chunk({}, usage=usage)
        elif event == "done":
            self.done = True
            reason = str(payload.get("finish_reason") or "stop")
            yield self._chunk({}, finish=reason)

    def _norm_call(self, call: dict) -> dict:
        """SOLO 的 function_call 形态 → OpenAI delta.tool_calls 形态(照抄 normalizeTraeToolCalls)。"""
        c = dict(call)
        idx = int(c.get("index") or 0) if str(c.get("index") or "").isdigit() else 0
        fc = c.pop("function_call", None)
        fn = c.get("function") if isinstance(c.get("function"), dict) else (fc or {})
        fn = {k: v for k, v in dict(fn).items() if k in ("name", "arguments")}
        if isinstance(c.get("id"), str) and c["id"]:
            self._tool_ids[idx] = c["id"]
        call_id = self._tool_ids.get(idx) or f"call_{idx}"
        return {"index": idx, "id": call_id, "type": "function", "function": fn}


class TraeStreamError(Exception):
    """SOLO 流内 error 事件(如 4008 配额 / 4011 限流)。"""

    def __init__(self, code: int, message: str):
        super().__init__(f"trae stream error {code}: {message}")
        self.code = code
        self.message = message
        self.is_quota = code in QUOTA_CODES
        self.is_rate = code in RATE_CODES


def _try_json(data: str):
    try:
        return json.loads(data)
    except (ValueError, TypeError):
        return None
