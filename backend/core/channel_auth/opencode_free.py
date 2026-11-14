# -*- coding: utf-8 -*-
# ============================================================
# OpenCode 免费车道 — v2 指纹门(Python 复刻)
#
# 逐字提取自 dsh-our-free-model **GitHub 主仓** src/upstream.js
# (安装到本机的 v2.1.0 还是 v1 老形状, 上游 2026-10-14 起已切 v2 ——
# 这就是本机 zen 全 403 的真正原因, 不是车道死亡)。
#
# v2 门规则(模块头注释逐条实测记录, 每条单独触发 403 FreeTierError):
#   1. 端点: 免费模型在 /inference/v1/* (/zen/v1 的 chat 恒 403);
#   2. User-Agent 必须逐字 "opencode/latest/2.0.26/cli" 单独发,
#      加任何前缀后缀即不再被认作 v2 客户端;
#   3. 会话四头同值必发: x-opencode-session / x-opencode-session-id /
#      x-session-affinity / x-session-id; **不带** x-opencode-request(v1 专用);
#   4. session id 的**编码**也是门: 前 12 位 hex 必须是降序
#      ~(ts_ms<<12|counter) 且 counter∈1..63(低 12 位 0xfc0–0xfff);
#      升序/sha 直摘要/随机 hex 即使形状对也 403;
#   5. 工具指纹门: 请求必须声明 read 与 shell 两个工具, 且 description 与
#      parameters 与官方客户端**逐字一致**(v2.0.26 原样, 本文件常量);
#      调用方同名不同定义 → 改名 <name>_client[N] 并同步历史/tool_choice;
#   6. tool_choice:'none' 即 403, 绝不下发。
#
# 配额按会话计(新 session/请求 → 429 雪崩), 所以一个用户渠道固定一个 session。
# ============================================================

from __future__ import annotations

import hashlib
import json
import random
import re
import string
import threading
import time
from typing import Any

import httpx

# 渠道 id: 与 zen_sources 建的 Provider.name 对齐 —— opencode 车道
# 由自动同步整渠道接管(免 key 行也走本车道协议)
CHANNEL_ID = "opencode"

UPSTREAM_BASE = "https://opencode.ai"
ZEN_MODELS_URL = f"{UPSTREAM_BASE}/zen/v1/models"      # 目录仍走 zen/v1(实测 200)
INFERENCE_BASE = f"{UPSTREAM_BASE}/inference/v1"       # chat/responses/messages
AUTHORIZATION = "Bearer public"                         # 免密池凭据

# ⚠ 逐字: v2 UA 必须单独原样发出(userAgentWith 的合并逻辑只适用于 harness 归属)
CLIENT_UA = "opencode/latest/2.0.26/cli"

BASE62 = string.digits + string.ascii_uppercase + string.ascii_lowercase

SESSION_RE = re.compile(r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$")
# 降序时间半区: 第 10 个 hex ∈ c–f, 即低 12 位 ∈ 0xfc0–0xfff(counter 1–63)
SESSION_DESCENDING_RE = re.compile(r"^[0-9a-f]{9}f[c-f][0-9a-f]$")

RESPONSES_MODELS = {"muse-spark-1.2-contributor-free", "muse-spark-1.3-contributor-free"}
MESSAGES_MODELS = {"union-alpha"}

# ============================================================
# session id 铸造 / 规范化(照抄 mintSessionId / canonicalizeSession)
# ============================================================

_mint_lock = threading.Lock()
_last_stamp = 0
_seq = 0
_MASK48 = 0xFFFFFFFFFFFF


def _b62(byte: int) -> str:
    return BASE62[byte % 62]


def mint_session_id(timestamp_ms: int | None = None) -> str:
    """铸造上游放行布局的 session: ~(ts_ms*0x1000 + counter), counter 1–63。"""
    global _last_stamp, _seq
    with _mint_lock:
        ts = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)
        if ts != _last_stamp:
            _last_stamp = ts
            _seq = 0
        _seq += 1
        seq = _seq
    value = (~(ts * 0x1000 + seq)) & _MASK48
    hexs = "".join(f"{(value >> (40 - 8 * i)) & 0xFF:02x}" for i in range(6))
    suffix = "".join(_b62(b) for b in random.randbytes(14))
    return f"ses_{hexs}{suffix}"


