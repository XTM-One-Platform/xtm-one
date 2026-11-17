# Quickstart: validating the AI-disabled platform mode

Scenarios that prove the feature works, in the order worth running them. Each maps
to success criteria in [spec.md](./spec.md); the coverage map at the end says which.

## Prerequisites

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build   # full stack (platform :8100, frontend :3100), migrated on startup
```

Stop with `docker compose -f docker-compose.yml -f docker-compose.dev.yml stop`. The platform and the worker restart on every change
under `backend/app`.

---

## Scenario 1 — A fresh install seeds nothing (SC-001)

The load-bearing one. Everything else assumes this works.

```bash
# A database with no prior content, so "empty" means "never created"
PLATFORM_MODE=ai_disabled docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

1. **Count, do not glance.** Assert zero rows in each seeded table — built-in
   agents, assignments, templates, skills, integrations, knowledge bases, intent
   bindings, the example flow, template containers. A visual check on the UI passes
   for the wrong reason: the lists are empty on a fresh install regardless.
2. Confirm the log states what was skipped and why. Silence is a defect (FR-005) —
   an operator debugging an empty platform needs the reason in the log.
3. Let it run for several scheduler intervals. Assert **zero** dispatches of
   assignments, evaluations, knowledge-base syncs and rule syncs.
4. Assert **zero** requests to any model or embedding provider. The cheapest
   reliable check is to point the provider at a closed port and confirm nothing
   logs a connection attempt — a missing-provider *error* means something tried.
5. Confirm the platform is usable: sign in, create a user, register a connected
   platform, read the audit log.

!!! warning "The trap that makes this scenario look passed"
    An install with AI enabled but no provider configured also produces an empty-ish
    UI and no successful AI work. The difference is whether anything **tried** —
    failed runs, provider errors, scheduler activity. Assert the counts, not the
    appearance.

---

## Scenario 2 — The runtime toggle, both ways (SC-002, SC-003, SC-010)

```bash
# Start from Scenario 1's state: AI off, nothing seeded
```

1. Turn AI on from Settings → Parameters. **No restart.**
2. Assert the built-in content now exists, matching what a normal boot creates —
   same counts, and the example flow present.
3. Turn it on **again**. Assert nothing duplicated and nothing failed (FR-012).
4. Turn it off. Within one scheduler interval, assert no new dispatch and that a new
   execution attempt is refused.
5. Start a long-running agent run, then turn AI off while it is running. Assert it
   **completes** rather than being cancelled, and that the confirmation dialogue said
   so before you confirmed (FR-013).
6. Off → on → off → on. Assert no duplicated content and no orphaned schedule
   (SC-010).
7. Check the audit trail names who changed it, when, and to what, for every one of
   those changes (SC-009).

---

## Scenario 3 — `NULL` follows the deployment default (FR-003)

The one whose failure is invisible, so worth doing deliberately.

1. On an install where the toggle has **never** been touched, confirm the stored
   value is `NULL`.
2. Change `PLATFORM_MODE` between `ai_disabled` and `xtm_one` and restart. Assert the
   platform follows it each time.
3. Now set the toggle explicitly. Change `PLATFORM_MODE` again and restart. Assert
   the platform **ignores** it — the explicit choice wins.
4. Use "reset to platform default". Assert the stored value is `NULL` again and the
   platform follows `PLATFORM_MODE` once more.

!!! warning "`false` is not `null`"
    A client that sends `false` for "reset" pins the platform to enabled and hides
    every later change to the deployment default. Step 4 is what catches that, and
    nothing in the UI would look wrong if it were broken.

---

## Scenario 4 — Reads work, writes and executions do not (SC-004)

With AI disabled and **existing** AI data present (enable AI, let it seed, disable
again — otherwise there is nothing to read).

1. Every AI listing and detail view returns `200` and renders.
2. Every create, change, delete and execution returns `403` with
   `code: "ai_disabled"` — the same payload everywhere
   ([contracts/refusal-payload.md](./contracts/refusal-payload.md)).
3. **No route 500s**, and none silently succeeds while doing nothing.
4. Repeat step 2 through a **non-interactive** path — an API token, an integration —
   and assert the same payload, not a vaguer error (FR-010).
5. Call the MCP endpoint. Assert it refuses entirely, with this payload, and is still
   discoverable rather than absent (FR-029).

---

## Scenario 5 — Deterministic flows keep running (SC-005)

1. Build a flow of non-AI steps only — schedule trigger, HTTP request, notify. With
   AI disabled, trigger it by schedule, then by webhook, then by event. Assert it
   completes normally each time.
2. Add one AI step. Trigger it. Assert it is **not dispatched**, is recorded as
   skipped with a readable reason in Operations, and that **zero** steps executed —
   check for side effects of the steps *before* the AI one, which is where a
   part-way start would show.
3. In the flow editor, assert AI steps appear locked rather than absent, and that a
   flow already containing one shows why it will not run.
4. Take a flow with an AI step, remove it, trigger again. Assert it now runs — the
   verdict comes from the current graph, not a stored one.
5. **The ambiguous ones**: a tool call with model-chosen arguments, guardrails,
   structured output. All three count as requiring AI (FR-030). Assert each is held
   back.

---

## Scenario 6 — The disabled state is loud, not hidden (SC-006, SC-007, SC-011)

Run this as a **non-administrator**, and on **both** empty states.

1. Every AI area is listed in navigation and reachable. None is removed (FR-017) —
   removal is the natural-looking implementation and the wrong one.
2. In each, actions needing AI are visibly unavailable with a consistent
   explanation, and cannot be started (FR-018).
3. The home screen states that AI is disabled, why, and what enabling it needs.
4. The state is visible on every screen, including for non-administrators (FR-020).
5. It is stated on the sign-in screen, **before** authenticating (FR-011).
6. Settings: users, groups, invitations, audit logs and platform registration are
   fully functional; the AI sections render read-only beside the toggle (FR-022).
7. **The judgement call (SC-011)**: ask someone unfamiliar with the deployment
   whether AI is broken or deliberately off. They should answer from the interface
   alone, without reading logs or docs. This is the one criterion no assertion
   covers, and the one the feature exists for.

---

## Scenario 7 — Nothing is destroyed (FR-025, SC-008)

1. With AI enabled, note the agents, flows, knowledge bases, runs, traces and
   conversations that exist.
2. Disable AI. Confirm all of it still reads.
3. Re-enable. Confirm nothing was lost and no manual repair was needed.
4. Throughout, confirm non-AI administration succeeds at the same rate as with AI
   enabled (SC-008).

---

## Full gate before pushing

```bash
cd backend && .venv/bin/python -m ruff check app/ tests/
cd backend && pytest -m "not integration" && pytest -m integration
cd frontend && yarn tsc --noEmit && yarn lint
```

Ruff is a blocking gate and is easy to skip when the tests are green.

## Coverage map

| Scenario | Success criteria |
|---|---|
| 1 | SC-001 · FR-005, FR-006, FR-007, FR-021 |
| 2 | SC-002, SC-003, SC-009, SC-010 · FR-012, FR-013, FR-023 |
| 3 | FR-003 — the failure with no visible symptom |
| 4 | SC-004 · FR-008, FR-009, FR-010, FR-029 |
| 5 | SC-005 · FR-014, FR-015, FR-016, FR-030 |
| 6 | SC-006, SC-007, SC-011 · FR-011, FR-017–FR-020, FR-022, FR-027 |
| 7 | SC-008 · FR-025 |
| Not covered here | FR-031 (licensing/telemetry) — a commercial decision, not a runnable check |
