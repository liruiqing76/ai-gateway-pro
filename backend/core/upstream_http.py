# ============================================================
# 上游出站 HTTP 客户端工厂
#
# 背景(2026-10-24 本机实测): Windows 系统代理(如 127.0.0.1:7892 Clash)
# 半挂时, httpx 默认 trust_env=True 会走它 → 所有上游调用 EOF/000;
# 而显式 transport 或 trust_env=False 直连则部分域名正常。
# 反过来, 国内直连不可达的海外域名(opencode.ai/github)又需要代理。
#
# 所以出站路径必须可配, 由 GW_UPSTREAM_PROXY 决定:
#   ""(默认)   跟随系统/环境变量代理(trust_env=True, httpx 原生行为)
#   "none"     强制直连(忽略系统代理)
#   "http://127.0.0.1:7892" 等  显式指定代理
# ============================================================

from __future__ import annotations

import httpx

from backend.config import settings


def _mode() -> tuple[str, str]:
    raw = (getattr(settings, "UPSTREAM_PROXY", "") or "").strip()
    low = raw.lower()
    if low in ("none", "direct", "off"):
        return "direct", ""
    if raw:
        return "proxy", raw
    return "system", ""


def make_async(**kwargs) -> httpx.AsyncClient:
    """按 GW_UPSTREAM_PROXY 构造异步出站 client(其余 kwargs 透传 httpx)。"""
    mode, proxy = _mode()
    if mode == "system":
        return httpx.AsyncClient(**kwargs)
    if mode == "direct":
        kwargs.setdefault("transport", httpx.AsyncHTTPTransport(http2=False))
        kwargs["trust_env"] = False
        return httpx.AsyncClient(**kwargs)
    kwargs["proxy"] = proxy
    kwargs["trust_env"] = False
    return httpx.AsyncClient(**kwargs)


def make_sync(**kwargs) -> httpx.Client:
    """按 GW_UPSTREAM_PROXY 构造同步出站 client。"""
    mode, proxy = _mode()
    if mode == "system":
        return httpx.Client(**kwargs)
    if mode == "direct":
        kwargs.setdefault("transport", httpx.HTTPTransport(http2=False))
        kwargs["trust_env"] = False
        return httpx.Client(**kwargs)
    kwargs["proxy"] = proxy
    kwargs["trust_env"] = False
    return httpx.Client(**kwargs)
