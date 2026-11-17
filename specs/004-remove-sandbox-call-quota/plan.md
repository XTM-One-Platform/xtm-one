# Implementation Plan: Remove Platform-Wide Sandbox Call Quota

**Branch**: `004-remove-sandbox-call-quota` | **Date**: 2026-08-03 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/004-remove-sandbox-call-quota/spec.md`

**Note**: This template is filled in by the `/speckit-plan` command; its definition describes the execution workflow.

## Summary

Delete spec `001-sandbox-call-quotas`'s platform-wide, all-users-combined OpenSandbox call quota entirely — its dedicated module (`app/services/sandbox_quota.py`), its dedicated model/table (`app/models/sandbox_quota.py` / `sandbox_quota_usage`), its config fields (`sandbox_quota_limits`, `sandbox_quota_fail_mode` and their validators/properties), its admin status endpoint, its scheduler-flush registration, and all spec-001-only tests — leaving spec `002-sandbox-quota-usage-tracking`'s per-user combined agentic+sandbox check (FR-015–FR-019) as the sole quota gate at the three OpenSandbox dispatch call sites. Because the migration that created `sandbox_quota_usage` (`e3877a6a8159`) has never been merged to `main`, it is deleted outright rather than reversed with a new downgrade migration (research.md §1) — the chain's new head becomes its parent, `a7c3sk1ll2697`, and this branch's own already-migrated dev/CI databases are reconciled once, manually, as branch-local housekeeping. Specs 002 and 003 are amended so their FR-009/FR-011 (and related scenarios/success-criteria) no longer describe the removed mechanism as continuing to exist.

The single riskiest part of this feature is a **naming collision**, confirmed by research: `settings.disable_sandbox_quota_usage` (spec 002, gates `increment_sandbox_usage`'s per-user `quota_usage` counter) is a completely different, must-NOT-touch field from the `sandbox_quota_usage` **table** and `sandbox_quota_fail_mode`/`sandbox_quota_limits` **fields** (spec 001, all removed). Every task below is written to name the exact symbol, not just a description, to keep this distinction unambiguous during implementation.

## Technical Context

**Language/Version**: Python 3.13 (backend), TypeScript/React (frontend — confirmed untouched, see Project Structure)

**Primary Dependencies**: FastAPI, SQLAlchemy (async), Alembic, `redis.asyncio` — no dependency changes; this feature only removes existing code, no new libraries.

**Storage**: Removes one PostgreSQL table (`sandbox_quota_usage`) and its Redis-side counters (`sandbox_quota:usage:*` keys, which simply expire via their existing TTL and need no explicit cleanup). Does not touch the unrelated `quota_usage` table (spec 002) in any way.

**Testing**: pytest. One file deleted outright (`tests/test_sandbox_quota.py`); one test function removed from `tests/test_custom_tool_sandbox_mode.py`; nine test functions removed from `tests/test_opensandbox_sandbox_mode.py` (spec-001-only behavior) with ~20 more edited in the same file to drop now-dead `sandbox_quota` mock setup (spec-002/003 tests that merely stubbed spec 001 out of the way); one test in `tests/test_fail_modes.py` edited the same way. `tests/test_sandbox_usage_quota.py` (spec 002) is entirely untouched (research.md §5 confirms zero references).

**Target Platform**: Linux server (existing FastAPI backend, Docker/Kubernetes replicas) — unchanged.

**Project Type**: Web application (backend + frontend). This feature is backend-only: research.md §2 confirms zero frontend references to the removed admin endpoint or any spec-001 concept (the four frontend `sandbox_quota` hits found by search are all comments citing spec 002, not spec 001).

**Performance Goals**: N/A — pure removal, no new hot path. Marginally *reduces* per-dispatch overhead (one fewer Redis round-trip per OpenSandbox call, from the now-removed `check_sandbox_quota()`).

**Constraints**: The removed migration (`e3877a6a8159`) must not leave any other migration pointing to it as a `down_revision` (research.md §4 confirms it is the current, undepended-on chain head — safe to delete outright); the chain's new head becomes `a7c3sk1ll2697` automatically once the file is gone. This branch's own dev/CI databases that already applied `e3877a6a8159` need a one-time manual reconciliation (drop table, re-stamp `alembic_version`) — not a shipped migration (spec FR-006/FR-007a; this is exactly the same class of fix already performed once this session for a different, unrelated alembic-state mismatch).

**Scale/Scope**: 7 backend source files edited/deleted (`app/services/sandbox_quota.py` deleted, `app/models/sandbox_quota.py` deleted, `app/services/code_interpreter.py` edited, `app/services/custom_tool_executor.py` edited at 2 call sites, `app/services/autonomous.py` edited, `app/api/v1/settings.py` edited, `app/core/config.py` edited); 1 migration file deleted; 4 test files touched (1 deleted, 3 edited); 1 docs file edited; 2 sibling spec files amended (002's spec.md, 003's spec.md); spec 001's own artifact directory (`specs/001-sandbox-call-quotas/`) removed.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Evaluated against `.specify/memory/constitution.md` v1.0.0.

- [x] **Fail-Closed by Default** (Principle I): N/A in the "new check" sense — this feature removes a check, it adds none. The remaining check (spec 002's per-user combined quota) already satisfies this principle on its own and is completely untouched by this removal (FR-002). No new fail-open/fail-closed decision is introduced.
- [x] **Concurrency-Safe, Multi-Replica by Design** (Principle II): N/A — no new scheduled job, startup task, or shared-counter mutation is introduced. This feature *removes* a scheduled job (`autonomous.py`'s sandbox-quota flush tick, research.md §2) and its advisory-lock-guarded flush function (`flush_sandbox_quota_usage`) — removing a concurrency-safe mechanism entirely is not itself a concurrency hazard, since nothing takes its place.
- [x] **CI-Enforced Quality Gates** (Principle III): satisfied — every deleted/edited test is enumerated in research.md §5 with exact line ranges, so removal is deliberate and complete rather than leaving dead/failing tests behind (FR-008). Lint (`ruff`) must pass with zero unused-import warnings after every deferred `from app.services.sandbox_quota import ...` line is removed from the three call sites. The migration CI job's idempotent-re-run/downgrade-round-trip walk is unaffected: deleting an undepended-on chain-tip migration (research.md §4) removes it from that walk entirely rather than requiring a new downgrade to test.
- [x] **Rationale-Documented Complexity** (Principle IV): the two call-site comments in `code_interpreter.py`/`custom_tool_executor.py` that explain spec 002's dedicated-session choice currently *also* reference the soon-to-be-deleted `record_sandbox_call()`/"platform-wide sandbox call counter" (research.md — read directly during planning, lines 558-567 and 1224-1238) — both must be reworded to justify the dedicated-session choice purely in terms of the concurrency hazard (`asyncio.gather` sharing one `AsyncSession`), without any longer citing code that no longer exists. Task list must carry this forward explicitly, not just delete-around it.
- [x] **Backward-Compatible, Auditable Evolution** (Principle V): the migration being removed (`e3877a6a8159`) has never been merged to `main` (confirmed: `git log --oneline --all` shows it only on this feature branch's own commits), so it carries no released-deployment compatibility obligation — deleting it outright, rather than adding a new downgrade migration, does not violate this principle's intent (which protects *already-shipped* schema history for real deployments). This is the explicit, deliberate exception the spec's own FR-006 documents, not an oversight. This branch's own already-migrated dev/CI databases are reconciled manually, once — not via a mechanism that ships to any real deployment (spec FR-007a).

No violations requiring justification beyond the Principle V note above (which the spec itself already documents as the reasoned exception, not a gap).

## Project Structure

### Documentation (this feature)

```text
specs/004-remove-sandbox-call-quota/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md        # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
├── checklists/
│   └── requirements.md  # Spec quality checklist (/speckit-specify)
└── tasks.md              # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

