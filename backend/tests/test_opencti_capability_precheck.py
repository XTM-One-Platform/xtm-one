"""Unattended OpenCTI runs do not send calls the acting user cannot pass.

A per-user fan-out (the weekly CTEM stage assessments) ran the same gated
queries as every user; each refusal became an "unauthorized read" event in the
customer's OpenCTI audit log attributed to a real employee who did nothing.
These tests stub the one HTTP chokepoint (``_common.safe_request``) and record
which GraphQL operations were actually sent. The requirements they pin come
from the OpenCTI schema ``@auth`` directives and the resolver checks that raise
``ForbiddenAccess`` (see the table in ``capabilities.py``).
"""

import json
from typing import Any

import pytest

from app.integrations.providers.opencti import _common as opencti_common
from app.integrations.providers.opencti import build_tools
from app.integrations.providers.opencti.capabilities import (
    TOOL_CAPABILITIES,
    has_any_capability,
    missing_capabilities,
    required_capabilities,
)
from app.services.integration_tools import _suffix_instance_tools
from app.tools.base import (
    CallableToolExecutor,
    ToolCall,
    is_auth_or_permission_error,
    is_error_shaped_result,
)

TOKEN = "https://octi.test||token"
CURRENT = "7.260917.0"


class _Resp:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


_PROBES = {"MeCapabilities", "MeDraftCapabilities"}


def _stub(
    monkeypatch,
    capabilities: list[str] | None,
    *,
    version: str | None = CURRENT,
    draft_capabilities: list[str] | None = None,
    probe_error: str | None = None,
    draft_error: str | None = None,
    probe_payload: dict | None = None,
    sent: list[tuple[str, str]] | None = None,
) -> list[str]:
    """Answer the capability probes, answer anything else with empty data."""
    operations: list[str] = []

    async def _fake_safe_request(client, method, url, provider, action, **kwargs):
        operation = action.removeprefix("execute GraphQL operation ").strip("'")
        operations.append(operation)
        if sent is not None:
            sent.append((operation, kwargs["headers"]["Authorization"]))
        if operation == "MeCapabilities":
            if probe_error:
                return None, probe_error
            if probe_payload is not None:
                return _Resp(probe_payload), None
            caps = [{"name": c} for c in capabilities or []]
            return _Resp({"data": {"me": {"capabilities": caps}, "about": {"version": version}}}), None
        if operation == "MeDraftCapabilities":
            if draft_error:
                return None, draft_error
            caps = [{"name": c} for c in draft_capabilities or []]
            return _Resp({"data": {"me": {"capabilitiesInDraft": caps}}}), None
        if operation == "PlaybookGraph":
            # The graph edits check the ids they address against it first.
            graph = {"nodes": [{"id": n} for n in ("a", "b", "n")], "links": []}
            return _Resp({"data": {"playbook": {"id": "pb", "playbook_definition": json.dumps(graph)}}}), None
        return _Resp({"data": {}}), None

    monkeypatch.setattr(opencti_common, "safe_request", _fake_safe_request)
    return operations


def _executor(autonomous: bool = True, **kwargs: Any) -> CallableToolExecutor:
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor, autonomous=autonomous, **kwargs)
    return executor


async def _call(executor, tool: str, /, **arguments: Any):
    return await executor.execute_tool(ToolCall(id=f"c-{tool}", name=tool, arguments=arguments))


async def _run(executor, tool: str, /, **arguments: Any) -> str:
    return (await _call(executor, tool, **arguments)).content


# ---------------------------------------------------------------------------
# The mapping, both directions: a role that passes is queried as before, a role
# that cannot pass sends nothing.
# ---------------------------------------------------------------------------

_PIR_CRITERIA = (
    '[{"weight": 1, "filters": {"mode": "and", "filterGroups": [], "filters": ['
    '{"key": ["entity_type"], "values": ["targets"]}, '
    '{"key": ["toId"], "values": ["2b1c7a8e-6f0d-4f4e-9d3b-3c9e4b1a5d6f"]}]}}]'
)

