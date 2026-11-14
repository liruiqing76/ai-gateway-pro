# ============================================================
# 智能网关 — 数据库模型
# 支持 SQLite(本地) / MySQL 8.0(远程)
# ============================================================

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, Float, Boolean,
    DateTime, ForeignKey, JSON, create_engine, UniqueConstraint, Index
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker
from pathlib import Path
from sqlalchemy import func

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)


class Base(DeclarativeBase):
    pass


# ============================================================
# 用户表
# ============================================================
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    email = Column(String(128), unique=True, nullable=False, index=True)
    password_hash = Column(String(128), nullable=False)
    role = Column(String(16), default="user")  # user / admin
    api_key = Column(String(64), unique=True, nullable=False, index=True)
    tier = Column(String(16), default="free")  # free / pro / enterprise
    tier_expires_at = Column(DateTime, nullable=True)
    quota_daily = Column(Integer, default=1000)
    quota_used_today = Column(Integer, default=0)
    quota_reset_at = Column(DateTime, nullable=True)
    email_verified = Column(Boolean, default=False)
    invite_code = Column(String(32), nullable=True, unique=True)
    invited_by = Column(Integer, nullable=True)
    login_attempts = Column(Integer, default=0)
    locked_until = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)
    # 商业化绑定: 本机机器码(HWID), 全局唯一 — 一台机器只能绑定一个账号
    machine_code = Column(String(128), nullable=True, unique=True, index=True)
    # 远程锁定状态(由授权服务器下发): locked=True 时整体锁定
    license_locked = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)
    last_login_at = Column(DateTime, nullable=True)

    providers = relationship("Provider", back_populates="user", cascade="all, delete-orphan")
    routers = relationship("Router", back_populates="user", cascade="all, delete-orphan")
    model_aliases = relationship("ModelAlias", backref="user", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "role": self.role,
            "tier": self.tier,
            "quota_daily": self.quota_daily,
            "quota_used_today": self.quota_used_today,
            "email_verified": self.email_verified,
            "is_active": self.is_active,
            "machine_code": self.machine_code,
            "license_locked": self.license_locked,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }


# ============================================================
# 渠道表 (每个用户独立的渠道配置)
# ============================================================
class Provider(Base):
    __tablename__ = "providers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(64), nullable=False)
    base_url = Column(String(256), nullable=False)
    api_key_encrypted = Column(Text, nullable=False)  # AES-256-GCM 加密
    models = Column(JSON, default=list)
    disabled_models = Column(JSON, default=list)
    free_only = Column(Boolean, default=True)
    # 流量整形(可空 = 用全局默认): 渠道并发上限 / 同一模型最小调用间隔(毫秒)
    max_concurrency = Column(Integer, nullable=True)
    min_interval_ms = Column(Integer, nullable=True)
    is_template = Column(Boolean, default=False)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    user = relationship("User", back_populates="providers")


# ============================================================
# 路由组
# ============================================================



class Router(Base):
    __tablename__ = "routers"
    # 同用户同组名唯一: 旧库曾出现同名重复行(历史版本/并发写入),
    # 读路径 dict 按 name 取最后一行、写路径却只改第一行 —— 编辑保存落到
    # 展示外的行上, 表现为"剔除了的模型又被勾选"。约束从根上杜绝重复。
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_routers_user_name"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(64), nullable=False)
    models = Column(JSON, default=list)
    strategy = Column(String(16), default="quality")  # quality / cost / random / failover
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("User", back_populates="routers")


# ============================================================
# 死模型拉黑(404/410 确认下架/停用 → 彻底删除, 永久不自动恢复)
# ============================================================
class DeadModel(Base):
    __tablename__ = "dead_models"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "provider_name", "model",
            name="uq_dead_models_user_provider_model",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider_name = Column(String(64), nullable=False)
    model = Column(String(255), nullable=False)
    marked_at = Column(DateTime, default=datetime.now)


