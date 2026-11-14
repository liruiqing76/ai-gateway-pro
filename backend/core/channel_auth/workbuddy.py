# ============================================================
# 白嫖渠道 — WorkBuddy 国际版 (www.workbuddy.ai)
# 同一协议也适配 CodeBuddy 国内版(copilot.tencent.com), 见 CODEBUDDY 常量。
#
# 提取自 dsh-our-free-model 渠道包 vendor/channel-pack/pack.js:
#   src/buddy.ts L33503        端点/头常量 + token 解析
#   src/product.ts L34104      CODEBUDDY / WORKBUDDY 产品常量 + 按模型 UA
#   src/buddy-oauth.ts L43451  state 登录 + token 轮询 + 刷新
#   src/buddy-adapter.ts L35262 推理请求头(send)
#
# 登录(浏览器一次 + 纯 HTTP 轮询):
#   1) POST {endpoint}/v2/plugin/auth/state?platform={platform}
#      头 X-Domain/X-No-Authorization/UA → {state, authUrl}
#   2) 用户浏览器打开 authUrl 登录
#   3) 轮询 GET {endpoint}/v2/plugin/auth/token?state= (1s 间隔)
#      code 11217=未就绪继续等, 200 → token 数据
#   4) (可选) GET /v2/plugin/login/account 拿昵称/uid
#
# 刷新: POST {endpoint}/v2/plugin/auth/token/refresh
#       头 Authorization(旧 token) + X-Refresh-Token + X-Auth-Refresh-Source
# 推理: POST {endpoint}/v2/chat/completions (标准 OpenAI SSE)
# 模型: GET {endpoint}/v3/config
# ============================================================

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from typing import Any

import httpx

CHANNEL_ID = "workbuddy"

# ---- 产品常量(照抄 product.ts) ----
WORKBUDDY_UA_INTL = "WorkBuddy/5.5.2 WorkBuddy AI/5.5.2 CLI/5.5.2"
WORKBUDDY_UA_CN = "WorkBuddy/5.5.2 WorkBuddy/5.5.2 CLI/5.5.2"

WORKBUDDY = {
    "id": "workbuddy",
    "platform": "workbuddy-ai",
    "endpoint": "https://www.workbuddy.ai",
    "api_domain": "www.workbuddy.ai",
    "display_name": "WorkBuddy (国际版)",
    "product_code": "workbuddy",
    "user_agent": WORKBUDDY_UA_INTL,
    # 国际版独有模型线(GPT/Gemini/Claude)→国际 UA; 国内系(glm/hy/kimi/minimax)→国内 UA
    "ua_by_model_family": [
        {"match": "gpt-", "ua": WORKBUDDY_UA_INTL},
        {"match": "gemini-", "ua": WORKBUDDY_UA_INTL},
        {"match": "claude-", "ua": WORKBUDDY_UA_INTL},
        {"match": "glm-", "ua": WORKBUDDY_UA_CN},
        {"match": "hy", "ua": WORKBUDDY_UA_CN},
        {"match": "kimi-", "ua": WORKBUDDY_UA_CN},
        {"match": "minimax-", "ua": WORKBUDDY_UA_CN},
    ],
    "attribution_name": "WorkBuddy",
    "client_version": "5.5.2",
}

# 国内版 CodeBuddy(copilot.tencent.com)同一套协议
CODEBUDDY = {
    "id": "buddy",
    "platform": "ide",
    "endpoint": "https://copilot.tencent.com",
    "api_domain": "copilot.tencent.com",
    "display_name": "CodeBuddy (腾讯)",
    "product_code": "codebuddy",
    "user_agent": "CodeBuddyIDE/1.106.1",
    "ua_by_model_family": [],
    "attribution_name": "CodeBuddy",
    "client_version": "1.106.1",
}

# ---- 路径与业务码常量(照抄 buddy.ts) ----
AUTH_STATE_PATH = "/v2/plugin/auth/state"
AUTH_TOKEN_PATH = "/v2/plugin/auth/token"
LOGIN_ACCOUNT_PATH = "/v2/plugin/login/account"
AUTH_REFRESH_PATH = "/v2/plugin/auth/token/refresh"
CONFIG_PATH = "/v3/config"

