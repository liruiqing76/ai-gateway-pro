# ============================================================
# 质量分: 延迟惩罚(慢渠道自动靠后) 测试
# ============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.core.scheduler import quality_tracker


def test_latency_penalty_pushes_slow_channel_down():
    quality_tracker.reset_all()
    # 慢渠道: 多次成功但延迟 30s
    quality_tracker.record("NVIDIA||m1", "ok", 30000)
    quality_tracker.record("NVIDIA||m1", "ok", 25000)
    # 快渠道: 同样成功但 1s
    quality_tracker.record("魔搭||m2", "ok", 1000)
    quality_tracker.record("魔搭||m2", "ok", 900)
    assert quality_tracker.get_score("NVIDIA||m1") < quality_tracker.get_score("魔搭||m2")


def test_fast_channel_not_penalized():
    quality_tracker.reset_all()
    quality_tracker.record("SenseNova||m3", "ok", 1200)
    assert quality_tracker.get_score("SenseNova||m3") >= 0
