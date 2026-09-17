# ============================================================
# 挂死型上游超时 — 快速失败 + 熔断指数退避(代理层行为)
# ============================================================
# 背景: 2026-09-03 实测 NVIDIA deepseek-v4-pro-0813 非流式请求
# 卡满 120s 全局超时才 fallback; sensenova-6.8-flash-lite 流式
# 每笔烧满 30s 读超时。修复:
#   1) 非流式也走 30s 读超时(连接后等首字节), 单候选最多拖 30s;
#   2) 超时类失败累计指数退避冷却(90s→1800s), 不再每 60s 重试挂死模型。
# 用本地 socket 上游模拟"连接成功但迟迟不发数据"的挂死场景。
import asyncio
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
import pytest

from backend.core import proxy as proxy_mod
from backend.models import Provider

SSE_BODY = (
    b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
    b"data: [DONE]\n\n"
)
JSON_BODY = (
    b'{"choices":[{"message":{"role":"assistant","content":"ok"},'
    b'"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1}}'
)


class _SlowUpstream:
    """本地慢上游: 连接即回响应头, 拖 delay 秒后才发 body(close-delimited)。"""

    def __init__(self, head: bytes, body: bytes, delay: float):
        self._head, self._body, self._delay = head, body, delay
        self._server = None
        self._tasks: set = set()
        self.port = 0

    async def __aenter__(self) -> "_SlowUpstream":
        async def handle(reader, writer):
            task = asyncio.current_task()
            if task is not None:
                self._tasks.add(task)
            try:
                # 先完整读完请求(按 Content-Length)再回响应: 若在客户端请求
                # 还在路上时直接响应并 close, Windows 会对未读数据发 RST,
                # httpx 得到空错误, 测不到真正的读超时行为。
                header = b""
                while b"\r\n\r\n" not in header:
                    chunk = await reader.read(4096)
                    if not chunk:
                        break
                    header += chunk
                m = re.search(rb"content-length:\s*(\d+)", header, re.I)
                if m:
                    consumed = len(header) - header.index(b"\r\n\r\n") - 4
                    remaining = int(m.group(1)) - max(0, consumed)
                    while remaining > 0:
                        chunk = await reader.read(min(remaining, 65536))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                writer.write(self._head)
                await writer.drain()
                await asyncio.sleep(self._delay)
                writer.write(self._body)
                await writer.drain()
            finally:
                if task is not None:
                    self._tasks.discard(task)
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

        self._server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()
        # 客户端超时后连接被丢弃, 服务端 handler 可能还在 sleep:
        # 必须显式取消, 否则协程被 GC 触发 unraisable 警告(还会连带
        # pytest 的 AST 重写异常导致 exit code 非 0)。
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


@pytest.fixture(autouse=True)
def _reset_breaker(monkeypatch):
    """每个用例独立熔断状态 + 极短读超时(0.15s)加快测试。"""
    monkeypatch.setattr(proxy_mod.settings, "PROXY_NONSTREAM_READ_TIMEOUT", 0.15)
    monkeypatch.setattr(proxy_mod.settings, "PROXY_STREAM_READ_TIMEOUT", 0.15)
    proxy_mod.circuit_breaker.reset_all()


def _provider(port: int) -> Provider:
    return Provider(
        user_id=1,
        name="Mock",
        base_url=f"http://127.0.0.1:{port}",
        api_key_encrypted="key",
        models=["m1"],
    )


def _body(model: str = "m1", stream: bool = False) -> dict:
    return {
        "model": model,
        "stream": stream,
        "messages": [{"role": "user", "content": "hi"}],
    }


