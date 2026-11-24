"""Tests for the OpenCTI provider package and its consolidated management tools.

Covers the package refactor (build_tools still exposes the legacy tools) and
the new domain tools: presence, valid JSON schemas, draft-gating exemption,
and executor dispatch smoke (early-return paths, no network).
"""

import json

import pytest

from app.integrations.providers.opencti import build_tools
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.opencti.io||test-token"

# The consolidated management tools added on top of the legacy STIX tools.
EXPECTED_NEW_TOOLS = {
    "manage_opencti_playbook",
    "edit_opencti_playbook_graph",
    "manage_opencti_workspace",
    "manage_opencti_user",
    "manage_opencti_group",
    "manage_opencti_role",
    "list_opencti_capabilities",
    "manage_opencti_profile",
    "manage_opencti_notifier",
    "manage_opencti_trigger",
    "manage_opencti_notification",
    "manage_opencti_entity_setting",
    "manage_opencti_vocabulary",
    "manage_opencti_status_template",
    "list_opencti_subtypes",
    "manage_opencti_decay_rule",
    "manage_opencti_retention_rule",
    "manage_opencti_exclusion_list",
    "query_opencti_activity",
    "manage_opencti_pir",
    "query_opencti_pir_entities",
    "query_opencti_pir_relationships",
    "manage_opencti_ingestion",
    "manage_opencti_form",
    "manage_opencti_data_sharing",
    "query_opencti_catalog",
    "manage_opencti_connector",
}

# A few legacy tools that must still be present after the package refactor.
LEGACY_SAMPLE = {
    "search_opencti",
    "create_opencti_indicator",
    "get_opencti_container_full",
    "validate_nlq_filter",
    "create_opencti_draft",
}


def _build(scope_headers=None, autonomous=False):
    executor = CallableToolExecutor()
    tools = build_tools(
        TOKEN,
        executor,
        scope_headers=scope_headers or {},
        autonomous=autonomous,
    )
    return executor, tools


def test_new_tools_present_and_legacy_preserved():
    _, tools = _build()
    names = {t.name for t in tools}
    missing_new = EXPECTED_NEW_TOOLS - names
    assert not missing_new, f"missing new tools: {missing_new}"
    missing_legacy = LEGACY_SAMPLE - names
    assert not missing_legacy, f"refactor dropped legacy tools: {missing_legacy}"


def test_no_duplicate_tool_names():
    _, tools = _build()
    names = [t.name for t in tools]
    assert len(names) == len(set(names)), "duplicate tool names registered"


def test_all_new_tool_schemas_are_valid():
    import json

    _, tools = _build()
    by_name = {t.name: t for t in tools}
    for name in EXPECTED_NEW_TOOLS:
        tool = by_name[name]
        assert tool.source == "integration:opencti"
        schema = tool.input_schema
        assert schema.get("type") == "object", name
        assert isinstance(schema.get("properties"), dict), name
        # Must be JSON-serializable (sent to LLM providers verbatim).
        json.dumps(schema)
        # Every registered tool must have an executor handler.


def test_every_tool_has_a_handler():
    executor, tools = _build()
    for tool in tools:
        assert tool.name in executor._handlers, f"no handler for {tool.name}"


async def test_new_tools_not_draft_gated():
    # With an active draft, knowledge mutations are gated, but the platform
    # management tools must remain available.
    _, tools = _build(scope_headers={"opencti-draft-id": "draft--abc"})
    names = {t.name for t in tools}
    missing = EXPECTED_NEW_TOOLS - names
    assert not missing, f"management tools wrongly gated by draft: {missing}"

    # The presence check above can't catch a management handler accidentally
    # wrapped by the draft gate, because gating wraps the handler (it doesn't
    # drop the tool def) and only fires when NO draft is selected. So build
    # WITHOUT a draft and execute a management tool's no-network validation
    # path: it must return its own validation message, not the draft-required
    # gate that wraps knowledge mutations.
    executor, _ = _build()  # no scope_headers -> no active draft
    result = await executor.execute_tool(
        ToolCall(
            id="no-draft",
            name="manage_opencti_user",
            arguments={"action": "__smoke__"},
        )
    )
    assert not result.is_error, result.content
    assert "draft" not in result.content.lower()
    assert "Unknown action" in result.content


def test_empty_base_url_returns_no_tools():
    executor = CallableToolExecutor()
    tools = build_tools("||token-only", executor)
    assert tools == []


# ── Dev/debug PoC tools are gated behind the DEBUG setting ────────────────

_DEV_TOOL_NAMES = {
    "dev_debug_hello",
    "dev_debug_json_wrap",
    "dev_debug_json_wrap_raw",
    "dev_debug_append_hello",
    "dev_files_hash",
}


def test_dev_tools_hidden_by_default():
    # PoC/debug tools must never ship to production agents (DEBUG off).
    _, tools = _build()
    leaked = _DEV_TOOL_NAMES & {t.name for t in tools}
    assert not leaked, f"dev tools exposed without DEBUG: {leaked}"


def test_dev_tools_exposed_with_debug(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "debug", True)
    executor, tools = _build()
    names = {t.name for t in tools}
    missing = _DEV_TOOL_NAMES - names
    assert not missing, f"dev tools missing under DEBUG: {missing}"
    # Definitions and handlers must stay in lockstep under the same gate.
    for name in _DEV_TOOL_NAMES:
        assert name in executor._handlers, f"no handler for {name}"


# ── Draft gate: human-oriented in chat, self-healing in autonomous runs ───


# What ``stixObjectOrStixRelationship`` returns for a domain object: the label
# and marking tools resolve the element before writing to it.
_SDO_TARGET = {
    "id": "intrusion-set--x",
    "entity_type": "Intrusion-Set",
    "parent_types": ["Basic-Object", "Stix-Object", "Stix-Core-Object", "Stix-Domain-Object"],
}


class _QueryRoutedGraphQL:
    """Stub for ``knowledge._graphql`` that routes by query content.

    Records calls so a test can assert whether a draft was auto-created, and
    returns minimal canned data so ``add_opencti_label`` runs to completion.
    """

    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []

    async def __call__(self, client, base_url, headers, query, variables=None):
        self.calls.append((query, variables))
        if "draftWorkspaceAdd" in query:
            return {"draftWorkspaceAdd": {"id": "draft--auto-test"}}, None
        if "labels(filters" in query:
            return {"labels": {"edges": [{"node": {"id": "lbl-1", "value": "apt"}}]}}, None
        if "RefTarget" in query:
            return {"stixObjectOrStixRelationship": _SDO_TARGET}, None
        if "stixCoreObject(id" in query:
            return {"stixCoreObject": {}}, None
        return {}, None

    def created_draft(self) -> bool:
        return any("draftWorkspaceAdd" in q for q, _ in self.calls)


def _patch_knowledge_graphql(monkeypatch):
    import app.integrations.providers.opencti.knowledge as knowledge_mod

    stub = _QueryRoutedGraphQL()
    monkeypatch.setattr(knowledge_mod, "_graphql", stub)
    return stub


async def test_draft_gate_blocks_mutation_in_interactive_mode(monkeypatch):
    # Default (interactive) run with no active draft: the gate must defer to the
    # human-oriented flow and NOT create a draft on its own.
    stub = _patch_knowledge_graphql(monkeypatch)
    executor, _ = _build()
    result = await _run(
        executor,
        "add_opencti_label",
        {"entity_id": "intrusion-set--x", "label_name": "apt"},
    )
    assert "ASK the user" in result.content
    assert not stub.created_draft(), "interactive run must not auto-create a draft"


async def test_draft_gate_self_heals_in_autonomous_mode(monkeypatch):
    # Autonomous run with no active draft: the gate must auto-create a draft and
    # run the mutation instead of asking a user that isn't there.
    stub = _patch_knowledge_graphql(monkeypatch)
    executor, _ = _build(autonomous=True)
    result = await _run(
        executor,
        "add_opencti_label",
        {"entity_id": "intrusion-set--x", "label_name": "apt"},
    )
    assert stub.created_draft(), "autonomous run should auto-create a draft"
    assert "ASK the user" not in result.content
    assert "NOT viewing any draft" not in result.content
    assert "Label 'apt' added" in result.content


def test_create_draft_schema_is_context_aware():
    # Interactive: name required + human-oriented wording.
    _, tools = _build()
    cd = {t.name: t for t in tools}["create_opencti_draft"]
    assert cd.input_schema.get("required") == ["name"]
    assert "ask the user" in cd.description.lower()
    # Autonomous: name optional + headless wording.
    _, tools_a = _build(autonomous=True)
    cd_a = {t.name: t for t in tools_a}["create_opencti_draft"]
    assert cd_a.input_schema.get("required") == []
    assert "autonomously" in cd_a.description.lower()


# ── Dispatch smoke (early-return validation paths only — no network) ──────

SMOKE_CALLS = [
    ("manage_opencti_playbook", {"action": "__smoke__"}),
    ("edit_opencti_playbook_graph", {"action": "add_node"}),
    ("manage_opencti_workspace", {"action": "__smoke__"}),
    ("manage_opencti_user", {"action": "__smoke__"}),
    ("manage_opencti_group", {"action": "__smoke__"}),
    ("manage_opencti_role", {"action": "__smoke__"}),
    ("manage_opencti_profile", {"action": "__smoke__"}),
    ("manage_opencti_notifier", {"action": "__smoke__"}),
    ("manage_opencti_trigger", {"action": "__smoke__"}),
    ("manage_opencti_notification", {"action": "__smoke__"}),
    ("manage_opencti_entity_setting", {"action": "__smoke__"}),
    ("manage_opencti_vocabulary", {"action": "__smoke__"}),
    ("manage_opencti_status_template", {"action": "__smoke__"}),
    ("manage_opencti_decay_rule", {"action": "__smoke__"}),
    ("manage_opencti_retention_rule", {"action": "__smoke__"}),
    ("manage_opencti_exclusion_list", {"action": "__smoke__"}),
    ("manage_opencti_pir", {"action": "__smoke__"}),
    ("query_opencti_pir_entities", {"pir_id": ""}),
    ("query_opencti_pir_relationships", {"pir_id": ""}),
    ("manage_opencti_ingestion", {"action": "list", "feed_type": "bogus"}),
    ("manage_opencti_form", {"action": "__smoke__"}),
    ("manage_opencti_data_sharing", {"action": "list", "share_type": "bogus"}),
    ("query_opencti_catalog", {"action": "__smoke__"}),
    ("manage_opencti_connector", {"action": "__smoke__"}),
]


async def test_consolidated_dispatch_smoke():
    executor, _ = _build()
    for idx, (name, args) in enumerate(SMOKE_CALLS):
        result = await executor.execute_tool(
            ToolCall(id=f"call-{idx}", name=name, arguments=args)
        )
        assert not result.is_error, f"{name} raised: {result.content}"
        assert isinstance(result.content, str) and result.content, name


# ── Regression tests: connection-node formatting + partial-update safety ──


class _RecordingGraphQL:
    """Stub for ``_graphql`` that records calls and returns canned data.

    Domain modules call ``_graphql(client, base_url, headers, query, variables)``
    through their module-level import, so patching e.g. ``access._graphql``
    reroutes every GraphQL call in that module through this stub (no network).
    """

    def __init__(self, response):
        self.response = response
        self.calls: list[tuple[str, dict | None]] = []

    async def __call__(self, client, base_url, headers, query, variables=None):
        self.calls.append((query, variables))
        return self.response, None

    def last_input_keys(self) -> set[str]:
        """Keys of the EditInput list passed in the most recent mutation."""
        _, variables = self.calls[-1]
        return {edit["key"] for edit in (variables or {}).get("input", [])}


async def _run(executor, name, arguments):
    return await executor.execute_tool(
        ToolCall(id=f"call-{name}", name=name, arguments=arguments)
    )


async def test_manage_user_get_unwraps_connection_nodes(monkeypatch):
    # _edges() already returns node dicts; the formatter must NOT index e["node"]
    # again or it raises KeyError for any user with groups/organizations.
    from app.integrations.providers.opencti import access

    executor, _ = _build()
    monkeypatch.setattr(
        access,
        "_graphql",
        _RecordingGraphQL(
            {
                "user": {
                    "id": "u1",
                    "name": "Alice",
                    "user_email": "alice@example.com",
                    "firstname": "Alice",
                    "lastname": "Doe",
                    "account_status": "Active",
                    "roles": [{"id": "r1", "name": "Analyst"}],
                    "groups": {"edges": [{"node": {"id": "g1", "name": "Engineering"}}]},
                    "objectOrganization": {
                        "edges": [{"node": {"id": "o1", "name": "Filigran"}}]
                    },
                }
            }
        ),
    )
    result = await _run(
        executor, "manage_opencti_user", {"action": "get", "user_id": "u1"}
    )
    assert not result.is_error, result.content
    assert "Engineering" in result.content
    assert "Filigran" in result.content
    assert "Analyst" in result.content


