"""「无可用的模型」的解释器。

候选为空时用户只看到 `无可用的模型: opencode-xxx`, 分不清到底是
① 渠道在上游限流冷却(等一会儿就好)、② 模型名写错、③ 渠道没启用。
这里只做**解释**, 不参与选路: 拿到的是同一份 providers 快照, 返回一句话后缀。
"""
from __future__ import annotations

from backend.core.circuit import circuit_breaker


def _pid(provider) -> int:
    try:
        return int(getattr(provider, "id", -1) or -1)
    except Exception:
        return -1


def unavailable_reason(providers, requested_model: str = "") -> str:
    """返回可读原因(空串 = 没有可解释的信息)。"""
    if not providers:
        return ""
    wanted = (requested_model or "").strip()
    if not wanted:
        return ""

    # 1) 命中渠道前缀: 优先解释这个渠道自己的状态
    hit = None
    for p in providers:
        name = getattr(p, "name", "") or ""
        if name and (wanted == name or wanted.startswith(f"{name}-")):
            # 取最长前缀, 避免 "qoder-" 抢占 "qodercn-"
            if hit is None or len(name) > len(getattr(hit, "name", "")):
                hit = p

    if hit is not None:
        name = getattr(hit, "name", "")
        if not getattr(hit, "enabled", True):
            return f"（渠道 {name} 已停用, 请在渠道页启用）"
        if circuit_breaker.provider_in_cooldown(_pid(hit)):
            secs = circuit_breaker.provider_cooldown_seconds(_pid(hit))
            return f"（{name} 上游限流/额度冷却中, 约 {secs} 秒后自动重试）"
        models = list(getattr(hit, "models", None) or [])
        bare = wanted[len(name) + 1:] if wanted.startswith(f"{name}-") else ""
        if models and bare and bare not in models:
            sample = ", ".join(models[:3])
            return f"（{name} 里没有模型 {bare}, 例如: {sample}）"

    # 2) 不是渠道前缀: 看是否有渠道整体在冷却
    cooling = [getattr(p, "name", "") for p in providers
               if getattr(p, "enabled", True) and circuit_breaker.provider_in_cooldown(_pid(p))]
    if cooling:
        return f"（{'/'.join(cooling)} 正在限流冷却, 稍后自动恢复）"
    return ""
