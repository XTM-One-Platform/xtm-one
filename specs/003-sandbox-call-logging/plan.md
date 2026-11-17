# Implementation Plan: Structured Per-Call Sandbox Logging (CPU/RAM/Egress/TTE)

**Branch**: `003-sandbox-call-logging` | **Date**: 2026-07-31 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/003-sandbox-call-logging/spec.md`

**Note**: This template is filled in by the `/speckit-plan` command; its definition describes the execution workflow.

## Summary

Emit one structured `sandbox_call` log event per OpenSandbox call at each of
the three existing call sites (`execute_python_opensandbox` in
`code_interpreter.py`, `execute_js_tool_opensandbox` and
`execute_python_tool_opensandbox` in `custom_tool_executor.py`), carrying
`usage_type`, `user_id` (when available), success/failure outcome, CPU/RAM
usage, TTE, and the egress policy applied — via a new shared helper,
`app/services/sandbox_call_logging.py::log_sandbox_call()`, using the same
stdlib logger these files already use, with a new `structlog.stdlib.ExtraAdder()`
processor added to `app/core/logging.py`'s foreign pre-chain so `extra=`
kwargs surface as top-level structured fields — a structlog-native logger
was tried first but found to crash in production once `setup_logging()`
runs, since its `PrintLoggerFactory` output has no `.name` attribute that
the shared `add_logger_name` processor requires (research.md §1, caught by
running the full test suite, not just this feature's own tests). CPU/RAM come from
`sandbox.get_metrics()` (an SDK instance method, not
`sandbox.metrics.get_metrics(sandbox_id)` as the source issue assumed —
research.md §3), fetched synchronously with a bounded 3s timeout that falls
back to null fields rather than blocking the caller
(`/speckit-clarify` Q2). TTE comes from
`Execution.complete.execution_time_in_millis`, execution-only
(`/speckit-clarify` Q3). No new database table, migration, dependency, or
API endpoint; no change to existing quota-check or quota-recording
behavior (FR-011) or to any function's return values/error behavior
(SC-004).

## Technical Context

**Language/Version**: Python 3.13 (`backend/pyproject.toml` targets `py313`)

**Primary Dependencies**: FastAPI, `structlog` (already a dependency,
configured in `app/core/logging.py`) — no new dependency; this feature
adds one new processor (`structlog.stdlib.ExtraAdder()`) to that existing
configuration so `logging.getLogger(...).info(msg, extra={...})` calls
surface `extra` as top-level structured fields, which no existing call
site needed before (research.md §1). `opensandbox` SDK already a
dependency; this feature is the first caller of `Sandbox.get_metrics()` in
this codebase (TTE already flows through the existing `Execution.complete`
return value, just not read today).

**Storage**: N/A — the log record is a stdout event, not a persisted row;
no schema change, no migration (spec Key Entities). Independent of the
`sandbox_quota_usage` table and `quota_usage` ledger (FR-011).

**Testing**: pytest (`asyncio_mode = "auto"`), extending
`tests/test_opensandbox_sandbox_mode.py` (already covers all three
`execute_*_opensandbox` functions — success, `SandboxException` on create,
non-zero exit code) with assertions that each path emits exactly one
`sandbox_call` record with the right `outcome`/`failure_reason`, using
`caplog` or a patched logger to capture structlog output; plus a new
`tests/test_sandbox_call_logging.py` unit-testing `log_sandbox_call()`
directly (field presence/omission rules, the bounded-timeout→null-metrics
fallback, `failure_reason` mapping) — no existing test file covers a
sandbox-logging helper (confirmed by repo search).

**Target Platform**: Linux server (existing FastAPI backend, Docker/Kubernetes replicas)

**Project Type**: Web application, backend-only for this feature (no
frontend change — this is a stdout log event, not a UI-visible feature).

**Performance Goals**: Metrics fetch adds one bounded (≤3s timeout) SDK
call on the success path of an already-multi-second sandbox-container call,
matching this codebase's existing pattern (per sibling feature
`002-sandbox-quota-usage-tracking`'s Performance Goals) of treating
per-call sandbox overhead as negligible against the sandbox round-trip
itself (spec SC-005).

**Constraints**: Metrics/TTE capture MUST be synchronous but bounded — a
slow/unreachable metrics call degrades to null resource fields rather than
hanging the caller (spec FR-007/FR-007a, `/speckit-clarify` Q2); exactly
one log record per call regardless of outcome (spec FR-001/FR-006); field
names identical across all three call sites (spec FR-008); MUST NOT alter
existing quota-check/quota-recording behavior (spec FR-011) or change any
existing function's return values/raised exceptions (spec SC-004).

**Scale/Scope**: One new module (`sandbox_call_logging.py`, ~1 public
function + fetch helper + failure-reason mapping); three existing
call sites modified to call it on every success/failure branch, including
wrapping the previously-unhandled exception path around
`sandbox.commands.run()` in the two custom-tool functions (research.md §7);
one new backend test file plus extensions to one existing test file; one
new documentation subsection. No new table, no new endpoint, no new
frontend component, no new dependency.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Evaluated against `.specify/memory/constitution.md` v1.0.0.

- [x] **Fail-Closed by Default** (Principle I): not applicable in its
      literal sense — this feature adds no security-, quota-, or
      integrity-relevant *check* that gates whether a call is allowed; it's
      a post-hoc observability record for a call that has already run to
      completion or already failed on its own terms. Per `/speckit-clarify`
      Q2, the metrics *lookup* itself fails closed in the narrow sense that
      matters here (bounded timeout, never hangs indefinitely) but fails
      open on data completeness (null fields rather than blocking/dropping
      the record) — matching the same "recording must never fail an
      already-completed call" precedent `002-sandbox-quota-usage-tracking`
      already established for `increment_sandbox_usage`. FR-011 keeps this
      feature from touching the actual fail-closed gate — at the time this
      was written, `check_sandbox_quota`/`SANDBOX_QUOTA_FAIL_MODE`; that
      mechanism has since been removed entirely by spec
      `004-remove-sandbox-call-quota`, leaving spec
      `002-sandbox-quota-usage-tracking`'s combined agentic+sandbox check
      (`check_agentic_quota`/`QUOTA_FAIL_MODE`) as the sole such gate — this
      feature still does not touch it.
- [x] **Concurrency-Safe, Multi-Replica by Design** (Principle II): not
      applicable — no scheduled job, no startup task, no shared counter.
      Each call emits its own independent log line (spec Edge Cases); no
      cross-call or cross-replica state is read or written.
- [x] **CI-Enforced Quality Gates** (Principle III): satisfied — plan
      includes new unit tests for `log_sandbox_call()` (field
      presence/omission, timeout fallback, failure_reason mapping) and
      extended coverage in `test_opensandbox_sandbox_mode.py` for all three
      call sites across success and every failure branch, per
      quickstart.md's Automated tests section. No migration is added, so no
      migration-CI-job interaction.
- [x] **Rationale-Documented Complexity** (Principle IV): satisfied in
      design intent — the synchronous-with-bounded-timeout choice for
      metrics fetching (research.md §5, driven by `/speckit-clarify` Q2)
      and the decision to wrap previously-unhandled `commands.run()`
      exceptions so every call still yields exactly one record
      (research.md §7) are both non-obvious "why"s that must carry inline
      comments in the implementation, not just live in this plan —
      tasks.md should carry this forward as an explicit reminder for the
      implementer.
- [x] **Backward-Compatible, Auditable Evolution** (Principle V): satisfied
      trivially — no schema or state change ships with this feature (no
      migration, no new table), so there is no unrecognized-prior-state
      concern to document.

No violations; no Complexity Tracking entries required.

## Project Structure

### Documentation (this feature)

```text
specs/003-sandbox-call-logging/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md         # Phase 1 output (/speckit-plan command)
├── quickstart.md         # Phase 1 output (/speckit-plan command)
├── contracts/
│   └── sandbox-call-log-event.md  # Phase 1 output (/speckit-plan command)
├── checklists/
│   └── requirements.md  # Spec quality checklist (/speckit-specify, updated by /speckit-clarify)
└── tasks.md              # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

