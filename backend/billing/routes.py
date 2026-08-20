# ============================================================
# 智能网关 — 计费路由
# 定价查询 / 充值下单 / 订单查询
# ============================================================

import secrets
from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from backend.auth.middleware import get_current_user_from_jwt
from backend.models import get_session, User, Order
from backend.billing.plans import PLANS, get_plan

router = APIRouter(prefix="/api/billing", tags=["billing"])


class PlaceOrderRequest(BaseModel):
    plan_name: str
    payment_method: str = "manual"  # 目前手动充值,后续接微信支付


@router.get("/plans")
async def list_plans():
    """列出所有定价计划"""
    return [{"plan": k, **v} for k, v in PLANS.items()]


@router.get("/me")
async def my_billing(user: User = Depends(get_current_user_from_jwt)):
    """当前用户的计费信息"""
    return {
        "tier": user.tier,
        "tier_expires_at": user.tier_expires_at.isoformat() if user.tier_expires_at else None,
        "quota_daily": user.quota_daily,
        "quota_used_today": user.quota_used_today or 0,
        "quota_reset_at": user.quota_reset_at.isoformat() if user.quota_reset_at else None,
    }


@router.post("/orders")
async def place_order(
    data: PlaceOrderRequest,
    user: User = Depends(get_current_user_from_jwt),
):
    """下单(目前手动充值,管理员后台确认)"""
    plan = get_plan(data.plan_name)
    if not plan:
        raise HTTPException(400, "无效的计划")

    session = get_session()
    try:
        order_no = datetime.now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(4)
        order = Order(
            user_id=user.id,
            order_no=order_no,
            plan_name=data.plan_name,
            amount_cents=plan["price_cents"],
            payment_method=data.payment_method,
            payment_status="pending",
        )
        session.add(order)
        session.commit()
        session.refresh(order)
        return {
            "order_no": order.order_no,
            "plan_name": order.plan_name,
            "amount_cents": order.amount_cents,
            "amount_yuan": f"¥{order.amount_cents / 100:.2f}",
            "payment_status": order.payment_status,
            "payment_method": order.payment_method,
        }
    finally:
        session.close()


@router.get("/orders")
async def list_orders(user: User = Depends(get_current_user_from_jwt)):
    session = get_session()
    try:
        orders = session.query(Order).filter_by(user_id=user.id).order_by(Order.created_at.desc()).limit(20).all()
        return [{
            "order_no": o.order_no,
            "plan_name": o.plan_name,
            "amount_cents": o.amount_cents,
            "payment_status": o.payment_status,
            "paid_at": o.paid_at.isoformat() if o.paid_at else None,
            "created_at": o.created_at.isoformat() if o.created_at else None,
        } for o in orders]
    finally:
        session.close()


# ============================================================
# 管理后台: 标记订单已支付
# ============================================================
class MarkPaidRequest(BaseModel):
    order_no: str


@router.post("/admin/mark-paid")
async def mark_paid(
    data: MarkPaidRequest,
    user: User = Depends(get_current_user_from_jwt),
):
    if user.role != "admin":
        raise HTTPException(403, "仅管理员可操作")

    session = get_session()
    try:
        order = session.query(Order).filter_by(order_no=data.order_no).first()
        if not order:
            raise HTTPException(404, "订单不存在")
        if order.payment_status == "paid":
            raise HTTPException(400, "订单已支付")

        order.payment_status = "paid"
        order.paid_at = datetime.now()

        # 升级用户 tier
        buyer = session.query(User).filter_by(id=order.user_id).first()
        if buyer:
            buyer.tier = order.plan_name
            buyer.quota_daily = PLANS[order.plan_name]["daily_quota"]
            if order.plan_name == "pro":
                from datetime import timedelta
                buyer.tier_expires_at = datetime.now() + timedelta(days=30)
            elif order.plan_name == "enterprise":
                from datetime import timedelta
                buyer.tier_expires_at = datetime.now() + timedelta(days=365)

        session.commit()
        return {"ok": True, "msg": f"订单 {data.order_no} 已支付,用户升级为 {order.plan_name}"}
    finally:
        session.close()