# ============================================================
# 路由组测试 — 简单/高质量/图片识别三分类
# 参照 model-gateway 的 ROUTERS 机制
# ============================================================
import sys, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.core.router_group import (
    DEFAULT_ROUTER_GROUPS,
    classify_model,
    build_router_groups,
    resolve_router_group,
)


class FakeProvider:
    def __init__(self, name, models, disabled_models=None):
        self.name = name
        self.models = models
        self.disabled_models = disabled_models or []

    def enabled_models(self):
        return [m for m in self.models if m not in self.disabled_models]


def test_default_groups_exist():
    """必须有 quick / high_quality / vision 三组"""
    assert "quick" in DEFAULT_ROUTER_GROUPS
    assert "high_quality" in DEFAULT_ROUTER_GROUPS
    assert "vision" in DEFAULT_ROUTER_GROUPS
    # 每组都要有成员
    for name, models in DEFAULT_ROUTER_GROUPS.items():
        assert models, f"{name} 组为空"


def test_classify_vision_model():
    """图片识别模型要能被识别"""
    assert classify_model("qwen-vl-plus") == "vision"
    assert classify_model("glm-4v") == "vision"
    assert classify_model("gpt-4o-mini") == "high_quality"  # 文本为主, 归高质量
    assert classify_model("gpt-4o-vision") == "vision"
    assert classify_model("internvl2") == "vision"


def test_classify_quality_model():
    """高质量推理模型"""
    assert classify_model("glm-4.6") == "high_quality"
    assert classify_model("qwen-max") == "high_quality"
    assert classify_model("deepseek-v3") == "high_quality"
    assert classify_model("qwen3-8b") == "quick"  # 小模型是简单


def test_build_groups_from_providers():
    """从渠道模型构建三组路由"""
    providers = [
        FakeProvider("NVIDIA", ["z-ai/glm-5.2", "qwen/qwen-vl-max"]),
        FakeProvider("魔搭", ["Qwen/Qwen3-8B-Instruct", "Qwen/Qwen2.5-VL"]),
    ]
    groups = build_router_groups(providers)
    assert "quick" in groups and "high_quality" in groups and "vision" in groups
    # vision 组应包含 qwen-vl
    assert any("vl" in m.lower() for m in groups["vision"])


def test_resolve_router_group():
    """请求 model 填组名时解析到组内模型"""
    groups = {
        "quick": ["Qwen/Qwen3-8B-Instruct"],
        "high_quality": ["z-ai/glm-5.2"],
        "vision": ["Qwen/Qwen2.5-VL"],
    }
    assert resolve_router_group("quick", groups) == ["Qwen/Qwen3-8B-Instruct"]
    assert resolve_router_group("high_quality", groups) == ["z-ai/glm-5.2"]
    assert resolve_router_group("vision", groups) == ["Qwen/Qwen2.5-VL"]
    # 不在组里 → None（走具体模型逻辑）
    assert resolve_router_group("unknown-group", groups) is None