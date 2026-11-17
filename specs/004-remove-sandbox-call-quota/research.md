# Phase 0 Research: Remove Platform-Wide Sandbox Call Quota

**Feature**: [spec.md](./spec.md)

## 1. Migration removal strategy: delete outright, not a new downgrade migration

**Decision**: Delete `backend/alembic/versions/e3877a6a8159_add_sandbox_quota_usage_table.py` entirely from the repository. Do not add a new migration that drops the table.

**Rationale**: This project's mandatory reversibility rule (backend `AGENTS.md` Rule 6, Constitution Principle V) exists to protect *already-released* schema history — every migration that has shipped to a real deployment must have a working `downgrade()`. It says nothing about migrations that have never shipped. Confirmed via `git log --oneline --all` on this file: every commit touching it (`921bb7cd` feat, `6f7b02e7`/`2b86b27d`/`c419a6c0` fixups) lives only on feature branches, never merged to `main`. There is no released deployment anywhere that has this migration applied except this repo's own local/CI development databases (created *by* running this branch's code). Treating it as "must be reversed, not deleted" would apply a rule meant for production schema history to a piece of code that was never production schema history in the first place.

This also matches existing precedent already in this codebase: backend `AGENTS.md` itself documents that the earlier migration history was squashed into a single baseline — i.e., this project has already rewritten migration history wholesale once, when the history being rewritten had no ongoing compatibility obligation to protect. Deleting one never-merged migration is a much smaller version of the same idea.

**Mechanics**: `e3877a6a8159`'s `down_revision` is `a7c3sk1ll2697`. Confirmed via `grep -rn "e3877a6a8159" alembic/versions/*.py`: no other file references it as *their* `down_revision` — it is the current, undepended-on chain head. Deleting the file makes `a7c3sk1ll2697` the new head automatically; no other file needs its `down_revision` edited.

**Consequence for already-migrated databases**: any database that already ran `alembic upgrade head` while this migration existed (this branch's own local dev database, and possibly CI) now has `alembic_version` pointing at a revision ID that no longer exists in `versions/`. This is the exact "unrecognized migration state" class of problem this session already diagnosed and fixed once before (for a different, unrelated cause — see spec 002's own history). The existing orphaned-revision auto-heal mechanism (`_reconcile_pre_squash_revision` in `app/core/migrations.py`) will **not** auto-heal this, since it only recognizes one specific, hardcoded historical marker (`PRE_SQUASH_FINAL_HEAD`) — by design, it deliberately refuses any other unrecognized revision rather than guessing. This is correct, expected behavior, not a bug to work around.

**Chosen fix**: a one-time, manual, branch-local reconciliation — not a shipped mechanism:
1. `DROP TABLE sandbox_quota_usage;` (cascades its unique constraint and index automatically)
2. `UPDATE alembic_version SET version_num = 'a7c3sk1ll2697';`

This is exactly the same two-step shape (direct SQL fix on `alembic_version` + a subsequent clean `upgrade head`) already used once this session for the earlier, unrelated `5ac9d3dffb30` mismatch — same tool, different cause.

**Alternatives considered**:
- *Add a new migration whose `upgrade()` drops the table* — rejected per the user's explicit direction and the reasoning above: there is nothing to "upgrade away from" for any real deployment, so a two-migration up/down pair (create-then-drop) would be pure noise in the permanent migration history of a project that has never released either state.
- *Edit `e3877a6a8159`'s body in place to become a no-op, keeping the revision ID* — rejected: this would leave a permanently-confusing artifact (a migration file named "add sandbox quota usage table" that does nothing) sitting in the chain forever, for zero benefit over deleting it outright, since nothing depends on its ID surviving (confirmed above — it's an undepended-on head).

## 2. The naming collision: `disable_sandbox_quota_usage` (keep) vs. `sandbox_quota_usage` (remove)

