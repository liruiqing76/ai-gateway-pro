# -*- coding: utf-8 -*-
"""凭据过期识别测试 —— 上游常把"凭据过期"报成 400, 必须给可操作文案且不记死模型。"""
import pytest

from backend.core.proxy import (_CHANNEL_NAMES, _CRED_EXPIRED_HINTS,
                                _is_cred_expired, _is_dead_model_400)


class TestCredExpiredDetection:
    def test_codearts_apig_0602(self):
        """华为云 security token 过期: 400 APIG.0602(真机实测)"""
        assert _is_cred_expired(
            400,
            '{"error_msg":"Bad request: the security token has expired",'
            '"error_code":"APIG.0602","request_id":"c2719e43"}')

    def test_codearts_sts_1806(self):
        """华为云 refresh token 已被用过: 400 STS5.1806"""
        assert _is_cred_expired(
            400, '{"error_code": "STS5.1806", "error_msg": '
                 '"invalid refresh token: \'the refresh token has been used\'"}')

    def test_cline_reauth_message(self):
        """cline 401 提示重新认证"""
        assert _is_cred_expired(
            401, '{"error":"Unauthorized: Please make sure you\'re using the '
                 'latest version of Cline and re-authenticate your Cline account."}')

    def test_plain_401_without_hint(self):
        """裸 401 没有过期特征 —— 交给调用点的渠道名分支处理"""
        assert not _is_cred_expired(401, '{"error":"unauthorized"}')

    def test_quota_403_not_cred_expired(self):
        """权益耗尽不是凭据过期"""
        assert not _is_cred_expired(
            403, "TM.000010051 当前所选模型的使用权益已耗尽")

    def test_5xx_never_cred_expired(self):
        assert not _is_cred_expired(500, "the security token has expired")

    def test_hints_are_lowercase(self):
        """匹配前统一 lower(), 常量本身必须小写, 否则永不命中"""
        for h in _CRED_EXPIRED_HINTS:
            assert h == h.lower()


class TestCredExpiredBeforeDeadModel:
    """过期判定必须先于死模型判定(否则 400 会被记成 24h 死模型)"""

    def test_expired_400_not_classified_dead(self):
        text = '{"error_msg":"Bad request: the security token has expired",' \
               '"error_code":"APIG.0602"}'
        # 过期文案本身不含死模型关键词, 两者互斥
        assert not _is_dead_model_400(text)
        assert _is_cred_expired(400, text)


class TestChannelNames:
    def test_channels_covered(self):
        for name in ("codearts", "cline", "workbuddy", "buddy",
                     "trae", "qodercn", "qoder", "opencode", "kilo"):
            assert name in _CHANNEL_NAMES
