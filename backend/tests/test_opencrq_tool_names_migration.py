"""The OpenCRQ tool names persisted before the provider was aligned move with it.

Eleven tools changed name and seventeen left. A tool name is stored in an
agent's deny list, in the allow lists of agents and assignments, in an
administrator's approval overrides and in a user's standing approvals, and a
stale one is not harmless: in ``agents.tool_deny_list`` and in an approval
``remove`` override it fails **open**, because both match literal names.

The revision's source is read, and the expected values are spelled as
literals: a migration must keep meaning what it meant on the day it ran, so
neither side imports the provider's or the app's current constants. The
behavioural tests load the revision from its file, like
``test_opencrq_rename_conflict_postgres.py``, and run its Python against a
recording connection; the SQL itself runs on CI (``Migrations (Alembic)``).
"""

from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import re

import pytest

REVISION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "x5762ocqtool01_align_opencrq_tool_names.py"
)

RENAMES = {
    "list_opencrq_assessments": "list_opencrq_compliance_assessments",
    "get_opencrq_assessment": "get_opencrq_compliance_assessment",
    "create_opencrq_assessment": "create_opencrq_compliance_assessment",
    "update_opencrq_assessment": "manage_opencrq_compliance_assessment",
    "update_opencrq_requirement_assessment": "update_opencrq_requirement_answer",
    "list_opencrq_controls": "list_opencrq_control_capabilities",
    "get_opencrq_control": "get_opencrq_control_capability",
    "update_opencrq_control_maturity": "update_opencrq_control_capability_assessment",
    "get_opencrq_control_maturity_history": "get_opencrq_control_capability_history",
    "get_opencrq_tenant_cve_posture": "list_opencrq_unique_findings",
    "list_opencrq_control_requirement_mappings": "list_opencrq_drata_controls",
}

REMOVED = (
    "get_opencrq_control_settings",
    "update_opencrq_control_settings",
    "add_opencrq_assessment_evidence",
    "update_opencrq_assessment_evidence",
    "delete_opencrq_assessment_evidence",
    "list_opencrq_assessment_evidences",
    "get_opencrq_requirement_assessment_history",
    "get_opencrq_suggested_controls",
    "list_opencrq_attack_surfaces",
    "update_opencrq_attack_surface",
    "create_opencrq_loss_driver",
    "update_opencrq_loss_driver",
    "delete_opencrq_loss_drivers",
    "list_opencrq_asset_families",
    "manage_opencrq_asset_family",
    "get_opencrq_security_needs",
    "update_opencrq_security_needs",
)

#: Every column the revision rewrites, as ``(table, column)``.
COVERED = (
    ("agents", "tool_deny_list"),
    ("agents", "integration_allowed_tools"),
    ("agents", "mcp_allowed_tools"),
    ("assignments", "integration_allowed_tools"),
    ("agents", "prepare_tool_ref"),
    ("agents", "finalize_tool_ref"),
    ("hitl_whitelist_overrides", "tool_key"),
    ("tool_approval_preferences", "tool_key"),
)

