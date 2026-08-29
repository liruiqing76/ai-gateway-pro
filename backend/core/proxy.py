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

# 配额/余额耗尽类 429(insufficient_quota / insufficient balance / entitlement)——
# 短期不会自动恢复(充值/重置额度前一直失败), 触发比一般限流更长的冷却;
# 一般限流(过载/被限)仍走指数退避 30s→300s。
_QUOTA_HINTS = ("insufficient_quota", "quota", "balance", "entitlement", "额度", "余额")


def _is_quota_error(text: str) -> bool:
    """429 响应体是否属于配额/余额耗尽(而非瞬时过载限流)。"""
    t = (text or "").lower()
    return any(h in t for h in _QUOTA_HINTS)

# 轮询计数器: 同一组/模型的候选列表每次请求整体轮转一位,
# 让"最快/质量最高"的模型不再独吞全部流量(单一上游额度/限流被打爆的根源)。
_round_robin_counter: dict[str, int] = {}


def _rotate_candidates(scored: list, key: str) -> list:
    """把健康候选按轮询计数器旋转, 熔断中的候选保持排最后。"""
    if len(scored) <= 1:
        return scored
    healthy = [x for x in scored if not x[5]]
    broken = [x for x in scored if x[5]]
    if not healthy:
        return scored
    idx = _round_robin_counter.get(key, 0) % len(healthy)
    _round_robin_counter[key] = idx + 1
    return healthy[idx:] + healthy[:idx] + broken


# ============================================================
# 模型选择: 按质量评分排序候选
# ============================================================
def pick_available_models(
    providers: list[Provider],
    requested_model: str | None = None,
    routers: dict | None = None,
    force: bool = False,
    strategy: str = "quality",
    user_aliases: dict | None = None,
) -> list[tuple[Provider, str]]:
    """
    返回按质量排序的候选 (provider, model) 列表。

    逻辑:
    - 应用用户自定义别名映射 (如 gpt-4 → high_quality)
    - 如果请求的是路由组名 → 组内模型依次尝试
    - 如果请求的是具体模型名 → 找到匹配的(含前缀匹配)
    - 按质量分 + 延迟排序,熔断中的排在最后
    """
    raw = []
    fallback = []

    # 应用别名映射
    if user_aliases and requested_model:
        resolved = user_aliases.get(requested_model)
        if resolved:
            logger.debug("alias resolved: %s -> %s", requested_model, resolved)
            requested_model = resolved

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
        scored = _sort_by_strategy(raw, strategy, rr_key=f"{group_key}:{strategy}")
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
    scored = _sort_by_strategy(
        raw, strategy, rr_key=f"{requested_model or 'any'}:{strategy}"
    )
    return [(p, m) for p, m, _ in scored]