def _base62_fixed_width(data: bytes, width: int) -> str:
    n = int.from_bytes(data, "big")
    out = [""] * width
    for i in range(width - 1, -1, -1):
        out[i] = BASE62[n % 62]
        n //= 62
    return "".join(out)


def canonicalize_session(signal: str) -> str:
    """任意会话信号 → 稳定的降序布局 session(同信号永远同 id, 配额稳定)。"""
    digest = hashlib.sha256(f"ses\0{signal}".encode("utf-8")).digest()
    time28 = (digest[0] << 28 | digest[1] << 20 | digest[2] << 12 |
              digest[3] << 4 | digest[4] >> 4)
    current = ((time28 << 12) | 1) & _MASK48
    inverted = (~current) & _MASK48
    return f"ses_{inverted:012x}{_base62_fixed_width(digest[6:16], 14)}"


def is_canonical_session(id: str | None) -> bool:
    if not isinstance(id, str):
        return False
    s = id.strip()
    return bool(SESSION_RE.match(s)) and bool(SESSION_DESCENDING_RE.match(s[4:16]))


def session_for_conversation(session_id: str | None) -> str:
    """已有规范 id 原样用; 否则按信号确定性重铸(缺省 global)。"""
    if is_canonical_session(session_id):
        return session_id.strip()
    seed = (session_id or "").strip() or "global"
    return canonicalize_session(seed)


# ============================================================
# 请求头(照抄 gatewayHeaders v2)
# ============================================================

def gateway_headers(session: str, stream: bool = True) -> dict:
    return {
        "content-type": "application/json",
        "authorization": AUTHORIZATION,
        "user-agent": CLIENT_UA,
        "x-opencode-client": "cli",
        "x-opencode-session": session,
        "x-opencode-session-id": session,
        "x-session-affinity": session,
        "x-session-id": session,
        "x-opencode-project": "global",
        "accept": "text/event-stream" if stream else "*/*",
        # ⚠ 没有 x-opencode-request: v1 专用, v2 发了就 403
    }


# ============================================================
# 工具指纹门(照抄 applyFingerprint 的 chat 形状)
# ============================================================