async def test_manage_group_get_unwraps_role_nodes(monkeypatch):
    from app.integrations.providers.opencti import access

    executor, _ = _build()
    monkeypatch.setattr(
        access,
        "_graphql",
        _RecordingGraphQL(
            {
                "group": {
                    "id": "g1",
                    "name": "Engineering",
                    "description": "Eng team",
                    "default_assignation": False,
                    "auto_new_marking": False,
                    "roles": {
                        "edges": [{"node": {"id": "r1", "name": "Administrator"}}]
                    },
                    "members": {"edges": [{"node": {"id": "u1", "name": "Alice"}}]},
                }
            }
        ),
    )
    result = await _run(
        executor, "manage_opencti_group", {"action": "get", "group_id": "g1"}
    )
    assert not result.is_error, result.content
    assert "Administrator" in result.content


async def test_manage_group_get_names_its_members(monkeypatch):
    from app.integrations.providers.opencti import access

    executor, _ = _build()
    fake = _RecordingGraphQL(
        {
            "group": {
                "id": "g1",
                "name": "Connectors (without labels)",
                "roles": {"edges": []},
                "members": {
                    "edges": [
                        {
                            "node": {
                                "id": "u1",
                                "name": "[F] feed",
                                "user_email": "feed@x.io",
                                "user_service_account": True,
                            }
                        },
                        {"node": {"id": "u2", "name": "Alice", "user_email": "", "user_service_account": False}},
                    ],
                    "pageInfo": {"hasNextPage": True, "globalCount": 5},
                },
            }
        }
    )
    monkeypatch.setattr(access, "_graphql", fake)
    result = await _run(executor, "manage_opencti_group", {"action": "get", "group_id": "g1"})
    assert not result.is_error, result.content
    assert "  members: 5" in result.content
    assert "    - [F] feed (id: u1, feed@x.io, service account)" in result.content
    assert "    - Alice (id: u2, user)" in result.content
    assert "3 not shown" in result.content
    query = fake.calls[-1][0]
    assert "user_service_account" in query and "globalCount" in query


def test_group_member_lines_without_page_info():
    from app.integrations.providers.opencti.access import _group_member_lines

    lines = _group_member_lines({"members": {"edges": [{"node": {"id": "u1", "name": "Alice"}}]}})
    assert lines == ["  members: 1", "    - Alice (id: u1, user)"]
    assert _group_member_lines({}) == ["  members: 0"]


class _ConfidenceOpenCTI:
    """Stateful ``_graphql`` stub for the confidence level of one group and one user.

    It applies a fieldPatch on ``group_confidence_level`` / ``user_confidence_level``
    as OpenCTI stores it (``[null]`` clears the user's own level, which then
    inherits from its group), so a test reads back what was written.
    """

    def __init__(self):
        self.group = {
            "id": "g1",
            "name": "Connectors (without labels)",
            "group_confidence_level": {"max_confidence": 100, "overrides": []},
        }
        self.user_level: dict | None = {"max_confidence": 70, "overrides": []}
        self.calls: list[tuple[str, dict | None]] = []

    def writes(self) -> list[dict]:
        return [v for q, v in self.calls if q.lstrip().startswith("mutation")]

    def _user(self) -> dict:
        own = self.user_level
        if own and own.get("max_confidence") is not None:
            eff = {
                "max_confidence": own["max_confidence"],
                "overrides": own.get("overrides") or [],
                "source": {"type": "User", "object": {"entity_type": "User", "id": "u1", "name": "feed"}},
            }
        else:
            g = self.group["group_confidence_level"]
            eff = {
                "max_confidence": g["max_confidence"],
                "overrides": g["overrides"],
                "source": {
                    "type": "Group",
                    "object": {"entity_type": "Group", "id": "g1", "name": self.group["name"]},
                },
            }
        return {
            "id": "u1",
            "name": "feed",
            "user_email": "feed@example.com",
            "account_status": "Active",
            "roles": [],
            "groups": {"edges": [{"node": {"id": "g1", "name": self.group["name"]}}]},
            "objectOrganization": {"edges": []},
            "user_confidence_level": own,
            "effective_confidence_level": eff,
        }

    async def __call__(self, client, base_url, headers, query, variables=None):
        self.calls.append((query, variables))
        if "groupEdit" in query and "fieldPatch" in query:
            for edit in variables["input"]:
                if edit["key"] == "group_confidence_level":
                    self.group["group_confidence_level"] = edit["value"][0]
                else:
                    self.group[edit["key"]] = edit["value"][0]
            return {"groupEdit": {"fieldPatch": {"id": "g1", "name": self.group["name"]}}}, None
        if "userEdit" in query and "fieldPatch" in query:
            for edit in variables["input"]:
                if edit["key"] == "user_confidence_level":
                    self.user_level = edit["value"][0]
            return {"userEdit": {"fieldPatch": {"id": "u1", "name": "feed"}}}, None
        if "groupAdd" in query:
            return {
                "groupAdd": {
                    "id": "g2",
                    "name": variables["input"]["name"],
                    "group_confidence_level": variables["input"]["group_confidence_level"],
                }
            }, None
        if "group(id:" in query:
            return {
                "group": {
                    **self.group,
                    "description": "",
                    "default_assignation": False,
                    "auto_new_marking": False,
                    "roles": {"edges": []},
                    "members": {"edges": []},
                }
            }, None
        if "user(id:" in query:
            return {"user": self._user()}, None
        raise AssertionError(f"unexpected query: {query}")


async def test_group_update_max_confidence_patches_and_reads_back(monkeypatch):
    from app.integrations.providers.opencti import access

    executor, _ = _build()
    fake = _ConfidenceOpenCTI()
    fake.group["group_confidence_level"] = {
        "max_confidence": 100,
        "overrides": [{"entity_type": "Report", "max_confidence": 90}],
    }
    monkeypatch.setattr(access, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_group",
        {"action": "update", "group_id": "g1", "max_confidence": 50},
    )
    assert not result.is_error, result.content
    [write] = fake.writes()
    assert write["input"] == [
        {
            "key": "group_confidence_level",
            "value": [
                {
                    "max_confidence": 50,
                    # The stored overrides are kept when only the max changes.
                    "overrides": [{"entity_type": "Report", "max_confidence": 90}],
                }
            ],
        }
    ]
    assert "Read back from OpenCTI: group_confidence_level 50 (overrides: Report 90)" in result.content


async def test_group_update_overrides_and_get_shows_them(monkeypatch):
    from app.integrations.providers.opencti import access

    executor, _ = _build()
    fake = _ConfidenceOpenCTI()
    monkeypatch.setattr(access, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_group",
        {
            "action": "update",
            "group_id": "g1",
            "confidence_overrides": [{"entity_type": "Indicator", "max_confidence": 30}],
        },
    )
    assert not result.is_error, result.content
    got = await _run(executor, "manage_opencti_group", {"action": "get", "group_id": "g1"})
    assert "group_confidence_level: 100 (overrides: Indicator 30)" in got.content


async def test_group_create_sends_confidence_level(monkeypatch):
    from app.integrations.providers.opencti import access

    executor, _ = _build()
    fake = _ConfidenceOpenCTI()
    monkeypatch.setattr(access, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_group",
        {"action": "create", "name": "Test group", "max_confidence": 50},
    )
    assert not result.is_error, result.content
    assert fake.writes()[0]["input"]["group_confidence_level"] == {
        "max_confidence": 50,
        "overrides": [],
    }
    assert "group_confidence_level: 50" in result.content


async def test_group_update_names_an_ignored_field_and_writes_the_rest(monkeypatch):
    from app.integrations.providers.opencti import access

    executor, _ = _build()
    fake = _ConfidenceOpenCTI()
    monkeypatch.setattr(access, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_group",
        {"action": "update", "group_id": "g1", "name": "x", "confidence_level": 50},
    )
    assert not result.is_error, result.content
    assert "does not apply confidence_level" in result.content and "ignored" in result.content
    assert fake.writes(), result.content


async def test_group_update_rejects_out_of_range_confidence(monkeypatch):
    from app.integrations.providers.opencti import access

    executor, _ = _build()
    fake = _ConfidenceOpenCTI()
    monkeypatch.setattr(access, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_group",
        {"action": "update", "group_id": "g1", "max_confidence": 150},
    )
    assert result.is_error, result.content
    assert fake.writes() == []


async def test_user_update_sets_then_clears_own_confidence(monkeypatch):
    from app.integrations.providers.opencti import access

    executor, _ = _build()
    fake = _ConfidenceOpenCTI()
    fake.user_level = None
    monkeypatch.setattr(access, "_graphql", fake)

    set_result = await _run(
        executor,
        "manage_opencti_user",
        {"action": "update", "user_id": "u1", "max_confidence": 40},
    )
    assert not set_result.is_error, set_result.content
    assert fake.writes()[-1]["input"] == [
        {"key": "user_confidence_level", "value": [{"max_confidence": 40, "overrides": []}]}
    ]
    assert "user_confidence_level: 40" in set_result.content
    assert "effective_confidence_level: 40, source: User 'feed'" in set_result.content

    clear_result = await _run(
        executor,
        "manage_opencti_user",
        {"action": "update", "user_id": "u1", "inherit_confidence": True},
    )
    assert not clear_result.is_error, clear_result.content
    assert fake.writes()[-1]["input"] == [{"key": "user_confidence_level", "value": [None]}]
    assert "user_confidence_level: inherit (from groups)" in clear_result.content
    assert "effective_confidence_level: 100, source: Group 'Connectors (without labels)'" in clear_result.content


async def test_user_get_shows_own_and_effective_confidence(monkeypatch):
    from app.integrations.providers.opencti import access

    executor, _ = _build()
    fake = _ConfidenceOpenCTI()
    fake.user_level = {
        "max_confidence": 60,
        "overrides": [{"entity_type": "Report", "max_confidence": 80}],
    }
    monkeypatch.setattr(access, "_graphql", fake)
    result = await _run(executor, "manage_opencti_user", {"action": "get", "user_id": "u1"})
    assert not result.is_error, result.content
    assert "user_confidence_level: 60 (overrides: Report 80)" in result.content
    assert "effective_confidence_level: 60 (overrides: Report 80), source: User 'feed'" in result.content


async def test_user_update_names_ignored_fields_and_writes_the_rest(monkeypatch):
    from app.integrations.providers.opencti import access

    executor, _ = _build()
    fake = _ConfidenceOpenCTI()
    monkeypatch.setattr(access, "_graphql", fake)
    result = await _run(
        executor, "manage_opencti_user", {"action": "update", "user_id": "u1", "name": "x", "group_ids": ["g1"]}
    )
    assert not result.is_error, result.content
    assert "does not apply group_ids" in result.content and "ignored" in result.content
    assert fake.writes(), result.content


async def test_user_update_names_an_undeclared_field(monkeypatch):
    from app.integrations.providers.opencti import access

    executor, _ = _build()
    fake = _ConfidenceOpenCTI()
    monkeypatch.setattr(access, "_graphql", fake)
    result = await _run(
        executor, "manage_opencti_user", {"action": "update", "user_id": "u1", "max_confidence_level": 50}
    )
    assert "does not apply max_confidence_level" in result.content, result.content


async def test_user_update_refuses_max_confidence_with_inherit(monkeypatch):
    from app.integrations.providers.opencti import access

    executor, _ = _build()
    fake = _ConfidenceOpenCTI()
    monkeypatch.setattr(access, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_user",
        {"action": "update", "user_id": "u1", "max_confidence": 50, "inherit_confidence": True},
    )
    assert result.is_error, result.content
    assert "othing was written" in result.content, result.content
    assert fake.writes() == []


@pytest.mark.parametrize(
    ("tool", "args", "named"),
    [
        # Reads and relation actions name a field they do not apply as ignored, as
        # create and update do: the answer must never read as proof the field was taken.
        ("manage_opencti_user", {"action": "list", "user_id": "u1"}, "user_id"),
        ("manage_opencti_user", {"action": "get", "user_id": "u1", "max_confidence": 50}, "max_confidence"),
        ("manage_opencti_user", {"action": "delete", "user_id": "u1", "name": "x"}, "name"),
        (
            "manage_opencti_user",
            {"action": "add_group", "user_id": "u1", "group_id": "g1", "organization_id": "o1"},
            "organization_id",
        ),
        (
            "manage_opencti_user",
            {"action": "remove_organization", "user_id": "u1", "organization_id": "o1", "search": "x"},
            "search",
        ),
        ("manage_opencti_user", {"action": "create", "name": "x", "limit": 5}, "limit"),
        ("manage_opencti_group", {"action": "list", "max_confidence": 50}, "max_confidence"),
        ("manage_opencti_group", {"action": "get", "group_id": "g1", "bogus": 1}, "bogus"),
        ("manage_opencti_group", {"action": "delete", "group_id": "g1", "role_id": "r1"}, "role_id"),
        (
            "manage_opencti_group",
            {"action": "add_role", "group_id": "g1", "role_id": "r1", "description": "x"},
            "description",
        ),
    ],
)
async def test_access_tools_name_unapplied_fields_on_every_action(monkeypatch, tool, args, named):
    from app.integrations.providers.opencti import access

    executor, _ = _build()
    fake = _ConfidenceOpenCTI()

    async def lenient(*args, **kw):
        # The action now runs: any document it sends is answered empty.
        try:
            return await fake(*args, **kw)
        except AssertionError:
            return {}, None

    monkeypatch.setattr(access, "_graphql", lenient)
    result = await _run(executor, tool, args)
    assert f"does not apply {named}" in result.content, result.content
    assert "ignored and the rest ran" in result.content, result.content


