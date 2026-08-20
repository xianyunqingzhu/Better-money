"""M7 现场自测：备份、导出、编辑、待处理队列、估算标记。

运行前必须把 ``BETTER_MONEY_HOME`` 指向一次性测试目录，并让被测服务使用
同一个目录。直接导入本模块不会访问 HTTP 服务或任何数据文件。
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import os
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app.paths import PROJECT_ROOT, get_paths


BASE = "http://127.0.0.1:8642"


def _require_isolated_application_home() -> None:
    configured = os.environ.get("BETTER_MONEY_HOME", "").strip()
    if not configured:
        raise RuntimeError(
            "M7 requires BETTER_MONEY_HOME to point to a disposable test directory"
        )
    paths = get_paths()
    if paths.root.resolve() == PROJECT_ROOT.resolve():
        raise RuntimeError("M7 refuses to use the repository application home")
    if paths.data_dir.resolve() == (PROJECT_ROOT / "data").resolve():
        raise RuntimeError("M7 refuses to use the repository data directory")


def _verified_archives_with_reason(reason: str) -> list[Path]:
    from app.backup import inspect_backup

    archives: list[Path] = []
    for candidate in get_paths().backups_dir.glob("*.zip"):
        try:
            manifest = inspect_backup(candidate)
        except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError):
            continue
        if manifest.reason == reason:
            archives.append(candidate)
    return archives


def check_backup_policy() -> None:
    """Run only the M7 backup assertions against an isolated application home."""
    _require_isolated_application_home()
    from app import backup

    paths = get_paths()
    if not paths.db_path.is_file():
        raise RuntimeError("initialize the isolated M7 database before this check")

    today = datetime.now().astimezone().date()
    before = _verified_archives_with_reason("automatic")
    first = backup.ensure_daily_backup()
    after_first = _verified_archives_with_reason("automatic")
    if first is None:
        assert any(
            datetime.fromisoformat(backup.inspect_backup(item).created_at)
            .astimezone()
            .date()
            == today
            for item in before
        )
    else:
        assert first.suffix == ".zip" and first in after_first
        assert backup.inspect_backup(first).reason == "automatic"
    assert backup.backup_database() is None, "同一天的兼容入口不得重复创建备份"
    assert set(_verified_archives_with_reason("automatic")) == set(after_first)

    manual = backup.create_backup("m7-manual")
    pre_operation = backup.create_backup("pre-m7-check")
    raw_pre_migration = paths.backups_dir / "pre-migration-m7.db"
    raw_pre_migration.write_bytes(paths.db_path.read_bytes())
    for _ in range(4):
        backup.create_backup("automatic")

    assert backup.ensure_daily_backup(keep=2) is None
    assert len(_verified_archives_with_reason("automatic")) == 2
    assert manual.exists(), "automatic retention must preserve manual ZIPs"
    assert pre_operation.exists(), "automatic retention must preserve pre-* ZIPs"
    assert raw_pre_migration.exists(), "automatic retention must preserve raw DBs"
    print("backup daily de-duplication and safe retention ok")


def post(path: str, payload: dict, expect: int = 200) -> dict:
    response = httpx.post(BASE + path, json=payload, timeout=10)
    assert response.status_code == expect, (
        f"{path} -> {response.status_code}: {response.text}"
    )
    return response.json()


def main() -> None:
    _require_isolated_application_home()
    today = time.strftime("%Y-%m-%d")

    post(
        "/api/transactions",
        {
            "date": today,
            "amount": 15,
            "type": "支出",
            "category": "餐饮",
            "merchant": "食堂",
            "note": "",
            "source": "手动",
        },
    )
    post(
        "/api/transactions",
        {
            "date": today,
            "amount": 30,
            "type": "支出",
            "category": "学习",
            "merchant": "打印店",
            "note": "",
            "source": "手动",
        },
    )

    transactions = httpx.get(BASE + "/api/transactions", timeout=10).json()
    transaction_id = transactions[0]["id"]
    response = httpx.patch(
        BASE + f"/api/transactions/{transaction_id}",
        json={
            "date": today,
            "amount": 18.5,
            "type": "支出",
            "category": "餐饮",
            "merchant": "食堂",
            "note": "改过",
        },
        timeout=10,
    )
    assert response.status_code == 200, response.text
    transactions = httpx.get(BASE + "/api/transactions", timeout=10).json()
    edited = [item for item in transactions if item["id"] == transaction_id][0]
    assert edited["amount"] == 18.5 and edited["note"] == "改过", edited
    print("edit tx ok")

    response = httpx.get(BASE + "/api/export/transactions.csv", timeout=10)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    text = response.content.decode("utf-8-sig")
    assert text.startswith("日期") and "食堂" in text, text[:80]
    print("csv export ok, lines:", len(text.splitlines()))

    response = httpx.get(BASE + "/api/export/backup.db", timeout=10)
    assert response.status_code == 200 and len(response.content) > 10000
    print("legacy backup download ok, bytes:", len(response.content))

    check_backup_policy()

    with closing(sqlite3.connect(get_paths().db_path)) as connection, connection:
        connection.execute(
            "INSERT INTO pending_items(raw_text, image_path, created_at) "
            "VALUES ('午饭没看懂多少钱', '', '2026-08-17 00:00:00')"
        )
        connection.commit()
    pending = httpx.get(BASE + "/api/pending", timeout=10).json()
    assert len(pending) == 1 and pending[0]["raw_text"] == "午饭没看懂多少钱"
    response = httpx.delete(BASE + f"/api/pending/{pending[0]['id']}", timeout=10)
    assert response.json()["ok"]
    assert httpx.get(BASE + "/api/pending", timeout=10).json() == []
    print("pending queue ok")

    post(
        "/api/transactions",
        {
            "date": today,
            "amount": 25,
            "type": "支出",
            "category": "生活",
            "merchant": "超市",
            "note": "",
            "source": "手动",
        },
    )
    httpx.patch(BASE + "/api/transactions", json={}, timeout=10)
    print("M7 E2E ALL OK")


if __name__ == "__main__":
    main()
