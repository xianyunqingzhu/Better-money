# Better Money Core Finance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix and extend goals, summaries, balances, onboarding, and readability so the confirmed workflows are correct and understandable before packaging.

**Architecture:** Move goal allocation and ledger arithmetic into focused service modules with direct unit tests, while keeping FastAPI endpoints as thin validation/serialization layers. Extend the existing browser UI rather than introducing a desktop framework; use explicit modals and view refreshes for destructive and long-running actions.

**Tech Stack:** Python 3.12+, FastAPI, SQLite, Pydantic, pytest, vanilla HTML/CSS/JavaScript, and ECharts.

**Spec:** `docs/superpowers/specs/2026-08-20-windows-productization-design.md`

## Global Constraints

- Execute after `2026-08-20-data-foundation-implementation.md` passes Gate A.
- All tests use temporary `BETTER_MONEY_HOME`; repository `data` is never test input.
- Manual bookkeeping, goals, charts, balances, backup, and restore work without an API Key or internet connection.
- Goal planned amounts never create a second balance deduction.
- Summary deletion never deletes transactions, goals, or adjustments.
- Initial balance is defined immediately before the first transaction on `initial_balance_date`.
- Body text is 15px, supporting text is at least 13px, and goal amounts are at least 14px.

---

### Task 1: Priority goal allocation and correct deletion contract

**Files:**
- Create: `app/goals.py`
- Create: `tests/test_goals.py`
- Modify: `app/main.py:43-88, 532-675`

**Interfaces:**
- Consumes: `db.get_conn()` and `db.now_str()`.
- Produces: `allocate_savings(conn: sqlite3.Connection, amount: float) -> list[GoalAllocation]`.
- Produces: unchanged `DELETE /api/goals/{gid}` route with 404 for a missing goal.

- [ ] **Step 1: Write failing allocation tests**

Create `tests/test_goals.py` with seeded goals in priority order and assert allocation fills without exceeding price:

```python
def insert_goal(conn, name, *, price, saved, priority, status):
    cursor = conn.execute(
        "INSERT INTO goals(name, price, saved, priority, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'now')",
        (name, price, saved, priority, status),
    )
    conn.commit()
    return cursor.lastrowid


def goal_saved(conn, goal_id):
    return conn.execute("SELECT saved FROM goals WHERE id = ?", (goal_id,)).fetchone()[0]


def test_allocate_savings_fills_goals_in_priority_order(conn):
    first = insert_goal(conn, "相机", price=100, saved=90, priority=0, status="进行中")
    second = insert_goal(conn, "旅行", price=200, saved=20, priority=1, status="冷静期")
    paused = insert_goal(conn, "电脑", price=500, saved=10, priority=2, status="已暂停")
    allocations = allocate_savings(conn, 50)
    assert [(a.goal_id, a.amount) for a in allocations] == [(first, 10), (second, 40)]
    assert goal_saved(conn, first) == 100
    assert goal_saved(conn, second) == 60
    assert goal_saved(conn, paused) == 10
```

Add tests for all goals full, no goals, zero amount, and a goal whose existing saved value already exceeds price.

- [ ] **Step 2: Run the allocation tests and verify the module import failure**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_goals.py -v
```

Expected: collection fails because `app.goals` does not exist.

- [ ] **Step 3: Implement the goal service**

Define:

```python
@dataclass(frozen=True)
class GoalAllocation:
    goal_id: int
    goal_name: str
    amount: float


def allocate_savings(conn, amount: float) -> list[GoalAllocation]:
    remaining = round(max(float(amount), 0.0), 2)
    allocations: list[GoalAllocation] = []
    rows = conn.execute(
        "SELECT id, name, price, saved FROM goals "
        "WHERE status IN ('冷静期','进行中') AND saved < price "
        "ORDER BY priority, id"
    ).fetchall()
    for row in rows:
        if remaining <= 0:
            break
        capacity = round(max(float(row["price"]) - float(row["saved"]), 0.0), 2)
        assigned = round(min(remaining, capacity), 2)
        if assigned <= 0:
            continue
        conn.execute("UPDATE goals SET saved = saved + ? WHERE id = ?", (assigned, row["id"]))
        allocations.append(GoalAllocation(row["id"], row["name"], assigned))
        remaining = round(remaining - assigned, 2)
    return allocations