class TestNonStreamFastFail:
    """非流式: 挂死上游最多拖读超时, 不再等满 120s; 超时计入退避冷却。"""

    @pytest.mark.asyncio
    async def test_hang_fails_fast_with_timeout_escalation(self):
        """挂死上游 → ~0.15s 内失败(而非 120s), 熔断 90s 冷却"""
        head = ("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(JSON_BODY)}\r\n\r\n").encode()
        async with _SlowUpstream(head, JSON_BODY, delay=5.0) as up:
            client = httpx.AsyncClient()
            try:
                t0 = time.monotonic()
                resp, err = await proxy_mod.forward_non_stream(
                    client, _provider(up.port), "key", "m1", _body())
                elapsed = time.monotonic() - t0
            finally:
                await client.aclose()
        assert resp is None
        assert str(err).startswith("upstream timeout")
        # 快速失败: 读超时 0.15s + 开销, 远小于修复前的 120s
        assert elapsed < 5.0
        # 超时即打开熔断(90s 退避, 不是普通失败的 60s)
        key = "Mock||m1"
        assert proxy_mod.circuit_breaker.is_open(key)
        remaining = proxy_mod.circuit_breaker.cooldown_seconds(key)
        assert 80 <= remaining <= 95

    @pytest.mark.asyncio
    async def test_repeated_timeouts_escalate_cooldown(self):
        """连续超时 → 冷却翻倍(第二次 ~180s)"""
        head = ("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(JSON_BODY)}\r\n\r\n").encode()
        async with _SlowUpstream(head, JSON_BODY, delay=5.0) as up:
            client = httpx.AsyncClient()
            try:
                for _ in range(2):
                    resp, err = await proxy_mod.forward_non_stream(
                        client, _provider(up.port), "key", "m1", _body())
                    assert resp is None
            finally:
                await client.aclose()
        key = "Mock||m1"
        remaining = proxy_mod.circuit_breaker.cooldown_seconds(key)
        assert 170 <= remaining <= 185

    @pytest.mark.asyncio
    async def test_healthy_upstream_still_succeeds(self):
        """正常上游不受影响: 秒回成功, 不触发熔断"""
        head = ("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(JSON_BODY)}\r\n\r\n").encode()
        async with _SlowUpstream(head, JSON_BODY, delay=0.0) as up:
            client = httpx.AsyncClient()
            try:
                t0 = time.monotonic()
                resp, err = await proxy_mod.forward_non_stream(
                    client, _provider(up.port), "key", "m1", _body())
                elapsed = time.monotonic() - t0
            finally:
                await client.aclose()
        assert err is None
        assert resp["choices"][0]["message"]["content"] == "ok"
        assert resp["model"] == "Mock · m1"
        assert elapsed < 5.0
        assert not proxy_mod.circuit_breaker.is_open("Mock||m1")


class TestStreamTimeoutEscalation:
    """流式: gen() 读超时计入退避冷却(而不是被普通 60s 熔断覆盖)。"""

    @pytest.mark.asyncio
    async def test_read_timeout_opens_90s_backoff(self):
        """响应头到了但 body 迟迟不来 → 30s 读超时判挂死, 冷却 90s"""
        head = b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n"
        async with _SlowUpstream(head, SSE_BODY, delay=5.0) as up:
            client = httpx.AsyncClient()
            try:
                gen, err = await proxy_mod.forward_stream(
                    client, _provider(up.port), "key", "m1", _body(stream=True))
                assert gen is not None, err
                chunks = []
                async for c in gen:
                    chunks.append(c)
            finally:
                await client.aclose()
        # 挂死流: 无任何内容产出
        assert b"hi" not in b"".join(chunks)
        key = "Mock||m1"
        assert proxy_mod.circuit_breaker.is_open(key)
        remaining = proxy_mod.circuit_breaker.cooldown_seconds(key)
        assert 80 <= remaining <= 95

    @pytest.mark.asyncio
    async def test_normal_stream_unaffected_and_resets(self):
        """正常流式: 内容正常产出, 不触发熔断"""
        head = b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n"
        async with _SlowUpstream(head, SSE_BODY, delay=0.0) as up:
            client = httpx.AsyncClient()
            try:
                gen, err = await proxy_mod.forward_stream(
                    client, _provider(up.port), "key", "m1", _body(stream=True))
                assert gen is not None, err
                chunks = b"".join([c async for c in gen])
            finally:
                await client.aclose()
        assert b'"hi"' in chunks
        assert b"[DONE]" in chunks
        assert not proxy_mod.circuit_breaker.is_open("Mock||m1")