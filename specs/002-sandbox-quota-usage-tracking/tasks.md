# Tasks: Sandbox Usage Quota Tracking

**Input**: Design documents from `/specs/002-sandbox-quota-usage-tracking/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md (all present)

**Tests**: This is a backend change, so per this project's constitution
(Principle III: CI-Enforced Quality Gates) test tasks are mandatory, not
optional — unit tests with mocked Redis/OpenSandbox, matching this
codebase's existing convention for these modules.

**Organization**: Tasks are grouped by user story (US1/US2/US3 map to
spec.md's priorities P1/P2/P3) to enable independent implementation and
testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)
- All file paths are relative to the repository root (`backend/...`, `docs/...`)

---

## Phase 1: Setup

**Purpose**: Pre-flight check — this feature adds no new dependencies, no new database table, and no new top-level structure to the existing backend service.

- [X] T001 Confirm local dev prerequisites are reachable (Redis, PostgreSQL, and — if validating against a real OpenSandbox server rather than mocks — `OPENSANDBOX_DOMAIN`/`OPENSANDBOX_API_KEY`), per the Prerequisites section of `specs/002-sandbox-quota-usage-tracking/quickstart.md`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The shared `increment_sandbox_usage` function and its config flag — every user story depends on this existing before any call site can be instrumented.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T002 Add `disable_sandbox_quota_usage: bool = False` field to the `Settings` class in `backend/app/core/config.py`, placed alongside the existing `disable_code_interpreter` / `disable_custom_tools` fields (around line 953) and matching their naming/comment style
- [X] T003 Write failing unit tests in new file `backend/tests/test_sandbox_usage_quota.py` for `increment_sandbox_usage(user_id, period="monthly")` (not yet implemented): (a) increments the `quota_usage` Redis counter for `usage_type="sandbox"`, `model=None`, `agent_id=None`; (b) is a no-op when `settings.disable_sandbox_quota_usage` is `True` (no Redis call made); (c) swallows an exception raised by the Redis client (mock `redis_client()`/`INCR` to raise) and returns normally, logging at `debug` level, per `/speckit-clarify` Q1 and research.md §5
- [X] T004 Implement `increment_sandbox_usage(user_id: uuid.UUID, period: str = "monthly") -> None` in `backend/app/services/quota.py`, alongside `increment_llm_proxy_usage`/`increment_agentic_usage`: build the Redis key via the existing `_usage_key(user_id, "sandbox", None, period_key)` and `_increment` helpers, check `settings.disable_sandbox_quota_usage` first (early return if set), and wrap the increment in `try/except Exception` (log at `debug`, never raise). Include a rationale comment (Constitution Principle IV) explaining why this function deliberately diverges from its two siblings by never raising and taking no `db` parameter (research.md §2, §5). Make T003's tests pass.

**Checkpoint**: `increment_sandbox_usage` exists, is tested, and respects the config flag — user story implementation can now begin.

---

## Phase 3: User Story 1 - Sandbox usage counts toward per-user quota by default (Priority: P1) 🎯 MVP

**Goal**: Every successful OpenSandbox-routed execution (code interpreter, custom-tool JS, custom-tool Python) increments the initiating user's `quota_usage` counter, with `disable_sandbox_quota_usage` left at its default.

**Independent Test**: Trigger a code-interpreter execution and, separately, a custom-tool execution (JS and Python) that each complete successfully, with no configuration changes; confirm the acting user's `quota_usage` row for `usage_type="sandbox"` increases by one for each.

### Tests for User Story 1 ⚠️

> Write these tests FIRST; confirm they fail before starting the implementation tasks below.

- [X] T005 [P] [US1] In `backend/tests/test_opensandbox_sandbox_mode.py`, add tests for `execute_python_opensandbox`: calls `increment_sandbox_usage(user_id)` exactly once on the success path (both the parsed-JSON return and the raw-stdout-fallback return); does **not** call it when `_probe_opensandbox_available()` fails, when `check_sandbox_quota()` raises `SandboxQuotaExceededError`, when sandbox creation raises `SandboxException`/`Exception`, or when `result.exit_code != 0 and not stdout_text`; does not call it when `user_id=None`
- [X] T006 [P] [US1] In `backend/tests/test_opensandbox_sandbox_mode.py`, add tests for `execute_js_tool_opensandbox`: calls `increment_sandbox_usage(user_id)` exactly once immediately before the final `return` statement on success; does **not** call it on any of the function's `RuntimeError`-raising paths (disabled-tools guard, sandbox creation failure, nonzero exit code); does not call it when `user_id=None`
- [X] T007 [P] [US1] In `backend/tests/test_opensandbox_sandbox_mode.py`, add the same coverage as T006 for `execute_python_tool_opensandbox`

### Implementation for User Story 1

- [X] T008 [US1] In `backend/app/services/code_interpreter.py`, add an optional `user_id: uuid.UUID | None = None` keyword parameter to `execute_python_opensandbox`; call `await increment_sandbox_usage(user_id)` (skip if `user_id is None`) immediately after the `if result.exit_code != 0 and not stdout_text:` early-return block and before the subsequent JSON-parsing `try` (research.md §4), so both remaining success return paths are covered. Depends on: T004.
- [X] T009 [US1] In `backend/app/services/code_interpreter.py`, add an optional `user_id: uuid.UUID | None = None` keyword parameter to `execute_python` (the dispatcher at line ~362); pass it through to `execute_python_opensandbox` on the `opensandbox`-mode branch only. Depends on: T008 (same file).
- [X] T010 [US1] In `backend/app/services/builtin_tools/media.py`, in `build_code_interpreter_tool`'s `_execute_code` closure, pass the closure's existing `user_id` (converting the `str` to `uuid.UUID`, matching the conversion pattern already used before calling `increment_agentic_usage` in `app/services/agent_chat.py`) to `execute_python(code, timeout=300, user_id=...)`; skip the conversion/pass-through when `user_id` is falsy. Depends on: T009.
- [X] T011 [US1] In `backend/app/services/custom_tool_executor.py`, add an optional `user_id: uuid.UUID | None = None` keyword parameter to `execute_js_tool_opensandbox`; call `await increment_sandbox_usage(user_id)` (skip if `user_id is None`) immediately before the function's final `return stdout_text or stderr_text or "(no output)"` statement. Depends on: T004.
- [X] T012 [US1] In `backend/app/services/custom_tool_executor.py`, add the same optional `user_id` parameter and the same `increment_sandbox_usage` call (before its final `return`) to `execute_python_tool_opensandbox`. Depends on: T004 (same file as T011, sequential).
- [X] T013 [US1] In `backend/app/services/custom_tool_executor.py`, add an optional `user_id: uuid.UUID | None = None` keyword parameter to `execute_tool_auto` (the unified dispatcher); pass it through to `execute_js_tool_opensandbox` and `execute_python_tool_opensandbox` on their respective `opensandbox`-mode branches only. Depends on: T011, T012 (same file).
- [X] T014 [US1] In `backend/app/services/builtin_tools/loaders.py`, in `load_custom_tools`'s `_run_custom` closure, pass the outer function's existing `user_id` parameter (converted to `uuid.UUID`) to `execute_tool_auto(_code, dict(kwargs), language=_language, timeout=_tout, user_id=...)`; skip the conversion/pass-through when `user_id` is falsy. Depends on: T013.
- [X] T015 [US1] In `backend/app/api/v1/custom_tools.py`, in the "test this tool" handler that calls `execute_tool_auto(...)` (around line 485), pass `user.id` through as the new `user_id` parameter. Depends on: T013.

**Checkpoint**: User Story 1 is fully functional and independently testable — sandbox usage is counted by default across all three execution paths.

---

## Phase 4: User Story 2 - Operators can turn off sandbox usage counting (Priority: P2)

**Goal**: Setting `disable_sandbox_quota_usage=true` stops all three execution paths from incrementing per-user usage, without touching the existing (separate) platform-wide sandbox call limits, and without any database change.

**Independent Test**: With `disable_sandbox_quota_usage=true`, trigger a successful code-interpreter execution and a successful custom-tool execution; confirm neither changes the acting user's `quota_usage` totals, while the existing platform-wide `SANDBOX_QUOTA_LIMITS` enforcement (from the sibling `001-sandbox-call-quotas` feature) still triggers exactly as before.

### Tests for User Story 2 ⚠️

- [X] T016 [P] [US2] In `backend/tests/test_opensandbox_sandbox_mode.py`, add an end-to-end-through-the-call-site test (not just the Foundational unit test of `increment_sandbox_usage` in isolation): with `settings.disable_sandbox_quota_usage=True`, calling `execute_python_opensandbox` (mocked sandbox, successful run) does not create or increment any `quota:usage:*sandbox*` Redis key
- [X] T017 [P] [US2] In `backend/tests/test_sandbox_quota.py`, add a test confirming `check_sandbox_quota()`/`record_sandbox_call()` (the existing platform-wide sandbox call limit machinery) behave identically regardless of `settings.disable_sandbox_quota_usage`'s value — proving the two mechanisms are independent (spec FR-009 / SC-004)

### Implementation for User Story 2

- [X] T018 [US2] Add `disable_sandbox_quota_usage` to the "Security feature flags" table in `docs/docs/deployment/configuration.md`, matching the existing `disable_code_interpreter`/`disable_custom_tools` rows in the same table (default `false`, restart required to change, per `/speckit-clarify` Q2)

**Checkpoint**: User Stories 1 AND 2 both work independently — the flag is respected end-to-end and doesn't disturb the existing platform-wide sandbox quota.

*(No further implementation task is needed beyond T002/T004 from the Foundational phase — the flag check already lives inside `increment_sandbox_usage`, a single choke point per research.md §6, so no per-call-site flag-checking code is required.)*

---

## Phase 5: User Story 3 - Sandbox usage is reported consistently with existing usage types (Priority: P3)

**Goal**: Sandbox usage is recorded as its own distinguishable `usage_type="sandbox"` row, never merged with or overwriting `llm_proxy`/`agentic` rows for the same user/period, and no row is created for users with no sandbox usage.

**Independent Test**: For a user with existing `llm_proxy` and/or `agentic` usage in the current period, trigger one successful sandbox execution and confirm three independent, separately-countable rows exist; for a user who has never used the sandbox, confirm no `"sandbox"` row exists.

### Tests for User Story 3 ⚠️

- [X] T019 [P] [US3] In `backend/tests/test_sandbox_usage_quota.py`, add a test that seeds existing `llm_proxy` and `agentic` usage for a user (via `increment_llm_proxy_usage`/`increment_agentic_usage`), then calls `increment_sandbox_usage` for the same user/period, and asserts all three `usage_type` counters (`llm_proxy`, `agentic`, `sandbox`) read back independently and correctly via `get_user_quota_usage`-style Redis reads (or direct `_get_user_count` calls) — none overwritten or merged
- [X] T020 [P] [US3] In `backend/tests/test_sandbox_usage_quota.py`, add a test confirming that a user who never triggers a sandbox execution has a zero/absent `"sandbox"` counter (no row created), consistent with how `llm_proxy`/`agentic` counters already behave for unused types

**Checkpoint**: All three user stories are independently functional. No implementation tasks are needed for this story beyond what Phases 2–4 already built — reuse of the existing `quota_usage` table's per-`usage_type` distinctness already satisfies it by construction (research.md §1, §7); note the follow-up in Phase 6 (T022) about the display-layer gap this story's spec explicitly left out of scope.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Documentation/traceability follow-ups that span the whole feature.

- [X] T021 [P] Review `backend/app/services/quota.py`, `backend/app/services/code_interpreter.py`, and `backend/app/services/custom_tool_executor.py` for Constitution Principle IV "why" rationale comments on the two non-obvious decisions from research.md: (1) why `increment_sandbox_usage` never raises and takes no `db` parameter (research.md §2, §5), and (2) why `user_id` is threaded into the three `execute_*_opensandbox` functions themselves rather than called from their simpler-looking outer closures (research.md §3)
- [X] T022 [P] Add a short code comment near `get_user_quota_usage` in `backend/app/services/quota.py` (its hardcoded `for qt in ("llm_proxy", "agentic"):` loop) and near `usage_types` in `backend/app/api/v1/metrics.py`, noting that `"sandbox"` is deliberately not yet included in these display surfaces (research.md §7) — so a future contributor doesn't mistake the omission for an oversight, and has a concrete pointer for the follow-up
- [X] T023 Run through all 5 scenarios in `specs/002-sandbox-quota-usage-tracking/quickstart.md` end-to-end (against a local OpenSandbox-backed dev environment, or the mocked unit-test scenario for Scenario 4) to confirm the full feature works as specified
- [X] T024 [P] Extend `backend/tests/test_quota_flush.py` with a case that seeds a `usage_type="sandbox"` Redis counter and asserts `flush_quota_usage()` writes the corresponding `quota_usage` row — confirms research.md §1's claim that the flush job needs no changes for the new usage type (it derives `usage_type` generically from the Redis key, not a hardcoded list)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — can start immediately
- **Foundational (Phase 2)**: No dependencies on Setup completing (T001 is just an environment check) — but BLOCKS all user stories (T005–T020 all depend transitively on T004)
- **User Stories (Phase 3+)**: All depend on Foundational (Phase 2) completion
  - US1 (Phase 3) has no dependency on US2/US3
  - US2 (Phase 4) depends on US1's call-site changes existing (T008–T015) so there's something for the flag to gate end-to-end (T016); T017 depends only on Foundational
  - US3 (Phase 5) depends on US1's call-site changes existing (so a sandbox execution can actually be triggered/simulated) but tests the *ledger*, not the call sites themselves
- **Polish (Phase 6)**: Depends on all three user stories being complete

### Within Each User Story

- Tests are written first and must fail before the corresponding implementation task
- Within US1, tasks T008→T009→T010 (code interpreter chain) and T011→T012→T013→T014/T015 (custom tool chain) are each a strict same-file/same-chain sequence; the two chains are independent of each other and can run in parallel

### Parallel Opportunities

- T005, T006, T007 (all in the same test file but covering independent functions) can be drafted in parallel by different contributors, then merged
- The code-interpreter chain (T008–T010) and the custom-tool chain (T011–T015) touch entirely different files and can proceed in parallel once T004 is done
- T016 and T017 (US2) are independent test files and can run in parallel
- T019 and T020 (US3) are in the same file but independent test cases
- T021 and T022 (Polish) are independent review/documentation tasks

---

## Parallel Example: User Story 1

```bash
# After T004 (Foundational) is done, these two chains can proceed in parallel:

