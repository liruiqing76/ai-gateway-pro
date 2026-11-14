# ============================================================
# 智能网关 — 白嫖渠道每日签到调度
#
# 参考 dsh-our-free-model 的 auto-checkin.ts:
#   - UTC+8 日期防重(同一天不重复领)
#   - 状态落盘(JSON 文件, 重启不丢)
#   - 逐渠道独立结果(claimed / already-claimed / inactive / failed / 出错)
#   - 启动延迟 + 每日定点循环
#
# 与插件的差异(适配本项目):
#   - 插件靠宿主加密凭据库, 这里从 Provider.api_key_encrypted 现解
#   - 插件签到在 channel 内存循环; 这里按用户逐个跑, 每用户只看自己的渠道
#   - 凭据过期时先尝试刷新(workbuddy/codearts 可续期), 刷新失败记 failed
# ============================================================

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

from backend.config import settings
from backend.models import get_session, Provider
from backend.core.channel_auth import (
    checkin_channels, channel_checkin, parse_credentials,
)
from backend.core.channel_auth import codearts, workbuddy, minimax

logger = logging.getLogger("gateway.checkin")

UTC8 = timezone(timedelta(hours=8))
STATE_FILENAME = "channel_checkin_state.json"


def _state_dir() -> Path:
    """状态文件目录: 与真实数据库同级(见 state_paths 的优先级说明)。"""
    from backend.core.state_paths import state_dir
    return state_dir()


def _state_path() -> Path:
    return _state_dir() / STATE_FILENAME


def utc8_date_str(now_ms: float | None = None) -> str:
    """UTC+8 的自然日字符串(签到防重单位, 照抄插件 utc8DateString)。"""
    ts = (now_ms / 1000) if now_ms is not None else time.time()
    return datetime.fromtimestamp(ts, UTC8).strftime("%Y-%m-%d")


def load_state() -> dict:
    try:
        p = _state_path()
        if not p.exists():
            return {}
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception as e:
        logger.warning("签到状态读取失败, 按空处理: %s", e)
        return {}


def save_state(state: dict) -> None:
    try:
        p = _state_path()
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(p)
    except Exception as e:
        logger.warning("签到状态写入失败: %s", e)


_STATE_KINDS_DONE = ("claimed", "already-claimed")


def record_state(user_id: int, channel: str, result: dict) -> None:
    """把单次签到结果落到状态文件(供前端"今日已领/失败原因"展示)。

    手动单渠道签到也要落盘, 否则界面上点完签到状态不更新(旧实现只有
    全量调度写状态)。失败原因一并保存, 前端才能显示"为什么没领到"。
    """
    state = load_state()
    state[f"{user_id}:{channel}"] = {
        "date": utc8_date_str(),
        "kind": result.get("kind"),
        "credit": result.get("credit") or 0,
        "reason": (result.get("message") or "")[:200],
        "streak_days": result.get("streak_days"),
        "at": int(time.time()),
    }
    save_state(state)


def state_entry_done(user_id: int, channel: str, today: str | None = None) -> bool:
    """今天这个用户这个渠道是否已经领过了(claimed/already-claimed)。"""
    today = today or utc8_date_str()
    entry = load_state().get(f"{user_id}:{channel}")
    return (isinstance(entry, dict) and entry.get("date") == today
            and entry.get("kind") in _STATE_KINDS_DONE)


# ============================================================
# 凭据刷新(签到前先保证凭据活着)
# ============================================================

