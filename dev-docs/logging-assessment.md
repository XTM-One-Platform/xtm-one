# Backend Logging Assessment

Full audit of `backend/app` logging, motivated by production Kubernetes log
review: empty error reasons (`"Failed to refresh token for [Sam] Notion
(notion): "`), suspected warning/error miscalibration, and log lines missing
the context needed to act on them.

## Methodology

1. **Inventory** - enumerated every `logger.<level>()` call in `backend/app`
   (ripgrep + AST analysis): **2,190 calls across 207 files** at baseline.

   | Level | Calls |
   |---|---|
   | `warning` | 931 |
   | `info` | 651 |
   | `debug` | 286 |
   | `error` | 261 |
   | `exception` | 61 |
   | `critical` | 0 |

2. **Categorised problem classes** against the severity rubric below, with
   an AST pass to find (a) logging calls that interpolate the caught
   exception variable (`%s` of `e` / `str(e)` / f-string `{e}`), (b) broad
   `except Exception:` handlers whose body is only `pass`, (c) `error`-level
   logs in broad catches without `exc_info`, and (d) logging calls that
   could interpolate credentials or full token-endpoint payloads.

3. **Fixed** the findings mechanically where safe (AST-verified codemod for
   the empty-reason class) and manually where judgment was required
   (severity, tracebacks, context, secrets).

### Severity rubric applied

- **error** - actionable failure of a platform operation: data-loss risk,
  feature broken, config/credentials permanently broken, invariant violated.
- **warning** - degraded-but-recovering, or caused by user/external input.
- **info** - lifecycle events.
- **debug** - diagnostics.

### Out of scope (addressed separately)

Three parallel workstreams own these files; they are intentionally NOT
modified here to avoid PR conflicts:

- `app/services/token_refresh.py` (empty-message formatting + permanent-
  failure severity for token refresh - the exact production example)
- `app/services/kb_sync.py` (retry/backoff + timeout severity)
- `app/services/custom_tool_executor.py` (the per-execution `SECURITY:`
  error spam -> log-once)
- `app/services/persistent_memory.py`

The severity questions raised by the operator for those paths (permanent
token-refresh failures at warning, the 1800s KB-sync timeout at error while
transient 502s are warning) are therefore documented here but fixed in the
parallel PRs. `app/services/kb_sync_scheduler.py` already escalates
deterministically (quarantine after N consecutive failures),
which is the right shape: transient failure = warning, persistent/quarantine
= error.

## Findings and fixes

### 1. Empty exception reasons (the `": "` class) - FIXED, systemic

`str(e)` is empty for `asyncio.TimeoutError()`, `asyncio.CancelledError()`,
several httpx transport errors, and bare re-raised exceptions, so the
extremely common pattern `logger.warning("...: %s", e)` produced log lines
with no reason at all.

**Fix**: new shared pure-stdlib helper in `app/core/utils.py`:

```python
format_exception_message(exc: BaseException) -> str
```

- Non-empty `str(exc)` is returned verbatim (existing messages, and any
  alerting keyed on them, are unchanged).
- Empty `str(exc)` falls back to the class name (`"TimeoutError"`), and
  appends the chained cause when present
  (`"RetryError (caused by ConnectTimeout)"`); `raise ... from None`
  suppression is honoured.

Applied by an AST-verified codemod to **561 logging call sites in 116
files**: every logging call that interpolated the enclosing `except`
handler's variable (as `e`, `str(e)`, or in an f-string) now routes through
the helper. Calls that already used `%r` were deliberately left alone
(repr already includes the class name).

The shared integration-error formatter `format_network_error()` in
`app/integrations/http.py` (used by all integration providers and
`safe_request`) also routes its fallback branch through the helper, which
fixes the whole provider tool-call surface at one leverage point.

**Helper unification**: the parallel KB-sync hardening PR landed a
near-identical `format_exc()` in `core/utils.py` (str-or-repr fallback).
To keep ONE canonical helper, `format_exc()` was removed and its users
(`kb_sync.py`, `kb_sync_scheduler.py`, `tests/test_kb_sync.py`) now call
`format_exception_message()` - same non-empty guarantee, plus cause-chain
reporting and containment of raising `__str__` implementations.

### 2. Severity miscalibration - 4 rebalances (all warning -> error)

Deliberately conservative; each change is a case where the platform
silently loses a deliverable or a configured behaviour:

