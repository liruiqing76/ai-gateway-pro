# ============================================================
# 智能网关 — 熔断器
# 连续失败 N 次 → 熔断 M 秒 → 半开 → 恢复
# 从原版 app.py 重构而来,增加时间戳和可测试性
# ============================================================

import time
import logging
from collections import deque

from backend.config import settings

logger = logging.getLogger("gateway.circuit")

# 死模型判定阈值与冷却时长:
# 上游 404/410 = 模型不存在/已下架, 是确定性状态(不像 429/5xx 会自行恢复),
# 连续出现多次后按"死模型"处理, 用 24h 长冷却而不是 60s 短冷却,
# 避免每笔请求都白试一遍下架模型(历史上 kimi-k2.6/k3、AMD GLM-5.2 一天烧掉 400 次失败尝试)
DEAD_MODEL_THRESHOLD = 3
DEAD_MODEL_COOLDOWN_SECONDS = 24 * 3600

# 模型级"反复报错自动下架": 连续 DOWN_STRIKE_THRESHOLD 轮失败(每轮冷却结束仍再失败)
# 即视为该模型持续不可用, 进入 DOWN_COOLDOWN_SECONDS 长下架冷却。只下架具体模型(key),
# 绝不下架整个源; 长冷却到期自动恢复参与路由(免费模型偶发恢复是常态)。
DOWN_STRIKE_THRESHOLD = 5
DOWN_COOLDOWN_SECONDS = 6 * 3600


