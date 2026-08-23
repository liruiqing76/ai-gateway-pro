# -*- coding: utf-8 -*-
"""验证: ①探测不写 UsageLog 不占额度 ②/api/usage/daily 按日聚合补零 ③额度零点重置"""
import os, sys, json
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_v = ROOT / ".tmp-verify" / "nested"
_v.mkdir(parents=True, exist_ok=True)
os.environ["GW_DB_PATH"] = str(_v / "data.db")
# 每次全新开始(上轮残留用户会撞 api_key 唯一约束)
for stale in (_v / "data.db",):
    if stale.exists():
        stale.unlink()

from fastapi.testclient import TestClient
from backend.main import app
from backend.auth.middleware import get_current_user_any, get_current_user_from_jwt, get_current_user_from_api_key, check_quota
from backend.models import get_session, User, RequestTrace, UsageLog

from backend.models import create_tables
create_tables()

s = get_session()
u = User(email="u1@t.io", username="u1", password_hash="x", api_key="sk-u1",
         is_active=True, quota_daily=1000)
s.add(u); s.commit(); s.refresh(u)
uid = u.id
now = datetime.now()

# 历史真实调用: 昨天2条(共300 token) + 今天1条(120 token)
for d, tk in [(( now - timedelta(days=1)).replace(hour=10), 180),
              (( now - timedelta(days=1)).replace(hour=15), 120),
              ( now.replace(hour=9), 120)]:
    s.add(UsageLog(user_id=uid, provider_name="p", model_name="m",
                   prompt_tokens=tk - 20, completion_tokens=20,
                   total_tokens=tk, success=True, created_at=d))
# 用户当前额度: 已用 999, reset_at = 昨天(应触发零点重置)
u.quota_used_today = 999
u.quota_reset_at = now - timedelta(hours=3)
s.merge(u); s.commit(); s.close()


class FU: id = uid
app.dependency_overrides[get_current_user_any] = lambda: FU()
app.dependency_overrides[get_current_user_from_jwt] = lambda: FU()
app.dependency_overrides[get_current_user_from_api_key] = lambda: FU()
app.dependency_overrides[check_quota] = lambda: FU()   # 绕过依赖注入版, 直接测内部逻辑另做

with TestClient(app) as c:
    # ① daily 聚合: 昨天calls=2 tokens=300, 今天calls=1 tokens=120, 其余补零
    r = c.get("/api/usage/daily", params={"days": 7}).json()["data"]
    assert len(r) == 7
    yest = r[-2]; today = r[-1]
    assert yest["date"] == (now - timedelta(days=1)).strftime("%Y-%m-%d"), yest
    assert yest["calls"] == 2 and yest["tokens"] == 300, yest
    assert today["calls"] == 1 and today["tokens"] == 120, today
    assert sum(x["tokens"] for x in r) == 420

    # ② summary 不变
    sm = c.get("/api/usage/summary").json()
    assert sm["total"] == 3 and sm["today_tokens"] == 120

print("daily/summary PASS:", json.dumps({"yesterday": yest, "today": today}, ensure_ascii=False))

# ③ 探测模式: set_probe_mode(True) 后 _log_usage_and_quota 应跳过
import backend.api.proxy as P
from backend.core.chat_log import set_probe_mode
s = get_session()
uu = s.query(User).filter_by(id=uid).first()
before_log = s.query(UsageLog).filter_by(user_id=uid).count()
set_probe_mode(True)
P._log_usage_and_quota(user=uu, provider_name="p", model_name="grp", pt=1, ct=0)
set_probe_mode(False)
after_log = s.query(UsageLog).filter_by(user_id=uid).count()
assert before_log == after_log == 3, f"探测不应写 UsageLog: {before_log} -> {after_log}"
assert uu.quota_used_today == 999, f"探测不应占额度(应保持999不变), 实际 {uu.quota_used_today}"
# 真实调用照常计数
P._log_usage_and_quota(user=uu, provider_name="p", model_name="m", pt=10, ct=5)
s.refresh(uu)
assert s.query(UsageLog).filter_by(user_id=uid).count() == 4
assert uu.quota_used_today == 1000
s.close()
print("probe-skip PASS: UsageLog 不增, quota_used_today =", 1)

# ④ check_quota 零点重置逻辑(直接调内部协程; 需要真实 ORM 实例)
import asyncio
from backend.auth.middleware import check_quota as cq
s = get_session()
uu = s.query(User).filter_by(id=uid).first()
uu.quota_used_today = 500
uu.quota_reset_at = datetime.now() - timedelta(hours=1)   # 已过期
s.merge(uu); s.commit(); s.close()

def fresh_user():
    s = get_session()
    try:
        return s.query(User).filter_by(id=uid).first()
    finally:
        s.close()

asyncio.run(cq(fresh_user()))

s = get_session()
uu = s.query(User).filter_by(id=uid).first()
exp = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
assert uu.quota_used_today == 0, uu.quota_used_today
assert abs((uu.quota_reset_at - exp).total_seconds()) < 60, uu.quota_reset_at
assert uu.quota_reset_at.hour == 0 and uu.quota_reset_at.minute == 0
s.close()
print("midnight-reset PASS: used=0, next reset =", uu.quota_reset_at)

# ⑤ NULL 锚点: 只对齐不清零(超额老用户不变相续命)
uu.quota_used_today = 500
uu.quota_reset_at = None
s.merge(uu); s.commit(); s.close()
asyncio.run(cq(fresh_user()))
s = get_session()
uu = s.query(User).filter_by(id=uid).first()
assert uu.quota_used_today == 500, f"NULL锚点不应清零, 实际 {uu.quota_used_today}"
assert uu.quota_reset_at is not None and uu.quota_reset_at.hour == 0
s.close()
print("null-anchor PASS: used 保持500, reset_at 对齐明天零点")

# 清理
s = get_session()
for tbl in (UsageLog,):
    s.query(tbl).filter_by(user_id=uid).delete()
s.query(User).filter_by(id=uid).delete()
s.commit(); s.close()
print("ALL USAGE E2E PASS")
