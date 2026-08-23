# -*- coding: utf-8 -*-
"""桌面壳托盘逻辑冒烟测试(不打开真窗口; 会短暂出现一个托盘图标后自动消失)"""
import importlib.util, threading, time, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))          # backend 等
sys.path.insert(0, str(ROOT / "desktop"))  # scheduler 模块

spec = importlib.util.spec_from_file_location("dtop", ROOT / "desktop" / "desktop.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# 1) 托盘图加载(app.ico 占位存在 → 应成功载入)
img = m._load_tray_image()
assert img.size[0] > 0, "托盘图加载失败"
print(f"1. tray image loaded: {img.format}/{img.mode}/{img.size}")

# 2) 关闭→隐藏 / 退出→放行 语义
class StubWindow:
    def __init__(self):
        self.hidden = False
        self.destroyed = False
    def hide(self): self.hidden = True
    def show(self): self.hidden = False
    def destroy(self): self.destroyed = True

w = StubWindow()
handler = m._make_closing_handler(w)
r1 = handler()
assert r1 is False and w.hidden and not w.destroyed, f"关闭应取消并隐藏: r={r1} hidden={w.hidden}"
print("2. close -> cancel & hide:", r1, "hidden =", w.hidden)

m._really_exit = True
r2 = handler()
assert r2 is None and not w.destroyed or w.destroyed, f"退出应放行: r={r2}"
print("3. really-exit -> allow close:", r2)

# 3) pystray 真实起停(验证消息循环可用)
import pystray
icon = pystray.Icon("gw-smoke", img, "智能网关 冒烟")
t = threading.Thread(target=icon.run, daemon=True)
t.start()
time.sleep(1.2)
visible = icon.visible
icon.stop()
time.sleep(0.4)
print(f"4. pystray start/stop OK, was_visible={visible}")

print("ALL TRAY SMOKE PASS")
