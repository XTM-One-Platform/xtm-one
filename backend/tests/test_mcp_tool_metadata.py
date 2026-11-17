"""What XTM One's MCP servers tell a client about each tool.

Every tool the six MCP endpoints list carries a ``title`` and the MCP tool
``annotations`` (``readOnlyHint``, ``destructiveHint``, ``idempotentHint``,
``openWorldHint``), read from its name by ``app/services/mcp_tool_metadata.py``
through the platform's one read/write classifier. These tests pin:

- every tool on the platform servers' allow-lists and on the ``/mcp/xtm``
  surface is READ from its name - none falls back to the specification's
  default - and fits the 64 characters hosted clients accept;
- no tool marked read-only names a destructive token unless its verb is a
  read (a wrong ``readOnlyHint: true`` skips the client's confirmation);
- reads, additive writes and destructive writes are told apart;
- an agent exposed as a tool is annotated as an agent run, and a slug too long
  for a tool name becomes a stable shortened name ``tools/call`` still resolves,
  as it does the full slug;
- the three MCP server modules serialize tools through the one serializer.
"""

import re
import uuid
from types import SimpleNamespace

import pytest

from app.api.v1 import mcp_endpoint, mcp_xtm
from app.api.v1.mcp_platform import MCP_TOOL_ALLOW_LISTS
from app.services import builtin_tools
from app.services.mcp_tool_metadata import (
    _DESTRUCTIVE_TOKENS,
    _READ_ONLY_NAMES,
    MAX_TOOL_NAME_LENGTH,
    agent_tool_annotations,
    agent_tool_name,
    annotations_read_from_name,
    tool_annotations,
    tool_title,
    tool_to_mcp,
    tools_to_mcp,
)
from app.services.tool_classification import classify_tool_name
from app.tools.base import ToolDefinition
from tests import app_sources

#: What a tool name may contain for every hosted client (Claude's own rule).
_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

_PLATFORM_TOOL_NAMES = sorted(
    {name for names in MCP_TOOL_ALLOW_LISTS.values() for name in names}
)


class _EmptyResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _EmptySession:
    """A session that answers every SELECT with no rows."""

    def __init__(self):
        self.info: dict = {}

    async def execute(self, _statement):
        return _EmptyResult()


async def _xtm_surface(monkeypatch) -> list[ToolDefinition]:
    """The ``/mcp/xtm`` surface with every optional builder taking part."""
    kb = (str(uuid.uuid4()), "Threat reports")

    async def _knowledge_bases(_db, _user, _group_ids):
        return [kb], [kb]

    async def _skill_ids(_db, _user, _group_ids):
        return [str(uuid.uuid4())]

    async def _access(_db, _user_id):
        return builtin_tools.SkillAccessContext()

    monkeypatch.setattr(mcp_xtm, "_resolve_knowledge_bases", _knowledge_bases)
    monkeypatch.setattr(mcp_xtm, "_resolve_skill_ids", _skill_ids)
    monkeypatch.setattr(builtin_tools, "build_skill_access_context", _access)
    user = SimpleNamespace(id=uuid.uuid4(), is_active=True, is_admin=True, role="admin")
    definitions, _executor = await mcp_xtm._build_xtm_tools(_EmptySession(), user)
    return definitions


# ── Every exposed tool is read from its name ─────────────────────────────────


def _read_only_but_destructive(names) -> list[str]:
    """Names marked read-only that carry a destructive token behind a verb that
    is not a read (the names this module reads as reads are kept as listed)."""
    return sorted(
        name
        for name in names
        if tool_annotations(name)["readOnlyHint"]
        and name.lower() not in _READ_ONLY_NAMES
        and classify_tool_name(name)[1] not in {"read_verb", "override"}
        and any(token in _DESTRUCTIVE_TOKENS for token in name.lower().split("_"))
    )


@pytest.mark.parametrize("name", _PLATFORM_TOOL_NAMES)
def test_every_platform_tool_is_annotated_from_its_name(name):
    assert annotations_read_from_name(name), (
        f"{name} is exposed over MCP but its name says neither read nor what "
        "it writes: teach app/services/mcp_tool_metadata.py its verb"
    )
    assert _TOOL_NAME.match(name)


