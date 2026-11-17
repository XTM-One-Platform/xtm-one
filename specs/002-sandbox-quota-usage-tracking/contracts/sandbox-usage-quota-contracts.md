# Phase 1 Contracts: Sandbox Usage Quota Tracking

**Feature**: [spec.md](./../spec.md) | **Data model**: [data-model.md](./../data-model.md)

No new external HTTP endpoint is introduced. The `QuotaConfig` schema
originally gained a `sandbox` field here (spec FR-011, 2026-07-31) but that
field was removed again in the 2026-08-03 revision (see "Schema contract"
below) — the primary contract surface is, and remains, internal: the
function interface the three OpenSandbox call sites (and their callers,
which supply `user_id` and now `db`) depend on.

## Internal usage-recording contract — `app/services/quota.py`

```python
async def increment_sandbox_usage(
    user_id: uuid.UUID,
    period: str = "monthly",
    db: AsyncSession | None = None,
) -> None:
    """Best-effort increment of the per-user sandbox usage counter.

    Writes into the existing `quota_usage` table/Redis-counter pattern with
    usage_type="sandbox" (no new table). Unlike increment_agentic_usage,
    this function:
      - never raises: internally checks `settings.disable_sandbox_quota_usage`
        (no-op if set) and swallows any Redis error, logging at debug level
        — a failure to record usage must never fail an already-completed
        sandbox call (spec FR-010 / clarify Q1).
      - maintains no global/platform-wide companion counter and no
        XTM-license ceiling handling — sandbox usage has no enforceable cap,
        unlike agentic usage's `check_agentic_quota`.

    Has no period of its own (spec FR-011, revised 2026-08-03): when `db`
    is provided, resolves and uses the *agentic* quota's period via
    `resolve_quota_config(db, redis, user_id, "agentic")`, falling back to
    "monthly" when nothing resolves. `resolve_quota_config` no longer
    recognizes "sandbox" as a quota type at all (see below) — originally
    (2026-07-31) this resolved its own, independently-configured "sandbox"
    period instead, but that period had no remaining purpose once the
    combined check always reads sandbox usage under agentic's period
    anyway (FR-017), and it made the check's read key and this function's
    write key drift apart whenever an admin configured the two periods
    differently.
    """
```

**Updated 2026-08-03 (spec FR-015–FR-019)**: "sandbox usage has no
enforceable cap" above still means what it always meant — sandbox has no
*limit field of its own* (`QuotaConfig.sandbox.default`/`platform_limit`
remain inert, FR-016) — but is no longer the whole picture. As of this
revision, `check_agentic_quota` itself reads the `"sandbox"` counter this
function writes and sums it into agentic's per-user comparison (see the
new contract below), so sandbox usage now *does* indirectly feed an
enforceable ceiling — agentic's. `increment_sandbox_usage` itself is
unchanged by this: it still only writes the counter, never checks or
raises.

## Internal combined-enforcement contract — `check_agentic_quota` (`app/services/quota.py:611`, revised 2026-08-03)

```python
async def check_agentic_quota(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> None:
    """Check agentic execution quota. Raises `QuotaExceededError` if exceeded.

    Per-user branch (both the XTM One and Copilot code paths) now compares
    `agentic_count + sandbox_count` — both read under the *agentic* entry's
    resolved `period_key` (spec FR-017) — against the existing per-user
    `default_limit` (spec FR-016: no new limit field). Sandbox has no
    period of its own to read instead (FR-011, revised) — this is the only
    period sandbox usage is ever recorded under for per-user purposes. The
    platform-wide / XTM-ceiling blocks earlier in the function are
    unaffected — this only changes the per-user branch.

    Callers are unaffected: all four existing callers (`agent_chat.py`,
    `assignment_engine.py`, `extension.py`, `platform.py`) keep their
    existing `QuotaExceededError` handling unchanged — only the quantity
    being compared to the limit changed, not how a breach is raised or
    surfaced (spec FR-015, research.md §11.1).
    """
```

