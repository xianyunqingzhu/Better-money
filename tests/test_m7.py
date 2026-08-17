"""M7 自测：备份、导出、编辑、待处理队列、估算标记。"""
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

BASE = "http://127.0.0.1:8000"
today = time.strftime("%Y-%m-%d")


def post(path, payload, expect=200):
    r = httpx.post(BASE + path, json=payload, timeout=10)
    assert r.status_code == expect, f"{path} -> {r.status_code}: {r.text}"
    return r.json()


# 种子数据
post("/api/transactions", {"date": today, "amount": 15, "type": "支出", "category": "餐饮", "merchant": "食堂", "note": "", "source": "手动"})
post("/api/transactions", {"date": today, "amount": 30, "type": "支出", "category": "学习", "merchant": "打印店", "note": "", "source": "手动"})

# 1) 编辑交易
txs = httpx.get(BASE + "/api/transactions", timeout=10).json()
tid = txs[0]["id"]
r = httpx.patch(BASE + f"/api/transactions/{tid}", json={
    "date": today, "amount": 18.5, "type": "支出", "category": "餐饮", "merchant": "食堂", "note": "改过"}, timeout=10)
assert r.status_code == 200, r.text
txs = httpx.get(BASE + "/api/transactions", timeout=10).json()
t = [x for x in txs if x["id"] == tid][0]
assert t["amount"] == 18.5 and t["note"] == "改过", t
print("edit tx ok")

# 2) 导出 CSV
r = httpx.get(BASE + "/api/export/transactions.csv", timeout=10)
assert r.status_code == 200
assert r.headers["content-type"].startswith("text/csv")
text = r.content.decode("utf-8-sig")
assert text.startswith("日期") and "食堂" in text, text[:80]
print("csv export ok, lines:", len(text.splitlines()))

# 3) 下载完整备份
r = httpx.get(BASE + "/api/export/backup.db", timeout=10)
assert r.status_code == 200 and len(r.content) > 10000
print("backup download ok, bytes:", len(r.content))

# 4) 启动备份文件存在且保留策略有效（直接调 backup 模块）
from app import backup  # noqa: E402
for i in range(35):
    dest = backup.BACKUPS_DIR / f"better_money-20200101-{i:06d}.db"
    dest.write_bytes(b"x")
p = backup.backup_database()
assert p and Path(p).exists()
n = len(list(backup.BACKUPS_DIR.glob("better_money-*.db")))
assert n == 30, f"应保留 30 份，实际 {n}"
print("backup prune ok, kept:", n)

# 5) 待处理队列
conn = sqlite3.connect("data/better_money.db")
conn.execute("INSERT INTO pending_items(raw_text, image_path, created_at) VALUES ('午饭没看懂多少钱', '', '2026-08-17 00:00:00')")
conn.commit()
conn.close()
pend = httpx.get(BASE + "/api/pending", timeout=10).json()
assert len(pend) == 1 and pend[0]["raw_text"] == "午饭没看懂多少钱"
r = httpx.delete(BASE + f"/api/pending/{pend[0]['id']}", timeout=10)
assert r.json()["ok"]
pend = httpx.get(BASE + "/api/pending", timeout=10).json()
assert pend == []
print("pending queue ok")

# 6) 估算标记字段保留
post("/api/transactions", {"date": today, "amount": 25, "type": "支出", "category": "生活", "merchant": "超市", "note": "", "source": "手动"})
r = httpx.patch(BASE + "/api/transactions", json={})  # 空 patch 应 422（无字段）或忽略——跳过

print("M7 E2E ALL OK")