# (tool, arguments, operation sent, a role that passes, a role that cannot)
_MAPPING = [
    # playbooks / playbook / playbookComponents / playbookExecute: @auth(for: [AUTOMATION])
    ("manage_opencti_playbook", {"action": "list"}, "Playbooks", ["AUTOMATION"], ["KNOWLEDGE"]),
    ("manage_opencti_playbook", {"action": "get", "playbook_id": "pb"}, "Playbook", ["AUTOMATION"], ["KNOWLEDGE"]),
    ("manage_opencti_playbook", {"action": "components"}, "PlaybookComponents", ["AUTOMATION"], ["KNOWLEDGE"]),
    (
        "manage_opencti_playbook",
        {"action": "execute", "playbook_id": "pb", "entity_id": "e"},
        "PlaybookExecute",
        ["AUTOMATION"],
        ["KNOWLEDGE"],
    ),
    # playbookAdd / playbookDelete / playbookFieldPatch / playbookDuplicate: AUTOMATION_AUTMANAGE
    ("manage_opencti_playbook", {"action": "create", "name": "pb"}, "PlaybookAdd", ["AUTOMATION_AUTMANAGE"], ["AUTOMATION"]),
    (
        "manage_opencti_playbook",
        {"action": "delete", "playbook_id": "pb"},
        "PlaybookDelete",
        ["AUTOMATION_AUTMANAGE"],
        ["AUTOMATION"],
    ),
    (
        "manage_opencti_playbook",
        {"action": "toggle", "playbook_id": "pb", "running": True},
        "PlaybookToggle",
        ["AUTOMATION_AUTMANAGE"],
        ["AUTOMATION"],
    ),
    (
        "manage_opencti_playbook",
        {"action": "duplicate", "playbook_id": "pb"},
        "PlaybookDuplicate",
        ["AUTOMATION_AUTMANAGE"],
        ["AUTOMATION"],
    ),
    # playbook graph mutations: AUTOMATION_AUTMANAGE
    (
        "edit_opencti_playbook_graph",
        {"action": "add_link", "playbook_id": "pb", "from_node": "a", "from_port": "out", "to_node": "b"},
        "AddLink",
        ["AUTOMATION_AUTMANAGE"],
        ["AUTOMATION"],
    ),
    (
        "edit_opencti_playbook_graph",
        {"action": "delete_node", "playbook_id": "pb", "node_id": "n"},
        "DeleteNode",
        ["AUTOMATION_AUTMANAGE"],
        ["AUTOMATION"],
    ),
    (
        "edit_opencti_playbook_graph",
        {"action": "update_positions", "playbook_id": "pb", "positions": '{"n": {"x": 1, "y": 2}}'},
        "UpdatePositions",
        ["AUTOMATION_AUTMANAGE"],
        ["AUTOMATION"],
    ),
    # pirs / pir: @auth(for: [PIRAPI]); PIRAPI_PIRUPDATE holds PIRAPI (substring match)
    ("manage_opencti_pir", {"action": "list"}, "Pirs", ["PIRAPI"], ["KNOWLEDGE"]),
    ("manage_opencti_pir", {"action": "get", "pir_id": "p"}, "Pir", ["PIRAPI_PIRUPDATE"], ["KNOWLEDGE"]),
    # pirFieldPatch / pirDelete: PIRAPI_PIRUPDATE
    ("manage_opencti_pir", {"action": "update", "pir_id": "p", "name": "n"}, "PirPatch", ["PIRAPI_PIRUPDATE"], ["PIRAPI"]),
    ("manage_opencti_pir", {"action": "delete", "pir_id": "p"}, "PirDelete", ["PIRAPI_PIRUPDATE"], ["PIRAPI"]),
    # pirFlagElement / pirUnflagElement: @auth(for: [BYPASS])
    (
        "manage_opencti_pir",
        {"action": "flag", "pir_id": "p", "relationship_id": "r", "source_id": "s", "matching_criteria": _PIR_CRITERIA},
        "PirFlag",
        ["BYPASS"],
        ["PIRAPI", "PIRAPI_PIRUPDATE"],
    ),
    (
        "manage_opencti_pir",
        {"action": "unflag", "pir_id": "p", "relationship_id": "r", "source_id": "s"},
        "PirUnflag",
        ["BYPASS"],
        ["PIRAPI", "PIRAPI_PIRUPDATE", "KNOWLEDGE"],
    ),
    # stixDomainObjects: KNOWLEDGE, AND its pirInformation: PIRAPI
    ("query_opencti_pir_entities", {"pir_id": "p"}, "PirEntities", ["KNOWLEDGE", "PIRAPI"], ["PIRAPI"]),
    ("query_opencti_pir_entities", {"pir_id": "p"}, "PirEntities", ["KNOWLEDGE_KNUPDATE", "PIRAPI_PIRUPDATE"], ["KNOWLEDGE"]),
    # pirRelationships: the resolver answers no user without PIRAPI
    ("query_opencti_pir_relationships", {"pir_id": "p"}, "PirRels", ["PIRAPI"], ["KNOWLEDGE"]),
    # audits: one of the three settings capabilities, plus the stream's own check
    (
        "query_opencti_activity",
        {"stream": "History", "mode": "list"},
        "Audits",
        ["KNOWLEDGE", "SETTINGS_SETACCESSES"],
        ["SETTINGS_SETACCESSES"],
    ),
    (
        "query_opencti_activity",
        {"stream": "History"},
        "Audits",
        ["KNOWLEDGE", "VIRTUAL_ORGANIZATION_ADMIN"],
        ["KNOWLEDGE"],
    ),
    (
        "query_opencti_activity",
        {"stream": "Activity", "mode": "list"},
        "Audits",
        ["SETTINGS_SECURITYACTIVITY"],
        ["SETTINGS_SETACCESSES", "KNOWLEDGE"],
    ),
    (
        "query_opencti_activity",
        {"stream": "security"},
        "Audits",
        ["SETTINGS_SECURITYACTIVITY"],
        ["VIRTUAL_ORGANIZATION_ADMIN", "KNOWLEDGE"],
    ),
    (
        "query_opencti_activity",
        {"stream": "Activity", "mode": "count"},
        "AuditsNumber",
        ["SETTINGS_SECURITYACTIVITY"],
        ["SETTINGS_SETACCESSES", "KNOWLEDGE"],
    ),
]


