# Phase 1 Data Model: AI-disabled platform mode

Four things carry state or meaning in this feature. Only one is a schema change.

> **Surface and term names** follow the spec. "AI" means anything that needs a
> model or embedding provider, directly or transitively; storing or displaying
> data produced earlier by AI is not itself AI, which is what makes the read
> surface safe to keep.

## 1. `platform_settings.ai_disabled` — **NEW**, one nullable boolean

| | |
|---|---|
| Table | `platform_settings` (existing singleton) |
| Column | `ai_disabled BOOLEAN NULL` |
| Default | none — the column is nullable and unset |
| Migration | one Alembic revision, add column, no backfill |

The singleton invariant is already enforced at the database level by a unique
index on a constant expression (`models/platform_settings.py:29`), so concurrent
writers converge on one row rather than racing a check-then-insert. That is what
satisfies FR-026 without a lock.

**Three states, two outcomes:**

| Stored value | Meaning | Resolved |
|---|---|---|
| `NULL` | No administrator has chosen. Follow the deployment. | from `PLATFORM_MODE` |
| `true` | An administrator turned AI off. | disabled |
| `false` | An administrator turned AI on. | enabled |

**The column is the only place the negative polarity appears.** The wire uses
`ai_enabled` for both reading and writing (`contracts/ai-state-api.md`), and
`services/ai_state.py` performs the inversion once. Two names differing only by a
negation, both visible to clients, is a bug waiting at the boundary where being
wrong means AI runs where it must not.

`NULL` is load-bearing and is the reason this is not a plain boolean. Without it,
whatever value first boot writes pins the platform, and a later change to
`PLATFORM_MODE` silently does nothing — a failure an operator discovers only by
its absence. `platform_name` already uses this convention
(`platform_settings_helpers.py:66`).

"Reset to platform default" writes `NULL` back. That is a distinct operation from
writing `false`, and the interface must not conflate them.

## 2. Resolved AI state — derived, not stored

The single boolean every decision reads. Lives in `services/ai_state.py`, which is
the **only** module permitted to read `platform_mode` for this purpose (FR-004) —
so a second reader is a defect findable by grep rather than by reasoning.

```
resolved = (stored is not None) ? not stored : (platform_mode != "ai_disabled")
```

**When it cannot be resolved** — the database is unreachable, the value is
unrecognised — it resolves to **disabled**, with one exception: boot itself must
still complete. Refusing AI on an unknown state is recoverable and visible;
assuming AI is permitted runs a model on a deployment that may prohibit it, which
is the scenario the mode exists for. Refusing to *boot*, though, would turn a
transient database condition into an outage, so the resolver distinguishes "gate a
request" from "start the process".

**Cached for five seconds**, process-local, primed at boot and invalidated by
the toggle — so the steady-state request path does no database work. Staleness
is bounded per replica by the TTL (FR-003a) rather than by cross-replica
invalidation. A failed read is *not* cached, so a blip cannot hold AI off after
the database recovers. Never a process-lifetime constant, which FR-002 forbids.

## 3. Built-in content provisioning — an operation, not an entity

The set of AI content a normally configured platform creates for itself. Today it
exists only as inline lifespan code (`main.py:1044-1200`); this feature extracts it
into one callable, idempotent operation invoked from **two** places: boot, and the
moment an administrator turns AI on.

Ordered, because later steps reference entities earlier ones create:

1. built-in templates
2. built-in agents
3. built-in assignments
4. built-in skills
5. built-in integrations
6. built-in knowledge bases
7. the example flow
8. built-in template containers
9. built-in intent bindings
10. model prices

**Idempotence is inherited, not added.** These functions already run at every boot
and already return counts, which is why they can be reused. What the extraction
must not change is their **order** — that is the property with no test and the one
a refactor is most likely to break.

**No new table records that provisioning happened.** Re-running it is safe, so
there is nothing to remember; a marker would be a second source of truth that
could disagree with the content actually present.

## 4. Flow node AI dependence — one declared property

| | |
|---|---|
| Where | `NodeKind` dataclass, `services/flow_catalog.py:295` |
| Field | `requires_ai: bool = True` |
| Consumers | the runner, the trigger path, the flow editor |

Frozen dataclass, one definition per placeable node type, so all three consumers
read the same property and cannot disagree (FR-015).

**The default is `True`, and that is deliberate.** Every other field on `NodeKind`
defaults to its permissive value; this one does the opposite so that a node type
added by someone who has never heard of this feature is held back rather than run
(FR-030). Each of the ~50 non-AI node kinds declares `requires_ai=False`
explicitly — verbose on purpose, one reviewable line per exemption, the same
posture as a reviewed allow-list.

**A flow's verdict is computed from its current graph at trigger time**, never
stored. A flow that gains or loses an AI node changes its own runnability, and a
cached verdict would be wrong exactly when someone had just fixed the flow.

Node kinds whose classification the spec settles as *requires AI* despite
ambiguity: a tool call with model-chosen arguments, guardrails, structured
output. Recorded here because they are the ones a reader will question.

## 5. Refusal reason — one wire value

The machine-readable code returned by every refused write or execution, so a
client branches on state rather than parsing prose (FR-009). Added **beside** the
existing prose in the 403 body, not instead of it: no in-repo consumer parses that
prose today, but an external one may display it.

Carried by the two existing dependencies (`api/deps.py:399`, `:420`), which are
attached at 83 route call sites — so the value reaches every gated route without
any of them being edited.

**It is the secondary signal.** The primary one is the AI state on the platform
config payload, which is how the frontend already consumes the licence answer
(`agentic_enabled` now, `agentic_ai_enabled` when this was written) and the only thing available before authentication
(FR-011). A client that only ever sees the code has already made a request that
should have been prevented.

See [contracts/refusal-payload.md](./contracts/refusal-payload.md) and
[contracts/ai-state-api.md](./contracts/ai-state-api.md).
