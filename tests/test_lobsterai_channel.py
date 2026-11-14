# -*- coding: utf-8 -*-
"""LobsterAI (有道) 渠道契约测试。

对应插件 vendor/channel-pack/pack.js:
  src/lobsterai.ts / lobsterai-product.ts / lobsterai-oauth.ts /
  lobsterai-adapter.ts / lobsterai-credits.ts
覆盖: 信封拆壳 / token 解析 / uid 兜底 / 凭据包 / 登录 URL / exchange /
      刷新(旧 refresh 保留) / 过期判定 / 模型清单(含兜底) / 推理头 /
      活动签到三步(slot → context → check_in) / 注册表接线 / 插件导入
"""
import json

import httpx
import pytest

from backend.core.channel_auth import lobsterai as lb
from backend.core.channel_auth import (
    channel_headers, checkin_channels, is_channel_provider,
    parse_credentials,
)


def asyncio_run(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _json(status, payload):
    return _client(lambda req: httpx.Response(status, json=payload))


SESSION = {"uuid": "uuid-1", "state": "uuid-1", "first_keyfrom": "1700000000000"}

TOKEN_DATA = {
    "accessToken": "at-1",
    "refreshToken": "rt-1",
    "expiresIn": 3600,
    "user": {"id": "u-1", "userId": "acct-1", "yid": "y-1", "nickname": "小青龙"},
}


# ------------------------------------------------------------
# 基础解析
# ------------------------------------------------------------

class TestEnvelope:
    def test_ok(self):
        data, code, msg = lb.unwrap_envelope({"code": 0, "data": {"a": 1}})
        assert data == {"a": 1} and code == 0 and msg == ""

    def test_business_error_keeps_message(self):
        data, code, msg = lb.unwrap_envelope(
            {"code": 401, "msg": "token 失效"})
        assert data is None and code == 401 and msg == "token 失效"

    def test_message_field_fallback(self):
        _data, code, msg = lb.unwrap_envelope({"code": 500, "message": "boom"})
        assert code == 500 and msg == "boom"

    def test_data_missing(self):
        data, code, msg = lb.unwrap_envelope({"code": 0})
        assert data is None and code == 0 and msg

    def test_not_dict(self):
        data, code, _msg = lb.unwrap_envelope(["x"])
        assert data is None and code == -1


class TestTokenPayload:
    def test_parse(self):
        p = lb.parse_token_payload(TOKEN_DATA)
        assert p["access_token"] == "at-1"
        assert p["refresh_token"] == "rt-1"
        assert p["expires_in"] == 3600
        assert p["uid_raw"] == "u-1"
        assert p["user_id_raw"] == "acct-1"
        assert p["yid"] == "y-1"
        assert p["nickname"] == "小青龙"

    def test_uid_prefers_user_id(self):
        assert lb.resolve_uid({"uid_raw": "u-1", "user_id_raw": "acct-1"}) == "u-1"

    def test_uid_falls_back_to_account_user_id(self):
        assert lb.resolve_uid({"user_id_raw": "acct-1", "yid": "y-1"}) == "acct-1"

    def test_uid_falls_back_to_yid(self):
        assert lb.resolve_uid({"yid": "y-1"}) == "y-1"

    def test_uid_hashes_token(self):
        uid = lb.resolve_uid({"access_token": "tok"})
        assert len(uid) == 16 and uid.isalnum()

    def test_expires_at_from_expires_in(self):
        cred = lb.build_credential(lb.parse_token_payload(TOKEN_DATA), SESSION)
        assert cred["expires_at"].isdigit()
        assert int(cred["expires_at"]) > 1_700_000_000_000

    def test_credential_fields(self):
        cred = lb.build_credential(lb.parse_token_payload(TOKEN_DATA), SESSION)
        assert cred["channel"] == "lobsterai"
        assert cred["uid"] == "u-1"
        assert cred["user_id"] == "acct-1"
        assert cred["uuid"] == "uuid-1"
        assert cred["first_keyfrom"] == "1700000000000"
        assert cred["latest_keyfrom"].isdigit()


class TestPackage:
    def test_roundtrip(self):
        cred = lb.build_credential(lb.parse_token_payload(TOKEN_DATA), SESSION)
        pkg = lb.credentials_package(cred)
        parsed = lb.parse_credentials_package(pkg)
        assert parsed == cred

    def test_reject_other_channel(self):
        assert lb.parse_credentials_package(
            json.dumps({"channel": "trae"})) is None

    def test_reject_garbage(self):
        assert lb.parse_credentials_package("not-json") is None


class TestExpiry:
    def test_ms_string(self):
        assert lb.credential_expires_at_ms({"expires_at": "1700000000000"}) == 1700000000000

    def test_seconds_normalized(self):
        assert lb.credential_expires_at_ms({"expires_at": "1700000000"}) == 1700000000000

    def test_jwt_fallback(self):
        import base64
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": 1_800_000_000}).encode()).decode().rstrip("=")
        token = f"h.{payload}.s"
        assert lb.credential_expires_at_ms({"access_token": token}) == 1_800_000_000_000

    def test_unknown_expiry_not_expired(self):
        assert lb.is_expired({"access_token": "plain"}) is False

    def test_expired(self):
        assert lb.is_expired({"expires_at": "1000"}) is True


# ------------------------------------------------------------
# 登录流
# ------------------------------------------------------------

class TestLoginUrl:
    def test_url_shape(self):
        url = lb.build_login_url(18731, SESSION)
        assert url.startswith("https://lobsterai.youdao.com/portal#/login?")
        assert "source=electron" in url
        assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A18731%2Fauth%2Fcallback" in url
        assert "state=uuid-1" in url

    def test_session_fresh(self):
        a = lb.new_login_session()
        b = lb.new_login_session()
        assert a["uuid"] != b["uuid"]
        assert a["state"] == a["uuid"]
        assert a["first_keyfrom"].isdigit()

    def test_parse_callback(self):
        info = lb.parse_callback_params({"code": "c1", "state": "s1"})
        assert info == {"code": "c1", "state": "s1"}

    def test_parse_callback_missing_code(self):
        assert lb.parse_callback_params({"state": "s1"}) is None


class TestExchange:
    def test_success(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"code": 0, "data": TOKEN_DATA})

        async def run():
            async with _client(handler) as client:
                return await lb.exchange_code(client, "code-1", SESSION, version="9.9.9")

        cred = asyncio_run(run())
        assert cred["access_token"] == "at-1"
        assert seen["url"].endswith("/api/auth/exchange")
        assert seen["body"] == {
            "authCode": "code-1",
            "firstKeyfrom": "1700000000000",
            "latestKeyfrom": seen["body"]["latestKeyfrom"],
            "uuid": "uuid-1",
            "version": "9.9.9",
        }
        # 兑换时的 latestKeyfrom 必须落进凭据(刷新要用同一个)
        assert cred["latest_keyfrom"] == seen["body"]["latestKeyfrom"]

    def test_business_error(self):
        async def run():
            async with _json(200, {"code": 1001, "msg": "授权码已使用"}) as client:
                return await lb.exchange_code(client, "c", SESSION, version="1")

        with pytest.raises(RuntimeError) as exc:
            asyncio_run(run())
        assert "授权码已使用" in str(exc.value)

    def test_missing_access_token(self):
        async def run():
            async with _json(200, {"code": 0, "data": {"refreshToken": "r"}}) as client:
                return await lb.exchange_code(client, "c", SESSION, version="1")

        with pytest.raises(RuntimeError):
            asyncio_run(run())

    def test_non_json(self):
        async def run():
            async with _client(lambda r: httpx.Response(502, text="bad gateway")) as client:
                return await lb.exchange_code(client, "c", SESSION, version="1")

        with pytest.raises(RuntimeError) as exc:
            asyncio_run(run())
        assert "HTTP 502" in str(exc.value)


