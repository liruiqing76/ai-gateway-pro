# ============================================================
# AI 模型网关 Pro — 主应用入口
# ============================================================

import logging
import os
import sys
from contextlib import asynccontextmanager
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
from backend.api.usage import router as usage_router

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
    logger.info("AI 模型网关 Pro 启动中...")
    yield
    logger.info("AI 模型网关 Pro 已停止")

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
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

# 路由注册
app.include_router(auth_router)
app.include_router(providers_router)
app.include_router(proxy_router)
app.include_router(billing_router)
app.include_router(routers_router)
app.include_router(usage_router)
app.include_router(usage_router)


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