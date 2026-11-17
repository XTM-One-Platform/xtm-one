# What a calling agent gets back

The delegation tool's result is the only thing the model sees, so this is the
feature's real interface. `009` shipped three shapes; this adds a fourth and one
new way to call.

## The four shapes

| `status` | When | Carries |
| --- | --- | --- |
| `completed` | the partner's task reached a terminal state | `result`, `partner`, `card_provenance`, `protocol_version` |
| `input_required` | the partner asked a question | `question`, `continue_task_id`, optionally `partial_result` |
| `still_running` **(new)** | the attempt ended with the task live | `collect_task_id`, `partner`, and how the platform expects to learn the outcome |
| `failed` | one of `009` FR-029's named classes | `reason`, `detail`, optionally `partner_reason` |

`still_running` is **not** a failure and must not be reachable through the
failure branch. A calling agent that cannot tell it from a timeout will
apologise to a user for work that is still running and will never collect it —
which is the behaviour this feature exists to remove.

Everything partner-authored in any shape goes through the same guard `009`
FR-036 already applies. A still-running result carries no partner text at all
beyond the identity, which is ours.

## Calling to collect

No new tool. The delegation tool already takes `continue_task_id` for answering
a partner's question, and collecting is the same call with nothing to say:

| Fields sent | Meaning |
| --- | --- |
| `message` | a fresh delegation |
| `continue_task_id` + `message` | answering the partner's question |
| `continue_task_id`, no `message` | collect: has this finished? |

A second tool would cost every run bound to a partner the tokens of its
description on every turn, to express what an existing argument already can.

Ownership is `009` FR-046's rule unchanged — the exchange must belong to this
caller — so a collect is refused for a task this run did not create, matched on
integration, target and user, plus the run where the caller has one.

## When the attempt ends

| Condition | The attempt ends at |
| --- | --- |
| the platform will be told the outcome (a callback is registered) | the grace period |
| it will not | the ceiling, as `009` FR-024 already defines it |
| the task reaches a terminal state first | immediately, with the result |

The grace period exists because this platform's own inbound surface answers
every submission before the work is done — it detaches the run and replies
`submitted`/`working`. Without a grace period, a three-second delegation
against it would return `still_running`, cost the calling agent a turn saying
so, and cost a second turn once the answer arrived.

## What does not change

A delegation's result, its failure classes and their reasons are identical
whichever mechanism the platform used to wait (FR-021). The mechanism is
recorded for an operator, and is invisible to the calling agent except through
progress detail and this fourth shape.
