# ============================================================
# 智能网关 — 代理转发引擎
# OpenAI 兼容 /v1/chat/completions 和 /v1/models
# 从原版 app.py 重构,适配多用户
# ============================================================

import json
import time
import logging
from typing import AsyncGenerator

import httpx

from backend.config import settings
from backend.core.circuit import circuit_breaker
from backend.core.scheduler import quality_tracker
from backend.models import Provider

logger = logging.getLogger("gateway.proxy")


# ============================================================
# 模型选择: 按质量评分排序候选
# ============================================================
def pick_available_models(
    providers: list[Provider],
    requested_model: str | None = None,
    routers: dict | None = None,
    force: bool = False,
    strategy: str = "quality",
) -> list[tuple[Provider, str]]:
    """
    返回按质量排序的候选 (provider, model) 列表。

    逻辑:
    - 如果请求的是路由组名 → 组内模型依次尝试
    - 如果请求的是具体模型名 → 找到匹配的(含前缀匹配)
    - 按质量分 + 延迟排序,熔断中的排在最后
    """
    raw = []
    fallback = []

    # 预处理: Provider.models 可能是 JSON 字符串
    import json as _json

    def _model_list(p):
        m = p.models or []
        if isinstance(m, str):
            try:
                return _json.loads(m)
            except Exception:
                return []
        return m

    def _disabled_list(p):
        d = p.disabled_models or []
        if isinstance(d, str):
            try:
                return _json.loads(d)
            except Exception:
                return []
        return d

    # 1. 路由组(中文组名「快速/日常/识图」自动映射到英文键)
    from backend.core.router_group import normalize_group_name
    group_key = normalize_group_name(requested_model) if requested_model else None
    if routers and group_key in routers:
        target_models = set(routers[group_key])
        for p in providers:
            for m in _model_list(p):
                if m in _disabled_list(p):
                    continue
                prefixed = f"{p.name}-{m}"
                if m in target_models or prefixed in target_models:
                    k = f"{p.name}||{m}"
                    if force or not circuit_breaker.is_open(k):
                        raw.append((p, m, k))
                    else:
                        fallback.append((p, m, k))
        if not raw:
            raw = fallback
        scored = _sort_by_strategy(raw, strategy)
        return [(p, m) for p, m, _ in scored]

    # 2. 具体模型
    for p in providers:
        for m in _model_list(p):
            if m in _disabled_list(p):
                continue
            prefixed = f"{p.name}-{m}"
            if requested_model and requested_model != m and requested_model != prefixed:
                continue
            k = f"{p.name}||{m}"
            if force or not circuit_breaker.is_open(k):
                raw.append((p, m, k))
            else:
                fallback.append((p, m, k))

    if not raw:
        raw = fallback

    # 按质量分排序
    scored = [
        (
            quality_tracker.get_score(k),
            quality_tracker.get_avg_latency(k) or 1e9,
            p, m
        )
        for p, m, k in raw
    ]
    scored = _sort_by_strategy(raw, strategy)
    return [(p, m) for _, _, p, m in scored]


# ============================================================
# 非流式转发
# ============================================================
async def forward_non_stream(
    http_client: httpx.AsyncClient,
    provider: Provider,
    api_key: str,
    model: str,
    body: dict,
) -> tuple[dict | None, str | None]:
    """转发非流式请求,返回 (response_dict, error)"""
    key = f"{provider.name}||{model}"
    url = provider.base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    req_body = json.loads(json.dumps(body))
    # 始终用去掉前缀的真实模型名发给上游
    req_body["model"] = MODEL_ALIASES.get(model, model)

    try:
        start = time.time()
        resp = await http_client.post(
            url, json=req_body, headers=headers, timeout=settings.PROXY_TIMEOUT_SECONDS
        )
        latency = (time.time() - start) * 1000

        if resp.status_code >= 400:
            logger.warning("upstream %d from %s: %s", resp.status_code, provider.name, resp.text[:200])
            # 429/限流 → 冷却(指数退避)
            # 429/限流 → 冷却(指数退避)
            if resp.status_code == 429:
                circuit_breaker.record_rate_limit(key)
                circuit_breaker.record_fail(key)
            quality_tracker.record(key, "fail", latency)
            return None, f"upstream {resp.status_code}"

        try:
            data = resp.json()
            quality_tracker.record(key, "ok", latency)
            circuit_breaker.record_success(key)
            data["model"] = f"{provider.name} · {model}"
            return data, None
        except json.JSONDecodeError:
            logger.warning("upstream non-json from %s: %s", provider.name, resp.text[:200])
            circuit_breaker.record_fail(key)
            quality_tracker.record(key, "fail", latency)
            return None, "upstream non-json"

    except httpx.RequestError as e:
        logger.warning("forward error to %s: %s", provider.name, e)
        circuit_breaker.record_fail(key)
        quality_tracker.record(key, "error")
        return None, str(e)
    except Exception as e:
        logger.exception("unexpected forward error to %s", provider.name)
        circuit_breaker.record_fail(key)
        quality_tracker.record(key, "error")
        return None, str(e)


