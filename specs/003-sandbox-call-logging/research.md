# Research: Structured Per-Call Sandbox Logging

## 1. Logging mechanism: stdlib `logging.getLogger()` + `extra=`, plus a new `ExtraAdder()` processor

**Decision (revised during implementation — see below)**: The new log
event is emitted via the stdlib `logging.getLogger(__name__)` already used
in `code_interpreter.py` / `custom_tool_executor.py`, calling
`logger.info("sandbox_call", extra={...fields...})` /
`logger.warning(...)`. `app/core/logging.py`'s `foreign_pre_chain` gains
one new processor, `structlog.stdlib.ExtraAdder()`, so those `extra` keys
surface as top-level JSON fields (previously they were silently dropped —
no processor in the chain read `record.__dict__` for arbitrary keys).

**Original decision (superseded)**: This section originally chose a
structlog-native logger (`structlog.get_logger(__name__)`) instead,
reasoning that `extra=` was dropped and a native logger would pass
keyword arguments straight through to the renderer. That was correct
about `extra=` being dropped, but wrong about the native logger being a
safe alternative — caught only by running the *full* backend test suite
(not just this feature's own test files) during implementation (task
T024): once any test in the suite triggers `app.core.logging.setup_logging()`
(e.g. any test that boots the FastAPI app), structlog's global config uses
`logger_factory=structlog.PrintLoggerFactory()`, and the shared
`add_logger_name` processor unconditionally does `logger.name` on
whatever `logger_factory()` produced — but `structlog.PrintLogger`
objects have **no `.name` attribute**, so every `log_sandbox_call()` call
raised `AttributeError: 'PrintLogger' object has no attribute 'name'`
once real `setup_logging()` had run anywhere in the process (i.e. in
production itself, not just an isolated test quirk). No existing module
in this codebase had ever called `structlog.get_logger()` before this
feature, so this incompatibility had never been exercised. Confirmed via
`python3 -c "import structlog; structlog.PrintLogger().name"` → `AttributeError`.

**Alternatives considered (final)**:
- Keep `structlog.get_logger()` and instead fix `add_logger_name`/
  `PrintLoggerFactory` to tolerate loggers without `.name`, or switch the
  native `logger_factory` to `structlog.stdlib.LoggerFactory()` (real
  stdlib `Logger` objects, which have `.name`). Rejected: a materially
  larger, riskier change to shared logging infrastructure (changes
  behavior for any *future* structlog-native caller, and interacts with
  the stdlib bridge in ways not fully explored) versus the one-line,
  purely-additive `ExtraAdder()` fix, which only activates when a caller
  passes `extra=` (every existing stdlib `logger.info(...)` call without
  `extra=` is completely unaffected).
- Manually build the field dict and pass it through the existing stdlib
  logger's message string (e.g. JSON-encode it into `%s`). Rejected: fights
  the renderer instead of using it — would produce a JSON blob nested
  inside a `message` string field rather than top-level, queryable keys.

## 2. Shared helper module: new `app/services/sandbox_call_logging.py`

**Decision**: Add one new module with a single function,
`log_sandbox_call(...)`, called from all three existing call sites. It owns
the structlog logger instance, the fixed field names, and the bounded
metrics-fetch helper.

**Rationale**: FR-008 requires identical field names across all three call
sites; a shared function is the only way to guarantee that without
duplicating the field list three times (and risking drift, which is
exactly the failure mode FR-008/FR-009 exist to prevent). It's a distinct
concern from `app/services/sandbox_quota.py` (call-count enforcement) and
`app/services/quota.py` (per-user usage ledger) — neither is touched by
this feature (FR-011) — so it gets its own module rather than being folded
into either, mirroring this codebase's existing pattern of one narrowly-scoped
module per sandbox-adjacent concern.

**Alternatives considered**:
- Inline the logging calls separately in each of the three call sites.
  Rejected: directly risks the field-name drift FR-008 forbids.
- Add the function to `sandbox_quota.py`. Rejected: that module is
  explicitly scoped to quota *enforcement* (its own docstring says so); this
  is an unrelated observability concern with no shared state.

## 3. Metrics source: `sandbox.get_metrics()` (instance method), not `sandbox.metrics.get_metrics(sandbox_id)`

