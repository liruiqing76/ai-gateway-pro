# ============================================================
# 智能网关 — 数据库模型
# 支持 SQLite(本地) / MySQL 8.0(远程)
# ============================================================

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, Float, Boolean,
    DateTime, ForeignKey, JSON, create_engine
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

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(64), nullable=False)
    models = Column(JSON, default=list)
    strategy = Column(String(16), default="quality")  # quality / cost / random / failover
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("User", back_populates="routers")


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
    prompt_text = Column(Text, nullable=True)            # 输入 messages 拼接文本
    completion_text = Column(Text, nullable=True)        # 输出全文(失败时为空)

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
        from sqlalchemy import create_engine
        _engine = create_engine(get_db_url(), connect_args={"check_same_thread": False})
    return _engine


def create_tables():
    """同步创建所有表"""
    engine = _get_engine()
    Base.metadata.create_all(engine)
    return engine


def get_session():
    """获取同步 session"""
    from sqlalchemy.orm import sessionmaker
    engine = _get_engine()
    Session = sessionmaker(bind=engine)
    return Session()