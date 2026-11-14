# ============================================================
# 免费渠道 — Kilo 公共网关 (api.kilo.ai, 无需任何认证)
#
# 参考 dsh-our-free-model src/kilo.js: 拉 isFree 模型清单, 无认证直连。
# 2026-10-24 本机实测: /models 200, 17 个 isFree 模型; kilo-auto/free 对话 200。
# 同机 OpenCode Zen 匿名车道已被上游 FreeTierError 关闭(插件本机同样全灭),
# Kilo 是当下唯一可用的免密免费池, 默认由启动钩子自动为所有用户收编。
#
# 合规: Kilo 官方在模型卡声明 prompt 可能被记录用于改进服务, 勿发敏感内容。
# ============================================================

from __future__ import annotations

import logging
import threading
import time

import httpx

from backend.config import settings
from backend.models import get_session, Provider, Router, User

logger = logging.getLogger("gateway.kilo")

KILO_BASE_URL = "https://api.kilo.ai/api/gateway"
KILO_MODELS_URL = KILO_BASE_URL + "/models"
KILO_CHAT_URL = KILO_BASE_URL + "/chat/completions"
KILO_UA = "ai-gateway-pro/kilo-sync"

PROVIDER_NAME = "kilo"
ROUTER_NAME = "kilo"

_lock = threading.Lock()
_runtime: dict = {"last_sync_at": None, "last_result": None}


def fetch_kilo_free_models(timeout: float = 25.0) -> list[str]:
    """拉 isFree 模型清单(照插件: GET /models → data[].isFree)。失败返回空。"""
    from backend.core import upstream_http
    try:
        with upstream_http.make_sync(timeout=timeout) as client:
            r = client.get(KILO_MODELS_URL, headers={"User-Agent": KILO_UA})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("kilo 模型目录拉取失败: %s", e)
        return []
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        mid = str(it.get("id") or "").strip()
        if mid and it.get("isFree") is True:
            out.append(mid)
    return out


def probe_kilo_model(model_id: str, timeout: float = 40.0) -> tuple[bool, str]:
    """真实 1-token 探测(免认证)。200 = 可用。"""
    from backend.core import upstream_http
    try:
        with upstream_http.make_sync(timeout=timeout) as client:
            r = client.post(KILO_CHAT_URL,
                            json={"model": model_id,
                                  "messages": [{"role": "user", "content": "OK"}],
                                  "max_tokens": 1},
                            headers={"User-Agent": KILO_UA})
        if r.status_code == 200:
            return True, "ok"
        return False, f"{r.status_code} {r.text[:90]}"
    except Exception as e:
        return False, f"ERR {str(e)[:70]}"


# Kilo 的 429 是"免费推广额度用尽", 额度会随账号注册/时间重置 —— 与 opencode
# 的 FreeUsageLimitError 同类。若把它当不可用, 同步会判定整条车道 closed 并
# 在界面上显示红色"车道关闭", 但模型其实明天就能用。统一按"可用(等重置)"保留,
# 与 zen_sources._is_free_quota 口径一致。
_KILO_QUOTA_HINTS = (
    "promotion_model_limit_reached",
    "sign up for free to continue",
    "rate_limit_exceeded",
    "quota",
)


def _is_kilo_quota(why: str) -> bool:
    """探测失败原因是否属于"额度用尽(会重置)"而非"模型/车道不可用"。"""
    t = (why or "").lower()
    if not t.startswith("429"):
        return False
    return any(h in t for h in _KILO_QUOTA_HINTS)


def _apply_kilo_provider(session, user: User, models: list[str]):
    p = (session.query(Provider)
         .filter_by(user_id=user.id, base_url=KILO_BASE_URL).first())
    if p is None:
        p = Provider(user_id=user.id, name=PROVIDER_NAME,
                     base_url=KILO_BASE_URL,
                     api_key_encrypted="",  # 免 key 匿名渠道
                     models=models, free_only=True, enabled=True)
        session.add(p)
        logger.info("创建 kilo 免费渠道 user=%s 模型=%d 个", user.username, len(models))
    else:
        p.models = models
        p.free_only = True
        p.enabled = True
    return p


def _ensure_kilo_router(session, user: User, models: list[str]):
    prefixed = [f"{PROVIDER_NAME}-{m}" for m in models]
    r = (session.query(Router)
         .filter_by(user_id=user.id, name=ROUTER_NAME).first())
    if r is None:
        r = Router(user_id=user.id, name=ROUTER_NAME, models=prefixed,
                   strategy="failover")
        session.add(r)
        logger.info("创建 kilo 路由组 user=%s 模型=%d 个", user.username, len(prefixed))
    else:
        r.models = prefixed
    return r


def sync_kilo_free_models(probe_timeout: float = 40.0,
                          max_probes: int = 20) -> dict:
    """发现 + 抽查 + 落地到所有激活用户。全灭保护同 zen。"""
    with _lock:
        models = fetch_kilo_free_models()
        if not models:
            return {"ok": False, "error": "未能获取 kilo 免费模型列表"}
        # 抽查(前 max_probes 个, 串行即可, 免密渠道无风暴风险)
        usable, bad, quota_hit = [], {}, 0
        for m in models[:max_probes]:
            ok, why = probe_kilo_model(m, timeout=probe_timeout)
            if ok:
                usable.append(m)
            elif _is_kilo_quota(why):
                # 429 免费推广额度用尽: 额度会重置, 模型本身可用 → 保留
                quota_hit += 1
                usable.append(m)
            else:
                bad[m] = why
        # 未抽查到的默认保留(目录已声明 isFree; 探测只是剔雷)
        unprobed = models[max_probes:]
        final = sorted(set(usable) | set(unprobed))

        # 车道状态: 只要有模型被判可用(含"额度用尽但会重置")或未抽查 → open;
        # 全被明确拒绝(模型级 4xx/5xx) → closed; 其余 unknown。
        if usable or unprobed:
            lane_state = "open"
        elif bad:
            lane_state = "closed"
        else:
            lane_state = "unknown"

        if not usable and bad:
            result = {"ok": True,
                      "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                      "total_free": len(models), "usable": [],
                      "unusable": bad, "users_updated": 0,
                      "lane_state": lane_state,
                      "skipped": "抽查全部失败, 保留现有渠道不覆盖"}
            _runtime["last_result"] = result
            return result

        session = get_session()
        try:
            users = session.query(User).filter_by(is_active=True).all()
            for u in users:
                _apply_kilo_provider(session, u, final)
                _ensure_kilo_router(session, u, final)
            session.commit()
            updated = len(users)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        result = {"ok": True,
                  "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "total_free": len(models), "usable": final,
                  "unusable": bad, "users_updated": updated,
                  "lane_state": lane_state,
                  **({"quota_only": quota_hit} if quota_hit else {})}
        # 车道状态落盘: 进程重启后到下一轮同步前, 界面仍能显示上次结论
        try:
            from backend.core import lane_state as _ls
            _ls.save("kilo", lane_state, synced_at=result["synced_at"],
                     usable=len(final))
        except Exception:
            pass
        _runtime["last_sync_at"] = result["synced_at"]
        _runtime["last_result"] = result
        return result


def runtime_state() -> dict:
    return _runtime
