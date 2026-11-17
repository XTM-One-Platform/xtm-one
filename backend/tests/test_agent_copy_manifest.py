"""The agent copy/bundle manifests must cover every ``Agent`` column.

This is the test that makes the next added column fail loudly. Duplication,
export and import each used to carry a hand-written kwargs list, so a new
column defaulted to *silently not copied* — which is how a duplicated OpenCTI
agent lost ``integration_template_bindings`` (and with it every OpenCTI tool)
and how its ``tool_deny_list`` came back empty, re-granting explicitly denied
tools.

If a new ``Agent`` column trips these tests: add it to the copy list and the
portable-bundle list, or to the matching skip mapping with the reason it cannot
travel. Deciding is the point; the failure only asks you to decide.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import agent_copy
from app.models.agent_copy import (
    AGENT_BUNDLE_EXPORT_ONLY_FIELDS,
    AGENT_BUNDLE_PORTABLE_FIELDS,
    AGENT_BUNDLE_SKIP_FIELDS,
    AGENT_CATALOG_CLONE_FIELDS,
    AGENT_CATALOG_CLONE_SKIP_FIELDS,
    AGENT_COPY_FIELDS,
    AGENT_COPY_SKIP_FIELDS,
    ASSIGNMENT_CATALOG_CLONE_FIELDS,
    ASSIGNMENT_CATALOG_CLONE_SKIP_FIELDS,
    ASSIGNMENT_COPY_FIELDS,
    ASSIGNMENT_COPY_SKIP_FIELDS,
    PUBLIC_DOWNLOAD_DROPPED_AGENT_FIELDS,
    REDACTED_MCP_SERVER_ID,
    REDACTED_RUNNER_ID,
    agent_mapped_columns,
    assignment_mapped_columns,
    build_agent_catalog_clone_kwargs,
    build_agent_copy_kwargs,
    build_agent_import_kwargs,
    build_assignment_catalog_clone_kwargs,
    build_assignment_copy_kwargs,
    rekey_id_list,
    rekey_id_map,
    strip_internal_ids_for_public_download,
)

AGENTS_API = Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "agents.py"
AGENT_IMPORT_API = (
    Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "agent_import.py"
)
PUBLIC_CATALOG_API = (
    Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "public_catalog.py"
)
AGENT_BUNDLE_PROJECTION = (
    Path(__file__).resolve().parents[1] / "app" / "services" / "agent_bundle_projection.py"
)


# --- Manifest coverage ------------------------------------------------------


def test_copy_manifest_covers_every_agent_column():
    columns = set(agent_mapped_columns())
    covered = set(AGENT_COPY_FIELDS) | set(AGENT_COPY_SKIP_FIELDS)
    assert columns - covered == set(), (
        "New Agent column(s) are in neither AGENT_COPY_FIELDS nor "
        "AGENT_COPY_SKIP_FIELDS. Duplicating an agent would silently drop them."
    )
    assert covered - columns == set(), "Manifest names a field that is not a column"


def test_copy_manifest_buckets_are_disjoint():
    assert set(AGENT_COPY_FIELDS) & set(AGENT_COPY_SKIP_FIELDS) == set()


def test_bundle_manifest_covers_every_agent_column():
    columns = set(agent_mapped_columns())
    covered = (
        set(AGENT_BUNDLE_PORTABLE_FIELDS)
        | set(AGENT_BUNDLE_EXPORT_ONLY_FIELDS)
        | set(AGENT_BUNDLE_SKIP_FIELDS)
    )
    assert columns - covered == set(), (
        "New Agent column(s) are in none of the bundle manifest buckets. "
        "Exporting and re-importing an agent would silently drop them."
    )
    assert covered - columns == set(), "Manifest names a field that is not a column"


def test_bundle_manifest_buckets_are_disjoint():
    portable = set(AGENT_BUNDLE_PORTABLE_FIELDS)
    export_only = set(AGENT_BUNDLE_EXPORT_ONLY_FIELDS)
    skipped = set(AGENT_BUNDLE_SKIP_FIELDS)
    assert portable & export_only == set()
    assert portable & skipped == set()
    assert export_only & skipped == set()


def test_assignment_manifest_covers_every_assignment_column():
    """Duplicating an agent duplicates its assignments — same discipline."""
    columns = set(assignment_mapped_columns())
    covered = set(ASSIGNMENT_COPY_FIELDS) | set(ASSIGNMENT_COPY_SKIP_FIELDS)
    assert columns - covered == set(), (
        "New Assignment column(s) are in neither ASSIGNMENT_COPY_FIELDS nor "
        "ASSIGNMENT_COPY_SKIP_FIELDS. Duplicating an agent would silently drop "
        "them from every copied assignment."
    )
    assert covered - columns == set(), "Manifest names a field that is not a column"


def test_assignment_manifest_buckets_are_disjoint():
    assert set(ASSIGNMENT_COPY_FIELDS) & set(ASSIGNMENT_COPY_SKIP_FIELDS) == set()


@pytest.mark.parametrize(
    "field",
    [
        # The loop guard: a copy without it can react to its own writes forever.
        "ignore_self_triggered_events",
        # Tri-state refinements whose loss silently WIDENS the copy's reach.
        "runner_ids",
        "integration_element_bindings",
        "hide_runs",
        "manual_trigger_parameters",
    ],
)
def test_fields_lost_by_the_old_assignment_copy_are_copied(field):
    assert field in ASSIGNMENT_COPY_FIELDS


@pytest.mark.parametrize("field", ["run_as_user_id", "run_as_all_users"])
def test_assignment_execution_identity_is_never_inherited(field):
    """``run_as`` is an admin-only company-managed setting.

    Any user who can see a company-managed agent may duplicate it, so carrying
    the execution identity would let the copy run with another user's
    integrations and credentials.
    """
    assert field in ASSIGNMENT_COPY_SKIP_FIELDS


@pytest.mark.parametrize(
    "manifest",
    [
        AGENT_COPY_SKIP_FIELDS,
        AGENT_BUNDLE_EXPORT_ONLY_FIELDS,
        AGENT_BUNDLE_SKIP_FIELDS,
        ASSIGNMENT_COPY_SKIP_FIELDS,
    ],
)
def test_every_skipped_field_states_a_reason(manifest):
    """A skip without a justification is indistinguishable from an omission."""
    for name, reason in manifest.items():
        assert isinstance(reason, str) and len(reason.strip()) > 20, name


def test_no_duplicate_entries_in_ordered_manifests():
    for manifest in (
        AGENT_COPY_FIELDS,
        AGENT_BUNDLE_PORTABLE_FIELDS,
        ASSIGNMENT_COPY_FIELDS,
    ):
        assert len(manifest) == len(set(manifest))


# --- The per-user integration fields ----------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "integration_template_bindings",
        "tool_deny_list",
        "handover_prompt",
        "supported_verticals",
        "human_in_the_loop",
        "human_in_the_loop_prompt",
    ],
)
def test_fields_lost_by_the_old_duplicate_path_are_copied(field):
    assert field in AGENT_COPY_FIELDS


def test_per_user_integration_bindings_are_portable_but_uuid_bindings_are_not():
    """The distinction the import path got wrong: names travel, UUIDs don't."""
    assert "integration_template_bindings" in AGENT_BUNDLE_PORTABLE_FIELDS
    assert "integration_bindings" in AGENT_BUNDLE_SKIP_FIELDS