LOGIN_TIMEOUT_MS = 5 * 60 * 1000
POLL_INTERVAL_MS = 1000
REQUEST_TIMEOUT_MS = 60_000
CODE_TOKEN_NOT_READY = 11217
CODE_ACCOUNT_NOT_READY = 12151

AUTH_REFRESH_SOURCE = "ide-main"

# WorkBuddy 国际版 /v3/config 实测兜底模型表(照抄 WORKBUDDY_FALLBACK_MODELS 摘要)
FALLBACK_MODELS = [
    {"id": "default-model", "name": "Auto"},
    {"id": "fast-model", "name": "Fast"},
    {"id": "balanced-model", "name": "Balanced"},
    {"id": "primary-model", "name": "Primary"},
    {"id": "deep-model", "name": "Deep"},
    {"id": "deepseek-v4.1-flash", "name": "Deepseek-V4.1-Flash"},
    {"id": "deepseek-v4.1-flash-sg", "name": "Deepseek-V4.1-Flash"},
    {"id": "gpt-6-astra", "name": "GPT-6-Astra"},
    {"id": "gpt-5.6-sol", "name": "GPT-5.6-Sol"},
    {"id": "gpt-5.6-terra", "name": "GPT-5.6-Terra"},
    {"id": "gpt-5.6-luna", "name": "GPT-5.6-Luna"},
    {"id": "gpt-5.5", "name": "GPT-5.5"},
    {"id": "gpt-5.4", "name": "GPT-5.4"},
    {"id": "gemini-3.5-flash", "name": "Gemini-3.5-Flash"},
    {"id": "glm-5.3", "name": "GLM-5.3"},
    {"id": "glm-5.2", "name": "GLM-5.2"},
    {"id": "hy4-preview-f", "name": "Hy4 preview"},
    {"id": "hy4-preview", "name": "Hy4 preview"},
    {"id": "hy3", "name": "Hy3"},
    {"id": "kimi-k3", "name": "Kimi-K3"},
    {"id": "kimi-k2.8-preview", "name": "Kimi-K2.8-Preview"},
    {"id": "kimi-k2.6", "name": "Kimi-K2.6"},
]


def product_for(provider_name: str) -> dict:
    """渠道名 → 产品常量(workbuddy / buddy)。"""
    if provider_name == "buddy":
        return CODEBUDDY
    return WORKBUDDY


def resolve_user_agent(product: dict, model: str) -> str:
    """按模型家族选 UA(照抄 resolveUserAgent)。"""
    for rule in product.get("ua_by_model_family", []):
        if model.startswith(rule["match"]):
            return rule["ua"]
    return product["user_agent"]


# ============================================================
# token 解析(照抄 parseTokenData; 过期时间兼容绝对/相对两种字段)
# ============================================================

def _strip_control_chars(value: str) -> str:
    import re
    return re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()


def _read_str(data: dict, key: str) -> str:
    v = data.get(key)
    if isinstance(v, str):
        return _strip_control_chars(v)
    if isinstance(v, (int, float)):
        return str(v)
    return ""


# code=10001 在不同产品上语义不同: buddy="今天已签到", workbuddy="签到活动未开启或已过期"
_ACTIVITY_CLOSED_HINTS = ("未开启", "已过期", "已结束", "活动结束", "not started",
                          "expired", "has ended")


def _looks_activity_closed(msg: str) -> bool:
    if not msg:
        return False
    low = msg.lower()
    return any(h in low for h in _ACTIVITY_CLOSED_HINTS)


def _jwt_payload(token: str) -> dict:
    if not token:
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(pad.encode()))
    except Exception:
        return {}


def _absolute_expiry_ms(record: dict, absolute_key: str, relative_key: str,
                        access_token: str) -> str:
    """expiresAt(绝对, 秒或 ms 或 ISO) / expiresIn(相对秒) → ms 字符串(照抄 absoluteExpiryMs)。"""
    absolute = _read_str(record, absolute_key)
    if absolute:
        if absolute.isdigit():
            num = int(absolute)
            ms = num if num > 1e12 else num * 1000
            return str(int(ms))
        try:
            from datetime import datetime
            parsed = datetime.fromisoformat(absolute.replace("Z", "+00:00"))
            return str(int(parsed.timestamp() * 1000))
        except ValueError:
            return absolute
    rel = record.get(relative_key)
    if isinstance(rel, (int, float)):
        iat = _jwt_payload(access_token).get("iat")
        base_ms = int(iat * 1000) if isinstance(iat, (int, float)) else int(time.time() * 1000)
        return str(int(base_ms + rel * 1000))
    return ""


