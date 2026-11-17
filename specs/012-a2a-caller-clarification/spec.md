# Feature Specification: An exposed agent asks its caller for a detail

**Feature Branch**: `feat/follow-up-A2A-handover`

**Created**: 2026-09-10

**Status**: Draft

## Context

A2A's `input-required` means a task is blocked on its **client**. The client of
an exposed agent is the calling agent, not a person: it reads the question,
answers it in its own turn, and the task continues. `010` states the rule
already — "`input-required` is reserved for a task blocked on the client".

This platform could not produce it. The only run status mapped to
`input-required` is `awaiting_human`, which belongs to the assignment engine's
human-in-the-loop machinery, and external tasks run the chat path. So an
exposed agent that needed a detail it had not been given could only write the
question into its answer and let the task complete — which reaches the caller
as a result, not a question. Observed live: a task answered with a premise
correction the caller had no way to respond to.

`006` FR-023 refused continuation outright for the same reason, and its
Out-of-scope list recorded it. Both are corrected by this feature; `006`'s
dated Clarifications entry stands as the record of what was decided then.

## User Story - An agent asks rather than guesses (Priority: P1)

An exposed agent given a task it cannot do well without one more detail asks
the caller for it. The calling agent answers from its own context, the task
continues, and neither side involved a person.

**Independent test**: task an exposed agent with something deliberately
underspecified. It reports `input-required` with the question as its status
message; answer with a message naming that task id; the same task reaches
`completed` with an answer that used the detail.

## Requirements

- **FR-001**: An exposed agent MUST be able to ask the caller of its task for a
  detail. The question MUST be addressed to the calling agent — no path here
  may park a run for a person, and the capability MUST NOT be offered outside
  an external task.
- **FR-002**: A task whose run ended by asking MUST report `input-required`
  with the question as its status message, and MUST NOT report a result.
  Anything the agent wrote after asking MUST NOT reach the caller.
- **FR-003**: A message naming a task that is waiting on its caller MUST
  continue **that task** rather than create another. A message naming a task
  the caller does not own, or one that is not waiting, MUST be refused —
  not-found in the first case, so ownership stays unprobeable, and a
  machine-readable reason in the second.
- **FR-004**: A continuation MUST carry the exchange that produced it, so the
  agent answering is the one that remembers asking.
- **FR-005**: The number of times one task may ask MUST be bounded. At the
  bound the capability is withheld and the agent answers with what it has.
- **FR-006**: The task's identity MUST survive the exchange: one delegation is
  one task however many rounds it takes, and its context is unchanged.

## Out of Scope

- **A question a person answers.** Unchanged from `006`: nothing on this
  surface parks a run for human input. An agent that needs a human decision
  cannot ask for one, and the tool says so.
- **The calling side.** `009` FR-031 already answers a partner's question from
  the calling agent's own context; nothing there changes.
- **Asking across a transfer.** The agent that asks is the agent that answers
  the continuation, whoever the context records.

## Assumptions

- The calling agent can answer without a person. Where it cannot, it says so
  as its own answer — this feature adds no way to escalate.
- A partner that is not this platform may never ask; the capability is
  declared by neither side and costs nothing when unused.
