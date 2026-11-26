# Frictions

The friction ledger turns the harness improvement loop into a record. A
friction is anything a run struggled with: a tool error, a retry, an approval
pause, a sandbox or runner failure, a code_interpreter write to a checked-out
source file, a resumed turn, a trigger filter edited by an agent, an event
replay, or an observation the agent records itself. Each friction is
classified and disposed of: the work item, shared-state entry, skill or prompt
edit that carries its fix, or `accepted`.

Issue: #8528. Slice 1 shipped the store, the automatic record of tool errors
and the three coordination tools. Slice 2a adds the approval pause and resumed
turn records, `frictions_undisposed` on the run and the two platform events.
Slice 2b adds the sandbox, runner, code_interpreter and trigger-config hooks.
Slice 3a adds the trace rollup read-back, slice 3b the finalisation nag,
slice 3c the insights counts, slice 3d the run page Frictions card. The
event-replay hook waits for a replay path (none exists yet).

## Store

`agent_frictions` (`app/models/agent_friction.py`, migration `f8528frict01`):

| Column | Meaning |
|---|---|
| `agent_id`, `user_id` | The agent that met it, the person the run acts for |
| `run_id`, `assignment_id`, `work_item_id` | The run, its assignment and the work item that triggered it, when known |
| `kind` | `tool_error`, `retry`, `approval_pause`, `sandbox_failure`, `runner_failure`, `code_interpreter_source_write`, `trigger_filter_hand_edit`, `event_replayed`, `turn_resumed`, `manual`, `expected_error`, `probe_passed_unexpectedly` |
| `tool`, `evidence` | The tool involved and what was observed, bounded to 2,000 characters |
| `evidence_hash`, `count` | The dedupe key and how many times the run met it |
| `friction_class` | `harness`, `procedure`, `persona_prompt_trigger`, `knowledge`, `platform_acceptable`, `unclassified` |
| `disposition_kind`, `disposition_ref`, `disposed_at` | What carries the fix: `work_item`, `shared_state`, `skill_edit`, `prompt_edit` (each with a ref) or `accepted`; a `harness` friction never takes `skill_edit` or `prompt_edit` (`HARNESS_REFUSED_DISPOSITIONS`, #1686) |

**Dedupe.** Within one run a friction is unique by (kind, tool, evidence hash):
the same friction met again raises `count` and `last_seen_at`. The hash is
taken over the first 500 characters of the evidence with ids, hex digests,
numbers and whitespace normalised, so "timeout after 31 s" and "timeout after
33 s" are one friction. A friction outside a run is never merged.

## Automatic records

`services/frictions.py` keeps a `FrictionScope` (agent, person, run,
assignment, work item) in a context variable:

- the assignment engine binds it around `run_agent_loop` for every run;
- the chat turn binds it around its loop, keeping a scope its run already
  bound (an assignment run's turn) and otherwise using the turn's agent,
  person and `parent_run_id`.

`agent_loop._log_tool_result` calls `note_tool_error` for every result that is
an error (`is_error`, or content starting with `Error`). With a scope bound,
the friction is written in the background with no model call; without one,
nothing happens, so a path that has not opted in is unchanged. The ledger
tools' own errors are not recorded. A ledger failure is logged and never
changes the tool result or the run.

**Declared probes (`expect_error`, #8672).** A call of any tool may carry the
top-level argument `expect_error` (`true` or a one-line reason, at most 200
characters) to declare that its error is the expected outcome of a probe (a
retest that expects a gate refusal). The agent loop takes it off the call
before validation and dispatch (`agent_loop._strip_expect_error`), so the
tool never sees it; a tool whose own schema declares `expect_error` keeps the
argument and no probe is declared; `false` or an empty string declares
nothing. Then, by outcome:

- the call errors: `note_expected_error` writes one `expected_error` record
  already disposed of in the same write (class `platform_acceptable`,
  disposition `accepted`, ref the reason; `record_friction(disposed=...)`).
  It never counts as undisposed, raises no `run.friction_recorded`, and a run
  whose only frictions are declared probes raises no
  `run.completed_with_undisposed_frictions`;
- the call succeeds: `note_probe_passed` writes one undisposed
  `probe_passed_unexpectedly` record naming the mismatch.

The coordination allow-lists, `dispose_friction` and `list_frictions` are
unchanged; a call without the declaration behaves as before. The
`record_friction` description names the declaration for the model.

Two errors the platform already reports are filed the same way, with no
declaration (`agent_loop._log_tool_result`):

- a per-user CTEM stage refused by a module the member has no account on:
  ref `no_module_account: <module>` (#2042, see `dev-docs/ctem-fleets.md`);
- an integration 401 / 403 the run's `IntegrationAuthGuard` recorded on
  `AssignmentRun.integration_auth_failures` and the coverage notice, and every
  call the guard refuses after it: `AuthGuardedExecutor` sets
  `ToolResult.auth_failure_recorded` (`"<integration> <status>"`), and the
  record's ref is `integration auth failure recorded on the run: <integration>
  <status>` (`AUTH_FAILURE_RECORDED_REF`, #2204). An auth error outside a
  guarded integration stays a `tool_error`.

The assignment engine also records, under the run's own scope
(`run_scope(run)`):

- `approval_pause`: when a run pauses on the per-tool approval gate
  (`pending_approvals` set), one friction per tool asked, even when the run
  later completes (`note_approval_pause`);
- `turn_resumed`: when a run is resumed after its worker stopped
  (`assignment_run_resume.prepare_resume`), with the cause and the replayed
  call count (`note_turn_resumed`).

Under the scope the run bound, the tool paths record:

- `sandbox_failure`: an OpenSandbox container that could not be created
  (`_sandbox_selector._fail`, after `log_sandbox_create_failure`), the usage
  type as the tool (`note_sandbox_failure`). A stateless code_interpreter
  create cut by `CREATE_STAGE_MAX_SECONDS` is cancelled before `_fail` runs,
  so `_run_opensandbox` records it itself; its result carries
  `platform_failure` and the tool answers it with `is_error` true, so the
  agent loop also records a `tool_error`. A `run` or `upload` stage timeout
  stays a result of the code, not a platform failure;
- `runner_failure`: a runner command with no runner to run on, or refused by
  the runner's permission policy (`computer_use._drive_command`), the command
  kind as the tool (`note_runner_failure`);
- `code_interpreter_source_write`: a code_interpreter run (stateless or
  kernel, local or OpenSandbox) that added or changed files inside a
  `materialize_repository` checkout, one friction per checkout
  (`commit_scan(count_source_writes=True)` then
  `note_code_interpreter_source_writes`). The checkout record itself does not
  count, nor does a Python bytecode cache (`__pycache__/`, `*.pyc`): a
  code_interpreter run writes none (`PYTHONDONTWRITEBYTECODE`), and
  `sandbox_checkout_status` / `from_checkout` never list one the record does
  not hold (#9518); `sandbox_run_command` leaves it off, so a formatter run is not one.
  `sandbox_write_file` and `sandbox_edit_file` into a checkout are not a
  friction: they are the editor tools this kind points to. A written
  `*.json` under an `i18n/` or `locales/` directory of a checkout is a key
  catalog (`is_i18n_catalog`): the evidence and the code_interpreter result
  both end with the `sandbox_json_keys` call to use instead
  (`catalog_write_hint`, carried on `WorkspaceCall.hints` and appended by
  `format_text_result`, with or without a friction scope); no refusal;
- `trigger_filter_hand_edit`: `update_assignment` saved a new
  `trigger_config` (`note_trigger_config_edit`, the keys named). A write
  that applies a merged fix passes `fix_applied` (a short reason naming the
  PR or the work item, at most 200 characters): the row keeps this kind but
  is written disposed (`platform_acceptable`, `accepted`, the reason as
  ref), like a declared probe, and raises no `run.friction_recorded`. The
  argument is a parameter of the tool, never saved on the assignment.

## Finalisation nag

When the agent loop is about to end on a text answer (no tool calls),
`finalisation_nag()` reads the frictions of the bound run that have no
disposition. If there are some, the loop appends ONE system line naming them
(at most `NAG_MAX_NAMED` = 5 ids, then "and N more") and runs one more round
so the model can call `dispose_friction`; it emits `composing` with reason
`frictions_continuation` (a continuation, never a turn-limit signal). The
guard fires at most once per run, only when `dispose_friction` is among the
tools offered, the next round still has tools, and neither the token budget
nor the tool-failure brake is engaged. No run scope bound, nothing
undisposed, or an unreadable ledger: the loop ends exactly as before. This is
the only prompt addition of the ledger. When the scope names a work item (the
run's trigger) that is already `done` or `cancelled`, the line adds one
sentence saying so and telling the run to write nothing more on it, so a run
that completed its item before its final answer does not write on it again
and meet an "already done" refusal (#269).

## End of run

At a run's terminal ending (completed or failed), after its commit,
`settle_run_frictions(run)` waits up to 5 s for the automatic records in
flight, counts the run's frictions with no disposition and, when there are
some, writes `assignment_runs.frictions_undisposed` (migration `f8528frict02`)
and raises `run.completed_with_undisposed_frictions`. A run that left none is
not written (the column stays NULL) and raises nothing.

## Read-back in `inspect_my_traces`

The calling assignment run's item (the first one, `in_progress`) carries
`rollup.frictions` = `{"recorded": N, "undisposed": M, "by_kind": {kind: N}}`
when the run has recorded at least one friction, read by
`run_friction_summary(db, run_id)` in one grouped query. `recorded` counts
records, not repeats (a friction met again raises its `count`). A chat turn,
a run with no friction, or an unreadable ledger has no `frictions` key, so
the rollup of every other run is unchanged.

Every other listed trace that belongs to an assignment run carries the same
`rollup.frictions`, read for the whole page by
`run_friction_summaries(db, run_ids)` in one grouped query. The ledger is per
run, so each segment of a resumed run shows the run's figures. A finished run
is therefore read back with `inspect_my_traces` (its own agent) or
`inspect_agent_traces` (an agent it may read) without `list_frictions`.

## Counts in platform insights

`query_platform_frictions` (platform insights suite,
`services/platform_insights/frictions.py`) counts the ledger over a window
(`period`, or `date_from` / `date_to`): `totals` = `{recorded, occurrences,
undisposed}` (`occurrences` sums `count`), and with `group_by` (agent, kind,
class, tool, disposition; an undisposed friction is `undisposed`) and / or
`bucket` (day, week, month of `recorded_at`, UTC) tidy rows `{bucket,
dimension, value}` of the chosen `measure` (recorded, occurrences,
undisposed), at most 500, for `render_chart`. An agent row names the agent
and keeps `agent_id`. Filters: `agent_ids`, `kinds`, `classes`, `undisposed`.
The insights scope applies: an administrator's agent counts the platform,
anyone else the frictions recorded for them (`user_id`). Read-only, in the
default approval whitelist.

## Run page

`GET /api/v1/assignments/runs/{run_id}` carries `frictions` (the run's
ledger, oldest first, at most `RUN_PAGE_MAX_FRICTIONS` = 50, each in
`serialize_friction`'s shape, read by `run_frictions(db, run_id)` in the
route's session after its visibility check) and `frictions_undisposed` (the
count the run left at its end). Both are NULL for a run with no friction, so
every other run's response is unchanged. The run page (`RunDetail.tsx`)
shows them in the Frictions card (`components/ui/run-frictions-card.tsx`)
above the conversation log: kind, tool, repeats, class, disposition and the
evidence behind a disclosure, with the undisposed count (the run's figure at
its end, else counted from the rows while it is live).

## Platform events

| Event | When | Payload (filter keys) |
|---|---|---|
| `run.friction_recorded` | A friction is first recorded in a run (a repeat raises only its count) | `friction_id`, `kind`, `tool`, `run_id`, `agent_id`, `assignment_id`, `work_item_id`, plus `evidence` (bounded preview) |
| `run.completed_with_undisposed_frictions` | A run ended leaving frictions undisposed | the `run.*` keys, `count`, `friction_ids` |

Both carry a stable `event_id` (`friction:<id>`, `undisposed-frictions:<run id>`),
so a second publish starts no second run. `run.friction_recorded` is a
personal event of the person the friction was recorded for: only that
person's assignments hear it, and a `scope: all` listener of an administrator.
A fleet orchestrator's listener on `run.completed_with_undisposed_frictions`
uses `scope: fleet`: it hears the runs of its fleet's home members only,
without an agent-id list in its filter.

## Tools (coordination group)

Registered with the shared-state tools, in the `coordination` group (former
id `shared_state`). No approval of any kind.

| Tool | What it does |
|---|---|
| `record_friction(what, where?, evidence?)` | Records a `manual` friction under the run's scope; the same friction twice in a run is counted |
| `list_frictions(run_id?, agent_id?, undisposed?, since_hours?, limit?, offset?)` | The frictions, newest first; default: this run's, else this agent's. `run_id` and `agent_id` take a full id or an 8+ hexadecimal prefix resolved among the caller's frictions; a prefix starting several is refused naming the candidates, one starting none (or a value that is no id) is refused, never dropped: a dropped filter widened the listing to every friction of the person (#11553) |
| `dispose_friction(id, friction_class, disposition, ref?)` | `id` is the full id or an 8+ hexadecimal prefix of one friction of the calling agent or its person (ambiguous: refused with the candidates; none: not found). Classifies a friction and names what carries its fix; a `work_item` ref must be an item the caller can read, by its full id or an 8+ hexadecimal prefix resolved like the work item tools (`builtin_tools/id_prefix.py`; an ambiguous prefix is refused naming the candidates), and its full id is stored; only `accepted` needs no ref; only the agent's own frictions, or its person's, can be disposed of |

`list_frictions` is in the default read-only approval whitelist.

## Tests

`backend/tests/test_frictions.py`: the evidence hash and bound, the scope
binding, the agent loop reporting both error shapes, the dedupe (one record,
count 2), the refusal of an unknown kind, the three tools end to end with the
work-item validation, the refusal to dispose of another agent's friction, the
`run.friction_recorded` event raised once per new friction, the end-of-run
count and event (and nothing for a run that left none), the approval pause
and resumed turn records, the sandbox, runner and trigger-config hooks, the
checkout source writes grouped by checkout and recorded only under a scope,
the two events in the catalogue, and the declared probes (the reason read
from `expect_error`, the argument stripped before the tool runs and kept by a
tool that declares it, the routing by outcome, the expected error written
disposed with no event, a run of probes only raising nothing, a probe that
succeeds recorded undisposed).
`backend/tests/test_frictions_nag.py`: one disposal round when the run ends
with undisposed frictions, at most once, none without `dispose_friction`
offered or with nothing undisposed, and the line itself (names, bound, None
without a run, frictions or a readable store; the finished-item sentence for a
done or cancelled item, none for an open, unread or absent one).
The run page read: `run_frictions` (rows, the bound, `None` without
frictions, a run or a readable store) and the response fields, in
`test_frictions.py`; the card in
`frontend/src/components/ui/run-frictions-card.test.tsx`.
`backend/tests/test_platform_insights_frictions.py`: the totals, the
self scope, the agent names, the tidy rows per bucket and dimension, the
filters in the statement, the refusals by name and the suite wiring.
