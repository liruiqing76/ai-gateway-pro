#!/usr/bin/env python3
# ============================================================
# 智能网关 — 桌面客户端
# pywebview 壳 + 内嵌 FastAPI + 自动签到
# ============================================================
import os
import sys

# 修复 WebView2 白屏: 必须在 import webview/pythonnet 之前, 把主线程 COM 设成 STA。
# 原因: pythonnet 初始化时会 CoInitializeEx 把主线程设成 MTA, 而 WebView2 的
# CoreWebView2Environment.CreateAsync 内部也需设置 COM 模式, 发现已是 MTA 无法改成 STA
# -> 抛 RPC_E_CHANGED_MODE (0x80010106) -> WebView2 初始化失败 -> 窗口白屏。
# 提前设成 STA 后 pythonnet 沿用此模式, WebView2 也能正常初始化。
import pythoncom
pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)  # STA

import json
import signal
import socket
import threading
import subprocess
import time
from pathlib import Path
from datetime import date

import webview
import uvicorn

# 调度器(桌面模式)
from scheduler import CheckinScheduler, CHECKIN_DIR, get_state_dir

if getattr(sys, "frozen", False):
    BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    # 数据目录 = exe 所在目录: 单文件 exe 每次解包到不同临时目录,
    # 必须把 DB/配置放到 exe 旁才能持久化(否则即用即丢)
    DATA_DIR = Path(sys.executable).resolve().parent
else:
    BASE = Path(__file__).resolve().parent.parent
    DATA_DIR = BASE
sys.path.insert(0, str(BASE))

APP_DIR = Path(__file__).resolve().parent.parent

# 调度器(全局单例, 供 API 与 APScheduler 共用)
_scheduler = CheckinScheduler()
_apscheduler = None

# ── 随机端口 ──

API_PORT = 8765
API_URL = f"http://127.0.0.1:{API_PORT}"

# ── 签到 API ──
class CheckinAPI:
    """暴露给前端的签到接口"""

    def __init__(self, scheduler: CheckinScheduler = None):
        self._scheduler = scheduler

    def get_status(self) -> str:
        """返回 WorkBuddy 和 Trae 的签到状态"""
        wb = self._read_last("wb_last_checkin.txt")
        trae = self._read_last("last_checkin.txt")
        today = str(date.today())
        result = {
            "workbuddy": {"signed": wb == today, "date": wb},
            "trae": {"signed": trae == today, "date": trae},
        }
        # 调度状态
        if self._scheduler:
            result["schedule"] = {
                "enabled": self._scheduler.config.get("enabled", True),
                "workbuddy_time": self._scheduler.config.get("workbuddy_time", "02:00"),
                "trae_time": self._scheduler.config.get("trae_time", "02:05"),
                "next_run": self._scheduler.next_run(),
            }
        return json.dumps(result, ensure_ascii=False)

    def save_schedule(self, data: str) -> str:
        """保存定时配置: {"enabled": true, "workbuddy_time": "02:00", ...}"""
        if not self._scheduler:
            return json.dumps({"ok": False, "error": "调度器未初始化"})
        try:
            payload = json.loads(data) if isinstance(data, str) else data
            cfg = self._scheduler.set_schedule(payload)
            return json.dumps({"ok": True, "config": cfg}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    def run_checkin(self, app: str = "all") -> str:
        """执行签到: app = workbuddy | trae | all, 返回各应用执行结果"""
        results = {}
        checkin_dir = CHECKIN_DIR  # 打包后为 _MEIPASS/desktop/checkin, 源码为 desktop/checkin
        if app in ("all", "workbuddy"):
            results["workbuddy"] = self._run(checkin_dir / "workbuddy_checkin.py")
        if app in ("all", "trae"):
            results["trae"] = self._run(checkin_dir / "trae_checkin.py")
        return json.dumps(results)

    def _read_last(self, filename: str) -> str:
        # 标记文件读持久目录(打包后= exe旁 data/checkin_state), 与脚本写入位置一致
        path = get_state_dir() / filename
        if path.exists():
            return path.read_text().strip()
        return ""

    def _run(self, script: Path) -> str:
        if not script.exists():
            return "脚本不存在"
        try:
            # 标记/日志写持久目录, 脚本通过环境变量感知
            os.environ["CHECKIN_STATE_DIR"] = str(get_state_dir())
            # 打包后 python 解释器不可用，签到脚本直接内嵌运行
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
                capture_output=True, text=True, timeout=60,
                cwd=str(script.parent),
                encoding="utf-8", errors="replace",  # 子进程输出可能含✅等emoji, GBK会炸
            )
            return (r.stdout + r.stderr)[-500:]
        except subprocess.TimeoutExpired:
            return "签到超时(60s)"
        except Exception as e:
            return str(e)


