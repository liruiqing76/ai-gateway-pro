"""额度恢复时刻透传: retry-after → 错误对象 → 文案 → lane_state → /api/channels。

背景(2026-10-24 实测): opencode 免费车道 429 响应头带 `retry-after: 26715`,
正文是 FreeUsageLimitError。用户看到"明天再试"会以为是日历日重置, 实际
是滚动窗口(约 7h25m 后, 08:00 UTC+8)。本文件锁住这条链路。
"""
from __future__ import annotations

import json
import time

import httpx
import pytest

from backend.core import lane_state
from backend.core.channel_auth import channel_transform
from backend.core.proxy import _quota_reason, _retry_after_phrase


class TestRetryAfterParsing:
    def test_plain_seconds(self):
        assert channel_transform._retry_after_seconds(
            {"retry-after": "26715"}) == 26715

    def test_header_case_insensitive(self):
        # 真实场景走 httpx.Headers(大小写不敏感), 用真类型而不是自造 dict
        assert channel_transform._retry_after_seconds(
            httpx.Headers({"Retry-After": "60"})) == 60
        assert channel_transform._retry_after_seconds(
            httpx.Headers({"retry-after": "60"})) == 60

    def test_rejects_http_date(self):
        # 只认纯数字; HTTP 日期不猜
        assert channel_transform._retry_after_seconds(
            {"retry-after": "Wed, 25 Oct 2026 00:00:00 GMT"}) is None

    def test_rejects_zero_negative_and_absurd(self):
        assert channel_transform._retry_after_seconds({"retry-after": "0"}) is None
        assert channel_transform._retry_after_seconds({"retry-after": "-5"}) is None
        assert channel_transform._retry_after_seconds(
            {"retry-after": str(30 * 24 * 3600)}) is None

    def test_missing_header(self):
        assert channel_transform._retry_after_seconds({}) is None


class TestErrorObject:
    def test_carries_retry_after(self):
        e = channel_transform.TransformUpstreamError(
            429, '{"type":"error","error":{"type":"FreeUsageLimitError"}}', 26715)
        assert e.retry_after == 26715
        assert e.resume_at_utc8(now=1790000000).count(":") == 1

    def test_default_none(self):
        e = channel_transform.TransformUpstreamError(500, "boom")
        assert e.retry_after is None
        assert e.resume_at_utc8() is None

    def test_records_lane_resume_at_construction(self, monkeypatch, tmp_path):
        """9 个候选模型里最后一条常是 403 地区封锁 —— 恢复时刻必须在
        抛出错误的那一刻就落盘, 不能依赖候选循环最后落在哪条错误上。"""
        monkeypatch.setattr(lane_state, "state_dir", lambda: tmp_path)
        channel_transform.TransformUpstreamError(
            429, "FreeUsageLimitError", 26715, channel="opencode")
        entry = lane_state.get("opencode")
        assert entry.get("resume_at"), "构造错误时就该写进 lane_state"
        assert entry.get("retry_after") == 26715

    def test_no_channel_no_write(self, monkeypatch, tmp_path):
        monkeypatch.setattr(lane_state, "state_dir", lambda: tmp_path)
        channel_transform.TransformUpstreamError(429, "FreeUsageLimitError", 60)
        assert lane_state.load() == {}


class TestQuotaReasonText:
    def test_includes_resume_time_and_duration(self):
        text = _quota_reason(
            '{"error":{"type":"FreeUsageLimitError","message":"Free usage limit reached"}}',
            26715)
        assert "免费额度已用尽" in text
        assert "恢复" in text
        assert "小时" in text

    def test_without_retry_after_keeps_old_wording(self):
        text = _quota_reason("Free usage limit reached")
        assert "明天再试" in text

    def test_short_window_uses_minutes(self):
        text = _quota_reason("free usage limit", 90)
        assert "分钟后" in text

    def test_other_quota_kinds_also_get_resume(self):
        text = _quota_reason("insufficient_quota for this key", 90)
        assert "配额已用尽" in text and "分钟后" in text

    def test_phrase_helper(self):
        when, dur = _retry_after_phrase(3600)
        assert len(when) == 5 and "1 小时" in dur
        assert _retry_after_phrase(None) == ("", "")


class TestLaneStateResume:
    @pytest.fixture(autouse=True)
    def _isolated(self, monkeypatch, tmp_path):
        monkeypatch.setattr(lane_state, "state_dir", lambda: tmp_path)

    def test_note_quota_resume_saves_display_fields(self):
        lane_state.note_quota_resume("opencode", 26715)
        entry = lane_state.get("opencode")
        assert entry["state"] == "open"
        assert entry["retry_after"] == 26715
        assert entry["resume_at"].count(":") == 1
        assert entry["resume_ts"] > time.time()

    def test_later_save_does_not_wipe_resume(self):
        lane_state.note_quota_resume("opencode", 600)
        lane_state.save("opencode", "open", usable=9)
        entry = lane_state.get("opencode")
        assert entry.get("resume_ts"), "同步循环只报 state 时不能冲掉恢复时刻"
        assert entry["usable"] == 9

    def test_expired_resume_is_dropped_on_next_save(self):
        lane_state.save("opencode", "open", retry_after=60,
                        resume_ts=time.time() - 3600, resume_at="10-24 08:00",
                        reason="额度用尽")
        lane_state.save("opencode", "open")
        entry = lane_state.get("opencode")
        assert "resume_at" not in entry and "resume_ts" not in entry
        assert "reason" not in entry

    def test_expired_resume_is_dropped_on_read(self, tmp_path):
        """只读也要过期: 同步循环几小时才跑一次, 否则到点后徽标还挂着。

        模拟"落盘时还没过期, 之后时间走过了恢复时刻"的真实盘上状态。
        """
        stale = {"opencode": {
            "state": "open", "retry_after": 60,
            "resume_ts": time.time() - 3600, "resume_at": "10-25 08:00",
            "reason": "免费额度已用尽",
        }}
        (tmp_path / "lane_state.json").write_text(
            json.dumps(stale, ensure_ascii=False), encoding="utf-8")
        entry = lane_state.get("opencode")
        assert "resume_at" not in entry and "resume_ts" not in entry
        assert "reason" not in entry
        assert entry["state"] == "open"     # 状态本身保留

    def test_future_resume_survives_read(self):
        lane_state.note_quota_resume("opencode", 600)
        assert lane_state.get("opencode").get("resume_at")

    def test_zero_retry_after_is_noop(self):
        lane_state.note_quota_resume("opencode", 0)
        assert lane_state.get("opencode") == {}
        lane_state.note_quota_resume("opencode", None)
        assert lane_state.get("opencode") == {}

    def test_empty_channel_is_noop(self):
        lane_state.note_quota_resume("", 60)
        assert lane_state.load() == {}


class TestChannelsApiPayload:
    def test_lane_resume_shape(self, monkeypatch, tmp_path):
        monkeypatch.setattr(lane_state, "state_dir", lambda: tmp_path)
        lane_state.note_quota_resume("opencode", 26715)

        # 验证 API 分支取的两个字段(避免起整个 app + 用户鉴权)
        entry = lane_state.get("opencode")
        payload = {"at": entry.get("resume_at"), "reason": entry.get("reason")}
        assert payload["at"] and payload["reason"]
        dumped = json.loads(json.dumps(payload, ensure_ascii=False))
        assert dumped["at"].count(":") == 1
