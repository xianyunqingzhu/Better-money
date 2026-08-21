"""Summary range validation, duplicate lookup, and safe deletion."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import logging
from pathlib import Path
import sqlite3

from app import db
from app.paths import get_paths

logger = logging.getLogger(__name__)

MAX_RANGE_DAYS = 366
VALID_PERIOD_TYPES = ("周", "月")


class SummaryRangeError(ValueError):
    """User-facing validation error with a stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SummaryNotFoundError(LookupError):
    """Raised when a summary id does not exist."""


@dataclass(frozen=True)
class SummaryRange:
    period_type: str
    start: date
    end: date

    @classmethod
    def parse(cls, start: str, end: str, period_type: str) -> "SummaryRange":
        if period_type not in VALID_PERIOD_TYPES:
            raise SummaryRangeError(
                "bad_period", "period_type 只能是 周 或 月")
        try:
            start_date = date.fromisoformat(start)
        except (TypeError, ValueError) as exc:
            raise SummaryRangeError(
                "bad_date", "开始日期格式不正确，应为 YYYY-MM-DD") from exc
        try:
            end_date = date.fromisoformat(end)
        except (TypeError, ValueError) as exc:
            raise SummaryRangeError(
                "bad_date", "结束日期格式不正确，应为 YYYY-MM-DD") from exc
        if start_date > end_date:
            raise SummaryRangeError(
                "bad_range", "开始日期不能晚于结束日期")
        if (end_date - start_date).days + 1 > MAX_RANGE_DAYS:
            raise SummaryRangeError(
                "range_too_long", f"区间最长 {MAX_RANGE_DAYS} 天")
        return cls(period_type, start_date, end_date)


def find_existing(
    conn: sqlite3.Connection, period_type: str, start: date, end: date
) -> int | None:
    row = conn.execute(
        "SELECT id FROM summaries WHERE period_type = ? AND period_start = ? "
        "AND period_end = ?",
        (period_type, start.isoformat(), end.isoformat()),
    ).fetchone()
    return row["id"] if row else None


@dataclass(frozen=True)
class SummaryDeleteResult:
    summary_id: int
    image_cleanup: str  # "not_needed", "deleted", or "failed"
    message: str


def delete_summary(summary_id: int) -> SummaryDeleteResult:
    """Delete one summary row and, when safe, its dedicated image file."""
    conn = db.get_conn()
    row = conn.execute(
        "SELECT image_path FROM summaries WHERE id = ?", (summary_id,)
    ).fetchone()
    if not row:
        conn.close()
        raise SummaryNotFoundError(summary_id)
    conn.execute("DELETE FROM summaries WHERE id = ?", (summary_id,))
    conn.commit()
    conn.close()

    image_path = (row["image_path"] or "").strip()
    if not image_path:
        return SummaryDeleteResult(summary_id, "not_needed", "总结已删除")

    paths = get_paths()
    resolved = Path(image_path).resolve()
    try:
        inside_images = resolved.is_relative_to(paths.images_dir.resolve())
    except (OSError, ValueError):
        inside_images = False
    check_conn = db.get_conn()
    try:
        still_referenced = check_conn.execute(
            "SELECT COUNT(*) FROM summaries WHERE image_path = ?", (image_path,)
        ).fetchone()[0] > 0
    finally:
        check_conn.close()
    if not inside_images or still_referenced:
        return SummaryDeleteResult(
            summary_id, "not_needed", "总结已删除，配图未改动")
    try:
        resolved.unlink(missing_ok=True)
    except OSError:
        logger.error("Summary image cleanup failed")
        return SummaryDeleteResult(
            summary_id, "failed", "总结已删除，但配图文件未能清理")
    return SummaryDeleteResult(summary_id, "deleted", "总结已删除")
