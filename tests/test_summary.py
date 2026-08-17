"""M5 自测：周/月总结生成、存储、过期标记、配图、AI 不可用路径。"""
import json
import sqlite3
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8642"

# 种子数据：本周（08-17 周一）3 笔 + 上周 2 笔
seed = [
    ("2026-08-17", 15, "支出", "餐饮", "食堂"),
    ("2026-08-17", 12, "支出", "奶茶咖啡", "奶茶店"),
    ("2026-08-17", 200, "收入", "兼职", ""),
    ("2026-08-14", 20, "支出", "餐饮", "食堂"),
    ("2026-08-13", 40, "支出", "学习", "书店"),
]
for d, amt, t, cat, m in seed:
    r = httpx.post(BASE + "/api/transactions", json={
        "date": d, "amount": amt, "type": t, "category": cat, "merchant": m, "note": "", "source": "手动",
    }, timeout=10)
    assert r.json().get("ok"), r.text
print("seeded", len(seed))

# 1) 生成周总结（配图开启）
r = httpx.post(BASE + "/api/summaries/generate", json={"period_type": "周"}, timeout=60)
print("gen week:", r.status_code, json.dumps(r.json(), ensure_ascii=False)[:300])
assert r.status_code == 200, r.text
d = r.json()
assert d["content"] and "奶茶" in d["content"]
assert d["image_path"] and Path(d["image_path"]).exists(), "配图应生成并落盘"

conn = sqlite3.connect("data/better_money.db")
n = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
row = conn.execute("SELECT * FROM summaries").fetchone()
print("summaries:", n, "| type:", row[1], "| start:", row[2], "| expired:", row[6])
assert n == 1 and row[1] == "周" and row[2] == "2026-08-17" and row[6] == 0
conn.close()

# 2) 重新生成 → 覆盖而不是新增
r = httpx.post(BASE + "/api/summaries/generate", json={"period_type": "周"}, timeout=60)
assert r.status_code == 200
conn = sqlite3.connect("data/better_money.db")
assert conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 1, "应覆盖同周期"
conn.close()
print("regenerate overwrite ok")

# 3) 账目变化 → 总结过期
r = httpx.post(BASE + "/api/transactions", json={
    "date": "2026-08-17", "amount": 5, "type": "支出", "category": "餐饮",
    "merchant": "小卖部", "note": "", "source": "手动"}, timeout=10)
assert r.json().get("ok")
conn = sqlite3.connect("data/better_money.db")
expired = conn.execute("SELECT expired FROM summaries WHERE period_type='周'").fetchone()[0]
assert expired == 1, "本周总结应标记过期"
conn.close()
print("expire marking ok")

# 4) 月总结
r = httpx.post(BASE + "/api/summaries/generate", json={"period_type": "月"}, timeout=60)
assert r.status_code == 200
conn = sqlite3.connect("data/better_money.db")
n = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
assert n == 2
conn.close()
print("month summary ok")

# 5) 列表接口
lst = httpx.get(BASE + "/api/summaries", timeout=10).json()
assert len(lst) == 2 and {s["period_type"] for s in lst} == {"周", "月"}
print("list ok")

# 6) 配图接口可访问
img_id = [s for s in lst if s["image_path"]][0]["id"]
img = httpx.get(BASE + f"/api/summary_image/{img_id}", timeout=10)
assert img.status_code == 200 and img.headers["content-type"] == "image/png"
print("image serve ok")

# 7) AI 不可用 → 503
cfg = Path("data/config.json")
backup = cfg.read_text(encoding="utf-8")
cfg_data = json.loads(backup)
cfg_data["api_key"] = ""
cfg.write_text(json.dumps(cfg_data, ensure_ascii=False), encoding="utf-8")
r = httpx.post(BASE + "/api/summaries/generate", json={"period_type": "周"}, timeout=30)
print("unavailable:", r.status_code, r.json().get("error"))
assert r.status_code == 503 and r.json()["error"] == "ai_unavailable"
cfg.write_text(backup, encoding="utf-8")

print("M5 E2E ALL OK")
