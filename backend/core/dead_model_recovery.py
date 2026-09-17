#!/usr/bin/env python3
# ============================================================
# 死模型自动复检 — 每日给被永久拉黑的模型一次"平反"机会。
#
# dead_models 设计初衷是 404/410 确定性下架 → 永久拉黑; 但实测里
# NVIDIA 等上游的"平台故障(502/断连/读超时)"与"账户权限 404
# (Function not found for account)"同样会触发拉黑, 而拉黑后永不复查,
# 恢复可用的模型(如 kimi-k3 曾被 8/31 晚平台故障连坐误杀)再也回不来。
# 本模块按调度周期对全部拉黑记录做一次 1-token 真实请求探测:
#   200 → 自动平反: 删拉黑 + 补回渠道 models + 放回同渠道兄弟模型所在的
#         路由组(只增不减, 不覆盖用户手动调整; 用户手动 disabled 的不补)
#   404/410/429/超时 → 保持拉黑, 下个周期再探 —— 对真死模型零打扰,
#   对误杀模型最迟一个周期内自动复活, 无需人工干预。
# ============================================================
import json
import logging
from concurrent.futures import ThreadPoolExecutor

import httpx

from sqlalchemy.orm.attributes import flag_modified

from backend.config import settings
from backend.models import DeadModel, Provider, Router, get_session

logger = logging.getLogger("gateway")


# ============================================================
# 探测
# ============================================================
def probe_model_online(base_url: str, api_key: str, model: str,
                       timeout: float | None = None) -> str:
    """对单个模型发 1-token 真实请求, 判定上游当前是否可用。

    返回:
      "online"     HTTP 2xx — 模型活着, 可以平反
      "dead"       404(非权限类)/410 — 确定性不可用, 保持拉黑
      "no_access"  404 且报 Function not found for account — 账户未开通,
                   保持拉黑(用户在官网开通后自然转 200)
      "auth"       401/403 — key 本身失效(该渠道所有模型都会失败, 跳过整渠道)
      "uncertain"  429/5xx/400/网络错误/超时 — 状态不明, 保持拉黑下轮再探
    """
    timeout = timeout if timeout is not None else settings.DEAD_MODEL_PROBE_TIMEOUT
    base = (base_url or "").rstrip("/")
    if not base:
        return "uncertain"
    if base.endswith("/v1"):
        candidates = [f"{base}/chat/completions"]
    else:
        candidates = [f"{base}/v1/chat/completions", f"{base}/chat/completions"]

    headers = {"Content-Type": "application/json"}
    if api_key and api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }
    last = ""
    try:
        with httpx.Client(timeout=timeout) as client:
            for url in candidates:
                try:
                    resp = client.post(url, headers=headers, json=payload)
                except httpx.TimeoutException:
                    return "uncertain"
                except httpx.HTTPError as e:
                    last = str(e)[:120]
                    continue
                code = resp.status_code
                if 200 <= code < 300:
                    return "online"
                if code in (401, 403):
                    return "auth"
                body = resp.text[:300].lower()
                if code == 404:
                    if "not found for account" in body \
                            or ("function" in body and "account" in body):
                        return "no_access"
                    return "dead"
                if code == 410:
                    return "dead"
                return "uncertain"  # 400/429/5xx: 上游繁忙或格式问题, 非模型死
    except httpx.HTTPError as e:
        last = str(e)[:120]
    logger.warning("死模型复检探测 %s 网络异常: %s", model, last)
    return "uncertain"


# ============================================================
# 平反
# ============================================================
def _revive(session, user_id: int, provider: Provider, model: str) -> bool:
    """平反一个模型: 删拉黑记录 + 补回渠道模型表 + 放回同渠道兄弟路由组。

    用户手动 disabled 的模型只解封拉黑, 不补回渠道/路由组(尊重用户意图)。
    返回是否发生了任何落库变化。
    """
    changed = False
    for d in session.query(DeadModel).filter_by(
            user_id=user_id, provider_name=provider.name, model=model).all():
        session.delete(d)
        changed = True

    disabled = provider.disabled_models or []
    if isinstance(disabled, str):
        disabled = json.loads(disabled)
    entry = f"{provider.name}-{model}"
    if model not in disabled:
        models = provider.models or []
        if isinstance(models, str):
            models = json.loads(models)
        if model not in models:
            models.append(model)
            provider.models = models
            # 原地 append 后赋回原对象: SQLAlchemy 判相等视作未修改, 不落库,
            # 必须 flag_modified 强制标记(删除拉黑记录同事务已证明会持久化)
            flag_modified(provider, "models")
            changed = True
        # 放回同渠道"兄弟模型"所在的路由组(只增不减, 不碰组内其他成员)
        for g in session.query(Router).filter_by(user_id=user_id).all():
            ms = g.models or []
            if isinstance(ms, str):
                ms = json.loads(ms)
            has_sibling = any(
                m != entry and m.startswith(f"{provider.name}-") for m in ms
            )
            if has_sibling and entry not in ms:
                ms.append(entry)
                g.models = ms
                flag_modified(g, "models")
                changed = True
    session.commit()
    return changed