@pytest.mark.parametrize(
    "field",
    ["tool_deny_list", "integration_allowed_tools", "runner_ids", "coordination_access"],
)
def test_access_narrowing_fields_travel_in_the_bundle(field):
    """Dropping a narrowing field on import silently WIDENS the copy's reach."""
    assert field in AGENT_BUNDLE_PORTABLE_FIELDS


def test_coordination_access_travels_as_its_scopes_never_its_agents():
    """The listed agents name rows of the source; a narrowed scope with nobody
    listed admits the agent alone. In-place copies keep the list: the
    agents still exist there."""
    from app.services.company_managed_writes import portable_coordination_access

    stored = {
        "read": "agents",
        "write": "self",
        "read_agent_ids": ["22222222-2222-2222-2222-222222222222"],
        "write_agent_ids": [],
    }
    assert portable_coordination_access(stored) == {
        "read": "agents",
        "write": "self",
        "read_agent_ids": [],
        "write_agent_ids": [],
    }
    assert portable_coordination_access(None) is None
    assert "coordination_access" in AGENT_COPY_FIELDS
    assert "coordination_access" in AGENT_CATALOG_CLONE_SKIP_FIELDS
    import_source = AGENT_IMPORT_API.read_text(encoding="utf-8")
    assert "coordination_access=portable_coordination_access(" in import_source
    clone_source = PUBLIC_CATALOG_API.read_text(encoding="utf-8")
    assert "coordination_access=portable_coordination_access(" in clone_source


