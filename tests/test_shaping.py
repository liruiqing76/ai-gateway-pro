# ============================================================
# 流量整形测试 — 渠道并发信号量 + 模型最小调用间隔
# ============================================================
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from backend.config import settings
from backend.core.shaping import TrafficShaping


@pytest.fixture
def ts():
    return TrafficShaping()


# ── 参数解析 ──
def test_resolve_params_defaults(ts):
    """free-only 渠道并发取 SHAPING_FREE_* 默认, 普通渠道取 SHAPING_DEFAULT_*;
    间隔默认 600ms(.env 可覆盖, 断言跟随 settings 常量)"""
    mc, mi = ts.resolve_params(1, free_only=True)
    assert mc == settings.SHAPING_FREE_MAX_CONCURRENCY
    assert mi == pytest.approx(settings.SHAPING_DEFAULT_MIN_INTERVAL_MS / 1000.0)

    mc, mi = ts.resolve_params(2, free_only=False)
    assert mc == settings.SHAPING_DEFAULT_MAX_CONCURRENCY


def test_resolve_params_db_overrides(ts):
    """渠道级 DB 列覆盖全局默认"""
    mc, mi = ts.resolve_params(3, False, max_concurrency=7, min_interval_ms=120)
    assert mc == 7
    assert mi == pytest.approx(0.12)
    # 传 0/None 时回落默认(min_interval_ms 显式为 0 → 无间隔)
    mc, mi = ts.resolve_params(4, False, max_concurrency=0, min_interval_ms=0)
    assert mc == settings.SHAPING_DEFAULT_MAX_CONCURRENCY
    assert mi == 0.0


# ── 并发信号量 ──
@pytest.mark.asyncio
async def test_acquire_slot_limit_and_timeout(ts):
    """并发 2 时第 3 个请求超时跳过, 释放后立即可用"""
    ts.resolve_params(10, False, max_concurrency=2, min_interval_ms=0)
    assert await ts.acquire(10, timeout_s=0.05) is True
    assert await ts.acquire(10, timeout_s=0.05) is True
    assert await ts.acquire(10, timeout_s=0.05) is False  # 槽满 → 超时跳过
    ts.release(10)
    assert await ts.acquire(10, timeout_s=0.05) is True


@pytest.mark.asyncio
async def test_acquire_lazy_creates_slot(ts):
    """未显式 resolve 也按默认上限限流(防御路径)"""
    # 默认并发: 前 N 个立即通过, 第 N+1 个超时
    for _ in range(settings.SHAPING_DEFAULT_MAX_CONCURRENCY):
        assert await ts.acquire(99, timeout_s=0.05) is True
    assert await ts.acquire(99, timeout_s=0.05) is False


@pytest.mark.asyncio
async def test_concurrency_change_rebuilds_slot(ts):
    """配置变更后按新上限重建信号量"""
    ts.resolve_params(11, False, max_concurrency=1, min_interval_ms=0)
    assert await ts.acquire(11, timeout_s=0.05) is True
    assert await ts.acquire(11, timeout_s=0.05) is False  # 只有 1 槽
    ts.release(11)
    ts.resolve_params(11, False, max_concurrency=2, min_interval_ms=0)
    assert await ts.acquire(11, timeout_s=0.05) is True
    assert await ts.acquire(11, timeout_s=0.05) is True
    ts.release(11)
    ts.release(11)


@pytest.mark.asyncio
async def test_pace_min_interval(ts):
    """同一 (渠道,模型) 两次调用间隔不低于最小间隔"""
    ts.resolve_params(20, False, max_concurrency=8, min_interval_ms=80)
    t0 = time.monotonic()
    await ts.pace("P||m", 20)
    t1 = time.monotonic()
    await ts.pace("P||m", 20)
    t2 = time.monotonic()
    assert t2 - t1 >= 0.070, f"second call only {t2 - t1:.3f}s after first"
    # 不同模型互不排队(各自独立令牌)
    await ts.pace("P||other", 20)
    t3 = time.monotonic()
    assert t3 - t2 < 0.05


@pytest.mark.asyncio
async def test_pace_no_interval_when_zero(ts):
    """min_interval_ms=0 → 不做最小间隔限制"""
    ts.resolve_params(21, False, max_concurrency=8, min_interval_ms=0)
    t0 = time.monotonic()
    await ts.pace("P||m", 21)
    await ts.pace("P||m", 21)
    assert time.monotonic() - t0 < 0.05


def test_reset_clears_all(ts):
    ts.resolve_params(30, False, max_concurrency=2, min_interval_ms=100)
    ts.reset()
    assert ts._sems == {} and ts._max_conc == {} and ts._last_call == {}