# -*- coding: utf-8 -*-
"""端到端验证: 链路 request_type 标记/筛选 + 分组筛选 + stats.probe_count(临时隔离库, 不碰开发数据)"""
import os, sys, tempfile, json, shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 临时库放工作区嵌套目录: main.py 会在 GW_DB_PATH 上两级建 logs/, 避免写系统 Temp
_VERIFY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp-verify", "nested")
os.makedirs(_VERIFY_DIR, exist_ok=True)
os.environ["GW_DB_PATH"] = os.path.join(_VERIFY_DIR, "data.db")

from fastapi.testclient import TestClient
from backend.main import app
from backend.auth.middleware import get_current_user_any
from backend.models import get_session, User, RequestTrace, create_tables

create_tables()

s = get_session()
u = User(email="probe-test@example.com", username="probetest", password_hash="x", api_key="sk-probe-test", is_active=True)
s.add(u); s.commit(); s.refresh(u)
uid = u.id
for i, rt in enumerate(["real", "speed_test", None]):
    s.add(RequestTrace(user_id=uid, request_id=f"req{i}", original_model="quick",
                       resolved_model="deepseek-v3" if rt == "speed_test" else None,
                       model_name="m1", request_type=rt))
s.commit(); s.close()

class FakeUser: id = uid
app.dependency_overrides[get_current_user_any] = lambda: FakeUser()

with TestClient(app) as c:
    r1 = c.get("/api/traces").json()
    r2 = c.get("/api/traces", params={"request_type": "real"}).json()
    r3 = c.get("/api/traces", params={"request_type": "speed_test"}).json()
    r4 = c.get("/api/traces", params={"group": "quick"}).json()
    r5 = c.get("/api/traces", params={"group": "nope"}).json()
    st = c.get("/api/traces/stats", params={"days": 7}).json()
    d3 = r3["data"][0]

    assert r1["total"] == 3, f"全部=3 实际 {r1['total']}"
    assert r2["total"] == 2, f"真实(real+NULL)=2 实际 {r2['total']}"
    assert r3["total"] == 1, f"探测=1 实际 {r3['total']}"
    assert d3["request_type"] == "speed_test"
    assert r4["total"] == 3 and r5["total"] == 0, f"分组筛选异常 {r4['total']}/{r5['total']}"
    assert st["probe_count"] == 1, f"probe_count=1 实际 {st['probe_count']}"

    print("E2E PASS:")
    print(json.dumps({
        "all": r1["total"], "real_incl_legacy_null": r2["total"],
        "speed_test": r3["total"], "group_quick": r4["total"],
        "group_none": r5["total"], "stats_probe_count": st["probe_count"],
    }, ensure_ascii=False))

# 清理临时用户
s = get_session()
s.query(RequestTrace).filter_by(user_id=uid).delete()
s.query(User).filter_by(id=uid).delete()
s.commit(); s.close()
