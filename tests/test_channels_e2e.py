# ============================================================
# 白嫖渠道端到端流程测试: 登录 → 建渠道 Provider → 真实模型可选 → 转发 → 签到
#
# 验证核心诉求: "渠道作为专门 Provider, 在其它工具里选真正的模型使用"
#   workbuddy-kimi-k3 / codearts-glm-5.3-flash 出现在 /v1/models,
#   /v1/chat/completions 与 /v1/messages 都能用这些名字调用,
#   上游收到渠道专属头(WB: X-Agent-Purpose; CodeArts: SDK-HMAC-SHA256+maas_type),
#   CodeArts 每日积分一键签到 + UTC+8 防重。
# ============================================================

import json
import time
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine

import backend.api.anthropic as anthropic_mod
import backend.api.channels as channels_mod
import backend.api.proxy as proxy_mod
import backend.core.checkin as checkin_mod
import backend.models as models_mod
from backend.auth.security import create_access_token
from backend.main import app

WB_HOST = "www.workbuddy.ai"
CA_HOST = "snap-access.cn-north-4.myhuaweicloud.com"
STS_HOST = "sts.cn-north-4.myhuaweicloud.com"
GW_HOST = "opengw.developer.huaweicloud.com"


# ============================================================
# mock 上游: 按 host+path 路由, 记录收到的头
# ============================================================

