"""M7 测试后的清理：清空业务表、测试备份、测试配置。"""
import shutil
import sqlite3
from pathlib import Path

conn = sqlite3.connect("data/better_money.db")
for t in ("line_items", "transactions", "pending_items", "goals",
          "savings_wins", "adjustments", "summaries"):
    conn.execute(f"DELETE FROM {t}")
conn.commit()
conn.close()
print("all tables cleaned")

d = Path("data/backups")
if d.exists():
    shutil.rmtree(d)
    print("backups dir removed")

cfg = Path("data/config.json")
if cfg.exists():
    cfg.unlink()
    print("config.json removed")
