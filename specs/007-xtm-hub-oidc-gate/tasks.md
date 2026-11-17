# Tasks: XTM Hub sign-in entitlement gate

**Feature**: `specs/007-xtm-hub-oidc-gate` | **Branch**: `feat/2952-xtm-hub-oidc-platform-id`

**Input**: [spec.md](./spec.md), [plan.md](./plan.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/oidc-entitlement.md](./contracts/oidc-entitlement.md), [quickstart.md](./quickstart.md)

**Tests**: Required. Constitution Principle III (CI-Enforced Quality Gates) covers backend changes with unit tests plus the `integration` marker for real-Postgres behaviour.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallelisable — different file, no dependency on an incomplete task
- **[US1/US2/US3]**: the user story a task serves; setup, foundational and polish tasks carry none

## Path Conventions

Backend at `backend/app/`, its tests at `backend/tests/`, migrations at
`backend/alembic/versions/`. Frontend at `frontend/src/`.

---

## Phase 1: Setup

No project initialisation — this feature adds to an existing service. One
preparatory task only.

- [X] T001 Confirm the working tree carries no leftover diagnostics from claim discovery by checking `backend/app/api/auth_sso.py` contains no `TEMP` markers and `git diff` on that file is empty

---

## Phase 2: Foundational (blocking — all stories depend on these)

**Purpose**: configuration and the pure decision, which every story reads.

- [X] T002 Add the `xtm_hub_admin_group` setting (empty by default) in `backend/app/core/config.py` as the gate's enable switch, plus an `xtm_hub_effective_platform_id` property resolving the identifier from `platform_id`
- [X] T003 Add a `Settings` model validator (cross-field, so not a `field_validator`) in `backend/app/core/config.py` refusing startup when `xtm_hub_admin_group` is set and no identifier resolves, naming `PLATFORM_ID` in the error — and deliberately allowing the inverse, since an identifier without a marker is the ordinary case (FR-011)
- [X] T004 [P] Implement the pure decision helper `_resolve_hub_entitlement(userinfo)` in `backend/app/api/auth_sso.py`, returning "disabled" when no admin marker is configured, "refused" when the `{platform_id}_groups` claim is absent or not a list, and otherwise entitled with `is_admin` from an exact case-sensitive match of the admin marker within that list
- [X] T005 [P] Unit-test the decision table from `contracts/oidc-entitlement.md` exhaustively in `backend/tests/test_xtm_hub_entitlement.py` — disabled, absent claim, malformed claim, entitled-admin, entitled-user — asserting the helper performs no I/O

**Checkpoint**: configuration exists and the decision is provable without a browser or an identity provider.

---

## Phase 3: User Story 1 — An unentitled person is turned away cleanly (P1) 🎯 MVP

**Goal**: a person without this instance's entitlement is refused, no account is created, they see a readable page, and the refusal is recorded.

**Independent test**: with the gate configured to an identifier no Hub identity carries, sign in and confirm the account count is unchanged, the browser shows a readable message rather than JSON, and — when the address already had an account — an audit row is attributed to it.

### Tests for User Story 1

- [X] T006 [P] [US1] Test that a refused sign-in creates no user and returns a redirect rather than raising, in `backend/tests/test_xtm_hub_entitlement.py`
- [X] T007 [P] [US1] Integration-test refusal attribution against real Postgres in `backend/tests/test_xtm_hub_entitlement_postgres.py`, marked `integration`: a refusal for an existing account writes an audit row against it, and a refusal for an unknown address creates no row **and no user**
- [X] T008 [P] [US1] Test that the refusal message names no criteria, so an unauthenticated visitor learns nothing about the identity provider's configuration (FR-008), in `backend/tests/test_xtm_hub_entitlement.py`

### Implementation for User Story 1