@pytest.mark.asyncio
async def test_every_xtm_tool_is_annotated_from_its_name(monkeypatch):
    definitions = await _xtm_surface(monkeypatch)
    names = {d.name for d in definitions}
    # The optional builders took part, so the scan covers the whole surface.
    assert {"search_knowledge", "update_knowledge_document", "load_skill"} <= names

    unread = sorted(n for n in names if not annotations_read_from_name(n))
    assert not unread, (
        f"{unread} are exposed on /mcp/xtm but their names say neither read "
        "nor what they write: teach app/services/mcp_tool_metadata.py the verb"
    )
    assert all(_TOOL_NAME.match(n) for n in names)
    assert not _read_only_but_destructive(names)


@pytest.mark.asyncio
async def test_every_flow_tool_is_annotated_from_its_name(monkeypatch):
    # The flag is off in the unit-test job, which hides the flow tools from the
    # test above: scan them with it on.
    monkeypatch.setattr(mcp_xtm, "is_feature_enabled", lambda flag: flag == "FLOW_BUILDER")
    names = {d.name for d in await _xtm_surface(monkeypatch)}
    assert {"create_flow", "run_flow", "activate_flow", "deactivate_flow"} <= names
    unread = sorted(n for n in names if not annotations_read_from_name(n))
    assert not unread
    assert not _read_only_but_destructive(names)


def test_no_platform_tool_is_read_only_and_destructive():
    assert not _read_only_but_destructive(_PLATFORM_TOOL_NAMES)


@pytest.mark.asyncio
async def test_the_xtm_surface_is_listed_with_titles_and_annotations(monkeypatch):
    definitions = await _xtm_surface(monkeypatch)

    async def _built(_db, _user):
        return definitions, None

    monkeypatch.setattr(mcp_xtm, "_build_xtm_tools", _built)
    body = mcp_endpoint.MCPRequest(id=1, method="tools/list")
    result = await mcp_xtm._dispatch_xtm_mcp(body, None, SimpleNamespace(id=uuid.uuid4()))

    by_name = {tool["name"]: tool for tool in result["result"]["tools"]}
    assert by_name["list_agents"]["annotations"]["readOnlyHint"] is True
    assert by_name["delete_agent"]["annotations"]["destructiveHint"] is True
    assert by_name["create_agent"]["annotations"]["destructiveHint"] is False
    assert by_name["inspect_agent_traces"]["annotations"]["readOnlyHint"] is True
    assert by_name["load_skill"]["annotations"]["readOnlyHint"] is True
    assert by_name["trigger_assignment"]["annotations"]["destructiveHint"] is True
    assert by_name["run_agent_once"]["annotations"]["destructiveHint"] is True
    for name, tool in by_name.items():
        assert tool["title"] == tool["annotations"]["title"]
        # XTM One's own data is a closed domain; starting an agent run is not.
        assert tool["annotations"]["openWorldHint"] is (
            name in {"trigger_assignment", "run_agent_once"}
        )


# ── Reads, additive writes, destructive writes ───────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "get_opencti_entity",
        "list_openaev_tags",
        "search_opencti",
        "full_text_search_openaev",
        "query_openaev_analytics",
        "validate_nlq_filter",
        "inspect_agent_traces",
        "load_skill",
        "read_skill_resource",
    ],
)
def test_a_read_is_read_only(name):
    annotations = tool_annotations(name)
    assert annotations["readOnlyHint"] is True
    assert annotations["destructiveHint"] is False
    assert "idempotentHint" not in annotations


@pytest.mark.parametrize(
    "name",
    [
        "create_opencti_entity",
        "add_opencti_label",
        "add_opencti_to_container",
        "promote_opencti_observable",
        "run_opencrq_crq_simulation",
        "add_kb_document",
    ],
)
def test_an_additive_write_is_not_destructive(name):
    annotations = tool_annotations(name)
    assert annotations["readOnlyHint"] is False
    assert annotations["destructiveHint"] is False
    assert annotations["idempotentHint"] is False


