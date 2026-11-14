# ============================================================
# 智能网关 — 代理 API 路由
# OpenAI 兼容接口: /v1/chat/completions /v1/models
# ============================================================

import asyncio
import json
import time
import logging
import urllib.parse
from typing import AsyncGenerator, Callable, Awaitable

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
    pick_fallback_models,
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
from backend.core.hint import unavailable_reason
from backend.core import vision
from backend.core.orchestrate import _extract_text
from backend.config import settings
from backend.models import get_session, User, Provider, Router, UsageLog, ModelAlias, ModelRate
from backend.core.dead_models import banned_keys

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
            # 失败尝试也留痕: 可靠性统计按渠道归因失败率, 不记就永远"100% 成功"
            trace.add_stage(len(trace.stages_detail), prov.name, mdl,
                            int((time.time() - t0) * 1000), False,
                            str(_err or "empty vision result")[:200])
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
                    _t0 = time.time()
                    gen, _ = await forward_stream(client, prov, key_api, mdl, text_body)
                    if gen is None:
                        # 失败尝试留痕(可靠性统计按渠道归因)
                        trace.add_stage(len(trace.stages_detail), prov.name, mdl,
                                        int((time.time() - _t0) * 1000), False, "stream open failed")
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
        # 失败尝试留痕(可靠性统计按渠道归因)
        trace.add_stage(len(trace.stages_detail), prov.name, mdl,
                        int((time.time() - t0) * 1000), False,
                        str(_err or "no choices")[:200])
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
            # 积分消耗倍率(渠道-模型级, 未设置=1.0): 落库为 cost,
            # 用量统计按 SUM(cost) 展示"等效调用次数"
            rate_row = (
                session.query(ModelRate.rate)
                .filter_by(user_id=user.id, provider_name=provider_name,
                           model=model_name)
                .first()
            )
            session.add(UsageLog(
                user_id=user.id,
                provider_name=provider_name,
                model_name=model_name,
                prompt_tokens=pt,
                completion_tokens=ct,
                total_tokens=pt + ct,
                cost=float(rate_row[0]) if rate_row else 1.0,
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


def _stream_chunk_has_content(chunk: bytes) -> bool:
    """流块是否携带有效内容(文本/工具调用/推理过程)——供流式空流检测用。

    注: reasoning_content 也算内容: 推理模型先吐思考、content 为空的块是合法输出,
    不能把整条流误判成 NVIDIA 式"200 但全程无内容"的空流。
    """
    try:
        if not chunk.startswith(b"data: "):
            return False
        payload = chunk[6:].strip()
        if not payload or payload == b"[DONE]":
            return False
        obj = json.loads(payload)
    except Exception:
        return False
    for ch in obj.get("choices") or []:
        delta = ch.get("delta") or ch.get("message") or {}
        if (delta.get("content") or "").strip() \
                or delta.get("tool_calls") or delta.get("function_call") \
                or (delta.get("reasoning_content") or "").strip():
            return True
    return False


async def _stream_with_fallback(
    primary_gen,
    remaining: list,
    *,
    forward_candidate: Callable[[object, str], Awaitable[tuple]],
    candidate_available: Callable[[object, str], bool],
) -> AsyncGenerator[bytes, None]:
    """流式失败转移包装。

    触发切换候选继续流的两种情况:
      1) primary 提前断流(没发 [DONE]);
      2) primary 正常结束(发了 [DONE])但全程无有效内容——NVIDIA 反复出现的
         "200 OK 但空流" 伪成功, 不切换的话客户端只拿得到空回复。
    任一情况都不转发该 [DONE], 改用 remaining 里的下一候选继续流。
    forward_candidate(prov, mdl) -> (gen|None, err), candidate_available(prov, mdl) -> bool
    """
    done_sent = False
    primary_has_content = False
    try:
        async for chunk in primary_gen:
            if not primary_has_content and _stream_chunk_has_content(chunk):
                primary_has_content = True
            if b"[DONE]" in chunk:
                done_sent = True
                if not primary_has_content:
                    logger.warning("stream primary ended empty (no content), switching candidate")
                    done_sent = False
                    break  # 不 yield 这个 [DONE], 落入候选转移
                yield chunk
                return
            yield chunk
    except Exception as e:
        logger.warning("stream primary gen error: %s", e)

    if done_sent:
        return

    # primary 断流/空流 → 尝试下一个候选
    for fb_prov, fb_mdl in remaining:
        if not candidate_available(fb_prov, fb_mdl):
            continue
        logger.info("stream fallback: switching to %s/%s", fb_prov.name, fb_mdl)
        fb_gen, fb_err = await forward_candidate(fb_prov, fb_mdl)
        if fb_gen is None:
            logger.warning("stream fallback %s/%s also failed: %s", fb_prov.name, fb_mdl, fb_err)
            continue
        fb_has_content = False
        try:
            async for chunk in fb_gen:
                if not fb_has_content and _stream_chunk_has_content(chunk):
                    fb_has_content = True
                if b"[DONE]" in chunk:
                    done_sent = True
                    if not fb_has_content:
                        logger.warning("stream fallback %s/%s also empty, trying next", fb_prov.name, fb_mdl)
                        done_sent = False
                        break
                    yield chunk
                    return
                yield chunk
        except Exception as e:
            logger.warning("stream fallback gen error: %s", e)

    # 所有候选都用完了(或全部空流), 确保发 [DONE]
    if not done_sent:
        yield "data: [DONE]\n\n".encode()


# 全局 httpx 客户端(复用连接池)
_http_client: httpx.AsyncClient | None = None


def _make_upstream_client() -> httpx.AsyncClient:
    """构造上游出站 client(代理策略见 GW_UPSTREAM_PROXY / upstream_http)。

    实测(2026-10-24, 本机): 默认出站被半死的系统代理拖死(所有上游 EOF),
    显式 transport 或 trust_env=False 直连全部可达 → 用工厂统一可配。
    """
    from backend.core import upstream_http
    return upstream_http.make_async(timeout=120)


async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = _make_upstream_client()
    return _http_client


# ============================================================
# POST /v1/chat/completions — OpenAI 兼容代理
# ============================================================
def _long_cooldown_active(key: str) -> bool:
    """该 key 是否已进入"确定性不可用"的 24h 长冷却。

    实现上「先记一次计数, 再看冷却状态」而不是只看 `record_not_found()` 的返回值:
    core/proxy.py 在识别到确定性错误时已经调过一次 `record_not_found`, 两边共用
    同一个 `_not_found_count`, 于是"刚判定为死模型"这个 True 有可能落在 core 那次
    调用上(它不看返回值), api 这次拿到 False → 移除动作永远不发生(实测: 日志已打
    "model marked dead" 但模型仍在路由组里)。
    计数照记(直接由 api 层驱动的路径, 如单测与人工修复, 仍能自行达到阈值), 同时
    以「是否已处于长冷却(≥1h)」这个状态量为准 —— 与调用次数无关, 幂等且可靠。
    """
    circuit_breaker.record_not_found(key)
    return circuit_breaker.cooldown_seconds(key) >= 3600


def _handle_upstream_404(user, provider, model: str, err) -> None:
    """上游确定性"模型不可用"错误处理: 判定死模型 → 彻底移除 + 永久拉黑。

    覆盖 404/410(模型不存在/已下架)与模型级 400(无服务商支持/未开通,
    由 core/proxy.py 标记为 "upstream 400 (dead model)")。

    判定条件: 该 (渠道,模型) 已进入 24h 长冷却 —— 即 core 层已连续
    DEAD_MODEL_THRESHOLD 次确认它确定性不可用。之后:
      1) 从该用户全部路由组彻底删除(写库永久, 热生效);
      2) 拉黑登记 dead_models(写库永久): 刷新模型/自动分类/兜底池此后
         都不会再把它带回来 —— 没有恢复的必要。
    用户显式手动加回仍可行(那是用户意图); 若仍报错 → 再次移除+拉黑(幂等)。
    """
    if not err:
        return
    msg = str(err)
    if ("upstream 404" not in msg and "upstream 410" not in msg
            and "dead model" not in msg):
        return
    key = f"{provider.name}||{model}"
    if not _long_cooldown_active(key):
        return
    entry = f"{provider.name}-{model}"
    from backend.models import get_session, Router
    from backend.core.dead_models import ban_model
    removed = 0
    s = get_session()
    try:
        for r in s.query(Router).filter_by(user_id=user.id).all():
            ms = r.models or []
            if entry in ms:
                r.models = [m for m in ms if m != entry]
                removed += 1
        if removed:
            s.commit()
    finally:
        s.close()
    ban_model(user.id, provider.name, model)
    logger.warning(
        "dead model %s removed from %d group(s) & banned permanently",
        entry, removed,
    )


def _handle_tier_block(user, provider, model: str, err) -> None:
    """账号等级限制(如 workbuddy 11158 personal_free_user): 移出路由组, 但不拉黑。

    与 `_handle_upstream_404` 的区别是**不写 dead_models 永久黑名单** —— 这类
    拒绝是"当前账号类型不支持", 用户升级套餐/换账号后同一个模型名就能用,
    永久拉黑会把它再也带不回来。这里只做两件事:
      1) 24h 长冷却(由 core/proxy.py 的 record_not_found 负责), 期间不再白试;
      2) 从该用户路由组里移除, 让组内其它模型不必每次都多等一轮。
    下次刷新模型时若上游仍返回该模型, 会重新进组(仍受冷却保护)。
    """
    if not err or "账号等级" not in str(err):
        return
    key = f"{provider.name}||{model}"
    if not _long_cooldown_active(key):
        return
    entry = f"{provider.name}-{model}"
    from backend.models import get_session, Router
    removed = 0
    s = get_session()
    try:
        for r in s.query(Router).filter_by(user_id=user.id).all():
            ms = r.models or []
            if entry in ms:
                r.models = [m for m in ms if m != entry]
                removed += 1
        if removed:
            s.commit()
    finally:
        s.close()
    logger.warning(
        "tier-blocked model %s removed from %d group(s) (not banned; "
        "cooldown 24h, upgrade account or refresh models to retry)",
        entry, removed,
    )


def _infer_down_reason(err) -> str:
    """根据上游错误推断下架原因(仅用于界面展示)。"""
    e = str(err or "").lower()
    if "429" in e or "rate" in e or "quota" in e:
        return "rate_limit"
    if "timeout" in e:
        return "timeout"
    if "empty" in e:
        return "empty"
    if "503" in e or "overload" in e:
        return "overload"
    return "auto"


def _maybe_down_model(user, provider, model: str, err) -> None:
    """反复报错(429/503/超时/空回等)触发熔断下架时, 持久化临时下架。

    熔断器连续 DOWN_STRIKE_THRESHOLD 轮失败后把该 model 记入"刚下架"集合,
    这里消费信号并写 downed_models(重启不丢, 到期自动恢复)。
    只下架具体模型(provider||model), 绝不动整个源。
    """
    key = f"{provider.name}||{model}"
    if not circuit_breaker.consume_downed(key):
        return
    from backend.core.downed_models import down_model
    reason = _infer_down_reason(err)
    down_model(user.id, provider.name, model, reason=reason)
    logger.warning("model %s auto-downed & persisted (reason=%s)", key, reason)


_CRED_REJECT_HINTS = (
    "渠道凭据已过期或失效",
    "re-authenticate",
    "unauthorized",
    "invalid access token",
    "security token has expired",
    "apig.0602",
    "sts5.1806",
    "invalid refresh token",
    "token has been used",
    # 兜底: 未识别具体文案的 401/403 也先试一次刷新(仅白嫖渠道会真去刷,
    # 付费渠道在 _heal_credential 里直接返回 None, 不会白耗请求)
    "upstream 401",
    "upstream 403",
)


def _looks_like_cred_reject(err: str | None) -> bool:
    """失败原因是"上游拒绝了这份凭据"吗(而不是限流/模型不存在/超时)。

    只有这类失败值得现场刷新凭据后原地重试; 别的失败刷了也白刷。
    """
    text = str(err or "").lower()
    return any(h.lower() in text for h in _CRED_REJECT_HINTS)


def _skip_provider_after_fail(provider: Provider, skip: set[int]) -> bool:
    """失败后若该渠道已进入渠道级冷却, 把这次请求里它的其余模型也跳过。

    依据是 `record_provider_rate_limit(provider_id, quota=...)` 记下的渠道冷却:
    同渠道模型共享额度(典型是 opencode 免费车道的 9 个模型), 一次 429 之后
    再挨个试别的模型只会白打上游 N 次。返回 True 表示已标记跳过。
    """
    try:
        if circuit_breaker.provider_in_cooldown(provider.id):
            skip.add(provider.id)
            return True
    except Exception:  # pragma: no cover - 纯优化, 不能影响主流程
        pass
    return False


async def _heal_credential(provider: Provider, api_key: str) -> str | None:
    """凭据自愈: 强制刷新该渠道凭据并回写, 返回新的 api_key(失败返回 None)。

    只在白嫖渠道上生效(它们的 api_key 是 JSON 凭据包, 且都有 refresh 通道);
    普通付费渠道的 key 刷不了, 直接返回 None 走原有失败路径。
    """
    from backend.core.channel_auth import is_channel_provider
    if not is_channel_provider(provider.name):
        return None
    try:
        from backend.core.checkin import refresh_provider_credentials
        return await refresh_provider_credentials(
            provider.id, provider.name, api_key)
    except Exception:
        logger.exception("凭据自愈失败: %s(id=%s)", provider.name, provider.id)
        return None


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

        # 路由组(用户自定义)。按名字+id 排序保证确定性: 同名行已由启动迁移去重,
        # 排序兜底避免无 ORDER BY 时 SQLite 全表扫描顺序波动
        router_rows = session.query(Router).filter_by(user_id=user.id)\
            .order_by(Router.name, Router.id).all()
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
                if circuit_breaker.is_open(f"{prov.name}||{mdl}"):
                    continue  # 熔断中的候选跳过
                key_api = decrypted.get(prov.id, "")
                if not key_api and prov.api_key_encrypted:
                    continue
                stage_start = time.time()
                resp_ns, _err = await forward_non_stream(client_orch, prov, key_api, mdl, stage_body)
                stage_latency = int((time.time() - stage_start) * 1000)
                if resp_ns is not None:
                    choices_c = resp_ns.get("choices")
                    if not choices_c:
                        # 空候选也是失败尝试, 留痕供可靠性统计归因
                        trace.add_stage(len(trace.stages_detail), prov.name, mdl,
                                        stage_latency, False, "empty choices")
                        continue
                    trace.add_stage(len(trace.stages_detail), prov.name, mdl, stage_latency, True)
                    return resp_ns
                # 失败尝试留痕(可靠性统计按渠道归因)
                trace.add_stage(len(trace.stages_detail), prov.name, mdl,
                                stage_latency, False, str(_err or "forward failed")[:200])
            trace.add_stage(len(trace.stages_detail), "", stage, 0, False, "all candidates failed")
            return None

        async def stage_forward_stream(stage: str, stage_body: dict):
            """按 stage 名找候选并流式转发, 返回 (generator, error)"""
            cands = pick_available_models(providers, stage, routers, user_aliases=user_aliases)
            if not cands:
                return None, "无可用模型"
            for prov, mdl in cands:
                if circuit_breaker.is_open(f"{prov.name}||{mdl}"):
                    continue  # 熔断中的候选跳过
                key_api = decrypted.get(prov.id, "")
                if not key_api and prov.api_key_encrypted:
                    continue
                _t0 = time.time()
                gen, err = await forward_stream(client_orch, prov, key_api, mdl, stage_body)
                if gen is not None:
                    trace.add_stage(len(trace.stages_detail), prov.name, mdl, 0, True, "streaming")
                    return gen, None
                # 失败尝试留痕(可靠性统计按渠道归因)
                trace.add_stage(len(trace.stages_detail), prov.name, mdl,
                                int((time.time() - _t0) * 1000), False,
                                str(err or "stream open failed")[:200])
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
                user_id=user.id, request_id=trace.request_id, original_model=requested_model or "",
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
                    user_id=user.id, request_id=trace.request_id, original_model=requested_model or "",
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
                user_id=user.id, request_id=trace.request_id, original_model=requested_model or "",
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
            save_chat_log(user_id=user.id, request_id=trace.request_id, original_model=requested_model or "",
                          prompt_text=prompt_text, success=False,
                          error_msg="无可用视觉模型", is_stream=stream)
            raise HTTPException(400, "检测到图片输入，但当前没有可用的视觉模型，请先在路由组配置 vision 组")
        if err == "vision_failed":
            save_chat_log(user_id=user.id, request_id=trace.request_id, original_model=requested_model or "",
                          prompt_text=prompt_text, success=False,
                          error_msg="视觉识别失败", is_stream=stream,
                          is_pipeline=True, pipeline_mode="auto-vision")
            raise HTTPException(502, "视觉识别失败")
        if err == "no_text_model":
            save_chat_log(user_id=user.id, request_id=trace.request_id, original_model=requested_model or "",
                          prompt_text=prompt_text, success=False,
                          error_msg="原文本模型无可用候选", is_stream=stream,
                          is_pipeline=True, pipeline_mode="auto-vision")
            raise HTTPException(502, "原文本模型无可用候选")
        if err == "text_failed":
            save_chat_log(user_id=user.id, request_id=trace.request_id, original_model=requested_model or "",
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
                user_id=user.id, request_id=trace.request_id, original_model=requested_model or "",
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
            user_id=user.id, request_id=trace.request_id, original_model=requested_model or "",
            prompt_text=prompt_text, success=False, error_msg="无可用模型",
            is_stream=stream, latency_ms=trace.latency_ms,
        )
        raise HTTPException(
            503,
            f"无可用的模型: {requested_model or '任意'}"
            + unavailable_reason(providers, requested_model or ""),
        )

    # 解析后的目标(别名/路由组)
    if requested_model and requested_model != (trace.resolved_model or requested_model):
        trace.resolved_model = requested_model

    # 兜底: 如果所有候选都失败,放宽到同一渠道的其他模型。
    # 路由组保持分组隔离(model=qodercn 只允许 qodercn 渠道), 由 pick_fallback_models 保证
    fallback_candidates = pick_fallback_models(
        providers, requested_model, routers, user_aliases=user_aliases)

    client = await get_http_client()
    last_err = None
    # 主候选(用户点名那个模型/组内首选)的失败原因单独留一份。
    # 不能只用 last_err: 兜底会把同渠道其它模型也试一遍, 最后一条错误往往来自
    # 一个完全不同的模型 —— 例如点名 opencode-glm-5.3-flash-free(当日额度耗尽)
    # 却因为兜底打到 beam-free(地区封锁)而提示"该模型在当前出口地区不可用",
    # 用户会以为换个出口就行, 实际是等额度重置。
    primary_err = None
    primary_label = None
    total_usage = None

    # 主候选 + 兜底合并,去重
    # 注意: 主候选必须先登记进 seen, 否则兜底里与主候选重复的 (provider, model)
    # 会被再追加一次 —— 同一候选在一轮里被打两遍, 第二遍必然撞上第一遍刚
    # 打开的熔断, 最后把 "breaker open" 当成真实失败原因回给用户。
    seen = {(p.id, m) for p, m in candidates}
    all_candidates = candidates.copy()
    # 主候选集合: 只有它们能代表"用户点名的那个模型"的失败原因
    primary_keys = set(seen)
    for fb in fallback_candidates:
        if (fb[0].id, fb[1]) not in seen:
            seen.add((fb[0].id, fb[1]))
            all_candidates.append(fb)

    # 冷却期快速失败: 全部候选都处于熔断冷却中时直接回 429 + Retry-After,
    # 不再顺次把上游逐个打一遍 —— 每打一次都在加重对方限流/延长冷却,
    # 客户端拿到 429 也会按 Retry-After 延迟重试, 链路整体更稳。
    from backend.core.dead_models import banned_keys
    banned = banned_keys(user.id)
    # 临时下架(反复报错自动下架, 到期自动恢复): 重启后熔断器内存态已清空,
    # 靠 downed_models 持久化表继续拦截, 保证下架重启不丢。
    from backend.core.downed_models import active_down_keys
    downed = active_down_keys(user.id)
    usable = []
    for provider, model in all_candidates:
        key = f"{provider.name}||{model}"
        if key in banned:
            continue  # 死模型黑名单: 永久跳过(即使冷却已过也不恢复尝试)
        if key in downed:
            continue  # 临时下架中: 反复报错被自动下架, 到期自动恢复
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
        save_chat_log(
            user_id=user.id, request_id=trace.request_id, original_model=requested_model or "",
            prompt_text=prompt_text, success=False,
            error_msg=f"all candidates in cooldown ({retry_after}s)",
            is_stream=stream, latency_ms=trace.latency_ms,
        )
        raise HTTPException(
            429, "上游全部处于限流/配额冷却中, 请稍后重试",
            headers={"Retry-After": str(max(1, retry_after))},
        )
    all_candidates = usable

    # 渠道级冷却(配额/限流): 同一渠道的模型共享同一份额度, 一旦被判渠道冷却,
    # 本轮请求里直接跳过它的其余模型 —— 否则 opencode 这种 9 模型分组会为一次
    # 调用白打 9 次上游(既是浪费, 也容易被上游当成刷量)。
    cooldown_skip: set[int] = set()

    for provider, model in all_candidates:
        if provider.id in cooldown_skip:
            continue
        api_key = decrypted.get(provider.id, "")
        # 免 key 渠道(如 opencode zen)存的是空串, 放行; 有存储但解密失败/未配才算不合法
        if not api_key and provider.api_key_encrypted:
            last_err = "API Key 不合法"
            continue

        key_cb = f"{provider.name}||{model}"
        cb_open = circuit_breaker.is_open(key_cb)
        if cb_open:
            # 熔断中的候选直接跳过(与自动识图/流式 fallback 的行为一致),
            # 不再每次请求都白打一次; 冷却到期后自动恢复参与路由。
            trace.mark_circuit_breaker(True)
            # 只在还没有真实失败原因时才用 breaker open 兜底 —— 否则会把
            # "该模型配额耗尽"之类的真实原因覆盖成一句无信息量的熔断提示。
            if last_err is None:
                last_err = f"breaker open: {provider.name}/{model}"
            continue

        if stream:
            _t0 = time.time()
            resp, err = await forward_stream(client, provider, api_key, model, body)
            if resp is None and _looks_like_cred_reject(err):
                # 凭据自愈: 上游拒绝了这个 token(可能本地时间还没到), 现场刷一次
                # 并原地重试同一候选, 免得整条请求因一次 token 吊销而失败。
                new_key = await _heal_credential(provider, api_key)
                if new_key:
                    decrypted[provider.id] = new_key
                    resp, err = await forward_stream(
                        client, provider, new_key, model, body)
            if resp is None and primary_err is None and (provider.id, model) in primary_keys:
                primary_err, primary_label = err, f"{provider.name}/{model}"
            if resp is not None:
                # 流式 fallback: 主候选断流或"200 空流"时, 由 _stream_with_fallback 切换候选
                remaining = [(p, m) for p, m in all_candidates
                             if (p.id, m) != (provider.id, model)]

                def _candidate_available(p, m):
                    if not decrypted.get(p.id, "") and p.api_key_encrypted:
                        return False
                    return not circuit_breaker.is_open(f"{p.name}||{m}")

                async def _forward_candidate(p, m):
                    return await forward_stream(client, p, decrypted.get(p.id, ""), m, body)

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
                        _stream_with_fallback(
                            resp, remaining,
                            forward_candidate=_forward_candidate,
                            candidate_available=_candidate_available,
                        ),
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
            _handle_upstream_404(user, provider, model, err)
            _handle_tier_block(user, provider, model, err)
            _maybe_down_model(user, provider, model, err)
            # 失败尝试留痕: 可靠性统计按渠道/模型归因失败率, 不记就永远"100% 成功"
            trace.add_stage(len(trace.stages_detail), provider.name, model,
                            int((time.time() - _t0) * 1000), False,
                            str(err or "stream failed")[:200])
            logger.warning("stream fallback: %s failed, trying next: %s", f"{provider.name}/{model}", err)
            _skip_provider_after_fail(provider, cooldown_skip)
            continue

        # 非流式
        _t0 = time.time()
        resp, err = await forward_non_stream(client, provider, api_key, model, body)
        if resp is None and _looks_like_cred_reject(err):
            new_key = await _heal_credential(provider, api_key)
            if new_key:
                decrypted[provider.id] = new_key
                resp, err = await forward_non_stream(
                    client, provider, new_key, model, body)
        if resp is None and _skip_provider_after_fail(provider, cooldown_skip):
            # 渠道级配额/限流冷却(opencode 免费额度 429 等): 同一渠道剩下的
            # 模型共享同一份额度, 这次请求里继续试只是白打上游 —— 直接跳过。
            pass
        if resp is None and primary_err is None and (provider.id, model) in primary_keys:
            primary_err, primary_label = err, f"{provider.name}/{model}"
        if resp is not None:
            # 防护: 上游返回 200 但 choices=null(魔搭在小 max_tokens 时会这样)
            choices = resp.get("choices")
            if not choices:
                logger.warning("upstream %s returned empty choices for %s, trying next", provider.name, model)
                circuit_breaker.record_fail(f"{provider.name}||{model}")
                trace.add_stage(len(trace.stages_detail), provider.name, model,
                                int((time.time() - _t0) * 1000), False, "empty choices")
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
                user_id=user.id, request_id=trace.request_id, original_model=requested_model or "",
                provider_name=provider.name, model_name=model,
                prompt_text=prompt_text,
                completion_text=extract_completion_text(resp),
                prompt_tokens=pt, completion_tokens=ct,
                is_stream=False, latency_ms=trace.latency_ms,
            )

            _log_usage_and_quota(user=user, provider_name=provider.name,
                                 model_name=model, pt=pt, ct=ct)
            return JSONResponse(content=resp)

        # 非流式失败尝试留痕(可靠性统计按渠道归因)
        trace.add_stage(len(trace.stages_detail), provider.name, model,
                        int((time.time() - _t0) * 1000), False,
                        str(err or "forward failed")[:200])
        _handle_upstream_404(user, provider, model, err)
        _handle_tier_block(user, provider, model, err)
        _maybe_down_model(user, provider, model, err)
        last_err = err

    trace.mark_error(last_err or "all candidates failed")
    trace.finish()
    save_trace(trace)
    save_chat_log(
        user_id=user.id, request_id=trace.request_id, original_model=requested_model or "",
        prompt_text=prompt_text, success=False,
        error_msg=str(last_err or "all candidates failed"),
        is_stream=stream, latency_ms=trace.latency_ms,
    )
    # 优先回主候选(用户点名那个模型)的真实原因: 兜底候选的失败原因常常是
    # 另一件事(如 beam-free 地区封锁), 拿它当结论会把用户引向错误处置。
    final_err = primary_err or last_err
    raise HTTPException(502, f"所有候选模型均失败: {final_err}")


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
        router_rows = session.query(Router).filter_by(user_id=user.id)            .order_by(Router.name, Router.id).all()
        # 计费倍率(渠道-模型级): workbuddy/lobsterai 登录时从上游目录同步,
        # 其余手填; 未设置的模型等效 1.0。暴露给客户端作参考(x-gw-rate)。
        rate_map = {
            (r.provider_name, r.model): r.rate
            for r in session.query(ModelRate).filter_by(user_id=user.id).all()
        }

        models_list = []

        # 路由组作为模型
        for r in router_rows:
            models_list.append({
                "id": r.name,
                "object": "model",
                "owned_by": "gateway",
                "created": 0,
            })

        # 裸模型名(全渠道去重并集): pick_available_models 对裸名做精确匹配,
        # 填裸名即自动在所有提供它的渠道间路由+故障转移, 无需配置路由组。
        # 放在组名之后、渠道前缀条目之前, 客户端能直接看到"可以填什么名"。
        dead_keys = banned_keys(user.id)
        bare_models: dict[str, list[str]] = {}
        for p in providers:
            for m in (p.models or []):
                if m in (p.disabled_models or []):
                    continue
                if f"{p.name}||{m}" in dead_keys:
                    continue
                bare_models.setdefault(m, []).append(p.name)
        for m, owners in bare_models.items():
            # 同一裸模型在多渠道的倍率可能不同, 报最低值(路由时免费/低倍渠道优先)
            owner_rates = [float(rate_map.get((pn, m), 1.0)) for pn in owners]
            models_list.append({
                "id": m,
                "object": "model",
                "owned_by": "gateway",
                "x-gw-providers": owners,
                "x-gw-rate": min(owner_rates) if owner_rates else 1.0,
                "context_length": get_context_length(m),
                "created": 0,
            })

        # 所有渠道的模型(渠道名前缀条目; 死模型已被候选池剔除, 列表同步隐藏)
        for p in providers:
            for m in (p.models or []):
                if m in (p.disabled_models or []):
                    continue
                if f"{p.name}||{m}" in dead_keys:
                    continue
                models_list.append({
                    "id": f"{p.name}-{m}",
                    "object": "model",
                    "owned_by": p.name,
                    "x-gw-rate": float(rate_map.get((p.name, m), 1.0)),
                    "context_length": get_context_length(m),
                    "created": 0,
                })

        return {"object": "list", "data": models_list}
    finally:
        session.close()