# ============================================================
# 智能网关 — 主应用入口
# ============================================================

import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
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
from backend.api.requests import router as requests_router
from backend.api.license import router as license_router
from backend.api.experience import router as experience_router
from backend.api.free_sources import router as free_sources_router

# 日志: 控制台 + 文件(exe 打包后 windowed 无 stderr, 500 堆栈只能靠文件日志排查)
_LOG_ROOT = (Path(os.environ.get("GW_DB_PATH", "")).resolve().parent.parent
             if os.environ.get("GW_DB_PATH") else Path(__file__).resolve().parent.parent)
_LOG_DIR = _LOG_ROOT / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(_LOG_DIR / "gateway.log"), encoding="utf-8"),
    ],
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

    # 免费源自动同步: 对齐 CX-AIRouter 的"每6小时扫描全网免费源"
    from backend.core.free_sources import sync_free_sources

    async def _free_source_sync_loop():
        if not settings.FREE_SOURCE_AUTO_SYNC:
            logger.info("免费源自动同步未启用")
            return
        await asyncio.sleep(settings.FREE_SOURCE_SYNC_START_DELAY)
        while True:
            try:
                r = sync_free_sources()
                logger.info(
                    "免费源同步完成: 模板%d个, 新增%s, 更新%s, 失败%s",
                    r["total_templates"], r["added"], r["updated"], r["failed_sources"],
                )
            except Exception as e:
                logger.warning("免费源同步异常: %s", e)
            await asyncio.sleep(settings.FREE_SOURCE_SYNC_HOURS * 3600)

    sync_task = asyncio.create_task(_free_source_sync_loop())

    # OpenCode Zen 免费模型: 自动发现同步 + 持续健康检测
    from backend.core.zen_sources import sync_zen_free_models, health_check_zen_models

    async def _zen_sync_loop():
        if not settings.ZEN_AUTO_SYNC:
            logger.info("opencode 免费模型同步未启用")
            return
        await asyncio.sleep(180)  # 等服务稳定
        while True:
            try:
                # 同步含网络探测: 放后台线程, 避免阻塞事件循环卡死网关
                r = await asyncio.to_thread(sync_zen_free_models)
                logger.info("opencode 免费模型同步: 发现%d个, 可用%d个, 不可用%d",
                            r.get("total_free", 0), len(r.get("usable", [])),
                            len(r.get("unusable", {})))
            except Exception as e:
                logger.warning("opencode 免费模型同步异常: %s", e)
            await asyncio.sleep(settings.ZEN_SYNC_HOURS * 3600)

    async def _zen_health_loop():
        if not settings.ZEN_AUTO_SYNC:
            return
        await asyncio.sleep(480)  # 首次健康检测等同步完成后
        while True:
            try:
                r = await asyncio.to_thread(health_check_zen_models)
                if r.get("removed"):
                    logger.info("opencode 健康检测: 移出不可用模型 %s", r["removed"])
            except Exception as e:
                logger.warning("opencode 健康检测异常: %s", e)
            await asyncio.sleep(settings.ZEN_HEALTH_CHECK_MINUTES * 60)

    # 每日额度重置: 魔搭等渠道按天发放免费额度, 每天 00:10 清空质量/熔断统计,
    # 让额度恢复的渠道自动重新参与路由(白天额度用尽则由熔断/排序自动绕开);
    # 同时把锚点已过零点的用户当日额度清零(幂等), 界面无需等待真实调用即可见新一天。
    def _sweep_user_quota() -> int:
        """把 quota_reset_at 已过期的用户清零并推进锚点到明天零点, 返回受影响行数"""
        session = None
        try:
            from backend.models import get_session
            from sqlalchemy import text
            session = get_session()
            now_s = datetime.now()
            tm = now_s.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            res = session.execute(
                text("UPDATE users SET quota_used_today = 0, quota_reset_at = :tm "
                     "WHERE quota_reset_at IS NOT NULL AND quota_reset_at < :now"),
                {"tm": tm, "now": now_s},
            )
            session.commit()
            return res.rowcount or 0
        except Exception as e:
            logger.warning("用户额度日清零 sweep 失败: %s", e)
            return 0
        finally:
            if session is not None:
                session.close()

    async def _daily_quota_reset_loop():
        # 启动补跑: 应用在零点之后才启动/重启时, 已过期用户的额度立即清零(幂等)
        swept = _sweep_user_quota()
        if swept:
            logger.info("每日额度重置: 启动补跑清零 %s 个用户的当日额度", swept)
        while True:
            now = datetime.now()
            target = (now.replace(hour=0, minute=10, second=0, microsecond=0)
                      + timedelta(days=1))
            await asyncio.sleep(max(1, (target - now).total_seconds()))
            try:
                from backend.core.scheduler import quality_tracker
                from backend.core.circuit import circuit_breaker
                quality_tracker.reset_all()
                circuit_breaker.reset_all()
                swept = _sweep_user_quota()
                logger.info("每日额度重置: 质量/熔断已清空, 用户额度清零 %s 个, 渠道重新公平路由", swept)
            except Exception as e:
                logger.warning("每日额度重置异常: %s", e)

    quota_reset_task = asyncio.create_task(_daily_quota_reset_loop())
    zen_sync_task = asyncio.create_task(_zen_sync_loop())
    zen_health_task = asyncio.create_task(_zen_health_loop())
    yield
    speed_task.cancel()
    summary_task.cancel()
    sync_task.cancel()
    zen_sync_task.cancel()
    zen_health_task.cancel()
    quota_reset_task.cancel()
    try:
        await speed_task
        await summary_task
        await sync_task
        await zen_sync_task
        await zen_health_task
        await quota_reset_task
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
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


# 站点图标 / 品牌 logo: 根路径直接返回, 桌面端(生产)与 dev 均可访问
@app.get("/favicon.png")
async def favicon_png():
    return FileResponse(WEB_DIR / "favicon.png", media_type="image/png")


@app.get("/frenchie-logo.png")
async def frenchie_logo_png():
    return FileResponse(WEB_DIR / "frenchie-logo.png", media_type="image/png")

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
app.include_router(requests_router)
app.include_router(license_router)
app.include_router(experience_router)
app.include_router(free_sources_router)


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