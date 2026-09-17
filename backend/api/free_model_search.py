#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""免费模型帖子搜索 API — 查看报告列表/详情, 手动触发搜索, 查看统计。"""
import logging
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from backend.auth.routes import get_current_user_from_jwt
from backend.models import User
from backend.core.free_model_search import (
    run_free_model_search,
    runtime_state,
    REPORTS_DIR,
    mark_post_as_ignored,
    unmark_post_as_ignored,
    load_ignored_hashes,
    make_ignore_check,
    generate_report_content,
    save_report_file,
    is_post_fresh,
    MAX_POST_AGE_DAYS,
)

logger = logging.getLogger("gateway")

router = APIRouter(prefix="/api/free-model-search", tags=["free-model-search"])


@router.get("/reports")
async def list_reports(
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(get_current_user_from_jwt),
):
    """返回历史报告列表(按日期倒序, 只保留最近7天内的报告)。"""
    from backend.models import get_session, FreeModelReport
    session = get_session()
    try:
        # 时效过滤: 超过 7 天(MAX_POST_AGE_DAYS)的报告不再返回
        cutoff_str = (datetime.now() - timedelta(days=MAX_POST_AGE_DAYS)).strftime("%Y-%m-%d")
        reports = (
            session.query(FreeModelReport)
            .filter(FreeModelReport.run_date >= cutoff_str)
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
        
        # 帖子详情(过滤已忽略的 + 超过7天时效的帖子)
        post_ids = list(report.post_ids or [])
        posts_by_id = {
            p.id: p for p in session.query(FreeModelPost).filter(FreeModelPost.id.in_(post_ids)).all()
        }
        ignore = make_ignore_check(session)
        posts = []
        for pid in post_ids:
            p = posts_by_id.get(pid)
            if p and is_post_fresh(p) and not ignore(p.hash_key, p.title):
                posts.append({
                    "id": p.id,
                    "hash_key": p.hash_key,  # 32位sha256, 前端"忽略"按钮依赖它
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
    """返回某日期报告的 Markdown 文件（每次按数据库实时重建，剔除已忽略帖子）。

    旧版本把报告文件写入 onefile 临时解压目录(_MEIxxx), 重启即丢且与新的
    持久化 REPORTS_DIR 不匹配 → 这里始终从数据库重新生成 Markdown 并落盘
    (持久目录), 保证下载/预览的内容与"帖子列表"一致: 被忽略的帖子不再出现。
    """
    from backend.models import get_session, FreeModelReport, FreeModelPost, FreeModelEvaluation
    new_path = None
    session = get_session()
    try:
        report = session.query(FreeModelReport).filter_by(run_date=date).first()
        if not report:
            raise HTTPException(404, "报告不存在")
        # 剔除已忽略的帖子（精确 hash 或标题签名命中），再重建 Markdown
        post_ids = list(report.post_ids or [])
        posts_by_id = {
            p.id: p for p in session.query(FreeModelPost).filter(FreeModelPost.id.in_(post_ids)).all()
        }
        posts = []
        ignore = make_ignore_check(session)
        for pid in post_ids:
            p = posts_by_id.get(pid)
            if p and is_post_fresh(p) and not ignore(p.hash_key, p.title):
                posts.append({
                    "id": p.id,
                    "source": p.source,
                    "url": p.url,
                    "title": p.title,
                    "author": p.author,
                    "published_at": p.published_at.isoformat() if p.published_at else None,
                    "content_snippet": p.content_snippet,
                })
        evaluations = []
        for e in session.query(FreeModelEvaluation).filter_by(report_id=report.id).all():
            evaluations.append({
                "post_id": e.post_id,
                "feasibility_score": e.feasibility_score,
                "truthfulness_score": e.truthfulness_score,
                "reason": e.reason,
            })
        content = generate_report_content(date, posts, evaluations)
        # 用 save_report_file 返回的实际落盘路径, 避免依赖模块导入时的 REPORTS_DIR 绑定
        new_path = save_report_file(date, content)
        report.file_path = new_path
        session.commit()
        logger.info("报告文件按需重建: %s -> %s", date, new_path)
    finally:
        session.close()
    if not new_path or not Path(new_path).exists():
        raise HTTPException(404, "报告文件不存在")
    return FileResponse(new_path, media_type="text/markdown", filename=f"free_model_report_{date}.md")


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
    """返回搜索运行的统计与状态 (内存态)。"""
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


@router.post("/post/{post_hash}/ignore")
async def ignore_post(
    post_hash: str,
    user: User = Depends(get_current_user_from_jwt),
):
    """手动标记某条帖子为忽略（不再出现在搜索结果中）。"""
    if not post_hash or len(post_hash) != 32:
        raise HTTPException(400, "无效的帖子哈希")
    
    # 从数据库取出帖子标题，一并记录标题签名：
    # 同一篇文章下次搜索带着不同的 URL/token 回来时也能识别为忽略
    from backend.models import get_session, FreeModelPost
    session = get_session()
    title = ""
    try:
        p = session.query(FreeModelPost).filter_by(hash_key=post_hash).first()
        if p:
            title = p.title
    finally:
        session.close()

    result = mark_post_as_ignored(post_hash, title)
    return {"ok": True, "ignored": result}


@router.delete("/post/{post_hash}/ignore")
async def unignore_post(
    post_hash: str,
    user: User = Depends(get_current_user_from_jwt),
):
    """取消忽略某条帖子。"""
    if not post_hash or len(post_hash) != 32:
        raise HTTPException(400, "无效的帖子哈希")
    
    from backend.models import get_session, FreeModelPost
    session = get_session()
    title = ""
    try:
        p = session.query(FreeModelPost).filter_by(hash_key=post_hash).first()
        if p:
            title = p.title
    finally:
        session.close()

    result = unmark_post_as_ignored(post_hash, title)
    return {"ok": True, "unignored": result}


@router.get("/ignored-posts")
async def list_ignored_posts(
    user: User = Depends(get_current_user_from_jwt),
):
    """获取当前所有被忽略的帖子哈希列表。"""
    hashes = load_ignored_hashes()
    return {"ok": True, "data": list(hashes), "total": len(hashes)}
