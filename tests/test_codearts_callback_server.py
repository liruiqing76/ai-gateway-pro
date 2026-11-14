# ============================================================
# CodeArts OAuth 回调 server 真机式测试:
# 用真 uvicorn 在 127.0.0.1 随机≥10000 端口起临时回调 app,
# 模拟华为云浏览器回跳(?code=), 验证 307→portal + 会话置 done。
# (e2e 测试里为避免起服跳过了这步, 这里补真起服的一条路径)
# ============================================================

import asyncio
import time

import httpx
import pytest
import uvicorn

import backend.api.channels as channels_mod


@pytest.mark.asyncio
async def test_real_callback_server_roundtrip(monkeypatch):
    login_id = "testlogin123"
    # 造一个 pending 会话(codearts)
    channels_mod._PENDING_LOGINS[login_id] = {
        "user_id": 1, "channel": "codearts",
        "pkce": {"code_verifier": "v", "code_challenge": "c"},
        "keypair": None,  # 不需要真 keypair: mock 掉 exchange
        "started_at": time.time(), "status": "pending",
    }

    # mock 掉 code 路(STS 交换 + 模型拉取), 聚焦"回调 server 本身"行为
    async def fake_handle(session_info, code, port):
        session_info["creds"] = {"access_key_id": "AK"}
        session_info["models"] = [{"id": "m1", "name": "M1"}]
        session_info["status"] = "done"
    monkeypatch.setattr(channels_mod, "_handle_codearts_code", fake_handle)

    port = await channels_mod._pick_callback_port()
    assert port >= 10000

    app, state = channels_mod._build_oauth_callback_app(login_id)
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    # 等 server 就绪
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started

    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"http://127.0.0.1:{port}/oauth/callback?code=REALCODE",
                follow_redirects=False)
        assert r.status_code == 307
        assert "codearts.huaweicloud.com/portal/login?login_succeed=true" in \
            r.headers["location"]
        assert r.headers["access-control-allow-origin"] == "*"
        sess = channels_mod._PENDING_LOGINS[login_id]
        assert sess["status"] == "done"
        assert sess["models"] == [{"id": "m1", "name": "M1"}]

        # 非 REDIRECT_PATH 404; OPTIONS 204
        async with httpx.AsyncClient() as c:
            r = await c.get(f"http://127.0.0.1:{port}/other")
            assert r.status_code == 404
            r = await c.options(f"http://127.0.0.1:{port}/oauth/callback")
            assert r.status_code == 204

        # 缺参数 → failed + login_succeed=false
        channels_mod._PENDING_LOGINS["bad1"] = {
            "user_id": 1, "channel": "codearts", "pkce": {"code_verifier": "v"},
            "started_at": time.time(), "status": "pending",
        }
        app2, _state2 = channels_mod._build_oauth_callback_app("bad1")
        server2 = uvicorn.Server(uvicorn.Config(
            app2, host="127.0.0.1", port=port + 1, log_level="error",
            lifespan="off"))
        task2 = asyncio.create_task(server2.serve())
        for _ in range(50):
            if server2.started:
                break
            await asyncio.sleep(0.05)
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"http://127.0.0.1:{port + 1}/oauth/callback",
                                follow_redirects=False)
            assert r.status_code == 307
            assert "login_succeed=false" in r.headers["location"]
            assert channels_mod._PENDING_LOGINS["bad1"]["status"] == "failed"
        finally:
            server2.should_exit = True
            await task2
    finally:
        server.should_exit = True
        await task
        channels_mod._PENDING_LOGINS.pop(login_id, None)
        channels_mod._PENDING_LOGINS.pop("bad1", None)


def test_pick_callback_port_in_range():
    async def _many():
        return [await channels_mod._pick_callback_port() for _ in range(5)]
    ports = asyncio.run(_many())
    assert all(10000 <= p < 65536 for p in ports)


def test_build_portal_login_result_url():
    url = channels_mod.codearts.build_portal_login_result_url(True)
    assert "login_succeed=true" in url and "uri_scheme=codearts-agent" in url
    url = channels_mod.codearts.build_portal_login_result_url(False)
    assert "login_succeed=false" in url
