# Quickstart: Validating Sandbox Usage Quota Tracking

**Feature**: [spec.md](./spec.md) | **Contracts**: [contracts/sandbox-usage-quota-contracts.md](./contracts/sandbox-usage-quota-contracts.md)

This is a validation guide for proving the feature works end-to-end once
implemented — not an implementation guide (see `tasks.md` for that).

## Prerequisites

- Local backend running with Redis and PostgreSQL reachable (standard
  `backend/` dev setup).
- `CUSTOM_TOOL_SANDBOX=opensandbox` and a reachable OpenSandbox server
  (`OPENSANDBOX_DOMAIN`, `OPENSANDBOX_API_KEY`) — see `backend/SANDBOX.md`.
  Without a reachable OpenSandbox server, use the mocked unit tests
  described below instead.
- No database migration is needed for this feature (see data-model.md —
  `usage_type` is a free-text column).

## Scenario 1 — Default: sandbox usage counts by default (User Story 1)

1. Start the backend with `disable_sandbox_quota_usage` unset (default
   `False`).
2. As a logged-in user, trigger a code-interpreter execution (e.g. ask an
   agent to run a short Python snippet) that completes successfully.
3. Call `get_user_quota_usage(db, user_id)` (or inspect the `quota_usage`
   table directly: `SELECT * FROM quota_usage WHERE user_id = :id AND
   usage_type = 'sandbox'`).
4. **Expected**: a row exists with `usage_type="sandbox"`, `model=NULL`,
   `agent_id=NULL`, `count=1` for the current month's period key.
5. Repeat with a custom tool (JavaScript, then Python) routed through
   OpenSandbox. **Expected**: the same counter increments again (`count=2`,
   then `3`) — one shared counter across all three sandbox-triggering
   features (FR-004).

## Scenario 2 — Failed executions don't count (User Story 1, scenario 3)

1. Trigger a code-interpreter execution with code that causes the sandbox
   itself to fail before producing output (e.g. an unreachable
   `OPENSANDBOX_DOMAIN`, or a quota-exceeded response from the combined
   agentic+sandbox quota check, FR-015-FR-019).
2. **Expected**: the user-facing error is returned as before (unchanged
   behavior); the `quota_usage` `"sandbox"` counter for that user does
   **not** increment.

## Scenario 3 — Operator opt-out (User Story 2)

1. Restart the backend with `disable_sandbox_quota_usage=true`.
2. Trigger a successful code-interpreter execution and a successful custom
   tool execution.
3. **Expected**: both executions succeed normally; no `"sandbox"`
   `quota_usage` row is created or incremented for that user — but see
   Scenario 9 below: the combined agentic+sandbox check still runs
   regardless of this setting, since it enforces the existing per-user
   agentic limit, not a sandbox-specific one (FR-014).
4. Restart again with `disable_sandbox_quota_usage=false` (or unset).
   **Expected**: the next successful sandbox call is counted again, with no
   migration or other maintenance step needed.

## Scenario 4 — Recording failure doesn't fail the call (User Story 1, clarify Q1)

Unit-test-level scenario (mock Redis to raise on the increment call):

1. Mock the Redis client used by `increment_sandbox_usage` to raise an
   exception on `INCR`.
2. Trigger a successful code-interpreter execution.
3. **Expected**: the execution's result is still returned to the caller
   unchanged; the exception is caught and logged (not propagated); no
   retry is attempted.

## Scenario 5 — Distinct from other usage types (User Story 3)

1. As a user with existing `llm_proxy` and/or `agentic` usage in the
   current period, trigger one successful sandbox execution.
2. Query `quota_usage` for that user and period.
3. **Expected**: three separate rows (or two, if the user has no
   `llm_proxy`/`agentic` usage this period) — `usage_type` values
   `"llm_proxy"`, `"agentic"`, `"sandbox"` — each with its own independent
   `count`, none merged or overwritten.

## Scenario 6 — Sandbox always uses agentic's period, not one of its own (User Story 1, spec FR-011, revised 2026-08-03)

1. Configure a user's `quota_config` (or their group's) with
   `{"agentic": {"default": 5, "period": "daily"}}` — via the existing
   raw-JSON `PATCH`/`PUT` endpoint for user/group quota config.
2. Trigger a successful sandbox execution for that user.
3. **Expected**: the `quota_usage` row for `usage_type="sandbox"` uses a
   daily period key (e.g. `"2026-07-31"`) — the same key as the user's
   `"agentic"` row — proving sandbox usage always follows agentic's
   resolved period rather than resolving one of its own.
4. Attempt to `PUT /quotas` (or a user/group override) with a
   `{"sandbox": {"period": ...}}` key. **Expected**: the key is silently
   dropped — `QuotaConfig` has no `sandbox` field to bind it to — and has
   no effect on where sandbox usage is recorded.
