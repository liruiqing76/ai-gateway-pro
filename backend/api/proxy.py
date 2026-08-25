# ============================================================
# 智能网关 — 代理 API 路由
# OpenAI 兼容接口: /v1/chat/completions /v1/models
# ============================================================

import asyncio
import json
import time
import logging
import urllib.parse

import httpx
from sqlalchemy import text

def _safe_header(value) -> str:
    """HTTP 响应头只允许 latin-1; 渠道名/路由组名可能含中文(如"魔搭"),
    直接放入会导致 uvicorn 写头时 UnicodeEncodeError -> 整请求 500。"""
    return urllib.parse.quote(str(value), safe="")
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import JSONResponse, StreamingResponse

from backend.auth.middleware import get_current_user_from_api_key, get_current_user_any, check_quota
from backend.auth.security import decrypt_secret
from backend.core.proxy import (
    pick_available_models,
    forward_non_stream,
    forward_stream,
    get_context_length,
    is_1m_model,
)
from backend.core.circuit import circuit_breaker
from backend.core.scheduler import quality_tracker
from backend.core.orchestrate import (
    parse_pipeline, run_relay, run_review,
    run_relay_stream, run_review_stream,
)
from backend.core.trace import TraceContext, save_trace
from backend.core.chat_log import (
    extract_prompt_text, extract_completion_text, save_chat_log, StreamRecorder,
    set_probe_mode, is_probe_mode, estimate_tokens_from_text,
)
from backend.core.router_group import classify_model
from backend.core import vision
from backend.core.orchestrate import _extract_text
from backend.config import settings
from backend.models import get_session, User, Provider, Router, UsageLog, ModelAlias

logger = logging.getLogger("gateway.proxy")
router = APIRouter(prefix="/v1", tags=["proxy"])


