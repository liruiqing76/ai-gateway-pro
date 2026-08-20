# ============================================================
# 转发引擎错误分支测试 (backend/core/proxy.py)
# 覆盖: 429限流 / 上游4xx / 非JSON / 网络异常 / 流式首chunk验证 / 重试
# ============================================================
import sys, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from backend.core.circuit import circuit_breaker
from backend.core.scheduler import quality_tracker


@pytest.fixture(autouse=True)
def clean_state():
    """每个测试后清空熔断与质量分,避免串扰"""
    yield
    circuit_breaker._states.clear()
    quality_tracker.reset_all()


class FakeProvider:
    def __init__(self, name="TestUp", base_url="https://up.example/v1"):
        self.id = 1
        self.name = name
        self.base_url = base_url


def make_httpx_response(status=200, json_data=None, text="", async_lines=None):
    """构造一个假的 httpx.Response

    aiter_lines 必须是真正的异步生成器(async for 需要 __aiter__),
    不能用 AsyncMock(list) (async for 无法消费普通 list coroutine)
    """
    if async_lines is None:
        async_lines = []

    async def _iter_lines():
        for line in async_lines:
            yield line

    resp = MagicMock()
    resp.status_code = status
    resp.text = text or (json.dumps(json_data) if json_data is not None else "")
    resp.json = MagicMock(return_value=json_data if json_data is not None else {})
    resp.aiter_lines = _iter_lines          # async generator 函数
    resp.aread = AsyncMock(return_value=resp.text.encode())
    resp.aclose = AsyncMock()
    return resp


class TestForwardNonStream:
    """非流式转发的错误分支"""

    async def _client_with(self, status=200, json_data=None, text="", exc=None):
        """构造 mock http_client, 可抛异常"""
        client = MagicMock()
        if exc is not None:
            client.post = AsyncMock(side_effect=exc)
        else:
            client.post = AsyncMock(return_value=make_httpx_response(status, json_data, text))
        return client

    @pytest.mark.asyncio
    async def test_success_records_stats(self):
        from backend.core.proxy import forward_non_stream

        client = await self._client_with(200, {"choices": [], "usage": {}})
        provider = FakeProvider()
        resp, err = await forward_non_stream(client, provider, "key", "model-x", {"model": "model-x"})

        assert err is None
        assert resp["model"] == "TestUp · model-x"  # 标记渠道

    @pytest.mark.asyncio
    async def test_429_records_rate_limit_and_backoff(self):
        from backend.core.proxy import forward_non_stream

        client = await self._client_with(429, text="rate limited")
        provider = FakeProvider()
        resp, err = await forward_non_stream(client, provider, "key", "model-x", {"model": "model-x"})

        assert resp is None
        assert err == "upstream 429"
        # 限流应触发熔断 + 冷却
        key = "TestUp||model-x"
        st = circuit_breaker._states[key]
        assert st.rate_limit_count == 1
        assert circuit_breaker.is_open(key)

    @pytest.mark.asyncio
    async def test_4xx_marks_fail(self):
        from backend.core.proxy import forward_non_stream

        client = await self._client_with(401, text="unauthorized")
        provider = FakeProvider()
        resp, err = await forward_non_stream(client, provider, "key", "model-x", {"model": "model-x"})

        assert resp is None
        assert err == "upstream 401"
        assert quality_tracker.get_score("TestUp||model-x") < 0

    @pytest.mark.asyncio
    async def test_non_json_body(self):
        from backend.core.proxy import forward_non_stream

        client = await self._client_with(200, text="<html>not json</html>")
        # 覆盖 json() 抛异常
        client.post.return_value.json = MagicMock(side_effect=json.JSONDecodeError("x", "", 0))
        provider = FakeProvider()
        resp, err = await forward_non_stream(client, provider, "key", "model-x", {"model": "model-x"})

        assert resp is None
        assert err == "upstream non-json"

    @pytest.mark.asyncio
    async def test_connect_timeout(self):
        from backend.core.proxy import forward_non_stream
        import httpx

        client = await self._client_with(exc=httpx.ConnectTimeout("timeout"))
        provider = FakeProvider()
        resp, err = await forward_non_stream(client, provider, "key", "model-x", {"model": "model-x"})

        assert resp is None
        assert "timeout" in err.lower()
        assert quality_tracker.get_score("TestUp||model-x") < 0

    @pytest.mark.asyncio
    async def test_unexpected_exception(self):
        from backend.core.proxy import forward_non_stream

        client = await self._client_with(exc=RuntimeError("weird"))
        provider = FakeProvider()
        resp, err = await forward_non_stream(client, provider, "key", "model-x", {"model": "model-x"})

        assert resp is None
        assert "weird" in err