def parse_token_data(data: dict) -> dict:
    """token 接口 data → {accessToken, refreshToken, expiresAt, refreshExpiresAt, domain...}。"""
    record = data if isinstance(data, dict) else {}
    access = _read_str(record, "accessToken")
    return {
        "access_token": access,
        "refresh_token": _read_str(record, "refreshToken"),
        "expires_at": _absolute_expiry_ms(record, "expiresAt", "expiresIn", access),
        "refresh_expires_at": _absolute_expiry_ms(record, "refreshExpiresAt", "refreshExpiresIn", access),
        "token_type": _read_str(record, "tokenType") or "Bearer",
        "scope": _read_str(record, "scope"),
        "domain": _read_str(record, "domain"),
    }


def credential_expires_at_ms(credential: dict) -> int | None:
    """凭据过期时间 ms(兼容秒/ms 数字串与 ISO; 兜底解 JWT exp; 照抄 credentialExpiresAtMs)。"""
    raw = credential.get("expires_at", "")
    if isinstance(raw, str) and raw:
        if raw.isdigit():
            v = int(raw)
            return v if v > 1e12 else v * 1000
        try:
            from datetime import datetime
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            pass
    exp = _jwt_payload(credential.get("access_token", "")).get("exp")
    return int(exp * 1000) if isinstance(exp, (int, float)) else None


# ============================================================
# 登录流
# ============================================================

async def start_auth_state(
    client: httpx.AsyncClient, product: dict | None = None,
) -> dict:
    """发起登录: 返回 {state, auth_url}(用户浏览器打开 auth_url)。"""
    p = product or WORKBUDDY
    url = f"{p['endpoint']}{AUTH_STATE_PATH}?platform={p['platform']}"
    headers = {
        "X-Domain": p["api_domain"],
        "X-No-Authorization": "true",
        "X-No-User-Id": "true",
        "X-No-Enterprise-Id": "true",
        "X-No-Department-Info": "true",
        "User-Agent": p["user_agent"],
    }
    resp = await client.post(url, headers=headers, timeout=10)
    body = resp.json() if resp.status_code == 200 else {}
    data = (body or {}).get("data") or {}
    state = data.get("state") or ""
    auth_url = data.get("authUrl") or ""
    if resp.status_code != 200 or not state or not auth_url:
        raise RuntimeError(
            f"auth/state HTTP {resp.status_code}: {json.dumps(body, ensure_ascii=False)[:200]}")
    return {"state": state, "auth_url": auth_url}


async def poll_token(
    client: httpx.AsyncClient, state: str, product: dict | None = None,
    timeout_ms: int = LOGIN_TIMEOUT_MS, interval_ms: int = POLL_INTERVAL_MS,
) -> dict | None:
    """轮询 token(11217=未就绪; 200 → parse_token_data)。超时返回 None。"""
    import asyncio
    p = product or WORKBUDDY
    url = f"{p['endpoint']}{AUTH_TOKEN_PATH}?state={state}"
    headers = {"X-No-Authorization": "true", "User-Agent": p["user_agent"]}
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        await asyncio.sleep(interval_ms / 1000)
        try:
            resp = await client.get(url, headers=headers, timeout=REQUEST_TIMEOUT_MS / 1000)
        except httpx.HTTPError:
            continue
        if resp.status_code == 200:
            data = (resp.json() or {}).get("data")
            if isinstance(data, dict):
                return parse_token_data(data)
            continue
        code = ((resp.json() or {}).get("code")
                if resp.status_code != 200 else 0)
        if code == CODE_TOKEN_NOT_READY:
            continue
        raise RuntimeError(
            f"auth/token HTTP {resp.status_code} code={code}: {resp.text[:200]}")
    return None


