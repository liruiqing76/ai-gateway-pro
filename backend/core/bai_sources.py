#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
b.ai 免费模型 — 自动发现 + 可用性探测 + 持续健康检测。

与 OpenCode Zen(opencode) 同一套"免费模型一直可用"玩法:
  1. 拉取 b.ai 上游 {base}/models 目录(或配置的模型 ID 列表), 收集候选
  2. 真实探测 POST {base}/chat/completions(max_tokens=1):
       200 = 可用; 429 = 免费额度当日用尽(模型保留); 401 = 需 key/付费档;
       其余 = 死模型
  3. 同步: 为每个激活用户创建/更新 "biai" 渠道(base_url, 免 key 或配置 key),
     models 只保留探测可用的模型
  4. 健康检测: 定时重探, 连续失败 N 次自动移出渠道;
     所有模型同时失败(疑似网络/上游整体故障)时保留不动, 防误杀

说明: b.ai 在上游网络不可直连的环境(如国内直连)会同步失败, 此时保留现有
渠道不覆盖, 不会清空模型; 部署到能连上 b.ai 的网络后即可自动生效。
"""
import concurrent.futures as cf
import logging
import threading
import time

import httpx

from backend.config import settings
from backend.models import get_session, Provider, Router, User

logger = logging.getLogger("gateway")

# 上游可配: 配置项 B_AI_BASE_URL 可用环境变量 GW_B_AI_BASE_URL 覆盖(见 config.py)
B_AI_BASE_URL = settings.B_AI_BASE_URL.rstrip("/")
B_AI_CHAT_URL = B_AI_BASE_URL + "/chat/completions"
B_AI_MODELS_URL = B_AI_BASE_URL + "/models"
# 空 = 匿名免费档探测; 有值 = 所有请求带 Bearer token(环境变量 GW_B_AI_API_KEY 覆盖)
B_AI_API_KEY = settings.B_AI_API_KEY

B_AI_PROVIDER_NAME = "biai"
B_AI_ROUTER_NAME = "biai"

_lock = threading.Lock()
_runtime = {
    "last_sync_at": None,
    "last_probe_at": None,
    "last_result": None,
    "fail_count": {},   # model -> 连续失败次数
}


def _auth_headers() -> dict:
    h = {"User-Agent": "ai-gateway-pro/bai-sync", "Content-Type": "application/json"}
    if B_AI_API_KEY:
        h["Authorization"] = f"Bearer {B_AI_API_KEY}"
    return h


# ---------- 数据源 ----------
def _is_free_hint(model_id: str) -> bool:
    """模型 ID 里是否带"免费档"特征(目录过大时优先探测这类, 防探测风暴)。"""
    i = model_id.lower()
    return ("free" in i or i.endswith(("-lite", "-mini", "-nano", "-sprout")))


def _select_candidates(ids: list) -> list:
    """目录过大时优先选免费特征模型, 否则全量。上限 B_AI_PROBE_CAP。"""
    cap = settings.B_AI_PROBE_CAP
    if len(ids) <= cap:
        return ids
    hinted = [i for i in ids if _is_free_hint(i)]
    rest = [i for i in ids if not _is_free_hint(i)]
    return (hinted + rest)[:cap]


def fetch_bai_free_models(timeout: float = 25.0) -> list:
    """拉取 b.ai 上游 /v1/models 目录, 收集候选模型 ID。

    目录拉取失败 / 未授权时返回空列表, 交由配置的兜底模型列表补位。
    """
    ids = []
    try:
        r = httpx.get(B_AI_MODELS_URL, timeout=timeout, headers=_auth_headers())
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("b.ai 上游模型目录拉取失败: %s", e)
        data = None
    if isinstance(data, dict):
        items = data.get("data") if isinstance(data.get("data"), list) else data.get("models")
    elif isinstance(data, list):
        items = data
    else:
        items = None
    if isinstance(items, list):
        for it in items:
            mid = (it.get("id") if isinstance(it, dict) else str(it)) or ""
            mid = mid.strip()
            if mid:
                ids.append(mid)
    ids = sorted(set(ids))
    if not ids:
        # 兜底: 目录不可用时用配置的模型列表(B_AI_FREE_MODEL_IDS)
        ids = [m.strip() for m in (settings.B_AI_FREE_MODEL_IDS or []) if m and m.strip()]
    return _select_candidates(ids)


def _is_free_quota(why: str) -> bool:
    """探测原因是否为 429 免费额度限制(模型本身可用, 当日额度用尽)。"""
    return why.startswith("429")


# ---------- 探测 ----------
def probe_bai_model(model_id: str, timeout: float = 25.0) -> tuple:
    """探测单个模型: (可用, 原因)。200 可用; 429 免费额度用尽(可用); 其余不可用。"""
    try:
        r = httpx.post(
            B_AI_CHAT_URL,
            json={"model": model_id, "messages": [{"role": "user", "content": "OK"}], "max_tokens": 1},
            timeout=timeout,
            headers=_auth_headers(),
        )
        if r.status_code == 200:
            return True, "ok"
        return False, f"{r.status_code} {r.text[:90]}"
    except Exception as e:
        return False, f"ERR {str(e)[:70]}"


def probe_many(models: list, max_workers: int = 8, timeout: float = 25.0) -> dict:
    """并发探测, 返回 {model: (ok, reason)}。"""
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        return dict(zip(models, ex.map(lambda m: probe_bai_model(m, timeout=timeout),
                                       models)))


# ---------- 落地(每用户渠道 + 路由组) ----------
def _apply_bai_provider(session, user: User, models: list):
    """为该用户创建/更新 biai 渠道。免 key 时 api_key_encrypted 为空。"""
    prov = (session.query(Provider)
            .filter_by(user_id=user.id, base_url=B_AI_BASE_URL).first())
    if prov is None:
        from backend.auth.security import encrypt_secret
        prov = Provider(
            user_id=user.id,
            name=B_AI_PROVIDER_NAME,
            base_url=B_AI_BASE_URL,
            api_key_encrypted=encrypt_secret(B_AI_API_KEY) if B_AI_API_KEY else "",
            models=models,
            free_only=True,
            enabled=True,
        )
        session.add(prov)
        logger.info("创建 biai 免费渠道 user=%s 模型=%d 个", user.username, len(models))
    else:
        prov.models = models
        prov.free_only = True
        prov.enabled = True
    return prov


def _ensure_bai_router(session, user: User, models: list):
    """创建/更新 'biai' 路由组(模型带渠道前缀), 随可用集自动刷新。"""
    prefixed = [f"{B_AI_PROVIDER_NAME}-{m}" for m in models]
    r = (session.query(Router)
         .filter_by(user_id=user.id, name=B_AI_ROUTER_NAME).first())
    if r is None:
        r = Router(user_id=user.id, name=B_AI_ROUTER_NAME,
                   models=prefixed, strategy="failover")
        session.add(r)
        logger.info("创建 biai 免费路由组 user=%s 模型=%d 个", user.username, len(prefixed))
    else:
        r.models = prefixed
    return r


# ---------- 同步(发现+探测+落地) ----------
def sync_bai_free_models(probe_timeout: float = 25.0) -> dict:
    """执行一次完整同步: 拉列表 → 真探测 → 给每个激活用户建/更新渠道。线程安全。"""
    with _lock:
        models = fetch_bai_free_models()
        if not models:
            return {"ok": False, "error": "未能获取 b.ai 免费模型列表(目录不可达或未配置)"}
        results = probe_many(models, timeout=probe_timeout)
        # 200 或 429(免费额度当日用尽) 都视为可用
        usable = [m for m, (ok, why) in results.items() if ok or _is_free_quota(why)]
        unusable = {m: why for m, (ok, why) in results.items() if not ok and not _is_free_quota(why)}

        if not usable:
            # 全灭保护: 疑似上游/网络整体故障, 保留现有渠道不覆盖
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
                _apply_bai_provider(session, u, usable)
                _ensure_bai_router(session, u, usable)
            session.commit()
            updated_users = len(users)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        # 同步成功的模型清零失败计数
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
def health_check_bai_models() -> dict:
    """重新探测所有 biai 渠道里的模型; 连续失败 >= 阈值 的从渠道移除。
    保护: 若某渠道模型全部失败(疑似网络/上游整体故障), 保留不动, 避免误杀。"""
    with _lock:
        session = get_session()
        removed, kept = [], []
        try:
            provs = (session.query(Provider)
                     .filter_by(base_url=B_AI_BASE_URL).all())
            for p in provs:
                models = list(p.models or [])
                if not models:
                    continue
                results = probe_many(models)
                alive = []
                dead = []
                for m in models:
                    ok, why = results[m]
                    if ok or _is_free_quota(why):
                        _runtime["fail_count"][m] = 0
                        alive.append(m)
                    else:
                        n = _runtime["fail_count"].get(m, 0) + 1
                        _runtime["fail_count"][m] = n
                        if n >= settings.B_AI_PROBE_FAIL_LIMIT:
                            dead.append(m)
                            logger.warning("biai 模型 %s 连续失败 %d 次, 移出渠道", m, n)
                        else:
                            alive.append(m)
                if dead and not alive:
                    logger.warning("biai 渠道 %s 全部模型失败, 本次保留不清理(防误杀)", p.name)
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