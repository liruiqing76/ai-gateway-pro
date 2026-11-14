# ============================================================
# 智能网关 — 代理转发引擎
# OpenAI 兼容 /v1/chat/completions 和 /v1/models
# 从原版 app.py 重构,适配多用户
# ============================================================

import json
import random
import re
import time
import uuid
import logging
from datetime import datetime, timedelta
from typing import AsyncGenerator

import httpx

from backend.config import settings
from backend.core.circuit import circuit_breaker
from backend.core.scheduler import quality_tracker
from backend.core.shaping import traffic_shaping
from backend.models import Provider

logger = logging.getLogger("gateway.proxy")

# 配额/余额耗尽类 429(insufficient_quota / insufficient balance / entitlement)——
# 短期不会自动恢复(充值/重置额度前一直失败), 触发比一般限流更长的冷却;
# 一般限流(过载/被限)仍走指数退避 30s→300s。
_QUOTA_HINTS = ("insufficient_quota", "quota", "balance", "entitlement", "额度", "余额",
                # OpenCode 免费车道当日额度用尽: {"type":"FreeUsageLimitError",
                # "message":"Free usage limit reached. Subscribe to OpenCode Go ..."}
                "freeusagelimiterror", "free usage limit",
                # Kilo 免密车道的免费推广额度: {"code":"PROMOTION_MODEL_LIMIT_REACHED",
                # "message":"Sign up for free to continue and explore 500 other models."}
                "promotion_model_limit_reached")


def _is_quota_error(text: str) -> bool:
    """429 响应体是否属于配额/余额耗尽(而非瞬时过载限流)。"""
    t = (text or "").lower()
    return any(h in t for h in _QUOTA_HINTS)


# 白嫖渠道(登录型)的 Provider 名 —— 它们的 401/403 一律按"凭据过期"处理,
# 提示用户去「免费渠道」页重新登录, 而不是丢一个 upstream 401 让人猜。
_CHANNEL_NAMES = frozenset({
    "codearts", "workbuddy", "buddy", "cline", "trae",
    "qodercn", "qoder", "opencode", "kilo", "minimax",
})

# 凭据过期的**文案特征** —— 有些上游把"凭据过期"报成 400 而不是 401:
#   华为云 APIG: 400 {"error_msg":"Bad request: the security token has expired",
#                      "error_code":"APIG.0602"}
#   华为云 STS : 400 {"error_code":"STS5.1806","error_msg":"invalid refresh token:
#                      'the refresh token has been used'"}
#   Cline      : 401 "Please make sure you're using the latest version of Cline
#                      and re-authenticate your Cline account."
# 这些都必须给出"重新登录"的可操作提示, 且绝不能记成死模型(会 24h 拉黑好模型)。
_CRED_EXPIRED_HINTS = (
    "security token has expired",
    "the security token has expired",
    "apig.0602",
    "sts5.1806",
    "invalid refresh token",
    "token has been used",
    "re-authenticate",
    "reauthenticate",
    "please re-login",
    "please login again",
    "credentials expired",
    "credential has expired",
    "refresh token expired",
    "refresh token has expired",
)


def _is_cred_expired(status: int, text: str) -> bool:
    """上游错误是否属于"渠道凭据过期/失效"(需要用户重新登录)。"""
    if status not in (400, 401, 403):
        return False
    t = (text or "").lower()
    return any(h in t for h in _CRED_EXPIRED_HINTS)


# 模型级确定性 400 —— 模型名/ID 在个上游不存在、无服务商支持、未开通:
# 是确定性状态(不像上下文超限/字段不支持这类请求侧 400 会随请求内容变化),
# 与 404/410 一样按"死模型"处理, 连续出现多次后 24h 长冷却并自动移除。
# 注意排除请求侧 400: 上下文超限(context length)、max_tokens 超限等,
# 这些不能算模型死掉, 否则会误杀健康模型。
_DEAD_MODEL_400_HINTS = (
    "has no provider supported",
    "no provider supported",
    "provider not found",
    "invalid model",
    "model not found",
    "model does not exist",
    "model is not supported",
    "model not supported",
    "unknown model",
    "not supported by this deployment",
    "no such model",
    "undefined model",
)
# 请求侧 400(模型本身健康, 请求内容问题) —— 命中则不算死模型
_REQUEST_400_HINTS = (
    "context length",
    "context_length",
    "maximum context",
    "max_tokens",
    "too many tokens",
    "exceeds maximum",
    "prompt is too long",
    "input is too long",
    "stream_options",
)


def _is_dead_model_400(text: str) -> bool:
    """400 响应体是否属于"模型不可用"确定性错误(而非请求内容问题)。"""
    t = (text or "").lower()
    if any(h in t for h in _REQUEST_400_HINTS):
        return False
    return any(h in t for h in _DEAD_MODEL_400_HINTS)


# 账号等级限制: 模型本身存在, 但当前账号类型(个人免费版)不允许用。
# 实测 workbuddy: {"code":11158,"msg":"model [gpt-6-luna] is not allowed for
# user type [personal_free_user]","displayMsg":{"en":"Your account type does not
# support thi..."}}。这跟"请求内容有问题"无关, 也不会自愈 —— 每次落到这个模型都
# 白费一个候选, 必须像死模型一样冷却并移出, 否则用户每次调用都要多等一轮。
_TIER_BLOCK_HINTS = (
    "not allowed for user type",
    "account type does not support",
    "not supported for your account",
    "upgrade your plan to use this model",
    "个人免费版",
    "账号类型不支持",
)


def _is_tier_block_400(status: int, text: str) -> bool:
    if status != 400:
        return False
    t = (text or "").lower()
    return any(h in t for h in _TIER_BLOCK_HINTS)


def _tier_reason(text: str) -> str:
    """账号等级限制的可读文案 —— 换模型, 不是重登录也不是等额度。"""
    return "该模型不支持当前账号等级(如个人免费版), 已自动跳过, 请换模型"


# 上游"暂时不可用"的 400/403 —— 既不是死模型也不是请求错, 属于瞬时限流/会话
# 上限/权益耗尽。不能记 24h 死模型(会误杀好模型), 也不该当成请求侧错误静默。
#   codearts TM.00001041 并发会话数已达上限(3个)  → 并发槽满, 稍后重试即可
#   codearts TM.000010051 当前所选模型的使用权益已耗尽 → 当日权益耗尽
#   codearts InferHub.4004.200 benefit not found       → 免费额度模型未开通
_UPSTREAM_BUSY_HINTS = (
    "并发会话数已达上限",
    "concurrent session",
    "使用权益已耗尽",
    "benefit not found",
    "inferhub.4004.200",
    "tm.00001041",
    "tm.000010051",
    "billing daily count exceeded",
    "isqueued",
    "10605",
)


