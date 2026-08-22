"""跨实现互通验证：Python 端（电脑）种子/导入。用法见 cross_compat_check.ps1。"""
import json
import os
import sys
from pathlib import Path

mode = sys.argv[1]  # seed | import
home = sys.argv[2]
zip_path = sys.argv[3] if len(sys.argv) > 3 else None

os.environ["BETTER_MONEY_HOME"] = home
sys.path.insert(0, r"D:\Better-money")

from app.paths import reset_paths_cache  # noqa: E402

reset_paths_cache()
from app import db, sync  # noqa: E402
from app.config import load_config  # noqa: E402

db.init_db()
conn = db.get_conn()
cfg = load_config()
sync.ensure_device_identity(conn, cfg)
conn.commit()
conn.close()

if mode == "seed":
    conn = db.get_conn()
    now = db.now_str()
    device_id = sync.local_device_id(conn)
    conn.execute(
        "INSERT INTO transactions(date, amount, type, category, merchant, note, source, "
        "estimated, created_at, updated_at, uuid, device_id) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
        ("2026-08-21", 29.9, "支出", "餐饮", "KFC", "跨端测试", "手动", now, now,
         "a" * 32, device_id))
    conn.execute(
        "INSERT INTO transactions(date, amount, type, category, merchant, note, source, "
        "estimated, created_at, updated_at, uuid, device_id) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
        ("2026-08-22", 200, "收入", "兼职", "", "跨端收入", "手动", now, now,
         "b" * 32, device_id))
    conn.execute(
        "INSERT INTO goals(name, price, saved, priority, status, cooldown_until, "
        "expected_date, note, created_at, achieved_at, uuid, device_id, updated_at) "
        "VALUES ('跨端目标', 999, 100, 0, '进行中', '', '', '跨端测试', ?, '', ?, ?, ?)",
        (now, "c" * 32, device_id, now))
    conn.commit()
    conn.close()
    info = sync.export_share_package()
    Path(zip_path).write_bytes(Path(info["path"]).read_bytes())
    print(json.dumps({"exported": info["counts"]}))
elif mode == "import":
    data = Path(zip_path).read_bytes()
    token = sync.stage_import_package(data)
    preview = sync.preview_import(token)
    result = sync.apply_import(token, {})
    conn = db.get_conn()
    tx = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE deleted_at = ''").fetchone()[0]
    goals = conn.execute(
        "SELECT COUNT(*) FROM goals WHERE deleted_at = ''").fetchone()[0]
    pending = sync.sync_status(conn)["pending_changes"]
    conn.close()
    print(json.dumps({
        "preview_add_tx": preview["summary"]["add_transactions"],
        "applied_add_tx": result["add_transactions"],
        "applied_add_goals": result["add_goals"],
        "live_tx": tx,
        "live_goals": goals,
        "pending": pending,
    }))