_MAPPING_IDS = [f"{t}-{op}-{'+'.join(ok)}-not-{'+'.join(ko)}" for t, _, op, ok, ko in _MAPPING]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "arguments", "operation", "passes", "_cannot"), _MAPPING, ids=_MAPPING_IDS)
async def test_a_role_that_passes_is_queried_as_before(monkeypatch, tool, arguments, operation, passes, _cannot):
    operations = _stub(monkeypatch, passes)
    content = await _run(_executor(), tool, **arguments)
    assert not content.startswith("Skipped:"), content
    # A graph edit reads the graph before and after it (PlaybookGraph).
    assert [o for o in operations if o != "PlaybookGraph"] == ["MeCapabilities", operation]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "arguments", "operation", "_passes", "cannot"), _MAPPING, ids=_MAPPING_IDS)
async def test_a_role_that_cannot_pass_sends_nothing(monkeypatch, tool, arguments, operation, _passes, cannot):
    operations = _stub(monkeypatch, cannot)
    content = await _run(_executor(), tool, **arguments)
    assert content.startswith("Skipped:"), content
    assert f"`{tool}`" in content
    assert operation not in operations, "the gated call must never be sent"
    assert set(operations) <= _PROBES


@pytest.mark.asyncio
async def test_bypass_passes_every_gated_call(monkeypatch):
    for tool, arguments, operation, _, _ in _MAPPING:
        operations = _stub(monkeypatch, ["BYPASS"])
        await _run(_executor(), tool, **arguments)
        assert operation in operations, tool


@pytest.mark.asyncio
async def test_the_skip_names_only_what_is_missing(monkeypatch):
    _stub(monkeypatch, ["KNOWLEDGE"])
    executor = _executor()
    entities = await _run(executor, "query_opencti_pir_entities", pir_id="p")
    assert "does not hold PIRAPI," in entities
    activity = await _run(executor, "query_opencti_activity", stream="History")
    assert "one of SETTINGS_SETACCESSES, SETTINGS_SECURITYACTIVITY, VIRTUAL_ORGANIZATION_ADMIN," in activity