async def test_access_tools_accept_the_fields_each_action_applies(monkeypatch):
    from app.integrations.providers.opencti import access

    executor, _ = _build()
    fake = _ConfidenceOpenCTI()
    monkeypatch.setattr(access, "_graphql", fake)
    for tool, args in (
        ("manage_opencti_user", {"action": "list", "search": "x", "limit": 5}),
        ("manage_opencti_user", {"action": "get", "user_id": "u1"}),
        ("manage_opencti_group", {"action": "list", "search": "x", "limit": 20}),
        ("manage_opencti_group", {"action": "get", "group_id": "g1"}),
    ):
        result = await _run(executor, tool, args)
        assert "does not apply" not in result.content, (args, result.content)


async def test_manage_profile_get_unwraps_group_nodes(monkeypatch):
    from app.integrations.providers.opencti import access

    executor, _ = _build()
    monkeypatch.setattr(
        access,
        "_graphql",
        _RecordingGraphQL(
            {
                "me": {
                    "id": "u1",
                    "name": "Alice",
                    "user_email": "alice@example.com",
                    "groups": {"edges": [{"node": {"name": "Engineering"}}]},
                }
            }
        ),
    )
    result = await _run(executor, "manage_opencti_profile", {"action": "get"})
    assert not result.is_error, result.content
    assert "Engineering" in result.content


async def test_ingestion_create_accepts_automatic_user_in_config(monkeypatch):
    from app.integrations.providers.opencti import ingestion

    executor, _ = _build()
    monkeypatch.setattr(
        ingestion,
        "_graphql",
        _RecordingGraphQL({"ingestionRssAdd": {"id": "f1", "name": "Threats RSS"}}),
    )
    result = await _run(
        executor,
        "manage_opencti_ingestion",
        {
            "action": "create",
            "feed_type": "rss",
            "name": "Threats RSS",
            "uri": "https://example.com/feed.xml",
            "config": '{"automatic_user": true}',
        },
    )
    assert not result.is_error, result.content
    assert "user_id is required" not in result.content
    assert "Created" in result.content


async def test_decay_rule_update_only_patches_provided_fields(monkeypatch):
    from app.integrations.providers.opencti import customization

    executor, _ = _build()
    fake = _RecordingGraphQL({"decayRuleFieldPatch": {"id": "d1", "name": "Renamed"}})
    monkeypatch.setattr(customization, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_decay_rule",
        {"action": "update", "decay_rule_id": "d1", "name": "Renamed"},
    )
    assert not result.is_error, result.content
    # Only the explicitly-provided name is patched — order/active/lifetime/
    # pound/revoke_score are NOT reset to their handler defaults.
    assert fake.last_input_keys() == {"name"}


async def test_decay_rule_update_includes_decay_filters(monkeypatch):
    from app.integrations.providers.opencti import customization

    executor, _ = _build()
    fake = _RecordingGraphQL({"decayRuleFieldPatch": {"id": "d1", "name": "d"}})
    monkeypatch.setattr(customization, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_decay_rule",
        {
            "action": "update",
            "decay_rule_id": "d1",
            "decay_filters": '{"mode": "and", "filters": [], "filterGroups": []}',
        },
    )
    assert not result.is_error, result.content
    assert "decay_filters" in fake.last_input_keys()


async def test_retention_rule_update_only_patches_provided_fields(monkeypatch):
    from app.integrations.providers.opencti import customization

    executor, _ = _build()
    fake = _RecordingGraphQL(
        {"retentionRuleEdit": {"fieldPatch": {"id": "r1", "name": "Renamed"}}}
    )
    monkeypatch.setattr(customization, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_retention_rule",
        {"action": "update", "retention_rule_id": "r1", "name": "Renamed"},
    )
    assert not result.is_error, result.content
    # max_retention / retention_unit are NOT reset to their handler defaults.
    assert fake.last_input_keys() == {"name"}


async def test_pir_update_does_not_reset_rescan_days(monkeypatch):
    from app.integrations.providers.opencti import pir

    executor, _ = _build()
    fake = _RecordingGraphQL({"pirFieldPatch": {"id": "p1", "name": "Renamed"}})
    monkeypatch.setattr(pir, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_pir",
        {"action": "update", "pir_id": "p1", "name": "Renamed"},
    )
    assert not result.is_error, result.content
    assert fake.last_input_keys() == {"name"}


async def test_pir_create_accepts_native_filter_objects(monkeypatch):
    # Docs describe pir_filters/pir_criteria as native FilterGroup objects, so a
    # caller may pass parsed objects — json.loads would raise TypeError on those.
    from app.integrations.providers.opencti import pir

    executor, _ = _build()
    monkeypatch.setattr(
        pir, "_graphql", _RecordingGraphQL({"pirAdd": {"id": "p1", "name": "APT PIR"}})
    )
    filter_group = {"mode": "and", "filters": [], "filterGroups": []}
    criterion_group = {
        "mode": "and",
        "filters": [
            {"key": ["entity_type"], "values": ["targets"]},
            {"key": ["toId"], "values": ["550e8400-e29b-41d4-a716-446655440000"]},
        ],
        "filterGroups": [],
    }
    result = await _run(
        executor,
        "manage_opencti_pir",
        {
            "action": "create",
            "name": "APT PIR",
            "pir_filters": filter_group,
            "pir_criteria": [{"weight": 1, "filters": criterion_group}],
        },
    )
    assert not result.is_error, result.content
    assert "Created" in result.content


async def test_pir_entities_uses_stixdomainobjects_with_pirid(monkeypatch):
    # Ranking flagged entities by pir_score ONLY works through the
    # stixDomainObjects(pirId: ...) query (OpenCTI strips pirId on
    # pirRelationships before sorting). Verify the tool threads pirId into the
    # query + variables, auto-builds the regardingOf/in-pir scope filter, and
    # surfaces the per-entity score + matching criteria.
    import json

    from app.integrations.providers.opencti import pir

    pir_id = "68444915-c6ce-4cd9-987d-83013b01bc0d"
    executor, _ = _build()
    fake = _RecordingGraphQL(
        {
            "stixDomainObjects": {
                "edges": [
                    {
                        "node": {
                            "id": "11111111-1111-1111-1111-111111111111",
                            "entity_type": "Intrusion-Set",
                            "created_at": "2026-01-01T00:00:00.000Z",
                            "representative": {"main": "APT28"},
                            "objectLabel": [
                                {"id": "l1", "value": "apt", "color": "#fff"}
                            ],
                            "objectMarking": [],
                            "creators": [{"id": "c1", "name": "admin"}],
                            "createdBy": {"id": "a1", "name": "Acme"},
                            "pirInformation": {
                                "pir_score": 87,
                                "last_pir_score_date": "2026-06-20T00:00:00.000Z",
                                "pir_explanation": [
                                    {
                                        "criterion": {
                                            "filters": json.dumps(
                                                {
                                                    "mode": "and",
                                                    "filters": [
                                                        {
                                                            "key": ["entity_type"],
                                                            "values": ["targets"],
                                                        },
                                                        {
                                                            "key": ["toId"],
                                                            "values": ["e-uuid"],
                                                        },
                                                    ],
                                                    "filterGroups": [],
                                                }
                                            )
                                        }
                                    }
                                ],
                            },
                        }
                    }
                ],
                "pageInfo": {"globalCount": 1},
            }
        }
    )
    monkeypatch.setattr(pir, "_graphql", fake)
    result = await _run(executor, "query_opencti_pir_entities", {"pir_id": pir_id})
    assert not result.is_error, result.content
    query, variables = fake.calls[-1]
    assert "stixDomainObjects(" in query
    assert "pirId: $pirId" in query
    assert "pirInformation(pirId: $pirId)" in query
    assert variables["pirId"] == pir_id
    assert variables["orderBy"] == "pir_score"
    # auto-built regardingOf / in-pir scope filter
    base_filter = variables["filters"]["filters"][0]
    assert base_filter["key"] == "regardingOf"
    rel_values = {v["key"]: v["values"] for v in base_filter["values"]}
    assert rel_values["id"] == [pir_id]
    assert rel_values["relationship_type"] == ["in-pir"]
    # output surfaces the score + matched criteria explanation
    assert "PIR score 87" in result.content
    assert "APT28" in result.content
    assert "entity_type=targets" in result.content


async def test_pir_relationships_does_not_sort_by_pir_score(monkeypatch):
    # pirRelationships cannot order by pir_score (its resolver dissocs pirId
    # before the sort builder runs -> "You should provide a PIR ID to order by
    # pir_score."). The tool must clamp a pir_score request to created_at.
    from app.integrations.providers.opencti import pir

    executor, _ = _build()
    fake = _RecordingGraphQL({"pirRelationships": {"edges": []}})
    monkeypatch.setattr(pir, "_graphql", fake)
    result = await _run(
        executor,
        "query_opencti_pir_relationships",
        {"pir_id": "p1", "order_by": "pir_score"},
    )
    assert not result.is_error, result.content
    _, variables = fake.calls[-1]
    assert variables["orderBy"] == "created_at"


async def test_pir_flag_rejects_non_list_matching_criteria(monkeypatch):
    # matching_criteria is documented as a JSON array; a JSON object/scalar must
    # be rejected with a friendly message (mirroring the create-path guard)
    # before any GraphQL call, rather than forwarded to fail with an opaque API
    # error.
    from app.integrations.providers.opencti import pir

    executor, _ = _build()
    fake = _RecordingGraphQL({"pirFlagElement": True})
    monkeypatch.setattr(pir, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_pir",
        {
            "action": "flag",
            "pir_id": "pir1",
            "relationship_id": "rel1",
            "source_id": "src1",
            "matching_criteria": '{"weight": 1}',  # JSON object, not an array
        },
    )
    assert not result.is_error, result.content
    assert "non-empty JSON array" in result.content
    assert fake.calls == []  # rejected before any GraphQL call


async def test_group_remove_role_uses_non_null_toid(monkeypatch):
    # The relationDelete mutation MUST declare $toId as StixRef! (non-null), like
    # the group-membership and capability removals — a nullable StixRef is
    # incompatible with the non-null argument and fails at query-validation time.
    from app.integrations.providers.opencti import access

    executor, _ = _build()
    fake = _RecordingGraphQL({"groupEdit": {"relationDelete": {"id": "g1"}}})
    monkeypatch.setattr(access, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_group",
        {"action": "remove_role", "group_id": "g1", "role_id": "r1"},
    )
    assert not result.is_error, result.content
    query, _vars = fake.calls[-1]
    assert "$toId: StixRef!" in query


async def test_audit_list_rejects_non_object_filters():
    # A bare JSON array/scalar is not a FilterGroup; the tool must return a
    # friendly validation message instead of raising on setdefault/append.
    executor, _ = _build()
    result = await _run(
        executor,
        "query_opencti_activity",
        {"stream": "History", "mode": "list", "filters": "[1, 2, 3]"},
    )
    assert not result.is_error, result.content
    assert "FilterGroup object" in result.content


async def test_audit_list_tolerates_null_filter_fields(monkeypatch):
    # A FilterGroup whose 'filters'/'filterGroups' are null must not crash when
    # date filters are merged in.
    from app.integrations.providers.opencti import audit

    executor, _ = _build()
    fake = _RecordingGraphQL({"audits": {"edges": []}})
    monkeypatch.setattr(audit, "_graphql", fake)
    result = await _run(
        executor,
        "query_opencti_activity",
        {
            "stream": "History",
            "mode": "list",
            "filters": '{"mode": "or", "filters": null, "filterGroups": null}',
            "start_date": "2026-01-01T00:00:00Z",
        },
    )
    assert not result.is_error, result.content
    _, variables = fake.calls[-1]
    fg = variables["filters"]
    assert isinstance(fg["filters"], list)
    assert any(f.get("key") == "timestamp" for f in fg["filters"])