**Decision**: Call `await sandbox.get_metrics()` on the already-open
`Sandbox` instance (`opensandbox/sandbox.py:237`), which returns a
`SandboxMetrics` (`opensandbox/models/sandboxes.py:801`) with
`cpu_count: float`, `cpu_used_percentage: float`,
`memory_total_in_mib: float`, `memory_used_in_mib: float`.

**Rationale**: The source issue describes the call as
`sandbox.metrics.get_metrics(sandbox_id)`; the installed SDK
(`opensandbox==0.1.12`) exposes it as a no-argument instance method,
`Sandbox.get_metrics(self) -> SandboxMetrics`, with no `.metrics` sub-object
and no `sandbox_id` argument (the instance already knows its own ID). This
correction only affects the call shape, not the field names the issue asks
for — all four are present on `SandboxMetrics` exactly as named.

**Alternatives considered**: None — this is a factual correction from
reading the installed dependency, not a design choice.

## 4. TTE source: `Execution.complete.execution_time_in_millis`, execution-only (per `/speckit-clarify`)

**Decision**: `result = await sandbox.commands.run(...)` returns an
`Execution` (`opensandbox/models/execd.py:142`) whose `complete` field
(`ExecutionComplete | None`) carries `execution_time_in_millis: int`. TTE =
`result.complete.execution_time_in_millis` when `result.complete` is not
`None`; otherwise the log record's `tte_ms` field is omitted/null (matches
the `/speckit-clarify` decision that TTE is execution-only, and the spec's
edge case for executions that never reach a `complete` event).

**Alternatives considered**: Wall-clock timing the whole call
(`Sandbox.create` + upload + `commands.run`) with a local `time.monotonic()`
pair. Rejected by `/speckit-clarify` Q3 — the spec deliberately scopes TTE
to the SDK-reported execution time only, no second wall-clock field.

## 5. Bounded metrics-fetch timeout

**Decision**: Wrap the `sandbox.get_metrics()` call in
`asyncio.wait_for(..., timeout=_METRICS_FETCH_TIMEOUT_SECONDS)` with a
module-level constant `_METRICS_FETCH_TIMEOUT_SECONDS = 3.0`, catching
`TimeoutError` and any `SandboxException`/generic `Exception` from the
metrics call and falling back to `None` for all four resource fields (FR-007).
Not made configurable via a new `Settings` field / env var — the spec does
not ask for operator tuning of this value, and adding one would be
unrequested scope (SC-005 only requires the delay stay small and bounded,
which a fixed constant satisfies).

**Rationale**: `/speckit-clarify` Q2 resolved metrics fetching as
synchronous-with-bounded-timeout (FR-007a); a fixed, short timeout is the
simplest way to satisfy "bounded" without introducing new configuration
surface.

## 6. Where the metrics call must happen: inside `async with sandbox:`, right after `commands.run(...)`

**Decision**: Call `sandbox.get_metrics()` immediately after
`result = await sandbox.commands.run(...)`, still inside the `async with
sandbox:` block, before the block exits and tears the sandbox down.

**Rationale**: `get_metrics()` is an instance method that needs a live
sandbox connection; calling it after the `async with` block exits would
hit an already-torn-down sandbox. This ordering also lets the metrics
reflect resource usage from the run that just completed, which is the
point of FR-003.

## 7. Failure-path coverage: wrap post-creation logic so every existing return/raise still emits exactly one record

**Decision**: Each of the three functions wraps its `sandbox.commands.run()`
call (plus immediate stdout/stderr extraction) in a `try/except Exception`
that logs one failure-outcome record (`failure_reason="execution_error"`)
and re-raises unchanged. All three call sites have this exact gap today —
`commands.run()` is not currently wrapped in any of
`execute_python_opensandbox`, `execute_js_tool_opensandbox`, or
`execute_python_tool_opensandbox` — so the fix applies uniformly, not just
to the two custom-tool functions as this section originally scoped it
(confirmed while implementing: `code_interpreter.py`'s function has the
identical unwrapped `commands.run()` call, so leaving it out there would
have been an inconsistent, arbitrary gap). Today that exception propagates
uncaught with no log record; after this feature it still propagates
(unchanged behavior, per FR-011/SC-004) but is preceded by exactly one
failure log record (FR-001).

