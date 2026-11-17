# Quickstart: Validating the Minimal A2A Server

**Date**: 2026-08-18 | **Plan**: [plan.md](./plan.md)

How to prove the feature works end to end. Scenarios map to the spec's user stories and success criteria; details live in [contracts/](./contracts/) and [data-model.md](./data-model.md) rather than being repeated here.

## Prerequisites

- Postgres, Redis and MinIO running (the repo's usual compose stack)
- Migrations applied
- The backend venv — there is no `python` or `pytest` on PATH

## Licensing

A2A is offered exactly while agentic chat is (`is_chat_enabled`: AI on, Enterprise Edition), one rule for both directions. Otherwise every A2A route answers `403`; a deployment with chat needs nothing enabled.

Behind a reverse proxy, set `TRUST_PROXY_HEADERS=true` — otherwise the application cannot confirm the connection is encrypted and will refuse every card fetch (FR-017, research R10).

## Running the tests

From `backend/`. Two things are not obvious: the interpreter lives in the local venv, and the shell's exported `CUSTOM_TOOL_SANDBOX` value is rejected by `Settings`, so it must be overridden per invocation or collection fails at import.

```bash
CUSTOM_TOOL_SANDBOX=auto ./.venv/bin/python -m pytest tests/ -k a2a -q
```

Integration-marked tests (real Postgres/Redis) are deselected by default:

```bash
CUSTOM_TOOL_SANDBOX=auto ./.venv/bin/python -m pytest tests/ -k a2a -m integration -q
```

If unrelated failures appear in `test_mfa_totp.py` or the auth/SSO tests, the venv is stale rather than the branch broken — `uv sync` in `backend/`.

## Validation scenarios

### 1. Exposure authority — no protocol surface needed (User Story 1)

Prove authority before anything is externally reachable:

- An agent's owner can expose it; a user with no management rights cannot, and the denial is audit-logged.
- A group member holding the `config` tier on someone else's agent **can** expose it, and the owner is notified (FR-005).
- A company-managed agent is refused for a non-administrator and accepted for an administrator.
- A **built-in** agent behaves exactly like any other company-managed one: accepted for an administrator, refused for everyone else (FR-044). Then confirm the read-only rule is otherwise intact — the same administrator must still be refused when the request changes any seeded field (persona, name, tools, model), whether alone or alongside the binding. A gate that admits the change by checking only which fields are *present* passes the first half of this and fails the second.
- Flipping the agent between personal and company-managed revokes the exposure **in both directions** (FR-041) — the direction that narrows must revoke just as the widening one does. This is the scenario most likely to be implemented for the widening case only.

### 2. Card discovery (User Story 2)

```bash
curl -sS https://<host>/a2a/<card_token>/.well-known/agent-card.json
```

Expect a v1.0-shaped card when no `A2A-Version` header is sent, and a v0.3-shaped one when `A2A-Version: 0.3` is (FR-027). Then confirm the negative paths are indistinguishable: an unknown token, a revoked exposure and a disabled agent must all return the **same JSON 404**.

> Check this against a deployment with the SPA catch-all installed, not only in unit tests. Without `"a2a"` in `_API_NAMESPACES`, the 404 arrives as HTTP 200 + HTML and a partner's registration tooling fails on parse (research R2).

Probe the deployment root and any listing path — nothing may enumerate exposed agents (FR-013, SC-006).

### 3. Partner tasking (User Story 3)

Obtain a token via client credentials, then `message/send`, poll `tasks/get` to a terminal state, and `tasks/cancel` a second task. Then the negatives:

- A `fcp-` platform key is rejected and audit-logged (FR-020).
- Credentials for exposure A presented against exposure B return the uniform not-found, not a 403 (FR-040).
- Re-sending the same message id returns the **existing** task and starts no second run (FR-043).

### 4. Boundary containment — the one that matters most (User Story 4, SC-003)

Run the adversarial probe against four configurations, because a single-configuration pass proves very little here:

| Configuration | Must not reach |
| --- | --- |
| Personally-owned agent | anything outside the owner's own three-tier visibility |
| Company-managed agent | anything outside the run-as user's own three-tier visibility — that identity is usually inherited from whoever seeded the agent, so check what it actually reaches rather than what the agent is called |
| Agent exposed by a group member, not the owner | anything in the *exposer's* boundary that is outside the *owner's* |
| Exposure owned by an administrator | anything beyond that user's explicit visibility — `is_admin` must not widen |

The third and fourth rows are where a plausible-looking implementation typically fails: passing the executing user through to `user_visible_filter` without pinning `is_admin=False` silently restores full-platform reach.

Then run the **inverse** probe, which fails in the other direction and is easy to miss because nothing errors: expose a built-in and confirm the partner actually reaches its OpenCTI / OpenAEV tools. Every built-in binds those per-user rather than to a fixed integration (FR-045), and the session scope handed to a run *replaces* the resolved set — so an implementation that passes only the fixed bindings answers the partner from an agent with no product data, successfully, with a plausible reply and no failure anywhere to look at. Confirm the resolution runs against the exposure's run-as user: a provider that user has no eligible integration for must be absent, never satisfied from someone else's account.

### 5. Re-exposure (FR-042)

Expose, revoke, expose again. Replay every previously issued URL and credential — all must stay dead. The new pair must differ from the old.

## What is deliberately not validated

No latency or throughput target is checked. The spec withdraws that claim; per-registration rate limiting is the sole control on external load, bounded by exposures × limit. Verify the limits refuse as configured and that a Redis outage refuses rather than admits (research R8) — that fail-closed behaviour diverges from the platform default and is easy to regress.
