# Better Money Windows Productization Roadmap

> **For agentic workers:** Execute the three linked plans in order. Each plan has its own test and review gate; do not start a later plan while an earlier plan has failing verification.

**Goal:** Deliver an installable Windows 10/11 64-bit Better Money application that starts reliably without Python, preserves user data, and fixes the confirmed goal, summary, and balance defects.

**Spec:** `docs/superpowers/specs/2026-08-20-windows-productization-design.md`

## Execution order

1. [`2026-08-20-data-foundation-implementation.md`](2026-08-20-data-foundation-implementation.md)
   - Isolated test environment
   - Installed/development path abstraction
   - Versioned database migrations
   - Backup, restore, and legacy data migration
   - Data-management APIs

2. [`2026-08-20-core-finance-implementation.md`](2026-08-20-core-finance-implementation.md)
   - Multi-goal allocation, display, and deletion
   - Custom-range summaries and deletion
   - Initial-balance date, monthly roll-forward, and adjustment reversal
   - First-run flow, readability, and user-facing error behavior

3. [`2026-08-20-windows-packaging-implementation.md`](2026-08-20-windows-packaging-implementation.md)
   - Single-instance Windows launcher
   - Dynamic local port and verified health checks
   - Secure shutdown
   - PyInstaller and Inno Setup builds
   - Clean-machine installation, upgrade, uninstall, and release verification

## Review gates

- Gate A: all data-foundation tests pass against a temporary application home; legacy migration and failed-restore rollback are demonstrated.
- Gate B: all business tests pass; each confirmed defect is manually verified in the browser at 100%, 125%, and 150% scaling.
- Gate C: the installer works on clean Windows 10 and Windows 11 64-bit machines without Python; upgrade and uninstall preserve data by default.

## Specification coverage

| Design sections | Owning plan/tasks |
|---|---|
| 1–3 Background, scope, architecture | Roadmap; Windows Packaging Tasks 3–4 |
| 4 Install, launch, repeat click, exit | Windows Packaging Tasks 1–2 and 4–5 |
| 5 Data paths, migration, backup, restore | Data Foundation Tasks 1–6 |
| 6 Goals | Core Finance Tasks 1–2 |
| 7 Summaries | Core Finance Tasks 3–4 |
| 8 Initial balance and reconciliation | Core Finance Tasks 5–6 |
| 9 First run and optional AI | Core Finance Task 6 |
| 10 Readability and error feedback | Core Finance Tasks 2, 4, and 6 |
| 11 Database compatibility | Data Foundation Task 2; Core Finance Tasks 3 and 5 |
| 12 Security and privacy | Data Foundation Tasks 3–5; Core Finance Task 7; Windows Packaging Tasks 1–2 and 4 |
| 13 Tests | Every plan's final gate |
| 14 Release | Windows Packaging Task 6 |
| 15–16 Sequence and completion | This roadmap and Gates A–C |

## Final verification

After all three plans pass their gates, run the full Python suite, the packaged application smoke suite, and the installer checklist. Build `BetterMoney-Setup-1.0.0.exe`, calculate its SHA-256 digest, and prepare release notes. Do not publish a GitHub Release without explicit user authorization.
