# Tasks: Remove Platform-Wide Sandbox Call Quota

**Input**: Design documents from `/specs/004-remove-sandbox-call-quota/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md (all present)

**Tests**: This removal deletes/edits existing tests as part of the deliverable itself (there is no new behavior to write new tests for) — test-file changes are folded into the same story as the code they exercise, not a separate TDD phase.

**Organization**: Tasks are grouped by user story (US1–US4 map to spec.md's priorities P1/P1/P2/P2). Unlike an additive feature, this removal's stories are **not independently shippable** — US2's call-site edits must land in the same change as US1's module deletion (deleting the module while call sites still import it would break the app at boot). The Dependencies section below states the real required order; the `[Story]` labels below track *which capability a task serves*, not an independent-deployment boundary.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2, US3, US4)
- All file paths are relative to the repository root (`backend/...`, `docs/...`, `specs/...`)

---

## Phase 1: Setup

**Purpose**: Establish a baseline to diff against — this is a removal, so "nothing else changed" is the success condition.

- [X] T001 Run `cd backend && python -m pytest tests/ -q 2>&1 | tail -5` and record the exact pass/fail/skip counts. This is the baseline: after the removal, the count must be identical minus exactly the number of deleted test functions (T013, T016, T019a) — any other change in the count is a regression outside this feature's scope.

---

## Phase 2: User Story 2 - Sandbox dispatch keeps working, gated solely by the per-user check (Priority: P1)

**Goal**: Remove the platform-wide guard from all three OpenSandbox call sites, leaving spec 002's per-user combined check as the sole quota gate — done **first**, so nothing in the codebase still imports `app.services.sandbox_quota` before that module is deleted in User Story 1.

**Independent Test**: Drive a user's combined agentic+sandbox usage to their configured limit; confirm sandbox dispatch is refused at exactly that point (spec 002's existing behavior, unchanged) with no other, platform-wide-flavored refusal path reachable — see quickstart.md Scenario 2.

- [X] T002 [US2] In `backend/app/services/code_interpreter.py`, inside `execute_python_opensandbox`: delete the `from app.services.sandbox_quota import (SandboxQuotaExceededError, check_sandbox_quota, record_sandbox_call, sandbox_quota_message)` block and the `try: await check_sandbox_quota() except SandboxQuotaExceededError as exc: return {...}` block immediately after it (research.md §3 — lines 540-556 as read during planning); delete the standalone `await record_sandbox_call()` line that currently follows the spec-002 combined-check block (research.md §3, line 611 as read during planning). Leave the spec-002 `if user_id is not None:` block itself completely unchanged in behavior.
- [X] T003 [US2] In the same function, reword the spec-002 combined-check block's leading comment (research.md §3 — currently references "before `record_sandbox_call()`" and "the platform-wide sandbox call counter," both now deleted): justify the dedicated-session choice purely on the `asyncio.gather` concurrent-`AsyncSession` hazard, which is unaffected by this removal. Depends on: T002 (same edit region).
- [X] T004 [US2] In `backend/app/services/custom_tool_executor.py`, inside `execute_js_tool_opensandbox`: delete the same `sandbox_quota` import + `try/except SandboxQuotaExceededError` block (research.md §3 — lines 1212-1222 as read during planning) and the standalone `await record_sandbox_call()` line (line 1270 as read during planning). Leave the spec-002 combined-check block unchanged.
- [X] T005 [US2] In the same function, reword the spec-002 block's leading comment (research.md §3 — lines 1224-1238 as read during planning): drop both the "before `record_sandbox_call()`" framing **and** the "to match this function's own existing `SandboxQuotaExceededError` handling one line above" cross-reference (that referent is deleted by T004) — justify the dedicated-session choice on the concurrency hazard alone, same wording approach as T003. Depends on: T004.
- [X] T006 [US2] In `backend/app/services/custom_tool_executor.py`, inside `execute_python_tool_opensandbox`: apply the identical edit as T004 at this function's own call site. Depends on: none (independent function, same file as T004 — can proceed in parallel with T004/T005 since it touches a different function body).
- [X] T007 [US2] In the same function, apply the identical comment rewording as T005. Depends on: T006.
- [X] T008 [P] [US2] In `backend/tests/test_opensandbox_sandbox_mode.py`, delete these 9 spec-001-only test functions (research.md §4 / plan.md Project Structure): `test_execute_js_opensandbox_raises_when_quota_exhausted`, `test_execute_js_opensandbox_proceeds_when_under_quota`, `test_execute_python_tool_opensandbox_raises_when_quota_exhausted`, `test_execute_python_tool_opensandbox_proceeds_when_under_quota`, `test_execute_python_opensandbox_refuses_when_quota_exhausted`, `test_execute_python_opensandbox_proceeds_when_under_quota`, `test_non_opensandbox_code_interpreter_unaffected_by_exhausted_quota`, `test_execute_python_opensandbox_skips_usage_on_quota_exceeded`, `test_execute_python_opensandbox_quota_exceeded_emits_no_sandbox_call_record`.
- [X] T009 [US2] In the same file, edit the remaining ~20 spec-002/003 test functions that import `from app.services import sandbox_quota as sq` purely to neutralize it (`monkeypatch.setattr(sq, "check_sandbox_quota", ...)` / `"record_sandbox_call"` — research.md §4 lists anchor line ranges): remove the `sq` import and both neutralizing `monkeypatch.setattr` lines from each one (that module no longer exists after User Story 1's T014). Do not change any other assertion in these functions — they test spec 002 (`increment_sandbox_usage`, `disable_sandbox_quota_usage`) or spec 003 (structured logging) behavior, which this removal does not touch. Also reword the section-header comment at the "Combined agentic+sandbox quota check" block (research.md §4, originally lines 1566-1568) so it no longer says "alongside the pre-existing platform-wide sandbox call quota (spec 001) exercised above" — that mechanism is gone. Depends on: T008 (same file, do the bulk edit after the 9 deletions to avoid re-finding shifted line numbers).
- [X] T010 [P] [US2] In `backend/tests/test_fail_modes.py`, edit `test_sandbox_dispatch_combined_quota_check_reads_quota_fail_mode_not_sandbox`: remove its `from app.services import sandbox_quota as sq` import, its two `sq`-neutralizing `monkeypatch.setattr` lines, and its `config.settings.sandbox_quota_fail_mode = "open"` line (proves a field that no longer exists has no effect — no longer meaningful once the field is gone). Keep the core assertion (spec 002 FR-019: the combined check reads `QUOTA_FAIL_MODE`) unchanged. Also update the module docstring's second bullet (added this session) that currently cites `sandbox_quota_fail_mode` by name.
- [X] T011 [P] [US2] In `backend/tests/test_custom_tool_sandbox_mode.py`, delete `test_docker_mode_unaffected_by_exhausted_opensandbox_quota` (research.md §4) — the file's remaining ~40 tests (Docker/subprocess/Node-permission sandbox-mode *selection*) are unrelated and untouched.

**Checkpoint**: `grep -rn "sandbox_quota" backend/app/services/code_interpreter.py backend/app/services/custom_tool_executor.py` returns zero matches. `python -m pytest backend/tests/test_opensandbox_sandbox_mode.py backend/tests/test_fail_modes.py backend/tests/test_custom_tool_sandbox_mode.py -q` passes (module `app.services.sandbox_quota` still exists at this checkpoint — User Story 1 hasn't deleted it yet — so these tests run against live code either way; T009/T010's edits just stop *exercising* it here).

---

## Phase 3: User Story 1 - Retire the redundant platform-wide quota mechanism (Priority: P1)

**Goal**: Delete the platform-wide mechanism's module, model, config surface, admin endpoint, and scheduler registration outright — safe now that Phase 2 removed every caller.

**Independent Test**: `grep -rn "check_sandbox_quota\|record_sandbox_call\|SandboxQuotaExceededError\|sandbox_quota_message\|flush_sandbox_quota_usage\|sandbox_quota_limits\|sandbox_quota_fail_mode" backend/app/` returns zero matches; the backend boots and serves requests normally — see quickstart.md Scenario 1.

- [X] T012 [US1] Delete `backend/app/services/autonomous.py`'s spec-001 scheduler registration: the `_SANDBOX_QUOTA_FLUSH_INTERVAL` / `_sandbox_quota_flush_tick_counter` module-level state (research.md — originally lines 132-137) and the "10b. Flush sandbox quota usage counters" block (originally lines 1511-1525) that calls `flush_sandbox_quota_usage()` every 5 ticks. Depends on: Phase 2 complete (T002-T011) — this is the last remaining caller of `app.services.sandbox_quota` outside that module itself.
- [X] T013 [P] [US1] Delete `backend/app/api/v1/settings.py`'s `GET /sandbox-quotas` admin endpoint (`get_sandbox_quotas`, docstring explicitly labeled "spec 001-sandbox-call-quotas" — research.md §5). No replacement, no frontend consumer to update (confirmed by repo-wide search). Depends on: Phase 2 complete.
- [X] T014 [US1] Delete `backend/app/services/sandbox_quota.py` in its entirety (every function/class in it — `SANDBOX_QUOTA_UNAVAILABLE_MESSAGE`, `sandbox_quota_fail_closed`, `SandboxQuotaExceededError`, `check_sandbox_quota`, `record_sandbox_call`, `sandbox_quota_message`, `get_sandbox_quota_status`, `flush_sandbox_quota_usage`, and all private helpers — data-model.md). Depends on: T012, T013 (must be the last two callers gone first).
- [X] T015 [P] [US1] Delete `backend/app/models/sandbox_quota.py` in its entirety (the `SandboxQuotaUsage` model, table `sandbox_quota_usage` — data-model.md). Depends on: T014 (same reasoning — nothing should reference this model once the service module using it is gone; in practice independent file, can run alongside T014).
- [X] T016 [P] [US1] Delete `backend/tests/test_sandbox_quota.py` in its entirety (~26 spec-001-only tests, including the two "unaffected by `disable_sandbox_quota_usage`" non-interference proofs — research.md §4: these test spec 001's behavior, not spec 002's, so they die with spec 001). Depends on: T014.
- [X] T017 [US1] In `backend/app/core/config.py`: delete the `sandbox_quota_limits: str = ""` and `sandbox_quota_fail_mode: str = "open"` fields and their preceding comment block (data-model.md — originally lines 1073-1100); delete the dedicated `@field_validator("sandbox_quota_limits") def _validate_sandbox_quota_limits(...)` validator entirely (originally lines 1274-1305); remove only the string `"sandbox_quota_fail_mode"` from the shared `@field_validator("rate_limit_fail_mode", "quota_fail_mode", "sandbox_quota_fail_mode")` tuple (originally line 1260) — keep the validator function and the other two field names untouched, they belong to unrelated mechanisms; delete the `_sandbox_quota_limits_map`, `sandbox_quota_daily_limit`, `sandbox_quota_weekly_limit`, `sandbox_quota_monthly_limit` properties (originally lines 1594-1623). **Do NOT touch** `disable_sandbox_quota_usage` (originally line 1007) — confirmed spec 002, unrelated (research.md §2). Depends on: T014, T015 (nothing should still read these fields once the module consuming them is gone).
- [X] T018 [P] [US1] In `docs/docs/deployment/configuration.md`: delete the `SANDBOX_QUOTA_LIMITS` and `SANDBOX_QUOTA_FAIL_MODE` rows (originally lines 143-144); reword the `DISABLE_SANDBOX_QUOTA_USAGE` row's trailing clause (originally line 129, currently "Independent of the platform-wide `SANDBOX_QUOTA_LIMITS` caps below, which are unaffected either way.") to drop the now-dangling cross-reference — that row itself stays, it's spec 002's; reword the "Sandbox call log fields" section's prose (originally line 151) that name-drops `sandbox_quota_usage` alongside `quota_usage`. Depends on: T017.

**Checkpoint**: `ls backend/app/services/sandbox_quota.py backend/app/models/sandbox_quota.py` both report "No such file or directory." Full backend test suite (excluding the migration-dependent tests handled in User Story 4) passes.

---

## Phase 4: User Story 3 - Keep the spec suite internally consistent (Priority: P2)

**Goal**: Amend spec 002 and spec 003 so neither describes the now-removed mechanism as still operating.

**Independent Test**: Read spec 002's FR-009 and spec 003's FR-011 directly and confirm neither requires the removed platform-wide cap / `sandbox_quota_usage` ledger to keep existing — see quickstart.md Scenario 3.

- [X] T019 [P] [US3] In `specs/002-sandbox-quota-usage-tracking/spec.md`: rewrite FR-009 (currently "This feature MUST NOT change the behavior of the existing platform-wide sandbox call limits... those continue to operate independently of per-user usage counting") to reflect that the platform-wide cap no longer exists — this feature's per-user counting is now the only sandbox-usage-related mechanism. Update User Story 2's Independent Test and its acceptance scenario 2 (both currently assert the platform-wide limits "continue to be enforced exactly as before"), SC-004 (currently asserts the platform-wide limits "continue to trigger at exactly the same thresholds"), and the two Assumptions bullets describing "the existing platform-wide sandbox call counter" as a still-existing, unaffected mechanism. Do not change any requirement about spec 002's own per-user tracking/combined-check behavior (FR-001–FR-008, FR-010–FR-019) — only the now-moot platform-wide references.
- [X] T020 [P] [US3] In `specs/003-sandbox-call-logging/spec.md`: rewrite FR-011 (currently "This feature MUST NOT change the existing quota-check or quota-recording behavior (`sandbox_quota_usage` ledger)") to reflect that the ledger it referenced no longer exists — this feature's per-call structured logging is independent of whatever per-user quota mechanism remains (spec 002's). Do not change any requirement about spec 003's own logging behavior.

**Checkpoint**: Neither spec.md contains a requirement, acceptance scenario, or success criterion that depends on the removed mechanism still existing.

---

## Phase 5: User Story 4 - Remove the never-merged migration cleanly, without a downgrade path (Priority: P2)

**Goal**: Delete the migration that created `sandbox_quota_usage` directly at its source (research.md §1 — it was never merged to `main`, so it carries no released-deployment compatibility obligation), and reconcile this branch's own already-migrated local/CI databases as one-time housekeeping.

**Independent Test**: `python -m alembic -c backend/alembic/alembic.ini heads` reports a single head, `a7c3sk1ll2697`, with no reference anywhere to the deleted revision — see quickstart.md Scenario 4.

- [X] T021 [US4] Delete `backend/alembic/versions/e3877a6a8159_add_sandbox_quota_usage_table.py` outright. Confirmed via `grep -rn "e3877a6a8159" backend/alembic/versions/*.py` (research.md §1) that no other migration references it as their own `down_revision` — the chain's new head becomes `a7c3sk1ll2697` (its former parent) automatically, no other file needs editing. Depends on: T014, T015 (the model/service using this table should be gone first, though the migration and the model are independently deletable — sequencing here just keeps the "remove the whole vertical slice" story coherent).
- [X] T022 [US4] On this branch's own local development database (and any CI database that already ran `alembic upgrade head` while `e3877a6a8159` existed): run the one-time manual reconciliation from research.md §1 — `DROP TABLE sandbox_quota_usage;` followed by `UPDATE alembic_version SET version_num = 'a7c3sk1ll2697';`. This is branch-local housekeeping, not a shipped migration step (spec FR-007a) — do not write this as a new Alembic revision. Depends on: T021.
- [X] T023 [US4] Verify: `python -m alembic -c backend/alembic/alembic.ini heads` reports exactly one head, `a7c3sk1ll2697`; `python -m alembic -c backend/alembic/alembic.ini upgrade head` against the reconciled database in T022 is a no-op (already at head); the backend boots cleanly (`curl localhost:8100/api/health/ready` → `200`) with no alembic-state error. Depends on: T022.

**Checkpoint**: Fresh installs never create `sandbox_quota_usage`; this branch's own dev database is fully reconciled to the new chain head.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Final verification that the removal is complete, and removal of the spec artifact directory itself.

- [X] T024 [P] Delete the `specs/001-sandbox-call-quotas/` directory in its entirety (spec.md, plan.md, research.md, data-model.md, quickstart.md, tasks.md, checklists/, contracts/) — the spec being removed by this feature (spec 004's own framing: "remove... entirely").
- [X] T025 Run quickstart.md Scenarios 1-4 end-to-end and confirm every `grep`/`ls`/`alembic heads` check reports exactly the expected result.
- [X] T026 Run `cd backend && python -m pytest tests/ -q` and compare the pass/fail/skip counts against T001's baseline: expect identical counts minus exactly 26 (test_sandbox_quota.py) + 9 (test_opensandbox_sandbox_mode.py deletions) + 1 (test_custom_tool_sandbox_mode.py) = 36 fewer tests, zero new failures anywhere else.
- [X] T027 [P] Run `ruff check backend/app/ backend/tests/` and confirm zero unused-import or unreachable-code warnings introduced by this removal (particularly: no dangling `from app.services.sandbox_quota import ...` anywhere, no unused `SandboxQuotaExceededError`-style imports left in any edited test file).

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — T001 is a read-only baseline capture.
- **US2 (Phase 2)**: No dependencies on other phases — but internally, T002/T004/T006 (the three call-site edits) must land **before** US1's T014 deletes the module they currently import, or the app breaks at boot in between. This is why US2 is sequenced before US1 in this file despite both being P1 — it is a hard correctness dependency, not a priority ranking.
- **US1 (Phase 3)**: Depends on US2 (Phase 2) being complete — T012/T013 depend on Phase 2 finishing (last callers gone); T014 depends on T012+T013; T015/T016/T017 depend on T014.
- **US3 (Phase 4)**: Independent of US1/US2/US4 — pure documentation, can run in parallel with any other phase, including before them if preferred (there is no code dependency).
- **US4 (Phase 5)**: Independent of US1's config/module deletion in principle, but sequenced after it here (T021 depends on T014/T015) to keep the "delete the whole vertical slice together" narrative coherent — deleting the migration file itself has no actual code dependency on T014/T015 having happened first.
- **Polish (Phase 6)**: Depends on all of Phases 2-5 being complete.

### Within Each Phase

- Phase 2: T002→T003 (same edit region); T004→T005 (same); T006→T007 (same, independent of T004/T005 — different function, same file); T008→T009 (T009 needs T008's deletions done first so its own line numbers aren't shifting under it); T010, T011 independent files, parallel with everything else in this phase.
- Phase 3: T012, T013 parallel (different files); T014 depends on both; T015, T016 parallel with each other once T014 is done; T017 depends on T014+T015; T018 depends on T017.
- Phase 5: T021→T022→T023, strictly sequential (each step's precondition is the previous step's completion).

### Parallel Opportunities

- T008, T010, T011 (three different test files, Phase 2) can proceed in parallel once the corresponding call-site edits (T002-T007) are done.
- T012 and T013 (Phase 3, different files) can proceed in parallel.
- T015 and T016 (Phase 3, different files) can proceed in parallel once T014 is done.
- T019 and T020 (Phase 4, different spec files) can proceed in parallel, and this whole phase can run in parallel with Phases 2/3/5 since it has no code dependency.
- T024 and T027 (Phase 6) are independent of each other and of T025/T026's ordering.

---

## Parallel Example: Phase 3 (User Story 1)

```bash
# T012 and T013 first, in parallel (different files, both must finish before T014):
Task: "T012 Delete autonomous.py's sandbox-quota-flush scheduler block"
Task: "T013 Delete settings.py's GET /sandbox-quotas admin endpoint"

# Then T014 (depends on both above):
Task: "T014 Delete app/services/sandbox_quota.py entirely"

# Then T015 and T016 in parallel (depend on T014, independent of each other):
Task: "T015 Delete app/models/sandbox_quota.py entirely"
Task: "T016 Delete tests/test_sandbox_quota.py entirely"

# Then T017 (depends on T014+T015), then T018 (depends on T017):
Task: "T017 Remove sandbox_quota_limits/sandbox_quota_fail_mode from config.py"
Task: "T018 Remove SANDBOX_QUOTA_LIMITS/SANDBOX_QUOTA_FAIL_MODE from configuration.md"
```

---

## Implementation Strategy

### Recommended Order (this removal is not meaningfully "MVP-sliceable" — it's one coherent change)

1. Phase 1: Setup (T001) — capture baseline.
2. Phase 2: US2 (T002-T011) — edit the three call sites and their tests first, so the app never has a moment where it imports a module about to be deleted.
3. Phase 3: US1 (T012-T018) — delete the module, model, config, endpoint, scheduler registration, and their dedicated tests.
4. Phase 5: US4 (T021-T023) — delete the migration, reconcile local databases. (Can also run any time after Phase 3, or even in parallel with it — the only hard dependency is deleting the migration file after or alongside the model it backs.)
5. Phase 4: US3 (T019-T020) — amend spec 002/003. Can run at any point, including first — pure documentation with no code dependency.
6. Phase 6: Polish (T024-T027) — remove spec 001's own directory, run full verification.

### Parallel Team Strategy

With multiple developers: one person takes Phase 2 (call sites + their tests) while another takes Phase 4 (spec amendments, no dependency on anything) and a third prepares Phase 5's migration deletion (safe to draft in parallel, just don't merge before Phase 3's T014/T015 land in the same change). Converge on Phase 3 together once Phase 2 is done, then Phase 6 last.

---

## Notes

- [P] tasks = different files, no blocking dependency.
- [Story] label maps task to which capability it serves, not an independent-deployment guarantee (see the note under Organization above).
- No new database table, no new migration, no new config, no new frontend work — this file only deletes and amends.
- Commit after each phase checkpoint, not each individual task — a removal is easiest to review as coherent chunks (e.g. "all three call sites," "the whole module + its tests"), not as 27 tiny diffs.
- Re-run `grep -rn "sandbox_quota" backend/` (case-sensitive) periodically during implementation as a cheap sanity check — the only survivors should be `disable_sandbox_quota_usage` and `quota_usage`/`increment_sandbox_usage` (spec 002, untouched) and this feature's own `specs/004-.../` documentation.
