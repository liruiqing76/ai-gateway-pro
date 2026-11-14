# ============================================================
# 白嫖渠道登录器 — Cline (api.cline.bot)
#
# 参考 dsh-our-free-model 渠道包(vendor/channel-pack/pack.js)的 cline 实现:
#   cline-oauth.ts L46209  WorkOS 设备码流
#   cline-product.ts L40970  产品常量
#
# 流程(全程纯 HTTP, 无本地回调 server):
#   1) POST api.workos.com/user_management/authorize/device  → device_code/user_code/verification_uri
#   2) 用户浏览器打开 verification_uri_complete 确认授权
#   3) 轮询 POST api.workos.com/user_management/authenticate
#      (grant_type=urn:ietf:params:oauth:grant-type:device_code)
#      pending 时返回 400 {"error":"authorization_pending"}
#   4) POST api.cline.bot/api/v1/auth/register 兑换 cline token (前缀 workos:)
#   5) 刷新 POST api.cline.bot/api/v1/auth/refresh
#
# 推理端点: POST api.cline.bot/api/v1/chat/completions (标准 OpenAI SSE)
#   附加头: HTTP-Referer: https://cline.bot / X-Title: Cline / X-CLIENT-TYPE: cline-sdk
# 免费模型: cline-free/ 前缀(GET /api/v1/models 过滤 free)
#
# 合规提示: 属 Cline 服务条款灰色地带的私有接口, 仅限个人自用。
# ============================================================

from __future__ import annotations

import time
from typing import Any

import httpx

WORKOS_BASE = "https://api.workos.com"
WORKOS_CLIENT_ID = "client_01K3A541FN8TA3EPPHTD2325AR"
CLINE_API_BASE = "https://api.cline.bot"

DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# 轮询节奏: 与插件一致(5s 间隔); 总时长上限 10 分钟
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 600

# 渠道在网关里的标识(Provider.name), 路由组里 "cline-模型名" 前缀
CHANNEL_ID = "cline"

CLINE_REFRESH_PATH = "/api/v1/auth/refresh"
# 官方 SDK 伪装头(照抄 cline-product.ts 的 clientHeaders)
CLINE_CLIENT_HEADERS = {
    "HTTP-Referer": "https://cline.bot",
    "X-Title": "Cline",
    "X-IS-MULTIROOT": "false",
    "X-CLIENT-TYPE": "cline-sdk",
}


def chat_headers(access_token: str) -> dict:
    """推理请求附加头(伪装 Cline 官方 SDK, 与插件 cline-product.ts 一致)。"""
    return {
        "Authorization": f"Bearer {access_token}",
        "HTTP-Referer": "https://cline.bot",
        "X-Title": "Cline",
        "X-CLIENT-TYPE": "cline-sdk",
    }


async def start_device_login(client: httpx.AsyncClient) -> dict:
    """发起设备码登录, 返回给用户看的一步。

    返回 {"device_code", "user_code", "verification_uri",
          "verification_uri_complete", "expires_in", "interval"}
    """
    resp = await client.post(
        f"{WORKOS_BASE}/user_management/authorize/device",
        json={"client_id": WORKOS_CLIENT_ID},
    )
    resp.raise_for_status()
    return resp.json()


async def poll_device_token(
    client: httpx.AsyncClient,
    device_code: str,
    interval: int = POLL_INTERVAL_SECONDS,
    timeout: int = POLL_TIMEOUT_SECONDS,
) -> dict | None:
    """轮询 WorkOS 直到用户确认; 返回 WorkOS token 响应或 None(超时)。

    成功响应含 access_token / refresh_token / expires_in 等。
    authorization_pending / slow_down 之外的错误向上抛(浏览器里拒绝授权等)。
    """
    deadline = time.monotonic() + timeout
    wait = interval
    while time.monotonic() < deadline:
        resp = await client.post(
            f"{WORKOS_BASE}/user_management/authenticate",
            json={
                "client_id": WORKOS_CLIENT_ID,
                "device_code": device_code,
                "grant_type": DEVICE_CODE_GRANT,
            },
        )
        if resp.status_code == 200:
            return resp.json()
        try:
            err = resp.json().get("error", "")
        except Exception:
            err = ""
        if err == "authorization_pending":
            time.sleep(wait)
            continue
        if err == "slow_down":
            wait += 5
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return None
    return None


async def register_cline_token(client: httpx.AsyncClient, workos_token: dict) -> dict:
    """WorkOS token → Cline token(存这个, 前缀 workos:)。"""
    resp = await client.post(
        f"{CLINE_API_BASE}/api/v1/auth/register",
        json={
            "accessToken": workos_token.get("access_token", ""),
            "refreshToken": workos_token.get("refresh_token", ""),
            "workOSUserId": workos_token.get("user_id",
                                             workos_token.get("workspace_id", "")),
        },
    )
    resp.raise_for_status()
    data = resp.json()
    # 响应形状按插件兼容: 直接回 token 字符串或 {token,...} 包装
    if isinstance(data, dict) and "token" not in data:
        for key in ("apiKey", "access_token", "accessToken"):
            if data.get(key):
                data["token"] = data[key]
                break
    return data


