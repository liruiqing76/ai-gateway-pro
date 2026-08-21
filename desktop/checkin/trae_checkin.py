#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trae Work 桌面端 自动签到 —— 界面点击版（pyautogui + RapidOCR）

原理：不调用任何内部 API，纯粹用桌面 GUI 自动点击：
  1. 用 ctypes 把 Trae Work 桌面端真正提到前台（绕过 Win SetForegroundWindow 限制）
  2. 点击左下角【头像】切换弹出左侧菜单
  3. 用 RapidOCR（本地轻量 OCR 模型，纯 onnxruntime，不联网、不调任何大语言模型）
     识别菜单里的「每日签到领 200 积分」行，计算右侧「签到」按钮位置，点击按钮
  4. 再次打开菜单，用 OCR 检测是否出现「今日已签」，出现即判定成功

说明：本脚本使用的 RapidOCR 是本地运行的 OCR 模型，并非大语言模型（LLM），
不向任何外部服务发起请求，满足「不调用大模型」的要求。

首次校准：python trae_checkin.py calibrate
日常签到：python trae_checkin.py
仅检测：  python trae_checkin.py status

关键经验（2026-08-18 实测）：
  * 头像菜单每次打开位置会漂移，不能靠固定坐标。
  * 头像点击是「切换」而非纯打开，必须做 OCR 反馈：若菜单没出现则再点一次。
  * OCR 把「每日签到领 200 积分」文字和右侧「签到」按钮常识别成同一行；
    必须取文字行的【右边界 + 固定偏移】来点真正的按钮，点文字中心无效。
  * pygetwindow 的 activate() 在后台会报 Win32 错误 183，必须用 ctypes
    AttachThreadInput + SetForegroundWindow 才能真正把窗口提到前台。

注意：签到入口只在 Trae Work 桌面端客户端（官方社区确认网页/手机端无入口），
所以本脚本必须在【你本机】运行，且 Trae Work 桌面端已登录、已开启积分计费模式。
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

# OCR 引擎: rapid(默认, rapidocr_onnxruntime, 纯onnxruntime轻量) / paddle(paddleocr 3.x)
# 可通过环境变量 CHECKIN_OCR 或命令行 --engine 切换。
OCR_ENGINE = os.environ.get("CHECKIN_OCR", "rapid").lower()

RAPIDOCR_AVAILABLE = False
try:
    from rapidocr_onnxruntime import RapidOCR
    RAPIDOCR_AVAILABLE = True
except ImportError:
    pass

PADDLEOCR_AVAILABLE = False
try:
    from paddleocr import PaddleOCR
    PADDLEOCR_AVAILABLE = True
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
CONFIG = os.path.join(BASE, "checkin_config.json")
SHOTS = os.path.join(BASE, "shots")
LAST = os.path.join(STATE_DIR, "last_checkin.txt")
LOG = os.path.join(STATE_DIR, "checkin.log")

# 鼠标移到屏幕左上角 (0,0) 可紧急中断整个脚本
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.5

# 按钮相对文字行右边的偏移（实测：文字右边界 + 60px 落在右侧「签到」按钮上）
BUTTON_X_OFFSET = 60
BUTTON_X_MIN = 230
# 右侧「签到」按钮带（模板匹配备份验证用）
DEFAULT_SEARCH_BAND = {"x0": 180, "y0": 130, "x1": 290, "y1": 230}
# 判定"按钮消失"的模板匹配分数阈值（越大越不像；实测成功 ~1150000，失败 ~0）
DISAPPEAR_THRESHOLD = 150000


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
        # 回退：最小化再恢复，通常能拿到前台
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
    try:
        wins = gw.getWindowsWithTitle(title)
        return wins[0] if wins else None
    except Exception as e:
        log("查找窗口异常: " + str(e))
        return None


# ---------- OCR 引擎(rapid / paddle 双后端, 统一输出) ----------
_ocr_engine = None
_engine_name = None


def _run_rapid(img_bgr):
    """RapidOCR: 入参 numpy BGR, 返回 [(text, cx, cy, box)] (box 为图像像素四点)."""
    engine = RapidOCR()
    result, _ = engine(img_bgr)
    out = []
    if result:
        for box, text, score in result:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            cx = int(sum(xs) / 4)
            cy = int(sum(ys) / 4)
            out.append((text, cx, cy, box))
    return out


def _run_paddle(img_bgr):
    """PaddleOCR 3.x: 入参 numpy BGR(ndarray), 返回统一 [(text, cx, cy, box)].
    dt_polys 是相对图像 4 点坐标, rec_texts/rec_scores 按行对齐。"""
    engine = PaddleOCR(use_textline_orientation=False, lang="ch")
    res = engine.predict(input=img_bgr)
    out = []
    for r in res or []:
        if not isinstance(r, dict):
            continue
        polys = r.get("dt_polys") or []
        texts = r.get("rec_texts") or []
        for i, txt in enumerate(texts):
            box = polys[i] if i < len(polys) else None
            if box is None:
                continue
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            cx = int(sum(xs) / len(xs))
            cy = int(sum(ys) / len(ys))
            out.append((txt, cx, cy, box))
    return out