@pytest.mark.parametrize(
    "name, idempotent",
    [
        ("delete_opencti_entity", True),
        ("remove_opencti_label", True),
        ("delete_opencrq_risk_scenarios", True),
        ("delete_opencrq_asset_evidence", True),
        ("reset_opencrq_evidence_state", False),
        ("reset_opencrq_supporting_asset_to_connectors", False),
        ("update_opencti_field", False),
        ("manage_openaev_scenario", False),
        ("manage_opencrq_compliance_assessment", False),
        ("manage_opencrq_scope_control_posture", False),
        ("manage_opencrq_capability_techniques", False),
        ("update_opencrq_requirement_answer", False),
        ("update_knowledge_document", False),
    ],
)
def test_a_write_that_changes_what_exists_is_destructive(name, idempotent):
    annotations = tool_annotations(name)
    assert annotations["readOnlyHint"] is False
    assert annotations["destructiveHint"] is True
    assert annotations["idempotentHint"] is idempotent


def test_a_tool_that_starts_an_agent_run_is_annotated_as_one():
    # ``trigger`` reads as additive, but the assignment runs a whole agent loop.
    annotations = tool_annotations("trigger_assignment")
    assert annotations["readOnlyHint"] is False
    assert annotations["destructiveHint"] is True
    assert annotations["openWorldHint"] is True


def test_a_name_with_no_known_verb_keeps_the_specification_defaults():
    # Not read-only, destructive: the default a client must assume.
    assert not annotations_read_from_name("frobnicate_widgets")
    annotations = tool_annotations("frobnicate_widgets")
    assert annotations["readOnlyHint"] is False
    assert annotations["destructiveHint"] is True
    assert annotations["openWorldHint"] is False


@pytest.mark.parametrize("name", ["intune_status_purge", "clear_summary_cache"])
def test_a_read_found_on_a_word_does_not_outweigh_a_destructive_token(name):
    """The classifier reads these as reads from a word of the name (``status``,
    ``summary``), not from their verb, and its write tokens do not hold
    ``purge`` / ``clear``: they are destructive here."""
    assert classify_tool_name(name) == ("read", "read_token")
    annotations = tool_annotations(name)
    assert annotations["readOnlyHint"] is False
    assert annotations["destructiveHint"] is True


@pytest.mark.parametrize("name", ["reset_search_index", "reset_opencrq_job_status"])
def test_a_reset_is_a_write_to_the_classifier_too(name):
    """``reset`` is a write token: the classifier no longer reads these
    as reads from ``search`` / ``status``, so the catalog badge, the HITL
    whitelists and the dedup cache agree with the destructive annotation."""
    assert classify_tool_name(name) == ("write", "write_token")
    annotations = tool_annotations(name)
    assert annotations["readOnlyHint"] is False
    assert annotations["destructiveHint"] is True


@pytest.mark.parametrize(
    "name, title",
    [
        ("get_opencti_entity", "Get OpenCTI entity"),
        ("list_opencrq_cves", "List OpenCRQ CVEs"),
        ("get_opencrq_tef_input", "Get OpenCRQ TEF input"),
        ("validate_nlq_filter", "Validate NLQ filter"),
        ("inspect_agent_traces", "Inspect agent traces"),
    ],
)
def test_a_title_is_read_from_the_name(name, title):
    assert tool_title(name) == title


