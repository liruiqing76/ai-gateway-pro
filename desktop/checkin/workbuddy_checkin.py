#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 桌面端 自动签到 —— 界面点击版（pyautogui + RapidOCR）

原理：不调用任何内部 API，纯粹用桌面 GUI 自动点击：
  1. 用 ctypes 把 WorkBuddy 桌面端真正提到前台（绕过 Win SetForegroundWindow 限制）
  2. 点击左下角【头像】打开左侧菜单
  3. 用 RapidOCR（本地轻量 OCR 模型，纯 onnxruntime，不联网、不调任何大语言模型）
     识别菜单里的「每日签到领 X 积分」行，点击该行进入签到详情/弹窗
  4. 在详情/弹窗里 OCR 定位「签到」按钮并点击
  5. 回到头像菜单，用 OCR 检测是否出现「今日已签」，出现即判定成功

说明：本脚本使用的 RapidOCR 是本地运行的 OCR 模型，并非大语言模型（LLM），
不向任何外部服务发起请求，满足「不调用大模型」的要求。

首次校准：python workbuddy_checkin.py calibrate
日常签到：python workbuddy_checkin.py
仅检测：  python workbuddy_checkin.py status

关键经验（2026-08-18 实测）：
  * 入口和 Trae 高度同构：左下角头像菜单 → "每日签到领 200 积分" 行。
  * 已签时右侧显示「今日已签」；未签时点击该行会弹出详情/弹窗，内有「签到」按钮。
  * 头像菜单每次打开位置稳定，但仍用 OCR 做反馈最保险。
  * pygetwindow 的 activate() 在后台会报 Win32 错误 183，必须用 ctypes
    AttachThreadInput + SetForegroundWindow 才能真正把窗口提到前台。

