"""账号等级限制(workbuddy 11158)识别与归因。"""
import sys

sys.path.insert(0, r"D:\zmzc-code\ai-gateway-pro")

from backend.core import proxy as core  # noqa: E402

WB_11158 = ('{"code":11158,"msg":"model [gpt-6-luna] is not allowed for user type '
            '[personal_free_user]","requestId":"9a39","displayMsg":{"en":"Your account '
            'type does not support thi')


class TestIsTierBlock:
    def test_hits_workbuddy_11158(self):
        assert core._is_tier_block_400(400, WB_11158)

    def test_hits_english_phrasings(self):
        for text in (
            "Your account type does not support this model",
            "this model is not supported for your account",
            "please upgrade your plan to use this model",
            "该模型对当前账号类型不支持",
        ):
            assert core._is_tier_block_400(400, text), text

    def test_ignores_non_400(self):
        for status in (200, 401, 403, 404, 429, 500):
            assert not core._is_tier_block_400(status, WB_11158), status

    def test_ignores_other_400(self):
        for text in (
            '{"code":11128,"msg":"Illegal API invocation from an unapproved channel"}',
            '{"code":11101,"msg":"Non-stream chat request is currently not supported"}',
            "context length exceeded",
            "APIG.0602 Bad request: the security token has expired",
            "",
            None,
        ):
            assert not core._is_tier_block_400(400, text), text


class TestTierReason:
    def test_reason_is_actionable(self):
        r = core._tier_reason(WB_11158)
        assert "账号等级" in r
        assert "换模型" in r
        # 不能把用户引去重登录(凭据是好的), 也不能说"等额度"(不会自愈)
        assert "重新登录" not in r
        assert "额度" not in r

    def test_reason_constant_for_any_input(self):
        assert core._tier_reason("x") == core._tier_reason(WB_11158)


class TestTierNotConfusedWithOthers:
    def test_not_dead_model(self):
        # 等级限制是"模型存在但账号不行", 与"模型不存在"分开判定, 但两者都不算
        # 请求侧 400 —— 关键是不能落进 _REQUEST_400_HINTS 的排除集
        assert not core._is_dead_model_400(WB_11158)

    def test_not_busy(self):
        assert not core._is_upstream_busy(400, WB_11158)

    def test_not_cred_expired(self):
        assert not core._is_cred_expired(400, WB_11158)

    def test_not_region(self):
        assert not core._is_region_blocked(400, WB_11158)


class TestBranchesWired:
    """四处 400 分支都必须接上等级限制判定, 且排在 busy 之前。"""

    def test_all_sites_present(self):
        import io
        src = io.open(r"D:\zmzc-code\ai-gateway-pro\backend\core\proxy.py",
                      encoding="utf-8").read()
        assert src.count("_is_tier_block_400(") >= 5, "def + 4 call sites"
        # 每一处调用都必须紧跟 record_not_found(确定性, 冷却移出)
        import re
        for m in re.finditer(r"_is_tier_block_400\([^)]*\)", src):
            if "def " in src[max(0, m.start() - 6):m.start()]:
                continue
            tail = src[m.end():m.end() + 200]
            assert "record_not_found" in tail, tail[:120]
