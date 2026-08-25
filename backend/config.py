# ============================================================
# 智能网关 — 配置(pydantic-settings)
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
    APP_NAME: str = "智能网关"
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
    # 代理转发超时(秒) — 每个上游模型尝试的最长时间
    PROXY_TIMEOUT_SECONDS: int = 120
    # 流式转发读超时(秒): 等首字节/相邻数据块超过该值即放弃当前候选,
    # 自动切换组内下一个模型("卡住30秒就换模型"), 防止单上游拖死整个请求。
    PROXY_STREAM_READ_TIMEOUT: int = 30
    # 流式传输 per-chunk 超时(秒) — 上游超过此时间无数据则判定断流
    STREAMING_TIMEOUT_SECONDS: int = 300

    # ---- 自动签到 ----
    CHECKIN_ENABLED: bool = True
    CHECKIN_INTERVAL_HOURS: int = 24
    CHECKIN_RETRY: int = 3
    CHECKIN_TIMEOUT: int = 30

    # ---- 免费源自动同步 (对齐 CX-AIRouter 的"每6小时扫描全网免费源") ----
    # 定时把外部免费源清单(JSON)拉取下来, 与本地 provider_templates.json 合并,
    # 新增的免费服务商自动进入"添加渠道"模板池。
    FREE_SOURCE_AUTO_SYNC: bool = True
    FREE_SOURCE_SYNC_HOURS: int = 6
    FREE_SOURCE_SYNC_START_DELAY: int = 90

    # ---- OpenCode Zen 免费模型: 自动发现 + 健康检测 ----
    # 定时拉 models.dev 快照里的 zen 免费模型, 真实探测可用性后写进"opencode"渠道,
    # 并持续重探保活: 连续失败 ZEN_PROBE_FAIL_LIMIT 次的模型自动移出渠道。
    ZEN_AUTO_SYNC: bool = True
    ZEN_SYNC_HOURS: int = 6
    ZEN_HEALTH_CHECK_MINUTES: int = 60
    ZEN_PROBE_FAIL_LIMIT: int = 2

    # ---- 自动识图(auto-vision) ----
    # 视觉识别阶段对候选渠道的全量扫描轮数(轮间短暂退避);
    # 全部失败后仍会降级为文本兜底, 不会对客户端硬失败。
    VISION_RECOGNIZE_PASSES: int = 3
    # 视觉子调用的输出上限(token): 识别结果通常很短, 钳制后不会因
    # completion 侧超限被 NVIDIA 等上游 400(Agent 客户端常带 65536)。
    VISION_MAX_OUTPUT_TOKENS: int = 2048

    # ---- CORS ----
    CORS_ORIGINS: list = [
        "http://localhost",
        "http://127.0.0.1",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    # ---- 商业化绑定 / 远程授权 ----
    # 机器码采集开关; False 时(如非 Windows/CI)跳过硬件绑定, 不锁定
    LICENSE_ENABLED: bool = True
    # 远程授权服务器地址。为空 = 仅本地校验(机器码↔账号); 配置后每次登录/启动联网向
    # LICENSE_SERVER_URL/{verify} 校验绑定与封禁状态, 实现远程锁定。
    # 远程接口约定: POST {LICENSE_SERVER_URL}/verify  body={"machine_code":..,"email":..}
    #              返回 {"ok":bool,"locked":bool?,"message":str?}
    LICENSE_SERVER_URL: str = ""
    # 远程校验超时(秒)
    LICENSE_SERVER_TIMEOUT: int = 5

    # ---- 经验积累 / 每日总结 ----
    # 是否启用: 从聊天记录提炼经验 + 每日总结 + 下次调用注入
    EXPERIENCE_ENABLED: bool = True
    # 是否在每次 /v1/chat/completions 调用时注入历史经验(自动)
    EXPERIENCE_INJECT_AUTO: bool = True
    # 每日总结自动执行时间(24h, "HH:MM")
    EXPERIENCE_SUMMARY_TIME: str = "23:30"
    # 每次最多总结经验条数
    EXPERIENCE_DISTILL_LIMIT: int = 40
    # 每日总结保留最近天数
    EXPERIENCE_KEEP_DAYS: int = 90


settings = Settings()