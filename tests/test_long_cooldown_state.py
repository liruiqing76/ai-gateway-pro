"""`_long_cooldown_active`: 两层共用一个计数器时不能把"已判定死模型"摊薄掉。

真实故障: core/proxy.py 识别到确定性错误时先调 `record_not_found()`(不看返回值),
api 层再调一次时计数器已经被消耗, 若 api 层只信 `record_not_found()` 的返回值,
移除动作永远不发生 —— 日志里明明打了 "model marked dead" 但模型仍在路由组里。
"""
import sys

sys.path.insert(0, r"D:\zmzc-code\ai-gateway-pro")

import pytest  # noqa: E402

from backend.api.proxy import _long_cooldown_active  # noqa: E402
from backend.core.circuit import circuit_breaker  # noqa: E402


@pytest.fixture
def fresh_key():
    key = "tierblock-unit||dilution-model"
    circuit_breaker.reset_key(key) if hasattr(circuit_breaker, "reset_key") else None
    circuit_breaker._not_found_count.pop(key, None)
    st = circuit_breaker._get(key)
    st.open_until = 0
    st.fails = 0
    yield key
    circuit_breaker._not_found_count.pop(key, None)
    circuit_breaker._get(key).open_until = 0


class TestLongCooldownActive:
    def test_false_before_threshold(self, fresh_key):
        assert _long_cooldown_active(fresh_key) is False

    def test_true_once_threshold_reached(self, fresh_key):
        # 第 3 次达到 DEAD_MODEL_THRESHOLD → 进入 24h 长冷却
        assert _long_cooldown_active(fresh_key) is False
        assert _long_cooldown_active(fresh_key) is False
        assert _long_cooldown_active(fresh_key) is True

    def test_stays_true_on_extra_calls(self, fresh_key):
        """关键: 再多调用几次也必须继续 True(不能因计数器被清而回退)。"""
        for _ in range(3):
            _long_cooldown_active(fresh_key)
        assert _long_cooldown_active(fresh_key) is True
        assert _long_cooldown_active(fresh_key) is True
        assert _long_cooldown_active(fresh_key) is True

    def test_true_when_core_already_opened_it(self, fresh_key):
        """模拟 core 层先判定: api 层第一次调用就必须看到 True。"""
        for _ in range(3):
            circuit_breaker.record_not_found(fresh_key)  # core 层不看返回值
        assert _long_cooldown_active(fresh_key) is True

    def test_short_cooldown_is_not_enough(self, fresh_key):
        """60s 普通熔断不能被当成"确定性不可用"去永久拉黑。"""
        for _ in range(circuit_breaker.fail_threshold):
            circuit_breaker.record_fail(fresh_key)
        assert circuit_breaker.cooldown_seconds(fresh_key) > 0
        assert _long_cooldown_active(fresh_key) is False
