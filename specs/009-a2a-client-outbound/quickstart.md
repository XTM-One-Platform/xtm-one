# Quickstart: validating the A2A client (outbound)

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Date**: 2026-08-26

How to prove this feature works, end to end, **without a Vertex or Foundry account**. The partner side is stubbed: a local A2A server serving a card and a task surface. That is deliberate — if validating the feature required a vendor Preview account, most of it would never be validated.

The third target needs no stub at all: **this platform's own inbound surface is an OAuth client-credentials partner**, so an outbound registration pointed at a locally exposed agent exercises both halves of the A2A work at once.

That pairing needs one deployment, not two: [../../dev-docs/a2a-review-scenarios.md](../../dev-docs/a2a-review-scenarios.md) walks through what to try, and says how to start it. A deployment cannot reach itself by default — the destination guard refuses loopback, private addresses and plain http — so its own origin has to be named in `A2A_CLIENT_DEV_TRUSTED_ORIGINS`. The inbound surface refuses a transport it cannot confirm encrypted (`403 https required`), so the deployment reaching its own card over plain http from 127.0.0.1 needs `A2A_ALLOW_INSECURE_LOOPBACK`, which exempts real loopback only - never a request carrying `X-Forwarded-For`.

For what the wire exchange and the tool interface must look like, see [contracts/](./contracts/). For field-level rules, [data-model.md](./data-model.md).

## Prerequisites

- Postgres reachable (see the dev-DB notes; the default settings URL has the wrong password for the local instance).
- Migrations at head. The new revision chains onto main's head, which moves with each rebase; `a2a2intmodel1` and `a2a1srv0001` are merged history and are never edited. If Alembic reports multiple heads, the branch revision's `down_revision` needs re-pointing.
- Agentic chat enabled — AI on and **Enterprise Edition** (an XTM One license, or a connected Enterprise Edition platform; `is_chat_enabled`, shared with the inbound half). A deployment without it is the inert case in scenario 1.
- Backend interpreter: there is no `python` on PATH and no repo-root venv.

```bash
cd backend && CUSTOM_TOOL_SANDBOX=auto ./.venv/bin/python -m pytest tests/ -k a2a_client -q
```

## Scenario 0 — Direction, and the inbound regression it could cause (FR-055, FR-057, FR-058, SC-004)

Run this first. It guards the one shipped behaviour this feature changes.

1. Create an integration with direction **outbound** on a mechanism that also serves inbound, and bind it to an agent. **Expected**: the agent has a delegation tool and **no exposure** — no card URL, nothing resolving. An exposure created here is the fail-open regression this scenario exists to catch (SC-020).
2. Create one with direction **inbound** and bind it. **Expected**: exposure created exactly as before this feature, and **no delegation tool**.
3. Delete the settings row directly, leaving an A2A integration with no direction. **Expected**: neither exposed nor tool-bearing — a missing row degrades to "not exposed", never to an unintended exposure.
4. Attempt a direction change while bound. **Expected**: refused, naming the bound agents. Unbind and retry. **Expected**: succeeds, and the previous direction's credentials are gone.
5. Repeat steps 1–2 by **creating** an agent with bindings already set, not by editing an existing one — `_sync_new_agent_exposure` serves that path separately and SC-020 covers both.
6. Run the existing inbound suite (`backend/tests/test_a2a_*.py`, excluding `_client_`). **Expected**: unchanged.

## Scenario 1 — Nothing is reachable by default (FR-007, FR-005, SC-004)

The first check is that the feature is inert. Before any registration exists:

1. Without agentic chat (AI off, or Community Edition), a bound agent must have no delegation tool at all and no outbound request must be attempted.
2. With chat but no registration bound, an agent must still have no reach.

**Expected**: zero outbound requests in both cases; the tool absent from the run's tool list rather than present-and-refusing. A tool that appears and then refuses teaches a model to retry; an absent tool cannot be called.

## Scenario 2 — Register a stubbed partner and read what it declares (FR-011–FR-020, SC-001, SC-003)

Point a registration at the stub's card URL with credentials the stub accepts, then read `GET /api/v1/a2a/client/{integration_id}`.

**Expected**: `usability: usable`; the card's declared name, description, skills and content types visible before anything is bound; `agreed_protocol_version` recorded; `signature_state` either `verified` or `unsigned` and never absent.

Then walk the defective-card matrix, re-serving the stub's card each time. Each row must produce its own reason, not a generic failure:

| Stub serves | Expected |
|---|---|
| Nothing (connection refused) | `unusable[card_unfetchable]` |
| Malformed JSON | `unusable[card_malformed]` |
| Plain HTTP, or an unverifiable TLS chain | `unusable[transport_unverified]` — refused, never downgraded to |
| A signature that does not verify | `unusable[signature_invalid]`, and **no** operator action can clear it |
| No signature | `usable`, `signature_state: unsigned` |
| A card whose invocation endpoint is on another origin | `unusable[origin_mismatch]` |
| Only a protocol version the platform does not speak | `unusable[no_common_protocol_version]` |
| A card URL on a loopback, link-local or metadata address | refused by the destination guard |

The last row is worth running against `169.254.169.254` specifically: the card URL is operator-supplied and fetched by the backend with stored credentials, so it is a real SSRF surface (research R6).

## Scenario 3 — Delegate one unit of work (FR-021–FR-029, SC-002, SC-009, SC-010, SC-016)

Bind the registration to an agent, then run the agent so it calls the tool.