# ============================================================
# 临时下架模型(429/503/超时/空回复等反复报错 → 自动下架, 到期自动恢复)
# 与 dead_models 区分: 那是 404/410 确定性死模型(永久拉黑, 不自动恢复);
# 这里是"暂时性故障反复出现"的临时下架, recovers_at 到期后自动重新参与路由,
# 不删源、不永久封死——免费模型偶发恢复是常态。
# ============================================================
class DownedModel(Base):
    __tablename__ = "downed_models"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "provider_name", "model",
            name="uq_downed_models_user_provider_model",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider_name = Column(String(64), nullable=False)
    model = Column(String(255), nullable=False)
    reason = Column(String(32), nullable=True)   # rate_limit / overload / timeout / empty / quota
    downed_at = Column(DateTime, default=datetime.now)
    recovers_at = Column(DateTime, nullable=False, index=True)  # 到期自动恢复


# ============================================================
# 用量记录
# ============================================================
class UsageLog(Base):
    __tablename__ = "usage_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider_name = Column(String(64), nullable=True)
    model_name = Column(String(128), nullable=True)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    cost = Column(Float, default=0.0)
    latency_ms = Column(Integer, nullable=True)
    success = Column(Boolean, default=True)
    error_msg = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now, index=True)


# ============================================================
# 签到记录
# ============================================================
class CheckinLog(Base):
    __tablename__ = "checkin_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    platform_name = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False)  # success / failed / skipped
    detail = Column(Text, nullable=True)
    claimed_quota = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=datetime.now)


# ============================================================
# 订单表
# ============================================================
class Order(Base):
    """充值订单"""
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    order_no = Column(String(32), unique=True, nullable=False)
    plan_name = Column(String(32), nullable=False)          # free / pro / enterprise
    amount_cents = Column(Integer, nullable=False)          # 分
    amount_paid = Column(Integer, default=0)
    payment_method = Column(String(16), nullable=True)       # wechat / alipay / manual
    payment_status = Column(String(16), default="pending")   # pending / paid / refunded / expired
    payment_ref = Column(String(128), default="")
    paid_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    remark = Column(String(512), default="")


# ============================================================
# 系统预设渠道模板
# ============================================================
class ProviderTemplate(Base):
    __tablename__ = "provider_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), nullable=False)
    base_url = Column(String(256), nullable=False)
    models = Column(JSON, default=list)
    free_only = Column(Boolean, default=True)
    signup_url = Column(String(256), nullable=True)
    config_url = Column(String(256), nullable=True)
    notes = Column(Text, nullable=True)
    tags = Column(JSON, default=list)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)


# ============================================================
# 模型别名表 (用户自定义别名映射)
# ============================================================
class ModelAlias(Base):
    __tablename__ = "model_aliases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    alias = Column(String(128), nullable=False)  # 别名, 如 "gpt-4"
    target = Column(String(128), nullable=False)  # 目标模型/路由组, 如 "high_quality" 或 "NVIDIA-deepseek-v3"
    description = Column(String(256), nullable=True)  # 描述
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # 复合唯一索引: 同一用户的别名不能重复
    __table_args__ = (
        {"sqlite_autoincrement": True},
    )


