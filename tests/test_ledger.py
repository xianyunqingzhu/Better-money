"""Ledger arithmetic: dated initial balance, monthly roll-forward, planning."""
from datetime import date, timedelta

import pytest

from app.ledger import (
    calculate_balance,
    ensure_finance_config,
    monthly_snapshot,
    planned_amount,
)


def insert_tx(conn, tx_date, amount, tx_type):
    conn.execute(
        "INSERT INTO transactions(date, amount, type, category, created_at, updated_at) "
        "VALUES (?, ?, ?, '测试', 'now', 'now')",
        (tx_date, amount, tx_type),
    )
    conn.commit()


def insert_goal(conn, name, *, price, saved, status):
    conn.execute(
        "INSERT INTO goals(name, price, saved, priority, status, created_at) "
        "VALUES (?, ?, ?, 0, ?, 'now')",
        (name, price, saved, status),
    )
    conn.commit()


def insert_adjustment(conn, adj_date, diff, note="对账"):
    conn.execute(
        "INSERT INTO adjustments(date, diff, note, created_at) VALUES (?, ?, ?, 'now')",
        (adj_date, diff, note),
    )
    conn.commit()


def test_monthly_snapshot_rolls_forward_without_manual_reset(conn):
    cfg = {"initial_balance": 1000, "initial_balance_date": "2026-07-15"}
    insert_tx(conn, "2026-07-20", 200, "收入")
    insert_tx(conn, "2026-07-25", 50, "支出")
    insert_tx(conn, "2026-08-02", 100, "支出")
    snap = monthly_snapshot(conn, cfg, "2026-08")
    assert snap.opening_balance == 1150
    assert snap.income == 0
    assert snap.expense == 100
    assert snap.closing_balance == 1050


def test_transactions_before_initial_balance_date_are_excluded(conn):
    cfg = {"initial_balance": 100, "initial_balance_date": "2026-07-10"}
    insert_tx(conn, "2026-07-01", 999, "收入")
    insert_tx(conn, "2026-07-10", 20, "支出")
    snap = monthly_snapshot(conn, cfg, "2026-07")
    assert snap.opening_balance == 100
    assert snap.closing_balance == 80


def test_refunds_transfers_and_adjustments_affect_balance(conn):
    cfg = {"initial_balance": 1000, "initial_balance_date": "2026-08-01"}
    insert_tx(conn, "2026-08-03", 200, "退款")
    insert_tx(conn, "2026-08-04", 100, "取现")
    insert_tx(conn, "2026-08-05", 60, "支出")
    insert_adjustment(conn, "2026-08-06", 25)
    snap = monthly_snapshot(conn, cfg, "2026-08")
    assert snap.refund == 200
    assert snap.transfer_out == 100
    assert snap.expense == 60
    assert snap.adjustments == 25
    assert snap.closing_balance == 1065


def test_calculate_balance_through_date_is_exclusive_after(conn):
    insert_tx(conn, "2026-08-01", 50, "支出")
    insert_tx(conn, "2026-08-10", 30, "支出")
    insert_tx(conn, "2026-08-20", 10, "支出")
    through = date(2026, 8, 10)
    balance = calculate_balance(conn, 100, date(2026, 8, 1), through)
    assert balance == 20


def test_monthly_snapshot_period_bounds(conn):
    cfg = {"initial_balance": 0, "initial_balance_date": "2026-01-01"}
    snap = monthly_snapshot(conn, cfg, "2026-02")
    assert snap.period_start == "2026-02-01"
    assert snap.period_end == "2026-02-28"


def test_planned_amount_sums_planned_goals_and_unplanned_floor(conn):
    cfg = {"initial_balance": 500, "initial_balance_date": "2026-08-01"}
    insert_goal(conn, "相机", price=100, saved=40, status="进行中")
    insert_goal(conn, "旅行", price=200, saved=999, status="冷静期")  # overfilled
    insert_goal(conn, "电脑", price=500, saved=10, status="已暂停")
    insert_goal(conn, "耳机", price=300, saved=300, status="已达成")
    assert planned_amount(conn) == 40 + 200 + 10
    snap = monthly_snapshot(conn, cfg, "2026-08")
    assert snap.planned_amount == 250
    assert snap.unplanned_balance == 250
    # closing below planned → floor at 0
    cfg_low = {"initial_balance": 100, "initial_balance_date": "2026-08-01"}
    snap_low = monthly_snapshot(conn, cfg_low, "2026-08")
    assert snap_low.closing_balance == 100
    assert snap_low.unplanned_balance == 0


