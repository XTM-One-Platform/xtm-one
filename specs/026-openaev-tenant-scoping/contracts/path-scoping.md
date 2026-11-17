# Contract: Path Scoping

`_scope_path(headers, path)` → path to request.

| Input path | State | Result |
|---|---|---|
| `/api/<family>/...`, tenant-owned family | `resolved(id)` | `/api/tenants/{id}/<family>/...` |
| already `/api/tenants/...` | any | unchanged (FR-008) |
| `/api/me`, `/api/me/*` (only `GET /me` has a twin; the rest are base-only), `/api/settings`, `/api/settings/*`, `/api/platform-users`, `/api/platform-roles`, `/api/platform-groups`, `/api/schemas` (no twin) | any | unchanged (FR-010) |
| `/api/autonomous-runs/*` (tenant derived from the run) | any | unchanged (FR-010) |
| any | `none` or `failed` | unchanged |
| not `/api/...` | any | unchanged |
| caller passed explicit `tenant_id` | any | caller's value wins (FR-009) |

The exemption list lives in one constant in `_common.py`; a test pins it.

**Error hint** (`failed` only, and only on a `TENANT_WRITE_SCOPE` / `TENANT_SELECTOR_REQUIRED` rejection): `" Note: the default tenant could not be read from GET /api/settings (<cause>); on a multi-tenant OpenAEV this request needs it."` appended to the request error.
