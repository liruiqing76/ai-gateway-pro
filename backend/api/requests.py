# ============================================================
# 智能网关 — 请求记录 API(合并视图)
#
# 把"聊天记录"(chat_logs: 输入输出内容)与"请求链路"(request_traces:
# 路由过程/阶段明细)合并成一个列表。两表靠 request_id 关联, 历史上
# 存在三种形态:
#   a) 两侧都有  → 合并为一行(链路字段 + 内容字段)
#   b) 只有链路  → 探测请求等(无聊天内容)
#   c) 只有聊天  → 历史漏写 request_id 的行(内容可见, 链路缺失)
#
# 实现说明: 不用 SQL LEFT JOIN —— 生产库实测查询计划器对
# request_id 关联选错计划(全表扫大文本溢出页, 单次 60s+)。
# 这里两表各取一次小字段元数据(均走索引, 毫秒级), Python 侧按
# request_id 配对; 大文本预览只对当页行按主键截取。
# ============================================================

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query

from backend.auth.middleware import get_current_user_any
from backend.models import get_session, User

logger = logging.getLogger("gateway.requests")
router = APIRouter(prefix="/api/requests", tags=["requests"])

_TRACE_META = """
    SELECT id, request_id, created_at, original_model, model_name,
           provider_name, total_tokens, latency_ms, success, error_msg,
           request_type, is_pipeline, pipeline_mode, is_stream
    FROM request_traces WHERE user_id = :uid
"""

_CHAT_META = """
    SELECT id, request_id, created_at, original_model, model_name,
           provider_name, total_tokens, total_chars, latency_ms,
           success, error_msg, is_pipeline, pipeline_mode, is_stream
    FROM chat_logs WHERE user_id = :uid
"""