# ============================================================
# 非流式转发
# ============================================================
async def forward_non_stream(
    http_client: httpx.AsyncClient,
    provider: Provider,
    api_key: str,
    model: str,
    body: dict,
    timeout_seconds: int | None = None,
) -> tuple[dict | None, str | None]:
    """转发非流式请求,返回 (response_dict, error)。
    timeout_seconds 可覆盖全局 PROXY_TIMEOUT_SECONDS(如视觉子调用需要快速失败)。"""
    key = f"{provider.name}||{model}"
    url = provider.base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    # 免 key 渠道(如 opencode zen 免费档)不带 Authorization, 否则空 Bearer 会被上游拒
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req_body = json.loads(json.dumps(body))
    # 始终用去掉前缀的真实模型名发给上游
    req_body["model"] = MODEL_ALIASES.get(model, model)

    try:
        start = time.time()
        resp = await http_client.post(
            url, json=req_body, headers=headers,
            timeout=timeout_seconds or settings.PROXY_TIMEOUT_SECONDS,
        )
        latency = (time.time() - start) * 1000

        if resp.status_code >= 400:
            logger.warning("upstream %d from %s: %s", resp.status_code, provider.name, resp.text[:200])
            quality_tracker.record(key, "fail", latency)
            # 429: 配额/余额耗尽走长效冷却, 一般限流走指数退避; 不再叠加 record_fail
            # (record_fail 会以 60s 覆盖掉刚才设置的更长冷却, 让配额冷却形同虚设)
            if resp.status_code == 429:
                if _is_quota_error(resp.text):
                    circuit_breaker.record_quota_exhausted(key)
                else:
                    circuit_breaker.record_rate_limit(key)
            # 其他 4xx 是确定性失败(上下文超限/不支持字段/鉴权等), 同样计入熔断:
            # 只对 429 记录会导致确定性 400 每次请求都原样重试, 慢请求反复烧时间
            else:
                circuit_breaker.record_fail(key)
            return None, f"upstream {resp.status_code}"

        try:
            data = resp.json()
            # 空回复防护: 200 但所有 choice 既无内容也无工具调用 —— 对 Agent
            # 是致命"假成功"(客户端只能拿空消息重试), 视为失败换下一个候选。
            # 有 tool_calls/function_call 的纯工具响应是合法的, 不算空。
            has_payload = False
            has_reasoning = False
            for ch in data.get("choices") or []:
                msg = ch.get("message") or {}
                if (msg.get("content") or "").strip() \
                        or msg.get("tool_calls") or msg.get("function_call"):
                    has_payload = True
                    break
                # 推理模型(deepseek-v4-flash / glm-5.2 等)先吐 reasoning_content、
                # content 为空的响应是合法输出, 不是"假成功", 不能误判为空回复
                # (否则 record_fail 会把健康候选连坐熔断/降权)。
                if (msg.get("reasoning_content") or "").strip():
                    has_reasoning = True
            if not has_payload and not has_reasoning:
                logger.warning("upstream %s returned empty response (no content/tool_calls): %s",
                               provider.name, resp.text[:120])
                circuit_breaker.record_fail(key)
                quality_tracker.record(key, "fail", latency)
                return None, "upstream empty response"
            quality_tracker.record(key, "ok", latency)
            circuit_breaker.record_success(key)
            data["model"] = f"{provider.name} · {model}"
            return data, None
        except json.JSONDecodeError:
            logger.warning("upstream non-json from %s: %s", provider.name, resp.text[:200])
            circuit_breaker.record_fail(key)
            quality_tracker.record(key, "fail")
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
    """
    流式转发,返回 (async_generator, error)

    改进:连接成功后验证第一个chunk，确保数据格式正确
    如果连接成功但数据格式错误（如上游返回非SSE格式），会记录失败并返回错误
    """
    key = f"{provider.name}||{model}"
    url = provider.base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    # 免 key 渠道(如 opencode zen 免费档)不带 Authorization, 否则空 Bearer 会被上游拒
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req_body = json.loads(json.dumps(body))
    req_body["model"] = MODEL_ALIASES.get(model, model)

    # 流式请求显式向上游索要 token 用量: OpenAI 兼容上游默认不在流里回传
    # usage, 必须带 stream_options.include_usage 才有; 否则网关只能字符估算,
    # 与官方计费口径偏差很大。个别上游不认识该字段会 400, 捕获后去掉重试。
    if req_body.get("stream"):
        so = req_body.get("stream_options")
        if isinstance(so, dict):
            so.setdefault("include_usage", True)
        else:
            req_body["stream_options"] = {"include_usage": True}

    try:
        # 请求级超时: 读超时(首字节/相邻块间隔)超过 PROXY_STREAM_READ_TIMEOUT
        # 即放弃当前候选换下一个, 避免"慢模型卡满 120s 全局超时"
        req_timeout = httpx.Timeout(
            connect=15,
            read=settings.PROXY_STREAM_READ_TIMEOUT,
            write=30,
            pool=10,
        )
        req = http_client.build_request("POST", url, json=req_body, headers=headers,
                                        timeout=req_timeout)
        resp = await http_client.send(req, stream=True)
        if resp.status_code == 400 and req_body.pop("stream_options", None) is not None:
            err_bytes = await resp.aread()
            await resp.aclose()
            logger.warning("upstream %s rejected stream_options (%s), retrying without it",
                           provider.name, err_bytes.decode('utf-8', errors='ignore')[:120])
            req = http_client.build_request("POST", url, json=req_body, headers=headers,
                                            timeout=req_timeout)
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
        logger.warning("upstream stream error %d from %s: %s", resp.status_code, provider.name, err_msg[:200])
        quality_tracker.record(key, "fail")
        # 429 同样区分配额耗尽(长效冷却)与一般限流(指数退避); 5xx 与其他 4xx 走普通熔断
        if resp.status_code == 429:
            if _is_quota_error(err_msg):
                circuit_breaker.record_quota_exhausted(key)
            else:
                circuit_breaker.record_rate_limit(key)
        else:
            circuit_breaker.record_fail(key)
        return None, f"upstream {resp.status_code}"

    # 连接成功,创建共享迭代器避免重复消费httpx流
    circuit_breaker.record_success(key)
    quality_tracker.record(key, "ok")

    async def gen():
        done_sent = False
        first_line_yielded = False
        stream_ok = False
        got_content = False  # 是否出现过有效内容(回答文本/工具调用)
        try:
            async for line in resp.aiter_lines():
                if not line:
                    continue

                # 预读阶段:第一个有效行用于验证格式
                if not first_line_yielded:
                    first_line_yielded = True
                    if not line.startswith("data: "):
                        logger.warning("upstream stream invalid format from %s: %s", provider.name, line)
                        circuit_breaker.record_fail(key)
                        quality_tracker.record(key, "fail")
                        break

                # yield数据
                if not line.startswith("data: "):
                    stream_ok = True
                    yield (line + "\n").encode()
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    stream_ok = True
                    yield "data: [DONE]\n\n".encode()
                    done_sent = True
                    break
                try:
                    obj = json.loads(data_str)
                    if "model" in obj and isinstance(obj.get("model"), str):
                        obj["model"] = f"{provider.name} · {model}"
                    # 空回复检测: 只有 role/finish 的空壳块不算内容;
                    # reasoning_content 不算(只有思考没有答案对 Agent 等于失败)
                    for ch in obj.get("choices") or []:
                        delta = ch.get("delta") or ch.get("message") or {}
                        if (delta.get("content") or "").strip() \
                                or delta.get("tool_calls") or delta.get("function_call") \
                                or (delta.get("reasoning_content") or "").strip():
                            got_content = True
                            break
                    stream_ok = True
                    yield f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()
                except json.JSONDecodeError:
                    stream_ok = True
                    yield (line + "\n").encode()
        except Exception as e:
            logger.warning("stream gen error from %s/%s: %s", provider.name, model, e)
        finally:
            # 空流(上游 200 但没有任何有效行)同样视为失败, 计入熔断/质量统计
            if not stream_ok:
                circuit_breaker.record_fail(key)
                quality_tracker.record(key, "fail")
            elif not got_content:
                # "假成功": 流格式正常但整条没有一个内容/工具调用块。
                # 实测 nemotron 对超大上下文就是秒回空消息, 质量分却一直
                # 记成功霸占排序, 客户端(Hermes)只能无限重试。记失败让它降权。
                logger.warning("upstream %s/%s returned empty stream (no content/tool_calls), marking fail",
                               provider.name, model)
                circuit_breaker.record_fail(key)
                quality_tracker.record(key, "fail")
            # 注意:finally 中不能 yield,否则抛 RuntimeError
            try:
                await resp.aclose()
            except Exception:
                pass

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
    "z-ai/glm-5.2": 1048576,  # NVIDIA 平台的智谱 GLM-5.2(与 glm-5.2 同型号)
    "ZhipuAI/GLM-5.2": 1048576,
    "deepseek-v4-flash": 131072,  # SenseNova / AMD 等短名
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
    "nvidia/nemotron-nano-12b-v2-vl": 131072,
    "nvidia/llama-3.1-nemotron-nano-vl-8b-v1": 131072,
    "meta/llama-3.2-11b-vision-instruct": 131072,
    "meta/llama-3.2-90b-vision-instruct": 131072,
    "microsoft/phi-3-vision-128k-instruct": 131072,
}

