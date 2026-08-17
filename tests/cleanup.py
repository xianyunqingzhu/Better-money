"""M2 测试后的清理脚本：验证 UTF-8、清空测试数据。"""
import sqlite3

conn = sqlite3.connect("data/better_money.db")

r = conn.execute(
    "SELECT hex(note) FROM transactions WHERE note LIKE ?", ("%4%",)
).fetchone()
print("AA note hex:", r[0] if r else None,
      "| expect:", "4人AA，原价200".encode("utf-8").hex())

conn.execute("DELETE FROM transactions")
conn.execute("DELETE FROM pending_items")
conn.commit()
print("cleaned. tx:", conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
      "pending:", conn.execute("SELECT COUNT(*) FROM pending_items").fetchone()[0])
conn.close()
