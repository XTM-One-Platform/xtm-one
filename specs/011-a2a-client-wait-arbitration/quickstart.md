# Validating this feature

One deployment, federated with itself: an inbound registration whose agents are
the partner and an outbound registration that reaches them.
[../../dev-docs/a2a-review-scenarios.md](../../dev-docs/a2a-review-scenarios.md)
says how to start it.

Restart it before validating — it does not reload, and a migration applied
under a running instance breaks its stale ORM.

## Before you start

The failure this feature removes is reproducible today. In chat, ask an agent
bound to the outbound registration to call its
`delegate_to_*_aev_detection_rules_generator` tool with a malware description
and "generate CrowdStrike IOA detection rules".
That chain took 631 seconds when measured; the ceiling is 300. Expect the
calling agent to be told the delegation timed out, expect no row in
`a2a_delegations`, and expect the partner to finish the work anyway with nobody
listening. Everything below is a departure from that.

## 1. A short delegation still costs one turn

Delegate something the partner answers quickly. The calling agent should receive
a result in the same turn — no "I've started that" message, no second turn.

This is the regression guard for the grace period. A2A submissions answer
before the work is done, so a hand-back with no grace turns every short
delegation into a two-turn exchange (SC-008).

## 2. A long delegation is handed back, not failed

Re-run the detection-rule chain from above. Expect:

- a `still_running` result naming the task — **not** `failed`, and not a timeout
- no cancellation at A: the task keeps running, checkable in `a2a_tasks` on
  `copilot`
- a row in `a2a_delegations` while the task is outstanding, with
  a state and no outcome

Then collect it on a later turn and expect the answer. Ask it to collect a task
identifier it never created and expect a refusal (FR-003).

## 3. The turn ending does not cancel

After the hand-back in §2, let the chat turn end. Expect the partner task to
keep running.

Cancelling at turn end would kill every handed-back task seconds after creating
it, since handing back is what ends the turn. So stopping it is something the
user asks for: the registration carries one `cancel_task_at_<partner>` tool,
the hand-back tells the agent the work continues and can be stopped, and the
agent calls it when asked. Ask on a later turn and expect `cancel_requested_at`
set on `copilot`'s `a2a_tasks` row. Naming a task id from another conversation
cancels nothing and answers as a finished task does.

The conversation-level cascade still reaches these tasks — pressing Stop on a
later turn cancels what the conversation left outstanding — but there is no
control that appears on its own once a turn has ended.

## 4. What the partner declares is recorded

Look at the outbound registration's verification card for a partner agent. Callback support should be
shown alongside skills and content types.

Then change the exposed agent's card so it stops declaring callbacks, and let verification
sweep run. Expect the recorded capability to change and **the target to stay
usable** — no suspension, no operator acceptance prompt. Suspension is for a
changed endpoint or content type; a capability changes how the caller waits, not what it
may send (FR-009).

## 5. Progress is visible before the answer

Delegate to the exposed detection-rule chain again and watch the caller's side. Each hand-off in
A's pipeline should be visible to the calling agent before the task finishes —
`010` made the partner publish them; this is the caller learning to listen.

Compare against a poll: a poll shows only "still working", which is why a
631-second pipeline and a stalled one look identical without this.

## 6. A callback resumes the caller

**From an assignment**: schedule an assignment on a family-2 agent that delegates the long
chain. Expect its run to park rather than block — no lease held — and to resume
when A's callback arrives, completing with the answer. The agent never sees a
still-running outcome on this path; its run suspends and wakes.

**From chat**: the same delegation from a conversation. Expect the hand-back
from §2, and then the conversation to be woken with the answer when A finishes,
without anyone asking.

Then, with a callback registered, block it — expect the polling floor to
conclude the delegation regardless (FR-018, SC-003).

## 7. Nothing concludes twice

Let a callback and the polling floor race on the same delegation. Exactly one
conclusion, one result into the calling run, one row transition out of
outstanding (FR-019, SC-005).

## What to check in the databases

| Question | Where |
| --- | --- |
| Was the task cancelled? | `copilot.a2a_tasks.cancel_requested_at` |
| Is the delegation outstanding? | `a2a_delegations` — state set, outcome NULL |
| How did it wait? | `a2a_delegations.mechanism` |
| What does the partner declare? | `a2a_client_targets` |

Access is `docker compose -f docker-compose.yml -f docker-compose.dev.yml exec postgres psql -U copilot -d <db>`.