- [X] T009 [US1] Implement `_record_refusal(db, email, reason)` in `backend/app/api/auth_sso.py`: look the address up, write an ordinary audit row against the account when one exists, and log a structured line when it does not — with a comment saying why durability follows attribution rather than widening a shared table for rows belonging to nobody (FR-009, FR-009a)
- [X] T010 [US1] Ensure `_record_refusal` never records the wider corporate group claim, only the reason and the instance that failed to match (FR-004a)
- [X] T011 [US1] Insert the guard in `oidc_callback` in `backend/app/api/auth_sso.py`, between the `if not email:` check and the `_get_or_create_sso_user(...)` call, with a comment explaining that this placement is what makes "no account created" true rather than compensated for (depends on T004)
- [X] T012 [US1] Call `_record_refusal` and redirect to `{frontend_url}/login?sso_error=not_authorised` from that guard, keeping the actual reason server-side while the redirect carries none (depends on T009, T011)
- [X] T012a [US1] Answer a provider error response (`?error=access_denied`, and any other) in `backend/app/api/auth_sso.py` with the same `sso_error` redirect rather than FastAPI's 422 for a missing `code` — scope added on the owner's request after a declined consent screen returned raw JSON; applies to every OIDC deployment, not only gated ones
- [X] T013 [P] [US1] Render the `sso_error` branch in `frontend/src/pages/Login.tsx` using the existing `useSearchParams`, stating the account is not authorised for this instance and scrubbing the parameter afterwards, mirroring how the page already scrubs the token fragment

**Checkpoint**: the security outcome is complete and demonstrable on its own.

---

## Phase 4: User Story 3 — Existing deployments are untouched (P1)

**Goal**: a deployment that has not configured the gate behaves exactly as before.

**Independent test**: with no `XTM_HUB_*` configuration, sign in through any identity provider and observe behaviour identical to before the feature — including provisioning an unknown address.

**Note**: this story is ordered after US1 because it verifies the absence of a change US1 introduces. It carries no implementation of its own — if it needs any, the default is wrong.

- [X] T014 [P] [US3] Test that an unconfigured deployment provisions and signs in exactly as before, in `backend/tests/test_xtm_hub_entitlement.py` (SC-004)
- [X] T015 [P] [US3] Run the existing SSO and auth test suites unchanged and confirm no regressions: `cd backend && pytest tests/ -q -m "not integration" -k "sso or auth"`
- [X] T016 [US3] Confirm the non-federated sign-in route remains available and documented as a recovery path (FR-012), in `docs/docs/deployment/configuration.md`

**Checkpoint**: the feature is proven inert when unconfigured.

---

## Phase 5: User Story 2 — An entitled person gets the role the Hub says they have (P2)

**Goal**: an entitled person's administrator status follows the Hub, in both directions, except for the platform admin.

**Independent test**: sign in with an identity carrying the admin marker and confirm administrator access; have the Hub drop the marker, sign in again, and confirm it is gone.

### Tests for User Story 2

- [X] T017 [P] [US2] Test that the admin marker grants administrator and its absence does not, in `backend/tests/test_xtm_hub_entitlement.py`
- [X] T018 [P] [US2] Test that administrator is **removed** when the marker disappears — the half that usually goes untested (FR-005)
- [X] T019 [P] [US2] Test that the platform admin (`settings.admin_email`) keeps administrator regardless of what the Hub asserts (FR-006), since the failure it prevents is unrecoverable
- [X] T020 [P] [US2] Test that the wider corporate group claim is never read or recorded (FR-004a)

### Implementation for User Story 2

- [X] T021 [US2] Apply the resolved role on every entitled sign-in in `backend/app/api/auth_sso.py`, promoting and demoting so the Hub stays authoritative, deliberately not routed through `_apply_auto_admin` — which is documented promote-only, and changing it would make every existing SSO deployment start demoting people (depends on T011)
- [X] T022 [US2] Exempt the platform admin from demotion in that same path, with a comment naming the failure it prevents: a Hub-side group change costing an operator access to their own instance, with no way back through the gate (depends on T021)

**Checkpoint**: central role management works end to end, and cannot lock the operator out.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T023 [P] Document `XTM_HUB_ADMIN_GROUP` in `docs/docs/deployment/configuration.md` as the gate's switch, alongside the `PLATFORM_ID` it needs
- [X] T023a [P] Declare `XTM_HUB_ADMIN_GROUP` in `.env.sample` and confirm `scripts/check-new-env-vars-documented.sh` passes with `BASE_REF=main`
- [X] T024 [P] Note the Hub entitlement gate in `docs/docs/deployment/chat-approvals-in-clients.md`'s sibling SSO documentation, or wherever OIDC setup is described, so an operator meets it while configuring SSO rather than afterwards
- [X] T025 Run the full backend suite and lint: `cd backend && pytest tests/ -q -m "not integration" && ruff check .`
- [X] T026 Run the frontend gates: `cd frontend && npm run check` (typecheck, lint, command manifests) — **passed**. `npx playwright test tests_e2e/login.spec.ts` needs `frontend/.env.e2e` credentials and a running backend, neither present; it is folded into T027
- [X] T027 Walk `quickstart.md` scenarios 1–6 against the live Hub development tenant, including the half-configured startup failure and the platform-admin exemption
- [X] T028 Post the verified behaviour for the Hub team (JPK, CL), noting anything the connection tests contradicted

