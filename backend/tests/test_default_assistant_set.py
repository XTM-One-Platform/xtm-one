"""One default built-in set for the General Assistant and the CTEM Assistant."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from app.agents.builtin_agents import BUILTIN_AGENTS
from app.models.general_assistant_config import (
    ADMIN_ONLY_GA_BUILTIN_TOOL_IDS,
    DEFAULT_GA_BUILTIN_TOOL_IDS,
    GENERAL_ASSISTANT_BUILTIN_TOOL_IDS,
    general_assistant_turn_tools,
)
from app.services.builtin_tool_aliases import PLATFORM_ADMIN_FUNCTIONS, alias_group, caller_may_run

#: The default set as groups (the consolidation assessment, section 8, with
#: Sam's decision 2: the platform group stays on by default).
PINNED_GROUPS = {
    "platform",
    "files",
    "generate_presentation",
    "generate_report",
    "video_studio",
    "custom_dashboards",
    "design",
    "code_interpreter",
    "web",
    "cti",
    "platform_insights",
}


def _groups(ids):
    return {alias_group(tool_id) or tool_id for tool_id in ids}


def _ctem():
    return next(agent for agent in BUILTIN_AGENTS if agent["slug"] == "ctem-assistant")


def _migration():
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "q8317gadflt_add_sandbox_and_cti_to_ga_tools.py"
    spec = importlib.util.spec_from_file_location("q8317gadflt", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_default_set_is_pinned():
    assert _groups(DEFAULT_GA_BUILTIN_TOOL_IDS) == PINNED_GROUPS


def test_the_ctem_assistant_carries_exactly_the_default_set():
    assert list(_ctem()["builtin_tool_ids"]) == list(DEFAULT_GA_BUILTIN_TOOL_IDS)


def test_the_general_assistant_offers_every_default_tool():
    assert set(DEFAULT_GA_BUILTIN_TOOL_IDS) <= set(GENERAL_ASSISTANT_BUILTIN_TOOL_IDS)


def test_a_non_admin_turn_drops_platform_insights_and_the_admin_platform_functions():
    turn = general_assistant_turn_tools(list(DEFAULT_GA_BUILTIN_TOOL_IDS), is_admin=False)
    assert not set(turn) & ADMIN_ONLY_GA_BUILTIN_TOOL_IDS
    # The platform group stays (decision 2), its admin-only functions are not offered.
    assert "agent_builder" in turn
    assert not any(caller_may_run(fn, is_admin=False) for fn in PLATFORM_ADMIN_FUNCTIONS)
    assert all(caller_may_run(fn, is_admin=True) for fn in PLATFORM_ADMIN_FUNCTIONS)


def test_the_migration_adds_what_the_default_set_gained():
    migration = _migration()
    assert set(migration.ADDED_TOOL_IDS) <= set(DEFAULT_GA_BUILTIN_TOOL_IDS)
    for tool_id, group in migration.ADDED_TOOL_IDS.items():
        assert (alias_group(tool_id) or tool_id) == group


def test_the_migration_appends_the_missing_tools_once():
    migration = _migration()
    stored = json.dumps(["generate_file", "agent_builder"])
    added = json.loads(migration.with_added_tools(stored))
    assert added == ["generate_file", "agent_builder", "code_interpreter", "stix_bundle_validator", "fimi_analytics"]
    assert migration.with_added_tools(json.dumps(added)) is None


def test_the_migration_reads_the_group_form():
    migration = _migration()
    assert migration.with_added_tools(json.dumps(["cti", "code_interpreter"])) is None
    assert json.loads(migration.with_added_tools(json.dumps(["cti"]))) == ["cti", "code_interpreter"]


def test_the_migration_reads_null_and_ignores_garbage():
    migration = _migration()
    assert json.loads(migration.with_added_tools(None)) == list(migration.ADDED_TOOL_IDS)
    assert migration.with_added_tools("null") is not None
    assert migration.with_added_tools("{}") is None
    assert migration.with_added_tools("not json") is None


def test_the_migration_revises_the_previous_head():
    migration = _migration()
    assert migration.revision == "q8317gadflt"
    assert migration.down_revision == "p8270platgrp"


def _dashboards_migration():
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "s1925gadash01_add_custom_dashboards_to_ga_tools.py"
    spec = importlib.util.spec_from_file_location("s1925gadash01", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_dashboards_migration_adds_the_dashboard_tool_once():
    migration = _dashboards_migration()
    assert set(migration.ADDED_TOOL_IDS) <= set(DEFAULT_GA_BUILTIN_TOOL_IDS)
    added = json.loads(migration.with_added_tools(json.dumps(["generate_file"])))
    assert added == ["generate_file", "custom_dashboards"]
    assert migration.with_added_tools(json.dumps(added)) is None
    assert json.loads(migration.with_added_tools(None)) == ["custom_dashboards"]
    assert migration.with_added_tools("{}") is None
    assert migration.with_added_tools("not json") is None
