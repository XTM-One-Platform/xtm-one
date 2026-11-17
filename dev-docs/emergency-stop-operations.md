# Emergency stop — operations

The emergency stop ("panic button") halts every agentic activity at once. This
page is for operators: what it does, how to press it with and without a
session, what it leaves behind, and how to lift it. The end-user procedure
that administrators print from **Settings > Security > Emergency stop** is generated in the
platform's UI language (English, French, Spanish, Japanese); this page is the
operator's reference behind it.

## Scopes

| Scope | Who | What is stopped |
|---|---|---|
| `self` | every user | their active runs and run trees (children, sub-agent tasks, durable executions, A2A partners), their in-flight chat turns, their flow runs, their pending runner commands; the assignments they own or that run as them are paused |
| `platform` | administrators | the same over every user, plus evaluation and autonomous executions, plus the `ai_disabled` kill switch so nothing new starts |

Document processing (SAQ) and knowledge-base sync are not agentic and are not
touched. A `dispatched` runner command is already executing on the host and
cannot be recalled; `pending` and `pending_approval` ones are cancelled.

## Pressing it

**From the interface.** Profile menu → *Emergency stop*. Administrators pick
the scope; everyone retypes a random 6-character code before the red button
enables. The stop runs immediately; a *why?* form follows and annotates the
audit row and the paused assignments.

**From any terminal (no session).** An administrator generates the emergency
API token in *Settings > Security > Emergency stop* (shown once, stored as a bcrypt hash) and
prints the procedure. The token does exactly one thing:

```bash
curl -X POST https://<xtm-one>/api/v1/panic/emergency \
  -H "Authorization: Bearer xes-…" \
  -H "Content-Type: application/json" \
  -d '{"reason": "<why you are stopping everything>"}'
```

Always the platform scope, attributed to the administrator who minted the
token (falls back to the oldest active administrator, then — with no
active administrator left — to the minter or the oldest administrator even
when deactivated: a valid token is never refused for want of an actor; see
`_token_actor`). A wrong or missing
token answers `401` and writes a `panic_token_rejected` security event under
the token's owner. Rotate the token whenever a printed copy is lost.

## What it leaves behind

- Audit rows `panic_stop` / `panic_resume` with per-kind counts, the reason,
  and `details.via = "api_token"` for the token path; `panic_token_generated`
  / `panic_token_revoked` for the credential.
- Every paused assignment carries `paused_reason = "panic_stop"` and shows the
  red **Emergency stop** badge instead of *Paused*; its detail page explains
  who, when and why.
- Cancelled runs carry `error = "Stopped by an emergency stop."`.
- A red banner on every page while a stop is in effect (`GET /api/v1/panic/status`
  → `active`, `last_stop`), with a direct **Resume** button.

## Lifting it

