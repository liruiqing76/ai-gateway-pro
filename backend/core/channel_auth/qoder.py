# ============================================================
# 白嫖渠道 — Qoder (阿里系; 国际版 qoder / 中国版 qodercn)
#
# 提取自 dsh-our-free-model pack.js:
#   qoder-product.ts L37488-37789  两个产品的端点/clientId/模型表
#   qoder.ts L36873-37030          设备码登录: PKCE(S256)+nonce+machine_id
#   qoder-auth.ts L44779+ / L45121 refresh (deviceToken/refresh)
#   qoder-wasm.ts L37030+          加密推理签名 → Node sidecar 原样复用 JS glue
#   qoder-envelope.ts L37378+      推理响应信封解包 {"body":...} → chunk
#   qoder-credits.ts L56559+       每日 100 Credits: campaigns + claim
#   qoder-machine.ts L56396+       machine token(runtime-info.exe / json 文件)
#
# 关键事实(2026-10-24 本机实测):
#   - CN 没有公开 OpenAI 兼容端点(插件注释明示 503/无路由), 推理必须走
#     加密端点: wasm 签名的 Bearer COSY.* + 加密 body + 信封流 → 全部由
#     vendor/qoder_signer.mjs(Node, 复用插件 MIT 的 JS glue)产出, Python 侧
#     只做行协议调用与信封解包。
#   - 本机 ~/.qoder-cn/.bin/umid-*/runtime-info.exe 可用(mk token 能领)。
#   - 设备码轮询 openapi.qoder.com.cn/api/v1/deviceToken/poll 404=pending。
# ============================================================

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import uuid as uuidlib
from pathlib import Path
from typing import Any

import httpx

CHANNEL_ID_CN = "qodercn"
CHANNEL_ID_INTL = "qoder"

# ---- 产品表(照抄 qoder-product.ts) ----
PRODUCTS: dict[str, dict] = {
    CHANNEL_ID_CN: {
        "id": "qodercn",
        "display_name": "Qoder (中国版)",
        "auth_base": "https://qoder.cn",
        "openapi_base": "https://openapi.qoder.com.cn",
        "encrypted_infer_base": "https://gateway.qoder.com.cn",
        "client_id": "732aef47-9cf2-46a2-95fe-4cebb5d0d1fa",
        "client_metadata": {"client_type": "5", "business_product": "cli",
                            "business_type": "agent", "scene": "assistant"},
        "sash_client_type": "10",
        "user_agent": "qoder/1.0.0",
    },
    CHANNEL_ID_INTL: {
        "id": "qoder",
        "display_name": "Qoder",
        "auth_base": "https://qoder.com",
        "openapi_base": "https://openapi.qoder.sh",
        "encrypted_infer_base": "https://api2.qoder.sh",
        "client_id": "e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb",
        "client_metadata": {"client_type": "5", "business_product": "cli",
                            "business_type": "agent", "scene": "assistant"},
        "sash_client_type": "10",
        "user_agent": "qoder/1.0.0",
    },
}

DEVICE_SELECT_PATH = "/device/selectAccounts"
POLL_PATH = "/api/v1/deviceToken/poll"
REFRESH_PATH = "/api/v1/deviceToken/refresh"
USERINFO_PATH = "/api/v1/userinfo"
USAGE_PATH = "/sash/api/v2/me/usage"
CAMPAIGNS_PATH = "/sash/api/v1/me/campaigns"
CAMPAIGN_REFRESH_HOUR_UTC8 = 10

REQUEST_TIMEOUT_SECONDS = 30
LOGIN_TIMEOUT_SECONDS = 300
POLL_INTERVAL_SECONDS = 2.0

# ---- 模型表(摘要; isFree=免费额度模型, 照抄 fallback) ----
_Q = lambda i, n, free=False, think=False, **kw: dict(
    {"id": i, "name": n, "isFree": free, "supportsThinking": think}, **kw)