# Chain A — code interpreter:
Task: "T008 Add user_id param + increment call to execute_python_opensandbox in backend/app/services/code_interpreter.py"
Task: "T009 Add user_id param to execute_python in backend/app/services/code_interpreter.py"
Task: "T010 Thread user_id through media.py's _execute_code closure"

# Chain B — custom tools:
Task: "T011 Add user_id param + increment call to execute_js_tool_opensandbox in backend/app/services/custom_tool_executor.py"
Task: "T012 Add user_id param + increment call to execute_python_tool_opensandbox in backend/app/services/custom_tool_executor.py"
Task: "T013 Add user_id param to execute_tool_auto in backend/app/services/custom_tool_executor.py"
Task: "T014 Thread user_id through loaders.py's _run_custom closure"
Task: "T015 Thread user.id through custom_tools.py's test-tool handler"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001)
2. Complete Phase 2: Foundational (T002–T004) — CRITICAL, blocks everything else
3. Complete Phase 3: User Story 1 (T005–T015)
4. **STOP and VALIDATE**: Run quickstart.md Scenarios 1, 2, and 4 independently
5. This alone delivers the feature's core value: sandbox usage counted by default

### Incremental Delivery

1. Setup + Foundational → shared `increment_sandbox_usage` ready
2. Add User Story 1 → validate independently → this is the MVP
3. Add User Story 2 → validate the opt-out independently (quickstart Scenario 3)
4. Add User Story 3 → validate ledger distinctness independently (quickstart Scenario 5)
5. Polish (T021–T024) — documentation and full end-to-end pass