def get_ocr_engine():
    """按 OCR_ENGINE 选择可用后端实例, 返回 (backend_callable, name)."""
    global _ocr_engine, _engine_name
    if _ocr_engine is not None:
        return _ocr_engine, _engine_name
    if OCR_ENGINE == "paddle":
        if PADDLEOCR_AVAILABLE:
            _ocr_engine, _engine_name = _run_paddle, "paddle"
        else:
            log("未安装 paddleocr, 回退到 rapid")
    else:
        if RAPIDOCR_AVAILABLE:
            _ocr_engine, _engine_name = _run_rapid, "rapid"
        else:
            log("未安装 rapidocr, 尝试 paddle")
    if _ocr_engine is None and PADDLEOCR_AVAILABLE:
        _ocr_engine, _engine_name = _run_paddle, "paddle"
    return _ocr_engine, _engine_name


def ocr_image(img):
    """识别整张窗口截图，返回 [(text, cx, cy, box), ...]（box 为窗口内像素）。"""
    backend, name = get_ocr_engine()
    if not backend:
        return []
    try:
        arr = np.array(img)[:, :, ::-1]  # PIL(RGB) -> numpy(BGR)
        return backend(arr)
    except Exception as e:
        log("OCR(%s) 异常: %r" % (name, e))
        return []


def find_checkin_row(img):
    """找到「每日签到领 200 积分」文字行，返回 (text, box) 或 None。
    box = [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]，单位为窗口内像素。"""
    for text, cx, cy, box in ocr_image(img):
        if "签到" in text and "积分" in text:
            return text, box
    return None, None


def compute_button_click(box, window_left, window_top):
    """根据文字行的 bbox，计算右侧「签到」按钮的可点击坐标（屏幕绝对坐标）。"""
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    right = int(max(xs))
    top = int(min(ys))
    bottom = int(max(ys))
    cy = (top + bottom) // 2
    tx = max(right + BUTTON_X_OFFSET, BUTTON_X_MIN)
    return window_left + tx, window_top + cy


def is_signed(img):
    """返回 (signed:bool, reason:str)。"""
    results = ocr_image(img)
    texts = " ".join(t for t, _, _, _ in results)
    if "今日已签" in texts or "已签到" in texts:
        return True, "ocr-today-signed"
    return False, "unknown"


# ---------- 模板匹配（备份验证） ----------
def template_score(img, tmpl_path, band):
    """在 img 的 band 区域内用按钮模板匹配，返回分数（越大越不像/按钮已消失）。"""
    try:
        tmpl = Image.open(tmpl_path).convert("RGB")
    except Exception as e:
        log("加载模板失败: " + str(e))
        return 1e18
    try:
        sub = img.crop((band["x0"], band["y0"], band["x1"], band["y1"]))
        # numpy 计算模板在子图每个位置的最小差异
        import numpy as np
        sub_np = np.array(sub)
        tmpl_np = np.array(tmpl)
        th, tw = tmpl_np.shape[:2]
        sh, sw = sub_np.shape[:2]
        if sw < tw or sh < th:
            return 1e18
        best = 1e18
        for y in range(0, sh - th, 2):
            for x in range(0, sw - tw, 2):
                patch = sub_np[y:y + th, x:x + tw]
                diff = np.abs(patch.astype(int) - tmpl_np.astype(int)).sum()
                if diff < best:
                    best = diff
        return int(best)
    except Exception as e:
        log("模板匹配异常: " + str(e))
        return 1e18


def ensure_menu_open(win, ax, ay, region, attempts=3):
    """确保头像菜单处于打开状态，返回菜单截图。
    头像点击是切换，若 OCR 没找到签到行则再点一次。"""
    last_img = None
    for i in range(attempts):
        pyautogui.click(ax, ay)
        time.sleep(2.5)
        last_img = pyautogui.screenshot(region=region).convert("RGB")
        if find_checkin_row(last_img)[0] is not None:
            return last_img
    return last_img