def make_mock(captured: list):
    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        captured.append((f"{host}{path}", dict(request.headers)))

        # ---- WorkBuddy ----
        if host == WB_HOST:
            if path == "/v2/plugin/auth/state":
                return httpx.Response(200, json={
                    "code": 0, "data": {"state": "ST-1",
                                        "authUrl": "https://auth.example/wb-login"}})
            if path == "/v2/plugin/auth/token":
                return httpx.Response(200, json={
                    "data": {"accessToken": "wb-at-1", "refreshToken": "wb-rt-1",
                             "expiresIn": 7200, "domain": WB_HOST}})
            if path == "/v2/plugin/login/account":
                return httpx.Response(200, json={"code": 0, "data": {
                    "uid": "wb-uid-1", "nickname": "wbuser",
                    "enterpriseId": "", "type": "personal"}})
            if path == "/v3/config":
                return httpx.Response(200, json={"agentModels": [
                    {"id": "kimi-k3", "name": "Kimi-K3"},
                    {"id": "deepseek-v4.1-flash", "name": "DeepSeek-V4.1-Flash"},
                ]})
            if path == "/v2/plugin/auth/token/refresh":
                # 保活/自愈路径: token 剩余寿命低于提前量
                # (CRED_REFRESH_AHEAD_MINUTES, 默认 2h)时会提前刷新
                return httpx.Response(200, json={
                    "data": {"accessToken": "wb-at-2", "refreshToken": "wb-rt-2",
                             "expiresIn": 86400, "domain": WB_HOST}})
            if path == "/v2/chat/completions":
                body = json.loads(request.content)
                if body.get("stream"):
                    sse = (
                        'data: {"choices":[{"index":0,"delta":{"content":"Hello WB"},'
                        '"finish_reason":null}]}\n\n'
                        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                        'data: [DONE]\n\n')
                    return httpx.Response(200, text=sse,
                                          headers={"content-type": "text/event-stream"})
                return httpx.Response(200, json={
                    "id": "wb-1", "object": "chat.completion",
                    "model": body.get("model"),
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant",
                                             "content": "Hello from WorkBuddy"}}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 4}})

        # ---- CodeArts: STS token ----
        if host == STS_HOST and path == "/v1/oauth2/tokens":
            return httpx.Response(200, json={
                "credentials": {
                    "access_key_id": "AK-TEST", "secret_access_key": "SK-TEST",
                    "security_token": "ST-TEST",
                    "expiration": "2099-01-01T00:00:00Z"},
                "refresh_token": "RT-TEST"})

        # ---- CodeArts: 模型目录 ----
        if host == GW_HOST and path == "/api/v1/gateway/config":
            return httpx.Response(200, json={"result": {"models": [
                {"model_id": "glm-5.3-flash", "model_name": "GLM-5.3-Flash"},
            ]}})
        if host == CA_HOST and path == "/v1/model/builtin":
            return httpx.Response(200, json={"models": [
                {"model_id": "deepseek-v4.1-flash-0815"}]})

        # ---- CodeArts: 推理(标准 OpenAI, 验证签名头到达) ----
        if host == CA_HOST and path == "/api/v2/chat/completions":
            body = json.loads(request.content)
            return httpx.Response(200, json={
                "id": "ca-1", "object": "chat.completion",
                "model": body.get("model"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": "Hello from CodeArts"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3}})

        # ---- CodeArts: 签到三连 ----
        if host == CA_HOST and path == "/snap-manager/v1/statistics/plugin":
            return httpx.Response(200, json={"code": 0, "data": {
                "package": {"is_credit_package": True}, "metrics": []}})
        if host == CA_HOST and path == "/v1/ops/delivery":
            return httpx.Response(200, json={"code": 0, "data": {"items": [
                {"type": "USER_LOGIN", "claimable": True, "campaignId": 1,
                 "status": "CLAIMABLE", "benefitAmount": 1000}]}})
        if host == CA_HOST and path == "/v1/ops/claim":
            return httpx.Response(200, json={"code": 0, "data": {
                "id": 77, "benefitAmount": 1000}})
        if host == CA_HOST and path == "/v1/ops/confirm":
            return httpx.Response(200, json={"code": 0, "data": {"ok": True}})

        raise AssertionError(f"unmocked upstream: {host}{path}")
    return httpx.MockTransport(handler)


# ============================================================
# fixture: 独立临时库 + 用户 + 全上游 mock
# ============================================================

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
    from backend.core.channel_auth.codearts import BENEFIT_SET
    circuit_breaker._states.clear()
    BENEFIT_SET.discard("glm-5.3-flash")  # 保证 benefit 判定来自"网关目录实测"而非兜底
    channels_mod._PENDING_LOGINS.clear()

    # 签到状态落盘到 tmp, 不污染项目 data/
    state_file = tmp_path / "checkin_state.json"
    monkeypatch.setattr(checkin_mod, "_state_path", lambda: state_file)

    captured: list = []
    transport = make_mock(captured)

    def _make_async(**kw):
        kw.pop("transport", None)
        return httpx.AsyncClient(transport=transport, **kw)

    # 出站总缝: 网关所有真实网络都从 upstream_http 工厂出 → mock 它
    import backend.core.upstream_http as uh
    monkeypatch.setattr(uh, "make_async", _make_async)
    fake_httpx = SimpleNamespace(
        AsyncClient=_make_async, HTTPError=httpx.HTTPError)
    monkeypatch.setattr(channels_mod, "httpx", fake_httpx)
    monkeypatch.setattr(checkin_mod, "httpx", fake_httpx)

    # 转发层的共享 client 也走 mock
    shared = httpx.AsyncClient(transport=transport)

    async def fake_get_http_client():
        return shared

    monkeypatch.setattr(proxy_mod, "get_http_client", fake_get_http_client)
    monkeypatch.setattr(anthropic_mod, "get_http_client", fake_get_http_client)

    # CodeArts 登录端点会 spawn 真 uvicorn 回调 server; 测试里停掉,
    # 由测试直接驱动同款 ASGI 回调 app
    async def _no_serve(login_id, port):
        return None
    monkeypatch.setattr(channels_mod, "_serve_codearts_callback", _no_serve)

    # 用户
    s = models_mod.get_session()
    try:
        u = models_mod.User(username="chan", email="chan@t.local",
                            password_hash="x", api_key="sk-gw-chan",
                            role="user", is_active=True,
                            quota_daily=1000, quota_used_today=0)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    finally:
        s.close()

    yield {"user_id": uid, "jwt": create_access_token(uid),
           "captured": captured, "api_key": "sk-gw-chan"}
    import asyncio
    asyncio.get_event_loop()  # shared 在测试里关闭


@pytest.mark.asyncio
async def test_full_channel_flow(env):
    from httpx import ASGITransport, AsyncClient
    jwt = {"Authorization": f"Bearer {env['jwt']}"}
    api = {"x-api-key": env["api_key"]}
    transport = env["captured"]  # list alias

    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://t", timeout=30) as c:
        # ═══ 1. WorkBuddy 登录(state 轮询) → 建 Provider ═══
        r = await c.post("/api/channels/workbuddy/login", headers=jwt)
        assert r.status_code == 200, r.text
        login_id = r.json()["login_id"]
        assert r.json()["auth_url"].startswith("https://auth.example/")

        # 轮询登录结果(第一次触发后台轮询+1.2s 等待)
        deadline = time.time() + 10
        status = None
        while time.time() < deadline:
            r = await c.get(f"/api/channels/login/{login_id}", headers=jwt)
            status = r.json().get("status")
            if status == "done":
                break
            await __import__("asyncio").sleep(0.3)
        assert status == "done", r.text
        done = r.json()
        model_ids = [m["id"] for m in done["models"]]
        assert "kimi-k3" in model_ids and "deepseek-v4.1-flash" in model_ids

        # ═══ 2. 渠道模型出现在 /v1/models(专门渠道, 真实模型名) ═══
        r = await c.get("/v1/models", headers=api)
        assert r.status_code == 200, r.text
        ids = [m["id"] for m in r.json()["data"]]
        assert "workbuddy-kimi-k3" in ids
        assert "workbuddy-deepseek-v4.1-flash" in ids

        # ═══ 3. OpenAI 面: 按真模型名调用, 上游收到渠道专属头 ═══
        transport_before = len(transport)
        r = await c.post("/v1/chat/completions", headers=api, json={
            "model": "workbuddy-kimi-k3",
            "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
        # workbuddy/buddy 只吃流式(上游 11101), 非流式请求在 forward_non_stream
        # 里被改成 stream=True 再聚合回来, 所以拿到的是 mock 的 SSE 内容。
        assert r.json()["choices"][0]["message"]["content"] == "Hello WB"
        # 找到那次 WB chat 转发的头
        wb_chat = [(h, hd) for h, hd in transport[transport_before:]
                   if h == f"{WB_HOST}/v2/chat/completions"]
        assert wb_chat, "workbuddy 推理未转发"
        hd = wb_chat[-1][1]
        assert hd["authorization"] == "Bearer wb-at-1"
        assert hd["x-agent-purpose"] == "conversation"
        assert hd["x-product-code"] == "workbuddy"
        # kimi-k3 走国内 UA(按模型家族切换)
        assert "WorkBuddy/5.5.2 WorkBuddy/5.5.2" in hd["user-agent"]

        # ═══ 4. Anthropic 面(Claude Code): 流式 + 渠道头 ═══
        transport_before = len(transport)
        r = await c.post("/v1/messages", headers=api, json={
            "model": "workbuddy-deepseek-v4.1-flash", "max_tokens": 100,
            "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
        assert "message_start" in r.text and "message_stop" in r.text
        assert "Hello WB" in r.text
        wb_chat = [(h, hd) for h, hd in transport[transport_before:]
                   if h == f"{WB_HOST}/v2/chat/completions"]
        assert wb_chat and wb_chat[-1][1]["x-product-code"] == "workbuddy"

        # ═══ 5. CodeArts 登录(OAuth code 流 + 本地回调 app) → 建 Provider ═══
        # 真 uvicorn 回调 server 不便在测试里起, 直接驱动同款 ASGI app
        # (login 端点已被 monkeypatch 不 spawn 真 server, 见下方 fixture)
        r = await c.post("/api/channels/codearts/login", headers=jwt)
        assert r.status_code == 200, r.text
        ca_login = r.json()["login_id"]
        assert "codearts.huaweicloud.com" in r.json()["auth_url"]
        assert "code_challenge_method=SHA-256" in r.json()["auth_url"]
        port = channels_mod._PENDING_LOGINS[ca_login]["callback_port"]
        assert port >= 10000  # 插件同款端口下限

        cb_app, state = channels_mod._build_oauth_callback_app(ca_login)
        async with AsyncClient(transport=ASGITransport(app=cb_app),
                               base_url=f"http://127.0.0.1:{port}") as cc:
            r = await cc.get("/oauth/callback?code=THE-CODE",
                             follow_redirects=False)
        assert r.status_code == 307
        assert "login_succeed=true" in r.headers["location"]

        done = channels_mod._PENDING_LOGINS[ca_login]
        assert done["status"] == "done"
        # 模拟前端轮询登录结果 → 建 Provider
        r = await c.get(f"/api/channels/login/{ca_login}", headers=jwt)
        done = r.json()
        assert done["status"] == "done", r.text
        assert "glm-5.3-flash" in [m["id"] for m in done["models"]]

        r = await c.get("/v1/models", headers=api)
        ids = [m["id"] for m in r.json()["data"]]
        assert "codearts-glm-5.3-flash" in ids

        # ═══ 6. CodeArts 推理: 华为云 HMAC 签名头 + benefit 标记 ═══
        transport_before = len(transport)
        r = await c.post("/v1/chat/completions", headers=api, json={
            "model": "codearts-glm-5.3-flash",
            "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
        assert r.json()["choices"][0]["message"]["content"] == "Hello from CodeArts"
        ca_chat = [(h, hd) for h, hd in transport[transport_before:]
                   if h == f"{CA_HOST}/api/v2/chat/completions"]
        assert ca_chat, "codearts 推理未转发"
        hd = ca_chat[-1][1]
        assert hd["authorization"].startswith("SDK-HMAC-SHA256 Access=AK-TEST")
        assert "SignedHeaders=" in hd["authorization"]
        assert hd["maas_type"] == "benefit"          # 免费额度模型标记
        assert hd["x-security-token"] == "ST-TEST"   # DPoP 换来的 ST
        assert "chat-id" in hd and "session-id" in hd

        # ═══ 7. 一键签到(CodeArts): 领取成功 ═══
        r = await c.post("/api/channels/checkin", headers=jwt)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["claimed"] == 1
        assert out["total_credit"] == 1000

        # ═══ 8. 签到防重(UTC+8 同日第二次跳过) ═══
        r = await c.post("/api/channels/checkin", headers=jwt)
        out2 = r.json()
        assert out2["claimed"] == 0
        assert any(x.get("kind") == "skipped" for x in out2["results"])

        # ═══ 9. 渠道目录显示连接状态 ═══
        r = await c.get("/api/channels", headers=jwt)
        chans = {x["id"]: x for x in r.json()["channels"]}
        assert chans["workbuddy"]["connected"] is True
        assert chans["codearts"]["connected"] is True
        assert chans["codearts"]["has_checkin"] is True
        assert "codearts" in r.json()["checkin_channels"]
        assert chans["workbuddy"]["model_count"] == 2
