# Phase 0 Research: Sandbox Usage Quota Tracking

**Feature**: [spec.md](./spec.md) | **Date**: 2026-07-31

All items below were resolved by inspecting the existing codebase rather than
external research, since this feature extends an existing, in-repo pattern
(`backend/app/services/quota.py`'s `quota_usage` ledger) to a new usage type.
The most consequential finding (§3) corrects an assumption made in the
source GitHub issue, which proposed instrumenting the three
`execute_*_opensandbox` functions directly — those functions turn out not to
have the user context this feature needs.

## 1. Storage: extend the existing `quota_usage` table, no new table

- **Decision**: Add a new `increment_sandbox_usage(user_id, period="monthly")`
  function to `app/services/quota.py`, writing into the *existing*
  `quota_usage` table/Redis-counter pattern with `usage_type="sandbox"` —
  not a new table.
- **Rationale**: `QuotaUsage.usage_type` (`app/models/quota.py:47`) is a
  free-text `String(30)` column with no enum/CHECK constraint, so a third
  value fits without a migration (confirmed by inspection — this was also
  asserted as an Assumption in spec.md and holds up). This is the whole
  point of the feature: sandbox usage should live in the *same* per-user
  ledger as `llm_proxy`/`agentic`, not a parallel one (spec User Story 3).
- **Alternatives considered**:
  - *A new `sandbox_quota_usage_by_user` table* — rejected: duplicates the
    Redis-fast/Postgres-durable machinery (`_usage_key`, `flush_quota_usage`)
    for no benefit, and defeats User Story 3 (consistent reporting surface).
  - *Extend the existing platform-wide `sandbox_quota_usage` table (from
    the sibling feature) with a `user_id` column* — rejected: that
    table is deliberately schema'd as platform-wide-only (no per-user
    dimension, per its own docstring), and repurposing it would conflate
    two independent counters that spec FR-009 explicitly requires to stay
    independent.

## 2. Period resolution: same precedence as LLM-proxy/agentic (supersedes the original "hardcode monthly" decision)

> **Superseded 2026-07-31** by a follow-up `/speckit-clarify` session run
> after `/speckit-implement` had already shipped code against the original
> decision below (kept struck through, not deleted, so the reasoning that
> led to the reversal stays visible). The shipped code currently hardcodes
> `period="monthly"` with no `db` parameter — this section documents what
> changes to bring it in line with FR-011.

- **Original decision (superseded)**: ~~`increment_sandbox_usage` takes no
  `db` parameter and always resolves to `period="monthly"`~~ — rejected
  because spec FR-011 (added 2026-07-31) now explicitly requires the same
  user > group > platform > monthly-default precedence LLM-proxy/agentic
  usage already gets.
