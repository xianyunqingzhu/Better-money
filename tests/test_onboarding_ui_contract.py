"""Onboarding and settings UI contract."""
from pathlib import Path


def _read(name: str) -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "static" / name).read_text(encoding="utf-8")


def test_onboarding_modal_has_four_steps_and_choices():
    html = _read("index.html")
    assert 'id="onboarding-modal"' in html
    assert "全新开始" in html
    assert "迁移旧数据" in html
    assert "从备份恢复" in html
    for step in ("onboard-step-1", "onboard-step-2", "onboard-step-3", "onboard-step-4"):
        assert f'id="{step}"' in html
    assert 'id="onboard-initial-date"' in html
    assert 'id="onboard-initial-balance"' in html
    assert 'id="onboard-budget"' in html
    assert 'id="onboard-ratio"' in html
    assert 'id="onboard-ai-provider"' in html
    assert 'id="onboard-ai-key"' in html
    assert 'id="onboard-skip-ai"' in html
    assert 'id="onboard-done"' in html


def test_settings_has_ai_presets_test_and_backup_controls():
    html = _read("index.html")
    assert 'id="s-ai-provider"' in html
    assert "OpenAI" in html and "DeepSeek" in html and "Qwen" in html
    assert 'id="s-test-ai"' in html
    assert 'id="adjustment-list"' in html
    for label in ("立即备份", "恢复备份", "导出完整备份 ZIP", "打开数据文件夹"):
        assert label in html
    assert 'id="s-latest-backup"' in html
    assert 'id="correct-initial"' in html


def test_app_js_onboarding_and_protected_balance_flows():
    js = _read("app.js")
    assert "function loadOnboardingState" in js
    assert "function submitOnboarding" in js
    assert "function correctInitialBalance" in js
    assert "function loadAdjustments" in js
    assert "'/api/settings/initial-balance'" in js
    assert "'/api/settings/test-ai'" in js
    assert "'/api/backups/restore'" in js
    assert "'/api/system/open-data-folder'" in js
    # generic settings must not post initial_balance
    settings_section = js.split("async function saveSettings")[1].split("\n}\n")[0]
    assert "initial_balance" not in settings_section
