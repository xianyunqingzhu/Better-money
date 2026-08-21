"""全量回归：依次运行 M2~M7 测试套件，套件间清理数据表（保留测试配置）。"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.paths import get_paths, reset_paths_cache

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
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

# 核心业务单元/契约套件（不含需要真实服务的脚本与 runner 测试本身）
PYTEST_SUITES = [
    "tests/test_paths.py",
    "tests/test_migrations.py",
    "tests/test_backup.py",
    "tests/test_legacy_migration.py",
    "tests/test_data_api.py",
    "tests/test_startup_recovery.py",
    "tests/test_run_all_guard.py",
    "tests/test_goals.py",
    "tests/test_goal_ui_contract.py",
    "tests/test_summaries_api.py",
    "tests/test_summary_ui_contract.py",
    "tests/test_ledger.py",
    "tests/test_onboarding_ui_contract.py",
    "tests/test_ai_settings.py",
    "tests/test_uploads.py",
]


def require_test_home() -> Path:
    configured = os.environ.get("BETTER_MONEY_HOME", "").strip()
    if not configured:
        raise SystemExit("BETTER_MONEY_HOME must point to a temporary test directory")
    test_home = Path(configured).expanduser().resolve()
    if test_home == REPOSITORY_ROOT:
        raise SystemExit("refusing to run tests against repository data")
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if test_home == temporary_root or not test_home.is_relative_to(temporary_root):
        raise SystemExit(
            "BETTER_MONEY_HOME must be inside the system temporary directory"
        )
    return test_home


def clean_tables(data_dir: Path) -> None:
    conn = sqlite3.connect(data_dir / "better_money.db")
    for t in CLEAN_TABLES:
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()
    shutil.rmtree(data_dir / "images", ignore_errors=True)


def main() -> int:
    test_home = require_test_home()
    reset_paths_cache()
    paths = get_paths()
    data_dir = paths.data_dir
    child_environment = os.environ.copy()
    for name, script in STEPS:
        result = subprocess.run(
            [sys.executable, str(REPOSITORY_ROOT / script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_environment,
        )
        print(f"== {name} exit={result.returncode}", flush=True)
        if result.returncode != 0:
            print(result.stdout[-1200:], flush=True)
            print(result.stderr[-1200:], flush=True)
            return 1
        print(
            result.stdout.strip().splitlines()[-1]
            if result.stdout.strip()
            else "(no output)",
            flush=True,
        )
        clean_tables(data_dir)

    # 业务单元/契约套件（pytest，隔离应用目录，禁止触碰仓库数据）
    # CI 会单独跑这些套件；设 BETTER_MONEY_SKIP_NESTED_PYTEST=1 时跳过避免重复。
    if os.environ.get("BETTER_MONEY_SKIP_NESTED_PYTEST") == "1":
        print("== pytest business suites skipped (BETTER_MONEY_SKIP_NESTED_PYTEST) ==", flush=True)
    else:
        pytest_basetemp = test_home / "pytest-basetemp"
        pytest_basetemp.mkdir(parents=True, exist_ok=True)
        pytest_command = [
            sys.executable,
            "-m",
            "pytest",
            *PYTEST_SUITES,
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(pytest_basetemp),
        ]
        print("== pytest business suites ==", flush=True)
        pytest_result = subprocess.run(
            pytest_command,
            cwd=REPOSITORY_ROOT,
            env=child_environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        print(
            pytest_result.stdout.strip().splitlines()[-1]
            if pytest_result.stdout.strip()
            else "(no output)",
            flush=True,
        )
        if pytest_result.returncode != 0:
            print(pytest_result.stdout[-3000:], flush=True)
            print(pytest_result.stderr[-1500:], flush=True)
            return 1

    shutil.rmtree(paths.backups_dir, ignore_errors=True)
    paths.config_path.unlink(missing_ok=True)
    print("REGRESSION ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