```

Change `_apply_auto_save` to calculate the configured percentage and call `allocate_savings`. Return the allocations in the create-transaction response as `savings_allocations` so the UI can explain where money was planned.

- [ ] **Step 4: Write failing goal deletion API tests**

Use TestClient to create a goal, delete it with HTTP DELETE, and assert a second delete returns 404 with `error: "not_found"`. Seed an unrelated transaction and assert it remains after goal deletion.

- [ ] **Step 5: Make deletion validate existence and preserve ledger records**

Before deleting, query the goal. Return:

```json
{"error":"not_found","message":"目标不存在"}
```

for missing IDs. Return the deleted goal name and saved amount on success so the UI can produce an accurate confirmation result.

- [ ] **Step 6: Run goal service and API tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_goals.py -v
```

Expected: all allocation and deletion tests pass.

- [ ] **Step 7: Commit goal backend behavior**

```powershell
git add app\goals.py app\main.py tests\test_goals.py
git commit -m "fix: allocate savings across goals and delete correctly"
```

### Task 2: Multi-goal dashboard and readable goal cards

**Files:**
- Create: `tests/test_goal_ui_contract.py`
- Modify: `static/app.js:323-422, 500-632`
- Modify: `static/index.html:85-95, 102-114`
- Modify: `static/style.css:1-28, 229-260, 305-365`

**Interfaces:**
- Consumes: `/api/goals` ordered list and `savings_allocations` from Task 1.
- Produces: `renderGoalProgressList(goals: object[]) -> string` in `static/app.js` and dashboard container `#goal-progress-list`.

- [ ] **Step 1: Write a failing static UI contract test**

Create `tests/test_goal_ui_contract.py`:

```python
from pathlib import Path


def test_goal_dashboard_uses_multi_goal_list():
    root = Path(__file__).resolve().parents[1]
    html = (root / "static/index.html").read_text(encoding="utf-8")
    js = (root / "static/app.js").read_text(encoding="utf-8")
    assert 'id="goal-progress-list"' in html
    assert "function renderGoalProgressList" in js
    assert "goals.filter" in js
    assert "尚未规划" in js
    assert "goals.find((g) => ['冷静期', '进行中']" not in js
```

- [ ] **Step 2: Run the contract test and verify it fails**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_goal_ui_contract.py -v
```

Expected: fail because the dashboard still uses `chart-goal` and `find()`.

- [ ] **Step 3: Replace the single ECharts gauge with a semantic list**

Change the dashboard card to contain `<div id="goal-progress-list" class="goal-progress-list">`. In `loadStats`, fetch goals once and pass them to `renderGoalProgressList`. Filter statuses `冷静期`, `进行中`, and `已暂停`; preserve API order; render every result with escaped name, status, `已存`, `需要`, `还差`, percentage, and a progress element.

Use this calculation:

```javascript
const saved = Number(g.saved) || 0;
const target = Number(g.price) || 0;
const remaining = Math.max(0, target - saved);
const pct = target > 0 ? Math.min(100, saved / target * 100) : 0;
```

The first eligible goal receives a visible `当前优先目标` tag; paused goals receive a paused class.

- [ ] **Step 4: Fix goal deletion in the browser**

In `goalAction`, handle `delete` before the action endpoint:

```javascript
if (act === 'delete') {
  const goal = (await (await fetch('/api/goals')).json()).find((g) => g.id === id);
  if (!goal) { showToast('目标已经不存在', 'error'); return; }
  const message = `删除“${goal.name}”目标？\n其中规划的 ¥${Number(goal.saved).toFixed(2)} 将不再归属于任何目标，但不会改变账本余额。`;
  if (!confirm(message)) return;
  const response = await fetch(`/api/goals/${id}`, { method: 'DELETE' });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) { showToast(data.message || '删除失败', 'error'); return; }
  await Promise.all([loadGoals(), loadStats(), loadSummary()]);
  return;
}
```

Do not send `delete` to `/action`.

- [ ] **Step 5: Apply the confirmed typography and scrolling rules**

Set `body` to 15px, `.muted` to at least 13px, `.goal-meta` to 14px, and key amounts to 15px/600 weight. Give `.goal-progress-list` a bounded max height with `overflow:auto`; paused progress bars use a neutral color but text retains normal contrast.

- [ ] **Step 6: Run automated and manual checks**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_goal_ui_contract.py tests\test_goals.py -v
```

