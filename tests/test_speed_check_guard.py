# ============================================================
# 后台测速守卫: 不得对"公共免费额度"车道组发真实请求
# ============================================================
# 背景(2026-10-24 实测): 测速每 2h 对每个路由组发一次真实请求, 而 opencode 组
# 内 failover 在全部 429 时会一路试完 9 个模型 = 一次测速 9 个真请求, 一天 100+
# 次, 把公共匿名池的免费额度提前烧光, 用户自己还没用就没了。
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from backend.api import providers as pm


def test_default_skip_groups_include_free_lanes():
    assert {"opencode", "kilo"} <= pm._speed_check_skip_groups()


def test_skip_groups_parsing(monkeypatch):
    """逗号分隔 + 空白容忍 + 空串=不跳过。"""
    from backend.config import settings
    monkeypatch.setattr(settings, "SPEED_CHECK_SKIP_GROUPS", " opencode , kilo ,, ", raising=False)
    assert pm._speed_check_skip_groups() == {"opencode", "kilo"}
    monkeypatch.setattr(settings, "SPEED_CHECK_SKIP_GROUPS", "", raising=False)
    assert pm._speed_check_skip_groups() == set()


def test_skip_groups_survives_missing_setting(monkeypatch):
    """配置缺失(老版本 .env/打包残留)也不能抛, 退化成不跳过。"""
    from backend.config import settings
    monkeypatch.delattr(settings, "SPEED_CHECK_SKIP_GROUPS", raising=False)
    assert pm._speed_check_skip_groups() == set()


@pytest.mark.asyncio
async def test_speed_check_skips_free_lane_groups(monkeypatch):
    """测速主流程: 免密组一次请求都不发, 其它组照常测。"""
    import tempfile

    import backend.models as m
    m._engine = None
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp.name}")
    monkeypatch.setenv("GW_STATE_DIR", tempfile.mkdtemp(prefix="spd_"))
    m.create_tables()

    from backend.auth import security as sec
    s = m.get_session()
    u = m.User(username="spd", email="spd@t.com",
               password_hash=sec.hash_password("Aa123456!"), api_key="sk-spd-key")
    s.add(u)
    s.commit()
    uid, key = u.id, u.api_key
    s.add(m.Router(user_id=uid, name="opencode", models=["opencode-a-free"], strategy="failover"))
    s.add(m.Router(user_id=uid, name="kilo", models=["kilo-x-free"], strategy="failover"))
    s.add(m.Router(user_id=uid, name="workbuddy", models=["workbuddy-glm"], strategy="failover"))
    s.add(m.Provider(user_id=uid, name="workbuddy", base_url="https://wb.test/v2",
                     api_key_encrypted="", models=["glm"], enabled=True))
    s.commit()
    s.close()

    seen = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "hi"}}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            seen.append(json["model"])
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    await pm._do_speed_check(uid)

    assert "opencode" not in seen      # 关键: 公共额度组零请求
    assert "kilo" not in seen
    assert seen == ["workbuddy"]       # 付费/私有组照常测

    m._engine = None
