"""生成安装包用的图标和 DMG 背景（只在 Mac 上跑一次，结果放在 packaging/icons/，提交到仓库）。

    .venv-build/bin/python packaging/make_icons.py

- 源图：brand/logo.svg → 用 macOS 自带的 qlmanage 渲染成 1024×1024 PNG
- Mac：app.icns（按 macOS 图标规范四周留白，iconutil 生成）
- Windows：app.ico（16–256 多尺寸，Pillow 生成）
- DMG 窗口背景：dmg-background.png / @2x（左边 App，右边“应用程序”，中间箭头和一句说明）
- 菜单栏 / 托盘图标（0.2.2）：web/tray/ —— Mac 菜单栏用黑色模板图（系统按深色 / 浅色菜单栏自动着色），
  Windows 托盘用彩色 .ico，另有带红点的版本（有未处理的紧急提醒时显示）
    只生成托盘图标：.venv-build/bin/python packaging/make_icons.py --tray
"""
import sys
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "packaging" / "icons"


def render_svg(svg: Path, size=1024) -> Image.Image:
    tmp = Path(tempfile.mkdtemp())
    subprocess.run(["qlmanage", "-t", "-s", str(size), "-o", str(tmp), str(svg)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    img = Image.open(tmp / (svg.name + ".png")).convert("RGBA")
    shutil.rmtree(tmp, ignore_errors=True)
    return img


def mac_icon(src: Image.Image) -> Image.Image:
    """macOS 规范：1024 画布里图形约 824×824，居中，四周透明。"""
    canvas = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    art = src.resize((824, 824), Image.LANCZOS)
    canvas.alpha_composite(art, (100, 100))
    return canvas


def make_icns(img: Image.Image, dest: Path):
    tmp = Path(tempfile.mkdtemp()) / "app.iconset"
    tmp.mkdir()
    for s in (16, 32, 128, 256, 512):
        img.resize((s, s), Image.LANCZOS).save(tmp / f"icon_{s}x{s}.png")
        img.resize((s * 2, s * 2), Image.LANCZOS).save(tmp / f"icon_{s}x{s}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(tmp), "-o", str(dest)], check=True)
    shutil.rmtree(tmp.parent, ignore_errors=True)


def _font(size):
    for f in ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Medium.ttc",
              "/System/Library/Fonts/Helvetica.ttc"):
        if os.path.exists(f):
            try:
                return ImageFont.truetype(f, size)
            except OSError:
                continue
    return ImageFont.load_default()


def dmg_background(scale: int, dest: Path):
    """640×400 的窗口：App 图标在 (160, 190)，“应用程序”在 (480, 190)，中间画箭头。"""
    w, h = 640 * scale, 400 * scale
    img = Image.new("RGB", (w, h), (247, 247, 251))
    d = ImageDraw.Draw(img)
    y = 190 * scale
    x0, x1 = 250 * scale, 390 * scale
    d.line([(x0, y), (x1, y)], fill=(67, 56, 202), width=6 * scale)
    d.polygon([(x1 + 22 * scale, y), (x1 - 4 * scale, y - 16 * scale), (x1 - 4 * scale, y + 16 * scale)],
              fill=(67, 56, 202))
    for text, yy, size, color in (("把 詹姆斯智能体 拖到 “应用程序” 文件夹", 320, 17, (40, 40, 60)),
                                  ("Drag James Agent into the Applications folder", 346, 13, (110, 110, 130))):
        f = _font(size * scale)
        tw = d.textlength(text, font=f)
        d.text(((w - tw) / 2, yy * scale), text, fill=color, font=f)
    img.save(dest, dpi=(72 * scale, 72 * scale))


def tray_icons(src: Image.Image):
    """菜单栏模板图：logo 里的“AI 团队”（三角连线 + 三个成员），黑色 + 透明，36×36（18pt @2x）。"""
    out = ROOT / "web" / "tray"
    out.mkdir(parents=True, exist_ok=True)
    big = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.line([(256, 96), (110, 372), (402, 372), (256, 96)], fill=(0, 0, 0, 255), width=40, joint="curve")
    for cx, cy, r in ((256, 104, 74), (116, 368, 62), (396, 368, 62)):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(0, 0, 0, 255))
    big.resize((36, 36), Image.LANCZOS).save(out / "tray-mac.png")
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48)]
    src.save(out / "tray.ico", sizes=sizes)
    alert = src.copy()
    d = ImageDraw.Draw(alert)
    w = alert.width
    d.ellipse([w * 0.52, w * 0.52, w * 0.98, w * 0.98], fill=(220, 38, 38, 255), outline=(255, 255, 255, 255),
              width=int(w * 0.05))
    alert.save(out / "tray-alert.ico", sizes=sizes)
    print("tray icons written to", out)


def main():
    if "--tray" in sys.argv:
        return tray_icons(Image.open(OUT / "app-1024.png").convert("RGBA"))
    OUT.mkdir(parents=True, exist_ok=True)
    src = render_svg(ROOT / "brand" / "logo.svg")
    src.save(OUT / "app-1024.png")
    make_icns(mac_icon(src), OUT / "app.icns")
    src.save(OUT / "app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    dmg_background(1, OUT / "dmg-background.png")
    dmg_background(2, OUT / "dmg-background@2x.png")
    tray_icons(src)
    print("icons written to", OUT)


if __name__ == "__main__":
    main()
