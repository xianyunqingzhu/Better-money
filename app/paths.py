from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class AppPaths:
    root: Path
    data_dir: Path
    config_path: Path
    db_path: Path
    images_dir: Path
    backups_dir: Path
    logs_dir: Path
    runtime_dir: Path

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir, self.images_dir, self.backups_dir,
            self.logs_dir, self.runtime_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_paths() -> AppPaths:
    configured = os.environ.get("BETTER_MONEY_HOME", "").strip()
    if configured:
        root = Path(configured).expanduser().resolve()
        installed_layout = True
    elif getattr(sys, "frozen", False):
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if not local:
            raise RuntimeError("Windows LOCALAPPDATA is unavailable")
        root = (Path(local) / "BetterMoney").resolve()
        installed_layout = True
    else:
        root = PROJECT_ROOT
        installed_layout = False
    data = root / "data"
    support = root if installed_layout else data
    return AppPaths(
        root=root,
        data_dir=data,
        config_path=data / "config.json",
        db_path=data / "better_money.db",
        images_dir=data / "images",
        backups_dir=support / "backups",
        logs_dir=support / "logs",
        runtime_dir=support / "runtime",
    )


def resource_root() -> Path:
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled) if bundled else PROJECT_ROOT


def reset_paths_cache() -> None:
    get_paths.cache_clear()
