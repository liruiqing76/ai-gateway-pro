# ============================================================
# 智能网关 — 自动图片识别路由 (纯函数)
# 文本模型请求带图片时: 先视觉组识别 -> 再把结果拼回文本模型
# 这里只放与请求体相关、无副作用的纯逻辑, 避免与代理层循环依赖
# ============================================================

import json
import logging

from backend.core.router_group import classify_model, normalize_group_name
from backend.config import settings

logger = logging.getLogger("gateway.vision")


# 视觉内容部件类型的白名单(避免任意 type 含 "image" 就误判)
_IMAGE_PART_TYPES = ("image_url", "input_image", "image")


def _message_has_image(m) -> bool:
    """单条消息是否包含真正的图片内容(多模态)。

    注意: 字符串内容必须以 data:image/ 开头才算内联图片 —— 文本里"提到"
    data:image 子串(如 Agent 读到的处理图片的源码)不是图, 不能算。
    """
    if not isinstance(m, dict):
        return False
    c = m.get("content")
    if isinstance(c, list):
        for part in c:
            if not isinstance(part, dict):
                continue
            t = str(part.get("type", "")).lower()
            if t in _IMAGE_PART_TYPES:
                return True
            if "image_url" in part:
                return True
            src = part.get("source")
            if isinstance(src, dict) and src.get("type") == "base64":
                return True
    elif isinstance(c, str):
        if c.strip().startswith("data:image/"):
            return True
    return False


def detect_images(body: dict) -> bool:
    """检测请求是否需要走视觉管道。

    只看【最后一条 user 消息】: Agent(Hermes 等)的长对话历史一旦出现过图片
    或图片相关字样就会永远留在上下文里, 按全历史检测会让后续每次纯文本请求
    都被误判带图、被强制塞进视觉管道(实测: 历史里的 JS 源码含 'data:image/'
    字样导致连续几十次纯文本请求全走了 vision)。需要处理历史图片的场景请
    直接指定 vision 组模型。
    """
    if not isinstance(body, dict):
        return False
    messages = body.get("messages")
    if not isinstance(messages, list):
        return False
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            return _message_has_image(m)
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


# 单条消息里非图片文本的保留上限(字符): 防止某条消息里夹带巨型文本
_TEXT_KEEP_CHARS = 4000


def _limit_text_content(content, limit: int = _TEXT_KEEP_CHARS):
    """压缩消息里的纯文本部分, 图片部分(内联 base64/URL)原样保留"""
    if isinstance(content, list):
        out = []
        budget = limit
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                t = str(part.get("text", ""))
                if len(t) > budget:
                    part = {**part, "text": t[:budget]}
                    budget = 0
                else:
                    budget -= len(t)
            out.append(part)
        return out
    if isinstance(content, str):
        # 纯字符串夹带整段 base64 图片时不能截断(会损坏图片)
        if "data:image/" in content:
            return content
        return content[:limit]
    return content


def build_vision_stage_body(body: dict, max_output_tokens: int | None = None) -> dict:
    """构造视觉识别子调用的请求体(瘦身版)。

    直接把完整对话原样发给视觉模型是性能灾难: Hermes 等 Agent 的 system
    人设/历史可达几十万字符(实测 26.8 万字符 ≈ 8.4 万 token), 而视觉模型
    上下文只有 16k-32k, 必然 400 超限, 且会在 3 轮 × N 候选的识别阶段
    烧掉 50~130 秒再降级。识别只需要"图 + 问题":

    - 丢弃 system 与全部历史消息(system 是给文本模型的 Agent 人设, 与识图无关)
    - 只保留最后一条带图消息 + 最后一条 user 消息(不同时)
    - 剥掉消息里的 tool_calls(NVIDIA 视觉模型只支持单 tool-call, 历史里的
      并行 tool_calls 会直接 400)
    - 纯文本部分限长(内联 base64 图片不受影响)
    - max_tokens 钳制到 VISION_MAX_OUTPUT_TOKENS, 避免 completion 侧超限
    """
    nb = json.loads(json.dumps(body))
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        msgs = []

    last_img_i = None
    last_user_i = None
    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            continue
        if _message_has_image(m):
            last_img_i = i
        if m.get("role") == "user":
            last_user_i = i

    keep_idx = {last_img_i} if last_img_i is not None else set()
    if last_user_i is not None and last_user_i not in keep_idx:
        keep_idx.add(last_user_i)
    if not keep_idx and msgs:
        keep_idx.add(len(msgs) - 1)  # 兜底: 都没有图/用户消息时保留最后一条

    slim = []
    for i in keep_idx:
        m = msgs[i]
        new_m = {k: v for k, v in m.items() if k not in ("tool_calls",)}
        new_m["content"] = _limit_text_content(m.get("content"))
        slim.append(new_m)
    nb["messages"] = slim

    # 请求级工具/输出相关字段: 视觉子调用只负责识图
    for _tk in ("tools", "tool_choice", "parallel_tool_calls",
                "functions", "function_call"):
        nb.pop(_tk, None)
    # 阶段1走非流式: 强制关掉流式标记, 否则上游按流式回 SSE 解析必失败
    nb["stream"] = False
    nb.pop("stream_options", None)
    cap = max_output_tokens or settings.VISION_MAX_OUTPUT_TOKENS
    nb["max_tokens"] = min(int(nb.get("max_tokens") or cap), cap)
    return nb


def build_text_stage_body(body: dict, vision_text: str) -> dict:
    """构造文本模型阶段的请求体: 剥离图片, 把识别结果注入最后一条 user 消息。
    vision_text 为空时(识别彻底失败的兜底降级), 注入失败说明而不是让请求挂掉。"""
    nb = json.loads(json.dumps(body))
    messages = strip_images(body.get("messages", []))
    if (vision_text or "").strip():
        note = "\n\n[图片识别结果]\n" + (vision_text or "").strip() + \
               "\n\n请基于以上图片识别内容, 回答我的问题。"
    else:
        note = ("\n\n[系统提示] 用户附带了图片, 但本次自动识图未成功。"
                "请向用户说明暂时无法识别该图片内容, 建议重新发送图片或改用文字描述。")
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
