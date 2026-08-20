# ============================================================
# 智能网关 — 多模型编排引擎
# relay(接力) / review(审阅)
# model 命名: relay:组A/组B/组C  |  review:主/审
# 支持非流式 + 最后一级流式输出
# ============================================================

import json
import logging
from typing import Optional, Callable, Awaitable, AsyncGenerator

logger = logging.getLogger("gateway.orchestrate")

MAX_RELAY_STAGES = 5

ForwardFn = Callable[..., Awaitable[Optional[dict]]]
ForwardStreamFn = Callable[..., Awaitable[tuple[Optional[AsyncGenerator[bytes, None]], Optional[str]]]]


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


# ============================================================
# 流式编排: 最后一级流式输出, 前面的级非流式
# ============================================================

def build_stage_body(
    body: dict,
    stage_index: int,
    total_stages: int,
    original_prompt: str,
    last_text: str = "",
) -> dict:
    """构造某一阶段的请求 body"""
    stage_body = json.loads(json.dumps(body))
    if stage_index == 0:
        stage_body["messages"] = _set_prompt(body, original_prompt)
    else:
        combined = (
            f"{original_prompt}\n\n"
            f"[上一级输出]\n{last_text}\n\n"
            f"请基于以上内容继续处理, 输出最终结果:"
        )
        base_msgs = _set_prompt(body, combined)
        stage_body["messages"] = _context_handoff(base_msgs, stage_index, total_stages, last_text)
    return stage_body


async def run_relay_stream(
    stages: list[str],
    body: dict,
    forward: ForwardFn,
    forward_stream: ForwardStreamFn,
) -> tuple[Optional[AsyncGenerator[bytes, None]], Optional[str]]:
    """
    流式接力编排: 前面的级非流式, 最后一级流式输出。

    返回 (generator, error) — 与 forward_stream 签名一致,
    可直接作为 StreamingResponse 的 body。
    """
    original_prompt = _get_prompt(body)
    if not original_prompt:
        return None, "需要 user 消息"

    last_text = ""
    last_resp = None

    # 前 N-1 级: 非流式
    for i in range(len(stages) - 1):
        stage = stages[i]
        stage_body = build_stage_body(body, i, len(stages), original_prompt, last_text)
        resp = await forward(stage, stage_body)
        if resp is None:
            logger.warning("relay stream stage %d (%s) failed, skipping", i, stage)
            continue
        last_text = _extract_text(resp)
        if last_text:
            last_resp = resp
        else:
            logger.warning("relay stream stage %d (%s) returned empty, skipping", i, stage)

    # 最后一级: 流式
    last_stage = stages[-1]
    last_index = len(stages) - 1
    stage_body = build_stage_body(body, last_index, len(stages), original_prompt, last_text)

    gen, err = await forward_stream(last_stage, stage_body)
    if gen is not None:
        return gen, None

    # 最后一级也失败了, 回退到非流式结果
    if last_resp is not None:
        logger.warning("relay stream last stage failed, returning non-stream result")
        return None, "last stage stream failed, use non-stream fallback"

    return None, err or "all relay stages failed"


async def run_review_stream(
    stages: list[str],
    body: dict,
    forward: ForwardFn,
    forward_stream: ForwardStreamFn,
) -> tuple[Optional[AsyncGenerator[bytes, None]], Optional[str]]:
    """
    流式审阅编排: 主模型非流式生成初稿 → 审阅模型流式修订。

    返回 (generator, error)。
    """
    if len(stages) != 2:
        return None, "review 需要恰好 2 个阶段"

    main_stage, review_stage = stages
    original_prompt = _get_prompt(body)
    if not original_prompt:
        return None, "需要 user 消息"

    # 主模型: 非流式
    draft_resp = await forward(main_stage, body)
    if draft_resp is None:
        return None, "主模型生成失败"
    draft_text = _extract_text(draft_resp)
    if not draft_text:
        return None, "主模型返回空内容"

    # 审阅模型: 流式
    review_body = json.loads(json.dumps(body))
    review_prompt = (
        f"{original_prompt}\n\n"
        f"[初稿]\n{draft_text}\n\n"
        f"请审阅以上初稿, 检查错误/遗漏/表达, 输出修订后的最终版本:"
    )
    base_msgs = _set_prompt(body, review_prompt)
    review_body["messages"] = _context_handoff(base_msgs, 1, 2, draft_text)

    gen, err = await forward_stream(review_stage, review_body)
    if gen is not None:
        return gen, None

    # 审阅流式失败, 回退返回初稿
    logger.warning("review stream stage failed, returning draft as non-stream")
    return None, "review stream failed"