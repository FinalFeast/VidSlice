"""把 SVG 图标渲染成 Windows 多尺寸 .ico，并生成一张对比预览图。

渲染用系统自带的 Chromium 内核浏览器（Windows 10/11 都有 Edge）做无头截图——
它能输出**带 alpha 通道**的 PNG，透明背景和抗锯齿都是浏览器级质量。
（备选的 svglib/reportlab 路线不支持 alpha，圆角外侧会留下白边。）

用法：
    C:\\pybuild\\python.exe tools\\build\\make-icon.py icon.svg \\
        --ico dist/app.ico --preview dist/app-preview.png

需要 Pillow（`pip install pillow`），浏览器无需额外安装。
"""

from __future__ import annotations

import argparse
import io
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

# Chromium 内核浏览器的常见位置，按优先级尝试
BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

# .ico 内包含的尺寸：16–48 用于任务栏与资源管理器，256 用于大图标视图
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
# 这个尺寸及以上用 PNG 存储（体积小），其余用 BMP
# —— Windows 对小尺寸只认 BMP，PNG 条目会让资源管理器退回默认通用图标。
PNG_FROM = 256
# 主渲染分辨率，之后逐级 LANCZOS 缩小
MASTER = 1024
# 预览图上展示的尺寸
PREVIEW_SIZES = [256, 128, 64, 48, 32, 24, 16]

HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
html,body{{margin:0;padding:0;width:100%;height:100%;background:transparent;overflow:hidden}}
svg{{display:block;width:100%;height:100%}}
</style></head><body>
{svg}
</body></html>
"""


def find_browser() -> str:
    for path in BROWSERS:
        if Path(path).is_file():
            return path
    found = shutil.which("msedge") or shutil.which("chrome")
    if found:
        return found
    raise RuntimeError("找不到 Chromium 内核浏览器（Edge 或 Chrome），无法渲染 SVG")


def render_svg(svg_path: Path, out_png: Path, size: int = MASTER) -> Path:
    """用无头浏览器把 SVG 渲染成带 alpha 的 PNG。"""
    browser = find_browser()
    svg_text = svg_path.read_text(encoding="utf-8")
    # 已经带了 style 的话就不再插，避免出现两个 style 属性
    if not re.match(r"<svg[^>]*\sstyle=", svg_text):
        svg_text = re.sub(r"<svg\b", "<svg style=\"display:block;width:100%;height:100%\"",
                          svg_text, count=1)

    with tempfile.TemporaryDirectory(prefix="make-icon-") as tmp:
        tmp_dir = Path(tmp)
        page = tmp_dir / "page.html"
        page.write_text(HTML_TEMPLATE.format(svg=svg_text), encoding="utf-8")
        out_png.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            f"--user-data-dir={tmp_dir / 'profile'}",
            "--default-background-color=00000000",
            f"--window-size={size},{size}",
            f"--screenshot={out_png}",
            page.as_uri(),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            raise RuntimeError("浏览器渲染超时") from None
        if not out_png.is_file():
            detail = proc.stderr.decode("utf-8", "replace").strip()[-300:]
            raise RuntimeError(f"浏览器没有产出截图：{detail}")

    return out_png


def _bmp_entry(img: Image.Image) -> bytes:
    """把一张 RGBA 图编码成 .ico 里的 BMP(DIB) 条目。

    结构 = BITMAPINFOHEADER + XOR 位图（BGRA，自下而上）+ AND 掩码（1bpp，每行补齐 4 字节）。
    32 位色下透明度由 alpha 通道负责，所以 AND 掩码全 0。
    """
    width, height = img.size
    header = struct.pack(
        "<IiiHHIIiiII",
        40,          # biSize
        width,       # biWidth
        height * 2,  # biHeight：XOR 与 AND 两张图叠起来的高度
        1,           # biPlanes
        32,          # biBitCount
        0,           # biCompression = BI_RGB
        width * height * 4,  # biSizeImage
        0, 0, 0, 0,  # 分辨率与调色板
    )

    pixels = img.convert("RGBA").load()
    rows = []
    for y in range(height - 1, -1, -1):  # BMP 自下而上
        row = bytearray()
        for x in range(width):
            r, g, b, a = pixels[x, y]
            row += bytes((b, g, r, a))
        rows.append(bytes(row))
    xor_bitmap = b"".join(rows)

    mask_stride = ((width + 31) // 32) * 4
    and_mask = b"\x00" * (mask_stride * height)
    return header + xor_bitmap + and_mask


def build_ico(master_path: Path, ico_path: Path) -> list[int]:
    """从高分辨率 PNG 逐级缩小，写出多尺寸 .ico。

    自己编码而不是用 Pillow 的 ICO 保存：Pillow 会把所有尺寸都写成 PNG 条目，
    而 Windows 只对小尺寸认 BMP，结果就是资源管理器显示成默认通用图标。
    """
    master = Image.open(master_path).convert("RGBA")
    side = max(master.size)
    if master.size != (side, side):
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.paste(master, ((side - master.width) // 2, (side - master.height) // 2))
        master = square

    frames = [master.resize((s, s), Image.LANCZOS) for s in ICO_SIZES]
    frames.sort(key=lambda im: im.width, reverse=True)

    entries: list[tuple[int, bytes]] = []
    for frame in frames:
        if frame.width >= PNG_FROM:
            buffer = io.BytesIO()
            frame.save(buffer, format="PNG")
            entries.append((frame.width, buffer.getvalue()))
        else:
            entries.append((frame.width, _bmp_entry(frame)))

    offset = 6 + 16 * len(entries)
    out = bytearray(struct.pack("<HHH", 0, 1, len(entries)))  # reserved, type=ICO, count
    for width, data in entries:
        dim = 0 if width >= 256 else width  # 256 在这个字段里记作 0
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _width, data in entries:
        out += data

    ico_path.parent.mkdir(parents=True, exist_ok=True)
    ico_path.write_bytes(bytes(out))
    return ICO_SIZES


def verify_ico(ico_path: Path) -> list[tuple[int, str]]:
    """读回 .ico，确认每个尺寸用的是 BMP 还是 PNG（小尺寸必须是 BMP）。"""
    data = ico_path.read_bytes()
    _reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    if kind != 1:
        raise RuntimeError(f"{ico_path} 不是图标文件（类型字段 = {kind}）")
    result = []
    for i in range(count):
        offset = 6 + i * 16
        width, _h, _c, _r, _p, _bpp, _size, data_offset = struct.unpack_from("<BBBBHHII", data, offset)
        enc = "PNG" if data[data_offset:data_offset + 8] == b"\x89PNG\r\n\x1a\n" else "BMP"
        result.append((width or 256, enc))
    return sorted(result)



def build_preview(ico_path: Path, out_path: Path) -> None:
    """生成对比预览：深色底与浅色底各一排，展示从 256 到 16 的实际效果。"""
    master = Image.open(ico_path).convert("RGBA").resize((256, 256), Image.LANCZOS)

    pad, gap, label_h = 16, 16, 22
    width = pad * 2 + sum(s + gap for s in PREVIEW_SIZES)
    height = pad * 2 + 2 * (256 + label_h) + gap

    canvas = Image.new("RGB", (width, height), (28, 28, 32))
    draw = ImageDraw.Draw(canvas)

    for row, (bg, fg) in enumerate((( (28, 28, 32), (190, 190, 196)), ((246, 246, 248), (90, 90, 96)))):
        top = pad + row * (256 + label_h + gap)
        draw.rectangle([pad, top, width - pad, top + 256 + label_h], fill=bg)
        x = pad + gap // 2
        for s in PREVIEW_SIZES:
            icon = master.resize((s, s), Image.LANCZOS)
            canvas.paste(icon, (x, top + 4 + (256 - s) // 2), icon)
            draw.text((x, top + 256 + 6), f"{s}px", fill=fg)
            x += s + gap

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, format="PNG")


def main() -> int:
    ap = argparse.ArgumentParser(description="SVG 图标 → 多尺寸 ico + 预览图")
    ap.add_argument("svg", type=Path)
    ap.add_argument("--ico", type=Path, required=True)
    ap.add_argument("--preview", type=Path)
    ap.add_argument("--master", type=Path)
    ap.add_argument("--size", type=int, default=MASTER)
    args = ap.parse_args()

    if not args.svg.is_file():
        print(f"找不到输入文件：{args.svg}", file=sys.stderr)
        return 1

    master_path = args.master or args.ico.with_suffix(".master.png")
    render_svg(args.svg, master_path, args.size)
    with Image.open(master_path) as im:
        alphas = [p[3] for p in im.convert("RGBA").get_flattened_data()] \
            if hasattr(im, "get_flattened_data") else [p[3] for p in im.convert("RGBA").getdata()]
        transparent = sum(1 for a in alphas if a < 8) / len(alphas) * 100
    print(f"主渲染: {master_path} ({args.size}px, 透明像素 {transparent:.1f}%)")

    build_ico(master_path, args.ico)
    entries = verify_ico(args.ico)
    print(f"图标:   {args.ico} ({args.ico.stat().st_size / 1024:.1f} KB)")
    print("内含尺寸: " + ", ".join(f"{w}px/{enc}" for w, enc in entries))
    small_png = [w for w, enc in entries if w < PNG_FROM and enc != "BMP"]
    if small_png:
        print(f"错误：{small_png} 这些小尺寸不是 BMP，资源管理器会认不出", file=sys.stderr)
        return 1
    missing = [s for s in ICO_SIZES if s not in [w for w, _ in entries]]
    if missing:
        print(f"错误：缺少尺寸 {missing}", file=sys.stderr)
        return 1

    if args.preview:
        build_preview(args.ico, args.preview)
        print(f"预览:   {args.preview}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
