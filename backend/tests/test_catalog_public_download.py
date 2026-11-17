"""What a Filigran AI Catalog download may hand to another user.

``GET /public/catalog/agents/{id}/download`` is the agent export bundle, given
to any authenticated user of THIS instance - where every id of the publisher's
rows still resolves. So what it carries is an allow-list of its own, per bundle
section: a key the export builder starts emitting tomorrow stays out of public
downloads until it is classified in ``app/models/agent_copy.py``.

The tripwire below runs the REAL export builder over a fully populated agent,
so it fails on a new key whatever the builder's shape - including a portable
agent field added to the bundle manifest.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api.v1 import agent_import as agent_import_api
from app.core.utils import mask_secret_arg
from app.models.agent import Agent
from app.models.agent_copy import (
    AGENT_BUNDLE_EXPORT_ONLY_FIELDS,
    AGENT_BUNDLE_PORTABLE_FIELDS,
    PUBLIC_DOWNLOAD_AGENT_FIELDS,
    PUBLIC_DOWNLOAD_ASSIGNMENT_FIELDS,
    PUBLIC_DOWNLOAD_BUNDLE_KEYS,
    PUBLIC_DOWNLOAD_DROPPED_AGENT_FIELDS,
    PUBLIC_DOWNLOAD_DROPPED_ASSIGNMENT_FIELDS,
    PUBLIC_DOWNLOAD_ENTRY_FIELDS,
    REDACTED_MCP_SERVER_ID,
    REDACTED_RUNNER_ID,
    TRIGGER_CONFIG_INTERNAL_ID_KEYS,
    TRIGGER_CONFIG_PORTABLE_KEYS,
    strip_internal_ids_for_public_download,
)
from app.models.agent_skill import AgentSkill
from app.models.assignment import Assignment
from app.models.custom_tool import CustomTool
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase
from app.models.mcp_server import MCPServer
from app.models.prompt import Prompt
from app.models.variable import Variable
from app.services import agent_bundle_projection
from app.services.agentschema import build_agentschema_export, prepare_agent_import

FRONTEND_TYPES = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "types" / "index.ts"
)


class _Result:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def scalar(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _BundleDB:
    """Answers each ``select(Model)`` of the export builder with its rows."""

    def __init__(self, rows_by_model=None):
        self._rows = rows_by_model or {}
        self.added: list[object] = []

    async def execute(self, stmt):
        descriptions = getattr(stmt, "column_descriptions", None)
        if descriptions is None:
            # The provider config advisory lock the catalog clone takes.
            return _Result([])
        entity = descriptions[0].get("entity")
        return _Result(self._rows.get(entity, []))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self):
        return None

    async def refresh(self, _obj):
        return None


def _uid() -> str:
    return str(uuid.uuid4())


def _populated_agent_and_rows():
    """An agent with every bundle field set, and one row of each kind it names."""
    owner = uuid.uuid4()
    peer = _uid()
    variable = Variable(
        id=uuid.uuid4(), user_id=owner, name="REGION", value="emea", is_secret=False
    )
    tool = CustomTool(
        id=uuid.uuid4(),
        name="Lookup",
        description="Looks things up",
        icon_url="https://example.invalid/i.png",
        input_schema={"type": "object"},
        js_function="return 1",
        language="python",
        timeout=42,
        tags=["t"],
    )
    server = MCPServer(
        id=uuid.uuid4(),
        name="Docs",
        transport="http",
        command=None,
        args=["--flag"],
        url="https://mcp.example.invalid",
        tags=["docs"],
    )
    integration = Integration(
        id=uuid.uuid4(), provider="github", name="Shared GitHub", is_company_managed=True
    )
    skill = AgentSkill(
        id=uuid.uuid4(),
        name="Triage skill",
        description="How to triage",
        url="https://example.invalid/SKILL.md",
        content="Do it well",
        compatibility="any",
        tags=["s"],
    )
    prompt = Prompt(
        id=uuid.uuid4(), name="Tone", description="House tone", content="Be brief", tags=[]
    )
    kb = KnowledgeBase(id=uuid.uuid4(), name="Runbooks", description="Ops runbooks")

    values = {name: None for name in AGENT_BUNDLE_PORTABLE_FIELDS}
    values.update(
        name="Published",
        description="d",
        icon="Bot",
        persona=f"Region: {{{{var_id.{variable.id}}}}}",
        output_format="markdown",
        output_schema={"type": "object"},
        integration_template_bindings=["opencti"],
        integration_allowed_tools={"github": ["get_issue"]},
        integration_element_bindings={"github": {"repository": ["org/repo"]}},
        tool_deny_list=["github:delete_repo"],
        builtin_tool_ids=["web_fetch"],
        runner_ids=[_uid()],
        runner_labels=["lab"],
        handover_types={peer: "consult"},
        handover_conditions={peer: "when asked"},
        handover_prompt="Delegate when needed.",
        tags=["x"],
        supported_intents={"cti.summary": {"cti": 0}},
        supported_verticals=["cti"],
        config={"finalize_on_builtin_tool": {}},
        llm_provider="openai",
        llm_model="gpt-x",
        temperature=0.2,
        reasoning_effort="low",
        max_iterations=5,
        budget_config={"max_tool_calls": 10},
        guardrails="Never delete.",
        memory_short_term_enabled=True,
        memory_medium_term_enabled=True,
        memory_persistent_enabled=True,
        memory_user_enabled=True,
        human_in_the_loop=True,
        human_in_the_loop_prompt="Ask first.",
        hide_runs=True,
        disable_chat=False,
        web_suggested_prompts=["Hi"],
    )
    agent = Agent(
        id=uuid.uuid4(),
        user_id=owner,
        work_item_owner_agent_ids=[peer],
        trace_supervision_target_agent_ids=[peer],
        integration_bindings={"github": [str(integration.id)]},
        custom_tool_ids=[str(tool.id)],
        mcp_server_ids=[str(server.id)],
        mcp_allowed_tools={str(server.id): ["search"]},
        skill_ids=[str(skill.id)],
        skill_load_modes={str(skill.id): "always"},
        prompt_ids=[str(prompt.id)],
        knowledge_base_ids=[str(kb.id)],
        **values,
    )
    assignment = Assignment(
        id=uuid.uuid4(),
        agent_id=agent.id,
        user_id=owner,
        name="Poll issues",
        description="Polls",
        trigger_type="integration_event",
        trigger_config={
            "provider": "github",
            "event": "new_issue",
            "integration_id": str(integration.id),
            "owner_agent_id": peer,
            "filter": {"scope": {"github_repository": ["org/repo"]}},
            "webhook_secret": "live-secret",
        },
        activation_prompt="Only bugs.",
        prompt="Triage it.",
        delay="10m",
        tool_sources=[str(server.id), _uid()],
        use_knowledge_bases=True,
        ai_label=True,
        ai_label_prompt="Label it.",
        notify_on_complete=True,
        show_in_digest=True,
        human_in_the_loop=True,
        human_in_the_loop_prompt="Confirm.",
        max_iterations=3,
        timeout_seconds=120,
        retry_on_failure=True,
        max_retries=2,
        dry_run=True,
        ignore_self_triggered_events=True,
        integration_allowed_tools={"github": ["get_issue"]},
        integration_element_bindings={"github": {"repository": ["org/repo"]}},
        hide_runs=True,
        manual_trigger_parameters={"type": "object"},
        enabled=True,
        read_knowledge_base_ids=[str(kb.id)],
        write_knowledge_base_ids=[str(kb.id)],
        write_knowledge_base_enabled=True,
        runner_ids=[_uid()],
    )
    rows = {
        Assignment: [assignment],
        CustomTool: [tool],
        MCPServer: [server],
        Integration: [integration],
        AgentSkill: [skill],
        Prompt: [prompt],
        KnowledgeBase: [kb],
        Variable: [variable],
    }
    return agent, rows


async def _real_public_download():
    agent, rows = _populated_agent_and_rows()
    bundle = await agent_bundle_projection.build_agent_export_bundle(_BundleDB(rows), agent)
    return agent, bundle, strip_internal_ids_for_public_download(bundle)


# --- The allow-list covers exactly what the export emits ---------------------


def test_public_download_agent_classification_is_disjoint():
    assert set(PUBLIC_DOWNLOAD_AGENT_FIELDS) & set(PUBLIC_DOWNLOAD_DROPPED_AGENT_FIELDS) == set()
    assert (
        set(PUBLIC_DOWNLOAD_ASSIGNMENT_FIELDS) & set(PUBLIC_DOWNLOAD_DROPPED_ASSIGNMENT_FIELDS)
        == set()
    )


@pytest.mark.parametrize(
    "reasons", [PUBLIC_DOWNLOAD_DROPPED_AGENT_FIELDS, PUBLIC_DOWNLOAD_DROPPED_ASSIGNMENT_FIELDS]
)
def test_every_field_a_public_download_drops_states_a_reason(reasons):
    for name, reason in reasons.items():
        assert isinstance(reason, str) and len(reason.strip()) > 20, name


async def test_every_key_the_export_builder_emits_is_classified_for_public_download():
    """The tripwire: a key the export starts emitting must be classified before
    it can reach (or be kept out of) a public download."""
    _agent, bundle, _stripped = await _real_public_download()

    unclassified = set(bundle) - set(PUBLIC_DOWNLOAD_BUNDLE_KEYS)
    assert unclassified == set(), f"unclassified bundle sections: {sorted(unclassified)}"

    agent_classified = set(PUBLIC_DOWNLOAD_AGENT_FIELDS) | set(PUBLIC_DOWNLOAD_DROPPED_AGENT_FIELDS)
    assert set(bundle["agent"]) == agent_classified, (
        "The export's agent section and the public-download classification "
        f"disagree: unclassified {sorted(set(bundle['agent']) - agent_classified)}, "
        f"stale {sorted(agent_classified - set(bundle['agent']))}"
    )

    [assignment] = bundle["assignments"]
    assignment_classified = set(PUBLIC_DOWNLOAD_ASSIGNMENT_FIELDS) | set(
        PUBLIC_DOWNLOAD_DROPPED_ASSIGNMENT_FIELDS
    )
    assert set(assignment) == assignment_classified, (
        f"unclassified {sorted(set(assignment) - assignment_classified)}, "
        f"stale {sorted(assignment_classified - set(assignment))}"
    )

    for section, fields in PUBLIC_DOWNLOAD_ENTRY_FIELDS.items():
        entries = bundle[section]
        assert entries, f"the fixture must populate {section} for the check to mean anything"
        for entry in entries:
            extra = set(entry) - set(fields)
            assert extra == set(), f"unclassified {section} key(s): {sorted(extra)}"


def test_a_new_portable_agent_field_needs_a_public_download_decision():
    """The public download does not inherit the bundle manifest: a field that is
    portable ACROSS instances can still name rows that resolve on THIS one."""
    exported = set(AGENT_BUNDLE_PORTABLE_FIELDS) | set(AGENT_BUNDLE_EXPORT_ONLY_FIELDS)
    classified = set(PUBLIC_DOWNLOAD_AGENT_FIELDS) | set(PUBLIC_DOWNLOAD_DROPPED_AGENT_FIELDS)
    assert exported == classified


# --- What a public download never carries ------------------------------------


async def test_a_public_download_carries_no_id_of_the_publishers_rows():
    _agent, bundle, stripped = await _real_public_download()

    # Handover maps are keyed by the publisher's peer agents, which resolve on
    # this instance; the import has no targets to apply them to anyway.
    assert "handover_types" in bundle["agent"]
    assert "handover_types" not in stripped["agent"]
    assert "handover_conditions" not in stripped["agent"]
    assert "work_item_owner_agent_ids" not in stripped["agent"]
    assert "trace_supervision_target_agent_ids" not in stripped["agent"]

    [assignment] = stripped["assignments"]
    trigger = assignment["trigger_config"]
    # The integration a trigger polls and the queue a work-item inbox drains
    # are the publisher's rows; the webhook secret is a live credential.
    for key in (*TRIGGER_CONFIG_INTERNAL_ID_KEYS, "webhook_secret"):
        assert key not in trigger, key
    assert trigger["provider"] == "github" and trigger["event"] == "new_issue"
    assert trigger["filter"] == {"scope": {"github_repository": ["org/repo"]}}
    assert "read_knowledge_base_ids" not in assignment
    assert "write_knowledge_base_ids" not in assignment

    # Narrowing selectors are redacted, never dropped.
    assert stripped["agent"]["runner_ids"] == [REDACTED_RUNNER_ID]
    assert assignment["runner_ids"] == [REDACTED_RUNNER_ID]
    assert REDACTED_MCP_SERVER_ID in assignment["tool_sources"]
    assert all(
        entry == REDACTED_MCP_SERVER_ID
        for entry in assignment["tool_sources"]
        if _is_uuid(entry)
    )

    # No id of a publisher's row survives anywhere but the variable ids the
    # prompts reference (the import remaps them) and the redaction sentinels.
    allowed_ids = {str(v["id"]) for v in stripped["variables"]} | {REDACTED_MCP_SERVER_ID}
    leaked = _uuids_in(stripped) - allowed_ids
    assert leaked == set(), f"public download leaks internal ids: {sorted(leaked)}"


def test_a_public_download_drops_every_key_it_does_not_know():
    bundle = {
        "version": 3,
        "export_type": "agent",
        "future_section": [{"id": _uid()}],
        "agent": {
            "name": "Published",
            "tool_deny_list": ["x"],
            "future_agent_field": _uid(),
            "handover_types": {_uid(): "consult"},
        },
        "assignments": [
            {
                "name": "a",
                "future_assignment_field": _uid(),
                "trigger_config": {
                    "cron": "0 9 * * 1",
                    "integration_id": _uid(),
                    "owner_agent_id": _uid(),
                },
            },
            "not-an-object",
        ],
        "custom_tools": [{"name": "t", "js_function": "x", "env": {"TOKEN": "s3cret"}}],
        "mcp_servers": [{"name": "m", "headers": {"Authorization": "Bearer s3cret"}}],
        "variables": [{"id": _uid(), "name": "V", "value_enc": "cipher"}],
    }

    stripped = strip_internal_ids_for_public_download(bundle)

    assert "future_section" not in stripped
    assert stripped["agent"] == {"name": "Published", "tool_deny_list": ["x"]}
    assert stripped["assignments"] == [
        {"name": "a", "trigger_config": {"cron": "0 9 * * 1"}}
    ]
    assert stripped["custom_tools"] == [{"name": "t", "js_function": "x"}]
    assert stripped["mcp_servers"] == [{"name": "m"}]
    assert "value_enc" not in stripped["variables"][0]
    # The input is left untouched.
    assert "future_agent_field" in bundle["agent"]
    assert "integration_id" in bundle["assignments"][0]["trigger_config"]


def test_a_credential_in_an_mcp_server_definition_is_masked_in_a_public_download():
    """Any user of the instance can fetch a public download: a credential
    passed as an argument or carried in the URL never leaves raw."""
    token = "ghp_" + "A" * 36
    bundle = {
        "mcp_servers": [
            {
                "name": "GitHub",
                "command": "npx",
                "args": [
                    "-y",
                    "@modelcontextprotocol/server-github",
                    "--token",
                    token,
                    f"GITHUB_TOKEN={token}",
                ],
                "url": "postgresql://app:S3cret@db.internal/prod",
            }
        ]
    }

    [server] = strip_internal_ids_for_public_download(bundle)["mcp_servers"]

    assert server["args"] == [
        "-y",
        "@modelcontextprotocol/server-github",
        "--token",
        "***",
        "GITHUB_TOKEN=***",
    ]
    assert server["url"] == "postgresql://app:***@db.internal/prod"
    assert token not in json.dumps(server) and "S3cret" not in json.dumps(server)
    assert bundle["mcp_servers"][0]["args"][3] == token


async def test_a_reader_outside_the_audience_neither_sees_nor_downloads_a_narrowed_resource(
    monkeypatch,
):
    """A company-managed tool, MCP server, skill or prompt narrowed to an
    audience (``access_restricted``) the reader is not in is not the reader's:
    the catalog page does not list it and the download leaves it out - its
    content and the variables only it names - as the install does. What the
    reader may see still travels. (``bindable_ids`` is the real predicate;
    the PostgreSQL suite evaluates it.)"""
    from app.api.v1 import public_catalog

    owner = uuid.uuid4()
    audience_variable = Variable(
        id=uuid.uuid4(), user_id=owner, name="AUDIENCE_HOST", value="h", is_secret=False
    )
    narrowed = {"is_company_managed": True, "access_restricted": True}
    tool = CustomTool(
        id=uuid.uuid4(), name="Audience tool",
        js_function=f"return '{{{{var_id.{audience_variable.id}}}}}'", **narrowed,
    )
    server = MCPServer(
        id=uuid.uuid4(), name="Audience MCP", transport="http",
        url="https://audience.invalid", **narrowed,
    )
    skill = AgentSkill(id=uuid.uuid4(), name="Audience skill", content="audience playbook", **narrowed)
    prompt = Prompt(id=uuid.uuid4(), name="Audience prompt", content="audience tone", **narrowed)
    open_tool = CustomTool(
        id=uuid.uuid4(), name="Company lookup", js_function="return 1", is_company_managed=True
    )
    now = datetime.now(UTC)
    agent = Agent(
        id=uuid.uuid4(),
        user_id=owner,
        name="Published",
        persona="p",
        is_public_catalog=True,
        catalog_include_personal_tools=True,
        created_at=now,
        updated_at=now,
        custom_tool_ids=[str(tool.id), str(open_tool.id)],
        mcp_server_ids=[str(server.id)],
        skill_ids=[str(skill.id)],
        prompt_ids=[str(prompt.id)],
    )
    visible = {str(open_tool.id)}

    async def _bindable_ids(_db, _model, raw_ids, _user, _group_ids):
        return {str(i) for i in raw_ids or [] if str(i) in visible}

    monkeypatch.setattr(agent_bundle_projection, "bindable_ids", _bindable_ids)
    db = _BundleDB(
        {
            Agent: [agent],
            CustomTool: [tool, open_tool],
            MCPServer: [server],
            AgentSkill: [skill],
            Prompt: [prompt],
            Variable: [audience_variable],
        }
    )
    reader = SimpleNamespace(
        id=uuid.uuid4(), email="r@example.com", full_name="Reader", is_admin=False, role="user"
    )

    detail = json.loads(
        (await public_catalog.get_public_agent(agent_id=str(agent.id), user=reader, db=db)).body
    )
    bundle = json.loads(
        (
            await public_catalog.download_catalog_item(
                entity_type="agents", entity_id=str(agent.id), user=reader, db=db
            )
        ).body
    )

    assert [t["name"] for t in detail["custom_tools"]] == ["Company lookup"]
    assert detail["mcp_servers"] == detail["skills"] == detail["prompts"] == []
    assert [t["name"] for t in bundle["custom_tools"]] == ["Company lookup"]
    assert bundle["mcp_servers"] == bundle["skills"] == bundle["prompts"] == []
    assert bundle["variables"] == []
    for leaked in ("audience playbook", "audience tone", "audience.invalid", "AUDIENCE_HOST"):
        assert leaked not in json.dumps(bundle), leaked
    # Not presented as the publisher's choice either.
    assert "omitted_custom_tools" not in bundle


async def test_a_published_mcp_server_never_shows_downloads_or_copies_a_credential(
    monkeypatch,
):
    """The standalone catalog MCP server: its list entry, its page, its
    download and the installer's copy (whose export is raw) mask a credential
    in the arguments or the URL."""
    from app.api.v1 import public_catalog

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(public_catalog, "log_audit", _noop)
    token = "sk-" + "b" * 40
    now = datetime.now(UTC)
    server = MCPServer(
        id=uuid.uuid4(), name="Search", transport="http", command=None,
        args=["--api-key", "short-key", token],
        url="https://search.example.com/mcp?token=live-token&region=eu",
        tags=[], is_public_catalog=True, is_company_managed=True,
        created_at=now, updated_at=now,
    )
    db = _BundleDB({MCPServer: [server]})
    reader = SimpleNamespace(
        id=uuid.uuid4(), email="r@example.com", full_name="Reader", is_admin=False, role="user"
    )
    masked_args = ["--api-key", "***", mask_secret_arg(token)]
    masked_url = "https://search.example.com/mcp?token=***&region=eu"

    listed = json.loads(
        (
            await public_catalog.list_public_mcp_servers(
                search=None, tags=None, offset=0, limit=20, user=reader, db=db
            )
        ).body
    )
    page = json.loads(
        (
            await public_catalog.get_public_mcp_server(server_id=str(server.id), user=reader, db=db)
        ).body
    )
    download = json.loads(
        (
            await public_catalog.download_catalog_item(
                entity_type="mcp-servers", entity_id=str(server.id), user=reader, db=db
            )
        ).body
    )
    await public_catalog.add_catalog_item_to_account(
        entity_type="mcp-servers",
        entity_id=str(server.id),
        body=None,
        user=reader,
        db=db,
        _ai_gate=None,
    )

    assert [item["url"] for item in listed["items"]] == [masked_url]
    assert (page["args"], page["url"]) == (masked_args, masked_url)
    assert (download["mcp_server"]["args"], download["mcp_server"]["url"]) == (
        masked_args,
        masked_url,
    )
    [copy] = [obj for obj in db.added if isinstance(obj, MCPServer)]
    assert (copy.args, copy.url) == (masked_args, masked_url)
    assert server.args[1] == "short-key"


# --- The redaction sentinel is a UUID that names no server --------------------


def test_the_redacted_mcp_server_id_is_a_valid_uuid_no_row_can_carry():
    """``load_mcp_tools`` parses every ``tool_sources`` entry with ``uuid.UUID``;
    the placeholder must parse, and must be an id no row is ever given."""
    assert uuid.UUID(REDACTED_MCP_SERVER_ID) == uuid.UUID(int=0)


async def test_a_redacted_mcp_scope_loads_no_server_without_failing_the_run(caplog):
    from app.services.builtin_tools import load_mcp_tools

    composite = SimpleNamespace(add_tools=lambda *_a, **_kw: None)
    with caplog.at_level(logging.ERROR):
        manager = await load_mcp_tools(_BundleDB(), [REDACTED_MCP_SERVER_ID], composite)

    assert manager is None
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [], errors


# --- trigger_config: every key the platform writes is classified -------------


def test_every_trigger_config_key_the_frontend_writes_is_classified():
    """``trigger_config`` is free-form JSON, so it travels whole and its
    id-bearing keys are handled by name; a new key must be classified."""
    source = FRONTEND_TYPES.read_text(encoding="utf-8")
    match = re.search(r"export interface TriggerConfig \{(.*?)\n\}", source, re.S)
    assert match, "TriggerConfig moved - point this test at it"
    keys = set(re.findall(r"^  (\w+)\??:", match.group(1), re.M))
    classified = (
        set(TRIGGER_CONFIG_PORTABLE_KEYS) | set(TRIGGER_CONFIG_INTERNAL_ID_KEYS) | {"webhook_secret"}
    )
    assert keys, "no TriggerConfig key parsed"
    assert keys - classified == set(), (
        f"unclassified trigger_config key(s): {sorted(keys - classified)}"
    )


# --- The download stays importable (no breaking change) ----------------------


async def test_a_public_download_imports_through_every_path(monkeypatch):
    _agent, _bundle, stripped = await _real_public_download()

    # The file-upload path's verification loop accepts it ...
    prepared = prepare_agent_import(json.dumps(stripped).encode("utf-8"), filename="agent.json")
    assert prepared.bundle["agent"]["name"] == "Published"
    # ... and so does the AgentSchema rendering and its re-import.
    yaml_text, _document = build_agentschema_export(stripped)
    reimported = prepare_agent_import(yaml_text.encode("utf-8"), filename="agent.yaml")
    assert reimported.bundle["agent"]["name"] == "Published"

    db = _BundleDB()
    imported = await _import(monkeypatch, db, stripped)
    agent = imported.agent
    # Every safety field survives the trip.
    assert agent.tool_deny_list == ["github:delete_repo"]
    assert agent.human_in_the_loop is True
    assert agent.handover_types is None
    [wf] = [obj for obj in db.added if isinstance(obj, Assignment)]
    assert wf.ignore_self_triggered_events is True
    assert wf.activation_prompt == "Only bugs."
    assert wf.dry_run is True
    assert "integration_id" not in wf.trigger_config
    assert "owner_agent_id" not in wf.trigger_config
    # The imported MCP scope stays narrow: no entry names the publisher's server.
    assert wf.tool_sources and all(
        entry != str(_agent.mcp_server_ids[0]) for entry in wf.tool_sources
    )
    # Custom tools keep their language and timeout.
    [tool] = [obj for obj in db.added if isinstance(obj, CustomTool)]
    assert tool.language == "python"
    assert tool.timeout == 42


# --- Referenced variables ------------------------------------------------------


async def test_a_variable_only_a_custom_tool_names_travels_with_the_bundle(monkeypatch):
    """The tool loader resolves ``{{var_id.<uuid>}}`` in a custom tool's code
    and description, and the import rewrites both: a variable only a tool names
    must travel, or the imported tool runs with a placeholder naming nothing."""
    owner = uuid.uuid4()
    in_code, in_description = (
        Variable(id=uuid.uuid4(), user_id=owner, name=name, value="v", is_secret=False)
        for name in ("API_HOST", "TEAM")
    )
    tool = CustomTool(
        id=uuid.uuid4(),
        name="Lookup",
        description=f"Looks up for {{{{var_id.{in_description.id}}}}}",
        js_function=f"return '{{{{var_id.{in_code.id}}}}}'",
    )
    agent = Agent(
        id=uuid.uuid4(),
        user_id=owner,
        name="Tooling",
        persona="No variable here.",
        custom_tool_ids=[str(tool.id)],
    )

    bundle = await agent_bundle_projection.build_agent_export_bundle(
        _BundleDB({CustomTool: [tool], Variable: [in_code, in_description]}), agent
    )

    assert {v["id"] for v in bundle["variables"]} == {str(in_code.id), str(in_description.id)}
    db = _BundleDB()
    await _import(monkeypatch, db, strip_internal_ids_for_public_download(bundle))
    created = {v.name: str(v.id) for v in db.added if isinstance(v, Variable)}
    [imported_tool] = [obj for obj in db.added if isinstance(obj, CustomTool)]
    assert imported_tool.js_function == f"return '{{{{var_id.{created['API_HOST']}}}}}'"
    assert imported_tool.description == f"Looks up for {{{{var_id.{created['TEAM']}}}}}"


async def test_an_agent_without_an_owner_discloses_what_every_user_may_see():
    """With no owner to read for, disclosure is the company-wide tier of the
    shared predicate - never a hand-assembled copy of it."""
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql

    from app.api.v1.helpers import company_wide_visible_filter
    from app.services.variable_resolver import load_owner_visible_variable_records

    class _CapturingDB:
        def __init__(self):
            self.statements = []

        async def execute(self, stmt):
            self.statements.append(stmt)
            return _Result()

    db = _CapturingDB()
    variable_id = uuid.uuid4()
    await load_owner_visible_variable_records(db, [str(variable_id)], None)

    def _sql(stmt) -> str:
        return str(stmt.compile(dialect=postgresql.dialect()))

    [stmt] = db.statements
    expected = select(Variable).where(
        Variable.id.in_([variable_id]), company_wide_visible_filter(Variable)
    )
    assert _sql(stmt) == _sql(expected)


# --- helpers -----------------------------------------------------------------


async def _import(monkeypatch, db, bundle):
    """Run the shared import pipeline over ``bundle`` with its side effects
    (slug probe, version history, audit) stubbed out."""

    async def _unique_slug(_db, name):
        return "imported"

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent_import_api, "generate_unique_agent_slug", _unique_slug)
    monkeypatch.setattr(agent_import_api, "seed_initial_agent_versions", _noop)
    monkeypatch.setattr(agent_import_api, "log_audit", _noop)
    return await agent_import_api.create_agent_from_bundle(
        db,
        user=SimpleNamespace(
            id=uuid.uuid4(), email="i@example.com", full_name="Installer", is_admin=False, role="user"
        ),
        bundle=bundle,
    )

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


def _uuids_in(value) -> set[str]:
    return {m.lower() for m in _UUID_RE.findall(json.dumps(value, default=str))}
