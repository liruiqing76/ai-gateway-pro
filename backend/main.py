# ============================================================
# 智能网关 — 主应用入口
# ============================================================

import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# 确保 backend 可导入
sys.path.insert(0, str(Path(__file__).parent))

from backend.config import settings
from backend.models import create_tables
from backend.auth.routes import router as auth_router
from backend.api.providers import router as providers_router
from backend.api.proxy import router as proxy_router
from backend.billing.routes import router as billing_router
from backend.api.routers import router as routers_router
from backend.api.router_check import router as check_router
from backend.api.usage import router as usage_router
from backend.api.aliases import router as aliases_router
from backend.api.traces import router as traces_router
from backend.api.chats import router as chats_router
from backend.api.license import router as license_router
from backend.api.experience import router as experience_router

# 日志
logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("gateway")

# ============================================================
# 生命周期
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动/关闭钩子"""
    create_tables()  # 幂等建表: 全新数据库首次启动时补齐所有表
    logger.info("智能网关 启动中...")

    # 启动后台测速轮询任务
    import asyncio
    from backend.api.providers import _run_speed_check_for_all_users

    async def _speed_poll_loop():
        """每 2 小时自动测速一次, 启动后先等 60 秒再开始"""
        await asyncio.sleep(60)  # 先让服务完全启动
        while True:
            try:
                logger.info("后台测速开始...")
                await _run_speed_check_for_all_users()
                logger.info("后台测速完成")
            except Exception as e:
                logger.warning(f"后台测速异常: {e}")
            await asyncio.sleep(7200)  # 2 小时

    async def _daily_summary_loop():
        """每日定时: 为所有用户自动生成当日经验/教训总结。
        默认 23:30 执行; 启动时先等 120 秒再进入调度。
        """
        if not settings.EXPERIENCE_ENABLED:
            logger.info("经验积累未启用, 跳过每日总结调度")
            return
        from backend.core.experience import distill_chats, summarize_daily
        await asyncio.sleep(120)  # 先让服务与首次测速稳定
        while True:
            try:
                HH, MM = (settings.EXPERIENCE_SUMMARY_TIME or "23:30").split(":")
                target = int(HH) * 3600 + int(MM) * 60
                now = int(datetime.now().hour) * 3600 + int(datetime.now().minute) * 60
                # 若已过今天目标时间则明天再触发
                delay = (target - now) % 86400
                if delay == 0:
                    delay = 86400
                await asyncio.sleep(delay)
                # 触发每日总结 + 顺便提炼当天经验
                from backend.models import get_session, User
                session = get_session()
                try:
                    users = session.query(User).filter_by(is_active=True).all()
                    emails = [u.email for u in users]  # noqa: F841
                    uids = [u.id for u in users]
                finally:
                    session.close()
                day = datetime.now().strftime("%Y-%m-%d")
                for uid in uids:
                    try:
                        await distill_chats(uid, limit=settings.EXPERIENCE_DISTILL_LIMIT)
                        await summarize_daily(uid, day)
                        logger.info("每日总结完成 user=%s date=%s", uid, day)
                    except Exception as e:
                        logger.warning("每日总结失败 user=%s: %s", uid, e)
                await asyncio.sleep(86400)  # 等一天再继续
            except Exception as e:
                logger.warning("每日总结调度异常: %s", e)
                await asyncio.sleep(3600)

    speed_task = asyncio.create_task(_speed_poll_loop())
    summary_task = asyncio.create_task(_daily_summary_loop())
    summary_task = asyncio.create_task(_daily_summary_loop())
    yield
    speed_task.cancel()
    summary_task.cancel()
    try:
        await speed_task
        await summary_task
    except asyncio.CancelledError:
        pass
    logger.info("智能网关 已停止")

# ============================================================
# FastAPI 应用
# ============================================================
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件
WEB_DIR = Path(__file__).parent.parent / "web"
if WEB_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(WEB_DIR / "assets")), name="assets")
    app.mount("/favicon.svg", StaticFiles(directory=str(WEB_DIR), html=True), name="favicon")
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

# 路由注册
app.include_router(auth_router)
app.include_router(providers_router)
app.include_router(proxy_router)
app.include_router(billing_router)
app.include_router(routers_router)
app.include_router(check_router)
app.include_router(usage_router)
app.include_router(aliases_router)
app.include_router(traces_router)
app.include_router(chats_router)
app.include_router(license_router)
app.include_router(experience_router)


@app.get("/")
async def root():
    index_path = WEB_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return {"name": settings.APP_NAME, "version": settings.APP_VERSION, "status": "ok"}


@app.get("/api/health")
async def health():
    return {"status": "healthy", "version": settings.APP_VERSION}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.HOST, port=settings.PORT, log_level="info")