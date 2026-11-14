# ============================================================
# 智能网关 — Anthropic Messages 兼容端点
#
# 参考 dsh-our-free-model 插件(src/anthropic.js + src/forward.js)的实现:
# 让网关"原生说 Anthropic"—— Claude Code / Anthropic SDK / Cline 等
# 客户端把 ANTHROPIC_BASE_URL 指向本网关即可直连, 无需任何转换垫片。
#
#   POST /v1/messages               Anthropic Messages(流式/非流式, 含 tool_use/thinking)
#   POST /v1/messages/count_tokens  本地 token 估算(不打一次上游)
#
# 协议转换的纯函数在 backend/core/anthropic_codec.py; 本文件只做
# 路由/鉴权/候选循环/观测记录, 转发与可靠性设施全部复用 OpenAI 面:
#   pick_available_models / forward_non_stream / forward_stream /
#   _stream_with_fallback / StreamRecorder / circuit_breaker / trace
#
# 流式管道(与 dsh-our-free-model 的 forward listener 同构):
#   forward_stream(OpenAI SSE) → _stream_with_fallback(候选切换)
#   → StreamRecorder(聊天记录/用量) → AnthropicStreamRenderer(协议转换)
#   → 客户端(Anthropic 事件流, 含 SSE 心跳注释帧)
#
# 首版限制: 编排(relay:/review:)与自动识图请在 /v1/chat/completions 调用;
# 路由组/别名/具体模型名/auto-router-1m 在本面全部可用。
# ============================================================

from __future__ import annotations

import asyncio
import logging
import secrets
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.auth.middleware import get_current_user_from_api_key, check_quota
from backend.auth.security import decrypt_secret
from backend.core.hint import unavailable_reason
from backend.api.proxy import (
    _stream_with_fallback,
    _stream_usage_callback,
    _fallback_pt_ct,
    _handle_upstream_404,
    _handle_tier_block,
    _maybe_down_model,
    _log_usage_and_quota,
    _safe_header,
    _looks_like_cred_reject,
    _heal_credential,
    get_http_client,
)
from backend.core.anthropic_codec import (
    AnthropicStreamRenderer,
    count_tokens,
    from_anthropic_request,
    openai_completion_to_message,
)
from backend.core.proxy import (
    pick_available_models,
    pick_fallback_models,
    forward_non_stream,
    forward_stream,
    is_1m_model,
)
from backend.core.circuit import circuit_breaker
from backend.core.orchestrate import parse_pipeline
from backend.core.trace import TraceContext, save_trace
from backend.core.chat_log import (
    extract_prompt_text, extract_completion_text, save_chat_log, StreamRecorder,
)
from backend.models import get_session, User, Provider, Router, ModelAlias

logger = logging.getLogger("gateway.anthropic")
router = APIRouter(prefix="/v1", tags=["anthropic"])

# 与 dsh-our-free-model 的 SSE_HEARTBEAT_MS(15s)同款: 推理模型可能
# 70s+ 无输出, SSE 注释帧保活连接, 客户端按协议忽略。
HEARTBEAT_SECONDS = 15

# Anthropic 错误形状: {"type":"error","error":{"type","message"}}
_STATUS_TO_ERROR_TYPE = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    500: "api_error",
    502: "api_error",
    503: "overloaded_error",
    529: "overloaded_error",
}


def _anthropic_error(status: int, message: str, *, etype: str | None = None,
                     headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"type": "error",
                 "error": {"type": etype or _STATUS_TO_ERROR_TYPE.get(status, "api_error"),
                           "message": message}},
        headers=headers or {},
    )


async def anthropic_http_exception_handler(request: Request, exc: StarletteHTTPException):
    """把 /v1/messages* 路径的 HTTPException 渲染成 Anthropic 错误形状。

    其它路径完全复刻 FastAPI 默认行为({"detail": ...}), 不改变现有
    OpenAI 面的响应契约。在 main.py 里 add_exception_handler 注册。
    """
    if request.url.path.startswith("/v1/messages"):
        etype = _STATUS_TO_ERROR_TYPE.get(exc.status_code, "api_error")
        return JSONResponse(
            status_code=exc.status_code,
            content={"type": "error", "error": {"type": etype, "message": str(exc.detail)}},
            headers=getattr(exc, "headers", None) or {},
        )
    if getattr(exc, "headers", None):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


# ============================================================
# 流式管道最外层: OpenAI SSE 字节 → Anthropic 事件帧
# ============================================================

