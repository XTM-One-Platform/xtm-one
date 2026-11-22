"""Fleet autonomy level (#9769, slice S1): the level matrix, the raise rule and the seeded gate catalog."""

from __future__ import annotations

import pytest

from app.agents.builtin_fleet_docs import BUILTIN_FLEET_STRUCTURED
from app.models.fleet import Fleet
from app.services.fleet_autonomy import (
    AUTONOMY_LEVELS,
    DEFAULT_AUTONOMY_LEVEL,
    GATE_IMPACTS,
    effective_level,
    ensure_level_change_allowed,
    gate_catalog,
    is_gated,
    is_raise,
    normalize_level,
)
from app.services.fleet_docs import clean_structured
from app.services.fleets import BUILTIN_FLEET_WRITABLE_FIELDS, FleetActionRefusedError

_CTEM = sorted(BUILTIN_FLEET_STRUCTURED)

# Expected matrix: level -> impact -> waits for a person.
_MATRIX = {
    "full_gates": {"low": True, "high": True, "destructive": True},
    "medium": {"low": False, "high": True, "destructive": True},
    "low": {"low": False, "high": False, "destructive": True},
    "autonomous": {"low": False, "high": False, "destructive": False},
}


def test_the_default_is_full_human_gates_on_the_model_and_the_column():
    column = Fleet.__table__.c.autonomy_level
    assert DEFAULT_AUTONOMY_LEVEL == "full_gates"
    assert column.default.arg == "full_gates"
    assert column.server_default.arg == "full_gates"
    assert not column.nullable


@pytest.mark.parametrize("level", AUTONOMY_LEVELS)
@pytest.mark.parametrize("impact", GATE_IMPACTS)
def test_the_matrix(level, impact):
    assert is_gated(level, impact) is _MATRIX[level][impact]


def test_every_level_gates_at_least_as_much_as_the_next_one():
    for impact in GATE_IMPACTS:
        flags = [is_gated(level, impact) for level in AUTONOMY_LEVELS]
        assert flags == sorted(flags, reverse=True), impact


@pytest.mark.parametrize("level", [None, "", "FULL", "anything"])
def test_an_unknown_level_fails_closed(level):
    assert effective_level(level) == "full_gates"
    assert all(is_gated(level, impact) for impact in GATE_IMPACTS)


def test_an_unknown_impact_is_treated_as_destructive():
    assert is_gated("low", "unknown") is True
    assert is_gated("autonomous", "unknown") is False


def test_normalize_refuses_an_unknown_level():
    assert normalize_level("medium") == "medium"
    with pytest.raises(FleetActionRefusedError) as exc:
        normalize_level("max")
    assert exc.value.status_code == 422


def test_only_an_admin_raises_and_anyone_lowers():
    assert is_raise("full_gates", "medium") and not is_raise("low", "medium")
    ensure_level_change_allowed(previous="full_gates", new="autonomous", is_admin=True)
    ensure_level_change_allowed(previous="autonomous", new="full_gates", is_admin=False)
    ensure_level_change_allowed(previous="medium", new="medium", is_admin=False)
    with pytest.raises(FleetActionRefusedError) as exc:
        ensure_level_change_allowed(previous="full_gates", new="medium", is_admin=False)
    assert exc.value.status_code == 403


def test_the_level_is_writable_on_a_built_in_fleet():
    assert "autonomy_level" in BUILTIN_FLEET_WRITABLE_FIELDS


@pytest.mark.parametrize("slug", _CTEM)
def test_each_seeded_gate_is_structured_with_a_unique_key_and_an_impact(slug):
    gates = BUILTIN_FLEET_STRUCTURED[slug].get("gates") or []
    keys = [g["key"] for g in gates]
    assert all(keys), slug
    assert len(keys) == len(set(keys)), slug
    assert all(g["impact"] in GATE_IMPACTS for g in gates), slug
    cleaned = clean_structured(BUILTIN_FLEET_STRUCTURED[slug]) or {}
    assert [g["key"] for g in cleaned.get("gates", [])] == keys, slug


@pytest.mark.parametrize("slug", _CTEM)
def test_full_human_gates_keeps_every_seeded_gate(slug):
    """No gate bypass at the default level, on each of the five CTEM fleets."""
    catalog = gate_catalog("full_gates", BUILTIN_FLEET_STRUCTURED[slug].get("gates"))
    assert all(entry["gated"] for entry in catalog), slug


@pytest.mark.parametrize("slug", _CTEM)
@pytest.mark.parametrize("level", AUTONOMY_LEVELS)
def test_the_catalog_follows_the_matrix_per_fleet_and_level(slug, level):
    gates = BUILTIN_FLEET_STRUCTURED[slug].get("gates") or []
    catalog = gate_catalog(level, gates)
    assert len(catalog) == len(gates)
    for entry, gate in zip(catalog, gates, strict=True):
        assert entry["key"] == gate["key"]
        assert entry["gated"] is _MATRIX[level][gate["impact"]]
        assert entry["by_level"] == {lvl: _MATRIX[lvl][gate["impact"]] for lvl in AUTONOMY_LEVELS}


def test_autonomous_gates_nothing_and_medium_frees_the_analyst_review_only():
    threat = BUILTIN_FLEET_STRUCTURED["ctem-loop"]["gates"]
    assert not any(e["gated"] for e in gate_catalog("autonomous", threat))
    medium = {e["key"]: e["gated"] for e in gate_catalog("medium", threat)}
    assert [k for k, gated in medium.items() if not gated] == ["ctem.analyst_review"]


def test_a_gate_without_an_impact_reads_as_high():
    (entry,) = gate_catalog("medium", [{"action": "Do it"}])
    assert entry["impact"] == "high" and entry["gated"] is True
    (entry,) = gate_catalog("low", [{"action": "Do it"}])
    assert entry["gated"] is False
