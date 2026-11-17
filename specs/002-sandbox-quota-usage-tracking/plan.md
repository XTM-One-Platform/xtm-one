# Implementation Plan: Sandbox Usage Quota Tracking

**Branch**: `002-sandbox-quota-usage-tracking` | **Date**: 2026-07-31 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/002-sandbox-quota-usage-tracking/spec.md`

**Note**: This template is filled in by the `/speckit-plan` command; its definition describes the execution workflow.

## Summary

Record per-user sandbox usage in the existing `quota_usage` ledger
(`usage_type="sandbox"`, alongside the existing `llm_proxy`/`agentic`
values) for every successful OpenSandbox-routed execution — code
interpreter, custom-tool JS, custom-tool Python — on by default, with a new
restart-required config flag (`disable_sandbox_quota_usage`) letting
operators turn it off. No new database table or migration: `usage_type` is
already a free-text column. The main technical work is threading new
optional `user_id`/`db` parameters down to the three `execute_*_opensandbox`
functions (none of which currently receive user context — see
research.md §3, a correction to the source issue's original proposal) and
adding a new best-effort, never-raising `increment_sandbox_usage` function
to `app/services/quota.py`.

**Updated 2026-07-31** (spec FR-011, added by a `/speckit-clarify` session
run after this plan's original version had already been implemented): the
sandbox usage period must resolve with the same user > group > platform >
monthly-default precedence LLM-proxy/agentic usage already gets, instead of
always being fixed at monthly. This adds two things beyond the original
scope: a `db` parameter threaded alongside `user_id` through the same call
chain (no new plumbing problem — `db` is already available everywhere
`user_id` is), and a new `sandbox` field on the `QuotaConfig` API schema so
the platform-wide tier of that precedence is actually reachable through the
existing admin endpoint (research.md §2).

**Updated again 2026-07-31** (spec FR-012, added by a further
`/speckit-clarify` session, again after the prior version was already
implemented): sandbox usage must also appear in the existing Prometheus
`/metrics` endpoint, unconditionally across platform modes. This is a
one-line addition to that endpoint's `usage_types` list — `_read_quota_total`
and the `QUOTA_GAUGE` gauge are both already fully generic per `usage_type`
and need no change (research.md §9). Narrower in scope than the still-deferred
per-user `get_user_quota_usage` display (research.md §7).

**Updated a third time 2026-07-31** (spec FR-013/FR-014, added by yet
another `/speckit-clarify` session, fully reversing §7's remaining
deferral): sandbox usage must now be visibly displayed on the profile, user
detail, group detail, and platform quota settings pages — the first
frontend work in this feature. This turned out to touch three backend read
functions (`get_user_quota_usage`, `resolve_full_quota_info`,
`get_platform_quota_monitoring` — not one, discovered by tracing each named
page to its actual data source) and four frontend files, with a real
correctness requirement (FR-014): since sandbox has no enforceable limit,
every display must show a plain usage count without a percentage bar
implying an enforced cap that doesn't exist — a nuance the first design
pass under-specified before a second look caught it (research.md §10.2).

**Updated a fourth time 2026-08-03** (spec FR-015–FR-019, added by a fifth
`/speckit-clarify` session, reversing the *enforcement* half of FR-014's
"display-only" framing — the display merge itself, FR-013/FR-014, is
untouched): agentic's own enforced per-user limit must now be checked
against the sum of agentic + sandbox usage, not agentic usage alone,
sharing the existing agentic limit as a combined ceiling (no new limit
type). This is a single-choke-point change — `check_agentic_quota` itself
sums both counters under agentic's resolved period before comparing to the
limit — which automatically covers all **four** existing callers
(`agent_chat.py`, `assignment_engine.py`, `extension.py`, `platform.py` —
not just the two the clarify session's Q&A named illustratively, discovered
by a repo-wide search for `check_agentic_quota` callers, research.md
§11.1) with no changes to any of their existing refusal-handling code.
The one genuinely new code path is a pre-dispatch check added to the three
`execute_*_opensandbox` functions, mirroring each function's own existing
refusal convention for the platform-wide cap (a returned dict for the code
interpreter; a raised `RuntimeError`, caught generically by the tool
executor, for the two custom-tool functions — not one shape shared by all
three, research.md §11.3) and — because those functions can run
concurrently within one agent turn via `asyncio.gather` — the same
dedicated-session fix `increment_sandbox_usage` already needed for the
identical hazard (research.md §11.4).

**Updated a fifth time 2026-08-03** (spec FR-011, revised by a sixth
`/speckit-clarify` session): the independently-resolved sandbox period
added by the first FR-011 update above turned out to have no remaining
purpose — the combined check added by the FR-015–FR-019 update just above
always reads sandbox usage under *agentic's* period regardless (FR-017),
and per-user/group sandbox period configuration was never exposed in any
UI. Worse, resolving it independently meant `increment_sandbox_usage`
(write) and `check_agentic_quota` (read) could land on different Redis
keys whenever an admin configured the two periods differently — a real
bug, silently undercounting sandbox usage against the shared ceiling. This
update removes the concept entirely: sandbox always uses agentic's
resolved period for per-user enforcement and display, `resolve_quota_config`
no longer recognizes `"sandbox"` as a quota type, and the `QuotaConfig.sandbox`
field added by the first FR-011 update is removed (it had nothing left to
carry once `.period` was gone — `.platform_limit`/`.default`/`.per_model`
were already inert). The platform-wide Prometheus/monitoring row (FR-012)
keeps a fixed monthly bucket instead — it has no single "the" agentic
period to inherit, since XTM One mode scans agentic under up to three
periods (daily/monthly/yearly) simultaneously.

## Technical Context

**Language/Version**: Python 3.13 (`backend/pyproject.toml` targets `py313`)

**Primary Dependencies**: FastAPI, SQLAlchemy (async) — no new dependency;
reuses the existing `redis.asyncio` client and `app/services/quota.py`
Redis-fast/PostgreSQL-durable counter machinery. The `opensandbox` SDK is
already a dependency (unchanged by this feature — no new SDK calls, only
new parameters on existing wrapper functions).

**Storage**: Existing `quota_usage` PostgreSQL table (no schema change) +
existing Redis counters, via the existing `flush_quota_usage()` job — no
new table, no new migration. `User.quota_config`/`Group.quota_config`
(untyped JSON columns) and `PlatformSettings.quota_config` (also untyped
JSON) need no schema change either — a `"sandbox"` key fits without a
migration; only the `QuotaConfig` Pydantic *API* schema needs a new field
(research.md §2).

**Testing**: pytest (`asyncio_mode = "auto"`), `unittest.mock.AsyncMock` for
Redis and OpenSandbox SDK mocking, extending
`tests/test_opensandbox_sandbox_mode.py` (already covers all three
`execute_*_opensandbox` functions — not `test_code_interpreter_sandbox.py`,
which is module-blocklist security tests, or `test_custom_tool_sandbox_mode.py`,
which is `auto`/`docker`/`subprocess` mode *selection*, neither of which
touches opensandbox execution semantics), `tests/test_sandbox_usage_quota.py`
for `increment_sandbox_usage` itself and (corrected during implementation —
`test_quota_usage_display.py` only tests `get_user_quota_usage` consuming a
*mocked-away* `resolve_quota_config`; no file anywhere exercises
`resolve_quota_config`'s own precedence logic directly, confirmed by repo
search) also for `resolve_quota_config`'s new `"sandbox"` precedence cases —
**both since removed 2026-08-03 (FR-011 revised)**: sandbox no longer
resolves any config of its own, so `test_sandbox_usage_quota.py` now
confirms `increment_sandbox_usage` uses *agentic's* period and that
`resolve_quota_config(..., "sandbox")` always returns `None`, and
`test_quota_config_schema.py` now confirms `QuotaConfig` has no `sandbox`
field at all rather than testing one that round-trips — plus a new
`tests/test_quota_config_schema.py` (no existing test covers
`QuotaConfig`/`PUT /quotas` directly), and (2026-07-31, FR-012) a new
`tests/test_prometheus_metrics.py` (no existing test covers
`app/api/v1/metrics.py`'s `usage_types`/`_read_quota_total` logic at all) —
matching this codebase's convention of testing per module under test.
(2026-07-31, FR-013/FR-014): also extend `tests/test_quota_usage_display.py`
(`get_user_quota_usage`'s new `"sandbox"` entries) and add coverage for
`resolve_full_quota_info`'s and `get_platform_quota_monitoring`'s new
`"sandbox"`-handling, mirroring the `resolve_quota_config` precedence-test
approach in `test_sandbox_usage_quota.py`. (2026-08-03, FR-015–FR-019): a
new `tests/test_agentic_sandbox_combined_quota.py` for `check_agentic_quota`'s
combined-usage limit math (no existing file unit-tests that comparison in
isolation, confirmed by repo search); further extend
`tests/test_opensandbox_sandbox_mode.py` for the new pre-dispatch check at
all three call sites (success, refusal, unreachable-backend, `user_id=None`
skip, and a concurrency regression test for the dedicated-session fix); and
extend `tests/test_fail_modes.py` with a case proving the new checkpoint
reads `QUOTA_FAIL_MODE`. Frontend: this codebase's
Constitution Principle III requires TypeScript type-checking/lint to pass
(`tsc --noEmit`, `eslint`) for frontend changes but does not mandate new
frontend unit tests the way it mandates backend tests — confirmed by repo
search that no `.test.tsx`/`.spec.tsx` files exist anywhere in
`frontend/src` (a component-testing convention doesn't exist in this
codebase at all), and the existing Playwright e2e suite
(`frontend/tests_e2e/`) has no spec covering these four pages' quota cards
either. Frontend verification for this feature therefore relies on
type-checking/lint passing plus manual verification per quickstart.md
Scenario 8, not new automated frontend tests — consistent with, not a
lowering of, this codebase's actual existing frontend-testing bar.

**Target Platform**: Linux server (existing FastAPI backend, Docker/Kubernetes replicas)

**Project Type**: Web application (backend + frontend). **Updated
2026-07-31 (FR-013/FR-014)**: originally backend-only through FR-012 (User
Story 3's reporting-display follow-up was explicitly deferred, research.md
§7); FR-013/FR-014 fully reversed that deferral, adding this plan's first
frontend work — React/TypeScript changes to `frontend/src/pages/Profile.tsx`,
`UserDetail.tsx`, `GroupDetail.tsx`, and `settings/QuotasSection.tsx`
(research.md §10.2).

**Performance Goals**: Negligible added latency — one additional Redis
`INCR` on the success path of an already-multi-second sandbox-container
call, matching the existing `quota.py` enforcement path's overhead

**Constraints**: On by default, no configuration required (spec FR-005);
config flag is restart-required, no runtime hot-reload (spec FR-008,
`/speckit-clarify` Q2); usage recording must never fail an already-completed
sandbox call (spec FR-010, `/speckit-clarify` Q1); scoped to
OpenSandbox-routed executions only, matching the existing platform-wide
`sandbox_quota_usage` counter's scope (spec Assumptions); must not alter the
existing platform-wide sandbox call limits in any way (spec FR-009); period
resolution for per-user enforcement/display always uses agentic's own
resolved period (spec FR-011, revised 2026-08-03 — sandbox has no
configurable period of its own) without adding any new
enforceable sandbox quota tier (spec Assumptions still hold — usage
recording only); sandbox usage totals must appear in the existing
Prometheus `/metrics` endpoint unconditionally across platform modes (spec
FR-012), under a fixed monthly bucket for that platform-wide row, without
gating logic tied to platform mode; **(2026-08-03,
FR-015–FR-019)** agentic's own per-user enforced limit must additionally
treat sandbox usage as part of the same ceiling (sum of both, compared to
the existing agentic limit — no new limit field, spec FR-016), computed
under agentic's own resolved period, which is also the only period
sandbox usage is ever recorded under (spec FR-017); the new sandbox-dispatch
checkpoint this requires must refuse via whichever convention each of the
three call sites already uses — a returned dict
for the code interpreter, a raised `RuntimeError` for the two custom-tool
functions, not one shape shared by all three (spec FR-018), must run on a
dedicated DB session to avoid the
concurrent-session hazard `increment_sandbox_usage` already solved
(Constitution Principle II), and must honor `QUOTA_FAIL_MODE`
when the backend is unreachable (spec FR-019). Spec FR-009's separate,
platform-wide `sandbox_quota_usage` counter is superseded by spec
`004-remove-sandbox-call-quota`, which removes it entirely as redundant
with this combined check

**Scale/Scope**: Three existing call sites to instrument
(`code_interpreter.py` ×1, `custom_tool_executor.py` ×2, plus the
JS-only legacy dispatcher `execute_js_tool_auto` fixed for symmetry during
code review), each requiring two new optional parameters (`user_id`, `db`)
threaded through one intermediate dispatcher function and from one outer
caller; one new function in `quota.py`; one existing function
(`resolve_quota_config`) briefly extended to recognize a third quota type,
then reverted 2026-08-03 (FR-011 revised) once that type's only field
(`period`) turned out to have no remaining purpose; one new
`Settings` field; one new field on an existing API schema (`QuotaConfig`),
also since removed for the same reason;
one existing list (`usage_types` in `app/api/v1/metrics.py`) extended with
one new unconditional entry; no new table, no new endpoint. **Updated
2026-07-31 (FR-013/FR-014)**: two more existing backend functions extended
(`resolve_full_quota_info`, `get_platform_quota_monitoring`); four existing
frontend files changed (`Profile.tsx`, `UserDetail.tsx`, `GroupDetail.tsx`,
`settings/QuotasSection.tsx`) — the first frontend change in this feature.
Still no new table, no new endpoint, no new frontend component (existing
pages gain an additional array entry / card each). **Updated 2026-08-03
(FR-015–FR-019)**: one existing function's internal comparison changed
(`check_agentic_quota` — two lines, one added sandbox-count read plus the
updated comparison, applied identically in both its XTM One and Copilot
branches); zero changes to its four existing callers
(`agent_chat.py`, `assignment_engine.py`, `extension.py`, `platform.py`);
one new pre-dispatch check added to each of the same three
`execute_*_opensandbox` functions already touched by this feature (each
mirroring its own existing refusal shape for the platform-wide cap — dict
return or raised `RuntimeError` — plus a new dedicated-session
open, mirroring `increment_sandbox_usage`'s existing one). No new table, no
new endpoint, no new frontend change, no new `Settings` field.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Evaluated against `.specify/memory/constitution.md` v1.0.0.

- [x] **Fail-Closed by Default** (Principle I): satisfied by design, in the
      *opposite* direction the principle's literal examples describe — and
      that divergence is itself the correct call here. Principle I targets
      pre-flight verification *checks* that gate whether an action is
      allowed to happen at all (quota/rate-limit enforcement, migration
      state, credential/signature verification) — for those, an
      unconfident check must refuse, not silently permit. This feature adds
      no such check: it is a post-hoc usage *recording* step for a sandbox
      call that has already completed. `/speckit-clarify` (2026-07-31, Q1)
      explicitly resolved this as fail-open for recording only, matching
      the codebase's own existing precedent for the same class of action
      (`increment_llm_proxy_usage`'s caller, `_track_model_usage` in
      `app/api/v1/llm_proxy.py:190-217`, already treats a usage-counter
      increment as best-effort and non-blocking). At the time this was
      written, the *existing* sandbox quota check (`check_sandbox_quota`,
      from the sibling `001-sandbox-call-quotas` feature) already
      implemented genuine fail-closed-capable enforcement via
      `SANDBOX_QUOTA_FAIL_MODE`; this feature deliberately did not
      duplicate or alter that gate (FR-009 — since superseded by spec
      `004-remove-sandbox-call-quota`, which removes that mechanism
      entirely as redundant with this feature's own combined check below).
      **Updated 2026-08-03 (FR-015–FR-019)**: unlike the recording step
      above, the new sandbox-dispatch checkpoint *is* exactly the kind of
      pre-flight gate Principle I's literal examples describe — it decides
      whether a sandbox call is allowed to proceed at all. It satisfies the
      principle directly, not by exemption: an unreachable quota backend is
      handled via `quota_fail_closed()` (`QUOTA_FAIL_MODE`), the same
      administrator-selectable fail-open/fail-closed contract
      `check_agentic_quota`'s existing callers already use, not a new or
      silently-permissive default.
- [x] **Concurrency-Safe, Multi-Replica by Design** (Principle II):
      satisfied — `increment_sandbox_usage` reuses the existing atomic
      Redis `INCR` (`_increment` in `quota.py:456`) and the existing
      `flush_quota_usage()` advisory-lock-guarded flush job; no new
      scheduled job or shared-counter mechanism is introduced. **Updated
      2026-08-03 (FR-015–FR-019)**: the new pre-dispatch check re-triggers
      the identical concurrent-session hazard `increment_sandbox_usage`
      already solved (`agent_loop.py`'s parallel tool-call batch via
      `asyncio.gather`, all sharing one request-scoped `AsyncSession`) —
      resolved the same way: a dedicated `async with async_session() as
      own_db:` inside the check, never the caller's shared `db`
      (research.md §11.4). `check_agentic_quota`'s own Redis reads/writes
      are already atomic `INCR`/`GET`; no new counter mechanism.
- [x] **CI-Enforced Quality Gates** (Principle III): satisfied — quickstart.md
      specifies unit-test coverage (mocked Redis/OpenSandbox) for
      `increment_sandbox_usage` and all three call sites, covering success,
      each failure path, the config flag, and the never-raises contract;
      extended (2026-07-31) to also cover `resolve_quota_config`'s new
      `"sandbox"` recognition at each precedence tier and the `QuotaConfig`
      schema's new field round-tripping through `PUT /quotas` and both
      response-construction sites; further extended (2026-07-31, FR-012)
      with new coverage confirming `"sandbox"` appears unconditionally in
      `/metrics`'s `usage_types` list across platform modes; further
      extended (2026-07-31, FR-013/FR-014) with backend coverage for
      `resolve_full_quota_info`/`get_platform_quota_monitoring`'s new
      `"sandbox"` handling. Frontend changes are covered by this
      codebase's actual CI bar for `frontend/` — `tsc --noEmit` and
      `eslint` passing — not new automated tests, since no component-test
      or e2e convention exists for these four pages (confirmed by repo
      search, see Technical Context Testing). No migration is added, so no
      migration CI job interaction. **Updated 2026-08-03 (FR-015–FR-019)**:
      further extended with a new `test_agentic_sandbox_combined_quota.py`
      for `check_agentic_quota`'s combined-limit math, further coverage in
      `test_opensandbox_sandbox_mode.py` for the new pre-dispatch check
      (including a concurrency regression test for the dedicated-session
      fix), and a new case in `test_fail_modes.py` proving the checkpoint
      reads `QUOTA_FAIL_MODE` (quickstart.md
      Scenario 9). Still no migration, no frontend change.
- [x] **Rationale-Documented Complexity** (Principle IV): satisfied in
      design intent — the deliberate divergence from
      `increment_llm_proxy_usage`/`increment_agentic_usage`'s
      let-it-raise contract (research.md §5), the choice to instrument
      inside the three `execute_*_opensandbox` functions rather than their
      simpler-looking outer callers (research.md §3), and (2026-07-31) why
      `QuotaConfig.sandbox`'s `platform_limit`/`default`/`per_model` fields
      are inert while `period` is not (research.md §2), and (2026-07-31,
      FR-013/FR-014) why the four frontend cards must force "no limit"
      behavior unconditionally for `usage_type="sandbox"` rather than
      trusting the inert config fields to stay unset (research.md §10.2/
      §10.3) are all exactly the kind of non-obvious "why" this principle
      asks to be documented in code, not just in this plan — tasks.md
      should carry this forward as an explicit reminder for the
      implementer, on both the backend and frontend sides this time.
      **Updated 2026-08-03 (FR-015–FR-019)**: one more non-obvious "why"
      for tasks.md to carry into code comments — why the new pre-dispatch
      check opens its own dedicated session instead of reusing the
      caller's `db` (the concurrent-session hazard, research.md §11.4,
      not self-evident without that context).
      **Updated again 2026-08-03 (FR-011, revised)**: the prior note about
      `QuotaConfig.sandbox`'s fields and "combined check reads sandbox
      usage under agentic's period rather than sandbox's own
      independently-resolved one" is now moot — that field and that
      independent period were both removed as having no remaining purpose
      (see the fifth Technical Context update above); the code comment this
      principle asks for is now the removal's own rationale, not a
      divergence to explain.
- [x] **Backward-Compatible, Auditable Evolution** (Principle V): satisfied
      trivially — no schema or state change ships with this feature (no
      migration), so there is no unrecognized-prior-state concern to
      document. **Updated 2026-08-03 (FR-015–FR-019)**: still no schema or
      migration change — this revision only changes in-memory comparison
      logic inside existing functions.

No violations; no Complexity Tracking entries required.

## Project Structure

### Documentation (this feature)

```text
specs/002-sandbox-quota-usage-tracking/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md        # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
├── checklists/
│   └── requirements.md  # Spec quality checklist (/speckit-specify, updated by /speckit-clarify)
└── tasks.md              # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

