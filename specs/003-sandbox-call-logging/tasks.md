# Tasks: Structured Per-Call Sandbox Logging (CPU/RAM/Egress/TTE)

**Input**: Design documents from `/specs/003-sandbox-call-logging/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/sandbox-call-log-event.md, quickstart.md

**Tests**: Included — this feature is backend-only, and per this project's constitution (Principle III: CI-Enforced Quality Gates), backend changes MUST be covered by tests that run in CI.

**Organization**: Tasks are grouped by user story (spec.md: US1 = P1, US2 = P2, US3 = P3) to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)
- Tasks that edit the same file are listed sequentially (no `[P]`), even within the same story

## Path Conventions

Backend-only feature. All paths are under `backend/` (FastAPI service) and `docs/` (documentation site) — no `frontend/` changes.

---

## Phase 1: Setup

**Purpose**: Scaffold the new module this feature is built on.

- [X] T001 Create `backend/app/services/sandbox_call_logging.py` with a module docstring (per research.md §2: distinct concern from `sandbox_quota.py`/`quota.py`) and a module-level `logging.getLogger(__name__)` logger instance (research.md §1 — stdlib, not structlog-native; see the revised decision there) — no logic yet.
- [X] T001a Add `structlog.stdlib.ExtraAdder()` to the `foreign_pre_chain` in `backend/app/core/logging.py`, so `logging.getLogger(...).info(msg, extra={...})` calls surface `extra` keys as top-level structured fields (research.md §1 — the corrected decision, after the original structlog-native approach was found to crash in production).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The shared `log_sandbox_call()` helper and its supporting pieces that both US1 (success path) and US2 (failure path) depend on.

**⚠️ CRITICAL**: No user story task can begin until this phase is complete.

- [X] T002 [P] In `backend/app/services/sandbox_call_logging.py`, add the `failure_reason` constants/literal type per data-model.md: `sandbox_ready_timeout`, `sandbox_create_failed`, `sandbox_create_error`, `nonzero_exit`, `execution_error` (research.md §7).
- [X] T003 In `backend/app/services/sandbox_call_logging.py`, implement a bounded metrics-fetch helper (e.g. `_fetch_metrics(sandbox)`) that calls `await asyncio.wait_for(sandbox.get_metrics(), timeout=_METRICS_FETCH_TIMEOUT_SECONDS)` with `_METRICS_FETCH_TIMEOUT_SECONDS = 3.0`, catching `TimeoutError`, `opensandbox.exceptions.sandbox.SandboxException`, and generic `Exception`, returning `None` on any failure (FR-007, research.md §5) — include a one-line rationale comment for why synchronous-with-bounded-timeout was chosen over fire-and-forget (`/speckit-clarify` Q2, constitution Principle IV).
- [X] T004 In `backend/app/services/sandbox_call_logging.py`, implement the public `log_sandbox_call(*, usage_type, outcome, user_id=None, failure_reason=None, sandbox=None, execution_complete=None, egress_allow_list=())` function (depends on T002, T003): fetches metrics via T003's helper when `sandbox` is given and `outcome="success"`, derives `tte_ms` from `execution_complete.execution_time_in_millis` when present, builds the field dict per data-model.md (omitting `user_id`/`failure_reason`/CPU-RAM/`tte_ms` keys entirely when not applicable, per FR-002a/FR-007), and emits via the module logger at `info` (success) or `warning` (failure) level with event name `"sandbox_call"` (contracts/sandbox-call-log-event.md).
- [X] T005 [P] Create `backend/tests/test_sandbox_call_logging.py` with unit tests for `log_sandbox_call()`: field presence/omission rules (`user_id` present/absent, `failure_reason` present only on failure, CPU/RAM/`tte_ms` present only when data is available), the bounded-timeout-to-null-metrics fallback (mock `sandbox.get_metrics()` to raise `TimeoutError`/`SandboxException`), and correct log level per outcome.

**Checkpoint**: Foundation ready — `log_sandbox_call()` is fully unit-tested and can now be wired into all three call sites.

---

## Phase 3: User Story 1 - Diagnose resource usage per sandbox call (Priority: P1) 🎯 MVP

**Goal**: Every successful OpenSandbox call (code interpreter, custom JS tool, custom Python tool) emits one `sandbox_call` log record with CPU usage, memory usage, TTE, egress policy, `usage_type`, and `user_id` (when available).

**Independent Test**: Trigger one successful execution per usage type and confirm each produced exactly one structured record with all success-path fields populated (quickstart.md Scenarios 1–2).

### Tests for User Story 1

> Write these first; they must fail until the Implementation tasks below are done.

- [X] T006 [US1] In `backend/tests/test_opensandbox_sandbox_mode.py`, add assertions to the existing `execute_python_opensandbox` success-path test(s): exactly one `sandbox_call` record is logged with `usage_type="code_interpreter"`, `outcome="success"`, and populated `cpu_used_percentage`/`cpu_count`/`memory_used_in_mib`/`memory_total_in_mib`/`tte_ms`/`egress_default_action`/`egress_allow_list` (mock `sandbox.get_metrics()` and the `Execution.complete.execution_time_in_millis` return value); additionally assert `user_id` is present in the record when the call is made with a `user_id`, and absent (not a null placeholder) when called with `user_id=None` (FR-002a).
- [X] T007 [US1] In the same file, add the equivalent success-path assertions for `execute_js_tool_opensandbox` (`usage_type="custom_tool_js"`), including the same `user_id` present/absent coverage as T006.
- [X] T008 [US1] In the same file, add the equivalent success-path assertions for `execute_python_tool_opensandbox` (`usage_type="custom_tool_python"`), including the same `user_id` present/absent coverage as T006.

### Implementation for User Story 1

- [X] T009 [P] [US1] In `backend/app/services/code_interpreter.py`, in `execute_python_opensandbox`: inside the `async with sandbox:` block, immediately after the non-zero-exit-without-stdout check passes (i.e. the sandbox run is confirmed non-failing) and **before** branching into the JSON-parsed-vs-raw-stdout return shapes, call `log_sandbox_call(usage_type="code_interpreter", user_id=user_id, outcome="success", sandbox=sandbox, execution_complete=result.complete, egress_allow_list=settings.opensandbox_network_allow_list)` **exactly once** — same placement as the existing `increment_sandbox_usage()` call in this function; never at each of the two return statements, which would double-log a single call (depends on T004).
- [X] T010 [US1] In `backend/app/services/custom_tool_executor.py`, in `execute_js_tool_opensandbox`: same wiring on its success return path (`usage_type="custom_tool_js"`).
- [X] T011 [US1] In `backend/app/services/custom_tool_executor.py`, in `execute_python_tool_opensandbox`: same wiring on its success return path (`usage_type="custom_tool_python"`) — sequential after T010 (same file).

**Checkpoint**: User Story 1 is fully functional and independently testable — every successful call across all three call sites emits a complete `sandbox_call` record.

---

## Phase 4: User Story 2 - Understand outcomes for failed or errored sandbox calls (Priority: P2)

**Goal**: Failed/errored OpenSandbox calls (readiness timeout, other creation failure, non-zero exit, or any other execution-time exception) also emit exactly one `sandbox_call` record, with an `outcome`/`failure_reason` distinguishing them — including CPU/RAM/TTE when the sandbox actually reached completion (`nonzero_exit`), and omitting them when it never did — and calls rejected before a sandbox exists (the pre-existing quota check) still emit none.

**Independent Test**: Trigger a sandbox-creation failure and a non-zero-exit execution for each call site, and confirm a `sandbox_call` record is still emitted with `outcome="failure"` (quickstart.md Scenario 3).

### Tests for User Story 2

- [X] T012 [US2] In `backend/tests/test_opensandbox_sandbox_mode.py`, extend `execute_python_opensandbox`'s existing failure-path tests: assert a `sandbox_call` record with `outcome="failure"` and the correct `failure_reason` for each branch — `sandbox_ready_timeout` for a `SandboxReadyTimeoutException` at create (no CPU/RAM/`tte_ms` keys present), `sandbox_create_failed` for any other `SandboxException` at create (no CPU/RAM/`tte_ms` keys present), `sandbox_create_error` for a generic `Exception` at create (no CPU/RAM/`tte_ms` keys present), and `nonzero_exit` for a non-zero exit code (CPU/RAM/`tte_ms` keys **present**, mocking `sandbox.get_metrics()`/`result.complete` to return data, since the sandbox did reach completion — `/speckit-analyze` finding I1).
- [X] T013 [US2] In the same file, add the equivalent four failure-branch assertions for `execute_js_tool_opensandbox` (per T012, including CPU/RAM/`tte_ms` presence on `nonzero_exit`).
- [X] T014 [US2] In the same file, add the equivalent four failure-branch assertions for `execute_python_tool_opensandbox` (per T012, including CPU/RAM/`tte_ms` presence on `nonzero_exit`).
- [X] T015 [US2] In the same file, add a new test asserting that an exception raised by `sandbox.commands.run()` itself (currently unhandled) in `execute_js_tool_opensandbox` and `execute_python_tool_opensandbox` still results in exactly one `sandbox_call` record (`failure_reason="execution_error"`) and that the original exception still propagates unchanged (SC-004).
- [X] T016 [US2] In the same file, extend the existing sandbox-call-quota tests (spec `001-sandbox-call-quotas`) for at least one of the three `execute_*_opensandbox` functions: assert that when `check_sandbox_quota()` raises `SandboxQuotaExceededError` (call rejected before `Sandbox.create` is ever attempted), **no** `sandbox_call` record is logged (spec Edge Cases — quota rejections are out of this feature's scope, already covered by the existing quota-usage ledger).

### Implementation for User Story 2

- [X] T017 [US2] In `backend/app/services/code_interpreter.py`, in `execute_python_opensandbox`'s existing `except SandboxException as exc:` block: check `isinstance(exc, SandboxReadyTimeoutException)` (import from `opensandbox.exceptions.sandbox`) first — if true, call `log_sandbox_call(usage_type="code_interpreter", user_id=user_id, outcome="failure", failure_reason="sandbox_ready_timeout", egress_allow_list=settings.opensandbox_network_allow_list)`; otherwise `failure_reason="sandbox_create_failed"` (research.md §7). In the existing generic `Exception` except block, log with `failure_reason="sandbox_create_error"` (neither branch has a `sandbox`/`result` to draw metrics/TTE from). Wrap the `commands.run()` call (plus stdout/stderr extraction) in a `try/except Exception` that logs `failure_reason="execution_error"` (with `sandbox=sandbox`) and re-raises unchanged — this call site has the same previously-unhandled `commands.run()` gap as the two custom-tool functions (research.md §7, updated during implementation). In the existing non-zero-exit-without-stdout branch, call `log_sandbox_call(usage_type="code_interpreter", user_id=user_id, outcome="failure", failure_reason="nonzero_exit", sandbox=sandbox, execution_complete=result.complete, egress_allow_list=settings.opensandbox_network_allow_list)` — the sandbox is still alive and `result.complete` may be populated here, so CPU/RAM/TTE should be captured when available, not omitted (spec US2 Acceptance Scenario 2; `/speckit-analyze` finding I1). Do **not** add any logging around the pre-existing `check_sandbox_quota()`/`SandboxQuotaExceededError` rejection path — it must continue to emit no `sandbox_call` record (T016; spec Edge Cases, FR-011) (depends on T004).
- [X] T018 [US2] In `backend/app/services/custom_tool_executor.py`, in `execute_js_tool_opensandbox`: same `sandbox_ready_timeout`/`sandbox_create_failed`/`sandbox_create_error` wiring as T017 in the two existing creation except blocks (`usage_type="custom_tool_js"`); same `nonzero_exit` wiring passing `sandbox=sandbox, execution_complete=result.complete`; additionally wrap the `async with sandbox:` block's `commands.run()` call and its result-processing in a `try/except Exception` that calls `log_sandbox_call(..., outcome="failure", failure_reason="execution_error", sandbox=sandbox)` (still passing `sandbox` since it's live at that point) and then re-raises the original exception unchanged (research.md §7; include a rationale comment per constitution Principle IV explaining this closes a previously-unlogged gap without changing behavior); leave the `check_sandbox_quota()` rejection path unlogged, same as T017.
- [X] T019 [US2] In `backend/app/services/custom_tool_executor.py`, in `execute_python_tool_opensandbox`: same wiring as T018 (`usage_type="custom_tool_python"`) — sequential after T018 (same file).

**Checkpoint**: User Stories 1 AND 2 both work independently — every call that reaches sandbox creation, success or failure, across all three call sites, emits exactly one `sandbox_call` record; calls rejected earlier by the quota check still emit none.

---

## Phase 5: User Story 3 - Rely on a stable, documented log schema for dashboards (Priority: P3)

**Goal**: The `sandbox_call` field schema is documented in a discoverable location and matches what the code actually emits.

**Independent Test**: Compare the documented field list against a captured `sandbox_call` JSON line from Phase 3/4 testing; they must match exactly (quickstart.md Scenario 5).

### Implementation for User Story 3

- [X] T020 [US3] Add a new `### Sandbox call log fields` subsection under the existing `## OpenSandbox` section in `docs/docs/deployment/configuration.md`, documenting every field from `data-model.md` (name, type, unit, when present/omitted) and a short pointer to `usage_type`'s three values and `failure_reason`'s five values — content sourced from `contracts/sandbox-call-log-event.md`.
- [X] T021 [US3] Manually verify (quickstart.md Scenario 5) that the field names/units documented in T020 exactly match the fields emitted by `log_sandbox_call()` (T004) and asserted in the Phase 3/4 tests — no code change expected if T004/T020 were both done per data-model.md; fix whichever is out of sync if not.