# --- Handler wiring: no second field list may creep back in -----------------


def _agent_call_keywords(path: Path, func_name: str) -> tuple[list[str], bool]:
    """Keyword names (and whether a ``**`` splat is used) of ``Agent(...)``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == func_name
    )
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Agent"
        ):
            names = [kw.arg for kw in node.keywords if kw.arg is not None]
            splat = any(kw.arg is None for kw in node.keywords)
            return names, splat
    raise AssertionError(f"no Agent(...) call in {func_name}")


def test_duplicate_agent_builds_its_kwargs_from_the_manifest():
    # The duplicate route and the fleet fork (#9174) share copy_agent_for_user.
    names, splat = _agent_call_keywords(AGENTS_API, "copy_agent_for_user")
    assert splat, (
        "duplicate_agent must build its kwargs from build_agent_copy_kwargs, "
        "not from a hand-maintained keyword list"
    )
    assert names == [], f"unexpected literal kwargs on Agent(...): {names}"


def test_import_builds_its_kwargs_from_the_manifest():
    names, splat = _agent_call_keywords(AGENT_IMPORT_API, "create_agent_from_bundle")
    assert splat, (
        "create_agent_from_bundle must build its kwargs from "
        "build_agent_import_kwargs, not from a hand-maintained keyword list"
    )
    assert names == [], f"unexpected literal kwargs on Agent(...): {names}"


def test_export_bundle_agent_section_is_manifest_driven():
    """The exported ``agent`` section must not be a third hand-written list."""
    source = AGENT_BUNDLE_PROJECTION.read_text(encoding="utf-8")
    marker = '        "agent": {'
    assert marker in source
    section = source[source.index(marker) : source.index(marker) + 900]
    assert "AGENT_BUNDLE_PORTABLE_FIELDS" in section
    assert "AGENT_BUNDLE_EXPORT_ONLY_FIELDS" in section


# --- build_agent_copy_kwargs / build_agent_import_kwargs --------------------


class _Source:
    """Only the attributes the manifest reads — no ORM machinery needed."""

    def __init__(self, **values):
        for name in AGENT_COPY_FIELDS:
            setattr(self, name, values.get(name))


def test_copy_kwargs_carry_every_manifest_field():
    kwargs = build_agent_copy_kwargs(_Source(persona="p"))
    assert set(kwargs) == set(AGENT_COPY_FIELDS)
    assert kwargs["persona"] == "p"


def test_copy_kwargs_deep_copy_nested_containers():
    source = _Source(
        integration_allowed_tools={"github": ["get_repo"]},
        integration_element_bindings={"github:organization": [{"id": "filigran"}]},
    )
    kwargs = build_agent_copy_kwargs(source)

    kwargs["integration_allowed_tools"]["github"].append("delete_repo")
    kwargs["integration_element_bindings"]["github:organization"][0]["id"] = "other"

    assert source.integration_allowed_tools == {"github": ["get_repo"]}
    assert source.integration_element_bindings == {
        "github:organization": [{"id": "filigran"}]
    }


def test_copy_kwargs_overrides_win():
    kwargs = build_agent_copy_kwargs(_Source(description="original"), description="new")
    assert kwargs["description"] == "new"


def test_copy_kwargs_reject_an_override_that_is_not_a_column():
    with pytest.raises(ValueError, match="Unknown Agent column"):
        build_agent_copy_kwargs(_Source(), integration_template_binding=["opencti"])


def test_import_kwargs_take_only_the_portable_fields_present():
    kwargs = build_agent_import_kwargs(
        {
            "name": "Imported",
            "integration_template_bindings": ["opencti"],
            # Absent-or-null keys are left out so the column default applies.
            "tool_deny_list": None,
            # Not portable: must not reach the new row from the bundle.
            "integration_bindings": {"github": ["a-uuid"]},
            "web_token": "secret",
        }
    )
    assert kwargs["integration_template_bindings"] == ["opencti"]
    assert "tool_deny_list" not in kwargs
    assert "integration_bindings" not in kwargs
    assert "web_token" not in kwargs


def test_import_kwargs_reject_an_override_that_is_not_a_column():
    with pytest.raises(ValueError, match="Unknown Agent column"):
        build_agent_import_kwargs({}, mcp_allowed_tool={})


def test_assignment_copy_kwargs_deep_copy_and_validate_overrides():
    source = SimpleNamespace(
        **{name: None for name in ASSIGNMENT_COPY_FIELDS}
        | {"integration_element_bindings": {"github:organization": [{"id": "a"}]}}
    )
    kwargs = build_assignment_copy_kwargs(source)
    assert set(kwargs) == set(ASSIGNMENT_COPY_FIELDS)

    kwargs["integration_element_bindings"]["github:organization"][0]["id"] = "b"
    assert source.integration_element_bindings == {
        "github:organization": [{"id": "a"}]
    }

    with pytest.raises(ValueError, match="Unknown Assignment column"):
        build_assignment_copy_kwargs(source, runner_id=[])


def test_the_manifest_module_points_at_tests_that_exist():
    """Every ``tests/test_*.py`` the manifest cites must be a real file.

    The whole design leans on "a new column fails the suite", so a docstring
    pointing at a test that does not exist is worse than no pointer: it tells
    the next maintainer the guard is somewhere it is not. Caught in review on
    this very file, where the bundle manifest still named the file it had been
    drafted in.
    """
    module = Path(agent_copy.__file__)
    tests_dir = Path(__file__).parent
    cited = set(re.findall(r"tests/(test_\w+\.py)", module.read_text(encoding="utf-8")))
    assert cited, "the manifest module should say which test enforces it"
    missing = sorted(name for name in cited if not (tests_dir / name).is_file())
    assert missing == [], f"{module.name} cites tests that do not exist: {missing}"


# --- Catalog clone manifest -----------------------------------------
#
# "Add to my account" on a catalog agent and the catalog download carried two
# more hand-written field lists (16 of ~70 Agent columns): a cloned catalog
# agent lost its tool deny list, tool scopes, approval gate and loop guard.

def test_catalog_clone_manifest_covers_every_agent_column():
    columns = set(agent_mapped_columns())
    covered = set(AGENT_CATALOG_CLONE_FIELDS) | set(AGENT_CATALOG_CLONE_SKIP_FIELDS)
    assert columns - covered == set(), (
        "New Agent column(s) are in neither AGENT_CATALOG_CLONE_FIELDS nor "
        "AGENT_CATALOG_CLONE_SKIP_FIELDS. Installing a catalog agent would "
        "silently drop them."
    )
    assert covered - columns == set(), "Manifest names a field that is not a column"


def test_catalog_clone_manifest_buckets_are_disjoint():
    assert set(AGENT_CATALOG_CLONE_FIELDS) & set(AGENT_CATALOG_CLONE_SKIP_FIELDS) == set()


def test_catalog_assignment_manifest_covers_every_assignment_column():
    columns = set(assignment_mapped_columns())
    covered = set(ASSIGNMENT_CATALOG_CLONE_FIELDS) | set(
        ASSIGNMENT_CATALOG_CLONE_SKIP_FIELDS
    )
    assert columns - covered == set(), (
        "New Assignment column(s) are in neither ASSIGNMENT_CATALOG_CLONE_FIELDS "
        "nor ASSIGNMENT_CATALOG_CLONE_SKIP_FIELDS."
    )
    assert covered - columns == set(), "Manifest names a field that is not a column"


def test_catalog_assignment_manifest_buckets_are_disjoint():
    assert (
        set(ASSIGNMENT_CATALOG_CLONE_FIELDS) & set(ASSIGNMENT_CATALOG_CLONE_SKIP_FIELDS)
        == set()
    )


@pytest.mark.parametrize(
    "manifest", [AGENT_CATALOG_CLONE_SKIP_FIELDS, ASSIGNMENT_CATALOG_CLONE_SKIP_FIELDS]
)
def test_every_catalog_skipped_field_states_a_reason(manifest):
    for name, reason in manifest.items():
        assert isinstance(reason, str) and len(reason.strip()) > 20, name


def test_no_duplicate_entries_in_catalog_manifests():
    for manifest in (AGENT_CATALOG_CLONE_FIELDS, ASSIGNMENT_CATALOG_CLONE_FIELDS):
        assert len(manifest) == len(set(manifest))


@pytest.mark.parametrize(
    "field",
    [
        "tool_deny_list",
        "integration_allowed_tools",
        "integration_element_bindings",
        "integration_template_bindings",
        "human_in_the_loop",
        "human_in_the_loop_prompt",
        "runner_ids",
        "runner_labels",
        "builtin_tool_ids",
    ],
)
def test_catalog_clone_carries_every_narrowing_agent_field(field):
    """Dropping a narrowing field widens what a published agent does in the
    installer's account."""
    assert field in AGENT_CATALOG_CLONE_FIELDS


