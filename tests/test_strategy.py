# ============================================================
# 路由策略测试 — fastest / stable / quality
# ============================================================
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


class FakeProvider:
    def __init__(self, name, models, disabled_models=None):
        self.id = abs(hash(name)) % 1000
        self.name = name
        self.models = models
        self.disabled_models = disabled_models or []


def make_router_models(names):
    return [(p, m) for p, m in names]


class TestRoutingStrategy:
    """路由组策略排序 — fastest/stable/quality"""

    def _build_env(self):
        from backend.core.scheduler import quality_tracker
        quality_tracker.reset_all()

        # NVIDIA 平均延迟快但成功率低
        nvidia = FakeProvider("NVIDIA", ["model-a", "model-b"])
        # SenseNova 平均延迟慢但成功率高
        sense = FakeProvider("SenseNova", ["model-a"])
        # 魔搭 延迟中等
        moda = FakeProvider("魔搭", ["model-c"])

        providers = [nvidia, sense, moda]
        routers = {
            "test": ["NVIDIA-model-a", "NVIDIA-model-b", "SenseNova-model-a", "魔搭-model-c"]
        }
        return providers, routers

    def test_quality_strategy_sorts_by_score(self):
        """quality 策略: 高成功率排前面"""
        from backend.core.scheduler import quality_tracker
        providers, routers = self._build_env()

        # 记录历史: NVIDIA 多次失败, SenseNova 全成功
        for _ in range(5):
            quality_tracker.record("NVIDIA||model-a", "fail", 100)
        for _ in range(5):
            quality_tracker.record("SenseNova||model-a", "ok", 500)

        from backend.core.proxy import pick_available_models
        picks = pick_available_models(providers, "test", routers, strategy="quality")
        # SenseNova 应该排第一(成功率高)
        assert picks[0][0].name == "SenseNova"

    def test_fastest_strategy_sorts_by_latency(self):
        """fastest 策略: 低延迟排前面"""
        from backend.core.scheduler import quality_tracker
        providers, routers = self._build_env()

        # NVIDIA 延迟低(100ms), SenseNova 延迟高(900ms)
        quality_tracker.record("NVIDIA||model-a", "ok", 100)
        quality_tracker.record("NVIDIA||model-a", "ok", 120)
        quality_tracker.record("SenseNova||model-a", "ok", 900)
        quality_tracker.record("SenseNova||model-a", "ok", 800)

        from backend.core.proxy import pick_available_models
        picks = pick_available_models(providers, "test", routers, strategy="fastest")
        # NVIDIA 应该排第一(延迟低)
        assert picks[0][0].name == "NVIDIA"

    def test_stable_strategy_sorts_by_success_rate_then_latency(self):
        """stable 策略: 成功率最高的优先,同分比延迟"""
        from backend.core.scheduler import quality_tracker
        providers, routers = self._build_env()

        # NVIDIA 成功率高但慢, 魔搭成功率低但快
        for _ in range(4):
            quality_tracker.record("NVIDIA||model-a", "ok", 800)
        for _ in range(4):
            quality_tracker.record("魔搭||model-c", "fail", 100)

        from backend.core.proxy import pick_available_models
        picks = pick_available_models(providers, "test", routers, strategy="stable")
        # NVIDIA 排第一(成功率高)
        assert picks[0][0].name == "NVIDIA"

    def test_unknown_strategy_falls_back_to_quality(self):
        """未知策略回退到 quality"""
        from backend.core.scheduler import quality_tracker
        providers, routers = self._build_env()

        quality_tracker.record("SenseNova||model-a", "ok", 500)
        quality_tracker.record("NVIDIA||model-a", "fail", 100)

        from backend.core.proxy import pick_available_models
        picks = pick_available_models(providers, "test", routers, strategy="unknown")
        # 回退 quality: SenseNova(成功) 排第一
        assert picks[0][0].name == "SenseNova"