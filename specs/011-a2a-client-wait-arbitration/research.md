# Research

Phase 0 for [spec.md](./spec.md). Every decision below was checked against the
code, and the checks that changed a decision are named.

## R1 — When an attempt stops waiting

**Decision**: The attempt ends at a short grace period where a callback is
registered, and at the existing ceiling where one is not. A live task at that
point produces a still-running outcome, not a timeout, and nothing is cancelled.

**Rationale**: `009` FR-024 makes every exceeded ceiling a timeout that cancels
at the partner. That is what discarded a completed 631-second result. The
ceiling is not the problem — waiting is — and the grace period exists because
this platform's own inbound surface detaches the run
(`a2a_server.py:1142`, `asyncio.create_task`) and answers `submitted`/`working`
before any work is done. Every delegation against it therefore returns
non-terminal at submission, so a hand-back with no grace would turn a
three-second delegation into two agent turns.

**Alternatives considered**: hand back on the first non-terminal response
(rejected — the two-turn regression above); keep waiting the full ceiling even
with a callback registered (rejected — reintroduces the wall the feature
removes); derive a grace period from the partner's observed history (rejected —
a moving target, and `009` rejected the same shape of idea for the ceiling).

## R2 — How a calling agent collects

**Decision**: No new tool. The delegation tool already accepts
`continue_task_id`; collecting is that argument with no `message`.

**Rationale**: a tool's description is serialised into the model's schema on
every turn of every run bound to that partner (`tool.py:102`), so a second tool
is a permanent context cost to express what an existing argument already can.
Ownership is `009` FR-046's rule unchanged — `_may_continue`
(`delegation.py:865`) matches integration, target, user and, where the caller
has one, the run.

**Alternatives considered**: a dedicated `collect_partner_task` tool (rejected —
above); reusing the existing `collect_task_results` builtin (rejected — it
merges structured items from *local* background tasks into a session variable,
a different job).

## R3 — Recording what a partner declares

**Decision**: Add callback support to the projected card, record it on the
target, and **keep it out of the card fingerprint**.

**Rationale**: `project_card` (`sdk.py:239`) already reads `capabilities` but
only for `protocolVersions` and the partner-discovery extension —
`pushNotifications` is never read on the outbound side. Adding it is one
`PartnerCard` field and one line in the projection, written by the single
writer `apply_verdict` (`verification.py:563`).

The fingerprint decision is the load-bearing one. `fingerprint_card`
(`verification.py:105`) hashes exactly five material fields, and a change to any
of them suspends the target until an operator accepts it. Adding capabilities
there would re-fingerprint **every existing registration** and suspend them all
on the next sweep. The function's own docstring gives the reason not to:
suspending for a cosmetic change "would train an operator to click accept
without reading". A capability changes how the platform waits, not what it may
send — which is FR-009, and it now has a mechanical justification as well as a
principled one.

**Alternatives considered**: probing a partner for a capability (rejected — the
card is the contract, and `009` refuses to infer); treating a lost capability as
material (rejected — mass suspension, above).

## R4 — Deciding whether a partner can reach us

**Decision**: An explicit operator setting; where unset, derive from the
configured public address, treating anything `009` FR-035 would refuse —
loopback, private, link-local, cloud-metadata — as unreachable.

**Rationale**: `base_url` exists (`config.py:943`) but a configured address is
not evidence of reachability; a self-hosted deployment behind a firewall has one
too. The derivation reuses `assert_allowed_async`, so the dev and self-hosted
cases that matter most are already excluded by a guard the codebase trusts. The
explicit setting exists because registering a callback tells a partner where this
deployment lives — in an outbound-only relationship, an address it would
otherwise never learn.

Getting it wrong is benign: the partner exhausts its delivery attempts and the
polling floor concludes the delegation regardless.

**Alternatives considered**: derive only (rejected — no way for an operator to
refuse disclosure); explicit only, default off (rejected — most deployments
would never turn it on, so callbacks would ship dark); learn empirically (rejected —
wastes a registration per partner to discover a deployment-wide fact).

