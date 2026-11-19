"""The fleet spec envelope, without a database.

The PostgreSQL half (export, delete, re-apply identical; dry run writes
nothing; adoption; refusals) is ``test_fleet_spec_postgres.py``.
"""

from __future__ import annotations

import pytest

from app.services.fleet_spec import (
    FLEET_SPEC_API_VERSION,
    FleetSpecError,
    _portable_bundle,
    parse_fleet_spec_text,
    validate_fleet_spec,
)


def _spec(**body) -> dict:
    return {
        "apiVersion": FLEET_SPEC_API_VERSION,
        "kind": "Fleet",
        "metadata": {"name": "Office", "slug": "office"},
        "spec": {
            "orchestrator": "ea",
            "members": [
                {"key": "ea", "handovers": [{"to": "dev", "type": "consult"}]},
                {"key": "dev", "parent": "ea"},
            ],
            **body,
        },
    }


def test_a_valid_spec_is_accepted() -> None:
    assert validate_fleet_spec(_spec())["metadata"]["name"] == "Office"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda s: s.update(apiVersion="v0"), "apiVersion"),
        (lambda s: s.update(kind="Agent"), "kind"),
        (lambda s: s["metadata"].update(name=""), "metadata.name"),
        (lambda s: s["spec"].update(orchestrator="nobody"), "orchestrator"),
        (lambda s: s["spec"]["members"].append({"key": "ea"}), "twice"),
        (lambda s: s["spec"]["members"][1].update(parent="ghost"), "parent"),
        (lambda s: s["spec"]["members"][1].update(parent="dev"), "itself"),
        (lambda s: s["spec"]["members"][0].update(handovers=[{"to": "ghost"}]), "handover"),
        (lambda s: s["spec"]["members"][0].update(handovers=[{"to": "dev", "type": "shout"}]), "handover type"),
        (lambda s: s["spec"].update(connected=[{"agent": {"slug": "x"}, "context_inclusion": "all"}]), "context"),
        (lambda s: s["spec"].update(connected=[{"nothing": 1}]), "connected"),
        (lambda s: s["spec"].update(autonomy_level="wild"), "autonomy_level"),
    ],
)
def test_a_malformed_spec_is_refused_with_its_reason(mutate, message: str) -> None:
    spec = _spec()
    mutate(spec)
    with pytest.raises(FleetSpecError, match=message):
        validate_fleet_spec(spec)


@pytest.mark.parametrize("level", ["full_gates", "medium", "low", "autonomous"])
def test_a_spec_may_name_the_fleet_autonomy_level(level: str) -> None:
    """#379: a spec carries the level a new fleet is created at."""
    assert validate_fleet_spec(_spec(autonomy_level=level))["spec"]["autonomy_level"] == level


def test_yaml_and_json_text_parse_and_anything_else_is_refused() -> None:
    assert parse_fleet_spec_text('{"kind": "Fleet"}') == {"kind": "Fleet"}
    assert parse_fleet_spec_text("kind: Fleet\n") == {"kind": "Fleet"}
    with pytest.raises(FleetSpecError):
        parse_fleet_spec_text("- a list")
    with pytest.raises(FleetSpecError):
        parse_fleet_spec_text("key: [unclosed")


def test_a_spec_bundle_is_reproducible_and_carries_no_agent_ids() -> None:
    bundle = {
        "version": 3,
        "exported_at": "2026-11-02T00:00:00Z",
        "agent": {"name": "Dev", "handover_types": {"some-uuid": "consult"}, "handover_conditions": {"x": "y"}},
    }
    out = _portable_bundle(bundle)
    assert "exported_at" not in out
    assert out["agent"] == {"name": "Dev"}
    assert bundle["agent"]["handover_types"] == {"some-uuid": "consult"}, "the input is not mutated"
