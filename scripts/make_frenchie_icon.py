# -*- coding: utf-8 -*-
"""
法斗应用图标生成器 v2 — 纯 Pillow 绘制, 无外部素材依赖
输出: desktop/assets/frenchie.png (256px) + frenchie.ico (16..256 多尺寸)
用法: .venv\\Scripts\\python.exe scripts\\make_frenchie_icon.py

设计(1024 画布, 超采样后缩小):
- 圆角方形品牌蓝渐变底
- 米黄法斗头: 宽扁圆头 + 腮帮, 直立的深棕蝙蝠耳(多边形+圆尖),
  黑色眼罩式口鼻部, 短扁黑鼻, 微笑嘴线
"""
from pathlib import Path
from PIL import Image, ImageDraw

S = 4                 # 超采样倍数
CANVAS = 256 * S      # 1024

BG_TOP = (23, 119, 255)
BG_BOT = (13, 88, 199)
FAWN = (232, 196, 138)
FAWN_DARK = (214, 173, 110)
EAR = (74, 59, 50)
EAR_INNER = (104, 85, 72)
MASK = (74, 59, 50)
NOSE = (30, 24, 20)
EYE = (28, 22, 18)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def rounded_gradient_bg(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad = Image.new("RGBA", (size, size))
    px = grad.load()
    for y in range(size):
        c = lerp(BG_TOP, BG_BOT, y / max(size - 1, 1))
        for x in range(size):
            px[x, y] = (*c, 255)
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.225), fill=255)
    img.paste(grad, (0, 0), mask)
    return img


def mirror(points, size):
    """沿垂直中轴镜像(右耳 = 左耳镜像)"""
    return [(size - x, y) for x, y in points]