5. `GET` the user's own quota info after step 1. **Expected**: no
   `sandbox` key appears in `resolved_config` — only `llm_proxy`/`agentic`
   — while the `usage` list still includes a `"sandbox"` entry (folded into
   the displayed agentic count per FR-013/FR-014), under the same period
   key as the `"agentic"` entry.

## Scenario 7 — Sandbox usage appears in Prometheus /metrics (User Story 3, spec FR-012)

1. Trigger at least one successful sandbox execution for some user.
2. `GET /metrics` (with valid Prometheus bearer auth).
3. **Expected**: the response includes `xtmone_quota_usage` series with
   `usage_type="sandbox"`, alongside the existing `usage_type="agentic"`
   series — for daily, monthly, and yearly period buckets, same as the
   other usage types.
4. Repeat with `platform_mode` set to `xtm_one` and then to `copilot`.
   **Expected**: `sandbox` series appear in both modes identically —
   unlike `llm_proxy`, which only appears when *not* in `xtm_one` mode,
   sandbox's inclusion is unconditional (research.md §9).

## Scenario 8 — Sandbox usage folded into the displayed agentic count, no separate row anywhere (User Story 3, spec FR-013/FR-014, revised 2026-07-31)

**Superseded**: an earlier version of this scenario (giving sandbox its own
"Sandbox Executions" card/tile on all four pages) was implemented and then
reversed by a later `/speckit-clarify` session — see spec.md's Clarifications
for the full rationale. The ledger, Prometheus `/metrics` (Scenario 7 above),
and `resolve_quota_config`/`get_user_quota_usage` still keep `"sandbox"` and
`"agentic"` fully distinct; only the four display surfaces below merge them.

1. As a user with recorded sandbox usage, view your own Profile page.
   **Expected**: the "Agentic Executions" card's count equals your agentic
   count plus your sandbox count, added together as a single number — no
   separate "Sandbox Executions" card appears anywhere on the page.
2. As an admin, view that user's User Detail page. **Expected**: same
   merged count on the "Agentic Executions" card, no separate sandbox card.
3. As an admin, view a group's Group Detail page. **Expected**: no
   sandbox-related row anywhere — the group's sandbox period configuration
   (if any) has no display on this page (it was a read-only period
   indicator, not a usage count, so there is nothing to merge into agentic's
   group-level card).
4. As an admin, view the platform Quotas settings page. **Expected**: the
   "Agentic Executions" monitoring tile's current-usage number includes the
   platform-wide sandbox usage added in; no separate "Sandbox Executions"
   tile appears. In `xtm_one` mode with the 3-gauge daily/monthly/yearly
   layout, only the monthly gauge includes the added sandbox usage (sandbox
   has no daily/yearly breakdown to add to those gauges).
5. As a user who has never used the sandbox, **expected**: the agentic
   count/tile on every page above is unchanged (sandbox contributes 0), and
   behaves exactly as agentic already did before this feature — including
   showing "No quota configured — unlimited usage" if that was already
   agentic's behavior when unconfigured (this feature does not change
   agentic's own no-config behavior).

## Scenario 9 — Combined agentic+sandbox usage shares one enforcement ceiling (spec FR-015–FR-019, added 2026-08-03)

1. Configure a user with an agentic per-user limit of, say, 5 for the
   current (default monthly) period, and confirm they have 0 agentic and 0
   sandbox usage so far this period.
2. Trigger 3 successful sandbox executions (any mix of code interpreter /
   custom tools) for that user. **Expected**: all 3 succeed; the
   `"sandbox"` counter reads 3, the `"agentic"` counter still reads 0.
3. Trigger 2 successful interactive agent-chat turns for the same user.
   **Expected**: both succeed (combined usage 3 sandbox + 2 agentic = 5,
   at but not over the limit); the `"agentic"` counter now reads 2.
4. Trigger one more sandbox execution. **Expected**: refused — combined
   usage (3 sandbox + 2 agentic = 5) has already reached the limit, even
   though the user's own `"agentic"` counter alone is only 2. The refusal
   never becomes an HTTP 500 or an unhandled exception — the code
   interpreter returns its existing error-dict shape directly; the two
   custom-tool paths raise `RuntimeError`, which is caught generically by
   `CallableToolExecutor.execute_tool` and turned into a normal,
   LLM-visible tool error — not a crash either way.
