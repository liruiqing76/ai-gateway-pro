# ============================================================
# 白嫖渠道登录器 — LobsterAI (有道 · lobsterai-server.youdao.com)
#
# 提取自 dsh-our-free-model 渠道包 vendor/channel-pack/pack.js:
#   src/lobsterai.ts        L35590  端点 / 信封 / 凭据包契约
#   src/lobsterai-product.ts L35834 产品常量 + 兜底模型清单
#   src/lobsterai-oauth.ts  L44108  浏览器授权 → 本地回调 → /api/auth/exchange
#   src/lobsterai-adapter.ts L35949 推理请求头(标准 OpenAI SSE, 无协议变换)
#   src/lobsterai-credits.ts L57010 活动签到(活动槽位 → 上下文 → check_in)
#
# 登录(浏览器一次 + 本地回调, 与 TRAE 同构):
#   1) 后端起临时回调服 http://127.0.0.1:{port}/auth/callback
#   2) 授权页 {portal}/portal#/login?source=electron&redirect_uri=...&state=...
#   3) 回调带 code+state → POST {api}/api/auth/exchange
#      body {authCode, firstKeyfrom, latestKeyfrom, uuid, version}
#   4) 信封 {code:0, data:{accessToken, refreshToken, expiresIn, user}}
#
# 刷新: POST {api}/api/auth/refresh
#      {firstKeyfrom, latestKeyfrom, version, uuid?, userId?, refreshToken}
#      ⚠️ latestKeyfrom 用登录时存下的值(插件 RefreshToken 从不更新该字段)
# 推理: POST {api}/api/proxy/v1/chat/completions (标准 OpenAI SSE)
# 模型: GET  {api}/api/models/available?firstKeyfrom=&latestKeyfrom=&version=
# 签到: GET  /api/client-activities/slot?placement=desktop_sidebar&...
#       GET  /api/client-activities/{activityCode}/context?configRevision=
#       POST /api/client-activities/{activityCode}/actions/check_in
#
# 合规提示: 属客户端私有接口(非公开 API), 仅限个人自用。
# ============================================================

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any
from urllib.parse import quote, urlencode

import httpx

CHANNEL_ID = "lobsterai"
DISPLAY_NAME = "LobsterAI (有道)"

# ---- 产品常量(照抄 lobsterai-product.ts) ----
API_BASE = "https://lobsterai-server.youdao.com"
PORTAL_BASE = "https://lobsterai.youdao.com"
CLIENT_VERSION_API = (
    "https://api-overmind.youdao.com/openapi/get/luna/hardware/lobsterai/prod/update")
FALLBACK_CLIENT_VERSION = "2026.9.4"
CLIENT_CAPABILITIES = "kimi-k3-agentic-v1,thinking-level-control-v1"
USER_AGENT = "LobsterAI/0.1.0"

# ---- 路径常量(照抄 lobsterai.ts / lobsterai-credits.ts) ----
EXCHANGE_PATH = "/api/auth/exchange"
REFRESH_PATH = "/api/auth/refresh"
MODELS_PATH = "/api/models/available"
CHAT_PATH = "/api/proxy/v1/chat/completions"
CALLBACK_PATH = "/auth/callback"
ACTIVITY_SLOT_PATH = "/api/client-activities/slot"
ACTIVITY_CONTEXT_PATH = "/api/client-activities"
PROFILE_SUMMARY_PATH = "/api/user/profile-summary"

SLOT_PLACEMENT = "desktop_sidebar"
SLOT_CONTAINER_API_VERSION = "2"
SLOT_PLATFORM = "win32"

# ---- 时长 ----
REQUEST_TIMEOUT = 30.0
LOGIN_TIMEOUT_SECONDS = 600
CALLBACK_PORT = 18731          # TRAE 用 18080; 这家挑一个不冲突的
VERSION_CACHE_TTL = 12 * 3600  # 插件 12h 缓存版本号