async def _auto_vision_chat(body, requested_model, stream, user, providers,
                             routers, router_strategies, user_aliases, decrypted, trace):
    """两段式自动图片识别: 视觉组识别图片 -> 文本模型基于识别结果回答。

    返回 (result, is_stream, error):
      error 为 None 表示成功: result 是响应 dict(非流式) 或 async generator(流式)
      error 非空表示失败, 调用方据此抛对应 HTTPException
    内层视觉调用走 forward_non_stream, 不写 UsageLog/不记 trace/不动配额。
    """
    client = await get_http_client()

    # ---- 视觉候选 ----
    vision_cands = pick_available_models(providers, "vision", routers, user_aliases=user_aliases)
    if not vision_cands:
        # 回退: 各渠道里 classify 为 vision 的模型
        seen = set()
        for p in providers:
            for m in (p.models or []):
                if "image" in str(m).lower() or classify_model(str(m)) == "vision":
                    key = f"{p.name}||{m}"
                    if key not in seen:
                        seen.add(key)
                        vision_cands.append((p, m))
    if not vision_cands:
        trace.mark_error("检测到图片但无可用视觉模型")
        trace.finish()
        save_trace(trace)
        return None, False, "no_vision"

    trace.is_pipeline = True
    trace.pipeline_mode = "auto-vision"
    trace.resolved_model = f"{requested_model}+vision"

    # 每个视觉候选的识别超时(快速失败换下一个), 不占用整个代理的 120s 预算
    vision_timeout = max(15, min(45, settings.PROXY_TIMEOUT_SECONDS))

    async def _vision_pass(stage_body) -> tuple[str | None, str, int]:
        """完整扫一遍视觉候选, 返回 (识别文本, 最后错误, 实际尝试数)。
        要求提取出非空文本才算成功, 空结果/异常自动换下一个候选。
        attempted==0 表示候选全部在熔断冷却中, 调用方应直接降级, 不再空转。"""
        last_err = "all vision candidates failed"
        attempted = 0
        for prov, mdl in vision_cands:
            key_cb = f"{prov.name}||{mdl}"
            if circuit_breaker.is_open(key_cb):
                continue  # 连续失败的视觉候选直接跳过, 不再每次请求都重试
            key_api = decrypted.get(prov.id, "")
            if not key_api and prov.api_key_encrypted:
                continue
            attempted += 1
            t0 = time.time()
            try:
                resp, _err = await forward_non_stream(client, prov, key_api, mdl,
                                                      stage_body,
                                                      timeout_seconds=vision_timeout)
                # 个别上游模型 max_tokens 上限较低: 瘦身后若仍 400, 用更小的
                # 输出上限重试一次, 避免把健康模型误判成连续失败而熔断
                if resp is None and _err == "upstream 400":
                    small = json.loads(json.dumps(stage_body))
                    small["max_tokens"] = 512
                    resp, _err = await forward_non_stream(client, prov, key_api, mdl,
                                                          small,
                                                          timeout_seconds=vision_timeout)
            except Exception as e:  # 单个候选崩溃不拖垮整条管道
                logger.warning("auto-vision candidate %s/%s crashed: %s",
                               getattr(prov, "name", "?"), mdl, e)
                continue
            text_now = _extract_text(resp) if resp is not None else ""
            if text_now and str(text_now).strip():
                trace.add_stage(len(trace.stages_detail), prov.name, mdl,
                                int((time.time() - t0) * 1000), True)
                return str(text_now).strip(), "", attempted
            # 200 但无可识别文本同样视为失败(与流式"空流记失败"一致), 计入熔断
            if resp is not None:
                circuit_breaker.record_fail(key_cb)
            last_err = _err or "empty vision result"
            logger.warning("auto-vision candidate %s/%s failed: %s",
                           getattr(prov, "name", "?"), mdl, str(last_err)[:120])
        return None, last_err, attempted

    # ---- 阶段1: 视觉识别(瘦身请求: 只带图+问题, 不带几十万字的历史) ----
    # 直接原样转发完整对话会被 NVIDIA 之类的视觉模型 400:
    # 上下文超限(Hermes 的 system 人设可达 8 万+ token)/ 历史并行 tool_calls,
    # 再叠加 3 轮 × N 候选重试, 单请求能烧掉 50~130 秒。
    vision_body = vision.build_vision_stage_body(body)

    total_passes = max(1, int(settings.VISION_RECOGNIZE_PASSES))
    visor_text, vision_err = None, ""
    for attempt in range(1, total_passes + 1):
        visor_text, vision_err, tried = await _vision_pass(vision_body)
        if visor_text is not None:
            break
        logger.warning("auto-vision pass %d/%d failed: %s",
                       attempt, total_passes, str(vision_err)[:150])
        if tried == 0:
            # 所有候选都在熔断冷却中: 本轮重试无意义, 直接降级文本-only
            logger.warning("auto-vision all vision candidates circuit-open, degrade immediately")
            break
        if attempt < total_passes:
            # 轮间短暂退避, 给上游限流/瞬时抖动留恢复窗口
            await asyncio.sleep(min(5.0, 1.5 * attempt))
    if visor_text is None:
        # 终极兜底: 不再对客户端硬失败(502)。降级为告知文本模型"图片识别失败",
        # 保证 Agent 对话链路永远有响应; trace 里留痕便于排查
        logger.warning("auto-vision degraded to text-only after all retries: %s",
                       str(vision_err)[:150])
        trace.add_stage(len(trace.stages_detail), "", "(degraded)", 0, False,
                        f"vision failed, degrade to text-only: {str(vision_err)[:150]}")
        vision_text = ""
    else:
        vision_text = visor_text

    # ---- 阶段2: 文本模型(剥离图片 + 注入识别结果) ----
    text_body = vision.build_text_stage_body(body, vision_text)
    strategy = router_strategies.get(requested_model, "quality") if requested_model else "quality"
    text_cands = pick_available_models(providers, requested_model, routers,
                                       strategy=strategy, user_aliases=user_aliases)
    if not text_cands:
        trace.mark_error("无可用文本模型")
        trace.finish()
        save_trace(trace)
        return None, False, "no_text_model"

    if stream:
        async def _gen():
            done_sent = False
            try:
                for prov, mdl in text_cands:
                    if circuit_breaker.is_open(f"{prov.name}||{mdl}"):
                        continue  # 连续失败的文本候选直接跳过
                    key_api = decrypted.get(prov.id, "")
                    if not key_api and prov.api_key_encrypted:
                        continue
                    gen, _ = await forward_stream(client, prov, key_api, mdl, text_body)
                    if gen is None:
                        continue
                    trace.add_stage(len(trace.stages_detail), prov.name, mdl, 0, True, "streaming")
                    async for chunk in gen:
                        yield chunk
                        if b"[DONE]" in chunk:
                            done_sent = True
                            return
                    return
                trace.add_stage(len(trace.stages_detail), "", requested_model, 0, False,
                                "all text candidates failed")
            except Exception as e:
                logger.warning("auto-vision text stream error: %s", e)
            finally:
                if not done_sent:
                    yield "data: [DONE]\n\n".encode()
        trace.mark_selected("auto-vision", requested_model, "auto-vision stream ok")
        trace.finish()
        save_trace(trace)
        return _gen(), True, None

    # 非流式
    for prov, mdl in text_cands:
        if circuit_breaker.is_open(f"{prov.name}||{mdl}"):
            continue  # 连续失败的文本候选直接跳过
        key_api = decrypted.get(prov.id, "")
        if not key_api and prov.api_key_encrypted:
            continue
        t0 = time.time()
        resp, _err = await forward_non_stream(client, prov, key_api, mdl, text_body)
        if resp is not None and (resp.get("choices") or []):
            usage = resp.get("usage") or {}
            trace.add_stage(len(trace.stages_detail), prov.name, mdl,
                            int((time.time() - t0) * 1000), True)
            trace.mark_tokens(int(usage.get("prompt_tokens", 0) or 0),
                              int(usage.get("completion_tokens", 0) or 0))
            trace.mark_selected("auto-vision", requested_model, "auto-vision non-stream ok")
            trace.finish()
            save_trace(trace)
            pt = int(usage.get("prompt_tokens", 0) or 0)
            ct = int(usage.get("completion_tokens", 0) or 0)
            _log_usage_and_quota(user=user, provider_name="auto-vision",
                                 model_name=requested_model, pt=pt, ct=ct,
                                 tag="auto-vision")
            return resp, False, None
    trace.mark_error("文本模型调用失败")
    trace.finish()
    save_trace(trace)
    return None, False, "text_failed"


