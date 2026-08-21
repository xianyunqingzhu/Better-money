"""M6 自测：目标清单、冷静期、收入自动存、达成记支出、对账、预算预警数据。"""
import json
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.paths import get_paths

BASE = os.environ.get("BETTER_MONEY_TEST_BASE_URL", "http://127.0.0.1:8642")
today = date.today().isoformat()

# 共享持久客户端：Windows 上每新建一次 httpx 客户端都要重建 SSL 上下文
# （即使访问 http），证书库枚举偶发卡顿；复用一个客户端只建一次。
_client = httpx.Client(base_url=BASE, timeout=10)


def post(path, payload, expect=200):
    r = _client.post(path, json=payload)
    assert r.status_code == expect, f"{path} -> {r.status_code}: {r.text}"
    return r.json()


def patch(path, payload):
    r = _client.patch(path, json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def get(path):
    r = _client.get(path)
    assert r.status_code == 200, r.text
    return r.json()


# 预算设为 1000
post("/api/settings", {"monthly_budget": 1000})

# 1) 无目标时收入不自动存
post("/api/transactions", {"date": today, "amount": 200, "type": "收入", "category": "兼职", "merchant": "", "note": "", "source": "手动"})

# 2) 建目标 → 默认冷静期
g1 = post("/api/goals", {"name": "降噪耳机", "price": 1299, "expected_date": "", "note": ""})
assert g1["ok"]
goals = get("/api/goals")
assert goals[0]["status"] == "冷静期"
assert goals[0]["cooldown_until"] == (date.today() + timedelta(days=7)).isoformat()
print("goal cooldown ok")

# 3) 收入 300 → 自动存 30% = 90 到第一目标
post("/api/transactions", {"date": today, "amount": 300, "type": "收入", "category": "兼职", "merchant": "", "note": "", "source": "手动"})
goals = get("/api/goals")
assert goals[0]["saved"] == 90.0, goals
print("auto save ok:", goals[0]["saved"])

# 4) 再建两个目标
g2 = post("/api/goals", {"name": "机械键盘", "price": 500})
g3 = post("/api/goals", {"name": "游戏手柄", "price": 400})

# 5) 冷静期放弃 → 记入省下的钱
post(f"/api/goals/{g2['id']}/action", {"action": "pass"})
goals = get("/api/goals")
kbd = [g for g in goals if g["id"] == g2["id"]][0]
assert kbd["status"] == "已放弃" and kbd["saved"] == 0
wins = get("/api/savings_wins")
assert wins["total"] == 500.0 and wins["count"] == 1, wins
print("cooldown pass + savings_wins ok")

# 6) 冷静期想继续 → 进行中
post(f"/api/goals/{g1['id']}/action", {"action": "want"})
goals = get("/api/goals")
assert [g for g in goals if g["id"] == g1["id"]][0]["status"] == "进行中"
print("cooldown want ok")

# 7) 手柄上移两次 → 升到第一优先级
post(f"/api/goals/{g3['id']}/action", {"action": "up"})
post(f"/api/goals/{g3['id']}/action", {"action": "up"})
goals = get("/api/goals")
assert goals[0]["name"] == "游戏手柄", [g["name"] for g in goals]
print("move up ok")

# 8) 调拨：耳机 40 → 手柄
post(f"/api/goals/{g1['id']}/transfer", {"to_id": g3["id"], "amount": 40})
goals = get("/api/goals")
hp = [g for g in goals if g["id"] == g1["id"]][0]
pad = [g for g in goals if g["id"] == g3["id"]][0]
assert hp["saved"] == 50.0 and pad["saved"] == 40.0, (hp, pad)
print("transfer ok")

# 9) 达成手柄：我买了 → 记支出 + 已达成
post(f"/api/goals/{g3['id']}/action", {"action": "achieve_buy"})
goals = get("/api/goals")
pad = [g for g in goals if g["id"] == g3["id"]][0]
assert pad["status"] == "已达成" and pad["achieved_at"]
conn = sqlite3.connect(get_paths().db_path)
tx = conn.execute(
    "SELECT * FROM transactions WHERE source='目标' AND amount=400").fetchone()
assert tx and tx[4] == "购物" and tx[3] == "支出"  # type=idx3, category=idx4
conn.close()
print("achieve_buy ok")

# 10) 暂停/恢复
post(f"/api/goals/{g1['id']}/action", {"action": "pause"})
post(f"/api/goals/{g1['id']}/action", {"action": "resume"})
goals = get("/api/goals")
assert [g for g in goals if g["id"] == g1["id"]][0]["status"] == "进行中"
print("pause/resume ok")

# 11) 编辑目标（手动调拨 saved）
patch(f"/api/goals/{g1['id']}", {"saved": 123})
goals = get("/api/goals")
assert [g for g in goals if g["id"] == g1["id"]][0]["saved"] == 123.0
print("edit ok")

# 12) 预算预警数据：再加 500 支出 → 本月支出 900 / 预算 1000 = 0.9
post("/api/transactions", {"date": today, "amount": 500, "type": "支出", "category": "购物", "merchant": "商场", "note": "", "source": "手动"})
s = get("/api/summary")
assert s["month_expense"] == 900.0 and s["budget_ratio"] == 0.9, s
print("budget ratio ok:", s["budget_ratio"])

# 13) 对账：账本 −400（500 收入 − 900 支出）→ 真实 100 → 差额 +500
r = get("/api/reconcile")
assert r["ledger_balance"] == -400.0, r
d = post("/api/reconcile", {"actual": 100, "note": "测试"})
assert d["diff"] == 500.0, d
s = get("/api/summary")
assert s["balance"] == 100.0, s
print("reconcile ok:", d["diff"])

print("M6 E2E ALL OK")
