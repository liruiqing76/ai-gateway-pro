# ============================================================
# 智能网关 — 从 DSH 插件凭据库导入已登录渠道账号
#
# 背景: dsh-our-free-model 插件把各渠道账号存在
#   ~/.dsh/.credentials.yaml → refs.<CHANNEL>_ACCOUNT_<ID> = <JSON 字符串>
# 这些 JSON 就是完整的凭据包(access_token/refresh_token/expire...), 明文可读。
# 所以"插件里已经登录过的账号"可以直接导入网关, 不需要用户再走一次浏览器授权。
#
# 本模块只做两件事:
#   1. collect_accounts()  — 扫凭据库, 按渠道把插件字段映射成网关凭据包
#   2. fetch_channel_models() — 用该凭据去上游拉真实模型清单
# 建 Provider / 路由组由 api/channels.py 复用既有 _upsert_channel_provider 完成
# (避免本模块反向依赖 API 层)。
# ============================================================

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx

from backend.core.channel_auth import (
    cline, codearts, workbuddy,
    trae as trae_mod, qoder as qoder_mod,
    minimax as minimax_mod,
    lobsterai as lobsterai_mod,
)

logger = logging.getLogger("gateway.channel_import")

# 插件 refs 名前缀 → 网关渠道 id
_PREFIX_TO_CHANNEL = {
    "TRAE_ACCOUNT_": "trae",
    "QODERCN_ACCOUNT_": "qodercn",
    "QODER_ACCOUNT_": "qoder",
    "BUDDY_ACCOUNT_": "buddy",
    "WORKBUDDY_ACCOUNT_": "workbuddy",
    "CLINE_ACCOUNT_": "cline",
    "CODEARTS_ACCOUNT_": "codearts",
    "MINIMAX_ACCOUNT_": "minimax",
    "LOBSTERAI_ACCOUNT_": "lobsterai",
}

# 网关渠道 id → Provider.base_url(与 api/channels.py 的 _CHANNEL_BASE_URLS 同源)
CHANNEL_BASE_URLS = {
    "codearts": codearts.CHAT_API_BASE,
    "workbuddy": workbuddy.WORKBUDDY["endpoint"] + "/v2",
    "buddy": workbuddy.CODEBUDDY["endpoint"] + "/v2",
    "cline": cline.CLINE_API_BASE + "/api/v1",
    "trae": "https://trae-api-cn.mchost.guru/api/agent/v3",
    "qoder": "https://api2.qoder.sh/algo",
    "qodercn": "https://gateway.qoder.com.cn/algo",
    "minimax": minimax_mod.API_HOST + "/mavis/api/v1/llm/v1",
    "lobsterai": lobsterai_mod.API_BASE + "/api/proxy/v1",
}


def credentials_path() -> Path:
    """插件凭据库路径(可用 GW_DSH_CREDENTIALS 覆盖)。"""
    env = os.environ.get("GW_DSH_CREDENTIALS")
    if env:
        return Path(env)
    return Path(os.path.expanduser("~")) / ".dsh" / ".credentials.yaml"


def _load_refs(path: Path | None = None) -> dict:
    """读凭据库 refs 段(缺 yaml/文件都返回空, 不抛)。"""
    p = path or credentials_path()
    if not p.exists():
        return {}
    try:
        import yaml
    except ImportError:
        logger.warning("未安装 PyYAML, 无法读取插件凭据库")
        return {}
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("凭据库解析失败: %s", p, exc_info=True)
        return {}
    refs = doc.get("refs")
    return refs if isinstance(refs, dict) else {}


def _json_or_none(raw) -> dict | None:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


# ------------------------------------------------------------
# 各渠道: 插件账号 JSON → 网关凭据包(字符串)
# ------------------------------------------------------------

def _to_trae(a: dict) -> str:
    return trae_mod.credentials_package({
        "access_token": a.get("access_token", ""),
        "refresh_token": a.get("refresh_token", ""),
        "expires_at": a.get("expires_at", ""),
        "uid": str(a.get("uid", "")),
        "nickname": a.get("nickname", ""),
        "machine_id": a.get("machine_id", ""),
        "device_id": a.get("device_id", ""),
        "enterprise_id": a.get("enterprise_id", ""),
    })


