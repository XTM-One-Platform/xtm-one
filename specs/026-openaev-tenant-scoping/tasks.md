---

description: "Tasks for OpenAEV tenant scoping"
---

# Tasks: OpenAEV Tenant Scoping

**Input**: [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/path-scoping.md](contracts/path-scoping.md), [quickstart.md](quickstart.md)

**Tests**: Required (constitution VI). New tests go in `backend/tests/test_openaev_tenant_scoping.py`, written before the code they cover.

## Format: `[ID] [P?] [Story] Description`

Paths are repo-relative. `P` = `backend/app/integrations/providers/openaev`. **Source files are CRLF**, tests included: edit in place, never rewrite via `read_text`/`write_text`, and create the new test file with CRLF; T022 checks this.

**Test technique** (used by every test task): patch `_common.safe_request` (imported at `P/_common.py:29`) with a recorder. It answers `GET .../api/settings` with the settings under test and records every other URL. Both `_api` and the new `scoped_safe_request` live in `_common`, so one patch covers all modules. Build tools with `build_tools(TOKEN, executor)` and run them as `test_openaev_package_tools.py` does (`_build`, `_run`).

## Phase 1: Setup

- [X] T001 Record the baseline: `cd backend && pytest tests/test_openaev_package_tools.py tests/test_advisory_consult_openaev_writes.py tests/test_orchestrator_arsenal_write_gate.py tests/test_orchestrator_arsenal_findings.py tests/test_openaev_payload_output_example.py tests/test_autonomous_pentest_cycle.py -q` and note pass/fail/skip counts in the PR notes. A skip is unverified, not green.

---

## Phase 2: Foundational (blocks all stories)

**Purpose**: the scoping primitive and the tri-state lookup, with no behaviour change yet.

- [X] T002 Create `backend/tests/test_openaev_tenant_scoping.py` with the recorder fixture and a table-driven test of `_scope_path` covering every row of [contracts/path-scoping.md](contracts/path-scoping.md) (scoped family, already scoped, each exempt family incl. `/api/settings/version` and `/api/autonomous-runs/r1/events`, non-`/api` path, `none` and `failed` states, explicit `tenant_id` wins). Must fail first.
- [X] T003 In `P/_common.py` make `PlatformCapabilities._load` (line ~1347) safe under concurrent first calls: guard with an `asyncio.Lock` so a second concurrent caller waits for the fetch instead of seeing empty settings and going unscoped (autonomous callbacks `gather` tool calls).
- [X] T004 In `P/_common.py` add a frozen dataclass `TenantScope(tenant_id: str, error: str | None)` and `PlatformCapabilities.tenant_scope()` returning it: `tenant_id` set when resolved, both empty for `none`, `error` set for `failed` (reuse `self._error`). Make `_load` (line ~1357) treat any exception or non-JSON body as `failed(cause)` instead of raising, since every tool now depends on it. Leave `tenant_id()` untouched for its existing callers (`threat_arsenal.py`, `preview.py`, `platform.py`).
- [X] T005 In `P/_common.py` add the exemption constant (settings, platform-users, platform-roles, platform-groups, schemas, me, autonomous-runs) with a why-comment citing [research.md](research.md) R2, and `async _scope_path(headers, path) -> tuple[str, str]` returning `(path_to_request, lookup_error_hint)`. It reads the resolver with `getattr(headers, "tenant_scope", None)` like `refresh_fn`, and returns the path unchanged when there is no resolver. Reuse `_tenant_path` for the rewrite. Never call the resolver for exempt paths (`/api/settings` is how the resolver loads: no recursion).
- [X] T006 Add tests in `backend/tests/test_openaev_tenant_scoping.py` for T003/T004: two concurrent `tenant_scope()` calls trigger one settings fetch and both see the tenant; a failed read, and a 200 with a non-JSON body, are reported as `failed` (not `none`, no exception) and not retried.

**Checkpoint**: T002 and T006 pass; nothing else changes.

---

## Phase 3: User Story 1 - Author and launch simulations on a multi-tenant OpenAEV (P1) 🎯 MVP

**Goal**: create scenario, add inject, launch simulation succeed against tenant-scoped routes.

