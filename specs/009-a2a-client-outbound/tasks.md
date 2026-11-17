---

description: "Task list for the minimal A2A client (outbound)"
---

# Tasks: Minimal A2A Client (Outbound)

**Input**: Design documents from `/specs/009-a2a-client-outbound/`

> Test-file names below are the plan's. Implementation consolidated several into differently named files; the shipped list is `backend/tests/test_a2a_client_*.py`, and `test_a2a_client_no_dead_code.py` pins that every `app/a2a/client` module is covered.

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md), [data-model.md](./data-model.md), [contracts/](./contracts/), [quickstart.md](./quickstart.md)

**Supersedes** the 2026-08-26 task list, which predates the direction clarification. Direction is now foundational and this feature modifies shipped inbound code.

**Tests**: Mandatory. Backend tests are a blocking CI gate per the constitution's CI-Enforced Quality Gates principle. Each story phase writes its tests first.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallelizable — different file, no dependency on incomplete work
- **[Story]**: US1–US4 from [spec.md](./spec.md). Setup, Foundational and Polish carry no label

---

## Phase 1: Setup

- [X] T001 Create the outbound package skeleton at `backend/app/a2a/client/__init__.py`, with a docstring stating why the outbound half is its own package — the inbound modules answer "who is calling us", these answer "who are we calling"
- [X] T002 **Corrected during implementation**: the capability switch is the `A2A_CLIENT` **feature flag** in `backend/app/core/feature_flags.py`, not a setting — the inbound half is gated the same way (`A2A_SERVER`), and one concept with two mechanisms is the divergence Principle VIII exists to stop. Feature flags are already fail-closed when `ENABLED_FEATURES` is unset. The numeric bounds are tuning values and stay in `backend/app/core/config.py`: `a2a_client_sweep_interval_seconds` (3600, FR-019), `a2a_client_verdict_staleness_seconds` (900, FR-050), `a2a_client_delegation_ceiling_seconds` (300, FR-024), `a2a_client_response_max_bytes` (1_048_576, FR-027; extracted text is then bounded by the existing `a2a_max_message_chars`), `a2a_client_breaker_threshold` (5) and `a2a_client_breaker_cooldown_seconds` (300, FR-030). Each numeric field gets a validator rejecting non-positive or unparseable values at startup (constitution Principle II)
- [X] T003 [P] Amend the containment docstring in `backend/app/a2a/protocol.py` to record `app/a2a/client/sdk.py` as a second permitted `a2a_sdk` import site (research R1), and export what the client needs to negotiate a version without reading `DEFAULT_VERSION` (research R4)
- [x] T004 [P] ~~Correct FR-014 in `specs/009-a2a-client-outbound/spec.md`~~ — **done 2026-08-27** during the `/speckit-analyze` remediation. Retrieval now presents the registration's credentials *where the target requires them*; this platform serves its own cards anonymously, so the OAuth target does not need them (research R14). T030 implements the corrected rule
- [X] T005 [P] Add `backend/tests/test_a2a_client_flag.py`: with `a2a_client_enabled=False`, no delegation tool reaches any run, no sweep runs, and no outbound request is attempted regardless of registration, direction or binding state (FR-007, SC-004)

**Checkpoint**: inert scaffolding.

---

## Phase 2: Foundational (Blocking Prerequisites)

**⚠️ CRITICAL**: no story work begins until this completes. Direction is foundational because every story reads it, and because getting it wrong is a fail-open regression on a shipped surface.

### Schema and direction

