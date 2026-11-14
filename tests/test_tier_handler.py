"""账号等级限制: 移出路由组但不永久拉黑(与死模型区分)。"""
import sys

sys.path.insert(0, r"D:\zmzc-code\ai-gateway-pro")

from backend.core import proxy as core  # noqa: E402


class TestTierReasonMarker:
    """`_handle_tier_block` 靠文案里的"账号等级"识别, 这个契约必须锁住。"""

    def test_reason_contains_marker(self):
        assert "账号等级" in core._tier_reason("x")

    def test_reason_does_not_contain_dead_model_marker(self):
        # 若含 "dead model" 会被 _handle_upstream_404 抢走并永久拉黑
        assert "dead model" not in core._tier_reason("x")
        assert "upstream 404" not in core._tier_reason("x")


class TestHandlerSource:
    def test_handler_exists_and_skips_ban(self):
        import io
        src = io.open(r"D:\zmzc-code\ai-gateway-pro\backend\api\proxy.py",
                      encoding="utf-8").read()
        start = src.index("def _handle_tier_block(")
        body = src[start:start + 2200]
        # 必须移除路由组
        assert "Router" in body and "r.models" in body
        # 必须不写永久黑名单
        assert "ban_model" not in body, "等级限制不能永久拉黑(升级后可恢复)"
        # 必须要求冷却达阈值(record_not_found 返回 True)
        assert "record_not_found" in body
        assert 'if not err or "账号等级" not in str(err)' in body

    def test_called_from_both_surfaces(self):
        import io
        for path in (r"D:\zmzc-code\ai-gateway-pro\backend\api\proxy.py",
                     r"D:\zmzc-code\ai-gateway-pro\backend\api\anthropic.py"):
            src = io.open(path, encoding="utf-8").read()
            assert src.count("_handle_tier_block(user, provider, model, err)") == 2, path