async def test_audit_date_range_narrows_or_filter_group(monkeypatch):
    # A caller 'or' FilterGroup plus a date range must be combined under an
    # OUTER 'and' so the date range narrows (never widens) the result set.
    from app.integrations.providers.opencti import audit

    executor, _ = _build()
    fake = _RecordingGraphQL({"audits": {"edges": []}})
    monkeypatch.setattr(audit, "_graphql", fake)
    caller = {
        "mode": "or",
        "filters": [{"key": "event_type", "values": ["update"], "operator": "eq"}],
        "filterGroups": [],
    }
    import json as _json

    result = await _run(
        executor,
        "query_opencti_activity",
        {
            "stream": "History",
            "mode": "list",
            "filters": _json.dumps(caller),
            "start_date": "2026-01-01T00:00:00Z",
            "end_date": "2026-02-01T00:00:00Z",
        },
    )
    assert not result.is_error, result.content
    _, variables = fake.calls[-1]
    fg = variables["filters"]
    # Outer group is AND; date predicates live there; the caller's OR group is
    # nested (so its OR cannot absorb the timestamp predicates).
    assert fg["mode"] == "and"
    assert {f["operator"] for f in fg["filters"]} == {"gte", "lte"}
    assert all(f["key"] == "timestamp" for f in fg["filters"])
    assert caller in fg["filterGroups"]


async def test_audit_count_normalizes_null_filter_fields(monkeypatch):
    # The count path forwards base_filters straight to GraphQL (auditsNumber
    # takes startDate/endDate natively, so it does not go through _with_dates).
    # A FilterGroup with null 'filters'/'filterGroups' must still be normalized
    # to lists at parse time so the API receives a well-formed FilterGroup.
    from app.integrations.providers.opencti import audit

    executor, _ = _build()
    fake = _RecordingGraphQL({"auditsNumber": {"count": 0, "total": 0}})
    monkeypatch.setattr(audit, "_graphql", fake)
    result = await _run(
        executor,
        "query_opencti_activity",
        {
            "stream": "Activity",
            "mode": "count",
            "filters": '{"mode": "and", "filters": null, "filterGroups": null}',
            "start_date": "2026-01-01T00:00:00Z",
        },
    )
    assert not result.is_error, result.content
    _, variables = fake.calls[-1]
    fg = variables["filters"]
    assert isinstance(fg["filters"], list)
    assert isinstance(fg["filterGroups"], list)


async def test_pir_create_accepts_json_string_filters(monkeypatch):
    # The schema declares pir_filters/pir_criteria as JSON strings (the contract
    # surfaced to LLM providers); the string form must parse and reach the API.
    import json

    from app.integrations.providers.opencti import pir

    executor, _ = _build()
    fake = _RecordingGraphQL({"pirAdd": {"id": "p1", "name": "APT PIR"}})
    monkeypatch.setattr(pir, "_graphql", fake)
    filter_group = {"mode": "and", "filters": [], "filterGroups": []}
    criterion_group = {
        "mode": "and",
        "filters": [
            {
                "key": ["entity_type"],
                "values": ["targets"],
                "operator": "eq",
                "mode": "or",
            },
            {
                "key": ["toId"],
                "values": ["550e8400-e29b-41d4-a716-446655440000"],
                "operator": "eq",
                "mode": "or",
            },
        ],
        "filterGroups": [],
    }
    result = await _run(
        executor,
        "manage_opencti_pir",
        {
            "action": "create",
            "name": "APT PIR",
            "pir_filters": json.dumps(filter_group),
            "pir_criteria": json.dumps([{"weight": 1, "filters": criterion_group}]),
        },
    )
    assert not result.is_error, result.content
    assert "Created" in result.content
    # The JSON string is parsed to a native object before hitting the API.
    _, variables = fake.calls[-1]
    assert variables["input"]["pir_filters"] == filter_group
    assert variables["input"]["pir_criteria"] == [
        {"weight": 1, "filters": criterion_group}
    ]


async def test_notifier_get_redacts_configuration(monkeypatch):
    # notifier_configuration may hold webhook URLs / SMTP credentials — the read
    # path must never echo it back verbatim.
    from app.integrations.providers.opencti import notifications

    executor, _ = _build()
    secret = "https://hooks.example.com/T/abc/secret-token-xyz"
    monkeypatch.setattr(
        notifications,
        "_graphql",
        _RecordingGraphQL(
            {
                "notifier": {
                    "id": "n1",
                    "name": "Webhook",
                    "description": "alerts",
                    "notifier_connector": {"id": "c1", "name": "Webhook"},
                    "notifier_configuration": f'{{"url": "{secret}"}}',
                }
            }
        ),
    )
    result = await _run(
        executor,
        "manage_opencti_notifier",
        {"action": "get", "notifier_id": "n1"},
    )
    assert not result.is_error, result.content
    assert secret not in result.content
    assert "hidden" in result.content


async def test_build_tools_strips_whitespace_from_api_token(monkeypatch):
    # A stored compound token with stray whitespace around the api_token segment
    # (e.g. `base_url|| token`) must not produce a malformed `Bearer  token`
    # Authorization header that fails authentication. build_tools strips it.
    from app.integrations.providers.opencti import notifications

    captured: dict = {}

    async def _capture(client, base_url, headers, query, variables=None):
        captured["headers"] = headers
        return {"notifiers": {"edges": []}}, None

    monkeypatch.setattr(notifications, "_graphql", _capture)

    executor = CallableToolExecutor()
    build_tools("https://demo.opencti.io||  test-token  ", executor)
    result = await _run(executor, "manage_opencti_notifier", {"action": "list"})

    assert not result.is_error, result.content
    assert captured["headers"]["Authorization"] == "Bearer test-token"


async def test_notifier_test_rejects_invalid_configuration(monkeypatch):
    # The `test` action must validate notifier_configuration JSON like
    # create/update do, returning a friendly error before any GraphQL call.
    from app.integrations.providers.opencti import notifications

    executor, _ = _build()
    fake = _RecordingGraphQL({"notifierTest": "ok"})
    monkeypatch.setattr(notifications, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_notifier",
        {
            "action": "test",
            "notifier_connector_id": "c1",
            "notifier_configuration": "{not valid json}",
        },
    )
    assert not result.is_error, result.content
    assert "notifier_configuration must be valid JSON" in result.content
    assert fake.calls == []  # invalid config rejected before any GraphQL call


async def test_notifier_test_forwards_valid_configuration(monkeypatch):
    from app.integrations.providers.opencti import notifications

    executor, _ = _build()
    fake = _RecordingGraphQL({"notifierTest": "delivered"})
    monkeypatch.setattr(notifications, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_notifier",
        {
            "action": "test",
            "notifier_connector_id": "c1",
            "notifier_configuration": '{"url": "https://example.com/hook"}',
        },
    )
    assert not result.is_error, result.content
    assert "Notifier test result" in result.content
    assert fake.calls  # valid config forwarded to the API


async def test_playbook_insert_node_requires_node_fields(monkeypatch):
    # insert_node creates a NEW node via PlaybookAddNodeInput! (name +
    # component_id required). Supplying only parent/port/child must fail
    # validation BEFORE any GraphQL call rather than send empty name/component_id.
    from app.integrations.providers.opencti import playbooks

    executor, _ = _build()
    fake = _RecordingGraphQL({"playbookInsertNode": {"nodeId": "n1", "linkId": "l1"}})
    monkeypatch.setattr(playbooks, "_graphql", fake)
    result = await _run(
        executor,
        "edit_opencti_playbook_graph",
        {
            "action": "insert_node",
            "playbook_id": "pb1",
            "parent_node_id": "p1",
            "parent_port_id": "out",
            "child_node_id": "c1",
        },
    )
    assert not result.is_error, result.content
    assert "name and component_id are required" in result.content
    assert fake.calls == []  # no malformed mutation was sent


async def test_playbook_insert_node_proceeds_with_node_fields(monkeypatch):
    from app.integrations.providers.opencti import playbooks

    executor, _ = _build()
    graph = {"nodes": [{"id": n} for n in ("p1", "c1", "n1")], "links": [{"id": "l1"}]}
    fake = _RecordingGraphQL(
        {
            "playbookInsertNode": {"nodeId": "n1", "linkId": "l1"},
            "playbook": {"id": "pb1", "playbook_definition": json.dumps(graph)},
            "playbookComponents": [
                {
                    "id": "comp-1",
                    "configuration_schema": '{"type": "object", "properties": {"interval": {"type": "number"}}}',
                }
            ],
        }
    )
    monkeypatch.setattr(playbooks, "_graphql", fake)
    result = await _run(
        executor,
        "edit_opencti_playbook_graph",
        {
            "action": "insert_node",
            "playbook_id": "pb1",
            "parent_node_id": "p1",
            "parent_port_id": "out",
            "child_node_id": "c1",
            "name": "Enrich",
            "component_id": "comp-1",
        },
    )
    assert not result.is_error, result.content
    assert "Inserted node" in result.content
    _, variables = [c for c in fake.calls if "InsertNode" in c[0]][-1]
    assert variables["input"]["name"] == "Enrich"
    assert variables["input"]["component_id"] == "comp-1"


async def test_profile_update_uses_user_name_edit_key(monkeypatch):
    # meEdit only accepts keys in OpenCTI's ME_USER_MODIFIABLE_ATTRIBUTES
    # allowlist, which lists `user_name` (NOT `name`) for the display name and
    # throws ForbiddenAccess for any other key. Lock the key in so it is not
    # "normalised" to `name` (which would break profile name updates).
    from app.integrations.providers.opencti import access

    executor, _ = _build()
    fake = _RecordingGraphQL({"meEdit": {"id": "u1", "name": "Renamed"}})
    monkeypatch.setattr(access, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_profile",
        {"action": "update", "name": "Renamed"},
    )
    assert not result.is_error, result.content
    assert fake.last_input_keys() == {"user_name"}


async def test_playbook_node_invalid_configuration_rejected(monkeypatch):
    # `configuration` is documented as a JSON string; a malformed value must be
    # rejected upfront (like update_positions) instead of forwarded to the API.
    from app.integrations.providers.opencti import playbooks

    executor, _ = _build()
    fake = _RecordingGraphQL({"playbookAddNode": "n1"})
    monkeypatch.setattr(playbooks, "_graphql", fake)
    result = await _run(
        executor,
        "edit_opencti_playbook_graph",
        {
            "action": "add_node",
            "playbook_id": "pb1",
            "name": "Enrich",
            "component_id": "comp-1",
            "configuration": "{not valid json",
        },
    )
    assert not result.is_error, result.content
    assert "configuration must be valid JSON" in result.content
    assert fake.calls == []  # no malformed mutation was sent


async def test_playbook_node_valid_configuration_forwarded(monkeypatch):
    from app.integrations.providers.opencti import playbooks

    executor, _ = _build()
    fake = _RecordingGraphQL({
        "playbookAddNode": "n1",
        "playbookComponents": [
            {"id": "comp-1", "configuration_schema": '{"type": "object", "properties": {"interval": {"type": "number"}}}'}
        ],
    })
    monkeypatch.setattr(playbooks, "_graphql", fake)
    result = await _run(
        executor,
        "edit_opencti_playbook_graph",
        {
            "action": "add_node",
            "playbook_id": "pb1",
            "name": "Enrich",
            "component_id": "comp-1",
            "configuration": '{"interval": 5}',
        },
    )
    assert not result.is_error, result.content
    assert "Added node" in result.content
    # The validated JSON string is forwarded verbatim (the API field is a String).
    _, variables = [c for c in fake.calls if "AddNode" in c[0]][-1]
    assert variables["input"]["configuration"] == '{"interval": 5}'


async def test_workspace_add_entities_message_is_workspace_neutral(monkeypatch):
    # add_entities/remove_entities also serve dashboards, so the result message
    # must not hardcode "Investigation".
    from app.integrations.providers.opencti import workspaces

    executor, _ = _build()
    fake = _RecordingGraphQL(
        {"workspaceFieldPatch": {"id": "w1", "investigated_entities_ids": ["e1", "e2"]}}
    )
    monkeypatch.setattr(workspaces, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_workspace",
        {"action": "add_entities", "workspace_id": "w1", "entity_ids": ["e1", "e2"]},
    )
    assert not result.is_error, result.content
    assert "Workspace now has 2 entities" in result.content
    assert "Investigation now has" not in result.content


# ── Connector catalog + managed-connector deployment ─────────────────────


async def test_catalog_list_parses_contract_json(monkeypatch):
    # `catalogs` returns contracts as JSON strings; the tool must parse them and
    # surface a per-catalog connector count.
    import json as _json

    from app.integrations.providers.opencti import connectors

    executor, _ = _build()
    monkeypatch.setattr(
        connectors,
        "_graphql",
        _RecordingGraphQL(
            {
                "catalogs": [
                    {
                        "id": "cat1",
                        "name": "Filigran",
                        "description": "official",
                        "contracts": [
                            _json.dumps(
                                {
                                    "title": "AlienVault",
                                    "slug": "alienvault",
                                    "container_image": "opencti/connector-alienvault:6",
                                    "manager_supported": True,
                                }
                            )
                        ],
                    }
                ]
            }
        ),
    )
    result = await _run(executor, "query_opencti_catalog", {"action": "list"})
    assert not result.is_error, result.content
    assert "Filigran" in result.content
    assert "1 connectors" in result.content