## R6 — Resuming the calling run

**Decision**: Two mechanisms, both existing and neither new. A chat conversation
is woken by the mechanism that already wakes it when background work finishes.
An assignment run is **parked** at the delegation and resumed by the
orchestration engine.

**Rationale**: this is where the check changed the design. The chat half is
nearly free — `parent_wake` is already duck-typed (`should_wake_parent` reads
its fields with `getattr`), its prompt builder takes only scalars, and the run
turn is entirely work-agnostic. Three edits couple it to `SubAgentTask`: the row
lookup, the two result/error attribute reads, and a tool name baked into the
truncation branch of the prompt.

The assignment half is not free, and not what it first looks like. **Nothing
currently parks a run on an outbound delegation** — the delegation blocks
in-process with a deadline. Resuming a run that already finished is meaningless,
so the assignment path must park instead of handing back: `await_event` keeps
the execution `running` while releasing its lease, and `resume_assignment_run`
(`assignment_engine.py:5377`) needs only the run's id. That is precisely the
"hold no resource while waiting" property push is supposed to buy, and it is
modelled on the human-input park that already exists.

The consequence is deliberate and permitted by FR-021, which excepts the
still-running outcome: an assignment run's agent never sees one, because its run
suspends and resumes with the answer; a chat agent does, because a conversation
cannot suspend. Park with a deadline rather than forever, and let the polling
floor cover a callback that never arrives.

This is not the parking `009` rejected. That rejection — recorded in
`test_a2a_client_continuation.py` — was about parking a run so a **human**
could answer a partner's question, "a different feature needing a UX surface
that does not exist". Parking to wait for a machine callback needs no surface
at all, and the answer arrives on its own.

**Alternatives considered**: hand back on the assignment path too (rejected —
the run completes, and a later callback has nothing to resume); a new
resumption mechanism shared by both (rejected — two run paths genuinely resume
differently, and inventing a third is the parallel infrastructure this plan
exists to avoid).

## R7 — Receiving a callback

**Decision**: A token-in-path route authenticated by a per-delegation secret in
constant time, correlated through the existing delegation record, concluding at
most once.

**Rationale**: the correlation record already exists — `A2ADelegation` carries
`partner_task_id`, `run_ref`, `agent_id` and `user_id`, which is the whole path
from an inbound notification back to the work that should resume. The route
pattern exists too: the anonymous card route (`a2a_server.py:185`) is
token-in-path behind a pre-auth rate limit and a transport check, and the webhook
receivers authenticate with constant-time secret comparison
(`webhooks.py:1312`) **before** consulting their idempotency cache.

What we send when we are the server is the shape to expect when we are the
client: `{taskId, contextId, state}` plus an optional echoed token, with no
result content (`push.py:250`) — so a callback is an invitation to read the
task, never the answer itself. FR-019's conclude-once is a claim on the
delegation row, the same shape as the claim the inbound side already uses to
stop two replicas notifying twice.

**Alternatives considered**: platform-session auth like the autonomous `/wake`
endpoint (rejected — a partner has no platform session); no authentication,
relying on an unguessable task id (rejected — Principle II, and a task id is
disclosed to the partner by definition).

## R8 — When the delegation record is written

**Decision**: Write it when the task is handed back, not only at conclusion.

**Rationale**: today the row is created by `_conclude` — a delegation in flight
has no row at all, which is why yesterday's two live delegations were invisible
in `a2a_delegations`. A collect needs the row for its ownership check, the
cancel cascade needs it to find what to cancel, and a callback needs it to find
the run to resume. All three require the row to exist while the task is
outstanding.

Outstanding is expressed as the absence of a conclusion — no `outcome`, no
`terminal_at` — rather than as a state column. The migrated schema has no such
column; the one in the dev databases is a leftover from an older model, created
by `create_all` rather than by any migration, and it is NOT NULL, so on those
databases every delegation insert has been failing into the recorder's
best-effort savepoint.

**Alternatives considered**: a separate outstanding-task table (rejected — the
delegation record is already the correlation point, and a second one could
disagree with it).