### Source Code (repository root)

`backend/` is a Python FastAPI service; `frontend/` is a React/TypeScript
SPA. **Updated 2026-07-31 (FR-013/FR-014)**: this feature now touches both
— the first frontend change in this plan (see the new frontend tree below).

```text
backend/
├── app/
│   ├── core/
│   │   └── config.py                    # Settings: add disable_sandbox_quota_usage: bool = False
│   ├── services/
│   │   ├── quota.py                     # NEW: increment_sandbox_usage(); NEW: _QUOTA_TYPES constant
│   │   │                                # ("llm_proxy", "agentic" only — "sandbox" was added here
│   │   │                                # 2026-07-31, then removed again 2026-08-03, FR-011 revised:
│   │   │                                # resolve_quota_config()/resolve_full_quota_info() no longer
│   │   │                                # recognize "sandbox" as a quota type at all); extend
│   │   │                                # get_user_quota_usage() to still emit a "sandbox" usage row,
│   │   │                                # reusing agentic's resolved period from the same loop;
│   │   │                                # get_platform_quota_monitoring(): append one
│   │   │                                # unconditional _scan_period("sandbox", "monthly", None,
│   │   │                                # is_global=True) row, always a fixed monthly bucket
│   │   │                                # (2026-07-31, FR-013/FR-014; period fixed 2026-08-03, FR-011);
│   │   │                                # MODIFY check_agentic_quota(): sum the "sandbox" counter
│   │   │                                # (via _get_user_count) into the per-user comparison, under
│   │   │                                # the agentic entry's own period_key, in both the XTM One
│   │   │                                # and Copilot branches — no change to its four callers or
│   │   │                                # to the platform-wide/XTM-ceiling blocks (2026-08-03,
│   │   │                                # FR-015–FR-017, research.md §11.2)
│   │   ├── code_interpreter.py          # add user_id/db params to execute_python / execute_python_opensandbox;
│   │   │                                # call increment_sandbox_usage() on the success path;
│   │   │                                # execute_python_opensandbox: NEW pre-dispatch combined-quota
│   │   │                                # check at the top of the function, only when a user to
│   │   │                                # attribute quota to is resolvable, on a dedicated session
│   │   │                                # (2026-08-03, FR-015/FR-018/FR-019, research.md §11.3/§11.4)
│   │   ├── custom_tool_executor.py      # add user_id/db params to execute_tool_auto / execute_js_tool_auto /
│   │   │                                # execute_js_tool_opensandbox / execute_python_tool_opensandbox;
│   │   │                                # call increment_sandbox_usage() on the success path;
│   │   │                                # execute_js_tool_opensandbox + execute_python_tool_opensandbox:
│   │   │                                # same NEW pre-dispatch check as code_interpreter.py above
│   │   │                                # (2026-08-03, FR-015/FR-018/FR-019)
│   │   └── builtin_tools/
│   │       ├── media.py                 # build_code_interpreter_tool._execute_code: pass user_id/db through
│   │       └── loaders.py               # load_custom_tools._run_custom: pass user_id/db through
│   └── api/
│       └── v1/
│           ├── custom_tools.py          # "test this tool" handler: pass user.id/db through
│           ├── metrics.py               # add "sandbox" to usage_types, unconditionally (outside the
│           │                            # `if not settings.is_xtm_one:` guard) — one-line change;
│           │                            # _read_quota_total/QUOTA_GAUGE already fully generic
│           └── schemas/
│               └── settings.py          # QuotaConfig.sandbox: QuotaTypeConfig | None = None, added
│                                         # 2026-07-31, removed again 2026-08-03 (FR-011 revised) — its
│                                         # only effect, .period, no longer exists
└── tests/
    ├── test_sandbox_usage_quota.py      # NEW: increment_sandbox_usage unit tests (resolves agentic's
    │                                    # period, not one of its own, per FR-011 revised) AND confirms
    │                                    # resolve_quota_config(..., "sandbox") always returns None — no
    │                                    # existing file exercises resolve_quota_config directly (confirmed
    │                                    # by repo search; test_quota_usage_display.py only tests
    │                                    # get_user_quota_usage consuming a mocked-away resolve_quota_config)
    ├── test_opensandbox_sandbox_mode.py # extend: covers all 3 execute_*_opensandbox functions already —
    │                                    # add success/failure/None-user_id coverage here; further extend
    │                                    # (2026-08-03, FR-015/FR-018) for the new pre-dispatch check:
    │                                    # each function's own existing refusal shape (dict / raised
    │                                    # RuntimeError) on QuotaExceededError, skipped when
    │                                    # user_id=None, and a concurrency regression test for the
    │                                    # dedicated-session fix (research.md §11.4)
    ├── test_quota_config_schema.py      # NEW: no existing test file covers QuotaConfig / PUT /quotas
    │                                    # directly (confirmed by repo search) — add sandbox field
    │                                    # round-trip coverage here
    ├── test_prometheus_metrics.py       # NEW: no existing test file covers metrics.py's usage_types /
    │                                    # _read_quota_total logic at all (confirmed by repo search) —
    │                                    # add "sandbox" unconditional-inclusion coverage here
    ├── test_quota_usage_display.py      # extend (2026-07-31, FR-013/FR-014): get_user_quota_usage's
    │                                    # new "sandbox" entries; add coverage for
    │                                    # resolve_full_quota_info / get_platform_quota_monitoring's
    │                                    # new "sandbox" handling here too (or a new file, at
    │                                    # implementer's discretion — no strong existing precedent
    │                                    # for either function's own test home, confirmed by repo search)
    ├── test_agentic_sandbox_combined_quota.py  # NEW (2026-08-03, FR-015–FR-019): check_agentic_quota's
    │                                    # combined-usage limit math — no existing file unit-tests that
    │                                    # comparison in isolation (confirmed by repo search)
    └── test_fail_modes.py               # extend (2026-08-03, FR-019): new sandbox-dispatch checkpoint
                                          # reads QUOTA_FAIL_MODE when the backend is unreachable

docs/
└── docs/deployment/configuration.md     # add disable_sandbox_quota_usage to the existing
                                          # "Security feature flags" table
```

