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

    # ---- 上游出站网络 ----
    # 网关访问所有上游(渠道/免费池)的代理策略:
    #   ""(默认)  跟随系统/环境变量代理(httpx trust_env 原生行为)
    #   "none"    强制直连, 忽略系统代理 —— 系统代理半死导致上游 EOF/000 时用
    #   "http://127.0.0.1:7892" 等  显式指定代理
    # 实测(2026-10-24): 本机 Clash(127.0.0.1:7892)半挂时, 默认走它会连所有
    # 上游都 EOF; 直连矩阵证明 kilo/opencode/github/trae/qoder/cline 均可直连。
    UPSTREAM_PROXY: str = ""

    # ---- 路由调度 ----
    CIRCUIT_FAIL_THRESHOLD: int = 3
    CIRCUIT_RECOVERY_SECONDS: int = 60
    QUALITY_WINDOW: int = 20
    # 轮询模式: 候选模型轮流使用, 避免逮着同一个模型把单一上游额度/限流打爆
    ROUND_ROBIN_ENABLED: bool = True
    # 加权随机分发: 替代"排序取首位", 按 质量分×今日用量平滑×渠道冷却权重 抽样,
    # 失败后按剩余权重继续抽样 —— 流量不再恒定砸向最高分候选, 免费额度日均摊开。
    # 仅在 ROUND_ROBIN_ENABLED=True 时生效(关闭即回到纯轮询)。
    SPREAD_WEIGHTED_ENABLED: bool = True
    # 今日调用量平滑强度: 每个候选权重除以 (1 + 今日成功调用数 × 该系数)。
    # 0 关闭; 0.4 ≈ 5 次后权重砍半, 20 次后剩 1/9, 防止单模型额度早上被打爆。
    SPREAD_QUOTA_BIAS: float = 0.4
    # 渠道并发上限: 免费档(free_only)平台多为账号级额度, 并发过高直接被打回 429;
    # 有 key 的付费/普通渠道给更高的并发。渠道级可在渠道详情里单独覆盖。
    SHAPING_DEFAULT_MAX_CONCURRENCY: int = 4
    SHAPING_FREE_MAX_CONCURRENCY: int = 2
    # 同一 (渠道, 模型) 的最小调用间隔(毫秒) —— 模型级令牌桶, 防止突发流量打爆 RPM。
    SHAPING_DEFAULT_MIN_INTERVAL_MS: int = 600
    # 渠道并发槽满时的等待上限(秒): 超过即跳过该渠道换下一个候选, 不无限排队
    SHAPING_SEMAPHORE_TIMEOUT: float = 2.0
    POLL_INTERVAL: int = 300
    MAX_CONCURRENT_POLLS: int = 10
    # 代理转发超时(秒) — 每个上游模型尝试的最长时间
    PROXY_TIMEOUT_SECONDS: int = 120
    # 非流式转发读超时(秒): 与流式同口径, 连接后等首字节超过该值即放弃
    # 当前候选换下一个。之前非流式套的是 PROXY_TIMEOUT_SECONDS 整体超时,
    # 挂死的上游(如 NVIDIA deepseek-v4-pro-0813)会把单次尝试拖满 120s。
    PROXY_NONSTREAM_READ_TIMEOUT: int = 30
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

    # ---- 白嫖渠道每日签到(HTTP 接口版, 参考 dsh-our-free-model auto-checkin) ----
    # 每天定点对所有已连接且支持签到接口的渠道(如 CodeArts)自动领取每日积分;
    # UTC+8 自然日防重, 状态落盘 data/channel_checkin_state.json
    CHANNEL_CHECKIN_ENABLED: bool = True
    # 每日执行时间(HH:MM, 本地时区)
    CHANNEL_CHECKIN_TIME: str = "09:30"
    # 启动后延迟多少秒先补跑一轮(服务当天晚启动也能领上当日积分)
    CHANNEL_CHECKIN_START_DELAY: int = 120
    # 凭据提前刷新量(分钟): access_token 剩余寿命低于该值即自动 refresh,
    # 让 token 永不过期(登录一次后由后台保活, 不再需要手动重登)。
    # 旧版仅剩 60s 才刷, 短寿命 token(trae/cline)常在两轮保活间过期。
    CRED_REFRESH_AHEAD_MINUTES: int = 120

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
    # ⚠️ 默认 0 = 关闭定时体检。原因: 车道是"公共匿名池"(无账号, 按出口/会话计免费额度),
    # 而体检 = 对渠道里每个模型发一次**真实 chat 请求**, 9 个模型 × 每小时 = 每天
    # 200+ 次真调用, 会把本该留给你的免费额度提前烧光(2026-10-24 实测踩到)。
    # 死模型改由真实流量判定: 请求 400 连续 3 次即长冷却并移出路由组。
    # 需要主动体检时设成正数(分钟), 或到「免费渠道」页点「立即同步」。
    ZEN_HEALTH_CHECK_MINUTES: int = 0
    ZEN_PROBE_FAIL_LIMIT: int = 2

    # ---- Kilo 公共网关免费池(免认证): opencode zen 匿名车道被上游
    # FreeTierError 关闭后(2026-10-24 实测, 插件本机同样全灭)的免费池替代 ----
    KILO_AUTO_SYNC: bool = True
    KILO_SYNC_HOURS: int = 6

    # ---- 后台测速: 跳过"额度是公共池"的免密车道路由组 ----
    # 测速是对每个路由组发一次真实请求, 而组内 failover 会一路试到有货为止:
    # opencode 组 9 个模型全 429 时 = 一次测速就发 9 个真请求, 每 2h 一轮,
    # 一天 100+ 次, 把公共免费额度提前烧光(2026-10-24 实测踩到)。
    # 逗号分隔的路由组名; 置空则不跳过任何组。
    SPEED_CHECK_SKIP_GROUPS: str = "opencode,kilo"

    # ---- b.ai 免费模型: 自动发现 + 健康检测(同 opencode zen 模式) ----
    # 定时拉 {B_AI_BASE_URL}/models 目录, 真实探测免费可用性后写进"biai"渠道,
    # 持续重探保活: 连续失败 B_AI_PROBE_FAIL_LIMIT 次的模型自动移出渠道。
    # 注意: b.ai 在国内网络通常不可直连, 同步失败时保留现有渠道不覆盖(防误清)。
    B_AI_AUTO_SYNC: bool = True
    B_AI_BASE_URL: str = "https://api.b.ai/v1"
    B_AI_API_KEY: str = ""   # 空=匿名免费档探测; 有值则所有请求带 Bearer token
    B_AI_SYNC_HOURS: int = 6
    B_AI_HEALTH_CHECK_MINUTES: int = 60
    B_AI_PROBE_FAIL_LIMIT: int = 2
    B_AI_PROBE_CAP: int = 60  # 目录过大时只探测免费特征模型, 防探测风暴
    B_AI_FREE_MODEL_IDS: list = []  # 目录不可用时的兜底模型 ID 列表

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
    # 前端入口已下线, 且每日 23:30 的提炼/总结会额外调用 LLM → 默认关闭
    EXPERIENCE_ENABLED: bool = False
    # 是否在每次 /v1/chat/completions 调用时注入历史经验(自动)
    EXPERIENCE_INJECT_AUTO: bool = True
    # 每日总结自动执行时间(24h, "HH:MM")
    EXPERIENCE_SUMMARY_TIME: str = "23:30"
    # 每次最多总结经验条数
    EXPERIENCE_DISTILL_LIMIT: int = 40
    # 每日总结保留最近天数
    EXPERIENCE_KEEP_DAYS: int = 90

    # ---- 免费模型帖子搜索(每日定时) ----
    # 定时爬取 Gitee/GitHub/百度/今日头条/微信公众号上"免费模型注册"相关帖子,
    # 入库 + LLM 评估可行性/真实性 + 生成 Markdown 报告(落盘 + 数据库)。
    FREE_MODEL_SEARCH_ENABLED: bool = True
    # 每天执行时间(HH:MM), 默认 23:00
    FREE_MODEL_SEARCH_TIME: str = "23:00"
    # 启动后延迟多少秒再首次运行(让服务稳定)
    FREE_MODEL_SEARCH_START_DELAY: int = 300
    # 每个来源每次搜索的关键词数量上限(避免单次请求过大)
    FREE_MODEL_SEARCH_PER_SOURCE_LIMIT: int = 15
    # 是否对帖子做 LLM 评估(可行性/真实性打分)
    FREE_MODEL_EVALUATION_ENABLED: bool = True
    # 搜索来源开关
    FREE_MODEL_SEARCH_SOURCES: list = [
        "gitee", "github", "baidu", "toutiao", "wechat"
    ]

    # ---- 死模型每日自动复检(拉黑平反) ----
    # dead_models 的 404/410 拉黑是永久性的, 但上游偶发故障(NVIDIA 平台
    # 502/读超时/权限 404 等)会把"其实还活着"的模型误拉黑且永不翻身。
    # 每天定时对所有被拉黑模型做一次真实小请求探测, 返回 200 的自动解封:
    # 删拉黑记录 + 补回渠道模型表 + 放回同渠道兄弟模型所在的路由组。
    DEAD_MODEL_RECOVERY_ENABLED: bool = True
    # 每日自动复检时间(HH:MM)
    DEAD_MODEL_RECOVERY_TIME: str = "04:20"
    # 启动后延迟多少秒先补跑一轮(部署后无需等次日即可平反存量误杀)
    DEAD_MODEL_RECOVERY_START_DELAY: int = 600
    # 单模型探测超时(秒): 上游高峰排队时 200 可能要 60-80s 才返回(实测
    # kimi-k3 77s 才出首个 token), 超时太紧会把"活着但慢"的模型永远留在
    # 拉黑名单。放宽到 90s: 只要上游最终返回 200 就平反; 太慢导致真实请求
    # 失败的模型由质量分/延迟惩罚自动排到候选末尾, 不会拖垮路由组。
    DEAD_MODEL_PROBE_TIMEOUT: float = 90.0
    # 探测并发上限(免费渠道并发过高会被打 429)
    DEAD_MODEL_PROBE_CONCURRENCY: int = 6


settings = Settings()