@pytest.mark.parametrize(
    "field",
    [
        "activation_prompt",
        "human_in_the_loop",
        "human_in_the_loop_prompt",
        "ignore_self_triggered_events",
        "dry_run",
        "integration_allowed_tools",
        "integration_element_bindings",
        "runner_ids",
    ],
)
def test_catalog_clone_carries_every_narrowing_assignment_field(field):
    assert field in ASSIGNMENT_CATALOG_CLONE_FIELDS


@pytest.mark.parametrize(
    "field",
    [
        # The publisher's bots and public exposure.
        "channel_config",
        "web_enabled",
        "web_token",
        # Sharing grants.
        "shared_group_ids",
        "shared_group_permissions",
        "shared_user_ids",
        "shared_user_permissions",
        # The publisher's own integrations (re-bound from the installer's choice).
        "integration_bindings",
        # Reach into the publisher's other agents.
        "handover_agent_ids",
        "handover_types",
        "handover_conditions",
        "work_item_owner_agent_ids",
        "trace_supervision_target_agent_ids",
    ],
)
def test_catalog_clone_never_carries_the_publishers_reach(field):
    assert field in AGENT_CATALOG_CLONE_SKIP_FIELDS
    assert field not in AGENT_CATALOG_CLONE_FIELDS


@pytest.mark.parametrize(
    "field", ["run_as_user_id", "run_as_all_users", "channel_config", "integration_bindings"]
)
def test_catalog_assignment_never_carries_identity_or_the_publishers_bots(field):
    assert field in ASSIGNMENT_CATALOG_CLONE_SKIP_FIELDS
    assert field not in ASSIGNMENT_CATALOG_CLONE_FIELDS


