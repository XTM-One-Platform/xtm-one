# Background task resume across a rollout

Companion to [`chat-turn-resilience.md`](chat-turn-resilience.md), which covers
the chat and channel turns. This page covers a **background task**: a
sub-agent delegation, or the child run of an assignment, run by the `sub_agent`
orchestration adapter (`services/orchestration_adapters/sub_agent.py`, body
`sub_agent._execute_sub_agent_task`).

Observed before the change: a rollout, a scale-in, a node drain or a lost lease
took the worker away mid-turn. The engine ran the task again from its
description, every tool call made a second time; with no attempt left the task
stayed `running` until its deadline plus grace (hours for a long assignment),
and the work item it had claimed stayed claimed by a dead run.

## 1. The turn checkpoints its completed rounds

`services/background_checkpoint.py`. `TaskCheckpoint` is the `TurnCheckpoint`
writer of a chat turn (`chat-turn-resilience.md` section 5) with a Redis
row per task instead of a conversation row:

- key `background-task:checkpoint:{task_id}`, TTL `CHECKPOINT_TTL_SECONDS`
  (24 h), `turn_id` = `attempt-N` of the attempt that wrote it;
- `offer()` is the `run_agent_chat(on_checkpoint=...)` callback: latest
  snapshot wins, written off the loop's path, each result bounded as section 9
  of the chat contract says;
- a turn that ends in its process (any outcome) closes without flushing and
  deletes the row (`discard_task_checkpoint`); nothing is left to resume.

Best effort by contract: a checkpoint that cannot be read or written costs the
resume, never the task.

## 2. The next attempt resumes from it, at most 3 times

When `_execute_sub_agent_task` finds an earlier attempt ran (the row is still
`running`, or `attempt > 1`):

1. `load_task_replay()` reads the checkpoint first: what it replays
   says whether the lost attempt progressed.
2. `resume_refused(metadata, replayed_calls=...)`: the bounds of section 5ter
   reached, the task is failed before its turn (`_fail_orphaned_task`, the
   reason names the counts), its checkpoint deleted, its claims released in
   the same pass once the failure is committed (`sub_agent.release_failed_task_claims`), the scheduler tick staying the backstop.