class TestForwardStream:
    """流式转发的错误分支与首chunk验证"""

    def _make_client(self, status=200, lines=None, send_exc=None):
        client = MagicMock()
        if send_exc is not None:
            client.send = AsyncMock(side_effect=send_exc)
        else:
            resp = make_httpx_response(status, async_lines=lines or [])
            client.send = AsyncMock(return_value=resp)
        client.build_request = MagicMock(return_value=MagicMock())
        return client

    @pytest.mark.asyncio
    async def test_success_stream(self):
        from backend.core.proxy import forward_stream

        lines = ["data: " + json.dumps({"choices": [{"delta": {"content": "hi"}}]}), "data: [DONE]", ""]
        client = self._make_client(200, lines)
        provider = FakeProvider()
        gen, err = await forward_stream(client, provider, "key", "model-x", {"model": "model-x"})

        assert err is None
        assert gen is not None
        collected = b"".join([c async for c in gen]).decode()
        assert "hi" in collected
        assert "[DONE]" in collected

    @pytest.mark.asyncio
    async def test_connect_error_fails(self):
        from backend.core.proxy import forward_stream
        import httpx

        client = self._make_client(send_exc=httpx.ConnectError("refused"))
        provider = FakeProvider()
        gen, err = await forward_stream(client, provider, "key", "model-x", {"model": "model-x"})

        assert gen is None
        assert "refused" in err
        # 失败计数 +1(阈值 3 次才熔断, 但质量分应下降)
        assert circuit_breaker._states["TestUp||model-x"].fails == 1
        assert quality_tracker.get_score("TestUp||model-x") < 0

    @pytest.mark.asyncio
    async def test_non_200_status(self):
        from backend.core.proxy import forward_stream

        client = self._make_client(status=401, lines=[])
        provider = FakeProvider()
        gen, err = await forward_stream(client, provider, "key", "model-x", {"model": "model-x"})

        assert gen is None
        assert "401" in err
        assert circuit_breaker._states["TestUp||model-x"].fails == 1

    @pytest.mark.asyncio
    async def test_first_chunk_not_sse_format(self):
        """上游 200 但返回非 SSE 内容 → 视为失败(触发故障转移)"""
        from backend.core.proxy import forward_stream

        # 第一个非空行不是 data: 前缀
        client = self._make_client(200, lines=["hello plain text"])
        provider = FakeProvider()
        gen, err = await forward_stream(client, provider, "key", "model-x", {"model": "model-x"})

        assert gen is None
        assert "invalid stream format" in err
        # 失败计数 +1(阈值 3 次才熔断, 但质量分应下降)
        assert circuit_breaker._states["TestUp||model-x"].fails == 1

    @pytest.mark.asyncio
    async def test_empty_body(self):
        """上游 200 但立刻结束(无内容) → 视为失败"""
        from backend.core.proxy import forward_stream

        client = self._make_client(200, lines=[""])
        provider = FakeProvider()
        gen, err = await forward_stream(client, provider, "key", "model-x", {"model": "model-x"})

        assert gen is None
        assert err  # 有错误信息(空流)

    @pytest.mark.asyncio
    async def test_stream_marks_success_on_valid_first_chunk(self):
        """首chunk 合法即记录成功(恢复熔断)"""
        from backend.core.proxy import forward_stream

        # 先人为熔断
        circuit_breaker.force_open("TestUp||model-x")
        assert circuit_breaker.is_open("TestUp||model-x")

        lines = ["data: " + json.dumps({"choices": [{"delta": {"content": "ok"}}]}), "data: [DONE]"]
        client = self._make_client(200, lines)
        provider = FakeProvider()
        gen, err = await forward_stream(client, provider, "key", "model-x", {"model": "model-x"})

        assert err is None
        assert gen is not None
        # 成功后熔断应恢复
        assert not circuit_breaker.is_open("TestUp||model-x")