async def test_catalog_list_logs_unparseable_contract(monkeypatch, caplog):
    # A contract whose JSON can't be parsed is skipped (so one bad entry doesn't
    # hide the rest) but must be logged at WARNING so the now-missing connector
    # is diagnosable instead of silently disappearing.
    import json as _json
    import logging

    from app.integrations.providers.opencti import connectors

    executor, _ = _build()
    monkeypatch.setattr(
        connectors,
        "_graphql",
        _RecordingGraphQL(
            {
                "catalogs": [
                    {
                        "id": "cat1",
                        "name": "Filigran",
                        "description": "official",
                        "contracts": [
                            _json.dumps(
                                {
                                    "title": "AlienVault",
                                    "slug": "alienvault",
                                    "manager_supported": True,
                                }
                            ),
                            "{not valid json",  # unparseable → skipped + logged
                        ],
                    }
                ]
            }
        ),
    )
    with caplog.at_level(
        logging.WARNING, logger="app.integrations.providers.opencti.connectors"
    ):
        result = await _run(executor, "query_opencti_catalog", {"action": "list"})
    assert not result.is_error, result.content
    # The parseable connector still loads (the bad one is skipped, not fatal).
    assert "1 connectors" in result.content
    # ...and the skip was logged for operational diagnosability.
    assert "unparseable connector contract" in caplog.text
    assert "cat1" in caplog.text


async def test_catalog_get_surfaces_required_and_secret_params(monkeypatch):
    import json as _json

    from app.integrations.providers.opencti import connectors

    executor, _ = _build()
    contract = {
        "title": "AlienVault",
        "slug": "alienvault",
        "container_image": "opencti/connector-alienvault:6",
        "container_type": "EXTERNAL_IMPORT",
        "manager_supported": True,
        "config_schema": {
            "type": "object",
            "required": ["ALIENVAULT_API_KEY"],
            "properties": {
                "ALIENVAULT_API_KEY": {
                    "type": "string",
                    "format": "password",
                    "description": "Your OTX key",
                },
                "ALIENVAULT_TLP": {"type": "string", "default": "White"},
            },
        },
    }
    monkeypatch.setattr(
        connectors,
        "_graphql",
        _RecordingGraphQL(
            {"contract": {"catalog_id": "cat1", "contract": _json.dumps(contract)}}
        ),
    )
    result = await _run(
        executor, "query_opencti_catalog", {"action": "get", "slug": "alienvault"}
    )
    assert not result.is_error, result.content
    assert "ALIENVAULT_API_KEY" in result.content
    assert "REQUIRED" in result.content
    assert "secret" in result.content
    assert "opencti/connector-alienvault:6" in result.content


async def test_connector_deploy_builds_contract_configuration(monkeypatch):
    # The flat config JSON object must be converted to the API's
    # [{key, value}] ContractConfigInput list, with all values stringified.
    from app.integrations.providers.opencti import connectors

    executor, _ = _build()
    fake = _RecordingGraphQL(
        {"managedConnectorAdd": {"id": "conn1", "name": "AlienVault"}}
    )
    monkeypatch.setattr(connectors, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_connector",
        {
            "action": "deploy",
            "name": "AlienVault",
            "manager_contract_image": "opencti/connector-alienvault:6",
            "catalog_id": "cat1",
            "config": '{"ALIENVAULT_API_KEY": "secret-key", "ALIENVAULT_TLP": "White"}',
            "user_id": "svc-alienvault",
            "automatic_user": True,
        },
    )
    assert not result.is_error, result.content
    assert "Deployed connector" in result.content
    _, variables = fake.calls[-1]
    inp = variables["input"]
    assert inp["manager_contract_image"] == "opencti/connector-alienvault:6"
    assert inp["automatic_user"] is True
    cfg = {c["key"]: c["value"] for c in inp["manager_contract_configuration"]}
    assert cfg == {"ALIENVAULT_API_KEY": "secret-key", "ALIENVAULT_TLP": "White"}


def _managed_connector(logs, *, managed=True, uptime=3725):
    return {
        "connector": {
            "id": "conn1",
            "name": "AlienVault",
            "is_managed": managed,
            "manager_current_status": "started",
            "manager_requested_status": "started",
            "manager_connector_uptime": uptime,
            "manager_connector_logs": logs,
        }
    }


async def test_connector_logs_returns_the_last_lines_oldest_first(monkeypatch):
    # action='logs' reads Connector.manager_connector_logs (pushed by XTM
    # Composer) and returns the last `tail` lines, oldest first, under a header
    # naming the connector, its status and its uptime.
    from app.integrations.providers.opencti import connectors

    executor, tools = _build()
    schema = {t.name: t for t in tools}["manage_opencti_connector"].input_schema
    assert "logs" in schema["properties"]["action"]["enum"]
    assert schema["properties"]["tail"]["maximum"] == 500

    logs = [f"line {i}" for i in range(30)] + [
        "ValueError: Unable to read/access the entity, please check the "
        "connector permissions"
    ]
    fake = _RecordingGraphQL(_managed_connector(logs))
    monkeypatch.setattr(connectors, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_connector",
        {"action": "logs", "connector_id": "conn1", "tail": 3},
    )
    assert not result.is_error, result.content
    content = result.content
    assert "AlienVault" in content
    assert "status: started" in content
    assert "uptime: 1h 2m" in content
    assert "line 28" in content and "line 29" in content
    assert "line 27" not in content
    assert content.index("line 28") < content.index("line 29") < content.index(
        "Unable to read/access the entity"
    )
    query, variables = fake.calls[-1]
    assert "manager_connector_logs" in query
    assert variables == {"id": "conn1"}


async def test_connector_logs_tail_bounds(monkeypatch):
    from app.integrations.providers.opencti import connectors

    executor, _ = _build()
    fake = _RecordingGraphQL(_managed_connector([f"l{i}" for i in range(800)]))
    monkeypatch.setattr(connectors, "_graphql", fake)

    # Default tail is 50 lines.
    result = await _run(
        executor, "manage_opencti_connector", {"action": "logs", "connector_id": "conn1"}
    )
    assert "Last 50 log line(s)" in result.content
    assert "l750" in result.content and "l749\n" not in result.content

    # Above the maximum is clamped to 500.
    result = await _run(
        executor,
        "manage_opencti_connector",
        {"action": "logs", "connector_id": "conn1", "tail": 9999},
    )
    assert "Last 500 log line(s)" in result.content

    # Zero or garbage is refused before any GraphQL call.
    calls = len(fake.calls)
    for bad in (0, "many"):
        result = await _run(
            executor,
            "manage_opencti_connector",
            {"action": "logs", "connector_id": "conn1", "tail": bad},
        )
        assert "tail must be an integer" in result.content
    assert len(fake.calls) == calls

    result = await _run(executor, "manage_opencti_connector", {"action": "logs"})
    assert "connector_id is required" in result.content


async def test_connector_logs_bounds_and_redacts_lines(monkeypatch):
    from app.integrations.providers.opencti import connectors

    executor, _ = _build()
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    logs = ["x" * 5000 for _ in range(60)] + [f"auth failed token={token}"]
    fake = _RecordingGraphQL(_managed_connector(logs))
    monkeypatch.setattr(connectors, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_connector",
        {"action": "logs", "connector_id": "conn1", "tail": 61},
    )
    content = result.content
    assert token not in content
    assert "[line cut]" in content
    assert "older line(s) left out" in content
    assert "auth failed" in content
    assert len(content) < connectors._LOG_TOTAL_CAP + 2000


async def test_connector_logs_unmanaged_and_empty(monkeypatch):
    from app.integrations.providers.opencti import connectors

    executor, _ = _build()
    monkeypatch.setattr(
        connectors, "_graphql", _RecordingGraphQL(_managed_connector(None, managed=False))
    )
    result = await _run(
        executor, "manage_opencti_connector", {"action": "logs", "connector_id": "conn1"}
    )
    assert "is not a managed (XTM Composer) connector" in result.content

    monkeypatch.setattr(
        connectors, "_graphql", _RecordingGraphQL(_managed_connector([]))
    )
    result = await _run(
        executor, "manage_opencti_connector", {"action": "logs", "connector_id": "conn1"}
    )
    assert "No logs reported by the connector manager yet" in result.content

    monkeypatch.setattr(connectors, "_graphql", _RecordingGraphQL({"connector": None}))
    result = await _run(
        executor, "manage_opencti_connector", {"action": "logs", "connector_id": "nope"}
    )
    assert "Connector nope not found" in result.content


async def test_connector_logs_on_an_opencti_without_the_field(monkeypatch):
    from app.integrations.providers.opencti import connectors

    executor, _ = _build()

    async def old_opencti(client, base_url, headers, query, variables=None):
        if "manager_connector_logs" in query:
            return {}, (
                'OpenCTI GraphQL error: Cannot query field "manager_connector_logs" '
                'on type "Connector".'
            )
        return {
            "connector": {
                "id": "conn1",
                "name": "AlienVault",
                "is_managed": True,
                "connector_type": "EXTERNAL_IMPORT",
                "manager_current_status": "started",
                "manager_requested_status": "started",
            }
        }, None

    monkeypatch.setattr(connectors, "_graphql", old_opencti)
    result = await _run(
        executor, "manage_opencti_connector", {"action": "logs", "connector_id": "conn1"}
    )
    assert "does not expose managed connector logs" in result.content

    # 'get' still answers, without the log excerpt.
    result = await _run(
        executor, "manage_opencti_connector", {"action": "get", "connector_id": "conn1"}
    )
    assert not result.is_error, result.content
    assert "Connector 'AlienVault'" in result.content
    assert "log line" not in result.content


async def test_connector_get_shows_the_last_log_lines(monkeypatch):
    from app.integrations.providers.opencti import connectors

    executor, _ = _build()
    data = _managed_connector([f"entry {i}" for i in range(12)])
    data["connector"]["connector_type"] = "EXTERNAL_IMPORT"
    monkeypatch.setattr(connectors, "_graphql", _RecordingGraphQL(data))
    result = await _run(
        executor, "manage_opencti_connector", {"action": "get", "connector_id": "conn1"}
    )
    assert not result.is_error, result.content
    assert "last 5 log line(s)" in result.content
    assert "entry 11" in result.content and "entry 7" in result.content
    assert "entry 6" not in result.content


async def test_connector_deploy_sends_catalog_id(monkeypatch):
    # catalog_id is a REQUIRED top-level field of AddManagedConnectorInput
    # (typed ID!): the deploy mutation fails server-side without it (the symptom
    # was "the GraphQL mutation requires catalog_id as a top-level field"). The
    # value the caller passes must be forwarded verbatim in the input, and the
    # tool schema must expose catalog_id so the LLM can supply it.
    from app.integrations.providers.opencti import connectors

    executor, tools = _build()

    schema = {t.name: t for t in tools}["manage_opencti_connector"].input_schema
    assert "catalog_id" in schema["properties"], schema

    fake = _RecordingGraphQL(
        {"managedConnectorAdd": {"id": "conn1", "name": "AlienVault"}}
    )
    monkeypatch.setattr(connectors, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_connector",
        {
            "action": "deploy",
            "name": "AlienVault",
            "manager_contract_image": "opencti/connector-alienvault:6",
            "catalog_id": "cat-123",
            "config": "{}",
            "user_id": "svc",
        },
    )
    assert not result.is_error, result.content
    assert fake.calls[-1][1]["input"]["catalog_id"] == "cat-123"


async def test_connector_deploy_resolves_catalog_id_from_image(monkeypatch):
    # When catalog_id is omitted, the handler resolves it from the catalog that
    # owns the manager_contract_image so a deploy still succeeds. It must NOT send
    # the deploy mutation without a catalog_id.
    import json as _json

    from app.integrations.providers.opencti import connectors

    executor, _ = _build()

    async def routed(client, base_url, headers, query, variables=None):
        if "catalogs" in query:
            return {
                "catalogs": [
                    {
                        "id": "cat-resolved",
                        "name": "Filigran",
                        "description": "official",
                        "contracts": [
                            _json.dumps(
                                {
                                    "title": "AlienVault",
                                    "slug": "alienvault",
                                    "container_image": "opencti/connector-alienvault:6",
                                    "manager_supported": True,
                                }
                            )
                        ],
                    }
                ]
            }, None
        return {"managedConnectorAdd": {"id": "conn1", "name": "AlienVault"}}, None

    calls: list[tuple[str, dict | None]] = []

    async def recording(client, base_url, headers, query, variables=None):
        calls.append((query, variables))
        return await routed(client, base_url, headers, query, variables)

    monkeypatch.setattr(connectors, "_graphql", recording)
    result = await _run(
        executor,
        "manage_opencti_connector",
        {
            "action": "deploy",
            "name": "AlienVault",
            "manager_contract_image": "opencti/connector-alienvault:6",
            "config": "{}",
            "user_id": "svc",
        },
    )
    assert not result.is_error, result.content
    deploy_call = next(c for c in calls if "managedConnectorAdd" in c[0])
    assert deploy_call[1]["input"]["catalog_id"] == "cat-resolved"


