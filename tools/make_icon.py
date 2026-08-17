"""把 图标.png 转成 Windows 多尺寸 icon.ico 和网页 favicon.ico。

用法：.venv\Scripts\python.exe tools\make_icon.py
依赖：pip install pillow（不装进 requirements，只在换图标时用到）
"""
from pathlib import Path

from PIL import Image

SRC = Path(__file__).resolve().parent.parent / "图标.png"


def main():
    img = Image.open(SRC).convert("RGBA")
    print("source:", img.size, img.mode)
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save("icon.ico", format="ICO", sizes=sizes)
    fav = img.resize((32, 32), Image.LANCZOS)
    fav.save("static/favicon.ico", format="ICO", sizes=[(16, 16), (32, 32)])
    print("written: icon.ico, static/favicon.ico")


if __name__ == "__main__":
    main()