async def refresh_token(
    client: httpx.AsyncClient, credential: dict, product: dict | None = None,
) -> dict | None:
    """刷新 token(失败/过期返回 None, 由调用方决定重登录)。"""
    p = product or WORKBUDDY
    if not credential.get("refresh_token"):
        return None
    url = f"{p['endpoint']}{AUTH_REFRESH_PATH}"
    headers = {
        "X-Domain": credential.get("domain") or p["api_domain"],
        "User-Agent": p["user_agent"],
        "Authorization": f"Bearer {credential.get('access_token', '')}",
        "X-Refresh-Token": credential["refresh_token"],
        "X-Auth-Refresh-Source": AUTH_REFRESH_SOURCE,
    }
    try:
        resp = await client.post(url, headers=headers, timeout=REQUEST_TIMEOUT_MS / 1000)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    data = (resp.json() or {}).get("data")
    if not isinstance(data, dict):
        return None
    return parse_token_data(data)


# ============================================================
# 凭据包(网关 Provider 契约)
# ============================================================

def credentials_package(token: dict, provider_name: str = CHANNEL_ID,
                        account: dict | None = None) -> str:
    """token(+账号信息) → JSON 凭据包(存 Provider.api_key_encrypted)。

    uid/enterprise_id 供签到头 X-User-Id / X-Enterprise-Id 使用(登录后补拉,
    拿不到就留空 —— 端点对这两个头是条件必填)。
    """
    acct = account or {}
    return json.dumps({
        "channel": provider_name,
        "access_token": token.get("access_token", ""),
        "refresh_token": token.get("refresh_token", ""),
        "expires_at": token.get("expires_at", ""),
        "refresh_expires_at": token.get("refresh_expires_at", ""),
        "domain": token.get("domain", ""),
        "user_id": acct.get("uid", "") or token.get("user_id", ""),
        "nickname": acct.get("nickname", ""),
        "enterprise_id": acct.get("enterprise_id", ""),
    }, ensure_ascii=False)


def parse_credentials_package(api_key: str, provider_name: str) -> dict | None:
    """Provider.api_key → 凭据包(非本渠道返回 None)。"""
    try:
        pkg = json.loads(api_key)
        if isinstance(pkg, dict) and pkg.get("channel") == provider_name:
            return pkg
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return None


# ============================================================
# 推理请求头(channel_auth 注册表契约)
# ============================================================

def conversation_headers() -> dict:
    """渠道校验必需的「会话 id 三件套」。

    实测(dsh-connect-workbuddy 文档 + 我们自己的复现, 2026-10-24):
    只有 `X-Conversation-ID` 不够, 必须三个一起发, 否则 400 / code 11128
    「request illegal / 请求被拦截」—— 上游把「缺少会话 id」伪装成
    「不是官方客户端发出的」安全文案。三个值都必须是 UUID, 但网关不校验
    来源(任意合法 UUID 都过), 所以每次请求现场生成。

    同时 `ensure_system_head` 仍必须保留: 网关的两道门相互独立, 都要满足
    (headers 三件套 + 首条消息是 system)。
    """
    return {
        "X-Conversation-ID": str(uuid.uuid4()),
        "X-Conversation-Request-ID": str(uuid.uuid4()),
        "X-Conversation-Message-ID": str(uuid.uuid4()),
    }


def chat_headers(credential: dict, provider_name: str, model: str) -> dict:
    """推理请求头(照抄 send() L35262-35274 + 会话 id 三件套)。"""
    p = product_for(provider_name)
    headers = {
        "Authorization": f"Bearer {credential.get('access_token', '')}",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "X-Domain": p["api_domain"] or credential.get("domain", ""),
        "X-Product-Code": p["product_code"],
        "X-Agent-Purpose": "conversation",
        "X-IDE-Name": p["attribution_name"],
        "X-IDE-Type": p["attribution_name"],
        "X-IDE-Version": p["client_version"],
        "X-Product": p["attribution_name"],
        "User-Agent": resolve_user_agent(p, model),
    }
    headers.update(conversation_headers())
    return headers


# ============================================================
# 账号信息(uid / 企业 id: 签到头与用户标识都要用)
# ============================================================

def parse_account_data(data: dict) -> dict:
    """`/v2/plugin/login/account` → {uid, nickname, enterprise_id, account_type}。

    照抄 parseAccountData: 缺字段就给空串/个人账号默认值, 不抛异常。
    """
    record = data if isinstance(data, dict) else {}
    acct_type = _read_str(record, "type") or "personal"
    return {
        "uid": _read_str(record, "uid"),
        "nickname": _read_str(record, "nickname"),
        "enterprise_id": _read_str(record, "enterpriseId"),
        "account_type": acct_type,
    }


