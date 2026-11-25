"""Platform Health's specialist groups merged into one internal group (#1943).

Migration ``h1943healthgrp01`` rewrites every stored id and allow list that
names a retired group onto ``health_specialists``; no alias of the old ids is
kept, so the runtime refuses them.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.services.builtin_tool_catalog import build_builtin_tool_catalog
from app.services.builtin_tool_groups import BUILTIN_TOOL_GROUPS

_RETIRED = (
    "health_sandbox",
    "health_evaluation",
    "improvement_proposals",
    "memory_steward",
    "health_diagnosis",
    "health_experiments",
    "flow_lab",
    "health_modules",
)


def _migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "h1943healthgrp01_merge_health_specialist_groups.py"
    )
    spec = importlib.util.spec_from_file_location("h1943healthgrp01", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_retired_id_is_a_group_or_an_alias():
    from app.services.builtin_tool_aliases import is_former_builtin_id

    group_ids = {group.id for group in BUILTIN_TOOL_GROUPS}
    for retired in _RETIRED:
        assert retired not in group_ids
        assert not is_former_builtin_id(retired)


def test_the_internal_group_keeps_every_function_the_retired_groups_granted():
    names = {d["name"] for d in build_builtin_tool_catalog(include_disabled=True)["health_specialists"]}
    assert {
        "list_memory_lanes",
        "consolidate_memory_lane",
        "create_sandbox_copy",
        "run_health_evaluation",
        "list_improvements",
        "propose_improvement",
        "record_finding",
        "run_experiment",
        "run_flow_shadow",
        "record_module_check_report",
    } <= names


@pytest.mark.parametrize("retired", _RETIRED)
def test_a_migrated_agent_holding_a_retired_id_ends_on_the_internal_group(retired):
    migration = _migration()
    assert migration.merged_ids(["web", retired, "health_orchestrator"]) == [
        "web",
        "health_specialists",
        "health_orchestrator",
    ]


def test_several_retired_ids_collapse_to_one():
    migration = _migration()
    assert migration.merged_ids(["health_diagnosis", "improvement_proposals", "concierge_sandbox"]) == [
        "health_specialists"
    ]
    assert migration.merged_ids(["web"]) == ["web"]
    assert migration.merged_ids(None) is None


def test_allow_lists_keyed_by_retired_ids_merge_under_the_internal_group():
    migration = _migration()
    merged = migration.merged_map(
        {"web": ["web_fetch"], "health_diagnosis": ["list_findings"], "improvement_proposals": ["list_improvements"]}
    )
    assert merged == {"web": ["web_fetch"], "health_specialists": ["list_findings", "list_improvements"]}
    # A retired key that restricted nothing leaves the merged key unrestricted.
    assert migration.merged_map({"flow_lab": None, "health_modules": ["list_module_findings"]}) == {
        "health_specialists": None
    }
    assert migration.merged_map({"web": None}) == {"web": None}


class _FakeBind:
    """Answers the SELECT with *rows* and records every UPDATE."""

    def __init__(self, rows):
        self.rows = rows
        self.updates = []

    def execute(self, statement, params=None):
        text = str(statement)
        if text.startswith("SELECT"):
            rows = self.rows

            class _Result:
                def fetchall(self):
                    return rows

            return _Result()
        self.updates.append((text, params))
        return None


def test_a_json_null_id_list_is_skipped_not_decoded():
    """A JSON column holding the literal ``null`` passes ``IS NOT NULL`` and reads as None (#1943 outage)."""
    migration = _migration()
    bind = _FakeBind([(1, None), (2, "null"), (3, '"web"'), (4, "not json"), (5, ["health_diagnosis"])])
    migration.rewrite_rows(bind, "agents", "builtin_tool_ids", list, migration.merged_ids)
    assert len(bind.updates) == 1
    assert bind.updates[0][1] == {"v": '["health_specialists"]', "id": 5}


def test_a_json_null_allow_list_is_skipped_not_decoded():
    migration = _migration()
    bind = _FakeBind([(1, None), (2, "null"), (3, "[]"), (4, '{"flow_lab": null}')])
    migration.rewrite_rows(bind, "assignments", "builtin_allowed_tools", dict, migration.merged_map)
    assert len(bind.updates) == 1
    assert bind.updates[0][1] == {"v": '{"health_specialists": null}', "id": 4}
