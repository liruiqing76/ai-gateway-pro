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
from backend.api.anthropic import router as anthropic_router
from backend.api.anthropic import anthropic_http_exception_handler
from backend.api.responses import router as responses_router
from backend.api.responses import responses_http_exception_handler
from backend.api.channels import router as channels_router
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
from backend.api.free_model_search import router as free_model_search_router
from backend.api.downed_models import router as downed_models_router
from backend.api.dead_model_api import router as dead_model_router
from backend.api.model_catalog import router as model_catalog_router

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
    from backend.core.chat_archive import ensure_archive_column
    ensure_archive_column()  # 为旧库补 prompt_archive 列(聊天原文外置归档用)
    # 首次启动 seed 持久化模板/免费源配置(从打包内置基线), 保证模板一键添加可用
    from backend.core.free_sources import ensure_runtime_assets
    ensure_runtime_assets()
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


    # 免费源自动同步: 对齐 CX-AIRouter 的"每6小时扫描全网免费源"
    from backend.core.free_sources import sync_free_sources

    async def _free_source_sync_loop():
        if not settings.FREE_SOURCE_AUTO_SYNC:
            logger.info("免费源自动同步未启用")
            return
        await asyncio.sleep(settings.FREE_SOURCE_SYNC_START_DELAY)
        while True:
            try:
                # sync_free_sources 内部用同步 httpx.get 拉外部源(每个最长 15s),
                # 必须放后台线程: 裸跑在事件循环里会冻死所有接口(含模板一键添加)
                r = await asyncio.to_thread(sync_free_sources)
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
        if settings.ZEN_HEALTH_CHECK_MINUTES <= 0:
            # 默认关闭: 体检=对每个模型发真实 chat 请求, 会提前烧光公共免费额度。
            # 死模型由真实流量判定(400 连续 3 次 → 长冷却 + 移出路由组)。
            logger.info("opencode 定时体检未启用(ZEN_HEALTH_CHECK_MINUTES=0), "
                        "死模型改由真实流量判定")
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

    # b.ai 免费模型: 自动发现同步 + 持续健康检测(与 opencode zen 同模式)
    from backend.core.bai_sources import sync_bai_free_models, health_check_bai_models

    async def _bai_sync_loop():
        if not settings.B_AI_AUTO_SYNC:
            logger.info("b.ai 免费模型同步未启用")
            return
        await asyncio.sleep(240)  # 等服务稳定
        while True:
            try:
                r = await asyncio.to_thread(sync_bai_free_models)
                logger.info("b.ai 免费模型同步: 发现%d个, 可用%d个, 不可用%d",
                            r.get("total_free", 0), len(r.get("usable", [])),
                            len(r.get("unusable", {})))
            except Exception as e:
                logger.warning("b.ai 免费模型同步异常: %s", e)
            await asyncio.sleep(settings.B_AI_SYNC_HOURS * 3600)

    async def _bai_health_loop():
        if not settings.B_AI_AUTO_SYNC:
            return
        await asyncio.sleep(540)  # 首次健康检测等同步完成后
        while True:
            try:
                r = await asyncio.to_thread(health_check_bai_models)
                if r.get("removed"):
                    logger.info("b.ai 健康检测: 移出不可用模型 %s", r["removed"])
            except Exception as e:
                logger.warning("b.ai 健康检测异常: %s", e)
            await asyncio.sleep(settings.B_AI_HEALTH_CHECK_MINUTES * 60)

    # 免费模型帖子搜索: 每天定时爬取 Gitee/GitHub/百度/今日头条/微信公众号上
    # "免费模型注册"相关帖子, 去重入库 + LLM 评估 + 生成 Markdown 报告。
    from backend.core.free_model_search import run_free_model_search

    async def _free_model_search_loop():
        if not settings.FREE_MODEL_SEARCH_ENABLED:
            logger.info("免费模型帖子搜索未启用")
            return
        await asyncio.sleep(settings.FREE_MODEL_SEARCH_START_DELAY)
        while True:
            try:
                HH, MM = (settings.FREE_MODEL_SEARCH_TIME or "23:00").split(":")
                target = int(HH) * 3600 + int(MM) * 60
                now = int(datetime.now().hour) * 3600 + int(datetime.now().minute) * 60
                delay = (target - now) % 86400
                if delay == 0:
                    delay = 86400
                await asyncio.sleep(delay)
                # 搜索含网络请求与 LLM 评估: 放后台线程, 避免阻塞事件循环
                r = await asyncio.to_thread(
                    run_free_model_search,
                    None,  # keywords: 用默认
                    settings.FREE_MODEL_SEARCH_SOURCES,
                    settings.FREE_MODEL_SEARCH_PER_SOURCE_LIMIT,
                    settings.FREE_MODEL_EVALUATION_ENABLED,
                )
                logger.info("免费模型搜索完成: 发现%s, 新增%s, 评估%s",
                            r.get("total_found"), r.get("added"), r.get("evaluated"))
            except Exception as e:
                logger.warning("免费模型搜索异常: %s", e)
                await asyncio.sleep(3600)  # 异常后等 1 小时再试
                continue
            # delay 已含"距下次 23:00 的秒数", 循环回到顶部会重新计算,
            # 无需再额外 sleep 86400(否则会变成 48 小时间隔)

    # 死模型每日自动复检: NVIDIA 等上游偶发故障(平台 502/断连/读超时)会把
    # "其实还活着"的模型误拉黑进 dead_models(永久), 且拉黑后从不复查, 导致
    # 可用模型数单向缩水。每天定时对所有拉黑记录做 1-token 真实探测, 返回
    # 200 的自动解封并放回渠道/路由组; 启动约 10 分钟后先补跑一轮, 部署后
    # 无需等次日即可平反存量误杀。探测含网络请求, 放后台线程不阻塞事件循环。
    async def _dead_model_recovery_loop():
        if not settings.DEAD_MODEL_RECOVERY_ENABLED:
            logger.info("死模型自动复检未启用")
            return
        from backend.core.dead_model_recovery import recover_dead_models

        def _run_once() -> dict:
            r = recover_dead_models()
            logger.info(
                "死模型自动复检: 探测%d个, 复活%d个(%s), 仍死%d, 未开通%d, "
                "状态不明%d, 跳过停用渠道%d, 清孤儿%d",
                r["probed"], len(r["revived"]), ",".join(r["revived"]) or "-",
                r["dead"], r["no_access"], r["uncertain"],
                r["skipped_disabled"], r["orphan_removed"],
            )
            return r

        # 启动补跑: 等服务稳定后先平反一轮存量误杀
        await asyncio.sleep(settings.DEAD_MODEL_RECOVERY_START_DELAY)
        try:
            await asyncio.to_thread(_run_once)
        except Exception as e:
            logger.warning("死模型自动复检(启动补跑)异常: %s", e)
        while True:
            try:
                HH, MM = (settings.DEAD_MODEL_RECOVERY_TIME or "04:20").split(":")
                target = int(HH) * 3600 + int(MM) * 60
                now = int(datetime.now().hour) * 3600 + int(datetime.now().minute) * 60
                delay = (target - now) % 86400
                if delay == 0:
                    delay = 86400
                await asyncio.sleep(delay)
                await asyncio.to_thread(_run_once)
            except Exception as e:
                logger.warning("死模型自动复检异常: %s", e)
                await asyncio.sleep(3600)

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

    # Kilo 公共网关免费池(免认证): zen 匿名车道被上游 v2 门收紧期间的免费池
    # (2026-10-24 实测 kilo 200 可用 / 车道实现见 opencode_free.py 等网络恢复)
    from backend.core.kilo_sources import sync_kilo_free_models

    async def _kilo_sync_loop():
        if not settings.KILO_AUTO_SYNC:
            logger.info("kilo 免费池同步未启用")
            return
        await asyncio.sleep(300)  # 等服务稳定, 排在 zen 同步之后
        while True:
            try:
                r = await asyncio.to_thread(sync_kilo_free_models)
                logger.info("kilo 免费池同步: 发现%d个, 落地%d个, 探测失败%d, 用户%d",
                            r.get("total_free", 0), len(r.get("usable", [])),
                            len(r.get("unusable", {})), r.get("users_updated", 0))
            except Exception as e:
                logger.warning("kilo 免费池同步异常: %s", e)
            await asyncio.sleep(settings.KILO_SYNC_HOURS * 3600)

    # 白嫖渠道每日签到(HTTP 接口版, 参考 dsh-our-free-model auto-checkin):
    # 每天定点对所有已连接且支持签到接口的渠道(如 CodeArts)自动领取每日积分。
    # UTC+8 自然日防重由 core/checkin.py 落盘状态保证。
    async def _channel_checkin_loop():
        if not settings.CHANNEL_CHECKIN_ENABLED:
            logger.info("渠道每日签到未启用")
            return
        from backend.core.checkin import run_checkin_for_all_users

        await asyncio.sleep(settings.CHANNEL_CHECKIN_START_DELAY)
        try:
            await run_checkin_for_all_users()
        except Exception as e:
            logger.warning("渠道签到(启动补跑)异常: %s", e)
        while True:
            try:
                HH, MM = (settings.CHANNEL_CHECKIN_TIME or "09:30").split(":")
                now = datetime.now()
                target = now.replace(hour=int(HH), minute=int(MM),
                                     second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                await asyncio.sleep((target - now).total_seconds())
                await run_checkin_for_all_users()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("渠道签到异常: %s", e)
                await asyncio.sleep(3600)

    # 渠道凭据保活: 参考插件 refreshAllCredentials(每 30 分钟批量续期)。
    # 推理路径不同步等刷新, 靠本循环把白嫖渠道凭据始终保持在有效期内。
    async def _channel_credential_keepalive_loop():
        from backend.core.checkin import refresh_all_expiring
        await asyncio.sleep(60)  # 等服务稳定
        while True:
            try:
                await refresh_all_expiring()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("渠道凭据保活异常: %s", e)
            await asyncio.sleep(1800)

    quota_reset_task = asyncio.create_task(_daily_quota_reset_loop())
    dead_recovery_task = asyncio.create_task(_dead_model_recovery_loop())
    zen_sync_task = asyncio.create_task(_zen_sync_loop())
    zen_health_task = asyncio.create_task(_zen_health_loop())
    bai_sync_task = asyncio.create_task(_bai_sync_loop())
    bai_health_task = asyncio.create_task(_bai_health_loop())
    free_model_search_task = asyncio.create_task(_free_model_search_loop())
    kilo_sync_task = asyncio.create_task(_kilo_sync_loop())
    channel_checkin_task = asyncio.create_task(_channel_checkin_loop())
    channel_keepalive_task = asyncio.create_task(_channel_credential_keepalive_loop())
    yield
    speed_task.cancel()
    summary_task.cancel()
    sync_task.cancel()
    zen_sync_task.cancel()
    zen_health_task.cancel()
    bai_sync_task.cancel()
    bai_health_task.cancel()
    quota_reset_task.cancel()
    free_model_search_task.cancel()
    kilo_sync_task.cancel()
    dead_recovery_task.cancel()
    channel_checkin_task.cancel()
    channel_keepalive_task.cancel()
    try:
        await speed_task
        await summary_task
        await sync_task
        await zen_sync_task
        await zen_health_task
        await bai_sync_task
        await bai_health_task
        await quota_reset_task
        await free_model_search_task
        await kilo_sync_task
        await dead_recovery_task
        await channel_checkin_task
        await channel_keepalive_task
    except asyncio.CancelledError:
        pass
    # Qoder 签名器常驻 node 进程: 服务停止时回收(不留孤儿进程)
    try:
        from backend.core.channel_auth import qoder as qoder_mod
        if qoder_mod._signer is not None:
            await qoder_mod._signer.shutdown()
    except Exception:
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
app.include_router(anthropic_router)
app.include_router(responses_router)
app.include_router(billing_router)
app.include_router(routers_router)
app.include_router(check_router)
app.include_router(usage_router)
app.include_router(aliases_router)
app.include_router(model_catalog_router)
app.include_router(traces_router)
app.include_router(chats_router)
app.include_router(requests_router)
app.include_router(license_router)
app.include_router(experience_router)
app.include_router(free_sources_router)
app.include_router(free_model_search_router)
app.include_router(downed_models_router)
app.include_router(dead_model_router)
app.include_router(channels_router)

# 协议面错误形状: /v1/messages* → {"type":"error","error":{...}}(Anthropic),
# /v1/responses* → {"error":{...}}(Responses), 其它路径保持 FastAPI 默认
# {"detail": ...} 不变。两个 handler 各自已按路径前缀判断, 这里做一层分派。
from fastapi.exceptions import HTTPException as FastAPIHTTPException


async def _protocol_exception_handler(request, exc: FastAPIHTTPException):
    if request.url.path.startswith("/v1/responses"):
        return await responses_http_exception_handler(request, exc)
    return await anthropic_http_exception_handler(request, exc)


app.add_exception_handler(FastAPIHTTPException, _protocol_exception_handler)


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