Then seed four active goals and verify in the browser that all four appear, the card scrolls rather than truncates, deletion refreshes both views, and text remains readable at Windows 100%, 125%, and 150% scaling.

- [ ] **Step 7: Commit the multi-goal UI**

```powershell
git add static\app.js static\index.html static\style.css tests\test_goal_ui_contract.py
git commit -m "feat: show all active goal progress"
```

### Task 3: Custom-range summary API, overwrite, and deletion

**Files:**
- Create: `app/summaries.py`
- Create: `tests/test_summaries_api.py`
- Modify: `app/summarizer.py:40-51, 53-140, 225-280`
- Modify: `app/main.py:217-233, 721-760`

**Interfaces:**
- Produces: `SummaryRange.parse(start: str, end: str) -> SummaryRange`, `find_existing(conn, period_type, start, end)`, and `delete_summary(summary_id: int) -> SummaryDeleteResult`.
- Changes: `summarizer.generate(period_type: str, start: date, end: date) -> tuple[str, str]`.
- Produces: `POST /api/summaries/generate` body `{period_type, period_start, period_end, overwrite}` and `DELETE /api/summaries/{sid}`.

- [ ] **Step 1: Write failing range-validation tests**

Test accepted arbitrary ranges, start after end, 367-day range, invalid ISO dates, and invalid period type. Expected error codes are `bad_date`, `bad_range`, `range_too_long`, and `bad_period`.

```python
from datetime import date

import pytest


class FakeSummaryGenerator:
    def __init__(self):
        self.calls = []

    def __call__(self, period_type, start, end):
        self.calls.append((period_type, start, end))
        return "测试总结正文", ""


@pytest.fixture
def fake_summary_generator(monkeypatch):
    fake = FakeSummaryGenerator()
    monkeypatch.setattr("app.summarizer.generate", fake)
    return fake


def test_custom_range_is_not_forced_to_calendar_week(client, fake_summary_generator):
    response = client.post("/api/summaries/generate", json={
        "period_type": "周",
        "period_start": "2026-08-03",
        "period_end": "2026-08-12",
        "overwrite": False,
    })
    assert response.status_code == 200
    assert fake_summary_generator.calls == [("周", date(2026, 8, 3), date(2026, 8, 12))]
```

- [ ] **Step 2: Run the summary API tests and verify the request model failure**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_summaries_api.py -v
```

Expected: fail because current generation accepts only `period_type` and optional `anchor`.

- [ ] **Step 3: Implement summary range validation and generation contract**

Move range parsing and duplicate lookup into `app/summaries.py`. Change `summarizer.gather` and `generate` to receive explicit `date` objects rather than deriving a range from an anchor. Preserve the 200–400/400–800 word prompt behavior based on period type.

Define the public result type exactly:

```python
@dataclass(frozen=True)
class SummaryDeleteResult:
    summary_id: int
    image_cleanup: str  # "not_needed", "deleted", or "failed"
    message: str
