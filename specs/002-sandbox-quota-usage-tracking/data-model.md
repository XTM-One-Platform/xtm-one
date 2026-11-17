# Phase 1 Data Model: Sandbox Usage Quota Tracking

**Feature**: [spec.md](./spec.md) | **Research**: [research.md](./research.md)

## Entities

### QuotaUsage (existing table `quota_usage` — no schema change)

`app/models/quota.py:19`. This feature adds a new logical row shape by using
a new `usage_type` value; no column, index, or constraint changes.

| Column | Type | Notes for this feature |
|---|---|---|
| `id` | UUID, PK | unchanged |
| `user_id` | UUID, FK `users.id` (CASCADE) | the sandbox execution's initiating user (spec Key Entity "Sandbox Execution") |
| `usage_type` | `String(30)` | **new value `"sandbox"`** alongside existing `"llm_proxy"` / `"agentic"` — free-text column, no enum/CHECK constraint, so no migration needed |
| `model` | `String(200) \| None` | always `NULL` for `usage_type="sandbox"` (mirrors `"agentic"`'s convention — `model` is an `llm_proxy`-only dimension) |
| `period` | `String(10)` | period key resolved per research.md §2 (e.g. `"2026-07"` monthly, `"2026-07-15"` daily) — user > group > platform > `"monthly"` default, same precedence as `llm_proxy`/`agentic` |
| `agent_id` | UUID \| None, FK `agents.id` (CASCADE) | always `NULL` for this feature — no per-agent attribution requested by the spec (unlike `"agentic"`, which optionally sets this); revisit only if a future feature asks for per-agent sandbox usage breakdown |
| `count` | `Integer` | incremented by 1 per successful sandbox execution |
| `updated_at` | `DateTime(timezone=True)` | unchanged |

**Uniqueness**: the existing `uq_quota_usage_user_type_model_period`
constraint (`user_id, usage_type, model, period, agent_id`, with
`postgresql_nulls_not_distinct=True`) already produces exactly the row shape
this feature needs: one row per `(user, "sandbox", NULL, period, NULL)` —
i.e., one counter per user per month. No constraint change required.

**Lifecycle**: Redis-fast counter, incremented immediately per successful
sandbox call; periodically flushed to this durable table by the existing
`flush_quota_usage()` job (`quota.py:960`), which already iterates
Redis-side counters generically and does not hardcode the set of
`usage_type` values it flushes — confirmed no change needed there.

**Read exposure (spec FR-012, added 2026-07-31)**: also read by
`_read_quota_total` (`app/api/v1/metrics.py:65`) to populate the existing
`QUOTA_GAUGE` Prometheus gauge — also fully generic per `usage_type`,
confirmed no change needed there either. Only the `usage_types` list built
in the `/metrics` endpoint handler needs `"sandbox"` added (research.md §9).

**Read exposure (spec FR-013/FR-014, added 2026-07-31)**: also read by
`get_user_quota_usage` (`quota.py:846`, powers Profile/UserDetail usage
counts) and `get_platform_quota_monitoring` (`quota.py:1475`, via a direct
`_scan_period("sandbox", "monthly", None, is_global=True)` call, powers the
platform settings monitoring dashboard) — see research.md §10.1 for why
three separate functions needed extending, not one.

**Read exposure (spec FR-015–FR-019, added 2026-08-03)**: also read by
`check_agentic_quota` (`quota.py:611`) itself — the per-user `"sandbox"`
counter is now summed with the `"agentic"` counter, under the `"agentic"`
entry's resolved period window, before comparing against the agentic
per-user limit (research.md §11.2). This is enforcement, not just
reporting: a user's counter under `usage_type="sandbox"` can now
contribute to blocking their next agentic action, and vice versa via the
new pre-dispatch check (research.md §11.3) that reads the `"agentic"`
counter before permitting a sandbox execution to proceed.

### Sandbox Execution (conceptual — not a new persisted entity)

A single call to one of the three OpenSandbox-routed execution paths (code
interpreter, custom-tool JS, custom-tool Python), attributed to the
initiating user. Not modeled as its own row; represented only as one
increment against the `QuotaUsage` counter above. See research.md §3–4 for
exactly which of the three underlying functions this corresponds to and how
"successful" is determined per path.

### Sandbox Usage Counting Setting (config, not persisted)

| Field | Type | Source | Notes |
|---|---|---|---|
| `disable_sandbox_quota_usage` | `bool` | `Settings` (`app/core/config.py`), env var | Default `False` (counting enabled by default, per spec FR-005). Read once at process startup — no runtime hot-reload (spec FR-008, `/speckit-clarify` Q2). Checked once inside `increment_sandbox_usage`, not at each of the three call sites. |

## Function contracts (see also `contracts/`)

### `increment_sandbox_usage(user_id: uuid.UUID, period: str = "monthly", db: AsyncSession | None = None) -> None`

New function in `app/services/quota.py`, alongside `increment_llm_proxy_usage`
and `increment_agentic_usage`.

**Revised 2026-08-03 (FR-011)**: originally modeled on
`increment_llm_proxy_usage`'s period-resolution shape — resolving its own,
independently-configured `"sandbox"` period via `resolve_quota_config(db,
redis, user_id, "sandbox")` (research.md §2, 2026-07-31). That
independently-resolved period turned out to have no remaining purpose once
the combined check always reads sandbox usage under *agentic's* period
regardless (FR-017) — it only made the check's read key and this function's
write key drift apart whenever an admin configured the two differently (a
real bug, since fixed). The function now instead:

- When `db` is provided, resolves *agentic's* effective period via
  `resolve_quota_config(db, redis, user_id, "agentic")` and uses that,
  falling back to the `"monthly"` default when nothing resolves.
  `resolve_quota_config` no longer recognizes `"sandbox"` as a quota type at
  all — there is nothing left for it to resolve.
- Still never raises (research.md §5 — the fail-open behavior is built into
  the function itself, not left to callers to wrap) — unaffected by this
  change.
- Still increments no platform-wide/global companion counter — sandbox
  usage has no enforceable cap, so unlike `increment_agentic_usage` there is
  no global/license-ceiling counter to maintain here.

### `QuotaConfig` schema (`app/api/v1/schemas/settings.py`) — no `sandbox` field

```python
class QuotaConfig(BaseModel):
    llm_proxy: QuotaTypeConfig | None = None
    agentic: QuotaTypeConfig | None = None
    # No sandbox field — see FR-011 revision below.
```

**Revised 2026-08-03 (FR-011)**: a `sandbox: QuotaTypeConfig | None = None`
field was originally added here (2026-07-31) so an admin-configured
`"sandbox"` key would round-trip through `PUT /quotas` and the two
response-construction sites that reconstruct a `QuotaConfig` from a raw dict
(`app/api/v1/users.py`, `app/api/v1/settings.py`) instead of being silently
dropped. Since `period` was the field's only effect — `platform_limit`,
`default`, and `per_model` were always inert, as no `check_sandbox_*`
enforcement function ever read them — and that period is now gone too
(sandbox always uses agentic's), the field itself was removed. A stray
`"sandbox"` key in a raw dict (e.g. from before this change) is now silently
ignored by `QuotaConfig(**dict)`'s default extra-field handling, same as any
other unrecognized key — no special handling needed.

`User.quota_config` / `Group.quota_config` needed no schema change either
way — both are untyped `dict | None` JSON columns.

### New optional parameters on existing execution functions

To carry `user_id` (already shipped) and now also `db` (new, for FR-011)
down to the three `execute_*_opensandbox` functions (research.md §3):

- `execute_python(code: str, *, timeout: int = DEFAULT_TIMEOUT, user_id: uuid.UUID | None = None, db: AsyncSession | None = None) -> dict`
- `execute_python_opensandbox(code: str, *, timeout: int = DEFAULT_TIMEOUT, user_id: uuid.UUID | None = None, db: AsyncSession | None = None) -> dict`
- `execute_tool_auto(function_code: str, inputs: dict, language: str = "javascript", timeout: int = 300, *, use_docker: bool | None = None, user_id: uuid.UUID | None = None, db: AsyncSession | None = None) -> str`
- `execute_js_tool_auto(js_function: str, inputs: dict, timeout: int = 300, *, use_docker: bool | None = None, user_id: uuid.UUID | None = None, db: AsyncSession | None = None) -> str`
- `execute_js_tool_opensandbox(js_function: str, inputs: dict, timeout: int = 300, *, user_id: uuid.UUID | None = None, db: AsyncSession | None = None) -> str`
- `execute_python_tool_opensandbox(py_function: str, inputs: dict, timeout: int = 300, *, user_id: uuid.UUID | None = None, db: AsyncSession | None = None) -> str`

`db` is already available at every point `user_id` is available (the three
outer callers — `media.py`'s `_execute_code` closure, `loaders.py`'s
`_run_custom` closure, `custom_tools.py`'s test-tool handler — all already
receive `db` alongside `user_id`), so no new plumbing problem is introduced;
`db` simply rides along the same path already built for `user_id`.

All new parameters are optional and default to `None` (existing callers that
don't pass one are unaffected; `increment_sandbox_usage` is simply skipped
when `user_id` is `None`, matching spec's edge case "a sandbox call cannot be
attributed to a specific user"; period resolution falls back to `"monthly"`
when `db` is `None`).

### `check_agentic_quota(db: AsyncSession, user_id: uuid.UUID) -> None` — combined-usage revision (spec FR-015–FR-019, added 2026-08-03)

Existing function in `app/services/quota.py:611`, called today by four
sites (research.md §11.1): `agent_chat.py`'s `enforce_agentic_quota_ex`,
`assignment_engine.py`, `extension.py`, and `platform.py`. Its per-user
branch (both the XTM One and Copilot code paths) changes from comparing
the user's `"agentic"` count alone against `default_limit` to comparing
`agentic_count + sandbox_count` (both read under the `"agentic"` entry's
resolved period key, per FR-017) against the same `default_limit` (FR-016
— no new limit field). All four existing callers need **no changes** —
their refusal handling (soft in-conversation message, run-failure,
`HTTPException(429)`) is unaffected; only the quantity being compared
changes. The platform-wide/XTM-ceiling blocks earlier in the function are
unaffected — FR-015 only extends the per-user branch.

### New pre-dispatch check inside the three `execute_*_opensandbox` functions (spec FR-015/FR-018/FR-019, added 2026-08-03; the platform-wide `check_sandbox_quota()` call this originally sat next to was itself removed by spec `004-remove-sandbox-call-quota`)

At the top of each function, only when a user to attribute quota to is
resolvable: call `check_agentic_quota` (now combined-usage-aware, see
above) on a **dedicated session**, not the caller's shared `db` — reusing
`increment_sandbox_usage`'s existing `async with async_session() as
own_db:` pattern (research.md §11.4) to avoid the same concurrent-session
hazard that fix already exists to prevent. On `QuotaExceededError`, mirror
whichever shape that function already uses (FR-018) — **not** one uniform
shape: `execute_python_opensandbox` returns its existing error dict;
`execute_js_tool_opensandbox`/`execute_python_tool_opensandbox` raise
`RuntimeError` (caught generically by `CallableToolExecutor.execute_tool`,
`app/tools/base.py:880-882`, into a graceful `ToolResult(is_error=True)` —
never an unhandled crash). On any other exception (backend unreachable),
branch on `quota_fail_closed()` per FR-019 (research.md §11.5).

## Validation rules

- `usage_type="sandbox"` rows are only ever written by `increment_sandbox_usage`
  — no user input flows into this column.
- `count` only increments (this feature adds no decrement/refund path,
  unlike `agentic` usage's `refund_agentic_usage` — not requested by the spec).
- A missing/`None` `user_id` at any of the three call sites means the
  increment is skipped entirely for that call (no row written with a null
  or placeholder user).
- *(Revised 2026-08-03, FR-011)* `QuotaConfig` no longer has a `sandbox`
  field at all — the prior rules about its `.platform_limit`/`.default`/
  `.per_model`/`.period` sub-fields being accepted-but-inert no longer
  apply, since there is nothing left for an admin to set for sandbox in the
  first place. Every display surface still treats sandbox as limit-less
  (FR-014) — there is simply no config path left that could suggest
  otherwise.
- The combined check reuses the **agentic** entry's limit (FR-016) — this
  is unaffected by the `sandbox` field's removal, since the check never
  read a sandbox-specific limit field to begin with.
- `refund_agentic_usage` is unaffected by FR-015–FR-019: it only
  decrements the `"agentic"` counter (never `"sandbox"`), since a refund
  reverses a specific prior `increment_agentic_usage` call, not the
  separate `increment_sandbox_usage` counter that the combined *check*
  (read-only) now also considers.

## Display Entities (spec FR-013/FR-014, added 2026-07-31, revised 2026-07-31 — see research.md §10.4)

**Superseded**: sandbox no longer gets its own display entity anywhere.
`get_user_quota_usage`, `resolve_full_quota_info`, and
`get_platform_quota_monitoring` are unchanged and still return `"sandbox"`
as its own distinct backend entry (research.md §10.1); only what the
frontend does with that entry changed.

### Per-user usage entry (Profile.tsx / UserDetail.tsx card)

No separate sandbox card. The existing `"agentic"` card's displayed count
is `get_user_quota_usage`'s agentic total-entry count plus its sandbox
total-entry count, added together — the agentic card's `source`/`hasLimit`
logic is otherwise completely unmodified (unlike the superseded design,
which forced two overrides onto a dedicated sandbox card).

### Group quota entry (GroupDetail.tsx card)

No sandbox row at all (reverted to its pre-FR-013 shape). There was no
usage count on this row in the first place — it showed group-level sandbox
*period configuration*, not a count — so there is nothing to fold into
agentic's group-level card.

### Platform monitoring summary row (QuotasSection.tsx)

No separate sandbox tile. The global-only summary row still appended by
`get_platform_quota_monitoring` via `_scan_period("sandbox", "monthly",
None, is_global=True)` (§10.1, unchanged) has its `current_usage` added
into the displayed `"agentic"` `QuotaProgressCard`'s `current_usage` (and,
in the `xtm_one` 3-gauge layout, into only the `"monthly"` `agentic`
gauge — sandbox has no daily/yearly buckets to add to the other two). No
`QUOTA_TYPE_META.sandbox` entry exists anymore.
