# ============================================================
# 智能网关 — 用量读取 API
# 修复: UsageLog 之前只写不读, 前端用量明细表永远为空
# 这里提供读取接口并接通 Dashboard 明细表
# ============================================================

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, func

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
    """聚合统计: 总调用数 / 总 token / 今日调用数 / 今日 token / 失败数 /
    今日字符 / 总字符。

    失败数从 chat_logs 统计(与"聊天记录"菜单同口径): usage_logs 只记成功
    调用, 旧实现这里恒为 0, 用户对比两个菜单时永远"对不上"。
    字符数同样来自 chat_logs.total_chars(与 daily/monthly 的 chars 同口径),
    供仪表盘"Token + 字符数"双口径主卡展示。"""
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
        total_tokens = (
            session.query(func.coalesce(func.sum(UsageLog.total_tokens), 0))
            .filter(UsageLog.user_id == user.id)
            .scalar() or 0
        )
        failed = (
            session.query(func.count(ChatLog.id))
            .filter(ChatLog.user_id == user.id, ChatLog.success == False)  # noqa: E712
            .scalar() or 0
        )
        today_chars = (
            session.query(func.coalesce(func.sum(ChatLog.total_chars), 0))
            .filter(ChatLog.user_id == user.id, ChatLog.created_at >= today_start)
            .scalar() or 0
        )
        total_chars = (
            session.query(func.coalesce(func.sum(ChatLog.total_chars), 0))
            .filter(ChatLog.user_id == user.id)
            .scalar() or 0
        )
        return {
            "total": int(total),
            "today": int(today),
            "today_tokens": int(today_tokens),
            "total_tokens": int(total_tokens),
            "failed": int(failed),
            "today_chars": int(today_chars),
            "total_chars": int(total_chars),
        }
    finally:
        session.close()


