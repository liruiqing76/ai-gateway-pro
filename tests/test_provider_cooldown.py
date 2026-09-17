# ============================================================
# 渠道级软冷却 — 429/配额 → 降权, 成功 → 立即恢复
# ============================================================
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from backend.core.circuit import CircuitBreaker


class TestProviderCooldown:
    """渠道级冷却: 只降权(0.05), 绝不硬禁用渠道; 成功调用即清零"""

    def test_first_429_cools_30s(self):
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb.record_provider_rate_limit(1)
        assert cb.provider_in_cooldown(1)
        assert 25 <= cb.provider_cooldown_seconds(1) <= 35
        assert cb.provider_weight(1) == 0.05

    def test_quota_exhaust_cools_120s(self):
        """配额/余额耗尽走长效冷却"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb.record_provider_rate_limit(2, quota=True)
        assert 110 <= cb.provider_cooldown_seconds(2) <= 130

    def test_consecutive_429_doubles(self):
        """连续 429 指数退避: 第 2 次 60s, 第 3 次 120s(上限 300s)"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb.record_provider_rate_limit(3)  # 第 1 次: 30s(窗口见 test_first)
        cb.record_provider_rate_limit(3)  # 第 2 次: 60s
        assert 55 <= cb.provider_cooldown_seconds(3) <= 65
        cb.record_provider_rate_limit(3)  # 第 3 次: 120s
        assert 115 <= cb.provider_cooldown_seconds(3) <= 125

    def test_success_clears_immediately(self):
        """任意成功调用 → 冷却清除, 权重恢复 1.0"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb.record_provider_rate_limit(4)
        assert cb.provider_in_cooldown(4)
        cb.record_provider_success(4)
        assert not cb.provider_in_cooldown(4)
        assert cb.provider_weight(4) == 1.0
        assert cb.provider_cooldown_seconds(4) == 0

    def test_success_without_cooldown_is_noop(self):
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb.record_provider_success(5)  # 不应抛错
        assert cb.provider_weight(5) == 1.0

    def test_multiple_providers_independent(self):
        """渠道冷却互不影响"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb.record_provider_rate_limit(6)
        assert cb.provider_in_cooldown(6)
        assert not cb.provider_in_cooldown(7)
        assert cb.provider_weight(7) == 1.0

    def test_cooldowns_lists_only_active(self):
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb.record_provider_rate_limit(8)
        cooldowns = cb.provider_cooldowns()
        assert 8 in cooldowns
        assert 25 <= cooldowns[8] <= 35
        cb.record_provider_success(8)
        assert cb.provider_cooldowns() == {}

    def test_expired_cooldown_restores_weight(self):
        """冷却到期后权重自动回到 1.0(时间驱动, 无需显式清除)"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb.record_provider_rate_limit(9)
        # 直接把冷却时间拨回过去, 模拟时间流逝
        cb._provider_cooldown[9] = time.time() - 1
        assert not cb.provider_in_cooldown(9)
        assert cb.provider_weight(9) == 1.0

    def test_reset_all_clears_provider_cooldown(self):
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb.record_provider_rate_limit(10)
        cb.reset_all()
        assert not cb.provider_in_cooldown(10)
        assert cb.provider_cooldowns() == {}