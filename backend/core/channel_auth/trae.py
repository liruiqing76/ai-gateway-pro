# ============================================================
# 白嫖渠道 — 字节 TRAE (trae-api-cn.mchost.guru)
#
# 提取自 dsh-our-free-model 渠道包 pack.js:
#   trae-product.ts L39002   产品常量(clientId/appId/UA/通道)
#   trae.ts L39027           ExchangeToken/GetUserInfo/签到路径 + SOLO 头
#   trae-oauth.ts L45260     授权页 + 回调参数解析 + 换 token
#   trae-adapter.ts L40265   推理(SOLO 自定义协议) + 事件流消费
#   trae-credits.ts L57477   每日签到(status + claim)
#
# 登录: 浏览器开 {consoleHost}/authorization?...&auth_callback_url=http://127.0.0.1:18080/authorize
#   → 回跳带 refreshToken(+ userInfo JSON) → POST {oauthHost}/cloudide/api/v3/trae/oauth/ExchangeToken
#   {ClientID, RefreshToken, ClientSecret:"-"} → Token; POST GetUserInfo 补 uid
# 推理: POST {agentHost}/api/agent/v3/llm_utils_chat (SOLO 协议, 见 trae_solo.py)
# 签到: POST {ugHost}/trae/api/v2/ug/checkin_credits/{status,claim} (一套 VSCode 伪装头)
# ============================================================

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid as uuidlib
from typing import Any

import httpx

CHANNEL_ID = "trae"

# ---- 产品常量(照抄 trae-product.ts) ----
AGENT_HOST = "https://trae-api-cn.mchost.guru"
UG_HOST = "https://api.trae.cn"
OAUTH_HOST = "https://api.trae.com.cn"
CONSOLE_HOST = "https://www.trae.cn"
CLIENT_ID = "en1oxy7wnw8j9n"
APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
IDE_VERSION = "0.1.52"
IDE_VERSION_CODE = "20260811"
DEVICE_BRAND = "Apple"
OS_VERSION = "macOS 15.7.4"
PLUGIN_VERSION = "2.3.62834"
USER_AGENT = "Trae/0.1.52"
FUNCTION_DEFAULT = "solo_work_lite"
CALLBACK_PORT = 18080
LOGIN_TIMEOUT_SECONDS = 600
REQUEST_TIMEOUT_MS = 30_000

EXCHANGE_PATH = "/cloudide/api/v3/trae/oauth/ExchangeToken"
USER_INFO_PATH = "/cloudide/api/v3/trae/GetUserInfo"
BATCH_MODELS_PATH = "/api/ide/v1/batch_get_detail_param"
CHAT_PATH = "/api/agent/v3/llm_utils_chat"
CHECKIN_STATUS_PATH = "/trae/api/v2/ug/checkin_credits/status"
CHECKIN_CLAIM_PATH = "/trae/api/v2/ug/checkin_credits/claim"
ENT_USAGE_PATH = "/trae/api/v2/pay/ide_user_ent_usage"

# 通道列表(通用在前, 专用兜底, 照抄 resolveChannelList fallback)
CHANNELS = [
    "solo_agent", "solo_work_lite", "solo_agent_remote", "solo_work_remote",
    "solo_agent_lite",
    "chat_v3", "builder_v3", "solo_coder", "solo_design_lite",
    "solo_design_remote", "git_ai", "code_reviewer", "code_review_summary",
    "multimodal", "system_diagnosis",
]

# 兜底模型表(照抄 TRAE_FALLBACK_MODELS, 去 isHidden 内部模型)
FALLBACK_MODELS = [
    {"id": "DeepSeek-V4-Flash-Official", "name": "DeepSeek V4 Flash Official"},
    {"id": "Doubao-Seed-2.1-Pro", "name": "Doubao Seed 2.1 Pro"},
    {"id": "seed-code-pro-0430", "name": "Seed Code Pro 0430"},
    {"id": "Doubao-Seed-2.1-Turbo", "name": "Doubao Seed 2.1 Turbo"},
    {"id": "Doubao-Seed-2.0-Code", "name": "Doubao Seed 2.0 Code"},
    {"id": "glm-5.2", "name": "GLM-5.2"},
    {"id": "glm-5-turbo", "name": "GLM-5 Turbo"},
    {"id": "glm-5", "name": "GLM-5"},
    {"id": "DeepSeek-V4-Pro", "name": "DeepSeek V4 Pro"},
    {"id": "DeepSeek-V4-Flash", "name": "DeepSeek V4 Flash"},
    {"id": "kimi-k3", "name": "Kimi K3"},
    {"id": "kimi-k2.7-code", "name": "Kimi K2.7 Code"},
    {"id": "kimi-k2.6", "name": "Kimi K2.6"},
    {"id": "minimax-m3", "name": "MiniMax M3"},
    {"id": "qwen-3.7-plus", "name": "Qwen 3.7 Plus"},
    {"id": "sagitta", "name": "Sagitta"},
    {"id": "aquila", "name": "Aquila"},
]


