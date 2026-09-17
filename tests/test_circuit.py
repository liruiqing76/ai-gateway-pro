# ============================================================
# 429 限流冷却 — 指数退避测试
# ============================================================
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from backend.core.circuit import CircuitBreaker


class TestRateLimitCooldown:
    """429 限流冷却: 每次连续 429 冷却时间翻倍, 最多 300s"""

    def test_first_429_short_cooldown(self):
        """第一次 429 → 冷却 30s"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb.record_rate_limit("NVIDIA||model-a")
        assert cb.is_open("NVIDIA||model-a")
        # 冷却时间应 < 60s(默认), 约 30s
        st = cb._states["NVIDIA||model-a"]
        remaining = st.open_until - time.time()
        assert 25 <= remaining <= 35

    def test_second_429_doubles_cooldown(self):
        """第二次连续 429 → 冷却 60s"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb.record_rate_limit("NVIDIA||model-a")
        cb.record_rate_limit("NVIDIA||model-a")
        st = cb._states["NVIDIA||model-a"]
        remaining = st.open_until - time.time()
        assert 55 <= remaining <= 65

    def test_third_429_quadruples(self):
        """第三次连续 429 → 冷却 120s"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        for _ in range(3):
            cb.record_rate_limit("NVIDIA||model-a")
        st = cb._states["NVIDIA||model-a"]
        remaining = st.open_until - time.time()
        assert 115 <= remaining <= 125

    def test_max_cooldown_300s(self):
        """冷却上限 300s"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        for _ in range(10):
            cb.record_rate_limit("NVIDIA||model-a")
        st = cb._states["NVIDIA||model-a"]
        remaining = st.open_until - time.time()
        assert remaining <= 301  # 不超过上限

    def test_success_resets_rate_limit_counter(self):
        """成功后重置 429 计数"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb.record_rate_limit("NVIDIA||model-a")
        cb.record_rate_limit("NVIDIA||model-a")
        # 成功
        cb.record_success("NVIDIA||model-a")
        st = cb._states["NVIDIA||model-a"]
        assert st.rate_limit_count == 0
        # 下次 429 应该又是 30s
        cb.record_rate_limit("NVIDIA||model-a")
        remaining = st.open_until - time.time()
        assert 25 <= remaining <= 35


class TestContextHandoff:
    """上下文交接: relay/review 换模型时注入接力提示"""

    def test_relay_injects_handoff_message(self):
        """relay 第二级 prompt 应包含交接提示"""
        import asyncio
        from backend.core.orchestrate import run_relay

        captured = []

        async def fake_forward(stage, body):
            captured.append(body["messages"])
            return {"choices": [{"message": {"content": f"[{stage}]"}}], "usage": {}}

        body = {"messages": [{"role": "user", "content": "写文案"}]}
        asyncio.run(run_relay(["A", "B"], body, fake_forward))

        # 第二级消息应包含交接提示
        second_msgs = captured[1]
        second_prompt = second_msgs[-1]["content"]
        assert "上一级" in second_prompt or "上一步" in second_prompt
        assert "[A]" in second_prompt

    def test_review_injects_handoff_message(self):
        """review 审阅 prompt 应包含交接提示"""
        import asyncio
        from backend.core.orchestrate import run_review

        captured = []

        async def fake_forward(stage, body):
            captured.append(body["messages"])
            if stage == "主":
                return {"choices": [{"message": {"content": "[初稿]"}}], "usage": {}}
            return {"choices": [{"message": {"content": "[修订]"}}], "usage": {}}

        body = {"messages": [{"role": "user", "content": "写文案"}]}
        asyncio.run(run_review(["主", "审"], body, fake_forward))

        # 审阅阶段应包含交接提示
        review_prompt = captured[1][-1]["content"]
        assert "初稿" in review_prompt
        assert "审阅" in review_prompt or "修订" in review_prompt
        assert "写文案" in review_prompt


class TestQuotaExhaustedCooldown:
    """配额/余额耗尽 429 → 长效冷却(120s 起, 上限 30min)"""

    def test_first_quota_long_cooldown(self):
        """第一次配额耗尽 → 冷却 120s"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb.record_quota_exhausted("SenseNova||glm-5.2")
        assert cb.is_open("SenseNova||glm-5.2")
        st = cb._states["SenseNova||glm-5.2"]
        remaining = st.open_until - time.time()
        assert 115 <= remaining <= 125

    def test_quota_escalates_to_30min_max(self):
        """连续配额耗尽 → 冷却递增到上限 1800s"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        for _ in range(6):
            cb.record_quota_exhausted("SenseNova||glm-5.2")
        st = cb._states["SenseNova||glm-5.2"]
        remaining = st.open_until - time.time()
        assert 1790 <= remaining <= 1801

    def test_quota_much_longer_than_plain_rate_limit(self):
        """配额冷却(首次 120s)远长于一般限流(首次 30s)"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb.record_quota_exhausted("k")
        q_remaining = cb._states["k"].open_until - time.time()
        cb2 = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb2.record_rate_limit("k")
        r_remaining = cb2._states["k"].open_until - time.time()
        assert q_remaining > r_remaining + 80

    def test_success_resets_quota_cooldown(self):
        """成功后配额计数重置"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb.record_quota_exhausted("k")
        cb.record_quota_exhausted("k")
        cb.record_success("k")
        st = cb._states["k"]
        assert st.rate_limit_count == 0
        assert not cb.is_open("k")
        # 下次配额耗尽又从 120s 开始
        cb.record_quota_exhausted("k")
        remaining = st.open_until - time.time()
        assert 115 <= remaining <= 125


class TestDeadModelAutoCooldown:
    """上游 404(模型下架) → 连续 3 次判死 → 24h 长冷却(而非 60s 短冷却)"""

    def test_first_404_suspect_not_open_long(self):
        """1 次 404: 不算死模型, 不触发 24h 长冷却(此时只有普通 record_fail 的 60s)"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        assert cb.record_not_found("NVIDIA||kimi-k2.6") is False
        assert not cb.is_open("NVIDIA||kimi-k2.6")
        assert cb.is_dead_suspect("NVIDIA||kimi-k2.6")

    def test_three_404_marks_dead_long_cooldown(self):
        """连续 3 次 404 → 判定死模型, 24h 长冷却, 仅第 3 次返回 True"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        assert cb.record_not_found("NVIDIA||kimi-k2.6") is False
        assert cb.record_not_found("NVIDIA||kimi-k2.6") is False
        assert cb.record_not_found("NVIDIA||kimi-k2.6") is True
        assert cb.is_open("NVIDIA||kimi-k2.6")
        st = cb._states["NVIDIA||kimi-k2.6"]
        remaining = st.open_until - time.time()
        # 24h 长冷却(远大于普通 60s 熔断)
        assert remaining > 23 * 3600
        # 判死后 404 计数清零, 冷却期内再记不会重复触发
        assert not cb.is_dead_suspect("NVIDIA||kimi-k2.6")
        assert cb.record_not_found("NVIDIA||kimi-k2.6") is False

    def test_404_interrupted_by_success_resets(self):
        """2 次 404 后成功 → 计数清零, 不会判死"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb.record_not_found("NVIDIA||kimi-k2.6")
        cb.record_not_found("NVIDIA||kimi-k2.6")
        cb.record_success("NVIDIA||kimi-k2.6")
        assert not cb.is_dead_suspect("NVIDIA||kimi-k2.6")
        assert cb.record_not_found("NVIDIA||kimi-k2.6") is False
        assert cb.record_not_found("NVIDIA||kimi-k2.6") is False
        # 3 次(重置后重新累计)后才判死
        assert cb.record_not_found("NVIDIA||kimi-k2.6") is True

    def test_dead_cooldown_expires(self):
        """长冷却到期后自动恢复(模型若恢复上线可重新参与路由)"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb.record_not_found("k")
        cb.record_not_found("k")
        cb.record_not_found("k")
        st = cb._states["k"]
        st.open_until = time.time() - 1  # 模拟 24h 已过
        assert not cb.is_open("k")


class TestTimeoutCooldown:
    """挂死型超时冷却: 比普通失败更长的指数退避(90s 起, 上限 1800s)。

    背景: 超时的模型(连接后长时间无数据)60s 短熔断恢复后又被路由,
    每笔请求白烧一次超时(实测非流式 120s / 流式 30s), 拖垮整条链路。
    """

    def test_first_timeout_90s(self):
        """第一次超时 → 直接打开熔断, 冷却 90s(远大于普通失败 60s)"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb.record_timeout_fail("NVIDIA||model-a")
        assert cb.is_open("NVIDIA||model-a")
        st = cb._states["NVIDIA||model-a"]
        remaining = st.open_until - time.time()
        assert 85 <= remaining <= 95

    def test_second_timeout_doubles(self):
        """第二次连续超时 → 冷却翻倍到 180s"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb.record_timeout_fail("NVIDIA||model-a")
        cb.record_timeout_fail("NVIDIA||model-a")
        st = cb._states["NVIDIA||model-a"]
        remaining = st.open_until - time.time()
        assert 175 <= remaining <= 185

    def test_fourth_timeout_caps_at_1800(self):
        """连续超时冷却指数递增, 上限 1800s(30min)"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        for _ in range(10):
            cb.record_timeout_fail("NVIDIA||model-a")
        st = cb._states["NVIDIA||model-a"]
        remaining = st.open_until - time.time()
        assert remaining <= 1801

    def test_success_resets_timeout_escalation(self):
        """成功后超时计数清零, 熔断恢复(模型恢复后重新参与路由)"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb.record_timeout_fail("NVIDIA||model-a")
        cb.record_timeout_fail("NVIDIA||model-a")
        cb.record_success("NVIDIA||model-a")
        assert not cb.is_open("NVIDIA||model-a")
        assert cb._states["NVIDIA||model-a"].timeout_count == 0

    def test_timeout_opens_immediately_even_below_fail_threshold(self):
        """单次超时即打开(不等连续失败阈值)"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb.record_timeout_fail("NVIDIA||model-a")
        assert cb.is_open("NVIDIA||model-a")

    def test_429_and_timeout_backoff_independent(self):
        """限流退避与超时退避各自独立计数, 互不干扰"""
        cb = CircuitBreaker(fail_threshold=3, recovery_seconds=60)
        cb.record_rate_limit("NVIDIA||model-a")       # 30s
        cb.record_timeout_fail("NVIDIA||model-a")     # 90s → 更严重者覆盖
        st = cb._states["NVIDIA||model-a"]
        remaining = st.open_until - time.time()
        assert 85 <= remaining <= 95
        assert st.rate_limit_count == 1
        assert st.timeout_count == 1