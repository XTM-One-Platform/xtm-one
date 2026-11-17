# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The scope vocabularies a flow node can offer, and why there are three.

A `scope` select appears on five node kinds, and it does not ask the same
question on all five:

* **Coordination scope** — `capability.shared_state`, `capability.workset`.
  *Whose blackboard, whose corpus?* It decides isolation: which runs share
  state and which cannot see each other's.
* **Event scope** — `capability.on_delegate`, `action.emit_event`.
  *Who hears this emission?* It decides reach: which waiters an event wakes.
* **Subscription scope** — `trigger.platform_event`, and the `platform_event`
  trigger of an assignment. *Whose objects does this trigger hear?* It decides
  what the platform delivers: the owner's own objects, or every object for an
  administrator.

They coincide today at ``run`` / ``flow`` / ``global``, which is why the
event vocabulary's own comment described itself as *mirroring* the
shared-state one. Mirroring is the accurate word and the important one: the
two are free to stop coinciding, and a scope that containerizes tool state
has no meaning for an emission's reach. Keeping them as two named tuples is
what stops a new coordination scope from silently appearing on
``action.emit_event``.

**Why single-sourcing them matters at all.** The catalog's option list is not
presentation — it is a load-bearing enum. ``flow_graph`` validates a static
select's value against exactly these dicts and rejects anything out of range,
because *"the runtime treats unknown enum members as silent no-ops (an unknown
If operator evaluates false, an unknown scope falls back...)"*. A scope added
to one hand-written copy and not another is therefore not cosmetic drift: it
is a value the validator accepts on one node and rejects on the next, and a
runtime that falls back to the shared namespace rather than failing.

This module deliberately imports nothing. ``flow_catalog`` is free of ORM
imports and stays that way; the models below alias these names rather than
the other way round.
"""

# ── Coordination scope: whose state is this ──────────────────────────────────

COORDINATION_SCOPE_RUN = "run"
COORDINATION_SCOPE_FLOW = "flow"
COORDINATION_SCOPE_GLOBAL = "global"
#: A named, user-visible `Workspace`. The first coordination scope
#: whose referent is NOT derivable from the execution context: `run` and
#: `flow` take theirs from the running flow and `global` has none, but a
#: workspace must be CHOSEN — from the node's config on a flow, from the
#: conversation on a chat turn. That is why it is the only scope that can
#: fail to resolve, and why resolution fails CLOSED rather than falling back.
COORDINATION_SCOPE_WORKSPACE = "workspace"

#: Every accepted coordination scope, in the order the builder offers them
#: (narrowest isolation first).
COORDINATION_SCOPES: tuple[str, ...] = (
    COORDINATION_SCOPE_RUN,
    COORDINATION_SCOPE_FLOW,
    COORDINATION_SCOPE_WORKSPACE,
    COORDINATION_SCOPE_GLOBAL,
)

#: What a coordination capability scopes to when the builder does not choose.
#: `run` is the isolating default on purpose: an unconfigured node must not
#: quietly share state with everything else the owner runs.
COORDINATION_SCOPE_DEFAULT = COORDINATION_SCOPE_RUN

#: Rendered choices for a `scope` select on a coordination capability node.
#:
#: "(across runs)" is carried deliberately: for state, `flow` means the corpus
#: *survives between* runs — the property a recurring job is choosing. The
#: event vocabulary's `flow` means something else, so it is labelled without
#: the suffix. The two labels differing is correct, not drift.
COORDINATION_SCOPE_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": COORDINATION_SCOPE_RUN, "label": "This run"},
    {"value": COORDINATION_SCOPE_FLOW, "label": "This flow (across runs)"},
    {"value": COORDINATION_SCOPE_WORKSPACE, "label": "A workspace"},
    {"value": COORDINATION_SCOPE_GLOBAL, "label": "Global"},
)


# ── Event scope: who hears this emission ─────────────────────────────────────

EVENT_SCOPE_RUN = "run"
EVENT_SCOPE_FLOW = "flow"
EVENT_SCOPE_GLOBAL = "global"

EVENT_SCOPES: tuple[str, ...] = (
    EVENT_SCOPE_RUN,
    EVENT_SCOPE_FLOW,
    EVENT_SCOPE_GLOBAL,
)

#: Rendered choices for a `scope` select on an event-emitting node.
#:
#: `flow` here means any run of this flow can HEAR the emission — not that
#: state survives between runs — hence no "(across runs)" suffix.
EVENT_SCOPE_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": EVENT_SCOPE_RUN, "label": "This run"},
    {"value": EVENT_SCOPE_FLOW, "label": "This flow"},
    {"value": EVENT_SCOPE_GLOBAL, "label": "Global"},
)


# ── Subscription scope: whose objects a trigger hears ────────────────────────

SUBSCRIPTION_SCOPE_OWN = "own"
SUBSCRIPTION_SCOPE_ALL = "all"

SUBSCRIPTION_SCOPES: tuple[str, ...] = (
    SUBSCRIPTION_SCOPE_OWN,
    SUBSCRIPTION_SCOPE_ALL,
)

#: An assignment's platform-event trigger only: the events of the agents of
#: the assignment agent's home fleet, membership read at delivery. A flow has
#: no home fleet, so the flow node does not offer it.
SUBSCRIPTION_SCOPE_FLEET = "fleet"

#: Every scope an assignment's ``platform_event`` trigger accepts.
ASSIGNMENT_SUBSCRIPTION_SCOPES: tuple[str, ...] = (*SUBSCRIPTION_SCOPES, SUBSCRIPTION_SCOPE_FLEET)

#: Rendered choices for the `scope` select of a platform-event trigger.
#: `all` is honoured only for an administrator's flow or assignment
#: (``platform_events.in_scope``); for anyone else it reads as `own`.
SUBSCRIPTION_SCOPE_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": SUBSCRIPTION_SCOPE_OWN, "label": "The owner's objects and its agents'"},
    {"value": SUBSCRIPTION_SCOPE_ALL, "label": "Every object (admins)"},
)