#: Tool-flavoured columns reviewed and left alone, with why. A new column
#: whose name mentions a tool is neither here nor in COVERED until someone
#: decides, which is the point of ``test_every_tool_named_column_is_decided``.
REVIEWED_ELSEWHERE = {
    ("agent_frictions", "tool"): "a run's friction ledger: the tool met at the time, history, never resolved to a provider tool",
    ("agent_skill_package_origins", "custom_tool_id"): "an id",
    ("agents", "custom_tool_ids"): "ids",
    ("agents", "builtin_tool_ids"): "built-in tool ids, not provider tool names",
    ("agents", "builtin_allowed_tools"): "built-in group ids to built-in function names, not provider tool names",
    ("assignments", "builtin_allowed_tools"): "built-in group ids to built-in function names, not provider tool names",
    ("platform_builds", "builtin_tools"): "the built-in group ids and function names a build ships (#12192), not provider tool names",
    ("integrations", "tool_slug"): "the suffix of an instance's tool names, not a tool name",
    ("agents", "allow_self_fanout"): "a flag",
    ("agents", "approve_tool_calls"): "a flag",
    ("agents", "catalog_include_personal_tools"): "a flag",
    ("assignments", "tool_sources"): "MCP server names or ids",
    ("conversations", "tool_overrides_json"): "built-in, custom and integration ids",
    ("evaluation_runs", "require_tool_approval"): "a flag",
    ("evaluations", "require_tool_approval"): "a flag",
    ("fleets", "enforce_gates_at_tool_level"): "a flag (#1467)",
    ("general_assistant_config", "custom_tool_ids"): "ids",
    ("general_assistant_config", "builtin_tool_ids"): "built-in tool ids",
    ("integration_openapi_specs", "tool_prefix"): "an OpenAPI spec's own prefix",
    ("integration_openapi_specs", "tool_definitions"): "an OpenAPI spec's own tools",
    ("integration_openapi_specs", "active_tools"): "an OpenAPI spec's own tools",
    ("integration_openapi_specs", "tool_overrides"): "an OpenAPI spec's own tools",
    ("llm_spans", "tool_call_id"): "trace history",
    ("llm_spans", "tool_arguments"): "trace history",
    ("mcp_servers", "discovered_tools"): "a cache the next discovery refreshes",
    ("mcp_servers", "tools_discovered_at"): "a timestamp",
    ("mcp_servers", "investigation_tool_name"): "an MCP server's own tool",
    ("messages", "tool_calls_json"): "conversation history",
    ("messages", "tool_call_id"): "conversation history",
    ("messages", "tool_name"): "conversation history",
    ("platform_settings", "tool_approvals_enabled"): "a flag",
    ("platform_settings", "approve_tool_calls_default"): "a flag",
    ("tool_approval_preferences", "tool_source"): "a source label, not a tool name",
    ("tool_approval_preferences", "tool_entity_id"): "an MCP server's id, not a tool name",
    ("users", "ga_builtin_tool_ids"): "built-in tool ids",
    ("users", "ga_custom_tool_ids"): "ids",
    ("users", "approve_all_tools"): "a flag",
}


