# ============================================================
# 智能网关 — 自动图片识别路由 (纯函数)
# 文本模型请求带图片时: 先视觉组识别 -> 再把结果拼回文本模型
# 这里只放与请求体相关、无副作用的纯逻辑, 避免与代理层循环依赖
# ============================================================

import json
import logging

from backend.core.router_group import classify_model, normalize_group_name

logger = logging.getLogger("gateway.vision")


def detect_images(body: dict) -> bool:
    """检测请求体是否包含图片内容(多模态)

    兼容常见格式:
      - OpenAI: content 数组里 {type:"image_url", image_url:{url:...}}
      - Claude:  content 数组里 {type:"image", source:{type:"base64",...}}
      - 内联 base64 文本块等
    """
    if not isinstance(body, dict):
        return False
    messages = body.get("messages")
    if not isinstance(messages, list):
        return False
    for m in messages:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, list):
            for part in c:
                if not isinstance(part, dict):
                    continue
                t = part.get("type", "")
                if isinstance(t, str) and "image" in t.lower():
                    return True
                if "image_url" in part:
                    return True
                src = part.get("source")
                if isinstance(src, dict) and src.get("type") == "base64":
                    return True
        elif isinstance(c, str):
            if "data:image/" in c:
                return True
    return False


def strip_images(messages: list) -> list:
    """去掉所有图片部分, 仅保留文本, 返回新的 messages 列表"""
    out = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        c = m.get("content")
        if isinstance(c, list):
            texts = []
            for part in c:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        texts.append(str(part.get("text", "")))
                elif isinstance(part, str):
                    texts.append(part)
            text = "\n".join(t for t in texts if t).strip()
        elif isinstance(c, str):
            text = c
        else:
            text = ""
        new_m = {k: v for k, v in m.items() if k != "content"}
        new_m["content"] = text
        out.append(new_m)
    return out


def is_vision_target(model) -> bool:
    """请求目标本身是否为视觉模型/视觉组

    命中则不触发自动识别管线, 原样转发(避免重复处理视觉模型)。
    """
    if not model:
        return False
    s = str(model)
    if normalize_group_name(s) == "vision":
        return True
    if classify_model(s) == "vision":
        return True
    return False


def build_text_stage_body(body: dict, vision_text: str) -> dict:
    """构造文本模型阶段的请求体: 剥离图片, 把识别结果注入最后一条 user 消息"""
    nb = json.loads(json.dumps(body))
    messages = strip_images(body.get("messages", []))
    note = "\n\n[图片识别结果]\n" + (vision_text or "") + \
           "\n\n请基于以上图片识别内容, 回答我的问题。"
    last_user = None
    for i, m in enumerate(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            last_user = i
    if last_user is not None:
        base = messages[last_user].get("content") or ""
        if isinstance(base, str):
            messages[last_user]["content"] = base + note
        else:
            messages[last_user]["content"] = str(base) + note
    else:
        messages.append({"role": "user", "content": note})
    nb["messages"] = messages
    return nb