async def test_connector_deploy_forwards_string_confidence_level(monkeypatch):
    # confidence_level is exposed to the LLM as a numeric 0-100 field, but
    # OpenCTI's AddManagedConnectorInput types it as String (the resolver
    # parseInt()s it). The handler must stringify it before sending — a raw int
    # is rejected by GraphQL variable coercion for a String field. It must still
    # guard with ``is not None`` so an explicit 0 is sent (a falsy check would
    # silently drop it).
    from app.integrations.providers.opencti import connectors

    executor, tools = _build()

    # Schema contract surfaced to the LLM provider must be integer (0-100).
    schema = {t.name: t for t in tools}["manage_opencti_connector"].input_schema
    conf = schema["properties"]["confidence_level"]
    assert conf["type"] == "integer", conf
    assert conf.get("minimum") == 0 and conf.get("maximum") == 100, conf

    # A provided value (including 0) is forwarded as a STRING.
    for value in (80, 0):
        fake = _RecordingGraphQL(
            {"managedConnectorAdd": {"id": "c1", "name": "AlienVault"}}
        )
        monkeypatch.setattr(connectors, "_graphql", fake)
        result = await _run(
            executor,
            "manage_opencti_connector",
            {
                "action": "deploy",
                "name": "AlienVault",
                "manager_contract_image": "img:1",
                "catalog_id": "cat1",
                "config": "{}",
                "user_id": "svc",
                "automatic_user": True,
                "confidence_level": value,
            },
        )
        assert not result.is_error, result.content
        inp = fake.calls[-1][1]["input"]
        assert inp["confidence_level"] == str(value)
        assert isinstance(inp["confidence_level"], str)

    # When omitted, the key is absent (not sent as an empty / null value).
    fake = _RecordingGraphQL(
        {"managedConnectorAdd": {"id": "c1", "name": "AlienVault"}}
    )
    monkeypatch.setattr(connectors, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_connector",
        {
            "action": "deploy",
            "name": "AlienVault",
            "manager_contract_image": "img:1",
            "catalog_id": "cat1",
            "config": "{}",
            "user_id": "svc",
        },
    )
    assert not result.is_error, result.content
    assert "confidence_level" not in fake.calls[-1][1]["input"]


async def test_connector_deploy_defaults_confidence_from_contract(monkeypatch):
    # When deploying with automatic_user and NO explicit confidence_level, the
    # handler must default the auto-created service account's confidence to the
    # connector contract's max_confidence_level — mirroring OpenCTI's own deploy
    # UI. Omitting it makes OpenCTI create the service account with no confidence
    # at all, which can let the connector overwrite higher-confidence data.
    import json as _json

    from app.integrations.providers.opencti import connectors

    executor, _ = _build()

    async def routed(client, base_url, headers, query, variables=None):
        if "catalogs" in query:
            return {
                "catalogs": [
                    {
                        "id": "cat-resolved",
                        "name": "Filigran",
                        "description": "official",
                        "contracts": [
                            _json.dumps(
                                {
                                    "title": "AlienVault",
                                    "slug": "alienvault",
                                    "container_image": "opencti/connector-alienvault:6",
                                    "manager_supported": True,
                                    "max_confidence_level": 80,
                                }
                            )
                        ],
                    }
                ]
            }, None
        return {"managedConnectorAdd": {"id": "conn1", "name": "AlienVault"}}, None

    calls: list[tuple[str, dict | None]] = []

    async def recording(client, base_url, headers, query, variables=None):
        calls.append((query, variables))
        return await routed(client, base_url, headers, query, variables)

    monkeypatch.setattr(connectors, "_graphql", recording)
    result = await _run(
        executor,
        "manage_opencti_connector",
        {
            "action": "deploy",
            "name": "AlienVault",
            "manager_contract_image": "opencti/connector-alienvault:6",
            "config": "{}",
            "user_id": "svc-alienvault",
            "automatic_user": True,
        },
    )
    assert not result.is_error, result.content
    deploy_call = next(c for c in calls if "managedConnectorAdd" in c[0])
    inp = deploy_call[1]["input"]
    # Contract default applied, stringified for the String-typed GraphQL field.
    assert inp["confidence_level"] == "80"
    assert isinstance(inp["confidence_level"], str)
    # The deploy summary tells the operator the contract default was used.
    assert "confidence level 80" in result.content
    assert "contract default" in result.content


async def test_connector_deploy_explicit_confidence_overrides_contract_default(monkeypatch):
    # An explicit confidence_level always wins over the contract default and must
    # be forwarded verbatim (stringified) without the contract-default note.
    from app.integrations.providers.opencti import connectors

    executor, _ = _build()
    fake = _RecordingGraphQL(
        {"managedConnectorAdd": {"id": "c1", "name": "AlienVault"}}
    )
    monkeypatch.setattr(connectors, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_connector",
        {
            "action": "deploy",
            "name": "AlienVault",
            "manager_contract_image": "opencti/connector-alienvault:6",
            "catalog_id": "cat1",
            "config": "{}",
            "user_id": "svc",
            "automatic_user": True,
            "confidence_level": 30,
        },
    )
    assert not result.is_error, result.content
    # Explicit value wins; an explicit confidence short-circuits the catalog
    # lookup, so the only GraphQL call is the deploy mutation itself.
    assert all("catalogs" not in q for q, _ in fake.calls), fake.calls
    inp = fake.calls[-1][1]["input"]
    assert inp["confidence_level"] == "30"
    assert "confidence level 30" in result.content
    assert "contract default" not in result.content


async def test_connector_deploy_rejects_invalid_confidence_level(monkeypatch):
    # The tool executor forwards the LLM's raw JSON argument without coercion, so
    # a malformed (bool, non-numeric string) or out-of-range confidence_level
    # must be rejected up front rather than stringified and sent to OpenCTI
    # (which parseInt()s it) — otherwise the service account is created with an
    # unintended/absent confidence, the exact risk this PR guards against. No
    # GraphQL call (catalog lookup or deploy mutation) may be issued for an
    # invalid value.
    from app.integrations.providers.opencti import connectors

    executor, _ = _build()
    for bad in (150, -1, True, "abc"):
        fake = _RecordingGraphQL(
            {"managedConnectorAdd": {"id": "c1", "name": "AlienVault"}}
        )
        monkeypatch.setattr(connectors, "_graphql", fake)
        result = await _run(
            executor,
            "manage_opencti_connector",
            {
                "action": "deploy",
                "name": "AlienVault",
                "manager_contract_image": "img:1",
                "catalog_id": "cat1",
                "config": "{}",
                "user_id": "svc",
                "automatic_user": True,
                "confidence_level": bad,
            },
        )
        assert not result.is_error, result.content
        assert "between 0 and 100" in result.content, (bad, result.content)
        # Fail fast: nothing was sent to OpenCTI for an invalid value.
        assert fake.calls == [], (bad, fake.calls)


async def test_catalog_get_surfaces_default_confidence_level(monkeypatch):
    # The contract's max_confidence_level is the default confidence applied to a
    # deploy's auto-created service account, so action='get' must surface it so
    # the agent (and operator) knows what will be used.
    import json as _json

    from app.integrations.providers.opencti import connectors

    executor, _ = _build()
    contract = {
        "title": "AlienVault",
        "slug": "alienvault",
        "container_image": "opencti/connector-alienvault:6",
        "container_type": "EXTERNAL_IMPORT",
        "manager_supported": True,
        "max_confidence_level": 75,
        "config_schema": {
            "type": "object",
            "required": [],
            "properties": {},
        },
    }
    monkeypatch.setattr(
        connectors,
        "_graphql",
        _RecordingGraphQL(
            {"contract": {"catalog_id": "cat1", "contract": _json.dumps(contract)}}
        ),
    )
    result = await _run(
        executor, "query_opencti_catalog", {"action": "get", "slug": "alienvault"}
    )
    assert not result.is_error, result.content
    assert "default confidence level: 75" in result.content


async def test_connector_deploy_and_edit_declare_non_null_input(monkeypatch):
    # OpenCTI's managedConnectorAdd / managedConnectorEdit take a NON-NULL input
    # (AddManagedConnectorInput! / EditManagedConnectorInput!). A nullable
    # variable declaration is rejected at GraphQL validation ("variable of type
    # X used in position expecting X!"), so deploy / update_config would fail
    # against a real platform. Pin the `!` on both mutations.
    from app.integrations.providers.opencti import connectors

    executor, _ = _build()

    deploy_fake = _RecordingGraphQL(
        {"managedConnectorAdd": {"id": "c1", "name": "AlienVault"}}
    )
    monkeypatch.setattr(connectors, "_graphql", deploy_fake)
    deploy_result = await _run(
        executor,
        "manage_opencti_connector",
        {
            "action": "deploy",
            "name": "AlienVault",
            "manager_contract_image": "img:1",
            "catalog_id": "cat1",
            "config": "{}",
            "user_id": "svc",
        },
    )
    assert not deploy_result.is_error, deploy_result.content
    deploy_query = deploy_fake.calls[-1][0]
    assert "$input: AddManagedConnectorInput!" in deploy_query, deploy_query

    edit_fake = _RecordingGraphQL(
        {
            "connector": {"id": "c1", "name": "AlienVault", "connector_user_id": "svc"},
            "managedConnectorEdit": {"id": "c1", "name": "AlienVault"},
        }
    )
    monkeypatch.setattr(connectors, "_graphql", edit_fake)
    edit_result = await _run(
        executor,
        "manage_opencti_connector",
        {
            "action": "update_config",
            "connector_id": "c1",
            "name": "AlienVault",
            "config": '{"ALIENVAULT_TLP": "White"}',
            "user_id": "svc",
        },
    )
    assert not edit_result.is_error, edit_result.content
    edit_query = edit_fake.calls[-1][0]
    assert "$input: EditManagedConnectorInput!" in edit_query, edit_query


async def test_connector_config_sends_json_booleans(monkeypatch):
    # OpenCTI validates manager_contract_configuration values against the
    # contract: a boolean field takes "true"/"false" only and refuses Python's
    # str(True) == "True" ("must be a boolean value (true or false)").
    from app.integrations.providers.opencti import connectors

    executor, _ = _build()
    config = (
        '{"MATCH_SINGLE": true, "VERBOSE": false, "PERIOD": 60, '
        '"TLP": "White", "EMPTY": null, "TYPES": ["report", "note"]}'
    )
    expected = {
        "MATCH_SINGLE": "true",
        "VERBOSE": "false",
        "PERIOD": "60",
        "TLP": "White",
        "EMPTY": "",
        "TYPES": '["report","note"]',
    }

    edit_fake = _RecordingGraphQL({"managedConnectorEdit": {"id": "c1", "name": "Fetcher"}})
    # update_config reads the stored configuration before the edit.
    edit_fake.response["connector"] = {"id": "c1", "name": "Fetcher", "connector_user_id": "svc"}
    monkeypatch.setattr(connectors, "_graphql", edit_fake)
    edit_result = await _run(
        executor,
        "manage_opencti_connector",
        {
            "action": "update_config",
            "connector_id": "c1",
            "name": "Fetcher",
            "config": config,
            "user_id": "svc",
        },
    )
    assert not edit_result.is_error, edit_result.content
    inp = edit_fake.calls[-1][1]["input"]
    sent = {c["key"]: c["value"] for c in inp["manager_contract_configuration"]}
    assert sent == expected

    deploy_fake = _RecordingGraphQL({"managedConnectorAdd": {"id": "c2", "name": "Fetcher"}})
    monkeypatch.setattr(connectors, "_graphql", deploy_fake)
    deploy_result = await _run(
        executor,
        "manage_opencti_connector",
        {
            "action": "deploy",
            "name": "Fetcher",
            "manager_contract_image": "img:1",
            "catalog_id": "cat1",
            "config": config,
            "user_id": "svc",
        },
    )
    assert not deploy_result.is_error, deploy_result.content
    inp = deploy_fake.calls[-1][1]["input"]
    sent = {c["key"]: c["value"] for c in inp["manager_contract_configuration"]}
    assert sent == expected


async def test_connector_deploy_requires_image_and_user():
    executor, _ = _build()
    missing_image = await _run(
        executor,
        "manage_opencti_connector",
        {"action": "deploy", "name": "AlienVault"},
    )
    assert not missing_image.is_error, missing_image.content
    assert "manager_contract_image is required" in missing_image.content

    missing_user = await _run(
        executor,
        "manage_opencti_connector",
        {
            "action": "deploy",
            "name": "AlienVault",
            "manager_contract_image": "img:1",
        },
    )
    assert not missing_user.is_error, missing_user.content
    assert "user_id is required" in missing_user.content


