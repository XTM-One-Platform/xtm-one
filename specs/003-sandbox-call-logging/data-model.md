# Data Model: Sandbox Call Log Record

This feature introduces no database schema, table, or migration — the
"entity" below is a log-stream event shape (a single structlog call), not a
persisted row. Field names and semantics here are the authoritative source
for `contracts/sandbox-call-log-event.md` and the documentation added to
`docs/docs/deployment/configuration.md`.

## Entity: Sandbox Call Log Record

One structlog event, emitted exactly once per call to
`execute_python_opensandbox`, `execute_js_tool_opensandbox`, or
`execute_python_tool_opensandbox` (spec FR-001). Event name (structlog's
first positional arg, rendered as the `event` key by `JSONRenderer`):
`"sandbox_call"`.

| Field | Type | Always present? | Description |
|---|---|---|---|
| `usage_type` | `str` enum | Yes | One of `code_interpreter`, `custom_tool_js`, `custom_tool_python` — which call site produced the record (FR-002). |
| `user_id` | `str` (UUID) | No — omitted when the call had no user context | The caller's user identifier, when available (FR-002a, `/speckit-clarify` Q1). |
| `outcome` | `str` enum | Yes | `success` or `failure` (FR-006). |
| `failure_reason` | `str` enum | Only when `outcome="failure"` | One of `sandbox_ready_timeout`, `sandbox_create_failed`, `sandbox_create_error`, `nonzero_exit`, `execution_error` (research.md §7). |
| `cpu_used_percentage` | `float` | No — omitted/null if no sandbox reached completion, or metrics could not be retrieved | From `SandboxMetrics.cpu_used_percentage` (FR-003). Present for `outcome="success"` and for the `nonzero_exit` failure case (the sandbox still ran to completion there — `/speckit-analyze` finding I1). Never fetched for `sandbox_ready_timeout`/`sandbox_create_failed`/`sandbox_create_error` (no sandbox exists yet) or `execution_error` (`commands.run()` itself raised, so the sandbox's connection is presumed unhealthy — attempting a metrics fetch over it would just add latency to an already-failing call for no benefit; `/code-review` finding, corrected during implementation). |
| `cpu_count` | `float` | No — same presence rule as `cpu_used_percentage` | From `SandboxMetrics.cpu_count` (FR-003). |
| `memory_used_in_mib` | `float` | No — same presence rule as `cpu_used_percentage` | From `SandboxMetrics.memory_used_in_mib` (FR-003). |
| `memory_total_in_mib` | `float` | No — same presence rule as `cpu_used_percentage` | From `SandboxMetrics.memory_total_in_mib` (FR-003). |
| `tte_ms` | `int` | No — omitted/null if the execution never reached a `complete` event | From `Execution.complete.execution_time_in_millis` (FR-004, `/speckit-clarify` Q3). Present for `outcome="success"` and for `nonzero_exit` (same rationale as above); always absent for `sandbox_ready_timeout`/`sandbox_create_failed`/`sandbox_create_error`/`execution_error` (no execution ever completed). |
| `egress_default_action` | `str` | Yes | Always `"deny"` today (FR-005; research.md §8). |
| `egress_allow_list` | `list[str]` | Yes (may be empty) | Sorted `settings.opensandbox_network_allow_list` at call time (FR-005). |

### Field name stability (FR-008/FR-009)

Every field above uses the exact same name and meaning across all three
`usage_type` values — no call site renames or reshapes any field. This
table is the single source of truth; the shared `log_sandbox_call()`
helper (research.md §2) is the only place these names are literally typed
in code, so a future call site or field addition should extend this table
first, then the helper, never invent a parallel name at a call site.

### Lifecycle

Not applicable — this is a fire-once, immutable log event with no state
transitions, no update path, and no persistence layer of its own (it lives
only in the log stream, per the spec's Key Entities section and FR-010).

### Relationship to existing entities

- Independent of `SandboxQuotaUsage` (the `001-sandbox-call-quotas` /
  `002-sandbox-quota-usage-tracking` durable ledger) — this feature adds no
  field to, and reads no field from, that table (FR-011). The two can be
  correlated externally via `usage_type`/`user_id`/timestamp if needed, but
  neither system references the other's storage.
- `user_id` here is the same identifier already threaded through
  `execute_*_opensandbox`'s existing `user_id: uuid.UUID | None` parameter
  (research.md §10) — same value, no new source of truth.
