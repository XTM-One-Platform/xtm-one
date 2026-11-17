# Phase 0 Research: A2A-Compliant Handover

**Date**: 2026-09-07 | **Plan**: [plan.md](./plan.md) | **Spec**: [spec.md](./spec.md)

Six decisions. Each reuses an existing mechanism; the only additions are one
partial index (R1) and one setting (R6).

---

## R1 — A task's state derives from its children by query, over a new partial index

**Decision**: keep a task's state derived (never stored) and answer FR-001 by
asking whether the run has a non-terminal child, adding a partial index on
`sub_agent_tasks.metadata_json->>'parent_run_id'` restricted to
`status IN ('pending','running')`.

**Rationale**: the linkage already exists and is already the canonical one —
`mark_sub_agent_tasks_cancelled_for_runs` matches on exactly those JSON paths,
and its own docstring records that the two tables "are not otherwise correlated
to runs". Deriving keeps `006-a2a-server-inbound`'s rule that task state has one
source of truth. The index is what makes it safe on a polling method: `tasks/get`
is the hot path, and an unindexed JSONB scan per poll is the cost that
`build_artifacts` already refuses to pay for its own read. The same index
accelerates the existing cancel-by-run path, so FR-004 gets it for free.

**Alternatives considered**: a real foreign key column on `sub_agent_tasks`
(cleanest, rejected as a schema change to a table chat, assignments and
evaluations all write — out of proportion to one reader); a counter or flag
maintained on the A2A task as children terminate (rejected: a second copy of
"is this task finished", which is the drift `006`'s data model exists to avoid).

---

## R2 — Webhook authentication uses the credential the partner supplies

**Decision**: present back what the partner registered — its `token` echoed in
the notification, or the `scheme` and `credentials` of its `authentication`
block — and hold no key material of our own.

**Rationale**: `TaskPushNotificationConfig` carries both fields for exactly this
purpose, so the partner chooses how its own endpoint is protected and we present
what it chose. Echoing a partner-issued token authenticates us to that partner:
only we and they hold it. Nothing needs publishing, generating, storing or
rotating, and the notification stays content-free (FR-017), so a forged one
discloses nothing — the exposure it prevents is a partner being made to poll our
task endpoint on a false signal.

**Alternatives considered**: an asymmetric keypair signing a JWT the partner
verifies against a published JWKS. Rejected as disproportionate: the platform has
no asymmetric key today (`core/security.py` signs HS256 with `secret_key`, which
cannot be published), so it means new credential material, a JWKS route and a
rotation story — to protect a payload that carries nothing, from a forgery whose
worst outcome the echoed token already prevents. It is the right answer only for
a partner that cannot pre-share a secret, and no partner has asked.

---

## R4 — Callback addresses reuse the outbound egress guards

**Decision**: validate a registered callback address with the destination rules
and circuit breaker built for the client direction
(`a2a/client/destinations.py`, `a2a/client/breaker.py`), and deliver with
`httpx` through the same path.

**Rationale**: a partner-supplied URL we POST to is the same threat as a partner
card URL we fetch — SSRF into the deployment's own network — and that guard is
already written, already tested, and already the place a reviewer looks. The
breaker additionally gives FR-020's bound a shape that degrades rather than
retrying a dead endpoint forever. A second egress path would be a second thing
to keep correct (Principle VIII).

**Alternatives considered**: a fresh allow-list for webhook hosts (rejected:
partners' callback hosts are not knowable in advance, so an allow-list would
either be empty or a per-partner configuration burden); no breaker, fixed retries
only (rejected: a partner with a permanently dead endpoint would consume delivery
attempts on every task forever).

---

## R5 — Continuation attaches at the refusal that already names itself

**Decision**: implement FR-007 and FR-012 at the existing `taskId` refusal in
`api/v1/a2a_server.py`, replacing the refusal rather than adding a branch beside
it.

**Rationale**: that refusal exists precisely because continuation was
unimplementable without a conversation, and its comment already identifies itself
as the seam where a real resume goes. Replacing it keeps one code path deciding
what a follow-up means; a second entry point would let the two disagree about
which agent is responsible.

**Alternatives considered**: a separate continuation method outside the protocol
vocabulary (rejected: partners send `message/send` with a `taskId`, so a bespoke
method would be unreachable by conforming clients).

---

## R6 — The parked-task ceiling extends the existing reaper

**Decision**: bound a non-terminal task's total life with a new setting distinct
from the execution lease, and enforce it in the existing A2A stale-task reaper.

**Rationale**: the lease answers "is this run still alive" and is refreshed
every quarter-lease while it is; FR-002 keeps that true for a parked task, which
means the lease cannot also serve as the total bound — hence a second, longer
bound. The reaper is where "drive an abandoned task terminal" already
lives, holds the multi-replica discipline Principle IV asks for, and already
writes a curated partner-visible reason, so FR-005 is a predicate change rather
than a new loop.

**Alternatives considered**: reuse the lease as the ceiling by not renewing it
while parked (rejected: the task would be reported abandoned while its work is
demonstrably running, which is the untruth this feature exists to remove); a
per-task deadline column (rejected: the bound is a deployment policy, not a
property of one task, and a column would let two tasks disagree about it).

---

## Deferred to implementation

- **Concrete bounds** — retry count (FR-020), callbacks per task (FR-022),
  with a fail-closed default; the spec deliberately states the obligation and not
  the number.
- **Delivery observability** — the spec's one outstanding gap. An operator
  currently has nothing to look at when a partner's callback fails; `006`'s
  FR-037 covers submission audit only. Named in `tasks.md` so it is decided
  rather than forgotten.
