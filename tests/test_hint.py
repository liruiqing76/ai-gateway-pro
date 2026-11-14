"""「无可用的模型」解释器测试。

线上体验: opencode 全部模型被上游 429 后, 用户看到的是
`无可用的模型: opencode-nemotron-3-ultra-free` —— 分不清是额度、冷却还是名字写错。
hint.unavailable_reason 负责补一句人话, 且**只解释不改选路**。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from backend.core import hint


def prov(name, models, pid=1, enabled=True):
    return SimpleNamespace(id=pid, name=name, models=list(models), enabled=enabled)


@pytest.fixture
def cooling(monkeypatch):
    """把某 provider id 设为冷却中(打桩在 hint 引用的 circuit_breaker 实例上)。"""
    state = {"ids": {}, "secs": 42}

    monkeypatch.setattr(hint.circuit_breaker, "provider_in_cooldown",
                        lambda pid: pid in state["ids"], raising=True)
    monkeypatch.setattr(hint.circuit_breaker, "provider_cooldown_seconds",
                        lambda pid: state["secs"], raising=True)
    return state


def test_cooldown_explained(cooling):
    cooling["ids"][7] = True
    ps = [prov("opencode", ["glm-5.3-flash-free"], pid=7)]
    msg = hint.unavailable_reason(ps, "opencode-glm-5.3-flash-free")
    assert "冷却中" in msg and "42" in msg


def test_wrong_model_name_lists_samples(cooling):
    ps = [prov("opencode", ["a-free", "b-free", "c-free"], pid=1)]
    msg = hint.unavailable_reason(ps, "opencode-not-exist")
    assert "没有模型 not-exist" in msg
    assert "a-free" in msg


def test_disabled_channel(cooling):
    ps = [prov("trae", ["glm-5.2"], pid=2, enabled=False)]
    assert "已停用" in hint.unavailable_reason(ps, "trae-glm-5.2")


def test_longest_prefix_wins(cooling):
    """qoder- 不能抢 qodercn- 的模型: 两个渠道名字互相是前缀。"""
    cooling["ids"][3] = True
    ps = [prov("qoder", ["qfmodel"], pid=2),
          prov("qodercn", ["qfmodel"], pid=3)]
    msg = hint.unavailable_reason(ps, "qodercn-qfmodel")
    assert "qodercn" in msg and "冷却中" in msg


def test_group_name_alone(cooling):
    cooling["ids"][5] = True
    ps = [prov("kilo", ["kilo-auto/free"], pid=5)]
    msg = hint.unavailable_reason(ps, "kilo")
    assert "冷却中" in msg


def test_no_requested_model_returns_empty(cooling):
    cooling["ids"][1] = True
    assert hint.unavailable_reason([prov("opencode", ["x"], pid=1)], "") == ""


def test_unrelated_model_reports_cooling_channels(cooling):
    cooling["ids"][9] = True
    ps = [prov("opencode", ["x"], pid=9), prov("workbuddy", ["y"], pid=10)]
    msg = hint.unavailable_reason(ps, "some-dead-model")
    assert "opencode" in msg and "冷却" in msg


def test_unknown_model_no_cooling_is_quiet(cooling):
    ps = [prov("workbuddy", ["y"], pid=10)]
    assert hint.unavailable_reason(ps, "whatever") == ""


def test_empty_providers(cooling):
    assert hint.unavailable_reason([], "opencode-x") == ""


def test_no_providers_attribute_no_crash(cooling):
    """Provider 对象字段缺失时不能抛(选路主流程不能被解释器拖挂)。"""
    ps = [SimpleNamespace(name="opencode")]
    assert isinstance(hint.unavailable_reason(ps, "opencode-x"), str)


def test_hint_wired_into_three_surfaces():
    """三个协议面都要带上解释后缀, 否则用户换个工具又被这句干话糊住。"""
    root = Path(__file__).parent.parent
    for f in ("backend/api/proxy.py", "backend/api/anthropic.py", "backend/api/responses.py"):
        src = (root / f).read_text(encoding="utf-8")
        assert "unavailable_reason(providers" in src, f"{f} 未接入 hint"