async def fetch_account(client: httpx.AsyncClient, credential: dict,
                        provider_name: str = CHANNEL_ID) -> dict:
    """取账号信息(失败返回全空包, 不阻断登录)。"""
    p = product_for(provider_name)
    try:
        resp = await client.post(f"{p['endpoint']}{LOGIN_ACCOUNT_PATH}",
                                 headers={"Authorization": f"Bearer {credential.get('access_token', '')}",
                                          "X-Domain": p["api_domain"],
                                          "User-Agent": p["user_agent"]},
                                 timeout=REQUEST_TIMEOUT_MS / 1000)
    except httpx.HTTPError:
        return parse_account_data({})
    if resp.status_code != 200:
        return parse_account_data({})
    try:
        body = resp.json() or {}
    except ValueError:
        return parse_account_data({})
    data = body.get("data") if isinstance(body, dict) else None
    return parse_account_data(data or {})


# ============================================================
# 每日签到(照抄 credits.ts: WorkBuddy 与 CodeBuddy 共用同一套 billing 端点)
# ============================================================

CHECKIN_STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
DAILY_CHECKIN_PATH = "/v2/billing/meter/daily-checkin"
BUDDY_DEPLOYMENT_TYPE = "SaaS"

CODE_ALREADY_CLAIMED = 10001
CODE_ALREADY_CLAIMED_ALT = 1001
CODE_NO_QUALIFICATION = 1002
CODE_ACTIVITY_ENDED = 1003


def checkin_headers(credential: dict, provider_name: str = CHANNEL_ID) -> dict:
    """签到请求头(照抄 checkinHeaders): X-Product 固定 SaaS, UA 用产品自带 UA。"""
    p = product_for(provider_name)
    headers = {
        "Authorization": f"Bearer {credential.get('access_token', '')}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Domain": p["api_domain"] or credential.get("domain", ""),
        "X-Product": BUDDY_DEPLOYMENT_TYPE,
        "X-Product-Code": p["product_code"],
        "User-Agent": p["user_agent"],
    }
    uid = credential.get("user_id") or ""
    if uid:
        headers["X-User-Id"] = uid
    eid = credential.get("enterprise_id") or ""
    if eid:
        headers["X-Enterprise-Id"] = eid
        headers["X-Tenant-Id"] = eid
    return headers


async def _post_meter(client: httpx.AsyncClient, path: str, credential: dict,
                      provider_name: str) -> httpx.Response:
    p = product_for(provider_name)
    return await client.post(f"{p['endpoint']}{path}",
                             headers=checkin_headers(credential, provider_name),
                             content=b"{}", timeout=REQUEST_TIMEOUT_MS / 1000)


async def fetch_checkin_status(client: httpx.AsyncClient, credential: dict,
                               provider_name: str = CHANNEL_ID) -> dict | None:
    """查签到状态; 非 200 或 code!=0 一律 None(不谎报已签)。"""
    try:
        resp = await _post_meter(client, CHECKIN_STATUS_PATH, credential, provider_name)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict) or body.get("code") != 0:
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    return {
        "active": data.get("active") is True,
        "todayCheckedIn": data.get("today_checked_in") is True,
        "streakDays": int(data.get("streak_days") or 0),
        "dailyCredit": float(data.get("daily_credit") or 0),
        "totalCredits": float(data.get("total_credits") or 0),
        "activityName": _read_str(data, "activity_name"),
    }