3. Otherwise the replay holds the completed rounds,
   then the resume note of a chat turn (`turn_checkpoint.resume_note`: the calls
   replayed, the last one, the results cut) with the manifest of the sandbox
   workspace the task runs in (`manifest_for_resume`, the delegating
   conversation's workspace), so the resumed turn finds its files and its
   uncommitted edits. No prompt text beyond those facts (invariant 16).
3. `note_resume()` records the resume on the task, `metadata_json["resumes"]`:

   | Key | Meaning |
   |---|---|
   | `at` | when the resume started |
   | `attempt` | this attempt |
   | `cause` | what took the earlier attempt away (`lost_cause`: a stop signal hand-back, the engine's take-back reason with the worker gone and its last heartbeat, a lost lease) |
   | `replayed_calls` | tool calls replayed |
   | `resumed_from` | the attempt that wrote the checkpoint (`attempt-N`) |
   | `resumed_as` | this attempt (`attempt-M`): the trace link from the attempt lost to the one that went on |

   and logs one INFO line naming the task, its run, the cause and the count.
4. The turn starts with that history (`run_agent_chat(history=replay.history)`)
   and checkpoints its own rounds on top of the inherited ones.

The work item claims are carried: held by the task (or its run), which is alive
again.

## 3. A task nobody works on is failed, its claims released

- **Worker known gone.** The engine takes a lease back early when the worker's
  presence says it is gone (`lease_expiry(...).owner_record`). The
  adapter declares `resume_when_worker_gone = True`, and `on_dead_letter` fails
  such a task at once (no worker is left to write its ending) instead of
  marking it orphaned.
- **Lease lapsed, worker unknown.** The task is marked orphaned and
  failed by the orphan sweep at `fail_after` =
  `min(deadline + ORCHESTRATION_GRACE_SECONDS, now + orphan_wait_seconds())`.
  `orphan_wait_seconds()` is `STALE_IN_PROCESS_RUN_SECONDS` (900 s, at least
  60 s): a far deadline no longer keeps a dead task running for hours.
- **Same pass.** The scheduler tick (`autonomous._run_autonomous_tick_inner`)
  calls `work_item.release_ended_run_claims()` right after the orphan sweep and
  right after the stale assignment run cleanup, so the claims of what they just
  failed go back to the queue in that tick, each release logged.
- **Cadence.** `STALE_ASSIGNMENT_RUN_SECONDS` defaults to 900 s (was 1800 s),
  matching the orphan threshold: a run nobody works on is failed, and its
  claims released, within 15 minutes.
- Every failure logs one ERROR line naming the task, its run and the cause.

## 4. A stopping worker winds its turns down and hands the tasks back

- `worker._note_stop_signals` chains SAQ's SIGTERM / SIGINT handler;
  `worker.stop_asked()` records the stop in `core/worker_shutdown`
  (`note_shutdown(STOP, signal)`).
- From then on `worker_shutdown.wind_down_due()` holds (at once when the grace
  is unknown, else in the last `WIND_DOWN_MARGIN_SECONDS` of it) and the agent
  loop of a checkpointed turn stops at its next iteration boundary, the call in
  flight answered, by raising `TurnWoundDown` (`chat-turn-resilience.md`
  section 9).
- `_execute_sub_agent_task` catches it and calls `_hand_back_wound_down_task`:
  the checkpoint is flushed (`TaskCheckpoint.aclose(flush=True)` writes the
  latest snapshot once more), the task stays `running` with
  `metadata_json["handed_back"]` (`at`, `attempt`, `signal`), and the outcome is
  `handed_back`. A task that ended, or that a later attempt replaced, meanwhile
  keeps that ending.
- The adapter maps `handed_back` to a deferral (`AdapterResult.defer`,
  `place="head"`, `adapter_state="handed_back"`): the attempt is given
  back, the execution is first in its adapter's queue, every worker is woken,
  and the next worker resumes it (section 2) with the cause "its worker was
  asked to stop (SIGTERM) and handed it back". It was a retryable failure until
  On the execution's last attempt `engine.fail` dead-lettered it, so the
  task read "runs again at once" and never ran again. The task's own
  `MAX_RESUMES` is the bound.
- The deferral's `payload["hold"]` record only carries the attempt and the
  runnable budget over. It is not an AI hold: the claim that resumes the task
  drops it (`_end_hand_back`, before the AI gate is read), so a hold met later
  counts its `AI_UNAVAILABLE_HOLD_MAX_SECONDS` from its own start, not from
  the hand-back. An AI hold record is left to `close_hold_if_ai_is_back`.
  The record's `reason` decides, not the adapter state: a resuming claim lost
  before that point is taken back as `worker_gone` with the record kept, and
  the next claim still drops both.
- Oversized results are kept at the stop, not only at the hand-back (#8636).
  A task or run checkpoint with an acting user registers
  `worker_shutdown.on_stop`. The moment the stop is recorded, it writes its
  latest snapshot again with every result over `MAX_REPLAYED_RESULT_CHARS`
  kept whole as a file (`turn_checkpoint._spill`), and so does every later
  write. A turn still inside a tool call or a wait when the worker goes never
  reaches `TurnWoundDown`: the engine takes it back without a flushing close,
  and before this change its replay read "the rest was not kept". The file is
  stored once across writes and attempts (`spill` on the entry). A turn that
  ends with no stop keeps no file, and a closed checkpoint unregisters its
  listener. The scheduled instruction run passes its user to `checkpoint_for`
  as well.

## 4quater. A turn cut mid call is never a completion

The wind-down of section 4 stops a turn at an iteration boundary. A rollout can
also cut it in the middle of a call: the engine takes a draining worker's
executions back, the old worker's lease heartbeat gets `LeaseLostError` and
fires the execution's `cancel_event`, and the agent loop answers the call in
flight (a tool, a sandbox container starting) with `_cancelled_chat_response`:
`stop_reason == "cancelled"`, text "The request was cancelled.". Formerly
`_execute_sub_agent_task` persisted that text as the task's result: `completed`,
a completion notice, the parent woken as completed, no resume.

`run_agent_chat` reports the loop's `stop_reason` in `loop_outcome`, and the
task body reads it right after the turn:

- **Its worker is stopping** (`orchestration_reconciler.worker_stopping()`: a
  stop signal seen, or its drain): handed back exactly as a wound-down turn,
  through `_hand_back_wound_down_task` (checkpoint flushed, `handed_back`
  recorded, deferred first in the queue, resumed by the next worker, a graceful
  resume in the sense of section 5bis). A task that ended, or that a later
  attempt replaced, meanwhile keeps that ending.
- **No stop of this worker** (an operator cancel, a lease lost to another
  worker): it fails with `TURN_CUT_ERROR` ("Its turn was cut while it ran ...
  and stopped without an answer.") through the failure path, which keeps a
  task the user killed `cancelled` and lets a replaced attempt write nothing.
- A turn that answered (any other `stop_reason`) completes as before, on a
  stopping worker too.

Tests: `backend/tests/test_background_task_turn_cut.py`.

## 4quinquies. One turn, one writer: a taken-over turn stops before its next tool round

The engine can take an execution back from a worker still running it: its lease
expired while its heartbeats failed, or the reconciler took it as gone. The row
goes back to `pending`, another worker claims it and resumes the turn from its
checkpoint, and the first worker's agent loop runs on. Its ending is refused,
but every tool call in between used to run twice.

An adapter whose executions run agent turns sets
`OrchestrationAdapter.single_writer_turns` (background tasks, assignment
runs). `orchestration_reconciler._do_execute` then binds a check around
`adapter.execute` (`core/turn_writer.single_writer`), and the agent loop asks it
before every round with a tool call (`ensure_single_writer`, single and parallel
rounds, waits included), as does `assignment_engine._run_unless_stopped` before
each approved or cleared call of a resumed run, and the activation gate of a
run (before its gate tool, and before a failed gate ends the run). The check reads the execution
row by primary key on a session of its own and applies
`orchestration_engine.superseded_by`:

- `running` under another worker's lease, or at a later attempt than the one
  this turn runs: taken over;
- `pending`: taken back, queued for the next claim;
- the row gone;
- a terminal row is not a take-over (the cancel watcher and the turn's own end
  handle it).

A taken-over turn raises `TurnSuperseded` (a cancellation, like
`TurnWoundDown`): no further call runs, no failure handler records an error.
The background task stops its checkpoint writer without a last snapshot, and
its checkpoint writes carry the attempt's `(epoch, attempt)` place through the
same compare-and-set as a run's (`_set_if_not_later`), so a write still in
flight never lands over its successor's snapshot; the assignment run counts as
replaced (no ending, its partner work left to the attempt that goes on); the
reconciler logs it and writes nothing. A check that cannot be read lets the
turn go on, like the heartbeat.

Tests: `backend/tests/test_turn_single_writer.py`.

## 4bis. A worker that said it was stopping does not spend an attempt

`reclaim_from_gone_owners` takes back the executions of a worker known gone.
When that worker's last presence record says it was stopping (`draining` or
`stopped`, `orchestration_engine.stopped_on_its_word`), the attempt it counted
is given back: the work ended for a reason outside it. A worker gone without a
word (a crash, an OOM kill, a lapsed record) still spends it, since it may have
died of that very work. Formerly every rollout spent an attempt of each
execution its worker held, even one queued behind the semaphore that never ran,
so after two rollouts the next cut ended the task with "stopped (SIGTERM) ...
with no attempt left" and its work item stayed held until the release sweep.

Two rollout cases are read as a stop on its word too:

- **Its lease expired before the next worker started.** The reconciler runs
  the take-back of workers gone before the expired-lease sweep, over expired
  leases too (`leased_owners(..., include_expired=True)`): the worker's last
  word, kept an hour (`worker_presence.LAST_WORD_TTL_SECONDS`), decides. Formerly
  the expired-lease sweep took such a row first, with no record, and the
  resume that followed was counted as a loss without a word (cap 3). Under the
  row lock, `reclaim_from_gone_owners` leaves a row whose lease is live and
  was written after the holder's last word (`_renewed_since_its_word`): its
  presence lapsed but its process still works on it. The cycle runs this
  take-back before any other sweep, and the deadline hold repair never stamps
  a leased row's `updated_at` (it keeps it in the UPDATE with
  `flag_modified`, so the column's `onupdate` stays out too): only the holder
  writes it. A row already past its deadline is left to `sweep_deadlines`, so
  it dead-letters on its deadline, never as a lease taken back.
- **Its deploy killed it before its hand-back.** The version alone proves
  nothing, so all of these must hold (`worker_presence.replaced_by_deploy`):
  its last record says `running` from another release; it was last heard at
  or after the reclaiming process came up (`_PROCESS_STARTED_AT`: in a rolling
  deploy the new worker is up before the old one is killed); its memory
  telemetry is there and below `worker_presence.OOM_SHARE`; no out-of-memory
  kill counted on its container. Then `gone` marks its record
  `replaced_by_deploy`, `stopped_on_its_word` reads it, and `stop_reason` says
  "replaced by a deploy (old -> new)". Anything less (same release, no
  telemetry, an earlier last word, a reclaimer started after the kill) still
  spends its attempt.

Tests: `backend/tests/test_rollout_stop_graceful.py`.

## 4ter. A legacy claim is taken over by its own agent's next run

A claim that records no run (made in a chat turn, or by an older release) is released
or taken over only once quiet for `LEGACY_CLAIM_GRACE`. The holding agent's own
successor runs write progress notes on the item, which refresh its activity, so
the claim was never quiet and every successor was refused; the inbox then
dispatched the item again. A claim made in a run of the agent that holds the
legacy claim (`_take_over_orphaned_legacy_claim`) now waits for the claim's age
instead (`LEGACY_CLAIM_SAME_AGENT_AGE`, 2 hours), not for a quiet item, so a
chat turn or dispatcher still working on a claim it made recently keeps it. The
live-holder guards apply either way: no run or background task of that agent
alive since the claim, and no active flow run started for the item by its
work-item trigger (`trigger_context.work_item_id`, a claim that records no
run). Another agent, or a claim made outside a run, still waits the grace.

## 5. A task at its resume cap ends with that reason

Observed in production: a task resumed after two rollouts and cut by a
third ended `failed` with the hand-back's text ("... runs again at once from
that checkpoint") and no new run. Cause: its execution was created with the
engine's default `ORCHESTRATION_DEFAULT_MAX_ATTEMPTS` (3), so the third loss
reached `attempt_count >= max_attempts` one resume short of `MAX_RESUMES`;
the engine dead-lettered it and `on_dead_letter` copied the execution's last
error, the hand-back's.

- A task's execution is created with `background_checkpoint.MAX_ATTEMPTS`
  (`MAX_RESUMES + 1` = 4) attempts, or the engine default when higher: its
  first run and every resume it may make.
- A task past its resumes fails with `cap_reached_reason()`, starting
  `RESUME_CAP_REACHED` ("Resume cap reached: ... after N resume(s) ... last
  cause: ..."): said by the attempt that refuses the resume
  (`resume_refused`) and by `on_dead_letter` for a task that resumed or was
  handed back and ran out of attempts (`_cap_reached_error`). Any other dead
  letter (a deadline, a task never resumed) keeps its own reason.
- Its work item claims (the task's and its run's) are released by
  `on_dead_letter` itself once the failure is committed, through
  `release_ended_run_claims(run_ids=...)`, the same predicate scoped to those
  holders; the scheduler pass (section 3) stays the backstop. The ERROR line
  names the task, its run and the reason.

## 5bis. A graceful stop does not spend the resume cap

Observed in production: the hand-back resumed every cut task, but each
resume still counted against `MAX_RESUMES`, whatever its cause. With rollouts
every 5 to 20 minutes, a background task reached the cap after three
deployments and failed with "Resume cap reached ... after 3 resume(s)" though
none of its workers crashed (12 tasks in one morning).

- A resume is **graceful** when the attempt it follows ended on a stop the
  worker announced: the turn was handed back (`metadata_json["handed_back"]`,
  section 4), or the execution was taken back from a worker whose last
  presence record says `draining` or `stopped` (`adapter_state` `worker_gone`
  and `lease_expired.owner_record`, `stopped_on_its_word`, section 4bis; read
  by the adapter, `_taken_back_on_its_word`, and passed to the task as
  `taken_back_on_its_word`).
- A hand-back is graceful only for the claim that resumes it: its deferral
  gives the attempt back, so that claim runs at the attempt the hand-back
  recorded. A claim lost without a word before `note_resume` cleared the
  record spent its attempt; the next claim runs at a later attempt and its
  resume counts (`_hand_back_is_current`), with the cause "the attempt that
  took it over ... was lost without a word before it resumed".
- Every resume is still recorded in `resumes`, with `graceful: true|false`;
  an entry recorded before the flag is read by its cause (a hand-back's ends
  "and handed it back").
- `resume_counts(metadata)` gives `(lost, graceful)`. `resume_refused` refuses
  a resume after a loss without a word (a crash, an OOM kill, a lapsed lease)
  once `lost` reached `MAX_RESUMES` (3), and a graceful resume once `graceful`
  reached `MAX_GRACEFUL_RESUMES` (20 then; now a streak of 3 with no
  progress and 200 in all, section 8quater). A graceful resume never spends
  the first bound; the task's deadline bounds both.
- The reasons name both counts: `cap_reached_reason` ("lost its worker again
  after N resume(s) (N resume(s) after a loss without a word (the most a task
  makes is 3), G after a graceful stop (the most a task makes is 20)) ...")
  and `graceful_cap_reached_reason` ("stopped gracefully again after G
  graceful resume(s) ..."), both starting `RESUME_CAP_REACHED`.
- The engine's attempts follow the same split: a hand-back is a deferral and a
  take-back on its word gives the attempt back, so only losses without
  a word spend an attempt; `_cap_reached_error` (the dead letter) writes
  `cap_reached_reason` with every count. A task's execution runs
  under `MAX_TASK_ATTEMPTS` (section 5ter).
- The INFO line of each resume names it graceful or counted, and both counts;
  the trace's `metadata.resumed_from` carries `resume` of `of` (the counted
  resumes against `MAX_RESUMES`), `graceful`, and `graceful_resumes` of
  `graceful_of`.
- Assignment runs (section 8) still count every resume: out of this change.

## 5ter. Only wordless losses with no progress between them spend the cap

Observed in production: four background tasks failed in 45 minutes with
"Resume cap reached ... 3 resume(s) after a loss without a word (the most a task
makes is 3), 5 to 10 after a graceful stop", during rollouts every 5 to 15
minutes. Each attempt had completed tool calls before its worker went. The
cap counted every wordless loss in the task's life, and the execution was
created with `MAX_ATTEMPTS` (4), so the engine dead-lettered the task at its
third wordless take-back whatever its progress.

A take-back is wordless when the gone worker's last presence record does not
say `draining` or `stopped` (`stopped_on_its_word` false): a `running` record
that lapsed (a pod killed before its reconciler published its drain, a
worker whose loop was blocked), a process gone on the same host, or an
expired lease with no record at all. Formerly the resume recorded the
generic "its worker stopped or lost its lease before the turn ended"; the
engine's own reason never reached the task.

- **Progress** is the checkpoint's cumulative count of completed calls
  (`completed_calls` in the Redis payload and on each resume entry). Every
  `offer` adds the ids of the attempt's own completed calls (not replayed) to
  a set that only grows, and a write stores the inherited count plus its
  size: neither `bounded` dropping the oldest rounds nor the loop compacting
  its transcript before a later snapshot lowers it.
  A checkpoint at `MAX_CHECKPOINT_CHARS` keeps a flat or smaller
  `replayed_calls` while the task advances, and the count still grows. An
  attempt that completed nothing leaves the count as it was. A resume entry or
  a checkpoint written before the count existed is read by its
  `replayed_calls`.
- `lost_streak(metadata)`: the resumes after a wordless loss in a row with no
  progress between them. A resume that shows progress, graceful or not,
  starts the streak again; a graceful resume never adds to it.
- `resume_refused(metadata, replayed_calls=...)` refuses a wordless resume
  when the streak it would make (`next_lost_streak`) passes `MAX_RESUMES` (3),
  or once `MAX_LOST_RESUMES` (20) wordless resumes were made in all, progress
  or not. The task's deadline bounds both. A crash loop that does nothing
  still stops at its fourth loss; a slow one stops at 20.
- `MAX_TASK_ATTEMPTS` (`MAX_LOST_RESUMES + 1`) is the `max_attempts` a task's
  execution is created with: the engine's attempts are the hard bound, the
  streak is the task's own refusal. An execution queued by an older release (4
  attempts) is raised at its next claim (`_raise_attempt_bound`).
  `task_max_attempts(orchestration_default_max_attempts)`: an operator's
  value above the old floor (`MAX_ATTEMPTS`, 4) is kept as it is; the default
  and any value at or below the floor get `MAX_TASK_ATTEMPTS`. The claim-time
  raise touches only a row at exactly the old fixed floor (`MAX_ATTEMPTS`, 4,
  which no row created since carries), whatever the setting is now, and
  raises it to `task_max_attempts(setting)`; any other value is left alone.
  Assignment runs keep `MAX_ATTEMPTS` (section 8).
- **Cause.** The adapter reads the engine's take-back record for the attempt
  just lost (`lease_expired`: `why`, from `worker_presence.stop_reason`, and
  `silent_seconds`; `_taken_back_reason`) and passes it to the task as
  `taken_back_reason`. `lost_cause` uses it after a wordless loss, so the
  resume's `cause` and the cap message name the worker gone, its host and
  version, its memory at its last word and its silence.
- The cap messages, the INFO line and `metadata.resumed_from` carry the
  streak and the total: `streak` of `streak_of` (against `MAX_RESUMES`),
  `resume` of `of` (every wordless resume, against `MAX_LOST_RESUMES`),
  `graceful_resumes` of `graceful_of`.

## 6. The trace of a resumed task: `metadata.resumed_from`

The resumes were recorded on the task (`metadata_json["resumes"]`) and in an
INFO line, never on a trace: a trace search on `resumed_from` found none.
The resumed attempt now opens its turn inside
`langfuse_tracing.next_root_trace(resumed_trace_meta(...))` (the mechanism of
`chat-turn-resilience.md` section 11, slice 4): the first trace it opens
carries

```
metadata.resumed_from = {entry: "background_task", task_id, run_id, cause,
                         attempt (the attempt-N resumed), resumed_as,
                         resume (n), of (20), streak (n), streak_of (3),
                         graceful, graceful_resumes, graceful_of (200),
                         graceful_streak (n), graceful_streak_of (3), at}
```

The same key a resumed chat turn's trace carries, so one search on
`resumed_from` lists both, `entry` telling them apart. A resumed attempt lost
again keeps the link: the lost-run trace (`tracing/background_run.py`,
`build_lost_run_trace`) reads it from the task's `resumes` and carries the same
`metadata.resumed_from`.

## 7. An inbound A2A task cut by a platform stop resumes under the same task id (#1657)

`a2a.tasks.execute_task_run` runs in the web process that received the task.
It now checkpoints its completed tool rounds like an assignment run
(`assignment_run_resume.checkpoint_for`, `on_checkpoint` on `run_agent_chat`).
When its worker stops (`TurnWoundDown` at an iteration boundary, or a
`CancelledError` while `worker_shutdown.cut_cause()` records a stop or a
recycle with no cancel asked), `_hand_back` flushes the checkpoint and, under
the task row then the run row, puts the run back to `pending` with the
hand-back recorded (`background_checkpoint.handed_back`) and the task's lease
refreshed. No terminal write: the partner polling (or resending the same
message id, which `find_existing_task` answers with the same task) reads
`submitted`/`working`, never `failed`.

The A2A reaper loop of every live worker (`main._a2a_reaper_loop`) runs
`resume_handed_back_tasks` first: a hand-back older than
`HAND_BACK_SETTLE_SECONDS`, on a task with no cancel asked, is dispatched
again (`_spawn_task_run`); a stopping worker resumes nothing. The executor's
conditional claim (`pending` to `running`) keeps two dispatches from running
it twice. The resumed run reads the checkpoint (`load_replay`), passes the
same bounds as an assignment run (`resume_refused`: past them the task fails
with their reason), records the resume as graceful (`_record_resume`, the
hand-back cleared) with the claim, keeps its first `started_at`, and replays
the checkpointed rounds into the prompt history.

When the hand-back cannot be written (the run no longer `running`, or the
write fails), the former behaviour stands: the task fails with
`INTERRUPTED_ERROR_TEXT` ("External task interrupted by a platform restart
(...) before it finished; send the task again"). A partner's cancel, or a cut
of unknown cause, stays `canceled`; the reaper (`reap_stale_tasks`, a lease
later) stays the fallback for a process killed before its hand-back.

## 8. An assignment run cut by a rollout resumes the same way

An assignment run that is not a background task's child runs its turn in
`execute_assignment_run`, on the worker that claimed its execution. It now
follows sections 1 to 6 with the same writer, bounds and words
(`services/assignment_run_resume.py`):

- **Checkpoint.** `run_agent_loop` gets `on_checkpoint`: a `RunCheckpoint`
  (a `TaskCheckpoint`) keyed `assignment-run:<run id>` (never a task id),
  written to Redis at every iteration boundary with each secret variable value
  of the run stored as its marker, restored by the resuming attempt
  (`restore_history`). Each write carries the attempt's `(epoch, attempt)`
  place and is skipped when the stored snapshot is from a later attempt: an
  attempt the engine replaced, still alive on its worker, never regresses it.
  The comparison and the write are one Redis Lua script
  (`_WRITE_IF_NOT_LATER_LUA`, a compare-and-set), on every write and on the
  hand-back flush alike. An ending (the turn's own, the cap, the dead letter)
  replaces the checkpoint by a tombstone stamped with its place through the
  same script, and every attempt stops its writer in its teardown, a replaced
  one included: no writer at or before that place brings the transcript back,
  and the tombstone expires with the TTL. The dead letter stamps the later of
  the run's fence and the engine's attempt count (`dead_letter_place`): a
  first attempt records no fence, yet its writer, still winding down, writes
  at attempt 1.
- **Hand-back.** A worker asked to stop raises `TurnWoundDown` at the next
  boundary: the trace ends with the hand-back text, the checkpoint is flushed,
  `trigger_context.handed_back` records the signal and attempt, the run stays
  `running` (its inbox work item claim held by a live run), and the adapter
  returns a deferral first in the queue (`defer(place="head")`, adapter state
  `handed_back`): due again at once, its attempt given back. The
  hand-back credits nothing: its attempt's charge stays recorded on the run
  until the claim resuming it, which runs at the same attempt number, takes it
  as the lost attempt's charge (`take_lost_charge(handed_back_at=...)`) and
  credits it free, or the dead letter does. The take marks the record
  settling (`quota_metered_settling`) instead of removing it, and
  `settle_lost_charge` removes it only once the credit landed: a
  credit that fails, or a worker dying in between, leaves the marked record,
  which no other attempt takes and the dead letter credits once. The dead
  letter (`on_dead_letter` and `_settle_failed_run_charge`) goes through
  `settle_recorded_charge`: it marks the record settling and commits (the
  attempt it gave up on, still running, then leaves it in
  `release_charge_record`), credits, and removes the record only once the
  credit landed. Both credit through `credit_recorded_charge`, which calls
  `quota.credit_agentic_charge_once`: ONE Redis script reads the receipt
  `assignment_run_charge:<run id>:<charge id>` and, unless it reads
  `credited`, decrements every counter the charge added to (floored at 0)
  and sets the receipt `credited` with its expiry. The receipt therefore has
  two states, absent and `credited`, and the counters and the receipt move
  together or not at all. The answers: `LANDED` (credited here, the free
  credit then tallied best-effort), `ALREADY` (credited before, nothing
  moved), `NOT_APPLIED` (the script did not run, or its answer was lost: the
  record stays, and the next pass reads the receipt and credits at most
  once). The next claim ends the deferral record before the AI gate is read
  (`_end_hand_back`, as for a background task). A dead letter before any
  claim (the deferral reached its deadline) stamps its tombstone at least at
  the handed-back attempt (`dead_letter_place(handed_back=...)`), since the
  deferral reset the engine's attempt count.
- **Resume.** An attempt that finds the run left `running` (reclaimed)
  or handed back loads the checkpoint, appends its rounds and the resume note
  after the prompt, and records the resume in `trigger_context.resumes`
  (`resumed_from` / `resumed_as`, cause, replayed calls) with its "running"
  commit. The files the lost attempt produced are registered by reference
  (`load_run_generated_files`, as on a HITL resume), and so is every file a
  replayed result names that the run or its acting user owns, with the same
  id (`assignment_run_resume.replayed_files`; a background task's
  resumed attempt does the same with `sub_agent_files.resumed_file_refs`,
  Its carried record and every file a replayed result names, read as
  the task's user, registered as inherited in its `RecordingFileContext`,
  whether the worker handed it back on SIGTERM or lost its lease): a Video Studio frame,
  contact sheet or captions file is stored for the user without the run's
  stamp, and the checkpoint keeps a result's text, never its `files`. A file
  retention purged stays unreadable. A HITL resume
  (`awaiting_human`) is not a rollout resume.
- **Partner tasks.** A handed-back attempt's teardown, and that of an attempt
  a later one replaced (`REPLACED_ATTEMPT`), cancels no outstanding A2A
  delegation or partner task (`_teardown_partner_work`): the attempt that goes
  on with the run collects them. A terminal outcome cancels them. A run failed
  at the cap, by its attempt, or by its dead letter (whatever its resume
  state, a first attempt lost with nothing recorded included) cancels them
  (`cancel_partner_tasks`, the delegation rows concluded with the failure).
- **Cap.** The bounds of section 5ter (section 8ter): past `MAX_RESUMES` (3)
  wordless resumes in a row with no progress between them,
  `MAX_LOST_RESUMES` (20) wordless resumes in all, or `MAX_GRACEFUL_RESUMES`
  (20) graceful ones (section 8bis), the next attempt fails the run before it
  is charged with "Resume cap reached: the assignment run lost its worker
  again after N resume(s) (S resume(s) in a row after a loss without a word
  with no progress between them (the most a run makes is 3), N after a loss
  without a word in all (the most a run makes is 20), G after a graceful stop
  (the most a run makes is 20)) ...", failure cause `engine_abandoned`
  (`resume_cap`), and releases the inbox work item claim. Its execution is
  created with a background task's bound (`execution_max_attempts`, which is
  `task_max_attempts`), so the engine does not dead-letter a run that
  progressed at its fourth loss; a dead letter with no attempt left after a
  resume or a hand-back writes the same cap reason
  (`AssignmentAdapter.on_dead_letter`), never the hand-back's "runs again at
  once".
- **Retry run.** A `retry_on_failure` run starts without the resumes and
  hand-back of the run it retries (`without_resume_state`).
- **Trace.** The resumed attempt's `assignment-run:*` trace carries
  `metadata.resumed_from` with `entry: "assignment_run"`, `run_id`, cause,
  attempt, `resumed_as`, `resume` n `of` 20 (the wordless resumes in all),
  `streak` n `of` `streak_of` 3, `graceful`, `graceful_resumes` n
  `of` `graceful_of` 20.
- **Cleanup.** The checkpoint is deleted once the turn ends in its process,
  and at the dead letter; otherwise it expires after 24 h.

A run killed with no grace (OOM, node loss) is taken back after its lease as
before, resumed from the last checkpoint written; the stale-run cleanup of
section 3 stays the fallback.

## 8bis. A graceful hand-back does not spend an assignment run's resume cap

Section 5bis applies to assignment runs too. Formerly an assignment run
counted every resume against `MAX_RESUMES`, and its hand-back was a retryable
failure that spent an engine attempt: three rollouts in a row failed a run at
the fourth claim with "Resume cap reached ... after 3 resume(s); last cause:
its worker was asked to stop (SIGTERM) and handed it back", although no work
was lost.

- **Graceful.** The claim that resumes a hand-back current for it
  (`trigger_context.handed_back` recorded at this very attempt, the deferral
  having given the attempt back) records its resume with `graceful: true`
  (`assignment_run_resume.next_resume_is_graceful`). A claim lost without a
  word after the hand-back spent its attempt: the claim after it runs at a
  later attempt and its resume is counted, cause "the attempt that took it
  over ... was lost without a word before it resumed".
- **Bounds.** Counted resumes stop at `MAX_RESUMES` (3); graceful ones at
  `MAX_GRACEFUL_RESUMES` (20), refused with "the assignment run was stopped
  gracefully again after 20 graceful resume(s)"; the run's deadline bounds
  both. Every cap message names both counts.
- **Older records.** A resume recorded before the flag is read by its cause
  (`background_checkpoint.is_graceful`): one ending "and handed it back" is
  graceful. An older assignment-run writer stamped `graceful: false` on
  every resume; such an entry with the hand-back cause is graceful too
  (`assignment_run_resume._is_graceful`): the current writer records that
  cause only on a graceful resume.
- **Log and trace.** The INFO line says "a graceful resume" or "a counted
  resume" with both counts; `metadata.resumed_from` carries `resume` of `of`
  (counted), `graceful`, `graceful_resumes` of `graceful_of`.
- **Take-back.** An assignment run's execution is taken back
  from a gone worker as a background task's is (section 8ter).

## 8ter. An assignment run that progresses between losses keeps going

Sections 5bis and 5ter apply to assignment runs too. Formerly an
assignment run counted every wordless resume against `MAX_RESUMES` whatever it
did between losses, its execution ran under `max(configured, 4)` attempts, and
its adapter did not opt into `resume_when_worker_gone`: a rollout's worker
was never taken back on its word (nor read as replaced by a deploy),
its lease just lapsed, and the loss was wordless. Observed on 1 Nov 2026:
pick-up runs that completed 56 to 94 tool
calls between three consecutive rollouts failed at the third loss.

- **Progress.** The run's checkpoint (`RunCheckpoint`) carries the
  cumulative `completed_calls` of section 5ter; every resume records it
  (`prepare_resume`). The attempt that resumes reads the checkpoint first
  (`load_replay`), so the refusal (`resume_refused(completed_calls=...)`)
  knows whether the run progressed since its last resume; the same replay is
  then recorded and replayed.
- **Bounds.** `assignment_run_resume.lost_streak` / `next_lost_streak` (read
  with `_is_graceful`, so an older hand-back record never adds to the streak):
  refused past `MAX_RESUMES` in a row with no progress, or past
  `MAX_LOST_RESUMES` in all. Graceful resumes keep their own bound.
- **Take-back.** `AssignmentAdapter.resume_when_worker_gone = True`: the
  reconciler takes a run back the moment its worker is known gone, first in
  the queue. The adapter reads the take-back as a background task's does
  (`_taken_back_on_its_word`) and passes it to `execute_assignment_run`: a
  worker that said it was stopping, or that a deploy replaced, gives the
  attempt back and the resume is graceful. A wordless take-back keeps the
  generic cause: the engine's own reason (the worker's host, version and
  memory) is never written on a run, whose `trigger_context` anyone who can
  see the run reads.
- **Legacy rows.** An execution created at the old floor (4) is raised to the
  new bound at its next claim (`sub_agent._raise_attempt_bound`). The engine's
  take-backs read that bound first: `OrchestrationAdapter.raised_attempt_bound`
  (generic, the bound kept by default; `AssignmentAdapter` and
  `SubAgentAdapter` answer `sub_agent.raised_task_bound`, the task bound for a
  row at exactly 4) is handed by the reconciler (`attempt_bound_of`) to
  `reclaim_from_gone_owners` and `reclaim_expired_leases`, and `_take_back`
  raises `max_attempts` with it before its exhaustion check, under the row
  lock and in the transaction of the take-back. A legacy row that loses its
  worker without a word at its fourth claim, before any claim since the
  deploy, goes back to `pending` under the raised bound instead of
  `dead_letter`. A hook that raises keeps the row's
  own bound; the engine holds no adapter-specific logic.
- **Log and trace.** The INFO line names the streak, the total and the
  graceful count; `metadata.resumed_from` carries `streak` / `streak_of`
  with `resume` / `of`.

## 8quater. Rollouts never end a run or a task that progresses between them

Observed in production: assignment runs and background tasks alive for
two hours under a rollout every 4 to 8 minutes failed with "Resume cap reached:
... stopped gracefully again after 20 graceful resume(s)", their completed
calls growing between every take-back. The graceful bound was absolute.

- **Rule.** A graceful resume (a hand-back, a take-back on its worker's word)
  counts against a bound only when no tool call completed since the previous
  resume, the progress signal of section 5ter (`completed_calls`).
  `background_checkpoint.graceful_streak` reads it over the recorded resumes
  (any resume that progressed restarts it, graceful or not);
  `next_graceful_streak` adds the resume about to be made, given the count the
  resuming attempt loaded. `resume_refused` refuses a graceful resume past
  `MAX_GRACEFUL_STREAK` (3) in a row with no progress, or past
  `MAX_GRACEFUL_RESUMES` (200) in all; the deadline stays the absolute limit.
- **Both entries.** The assignment run reads the same functions with its
  own `_is_graceful` (`assignment_run_resume.graceful_streak`,
  `next_graceful_streak`), so runs and tasks share one accounting.
- **Older records.** A resume recorded before `completed_calls` existed tells
  no reliable progress: it restarts the graceful streak rather than ending a
  run alive across the deploy; the bound in all still counts it.
- **Engine attempts.** Unchanged: a graceful stop spends no orchestration
  attempt, so the legacy `max_attempts` raise of section 8ter is untouched.
- **Reason, log and trace.** The cap text names the bound reached ("at the
  bound of 3 graceful resume(s) in a row with no tool call completed between
  them" or "at the bound of 200 graceful resume(s) in all") and every count,
  the graceful streak included; the INFO line names "G of 3 in a row after a
  graceful stop with no progress, N of 200 after a graceful stop in all";
  `metadata.resumed_from` carries `graceful_streak` / `graceful_streak_of`.

### A wait cut by a stopping worker is progress

Observed in production: a run waiting on CI with `wait` was taken back
three times in a row during the same pause. Each cut cancelled the call, so no
tool call completed, `completed_calls` stayed flat and the run failed at the
bound of 3 graceful resumes with no progress.

- **Rule.** In a checkpointed turn (`run_agent_loop(on_checkpoint=...)`), the
  agent loop hands `worker_shutdown.wind_down_due` to `execute_wait` as its
  `stopping` probe, for explicit and implicit waits. The pause checks it at
  every poll slice; once true it ends with `WAIT_REASON_WORKER_STOPPING` and
  the tool result "Wait cut short after N: the server running this turn is
  stopping ... wait again there for the rest". The call is answered, the next
  iteration boundary checkpoints it and winds the turn down (section 4), and
  the resuming attempt loads one more completed call: the graceful streak
  restarts at every take-back.
- **Not changed.** A turn with no checkpoint gets no probe and pauses as
  before; the bounds of section 8quater and the absolute 200 are untouched.

### The resumed turn finishes the cut wait itself (#8607)

Observed in production: with rollouts every 5 to 10 minutes a run waiting on
CI was cut 2 to 7 times per pause, and each resume spent one full model
generation (the whole context) only to call `wait` again for the rest.

- **The cut carries its pause.** `execute_wait` returns `WaitResult.cut` on a
  `WAIT_REASON_WORKER_STOPPING` pause: `started_at` (wall clock), `granted`,
  `requested`, the `until` ids and the seconds `elapsed` before the cut. The
  loop puts it on the tool message (`ChatMessage.wait_cut`, never sent to a
  provider); the turn checkpoint (`turn_checkpoint._entries` / `_messages`)
  keeps it, for chat turns, background tasks and assignment runs alike.
- **The resumed turn waits the rest.** Before its first model call,
  `run_agent_loop` calls `_continue_cut_wait`: when the last tool result of
  the replayed rounds is a cut `wait` and no tool call followed it
  (`cut_wait_to_continue`), the rest is `granted - (now - started_at)`
  (`wait_policy.cut_wait_remaining`, the downtime counted, the pause made
  before the cut charged to the run's wait budget), granted by `grant_wait`
  like any wait (per call, total, run deadline), on the same `until` ids: a
  target that finished while the worker was down ends it at once. The cut
  result is replaced in place by one normal result covering the whole pause
  (`execute_wait(carried=cut)`); the model sees one call and one answer. Cut
  again, the new result keeps the first start for the next attempt. The
  continued pause is announced like any wait (#9265): a `wait_start` frame
  once its grant is settled (granted seconds, the cut call's `reason` and
  `until`, the watched items) and `wait_status` refreshes, so the chat shows
  the waiting state with its countdown and Skip instead of the last setup
  label ("Connecting integrations"). No frame when a target already
  finished. The stream then gets a `wait_continued` event, which the chat
  reads as the end of the pause; the log line names the seconds left and
  waited.
- **The continued round is checkpointed anew (#8652).** The replayed round
  came in with the caller's history, so the loop never offered it to the
  checkpoint again and the inherited copy, "cut short" text included, was
  replayed on every later resume of the same run. Once continued, the round
  (`_round_of_call`) counts as this turn's work, and `TurnCheckpoint.offer`
  drops from the inherited entries the round of every cut `wait` result
  (`wait_cut` set) the turn answers again with another text, by call id
  (`_superseded_dropped`): a run resumed twice or more replays one call and
  one "Waited" result for each continued wait. Any other inherited round a
  checkpoint offers again unchanged (a task's replayed rounds, their `spill`
  record) is kept as stored.
- **The ledger says so.** The `turn_resumed` friction of an assignment run
  whose replay ends on a cut wait carries `wait_continued: true` in its
  evidence (`frictions.note_turn_resumed`).
- **Not changed.** A cut on any other tool, a wait that was not cut and a
  turn without a checkpoint behave as before.

## 8quinquies. A scheduled run killed before its hand-back is taken back on its worker's word

An instruction-mode scheduled work item run (`ScheduledWorkItemAdapter`)
hands itself back like an assignment run when its turn reaches an iteration
boundary, or is cancelled, on a stopping worker. A turn held in one tool call
longer than the pod's termination grace never gets there: the pod is killed.
Formerly the adapter did not set `resume_when_worker_gone`, so the
reconciler's take-back of gone workers (section 4bis) skipped it, the row
waited for the expired-lease sweep, and the resume read "its worker stopped or
lost its lease before the turn ended", `graceful: false`, spending the wordless
cap of 3 although the worker had said it was stopping.

Now the adapter sets `resume_when_worker_gone` (taken back the moment its
worker is known gone, first in its queue) and `raised_attempt_bound` (the task
bound, as an assignment run), and reads `_taken_back_on_its_word(execution)`
before `_end_hand_back`. `execute_scheduled_run` passes the flag to
`resume_decision` (`assignment_run_resume.resume_refused`) and to
`prepare_resume`, so the resume is graceful, with the stop cause. A worker gone
without a word still spends the wordless cap.

Tests: `backend/tests/test_scheduled_action_rollout_resume.py`
(`TestATakeBackOnItsWorkersWord`).

## 8sexies. A resumed run never starves its own schedule

The scheduler does not start a fire while a run of the assignment is active
(`has_active_run`). A run resumed across many rollouts stayed active for
hours: its `started_at` was reset at every resume, each attempt got the whole
`timeout_seconds`, and every fire in between was dropped with a DEBUG log.
Four rules, in `services/schedule_overlap.py`:

1. **A dropped fire is a `skipped` run.** `autonomous._record_skipped_fire`
   records it with the shape of a schedule run, `trigger_context.skipped_behind_run`
   and the summary `Scheduled fire skipped: previous run <id> still <status>
   since <iso> (<n> min).` It is never executed. Best effort: a failure to
   record it is logged and the tick goes on.
2. **The timeout bounds the wall time across resumes.** A resume keeps the
   run's first `started_at`; before it is charged, `wall_time_refusal` fails a
   run whose wall time is past its effective timeout (the floored
   `timeout_seconds`, as its orchestration deadline), cause `engine_abandoned`
   (`wall_time`), its work item claims released. A disabled timeout
   (`timeout_seconds <= 0`) bounds nothing.
3. **A fire due behind a run tightens its graceful streak.** When the first
   fire of a `schedule` assignment after the run's `created_at` is due,
   `graceful_streak_bound` is `STARVING_GRACEFUL_STREAK` (1) instead of
   `MAX_GRACEFUL_STREAK` (3), passed to `resume_refused`: the run ends at its
   second graceful take-back with no tool call completed, not its third. A run
   that progresses between take-backs is never ended by it.
4. **A fire that never started is superseded, not waited for (#2040).** When
   every active run of the assignment is a `schedule` fire still `pending` or
   `delayed` with no `started_at`, the next fire supersedes it
   (`autonomous._supersede_stale_fires`, `schedule_overlap.supersede_never_started`):
   its execution, still `pending` with no `started_at` in its payload, is locked
   `FOR UPDATE SKIP LOCKED` and cancelled, the run ends `skipped` with
   `stop_reason` `superseded`, and the new fire is created. A run that started,
   a run a worker claimed meanwhile, or an event or manual run keeps its place
   and the new fire is recorded skipped as in rule 1. A saturated queue costs a
   monitor one pass, never two.

Waiting runs and starvation (#2040, `services/run_queue_starvation.py`): a
`pending` or `delayed` assignment run shows in `list_runs` and `get_run` a
`Waiting:` line, its reason (not due yet, or no free worker for N s) and its
position among the due assignment executions not started. On every scheduler
tick `check_queue_starvation` notifies every active administrator in the bell
(`notification_source = assignment_queue_starvation`) when the oldest due run
has waited over `STARVATION_ALERT_SECONDS` (900 s) or a run expired
`expired_not_started` in the last 5 minutes: ONCE per episode, the Redis key
`assignment_queue:starvation_episode` held while it lasts and dropped when the
queue drains. Redis unreadable sends nothing.

Outgoing workers (#2040): a worker that reads itself outgoing
(`worker_presence.outgoing`, #12386) leaves due resumable work to the incoming
release for `OUTGOING_LEAVE_SECONDS` (120 s, through `ripe_seconds`), then
claims it. It used to claim none for as long as a worker of a newer-started
release stayed present, so a pod of another build left up held the assignment
queue for hours.

Tests: `backend/tests/test_schedule_overlap.py`.

## 8bis. A consult is bounded by its events, and never left running

`consult_agent` runs the consulted peer's turn in-process, inside the
caller's tool call (`agent_chat._consult_agent`). Its child `AssignmentRun`
(`assignment_id IS NULL`, `trigger_payload.delegation_type = "consult"`) has
no orchestration execution by design: it is not resumed like a background
task, and formerly nothing but the caller could tell a live consult from
a dead one. Observed on 1 Nov 2026: one review took six consults in 40
minutes; child runs 214927b1 and 76b3891a sat `running` with no trace until
the stale-run sweep failed them at 900 s, the caller blocked for as long.

- **Watchdog** (`services/consult_watchdog.py`). Every event the consulted
  turn emits is observed. None within `CONSULT_START_TIMEOUT_SECONDS` (90 s):
  it never started. None for `CONSULT_IDLE_TIMEOUT_SECONDS` (600 s): it
  stopped. The loop emits `preparing` before setup, an event per iteration
  and a `tool_heartbeat` every 10 s while a tool runs, so only a model call
  longer than 600 s looks silent. `run_watched` then cancels the turn, the
  child run is failed with the reason, and the caller gets at once
  `Consultation with NAME failed: the consulted turn (child run ID) ...`.
- **Cancellation** (the 900 s agent-tool timeout, the caller's own turn cut
  or stopped): the child run is failed with `CONSULT_CANCELLED_ERROR` in a
  session of its own (`finalize_consult_child`, shielded) before the
  cancellation goes on. A process killed with no grace still leaves it to
  the shutdown sweep (`fail_orphaned_in_process_runs`) or the stale-run
  sweep of section 3.
- **Retries.** `ConsultRetryLedger` counts the consecutive failed consults
  of one turn per peer. Past `CONSULT_RETRY_WARN_AFTER` (2) the next consult
  logs a warning naming the parent run and the peer, and the caller's root
  trace carries `metadata.consult_retries` (failures per peer id).

## 8ter. A consult cut by a rollout fails at once, and the caller asks again

A consult is not resumed like a background task, by design: the consulted
turn runs inside the caller's tool call, with no orchestration execution to
hand back (section 4), no checkpoint of its own (`on_checkpoint` is not
passed to it) and an answer the caller awaits in memory. When the process
stops, the caller is what resumes: its own background task (section 2), its
assignment run (section 8) or its cut chat turn. What the consult can
do is end before the drain cuts it, so the caller's tool call has an answer
that its checkpoint keeps.

- **Cut.** `run_watched(..., cut_check=rollout_cut_reason)`: from the moment
  `worker_shutdown.wind_down_due()` holds (the moment a checkpointed turn of
  the same process winds down), the consulted turn is cancelled and
  `ConsultCutError` (a `ConsultStalledError`) is raised; it is also read
  before the first wait, so no consult starts on a stopping process. The chat
  handler fails the child run with the reason ("was cut by a rollout") and
  answers at once: `Consultation with NAME failed: the consulted turn (child
  run ID) was cut by a rollout ... Ask the same question again now`. The
  failure is counted by `ConsultRetryLedger` like any other.
- **Immediate retry.** The answer completes the caller's tool round, so its
  checkpoint holds it; the caller winds down at its next boundary, is resumed
  on the next process, and asks again there: no 90 s start bound, no 600 s
  idle bound, no 900 s stale sweep is waited on.
- **Drain.** A consult still cancelled by the drain itself (a turn with no
  checkpoint runs on to the grace's end) fails its child with
  `CONSULT_ROLLOUT_ERROR` instead of `CONSULT_CANCELLED_ERROR`
  (`consult_cancel_error`: the graceful-shutdown cancellation, or a stop
  recorded by `worker_shutdown`).
- **Assignment runs.** The consult handler of a scheduled assignment run
  (`assignment_engine`, `_consult_agent`) runs under the same watchdog
  (90 s start, 600 s idle, the rollout cut): it passes `on_event` to the
  consulted turn and answers `Consultation with NAME failed: ...` once a bound
  is crossed. It creates no child `AssignmentRun` (the delegation log row is
  its only record), so nothing is left `running` when it is cancelled.

### A cut consult becomes a durable work item

"Ask again on the next process" holds only when the next process lives longer
than the consulted turn. Under a rollout every 5 to 7 minutes a consult that
takes 5 to 10 (a UX review with a lab) is cut on every re-ask, and the
question lives only in the caller's context: when the caller gave up or
ended, nothing carried it (2 Nov 2026: a UI PR sat green 3 h 40 waiting for a
verdict nobody was asked for any more). The caller is taken back at the same
rollout, so an in-memory count of cuts restarts at zero on every resumed
process: the durable path starts at the FIRST cut
(`CONSULT_DURABLE_AFTER_CUTS = 1`).

- **The ask.** On a `ConsultCutError`, both handlers call
  `consult_watchdog.durable_consult_after_cut`: a work item owned by the
  consulted agent (priority high, `dedupe`, key `consult:<peer 8>-<sha256 of
  the question, 12>`) holding the question, the caller, its run and its work
  item. The consulted agent's inbox answers it and completes it with the
  answer as its result. A re-ask of the same question to the same peer finds
  the same unfinished item. A re-ask in other words (#2218, `open_twin_ask`):
  when the same caller has an open `consult:<peer 8>-*` ask (never a
  `-answer` item) to the same peer for the same person created within
  `TWIN_ASK_WINDOW` (30 minutes), the new wording is a stamped note on that
  ask and its key is reused for the follow-up: one ask, one run.
- **The answer back.** When the caller is an assignment run dispatched for a
  work item (`_inbox_dispatched_work_item`), a second item owned by the caller
  and `blocked_on` the ask (key `<ask key>-answer`) is created: the scheduler
  reopens it when the ask is done, and the caller's inbox resumes the work
  there with the answer, whether or not the cut run is still alive.
- **The tool result.** Names both ids and says not to ask again, so the model
  ends its turn with a note instead of looping on re-asks. A write that fails
  answers the plain cut message (never raises). Anonymous chat callers keep
  the plain message (an item needs an owning user).
- **A peer with no inbox.** The ask only works when a `work_item` inbox (an
  enabled assignment or an active flow trigger) takes it from the peer's
  queue. Before writing, `durable_consult_after_cut` reads the assignment and
  flow inboxes and checks one of the peer's takes an item with the ask's key
  and priority (`peer_inbox_takes`: owner, correlation prefix, priority floor,
  company-managed writer, the parts of `work_item._inbox_clauses` an unwritten
  item can be read against). None does: no ask and no follow-up are written,
  and the tool call answers `no_inbox_cut_answer` (cut by a rollout, the peer
  has no work-item inbox, ask again now or go on without the answer), so no
  orphan item sits in a queue nobody drains. A failed inbox read writes the
  ask as before.

## 8quater. A recording relaunched after its worker went (#9885)

A take taken back from a worker gone (`reclaim_from_gone_owners`, no hand-back)
kept the deadline of its first enqueue, mostly spent by its capture and its
wait in the queue: `sweep_deadlines` dead-lettered it while its next worker
held its lease and heartbeated, and the take failed although an attempt was
left.

- **The claim restarts the deadline.** `VideoTakeAdapter.execute` of a
  relaunched take (`relaunched_take`: handed back, or given its attempt back)
  moves its deadline out to a whole take's from that claim
  (`extend_deadline`, as its lease owner) and the running task's timeout
  follows at once (`orchestration_reconciler.follow_deadline`). The first
  deadline bounds the wait for a worker, never the run that worker does.
- **One more run on a deadline.** `VideoTakeAdapter.on_dead_letter` calls
  `relaunch_after_deadline`: a relaunched take dead-lettered on its deadline
  with an attempt left goes back first in the lane on its next attempt
  (`orchestration_engine.relaunch_dead_lettered`: the attempts used kept,
  `max_attempts` still the bound), once (`payload["deadline_relaunch"]`).
  Caught under a worker, its keyframes resume point is dropped and it is
  recorded again from its script; caught in the queue, it keeps it.
- **The reason names the worker and the stage.** `sweep_deadlines` writes
  "Deadline exceeded at ... (held by <worker>)." for a leased row, and the
  relaunch reason adds the take's progress and stage.

Tests: `tests/test_video_studio_take_relaunch_deadline_9885.py`.

## 9. Not covered yet

E-mail and other chat entry points: `chat-turn-resilience.md` section 11.

Tests: `tests/test_background_task_resume.py` (sections 1 to 6, 5bis),
`tests/test_sub_agent_lease_orphan.py`, `tests/test_turn_resume_replay.py`,
`tests/test_a2a_transfer_and_timeout.py` (section 7),
`tests/test_dead_holder_after_fix.py` (sections 4, 4bis and 4ter),
`tests/test_assignment_run_resume.py` (section 8),
`tests/test_consult_watchdog.py` (sections 8bis and 8ter).