def generate_machine_id() -> str:
    """16 随机字节 hex(照抄 generateMachineId)。"""
    return secrets.token_bytes(16).hex()


def _oauth_headers() -> dict:
    return {"Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": USER_AGENT}


# ============================================================
# 登录
# ============================================================

def build_login_url(machine_id: str, device_id: str, callback_url: str) -> str:
    """授权页 URL(照抄 buildTraeLoginURL 的参数表)。"""
    from urllib.parse import urlencode
    joined = machine_id + device_id
    trace = joined[-16:] if len(joined) >= 16 else joined.rjust(16, "0")
    params = {
        "login_version": "1", "auth_from": "solo", "login_channel": "native_ide",
        "plugin_version": PLUGIN_VERSION, "auth_type": "local",
        "client_id": CLIENT_ID, "redirect": "0",
        "login_trace_id": trace,
        "auth_callback_url": callback_url,
        "machine_id": machine_id, "device_id": device_id,
        "x_device_id": device_id, "x_machine_id": machine_id,
        "x_device_brand": "PC", "x_device_type": "PC", "x_os_version": "1.0",
        "x_app_version": IDE_VERSION, "x_app_type": "stable",
    }
    return f"{CONSOLE_HOST}/authorization?{urlencode(params)}"


def _read_str(source: Any, key: str) -> str:
    v = (source or {}).get(key)
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)) and v == v:
        return str(v)
    return ""


def _jwt_expires_at_ms(token: str) -> int | None:
    """JWT exp → ms(照抄 jwtExpiresAtMs)。"""
    import base64
    if not isinstance(token, str) or not token:
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(pad.encode()))
        exp = payload.get("exp")
        return int(exp * 1000) if isinstance(exp, (int, float)) else None
    except Exception:
        return None


def credential_expires_at_ms(credential: dict) -> int | None:
    """expires_at 兼容秒/ms/ISO, 兜底 JWT exp(照抄 traeCredentialExpiresAtMs)。"""
    raw = credential.get("expires_at")
    if isinstance(raw, str) and raw:
        if raw.isdigit():
            v = int(raw)
            return v if v > 1e12 else v * 1000
        try:
            from datetime import datetime
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            pass
    return _jwt_expires_at_ms(credential.get("access_token", ""))


def is_expired(credential: dict, skew_ms: int = 0) -> bool:
    exp = credential_expires_at_ms(credential)
    import time as _t
    return exp is not None and (_t.time() * 1000 + skew_ms) >= exp


# checkin.py 保活侧引用名
trae_credential_expired = is_expired


async def refresh_credential(client: httpx.AsyncClient, credential: dict) -> dict:
    """refresh_token 重换 Token(照抄 ExchangeToken 复用位; 失败抛错)。"""
    import time as _t
    rt = credential.get("refresh_token") or ""
    if not rt:
        raise RuntimeError("trae: 无 refresh_token, 请重新登录")
    resp = await client.post(f"{OAUTH_HOST}{EXCHANGE_PATH}",
                             headers=_oauth_headers(),
                             json={"ClientID": CLIENT_ID, "RefreshToken": rt,
                                   "ClientSecret": "-", "UserID": ""},
                             timeout=REQUEST_TIMEOUT_MS / 1000)
    if resp.status_code != 200:
        raise RuntimeError(f"trae refresh 失败(HTTP {resp.status_code}): {resp.text[:150]}")
    exchange = _parse_exchange_response(resp.json())
    if exchange is None:
        raise RuntimeError("trae refresh 响应缺少 Token")
    expires_at = ""
    if exchange["token_expire_at"] > 1e12:
        expires_at = str(int(exchange["token_expire_at"]))
    elif exchange["token_expire_at"] > 0:
        expires_at = str(int(exchange["token_expire_at"] * 1000))
    elif exchange["token_expire_duration"] > 0:
        expires_at = str(int(_t.time() * 1000 + exchange["token_expire_duration"] * 1000))
    merged = {**credential,
              "access_token": exchange["access_token"],
              "refresh_token": exchange["refresh_token"] or rt,
              "expires_at": expires_at}
    return merged