- **New decision**: `increment_sandbox_usage` gains a `db: AsyncSession |
  None = None` parameter, mirroring `increment_llm_proxy_usage`'s shape
  (not `increment_agentic_usage`'s — see rationale below): when `db` is
  provided, call `resolve_quota_config(db, redis, user_id, "sandbox")` and
  use `config.get("period", period)` if a config resolves; otherwise keep
  the `"monthly"` default. `resolve_quota_config` itself (`quota.py:325`)
  must be extended to recognize `"sandbox"` as a third quota type alongside
  its current hardcoded `("llm_proxy", "agentic")` tuple (4 occurrences
  within that one function — the user-level, group-level, and global-level
  resolution loops, *not* the XTM-license agentic-ceiling block, which is
  agentic-only and untouched).
- **Rationale for modeling on `increment_llm_proxy_usage`, not
  `increment_agentic_usage`**: `increment_agentic_usage` additionally
  maintains a *global* platform-wide/license-ceiling counter
  (`_global_usage_key`, `extra_global_periods`, XTM-license ceilings) because
  agentic usage has a real enforceable cap (`check_agentic_quota`). Sandbox
  usage has no such cap — FR-011 is only about which *period* buckets a
  count, not about adding a new enforceable limit (spec Assumptions:
  usage-recording only). So `increment_sandbox_usage` takes on
  `increment_llm_proxy_usage`'s period-resolution *shape* only, still
  incrementing nothing but the single per-user counter (§1's "no global
  companion counter" decision is unaffected by this change).
- **Finding — the write/read side also needs a schema change, not just the
  resolution function**: `User.quota_config`/`Group.quota_config` are
  untyped `dict | None` JSON columns (`app/models/user.py:170`,
  `app/models/group.py:43`) — already accept an arbitrary `{"sandbox":
  {...}}` key today with **no code change needed** for the user/group tiers.
  The **platform-wide tier is different**: `PUT /quotas`
  (`app/api/v1/settings.py:4759`) validates its body against the strict
  `QuotaConfig` Pydantic model (`app/api/v1/schemas/settings.py:316`, fields
  `llm_proxy` / `agentic` only) — a `"sandbox"` key sent today is silently
  dropped by Pydantic's default extra-field handling, permanently closing
  off the "platform-wide default" rung of FR-011's precedence ladder via the
  UI/API (though the JSON column itself has room for it). The **same
  `QuotaConfig` model is also reused to *read back* a user's raw override**
  in two response-construction call sites (`app/api/v1/users.py:1395-1396`,
  `app/api/v1/settings.py:4807`, both doing `QuotaConfig(**some_dict)`) — so
  even a `"sandbox"` key an admin successfully writes directly into a user's
  or group's raw JSON column today would be silently stripped when that
  config is read back through either of those two response paths, making it
  invisible in the existing quota-config UI.
- **Decision**: Add `sandbox: QuotaTypeConfig | None = None` to `QuotaConfig`
  (`app/api/v1/schemas/settings.py:316`) — reuses the existing
  `QuotaTypeConfig` shape (`platform_limit`, `default`, `per_model`,
  `period`) for consistency with its siblings, fixes the PUT-endpoint
  silent-drop and both response-construction silent-strip sites for free
  (all three already operate generically on whatever fields `QuotaConfig`
  declares), and requires no other code change at those three call sites.
  Document with a rationale comment (Constitution Principle IV) that
  `platform_limit`/`default`/`per_model` are inert for `"sandbox"` — only
  `period` has any effect — since this feature adds no `check_sandbox_*`
  enforcement function to read them, unlike the identically-shaped
  `llm_proxy`/`agentic` fields, which real enforcement functions do consult.
- **Alternatives considered**:
  - *Leave `increment_sandbox_usage` hardcoded, treat FR-011 as covering
    only "the code is capable of the same mechanism in principle"* —
    rejected: FR-011's plain text requires actual per-user/group/platform
    resolution to happen, not just architectural capability; the original
    (superseded) decision's own rejected-alternative reasoning ("don't
    design for hypothetical future requirements") no longer applies once
    the requirement is no longer hypothetical.
  - *Add a narrower schema (bare `period: str` field) instead of reusing
    `QuotaTypeConfig`* — rejected: introduces a second, inconsistent shape
    for what admins conceptually experience as "the same kind of setting"
    across three usage types, for a marginal reduction in inert-field
    surface area; a code comment documenting which fields are inert is
    cheaper than a second type.
  - *Skip the `QuotaConfig` schema change, accept the platform-tier gap* —
    rejected: FR-011 explicitly enumerates "platform-wide default" as one of
    the four precedence rungs; leaving it permanently unreachable via the
    existing admin surface would only partially satisfy the requirement.
  - *Also extend `resolve_full_quota_info` (the "my quotas" overview
    combining config + usage + source, `quota.py:384`)* — deferred, same
    reasoning as §7: that function backs a *display* surface, not the
    counting mechanism FR-011 governs, and remains out of scope for this
    plan for the same reason `get_user_quota_usage`/`metrics.py` do.
- **Deployment-transition note**: `resolve_quota_config`'s resolved-config
  cache (`_config_cache_key`, 5-minute TTL) is per-user, not per-quota-type
  — a cache entry populated by pre-deploy code (which never wrote a
  `"sandbox"` key) simply has no such key, so `.get("sandbox")` returns
  `None` gracefully (falls back to the `"monthly"` default) until that
  user's cache entry naturally expires and is rebuilt by the new code. No
  special cache-invalidation handling needed — the existing TTL is already
  the self-healing mechanism.

## 3. Where to call the increment: inside the three `execute_*_opensandbox` functions, not their outer callers — requires threading `user_id`

This is the key correction to the originally proposed approach.

- **Finding**: None of the three functions the issue named as call sites —
  `execute_python_opensandbox` (`code_interpreter.py:472`),
  `execute_js_tool_opensandbox` / `execute_python_tool_opensandbox`
  (`custom_tool_executor.py:1182,1264`) — currently receive a `user_id`
  parameter. `user_id` is only available further up each call chain:
  - Code interpreter: `build_code_interpreter_tool`'s `_execute_code`
    closure in `app/services/builtin_tools/media.py` (receives `user_id`,
    calls `execute_python(code, timeout=300)` → `execute_python_opensandbox`).
  - Custom tools (production agent loop): `load_custom_tools`'s
    `_run_custom` closure in `app/services/builtin_tools/loaders.py`
    (receives `user_id`, calls `execute_tool_auto(...)` →
    `execute_js_tool_opensandbox`/`execute_python_tool_opensandbox`).
  - Custom tools (the "test this tool" endpoint): `app/api/v1/custom_tools.py`
    has `user.id` in scope directly and also calls `execute_tool_auto(...)`.
- **Decision**: Thread a new optional `user_id: uuid.UUID | None = None`
  keyword parameter through the full call chain (`execute_python` →
  `execute_python_opensandbox`; `execute_tool_auto` →
  `execute_js_tool_opensandbox` / `execute_python_tool_opensandbox`), and
  call `increment_sandbox_usage(user_id)` **inside** each of the three
  `execute_*_opensandbox` functions, on the success path only, right where
  `record_sandbox_call()` already sits.
- **Rationale — why not call it from the outer closures instead (the
  simpler-looking alternative)**: `execute_python`/`execute_tool_auto` are
  mode-transparent dispatchers — from the closure's point of view, a call
  might have been routed to OpenSandbox, Docker, or a plain subprocess, and
  the closure has no way to tell which. The spec (Assumptions) explicitly
  scopes this feature to OpenSandbox-routed executions only, matching the
  existing platform-wide `sandbox_quota_usage` counter's own scope. Calling
  `increment_sandbox_usage` from inside the `execute_*_opensandbox`
  functions is what actually *guarantees* OpenSandbox-only scoping "for
  free" — those functions are only ever reached when the resolved sandbox
  mode is `opensandbox`. Hooking at the outer closures would silently start
  counting Docker/subprocess-routed executions too, which is out of scope.
  Threading one extra optional parameter through two thin dispatcher
  functions (`execute_python`, `execute_tool_auto`) is a small price for
  that correctness guarantee.
- **Alternatives considered**:
  - *Increment from the outer closures using the existing return value/
    exception as the success signal* — rejected per the mode-transparency
    problem above; would over-count non-OpenSandbox executions.
  - *Have the closures pass a `sandbox_mode` flag down so the closure
    itself could gate the call* — rejected: this just re-implements, at
    the closure level, information the `execute_*_opensandbox` functions
    already encode by construction (their own identity), for no benefit.

## 4. Success predicate per call site

- **Decision**:
  - `execute_python_opensandbox`: place the `increment_sandbox_usage` call
    right after the `if result.exit_code != 0 and not stdout_text:` early
    return (i.e., once execution reaches that point, the sandbox itself ran
    to completion) and before the subsequent JSON-parsing block — so both
    of that function's two remaining "normal" return paths (parsed
    structured JSON, or raw-stdout fallback with `"error": None`) count.
  - `execute_js_tool_opensandbox` / `execute_python_tool_opensandbox`: both
    already have a single, clean success path — a `RuntimeError` is raised
    for every failure mode (sandbox creation failure, nonzero exit code),
    and the function returns exactly once, at the very end, on success.
    Place the call immediately before that final `return` statement.
- **Rationale**: This treats "successful sandbox call" as "the sandbox
  infrastructure executed the request and returned a result" — matching
  FR-002's plain-language "fail or error out" framing and consistent with
  how the existing platform-wide `record_sandbox_call()` already thinks
  about acceptance/execution, not "the user's own code was bug-free." A
  user's script raising an unhandled exception *inside* the sandbox still
  consumed real CPU/container time and should still count; only
  infrastructure-level failures (quota exceeded, OpenSandbox unreachable,
  sandbox creation failure, or a hard exit with no output at all for the
  code interpreter) are excluded, and those already short-circuit via an
  early `return`/`raise` before reaching the chosen instrumentation point.
- **Alternatives considered**:
  - *Gate on `result.get("error") is None"` for the code interpreter* —
    rejected: would also exclude a legitimate sandbox execution whose
    wrapper JSON happens to report a user-code-level error, which the spec
    doesn't ask us to distinguish and would undercount real resource
    consumption.

## 5. Fail-open error handling for the increment itself

- **Decision**: `increment_sandbox_usage` never raises. Internally it
  checks the new `disable_sandbox_quota_usage` setting (no-op early return
  if set) and wraps its Redis increment in a blanket `try/except Exception`
  that logs at `debug` level and returns — deliberately *not* propagating,
  unlike its siblings `increment_llm_proxy_usage`/`increment_agentic_usage`,
  which let exceptions propagate and rely on their callers to decide
  fail-open/fail-closed behavior.
- **Rationale**: This directly implements the `/speckit-clarify` decision
  (2026-07-31, Q1): a failure to record usage must never fail an
  already-completed sandbox call. `increment_llm_proxy_usage`'s existing
  caller (`_track_model_usage` in `app/api/v1/llm_proxy.py:190-217`) already
  establishes this exact "best-effort, log at debug, never propagate"
  precedent for the *identical scenario* (metering after the fact, not
  gating a not-yet-performed action) — reusing that shape here, but
  building the swallow *into* `increment_sandbox_usage` itself (rather than
  requiring each of 3 call sites to remember to wrap it) removes a
  copy-pasted try/except from 3 places to 1.
- **Alternatives considered**:
  - *Match `increment_agentic_usage`'s contract exactly (let it raise,
    require callers to wrap)* — rejected: would need the identical
    try/except boilerplate duplicated at all three call sites for no
    behavioral difference, and one of the three (a future call site,
    should one appear) forgetting the wrapper would silently violate the
    fail-open decision.

