# 自动图片识别路由 — 纯逻辑单测
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.core import vision
from backend.config import settings


def test_detect_images_pure_text():
    body = {"messages": [{"role": "user", "content": "你好，介绍一下自己"}]}
    assert vision.detect_images(body) is False


def test_detect_images_openai_image_url():
    body = {"messages": [{
        "role": "user",
        "content": [
            {"type": "text", "text": "这是什么"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }]}
    assert vision.detect_images(body) is True


def test_detect_images_claude_base64():
    body = {"messages": [{
        "role": "user",
        "content": [
            {"type": "text", "text": "看图"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
        ],
    }]}
    assert vision.detect_images(body) is True


def test_detect_images_no_messages():
    assert vision.detect_images({"model": "high_quality"}) is False
    assert vision.detect_images("not a dict") is False


def test_strip_images_keeps_text_only():
    msgs = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": [
            {"type": "text", "text": "描述这张图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]},
    ]
    out = vision.strip_images(msgs)
    assert out[0]["content"] == "你是助手"
    assert out[1]["role"] == "user"
    assert "image" not in out[1]["content"].lower()
    assert "描述这张图" in out[1]["content"]


def test_strip_images_string_content_unchanged():
    msgs = [{"role": "user", "content": "纯文本"}]
    out = vision.strip_images(msgs)
    assert out[0]["content"] == "纯文本"


def test_is_vision_target():
    assert vision.is_vision_target("vision") is True
    assert vision.is_vision_target("识图") is True  # 中文别名 -> vision
    assert vision.is_vision_target("NVIDIA-meta/llama-3.2-11b-vision-instruct") is True
    assert vision.is_vision_target("high_quality") is False
    assert vision.is_vision_target("NVIDIA-deepseek-ai/deepseek-v4-flash-0731") is False
    assert vision.is_vision_target(None) is False
    assert vision.is_vision_target("") is False


def test_build_text_stage_body_injects_recognition():
    body = {"messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "这是什么?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]},
    ]}
    out = vision.build_text_stage_body(body, "一只橘猫坐在窗台")
    assert out["messages"][0]["role"] == "user"
    c = out["messages"][0]["content"]
    assert "[图片识别结果]" in c
    assert "一只橘猫坐在窗台" in c
    assert "这是什么?" in c
    # 图片部分已被剥离
    assert "image_url" not in c.lower()


def test_build_vision_stage_body_slims_history():
    """超大 system/history 必须被丢弃, 只留最后带图消息 + 最后 user 提问"""
    body = {
        "model": "high_quality",
        "max_tokens": 65536,
        "stream": True,
        "tools": [{"type": "function", "function": {"name": "f"}}],
        "tool_choice": "auto",
        "messages": [
            {"role": "system", "content": "x" * 200_000},  # Agent 人设巨块
            {"role": "user", "content": "旧消息, 与图无关"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "1"}, {"id": "2"},  # 历史并行 tool_calls
            ]},
            {"role": "user", "content": [
                {"type": "text", "text": "看下这张图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ]},
            {"role": "user", "content": "图里是什么?"},
        ],
    }
    out = vision.build_vision_stage_body(body)
    assert out["model"] == "high_quality"
    # 只保留最后带图消息 + 最后 user 提问, 系统/历史全丢
    assert len(out["messages"]) == 2
    texts = [m["content"] for m in out["messages"]]
    assert any(isinstance(c, list) and any("image_url" in p for p in c) for c in texts)
    assert "图里是什么?" in [c if isinstance(c, str) else "" for c in texts]
    assert "x" * 100 not in str(out["messages"])
    # 工具字段被剥掉, stream 强制关掉, max_tokens 钳制
    assert "tools" not in out and "tool_choice" not in out
    assert out["stream"] is False
    assert out["max_tokens"] == settings.VISION_MAX_OUTPUT_TOKENS
    # 任何残留消息都不允许带 tool_calls(视觉模型单 tool-call 限制)
    for m in out["messages"]:
        assert "tool_calls" not in m


def test_build_vision_stage_body_keeps_image_message_when_last():
    """带图消息本身就是最后一条 user 消息时只保留一条"""
    body = {"messages": [
        {"role": "system", "content": "人设" * 1000},
        {"role": "user", "content": [
            {"type": "text", "text": "这是什么?"},
            {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
        ]},
    ]}
    out = vision.build_vision_stage_body(body)
    assert len(out["messages"]) == 1
    c = out["messages"][0]["content"]
    assert any("image_url" in p for p in c)
    assert out["max_tokens"] == settings.VISION_MAX_OUTPUT_TOKENS


def test_build_vision_stage_body_does_not_truncate_inline_base64():
    """整段 base64 图片字符串不能被截断"""
    b64 = "data:image/png;base64," + "A" * 100_000
    body = {"messages": [{"role": "user", "content": b64}]}
    out = vision.build_vision_stage_body(body)
    assert out["messages"][0]["content"] == b64


def test_build_vision_stage_body_limits_text_blob():
    """同一消息里夹带的巨型文本会被限长, 图片部分保留"""
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "哈" * 50_000},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}]}
    out = vision.build_vision_stage_body(body)
    parts = out["messages"][0]["content"]
    text_part = next(p for p in parts if p.get("type") == "text")
    assert len(text_part["text"]) < 5000
    assert any("image_url" in p for p in parts)