# ---------------------------------------------------------------------------
# Audit counts: auditsNumber only checks the stream from OpenCTI 7.260828.0.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", ["History", "Activity"])
@pytest.mark.parametrize(
    ("version", "sent"),
    [
        ("6.9.29", True),
        ("7.260821.0", True),
        (None, True),  # unreadable release: the lenient rule
        ("master", True),
        ("7.260828.0", False),
        (CURRENT, False),
    ],
)
async def test_audit_counts_follow_the_platform_release(monkeypatch, stream, version, sent):
    # Passes the root directive; holds neither stream's own capability.
    operations = _stub(monkeypatch, ["SETTINGS_SETACCESSES"], version=version)
    content = await _run(_executor(), "query_opencti_activity", stream=stream, mode="count")
    assert ("AuditsNumber" in operations) is sent
    assert content.startswith("Skipped:") is (not sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["6.9.29", CURRENT])
async def test_the_audit_root_directive_holds_in_every_release(monkeypatch, version):
    operations = _stub(monkeypatch, ["KNOWLEDGE"], version=version)
    content = await _run(_executor(), "query_opencti_activity", stream="History", mode="count")
    assert content.startswith("Skipped:")
    assert operations == ["MeCapabilities"]


def test_an_unknown_stream_only_needs_the_root_directive():
    """OpenCTI's per-stream check only knows History and Activity."""
    assert required_capabilities("query_opencti_activity", {"stream": "Other"}, (7, 260917, 0)) == (
        TOOL_CAPABILITIES["query_opencti_activity"]
    )
    # Missing stream: the tool reads History.
    assert required_capabilities("query_opencti_activity", {}, (7, 260917, 0))[-1] == ("KNOWLEDGE",)


# ---------------------------------------------------------------------------
# Drafts: from 6.9.0 OpenCTI also accepts what the user's role grants in
# drafts (the KNOWLEDGE family), in the request's draft or the one open in the
# user's UI.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [["KNOWLEDGE"], ["KNOWLEDGE_KNUPDATE"]])
async def test_a_role_whose_drafts_grant_what_is_missing_is_not_skipped(monkeypatch, draft):
    operations = _stub(monkeypatch, ["PIRAPI"], draft_capabilities=draft)
    await _run(_executor(), "query_opencti_pir_entities", pir_id="p")
    assert operations == ["MeCapabilities", "MeDraftCapabilities", "PirEntities"]


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [[], ["SETTINGS_SETLABELS"]])
async def test_a_role_whose_drafts_do_not_grant_it_is_skipped(monkeypatch, draft):
    operations = _stub(monkeypatch, ["PIRAPI"], draft_capabilities=draft)
    content = await _run(_executor(), "query_opencti_pir_entities", pir_id="p")
    assert content.startswith("Skipped:") and "does not hold KNOWLEDGE," in content
    assert operations == ["MeCapabilities", "MeDraftCapabilities"]


@pytest.mark.asyncio
async def test_before_6_9_only_base_capabilities_count(monkeypatch):
    operations = _stub(monkeypatch, ["PIRAPI"], version="6.8.12", draft_capabilities=["KNOWLEDGE"])
    content = await _run(_executor(), "query_opencti_pir_entities", pir_id="p")
    assert content.startswith("Skipped:")
    assert operations == ["MeCapabilities"]


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [CURRENT, None])
async def test_unreadable_draft_grants_skip_nothing(monkeypatch, version):
    operations = _stub(monkeypatch, ["SETTINGS_SECURITYACTIVITY"], version=version, draft_error="Error: validation")
    await _run(_executor(), "query_opencti_activity", stream="History")
    assert operations == ["MeCapabilities", "MeDraftCapabilities", "Audits"]


@pytest.mark.asyncio
async def test_drafts_are_read_only_for_a_requirement_they_can_grant(monkeypatch):
    operations = _stub(monkeypatch, ["KNOWLEDGE"], draft_capabilities=["KNOWLEDGE"])
    executor = _executor()
    await _run(executor, "query_opencti_pir_entities", pir_id="p")  # missing PIRAPI
    await _run(executor, "query_opencti_activity", stream="Activity")  # missing settings
    assert operations == ["MeCapabilities"]


