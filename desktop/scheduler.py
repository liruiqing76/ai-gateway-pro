#!/usr/bin/env python3
# ============================================================
# 智能网关 — 定时签到调度器
# APScheduler 驱动,每天定点自动执行签到脚本
# ============================================================
import os
import sys
import json
import subprocess
from pathlib import Path
from datetime import datetime, date, timedelta

# ── 签到脚本执行常量 ──
def _resolve_checkin_dir() -> Path:
    """确定签到脚本目录。
    源码模式: desktop/checkin。
    打包后: desktop/ 没有 __init__.py, scheduler 是顶层模块, __file__ 变成
    _MEIPASS/scheduler.py(PyInstaller 虚拟路径), 直接 parent/"checkin" 会解析到
    _MEIPASS/checkin(空); 而 spec 用 ('desktop','desktop') 把脚本拷到了 _MEIPASS/desktop/checkin。
    """
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "desktop" / "checkin"
    return Path(__file__).resolve().parent / "checkin"


CHECKIN_DIR = _resolve_checkin_dir()
SCRIPTS = {
    "workbuddy": "workbuddy_checkin.py",
    "trae": "trae_checkin.py",
}


def parse_time_str(t: str) -> tuple:
    """解析 HH:MM 字符串为 (hour, minute)，非法输入抛 ValueError"""
    parts = t.split(":")
    if len(parts) != 2:
        raise ValueError(f"时间格式应为 HH:MM, 收到: {t!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23):
        raise ValueError(f"小时越界: {h}")
    if not (0 <= m <= 59):
        raise ValueError(f"分钟越界: {m}")
    return h, m


class CheckinScheduler:
    """定时签到调度器：持久化配置 + APScheduler 每日触发"""

    DEFAULT_CONFIG = {
        "enabled": True,          # 总开关
        "workbuddy_time": "09:00",
        "trae_time": "09:05",
        "last_run": None,
    }

    def __init__(self, config_path: Path = None):
        self.config_path = config_path or (Path(__file__).resolve().parent / "schedule_config.json")
        self.config = self.load_config()
        self._jobs = {}  # app_name -> APScheduler job

    # ---- 配置持久化 ----
    def load_config(self) -> dict:
        cfg = dict(self.DEFAULT_CONFIG)
        if self.config_path.exists():
            try:
                cfg.update(json.loads(self.config_path.read_text(encoding="utf-8")))
            except Exception:
                pass
        return cfg

    def save_config(self, cfg: dict = None):
        if cfg is not None:
            self.config = cfg
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(self.config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def set_schedule(self, data: dict) -> dict:
        """应用新的调度配置（enabled / workbuddy_time / trae_time）"""
        for key in ("enabled", "workbuddy_time", "trae_time"):
            if key in data:
                if key.endswith("_time"):
                    parse_time_str(str(data[key]))  # 校验格式
                self.config[key] = data[key]
        self.save_config()
        self.reschedule_jobs()
        return self.config

    def reschedule_jobs(self):
        """配置变更后，更新已注册的 APScheduler job 触发时间"""
        for app, job in self._jobs.items():
            if job is None:
                continue
            time_key = f"{app}_time"
            if self.config.get("enabled"):
                h, m = parse_time_str(str(self.config.get(time_key, "09:00")))
                try:
                    job.reschedule("cron", hour=h, minute=m)
                except Exception:
                    pass
            else:
                try:
                    job.pause()
                except Exception:
                    pass

    # ---- 下次执行时间计算 ----
    def _compute_next_run(self, time_str: str, now: datetime = None) -> datetime:
        h, m = parse_time_str(time_str)
        now = now or datetime.now()
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    def next_run(self) -> dict:
        """返回各应用下次执行时间（ISO 格式）"""
        out = {}
        now = datetime.now()
        for app, time_str in (("workbuddy", self.config["workbuddy_time"]),
                              ("trae", self.config["trae_time"])):
            out[app] = self._compute_next_run(time_str, now).isoformat()
        return out

    # ---- 签到执行 ----
    def run_checkin(self, app: str) -> str:
        """执行单个应用的签到（子进程或内嵌 runpy）"""
        script = CHECKIN_DIR / SCRIPTS[app]
        if not script.exists():
            return "脚本不存在"
        try:
            if getattr(sys, "frozen", False):
                import runpy
                old_cwd = os.getcwd()
                os.chdir(str(script.parent))
                try:
                    runpy.run_path(str(script), run_name="__main__")
                finally:
                    os.chdir(old_cwd)
                return "签到完成"
            r = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True, text=True, timeout=90,
                cwd=str(script.parent),
            )
            out = (r.stdout + r.stderr)[-800:]
            return out if out else "签到执行完成"
        except subprocess.TimeoutExpired:
            return "签到超时(90s)"
        except Exception as e:
            return f"签到异常: {e}"

    # ---- APScheduler 注册 ----
    def setup_jobs(self, scheduler) -> list:
        """在 APScheduler 上注册每日任务，返回 job 列表"""
        jobs = []
        if not self.config.get("enabled"):
            return jobs
        for app, script_name in SCRIPTS.items():
            h, m = parse_time_str(self.config[f"{app}_time"])
            job = scheduler.add_job(
                self.run_checkin,
                "cron",
                hour=h,
                minute=m,
                args=[app],
                id=f"checkin-{app}",
                replace_existing=True,
            )
            jobs.append(job)
            self._jobs[app] = job
        return jobs


if __name__ == "__main__":
    # CLI: python scheduler.py [status|checkin workbuddy|checkin trae]
    sched = CheckinScheduler()
    if len(sys.argv) > 1 and sys.argv[1] == "checkin" and len(sys.argv) > 2:
        print(sched.run_checkin(sys.argv[2]))
    else:
        print(json.dumps({"config": sched.config, "next": sched.next_run()}, ensure_ascii=False, indent=2))