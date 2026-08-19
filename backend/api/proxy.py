# ============================================================
# 智能网关 — 代理 API 路由
# OpenAI 兼容接口: /v1/chat/completions /v1/models
# ============================================================

import json
import time
import logging

import httpx
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
from backend.core.orchestrate import parse_pipeline, run_relay, run_review
from backend.models import get_session, User, Provider, Router, UsageLog

logger = logging.getLogger("gateway.proxy")
router = APIRouter(prefix="/v1", tags=["proxy"])

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
    if pipeline and not stream:
        mode, stages = pipeline
        if mode == "relay":
            max_attempts = len(stages)
        else:
            max_attempts = 2

        from backend.core.orchestrate import _get_prompt
        if not _get_prompt(body):
            raise HTTPException(400, "编排请求需要 user 消息")

        client_orch = await get_http_client()

        async def stage_forward(stage: str, stage_body: dict):
            """按 stage 名(路由组/模型名)找候选并转发, 返回响应或 None"""
            cands = pick_available_models(providers, stage, routers)
            if not cands:
                return None
            for prov, mdl in cands:
                key_api = decrypted.get(prov.id, "")
                if not key_api:
                    continue
                resp_ns, _err = await forward_non_stream(client_orch, prov, key_api, mdl, stage_body)
                if resp_ns is not None:
                    choices_c = resp_ns.get("choices")
                    if not choices_c:
                        continue
                    return resp_ns
            return None

        if mode == "relay":
            orch_result = await run_relay(stages, body, stage_forward, requested_model)
        else:
            orch_result = await run_review(stages, body, stage_forward, requested_model)

        if orch_result is None:
            raise HTTPException(502, "编排执行失败: 所有阶段均无可用模型")

        # 记录用量(取第一段 usage 或汇总)
        usage = orch_result.get("usage") or {}
        pt = usage.get("prompt_tokens", 0) or 0
        ct = usage.get("completion_tokens", 0) or 0
        try:
            session = get_session()
            try:
                log = UsageLog(
                    user_id=user.id,
                    provider_name="orchestrate",
                    model_name=requested_model,
                    prompt_tokens=pt,
                    completion_tokens=ct,
                    total_tokens=pt + ct,
                    success=True,
                )
                session.add(log)
                user.quota_used_today = (user.quota_used_today or 0) + 1
                session.merge(user)
                session.commit()
            finally:
                session.close()
        except Exception:
            logger.exception("orchestrate usage log failed")

        return JSONResponse(content=orch_result)

    _strat = router_strategies.get(requested_model, "quality") if requested_model else "quality"
    candidates = pick_available_models(providers, requested_model, routers, strategy=_strat)
    if not candidates:
        raise HTTPException(503, f"无可用的模型: {requested_model or '任意'}")

    # 兜底: 如果所有候选都失败,放宽到同一渠道的其他模型
    fallback_candidates = pick_available_models(providers, None, routers)

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
        if not api_key:
            last_err = "API Key 不合法"
            continue

        if stream:
            resp, err = await forward_stream(client, provider, api_key, model, body)
            if resp is not None:
                return StreamingResponse(
                    resp,
                    media_type="text/event-stream",
                    headers={"X-Provider": provider.name, "X-Model": model},
                )
            last_err = err
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
            try:
                session = get_session()
                try:
                    log = UsageLog(
                        user_id=user.id,
                        provider_name=provider.name,
                        model_name=model,
                        prompt_tokens=pt,
                        completion_tokens=ct,
                        total_tokens=pt + ct,
                        success=True,
                    )
                    session.add(log)
                    user.quota_used_today = (user.quota_used_today or 0) + 1
                    session.merge(user)
                    session.commit()
                finally:
                    session.close()
            except Exception:
                logger.exception("usage log failed")

            return JSONResponse(content=resp)

        last_err = err

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