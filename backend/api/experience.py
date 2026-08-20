# ============================================================
# 智能网关 — 经验库 / 每日总结 API
# ============================================================

import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy import func

from backend.config import settings
from backend.models import get_session, ExperienceLog, DailySummary, User
from backend.auth.middleware import get_current_user_any

logger = logging.getLogger("gateway.experience_api")
router = APIRouter(prefix="/api/experience", tags=["experience"])


_KIND_LABEL = {"lesson": "教训", "fact": "事实", "sop": "流程"}


@router.get("")
async def list_experiences(
    user: User = Depends(get_current_user_any),
    kind: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    """经验库列表。"""
    session = get_session()
    try:
        q = session.query(ExperienceLog).filter_by(user_id=user.id)
        if kind:
            q = q.filter_by(kind=kind)
        rows = q.order_by(ExperienceLog.created_at.desc()).limit(limit).all()
        return {"data": [_exp(r) for r in rows], "count": len(rows)}
    finally:
        session.close()


@router.get("/stats")
async def experience_stats(user: User = Depends(get_current_user_any)):
    """经验库统计。"""
    session = get_session()
    try:
        base = session.query(ExperienceLog).filter_by(user_id=user.id)
        total = base.count()
        by_kind = dict(session.query(
            ExperienceLog.kind, func.count(ExperienceLog.id)
        ).filter_by(user_id=user.id).group_by(ExperienceLog.kind).all())
        pinned = base.filter_by(pinned=True).count()
        # 近7天新增
        since = datetime.now() - timedelta(days=7)
        week = base.filter(ExperienceLog.created_at >= since).count()
        return {
            "total": total,
            "lessons": by_kind.get("lesson", 0),
            "facts": by_kind.get("fact", 0),
            "sops": by_kind.get("sop", 0),
            "pinned": pinned,
            "week_added": week,
            "enabled": bool(settings.EXPERIENCE_ENABLED),
        }
    finally:
        session.close()


@router.post("/distill")
async def distill(
    user: User = Depends(get_current_user_any),
    limit: int = Query(40, ge=1, le=200),
):
    """手动从最近聊天记录提炼经验。"""
    if not settings.EXPERIENCE_ENABLED:
        raise HTTPException(400, "经验积累未启用")
    from backend.core.experience import distill_chats
    n = await distill_chats(user.id, limit=min(limit, settings.EXPERIENCE_DISTILL_LIMIT))
    return {"created": n}


@router.get("/summaries")
async def list_summaries(
    user: User = Depends(get_current_user_any),
    limit: int = Query(30, ge=1, le=90),
):
    """每日总结列表。"""
    session = get_session()
    try:
        rows = session.query(DailySummary).filter_by(
            user_id=user.id
        ).order_by(DailySummary.summary_date.desc()).limit(limit).all()
        return {"data": [_sum(r) for r in rows], "count": len(rows)}
    finally:
        session.close()


@router.post("/summarize")
async def summarize(
    user: User = Depends(get_current_user_any),
    day: Optional[str] = Query(None),
):
    """生成指定日期(默认今天)的经验/教训总结。"""
    if not settings.EXPERIENCE_ENABLED:
        raise HTTPException(400, "经验积累未启用")
    from backend.core.experience import summarize_daily
    result = await summarize_daily(user.id, day)
    return result


@router.get("/injection")
async def preview_injection(
    user: User = Depends(get_current_user_any),
    model: Optional[str] = Query(None),
):
    """预览将注入到下一次调用的经验块。"""
    from backend.core.experience import build_injection_block
    return {"block": build_injection_block(user.id, model or "")}


@router.post("/{exp_id}/feedback")
async def feedback(
    exp_id: int,
    positive: bool = Query(True),
    user: User = Depends(get_current_user_any),
):
    """经验反馈: 👍 有效(positive=true) / 👎 无效(positive=false)。"""
    session = get_session()
    try:
        row = session.query(ExperienceLog).filter_by(
            id=exp_id, user_id=user.id).first()
        if not row:
            raise HTTPException(404, "经验不存在")
        if positive:
            row.pinned = True
        else:
            row.verify_fail_count = (row.verify_fail_count or 0) + 1
            if row.verify_fail_count >= 2:
                session.delete(row)
        session.commit()
        return {"ok": True}
    finally:
        session.close()


@router.delete("/{exp_id}")
async def delete_experience(
    exp_id: int,
    user: User = Depends(get_current_user_any),
):
    """删除一条经验。"""
    session = get_session()
    try:
        row = session.query(ExperienceLog).filter_by(
            id=exp_id, user_id=user.id).first()
        if not row:
            raise HTTPException(404, "经验不存在")
        session.delete(row)
        session.commit()
        return {"ok": True}
    finally:
        session.close()


def _exp(r: ExperienceLog) -> dict:
    return {
        "id": r.id,
        "kind": r.kind,
        "kind_label": _KIND_LABEL.get(r.kind, r.kind),
        "title": r.title,
        "content": r.content,
        "source_model": r.source_model,
        "pinned": r.pinned,
        "verify_fail_count": r.verify_fail_count,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _sum(r: DailySummary) -> dict:
    return {
        "id": r.id,
        "date": r.summary_date,
        "content": r.content,
        "stats": r.daily_stats,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }
