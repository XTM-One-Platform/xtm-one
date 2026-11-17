"""The scope vocabularies are single-sourced, and there are three of them.

A `scope` select appears on five node kinds and asks three different questions:
who *holds* this state (coordination), who *hears* this emission (event), and
whose objects a platform-event trigger hears (subscription). The first
two coincide at ``run`` / ``flow`` / ``global`` today and are free to stop.

These tests exist because the catalog's option list is a load-bearing enum:
``flow_graph`` validates a static select against it and rejects out-of-range
values, so a scope present in one hand-written copy and absent from another is
accepted on one node and refused on the next. The guarantee worth locking down
is therefore not "the lists are tidy" but **"the offered options are exactly
the vocabulary, per family"** — which is what makes adding a fourth
coordination scope a one-line change that cannot leak onto an event node.
"""

from __future__ import annotations

from app.core.scopes import (
    COORDINATION_SCOPE_DEFAULT,
    COORDINATION_SCOPE_OPTIONS,
    COORDINATION_SCOPES,
    EVENT_SCOPE_OPTIONS,
    EVENT_SCOPES,
    SUBSCRIPTION_SCOPE_OPTIONS,
    SUBSCRIPTION_SCOPES,
)
from app.models.workset import WORKSET_DEFAULT_SCOPE, WORKSET_SCOPES
from app.services import platform_events
from app.services.flow_catalog import NODE_KINDS_BY_ID

# The two families, as the catalog wires them.
COORDINATION_NODES = ("capability.shared_state", "capability.workset")
EVENT_NODES = ("capability.on_delegate", "action.emit_event")
SUBSCRIPTION_NODES = ("trigger.platform_event",)


def _scope_field(kind: str):
    node = NODE_KINDS_BY_ID[kind]
    field = next(f for f in node.config_fields if f.name == "scope")
    return field


def _values(options) -> tuple[str, ...]:
    return tuple(o["value"] for o in options)


def test_options_are_exactly_the_vocabulary() -> None:
    """Adding a scope to a tuple must add it to what the builder offers.

    If these could drift, the validator would accept a value the builder never
    offers, or reject one it does — the two failure modes that make an enum
    worth having in the first place.
    """
    assert _values(COORDINATION_SCOPE_OPTIONS) == COORDINATION_SCOPES
    assert _values(EVENT_SCOPE_OPTIONS) == EVENT_SCOPES
    assert _values(SUBSCRIPTION_SCOPE_OPTIONS) == SUBSCRIPTION_SCOPES


def test_every_scope_field_draws_from_a_shared_tuple() -> None:
    """No fifth hand-written copy.

    Identity, not equality: an equal-but-separate literal would pass an
    equality check today and silently miss the next scope added.
    """
    for kind in COORDINATION_NODES:
        assert _scope_field(kind).options is COORDINATION_SCOPE_OPTIONS, kind
    for kind in EVENT_NODES:
        assert _scope_field(kind).options is EVENT_SCOPE_OPTIONS, kind
    for kind in SUBSCRIPTION_NODES:
        assert _scope_field(kind).options is SUBSCRIPTION_SCOPE_OPTIONS, kind


def test_the_subscription_vocabulary_is_the_one_assignments_validate() -> None:
    """An assignment's platform-event trigger and a flow's offer the same scopes."""
    assert (platform_events.SCOPE_OWN, platform_events.SCOPE_ALL) == SUBSCRIPTION_SCOPES
    assert not set(SUBSCRIPTION_SCOPES) & (set(COORDINATION_SCOPES) | set(EVENT_SCOPES))


def test_the_two_families_stay_separate() -> None:
    """A coordination scope must not appear on an event node, or vice versa.

    This is the guard the workspace scope is being added under: `workspace`
    containerizes tool state and means nothing for an emission's reach, so it
    must land in exactly one of these two lists. The assertion is written as a
    symmetric difference so it fails whichever list gains a value the other
    does not, rather than only in the direction anticipated today.
    """
    assert COORDINATION_SCOPE_OPTIONS is not EVENT_SCOPE_OPTIONS
    extra_coordination = set(COORDINATION_SCOPES) - set(EVENT_SCOPES)
    extra_event = set(EVENT_SCOPES) - set(COORDINATION_SCOPES)
    for kind in EVENT_NODES:
        offered = set(_values(_scope_field(kind).options))
        assert not (offered & extra_coordination), (
            f"{kind} offers a coordination-only scope: "
            f"{sorted(offered & extra_coordination)}"
        )
    for kind in COORDINATION_NODES:
        offered = set(_values(_scope_field(kind).options))
        assert not (offered & extra_event), (
            f"{kind} offers an event-only scope: "
            f"{sorted(offered & extra_event)}"
        )


def test_every_scope_default_is_offered() -> None:
    """A default the select does not offer renders as an unset field."""
    for kind in COORDINATION_NODES + EVENT_NODES + SUBSCRIPTION_NODES:
        field = _scope_field(kind)
        assert field.default in _values(field.options), kind


def test_workset_vocabulary_is_the_coordination_one() -> None:
    """`resolve_workset` validates against WORKSET_SCOPES.

    Keeping the historical names is fine; keeping a historical *copy* is not —
    the workset service would then refuse a scope the builder offers.
    """
    assert WORKSET_SCOPES is COORDINATION_SCOPES
    assert WORKSET_DEFAULT_SCOPE == COORDINATION_SCOPE_DEFAULT


def test_no_scope_field_hand_writes_its_options() -> None:
    """Catch a fifth site added later without going through this module."""
    shared = (COORDINATION_SCOPE_OPTIONS, EVENT_SCOPE_OPTIONS, SUBSCRIPTION_SCOPE_OPTIONS)
    strays = [
        f"{node.kind}.{f.name}"
        for node in NODE_KINDS_BY_ID.values()
        for f in node.config_fields
        if f.name == "scope" and f.options and not any(f.options is s for s in shared)
    ]
    assert not strays, f"scope selects not drawing from app.core.scopes: {strays}"
