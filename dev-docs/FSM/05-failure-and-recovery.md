# Failure & Recovery

All recovery is intrinsic to the engine — there is no startup recovery pass and
no per-consumer stale sweep.

## Worker crash mid-execution

The crashed worker's `running` rows keep a `lease_owner` and a
`lease_expires_at` in the past. On any reconciler cycle (this worker restarting,
or another replica), `reclaim_expired_leases` finds them:

- attempts remaining → back to `pending` (`run_at = now`), re-claimed and re-run;
- attempts exhausted → `dead_letter`, and the adapter's `on_dead_letter` marks
  the domain row (`AssignmentRun` / `EvaluationRun` / `SubAgentTask`) failed.

The engine sees a lease that ended, never a crash: `last_error` says
when the holder was last heard from (its last write of the row - its claim, a
heartbeat or a checkpoint): `Lease expired, last heartbeat 74 s ago, with no
attempt left.`, or `...: run again (1 of 2 attempts used).` when it is re-run.
The facts are recorded under `payload['lease_expired']` (last heartbeat, the
seconds since, the holder, the attempt), and `lease_expiry(execution)` returns
them while the attempt they ended is the current one, so an adapter can say
where its work was (a Video Studio job: `Lease expired at 63% (...), last
heartbeat 74 s ago, ...`). Each reclaim is logged at warning level. Only a
process that saw the exit - the render supervisor naming a render process's
exit code or signal - says a process crashed.

A lease outlives its holder by up to its length (three minutes for a render).
Every reconciler says it is there (`services/worker_presence.py`: a
Redis record refreshed every 5 s, living 20 s, its last word kept an hour),
and for the adapters that set `resume_when_worker_gone` (the Video Studio
render) each cycle takes back at once the `running` rows of a holder known
gone (`leased_owners` + `worker_presence.gone` + `reclaim_from_gone_owners`): a
holder that said it stopped, one on this host whose process is no longer the
one that wrote its word (a container restarted in place, at the new worker's
first cycle), one whose word ran out. They go back first in their adapter's
queue, `adapter_state` `worker_gone`, the attempt counted, with how the holder
went (`worker_presence.stop_reason`: its signal, or SIGKILL / a crash with its
memory at its last word) in `last_error` and its record in
`payload['lease_expired']`. A holder that never published (an older release)
and an unreachable Redis are never read as gone: their leases expire as above.

## Worker stopped (a deploy, a restart)

SAQ sets its stop event on SIGTERM / SIGINT and runs the shutdown hook after
`WORKER_SHUTDOWN_GRACE_PERIOD`. From the stop event the reconciler drains:
it claims no execution and drives no outbox event, while its sweeps
and the executions it runs go on; its presence says `draining`. The shutdown
hook (`stop_reconciler`) then sets `AdapterContext.stopping`: work that resumes
elsewhere hands its execution back (`defer(place="head")`, no attempt spent,
the workers woken), and the reconciler's last word says `stopped` with its
signal. Before it, the hook lets the Video Studio renders past half their
frames finish, within `VIDEO_STUDIO_RENDER_DRAIN_SECONDS` and the pod's
termination grace (`render_process.finish_before_stop`).

An adapter may declare that its executions keep their slot through a
hand-over (`OrchestrationAdapter.keeps_slot_through`): its claims
record when each first took a slot (`payload['slot_since']`), a running
worker keeps a free slot for each one in flight on a draining worker, and a
worker with none free asks the execution that took its slot last to give it
back (`AdapterContext.give_back`) once one handed back has waited
`GIVE_BACK_AFTER_SECONDS`, taking the waiting one in hand (`claim_for`).

The lease heartbeat (`extend_lease` every `lease/3`) keeps a legitimately
long-running job's lease valid so it is never reclaimed out from under itself.
Work that has a progress of its own can renew it from there too: a render's
process renews it from the frames it draws (`video_studio/lease.py`), so a
render that keeps drawing keeps its lease whatever the worker's loop is doing.

A lease is also lost by a worker that is still alive: a heartbeat that could not
reach the database for a lease period just stops (each failed beat is logged
at warning level with how many failed in a row), and the adapter body keeps
running - a render excepted, which stops at its next frame (`lease_lost`). The re-run starts from zero and is charged an agentic action of its
own, so each charge is fenced to the attempt that made it
(`services/attempt_charges.py`) and credited back by at most one of the
re-run that replaces the attempt, that attempt's own failure handler, and
`on_dead_letter`:

