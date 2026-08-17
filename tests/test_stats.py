"""M4 自测：统计接口（分类占比、近30天趋势、近8周、月份列表）。"""
import json

import httpx

BASE = "http://127.0.0.1:8642"

seed = [
    ("2026-07-20", 40, "支出", "学习", "书店"),
    ("2026-07-25", 20, "支出", "餐饮", "食堂"),
    ("2026-08-01", 100, "支出", "餐饮", "食堂"),
    ("2026-08-02", 30, "支出", "奶茶咖啡", "奶茶店"),
    ("2026-08-03", 10, "退款", "奶茶咖啡", "奶茶店"),
    ("2026-08-10", 300, "收入", "兼职", ""),
]

for d, amt, t, cat, m in seed:
    r = httpx.post(BASE + "/api/transactions", json={
        "date": d, "amount": amt, "type": t, "category": cat, "merchant": m, "note": "", "source": "手动",
    }, timeout=10)
    assert r.json().get("ok"), r.text
print("seeded", len(seed))

# 当前月统计
s = httpx.get(BASE + "/api/stats", timeout=10).json()
print("stats current:", json.dumps(s, ensure_ascii=False)[:500])
assert s["month_expense"] == 120.0, s  # 100+30-10
assert s["month_income"] == 300.0
cat_map = {c["name"]: c["value"] for c in s["category"]}
assert cat_map == {"餐饮": 100.0, "奶茶咖啡": 20.0}, cat_map
assert len(s["daily"]) == 30
assert s["daily"][-1]["date"] == "2026-08-17", "趋势应结束于今天"
assert any(d["date"] == "2026-08-01" and d["value"] == 100 for d in s["daily"])
assert len(s["weekly"]) == 8
week_vals = [w["value"] for w in s["weekly"]]
assert 130.0 in week_vals, f"07-27~08-02 周应合计 130（100+30），实际 {week_vals}"
assert -10.0 in week_vals, f"08-03 起的周应为 -10（退款冲减），实际 {week_vals}"
assert s["weekly"][-1]["label"] == "本周"

# 历史月统计
s2 = httpx.get(BASE + "/api/stats?month=2026-07", timeout=10).json()
print("stats 2026-07:", json.dumps(s2, ensure_ascii=False)[:400])
assert s2["month_expense"] == 60.0
assert {c["name"]: c["value"] for c in s2["category"]} == {"学习": 40.0, "餐饮": 20.0}
assert s2["daily"][-1]["date"] == "2026-07-31", "历史月趋势应结束于月末"

# 月份列表
ms = httpx.get(BASE + "/api/months", timeout=10).json()
print("months:", ms)
assert ms == ["2026-08", "2026-07"], ms

# 目标接口（空列表，进度环空态）
goals = httpx.get(BASE + "/api/goals", timeout=10).json()
assert goals == []
print("M4 STATS ALL OK")