# ── 设置 API (开机自启等) ──
class SettingsAPI:
    """暴露给前端的应用设置接口（开机自启、服务信息）"""

    def __init__(self):
        self._startup_dir = self._resolve_startup_dir()

    def _resolve_startup_dir(self) -> Path:
        """获取当前 exe 所在目录（源码运行取项目根）"""
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parent.parent

    def _startup_exe(self) -> Path:
        """开机自启项指向的 exe（打包后为当前 exe，否则为 python desktop.py）"""
        if getattr(sys, "frozen", False):
            return Path(sys.executable)
        return self._startup_dir / "desktop" / "desktop.py"

    def get_settings(self) -> str:
        """返回当前设置与应用信息"""
        exe = self._startup_exe()
        info = {
            "version": "0.1.0",
            "executable": str(exe),
            "auto_start_supported": getattr(sys, "frozen", False),
            "auto_start_enabled": False,
        }
        try:
            info["auto_start_enabled"] = self._is_autostart()
        except Exception:
            pass  # winshell 缺失时仅报告未启用
        if exe.exists():
            try:
                info["exe_size_mb"] = round(exe.stat().st_size / 1024 / 1024, 1)
            except Exception:
                pass
        return json.dumps(info, ensure_ascii=False)

    def _autostart_lnk(self) -> Path:
        """Windows 开机自启快捷方式路径"""
        import winshell
        folder = winshell.startup()
        name = "智能网关" + (".lnk" if getattr(sys, "frozen", False) else "")
        return Path(folder) / name

    def set_auto_start(self, enabled: str) -> str:
        """开启/关闭开机自启: {"enabled": true|false}

        失败时返回 {"ok": False, "error_code": ..., "error": ...},
        前端按 error_code 本地化(error 仅作兜底展示)。
        """
        try:
            enabled = json.loads(enabled) if isinstance(enabled, str) else enabled
            enabled = bool(enabled.get("enabled", False))
        except Exception:
            enabled = bool(enabled)

        if not getattr(sys, "frozen", False):
            return json.dumps(
                {"ok": False, "error_code": "not_packaged",
                 "error": "仅打包后的桌面应用支持开机自启"},
                ensure_ascii=False,
            )

        try:
            import winshell
            from win32com.client import Dispatch

            lnk = self._autostart_lnk()
            if enabled:
                lnk.parent.mkdir(parents=True, exist_ok=True)
                shell = Dispatch("WScript.Shell")
                shortcut = shell.CreateShortCut(str(lnk))
                shortcut.Targetpath = str(self._startup_exe())
                shortcut.WorkingDirectory = str(self._startup_dir)
                shortcut.save()
            else:
                if lnk.exists():
                    lnk.unlink()
            return json.dumps({"ok": True, "enabled": self._is_autostart()}, ensure_ascii=False)
        except Exception as e:
            return json.dumps(
                {"ok": False, "error_code": "exception", "error": str(e)},
                ensure_ascii=False,
            )

    def _is_autostart(self) -> bool:
        return self._autostart_lnk().exists()


