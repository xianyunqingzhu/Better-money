"""全量回归：依次运行 M2~M7 测试套件，套件间清理数据表（保留测试配置）。"""
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

CLEAN_TABLES = ["line_items", "transactions", "pending_items", "goals",
                "savings_wins", "adjustments", "summaries"]

STEPS = [
    ("M2 文字解析", "tests/test_flow.py"),
    ("M3 图片/CSV", "tests/test_vision_flow.py"),
    ("M4 统计", "tests/test_stats.py"),
    ("M5 总结", "tests/test_summary.py"),
    ("M6 攒钱增强", "tests/test_m6.py"),
    ("M7 打磨", "tests/test_m7.py"),
]


def clean_tables():
    conn = sqlite3.connect("data/better_money.db")
    for t in CLEAN_TABLES:
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()
    shutil.rmtree("data/images", ignore_errors=True)


for name, script in STEPS:
    r = subprocess.run([sys.executable, script], capture_output=True, text=True)
    print(f"== {name} exit={r.returncode}")
    if r.returncode != 0:
        print(r.stdout[-1200:])
        print(r.stderr[-1200:])
        sys.exit(1)
    print(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "(no output)")
    clean_tables()

shutil.rmtree("data/backups", ignore_errors=True)
Path("data/config.json").unlink(missing_ok=True)
print("REGRESSION ALL PASS")