### Source Code — Frontend (2026-07-31, FR-013/FR-014)

```text
frontend/
└── src/
    └── pages/
        ├── Profile.tsx                  # add "sandbox" to the usage-types array, unconditionally;
        │                                # force "always show count" + "hasLimit=false" for this key
        ├── UserDetail.tsx               # same two forced overrides as Profile.tsx
        ├── GroupDetail.tsx              # add "sandbox" to QUOTA_TYPES, unconditionally; read-only
        │                                # period display only (no QuotaEditor input — out of scope,
        │                                # research.md §10.2/§10.3)
        └── settings/
            └── QuotasSection.tsx        # add a `sandbox` entry to QUOTA_TYPE_META (new icon/color);
                                          # QuotaProgressCard/RadialGauge need no change — both already
                                          # render a limit-less summary row correctly (verified by
                                          # inspection, research.md §10.2)
```

**Structure Decision**: No new top-level directories, services, or frontend
components. This is a narrow extension of the existing `quota.py`
usage-recording pattern (already used for `llm_proxy`/`agentic`) plus
parameters threaded through two existing sandbox-execution modules and
their three outer call sites, plus (2026-07-31, FR-011; reverted 2026-08-03,
FR-011 revised) a third quota type briefly recognized by the existing
config-resolution function and API schema, plus
(2026-07-31, FR-012) a one-line addition to an existing Prometheus-metrics
list, plus (2026-07-31, FR-013/FR-014) the same third quota type recognized
by two more existing backend read functions and added to four existing
frontend pages' hardcoded usage-type lists — each page gains one additional
array entry and reuses its existing card/row component with two forced
behavioral overrides (never hide the count, never show a limit), not a new
component, plus (2026-08-03, FR-015–FR-019) a two-line change inside one
existing function (`check_agentic_quota`) that automatically covers all
four of its existing callers, plus one new pre-dispatch check reusing an
existing return convention at the same three call sites already touched by
this feature. No new API endpoint, no new database migration, no new
frontend change.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

No violations — table intentionally omitted.
