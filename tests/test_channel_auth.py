# ============================================================
# 白嫖渠道(channel_auth)单元测试
#
# 覆盖:
#   1. channel_headers 注册表: 普通渠道零影响 / cline 渠道凭据包解出头
#   2. cline 登录器: 凭据包打包 / free 模型过滤 / token 刷新
#   3. forward_* 的附加头注入(mock transport 断言请求头)
# ============================================================

import json

import httpx
import pytest

from backend.core.channel_auth import channel_headers, is_channel_provider
from backend.core.channel_auth import cline


CLINE_CREDS = json.dumps({
    "channel": "cline",
    "access_token": "workos:abc123",
    "refresh_token": "rt-xyz",
})


class TestChannelHeaders:
    def test_unknown_provider_empty(self):
        """普通渠道(NVIDIA 等)返回空 dict —— 现有 Bearer 行为不变"""
        assert channel_headers("NVIDIA", "nv-key-123") == {}

    def test_bad_json_empty(self):
        assert channel_headers("cline", "not-json") == {}

    def test_wrong_channel_field_empty(self):
        """凭据包 channel 字段对不上 —— 不注入"""
        bad = json.dumps({"channel": "other", "access_token": "x"})
        assert channel_headers("cline", bad) == {}

    def test_cline_headers_full(self):
        h = channel_headers("cline", CLINE_CREDS)
        assert h["Authorization"] == "Bearer workos:abc123"
        assert h["HTTP-Referer"] == "https://cline.bot"
        assert h["X-Title"] == "Cline"
        assert h["X-CLIENT-TYPE"] == "cline-sdk"

    def test_is_channel_provider(self):
        assert is_channel_provider("cline") is True
        assert is_channel_provider("NVIDIA") is False