@pytest.mark.asyncio
async def test_the_draft_probe_runs_once_per_build(monkeypatch):
    operations = _stub(monkeypatch, ["PIRAPI", "SETTINGS_SETACCESSES"], draft_capabilities=[])
    executor = _executor()
    await _run(executor, "query_opencti_pir_entities", pir_id="p")
    await _run(executor, "query_opencti_activity", stream="History")
    assert operations == ["MeCapabilities", "MeDraftCapabilities"]


# ---------------------------------------------------------------------------
# The skip result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_skip_is_a_no_op_not_a_failure(monkeypatch):
    """A skip is not a broken credential: it must not trip the error brake,
    the auth cooldown or the assignment auth quarantine, which all read
    ``is_error`` / ``is_auth_or_permission_error``."""
    _stub(monkeypatch, ["KNOWLEDGE"])
    executor = _executor()
    for tool, arguments, _, _, _ in _MAPPING:
        result = await _call(executor, tool, **arguments)
        assert result.content.startswith("Skipped:")
        assert result.tool_call_id == f"c-{tool}"
        assert result.is_error is False
        assert result.no_op is True
        assert not is_error_shaped_result(result.content)
        assert not is_auth_or_permission_error(result.content)


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_capability_probe_runs_once_per_build(monkeypatch):
    operations = _stub(monkeypatch, ["KNOWLEDGE"])
    executor = _executor()
    for tool, arguments, _, _, _ in _MAPPING:
        await _run(executor, tool, **arguments)
    assert operations == ["MeCapabilities"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stub",
    [
        {"probe_error": "Error: OpenCTI server error (500)"},
        {"probe_payload": {"data": {"me": None}}},
        {"probe_payload": {"data": {"me": {"capabilities": "BYPASS"}}}},
        {"probe_payload": {"data": {"me": {"capabilities": [{"name": "PIRAPI"}, "KNOWLEDGE"]}}}},
        {"probe_payload": {"errors": [{"message": "Cannot query field"}]}},
    ],
)
async def test_a_probe_that_cannot_be_read_fails_open(monkeypatch, stub):
    operations = _stub(monkeypatch, None, **stub)
    content = await _run(_executor(), "query_opencti_pir_entities", pir_id="pir-1")
    assert not content.startswith("Skipped:")
    assert "PirEntities" in operations


@pytest.mark.asyncio
async def test_a_probe_without_the_version_reads_counts_leniently(monkeypatch):
    operations = _stub(
        monkeypatch,
        None,
        probe_payload={"data": {"me": {"capabilities": [{"name": "SETTINGS_SETACCESSES"}]}}},
    )
    await _run(_executor(), "query_opencti_activity", stream="Activity", mode="count")
    assert "AuditsNumber" in operations


# ---------------------------------------------------------------------------
# Callers: only unattended runs are gated.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "build_kwargs",
    [
        pytest.param({"autonomous": False}, id="interactive-chat"),
        pytest.param({"autonomous": False, "direct_mutations": True}, id="platform-mcp-server"),
    ],
)
async def test_interactive_callers_are_unchanged(monkeypatch, build_kwargs):
    """In chat and on /mcp/opencti the user asked for the call: no probe, it is sent."""
    operations = _stub(monkeypatch, ["KNOWLEDGE"])
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor, **build_kwargs)
    await _run(executor, "query_opencti_pir_entities", pir_id="pir-1")
    await _run(executor, "manage_opencti_pir", action="list")
    assert "MeCapabilities" not in operations
    assert operations == ["PirEntities", "Pirs"]


@pytest.mark.asyncio
async def test_ungated_tools_do_not_probe(monkeypatch):
    operations = _stub(monkeypatch, ["KNOWLEDGE"])
    await _run(_executor(), "search_opencti", query="apt28")
    assert "MeCapabilities" not in operations