def main():
    size = CANVAS
    img = rounded_gradient_bg(size)
    d = ImageDraw.Draw(img)

    # ── 头部几何 ──
    head_cx, head_cy = size // 2, int(size * 0.60)
    head_w, head_h = int(size * 0.63), int(size * 0.54)
    head_l = head_cx - head_w // 2          # 197
    head_t = head_cy - head_h // 2          # ~322

    # ── 蝙蝠耳: 多边形直绘(左耳, 右耳镜像) ──
    left_ear_outline = [
        (head_l + int(size * 0.045), head_t + int(head_h * 0.36)),   # 外基
        (head_l + int(size * 0.030), head_t - int(size * 0.075)),    # 外缘上行
        (head_l + int(size * 0.055), head_t - int(size * 0.160)),    # 外缘收拢
        (head_l + int(size * 0.095), head_t - int(size * 0.215)),    # 近尖外
        (head_l + int(size * 0.130), head_t - int(size * 0.235)),    # 尖顶
        (head_l + int(size * 0.168), head_t - int(size * 0.210)),    # 尖内
        (head_l + int(size * 0.196), head_t - int(size * 0.140)),    # 内缘下行
        (head_l + int(size * 0.212), head_t - int(size * 0.045)),    # 内缘
        (head_l + int(size * 0.218), head_t + int(head_h * 0.30)),   # 内基
    ]
    d.polygon(left_ear_outline, fill=(*EAR, 255))
    # 圆尖: 在尖端处叠一个圆
    tip_x, tip_y = head_l + int(size * 0.131), head_t - int(size * 0.212)
    tip_r = int(size * 0.038)
    d.ellipse([tip_x - tip_r, tip_y - tip_r, tip_x + tip_r, tip_y + tip_r], fill=(*EAR, 255))
    # 耳内浅色(缩小版轮廓上移)
    inner = [(int(head_cx + (x - head_cx) * 0.52), int((y - (head_t + head_h * 0.30)) * 0.60 + head_t - size * 0.02))
             for x, y in left_ear_outline]
    d.polygon(inner, fill=(*EAR_INNER, 255))

    right_ear_outline = mirror(left_ear_outline, size)
    d.polygon(right_ear_outline, fill=(*EAR, 255))
    rt_x = size - tip_x
    d.ellipse([rt_x - tip_r, tip_y - tip_r, rt_x + tip_r, tip_y + tip_r], fill=(*EAR, 255))
    inner_r = mirror(inner, size)
    d.polygon(inner_r, fill=(*EAR_INNER, 255))

    # ── 头: 宽扁大圆头(盖住耳基) ──
    d.ellipse([head_l, head_t, head_l + head_w, head_t + head_h], fill=(*FAWN, 255))
    # 头顶一条暗带做立体感
    d.ellipse([head_l + int(size * 0.03), head_t,
               head_l + head_w - int(size * 0.03), head_t + int(head_h * 0.42)],
              fill=(*FAWN_DARK, 255))
    d.ellipse([head_l, head_t + int(head_h * 0.16),
               head_l + head_w, head_t + head_h], fill=(*FAWN, 255))

    # ── 腮帮子 ──
    jaw_r = int(size * 0.150)
    jaw_cy = head_cy + int(head_h * 0.24)
    for sx in (-1, 1):
        jx = head_cx + sx * int(size * 0.205)
        d.ellipse([jx - jaw_r, jaw_cy - jaw_r, jx + jaw_r, jaw_cy + jaw_r], fill=(*FAWN, 255))

    # ── 黑色眼罩(覆盖眼周与口鼻) ──
    mask_w, mask_h = int(size * 0.38), int(size * 0.31)
    mask_cy = head_cy + int(head_h * 0.12)
    d.ellipse([head_cx - mask_w // 2, mask_cy - mask_h // 2,
               head_cx + mask_w // 2, mask_cy + mask_h // 2], fill=(*MASK, 255))

    # ── 眼睛 ──
    eye_dx = int(size * 0.108)
    eye_y = head_cy - int(head_h * 0.01)
    eye_rx, eye_ry = int(size * 0.029), int(size * 0.037)
    hr = max(int(size * 0.007), S // 2)
    for sx in (-1, 1):
        ex = head_cx + sx * eye_dx
        d.ellipse([ex - eye_rx, eye_y - eye_ry, ex + eye_rx, eye_y + eye_ry], fill=(*EYE, 255))
        hx, hy = ex - int(eye_rx * 0.25), eye_y - int(eye_ry * 0.35)
        d.ellipse([hx - hr, hy - hr, hx + hr, hy + hr], fill=(245, 245, 245, 255))

    # ── 短扁宽鼻 ──
    nose_w, nose_h = int(size * 0.100), int(size * 0.058)
    nose_cy = mask_cy + int(mask_h * 0.14)
    d.rounded_rectangle(
        [head_cx - nose_w // 2, nose_cy - nose_h // 2,
         head_cx + nose_w // 2, nose_cy + nose_h // 2],
        radius=nose_h // 2, fill=(*NOSE, 255))

    # ── 嘴: 人中 + 双侧微笑 ──
    line_w = max(int(size * 0.009), 2)
    philtrum_len = int(size * 0.042)
    mouth_y0 = nose_cy + nose_h // 2
    d.line([head_cx, mouth_y0, head_cx, mouth_y0 + philtrum_len],
           fill=(*NOSE, 255), width=line_w)
    smile_r = int(size * 0.052)
    smile_cy = mouth_y0 + philtrum_len
    d.arc([head_cx - smile_r, smile_cy - smile_r,
           head_cx, smile_cy + smile_r], start=270, end=180, fill=(*NOSE, 255), width=line_w)
    d.arc([head_cx, smile_cy - smile_r,
           head_cx + smile_r, smile_cy + smile_r], start=0, end=90, fill=(*NOSE, 255), width=line_w)

    # ── 输出 ──
    out_dir = Path(__file__).resolve().parent.parent / "desktop" / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)
    png_path = out_dir / "frenchie.png"
    img.resize((256, 256), Image.LANCZOS).save(png_path)
    img.resize((256, 256), Image.LANCZOS).save(
        out_dir / "frenchie.ico", format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"OK -> {png_path}")
    print(f"OK -> {out_dir / 'frenchie.ico'}")


if __name__ == "__main__":
    main()