def _is_upstream_busy(status: int, text: str) -> bool:
    """上游因并发/权益/排队暂时拒绝(换候选或稍后重试, 不判死)。"""
    if status not in (400, 403, 409, 429):
        return False
    t = (text or "").lower()
    return any(h in t for h in _UPSTREAM_BUSY_HINTS)


# 地区封锁: 模型对当前出口地区不开放(不是凭据问题, 也不是模型死掉)。
# 实测 opencode `beam-free` → 403 {"type":"error","error":{"type":"RegionError",
# "message":"This model is not available in your country"}}。
# 免密车道根本没有"登录"可做, 若落到渠道 401/403 分支会提示"请重新登录该渠道",
# 把用户引到死路 —— 必须单独识别, 给"换模型"的可操作文案。
_REGION_HINTS = (
    "regionerror",
    "not available in your country",
    "not available in your region",
    "unsupported region",
    "region not supported",
    "地区不支持",
)


def _is_region_blocked(status: int, text: str) -> bool:
    if status not in (400, 403):
        return False
    t = (text or "").lower()
    return any(h in t for h in _REGION_HINTS)


def _region_reason(text: str) -> str:
    """地区封锁的可读文案 —— 用户能做的是换模型, 不是重新登录。"""
    return "该模型在当前出口地区不可用, 请换模型"


def _busy_reason(text: str) -> str:
    """把上游"暂时忙"原文压成一句可读原因(给用户看, 不再是裸 upstream 400)。"""
    t = (text or "").lower()
    if "并发会话数已达上限" in t or "concurrent session" in t:
        return "上游并发会话已满(CodeArts 限 3 个), 请稍后重试"
    if "使用权益已耗尽" in t or "billing daily count exceeded" in t:
        return "该模型今日权益已耗尽, 请换模型或明天再试"
    if "benefit not found" in t or "inferhub.4004.200" in t:
        return "免费额度模型未开通(benefit not found), 请换模型"
    if "isqueued" in t or "10605" in t:
        return "上游排队中, 请稍后重试"
    return "上游暂时繁忙, 请稍后重试"


def _quota_reason(text: str, retry_after: int | None = None) -> str:
    """配额/余额耗尽的 429 压成可读原因 —— 用户需要知道"等"还是"换模型"。

    `retry_after`(秒)来自上游响应头, 有它就能把"明天再试"变成
    "上游将在 08:00 恢复(约 7 小时)" —— opencode 免费额度就是这个语义。
    """
    t = (text or "").lower()
    when, dur = _retry_after_phrase(retry_after)
    if "freeusagelimiterror" in t or "free usage limit" in t:
        if when:
            return f"该渠道免费额度已用尽, 上游将在 {when}{dur} 恢复"
        return "该渠道今日免费额度已用尽, 请换模型或明天再试"
    if "billing daily count exceeded" in t:
        return "该渠道今日调用额度已用尽, 请换模型或明天再试"
    if "promotion_model_limit_reached" in t or "promotion" in t:
        return "该渠道的免费推广额度已用尽, 请注册该渠道账号或稍后再试"
    if "insufficient_quota" in t or "quota" in t or "额度" in t:
        if when:
            return f"该渠道配额已用尽, 上游将在 {when}{dur} 恢复"
        return "该渠道配额已用尽, 请换模型或充值"
    if "balance" in t or "余额" in t:
        return "该渠道余额不足, 请充值"
    if when:
        return f"该渠道额度已用尽, 上游将在 {when}{dur} 恢复"
    return "该渠道额度已用尽, 请稍后或换模型重试"


