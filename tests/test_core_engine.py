# ============================================================
# 熔断器 + 质量调度测试
# ============================================================
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from backend.core.circuit import CircuitBreaker
from backend.core.scheduler import QualityTracker


class TestCircuitBreaker:
    def test_allows_initial_requests(self):
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=10)
        assert not cb.is_open("provider||model")

    def test_opens_after_threshold_fails(self):
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=10)
        key = "p1||m1"
        assert not cb.record_fail(key)  # 1/3
        assert not cb.record_fail(key)  # 2/3
        assert not cb.is_open(key)
        assert cb.record_fail(key)      # 3/3 → 开断
        assert cb.is_open(key)

    def test_success_resets_fail_count(self):
        cb = CircuitBreaker(fail_threshold=2, recovery_seconds=10)
        key = "p1||m1"
        cb.record_fail(key)
        cb.record_success(key)
        # 失败计数已重置,需要再失败 2 次才开
        cb.record_fail(key)
        assert not cb.is_open(key)
        cb.record_fail(key)
        assert cb.is_open(key)

    def test_recovers_after_timeout(self):
        cb = CircuitBreaker(fail_threshold=1, recovery_seconds=0.1)
        key = "p1||m1"
        cb.record_fail(key)
        assert cb.is_open(key)
        time.sleep(0.2)
        assert not cb.is_open(key)  # 恢复

    def test_force_open(self):
        cb = CircuitBreaker(fail_threshold=10, recovery_seconds=10)
        key = "p1||m1"
        cb.force_open(key, duration=5)
        assert cb.is_open(key)

    def test_reset(self):
        cb = CircuitBreaker(fail_threshold=1, recovery_seconds=10)
        key = "p1||m1"
        cb.record_fail(key)
        assert cb.is_open(key)
        cb.reset(key)
        assert not cb.is_open(key)


class TestQualityTracker:
    def test_initial_score_zero(self):
        qt = QualityTracker(window=20)
        assert qt.get_score("p1||m1") == 0.0

    def test_all_ok_high_score(self):
        qt = QualityTracker(window=20)
        for _ in range(20):
            qt.record("p1||m1", "ok", 100)
        score = qt.get_score("p1||m1")
        assert 0.5 <= score <= 1.0

    def test_all_fail_low_score(self):
        qt = QualityTracker(window=20)
        for _ in range(20):
            qt.record("p1||m1", "fail", 100)
        score = qt.get_score("p1||m1")
        assert score <= 0.0

    def test_ok_scores_higher_than_fail(self):
        qt = QualityTracker(window=20)
        for _ in range(20):
            qt.record("good||m", "ok", 100)
            qt.record("bad||m", "fail", 100)
        good = qt.get_score("good||m")
        bad = qt.get_score("bad||m")
        assert good > bad

    def test_error_penalty_heavier_than_fail(self):
        qt = QualityTracker(window=20)
        for _ in range(20):
            qt.record("error_m||m", "error", 100)
        error_score = qt.get_score("error_m||m")
        for _ in range(20):
            qt.record("fail_m||m", "fail", 100)
        fail_score = qt.get_score("fail_m||m")
        assert error_score < fail_score

    def test_latency_tracked(self):
        qt = QualityTracker(window=20)
        qt.record("p1||m1", "ok", 500)
        qt.record("p1||m1", "ok", 300)
        assert qt.get_avg_latency("p1||m1") == 400.0

    def test_stats(self):
        qt = QualityTracker(window=20)
        qt.record("p1||m1", "ok", 100)
        qt.record("p1||m1", "fail", 100)
        stats = qt.get_stats("p1||m1")
        assert stats["ok"] == 1
        assert stats["fail"] == 1
        assert stats["total"] == 2
        assert "score" in stats

    def test_confidence_weighting(self):
        """请求数少时分数被置信度压低"""
        qt = QualityTracker(window=20)
        qt.record("one||m", "ok", 100)  # 只有 1 次成功
        one_score = qt.get_score("one||m")

        qt2 = QualityTracker(window=20)
        for _ in range(20):
            qt2.record("many||m", "ok", 100)
        many_score = qt2.get_score("many||m")

        assert one_score < many_score