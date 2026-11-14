# -*- coding: utf-8 -*-
"""TRAE / Qoder / Cline 登录端到端: 登录 → 建 Provider+独立路由组 → 其它工具按真模型名调用 → 签到。

mock 渠道上游(授权/换token/模型列表/推理SOLO/信封/签到), 驱动 /api/channels 全链,
再经 /v1/chat/completions 与 /v1/messages 用 "trae-glm-5.2" / "qodercn-Qwen3.8-Flash" 等
真实模型名命中(验证"每个工具自己的分组模型")。
"""

import asyncio
import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine

import backend.api.anthropic as anthropic_mod
import backend.api.channels as channels_mod
import backend.api.proxy as proxy_mod
import backend.core.checkin as checkin_mod
import backend.core.channel_auth.qoder as qoder_mod
import backend.models as models_mod
from backend.auth.security import create_access_token, encrypt_secret
from backend.main import app

TRAEE = "trae"
QODER = "qodercn"


# ---------- mock: 各渠道上游 + 推理端点 ----------

TRAEE_SOLO_SSE = (
    b'event:output\ndata:{"response":"TRAE says hi"}\n\n'
    b'event:done\ndata:{"finish_reason":"stop"}\n\n')
QODER_ENVELOPED = (
    b'data:{"body":"{\\"choices\\":[{\\"index\\":0,\\"delta\\":{\\"content\\":\\"Qoder says hi\\"},\\"finish_reason\\":null}]}"}\n\n'
    b'data:{"body":"[DONE]"}\n\n')