class CircuitState:
    """单个 (provider, model) 键的熔断状态"""

    def __init__(self):
        self.fails = 0
        self.open_until = 0.0      # 熔断打开截止时间
        self.last_fail_at = 0.0
        self.last_success_at = 0.0
        self.rate_limit_count = 0
        self.timeout_count = 0     # 挂死型超时连续次数(指数退避, 见 record_timeout_fail)
        self.strike_count = 0      # 连续失败轮次(跨熔断周期), 达到阈值自动下架


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
        # 死模型 404 连续计数(key -> 次数), 达到阈值即 24h 长冷却(见 record_not_found)
        self._not_found_count: dict[str, int] = {}
        # 刚触发"自动下架"的 key 集合: api 层消费后持久化到 downed_models
        self._downed_keys: set[str] = set()
        # 渠道级软冷却(provider_id -> open_until): 免费档多为账号级共享额度/限流,
        # 一个模型被打 429, 同渠道其它模型大概率也受限。冷却期内该渠道整体降权
        # (权重×0.05, 优先吃别的渠道), 但不硬禁用 —— 模型级熔断才是硬开关。
        self._provider_cooldown: dict[int, float] = {}
        self._provider_rc: dict[int, int] = {}

    def _get(self, key: str) -> CircuitState:
        st = self._states.get(key)
        if st is None:
            st = CircuitState()
            self._states[key] = st
        return st

    def _register_strike(self, key: str, st: CircuitState) -> bool:
        """熔断/限流/超时触发时累计"失败轮次"; 达到 DOWN_STRIKE_THRESHOLD 即
        进入长下架冷却(等效自动下架该模型)。返回 True 表示本次刚触发下架。
        任何一次成功都会清零(见 record_success), 偶发故障不会误下架。"""
        st.strike_count += 1
        if st.strike_count >= DOWN_STRIKE_THRESHOLD:
            st.strike_count = 0
            st.fails = self.fail_threshold
            st.open_until = time.time() + DOWN_COOLDOWN_SECONDS
            st.last_fail_at = time.time()
            self._downed_keys.add(key)
            logger.warning(
                "model auto-downed (连续 %d 轮失败): %s — 下架冷却 %.1fh",
                DOWN_STRIKE_THRESHOLD, key, DOWN_COOLDOWN_SECONDS / 3600,
            )
            return True
        return False

    def consume_downed(self, key: str) -> bool:
        """取出并清除 key 的"刚触发自动下架"标记。

        供 api 层在模型失败时消费: 返回 True 表示该模型刚被熔断器判定为
        反复报错并自动下架, 应持久化到 downed_models(重启不丢)。
        """
        if key in self._downed_keys:
            self._downed_keys.discard(key)
            return True
        return False

    def is_open(self, key: str) -> bool:
        """熔断是否打开(该模型此刻不可用)"""
        st = self._states.get(key)
        if st is None:
            return False
        if st.open_until > time.time():
            return True
        return False

    def cooldown_seconds(self, key: str) -> int:
        """该模型剩余冷却秒数(用于快速失败时的 Retry-After)"""
        st = self._states.get(key)
        if st is None:
            return 0
        return max(0, int(st.open_until - time.time()))

    def record_fail(self, key: str) -> bool:
        """记录一次失败,返回是否触发熔断"""
        st = self._get(key)
        st.fails += 1
        st.last_fail_at = time.time()
        if st.fails >= self.fail_threshold:
            st.open_until = time.time() + self.recovery_seconds
            st.fails = 0
            self._register_strike(key, st)
            return True
        return False

    def record_success(self, key: str) -> None:
        """记录一次成功,重置失败计数"""
        st = self._get(key)
        st.fails = 0
        st.rate_limit_count = 0
        st.timeout_count = 0
        st.strike_count = 0
        st.last_success_at = time.time()
        # 如果熔断到期,自然恢复
        st.open_until = 0.0
        self._not_found_count.pop(key, None)

    def force_open(self, key: str, duration: int = None) -> None:
        """强制打开熔断(用于 429 快速限流)"""
        st = self._get(key)
        st.fails = self.fail_threshold  # 标记为触发了熔断
        st.open_until = time.time() + (duration or self.recovery_seconds)
        st.last_fail_at = time.time()

    def record_timeout_fail(self, key: str) -> None:
        """记录一次"挂死型"超时(连接成功但长时间无数据)。

        普通 record_fail 的 60s 短熔断只挡一轮: 挂死模型(如 NVIDIA
        deepseek-v4-pro-0813 非流式 120s 无响应、sensenova-6.8-flash-lite
        流式烧满 30s)每次冷却结束就又被路由到, 每笔请求白烧一次超时, 能拖
        几十分钟。这里用与 429 相同的指数退避(90s → 180s → ... → 30min),
        超时连续次数越多冷却越长; 模型恢复后一次成功即清零。
        """
        st = self._get(key)
        st.timeout_count += 1
        stage = min(st.timeout_count - 1, 4)  # 90→180→360→720→1440, 按 1800 封顶
        backoff = min(90 * (2 ** stage), 1800)
        st.fails = self.fail_threshold  # 触发熔断
        st.open_until = time.time() + backoff
        st.last_fail_at = time.time()
        self._register_strike(key, st)
        logger.info("timeout hang for %s, cooling %.0fs", key, backoff)

    def record_rate_limit(self, key: str) -> None:
        """记录 429 限流, 指数退避冷却: 30s→60s→120s→...→300s max"""
        st = self._get(key)
        st.rate_limit_count += 1
        # 指数退避: base=30s, *2^count, max=300s
        backoff = min(30 * (2 ** (st.rate_limit_count - 1)), 300)
        st.fails = self.fail_threshold  # 触发熔断
        st.open_until = time.time() + backoff
        st.last_fail_at = time.time()
        self._register_strike(key, st)

    def record_quota_exhausted(self, key: str) -> None:
        """记录配额/余额耗尽(insufficient_quota / insufficient balance / entitlement)。

        这类 429 在充值/重置额度之前会持续失败, 不像一般限流几十秒后能恢复,
        用更长的冷却(120s 起、指数递增、上限 30min), 避免每笔请求都白打一遍。
        """
        st = self._get(key)
        st.rate_limit_count += 1
        stage = min(st.rate_limit_count - 1, 4)  # 120→240→480→960→1920, 再按 1800 封顶
        backoff = min(120 * (2 ** stage), 1800)
        st.fails = self.fail_threshold
        st.open_until = time.time() + backoff
        st.last_fail_at = time.time()
        self._register_strike(key, st)
        logger.info("quota exhausted for %s, cooling %.0fs", key, backoff)

    def record_not_found(self, key: str, code: int | None = None) -> bool:
        """记录一次确定性"模型不可用"错误(404/410 下架, 或模型级 400 如
        "has no provider supported")。

        这类错误是确定性状态(不像 429/5xx 会自行恢复), 连续达到
        DEAD_MODEL_THRESHOLD 次后打开 DEAD_MODEL_COOLDOWN_SECONDS(24h)长冷却,
        避免每笔请求都白试一遍不可用模型。返回 True 表示"本次刚判定为死模型",
        调用方可据此把该模型从路由组里自动移除(移除是幂等的, 移除后冷却期
        内不会再被路由到)。
        """
        code = code or 404
        n = self._not_found_count.get(key, 0) + 1
        self._not_found_count[key] = n
        st = self._get(key)
        st.last_fail_at = time.time()
        if n >= DEAD_MODEL_THRESHOLD:
            self._not_found_count.pop(key, None)  # 冷却期结束后若仍不可用会重新累计
            st.fails = self.fail_threshold
            st.open_until = time.time() + DEAD_MODEL_COOLDOWN_SECONDS
            logger.warning(
                "model marked dead (连续 %d×%d): %s — 长冷却 %.1fh, 建议从路由组移除",
                DEAD_MODEL_THRESHOLD, code, key, DEAD_MODEL_COOLDOWN_SECONDS / 3600,
            )
            return True
        return False

    def is_dead_suspect(self, key: str) -> bool:
        """该模型是否已累计过确定性错误(未达阈值但已出现 404/400 迹象)"""
        return key in self._not_found_count

    # ============================================================
    # 渠道级软冷却: 429 → 整个渠道降权, 成功 → 立即恢复
    # ============================================================
    def record_provider_rate_limit(self, provider_id: int, quota: bool = False) -> None:
        """渠道收到 429/配额耗尽时记一次冷却(指数递增, 成功调用即清零)。

        quota=True 表示配额/余额耗尽(长效冷却 120s 起步), 否则一般限流 30s 起步。
        """
        cnt = self._provider_rc.get(provider_id, 0) + 1
        self._provider_rc[provider_id] = cnt
        stage = min(cnt - 1, 4)
        base = 120 if quota else 30
        backoff = min(base * (2 ** stage), 1800 if quota else 300)
        self._provider_cooldown[provider_id] = time.time() + backoff
        logger.info("provider %s cooling %.0fs (quota=%s)",
                    provider_id, backoff, quota)

    def record_provider_success(self, provider_id: int) -> None:
        """渠道任意模型调用成功 → 冷却清零(有成功说明渠道已恢复)"""
        if self._provider_rc.pop(provider_id, None) is not None:
            self._provider_cooldown.pop(provider_id, None)

    def provider_in_cooldown(self, provider_id: int) -> bool:
        until = self._provider_cooldown.get(provider_id, 0.0)
        return until > time.time()

    def provider_cooldown_seconds(self, provider_id: int) -> int:
        until = self._provider_cooldown.get(provider_id, 0.0)
        return max(0, int(until - time.time()))

    def provider_weight(self, provider_id: int) -> float:
        """冷却期权重 0.05(几乎不吃流量), 正常 1.0 —— 只降权不硬禁用"""
        return 0.05 if self.provider_in_cooldown(provider_id) else 1.0

    def provider_cooldowns(self) -> dict[int, int]:
        """当前所有冷却中的渠道 (provider_id -> 剩余秒数), 供快速失败计算 Retry-After"""
        now = time.time()
        return {pid: max(0, int(until - now))
                for pid, until in self._provider_cooldown.items()
                if until > now}

    def reset(self, key: str) -> None:
        """手动重置"""
        if key in self._states:
            del self._states[key]

    def reset_all(self) -> None:
        """清空全部熔断状态(每日额度渠道凌晨重置用)"""
        self._states.clear()
        self._provider_cooldown.clear()
        self._provider_rc.clear()

    @property
    def open_keys(self) -> list[str]:
        """当前所有打开的键"""
        return [k for k, st in self._states.items() if st.open_until > time.time()]


# ============================================================
# 全局熔断器实例
# ============================================================
circuit_breaker = CircuitBreaker()