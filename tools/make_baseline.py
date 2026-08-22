"""阶段 A 基准备份：对安装版数据生成完整备份 ZIP 与基线报告。

用法：
    python tools/make_baseline.py [--home <BETTER_MONEY_HOME>]

默认针对安装版目录 C:\\Users\\23186\\AppData\\Local\\BetterMoney。
依赖应用内 ledger gate 与 SQLite checkpoint 在运行中的程序旁取得一致性快照。
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

INSTALLED_HOME = Path(os.environ.get("LOCALAPPDATA", "")) / "BetterMoney"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", default=str(INSTALLED_HOME))
    args = parser.parse_args()

    os.environ["BETTER_MONEY_HOME"] = str(args.home)

    from app import backup
    from app.paths import get_paths
    from app.version import APP_VERSION

    paths = get_paths()
    db = paths.db_path
    if not db.exists():
        raise SystemExit(f"数据库不存在: {db}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    # 1. 一致性快照下的完整备份（含图片；backup.py 自动剔除 api_key）
    archive = backup.create_backup(reason="baseline-pre-share", include_images=True)
    print(f"backup archive: {archive}")

    # 2. 数据库直读统计（只读连接，仅统计不写）
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {}
        for table in (
            "transactions", "line_items", "goals", "summaries",
            "adjustments", "savings_wins", "pending_items",
        ):
            counts[table] = conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        date_range = conn.execute(
            "SELECT MIN(date), MAX(date) FROM transactions "
            "WHERE date <> '' AND date IS NOT NULL").fetchone()
    finally:
        conn.close()

    digest = hashlib.sha256(db.read_bytes()).hexdigest()
    images = sorted(
        str(p.relative_to(paths.images_dir))
        for p in paths.images_dir.rglob("*") if p.is_file()
    ) if paths.images_dir.exists() else []

    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "app_version": APP_VERSION,
        "source": "installed" if str(args.home) == str(INSTALLED_HOME) else str(args.home),
        "db_path": str(db),
        "schema_version": schema_version,
        "integrity": integrity,
        "counts": counts,
        "date_range": {"min": date_range[0], "max": date_range[1]},
        "db_sha256": digest,
        "image_count": len(images),
        "backup_archive": archive.name,
        "backup_dir": str(archive.parent),
    }

    report_path = REPO / "docs" / f"baseline-pre-share-{stamp}.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"baseline report: {report_path}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