async def test_connector_start_uses_requested_status(monkeypatch):
    from app.integrations.providers.opencti import connectors

    executor, _ = _build()
    fake = _RecordingGraphQL(
        {"updateConnectorRequestedStatus": {"id": "conn1", "name": "AlienVault"}}
    )
    monkeypatch.setattr(connectors, "_graphql", fake)
    result = await _run(
        executor,
        "manage_opencti_connector",
        {"action": "start", "connector_id": "conn1"},
    )
    assert not result.is_error, result.content
    _, variables = fake.calls[-1]
    assert variables["input"] == {"id": "conn1", "status": "starting"}


async def test_connector_list_filters_managed_only(monkeypatch):
    from app.integrations.providers.opencti import connectors

    executor, _ = _build()
    monkeypatch.setattr(
        connectors,
        "_graphql",
        _RecordingGraphQL(
            {
                "connectors": [
                    {"id": "c1", "name": "Managed", "is_managed": True,
                     "manager_current_status": "started"},
                    {"id": "c2", "name": "External", "is_managed": False},
                ]
            }
        ),
    )
    result = await _run(executor, "manage_opencti_connector", {"action": "list"})
    assert not result.is_error, result.content
    assert "Managed" in result.content
    assert "External" not in result.content


async def test_catalog_contracts_handles_null_search(monkeypatch):
    # The LLM can emit ``search: null`` even though the schema says string;
    # ``contracts`` must not raise AttributeError on ``None.strip()``.
    import json as _json

    from app.integrations.providers.opencti import connectors

    executor, _ = _build()
    monkeypatch.setattr(
        connectors,
        "_graphql",
        _RecordingGraphQL(
            {
                "catalogs": [
                    {
                        "id": "cat1",
                        "name": "Filigran",
                        "description": "official",
                        "contracts": [
                            _json.dumps(
                                {
                                    "title": "AlienVault",
                                    "slug": "alienvault",
                                    "manager_supported": True,
                                }
                            )
                        ],
                    }
                ]
            }
        ),
    )
    result = await _run(
        executor,
        "query_opencti_catalog",
        {"action": "contracts", "search": None},
    )
    assert not result.is_error, result.content
    assert "AlienVault" in result.content


# OpenCTI master src/modules/connector/connector.graphql:
# type ManagerContractConfiguration { key: String!  value: String! }
_MANAGER_CONTRACT_CONFIGURATION_FIELDS = {"key", "value"}


async def test_connector_get_selects_only_fields_the_schema_has(monkeypatch):
    # Selecting a field the type lacks (``encrypted``) failed every 'get' with
    # GRAPHQL_VALIDATION_FAILED; the selection is pinned to the schema shape.
    import re

    from app.integrations.providers.opencti import connectors

    executor, _ = _build()
    gql = _RecordingGraphQL({"connector": {"id": "conn1", "name": "AlienVault"}})
    monkeypatch.setattr(connectors, "_graphql", gql)
    result = await _run(
        executor,
        "manage_opencti_connector",
        {"action": "get", "connector_id": "conn1"},
    )
    assert not result.is_error, result.content
    query, variables = gql.calls[0]
    assert variables == {"id": "conn1"}
    selection = re.search(r"manager_contract_configuration \{([^}]*)\}", query)
    assert selection is not None, query
    assert set(selection.group(1).split()) == _MANAGER_CONTRACT_CONFIGURATION_FIELDS
    assert "encrypted" not in query


async def test_connector_get_redacts_secret_values(monkeypatch):
    # action='get' must never echo a secret value back to the agent. OpenCTI
    # does not say which value is a secret, so redaction runs on the key name
    # and on a ciphertext-looking value. Non-secret keys keep their value.
    from app.integrations.providers.opencti import connectors

    ciphertext = "Qm9ndXNDaXBoZXJ0ZXh0" * 8
    executor, _ = _build()
    monkeypatch.setattr(
        connectors,
        "_graphql",
        _RecordingGraphQL(
            {
                "connector": {
                    "id": "conn1",
                    "name": "AlienVault",
                    "is_managed": True,
                    "connector_type": "EXTERNAL_IMPORT",
                    "manager_contract_configuration": [
                        # secret-like key -> must redact
                        {"key": "ALIENVAULT_API_KEY", "value": "super-secret-otx-key"},
                        # neutral key, encrypted value -> must redact
                        {"key": "PROXY_AUTH", "value": ciphertext},
                        # non-secret -> plaintext is fine
                        {"key": "ALIENVAULT_TLP", "value": "White"},
                    ],
                }
            }
        ),
    )
    result = await _run(
        executor,
        "manage_opencti_connector",
        {"action": "get", "connector_id": "conn1"},
    )
    assert not result.is_error, result.content
    assert "super-secret-otx-key" not in result.content
    assert ciphertext not in result.content
    assert "ALIENVAULT_API_KEY" in result.content
    assert "<redacted (secret-like key)>" in result.content
    assert "<encrypted>" in result.content
    # Non-secret value is still shown.
    assert "White" in result.content


async def test_connector_get_reads_connector_state(monkeypatch):
    # 'get' asks OpenCTI for Connector.connector_state and its timestamp, so an
    # agent sees where a connector stands (after reset_state, for one).
    from app.integrations.providers.opencti import connectors

    executor, _ = _build()
    gql = _RecordingGraphQL(
        {
            "connector": {
                "id": "conn1",
                "name": "AlienVault",
                "connector_state": '{"last_run": 1763640000}',
                "connector_state_timestamp": "2026-11-20T12:00:00Z",
            }
        }
    )
    monkeypatch.setattr(connectors, "_graphql", gql)
    result = await _run(executor, "manage_opencti_connector", {"action": "get", "connector_id": "conn1"})
    assert not result.is_error, result.content
    assert any("connector_state connector_state_timestamp" in q for q, _ in gql.calls)
    assert '  connector_state: {"last_run": 1763640000}' in result.content
    assert "  connector_state_timestamp: 2026-11-20T12:00:00Z" in result.content


@pytest.mark.parametrize("state", [None, "", "   "])
async def test_connector_get_prints_none_for_an_empty_state(monkeypatch, state):
    from app.integrations.providers.opencti import connectors

    executor, _ = _build()
    monkeypatch.setattr(
        connectors,
        "_graphql",
        _RecordingGraphQL({"connector": {"id": "conn1", "name": "AlienVault", "connector_state": state}}),
    )
    result = await _run(executor, "manage_opencti_connector", {"action": "get", "connector_id": "conn1"})
    assert not result.is_error, result.content
    assert "  connector_state: none" in result.content
    assert "connector_state_timestamp" not in result.content


async def test_connector_get_caps_a_large_state(monkeypatch):
    from app.integrations.providers.opencti import connectors

    executor, _ = _build()
    big = "x" * 5000
    monkeypatch.setattr(
        connectors,
        "_graphql",
        _RecordingGraphQL({"connector": {"id": "conn1", "name": "AlienVault", "connector_state": big}}),
    )
    result = await _run(executor, "manage_opencti_connector", {"action": "get", "connector_id": "conn1"})
    assert not result.is_error, result.content
    assert big not in result.content
    assert "(5000 characters, cut)" in result.content


async def test_connector_get_answers_when_the_state_cannot_be_read(monkeypatch):
    # An OpenCTI without the state fields still answers 'get'.
    from app.integrations.providers.opencti import connectors

    executor, _ = _build()

    async def no_state(client, base_url, headers, query, variables=None):
        if "connector_state" in query:
            return {}, 'OpenCTI GraphQL error: Cannot query field "connector_state"'
        return {"connector": {"id": "conn1", "name": "AlienVault"}}, None

    monkeypatch.setattr(connectors, "_graphql", no_state)
    result = await _run(executor, "manage_opencti_connector", {"action": "get", "connector_id": "conn1"})
    assert not result.is_error, result.content
    assert "Connector 'AlienVault'" in result.content
    assert "connector_state: unreadable" in result.content


# ── Entity history selects fields that exist on ContextData ──────


async def test_entity_history_selects_external_references(monkeypatch):
    """``ContextData`` has ``external_references``, never ``references``: a
    selection naming the latter fails GraphQL validation on every call."""
    import re

    import app.integrations.providers.opencti.knowledge as knowledge_mod

    sent: list[str] = []

    async def _fake(client, base_url, headers, query, variables=None):
        sent.append(query)
        return {
            "logs": {
                "edges": [
                    {
                        "node": {
                            "id": "log-1",
                            "event_type": "mutation",
                            "event_scope": "update",
                            "timestamp": "2026-09-01T10:00:00Z",
                            "user": {"id": "u-1", "name": "Analyst"},
                            "context_data": {
                                "entity_name": "APT28",
                                "message": "adds `x` in `aliases`",
                                "external_references": [
                                    {
                                        "source_name": "mitre",
                                        "external_id": "G0007",
                                        "url": "https://attack.mitre.org/groups/G0007",
                                    }
                                ],
                            },
                        }
                    }
                ]
            }
        }, None

    monkeypatch.setattr(knowledge_mod, "_graphql", _fake)
    executor, _ = _build()
    result = await _run(
        executor, "get_opencti_entity_history", {"entity_id": "intrusion-set--x"}
    )
    assert "external_references {" in sent[0]
    assert not re.search(r"(?<![A-Za-z_])references\s*\{", sent[0])
    assert "Analyst" in result.content
    assert "[refs: mitre G0007 https://attack.mitre.org/groups/G0007]" in result.content
    assert not result.is_error


# ── add_opencti_label attaches before it creates ─────────────────


class _LabelGraphQL:
    """``knowledge._graphql`` stub for the label flow, recording every call."""

    def __init__(self, existing=None, create_error=None, near=None, by_name=None):
        self.existing = existing or []
        self.create_error = create_error
        self.near = near or []
        # What ``label(id:)`` resolves the name to (None: no such label).
        self.by_name = by_name
        self.calls: list[tuple[str, dict | None]] = []

    async def __call__(self, client, base_url, headers, query, variables=None):
        self.calls.append((query, variables))
        if "LabelByName" in query:
            return {"label": self.by_name}, None
        if "FindLabel" in query:
            return {"labels": {"edges": [{"node": n} for n in self.existing]}}, None
        if "NearLabels" in query:
            return {"labels": {"edges": [{"node": n} for n in self.near]}}, None
        if "CreateLabel" in query:
            if self.create_error:
                return {}, self.create_error
            return {"labelAdd": {"id": "lbl-new", "value": variables["input"]["value"]}}, None
        if "RefTarget" in query:
            return {"stixObjectOrStixRelationship": _SDO_TARGET}, None
        if "stixCoreObject(id" in query:
            return {"stixCoreObject": {}}, None
        return {}, None

    def sent(self, name: str) -> list[dict | None]:
        return [v for q, v in self.calls if name in q]


_DRAFT = {"opencti-draft-id": "draft-1"}
_FORBIDDEN = (
    "GraphQL error in 'CreateLabel': You are not allowed to do this. "
    "[FORBIDDEN_ACCESS] - permission denied: the token's role lacks the "
    "capability required for 'labelAdd', typically SETTINGS_SETLABELS."
)


async def test_add_label_reuses_a_case_insensitive_match(monkeypatch):
    import app.integrations.providers.opencti.knowledge as knowledge_mod

    stub = _LabelGraphQL(existing=[{"id": "lbl-apt28", "value": "apt28"}])
    monkeypatch.setattr(knowledge_mod, "_graphql", stub)
    executor, _ = _build(scope_headers=_DRAFT)
    result = await _run(
        executor, "add_opencti_label", {"entity_id": "x", "label_name": "APT28"}
    )
    assert stub.sent("CreateLabel") == [], "an existing label must not be re-created"
    assert stub.sent("AddLabel")[0]["input"]["toId"] == "lbl-apt28"
    assert "Label 'apt28' added (the existing label matching 'APT28')" in result.content
    assert not result.is_error


async def test_add_label_prefers_the_exact_value(monkeypatch):
    import app.integrations.providers.opencti.knowledge as knowledge_mod

    stub = _LabelGraphQL(
        existing=[{"id": "lbl-a", "value": "apt28 "}, {"id": "lbl-b", "value": "APT28"}]
    )
    monkeypatch.setattr(knowledge_mod, "_graphql", stub)
    executor, _ = _build(scope_headers=_DRAFT)
    await _run(executor, "add_opencti_label", {"entity_id": "x", "label_name": "APT28"})
    assert stub.sent("AddLabel")[0]["input"]["toId"] == "lbl-b"


async def test_add_label_matches_like_opencti_not_casefold(monkeypatch):
    """OpenCTI's label identity is trim + lower: 'strasse' is not 'Straße'."""
    import app.integrations.providers.opencti.knowledge as knowledge_mod

    stub = _LabelGraphQL(existing=[{"id": "lbl-folded", "value": "strasse"}])
    monkeypatch.setattr(knowledge_mod, "_graphql", stub)
    executor, _ = _build(scope_headers=_DRAFT)
    await _run(executor, "add_opencti_label", {"entity_id": "x", "label_name": "Straße"})
    assert stub.sent("CreateLabel")[0]["input"]["value"] == "Straße"
    assert stub.sent("AddLabel")[0]["input"]["toId"] == "lbl-new"