@pytest.fixture(scope="module")
def source() -> str:
    return REVISION.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def revision():
    spec = importlib.util.spec_from_file_location("x5762ocqtool01", REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _literal(source: str, name: str):
    for node in ast.parse(source).body:
        target = None
        if isinstance(node, ast.AnnAssign):
            target = node.target
        elif isinstance(node, ast.Assign):
            target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == name:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not assigned in the revision")


class TestTheMapping:
    def test_the_rename_table_is_the_expected_one(self, source):
        assert _literal(source, "_RENAMES") == RENAMES

    def test_the_removed_names_are_the_expected_ones(self, source):
        assert tuple(_literal(source, "_REMOVED")) == REMOVED

    def test_the_counts(self):
        assert len(RENAMES) == 11
        assert len(REMOVED) == len(set(REMOVED)) == 17

    def test_no_name_is_in_two_roles(self):
        old, new, removed = set(RENAMES), set(RENAMES.values()), set(REMOVED)
        assert len(new) == len(RENAMES), "two tools renamed onto one: a rename is no longer reversible"
        assert not old & new, "a new name is also an old one: a rewrite would chain"
        assert not removed & new
        assert not old & removed

    def test_the_downgrade_undoes_the_renames_only(self, source):
        body = source.split("def downgrade() -> None:")[1]
        assert "{new: old for old, new in _RENAMES.items()}" in body
        assert "_REMOVED" not in body, "removed names are not recreated"
        assert "drop_on_collision=False" in body


class TestTheProviderToday:
    """The table is only worth anything if it matches what the provider serves."""

    @staticmethod
    def _served() -> set[str]:
        from app.integrations.providers.opencrq import build_tools
        from app.tools.base import CallableToolExecutor

        return {t.name for t in build_tools("https://demo.opencrq.io||test-token", CallableToolExecutor())}

    def test_every_new_name_is_served(self):
        missing = set(RENAMES.values()) - self._served()
        assert not missing, f"renamed onto a tool the provider does not serve: {sorted(missing)}"

    def test_no_old_or_removed_name_is_served(self):
        stale = (set(RENAMES) | set(REMOVED)) & self._served()
        assert not stale, f"the provider still serves {sorted(stale)}: its stored name would be rewritten under it"


class TestEveryToolNameColumnIsCovered:
    @pytest.mark.parametrize(("table", "column"), COVERED)
    def test_column_is_named_in_the_revision(self, source, table, column):
        assert f'"{table}", "{column}"' in source, (
            f"{table}.{column} stores tool names but this revision never rewrites it"
        )

    def test_the_covered_columns_exist(self):
        """Guards COVERED from rotting into a tautology."""
        import app.models  # noqa: F401
        from app.core.database import Base

        for table, column in COVERED:
            assert table in Base.metadata.tables, f"{table} no longer exists"
            assert column in Base.metadata.tables[table].columns, f"{table}.{column} no longer exists"

    def test_every_tool_named_column_is_decided(self):
        """A column added later, named after tools, must be covered or reviewed.

        A stale name in a deny list or an approval override fails open, and a
        stale one in an allow list silently drops a tool: neither shows until
        someone notices a tool behaving differently.
        """
        import app.models  # noqa: F401
        from app.core.database import Base

        decided = {*COVERED, *REVIEWED_ELSEWHERE}
        undecided = sorted(
            f"{table.name}.{column.name}"
            for table in Base.metadata.tables.values()
            for column in table.columns
            if re.search(r"tool|whitelist", column.name) and (table.name, column.name) not in decided
        )
        assert not undecided, (
            f"{undecided} look like they store tool names. If they hold OpenCRQ tool names, "
            f"rewrite them in a revision like x5762ocqtool01; otherwise list them in "
            f"REVIEWED_ELSEWHERE with the reason."
        )

    def test_the_reviewed_columns_still_exist(self):
        import app.models  # noqa: F401
        from app.core.database import Base

        gone = [
            f"{t}.{c}"
            for t, c in REVIEWED_ELSEWHERE
            if t not in Base.metadata.tables or c not in Base.metadata.tables[t].columns
        ]
        assert not gone, f"{gone} no longer exist: drop them from REVIEWED_ELSEWHERE"


class TestItIsSafeToRunTwice:
    def test_rewrites_only_touch_rows_naming_a_stale_name(self, source):
        assert "AND CAST({column} AS text) ~ :pattern" in source
        assert '{column} ~ :pattern"' in source

    def test_writes_are_batched(self, source):
        assert '"ORDER BY id LIMIT :batch"' in source
        assert '"batch": _BATCH' in source

    def test_it_never_casts_to_jsonb(self, source):
        # A json column may hold what a ::jsonb cast refuses; the
        # rows are read as text and parsed in Python.
        assert "jsonb" not in source.lower().split('"""', 2)[2]

    def test_it_takes_no_ddl_lock(self, source):
        code = source.split('"""', 2)[2]
        assert not re.search(r"\b(ALTER|CREATE|DROP|TRUNCATE|LOCK)\s", code)

    def test_an_unreadable_deny_list_stops_the_upgrade(self, source):
        assert '_DENYING: frozenset[tuple[str, str]] = frozenset({("agents", "tool_deny_list")})' in source
        assert "raise RuntimeError(" in source


# -- behaviour, on the revision's own Python ---------------------------------


class TestRewritingAList:
    def test_a_name_is_renamed_in_place(self, revision):
        out = revision._rewrite_list(["a", "get_opencrq_control", "b"], RENAMES, REMOVED)
        assert out == ["a", "get_opencrq_control_capability", "b"]

    def test_a_removed_name_is_dropped(self, revision):
        assert revision._rewrite_list(["x", "list_opencrq_asset_families"], RENAMES, REMOVED) == ["x"]

    def test_the_instance_suffix_is_kept(self, revision):
        out = revision._rewrite_list(
            ["get_opencrq_control__acme", "update_opencrq_loss_driver__acme"], RENAMES, REMOVED
        )
        assert out == ["get_opencrq_control_capability__acme"]

    def test_a_name_that_only_starts_like_a_stale_one_is_left_alone(self, revision):
        names = ["get_opencrq_control_capability", "get_opencrq_controller", "list_opencrq_controls_x"]
        assert revision._rewrite_list(names, RENAMES, REMOVED) is None

    def test_a_target_already_listed_is_not_duplicated(self, revision):
        out = revision._rewrite_list(
            ["get_opencrq_control_capability", "get_opencrq_control"], RENAMES, REMOVED
        )
        assert out == ["get_opencrq_control_capability"]

    def test_two_stale_names_never_leave_a_duplicate(self, revision):
        out = revision._rewrite_list(["get_opencrq_control", "get_opencrq_control"], RENAMES, REMOVED)
        assert out == ["get_opencrq_control_capability"]

    def test_a_list_only_naming_removed_tools_becomes_empty_not_missing(self, revision):
        assert revision._rewrite_list(["get_opencrq_security_needs"], RENAMES, REMOVED) == []

    def test_an_empty_list_is_untouched(self, revision):
        assert revision._rewrite_list([], RENAMES, REMOVED) is None

    def test_a_second_pass_changes_nothing(self, revision):
        once = revision._rewrite_list(["get_opencrq_control", "get_opencrq_security_needs"], RENAMES, REMOVED)
        assert revision._rewrite_list(once, RENAMES, REMOVED) is None

    def test_non_string_elements_survive(self, revision):
        assert revision._rewrite_list([1, None, "get_opencrq_control"], RENAMES, REMOVED) == [
            1,
            None,
            "get_opencrq_control_capability",
        ]

    def test_the_downgrade_direction(self, revision):
        reverse = {new: old for old, new in RENAMES.items()}
        assert revision._rewrite_list(["list_opencrq_drata_controls__a"], reverse, ()) == [
            "list_opencrq_control_requirement_mappings__a"
        ]


class TestRewritingAnAllowList:
    @staticmethod
    def _keys_ok(provider_keyed: bool):
        return lambda key: not provider_keyed or key == "opencrq" or key.startswith("opencrq@")

    def test_an_emptied_list_stays_empty_and_its_key_stays(self, revision):
        value = {"opencrq": ["get_opencrq_security_needs"], "github": None}
        out = revision._rewrite_object_values(value, RENAMES, REMOVED, self._keys_ok(True))
        assert out == {"opencrq": [], "github": None}

    def test_an_already_empty_list_is_not_touched(self, revision):
        value = {"opencrq": []}
        assert revision._rewrite_object_values(value, RENAMES, REMOVED, self._keys_ok(True)) is None

    def test_only_opencrq_keys_of_a_provider_keyed_column_are_read(self, revision):
        value = {
            "opencrq": ["get_opencrq_control"],
            "opencrq@1234": ["get_opencrq_control"],
            "custom:mine": ["get_opencrq_control"],
        }
        out = revision._rewrite_object_values(value, RENAMES, REMOVED, self._keys_ok(True))
        assert out == {
            "opencrq": ["get_opencrq_control_capability"],
            "opencrq@1234": ["get_opencrq_control_capability"],
            "custom:mine": ["get_opencrq_control"],
        }

    def test_every_key_of_an_mcp_keyed_column_is_read(self, revision):
        value = {"5b1d": ["get_opencrq_control"], "7c2e": ["other"]}
        out = revision._rewrite_object_values(value, RENAMES, REMOVED, self._keys_ok(False))
        assert out == {"5b1d": ["get_opencrq_control_capability"], "7c2e": ["other"]}

    def test_a_value_that_is_not_an_object_is_left_alone(self, revision):
        assert revision._rewrite_object_values(["get_opencrq_control"], RENAMES, REMOVED, lambda _k: True) is None


class _Result:
    def __init__(self, rows=(), scalar=None, first=None):
        self._rows, self._scalar, self._first = list(rows), scalar, first

    def all(self):
        return self._rows

    def scalar(self):
        return self._scalar

    def first(self):
        return self._first


class _Conn:
    """Answers the revision's SELECTs from fixtures and records what it writes."""

    def __init__(self, rows, taken=False, target=None):
        self.rows, self.taken, self.target = rows, taken, target
        self.writes, self.selects = [], []

    def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        if "information_schema" in sql:
            return _Result(scalar=1)
        if sql.startswith("SELECT id,"):
            self.selects.append((sql, params))
            return _Result([] if "after" in params else self.rows)
        if sql.startswith("SELECT 1 FROM"):
            return _Result(scalar=1 if self.taken else None)
        if sql.startswith("SELECT action, id FROM"):
            return _Result(first=self.target)
        self.writes.append((sql.split(" WHERE")[0], params))
        return _Result()


class TestTheJsonColumnRewrite:
    def _run(self, revision, stored, *, table="agents", column="tool_deny_list", rewrite=None):
        conn = _Conn([("row-1", stored)])
        rewrite = rewrite or (lambda v: revision._rewrite_list(v, RENAMES, REMOVED))
        revision._rewrite_json_column(conn, table, column, rewrite, RENAMES, REMOVED)
        return conn

    def test_a_stale_list_is_written_back_as_json(self, revision):
        conn = self._run(revision, '["get_opencrq_control", "keep"]')
        (sql, params), = conn.writes
        assert sql == "UPDATE agents SET tool_deny_list = CAST(:value AS json)"
        assert json.loads(params["value"]) == ["get_opencrq_control_capability", "keep"]
        assert params["id"] == "row-1"

    def test_the_candidate_query_matches_whole_names_and_their_suffix(self, revision):
        conn = self._run(revision, "[]")
        pattern = conn.selects[0][1]["pattern"]
        assert re.search(pattern, '["get_opencrq_control"]')
        assert re.search(pattern, '["x", "get_opencrq_control__acme"]')
        assert not re.search(pattern, '["get_opencrq_control_capability"]')
        assert not re.search(pattern, '["get_opencrq_controller"]')

    def test_a_row_with_nothing_to_rewrite_is_not_written(self, revision):
        assert self._run(revision, '["a"]').writes == []

    def test_a_lone_surrogate_survives_the_round_trip(self, revision):
        conn = self._run(revision, '["get_opencrq_control", "\\ud800"]')
        assert conn.writes[0][1]["value"].isascii()
        assert "\\ud800" in conn.writes[0][1]["value"]

    def test_an_unreadable_deny_list_stops_the_upgrade(self, revision):
        with pytest.raises(RuntimeError, match="cannot be read"):
            self._run(revision, "[1e999999999999]", rewrite=lambda v: [float("nan")])

    def test_an_unreadable_allow_list_is_left_alone(self, revision, capsys):
        conn = self._run(
            revision,
            "[1e999999999999]",
            table="agents",
            column="mcp_allowed_tools",
            rewrite=lambda v: [float("nan")],
        )
        assert conn.writes == []
        assert "unchanged" in capsys.readouterr().out


class TestTheKeyRewrites:
    def test_overrides_rename_delete_and_keep_the_suffix(self, revision):
        conn = _Conn(
            [
                ("1", "get_opencrq_control", "remove"),
                ("2", "update_opencrq_loss_driver", "add"),
                ("3", "get_opencrq_control__acme", "remove"),
            ]
        )
        revision._rekey_overrides(conn, RENAMES, REMOVED, drop_on_collision=True)
        assert [(sql.split(" ")[0], p) for sql, p in conn.writes] == [
            ("UPDATE", {"new": "get_opencrq_control_capability", "id": "1"}),
            ("DELETE", {"id": "2"}),
            ("UPDATE", {"new": "get_opencrq_control_capability__acme", "id": "3"}),
        ]

    @pytest.mark.parametrize("stale, current", [("add", "add"), ("add", "remove"), ("remove", "remove")])
    def test_a_colliding_override_yields_to_one_at_least_as_strict(self, revision, capsys, stale, current):
        conn = _Conn([("1", "get_opencrq_control", stale)], target=(current, "9"))
        revision._rekey_overrides(conn, RENAMES, REMOVED, drop_on_collision=True)
        assert [(s.split(" ")[0], p) for s, p in conn.writes] == [("DELETE", {"id": "1"})]
        assert f"kept the stricter one ({current})" in capsys.readouterr().out

    def test_a_colliding_gate_replaces_the_exemption_under_the_new_name(self, revision, capsys):
        # An exemption under the new name must not lift the gate set under the old one.
        conn = _Conn([("1", "get_opencrq_control", "remove")], target=("add", "9"))
        revision._rekey_overrides(conn, RENAMES, REMOVED, drop_on_collision=True)
        assert [(s.split(" ")[0], p) for s, p in conn.writes] == [
            ("DELETE", {"id": "9"}),
            ("UPDATE", {"new": "get_opencrq_control_capability", "id": "1"}),
        ]
        assert "kept the stricter one (remove)" in capsys.readouterr().out

    def test_an_override_whose_target_exists_is_kept_on_downgrade(self, revision):
        conn = _Conn([("1", "get_opencrq_control_capability", "add")], target=("remove", "9"))
        revision._rekey_overrides(
            conn, {n: o for o, n in RENAMES.items()}, (), drop_on_collision=False
        )
        assert conn.writes == []

    def test_an_active_preference_colliding_with_the_new_name_is_deleted(self, revision, capsys):
        conn = _Conn([("1", "get_opencrq_control", "user-1", None)], taken=True)
        revision._rekey_preferences(conn, RENAMES, REMOVED, drop_on_collision=True)
        assert [(s.split(" ")[0], p) for s, p in conn.writes] == [("DELETE", {"id": "1"})]
        assert "kept the one under get_opencrq_control_capability" in capsys.readouterr().out

    def test_a_revoked_preference_is_renamed_even_when_the_new_name_is_taken(self, revision):
        # The unique index only binds active rows.
        conn = _Conn([("1", "get_opencrq_control", "user-1", "2026-10-01")], taken=True)
        revision._rekey_preferences(conn, RENAMES, REMOVED, drop_on_collision=True)
        assert [(s.split(" ")[0], p) for s, p in conn.writes] == [
            ("UPDATE", {"new": "get_opencrq_control_capability", "id": "1"})
        ]

    def test_a_removed_preference_is_deleted_active_or_not(self, revision):
        conn = _Conn(
            [
                ("1", "get_opencrq_security_needs", "u", None),
                ("2", "get_opencrq_security_needs", "u", "2026-10-01"),
            ]
        )
        revision._rekey_preferences(conn, RENAMES, REMOVED, drop_on_collision=True)
        assert [(s.split(" ")[0], p) for s, p in conn.writes] == [
            ("DELETE", {"id": "1"}),
            ("DELETE", {"id": "2"}),
        ]

    def test_the_fingerprint_is_never_written(self, source):
        assert "schema_fingerprint" not in source.split('"""', 2)[2]

    def test_the_row_pattern_matches_whole_keys_and_their_suffix(self, revision):
        conn = _Conn([])
        revision._rekey_overrides(conn, RENAMES, REMOVED, drop_on_collision=True)
        pattern = conn.selects[0][1]["pattern"]
        assert re.search(pattern, "get_opencrq_control")
        assert re.search(pattern, "get_opencrq_control__acme")
        assert not re.search(pattern, "get_opencrq_control_capability")
        assert not re.search(pattern, "xget_opencrq_control")


class TestThePipelineRefs:
    @staticmethod
    def _ref(name, provider="opencrq", **extra):
        return {"source": "integration", "provider": provider, "tool_name": name, **extra}

    def test_a_renamed_tool_keeps_the_rest_of_the_ref(self, revision):
        ref = self._ref("list_opencrq_controls", integration_id="1234")
        assert revision._rewrite_tool_ref(ref, RENAMES, REMOVED) == self._ref(
            "list_opencrq_control_capabilities", integration_id="1234"
        )

    def test_a_removed_tool_clears_the_ref(self, revision):
        assert revision._rewrite_tool_ref(self._ref("list_opencrq_attack_surfaces"), RENAMES, REMOVED) is revision._DROP

    def test_only_an_opencrq_ref_is_read(self, revision):
        for name in ("list_opencrq_controls", "list_opencrq_attack_surfaces"):
            assert revision._rewrite_tool_ref(self._ref(name, provider="github"), RENAMES, REMOVED) is None
        assert revision._rewrite_tool_ref({"source": "custom_tool", "tool_name": "list_opencrq_controls"}, RENAMES, REMOVED) is None

    def test_a_current_or_malformed_ref_is_left_alone(self, revision):
        assert revision._rewrite_tool_ref(self._ref("list_opencrq_tenants"), RENAMES, REMOVED) is None
        assert revision._rewrite_tool_ref(self._ref(None), RENAMES, REMOVED) is None
        assert revision._rewrite_tool_ref(["list_opencrq_controls"], RENAMES, REMOVED) is None

    def test_the_suffix_is_kept(self, revision):
        out = revision._rewrite_tool_ref(self._ref("get_opencrq_control__acme"), RENAMES, REMOVED)
        assert out["tool_name"] == "get_opencrq_control_capability__acme"

    def test_a_second_pass_changes_nothing(self, revision):
        once = revision._rewrite_tool_ref(self._ref("get_opencrq_control"), RENAMES, REMOVED)
        assert revision._rewrite_tool_ref(once, RENAMES, REMOVED) is None

    def test_the_downgrade_direction_renames_and_never_clears(self, revision):
        reverse = {new: old for old, new in RENAMES.items()}
        assert revision._rewrite_tool_ref(self._ref("list_opencrq_unique_findings"), reverse, ())["tool_name"] == (
            "get_opencrq_tenant_cve_posture"
        )
        assert revision._rewrite_tool_ref(self._ref("list_opencrq_attack_surfaces"), reverse, ()) is None

    def _run(self, revision, stored, column="prepare_tool_ref"):
        conn = _Conn([("row-1", stored)])
        revision._rewrite_json_column(
            conn,
            "agents",
            column,
            lambda value: revision._rewrite_tool_ref(value, RENAMES, REMOVED),
            RENAMES,
            REMOVED,
            tool_ref=True,
        )
        return conn

    def test_a_renamed_ref_is_written_back(self, revision):
        conn = self._run(revision, json.dumps(self._ref("get_opencrq_control")), "finalize_tool_ref")
        (sql, params), = conn.writes
        assert sql == "UPDATE agents SET finalize_tool_ref = CAST(:value AS json)"
        assert json.loads(params["value"])["tool_name"] == "get_opencrq_control_capability"

    def test_a_removed_ref_is_set_to_null(self, revision):
        conn = self._run(revision, json.dumps(self._ref("update_opencrq_security_needs")))
        assert conn.writes == [("UPDATE agents SET prepare_tool_ref = NULL", {"id": "row-1"})]

    def test_a_ref_of_another_provider_is_not_written(self, revision):
        assert self._run(revision, json.dumps(self._ref("get_opencrq_control", provider="x"))).writes == []

    def test_the_candidate_query_matches_the_tool_name_value_only(self, revision):
        pattern = self._run(revision, "{}").selects[0][1]["pattern"]
        assert re.search(pattern, json.dumps(self._ref("get_opencrq_control")))
        assert re.search(pattern, '{"tool_name":"get_opencrq_control__a"}')
        assert re.search(pattern, '{"provider": "opencrq", "tool_name" : "get_opencrq_control"}')
        assert not re.search(pattern, json.dumps(self._ref("get_opencrq_control_capability")))
        assert not re.search(pattern, '{"description": "get_opencrq_control"}')

    def test_an_unreadable_ref_is_left_alone_and_reported(self, revision, capsys):
        conn = _Conn([("row-1", "{}")])
        revision._rewrite_json_column(
            conn, "agents", "prepare_tool_ref", lambda v: {"x": float("nan")}, RENAMES, REMOVED, tool_ref=True
        )
        assert conn.writes == []
        assert "unchanged" in capsys.readouterr().out
