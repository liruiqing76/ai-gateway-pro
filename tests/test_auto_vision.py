# 自动图片识别路由 — 纯逻辑单测
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.core import vision


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
