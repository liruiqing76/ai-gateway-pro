"""渠道级配额冷却后, 同一渠道的其余模型不再白打上游。

opencode 免费车道是一个渠道 9 个模型; 一次调用若首个模型 429 配额,
原来的实现会逐个试完 9 个(4~5 秒 + 9 次真实请求)。本文件锁住"跳过"行为。
"""
from __future__ import annotations

import pytest

from backend.api.proxy import _skip_provider_after_fail
from backend.core.circuit import circuit_breaker


class _P:
    def __init__(self, pid):
        self.id = pid


@pytest.fixture(autouse=True)
def _clean():
    circuit_breaker.reset_all()
    yield
    circuit_breaker.reset_all()


class TestSkipProviderAfterFail:
    def test_skips_when_provider_cooling(self):
        p = _P(4242)
        circuit_breaker.record_provider_rate_limit(p.id, quota=True)
        skip: set[int] = set()
        assert _skip_provider_after_fail(p, skip) is True
        assert p.id in skip

    def test_does_not_skip_when_healthy(self):
        p = _P(4243)
        skip: set[int] = set()
        assert _skip_provider_after_fail(p, skip) is False
        assert skip == set()

    def test_success_clears_and_no_longer_skips(self):
        p = _P(4244)
        circuit_breaker.record_provider_rate_limit(p.id, quota=True)
        circuit_breaker.record_provider_success(p.id)
        skip: set[int] = set()
        assert _skip_provider_after_fail(p, skip) is False

    def test_plain_rate_limit_also_skips(self):
        # 普通限流(非配额)同样共享渠道配额, 也该跳过同渠道其余模型
        p = _P(4245)
        circuit_breaker.record_provider_rate_limit(p.id, quota=False)
        skip: set[int] = set()
        assert _skip_provider_after_fail(p, skip) is True
