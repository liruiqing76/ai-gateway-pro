# -*- coding: utf-8 -*-
"""Channel 401 文案测试: 白嫖渠道凭据过期要给人能看懂的提示。"""
import httpx
import pytest

from backend.core.proxy import _CHANNEL_NAMES, _busy_reason, _is_upstream_busy


class TestChannelNameSet:
    def test_all_logged_in_channels_listed(self):
        for name in ("codearts", "workbuddy", "buddy", "cline",
                     "trae", "qodercn", "qoder", "opencode", "kilo"):
            assert name in _CHANNEL_NAMES

    def test_paid_upstreams_not_listed(self):
        for name in ("NVIDIA", "魔搭", "OpenRouter", "Agnes", "SenseNova"):
            assert name not in _CHANNEL_NAMES


class TestBusyStillWorks:
    def test_channel_quota_still_classified_busy(self):
        """渠道的配额/权益错误优先走 busy 分支, 不被 401/403 文案覆盖"""
        assert _is_upstream_busy(403, "TM.000010051 使用权益已耗尽")
        assert "权益" in _busy_reason("TM.000010051 使用权益已耗尽")

    def test_plain_401_not_busy(self):
        """裸 401 不算 busy —— 该走凭据过期文案"""
        assert not _is_upstream_busy(401, "unauthorized")