# ============================================================
# 模型倍率表 (渠道-模型 级的积分/额度消耗倍率, 默认 1.0)
# 白嫖渠道各平台按模型扣积分的倍率多数不对外提供接口: 支持的渠道
# (workbuddy/buddy 的 credits、lobsterai 的 costMultiplier)在登录/刷新时
# 从上游目录自动同步; 其余由用户按实际观察填写。
# 落库到 UsageLog.cost, 用量统计展示为"等效调用次数"。
# ============================================================
class ModelRate(Base):
    __tablename__ = "model_rates"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "provider_name", "model",
            name="uq_model_rates_user_provider_model",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider_name = Column(String(64), nullable=False)
    model = Column(String(255), nullable=False)
    rate = Column(Float, default=1.0, nullable=False)  # 0=免费, 1=默认
    # auto=上游目录同步(随渠道刷新覆盖); manual=用户手填(永不被覆盖)
    source = Column(String(16), default="manual", nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


# ============================================================
# 请求链路日志表
# ============================================================
class RequestTrace(Base):
    __tablename__ = "request_traces"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    request_id = Column(String(36), nullable=False, index=True)  # UUID

    # 请求输入
    original_model = Column(String(128), nullable=False)  # 客户端原始 model 字段
    resolved_model = Column(String(128), nullable=True)   # 别名/路由组解析后的目标
    is_stream = Column(Boolean, default=False)
    is_pipeline = Column(Boolean, default=False)          # 是否编排请求
    pipeline_mode = Column(String(16), nullable=True)     # relay / review
    request_type = Column(String(16), default="real")     # real 真实请求 / speed_test 速度探测

    # 实际选择
    provider_name = Column(String(64), nullable=True)
    model_name = Column(String(128), nullable=True)
    selection_strategy = Column(String(16), nullable=True)  # quality / fastest / stable
    selection_reason = Column(String(256), nullable=True)   # 选择原因

    # 熔断/冷却
    circuit_breaker_hit = Column(Boolean, default=False)
    rate_limit_hit = Column(Boolean, default=False)
    cooldown_seconds = Column(Integer, nullable=True)

    # 性能
    latency_ms = Column(Integer, nullable=True)
    success = Column(Boolean, default=True)
    error_msg = Column(Text, nullable=True)

    # Token 用量
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)

    # 编排阶段详情 (JSON)
    stages_detail = Column(JSON, nullable=True)
    # 例: [{"stage": 0, "provider": "NVIDIA", "model": "deepseek-v3", "latency_ms": 1200, "success": true}]

    created_at = Column(DateTime, default=datetime.now, index=True)


# ============================================================
# 聊天记录表 — 每次请求的输入输出全文与长度
# ============================================================
class ChatLog(Base):
    __tablename__ = "chat_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    request_id = Column(String(36), nullable=True, index=True)  # 关联 trace 的 request_id

    # 请求输入
    original_model = Column(String(128), nullable=False)  # 客户端原始 model 字段
    provider_name = Column(String(64), nullable=True)    # 实际命中渠道
    model_name = Column(String(128), nullable=True)      # 实际命中模型

    # 请求/回复全文
    prompt_text = Column(Text, nullable=True)   # 原文外置后为"头+尾预览"(全文在归档文件)
    completion_text = Column(Text, nullable=True)  # 输出全文(失败时为空)
    prompt_archive = Column(String(16), nullable=True)  # 原文归档月份 "YYYY-MM", NULL=未外置

    # 长度统计(字符数)
    prompt_chars = Column(Integer, default=0)
    completion_chars = Column(Integer, default=0)
    total_chars = Column(Integer, default=0)

    # token 用量(非流式有, 流式可能为空)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)

    # 元数据
    is_stream = Column(Boolean, default=False)
    is_pipeline = Column(Boolean, default=False)
    pipeline_mode = Column(String(16), nullable=True)

    # 性能
    latency_ms = Column(Integer, nullable=True)
    success = Column(Boolean, default=True)
    error_msg = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.now, index=True)


# ============================================================
# 经验库表 — 从聊天记录沉淀的经验/教训/标准流程
# 参考 OpenCovibe 经验引擎: lesson(教训)/fact(事实)/sop(标准流程)
# 定时任务从 chat_logs 提炼, 下次调用时注入到模型上下文
# ============================================================
class ExperienceLog(Base):
    __tablename__ = "experience_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    kind = Column(String(16), default="lesson")  # lesson / fact / sop
    title = Column(String(256), nullable=False)
    content = Column(Text, nullable=False)        # 经验/教训正文
    source_model = Column(String(128), nullable=True)  # 来源模型
    source_chat_id = Column(Integer, nullable=True)    # 来源 chat_logs.id
    pinned = Column(Boolean, default=False)            # 钉住豁免自动淘汰
    verify_fail_count = Column(Integer, default=0)     # 被验证无效次数
    created_at = Column(DateTime, default=datetime.now, index=True)


# ============================================================
# 每日总结表 — 每天自动生成的经验/教训总结
# ============================================================
class DailySummary(Base):
    __tablename__ = "daily_summaries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    summary_date = Column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    content = Column(Text, nullable=False)      # 当日经验教训总结
    daily_stats = Column(JSON, nullable=True)   # 当日请求/token/成功数等
    created_at = Column(DateTime, default=datetime.now)


