# ============================================================
# 模型上下文长度解析测试(平台前缀/大小写兜底)
# ============================================================
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.core.proxy import get_context_length


def test_glm_5_2_all_platforms():
    """GLM-5.2 无论哪个平台写法都是 1M。"""
    for m in ("glm-5.2", "z-ai/glm-5.2", "ZhipuAI/GLM-5.2"):
        assert get_context_length(m) == 1048576


def test_size_insensitive_and_shortname():
    assert get_context_length("deepseek-ai/deepseek-v4-flash-0731") == 131072
    assert get_context_length("deepseek-v4-flash") == 131072
    assert get_context_length("DeepSeek-V4-Flash") == 131072  # AMD 写法


def test_unknown_falls_back_to_32768():
    assert get_context_length("PaddlePaddle/ERNIE-4.5-VL-28B-A3B-PT") == 32768
    assert get_context_length("unknown/model-x") == 32768
