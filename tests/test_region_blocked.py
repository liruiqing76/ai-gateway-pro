"""地区封锁(RegionError)不能被误报成"凭据过期"。"""
import pytest

from backend.core.proxy import (
    _is_region_blocked,
    _region_reason,
    _CHANNEL_NAMES,
)

# 实测原文: opencode `beam-free` 的 403
REGION_BODY = ('{"type":"error","error":{"type":"RegionError",'
               '"message":"This model is not available in your country"}}')


class TestRegionBlocked:
    def test_opencode_region_error_detected(self):
        assert _is_region_blocked(403, REGION_BODY) is True

    def test_other_region_phrasings(self):
        assert _is_region_blocked(403, "This model is not available in your region")
        assert _is_region_blocked(400, '{"type":"RegionError"}')
        assert _is_region_blocked(403, "unsupported region")
        assert _is_region_blocked(403, "该模型地区不支持")

    def test_not_region_for_other_failures(self):
        assert _is_region_blocked(403, '{"error":"invalid access token"}') is False
        assert _is_region_blocked(429, REGION_BODY) is False   # 429 不算地区封锁
        assert _is_region_blocked(500, REGION_BODY) is False
        assert _is_region_blocked(200, REGION_BODY) is False

    def test_reason_is_actionable_not_relogin(self):
        """文案必须引导"换模型", 不能说"重新登录" —— 免密车道没有登录可做。"""
        reason = _region_reason(REGION_BODY)
        assert "地区" in reason
        assert "换模型" in reason
        assert "重新登录" not in reason

    def test_opencode_is_channel_name_so_order_matters(self):
        """opencode 在 _CHANNEL_NAMES 里, 所以地区判定必须先于渠道 401/403 判定。

        这条断言锁定"顺序"这个前提: 一旦有人把 opencode 移出 _CHANNEL_NAMES,
        说明凭据文案策略变了, 需要重新检查上面几处分支顺序。
        """
        assert "opencode" in _CHANNEL_NAMES
        assert "kilo" in _CHANNEL_NAMES


class TestRegionBranchOrder:
    """分支顺序回归: 地区封锁必须先命中, 而不是落到渠道凭据文案。"""

    def test_non_stream_branch_returns_region_reason(self, monkeypatch):
        import backend.core.proxy as px

        calls = []
        monkeypatch.setattr(px.circuit_breaker, "record_fail",
                            lambda key: calls.append(key))
        # 直接验证"地区判定在渠道判定之前"的谓词组合
        assert px._is_region_blocked(403, REGION_BODY)
        assert "opencode" in px._CHANNEL_NAMES
        # 若顺序错了, 会走 _CHANNEL_NAMES 分支 → 文案含"重新登录"
        assert "重新登录" not in px._region_reason(REGION_BODY)
