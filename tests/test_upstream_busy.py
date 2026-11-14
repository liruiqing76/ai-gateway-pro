# -*- coding: utf-8 -*-
"""上游"暂时忙"分类测试: 不能误判成死模型。"""
from backend.core.proxy import _is_dead_model_400, _is_upstream_busy, _busy_reason


class TestUpstreamBusy:
    def test_codearts_concurrent_session_limit(self):
        text = ('{"error_code":"TM.00001041","error_msg":"并发会话数已达上限(3个)，'
                '请关闭部分会话后重试。"}')
        assert _is_upstream_busy(400, text) is True
        assert _is_dead_model_400(text) is False
        assert "并发" in _busy_reason(text)

    def test_codearts_benefit_exhausted_403(self):
        text = ('{"error_code":"TM.000010051","error_msg":"当前所选模型的使用权益已耗尽，'
                '可选择如下模型：GLM-5.2"}')
        assert _is_upstream_busy(403, text) is True
        assert _is_dead_model_400(text) is False
        assert "权益" in _busy_reason(text)

    def test_benefit_not_found(self):
        text = '{"error_code":"InferHub.4004.200","error_msg":"benefit not found"}'
        assert _is_upstream_busy(200, text) is False   # 200 不算
        assert _is_upstream_busy(400, text) is True
        assert "benefit" in _busy_reason(text).lower() or "未开通" in _busy_reason(text)

    def test_qoder_queued(self):
        text = '{"code":"10605","message":"{\\"isQueued\\":true}"}'
        assert _is_upstream_busy(400, text) is True
        assert "排队" in _busy_reason(text)

    def test_billing_daily_exceeded(self):
        text = "qoder upstream error: Billing daily count exceeded"
        assert _is_upstream_busy(429, text) is True
        assert "权益" in _busy_reason(text)

    def test_dead_model_not_busy(self):
        text = '{"error":{"message":"model not found"}}'
        assert _is_upstream_busy(400, text) is False
        assert _is_dead_model_400(text) is True

    def test_plain_bad_request_not_busy(self):
        text = '{"error":{"message":"max_tokens too large"}}'
        assert _is_upstream_busy(400, text) is False

    def test_status_gate(self):
        """5xx/401 不属于 busy 分类(走原有熔断逻辑)。"""
        assert _is_upstream_busy(500, "并发会话数已达上限") is False
        assert _is_upstream_busy(401, "并发会话数已达上限") is False


class TestQuotaReason:
    """429 配额耗尽必须给出可读原因, 不能只回 upstream 429。"""

    def test_opencode_free_usage_limit(self):
        from backend.core.proxy import _quota_reason
        text = ('{"type":"FreeUsageLimitError","message":"Free usage limit reached. '
                'Subscribe to OpenCode Go to keep going: https://opencode.ai/go"}')
        reason = _quota_reason(text)
        assert "免费额度" in reason
        assert "429" not in reason

    def test_billing_daily(self):
        from backend.core.proxy import _quota_reason
        assert "额度" in _quota_reason("Billing daily count exceeded")

    def test_insufficient_quota(self):
        from backend.core.proxy import _quota_reason
        assert "配额" in _quota_reason('{"error":{"code":"insufficient_quota"}}')

    def test_balance(self):
        from backend.core.proxy import _quota_reason
        assert "余额" in _quota_reason("insufficient balance")

    def test_unknown_quota_text_falls_back(self):
        from backend.core.proxy import _quota_reason
        assert "额度" in _quota_reason("something weird")

    def test_kilo_promotion_limit(self):
        """Kilo 的免费推广额度耗尽会回 PROMOTION_MODEL_LIMIT_REACHED。"""
        from backend.core.proxy import _quota_reason
        text = ('{"error":{"code":"PROMOTION_MODEL_LIMIT_REACHED","message":'
                '"Sign up for free to continue and explore 500 other models."}}')
        reason = _quota_reason(text)
        assert "免费推广额度" in reason
        assert "429" not in reason