# ---- 计费倍率(priceFactor): 照抄 dsh channel-pack 兜底表(catalog 实测快照) ----
# 全模型按倍率扣积分, isFree 仅标记"当前不耗积分"档(网关照样暴露, 倍率给 0/促销价)。
_RATE_CN = {
    "auto": 0.5, "qfmodel": 0, "qmodel_38max": 0.2, "qmodel_latest": 0.1,
    "qmodel": 0.04, "q37fmodel": 0.1, "dmodel": 0.5, "dfmodel": 0.1,
    "gmodel": 0.8, "gfmodel": 0.1, "gm51model": 0.6,
    "kmodel_latest": 1.4, "kmodel": 0.8, "mmodel": 0.2,
}
_RATE_INTL = {
    "auto": 0.5, "ultimate": 2, "performance": 1.1, "efficient": 0.3,
    "smodel": 8, "cmodel": 4, "qfmodel": 0, "qmodel_38max": 0.2,
    "qmodel_latest": 0.1, "qmodel": 0.04,
    "kmodel_latest": 1.4, "kmodel": 0.8, "gmodel": 0.8, "gfmodel": 0.1,
    "dmodel": 0.5, "dfmodel": 0.1, "mmodel": 0.2,
}

MODELS_CN = [
    _Q("qfmodel", "Qwen3.8-Flash", free=True, think=True, contextWindow=1_000_000),
    _Q("qmodel_38max", "Qwen3.8-Max", free=True, think=True, contextWindow=1_000_000),
    _Q("auto", "Auto", contextWindow=200_000),
    _Q("qmodel_latest", "Qwen3.7-Max", contextWindow=1_000_000),
    _Q("qmodel", "Qwen3.7-Plus", contextWindow=1_000_000),
    _Q("q37fmodel", "Qwen3.7-Flash", contextWindow=1_000_000),
    _Q("dmodel", "DeepSeek-V4-Pro", think=True, contextWindow=1_000_000),
    _Q("dfmodel", "DeepSeek-Flash", contextWindow=1_000_000),
    _Q("gmodel", "GLM-5.3", think=True, contextWindow=1_000_000),
    _Q("gfmodel", "GLM-5.3-Flash", think=True, contextWindow=1_000_000),
    _Q("gm51model", "GLM-5.2", think=True, contextWindow=1_000_000),
    _Q("kmodel_latest", "Kimi-K3", contextWindow=1_000_000),
    _Q("kmodel", "Kimi-K2.8-Preview", think=True, contextWindow=1_000_000),
    _Q("mmodel", "MiniMax-M2.7", contextWindow=200_000),
]
MODELS_INTL = [
    _Q("qfmodel", "Qwen3.8-Flash", free=True, think=True, contextWindow=1_000_000),
    _Q("qmodel_38max", "Qwen3.8-Max", free=True, think=True, contextWindow=1_000_000),
    _Q("auto", "Auto", contextWindow=200_000),
    _Q("ultimate", "Ultimate", think=True, contextWindow=1_000_000),
    _Q("performance", "Performance", contextWindow=1_000_000),
    _Q("efficient", "Efficient", contextWindow=1_000_000),
    _Q("smodel", "Sonus", think=True, contextWindow=1_000_000),
    _Q("cmodel", "Cantus", think=True, contextWindow=1_000_000),
    _Q("qmodel_latest", "Qwen3.7-Max", contextWindow=1_000_000),
    _Q("qmodel", "Qwen3.7-Plus", contextWindow=1_000_000),
    _Q("kmodel_latest", "Kimi-K3", contextWindow=1_000_000),
    _Q("kmodel", "Kimi-K2.8-Preview", contextWindow=1_000_000),
    _Q("gmodel", "GLM-5.3", think=True, contextWindow=1_000_000),
    _Q("gfmodel", "GLM-5.3-Flash", think=True, contextWindow=1_000_000),
    _Q("dmodel", "DeepSeek-V4-Pro", think=True, contextWindow=1_000_000),
    _Q("dfmodel", "DeepSeek-Flash", think=True, contextWindow=1_000_000),
    _Q("mmodel", "MiniMax-M3", contextWindow=1_000_000),
]

