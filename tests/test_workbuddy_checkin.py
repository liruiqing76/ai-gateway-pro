# -*- coding: utf-8 -*-
"""WorkBuddy / CodeBuddy 每日签到契约测试(照抄插件 credits.ts 的 billing 端点)。"""
import json

import httpx
import pytest

from backend.core.channel_auth import workbuddy as wb
from backend.core.channel_auth import checkin_channels, channel_checkin

CREDS = {
    "channel": "workbuddy",
    "access_token": "tok-abc",
    "refresh_token": "rt",
    "user_id": "u-123",
    "enterprise_id": "ent-9",
}

STATUS_OK = {"code": 0, "data": {
    "active": True, "today_checked_in": False, "streak_days": 3,
    "daily_credit": 50, "total_credits": 1200,
    "activity_name": "每日签到"}}

CLAIM_OK = {"code": 0, "data": {"credit": 50, "streak_days": 4, "is_streak_day": True}}


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestCheckinHeaders:
    def test_workbuddy_headers(self):
        h = wb.checkin_headers(CREDS, "workbuddy")
        assert h["Authorization"] == "Bearer tok-abc"
        assert h["X-Domain"] == "www.workbuddy.ai"
        assert h["X-Product"] == "SaaS"
        assert h["X-Product-Code"] == "workbuddy"
        assert h["X-User-Id"] == "u-123"
        assert h["X-Enterprise-Id"] == "ent-9"
        assert h["X-Tenant-Id"] == "ent-9"
        assert h["Accept"] == "application/json"

    def test_codebuddy_headers(self):
        h = wb.checkin_headers(CREDS, "buddy")
        assert h["X-Domain"] == "copilot.tencent.com"
        assert h["X-Product-Code"] == "codebuddy"
        assert h["User-Agent"] == "CodeBuddyIDE/1.106.1"

    def test_no_user_id_when_absent(self):
        h = wb.checkin_headers({"access_token": "t", "channel": "buddy"}, "buddy")
        assert "X-User-Id" not in h and "X-Enterprise-Id" not in h


class TestCheckinStatus:
    def test_status_parsed(self):
        async def run():
            def handler(req: httpx.Request) -> httpx.Response:
                assert req.url.path == "/v2/billing/meter/checkin-activity-status"
                assert req.content == b"{}"
                return httpx.Response(200, json=STATUS_OK)

            async with _client(handler) as c:
                return await wb.fetch_checkin_status(c, CREDS, "workbuddy")

        st = asyncio_run(run())
        assert st["active"] is True
        assert st["todayCheckedIn"] is False
        assert st["streakDays"] == 3
        assert st["dailyCredit"] == 50
        assert st["activityName"] == "每日签到"

    def test_status_code_nonzero_is_none(self):
        async def run():
            return await wb.fetch_checkin_status(
                _c(200, {"code": 1001, "msg": "not auth"}), CREDS, "workbuddy")
        assert asyncio_run(run()) is None

    def test_status_http_error_is_none(self):
        async def run():
            return await wb.fetch_checkin_status(
                _c(401, {"code": 0, "data": {}}), CREDS, "workbuddy")
        assert asyncio_run(run()) is None