def revive_model(user_id: int, provider_name: str, model: str) -> bool:
    """手动平反(API/用户意图, 不做探测): 渠道不存在返回 False。"""
    s = get_session()
    try:
        p = s.query(Provider).filter_by(
            user_id=user_id, name=provider_name).first()
        if not p:
            return False
        return _revive(s, user_id, p, model)
    finally:
        s.close()


# ============================================================
# 主入口(每日定时 / 手动触发共用)
# ============================================================
def recover_dead_models(user_id: int | None = None) -> dict:
    """扫描全部(或指定)用户的死模型拉黑记录, 真实探测后平反已恢复的。

    返回统计: {probed, revived:[...], dead, no_access, uncertain,
               auth_failed_channels:[...], skipped_disabled, orphan_removed}
    探测在后台线程并发执行(不碰 DB), 平反写库回到主线程串行, SQLite 安全。
    """
    stats = {
        "probed": 0, "revived": [], "dead": 0, "no_access": 0,
        "uncertain": 0, "auth_failed_channels": [], "skipped_disabled": 0,
        "orphan_removed": 0,
    }
    s = get_session()
    try:
        q = s.query(DeadModel)
        if user_id is not None:
            q = q.filter_by(user_id=user_id)
        rows = q.all()
        if not rows:
            stats["note"] = "没有拉黑记录"
            return stats
        # 拉齐相关用户的渠道表
        providers_by_user: dict[int, dict[str, Provider]] = {}
        for uid in {r.user_id for r in rows}:
            providers_by_user[uid] = {
                p.name: p
                for p in s.query(Provider).filter_by(user_id=uid).all()
            }
        # 按 (user, provider) 分组
        groups: dict[tuple[int, str], list] = {}
        for r in rows:
            groups.setdefault((r.user_id, r.provider_name), []).append(r.model)

        for (uid, pname), models in sorted(groups.items()):
            p = providers_by_user.get(uid, {}).get(pname)
            if p is None:
                # 渠道已删除: 拉黑记录失去意义, 清掉孤儿记录
                for d in s.query(DeadModel).filter_by(
                        user_id=uid, provider_name=pname).all():
                    s.delete(d)
                stats["orphan_removed"] += len(models)
                s.commit()
                continue
            if not p.enabled:
                stats["skipped_disabled"] += len(models)  # 渠道停用, 不探测
                continue
            try:
                from backend.auth.security import decrypt_secret
                key = decrypt_secret(p.api_key_encrypted)
            except Exception:
                stats["auth_failed_channels"].append(pname)
                continue
            with ThreadPoolExecutor(
                    max_workers=settings.DEAD_MODEL_PROBE_CONCURRENCY) as ex:
                results = dict(zip(
                    models,
                    ex.map(lambda m: probe_model_online(
                        p.base_url, key, m), models),
                ))
            for m, outcome in results.items():
                stats["probed"] += 1
                if outcome == "online":
                    try:
                        if _revive(s, uid, p, m):
                            stats["revived"].append(f"{pname}-{m}")
                            logger.warning(
                                "死模型复检: %s-%s 已恢复可用, 自动解封并放回路由组",
                                pname, m)
                    except Exception:
                        logger.exception("死模型复检平反失败 %s-%s", pname, m)
                elif outcome == "no_access":
                    stats["no_access"] += 1
                elif outcome == "dead":
                    stats["dead"] += 1
                else:
                    stats["uncertain"] += 1
        s.commit()
    finally:
        s.close()
    return stats
