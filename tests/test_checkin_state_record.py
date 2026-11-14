"""签到状态落盘与展示契约。

背景: 前端"自动签到"页原来只有一个桌面端开关, 点完签到看不到结果。现在页面
改由后端真实数据驱动, 所以状态文件必须能回答四个问题:

1. 今天这个渠道领到了吗(claimed / already-claimed)
2. 领了多少积分
3. 没领到是为什么(失败原因要落盘, 不能只写"失败")
4. 手动单渠道签到也要更新状态(旧实现只有全量调度写)
"""
from __future__ import annotations

import pytest

from backend.core import checkin as ck


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """把状态文件指到临时目录, 避免污染 data/channel_checkin_state.json。"""
    monkeypatch.setattr(ck, "_state_dir", lambda: tmp_path)
    return tmp_path


def test_record_state_writes_entry(state_dir):
    ck.record_state(4, "trae", {"kind": "claimed", "credit": 100,
                                "message": "签到成功", "streak_days": 3})
    raw = ck.load_state()
    entry = raw[f"4:trae"]
    assert entry["kind"] == "claimed"
    assert entry["credit"] == 100
    assert entry["reason"] == "签到成功"
    assert entry["streak_days"] == 3
    assert entry["date"] == ck.utc8_date_str()
    assert entry["at"] > 0


def test_record_state_truncates_long_reason(state_dir):
    ck.record_state(1, "codearts", {"kind": "failed", "message": "x" * 500})
    assert len(ck.load_state()["1:codearts"]["reason"]) == 200


def test_state_entry_done_true_for_claimed_and_already(state_dir):
    ck.record_state(4, "trae", {"kind": "claimed", "credit": 100})
    ck.record_state(4, "qodercn", {"kind": "already-claimed"})
    assert ck.state_entry_done(4, "trae") is True
    assert ck.state_entry_done(4, "qodercn") is True


def test_state_entry_done_false_for_failed_and_other_day(state_dir):
    ck.record_state(4, "codearts", {"kind": "failed", "message": "AK 无效"})
    assert ck.state_entry_done(4, "codearts") is False
    # 别人的记录不算
    ck.record_state(9, "trae", {"kind": "claimed"})
    assert ck.state_entry_done(4, "trae") is False


def test_state_entry_done_false_when_date_is_stale(state_dir, monkeypatch):
    ck.record_state(4, "trae", {"kind": "claimed"})
    state = ck.load_state()
    state["4:trae"]["date"] = "2000-01-01"
    ck.save_state(state)
    assert ck.state_entry_done(4, "trae") is False


def test_summary_exposes_kind_reason_and_credit(state_dir):
    ck.record_state(4, "trae", {"kind": "claimed", "credit": 100})
    ck.record_state(4, "qodercn", {"kind": "already-claimed", "credit": 0})
    ck.record_state(4, "codearts", {"kind": "failed",
                                    "message": "refresh_token 已失效"})
    today = ck.checkin_state_summary()
    assert today["today"] == ck.utc8_date_str()
    ch = today["channels"]
    assert ch["trae"]["kind"] == "claimed"
    assert ch["trae"]["credit"] == 100
    assert ch["trae"]["reason"] == ""
    assert ch["qodercn"]["kind"] == "already-claimed"
    assert ch["codearts"]["kind"] == "failed"
    assert "失效" in ch["codearts"]["reason"]
    assert ch["codearts"]["at"] > 0


def test_summary_prefers_success_over_failure_same_channel(state_dir):
    """同一渠道先失败后重试成功: 展示要以成功为准, 且清掉失败原因。"""
    ck.record_state(4, "codearts", {"kind": "failed", "message": "AK access limit"})
    ck.record_state(4, "codearts", {"kind": "claimed", "credit": 8})
    ch = ck.checkin_state_summary()["channels"]["codearts"]
    assert ch["kind"] == "claimed"
    assert ch["credit"] == 8
    assert ch["reason"] == ""


def test_summary_keeps_inactive_and_unsupported_kinds(state_dir):
    ck.record_state(4, "workbuddy", {"kind": "inactive", "message": "活动已结束"})
    ck.record_state(4, "qoder", {"kind": "unsupported"})
    ch = ck.checkin_state_summary()["channels"]
    assert ch["workbuddy"]["kind"] == "inactive"
    assert ch["qoder"]["kind"] == "unsupported"


