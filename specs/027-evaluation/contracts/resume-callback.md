# Contract: Resume callback

> **Frozen design record of the 1780 delivery (2026-10-30). Not maintained**: the code has moved on since. The current contract is `dev-docs/reference/evaluations.md`; the design is `dev-docs/evaluations.md`.

The signed call an external system makes to finish an entry that paused for a callback (FR-032). The route is `resume_evaluation_episode` in `backend/app/api/v1/evaluations.py`. The episode logic is `resume_from_callback` and `process_resume_episode` in `backend/app/services/evaluation_continuation.py`.

## Enabling

Callback mode is set through the API in `evaluations.trigger_config`:

| Key | Meaning |
|---|---|
| `await_callback` | `true` makes every entry pause after the agent's first turn |
| `resume_secret` | the HMAC key. Write-only: stored Fernet-encrypted as `resume_secret_enc`, served as `"***"`, never exported. An update that omits it or sends `"***"` keeps it; `null` or `""` removes it; a JSON number is stored as its digits; any other value that is not text, or a vault reference, is a 422. A stored clear secret is encrypted by the migration `e1780evp001`. |
| `callback_timeout_seconds` | how long an entry waits; defaults to the evaluation's `timeout_seconds` |

- Each entry has one episode. Its `correlation_id` is `eval:<run_id>:<entry_id>`.
- While paused, the episode is `waiting_callback` with a `callback_deadline`.
- Reuse is off in callback mode (FR-093).
- With no secret set, a trigger, replay or re-run is refused with a 400: no callback could resume its entries.

## Request

```
POST /api/v1/evaluations/resume
X-Signature: <lowercase hex HMAC-SHA256(key = resume_secret, message = raw request body)>

{ "correlation_id":   str (1–120),
  "idempotency_key":  str (1–120),
  "callback_payload": { "final_output"?: str, "agent_output"?: str, "error"?: str } | null }
```

- There is no JWT and no AI gate: the episode was already dispatched.
- The signature is compared in constant time.

## Responses

Checks run in this order:

| Status | Body | When |
|---|---|---|
| 404 | | no episode has this `correlation_id`, or its run is gone |
| 401 | | the stored secret cannot be read: written under another `SECRET_KEY`, not a string, or a vault reference |
| 400 | | no `resume_secret` is configured |
| 401 | | `X-Signature` is missing or wrong |
| 410 | detail names the episode and its status | the episode was closed before any callback was applied. The payload is **not** applied and this is **not** a duplicate. |
| 200 | `{accepted: true, duplicate: true, episode_id, status}` | the same `idempotency_key` was already applied; or the episode is `completed`; or it was closed after an earlier callback was applied. Nothing is enqueued. |
| 200 | `{accepted: true, duplicate: false, episode_id, status}` | accepted (below) |
| 422 | | the body fails validation |

## Acceptance and idempotency

- Accepting a callback does all of the following in **one transaction**, then wakes the worker:
  - stores `idempotency_key` and `callback_payload`;
  - sets the episode to `resumed`;
  - clears its deadline;
  - creates one orchestration execution `{adapter: "evaluation", payload: {kind: "resume_episode", episode_id, idempotency_key}}`, with deadline = evaluation timeout + orchestration grace.
- `idempotency_key` is both the duplicate key and a generation token.
  - A repeat with the same key is a duplicate.
  - A different key on an episode still `resumed` is a new generation: it is accepted and gets its own execution.
  - The key tells concurrent executions apart, so an older execution's failure handling never closes a newer callback's episode.

## Processing the payload (`process_resume_episode`)

| Payload | Effect |
|---|---|
| `error` is a non-blank string | episode `failed` with that error |
| `final_output` (else `agent_output`) is a non-blank string | the result is written with that output and scored; episode `completed` |
| neither | the payload is kept; the episode stays `resumed` |

- Once every episode of a run is terminal, the run ends `failed` if any episode failed or timed out, else `completed` (FR-037).
- An answer is stored, judged, then its episode finalized, in three transactions: the judge runs with no lock on the run. A new result reads as a judge failure until its verdict is stored. An episode a cancel or the emergency stop closes meanwhile keeps its closed state, and its answer is not judged when the closure comes before the judge call. A callback with a new key accepted while an answer is judged replaces it: that verdict, or its scoring failure, is dropped and the newer callback's resume finalizes the episode with its own answer. A newer callback carrying neither an output nor an error replaces the payload all the same: the episode then stays `resumed`, with no deadline, until a later callback.
- The adapter's periodic tick closes overdue `waiting_callback` episodes as `timed_out`, with the error "Callback deadline exceeded". It skips a run another transaction holds; that run's episodes are closed on a later tick.