async def test_add_label_explains_a_refused_create(monkeypatch):
    import app.integrations.providers.opencti.knowledge as knowledge_mod

    stub = _LabelGraphQL(create_error=_FORBIDDEN, near=[{"id": "l", "value": "apt-28"}])
    monkeypatch.setattr(knowledge_mod, "_graphql", stub)
    executor, _ = _build(scope_headers=_DRAFT)
    result = await _run(
        executor, "add_opencti_label", {"entity_id": "x", "label_name": "APT28"}
    )
    assert result.content.startswith("Failed to add label 'APT28'")
    assert "SETTINGS_SETLABELS" in result.content
    assert "apt-28" in result.content
    assert stub.sent("AddLabel") == [], "nothing may be attached after a refused create"
    assert result.is_error


async def test_add_label_creates_a_missing_label(monkeypatch):
    import app.integrations.providers.opencti.knowledge as knowledge_mod

    stub = _LabelGraphQL()
    monkeypatch.setattr(knowledge_mod, "_graphql", stub)
    executor, _ = _build(scope_headers=_DRAFT)
    result = await _run(
        executor, "add_opencti_label", {"entity_id": "x", "label_name": " triaged "}
    )
    assert stub.sent("CreateLabel")[0]["input"]["value"] == "triaged"
    assert stub.sent("AddLabel")[0]["input"]["toId"] == "lbl-new"
    assert "Label 'triaged' added" in result.content


async def test_add_label_passes_other_create_errors_through(monkeypatch):
    import app.integrations.providers.opencti.knowledge as knowledge_mod

    stub = _LabelGraphQL(create_error="GraphQL error in 'CreateLabel': boom")
    monkeypatch.setattr(knowledge_mod, "_graphql", stub)
    executor, _ = _build(scope_headers=_DRAFT)
    result = await _run(
        executor, "add_opencti_label", {"entity_id": "x", "label_name": "x"}
    )
    assert result.content == "GraphQL error in 'CreateLabel': boom"
    assert stub.sent("NearLabels") == []


def test_label_identity_mirrors_opencti_normalize_name():
    """OpenCTI's label identity is ``normalizeName(value)`` =
    ``(name || '').toLowerCase().trim()`` (``schema/identifier.js``)."""
    from app.integrations.providers.opencti.knowledge import _label_identity

    assert _label_identity("  APT28 ") == "apt28"
    # JS ``trim()`` strips the BOM and every Unicode space separator ...
    assert _label_identity("\ufeffAPT28\u3000") == "apt28"
    # ... but not the ASCII separators Python's ``strip()`` would also drop.
    assert _label_identity("apt28\x1f") == "apt28\x1f"
    # Inner whitespace is kept, and lowercasing is not case folding.
    assert _label_identity("APT 28") == "apt 28"
    assert _label_identity("STRA\u00dfE") == "stra\u00dfe"


async def test_add_label_never_takes_an_accent_folded_candidate(monkeypatch):
    """The ``value`` filter folds case AND accents (lowercase + asciifolding
    keyword normalizer), so it also returns labels OpenCTI holds as different
    ones: ``strasse`` is not ``stra\u00dfe``, ``cafe`` is not ``caf\u00e9``."""
    import app.integrations.providers.opencti.knowledge as knowledge_mod

    for existing, asked in (("stra\u00dfe", "strasse"), ("caf\u00e9", "cafe")):
        stub = _LabelGraphQL(existing=[{"id": "lbl-folded", "value": existing}])
        monkeypatch.setattr(knowledge_mod, "_graphql", stub)
        executor, _ = _build(scope_headers=_DRAFT)
        result = await _run(
            executor, "add_opencti_label", {"entity_id": "x", "label_name": asked}
        )
        assert [v["input"]["value"] for v in stub.sent("CreateLabel")] == [asked]
        assert stub.sent("AddLabel")[0]["input"]["toId"] == "lbl-new"
        assert f"Label '{asked}' added" in result.content


async def test_add_label_refused_create_names_the_folded_candidates(monkeypatch):
    """The accent-folded labels the lookup returned are the closest spellings."""
    import app.integrations.providers.opencti.knowledge as knowledge_mod

    stub = _LabelGraphQL(
        existing=[{"id": "l-1", "value": "stra\u00dfe"}],
        create_error=_FORBIDDEN,
        near=[{"id": "l-1", "value": "stra\u00dfe"}, {"id": "l-2", "value": "strasse-2"}],
    )
    monkeypatch.setattr(knowledge_mod, "_graphql", stub)
    executor, _ = _build(scope_headers=_DRAFT)
    result = await _run(
        executor, "add_opencti_label", {"entity_id": "x", "label_name": "strasse"}
    )
    assert "Existing labels that look close: stra\u00dfe, strasse-2." in result.content
    assert stub.sent("AddLabel") == []
    assert result.is_error


async def test_add_label_reports_a_create_that_returned_nothing(monkeypatch):
    import app.integrations.providers.opencti.knowledge as knowledge_mod

    class _NullCreate(_LabelGraphQL):
        async def __call__(self, client, base_url, headers, query, variables=None):
            if "CreateLabel" in query:
                self.calls.append((query, variables))
                return {"labelAdd": None}, None
            return await super().__call__(client, base_url, headers, query, variables)

    stub = _NullCreate()
    monkeypatch.setattr(knowledge_mod, "_graphql", stub)
    executor, _ = _build(scope_headers=_DRAFT)
    result = await _run(
        executor, "add_opencti_label", {"entity_id": "x", "label_name": "triaged"}
    )
    assert result.content == "Failed to create label 'triaged'."
    assert stub.sent("AddLabel") == []
    assert result.is_error


async def test_remove_label_never_removes_an_accent_folded_label(monkeypatch):
    import app.integrations.providers.opencti.knowledge as knowledge_mod

    stub = _LabelGraphQL(existing=[{"id": "lbl-folded", "value": "stra\u00dfe"}])
    monkeypatch.setattr(knowledge_mod, "_graphql", stub)
    executor, _ = _build(scope_headers=_DRAFT)
    result = await _run(
        executor, "remove_opencti_label", {"entity_id": "x", "label_name": "strasse"}
    )
    assert stub.sent("RemoveLabel") == []
    assert result.content == "Label 'strasse' not found."


async def test_remove_label_matches_the_opencti_identity(monkeypatch):
    import app.integrations.providers.opencti.knowledge as knowledge_mod

    stub = _LabelGraphQL(existing=[{"id": "lbl-apt28", "value": "apt28"}])
    monkeypatch.setattr(knowledge_mod, "_graphql", stub)
    executor, _ = _build(scope_headers=_DRAFT)
    result = await _run(
        executor, "remove_opencti_label", {"entity_id": "x", "label_name": " APT28 "}
    )
    lookup = stub.sent("FindLabel")[0]["filters"]["filters"][0]["values"]
    assert lookup == ["APT28"]
    assert stub.sent("RemoveLabel")[0]["toId"] == "lbl-apt28"
    assert "Label 'apt28' removed" in result.content


# Accent variants the folded ``value`` filter returns before the real label.
_FOLDED_PAGE = [
    {"id": f"lbl-{i}", "value": v}
    for i, v in enumerate(("caf\u00e9", "caf\u00e8", "caf\u00ea", "caf\u00eb", "c\u00e0fe"))
]


async def test_add_label_resolves_the_name_before_the_folded_filter(monkeypatch):
    """``label(id: name)`` finds the one label OpenCTI holds as the name, which
    a page of accent-folded hits can leave out."""
    import app.integrations.providers.opencti.knowledge as knowledge_mod

    stub = _LabelGraphQL(
        existing=_FOLDED_PAGE,
        create_error=_FORBIDDEN,
        by_name={"id": "lbl-cafe", "value": "cafe"},
    )
    monkeypatch.setattr(knowledge_mod, "_graphql", stub)
    executor, _ = _build(scope_headers=_DRAFT)
    result = await _run(
        executor, "add_opencti_label", {"entity_id": "x", "label_name": " Cafe "}
    )
    assert stub.sent("LabelByName") == [{"id": "Cafe"}]
    assert stub.sent("FindLabel") == [], "the folded filter is only a fallback"
    assert stub.sent("CreateLabel") == []
    assert stub.sent("AddLabel")[0]["input"]["toId"] == "lbl-cafe"
    assert "Label 'cafe' added (the existing label matching 'Cafe')" in result.content
    assert not result.is_error


async def test_remove_label_resolves_the_name_before_the_folded_filter(monkeypatch):
    import app.integrations.providers.opencti.knowledge as knowledge_mod

    stub = _LabelGraphQL(existing=_FOLDED_PAGE, by_name={"id": "lbl-cafe", "value": "cafe"})
    monkeypatch.setattr(knowledge_mod, "_graphql", stub)
    executor, _ = _build(scope_headers=_DRAFT)
    result = await _run(
        executor, "remove_opencti_label", {"entity_id": "x", "label_name": "CAFE"}
    )
    assert stub.sent("FindLabel") == []
    assert stub.sent("RemoveLabel")[0]["toId"] == "lbl-cafe"
    assert result.content == "Label 'cafe' removed from entity x."


async def test_label_lookup_ignores_another_label_found_by_id(monkeypatch):
    """``label(id:)`` loads an id-shaped name as an id; a label found that way
    whose value is not the name is neither attached nor removed."""
    import app.integrations.providers.opencti.knowledge as knowledge_mod

    id_shaped = "0b6f2c6e-4a8e-4d6c-9d5f-0e2a1c3b4d5e"
    other = {"id": id_shaped, "value": "apt28"}

    stub = _LabelGraphQL(by_name=other)
    monkeypatch.setattr(knowledge_mod, "_graphql", stub)
    executor, _ = _build(scope_headers=_DRAFT)
    await _run(executor, "add_opencti_label", {"entity_id": "x", "label_name": id_shaped})
    assert stub.sent("FindLabel"), "the value filter must run"
    assert stub.sent("CreateLabel")[0]["input"]["value"] == id_shaped
    assert stub.sent("AddLabel")[0]["input"]["toId"] == "lbl-new"

    stub = _LabelGraphQL(by_name=other)
    monkeypatch.setattr(knowledge_mod, "_graphql", stub)
    executor, _ = _build(scope_headers=_DRAFT)
    result = await _run(
        executor, "remove_opencti_label", {"entity_id": "x", "label_name": id_shaped}
    )
    assert stub.sent("RemoveLabel") == []
    assert result.content == f"Label '{id_shaped}' not found."


async def test_label_lookup_error_is_returned_before_any_write(monkeypatch):
    import app.integrations.providers.opencti.knowledge as knowledge_mod

    class _FailingLookup(_LabelGraphQL):
        async def __call__(self, client, base_url, headers, query, variables=None):
            if "LabelByName" in query:
                self.calls.append((query, variables))
                return {}, "GraphQL error in 'LabelByName': boom"
            return await super().__call__(client, base_url, headers, query, variables)

    for tool in ("add_opencti_label", "remove_opencti_label"):
        stub = _FailingLookup()
        monkeypatch.setattr(knowledge_mod, "_graphql", stub)
        executor, _ = _build(scope_headers=_DRAFT)
        result = await _run(executor, tool, {"entity_id": "x", "label_name": "apt28"})
        assert result.content == "GraphQL error in 'LabelByName': boom"
        assert [q for q, _ in stub.calls if "LabelByName" not in q] == []


async def test_entity_history_bounds_the_references_it_renders(monkeypatch):
    """At most ``_HISTORY_MAX_REFS`` references per entry, each on one line and
    cut to a readable length; the rest are counted, not dropped silently."""
    import app.integrations.providers.opencti.knowledge as knowledge_mod

    refs = [
        {
            "source_name": "vendor\nblog",
            "external_id": f"E{i}",
            "url": "https://example.test/" + "a" * 500,
        }
        for i in range(5)
    ]

    async def _fake(client, base_url, headers, query, variables=None):
        node = {
            "id": "log-1",
            "event_type": "mutation",
            "event_scope": "update",
            "timestamp": "2026-09-01T10:00:00Z",
            "user": {"id": "u-1", "name": "Analyst"},
            "context_data": {"message": "updates `description`", "external_references": refs},
        }
        return {"logs": {"edges": [{"node": node}]}}, None

    monkeypatch.setattr(knowledge_mod, "_graphql", _fake)
    executor, _ = _build()
    result = await _run(
        executor, "get_opencti_entity_history", {"entity_id": "intrusion-set--x"}
    )
    lines = [line for line in result.content.splitlines() if "Analyst" in line]
    assert len(lines) == 1, result.content
    entry = lines[0]
    assert "vendor blog E0 https://example.test/" in entry
    assert "E2" in entry and "E3" not in entry
    assert entry.endswith(", +2 more]")
    assert len(entry) < 3 * (knowledge_mod._HISTORY_MAX_REF_CHARS + 2) + 200