# 插件直连可用, 但列表接口 401 时用它兜底(照抄 LOBSTERAI_FALLBACK_MODELS,
# 去掉重复项; 真实清单登录后以 /api/models/available 为准)
FALLBACK_MODELS: list[str] = [
    "deepseek-v4-flash", "deepseek-v4-pro",
    "MiniMax-M3", "MiniMax-M2.7",
    "qwen3.7-max", "qwen3.7-plus", "qwen3.6-plus", "qwen3.5-plus-2026-04-20",
    "kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6", "kimi-k2.5",
    "doubao-seed-2-1-pro-260628", "doubao-seed-2-1-turbo-260628",
    "doubao-seed-2-0-code-preview-260215",
    "glm-5.2", "glm-5.1", "glm-5v-turbo", "glm-5",
]

# 客户端版本号缓存(进程内, 12h) —— 同步头构造用这个, 异步刷新由
# resolve_client_version() 负责(与插件 LobsteraiClientVersionResolver 同构)
_VERSION_CACHE: dict[str, Any] = {"value": FALLBACK_CLIENT_VERSION, "at": 0.0}


# ============================================================
# 基础工具
# ============================================================

def _read_str(source: dict, key: str) -> str:
    """取字符串字段(数字也转串; 缺失给空串, 不抛)。"""
    value = (source or {}).get(key)
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _read_num(source: dict, key: str) -> int | None:
    value = (source or {}).get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def unwrap_envelope(body: Any) -> tuple[dict | None, int, str]:
    """信封拆壳: {code:0, data:{...}} → (data, 0, ""); 否则 (None, code, msg)。"""
    if not isinstance(body, dict):
        return None, -1, "响应不是 JSON 对象"
    code = _read_num(body, "code")
    if code is None:
        code = -1
    message = _read_str(body, "msg") or _read_str(body, "message")
    if code != 0:
        return None, code, message or f"code={code}"
    data = body.get("data")
    if not isinstance(data, dict):
        return None, code, message or "data 为空(access token 可能已失效)"
    return data, 0, ""


def _anonymous_headers() -> dict:
    """登录/兑换用的匿名头(照抄 lobsteraiAnonymousHeaders)。"""
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }


def _auth_headers(creds: dict, accept: str = "application/json") -> dict:
    return {
        "Authorization": f"Bearer {creds.get('access_token', '')}",
        "Accept": accept,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }


def client_version_cached() -> str:
    """当前缓存的客户端版本号(同步路径用; 缺省走兜底常量)。"""
    value = _VERSION_CACHE.get("value")
    return value if isinstance(value, str) and value else FALLBACK_CLIENT_VERSION