### New pre-dispatch call at the three `execute_*_opensandbox` functions (spec FR-015/FR-018/FR-019; the platform-wide `check_sandbox_quota()` call this originally sat next to was itself removed by spec `004-remove-sandbox-call-quota`)

At the top of each function, only when a user to attribute quota to is
resolvable. The refusal shape mirrors whichever convention that function
already uses — **not** a single shape shared by all three (an earlier
draft of these docs incorrectly assumed a uniform dict return; corrected
2026-08-03, research.md §11.3):

Reuses two message constants, both fixed strings (not built from the
exception's fields, unlike `sandbox_quota_message`) — `QUOTA_UNAVAILABLE_MESSAGE`
already lives in `quota.py`; add a sibling `QUOTA_EXCEEDED_MESSAGE` constant
there too (same wording as `agent_chat.py`'s existing local constant of the
same name — that one stays as-is; this is a new, separate constant for the
sandbox call sites to import, not a refactor of the existing one):

```python
# execute_python_opensandbox — returns a dict, never raises
if user_id is not None:
    from app.services.quota import (
        QUOTA_EXCEEDED_MESSAGE,
        QUOTA_UNAVAILABLE_MESSAGE,
        QuotaExceededError,
        check_agentic_quota,
        quota_fail_closed,
    )
    from app.core.database import async_session

    try:
        async with async_session() as own_db:  # NOT the caller's shared `db` —
            # avoids the same concurrent-session hazard increment_sandbox_usage
            # already had to solve (research.md §11.4)
            await check_agentic_quota(own_db, user_id)
    except QuotaExceededError:
        return {"stdout": "", "stderr": "", "error": QUOTA_EXCEEDED_MESSAGE, "images": [], "output_files": []}
    except Exception:
        if quota_fail_closed():  # QUOTA_FAIL_MODE (FR-019)
            return {"stdout": "", "stderr": "", "error": QUOTA_UNAVAILABLE_MESSAGE, "images": [], "output_files": []}
        # else: fail open — proceed to dispatch unmetered for this check

# execute_js_tool_opensandbox / execute_python_tool_opensandbox — raise instead
if user_id is not None:
    from app.services.quota import (
        QUOTA_EXCEEDED_MESSAGE,
        QUOTA_UNAVAILABLE_MESSAGE,
        QuotaExceededError,
        check_agentic_quota,
        quota_fail_closed,
    )
    from app.core.database import async_session

    try:
        async with async_session() as own_db:
            await check_agentic_quota(own_db, user_id)
    except QuotaExceededError as exc:
        raise RuntimeError(QUOTA_EXCEEDED_MESSAGE) from exc
    except Exception as exc:
        if quota_fail_closed():
            raise RuntimeError(QUOTA_UNAVAILABLE_MESSAGE) from exc
        # else: fail open
```

### `resolve_quota_config` extension — `app/services/quota.py:325`

Add `"sandbox"` to the three generic resolution loops in this function
(user-level, group-level, global-level — 4 tuple occurrences total within
the function body). Do **not** touch the XTM-license agentic-ceiling block
(`if settings.is_xtm_one: ...`) — that block is agentic-specific and has no
sandbox equivalent (sandbox has no license-derived ceiling).

### Call-site integration (per research.md §3–4; `db` threading added per §2)

Each of the three `execute_*_opensandbox` functions gains `user_id:
uuid.UUID | None = None` and (new) `db: AsyncSession | None = None` keyword
parameters, threaded down from the one outer caller (per function) that
already has both in scope. On the success path only — see research.md §4
for the exact predicate per function — each calls:

```python
if user_id is not None:
    from app.services.quota import increment_sandbox_usage
    await increment_sandbox_usage(user_id, db=db)
```

placed at these exact points:

- `code_interpreter.py::execute_python_opensandbox` — immediately after the
  `if result.exit_code != 0 and not stdout_text:` early-return block, before
  the subsequent JSON-parsing `try`. Both of the function's two remaining
  return paths (parsed JSON, or raw-stdout fallback) are downstream of this
  point and are therefore both counted.
- `custom_tool_executor.py::execute_js_tool_opensandbox` and
  `execute_python_tool_opensandbox` — immediately before each function's
  single, final `return stdout_text or stderr_text or "(no output)"`
  statement (the only normal-exit path; every failure mode already raises
  `RuntimeError` above it).

`user_id`/`db` are threaded one layer further up from each of these call
chains, with no other behavior change to the intermediate dispatcher
functions:

- `code_interpreter.py::execute_python(code, *, timeout, user_id=None,
  db=None)` → passes both through to `execute_python_opensandbox` only when
  the resolved mode is `opensandbox` (the existing dispatch branch at
  `code_interpreter.py:384`); the non-opensandbox branches ignore them.
- `custom_tool_executor.py::execute_tool_auto(function_code, inputs,
  language, timeout, *, use_docker=None, user_id=None, db=None)` → passes
  both through only on the two `opensandbox`-routed dispatch branches; the
  `docker`/`subprocess` branches ignore them.
- `custom_tool_executor.py::execute_js_tool_auto(js_function, inputs,
  timeout, *, use_docker=None, user_id=None, db=None)` — the JS-only legacy
  sibling dispatcher to `execute_tool_auto` (no production caller today,
  confirmed by repo-wide grep, but a public function fixed for symmetry
  during code review) → same threading, opensandbox branch only.

And the outer callers each pass their already-available `user_id` (parsed
to `uuid.UUID` via `app.core.utils.safe_uuid` where the local variable is a
`str`) and `db`:

- `app/services/builtin_tools/media.py::build_code_interpreter_tool`'s
  `_execute_code` closure — passes its own `user_id`/`db` to
  `execute_python(code, timeout=300, user_id=..., db=db)`.
- `app/services/builtin_tools/loaders.py::load_custom_tools`'s
  `_run_custom` closure — passes its own `user_id`/`db` to
  `execute_tool_auto(_code, dict(kwargs), language=_language, timeout=_tout,
  user_id=..., db=db)`.
- `app/api/v1/custom_tools.py`'s "test this tool" handler — passes
  `user.id`/`db` to its existing `execute_tool_auto(...)` call the same way.

No existing caller that does *not* have a `user_id`/`db` in scope needs any
change — both new parameters default to `None`, in which case
`increment_sandbox_usage` is either skipped entirely (`user_id is None`,
matching spec's "cannot be attributed to a specific user" edge case) or
falls back to the `"monthly"` default period (`db is None`).

## Schema contract — `app/api/v1/schemas/settings.py::QuotaConfig`

```python
class QuotaConfig(BaseModel):
    llm_proxy: QuotaTypeConfig | None = None
    agentic: QuotaTypeConfig | None = None
    # No sandbox field.
```

**Revised 2026-08-03 (FR-011)**: a `sandbox: QuotaTypeConfig | None = None`
field was originally added here (2026-07-31) — see data-model.md for why —
purely so its `.period` sub-field would round-trip through `PUT /quotas`
(`app/api/v1/settings.py:4759`, admin-only, platform-wide config) and the
two response-construction sites that reconstruct a `QuotaConfig` from a raw
dict (`app/api/v1/users.py:1395-1396`, `app/api/v1/settings.py:4807`)
instead of being silently dropped. Since that period no longer exists, the
field was removed entirely; a stray `"sandbox"` key is now silently ignored
by `QuotaConfig(**dict)`'s default extra-field handling at all three sites,
same as any other unrecognized key.

## Configuration contract — `app/core/config.py`

```python
disable_sandbox_quota_usage: bool = False
```

Placed alongside the existing `disable_code_interpreter` /
`disable_custom_tools` fields (`config.py:953-954`), matching their naming
convention. Read once at process startup; changing it requires a
restart/redeploy (spec FR-008, `/speckit-clarify` Q2). No new env-var
documentation entry is needed beyond adding it to
`docs/docs/deployment/configuration.md`'s existing "Security feature flags"
table (same table `disable_code_interpreter`/`disable_custom_tools` are
already documented in).

## Metrics contract — `app/api/v1/metrics.py` (spec FR-012, added 2026-07-31)

```python
usage_types = ["agentic", "sandbox"]  # both unconditional
if not settings.is_xtm_one:
    usage_types.append("llm_proxy")
```

The only change to the existing `GET /metrics` endpoint: `"sandbox"` added
to the `usage_types` list, unconditionally (research.md §9 — sandbox usage
has no platform-mode branching, so its exposure here shouldn't either,
unlike `"llm_proxy"`'s mode-gated inclusion). `_read_quota_total` and the
`QUOTA_GAUGE` gauge definition are both already fully generic per
`usage_type` and require no change — this is a one-line addition to an
existing list, not a new metric or a new endpoint.

## Usage-display contracts (spec FR-013/FR-014, added 2026-07-31)

Three existing backend read functions, each extended for `"sandbox"`
(research.md §10.1 — no new endpoints, no schema changes to their response
shapes, since both `QuotaUsageEntry` and the monitoring summary dict are
already generic per `usage_type`/`type`):

```python
# app/services/quota.py

# _QUOTA_TYPES ("llm_proxy", "agentic") is config-resolution scope only —
# revised 2026-08-03 (FR-011): "sandbox" was originally added here
# (2026-07-31) so it could resolve its own period, but that period turned
# out to have no remaining purpose (see the increment_sandbox_usage
# contract above) and was removed. "sandbox" still gets its own *usage*
# row below, just without a config-resolution entry.

async def get_user_quota_usage(db, user_id) -> list[dict]:
    for qt in _QUOTA_TYPES:  # ("llm_proxy", "agentic")
        ...
    # sandbox row appended separately, reusing agentic's period_key from
    # the loop above rather than resolving one of its own

async def resolve_full_quota_info(db, redis, user_id) -> tuple[dict, dict[str, str]]:
    for qt in _QUOTA_TYPES:  # ("llm_proxy", "agentic") — no sandbox entry at all
        ...
        # XTM-license fallback branch stays agentic-only — untouched

async def get_platform_quota_monitoring(db) -> dict:
    # existing `for qt in ("agentic", "llm_proxy"):` branch UNCHANGED —
    # its platform_limit/default-gated emission logic doesn't fit a
    # limit-less type (research.md §10.3). Instead, append one
    # unconditional, global-only, limit-less summary row, always under a
    # fixed monthly bucket (FR-011, revised — no configurable period of its
    # own, and no single "the" agentic period to inherit in XTM One mode):
    quotas_summary.append(
        await _scan_period("sandbox", "monthly", None, is_global=True)
    )
```

### Frontend contracts (four files, spec FR-013/FR-014, revised 2026-07-31 — see research.md §10.4)

**Superseded**: the design below (giving sandbox its own row on all four
pages) shipped, then was reversed the same day. Current contract: sandbox
never appears as its own row; its count is folded into the displayed
`"agentic"` number wherever one is already shown.

- `frontend/src/pages/Profile.tsx`, `frontend/src/pages/UserDetail.tsx`: no
  `"sandbox"` entry in the usage-types array. In the `"agentic"` row's
  render logic (otherwise completely unmodified), add the usage list's
  `usage_type === "sandbox"` total-entry count into the displayed count.
- `frontend/src/pages/GroupDetail.tsx`: no `"sandbox"` entry in
  `QUOTA_TYPES` — fully reverted to its pre-FR-013 shape. There was no usage
  count on this row to merge (it was a period-config indicator).
- `frontend/src/pages/settings/QuotasSection.tsx`: no `sandbox` entry in
  `QUOTA_TYPE_META`, no standalone sandbox tile. The backend's global-only
  `"sandbox"` summary row's `current_usage` is added into the displayed
  `"agentic"` `QuotaProgressCard`'s `current_usage` (and, in the `xtm_one`
  multi-gauge layout, into only the `"monthly"` `agentic` entry).