async def _anthropic_frame_gen(openai_gen, renderer: AnthropicStreamRenderer):
    """消费 OpenAI SSE 字节流, 产出 Anthropic 事件帧字节流。

    - 用 asyncio.wait(不 cancel)实现心跳: 上游 15s 无输出时发 SSE 注释帧;
    - 上游发 data: [DONE] 时 renderer 已收尾, 再耗尽即退出;
    - 上游异常(理论上已被内层 record 吞掉)兜底渲染 error 事件;
    - 客户端断开(CancelledError)直接上抛, 由内层记为失败。
    """
    it = openai_gen.__aiter__()
    task = asyncio.ensure_future(it.__anext__())
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=HEARTBEAT_SECONDS)
            if not done:
                yield b": ping\n\n"
                continue
            try:
                chunk = task.result()
            except StopAsyncIteration:
                break
            # 一个 TCP chunk 可能含多行; renderer 逐行消费并忽略注释行
            for line in chunk.splitlines():
                for frame in renderer.push_line(line):
                    yield frame.encode("utf-8")
            task = asyncio.ensure_future(it.__anext__())
        # 上游没发 [DONE] 就耗尽: 兜底补收尾帧
        for frame in renderer.finish():
            yield frame.encode("utf-8")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("anthropic frame gen error: %s", e)
        try:
            for frame in renderer.push_error(str(e)):
                yield frame.encode("utf-8")
        except Exception:
            pass
    finally:
        if not task.done():
            task.cancel()


# ============================================================
# POST /v1/messages — Anthropic Messages 端点
# ============================================================

