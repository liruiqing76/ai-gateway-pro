# ============================================================
# 轮询模式 — 候选模型轮流使用, 避免逮着同一个模型打爆单上游
# ============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from backend.core import proxy
from backend.core.circuit import circuit_breaker
from backend.models import Provider


def _provider(name, models, disabled=None):
    return Provider(
        name=name, models=models, disabled_models=disabled or [],
        api_key_encrypted="", base_url="http://x", enabled=True,
    )


def _names(cands):
    return [f"{p.name}-{m}" for p, m in cands]


@pytest.fixture(autouse=True)
def fresh_round_robin():
    proxy._round_robin_counter.clear()
    yield
    proxy._round_robin_counter.clear()


@pytest.fixture
def no_breaker(monkeypatch):
    monkeypatch.setattr(circuit_breaker, "is_open", lambda k: False)


def test_round_robin_rotates_first_candidate(no_breaker, monkeypatch):
    providers = [_provider("A", ["a1", "a2"]), _provider("B", ["b1"])]
    routers = {"g": ["A-a1", "A-a2", "B-b1"]}
    # 本测试专测确定性轮询语义 → 关闭加权分发(其分布正确性由 test_spread.py 覆盖)
    monkeypatch.setattr(proxy.settings, "SPREAD_WEIGHTED_ENABLED", False)
    firsts = []
    for _ in range(9):
        cands = proxy.pick_available_models(providers, "g", routers, strategy="fastest")
        firsts.append(_names(cands)[0])
    # 首位应在所有候选间轮转, 而不是永远同一个
    assert len(set(firsts)) == 3, f"expected rotation over 3 models, got {firsts}"
    # 每个候选最终都会被轮到首位
    for want in ("A-a1", "A-a2", "B-b1"):
        assert want in firsts, f"{want} never became first: {firsts}"


def test_round_robin_circuit_open_excluded_unless_all_open(monkeypatch):
    # 部分熔断: 熔断候选直接排除(原有行为)
    monkeypatch.setattr(circuit_breaker, "is_open", lambda k: k == "B||b1")
    providers = [_provider("A", ["a1"]), _provider("B", ["b1"])]
    routers = {"g": ["A-a1", "B-b1"]}
    for _ in range(4):
        cands = proxy.pick_available_models(providers, "g", routers, strategy="fastest")
        assert _names(cands) == ["A-a1"]

    # 全部熔断: 全列表兜底返回, 且轮询不会把熔断候选提前
    monkeypatch.setattr(circuit_breaker, "is_open", lambda k: True)
    for _ in range(4):
        cands = proxy.pick_available_models(providers, "g", routers, strategy="fastest")
        assert set(_names(cands)) == {"A-a1", "B-b1"}


def test_round_robin_disabled_model_excluded(no_breaker):
    providers = [_provider("A", ["a1", "a2"], disabled=["a2"])]
    routers = {"g": ["A-a1", "A-a2"]}
    cands = proxy.pick_available_models(providers, "g", routers)
    assert _names(cands) == ["A-a1"]


def test_round_robin_can_be_disabled(no_breaker, monkeypatch):
    providers = [_provider("A", ["a1", "a2", "a3"])]
    routers = {"g": ["A-a1", "A-a2", "A-a3"]}
    monkeypatch.setattr(proxy.settings, "ROUND_ROBIN_ENABLED", False)
    firsts = [proxy.pick_available_models(providers, "g", routers, strategy="fastest")[0][1]
              for _ in range(5)]
    assert len(set(firsts)) == 1  # 关闭轮询后恒为同一候选(原排序行为)