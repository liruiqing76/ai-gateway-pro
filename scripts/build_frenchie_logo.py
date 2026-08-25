# -*- coding: utf-8 -*-
"""
法斗项目 Logo / 图标资源生成器
================================
以 docs/logo 下选中的「方案 A」(A_professional_minimalist_flat_*.png) 为准,
把它做成统一的「白色圆角底 + navy+orange 法斗」素材, 输出到各使用位:

  desktop/assets/app.ico   任务栏 / 系统托盘 / 窗口标题栏图标 (多尺寸 16..256)
  desktop/assets/app.png   高分辨率回退 (托盘/窗口)
  web/favicon.png          前端 favicon (被桌面端直接 serve, 立即生效)
  web-vue/public/favicon.png   源码侧 favicon (npm build 后同步到 web/)
  web/frenchie-logo.png    App 内品牌图标 (立即生效)
  web-vue/public/frenchie-logo.png  App 内品牌图标源码 (build 同步)

设计要点: 白色圆角底 + 透明圆角。浅色主题下白底隐形、只显法斗;
深色主题下显白底芯片, 两种主题都清晰。所有位统一视觉。

用法: .venv\Scripts\python.exe scripts\build_frenchie_logo.py
"""
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent

# 自动挑 docs/logo 下最新生成的「方案 A」文件
logo_dir = ROOT / "docs" / "logo"
cands = sorted(logo_dir.glob("A_professional_minimalist_flat_*.png"))
if not cands:
    raise SystemExit("找不到方案 A 的源图, 请在 docs/logo 下确认文件名")
SRC = cands[-1]
print(f"源图: {SRC.name}")

src = Image.open(SRC).convert("RGBA")  # 1024x1024, 白底 navy 法斗


def rounded_tile(im, size, radius_ratio: float = 0.20):
    """把图缩放到 size, 套圆角矩形遮罩: 内部保留(白底+法斗), 圆角外透明。"""
    im = im.resize((size, size), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * radius_ratio), fill=255
    )
    out.paste(im, (0, 0), mask)
    return out


# ── 1) 任务栏 / 托盘 / 窗口图标 (.ico 多尺寸) ──
ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
base512 = rounded_tile(src, 512)
base512.save(
    ROOT / "desktop" / "assets" / "app.ico",
    format="ICO",
    sizes=ico_sizes,
)
print("OK -> desktop/assets/app.ico")

# ── 2) 高分辨率回退 png ──
base512.save(ROOT / "desktop" / "assets" / "app.png")
print("OK -> desktop/assets/app.png")

# ── 3) favicon (png, 透明圆角白底) ──
fav = rounded_tile(src, 256)
fav.save(ROOT / "web" / "favicon.png")
fav.save(ROOT / "web-vue" / "public" / "favicon.png")
print("OK -> web/favicon.png + web-vue/public/favicon.png")

# ── 4) App 内品牌图标 ──
logo = rounded_tile(src, 256)
logo.save(ROOT / "web" / "frenchie-logo.png")
logo.save(ROOT / "web-vue" / "public" / "frenchie-logo.png")
print("OK -> web/frenchie-logo.png + web-vue/public/frenchie-logo.png")

print("全部资源生成完成 ✅")
