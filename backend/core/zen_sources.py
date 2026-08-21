#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenCode Zen 免费模型 — 自动发现 + 可用性探测 + 持续健康检测。

思路(对齐"免费模型一直可用"):
  1. 定时拉取 models.dev 快照(在线优先, 本地 opencode 缓存兜底), 提取 OpenCode Zen 免费模型
     (cost==0 或 -free 后缀)
  2. 对每个模型做真实探测(POST zen/v1/chat/completions, max_tokens=1): 200 = 可用
  3. 同步: 为每个激活用户创建/更新 "opencode" 渠道(base_url=zen/v1, 免 key),
     models 只保留"实测可用"的模型
  4. 健康检测: 定时重新探测渠道内模型, 连续失败 N 次自动从 models 移除,
     保证渠道里的模型始终是活的; 瞬时抖动(网络/上游临时故障)不会被立刻踢掉
"""
import concurrent.futures as cf
import json
import logging
import threading
import time
from pathlib import Path

import httpx

from backend.config import settings
from backend.models import get_session, Provider, Router, User

logger = logging.getLogger("gateway")

ZEN_BASE_URL = "https://opencode.ai/zen/v1"
ZEN_CHAT_URL = ZEN_BASE_URL + "/chat/completions"
MODELS_DEV_URL = "https://models.dev/api.json"
LOCAL_MODELS_CACHE = Path.home() / ".cache" / "opencode" / "models.json"

_lock = threading.Lock()
_runtime = {
    "last_sync_at": None,
    "last_probe_at": None,
    "last_result": None,
    "fail_count": {},   # model -> 连续失败次数
}


# ---------- 数据源 ----------
def _is_free_model(model_id: str, meta: dict) -> bool:
    """免费判定: 价格全为 0(cost 字典/数字), 或模型 ID 带 -free 后缀。二者满足其一。"""
    cost = (meta or {}).get("cost")
    if cost == 0:
        return True
    if isinstance(cost, dict) and cost:
        vals = [v for v in cost.values() if isinstance(v, (int, float))]
        if vals and all(v == 0 for v in vals):
            return True
    return "free" in model_id.lower()


def fetch_zen_free_models(timeout: float = 25.0) -> list:
    """拉取 models.dev 快照中 opencode(OpenCode Zen) 的免费模型列表。在线失败回退本地缓存。
    只收录免费档(cost 全 0 或 ID 带 free); opencode-go 等付费 provider 完全不碰。"""
    data = None
    try:
        r = httpx.get(MODELS_DEV_URL, timeout=timeout,
                      headers={"User-Agent": "ai-gateway-pro/zen-sync"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("models.dev 在线拉取失败(%s), 回退本地缓存 %s", e, LOCAL_MODELS_CACHE)
    if not data:
        try:
            data = json.loads(LOCAL_MODELS_CACHE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("本地 models.dev 缓存读取失败: %s", e)
            return []
    zen = (data or {}).get("opencode") or {}
    ms = zen.get("models") or {}
    return [mid for mid, m in ms.items() if _is_free_model(mid, m)]


# ---------- 探测 ----------
def probe_zen_model(model_id: str, timeout: float = 25.0) -> tuple:
    """探测单个模型: (可用, 原因)。200 即可用; 401 not supported=不在匿名白名单; 403=地区; 400=临时。"""
    try:
        r = httpx.post(
            ZEN_CHAT_URL,
            json={"model": model_id, "messages": [{"role": "user", "content": "OK"}], "max_tokens": 1},
            timeout=timeout,
            headers={"User-Agent": "ai-gateway-pro/zen-probe"},
        )
        if r.status_code == 200:
            return True, "ok"
        return False, f"{r.status_code} {r.text[:90]}"
    except Exception as e:
        return False, f"ERR {str(e)[:70]}"


def probe_many(models: list, max_workers: int = 8) -> dict:
    """并发探测, 返回 {model: (ok, reason)}。"""
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        return dict(zip(models, ex.map(lambda m: probe_zen_model(m), models)))


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
def sync_zen_free_models() -> dict:
    """执行一次完整同步: 拉列表 → 真探测 → 给每个激活用户建/更新渠道。线程安全。"""
    with _lock:
        models = fetch_zen_free_models()
        if not models:
            return {"ok": False, "error": "未能获取 opencode 免费模型列表"}
        results = probe_many(models)
        usable = [m for m, (ok, _) in results.items() if ok]
        unusable = {m: why for m, (ok, why) in results.items() if not ok}

        if not usable:
            # 全灭保护: 疑似上游/网络整体故障, 保留现有渠道不覆盖, 避免模型全被清空
            result = {
                "ok": True,
                "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "total_free": len(models),
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
        result = {
            "ok": True,
            "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_free": len(models),
            "usable": usable,
            "unusable": unusable,
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
                    if ok:
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