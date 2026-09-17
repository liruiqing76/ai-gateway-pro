#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# 临时下架模型登记 — 429/503/超时/空回复等"暂时性故障"反复出现时自动下架。
#
# 与 dead_models(404/410 永久拉黑)不同: 这里下架是临时的, recovers_at
# 到期后自动恢复参与路由(免费模型偶发恢复是常态, 不能永久封死)。
#   - 路由候选过滤: active_down_keys() 只返回未过期的
#   - 到期自动恢复: 过期记录在查询时懒删除, 模型自然回到可路由状态
# ============================================================
from datetime import datetime, timedelta

from backend.models import get_session, DownedModel

# 默认下架时长(与 circuit.DOWN_COOLDOWN_SECONDS 一致, 见配置处)
DEFAULT_DOWN_SECONDS = 6 * 3600


def down_model(user_id: int, provider_name: str, model: str,
               reason: str = "auto",
               duration_seconds: int = DEFAULT_DOWN_SECONDS):
    """登记(或刷新)一个模型为临时下架; 返回到期时间(datetime)。"""
    s = get_session()
    try:
        row = s.query(DownedModel).filter_by(
            user_id=user_id, provider_name=provider_name, model=model,
        ).first()
        recovers_at = datetime.now() + timedelta(seconds=duration_seconds)
        if row:
            row.reason = reason
            row.downed_at = datetime.now()
            row.recovers_at = recovers_at
        else:
            s.add(DownedModel(
                user_id=user_id, provider_name=provider_name, model=model,
                reason=reason, recovers_at=recovers_at,
            ))
        s.commit()
        return recovers_at
    finally:
        s.close()


def active_down_keys(user_id: int) -> set:
    """返回仍在临时下架期内的 {provider_name||model}(过期的懒删除)。"""
    s = get_session()
    try:
        now = datetime.now()
        rows = s.query(DownedModel).filter_by(user_id=user_id).all()
        active, expired = set(), []
        for r in rows:
            if r.recovers_at and r.recovers_at > now:
                active.add(f"{r.provider_name}||{r.model}")
            else:
                expired.append(r)
        for r in expired:
            s.delete(r)
        if expired:
            s.commit()
        return active
    finally:
        s.close()


def list_downed(user_id: int) -> list:
    """下架模型列表(界面展示用), 含剩余冷却秒数; 过期项顺带清理。"""
    s = get_session()
    try:
        now = datetime.now()
        rows = s.query(DownedModel).filter_by(user_id=user_id).all()
        out, expired = [], []
        for r in rows:
            if r.recovers_at and r.recovers_at > now:
                out.append({
                    "provider_name": r.provider_name,
                    "model": r.model,
                    "reason": r.reason,
                    "downed_at": r.downed_at.isoformat() if r.downed_at else None,
                    "recovers_at": r.recovers_at.isoformat(),
                    "remaining_seconds": int((r.recovers_at - now).total_seconds()),
                })
            else:
                expired.append(r)
        for r in expired:
            s.delete(r)
        if expired:
            s.commit()
        out.sort(key=lambda x: x["remaining_seconds"])
        return out
    finally:
        s.close()


def recover_model(user_id: int, provider_name: str, model: str) -> bool:
    """手动恢复某个下架模型(立即重新参与路由)。返回是否确实移除了记录。"""
    s = get_session()
    try:
        row = s.query(DownedModel).filter_by(
            user_id=user_id, provider_name=provider_name, model=model,
        ).first()
        if not row:
            return False
        s.delete(row)
        s.commit()
        return True
    finally:
        s.close()