| Site | Old -> New | Justification |
|---|---|---|
| `app/services/integration_tools.py` (`Failed to build tools for provider ...`) | warning -> error (+ traceback) | The agent silently runs WITHOUT that provider's tools. The same event class was already `error` in `assignment_engine.py` and `general_chat.py` - three sites, three levels, now consistent. |
| `app/services/assignment_engine.py` (`Failed to create retry run ...`) | warning -> error (+ traceback + run id) | The configured retry silently never happens; the failed run is permanently abandoned. |
| `app/services/assignment_engine.py` (`HITL notification failed for run ...`) | warning -> error (+ traceback) | The user never learns the run is waiting for their input; the run stalls until timeout. |
| `app/services/assignment_engine.py` (`Notification delivery failed for run ...`) | warning -> error (+ traceback) | For channel-routed assignments the notification IS the deliverable - losing it means the run output never reaches the user. |

Reviewed but intentionally NOT changed:

- `agent_chat.py` `Failed to load memory for chat` stays at `error` - a
  previous PR added an explicit comment justifying the level.
- `event_poller.py` per-assignment poll errors stay `warning` - transient
  Graph/Slack blips would page at error level; persistent-failure
  escalation would need state (pattern to copy: `kb_sync_scheduler`'s
  consecutive-failure quarantine).
- Channel send failures (`Slack/Teams/WhatsApp send_message failed`) were
  already `error`.

### 3. Silently swallowed exceptions - guarded

17 `except ...: pass` sites found. All but one use narrow exception types
for parse/cleanup fallbacks (`json.JSONDecodeError`, `ValueError`, ...) and
are legitimate. The only broad ones (`except Exception: pass`) are the
matplotlib capture cleanup inside the code-interpreter sandbox preamble and
one site in `custom_tool_executor.py` (owned by the parallel workstream).

**Fix**: `tests/test_logging_conventions.py` adds an AST-based guard that
fails the build if a broad (`Exception`/`BaseException`/bare) handler with
a body of only `pass` is introduced anywhere in `app/`, with an explicit
allowlist for the two reviewed sites.

### 4. Lost tracebacks in background paths - 8 sites fixed

`exc_info=True` added where an `error`-level log inside a broad catch had
no traceback (schedulers, channel webhooks, background collectors):

- `app/channels/teams.py` - Graph subscription creation error
- `app/channels/google_chat.py` - Google Chat auth error
- `app/services/kb_sync_scheduler.py` - scheduled KB sync failure
- `app/services/ai_metrics_collector.py` - GH Copilot enterprise download error
- the four severity-promoted sites above (traceback added with promotion)

The autonomous scheduler (`autonomous.py`) was already exemplary - every
tick-section catch logs at `error` with `exc_info=True`.

### 5. Missing context - 10 sites enriched

Cheap, in-scope identifiers added so lines are actionable:

- `assignment_helpers.py` memory-load failures now carry `agent=`/`user=`
- `integration_tools.py` tenant-URL rewrite failure now carries the
  integration name + provider
- `channels/router.py` thread mark/mute/unmute/check failures now carry
  `channel=` and `thread=`
- `tools/mcp_client.py` disconnect errors now carry the reason
- `assignment_engine.py` retry-run failure now carries the run id

(Structural note: `agent_chat` / `integration_tools` already bind
`structlog.contextvars` for agent/integration context - new code should
prefer that pattern for per-request fields.)

### 6. Secrets / PII in logs - 1 finding, fixed

- **`app/services/ai_metrics_collector.py`**: on a GitHub Copilot token
  refresh that returned 200 without an `access_token`, the FULL parsed
  token-endpoint payload was logged (`token_data`) - such a payload can
  carry a `refresh_token` even when `access_token` is absent. Now logs the
  response **keys** only. (The GitHub Copilot collector was removed in
  #11380.)

Reviewed, no leak: token-endpoint failure logs elsewhere only include
truncated 4xx/5xx error bodies (no tokens present on those paths);
`_log_request_warning` in `integrations/http.py` logs `urlparse(url).path`
with query/fragment stripped; skill auto-refresh redacts URLs; no logging
call interpolates `Authorization` headers or decrypted credentials.

## Conventions going forward

1. When a log line includes an exception message, use
   `format_exception_message(e)` from `app.core.utils` - never raw `{e}` /
   `%s` of `e` (empty for common timeout/cancellation types).
2. `exc_info=True` (or `logger.exception`) is mandatory for `error`-level
   logs in broad `except Exception` handlers on background paths
   (schedulers, workers, webhook processors).
3. No `except Exception: pass` - narrow the type or log at least at debug
   level (enforced by `tests/test_logging_conventions.py`).
4. Severity follows the rubric above; per-item transient failures in retry
   loops are `warning`, escalation to `error` should be deterministic
   (consecutive-failure counters, like the KB sync quarantine).
5. Log the identifiers needed to act (entity id, provider, user id,
   run/conversation id) - prefer `structlog.contextvars.bind_contextvars`
   for per-request fields.
6. Never log token-endpoint payloads, decrypted credentials, or
   `Authorization` headers; log response keys or truncated error bodies
   instead.