- [X] T006 Add the `A2AIntegrationSettings` model to `backend/app/models/a2a.py` per [data-model.md](./data-model.md#new-table-a2a_integration_settings) (FR-055, FR-020, FR-053): `direction` NOT NULL, `usability` defaulting to `unusable`, the closed `unusable_reason` set, and outbound columns nullable. Comment the mode-dependent NULLs and the three invariants
- [X] T007 Add the `A2ADelegation` model to `backend/app/models/a2a.py` — same file as T006, so sequential. `run_ref` is a real FK to `assignment_runs.id` with `ON DELETE CASCADE`; carry the rationale for diverging from the inbound sibling's `SET NULL` (FR-042, FR-044, FR-046)
- [X] T008 Create one Alembic revision in `backend/alembic/versions/` chained on the single head `a2a2intmodel1`: both tables with `IF NOT EXISTS`, a bounded backfill inserting one settings row with `direction='inbound'` per existing `oauth_a2a` / `entra_a2a` integration — stating what is already true of every integration in the field rather than changing behaviour (FR-055) — and a working `downgrade()`. Do not edit `a2a1srv0001` or `a2a2intmodel1` — both are merged history (constitution Principle V)
- [X] T009 Extend `backend/app/a2a/providers.py` with the single answer to "is this integration A2A, and which direction": a session-taking lookup returning an agent's **inbound** bindings and one returning its **outbound** ones. An integration with no settings row belongs to neither — comment that this is what stops a missing row from becoming an unintended exposure (research R11, FR-055)

### The shipped-code change

- [X] T010 Change `binds_a2a` and its call site in `backend/app/a2a/hooks.py` so `_sync_implicit_exposure` creates an exposure only for **inbound** bindings. Today it tests provider-key membership alone, which would expose any agent bound to an outbound integration. Carry a rationale comment naming the regression it prevents (constitution Principle VII, FR-055)
- [X] T011 Enforce FR-057 in `backend/app/api/v1/integrations.py`: refuse a direction change while the integration is bound to any agent, naming them in the refusal. Reuse the existing `agents_binding()` rather than writing a second query (research R12)
- [X] T012 Clear the previous direction's credentials on a permitted direction change in `backend/app/api/v1/integrations.py` (FR-058) — a partner credential retained for a direction no longer served is a stored secret with no purpose and no expiry

### Shared outbound machinery

- [X] T013 [P] Create `backend/app/a2a/client/sdk.py` as the containment point for the SDK's client half — `A2ACardResolver` with a per-registration card path, `client_factory`, auth interceptors, and `utils.signing.create_signature_verifier`. No `a2a_sdk` type leaves this module (research R1)
- [X] T014 [P] Create `backend/app/a2a/client/failures.py`: one classifier returning `transient` or `durable` plus a reason from the closed set, mapping token-mint failure and partner 401/403 to durable, transport failures to transient, and a deployment-refused destination to `destination_blocked_by_deployment`. The single thing the breaker and the health verdict both read (FR-053, FR-054)
- [X] T015 [P] Create `backend/app/a2a/client/destinations.py`: reject non-`https`, loopback, private, link-local and cloud-metadata destinations at registration and before every request, and pin an origin for comparison. Comment that this exists because the repo's only egress allow-list is sandbox-scoped (FR-034, FR-035, research R6)
- [X] T016 [P] Extend `backend/app/a2a/metrics.py` with outbound counters distinct from the inbound ones, each written from the single place its event occurs (FR-043)
- [X] T017 Extend the catalog entry shape in `backend/app/api/v1/integration_catalog.py` to carry per-direction field sets, and have the endpoint return the set matching an integration's direction. No conditional-field precedent exists, so this is a new shape (research R13, FR-056)

### Test scaffolding and foundational tests

- [X] T018 Create `backend/tests/a2a_client_stubs.py`: a stubbed partner serving a configurable card (signed, unsigned, invalid-signature, malformed, absent) and task surface (terminal, `input-required`, never-answers, unknown state, oversized, off-origin redirect). Every later test builds on it
- [X] T019 **Inbound regression suite** — `backend/tests/test_a2a_direction_regression.py`, covering quickstart Scenario 0 and measuring SC-020: an outbound binding creates **no** exposure; an inbound binding exposes exactly as before and confers no tool; an A2A integration with no settings row is neither; and the existing inbound suite still passes unchanged. Exercise every path reaching the exposure hook — binding an existing agent, unbinding, and **creating an agent with bindings already set**, which `_sync_new_agent_exposure` serves separately — across all three mechanisms (FR-055, SC-004, SC-020)
- [X] T020 [P] Add `backend/tests/test_a2a_direction_switch.py`: a direction change is refused while bound and names the agents; succeeds after unbinding; and clears the previous direction's credentials (FR-057, FR-058)
- [X] T021 [P] Add `backend/tests/test_a2a_client_models.py`: the closed `unusable_reason` set, the unique constraint on `integration_id`, `usability` defaulting to `unusable`, and cascade deletion removing settings and delegations with the integration (FR-008)
- [X] T022 [P] Add a migration test to `backend/tests/` asserting the backfill stamps every pre-existing A2A integration as `inbound` and creates no row for anything else (FR-055, SC-020), then confirm the revision is picked up by the `migrations` job in `.github/workflows/ci.yml` (constitution Principle V)
- [X] T023 [P] Add `backend/tests/test_a2a_client_failure_classification.py`: each failure kind classifies once, a durable failure never increments the transient counter, and the classifier is the only producer of an `unusable_reason` (FR-053, SC-019)
- [X] T024 [P] Add `backend/tests/test_a2a_client_destinations.py`: an SSRF probe matrix — `169.254.169.254`, loopback, RFC1918, link-local, plain HTTP, off-origin redirect (FR-012, FR-034, FR-035, SC-006)
- [X] T025 [P] Add `backend/tests/test_a2a_client_sdk_containment.py` asserting `a2a_sdk` is imported only by `app/a2a/protocol.py` and `app/a2a/client/sdk.py` — a static guard, so the boundary is enforced rather than merely documented

**Checkpoint**: direction is live, the shipped surface is protected by a regression suite, and outbound scaffolding is ready.

---

## Phase 3: User Story 1 — Register and verify a partner's agent (Priority: P1) 🎯 MVP

**Goal**: An operator registers an outbound partner on any of three mechanisms, the platform verifies its card, and the operator sees what it declares — with every defective-card case producing its own reason.

**Independent Test**: With no agent bound to anything, register against the stub and read the verification state; then walk the defective-card matrix and assert one distinct reason per row.

### Tests for User Story 1

- [X] T026 [P] [US1] Add `backend/tests/test_a2a_client_verification.py`: the card matrix from [quickstart.md](./quickstart.md) — good card, unfetchable, malformed, unconfirmable transport, invalid signature (unusable with no override), unsigned (usable, `signature_state: unsigned`), off-origin endpoint, no common protocol version (FR-011–FR-016, FR-051, SC-003)
- [X] T027 [P] [US1] Add `backend/tests/test_a2a_client_registration_authority.py`: create, edit, share and delete follow the existing integration authority; company-managed is administrator-only; denials are audit-logged (FR-002, FR-003)
- [X] T028 [P] [US1] Add `backend/tests/test_a2a_client_secrets.py`: credentials never appear in an API response, log line, health message, `unusable_reason` or `last_verification_error`, across the whole lifecycle including a direction change (FR-004, SC-012)
- [X] T029 [P] [US1] Add `backend/tests/test_a2a_client_protocol.py`: negotiation picks the highest version in common, records it, and the outbound path never reads `DEFAULT_VERSION` (FR-023, FR-051, SC-018)

### Implementation for User Story 1

- [X] T030 [P] [US1] Create `backend/app/a2a/client/verification.py`: fetch the card at the per-registration location with the registration's credentials where the target needs them, verify a present signature fail-closed, negotiate and record the version, pin the origin, fingerprint the material fields, and write one verdict (FR-011, FR-013, FR-014, FR-015, FR-016, FR-018, FR-019, FR-023)
- [X] T031 [US1] Add the outbound mode to `backend/app/integrations/providers/oauth_a2a.py`: client id, secret and token endpoint exchanged for a bearer, targeting any partner accepting OAuth client-credentials including this platform's own inbound surface. Leave the shipped inbound path untouched (FR-045, FR-010)
- [X] T032 [US1] Add the outbound mode to `backend/app/integrations/providers/entra_a2a.py`: Entra client credentials against the pinned tenant, targeting Azure Foundry, reaching identical delegation behaviour. Leave the shipped inbound pins untouched (FR-045, FR-010)
- [X] T033 [US1] Create `backend/app/integrations/providers/google_a2a.py` — **corrected during implementation**: serves both directions, not outbound-only (the inbound half is the Google analogue of Foundry federation, FR-061). Outbound: a Google service-account key exchanged for a `cloud-platform`-scoped token via signed assertion, using the existing `authlib`/`pyjwt` stack and `integrations/http.py` token helpers — no new auth SDK (FR-045, FR-010, research R8)
- [X] T034 [US1] Add the three catalog entries in `backend/app/api/v1/integration_catalog.py` using T017's per-direction shape: the two shipped keys gain outbound field sets, `google_a2a` is added — **corrected during implementation**: with `a2a_directions: ["inbound", "outbound"]`, not outbound-only (see T033). The mechanism plus direction determines the target, so no separate platform field exists (FR-001, FR-009, FR-056)
- [X] T035 [US1] Register a connectivity probe per mechanism via `register_connectivity` in `backend/app/integrations/providers/{oauth_a2a,entra_a2a,google_a2a}.py`, returning the stored health verdict and its reason, never echoing a credential (FR-020)
- [X] T036 [US1] **Corrected during implementation**: no `on_connect` hook exists — no provider registers one for the outbound direction. First verification happens through the sweep (a never-verified target is due immediately), the manual Verify action, or the pre-delegation freshness check, so a hook would be a fourth path to one verdict (FR-011)
- [X] T037 [US1] Create `backend/app/api/v1/a2a_client.py` with `GET /{integration_id}` (declared card contents, signature state, agreed version, health verdict and reason, circuit state, pending card change) and `POST /{integration_id}/verify` (on-demand re-verification, which also closes the breaker) per [contracts/internal-management.md](./contracts/internal-management.md). JWT-authenticated, authority-checked, audit-logged, no credential in any payload or error (FR-018, FR-019, FR-020, FR-039, FR-004)
- [X] T038 [US1] Register the new router in `backend/app/api/v1/__init__.py` behind the deployment switch (FR-007)
- [X] T039 [US1] Wire audit logging for registration creation, credential update, direction change and deletion through `backend/app/a2a/audit.py`, recording what happened and leaving who to the surrounding trail where no request context exists (FR-039)
- [X] T040 [P] [US1] Create `frontend/src/hooks/api/useA2AClientApi.ts` with TanStack Query, intervals from `frontend/src/lib/constants.ts`, no `useState` for server data (FR-018, constitution Principle IX)
- [X] T041 [P] [US1] Add the direction control and verification panel under `frontend/src/pages/integration-detail/`: direction chosen at creation and switchable when unbound, what the card declares, the health verdict and reason, and an explicit unverified-card indicator when `signature_state` is `unsigned` (FR-017, FR-018, FR-055, FR-057). CSS tokens, `Switch`, `Tip`
- [X] T042 [P] [US1] Add the new UI strings to `frontend/src/i18n/lang/en.json`, `fr.json` and `ja.json`

**Checkpoint**: an operator can register a partner on any mechanism, see what it declares, and see why an unusable one is unusable. Nothing can be delegated to yet.

---

## Phase 4: User Story 2 — Bind the partner to an agent and delegate work (Priority: P1)

**Goal**: Binding an outbound registration gives that agent one delegation tool and nothing else; the agent hands the partner work and gets a result or a distinguishable failure.

**Independent Test**: Against the stub, an unbound agent has no reach, a bound agent completes a delegation end-to-end, and each of the five failure classes arrives distinctly.

### Tests for User Story 2

- [X] T043 [P] [US2] Add `backend/tests/test_a2a_client_delegation.py`: an unbound agent has no delegation tool at all (absent, not present-and-refusing); a bound agent submits, follows to terminal, and returns the result with `card_provenance` and `protocol_version` (FR-005, FR-021, FR-022, SC-004)
- [X] T044 [P] [US2] Add `backend/tests/test_a2a_client_delegation_failures.py`: unreachable, credential rejected, partner-reported failure, timeout and content-type mismatch each arrive as their own reason with the partner's preserved, and zero undifferentiated errors (FR-029, SC-009)
- [X] T045 [P] [US2] Timeout and cancel coverage (landed in `test_a2a_client_delegation.py` / `test_a2a_client_rest_transport.py`, not a separate file): the ceiling binds and expiry cancels at the partner (FR-024, SC-010). The run-teardown half of FR-025 — a run ending with a task still at `input-required` — is wired through `run_agent_chat`'s teardown (`cancel_outstanding_delegations`), covered in `test_a2a_client_tool_wiring.py`
- [X] T046 [P] [US2] Add `backend/tests/test_a2a_client_preflight.py`: refusal before any network call when the switch is off, direction is not outbound, the registration is unusable, the circuit is open, the accepted card does not match, or content types mismatch — and a pre-flight refusal never counts toward the breaker unless its cause is durable (FR-028, FR-030, FR-038, FR-055)
- [X] T047 [P] [US2] Add `backend/tests/test_a2a_client_circuit_breaker.py`: N consecutive transient failures open it, cooldown lengthens on repeat, a durable failure never increments the transient counter, a successful on-demand verify closes it, and two registrations on one agent never cross-suppress (FR-030, SC-010, SC-019)
- [X] T048 [P] [US2] Add `backend/tests/test_a2a_client_protocol_refusals.py`: a response for an unsubmitted task, a shape mismatched to the agreed version, an unrecognised task state and an oversized body are each refused whole (FR-026, FR-027, SC-015)
- [X] T049 [P] [US2] Add `backend/tests/test_a2a_client_continuation.py`: an `input-required` partner returns control with question and task id and no human is waited on; a continuation resumes that same task; a task id from another run is refused; a partner replying `input-required` forever exhausts a run budget with `continuation_count` recording the rounds; and an unsupplied authorisation fails without retry (FR-031, FR-046, FR-048, FR-049, SC-002)
- [X] T050 [P] [US2] Add `backend/tests/test_a2a_client_binding_resolution.py`: a company-managed outbound registration bound per-user resolves against the acting user under the ordinary eligibility rule, and a user with no eligible integration simply has no tool (FR-006)

### Implementation for User Story 2

- [X] T051 [US2] Create `backend/app/a2a/client/breaker.py`: consecutive-transient counting, backing-off cooldown, and closure on a successful verification — all on the settings row, reading its classification from `failures.py` rather than deciding again (FR-030, research R5)
- [X] T052 [US2] Create `backend/app/a2a/client/delegation.py` (**after T051** — its pre-flight reads circuit state): pre-flight in the order given in [data-model.md](./data-model.md#validation-rules), submit the composed message, poll to terminal or `input-required` using the existing wait primitive so polling does not consume loop iterations, map states through `a2a/protocol.py`, and cancel outward on ceiling expiry or run teardown (FR-021–FR-029, FR-031)
- [X] T053 [US2] Create the shared delegation tool builder in `backend/app/a2a/client/tool.py` — one implementation all three mechanisms import, since FR-045 requires identical behaviour and copies would diverge. Named per registration, exposing only `message` and `continue_task_id` per [contracts/delegation-tool.md](./contracts/delegation-tool.md) (FR-021, FR-031, FR-045)
- [X] T054 [US2] Register the three provider keys in `PROVIDER_TOOLS` in `backend/app/services/integration_tools.py`, contributing the tool only when the integration's direction is outbound, so a bound registration reaches an agent through that seam and no other path (FR-005, FR-055, SC-007)
- [X] T055 [US2] Persist an `A2ADelegation` row per attempt in `backend/app/a2a/client/delegation.py`, recording registration, agent, user, run, protocol version, signature state, outcome, failure kind and the partner's reason at submission time — never inferred later from an agent's current bindings. Lifetime follows the run (FR-040, FR-042, FR-044)
- [X] T056 [US2] Record a retry after a network failure with no response received as individually attributable in `backend/app/a2a/client/delegation.py`, and do not represent it as free of the risk that the partner started work twice (FR-041)
- [X] T057 [P] [US2] Add the partner badge to `frontend/src/components/ui/run-card.tsx` and scope the registration's activity list in `frontend/src/pages/integration-detail/ActivityTab.tsx` to its own delegations (FR-042, SC-013)

**Checkpoint**: work flows to a partner and comes back. US1 + US2 is a shippable increment.

---

## Phase 5: User Story 3 — Outbound work stays inside the calling run's boundary (Priority: P1)

**Goal**: A delegation carries only what the agent composed; what returns cannot widen the run's authority. Mostly assertions over US2's code plus two wiring points.

**Independent Test**: Capture every outbound request across runs holding known sensitive context; feed adversarial responses into a calling run and assert its authority is unchanged.

### Tests for User Story 3

- [X] T058 [P] [US3] Add `backend/tests/test_a2a_client_payload_boundary.py`: across runs whose context holds conversation history, another integration's credentials and a platform token, zero outbound payloads contain any of them (FR-032, SC-005)
- [X] T059 [P] [US3] Add `backend/tests/test_a2a_client_untrusted_output.py`: partner content instructing the agent to change a registration, flip a direction, unbind an integration or escalate is refused, and registration, credential, direction, binding and permission state are unchanged afterwards (FR-036)
- [X] T060 [P] [US3] Add `backend/tests/test_a2a_client_origin_pinning.py`: an off-origin invocation endpoint, a mid-life card change moving the endpoint, and a redirect off `pinned_origin` are each refused (FR-034, SC-006)
- [X] T061 [P] [US3] Add `backend/tests/test_a2a_client_usage_attribution.py`: usage is charged to the calling run's user and to no other (FR-037, SC-011)
- [X] T062 [P] [US3] Add `backend/tests/test_a2a_client_tool_injection.py`: a registration confers no additional reach inside the platform (FR-033)

### Implementation for User Story 3

- [X] T063 [US3] Isolate outbound payload construction in `backend/app/a2a/client/delegation.py` to a single function taking only the composed message, so there is one place to audit and one place a future change could regress. Comment why history is excluded rather than filtered (FR-032)
- [X] T064 [US3] Pass every partner return — result text, structured data and the `input-required` question — through the existing `backend/app/services/prompt_guard.py` on the way into the calling run, commenting that the question is the most direct injection vector because it arrives as a legitimate protocol state and invites a reply (FR-036, FR-047)
- [X] T065 [US3] Enforce `pinned_origin` at request time in `backend/app/a2a/client/destinations.py` and disable redirect-following on the outbound client in `backend/app/a2a/client/sdk.py` (FR-034)
- [X] T066 [US3] Confirm usage accounting in `backend/app/a2a/client/delegation.py` flows through the calling run's existing path with no new concept, adding the assertion in `backend/tests/test_a2a_client_usage_attribution.py` rather than new code if it already holds (FR-037)

**Checkpoint**: the outbound leak paths are closed and asserted.

---

## Phase 6: User Story 4 — Registrations stay trustworthy as cards and credentials change (Priority: P2)

**Goal**: Verdicts stay fresh, a materially changed card suspends delegation until accepted, downgrades are caught, and a credential that can no longer mint a token says so distinguishably.

**Independent Test**: Mutate the stub's card and credentials and assert usability, reason and delegation behaviour change by the next stale-verdict delegation, and in no case later than the sweep interval.

### Tests for User Story 4

- [X] T067 [P] [US4] Add `backend/tests/test_a2a_client_card_drift.py`: a material change suspends delegation with zero sent against the unaccepted card; acceptance resumes and is audit-logged with the fingerprint; accepting a non-current fingerprint or an invalid-signature card is refused (FR-019, SC-008)
- [X] T068 [P] [US4] Add `backend/tests/test_a2a_client_staleness.py`: a verdict inside the threshold is read not re-checked; a stale verdict forces re-verification; and **a stale verdict whose re-verification fails refuses the delegation** rather than falling back (FR-050, SC-017)
- [X] T069 [P] [US4] Add `backend/tests/test_a2a_client_downgrades.py`: losing a previously present signature and ceasing to serve the agreed version each suspend as a material change; gaining a newer supported version renegotiates upward and is audit-logged (FR-052)
- [X] T070 [P] [US4] Add `backend/tests/test_a2a_client_credentials.py`: credentials that can no longer mint a token report `credential_cannot_mint_token`, distinguishable from unreachable and from a partner rejecting a validly minted token (FR-010)
- [X] T071 [P] [US4] Add `backend/tests/test_a2a_client_reverify_job.py` with the `integration` marker: concurrent sweepers on separate sessions divide due rows rather than duplicating them, an interrupted cycle is safe to re-run, and inbound settings rows are never swept (FR-019, constitution Principle IV)

### Implementation for User Story 4

- [X] T072 [US4] Create `backend/app/a2a/client/sweep.py`: claim due **outbound** rows with `FOR UPDATE SKIP LOCKED` in bounded batches, mirroring `reap_stale_tasks`, and write a verdict per row. Comment that a missed cycle is safe because FR-050's threshold is the binding control (research R9)
- [X] T073 [US4] Schedule the sweep in `backend/app/main.py` beside the existing A2A reaper, gated on the deployment switch (FR-007, FR-019)
- [X] T074 [US4] Add the delegation-time staleness check to the pre-flight in `backend/app/a2a/client/delegation.py`: re-verify past the threshold and refuse when that cannot complete, never using the stale verdict. This is the fail-closed rule at the one moment it is tempting to bend, so it carries a rationale comment (FR-050)
- [X] T075 [US4] Add material-change detection and the two downgrade cases to `backend/app/a2a/client/verification.py`, comparing `card_fingerprint` against `accepted_card_fingerprint` (FR-019, FR-052)
- [X] T076 [US4] Add `POST /{integration_id}/accept-card` to `backend/app/api/v1/a2a_client.py`, accepting only the currently observed fingerprint and refusing an invalid-signature card outright, audit-logged with actor and fingerprint (FR-019, FR-039)
- [X] T077 [P] [US4] Surface pending card changes, the suspension reason and an accept action in the verification panel under `frontend/src/pages/integration-detail/`, with strings in `frontend/src/i18n/lang/{en,fr,ja}.json` (FR-018, FR-019)

**Checkpoint**: all four stories independently functional.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T078 **Run the zero-call-site sweep** over every function introduced under `backend/app/a2a/client/`, the three provider files and the direction helpers in `backend/app/a2a/providers.py`, and fix or delete anything with no call site on the implemented path. Look for: a `signature_state` value nothing writes, an `unusable_reason` no path produces, a direction branch no test exercises, a partner state the stub cannot produce (SC-014)
- [X] T079 [P] Add `backend/tests/test_a2a_client_self_federation.py`: expose an agent inbound, register it as an outbound OAuth partner from a second agent, and delegate end-to-end. The only target testable without a vendor account, and the one that exercises both directions at once (FR-045, SC-001)
- [X] T080 [P] Add `backend/tests/test_a2a_client_run_paths.py` exercising delegation from an assignment-triggered run as well as an interactive chat, asserting identical behaviour including the `input-required` round trip (FR-021, SC-014)
- [X] T081 [P] Add `backend/tests/test_a2a_client_platform_parity.py`: the delegation suite runs against a stub serving v1.0 and one serving only the preceding version, with no code change between them (FR-051, SC-018)
- [X] T082 [P] Add `backend/tests/test_a2a_client_audit.py`: registration changes, direction changes, card-change acceptances, bindings and delegations are all attributable to an actor or calling run, a registration and a time (FR-039, FR-040, SC-013)
- [X] T083 [P] Add `backend/tests/test_a2a_client_egress.py`: a non-public or metadata destination is refused by the guard, and a destination the deployment's network policy refuses surfaces as a durable health verdict rather than failing opaquely each run (FR-035, FR-054)
- [X] T084 [P] Add `backend/tests/test_a2a_client_metrics.py` asserting the counters in `backend/app/a2a/metrics.py` are distinct from the inbound ones and each is written from the single place its event occurs (FR-043, SC-016)
- [X] T085 [P] Add `dev-docs/a2a-client-operations.md` mirroring `dev-docs/a2a-server-operations.md`: choosing a direction and what switching costs, registering each of the three mechanisms, what an unsigned card means, reading the health verdict, what suspends delegation, and how to close a breaker
- [X] T086 [P] Update `dev-docs/a2a-server-operations.md` to state that an A2A integration now carries a direction and that only an inbound one exposes an agent — the operator-facing half of the shipped-behaviour change (FR-055)
- [X] T087 Run `ruff check` (with `flake8-bandit`) over `backend/app/` and `backend/tests/`, and `npm run typecheck` + `npm run lint` over `frontend/src/`, until clean
- [ ] T088 Run the [quickstart.md](./quickstart.md) scenarios end to end against the stub, starting with Scenario 0, and record any scenario that cannot be executed as written
- [X] T089 Re-verify the migration gates locally for the new revision in `backend/alembic/versions/`: single head, idempotent re-run, downgrade round-trip, and the backfill re-running cleanly (constitution Principle V)
- [ ] T090 **Manual, pre-release, cannot be automated**: register a real Google Agent Platform agent and a real Azure Foundry agent against a live deployment and delegate one unit of work to each, timing the operator flow from catalog entry to returned result. Record both timings in `dev-docs/a2a-client-operations.md`. The third target is covered by T079; these two need vendor accounts and both surfaces are Preview, so the first real registration is also the first genuine test of those credential flows (SC-001, FR-045)

---

## Implementation Log

### 2026-08-27 — Phase 1 complete, Phase 2 partial (11 / 90 tasks)

Done: T001–T010 and T019. Backend suite **13,834 passed, 0 failed**; `ruff` clean on every touched file; migration applied, downgraded and re-applied against the dev database with the backfill verified idempotent.

Two corrections the design docs did not anticipate, both found by running the code:

- **T002 — the switch is a feature flag, not a setting.** `A2A_SERVER` gates the inbound half through `app/core/feature_flags.py`, a registry built for exactly this and already fail-closed when `ENABLED_FEATURES` is unset. Adding `a2a_client_enabled` to `Settings` would have been a second mechanism for one concept (Principle VIII). `A2A_CLIENT` now sits beside it; the numeric bounds stay in `config.py`, where they belong.
- **T008 — the migration was not lock-safe, and CI caught it.** The first draft argued no lock dance was needed because `CREATE TABLE` locks nothing. True of the new tables, false of the statement: every foreign key locks its *referenced* table, and `integrations`, `agents`, `users` and `assignment_runs` are all hot. `test_lock_taking_ddl_sets_lock_timeout` — the gate the 2026-07-05 outage bought — rejected it. Now follows the canonical recipe.

**T019 did its job.** Changing `binds_a2a` to resolve direction broke **18 shipped inbound tests**, which is the signal the task existed to produce. None was a defect in the new behaviour: 2 were direct calls to the changed signature, and the rest were fixtures binding a random UUID — an integration with no settings row, which the fail-closed rule correctly treats as neither direction. Fixtures now create a real inbound integration; the session doubles answer the direction query explicitly, so a test must *opt in* to exposure rather than getting it by default.

### 2026-08-27 (cont.) — Phase 2 complete (25 / 90 tasks)

T011–T025 done. Backend suite **13,909 passed, 0 failed**; `ruff` clean; migration gates re-verified (single head, idempotent re-run, downgrade round-trip) against the dev database.

Delivered: the direction lifecycle (`create_settings`, `change_direction`, refusal while bound, credential clearing) wired into the integrations API with per-direction feature-flag gating; `google_a2a` added and the two shipped entries given `a2a_directions` plus an `outbound` variant; the SDK containment module, the failure classifier, the destination guard and outbound metrics; and the partner stub every later test builds on.

Two more corrections found by running the code:

- **`bound_integration_ids` spanned only the inbound-capable keys.** With an outbound-only mechanism added, a binding it could not see is a tool the run never gets. It now spans `ALL_A2A_PROVIDERS`; `A2A_PROVIDERS` stays as the inbound-capable set because the `a2a2intmodel1` migration predicate mirrors it and must keep meaning what it meant.
- **Two lists of permitted `a2a_sdk` importers.** `test_a2a_protocol_shaping.py` enforced the old one-module rule and would have disagreed with the new guard the moment either changed — the same two-sources bug FR-053 exists to prevent, in the test suite. The shipped test now defers to the single list in `test_a2a_client_sdk_containment.py`.

### 2026-08-27 (cont.) — US1 backend complete (39 / 90 tasks)

T026–T039 done, less the three frontend tasks. Backend suite **13,955 passed, 0 failed**; `ruff` clean.

Delivered: `verification.py` (fetch, fail-closed verify, negotiate, fingerprint) with the full defective-card matrix passing; `credentials.py` with all three token flows; `registration.py` joining them and writing the verdict; `probe.py` reporting the stored health verdict through the existing connectivity seam; the `google_a2a` provider; and the management API (`GET`, `verify`, `accept-card`) mounted behind its own flag.

Three more corrections found by running the code:

- **A verdict reason was being derived twice.** `verify_card` inspected the destination guard's *message* to pick a reason while the guard already knew one. Two classifications of one failure, disagreeing the moment either changed — FR-053's bug in miniature. `DestinationRefusedError` now carries its reason, and both the verifier and the failure classifier read it. That also split three cases the message-sniffing had flattened: an unencrypted URL, a forbidden address, and a name that will not resolve are three different operator problems.
- **The credential flows went in one module, not three.** The plan put each beside its provider. Together, the property that matters — all three mint a short-lived bearer from a stored non-interactive credential, none stores a bearer — is checkable by reading one file. Split across three inbound-focused provider modules, a fourth target could break that symmetry unseen.
- **A sequenced test double shifted.** `test_a2a_entra_federation.py`'s `_DB` pops answers from a list, so the probe's new direction lookup silently shifted every later answer by one and the tests failed somewhere unrelated. The double now recognises the settings query instead of consuming a queued result.

### 2026-08-27 (cont.) — the delegation loop works (47 / 90 tasks)

T043–T044, T046, T049, T051–T054 done. Backend suite **13,986 passed, 0 failed**; `ruff` clean.

Delivered: `breaker.py` (consecutive-transient counting with backoff, durable failures excluded), `delegation.py` (eight-step pre-flight, submit, poll, continue, cancel), `tool.py` (one shared tool for all three mechanisms), and the wiring into `PROVIDER_TOOLS` that makes it reach a run. 31 new tests covering the five failure classes, every pre-flight refusal, and the `input-required` continuation.

Two more corrections:

- **The tool would never have reached a run.** `_build_integration_tools_inner` skips any binding with no stored token — and an A2A registration has none, because it mints a bearer per call. The delegation tool would have been written, unit-tested and unreachable: the exact failure the inbound phase's review named. The A2A branch now resolves *before* the token step, and `test_a2a_client_tool_wiring.py` pins that ordering.
- **A test matched its own subject's comment.** The ordering assertion searched for `if not token:` and found the phrase inside the explanatory comment of the branch it was checking — failing on correct code. It now matches the statement, and also pins that there is exactly one bindings loop, so a second one appearing (per-user templates, say) cannot silently leave template-bound partners without a tool.

### Remaining

43 tasks. T045/T047/T048/T050/T055–T057 finish US2; then Phase 5 (boundary assertions), Phase 6 (sweep, drift, downgrades), Phase 7 (polish, docs, the zero-call-site sweep), and the three frontend tasks. A bound agent can now delegate end to end against a stub.

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (Phase 1)**: no dependencies. T004 is complete — the FR-014 correction it called for was applied on 2026-08-27
- **Foundational (Phase 2)**: depends on Setup — **blocks every story**. Larger than in the superseded plan because direction is foundational and touches shipped code
- **US1 (Phase 3)**: depends on Phase 2
- **US2 (Phase 4)**: depends on Phase 2 and, in practice, US1 — nothing can be delegated to until a registration can become usable
- **US3 (Phase 5)**: depends on US2; its assertions are about US2's payload and return paths
- **US4 (Phase 6)**: depends on US1. Independent of US2/US3 except that suspension is only observable through a delegation attempt
- **Polish (Phase 7)**: depends on the stories intended for the release

### Story dependencies — stated honestly

The stories are not fully independent. US1 is genuinely standalone: a verified, health-verdicted register. US2 needs US1. US3 is largely a test surface over US2 plus two wiring points (T063, T064). US4 layers onto US1 and is the only P2. The useful independence is at the checkpoint level: US1 alone is demonstrable, US1+US2+US3 is shippable, US4 can be deferred.

### Within Phase 2, order matters

T006 → T007 (same file) → T008 (needs both models) → T009 (needs the settings table) → T010–T012 (need T009's lookup). T013–T017 are parallel with each other and with T009–T012. T018 precedes T019–T025.

### Parallel opportunities

- Phase 1: T003, T004, T005
- Phase 2: T013–T016 in parallel; T020–T025 in parallel after T018. **T006/T007 are not parallel** (same file); **T019 is not parallel** (it gates the shipped-code change)
- Phase 3: T026–T029 in parallel; then T030 with T040–T042; T031–T033 are separate files and parallel, but **T034 touches one catalog file** and is sequential
- Phase 4: T043–T050 in parallel; then T051 → T052 → T053–T056 sequential on `delegation.py`; T057 parallel throughout
- Phase 5: T058–T062 in parallel; T063–T066 sequential on `delegation.py`
- Phase 6: T067–T071 in parallel; T072 parallel with T075
- Phase 7: T079–T086 in parallel; T078 after every other automated task; T090 is manual

---

## Parallel Example: Phase 2 foundational tests

```bash
Task: "backend/tests/test_a2a_direction_switch.py"
Task: "backend/tests/test_a2a_client_models.py"
Task: "backend/tests/test_a2a_client_failure_classification.py"
Task: "backend/tests/test_a2a_client_destinations.py"
Task: "backend/tests/test_a2a_client_sdk_containment.py"
```

Run the backend suite as:

```bash
cd backend && CUSTOM_TOOL_SANDBOX=auto ./.venv/bin/python -m pytest tests/ -k a2a -q
```

---

## Implementation Strategy

### MVP (Setup + Foundational + US1)

T001–T042. The MVP is a register of verified partner agents with a health verdict each, direction enforced end to end, and no outbound traffic beyond card fetches. Useful on its own — an operator can confirm a partner is reachable and correctly configured before any agent can reach it — and it is where benchmark finding F7 is answered.

### Incremental delivery

1. Setup + Foundational → direction live, inbound protected by T019, switch off
2. + US1 → verified register (demonstrable)
3. + US2 + US3 → work flows outward with its leak paths closed (ship these together)
4. + US4 → stays trustworthy over time
5. Polish, with T078 as the gate

### Parallel team strategy

Phase 1 and 2 together, with one developer owning T009–T012 and T019 — the shipped-code change is the riskiest work and should not be split. Then: one on US1 (the largest story), a second on US2's test files as soon as T018 lands, a third on US4's sweep, which touches only `sweep.py` and `main.py`. US3 belongs to whoever wrote US2, since it audits that code.

---

## Notes

- **T019 is the task that protects the shipped product**, and SC-020 is the criterion it answers to. Without a direction-aware exposure hook, binding an integration for outbound purposes exposes the agent inbound. Write it before T010, not after
- **T064 is where a hostile partner is stopped.** The `input-required` question is partner-authored text that invites a reply
- **T068 and T078 are the two not to skip under time pressure.** One asserts the fail-closed rule at the moment it is tempting to bend; the other is the guard the inbound phase's review asked this phase to carry
- [P] means different files and no dependency on incomplete work
- Commit per task or logical group; stop at any checkpoint to validate a story independently