def _to_qoder(a: dict, channel: str) -> str:
    pkg = {
        "channel": channel,
        "security_oauth_token": a.get("security_oauth_token", ""),
        "access_token": a.get("access_token", ""),
        "refresh_token": a.get("refresh_token", ""),
        "expire_time": a.get("expire_time", 0),
        "refresh_token_expire_time": a.get("refresh_token_expire_time", 0),
        "machine_id": a.get("machine_id", ""),
        "uid": a.get("uid", ""),
        "nickname": a.get("nickname", ""),
    }
    return qoder_mod.credentials_package(pkg)


def _to_buddy(a: dict, channel: str) -> str:
    """WorkBuddy / CodeBuddy: token 字段 + 账号字段(uid 供签到头)。"""
    return workbuddy.credentials_package(
        {
            "access_token": a.get("access_token", ""),
            "refresh_token": a.get("refresh_token", ""),
            "expires_at": a.get("expires_at", ""),
            "refresh_expires_at": a.get("refresh_expires_at", ""),
            "domain": a.get("domain", ""),
            "user_id": a.get("user_id", ""),
        },
        channel,
        {
            "uid": a.get("user_id", ""),
            "nickname": a.get("nickname", ""),
            "enterprise_id": a.get("enterprise_id", ""),
        },
    )


def _to_cline(a: dict) -> str:
    return cline.credentials_package({
        "token": a.get("access_token", ""),
        "refreshToken": a.get("refresh_token", ""),
    })


def _to_codearts(a: dict) -> str:
    return codearts.credentials_package({
        "access_key_id": a.get("access_key_id", ""),
        "secret_access_key": a.get("secret_access_key", ""),
        "security_token": a.get("security_token", ""),
        "expires_at": a.get("expires_at", ""),
        "refresh_token": a.get("refresh_token", ""),
        "code_verifier": a.get("code_verifier", ""),
        "dpop_private_key_jwk": a.get("dpop_private_key_jwk"),
    })


def _to_minimax(a: dict) -> str:
    """MiniMax: access/refresh + 过期时间(插件存 ms 时间戳字符串)。"""
    return minimax_mod.credentials_package({
        "access_token": a.get("access_token", ""),
        "refresh_token": a.get("refresh_token", ""),
        "token_type": a.get("token_type", "Bearer"),
        "expires_at": str(a.get("expires_at", "") or ""),
        "scope": a.get("scope", ""),
        "account_id": a.get("account_id", ""),
    })


def _to_lobsterai(a: dict) -> str:
    """LobsterAI: 插件凭据包字段与网关一致, 直传(缺字段给空串)。"""
    return lobsterai_mod.credentials_package({
        "access_token": a.get("access_token", ""),
        "refresh_token": a.get("refresh_token", ""),
        "expires_at": str(a.get("expires_at", "") or ""),
        "uid": str(a.get("uid", "") or ""),
        "user_id": str(a.get("user_id", "") or ""),
        "nickname": a.get("nickname", ""),
        "uuid": a.get("uuid", ""),
        "first_keyfrom": str(a.get("first_keyfrom", "") or ""),
        "latest_keyfrom": str(a.get("latest_keyfrom", "") or ""),
    })