# 对外暴露 ID 用真名(Qwen3.8-Flash 等), 内部签名仍走 modelKey 代号;
# 请求侧真名/代号都接受 → resolve_model_key 双向反查。
MODEL_INDEX = {
    CHANNEL_ID_CN: {m["id"]: m for m in MODELS_CN},
    CHANNEL_ID_INTL: {m["id"]: m for m in MODELS_INTL},
}
# 显示名 → 内部代号(真名↔代号双向映射的另一半)
_DISPLAY_INDEX = {
    ch: {m["name"]: m["id"] for m in table}
    for ch, table in ((CHANNEL_ID_CN, MODELS_CN), (CHANNEL_ID_INTL, MODELS_INTL))
}


def resolve_model_key(channel: str, model: str) -> str:
    """真名(显示 ID)或旧代号 → 内部 modelKey 代号。未知模型原样返回(上游判错)。"""
    if not model:
        return "qfmodel"
    idx = MODEL_INDEX.get(channel, {})
    if model in idx:
        return model                      # 已是代号
    return _DISPLAY_INDEX.get(channel, {}).get(model, model)


def models(channel: str) -> list[dict]:
    """全量模型清单(条目 id=真名/Qwen3.8-Flash 等, code=内部代号, rate=积分倍率)。

    之前只暴露 isFree 的 2 个 —— 但 qoder 账号全部模型都能调, 只是扣积分
    速度不同(priceFactor); 过滤反而把 90% 的额度挡在门外。
    之前条目 id 直接用内部代号(qfmodel), 用户在路由分组界面完全看不懂;
    现在对外一律真名, 签名时由 resolve_model_key 反查回代号。
    """
    table = MODELS_CN if channel == CHANNEL_ID_CN else MODELS_INTL
    rates = _RATE_CN if channel == CHANNEL_ID_CN else _RATE_INTL
    return [{**m, "id": m["name"], "code": m["id"],
             "rate": rates.get(m["id"], 1.0)} for m in table]


def free_models(channel: str) -> list[dict]:
    """免费档(条目同 models(): id=真名, code=代号)。e2e 探针用。"""
    return [{**m, "id": m["name"], "code": m["id"]}
            for m in (MODELS_CN if channel == CHANNEL_ID_CN else MODELS_INTL)
            if m.get("isFree")]


def product_for(channel: str) -> dict:
    return PRODUCTS.get(channel) or PRODUCTS[CHANNEL_ID_CN]


# ============================================================
# 设备码登录
# ============================================================