def test_ensure_finance_config_infers_dates_for_legacy_data(conn, monkeypatch, tmp_path):
    insert_tx(conn, "2026-07-05", 10, "支出")
    monkeypatch.setattr("app.ledger.date", FakeDate)

    class Recorder:
        def __init__(self):
            self.calls = []

        def __call__(self, cfg):
            self.calls.append(dict(cfg))
            return cfg

    save = Recorder()
    cfg = {"initial_balance": 0}
    result = ensure_finance_config(conn, cfg, save)
    assert result["initial_balance_date"] == "2026-07-05"
    assert result["onboarding_completed"] is True
    assert result["app_version"] == "1.0.0"
    assert save.calls and save.calls[-1]["initial_balance_date"] == "2026-07-05"


def test_ensure_finance_config_empty_installation_uses_today(conn, monkeypatch, tmp_path):
    monkeypatch.setattr("app.ledger.date", FakeDate)

    class Recorder:
        def __init__(self):
            self.calls = []

        def __call__(self, cfg):
            self.calls.append(dict(cfg))
            return cfg

    save = Recorder()
    cfg = {}
    result = ensure_finance_config(conn, cfg, save)
    assert result["initial_balance_date"] == FakeDate.today().isoformat()
    assert result["onboarding_completed"] is False
    assert save.calls[-1]["onboarding_completed"] is False


class FakeDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 8, 21)


def test_initial_balance_endpoint_validates_and_backs_up(client, monkeypatch):
    calls = []
    monkeypatch.setattr("app.backup.create_backup", lambda reason: calls.append(reason))
    ok = client.post("/api/settings/initial-balance", json={
        "initial_balance": 520.5, "initial_balance_date": "2026-08-01"})
    assert ok.status_code == 200
    assert ok.json()["ok"] is True
    bad = client.post("/api/settings/initial-balance", json={
        "initial_balance": 520.5, "initial_balance_date": "not-a-date"})
    assert bad.status_code == 400
    assert bad.json()["error"] == "bad_date"
    nan = client.post(
        "/api/settings/initial-balance",
        content='{"initial_balance": NaN, "initial_balance_date": "2026-08-01"}',
        headers={"Content-Type": "application/json"},
    )
    assert nan.status_code == 400
    assert nan.json()["error"] == "bad_amount"
    # change with onboarding completed → safety backup
    from app import config as config_module
    cfg = config_module.load_config()
    cfg["onboarding_completed"] = True
    config_module.save_config(cfg)
    calls.clear()
    changed = client.post("/api/settings/initial-balance", json={
        "initial_balance": 777, "initial_balance_date": "2026-08-02"})
    assert changed.status_code == 200
    assert calls == ["pre-initial-balance-change"]


def test_generic_settings_ignores_initial_balance_fields(client):
    r = client.post("/api/settings", json={
        "initial_balance": 9999, "initial_balance_date": "2000-01-01",
        "monthly_budget": 321})
    assert r.status_code == 200
    cfg = client.get("/api/settings").json()
    assert cfg["monthly_budget"] == 321
    assert cfg["initial_balance"] != 9999
    assert cfg["initial_balance_date"] != "2000-01-01"


def test_adjustment_reversal_creates_mirror_row(client):
    created = client.post("/api/reconcile", json={
        "actual": 1234, "note": "月底盘点"})
    assert created.status_code == 200
    listed = client.get("/api/adjustments").json()
    assert len(listed) == 1
    first = listed[0]
    assert "reversed_by_id" in first and first["reversed_by_id"] is None
    rev = client.post(f"/api/adjustments/{first['id']}/reverse")
    assert rev.status_code == 200
    body = rev.json()
    assert body["ok"] is True and body["reversal_id"] != first["id"]
    listed2 = client.get("/api/adjustments").json()
    assert len(listed2) == 2
    original = next(a for a in listed2 if a["id"] == first["id"])
    mirror = next(a for a in listed2 if a["id"] == body["reversal_id"])
    assert original["reversed_by_id"] == mirror["id"]
    assert mirror["diff"] == -original["diff"]
    assert mirror["reverses_adjustment_id"] == original["id"]
    assert mirror["note"] == f"撤销：{original['note']}"
    again = client.post(f"/api/adjustments/{first['id']}/reverse")
    assert again.status_code == 409
    assert again.json()["error"] == "already_reversed"


def test_adjustment_reversal_marks_covering_summaries_expired(client):
    from contextlib import closing
    import sqlite3

    from app.paths import get_paths

    today = date.today().isoformat()
    with closing(sqlite3.connect(get_paths().db_path)) as raw:
        raw.execute(
            "INSERT INTO summaries(period_type, period_start, period_end, content, "
            "image_path, expired, created_at) VALUES ('周', ?, ?, '旧', '', 0, 'now')",
            (today, today))
        raw.commit()
    created = client.post("/api/reconcile", json={"actual": 100, "note": "校准"})
    assert created.status_code == 200
    listed = client.get("/api/adjustments").json()
    rev = client.post(f"/api/adjustments/{listed[0]['id']}/reverse")
    assert rev.status_code == 200
    with closing(sqlite3.connect(get_paths().db_path)) as raw:
        assert raw.execute("SELECT expired FROM summaries").fetchone()[0] == 1