**Checkpoint**: All three user stories are independently functional — the feature is complete.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Final verification across all stories.

- [X] T022 [P] Add an automated test (in `backend/tests/test_sandbox_call_logging.py` or `test_opensandbox_sandbox_mode.py`) asserting that the `sandbox_call` record's JSON key set is byte-for-byte identical across `usage_type="code_interpreter"`, `"custom_tool_js"`, and `"custom_tool_python"` success records (FR-008), rather than relying solely on manual quickstart validation (`/speckit-analyze` finding A2).
- [X] T023 [P] Run `quickstart.md` Scenarios 1–5 end-to-end against a local/dev OpenSandbox instance, confirming success records, failure records, bounded-latency behavior under a simulated slow metrics call, and doc/schema parity.
- [X] T024 Run the full backend test suite (`cd backend && pytest`) to confirm no regression to any `execute_*_opensandbox` function's return values or raised exceptions (SC-004) and that all new/extended tests pass.
- [X] T025 [P] Run backend lint (`ruff check`) on `sandbox_call_logging.py`, `code_interpreter.py`, `custom_tool_executor.py`, and the test files (constitution Principle III).
- [X] T026 Fix findings from a post-implementation `/code-review` pass (research.md §11): correct `execute_python_opensandbox`'s nonzero-exit-with-stdout misclassified as `outcome="success"`; stop attempting a metrics fetch in the `execution_error` branch (removes both the added latency and the cancellation race); extract `log_sandbox_create_failure()` and `log_execution_errors()` shared helpers into `sandbox_call_logging.py` to remove the 3x-copy-pasted classification/wrapper logic; cache `settings.opensandbox_network_allow_list` into a local (`_egress_allow_list`) once per call instead of re-reading the CSV-parsing property up to 6 times; update tests and `data-model.md` to match.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately.
- **Foundational (Phase 2)**: Depends on Setup (T001). BLOCKS all user stories (T004 in particular is a hard dependency for every call-site wiring task).
- **User Story 1 (Phase 3)**: Depends on Foundational (Phase 2). No dependency on US2/US3.
- **User Story 2 (Phase 4)**: Depends on Foundational (Phase 2). Independently testable from US1, but its test edits (T012–T016) land in the same test file as US1's (T006–T008), so run Phase 3 to completion first to avoid merge conflicts within the file.
- **User Story 3 (Phase 5)**: Depends on Foundational (Phase 2) and benefits from US1/US2 being implemented first (T021 verifies docs against actual emitted fields), but the documentation content itself (T020) only needs data-model.md, not working code.
- **Polish (Phase 6)**: Depends on all desired user stories being complete.