def _call_keywords(path: Path, func_name: str, class_name: str) -> tuple[list[str], bool]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == func_name
    )
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == class_name
        ):
            names = [kw.arg for kw in node.keywords if kw.arg is not None]
            splat = any(kw.arg is None for kw in node.keywords)
            return names, splat
    raise AssertionError(f"no {class_name}(...) call in {func_name}")


def test_catalog_clone_builds_the_agent_from_the_manifest():
    names, splat = _call_keywords(PUBLIC_CATALOG_API, "add_catalog_item_to_account", "Agent")
    assert splat and names == [], (
        "add_catalog_item_to_account must build Agent(...) from "
        "build_agent_catalog_clone_kwargs, not from a keyword list"
    )
    source = PUBLIC_CATALOG_API.read_text(encoding="utf-8")
    assert "build_agent_catalog_clone_kwargs(" in source


def test_catalog_clone_builds_assignments_from_the_manifest():
    names, splat = _call_keywords(
        PUBLIC_CATALOG_API, "add_catalog_item_to_account", "Assignment"
    )
    assert splat and names == [], (
        "add_catalog_item_to_account must build Assignment(...) from "
        "build_assignment_catalog_clone_kwargs"
    )
    source = PUBLIC_CATALOG_API.read_text(encoding="utf-8")
    assert "build_assignment_catalog_clone_kwargs(" in source