async def _ensure_fresh_credentials(client: httpx.AsyncClient,
                                    channel: str, creds: dict,
                                    force: bool = False) -> dict | None:
    """凭据临期/过期时刷新; 返回可用凭据或 None(刷不动=需重登录)。

    force=True 时跳过本地过期判定, 无条件刷新一次 —— 用于上游已经明确
    拒绝该 token(但本地时间还没到)的自愈路径。

    临期阈值=CRED_REFRESH_AHEAD_MINUTES(默认 120 分钟): 保活循环(30 分钟
    一轮)在 token 剩余寿命不足提前量时就刷新, 让 access_token 永远"年轻"。
    旧版只在剩 60s 才刷, 短寿命 token(trae 几小时)在两轮保活之间就已过期,
    全靠请求路径自愈抢救 —— 用户体感就是"隔天就要重新登录"。

    - codearts: expires_at(ISO) 临期 → refresh_token 换新 AK/SK
    - workbuddy/buddy: credential_expires_at_ms 临期 → refresh
    - trae: 同款过期判定 → ExchangeToken(refresh_token) 重换
    - qoder/qodercn: expire_time(ms)/JWT exp → deviceToken/refresh
    - cline: WorkOS JWT 1h 寿命, 靠 expires_at 判定
    - minimax: 一次性轮换 refresh_token, 刷新后必须回写
    """
    from backend.core.channel_auth import (
        trae as trae_mod, qoder as qoder_mod, lobsterai as lobsterai_mod)
    now_ms = int(time.time() * 1000)
    SKEW = max(60_000, settings.CRED_REFRESH_AHEAD_MINUTES * 60_000)

    if channel == codearts.CHANNEL_ID:
        exp = creds.get("expires_at", "")
        expired = True
        if exp:
            try:
                dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                expired = dt.timestamp() * 1000 <= now_ms + SKEW
            except ValueError:
                expired = False
        if not force and not expired:
            return creds
        if not creds.get("refresh_token"):
            return None
        keypair = {"private_key_jwk": creds["dpop_private_key_jwk"]}
        try:
            new = await codearts.exchange_refresh_token(
                client, creds["refresh_token"],
                creds.get("code_verifier", ""), keypair)
            return new
        except Exception as e:
            logger.warning("codearts 凭据刷新失败: %s", e)
            return None

    if channel in (workbuddy.CHANNEL_ID, "buddy"):
        exp_ms = workbuddy.credential_expires_at_ms(creds)
        if not force and (exp_ms is None or exp_ms > now_ms + SKEW):
            return creds
        product = workbuddy.product_for(channel)
        new = await workbuddy.refresh_token(client, creds, product)
        if new is None:
            logger.warning("%s 凭据已过期且刷新失败, 需重登录", channel)
            return None
        return new

    if channel == trae_mod.CHANNEL_ID:
        from backend.core.channel_auth.trae import trae_credential_expired
        if not force and not trae_credential_expired(creds, SKEW):
            return creds
        if not creds.get("refresh_token"):
            return None
        try:
            new = await trae_mod.refresh_credential(client, creds)
            return new
        except Exception as e:
            logger.warning("trae 凭据刷新失败: %s", e)
            return None

    if channel in (qoder_mod.CHANNEL_ID_CN, qoder_mod.CHANNEL_ID_INTL):
        if not force and not qoder_mod.credential_expired(creds, SKEW):
            return creds
        product = qoder_mod.product_for(channel)
        return await qoder_mod.refresh_credential(client, creds, product)

    if channel == "cline":
        # cline 的 WorkOS JWT 只有 ~1h 寿命, 必须靠刷新保活(实测过期后
        # 上游回 401 "Please re-authenticate")。刷新体要 refreshToken + grantType。
        from backend.core.channel_auth import cline as cline_mod
        exp_ms = cline_mod.credential_expires_at_ms(creds)
        if not force and exp_ms is not None and exp_ms > now_ms + SKEW:
            return creds
        fresh = await cline_mod.refresh_credential(client, creds)
        if fresh is None:
            logger.warning("cline 凭据刷新失败, 需重新登录")
            return None
        return fresh

    if channel == minimax.CHANNEL_ID:
        # ⚠️ MiniMax 的 refresh_token 是**一次性轮换**的: 刷新成功即刻作废旧
        # refresh_token, 所以必须回写 Provider(见 _persist_refreshed_credentials),
        # 否则下一次保活必然 invalid_grant, 只能重新走设备码登录。
        if not force and not minimax.is_expired(creds, SKEW):
            return creds
        if not minimax.is_refreshable(creds):
            logger.warning("minimax 凭据已过期且无 refresh_token, 需重新登录")
            return None
        fresh = await minimax.refresh_credential(client, creds)
        if fresh is None:
            logger.warning("minimax 凭据刷新失败, 需重新登录")
            return None
        return fresh

    if channel == lobsterai_mod.CHANNEL_ID:
        # LobsterAI: expiresIn/JWT exp 判定, refresh_token 换新 accessToken
        if not force and not lobsterai_mod.is_expired(creds, SKEW):
            return creds
        if not creds.get("refresh_token"):
            logger.warning("lobsterai 凭据已过期且无 refresh_token, 需重新登录")
            return None
        fresh = await lobsterai_mod.refresh_credential(client, creds)
        if fresh is None:
            logger.warning("lobsterai 凭据刷新失败, 需重新登录")
            return None
        return fresh

    return creds