```

If an equal range exists and `overwrite` is false, return HTTP 409:

```json
{"error":"summary_exists","message":"这个类型和区间已经有总结","summary_id":12}
```

If `overwrite` is true, update that exact record after successful AI generation.

- [ ] **Step 4: Write failing deletion tests**

Seed a summary with a dedicated temporary image and an unrelated transaction. Delete the summary and assert the record and image disappear while the transaction remains. Add a missing-ID 404 test and a shared/outside-image-path safety test that asserts no external file is deleted.

- [ ] **Step 5: Implement safe summary deletion**

Only unlink an image when its resolved path is inside `get_paths().images_dir` and no other summary references it. Delete the database row in a transaction. If row deletion succeeds but file deletion raises `OSError`, return HTTP 200 with:

```json
{"ok":true,"image_cleanup":"failed","message":"总结已删除，但配图文件未能清理"}
```

Write the cleanup error to the business log without sensitive values.

- [ ] **Step 6: Run summary backend tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_summaries_api.py -v
```

Expected: validation, 409 overwrite, regeneration, expiry marking, and deletion tests pass without calling a real AI service.

- [ ] **Step 7: Commit summary backend behavior**

```powershell
git add app\summaries.py app\summarizer.py app\main.py tests\test_summaries_api.py
git commit -m "feat: support ranged summaries and deletion"
```

### Task 4: Summary generation modal and card actions

**Files:**
- Create: `tests/test_summary_ui_contract.py`
- Modify: `static/index.html:116-125, 190-230`
- Modify: `static/app.js:424-498, 867-900`
- Modify: `static/style.css:261-280, 365-430`

**Interfaces:**
- Consumes: summary API contract from Task 3.
- Produces: modal `#summary-modal`, `openSummaryModal(preset)`, `submitSummary(overwrite)`, `regenerateSummary(id)`, and `deleteSummary(id)`.

- [ ] **Step 1: Write a failing summary modal contract test**

Assert `index.html` contains period type controls, preset buttons, `summary-start`, `summary-end`, and submit/cancel controls. Assert `app.js` contains `submitSummary`, DELETE call to `/api/summaries/`, and 409 overwrite handling.

- [ ] **Step 2: Run the contract test and verify it fails**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_summary_ui_contract.py -v
```

- [ ] **Step 3: Implement modal defaults and validation**

Replace “生成本周总结”和“生成本月总结” with one “生成总结” button. Presets compute local dates without `toISOString()` UTC rollover. Populate:

- 本周: Monday through Sunday.
- 上周: previous Monday through Sunday.
- 本月: first through last day of current month.
- 上月: first through last day of previous month.
- 自定义: keep user-entered values.

Client-side validation mirrors backend start/end and 366-day limits, while backend remains authoritative.

- [ ] **Step 4: Implement generate, conflict, regenerate, and delete actions**

On HTTP 409, show a confirmation naming the exact type and range; confirmation resubmits with `overwrite: true`. Summary cards render “重新生成”和“删除”. Regeneration opens the modal with original values and overwrite intent. Deletion confirms that transactions are unaffected, sends DELETE, and refreshes the list.

Keep selected values after AI/network failure. Disable only the active submit button during generation and label it `写作中…`.

- [ ] **Step 5: Run automated and manual summary checks**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_summary_ui_contract.py tests\test_summaries_api.py -v
```

Manually verify presets, a 10-day 周 summary, duplicate overwrite prompt, expired regeneration, delete confirmation, and AI failure retaining the chosen range.

- [ ] **Step 6: Commit summary UI changes**

```powershell
git add static\index.html static\app.js static\style.css tests\test_summary_ui_contract.py
git commit -m "feat: add ranged summary workflow"
```

### Task 5: Initial-balance date and monthly ledger calculations

**Files:**
- Create: `app/ledger.py`
- Create: `tests/test_ledger.py`
- Modify: `app/config.py:12-32`
- Modify: `app/main.py:693-720, 762-819, 821-833`

**Interfaces:**
- Consumes: `create_backup(reason)` from the data foundation.
- Produces: `LedgerSnapshot`, `calculate_balance(conn, initial_balance: float, start_date: date, through_date: date | None = None) -> float`, `monthly_snapshot(conn, cfg: dict, month: str) -> LedgerSnapshot`, and `planned_amount(conn) -> float`.
- Produces: `POST /api/settings/initial-balance`, `GET /api/adjustments`, and `POST /api/adjustments/{id}/reverse`.