async def resolve_client_version(client: httpx.AsyncClient,
                                 force: bool = False) -> str:
    """解析客户端版本号: 进程内缓存 → 上游更新接口 → 兜底常量。

    与插件一致: 取不到不抛错, 返回兜底值(否则一次网络抖动就登录失败)。
    """
    now = time.time()
    cached = _VERSION_CACHE.get("value")
    if (not force and isinstance(cached, str) and cached
            and now - float(_VERSION_CACHE.get("at") or 0) < VERSION_CACHE_TTL):
        return cached
    try:
        resp = await client.get(
            CLIENT_VERSION_API,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            body = resp.json()
            outer = body.get("data") if isinstance(body, dict) else None
            value = outer.get("value") if isinstance(outer, dict) else None
            version = value.get("version") if isinstance(value, dict) else None
            if isinstance(version, str) and version.strip():
                _VERSION_CACHE["value"] = version.strip()
                _VERSION_CACHE["at"] = now
                return version.strip()
    except Exception:
        pass
    _VERSION_CACHE["value"] = FALLBACK_CLIENT_VERSION
    _VERSION_CACHE["at"] = now
    return FALLBACK_CLIENT_VERSION


# ============================================================
# 登录流
# ============================================================

def new_login_session() -> dict:
    """新建登录会话(uuid 兼作 state; firstKeyfrom 为发起时刻毫秒串)。"""
    session_uuid = str(uuid.uuid4())
    return {
        "uuid": session_uuid,
        "state": session_uuid,
        "first_keyfrom": str(int(time.time() * 1000)),
    }


def build_login_url(port: int, session: dict) -> str:
    """授权页 URL(照抄 buildLobsteraiLoginUrl: 参数在 #/login 的 query 里)。"""
    callback = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
    query = urlencode({
        "source": "electron",
        "redirect_uri": callback,
        "state": session.get("state", ""),
    })
    return f"{PORTAL_BASE}/portal#/login?{query}"


def build_callback_url(port: int) -> str:
    return f"http://127.0.0.1:{port}{CALLBACK_PATH}"


def parse_callback_params(params: dict) -> dict | None:
    """回调 query → {code, state}; 缺 code 返回 None。"""
    code = (params or {}).get("code") or ""
    if isinstance(code, list):
        code = code[0] if code else ""
    if not code:
        return None
    state = (params or {}).get("state") or ""
    if isinstance(state, list):
        state = state[0] if state else ""
    return {"code": str(code), "state": str(state)}


def _keyfrom_body(creds: dict, version: str) -> dict:
    """firstKeyfrom/latestKeyfrom/version(+ uuid/userId) 公共体。

    ⚠️ latestKeyfrom 用凭据里**存储的**值, 不取当前时刻 —— 严格对齐插件
    lobsteraiKeyfromBody 的注释(Go 侧 RefreshToken 从不更新该字段)。
    """
    body = {
        "firstKeyfrom": creds.get("first_keyfrom", "") or "",
        "latestKeyfrom": creds.get("latest_keyfrom", "") or "",
        "version": version,
    }
    if creds.get("uuid"):
        body["uuid"] = creds["uuid"]
    if creds.get("user_id"):
        body["userId"] = creds["user_id"]
    return body


def parse_token_payload(data: dict) -> dict:
    """/api/auth/exchange|refresh 的 data → 扁平 token 字段。"""
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    expires_in = _read_num(data, "expiresIn")
    return {
        "access_token": _read_str(data, "accessToken"),
        "refresh_token": _read_str(data, "refreshToken"),
        "expires_in": expires_in,
        "user_id_raw": _read_str(user, "userId"),
        "yid": _read_str(user, "yid"),
        "uid_raw": _read_str(user, "id"),
        "nickname": _read_str(user, "nickname"),
    }


def resolve_uid(payload: dict) -> str:
    """uid 取 user.id → user.userId → user.yid → sha256(token)[:16]。"""
    for key in ("uid_raw", "user_id_raw", "yid"):
        value = payload.get(key) or ""
        if value:
            return str(value)
    token = payload.get("access_token") or ""
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _expires_at_ms(payload: dict, now_ms: int | None = None) -> str:
    """expiresIn(秒) → 绝对毫秒串; 缺失则解 JWT exp; 都没有给空串。"""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, int) and expires_in > 0:
        return str(now + expires_in * 1000)
    exp = _jwt_exp_ms(payload.get("access_token") or "")
    return str(exp) if exp is not None else ""