# ============================================================
# 流式转发
# ============================================================
async def forward_stream(
    http_client: httpx.AsyncClient,
    provider: Provider,
    api_key: str,
    model: str,
    body: dict,
) -> tuple[AsyncGenerator[bytes, None] | None, str | None]:
    """流式转发,返回 (async_generator, error)"""
    key = f"{provider.name}||{model}"
    url = provider.base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    req_body = json.loads(json.dumps(body))
    req_body["model"] = model

    try:
        req = http_client.build_request("POST", url, json=req_body, headers=headers)
        resp = await http_client.send(req, stream=True)
    except httpx.RequestError as e:
        logger.warning("stream connect error to %s: %s", provider.name, e)
        circuit_breaker.record_fail(key)
        quality_tracker.record(key, "error")
        return None, str(e)

    if resp.status_code != 200:
        try:
            err_bytes = await resp.aread()
            err_msg = err_bytes.decode("utf-8", errors="ignore")
        except Exception:
            err_msg = ""
        await resp.aclose()
        logger.warning("upstream stream error %d from %s", resp.status_code, provider.name)
        circuit_breaker.record_fail(key)
        quality_tracker.record(key, "fail")
        return None, f"upstream {resp.status_code}"

    # 成功
    circuit_breaker.record_success(key)
    quality_tracker.record(key, "ok")
    start = time.time()

    async def gen():
        prefix = f"🤖 {provider.name} · {model}"
        prefix_done = False
        try:
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if not line.startswith("data: "):
                    yield (line + "\n").encode()
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    yield "data: [DONE]\n\n".encode()
                    break
                try:
                    obj = json.loads(data_str)
                    if "model" in obj and isinstance(obj.get("model"), str):
                        obj["model"] = f"{provider.name} · {model}"
                    yield f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()
                except json.JSONDecodeError:
                    yield (line + "\n").encode()
        finally:
            await resp.aclose()

    return gen(), None


# ============================================================
# 模型别名/上下文长度
# ============================================================
MODEL_ALIASES = {}
CONTEXT_LIMITS: dict[str, int] = {
    # 常见模型上下文长度(默认值)
    "deepseek-ai/DeepSeek-V4-Flash-0731": 131072,
    "deepseek-ai/DeepSeek-V4-Pro": 131072,
    "glm-5.2": 1048576,
    "ZhipuAI/GLM-5.2": 1048576,
    "moonshotai/kimi-k2.6": 262144,
    "minimaxai/minimax-m3": 262144,
    "MiniMax/MiniMax-M3": 262144,
    "Qwen/Qwen3.5-397B-A17B": 131072,
    "Qwen/Qwen3-Next-80B-A3B-Instruct": 131072,
    "Qwen/Qwen3-Next-80B-A3B-Thinking": 131072,
    "Qwen/Qwen3.5-122B-A10B": 131072,
    "Qwen/Qwen3.5-27B": 131072,
    "Qwen/Qwen3-VL-235B-A22B-Instruct": 32768,
    "Qwen/Qwen3-VL-8B-Instruct": 32768,
    "Tencent-Hunyuan/Hy3": 131072,
    "nvidia/nemotron-3-ultra-550b-a55b": 131072,
    "nvidia/nemotron-3-super-120b-a12b": 131072,
    "meta/llama-3.2-90b-vision-instruct": 131072,
}


def get_context_length(model: str) -> int:
    return CONTEXT_LIMITS.get(model, 32768)





def _sort_by_strategy(
    raw: list,
    strategy: str = "quality",
) -> list:
    """按策略排序候选列表: quality / fastest / stable"""
    from backend.core.scheduler import quality_tracker
    from backend.core.circuit import circuit_breaker

    # 收集每个候选的分数
    scored = []
    for p, m, k in raw:
        score = quality_tracker.get_score(k)
        latency = quality_tracker.get_avg_latency(k) or 1e9
        is_open = circuit_breaker.is_open(k)
        scored.append((p, m, k, score, latency, is_open))

    if strategy == "fastest":
        # 延迟最低优先, 熔断中的最后
        scored.sort(key=lambda x: (1 if x[5] else 0, x[4]))
    elif strategy == "stable":
        # 成功率最高优先, 同分按延迟
        scored.sort(key=lambda x: (1 if x[5] else 0, -x[3], x[4]))
    else:  # quality (默认)
        # 质量分最高优先, 同分按延迟
        scored.sort(key=lambda x: (1 if x[5] else 0, -x[3], x[4]))

    return [(p, m, k) for p, m, k, _, _, _ in scored]


def is_1m_model(model: str) -> bool:
    return get_context_length(model) >= 1048576