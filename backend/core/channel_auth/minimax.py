# ============================================================
# MiniMax Code(国内版) 渠道适配
#
# 协议来源: dsh-our-free-model pack.js 的 vendor/channel-pack/src/minimax.ts
# (常量与函数逐条对齐), 并已在本机真机验证:
#   - 设备码登录 POST {account}/oauth2/device/code (PKCE S256)
#   - 轮询     POST {account}/oauth2/token  grant_type=device_code
#   - 续期     POST {account}/oauth2/token  grant_type=refresh_token
#   - 模型     GET  {api}/mavis/api/v1/models            (200 实测)
#   - 推理     POST {api}/mavis/api/v1/llm/v1/messages   (200 实测, **Anthropic
#              Messages 形状** + SSE, 故走 channel_transform 反向转换)
#   - 签到状态 GET  {api}/minimax-cloud/api/v1/signin/status?timezone_id=
#   - 签到领取 POST {api}/minimax-cloud/api/v1/signin/claim?timezone_id=  body {}
# 信封: {data: ..., base_resp: {status_code, status_msg}}; status_code != 0 = 业务错。
#
# ⚠️ refresh_token 是**一次性**的(轮换): 用掉即失效
# (`{"error":"invalid_grant","error_description":"this refresh token can no
# longer be used, start a new authorization"}`), 所以每次续期后必须立刻回写。
# ============================================================

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from typing import Any

import httpx

logger = logging.getLogger("gateway.channel_auth.minimax")

CHANNEL_ID = "minimax"
DISPLAY_NAME = "MiniMax Code"

ACCOUNT_HOST = "https://account.minimax.cn"
API_HOST = "https://agent.minimax.cn"

DEVICE_CODE_PATH = "/oauth2/device/code"
TOKEN_PATH = "/oauth2/token"
MODELS_PATH = "/mavis/api/v1/models"
INFER_PATH = "/mavis/api/v1/llm/v1/messages"
SIGNIN_STATUS_PATH = "/minimax-cloud/api/v1/signin/status"
SIGNIN_CLAIM_PATH = "/minimax-cloud/api/v1/signin/claim"
CREDIT_DETAILS_PATH = "/minimax-cloud/api/v1/credit/details"

CLIENT_ID = "mcode-public"
AUDIENCE = "agent-backend"
SCOPE = "agent.default"

REQUEST_TIMEOUT = 30.0
OAUTH_TIMEOUT = 20.0
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# 签到面板状态(parseMinimaxSigninPanel 的 MINIMAX_SIGNIN_STATUS)
SIGNIN_UPCOMING = 1
SIGNIN_CLAIMABLE = 2
SIGNIN_CLAIMED = 3
SIGNIN_DISABLED = 4

# claim 响应 claim_result
CLAIM_OK = 1
CLAIM_ALREADY = 2

PANEL_SCENE_UNKNOWN = 0
PANEL_SCENE_FIRST = 1
PANEL_SCENE_ACTIVE = 2
PANEL_SCENE_COMPLETED = 3
PANEL_SCENE_BROKEN = 4

# 上游 /mavis/api/v1/models 拉不到时的兜底(插件 MINIMAX_FALLBACK_MODELS)
FALLBACK_MODELS: list[dict] = [
    {"id": "MiniMax-M3.1-Flash-Preview", "name": "M3.1-Flash-Preview",
     "context": 1000000, "output": 128000, "vision": True,
     "thinking": "forced_on",
     "efforts": ["default", "low", "medium", "high", "xhigh", "max"],
     "default_effort": "default"},
    {"id": "MiniMax-M3.1-Flash", "name": "M3.1-Flash",
     "context": 512000, "output": 128000, "vision": True,
     "thinking": "forced_on",
     "efforts": ["default", "low", "medium", "high", "xhigh", "max"],
     "default_effort": "default"},
    {"id": "MiniMax-M3", "name": "M3",
     "context": 1000000, "output": 128000, "vision": True,
     "thinking": "switchable", "efforts": [], "default_effort": ""},
    {"id": "MiniMax-M2.7-highspeed", "name": "M2.7-highspeed",
     "context": 200000, "output": 128000, "vision": False,
     "thinking": "forced_on", "efforts": [], "default_effort": ""},
    {"id": "MiniMax-M2.7", "name": "M2.7",
     "context": 200000, "output": 128000, "vision": False,
     "thinking": "forced_on", "efforts": [], "default_effort": ""},
]