# ── 用量明细与额度累计 ──
def _log_usage_and_quota(*, user, provider_name, model_name, pt, ct, tag="usage"):
    """成功调用后写 UsageLog 并累计当日额度(原子+1, 并发安全)。
    速度探测请求跳过: 不污染调用量/token 统计, 也不占用每日配额
    (后台每 2h 自动测速若计入会白白消耗用户额度)。"""
    if is_probe_mode():
        return
    try:
        session = get_session()
        try:
            session.add(UsageLog(
                user_id=user.id,
                provider_name=provider_name,
                model_name=model_name,
                prompt_tokens=pt,
                completion_tokens=ct,
                total_tokens=pt + ct,
                success=True,
            ))
            # 原子自增: 旧实现是读 ORM user 再 merge 写回, 并发请求会互相覆盖丢计数
            # (实测当日调用数百条, quota_used_today 却只有个位数, 仪表盘"今日调用"严重偏低)
            session.execute(
                text(
                    "UPDATE users SET quota_used_today = COALESCE(quota_used_today, 0) + 1 "
                    "WHERE id = :uid"
                ),
                {"uid": user.id},
            )
            session.commit()
        finally:
            session.close()
    except Exception:
        logger.exception(f"{tag} usage log failed")


def _stream_usage_callback(user, provider_name, model_name, tag="stream"):
    """流式请求结束后的用量统计回调(给 StreamRecorder.record(on_done=...) 用)。

    之前流式路径只走 StreamRecorder 写聊天记录, 从不计数: 结果就是流式调用
    (通常占 90%+)在仪表盘的调用数/token 里全部消失。这里在流式正常结束时
    补记 UsageLog 与当日配额, 与 _log_usage_and_quota 同一套原子逻辑。
    """
    def _cb(rec, pt, ct, success):
        try:
            if not success:
                return  # 客户端断开/上游异常不算成功调用
            _log_usage_and_quota(
                user=user, provider_name=provider_name,
                model_name=model_name, pt=pt, ct=ct, tag=tag,
            )
        except Exception:
            logger.exception(f"{tag} stream usage callback failed")
    return _cb