# ============================================================
# 模型测速历史表
# ============================================================
class ModelSpeedLog(Base):
    __tablename__ = "model_speed_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider_name = Column(String(64), nullable=False)
    model_name = Column(String(256), nullable=False)
    prefixed_id = Column(String(320), nullable=False)  # provider-model
    latency_ms = Column(Integer, nullable=True)
    available = Column(Boolean, default=False)
    error_msg = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now, index=True)


# ============================================================
# 免费模型搜索 - 帖子表
# ============================================================
class FreeModelPost(Base):
    __tablename__ = "free_model_posts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(32), nullable=False, index=True)  # gitee, github, baidu, toutiao, wechat
    url = Column(String(512), nullable=False)
    title = Column(String(256), nullable=False)
    author = Column(String(128), nullable=True)
    published_at = Column(DateTime, nullable=True)
    fetched_at = Column(DateTime, default=datetime.now, nullable=False, index=True)
    raw_html_path = Column(String(512), nullable=True)  # 保存原始HTML的路径
    content_snippet = Column(Text, nullable=True)  # 抽取的内容片段
    hash_key = Column(String(64), nullable=False, index=True)  # 用于去重的哈希键
    
    # 复合唯一索引防止同一来源同一URL重复
    __table_args__ = (
        UniqueConstraint('source', 'url', name='uix_free_model_post_source_url'),
    )


# ============================================================
# 免费模型搜索 - 报告表
# ============================================================
class FreeModelReport(Base):
    __tablename__ = "free_model_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_date = Column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    post_ids = Column(JSON, nullable=False)  # 关注的post ID列表
    summary = Column(Text, nullable=True)  # LLM生成的摘要
    feasibility_score = Column(Float, nullable=True)  # 平均可行性得分 1-5
    truthfulness_score = Column(Float, nullable=True)  # 平均真实性得分 1-5
    status = Column(String(16), default="completed")  # running, completed, failed
    finished_at = Column(DateTime, nullable=True)
    file_path = Column(String(512), nullable=True)  # 报告文件保存路径
    created_at = Column(DateTime, default=datetime.now, nullable=False, index=True)


# ============================================================
# 免费模型搜索 - 评估表
# ============================================================
class FreeModelEvaluation(Base):
    __tablename__ = "free_model_evaluations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    report_id = Column(Integer, ForeignKey("free_model_reports.id"), nullable=False, index=True)
    post_id = Column(Integer, ForeignKey("free_model_posts.id"), nullable=False, index=True)
    feasibility_score = Column(Integer, nullable=False)  # 1-5分
    truthfulness_score = Column(Integer, nullable=False)  # 1-5分
    reason = Column(Text, nullable=True)  # 评估原因
    created_at = Column(DateTime, default=datetime.now, nullable=False, index=True)
    
    # 关系定义
    report = relationship("FreeModelReport", backref="evaluations")
    post = relationship("FreeModelPost", backref="evaluations")


# ============================================================
# 数据库初始化
# ============================================================
_engine = None

def get_db_url() -> str:
    """获取数据库 URL,默认 SQLite 用绝对路径
    优先级: DATABASE_URL > GW_DB_PATH(桌面版 exe 目录) > backend/data/gateway.db
    """
    import os
    from backend.config import settings
    url = os.getenv("DATABASE_URL", settings.DATABASE_URL)
    if url:
        return url
    # 桌面模式: desktop.py 设置 GW_DB_PATH 指向 exe 旁的持久化目录,
    # 否则单文件 exe 每次解包到不同临时目录导致数据丢失
    gw_path = os.getenv("GW_DB_PATH")
    if gw_path:
        Path(gw_path).parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{Path(gw_path).as_posix()}"
    db_path = (Path(__file__).parent.parent / "data" / "gateway.db").as_posix()
    return f"sqlite:///{db_path}"