# ============================================================
# 小工具
# ============================================================

def _nonempty(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _pos_num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    if isinstance(value, str):
        try:
            v = float(value)
        except ValueError:
            return None
        return v if v > 0 else None
    return None


def _jwt_payload(token: str) -> dict:
    parts = (token or "").split(".")
    if len(parts) != 3:
        return {}
    try:
        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        obj = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _jwt_exp_ms(token: str) -> int | None:
    exp = _jwt_payload(token).get("exp")
    if isinstance(exp, (int, float)) and not isinstance(exp, bool) and exp > 0:
        return int(exp * 1000)
    return None


def _jwt_sub(token: str) -> str | None:
    sub = _jwt_payload(token).get("sub")
    return sub if isinstance(sub, str) and sub else None


def resolve_timezone_id() -> str:
    """签到接口要 timezone_id; 取本机 IANA 名, 失败用 Asia/Shanghai。"""
    for env in ("TZ", "GW_TIMEZONE"):
        raw = os.environ.get(env)
        if raw and raw.strip():
            return raw.strip()
    try:
        import datetime
        # Windows 上 zoneinfo 未必有 tzdata; 直接从本地时区推断偏移
        off = -datetime.datetime.now().astimezone().utcoffset().total_seconds()
        table = {8 * 3600: "Asia/Shanghai", 0: "UTC", 9 * 3600: "Asia/Tokyo",
                 -5 * 3600: "America/New_York", -8 * 3600: "America/Los_Angeles",
                 1 * 3600: "Europe/Berlin", 2 * 3600: "Europe/Athens"}
        return table.get(int(off), "Asia/Shanghai")
    except Exception:
        return "Asia/Shanghai"


# ============================================================
# 设备码登录(PKCE S256)
# ============================================================

def create_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    return verifier, challenge


def parse_device_authorization(payload: Any) -> dict | None:
    if not isinstance(payload, dict):
        return None
    device_code = _nonempty(payload.get("device_code"))
    user_code = _nonempty(payload.get("user_code"))
    uri = _nonempty(payload.get("verification_uri")) or _nonempty(payload.get("verification_url"))
    expires_in = _pos_num(payload.get("expires_in"))
    if not (device_code and user_code and uri and expires_in):
        return None
    interval = _pos_num(payload.get("interval")) or 5.0
    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": uri,
        "verification_uri_complete": (_nonempty(payload.get("verification_uri_complete"))
                                      or uri),
        "expires_in": int(expires_in),
        "interval": int(interval),
    }


async def start_device_login(client: httpx.AsyncClient) -> dict | None:
    """申请设备码 → {device_code,user_code,verification_uri,...,code_verifier}。"""
    verifier, challenge = create_pkce()
    try:
        resp = await client.post(
            ACCOUNT_HOST + DEVICE_CODE_PATH,
            data={"client_id": CLIENT_ID, "scope": SCOPE, "audience": AUDIENCE,
                  "code_challenge": challenge, "code_challenge_method": "S256"},
            headers={"Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"},
            timeout=OAUTH_TIMEOUT)
    except Exception as exc:
        logger.warning("minimax device code request failed: %s", exc)
        return None
    if resp.status_code != 200:
        logger.warning("minimax device code HTTP %s: %s",
                       resp.status_code, resp.text[:200])
        return None
    try:
        parsed = parse_device_authorization(resp.json())
    except Exception:
        return None
    if parsed is None:
        return None
    parsed["code_verifier"] = verifier
    return parsed


def parse_token_grant(payload: Any, previous_refresh: str = "") -> dict | None:
    """oauth2/token 响应 → 凭据 dict; 不合规返回 None(不编造)。"""
    if not isinstance(payload, dict):
        return None
    access = _nonempty(payload.get("access_token"))
    refresh = _nonempty(payload.get("refresh_token")) or previous_refresh
    token_type = _nonempty(payload.get("token_type"))
    expires_in = _pos_num(payload.get("expires_in"))
    if not access or not refresh:
        return None
    if not token_type or token_type.lower() != "bearer":
        return None
    if expires_in is None:
        return None
    scope = payload.get("scope") if isinstance(payload.get("scope"), str) else ""
    expires_at = _jwt_exp_ms(access) or int(time.time() * 1000 + expires_in * 1000)
    out = {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_at": str(expires_at),
        "scope": scope,
    }
    account_id = _jwt_sub(access)
    if account_id:
        out["account_id"] = account_id
    return out


