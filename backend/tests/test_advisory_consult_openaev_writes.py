"""Advisory-consult enforcement: a specialist consulted during an autonomous
run can never write to OpenAEV.

The OpenAEV Attack Path Orchestrator's persona promises that consulted
specialists are ADVISORY - they return recon/recommendations and the
orchestrator stays the single author of the attack path. That promise used to
be prompt-only: a consult runs the target agent's full toolset, so a
user-created recon agent whose own OpenAEV integration is bound with write
scope could call ``manage_openaev_*`` directly. A consult has no run/simulation
context, so any such write lands OUTSIDE the attack path and OUTSIDE the
discovery-mode/scope choke points - exactly the contradictory, out-of-band
entity the orchestrator model forbids.

These tests pin the code-level enforcement:

* ``strip_openaev_write_tools`` removes every OpenAEV mutation tool
  (``manage_openaev_*`` + the run-scoped authoring/writing tools) while keeping
  read-only OpenAEV tools and the specialist's own non-OpenAEV tools - with the
  single exception of ``manage_openaev_threat_arsenal`` (the global, reusable
  arsenal library has no run-scope implications, so a consulted Payload Creator
  keeps it and authors the payload directly). ``is_openaev_write_tool`` still
  classifies the arsenal tool as a write; the exception lives in the strip.
* Source-level pins: ``run_agent_chat`` strips writes when
  ``restrict_openaev_writes`` is set, the consult path propagates
  ``advisory_consult_specialists`` onto its target, and the autonomous adapter
  turns the flag on for the orchestrator cycle.
"""

from __future__ import annotations

import inspect

import app.services.agent_chat as agent_chat_mod
import app.services.autonomous_pentest as autonomous_pentest_mod
from app.services.builtin_tools import (
    is_advisory_forbidden_arsenal_action,
    is_openaev_arsenal_advisory_agent,
    is_openaev_write_tool,
    reset_advisory_arsenal_read_only,
    reset_advisory_openaev_writes,
    set_advisory_arsenal_read_only,
    set_advisory_openaev_writes,
    strip_openaev_write_tools,
)
from app.tools.base import ToolDefinition


def _make_tool(name: str, *, read_only: bool = False) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"fake tool {name}",
        input_schema={"type": "object", "properties": {}},
        source="test",
        read_only=read_only,
    )


# ---------------------------------------------------------------------------
# is_openaev_write_tool
# ---------------------------------------------------------------------------


def test_manage_openaev_tools_are_writes():
    for name in (
        "manage_openaev_asset",
        "manage_openaev_threat_arsenal",
        "manage_openaev_inject",
        "manage_openaev_team",
    ):
        assert is_openaev_write_tool(name), name


def test_run_scoped_authoring_tools_are_writes():
    for name in (
        "author_openaev_attack_path_step",
        "update_openaev_attack_path_step",
        # Deleting a step is a run-scoped mutation: it MUST be stripped from an
        # advisory specialist, or a consult could delete the orchestrator's steps.
        "delete_openaev_attack_path_step",
        "evaluate_openaev_attack_path",
        "promote_openaev_finding_to_asset",
        "ensure_openaev_target_team",
        "set_openaev_run_scope",
        "record_openaev_event",
        "update_openaev_run_status",
        "consume_openaev_directives",
    ):
        assert is_openaev_write_tool(name), name


def test_read_only_openaev_tools_are_not_writes():
    for name in (
        "search_openaev_findings",
        "search_openaev_assets",
        "read_openaev_attack_path_state",
        "get_openaev_run_scope",
    ):
        assert not is_openaev_write_tool(name), name


def test_a_manage_tool_its_provider_declares_read_only_is_not_a_write() -> None:
    """Read-only since OpenAEV #9233 (get and summary only): the definition says so, not its name."""
    from app.integrations.providers.openaev.findings import register
    from app.tools.base import CallableToolExecutor

    finding = next(
        td for td in register("https://openaev.test", {}, CallableToolExecutor()) if td.name == "manage_openaev_finding"
    )
    assert finding.read_only
    assert not is_openaev_write_tool(finding)
    assert strip_openaev_write_tools([finding]) == [finding]
    # The name alone stays a write: only the provider's declaration exempts it.
    assert is_openaev_write_tool("manage_openaev_finding")


def test_non_openaev_tools_are_not_writes():
    for name in ("web_fetch", "manage_github_issue", "search_knowledge"):
        assert not is_openaev_write_tool(name), name


# ---------------------------------------------------------------------------
# strip_openaev_write_tools
# ---------------------------------------------------------------------------