def _get_engine():
    """获取共享引擎(单例)"""
    global _engine
    if _engine is None:
        from sqlalchemy import create_engine, event
        _engine = create_engine(get_db_url(), connect_args={"check_same_thread": False})
        if _engine.url.get_backend_name() == "sqlite":
            # WAL + NORMAL: 聊天记录页等读操作不再被并发写入阻塞;
            # busy_timeout 兜住偶发写锁竞争
            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, rec):
                cur = dbapi_conn.cursor()
                try:
                    cur.execute("PRAGMA journal_mode=WAL")
                    cur.execute("PRAGMA synchronous=NORMAL")
                    cur.execute("PRAGMA busy_timeout=5000")
                finally:
                    cur.close()
    return _engine


# 高频列表/统计查询的幂等索引。chat_logs 单行含数百 KB 大文本, 没有覆盖索引时
# count()/avg()/sum() 都要全表扫几百 MB —— 实测聊天列表 5.8s / 统计 5.2s。
_PERF_INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS ix_chat_logs_user_created "
    "ON chat_logs(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_chat_logs_stats_cover "
    "ON chat_logs(user_id, created_at, success, is_pipeline, "
    "prompt_chars, completion_chars, total_chars)",
    "CREATE INDEX IF NOT EXISTS ix_request_traces_user_created "
    "ON request_traces(user_id, created_at)",
]


def _ensure_perf_indexes(engine):
    """create_all 不会给已存在的表补索引, 这里幂等补齐"""
    import sqlalchemy as sa
    try:
        with engine.begin() as conn:
            for ddl in _PERF_INDEX_DDL:
                conn.execute(sa.text(ddl))
    except Exception:
        import logging
        logging.getLogger("gateway").exception("DB perf index creation failed")


def create_tables():
    """同步创建所有表 + 轻量迁移(为已存在的库补充新增列)"""
    engine = _get_engine()
    Base.metadata.create_all(engine)
    _migrate_columns(engine)
    _ensure_perf_indexes(engine)
    return engine


def _migrate_columns(engine):
    """为已存在的表补充/重命名列 (SQLite ALTER TABLE ADD COLUMN + RENAME COLUMN)"""
    import sqlalchemy as sa
    from sqlalchemy import inspect
    import logging
    log = logging.getLogger("gateway")

    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())

        # ── 1. orders 表列重命名（旧版用 tier/amount，新版用 plan_name/amount_cents） ──
        if "orders" in tables:
            existing = {c["name"] for c in inspector.get_columns("orders")}
            renames = {
                "tier": "plan_name",
                "amount": "amount_cents",
            }
            with engine.begin() as conn:
                for old, new in renames.items():
                    if old in existing and new not in existing:
                        conn.execute(sa.text(f'ALTER TABLE orders RENAME COLUMN {old} TO {new}'))
                        log.info("DB migration: orders.%s → %s", old, new)
                # 重命名后刷新列信息
                existing = {c["name"] for c in inspector.get_columns("orders")}
                # 新增缺失列
                new_cols = {
                    "amount_paid": "INTEGER DEFAULT 0",
                    "payment_ref": "VARCHAR(128) DEFAULT ''",
                    "remark": "VARCHAR(512) DEFAULT ''",
                }
                for col, coltype in new_cols.items():
                    if col not in existing:
                        conn.execute(sa.text(f'ALTER TABLE orders ADD COLUMN {col} {coltype}'))
                        log.info("DB migration: orders ADD COLUMN %s", col)

        # ── 2. 补充列（简单 ADD COLUMN） ──
        add_cols = {
            "users": {
                "machine_code": "VARCHAR(128)",
                "license_locked": "BOOLEAN",
            },
            "request_traces": {
                "request_type": "VARCHAR(16)",
            },
            "providers": {
                "max_concurrency": "INTEGER",
                "min_interval_ms": "INTEGER",
            },
            "model_rates": {
                "source": "VARCHAR(16) DEFAULT 'manual'",
            },
        }
        for table, cols in add_cols.items():
            if table not in tables:
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            with engine.begin() as conn:
                for col, coltype in cols.items():
                    if col not in existing:
                        conn.execute(sa.text(
                            f'ALTER TABLE {table} ADD COLUMN {col} {coltype}'
                        ))
                        log.info("DB migration: %s ADD COLUMN %s", table, col)

        # ── 4. qoder 模型 ID: 内部代号 → 真名(显示 ID) ──
        # 老库里 qoder/qodercn 的模型写的是上游内部代号(qfmodel/dmodel...),
        # 用户在路由分组界面完全看不懂。新版本对外一律真名(Qwen3.8-Flash),
        # 签名时由 resolve_model_key 反查回代号; 这里把存量数据改写过去,
        # 保持 provider.models / 路由组成员 / 倍率表 / 禁用表一致。
        _migrate_qoder_display_ids(engine, tables, log)

        # ── 5. 路由组同名去重 + 唯一索引 ──
        # 旧库遗留: 同名路由组多行(历史版本/并发写入), 读路径 dict 按 name 取
        # 最后一行、写路径却只改第一行 → 编辑保存到展示外的行上("剔除又勾选")。
        # 去重保留 id 最大的一行(即读路径一直生效的那行), 行为与迁移前一致。
        if "routers" in tables:
            with engine.begin() as conn:
                conn.execute(sa.text(
                    "DELETE FROM routers WHERE id NOT IN ("
                    "  SELECT MAX(id) FROM routers GROUP BY user_id, name)"
                ))
            try:
                conn = engine.connect()
                try:
                    conn.execute(sa.text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_routers_user_name "
                        "ON routers(user_id, name)"
                    ))
                    conn.commit()
                finally:
                    conn.close()
            except Exception:
                log.exception("routers unique index creation failed")

    except Exception:
        log.exception("DB column migration failed")