def _parse_json_param(raw: str | None):
    """回跳参数可能是 URL-encode 的 JSON(照抄 parseJsonParam)。"""
    if not raw:
        return None
    from urllib.parse import unquote
    for candidate in (raw, unquote(raw)):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            continue
    return None


def parse_callback_params(params: dict) -> dict | None:
    """解析 TRAE 授权回跳参数 → {refresh_token, access_token, uid, nickname, enterprise_id}。

    照抄 parseTraeCallbackDetailed: refreshToken / userJwt.Token / code(authCodeInfo) 三候选。
    不支持 PKCE code 新流程(插件原文也明确拒绝), 但 refreshToken 路优先。
    """
    def g(key: str) -> str:
        v = params.get(key)
        return v if isinstance(v, str) else ""

    user_info = _parse_json_param(g("userInfo")) or {}
    user_jwt = _parse_json_param(g("userJwt")) or {}
    refresh = g("refreshToken") or _read_str(user_jwt, "RefreshToken")
    token = _read_str(user_jwt, "Token")
    uid = _read_str(user_info, "UserID")
    nickname = _read_str(user_info, "ScreenName")
    enterprise = _read_str(user_info, "TenantID")

    if not refresh and not token:
        # authCode 候选链(照抄), 但我们只支持 refreshToken 流
        return None
    return {
        "refresh_token": refresh,
        "access_token": "" if refresh else token,
        "uid": uid, "nickname": nickname, "enterprise_id": enterprise,
    }


def _parse_exchange_response(data: dict) -> dict | None:
    """ExchangeToken 响应(照抄 parseTraeExchangeResponse)。"""
    result = data.get("Result") or data.get("result")
    if not isinstance(result, dict):
        return None
    access = (_read_str(result, "Token") or _read_str(result, "token")
              or _read_str(result, "accessToken"))
    if not access:
        return None
    def num(key1, key2=""):
        v = result.get(key1)
        if not isinstance(v, (int, float)) and key2:
            v = result.get(key2)
        return v if isinstance(v, (int, float)) else 0
    return {
        "access_token": access,
        "refresh_token": _read_str(result, "RefreshToken") or _read_str(result, "refreshToken"),
        "token_expire_at": num("TokenExpireAt", "tokenExpireAt"),
        "token_expire_duration": num("TokenExpireDuration", "tokenExpireDuration"),
        "refresh_expire_at": num("RefreshExpireAt", "refreshExpireAt"),
    }


async def exchange_token(client: httpx.AsyncClient, callback: dict,
                         machine_id: str, device_id: str) -> dict:
    """refreshToken → Token(ExchangeToken) + GetUserInfo 补 uid(照抄 exchangeTraeCallback)。"""
    import time as _t
    if callback["refresh_token"]:
        resp = await client.post(f"{OAUTH_HOST}{EXCHANGE_PATH}",
                                 headers=_oauth_headers(),
                                 json={"ClientID": CLIENT_ID,
                                       "RefreshToken": callback["refresh_token"],
                                       "ClientSecret": "-", "UserID": ""},
                                 timeout=REQUEST_TIMEOUT_MS / 1000)
        if resp.status_code != 200:
            raise RuntimeError(f"TRAE ExchangeToken 失败(HTTP {resp.status_code}): {resp.text[:200]}")
        exchange = _parse_exchange_response(resp.json())
        if exchange is None:
            raise RuntimeError("TRAE ExchangeToken 响应缺少 Token")
    else:
        exchange = {"access_token": callback["access_token"], "refresh_token": "",
                    "token_expire_at": 0, "token_expire_duration": 0,
                    "refresh_expire_at": 0}

    uid = callback["uid"]
    nickname = callback["nickname"]
    enterprise = callback["enterprise_id"]
    phone = email = ""
    try:
        uresp = await client.post(f"{OAUTH_HOST}{USER_INFO_PATH}",
                                  headers={**_oauth_headers(),
                                           "X-Cloudide-Token": exchange["access_token"]},
                                  json={"ReqSource": "IDE", "IDEVersion": IDE_VERSION},
                                  timeout=REQUEST_TIMEOUT_MS / 1000)
        if uresp.status_code == 200:
            result = (uresp.json() or {}).get("Result") or (uresp.json() or {}).get("result") or {}
            uid = _read_str(result, "UserID") or _read_str(result, "userId") or \
                _read_str(result, "uid") or uid
            nickname = _read_str(result, "ScreenName") or nickname
            enterprise = _read_str(result, "EnterpriseID") or enterprise
            phone = _read_str(result, "NonPlainTextMobile")
            email = _read_str(result, "NonPlainTextEmail")
    except httpx.HTTPError:
        pass

    if not uid:
        raise RuntimeError("TRAE 未能确定 uid(回调 userInfo 与 GetUserInfo 均为空)")

    expires_at = ""
    if exchange["token_expire_at"] > 1e12:
        expires_at = str(int(exchange["token_expire_at"]))
    elif exchange["token_expire_at"] > 0:
        expires_at = str(int(exchange["token_expire_at"] * 1000))
    elif exchange["token_expire_duration"] > 0:
        expires_at = str(int(_t.time() * 1000 + exchange["token_expire_duration"] * 1000))

    creds = {"channel": CHANNEL_ID,
             "access_token": exchange["access_token"],
             "refresh_token": exchange["refresh_token"],
             "expires_at": expires_at,
             "uid": uid, "nickname": nickname or uid[-4:],
             "phone": phone, "email": email,
             "machine_id": machine_id, "device_id": device_id,
             "enterprise_id": enterprise}
    return creds