# ---------- 校准 ----------
def calibrate():
    print("=== Trae Work 自动签到 · 坐标校准 ===")
    print("目标：记录【头像】相对窗口左上角的坐标。OCR 会自动定位签到行与按钮。")
    print()
    input("把 Trae Work 桌面端打开并置于前台，就绪后按 Enter...")
    win = gw.getActiveWindow()
    if not win:
        sys.exit("未能获取前台窗口，请确认 Trae Work 在前台后再试。")
    title = win.title
    print("已记录窗口标题:", repr(title))

    print()
    print("把鼠标移动到左下角【头像】图标上（不要点击）。")
    input("就位后按 Enter 记录头像位置...")
    p = pyautogui.position()
    avatar = {"rel_x": int(p.x - win.left), "rel_y": int(p.y - win.top)}
    print("头像相对窗口坐标:", avatar)

    cfg = load_config() or {}
    cfg.update({
        "window_title": title,
        "avatar": avatar,
        "button_template": "trae_signin_btn.png",
        "search_band": DEFAULT_SEARCH_BAND,
    })
    save_config(cfg)
    print()
    print("校准完成！配置已保存到:", CONFIG)
    print("以后直接运行: python trae_checkin.py")


# ---------- 执行签到 ----------
def run(dry=False):
    cfg = load_config()
    if not cfg:
        sys.exit("尚未校准。请先运行: python trae_checkin.py calibrate")

    backend, name = get_ocr_engine()
    if not backend:
        sys.exit("缺少 OCR 引擎。请先安装 rapidocr_onnxruntime(pip install rapidocr_onnxruntime) 或 paddleocr(pip install paddleocr)")

    if already_today():
        log("今天 (%s) 已经签到过，跳过。" % date.today())
        return

    win = find_window(cfg["window_title"])
    if not win:
        log("找不到标题为 %r 的窗口。请先打开 Trae Work 桌面端并登录。" % cfg["window_title"])
        return

    wl, wt = activate_window(win)

    ax = wl + int(cfg["avatar"]["rel_x"])
    ay = wt + int(cfg["avatar"]["rel_y"])
    log("点击头像 (%d, %d) 打开菜单" % (ax, ay))

    region = (wl, wt, int(win.width), int(win.height))
    os.makedirs(SHOTS, exist_ok=True)

    img = ensure_menu_open(win, ax, ay, region)
    text, box = find_checkin_row(img)
    if text is None:
        img.save(os.path.join(SHOTS, "diagnose_%s.png" % date.today()))
        log("OCR 未定位到【每日签到】行，已保存诊断截图 diagnose_%s.png。" % date.today())
        log("可能原因：今日已签 / 界面改版 / 未开启积分计费模式。若改版请重跑 calibrate。")
        return

    if dry:
        log("检测到【每日签到】行: %r" % text)
        log("(status 模式不点击，如需真正签到请运行: python trae_checkin.py)")
        return

    sx, sy = compute_button_click(box, wl, wt)
    log("OCR 定位签到行: %r，点击右侧按钮 (%d, %d)" % (text, sx, sy))
    pyautogui.click(sx, sy)
    time.sleep(3.5)

    # 验证：再次确保菜单打开，检查「今日已签」
    img2 = ensure_menu_open(win, ax, ay, region)
    img2.save(os.path.join(SHOTS, "after_checkin_%s.png" % date.today()))

    signed, reason = is_signed(img2)
    if signed:
        mark_today()
        log("✅ 签到成功！（验证: %s）" % reason)
        log("截图: shots/after_checkin_%s.png" % date.today())
        return

    # 备份：模板判断右侧按钮是否消失
    tmpl_path = os.path.join(BASE, cfg.get("button_template", "trae_signin_btn.png"))
    band = cfg.get("search_band", DEFAULT_SEARCH_BAND)
    if os.path.exists(tmpl_path):
        score = template_score(img2, tmpl_path, band)
        if score >= DISAPPEAR_THRESHOLD:
            mark_today()
            log("✅ 签到成功！（右侧按钮消失，模板分 %d >= %d）" % (score, DISAPPEAR_THRESHOLD))
            return
        log("⚠️ OCR 未识别到今日已签，且右侧按钮仍可能可见（模板分 %d < %d）。" % (score, DISAPPEAR_THRESHOLD))

    log("⚠️ 验证未通过。请打开 shots/after_checkin_%s.png 核对；若今日已手动签过属正常。" % date.today())


def main():
    global OCR_ENGINE
    ap = argparse.ArgumentParser(description="Trae Work 桌面端自动签到（界面点击版）")
    ap.add_argument(
        "mode", nargs="?", default="run", choices=["run", "calibrate", "status"],
        help="run=执行签到(默认), calibrate=首次坐标校准, status=仅检测不点击")
    ap.add_argument(
        "--engine", choices=["rapid", "paddle"], default=None,
        help="OCR 引擎: rapid(默认, rapidocr_onnxruntime) / paddle(paddleocr)。也可用环境变量 CHECKIN_OCR")
    args = ap.parse_args()
    if args.engine:
        OCR_ENGINE = args.engine
    if args.mode == "calibrate":
        calibrate()
    elif args.mode == "status":
        run(dry=True)
    else:
        run(dry=False)


if __name__ == "__main__":
    main()