def package_credentials(channel: str, creds: dict) -> str | None:
    """按渠道把凭据 dict 打包回 api_key 字符串(各渠道字段不同)。

    刷新凭据后必须用它重新打包: 之前 checkin_provider 里对非 codearts 渠道
    一律调 workbuddy.credentials_package, 会把 cline/minimax/qoder 的凭据
    按 WorkBuddy 的字段名重写(access_token 还在但 expires_at/refresh_token
    语义错位), 下次解析必失败 —— 这是刷新成功却仍然"凭据过期"的根因之一。
    """
    from backend.core.channel_auth import (
        trae as trae_mod, qoder as qoder_mod, lobsterai as lobsterai_mod)
    if channel == codearts.CHANNEL_ID:
        return codearts.credentials_package(creds)
    if channel in (workbuddy.CHANNEL_ID, "buddy"):
        return workbuddy.credentials_package(creds, channel)
    if channel == trae_mod.CHANNEL_ID:
        return trae_mod.credentials_package(creds)
    if channel in (qoder_mod.CHANNEL_ID_CN, qoder_mod.CHANNEL_ID_INTL):
        return qoder_mod.credentials_package(creds)
    if channel == "cline":
        from backend.core.channel_auth import cline as cline_mod
        return cline_mod.credentials_package(creds)
    if channel == minimax.CHANNEL_ID:
        return minimax.credentials_package(creds)
    if channel == lobsterai_mod.CHANNEL_ID:
        return lobsterai_mod.credentials_package(creds)
    return None


def _persist_refreshed_credentials(provider_id: int, channel: str,
                                   creds: dict) -> None:
    """刷新后的凭据写回 Provider(否则下次还得刷/已失效)。"""
    from backend.auth.security import encrypt_secret
    plain = package_credentials(channel, creds)
    if plain is None:
        return
    session = get_session()
    try:
        p = session.query(Provider).filter_by(id=provider_id).first()
        if p is not None:
            p.api_key_encrypted = encrypt_secret(plain)
            session.commit()
    except Exception:
        logger.exception("签到凭据回写失败")
        session.rollback()
    finally:
        session.close()


# ============================================================
# 单用户单渠道签到
# ============================================================

