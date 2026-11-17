# Implementation Plan: XTM Hub sign-in entitlement gate

**Branch**: `feat/2952-xtm-hub-oidc-platform-id` | **Date**: 2026-08-19 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/007-xtm-hub-oidc-gate/spec.md`

## Summary

When an operator names which XTM One instance this is, every OIDC sign-in must
carry that instance's own entitlement claim or be refused — before any account
is created — and an entitled person's role is read from that same claim. The
claim contract was pinned against a live Hub token (see
[research.md](./research.md)); it is a per-instance claim whose *presence* is the
entitlement, so there is no value comparison.

The whole feature is one guard in `oidc_callback`, two settings, a rejection
redirect, and a login-page branch. **No schema change**: a refusal is attributed
to the account it concerns when one exists, and logged when it does not.

## Technical Context

**Language/Version**: Python 3.14 (backend — prod and CI; ruff lints at py313 so
contributors on 3.13 are not broken), TypeScript 5 / React 19 (frontend)

**Primary Dependencies**: FastAPI, SQLAlchemy 2 async, Authlib (`AsyncOAuth2Client`), Alembic; Vite + React Router on the frontend

**Storage**: PostgreSQL — existing tables only, no migration. `audit_logs` written through its current shape; `users` role written as today

**Testing**: pytest (unit), pytest with the `integration` marker for real-Postgres behaviour; Playwright for the login page

**Target Platform**: Linux server, multi-replica

**Project Type**: Web application (backend + frontend)

**Performance Goals**: No measurable change to sign-in latency — the check reads claims already in memory and adds no network call

**Constraints**: Must be inert when unconfigured; must not lock an operator out of their own instance under any configuration or Hub-side change

**Scale/Scope**: One callback function, one setting, no migration, one frontend branch

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- [x] **Fail-Closed by Default** (Principle I): the gate denies on absence. A
      missing claim, an unreadable claim, or a claim of the wrong shape all
      refuse the sign-in rather than admitting the person. The one place this
      principle cuts the other way is deliberate and recorded: an operator who
      enables the gate without the configuration to evaluate it gets a **startup
      failure**, not a running instance that refuses everybody — refusing to
      boot is itself the fail-closed behaviour the constitution names for
      unrecognised configuration.
- [x] **Concurrency-Safe, Multi-Replica by Design** (Principle II): no new
      scheduled job, startup task, shared counter, or migration. Nothing about
      this feature differs between one replica and many.
- [x] **CI-Enforced Quality Gates** (Principle III): unit tests for the decision
      logic (pure, no I/O) and for the attribution fallback. No migration, so the
      migration CI job is not engaged. Frontend change is covered by `tsc`,
      eslint, and the existing login Playwright spec.
- [x] **Rationale-Documented Complexity** (Principle IV): three decisions carry
      "why" comments in the code — why the check sits above
      `_get_or_create_sso_user`, why the platform admin is exempt from demotion,
      and why the instance value is not read from `PLATFORM_ID`.
- [x] **Backward-Compatible, Auditable Evolution** (Principle V): no schema or
      state change ships, so there is no prior state to interpret and nothing to
      roll back.

## Project Structure

### Documentation (this feature)

```
specs/007-xtm-hub-oidc-gate/
├── spec.md
├── plan.md              # this file
├── research.md          # the pinned claim contract and the decisions taken
├── data-model.md        # audit record shape; no new entities
├── contracts/
│   └── oidc-entitlement.md
├── quickstart.md
└── checklists/requirements.md
```

### Source Code (repository root)

```
backend/
├── app/
│   ├── api/auth_sso.py                  # the guard, in oidc_callback
│   ├── core/config.py                   # two settings + startup validation
│   ├── services/audit.py                # allow a user-less record
│   └── models/audit_log.py              # user_id becomes nullable
└── tests/
    ├── test_xtm_hub_entitlement.py      # decision logic, unit
    └── test_xtm_hub_entitlement_postgres.py  # audit write, integration