class TestRefresh:
    CREDS = {
        "channel": "lobsterai", "access_token": "old", "refresh_token": "rt-old",
        "expires_at": "1000", "uuid": "uuid-1",
        "first_keyfrom": "1700000000000", "latest_keyfrom": "1700000000500",
        "user_id": "acct-1",
    }

    def test_success_replaces_token(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"code": 0, "data": {
                "accessToken": "new", "refreshToken": "", "expiresIn": 600}})

        async def run():
            async with _client(handler) as client:
                return await lb.refresh_credential(client, self.CREDS)

        cred = asyncio_run(run())
        assert cred["access_token"] == "new"
        # 响应不回新 refreshToken → 沿用旧的, 不能变空
        assert cred["refresh_token"] == "rt-old"
        assert seen["body"]["latestKeyfrom"] == "1700000000500"
        assert seen["body"]["firstKeyfrom"] == "1700000000000"
        assert seen["body"]["userId"] == "acct-1"
        assert int(cred["expires_at"]) > 1_700_000_000_000

    def test_401_returns_none(self):
        async def run():
            async with _json(401, {"code": 401}) as client:
                return await lb.refresh_credential(client, self.CREDS)

        assert asyncio_run(run()) is None

    def test_no_refresh_token(self):
        async def run():
            async with _json(200, {"code": 0, "data": {}}) as client:
                return await lb.refresh_credential(client, {"channel": "lobsterai"})

        assert asyncio_run(run()) is None


