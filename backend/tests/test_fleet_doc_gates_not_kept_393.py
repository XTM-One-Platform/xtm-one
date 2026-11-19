"""#393: a documentation regenerate never keeps approval gates, and an unknown section is refused up front.

A fleet with no stored gate catalog acts on the gates of its documentation
(``fleet_autonomy.fleet_gates``): a regenerate that kept the previous version's
gates once the configuration gave none left the status chip on the stored level
instead of the instructions position. An unknown section was recorded as a
generation failure on the About page although nothing ran.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import fleet_doc_generation as gen
from app.services.fleet_autonomy import fleet_gates
from app.services.fleet_docs import DOC_SECTIONS


def _context() -> dict[str, Any]:
    return {
        "members": [{"agent": "lead", "name": "Lead", "orchestrator": True, "parent": None}],
        "automations": [],
        "trigger_cards": [],
        "schedule_quota": [],
        "tools": [],
        "approvals": {},
    }


def test_gates_are_not_a_kept_block() -> None:
    assert "gates" not in gen.KEPT_BLOCKS


def test_a_regenerate_drops_gates_the_configuration_no_longer_gives() -> None:
    previous = {"gates": [{"action": "Lead: each tool call waits for approval", "approver": "a person"}]}
    notes: list[dict[str, Any]] = []
    parts = gen.build_structured(None, _context(), notes, previous=previous) or {}
    assert "gates" not in parts
    assert {"code": "part_kept", "params": {"block": "gates"}} not in notes
    # The level then acts on nothing: the chip reads the instructions position.
    assert fleet_gates(None, {"structured": parts}) == []


def test_gates_still_derived_from_the_configuration() -> None:
    context = {**_context(), "approvals": {"Lead": ["each tool call waits for approval"]}}
    parts = gen.build_structured(None, context, [], previous=None) or {}
    assert [g["action"] for g in parts.get("gates", [])] == ["Lead: each tool call waits for approval"]


def test_check_section_accepts_every_known_section_and_none() -> None:
    gen.check_section(None)
    for key in DOC_SECTIONS:
        gen.check_section(key)


def test_check_section_refuses_an_unknown_one_naming_the_known_ones() -> None:
    with pytest.raises(gen.FleetDocDraftError) as exc:
        gen.check_section("approval")
    message = str(exc.value)
    assert "Unknown documentation section: approval." in message
    assert all(key in message for key in DOC_SECTIONS)
