"""Ledger arithmetic: dated initial balance, monthly snapshots, and planning.

All monetary math is in cents-free float arithmetic rounded to 2 decimals.
The initial balance is defined immediately before the first transaction on
``initial_balance_date``; transactions dated before it are ignored.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import sqlite3

from app.config import DEFAULTS
from app.version import APP_VERSION


@dataclass(frozen=True)
class LedgerSnapshot:
    opening_balance: float
    income: float
    refund: float
    expense: float
    transfer_out: float
    adjustments: float
    closing_balance: float
    planned_amount: float
    unplanned_balance: float
    period_start: str
    period_end: str


def _month_bounds(month: str) -> tuple[date, date]:
    first = date.fromisoformat(month + "-01")
    nxt = (
        date(first.year + 1, 1, 1)
        if first.month == 12
        else date(first.year, first.month + 1, 1)
    )
    return first, nxt - timedelta(days=1)


def initial_balance_start(cfg: dict) -> date:
    """The date immediately before which the configured initial balance applies."""
    raw = str(cfg.get("initial_balance_date") or "").strip()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return date.today()


def calculate_balance(
    conn: sqlite3.Connection,
    initial_balance: float,
    start_date: date,
    through_date: date | None = None,
) -> float:
    """Balance after all transactions from start_date up to through_date."""
    cond = "date >= ?"
    args: list[str] = [start_date.isoformat()]
    if through_date is not None:
        cond += " AND date <= ?"
        args.append(through_date.isoformat())

    def one(sql: str, *a) -> float:
        return float(conn.execute(sql, a).fetchone()[0] or 0)

    income = one(f"SELECT SUM(amount) FROM transactions WHERE type='收入' AND {cond}", *args)
    refund = one(f"SELECT SUM(amount) FROM transactions WHERE type='退款' AND {cond}", *args)
    expense = one(f"SELECT SUM(amount) FROM transactions WHERE type='支出' AND {cond}", *args)
    transfer_out = one(
        f"SELECT SUM(amount) FROM transactions "
        f"WHERE type IN ('取现','转账','还款') AND {cond}", *args)
    adjustments = one(f"SELECT SUM(diff) FROM adjustments WHERE {cond}", *args)
    total = initial_balance + income + refund - expense - transfer_out + adjustments
    return round(total, 2)


def monthly_snapshot(conn: sqlite3.Connection, cfg: dict, month: str) -> LedgerSnapshot:
    """Opening/closing balances and monthly activity for one calendar month."""
    first, last = _month_bounds(month)
    initial = float(cfg.get("initial_balance") or 0)
    start = initial_balance_start(cfg)
    opening = calculate_balance(conn, initial, start, first - timedelta(days=1))
    closing = calculate_balance(conn, initial, start, last)

    def one(sql: str, *a) -> float:
        return round(float(conn.execute(sql, a).fetchone()[0] or 0), 2)

    s, e = first.isoformat(), last.isoformat()
    income = one(
        "SELECT SUM(amount) FROM transactions WHERE type='收入' AND date BETWEEN ? AND ?", s, e)
    refund = one(
        "SELECT SUM(amount) FROM transactions WHERE type='退款' AND date BETWEEN ? AND ?", s, e)
    expense = one(
        "SELECT SUM(amount) FROM transactions WHERE type='支出' AND date BETWEEN ? AND ?", s, e)
    transfer_out = one(
        "SELECT SUM(amount) FROM transactions WHERE type IN ('取现','转账','还款') "
        "AND date BETWEEN ? AND ?", s, e)
    adjustments = one(
        "SELECT SUM(diff) FROM adjustments WHERE date BETWEEN ? AND ?", s, e)
    planned = planned_amount(conn)
    unplanned = max(round(closing - planned, 2), 0.0)
    return LedgerSnapshot(
        opening_balance=opening,
        income=income,
        refund=refund,
        expense=expense,
        transfer_out=transfer_out,
        adjustments=adjustments,
        closing_balance=closing,
        planned_amount=planned,
        unplanned_balance=unplanned,
        period_start=s,
        period_end=e,
    )


def planned_amount(conn: sqlite3.Connection) -> float:
    """Total still-earmarked savings across cold-period, active, paused goals."""
    row = conn.execute(
        "SELECT SUM(MIN(saved, price)) FROM goals "
        "WHERE status IN ('冷静期','进行中','已暂停')"
    ).fetchone()
    return round(float(row[0] or 0), 2)


def _database_has_user_data(conn: sqlite3.Connection) -> bool:
    for table in ("transactions", "goals", "adjustments", "summaries"):
        if conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0:
            return True
    return False


def _config_has_user_data(raw_cfg: dict) -> bool:
    return bool(raw_cfg.get("api_key")) or any(k not in DEFAULTS for k in raw_cfg)


def ensure_finance_config(conn: sqlite3.Connection, raw_cfg: dict, save) -> dict:
    """Fill missing finance config keys once for upgraded installations.

    ``raw_cfg`` is the saved config file contents without defaults applied.
    Inferred values are persisted through ``save`` exactly once.
    """
    merged = dict(raw_cfg)
    changed = False

    if not str(merged.get("initial_balance_date") or "").strip():
        row = conn.execute(
            "SELECT MIN(date) FROM transactions WHERE date <> '' AND date IS NOT NULL"
        ).fetchone()
        merged["initial_balance_date"] = (
            row[0] if row and row[0] else date.today().isoformat()
        )
        changed = True

    if "onboarding_completed" not in merged:
        merged["onboarding_completed"] = bool(
            _database_has_user_data(conn) or _config_has_user_data(raw_cfg)
        )
        changed = True

    if "app_version" not in merged:
        merged["app_version"] = APP_VERSION
        changed = True

    if changed:
        save(merged)
    return merged
