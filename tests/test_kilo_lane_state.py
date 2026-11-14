"""Kilo 车道状态: 429 免费推广额度用尽 ≠ 车道关闭。

额度会随注册/时间重置, 所以同步时应把这类模型保留为"可用(等重置)",
lane_state 记 open —— 否则界面会显示红色"车道关闭", 把用户引向错误结论
(2026-10-24 实测: 17 个 isFree 模型全部 429 PROMOTION_MODEL_LIMIT_REACHED,
但这不是模型死掉, 也不是渠道不可达)。
"""
import pytest

from backend.core.kilo_sources import _is_kilo_quota, _KILO_QUOTA_HINTS

# 实测原文(截断)
KILO_429 = ('429 {"error":{"code":"PROMOTION_MODEL_LIMIT_REACHED","message":'
            '"Sign up for free to continue and explore 500 other models."},'
            '"error_type":"rate_limit_exceeded"}')


class TestKiloQuotaClassification:
    def test_promotion_limit_is_quota(self):
        assert _is_kilo_quota(KILO_429) is True

    def test_rate_limit_exceeded_is_quota(self):
        assert _is_kilo_quota('429 {"error_type":"rate_limit_exceeded"}') is True

    def test_non_429_is_not_quota(self):
        """403/404/500 是确定性拒绝, 不能算"额度用尽会重置"。"""
        assert _is_kilo_quota('403 {"error":"forbidden"}') is False
        assert _is_kilo_quota('404 model not found') is False
        assert _is_kilo_quota('500 internal error') is False

    def test_429_without_quota_hint_is_not_quota(self):
        """429 但文案不是额度类(纯并发限流) → 交给原有 bad 处理。"""
        assert _is_kilo_quota('429 slow down') is False

    def test_err_and_empty(self):
        assert _is_kilo_quota('ERR connect timeout') is False
        assert _is_kilo_quota('') is False
        assert _is_kilo_quota(None) is False

    def test_hints_are_lowercase(self):
        """提示词必须全小写, 否则 lower() 后匹配不到。"""
        for h in _KILO_QUOTA_HINTS:
            assert h == h.lower()