5. Separately, trigger one more agent-chat turn instead. **Expected**:
   also refused, with the existing agentic quota-exceeded message
   (`agent_chat.py`'s `QUOTA_EXCEEDED_MESSAGE`) — proving the *same* shared
   ceiling blocks both directions, not just sandbox-into-agentic.
6. Repeat step 4/5 via the assignment-engine path (a scheduled/assignment
   agent run) and via the extension/platform chat API paths. **Expected**:
   each is refused using its own existing mechanism (run marked `failed`,
   or `HTTPException(429)` with `Retry-After`) — none of the four existing
   call sites needed new refusal code, only the underlying comparison
   changed (research.md §11.1).
7. Repeat the whole scenario with a leftover `{"sandbox": {"period":
   "monthly"}}` key still present in the user's stored `quota_config` (e.g.
   from before FR-011's 2026-08-03 revision) while their agentic period is
   `"daily"`. **Expected**: behavior is unaffected — the stray key has no
   field to bind to (`QuotaConfig` has no `sandbox` field) and no code path
   reads it; the combined check still sums sandbox usage under the
   *agentic* period's window (FR-017) regardless.
8. Configure the usage-tracking backend (Redis) to be unreachable, with
   `QUOTA_FAIL_MODE=open` (the default). Trigger a sandbox execution for a
   user who would otherwise be within their combined limit. **Expected**:
   the call proceeds unmetered (fails open), exactly like the existing
   agentic checkpoints already do on the same setting. Repeat with
   `QUOTA_FAIL_MODE=closed`. **Expected**: refused (FR-019).

## Unit test coverage (mocked Redis/OpenSandbox, per Constitution Principle III)

Matching this codebase's convention of grouping tests by the module under
test — extend an existing file where one already covers that module,
otherwise add a new one:

- `tests/test_sandbox_usage_quota.py` — `increment_sandbox_usage` unit
  tests: increments correctly, respects `disable_sandbox_quota_usage`,
  never raises on a mocked Redis failure, resolves *agentic's* configured
  period via `db` (FR-011, revised — not one of its own), falls back to
  monthly when `db` is `None` or nothing resolves. Also confirms
  `resolve_quota_config(db, redis, user_id, "sandbox")` always returns
  `None` — sandbox is no longer a recognized quota type — and that a stray
  `"sandbox"` key in a stored config doesn't affect `llm_proxy`/`agentic`
  resolution.
- `tests/test_quota_config_schema.py` — confirms `QuotaConfig` has no
  `sandbox` field, and that a stray `"sandbox"` key in a raw dict is
  silently ignored by `QuotaConfig(**dict)` rather than erroring (the
  pattern used by `PUT /quotas` and both response-construction sites).
- `tests/test_opensandbox_sandbox_mode.py` — this file already covers
  `execute_python_opensandbox`, `execute_js_tool_opensandbox`, and
  `execute_python_tool_opensandbox` (not `test_code_interpreter_sandbox.py`,
  which is about the module-blocklist sandboxing security tests, or
  `test_custom_tool_sandbox_mode.py`, which is about `auto`/`docker`/
  `subprocess` mode *selection* — neither touches opensandbox execution
  semantics). Extend it with: `increment_sandbox_usage` is called on the
  success path, skipped on each failure path (quota exceeded, sandbox
  unreachable, creation failure, hard exit with no output), and skipped
  entirely when `user_id=None`, for all three functions.
- `tests/test_prometheus_metrics.py` (new file — no existing test covers
  `app/api/v1/metrics.py`'s `usage_types`/`_read_quota_total` logic at all,
  confirmed by repo search) — the `metrics()` endpoint's `usage_types` list
  includes `"sandbox"` unconditionally, in both `xtm_one` and non-`xtm_one`
  `platform_mode` (spec FR-012).
- `tests/test_agentic_sandbox_combined_quota.py` (new file — no existing
  test directly unit-tests `check_agentic_quota`'s per-user limit
  comparison in isolation; existing quota tests, e.g. `test_fail_modes.py`,
  cover fail-mode branching and exemption logic, not the limit math itself,
  confirmed by repo search) — `check_agentic_quota` raises once
  `agentic_count + sandbox_count >= limit`, for both the XTM One and
  Copilot branches; the sandbox term is read under the agentic entry's
  resolved period — the only period sandbox usage is ever recorded under
  (FR-011/FR-017, revised); the platform-wide/XTM-ceiling blocks are
  unaffected by a large sandbox count alone.
- `tests/test_opensandbox_sandbox_mode.py` — extend further: the
  pre-dispatch combined check runs for all three functions, gated on
  whether a user to charge quota against is resolvable at all (either
  `user_id` or the caller-supplied `quota_user_id`); on
  `QuotaExceededError`, `execute_python_opensandbox` returns its existing
  error-dict shape while `execute_js_tool_opensandbox`/
  `execute_python_tool_opensandbox` raise `RuntimeError` — not one shared
  shape; resolves its quota check on a dedicated session, not the caller's
  shared `db` (regression test:
  simulate two concurrent calls sharing one mocked `AsyncSession` and
  assert no "concurrent operations are not permitted" error, mirroring the
  existing `increment_sandbox_usage` concurrency test if one exists,
  otherwise modeled on it).
- `tests/test_fail_modes.py` — extend with a case for the
  sandbox-dispatch checkpoint specifically: unreachable quota backend
  branches on `QUOTA_FAIL_MODE` (open proceeds, closed refuses) (FR-019).
