# ============================================================
# 白嫖渠道登录器注册表
#
# 每渠道一个模块, 对外统一契约(参考 dsh-our-free-model pack.js 的
# registerXxxLlm 模式, 适配本网关 Provider 模型):
#
#   CHANNEL_ID          渠道标识(= Provider.name)
#   chat_headers(...)   简单头渠道(Bearer + 附加头) → 头 dict
#   sign_request(...)   签名渠道(CodeArts 华为云 HMAC) → 头 dict
#   credentials_package / parse_credentials_package  凭据包打包/解包
#   fetch_models        模型清单(登录后调用, 写 Provider.models)
#   claim_daily_checkin 每日一键签到(有 HTTP 签到接口的渠道)
#
# 转发侧(core/proxy.py forward_*)按 CHANNEL_ID 查本注册表:
#   - HEADERS 渠道: 静态附加头(含 Bearer)
#   - SIGNER 渠道: 对(method, url, body)现算签名头(华为云 HMAC)
#   未知渠道返回空 → 现有纯 Bearer 行为不变
#
# 可行性分级与路线图见 docs/07-白嫖渠道收编路线图.md
# ============================================================

from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable

from . import cline
from . import codearts
from . import workbuddy
from . import trae
from . import qoder
from . import minimax
from . import lobsterai
from . import channel_transform

logger = logging.getLogger("gateway.channel_auth")

# 加密/自定义协议渠道的转发超时(Qoder 排队重试最长 30 分钟, 插件 L38533)
CHANNEL_TIMEOUT_HINTS: dict[str, float] = {
    "qoder": 1900, "qodercn": 1900,
    "trae": 300,
    "minimax": 300,
    "lobsterai": 300,
}


def is_transformed_channel(provider_name: str) -> bool:
    """该渠道走自定义协议整次转发(TRAE SOLO / Qoder 加密信封), 不经通用头路径。"""
    return channel_transform.is_transformed_channel(provider_name)


def channel_build_request(provider_name, api_key, model, req_body):
    """变换渠道: OpenAI body → (url, headers, body_bytes)。"""
    return channel_transform.build_infer_request(provider_name, api_key, model, req_body)


def channel_line_converter(provider_name, model):
    """变换渠道: 响应 SSE 行 → 标准 OpenAI chunk 行。"""
    return channel_transform.make_line_converter(provider_name, model)


def channel_non_stream(provider_name, api_key, model, req_body, client, timeout):
    """变换渠道的非流式聚合(消费渠道流 → chat.completion dict)。"""
    return channel_transform.transformed_non_stream(
        provider_name, api_key, model, req_body, client, timeout)

# ---- 简单头渠道: (凭据包 dict, model) → 头 dict ----
_HEADER_BUILDERS: dict[str, Callable[[dict, str], dict]] = {
    cline.CHANNEL_ID: lambda creds, model: cline.chat_headers(creds.get("access_token", "")),
    workbuddy.CHANNEL_ID: lambda creds, model: workbuddy.chat_headers(creds, workbuddy.CHANNEL_ID, model),
    "buddy": lambda creds, model: workbuddy.chat_headers(creds, "buddy", model),
    lobsterai.CHANNEL_ID: lambda creds, model: lobsterai.chat_headers(creds, model),
}

# ---- 签名渠道: (凭据包 dict, method, url, body bytes) → 头 dict ----
_SIGNER_BUILDERS: dict[str, Callable[[dict, str, str, bytes], dict]] = {
    codearts.CHANNEL_ID: codearts.sign_request_for_chat,
}

# ---- 凭据包解析器: (api_key str) → 凭据 dict | None ----
_CREDENTIAL_PARSERS: dict[str, Callable[[str], dict | None]] = {
    cline.CHANNEL_ID: lambda key: _parse_pkg(key, cline.CHANNEL_ID),
    workbuddy.CHANNEL_ID: lambda key: workbuddy.parse_credentials_package(key, workbuddy.CHANNEL_ID),
    "buddy": lambda key: workbuddy.parse_credentials_package(key, "buddy"),
    codearts.CHANNEL_ID: codearts.parse_credentials_package,
    trae.CHANNEL_ID: trae.parse_credentials_package,
    qoder.CHANNEL_ID_CN: lambda key: qoder.parse_credentials_package(key, qoder.CHANNEL_ID_CN),
    qoder.CHANNEL_ID_INTL: lambda key: qoder.parse_credentials_package(key, qoder.CHANNEL_ID_INTL),
    minimax.CHANNEL_ID: minimax.parse_credentials_package,
    lobsterai.CHANNEL_ID: lobsterai.parse_credentials_package,
}

