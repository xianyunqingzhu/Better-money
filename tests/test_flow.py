"""M2 端到端自测：通过 httpx 走真实 HTTP 接口，验证解析入账、AA、去重、提问。"""
import json
import sqlite3

import httpx

BASE = "http://127.0.0.1:8000"


def post(path, payload):
    r = httpx.post(BASE + path, json=payload, timeout=30)
    return r.status_code, r.json()


# 1) 批量解析入账（含收入、AA、估算、昨天、提问）
status, data = post("/api/parse_text", {
    "text": "昨天兼职 200\n午饭食堂 15 奶茶12\n聚餐 200 4人AA\n打印大概30",
    "date": "2026-08-17",
})
print("parse:", status, json.dumps(data, ensure_ascii=False))
assert status == 200, status
assert data["saved"] == 5, data
assert data["questions"], "应返回澄清问题"

# 2) 重复提交 → 全部跳过
status, data2 = post("/api/parse_text", {"text": "午饭食堂 15", "date": "2026-08-17"})
assert status == 200 and data2["saved"] == 0 and data2["skipped"], data2
print("dedup ok:", json.dumps(data2, ensure_ascii=False))

# 3) 数据库检查
conn = sqlite3.connect("data/better_money.db")
rows = conn.execute(
    "SELECT date, amount, type, category, merchant, note, estimated FROM transactions ORDER BY id"
).fetchall()
print("rows:", rows)
assert len(rows) == 5, rows
aa = [r for r in rows if r[5] and "4人AA" in r[5]][0]
assert aa[1] == 50.0, "AA 金额应为 50"
est = [r for r in rows if r[6] == 1][0]
assert est[1] == 30.0, "估算标记应保留"
inc = [r for r in rows if r[2] == "收入"][0]
assert inc[0] == "2026-08-16", "昨天兼职应记到 08-16"
conn.close()

# 4) 汇总联动
s = httpx.get(BASE + "/api/summary", timeout=10).json()
print("summary:", s)
assert s["month_income"] == 200.0 and s["month_expense"] == 107.0, s
print("M2 E2E ALL OK")
