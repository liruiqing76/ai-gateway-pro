#!/usr/bin/env python3
# ============================================================
# 路由组 — 简单 / 高质量 / 图片识别 三分类
# 参照 model-gateway 的 ROUTERS 机制:
#   客户端 model 字段填组名即命中, 组内按可用率/延迟择优
#   未命中的模型请求 → 具体模型直连
# ============================================================
import re

# ── 内置默认路由组 ──
# key: 组名（客户端用 model=quick / high_quality / vision 调用）
# value: 模型匹配模式列表（正则, 匹配上游模型 ID）
DEFAULT_ROUTER_GROUPS = {
    "quick": {
        "description": "简单模型 — 快速响应, 轻量任务",
        "patterns": [
            r"(?i)qwen3?-?8b",        # Qwen3-8B 系列
            r"(?i)qwen.*72b",          # Qwen 轻量
            r"(?i)llama-?3\.?2.*3b",   # LLaMA 3.2 3B
            r"(?i)gemma.*2b",          # Gemma 2B
            r"(?i)glm-?4?-?flash",     # GLM 轻量
            r"(?i)deepseek.*lite",     # DeepSeek Lite
            r"(?i).*-8b",              # 通用 8B 模型
            r"(?i).*-7b",              # 通用 7B 模型
            r"(?i).*-3b",              # 通用 3B 模型
        ],
    },
    "high_quality": {
        "description": "高质量模型 — 复杂推理/创作",
        "patterns": [
            r"(?i)glm-?[45]",           # GLM-4/5 系列(不含 4v/5v 视觉)
            r"(?i)qwen.*(max|plus|3\.5|2507)",  # Qwen 强推理
            r"(?i)deepseek.*(v3|r1|reasoner)",  # DeepSeek 强推理
            r"(?i)claude",             # Claude
            r"(?i)gpt-4o?-?mini",      # GPT-4o mini (若启用)
            r"(?i)kimi",               # Kimi
            r"(?i)f1|f2",              # F1/F2 推理
        ],
    },
    "vision": {
        "description": "图片识别模型 — 多模态视觉",
        "patterns": [
            r"(?i)(qwen.*vl|qvq)",     # Qwen-VL
            r"(?i)glm-?4?v",           # GLM-4V
            r"(?i)gpt-4[o]?(mini)?-vision",  # GPT-4o
            r"(?i)internvl",           # InternVL
            r"(?i)qwen2\.5-vl",
            r"(?i)siglip|clip",        # 视觉底座
            r"(?i).*vision",           # 通用 vision
            r"(?i).*-vl-",             # 通用 VL
        ],
    },
}


# ── 组名中英文别名(参照 D:\AI模型网关: 客户端填中文组名即可调用) ──
GROUP_ALIASES = {
    "快速": "quick",
    "日常": "high_quality",
    "识图": "vision",
}
# 反向: 英文 -> 中文(前端展示用)
GROUP_LABELS = {v: k for k, v in GROUP_ALIASES.items()}


def normalize_group_name(name: str) -> str:
    """中文组名归一化为英文键; 已是英文则原样返回"""
    return GROUP_ALIASES.get(name, name)


def classify_model(model: str) -> str:
    """将模型 ID 分类到默认组: quick / high_quality / vision / other
    顺序: vision 优先 (VL/vision 关键字), 再 high_quality, 再 quick
    """
    # vision 组优先 — VL/vision 模型不能误分到推理组
    for pattern in DEFAULT_ROUTER_GROUPS["vision"]["patterns"]:
        if re.search(pattern, model):
            return "vision"

    for pattern in DEFAULT_ROUTER_GROUPS["high_quality"]["patterns"]:
        if re.search(pattern, model):
            return "high_quality"

    for pattern in DEFAULT_ROUTER_GROUPS["quick"]["patterns"]:
        if re.search(pattern, model):
            return "quick"

    return "other"


def build_router_groups(providers) -> dict:
    """
    从渠道列表构建路由组。
    providers: 每个 has .name, .models (JSON string list), .disabled_models (JSON string list)
    返回: {组名: [带渠道前缀的模型ID, ...]}
    例: NVIDIA-z-ai/glm-5.2
    """
    groups = {name: [] for name in DEFAULT_ROUTER_GROUPS}
    for p in providers:
        models = p.models or []
        disabled = p.disabled_models or []
        if isinstance(models, str):
            import json
            models = json.loads(models)
        if isinstance(disabled, str):
            import json
            disabled = json.loads(disabled)
        for m in models:
            if m in disabled:
                continue
            cls = classify_model(m)
            prefixed = f"{p.name}-{m}"
            if cls in groups:
                groups[cls].append(prefixed)
    return groups


def resolve_router_group(model: str, router_groups: dict) -> list:
    """请求 model 是组名(中/英文)时, 返回组内模型列表; 否则 None"""
    return router_groups.get(normalize_group_name(model))