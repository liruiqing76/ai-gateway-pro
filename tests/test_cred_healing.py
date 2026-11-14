"""凭据自愈 + 统一打包 的单元测试。"""
import asyncio
import json

import pytest

from backend.core import checkin as ck
from backend.core.channel_auth import cline as cline_mod


# ---------------------------------------------------------------- package

def test_package_credentials_cline_keeps_cline_shape():
    creds = {"channel": "cline", "access_token": "workos:abc",
             "refresh_token": "rt", "expires_at": "2030-01-01T00:00:00Z"}
    plain = ck.package_credentials("cline", creds)
    assert plain is not None
    pkg = json.loads(plain)
    assert pkg["channel"] == "cline"
    assert pkg["access_token"] == "workos:abc"
    assert pkg["refresh_token"] == "rt"


def test_package_credentials_unknown_channel_returns_none():
    assert ck.package_credentials("nvidia", {"access_token": "x"}) is None


def test_package_credentials_minimax_shape():
    creds = {"channel": "minimax", "access_token": "mmoat-x",
             "refresh_token": "mmort-y", "expires_at": "4102444800000",
             "token_type": "Bearer"}
    pkg = json.loads(ck.package_credentials("minimax", creds))
    assert pkg["access_token"] == "mmoat-x"
    assert pkg["refresh_token"] == "mmort-y"


# ---------------------------------------------------------------- force

class _FakeClient:
    """只用于让 _ensure_fresh_credentials 的调用签名成立。"""


def test_ensure_fresh_force_bypasses_fresh_window(monkeypatch):
    """force=True 时必须刷新, 即使本地记录的有效期还没到。"""
    called = {}

    async def fake_refresh(client, creds):
        called["hit"] = True
        return {"channel": "cline", "access_token": "workos:new",
                "refresh_token": creds.get("refresh_token", "rt"),
                "expires_at": "2030-01-01T00:00:00Z"}

    monkeypatch.setattr(cline_mod, "refresh_credential", fake_refresh)

    creds = {"channel": "cline", "access_token": "workos:old",
             "refresh_token": "rt", "expires_at": "2999-01-01T00:00:00Z"}
    # 不 force: 未临期 → 原样返回, 不刷新
    same = asyncio.run(ck._ensure_fresh_credentials(_FakeClient(), "cline", creds))
    assert same is creds
    assert "hit" not in called

    fresh = asyncio.run(ck._ensure_fresh_credentials(
        _FakeClient(), "cline", creds, force=True))
    assert called["hit"] is True
    assert fresh["access_token"] == "workos:new"


def test_ensure_fresh_force_without_refresh_token_returns_none():
    creds = {"channel": "cline", "access_token": "workos:old",
             "expires_at": "2999-01-01T00:00:00Z"}
    assert asyncio.run(ck._ensure_fresh_credentials(
        _FakeClient(), "cline", creds, force=True)) is None


def test_refresh_provider_credentials_persists(monkeypatch):
    """强制刷新成功 → 回写 Provider + 返回新 api_key。"""
    persisted = {}

    async def fake_ensure(client, channel, creds, force=False):
        assert force is True
        return {"channel": "cline", "access_token": "workos:new",
                "refresh_token": "rt2", "expires_at": "2030-01-01T00:00:00Z"}

    monkeypatch.setattr(ck, "_ensure_fresh_credentials", fake_ensure)
    monkeypatch.setattr(ck, "_persist_refreshed_credentials",
                        lambda pid, ch, cr: persisted.update(pid=pid, ch=ch))

    old_key = json.dumps({"channel": "cline", "access_token": "workos:old",
                          "refresh_token": "rt", "expires_at": "2999-01-01T00:00:00Z"})
    new_key = asyncio.run(ck.refresh_provider_credentials(17, "cline", old_key))
    assert new_key is not None
    assert json.loads(new_key)["access_token"] == "workos:new"
    assert persisted == {"pid": 17, "ch": "cline"}


def test_refresh_provider_credentials_without_refresh_token_returns_none():
    old_key = json.dumps({"channel": "cline", "access_token": "workos:old"})
    assert asyncio.run(
        ck.refresh_provider_credentials(17, "cline", old_key)) is None


def test_refresh_provider_credentials_bad_package_returns_none():
    assert asyncio.run(
        ck.refresh_provider_credentials(17, "cline", "not-json")) is None


# ---------------------------------------------------------------- api helper

def test_looks_like_cred_reject_matches_known_shapes():
    from backend.api.proxy import _looks_like_cred_reject
    assert _looks_like_cred_reject("渠道凭据已过期或失效, 请到「免费渠道」页重新登录")
    assert _looks_like_cred_reject("upstream 401")
    assert _looks_like_cred_reject("upstream 400: APIG.0602")
    assert _looks_like_cred_reject('{"error":"invalid access token"}')


def test_looks_like_cred_reject_ignores_other_failures():
    from backend.api.proxy import _looks_like_cred_reject
    assert not _looks_like_cred_reject("upstream 429")
    assert not _looks_like_cred_reject("该渠道今日免费额度已用尽")
    assert not _looks_like_cred_reject("upstream 400 (dead model)")
    assert not _looks_like_cred_reject("provider busy")
    assert not _looks_like_cred_reject(None)


def test_heal_credential_skips_non_channel_provider():
    from backend.api.proxy import _heal_credential

    class P:
        id = 1
        name = "nvidia"

    assert asyncio.run(_heal_credential(P(), "sk-x")) is None


# ---------------------------------------------------------------- 提前保活

@pytest.mark.asyncio
async def test_keepalive_refreshes_ahead_of_expiry(monkeypatch):
    """token 剩 1h(低于提前量 CRED_REFRESH_AHEAD_MINUTES=120min)即在保活时
    提前刷新, 而不是等真过期后靠请求路径自愈抢救 —— 短寿命渠道(trae/cline)
    不再出现"隔天就要重新登录"。"""
    import time as _time

    import httpx

    soon = int((_time.time() + 3600) * 1000)  # 剩 1 小时
    creds = {"channel": "workbuddy", "access_token": "at-old",
             "refresh_token": "rt-old", "expires_at": str(soon)}
    used = {}

    async def fake_refresh(client, credential, product):
        used["refresh_token"] = credential["refresh_token"]
        return {**credential, "access_token": "at-new",
                "refresh_token": "rt-new",
                "expires_at": str(soon + 7200_000)}

    monkeypatch.setattr(
        "backend.core.channel_auth.workbuddy.refresh_token", fake_refresh)
    async with httpx.AsyncClient() as client:
        fresh = await ck._ensure_fresh_credentials(client, "workbuddy", creds)
    assert fresh["access_token"] == "at-new"
    assert used["refresh_token"] == "rt-old"

    # 对照: 剩余寿命远超提前量 → 原样返回不刷
    far = int((_time.time() + 30 * 86400) * 1000)
    far_creds = {**creds, "expires_at": str(far)}
    async with httpx.AsyncClient() as client:
        same = await ck._ensure_fresh_credentials(client, "workbuddy", far_creds)
    assert same is far_creds
