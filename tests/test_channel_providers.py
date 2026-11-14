# ============================================================
# 白嫖渠道适配单元测试: CodeArts(华为云) + WorkBuddy(腾讯)
#
# 覆盖:
#   1. huawei_sign: SDK-HMAC-SHA256 规范化请求与签名(算法层)
#   2. codearts: PKCE 对 / DPoP proof JWT 签名 / 授权页 URL /
#      凭据包 / 签到信封解包(mock HTTP)
#   3. workbuddy: state 登录 / token 轮询(11217 未就绪) / 刷新 /
#      过期时间解析 / 按模型家族 UA / 凭据包
#   4. channel_auth 注册表: 头注入路由
#   5. checkin: 凭据刷新判定 / 全用户签到(mock)
# ============================================================

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timezone, timedelta

import httpx
import pytest

from backend.core.channel_auth import huawei_sign as hs
from backend.core.channel_auth import codearts as ca
from backend.core.channel_auth import workbuddy as wb
from backend.core.channel_auth import channel_headers, channel_signed_headers, parse_credentials, is_channel_provider


# ============================================================
# 1) 华为云签名(算法层)
# ============================================================

class TestHuaweiSign:
    def test_canonical_shape(self):
        """规范化请求 = method/uri/query/headers/空行/signedHeaders/hash 六段"""
        headers = {"content-type": "application/json", "host": "h.test",
                   "x-sdk-date": "20261024T000000Z"}
        canonical, signed = hs.build_canonical_request(
            "POST", "/api/v2/chat/completions/", "", headers, "deadbeef")
        assert canonical == "\n".join([
            "POST", "/api/v2/chat/completions/", "",
            "content-type:application/json",
            "host:h.test",
            "x-sdk-date:20261024T000000Z",
            "", "content-type;host;x-sdk-date", "deadbeef",
        ])
        assert signed == "content-type;host;x-sdk-date"

    def test_uri_gets_trailing_slash(self):
        """pack.js: uri 不以 / 结尾时补尾 / —— 华为云 APIG 规范"""
        headers = hs.sign_request_huawei(
            "AK", "SK", "ST", "GET", "https://svc.test/v1/model/builtin")
        assert "Authorization" in headers
        # GET 不带 content-type(不进签名集合)
        assert "content-type" not in headers
        assert "Content-Type" not in headers

    def test_post_content_type_in_signature(self):
        signed = hs.sign_request_huawei(
            "AK", "SK", "ST", "POST", "https://svc.test/v1/chat", b'{"a":1}')
        auth = signed["Authorization"]
        assert "content-type" in auth  # 非 GET 把 content-type 签进去
        assert signed["content-type"] == "application/json"

    def test_security_token_present(self):
        signed = hs.sign_request_huawei("AK", "SK", "SECRET-TOKEN",
                                        "GET", "https://svc.test/x")
        assert signed["x-security-token"] == "SECRET-TOKEN"
        assert "x-security-token" in signed["Authorization"]

    def test_no_host_in_output(self):
        """host 由 httpx 自管, 不进返回头(但进签名集合)"""
        signed = hs.sign_request_huawei("AK", "SK", "ST", "GET",
                                        "https://svc.test/path?x=1")
        assert "host" not in signed
        assert "host" in signed["Authorization"]  # signedHeaders 里有

    def test_query_string_signed(self):
        signed = hs.sign_request_huawei("AK", "SK", "ST", "GET",
                                        "https://svc.test/path?channel=IDE")
        # query 参与规范化(第二段后第三段)
        canonical, _ = hs.build_canonical_request(
            "GET", "/path/", "channel=IDE",
            {"host": "svc.test", "x-sdk-date": signed["x-sdk-date"],
             "x-sdk-content-sha256": signed["x-sdk-content-sha256"],
             "x-security-token": "ST"},
            signed["x-sdk-content-sha256"])
        assert "channel=IDE" in canonical

    def test_deterministic_for_same_inputs(self):
        """同输入同输出(时间戳固定时)"""
        from datetime import datetime
        now = datetime(2026, 10, 24, 0, 0, 0, tzinfo=timezone.utc)
        kw = dict(ak="AK", sk="SK", security_token="ST", method="POST",
                  url="https://svc.test/v1/chat", body=b'{"m":1}', now=now)
        a = hs.sign_request_huawei(**kw)
        b = hs.sign_request_huawei(**kw)
        assert a["Authorization"] == b["Authorization"]

    def test_extra_headers_signed(self):
        signed = hs.sign_request_huawei(
            "AK", "SK", "ST", "POST", "https://svc.test/v1/chat", b"{}",
            extra_headers={"maas_type": "benefit"})
        assert signed["maas_type"] == "benefit"
        assert "maas_type" in signed["Authorization"]

    def test_hash_math(self):
        assert hs._sha256_hex(b"abc") == hashlib.sha256(b"abc").hexdigest()
        assert re.fullmatch(r"[0-9a-f]{64}",
                            hs._hmac_sha256_hex(b"k", b"m"))