**Decision**: `settings.disable_sandbox_quota_usage` (a boolean, `app/core/config.py`) is a **spec 002** field and must not be touched. It gates `increment_sandbox_usage` in `app/services/quota.py` (`if settings.disable_sandbox_quota_usage: return`) — a per-user counter written into the pre-existing `quota_usage` table under `usage_type="sandbox"`. This is a completely different mechanism from the **spec 001** table literally named `sandbox_quota_usage` (`app/models/sandbox_quota.py`) and the fields `sandbox_quota_limits`/`sandbox_quota_fail_mode`, all of which back the platform-wide, all-users-combined counter this feature removes.

**Rationale for calling this out explicitly**: an exhaustive repo-wide inventory (dispatched specifically to resolve this ambiguity before any deletion) confirmed both mechanisms independently, by reading behavior rather than trusting name similarity:
- `quota.py` docstring, verbatim: *"Respects `settings.disable_sandbox_quota_usage` (default `False` — counting is on by default; set `True`, e.g. for self-hosted/on-prem deployments, to opt out)."* — this is spec 002's own documented contract for a flag that predates this feature and is unaffected by it.
- `sandbox_quota.py`'s own module docstring describes a durability sink for "the platform-wide OpenSandbox call quota counters (daily/weekly/monthly) maintained by `app.services.sandbox_quota`" with "no per-user/group/agent dimension" — explicitly the opposite shape from spec 002's per-user counter.

**Consequence for implementation**: every removal task must name the exact symbol (not just "the sandbox quota setting") to make this distinction impossible to miss during implementation. `docs/docs/deployment/configuration.md`'s `DISABLE_SANDBOX_QUOTA_USAGE` row (spec 002) stays; only its trailing cross-reference to the now-removed `SANDBOX_QUOTA_LIMITS` needs rewording, not the row itself.

## 3. Call-site removal shape: delete the spec-001 guard, keep the spec-002 guard, fix the comment that bridges them

**Decision**: At all three OpenSandbox dispatch call sites (`code_interpreter.py:execute_python_opensandbox`, `custom_tool_executor.py:execute_js_tool_opensandbox`, `custom_tool_executor.py:execute_python_tool_opensandbox`), the pattern is identical and was read directly during planning:

```python
# BEFORE (current state, all three sites):
from app.services.sandbox_quota import (
    SandboxQuotaExceededError, check_sandbox_quota, record_sandbox_call, sandbox_quota_message,
)
try:
    await check_sandbox_quota()
except SandboxQuotaExceededError as exc:
    <return error dict, or: raise RuntimeError(sandbox_quota_message(exc)) from exc>

# <spec-002 combined-check block — KEEP UNCHANGED, only its leading comment needs rewording>

await record_sandbox_call()

<sandbox creation continues...>
```

```python
# AFTER (all three sites):
# <spec-002 combined-check block — unchanged, comment reworded per below>

<sandbox creation continues...>
```

Both the `from app.services.sandbox_quota import (...)` block and the `try/except SandboxQuotaExceededError` block are deleted; the standalone `await record_sandbox_call()` line is deleted. The spec-002 combined-check `if user_id is not None: ...` block that already sits between them is untouched in behavior.

**The comment problem**: the spec-002 block's leading comment currently justifies its dedicated-session choice partly by reference to code that is being deleted:
- `code_interpreter.py` (lines 558-567, read directly): *"Deliberately BEFORE `record_sandbox_call()`: a call refused here never actually dispatches, so it must not count toward the platform-wide sandbox call counter either."* — both `record_sandbox_call()` and "the platform-wide sandbox call counter" cease to exist.
- `custom_tool_executor.py` (lines 1224-1238, read directly, both call sites): same "before `record_sandbox_call()`" framing, **plus** an additional dangling reference: *"to match this function's own existing `SandboxQuotaExceededError` handling one line above"* — "one line above" is the now-deleted `check_sandbox_quota()` catch block.

**Resolution**: reword both comments to justify the dedicated-session choice purely on its own remaining merits (the `asyncio.gather` concurrent-session hazard, which is unaffected by this removal and still fully applies) — dropping the "before `record_sandbox_call()`" framing and the "one line above" cross-reference entirely, since neither referent survives. This is a Constitution Principle IV concern (rationale comments must explain a real, current constraint) as much as a correctness one — a comment that cites deleted code actively misleads the next reader.