### Source Code (repository root)

`backend/` is a Python FastAPI service. This feature is backend-only — no
`frontend/` changes.

```text
backend/
├── app/
│   └── services/
│       ├── sandbox_call_logging.py   # NEW: log_sandbox_call(), bounded metrics fetch,
│       │                             # failure_reason mapping, structlog logger instance
│       ├── code_interpreter.py       # execute_python_opensandbox: call log_sandbox_call()
│       │                             # on every success/failure branch; fetch metrics inside
│       │                             # the `async with sandbox:` block after commands.run()
│       └── custom_tool_executor.py   # execute_js_tool_opensandbox, execute_python_tool_opensandbox:
│                                     # same treatment; also wrap the previously-unhandled
│                                     # commands.run() exception path (research.md §7)
└── tests/
    ├── test_sandbox_call_logging.py      # NEW: log_sandbox_call() unit tests
    └── test_opensandbox_sandbox_mode.py  # extend: assert exactly one sandbox_call record
                                           # per call, correct outcome/failure_reason, across
                                           # all three execute_*_opensandbox functions

docs/
└── docs/deployment/configuration.md   # new "### Sandbox call log fields" subsection
                                        # under the existing "## OpenSandbox" section
```

**Structure Decision**: No new top-level directories, services, or database
objects. One new narrowly-scoped service module
(`sandbox_call_logging.py`) reused by the three existing
`execute_*_opensandbox` functions, mirroring this codebase's existing
pattern (e.g. `sandbox_quota.py`) of one small module per sandbox-adjacent
concern. Backend-only; no frontend, no new API endpoint, no new migration.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

No violations — table intentionally omitted.