@router.get("/reliability")
async def usage_reliability(
    min_calls: int = 10,
    limit: int = 5,
    user: User = Depends(get_current_user_from_jwt),
):
    """渠道 / 模型可靠性排行 — 回答"哪个渠道/模型最可靠"。

    口径(与仪表盘设计稿一致):
    - 数据源 request_traces: 只有它同时记录 success / latency_ms /
      request_type, usage_logs 只写成功调用(成功率恒 100%), 不具备
      可靠性意义
    - 只统计真实请求: request_type='real' 或 NULL, 排除 speed_test
      速度探测(后台自动测速的偶发失败不该拉低渠道成功率)
    - **尝试级统计**: 网关有故障转移, 整条 trace 几乎总是"成功"
      (失败会被重试兜底), 只看 trace 级成功率恒为 100% 没有区分度。
      这里把 stages_detail 里的每次上游尝试(含失败重试)都计入,
      成功率 = 成功尝试 / 总尝试, 才能看出"哪个渠道经常失败被跳过"
    - 样本量(尝试次数) < min_calls 的渠道/模型不进榜, 把低频小模型
      挡在榜外
    """
    from backend.models import RequestTrace
    min_calls = max(1, min(int(min_calls), 1000))
    limit = max(1, min(int(limit), 20))
    session = get_session()
    try:
        real_filter = (
            (RequestTrace.user_id == user.id)
            & ((RequestTrace.request_type == "real")
               | (RequestTrace.request_type.is_(None)))
        )
        probes = (
            session.query(func.count(RequestTrace.id))
            .filter(RequestTrace.user_id == user.id,
                    RequestTrace.request_type == "speed_test")
            .scalar() or 0
        )
        rows = (
            session.query(
                RequestTrace.provider_name,
                RequestTrace.model_name,
                RequestTrace.success,
                RequestTrace.latency_ms,
                RequestTrace.total_tokens,
                RequestTrace.stages_detail,
            )
            .filter(real_filter)
            .all()
        )

        # 尝试级聚合: {name: {attempts, ok, lat_sum, lat_n, tokens}}
        def _bucket():
            return {"attempts": 0, "ok": 0, "lat_sum": 0, "lat_n": 0, "tokens": 0}

        prov_stats, model_stats = {}, {}

        def _acc(stats, name, ok, lat):
            if not name:
                return
            b = stats.setdefault(name, _bucket())
            b["attempts"] += 1
            if ok:
                b["ok"] += 1
                if lat:
                    b["lat_sum"] += int(lat)
                    b["lat_n"] += 1

        for prov, model, success, lat, tok, stages in rows:
            # trace 级(整体结果 + token 只记一次, 不随重试翻倍)
            _acc(prov_stats, prov, bool(success), lat if success else 0)
            _acc(model_stats, model, bool(success), lat if success else 0)
            if success and prov:
                prov_stats[prov]["tokens"] += int(tok or 0)
            if success and model:
                model_stats[model]["tokens"] += int(tok or 0)
            # 阶段级: 故障转移中的每次上游尝试(失败重试是可靠性的核心信号)
            for st in stages or []:
                if not isinstance(st, dict):
                    continue
                _acc(prov_stats, st.get("provider"), bool(st.get("success")),
                     st.get("latency_ms") if st.get("success") else 0)
                _acc(model_stats, st.get("model"), bool(st.get("success")),
                     st.get("latency_ms") if st.get("success") else 0)

        def _rank(stats):
            items = []
            for name, b in stats.items():
                if b["attempts"] < min_calls:
                    continue
                items.append({
                    "name": name,
                    "calls": b["attempts"],
                    "success": b["ok"],
                    "success_rate": round(b["ok"] / b["attempts"] * 100, 1),
                    "avg_latency_ms": int(b["lat_sum"] / b["lat_n"]) if b["lat_n"] else None,
                    "tokens": b["tokens"],
                })
            # 先按成功率、再按调用量排序: 同样可靠时高频渠道更可信
            items.sort(key=lambda x: (-x["success_rate"], -x["calls"]))
            return items[:limit]

        return {
            "providers": _rank(prov_stats),
            "models": _rank(model_stats),
            "excluded_probes": int(probes),
            "min_calls": min_calls,
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
async def usage_daily(days: int = 30, month: str | None = None,
                      user: User = Depends(get_current_user_from_jwt)):
    """按自然日聚合调用数与 token 总量。两种口径:
    - 默认: 最近 days 天(从今天往前, 仪表盘趋势)
    - month="YYYY-MM": 指定自然月的每一天(日历翻页), 与 days 互斥
    返回连续日期序列, 无数据的日子补零。chars 来自 chat_logs(与聊天
    记录菜单同口径): 字符与 token 是两种单位, 代码/英文场景字符可达
    token 的 5~6 倍, 仪表盘同时给出避免跨页误读。"""
    session = get_session()
    try:
        from backend.models import ChatLog
        if month:
            try:
                first = datetime.strptime(month, "%Y-%m").replace(
                    hour=0, minute=0, second=0, microsecond=0)
            except ValueError:
                raise HTTPException(status_code=400, detail="month 格式须为 YYYY-MM")
            if first > datetime.now().replace(hour=0, minute=0, second=0, microsecond=0):
                raise HTTPException(status_code=400, detail="不能查询未来月份")
            if first.year < 2023:
                raise HTTPException(status_code=400, detail="时间范围非法")
            last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
            start = first
            num_days = (last - first).days + 1
        else:
            days = max(7, min(int(days), 90))
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            start = today_start - timedelta(days=days - 1)
            last = today_start
            num_days = days
        rows = (
            session.query(
                func.date(UsageLog.created_at).label("d"),
                func.count(UsageLog.id),
                func.coalesce(func.sum(UsageLog.total_tokens), 0),
            )
            .filter(UsageLog.user_id == user.id, UsageLog.created_at >= start,
                    UsageLog.created_at <= last + timedelta(days=1))
            .group_by(func.date(UsageLog.created_at))
            .all()
        )
        by_date = {str(r[0]): (int(r[1]), int(r[2])) for r in rows}
        char_rows = (
            session.query(
                func.date(ChatLog.created_at).label("d"),
                func.coalesce(func.sum(ChatLog.total_chars), 0),
            )
            .filter(ChatLog.user_id == user.id, ChatLog.created_at >= start,
                    ChatLog.created_at <= last + timedelta(days=1))
            .group_by(func.date(ChatLog.created_at))
            .all()
        )
        chars_by_date = {str(r[0]): int(r[1] or 0) for r in char_rows}
        data = []
        for i in range(num_days):
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