# ── 启动 FastAPI ──
def start_backend():
    """后台线程启动 FastAPI"""
    os.environ["GW_DESKTOP_MODE"] = "1"
    if getattr(sys, "frozen", False):
        # 打包后: DB 放在 exe 旁的 data/ 目录, 持久化
        os.environ["GW_DB_PATH"] = str(DATA_DIR / "data" / "gateway.db")
    # 源码模式不设置 GW_DB_PATH → 走默认 backend/data/gateway.db, 与 uvicorn 模式一致
    from backend.main import app
    uvicorn.run(app, host="127.0.0.1", port=API_PORT, log_level="warning")


def _wait_for_backend(timeout: int = 40) -> None:
    """轮询 /api/health 直到后端就绪, 最多等 timeout 秒"""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{API_URL}/api/health", timeout=2) as r:
                if r.status == 200:
                    print("[desktop] 后端已就绪")
                    return
        except Exception:
            pass
        time.sleep(0.5)
    print("[desktop] 警告: 等待后端超时, 仍将继续打开窗口")


# ── 桌面 JS 桥容器 ──
class DesktopBridge:
    """pywebview js_api 容器。

    必须用普通对象而不是 dict: pywebview 用 dir() 枚举 js_api 的属性名,
    字典会被枚举出 dict 内置方法(clear/get/keys...) 而丢失 checkin/settings 两个 key,
    导致 JS 端 window.pywebview.api.checkin 为 undefined, 调用即报 TypeError。
    对象属性会被递归展开 → JS 端 window.pywebview.api.checkin.get_status() 可用。
    """

    def __init__(self, checkin: "CheckinAPI", settings: "SettingsAPI"):
        self.checkin = checkin
        self.settings = settings


# ── 系统托盘(右下角) ──
# 行为: 点窗口 X / 任务栏关闭 → 隐藏到托盘(不退出); 托盘左键单击或菜单"打开"恢复;
#       菜单"退出"才真正结束进程。
# 图标: 优先使用用户自备的 desktop/assets/app.ico(或 app.png), 没有则运行时画一个
#       品牌蓝圆角+闪电占位图。想换图标直接覆盖 app.ico 即可, 无需改代码。
ASSETS_DIR = BASE / "desktop" / "assets"
APP_TITLE = "智能网关 v0.1.0"
_tray_icon = None          # pystray.Icon 单例
_really_exit = False       # 托盘"退出"置位后, closing 事件放行真正关闭


def _load_tray_image():
    """托盘图标: 依次尝试 desktop/assets/app.ico、app.png; 都没有则画占位图"""
    from PIL import Image, ImageDraw
    for name in ("app.ico", "app.png"):
        p = ASSETS_DIR / name
        if p.exists():
            try:
                return Image.open(p)
            except Exception:
                pass
    # 占位: 品牌蓝圆角方块 + 白色闪电
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, 61, 61], radius=14, fill=(23, 119, 255, 255))
    d.polygon([(36, 8), (20, 34), (30, 34), (26, 56), (44, 28), (33, 28)],
              fill=(255, 255, 255, 255))
    return img


def _apply_native_window_icon(window):
    """源码模式下给 WinForms 窗体设置原生图标(标题栏/任务栏/Alt+Tab)。
    打包模式下 exe 已内嵌图标, 此函数同样无害。

    注意: 本函数由 shown 事件派生到后台线程执行。绝不能从后台线程直接改
    WinForms 属性 —— 会与 UI 线程的 WebView2 初始化竞态死锁(表现为红屏+无响应)。
    必须用 BeginInvoke 把实际赋值排队回 UI 线程, 调用本身立即返回。
    """
    ico_path = ASSETS_DIR / "app.ico"
    if not ico_path.exists():
        return
    try:
        form = getattr(window, "native", None)
        if form is None:
            return
        import clr
        clr.AddReference("System.Drawing")
        from System import Action
        from System.Drawing import Icon

        def _set_icon():
            try:
                form.Icon = Icon(str(ico_path))
                print("[desktop] 已设置原生窗口图标(app.ico)")
            except Exception as e:
                print(f"[desktop] 原生窗口图标设置失败(不影响使用): {e}")

        form.BeginInvoke(Action(_set_icon))
    except Exception as e:
        print(f"[desktop] 原生窗口图标设置失败(不影响使用): {e}")