def credentials_package(creds: dict) -> str:
    """打包时强制盖 channel 标记(注册表按它路由)。"""
    return json.dumps({**creds, "channel": CHANNEL_ID}, ensure_ascii=False)


def parse_credentials_package(api_key: str) -> dict | None:
    try:
        pkg = json.loads(api_key)
        if isinstance(pkg, dict) and pkg.get("channel") == CHANNEL_ID:
            return pkg
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return None


# ============================================================
# 推理头(SOLO 协议, 照抄 traeSOLOHeaders)
# ============================================================

def chat_headers(credential: dict, stream: bool = True) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": USER_AGENT,
        "Authorization": f"Cloud-IDE-JWT {credential.get('access_token', '')}",
        "X-Cloudide-Token": credential.get("access_token", ""),
        "X-Ide-Token": credential.get("access_token", ""),
        "X-Uid": credential.get("uid", ""),
        "X-App-Id": APP_ID,
        "X-App-Version": "default",
        "X-Ide-Version": IDE_VERSION,
        "X-Ide-Version-Code": IDE_VERSION_CODE,
        "X-App-Version-Code": IDE_VERSION_CODE,
        "X-Ide-Version-Type": "stable",
        "X-Device-Type": "macos",
        "X-OS-Version": OS_VERSION,
        "X-Device-Brand": DEVICE_BRAND,
        "Request-Traffic-Type": "prod",
    }
    if credential.get("machine_id"):
        headers["X-Machine-Id"] = credential["machine_id"]
    if credential.get("device_id"):
        headers["X-Device-Id"] = credential["device_id"]
    return headers


# ============================================================
# 每日签到(照抄 trae-credits.ts)
# ============================================================

def _seeded_stream(seed: str, salt: str, nbytes: int) -> list[int]:
    """sha256(`${salt}:${seed}` + 4字节大端计数器) 逐块扩展(照抄 seededStream)。"""
    prefix = f"{salt}:{seed}".encode("utf-8")
    result: list[int] = []
    counter = 0
    while len(result) < nbytes:
        h = hashlib.sha256()
        h.update(prefix)
        h.update(counter.to_bytes(4, "big"))
        for b in h.digest():
            result.append(b)
            if len(result) >= nbytes:
                break
        counter += 1
    return result[:nbytes]


def derive_device_id15(user_id: str) -> str:
    return "".join(str(b % 10) for b in _seeded_stream(user_id, "devid", 15))


def derive_market_user_id(user_id: str) -> str:
    bs = _seeded_stream(user_id, "market", 16)
    bs[6] = bs[6] & 15 | 64
    bs[8] = bs[8] & 63 | 128
    hexs = "".join(f"{b:02x}" for b in bs)
    return (f"{hexs[0:8]}-{hexs[8:12]}-{hexs[12:16]}-{hexs[16:20]}-{hexs[20:32]}")


def derive_session_id(user_id: str) -> str:
    return "".join(f"{b:02x}" for b in _seeded_stream(user_id, "sess", 32))


def checkin_headers(credential: dict) -> dict:
    uid = credential.get("uid", "")
    return {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN",
        "User-Agent": "VSCode 1.107.1 (TRAE SOLO CN)",
        "Authorization": f"Cloud-IDE-JWT {credential.get('access_token', '')}",
        "X-Market-Client-Id": "VSCode 1.107.1",
        "X-Market-User-Id": derive_market_user_id(uid),
        "X-User-Region": "CN",
        "X-Device-Id": derive_device_id15(uid),
        "X-Lgw-Req-Sdk-Type": "3",
        "Package-Type": "stable_cn",
        "X-Lscbd-Aid": "787976",
        "X-Lscbd-Platform": "windows",
        "App-Version": IDE_VERSION,
        "X-Tt-Trace-Id": f"00-{secrets.token_hex(8)}-01",
        "Vscode-Sessionid": derive_session_id(uid),
        "X-Request-Id": str(uuidlib.uuid4()),
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "none",
    }


