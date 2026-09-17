#!/usr/bin/env python3
# ============================================================
# 死模型拉黑登记 — 404/410 确认下架/停用后, 永久剔除。
#
# 被拉黑后的效果(写库持久, 重启不丢):
#   - 运行时候选/兜底池不再尝试(api/proxy.py 候选过滤)
#   - 刷新模型/模板落库时过滤掉(fetch 结果里直接剔除)
#   - 自动分类 / 渠道重建不再生成(build_router_groups 按 user_id 过滤)
# 用户显式手动加回不拦(那是用户意图); 若仍 404 会被再次自动移除+拉黑(幂等)。
# ============================================================
from backend.models import get_session, DeadModel


def ban_model(user_id: int, provider_name: str, model: str) -> None:
    """登记一条死模型拉黑(已存在则忽略)。"""
    s = get_session()
    try:
        exists = s.query(DeadModel.id).filter_by(
            user_id=user_id, provider_name=provider_name, model=model,
        ).first()
        if not exists:
            s.add(DeadModel(user_id=user_id, provider_name=provider_name, model=model))
            s.commit()
    finally:
        s.close()


def banned_keys(user_id: int) -> set:
    """返回 {provider_name||model} 集合, 供候选/落库过滤一次取全。"""
    s = get_session()
    try:
        return {
            f"{r.provider_name}||{r.model}"
            for r in s.query(DeadModel).filter_by(user_id=user_id).all()
        }
    finally:
        s.close()


def is_banned(user_id: int, provider_name: str, model: str) -> bool:
    s = get_session()
    try:
        return (
            s.query(DeadModel.id).filter_by(
                user_id=user_id, provider_name=provider_name, model=model,
            ).first()
            is not None
        )
    finally:
        s.close()