async def claim_daily_checkin(client: httpx.AsyncClient, credential: dict,
                              provider_name: str = CHANNEL_ID) -> dict:
    """领取每日签到额度(照抄 claimDailyCheckin 的业务码语义)。

    注意: 上游把业务码放在 HTTP 400 的 body 里(如 code=10001), 所以必须先解析
    body 再按 code 判定, 不能一见非 200 就 failed。同一个 code=10001 在 buddy 上
    表示"今天已签到", 在 workbuddy 上却是"签到活动未开启或已过期", 只能按 msg 区分。
    """
    try:
        resp = await _post_meter(client, DAILY_CHECKIN_PATH, credential, provider_name)
    except httpx.HTTPError as e:
        return {"kind": "failed", "message": f"网络错误: {str(e)[:150]}"}
    if resp.status_code in (401, 403):
        return {"kind": "failed", "code": resp.status_code,
                "message": f"凭据已失效(HTTP {resp.status_code}), 请重新登录该渠道"}
    body = None
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        if resp.status_code != 200:
            return {"kind": "failed", "code": resp.status_code,
                    "message": f"HTTP {resp.status_code}: {resp.text[:120]}"}
        return {"kind": "failed", "message": f"非 JSON 响应: {resp.text[:80]}"}
    code = body.get("code")
    msg = _read_str(body, "msg") or _read_str(body, "message")
    if code in (CODE_ALREADY_CLAIMED, CODE_ALREADY_CLAIMED_ALT):
        # 10001 语义分叉: "今天已签到" vs "活动未开启/已过期"
        if _looks_activity_closed(msg):
            return {"kind": "inactive", "code": code,
                    "message": msg or "签到活动未开启或已过期"}
        return {"kind": "already-claimed", "code": code, "message": msg or "今天已签到"}
    if code in (CODE_NO_QUALIFICATION, CODE_ACTIVITY_ENDED):
        return {"kind": "inactive", "code": code, "message": msg or "当前无领取资格"}
    if code not in (0, None):
        return {"kind": "failed", "code": code, "message": msg or "领取失败"}
    if resp.status_code != 200:
        return {"kind": "failed", "code": resp.status_code,
                "message": f"HTTP {resp.status_code}: {resp.text[:120]}"}
    data = body.get("data")
    if not isinstance(data, dict):
        return {"kind": "failed", "code": code, "message": "领取响应缺少 data 字段"}
    return {"kind": "claimed",
            "credit": float(data.get("credit") or 0),
            "streakDays": int(data.get("streak_days") or 0)}


# ============================================================
# 模型清单
# ============================================================

# 上游 /v3/config 的 models[].credits 形如 "x1" / "1x" / "x0.29" / "x0"(免费),
# 归一为浮点倍率(照 dsh channel-pack pack.js normalizeCreditsRate 的判据)。
_CREDITS_RE = (
    re.compile(r"^x(\d+(?:\.\d+)?)\b", re.IGNORECASE),
    re.compile(r"^(\d+(?:\.\d+)?)x\b", re.IGNORECASE),
)


def normalize_credits(value) -> float | None:
    """credits 字符串 → 浮点倍率; 无效/缺失返回 None(不编造)。"""
    if not isinstance(value, str):
        return None
    head = value.strip().split()[0] if value.strip() else ""
    for pattern in _CREDITS_RE:
        m = pattern.match(head)
        if m:
            return float(m.group(1))
    return None


async def fetch_models(client: httpx.AsyncClient, credential: dict,
                       provider_name: str = CHANNEL_ID) -> list[dict]:
    """GET /v3/config → 模型清单(失败回兜底表)。条目含上游计费倍率 rate。"""
    p = product_for(provider_name)
    headers = {
        "Authorization": f"Bearer {credential.get('access_token', '')}",
        "X-Domain": p["api_domain"],
        "X-Product-Code": p["product_code"],
        "User-Agent": p["user_agent"],
    }
    try:
        resp = await client.get(f"{p['endpoint']}{CONFIG_PATH}", headers=headers, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            # config 形状: agent 模型白名单(字段名按 pack.js reconcile 逻辑兼容几种)
            items = None
            if isinstance(data, dict):
                for key in ("agentModels", "models", "data"):
                    v = data.get(key)
                    if isinstance(v, list):
                        items = v
                        break
                    if isinstance(v, dict):
                        for k2 in ("agentModels", "models", "cli"):
                            v2 = v.get(k2)
                            if isinstance(v2, list):
                                items = v2
                                break
                        break
            if items is not None:
                out = []
                for m in items:
                    if not isinstance(m, dict):
                        continue
                    mid = m.get("id") or m.get("model_id") or ""
                    if not mid:
                        continue
                    entry = {"id": mid, "name": m.get("name") or mid}
                    rate = normalize_credits(m.get("credits"))
                    if rate is not None:
                        entry["rate"] = rate
                    out.append(entry)
                if out:
                    return out
    except Exception:
        pass
    return FALLBACK_MODELS