### Within Each User Story

- Tests are written before implementation (and must fail first).
- Same-file edits (e.g. both custom-tool functions in `custom_tool_executor.py`, or all three call sites' assertions in `test_opensandbox_sandbox_mode.py`) are sequential, not parallel.

### Parallel Opportunities

- T002 and T005 can run in parallel with each other (different files) once T001 is done; T003/T004 are sequential (same file, T004 depends on T003).
- T009 (`code_interpreter.py`) can run in parallel with T010/T011 (`custom_tool_executor.py`) — different files — but T010 and T011 are sequential with each other.
- T017 (`code_interpreter.py`) can run in parallel with T018/T019 (`custom_tool_executor.py`) — different files — but T018 and T019 are sequential with each other.
- T022, T023, and T025 can run in parallel with each other; T024 should run after all three (it's the final regression check and should see every new test in place).

---

## Parallel Example: User Story 1

```bash
# T009 and T010 touch different files — can run together:
Task: "code_interpreter.py: wire log_sandbox_call() into execute_python_opensandbox's success path"
Task: "custom_tool_executor.py: wire log_sandbox_call() into execute_js_tool_opensandbox's success path"
# T011 must wait for T010 (same file)
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001).
2. Complete Phase 2: Foundational (T002–T005) — critical, blocks everything else.
3. Complete Phase 3: User Story 1 (T006–T011).
4. **STOP and VALIDATE**: Run quickstart.md Scenarios 1–2 independently.
5. This alone already satisfies the issue's primary ask (per-call CPU/RAM/TTE/egress visibility on the happy path).

### Incremental Delivery

1. Setup + Foundational → shared helper ready and unit-tested.
2. Add User Story 1 → validate independently → this is the MVP.
3. Add User Story 2 → failure paths now visible too, quota-rejected calls confirmed silent → validate independently.
4. Add User Story 3 → schema documented and verified → feature complete.
5. Polish → cross-site field-name check, full regression pass, lint.

---

## Notes

- No new dependency, no new database table/migration, no new API endpoint, no frontend change (plan.md Project Structure).
- `[P]` tasks touch different files with no unresolved dependency between them.
- `[Story]` label maps every user-story-phase task back to spec.md's US1/US2/US3 for traceability.
- Commit after each task or logical group; stop at any checkpoint to validate a story independently.
- This revision folds in `/speckit-analyze` findings I1 (nonzero_exit resource capture), U1 (dedicated `sandbox_ready_timeout` failure reason), A1 (single log call in `execute_python_opensandbox`), G1 (quota-rejection no-log coverage, T016), G2 (`user_id` presence/absence coverage in T006–T008), and A2 (automated cross-`usage_type` field-name test, T022).
- **T023 completed against a real OpenSandbox server** (started via `backend/sandbox/opensandbox-docker-start.sh`, `python-sandbox:0.1`/`nodejs-sandbox:0.1` images, Redis for the quota check). Confirmed live: a full success record with every field populated (`custom_tool_js`, `tte_ms=21`, real `cpu_used_percentage`/`cpu_count`/`memory_*` from `sandbox.get_metrics()`); `sandbox_create_failed`/`nonzero_exit` failure records with `user_id` correctly present/omitted; `egress_default_action`/`egress_allow_list` on every record; correct `info`/`warning` log levels; and — most importantly — the corrected stdlib+`ExtraAdder` logging mechanism (T001a) produces well-formed JSON via `setup_logging(log_format="json")`, the exact real-world configuration the original structlog-native approach would have crashed under. One doc-precision fix came out of this: `tte_ms` can be absent even on a `nonzero_exit` failure (an immediate crash before an execution-complete event) — `configuration.md`'s presence column was tightened to say so.
- **Unrelated operational finding (not fixed, out of scope for this feature)**: `python-sandbox:0.1` sandbox creation failed repeatedly in this environment with the existing hardcoded `resource={"cpu": "1", "memory": "128Mi"}` (pre-existing in `code_interpreter.py`/`custom_tool_executor.py`, unchanged by this feature) — root-caused to the memory limit via direct testing (128Mi fails with "Egress sidecar container failed to start"; 512Mi and 2Gi succeed reliably). Worth a separate look if this OpenSandbox test environment is used regularly, but changing sandbox resource limits is unrelated to structured logging and was not touched here.
- **Implementation discovery (T001a, not in the original task list)**: the logging-mechanism decision in research.md §1 was revised during implementation — the originally-planned structlog-native logger crashed once the app's real `setup_logging()` had run anywhere in the process (caught only by running the *full* backend test suite, T024, not just this feature's own tests). Fixed by reverting to the stdlib logger (as originally used at all three call sites) plus one new `structlog.stdlib.ExtraAdder()` processor in `app/core/logging.py`'s foreign pre-chain — see research.md §1 for the full account.
- **Post-implementation `/code-review` fixes (T026, not in the original task list — research.md §11 for the full account)**: a high-effort review after T001–T025 surfaced and fixed (a) `execute_python_opensandbox` misreporting `outcome="success"` for a non-zero exit code that still produced stdout, now decided purely from `result.exit_code` before either return path; (b) the `execution_error` branch at all three call sites attempting a metrics fetch on a sandbox whose connection had just failed, adding up to 3s of latency to an already-failing call — fixed by no longer passing `sandbox=` there, which also closes (c) a cancellation race that could have replaced the original exception during that now-removed await; and (d) the `SandboxReadyTimeoutException` classification and the commands.run() try/except-log-reraise pattern being copy-pasted across all three functions, now factored into two shared helpers, `log_sandbox_create_failure()` and `log_execution_errors()`, in `sandbox_call_logging.py`. `execution_error` records now never carry resource/TTE fields (data-model.md updated); all other fields/behavior unchanged. New tests added: `test_execute_python_opensandbox_logs_nonzero_exit_with_stdout_as_failure` plus unit tests for both new helpers; the three existing `execution_error` tests updated to assert absent (not present) resource fields. Full suite re-verified: same pre-existing baseline failures, zero new regressions.