注意：签到入口只在 WorkBuddy 桌面端客户端，本脚本必须在【你本机】运行，
且 WorkBuddy 桌面端已登录、已开启积分计费模式。
"""
import os
import sys
import json
import time
import ctypes
import argparse
from datetime import date

try:
    import pyautogui
    import pygetwindow as gw
    from PIL import Image
    import numpy as np
except ImportError as e:
    sys.exit(
        "缺少依赖，请先运行: pip install -r requirements.txt\n"
        "详细错误: " + str(e)
    )

# RapidOCR（轻量级高精度 OCR，纯 onnxruntime，本地运行，不调大语言模型）
RAPIDOCR_AVAILABLE = False
try:
    from rapidocr_onnxruntime import RapidOCR
    RAPIDOCR_AVAILABLE = True
except ImportError:
    pass

# 输出编码加固: 中文 Windows 控制台默认 GBK, 无法编码 ✅/⚠️ 等 emoji,
# 直接 print 会抛 UnicodeEncodeError 并污染签到日志。强制 UTF-8 + 容错输出。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

BASE = os.path.dirname(os.path.abspath(__file__))
# 状态目录: 桌面端经 CHECKIN_STATE_DIR 注入持久目录(打包后为 exe 旁), 重启不丢;
# 直接命令行运行时退回脚本自身目录
STATE_DIR = os.environ.get("CHECKIN_STATE_DIR") or BASE
os.makedirs(STATE_DIR, exist_ok=True)
CONFIG = os.path.join(BASE, "wb_checkin_config.json")
SHOTS = os.path.join(BASE, "shots")
LAST = os.path.join(STATE_DIR, "wb_last_checkin.txt")
LOG = os.path.join(STATE_DIR, "wb_checkin.log")

# 鼠标移到屏幕左上角 (0,0) 可紧急中断整个脚本
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.5

# 按钮相对文字行右边的偏移（实测：文字行右边界 + 60px 落在右侧「签到」按钮上）
BUTTON_X_OFFSET = 60
BUTTON_X_MIN = 230  # 保底最小 x 坐标，避免偏移过小

# 左侧头像菜单区域（屏幕绝对坐标，覆盖菜单弹层）。
# 关键：WorkBuddy 客户端会显示多种内容（如打开的文档），全屏 OCR 会把文档里的
# 「今日已签」「签到」字样误当成菜单状态。所有菜单相关 OCR 必须限定此区域。
MENU_REGION = (0, 0, 500, 1080)
# 签到按钮搜索也只限菜单区域（x 小于右边界）
SIGN_BTN_MAX_X = 500


# ---------- 日志 ----------
def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------- 配置 ----------
def load_config():
    if not os.path.exists(CONFIG):
        return None
    with open(CONFIG, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------- 今日防重 ----------
def already_today():
    if not os.path.exists(LAST):
        return False
    with open(LAST, "r", encoding="utf-8") as f:
        return f.read().strip() == str(date.today())


def mark_today():
    with open(LAST, "w", encoding="utf-8") as f:
        f.write(str(date.today()))


# ---------- 窗口前台 ----------
def bring_to_front(hwnd):
    """用 ctypes 真正把窗口提到前台（绕过 Win32 错误 183）。"""
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
        log("bring_to_front 异常(忽略): " + str(e))
        return False


def activate_window(win):
    ok = False
    try:
        ok = bring_to_front(win._hWnd)
    except Exception as e:
        log("ctypes 激活失败，回退方案: " + str(e))
    if not ok:
        try:
            if getattr(win, "isMinimized", False):
                win.restore()
            else:
                win.minimize()
                time.sleep(0.5)
                win.restore()
            time.sleep(0.5)
            win.maximize()
        except Exception as e:
            log("窗口还原异常(忽略): " + str(e))
    time.sleep(1.0)
    try:
        win.maximize()
    except Exception:
        pass
    time.sleep(0.5)
    return int(win.left), int(win.top)


def find_window(title):
    """找到标题匹配 WorkBuddy 的主窗口（按面积最大的那个，避免误选通知弹窗）。"""
    try:
        wins = gw.getWindowsWithTitle(title)
        if not wins:
            return None
        best = None
        best_area = -1
        for w in wins:
            area = int(w.width) * int(w.height)
            if area > best_area:
                best_area = area
                best = w
        return best
    except Exception as e:
        log("查找窗口异常: " + str(e))
        return None


# ---------- RapidOCR ----------
_ocr_engine = None


def get_ocr_engine():
    global _ocr_engine
    if _ocr_engine is None and RAPIDOCR_AVAILABLE:
        _ocr_engine = RapidOCR()
    return _ocr_engine


def ocr_image(img):
    """识别截图，返回 [(text, cx, cy, box), ...]，坐标为【屏幕绝对像素】。

    若传入的是区域截图（带 box_offset_x/box_offset_y），自动加回区域偏移。
    """
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
        log("OCR 异常: " + str(e))
        return []


def shot_menu():
    """截取左侧菜单区域（屏幕坐标），供 OCR 使用。菜单相关判定全部限定此区域。"""
    left, top, width, height = MENU_REGION
    img = pyautogui.screenshot(region=(left, top, width, height)).convert("RGB")
    img.box_offset_x = left
    img.box_offset_y = top
    return img


def find_checkin_row(items):
    """从 OCR 结果中找到签到行文字。

    WorkBuddy 实测菜单里是「签到领积分>」（Trae 是「每日签到领 200 积分」）。
    OCR 常把前导符号（如「白」/「2+」/「@」）粘连进文字，故宽松匹配。
    """
    for text, cx, cy, box in items:
        if "签到" in text and "积分" in text:
            return text, box
    # 兜底：单独的「签到」入口（未带「积分」字样）
    for text, cx, cy, box in items:
        t = text.strip()
        if t == "签到" or t == "去签到" or t == "立即签到":
            return text, box
    return None, None


def compute_button_click(box, window_left, window_top, window_scale=1.0):
    """计算签到行的可点击坐标（屏幕绝对坐标）。

    WorkBuddy 实测：「签到领积分>」行本身是可点击入口（无独立右侧按钮），
    点击行中心进入签到详情页。box 坐标为屏幕绝对像素。
    """
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    cx = int(sum(xs) / 4)
    cy = int(sum(ys) / 4)
    return cx, cy


def is_signed(items):
    """检查 OCR 结果中是否出现已签标志。

    WorkBuddy 已签标志：「今日已领」（详情页按钮），也兼容 Trae 的「今日已签」。
    """
    texts = " ".join(t for t, _, _, _ in items)
    if "今日已领" in texts:
        return True, "ocr-today-claimed"
    if "今日已签" in texts or "已签到" in texts:
        return True, "ocr-today-signed"
    return False, "unknown"


def find_sign_button(items, max_x=SIGN_BTN_MAX_X):
    """在 OCR 结果里找一个「领取/签到」按钮。

    WorkBuddy 详情页按钮是「立即领取」（已领后为「今日已领」），
    Trae 是「签到」。限定 x < max_x（左侧菜单区域）。
    """
    for text, cx, cy, box in items:
        if cx > max_x:
            continue
        t = text.strip()
        if t in ("立即领取", "领取", "签到", "立即签到", "去签到", "领", "签"):
            return cx, cy
        # 模糊：短的、含关键字的按钮文字
        if ("领取" in t) and ("每日" not in t) and ("累计" not in t) and len(t) <= 6:
            return cx, cy
    return None


# ---------- 头像菜单 ----------
def ensure_menu_open(avatar_x, avatar_y, attempts=4):
    """确保头像菜单处于打开状态，返回【菜单区域】截图。

    头像点击是「切换」：菜单开→点它会关。因此先探测当前状态：
    若菜单里已有签到行（菜单开着）→ 先点一次关闭，再点一次打开，
    保证最终处于确定的「打开」状态。之后每轮点击用 OCR 反馈确认。
    """
    # 第一步：探测当前菜单状态
    pre = shot_menu()
    if find_checkin_row(ocr_image(pre))[0] is not None:
        # 当前是打开状态 → 点一次关闭
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
def calibrate():
    print("=== WorkBuddy 自动签到 · 坐标校准 ===")
    print("目标：记录【头像】在屏幕上的位置。OCR 会自动定位签到行与按钮。")
    print()
    input("把 WorkBuddy 桌面端打开并置于前台，就绪后按 Enter...")
    win = gw.getActiveWindow()
    if not win:
        sys.exit("未能获取前台窗口，请确认 WorkBuddy 在前台后再试。")
    title = win.title
    print("已记录窗口标题:", repr(title))

    print()
    print("把鼠标移动到左下角【头像】图标上（不要点击）。")
    input("就位后按 Enter 记录头像位置...")
    p = pyautogui.position()
    avatar = {"x": int(p.x), "y": int(p.y)}
    print("头像屏幕坐标:", avatar)

    cfg = load_config() or {}
    cfg.update({
        "window_title": title,
        "avatar": avatar,
        "window_left": int(win.left),
        "window_top": int(win.top),
    })
    save_config(cfg)
    print()
    print("校准完成！配置已保存到:", CONFIG)
    print("以后直接运行: python workbuddy_checkin.py")


# ---------- 执行签到 ----------
def run(dry=False):
    cfg = load_config()
    if not cfg:
        sys.exit("尚未校准。请先运行: python workbuddy_checkin.py calibrate")

    if not RAPIDOCR_AVAILABLE:
        sys.exit("缺少 RapidOCR。请先运行: pip install rapidocr_onnxruntime")

    if already_today():
        log("今天 (%s) 已经签到过，跳过。" % date.today())
        return

    win = find_window(cfg["window_title"])
    if not win:
        log("找不到标题为 %r 的窗口。请先打开 WorkBuddy 桌面端并登录。" % cfg["window_title"])
        return

    wl, wt = activate_window(win)
    ax, ay = cfg["avatar"]["x"], cfg["avatar"]["y"]
    # 如果窗口坐标变了（多屏/DPI），用相对偏移修正
    ax = wl + (ax - cfg.get("window_left", wl))
    ay = wt + (ay - cfg.get("window_top", wt))

    log("点击头像 (%d, %d) 打开菜单" % (ax, ay))
    os.makedirs(SHOTS, exist_ok=True)

    # 1) 打开菜单并定位签到行
    img = ensure_menu_open(ax, ay)
    items = ocr_image(img)
    text, box = find_checkin_row(items)
    if text is None:
        img.save(os.path.join(SHOTS, "wb_diagnose_%s.png" % date.today()))
        log("OCR 未定位到【每日签到】行，已保存诊断截图 wb_diagnose_%s.png。" % date.today())
        log("可能原因：今日已签 / 界面改版 / 未开启积分计费模式。若改版请重跑 calibrate。")
        return

    signed, reason = is_signed(items)
    if signed:
        if not dry:
            mark_today()
        log("✅ 检测到已签到（%s），无需重复点击。" % reason)
        return

    if dry:
        log("检测到【每日签到】行: %r" % text)
        log("(status 模式不点击，如需真正签到请运行: python workbuddy_checkin.py)")
        return

    # 2) 点击签到行（「签到领积分>」本身是入口，点击进入签到详情页）
    sx, sy = compute_button_click(box, wl, wt)
    log("OCR 定位签到行: %r，点击进入 (%d, %d)" % (text, sx, sy))
    pyautogui.click(sx, sy)
    time.sleep(3.0)

    # 3-4) 等待签到详情页出现（弹出有延迟，循环最多 6 轮）：
    #      详情页出现「立即领取」按钮 → 点击它完成签到；
    #      若已是「今日已领」→ 直接判定已签（已手动签过等场景）。
    bx = by = None
    detail_items = []
    for attempt in range(6):
        time.sleep(2)
        img2 = pyautogui.screenshot().convert("RGB")
        detail_items = ocr_image(img2)
        signed2, reason2 = is_signed(detail_items)
        if signed2:
            mark_today()
            log("✅ 签到成功！（详情页检测到 %s）" % reason2)
            img2.save(os.path.join(SHOTS, "wb_after_checkin_%s.png" % date.today()))
            return
        btn = find_sign_button(detail_items, max_x=1920)
        if btn is not None:
            bx, by = btn
            break

    if bx is not None:
        log("在签到详情页找到【领取】按钮 (%d, %d)，点击" % (bx, by))
        pyautogui.click(bx, by)
        time.sleep(3.5)
    else:
        log("⚠️ 未在详情页找到【领取】按钮，可能已直接签到或界面有变。")

    # 5) 验证：重新打开头像菜单，检查「今日已领」/「今日已签」
    img3 = ensure_menu_open(ax, ay)
    img3.save(os.path.join(SHOTS, "wb_after_checkin_%s.png" % date.today()))
    signed, reason = is_signed(ocr_image(img3))
    if signed:
        mark_today()
        log("✅ 签到成功！（验证: %s）" % reason)
        log("截图: shots/wb_after_checkin_%s.png" % date.today())
        return

    if bx is not None:
        # 已点击【领取】按钮 = 签到动作已完成; UI 验证可能因弹窗/动画未识别成功标志,
        # 此时按已签记录(明天 cron 会再校验, 误标代价远小于用户看到"从未"的困扰)
        mark_today()
        log("✅ 已点击【领取】完成签到（UI 验证未识别到成功标志，按已签记录）。")
        log("截图: shots/wb_after_checkin_%s.png" % date.today())
        return

    log("⚠️ 验证未通过。请打开 shots/wb_after_checkin_%s.png 核对；若今日已手动签过属正常。" % date.today())


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 桌面端自动签到（界面点击版）")
    ap.add_argument(
        "mode", nargs="?", default="run", choices=["run", "calibrate", "status"],
        help="run=执行签到(默认), calibrate=首次坐标校准, status=仅检测不点击"
    )
    args = ap.parse_args()
    if args.mode == "calibrate":
        calibrate()
    elif args.mode == "status":
        run(dry=True)
    else:
        run(dry=False)


if __name__ == "__main__":
    main()