class TestClineLogin:
    def test_credentials_package(self):
        pkg = json.loads(cline.credentials_package({
            "token": "workos:t1", "refreshToken": "r1"}))
        assert pkg == {"channel": "cline",
                       "access_token": "workos:t1",
                       "refresh_token": "r1"}

    def test_credentials_package_alt_keys(self):
        pkg = json.loads(cline.credentials_package({
            "apiKey": "workos:t2", "refresh_token": "r2"}))
        assert pkg["access_token"] == "workos:t2"

    @pytest.mark.asyncio
    async def test_fetch_free_models_filters(self):
        """cline-free/ 前缀与 :free 后缀保留, 其它丢弃"""
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["X-CLIENT-TYPE"] == "cline-sdk"
            return httpx.Response(200, json={
                "data": [
                    {"id": "cline-free/stealth-space-bunny-alpha"},
                    {"id": "anthropic/claude-sonnet-4-5"},
                    {"id": "openai/gpt-5:free"},
                    {"id": "cline-free/mimo-v2.6-flash"},
                    {"id": "openrouter/deepseek-v3"},
                ]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            models = await cline.fetch_free_models(c, "workos:t")
        assert models == [
            "cline-free/stealth-space-bunny-alpha",
            "openai/gpt-5:free",
            "cline-free/mimo-v2.6-flash",
        ]

    @pytest.mark.asyncio
    async def test_refresh_failure_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            assert await cline.refresh_cline_token(c, "bad-rt") is None

    @pytest.mark.asyncio
    async def test_refresh_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"token": "workos:new"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            r = await cline.refresh_cline_token(c, "good-rt")
        assert r["token"] == "workos:new"

    @pytest.mark.asyncio
    async def test_device_poll_pending_then_ok(self, monkeypatch):
        """pending → 成功的轮询(mock 同步 sleep 防真实等待)"""
        state = {"n": 0}

        def fake_sleep(s):
            state["n"] += 1

        monkeypatch.setattr("backend.core.channel_auth.cline.time.sleep", fake_sleep)

        def handler(request: httpx.Request) -> httpx.Response:
            if state["n"] < 2:
                return httpx.Response(400, json={"error": "authorization_pending"})
            return httpx.Response(200, json={"access_token": "wa", "refresh_token": "wr"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            r = await cline.poll_device_token(c, "dc", interval=1, timeout=5)
        assert r["access_token"] == "wa"
        assert state["n"] == 2


class TestForwardHeaderInjection:
    """forward_stream/forward_non_stream 对 cline Provider 的附加头注入"""

    def _make_provider(self, name="cline", creds=CLINE_CREDS):
        from types import SimpleNamespace
        return SimpleNamespace(
            id=1, name=name, base_url="http://up.test/v1",
            api_key_encrypted="enc", free_only=True,
            max_concurrency=None, min_interval_ms=None,
        )

    @pytest.mark.asyncio
    async def test_nonstream_injects_cline_headers(self, monkeypatch):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(request.headers)
            return httpx.Response(200, json={
                "id": "c1", "object": "chat.completion", "model": "m",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            })

        import backend.core.proxy as core_proxy
        # 免流量整形阻塞
        monkeypatch.setattr(core_proxy.traffic_shaping, "pace", lambda *a, **k: _noop_async())
        monkeypatch.setattr(core_proxy.traffic_shaping, "acquire", lambda *a, **k: _noop_async(True))
        monkeypatch.setattr(core_proxy.traffic_shaping, "release", lambda *a, **k: None)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            resp, err = await core_proxy.forward_non_stream(
                client, self._make_provider(), CLINE_CREDS, "cline-free/x",
                {"model": "cline-free/x", "messages": [{"role": "user", "content": "hi"}]})
            assert err is None and resp is not None
            assert captured["x-client-type"] == "cline-sdk"
            assert captured["authorization"] == "Bearer workos:abc123"
            assert captured["http-referer"] == "https://cline.bot"
        finally:
            await client.aclose()


async def _noop_async(val=None):
    return val


class TestModelRateParsing:
    """渠道模型清单的计费倍率解析(上游目录 → model_rates 自动同步)"""

    def test_normalize_credits_formats(self):
        """workbuddy /v3/config credits 字段: "x1"/"1x"/"x0.29" → 浮点(照 pack.js 判据)"""
        from backend.core.channel_auth.workbuddy import normalize_credits
        assert normalize_credits("x1") == 1.0
        assert normalize_credits("1x") == 1.0
        assert normalize_credits("x0") == 0.0
        assert normalize_credits("x0.29") == 0.29
        assert normalize_credits("2.5x") == 2.5
        assert normalize_credits("X2") == 2.0
        assert normalize_credits("x1 说明文字") == 1.0  # 只取首段
        assert normalize_credits("") is None
        assert normalize_credits("  ") is None
        assert normalize_credits(None) is None
        assert normalize_credits("abc") is None
        assert normalize_credits(2) is None  # 非字符串不猜

    @pytest.mark.asyncio
    async def test_workbuddy_fetch_models_carries_rate(self):
        """/v3/config 带 credits 的模型条目输出 rate; 没带的不编造"""
        import httpx

        from backend.core.channel_auth import workbuddy

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"agentModels": [
                {"id": "kimi-k3", "name": "Kimi-K3", "credits": "x2"},
                {"id": "glm-5.1", "name": "GLM", "credits": "x0"},
                {"id": "no-rate", "name": "NR"},
            ]})

        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as client:
            models = await workbuddy.fetch_models(
                client, {"access_token": "wb-at-1"})
        by_id = {m["id"]: m for m in models}
        assert by_id["kimi-k3"]["rate"] == 2.0
        assert by_id["glm-5.1"]["rate"] == 0.0
        assert "rate" not in by_id["no-rate"]

    def test_lobsterai_parse_models_cost_multiplier(self):
        """lobsterai 模型条目 costMultiplier → rate; 缺失/非正数不编造"""
        from backend.core.channel_auth.lobsterai import parse_models
        body = {"code": 0, "data": [
            {"modelId": "m1", "modelName": "M1", "costMultiplier": 3},
            {"modelId": "m2", "modelName": "M2"},
            {"modelId": "m3", "modelName": "M3", "costMultiplier": 0},
        ]}
        models = parse_models(body)
        by_id = {m["id"]: m for m in models}
        assert by_id["m1"]["rate"] == 3.0
        assert "rate" not in by_id["m2"]
        assert "rate" not in by_id["m3"]


class TestQoderModelCatalog:
    """qoder/qodercn 全量模型清单(带积分倍率)"""

    def test_models_full_catalog_with_rates(self):
        """登录/导入应拿到全量模型(不再只收 isFree 的 2 个), 条目带倍率。

        对外 id 一律真名(Qwen3.8-Flash), 内部代号在 code 字段;
        签名链由 resolve_model_key 把真名反查回代号。
        """
        from backend.core.channel_auth import qoder
        cn = qoder.models("qodercn")
        intl = qoder.models("qoder")
        assert len(cn) == len(qoder.MODELS_CN)          # 14 个
        assert len(intl) == len(qoder.MODELS_INTL)      # 17 个
        by_id = {m["id"]: m for m in cn}
        # id=真名, code=内部代号, 真名↔代号一一对应
        assert by_id["Qwen3.8-Flash"]["code"] == "qfmodel"
        assert by_id["Kimi-K3"]["code"] == "kmodel_latest"
        # 全部条目都带 rate(倍率来源 pack.js catalog 实测快照)
        assert by_id["Qwen3.8-Flash"]["rate"] == 0      # 免费档
        assert by_id["Kimi-K3"]["rate"] == 1.4
        assert by_id["Qwen3.7-Plus"]["rate"] == 0.04
        assert {m["id"]: m["rate"] for m in qoder.models("qoder")}["Sonus"] == 8
        # 免费档仍可单独取(e2e 探针用)
        assert {m["id"] for m in qoder.free_models("qodercn")} == \
            {"Qwen3.8-Flash", "Qwen3.8-Max"}

    def test_resolve_model_key_bidirectional(self):
        """签名入口: 真名/代号都接受, 未知模型原样透传(上游报错兜底)"""
        from backend.core.channel_auth import qoder
        assert qoder.resolve_model_key("qodercn", "DeepSeek-V4-Pro") == "dmodel"
        assert qoder.resolve_model_key("qodercn", "dmodel") == "dmodel"
        assert qoder.resolve_model_key("qoder", "Sonus") == "smodel"
        assert qoder.resolve_model_key("qodercn", "") == "qfmodel"
        assert qoder.resolve_model_key("qodercn", "not-a-model") == "not-a-model"

    @pytest.mark.asyncio
    async def test_import_fetch_channel_models_qoder_full(self):
        """DSH 导入的 qoder 分支返回全量清单"""
        from backend.core.channel_import import fetch_channel_models

        async with httpx.AsyncClient() as client:
            models = await fetch_channel_models(client, "qodercn", "{}")
        from backend.core.channel_auth import qoder as qoder_mod
        assert len(models) == len(qoder_mod.MODELS_CN)
        assert all("rate" in m for m in models)


class TestCodeartsModelFallback:
    """codearts 目录双源失败时回退 benefit 兜底表, 不给渠道留空模型清单"""

    @pytest.mark.asyncio
    async def test_fetch_models_falls_back_on_dead_creds(self):
        """凭据失效(两个源都 401)时至少返回 benefit 兜底模型"""
        import httpx

        from backend.core.channel_auth import codearts

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error_code": "APIG.0602"})

        creds = {"access_key_id": "ak", "secret_access_key": "sk",
                 "security_token": "st"}
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as client:
            models = await codearts.fetch_models(client, creds)
        ids = {m["id"] for m in models}
        # 完整目录兜底(pack.js DEFAULT_MODELS): 含用户实测可用的盘古与 GLM-5.2
        assert {"glm-5.3-flash", "deepseek-v4.1-flash",
                "openpangu-2.0-flash", "openpangu-2.0-pro", "GLM-5.2"} <= ids
        # benefit 档标记(调用带 maas_type), builtin 模型不标
        by_id = {m["id"]: m for m in models}
        assert by_id["glm-5.3-flash"]["benefit"] is True
        assert by_id["GLM-5.2"]["benefit"] is False
        assert by_id["openpangu-2.0-flash"]["benefit"] is False

    @pytest.mark.asyncio
    async def test_fetch_models_missing_ak_sk(self):
        """凭据缺 AK/SK 也回兜底而非空表"""
        import httpx

        from backend.core.channel_auth import codearts

        async with httpx.AsyncClient() as client:
            models = await codearts.fetch_models(client, {})
        assert len(models) >= len(codearts.FALLBACK_MODELS)
