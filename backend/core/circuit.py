# ============================================================
# 智能网关 — 熔断器
# 连续失败 N 次 → 熔断 M 秒 → 半开 → 恢复
# 从原版 app.py 重构而来,增加时间戳和可测试性
# ============================================================

import time
from collections import deque

from backend.config import settings


class CircuitState:
    """单个 (provider, model) 键的熔断状态"""

    def __init__(self):
        self.fails = 0
        self.open_until = 0.0      # 熔断打开截止时间
        self.last_fail_at = 0.0
        self.last_success_at = 0.0


class CircuitBreaker:
    """
    熔断器管理器。

    规则:
    - 连续失败 >= FAIL_THRESHOLD 次 → open,持续 RECOVERY_SECONDS
    - open 期间该模型被跳过(经验证:直接失败 vs 跳过,跳过更稳)
    - 成功后重置失败计数
    """

    def __init__(
        self,
        fail_threshold: int = None,
        recovery_seconds: int = None,
    ):
        self.fail_threshold = fail_threshold or settings.CIRCUIT_FAIL_THRESHOLD
        self.recovery_seconds = recovery_seconds or settings.CIRCUIT_RECOVERY_SECONDS
        self._states: dict[str, CircuitState] = {}

    def _get(self, key: str) -> CircuitState:
        st = self._states.get(key)
        if st is None:
            st = CircuitState()
            self._states[key] = st
        return st

    def is_open(self, key: str) -> bool:
        """熔断是否打开(该模型此刻不可用)"""
        st = self._states.get(key)
        if st is None:
            return False
        if st.open_until > time.time():
            return True
        return False

    def record_fail(self, key: str) -> bool:
        """记录一次失败,返回是否触发熔断"""
        st = self._get(key)
        st.fails += 1
        st.last_fail_at = time.time()
        if st.fails >= self.fail_threshold:
            st.open_until = time.time() + self.recovery_seconds
            st.fails = 0
            return True
        return False

    def record_success(self, key: str) -> None:
        """记录一次成功,重置失败计数"""
        st = self._get(key)
        st.fails = 0
        st.last_success_at = time.time()
        # 如果熔断到期,自然恢复
        st.open_until = 0.0

    def force_open(self, key: str, duration: int = None) -> None:
        """强制打开熔断(用于 429 快速限流)"""
        st = self._get(key)
        st.fails = self.fail_threshold  # 标记为触发了熔断
        st.open_until = time.time() + (duration or self.recovery_seconds)
        st.last_fail_at = time.time()

    def reset(self, key: str) -> None:
        """手动重置"""
        if key in self._states:
            del self._states[key]

    @property
    def open_keys(self) -> list[str]:
        """当前所有打开的键"""
        return [k for k, st in self._states.items() if st.open_until > time.time()]


# ============================================================
# 全局熔断器实例
# ============================================================
circuit_breaker = CircuitBreaker()