def _retry_after_phrase(retry_after: int | None) -> tuple[str, str]:
    """把 retry-after 秒数变成 (恢复时刻 'HH:MM', 剩余时长 '(约 7 小时 25 分后)')。"""
    if not retry_after or retry_after <= 0:
        return "", ""
    try:
        resume = datetime.now().astimezone() + timedelta(seconds=retry_after)
        when = resume.strftime("%H:%M")
        hours, mins = divmod(retry_after // 60, 60)
        if hours:
            dur = f"(约 {hours} 小时 {mins} 分后)"
        else:
            dur = f"(约 {mins} 分钟后)"
        return when, dur
    except Exception:
        return "", ""


def _note_lane_resume(channel: str, retry_after: int | None) -> None:
    """把"上游额度何时恢复"记进车道状态, 界面上就能显示倒计时。

    只对变换渠道(免密/协议变换车道)有意义; 落盘失败绝不能影响主流程。
    """
    if not retry_after or retry_after <= 0:
        return
    try:
        from backend.core import lane_state
        from backend.core.channel_auth.channel_transform import is_transformed_channel
        if not is_transformed_channel(channel):
            return
        lane_state.note_quota_resume(channel, retry_after)
    except Exception:  # pragma: no cover - 纯展示信息
        pass


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


def _sample_weighted(n: int, weights: list, rng: random.Random) -> list:
    """按权重不放回抽样, 返回索引序列(权重为 0/无效的位置自然沉底)。"""
    idxs = list(range(n))
    ws = list(weights)
    order = []
    for _ in range(n):
        total = sum(ws)
        if total <= 0:
            order.extend(idxs)
            break
        r = rng.random() * total
        acc = 0.0
        pick = idxs[-1]
        for i in idxs:
            acc += ws[i]
            if r <= acc:
                pick = i
                break
        order.append(pick)
        idxs.remove(pick)
        ws[pick] = 0.0
    return order


def _spread_weights(scored: list, strategy: str) -> tuple[list, list, list]:
    """计算健康候选的抽样权重, 返回 (健康候选, 权重列表, 熔断候选)。

    权重 = 基准分(按策略: quality/stable 用质量分, fastest 用延迟倒数)
           × 今日用量平滑(调用越少越优先, 免费额度日均摊开)
           × 渠道冷却权重(429/配额耗尽的渠道几乎不吃流量, 但不硬禁用)。
    熔断中的候选不参与抽样, 固定沉底当最后兜底。
    """
    from backend.core.circuit import circuit_breaker

    healthy = [x for x in scored if not x[5]]
    broken = [x for x in scored if x[5]]
    weights = []
    for p, m, k, score, latency, is_open, idx in healthy:
        if strategy == "fastest":
            base = 1e3 / (latency + 1e-3) if latency else 0.5
        else:
            # 质量策略: 正分按分加权摊开; 0 分(无记录)给探索权重;
            # 负分(连败)压到 1e-3 —— 只留一线机会, 不硬排除
            s = score or 0.0
            base = min(1.5, s) if s > 0 else (0.3 if s == 0.0 else 1e-3)
        today = quality_tracker.get_today_usage(k)
        quota_f = 1.0 / (1.0 + today * settings.SPREAD_QUOTA_BIAS)
        prov_f = circuit_breaker.provider_weight(p.id)
        weights.append(max(1e-6, base * quota_f * prov_f))
    return healthy, weights, broken


# 加权分发用的随机源(测试可替换)
_rng = random.Random()
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


def pick_fallback_models(
    providers: list[Provider],
    requested_model: str | None,
    routers: dict | None,
    user_aliases: dict | None = None,
) -> list[tuple[Provider, str]]:
    """兜底候选(主候选全失败时用)。

    关键: **路由组必须保持分组隔离**。当 requested_model 是路由组名(如
    `model=qodercn`)时, 兜底只允许该组内的渠道, 否则组内模型全失败会跨到别的
    渠道, 用户拿到的就不再是自己选的那个分组了(实测 qodercn 组曾命中 Agnes)。

    另外 `渠道名-模型名` 这种显式指定渠道的请求, 兜底也只在该渠道内换模型 ——
    用户点明了要用 codearts 的 GLM-5.2, 悄悄换成 OpenRouter 的免费模型属于
    "答非所问"(实测 codearts-GLM-5.2 兜底落到了 OpenRouter)。
    """
    if requested_model:
        from backend.core.router_group import normalize_group_name
        group_key = normalize_group_name(requested_model)
        # 1) 组名请求: 限定在组内渠道
        if routers and group_key in routers:
            group_models = set(routers.get(group_key) or [])
            group_channels = {
                p.name for p in providers
                if any(f"{p.name}-{m}" in group_models for m in (p.models or []))
            }
            if group_channels:
                return pick_available_models(
                    [p for p in providers if p.name in group_channels],
                    None, routers, user_aliases=user_aliases,
                )
        # 2) `渠道名-模型名` 显式请求: 限定在该渠道内
        for p in providers:
            if not requested_model.startswith(f"{p.name}-"):
                continue
            if requested_model[len(p.name) + 1:] not in (p.models or []):
                continue
            return pick_available_models(
                [p], None, routers, user_aliases=user_aliases)
    return pick_available_models(providers, None, routers, user_aliases=user_aliases)


# ============================================================
# 非流式转发
# ============================================================
def _merge_headers_ci(base: dict, extra: dict) -> dict:
    """大小写不敏感地合并头: extra 覆盖 base 里同名(任意大小写)的键。

    httpx 会把 "Content-Type" 与 "content-type" 当成两个头合并成
    "application/json,application/json" 发出去 —— 华为 APIG 按合并后的值验签,
    会恒定 401 verify ak sk signature fail(实测)。签名头是小写键, 基础头是大写,
    因此必须在这里按名覆盖而不是直接 update。
    """
    if not extra:
        return base
    merged = dict(base)
    lower_map = {k.lower(): k for k in merged}
    for k, v in extra.items():
        old = lower_map.get(k.lower())
        if old is not None:
            merged[old] = v
        else:
            merged[k] = v
            lower_map[k.lower()] = k
    return merged


# 只接受流式上行的渠道: 非流式请求会被上游直接拒绝
# (实测 workbuddy/buddy 返回 {"code":11101,"msg":"Non-stream chat request is
#  currently not supported"})。这类渠道在 forward_non_stream 里把 stream 打开,
# 再把 SSE 聚合回非流式结构。
STREAM_ONLY_CHANNELS = {"workbuddy", "buddy"}

# 需要「渠道校验」二次加固的渠道: WorkBuddy 国际版网关除了要会话 id 三件套
# (见 channel_auth.workbuddy.conversation_headers), 还要求
#   1) 首条消息必须是 system, 否则 400 / code 11128(伪装成安全拦截)
#   2) body 带 prompt_cache_key(缺了缓存命中恒为 0, 且部分模型直接拒)
#   3) deepseek 系模型带 thinking:{type:"enabled"}
#   4) **system 正文里不能出现被上游列黑的 agent 指纹子串** —— 同样回 11128。
#      这一条**没法靠枚举词表解决**: 实测 Claude Code 触发它的不是自己的开场白,
#      而是 system 里一行看似无关的 git 信息
#      `Main branch (you will usually use this for PRs): master`。
#      上游做的是"大小写不敏感、任意位置的精确子串匹配", 黑名单未知且会变, 所以
#      照 dsh-connect-workbuddy 的思路做**盲扰动重试**: 被 11128 拒时往 system
#      正文里插零宽空格打碎字节序列, 模型读到的内容不变。
BUDDY_GATE_CHANNELS = {"workbuddy", "buddy"}
_BUDDY_FALLBACK_SYSTEM = "You are a helpful assistant."

_ZWSP = "\u200b"
# 插件 perturbChannelFingerprints 的 sparse 规则(标点后 + 大写词之间)
_SPARSE_AFTER_PUNCT = re.compile(r"([.!?])(\s+)")
_SPARSE_AFTER_CJK = re.compile(r"([。！？])(?!\u200b)")
_SPARSE_AFTER_COMMA = re.compile(r"([,;])(\s+)")
_SPARSE_WORD_BREAK = re.compile(r"([A-Z][A-Za-z0-9]*)(\s+)(?=[A-Z][A-Za-z0-9])")
_CJK_OR_ALNUM = re.compile(r"[A-Za-z0-9\u3040-\u30ff\u4e00-\u9fff]")


def _perturb_sparse(text: str) -> str:
    """轻度扰动: 只在标点边界与大写词之间插零宽空格(约 +2% 长度)。"""
    if not isinstance(text, str) or not text:
        return text
    out = _SPARSE_AFTER_PUNCT.sub(lambda m: m.group(1) + _ZWSP + m.group(2), text)
    out = _SPARSE_AFTER_CJK.sub(lambda m: m.group(1) + _ZWSP, out)
    out = _SPARSE_AFTER_COMMA.sub(lambda m: m.group(1) + _ZWSP + m.group(2), out)
    out = _SPARSE_WORD_BREAK.sub(lambda m: m.group(1) + _ZWSP + m.group(2), out)
    return out


def _perturb_dense(text: str) -> str:
    """重度扰动: 每个非空白字符前都插零宽空格(约 +70% 长度)。

    插件阶梯的最后一级。实测能把 `Main branch (you will usually use this for
    PRs): master` 这类"没有标点边界也没有大写词对"的句子也打碎 —— sparse 对它
    一个字都不加(实测 +0 字符, 仍被拦), dense 加 42 字符后通过。
    """
    if not isinstance(text, str) or not text:
        return text
    base = _perturb_sparse(text)
    out: list[str] = []
    prev: str | None = None
    for ch in base:
        if ch.isspace():
            out.append(ch)
            continue
        if prev is not None and _CJK_OR_ALNUM.match(prev):
            out.append(_ZWSP)
        out.append(ch)
        prev = ch
    return "".join(out)


def _perturb_buddy_body(req_body: dict, dense: bool = False) -> dict:
    """就地扰动 system 正文(只动 system, 不动 user/assistant 历史)。"""
    messages = req_body.get("messages")
    if not isinstance(messages, list):
        return req_body
    fn = _perturb_dense if dense else _perturb_sparse
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        # 只扰动 system: 动 user 消息会改变模型对用户原话的理解。
        if str(msg.get("role", "")).strip().lower() != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = fn(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    part["text"] = fn(part["text"])
    return req_body


def _buddy_system_text(req_body: dict) -> str:
    """把 body 里所有 system 文本拼起来(只用于判断扰动有没有真的改动)。"""
    messages = req_body.get("messages")
    if not isinstance(messages, list):
        return ""
    chunks: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role", "")).strip().lower() != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
    return "\n".join(chunks)


def _is_buddy_gate_reject(status: int, text: str) -> bool:
    """是否是 WorkBuddy/CodeBuddy 的渠道校验拒绝(code 11128)。"""
    if status != 400 or not text:
        return False
    low = text.lower()
    return "11128" in low or "unapproved channel" in low


def _prepare_buddy_body(req_body: dict) -> dict:
    """按 WorkBuddy/CodeBuddy 渠道契约补全请求体。

    照抄 dsh-connect-workbuddy 的 ensureSystemHead + BuddyAdapter body 构造:
      - 首条消息不是 system 时补一条占位 system(上游 11128 硬要求);
      - prompt_cache_key 用现场生成的 32 位 hex(前缀缓存命中, 实测费用差 ~17 倍);
      - deepseek 系模型补 thinking:{type:"enabled"}。

    注意: **这里不做指纹扰动**。扰动会让 prompt 前缀缓存失效, 所以只在真的被
    11128 拒绝之后由调用方升级重试(_perturb_buddy_body)。
    """
    messages = req_body.get("messages")
    if isinstance(messages, list):
        head = messages[0] if messages else None
        is_system = (isinstance(head, dict)
                     and str(head.get("role", "")).strip().lower() == "system")
        if not is_system:
            messages.insert(0, {"role": "system", "content": _BUDDY_FALLBACK_SYSTEM})
    req_body.setdefault("prompt_cache_key", uuid.uuid4().hex)
    model_name = str(req_body.get("model") or "")
    if model_name.startswith("deepseek"):
        req_body.setdefault("thinking", {"type": "enabled"})
    return req_body


def _aggregate_openai_sse(text: str) -> dict | None:
    """把 OpenAI 形状的 SSE 文本聚合成一个非流式 completion dict。

    上游"非流式"接口实际返回 SSE(实测 codearts 200 但 body 是 `data:{...}`),
    以及只能流式的渠道(workbuddy/buddy), 都要靠这里还原成完整消息。
    聚不出来(无任何 delta)返回 None, 由调用方判失败。
    """
    content: list[str] = []
    reasoning: list[str] = []
    calls: dict[int, dict] = {}
    finish_reason = None
    usage = None
    last_id = None
    last_model = None

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        # 上游把错误塞在 200 的 SSE 帧里: 不当正常内容吞掉
        if obj.get("error") or (obj.get("code") not in (None, 0, "0")
                               and not obj.get("choices")):
            raise RuntimeError(json.dumps(obj, ensure_ascii=False)[:300])
        last_id = obj.get("id") or last_id
        last_model = obj.get("model") or last_model
        if obj.get("usage"):
            usage = obj["usage"]
        for ch in obj.get("choices") or []:
            holder = ch.get("delta") or ch.get("message") or {}
            if (holder.get("content") or ""):
                content.append(holder["content"])
            if (holder.get("reasoning_content") or ""):
                reasoning.append(holder["reasoning_content"])
            for call in holder.get("tool_calls") or []:
                idx = call.get("index", len(calls))
                slot = calls.setdefault(idx, {"id": None, "type": "function",
                                              "function": {"name": "", "arguments": ""}})
                if call.get("id"):
                    slot["id"] = call["id"]
                fn = call.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if ch.get("finish_reason"):
                finish_reason = ch["finish_reason"]

    if not content and not reasoning and not calls:
        return None

    message: dict = {"role": "assistant", "content": "".join(content)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if calls:
        ordered = [calls[i] for i in sorted(calls)]
        for i, slot in enumerate(ordered):
            if not slot.get("id"):
                slot["id"] = f"call_{i}"
        message["tool_calls"] = ordered
        message["content"] = message["content"] or None
        finish_reason = finish_reason or "tool_calls"

    out = {
        "id": last_id or f"chatcmpl-agg-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": last_model or "",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish_reason or "stop"}],
    }
    if usage:
        out["usage"] = usage
    return out


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
    # 白嫖渠道(cline/workbuddy 等): api_key 是 JSON 凭据包, 附加头由渠道登录器给出
    # (含渠道自己的 Authorization; 覆盖上面的默认 Bearer)
    try:
        from backend.core.channel_auth import channel_headers
        extra = channel_headers(provider.name, api_key, model)
        if extra:
            headers = _merge_headers_ci(headers, extra)
    except Exception:
        pass

    req_body = json.loads(json.dumps(body))
    # 始终用去掉前缀的真实模型名发给上游
    req_body["model"] = MODEL_ALIASES.get(model, model)
    # 只吃流式的渠道: 上游明确拒绝非流式(workbuddy/buddy 返回 code 11101),
    # 这里把 stream 打开, 响应侧由 _aggregate_openai_sse 聚合回非流式结构。
    if provider.name in STREAM_ONLY_CHANNELS:
        req_body["stream"] = True
        req_body.pop("stream_options", None)
    # 渠道校验加固(workbuddy 11128: 首条消息必须 system + prompt_cache_key)
    if provider.name in BUDDY_GATE_CHANNELS:
        req_body = _prepare_buddy_body(req_body)

    # 流量整形: 模型级最小间隔(令牌桶) + 渠道并发槽(槽满超时则换候选)
    traffic_shaping.resolve_params(
        provider.id, getattr(provider, "free_only", True),
        getattr(provider, "max_concurrency", None),
        getattr(provider, "min_interval_ms", None),
    )
    await traffic_shaping.pace(key, provider.id)
    if not await traffic_shaping.acquire(provider.id):
        logger.warning("provider %s busy, skip %s", provider.name, model)
        return None, "provider busy"

    try:
        start = time.time()
        # ── 变换渠道(TRAE SOLO / Qoder 加密信封): 请求构造与响应解包
        #    由 channel_transform 接管, 产出标准 OpenAI dict ──
        from backend.core.channel_auth import (
            is_transformed_channel, channel_transform, CHANNEL_TIMEOUT_HINTS)
        if is_transformed_channel(provider.name):
            t = (timeout_seconds or CHANNEL_TIMEOUT_HINTS.get(provider.name)
                 or settings.PROXY_TIMEOUT_SECONDS)
            try:
                data = await channel_transform.transformed_non_stream(
                    provider.name, api_key, model, req_body, http_client, t)
            except channel_transform.TransformUpstreamError as e:
                quality_tracker.record(key, "fail")
                if e.status == 429:
                    is_quota = _is_quota_error(e.text)
                    circuit_breaker.record_provider_rate_limit(provider.id, quota=is_quota)
                    if is_quota:
                        circuit_breaker.record_quota_exhausted(key)
                        _note_lane_resume(provider.name, e.retry_after)
                        return None, _quota_reason(e.text, e.retry_after)
                    circuit_breaker.record_rate_limit(key)
                elif _is_cred_expired(e.status, e.text):
                    circuit_breaker.record_fail(key)
                    return None, "渠道凭据已过期或失效, 请到「免费渠道」页重新登录"
                elif e.status == 400 and _is_dead_model_400(e.text):
                    circuit_breaker.record_not_found(key, code=400)
                    return None, "upstream 400 (dead model)"
                elif _is_tier_block_400(e.status, e.text):
                    # 账号等级限制(如个人免费版不支持该模型): 确定性, 冷却并移出
                    circuit_breaker.record_not_found(key, code=400)
                    return None, _tier_reason(e.text)
                elif _is_upstream_busy(e.status, e.text):
                    # TRAE/Qoder 的 4008 配额、4011 软限流、10605 排队都归这里
                    circuit_breaker.record_fail(key)
                    return None, _busy_reason(e.text)
                elif _is_region_blocked(e.status, e.text):
                    # 地区封锁(如 opencode beam-free 的 403 RegionError):
                    # 不是凭据问题, 换模型即可; 免密车道没有"重新登录"可做
                    circuit_breaker.record_fail(key)
                    return None, _region_reason(e.text)
                elif e.status in (401, 403):
                    circuit_breaker.record_fail(key)
                    return None, "渠道凭据已过期或失效, 请到「免费渠道」页重新登录"
                else:
                    circuit_breaker.record_fail(key)
                return None, f"upstream {e.status}"
            latency = (time.time() - start) * 1000
            msg = ((data.get("choices") or [{}])[0].get("message") or {})
            if not ((msg.get("content") or "").strip() or msg.get("tool_calls")
                    or (msg.get("reasoning_content") or "").strip()):
                logger.warning("transformed %s/%s empty response, trying next",
                               provider.name, model)
                circuit_breaker.record_fail(key)
                quality_tracker.record(key, "fail", latency)
                return None, "upstream empty response"
            quality_tracker.record(key, "ok", latency)
            circuit_breaker.record_success(key)
            circuit_breaker.record_provider_success(provider.id)
            data["model"] = f"{provider.name} · {model}"
            return data, None

        # 签名渠道(CodeArts 华为云 HMAC)必须对最终 body bytes 现算签名,
        # 因此统一走"序列化 → 签名 → content= 发送"路径(非签名渠道等价于 json=)
        async def _post_non_stream(rb: dict) -> httpx.Response:
            body_bytes = json.dumps(rb, ensure_ascii=False).encode("utf-8")
            send_headers = headers
            try:
                from backend.core.channel_auth import channel_signed_headers
                signed = channel_signed_headers(provider.name, api_key, "POST", url, body_bytes)
                if signed:
                    send_headers = _merge_headers_ci(headers, signed)
            except Exception:
                pass
            return await http_client.post(
                url, content=body_bytes, headers=send_headers,
                # 非流式同流式口径: 连接后等首字节超过 PROXY_NONSTREAM_READ_TIMEOUT
                # 即放弃该候选换下一个。之前用 PROXY_TIMEOUT_SECONDS 扁平整体超时,
                # 挂死上游会把单次尝试拖满 120s; 调用方显式传 timeout_seconds
                # (如视觉子调用快速失败)则保留原先"整体超时"语义。
                timeout=(httpx.Timeout(total=timeout_seconds)
                         if timeout_seconds is not None
                         else httpx.Timeout(
                             connect=15,
                             read=settings.PROXY_NONSTREAM_READ_TIMEOUT,
                             write=30,
                             pool=10,
                         )),
            )

        resp = await _post_non_stream(req_body)
        # WorkBuddy/CodeBuddy 渠道校验(11128): 黑名单是未知的精确子串, 只能靠
        # 盲扰动重试 —— 先 sparse(便宜, +2%), 再 dense(+70%)。见 _perturb_dense。
        # 某一级扰动若一个字都没改(整段既无标点边界也无大写词对), 跳过不重试。
        if (provider.name in BUDDY_GATE_CHANNELS
                and _is_buddy_gate_reject(resp.status_code, resp.text)):
            for dense in (False, True):
                retry_body = _perturb_buddy_body(json.loads(json.dumps(req_body)), dense=dense)
                if _buddy_system_text(retry_body) == _buddy_system_text(req_body):
                    logger.warning(
                        "workbuddy gate 11128 from %s/%s: %s perturbation changed nothing,"
                        " escalating", provider.name, model,
                        "dense" if dense else "sparse")
                    continue
                await resp.aclose()
                logger.warning("workbuddy gate 11128 from %s/%s, retrying with %s perturbation",
                               provider.name, model, "dense" if dense else "sparse")
                resp = await _post_non_stream(retry_body)
                if not _is_buddy_gate_reject(resp.status_code, resp.text):
                    break
        latency = (time.time() - start) * 1000

        if resp.status_code >= 400:
            logger.warning("upstream %d from %s: %s", resp.status_code, provider.name, resp.text[:200])
            quality_tracker.record(key, "fail", latency)
            # 地区封锁优先判: 免密车道(opencode 等)根本没有登录可做,
            # 落到下面的"请重新登录该渠道"会把用户引到死路。
            if _is_region_blocked(resp.status_code, resp.text):
                circuit_breaker.record_fail(key)
                return None, _region_reason(resp.text)
            # 白嫖渠道的 401/403 基本都是凭据过期(WorkOS JWT 1h、华为云 STS 1.5h),
            # 直接回 "upstream 401" 用户看不懂也不知道要做什么 —— 给出可操作文案。
            if resp.status_code in (401, 403) and provider.name in _CHANNEL_NAMES:
                circuit_breaker.record_fail(key)
                return None, "渠道凭据已过期或失效, 请到「免费渠道」页重新登录"
            # 429: 配额/余额耗尽走长效冷却, 一般限流走指数退避; 不再叠加 record_fail
            # (record_fail 会以 60s 覆盖掉刚才设置的更长冷却, 让配额冷却形同虚设)
            if resp.status_code == 429:
                is_quota = _is_quota_error(resp.text)
                circuit_breaker.record_provider_rate_limit(provider.id, quota=is_quota)
                if is_quota:
                    circuit_breaker.record_quota_exhausted(key)
                    return None, _quota_reason(resp.text)
                circuit_breaker.record_rate_limit(key)
            # 其他 4xx 是确定性失败(上下文超限/不支持字段/鉴权等), 同样计入熔断:
            # 其他 4xx: 模型级 400(无服务商支持/模型不存在等)是确定性"死模型",
            # 走 24h 长冷却并标记, 由 api 层从路由组移除+永久拉黑; 其余 4xx
            # (上下文超限/不支持字段/鉴权等)走普通熔断 60s, 请求本身的问题
            # 不能连坐模型本身。
            else:
                # 凭据过期优先判: 上游常把它报成 400(APIG.0602 / STS5.1806),
                # 必须先于"死模型"判定, 否则会把好模型记进 24h 黑名单。
                if _is_cred_expired(resp.status_code, resp.text):
                    circuit_breaker.record_fail(key)
                    return None, "渠道凭据已过期或失效, 请到「免费渠道」页重新登录"
                if resp.status_code == 400 and _is_dead_model_400(resp.text):
                    circuit_breaker.record_not_found(key, code=400)
                    return None, "upstream 400 (dead model)"
                # 账号等级限制(个人免费版不支持该模型): 确定性, 冷却并移出
                if _is_tier_block_400(resp.status_code, resp.text):
                    circuit_breaker.record_not_found(key, code=400)
                    return None, _tier_reason(resp.text)
                # 并发槽满/当日权益耗尽/排队中: 是"暂时忙", 不能记死模型
                # (记了会 24h 拉黑一个好模型), 用普通熔断让候选轮转走。
                if _is_upstream_busy(resp.status_code, resp.text):
                    circuit_breaker.record_fail(key)
                    return None, _busy_reason(resp.text)
                circuit_breaker.record_fail(key)
            return None, f"upstream {resp.status_code}"

        try:
            try:
                data = resp.json()
                # cline 的**非流式**响应外面多包一层 {"data": {...}} 信封
                # (实测 POST api.cline.bot/api/v1/chat/completions stream=false
                #  → {"data":{"choices":[...]}})。不拆壳会被下面的空回复防护
                # 判成 "upstream empty response" 并连坐熔断一个健康候选。
                if (provider.name == "cline" and isinstance(data, dict)
                        and isinstance(data.get("data"), dict)
                        and data["data"].get("choices") and not data.get("choices")):
                    data = data["data"]
            except json.JSONDecodeError:
                # 上游"非流式"却回了 SSE: codearts 的 200 body 是 `data:{...}`
                # 单帧; workbuddy/buddy 只能流式(下面强制 stream:true)。两种都
                # 在这里聚合成非流式 completion, 否则会被误判 "upstream non-json"
                # 并连坐熔断一个健康的候选。
                data = _aggregate_openai_sse(resp.text)
                if data is None:
                    logger.warning("upstream non-json from %s: %s",
                                   provider.name, resp.text[:200])
                    circuit_breaker.record_fail(key)
                    quality_tracker.record(key, "fail")
                    return None, "upstream non-json"
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
            circuit_breaker.record_provider_success(provider.id)
            data["model"] = f"{provider.name} · {model}"
            return data, None
        except json.JSONDecodeError:
            logger.warning("upstream non-json from %s: %s", provider.name, resp.text[:200])
            circuit_breaker.record_fail(key)
            quality_tracker.record(key, "fail")
            return None, "upstream non-json"

    except httpx.TimeoutException as e:
        # 挂死型超时: 连接成功但长时间无数据。除返回错误外累计指数退避
        # 冷却(90s 起, 连续超时封顶 30min), 避免挂死模型每次冷却结束
        # 又被路由到、每笔请求白烧一个超时(实测会把单条消息拖到 2 分钟)。
        logger.warning("upstream timeout from %s: %s", provider.name, e)
        circuit_breaker.record_timeout_fail(key)
        quality_tracker.record(key, "error")
        return None, f"upstream timeout: {e}"
    except httpx.RequestError as e:
        logger.warning("forward error to %s: %s", provider.name, e)
        circuit_breaker.record_fail(key)
        quality_tracker.record(key, "error")
        return None, str(e)
    except Exception as e:
        # 变换渠道的流内错误(qoder 信封里的 Billing daily count exceeded /
        # 10605 isQueued 等)在响应阶段才抛出, 走不到上面的 status 分支 ——
        # 这里同样按"暂时忙"识别, 别把好模型记成失败。
        msg = str(e)
        if _is_upstream_busy(400, msg) or _is_upstream_busy(429, msg):
            logger.warning("upstream busy from %s: %s", provider.name, msg[:200])
            circuit_breaker.record_fail(key)
            quality_tracker.record(key, "fail")
            return None, _busy_reason(msg)
        if _is_tier_block_400(400, msg):
            # 账号等级限制在流内错误里同样会出现(部分上游把 400 抛成异常)
            logger.warning("upstream tier block from %s: %s", provider.name, msg[:200])
            circuit_breaker.record_not_found(key, code=400)
            quality_tracker.record(key, "fail")
            return None, _tier_reason(msg)
        logger.exception("unexpected forward error to %s", provider.name)
        circuit_breaker.record_fail(key)
        quality_tracker.record(key, "error")
        return None, msg
    finally:
        traffic_shaping.release(provider.id)


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
    # 白嫖渠道(cline/workbuddy 等): api_key 是 JSON 凭据包, 附加头由渠道登录器给出
    try:
        from backend.core.channel_auth import channel_headers
        extra = channel_headers(provider.name, api_key, model)
        if extra:
            headers = _merge_headers_ci(headers, extra)
    except Exception:
        pass

    req_body = json.loads(json.dumps(body))
    req_body["model"] = MODEL_ALIASES.get(model, model)

    # 渠道校验加固(workbuddy 11128: 首条消息必须 system + prompt_cache_key)
    if provider.name in BUDDY_GATE_CHANNELS:
        req_body = _prepare_buddy_body(req_body)

    # 流式请求显式向上游索要 token 用量: OpenAI 兼容上游默认不在流里回传
    # usage, 必须带 stream_options.include_usage 才有; 否则网关只能字符估算,
    # 与官方计费口径偏差很大。个别上游不认识该字段会 400, 捕获后去掉重试。
    if req_body.get("stream"):
        so = req_body.get("stream_options")
        if isinstance(so, dict):
            so.setdefault("include_usage", True)
        else:
            req_body["stream_options"] = {"include_usage": True}

    # 流量整形: 模型级最小间隔 + 渠道并发槽(槽满超时则换候选)。
    # 流式请求占用槽位到流结束才释放, 防并发长流把上游 RPM 打爆。
    traffic_shaping.resolve_params(
        provider.id, getattr(provider, "free_only", True),
        getattr(provider, "max_concurrency", None),
        getattr(provider, "min_interval_ms", None),
    )
    await traffic_shaping.pace(key, provider.id)
    if not await traffic_shaping.acquire(provider.id):
        logger.warning("provider %s busy, skip %s", provider.name, model)
        return None, "provider busy"

    gen_handoff = {"taken": False}

    # ── 变换渠道(TRAE SOLO / Qoder 加密信封): 请求构造 + 响应解包由
    #    channel_transform 接管, 产出标准 OpenAI SSE 字节流; 本函数其余
    #    记账(空流降权/成功记录/释放并发槽)单独走一条精简路径 ──
    from backend.core.channel_auth import (
        is_transformed_channel, channel_transform, CHANNEL_TIMEOUT_HINTS)
    if is_transformed_channel(provider.name):
        timeout = (CHANNEL_TIMEOUT_HINTS.get(provider.name)
                   or settings.PROXY_TIMEOUT_SECONDS)
        try:
            inner = await channel_transform.transformed_stream(
                provider.name, api_key, model, req_body, http_client, timeout)
        except channel_transform.TransformUpstreamError as e:
            quality_tracker.record(key, "fail")
            if e.status == 429:
                is_quota = _is_quota_error(e.text)
                circuit_breaker.record_provider_rate_limit(provider.id, quota=is_quota)
                if is_quota:
                    circuit_breaker.record_quota_exhausted(key)
                    _note_lane_resume(provider.name, e.retry_after)
                    return None, _quota_reason(e.text, e.retry_after)
                circuit_breaker.record_rate_limit(key)
            elif _is_cred_expired(e.status, e.text):
                circuit_breaker.record_fail(key)
                return None, "渠道凭据已过期或失效, 请到「免费渠道」页重新登录"
            elif e.status == 400 and _is_dead_model_400(e.text):
                circuit_breaker.record_not_found(key, code=400)
                return None, "upstream 400 (dead model)"
            elif _is_tier_block_400(e.status, e.text):
                # 账号等级限制(个人免费版不支持该模型): 确定性, 冷却并移出
                circuit_breaker.record_not_found(key, code=400)
                return None, _tier_reason(e.text)
            elif _is_upstream_busy(e.status, e.text):
                circuit_breaker.record_fail(key)
                return None, _busy_reason(e.text)
            elif _is_region_blocked(e.status, e.text):
                # 地区封锁(如 opencode beam-free 的 403 RegionError)
                circuit_breaker.record_fail(key)
                return None, _region_reason(e.text)
            else:
                circuit_breaker.record_fail(key)
            return None, f"upstream {e.status}"
        except httpx.TimeoutException as e:
            logger.warning("transformed stream timeout from %s: %s", provider.name, e)
            circuit_breaker.record_timeout_fail(key)
            quality_tracker.record(key, "error")
            return None, f"upstream timeout: {e}"
        except httpx.RequestError as e:
            logger.warning("transformed stream error to %s: %s", provider.name, e)
            circuit_breaker.record_fail(key)
            quality_tracker.record(key, "error")
            return None, str(e)

        circuit_breaker.record_success(key)
        circuit_breaker.record_provider_success(provider.id)
        quality_tracker.record(key, "ok")

        async def transformed_gen():
            stream_ok = False
            got_content = False
            try:
                async for chunk in inner:
                    c = chunk.strip()
                    if c.startswith(b"data:") and c != b"data: [DONE]":
                        stream_ok = True
                        try:
                            obj = json.loads(c[5:].decode("utf-8", "replace"))
                            for ch2 in obj.get("choices") or []:
                                d = ch2.get("delta") or {}
                                if (d.get("content") or "").strip() \
                                        or d.get("tool_calls") or d.get("function_call") \
                                        or (d.get("reasoning_content") or "").strip():
                                    got_content = True
                                    break
                        except (ValueError, UnicodeDecodeError):
                            pass
                    elif c == b"data: [DONE]":
                        stream_ok = True
                    yield chunk
            except Exception as e:
                # 流内 error 事件(SOLO 4008/Qoder 信封错误): 断流交给
                # _stream_with_fallback 切候选, 不把原文错误透传给客户端
                logger.warning("transformed stream error from %s/%s: %s",
                               provider.name, model, e)
            finally:
                if not got_content:
                    # 空流/假成功: 降权熔断(与通用路径同语义)
                    logger.warning("transformed %s/%s empty stream, marking fail",
                                   provider.name, model)
                    circuit_breaker.record_fail(key)
                    quality_tracker.record(key, "fail")
                traffic_shaping.release(provider.id)

        gen_handoff["taken"] = True
        return transformed_gen(), None

    try:
        # 请求级超时: 读超时(首字节/相邻块间隔)超过 PROXY_STREAM_READ_TIMEOUT
        # 即放弃当前候选换下一个, 避免"慢模型卡满 120s 全局超时"
        req_timeout = httpx.Timeout(
            connect=15,
            read=settings.PROXY_STREAM_READ_TIMEOUT,
            write=30,
            pool=10,
        )
        # 签名渠道(CodeArts)对最终 body bytes 现算签名; stream_options 被拒
        # 去掉重试时 body 变了, 必须重新序列化+重新签名
        def _build_signed_request(rb: dict) -> httpx.Request:
            body_bytes = json.dumps(rb, ensure_ascii=False).encode("utf-8")
            send_headers = dict(headers)
            try:
                from backend.core.channel_auth import channel_signed_headers
                signed = channel_signed_headers(provider.name, api_key, "POST", url, body_bytes)
                if signed:
                    send_headers = _merge_headers_ci(send_headers, signed)
            except Exception:
                pass
            return http_client.build_request(
                "POST", url, content=body_bytes, headers=send_headers,
                timeout=req_timeout)

        req = _build_signed_request(req_body)
        resp = await http_client.send(req, stream=True)
        if resp.status_code == 400 and req_body.pop("stream_options", None) is not None:
            err_bytes = await resp.aread()
            await resp.aclose()
            logger.warning("upstream %s rejected stream_options (%s), retrying without it",
                           provider.name, err_bytes.decode('utf-8', errors='ignore')[:120])
            req = _build_signed_request(req_body)
            resp = await http_client.send(req, stream=True)

        # WorkBuddy/CodeBuddy 渠道校验(11128): 黑名单是未知的精确子串, 只能靠
        # 盲扰动重试 —— 先 sparse(便宜, +2%), 再 dense(+70%)。
        # aread() 有缓存, 下面通用错误分支再读一次也拿得到同一份 body。
        # 若某一级扰动对这段 system 一个字都没改(常见: 整段没有标点边界也没有
        # 大写词对, 如 `Main branch (you will usually use this for PRs)`), 直接
        # 跳到下一级, 不浪费一次往返。
        if provider.name in BUDDY_GATE_CHANNELS and resp.status_code == 400:
            probe = await resp.aread()
            for dense in (False, True):
                if not _is_buddy_gate_reject(resp.status_code,
                                             probe.decode("utf-8", "ignore")):
                    break
                await resp.aclose()
                retry_body = _perturb_buddy_body(
                    json.loads(json.dumps(req_body)), dense=dense)
                if _buddy_system_text(retry_body) == _buddy_system_text(req_body):
                    logger.warning(
                        "workbuddy gate 11128 from %s/%s: %s perturbation changed nothing,"
                        " escalating", provider.name, model,
                        "dense" if dense else "sparse")
                    continue
                logger.warning(
                    "workbuddy gate 11128 from %s/%s, retrying with %s perturbation",
                    provider.name, model, "dense" if dense else "sparse")
                resp = await http_client.send(
                    _build_signed_request(retry_body), stream=True)
                if resp.status_code == 400:
                    probe = await resp.aread()

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
                is_quota = _is_quota_error(err_msg)
                circuit_breaker.record_provider_rate_limit(provider.id, quota=is_quota)
                if is_quota:
                    circuit_breaker.record_quota_exhausted(key)
                    return None, _quota_reason(err_msg)
                circuit_breaker.record_rate_limit(key)
            elif _is_cred_expired(resp.status_code, err_msg):
                # 凭据过期(可能报成 400): 给可操作文案, 不记死模型。
                # 必须排在死模型判定之前 —— APIG.0602 / STS5.1806 都是 400。
                circuit_breaker.record_fail(key)
                return None, "渠道凭据已过期或失效, 请到「免费渠道」页重新登录"
            elif resp.status_code == 400 and _is_dead_model_400(err_msg):
                # 模型级确定性 400 → 死模型长冷却, 由 api 层移除+拉黑
                circuit_breaker.record_not_found(key, code=400)
                return None, "upstream 400 (dead model)"
            elif _is_tier_block_400(resp.status_code, err_msg):
                # 账号等级限制(如 workbuddy 11158 personal_free_user): 确定性,
                # 每次落到这个模型都白费一个候选, 与死模型同样长冷却并移出。
                circuit_breaker.record_not_found(key, code=400)
                return None, _tier_reason(err_msg)
            elif _is_upstream_busy(resp.status_code, err_msg):
                # 并发槽满/权益耗尽/排队: 暂时忙, 不记死模型
                circuit_breaker.record_fail(key)
                return None, _busy_reason(err_msg)
            elif _is_region_blocked(resp.status_code, err_msg):
                # 地区封锁: 换模型即可, 免密车道没有"重新登录"可做
                circuit_breaker.record_fail(key)
                return None, _region_reason(err_msg)
            elif resp.status_code in (401, 403) and provider.name in _CHANNEL_NAMES:
                # 白嫖渠道凭据过期: 给可操作文案, 别丢裸 upstream 401
                circuit_breaker.record_fail(key)
                return None, "渠道凭据已过期或失效, 请到「免费渠道」页重新登录"
            else:
                circuit_breaker.record_fail(key)
            return None, f"upstream {resp.status_code}"

        # 连接成功,创建共享迭代器避免重复消费httpx流
        circuit_breaker.record_success(key)
        circuit_breaker.record_provider_success(provider.id)
        quality_tracker.record(key, "ok")

        async def gen():
            done_sent = False
            first_line_yielded = False
            stream_ok = False
            got_content = False  # 是否出现过有效内容(回答文本/工具调用)
            timed_out = False    # 读超时(挂死): 累计指数退避, 不走普通 60s 熔断
            try:
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    # SSE 注释/心跳行(`: heartbeat` 之类): 部分上游(实测 workbuddy)
                    # 在真正的 data 帧之前先发注释行保活。它既不是错误也不是数据,
                    # 必须在「首个有效行」判定之前跳过, 否则会被误判成非法格式。
                    if line.lstrip().startswith(":"):
                        continue

                    # 预读阶段:第一个有效行用于验证格式
                    if not first_line_yielded:
                        first_line_yielded = True
                        if not line.startswith("data:"):
                            logger.warning("upstream stream invalid format from %s: %s", provider.name, line)
                            circuit_breaker.record_fail(key)
                            quality_tracker.record(key, "fail")
                            break

                    # yield数据
                    if not line.startswith("data:"):
                        stream_ok = True
                        yield (line + "\n").encode()
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        stream_ok = True
                        yield "data: [DONE]\n\n".encode()
                        done_sent = True
                        break
                    try:
                        obj = json.loads(data_str)
                        # 上游在 200 流内回传 error 块(过载/限流的 SSE 形态, 如
                        # {"error":{"message":"Service temporarily overloaded",...}}):
                        # 不透传给客户端(否则表现为"阻断调用"的原文报错), 直接断流,
                        # 让上层 _stream_with_fallback 自动切到下一个健康候选。
                        if isinstance(obj, dict) and "error" in obj and not obj.get("choices"):
                            logger.warning("upstream %s/%s streamed error block: %s",
                                           provider.name, model, data_str[:160])
                            break
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
            except httpx.TimeoutException as e:
                # 挂死型超时(等首字节/相邻块超过读超时): 常见于免费模型
                # 扛不住真实负载(sensenova-6.8-flash-lite 每笔烧满 30s)。
                # 记普通失败只会 60s 短冷却, 下个请求又白烧 30s; 这里累计
                # 指数退避冷却, 连续超时的模型直接冷却掉不再被路由。
                logger.warning("stream read timeout from %s/%s: %s", provider.name, model, e)
                timed_out = True
            except Exception as e:
                logger.warning("stream gen error from %s/%s: %s", provider.name, model, e)
            finally:
                # 空流(上游 200 但没有任何有效行)同样视为失败, 计入熔断/质量统计
                if not stream_ok:
                    if timed_out:
                        circuit_breaker.record_timeout_fail(key)
                    else:
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
                traffic_shaping.release(provider.id)

        gen_handoff["taken"] = True
        return gen(), None

    except httpx.RequestError as e:
        logger.warning("stream connect error to %s: %s", provider.name, e)
        circuit_breaker.record_fail(key)
        quality_tracker.record(key, "error")
        return None, str(e)
    finally:
        # 未交接给流式生成器的返回路径(连接失败/上游错误/异常)都释放并发槽;
        # 交接成功后槽位由 gen() 的 finally 在流结束时释放。
        if not gen_handoff["taken"]:
            traffic_shaping.release(provider.id)


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
        if settings.SPREAD_WEIGHTED_ENABLED:
            # 加权随机分发: 每请求按 质量×今日用量平滑×渠道冷却 抽样, 失败后
            # 按剩余权重继续 —— 流量摊开而非恒定砸向最高分候选, 防单一上游
            # 免费额度/限流被打爆(ROUND_ROBIN 关闭时一并禁用, 保持可回退)。
            healthy, weights, broken = _spread_weights(scored, strategy)
            if len(healthy) > 1:
                order = _sample_weighted(len(healthy), weights, _rng)
                scored = [healthy[i] for i in order] + broken
        else:
            scored = _rotate_candidates(scored, rr_key)

    return [(p, m, k) for p, m, k, _, _, _, _ in scored]


def is_1m_model(model: str) -> bool:
    return get_context_length(model) >= 1048576