**Independent Test**: with settings `{"default_tenant_id": "t1"}`, the three tools request `/api/tenants/t1/...`.

- [X] T007 [US1] Add a test in `backend/tests/test_openaev_tenant_scoping.py`: `create_openaev_scenario`, `add_openaev_inject_to_scenario` (both `core.py`, direct `safe_request`) and `manage_openaev_simulation` / `manage_openaev_scenario` launch (`simulations.py`, `_api`) record `/api/tenants/t1/scenarios`, `/api/tenants/t1/scenarios/{id}/injects`, `/api/tenants/t1/scenarios/{id}/exercise/running`. Must fail first.
- [X] T008 [US1] In `P/_common.py` `_api` (line ~1195): when `tenant_id` is empty, compute the path with `_scope_path(headers, path)` before `_tenant_path`; an explicit `tenant_id` keeps winning (FR-009).
- [X] T009 [US1] In `P/_common.py` add `scoped_safe_request` with `safe_request`'s exact signature; it scopes the path part of `url` using its `headers` kwarg via `_scope_path`, then delegates to `safe_request`. Add it to `__all__` (line ~1970).
- [X] T010 [US1] In `P/__init__.py` after `capabilities = PlatformCapabilities(base_url, headers)` (line ~140) set `headers.tenant_scope = capabilities` so every module's `headers` carries the one resolver (FR-003, FR-004).
- [X] T011 [US1] In `P/core.py` import `scoped_safe_request` and replace all 26 `await safe_request(` calls (import at line 19; keep `format_entity_list`). Keep `egress_client` as is.
- [X] T012 [P] [US1] In `backend/tests/test_openaev_package_tools.py` change the three `monkeypatch.setattr(core, "safe_request", ...)` patches (lines ~2454, 2474, 2600) to `scoped_safe_request`, and any other test that patches `core.safe_request`.
- [X] T013 [US1] Run T001's test list plus `pytest tests/test_openaev_tenant_scoping.py`; fix fallout (tests that patch `_common.safe_request` with a fake that now also receives the settings probe).

**Checkpoint**: US1 test passes; the quickstart live step 1 is runnable.

---

## Phase 4: User Story 2 - Every OpenAEV tool works on a multi-tenant instance (P1)

**Goal**: no module is left unscoped, and a future one cannot be.

**Independent Test**: one tool per module records a tenant-scoped path.

- [X] T014 [US2] Add tests in `backend/tests/test_openaev_tenant_scoping.py`: (a) parametrized over modules `access, assets, content, dashboards, expectations, findings, injects, lessons_reports, notifications, phishing, platform, taxonomy, teams, testing` (`simulations`/`core` are covered by T007, `preview`/`threat_arsenal` by T017), with one read and one write action each, asserting the recorded path starts `/api/tenants/t1/`; (b) a settings read, a platform-level user/group call and an autonomous-run callback (`record_openaev_event`) keep their base path (FR-010); (c) static guard: scanning `P/*.py`, any `safe_request(` or `httpx.AsyncClient(` call outside `_common.py`, `core.py` and `content.py` fails the test (SC-005). Must fail first where it covers `content.py`.
- [X] T015 [US2] In `P/content.py` the document upload (line ~154) sends stripped headers (`auth_only`), so compute its URL with `await _scope_path(headers, "/api/documents")` using the full headers, then keep the existing `safe_request` call. Import `_scope_path` from `._common`.
- [X] T016 [US2] Run T014; for every module that records an unscoped path, find the call that bypasses `_api` (direct client use) and route it through `_scope_path`. If a route 404s live, add its family to the exemption constant with a [research.md](research.md) R2 note.

**Checkpoint**: T014 passes for all modules.

---

## Phase 5: User Story 3 - Single-tenant instances are unchanged (P1)

**Goal**: no default tenant means identical requests.

**Independent Test**: same tool sample as US1/US2 with settings `{}`.