# ------------------------------------------------------------
# 模型清单 / 推理头
# ------------------------------------------------------------

MODELS_BODY = {"code": 0, "data": [
    {"modelId": "glm-5.2", "modelName": "GLM-5.2", "contextWindow": 200000},
    {"modelId": "deepseek-v4-flash", "modelName": "DeepSeek V4 Flash"},
    {"modelName": "无 id 条目"},
]}


class TestModels:
    CREDS = {"channel": "lobsterai", "access_token": "at", "uid": "u",
             "first_keyfrom": "1", "latest_keyfrom": "2"}

    def test_parse(self):
        models = lb.parse_models(MODELS_BODY)
        assert [m["id"] for m in models] == ["glm-5.2", "deepseek-v4-flash"]
        assert models[0]["contextWindow"] == 200000
        assert models[1]["name"] == "DeepSeek V4 Flash"

    def test_parse_error_code(self):
        assert lb.parse_models({"code": 1, "data": []}) == []

    def test_fetch_ok(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["version"] = request.headers.get("X-LobsterAI-Client-Version")
            return httpx.Response(200, json=MODELS_BODY)

        async def run():
            async with _client(handler) as client:
                return await lb.fetch_models(client, self.CREDS)

        models = asyncio_run(run())
        assert [m["id"] for m in models] == ["glm-5.2", "deepseek-v4-flash"]
        assert "/api/models/available?" in seen["url"]
        assert "firstKeyfrom=1" in seen["url"]
        assert seen["version"]

    def test_fetch_falls_back(self):
        async def run():
            async with _json(401, {"code": 401}) as client:
                return await lb.fetch_models(client, self.CREDS)

        models = asyncio_run(run())
        assert len(models) == len(lb.FALLBACK_MODELS)
        assert models[0]["id"] == lb.FALLBACK_MODELS[0]

    def test_fetch_no_fallback(self):
        async def run():
            async with _json(401, {"code": 401}) as client:
                return await lb.fetch_models(client, self.CREDS, allow_fallback=False)

        assert asyncio_run(run()) == []


class TestChatHeaders:
    def test_headers(self):
        h = lb.chat_headers({"access_token": "at"}, "glm-5.2", version="9.9.9")
        assert h["Authorization"] == "Bearer at"
        assert h["X-LobsterAI-Client-Capabilities"] == lb.CLIENT_CAPABILITIES
        assert h["X-LobsterAI-Client-Version"] == "9.9.9"
        assert "text/event-stream" in h["Accept"]

    def test_version_falls_back_to_cache(self):
        h = lb.chat_headers({"access_token": "at"})
        assert h["X-LobsterAI-Client-Version"]


class TestClientVersion:
    def test_resolve_from_upstream(self):
        lb._VERSION_CACHE.update({"value": "", "at": 0.0})

        def handler(request):
            return httpx.Response(200, json={"data": {"value": {"version": "2026.10.1"}}})

        async def run():
            async with _client(handler) as client:
                return await lb.resolve_client_version(client, force=True)

        assert asyncio_run(run()) == "2026.10.1"
        assert lb.client_version_cached() == "2026.10.1"

    def test_resolve_falls_back_on_error(self):
        lb._VERSION_CACHE.update({"value": "", "at": 0.0})

        async def run():
            async with _json(500, {}) as client:
                return await lb.resolve_client_version(client, force=True)

        assert asyncio_run(run()) == lb.FALLBACK_CLIENT_VERSION


# ------------------------------------------------------------
# 签到
# ------------------------------------------------------------

SLOT_OK = {"code": 0, "data": {
    "slotState": "available",
    "activity": {"activityCode": "daily-sign-in", "configRevision": 7},
}}

CTX_ACTIVE = {"code": 0, "data": {
    "state": {"claimedToday": False}, "actions": [{"action": "check_in"}]}}

CREDS = {"channel": "lobsterai", "access_token": "at", "uid": "u"}


def _checkin_handler(claim_payload, slot=SLOT_OK, ctx=CTX_ACTIVE, claim_status=200,
                     hits=None):
    def handler(request):
        path = request.url.path
        if hits is not None:
            hits.append(path)
        if path.endswith("/api/client-activities/slot"):
            return httpx.Response(200, json=slot)
        if path.endswith("/context"):
            return httpx.Response(200, json=ctx)
        if path.endswith("/actions/check_in"):
            return httpx.Response(claim_status, json=claim_payload)
        return httpx.Response(404, json={})
    return _client(handler)


class TestCheckin:
    def test_claim_ok(self):
        hits = []
        client = _checkin_handler(
            {"code": 0, "data": {"result": {"creditsGranted": 50}}}, hits=hits)

        async def run():
            async with client:
                return await lb.claim_daily_checkin(client, CREDS)

        r = asyncio_run(run())
        assert r["kind"] == "claimed" and r["credit"] == 50
        assert hits[0].endswith("/api/client-activities/slot")
        assert hits[-1].endswith("/api/client-activities/daily-sign-in/actions/check_in")

    def test_claim_already_today(self):
        client = _checkin_handler(
            {}, ctx={"code": 0, "data": {"state": {"claimedToday": True},
                                         "actions": ["check_in"]}})

        async def run():
            async with client:
                return await lb.claim_daily_checkin(client, CREDS)

        assert asyncio_run(run())["kind"] == "already-claimed"

    def test_no_available_activity(self):
        client = _checkin_handler({}, slot={"code": 0, "data": {"slotState": "hidden"}})

        async def run():
            async with client:
                return await lb.claim_daily_checkin(client, CREDS)

        assert asyncio_run(run())["kind"] == "inactive"

    def test_action_not_offered(self):
        client = _checkin_handler(
            {}, ctx={"code": 0, "data": {"state": {"claimedToday": False},
                                         "actions": ["share"]}})

        async def run():
            async with client:
                return await lb.claim_daily_checkin(client, CREDS)

        r = asyncio_run(run())
        assert r["kind"] == "inactive" and "不可签到" in r["message"]

    def test_claim_401(self):
        client = _checkin_handler({"code": 401, "msg": "凭据已失效"}, claim_status=401)

        async def run():
            async with client:
                return await lb.claim_daily_checkin(client, CREDS)

        r = asyncio_run(run())
        assert r["kind"] == "failed"

    def test_claim_reports_already_in_message(self):
        client = _checkin_handler({"code": 1201, "msg": "今日已领取"})

        async def run():
            async with client:
                return await lb.claim_daily_checkin(client, CREDS)

        assert asyncio_run(run())["kind"] == "already-claimed"

    def test_slot_http_error(self):
        client = _client(lambda req: httpx.Response(500, text="oops"))

        async def run():
            async with client:
                return await lb.claim_daily_checkin(client, CREDS)

        assert asyncio_run(run())["kind"] == "failed"

    def test_status_reports_claimed(self):
        client = _checkin_handler(
            {}, ctx={"code": 0, "data": {"state": {"claimedToday": True},
                                         "actions": ["check_in"]}})

        async def run():
            async with client:
                return await lb.fetch_checkin_status(client, CREDS)

        st = asyncio_run(run())
        assert st["today_checked_in"] is True and st["active"] is True

    def test_status_no_activity(self):
        client = _checkin_handler({}, slot={"code": 0, "data": {"slotState": "absent"}})

        async def run():
            async with client:
                return await lb.fetch_checkin_status(client, CREDS)

        st = asyncio_run(run())
        assert st["active"] is False


# ------------------------------------------------------------
# 注册表 / 导入接线
# ------------------------------------------------------------

class TestRegistry:
    def test_is_channel_provider(self):
        assert is_channel_provider("lobsterai") is True

    def test_checkin_channels_includes_lobsterai(self):
        assert "lobsterai" in checkin_channels()

    def test_channel_headers_uses_bearer(self):
        pkg = lb.credentials_package({
            "access_token": "at", "refresh_token": "rt", "expires_at": "1",
            "uid": "u", "uuid": "x", "first_keyfrom": "1", "latest_keyfrom": "2"})
        h = channel_headers("lobsterai", pkg, "glm-5.2")
        assert h["Authorization"] == "Bearer at"
        assert h["X-LobsterAI-Client-Version"]

    def test_parse_credentials_via_registry(self):
        pkg = lb.credentials_package({"access_token": "at", "uid": "u"})
        assert parse_credentials("lobsterai", pkg)["access_token"] == "at"


class TestImportFromPlugin:
    def test_prefix_maps_to_channel(self):
        from backend.core.channel_import import _PREFIX_TO_CHANNEL
        assert _PREFIX_TO_CHANNEL["LOBSTERAI_ACCOUNT_"] == "lobsterai"

    def test_plugin_package_conversion(self):
        from backend.core.channel_import import _to_lobsterai
        raw = {
            "access_token": "at", "refresh_token": "rt", "expires_at": "1800000000000",
            "uid": "u-1", "user_id": "acct-1", "nickname": "小龙",
            "uuid": "uuid-9", "first_keyfrom": "111", "latest_keyfrom": "222",
        }
        parsed = lb.parse_credentials_package(_to_lobsterai(raw))
        assert parsed["access_token"] == "at"
        assert parsed["uid"] == "u-1"
        assert parsed["user_id"] == "acct-1"
        assert parsed["uuid"] == "uuid-9"
        assert parsed["latest_keyfrom"] == "222"

    def test_collect_accounts_from_yaml(self, tmp_path):
        yaml = pytest.importorskip("yaml")
        path = tmp_path / "creds.yaml"
        path.write_text(yaml.safe_dump({"refs": {
            "LOBSTERAI_ACCOUNT_u-1": json.dumps({
                "access_token": "at", "refresh_token": "rt",
                "expires_at": "1800000000000", "uid": "u-1", "nickname": "小龙"}),
        }}), encoding="utf-8")
        from backend.core.channel_import import collect_accounts
        found = collect_accounts(path)
        assert [f["channel"] for f in found] == ["lobsterai"]
        assert found[0]["package"]
        assert lb.parse_credentials_package(found[0]["package"])["access_token"] == "at"