def create_device_session(machine_id: str = "") -> dict:
    """PKCE(S256) + nonce + machine_id(照抄 createQoderDeviceSession)。"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return {"verifier": verifier, "challenge": challenge,
            "nonce": str(uuidlib.uuid4()),
            "machine_id": machine_id or str(uuidlib.uuid4())}


def build_auth_url(session: dict, product: dict) -> str:
    from urllib.parse import urlencode
    q = urlencode({"challenge": session["challenge"], "challenge_method": "S256",
                   "nonce": session["nonce"], "machine_id": session["machine_id"],
                   "client_id": product["client_id"]})
    return f"{product['auth_base']}{DEVICE_SELECT_PATH}?{q}"


def _read_str(source: Any, *keys) -> str:
    for k in keys:
        v = (source or {}).get(k)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, (int, float)):
            return str(v)
    return ""


def _read_ts(value) -> int | None:
    if isinstance(value, (int, float)) and value > 0:
        return int(value * 1000 if value < 1e12 else value)
    if isinstance(value, str) and value:
        try:
            from datetime import datetime
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return None


def parse_token_payload(value: dict) -> dict:
    """poll/refresh 响应 → 凭据字段(照抄 parseQoderTokenPayload)。"""
    if not isinstance(value, dict):
        return {"access_token": ""}
    access = _read_str(value, "token", "device_token", "access_token")
    out = {"access_token": access}
    for src_key, dst in (("refresh_token", "refresh_token"),
                         ("refreshToken", "refresh_token")):
        v = _read_str(value, src_key)
        if v:
            out[dst] = v
            break
    for src_key, dst in (("expires_at", "expire_time"), ("expiresAt", "expire_time")):
        ts = _read_ts(value.get(src_key))
        if ts:
            out[dst] = ts
            break
    for src_key, dst in (("refresh_token_expires_at", "refresh_token_expire_time"),
                         ("refreshTokenExpiresAt", "refresh_token_expire_time")):
        ts = _read_ts(value.get(src_key))
        if ts:
            out[dst] = ts
            break
    uid = _read_str(value, "user_id", "userId")
    if uid:
        out["uid"] = uid
    name = _read_str(value, "user_name", "userName")
    if name:
        out["user_name"] = name
    return out


async def poll_device_token(client: httpx.AsyncClient, session: dict,
                            product: dict, timeout: float = LOGIN_TIMEOUT_SECONDS) -> dict | None:
    """轮询设备码; 404=pending 继续, 200 → 凭据。超时 None。"""
    from urllib.parse import urlencode
    q = urlencode({"nonce": session["nonce"], "verifier": session["verifier"],
                   "challenge_method": "S256"})
    url = f"{product['openapi_base']}{POLL_PATH}?{q}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = await client.get(url, headers={"User-Agent": product["user_agent"]},
                                 timeout=15)
            if r.status_code == 200:
                data = parse_token_payload(r.json())
                if data.get("access_token"):
                    return data
            # 404 pending / 其它瞬时码继续等
        except httpx.HTTPError:
            pass
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    return None


async def fetch_userinfo(client: httpx.AsyncClient, token: str,
                         product: dict) -> dict:
    try:
        r = await client.get(f"{product['openapi_base']}{USERINFO_PATH}",
                             headers={"Authorization": f"Bearer {token}",
                                      "User-Agent": product["user_agent"]},
                             timeout=15)
        if r.status_code == 200:
            return r.json() if isinstance(r.json(), dict) else {}
    except httpx.HTTPError:
        pass
    return {}


async def refresh_credential(client: httpx.AsyncClient, credential: dict,
                             product: dict) -> dict | None:
    """deviceToken/refresh(照抄 refreshCredential L45121)。None=需重登。"""
    if not credential.get("refresh_token"):
        return None
    try:
        r = await client.post(f"{product['openapi_base']}{REFRESH_PATH}",
                              json={"refresh_token": credential["refresh_token"],
                                    "machine_id": credential.get("machine_id", "")},
                              headers={"Content-Type": "application/json",
                                       "Accept": "application/json",
                                       "User-Agent": product["user_agent"]},
                              timeout=15)
    except httpx.HTTPError:
        return None
    if r.status_code in (401, 403):
        return None
    try:
        data = parse_token_payload(r.json())
    except ValueError:
        return None
    if r.status_code != 200 or not data.get("access_token"):
        return None
    merged = {**credential, **data}
    merged.setdefault("refresh_token", credential.get("refresh_token", ""))
    merged.setdefault("uid", credential.get("uid", ""))
    return merged


def build_credential(token: dict, machine_id: str, nickname: str = "",
                     channel: str = CHANNEL_ID_CN) -> dict:
    """凭据包(照抄 buildQoderCredential + channel 标记)。"""
    creds = {
        "channel": channel,
        "security_oauth_token": token.get("access_token", ""),
        "access_token": token.get("access_token", ""),
        "machine_id": machine_id,
    }
    for k in ("refresh_token", "expire_time", "refresh_token_expire_time", "uid"):
        if token.get(k):
            creds[k] = token[k]
    nick = nickname or token.get("user_name") or ""
    if nick:
        creds["nickname"] = nick
    return creds


def credentials_package(creds: dict) -> str:
    return json.dumps(creds, ensure_ascii=False)


def parse_credentials_package(api_key: str, channel: str) -> dict | None:
    try:
        pkg = json.loads(api_key)
        if isinstance(pkg, dict) and pkg.get("channel") == channel:
            return pkg
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return None


def credential_expired(credential: dict, skew_ms: int = 60_000) -> bool:
    """expire_time(ms) 或 JWT exp 判定(照抄 isQoderExpired)。"""
    exp = credential.get("expire_time")
    if isinstance(exp, (int, float)) and exp > 0:
        return exp <= int(time.time() * 1000) + skew_ms
    token = credential.get("access_token", "")
    parts = token.split(".")
    if len(parts) >= 2:
        try:
            pad = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(pad.encode()))
            if isinstance(payload.get("exp"), (int, float)):
                return payload["exp"] * 1000 <= int(time.time() * 1000) + skew_ms
        except Exception:
            pass
    return False


# ============================================================
# Node 签名 sidecar(加密端点): 行协议常驻进程
# ============================================================

_SIGNER_SCRIPT = Path(__file__).resolve().parent / "vendor" / "qoder_signer.mjs"

# 签名器行协议的单行上限。默认 64KB 会被长上下文请求撑爆, 放大到 8MB。
_SIGNER_STREAM_LIMIT = 8 * 1024 * 1024


class _SignerProcess:
    """常驻 node 进程, stdin 行请求 / stdout 行响应。挂了自动重启。"""

    def __init__(self):
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def _ensure(self):
        if self._proc is not None and self._proc.returncode is None:
            return
        node = _find_node()
        self._proc = await asyncio.create_subprocess_exec(
            node, str(_SIGNER_SCRIPT),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            # 签名器返回的是单行 JSON, 长度随请求体膨胀(长上下文实测 17KB+)。
            # asyncio 默认 stream limit 只有 64KB, 超了 readline 直接抛
            # LimitOverrunError/ValueError("Separator is found/not found ... limit"),
            # 表现为"所有候选模型均失败"。这里放大到 8MB 留足余量。
            limit=_SIGNER_STREAM_LIMIT,
            creationflags=(subprocess.CREATE_NO_WINDOW
                           if os.name == "nt" else 0),
        )

    async def request(self, payload: dict, timeout: float = 15.0) -> dict:
        async with self._lock:
            for attempt in (0, 1):
                await self._ensure()
                proc = self._proc
                assert proc is not None and proc.stdin is not None and proc.stdout is not None
                try:
                    proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
                    await proc.stdin.drain()
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
                except (asyncio.TimeoutError, OSError,
                        asyncio.LimitOverrunError, ValueError) as e:
                    # LimitOverrunError/ValueError 是单行超过 stream limit 时
                    # readline 抛出的(旧版 64KB 上限踩过 93 次); 进程状态已不可信,
                    # 杀掉重开一次。
                    await self._kill()
                    if attempt:
                        raise RuntimeError(f"qoder signer 无响应: {e}") from e
                    continue
                if not line:
                    await self._kill()
                    if attempt:
                        raise RuntimeError("qoder signer 进程退出")
                    continue
                resp = json.loads(line.decode("utf-8", "replace"))
                if resp.get("ok"):
                    return resp["result"]
                raise RuntimeError(f"qoder signer 错误: {str(resp.get('error'))[:300]}")
            raise RuntimeError("qoder signer 不可用")

    async def _kill(self):
        if self._proc is not None:
            proc, self._proc = self._proc, None
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                # reap + 关闭 transport, 否则解释器退出时
                # BaseSubprocessTransport.__del__ 报 "Event loop is closed"
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass

    async def shutdown(self):
        """服务停止时回收常驻 node 进程。"""
        async with self._lock:
            await self._kill()


def _find_node() -> str:
    """node 可执行: env → PATH → DSH 桌面端 runtime。"""
    env = os.environ.get("GW_NODE_PATH", "")
    if env and Path(env).exists():
        return env
    from shutil import which
    n = which("node") or which("node.exe")
    if n:
        return n
    for cand in (r"D:\software\deepseekharness\resources\runtime\node.exe",):
        if Path(cand).exists():
            return cand
    raise RuntimeError("找不到 node, 无法为 Qoder 加密端点签名(设 GW_NODE_PATH)")


_signer: _SignerProcess | None = None


def get_signer() -> _SignerProcess:
    global _signer
    if _signer is None:
        _signer = _SignerProcess()
    return _signer


# ---- OpenAI body → qoder ask → 签名请求 ----

def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(b.get("text") or "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _content_parts(content):
    """带图的 user content → OpenAI parts(照抄 qoderContentParts)。"""
    if not isinstance(content, list):
        return None
    parts, has_image = [], False
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and b.get("text"):
            parts.append({"type": "text", "text": str(b["text"])})
        elif b.get("type") == "image_url":
            url = ((b.get("image_url") or {}).get("url")) or ""
            if not url:
                continue
            item: dict = {"type": "image_url", "image_url": {"url": url}}
            detail = (b.get("image_url") or {}).get("detail")
            if isinstance(detail, str) and detail:
                item["image_url"]["detail"] = detail
            parts.append(item)
            has_image = True
    return parts if has_image else None


def build_qoder_history(messages: list) -> list:
    history = []
    for m in messages:
        if not isinstance(m, dict) or not isinstance(m.get("role"), str):
            continue
        parts = _content_parts(m.get("content"))
        content = parts if parts is not None else _content_text(m.get("content"))
        tool_calls = m.get("tool_calls") if isinstance(m.get("tool_calls"), list) and m.get("tool_calls") else None
        tool_call_id = m.get("tool_call_id") if isinstance(m.get("tool_call_id"), str) else None
        empty = (len(content) == 0) if parts is None else (len(content) == 0)
        if empty and tool_calls is None and tool_call_id is None:
            continue
        row = {"role": m["role"], "content": content}
        if tool_calls is not None:
            row["tool_calls"] = tool_calls
        if tool_call_id is not None:
            row["tool_call_id"] = tool_call_id
        history.append(row)
    return history


def build_qoder_tools(tools) -> list:
    if not isinstance(tools, list) or not tools:
        return []
    out = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") or {}
        item = {"type": "function", "function": {"name": fn.get("name") or t.get("name") or ""}}
        if fn.get("description"):
            item["function"]["description"] = fn["description"]
        if "parameters" in fn:
            item["function"]["parameters"] = fn["parameters"]
        out.append(item)
    return out


async def sign_infer_request(channel: str, credential: dict,
                             openai_body: dict) -> dict:
    """OpenAI body → 签名好的 {url, headers, body}(加密端点)。"""
    product = product_for(channel)
    # 对外 ID 是真名(Qwen3.8-Flash), 旧配置可能是代号(qfmodel): 双向都接受
    model_key = resolve_model_key(
        channel, str(openai_body.get("model") or "qfmodel"))
    meta = MODEL_INDEX.get(channel, {}).get(model_key, {})
    messages = openai_body.get("messages") or []
    system_text = ""
    rest = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "system":
            system_text = (system_text + "\n\n" if system_text else "") + _content_text(m.get("content"))
        else:
            rest.append(m)
    history = build_qoder_history(rest)
    user_text = ""
    for m in reversed(history):
        if m.get("role") == "user":
            user_text = _content_text(m.get("content"))
            break
    effort = openai_body.get("reasoning_effort")
    ask = {
        "modelKey": model_key,
        "userText": user_text,
        "isReasoning": bool(meta.get("supportsThinking")),
        "history": history,
        "tools": build_qoder_tools(openai_body.get("tools")),
        "displayName": meta.get("name", model_key),
        "contextWindow": meta.get("contextWindow", 200_000),
        "isVl": True,
        "business": {"type": "agent"},
    }
    if system_text:
        ask["systemText"] = system_text
    if isinstance(openai_body.get("max_tokens"), int) and openai_body["max_tokens"] > 0:
        ask["maxTokens"] = openai_body["max_tokens"]
    if effort:
        ask["reasoningEffort"] = ("none" if effort == "none" else str(effort))
    machine_token, machine_type = resolve_machine_identity()
    user = {"uid": str(credential.get("uid") or "0"),
            "securityOauthToken": credential.get("security_oauth_token")
            or credential.get("access_token", "")}
    signed = await get_signer().request({
        "op": "infer", "user": user,
        "machineId": credential.get("machine_id", ""),
        "metadata": product["client_metadata"],
        "host": product["encrypted_infer_base"],
        "ask": ask,
    })
    # wasm 产物里 Cosy-MachineToken/Type 为空; 本机 runtime-info 的真实
    # 设备身份在这补上(照抄 withQoderMachineHeadersAsync 的注入位)。
    headers = signed.get("headers") or {}
    if machine_token:
        headers["Cosy-MachineToken"] = machine_token
        headers["Cosy-MachineType"] = machine_type
    signed["headers"] = headers
    return signed


# ---- machine identity(runtime-info.exe / machine_token.json, 照抄 qoder-machine.ts) ----

_machine_cache: dict | None = None
_machine_lock = threading.Lock()


def resolve_machine_identity() -> tuple[str, str]:
    global _machine_cache
    with _machine_lock:
        if _machine_cache is not None:
            return _machine_cache
        _machine_cache = _machine_cache = _resolve_machine_uncached()
        return _machine_cache


def _resolve_machine_uncached() -> tuple[str, str]:
    override = os.environ.get("QODER_MACHINE_TOKEN_PATH", "")
    if override and Path(override).exists():
        got = _parse_machine_file(Path(override))
        if got:
            return got
    appdata = os.environ.get("APPDATA", "")
    dirs = [Path(appdata) / d / "SharedClientCache" / "cache" / "machine_token.json"
            for d in ("Qoder", "Qoder CN") if appdata]
    home = Path.home()
    for d in ("Qoder", "Qoder CN"):
        dirs.append(home / "Library/Application Support" / d / "SharedClientCache/cache/machine_token.json")
        dirs.append(home / ".config" / d / "SharedClientCache/cache/machine_token.json")
    for p in dirs:
        got = _parse_machine_file(p)
        if got:
            return got
    exe = _find_runtime_info()
    if exe:
        got = _run_runtime_info(exe)
        if got:
            return got
    return "", ""


def _parse_machine_file(path: Path) -> tuple[str, str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("token") and data.get("type"):
            return str(data["token"]), str(data["type"])
    except (OSError, ValueError):
        return None
    return None


def _find_runtime_info() -> str | None:
    env = os.environ.get("QODER_RUNTIME_INFO", "")
    if env and Path(env).exists():
        return env
    home = Path.home()
    exe_name = "runtime-info.exe" if os.name == "nt" else "runtime-info"
    for d in (".qoder", ".qoder-cn"):
        bin_dir = home / d / ".bin"
        try:
            entries = os.listdir(bin_dir)
        except OSError:
            continue
        for name in entries:
            if name.startswith("umid-"):
                cand = bin_dir / name / exe_name
                if cand.exists():
                    return str(cand)
    return None


def _run_runtime_info(exe: str) -> tuple[str, str] | None:
    try:
        p = subprocess.run([exe, "3", "--account-stdin"],
                           input=json.dumps({"account": ""}),
                           capture_output=True, text=True, timeout=20,
                           creationflags=(subprocess.CREATE_NO_WINDOW
                                          if os.name == "nt" else 0))
        out = p.stdout or ""
        start = out.find("{")
        if start < 0:
            return None
        data = json.loads(out[start:])
        if data.get("machineToken") and data.get("machineType"):
            return str(data["machineToken"]), str(data["machineType"])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return None


# ---- 信封解包(推理响应流) ----

def unwrap_qoder_line(line: str) -> str | None:
    """SOLO 信封行 → OpenAI chunk 行; None=心跳丢弃; 抛错=流内错误。

    照抄 unwrapQoderEnvelopeStream.classifyInner:
      data:{"body":"<json|string>"} → inner; 空=心跳; 含[DONE]/choices/usage=chunk;
      code/message/error/statusCodeValue/type=model_error → error。
    """
    s = line.strip()
    if not s.startswith("data:"):
        return None
    payload = s[5:].strip()
    if payload == "[DONE]":
        return "data: [DONE]"
    try:
        envelope = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(envelope, dict) or "body" not in envelope:
        return f"data: {payload}"
    body = envelope.get("body")
    inner = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    trimmed = (inner or "").strip()
    if not trimmed:
        return None                      # heartbeat
    if "[DONE]" in trimmed:
        return "data: [DONE]"
    try:
        parsed = json.loads(trimmed)
    except ValueError:
        raise RuntimeError(f"qoder upstream error: {trimmed[:200]}")
    if isinstance(parsed, list) or not isinstance(parsed, dict):
        return None
    if isinstance(parsed.get("choices"), list) or parsed.get("usage") is not None:
        return f"data: {trimmed}"
    if ("code" in parsed or "message" in parsed or "error" in parsed
            or "statusCodeValue" in parsed or "type" in parsed):
        msg = parsed.get("message") if isinstance(parsed.get("message"), str) else trimmed[:200]
        raise RuntimeError(f"qoder upstream error: {msg}")
    return None                          # 未知对象 → 心跳


# ============================================================
# 每日积分领取(campaigns; 照抄 qoder-credits.ts)
# ============================================================

def _utc8_hour(now_ms: float | None = None) -> int:
    ts = (now_ms or time.time() * 1000) / 1000
    from datetime import datetime, timezone, timedelta
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).hour


async def claim_daily_checkin(client: httpx.AsyncClient, credential: dict,
                              channel: str = CHANNEL_ID_CN) -> dict:
    product = product_for(channel)
    token = credential.get("security_oauth_token") or credential.get("access_token", "")
    machine_token, machine_type = resolve_machine_identity()
    headers = {"Accept": "application/json",
               "Authorization": f"Bearer {token}",
               "Cosy-ClientType": product["sash_client_type"],
               "User-Agent": "Qoder"}
    if machine_token:
        headers["Cosy-MachineToken"] = machine_token
        headers["Cosy-MachineType"] = machine_type
    base = product["openapi_base"]
    try:
        r = await client.get(f"{base}{CAMPAIGNS_PATH}", headers=headers, timeout=15)
        if r.status_code != 200:
            return {"kind": "failed", "message": f"活动列表查询失败(HTTP {r.status_code})"}
        body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return {"kind": "failed", "message": f"活动列表查询失败: {e}"}
    campaigns = body.get("campaigns") if isinstance(body, dict) else None
    if not isinstance(campaigns, list):
        return {"kind": "failed", "message": "活动列表响应异常"}
    targets = [c for c in campaigns
               if isinstance(c, dict) and c.get("actionType") == "CLAIM_BENEFIT"
               and c.get("claimStatus") == "CLAIMABLE"]
    refreshed = _utc8_hour() >= CAMPAIGN_REFRESH_HOUR_UTC8
    if not targets:
        claimed_before = any(isinstance(c, dict) and c.get("actionType") == "CLAIM_BENEFIT"
                             and c.get("claimStatus") == "CLAIMED" for c in campaigns)
        if claimed_before:
            return ({"kind": "inactive",
                     "message": f"今日活动尚未刷新(每日 {CAMPAIGN_REFRESH_HOUR_UTC8}:00 UTC+8 刷新)"}
                    if not refreshed else
                    {"kind": "already-claimed", "message": "今天已领取"})
        has_benefit = any(isinstance(c, dict) and c.get("actionType") == "CLAIM_BENEFIT"
                          for c in campaigns)
        if not has_benefit:
            return {"kind": "inactive",
                    "message": "该账号尚未在 Qoder 侧开通每日领取(每日 100 Credits); 先用 Qoder 官方客户端登录一次该账号再来"}
        return {"kind": "inactive", "message": "当前没有可领取的活动"}
    total = 0
    first_error = ""
    for t in targets:
        cid = str(t.get("campaignId") or "")
        try:
            cr = await client.post(f"{base}{CAMPAIGNS_PATH}/{cid}/claim",
                                   headers={**headers,
                                            "Content-Type": "application/json"},
                                   content=b"", timeout=15)
            cres = cr.json() if cr.text else {}
        except (httpx.HTTPError, ValueError) as e:
            first_error = first_error or str(e)
            continue
        if cr.status_code in (401, 403):
            return {"kind": "failed", "message": f"凭据已失效(HTTP {cr.status_code}), 请重新登录"}
        if cr.status_code != 200:
            first_error = first_error or f"HTTP {cr.status_code}: {cr.text[:80]}"
            continue
        if cres.get("replayed") is True:
            return {"kind": "already-claimed", "message": "今天已领取"}
        if cres.get("status") not in (None, "CLAIMED"):
            first_error = first_error or f"领取未成功(status={cres.get('status')})"
            continue
        benefit = cres.get("benefit") or {}
        total += int(benefit.get("amount") or 0)
    if total > 0:
        return {"kind": "claimed", "credit": total}
    return {"kind": "failed", "message": first_error or "领取失败"}