def collect_accounts(path: Path | None = None) -> list[dict]:
    """扫插件凭据库 → [{channel, ref, package, nickname, expires_at, error?}]。

    一个渠道可能有多个账号; 网关 Provider 以渠道名为唯一键, 因此每个渠道
    取"过期时间最晚"的那个(其余记在 skipped 里, 不静默丢)。
    """
    refs = _load_refs(path)
    per_channel: dict[str, list[dict]] = {}

    for name, raw in refs.items():
        channel = next((ch for pre, ch in _PREFIX_TO_CHANNEL.items()
                        if name.startswith(pre)), None)
        if not channel:
            continue
        acct = _json_or_none(raw)
        if not acct:
            per_channel.setdefault(channel, []).append(
                {"channel": channel, "ref": name, "error": "凭据不是合法 JSON"})
            continue
        try:
            if channel == "trae":
                pkg = _to_trae(acct)
            elif channel in ("qoder", "qodercn"):
                pkg = _to_qoder(acct, channel)
            elif channel in ("workbuddy", "buddy"):
                pkg = _to_buddy(acct, channel)
            elif channel == "cline":
                pkg = _to_cline(acct)
            elif channel == "codearts":
                pkg = _to_codearts(acct)
            elif channel == "minimax":
                pkg = _to_minimax(acct)
            elif channel == "lobsterai":
                pkg = _to_lobsterai(acct)
            else:
                continue
        except Exception as e:  # 单账号映射失败不影响其它渠道
            per_channel.setdefault(channel, []).append(
                {"channel": channel, "ref": name, "error": f"映射失败: {e}"})
            continue

        exp = (acct.get("expires_at") or acct.get("expire_time") or 0)
        if isinstance(exp, str) and exp.isdigit():
            exp = int(exp)
        if not isinstance(exp, (int, float)):
            exp = 0
        per_channel.setdefault(channel, []).append({
            "channel": channel,
            "ref": name,
            "package": pkg,
            "nickname": acct.get("nickname") or acct.get("email") or "",
            "expires_at": exp,
        })

    picked: list[dict] = []
    for channel, items in per_channel.items():
        good = [i for i in items if i.get("package")]
        if not good:
            picked.extend(items)          # 全是错误项, 原样上报
            continue
        good.sort(key=lambda i: i.get("expires_at") or 0, reverse=True)
        chosen = dict(good[0])
        rest = good[1:]
        if rest:
            chosen["skipped"] = [
                {"ref": r["ref"], "nickname": r.get("nickname", ""),
                 "reason": "同渠道仅导入一个账号(取过期最晚)"} for r in rest]
        picked.append(chosen)
    return picked


# ------------------------------------------------------------
# 拉模型清单(各渠道签名不同, 这里做统一适配)
# ------------------------------------------------------------

async def fetch_channel_models(client: httpx.AsyncClient, channel: str,
                               package: str) -> list[dict]:
    """用导入的凭据拉真实模型清单 → [{id, name}] (失败回空表)。"""
    try:
        if channel == "trae":
            cred = trae_mod.parse_credentials_package(package)
            if not cred:
                return []
            return await trae_mod.fetch_models(client, cred)
        if channel in ("qoder", "qodercn"):
            # 全量模型(带积分倍率), 不再只收 isFree 档
            return qoder_mod.models(channel)
        if channel in ("workbuddy", "buddy"):
            cred = workbuddy.parse_credentials_package(package, channel)
            if not cred:
                return []
            return await workbuddy.fetch_models(client, cred, channel)
        if channel == "cline":
            cred = json.loads(package)
            return await cline.fetch_free_models(
                client, cred.get("access_token", ""))
        if channel == "codearts":
            cred = codearts.parse_credentials_package(package)
            if not cred:
                return []
            return await codearts.fetch_models(client, cred)
        if channel == "minimax":
            cred = minimax_mod.parse_credentials_package(package)
            if not cred:
                return []
            return await minimax_mod.fetch_models(client, cred)
        if channel == "lobsterai":
            cred = lobsterai_mod.parse_credentials_package(package)
            if not cred:
                return []
            return await lobsterai_mod.fetch_models(client, cred)
    except Exception:
        logger.warning("渠道 %s 拉模型失败", channel, exc_info=True)
    return []


def sync_model_rates(user_id: int, channel: str, models: list) -> None:
    """把模型条目里的上游计费倍率同步进 model_rates 表。

    覆盖策略: 只写 source='auto' 的行——用户手填(source='manual')的倍率
    永不被上游刷新覆盖; 上游变价后 auto 行随下次登录/刷新自动跟随。
    """
    from backend.models import ModelRate, get_session

    rates = {
        m["id"]: float(m["rate"]) for m in models
        if isinstance(m, dict) and m.get("id") and isinstance(m.get("rate"), (int, float))
    }
    if not rates:
        return
    session = get_session()
    try:
        existing = {
            r.model: r for r in session.query(ModelRate).filter_by(
                user_id=user_id, provider_name=channel).all()
        }
        for model, rate in rates.items():
            row = existing.get(model)
            if row is None:
                session.add(ModelRate(user_id=user_id, provider_name=channel,
                                      model=model, rate=rate, source="auto"))
            elif (row.source or "manual") == "auto":
                row.rate = rate
        session.commit()
    except Exception:
        session.rollback()
        logger.warning("渠道 %s 模型倍率同步失败", channel, exc_info=True)
    finally:
        session.close()