_QODER_CN_TABLE = {
    "qfmodel": "Qwen3.8-Flash", "qmodel_38max": "Qwen3.8-Max",
    "auto": "Auto", "qmodel_latest": "Qwen3.7-Max",
    "qmodel": "Qwen3.7-Plus", "q37fmodel": "Qwen3.7-Flash",
    "dmodel": "DeepSeek-V4-Pro", "dfmodel": "DeepSeek-Flash",
    "gmodel": "GLM-5.3", "gfmodel": "GLM-5.3-Flash",
    "gm51model": "GLM-5.2", "kmodel_latest": "Kimi-K3",
    "kmodel": "Kimi-K2.8-Preview", "mmodel": "MiniMax-M2.7",
}
_QODER_INTL_TABLE = {
    "qfmodel": "Qwen3.8-Flash", "qmodel_38max": "Qwen3.8-Max",
    "auto": "Auto", "ultimate": "Ultimate", "performance": "Performance",
    "efficient": "Efficient", "smodel": "Sonus", "cmodel": "Cantus",
    "qmodel_latest": "Qwen3.7-Max", "qmodel": "Qwen3.7-Plus",
    "kmodel_latest": "Kimi-K3", "kmodel": "Kimi-K2.8-Preview",
    "gmodel": "GLM-5.3", "gfmodel": "GLM-5.3-Flash",
    "dmodel": "DeepSeek-V4-Pro", "dfmodel": "DeepSeek-Flash",
    "mmodel": "MiniMax-M3",
}


def _qoder_display_name(provider_name, m):
    """旧内部代号 → 真名(无映射时原样返回, 不误伤其它模型)。"""
    table = _QODER_INTL_TABLE
    if provider_name == "qodercn":
        table = _QODER_CN_TABLE
    return table.get(m, m)


def _qoder_rename_list(provider_name, items):
    """JSON 列值逐项代号→真名, 返回 (新列表, 是否变化)。

    列可能是 JSON 字符串(裸 SQL 读出)或已解析的 list(ORM 读出), 两种都接受。
    """
    import json as _json
    if isinstance(items, str):
        try:
            items = _json.loads(items)
        except Exception:
            items = []
    if not isinstance(items, list):
        items = list(items) if items else []
    out, changed = [], False
    for m in items:
        new = _qoder_display_name(provider_name, m) if isinstance(m, str) else m
        changed = changed or new != m
        out.append(new)
    return out, changed