async def poll_device_token(client: httpx.AsyncClient, auth: dict,
                            timeout: float = 300.0) -> dict | None:
    """轮询设备码换 token。pending 继续, 其它错误立即放弃。"""
    deadline = time.monotonic() + min(float(auth.get("expires_in") or 300), timeout)
    interval = float(auth.get("interval") or 5)
    while time.monotonic() < deadline:
        try:
            resp = await client.post(
                ACCOUNT_HOST + TOKEN_PATH,
                data={"grant_type": DEVICE_GRANT,
                      "device_code": auth["device_code"],
                      "client_id": CLIENT_ID,
                      "code_verifier": auth["code_verifier"]},
                headers={"Accept": "application/json",
                         "Content-Type": "application/x-www-form-urlencoded"},
                timeout=OAUTH_TIMEOUT)
        except Exception as exc:
            logger.debug("minimax poll error: %s", exc)
            await _sleep(interval)
            continue
        if resp.status_code == 200:
            try:
                grant = parse_token_grant(resp.json())
            except Exception:
                grant = None
            if grant:
                return grant
            await _sleep(interval)
            continue
        # 400 + authorization_pending = 还在等用户确认
        try:
            err = (resp.json() or {}).get("error") or ""
        except Exception:
            err = ""
        if err in ("authorization_pending", "slow_down"):
            if err == "slow_down":
                interval = min(interval + 5, 30)
            await _sleep(interval)
            continue
        logger.warning("minimax poll stopped: HTTP %s %s",
                       resp.status_code, resp.text[:200])
        return None
    return None


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


async def refresh_credential(client: httpx.AsyncClient, creds: dict) -> dict | None:
    """refresh_token 续期。⚠️ 旧 refresh_token 一次性, 成功后必须立刻回写。"""
    token = _nonempty(creds.get("refresh_token"))
    if not token:
        return None
    try:
        resp = await client.post(
            ACCOUNT_HOST + TOKEN_PATH,
            data={"grant_type": "refresh_token", "refresh_token": token,
                  "client_id": CLIENT_ID, "scope": SCOPE, "audience": AUDIENCE},
            headers={"Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"},
            timeout=OAUTH_TIMEOUT)
    except Exception as exc:
        logger.warning("minimax refresh failed: %s", exc)
        return None
    if resp.status_code != 200:
        logger.warning("minimax refresh HTTP %s: %s", resp.status_code, resp.text[:200])
        return None
    try:
        grant = parse_token_grant(resp.json(), previous_refresh=token)
    except Exception:
        return None
    if grant is None:
        return None
    merged = dict(creds)
    merged.update(grant)
    merged["channel"] = CHANNEL_ID
    return merged


# ============================================================
# 凭据包
# ============================================================

def credential_expires_at_ms(creds: dict) -> int | None:
    raw = creds.get("expires_at")
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        if text.isdigit():
            value = int(text)
            return value if value > 10 ** 12 else value * 1000
        try:
            import datetime
            dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            pass
    elif isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        value = int(raw)
        return value if value > 10 ** 12 else value * 1000
    return _jwt_exp_ms(creds.get("access_token") or "")


def is_expired(creds: dict, skew_ms: int = 60000) -> bool:
    exp = credential_expires_at_ms(creds)
    if exp is None:
        return False
    return int(time.time() * 1000) >= exp - skew_ms


def is_refreshable(creds: dict) -> bool:
    return bool(_nonempty(creds.get("refresh_token")))


def credentials_package(grant: dict, channel: str = CHANNEL_ID) -> str:
    pkg = dict(grant or {})
    pkg["channel"] = channel
    return json.dumps(pkg, ensure_ascii=False)


def parse_credentials_package(api_key: str, channel: str = CHANNEL_ID) -> dict | None:
    try:
        pkg = json.loads(api_key)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(pkg, dict):
        return None
    if pkg.get("channel") != channel:
        return None
    if not _nonempty(pkg.get("access_token")):
        return None
    return pkg


# ============================================================
# 信封与请求
# ============================================================

def unwrap_envelope(payload: Any) -> tuple[dict | None, int, str]:
    """{data, base_resp} → (data, status_code, status_msg); 非 0 = 业务错。"""
    if not isinstance(payload, dict):
        return None, -1, "响应无法解析"
    base = payload.get("base_resp") if isinstance(payload.get("base_resp"), dict) else {}
    code = base.get("status_code")
    code = int(code) if isinstance(code, (int, float)) and not isinstance(code, bool) else 0
    msg = base.get("status_msg") if isinstance(base.get("status_msg"), str) else ""
    if code != 0:
        return None, code, msg or f"业务错误 {code}"
    data = payload.get("data")
    return (data if isinstance(data, dict) else None), 0, ""


