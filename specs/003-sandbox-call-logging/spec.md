# Feature Specification: Structured Per-Call Sandbox Logging (CPU/RAM/Egress/TTE)

**Feature Branch**: `003-sandbox-call-logging`

**Created**: 2026-07-31

**Status**: Draft

**Input**: User description: "feat(sandbox): structured per-call logging (CPU/RAM/egress/TTE) for OpenSandbox calls". Emit one structured log event per OpenSandbox call, capturing usage type, CPU usage, RAM usage, the egress/network policy applied, and time-to-execute (TTE), with consistent, well-defined field names so a log pipeline shipping stdout (e.g. into Grafana/Loki) can filter/aggregate on them to build charts and stats.

## Clarifications

### Session 2026-07-31

- Q: Should the structured log record include a user/actor identifier for per-user drill-down, or stay anonymous like the platform-wide quota counter? → A: Include the caller's `user_id` (when available) in the log record for per-user drill-down.
- Q: Should fetching CPU/RAM metrics happen synchronously before returning the result, or asynchronously so it never delays the caller? → A: Synchronous, with a short bounded timeout so a slow/unreachable metrics call degrades to null fields (FR-007) rather than hanging the caller.
- Q: Should TTE represent only the SDK-reported execution time, or also a separate total wall-clock call duration? → A: TTE = SDK-reported execution time only; no separate wall-clock field.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Diagnose resource usage per sandbox call (Priority: P1)

An operator responsible for platform reliability wants to see, for every sandbox execution, how much CPU and memory it consumed and how long it took, broken down by which feature triggered it (code interpreter, custom JS tool, custom Python tool), so they can spot resource-hungry or slow executions and decide whether limits or images need adjusting.

**Why this priority**: This is the core value of the feature — without per-call resource and timing visibility, there is no way to build the charts/alerts the issue asks for. Everything else in the feature exists to support this.

**Independent Test**: Trigger one execution of each usage type (code interpreter, custom JS tool, custom Python tool) against the sandbox backend, then inspect the emitted log output and confirm each execution produced exactly one structured record containing CPU usage, memory usage, execution duration, and a label identifying which usage type it was.

**Acceptance Scenarios**:

1. **Given** a user runs a code interpreter execution that completes successfully, **When** the sandbox execution finishes, **Then** a structured log record is emitted containing CPU usage, memory usage, execution duration, egress policy, and a `usage_type` of `code_interpreter`.
2. **Given** a user invokes a custom tool implemented in JavaScript, **When** the sandbox execution finishes, **Then** a structured log record is emitted with `usage_type` of `custom_tool_js` and the same set of resource/timing/policy fields.
3. **Given** a user invokes a custom tool implemented in Python, **When** the sandbox execution finishes, **Then** a structured log record is emitted with `usage_type` of `custom_tool_python` and the same set of resource/timing/policy fields.

---

### User Story 2 - Understand outcomes for failed or errored sandbox calls (Priority: P2)

An operator wants failed or errored sandbox calls (creation failures, timeouts, non-zero exit codes) to also produce a log record with whatever timing/outcome information is available, so that failure-rate dashboards aren't blind to the calls that didn't succeed.

**Why this priority**: Resource/performance dashboards are incomplete — and can be misleading — if only successful calls are visible. This is secondary to P1 because the happy-path record is the foundation; this story extends the same mechanism to cover non-happy paths.

**Independent Test**: Trigger a sandbox call that fails (e.g. sandbox creation failure, execution timeout, or non-zero exit code) and confirm a structured log record is still emitted for that call, with an outcome/status field distinguishing it from a successful call, even if resource metrics could not be captured.

**Acceptance Scenarios**:

1. **Given** an OpenSandbox sandbox fails to be created (e.g. the backend is unreachable), **When** the call site handles that failure, **Then** a structured log record is emitted identifying the usage type and that the call did not succeed, without requiring CPU/RAM metrics to be present.
2. **Given** a sandbox execution runs but the executed code/tool exits with a non-zero status, **When** the call site returns its error result, **Then** a structured log record is emitted with an outcome indicating failure, alongside whatever timing/resource data was captured before the failure.

---

### User Story 3 - Rely on a stable, documented log schema for dashboards (Priority: P3)

A person building or maintaining a Grafana/Loki dashboard on top of these logs wants the field names and their meanings to be documented and stable across releases, so dashboards and alert rules built today keep working as the code evolves.

**Why this priority**: Without documentation, field names could drift silently between call sites or over time, silently breaking downstream dashboards. This is lower priority than emitting the data in the first place, but still required by the issue's acceptance criteria.

**Independent Test**: Compare the field names emitted by all three call sites against a written schema document; confirm they match exactly, and that the document explains what each field represents and its unit (e.g. milliseconds, percentage, MiB).

**Acceptance Scenarios**:

1. **Given** the documented schema lists a fixed set of field names and units, **When** log records are emitted from any of the three call sites, **Then** every record uses exactly those field names with values in the documented units.
2. **Given** a developer adds a new sandbox call site in the future, **When** they consult the schema document, **Then** they have enough information to emit a conforming log record without inventing new field names for the same concepts.

---

### Edge Cases

