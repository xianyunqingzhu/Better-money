"""Summary API tests: custom ranges, overwrite, expiry, and deletion."""
from contextlib import closing
from datetime import date
from pathlib import Path
import sqlite3

import pytest

from app.paths import get_paths


class FakeSummaryGenerator:
    def __init__(self, content="测试总结正文"):
        self.calls = []
        self.content = content

    def __call__(self, period_type, start, end):
        self.calls.append((period_type, start, end))
        # Mirror summarizer._upsert so tests can observe the stored record.
        with closing(_raw_conn()) as conn:
            existing = conn.execute(
                "SELECT id FROM summaries WHERE period_type = ? AND "
                "period_start = ? AND period_end = ?",
                (period_type, start.isoformat(), end.isoformat()),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE summaries SET content = ?, expired = 0 "
                    "WHERE id = ?",
                    (self.content, existing["id"]),
                )
                summary_id = existing["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO summaries(period_type, period_start, period_end, "
                    "content, image_path, expired, created_at) "
                    "VALUES (?, ?, ?, ?, '', 0, 'now')",
                    (period_type, start.isoformat(), end.isoformat(), self.content),
                )
                summary_id = cur.lastrowid
            conn.commit()
        return self.content, "", summary_id


@pytest.fixture
def fake_summary_generator(monkeypatch):
    fake = FakeSummaryGenerator()
    monkeypatch.setattr("app.summarizer.generate", fake)
    return fake


def _raw_conn():
    """Open a plain SQLite connection that bypasses the application gate.

    The gate serializes application connections, so tests must not hold a
    gated connection while the TestClient handles a request.
    """
    conn = sqlite3.connect(get_paths().db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_summary(period_type, start, end, image_path=""):
    with closing(_raw_conn()) as conn:
        cur = conn.execute(
            "INSERT INTO summaries(period_type, period_start, period_end, content, "
            "image_path, expired, created_at) VALUES (?, ?, ?, '旧内容', ?, 0, 'now')",
            (period_type, start, end, image_path),
        )
        conn.commit()
        return cur.lastrowid


def _one(sql, *args):
    with closing(_raw_conn()) as conn:
        return conn.execute(sql, args).fetchone()[0]


def test_custom_range_is_not_forced_to_calendar_week(client, fake_summary_generator):
    response = client.post("/api/summaries/generate", json={
        "period_type": "周",
        "period_start": "2026-08-03",
        "period_end": "2026-08-12",
        "overwrite": False,
    })
    assert response.status_code == 200
    assert fake_summary_generator.calls == [("周", date(2026, 8, 3), date(2026, 8, 12))]


def test_legacy_anchor_still_derives_whole_week(client, fake_summary_generator):
    response = client.post("/api/summaries/generate", json={
        "period_type": "周",
        "anchor": "2026-08-05",
    })
    assert response.status_code == 200
    assert fake_summary_generator.calls == [("周", date(2026, 8, 3), date(2026, 8, 9))]


@pytest.mark.parametrize("payload,code", [
    ({"period_type": "周", "period_start": "2026-08-12", "period_end": "2026-08-03"}, "bad_range"),
    ({"period_type": "周", "period_start": "2025-08-01", "period_end": "2026-08-02"}, "range_too_long"),
    ({"period_type": "周", "period_start": "bad", "period_end": "2026-08-03"}, "bad_date"),
    ({"period_type": "周", "period_start": "2026-08-03", "period_end": "bad"}, "bad_date"),
    ({"period_type": "季", "period_start": "2026-08-03", "period_end": "2026-08-12"}, "bad_period"),
])
def test_range_validation_errors(client, payload, code):
    response = client.post("/api/summaries/generate", json=payload)
    assert response.status_code == 400
    assert response.json()["error"] == code


def test_equal_range_conflicts_unless_overwrite(client, fake_summary_generator):
    summary_id = _seed_summary("周", "2026-08-03", "2026-08-12")
    response = client.post("/api/summaries/generate", json={
        "period_type": "周",
        "period_start": "2026-08-03",
        "period_end": "2026-08-12",
        "overwrite": False,
    })
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "summary_exists"
    assert body["summary_id"] == summary_id
    assert fake_summary_generator.calls == []


def test_overwrite_updates_the_exact_record(client, fake_summary_generator):
    summary_id = _seed_summary("周", "2026-08-03", "2026-08-12")
    fake_summary_generator.content = "新总结正文"
    response = client.post("/api/summaries/generate", json={
        "period_type": "周",
        "period_start": "2026-08-03",
        "period_end": "2026-08-12",
        "overwrite": True,
    })
    assert response.status_code == 200
    assert response.json()["overwritten"] is True
    assert response.json()["summary_id"] == summary_id
    assert _one("SELECT COUNT(*) FROM summaries WHERE period_type = '周'") == 1
    assert _one(
        "SELECT content FROM summaries WHERE id = ?", summary_id) == "新总结正文"


def test_transaction_in_range_marks_summary_expired(client, fake_summary_generator):
    today = date.today().isoformat()
    client.post("/api/summaries/generate", json={
        "period_type": "周", "period_start": today, "period_end": today,
    })
    response = client.post("/api/transactions", json={
        "date": today, "amount": 10, "type": "支出", "category": "餐饮",
        "merchant": "食堂", "note": "", "source": "手动",
    })
    assert response.status_code == 200
    assert _one("SELECT expired FROM summaries") == 1


def test_delete_removes_row_and_dedicated_image_only(client):
    paths = get_paths()
    image_dir = paths.images_dir / "summaries"
    image_dir.mkdir(parents=True, exist_ok=True)
    image = image_dir / "dedicated.png"
    image.write_bytes(b"png-bytes")
    # unrelated transaction that must survive
    with closing(_raw_conn()) as conn:
        conn.execute(
            "INSERT INTO transactions(date, amount, type, category, merchant, note, "
            "source, estimated, created_at, updated_at) "
            "VALUES ('2026-08-05', 12, '支出', '餐饮', '食堂', '', '手动', 0, 'now', 'now')")
        conn.commit()
    summary_id = _seed_summary("月", "2026-08-01", "2026-08-31", str(image))
    response = client.delete(f"/api/summaries/{summary_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["image_cleanup"] == "deleted"
    assert not image.exists()
    assert _one("SELECT COUNT(*) FROM summaries") == 0
    assert _one("SELECT COUNT(*) FROM transactions") == 1


def test_delete_missing_summary_returns_404(client):
    response = client.delete("/api/summaries/999")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_delete_never_removes_files_outside_images_dir(client, tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    summary_id = _seed_summary("周", "2026-08-03", "2026-08-09", str(outside))
    response = client.delete(f"/api/summaries/{summary_id}")
    assert response.status_code == 200
    assert response.json()["image_cleanup"] == "not_needed"
    assert outside.exists()