- [ ] **Step 1: Write failing ledger arithmetic tests**

Cover transactions before the start date, transactions on the start date, month opening/closing, refunds, transfers, adjustments, and goal planned amounts:

```python
def insert_tx(conn, tx_date, amount, tx_type):
    conn.execute(
        "INSERT INTO transactions(date, amount, type, category, created_at, updated_at) "
        "VALUES (?, ?, ?, '测试', 'now', 'now')",
        (tx_date, amount, tx_type),
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
```

- [ ] **Step 2: Run ledger tests and verify the module import failure**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_ledger.py -v
```

Expected: collection fails because `app.ledger` does not exist.

- [ ] **Step 3: Implement ledger calculations in one service**

Move `_ledger_balance` arithmetic from `main.py` to `app/ledger.py`. Use inclusive `date >= initial_balance_date`. `monthly_snapshot` calculates opening through the day before month start and closing through month end. `planned_amount` sums `min(saved, price)` for cold-period, active, and paused goals; unplanned balance is `max(closing_balance - planned_amount, 0)` for display only.

Define `LedgerSnapshot` with exact float fields `opening_balance`, `income`, `refund`, `expense`, `transfer_out`, `adjustments`, `closing_balance`, `planned_amount`, and `unplanned_balance`, plus string fields `period_start` and `period_end`.

- [ ] **Step 4: Add protected initial-balance correction**

Add default config keys:

```python
"initial_balance_date": date.today().isoformat(),
"onboarding_completed": False,
"app_version": "1.0.0",
```

The dedicated endpoint validates ISO date and finite numeric balance, calls `create_backup("pre-initial-balance-change")` when an existing completed onboarding value changes, saves both fields together, and returns recalculated current balance. The generic `/api/settings` route must ignore `initial_balance` and `initial_balance_date`.

Add `ensure_finance_config(conn, cfg) -> dict` for existing installations. When `initial_balance_date` is absent, use the earliest transaction date or today's date when there are no transactions. When `onboarding_completed` is absent and the database or config already contains user data, set it to true so an upgraded user is not forced through new-user onboarding. Persist the inferred values once and test both legacy-data and empty-installation cases.

- [ ] **Step 5: Write failing adjustment reversal tests**

Create an adjustment, reverse it, and assert a new row with negative diff and `reverses_adjustment_id` pointing to the original. Assert a second reversal returns 409 `already_reversed`; the original row remains unchanged.

- [ ] **Step 6: Implement adjustment history and reversal endpoints**

List newest adjustments first and include `reversed_by_id`. Reversal runs in one transaction and uses note `撤销：<original note>`. It marks affected summaries expired for the adjustment date.

- [ ] **Step 7: Replace dashboard and reconcile arithmetic with ledger service calls**

Return `opening_balance`, `closing_balance`, `planned_amount`, and `unplanned_balance` from relevant summary/month endpoints. Retain `balance` as an alias of current closing balance for compatibility during the UI transition.

- [ ] **Step 8: Run ledger and API regression tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_ledger.py tests\test_goals.py tests\test_summaries_api.py -v
```

Expected: all tests pass.

- [ ] **Step 9: Commit ledger and adjustment behavior**

```powershell
git add app\ledger.py app\config.py app\main.py tests\test_ledger.py
git commit -m "feat: add dated opening balance and traceable reconciliation"
```

### Task 6: First-run flow, balance settings, and backup controls

**Files:**
- Create: `tests/test_onboarding_ui_contract.py`
- Create: `tests/test_ai_settings.py`
- Modify: `app/ai.py:1-80`
- Modify: `app/main.py:821-833`
- Modify: `static/index.html:1-35, 140-190, 230-280`
- Modify: `static/app.js:637-660, 753-835, 867-905`
- Modify: `static/style.css:105-145, 275-365, 430-520`

