# Contract: `sandbox_call` structured log event

This is the interface this feature exposes to the outside world: not an
HTTP endpoint, but a stable structured-log event shape that a log pipeline
(e.g. stdout → Grafana/Loki) can parse, filter, and aggregate on. This
contract is what stays stable across releases per spec FR-009/User Story 3.

## Emission

- Emitted by `app.services.sandbox_call_logging.log_sandbox_call()`.
- Called exactly once per invocation of `execute_python_opensandbox`,
  `execute_js_tool_opensandbox`, and `execute_python_tool_opensandbox`
  (spec FR-001), on every outcome (success or failure — spec FR-006).
- Rendered to stdout as one JSON object per call in production
  (`LOG_FORMAT=json`, the existing default — no new log-shipping
  infrastructure per FR-010).
- Log level: `info` for `outcome="success"`, `warning` for
  `outcome="failure"` (consistent with this codebase's existing
  convention of `logger.error`/`logger.warning` for failed sandbox
  operations at these same call sites).

## Shape

```json
{
  "event": "sandbox_call",
  "usage_type": "code_interpreter",
  "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "outcome": "success",
  "cpu_used_percentage": 12.5,
  "cpu_count": 1.0,
  "memory_used_in_mib": 64.2,
  "memory_total_in_mib": 128.0,
  "tte_ms": 842,
  "egress_default_action": "deny",
  "egress_allow_list": [],
  "level": "info",
  "timestamp": "2026-07-31T12:00:00.000000Z",
  "logger": "app.services.sandbox_call_logging"
}
```

Failure example (metrics/TTE unavailable, no user context):

```json
{
  "event": "sandbox_call",
  "usage_type": "custom_tool_js",
  "outcome": "failure",
  "failure_reason": "sandbox_create_failed",
  "egress_default_action": "deny",
  "egress_allow_list": ["api.example.com"],
  "level": "warning",
  "timestamp": "2026-07-31T12:00:05.000000Z",
  "logger": "app.services.sandbox_call_logging"
}
```

`level`, `timestamp`, and `logger` are added by the existing shared
structlog processor chain (`app/core/logging.py`), not by this feature's
code — they're included above only to show the full line a log pipeline
actually receives.

## Field reference

See `data-model.md` for the authoritative field table (names, types,
presence rules, source). This contract file describes the wire shape;
`data-model.md` describes the field semantics.

## Stability guarantee (FR-009)

- Field names and types listed in `data-model.md` are stable across
  releases. Adding a new field is backward-compatible; renaming or
  removing an existing field, or changing an existing field's type/unit,
  is a breaking change to this contract and must be called out in the
  changelog.
- `usage_type` is a closed enum today (`code_interpreter`, `custom_tool_js`,
  `custom_tool_python`); a future new call site adds a new enum value here,
  not a new field.
- Consumers should treat any field not in `data-model.md`'s table (e.g.
  `level`, `timestamp`, `logger`) as generic structlog metadata, not part
  of this feature's contract.

## Non-goals

- Not a database schema — no query interface, no persistence guarantee
  beyond whatever retention the log pipeline itself provides.
- Not a replacement for `sandbox_quota_usage`/`quota_usage` — quota
  enforcement and per-user billing-relevant counting are unaffected by this
  contract (spec FR-011).