def _setup_tray(window):
    """窗口显示后启动托盘图标(独立线程 + 自带消息循环, 不依赖 UI 线程)"""
    global _tray_icon
    try:
        import pystray
    except Exception as e:
        print(f"[tray] pystray 不可用, 跳过托盘功能: {e}")
        return

    def on_open(icon, item=None):
        try:
            window.show()
        except Exception:
            pass

    def on_quit(icon, item=None):
        global _really_exit
        _really_exit = True
        try:
            icon.stop()
        except Exception:
            pass
        try:
            window.destroy()
        except Exception:
            os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("打开 智能网关", on_open, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", on_quit),
    )
    icon = pystray.Icon("ai-gateway-pro", _load_tray_image(), APP_TITLE, menu)
    threading.Thread(target=icon.run, daemon=True, name="gw-tray").start()
    _tray_icon = icon
    print("[tray] 托盘已启动")


def _make_closing_handler(window):
    """点关闭 → 隐藏到托盘并取消真实关闭; 仅托盘菜单'退出'时放行"""
    def on_closing():
        if _really_exit:
            return None  # 允许关闭
        try:
            window.hide()
        except Exception:
            pass
        return False     # 取消本次 close
    return on_closing


def main():
    # 启动后端
    t = threading.Thread(target=start_backend, daemon=True)
    t.start()
    # 等后端真正就绪再开窗口(轮询 health, 避免 WebView2 在后端未起好时导航失败->空白不重试)
    _wait_for_backend()

    # 启动 APScheduler 定时签到
    global _apscheduler
    if _scheduler.config.get("enabled"):
        from apscheduler.schedulers.background import BackgroundScheduler
        _apscheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        jobs = _scheduler.setup_jobs(_apscheduler)
        if jobs:
            _apscheduler.start()
            print(f"[scheduler] 已启动 {len(jobs)} 个定时签到任务")

    # 前端路径: 同源加载本地 FastAPI(http://127.0.0.1:8765/)
    # 注意: 必须用 http 同源加载, 不能用 file://
    #  - file:// 下 fetch 相对路径会解析到 file:// 而非后端
    #  - CORS 不允许 file:// 的 null 源, 接口会全部失败(登录都登不上)
    # 本地 uvicorn 根路径 "/" 会返回 web/index.html(纯 JS 管理界面)
    url = f"http://127.0.0.1:{API_PORT}/"

    # 创建窗口
    checkin_api = CheckinAPI(_scheduler)
    settings_api = SettingsAPI()
    window = webview.create_window(
        "智能网关 v0.1.0",   # 加版本号便于任务栏/Alt+Tab 识别; 不带emoji(标题栏左侧已是法斗图标)
        url,
        width=1200,
        height=800,
        min_size=(900, 600),
        x=100, y=80,  # 显式位置, 避开屏幕左上角(0,0), 避免被 WorkBuddy 窗口遮盖
        # 显眼红色背景, 即便内容没渲染, 也能立刻分辨"这是我们的窗口 vs WorkBuddy IDE 界面"
        background_color="#FF3B3B",
        text_select=True,
        confirm_close=False,
        js_api=DesktopBridge(checkin_api, settings_api),
    )
    # 托盘 + 关闭最小化到托盘; shown 事件触发一次, 在窗口就绪后再装图标/托盘
    window.events.shown += lambda: (_apply_native_window_icon(window), _setup_tray(window))
    window.events.closing += _make_closing_handler(window)
    # 显式使用 Edge WebView2 引擎(已确认运行时已安装, GUIType 值为 'edgechromium')。
    # 不显式指定时 pywebview 在某些环境会自动选成 mshtml(IE)导致 JS 失效/窗口空白。
    webview.start(gui='edgechromium', debug=False)


if __name__ == "__main__":
    main()