@router.get("")
async def list_requests(
    user: User = Depends(get_current_user_any),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    model: Optional[str] = Query(None),
    success: Optional[bool] = Query(None),
    request_type: Optional[str] = Query(None, description="real / speed_test"),
    pipeline_only: bool = Query(False),
):
    """合并后的请求记录列表(时间倒序)。"""
    session = get_session()
    try:
        t_rows = session.execute(
            __import__("sqlalchemy", fromlist=["text"]).text(_TRACE_META),
            {"uid": user.id},
        ).mappings().all()
        c_rows = session.execute(
            __import__("sqlalchemy", fromlist=["text"]).text(_CHAT_META),
            {"uid": user.id},
        ).mappings().all()

        # request_id -> 聊天行(成对的走合并分支)
        chat_by_req = {}
        orphan_chats = []
        for c in c_rows:
            rid = c["request_id"]
            if rid:
                chat_by_req[rid] = c
            else:
                orphan_chats.append(c)

        merged = []
        paired_chat_ids = set()
        for tr in t_rows:
            c = chat_by_req.get(tr["request_id"])
            has_chat = c is not None
            if has_chat:
                paired_chat_ids.add(c["id"])
                tokens = c["total_tokens"] if c["total_tokens"] is not None \
                    else (tr["total_tokens"] or 0)
            else:
                tokens = tr["total_tokens"] or 0
            merged.append({
                "trace_id": tr["id"],
                "chat_id": c["id"] if has_chat else None,
                "created_at": tr["created_at"],
                "original_model": tr["original_model"],
                "model_name": tr["model_name"] or (c["model_name"] if has_chat else None),
                "provider_name": tr["provider_name"]
                or (c["provider_name"] if has_chat else None),
                "total_tokens": int(tokens or 0),
                "total_chars": int(c["total_chars"] or 0) if has_chat else 0,
                "latency_ms": tr["latency_ms"],
                "success": bool(tr["success"]),
                "error_msg": tr["error_msg"],
                "request_type": tr["request_type"] or "real",
                "is_pipeline": bool(tr["is_pipeline"]),
                "pipeline_mode": tr["pipeline_mode"],
                "is_stream": bool(tr["is_stream"]),
                "has_trace": True,
                "has_chat": has_chat,
            })

        # 孤儿聊天: 无链路对应(request_id 为空或指向不存在的链路)
        for c in orphan_chats:
            if c["request_id"] and c["request_id"] in chat_by_req and c["id"] in paired_chat_ids:
                continue
            merged.append({
                "trace_id": None,
                "chat_id": c["id"],
                "created_at": c["created_at"],
                "original_model": c["original_model"],
                "model_name": c["model_name"],
                "provider_name": c["provider_name"],
                "total_tokens": int(c["total_tokens"] or 0),
                "total_chars": int(c["total_chars"] or 0),
                "latency_ms": c["latency_ms"],
                "success": bool(c["success"]),
                "error_msg": c["error_msg"],
                "request_type": "real",
                "is_pipeline": bool(c["is_pipeline"]),
                "pipeline_mode": c["pipeline_mode"],
                "is_stream": bool(c["is_stream"]),
                "has_trace": False,
                "has_chat": True,
            })

        # 过滤(数据量千级, 内存过滤即可)
        def _match(r):
            if model and model.lower() not in (r["original_model"] or "").lower() \
                    and model.lower() not in (r["model_name"] or "").lower():
                return False
            if success is not None and r["success"] != bool(success):
                return False
            if request_type == "speed_test" and r["request_type"] != "speed_test":
                return False
            if request_type == "real" and r["request_type"] == "speed_test":
                return False
            if pipeline_only and not r["is_pipeline"]:
                return False
            return True

        filtered = [r for r in merged if _match(r)]
        # created_at 为 ISO 字符串, 字典序即时间序
        filtered.sort(key=lambda r: r["created_at"] or "", reverse=True)

        total = len(filtered)
        page = filtered[offset:offset + limit]

        # 预览二次获取: 只对当页聊天行按主键截取前 201 字符
        chat_ids = [r["chat_id"] for r in page if r["chat_id"]]
        previews = {}
        if chat_ids:
            from sqlalchemy import text as sa_text
            ph = ",".join(f":c{i}" for i in range(len(chat_ids)))
            pparams = {f"c{i}": cid for i, cid in enumerate(chat_ids)}
            prow = session.execute(
                sa_text("SELECT id, substr(prompt_text,1,201) FROM chat_logs "
                        "WHERE id IN (" + ph + ")"),
                pparams,
            ).all()
            previews = {r[0]: r[1] for r in prow}
        for r in page:
            pv = previews.get(r.get("chat_id"))
            r["prompt_preview"] = ((pv + "…") if len(pv) > 200 else pv) if pv else None

        return {"total": total, "limit": limit, "offset": offset, "data": page}
    finally:
        session.close()


@router.get("/stats")
async def request_stats(
    user: User = Depends(get_current_user_any),
    days: int = Query(7, ge=1, le=90),
):
    """合并页统计卡: 以 traces 为主口径(含探测), 内容量取自 chat_logs"""
    from datetime import datetime, timedelta
    from sqlalchemy import func
    from backend.models import RequestTrace, ChatLog

    session = get_session()
    try:
        since = datetime.now() - timedelta(days=days)
        base_t = session.query(RequestTrace).filter(
            RequestTrace.user_id == user.id, RequestTrace.created_at >= since)
        base_c = session.query(ChatLog).filter(
            ChatLog.user_id == user.id, ChatLog.created_at >= since)

        total = base_t.count()
        ok = base_t.filter_by(success=True).count()
        probe = base_t.filter(RequestTrace.request_type == "speed_test").count()
        pipeline_n = base_t.filter_by(is_pipeline=True).count()
        sum_tokens = session.query(
            func.coalesce(func.sum(ChatLog.total_tokens), 0)).filter(
            ChatLog.user_id == user.id, ChatLog.created_at >= since).scalar() or 0
        sum_chars = session.query(
            func.coalesce(func.sum(ChatLog.total_chars), 0)).filter(
            ChatLog.user_id == user.id, ChatLog.created_at >= since).scalar() or 0

        return {
            "period_days": days,
            "total_requests": total,
            "success_rate": round(ok / total * 100, 1) if total else 0.0,
            "probe_count": probe,
            "pipeline_count": pipeline_n,
            "sum_total_tokens": int(sum_tokens),
            "sum_total_chars": int(sum_chars),
        }
    finally:
        session.close()
