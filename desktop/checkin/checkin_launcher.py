#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy + Trae 自动签到（集成版） + 一键配置导出
- 自动检测目标程序是否运行，未运行则启动
- 使用界面点击 (pyautogui + RapidOCR) 进行签到
- 提供导出 Hermes / Claude / OpenCode 配置的功能
- 不调用任何内部 API，满足“不调用大模型”要求
"""

import os, sys, json, time, ctypes, subprocess, threading, datetime, urllib.request, urllib.error
from pathlib import Path

# 输出编码加固: 中文 Windows 控制台默认 GBK, 无法编码 ✅/⚠️ 等 emoji,
# 直接 print 会抛 UnicodeEncodeError 并污染签到日志。强制 UTF-8 + 容错输出。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

try:
    import pyautogui, pygetwindow as gw
    from PIL import Image
    import numpy as np
except ImportError as e:
    sys.exit(f"缺少依赖，请先运行: pip install -r requirements.txt\n详细错误: {e}")

# RapidOCR（轻量级高精度 OCR，纯 onnxruntime，本地运行，不调大语言模型）
RAPIDOCR_AVAILABLE = False
try:
    from rapidocr_onnxruntime import RapidOCR
    RAPIDOCR_AVAILABLE = True
except ImportError:
    pass

# 日志使用 loguru（结构化、易配置）
from loguru import logger

BASE = Path(__file__).resolve().parent
CONFIG_WB = BASE / "wb_checkin_config.json"
CONFIG_TRAE = BASE / "checkin_config.json"
SHOTS = BASE / "shots"
LAST_WB = BASE / "wb_last_checkin.txt"
LAST_TRAE = BASE / "last_checkin.txt"
LOG = BASE / "checkin.log"

# 确保目录存在
SHOTS.mkdir(exist_ok=True)

# 配置 loguru：输出到文件和控制台，保留最近 7 天，每日一个文件
logger.remove()
logger.add(
    LOG,
    rotation="00:00",           # 每天午夜轮转
    retention="7 days",         # 保留 7 天
    encoding="utf-8",
    enqueue=True,               # 多线程安全
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
    level="INFO",
)
logger.add(
    sys.stdout,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{message}</cyan>",
    level="INFO",
)

def log(msg: str):
    """统一的日志入口，使用 loguru"""
    logger.info(msg)

# ---------- 配置 ----------
def load_config(path: Path):
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(path: Path, cfg):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

# ---------- 今日防重 ----------
def already_today(last_file: Path) -> bool:
    if not last_file.exists():
        return False
    return last_file.read_text(encoding="utf-8").strip() == str(datetime.date.today())

def mark_today(last_file: Path):
    last_file.write_text(str(datetime.date.today()), encoding="utf-8")

# ---------- 窗口前台 ----------
def bring_to_front(hwnd):
    try:
        user32 = ctypes.windll.user32
        fore = user32.GetForegroundWindow()
        if fore == hwnd:
            return True
        fore_tid = user32.GetWindowThreadProcessId(fore, None)
        app_tid = user32.GetWindowThreadProcessId(hwnd, None)
        if fore_tid != app_tid:
            user32.AttachThreadInput(fore_tid, app_tid, True)
        user32.SetForegroundWindow(hwnd)
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        if fore_tid != app_tid:
            user32.AttachThreadInput(fore_tid, app_tid, False)
        return True
    except Exception as e:
        log(f"bring_to_front 异常(忽略): {e}")
        return False

def activate_window(win):
    ok = False
    try:
        ok = bring_to_front(win._hWnd)
    except Exception as e:
        log(f"ctypes 激活失败，回退方案: {e}")
    if not ok:
        try:
            if getattr(win, "isMinimized", False):
                win.restore()
            else:
                win.minimize()
                time.sleep(0.5)
                win.restore()
        except Exception as e:
            log(f"窗口还原异常(忽略): {e}")
    time.sleep(1.0)
    try:
        win.maximize()
    except Exception:
        pass
    time.sleep(0.5)
    return int(win.left), int(win.top)

def find_window(title: str):
    try:
        wins = gw.getWindowsWithTitle(title)
        if not wins:
            return None
        best = None
        best_area = -1
        for w in wins:
            area = w.width * w.height
            if area > best_area:
                best_area = area
                best = w
        return best
    except Exception as e:
        log(f"查找窗口异常: {e}")
        return None

# ---------- RapidOCR ----------
_ocr_engine = None
def get_ocr_engine():
    global _ocr_engine
    if _ocr_engine is None and RAPIDOCR_AVAILABLE:
        _ocr_engine = RapidOCR()
    return _ocr_engine

def ocr_image(img):
    engine = get_ocr_engine()
    if not engine:
        return []
    ox = getattr(img, "box_offset_x", 0)
    oy = getattr(img, "box_offset_y", 0)
    try:
        arr = np.array(img.convert("RGB"))[:, :, ::-1]
        result, _ = engine(arr)
        out = []
        if result:
            for box, text, score in result:
                xs = [p[0] for p in box]
                ys = [p[1] for p in box]
                cx = int(sum(xs) / 4) + ox
                cy = int(sum(ys) / 4) + oy
                out.append((text, cx, cy, box))
        return out
    except Exception as e:
        log(f"OCR 异常: {e}")
        return []

def shot_menu():
    left, top, width, height = (0, 0, 500, 1080)  # MENU_REGION
    img = pyautogui.screenshot(region=(left, top, width, height)).convert("RGB")
    img.box_offset_x = left
    img.box_offset_y = top
    return img

def find_checkin_row(items):
    for text, cx, cy, box in items:
        if "签到" in text and "积分" in text:
            return text, box
    for text, cx, cy, box in items:
        t = text.strip()
        if t in ("签到", "去签到", "立即签到"):
            return text, box
    return None, None

def compute_button_click(box, window_left, window_top):
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    cx = int(sum(xs) / 4)
    cy = int(sum(ys) / 4)
    return cx, cy

def is_signed(items):
    texts = " ".join(t for t, _, _, _ in items)
    if "今日已领" in texts:
        return True, "ocr-today-claimed"
    if "今日已签" in texts or "已签到" in texts:
        return True, "ocr-today-signed"
    return False, "unknown"

def find_sign_button(items, max_x=500):
    for text, cx, cy, box in items:
        if cx > max_x:
            continue
        t = text.strip()
        if t in ("立即领取", "领取", "签到", "立即签到", "去签到", "领", "签"):
            return cx, cy
        if ("领取" in t) and ("每日" not in t) and ("累计" not in t) and len(t) <= 6:
            return cx, cy
    return None

# ---------- 菜单确保打开 ----------
def ensure_menu_open(avatar_x, avatar_y, attempts=4):
    pre = shot_menu()
    if find_checkin_row(ocr_image(pre))[0] is not None:
        pyautogui.click(avatar_x, avatar_y)
        time.sleep(2.0)
    last_img = None
    for i in range(attempts):
        pyautogui.click(avatar_x, avatar_y)
        time.sleep(2.5)
        last_img = shot_menu()
        if find_checkin_row(ocr_image(last_img))[0] is not None:
            return last_img
    return last_img

# ---------- 校准 ----------
def calibrate_app(app_name: str, config_path: Path, is_trae: bool = False):
    print(f"=== {app_name} 自动签到 · 坐标校准 ===")
    print("目标：记录【头像】在屏幕上的位置。OCR 会自动定位签到行与按钮。")
    input("把对应桌面端打开并置于前台，就绪后按 Enter...")
    win = gw.getActiveWindow()
    if not win:
        sys.exit("未能获取前台窗口，请确认应用在前台后再试。")
    title = win.title
    print(f"已记录窗口标题: {repr(title)}")
    print("把鼠标移动到左下角【头像】图标上（不要点击）。")
    input("就位后按 Enter 记录头像位置...")
    p = pyautogui.position()
    avatar = {"x": int(p.x), "y": int(p.y)}
    print(f"头像屏幕坐标: {avatar}")
    cfg = load_config(config_path) or {}
    cfg.update({
        "window_title": title,
        "avatar": avatar,
        "window_left": int(win.left),
        "window_top": int(win.top),
    })
    if is_trae:
        cfg["button_template"] = "trae_signin_btn.png"
        cfg["search_band"] = {"x0": 180, "y0": 130, "x1": 290, "y1": 230}
    save_config(config_path, cfg)
    print(f"校准完成！配置已保存到: {config_path}")
    print("以后直接运行: python checkin_launcher.py")

# ---------- 启动目标程序 ----------
def launch_if_not_running(exe_name: str, exe_path: str | None = None) -> bool:
    """检测进程是否存在，不存在则尝试启动。返回 True 表示当前已在运行。"""
    try:
        output = subprocess.check_output(["tasklist"], text=True, encoding="gbk", errors="replace")
        if exe_name.lower() in output.lower():
            return True
    except Exception:
        pass
    if exe_path and Path(exe_path).exists():
        try:
            subprocess.Popen([exe_path])
            log(f"已启动 {exe_name}: {exe_path}")
            time.sleep(3)  # 给启动一点时间
            return True
        except Exception as e:
            log(f"启动 {exe_name} 失败: {e}")
            return False
    else:
        log(f"未找到 {exe_name} 的可执文件路径，请手动确保程序已运行。")
        return False

# ---------- 获取聚合 key ----------
def get_gateway_key() -> str | None:
    """向本地网关请求当前用户的聚合 API Key。"""
    try:
        # 先登录获取 token（这里简化：直接请求 /api/users/current/key，假设已登录且有 session）
        req = urllib.request.Request("http://127.0.0.1:8765/api/users/current/key")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.load(resp)
            return data.get("api_key")
    except Exception as e:
        log(f"获取聚合 key 失败: {e}")
        return None

# ---------- 导出配置 ----------
def export_hermes_config():
    key = get_gateway_key()
    if not key:
        log("无法获取聚合 key，请先确保网关已登录并在线。")
        return False
    groups = ["high_quality", "vision"]
    lines = ["models:"]
    for g in groups:
        lines.append(f"  - name: {g}")
        lines.append(f"    provider: custom")
        lines.append(f"    base_url: http://127.0.0.1:8765/v1")
        lines.append(f"    api_key: {key}")
    text = "\n".join(lines)
    try:
        import pyperclip
        pyperclip.copy(text)
    except Exception:
        out = BASE / "hermes_config.yaml"
        out.write_text(text, encoding="utf-8")
        log(f"未检测到 pyperclip，已将配置写入 {out}")
    log("Hermes 配置已复制到剪贴板（或写入文件）。")
    return True

def export_claude_config():
    key = get_gateway_key()
    if not key:
        log("无法获取聚合 key，请先确保网关已登录并在线。")
        return False
    env_lines = [
        f"OPENAI_API_KEY={key}",
        "OPENAI_API_BASE=http://127.0.0.1:8765/v1"
    ]
    text = "\n".join(env_lines)
    try:
        import pyperclip
        pyperclip.copy(text)
    except Exception:
        out = BASE / "claude_config.env"
        out.write_text(text, encoding="utf-8")
        log(f"未检测到 pyperclip，已将配置写入 {out}")
    log("Claude 配置已复制到剪贴板（或写入文件）。")
    return True

def export_opencode_config():
    key = get_gateway_key()
    if not key:
        log("无法获取聚合 key，请先确保网关已登录并在线。")
        return False
    cmds = [
        f'opencode config set openai.api_base "http://127.0.0.1:8765/v1"',
        f'opencode config set openai.api_key "{key}"'
    ]
    text = "\n".join(cmds)
    try:
        import pyperclip
        pyperclip.copy(text)
    except Exception:
        out = BASE / "opencode_config.cmd"
        out.write_text(text, encoding="utf-8")
        log(f"未检测到 pyperclip，已将配置写入 {out}")
    log("OpenCode 配置命令已复制到剪贴板（或写入文件）。")
    return True

# ---------- 主签到流程 ----------
def run_one(app_name: str, config_path: Path, last_file: Path, exe_name: str, exe_path: str | None, is_trae: bool = False, dry: bool = False):
    if already_today(last_file):
        log(f"今天 ({datetime.date.today()}) 已经签到过，跳过。")
        return

    # 1. 确保目标程序在运行
    if not launch_if_not_running(exe_name, exe_path):
        log(f"无法确保 {exe_name} 运行，终止签到。")
        return

    # 2. 加载配置
    cfg = load_config(config_path)
    if not cfg:
        sys.exit(f"尚未校准。请先运行: python checkin_launcher.py calibrate {app_name}")
    if not RAPIDOCR_AVAILABLE:
        sys.exit("缺少 RapidOCR。请先运行: pip install rapidocr_onnxruntime")

    win = find_window(cfg["window_title"])
    if not win:
        log(f"找不到标题为 {repr(cfg['window_title'])} 的窗口。请先打开 {app_name} 桌面端并登录。")
        return

    wl, wt = activate_window(win)
    ax, ay = cfg["avatar"]["x"], cfg["avatar"]["y"]
    # 修正坐标（多屏/DPI）
    ax = wl + (ax - cfg.get("window_left", wl))
    ay = wt + (ay - cfg.get("window_top", wt))

    log(f"点击头像 ({ax}, {ay}) 打开菜单")
    img = ensure_menu_open(ax, ay)
    items = ocr_image(img)
    text, box = find_checkin_row(items)
    if text is None:
        img.save(SHOTS / f"{app_name.lower()}_diagnose_{datetime.date.today()}.png")
        log(f"OCR 未定位到【每日签到】行，已保存诊断截图。")
        log("可能原因：今日已签 / 界面改版 / 未开启积分计费模式。若改版请重跑 calibrate。")
        return

    signed, reason = is_signed(items)
    if signed:
        if not dry:
            mark_today(last_file)
        log(f"✅ 检测到已签到（{reason}），无需重复点击。")
        return

    if dry:
        log(f"检测到【每日签到】行: {repr(text)}")
        log("(status 模式不点击，如需真正签到请运行: python checkin_launcher.py run {app_name})")
        return

    # 2) 点击签到行（「签到领积分>」本身是入口，点击进入签到详情页）
    sx, sy = compute_button_click(box, wl, wt)
    log(f"OCR 定位签到行: {repr(text)}，点击进入 ({sx}, {sy})")
    pyautogui.click(sx, sy)
    time.sleep(3.0)

    # 3-4) 等待签到详情页出现（弹出有延迟，循环最多 6 轮）：
    #      详情页出现「立即领取」按钮 → 点击它完成签到；      若已是「今日已领」→ 直接判定已签（已手动签过等场景）。
    bx = by = None
    detail_items = []
    for attempt in range(6):
        time.sleep(2)
        img2 = pyautogui.screenshot().convert("RGB")
        detail_items = ocr_image(img2)
        signed2, reason2 = is_signed(detail_items)
        if signed2:
            mark_today(last_file)
            log(f"✅ 签到成功！（详情页检测到 {reason2}）")
            img2.save(SHOTS / f"{app_name.lower()}_after_checkin_{datetime.date.today()}.png")
            return
        btn = find_sign_button(detail_items, max_x=1920)
        if btn is not None:
            bx, by = btn
            break

    if bx is not None:
        log(f"在签到详情页找到【领取】按钮 ({bx}, {by})，点击")
        pyautogui.click(bx, by)
        time.sleep(3.5)
    else:
        log("⚠️ 未在详情页找到【领取】按钮，可能已直接签到或界面有变。")

    # 5) 验证：重新打开头像菜单，检查「今日已领」/「今日已签」
    img3 = ensure_menu_open(ax, ay)
    img3.save(SHOTS / f"{app_name.lower()}_after_checkin_{datetime.date.today()}.png")
    signed, reason = is_signed(ocr_image(img3))
    if signed:
        mark_today(last_file)
        log(f"✅ 签到成功！（验证: {reason}）")
        log(f"截图: shots/{app_name.lower()}_after_checkin_{datetime.date.today()}.png")
        return

    log("⚠️ 验证未通过。请打开 shots/ 检查；若今日已手动签过属正常。")

# ---------- 入口 ----------
def main():
    import argparse
    parser = argparse.ArgumentParser(description="WorkBuddy + Trae 自动签到（集成版） + 一键配置导出")
    sub = parser.add_subparsers(dest="command", required=True)

    # calibrate
    p_cal = sub.add_parser("calibrate", help="首次坐标校准")
    p_cal.add_argument("app", choices=["workbuddy", "trae"], help="选择应用")

    # run
    p_run = sub.add_parser("run", help="执行签到")
    p_run.add_argument("app", choices=["workbuddy", "trae"], help="选择应用")
    p_run.add_argument("--dry", action="store_true", help="仅检测不点击")

    # status
    p_st = sub.add_parser("status", help="仅检测签到状态")
    p_st.add_argument("app", choices=["workbuddy", "trae"], help="选择应用")

    # export
    p_exp = sub.add_parser("export", help="导出外部工具配置")
    p_exp.add_argument("tool", choices=["hermes", "claude", "opencode"], help="选择要导出的配置")

    args = parser.parse_args()

    if args.command == "calibrate":
        if args.app == "workbuddy":
            calibrate_app("WorkBuddy", CONFIG_WB, is_trae=False)
        else:
            calibrate_app("Trae", CONFIG_TRAE, is_trae=True)
        return

    if args.command == "status":
        if args.app == "workbuddy":
            cfg = load_config(CONFIG_WB)
            if not cfg:
                print("尚未校准 WorkBuddy。请先运行: python checkin_launcher.py calibrate workbuddy")
                return
            win = find_window(cfg["window_title"])
            if not win:
                print("未找到 WorkBuddy 窗口，请先打开并登录。")
                return
            wl, wt = activate_window(win)
            ax, ay = cfg["avatar"]["x"], cfg["avatar"]["y"]
            ax = wl + (ax - cfg.get("window_left", wl))
            ay = wt + (ay - cfg.get("window_top", wt))
            img = ensure_menu_open(ax, ay)
            items = ocr_image(img)
            signed, reason = is_signed(items)
            if signed:
                print(f"WorkBuddy 今日已签到（{reason}）")
            else:
                print("WorkBuddy 今日未签到")
            return
        else:
            cfg = load_config(CONFIG_TRAE)
            if not cfg:
                print("尚未校准 Trae。请先运行: python checkin_launcher.py calibrate trae")
                return
            win = find_window(cfg["window_title"])
            if not win:
                print("未找到 Trae 窗口，请先打开并登录。")
                return
            wl, wt = activate_window(win)
            ax, ay = cfg["avatar"]["x"], cfg["avatar"]["y"]
            ax = wl + (ax - cfg.get("window_left", wl))
            ay = wt + (ay - cfg.get("window_top", wt))
            img = ensure_menu_open(ax, ay)
            items = ocr_image(img)
            signed, reason = is_signed(items)
            if signed:
                print(f"Trae 今日已签到（{reason}）")
            else:
                print("Trae 今日未签到")
            return

    if args.command == "export":
        if args.tool == "hermes":
            export_hermes_config()
        elif args.tool == "claude":
            export_claude_config()
        elif args.tool == "opencode":
            export_opencode_config()
        return

    # run command
    if args.app == "workbuddy":
        run_one(
            app_name="WorkBuddy",
            config_path=CONFIG_WB,
            last_file=LAST_WB,
            exe_name="WorkBuddy",
            exe_path=r"C:\Program Files\WorkBuddy\WorkBuddy.exe",
            is_trae=False,
            dry=getattr(args, "dry", False),
        )
    else:
        run_one(
            app_name="Trae",
            config_path=CONFIG_TRAE,
            last_file=LAST_TRAE,
            exe_name="TraeWork CN",
            exe_path=r"C:\Program Files\Trae Work CN\TraeWorkCN.exe",
            is_trae=True,
            dry=getattr(args, "dry", False),
        )

if __name__ == "__main__":
    main()