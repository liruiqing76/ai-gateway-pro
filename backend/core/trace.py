# ============================================================
# 智能网关 — 请求链路追踪
# 记录每次请求的完整决策链路
# ============================================================

import uuid
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("gateway.trace")


@dataclass
class TraceContext:
    """单次请求的追踪上下文"""
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    user_id: int = 0

    # 请求输入
    original_model: str = ""
    resolved_model: str = ""
    is_stream: bool = False
    is_pipeline: bool = False
    pipeline_mode: str = ""

    # 实际选择
    provider_name: str = ""
    model_name: str = ""
    selection_strategy: str = ""
    selection_reason: str = ""

    # 熔断/冷却
    circuit_breaker_hit: bool = False
    rate_limit_hit: bool = False
    cooldown_seconds: int = 0

    # 性能
    latency_ms: int = 0
    success: bool = True
    error_msg: str = ""

    # Token 用量
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    # 编排阶段
    stages_detail: list = field(default_factory=list)

    # 内部计时
    _start_time: float = field(default_factory=time.time)

    def mark_selected(self, provider_name: str, model_name: str, reason: str):
        """记录模型选择"""
        self.provider_name = provider_name
        self.model_name = model_name
        self.selection_reason = reason

    def mark_circuit_breaker(self, hit: bool):
        self.circuit_breaker_hit = hit

    def mark_rate_limit(self, hit: bool, cooldown: int = 0):
        self.rate_limit_hit = hit
        self.cooldown_seconds = cooldown

    def mark_error(self, msg: str):
        self.success = False
        self.error_msg = msg

    def mark_tokens(self, prompt: int, completion: int):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = prompt + completion

    def add_stage(self, stage_index: int, provider: str, model: str,
                  latency_ms: int, success: bool, error: str = ""):
        """记录编排阶段详情"""
        self.stages_detail.append({
            "stage": stage_index,
            "provider": provider,
            "model": model,
            "latency_ms": latency_ms,
            "success": success,
            "error": error,
        })

    def finish(self):
        """完成追踪,计算总延迟"""
        self.latency_ms = int((time.time() - self._start_time) * 1000)

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "user_id": self.user_id,
            "original_model": self.original_model,
            "resolved_model": self.resolved_model,
            "is_stream": self.is_stream,
            "is_pipeline": self.is_pipeline,
            "pipeline_mode": self.pipeline_mode,
            "provider_name": self.provider_name,
            "model_name": self.model_name,
            "selection_strategy": self.selection_strategy,
            "selection_reason": self.selection_reason,
            "circuit_breaker_hit": self.circuit_breaker_hit,
            "rate_limit_hit": self.rate_limit_hit,
            "cooldown_seconds": self.cooldown_seconds,
            "latency_ms": self.latency_ms,
            "success": self.success,
            "error_msg": self.error_msg,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "stages_detail": self.stages_detail if self.stages_detail else None,
        }


def save_trace(trace: TraceContext):
    """异步保存 trace 到数据库(非阻塞)"""
    try:
        from backend.models import get_session, RequestTrace
        session = get_session()
        try:
            record = RequestTrace(
                user_id=trace.user_id,
                request_id=trace.request_id,
                original_model=trace.original_model,
                resolved_model=trace.resolved_model,
                is_stream=trace.is_stream,
                is_pipeline=trace.is_pipeline,
                pipeline_mode=trace.pipeline_mode or None,
                provider_name=trace.provider_name or None,
                model_name=trace.model_name or None,
                selection_strategy=trace.selection_strategy or None,
                selection_reason=trace.selection_reason or None,
                circuit_breaker_hit=trace.circuit_breaker_hit,
                rate_limit_hit=trace.rate_limit_hit,
                cooldown_seconds=trace.cooldown_seconds or None,
                latency_ms=trace.latency_ms,
                success=trace.success,
                error_msg=trace.error_msg or None,
                prompt_tokens=trace.prompt_tokens,
                completion_tokens=trace.completion_tokens,
                total_tokens=trace.total_tokens,
                stages_detail=trace.stages_detail if trace.stages_detail else None,
            )
            session.add(record)
            session.commit()
        finally:
            session.close()
    except Exception:
        logger.exception("save trace failed")
