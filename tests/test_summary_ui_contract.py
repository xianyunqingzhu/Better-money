"""Summary UI contract: ranged generation modal, overwrite, and card actions."""
from pathlib import Path


def _read(name: str) -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "static" / name).read_text(encoding="utf-8")


def test_summary_modal_has_period_type_and_range_controls():
    html = _read("index.html")
    assert 'id="summary-modal"' in html
    assert 'id="summary-start"' in html
    assert 'id="summary-end"' in html
    assert 'name="summary-type"' in html
    # preset buttons for the local-date shortcuts
    for preset in ("本周", "上周", "本月", "上月", "自定义"):
        assert preset in html
    assert 'id="summary-submit"' in html
    assert 'id="summary-cancel"' in html
    # one combined generation button instead of the two legacy buttons
    assert 'id="gen-summary"' in html
    assert 'id="gen-week"' not in html
    assert 'id="gen-month"' not in html


def test_app_js_supports_ranged_generation_and_actions():
    js = _read("app.js")
    assert "function openSummaryModal" in js
    assert "function submitSummary" in js
    assert "function regenerateSummary" in js
    assert "function deleteSummary" in js
    assert "method: 'DELETE'" in js
    assert "'/api/summaries/' + id" in js or "`/api/summaries/${id}`" in js
    assert "period_start" in js
    assert "period_end" in js
    assert "overwrite" in js
    # 409 conflict handling offers an explicit overwrite confirmation
    assert "summary_exists" in js
    # local-date presets must not use toISOString for date math
    assert "toISOString" not in js.split("/* ---------- 周/月总结")[1].split("/* ---------- 目标清单")[0]
