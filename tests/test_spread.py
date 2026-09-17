# ============================================================
# 加权随机分发(spread)测试 — 流量摊开, 防单一上游限流打爆
# ============================================================
import sys, random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from backend.core import proxy
from backend.core.circuit import circuit_breaker
from backend.core.scheduler import quality_tracker
from backend.models import Provider


def _provider(name, models, disabled=None):
    return Provider(
        name=name, models=models, disabled_models=disabled or [],
        api_key_encrypted="", base_url="http://x", enabled=True,
    )


def _names(cands):
    return [f"{p.name}-{m}" for p, m, *_ in cands]


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    """隔离全局状态 + 固定随机种子(分布测试可复现)"""
    proxy._round_robin_counter.clear()
    quality_tracker.reset_all()   # 含 today_usage 清零(seed 置 True, 不碰真实 DB)
    circuit_breaker.reset_all()   # 含渠道冷却/计数清零
    monkeypatch.setattr(proxy, "_rng", random.Random(20260831))
    yield
    proxy._round_robin_counter.clear()


def test_spread_broken_candidates_always_last(monkeypatch):
    """熔断候选不参与抽样, 恒沉底当兜底(_sort_by_strategy 层)"""
    monkeypatch.setattr(circuit_breaker, "is_open", lambda k: k == "B||b1")
    providers = [_provider("A", ["a1"]), _provider("B", ["b1"])]
    raw = [(providers[0], "a1", "A||a1"), (providers[1], "b1", "B||b1")]
    for _ in range(10):
        cands = proxy._sort_by_strategy(raw, "fastest", rr_key="g:fastest")
        assert _names(cands)[-1] == "B-b1"
        assert _names(cands)[0] == "A-a1"


def test_spread_weights_quota_bias():
    """今日用量高的模型权重显著下降(用量摊开)"""
    pA, pB = _provider("A", ["a1"]), _provider("B", ["b1"])
    quality_tracker.today_usage["A||a1"] = 40
    quality_tracker.today_usage["B||b1"] = 0
    scored = [
        (pA, "a1", "A||a1", 0.0, 0.0, False, 0),
        (pB, "b1", "B||b1", 0.0, 0.0, False, 1),
    ]
    healthy, weights, broken = proxy._spread_weights(scored, "quality")
    assert len(healthy) == 2 and not broken
    # 用量高(A, 40 次)的权重低: SPREAD_QUOTA_BIAS=0.4 → ÷(1+40×0.4)=17
    assert weights[0] < weights[1]
    ratio = weights[1] / weights[0]
    assert 15 <= ratio <= 19, f"quota bias ratio {ratio}"


def test_spread_provider_cooldown_cuts_weight():
    """渠道 429 冷却期权重 ×0.05(只降权不硬禁用)"""
    pB = _provider("B", ["b1"])
    pB.id = 999
    circuit_breaker.record_provider_rate_limit(999)
    pA = _provider("A", ["a1"])
    scored = [
        (pA, "a1", "A||a1", 0.0, 0.0, False, 0),
        (pB, "b1", "B||b1", 0.0, 0.0, False, 1),
    ]
    healthy, weights, broken = proxy._spread_weights(scored, "quality")
    assert weights[1] == pytest.approx(weights[0] * 0.05, rel=0.01)
    # 成功调用 → 冷却清除 → 权重恢复
    circuit_breaker.record_provider_success(999)
    assert circuit_breaker.provider_weight(999) == 1.0


def test_spread_heavier_candidate_first_most_of_time(monkeypatch):
    """质量/延迟占优的候选被抽样到首位的次数显著更多, 但不是恒定(仍会摊开)"""
    monkeypatch.setattr(circuit_breaker, "is_open", lambda k: False)
    providers = [_provider("A", ["a1"]), _provider("B", ["b1"])]
    routers = {"g": ["A-a1", "B-b1"]}
    # A 快(10ms), B 慢(5s) → fastest 权重 A 约 500 倍于 B
    quality_tracker.record("A||a1", "ok", latency_ms=10)
    quality_tracker.record("B||b1", "ok", latency_ms=5000)
    firsts = []
    for _ in range(300):
        cands = proxy.pick_available_models(providers, "g", routers, strategy="fastest")
        firsts.append(_names(cands)[0])
    a_first = firsts.count("A-a1")
    # 均匀期望 150, 权重极端偏 A ≈ 285+; 放宽到 200 留足余量且仍远高于随机
    assert a_first > 200, f"A first only {a_first}/300"
    # 慢候选也必须能偶尔轮上(证明是抽样, 不是恒定排序)
    assert firsts.count("B-b1") > 0


def test_spread_uniform_when_equal_weights(monkeypatch):
    """权重相同时近似均匀分布, 每候选都能轮到首位"""
    monkeypatch.setattr(circuit_breaker, "is_open", lambda k: False)
    providers = [_provider("A", ["a1"]), _provider("B", ["b1"]), _provider("C", ["c1"])]
    routers = {"g": ["A-a1", "B-b1", "C-c1"]}
    firsts = []
    for _ in range(300):
        cands = proxy.pick_available_models(providers, "g", routers, strategy="fastest")
        firsts.append(_names(cands)[0])
    counts = {n: firsts.count(n) for n in ("A-a1", "B-b1", "C-c1")}
    # 均匀期望各 100; 3 选 1 均匀下 90% 区间约 75~125
    for n, c in counts.items():
        assert 60 <= c <= 140, f"{n} first {c}/300, counts={counts}"


def test_spread_weight_never_zero_min_floor():
    """权重下限 1e-6: 最差候选也只是沉底, 不彻底移除"""
    pA, pB = _provider("A", ["a1"]), _provider("B", ["b1"])
    scored = [
        (pA, "a1", "A||a1", -2.0, 1e9, False, 0),
        (pB, "b1", "B||b1", 0.0, 0.1, False, 1),
    ]
    healthy, weights, broken = proxy._spread_weights(scored, "fastest")
    assert all(w >= 1e-6 for w in weights)


def test_spread_disabled_falls_back_to_round_robin(monkeypatch):
    """关闭加权分发 → 恢复确定性轮询(回退路径保留)"""
    monkeypatch.setattr(proxy.settings, "SPREAD_WEIGHTED_ENABLED", False)
    monkeypatch.setattr(circuit_breaker, "is_open", lambda k: False)
    providers = [_provider("A", ["a1", "a2"]), _provider("B", ["b1"])]
    routers = {"g": ["A-a1", "A-a2", "B-b1"]}
    firsts = []
    for _ in range(9):
        cands = proxy.pick_available_models(providers, "g", routers, strategy="fastest")
        firsts.append(_names(cands)[0])
    assert len(set(firsts)) == 3
    for want in ("A-a1", "A-a2", "B-b1"):
        assert want in firsts, f"{want} never became first: {firsts}"


def test_sample_weighted_zero_weight_sinks():
    """零权重候选在抽样中沉底(权重 0 → 永不提前)"""
    rng = random.Random(7)
    order = proxy._sample_weighted(3, [1.0, 0.0, 0.0], rng)
    assert order[0] == 0  # 唯一正权重必先出
    assert set(order) == {0, 1, 2}  # 全部位置最终覆盖