def test_strip_removes_only_openaev_writes():
    tools = [
        _make_tool("manage_openaev_finding", read_only=True),
        _make_tool("manage_openaev_asset"),
        _make_tool("author_openaev_attack_path_step"),
        _make_tool("promote_openaev_finding_to_asset"),
        _make_tool("search_openaev_findings"),
        _make_tool("read_openaev_attack_path_state"),
        _make_tool("get_openaev_run_scope"),
        _make_tool("web_fetch"),
        _make_tool("manage_github_issue"),
    ]

    kept = {t.name for t in strip_openaev_write_tools(tools)}

    # Reads + the specialist's own non-OpenAEV tools survive.
    assert kept == {
        "manage_openaev_finding",
        "search_openaev_findings",
        "read_openaev_attack_path_state",
        "get_openaev_run_scope",
        "web_fetch",
        "manage_github_issue",
    }


def test_strip_keeps_the_arsenal_but_strips_every_other_write():
    # The threat arsenal is a global reusable library (no run-scope), so a
    # consulted Payload Creator keeps it and authors the payload directly - this
    # is what makes "consult -> a payload actually gets created" hold. Every
    # OTHER global write and every run-scoped write is still stripped.
    tools = [
        _make_tool("manage_openaev_threat_arsenal"),
        _make_tool("manage_openaev_finding", read_only=True),
        _make_tool("manage_openaev_asset"),
        _make_tool("manage_openaev_inject"),
        _make_tool("author_openaev_attack_path_step"),
        _make_tool("set_openaev_run_scope"),
        _make_tool("promote_openaev_finding_to_asset"),
        _make_tool("search_openaev_injector_contracts"),
    ]

    kept = {t.name for t in strip_openaev_write_tools(tools)}

    assert kept == {
        "manage_openaev_threat_arsenal",
        "search_openaev_injector_contracts",
        "manage_openaev_finding",
    }


def test_strip_drops_fork_duplicate_and_deletes_from_the_advisory_arsenal_schema():
    # fork is the orchestrator's own atomic op and duplicate is how a "dumb PC"
    # makes half-baked copies, so both are stripped from the advisory schema
    # alongside delete / bulk_delete. The consulted Payload Creator keeps only
    # create / update (of ORIGINALS) plus the reads.
    original_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "list",
                    "get",
                    "create",
                    "update",
                    "fork",
                    "duplicate",
                    "delete",
                    "bulk_delete",
                ],
            }
        },
    }
    tools = [
        ToolDefinition(
            name="manage_openaev_threat_arsenal",
            description="fake tool manage_openaev_threat_arsenal",
            input_schema=original_schema,
            source="test",
        )
    ]

    kept = strip_openaev_write_tools(tools)
    assert len(kept) == 1
    enum = kept[0].input_schema["properties"]["action"]["enum"]
    assert "create" in enum
    assert "update" in enum
    assert "fork" not in enum
    assert "duplicate" not in enum
    assert "delete" not in enum
    assert "bulk_delete" not in enum
    # Original definition is not mutated (orchestrator keeps fork; chat keeps all).
    original_enum = tools[0].input_schema["properties"]["action"]["enum"]
    assert "fork" in original_enum
    assert "duplicate" in original_enum
    assert "delete" in original_enum


def test_advisory_guard_rejects_forbidden_actions_only_during_a_consult():
    assert not is_advisory_forbidden_arsenal_action("delete")
    token = set_advisory_openaev_writes(True)
    try:
        assert is_advisory_forbidden_arsenal_action("delete")
        assert is_advisory_forbidden_arsenal_action("bulk_delete")
        assert is_advisory_forbidden_arsenal_action("fork")
        assert is_advisory_forbidden_arsenal_action("duplicate")
        # Delegate mode (read-only flag NOT set): create/update stay allowed.
        assert not is_advisory_forbidden_arsenal_action("create")
        assert not is_advisory_forbidden_arsenal_action("update")
    finally:
        reset_advisory_openaev_writes(token)
    assert not is_advisory_forbidden_arsenal_action("delete")


# ---------------------------------------------------------------------------
# WS10 / D3 - a CONSULT is advise-only: allow_arsenal=False makes the arsenal
# read-only (create/update dropped too), and the execution-time guard refuses
# EVERY write under the read-only flag.
# ---------------------------------------------------------------------------


def test_strip_read_only_arsenal_drops_every_write_action():
    original_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "list",
                    "get",
                    "create",
                    "update",
                    "fork",
                    "duplicate",
                    "delete",
                    "bulk_delete",
                ],
            }
        },
    }
    tools = [
        ToolDefinition(
            name="manage_openaev_threat_arsenal",
            description="fake tool manage_openaev_threat_arsenal",
            input_schema=original_schema,
            source="test",
        )
    ]

    kept = strip_openaev_write_tools(tools, allow_arsenal=False)
    assert len(kept) == 1
    enum = kept[0].input_schema["properties"]["action"]["enum"]
    # Reads survive; EVERY write is gone (create/update included).
    assert "list" in enum
    assert "get" in enum
    for write in ("create", "update", "fork", "duplicate", "delete", "bulk_delete"):
        assert write not in enum, write
    # Original definition is not mutated.
    assert "create" in tools[0].input_schema["properties"]["action"]["enum"]