### Parallel Team Strategy

With multiple developers, once Foundational (T002–T004) is done:

- Developer A: User Story 1, code-interpreter chain (T005, T008–T010)
- Developer B: User Story 1, custom-tool chain (T006, T007, T011–T015)
- Developer C: User Story 2 (T016–T018) and User Story 3 (T019–T020), once Developer A/B's call-site changes land

---

## Notes

- [P] tasks = different files (or independent test cases within one file), no blocking dependency
- [Story] label maps task to specific user story for traceability
- No new database table or migration — `usage_type` is an existing free-text column (data-model.md)
- Commit after each task or logical group
- Stop at any checkpoint to validate a story independently before moving to the next

---

## Phase 7: FR-011 — Sandbox Usage Period Resolution (Parity with LLM-proxy/Agentic)

**Added 2026-07-31** by a `/speckit-plan` re-run reconciling this file with
spec.md's FR-011 (added by a later `/speckit-clarify` session, after Phases
1–6 above were already implemented and verified — see research.md §2 for
the full technical finding). No `[Story]` label: like Foundational, this is
cross-cutting infrastructure feeding User Story 1's existing "usage total
for the current period" behavior, not a new user-facing story of its own.

**Goal**: Sandbox usage periods resolve with the same user > group >
platform > monthly-default precedence LLM-proxy/agentic usage already gets,
instead of always being fixed at monthly — including making the
platform-wide tier actually reachable via the existing `PUT /quotas` admin
endpoint (today it would be silently dropped).

