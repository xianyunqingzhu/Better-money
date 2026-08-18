"""把 图标.png 转成多平台图标：Windows 的 icon.ico、网页 favicon.ico、macOS 的 icon.icns。

用法：.venv/Scripts/python.exe tools/make_icon.py
依赖：pip install pillow（不装进 requirements，只在换图标时用到）
"""
import io
import struct
from pathlib import Path

from PIL import Image

SRC = Path(__file__).resolve().parent.parent / "图标.png"

# ICNS 块：类型 -> 像素尺寸（PNG 数据块，macOS 10.13+ 全部支持）
ICNS_BLOCKS = [
    ("ic11", 32), ("ic12", 64),
    ("ic07", 128), ("ic13", 256), ("ic08", 256),
    ("ic09", 512), ("ic14", 512), ("ic10", 1024),
]


def _png_bytes(img: Image.Image, size: int) -> bytes:
    buf = io.BytesIO()
    img.resize((size, size), Image.LANCZOS).save(buf, format="PNG")
    return buf.getvalue()


def _write_icns(path: Path, img: Image.Image) -> None:
    blocks = b""
    for typ, size in ICNS_BLOCKS:
        data = _png_bytes(img, size)
        blocks += typ.encode("ascii") + struct.pack(">I", len(data) + 8) + data
    with open(path, "wb") as f:
        f.write(b"icns" + struct.pack(">I", 8 + len(blocks)) + blocks)


def main():
    img = Image.open(SRC).convert("RGBA")
    print("source:", img.size, img.mode)
    # Windows 图标（多尺寸 .ico）
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save("icon.ico", format="ICO", sizes=sizes)
    # 网页 favicon
    img.resize((32, 32), Image.LANCZOS).save(
        "static/favicon.ico", format="ICO", sizes=[(16, 16), (32, 32)])
    # macOS 图标（.icns）
    _write_icns(Path("icon.icns"), img)
    print("written: icon.ico, icon.icns, static/favicon.ico")


if __name__ == "__main__":
    main()
