# ============================================================
# 智能网关 — 聊天记录
# 记录每次 /v1/chat/completions 请求的输入全文、输出全文、字符长度
# 写入失败绝不影响主链路(与 save_trace 同风格)
# ============================================================

import json
import logging
import time
from contextvars import ContextVar
from typing import Optional

logger = logging.getLogger("gateway.chat_log")

# 速度探测等系统内部请求标记: 置位后 save_chat_log 直接跳过,
# 避免测速的 "hi" 探测污染聊天记录(链路 trace 里仍会以 request_type=speed_test 记录)
_probe_mode: ContextVar = ContextVar("gw_probe_mode", default=False)


def set_probe_mode(enabled: bool) -> None:
    """标记当前请求上下文为系统探测(不写聊天记录)"""
    _probe_mode.set(enabled)


def is_probe_mode() -> bool:
    return _probe_mode.get()


# ─────────────────────────────────────────────
# 文本提取
# ─────────────────────────────────────────────

def extract_prompt_text(body: dict) -> str:
    """把请求 body['messages'] 拼成可读全文。
    多模态: content 是 list 时,提取其中的 text 部分,跳过 image; 否则原样取 str。
    返回形如:
        [system] ...\\n[user] ...\\n[assistant] ...
    """
    try:
        messages = body.get("messages") or []
        parts = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content")
            text = _content_to_text(content)
            if text is None:
                continue
            parts.append(f"[{role}] {text}")
        return "\n".join(parts)
    except Exception:
        return ""


def _content_to_text(content) -> Optional[str]:
    """OpenAI 多模态 content 支持字符串或 list-of-parts; 提取纯文本部分。"""
    if content is None:
        return None
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, dict):
                # {"type":"text","text":"..."}
                if part.get("type") == "text" and part.get("text"):
                    texts.append(str(part.get("text")))
                # {"type":"image_url", ...} -> 跳过
            elif isinstance(part, str):
                texts.append(part)
        return "\n".join(texts) if texts else None
    return str(content) or None


def extract_completion_text(resp: dict) -> str:
    """从非流式响应取回复全文 choices[0].message.content"""
    try:
        choices = resp.get("choices") or []
        if not choices:
            return ""
        return str(choices[0].get("message", {}).get("content", "") or "")
    except Exception:
        return ""


def extract_delta_text(chunk_bytes: bytes) -> str:
    """从流式 chunk 字节里提取 delta.content 文本(用于流式拼接)。
    chunk 形如 b'data: {...}\\n\\n' 或 b'data: [DONE]\\n\\n'
    """
    try:
        if isinstance(chunk_bytes, bytes):
            line = chunk_bytes.decode("utf-8", errors="ignore")
        else:
            line = str(chunk_bytes)
        if not line.startswith("data: "):
            return ""
        data_str = line.split("data: ", 1)[1].strip()
        if not data_str or data_str == "[DONE]":
            return ""
        obj = json.loads(data_str)
        choices = obj.get("choices") or []
        if not choices:
            return ""
        delta = choices[0].get("delta", {}) or {}
        return str(delta.get("content", "") or "")
    except Exception:
        return ""


# ─────────────────────────────────────────────
# 写库
# ─────────────────────────────────────────────

def save_chat_log(
    *,
    user_id: int,
    request_id: Optional[str] = None,
    original_model: str = "",
    provider_name: Optional[str] = None,
    model_name: Optional[str] = None,
    prompt_text: str = "",
    completion_text: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    is_stream: bool = False,
    is_pipeline: bool = False,
    pipeline_mode: Optional[str] = None,
    latency_ms: Optional[int] = None,
    success: bool = True,
    error_msg: Optional[str] = None,
):
    """保存聊天记录(非阻塞, 失败仅记日志; 速度探测请求自动跳过)"""
    if is_probe_mode():
        return None
    try:
        from backend.models import get_session, ChatLog
        pc = len(prompt_text or "")
        cc = len(completion_text or "")
        record = ChatLog(
            user_id=user_id,
            request_id=request_id,
            original_model=original_model or "",
            provider_name=provider_name,
            model_name=model_name,
            prompt_text=prompt_text,
            completion_text=completion_text,
            prompt_chars=pc,
            completion_chars=cc,
            total_chars=pc + cc,
            prompt_tokens=int(prompt_tokens or 0),
            completion_tokens=int(completion_tokens or 0),
            total_tokens=int((prompt_tokens or 0) + (completion_tokens or 0)),
            is_stream=is_stream,
            is_pipeline=is_pipeline,
            pipeline_mode=pipeline_mode,
            latency_ms=latency_ms,
            success=success,
            error_msg=error_msg,
        )
        session = get_session()
        try:
            session.add(record)
            session.commit()
        finally:
            session.close()
        return record
    except Exception:
        logger.exception("save chat_log failed")
        return None


class StreamRecorder:
    """包装流式生成器, 边 yield 给客户端边拼接 delta.content,
    生成器结束后调 save_chat_log 持久化。用法:

        rec = StreamRecorder(meta...)
        async for chunk in rec.record(primary_gen, on_done):
            yield chunk
    """
    def __init__(self, *, user_id, original_model, prompt_text,
                 provider_name=None, model_name=None, request_id=None,
                 is_pipeline=False, pipeline_mode=None, start_time=None):
        self.user_id = user_id
        self.original_model = original_model
        self.prompt_text = prompt_text
        self.provider_name = provider_name
        self.model_name = model_name
        self.request_id = request_id
        self.is_pipeline = is_pipeline
        self.pipeline_mode = pipeline_mode
        self.start_time = start_time or time.time()
        self._accum = []

    def _consume(self, chunk: bytes) -> bytes:
        """解析并累加文本, 返回原 chunk(透传给客户端)"""
        try:
            txt = extract_delta_text(chunk)
            if txt:
                self._accum.append(txt)
        except Exception:
            pass
        return chunk

    async def record(self, gen):
        """异步迭代 gen, yield 每个 chunk 并累加文本; 正常结束时保存成功记录。"""
        success = True
        error_msg = None
        try:
            async for chunk in gen:
                yield self._consume(chunk)
        except Exception as e:
            success = False
            error_msg = str(e)
            logger.warning("chat_log stream record error: %s", e)
        finally:
            latency = int((time.time() - self.start_time) * 1000)
            save_chat_log(
                user_id=self.user_id,
                request_id=self.request_id,
                original_model=self.original_model,
                provider_name=self.provider_name,
                model_name=self.model_name,
                prompt_text=self.prompt_text,
                completion_text="".join(self._accum),
                is_stream=True,
                is_pipeline=self.is_pipeline,
                pipeline_mode=self.pipeline_mode,
                latency_ms=latency,
                success=success,
                error_msg=error_msg,
            )
