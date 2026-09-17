# ============================================================
# 智能网关 — 流量整形
# 渠道级并发信号量 + 模型级最小调用间隔(令牌桶)
#
# 为什么需要: 网关本身没有并发/频率控制时, Hermes 等客户端并行发 N 个
# 请求会同时打到同一个上游 → RPM 打爆 → 429, 再连锁触发模型熔断。
# 整形后: 并发超限的请求等在信号量外(超时则换候选), 同一 (渠道,模型)
# 的调用间隔被拉开, 突发流量被削峰。
# ============================================================

import asyncio
import threading
import time

from backend.config import settings

logger = None  # 延迟导入避免循环


def _log():
    global logger
    if logger is None:
        import logging
        logger = logging.getLogger("gateway.shaping")
    return logger


class TrafficShaping:
    """渠道并发 + 模型调用间隔的统一整形器(单进程单例)。"""

    def __init__(self):
        # provider_id -> asyncio.Semaphore(按渠道并发上限懒创建, 上限变化时重建)
        self._sems: dict[int, asyncio.Semaphore] = {}
        self._sem_lock = threading.Lock()
        self._max_conc: dict[int, int] = {}
        # provider_id -> 最小调用间隔(秒); key -> 上次调用时间戳(monotonic)
        self._min_interval: dict[int, float] = {}
        self._last_call: dict[str, float] = {}

    # ── 参数解析: 渠道 DB 列可覆盖全局默认 ──
    def resolve_params(self, provider_id: int, free_only: bool,
                       max_concurrency: int | None = None,
                       min_interval_ms: int | None = None) -> tuple[int, float]:
        """返回 (并发上限, 最小间隔秒数), 并缓存到本实例。"""
        mc = (max_concurrency or
              (settings.SHAPING_FREE_MAX_CONCURRENCY if free_only
               else settings.SHAPING_DEFAULT_MAX_CONCURRENCY))
        # min_interval_ms 显式传 0 → 不限速(pace 遇 <=0 跳过); None/缺省 → 默认
        mi = ((min_interval_ms / 1000.0)
              if min_interval_ms is not None
              else settings.SHAPING_DEFAULT_MIN_INTERVAL_MS / 1000.0)
        self._max_conc[provider_id] = mc
        self._min_interval[provider_id] = mi
        # 并发上限变化 → 重建信号量(acquire 依赖已创建的槽)
        self._semaphore(provider_id, mc)
        return mc, mi

    def max_concurrency(self, provider_id: int) -> int:
        return self._max_conc.get(provider_id, settings.SHAPING_DEFAULT_MAX_CONCURRENCY)

    def _semaphore(self, provider_id: int, mc: int) -> asyncio.Semaphore:
        sem = self._sems.get(provider_id)
        if sem is None or sem._value != mc:  # 并发上限变化时重建
            with self._sem_lock:
                sem = self._sems.get(provider_id)
                if sem is None or sem._value != mc:
                    sem = asyncio.Semaphore(mc)
                    self._sems[provider_id] = sem
        return sem

    async def acquire(self, provider_id: int, timeout_s: float | None = None) -> bool:
        """占用一个并发槽。槽满时最多等 timeout_s 秒, 仍无槽则返回 False(调用方换候选)。"""
        sem = self._sems.get(provider_id)
        if sem is None:
            # 未显式 resolve 过的渠道: 按默认上限懒创建, 保证槽位始终生效
            mc = self._max_conc.get(provider_id, settings.SHAPING_DEFAULT_MAX_CONCURRENCY)
            sem = self._semaphore(provider_id, mc)
        timeout = timeout_s if timeout_s is not None else settings.SHAPING_SEMAPHORE_TIMEOUT
        try:
            await asyncio.wait_for(sem.acquire(), timeout)
            return True
        except asyncio.TimeoutError:
            _log().warning("provider %s slot busy >%.1fs, skip", provider_id, timeout)
            return False
        except asyncio.CancelledError:
            raise

    def release(self, provider_id: int) -> None:
        sem = self._sems.get(provider_id)
        if sem is not None:
            sem.release()

    async def pace(self, key: str, provider_id: int) -> None:
        """模型级最小间隔: 距上次调用不足间隔则补觉, 防 RPM 打爆。"""
        interval = self._min_interval.get(provider_id, 0.0)
        if interval <= 0:
            return
        now = time.monotonic()
        last = self._last_call.get(key, 0.0)
        wait = last + interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call[key] = time.monotonic()

    def reset(self) -> None:
        """清空全部整形状态(测试/每日重置用)"""
        self._sems.clear()
        self._max_conc.clear()
        self._min_interval.clear()
        self._last_call.clear()


# 全局整形器单例
traffic_shaping = TrafficShaping()