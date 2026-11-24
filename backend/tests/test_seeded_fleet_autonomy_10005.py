"""Seeded fleets ship at the Low autonomy level (#10005): only destructive gates wait.

Pinned here without a database: the shipped level, what waits and what runs
alone at Low on every built-in fleet, and the reclassified gates. The boot
behaviour (fresh seed, person-set level kept, system audit row) is pinned
against PostgreSQL in ``test_builtin_fleets_postgres.py``.
"""

from __future__ import annotations

import pytest

from app.agents.builtin_fleet_docs import BUILTIN_FLEET_STRUCTURED
from app.agents.fleet_seed import SEED_AUDIT_ACTOR, SEED_DEFAULT_AUDIT_ACTION, SEEDED_FLEET_AUTONOMY_LEVEL
from app.models.fleet import Fleet
from app.services.fleet_autonomy import AUTONOMY_LEVELS, DEFAULT_AUTONOMY_LEVEL, gate_catalog, gated_tool_rules

_CTEM = sorted(BUILTIN_FLEET_STRUCTURED)

#: Per fleet at Low: the gates that still wait for a person. Every other gate runs alone and is logged.
_WAITS_AT_LOW = {
    # The CTEM loop fleets (#916, CTEM-34; #1339): the OpenAEV validation and Mobilize wait
    # out of the box on a fleet with the validation step, Mobilize alone on the third-party one.
    "ctem-loop": {"ctem.validation.non_readonly", "ctem.mobilize"},
    "ctem-vuln-prioritization": {"ctem.validation.non_readonly", "ctem.mobilize"},
    "ctem-ai-security": {"ctem.validation.non_readonly", "ctem.mobilize"},
    "ctem-third-party-risk": {"ctem.mobilize"},
}


def test_seeded_fleets_ship_at_low_the_last_level_before_full_autonomy():
    assert SEEDED_FLEET_AUTONOMY_LEVEL == "low"
    assert AUTONOMY_LEVELS.index(SEEDED_FLEET_AUTONOMY_LEVEL) == len(AUTONOMY_LEVELS) - 2


def test_custom_fleets_keep_full_human_gates():
    assert DEFAULT_AUTONOMY_LEVEL == "full_gates"
    assert Fleet.__table__.c.autonomy_level.server_default.arg == "full_gates"


def test_the_seed_audit_row_is_not_a_user_change():
    assert SEED_DEFAULT_AUDIT_ACTION != "autonomy_level_change"
    assert SEED_AUDIT_ACTOR == "fleet-seed"


def test_every_shipped_fleet_is_listed():
    assert set(_WAITS_AT_LOW) == set(_CTEM)


@pytest.mark.parametrize("slug", _CTEM)
def test_at_low_only_destructive_gates_wait(slug):
    catalog = gate_catalog(SEEDED_FLEET_AUTONOMY_LEVEL, BUILTIN_FLEET_STRUCTURED[slug].get("gates"))
    waiting = {e["key"] for e in catalog if e["gated"]}
    assert waiting == _WAITS_AT_LOW[slug], slug
    assert all(e["impact"] == "destructive" for e in catalog if e["gated"]), slug
    assert all(e["impact"] != "destructive" for e in catalog if not e["gated"]), slug


def test_at_low_the_validation_and_the_mobilize_wait_as_decisions_never_in_the_tool_path() -> None:
    """#1339: the OpenAEV validation and the one Mobilize decision wait at Low.

    #1675 (Sam, 24 Nov 2026): as decision items between steps; no tool call pauses.
    """
    gates = BUILTIN_FLEET_STRUCTURED["ctem-loop"]["gates"]
    catalog = {e["key"]: e["gated"] for e in gate_catalog(SEEDED_FLEET_AUTONOMY_LEVEL, gates)}
    assert catalog["ctem.validation.non_readonly"] is True and catalog["ctem.mobilize"] is True
    assert gated_tool_rules(SEEDED_FLEET_AUTONOMY_LEVEL, gates) == []


def test_the_openaev_validation_waits_at_low() -> None:
    gates = BUILTIN_FLEET_STRUCTURED["ctem-vuln-prioritization"]["gates"]
    catalog = {e["key"]: e for e in gate_catalog(SEEDED_FLEET_AUTONOMY_LEVEL, gates)}
    assert catalog["ctem.validation.non_readonly"]["gated"] is True
    assert gated_tool_rules(SEEDED_FLEET_AUTONOMY_LEVEL, gates) == []
