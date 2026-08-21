from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import runpy


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPOSITORY_ROOT / "tests" / "run_all.py"
TABLES = (
    "line_items",
    "transactions",
    "pending_items",
    "goals",
    "savings_wins",
    "adjustments",
    "summaries",
)


def _run_runner(cwd: Path, configured_home: str | None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    if configured_home is None:
        environment.pop("BETTER_MONEY_HOME", None)
    else:
        environment["BETTER_MONEY_HOME"] = configured_home
    return subprocess.run(
        [sys.executable, str(RUNNER)],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _create_runner_database(home: Path) -> None:
    data_dir = home / "data"
    data_dir.mkdir(parents=True)
    with sqlite3.connect(data_dir / "better_money.db") as connection:
        for table in TABLES:
            connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")


def test_runner_rejects_missing_home_before_running_legacy_tests(tmp_path: Path) -> None:
    result = _run_runner(tmp_path, None)

    assert result.returncode != 0
    assert "BETTER_MONEY_HOME must point to a temporary test directory" in result.stderr
    assert "M2 文字解析" not in result.stdout


def test_runner_rejects_repository_root_before_running_legacy_tests(
    tmp_path: Path,
) -> None:
    result = _run_runner(tmp_path, str(REPOSITORY_ROOT / "."))

    assert result.returncode != 0
    assert "refusing to run tests against repository data" in result.stderr
    assert "M2 文字解析" not in result.stdout


def test_runner_rejects_non_temp_parent_repository_before_legacy_tests(
    tmp_path: Path,
) -> None:
    result = _run_runner(tmp_path, str(REPOSITORY_ROOT.parents[1]))

    assert result.returncode != 0
    assert "BETTER_MONEY_HOME must be inside the system temporary directory" in result.stderr
    assert "M2 文字解析" not in result.stdout


def test_runner_uses_temporary_home_for_cleanup(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    _create_runner_database(home)
    namespace = runpy.run_path(str(RUNNER))

    namespace["clean_tables"](home / "data")

    assert (home / "data" / "better_money.db").is_file()
    assert not (workspace / "data").exists()