def chat_headers(creds: dict) -> dict:
    return {
        "Authorization": f"Bearer {creds.get('access_token', '')}",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
    }


def api_headers(creds: dict) -> dict:
    return {
        "Authorization": f"Bearer {creds.get('access_token', '')}",
        "Accept": "application/json",
    }


# ============================================================
# 模型清单
# ============================================================

def parse_models_payload(payload: Any) -> list[dict]:
    """{providers:[{providerId:'minimax', config:{models:{...}, model_order:[]}}]}。"""
    if not isinstance(payload, dict):
        return []
    providers = payload.get("providers")
    if not isinstance(providers, list):
        return []
    target = None
    for prov in providers:
        if isinstance(prov, dict) and prov.get("providerId") == "minimax":
            target = prov
            break
    if target is None:
        return []
    config = target.get("config")
    if not isinstance(config, dict):
        return []
    models = config.get("models")
    if not isinstance(models, dict):
        return []
    rows: list[dict] = []
    for mid, raw in models.items():
        if not isinstance(mid, str) or not mid:
            continue
        meta = raw if isinstance(raw, dict) else {}
        limit = meta.get("limit") if isinstance(meta.get("limit"), dict) else {}
        ctx_options = meta.get("context_window_options")
        ctx = None
        if isinstance(ctx_options, list):
            nums = [int(v) for v in ctx_options
                    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0]
            if nums:
                ctx = max(nums)
        if ctx is None and isinstance(limit.get("context"), (int, float)):
            ctx = int(limit["context"])
        modalities = meta.get("modalities") if isinstance(meta.get("modalities"), dict) else {}
        inputs = modalities.get("input") if isinstance(modalities.get("input"), list) else []
        thinking = meta.get("thinking_config") if isinstance(meta.get("thinking_config"), dict) else {}
        efforts = meta.get("effort_options") if isinstance(meta.get("effort_options"), list) else []
        rows.append({
            "id": mid,
            "name": _nonempty(meta.get("name")) or mid,
            "context": ctx or 0,
            "output": int(limit["output"]) if isinstance(limit.get("output"), (int, float)) else None,
            "vision": "image" in inputs,
            "thinking": _nonempty(thinking.get("mode")) or "",
            "efforts": [e for e in efforts if isinstance(e, str)],
            "default_effort": _nonempty(meta.get("default_effort")) or "",
        })
    order = config.get("model_order")
    if isinstance(order, list):
        index = {mid: i for i, mid in enumerate(order) if isinstance(mid, str)}
        rows.sort(key=lambda r: index.get(r["id"], 10 ** 6))
    return rows


async def fetch_models(client: httpx.AsyncClient, creds: dict) -> list[dict]:
    """拉真实模型清单; 失败用 FALLBACK_MODELS(不谎报)。"""
    try:
        resp = await client.get(API_HOST + MODELS_PATH, headers=api_headers(creds),
                                timeout=REQUEST_TIMEOUT)
    except Exception as exc:
        logger.warning("minimax models failed: %s", exc)
        return list(FALLBACK_MODELS)
    if resp.status_code != 200:
        logger.warning("minimax models HTTP %s", resp.status_code)
        return list(FALLBACK_MODELS)
    try:
        rows = parse_models_payload(resp.json())
    except Exception:
        rows = []
    return rows or list(FALLBACK_MODELS)


# ============================================================
# 签到(HTTP billing: signin/status + signin/claim)
# ============================================================

def _with_timezone(base: str, path: str) -> str:
    return f"{base}{path}?timezone_id={resolve_timezone_id()}"