def _async_function(path: Path, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )


def _called(func: ast.AST) -> set[str]:
    """The names ``func`` calls, as ``name(...)`` or ``module.name(...)``."""
    return {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)
    }


def test_catalog_download_reuses_the_export_builder():
    """The download must not be a second hand-written bundle: it is the export
    bundle through the one reader projection, which an export by a user who
    cannot manage the agent serves too."""
    func = _async_function(PUBLIC_CATALOG_API, "download_catalog_item")
    assert "build_reader_agent_bundle" in _called(func)
    assert {"build_agent_export_bundle", "strip_internal_ids_for_public_download"} <= _called(
        _async_function(AGENT_BUNDLE_PROJECTION, "build_reader_agent_bundle")
    )
    assert {"build_agent_export_bundle", "build_reader_agent_bundle"} <= _called(
        _async_function(AGENTS_API, "_export_bundle_for")
    )
    export_types = [
        value.value
        for node in ast.walk(func)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values, strict=True)
        if isinstance(key, ast.Constant)
        and key.value == "export_type"
        and isinstance(value, ast.Constant)
    ]
    assert "agent" not in export_types, (
        "the agent bundle must come from build_agent_export_bundle"
    )


@pytest.mark.parametrize(
    "field",
    [
        "activation_prompt",
        "human_in_the_loop",
        "ignore_self_triggered_events",
        "integration_allowed_tools",
        "integration_element_bindings",
        "hide_runs",
        "dry_run",
    ],
)
def test_export_bundle_and_import_carry_the_assignment_safety_fields(field):
    """What the download (and every export) hands over, and what the import
    applies: the loop guard and the tool scopes used to be missing from both."""
    export_source = AGENT_BUNDLE_PROJECTION.read_text(encoding="utf-8")
    assert f'"{field}": wf.{field},' in export_source
    names, _ = _call_keywords(AGENT_IMPORT_API, "create_agent_from_bundle", "Assignment")
    assert field in names


class _CatalogSource:
    def __init__(self, **values):
        for name in AGENT_CATALOG_CLONE_FIELDS:
            setattr(self, name, values.get(name))


def test_catalog_clone_kwargs_carry_every_manifest_field_deep_copied():
    source = _CatalogSource(
        tool_deny_list=["github:delete_repo"],
        integration_allowed_tools={"github": ["get_repo"]},
        human_in_the_loop=True,
    )
    kwargs = build_agent_catalog_clone_kwargs(source, name="Mine")
    assert set(kwargs) == set(AGENT_CATALOG_CLONE_FIELDS) | {"name"}
    assert kwargs["tool_deny_list"] == ["github:delete_repo"]
    assert kwargs["human_in_the_loop"] is True
    kwargs["integration_allowed_tools"]["github"].append("delete_repo")
    assert source.integration_allowed_tools == {"github": ["get_repo"]}
    with pytest.raises(ValueError, match="Unknown Agent column"):
        build_agent_catalog_clone_kwargs(source, channel_configs={})


def test_catalog_assignment_kwargs_carry_every_manifest_field():
    source = SimpleNamespace(**{name: None for name in ASSIGNMENT_CATALOG_CLONE_FIELDS})
    source.ignore_self_triggered_events = True
    kwargs = build_assignment_catalog_clone_kwargs(source, enabled=False)
    assert set(kwargs) == set(ASSIGNMENT_CATALOG_CLONE_FIELDS) | {"enabled"}
    assert kwargs["ignore_self_triggered_events"] is True
    with pytest.raises(ValueError, match="Unknown Assignment column"):
        build_assignment_catalog_clone_kwargs(source, run_as_user=None)