## 6. Configuration flag

- **Decision**: New `Settings` field `disable_sandbox_quota_usage: bool =
  False` in `app/core/config.py`, checked once inside
  `increment_sandbox_usage` (single choke point, not re-checked at each of
  the 3 call sites). Standard environment variable, read once at process
  startup — no runtime hot-reload.
- **Rationale**: Matches the `/speckit-clarify` decision (2026-07-31, Q2):
  restart-required, matching every other on/off `Settings` toggle in this
  codebase (confirmed: no field in `config.py` supports hot-reload — an
  explicit comment at `config.py:1034` states "there is no runtime
  hot-reload"). Naming follows the closest local precedent,
  `disable_code_interpreter`/`disable_custom_tools` (`config.py:953-954`,
  same file, same sandbox-adjacent feature area), rather than the more
  generic `<feature>_enabled: bool` pattern used elsewhere in the file —
  consistency with immediate neighbors wins over a repo-wide-but-more-distant
  convention.
- **Alternatives considered**:
  - *`sandbox_quota_usage_disabled` (the originally suggested name)* —
    functionally identical; renamed to the verb-first `disable_*` form to
    match the two existing sibling flags in this exact settings block
    rather than introducing a third naming style in the same neighborhood.

## 7. Reporting surface (User Story 3, P3) — partially reversed 2026-07-31 (see §9); per-user display remains out of scope

- **Finding**: `get_user_quota_usage` (`quota.py:819`), the function backing
  the user-facing usage display, hardcodes `for qt in ("llm_proxy",
  "agentic"):` — adding `"sandbox"` usage to the ledger does **not**
  automatically make it appear in that display. The same hardcoded-list
  pattern also exists in the admin-facing Prometheus endpoint
  (`app/api/v1/metrics.py:166`, `usage_types = ["agentic"]` +
  conditionally `"llm_proxy"`).
- **Original decision (still holds for `get_user_quota_usage` only)**: Not
  addressed by this plan's tasks. Recording is the P1/P2 scope; User Story 3
  (P3) is satisfied by the *data* being distinguishable in the ledger
  (independently testable by inspecting recorded rows), not by requiring
  every display surface to be updated. Flagging the hardcoded list here so a
  future follow-up has a concrete starting point.
- **Superseded 2026-07-31 for the Prometheus `/metrics` list specifically**:
  a follow-up `/speckit-clarify` session added spec FR-012, requiring
  sandbox usage to appear in `/metrics`. See §9 for the concrete decision —
  this is a narrower reversal than "include updating both lists" (the
  rejected alternative below): only the Prometheus list is now in scope; the
  per-user `get_user_quota_usage` display remains deferred, per spec User
  Story 3's own updated framing ("the platform-wide Prometheus metrics
  surface is in scope... while per-user usage-display UI updates remain a
  documented follow-up").
- **Alternatives considered**: *Include updating both lists in this
  feature's tasks* — originally deferred rather than rejected outright;
  partially adopted 2026-07-31 for the Prometheus list only (§9), still
  deferred for `get_user_quota_usage`.

## 9. Exposing sandbox usage in `/metrics` (spec FR-012, added 2026-07-31)

- **Finding**: `_read_quota_total` (`app/api/v1/metrics.py:65`), the
  function that actually reads and records `QUOTA_GAUGE` values, is fully
  generic per `usage_type` — it queries `QuotaUsage.usage_type == qt` and
  scans Redis key patterns parameterized by `qt`, with no hardcoded set of
  recognized types. It requires **no code change** for a new type. The only
  gate is the `usage_types` list built in the `metrics()` endpoint handler
  itself (`app/api/v1/metrics.py:170-172`):
  ```python
  usage_types = ["agentic"]
  if not settings.is_xtm_one:
      usage_types.append("llm_proxy")
  ```
  `QUOTA_GAUGE`'s labels (`user_id`, `usage_type`, `model`, `period`,
  `platform_id`) also need no change — `usage_type="sandbox"` is just a new
  label *value*, not a new label.
