# ============================================================
# 智能网关 — 多模型编排引擎
# relay(接力) / review(审阅)
# model 命名: relay:组A/组B/组C  |  review:主/审
# ============================================================

import json
import logging
from typing import Optional, Callable, Awaitable

logger = logging.getLogger("gateway.orchestrate")

MAX_RELAY_STAGES = 5

ForwardFn = Callable[..., Awaitable[Optional[dict]]]


def parse_pipeline(model: str) -> Optional[tuple[str, list[str]]]:
    """解析编排格式的 model 名。返回 (mode, stages) 或 None。"""
    if not model or ":" not in model:
        return None
    mode_raw, _, stages_raw = model.partition(":")
    mode = mode_raw.strip().lower()
    if mode not in ("relay", "review"):
        return None
    stages = [s.strip() for s in stages_raw.split("/") if s.strip()]
    if not stages:
        return None
    if mode == "relay":
        if len(stages) < 2:
            return None
        if len(stages) > MAX_RELAY_STAGES:
            return None
    elif mode == "review":
        if len(stages) != 2:
            return None
    return mode, stages


def _extract_text(resp: dict | None) -> str:
    if not resp:
        return ""
    try:
        choices = resp.get("choices") or []
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content", "") or ""
    except Exception:
        return ""


def _get_prompt(body: dict) -> str:
    try:
        messages = body.get("messages") or []
        for m in messages:
            if m.get("role") == "user":
                return str(m.get("content", ""))
        return ""
    except Exception:
        return ""


def _set_prompt(body: dict, prompt: str) -> list:
    """构造新 messages, 保留 system 前缀"""
    messages = body.get("messages") or []
    new_messages = []
    for m in messages:
        if m.get("role") == "system":
            new_messages.append(m)
    new_messages.append({"role": "user", "content": prompt})
    return new_messages


def _context_handoff(messages: list, stage_index: int, total_stages: int, last_output: str = "") -> list:
    """为非首级模型注入上下文交接 system 消息，防止换模型失忆"""
    has_handoff = any(
        m.get("role") == "system" and "继续之前的对话" in str(m.get("content", ""))
        for m in messages
    )
    if has_handoff:
        return messages
    new_msgs = list(messages)
    handoff_msg = {
        "role": "system",
        "content": (
            f"你正在以接力({stage_index+1}/{total_stages})的方式继续之前的对话。"
            f"上一步的输出仅供参考。请基于用户原始问题继续处理。"
        ),
    }
    last_sys = -1
    for k, m in enumerate(new_msgs):
        if m.get("role") == "system":
            last_sys = k
    new_msgs.insert(last_sys + 1, handoff_msg)
    return new_msgs


async def run_relay(
    stages: list[str],
    body: dict,
    forward: ForwardFn,
    final_model: str = "",
) -> dict | None:
    """接力编排: 第一级原始 prompt → 后续每级注入上一步输出 + 上下文交接"""
    original_prompt = _get_prompt(body)
    if not original_prompt:
        return None
    last_text = ""
    last_resp = None
    for i, stage in enumerate(stages):
        stage_body = json.loads(json.dumps(body))
        if i == 0:
            stage_body["messages"] = _set_prompt(body, original_prompt)
        else:
            combined = (
                f"{original_prompt}\n\n"
                f"[上一级输出]\n{last_text}\n\n"
                f"请基于以上内容继续处理, 输出最终结果:"
            )
            base_msgs = _set_prompt(body, combined)
            stage_body["messages"] = _context_handoff(base_msgs, i, len(stages), last_text)
        resp = await forward(stage, stage_body)
        if resp is None:
            logger.warning("relay stage %s failed, skipping", stage)
            continue
        last_text = _extract_text(resp)
        if last_text:
            last_resp = resp
        else:
            logger.warning("relay stage %s returned empty, skipping", stage)
    return last_resp


async def run_review(
    stages: list[str],
    body: dict,
    forward: ForwardFn,
    final_model: str = "",
) -> dict | None:
    """审阅编排: 主模型生成初稿 → 审阅模型修订 + 上下文交接"""
    if len(stages) != 2:
        return None
    main_stage, review_stage = stages
    original_prompt = _get_prompt(body)
    if not original_prompt:
        return None
    draft_resp = await forward(main_stage, body)
    if draft_resp is None:
        logger.warning("review main stage failed")
        return None
    draft_text = _extract_text(draft_resp)
    if not draft_text:
        logger.warning("review main stage returned empty")
        return None
    review_body = json.loads(json.dumps(body))
    review_prompt = (
        f"{original_prompt}\n\n"
        f"[初稿]\n{draft_text}\n\n"
        f"请审阅以上初稿, 检查错误/遗漏/表达, 输出修订后的最终版本:"
    )
    base_msgs = _set_prompt(body, review_prompt)
    review_body["messages"] = _context_handoff(base_msgs, 1, 2, draft_text)
    review_resp = await forward(review_stage, review_body)
    if review_resp is None:
        logger.warning("review stage failed, return draft")
        return draft_resp
    review_text = _extract_text(review_resp)
    if review_text:
        return review_resp
    logger.warning("review stage returned empty, return draft")
    return draft_resp