- an assignment run records its charge on the run with the id and the
  `attempt_count` of the attempt that made it
  (`trigger_context['quota_metered_charge']` / `['quota_metered_attempt']`);
  every path takes the record off the row under the row lock before it credits,
  and a re-run takes only the charge of an attempt before it. A re-run records
  its place on the run as it starts (`['quota_attempt_fence']`, `[epoch,
  attempt]`; the HITL pause opens the next epoch, since a resume counts its
  attempts from 1 again), so an attempt that charges after a later one started
  gives its charge back and records nothing, whether or not the later one has
  charged yet. `on_dead_letter` of a run a stale attempt already failed credits
  the record it still carries;
- a delegated task's agent turn writes a metering receipt per attempt
  (`sub_agent_task:<id>:<attempt_count>`); a re-run redeems the receipts of the
  attempts before it, `on_dead_letter` those of every attempt (of a task that had
  not ended, or that failed), the charging turn its own - a receipt is redeemed
  once, and a charge made under a receipt already redeemed is given straight
  back.

A run re-run after a lost lease is therefore charged at most one action in
total. The lost attempt's other writes (status, result, files) are not fenced:
it keeps running under the old rules, and what it writes can still land after
the re-run's.

### A background task's lease, and its orphaned worker

A background task (a sub-agent delegation) runs an agent turn whose tool
calls hold the worker for minutes at a time (a `wait` of up to five minutes, a
CI poll, a large read), so it runs under a lease of its own
(`SubAgentAdapter.lease_seconds`, `SUB_AGENT_LEASE_SECONDS`, 900 s by default:
three times the longest tool call, `0` for the engine's), renewed every third
of it like any other. One long call never costs the lease; the recovery of a
dead worker waits that much longer.

When the lease is lost all the same and no attempt is left, the engine
cannot tell a dead worker from one still working, and the worker may be about
to write the ending the caller waits for. `on_dead_letter` therefore leaves a
`running` task running, marked `metadata_json['orphaned']` (when, the
attempt, the holder, its last heartbeat, the execution's error, and
`fail_after`: the execution's deadline plus `ORCHESTRATION_GRACE_SECONDS`, or
`WORKER_JOB_TIMEOUT` from the task's start without one), credits nothing and
wakes nobody. The worker's ending stands when it comes (`_ended_elsewhere`
finds the task unended and unreplaced; `get_task_result` shows it
`completed`). The orphan sweep (`recover_orphaned_sub_agent_tasks`, which
runs on the scheduler tick) fails the task only past `fail_after`, with an
error naming the attempt and its last heartbeat, closes its delegation and
run, and credits the attempts' receipts then. A `pending` task (no worker), a
`failed` one, a deadline blown, or an expiry recorded for an earlier attempt
settle at once, as before.

A task whose execution reached `completed` while the task still reads
`pending` / `running` (its ending write lost between the body and the engine)
is settled by the same sweep once the execution has been quiet for the
stale-run threshold (`orphan_wait_seconds`, 900 s): `completed` when the task
holds a result, `failed` otherwise. Every reason the sweep writes names the
execution id, its state and the run. The first tick after a deploy is the
backfill for the rows already stuck. `list_background_tasks` (the tool and
`GET /sub-agent-tasks`) adds, for a pending or running task,
`execution_state` (`none` without an execution row), `execution_id` and
`last_progress_at` (the execution's `updated_at`), and the tool line names the
run, so a task with no live execution is visible as one.

## Deadline exceeded

`sweep_deadlines` moves any `pending`/`running` row past `deadline_at` to
`dead_letter` (catches "never claimed", "held the lease too long", and "waited
on a callback that never arrived"), then `on_dead_letter` fails the domain row.
`deadline_at` is derived from `assignment.timeout_seconds` /
`evaluation.timeout_seconds` / the sub-agent timeout + a grace window.

Assignment runs (trigger, retry, de-queued delivery) and background tasks
count that budget from their START, not from their creation (#1433):
`queued_deadline()` puts the budget on the payload
(`start_budget_seconds`) and sets `deadline_at` to the queue-wait bound
(`run_at + ORCHESTRATION_MAX_QUEUE_WAIT_SECONDS`, or the assignment's
`trigger_config.max_queue_wait_seconds`; `0` = none). The first `claim_due`
sets `deadline_at = start + budget` and records `started_at` and
`queue_wait_seconds`; a re-claim keeps them. A row the sweep finds past its
queue bound with no `started_at` is marked `expired_before_start`: the
assignment adapter fails the run with `deadline_exceeded_before_start`,
`trigger_context.queue = {queue_wait_seconds, started: false}` and no
`run.failed`; the sub-agent adapter fails the task with the same words.
`timeout_seconds` is execution time. The budget is the effective timeout,
`max(timeout_seconds, 600 s, max_iterations x 20 s)`
(`_apply_iteration_timeout_floor`), plus the grace: `get_assignment` shows it
with the reason (`describe_effective_timeout`) and `get_run` shows the
`start_budget_seconds` and `queue_wait_seconds` its execution recorded. A run
the `wait_for` bound stops ends `failed` with `stop_reason` `timeout`
(`AssignmentTimeoutError`), or `deadline` when its orchestration deadline cut
the bound (`DeadlineCutTimeoutError`). A run a fleet's run budget holds
(`fleet_run_budget.hold_over_budget`, #1526) is not waiting for a worker:
each claim that holds it moves its queue bound to that claim plus its own
queue window (`keep_queue_clock`, the window recorded once on the payload as
`run_budget_queue_window_seconds`), so the sweep never fails it for the hold;
once the fleet is back under budget the ordinary bound runs from the last hold.

The sweep is a backstop, never the way work is meant to end: the work's own
bound fires first, `orchestration_grace_seconds` earlier, and records what the
run did. A re-run after a worker crash keeps the deadline it was created with,
so the assignment and sub-agent adapters hand `deadline_at` to the work they
run, and the work bounds its `asyncio.wait_for` and its agent loop's
`wall_clock_deadline` by `bound_by_deadline()`: what the deadline
leaves, never the whole budget again. Without it the re-run believed it had
its full budget, never wound down, and was dead-lettered mid-work with nothing
recorded. A delegated task also hands its loop that bound (minus a reserve for
the turn's wrap-up) instead of the chat formula's wall clock, so a delegated
agent waiting on its own delegations hands off (`waiting_on_tasks`)
before the bound instead of being killed mid-wait; a turn whose own
`wait_for` still ends the loop (`loop_timeout`) fails the task as a timeout,
never completes it with the chat apology.

Work the deadline leaves nothing of (`deadline_spent()`: reclaimed inside the
grace window, or picked up that late) records its own timeout
(`engine_deadline_exceeded`) before any setup and before anything is charged,
and a setup that used what was left never starts the loop.
Whose timeout it is follows what the engine would have done: within the grace
the bound is the budget less the claim latency and a timeout is the work's own,
as it always was; past it (`deadline_cut_budget()`) the budget would have
outrun the deadline and the sweep would have dead-lettered it, so a timeout
that fired on that bound (not an inner one, not a shorter limit of the agent's
own) goes back free (the attempt's sub-agent receipt, the assignment's free
path), never from the agent's failure pool. A re-run of a row still `running`
from an attempt the engine lost keeps the files that attempt recorded (a task's
record its parent is woken with) and credits that attempt's charge back, fenced
as described under "Worker crash mid-execution"; held instead of run,
the row waits as `pending` (a task with its linked run and delegation), never
shown `running` for the whole hold. A retry run never inherits the failed run's
charge record.

A stale attempt never writes an ending over another. Such an attempt
and the re-run beside it both reach an ending; each reads the row under its
lock right before writing its own (`_ended_elsewhere` in `assignment_engine.py`
and `sub_agent.py`) and discards it, with what follows it (notification,
chain, retry, delivery, wake), when the row already ended - completed, failed,
cancelled, or for a run paused for a human (every attempt commits `running`
before its loop) - or when a later attempt replaced this one: the run's
attempt fence (`quota_attempt_fence`), the task's
`metadata_json['orchestration_attempt']`, written by the re-run as it starts.
The run then ends as the attempt the engine runs it with ends. Every attempt
makes the same check as it starts, a first attempt included (a re-run
can replace it before its first commit), under a lock held until that commit,
so nothing it writes before its loop - an ending, an AI or quota-store hold
with its status and trigger context, the charge record - lands over another
attempt's row either. A hold the engine defers restores the attempt count
(`orchestration_engine.defer`), so the next claim may carry a lower number
than a re-run that fenced the run meanwhile (a worker lost mid-check): such a
hold opens the next epoch (`attempt_charges.fence_for_deferral`,
`sub_agent_files.task_fence_for_deferral` for a task, whose epoch has a key
of its own next to the attempt number), which keeps every earlier attempt replaced and the
next one live; a delegated
task locks its first read for the same reason and re-reads the row before the
endings it writes after its "running" commit. The file record written as files
land (`sub_agent_files.write_task_result_file_ids`) is never written on a task
that has ended or by an attempt a later one replaced. A read that fails
raises: the transaction is unusable, and the engine retries the execution.

## Transient failure

An adapter raising (or returning `FAILED(retryable=True)`) routes through
`fail`: retry with exponential backoff + jitter (`pending`, future `run_at`)
until `max_attempts`, then `dead_letter`.

## Held while agentic AI is unavailable

Work that comes due while the background gate refuses (Community Edition, or
the AI switch off) has not started and is not at fault. The adapter returns
`DEFER` (`ai_hold.hold_result`) and the reconciler applies `defer`: back to
`pending`, `run_at` after a backoff of 30 s doubling up to 300 s, `adapter_state
= held:<code>`. The first deferral records the attempts and the runnable time
the work had left (`payload['hold']`), and every deferral restores them: the
claim is given back, and so is an attempt used meanwhile by a transient read or
a worker lost mid-check, and `deadline_at` is set that runnable time past the
new `run_at` - a week of re-checks never drains the timeout or the attempts.
Each re-claim asks the gate again; once AI is available the adapter closes the
hold (`end_hold`) and the work runs - a later hold starts its own bound. Held
work is `running` for each re-check (`ai_hold.held_execution_conditions`). The
recovery sweeps know a held row
too: `reclaim_expired_leases` gives a lost re-check's claim back (the saved
attempts), and `sweep_deadlines` gives a held row its runnable budget back
instead of dead-lettering it while the hold is within its bound (a worker down
for longer than the budget is not runnable time). A hold
lasting `AI_UNAVAILABLE_HOLD_MAX_SECONDS` ends it: the adapter runs the body
without holding and the domain row is failed with the reason. A transient read
failure during a hold still takes the retry path above, and keeps the hold's
start; consecutive ones still end in `dead_letter`.

## Lease races

`complete` / `fail` / `await_event` / `extend_lease` re-read the row `FOR
UPDATE` and verify ownership. If another transaction reclaimed it, the caller
gets `LeaseLostError` and bails — it can never overwrite the new owner's state.

The re-read must see the row as it is: sessions are `expire_on_commit=False`,
and the ORM hands back an instance the session already holds untouched. So
every transition that decides on the row's state or lease ownership and whose
caller may already hold the row re-reads with `refresh`, which adds
`populate_existing`: `complete` / `fail` / `await_event`
(the reconciler applies them on the session that loaded the row before the
adapter ran), `rearm_for_resume` (the outbox handler read the row to route the
event; a stale copy would revive a cancelled run or steal a live lease) and
`cancel_execution` (its callers may look the execution up first; a stale copy
would cancel over a row that already completed). The heartbeat opens a fresh
session per beat, so `extend_lease` needs no refresh. `reset_execution` also
preloads then locks, but it overwrites every mutable field unconditionally and
reads none to decide, so it too keeps the plain `_locked_get`.

## Redis / wake outage

The Redis "wake" pub/sub is best-effort. If it is unavailable, the reconciler
falls back to its poll interval (`reconciler_interval_seconds`, default 2s) — at
most a couple seconds of added dispatch latency, never a lost job (state is in
Postgres).

## Operator recovery

- `POST /api/v1/assignments/runs/cleanup-stale` drives `sweep_deadlines` +
  `reclaim_expired_leases` for the assignment adapter on demand.
- `requeue_execution` resets a `failed` / `dead_letter` row back to `pending`.