**Failure reason taxonomy** (`failure_reason` field, only present when
`outcome="failure"`):
- `"sandbox_ready_timeout"` — `Sandbox.create` raised
  `SandboxReadyTimeoutException` (a `SandboxException` subclass,
  `opensandbox/exceptions/sandbox.py:117`, `error.code ==
  SandboxError.READY_TIMEOUT`) — the sandbox never became ready within
  `ready_timeout` before any execution could start. Checked first, inside
  the existing `except SandboxException` block, via `isinstance(exc,
  SandboxReadyTimeoutException)` (or equivalently `exc.error.code ==
  SandboxError.READY_TIMEOUT`).
- `"sandbox_create_failed"` — any other `SandboxException` during
  `Sandbox.create` (checked after ruling out the ready-timeout case above)
- `"sandbox_create_error"` — any other exception during `Sandbox.create`
- `"nonzero_exit"` — `commands.run()` returned with a non-zero/failing exit
  code — the sandbox *did* reach completion here, so CPU/RAM (via
  `sandbox.get_metrics()`) and TTE (via `result.complete`) should still be
  captured when available, not omitted (spec `/speckit-analyze` finding
  I1 — the sandbox is still live and `result.complete` may be populated at
  this point)
- `"execution_error"` — any other exception raised after sandbox creation
  succeeded (covers unhandled `commands.run()` exceptions and any
  post-processing failure). No metrics fetch is attempted for this
  branch (`sandbox=` is deliberately not passed to `log_sandbox_call`
  here) — see §11, a correction made during a post-implementation code
  review: the original design did pass `sandbox=`, but an exception from
  `commands.run()` itself typically means the sandbox's connection is
  already unhealthy, so attempting a metrics fetch over that same channel
  just burns the full bounded timeout for no benefit, adding latency to
  an already-failing call.

**Why not a generic mid-execution "timeout"**: none of the three call
sites pass `opts=RunCommandOpts(timeout=...)` to `commands.run()`
(`opensandbox/models/execd.py:273-289` — a per-command timeout the server
would enforce), so there is no SDK-visible signal for "the executed code
ran too long" today; only `Sandbox.create`'s separate `timeout` (sandbox
lifetime) and `ready_timeout` (startup) parameters exist
(`opensandbox/sandbox.py:479-508`). `SandboxReadyTimeoutException` is the
only genuinely distinguishable, currently-detectable timeout signal at
these three call sites, and it matches the spec's Edge Case wording
("times out before producing any execution result") exactly — a
readiness timeout means no execution ever started. Inventing a
generic `"timeout"` value with no way to actually detect it would be a
label nothing could ever populate.

**Alternatives considered**:
- Only log the happy path and the three previously-named failure
  branches, leaving any exception from `commands.run()` itself unlogged.
  Rejected: violates FR-001/SC-001 ("every call", "whether it succeeds or
  fails") — that gap is exactly the kind of silent blind spot User Story 2
  exists to close.
- A single generic `"timeout"` bucket covering both readiness timeouts and
  a hypothetical mid-execution timeout. Rejected: no mid-execution timeout
  is currently wired up at these call sites (see above), so the only real
  timeout is the readiness one — naming it precisely
  (`sandbox_ready_timeout`) is both accurate and forward-compatible if a
  per-command timeout is added later (it would get its own value, not
  overload this one).

## 8. Egress policy field shape: two flat fields, not one nested object

**Decision**: Log `egress_default_action: "deny"` and
`egress_allow_list: list[str]` (sorted, from
`settings.opensandbox_network_allow_list`) as two separate flat fields,
rather than one nested `egress_policy` object.

**Rationale**: Flat fields are directly filterable/aggregable in Loki/Grafana
(FR-008/SC-002/SC-003 — "using only documented field names") without a
JSON-path query; a nested object would work but adds friction for the
exact dashboard use case the issue names. `default_action` is currently
always `"deny"` (the constitution's Security Requirements mandate
fail-closed egress; both call-site modules hardcode
`NetworkPolicy(default_action="deny", ...)`), so logging it is mostly
forward-looking audit trail per the spec's Assumptions — still worth a
real field rather than a comment, in case a future per-tool override
changes it.

## 9. Documentation location: `docs/docs/deployment/configuration.md`, new subsection under `## OpenSandbox`

**Decision**: Add a new `### Sandbox call log fields` subsection directly
under the existing `## OpenSandbox` section (which already documents
`OPENSANDBOX_NETWORK_ALLOW`, `SANDBOX_QUOTA_LIMITS`, etc.), listing every
field name, type, unit, and when it's present/omitted.

**Rationale**: This is where every other OpenSandbox-adjacent
operational fact already lives in this repo (confirmed: no existing
`docs/docs/administration/*sandbox*` or `*observability*` file). Keeping
docs co-located with the feature's other operational docs (rather than
inventing a new page) matches this codebase's existing organization and is
"discoverable" per FR-009 by anyone already reading the OpenSandbox
section for env vars.