**Expected**: the stub receives one request carrying **only** the composed message; the delegation follows the partner task to terminal; the result returns into the run with `card_provenance` and `protocol_version` present.

Repeat against **this platform's own inbound surface**: expose an agent inbound, register it as an outbound OAuth partner from a second agent, and delegate. The only target testable without a vendor account, and the only one exercising both directions at once.

Then make the stub misbehave, once per failure class. Each must arrive as its own `reason`, never as an undifferentiated error:

- never answers → `timeout` after the ceiling (SC-010), and the stub must observe a **cancel** for the task
- refuses the token → `credential_rejected`
- unreachable mid-exchange → `unreachable`
- reports task failure → `partner_failed` with the partner's reason preserved
- declares content types the exchange cannot handle → `content_type_mismatch`, refused **before** submitting

## Scenario 4 — The `input-required` round trip, on both run paths (FR-031, FR-046–FR-048, SC-014)

Have the stub answer `input-required` with a question.

**Expected**: control returns to the model with the question and a `continue_task_id`; a second tool call continues **that same task** rather than creating a new one; no human is waited on.

Then the three cases that make this trustworthy rather than merely working:

1. **Run it from an assignment-triggered run as well as an interactive chat.** Identical behaviour is the point — FR-021 admits both paths, so both must be exercised. This is the specific check the inbound phase's review asked for.
2. **Continuation ownership**: a `continue_task_id` naming another run's delegation must be refused. That id arrives inside partner-controlled content, so this is a boundary test, not a validation nicety.
3. **Turn farming**: a stub that answers `input-required` forever must exhaust a run budget and end with a defined outcome, with `continuation_count` recording how many rounds happened. If the stub can loop indefinitely, FR-048 is not implemented.

## Scenario 5 — Boundary and payload (FR-032–FR-036, SC-005, SC-006, SC-011, SC-015)

Capture every outbound request while running an agent whose context deliberately holds sensitive material — conversation history, another integration's credentials, a platform token.

**Expected**: zero payloads contain run history, credentials, API keys, session material or a cross-platform identity token. Then:

- redirect the stub off-origin → not followed
- return content instructing the agent to change a registration, unbind an integration or escalate → refused; registration, credential, binding and permission state unchanged afterwards
- return a response exceeding the ingest bound, a response for a task we never submitted, and a task state unknown to the agreed version → each refused **whole**, not partially interpreted
- check usage accounting → charged to the calling run's user and to no one else (SC-011)

## Scenario 6 — Staying trustworthy over time (FR-019, FR-050–FR-053, SC-008, SC-013, SC-017, SC-018, SC-019)

**Card change**: mutate a material field on the stub's card. Delegation must suspend at the next delegation that finds the verdict stale, and in no case later than the sweep interval. Accepting the change must resume it, and the acceptance must be audit-logged with the fingerprint accepted.

**Staleness, both halves**:
- hold a verdict past the threshold and let re-verification succeed → delegation proceeds against the fresh verdict
- hold a verdict past the threshold and make the card endpoint unreachable → the delegation is **refused**. A delegation that proceeds here is the single most important failure this feature can have: it is the fail-closed rule bending at the one moment it is tempting (FR-050)

**Downgrades**: a stub that drops its signature, and one that stops serving the agreed protocol version, must each suspend rather than silently renegotiate. A stub that *gains* a newer supported version must renegotiate upward without suspension.

**Breaker**: N consecutive transient failures must open it, with a cooldown that lengthens on repeat. Then the three properties that make FR-053 real:
- a durable failure (rejected credential) must **not** increment the transient counter — it must mark the registration unusable directly
- the health verdict and the breaker must agree about the same partner, after each failure class
- a successful on-demand verify must close the breaker immediately, without waiting out the cooldown

**Two registrations on one agent**: one failing partner must not suppress the other, and each registration's activity must show only its own delegations (SC-013).

**Version declared, never defaulted**: run the whole scenario twice — once against a stub serving v1.0 and once against a stub serving only the preceding version — with no code change between them. Every request must declare the agreed version explicitly (SC-018).

**Per-target parity**: SC-001 is measured on all three targets. Two need vendor accounts and are covered by the manual pre-release task; the OAuth one runs here against our own inbound surface.

## Scenario 7 — The zero-call-site sweep (SC-014)

Not a runtime test. Before declaring the feature done, sweep every function introduced by it for call sites on the implemented path.

The inbound phase shipped three requirements as functions that were written, documented and unit-tested but never called — and their passing tests read as assurance. The specific shapes to look for here: a `signature_state` value nothing ever writes; a `unusable_reason` in the closed set that no code path produces; a run path FR-021 admits but no test exercises; a branch for a partner state the stub cannot produce.

A function with no call site and a green test is worse than no function at all.

## Migration checks (constitution Principle V)

The migrations CI job covers these, but run them locally before pushing:

- single head after the new revision
- idempotent re-run — the body is safe to apply twice
- downgrade round-trip — `downgrade()` works and `upgrade` re-applies cleanly
- no table rewrite and no non-`CONCURRENTLY` index build; the new tables are empty on arrival, so this is naturally lock-light

## Definition of done for this quickstart

Every scenario above passes, plus:

- `ruff check` (with `flake8-bandit`) clean; `tsc --noEmit` and `eslint` clean
- new strings present in `en`, `fr` and `ja`
- no credential appears in any log, health message, API response or audit record, at any point in a registration's life (SC-012)