def make_transport(captured: list):
    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        captured.append((host + path, dict(request.headers)))

        # ---- TRAE 登录 + 模型 + 推理 ----
        if host.endswith("api.trae.com.cn") and path.endswith("ExchangeToken"):
            return httpx.Response(200, json={"Result": {
                "Token": "trae-jwt", "RefreshToken": "trae-rt",
                "TokenExpireAt": 1_900_000_000}})
        if host.endswith("api.trae.com.cn") and path.endswith("GetUserInfo"):
            return httpx.Response(200, json={"Result": {
                "UserID": "tu1", "ScreenName": "trae-user"}})
        if host.endswith("mchost.guru") and path.endswith("batch_get_detail_param"):
            return httpx.Response(200, json={"result": {"model_detail_list": [
                {"model_name": "glm-5.2", "display_name": "GLM-5.2"},
                {"model_name": "kimi-k3", "display_name": "Kimi K3"}]}})
        if host.endswith("mchost.guru") and path.endswith("llm_utils_chat"):
            return httpx.Response(200, content=TRAEE_SOLO_SSE,
                                  headers={"content-type": "text/event-stream"})
        if host.endswith("api.trae.cn") and "checkin_credits/claim" in path:
            return httpx.Response(200, json={"code": 0})
        if host.endswith("api.trae.cn") and "checkin_credits/status" in path:
            return httpx.Response(200, json={"code": 0, "enable": True,
                                             "checked_in": False, "credits": 300,
                                             "streak_days": 2})

        # ---- Qoder 设备码 + 积分 ----
        if host.endswith("openapi.qoder.com.cn") and path.endswith("deviceToken/poll"):
            return httpx.Response(200, json={
                "token": "qoder-tk", "refresh_token": "qoder-rt",
                "expires_at": 1_900_000_000_000, "user_id": "qu1"})
        if host.endswith("openapi.qoder.com.cn") and path.endswith("/userinfo"):
            return httpx.Response(200, json={"nickname": "qoder-user"})
        if host.endswith("openapi.qoder.com.cn") and "campaigns" in path \
                and path.endswith("/claim"):
            return httpx.Response(200, json={"status": "CLAIMED",
                                             "benefit": {"amount": 100}})
        if host.endswith("openapi.qoder.com.cn") and "me/campaigns" in path:
            return httpx.Response(200, json={"showCampaign": True, "claimable": True,
                                             "campaigns": [
                        {"campaignId": "c1", "actionType": "CLAIM_BENEFIT",
                         "claimStatus": "CLAIMABLE", "benefit": {"amount": 100}}]})
        # Qoder 推理(加密端点): 由 mock 的 sign_infer_request 指向本地假 URL,
        # 这里拦 gateway.qoder.com.cn 任意 POST 返回信封流
        if host.endswith("gateway.qoder.com.cn"):
            return httpx.Response(200, content=QODER_ENVELOPED,
                                  headers={"content-type": "text/event-stream"})

        # ---- Cline 设备码 + 模型 + 推理 ----
        if host.endswith("api.workos.com") and path.endswith("/authorize/device"):
            return httpx.Response(200, json={
                "device_code": "dc", "user_code": "ABCD",
                "verification_uri": "https://cline.bot/activate",
                "verification_uri_complete": "https://cline.bot/activate?uc=ABCD",
                "interval": 1})
        if host.endswith("api.workos.com") and path.endswith("/authenticate"):
            return httpx.Response(200, json={
                "access_token": "wo-at", "refresh_token": "wo-rt", "user_id": "wu"})
        if host.endswith("api.cline.bot") and path.endswith("/auth/register"):
            return httpx.Response(200, json={
                "token": "workos:cline-tk", "refreshToken": "wo-rt"})
        if host.endswith("api.cline.bot") and path.endswith("/models"):
            return httpx.Response(200, json={"data": [
                {"id": "cline-free/stealth"}, {"id": "anthropic/paid"}]})
        if host.endswith("api.cline.bot") and "recommended-models" in path:
            return httpx.Response(200, json={"data": []})
        if host.endswith("api.cline.bot") and path.endswith("/chat/completions"):
            body = json.loads(request.content)
            return httpx.Response(200, json={
                "id": "cl", "object": "chat.completion", "model": body.get("model"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": "Cline says hi"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 3}})

        # ---- LobsterAI 版本号 + 授权兑换 + 模型 + 推理 + 签到 ----
        if host.endswith("api-overmind.youdao.com"):
            return httpx.Response(200, json={"data": {"value": {"version": "2026.10.1"}}})
        if host.endswith("lobsterai-server.youdao.com") and path.endswith("/api/auth/exchange"):
            return httpx.Response(200, json={"code": 0, "data": {
                "accessToken": "lad-at", "refreshToken": "lad-rt", "expiresIn": 3600,
                "user": {"id": "lu1", "userId": "lacct1", "yid": "ly1",
                         "nickname": "小青龙"}}})
        if host.endswith("lobsterai-server.youdao.com") and path.endswith("/api/auth/refresh"):
            return httpx.Response(200, json={"code": 0, "data": {
                "accessToken": "lad-at2", "refreshToken": "", "expiresIn": 3600}})
        if host.endswith("lobsterai-server.youdao.com") and path.endswith("/api/models/available"):
            return httpx.Response(200, json={"code": 0, "data": [
                {"modelId": "glm-5.2", "modelName": "GLM-5.2", "contextWindow": 200000},
                {"modelId": "deepseek-v4-flash", "modelName": "DeepSeek V4 Flash"}]})
        if host.endswith("lobsterai-server.youdao.com") \
                and path.endswith("/api/proxy/v1/chat/completions"):
            body = json.loads(request.content)
            return httpx.Response(200, json={
                "id": "lad", "object": "chat.completion", "model": body.get("model"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": "LobsterAI says hi"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 3}})
        if host.endswith("lobsterai-server.youdao.com") \
                and path.endswith("/api/client-activities/slot"):
            return httpx.Response(200, json={"code": 0, "data": {
                "slotState": "available",
                "activity": {"activityCode": "daily-sign-in", "configRevision": 3}}})
        if host.endswith("lobsterai-server.youdao.com") and path.endswith("/context"):
            return httpx.Response(200, json={"code": 0, "data": {
                "state": {"claimedToday": False}, "actions": ["check_in"]}})
        if host.endswith("lobsterai-server.youdao.com") and path.endswith("/actions/check_in"):
            return httpx.Response(200, json={"code": 0, "data": {
                "result": {"creditsGranted": 30}}})

        raise AssertionError(f"unmocked: {host}{path}")
    return httpx.MockTransport(handler)


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "gw.db"
    engine = create_engine(f"sqlite:///{db.as_posix()}",
                           connect_args={"check_same_thread": False})
    models_mod.Base.metadata.create_all(engine)
    monkeypatch.setattr(models_mod, "_engine", None)
    monkeypatch.setattr(models_mod, "_get_engine", lambda: engine)
    monkeypatch.setattr(models_mod, "get_db_url",
                        lambda: f"sqlite:///{db.as_posix()}")
    from backend.core.circuit import circuit_breaker
    circuit_breaker._states.clear()
    channels_mod._PENDING_LOGINS.clear()

    captured: list = []
    transport = make_transport(captured)

    def _client(**kw):
        kw.pop("transport", None)
        return httpx.AsyncClient(transport=transport, **kw)
    import types
    import backend.core.upstream_http as uh
    monkeypatch.setattr(uh, "make_async", _client)
    fake_httpx = types.SimpleNamespace(AsyncClient=_client,
                                       HTTPError=httpx.HTTPError)
    monkeypatch.setattr(channels_mod, "httpx", fake_httpx)
    monkeypatch.setattr(checkin_mod, "httpx", fake_httpx)
    monkeypatch.setattr(qoder_mod, "httpx", fake_httpx)

    # 转发层共享 client 走 mock
    shared = httpx.AsyncClient(transport=transport)

    async def fake_get_client():
        return shared
    monkeypatch.setattr(proxy_mod, "get_http_client", fake_get_client)
    monkeypatch.setattr(anthropic_mod, "get_http_client", fake_get_client)

    # TRAE/Qoder/Cline 登录会 spawn 真 uvicorn 回调服; 测试直接驱动同款 app / 关轮询
    async def _no_serve(*a, **k):
        return None
    monkeypatch.setattr(channels_mod, "_serve_transient_login_server", _no_serve)

    # Qoder sign_infer_request: 用本地假网关 URL(信封流由上面 handler 供给)
    async def fake_sign(channel, creds, body):
        return {"url": "https://gateway.qoder.com.cn/algo/fake",
                "headers": {"Authorization": "Bearer COSY.fake",
                            "Content-Type": "application/json"},
                "body": json.dumps(body)}
    monkeypatch.setattr(qoder_mod, "sign_infer_request", fake_sign)
    # machine identity 不真跑 exe
    monkeypatch.setattr(qoder_mod, "resolve_machine_identity", lambda: ("mtok", "mtype"))

    state_file = tmp_path / "checkin_state.json"
    monkeypatch.setattr(checkin_mod, "_state_path", lambda: state_file)

    s = models_mod.get_session()
    u = models_mod.User(username="t", email="t@t.local", password_hash="x",
                        api_key="sk-gw-t", role="user", is_active=True,
                        quota_daily=1000, quota_used_today=0)
    s.add(u); s.commit(); s.refresh(u)
    uid = u.id
    s.close()
    yield {"user_id": uid, "jwt": create_access_token(uid), "captured": captured}


@pytest.mark.asyncio
async def test_trae_login_model_invoke_checkin(env):
    jwt = {"Authorization": f"Bearer {env['jwt']}"}
    api = {"x-api-key": "sk-gw-t"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # 1) 登录
        r = await c.post("/api/channels/trae/login", headers=jwt)
        assert r.status_code == 200
        login_id = r.json()["login_id"]
        assert "trae.cn/authorization" in r.json()["auth_url"]

        # 驱动同款回调 app: 模拟 /authorize 回跳(带 refreshToken+userInfo)
        import urllib.parse
        cb_app, state = channels_mod._build_trae_callback_app(login_id)
        userinfo = urllib.parse.quote(json.dumps({"UserID": "tu1", "ScreenName": "n"}))
        async with AsyncClient(transport=ASGITransport(app=cb_app),
                               base_url="http://127.0.0.1:18080") as cc:
            r = await cc.get(f"/authorize?refreshToken=tr-rt&userInfo={userinfo}")
        assert r.status_code == 200

        r = await c.get(f"/api/channels/login/{login_id}", headers=jwt)
        assert r.json()["status"] == "done", r.text
        assert {"id": "glm-5.2", "name": "GLM-5.2"} in r.json()["models"]

        # 2) 真模型名进 /v1/models + 独立路由组
        r = await c.get("/v1/models", headers=api)
        ids = [m["id"] for m in r.json()["data"]]
        assert "trae-glm-5.2" in ids

        # 3) 其它工具(OpenAI + Anthropic 面)按真模型名调用
        r = await c.post("/v1/chat/completions", headers=api, json={
            "model": "trae-glm-5.2",
            "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200 and "TRAE says hi" in r.text
        r = await c.post("/v1/messages", headers=api, json={
            "model": "trae-glm-5.2", "max_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200 and "TRAE says hi" in r.text

        # 4) 每日签到
        r = await c.post("/api/channels/checkin/trae", headers=jwt)
        assert r.status_code == 200
        assert r.json()["kind"] == "claimed" and r.json()["credit"] == 300


@pytest.mark.asyncio
async def test_qodercn_login_and_invoke(env):
    jwt = {"Authorization": f"Bearer {env['jwt']}"}
    api = {"x-api-key": "sk-gw-t"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/channels/qodercn/login", headers=jwt)
        login_id = r.json()["login_id"]
        assert "qoder.cn/device/selectAccounts" in r.json()["auth_url"]

        # 直接驱动后台轮询(它 mock 了 httpx, poll 立即成功)
        await channels_mod._poll_qoder_token(login_id)
        r = await c.get(f"/api/channels/login/{login_id}", headers=jwt)
        done = r.json()
        assert done["status"] == "done", r.text
        # 对外 id=真名(内部代号在 code 字段)
        assert any(m["id"] == "Qwen3.8-Flash" and m["code"] == "qfmodel"
                   for m in done["models"])

        r = await c.get("/v1/models", headers=api)
        ids = [m["id"] for m in r.json()["data"]]
        assert "qodercn-Qwen3.8-Flash" in ids

        r = await c.post("/v1/chat/completions", headers=api, json={
            "model": "qodercn-Qwen3.8-Flash",
            "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200 and "Qoder says hi" in r.text

        # Qoder 积分领取
        r = await c.post("/api/channels/checkin/qodercn", headers=jwt)
        assert r.json()["kind"] == "claimed" and r.json()["credit"] == 100


@pytest.mark.asyncio
async def test_cline_login_and_invoke(env):
    jwt = {"Authorization": f"Bearer {env['jwt']}"}
    api = {"x-api-key": "sk-gw-t"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/channels/cline/login", headers=jwt)
        login_id = r.json()["login_id"]
        assert "cline.bot/activate" in r.json()["auth_url"]

        await channels_mod._poll_cline_token(login_id)
        r = await c.get(f"/api/channels/login/{login_id}", headers=jwt)
        done = r.json()
        assert done["status"] == "done", r.text
        assert any(m["id"] == "cline-free/stealth" for m in done["models"])

        r = await c.get("/v1/models", headers=api)
        ids = [m["id"] for m in r.json()["data"]]
        assert "cline-cline-free/stealth" in ids

        r = await c.post("/v1/chat/completions", headers=api, json={
            "model": "cline-cline-free/stealth",
            "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200 and "Cline says hi" in r.text


@pytest.mark.asyncio
async def test_lobsterai_login_and_invoke(env):
    """LobsterAI: 浏览器授权(本地回调 code+state) → token → 模型 → 三协议面调用 → 签到。"""
    jwt = {"Authorization": f"Bearer {env['jwt']}"}
    api = {"x-api-key": "sk-gw-t"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/channels/lobsterai/login", headers=jwt)
        assert r.status_code == 200, r.text
        login_id = r.json()["login_id"]
        auth_url = r.json()["auth_url"]
        assert "lobsterai.youdao.com/portal#/login" in auth_url
        assert "127.0.0.1%3A18731%2Fauth%2Fcallback" in auth_url

        # 驱动同款回调 app: 模拟浏览器回跳 /auth/callback?code=...&state=...
        expected = channels_mod._PENDING_LOGINS[login_id]["session"]["state"]
        cb_app, _state = channels_mod._build_lobsterai_callback_app(login_id)
        async with AsyncClient(transport=ASGITransport(app=cb_app),
                               base_url="http://127.0.0.1:18731") as cc:
            r = await cc.get(f"/auth/callback?code=lc-1&state={expected}")
            assert r.status_code == 200
            assert "登录完成" in r.text

        r = await c.get(f"/api/channels/login/{login_id}", headers=jwt)
        done = r.json()
        assert done["status"] == "done", r.text
        assert any(m["id"] == "glm-5.2" and m["name"] == "GLM-5.2" for m in done["models"])
        assert done["model_prefix"] == "lobsterai-"

        # 真模型名进 /v1/models + 独立路由组
        r = await c.get("/v1/models", headers=api)
        ids = [m["id"] for m in r.json()["data"]]
        assert "lobsterai-glm-5.2" in ids

        r = await c.get("/api/routers", headers=jwt)
        group = r.json()["data"]["lobsterai"]
        assert "lobsterai-glm-5.2" in group["models"]

        # 三协议面都按真模型名调用
        r = await c.post("/v1/chat/completions", headers=api, json={
            "model": "lobsterai-glm-5.2",
            "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200 and "LobsterAI says hi" in r.text
        r = await c.post("/v1/messages", headers=api, json={
            "model": "lobsterai-glm-5.2", "max_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200 and "LobsterAI says hi" in r.text
        r = await c.post("/v1/responses", headers=api, json={
            "model": "lobsterai-glm-5.2", "input": "hi"})
        assert r.status_code == 200 and "LobsterAI says hi" in r.text

        # 每日活动签到(HTTP 积分 30)
        r = await c.post("/api/channels/checkin/lobsterai", headers=jwt)
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "claimed" and r.json()["credit"] == 30
