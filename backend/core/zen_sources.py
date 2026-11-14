#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenCode Zen 免费模型 — 自动发现 + 可用性探测 + 持续健康检测。

思路(对齐"免费模型一直可用"):
  1. 定时拉取 zen 上游 /v1/models 目录, 提取官方免费档(模型 id 尾部带 -free;
     目录本身区分免费/付费, 无需第三方快照)
  2. 对每个 -free 模型做真实探测(POST zen/v1/chat/completions, max_tokens=1): 200 = 可用
  3. 同步: 为每个激活用户创建/更新 "opencode" 渠道(base_url=zen/v1, 免 key),
     models 只保留"实测可用"的模型
  4. 健康检测: 定时重新探测渠道内模型, 连续失败 N 次自动从 models 移除,
     保证渠道里的模型始终是活的; 瞬时抖动(网络/上游临时故障)不会被立刻踢掉
"""
import concurrent.futures as cf
import logging
import threading
import time

import httpx

from backend.config import settings
from backend.models import get_session, Provider, Router, User

logger = logging.getLogger("gateway")

ZEN_BASE_URL = "https://opencode.ai/zen/v1"
ZEN_CHAT_URL = ZEN_BASE_URL + "/chat/completions"
ZEN_MODELS_URL = ZEN_BASE_URL + "/models"

# opencode 官方免费模型白名单: 模型 id 不带 -free 后缀(如 big-pickle),
# 但匿名探测返回 429 FreeUsageLimitError —— 同属免费档, 只是当日额度用尽。
ZEN_OFFICIAL_FREE_EXTRA = ("big-pickle",)

_lock = threading.Lock()
_runtime = {
    "last_sync_at": None,
    "last_probe_at": None,
    "last_result": None,
    "fail_count": {},   # model -> 连续失败次数
}


# ---------- 数据源 ----------
def fetch_zen_free_models(timeout: float = 25.0) -> list:
    """拉取 zen 上游 /v1/models 目录, 收匿名免费通道可用的模型。

    两类模型都会进入候选:
      - 模型 id 尾部 -free (官方免费档);
      - ZEN_OFFICIAL_FREE_EXTRA 白名单: opencode 官方模型 id 不带 -free,
        但匿名探测返回 429 FreeUsageLimitError, 属免费档(如 big-pickle)。
    付费模型(匿名探测 401 Missing API key)一律不收。失败返回空列表。
    """
    from backend.core import upstream_http
    try:
        with upstream_http.make_sync(timeout=timeout) as client:
            r = client.get(ZEN_MODELS_URL, headers={"User-Agent": "ai-gateway-pro/zen-sync"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("zen 上游模型目录拉取失败: %s", e)
        return []
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    return sorted({
        it.get("id", "").strip() for it in items
        if isinstance(it, dict)
        and it.get("id", "").strip()
        and (it.get("id", "").strip().endswith("-free")
             or it.get("id", "").strip() in ZEN_OFFICIAL_FREE_EXTRA)
    })


def _is_free_quota(why: str) -> bool:
    """探测原因是否为 429 免费额度限制(模型本身可用, 当日额度用尽)。

    probe 失败原因为 "{status_code} {body}", 429 FreeUsageLimitError
    表示匿名免费通道接受该模型, 只是限流 —— 与 401 付费档/403 地区/
    400 下架不同, 不应计入失败。
    """
    return why.startswith("429")


def _is_lane_closed(why: str) -> bool:
    """403 FreeTierError/RegionError: 车道对**当前出口**关闭(身份/地区属性)。

    对齐插件 probe.js 判定: 这不是对"模型"的裁决 —— 换出口/上游放开即恢复,
    模型必须保留在渠道里(插件的 picker 同样不因 unknown 移除模型), 否则
    每次出口抖动都会把清单清空, 恢复后还要等下一轮目录同步。
    """
    return (why.startswith("403")
            and ("FreeTierError" in why or "RegionError" in why
                 or "not available in" in why))


# ---------- 探测 ----------
def probe_zen_model(model_id: str, timeout: float = 25.0) -> tuple:
    """探测单个模型: (可用, 原因)。

    v2 车道(2026-10 起): 免费模型在 /inference/v1/* 应答, 必须带官方
    客户端指纹门(v2 UA + 会话四头 + read/shell 逐字工具定义)。旧 /zen/v1
    chat 路径对免费 id 恒 403 FreeTierError —— 所以探测委托 opencode_free。
    200=可用; 429=免费档当日额度(保留); 其余=不可用。
    """
    from backend.core.channel_auth import opencode_free
    return opencode_free.probe_model_sync(model_id, timeout=timeout)


def probe_many(models: list, max_workers: int = 8, timeout: float = 25.0) -> dict:
    """并发探测, 返回 {model: (ok, reason)}。timeout 为单模型探测超时。"""
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        return dict(zip(models, ex.map(lambda m: probe_zen_model(m, timeout=timeout),
                                       models)))


ZEN_PROVIDER_NAME = "opencode"
ZEN_ROUTER_NAME = "opencode"   # 自动维护的免费档路由组名


def _apply_zen_provider(session, user: User, models: list):
    """为该用户创建/更新 opencode 免 key 渠道。"""
    prov = (session.query(Provider)
            .filter_by(user_id=user.id, base_url=ZEN_BASE_URL).first())
    if prov is None:
        prov = Provider(
            user_id=user.id,
            name=ZEN_PROVIDER_NAME,
            base_url=ZEN_BASE_URL,
            api_key_encrypted="",  # 免 key 匿名渠道
            models=models,
            free_only=True,
            enabled=True,
        )
        session.add(prov)
        logger.info("创建 opencode 免费渠道 user=%s 模型=%d 个", user.username, len(models))
    else:
        prov.models = models
        prov.free_only = True
        prov.enabled = True
    return prov


def _ensure_zen_router(session, user: User, models: list):
    """创建/更新 'opencode' 路由组(模型带渠道前缀), 随可用集自动刷新。"""
    prefixed = [f"{ZEN_PROVIDER_NAME}-{m}" for m in models]
    r = (session.query(Router)
         .filter_by(user_id=user.id, name=ZEN_ROUTER_NAME).first())
    if r is None:
        r = Router(user_id=user.id, name=ZEN_ROUTER_NAME,
                   models=prefixed, strategy="failover")
        session.add(r)
        logger.info("创建 opencode 免费路由组 user=%s 模型=%d 个", user.username, len(prefixed))
    else:
        r.models = prefixed
    return r


# ---------- 同步(发现+探测+落地) ----------
def _known_usable_models() -> set:
    """当前 opencode 渠道里已有的模型(= 已确认可用), 定时同步不再重复探测。

    车道是公共匿名池, 每个模型探测都是一次**真实 chat 调用**, 会消耗本留给用户的
    免费额度。所以定时同步只探"新出现的模型", 老模型直接沿用。
    """
    session = get_session()
    try:
        rows = session.query(Provider).filter_by(base_url=ZEN_BASE_URL).all()
        out = set()
        for p in rows:
            out.update(p.models or [])
        return out
    finally:
        session.close()


def sync_zen_free_models(probe_timeout: float = 25.0, probe_known: bool = False) -> dict:
    """执行一次完整同步: 拉列表 → 真探测(默认只探新增) → 给每个激活用户建/更新渠道。

    probe_known=False(默认, 定时任务): 渠道里已有的模型视为已确认可用, 不再探测
    ——否则每 6h × N 个模型都在白烧免费额度。probe_known=True(用户点「立即同步」
    或调接口显式要求)才全量重探。
    """
    with _lock:
        models = fetch_zen_free_models()
        if not models:
            return {"ok": False, "error": "未能获取 opencode 免费模型列表"}
        known = set() if probe_known else _known_usable_models()
        to_probe = [m for m in models if m not in known]
        probed = probe_many(to_probe, timeout=probe_timeout) if to_probe else {}
        # 判定对齐插件 probe.js: 只有"网关点名这个模型不可用"(400/404/422 或
        # 文案 named)才把模型踢出渠道; 403 FreeTier/Region 是**出口属性**不是
        # 模型裁决(unknown) → 保留, 换出口即恢复; 429 是当日额度 → 保留。
        fresh = [m for m, (ok, why) in probed.items()
                 if ok or _is_free_quota(why) or _is_lane_closed(why)]
        usable = [m for m in models if m in known or m in fresh]
        unusable = {m: why for m, (ok, why) in probed.items() if m not in fresh}
        # 车道状态: 有任何 200 → open; 全 403 → closed(出口被上游关闭)
        any_live = any(ok for ok, _ in probed.values())
        lane_closed = bool(probed) and (not any_live) and all(
            _is_lane_closed(why) for ok, why in probed.values() if not ok)
        if probed:
            lane_state = "open" if any_live else ("closed" if lane_closed else "unknown")
        else:
            # 本轮一个请求都没发(模型全部已知可用): 沿用上次落盘结论
            try:
                from backend.core import lane_state as _ls
                lane_state = (_ls.get(ZEN_PROVIDER_NAME) or {}).get("state") or "open"
            except Exception:
                lane_state = "unknown"

        if not usable:
            # 全灭保护: 疑似上游/网络整体故障, 保留现有渠道不覆盖, 避免模型全被清空
            result = {
                "ok": True,
                "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "total_free": len(models),
                "probed": len(to_probe),
                "usable": [],
                "unusable": unusable,
                "users_updated": 0,
                "skipped": "全部探测失败, 保留现有渠道模型不覆盖",
            }
            _runtime["last_sync_at"] = result["synced_at"]
            _runtime["last_result"] = result
            return result

        session = get_session()
        try:
            users = session.query(User).filter_by(is_active=True).all()
            for u in users:
                _apply_zen_provider(session, u, usable)
                _ensure_zen_router(session, u, usable)
            session.commit()
            updated_users = len(users)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        # 失败计数: 同步成功的模型清零
        _runtime["fail_count"] = {m: 0 for m in usable}
        # 车道状态落盘: 进程重启后到下一轮同步前, 界面仍能显示上次结论
        try:
            from backend.core import lane_state as _ls
            _ls.save("opencode", lane_state, synced_at=time.strftime(
                "%Y-%m-%d %H:%M:%S"), usable=len(usable))
        except Exception:
            pass
        result = {
            "ok": True,
            "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_free": len(models),
            "probed": len(to_probe),    # 本轮真实探测(消耗免费额度)的模型数
            "usable": usable,
            "unusable": unusable,
            "lane_state": lane_state,   # open/closed/unknown(出口属性)
            "users_updated": updated_users,
        }
        _runtime["last_sync_at"] = result["synced_at"]
        _runtime["last_result"] = result
        return result


# ---------- 健康检测(保活) ----------
def health_check_zen_models() -> dict:
    """重新探测所有 opencode 渠道里的模型; 连续失败 >= 阈值 的从渠道移除。
    保护: 若某渠道模型全部失败(疑似网络/上游整体故障), 保留不动, 避免误杀。"""
    with _lock:
        session = get_session()
        removed, kept = [], []
        try:
            provs = (session.query(Provider)
                     .filter_by(base_url=ZEN_BASE_URL).all())
            for p in provs:
                models = list(p.models or [])
                if not models:
                    continue
                results = probe_many(models)
                alive = []
                dead = []
                for m in models:
                    ok, why = results[m]
                    if ok or _is_free_quota(why) or _is_lane_closed(why):
                        # 可用 / 429 当日额度 / 403 车道对出口关闭(unknown,
                        # 非模型裁决) → 保留; 对齐插件不因出口抖动移除模型
                        _runtime["fail_count"][m] = 0
                        alive.append(m)
                    else:
                        n = _runtime["fail_count"].get(m, 0) + 1
                        _runtime["fail_count"][m] = n
                        if n >= settings.ZEN_PROBE_FAIL_LIMIT:
                            dead.append(m)
                            logger.warning("opencode 模型 %s 连续失败 %d 次, 移出渠道", m, n)
                        else:
                            alive.append(m)
                if dead and not alive:
                    # 全灭保护: 疑似整体故障, 保留现有模型不清理
                    logger.warning("opencode 渠道 %s 全部模型失败, 本次保留不清理(防误杀)", p.name)
                    continue
                p.models = alive
                removed.extend(dead)
                kept.extend(alive)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        result = {
            "ok": True,
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "removed": removed,
            "kept": list(dict.fromkeys(kept)),
        }
        _runtime["last_probe_at"] = result["checked_at"]
        _runtime["last_result"] = result
        return result


def runtime_state() -> dict:
    return _runtime