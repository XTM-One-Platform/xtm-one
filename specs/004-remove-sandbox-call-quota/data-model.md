# Phase 1 Data Model: Remove Platform-Wide Sandbox Call Quota

**Feature**: [spec.md](./spec.md) | **Research**: [research.md](./research.md)

This feature removes one entity and its backing table; it introduces no new entity, column, or table. It does not modify the schema, model, or behavior of any other entity in the system.

## Entity removed

### SandboxQuotaUsage (table `sandbox_quota_usage` — DELETED)

`app/models/sandbox_quota.py` (entire file deleted).

| Column | Type | Notes |
|---|---|---|
| `id` | UUID, PK | — |
| `period_type` | `String(10)` | `"daily"` / `"weekly"` / `"monthly"` |
| `period_key` | `String(10)` | e.g. `"2026-08-03"`, a week identifier, or `"2026-08"` |
| `count` | `Integer` | all-users-combined call count for that period instance |
| `updated_at` | `DateTime(timezone=True)` | — |

**Uniqueness**: `uq_sandbox_quota_usage_period_type_key` on `(period_type, period_key)` — one row per period instance, shared across every user (no `user_id` column at all — this is the defining shape difference from spec 002's per-user `quota_usage` ledger, which this table is never confused with at the schema level even though the two names look similar).

**Lifecycle before removal**: Redis-fast counter (`sandbox_quota:usage:*` keys, incremented by `record_sandbox_call()`), periodically flushed to this table by `flush_sandbox_quota_usage()` (advisory-lock-guarded, called from `autonomous.py`'s scheduler tick). After removal, no code writes to or reads from this shape at all — the table itself is dropped (research.md §1).

**Removal mechanics**: the migration that created this table (`e3877a6a8159`) is deleted from `alembic/versions/` outright, not reversed by a new migration (research.md §1 — it was never merged to `main`, so it carries no released-deployment compatibility obligation). Fresh installs after this change never create the table. This branch's own already-migrated databases require a one-time manual reconciliation (`DROP TABLE sandbox_quota_usage;` + re-stamp `alembic_version` to the chain's new head, `a7c3sk1ll2697`) — not a mechanism that ships anywhere.

## Entity explicitly NOT removed (named here only to document the boundary)

### QuotaUsage (table `quota_usage` — UNCHANGED)

`app/models/quota.py` — spec 002's pre-existing per-user, per-usage-type, per-period ledger (`llm_proxy` / `agentic` / `sandbox` values in its `usage_type` column). This feature does not add, remove, or alter any column, row, or behavior here. It is named in this document only because its name is easily confused with the table being removed above — see research.md §2 for the full naming-collision analysis (`disable_sandbox_quota_usage`, which gates writes into *this* table, is a completely different, untouched setting from the removed `sandbox_quota_limits`/`sandbox_quota_fail_mode`, which controlled the *other*, now-deleted table).

## Configuration fields removed

| Field | Type | Source | Removed because |
|---|---|---|---|
| `sandbox_quota_limits` | `str` | `Settings`, env var `SANDBOX_QUOTA_LIMITS` | Backed the removed platform-wide daily/weekly/monthly limits (CSV `period:value` format). Its dedicated `field_validator` (`_validate_sandbox_quota_limits`) and its three derived properties (`_sandbox_quota_limits_map`, `sandbox_quota_daily_limit`, `sandbox_quota_weekly_limit`, `sandbox_quota_monthly_limit`) are removed alongside it. |
| `sandbox_quota_fail_mode` | `str` | `Settings`, env var `SANDBOX_QUOTA_FAIL_MODE` | Backed the removed platform-wide check's fail-open/fail-closed behavior. Removed from the shared `_validate_fail_mode` validator's field list (the validator itself, and its other two fields `rate_limit_fail_mode`/`quota_fail_mode`, are untouched — they belong to unrelated, still-existing mechanisms). |

## Configuration field explicitly NOT removed (named here only to document the boundary)

| Field | Type | Source | Why it stays |
|---|---|---|---|
| `disable_sandbox_quota_usage` | `bool` | `Settings`, env var `DISABLE_SANDBOX_QUOTA_USAGE` | Spec 002's per-user sandbox-usage-counting opt-out, gating `increment_sandbox_usage`'s write into `quota_usage` (a completely different table from the one being removed — see research.md §2). Its own deployment-docs row stays; only its trailing cross-reference to the now-removed `SANDBOX_QUOTA_LIMITS` needs rewording. |

## Interface removed

### `GET /api/v1/settings/sandbox-quotas` (admin-only) — DELETED

Returned `{"quotas": [...]}` from `get_sandbox_quota_status()` — current usage/limit/remaining/reset-time per configured period, all-users-combined. No request body, no path/query parameters. Confirmed zero frontend consumers (research.md §5) — removed with no replacement, no deprecation period, no versioning concern.
