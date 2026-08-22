# ============================================================
# 定时签到调度器测试
# ============================================================
import sys, json, time
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))

from scheduler import CheckinScheduler, parse_time_str


def test_parse_time():
    assert parse_time_str("09:05") == (9, 5)
    assert parse_time_str("09:5") == (9, 5)
    assert parse_time_str("23:59") == (23, 59)
    try:
        parse_time_str("25:00")
        assert False, "应该抛异常(小时超界)"
    except ValueError:
        pass
    try:
        parse_time_str("09:60")
        assert False, "应该抛异常(分钟超界)"
    except ValueError:
        pass


def test_default_schedule():
    sched = CheckinScheduler(Path("/nonexistent/config.json"))
    cfg = sched.load_config()
    assert cfg["enabled"] is True
    assert cfg["workbuddy_time"] == "02:00"
    assert cfg["trae_time"] == "02:05"


def test_schedule_roundtrip(tmp_path):
    cfg_path = tmp_path / "schedule.json"
    sched = CheckinScheduler(cfg_path)

    # 保存配置
    sched.set_schedule({
        "enabled": True,
        "workbuddy_time": "08:30",
        "trae_time": "09:45",
    })
    assert cfg_path.exists()

    # 重新加载，验证持久化
    sched2 = CheckinScheduler(cfg_path)
    cfg2 = sched2.load_config()
    assert cfg2["workbuddy_time"] == "08:30"
    assert cfg2["trae_time"] == "09:45"

    # 禁用也能保存
    sched2.set_schedule({"enabled": False})
    assert sched2.load_config()["enabled"] is False


def test_next_run_time():
    """验证下次执行时间计算（同一时间今天已过→明天，未过→今天）"""
    sched = CheckinScheduler(Path("/nonexistent/config.json"))
    # 用一个确定的当前时间点测试
    now = datetime(2026, 8, 18, 10, 0, 0)
    # 09:00 今天已过 → 明天 09:00
    nxt = sched._compute_next_run("09:00", now)
    assert nxt == datetime(2026, 8, 19, 9, 0, 0)
    # 11:00 今天未过 → 今天 11:00
    nxt2 = sched._compute_next_run("11:00", now)
    assert nxt2 == datetime(2026, 8, 18, 11, 0, 0)