**Independent Test**: Configure a user's (or their group's, or the
platform's) quota config with a `sandbox` period other than monthly;
trigger a successful sandbox execution; confirm the recorded `quota_usage`
row uses that period's key instead of the monthly default, and that the
configured value is visible when reading that config back via the existing
API.

### Tests for Phase 7 ⚠️

> Write these tests FIRST; confirm they fail before starting the implementation tasks below.

- [X] T025 [P] In `backend/tests/test_sandbox_usage_quota.py`, add tests for `increment_sandbox_usage`'s new `db` parameter: resolves the period via `resolve_quota_config(db, redis, user_id, "sandbox")` when a user- or group-level config exists; falls back to `"monthly"` when `db` is `None` or nothing resolves for any tier
- [X] T026 [P] In `backend/tests/test_sandbox_usage_quota.py` (corrected during implementation: `test_quota_usage_display.py` only tests `get_user_quota_usage`'s consumption of a *mocked-away* `resolve_quota_config` — confirmed by repo search that no file anywhere exercises `resolve_quota_config`'s own precedence logic directly), add tests confirming `"sandbox"` resolves correctly at each precedence tier — user-level, group-level, platform/global-level, and none-configured (falls through to no config, i.e. `None`) — and that adding `"sandbox"` recognition does not change `"llm_proxy"`/`"agentic"` resolution behavior
- [X] T027 [P] In new file `backend/tests/test_quota_config_schema.py` (no existing test covers `QuotaConfig`/`PUT /quotas` directly — confirmed by repo search), add tests: a `sandbox` field sent in a `PUT /quotas` request body is persisted (not silently dropped); a `sandbox` key present in a user's raw `quota_config` override is visible (not silently stripped) in the user quota-info response and the platform `GET /quotas` response

### Implementation for Phase 7

- [X] T028 [P] Add `sandbox: QuotaTypeConfig | None = None` field to `QuotaConfig` in `backend/app/api/v1/schemas/settings.py`, with a rationale comment (Constitution Principle IV) noting that only `.period` has any effect for `"sandbox"` — `.platform_limit`/`.default`/`.per_model` are accepted and stored but inert, since no `check_sandbox_*` enforcement function reads them (this feature adds no enforceable sandbox quota tier). Makes the response-visibility half of T027 pass.
- [X] T029 [P] Extend `resolve_quota_config` in `backend/app/services/quota.py` (the user-level loop, group-level loop, global-level loop, and their `not all(...)` guards — 4 tuple occurrences within the function) to include `"sandbox"` alongside `"llm_proxy"`/`"agentic"`. Do **not** touch the XTM-license agentic-ceiling block (`if settings.is_xtm_one: ...`) — that block is agentic-only and has no sandbox equivalent. Makes T026 pass and the persistence half of T027 pass. Independent of T028 (different file).
- [X] T030 In `backend/app/services/quota.py`, add `db: AsyncSession | None = None` parameter to `increment_sandbox_usage`; when provided, call `resolve_quota_config(db, redis, user_id, "sandbox")` and use `config.get("period", period)` if a config resolves — modeled on `increment_llm_proxy_usage`'s period-resolution shape specifically, not `increment_agentic_usage`'s (sandbox has no global/license-ceiling counter to maintain). Makes T025 pass. Depends on: T029.
- [X] T031 In `backend/app/services/code_interpreter.py`, add `db: AsyncSession | None = None` parameter to `execute_python_opensandbox`; pass it through to `increment_sandbox_usage(user_id, db=db)` at its existing call point. Depends on: T030.
- [X] T032 In `backend/app/services/code_interpreter.py`, add `db: AsyncSession | None = None` parameter to `execute_python` (the dispatcher); pass it through to `execute_python_opensandbox` on the `opensandbox`-mode branch only. Depends on: T031 (same file).
- [X] T033 In `backend/app/services/builtin_tools/media.py`, in `build_code_interpreter_tool`'s `_execute_code` closure, pass the closure's already-available `db` parameter through to `execute_python(code, timeout=300, user_id=..., db=db)`. Depends on: T032.
- [X] T034 [P] In `backend/app/services/custom_tool_executor.py`, add `db: AsyncSession | None = None` parameter to `execute_js_tool_opensandbox` and `execute_python_tool_opensandbox`; pass it through to `increment_sandbox_usage(user_id, db=db)` at each of their existing call points. Depends on: T030 (independent of T031–T033, different file).
- [X] T035 In `backend/app/services/custom_tool_executor.py`, add `db: AsyncSession | None = None` parameter to `execute_tool_auto` and `execute_js_tool_auto` (the JS-only legacy dispatcher); pass it through on their respective `opensandbox`-mode branches only. Depends on: T034 (same file).
- [X] T036 In `backend/app/services/builtin_tools/loaders.py`, in `load_custom_tools`'s `_run_custom` closure, pass the closure's already-available `db` parameter through to `execute_tool_auto(..., db=db)`. Depends on: T035.
- [X] T037 In `backend/app/api/v1/custom_tools.py`, in the "test this tool" handler, pass its already-available `db` (from `Depends(get_db)`) through to its existing `execute_tool_auto(..., db=db)` call. Depends on: T035.

**Checkpoint**: Sandbox usage periods resolve with the same precedence as LLM-proxy/agentic usage (FR-011); the platform-wide tier is reachable via the existing `PUT /quotas` admin endpoint; T001–T024 remain unaffected and still pass.

---

## Phase 8: Polish (FR-011 delta)

- [X] T038 [P] Review `backend/app/services/quota.py` and `backend/app/api/v1/schemas/settings.py` for Constitution Principle IV rationale comments: (1) why `increment_sandbox_usage` mirrors `increment_llm_proxy_usage`'s period-resolution shape rather than `increment_agentic_usage`'s (no global/ceiling counter — sandbox has no enforceable cap), and (2) why `QuotaConfig.sandbox`'s non-`period` fields are inert
- [X] T039 Run quickstart.md Scenario 6 end-to-end (or its mocked unit-test equivalent) to confirm period resolution works as specified — including that a configured sandbox period is visible via `GET` responses, not just written correctly

### Phase 7–8 Dependencies

- Phase 7 depends only on Phases 1–6 already being complete (specifically T004's `increment_sandbox_usage` and T008–T015's call-site instrumentation existing) — it does not depend on Phase 7/8 being done in the original priority order relative to US2/US3, since it extends the already-shipped mechanism rather than adding a new one.
- Within Phase 7: T028 and T029 are independent (different files) and can run in parallel; T030 depends on T029; the code-interpreter chain (T031→T032→T033) and the custom-tool chain (T034→T035→T036/T037) are independent of each other once T030 is done and can proceed in parallel.
- Phase 8 depends on Phase 7 being complete.

---

## Phase 9: FR-012 — Sandbox Usage in Prometheus /metrics

**Added 2026-07-31** by a `/speckit-plan` re-run reconciling this file with spec.md's FR-012 (added by a `/speckit-clarify` session run after Phase 7/8 were already implemented — research.md §9). No `[Story]` label, same reasoning as Phase 7.

**Goal**: Sandbox usage totals appear in the existing Prometheus `/metrics` endpoint's output, unconditionally across all platform modes — not gated by `platform_mode` the way `llm_proxy` is on that same endpoint.

**Independent Test**: `GET /metrics` with valid Prometheus auth in both `xtm_one` and non-`xtm_one` `platform_mode`; confirm `xtmone_quota_usage` series with `usage_type="sandbox"` appear in both, alongside `usage_type="agentic"`.

### Tests for Phase 9 ⚠️

- [X] T040 In new file `backend/tests/test_prometheus_metrics.py` (no existing test covers `app/api/v1/metrics.py`'s `usage_types`/`_read_quota_total` logic at all — confirmed by repo search), add tests confirming the `metrics()` endpoint's `usage_types` list includes `"sandbox"` in both `xtm_one` and non-`xtm_one` `platform_mode` (unlike `"llm_proxy"`, which only appears in the latter)

### Implementation for Phase 9

- [X] T041 In `backend/app/api/v1/metrics.py`, add `"sandbox"` to the `usage_types` list inside the `metrics()` endpoint handler, unconditionally — outside the `if not settings.is_xtm_one:` guard, alongside `"agentic"`. No change needed to `_read_quota_total` or `QUOTA_GAUGE` (both already fully generic per `usage_type`). Makes T040 pass. Depends on: none (independent of Phase 7/8's files).

**Checkpoint**: Sandbox usage is visible in `/metrics` in every platform mode (FR-012); Phases 1–8 remain unaffected and still pass.

---

## Phase 10: FR-013/FR-014 — Sandbox Usage Displayed on Existing Usage-Review Pages

**Added 2026-07-31** by a further `/speckit-plan` re-run reconciling this file with spec.md's FR-013/FR-014 (added by a `/speckit-clarify` session run after Phase 9 was already implemented — research.md §10). No `[Story]` label, same reasoning as Phase 7/9 — this extends User Story 3's existing display requirement rather than adding a new story. This is the largest single increment appended to this file so far: **three** backend read functions (not one) and **four** frontend files, discovered by tracing each of FR-013's named pages back to its actual data source.

**Goal**: A sandbox usage entry is visibly displayed on every existing page that already displays `agentic`/`llm_proxy` usage (profile, user detail, group detail, platform quota settings) — always showing the usage count, never implying an enforced limit that doesn't exist (FR-014), since sandbox has no configurable enforceable limit.

**Independent Test**: As a user with recorded sandbox usage, view each of the four pages; confirm a "Sandbox Executions" entry appears on all of them, showing a plain usage count with no percentage-toward-limit indicator anywhere, even where an admin has set the (inert) `sandbox.default`/`platform_limit` config fields.

### Tests for Phase 10 ⚠️

> Backend only — see the note on frontend testing below (no component-test or e2e convention exists for these four pages in this codebase, confirmed by repo search; frontend verification relies on type-checking/lint plus manual verification, T054/T055).

- [X] T042 [P] In `backend/tests/test_quota_usage_display.py`, add tests confirming `get_user_quota_usage` returns a `"sandbox"` entry (count `0` when unused, correct count and period key when used) alongside the existing `"llm_proxy"`/`"agentic"` entries — matching the existing zero-count-entry behavior already proven for those two types
- [X] T043 [P] In `backend/tests/test_quota_usage_display.py`, add tests confirming `resolve_full_quota_info`'s `sources` dict resolves `"sandbox"` correctly at each precedence tier (user, group, global, none-configured), mirroring T026's `resolve_quota_config` precedence tests, and that `"llm_proxy"`/`"agentic"` sources are unaffected
- [X] T044 [P] In `backend/tests/test_quota_usage_display.py`, add tests confirming `get_platform_quota_monitoring`'s response includes a `sandbox` summary entry (`limit=None`, `is_global=True`, correct `current_usage`/`active_users`) unconditionally — present even when no `sandbox` config exists anywhere (unlike `"agentic"`/`"llm_proxy"`, whose summary rows are conditionally emitted based on configured limits)

(T042–T044 are in the same file but cover independent functions — can be drafted in parallel by different contributors, then merged, same convention as T005–T007 in Phase 3.)

### Implementation for Phase 10

- [X] T045 In `backend/app/services/quota.py`, change `get_user_quota_usage`'s loop from `for qt in ("llm_proxy", "agentic"):` to `for qt in _QUOTA_TYPES:` (the shared constant from T029). Makes T042 pass.
- [X] T046 In `backend/app/services/quota.py`, change `resolve_full_quota_info`'s main loop from `for qt in ("llm_proxy", "agentic"):` to `for qt in _QUOTA_TYPES:`. Do **not** touch the XTM-license agentic-ceiling `elif` branch — it is agentic-only and has no sandbox equivalent. Makes T043 pass. Depends on: T045 (same file, sequential).
- [X] T047 In `backend/app/services/quota.py`, in `get_platform_quota_monitoring`, after the existing `quotas_summary` construction (the `if/elif/else` branches handling XTM-license and platform-config-driven rows), unconditionally append `await _scan_period("sandbox", "monthly", None, is_global=True)` to `quotas_summary` — do **not** fold `"sandbox"` into the existing `for qt in ("agentic", "llm_proxy"):` branch, which assumes a limit-bearing type (research.md §10.1/§10.3). Makes T044 pass. Depends on: T046 (same file, sequential).
- [X] T048 [P] In `frontend/src/pages/Profile.tsx`, add `{ key: "sandbox", label: "Sandbox Executions", icon, color, barColor }` to the `types` array, unconditionally (no `isXtmOne` gating). In the render loop, for `key === "sandbox"`: (1) never take the `source === "none"` → "No quota configured" branch — always render the count-display branch instead; (2) force `hasLimit = false` unconditionally, ignoring `cfg?.default` entirely for this key. Both overrides are required together (research.md §10.2 — a first design pass that only applied one left the card broken for the common no-config case). Depends on: T045/T046 (backend data must exist for this to be meaningful, though the frontend edit itself can be drafted in parallel).
- [X] T049 [P] In `frontend/src/pages/UserDetail.tsx`, apply the same two overrides as T048 (independent file from T048, can run in parallel).
- [X] T050 [P] In `frontend/src/pages/GroupDetail.tsx`, add `{ key: "sandbox", label: "Sandbox Executions", icon, color }` to `QUOTA_TYPES`, unconditionally. Render read-only: "Configured" badge + period text when `group.quota_config?.sandbox?.period` is set, "Not set" otherwise — no limit text, no `QuotaEditor` input for sandbox (editable period input for user/group-level sandbox config is explicitly out of scope for this plan — research.md §10.2/§10.3).
- [X] T051 [P] In `frontend/src/pages/settings/QuotasSection.tsx`, add a `sandbox` entry to `QUOTA_TYPE_META` (label "Sandbox Executions", a new icon/color not already used by `agentic`/`llm_proxy`). No change needed to `QuotaProgressCard`/`RadialGauge` — both already render a limit-less summary row correctly (icon instead of %, raw count, "No platform limit" text), verified by inspection during planning (research.md §10.2). Depends on: T047 (the summary row this renders).

**Checkpoint**: Sandbox usage is visibly displayed on all four existing usage-review pages (FR-013), never implying an enforced limit (FR-014); Phases 1–9 remain unaffected and still pass.

---

## Phase 11: Polish (FR-012/FR-013/FR-014 delta)

- [X] T052 [P] Review `backend/app/api/v1/metrics.py` for a Constitution Principle IV rationale comment on why `"sandbox"` is unconditional in `usage_types` while `"llm_proxy"` is mode-gated (research.md §9)
- [X] T053 [P] Review `backend/app/services/quota.py` for a Constitution Principle IV rationale comment on why `get_platform_quota_monitoring` appends an unconditional `_scan_period` call for `"sandbox"` rather than routing it through the existing `platform_cfg.get(qt)`-gated branch (research.md §10.1/§10.3)
- [X] T054 [P] Review the four frontend files (`Profile.tsx`, `UserDetail.tsx`, `GroupDetail.tsx`, `settings/QuotasSection.tsx`) for inline comments explaining why the `"sandbox"` entries force "always show count" + "no limit" behavior, so a future contributor doesn't "fix" them back to matching `llm_proxy`/`agentic`'s behavior
- [X] T055 Run `tsc --noEmit` and `eslint` on `frontend/` to confirm the four modified files pass type-checking/lint — the actual CI bar for this frontend change (no new automated frontend tests exist for these pages, confirmed by repo search during planning)
- [X] T056 Run quickstart.md Scenario 7 (Prometheus `/metrics`) and Scenario 8 (all four usage-review pages) end-to-end — Scenario 8 requires manual verification in a running dev instance, since no automated frontend test covers these pages

### Phase 9–11 Dependencies

- Phase 9 depends only on Phases 1–8 already being complete; independent of Phase 10/11's files.
- Phase 10 depends on Phase 9 being complete (shares `_QUOTA_TYPES` and the general "extend the read side" pattern, though not a hard file dependency). Within Phase 10: T045→T046→T047 are a strict same-file sequence (all in `quota.py`); T048–T051 are four independent frontend files and can all proceed in parallel once the backend data they display exists (T045–T047).
- Phase 11 depends on Phase 10 being complete.

---

## Phase 12: FR-013/FR-014 v2 — Fold Sandbox Usage Into the Displayed Agentic Count (delta)

**Added 2026-07-31** by a fifth `/speckit-clarify` session, run after Phase 10/11 had already shipped a dedicated "Sandbox Executions" row on all four pages. That design is now reversed: sandbox usage must no longer appear as its own identifiable row anywhere in the usage UI. Instead, wherever the UI already displays a usage *count* for agentic, that number must include sandbox usage added in. Group detail's sandbox row (a read-only config/period indicator, not a usage count) is removed outright — there is nothing to merge it into. This phase only touches frontend display code: the ledger, `resolve_quota_config`/`get_user_quota_usage`/`resolve_full_quota_info`/`get_platform_quota_monitoring` (Phase 10's backend work), and the Prometheus `/metrics` endpoint (Phase 9) all keep sandbox and agentic fully distinct and are unaffected by this phase.

**Goal**: No page shows a separate "Sandbox Executions" row; Profile, User Detail, and the platform Quotas settings page show sandbox usage folded into the existing "Agentic Executions" number; Group Detail shows no sandbox-related row at all.

**Independent Test**: As a user with recorded sandbox usage, view Profile/User Detail/platform Quotas settings and confirm the "Agentic Executions" count equals (agentic count + sandbox count), with no separate sandbox card/tile visible anywhere, including Group Detail.

- [X] T057 [P] In `frontend/src/pages/Profile.tsx`, remove the `sandbox` entry from the `types` array and the two `key === "sandbox"` overrides added in T048. Instead, when `key === "agentic"`, add the user's `usage_type === "sandbox"` total-entry count into the displayed `current` value. Revert everything else in this card's render loop to its pre-Phase-10 shape.
- [X] T058 [P] In `frontend/src/pages/UserDetail.tsx`, apply the same change as T057 (independent file, can run in parallel).
- [X] T059 [P] In `frontend/src/pages/GroupDetail.tsx`, remove the `sandbox` entry from `QUOTA_TYPES` and all `key === "sandbox"` branches added in T050 — fully revert this file to its pre-Phase-10 shape (no sandbox row, no merge, since group detail never showed a usage count for sandbox to merge).
- [X] T060 [P] In `frontend/src/pages/settings/QuotasSection.tsx`, remove the `sandbox` entry from `QUOTA_TYPE_META` and the standalone `sandboxGauge` card added in T051. Instead, fold sandbox's platform-wide (monthly-only) current usage into the displayed `agenticGauge.current_usage`, and into the `"monthly"` entry of `agenticQuotas` (used by the XTM multi-gauge daily/monthly/yearly layout) — daily/yearly entries are left untouched since sandbox has no per-period breakdown to add to them.
- [X] T061 Run `tsc --noEmit` and `eslint` on `frontend/` to confirm the four modified files pass type-checking/lint. Depends on: T057–T060.
- [X] T062 Manually re-verify quickstart.md Scenario 8 against the new behavior (no automated frontend test covers these pages) — requires a running dev instance.

### Phase 12 Dependencies

- T057–T060 are four independent frontend files and can proceed in parallel; no backend changes in this phase.
- T061 depends on T057–T060 all being complete.

---

## Phase 13: FR-015–FR-019 — Combined Agentic+Sandbox Enforcement (delta)

**Added 2026-08-03** by a fifth `/speckit-clarify` session, reversing the *enforcement* half of Phase 12's premise: Phase 12's display merge (still correct, still unchanged) explicitly kept "agentic's own enforced limit/blocking behavior" using agentic-only usage. That boundary is now deliberately removed: agentic's per-user enforced limit must count sandbox usage too, sharing one ceiling — no new limit type (research.md §11). No `[Story]` label, same reasoning as Phase 7/9/10/12 — this is cross-cutting enforcement infrastructure, not a new user-facing story.

**Goal**: `check_agentic_quota`'s per-user branch compares `agentic_count + sandbox_count` (both under agentic's own resolved period) against the existing agentic limit — automatically covering all four existing callers (`agent_chat.py`, `assignment_engine.py`, `extension.py`, `platform.py`, research.md §11.1) with zero changes to any of them — plus a brand-new per-user check at the three sandbox-dispatch call sites, which today enforce only the platform-wide cap.

**Independent Test**: Configure a user's agentic limit to 5; drive 3 sandbox executions then 2 agentic actions (chat turns or assignment runs) — both succeed, combined usage now at 5; the 6th action of either kind (agentic OR sandbox) is refused. Repeat with the user's agentic and sandbox periods configured to differ (e.g. agentic daily, sandbox monthly) — behavior is unaffected, since the combined check always uses agentic's period window (see quickstart.md Scenario 9 for the full 8-step version, including fail-mode).

### Tests for Phase 13 ⚠️

> Write these tests FIRST; confirm they fail before starting the implementation tasks below.

- [X] T063 [P] In new file `backend/tests/test_agentic_sandbox_combined_quota.py` (no existing file unit-tests `check_agentic_quota`'s limit comparison in isolation — confirmed by repo search), add tests for the Copilot-mode branch: `check_agentic_quota` raises `QuotaExceededError` once `agentic_count + sandbox_count >= default_limit`, even when `agentic_count` alone is under the limit; does not raise when the sum is under; the platform-wide hard-cap check (`_check_platform_wide_limit`) earlier in the function is unaffected by a large sandbox count for one user
- [X] T064 [P] In the same new file, add the equivalent tests for the XTM One-mode branch (the per-user comparison after the XTM-ceiling loop): same combined-sum behavior; the XTM-ceiling loop itself (platform-wide, iterates `get_xtm_agentic_ceilings`) is unaffected by sandbox usage
- [X] T065 [P] In the same new file, add a test proving the sandbox term is read under **agentic's** resolved `period_key`, not sandbox's own independently-resolved one (spec FR-017): mock `resolve_quota_config` to return `{"period": "daily"}` for `quota_type="agentic"` and `{"period": "monthly"}` for `quota_type="sandbox"` for the same user, seed a sandbox counter only under the daily period key, and assert `check_agentic_quota` sees it (i.e. does NOT look it up under the monthly key)
- [X] T066 [P] In `backend/tests/test_opensandbox_sandbox_mode.py`, add tests for `execute_python_opensandbox`'s new pre-dispatch check: when `check_agentic_quota` raises `QuotaExceededError` (mocked), the function returns its existing error-dict shape (`{"stdout": "", "stderr": "", "error": ..., "images": [], "output_files": []}`) using the new `QUOTA_EXCEEDED_MESSAGE` constant, and `record_sandbox_call()` is **not** called for that refused attempt; the check is skipped entirely when `user_id=None`; the check runs strictly after `check_sandbox_quota()` (a platform-wide-cap refusal short-circuits before this new check ever runs)
- [X] T067 [P] In the same file, add the equivalent tests for `execute_js_tool_opensandbox`: on `QuotaExceededError`, the function raises `RuntimeError(QUOTA_EXCEEDED_MESSAGE)` (matching its own existing `SandboxQuotaExceededError`-handling shape, not a dict); same `user_id=None` skip and call-ordering assertions as T066
- [X] T068 [P] In the same file, add the equivalent tests for `execute_python_tool_opensandbox` (same shape/assertions as T067)
- [X] T069 In the same file, add a concurrency regression test: two simultaneous calls (via `asyncio.gather`) to one of the three functions sharing a single mocked `AsyncSession` that raises if invoked while another operation on it is in flight; assert both calls complete without a "concurrent operations are not permitted"-class error — proving the new check opens its own dedicated session (`async_session()`) rather than reusing the shared `db`, the same hazard/fix pattern as `increment_sandbox_usage`'s existing concurrency handling (research.md §11.4)
- [X] T070 [P] In `backend/tests/test_fail_modes.py`, add a case for the new sandbox-dispatch checkpoint: with the quota backend unreachable (mocked), `settings.quota_fail_mode="open"` lets dispatch proceed to the sandbox call; `settings.quota_fail_mode="closed"` refuses it (via that function's own shape); flipping only `settings.sandbox_quota_fail_mode` has no effect on this checkpoint either way (FR-019)

(T063–T065 are in one new file but independent test cases; T066–T068 are in one existing file but cover independent functions — both groups follow this file's established convention, e.g. T005–T007/T042–T044, of drafting in parallel then merging.)

### Implementation for Phase 13

- [X] T071 In `backend/app/services/quota.py`, add a `QUOTA_EXCEEDED_MESSAGE` constant near the existing `QUOTA_UNAVAILABLE_MESSAGE` (around line 51), with the same wording already used by `agent_chat.py`'s local constant of the same name (a new, separate constant for the three sandbox call sites to import — not a refactor of the existing one). Depends on: none.
- [X] T072 In `backend/app/services/quota.py`, modify `check_agentic_quota`'s per-user branch in **both** the XTM One code path (~line 641-654) and the Copilot code path (~line 659-669): after computing `current = await _get_user_count(redis, user_id, "agentic", None, period_key)`, add `sandbox_current = await _get_user_count(redis, user_id, "sandbox", None, period_key)` and change the comparison/raise to `if current + sandbox_current >= default_limit: raise QuotaExceededError("agentic", default_limit, current + sandbox_current, period)`. Add a Constitution Principle IV rationale comment explaining why the sandbox lookup reuses `period_key` (agentic's resolved period) rather than re-resolving sandbox's own independently-configured one (spec FR-017 — deliberate, not an oversight). Do not touch the platform-wide/XTM-ceiling blocks earlier in the function. Makes T063/T064/T065 pass. Depends on: none (single file, no dependency on T071 — different constant, same file, can be done in either order).
- [X] T073 In `backend/app/services/code_interpreter.py`, in `execute_python_opensandbox`, add a new pre-dispatch check immediately after the existing `check_sandbox_quota()` try/except block and before `await record_sandbox_call()`, only when `user_id is not None`: open a dedicated session (`async with async_session() as own_db:`, importing `async_session` from `app.core.database` — NOT the function's own `db` parameter, per research.md §11.4's concurrency hazard), call `await check_agentic_quota(own_db, user_id)`; on `QuotaExceededError` return `{"stdout": "", "stderr": "", "error": QUOTA_EXCEEDED_MESSAGE, "images": [], "output_files": []}`; on any other exception, branch on `quota_fail_closed()` — return the same dict shape with `QUOTA_UNAVAILABLE_MESSAGE` if closed, otherwise let dispatch proceed (fail open). Makes T066 pass. Depends on: T071, T072.
- [X] T074 In `backend/app/services/custom_tool_executor.py`, in `execute_js_tool_opensandbox`, add the same pre-dispatch check at the same insertion point (after its `check_sandbox_quota()` try/except, before `record_sandbox_call()`) — but on `QuotaExceededError` raise `RuntimeError(QUOTA_EXCEEDED_MESSAGE) from exc` (matching this function's own existing `SandboxQuotaExceededError` handling one line above, not a dict return); on other exceptions, branch on `quota_fail_closed()` to either raise `RuntimeError(QUOTA_UNAVAILABLE_MESSAGE)` or fail open. Makes T067 pass. Depends on: T071, T072.
- [X] T075 In `backend/app/services/custom_tool_executor.py`, apply the identical check to `execute_python_tool_opensandbox` (same file as T074, same shape). Makes T068 pass. Depends on: T071, T072 (independent of T074 — different function, can proceed in parallel with it despite sharing a file, since neither touches the other's function body).

**Checkpoint**: Agentic's per-user enforced limit now counts sandbox usage (FR-015–FR-017) across all four existing callers with no changes to them; the three sandbox-dispatch call sites gain a new per-user check that degrades the same way their existing platform-wide check already does (FR-018/FR-019); Phases 1–12 remain unaffected and still pass.

---

## Phase 14: Polish (FR-015–FR-019 delta)

- [X] T076 [P] Review `backend/app/services/quota.py` for the Principle IV rationale comment added in T072 (why agentic's period, not sandbox's own, governs the combined lookup) and confirm it's present and accurate.
- [X] T077 [P] Review `backend/app/services/code_interpreter.py` and `backend/app/services/custom_tool_executor.py` for a Constitution Principle IV rationale comment explaining (1) why the new check opens a dedicated session instead of reusing the caller's `db` (research.md §11.4), and (2) why the refusal shape deliberately differs between the code interpreter (dict) and the two custom-tool functions (raised `RuntimeError`) rather than being unified across all three (research.md §11.3).
- [X] T078 Run quickstart.md Scenario 9 end-to-end (or its mocked unit-test equivalent) to confirm the combined ceiling, period-window handling, and fail-mode behavior all work as specified.

### Phase 13–14 Dependencies

- Phase 13 depends only on Phases 1–12 already being complete (specifically `check_agentic_quota`, `check_sandbox_quota`, and `increment_sandbox_usage` already existing) — independent of Phase 12's frontend files.
- Within Phase 13: T071 and T072 are independent (different concerns, same file — either order); T073, T074, T075 all depend on both but are otherwise independent of each other (T074/T075 share a file but touch different functions, so can proceed in parallel).
- Phase 14 depends on Phase 13 being complete.
