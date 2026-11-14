# ============================================================
# 白嫖渠道 — 华为云 CodeArts (snap-access)
#
# 提取自 dsh-our-free-model 渠道包 vendor/channel-pack/pack.js:
#   src/oauth.ts L1175   PKCE + DPoP(ES256) token 交换
#   src/login.ts L1286   OAuth 授权页 + 本地回调 + ticket 流
#   src/sign.ts L30996   华为云 SDK-HMAC-SHA256 请求签名
#   src/models.ts L31042 模型清单(builtin + benefit)
#   src/codearts-credits.ts L57214 每日签到(delivery→claim→confirm)
#
# 登录流(浏览器一次 + 纯 HTTP):
#   1) 本地起 127.0.0.1 回调 server(端口≥10000)
#   2) 生成 PKCE pair + DPoP ES256 密钥对
#   3) 浏览器打开授权页(codearts.huaweicloud.com/portal/authorize?
#      client_id=codearts-agent&port={port}&code_challenge=...&ticket_id=...)
#   4) 用户登录华为云账号 → 回调 /oauth/callback?code=...
#   5) POST sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens
#      (Authorization-Code + DPoP 头) → AK/SK/security_token + refresh_token
#   6) 凭据包(AK/SK/ST + refresh_token + DPoP 私钥 JWK)存 Provider
#
# 刷新: 同端点 grant_type=refresh_token + 新 PKCE verifier + DPoP 头
# 推理: POST snap-access.cn-north-4.myhuaweicloud.com/api/v2/chat/completions
#       华为云 HMAC 签名头 + Chat-Id/Session-Id + benefit 模型加 maas_type
# 签到: GET /v1/ops/delivery?channel=IDE → POST /v1/ops/claim → POST /v1/ops/confirm
# ============================================================

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .huawei_sign import sign_request_huawei

CHANNEL_ID = "codearts"

# ---- 端点常量(照抄 pack.js) ----
PORTAL_AUTHORIZE_BASE = "https://codearts.huaweicloud.com/portal/authorize"
PORTAL_LOGIN_BASE = "https://codearts.huaweicloud.com/portal/login"
STS_TOKEN_ENDPOINT = "https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens"
SNAP_ENGINE_URL = "https://snap-access.cn-north-4.myhuaweicloud.com"
CHAT_API_BASE = f"{SNAP_ENGINE_URL}/api/v2"
MODEL_BUILTIN_URL = f"{SNAP_ENGINE_URL}/v1/model/builtin"
OPENGW_GATEWAY_CONFIG_URL = "https://opengw.developer.huaweicloud.com/api/v1/gateway/config"
CREDENTIAL_ENDPOINT = f"{SNAP_ENGINE_URL}/snap-manager/v1/login/ticket"

CLIENT_ID = "codearts-agent"
LOGIN_PLUGIN_NAME = "snap_AIIDE"
LOGIN_PLUGIN_VERSION = "5.2.0"
REDIRECT_PATH = "/oauth/callback"
OAUTH_THEME = "2"
OAUTH_LOCALE = "zh-cn"
MIN_CALLBACK_PORT = 10000
OAUTH_CALLBACK_TIMEOUT_MS = 180_000
OAUTH_CALLBACK_TIMEOUT_SECONDS = 180

# ticket(secret)流的插件身份(照抄 login.ts PLUGIN_NAME/PLUGIN_VERSION 老常量)
LOGIN_TICKET_PLUGIN_NAME = "snap_jetbrains"
LOGIN_TICKET_PLUGIN_VERSION = "26.3.3"

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Requested-With",
    "Access-Control-Max-Age": "86400",
}

# ticket(secret)流轮询(照抄 pollForCredential: 120 次 × 1s)
CREDENTIAL_POLL_MAX_ATTEMPTS = 120
CREDENTIAL_POLL_INTERVAL_SECONDS = 1.0

