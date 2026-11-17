"""The handover type map is read and written by one rule on each side.

An empty ``handover_types`` map is the legacy mode in which every target runs
every handover type. ``app/services/handover_types.py`` holds the backend rules
and ``frontend/src/lib/handover-types.ts`` the screens' ones; both are checked
against the ONE table ``frontend/tests_e2e/fixtures/handover-type-cases.json``.
"""

from __future__ import annotations

import inspect
import json
import pathlib
import re

import pytest

from app.services import agent_chat, assignment_engine, general_chat
from app.services.builtin_tools import agent_builder
from app.services.handover_types import (
    handover_types_after_removal,
    is_legacy_handover_map,
    runs_every_handover_type,
    with_added_handover_targets,
)

FRONTEND_ROOT = pathlib.Path(__file__).resolve().parents[2] / "frontend"
CASES = json.loads(
    (FRONTEND_ROOT / "tests_e2e" / "fixtures" / "handover-type-cases.json").read_text(encoding="utf-8")
)


def _runs(type_map: dict[str, str], agent_id: str) -> str:
    return "all" if is_legacy_handover_map(type_map) else type_map.get(agent_id, "delegate")


@pytest.mark.parametrize("case", CASES["legacy"])
def test_targets_run_every_type_only_while_no_target_has_one(case):
    assert runs_every_handover_type(case["targets"], case["types"]) is case["runs_every_type"]


@pytest.mark.parametrize("case", CASES["added"])
def test_an_added_target_joins_the_legacy_mode_or_is_recorded_as_delegate(case):
    assert with_added_handover_targets(case["targets"], case["types"], [case["add"]]) == case["expected"]


def test_a_type_sent_for_an_added_target_wins_over_the_delegate_default():
    assert with_added_handover_targets(["a"], {"a": "consult", "b": "transfer"}, ["b"]) == {
        "a": "consult",
        "b": "transfer",
    }


@pytest.mark.parametrize("case", CASES["removed"])
def test_removing_a_target_never_turns_an_explicit_map_into_the_legacy_mode(case):
    left = [agent_id for agent_id in case["targets"] if agent_id != case["remove"]]
    before = case["types"]
    kept = {agent_id: htype for agent_id, htype in before.items() if agent_id in left}
    after = handover_types_after_removal(left, before, kept)
    assert {agent_id: _runs(after, agent_id) for agent_id in left} == case["runs"]


def test_the_frontend_rules_are_checked_against_the_same_table():
    unit = (FRONTEND_ROOT / "src" / "lib" / "handover-types.test.ts").read_text(encoding="utf-8")
    assert "handover-type-cases.json" in unit
    for mirror in ("runsEveryHandoverType(", "withAddedHandoverTarget(", "withoutHandoverEdge("):
        assert mirror in unit


#: The runtime sites that register handover tools from a type map: each reads
#: the legacy mode through a helper, never with a local ``not <map>``. Agent
#: chat and assignment runs read it through ``handover_tool_types``.
RUNTIME_SITES = (general_chat, agent_chat, assignment_engine, agent_builder)


@pytest.mark.parametrize("module", RUNTIME_SITES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_every_runtime_site_reads_the_legacy_mode_through_the_helper(module):
    source = pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8")
    assert "is_legacy_handover_map(" in source or "handover_tool_types(" in source
    assert not re.search(r"\bor not _?(?:ga_)?handover_type_map\b", source)