**Alternatives considered**:
- *Leave the comments as-is, since they're "just comments"* — rejected: Principle IV exists precisely because this codebase treats rationale comments as load-bearing documentation in its most security/concurrency-sensitive modules, and a comment citing nonexistent code is worse than no comment (it actively misdirects).

## 4. Test file classification: three clean deletions, two mixed files, one untouched file

**Decision**: classification per file, driven by a full-text inventory of every test referencing any spec-001 symbol:

| File | Action |
|---|---|
| `tests/test_sandbox_quota.py` | Delete entirely — every test in it is spec-001-only, including two tests proving spec-001 behavior is unaffected by spec-002's `disable_sandbox_quota_usage` flag (a non-interference proof, not a spec-002 test — it dies with spec 001, not with spec 002). |
| `tests/test_custom_tool_sandbox_mode.py` | Delete one function only (`test_docker_mode_unaffected_by_exhausted_opensandbox_quota`, FR-014 coverage) — the file's other ~40 tests cover Docker/subprocess/Node-permission sandbox-mode *selection*, unrelated to either quota mechanism. |
| `tests/test_opensandbox_sandbox_mode.py` | Delete 9 functions that test the removed platform-wide enforcement itself (quota-exhausted/proceeds-when-under-quota pairs for all three call sites, the FR-014 non-OpenSandbox-unaffected test, and two cross-spec tests proving spec-002/003 behavior around a now-impossible exhaustion state). Separately **edit** roughly 20 more functions that only imported `sandbox_quota` to neutralize it (`monkeypatch.setattr(sq, "check_sandbox_quota", ...)`) while testing spec-002 (`increment_sandbox_usage`, `disable_sandbox_quota_usage`) or spec-003 (structured logging) behavior — once the call sites no longer call `check_sandbox_quota`/`record_sandbox_call` at all, there is nothing left to neutralize, and the `from app.services import sandbox_quota as sq` import itself must go too (that module will no longer exist). |
| `tests/test_fail_modes.py` | Edit one function (`test_sandbox_dispatch_combined_quota_check_reads_quota_fail_mode_not_sandbox`, spec-002 FR-019 coverage) the same way — drop its `sq` import/neutralizing mocks and its `settings.sandbox_quota_fail_mode` line (which exists only to prove that *now-nonexistent* field has no effect); its core assertion (spec-002's combined check reads `QUOTA_FAIL_MODE`) is untouched. |
| `tests/test_sandbox_usage_quota.py` | Untouched — confirmed zero references to any spec-001 symbol; this file is spec 002's own `increment_sandbox_usage`/period-resolution test home. |

**Rationale**: this mirrors the same "mirror each function's own existing convention, verify by reading not by name" discipline this session already established while *building* spec 002's combined check (which discovered the code-interpreter-vs-custom-tool refusal-shape difference the same way) — apply it symmetrically while *removing* spec 001.

**Alternatives considered**:
- *Delete the whole `test_opensandbox_sandbox_mode.py` file and rewrite it from scratch* — rejected: the file is 1835 lines covering probe/selection/config logic entirely unrelated to either quota mechanism (confirmed by the inventory); a targeted removal is both safer (less risk of losing unrelated coverage) and smaller.

## 5. Admin endpoint removal: no frontend consumer

**Decision**: delete `GET /api/v1/settings/sandbox-quotas` (`get_sandbox_quotas` in `app/api/v1/settings.py`) outright, with no replacement.

**Rationale**: a repo-wide frontend search for `sandbox-quotas`/`sandbox_quotas` returns zero matches — this admin status endpoint (added by spec 001, explicitly labeled as such in its own docstring) has no UI consumer anywhere. There is nothing to redirect or degrade gracefully; the endpoint simply stops existing, and no frontend code needs to change (confirmed in Technical Context / plan.md's Project Structure — this feature touches zero frontend files).

**Alternatives considered**:
- *Keep the endpoint but have it return an empty/"gone" response* — rejected: this is unnecessary API-versioning caution for an endpoint with zero confirmed consumers, on a feature that was itself never merged to `main`.