# ⚠ 逐字取自 OpenCode v2.0.26 CLI(GitHub 主仓 upstream.js 2026-10-24 拉取核对);
#   上游比对 description 与 parameters, 改写任何措辞/精简 schema/改名都会 403。
FINGERPRINT_TOOL_DEFINITIONS: dict[str, dict] = {
    "read": {
        "description": ("Read the contents of a file or directory. Supports text files, "
            "images, and PDFs. Images and PDFs are presented directly to the model. "
            "Each text line is prefixed by its 1-based line number as <line>: <content>. "
            "The prefix is for reference and is not part of the file content. Directory "
            "entries are returned one per line. Use offset and limit to read large files "
            "or directories in sections. Prefer one larger read over many small slices, "
            "and use grep to find specific content in large files."),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File or directory to read"},
                "offset": {"type": "integer", "minimum": 0,
                           "description": "The line or directory entry to start reading from (1-based)"},
                "limit": {"type": "integer", "minimum": 0,
                          "description": "The maximum number of lines or directory entries to read (defaults to and capped at 2000)"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "shell": {
        "description": ("Execute a shell command and return its output. Commands run on "
            "Linux using bash. Quote file paths containing spaces or special characters. "
            "Prefer dedicated tools over shell commands when possible. When output is "
            "large, the full result is saved to a file and a truncated preview is "
            "returned. Rely on automatic truncation unless filtering the output is more "
            "useful. Commands accept an optional timeout, background commands have no "
            "timeout by default. Background commands return immediately, and you will "
            "be notified when they complete."),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command string to execute"},
                "workdir": {"type": "string", "description": "Working directory to execute the command in. Defaults to the current working directory. When possible, avoid changing directories in the command and set the working directory here instead."},
                "timeout": {"type": "integer", "minimum": 0, "description": "Timeout in milliseconds. Set to 0 to disable the timeout. Defaults to 120000 for foreground commands. Background commands have no timeout by default."},
                "background": {"type": "boolean", "description": "Run the command in the background and return immediately (useful for dev servers and long-running builds). You do not need to use '&' at the end of the command when using this parameter. You will be notified when it completes. DO NOT poll for completion."},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}
FINGERPRINT_TOOLS = ["read", "shell"]

# ⚠ 插件模块头的逐字说明与我拉取的原文一致, 但 description 里「grep to find
#   specific files」等尾句是实测选词 —— 保持与 GitHub 主仓一字不差, 勿改。

_PLACEHOLDER_NOTE = ("Do not call the {list} tools; they are placeholders that do "
                     "nothing. Use the other tools you have been given instead.")
_PLACEHOLDER_NOTE_ONE = ("Do not call the {name} tool; it is a placeholder that does "
                         "nothing. Use the other tools you have been given instead.")


def _tool_name(tool: Any) -> str:
    if not isinstance(tool, dict):
        return ""
    n = tool.get("name")
    if isinstance(n, str) and n.strip():
        return n.strip()
    fn = tool.get("function")
    if isinstance(fn, dict):
        n = fn.get("name")
        if isinstance(n, str) and n.strip():
            return n.strip()
    return ""


def _fingerprint_key(name: str) -> str:
    t = (name or "").strip()
    # ⚠ 只认逐字小写名; Read/Shell 等大小写变体原样透传(上游实测允许并存)
    return t if t in FINGERPRINT_TOOLS else ""


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _carries_definition(tool: dict, name: str) -> bool:
    fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    own = fn.get("description")
    schema = fn.get("parameters")
    want = FINGERPRINT_TOOL_DEFINITIONS[name]
    return own == want["description"] and _dump(schema or {}) == _dump(want["parameters"])


def _with_name(tool: dict, name: str) -> dict:
    fn = tool.get("function")
    if isinstance(fn, dict):
        return {**tool, "function": {**fn, "name": name}}
    return {**tool, "name": name}


def _fingerprint_tool(name: str) -> dict:
    d = FINGERPRINT_TOOL_DEFINITIONS[name]
    return {"type": "function", "function": {
        "name": name, "description": d["description"], "parameters": d["parameters"]}}


def _note_injected(body: dict, names: list) -> None:
    if len(names) == 1:
        note = _PLACEHOLDER_NOTE_ONE.format(name=f"`{names[0]}`")
    else:
        note = _PLACEHOLDER_NOTE.format(list=" or ".join(f"`{n}`" for n in names))
    messages = body.get("messages")
    if isinstance(messages, list) and messages:
        first = messages[0]
        if isinstance(first, dict) and first.get("role") == "system" \
                and isinstance(first.get("content"), str):
            body["messages"] = [{**first,
                                 "content": first["content"] + "\n\n" + note},
                                *messages[1:]]
            return
    body["messages"] = [{"role": "system", "content": note},
                        *(body.get("messages") or [])]


def apply_fingerprint(body: dict) -> dict:
    """OpenAI chat body 过 v2 指纹门(原地改), 返回 发出名→调用方原名 改名表。

    规则(pack.js 注释逐条):
      1. read/shell 必须声明且定义逐字一致(缺则注入官方定义);
      2. 调用方工具原样保留(含大小写变体);
      3. 调用方恰好用同名但定义不同 → 改名 <name>_client[N], 历史与
         tool_choice 同步改指;
      4. 同名重复只留第一个;
      5. 绝不下发 tool_choice:'none'。
    """
    out_map: dict[str, str] = {}
    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    taken = {_tool_name(t) for t in tools if _tool_name(t)}
    seen: set[str] = set()
    present: set[str] = set()
    out: list = []
    for tool in tools:
        if not isinstance(tool, dict):
            out.append(tool)
            continue
        name = _tool_name(tool)
        key = _fingerprint_key(name)
        if key == "":
            out.append(tool)
            continue
        if key in seen:
            continue          # 同名重复: 上游按重复拒, 留第一个
        seen.add(key)
        if _carries_definition(tool, key):
            out.append(tool)
            present.add(key)
            continue
        alias = f"{key}_client"
        n = 2
        while alias in taken:
            alias = f"{key}_client{n}"
            n += 1
        taken.add(alias)
        out_map[alias] = name
        out.append(_with_name(tool, alias))

    injected = []
    for name in FINGERPRINT_TOOLS:
        if name in present:
            continue
        out.append(_fingerprint_tool(name))
        injected.append(name)
    body["tools"] = out

    if out_map:
        _retarget_tool_choice(body, out_map)
        _alias_history(body, out_map)
    if injected:
        _note_injected(body, injected)

    # ⚠ tool_choice:'none' 即 403, 兜底清除(chat 线不补 auto —— 那是 Responses 线行为)
    tc = body.get("tool_choice")
    if tc == "none" or (isinstance(tc, dict) and tc.get("type") == "none"):
        body.pop("tool_choice")
    return out_map


def _retarget_tool_choice(body: dict, rename: dict) -> None:
    tc = body.get("tool_choice")
    if not isinstance(tc, dict) or not rename:
        return
    inv = {v: k for k, v in rename.items()}
    name = tc.get("name")
    if isinstance(name, str) and name.strip() in inv:
        body["tool_choice"] = {**tc, "name": inv[name.strip()]}
        return
    fn = tc.get("function")
    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
        nm = fn["name"].strip()
        if nm in inv:
            body["tool_choice"] = {**tc, "function": {**fn, "name": inv[nm]}}


def _alias_history(body: dict, rename: dict) -> None:
    """历史里对被改名工具的调用记录同步改名(不然模型看到两套定义打架)。"""
    inv = {v: k for k, v in rename.items()}
    messages = body.get("messages")
    if not isinstance(messages, list) or not inv:
        return

    def rn(name):
        return inv.get(name.strip()) if isinstance(name, str) else None

    out = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        if isinstance(m.get("tool_calls"), list):
            calls = []
            for call in m["tool_calls"]:
                if isinstance(call, dict) and isinstance(call.get("function"), dict) \
                        and rn(call["function"].get("name")):
                    call = {**call, "function": {
                        **call["function"], "name": rn(call["function"]["name"])}}
                calls.append(call)
            m = {**m, "tool_calls": calls}
        out.append(m)
    body["messages"] = out


def restore_tool_name(name: str, rename: dict) -> str:
    """响应侧: 发出名 → 调用方原名。"""
    if isinstance(name, str) and rename.get(name):
        return rename[name]
    return name


# ============================================================
# 端点选择 / 模型过滤
# ============================================================

def endpoint_for(model: str) -> str:
    if model in RESPONSES_MODELS:
        return "/responses"
    if model in MESSAGES_MODELS:
        return "/messages"
    return "/chat/completions"


def is_chat_wire(model: str) -> bool:
    """本网关车道只收 chat 形状模型(responses/messages 需另两种协议, 先排除)。"""
    return endpoint_for(model) == "/chat/completions"


# ============================================================
# 请求组装(网关转发与探测共用)
# ============================================================

def build_lane_request(req_body: dict, session: str) -> tuple[str, dict, bytes, dict]:
    """OpenAI body → (url, headers, body_bytes, 改名表)。

    ⚠ v2 门实测: 免费车道只接受流式请求 —— body 不带 stream:true 一律
    403 FreeTierError(2026-10-24 三种形状对照实测)。调用方要非流式也由
    transformed_non_stream 聚合 SSE 兜底, 所以这里强制 stream:true 上行。"""
    body = json.loads(json.dumps(req_body, ensure_ascii=False))
    body["stream"] = True
    rename = apply_fingerprint(body)
    model = str(body.get("model") or "")
    url = INFERENCE_BASE + endpoint_for(model)
    headers = gateway_headers(session, stream=True)
    return url, headers, _dump(body).encode("utf-8"), rename


def make_lane_response_converter(rename: dict):
    """响应侧: 改名表还原工具名; 其余透传(本车道就是标准 OpenAI SSE)。"""
    def convert(line: str) -> list:
        s = line.strip()
        if not s.startswith("data:"):
            return [line]
        payload = s[5:].strip()
        if payload in ("", "[DONE]") or not rename:
            return [line]
        try:
            obj = json.loads(payload)
        except ValueError:
            return [line]
        changed = False
        for ch in obj.get("choices") or []:
            for holder_key in ("delta", "message"):
                holder = ch.get(holder_key)
                if not isinstance(holder, dict):
                    continue
                calls = holder.get("tool_calls")
                if isinstance(calls, list):
                    for call in calls:
                        if not isinstance(call, dict):
                            continue
                        fn = call.get("function")
                        if isinstance(fn, dict) and fn.get("name") in rename:
                            fn["name"] = rename[fn["name"]]
                            changed = True
                # OpenRouter 风格 function_call 形态
                fc = holder.get("function_call")
                if isinstance(fc, dict) and fc.get("name") in rename:
                    fc["name"] = rename[fc["name"]]
                    changed = True
        return [f"data: {_dump(obj)}"] if changed else [line]
    return convert


def lane_credentials(api_key: str) -> dict | None:
    """Provider.api_key → {"channel":"opencode","session":...}(非本车道 None)。"""
    try:
        pkg = json.loads(api_key)
        if isinstance(pkg, dict) and pkg.get("channel") == CHANNEL_ID:
            return pkg
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    return None


def session_from_api_key(api_key: str) -> str:
    """车道会话: api_key JSON 里固定 session(配额按会话计, 一渠道一会话);
    空 key / 非 JSON / 缺字段 → 按 "global" 信号确定性铸造(重启不变)。"""
    pkg = lane_credentials(api_key or "")
    if pkg and pkg.get("session"):
        return session_for_conversation(pkg["session"])
    return session_for_conversation((api_key or "").strip() or "global")


# ============================================================
# 同步侧探测(zen_sources 用): 真实 1-token 请求
# ============================================================

def _retry_after_from(headers) -> int | None:
    """上游 429 的 `retry-after`(秒); 非数字/超范围一律 None。"""
    try:
        raw = headers.get("retry-after")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        secs = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return secs if 0 < secs <= 7 * 24 * 3600 else None


def note_quota_resume(headers) -> None:
    """429 时把"额度何时恢复"写进 lane_state(界面显示倒计时, 不必等用户请求)。"""
    secs = _retry_after_from(headers)
    if not secs:
        return
    try:
        from backend.core import lane_state
        lane_state.note_quota_resume(CHANNEL_ID, secs)
    except Exception:  # pragma: no cover - 纯展示信息
        pass


def probe_model_sync(model_id: str, timeout: float = 40.0,
                     session: str | None = None) -> tuple[bool, str]:
    """同步真探测: 200+流式 delta 可用 / 429 免费档额度(视为可用, 保留) / 其它不可用。

    ⚠ body 形状也是门: 必须与官方客户端一致 —— stream:true + 'ping' +
    max_tokens:16(对照插件 probe.js buildPing)。非流式探测一律 403
    FreeTierError(2026-10-24 实测), 会把好模型误判成死模型。
    流式应答里 error 帧按插件口径算 unknown(不可用但不踢出)。
    """
    from backend.core import upstream_http
    body = {"model": model_id, "messages": [{"role": "user", "content": "ping"}],
            "stream": True, "max_tokens": 16}
    apply_fingerprint(body)
    sess = session or canonicalize_session("probe")
    try:
        with upstream_http.make_sync(timeout=timeout) as client:
            with client.stream(
                    "POST", INFERENCE_BASE + endpoint_for(model_id),
                    content=_dump(body).encode("utf-8"),
                    headers=gateway_headers(sess, stream=True)) as r:
                if r.status_code == 429:
                    # 免费额度用尽(会重置): retry-after 就是恢复秒数, 记下来给界面
                    note_quota_resume(r.headers)
                    return True, f"429 quota {r.read().decode('utf-8', 'replace')[:80]}"
                if r.status_code != 200:
                    return False, f"{r.status_code} {r.read().decode('utf-8', 'replace')[:90]}"
                # 200: 读流找 delta 帧 / error 帧(对齐插件 probe.js 判定)
                saw_delta = False
                stream_err = None
                for raw in r.iter_lines():
                    s = raw.strip()
                    if not s.startswith("data:"):
                        continue
                    payload = s[5:].strip()
                    if payload in ("", "[DONE]"):
                        continue
                    if '"error"' in payload or '"type":"error"' in payload.replace(" ", ""):
                        try:
                            obj = json.loads(payload)
                            fail = obj.get("error") or (obj if obj.get("type") == "error" else None)
                            if fail:
                                stream_err = str(fail.get("message") or "upstream error")[:120]
                                break
                        except ValueError:
                            pass
                    if ('"delta"' in payload or '"content"' in payload
                            or '"text"' in payload):
                        saw_delta = True
                        break
                if stream_err:
                    return False, f"stream-error {stream_err}"
                if saw_delta:
                    return True, "ok"
                return False, "200 empty stream"
    except Exception as e:
        return False, f"ERR {str(e)[:70]}"