**Alternatives considered**: A brand-new top-level doc page. Rejected:
unwarranted new surface for a single field-schema table; the existing
section is a better-discovered home.

## 10. `user_id` plumbing: already available, no new threading needed

**Decision**: Reuse the `user_id: uuid.UUID | None` parameter already
present on all three `execute_*_opensandbox` functions (added by spec
`002-sandbox-quota-usage-tracking`) — pass it straight to
`log_sandbox_call(..., user_id=user_id, ...)`. No new parameter, no new
call-chain threading.

**Rationale**: `/speckit-clarify` Q1 resolved that the log record should
include `user_id` when available; that parameter already exists end-to-end
at all three call sites for the sibling quota-tracking feature, so this is
a zero-plumbing reuse, not new wiring.

## 11. Post-implementation corrections from a `/code-review` pass

A high-effort code review after initial implementation surfaced three real
bugs and one duplication concern in the delivered code, all fixed:

- **`nonzero_exit` misclassified as `outcome="success"` when stdout was
  present** (`code_interpreter.py`'s `execute_python_opensandbox`). The
  pre-existing branch `if result.exit_code != 0 and not stdout_text:` only
  covers the no-stdout case; when the process exited non-zero *and* still
  produced stdout, execution fell through to the success-logging path.
  That branching is correct and unchanged for its original purpose
  (spec `002-sandbox-quota-usage-tracking` FR-002 — the sandbox still ran
  to completion, so it's still billable usage), but this feature's
  `outcome` field must independently reflect the actual exit code per
  FR-006, regardless of whether stdout happened to appear. Fixed by
  deciding `outcome`/`failure_reason` purely from `result.exit_code`,
  once, before either return path — usage-metering behavior (SC-004) is
  untouched. Confirmed to be code-interpreter-specific: the two
  custom-tool functions already checked `result.exit_code != 0`
  unconditionally with no such gap.
- **Metrics fetch attempted on a sandbox whose connection just failed**
  (all three functions' `execution_error` branch). Originally passed
  `sandbox=sandbox` to `log_sandbox_call` there, so a failing
  `commands.run()` call would still trigger a `sandbox.get_metrics()`
  attempt over the same (presumed-dead) connection, adding up to the full
  3s bound (§5) of latency to an already-failing request before the
  original exception could re-raise. Fixed by not passing `sandbox=` in
  this branch — `execution_error` records now never carry resource/TTE
  fields (data-model.md updated to match), consistent with the other
  never-completed-execution branches.
- **A cancellation during that (now-removed) metrics-fetch await could
  have replaced the original exception** in the `execution_error`
  except-block, contradicting the "still propagates unchanged" (SC-004)
  claim in both the code's own comment and its test. Resolved as a
  side-effect of the previous fix: with no `sandbox=` passed,
  `log_sandbox_call` has no `await` left to take in that branch, so the
  guarded block has no suspension point between logging and the bare
  `raise` — a cancellation cannot land there anymore.
- **The `SandboxReadyTimeoutException`/`SandboxException`/generic-`Exception`
  classification, and the try/except-log-reraise wrapper around
  `commands.run()`, were copy-pasted across all three functions.**
  Extracted into two shared helpers in `sandbox_call_logging.py`:
  `log_sandbox_create_failure(usage_type, user_id, exc, egress_allow_list)`
  (the classification + log call) and `log_execution_errors(usage_type,
  user_id, egress_allow_list)` (an `asynccontextmanager` wrapping the
  try/except-log-reraise pattern, deliberately not accepting a `sandbox`
  parameter at all — so the latency/cancellation fix above is structural,
  not just a call-site convention future call sites could drift from).