**Interfaces:**
- Consumes: data-management APIs from Plan A and ledger endpoints from Task 5.
- Produces: `#onboarding-modal`, `loadOnboardingState()`, `submitOnboarding()`, `correctInitialBalance()`, `loadAdjustments()`, and backup/restore UI handlers.

- [ ] **Step 1: Write a failing onboarding UI contract test**

Assert the HTML contains four steps, migration/restore/new choices, initial date and amount, budget and auto-save ratio, optional AI configuration, and backup controls. Assert generic settings no longer posts `initial_balance`.

- [ ] **Step 2: Run the contract test and verify it fails**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_onboarding_ui_contract.py -v
```

- [ ] **Step 3: Add AI provider presets and a tested connection endpoint**

Add `ai_provider` to config with supported values and API bases:

```python
AI_PROVIDERS = {
    "OpenAI": "https://api.openai.com/v1",
    "DeepSeek": "https://api.deepseek.com",
    "Qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "自定义": "",
}
```

Implement `ai.test_connection(api_base: str, api_key: str, model: str) -> None` using a one-token chat completion with the message `回复 OK`; set a ten-second timeout and never log the key. Add `POST /api/settings/test-ai`, accepting unsaved draft values and returning `{"ok": true}` or HTTP 503 `ai_connection_failed`. In `tests/test_ai_settings.py`, monkeypatch the OpenAI-compatible client for success, timeout, invalid URL, and authentication failure; no test calls the internet.

- [ ] **Step 4: Implement first-run state flow**

On startup, read settings. When `onboarding_completed` is false, show a modal that cannot be dismissed until the user completes a new setup or successful migration/restore. AI configuration is skippable. Completion saves budget, ratio, tone, provider fields, and `onboarding_completed: true`; initial balance goes through the dedicated protected endpoint.

The migration choice opens the native directory picker, shows inspection counts and suggested date, then requires explicit confirmation. Restore accepts a `.zip` file and reports validation errors without clearing the form.

- [ ] **Step 5: Protect initial balance in settings**

Render date and amount as read-only text plus “更正初始余额”. The correction dialog repeats the impact warning; confirmation posts to `/api/settings/initial-balance`. Show the created safety-backup filename in the success result.

- [ ] **Step 6: Add adjustment history and data controls**

Render date, diff, reason, and reversal state. Only unreversed entries have a “撤销” button. Replace the old backup links with “立即备份”“恢复备份”“导出完整备份 ZIP”“打开数据文件夹”, and display the latest backup time.

- [ ] **Step 7: Add reusable toast and error helpers**

Implement `showToast(message, type = 'info')` and `requestJson(url, options)` so non-2xx responses use backend `message`. Use confirmations only for delete, overwrite, restore, migration, adjustment reversal, and initial-balance correction. Disable submit buttons until each request completes.

- [ ] **Step 8: Run automated and manual onboarding checks**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_onboarding_ui_contract.py tests\test_ai_settings.py tests\test_data_api.py tests\test_ledger.py -v
```

Manually verify all three first-run paths, skipping AI, correcting initial balance, reversing an adjustment, creating/restoring backup, and opening the data folder.

- [ ] **Step 9: Commit onboarding and settings UI**

```powershell
git add app\ai.py app\main.py static\index.html static\app.js static\style.css tests\test_onboarding_ui_contract.py tests\test_ai_settings.py
git commit -m "feat: add first-run and protected balance settings"
```

### Task 7: Upload type, size, and filename safety

**Files:**
- Create: `app/uploads.py`
- Create: `tests/test_uploads.py`
- Modify: `app/main.py:280-386`

**Interfaces:**
- Produces: `read_limited(upload: UploadFile, max_bytes: int) -> bytes`, `validate_image(upload) -> str`, and `validate_statement(upload) -> str`.
- Enforces: at most 10 images per request, 10 MiB per image, and 20 MiB per CSV/XLSX/XLSM statement file.

- [ ] **Step 1: Write failing upload validation tests**

