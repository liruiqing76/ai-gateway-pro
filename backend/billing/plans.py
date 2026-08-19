# ============================================================
# 智能网关 — 定价计划
# ============================================================

PLANS = {
    "free": {
        "name": "免费版",
        "price_cents": 0,
        "daily_quota": 100,        # 每天 100 次
        "max_providers": 3,         # 最多 3 个渠道
        "features": ["3 个渠道", "每日 100 次", "基础模型"],
    },
    "pro": {
        "name": "Pro 会员",
        "price_cents": 2900,        # ¥29/月
        "daily_quota": 99999,       # 无限
        "max_providers": 20,        # 20 个渠道
        "features": ["20 个渠道", "无限调用", "所有模型", "优先路由", "API 文档"],
    },
    "enterprise": {
        "name": "企业版",
        "price_cents": 99900,       # ¥999/月起
        "daily_quota": 999999,
        "max_providers": 999,
        "features": ["无限渠道", "无限调用", "所有模型", "SLA 99.9%", "私有部署", "专属客服", "SSO 集成"],
    },
}

# 每天自动重置时间(UTC+8 零点)
from datetime import time
RESET_HOUR = 0
RESET_TZ_OFFSET = 8  # Asia/Shanghai


def get_plan(plan_name: str) -> dict | None:
    return PLANS.get(plan_name)


def get_plan_daily_quota(plan_name: str) -> int:
    plan = PLANS.get(plan_name)
    return plan["daily_quota"] if plan else 0


def get_plan_max_providers(plan_name: str) -> int:
    plan = PLANS.get(plan_name)
    return plan["max_providers"] if plan else 0