# ============================================================
# 2) CodeArts: PKCE / DPoP / OAuth URL
# ============================================================

class TestCodeArtsOauth:
    def test_pkce_pair_s256(self):
        pair = ca.generate_pkce_pair()
        verifier = pair["code_verifier"]
        challenge = pair["code_challenge"]
        assert verifier and challenge
        # S256 = BASE64URL(SHA256(verifier))
        digest = hashlib.sha256(verifier.encode()).digest()
        expected = (digest.hex())
        import base64
        recomputed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        assert challenge == recomputed

    def test_dpop_jwk_shape(self):
        kp = ca.generate_dpop_keypair()
        pub = kp["public_key_jwk"]
        assert pub["kty"] == "EC" and pub["crv"] == "P-256"
        assert set(pub.keys()) == {"kty", "crv", "x", "y"}
        assert "d" in kp["private_key_jwk"]

    def test_dpop_proof_jwt_structure(self):
        kp = ca.generate_dpop_keypair()
        jwt = ca.sign_dpop_jws(kp["private_key_jwk"], "POST",
                               "https://sts.test/v1/oauth2/tokens")
        parts = jwt.split(".")
        assert len(parts) == 3
        import base64
        def _b64(s):
            return json.loads(base64.urlsafe_b64decode(
                (s + "=" * (-len(s) % 4)).encode()))
        header = _b64(parts[0])
        payload = _b64(parts[1])
        assert header["alg"] == "ES256"
        assert header["typ"] == "dpop+jwt"
        assert header["jwk"]["crv"] == "P-256"
        assert payload["htm"] == "POST"
        assert payload["htu"] == "https://sts.test/v1/oauth2/tokens"
        assert "iat" in payload and "jti" in payload
        # ES256 签名 = 64 字节 raw(r||s) → base64url 无填充 = 86 字符
        assert len(parts[2]) == 86

    def test_dpop_proof_verifies_with_public_key(self):
        """JWS 签名应能用 JWK 公钥验证(cryptography 自洽)"""
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import hashes
        from cryptography.exceptions import InvalidSignature
        import base64
        kp = ca.generate_dpop_keypair()
        jwt = ca.sign_dpop_jws(kp["private_key_jwk"], "GET", "https://x.test/y")
        h, p, s = jwt.split(".")
        sig = base64.urlsafe_b64decode((s + "=" * (-len(s) % 4)).encode())
        r = int.from_bytes(sig[:32], "big")
        s_int = int.from_bytes(sig[32:], "big")
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        der = encode_dss_signature(r, s_int)
        x = int.from_bytes(base64.urlsafe_b64decode(
            (kp["public_key_jwk"]["x"] + "=" * (-len(kp["public_key_jwk"]["x"]) % 4))), "big")
        y = int.from_bytes(base64.urlsafe_b64decode(
            (kp["public_key_jwk"]["y"] + "=" * (-len(kp["public_key_jwk"]["y"]) % 4))), "big")
        pubnums = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1())
        pub = pubnums.public_key()
        pub.verify(der, f"{h}.{p}".encode(), ec.ECDSA(hashes.SHA256()))

    def test_login_url_has_required_params(self):
        pkce = ca.generate_pkce_pair()
        url = ca.build_oauth_login_url(18080, pkce, "ticket-abc")
        assert url.startswith(ca.PORTAL_AUTHORIZE_BASE)
        assert "client_id=codearts-agent" in url
        assert "code_challenge_method=SHA-256" in url
        assert f"port=18080" in url
        assert "plugin-name=snap_AIIDE" in url

    def test_oauth_url_matches_plugin_buildOAuthLoginUrl(self):
        """逐字符对齐 pack.js buildOAuthLoginUrl 的查询串顺序与拼写"""
        pkce = {"code_challenge": "CHAL", "code_verifier": "V"}
        url = ca.build_oauth_login_url(12345, pkce, "TCK")
        tail = url.split("?", 1)[1]
        assert tail == (
            "theme=2&locale=zh-cn&uri_scheme=codearts-agent"
            "&client_id=codearts-agent&port=12345"
            "&code_challenge=CHAL&code_challenge_method=SHA-256"
            "&ticket_id=TCK&plugin-name=snap_AIIDE&plugin-version=5.2.0")

    def test_parse_credential_response_shapes(self):
        """ticket 接口两种形状(credential.access / result.accessKeyId)"""
        a = ca._parse_credential_response({"credential": {
            "access": "AK1", "secret": "SK1", "securitytoken": "ST1",
            "expires_at": "2099-01-01T00:00:00Z"}})
        assert a["access_key_id"] == "AK1" and a["security_token"] == "ST1"
        b = ca._parse_credential_response({"result": {
            "accessKeyId": "AK2", "secretAccessKey": "SK2",
            "securityToken": "ST2", "expiration": "x"}})
        assert b["access_key_id"] == "AK2"
        assert ca._parse_credential_response({}) is None
        assert ca._parse_credential_response({"result": {"accessKeyId": ""}}) is None

    @pytest.mark.asyncio
    async def test_poll_for_credential_success_and_timeout(self):
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.headers["plugin-name"] == "snap_jetbrains"
            assert "ticket_id" in str(req.url) and "secret" in str(req.url)
            return httpx.Response(200, json={"credential": {
                "access": "AK", "secret": "SK", "securityToken": "ST"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            creds = await ca.poll_for_credential(c, "SEC", "TCK", interval=0)
        assert creds["access_key_id"] == "AK"
        # 一直拿不到 → 超时抛错
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, json={}))) as c:
            with pytest.raises(RuntimeError, match="timed out"):
                await ca.poll_for_credential(
                    c, "SEC", "TCK", max_attempts=2, interval=0)

    def test_expires_from_credential(self):
        assert ca.expires_from_credential(
            {"expires_at": "2099-01-01T00:00:00Z"}) > int(time.time() * 1000)
        assert ca.expires_from_credential({}) <= int(time.time() * 1000) + 86_400_000

    def test_parse_token_response(self):
        data = {"credentials": {"access_key_id": "AK", "secret_access_key": "SK",
                                "security_token": "ST", "expiration": "2027-01-01T00:00:00Z"},
                "refresh_token": "RT"}
        creds = ca._parse_token_response(data)
        assert creds == {"access_key_id": "AK", "secret_access_key": "SK",
                         "security_token": "ST",
                         "expires_at": "2027-01-01T00:00:00Z",
                         "refresh_token": "RT"}

    def test_credentials_roundtrip(self):
        pkg = ca.credentials_package({"access_key_id": "AK",
                                      "secret_access_key": "SK",
                                      "security_token": "ST"})
        parsed = ca.parse_credentials_package(pkg)
        assert parsed["channel"] == "codearts"
        assert parsed["access_key_id"] == "AK"

    def test_normalize_model_id(self):
        assert ca._normalize_model_id("glm-5.3-flash-0912") == "glm-5.3-flash"
        assert ca._normalize_model_id("deepseek-v4.1-flash") == "deepseek-v4.1-flash"
        assert ca._normalize_model_id("glm-5.3-flash-x") == "glm-5.3-flash-x"

    def test_benefit_set_seeded(self):
        assert "glm-5.3-flash" in ca.BENEFIT_SET

    @pytest.mark.asyncio
    async def test_request_token_success(self):
        captured = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["dpop"] = req.headers.get("DPoP", "")
            captured["ct"] = req.headers.get("content-type", "")
            captured["body"] = req.content.decode()
            return httpx.Response(200, json={
                "credentials": {"access_key_id": "AK", "secret_access_key": "SK",
                                "security_token": "ST", "expiration": "x"},
                "refresh_token": "RT"})
        kp = ca.generate_dpop_keypair()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            creds = await ca.exchange_authorization_code(
                c, "THECODE", "verifier-xyz", 18080, kp)
        assert creds["access_key_id"] == "AK"
        assert creds["code_verifier"] == "verifier-xyz"
        assert creds["dpop_private_key_jwk"] == kp["private_key_jwk"]
        assert captured["dpop"]
        assert "authorization_code" in captured["body"]
        assert "code=THECODE" in captured["body"]
        assert "redirect_uri=" in captured["body"]

    @pytest.mark.asyncio
    async def test_request_token_refresh_expired_raises(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={
                "error": "invalid_grant",
                "error_code": "ExpiredRefreshToken"})
        kp = ca.generate_dpop_keypair()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            with pytest.raises(ca.RefreshTokenExpiredError):
                await ca.exchange_refresh_token(c, "rt", "v", kp)

    @pytest.mark.asyncio
    async def test_request_token_network_500(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")
        kp = ca.generate_dpop_keypair()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            with pytest.raises(RuntimeError, match="token request failed"):
                await ca.exchange_authorization_code(c, "code", "v", 1234, kp)

    @pytest.mark.asyncio
    async def test_checkin_claimed(self):
        """签到全流程: 积分账户→活动可领→claim→confirm→返回 credit"""
        seq = []

        def handler(req: httpx.Request) -> httpx.Response:
            seq.append(str(req.url))
            if "statistics/plugin" in str(req.url):
                return httpx.Response(200, json={
                    "code": 0, "data": {
                        "package": {"is_credit_package": True},
                        "metrics": []}})
            if "/v1/ops/delivery" in str(req.url):
                return httpx.Response(200, json={"code": 0, "data": {"items": [
                    {"type": "USER_LOGIN", "claimable": True,
                     "campaignId": 1, "status": "CLAIMABLE",
                     "benefitAmount": 1000}]}})
            if "/v1/ops/claim" in str(req.url):
                return httpx.Response(200, json={"code": 0, "data": {
                    "id": 77, "benefitAmount": 1000}})
            if "/v1/ops/confirm" in str(req.url):
                return httpx.Response(200, json={"code": 0, "data": {"ok": True}})
            return httpx.Response(404)

        creds = {"access_key_id": "AK", "secret_access_key": "SK",
                 "security_token": "ST"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            r = await ca.claim_daily_checkin(c, creds)
        assert r["kind"] == "claimed"
        assert r["credit"] == 1000
        # 4 个端点都打到了(账户/活动/claim/confirm)
        assert len(seq) == 4

    @pytest.mark.asyncio
    async def test_checkin_already_claimed(self):
        def handler(req: httpx.Request) -> httpx.Response:
            if "statistics/plugin" in str(req.url):
                return httpx.Response(200, json={"code": 0, "data": {
                    "package": {"is_credit_package": True}}})
            return httpx.Response(200, json={"code": 0, "data": {"items": [
                {"type": "USER_LOGIN", "claimable": False,
                 "status": "CLAIMED", "campaignId": 1}]}})
        creds = {"access_key_id": "AK", "secret_access_key": "SK"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            r = await ca.claim_daily_checkin(c, creds)
        assert r["kind"] == "already-claimed"

    @pytest.mark.asyncio
    async def test_checkin_inactive_token_account(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 0, "data": {
                "package": {"is_credit_package": False,
                            "is_token_package": True}}})
        creds = {"access_key_id": "AK", "secret_access_key": "SK"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            r = await ca.claim_daily_checkin(c, creds)
        assert r["kind"] == "inactive"
        assert "Token" in r["message"]

    @pytest.mark.asyncio
    async def test_checkin_business_error(self):
        def handler(req: httpx.Request) -> httpx.Response:
            if "statistics/plugin" in str(req.url):
                return httpx.Response(200, json={"code": 0, "data": {
                    "package": {"is_credit_package": True}}})
            return httpx.Response(200, json={"code": 4001, "message": "系统繁忙"})
        creds = {"access_key_id": "AK", "secret_access_key": "SK"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            r = await ca.claim_daily_checkin(c, creds)
        assert r["kind"] == "failed"
        assert "系统繁忙" in r["message"]


# ============================================================
# 3) WorkBuddy
# ============================================================

class TestWorkbuddy:
    def test_ua_by_model_family(self):
        assert wb.resolve_user_agent(wb.WORKBUDDY, "gpt-5.6-sol") == wb.WORKBUDDY_UA_INTL
        assert wb.resolve_user_agent(wb.WORKBUDDY, "glm-5.3") == wb.WORKBUDDY_UA_CN
        assert wb.resolve_user_agent(wb.WORKBUDDY, "kimi-k3") == wb.WORKBUDDY_UA_CN
        assert wb.resolve_user_agent(wb.WORKBUDDY, "hy4-preview") == wb.WORKBUDDY_UA_CN
        # 未知线 → 默认(国际)
        assert wb.resolve_user_agent(wb.WORKBUDDY, "mystery") == wb.WORKBUDDY_UA_INTL
        # buddy 永远 IDE UA
        assert wb.resolve_user_agent(wb.CODEBUDDY, "gpt-5") == "CodeBuddyIDE/1.106.1"

    def test_token_data_expiry_relative(self):
        """expiresIn 相对秒 → 以 JWT iat 为基线换算(照抄插件)"""
        import base64
        iat = 1_700_000_000
        payload = base64.urlsafe_b64encode(
            json.dumps({"iat": iat}).encode()).rstrip(b"=").decode()
        access = f"a.{payload}.b"
        token = wb.parse_token_data({
            "accessToken": access, "refreshToken": "rt",
            "expiresIn": 7200, "tokenType": "Bearer",
            "domain": "www.workbuddy.ai"})
        assert int(token["expires_at"]) == (iat + 7200) * 1000
        assert token["domain"] == "www.workbuddy.ai"

    def test_token_data_expiry_absolute(self):
        token = wb.parse_token_data({
            "accessToken": "x", "expiresAt": str(1_800_000_000_000)})
        assert token["expires_at"] == str(1_800_000_000_000)

    def test_credential_expires_seconds_string(self):
        assert wb.credential_expires_at_ms(
            {"expires_at": "1800000000"}) == 1_800_000_000_000

    def test_credential_expires_from_jwt(self):
        import base64
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": 1_900_000_000}).encode()).rstrip(b"=").decode()
        assert wb.credential_expires_at_ms(
            {"access_token": f"a.{payload}.b"}) == 1_900_000_000_000

    def test_chat_headers_contract(self):
        creds = {"access_token": "AT", "domain": "www.workbuddy.ai"}
        h = wb.chat_headers(creds, "workbuddy", "glm-5.3")
        assert h["Authorization"] == "Bearer AT"
        assert h["X-Domain"] == "www.workbuddy.ai"
        assert h["X-Product-Code"] == "workbuddy"
        assert h["X-Agent-Purpose"] == "conversation"
        assert h["X-IDE-Name"] == "WorkBuddy"
        assert h["X-IDE-Version"] == "5.5.2"
        assert h["User-Agent"] == wb.WORKBUDDY_UA_CN  # glm 走国内 UA

    def test_chat_headers_buddy_variant(self):
        creds = {"access_token": "AT", "domain": ""}
        h = wb.chat_headers(creds, "buddy", "hy3")
        assert h["X-IDE-Name"] == "CodeBuddy"
        assert h["User-Agent"] == "CodeBuddyIDE/1.106.1"

    def test_credentials_roundtrip(self):
        token = {"access_token": "AT", "refresh_token": "RT",
                 "expires_at": "1700000000000", "domain": "d"}
        pkg = wb.credentials_package(token, "workbuddy")
        parsed = wb.parse_credentials_package(pkg, "workbuddy")
        assert parsed["channel"] == "workbuddy"
        assert parsed["access_token"] == "AT"
        assert wb.parse_credentials_package(pkg, "buddy") is None

    @pytest.mark.asyncio
    async def test_auth_state_flow(self):
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.path == "/v2/plugin/auth/state"
            assert req.headers["X-Domain"] == "www.workbuddy.ai"
            assert req.headers["User-Agent"] == wb.WORKBUDDY_UA_INTL
            return httpx.Response(200, json={
                "code": 0, "data": {"state": "S-1", "authUrl": "https://auth/page"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            r = await wb.start_auth_state(c, wb.WORKBUDDY)
        assert r == {"state": "S-1", "auth_url": "https://auth/page"}

    @pytest.mark.asyncio
    async def test_auth_state_error(self):
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(500, json={}))) as c:
            with pytest.raises(RuntimeError):
                await wb.start_auth_state(c, wb.WORKBUDDY)

    @pytest.mark.asyncio
    async def test_poll_not_ready_then_ok(self):
        state = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            state["n"] += 1
            if state["n"] < 3:
                return httpx.Response(200, json={"code": wb.CODE_TOKEN_NOT_READY})
            return httpx.Response(200, json={
                "data": {"accessToken": "AT", "refreshToken": "RT",
                         "expiresIn": 3600, "domain": "d.test"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            token = await wb.poll_token(c, "S-1", wb.WORKBUDDY,
                                        timeout_ms=5000, interval_ms=10)
        assert token["access_token"] == "AT"
        assert token["refresh_token"] == "RT"
        assert state["n"] == 3

    @pytest.mark.asyncio
    async def test_poll_timeout_returns_none(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": wb.CODE_TOKEN_NOT_READY})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            assert await wb.poll_token(c, "S", wb.WORKBUDDY,
                                       timeout_ms=80, interval_ms=10) is None

    @pytest.mark.asyncio
    async def test_refresh_headers_and_parse(self):
        captured = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["auth"] = req.headers.get("Authorization")
            captured["rt"] = req.headers.get("X-Refresh-Token")
            captured["src"] = req.headers.get("X-Auth-Refresh-Source")
            return httpx.Response(200, json={"data": {
                "accessToken": "AT2", "refreshToken": "RT2",
                "expiresIn": 3600}})
        creds = {"access_token": "AT1", "refresh_token": "RT1", "domain": ""}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            new = await wb.refresh_token(c, creds, wb.WORKBUDDY)
        assert new["access_token"] == "AT2"
        assert captured["auth"] == "Bearer AT1"
        assert captured["rt"] == "RT1"
        assert captured["src"] == "ide-main"

    @pytest.mark.asyncio
    async def test_refresh_expired_returns_none(self):
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(401, json={}))) as c:
            assert await wb.refresh_token(
                c, {"access_token": "x", "refresh_token": "y"},
                wb.WORKBUDDY) is None

    @pytest.mark.asyncio
    async def test_refresh_no_rt_returns_none(self):
        assert await wb.refresh_token(
            None, {"access_token": "x", "refresh_token": ""},
            wb.WORKBUDDY) is None

    @pytest.mark.asyncio
    async def test_fetch_models_config_fallback(self):
        """config 不可解析 → 返回兜底表(非空)"""
        def handler(req):
            return httpx.Response(200, json={"weird": 1})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            models = await wb.fetch_models(c, {"access_token": "AT"}, "workbuddy")
        assert any(m["id"] == "kimi-k3" for m in models)
        assert any(m["id"] == "deepseek-v4.1-flash" for m in models)

    @pytest.mark.asyncio
    async def test_fetch_models_from_config(self):
        def handler(req):
            return httpx.Response(200, json={"agentModels": [
                {"id": "kimi-k4", "name": "Kimi-K4"},
                {"model_id": "glm-6.0"}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            models = await wb.fetch_models(c, {"access_token": "AT"}, "workbuddy")
        assert {"id": "kimi-k4", "name": "Kimi-K4"} in models
        assert {"id": "glm-6.0", "name": "glm-6.0"} in models


# ============================================================
# 4) 注册表路由
# ============================================================

class TestRegistry:
    def test_provider_classification(self):
        assert is_channel_provider("codearts")
        assert is_channel_provider("workbuddy")
        assert is_channel_provider("buddy")
        assert not is_channel_provider("NVIDIA")

    def test_parse_credentials_dispatch(self):
        pkg = wb.credentials_package({"access_token": "AT"}, "buddy")
        assert parse_credentials("buddy", pkg)["access_token"] == "AT"
        assert parse_credentials("NVIDIA", pkg) is None
        assert parse_credentials("buddy", "plain-key") is None

    def test_workbuddy_headers_via_registry(self):
        pkg = wb.credentials_package(
            {"access_token": "AT", "domain": "", "refresh_token": "r"}, "workbuddy")
        h = channel_headers("workbuddy", pkg, "kimi-k3")
        assert h["X-Agent-Purpose"] == "conversation"
        assert h["User-Agent"] == wb.WORKBUDDY_UA_CN

    def test_plain_key_no_headers(self):
        """普通渠道的明文 key 不触发渠道头注入"""
        assert channel_headers("workbuddy", "sk-gw-plain", "x") == {}

    def test_codearts_signed_via_registry(self):
        pkg = ca.credentials_package({"access_key_id": "AK",
                                      "secret_access_key": "SK",
                                      "security_token": "ST"})
        body = json.dumps({"model": "glm-5.3-flash", "messages": []}).encode()
        h = channel_signed_headers(
            "codearts", pkg, "POST", ca.CHAT_API_BASE + "/chat/completions", body)
        assert h["Authorization"].startswith("SDK-HMAC-SHA256 Access=AK")
        assert "maas_type" in h  # benefit 模型
        assert "Chat-Id" in h and "Session-Id" in h

    def test_codearts_signed_nonbenefit(self):
        pkg = ca.credentials_package({"access_key_id": "AK",
                                      "secret_access_key": "SK",
                                      "security_token": "ST"})
        body = json.dumps({"model": "some-paid-model", "messages": []}).encode()
        h = channel_signed_headers("codearts", pkg, "POST",
                                   ca.CHAT_API_BASE + "/chat/completions", body)
        assert h["Authorization"].startswith("SDK-HMAC-SHA256 Access=AK")

    def test_registry_cline(self):
        pkg = json.dumps({"channel": "cline", "access_token": "workos:t",
                          "refresh_token": "r"})
        h = channel_headers("cline", pkg, "cline-free/stealth")
        assert h["X-CLIENT-TYPE"] == "cline-sdk"
