"""免密车道状态(opencode / kilo)的落盘记忆。

`lane_state` 原本只活在进程内存里(`_runtime["last_result"]`), 重启后到同步循环
跑完前是 None —— 界面上「车道畅通 / 当前出口被上游关闭」徽标会凭空消失几分钟,
用户会以为渠道坏了。这里把它写进 data 目录的小 json, 启动即恢复上次结论。

状态语义(两个车道一致):
  open    = 有模型 200, 或只是"额度用尽(会重置)"
  closed  = 全被上游明确拒绝(如 403 FreeTier/Region)
  unknown = 探测失败但看不出原因
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from backend.core.state_paths import state_dir

logger = logging.getLogger("gateway.lane_state")

UTC8 = timezone(timedelta(hours=8))

FILENAME = "lane_state.json"


def _state_dir():
    return state_dir()


def _path():
    return _state_dir() / FILENAME


def load() -> dict:
    """读全部车道状态; 文件缺失/损坏一律返回空 dict(不抛)。"""
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def save(channel: str, state: str, **extra) -> None:
    """写单个车道的状态(与其他车道合并保存)。

    同一车道内的旧字段会保留(merge) —— 这样同步循环只报 `state` 时,
    不会把"额度将于 HH:MM 恢复"(resume_ts/resume_at)冲掉;
    resume_ts 一旦过期就自动清掉, 界面不会挂着过期的倒计时。
    """
    if not channel or not state:
        return
    data = load()
    prev = data.get(channel)
    entry = dict(prev) if isinstance(prev, dict) else {}
    entry["state"] = state
    entry.update({k: v for k, v in extra.items() if v is not None})
    _drop_expired_resume(entry)
    data[channel] = entry
    try:
        _path().write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # 落盘失败不影响主流程
        logger.debug("lane_state 写入失败: %s", e)


def _drop_expired_resume(entry: dict) -> None:
    """清掉过期的"额度恢复"提示(预留 60s 余量, 避免边界上闪一下又消失)。"""
    ts = entry.get("resume_ts")
    if ts is None:
        return
    try:
        if float(ts) + 60 > time.time():
            return
    except (TypeError, ValueError):
        pass
    for k in ("resume_ts", "retry_after", "resume_at", "reason"):
        entry.pop(k, None)


def note_quota_resume(channel: str, retry_after: int | None,
                      reason: str = "免费额度已用尽(上游 429, 会重置)") -> None:
    """上游 429 带 retry-after 时, 记住恢复时刻(界面显示倒计时)。"""
    if not channel or not retry_after or retry_after <= 0:
        return
    resume_ts = time.time() + retry_after
    resume_at = datetime.fromtimestamp(resume_ts, UTC8).strftime("%m-%d %H:%M")
    prev = get(channel)
    save(channel, prev.get("state") or "open",
         retry_after=int(retry_after), resume_ts=resume_ts,
         resume_at=resume_at, reason=reason)


def get(channel: str) -> dict:
    """取单个车道状态(空 dict 表示未知)。

    读取时也做一次过期清理: 同步循环几小时才跑一次, 若只靠 `save()` 清理,
    "额度 10-25 08:00 恢复"这种徽标在真正恢复后还会挂很久 —— 用户看不出
    现在到底该不该重试。过期即在返回值里剔除(文件本身下次 save 时清).
    """
    entry = load().get(channel)
    if not isinstance(entry, dict):
        return {}
    entry = dict(entry)
    _drop_expired_resume(entry)
    return entry