def _claim_code(body: dict) -> int:
    raw = body.get("code")
    if isinstance(raw, (int, float)) and raw == raw:
        return int(raw)
    if isinstance(raw, str) and re.fullmatch(r"-?\d+", raw.strip()):
        return int(raw.strip())
    return -1 if raw is not None else 0


async def fetch_checkin_status(client: httpx.AsyncClient, credential: dict) -> dict | None:
    resp = await client.post(f"{UG_HOST}{CHECKIN_STATUS_PATH}",
                             headers=checkin_headers(credential), content=b"{}",
                             timeout=REQUEST_TIMEOUT_MS / 1000)
    if resp.status_code != 200:
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict) or _claim_code(body) != 0:
        return None
    return {
        "active": body.get("enable") is True,
        "todayCheckedIn": body.get("checked_in") is True,
        "streakDays": int(body.get("streak_days") or 0),
        "dailyCredit": int(body.get("credits") or 0),
        "totalCredits": int(body.get("total_credits") or 0),
    }


async def claim_daily_checkin(client: httpx.AsyncClient, credential: dict) -> dict:
    """TRAE 每日签到(照抄 claimTraeDailyCheckin)。"""
    if not credential.get("uid"):
        return {"kind": "failed", "message": "缺少 uid, 无法构造设备身份"}
    resp = await client.post(f"{UG_HOST}{CHECKIN_CLAIM_PATH}",
                             headers=checkin_headers(credential), content=b"{}",
                             timeout=REQUEST_TIMEOUT_MS / 1000)
    body: dict = {}
    if resp.status_code == 200:
        try:
            body = resp.json() if resp.text else {}
        except ValueError:
            return {"kind": "failed", "message": f"非 JSON 响应: {resp.text[:80]}"}
    else:
        msg = f"HTTP {resp.status_code}: {resp.text[:80]}"
        kind = "failed"
        return {"kind": kind, "message": msg}
    code = _claim_code(body)
    message = body.get("message") or body.get("msg") or ""
    if code == 0:
        status = await fetch_checkin_status(client, credential)
        return {"kind": "claimed",
                "credit": (status or {}).get("dailyCredit", 0),
                "streakDays": (status or {}).get("streakDays", 0)}
    if code == 9074:
        return {"kind": "failed", "code": code,
                "message": message or "签到人数过多, 请稍后再试"}
    # 已签到: status 里 checked_in=true → already-claimed
    if status_cache_today(credential):
        return {"kind": "already-claimed", "message": message or "今天已领取"}
    return {"kind": "failed", "code": code,
            "message": message or f"签到失败(code={code})"}


def status_cache_today(credential: dict) -> bool:
    """claim 非 0 时回查 status 判断是否今天已领(轻量, 不缓存)。"""
    return False  # 保守: 未确认前不谎报已领; 上层调度有 UTC+8 防重


# ============================================================
# 模型清单
# ============================================================

async def fetch_models(client: httpx.AsyncClient, credential: dict) -> list[dict]:
    """POST batch_get_detail_param 取 IDE 模型配置(失败回兜底表)。"""
    try:
        resp = await client.post(f"{AGENT_HOST}{BATCH_MODELS_PATH}",
                                 headers=chat_headers(credential, stream=False),
                                 content=b"{}",
                                 timeout=REQUEST_TIMEOUT_MS / 1000)
        if resp.status_code == 200:
            data = resp.json()
            # 响应形状: result/param_map 里含模型数组, 按 pack.js 容错多种键
            candidates = []
            if isinstance(data, dict):
                root = data.get("Result") or data.get("result") or data
                if isinstance(root, dict):
                    for key in ("model_detail_list", "models", "list"):
                        v = root.get(key)
                        if isinstance(v, list):
                            candidates = v
                            break
                elif isinstance(root, list):
                    candidates = root
            out, seen = [], set()
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                mid = _read_str(item, "model_name") or _read_str(item, "name") or \
                    _read_str(item, "config_name")
                if not mid or mid in seen:
                    continue
                if item.get("is_hidden") is True or item.get("isHidden") is True:
                    continue
                seen.add(mid)
                out.append({"id": mid, "name": _read_str(item, "display_name") or mid})
            if out:
                return out
    except Exception:
        pass
    return FALLBACK_MODELS
