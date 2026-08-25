# ============================================================
# 智能网关 — 用量读取 API
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
    """聚合统计: 总调用数 / 今日调用数 / 今日 token / 失败数。

    失败数从 chat_logs 统计(与"聊天记录"菜单同口径): usage_logs 只记成功
    调用, 旧实现这里恒为 0, 用户对比两个菜单时永远"对不上"。"""
    session = get_session()
    try:
        from backend.models import ChatLog
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
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
            session.query(func.count(ChatLog.id))
            .filter(ChatLog.user_id == user.id, ChatLog.success == False)  # noqa: E712
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


@router.get("/monthly")
async def usage_monthly(months: int = 12, user: User = Depends(get_current_user_from_jwt)):
    """按自然月聚合最近 N 个月的调用数与 token 总量(仪表盘月视图)。
    返回从本月往前 months 个月的连续序列, 无数据月份补零。"""
    months = max(1, min(int(months), 24))
    now = datetime.now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for _ in range(months - 1):
        start = (start - timedelta(days=1)).replace(day=1)
    session = get_session()
    try:
        from backend.models import ChatLog
        month_expr = func.strftime("%Y-%m", UsageLog.created_at)
        rows = (
            session.query(
                month_expr.label("m"),
                func.count(UsageLog.id),
                func.coalesce(func.sum(UsageLog.total_tokens), 0),
            )
            .filter(UsageLog.user_id == user.id, UsageLog.created_at >= start)
            .group_by(month_expr)
            .all()
        )
        by_month = {r[0]: (int(r[1]), int(r[2])) for r in rows}
        chat_expr = func.strftime("%Y-%m", ChatLog.created_at)
        char_rows = (
            session.query(
                chat_expr.label("m"),
                func.coalesce(func.sum(ChatLog.total_chars), 0),
            )
            .filter(ChatLog.user_id == user.id, ChatLog.created_at >= start)
            .group_by(chat_expr)
            .all()
        )
        chars_by_month = {r[0]: int(r[1] or 0) for r in char_rows}
        data = []
        cur = start
        for _ in range(months):
            key = cur.strftime("%Y-%m")
            calls, tokens = by_month.get(key, (0, 0))
            data.append({"month": key, "calls": calls, "tokens": tokens,
                         "chars": chars_by_month.get(key, 0)})
            cur = (cur + timedelta(days=32)).replace(day=1)
        return {"data": data}
    finally:
        session.close()


@router.get("/daily")
async def usage_daily(days: int = 30, user: User = Depends(get_current_user_from_jwt)):
    """按自然日聚合最近 N 天的调用数与 token 总量(用于仪表盘日历/柱状趋势)。
    返回从今天往前 days 天的连续序列, 无数据的日子补零。
    chars 来自 chat_logs(与聊天记录菜单同口径): 字符与 token 是两种单位,
    代码/英文场景字符可达 token 的 5~6 倍, 仪表盘同时给出避免跨页误读。"""
    days = max(7, min(int(days), 90))
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = today_start - timedelta(days=days - 1)
    session = get_session()
    try:
        from backend.models import ChatLog
        rows = (
            session.query(
                func.date(UsageLog.created_at).label("d"),
                func.count(UsageLog.id),
                func.coalesce(func.sum(UsageLog.total_tokens), 0),
            )
            .filter(UsageLog.user_id == user.id, UsageLog.created_at >= start)
            .group_by(func.date(UsageLog.created_at))
            .all()
        )
        by_date = {str(r[0]): (int(r[1]), int(r[2])) for r in rows}
        char_rows = (
            session.query(
                func.date(ChatLog.created_at).label("d"),
                func.coalesce(func.sum(ChatLog.total_chars), 0),
            )
            .filter(ChatLog.user_id == user.id, ChatLog.created_at >= start)
            .group_by(func.date(ChatLog.created_at))
            .all()
        )
        chars_by_date = {str(r[0]): int(r[1] or 0) for r in char_rows}
        data = []
        for i in range(days):
            d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
            calls, tokens = by_date.get(d, (0, 0))
            data.append({"date": d, "calls": calls, "tokens": tokens,
                         "chars": chars_by_date.get(d, 0)})
        return {"data": data}
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
