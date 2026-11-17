"""The flow shadow tables (#8260): what their constraints admit, as the lifecycle names it."""

from __future__ import annotations

import re

from app.models.flow_shadow import (
    PAIR_STATUSES,
    SHADOW_OPEN_STATUSES,
    SHADOW_PURPOSES,
    SHADOW_STATUSES,
    FlowShadow,
    FlowShadowPair,
)


def _check_values(table, name: str) -> set[str]:
    constraint = next(c for c in table.constraints if c.name == name)
    return set(re.findall(r"'([a-z_]+)'", str(constraint.sqltext)))


def test_the_status_constraints_admit_exactly_the_lifecycle():
    assert _check_values(FlowShadow.__table__, "ck_flow_shadows_status") == set(SHADOW_STATUSES)
    assert _check_values(FlowShadow.__table__, "ck_flow_shadows_purpose") == set(SHADOW_PURPOSES) == {"measure", "switch_over"}
    assert _check_values(FlowShadowPair.__table__, "ck_flow_shadow_pairs_status") == set(PAIR_STATUSES)


def test_an_assignment_has_at_most_one_open_shadow():
    index = next(i for i in FlowShadow.__table__.indexes if i.name == "uq_flow_shadows_open_assignment")
    assert index.unique is True
    assert [c.name for c in index.columns] == ["assignment_id"]
    assert set(re.findall(r"'([a-z_]+)'", str(index.dialect_options["postgresql"]["where"]))) == set(SHADOW_OPEN_STATUSES)
    assert set(SHADOW_OPEN_STATUSES) == {"running", "watching"}


def test_a_shadow_outlives_its_assignment_and_its_copy():
    """SET NULL, never CASCADE: a deleted assignment or copy still closes the shadow and its proposal."""
    for column in ("assignment_id", "flow_id", "production_flow_id"):
        (key,) = FlowShadow.__table__.c[column].foreign_keys
        assert key.ondelete == "SET NULL"
    (pair_key,) = FlowShadowPair.__table__.c["shadow_id"].foreign_keys
    assert pair_key.ondelete == "CASCADE"