### Source Code (repository root)

`backend/` is a Python FastAPI service. This feature is backend-only — no frontend files are touched (Technical Context, Project Type).

```text
backend/
├── app/
│   ├── core/
│   │   └── config.py                    # DELETE: sandbox_quota_limits, sandbox_quota_fail_mode fields +
│   │   │                                 # their comment block; DELETE: _validate_sandbox_quota_limits
│   │   │                                 # validator entirely; EDIT: drop "sandbox_quota_fail_mode" from
│   │   │                                 # the shared _validate_fail_mode field_validator tuple (keep the
│   │   │                                 # validator + the other two entries); DELETE: _sandbox_quota_limits_map,
│   │   │                                 # sandbox_quota_daily_limit, sandbox_quota_weekly_limit,
│   │   │                                 # sandbox_quota_monthly_limit properties.
│   │   │                                 # KEEP UNTOUCHED: disable_sandbox_quota_usage (spec 002 — different field)
│   │   └── ...
│   ├── models/
│   │   └── sandbox_quota.py              # DELETE entire file (SandboxQuotaUsage model, table sandbox_quota_usage)
│   ├── services/
│   │   ├── sandbox_quota.py              # DELETE entire file (check_sandbox_quota, record_sandbox_call,
│   │   │                                 # SandboxQuotaExceededError, sandbox_quota_message,
│   │   │                                 # get_sandbox_quota_status, flush_sandbox_quota_usage, etc.)
│   │   ├── code_interpreter.py           # execute_python_opensandbox: DELETE the check_sandbox_quota()
│   │   │                                 # import+try/except block and the record_sandbox_call() call;
│   │   │                                 # REWORD the spec-002 combined-check comment to drop its
│   │   │                                 # "before record_sandbox_call()" framing (Principle IV, above)
│   │   ├── custom_tool_executor.py       # execute_js_tool_opensandbox AND execute_python_tool_opensandbox:
│   │   │                                 # same two edits as code_interpreter.py, at both call sites;
│   │   │                                 # REWORD the comment at each site that references "one line above"
│   │   │                                 # (the deleted check_sandbox_quota catch)
│   │   └── autonomous.py                 # DELETE _SANDBOX_QUOTA_FLUSH_INTERVAL, _sandbox_quota_flush_tick_counter,
│   │                                     # and the "10b. Flush sandbox quota usage counters" scheduler block
│   └── api/
│       └── v1/
│           └── settings.py               # DELETE GET /sandbox-quotas admin endpoint (get_sandbox_quotas) —
│                                          # no frontend consumer (confirmed by repo-wide search)
└── tests/
    ├── test_sandbox_quota.py             # DELETE entire file (~26 spec-001-only tests)
    ├── test_custom_tool_sandbox_mode.py  # DELETE only test_docker_mode_unaffected_by_exhausted_opensandbox_quota;
    │                                     # ~40 other tests in this file are unrelated, untouched
    ├── test_opensandbox_sandbox_mode.py  # DELETE 9 spec-001-only test functions (enumerated in research.md §5);
    │                                     # EDIT ~20 spec-002/003 tests to drop their now-dead
    │                                     # `from app.services import sandbox_quota as sq` import + the
    │                                     # sq.check_sandbox_quota/sq.record_sandbox_call monkeypatch lines
    │                                     # (the module they import no longer exists)
    ├── test_fail_modes.py                # EDIT test_sandbox_dispatch_combined_quota_check_reads_quota_fail_mode_not_sandbox:
    │                                     # drop the `sq` import + its two neutralizing monkeypatches, and the
    │                                     # settings.sandbox_quota_fail_mode line — core QUOTA_FAIL_MODE
    │                                     # assertion (spec 002 FR-019) stays
    └── test_sandbox_usage_quota.py       # UNTOUCHED (confirmed zero references — spec 002 only)

alembic/versions/
└── e3877a6a8159_add_sandbox_quota_usage_table.py   # DELETE outright (never merged to main, confirmed
                                                     # undepended-on chain head — no downgrade migration added;
                                                     # a7c3sk1ll2697 becomes the new head automatically)

docs/docs/deployment/
└── configuration.md                      # DELETE the SANDBOX_QUOTA_LIMITS and SANDBOX_QUOTA_FAIL_MODE rows;
                                           # REWORD the DISABLE_SANDBOX_QUOTA_USAGE row's trailing clause
                                           # (drops its SANDBOX_QUOTA_LIMITS cross-reference) and the
                                           # "Sandbox call log fields" prose mentioning sandbox_quota_usage

specs/
├── 001-sandbox-call-quotas/              # DELETE entire directory (the spec being removed)
├── 002-sandbox-quota-usage-tracking/
│   └── spec.md                           # AMEND FR-009 + related scenarios/SC-004/Assumptions (spec 004 FR-010)
└── 003-sandbox-call-logging/
    └── spec.md                           # AMEND FR-011 (spec 004 FR-011)
```

**Structure Decision**: No new files, no new directories, no new frontend work. This is a pure subtraction plus two small edits (call-site comment rewording, two sibling-spec amendments) — the smallest possible footprint that fully satisfies spec 004's four user stories.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

No violations — table intentionally omitted. (The Principle V note above documents a deliberate, spec-sanctioned exception, not a violation requiring a Complexity Tracking entry.)
