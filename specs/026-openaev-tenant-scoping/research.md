# Research: OpenAEV Tenant Scoping

## R1 — What is actually rejected

OpenAEV's `TenantWriteScopeResolver` throws `TENANT_WRITE_SCOPE` (HTTP 400) on a **create** whose request scope holds ≠1 tenant and carries no selector. A `/api/tenants/{id}/...` path pins the scope to one tenant, which is why the twin works.

- **Decision**: scope by path, as the spec says.
- **Consequence**: updates/deletes may already pass on base routes; scoping them anyway keeps one rule (FR-002) instead of a per-verb list.

## R2 — Twin coverage (audit of tool paths vs OpenAEV controllers)

55 distinct `/api/<family>` prefixes in the tools were checked against `~/Projects/openaev` (`@RequestMapping` with `TENANT_PREFIX`).

- **Base-only, no twin**: `settings` (incl. `/settings/*`), `platform-users`, `platform-roles`, `platform-groups`, `schemas`, and `me/*` (`PUT /me/profile`, `GET /me/tokens`, `/me/password`, ...; only `GET /me` has a twin and it ignores the tenant). A blanket rewrite would 404 them → FR-010.
- **Twin exists but must not be used**: `autonomous-runs`. Its callbacks are `@RunTenantScope`: on the non-prefixed route OpenAEV derives the tenant from the run for the verified XTM One service identity; the prefixed route pins the path tenant and a run owned by another tenant would 404 (`TxCtxArgumentResolver.runTenantScope`, `OrchestratorRunTenantInterceptor`). Exempt (FR-010).
- **Dead surfaces**: `reports`, `notification-rules` (deleted in v3, referenced only in comments).
- **Tenant-only, already scoped**: `chaining`, `workflows`.
- **All others** have twins.

Re-run on OpenAEV `726858530` (2026-10-05, after a pull): same result; `organizations` kept its twin.

Re-checked per ROUTE: every `@Get/Post/Put/Delete/PatchMapping` in `~/Projects/openaev` was resolved against its class-level mapping and compared with the paths the tools call (a family-level grep missed `autonomous-runs` and `me/*`). Of 95 base-only endpoints only `me/profile`, `me/tokens` and the exempt families are called by a tool; `POST /injectors|collectors|executors` is base-only but the tools only `GET` them. Method is not part of the comparison, so a verb mismatch would show as a collision; the new test asserts the exemption list, and the live check in [quickstart.md](quickstart.md) covers the rest.

## R3 — Where to scope

| Option | Verdict |
|---|---|
| Per-module `_tapi`/`_tsafe_request` (issue patch) | Rejected: duplicates `_tenant_path`, one `PlatformCapabilities` per module (extra `/api/settings` fetches), covers 2 of ~20 modules |
| Edit all ~200 `_api` call sites to pass `tenant_id` | Rejected: churn, and the next module forgets (FR-004) |
| Default inside `_api` via a resolver on headers | **Chosen**: one place; mirrors how `refresh_fn` already rides on headers |
| Put the resolver on `RefreshableHeaders` itself | Rejected: shared with OpenCTI |

## R4 — Lookup failure

`GET /api/settings` needs `READ` on `PLATFORM_SETTING`, so a restricted token is denied even on a single-tenant instance. Failing every tool would break those deployments (US3), and the server enforces tenancy regardless.

- **Decision** (confirmed with requester): proceed unscoped; append the lookup cause to a tenant-scope rejection only (a 404 is not a scoping symptom). The probe retries transient 5xx, since a cached failure unscopes the whole tool set.
- **Alternatives named**: fail closed on any failure; fail closed only on transport/5xx.

## R5 — Direct `safe_request` callers

`core.py` (26, via `egress_client`) and `content.py` (1, multipart with stripped headers) bypass `_api`. Converting them to `_api` would change their egress client, so they keep their transport and gain a scoped wrapper with an identical signature.

## R6 — Reads narrow to the default tenant

Attack patterns, kill chain phases, injector contracts, domains and tags are tenant-owned in OpenAEV (`TenantBase`, v2 tenant isolation), so the twin returns the default tenant's rows, not a larger set. A multi-tenant token that spanned several tenants on the base path now sees one (FR-002, accepted). Pinned by `test_catalog_reads_use_the_tenant_twin`.
