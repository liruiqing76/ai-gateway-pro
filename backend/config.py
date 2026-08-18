# ============================================================
# AI 模型网关 Pro — 配置(pydantic-settings)
# 所有配置项可通过环境变量覆盖,格式: GW_<KEY>
# ============================================================

from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    model_config = {
        "env_prefix": "GW_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
    }

    # ---- 应用 ----
    APP_NAME: str = "AI 模型网关 Pro"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False
    HOST: str = "127.0.0.1"
    PORT: int = 8000

    # ---- 数据库 ----
    DATABASE_URL: str = ""  # 空则自动用 sqlite:///绝对路径/data/gateway.db

    # ---- 安全 ----
    SECRET_KEY: str = "CHANGE_ME_IN_PRODUCTION_0123456789abcdef"
    ENCRYPTION_KEY: str = "CHANGE_ME_IN_PRODUCTION_0123456789abcdef"
    JWT_EXPIRE_MINUTES: int = 1440
    JWT_REFRESH_DAYS: int = 7
    BCRYPT_ROUNDS: int = 12
    LOGIN_MAX_ATTEMPTS: int = 5
    LOGIN_LOCK_MINUTES: int = 15

    # ---- API Key ----
    API_KEY_PREFIX: str = "sk-gw-"
    API_KEY_LENGTH: int = 32

    # ---- 速率限制 ----
    RATE_LIMIT_PER_MINUTE: int = 100

    # ---- 路由调度 ----
    CIRCUIT_FAIL_THRESHOLD: int = 3
    CIRCUIT_RECOVERY_SECONDS: int = 60
    QUALITY_WINDOW: int = 20
    POLL_INTERVAL: int = 300
    MAX_CONCURRENT_POLLS: int = 10

    # ---- 自动签到 ----
    CHECKIN_ENABLED: bool = True
    CHECKIN_INTERVAL_HOURS: int = 24
    CHECKIN_RETRY: int = 3
    CHECKIN_TIMEOUT: int = 30

    # ---- CORS ----
    CORS_ORIGINS: list = [
        "http://localhost",
        "http://127.0.0.1",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


settings = Settings()