Profile menu or banner → *Resume*. A reason is mandatory. Only assignments
stamped `panic_stop` are re-enabled — a manual pause stays manual; a platform
resume takes only those a platform stop paused (`paused_at` is one of the
platform stops' `stopped_at`), so a user's own stop survives it — and, for the
platform scope, the AI override goes back to what it was before the stop (see
below; never forced on). Cancelled work stays cancelled: a resume re-arms
schedules, it does not resurrect runs. A restart during the stop keeps it:
startup seeding leaves a built-in assignment stamped `panic_stop` paused
(`is_emergency_stopped`), and the AI override is stored.

## Two phases, one guarantee

`POST /panic/stop` runs `arm_admission_barrier` (pause the assignments in
scope, flip `ai_disabled` for the platform, write the `panic_stop` audit row)
and COMMITS, invalidates the local AI cache, then runs `emergency_stop`
(cancel everything live, complete the audit row's counts) and commits again.
Once the first commit lands, every admission check made AFTER it — on any
replica — sees the paused assignment and the refused AI gate. A scheduler that
had read `enabled = True` (or a still-warm AI cache, up to 5 s) BEFORE that
commit can still insert one last run afterwards, so the second phase's SELECT
is not guaranteed complete on its own: `sweep_late_admissions` runs after the
second commit for exactly those pre-commit reads and reports what it caught in
`late_admissions_cancelled` and on the audit row — then runs ONCE MORE, detached,
`ai_state.CACHE_TTL_SECONDS + 1` seconds later, because the AI-gate cache is
process-local (no cross-replica invalidation, by design) and another replica may
admit work for up to one TTL after the first commit; that second pass counts
into the audit row only (the response is already out) and is skipped when a
resume in the same scope landed meanwhile — runs a lift legitimately started
are not cancelled. A `self` stop does not gate new work, so its second pass
(`_self_stop_later_pass_filter`) only cancels a run created before the
first pass started (`first_swept_at`), a run of an assignment this stop paused,
or a child of a run this stop cancelled: a run the user starts after the stop
(a chat turn consulting another agent) is theirs. The `platform` second pass
keeps the unbounded sweep. For the `platform` scope both passes also cancel every
pending / running background task and every chat turn still holding a marker
(`_sweep_platform_live_work`): with AI off nothing agentic may be live, and a
task a late turn spawned would otherwise be held by the AI hold and run
at the resume. A `self` stop does not gate new work, so it leaves them alone.
In both scopes they also cancel a sub-flow a cancelled flow run started as the
stop landed (`_sweep_orphaned_sub_flows`: an active flow run created since the
stop whose parent run is cancelled, followed down the chain), since a
`flow.call` node already running can insert its child after the stop's
selection. The partner cancels, conversation cascades, flow-run frames
and runner notifications run immediately in `after_commit`, never behind that
delay. The token path does the
same two phases. Writing the platform stop's state and the audit row in phase 1
is what keeps `platform_stop_open` true (banner, resume) and the reason
recordable if phase 2 fails after phase 1 committed.

Both transitions — `arm_admission_barrier` and `resume_after_emergency_stop` —
start by taking `pg_advisory_xact_lock(hashtext('xtm_one:emergency_stop'))`, so
a `self` resume's "is a platform stop open?" check and its re-enables cannot
straddle a platform stop being armed on another request. A `self` resume is
refused (409 `platform_stop_active`) while a platform stop is open: those
assignments are the platform stop's boundary.

The platform resume restores the AI switch to what it was BEFORE the stop
(`platform_stop_ai_override`, recorded by the first stop of the open sequence:
on / off / deployment default; the barrier's audit row repeats it as
`ai_override_before_stop`), and only if the switch still reads as the stop left
it — a change made in Settings during the stop is kept.

"A platform stop is in effect" is stored on `PlatformSettings`
(`platform_stop_at`, set by the first platform barrier, cleared by the platform
resume; `platform_stop_times` lists the `stopped_at` of every platform stop
pressed meanwhile, the stamps the resume re-enables). It is not derived from
the audit trail: `audit_logs` rows cascade with the user who pressed the stop
(the token path files them under the administrator who minted the token), and
the stop's effects must not outlive their record. With the open stop's
rows gone, the banner still shows the stop, with its time and no author. It is
never derived from the raw `ai_disabled` bit either: an administrator's own
Settings override is not an emergency, is not reported as one, and is not
cleared by a resume. A `self` stop is still read from the trail: its rows go
only with the user whose scope it is.

`POST /panic/emergency` is rate-limited per client IP before the token is
checked (`PANIC_EMERGENCY_RATE_LIMIT` / `PANIC_EMERGENCY_RATE_WINDOW_SECONDS`,
10 per 600 s, fail-closed). Past the ceiling, or when Redis cannot answer, a
request carrying the current token's display prefix (`xes-` + 8 hex digits,
admin-visible only) is still checked (`carries_current_token_prefix`): a shared
egress or a proxy that hides the client must not lock the real token out, and
a guess, which lacks those 32 bits, still never reaches bcrypt.

## Latency

Runs are marked `cancelled` synchronously. An in-flight agent loop notices
through the reconciler's fast cancel watcher (`orchestration_cancel_poll_seconds`,
default 2 s) on the assignment, sub-agent, flow and evaluation adapters, and
through the Redis cross-process cancel for chat turns (1 s). The lease heartbeat
(~20 s) remains the backstop.

## Routes

| Route | Auth | Purpose |
|---|---|---|
| `POST /api/v1/panic/stop` | session | stop (`scope: self \| platform`) |
| `POST /api/v1/panic/stop/{audit_id}/reason` | session (the actor) | record why, after the fact |
| `POST /api/v1/panic/resume` | session | lift (mandatory `reason`) |
| `GET /api/v1/panic/status` | session | counts, `active`, `last_stop` |
| `GET/POST/DELETE /api/v1/panic/token` | admin | status / generate-rotate / revoke the API token |
| `POST /api/v1/panic/emergency` | bearer `xes-…` token | platform stop without a session |

None of these routes sit behind the AI gate: the stop is what switches AI off.
