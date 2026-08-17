"""M6 测试后的清理：清空全部业务表并移除测试配置。"""
import sqlite3
from pathlib import Path

conn = sqlite3.connect("data/better_money.db")
for t in ("line_items", "transactions", "pending_items", "goals",
          "savings_wins", "adjustments", "summaries"):
    conn.execute(f"DELETE FROM {t}")
conn.commit()
print("goals:", conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0],
      "| tx:", conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
      "| wins:", conn.execute("SELECT COUNT(*) FROM savings_wins").fetchone()[0],
      "| adj:", conn.execute("SELECT COUNT(*) FROM adjustments").fetchone()[0])
conn.close()

cfg = Path("data/config.json")
if cfg.exists():
    cfg.unlink()
    print("config.json removed (back to defaults)")
