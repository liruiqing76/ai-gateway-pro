# ============================================================
# 智能网关 — 请求链路日志 API
# ============================================================

import logging
from typing import Optional
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from backend.auth.middleware import get_current_user_any
from backend.models import get_session, User, RequestTrace

logger = logging.getLogger("gateway.traces")
router = APIRouter(prefix="/api/traces", tags=["traces"])


@router.get("")
async def list_traces(
    user: User = Depends(get_current_user_any),
    limit: int = Query(50, ge=1, le=200, description="返回条数"),
    offset: int = Query(0, ge=0, description="偏移量"),
    model: Optional[str] = Query(None, description="按模型名筛选"),
    group: Optional[str] = Query(None, description="按路由组/请求的 model 字段筛选"),
    success: Optional[bool] = Query(None, description="按成功/失败筛选"),
    pipeline_only: bool = Query(False, description="只看编排请求"),
    request_type: Optional[str] = Query(None, description="请求类型: real 真实 / speed_test 探测, 不传=全部"),
):
    """查询请求链路日志"""
    session = get_session()
    try:
        query = session.query(RequestTrace).filter_by(user_id=user.id)

        if model:
            query = query.filter(
                RequestTrace.model_name.ilike(f"%{model}%") |
                RequestTrace.original_model.ilike(f"%{model}%")
            )
        if group:
            # 组名可能出现在原始 model(组名直调)或解析目标里
            query = query.filter(
                (RequestTrace.original_model == group) |
                (RequestTrace.resolved_model == group)
            )
        if success is not None:
            query = query.filter_by(success=success)
        if pipeline_only:
            query = query.filter_by(is_pipeline=True)
        if request_type == "real":
            # 兼容历史数据: 旧记录无该列值(NULL), 视为真实请求
            query = query.filter(
                (RequestTrace.request_type == "real") |
                (RequestTrace.request_type.is_(None))
            )
        elif request_type == "speed_test":
            query = query.filter(RequestTrace.request_type == "speed_test")

        total = query.count()
        records = query.order_by(RequestTrace.created_at.desc()).offset(offset).limit(limit).all()

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "data": [_record_to_dict(r) for r in records],
        }
    finally:
        session.close()


@router.get("/stats")
async def trace_stats(
    user: User = Depends(get_current_user_any),
    days: int = Query(7, ge=1, le=90, description="统计天数"),
):
    """请求链路统计概览"""
    session = get_session()
    try:
        since = datetime.now() - timedelta(days=days)
        base = session.query(RequestTrace).filter(
            RequestTrace.user_id == user.id,
            RequestTrace.created_at >= since,
        )

        total = base.count()
        success_count = base.filter_by(success=True).count()
        fail_count = total - success_count
        pipeline_count = base.filter_by(is_pipeline=True).count()
        probe_count = base.filter_by(request_type="speed_test").count()
        cb_hit = base.filter_by(circuit_breaker_hit=True).count()
        rl_hit = base.filter_by(rate_limit_hit=True).count()

        # 平均延迟
        from sqlalchemy import func
        avg_latency = session.query(func.avg(RequestTrace.latency_ms)).filter(
            RequestTrace.user_id == user.id,
            RequestTrace.created_at >= since,
            RequestTrace.success == True,
        ).scalar() or 0

        # 按模型分组统计
        model_stats = session.query(
            RequestTrace.model_name,
            func.count(RequestTrace.id).label("count"),
            func.avg(RequestTrace.latency_ms).label("avg_latency"),
        ).filter(
            RequestTrace.user_id == user.id,
            RequestTrace.created_at >= since,
            RequestTrace.success == True,
            RequestTrace.model_name.isnot(None),
        ).group_by(RequestTrace.model_name).order_by(func.count(RequestTrace.id).desc()).limit(10).all()

        return {
            "period_days": days,
            "total_requests": total,
            "success_count": success_count,
            "fail_count": fail_count,
            "success_rate": round(success_count / total * 100, 1) if total > 0 else 0,
            "pipeline_count": pipeline_count,
            "probe_count": probe_count,
            "circuit_breaker_hits": cb_hit,
            "rate_limit_hits": rl_hit,
            "avg_latency_ms": round(avg_latency),
            "top_models": [
                {
                    "model": m[0],
                    "count": m[1],
                    "avg_latency_ms": round(m[2]) if m[2] else 0,
                }
                for m in model_stats
            ],
        }
    finally:
        session.close()


@router.delete("/cleanup")
async def cleanup_traces(
    user: User = Depends(get_current_user_any),
    days: int = Query(30, ge=1, le=365, description="保留天数"),
):
    """清理旧的链路日志"""
    session = get_session()
    try:
        cutoff = datetime.now() - timedelta(days=days)
        deleted = session.query(RequestTrace).filter(
            RequestTrace.user_id == user.id,
            RequestTrace.created_at < cutoff,
        ).delete()
        session.commit()
        return {"deleted": deleted, "kept_days": days}
    except Exception as e:
        session.rollback()
        raise HTTPException(500, f"清理失败: {e}")
    finally:
        session.close()


def _record_to_dict(r: RequestTrace) -> dict:
    return {
        "id": r.id,
        "request_id": r.request_id,
        "original_model": r.original_model,
        "resolved_model": r.resolved_model,
        "is_stream": r.is_stream,
        "is_pipeline": r.is_pipeline,
        "pipeline_mode": r.pipeline_mode,
        "request_type": r.request_type or "real",
        "provider_name": r.provider_name,
        "model_name": r.model_name,
        "selection_strategy": r.selection_strategy,
        "selection_reason": r.selection_reason,
        "circuit_breaker_hit": r.circuit_breaker_hit,
        "rate_limit_hit": r.rate_limit_hit,
        "cooldown_seconds": r.cooldown_seconds,
        "latency_ms": r.latency_ms,
        "success": r.success,
        "error_msg": r.error_msg,
        "prompt_tokens": r.prompt_tokens,
        "completion_tokens": r.completion_tokens,
        "total_tokens": r.total_tokens,
        "stages_detail": r.stages_detail,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }
