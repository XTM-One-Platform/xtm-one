---

description: "Tasks for keeping the OpenCTI filter-key hint working across OpenCTI versions"
---

# Tasks: Keep the OpenCTI filter-key hint working across OpenCTI versions

**Input**: [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md), [quickstart.md](quickstart.md)

**Tests**: required (backend change, constitution VI). Written before the code they cover; the new-code test must fail first.

## Format: `[ID] [P?] [Story] Description`

Paths are from the repo root. Run Python from `backend/` with `.venv/bin/python`. Keep each file's line endings as they are.

## Phase 1: Setup

- [X] T001 Record the baseline: run `.venv/bin/python -m pytest tests/test_opencti_list_entities_filter_keys.py -q` in `backend/` and note any failure that exists before the change.

---

## Phase 2: Foundational

- [X] T002 In `backend/tests/test_opencti_list_entities_filter_keys.py`, give `_rejected` a `code` and `http_status` argument defaulting to `UNSUPPORTED_ERROR` and 500, so every existing case is unchanged (R1, R2).

**Checkpoint**: T001's cases still pass.

---

## Phase 3: User Story 1 - New OpenCTI code still gets a hint (Priority: P1) 🎯 MVP

**Goal**: `BAD_USER_INPUT_ERROR` with status 400 gets the close-key hint (FR-001, FR-002).

**Independent Test**: a `BAD_USER_INPUT_ERROR` / 400 rejection naming `labels` returns a hint pointing to `objectLabel`.

- [X] T003 [US1] Add a test to `backend/tests/test_opencti_list_entities_filter_keys.py`: a `_rejected("labels", code="BAD_USER_INPUT_ERROR", http_status=400)` answer returns `labels -> objectLabel` and one schema fetch. Run it: it fails.
- [X] T004 [US1] In `backend/app/integrations/providers/opencti/knowledge.py`, replace the `"UNSUPPORTED_ERROR" not in err` test in `_filter_key_hint` with a test against a module-level tuple `_FILTER_KEYS_ERROR_CODES = ("UNSUPPORTED_ERROR", "BAD_USER_INPUT_ERROR")`. One comment, two lines: old OpenCTI still sends the first code (R1). T003 now passes.

**Checkpoint**: the new code gets its hint.

---

## Phase 4: User Story 2 - Older OpenCTI keeps working (Priority: P1)

**Goal**: both codes behave identically in every existing case (FR-003, FR-004).

**Independent Test**: the full module passes for the old code with 500 and the new code with 400.

- [X] T005 [US2] Parametrize the hint cases in `backend/tests/test_opencti_list_entities_filter_keys.py` over `(UNSUPPORTED_ERROR, 500)` and `(BAD_USER_INPUT_ERROR, 400)`: close-key hint, no keys named (untouched, no schema fetch), empty schema, failed schema fetch, slow fetch, no-close-match generic hint. Update the `assert "UNSUPPORTED_ERROR" in content` echoes to the parametrized code.
- [X] T006 [US2] Add a case: an error with another code (`AUTH_REQUIRED`, and an unknown code carrying the `filter keys` message) gets no hint and no schema fetch (FR-003). The existing `test_other_errors_do_not_read_the_schema` covers the first; extend it.

**Checkpoint**: both rejection shapes pass the whole module.

---

## Phase 5: Polish

- [X] T007 Run the module and the other OpenCTI tests (`.venv/bin/python -m pytest tests -k opencti -q`); compare with T001's baseline.
- [X] T008 Run `.venv/bin/ruff check app tests` in `backend/` (blocking gate).
- [X] T009 Code name, 400 and unchanged message confirmed in OpenCTI PR #19096 (R3). Do the live check in [quickstart.md](quickstart.md) once it is merged.

---

## Dependencies & Execution Order

- T001 → T002 → T003 → T004 → T005 → T006 → T007–T009.
- US2 builds on US1's tuple; the cases in T005 for the old code pass from T002 on.
- Nothing is parallel: every task edits `knowledge.py` or the one test module.

## Implementation Strategy

MVP is US1 (T001–T004). US2 only adds regression coverage for the old code, so ship both in one PR.

---

## Phase 6: Convergence

- [X] T010 Add a test in `backend/tests/test_opencti_list_entities_filter_keys.py` that the `Incorrect filter keys containing invalid characters` message gets the close-key hint for both codes, per spec Edge Cases