class TestPickAvailableModelsBranches:
    """候选选择分支: 具体模型匹配 / 前缀匹配 / 熔断兜底 / 别名"""

    def test_exact_model_match(self):
        from backend.core.proxy import pick_available_models
        from backend.models import Provider

        providers = [Provider(id=1, name="UpA", models=["m1", "m2"], disabled_models="[]", enabled=True)]
        result = pick_available_models(providers, "m1", force=True)
        assert len(result) == 1
        assert result[0][1] == "m1"

    def test_prefixed_model_match(self):
        from backend.core.proxy import pick_available_models
        from backend.models import Provider

        providers = [Provider(id=1, name="UpA", models=["m1"], disabled_models="[]", enabled=True)]
        result = pick_available_models(providers, "UpA-m1", force=True)
        assert len(result) == 1 and result[0][1] == "m1"

    def test_disabled_models_excluded(self):
        from backend.core.proxy import pick_available_models
        from backend.models import Provider

        providers = [Provider(id=1, name="UpA", models=["m1", "m2"], disabled_models='["m2"]', enabled=True)]
        result = pick_available_models(providers, "m1", force=True)
        assert [m for _, m in result] == ["m1"]
        # 组内也不含禁用模型
        result = pick_available_models(providers, "UpA", {"UpA": ["m1", "m2"]}, force=True)
        assert [m for _, m in result] == ["m1"]

    def test_open_circuit_skipped_until_all_open(self):
        """熔断处理: 有健康候选时跳过熔断模型; 全部熔断时才兜底返回"""
        from backend.core.proxy import pick_available_models
        from backend.models import Provider

        providers = [
            Provider(id=1, name="UpA", models=["m1", "m2"], disabled_models="[]", enabled=True),
            Provider(id=2, name="UpB", models=["m3"], disabled_models="[]", enabled=True),
        ]
        routers = {"grp": ["m1", "m2", "m3"]}

        # 1) 仅 m1 熔断: 其余健康 → m1 被跳过
        circuit_breaker.force_open("UpA||m1")
        result = pick_available_models(providers, "grp", routers, strategy="quality")
        models = [m for _, m in result]
        assert set(models) == {"m2", "m3"}

        # 2) 全部熔断: 无健康候选 → 兜底返回全部(熔断中的仍在列)
        circuit_breaker.force_open("UpA||m2")
        circuit_breaker.force_open("UpB||m3")
        result = pick_available_models(providers, "grp", routers, strategy="quality")
        models = [m for _, m in result]
        assert set(models) == {"m1", "m2", "m3"}

    def test_no_match_returns_empty(self):
        from backend.core.proxy import pick_available_models
        from backend.models import Provider

        providers = [Provider(id=1, name="UpA", models=["m1"], disabled_models="[]", enabled=True)]
        assert pick_available_models(providers, "nonexistent", force=True) == []

    def test_alias_resolution(self):
        from backend.core.proxy import pick_available_models
        from backend.models import Provider

        providers = [Provider(id=1, name="UpA", models=["deekseek-v3"], disabled_models="[]", enabled=True)]
        routers = {"high": ["UpA-deekseek-v3"]}
        # 无别名: 不命中
        assert pick_available_models(providers, "gpt-4", routers, force=True) == []
        # 有别名: 命中
        result = pick_available_models(providers, "gpt-4", routers, force=True, user_aliases={"gpt-4": "high"})
        assert [m for _, m in result] == ["deekseek-v3"]