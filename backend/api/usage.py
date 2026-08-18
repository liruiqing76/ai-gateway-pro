# ============================================================
# AI 模型网关 Pro — 用量读取 API
# 修复: UsageLog 之前只写不读, 前端用量明细表永远为空
# 这里提供读取接口并接通 Dashboard 明细表
# ============================================================

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func

from backend.auth.middleware import get_current_user_from_jwt
from backend.models import get_session, User, UsageLog

router = APIRouter(prefix="/api/usage", tags=["usage"])


def _serialize(row: UsageLog) -> dict:
    """把一行 UsageLog 转成前端明细表所需的字段"""
    return {
        "id": row.id,
        "time": row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else "",
        "provider": row.provider_name or "-",
        "model": row.model_name or "-",
        "tokens": int(row.total_tokens or 0),
        "prompt_tokens": int(row.prompt_tokens or 0),
        "completion_tokens": int(row.completion_tokens or 0),
        "cost": float(row.cost or 0),
        "latency_ms": int(row.latency_ms) if row.latency_ms is not None else None,
        "success": bool(row.success),
        "error_msg": row.error_msg,
    }


@router.get("/summary")
async def usage_summary(user: User = Depends(get_current_user_from_jwt)):
    """聚合统计: 总调用数 / 今日调用数 / 今日 token / 失败数"""
    session = get_session()
    try:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        total = session.query(func.count(UsageLog.id)).filter_by(user_id=user.id).scalar() or 0
        today = (
            session.query(func.count(UsageLog.id))
            .filter(UsageLog.user_id == user.id, UsageLog.created_at >= today_start)
            .scalar() or 0
        )
        today_tokens = (
            session.query(func.coalesce(func.sum(UsageLog.total_tokens), 0))
            .filter(UsageLog.user_id == user.id, UsageLog.created_at >= today_start)
            .scalar() or 0
        )
        failed = (
            session.query(func.count(UsageLog.id))
            .filter(UsageLog.user_id == user.id, UsageLog.success == False)  # noqa: E712
            .scalar() or 0
        )
        return {
            "total": int(total),
            "today": int(today),
            "today_tokens": int(today_tokens),
            "failed": int(failed),
        }
    finally:
        session.close()


@router.get("")
async def list_usage(
    limit: int = 50,
    user: User = Depends(get_current_user_from_jwt),
):
    """最近用量明细(按时间倒序), limit 1~500"""
    limit = max(1, min(int(limit), 500))
    session = get_session()
    try:
        rows = (
            session.query(UsageLog)
            .filter_by(user_id=user.id)
            .order_by(UsageLog.created_at.desc())
            .limit(limit)
            .all()
        )
        data = [_serialize(r) for r in rows]
        return {"data": data, "count": len(data)}
    finally:
        session.close()
