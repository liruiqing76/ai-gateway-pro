# ============================================================
# 质量分排序: 延迟惩罚 + 按模型排序(无记录继承渠道延迟) 测试
# ============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.core.scheduler import quality_tracker
from backend.core.proxy import _sort_by_strategy


def _mk(name, url=None):
    class P:
        def __init__(self, n):
            self.name = n
            self.id = hash(n) % 1000
    return P(name)


def test_latency_penalty_pushes_slow_channel_down():
    quality_tracker.reset_all()
    quality_tracker.record("NVIDIA||m1", "ok", 30000)
    quality_tracker.record("NVIDIA||m1", "ok", 25000)
    quality_tracker.record("魔搭||m2", "ok", 1000)
    quality_tracker.record("魔搭||m2", "ok", 900)
    assert quality_tracker.get_score("NVIDIA||m1") < quality_tracker.get_score("魔搭||m2")


def test_unrecorded_model_inherits_channel_latency():
    """无记录的模型按同渠道已知延迟估计: 慢渠道的新模型自动排后(按模型排序)。"""
    quality_tracker.reset_all()
    # 只有 魔搭 有记录(快), NVIDIA 渠道整体慢但 m1 有记录
    quality_tracker.record("魔搭||known", "ok", 1500)
    quality_tracker.record("NVIDIA||known", "ok", 25000)

    nv = _mk("NVIDIA")
    mo = _mk("魔搭")
    # 两个都是无记录的新模型
    ordered = _sort_by_strategy(
        [(nv, "new-a", "NVIDIA||new-a"), (mo, "new-b", "魔搭||new-b")], "fastest")
    assert ordered[0][1] == "new-b"   # 魔搭的新模型在前(快渠道)
    assert ordered[1][1] == "new-a"   # NVIDIA 新模型靠后(继承 25s 渠道延迟)


def test_fast_channel_not_penalized():
    quality_tracker.reset_all()
    quality_tracker.record("SenseNova||m3", "ok", 1200)
    assert quality_tracker.get_score("SenseNova||m3") >= 0