# 大小写不敏感的查找表: 不同平台写法的模型名(大小写/前缀不同)也能命中
_CONTEXT_LIMITS_LOWER: dict[str, int] = {
    k.lower(): v for k, v in CONTEXT_LIMITS.items()
}


def get_context_length(model: str) -> int:
    """返回模型上下文长度。三档兜底: 精确名 → 短名(去平台前缀) → 忽略大小写。"""
    if model in CONTEXT_LIMITS:
        return CONTEXT_LIMITS[model]
    v = _CONTEXT_LIMITS_LOWER.get(model.lower())
    if v is not None:
        return v
    short = model.rsplit("/", 1)[-1]
    v = _CONTEXT_LIMITS_LOWER.get(short.lower())
    if v is not None:
        return v
    return 32768





def _sort_by_strategy(
    raw: list,
    strategy: str = "quality",
    rr_key: str | None = None,
) -> list:
    """按策略排序候选列表: quality / fastest / stable。rr_key 非空时轮询旋转健康候选。"""
    from backend.core.scheduler import quality_tracker
    from backend.core.circuit import circuit_breaker

    # 收集每个候选的分数
    scored = []
    for idx, (p, m, k) in enumerate(raw):
        score = quality_tracker.get_score(k)
        # 无该模型记录时, 用同渠道已知模型延迟估计(按模型排序, 渠道慢则其新模型自动靠后)
        latency = (quality_tracker.get_avg_latency(k)
                   or quality_tracker.avg_latency_for_provider(p.name)
                   or 1e9)
        is_open = circuit_breaker.is_open(k)
        scored.append((p, m, k, score, latency, is_open, idx))

    if strategy == "fastest":
        # 延迟最低优先, 熔断中的最后; 同分同时按声明顺序(组序)
        scored.sort(key=lambda x: (1 if x[5] else 0, x[4], x[6]))
    elif strategy == "stable":
        # 成功率最高优先, 同分按延迟, 再按声明顺序(组序/渠道序)
        scored.sort(key=lambda x: (1 if x[5] else 0, -x[3], x[4], x[6]))
    else:  # quality (默认)
        # 质量分最高优先, 同分按延迟, 再按声明顺序(组序/渠道序)
        scored.sort(key=lambda x: (1 if x[5] else 0, -x[3], x[4], x[6]))

    if settings.ROUND_ROBIN_ENABLED and rr_key:
        scored = _rotate_candidates(scored, rr_key)

    return [(p, m, k) for p, m, k, _, _, _, _ in scored]


def is_1m_model(model: str) -> bool:
    return get_context_length(model) >= 1048576