---

## Dependencies

```
Phase 1 (T001)
  └─> Phase 2 Foundational (T002–T005)   ← blocks everything
        ├─> Phase 3  US1 (T006–T013)     🎯 MVP, delivers the security outcome
        │     └─> Phase 4  US3 (T014–T016)   verifies US1 changed nothing when off
        │     └─> Phase 5  US2 (T017–T022)   builds on the guard from T011
        └─> Phase 6  Polish (T023–T028)
```

- **US1 depends only on Phase 2.** It is independently shippable.
- **US3 depends on US1** existing, since it proves that change is inert when unconfigured.
- **US2 depends on T011** (the guard) but on nothing in US3.
- T009 → T011 → T012 is the only strictly serial chain of substance.

## Parallel Execution

**Phase 2**: T004 and T005 run together (implementation and its unit tests are different files, and the helper is pure).

**Phase 3**: T006, T007, T008 in parallel; T013 (frontend) in parallel with all backend work in the phase.

**Phase 4**: T014 and T015 in parallel.

**Phase 5**: T017–T020 all in parallel — four independent assertions over the same pure helper.

**Phase 6**: T023 and T024 in parallel; T025 and T026 in parallel.

## Implementation Strategy

**MVP is Phase 3 (US1) alone.** It closes the security hole: an unentitled
person is refused, nothing is created, and the refusal is recorded. Role
management is valuable but is not what makes the integration safe.

**Deliver in this order**: Foundational → US1 → US3 → US2 → Polish. US3 sits
between the two implementation stories deliberately — it is the cheapest way to
catch the feature leaking into deployments that never asked for it, and running
it before US2 means any leak is found while the change is still small.

**There is no migration and no schema change.** The riskiest task is now T011,
the guard itself: placed wrongly it stops being true that no account is created,
and that is the whole security outcome.

---

## Phase 7: Convergence

Gaps found by assessing the code against `spec.md`, `plan.md` and this file. All
are `partial` — nothing specified is missing, and nothing contradicts the
constitution.

- [X] T029 Bound and sanitise `error` and `error_description` before logging them in `oidc_callback` in `backend/app/api/auth_sso.py` — the endpoint answers unauthenticated, so today anyone can write arbitrary text (newlines included, forging log lines) and unbounded WARNING entries into the operator's log, which is the record FR-008 relies on being trustworthy, per FR-008 (partial)
- [X] T030 Pin the platform admin's asymmetry with a test in `backend/tests/test_xtm_hub_entitlement.py`: they are exempt from **demotion** but not from **refusal**, so an unentitled `ADMIN_EMAIL` is turned away like anyone else and reaches the instance only through the local sign-in route. Note in the test's docstring that SC-006 ("retains access in **every** case") reads broader than FR-006 ("never remove administrator access"), and that only the second is implemented — for the owner to resolve, per SC-006 (partial)
- [X] T031 [P] Add a Playwright case in `frontend/tests_e2e/login.spec.ts` covering `/login?sso_error=` for `not_authorised`, `cancelled` and `failed`: each shows its own readable message, none shows a raw error, and the parameter is scrubbed from the URL. The page a refused person actually sees is currently the one part of FR-007 with no automated coverage — there is no frontend unit runner, so Playwright is the only route, per FR-007, SC-002 (partial)
- [X] T032 [P] Correct Scenario 3 in `specs/007-xtm-hub-oidc-gate/quickstart.md`: it tells an operator to confirm the corporate group claim is absent from `details`, but `_record_refusal` never populates that column, so the check passes whatever the code does. The evidence lives in `summary`, per FR-004a (partial)

---

## Phase 8: Convergence

- [X] T033 Move the SSO failure message out of the local-auth form in `frontend/src/pages/Login.tsx`: the only `formError` banner (line ~560) sits inside the `<form>` gated by `authConfig?.local_enabled !== false`, so on an **SSO-only** deployment a refused person has the marker consumed and scrubbed and then sees nothing at all — a blank page where the readable explanation should be. That is the configuration a Hub-federated instance is most likely to run. Add an E2E case in `frontend/tests_e2e/login.spec.ts` intercepting `/auth/config` with `local_enabled: false`, since the existing markers pass only because local auth happens to be enabled locally, per FR-007, SC-002, US1/AC1 (partial)
