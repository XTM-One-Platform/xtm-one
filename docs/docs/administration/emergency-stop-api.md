# Emergency stop API

Everything the [emergency stop](emergency-stop.md) does in the interface is
available over REST, plus one route that needs no session at all — the one a
printed runbook or a monitoring hook uses. This page describes each route, its
inputs, its responses and its errors.

All routes live under `/api/v1/panic`. None of them is affected by the AI
switch: the stop is what switches AI off, so it must keep answering while AI is
off.

In the examples, `<your-xtm-one>` is the platform's full base address. When a
reverse proxy serves XTM One under a path (`BASE_PATH`, see
[Serving under a path prefix](../deployment/installation.md#serving-under-a-path-prefix)), that path is part of it, for
example `https://intranet.example.com/xtm-one/api/v1/panic/emergency`. The
command shown in `Settings` → `Security` → `Emergency stop` and on the printed procedure already
includes it.

## Authentication

Two credentials exist, for two situations:

| Credential | Header | Used by | What it may do |
|---|---|---|---|
| A session token or a personal API key (`fcp-…`, created in [`My profile` → `API Keys`](../usage/my-profile.md#api-keys-tab)) | `Authorization: Bearer <token>` | every route except `POST /panic/emergency` | what the signed-in user may do: their own scope for everyone, the whole platform and the token management for administrators |
| The emergency stop token (`xes-…`, generated in `Settings` → `Security` → `Emergency stop`) | `Authorization: Bearer xes-…` | `POST /panic/emergency` only | press the platform-wide stop — nothing else |

## Routes at a glance

| Method and path | Who | Purpose |
|---|---|---|
| `POST /panic/stop` | any user; `platform` scope needs an administrator | press the stop |
| `POST /panic/stop/{audit_id}/reason` | the user who pressed that stop | record why, after the fact |
| `POST /panic/resume` | any user; `platform` scope needs an administrator | lift the stop, with a mandatory reason |
| `GET /panic/status` | any user | what is paused, whether a stop is in effect, who pressed the last one |
| `GET /panic/token` | administrator | whether an emergency token exists (never the token itself) |
| `POST /panic/token` | administrator | generate or rotate the emergency token — returned once |
| `DELETE /panic/token` | administrator | revoke the emergency token |
| `POST /panic/emergency` | the emergency token | press the platform-wide stop without a session |

## Press the stop

```bash
curl -X POST https://<your-xtm-one>/api/v1/panic/stop \
  -H "Authorization: Bearer fcp-..." -H "Content-Type: application/json" \
  -d '{"scope": "platform"}'
```

| Field | Values | Meaning |
|---|---|---|
| `scope` | `self` (default), `platform` | `self` stops everything that runs for you; `platform` stops every user's activity, evaluation and autonomous runs, and switches AI off. `platform` is refused with `403` for a non-administrator |

The stop runs immediately and the response says what it did:

```json
{
  "scope": "platform",
  "runs_cancelled": 3,
  "executions_cancelled": 4,
  "sub_agent_tasks_cancelled": 1,
  "chat_turns_cancelled": 0,
  "flow_runs_cancelled": 0,
  "runner_commands_cancelled": 0,
  "evaluation_runs_cancelled": 0,
  "autonomous_runs_cancelled": 0,
  "assignments_paused": 12,
  "ai_disabled": true,
  "late_admissions_cancelled": 0,
  "completed": true,
  "audit_id": "5f0c6d0a-2c8f-4a4e-9a35-1a6c9d2e7b10",
  "stopped_at": "2026-09-18T16:43:04.628632+00:00"
}
```

| Field | Meaning |
|---|---|
| `runs_cancelled` | runs stopped — those in progress, queued or waiting for a human, the runs they delegated to, and any run admitted while the stop was being applied |
| `executions_cancelled` | background executions released so no worker resumes them |
| `sub_agent_tasks_cancelled` | background tasks delegated to other agents |
| `chat_turns_cancelled` | chat answers being generated that were asked to stop |
| `flow_runs_cancelled` | flow runs in progress |
| `runner_commands_cancelled` | runner commands not yet dispatched to a machine. One already dispatched cannot be recalled, but it is never sent again |
| `evaluation_runs_cancelled`, `autonomous_runs_cancelled` | platform scope only |
| `assignments_paused` | assignments whose schedules and triggers no longer fire |
| `ai_disabled` | `true` when the platform AI switch was turned off (platform scope) |
| `late_admissions_cancelled` | runs a scheduler on another replica admitted while the stop was being applied, cancelled by the follow-up pass — already counted in `runs_cancelled`, reported apart so you know a second wave happened |
| `completed` | `false` when cancelling the running work failed after the assignments were paused (and AI turned off, for the platform): the follow-up passes cancelled what they reach, the counts are what was really halted, and the log entry is marked incomplete. Look at the server log, then press the stop again if work is still running |
| `audit_id` | the log entry of this stop — pass it to record the reason |
| `stopped_at` | when the stop took effect |

Pressing the stop twice is safe: the second press finds nothing left to cancel
and reports zeros.

## Record why, after the stop

The stop never waits for a justification. Once it has run, attach the reason
to the entry it returned:

```bash
curl -X POST https://<your-xtm-one>/api/v1/panic/stop/5f0c6d0a-2c8f-4a4e-9a35-1a6c9d2e7b10/reason \
  -H "Authorization: Bearer fcp-..." -H "Content-Type: application/json" \
  -d '{"reason": "Agent looping on the ticket queue, costs climbing fast."}'
```

```json
{"audit_id": "5f0c6d0a-2c8f-4a4e-9a35-1a6c9d2e7b10", "assignments_annotated": 12}
```

The reason is written on the stop's log entry and on every assignment that
stop paused, where the assignment page and the banner show it. Only the user
who pressed the stop may annotate it (`404` otherwise). A blank reason is
refused with `422`; the maximum length is 2000 characters.

## Lift the stop

```bash
curl -X POST https://<your-xtm-one>/api/v1/panic/resume \
  -H "Authorization: Bearer fcp-..." -H "Content-Type: application/json" \
  -d '{"scope": "platform", "reason": "Loop fixed and deployed, safe to resume."}'
```

| Field | Values | Meaning |
|---|---|---|
| `scope` | `self` (default), `platform` | same rules as the stop |
| `reason` | text, 1–2000 characters, required | recorded on the resume entry and on every assignment re-enabled |

```json
{"scope": "platform", "assignments_resumed": 12, "ai_enabled": true}
```

Only assignments the emergency stop paused are re-enabled; one paused by hand
stays paused. For the platform scope the AI switch goes back to **what it was
before the stop** (on, off, or `Deployment default`), and only if it still reads
as the stop left it — a change made in `Settings` during the stop is kept.
`ai_enabled` reports the **effective** state after that: on a platform started
without AI, or where AI was off before the stop, it stays `false`.

## Check the state

```bash
curl https://<your-xtm-one>/api/v1/panic/status -H "Authorization: Bearer fcp-..."
```

```json
{
  "self_paused_assignments": 2,
  "self_stop_active": false,
  "platform_paused_assignments": 12,
  "ai_disabled": true,
  "platform_stop_active": true,
  "active": true,
  "last_stop": {
    "at": "2026-09-18T16:43:04.628632+00:00",
    "by": "Alex Admin",
    "scope": "platform",
    "reason": "Agent looping on the ticket queue, costs climbing fast."
  }
}
```

| Field | Meaning |
|---|---|
| `self_paused_assignments` | assignments in your scope a `self` resume would re-enable. One that another person's stop still covers (an assignment you own that runs as them, or the reverse) is not counted: it comes back once their stop is lifted too |
| `self_stop_active` | your own stop is in effect and still covers a paused assignment, even one another person's stop also covers. Your resume stays available while it is `true`, even when it would re-enable nothing yet |
| `platform_paused_assignments` | the assignments a `platform` resume would re-enable: the ones a platform-wide stop paused, not a user's own stop — `null` for a non-administrator |
| `ai_disabled` | whether the platform AI switch is off **because of the stop** — `null` for a non-administrator. AI turned off from `Settings` by hand is not reported here and is not touched by a resume |
| `platform_stop_active` | a platform-wide stop is in effect (the latest one has not been lifted). Visible to everyone, including a user who owns nothing the stop paused |
| `active` | a stop is in effect for you: the platform one, or your own |
| `last_stop` | the most recent stop that concerns you — your own or a platform-wide one — with who (their display name; the log entry keeps the email), when, scope and reason (`null` until the reason is recorded) |

The interface polls this route every fifteen seconds to show the banner.

## Manage the emergency token

Administrators only.

```bash
# Is a token configured? (never returns the token)
curl https://<your-xtm-one>/api/v1/panic/token -H "Authorization: Bearer fcp-..."
```

```json
{
  "configured": true,
  "prefix": "xes-<first 8 characters>...",
  "created_at": "2026-09-18T15:02:11.410000+00:00",
  "created_by": "admin@example.com",
  "last_used_at": null
}
```

```bash
# Generate — or rotate — the token. The token is returned exactly once.
curl -X POST https://<your-xtm-one>/api/v1/panic/token -H "Authorization: Bearer fcp-..."
```

```json
{
  "token": "xes-<48 hexadecimal characters>",
  "prefix": "xes-<first 8 characters>...",
  "created_at": "2026-09-18T15:02:11.410000+00:00",
  "created_by": "admin@example.com"
}
```

There is one token per platform: generating a new one invalidates the previous
one immediately. Only a hash is stored, so a lost token cannot be recovered —
rotate it. `DELETE /panic/token` revokes it (`204`; `404` when none is
configured). Generation, rotation and revocation are all recorded in the logs.

## Press the stop without a session

This is the command the printed procedure carries:

```bash
curl -X POST https://<your-xtm-one>/api/v1/panic/emergency \
  -H "Authorization: Bearer xes-..." \
  -H "Content-Type: application/json" \
  -d '{"reason": "Runbook step 3: agent looping on the ticket queue."}'
```

| Field | Meaning |
|---|---|
| `reason` | optional, up to 2000 characters — a runbook usually states the cause, a monitoring hook may not. Recorded on the stop's log entry and on the paused assignments when given |

The response is the same as `POST /panic/stop`. The stop always covers the
whole platform and is attributed to the administrator who generated the token.
If that account is gone, deactivated or no longer an administrator, the stop is
attributed to the oldest active administrator instead. When no administrator is
active at all, a valid token still works and the stop is attributed to the
administrator who generated it, even if that account is deactivated. The
token's `last_used_at` is updated.

A missing or wrong token is refused with `401`, and the attempt is recorded in
the logs as a security event under the token owner's name. The token cannot
call any other route.

The route is rate-limited per client address — ten attempts per ten minutes by
default (`PANIC_EMERGENCY_RATE_LIMIT`, `PANIC_EMERGENCY_RATE_WINDOW_SECONDS`),
counted before the token is checked, so probing it answers `429` with a
`Retry-After` header. The limit fails closed: if the rate limiter cannot be
reached, the route answers `503` rather than becoming unlimited. Neither
refusal applies to a request that carries the current token's prefix (the
`xes-` and the eight characters shown in `Settings`): someone who spent the
limit from the same address, or a rate limiter outage, cannot lock the holder
of the real token out of the stop.

## Errors

| Code | When |
|---|---|
| `401` | no or invalid credential; on `POST /panic/emergency`, a missing or wrong emergency token |
| `403` | `platform` scope, or token management, requested by a non-administrator |
| `404` | recording a reason on a stop that is not yours, or revoking a token when none is configured |
| `409` | re-enabling an assignment the emergency stop paused through `PUT /assignments/{id}` — lift the stop instead (`code: "emergency_stop"`); or a `self` resume while a platform-wide stop is in effect — only an administrator lifting the platform stop resumes it (`code: "platform_stop_active"`) |
| `422` | unknown `scope`, or a missing or blank `reason` where one is required |
| `429` | too many attempts on `POST /panic/emergency` from one address — wait for `Retry-After` |

## What "immediately" means

The stop is two steps, in that order. First the door is closed: every
assignment in scope is paused and, for the platform, AI is switched off — and
this is committed on its own, so every check made after that commit, on any
replica, sees a paused assignment and a refused AI gate and admits nothing.
Then everything live is cancelled. A run already inside a model or tool call
notices within about two seconds through a fast cancel signal; a chat answer
being generated, within one second.

A scheduler that had already read the previous state when the first step
committed can still create one last run right after it. A follow-up pass right
after the stop catches and cancels those, and reports them in
`late_admissions_cancelled` (also in the log entry); a second pass runs a few
seconds later, once no server can still hold a cached "AI is on", and counts
into the log entry only. For your own activity (`self`), that second pass
leaves alone what you started after the stop: it only cancels runs that were
already there, runs of the assignments the stop paused, and runs started by a
run the stop cancelled. For the whole platform, both passes also cancel the
background tasks and chat answers still live: with AI off, anything found then
was admitted by a server whose cache still said "AI is on". Closing the door first is what makes those passes rare
and small; the passes are what make the stop complete.

## Next step

Read [Emergency stop](emergency-stop.md) for the interface, the printed
procedure and what users see while a stop is in effect.