def test_summary_ignores_other_days(state_dir):
    ck.record_state(4, "trae", {"kind": "claimed", "credit": 100})
    state = ck.load_state()
    state["4:trae"]["date"] = "2000-01-01"
    ck.save_state(state)
    assert ck.checkin_state_summary()["channels"] == {}


@pytest.mark.asyncio
async def test_single_channel_checkin_records_state(state_dir, monkeypatch):
    """POST /api/channels/checkin/{channel} 走 _do_channel_checkin 时必须落盘。"""
    from backend.api import channels as ch_api

    class _FakeProvider:
        id = 1
        user_id = 4
        name = "trae"
        api_key_encrypted = "enc"

    class _FakeSession:
        def query(self, *a, **kw):
            return self

        def filter_by(self, **kw):
            return self

        def first(self):
            return _FakeProvider()

        def close(self):
            pass

    monkeypatch.setattr(ch_api, "get_session", lambda: _FakeSession())

    async def _fake_checkin(client, user_id, provider):
        return {"channel": provider.name, "kind": "claimed", "credit": 300,
                "message": "签到成功"}

    monkeypatch.setattr(ck, "checkin_provider", _fake_checkin)

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(ch_api.upstream_http, "make_async",
                        lambda **kw: _FakeClient())

    result = await ch_api._do_channel_checkin(4, "trae")
    assert result["kind"] == "claimed"
    entry = ck.load_state()["4:trae"]
    assert entry["kind"] == "claimed"
    assert entry["credit"] == 300
    assert entry["reason"] == "签到成功"


@pytest.mark.asyncio
async def test_single_channel_checkin_records_failure_reason(state_dir, monkeypatch):
    from backend.api import channels as ch_api

    class _FakeProvider:
        id = 1
        user_id = 4
        name = "codearts"
        api_key_encrypted = "enc"

    class _FakeSession:
        def query(self, *a, **kw):
            return self

        def filter_by(self, **kw):
            return self

        def first(self):
            return _FakeProvider()

        def close(self):
            pass

    monkeypatch.setattr(ch_api, "get_session", lambda: _FakeSession())

    async def _fake_checkin(client, user_id, provider):
        return {"channel": provider.name, "kind": "failed",
                "message": "APIG.0301 AK access failed to reach the limit"}

    monkeypatch.setattr(ck, "checkin_provider", _fake_checkin)

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(ch_api.upstream_http, "make_async",
                        lambda **kw: _FakeClient())

    await ch_api._do_channel_checkin(4, "codearts")
    entry = ck.load_state()["4:codearts"]
    assert entry["kind"] == "failed"
    assert "APIG.0301" in entry["reason"]


def test_next_run_at_is_today_or_tomorrow_utc8(monkeypatch):
    from backend.api import channels as ch_api
    from datetime import datetime as _dt

    monkeypatch.setattr(ch_api.settings, "CHANNEL_CHECKIN_ENABLED", True,
                        raising=False)
    monkeypatch.setattr(ch_api.settings, "CHANNEL_CHECKIN_TIME", "09:30",
                        raising=False)
    nxt = ch_api._checkin_next_run_at()
    assert nxt is not None
    parsed = _dt.strptime(nxt, "%Y-%m-%dT%H:%M:%S")
    assert parsed.hour == 9 and parsed.minute == 30
    # 一定在未来(今天 09:30 未到就是今天, 否则明天), 且不超过 24h
    delta = parsed - _dt.now(ch_api.UTC8).replace(tzinfo=None)
    assert 0 < delta.total_seconds() <= 86400


def test_next_run_at_none_when_disabled(monkeypatch):
    from backend.api import channels as ch_api
    monkeypatch.setattr(ch_api.settings, "CHANNEL_CHECKIN_ENABLED", False,
                        raising=False)
    assert ch_api._checkin_next_run_at() is None


def test_next_run_at_survives_bad_time_value(monkeypatch):
    from backend.api import channels as ch_api
    monkeypatch.setattr(ch_api.settings, "CHANNEL_CHECKIN_ENABLED", True,
                        raising=False)
    monkeypatch.setattr(ch_api.settings, "CHANNEL_CHECKIN_TIME", "not-a-time",
                        raising=False)
    assert ch_api._checkin_next_run_at() is not None
