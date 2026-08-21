"""Static UI contract for the multi-goal dashboard (Core Finance Task 2)."""
from pathlib import Path


def _static(name):
    root = Path(__file__).resolve().parents[1]
    return (root / "static" / name).read_text(encoding="utf-8")


def test_goal_dashboard_uses_multi_goal_list():
    html = _static("index.html")
    js = _static("app.js")
    css = _static("style.css")
    assert 'id="goal-progress-list"' in html
    assert "function renderGoalProgressList" in js
    assert "goals.filter" in js
    assert "尚未规划" in js or "暂无目标" in js
    assert "当前优先目标" in js
    assert "已存" in js and "需要" in js and "还差" in js
    assert "goals.find((g) => ['冷静期', '进行中']" not in js
    assert "chart-goal" not in html


def test_goal_delete_uses_dedicated_endpoint():
    js = _static("app.js")
    assert "fetch(`/api/goals/${id}`, { method: 'DELETE' })" in js
    assert "body: JSON.stringify({ action: 'delete' })" not in js
    assert "其中规划的" in js
    assert "loadGoals(), loadStats(), loadSummary()" in js


def test_goal_progress_list_scrolls_instead_of_truncating():
    css = _static("style.css")
    assert ".goal-progress-list" in css
    assert "overflow: auto" in css


def test_readable_typography_rules():
    css = _static("style.css")
    assert "font-size: 15px" in css  # body text
    assert ".goal-meta { color: var(--muted); font-size: 14px" in css
    assert ".goal-amount" in css
