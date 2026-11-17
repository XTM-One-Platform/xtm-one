# Implementation Plan: OpenAEV Tenant Scoping

**Branch**: `fix/5335-openaev-tenant-write-scope` | **Date**: 2026-10-05 | **Spec**: [spec.md](spec.md)

## Summary

Scope every OpenAEV tool request to the default tenant in one place in `providers/openaev/_common.py`, using the per-tool-set `PlatformCapabilities` that `__init__.py` already builds. `_api` scopes by default; the 27 direct `safe_request` call sites (26 in `core.py`, 1 in `content.py`) go through a same-signature scoped wrapper. Decisions and evidence: [research.md](research.md).

## Technical Context

**Language/Version**: Python 3.13 (backend)

**Primary Dependencies**: httpx, existing `app.integrations.http` (`safe_request`, `RefreshableHeaders`)

**Storage**: N/A

**Testing**: pytest (`backend/tests/test_openaev_package_tools.py`; new `test_openaev_tenant_scoping.py`)

**Target Platform**: Linux server

**Project Type**: web-service (backend only)

**Performance Goals**: at most 1 `GET /api/settings` per tool set (SC-003)

**Constraints**: source files are CRLF, preserve; no change to the shared `RefreshableHeaders` class (OpenCTI also uses it)

**Scale/Scope**: ~20 modules, ~200 call sites, no per-site edits except the 27 direct calls

## Constitution Check

| Principle | Result |
|---|---|
| I Assignment-Driven | N/A, no new automation |
| II Fail-Closed | Does not apply: tenant routing is correctness, not a security/quota check; the server enforces tenancy. Fail-open on lookup failure is deliberate (FR-006, [research.md](research.md) R4) |
| III Security by Default | Pass, no secrets or new endpoints; scoped path is built from a server-reported id, not model input |
| VI Quality Gates | Pass, unit tests + `ruff check app/` |
| VII Rationale-Documented | Pass, the exemption list and fail-open choice carry why-comments |
| VIII Shared Utilities | Pass, this is the shared utility; per-module wrappers (the issue's patch) rejected |
| XI Minimal Writing | Pass |
| IV, V, IX, X | N/A |

Re-check after design: unchanged.

## Project Structure

```text
specs/026-openaev-tenant-scoping/
├── plan.md  research.md  data-model.md  quickstart.md
├── contracts/path-scoping.md
└── tasks.md   # /speckit-tasks
```

```text
backend/app/integrations/providers/openaev/
├── _common.py      # scoping primitive, _api default, scoped_safe_request, PlatformCapabilities tri-state
├── __init__.py     # attach the existing capabilities to headers
├── core.py         # safe_request -> scoped_safe_request (26 sites)
└── content.py      # upload site (1) scoped explicitly
backend/tests/
├── test_openaev_tenant_scoping.py   # new
└── test_openaev_package_tools.py    # 3 tests patch core.safe_request by name
```

**Structure Decision**: edit the existing OpenAEV provider package; no new modules.

## Design

1. **Resolver on headers.** `_headers()` returns `OpenAEVHeaders`, which declares `tenant_scope`; `__init__.py` sets it to `capabilities`, so ~200 `_api` call sites need no edit. Headers without a resolver (read-only collectors) stay unscoped.
2. **One primitive.** `_scope_path(headers, path)` returns the scoped path per [contracts/path-scoping.md](contracts/path-scoping.md). `_api` applies it when the caller passed no explicit `tenant_id` (Threat Arsenal and chaining keep theirs, FR-009).
3. **Direct callers.** `scoped_safe_request` has `safe_request`'s signature and scopes the URL using its `headers` kwarg; `core.py` swaps the import. `content.py` sends stripped headers, so it calls `_scope_path` with the full ones.
4. **Tri-state lookup.** `PlatformCapabilities` gains a lookup that reports `{id | none | failed(cause)}`, serialised so concurrent first callers share one fetch, and caches the failure as a failure. The probe retries transient 5xx. `tenant_id()` keeps its contract for existing callers.
5. **Failure hint.** When the lookup failed and a request is rejected with `TENANT_WRITE_SCOPE` / `TENANT_SELECTOR_REQUIRED`, `_api`, `scoped_safe_request` and the upload append the cause (`_with_scope_hint`).

## Risks

- **Reads narrow.** A tenant-scoped read pins one tenant; today's base read spans the token's whole scope. Accepted with FR-002; called out in the PR.
- **Test patch targets.** Three tests patch `core.safe_request`; they move to the new name.
- **Fake `_api` in tests** ignore scoping; coverage comes from the new tests exercising the real `_api` against a stub transport.
