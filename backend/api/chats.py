# ============================================================
# 智能网关 — 聊天记录 API
# 列表/详情/统计/清理: 每次请求的输入输出全文与长度
# ============================================================

import logging
from typing import Optional
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy import func

from backend.auth.middleware import get_current_user_any
from backend.models import get_session, ChatLog

logger = logging.getLogger("gateway.chats")
router = APIRouter(prefix="/api/chats", tags=["chats"])


@router.get("")
async def list_chats(
    user=Depends(get_current_user_any),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    model: Optional[str] = Query(None),
    success: Optional[bool] = Query(None),
    pipeline_only: bool = Query(False),
):
    """聊天记录列表(时间倒序). 列表只返回摘要不返回全文, 避免响应过大。"""
    session = get_session()
    try:
        q = session.query(ChatLog).filter_by(user_id=user.id)
        if model:
            q = q.filter(
                ChatLog.model_name.ilike(f"%{model}%") |
                ChatLog.original_model.ilike(f"%{model}%")
            )
        if success is not None:
            q = q.filter_by(success=success)
        if pipeline_only:
            q = q.filter_by(is_pipeline=True)

        total = q.count()
        rows = q.order_by(ChatLog.created_at.desc()).offset(offset).limit(limit).all()
        return {
            "total": total, "limit": limit, "offset": offset,
            "data": [_row_summary(r) for r in rows],
        }
    finally:
        session.close()


@router.get("/stats")
async def chat_stats(
    user=Depends(get_current_user_any),
    days: int = Query(7, ge=1, le=90),
):
    """聊天记录统计: 总对话数 / 平均字符长度 / 按模型分组。"""
    session = get_session()
    try:
        since = datetime.now() - timedelta(days=days)
        base = session.query(ChatLog).filter(
            ChatLog.user_id == user.id,
            ChatLog.created_at >= since,
        )
        total = base.count()
        success_count = base.filter_by(success=True).count()
        fail_count = total - success_count
        pipeline_count = base.filter_by(is_pipeline=True).count()

        avg_prompt = session.query(func.avg(ChatLog.prompt_chars)).filter(
            ChatLog.user_id == user.id, ChatLog.created_at >= since,
        ).scalar() or 0
        avg_completion = session.query(func.avg(ChatLog.completion_chars)).filter(
            ChatLog.user_id == user.id, ChatLog.created_at >= since, ChatLog.success == True,  # noqa: E712
        ).scalar() or 0
        avg_total = session.query(func.avg(ChatLog.total_chars)).filter(
            ChatLog.user_id == user.id, ChatLog.created_at >= since,
        ).scalar() or 0

        # 按模型分组
        model_stats = session.query(
            ChatLog.model_name,
            func.count(ChatLog.id).label("count"),
            func.avg(ChatLog.total_chars).label("avg_total_chars"),
        ).filter(
            ChatLog.user_id == user.id,
            ChatLog.created_at >= since,
            ChatLog.model_name.isnot(None),
        ).group_by(ChatLog.model_name).order_by(
            func.count(ChatLog.id).desc()
        ).limit(10).all()

        # 总字符数(用于"累计IO长度")
        sum_total_chars = session.query(
            func.coalesce(func.sum(ChatLog.total_chars), 0)
        ).filter(ChatLog.user_id == user.id, ChatLog.created_at >= since).scalar() or 0

        return {
            "period_days": days,
            "total_chats": total,
            "success_count": success_count,
            "fail_count": fail_count,
            "success_rate": round(success_count / total * 100, 1) if total > 0 else 0,
            "pipeline_count": pipeline_count,
            "avg_prompt_chars": round(avg_prompt),
            "avg_completion_chars": round(avg_completion),
            "avg_total_chars": round(avg_total),
            "sum_total_chars": int(sum_total_chars),
            "top_models": [
                {"model": m[0], "count": m[1],
                 "avg_total_chars": round(m[2]) if m[2] else 0}
                for m in model_stats
            ],
        }
    finally:
        session.close()


@router.get("/{chat_id}")
async def get_chat(
    chat_id: int,
    user=Depends(get_current_user_any),
):
    """单条聊天记录详情(含 prompt/completion 全文)"""
    session = get_session()
    try:
        row = session.query(ChatLog).filter_by(
            id=chat_id, user_id=user.id
        ).first()
        if not row:
            raise HTTPException(404, "记录不存在")
        return _row_detail(row)
    finally:
        session.close()


@router.delete("/cleanup")
async def cleanup_chats(
    user=Depends(get_current_user_any),
    days: int = Query(30, ge=1, le=365),
):
    """清理旧聊天记录"""
    session = get_session()
    try:
        cutoff = datetime.now() - timedelta(days=days)
        deleted = session.query(ChatLog).filter(
            ChatLog.user_id == user.id,
            ChatLog.created_at < cutoff,
        ).delete()
        session.commit()
        return {"deleted": deleted, "kept_days": days}
    except Exception as e:
        session.rollback()
        raise HTTPException(500, f"清理失败: {e}")
    finally:
        session.close()


# ─────────────────────────────────────────────
# 序列化
# ─────────────────────────────────────────────

def _row_summary(r: ChatLog) -> dict:
    """列表摘要: prompt 截断前 200 字"""
    p = r.prompt_text or ""
    return {
        "id": r.id,
        "request_id": r.request_id,
        "original_model": r.original_model,
        "provider_name": r.provider_name,
        "model_name": r.model_name,
        "is_stream": r.is_stream,
        "is_pipeline": r.is_pipeline,
        "pipeline_mode": r.pipeline_mode,
        "prompt_preview": (p[:200] + "…") if len(p) > 200 else p,
        "prompt_chars": r.prompt_chars,
        "completion_chars": r.completion_chars,
        "total_chars": r.total_chars,
        "total_tokens": r.total_tokens,
        "prompt_tokens": r.prompt_tokens,
        "completion_tokens": r.completion_tokens,
        "latency_ms": r.latency_ms,
        "success": r.success,
        "error_msg": r.error_msg,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _row_detail(r: ChatLog) -> dict:
    """详情: 含全文"""
    d = _row_summary(r)
    d["prompt_text"] = r.prompt_text
    d["completion_text"] = r.completion_text
    return d
