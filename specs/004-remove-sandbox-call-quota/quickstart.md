# Quickstart: Validating the Platform-Wide Sandbox Quota Removal

**Feature**: [spec.md](./spec.md)

This is a validation guide for proving the removal is complete and correct once implemented — not an implementation guide (see `tasks.md` for that).

## Prerequisites

- Local backend running (`docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build` from repo root).
- No new environment variables or services needed; this is a pure removal.

## Scenario 1 — No platform-wide check remains anywhere in the codebase (User Story 1)

1. `grep -rn "check_sandbox_quota\|record_sandbox_call\|SandboxQuotaExceededError\|sandbox_quota_message\|flush_sandbox_quota_usage" backend/app/` — **Expected**: zero matches.
2. `grep -rn "sandbox_quota_limits\|sandbox_quota_fail_mode\|SANDBOX_QUOTA_LIMITS\|SANDBOX_QUOTA_FAIL_MODE" backend/` (excluding this feature's own `specs/` directory and historical spec-002/003 docs, which may still name the removed variables in "Superseded"/"Related fix" style historical notes) — **Expected**: zero matches in `app/` and `docs/`.
3. `ls backend/app/services/sandbox_quota.py backend/app/models/sandbox_quota.py` — **Expected**: both `No such file or directory`.
4. `grep -c "sandbox_quota_usage" backend/alembic/versions/*.py` — **Expected**: zero matches (the migration that created it is deleted).

## Scenario 2 — Sandbox dispatch still works, gated solely by the per-user check (User Story 2)

1. Configure a test user's agentic per-user limit to a small number (e.g. 3) via the existing platform quota settings (unchanged by this feature — see spec 002's own quickstart).
2. Trigger sandbox executions (code interpreter, then each custom-tool language) up to that limit. **Expected**: all succeed, identical to pre-removal behavior.
3. Trigger one more. **Expected**: refused by spec 002's combined check (same message/shape as before this removal — FR-018/FR-019 unchanged), with **no** other, platform-wide-flavored refusal ever possible, since that code path no longer exists.
4. `grep -n "sandbox_quota" backend/app/services/code_interpreter.py backend/app/services/custom_tool_executor.py` — **Expected**: zero matches (only `app.services.quota` — spec 002 — remains referenced).

## Scenario 3 — Specs 002 and 003 no longer describe the removed mechanism (User Story 3)

1. `grep -n "platform-wide sandbox call\|SANDBOX_QUOTA\|sandbox_quota_usage" specs/002-sandbox-quota-usage-tracking/spec.md` — **Expected**: zero matches in live (non-historical-clarification-log) requirement/scenario/success-criteria text. Historical `## Clarifications` session entries from before this removal may still name the old mechanism as a factual record of what was asked/answered at the time — that's fine, those are a dated log, not a current requirement.
2. `grep -n "sandbox_quota_usage" specs/003-sandbox-call-logging/spec.md` — **Expected**: zero matches in FR-011's live text (its historical framing, if any, may remain as dated context the same way).
3. Read spec 002's FR-009 and spec 003's FR-011 directly — **Expected**: neither requires "the existing platform-wide sandbox call limit / `sandbox_quota_usage` ledger" to keep operating; both reflect that it no longer exists.

## Scenario 4 — Migration chain and database reconciliation (User Story 4)

1. `cd backend && python -m alembic -c alembic/alembic.ini heads` — **Expected**: single head, `a7c3sk1ll2697` (the former parent of the now-deleted `e3877a6a8159`), not the deleted revision.
2. Against a throwaway fresh database: `python -m alembic -c alembic/alembic.ini upgrade head` — **Expected**: completes with no reference to `sandbox_quota_usage`; `\dt` in psql shows no such table.
3. Against this branch's own already-migrated local dev database (which has both the table and `alembic_version` pointing at the now-deleted revision): confirm the one-time manual reconciliation from research.md §1 was applied (`DROP TABLE sandbox_quota_usage;` + `UPDATE alembic_version SET version_num = 'a7c3sk1ll2697';`), then run `alembic upgrade head` again — **Expected**: no-op (already at head), backend starts cleanly (`curl localhost:8100/api/health/ready` → `200`).

## Unit test coverage (per Constitution Principle III)

- `tests/test_sandbox_quota.py` — **deleted**; confirm `pytest tests/test_sandbox_quota.py` reports "file or directory not found," not a pass/fail.
- `tests/test_custom_tool_sandbox_mode.py` — confirm `test_docker_mode_unaffected_by_exhausted_opensandbox_quota` no longer exists; run the rest of the file, **expect** all pass unchanged.
- `tests/test_opensandbox_sandbox_mode.py` — confirm the 9 spec-001-only tests no longer exist; run the full file, **expect** all remaining tests (spec 002/003 coverage, now with their `sandbox_quota` neutralizing mocks removed) pass.
- `tests/test_fail_modes.py` — confirm `test_sandbox_dispatch_combined_quota_check_reads_quota_fail_mode_not_sandbox` still exists and passes, with its `sq`-neutralizing lines gone.
- `tests/test_sandbox_usage_quota.py` — run unmodified; **expect** all pass (this file was never touched).
- Full suite: `python -m pytest tests/ -q` — **expect** the same pass count as before this feature, minus exactly the number of deleted test functions (no new failures anywhere else — a regression here would mean something outside the intended scope broke).
