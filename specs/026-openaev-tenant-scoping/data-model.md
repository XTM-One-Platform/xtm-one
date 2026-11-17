# Data Model: OpenAEV Tenant Scoping

No persisted data. One in-memory value per tool set.

## Tenant scope (owned by `PlatformCapabilities`)

| State | Meaning | Request behaviour |
|---|---|---|
| `resolved(id)` | Settings read, `default_tenant_id` set | Scoped paths per [contract](contracts/path-scoping.md) |
| `none` | Settings read, no `default_tenant_id` | Unchanged (FR-007) |
| `failed(cause)` | Settings read failed or denied | Unscoped; `cause` appended to a tenant-scope rejection (FR-006) |

- Resolved at most once per tool set; `failed` is cached as `failed`, never as `none` (FR-003, FR-005).
- `tenant_id()` keeps returning `""` for both `none` and `failed` for its existing callers.
