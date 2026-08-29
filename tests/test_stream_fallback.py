# ============================================================
# 流式空流失败转移 — _stream_with_fallback / _stream_chunk_has_content
# ============================================================
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from backend.api.proxy import _stream_chunk_has_content, _stream_with_fallback


class _FakeProv:
    def __init__(self, name):
        self.name = name


async def _collect(agen):
    out = b""
    async for chunk in agen:
        out += chunk
    return out


def _mk_gen(chunks: list) -> object:
    """把字节块列表包装成 async 生成器"""
    async def gen():
        for c in chunks:
            yield c
    return gen()


def _mk_pair(prov_name, chunks):
    """size-1 候选: (prov, 'm') + 对应的 forward 行为"""
    return (_FakeProv(prov_name), chunks)


class TestStreamChunkHasContent:
    def test_role_only_is_empty(self):
        assert not _stream_chunk_has_content(
            b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n')

    def test_content_counts(self):
        assert _stream_chunk_has_content(
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')

    def test_reasoning_counts(self):
        assert _stream_chunk_has_content(
            b'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}\n\n')

    def test_tool_calls_counts(self):
        assert _stream_chunk_has_content(
            b'data: {"choices":[{"delta":{"tool_calls":[{"id":"x"}]}}]}\n\n')

    def test_done_and_usage_not_content(self):
        assert not _stream_chunk_has_content(b"data: [DONE]\n\n")
        assert not _stream_chunk_has_content(
            b'data: {"choices":[],"usage":{"total_tokens":1}}\n\n')


class TestStreamWithFallback:
    async def _run(self, primary_chunks, remaining, avail):
        """主候选 + 一系列 (prov, chunks) 候选, 返回 (客户端收到的完整流, 实际调用过的候选名)"""
        called = []

        async def forward_candidate(prov, mdl):
            named = [c for (p, c) in remaining if p.name == prov.name]
            if not named:
                return None, "no candidate"
            called.append(prov.name)
            if named[0] is None:
                return None, "connect error"
            return _mk_gen(named[0]), None

        def candidate_available(prov, mdl):
            return prov.name not in avail

        primary = _mk_gen(primary_chunks)
        out = await _collect(_stream_with_fallback(
            primary, remaining,
            forward_candidate=forward_candidate,
            candidate_available=candidate_available,
        ))
        return out, called

    def test_empty_primary_falls_back_and_gets_content(self):
        """主候选 200 空流(有 [DONE] 无内容) → 应切到下一候选, 客户端拿到内容"""
        C1 = b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        DONE = b"data: [DONE]\n\n"
        C2 = b'data: {"choices":[{"delta":{"content":"real answer"}}]}\n\n'
        primary = [C1, DONE]
        remaining = [(_FakeProv("NVIDIA"), [C2, DONE])]
        out, called = asyncio.run(self._run(primary, remaining, set()))
        assert called == ["NVIDIA"]
        assert b"real answer" in out
        # 主候选的空 [DONE] 不该被转发; 最终只出现一次 [DONE]
        assert out.count(b"[DONE]") == 1

    def test_primary_with_content_keeps_it_no_fallback(self):
        """主候选有内容 → 不切换, 原样透传"""
        C1 = b'data: {"choices":[{"delta":{"content":"good"}}]}\n\n'
        DONE = b"data: [DONE]\n\n"
        remaining = [(_FakeProv("NVIDIA"), None)]  # None → 若被调用会失败
        out, called = asyncio.run(self._run([C1, DONE], remaining, set()))
        assert called == []
        assert b"good" in out
        assert out.count(b"[DONE]") == 1

    def test_cut_stream_before_done_falls_back(self):
        """主候选中途断流(无 [DONE]) → 切到下一候选"""
        C1 = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        C2 = b'data: {"choices":[{"delta":{"content":"fallback full"}}]}\n\n'
        DONE = b"data: [DONE]\n\n"
        remaining = [(_FakeProv("NVIDIA"), [C2, DONE])]
        out, called = asyncio.run(self._run([C1], remaining, set()))
        assert called == ["NVIDIA"]
        assert b"partial" in out and b"fallback full" in out
        assert out.count(b"[DONE]") == 1

    def test_fallback_also_empty_then_next_candidate(self):
        """第一备用也空流 → 继续试下一个, 直到拿到内容"""
        DONE = b"data: [DONE]\n\n"
        EMPTY = b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        C3 = b'data: {"choices":[{"delta":{"content":"third works"}}]}\n\n'
        remaining = [
            (_FakeProv("A"), [EMPTY, DONE]),
            (_FakeProv("B"), [EMPTY, DONE]),
            (_FakeProv("C"), [C3, DONE]),
        ]
        out, called = asyncio.run(self._run([EMPTY, DONE], remaining, set()))
        assert called == ["A", "B", "C"]
        assert b"third works" in out
        assert out.count(b"[DONE]") == 1

    def test_all_fail_yields_final_done_only(self):
        """全部候选都失败 → 空块已转发、只补发一个 [DONE] 收尾(不以错误中断)"""
        DONE = b"data: [DONE]\n\n"
        EMPTY = b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        remaining = [(_FakeProv("A"), None), (_FakeProv("B"), [EMPTY, DONE])]
        out, called = asyncio.run(self._run([EMPTY, DONE], remaining, set()))
        # 主候选中间块会被如实转发, 但 [DONE] 只出现一次(由 wrapper 收尾), 且无任何内容
        assert out.count(b"[DONE]") == 1
        assert b"content" not in out

    def test_circuit_open_candidate_skipped(self):
        """熔断中的候选直接被跳过"""
        DONE = b"data: [DONE]\n\n"
        C2 = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        remaining = [(_FakeProv("A"), [C2, DONE]), (_FakeProv("B"), [C2, DONE])]
        out, called = asyncio.run(self._run([DONE], remaining, {"A"}))
        assert called == ["B"]
        assert b"ok" in out


if __name__ == "__main__":
    import unittest
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)