Use in-memory multipart uploads to assert PNG/JPEG/WebP acceptance; GIF, executable, mismatched extension/content type, empty file, 11 MiB image, 21 MiB statement, and 11-image requests return stable 400 or 413 errors. Assert saved image names match `^[0-9a-f]{32}\.(png|jpg|jpeg|webp)$` and never contain the submitted filename.

- [ ] **Step 2: Run upload tests and verify current permissive behavior fails them**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_uploads.py -v
```

Expected: current endpoints accept or rename unsupported image types rather than rejecting them.

- [ ] **Step 3: Implement streaming size limits and allowlists**

Read uploads in 64 KiB chunks and stop immediately when the size limit is exceeded. Accept image MIME/extension pairs for PNG, JPEG, and WebP only, and verify PNG `89 50 4E 47`, JPEG `FF D8 FF`, or WebP `RIFF....WEBP` file signatures before writing. Accept statements ending in `.csv`, `.xlsx`, or `.xlsm`; require ZIP signature `50 4B` for XLSX/XLSM and successful supported-text decoding for CSV. Keep the existing clear rejection for `.xls`. Return `too_many_files`, `file_too_large`, `empty_file`, or `unsupported_file_type` with Chinese messages.

- [ ] **Step 4: Preserve generated filenames and cleanup on failure**

Generate UUID filenames, write through a temporary `.uploading` file, and rename only after validation. If AI parsing fails, retain the validated image as a pending item as before. If validation or disk writing fails, remove the temporary file in `finally`.

- [ ] **Step 5: Run upload and core API tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_uploads.py tests\test_data_api.py tests\test_goals.py tests\test_summaries_api.py tests\test_ledger.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit upload safeguards**

```powershell
git add app\uploads.py app\main.py tests\test_uploads.py
git commit -m "fix: validate uploaded file types and sizes"
```

### Task 8: Core-finance regression gate

**Files:**
- Modify: `README.md:20-65`
- Modify: `使用说明.md:95-365`
- Modify: `tests/run_all.py:8-25`

**Interfaces:**
- Consumes: every interface from this plan.
- Produces: documented user workflows and a single business regression command.

- [ ] **Step 1: Update user documentation**

Document multiple goals, priority allocation, summary type versus range, summary deletion, one-time initial balance, automatic monthly roll-forward, adjustment reversal, optional AI, and the first-run paths. Use the exact labels present in the UI.

- [ ] **Step 2: Add new pytest files to the regression entry point**

Either make `tests/run_all.py` invoke pytest for the new files or replace it with a small wrapper around:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_paths.py tests\test_migrations.py tests\test_backup.py tests\test_legacy_migration.py tests\test_data_api.py tests\test_goals.py tests\test_goal_ui_contract.py tests\test_summaries_api.py tests\test_summary_ui_contract.py tests\test_ledger.py tests\test_onboarding_ui_contract.py tests\test_ai_settings.py tests\test_uploads.py -v
```

The wrapper must retain the real-data safety guard from Plan A.

- [ ] **Step 3: Run the complete automated suite**

Run the command above. Expected: all tests pass with no real AI or network call.

- [ ] **Step 4: Run browser acceptance at three display scales**

Using a disposable application home, verify at Windows 100%, 125%, and 150%:

- Four active goals all appear.
- Goal amounts and remaining amounts are readable.
- Target deletion works on the first attempt.
- A 10-day 周 summary generates, conflicts, overwrites, expires, regenerates, and deletes.
- Initial balance rolls from July into August without another user entry.
- Manual bookkeeping works with API Key blank and network disconnected.

- [ ] **Step 5: Commit documentation and any acceptance fixes**

```powershell
git add README.md 使用说明.md tests\run_all.py
git commit -m "docs: update goal summary and balance workflows"
```

- [ ] **Step 6: Record Gate B evidence**

Capture the full pytest summary and list the three display scales checked in the handoff. Confirm `git status --short` shows no unexpected files before proceeding to Windows packaging.