def test_advisory_guard_read_only_rejects_create_and_update_too():
    # Under the read-only consult flag, create/update are refused as well - not
    # just the destructive/fork actions.
    token = set_advisory_openaev_writes(True)
    ro_token = set_advisory_arsenal_read_only(True)
    try:
        assert is_advisory_forbidden_arsenal_action("create")
        assert is_advisory_forbidden_arsenal_action("update")
        assert is_advisory_forbidden_arsenal_action("fork")
        assert is_advisory_forbidden_arsenal_action("duplicate")
        assert is_advisory_forbidden_arsenal_action("delete")
        # Reads are still allowed.
        assert not is_advisory_forbidden_arsenal_action("list")
        assert not is_advisory_forbidden_arsenal_action("get")
    finally:
        reset_advisory_arsenal_read_only(ro_token)
        reset_advisory_openaev_writes(token)
    # Cleared after reset.
    assert not is_advisory_forbidden_arsenal_action("create")


def test_read_only_flag_is_inert_without_the_advisory_flag():
    # The read-only flag alone (no active advisory consult) must not forbid
    # anything - chat / orchestrator / tests are unaffected.
    ro_token = set_advisory_arsenal_read_only(True)
    try:
        assert not is_advisory_forbidden_arsenal_action("create")
        assert not is_advisory_forbidden_arsenal_action("delete")
    finally:
        reset_advisory_arsenal_read_only(ro_token)


def test_strip_is_a_noop_without_openaev_writes():
    tools = [_make_tool("web_fetch"), _make_tool("search_openaev_assets")]
    assert [t.name for t in strip_openaev_write_tools(tools)] == [
        "web_fetch",
        "search_openaev_assets",
    ]


def test_payload_creator_is_always_arsenal_advisory():
    # The built-in Payload Creator must run arsenal-advisory in EVERY context -
    # a consult sets restrict_openaev_writes, but the Scenario Creator delegates
    # it via a background task that does NOT, so the restriction has to key off
    # the agent identity to keep delete / bulk_delete off the global library.
    assert is_openaev_arsenal_advisory_agent(
        "openaev-payload-creator", is_builtin=True
    )
    # A non-built-in look-alike (e.g. a user clone) is NOT auto-restricted, and
    # unrelated built-ins are unaffected.
    assert not is_openaev_arsenal_advisory_agent(
        "openaev-payload-creator", is_builtin=False
    )
    assert not is_openaev_arsenal_advisory_agent(
        "openaev-scenario-creator", is_builtin=True
    )
    assert not is_openaev_arsenal_advisory_agent(None, is_builtin=True)


# ---------------------------------------------------------------------------
# Source-level pins — the enforcement is actually wired into the run paths
# ---------------------------------------------------------------------------


class TestAdvisoryConsultWiring:
    RUN_AGENT_CHAT_SOURCE = inspect.getsource(agent_chat_mod.run_agent_chat)
    ADAPTER_SOURCE = inspect.getsource(autonomous_pentest_mod)

    def test_run_agent_chat_strips_writes_when_restricted(self):
        # The finalized tool surface is passed through strip_openaev_write_tools
        # under the restrict flag, and the arsenal handler is marked advisory
        # for the duration of the agentic loop so delete / bulk_delete are refused.
        assert "restrict_openaev_writes" in self.RUN_AGENT_CHAT_SOURCE
        assert "strip_openaev_write_tools" in self.RUN_AGENT_CHAT_SOURCE
        assert "set_advisory_openaev_writes" in self.RUN_AGENT_CHAT_SOURCE
        assert "reset_advisory_openaev_writes" in self.RUN_AGENT_CHAT_SOURCE

    def test_run_agent_chat_forces_advisory_for_arsenal_specialist(self):
        # A built-in arsenal-advisory specialist (Payload Creator) gets the
        # restrict flag forced on from its identity, so the background-task path
        # (which never passes restrict_openaev_writes) is still create/update-only.
        assert (
            "is_openaev_arsenal_advisory_agent" in self.RUN_AGENT_CHAT_SOURCE
        )

    def test_consult_propagates_advisory_flag_to_target(self):
        # The advisory flag, or a restriction the consulting turn was itself
        # given by its caller (a consulted peer consulting in turn).
        assert (
            "_consult_restricts_openaev = advisory_consult_specialists or restrict_openaev_writes"
            in self.RUN_AGENT_CHAT_SOURCE
        )
        assert "restrict_openaev_writes=_consult_restricts_openaev" in self.RUN_AGENT_CHAT_SOURCE

    def test_autonomous_adapter_enables_advisory_consults(self):
        assert (
            "advisory_consult_specialists=True" in self.ADAPTER_SOURCE
        )