def parse_signin_panel(data: Any) -> dict | None:
    """7 天面板 {scene, days:[{day_no,points,bonus_points,status,is_today}]}。"""
    if not isinstance(data, dict):
        return None
    scene = data.get("scene")
    if not isinstance(scene, int) or scene not in (0, 1, 2, 3, 4):
        return None
    raw_days = data.get("days")
    if not isinstance(raw_days, list) or len(raw_days) != 7:
        return None
    days: list[dict] = []
    seen: set[int] = set()
    claimable = 0
    today_count = 0
    for raw in raw_days:
        if not isinstance(raw, dict):
            return None
        day_no = raw.get("day_no")
        points = raw.get("points")
        status = raw.get("status")
        bonus = raw.get("bonus_points")
        bonus = 0 if bonus is None else bonus
        if not isinstance(day_no, int) or day_no < 1 or day_no > 7:
            return None
        if day_no in seen:
            return None
        if not isinstance(points, (int, float)) or isinstance(points, bool) or points < 0:
            return None
        if not isinstance(bonus, (int, float)) or isinstance(bonus, bool) or bonus < 0:
            return None
        if not isinstance(raw.get("is_today"), bool):
            return None
        if not isinstance(status, int) or status not in (1, 2, 3, 4):
            return None
        seen.add(day_no)
        if status == SIGNIN_CLAIMABLE:
            claimable += 1
        if raw["is_today"]:
            today_count += 1
        days.append({"day_no": day_no, "points": int(points),
                     "bonus_points": int(bonus), "status": status,
                     "is_today": raw["is_today"]})
    if claimable > 1 or today_count > 1:
        return None
    return {"scene": scene, "days": days}


def panel_to_status(panel: dict) -> dict:
    today = next((d for d in panel["days"] if d["is_today"]), None)
    claimed_today = bool(today and today["status"] == SIGNIN_CLAIMED)
    ordered = sorted(panel["days"], key=lambda d: d["day_no"])
    today_index = next((i for i, d in enumerate(ordered) if d["is_today"]), -1)
    streak = 0
    if today_index >= 0:
        if ordered[today_index]["status"] == SIGNIN_CLAIMED:
            idx = today_index
        elif ordered[today_index]["status"] == SIGNIN_CLAIMABLE:
            idx = today_index - 1
        else:
            idx = -1
        while idx >= 0:
            if ordered[idx]["status"] != SIGNIN_CLAIMED:
                break
            streak += 1
            idx -= 1
    claimable = next((d for d in panel["days"] if d["status"] == SIGNIN_CLAIMABLE), None)
    target = today or claimable
    return {
        "active": True,
        "today_checked_in": claimed_today,
        "streak_days": streak,
        "daily_credit": target["points"] if target else 0,
        "today_credit": (today["points"] if claimed_today and today else 0),
        "scene": panel["scene"],
    }


async def _request_envelope(client: httpx.AsyncClient, url: str, creds: dict,
                            method: str = "GET", body: str | None = None):
    headers = api_headers(creds)
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        resp = await client.request(method, url, headers=headers, content=body,
                                    timeout=REQUEST_TIMEOUT)
    except Exception as exc:
        return None, -1, str(exc)
    try:
        payload = resp.json()
    except Exception:
        if resp.status_code in (401, 403):
            return None, resp.status_code, f"凭据已失效(HTTP {resp.status_code})，请重新登录"
        return None, resp.status_code, f"服务端返回了非 JSON 响应(HTTP {resp.status_code})"
    data, code, msg = unwrap_envelope(payload)
    if code != 0:
        return None, code, msg
    return data, 0, ""


async def fetch_checkin_status(client: httpx.AsyncClient, creds: dict) -> dict | None:
    data, code, _msg = await _request_envelope(
        client, _with_timezone(API_HOST, SIGNIN_STATUS_PATH), creds, "GET")
    if data is None:
        return None
    panel = parse_signin_panel(data)
    if panel is None:
        return None
    return panel_to_status(panel)


async def claim_daily_checkin(client: httpx.AsyncClient, creds: dict) -> dict:
    """领取每日签到积分。返回 {kind: claimed|already-claimed|inactive|failed, ...}。"""
    data, code, msg = await _request_envelope(
        client, _with_timezone(API_HOST, SIGNIN_CLAIM_PATH), creds, "POST", "{}")
    if data is None:
        return {"kind": "failed", "code": code, "message": msg}
    claim_result = data.get("claim_result")
    if claim_result == CLAIM_ALREADY:
        return {"kind": "already-claimed", "message": "今日已签到"}
    if claim_result != CLAIM_OK:
        return {"kind": "failed", "code": -1,
                "message": "签到响应缺少有效的 claim_result"}
    points = data.get("points")
    points = int(points) if isinstance(points, (int, float)) and not isinstance(points, bool) else 0
    panel = parse_signin_panel(data.get("panel"))
    status = panel_to_status(panel) if panel else {}
    return {
        "kind": "claimed",
        "credit": points,
        "streak_days": status.get("streak_days", 0),
        "is_streak_day": False,
        "message": f"+{points} 积分",
    }