async def checkin_provider(
    client: httpx.AsyncClient, user_id: int, provider: Provider,
) -> dict:
    """对一个渠道 Provider 执行签到(含凭据刷新与回写)。"""
    from backend.auth.security import decrypt_secret
    channel = provider.name
    try:
        api_key = decrypt_secret(provider.api_key_encrypted)
    except Exception:
        return {"channel": channel, "kind": "failed", "message": "凭据解密失败"}
    creds = parse_credentials(channel, api_key)
    if creds is None:
        return {"channel": channel, "kind": "failed", "message": "凭据包格式不符"}

    fresh = await _ensure_fresh_credentials(client, channel, creds)
    if fresh is None:
        return {"channel": channel, "kind": "failed",
                "message": "凭据已过期, 请重新登录该渠道"}
    if fresh is not creds:
        _persist_refreshed_credentials(provider.id, channel, fresh)
        # 重新打包成 channel_checkin 需要的 api_key 字符串(按渠道各自字段)
        api_key = package_credentials(channel, fresh) or api_key

    call = channel_checkin(channel, api_key, client)
    if call is None:
        return {"channel": channel, "kind": "unsupported",
                "message": "该渠道无 HTTP 签到接口"}
    try:
        return {"channel": channel, **(await call)}
    except Exception as e:
        return {"channel": channel, "kind": "failed", "message": str(e)}


async def run_checkin_for_all_users() -> dict:
    """全用户签到一轮(调度与手动触发共用)。

    返回 {"date", "results": [...], "totals": {claimed, already, failed, credit}}
    """
    today = utc8_date_str()
    state = load_state()
    all_results: list[dict] = []

    session = get_session()
    try:
        # 已连接且支持签到的渠道 Provider(逐用户收集)
        supported = set(checkin_channels())
        providers = [p for p in session.query(Provider).filter_by(enabled=True).all()
                     if p.name in supported]
    finally:
        session.close()

    from backend.core import upstream_http
    async with upstream_http.make_async(timeout=60) as client:
        for p in providers:
            # 防重: 该用户该渠道今天已领/已失败过则跳过(照插件 lastDate 语义)
            key = f"{p.user_id}:{p.name}"
            entry = state.get(key)
            if isinstance(entry, dict) and entry.get("date") == today \
                    and entry.get("kind") in ("claimed", "already-claimed"):
                all_results.append({"channel": p.name, "user_id": p.user_id,
                                    "kind": "skipped",
                                    "message": "今日已签到"})
                continue
            result = await checkin_provider(client, p.user_id, p)
            result["user_id"] = p.user_id
            all_results.append(result)
            state[key] = {
                "date": today,
                "kind": result.get("kind"),
                "credit": result.get("credit", 0),
                "reason": (result.get("message") or "")[:200],
                "streak_days": result.get("streak_days"),
                "at": int(time.time()),
            }

    save_state(state)
    totals = {
        "claimed": sum(1 for r in all_results if r.get("kind") == "claimed"),
        "already": sum(1 for r in all_results
                       if r.get("kind") in ("already-claimed", "skipped")),
        "failed": sum(1 for r in all_results if r.get("kind") == "failed"),
        "credit": sum(r.get("credit") or 0 for r in all_results),
    }
    logger.info("渠道签到: %s 个账号, 领取 %s, 今日已领 %s, 失败 %s, 积分 %s",
                len(all_results), totals["claimed"], totals["already"],
                totals["failed"], totals["credit"])
    return {"date": today, "results": all_results, "totals": totals}


def checkin_state_summary() -> dict:
    """给 API 用: 今日各渠道签到概览。

    每个渠道额外带上 kind(按成功优先取最"好"的那个) / reason(失败原因) /
    at(时间戳), 前端据此显示"已领 100 积分""今日已领""失败: xxx", 而不是
    只有一个开关。
    """
    today = utc8_date_str()
    state = load_state()
    rank = {"claimed": 5, "already-claimed": 4, "inactive": 3,
            "unsupported": 2, "failed": 1}
    channels: dict[str, dict] = {}
    for key, entry in state.items():
        if not isinstance(entry, dict) or entry.get("date") != today:
            continue
        _, name = key.split(":", 1)
        cur = channels.setdefault(name, {"date": today, "kinds": [],
                                         "credit": 0, "reason": "", "at": 0})
        kind = entry.get("kind")
        cur["kinds"].append(kind)
        cur["credit"] += entry.get("credit") or 0
        if entry.get("at") and entry["at"] > (cur.get("at") or 0):
            cur["at"] = entry["at"]
        if rank.get(kind, 0) > rank.get(cur.get("kind"), 0):
            cur["kind"] = kind
        if kind in _STATE_KINDS_DONE:
            cur["reason"] = ""
        elif entry.get("reason") and not cur.get("reason") \
                and kind == cur.get("kind"):
            cur["reason"] = entry["reason"]
    return {"today": today, "channels": channels}