class TestClaim:
    def test_claim_success(self):
        async def run():
            def handler(req: httpx.Request) -> httpx.Response:
                assert req.url.path == "/v2/billing/meter/daily-checkin"
                return httpx.Response(200, json=CLAIM_OK)
            async with _client(handler) as c:
                return await wb.claim_daily_checkin(c, CREDS, "workbuddy")

        r = asyncio_run(run())
        assert r["kind"] == "claimed"
        assert r["credit"] == 50
        assert r["streakDays"] == 4

    @pytest.mark.parametrize("code", [10001, 1001])
    def test_claim_already(self, code):
        async def run():
            return await wb.claim_daily_checkin(
                _c(200, {"code": code, "msg": "今天已签到"}), CREDS, "buddy")
        r = asyncio_run(run())
        assert r["kind"] == "already-claimed"

    @pytest.mark.parametrize("code", [1002, 1003])
    def test_claim_inactive(self, code):
        async def run():
            return await wb.claim_daily_checkin(
                _c(200, {"code": code}), CREDS, "workbuddy")
        assert asyncio_run(run())["kind"] == "inactive"

    def test_claim_expired_credentials(self):
        async def run():
            return await wb.claim_daily_checkin(
                _c(401, {"code": 1}), CREDS, "workbuddy")
        r = asyncio_run(run())
        assert r["kind"] == "failed"
        assert "重新登录" in r["message"]

    def test_claim_unknown_code_failed(self):
        async def run():
            return await wb.claim_daily_checkin(
                _c(200, {"code": 5001, "msg": "系统繁忙"}), CREDS, "workbuddy")
        r = asyncio_run(run())
        assert r["kind"] == "failed" and r["code"] == 5001

    def test_claim_missing_data_failed(self):
        async def run():
            return await wb.claim_daily_checkin(
                _c(200, {"code": 0}), CREDS, "workbuddy")
        assert asyncio_run(run())["kind"] == "failed"

    def test_claim_non_json_failed(self):
        async def run():
            def handler(req):
                return httpx.Response(200, text="<html>gateway</html>")
            async with _client(handler) as c:
                return await wb.claim_daily_checkin(c, CREDS, "workbuddy")
        assert asyncio_run(run())["kind"] == "failed"

    def test_claim_already_on_http_400(self):
        """真机: buddy 今天已签到时返回的是 HTTP 400 + code 10001。"""
        async def run():
            return await wb.claim_daily_checkin(
                _c(400, {"code": 10001, "msg": "今天已签到，请明天再来"}), CREDS, "buddy")
        r = asyncio_run(run())
        assert r["kind"] == "already-claimed"
        assert r["code"] == 10001

    def test_claim_activity_closed_on_http_400(self):
        """真机: workbuddy 活动未开启时同样是 code 10001, 但语义是 inactive。"""
        async def run():
            return await wb.claim_daily_checkin(
                _c(400, {"code": 10001, "msg": "签到活动未开启或已过期"}), CREDS, "workbuddy")
        r = asyncio_run(run())
        assert r["kind"] == "inactive"
        assert r["code"] == 10001

    def test_claim_success_on_http_400_body_zero(self):
        """body code=0 但 HTTP 非 200 仍算失败, 不能凭 data 谎报领取成功。"""
        async def run():
            return await wb.claim_daily_checkin(
                _c(400, {"code": 0, "data": {"credit": 50}}), CREDS, "workbuddy")
        assert asyncio_run(run())["kind"] == "failed"


class TestRegistry:
    def test_channels_listed(self):
        chans = checkin_channels()
        assert "workbuddy" in chans and "buddy" in chans
        assert "codearts" in chans and "trae" in chans

    def test_channel_checkin_resolves(self):
        pkg = json.dumps({**CREDS, "channel": "buddy"})
        pkg2 = json.dumps({**CREDS, "channel": "workbuddy"})
        for name, p in (("buddy", pkg), ("workbuddy", pkg2)):
            coro = channel_checkin(name, p, None)
            assert coro is not None
            coro.close()          # 只为验证注册表能解析出可调用对象

    def test_non_channel_returns_none(self):
        assert channel_checkin("nvidia", "{}", None) is None
        assert channel_checkin("cline", "{}", None) is None


class TestAccountPackage:
    def test_parse_account(self):
        acct = wb.parse_account_data({"uid": "u1", "nickname": "n",
                                      "enterpriseId": "e1", "type": "enterprise"})
        assert acct == {"uid": "u1", "nickname": "n",
                        "enterprise_id": "e1", "account_type": "enterprise"}

    def test_parse_account_empty(self):
        assert wb.parse_account_data(None)["account_type"] == "personal"

    def test_package_carries_uid(self):
        pkg = json.loads(wb.credentials_package(
            {"access_token": "t"}, "workbuddy",
            {"uid": "u9", "nickname": "nick", "enterprise_id": "e9"}))
        assert pkg["user_id"] == "u9"
        assert pkg["enterprise_id"] == "e9"
        assert pkg["channel"] == "workbuddy"


# ---- helpers ----

def _c(status: int, payload: dict) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(status, json=payload)))


def asyncio_run(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
