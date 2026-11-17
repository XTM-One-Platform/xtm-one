# Contract: The delegation tool (agent-facing)

**Feature**: [spec.md](../spec.md) | Serves FR-021, FR-022, FR-026–FR-031, FR-046–FR-049

The interface an XTM One agent sees. Contributed through `PROVIDER_TOOLS` → `build_tools(token, executor, *, refresh_fn=None, **_kwargs)`, the same seam every other integration uses, so it appears in a run exactly when the partner's integration is bound to the agent and never otherwise.

One tool, not two. A continuation is the same tool called again with a task id — not a separate `continue_delegation` — because a second tool would be reachable only in a state the model has to be told about, and the inbound phase's review is explicit about what happens to paths the implementation can reach but nothing exercises.

## Tool: `delegate_to_<registration name>_<declared agent name>`

Named per partner agent (FR-059): a registration reaching three of the partner's agents contributes three tools, each carrying that agent's declared skills and card provenance in its own description. Two agents legitimately declaring one name are disambiguated with a short digest of the card URL. This keeps the choice of partner agent in the tool name, where the model cannot get it wrong by hallucinating an id.

### Arguments

| Argument | Type | Required | Meaning |
|---|---|---|---|
| `message` | string | yes | The work to hand the partner. This is the **only** thing that leaves the platform (FR-032) — the calling run's history is never appended |
| `continue_task_id` | string | no | Present only when answering a partner that reported `input-required`. Must name a task this run created (FR-046) |

No `partner`, no `url`, no `version`, no `timeout` argument. Every one of those is registration state or a platform bound, and exposing any of them would let a model's output influence a destination or a limit.

### Returns — reached a terminal state

```json
{
  "status": "completed",
  "result": "<partner's text and/or structured result>",
  "partner": "<registration name>",
  "card_provenance": "verified" | "unsigned",
  "protocol_version": "<version actually used>"
}
```

`card_provenance` is on every successful return, not just unverified ones. FR-017 requires the distinction legible *where the risk is taken*, and a field that appears only in the risky case is one a reader learns to stop looking for.

### Returns — partner needs more input

```json
{
  "status": "input_required",
  "question": "<the partner's question, prompt-guarded>",
  "continue_task_id": "<partner task id>",
  "partner": "<registration name>",
  "card_provenance": "verified" | "unsigned"
}
```

The model may answer by calling the tool again with `continue_task_id`, or stop and report. Nothing waits on a human (FR-031). `question` has passed `prompt_guard` — it is partner-controlled text inviting a reply, which makes it this feature's most direct injection vector (FR-047).

### Returns — failure

```json
{
  "status": "failed",
  "reason": "unreachable" | "credential_rejected" | "partner_failed"
             | "timeout" | "content_type_mismatch" | "refused_preflight",
  "detail": "<partner's machine-readable reason, or the pre-flight refusal>",
  "partner": "<registration name>"
}
```

The five FR-029 classes are distinguishable in `reason`; `refused_preflight` covers everything refused before a request was sent. No `status: "error"` catch-all exists — an undifferentiated failure is what SC-009 counts as a defect.

## Behaviour the tool guarantees

1. **Pre-flight before any network call**, in the order given in [data-model.md](../data-model.md#validation-rules): switch, binding, usability, circuit, verdict freshness, accepted card, content types, origin. A refusal here never reaches the partner and never counts toward the breaker unless the cause is durable.
2. **Polls to a terminal state or `input-required`.** Polling uses the platform's existing wait primitive, which deliberately does not consume a loop iteration — so a slow partner cannot spend the run's reasoning budget on polls.
3. **Bounded per attempt** by the per-delegation ceiling (default 5 minutes) or whatever remains of the run's deadline, whichever is shorter. On expiry the partner task is cancelled and a `timeout` is returned (FR-024).
4. **Cancels outward on the way down.** If the calling run is cancelled or fails with a task outstanding, the platform attempts cancellation at the partner (FR-025) — including a task left in `input-required` when the model stops answering.
5. **Continuations are ownership-checked.** A `continue_task_id` naming another run's delegation is refused as `refused_preflight`, because that id arrives inside partner-controlled content (FR-046).
6. **Turn-farming is bounded, not blocked.** Repeated `input_required` rounds are limited only by the run's existing iteration, token, spend and wall-clock budgets; `continuation_count` records how many happened (FR-048).

## What the tool cannot do

Stated because these are the properties tests assert, not aspirations:

- Send anything the calling run could not itself see; a registration confers no extra reach inward (FR-033).
- Reach an origin other than `pinned_origin`, or follow a redirect away from it (FR-034).
- Include a platform credential, API key, session material or cross-platform identity token in a payload (FR-032).
- Change registration, credential, binding or permission state — whatever the returned content asks for (FR-036).
- Consume another user's usage allowance; accounting follows the calling run (FR-037).
