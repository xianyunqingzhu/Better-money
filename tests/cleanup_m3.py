"""M3 测试后的清理：清空测试交易/单品/待处理/测试图片。"""
import shutil
import sqlite3
from pathlib import Path

conn = sqlite3.connect("data/better_money.db")
for t in ("line_items", "transactions", "pending_items"):
    conn.execute(f"DELETE FROM {t}")
conn.commit()
print("tx:", conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
      "| line_items:", conn.execute("SELECT COUNT(*) FROM line_items").fetchone()[0],
      "| pending:", conn.execute("SELECT COUNT(*) FROM pending_items").fetchone()[0])
conn.close()

img_dir = Path("data/images")
if img_dir.exists():
    shutil.rmtree(img_dir)
    print("test images removed")

cfg = Path("data/config.json")
if cfg.exists():
    cfg.unlink()
    print("config.json removed (back to defaults)")

test_png = Path("tests/xiaopiao.png")
if test_png.exists():
    test_png.unlink()
    print("test png removed")