# ============================================================
# 凭据保活(参考插件 refreshAllCredentials: 每 30 分钟批量续期)
# ============================================================

async def refresh_provider_credentials(provider_id: int, channel: str,
                                       api_key: str) -> str | None:
    """**强制**刷新某渠道凭据(不看本地过期时间)并回写, 返回新的 api_key。

    场景: 上游说 token 无效(401/403/APIG.0602), 但本地记录的过期时间还没到 ——
    服务端提前吊销、或 refresh_token 已被别处轮换。这时靠"临期才刷"的保活循环
    永远救不回来, 必须在推理路径上现场刷一次再重试。
    返回 None 表示该渠道不可刷新或刷新失败(调用方应提示重新登录)。
    """
    from backend.auth.security import decrypt_secret
    creds = parse_credentials(channel, api_key)
    if creds is None or not creds.get("refresh_token"):
        return None
    from backend.core import upstream_http
    try:
        async with upstream_http.make_async(timeout=60) as client:
            fresh = await _ensure_fresh_credentials(
                client, channel, {**creds, "expires_at": "", "expire_time": 0},
                force=True)
    except Exception as e:
        logger.warning("渠道 %s 强制刷新异常: %s", channel, e)
        return None
    if fresh is None:
        return None
    _persist_refreshed_credentials(provider_id, channel, fresh)
    plain = package_credentials(channel, fresh)
    if plain is None:
        return None
    logger.info("渠道 %s(id=%s) 凭据已现场刷新", channel, provider_id)
    return plain


async def refresh_all_expiring() -> dict:
    """扫描所有白嫖渠道 Provider, 凭据临期(60s 偏差)即刷新并回写。

    推理路径是同步读 api_key, 不能在线等刷新; 所以靠本函数在后台
    把凭据保持在有效期内(与插件的 refresh 循环同构)。
    """
    from backend.core.channel_auth import is_channel_provider
    stats = {"checked": 0, "refreshed": 0, "dead": 0}
    session = get_session()
    try:
        providers = [p for p in session.query(Provider).filter_by(enabled=True).all()
                     if is_channel_provider(p.name)]
    finally:
        session.close()

    from backend.core import upstream_http
    async with upstream_http.make_async(timeout=60) as client:
        # 客户端版本号预热(LobsterAI 头里必须带; 缓存 12h, 进程重启后靠这轮补)
        try:
            from backend.core.channel_auth import lobsterai as lobsterai_mod
            await lobsterai_mod.resolve_client_version(client)
        except Exception:
            pass
        for p in providers:
            from backend.auth.security import decrypt_secret
            try:
                api_key = decrypt_secret(p.api_key_encrypted)
            except Exception:
                continue
            creds = parse_credentials(p.name, api_key)
            if creds is None:
                continue
            stats["checked"] += 1
            fresh = await _ensure_fresh_credentials(client, p.name, creds)
            if fresh is None:
                stats["dead"] += 1
                logger.warning("渠道 %s(id=%s) 凭据过期且刷新失败, 需重新登录",
                               p.name, p.id)
            elif fresh is not creds:
                _persist_refreshed_credentials(p.id, p.name, fresh)
                stats["refreshed"] += 1
    if stats["refreshed"] or stats["dead"]:
        logger.info("渠道凭据保活: 检查%d 刷新%d 失效%d",
                    stats["checked"], stats["refreshed"], stats["dead"])
    return stats