- **Decision**: Add `"sandbox"` to `usage_types` **unconditionally** —
  outside the `if not settings.is_xtm_one:` conditional, alongside
  `"agentic"` — per `/speckit-clarify`'s 2026-07-31 resolution (spec FR-012).
- **Rationale**: `increment_sandbox_usage` has no platform-mode branching
  anywhere — sandbox usage is recorded identically in every mode. Gating its
  visibility in `/metrics` behind `is_xtm_one` (the way `"llm_proxy"` is
  gated, for reasons specific to how LLM-proxy usage relates to XTM
  licensing/billing in that mode) would hide real, always-collected data for
  a reason that has nothing to do with how sandbox usage actually works.
  `"agentic"`'s unconditional inclusion is the correct precedent to follow,
  not `"llm_proxy"`'s conditional one.
- **Alternatives considered**:
  - *Gate like `"llm_proxy"`* — rejected per the `/speckit-clarify` decision
    and the rationale above; there is no sandbox-specific reason (analogous
    to whatever drives `llm_proxy`'s exclusion in `xtm_one` mode) to hide
    this data in any particular mode.
  - *Also update `get_user_quota_usage` in the same pass* — rejected/deferred;
    the `/speckit-clarify` session that produced FR-012 was scoped
    specifically to "the quotas read by Prometheus through `/metrics`," and
    §7's original P3 deferral reasoning still holds for that separate,
    per-user display surface.

## 10. Displaying sandbox usage on existing usage-review pages (spec FR-013/FR-014, added 2026-07-31)

Fully reverses §7's remaining deferral. This turned out to touch **three**
backend read functions and **four** frontend files — materially larger than
FR-011/FR-012, discovered only by tracing each of the four named pages back
to its actual data source.

### 10.1 Backend: three functions, not one

- `get_user_quota_usage` (`quota.py:846`) — powers the *usage counts* shown
  on Profile/UserDetail. Same shape as `resolve_quota_config`: hardcodes
  `for qt in ("llm_proxy", "agentic"):`. **Decision**: extend to the shared
  `_QUOTA_TYPES` constant (§2/§9) instead of a third separate literal tuple.
- `resolve_full_quota_info` (`quota.py:394`) — powers the **source badge**
  ("User override" / "Group quota" / "Platform default" / "No quota") shown
  next to each usage card on Profile/UserDetail, and the **limit text**
  shown on GroupDetail. Also hardcodes the same tuple. **Finding**: without
  extending this too, `sources["sandbox"]` is always absent → the frontend's
  `sources[key] ?? "none"` fallback makes every sandbox card render as
  `source === "none"`, which on Profile/UserDetail **hides the usage count
  entirely** and shows "No quota configured — unlimited usage." instead —
  directly contradicting FR-013's "showing the current usage count."
  **Decision**: extend this to `_QUOTA_TYPES` too (skip only the
  agentic-only XTM-license fallback branch, same as §9's rule for
  `resolve_quota_config`).
- `get_platform_quota_monitoring` (`quota.py:1475`) — powers
  `QuotasSection.tsx` (the platform settings page), and is a **different,
  limit-centric aggregate dashboard** (near/over-limit user counts, top
  consumers ranked by usage-vs-limit ratio) — not `get_user_quota_usage` at
  all. Its per-type loop (`for qt in ("agentic", "llm_proxy"):`,
  `quota.py:1652`) reads a configured `platform_limit`/`default` to decide
  *whether to emit a summary row at all* for that type. **Finding**: its
  internal `_scan_period(qt, period, limit_val, is_global=True)` helper
  already handles `limit_val=None` gracefully — with `is_global=True` the
  near/over-limit block is skipped entirely, and it still returns a valid
  `current_usage`/`active_users` summary. **Decision**: call
  `_scan_period("sandbox", "monthly", None, is_global=True)` unconditionally
  and append its result to `quotas_summary` — a global-only, limit-less
  summary row, not folded into the existing `for qt in (...)` branch (which
  is entangled with `platform_cfg.get(qt)` limit lookups that don't apply).

### 10.2 Frontend: two different card shapes needed, not one — **SUPERSEDED 2026-07-31, see §10.4**

> This subsection describes the design that was actually implemented first,
> then reversed by a same-day `/speckit-clarify` session after the operator
> decided a dedicated per-page "Sandbox Executions" row was more visual
> clutter than value for a usage type with no enforceable limit of its own.
> Kept here for the historical record (same convention as §7's "partially
> reversed" note); §10.4 documents what's actually implemented now. §10.1
> (the backend) is unaffected by the reversal and remains accurate as-is.

- **Profile.tsx / UserDetail.tsx** (per-user usage display, near-identical
  card logic in each): add `"sandbox"` to the `QUOTA_TYPES`/`types` array,
  unconditionally (no `isXtmOne` gating — same reasoning as §9). **Two
  deviations from the existing card logic are both required, not one — a
  first pass caught only the second and would have shipped broken**:
  1. The existing card's top-level branch is `source === "none" ? <p>No
     quota configured — unlimited usage.</p> : <>...show count...</>` — when
     `source` is `"none"` (resolved config absent), the count is not merely
     unbounded, it is **not rendered at all**. Since sandbox will have
     `source === "none"` in the overwhelming majority of cases (no admin UI
     sets a per-user/group sandbox period by default), leaving this branch
     untouched would mean the sandbox card almost always shows "unlimited
     usage" text instead of the count — directly contradicting FR-013's
     "showing the current usage count." **Decision**: for `key ===
     "sandbox"`, this branch must be bypassed — always render the
     count-display branch regardless of `source`.
  2. Separately, `QuotaConfig.sandbox.default`/`.platform_limit` are schema
     fields an admin *can* set (§2 — accepted and stored, just inert) via
     the platform `/quotas` endpoint. If one is ever set, `source` would
     resolve to `"global"` (not `"none"`) and the existing logic would
     compute `hasLimit = true`, rendering a misleading
     percentage-toward-a-cap that is never actually enforced — the exact
     failure mode FR-014 exists to prevent independently of point 1.
     **Decision**: for `key === "sandbox"`, also force `hasLimit = false`
     unconditionally (ignore `cfg?.default` entirely for this type).
  Net effect: `key === "sandbox"` always takes the plain "show count, no
  bar" path, regardless of `source` or whether `default` happens to be set.
  Skip the per-model block entirely (already gated on `key === "llm_proxy"`,
  needs no change).
- **GroupDetail.tsx** (group-level quota *config*, not usage — no per-group
  usage counts exist, since usage is tracked per-user): add `"sandbox"` to
  its `QUOTA_TYPES` array, unconditionally. Its card shows *configured
  limit* text ("Each member gets X requests per period" / "Not set"), which
  has no sandbox equivalent (no limit concept). **Decision**: scope the
  sandbox card here to **read-only period display** only — "Configured"
  badge and "period" text if `group.quota_config?.sandbox?.period` is set,
  "Not set" otherwise — with no limit text and no editable `QuotaEditor`
  input for sandbox. Extending `QuotaEditor` (and the `handleSave` payload
  construction, and the equivalent construction in UserDetail.tsx) to
  support editing a sandbox *period* (with no limit field) is deliberately
  **out of scope for this plan** — FR-013's literal requirement is display,
  not editing; the platform-wide tier is already editable via the existing
  `PUT /quotas` API (§2), so admins are not fully blocked, just without a
  dedicated UI control at the user/group tier yet. Flagging this the same
  way §7 flagged the original per-user-display gap, for the same reason:
  don't silently build unrequested scope, don't silently drop a real gap
  either.
- **QuotasSection.tsx** (platform-wide monitoring dashboard): add a
  `sandbox` entry to `QUOTA_TYPE_META` (label "Sandbox Executions", a new
  icon/color not already used by `agentic`/`llm_proxy`) so
  `QuotaProgressCard` renders the new summary row from §10.1's
  `get_platform_quota_monitoring` addition. **Verified**: `QuotaProgressCard`
  already has a complete, correct "no limit" rendering path (`hasLimit =
  quota.limit != null && quota.limit > 0`; when false, shows the plain
  icon instead of a percentage, the raw count, and "No platform limit" text)
  — this is exactly FR-014's required behavior, and needs **no new
  conditional logic**, only the same "always treat as limit-less" precaution
  as the per-user cards: since `quota.limit` here is sourced from
  `platform_cfg.get("sandbox", {}).get("default")` (§10.1's summary row
  intentionally passes `limit_val=None` unconditionally rather than reading
  the config), this card can never show a misleading percentage for
  sandbox even if an admin sets the inert `default` field via `PUT /quotas`.

### 10.3 Alternatives considered

- *Extend `get_platform_quota_monitoring`'s existing `for qt in ("agentic",
  "llm_proxy"):` branch to also loop over `"sandbox"`* — rejected: that
  branch's whole structure (`platform_cfg.get(qt)` → conditionally emit a
  global row *only if* `platform_limit` is set, conditionally emit a
  per-user row *only if* `default` is set) assumes a limit-bearing type;
  shoehorning sandbox through it would mean the summary row simply never
  appears unless an admin happens to set the (inert, unenforced) `default`
  field — the opposite of always-visible, which is what FR-013 asks for.
  Calling `_scan_period` directly and unconditionally is both simpler and
  correctly always-visible.
- *Extend `QuotaEditor` for user/group-level sandbox period editing now* —
  deferred (10.2); a real gap, explicitly flagged, not silently dropped.
- *Read `quota.limit` from the actual (possibly-set) `default`/`platform_limit`
  config value for the `QuotasSection.tsx`/card-level sandbox entries,
  trusting admins never populate the inert fields* — rejected: FR-14 is a
  hard requirement, not a best-effort one; forcing "no limit" unconditionally
  in code is a correctness guarantee, not a convention that can be violated
  by an admin's action elsewhere in the same feature (the `PUT /quotas`
  endpoint this same effort already wired in §2).

### 10.4 Reversal (2026-07-31): sandbox display folded into agentic, no separate row

§10.2's design shipped, then was reversed the same day: sandbox usage no
longer gets its own row anywhere in the frontend. §10.1's backend read
functions are entirely unaffected and unchanged — `get_user_quota_usage`,
`resolve_full_quota_info`, and `get_platform_quota_monitoring` still return
`"sandbox"` as its own distinct entry, and Prometheus `/metrics` (§9) still
exposes it as its own series. Only the four frontend files change:

- **Profile.tsx / UserDetail.tsx**: removed `"sandbox"` from the
  `types`/`QUOTA_TYPES` array entirely (no card). When rendering the
  `"agentic"` row, add the user's `usage_type === "sandbox"` count into the
  displayed `current` number via the shared `mergeSandboxIntoAgenticCount`
  helper (`@/lib/utils.ts`) — extracted after a code-review pass caught the
  logic duplicated near-verbatim across both files. All of §10.2's two
  overrides (bypass `source === "none"`, force `hasLimit = false`) are gone
  — the `"agentic"` row keeps its original, unmodified display logic;
  sandbox only ever contributes to the numerator, **and only when both
  entries resolve to the same period** — each usage type resolves its own
  effective period independently (FR-011: user > group > platform > monthly
  default), so an agentic entry on a daily period and a sandbox entry on a
  monthly period are never summed (a code-review finding: the first version
  of this merge summed unconditionally, which could inflate/misrepresent a
  user's agentic quota status whenever the two periods didn't match).
- **GroupDetail.tsx**: fully reverted to its pre-§10.2 shape — no
  `"sandbox"` entry in `QUOTA_TYPES` at all. There was no usage count on
  that row to begin with (it showed group-level sandbox *period config*,
  not usage), so there is nothing to fold into agentic's group-level card.
- **QuotasSection.tsx**: removed the `sandbox` entry from `QUOTA_TYPE_META`
  and the standalone summary tile. The platform-wide sandbox current-usage
  (from §10.1's `_scan_period` row, see the period fix below) is added into
  `agenticGauge.current_usage` for the single-gauge layout, and into the
  matching-period entry of `agenticQuotas` for the XTM 3-gauge
  daily/monthly/yearly layout — in both cases gated on the sandbox row's
  resolved `.period` matching the agentic entry's `.period`, the same
  period-match guard as Profile.tsx/UserDetail.tsx above (originally this
  compared against a hardcoded `"monthly"` literal, which broke once the
  backend period-hardcoding bug below was fixed and sandbox could resolve
  to a non-monthly period).

**Related fix — platform monitoring's sandbox row hardcoded `"monthly"`**:
`get_platform_quota_monitoring`'s sandbox summary row originally called
`_scan_period("sandbox", "monthly", None, is_global=True)` unconditionally,
ignoring an admin-configured `QuotaConfig.sandbox.period` (unlike the
sibling `agentic`/`llm_proxy` branches just above it, which read
`cfg.get("period", "monthly")` from `platform_cfg`). A code-review pass
caught this: usage recorded under a daily/yearly bucket would silently
never appear on the monitoring dashboard. Fixed to read
`(platform_cfg.get("sandbox") or {}).get("period", "monthly")` before
scanning, matching the sibling branches' pattern.

**Related fix — concurrent DB session reuse in `increment_sandbox_usage`**:
a code-review pass also caught that `increment_sandbox_usage`'s period
resolution (`resolve_quota_config(db, ...)`) reused the caller's shared,
per-request `AsyncSession`. Sandbox-routed tool calls can run concurrently
within one agent turn (`agent_loop.py`'s parallel tool-call batch via
`asyncio.gather`), all sharing that turn's one session — SQLAlchemy's
`AsyncSession` forbids concurrent operations on the same session (the same
class of bug `sub_agent.py`'s `count_active_sub_agent_executions` already
documents and avoids). Fixed by resolving the period on a dedicated,
independent session (`async with async_session() as own_db:`) opened
inside `increment_sandbox_usage` itself, rather than the caller's shared
one — the passed `db` argument now only signals that resolution should be
attempted at all, matching the existing codebase convention for this exact
concurrency hazard.

**Why display-only, not a real merge into agentic's counter**: sandbox
counting toward agentic's *enforced* limit would mean sandbox calls could
newly trigger agentic blocking — a behavior change with real user impact,
never requested, and contradicted by §7's original framing of sandbox
enforcement as already handled by the separate, pre-existing
`sandbox_quota_usage` platform-wide counter (spec FR-009). Keeping the
merge display-only (numerator-only, on a copy of the row, never touching
`increment_agentic_usage`/`check_agentic_quota`) preserves that separation
while still satisfying the operator's request not to clutter the UI with a
usage type that has no limit of its own.

**Superseded 2026-08-03 — see §11.** The "never requested" premise above no
longer holds: a `/speckit-clarify` session explicitly requested exactly this
behavior change (spec FR-015–FR-019). `sandbox_quota_usage` (spec
`001-sandbox-call-quotas`) remains untouched and independent (FR-009 still
holds) — only agentic's *own* per-user enforcement now also counts sandbox
usage, which is a different mechanism than the platform-wide counter this
paragraph was originally defending.

## 11. Combined agentic+sandbox enforcement (spec FR-015–FR-019, added 2026-08-03)

A further `/speckit-clarify` session reversed §10's "display-only" framing
for the *enforcement* half specifically (the display merge from §10.4
stays exactly as it is — this section is purely additive on top of it):
agentic's own enforced per-user limit must now be checked against the
**sum** of agentic + sandbox usage, not agentic usage alone.

### 11.1 Where "agentic usage alone is checked today" actually is — four call sites, not two

The clarify session's Q&A named two call sites illustratively (interactive
chat, assignment-triggered runs), but FR-015 itself is written generically
("every point that today checks agentic usage alone"). A repo-wide search
for `check_agentic_quota` callers finds **four**, not two:

1. `app/services/agent_chat.py:547` (inside `enforce_agentic_quota_ex`) —
   interactive agent chat. Swallows `QuotaExceededError` into
   `QUOTA_EXCEEDED_MESSAGE`, returned as an in-conversation refusal.
2. `app/services/assignment_engine.py:1275` — scheduled/assignment-triggered
   runs. Catches `QuotaExceededError`, sets `run.status = "failed"` /
   `run.error`.
3. `app/api/v1/extension.py:88` — the browser-extension "execute task"
   endpoint. Lets `QuotaExceededError` propagate to an `HTTPException(429)`
   with a `Retry-After` header.
4. `app/api/v1/platform.py:2452` — the platform/XTM One chat-streaming
   endpoint's pre-flight check (`quota.py`'s own docstring calls this
   "general chat"). Same `HTTPException(429)` pattern as extension.py.

**Decision**: modify `check_agentic_quota()` itself (the single shared
function all four call — `quota.py:611`) to sum sandbox usage into the
per-user comparison, rather than touching four call sites individually.
This satisfies FR-015 for all four automatically, with each site's existing
refusal mechanism (soft in-conversation message, run-failure, or HTTP 429)
completely unchanged — only the *quantity being compared to the limit*
changes, not how a breach is surfaced. This is also why FR-018 (soft
refusal) only needed to be scoped to the *new* sandbox-dispatch checkpoint
below: none of these four pre-existing checkpoints need new refusal
handling, since they already have some.

**Alternative considered**: add the summation at each of the four call
sites instead of inside `check_agentic_quota` itself — rejected. Four
call sites means four chances to forget the sandbox term or apply it
inconsistently (e.g. under the wrong period window, contradicting FR-017);
a single choke point inside the shared function makes the combination
structurally impossible to skip.

### 11.2 Implementation shape inside `check_agentic_quota`

Both the XTM One and Copilot branches of `check_agentic_quota` (`quota.py:641-669`)
already compute `period_key` from the agentic config's resolved period
before fetching `current = await _get_user_count(redis, user_id, "agentic",
None, period_key)`. Per FR-017, add one line immediately after that fetch:

```python
sandbox_current = await _get_user_count(redis, user_id, "sandbox", None, period_key)
if current + sandbox_current >= default_limit:
    raise QuotaExceededError("agentic", default_limit, current + sandbox_current, period)
```

`_get_user_count` (`quota.py:472`) already takes an arbitrary `usage_type`
string and `period_key`, so reusing agentic's own `period_key` for the
sandbox lookup (rather than re-resolving sandbox's own independently
configured period, per FR-011/FR-017) requires no new helper — just one
extra call with `"sandbox"` substituted for `"agentic"`. No change to the
platform-wide/XTM-ceiling blocks earlier in the function: FR-015 only
extends the *per-user* branch, not the platform-wide hard cap, which spec
FR-009 already keeps sandbox-independent via the separate
`sandbox_quota_usage` counter.

### 11.3 The new sandbox-dispatch checkpoint — mirroring each function's OWN existing refusal shape (not one uniform shape)

All three `execute_*_opensandbox` functions
(`code_interpreter.py:483`, `custom_tool_executor.py:1175`,
`custom_tool_executor.py:1306`) already call `check_sandbox_quota()`
(the platform-wide cap from spec 001) immediately after the OpenSandbox
reachability probe, and already catch its `SandboxQuotaExceededError` —
but **not identically**, a detail an earlier draft of this research
incorrectly generalized:

- `execute_python_opensandbox` (`code_interpreter.py:548-556`) **returns**
  a dict: `{"stdout": "", "stderr": "", "error": sandbox_quota_message(exc),
  "images": [], "output_files": []}` — no exception propagates.
- `execute_js_tool_opensandbox` (`custom_tool_executor.py:1219-1222`) and
  `execute_python_tool_opensandbox` (`custom_tool_executor.py:1350-1353`)
  both **raise** `RuntimeError(sandbox_quota_message(exc)) from exc` — they
  return `str`, not `dict`, so raising is their only option for signaling
  failure. This is still "soft" in the sense FR-018 cares about: the
  generic `CallableToolExecutor.execute_tool` catch-all
  (`app/tools/base.py:880-882`, `except Exception as exc: ... return
  ToolResult(..., content=str(exc), is_error=True)`) turns any exception
  from a registered tool handler — including this `RuntimeError` — into a
  graceful, LLM-visible tool error, never an unhandled crash or HTTP 500.

FR-018's "soft refusal, not a hard error" therefore means: **mirror
whichever of these two shapes each function already uses for
`SandboxQuotaExceededError`, applied identically to the new
`QuotaExceededError`** — not a new, uniform shape across all three. The
new per-user combined check is added as a second guard immediately after
each function's existing `check_sandbox_quota()` try/except (and before
`record_sandbox_call()` — a call refused by the new check never actually
runs, so it should not increment the platform-wide sandbox call counter
either), only when `user_id` is not `None` (mirroring spec's existing "a
sandbox call cannot be attributed to a specific user" edge case — system-
initiated calls with no user skip this new check entirely, same as they
already skip `increment_sandbox_usage`):

```python
# execute_python_opensandbox — returns, does not raise
if user_id is not None:
    from app.services.quota import QuotaExceededError, check_agentic_quota

    try:
        await check_agentic_quota(<dedicated session>, user_id)
    except QuotaExceededError as exc:
        return {"stdout": "", "stderr": "", "error": <message>, "images": [], "output_files": []}
    except Exception:
        # unreachable backend — see §11.4 for which fail-mode setting governs this
        ...

# execute_js_tool_opensandbox / execute_python_tool_opensandbox — raises, matching
# their existing SandboxQuotaExceededError handling one line above this block
if user_id is not None:
    from app.services.quota import QuotaExceededError, check_agentic_quota

    try:
        await check_agentic_quota(<dedicated session>, user_id)
    except QuotaExceededError as exc:
        raise RuntimeError(<message>) from exc
    except Exception:
        ...
```

The exact contract — which message constant, and how the unreachable-backend
branch handles both shapes — is pinned down precisely in
`contracts/sandbox-usage-quota-contracts.md` rather than duplicated here: a
new fixed `QUOTA_EXCEEDED_MESSAGE` constant added to `quota.py` (sibling to
the existing `QUOTA_UNAVAILABLE_MESSAGE`), not a dynamically-built message
like `sandbox_quota_message`'s (`check_agentic_quota`'s `QuotaExceededError`
carries the same structured fields, but its existing callers all surface a
fixed string, not one built from `exc.current`/`exc.limit`/`exc.period` —
matching that convention here).

### 11.4 Concurrency hazard — reuse the `increment_sandbox_usage` "own session" fix, not the caller's shared session

The "Related fix — concurrent DB session reuse in `increment_sandbox_usage`"
note earlier in this file (end of §10.4) already documents the exact hazard this new check reintroduces:
`agent_loop.py` runs a batch of tool calls concurrently via
`asyncio.gather`, all sharing one request-scoped `AsyncSession`. That fix
resolved it for `increment_sandbox_usage` by opening a dedicated,
independent session (`async with async_session() as own_db:`) rather than
touching the caller's shared `db`. `check_agentic_quota` → `resolve_quota_config`
also executes against whatever `AsyncSession` it's given
(`quota.py:348`, on a cache miss), so the new pre-dispatch call **must**
follow the same pattern — open its own dedicated session, not reuse the
`db` parameter already threaded to these three functions for
`increment_sandbox_usage`'s sake. Passing the caller's shared session here
would reintroduce the exact "concurrent operations are not permitted"
class of bug that fix already exists to prevent (Constitution Principle II).

### 11.5 Fail-mode — reuse `quota_fail_closed()`, not `sandbox_quota_fail_mode`

Per FR-019, the new checkpoint's unreachable-backend handling must reuse
`quota.py`'s existing `quota_fail_closed()` (reads `settings.quota_fail_mode`)
— the same one `agent_chat.py`/`assignment_engine.py` already call — not
`sandbox_quota.py`'s separate `sandbox_quota_fail_mode`. This is a plain
`except Exception:` branch around the new `check_agentic_quota` call
(§11.3), mirroring `agent_chat.py:554-579`'s existing shape: log at ERROR,
then branch on `quota_fail_closed()` to either return the same soft refusal
as a quota-exceeded breach (closed) or let the sandbox call proceed
unmetered for this check (open, the default) — `check_sandbox_quota()`'s
own, separate fail-mode handling directly above is completely unaffected
either way.

### 11.6 Alternatives considered

- *Add a brand-new "combined" quota type with its own configurable limit*
  — rejected per FR-016; would need a new `QuotaConfig` field, a new
  admin-UI surface, and a new precedence-resolution path for something the
  spec explicitly says should reuse the existing agentic limit.
- *Require agentic's and sandbox's resolved periods to match before
  combining, skipping the sandbox term otherwise* — rejected per FR-017;
  the combined check always uses agentic's resolved period as the window
  for both terms, so there's no "mismatch" case to special-case at
  enforcement time (unlike the *display* merge in §10.4, which does need a
  period-match guard, because it sums two *independently* resolved periods
  rather than looking one up under the other's window).
- *Raise `HTTPException(429)` from the new sandbox-dispatch checkpoint,
  matching extension.py/platform.py's pattern* — rejected per FR-018; the
  three `execute_*_opensandbox` functions are called from contexts that
  aren't all HTTP request handlers (e.g. an agent's tool-call loop), so a
  raised HTTP-shaped exception would either be an ill-fitting error type
  for a non-HTTP caller or require every caller up the stack to specifically
  catch it — each function's own existing `SandboxQuotaExceededError`
  convention (§11.3 — a returned dict for the code interpreter, a
  `RuntimeError` caught generically by `CallableToolExecutor` for the two
  custom-tool functions) has none of that problem, since every caller
  already knows how to handle whichever shape that function already uses.
- *Make all three functions return the same shape for this new check
  (e.g. always a dict, or always raise), rather than mirroring each
  function's own existing convention* — rejected; the two custom-tool
  functions are typed to return `str`, not `dict`, so a dict return would
  be a type change forcing every caller of those two functions to be
  updated to handle a new return shape, for no benefit — mirroring what
  each function already does for the platform-wide check is strictly
  smaller and consistent with how that check already behaves.