- [X] T017 [US3] Add tests in `backend/tests/test_openaev_tenant_scoping.py`: with settings `{}` the US1 tools and one tool per module in T014 record exactly the pre-change base URLs; Threat Arsenal delete still uses its own explicit tenant (FR-009).
- [X] T018 [US3] Run `pytest tests/test_openaev_tenant_scoping.py tests/test_openaev_package_tools.py`; fix any divergence in `P/_common.py` (no new code expected).

**Checkpoint**: single-tenant behaviour proven unchanged.

---

## Phase 6: User Story 4 - A failed tenant lookup is diagnosable (P2)

**Goal**: a failed lookup never blocks a request and is named when a request fails.

**Independent Test**: settings read raises/denies; a tool request that succeeds is unchanged, one that fails carries the hint.

- [X] T019 [US4] Add tests in `backend/tests/test_openaev_tenant_scoping.py`: settings read returns an error (a) request succeeds on the base path, result unchanged; (b) request is rejected with `TENANT_WRITE_SCOPE`, the error contains the hint text from [contracts/path-scoping.md](contracts/path-scoping.md) with the cause, for both an `_api` tool and a `core.py` tool, while a 404 gets no hint; (c) settings fetched once across several calls; (d) a denied/404-masked settings read counts as `failed`, not `none`; (e) a 200 with a non-JSON body counts as `failed` and the tool still runs. Must fail first.
- [X] T020 [US4] In `P/_common.py` append the `_scope_path` hint to the error returned by `_api`, `scoped_safe_request` and the `content.py` upload only when the lookup failed and the error is a tenant-scope rejection (`_with_scope_hint`).

**Checkpoint**: all four stories pass.

---

## Phase 7: Polish

- [X] T021 Run `cd backend && ruff check app/ tests/test_openaev_tenant_scoping.py` and fix findings (blocking gate).
- [X] T022 Verify line endings: `file` on every touched `.py` under `P/` and on the new test file reports CRLF, and `git diff --stat` shows only intended lines (no phantom whole-file diffs).
- [X] T023 Run the full affected suite: `cd backend && pytest tests/ -k "openaev or arsenal or autonomous" -q`; compare with T001 and report any new skips.
- [X] T024 Run [quickstart.md](quickstart.md) live steps on a multi-tenant OpenAEV (needs the dev stack and a test instance; hand to the requester if unavailable) and record which tool groups were exercised. Done 2026-10-05 for the create/add/launch flow through the chat (default tenant only, no `TENANT_WRITE_SCOPE`); the other tool groups, a restricted token and a non-default-tenant run are tracked in the end-to-end test repository.

---

## Dependencies & Execution Order

- Phase 1 → Phase 2 → Phases 3-6 → Phase 7.
- Phase 2 is strictly sequential (all `_common.py`, one test file).
- US1 is the MVP and carries the shared mechanism (T008-T011); US2/US3/US4 depend on it. After US1: US2 (touches `content.py` + tests), US3 (tests only) and US4 (`_common.py` error path) are independent of each other, but share `test_openaev_tenant_scoping.py`, so write tests serially.
- Only T012 is marked `[P]` (a different file from T008-T011).

## Implementation Strategy

1. Phases 1-2, then US1: stop and validate the reported failure is fixed (MVP; shippable alone).
2. US2 closes the remaining modules and installs the guard; US3 proves no regression; US4 adds the diagnostics.
3. Commit per phase; PR title `fix(openaev): scope tool requests to the default tenant`.

---

## Phase 8: Convergence

- [X] T025 Update `plan.md` (design 5), `data-model.md` (the `failed(cause)` row) and `quickstart.md` (live step 3) so the lookup cause is appended only to `TENANT_WRITE_SCOPE` / `TENANT_SELECTOR_REQUIRED` rejections, replacing "any request error", per FR-006 (contradicts)
- [X] T026 Update `plan.md` design 1 and 4 to record that the resolver is a declared field on `OpenAEVHeaders` (`P/_common.py`) and that the settings probe retries transient 5xx, per plan: design 1 and 4 (partial)
- [X] T027 With the dev database up, run `cd backend && pytest tests/test_hitl_whitelist_layering.py tests/test_work_item_workspace_scope_postgres.py -q` and fix any OpenAEV registration regression, per T023 / Constitution VI (partial)