- What happens when the OpenSandbox metrics endpoint is unreachable or returns an error after an otherwise-successful execution? The log record MUST still be emitted for the call, with CPU/RAM fields omitted or null rather than blocking or dropping the whole record.
- What happens when a sandbox never becomes ready in time (a readiness timeout) before any execution result is produced? A log record MUST still be emitted with a distinguishable timeout outcome — not lumped in with other sandbox-creation failures — even though no execution-level TTE was ever produced.
- What happens when the quota check rejects a call before a sandbox is ever created (pre-existing quota-exceeded path)? This feature does not need to emit a resource-usage record for calls rejected before sandbox creation, since no sandbox ran and no resources were consumed. (See Assumptions.)
- What happens when local (non-OpenSandbox) Docker sandbox execution is used instead of OpenSandbox? Out of scope for this feature — see Assumptions.
- How does the system handle concurrent sandbox calls from different users at the same time? Each call MUST produce its own independent log record; records are not aggregated or batched across calls.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST emit exactly one structured log record per OpenSandbox call at each of the three existing call sites (code interpreter execution, custom JS tool execution, custom Python tool execution).
- **FR-002**: Each log record MUST identify which call site produced it via a `usage_type` field with one of the values `code_interpreter`, `custom_tool_js`, or `custom_tool_python`.
- **FR-002a**: Each log record MUST include the caller's user identifier when the call site was invoked with a known user context, and MUST omit that field (rather than emit a placeholder) when no user context is available, mirroring the existing `user_id | None` handling at these call sites.
- **FR-003**: Each log record for a call that reached a completed sandbox execution MUST include CPU usage (used percentage and CPU count) and memory usage (used and total, in MiB) as reported by the sandbox backend at the time the call completed.
- **FR-004**: Each log record MUST include the time-to-execute (TTE) for the call, i.e. the sandbox backend's own reported execution duration for the code/tool run (not overall call wall-clock time including sandbox creation/setup), expressed in milliseconds.
- **FR-005**: Each log record MUST include the egress/network policy that was applied when the sandbox was created for that call.
- **FR-006**: Each log record MUST include an outcome/status indicator distinguishing successful calls from failed calls (e.g. sandbox creation failure, timeout, non-zero exit code), so failure-rate dashboards can be built without a separate signal.
- **FR-007**: When resource metrics (CPU/RAM) cannot be retrieved for a given call (e.g. the metrics call fails or the call failed before execution), the system MUST still emit the log record for that call, with the resource fields omitted or null, rather than skipping the record entirely.
- **FR-007a**: Metrics retrieval MUST be performed synchronously (before the log record is emitted) and bounded by a short timeout, so that a slow or unreachable metrics endpoint degrades to a record with null resource fields (per FR-007) instead of materially delaying the response returned to the original caller.
- **FR-008**: Field names used across all three call sites MUST be identical for the same concept (e.g. the same field name for CPU used percentage everywhere it appears), so records from different usage types can be aggregated together.
- **FR-009**: The log record schema (field names, meaning, and units) MUST be documented in a discoverable location for people building dashboards or alerts on top of these logs.
- **FR-010**: Log records MUST be emitted using the application's existing structured logging mechanism (already shipped to stdout as JSON in production), requiring no new log-shipping infrastructure.
- **FR-011**: This feature MUST NOT change the existing quota-check or quota-recording behavior — it adds an observability record alongside, not a replacement for, quota tracking. *(Note, spec `004-remove-sandbox-call-quota`: the specific `sandbox_quota_usage` ledger this requirement originally named has since been removed entirely, as a separate platform-wide mechanism redundant with spec `002-sandbox-quota-usage-tracking`'s per-user combined enforcement. This requirement's substance is unchanged — this feature still MUST NOT change whatever quota mechanism is currently in place, it simply no longer names one that no longer exists.)*

### Key Entities

- **Sandbox call log record**: One structured log event per sandbox invocation. Key attributes: usage type (which call site), user identifier (when available), outcome/status (success or failure and failure kind), CPU used percentage, CPU count, memory used (MiB), memory total (MiB), time-to-execute (ms), egress/network policy applied. Not persisted to a database — this is a log-stream event, not a stored entity.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of sandbox calls across all three call sites (code interpreter, custom JS tool, custom Python tool) produce exactly one structured log record, whether the call succeeds or fails.
- **SC-002**: An operator can filter or group the emitted logs by usage type and by success/failure outcome using only documented field names, with no ambiguity about what a field represents.
- **SC-003**: A dashboard builder can identify, from documentation alone (without reading source code), every field name, its meaning, and its unit for the sandbox call log record.
- **SC-004**: Adding CPU/RAM/TTE/egress logging introduces no user-visible change in sandbox call behavior or response content — existing callers of the three functions receive identical return values and error behavior as before this feature.
- **SC-005**: A slow or unreachable metrics lookup adds no more than a few seconds of bounded, capped delay to any single sandbox call, and never hangs a call indefinitely.

## Assumptions

- Local (non-OpenSandbox) Docker-based sandbox execution is out of scope for this feature; only the three OpenSandbox call sites named in the issue are covered. The issue explicitly leaves this as an open decision ("Decide whether local (non-OpenSandbox) Docker sandbox calls should be included") and defers it rather than requiring it now.
- Sandbox calls rejected by the pre-existing quota check (before a sandbox is created) do not require a resource-usage log record, since no sandbox execution occurred; quota-rejection visibility is already covered by the existing quota-usage ledger (spec 002-sandbox-quota-usage-tracking).
- "Structured log record" means a single structured logging call (already available via the project's existing logging mechanism) — this feature does not introduce a new log transport, log aggregator, or dashboard; it only ensures the right fields are captured and emitted consistently.
- The egress/network policy is currently identical across all calls (a single platform-wide setting), so per-call logging of it will show the same value on every record today; this is still valuable as an audit trail and to detect any future per-tool policy differences.
- CPU/RAM metrics and TTE are sourced from data already exposed by the OpenSandbox SDK (`sandbox.metrics.get_metrics`, execution completion timing) — no new metrics collection mechanism needs to be built, only wiring existing SDK data into the log record.