def _fallback_pt_ct(prompt_text: str, completion_text: str):
    """上游没回传 usage 时的兜底估算(CJK 感知, 贴近官方计费口径)"""
    return (estimate_tokens_from_text(prompt_text or ""),
            estimate_tokens_from_text(completion_text or ""))


# 全局 httpx 客户端(复用连接池)
_http_client: httpx.AsyncClient | None = None


async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=120)
    return _http_client


# ============================================================
# POST /v1/chat/completions — OpenAI 兼容代理
# ============================================================
@router.post("/chat/completions")
async def proxy_chat(
    request: Request,
    user: User = Depends(get_current_user_from_api_key),
    _quota: bool = Depends(check_quota),
):
    body = await request.json()
    requested_model = body.get("model")
    stream = body.get("stream", False)

    # 输入全文(用于聊天记录; 失败也不阻断)
    prompt_text = extract_prompt_text(body)

    # ── 经验注入: 把历史积累的经验/教训作为 system 前缀注入本次调用 ──
    if settings.EXPERIENCE_ENABLED and settings.EXPERIENCE_INJECT_AUTO:
        try:
            from backend.core.experience import build_injection_block, has_injection
            msgs = body.get("messages") or []
            if msgs and not has_injection(msgs):
                block = build_injection_block(user.id, requested_model or "")
                if block:
                    body["messages"] = [
                        {"role": "system", "content": block}
                    ] + msgs
        except Exception:
            logger.exception("experience inject failed (non-blocking)")

    # 初始化链路追踪
    trace = TraceContext(user_id=user.id, original_model=requested_model or "", is_stream=stream)

    # ── 系统内部探测标记(路由组测速等): 链路里单独归类, 不写聊天记录 ──
    if (request.headers.get("x-gw-source") or "").lower() == "speed-test":
        trace.request_type = "speed_test"
        set_probe_mode(True)

    # 获取用户的渠道和路由
    session = get_session()
    try:
        providers = session.query(Provider).filter_by(
            user_id=user.id, enabled=True
        ).all()
        if not providers:
            raise HTTPException(503, "请先配置至少一个渠道")

        # 路由组(用户自定义)
        router_rows = session.query(Router).filter_by(user_id=user.id).all()
        routers = {r.name: r.models for r in router_rows}
        router_strategies = {r.name: (r.strategy or "quality") for r in router_rows}

        # 自动 1M 路由组
        one_m = [f"{p.name}-{m}" for p in providers
                 for m in (p.models or [])
                 if m not in (p.disabled_models or [])
                 and is_1m_model(m)]
        if one_m:
            routers["auto-router-1m"] = one_m

        # 如果用户没有手工建路由组, 自动初始化三分类
        user_routers = session.query(Router).filter_by(user_id=user.id).all()
        if not user_routers:
            from backend.core.router_group import build_router_groups
            builtin = build_router_groups(providers)
            for group_name, model_list in builtin.items():
                if model_list:
                    routers[group_name] = model_list

        # 加载用户自定义模型别名
        alias_rows = session.query(ModelAlias).filter_by(
            user_id=user.id, enabled=True
        ).all()
        user_aliases = {a.alias: a.target for a in alias_rows}
    finally:
        session.close()

    # 解密 API Key
    decrypted = {}
    for p in providers:
        try:
            decrypted[p.id] = decrypt_secret(p.api_key_encrypted)
        except Exception:
            decrypted[p.id] = ""

    # 候选模型
    # ---- 多模型编排: relay:组/组 review:主/审 ----
    pipeline = parse_pipeline(requested_model)
    if pipeline:
        mode, stages = pipeline
        trace.is_pipeline = True
        trace.pipeline_mode = mode
        trace.resolved_model = f"{mode}:{'/'.join(stages)}"

        from backend.core.orchestrate import _get_prompt
        if not _get_prompt(body):
            raise HTTPException(400, "编排请求需要 user 消息")

        client_orch = await get_http_client()

        async def stage_forward(stage: str, stage_body: dict):
            """按 stage 名(路由组/模型名)找候选并转发, 返回响应或 None"""
            cands = pick_available_models(providers, stage, routers, user_aliases=user_aliases)
            if not cands:
                return None
            for prov, mdl in cands:
                key_api = decrypted.get(prov.id, "")
                if not key_api and prov.api_key_encrypted:
                    continue
                stage_start = time.time()
                resp_ns, _err = await forward_non_stream(client_orch, prov, key_api, mdl, stage_body)
                stage_latency = int((time.time() - stage_start) * 1000)
                if resp_ns is not None:
                    choices_c = resp_ns.get("choices")
                    if not choices_c:
                        continue
                    trace.add_stage(len(trace.stages_detail), prov.name, mdl, stage_latency, True)
                    return resp_ns
            trace.add_stage(len(trace.stages_detail), "", stage, 0, False, "all candidates failed")
            return None

        async def stage_forward_stream(stage: str, stage_body: dict):
            """按 stage 名找候选并流式转发, 返回 (generator, error)"""
            cands = pick_available_models(providers, stage, routers, user_aliases=user_aliases)
            if not cands:
                return None, "无可用模型"
            for prov, mdl in cands:
                key_api = decrypted.get(prov.id, "")
                if not key_api and prov.api_key_encrypted:
                    continue
                gen, err = await forward_stream(client_orch, prov, key_api, mdl, stage_body)
                if gen is not None:
                    trace.add_stage(len(trace.stages_detail), prov.name, mdl, 0, True, "streaming")
                    return gen, None
            trace.add_stage(len(trace.stages_detail), "", stage, 0, False, err or "all failed")
            return None, "所有候选均失败"

        if stream:
            # 流式编排: 最后一级流式输出
            if mode == "relay":
                orch_gen, orch_err = await run_relay_stream(stages, body, stage_forward, stage_forward_stream)
            else:
                orch_gen, orch_err = await run_review_stream(stages, body, stage_forward, stage_forward_stream)

            if orch_gen is not None:
                async def _safe_orchestrate_gen(gen):
                    done_sent = False
                    try:
                        async for chunk in gen:
                            yield chunk
                            if b"[DONE]" in chunk:
                                done_sent = True
                                return
                    except Exception as e:
                        logger.warning("orchestrate stream gen error: %s", e)
                    finally:
                        if not done_sent:
                            yield "data: [DONE]\n\n".encode()

                trace.mark_selected("orchestrate", requested_model, f"pipeline stream ok")
                trace.finish()
                save_trace(trace)
                rec = StreamRecorder(
                    user_id=user.id, original_model=requested_model or "",
                    prompt_text=prompt_text, provider_name="orchestrate",
                    model_name=requested_model or "",
                    request_id=trace.request_id,
                    is_pipeline=True, pipeline_mode=mode,
                    start_time=trace._start_time,
                )
                return StreamingResponse(
                    rec.record(
                        _safe_orchestrate_gen(orch_gen),
                        on_done=_stream_usage_callback(
                            user, "orchestrate", requested_model or "",
                            tag="orchestrate-stream",
                        ),
                    ),
                    media_type="text/event-stream",
                    headers={"X-Pipeline": _safe_header(requested_model)},
                )
            # 流式编排全部失败
            trace.mark_error(orch_err)
            trace.finish()
            save_trace(trace)
            save_chat_log(
                user_id=user.id, original_model=requested_model or "",
                prompt_text=prompt_text, success=False,
                error_msg=str(orch_err), is_stream=True,
                is_pipeline=True, pipeline_mode=mode,
                latency_ms=trace.latency_ms,
            )
            raise HTTPException(502, f"流式编排失败: {orch_err}")
        else:
            # 非流式编排
            if mode == "relay":
                orch_result = await run_relay(stages, body, stage_forward, requested_model)
            else:
                orch_result = await run_review(stages, body, stage_forward, requested_model)

            if orch_result is None:
                trace.mark_error("所有阶段均无可用模型")
                trace.finish()
                save_trace(trace)
                save_chat_log(
                    user_id=user.id, original_model=requested_model or "",
                    prompt_text=prompt_text, success=False,
                    error_msg="所有阶段均无可用模型",
                    is_pipeline=True, pipeline_mode=mode,
                    latency_ms=trace.latency_ms,
                )
                raise HTTPException(502, "编排执行失败: 所有阶段均无可用模型")

            # 记录用量
            usage = orch_result.get("usage") or {}
            pt = usage.get("prompt_tokens", 0) or 0
            ct = usage.get("completion_tokens", 0) or 0
            if not pt and not ct:
                # 上游未回传 usage: 按内容估算兜底
                pt, ct = _fallback_pt_ct(prompt_text, extract_completion_text(orch_result))
            trace.mark_tokens(pt, ct)
            trace.mark_selected("orchestrate", requested_model, f"pipeline non-stream ok")
            trace.finish()
            save_trace(trace)
            save_chat_log(
                user_id=user.id, original_model=requested_model or "",
                provider_name="orchestrate", model_name=requested_model or "",
                prompt_text=prompt_text,
                completion_text=extract_completion_text(orch_result),
                prompt_tokens=pt, completion_tokens=ct,
                is_pipeline=True, pipeline_mode=mode,
                latency_ms=trace.latency_ms,
            )

            _log_usage_and_quota(user=user, provider_name="orchestrate",
                                 model_name=requested_model, pt=pt, ct=ct,
                                 tag="orchestrate")
            return JSONResponse(content=orch_result)

    # ---- 自动图片识别路由: 文本模型请求带图片 → 先视觉识别再调文本模型 ----
    if pipeline is None and requested_model and vision.detect_images(body) \
            and not vision.is_vision_target(requested_model):
        res, is_stream, err = await _auto_vision_chat(
            body, requested_model, stream, user, providers,
            routers, router_strategies, user_aliases, decrypted, trace,
        )
        if err == "no_vision":
            save_chat_log(user_id=user.id, original_model=requested_model or "",
                          prompt_text=prompt_text, success=False,
                          error_msg="无可用视觉模型", is_stream=stream)
            raise HTTPException(400, "检测到图片输入，但当前没有可用的视觉模型，请先在路由组配置 vision 组")
        if err == "vision_failed":
            save_chat_log(user_id=user.id, original_model=requested_model or "",
                          prompt_text=prompt_text, success=False,
                          error_msg="视觉识别失败", is_stream=stream,
                          is_pipeline=True, pipeline_mode="auto-vision")
            raise HTTPException(502, "视觉识别失败")
        if err == "no_text_model":
            save_chat_log(user_id=user.id, original_model=requested_model or "",
                          prompt_text=prompt_text, success=False,
                          error_msg="原文本模型无可用候选", is_stream=stream,
                          is_pipeline=True, pipeline_mode="auto-vision")
            raise HTTPException(502, "原文本模型无可用候选")
        if err == "text_failed":
            save_chat_log(user_id=user.id, original_model=requested_model or "",
                          prompt_text=prompt_text, success=False,
                          error_msg="文本模型调用失败", is_stream=stream,
                          is_pipeline=True, pipeline_mode="auto-vision")
            raise HTTPException(502, "文本模型调用失败")
        if res is not None:
            if is_stream:
                rec = StreamRecorder(
                    user_id=user.id, original_model=requested_model or "",
                    prompt_text=prompt_text, provider_name="auto-vision",
                    model_name=requested_model or "",
                    request_id=trace.request_id,
                    is_pipeline=True, pipeline_mode="auto-vision",
                    start_time=trace._start_time,
                )
                return StreamingResponse(
                    rec.record(
                        res,
                        on_done=_stream_usage_callback(
                            user, "auto-vision", requested_model or "",
                            tag="auto-vision-stream",
                        ),
                    ),
                    media_type="text/event-stream",
                    headers={"X-Pipeline": "auto-vision"},
                )
            # 非流式: 直接记录全文
            pt = int(((res.get("usage") or {}).get("prompt_tokens", 0)) or 0)
            ct_ = int(((res.get("usage") or {}).get("completion_tokens", 0)) or 0)
            if not pt and not ct_:
                # 上游未回传 usage: 按内容估算兜底
                pt, ct_ = _fallback_pt_ct(prompt_text, extract_completion_text(res))
            save_chat_log(
                user_id=user.id, original_model=requested_model or "",
                provider_name="auto-vision", model_name=requested_model or "",
                prompt_text=prompt_text,
                completion_text=extract_completion_text(res),
                prompt_tokens=pt, completion_tokens=ct_,
                is_pipeline=True, pipeline_mode="auto-vision",
                latency_ms=trace.latency_ms,
            )
            return JSONResponse(content=res)

    _strat = router_strategies.get(requested_model, "quality") if requested_model else "quality"
    trace.selection_strategy = _strat
    candidates = pick_available_models(providers, requested_model, routers, strategy=_strat, user_aliases=user_aliases)
    if not candidates:
        trace.mark_error("无可用模型")
        trace.finish()
        save_trace(trace)
        save_chat_log(
            user_id=user.id, original_model=requested_model or "",
            prompt_text=prompt_text, success=False, error_msg="无可用模型",
            is_stream=stream, latency_ms=trace.latency_ms,
        )
        raise HTTPException(503, f"无可用的模型: {requested_model or '任意'}")

    # 解析后的目标(别名/路由组)
    if requested_model and requested_model != (trace.resolved_model or requested_model):
        trace.resolved_model = requested_model

    # 兜底: 如果所有候选都失败,放宽到同一渠道的其他模型
    fallback_candidates = pick_available_models(providers, None, routers, user_aliases=user_aliases)

    client = await get_http_client()
    last_err = None
    total_usage = None

    # 主候选 + 兜底合并,去重
    seen = set()
    all_candidates = candidates.copy()
    for fb in fallback_candidates:
        if fb not in seen:
            seen.add((fb[0].id, fb[1]))
            all_candidates.append(fb)

    for provider, model in all_candidates:
        api_key = decrypted.get(provider.id, "")
        # 免 key 渠道(如 opencode zen)存的是空串, 放行; 有存储但解密失败/未配才算不合法
        if not api_key and provider.api_key_encrypted:
            last_err = "API Key 不合法"
            continue

        key_cb = f"{provider.name}||{model}"
        cb_open = circuit_breaker.is_open(key_cb)
        if cb_open:
            trace.mark_circuit_breaker(True)

        if stream:
            resp, err = await forward_stream(client, provider, api_key, model, body)
            if resp is not None:
                # 流式 fallback: 用包装生成器, 第一个候选断流时自动切换到下一个
                async def _stream_with_fallback(primary_gen, remaining):
                    """
                    先 yield 完 primary_gen, 如果它没发 [DONE] 就提前结束,
                    则用 remaining 里的下一个候选继续流。
                    """
                    done_sent = False
                    try:
                        async for chunk in primary_gen:
                            yield chunk
                            # 检测是否已发送 [DONE]
                            if b"[DONE]" in chunk:
                                done_sent = True
                                return
                    except Exception as e:
                        logger.warning("stream primary gen error: %s", e)

                    if done_sent:
                        return

                    # primary 没发 [DONE] 就结束了, 尝试下一个候选
                    for fb_prov, fb_mdl in remaining:
                        fb_key_api = decrypted.get(fb_prov.id, "")
                        if not fb_key_api and fb_prov.api_key_encrypted:
                            continue
                        fb_key_cb = f"{fb_prov.name}||{fb_mdl}"
                        if circuit_breaker.is_open(fb_key_cb):
                            continue
                        logger.info("stream fallback: switching to %s/%s", fb_prov.name, fb_mdl)
                        fb_gen, fb_err = await forward_stream(client, fb_prov, fb_key_api, fb_mdl, body)
                        if fb_gen is None:
                            logger.warning("stream fallback %s/%s also failed: %s", fb_prov.name, fb_mdl, fb_err)
                            continue
                        try:
                            async for chunk in fb_gen:
                                yield chunk
                                if b"[DONE]" in chunk:
                                    done_sent = True
                                    return
                        except Exception as e:
                            logger.warning("stream fallback gen error: %s", e)

                    # 所有候选都用完了, 确保发 [DONE]
                    if not done_sent:
                        yield "data: [DONE]\n\n".encode()

                # 收集剩余候选
                remaining = [(p, m) for p, m in all_candidates
                             if (p.id, m) != (provider.id, model)]

                trace.mark_selected(provider.name, model, f"stream ok, cb={'hit' if cb_open else 'clean'}")
                trace.finish()
                save_trace(trace)
                rec = StreamRecorder(
                    user_id=user.id, original_model=requested_model or "",
                    prompt_text=prompt_text, provider_name=provider.name,
                    model_name=model, request_id=trace.request_id,
                    start_time=trace._start_time,
                )
                return StreamingResponse(
                    rec.record(
                        _stream_with_fallback(resp, remaining),
                        on_done=_stream_usage_callback(
                            user, provider.name, model, tag="stream",
                        ),
                    ),
                    media_type="text/event-stream",
                    headers={"X-Provider": _safe_header(provider.name), "X-Model": _safe_header(model)},
                )
            last_err = err
            if "429" in str(err) or "rate" in str(err).lower():
                trace.mark_rate_limit(True)
            logger.warning("stream fallback: %s failed, trying next: %s", f"{provider.name}/{model}", err)
            continue

        # 非流式
        resp, err = await forward_non_stream(client, provider, api_key, model, body)
        if resp is not None:
            # 防护: 上游返回 200 但 choices=null(魔搭在小 max_tokens 时会这样)
            choices = resp.get("choices")
            if not choices:
                logger.warning("upstream %s returned empty choices for %s, trying next", provider.name, model)
                circuit_breaker.record_fail(f"{provider.name}||{model}")
                last_err = f"empty choices from {provider.name}"
                continue
            # 记录用量
            usage = resp.get("usage") or {}
            pt = usage.get("prompt_tokens", 0) or 0
            ct = usage.get("completion_tokens", 0) or 0
            if not pt and not ct:
                # 上游未回传 usage: 按内容估算兜底
                pt, ct = _fallback_pt_ct(prompt_text, extract_completion_text(resp))

            trace.mark_selected(provider.name, model, f"non-stream ok, cb={'hit' if cb_open else 'clean'}")
            trace.mark_tokens(pt, ct)
            trace.finish()
            save_trace(trace)
            save_chat_log(
                user_id=user.id, original_model=requested_model or "",
                provider_name=provider.name, model_name=model,
                prompt_text=prompt_text,
                completion_text=extract_completion_text(resp),
                prompt_tokens=pt, completion_tokens=ct,
                is_stream=False, latency_ms=trace.latency_ms,
            )

            _log_usage_and_quota(user=user, provider_name=provider.name,
                                 model_name=model, pt=pt, ct=ct)
            return JSONResponse(content=resp)

        last_err = err

    trace.mark_error(last_err or "all candidates failed")
    trace.finish()
    save_trace(trace)
    save_chat_log(
        user_id=user.id, original_model=requested_model or "",
        prompt_text=prompt_text, success=False,
        error_msg=str(last_err or "all candidates failed"),
        is_stream=stream, latency_ms=trace.latency_ms,
    )
    raise HTTPException(502, f"所有候选模型均失败: {last_err}")


# ============================================================
# GET /v1/models — 模型列表
# ============================================================
@router.get("/models")
async def proxy_models(
    user: User = Depends(get_current_user_any),
):
    session = get_session()
    try:
        providers = session.query(Provider).filter_by(
            user_id=user.id, enabled=True
        ).all()
        router_rows = session.query(Router).filter_by(user_id=user.id).all()

        models_list = []

        # 路由组作为模型
        for r in router_rows:
            models_list.append({
                "id": r.name,
                "object": "model",
                "owned_by": "gateway",
                "created": 0,
            })

        # 所有渠道的模型
        for p in providers:
            for m in (p.models or []):
                if m in (p.disabled_models or []):
                    continue
                models_list.append({
                    "id": f"{p.name}-{m}",
                    "object": "model",
                    "owned_by": p.name,
                    "context_length": get_context_length(m),
                    "created": 0,
                })

        return {"object": "list", "data": models_list}
    finally:
        session.close()