async def refresh_cline_token(client: httpx.AsyncClient, refresh_token: str) -> dict | None:
    """刷新 cline token; 失败(过期/撤销)返回 None。

    ⚠ 请求体字段名是驼峰 `refreshToken` **且必须带** `grantType:"refresh_token"`
    (照抄插件 `clineRefreshBody`)。只发 refreshToken 会被 400 拒绝。
    """
    try:
        resp = await client.post(
            f"{CLINE_API_BASE}{CLINE_REFRESH_PATH}",
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     **CLINE_CLIENT_HEADERS},
            json={"refreshToken": refresh_token, "grantType": "refresh_token"},
        )
        if resp.status_code in (401, 403):
            return None
        if resp.status_code != 200:
            return None
        return resp.json()
    except httpx.HTTPError:
        return None


async def fetch_free_models(client: httpx.AsyncClient, access_token: str) -> list[str]:
    """拉模型清单并筛免费档(cline-free/ 前缀与 :free 后缀)。"""
    headers = chat_headers(access_token)
    models: list[str] = []
    for path in ("/api/v1/models", "/api/v1/ai/cline/recommended-models"):
        try:
            resp = await client.get(f"{CLINE_API_BASE}{path}", headers=headers)
            if resp.status_code != 200:
                continue
            data = resp.json()
            items = data.get("data") if isinstance(data, dict) else data
            if not isinstance(items, list):
                continue
            for item in items:
                mid = item.get("id") if isinstance(item, dict) else None
                if not mid:
                    continue
                if mid.startswith("cline-free/") or mid.endswith(":free"):
                    models.append(mid)
        except httpx.HTTPError:
            continue
    return list(dict.fromkeys(models))


def credentials_package(cline_token: dict) -> str:
    """Cline token 响应 → JSON 凭据包字符串(存 Provider.api_key_encrypted)。

    包结构: {"access_token", "refresh_token", "channel": "cline"}
    与 encrypt_secret/decrypt_secret 现有链路零改动。
    """
    import json
    # 兼容三种入参: register 响应({token/apiKey})、刷新结果(accessToken)、
    # 已解析凭据包(access_token) —— 刷新回写走的正是第三种。
    token = (cline_token.get("token") or cline_token.get("apiKey")
             or cline_token.get("accessToken") or cline_token.get("access_token") or "")
    refresh = (cline_token.get("refreshToken") or cline_token.get("refresh_token") or "")
    if token and not str(token).startswith("workos:"):
        token = f"workos:{token}"
    pkg = {
        "channel": CHANNEL_ID,
        "access_token": token,
        "refresh_token": refresh,
    }
    # 落库过期时间: 保活循环据此判断是否该刷新(缺了会每轮都刷, 白耗一次请求)
    expires = (cline_token.get("expires_at") or cline_token.get("expiresAt")
               or cline_token.get("expire_time"))
    if expires:
        pkg["expires_at"] = expires
    return json.dumps(pkg, ensure_ascii=False)


def _unwrap_refresh(data: dict) -> dict:
    """刷新响应拆壳 + 驼峰转凭据字段。

    实测 `POST /api/v1/auth/refresh` 回 `{"success":true,"data":{accessToken,
    refreshToken,expiresAt,tokenType,userInfo}}` —— 字段是驼峰且在 data 里,
    与登录(register)返回的形状不同, 不能共用一套解析。
    """
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    token = inner.get("accessToken") or inner.get("access_token") or ""
    refresh = inner.get("refreshToken") or inner.get("refresh_token") or ""
    expires = inner.get("expiresAt") or inner.get("expires_at") or ""
    if token and not str(token).startswith("workos:"):
        token = f"workos:{token}"
    return {"access_token": token, "refresh_token": refresh,
            "expires_at": expires}


def credential_expires_at_ms(creds: dict) -> int | None:
    """凭据过期时间(毫秒); 无/解析不了返回 None。

    插件存 `expire_time`(ms) 或 `expires_at`(ISO)。ISO 缺时区按 UTC 处理。
    """
    from datetime import datetime, timezone
    raw = creds.get("expire_time") or creds.get("expires_at")
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        value = int(raw)
        # 秒级(10 位)统一到毫秒
        return value * 1000 if value < 10_000_000_000 else value
    text = str(raw).strip()
    if text.isdigit():
        value = int(text)
        return value * 1000 if value < 10_000_000_000 else value
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


async def refresh_credential(client: httpx.AsyncClient, creds: dict) -> dict | None:
    """凭据刷新(过期/401 时调用); 失败返回 None(需重登)。"""
    refresh = creds.get("refresh_token") or ""
    if not refresh:
        return None
    raw = await refresh_cline_token(client, refresh)
    if not raw:
        return None
    fresh = _unwrap_refresh(raw)
    if not fresh["access_token"]:
        return None
    # 刷新响应不回新 refresh_token 时保留旧的
    if not fresh["refresh_token"]:
        fresh["refresh_token"] = refresh
    fresh["channel"] = CHANNEL_ID
    return fresh