# 签到接口(照抄 codearts-credits.ts)
CODEARTS_OPS_DELIVERY_PATH = "/v1/ops/delivery"
CODEARTS_OPS_CLAIM_PATH = "/v1/ops/claim"
CODEARTS_OPS_CONFIRM_PATH = "/v1/ops/confirm"
CODEARTS_OPS_CHANNEL = "IDE"
CODEARTS_DAILY_LOGIN_TYPE = "USER_LOGIN"
CLAIMED_STATUSES = ("CLAIMED", "CONFIRMED", "CONSUMED")
SNAP_EXTRA_HEADERS = {"Agent-Type": "PromptCenter", "X-Language": "zh-cn"}

# benefit(免费额度)模型(照抄 CODEARTS_BENEFIT_FALLBACK; 调用带 maas_type 头)
BENEFIT_FALLBACK_MODELS = ["glm-5.3-flash", "deepseek-v4.1-flash"]

# 目录兜底表(照抄 pack.js DEFAULT_MODELS + CONTEXT_WINDOWS 实测快照)。
# 只有 2 个 benefit 档会把盘古(openpangu-2.0-*)和 GLM-5.2 这些实际可调的
# builtin 模型挡在目录外(用户实测可用) —— 兜底必须给完整清单。
FALLBACK_MODELS = [
    {"id": "GLM-5.2", "name": "GLM-5.2", "contextWindow": 202752},
    {"id": "GLM-5.1", "name": "GLM-5.1"},
    {"id": "GLM-5", "name": "GLM-5"},
    {"id": "glm-5.3-flash", "name": "glm-5.3-flash", "contextWindow": 1048576},
    {"id": "openpangu-2.0-flash", "name": "openpangu-2.0-flash"},
    {"id": "openpangu-2.0-pro", "name": "openpangu-2.0-pro"},
    {"id": "deepseek-v4-flash", "name": "deepseek-v4-flash", "contextWindow": 1048576},
    {"id": "deepseek-v4-pro", "name": "deepseek-v4-pro", "contextWindow": 1048576},
    {"id": "deepseek-v4.1-flash", "name": "deepseek-v4.1-flash", "contextWindow": 1000000},
]

TOKEN_TIMEOUT_SECONDS = 60


# ============================================================
# PKCE + DPoP
# ============================================================

