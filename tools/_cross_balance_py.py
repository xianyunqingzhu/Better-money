"""导出电脑端当前数据与配置为 JSON，供 TS 交叉验证余额算法。"""
import json
import os
import sys

sys.path.insert(0, r"D:\Better-money")
import httpx  # noqa: E402

base = "http://127.0.0.1:8642"
cfg = httpx.get(f"{base}/api/settings").json()
txs = httpx.get(f"{base}/api/transactions", params={"limit": 1000}).json()
goals = httpx.get(f"{base}/api/goals").json()
summary = httpx.get(f"{base}/api/summary").json()
adjustments = httpx.get(f"{base}/api/adjustments").json()
wins = httpx.get(f"{base}/api/savings_wins").json()

with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump({
        "config": {
            "initial_balance": cfg["initial_balance"],
            "initial_balance_date": cfg["initial_balance_date"],
            "monthly_budget": cfg["monthly_budget"],
            "auto_save_ratio": cfg["auto_save_ratio"],
            "cooldown_days": cfg["cooldown_days"],
        },
        "transactions": txs,
        "goals": goals,
        "adjustments": adjustments,
        "wins": wins,
        "summary": {
            "balance": summary["balance"],
            "month_income": summary["month_income"],
            "month_expense": summary["month_expense"],
            "monthly_budget": summary["monthly_budget"],
            "today_spendable": summary["today_spendable"],
        },
    }, f, ensure_ascii=False)
print("dumped")