# ---- 签到渠道: (httpx.AsyncClient, 凭据 dict) → {kind, credit?, message?} ----
# WorkBuddy / CodeBuddy 同属腾讯 buddy 系, 共用 /v2/billing/meter/* 签到端点
# (插件 credits.ts 里按 product 参数化), 两家都要接。
_CHECKIN_BUILDERS: dict[str, Callable[..., Awaitable[dict]]] = {
    codearts.CHANNEL_ID: codearts.claim_daily_checkin,
    trae.CHANNEL_ID: trae.claim_daily_checkin,
    qoder.CHANNEL_ID_CN: lambda client, creds: qoder.claim_daily_checkin(
        client, creds, qoder.CHANNEL_ID_CN),
    qoder.CHANNEL_ID_INTL: lambda client, creds: qoder.claim_daily_checkin(
        client, creds, qoder.CHANNEL_ID_INTL),
    workbuddy.CHANNEL_ID: lambda client, creds: workbuddy.claim_daily_checkin(
        client, creds, workbuddy.CHANNEL_ID),
    "buddy": lambda client, creds: workbuddy.claim_daily_checkin(
        client, creds, "buddy"),
    minimax.CHANNEL_ID: minimax.claim_daily_checkin,
    lobsterai.CHANNEL_ID: lobsterai.claim_daily_checkin,
}


def _parse_pkg(api_key: str, channel_id: str) -> dict | None:
    try:
        pkg = json.loads(api_key)
        if isinstance(pkg, dict) and pkg.get("channel") == channel_id:
            return pkg
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    return None


def parse_credentials(provider_name: str, api_key: str) -> dict | None:
    """渠道名 + Provider.api_key → 凭据包 dict(非本渠道/解析失败 → None)。"""
    parser = _CREDENTIAL_PARSERS.get(provider_name)
    if parser is None:
        return None
    return parser(api_key)


def channel_headers(provider_name: str, api_key: str, model: str = "") -> dict:
    """简单头渠道的推理附加头(含 Bearer)。非渠道返回空 dict。"""
    builder = _HEADER_BUILDERS.get(provider_name)
    if builder is None:
        return {}
    creds = parse_credentials(provider_name, api_key)
    if creds is None:
        return {}
    try:
        return builder(creds, model)
    except Exception:
        return {}


def channel_signed_headers(
    provider_name: str, api_key: str, method: str, url: str, body: bytes,
) -> dict:
    """签名渠道的推理头(对本次请求现算, 含华为云 HMAC Authorization)。

    非签名渠道返回空 dict(调用方回退到 channel_headers / 纯 Bearer)。
    """
    signer = _SIGNER_BUILDERS.get(provider_name)
    if signer is None:
        return {}
    creds = parse_credentials(provider_name, api_key)
    if creds is None:
        return {}
    try:
        return signer(creds, method, url, body)
    except Exception:
        return {}


def channel_checkin(
    provider_name: str, api_key: str, client,
) -> Awaitable[dict] | None:
    """签到可调用对象(无 HTTP 签到接口的渠道返回 None)。"""
    builder = _CHECKIN_BUILDERS.get(provider_name)
    if builder is None:
        return None
    creds = parse_credentials(provider_name, api_key)
    if creds is None:
        return None
    return builder(client, creds)


def is_channel_provider(provider_name: str) -> bool:
    """是否白嫖渠道(用于 UI 标记与模板过滤)。"""
    return (provider_name in _HEADER_BUILDERS
            or provider_name in _SIGNER_BUILDERS
            or channel_transform.is_transformed_channel(provider_name))


def checkin_channels() -> list[str]:
    """支持 HTTP 一键签到的渠道列表。"""
    return sorted(_CHECKIN_BUILDERS.keys())
