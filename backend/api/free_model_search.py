#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""免费模型帖子搜索 API — 查看报告列表/详情, 手动触发搜索, 查看统计。"""
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from backend.auth.routes import get_current_user_from_jwt
from backend.models import User
from backend.core.free_model_search import (
    run_free_model_search,
    runtime_state,
    REPORTS_DIR,
)

logger = logging.getLogger("gateway")

router = APIRouter(prefix="/api/free-model-search", tags=["free-model-search"])


@router.get("/reports")
async def list_reports(
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(get_current_user_from_jwt),
):
    """返回历史报告列表(按日期倒序)。"""
    from backend.models import get_session, FreeModelReport
    session = get_session()
    try:
        reports = (
            session.query(FreeModelReport)
            .order_by(FreeModelReport.run_date.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )
        data = []
        for r in reports:
            data.append({
                "id": r.id,
                "run_date": r.run_date,
                "post_count": len(r.post_ids) if r.post_ids else 0,
                "summary": r.summary,
                "feasibility_score": r.feasibility_score,
                "truthfulness_score": r.truthfulness_score,
                "status": r.status,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "file_path": r.file_path,
            })
        return {"ok": True, "data": data, "total": len(data)}
    finally:
        session.close()


@router.get("/report/{date}")
async def get_report_detail(
    date: str,
    user: User = Depends(get_current_user_from_jwt),
):
    """返回某日期报告的详情(含帖子列表与评估)。"""
    from backend.models import get_session, FreeModelReport, FreeModelPost, FreeModelEvaluation
    session = get_session()
    try:
        report = session.query(FreeModelReport).filter_by(run_date=date).first()
        if not report:
            raise HTTPException(404, "报告不存在")
        
        # 帖子详情
        posts = []
        for pid in (report.post_ids or []):
            p = session.query(FreeModelPost).filter_by(id=pid).first()
            if p:
                posts.append({
                    "id": p.id,
                    "source": p.source,
                    "url": p.url,
                    "title": p.title,
                    "author": p.author,
                    "published_at": p.published_at.isoformat() if p.published_at else None,
                    "content_snippet": p.content_snippet,
                })
        
        # 评估详情
        evaluations = []
        for e in session.query(FreeModelEvaluation).filter_by(report_id=report.id).all():
            evaluations.append({
                "post_id": e.post_id,
                "feasibility_score": e.feasibility_score,
                "truthfulness_score": e.truthfulness_score,
                "reason": e.reason,
            })
        
        return {
            "ok": True,
            "report": {
                "id": report.id,
                "run_date": report.run_date,
                "summary": report.summary,
                "feasibility_score": report.feasibility_score,
                "truthfulness_score": report.truthfulness_score,
                "status": report.status,
                "finished_at": report.finished_at.isoformat() if report.finished_at else None,
                "file_path": report.file_path,
            },
            "posts": posts,
            "evaluations": evaluations,
        }
    finally:
        session.close()


@router.get("/report/{date}/file")
async def get_report_file(
    date: str,
    user: User = Depends(get_current_user_from_jwt),
):
    """返回某日期报告的 Markdown 文件。"""
    filepath = REPORTS_DIR / f"{date}.md"
    if not filepath.exists():
        raise HTTPException(404, "报告文件不存在")
    return FileResponse(str(filepath), media_type="text/markdown", filename=f"free_model_report_{date}.md")


@router.post("/search")
async def trigger_search(
    user: User = Depends(get_current_user_from_jwt),
):
    """手动触发一次搜索(与定时任务同一函数, 放后台线程避免阻塞)。"""
    import asyncio
    from backend.config import settings
    try:
        # 放入线程池执行
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: run_free_model_search(
                None,
                settings.FREE_MODEL_SEARCH_SOURCES,
                settings.FREE_MODEL_SEARCH_PER_SOURCE_LIMIT,
                settings.FREE_MODEL_EVALUATION_ENABLED,
            )
        )
        return result
    except Exception as e:
        logger.exception("手动触发免费模型搜索失败")
        return {"ok": False, "error": str(e)}


@router.get("/stats")
async def get_stats(
    user: User = Depends(get_current_user_from_jwt),
):
    """返回搜索运行的统计与状态(内存态)。"""
    from backend.models import get_session, FreeModelPost, FreeModelReport
    session = get_session()
    try:
        total_posts = session.query(FreeModelPost).count()
        total_reports = session.query(FreeModelReport).count()
        # 各来源帖子数
        from sqlalchemy import func
        by_source = (
            session.query(FreeModelPost.source, func.count(FreeModelPost.id))
            .group_by(FreeModelPost.source)
            .all()
        )
        source_stats = {s: c for s, c in by_source}
        return {
            "ok": True,
            "total_posts": total_posts,
            "total_reports": total_reports,
            "by_source": source_stats,
            "runtime": runtime_state(),
        }
    finally:
        session.close()