def _qoder_rename_router_models(models):
    """路由组成员: 「渠道名-代号」→「渠道名-真名」(不限组名, 混合组也能改)"""
    import json as _json
    if isinstance(models, str):
        try:
            models = _json.loads(models)
        except Exception:
            models = []
    if not isinstance(models, list):
        models = list(models) if models else []
    out, changed = [], False
    for m in models or []:
        if isinstance(m, str):
            for pname in ("qodercn", "qoder"):     # 长名优先, 免得 qoder 抢先匹配
                head = f"{pname}-"
                if m.startswith(head):
                    base = m[len(head):]
                    newbase = _qoder_display_name(pname, base)
                    if newbase != base:
                        m, changed = f"{head}{newbase}", True
                    break
        out.append(m)
    return out, changed


def _migrate_qoder_display_ids(engine, tables, log):
    """把 qoder/qodercn 的存量模型 ID 从内部代号改写为对外真名。

    改写范围: providers.models / providers.disabled_models /
    routers.models(任意路由组里的「qoder-代号」「qodercn-代号」) /
    model_rates.model / dead_models.model / downed_models.model。
    重复执行幂等(真名映射回自身)。

    注意 providers/routers 的 models 是 JSON 列, 必须回写 json.dumps 后的字符串,
    直接绑 list 会被 SQLite 拒绝(sqlite3.ProgrammingError: type 'list' not supported)。
    """
    if not {"providers", "routers"}.issubset(tables):
        return
    import json as _json
    import sqlalchemy as sa
    try:
        with engine.begin() as conn:
            for row in conn.execute(sa.text(
                    "SELECT id, name, models, disabled_models FROM providers "
                    "WHERE name IN ('qoder','qodercn')")).mappings():
                nm, c1 = _qoder_rename_list(row["name"], row["models"])
                nd, c2 = _qoder_rename_list(row["name"], row["disabled_models"])
                if c1 or c2:
                    conn.execute(sa.text(
                        "UPDATE providers SET models=:m, disabled_models=:d "
                        "WHERE id=:pid"),
                        {"m": _json.dumps(nm, ensure_ascii=False),
                         "d": _json.dumps(nd, ensure_ascii=False),
                         "pid": row["id"]})
            for row in conn.execute(sa.text(
                    "SELECT id, models FROM routers")).mappings():
                rm, changed = _qoder_rename_router_models(row["models"])
                if changed:
                    conn.execute(sa.text(
                        "UPDATE routers SET models=:m WHERE id=:id"),
                        {"m": _json.dumps(rm, ensure_ascii=False),
                         "id": row["id"]})
    except Exception:
        log.exception("DB qoder model name migration failed")
        return
    # 列值层面(纯文本列, 不走同一事务):
    for table in ("model_rates", "dead_models", "downed_models"):
        if table in tables:
            _rename_qoder_rows(engine, table, log)


def _rename_qoder_rows(engine, table, log):
    """model_rates / dead_models / downed_models: (provider_name, model) 代号→真名。

    这三张表都有 (user_id, provider_name, model) 唯一约束。真名行已存在时
    不能直接 UPDATE(会撞唯一键), 改为删掉旧代号行 —— 新行是同一份信息,
    目标是保持"一个模型一行"。
    """
    import sqlalchemy as sa
    try:
        with engine.begin() as conn:
            rows = list(conn.execute(sa.text(
                f"SELECT id, user_id, provider_name, model FROM {table} "
                f"WHERE provider_name IN ('qoder','qodercn')")).mappings())
            for row in rows:
                new = _qoder_display_name(row["provider_name"], row["model"])
                if new == row["model"]:
                    continue
                exists = conn.execute(sa.text(
                    f"SELECT 1 FROM {table} WHERE user_id=:u AND provider_name=:p "
                    f"AND model=:m"), {"u": row["user_id"],
                                       "p": row["provider_name"], "m": new}).first()
                if exists:
                    conn.execute(sa.text(f"DELETE FROM {table} WHERE id=:id"),
                                 {"id": row["id"]})
                else:
                    conn.execute(sa.text(
                        f"UPDATE {table} SET model=:m WHERE id=:id"),
                        {"m": new, "id": row["id"]})
    except Exception:
        log.exception("%s qoder rename failed", table)


def get_session():
    """获取同步 session"""
    from sqlalchemy.orm import sessionmaker
    engine = _get_engine()
    Session = sessionmaker(bind=engine)
    return Session()