"""M2 端到端自测：通过 httpx 走真实 HTTP 接口，验证解析入账、AA、去重、提问。"""
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.paths import get_paths

BASE = os.environ.get("BETTER_MONEY_TEST_BASE_URL", "http://127.0.0.1:8642")

# 共享持久客户端：Windows 上每新建一次 httpx 客户端都要重建 SSL 上下文
# （即使访问 http），证书库枚举偶发卡顿；复用一个客户端只建一次。
_client = httpx.Client(base_url=BASE, timeout=30)


def post(path, payload):
    r = _client.post(path, json=payload)
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
conn = sqlite3.connect(get_paths().db_path)
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
s = _client.get("/api/summary").json()
print("summary:", s)
assert s["month_income"] == 200.0 and s["month_expense"] == 107.0, s
print("M2 E2E ALL OK")
