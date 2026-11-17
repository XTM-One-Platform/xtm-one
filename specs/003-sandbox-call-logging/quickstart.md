# Quickstart: Validating Sandbox Call Logging

Prerequisites: an OpenSandbox server reachable at `OPENSANDBOX_DOMAIN`
(see `docs/docs/deployment/configuration.md` § OpenSandbox), and
`CUSTOM_TOOL_SANDBOX=opensandbox` for the custom-tool scenarios. Backend
running locally with `LOG_FORMAT=json` (or unset in production mode) so log
lines are JSON.

## Scenario 1 — Successful code interpreter call (User Story 1)

1. Trigger a code interpreter execution that completes successfully (e.g.
   via the chat UI's code interpreter tool, or directly:
   `await execute_python_opensandbox("print(1+1)", user_id=some_user_id, db=session)`).
2. Inspect stdout for a JSON line with `"event": "sandbox_call"`,
   `"usage_type": "code_interpreter"`, `"outcome": "success"`.
3. Confirm `cpu_used_percentage`, `cpu_count`, `memory_used_in_mib`,
   `memory_total_in_mib`, `tte_ms`, `egress_default_action`,
   `egress_allow_list` are all present, and `user_id` matches the caller.

## Scenario 2 — Successful custom tool calls (JS and Python)

1. Invoke a custom tool implemented in JavaScript, then one implemented in
   Python, each routed through OpenSandbox.
2. Confirm one `sandbox_call` record per invocation, with `usage_type`
   `custom_tool_js` and `custom_tool_python` respectively, and the same
   field set as Scenario 1.
3. Confirm the field *names* are byte-for-byte identical across all three
   `usage_type` values (spec FR-008) — e.g. diff the JSON keys of one
   record from each usage type.

## Scenario 3 — Failure paths still produce a record (User Story 2)

1. Point `OPENSANDBOX_DOMAIN` at an unreachable host (or stop the
   OpenSandbox server) and trigger a code interpreter call. Confirm a
   `sandbox_call` record is emitted with `outcome="failure"` and
   `failure_reason` set, and that CPU/RAM/TTE fields are simply absent
   (not present as `null` placeholders that block the record).
2. Restore connectivity; run code that exits non-zero inside the sandbox
   (e.g. `import sys; sys.exit(1)`). Confirm a record with
   `outcome="failure"`, `failure_reason="nonzero_exit"`.

## Scenario 4 — Metrics fetch does not block the caller (User Story 1 / SC-005)

1. With a normal successful call, time the end-to-end call latency before
   and after this feature ships (or compare against the sibling
   `002-sandbox-quota-usage-tracking` baseline). Confirm no material
   latency regression under normal conditions.
2. (Optional, if reproducible in a test/staging environment) simulate a
   slow/unreachable metrics endpoint and confirm the call still returns
   within a few seconds — it does not hang — and the emitted record has
   null CPU/RAM fields.

## Scenario 5 — Schema documentation matches actual output (User Story 3)

1. Open `docs/docs/deployment/configuration.md` § OpenSandbox → "Sandbox
   call log fields".
2. Compare every field name/unit listed there against an actual captured
   `sandbox_call` JSON line from Scenario 1 or 2. They must match exactly —
   see `contracts/sandbox-call-log-event.md` for the authoritative shape.

## Automated tests

Run the backend test suite covering this feature:

```bash
cd backend
pytest tests/test_sandbox_call_logging.py tests/test_opensandbox_sandbox_mode.py -v
```

Expected: unit tests for `log_sandbox_call()` (field presence/omission
rules, failure_reason mapping, bounded-timeout fallback to null metrics)
plus extended coverage in `test_opensandbox_sandbox_mode.py` asserting each
of the three `execute_*_opensandbox` functions emits exactly one record per
call, on both success and each failure branch, without changing their
existing return values or raised exceptions (spec SC-004).