def _jwt_exp_ms(token: str) -> int | None:
    """JWT payload.exp(秒) → 毫秒; 非 JWT/无 exp 返回 None。"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        import base64
        pad = "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(parts[1] + pad)
        exp = json.loads(raw.decode("utf-8")).get("exp")
        if isinstance(exp, (int, float)):
            return int(exp * 1000)
    except Exception:
        return None
    return None


def build_credential(payload: dict, session: dict) -> dict:
    """token 字段 + 会话字段 → 网关凭据包 dict。"""
    return {
        "channel": CHANNEL_ID,
        "access_token": payload.get("access_token", ""),
        "refresh_token": payload.get("refresh_token", ""),
        "expires_at": _expires_at_ms(payload),
        "uid": resolve_uid(payload),
        "user_id": payload.get("user_id_raw", "") or payload.get("yid", ""),
        "nickname": payload.get("nickname", ""),
        "uuid": session.get("uuid", ""),
        "first_keyfrom": session.get("first_keyfrom", ""),
        "latest_keyfrom": session.get("latest_keyfrom", "")
        or str(int(time.time() * 1000)),
    }


async def exchange_code(client: httpx.AsyncClient, code: str, session: dict,
                        version: str | None = None) -> dict:
    """授权码 → 凭据包; 失败抛 RuntimeError(错误文案已可读)。"""
    ver = version or await resolve_client_version(client)
    now_ms = str(int(time.time() * 1000))
    body = {
        "authCode": code,
        "firstKeyfrom": session.get("first_keyfrom", ""),
        "latestKeyfrom": now_ms,
        "uuid": session.get("uuid", ""),
        "version": ver,
    }
    resp = await client.post(f"{API_BASE}{EXCHANGE_PATH}", headers=_anonymous_headers(),
                             json=body, timeout=REQUEST_TIMEOUT)
    try:
        payload = resp.json()
    except Exception:
        raise RuntimeError(f"exchange 非 JSON 响应(HTTP {resp.status_code}): "
                           f"{resp.text[:120]}")
    data, err_code, message = unwrap_envelope(payload)
    if data is None:
        raise RuntimeError(f"exchange 失败(code={err_code}): {message}")
    token = parse_token_payload(data)
    if not token["access_token"]:
        raise RuntimeError("exchange 响应缺少 accessToken")
    session = dict(session)
    session["latest_keyfrom"] = now_ms
    return build_credential(token, session)


async def refresh_credential(client: httpx.AsyncClient, creds: dict) -> dict | None:
    """刷新凭据; 失败(过期/撤销)返回 None(需重登录)。"""
    refresh = creds.get("refresh_token") or ""
    if not refresh:
        return None
    ver = client_version_cached()
    body = _keyfrom_body(creds, ver)
    body["refreshToken"] = refresh
    try:
        resp = await client.post(f"{API_BASE}{REFRESH_PATH}",
                                 headers=_anonymous_headers(), json=body,
                                 timeout=REQUEST_TIMEOUT)
    except httpx.HTTPError:
        return None
    if resp.status_code in (401, 403):
        return None
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except Exception:
        return None
    data, _code, _msg = unwrap_envelope(payload)
    if data is None:
        return None
    token = parse_token_payload(data)
    if not token["access_token"]:
        return None
    merged = dict(creds)
    merged["access_token"] = token["access_token"]
    # refresh 响应可能不回新 refreshToken(沿用旧的), 不能覆盖成空串
    if token["refresh_token"]:
        merged["refresh_token"] = token["refresh_token"]
    merged["expires_at"] = _expires_at_ms(token)
    if not merged["expires_at"]:
        merged["expires_at"] = creds.get("expires_at", "")
    merged["channel"] = CHANNEL_ID
    return merged


def credential_expires_at_ms(creds: dict) -> int | None:
    """凭据过期毫秒(数字串/秒级/ISO 都兼容; 兜底解 JWT exp)。"""
    from datetime import datetime, timezone
    raw = creds.get("expires_at")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = int(raw)
        return value if value > 1e12 else value * 1000
    text = str(raw or "").strip()
    if text.isdigit():
        value = int(text)
        return value if value > 1e12 else value * 1000
    if text:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            pass
    return _jwt_exp_ms(creds.get("access_token") or "")


def is_expired(creds: dict, skew_ms: int = 60_000) -> bool:
    exp = credential_expires_at_ms(creds)
    if exp is None:
        return False
    return exp <= int(time.time() * 1000) + skew_ms


# ============================================================
# 凭据包(网关 Provider 契约)
# ============================================================

def credentials_package(creds: dict) -> str:
    """凭据 dict → JSON 凭据包(存 Provider.api_key_encrypted)。"""
    pkg = {
        "channel": CHANNEL_ID,
        "access_token": creds.get("access_token", ""),
        "refresh_token": creds.get("refresh_token", ""),
        "expires_at": str(creds.get("expires_at", "") or ""),
        "uid": creds.get("uid", ""),
        "user_id": creds.get("user_id", ""),
        "nickname": creds.get("nickname", ""),
        "uuid": creds.get("uuid", ""),
        "first_keyfrom": creds.get("first_keyfrom", ""),
        "latest_keyfrom": creds.get("latest_keyfrom", ""),
    }
    return json.dumps(pkg, ensure_ascii=False)


def parse_credentials_package(api_key: str) -> dict | None:
    """Provider.api_key → 凭据包(非本渠道返回 None)。"""
    try:
        pkg = json.loads(api_key)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if isinstance(pkg, dict) and pkg.get("channel") == CHANNEL_ID:
        return pkg
    return None


# ============================================================
# 推理请求头 / 模型清单
# ============================================================

def chat_headers(creds: dict, model: str = "",
                 version: str | None = None) -> dict:
    """推理请求头(照抄 lobsteraiChatHeaders)。"""
    headers = _auth_headers(creds, accept="text/event-stream, application/json")
    headers["X-LobsterAI-Client-Capabilities"] = CLIENT_CAPABILITIES
    headers["X-LobsterAI-Client-Version"] = version or client_version_cached()
    return headers


def model_headers(creds: dict, version: str | None = None) -> dict:
    """模型清单请求头(照抄 lobsteraiModelsHeaders)。"""
    headers = _auth_headers(creds, accept="application/json")
    headers["X-LobsterAI-Client-Capabilities"] = CLIENT_CAPABILITIES
    headers["X-LobsterAI-Client-Version"] = version or client_version_cached()
    return headers


def build_models_url(creds: dict, version: str) -> str:
    """模型清单 URL(照抄 buildLobsteraiModelsUrl: 渠道字段作 query)。"""
    body = _keyfrom_body(creds, version)
    query = urlencode({k: v for k, v in body.items()
                       if isinstance(v, str) and v})
    return f"{API_BASE}{MODELS_PATH}?{query}" if query else f"{API_BASE}{MODELS_PATH}"


def parse_models(body: Any) -> list[dict]:
    """模型清单信封 → [{id, name, contextWindow?, maxTokens?, rate?}]。

    rate 来自上游 costMultiplier(dsh channel-pack 显示为 `· x{n}` 的计费倍率),
    供渠道登录后自动写入 model_rates; 上游未下发时不编造。
    """
    raw: Any = None
    if isinstance(body, dict):
        code = _read_num(body, "code")
        if code is None:
            code = -1
        if code != 0:
            return []
        data = body.get("data")
        if isinstance(data, list):
            raw = data
        elif isinstance(data, dict) and isinstance(data.get("data"), list):
            raw = data["data"]
    if not isinstance(raw, list):
        return []
    models: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        model_id = _read_str(item, "modelId")
        if not model_id:
            continue
        name = _read_str(item, "modelName") or model_id
        entry: dict[str, Any] = {"id": model_id, "name": name}
        ctx = _read_num(item, "contextWindow")
        if ctx and ctx > 0:
            entry["contextWindow"] = ctx
        max_tokens = _read_num(item, "maxTokens")
        if max_tokens and max_tokens > 0:
            entry["contextWindow"] = entry.get("contextWindow") or max_tokens
        multiplier = _read_num(item, "costMultiplier")
        if multiplier is not None and multiplier > 0:
            entry["rate"] = float(multiplier)
        models.append(entry)
    return models


async def fetch_models(client: httpx.AsyncClient, creds: dict,
                       allow_fallback: bool = True) -> list[dict]:
    """拉真实模型清单; 失败时按需回退兜底清单(登录页不空手)。"""
    version = await resolve_client_version(client)
    try:
        resp = await client.get(build_models_url(creds, version),
                                headers=model_headers(creds, version),
                                timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            models = parse_models(resp.json())
            if models:
                return models
    except Exception:
        pass
    if not allow_fallback:
        return []
    return [{"id": m, "name": m} for m in FALLBACK_MODELS]


# ============================================================
# 每日签到(活动槽位 → 活动上下文 → check_in 动作)
# ============================================================

async def _get_envelope(client: httpx.AsyncClient, url: str, creds: dict,
                        method: str = "GET", body: str | None = None
                        ) -> tuple[dict | None, int, str]:
    headers = _auth_headers(creds)
    try:
        resp = await client.request(method, url, headers=headers, content=body,
                                    timeout=REQUEST_TIMEOUT)
    except Exception as exc:
        return None, -1, f"网络错误: {str(exc)[:120]}"
    try:
        payload = resp.json()
    except Exception:
        if resp.status_code in (401, 403):
            return None, resp.status_code, "凭据已失效, 请重新登录"
        return None, resp.status_code, f"非 JSON 响应(HTTP {resp.status_code})"
    data, code, message = unwrap_envelope(payload)
    if data is None:
        return None, code, message
    return data, 0, ""


def _slot_url(version: str) -> str:
    query = urlencode({
        "placement": SLOT_PLACEMENT,
        "clientVersion": version,
        "containerApiVersion": SLOT_CONTAINER_API_VERSION,
        "platform": SLOT_PLATFORM,
    })
    return f"{API_BASE}{ACTIVITY_SLOT_PATH}?{query}"


def parse_slot(data: dict) -> dict:
    """活动槽位 → {slot_state, activity_code, config_revision}。"""
    activity = data.get("activity") if isinstance(data.get("activity"), dict) else {}
    return {
        "slot_state": _read_str(data, "slotState"),
        "activity_code": _read_str(activity, "activityCode"),
        "config_revision": _read_num(activity, "configRevision"),
    }


def parse_context(data: dict) -> dict:
    """活动上下文 → {claimed_today, actions}。"""
    state = data.get("state") if isinstance(data.get("state"), dict) else {}
    claimed = state.get("claimedToday")
    actions = data.get("actions")
    names: list[str] = []
    if isinstance(actions, list):
        for item in actions:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                name = _read_str(item, "action") or _read_str(item, "name")
                if name:
                    names.append(name)
    return {"claimed_today": bool(claimed), "actions": names}


async def fetch_checkin_status(client: httpx.AsyncClient,
                               creds: dict) -> dict | None:
    """签到状态(活动未开启/查询失败返回 None, 不谎报)。"""
    version = await resolve_client_version(client)
    slot_data, _code, _msg = await _get_envelope(client, _slot_url(version), creds)
    if slot_data is None:
        return None
    slot = parse_slot(slot_data)
    if slot["slot_state"] != "available" or not slot["activity_code"]:
        return {"active": False, "today_checked_in": False, "daily_credit": 0,
                "streak_days": 0}
    url = (f"{API_BASE}{ACTIVITY_CONTEXT_PATH}/{quote(slot['activity_code'])}"
           f"/context?configRevision={slot['config_revision']}")
    ctx_data, _c2, _m2 = await _get_envelope(client, url, creds)
    if ctx_data is None:
        return None
    ctx = parse_context(ctx_data)
    return {
        "active": "check_in" in ctx["actions"],
        "today_checked_in": ctx["claimed_today"],
        "daily_credit": 0,
        "streak_days": 0,
        "activity_code": slot["activity_code"],
    }


async def claim_daily_checkin(client: httpx.AsyncClient, creds: dict) -> dict:
    """领取每日签到奖励; 返回 {kind: claimed|already-claimed|inactive|failed, ...}。"""
    version = await resolve_client_version(client)
    slot_data, code, message = await _get_envelope(client, _slot_url(version), creds)
    if slot_data is None:
        return {"kind": "failed", "code": code,
                "message": message or "活动槽位查询失败"}
    slot = parse_slot(slot_data)
    if slot["slot_state"] != "available" or not slot["activity_code"]:
        return {"kind": "inactive",
                "message": f"暂无可用活动(slotState={slot['slot_state'] or 'unknown'})"}
    activity = quote(slot["activity_code"])
    ctx_url = (f"{API_BASE}{ACTIVITY_CONTEXT_PATH}/{activity}"
               f"/context?configRevision={slot['config_revision']}")
    ctx_data, code2, msg2 = await _get_envelope(client, ctx_url, creds)
    if ctx_data is None:
        return {"kind": "failed", "code": code2,
                "message": msg2 or "活动上下文查询失败"}
    ctx = parse_context(ctx_data)
    if ctx["claimed_today"]:
        return {"kind": "already-claimed", "message": "今天已签到"}
    if "check_in" not in ctx["actions"]:
        return {"kind": "inactive", "message": "当前不可签到(活动未开放该动作)"}
    body = json.dumps({
        "configRevision": slot["config_revision"],
        "idempotencyKey": str(uuid.uuid4()),   # 客户端幂等键, 服务端据此去重
        "payload": {},
    }, ensure_ascii=False)
    claim_url = f"{API_BASE}{ACTIVITY_CONTEXT_PATH}/{activity}/actions/check_in"
    claim_data, code3, msg3 = await _get_envelope(client, claim_url, creds,
                                                 method="POST", body=body)
    if claim_data is None:
        lowered = (msg3 or "").lower()
        if any(hint in lowered for hint in ("已领取", "已签到", "already")):
            return {"kind": "already-claimed", "message": msg3 or "今天已签到"}
        return {"kind": "failed", "code": code3, "message": msg3 or "领取失败"}
    result = claim_data.get("result") if isinstance(claim_data.get("result"), dict) else {}
    credit = 0
    for key in ("creditsGranted", "rewardCredits", "credits"):
        value = result.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            credit = int(value)
            break
    text = _read_str(result, "message")
    return {
        "kind": "claimed",
        "credit": credit,
        "streak_days": 0,
        "is_streak_day": False,
        "message": text or (f"+{credit} 积分" if credit else "签到成功"),
    }
