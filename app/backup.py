"""自动备份：每次启动把 SQLite 数据库备份到 data/backups/，保留最近 30 份。"""
import shutil
from datetime import datetime

from app.paths import get_paths

KEEP = 30


def backup_database(keep: int = KEEP) -> str | None:
    """返回备份文件路径；数据库不存在时返回 None。"""
    paths = get_paths()
    if not paths.db_path.exists():
        return None
    paths.backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = paths.backups_dir / f"better_money-{stamp}.db"
    shutil.copy2(paths.db_path, dest)
    files = sorted(paths.backups_dir.glob("better_money-*.db"))
    for f in files[:-keep]:
        f.unlink(missing_ok=True)
    return str(dest)