@pytest.mark.asyncio
async def test_each_instance_probes_with_its_own_credentials(monkeypatch):
    """Two OpenCTI integrations on one agent: each keeps its own gate and probe."""
    sent: list[tuple[str, str]] = []
    _stub(monkeypatch, ["KNOWLEDGE"], sent=sent)
    executor = CallableToolExecutor()
    used: dict[str, set[str]] = {}
    names = {}
    for label, token in (("A", "https://a.test||token-a"), ("B", "https://b.test||token-b")):
        defs = build_tools(token, executor, autonomous=True)
        enriched = _suffix_instance_tools(
            [d for d in defs if d.name in TOOL_CAPABILITIES],
            executor,
            provider="opencti",
            integ_name=f"OpenCTI {label}",
            integ_desc=None,
            integration_id=f"integ-{label}",
            used_slugs=used,
        )
        names[label] = next(d.name for d in enriched if d.name.startswith("manage_opencti_pir"))
    assert names["A"] != names["B"]
    assert (await _run(executor, names["B"], action="list")).startswith("Skipped:")
    assert (await _run(executor, names["A"], action="list")).startswith("Skipped:")
    assert sent == [("MeCapabilities", "Bearer token-b"), ("MeCapabilities", "Bearer token-a")]


# Gated tools that take every argument (**unknown) and refuse the ones their action
# does not apply by name, instead of dropping them silently (the unapplied-arguments
# contract, tests/test_opencti_pir_unapplied_args.py).
_REFUSES_UNKNOWN_ARGUMENTS = frozenset(
    {"manage_opencti_pir", "manage_opencti_playbook", "manage_opencti_security_coverage"}
)


def test_gate_keeps_the_handler_signature_for_argument_filtering():
    """Hallucinated arguments are still dropped (or refused) for a wrapped handler."""
    executor = _executor()
    for tool in TOOL_CAPABILITIES:
        if tool in _REFUSES_UNKNOWN_ARGUMENTS:
            assert executor._accepted_params[tool] is None, tool
            continue
        assert executor._accepted_params[tool] is not None, tool
        assert "hallucinated" not in executor._accepted_params[tool]


async def test_gate_keeps_the_refusal_of_unknown_arguments() -> None:
    """A gated tool that refuses unknown arguments still refuses them once wrapped."""
    executor = _executor()
    for tool in _REFUSES_UNKNOWN_ARGUMENTS:
        content = await _run(executor, tool, action="list", hallucinated="x")
        assert "hallucinated" in content, (tool, content)


# ---------------------------------------------------------------------------
# Matching semantics (authDirective.ts::checkCapabilities)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("held", "required", "expected"),
    [
        (["BYPASS"], ["AUTOMATION"], True),
        (["AUTOMATION"], ["AUTOMATION"], True),
        (["KNOWLEDGE_KNUPDATE_KNDELETE"], ["KNOWLEDGE_KNUPDATE"], True),
        (["KNOWLEDGE"], ["KNOWLEDGE_KNUPDATE"], False),
        (["KNOWLEDGE"], ["AUTOMATION"], False),
        ([], ["PIRAPI"], False),
        (["SETTINGS_SECURITYACTIVITY"], ["SETTINGS_SETACCESSES", "SETTINGS_SECURITYACTIVITY"], True),
        (["KNOWLEDGE"], ["BYPASS"], False),
    ],
)
def test_has_any_capability_mirrors_opencti(held, required, expected):
    assert has_any_capability(held, required) is expected


@pytest.mark.parametrize(
    ("held", "requirement", "missing"),
    [
        (["KNOWLEDGE", "PIRAPI"], (("KNOWLEDGE",), ("PIRAPI",)), ()),
        (["PIRAPI"], (("KNOWLEDGE",), ("PIRAPI",)), (("KNOWLEDGE",),)),
        (["KNOWLEDGE_KNUPDATE"], (("KNOWLEDGE",), ("PIRAPI",)), (("PIRAPI",),)),
        (["BYPASS"], (("KNOWLEDGE",), ("PIRAPI",)), ()),
        ([], (("SETTINGS_SETACCESSES", "SETTINGS_SECURITYACTIVITY"),), (("SETTINGS_SETACCESSES", "SETTINGS_SECURITYACTIVITY"),)),
    ],
)
def test_missing_capabilities(held, requirement, missing):
    assert missing_capabilities(held, requirement) == missing