@router.post("/messages")
async def anthropic_messages(
    request: Request,
    user: User = Depends(get_current_user_from_api_key),
    _quota: bool = Depends(check_quota),
):
    raw = await request.json()

    # 1) 协议转换(纯函数, 失败返回 {"error": ...})
    converted = from_anthropic_request(raw)
    if isinstance(converted, dict) and converted.get("error"):
        return _anthropic_error(400, converted["error"])

    stream = raw.get("stream") is True
    requested_model = str(converted.get("model") or "")
    converted["stream"] = stream

    prompt_text = extract_prompt_text(converted)
    trace = TraceContext(user_id=user.id, original_model=requested_model, is_stream=stream)

    # 2) 编排暂不在本面支持(与 OpenAI 面行为对齐的成本权衡, 首版)
    if parse_pipeline(requested_model):
        return _anthropic_error(
            400, "编排模型(relay:/review:)请暂时通过 /v1/chat/completions 调用")

    # 3) DB 加载: 渠道/路由组/别名(与 proxy_chat 同一段)
    session = get_session()
    try:
        providers = session.query(Provider).filter_by(
            user_id=user.id, enabled=True
        ).all()
        if not providers:
            return _anthropic_error(503, "请先配置至少一个渠道")

        router_rows = session.query(Router).filter_by(user_id=user.id) \
            .order_by(Router.name, Router.id).all()
        routers = {r.name: r.models for r in router_rows}
        router_strategies = {r.name: (r.strategy or "quality") for r in router_rows}

        one_m = [f"{p.name}-{m}" for p in providers
                 for m in (p.models or [])
                 if m not in (p.disabled_models or [])
                 and is_1m_model(m)]
        if one_m:
            routers["auto-router-1m"] = one_m

        user_routers = session.query(Router).filter_by(user_id=user.id).all()
        if not user_routers:
            from backend.core.router_group import build_router_groups
            builtin = build_router_groups(providers)
            for group_name, model_list in builtin.items():
                if model_list:
                    routers[group_name] = model_list

        alias_rows = session.query(ModelAlias).filter_by(
            user_id=user.id, enabled=True
        ).all()
        user_aliases = {a.alias: a.target for a in alias_rows}
    finally:
        session.close()

    # 4) 解密渠道 Key
    decrypted = {}
    for p in providers:
        try:
            decrypted[p.id] = decrypt_secret(p.api_key_encrypted)
        except Exception:
            decrypted[p.id] = ""

    # 5) 候选选取 + 黑名单/下架/熔断过滤(与 proxy_chat 同一段)
    _strat = router_strategies.get(requested_model, "quality") if requested_model else "quality"
    trace.selection_strategy = _strat
    candidates = pick_available_models(
        providers, requested_model, routers, strategy=_strat, user_aliases=user_aliases)
    if not candidates:
        trace.mark_error("无可用模型")
        trace.finish()
        save_trace(trace)
        save_chat_log(user_id=user.id, request_id=trace.request_id,
                      original_model=requested_model, prompt_text=prompt_text,
                      success=False, error_msg="无可用模型", is_stream=stream,
                      latency_ms=trace.latency_ms)
        return _anthropic_error(
            503,
            f"无可用的模型: {requested_model or '任意'}"
            + unavailable_reason(providers, requested_model or ""),
        )

    fallback_candidates = pick_fallback_models(
        providers, requested_model, routers, user_aliases=user_aliases)

    client = await get_http_client()
    last_err = None
    # 主候选(用户点名那个模型)的失败原因单独留一份 —— 兜底候选常因另一件事
    # 失败(如 opencode beam-free 地区封锁), 拿它当结论会把用户引向错误处置。
    primary_err = None

    # 同 proxy.py: 主候选先入 seen, 否则兜底会重复追加同一候选,
    # 第二次必然撞上第一次刚打开的熔断并把 "breaker open" 当成真实错误回给用户。
    seen = {(p.id, m) for p, m in candidates}
    primary_keys = set(seen)
    all_candidates = candidates.copy()
    for fb in fallback_candidates:
        if (fb[0].id, fb[1]) not in seen:
            seen.add((fb[0].id, fb[1]))
            all_candidates.append(fb)

    from backend.core.dead_models import banned_keys
    banned = banned_keys(user.id)
    from backend.core.downed_models import active_down_keys
    downed = active_down_keys(user.id)
    usable = []
    for provider, model in all_candidates:
        key = f"{provider.name}||{model}"
        if key in banned or key in downed:
            continue
        if not circuit_breaker.is_open(key):
            usable.append((provider, model))
    if not usable:
        retry_after = max(
            [circuit_breaker.cooldown_seconds(f"{p.name}||{m}") for p, m in all_candidates]
            or [60]
        )
        trace.mark_error(f"all candidates in cooldown, retry in {retry_after}s")
        trace.finish()
        save_trace(trace)
        save_chat_log(user_id=user.id, request_id=trace.request_id,
                      original_model=requested_model, prompt_text=prompt_text,
                      success=False,
                      error_msg=f"all candidates in cooldown ({retry_after}s)",
                      is_stream=stream, latency_ms=trace.latency_ms)
        return _anthropic_error(
            429, "上游全部处于限流/配额冷却中, 请稍后重试",
            headers={"Retry-After": str(max(1, retry_after))})
    all_candidates = usable

    message_id = f"msg_{secrets.token_hex(12)}"

    # 6) 候选循环(与 proxy_chat 主循环同构; 响应侧换成 Anthropic 形状)
    for provider, model in all_candidates:
        api_key = decrypted.get(provider.id, "")
        if not api_key and provider.api_key_encrypted:
            last_err = "API Key 不合法"
            continue

        key_cb = f"{provider.name}||{model}"
        if circuit_breaker.is_open(key_cb):
            trace.mark_circuit_breaker(True)
            # 只在还没有真实失败原因时才用 breaker open 兜底(同 proxy.py)
            if last_err is None:
                last_err = f"breaker open: {provider.name}/{model}"
            continue

        if stream:
            _t0 = time.time()
            resp, err = await forward_stream(client, provider, api_key, model, converted)
            if resp is None and _looks_like_cred_reject(err):
                # 凭据自愈(与 OpenAI 面同策略): 上游拒绝 token 就现场刷一次再重试
                new_key = await _heal_credential(provider, api_key)
                if new_key:
                    decrypted[provider.id] = new_key
                    resp, err = await forward_stream(
                        client, provider, new_key, model, converted)
            if resp is None and primary_err is None and (provider.id, model) in primary_keys:
                primary_err = err
            if resp is not None:
                remaining = [(p, m) for p, m in all_candidates
                             if (p.id, m) != (provider.id, model)]

                def _candidate_available(p, m):
                    if not decrypted.get(p.id, "") and p.api_key_encrypted:
                        return False
                    return not circuit_breaker.is_open(f"{p.name}||{m}")

                async def _forward_candidate(p, m):
                    return await forward_stream(client, p, decrypted.get(p.id, ""), m, converted)

                trace.mark_selected(provider.name, model, "stream ok")
                trace.finish()
                save_trace(trace)
                rec = StreamRecorder(
                    user_id=user.id, original_model=requested_model,
                    prompt_text=prompt_text, provider_name=provider.name,
                    model_name=model, request_id=trace.request_id,
                    start_time=trace._start_time,
                )
                renderer = AnthropicStreamRenderer(
                    message_id=message_id, model=requested_model or model)
                openai_stream = _stream_with_fallback(
                    resp, remaining,
                    forward_candidate=_forward_candidate,
                    candidate_available=_candidate_available,
                )
                recorded = rec.record(
                    openai_stream,
                    on_done=_stream_usage_callback(
                        user, provider.name, model, tag="anthropic-stream"),
                )
                return StreamingResponse(
                    _anthropic_frame_gen(recorded, renderer),
                    media_type="text/event-stream",
                    headers={
                        "X-Provider": _safe_header(provider.name),
                        "X-Model": _safe_header(model),
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                    },
                )
            last_err = err
            if "429" in str(err) or "rate" in str(err).lower():
                trace.mark_rate_limit(True)
            _handle_upstream_404(user, provider, model, err)
            _handle_tier_block(user, provider, model, err)
            _maybe_down_model(user, provider, model, err)
            trace.add_stage(len(trace.stages_detail), provider.name, model,
                            int((time.time() - _t0) * 1000), False,
                            str(err or "stream failed")[:200])
            logger.warning("anthropic stream: %s failed, trying next: %s",
                           f"{provider.name}/{model}", err)
            continue

        # 非流式
        _t0 = time.time()
        resp, err = await forward_non_stream(client, provider, api_key, model, converted)
        if resp is None and _looks_like_cred_reject(err):
            new_key = await _heal_credential(provider, api_key)
            if new_key:
                decrypted[provider.id] = new_key
                resp, err = await forward_non_stream(
                    client, provider, new_key, model, converted)
        if resp is None and primary_err is None and (provider.id, model) in primary_keys:
            primary_err = err
        if resp is not None:
            choices = resp.get("choices")
            if not choices:
                logger.warning("upstream %s returned empty choices for %s, trying next",
                               provider.name, model)
                circuit_breaker.record_fail(f"{provider.name}||{model}")
                trace.add_stage(len(trace.stages_detail), provider.name, model,
                                int((time.time() - _t0) * 1000), False, "empty choices")
                last_err = f"empty choices from {provider.name}"
                continue

            usage = resp.get("usage") or {}
            pt = usage.get("prompt_tokens", 0) or 0
            ct = usage.get("completion_tokens", 0) or 0
            if not pt and not ct:
                pt, ct = _fallback_pt_ct(prompt_text, extract_completion_text(resp))

            trace.mark_selected(provider.name, model, "non-stream ok")
            trace.mark_tokens(pt, ct)
            trace.finish()
            save_trace(trace)
            save_chat_log(
                user_id=user.id, request_id=trace.request_id,
                original_model=requested_model,
                provider_name=provider.name, model_name=model,
                prompt_text=prompt_text,
                completion_text=extract_completion_text(resp),
                prompt_tokens=pt, completion_tokens=ct,
                is_stream=False, latency_ms=trace.latency_ms,
            )
            _log_usage_and_quota(user=user, provider_name=provider.name,
                                 model_name=model, pt=pt, ct=ct, tag="anthropic")
            return JSONResponse(
                content=openai_completion_to_message(resp, requested_model or model, message_id))

        trace.add_stage(len(trace.stages_detail), provider.name, model,
                        int((time.time() - _t0) * 1000), False,
                        str(err or "forward failed")[:200])
        _handle_upstream_404(user, provider, model, err)
        _handle_tier_block(user, provider, model, err)
        _maybe_down_model(user, provider, model, err)
        last_err = err

    # 7) 全部候选失败
    trace.mark_error(last_err or "all candidates failed")
    trace.finish()
    save_trace(trace)
    save_chat_log(
        user_id=user.id, request_id=trace.request_id,
        original_model=requested_model, prompt_text=prompt_text,
        success=False, error_msg=str(last_err or "all candidates failed"),
        is_stream=stream, latency_ms=trace.latency_ms,
    )
    # 优先回主候选(用户点名那个模型)的真实原因, 见 primary_err 的说明
    final_err = primary_err or last_err
    return _anthropic_error(502, f"所有候选模型均失败: {final_err}")


# ============================================================
# POST /v1/messages/count_tokens — 本地估算(不打上游)
# ============================================================

@router.post("/messages/count_tokens")
async def anthropic_count_tokens(
    request: Request,
    user: User = Depends(get_current_user_from_api_key),
):
    body = await request.json()
    return count_tokens(body)