def test_a_definition_is_serialized_with_its_title_and_annotations():
    defn = ToolDefinition(
        name="delete_opencti_entity",
        description="Delete an entity.",
        input_schema={"type": "object", "properties": {"id": {"type": "string"}}},
    )
    tool = tool_to_mcp(defn)
    assert tool == {
        "name": "delete_opencti_entity",
        "title": "Delete OpenCTI entity",
        "description": "Delete an entity.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
        "annotations": {
            "title": "Delete OpenCTI entity",
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }
    empty = tool_to_mcp(ToolDefinition(name="list_x", description="", input_schema={}))
    assert empty["inputSchema"] == {"type": "object", "properties": {}}


def test_a_listed_description_names_only_the_tools_listed_with_it():
    """``tools/list`` resolves a surface-dependent description against the
    tools the server serves, as the agent loop does."""

    def _describe(surface):
        named = "read_x" if any(t.name == "read_x" for t in surface) else "nothing else"
        return f"Lists x; then {named}."

    lister = ToolDefinition(
        name="list_x", description="Lists x.", describe_for_surface=_describe
    )
    reader = ToolDefinition(name="read_x", description="Reads x.")

    alone = tools_to_mcp([lister])
    assert [t["description"] for t in alone] == ["Lists x; then nothing else."]
    together = tools_to_mcp([lister, reader])
    assert [t["description"] for t in together] == ["Lists x; then read_x.", "Reads x."]
    assert tools_to_mcp([]) == []


# ── Agents exposed as tools ──────────────────────────────────────────────────


def _agent(slug: str, name: str = "Threat Analyst") -> SimpleNamespace:
    return SimpleNamespace(slug=slug, name=name, persona="Answers CTI questions.")


def test_an_agent_tool_is_annotated_as_an_agent_run():
    tool = mcp_endpoint._agent_to_tool(_agent("threat-analyst"))
    assert tool["name"] == "threat-analyst"
    assert tool["title"] == "Threat Analyst"
    assert tool["annotations"] == agent_tool_annotations("Threat Analyst")
    assert tool["annotations"]["readOnlyHint"] is False
    assert tool["annotations"]["destructiveHint"] is True
    assert tool["annotations"]["openWorldHint"] is True


def test_a_slug_that_fits_is_the_tool_name():
    slug = "a" * MAX_TOOL_NAME_LENGTH
    assert agent_tool_name(slug) == slug


def test_a_long_slug_becomes_a_stable_name_that_fits():
    head = "threat-intelligence-analyst-for-the-europe-middle-east-and-africa"
    first = agent_tool_name(f"{head}-north")
    second = agent_tool_name(f"{head}-south")

    assert len(first) <= MAX_TOOL_NAME_LENGTH
    assert _TOOL_NAME.match(first)
    assert first == agent_tool_name(f"{head}-north")
    # Two slugs sharing their head stay two tools.
    assert first != second
    assert first.startswith("threat-intelligence-analyst")


def test_no_slug_can_be_another_agents_shortened_name():
    """A user cannot name an agent so that it captures another's calls: a
    slug is lowercase, a shortened name carries an uppercase marker."""
    from app.core.utils import slugify, validate_slug

    long_slug = "company-incident-response-coordinator-for-the-security-operations-center-emea"
    shortened = agent_tool_name(long_slug)
    assert shortened != shortened.lower()
    # Whatever a user types, the slug stored is lowercase.
    assert validate_slug(shortened) != shortened
    assert slugify(shortened) != shortened


@pytest.mark.asyncio
@pytest.mark.parametrize("by", ["listed name", "full slug"])
async def test_a_long_slug_agent_is_called_by_its_listed_name_or_its_slug(monkeypatch, by):
    slug = "threat-intelligence-analyst-" + "x" * 80
    agent = _agent(slug)
    called = []

    async def _run(_db, _user_id, target, message, context=None):
        called.append((target.slug, message))
        return "done"

    monkeypatch.setattr(mcp_endpoint, "_execute_agent_tool", _run)
    listed = mcp_endpoint._handle_tools_list(1, [agent])["result"]["tools"][0]["name"]
    assert len(listed) <= MAX_TOOL_NAME_LENGTH

    name = listed if by == "listed name" else slug
    result = await mcp_endpoint._handle_tools_call(
        2, {"name": name, "arguments": {"message": "hi"}}, [agent], None, "u1"
    )
    assert result["result"]["content"][0]["text"] == "done"
    assert called == [(slug, "hi")]


# ── One serializer ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("module", ["mcp_platform.py", "mcp_xtm.py"])
def test_the_mcp_server_modules_share_the_one_serializer(module):
    source = app_sources.source(app_sources.APP_DIR / "api" / "v1" / module)
    assert '"inputSchema"' not in source, (
        f"{module} builds a tools/list entry by hand: serialize it with "
        "app.services.mcp_tool_metadata.tools_to_mcp"
    )
    assert "tools_to_mcp(" in source
