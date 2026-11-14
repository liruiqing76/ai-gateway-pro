# ============================================================
# 智能网关 — 模型目录 API
# 按裸模型名聚合全部渠道: 可用性(熔断/冷却/下架)、质量分、延迟、
# 今日用量、积分消耗倍率。供前端「模型列表 · 按模型名」视图使用。
# ============================================================
import json
import logging
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func

from backend.auth.middleware import get_current_user_from_jwt
from backend.core.circuit import circuit_breaker
from backend.core.scheduler import quality_tracker
from backend.core.dead_models import banned_keys
from backend.models import (
    get_session, User, Provider, UsageLog, DownedModel, ModelRate,
)

logger = logging.getLogger("gateway.model_catalog")
router = APIRouter(prefix="/api/models", tags=["model-catalog"])


def _json_list(value):
    """Provider 的 JSON 列兼容字符串存储。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return []
    return value or []


def _free_channel_names() -> set:
    """白嫖渠道 id 集合(登录即免费, 无需 key)。"""
    try:
        from backend.api.channels import _CHANNEL_CATALOG
        return set(_CHANNEL_CATALOG.keys())
    except Exception:
        return set()


def _lane_resume() -> dict:
    """免密车道(opencode/kilo)的额度恢复提示: {channel: {at, reason}}。"""
    out = {}
    try:
        from backend.core import lane_state as lane_store
        for ch in ("opencode", "kilo"):
            entry = lane_store.get(ch) or {}
            if entry.get("resume_at"):
                out[ch] = {"at": entry.get("resume_at"), "reason": entry.get("reason") or ""}
    except Exception:
        pass
    return out


@router.get("/catalog")
async def model_catalog(user: User = Depends(get_current_user_from_jwt)):
    """按裸模型名聚合的目录: 每个模型 → 提供它的渠道列表(状态/质量/倍率)。"""
    session = get_session()
    try:
        providers = (
            session.query(Provider).filter_by(user_id=user.id, enabled=True).all()
        )
        rates = {
            (r.provider_name, r.model): (r.rate, r.source or "manual")
            for r in session.query(ModelRate).filter_by(user_id=user.id).all()
        }
        downed = {
            f"{d.provider_name}||{d.model}": d
            for d in session.query(DownedModel).filter_by(user_id=user.id).all()
        }
        dead = banned_keys(user.id)
        free_channels = _free_channel_names()
        lane_resume = _lane_resume()

        # 今日用量(直接从 usage_logs 聚合, 比内存计数可靠——重启不丢)
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        usage_rows = (
            session.query(
                UsageLog.provider_name, UsageLog.model_name,
                func.count(), func.sum(UsageLog.total_tokens),
            )
            .filter(UsageLog.success.is_(True), UsageLog.created_at >= today)
            .group_by(UsageLog.provider_name, UsageLog.model_name)
            .all()
        )
        today_usage = {
            f"{pn}||{mn}": {"calls": int(cnt or 0), "tokens": int(toks or 0)}
            for pn, mn, cnt, toks in usage_rows
        }

        now = time.time()
        catalog: dict[str, dict] = {}
        for p in providers:
            disabled = set(_json_list(p.disabled_models))
            for m in _json_list(p.models):
                if m in disabled:
                    continue
                key = f"{p.name}||{m}"
                if key in dead:
                    continue
                cb_open = circuit_breaker.is_open(key)
                cooldown = circuit_breaker.cooldown_seconds(key) if cb_open else 0
                stats = quality_tracker.get_stats(key)
                usage = today_usage.get(key, {"calls": 0, "tokens": 0})
                entry = {
                    "provider": p.name,
                    "prefixed": f"{p.name}-{m}",
                    "rate": float(rates.get((p.name, m), (1.0, "manual"))[0]),
                    "rate_source": rates.get((p.name, m), (1.0, "manual"))[1],
                    "free": bool(p.free_only) or p.name in free_channels,
                    "circuit_open": cb_open,
                    "cooldown_seconds": cooldown,
                    "cooldown_until": (
                        (datetime.now() + timedelta(seconds=cooldown)).strftime("%H:%M")
                        if cooldown > 0 else None
                    ),
                    "quality_score": round(float(stats.get("score") or 0.0), 3),
                    "avg_latency_ms": stats.get("avg_latency_ms"),
                    "today_calls": usage["calls"],
                    "today_tokens": usage["tokens"],
                    "downed": None,
                }
                d = downed.get(key)
                if d is not None:
                    remaining = int((d.recovers_at - datetime.now()).total_seconds())
                    if remaining > 0:
                        entry["downed"] = {
                            "reason": d.reason or "",
                            "recovers_at": d.recovers_at.strftime("%m-%d %H:%M"),
                            "remaining_seconds": remaining,
                        }
                if p.name in lane_resume:
                    entry["lane_resume"] = lane_resume[p.name]
                info = catalog.setdefault(m, {
                    "model": m,
                    "context_length": None,
                    "providers": [],
                })
                info["providers"].append(entry)

        out = []
        for m, info in catalog.items():
            provs = info["providers"]
            usable = [e for e in provs if not e["circuit_open"] and not e["downed"]]
            out.append({
                "model": m,
                "providers": sorted(provs, key=lambda e: (-e["quality_score"], e["rate"])),
                "usable_count": len(usable),
                "total_count": len(provs),
                "min_rate": min(e["rate"] for e in provs),
            })
        out.sort(key=lambda x: (-x["usable_count"], x["model"]))
        return {
            "models": out,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    finally:
        session.close()


class RateBody(BaseModel):
    provider: str
    model: str
    rate: float


@router.put("/rate")
async def set_model_rate(body: RateBody, user: User = Depends(get_current_user_from_jwt)):
    """设置某渠道-模型的积分消耗倍率(0=免费)。未设置的模型等效 1.0。

    手填标记 source='manual', 永不被上游自动同步覆盖; 想恢复自动跟随
    可删行后重新登录渠道(或等下次刷新写回 auto 值)。
    """
    if body.rate < 0 or body.rate > 10000:
        raise HTTPException(400, "倍率需在 0-10000 之间")
    session = get_session()
    try:
        row = session.query(ModelRate).filter_by(
            user_id=user.id, provider_name=body.provider, model=body.model,
        ).first()
        if row:
            row.rate = body.rate
            row.source = "manual"
        else:
            session.add(ModelRate(
                user_id=user.id,
                provider_name=body.provider,
                model=body.model,
                rate=body.rate,
                source="manual",
            ))
        session.commit()
        return {"provider": body.provider, "model": body.model, "rate": body.rate}
    finally:
        session.close()