def generate_pkce_pair() -> dict:
    """PKCE pair: code_verifier(base64url 48B) + S256 code_challenge。"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return {"code_verifier": verifier, "code_challenge": challenge}


def _b64url_uint(n: int, length: int = 32) -> str:
    """整数的定长 base64url 编码(JWK/JWS 必须按曲线字段长度补零前缀,
    RFC 7515/7518: ES256 的 r/s 与 x/y 都是固定 32 字节大端)"""
    return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()


def generate_dpop_keypair() -> dict:
    """生成 DPoP ES256 密钥对(返回 JWK 私钥/公钥 dict)。

    cryptography 库的 ES256 签名是纯本地运算, 无网络无时钟。
    """
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import (
        decode_dss_signature, encode_dss_signature)
    from cryptography.hazmat.primitives import hashes

    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key()
    nums = pub.public_numbers()
    pub_jwk = {
        "kty": "EC", "crv": "P-256",
        "x": _b64url_uint(nums.x), "y": _b64url_uint(nums.y),
    }
    priv_jwk = dict(pub_jwk, d=_b64url_uint(priv.private_numbers().private_value))
    return {"private_key_jwk": priv_jwk, "public_key_jwk": pub_jwk}


def sign_dpop_jws(private_key_jwk: dict, htm: str, htu: str,
                  now: float | None = None) -> str:
    """DPoP proof JWT(ES256, typ=dpop+jwt, header 带 jwk 公钥)。

    payload: {htm, htu, iat(秒), jti(32B hex)} —— 照抄 pack.js oauth.ts。
    """
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives import hashes

    d = base64.urlsafe_b64decode(
        (private_key_jwk["d"] + "=" * (-len(private_key_jwk["d"]) % 4)).encode())
    priv = ec.derive_private_key(int.from_bytes(d, "big"), ec.SECP256R1())

    header = {
        "alg": "ES256", "typ": "dpop+jwt",
        "jwk": {k: private_key_jwk[k] for k in ("kty", "crv", "x", "y")},
    }
    payload = {
        "htm": htm, "htu": htu,
        "iat": int(now if now is not None else time.time()),
        "jti": secrets.token_bytes(32).hex(),
    }
    h = base64.urlsafe_b64encode(json.dumps(header, separators=(",", ":")).encode()).rstrip(b"=").decode()
    p = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=").decode()
    signing_input = f"{h}.{p}".encode()
    der = priv.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    # JOSE ES256 签名 = r 与 s 各定长 32 字节大端拼接后**整体** base64url
    # (先拼字节再编码; 把两段 base64 字符串拼起来解码会错位)
    sig_bytes = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    sig = base64.urlsafe_b64encode(sig_bytes).rstrip(b"=").decode()
    return f"{h}.{p}.{sig}"


# ============================================================
# OAuth 流(登录 + 刷新)
# ============================================================

def build_oauth_login_url(port: int, pkce: dict, ticket_id: str) -> str:
    """授权页 URL(照抄 login.ts buildOAuthLoginUrl)。"""
    from urllib.parse import quote
    return (
        f"{PORTAL_AUTHORIZE_BASE}?theme={OAUTH_THEME}&locale={OAUTH_LOCALE}"
        f"&uri_scheme={CLIENT_ID}&client_id={CLIENT_ID}&port={port}"
        f"&code_challenge={pkce['code_challenge']}&code_challenge_method=SHA-256"
        f"&ticket_id={ticket_id}"
        f"&plugin-name={LOGIN_PLUGIN_NAME}&plugin-version={LOGIN_PLUGIN_VERSION}"
    )


def build_portal_login_result_url(succeeded: bool) -> str:
    """授权完成/失败后回跳的 portal 页(照抄 buildPortalLoginResultUrl)。"""
    return (f"{PORTAL_LOGIN_BASE}?login_succeed={str(succeeded).lower()}"
            f"&uri_scheme={CLIENT_ID}&locale={OAUTH_LOCALE}")


def _parse_token_response(data: dict) -> dict:
    """STS token 响应 → 凭据字段(照抄 credentialFromTokenResponse)。"""
    creds = data.get("credentials") or {}
    return {
        "access_key_id": creds.get("access_key_id", ""),
        "secret_access_key": creds.get("secret_access_key", ""),
        "security_token": creds.get("security_token", ""),
        "expires_at": creds.get("expiration", ""),
        "refresh_token": data.get("refresh_token", ""),
    }


class RefreshTokenExpiredError(Exception):
    pass


async def _request_token(client: httpx.AsyncClient, form: dict, keypair: dict) -> dict:
    dpop = sign_dpop_jws(keypair["private_key_jwk"], "POST", STS_TOKEN_ENDPOINT)
    resp = await client.post(
        STS_TOKEN_ENDPOINT,
        headers={"DPoP": dpop,
                 "Content-Type": "application/x-www-form-urlencoded"},
        data=form,
        timeout=TOKEN_TIMEOUT_SECONDS,
    )
    data = None
    try:
        data = resp.json()
    except Exception:
        data = None
    if resp.status_code != 200 or not (data or {}).get("credentials"):
        err_code = str((data or {}).get("error_code", ""))
        msg = f"CodeArts token request failed: {resp.status_code} {json.dumps(data, ensure_ascii=False)[:300]}"
        if (data or {}).get("error") == "invalid_grant" or "ExpiredRefreshToken" in err_code:
            raise RefreshTokenExpiredError(msg)
        raise RuntimeError(msg)
    return data


async def exchange_authorization_code(
    client: httpx.AsyncClient, code: str, code_verifier: str, port: int, keypair: dict,
) -> dict:
    """授权码 → AK/SK/ST/refresh_token(含 code_verifier, 照抄 exchangeAuthorizationCode)。"""
    data = await _request_token(client, {
        "client_id": CLIENT_ID,
        "code": code,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "redirect_uri": f"http://127.0.0.1:{port}{REDIRECT_PATH}",
    }, keypair)
    creds = _parse_token_response(data)
    creds["dpop_private_key_jwk"] = keypair["private_key_jwk"]
    creds["code_verifier"] = code_verifier
    return creds


async def exchange_refresh_token(
    client: httpx.AsyncClient, refresh_token: str, code_verifier: str, keypair: dict,
) -> dict:
    """refresh_token → 新 AK/SK/ST(新 PKCE verifier, 照抄 exchangeRefreshToken)。"""
    data = await _request_token(client, {
        "client_id": CLIENT_ID,
        "code_verifier": code_verifier,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }, keypair)
    creds = _parse_token_response(data)
    creds["dpop_private_key_jwk"] = keypair["private_key_jwk"]
    creds["code_verifier"] = code_verifier
    creds["refresh_token"] = data.get("refresh_token") or refresh_token
    return creds


def _parse_credential_response(data: dict) -> dict | None:
    """ticket 接口响应 → AK/SK 凭据(照抄 parseCredentialResponse 两种形状)。"""
    if not isinstance(data, dict):
        return None
    cred = data.get("credential")
    if isinstance(cred, dict):
        access = cred.get("access") or ""
        st = cred.get("securitytoken") or cred.get("securityToken") or ""
        if access and st:
            return {
                "access_key_id": access,
                "secret_access_key": cred.get("secret") or "",
                "security_token": st,
                "expires_at": cred.get("expires_at") or cred.get("expiresAt") or "",
                "domain_id": data.get("domain_id") or "",
                "user_id": data.get("user_id") or "",
                "user_name": data.get("user_name") or "",
            }
    result = data.get("result")
    if isinstance(result, dict):
        ak = result.get("accessKeyId") or ""
        st = result.get("securityToken") or ""
        if ak and st:
            return {
                "access_key_id": ak,
                "secret_access_key": result.get("secretAccessKey") or "",
                "security_token": st,
                "expires_at": result.get("expiration") or result.get("expiresAt") or "",
            }
    return None


async def poll_for_credential(client: httpx.AsyncClient, secret: str,
                              ticket_id: str = "",
                              max_attempts: int = CREDENTIAL_POLL_MAX_ATTEMPTS,
                              interval: float = CREDENTIAL_POLL_INTERVAL_SECONDS) -> dict:
    """ticket 轮询直接领 AK/SK(照抄 pollForCredential, 老 JetBrains 插件流)。

    GET snap-manager/v1/login/ticket?ticket_id=&secret=
    头 plugin-name=snap_jetbrains / plugin-version=26.3.3(PLUGIN_NAME 老常量)。
    成功返回凭据 dict; 120 次拿不到抛错。
    """
    import asyncio
    url = (f"{CREDENTIAL_ENDPOINT}?ticket_id={quote(ticket_id)}"
           f"&secret={quote(secret)}")
    headers = {"Content-Type": "application/json;charset=UTF-8",
               "plugin-name": LOGIN_TICKET_PLUGIN_NAME,
               "plugin-version": LOGIN_TICKET_PLUGIN_VERSION}
    for i in range(max_attempts):
        if i > 0:
            await asyncio.sleep(interval)
        try:
            resp = await client.get(url, headers=headers, timeout=30)
        except httpx.HTTPError:
            continue
        if resp.status_code != 200:
            continue
        try:
            data = resp.json()
        except ValueError:
            continue
        cred = _parse_credential_response(data)
        if cred:
            return cred
    raise RuntimeError("CodeArts login timed out")


def expires_from_credential(creds: dict) -> int:
    """凭据 expires_at → ms 时间戳(照抄 expiresFromCredential; 缺省 +24h)。"""
    raw = creds.get("expires_at") or ""
    if raw:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except ValueError:
            pass
    return int(time.time() * 1000) + 86_400_000


def credentials_package(creds: dict) -> str:
    """凭据 → JSON 包字符串(存 Provider.api_key_encrypted)。"""
    return json.dumps({"channel": CHANNEL_ID, **creds}, ensure_ascii=False)


def parse_credentials_package(api_key: str) -> dict | None:
    """Provider.api_key 解出的凭据包 → dict(非本渠道返回 None)。"""
    try:
        pkg = json.loads(api_key)
        if isinstance(pkg, dict) and pkg.get("channel") == CHANNEL_ID:
            return pkg
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return None


# ============================================================
# 推理请求签名器(channel_auth 注册表契约)
# ============================================================

# benefit 模型集合 = 兜底表(运行时由 fetch_models 增补 + 本地缓存恢复)
BENEFIT_SET: set[str] = set(BENEFIT_FALLBACK_MODELS)

_BENEFIT_CACHE_FILENAME = "codearts_benefit_models.json"


def _benefit_cache_path() -> Path:
    """benefit 模型缓存路径(对齐插件 codearts_benefit_models.json 的做法)。"""
    import os
    if os.environ.get("GW_CHANNEL_CACHE_DIR"):
        d = Path(os.environ["GW_CHANNEL_CACHE_DIR"])
    elif os.environ.get("GW_DB_PATH"):
        d = Path(os.environ["GW_DB_PATH"]).resolve().parent
    else:
        d = Path(__file__).resolve().parent.parent.parent / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d / _BENEFIT_CACHE_FILENAME


def _load_benefit_cache() -> None:
    """启动时从本地缓存恢复 benefit 集合(重启后推理不漏打 maas_type)。"""
    try:
        raw = json.loads(_benefit_cache_path().read_text(encoding="utf-8"))
        if isinstance(raw, list):
            BENEFIT_SET.update(str(m) for m in raw if m)
    except (OSError, ValueError):
        pass


def _save_benefit_cache() -> None:
    try:
        _benefit_cache_path().write_text(
            json.dumps(sorted(BENEFIT_SET - set(BENEFIT_FALLBACK_MODELS))),
            encoding="utf-8")
    except OSError:
        pass


_load_benefit_cache()


def sign_request_for_chat(creds: dict, method: str, url: str, body: bytes) -> dict:
    """按凭据包对推理请求做华为云签名, 返回完整请求头。

    benefit(免费额度)模型加 maas_type: benefit 头(照抄 stream() L32933);
    Chat-Id/Session-Id/lang 对齐 IDE 请求形态。
    model 名从 URL 无从判断, 由调用方通过 body.model 决定 maas_type。
    """
    import uuid
    model = ""
    try:
        model = json.loads(body.decode("utf-8")).get("model", "")
    except Exception:
        pass
    is_benefit = model in BENEFIT_SET
    extra = {"maas_type": "benefit"} if is_benefit else None
    signed = sign_request_huawei(
        creds.get("access_key_id", ""),
        creds.get("secret_access_key", ""),
        creds.get("security_token", ""),
        method, url, body, extra_headers=extra,
    )
    signed["Chat-Id"] = uuid.uuid4().hex
    signed["Session-Id"] = uuid.uuid4().hex
    signed["lang"] = "en"
    # ⚠️ content-type 已在签名集合里(小写键)。**不可**再加一个大写 "Content-Type":
    # httpx 会把同名不同大小写的头合并成 "application/json,application/json",
    # 华为 APIG 按合并后的值验签 → 恒定 401 verify ak sk signature fail。
    # (实测对照: 裸签名 200, 追加大写 Content-Type 后 401)
    return signed


# ============================================================
# 模型清单
# ============================================================

def _normalize_model_id(mid: str) -> str:
    """去掉模型 id 的 -MMDD 日期后缀(照抄 normalizeModelId)。"""
    if len(mid) > 5:
        suffix = mid[-5:]
        if suffix.startswith("-") and suffix[1:].isdigit() and len(suffix[1:]) == 4:
            return mid[:-5]
    return mid


async def fetch_models(client: httpx.AsyncClient, creds: dict) -> list[dict]:
    """拉模型清单: opengw 网关配置 + snap builtin 双源(照抄 fetchCodeArtsRemoteModels)。

    返回 [{"id", "name", "benefit": bool}]。benefit 标志免费额度模型
    (网关目录里 id 未带日期后缀的即是 benefit 档)。
    """
    ak, sk, st = creds.get("access_key_id", ""), creds.get("secret_access_key", ""), creds.get("security_token", "")
    if not ak or not sk:
        return _benefit_fallback_models()
    out: list[dict] = []
    seen: set[str] = set()

    def _feed(items: list, benefit: bool):
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("model_id") or ""
            if not isinstance(raw_id, str) or not raw_id:
                continue
            mid = _normalize_model_id(raw_id)
            if "-VL-" in mid or mid.endswith("-VL"):
                continue  # 视觉变体不收(照抄)
            if mid in seen:
                continue
            seen.add(mid)
            name = item.get("model_name") or mid
            out.append({"id": mid, "name": _normalize_model_id(str(name)), "benefit": benefit})

    # 源1: opengw 网关配置(result.models, id 无日期后缀 → benefit)
    try:
        signed = sign_request_huawei(ak, sk, st, "GET", OPENGW_GATEWAY_CONFIG_URL, b"")
        resp = await client.get(OPENGW_GATEWAY_CONFIG_URL, headers=signed, timeout=10)
        if resp.status_code == 200:
            arr = ((resp.json() or {}).get("result") or {}).get("models")
            if isinstance(arr, list):
                for item in arr:
                    raw_id = item.get("model_id", "") if isinstance(item, dict) else ""
                    _feed([item], benefit=bool(raw_id) and _normalize_model_id(raw_id) == raw_id)
    except Exception:
        pass

    # 源2: snap builtin(必须带 3 个"不参与签名"的头, 否则 400 请求头Agent-Type为空;
    #      数组在 builtinModels 键下 —— 均照抄 pack.js models.ts)
    try:
        signed = sign_request_huawei(ak, sk, st, "GET", MODEL_BUILTIN_URL, b"")
        signed.pop("host", None)
        signed.update({
            "Content-Type": "application/json",
            "Agent-Type": "PromptCenter",
            "X-Language": "zh-cn",
        })
        resp = await client.get(MODEL_BUILTIN_URL, headers=signed, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            arr = data.get("builtinModels") if isinstance(data, dict) else data
            if not isinstance(arr, list) and isinstance(data, dict):
                arr = data.get("models")
            if isinstance(arr, list):
                _feed(arr, benefit=False)
    except Exception:
        pass

    # benefit 集合增补(供 sign_request_for_chat 用)
    BENEFIT_SET.update(m["id"] for m in out if m.get("benefit"))
    return _benefit_fallback_models(out)


def _benefit_fallback_models(out: list | None = None) -> list[dict]:
    """目录拉空时的兜底: benefit 免费额度模型表。

    与 trae/workbuddy 的"失败回兜底"同思路: 空列表会让渠道一个模型都不显示,
    用户无法从界面区分"没模型"还是"凭据挂了"。兜底模型调用失败会走常规
    熔断/错误提示, 不影响诊断。"""
    if out is None:
        out = []
    out = list(out)
    seen = {m["id"] for m in out}
    for m in FALLBACK_MODELS:
        if m["id"] not in seen:
            out.append({
                "id": m["id"], "name": m["name"],
                # benefit 表内的标 benefit(调用带 maas_type), 其余按 builtin
                "benefit": m["id"] in BENEFIT_FALLBACK_MODELS,
            })
    return out


# ============================================================
# 每日签到(HTTP 接口版, 人家插件就是这么做的)
# ============================================================

async def _signed_snap_request(
    client: httpx.AsyncClient, creds: dict, method: str, url: str, body: bytes | None,
) -> dict:
    """签到族接口的统一封装: 华为云签名 + SNAP 附加头 + 信封解包(照抄 signedSnapRequest)。"""
    ak, sk, st = creds.get("access_key_id", ""), creds.get("secret_access_key", ""), creds.get("security_token", "")
    if not ak or not sk:
        return {"ok": False, "message": "凭据缺少 AK/SK"}
    try:
        payload = body if body is not None else b""
        signed = sign_request_huawei(ak, sk, st, method, url, payload)
        headers = {**signed, **SNAP_EXTRA_HEADERS}
        resp = await client.request(
            method, url, headers=headers,
            content=body if body is not None else None,
            timeout=30,
        )
        text = resp.text
        if resp.status_code != 200:
            return {"ok": False, "message": f"HTTP {resp.status_code}: {text[:200]}"}
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            return {"ok": False, "message": "响应无法解析"}
        code = parsed.get("code")
        if isinstance(code, (int, float)) and code != 0:
            msg = parsed.get("message") or parsed.get("msg") or f"业务码 {code}"
            return {"ok": False, "message": str(msg)}
        data = parsed.get("data") if isinstance(parsed.get("data"), dict) else parsed
        return {"ok": True, "data": data}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def _read_num(source: dict, key: str) -> float:
    v = source.get(key)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return 0.0
    return 0.0


async def claim_daily_checkin(client: httpx.AsyncClient, creds: dict) -> dict:
    """CodeArts 每日签到: 查活动 → claim → confirm(照抄 claimCodeArtsDailyCheckin)。

    返回 {kind: claimed|already-claimed|inactive|failed, credit?, message?}
    """
    # 1) 账户信息(积分包判定)
    info = await _signed_snap_request(
        client, creds, "GET", f"{SNAP_ENGINE_URL}/snap-manager/v1/statistics/plugin", None)
    if not info.get("ok"):
        return {"kind": "failed", "message": f"账户信息查询失败: {info.get('message')}"}
    pkg = (info.get("data") or {}).get("package") or {}
    if not (pkg.get("is_credit_package") is True
            or str(pkg.get("is_credit_package", "")).lower() == "true"):
        if pkg.get("is_token_package"):
            return {"kind": "inactive", "message": "Token 计费账户, 不在积分活动范围"}
        return {"kind": "inactive", "message": "非积分计费账户, 不在积分活动范围"}

    # 2) 活动列表
    acts = await _signed_snap_request(
        client, creds, "GET",
        f"{SNAP_ENGINE_URL}{CODEARTS_OPS_DELIVERY_PATH}?channel={CODEARTS_OPS_CHANNEL}", None)
    if not acts.get("ok"):
        return {"kind": "failed", "message": f"活动列表查询失败: {acts.get('message')}"}
    items = (acts.get("data") or {}).get("items")
    if not isinstance(items, list):
        return {"kind": "failed", "message": "响应缺少 items 字段"}
    activity = next(
        (a for a in items if isinstance(a, dict) and a.get("type") == CODEARTS_DAILY_LOGIN_TYPE),
        None)
    if activity is None:
        return {"kind": "inactive", "message": "未找到每日签到活动"}
    claimable = str(activity.get("claimable", "")).lower() == "true" or activity.get("claimable") is True
    if not claimable:
        status = str(activity.get("status", ""))
        if status in CLAIMED_STATUSES:
            return {"kind": "already-claimed", "message": "今天已领取"}
        return {"kind": "inactive", "message": f"当前不可领取(status={status})"}

    campaign_id = str(activity.get("campaignId") or "")
    if not campaign_id:
        return {"kind": "failed", "message": "活动缺少 campaignId"}

    # 3) claim
    claim = await _signed_snap_request(
        client, creds, "POST", f"{SNAP_ENGINE_URL}{CODEARTS_OPS_CLAIM_PATH}",
        json.dumps({"campaignId": campaign_id, "channel": CODEARTS_OPS_CHANNEL}).encode())
    if not claim.get("ok"):
        return {"kind": "failed", "message": str(claim.get("message"))}

    # 4) confirm(data.id 非空时, 照抄)
    claim_data = claim.get("data") or {}
    if claim_data.get("id") is not None:
        await _signed_snap_request(
            client, creds, "POST", f"{SNAP_ENGINE_URL}{CODEARTS_OPS_CONFIRM_PATH}",
            json.dumps({"campaignId": campaign_id}).encode())

    credit = (_read_num(claim_data, "benefitAmount") or _read_num(claim_data, "credit")
              or _read_num(claim_data, "credits") or _read_num(claim_data, "creditAmount")
              or _read_num(claim_data, "amount"))
    return {
        "kind": "claimed",
        "credit": credit if credit > 0 else _read_num(activity, "benefitAmount"),
    }
