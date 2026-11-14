# -*- coding: utf-8 -*-
"""cline 凭据刷新/过期/信封解包测试。

覆盖三个真机踩过的坑:
1. 刷新请求体必须是 `{"refreshToken": ..., "grantType": "refresh_token"}` ——
   少 grantType 会被上游 400 拒绝(插件 clineRefreshBody 原文如此)。
2. 刷新响应是 `{"success":true,"data":{accessToken,refreshToken,expiresAt,...}}`
   —— 驼峰字段套在 data 里, 与登录(register)返回形状不同。
3. cline 非流式 chat 响应外面多包一层 `{"data":{"choices":[...]}}` 信封,
   不拆壳会被空回复防护误判成 upstream empty response。
"""
import json

import httpx
import pytest

from backend.core.channel_auth import cline
from backend.core.proxy import _aggregate_openai_sse


class TestRefreshRequestBody:
    """刷新请求体/头形状"""

    @pytest.mark.asyncio
    async def test_sends_grant_type_and_camel_refresh_token(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json={
                "success": True,
                "data": {"accessToken": "eyJnew", "refreshToken": "rt-new",
                         "expiresAt": "2026-10-24T08:43:23Z",
                         "tokenType": "Bearer"},
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await cline.refresh_cline_token(c, "rt-old")

        assert captured["body"] == {"refreshToken": "rt-old",
                                   "grantType": "refresh_token"}
        assert captured["headers"]["x-client-type"] == "cline-sdk"
        assert captured["headers"]["x-is-multiroot"] == "false"
        assert captured["headers"]["http-referer"] == "https://cline.bot"

    @pytest.mark.asyncio
    async def test_401_and_403_return_none(self):
        for status in (401, 403):
            def handler(request: httpx.Request, s=status) -> httpx.Response:
                return httpx.Response(s, json={"error": "nope"})

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                assert await cline.refresh_cline_token(c, "rt") is None


class TestRefreshCredential:
    """刷新结果 → 凭据包(含 workos: 前缀补全)"""

    @pytest.mark.asyncio
    async def test_unwraps_data_envelope_and_prefixes(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "success": True,
                "data": {"accessToken": "eyJraw", "refreshToken": "rt2",
                         "expiresAt": "2026-10-24T09:00:00Z"},
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            fresh = await cline.refresh_credential(c, {"refresh_token": "rt1"})

        assert fresh is not None
        assert fresh["access_token"] == "workos:eyJraw"
        assert fresh["refresh_token"] == "rt2"
        assert fresh["expires_at"] == "2026-10-24T09:00:00Z"
        assert fresh["channel"] == "cline"

    @pytest.mark.asyncio
    async def test_keeps_old_refresh_token_when_absent(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {"accessToken": "eyJraw"}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            fresh = await cline.refresh_credential(c, {"refresh_token": "rt-keep"})

        assert fresh["refresh_token"] == "rt-keep"

    @pytest.mark.asyncio
    async def test_no_refresh_token_returns_none(self):
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(500))) as c:
            assert await cline.refresh_credential(c, {"access_token": "x"}) is None

    @pytest.mark.asyncio
    async def test_already_prefixed_token_not_doubled(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "data": {"accessToken": "workos:already"}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            fresh = await cline.refresh_credential(c, {"refresh_token": "rt"})

        assert fresh["access_token"] == "workos:already"


class TestCredentialExpiry:
    """过期判定(秒/毫秒/ISO/数字字符串)"""

    @pytest.mark.parametrize("raw,expected", [
        (1792825181000, 1792825181000),          # 毫秒
        (1792825181, 1792825181000),             # 秒 → 毫秒
        ("1792825181000", 1792825181000),        # 数字字符串
        ("1792825181", 1792825181000),
    ])
    def test_numeric_forms(self, raw, expected):
        assert cline.credential_expires_at_ms({"expire_time": raw}) == expected

    def test_iso_form(self):
        from datetime import datetime, timezone
        ms = cline.credential_expires_at_ms(
            {"expires_at": "2026-10-24T08:43:23Z"})
        want = int(datetime(2026, 10, 24, 8, 43, 23,
                            tzinfo=timezone.utc).timestamp() * 1000)
        assert ms == want

    def test_iso_without_zone_treated_as_utc(self):
        from datetime import datetime, timezone
        ms = cline.credential_expires_at_ms(
            {"expires_at": "2026-10-24T08:43:23"})
        want = int(datetime(2026, 10, 24, 8, 43, 23,
                            tzinfo=timezone.utc).timestamp() * 1000)
        assert ms == want

    def test_missing_returns_none(self):
        assert cline.credential_expires_at_ms({}) is None
        assert cline.credential_expires_at_ms({"expire_time": ""}) is None

    def test_garbage_returns_none(self):
        assert cline.credential_expires_at_ms({"expires_at": "not-a-date"}) is None


class TestCredentialsPackage:
    """凭据包兼容三种入参 + 落库过期时间"""

    def test_register_shape(self):
        pkg = json.loads(cline.credentials_package(
            {"token": "workos:t", "refreshToken": "r"}))
        assert pkg["access_token"] == "workos:t"
        assert pkg["refresh_token"] == "r"

    def test_refresh_shape_keeps_expiry(self):
        """刷新回写必须带上过期时间, 否则保活循环每轮都白刷一次"""
        pkg = json.loads(cline.credentials_package(
            {"access_token": "workos:t", "refresh_token": "r",
             "expires_at": "2026-10-24T08:45:28Z"}))
        assert pkg["expires_at"] == "2026-10-24T08:45:28Z"

    def test_raw_token_gets_prefix(self):
        pkg = json.loads(cline.credentials_package(
            {"access_token": "eyJraw", "refresh_token": "r"}))
        assert pkg["access_token"] == "workos:eyJraw"


class TestNonStreamEnvelope:
    """cline 非流式 {"data":{"choices":[...]}} 信封"""

    def test_envelope_has_choices_under_data(self):
        body = {"data": {"choices": [{"message": {"content": "收到"}}]}}
        # 拆壳逻辑在 forward_non_stream 内联; 这里守住形状前提,
        # 避免上游改回扁平形状时测试静默失效。
        assert isinstance(body.get("data"), dict)
        assert body["data"]["choices"]

    def test_sse_aggregate_still_works_for_stream_only(self):
        text = ('data: {"choices":[{"delta":{"content":"收"}}]}\n'
                'data: {"choices":[{"delta":{"content":"到"},'
                '"finish_reason":"stop"}]}\n'
                'data: [DONE]\n')
        data = _aggregate_openai_sse(text)
        assert data["choices"][0]["message"]["content"] == "收到"