def test_rekey_helpers_map_known_ids_and_drop_the_rest():
    id_map = {"a": "A", "b": "B"}
    assert rekey_id_map({"a": ["t1"], "zz": ["t2"]}, id_map) == {"A": ["t1"]}
    assert rekey_id_map({"zz": 1}, id_map) is None
    assert rekey_id_map(None, id_map) is None
    # NULL keeps meaning "inherit"; a list keeps meaning "this subset".
    assert rekey_id_list(None, id_map) is None
    assert rekey_id_list([], id_map) == []
    assert rekey_id_list(["a", "zz", "b"], id_map) == ["A", "B"]
    assert rekey_id_list(["zz"], id_map) == []


def test_public_download_drops_ids_the_import_never_applies():
    bundle = {
        "agent": {
            "name": "Published",
            "tool_deny_list": ["x"],
            "runner_ids": ["11111111-1111-1111-1111-111111111111"],
            "work_item_owner_agent_ids": ["22222222-2222-2222-2222-222222222222"],
            "trace_supervision_target_agent_ids": ["33333333-3333-3333-3333-333333333333"],
            "coordination_access": {
                "read": "all",
                "write": "agents",
                "read_agent_ids": [],
                "write_agent_ids": ["77777777-7777-7777-7777-777777777777"],
            },
        },
        "assignments": [
            {
                "name": "a",
                "read_knowledge_base_ids": ["kb"],
                "write_knowledge_base_ids": [],
                "runner_ids": None,
                "ignore_self_triggered_events": True,
            },
            {"name": "b", "runner_ids": [], "read_knowledge_base_ids": None, "tool_sources": []},
            {
                "name": "c",
                "runner_ids": ["44444444-4444-4444-4444-444444444444"],
                "tool_sources": ["66666666-6666-6666-6666-666666666666", "named-server"],
            },
        ],
    }
    stripped = strip_internal_ids_for_public_download(bundle)

    for key in PUBLIC_DOWNLOAD_DROPPED_AGENT_FIELDS:
        assert key not in stripped["agent"]
    assert stripped["agent"]["tool_deny_list"] == ["x"]
    # The narrowing travels, the publisher's agents it lists do not.
    assert stripped["agent"]["coordination_access"] == {
        "read": "all",
        "write": "agents",
        "read_agent_ids": [],
        "write_agent_ids": [],
    }
    first, second, third = stripped["assignments"]
    assert "read_knowledge_base_ids" not in first and "write_knowledge_base_ids" not in first
    assert first["ignore_self_triggered_events"] is True
    # Runner selectors narrow the scope: redacted, never dropped, and the
    # NULL / [] states are kept as they are.
    assert stripped["agent"]["runner_ids"] == [REDACTED_RUNNER_ID]
    assert first["runner_ids"] is None
    assert second["runner_ids"] == []
    assert third["runner_ids"] == [REDACTED_RUNNER_ID]
    # MCP scopes likewise: a server id is redacted (an empty list would mean
    # "every server of the agent"), a name and an empty list are left alone.
    assert third["tool_sources"] == [REDACTED_MCP_SERVER_ID, "named-server"]
    assert second["tool_sources"] == []
    # The input bundle is not mutated.
    assert bundle["agent"]["runner_ids"] == ["11111111-1111-1111-1111-111111111111"]
    assert "work_item_owner_agent_ids" in bundle["agent"]


def test_a_redacted_runner_id_resolves_to_no_runner():
    """The placeholder keeps the imported agent RESTRICTED (an empty agent list
    would mean "any runner") while matching no real machine."""
    from app.services.builtin_tools.computer_use import filter_eligible_runners

    runner = SimpleNamespace(id="55555555-5555-5555-5555-555555555555", labels=[])
    def _eligible(agent_runner_ids):
        return filter_eligible_runners(
            [runner],
            agent_runner_ids=agent_runner_ids,
            agent_runner_labels=None,
            assignment_runner_ids=None,
        )

    assert _eligible([REDACTED_RUNNER_ID]) == []
    assert _eligible([]) == [runner]