frontend/src/pages/Login.tsx             # render the sso_error branch
```

## Design

### 1. Configuration (`core/config.py`)

One setting, empty by default:

- `xtm_hub_admin_group` — the marker meaning "administrator", matched exactly
  including case. **Setting it is what enables the gate.**

The instance is identified by the existing `settings.platform_id`, which is what
the claim is named after. The switch is deliberately the admin marker rather
than the identifier: `platform_id` is set for clone/restore identity continuity,
entirely unrelated to XTM Hub, and every deployment that forces one would
otherwise start refusing everybody.

A model validator (cross-field, so not a `field_validator`) refuses to start
when the marker is set and no identifier resolves — the gate would otherwise
refuse every sign-in, each refusal indistinguishable from an ordinary
unentitled person.

The gate reads `platform_id` and nothing else. Pointing a test instance at a
Hub tenant that still issues non-UUID names is a local concern of whoever runs
that test, not a deployment setting, and so is deliberately absent from this
plan.

### 2. The decision (pure function, `auth_sso.py`)

`_resolve_hub_entitlement(userinfo) -> HubEntitlement`, with no I/O so it is
exhaustively unit-testable. The outcome is a `HubStatus` `StrEnum`:

- no admin marker configured → `DISABLED`, and the caller behaves exactly as today
- claim `f"{platform_id}_groups"` absent → `REFUSED`
- claim present → `ENTITLED`, `is_admin = admin_group in claim_list`

A claim of an unexpected type (not a list) is treated as a refusal, not coerced.

An enum rather than the `Literal[...]` alias used elsewhere in the codebase for
small closed sets: `Literal` is a type-checker construct and this repository runs
none, while the comparison that matters (`is HubStatus.REFUSED`) fails **open**
when mistyped — the guard would be skipped and everyone admitted. A wrong member
name raises `AttributeError` on the line instead.

### 3. The guard (`oidc_callback`)

Placed between the `if not email:` guard and `_get_or_create_sso_user(...)`,
because that call is what writes the row. On refusal: record the audit event,
then `RedirectResponse(f"{frontend_url}/login?sso_error=not_authorised")`.

A query string rather than the fragment used for the success token: the existing
fragment rationale is specifically about *tokens* not reaching servers, logs, or
`Referer` headers. An error code is not a secret, and the login page needs it on
first paint.

### 4. Role, and the exemption

For an entitled person the role is set from the claim on **every** sign-in,
including demotion — the Hub is authoritative. This deliberately does not reuse
`_apply_auto_admin`, which is documented as promote-only; the two rules would
contradict each other.

The platform admin (`settings.admin_email`) is never demoted. Without this, a
group change made in the Hub can remove the operator's access to their own
instance, and the operator has no way to grant it back through the gate.

### 5. Recording a refusal

`audit_logs.user_id` and `user_email` are both `NOT NULL`, so a refusal can only
be stored when there is an account to attribute it to. The refusal therefore
looks up the address it was given:

- **account exists** → an ordinary audit row against that account, recording
  that a sign-in was refused. This is the case worth investigating: someone who
  had access and lost it, or someone probing a real account.
- **no account** → a structured log line. Nothing is created merely to have
  somewhere to write, and no shared table is widened to admit rows belonging to
  nobody.

The lookup is one indexed read on an address already in hand, on a path that is
about to do the same lookup anyway when it succeeds.

A refusal can only follow a *successful* authentication at the identity
provider, so an unknown address here is a person the Hub vouched for but did not
entitle — routine, not hostile. That asymmetry is what makes the narrower
durability acceptable, and it is recorded as FR-009a rather than left implicit.

### 6. The login page

`Login.tsx` already uses `useSearchParams`. Add a branch that renders a plain
message for `sso_error=not_authorised` and scrubs the parameter, matching how it
already scrubs the token fragment.

## Complexity Tracking

| Deviation | Why needed | Simpler alternative rejected because |
|---|---|---|
| Not reusing `_apply_auto_admin` | It is documented promote-only, and this feature requires demotion | Changing `_apply_auto_admin` itself would silently make every SSO deployment demote users, which is a behaviour change nobody asked for |
| Two settings rather than reusing `PLATFORM_ID` | `PLATFORM_ID` is UUID-validated at startup and re-keys the `platform_settings` primary key; the Hub sends non-UUID names | Relaxing that validator would weaken an identifier used for telemetry continuity and clone/restore parity |
