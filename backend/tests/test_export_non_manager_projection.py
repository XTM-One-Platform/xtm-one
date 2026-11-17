"""An export by a caller who can see an agent or an MCP server but not manage
it is the Filigran AI Catalog download's projection.

``GET /agents/{id}/export`` and ``GET /mcp-servers/{id}/export`` handed every
caller who could SEE the entity its full bundle - for a company-managed agent,
every user of the instance: the credential passed to an MCP server, the code of
the owner's personal custom tools, the content of company-managed resources
narrowed to an audience the caller is not in, the owner's variable values. A
caller who cannot manage the entity (``helpers.can_manage``, the rule its write
routes enforce) now gets what the catalog download carries, with no variable
values; whoever manages it - its owner, a ``config`` grantee, an admin - keeps
the full bundle byte for byte.

The routes run over the export builder's session double; the visibility
predicate behind the narrowing (``bindable_ids``) is stubbed here and evaluated
for real by the PostgreSQL suite.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import yaml
from fastapi.responses import JSONResponse

from app.api.v1 import agents as agents_api
from app.api.v1 import mcp_servers as mcp_servers_api
from app.api.v1.schemas.mcp_server import ImportMCPServerRequest
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.assignment import Assignment
from app.models.custom_tool import CustomTool
from app.models.mcp_server import MCPServer
from app.models.prompt import Prompt
from app.models.variable import Variable
from app.services import agent_bundle_projection
from app.services.agentschema import prepare_agent_import
from tests.test_catalog_public_download import _BundleDB, _import

_NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
_TOKEN = "ghp_" + "A" * 36
_URL_KEY = "live-url-key"
_MASKED_ARGS = ["-y", "@modelcontextprotocol/server-github", "--token", "***", "--port", "8080"]
_MASKED_URL = "https://mcp.example.com/mcp?api_key=***&region=eu"
#: What no export by a non-manager may carry: the credentials, the owner's
#: personal tool code, the narrowed resources and the owner's variable value.
_WITHHELD = (
    _TOKEN,
    _URL_KEY,
    "owner-only code",
    "Audience tool",
    "audience code",
    "Audience MCP",
    "audience.invalid",
    "Audience skill",
    "audience playbook",
    "Audience prompt",
    "audience tone",
    "emea-private",
)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _NOW


def _user(*, role: str = "user") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        email=f"{role}-{uuid.uuid4().hex[:6]}@example.com",
        full_name=role.title(),
        role=role,
        is_admin=role == "admin",
    )


def _share(user: SimpleNamespace, permission: str) -> dict:
    return {
        "shared_user_ids": [str(user.id)],
        "shared_user_permissions": {str(user.id): permission},
    }


def _server(**overrides) -> MCPServer:
    values = {
        "id": uuid.uuid4(),
        "name": "GitHub",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github", "--token", _TOKEN, "--port", "8080"],
        "url": f"https://mcp.example.com/mcp?api_key={_URL_KEY}&region=eu",
        "auth_mode": "oauth",
        "scopes": "repo read:org",
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "registration_url": f"https://auth.example.com/register?token={_URL_KEY}",
        "tags": ["dev"],
        "is_company_managed": True,
    }
    values.update(overrides)
    return MCPServer(**values)


def _agent_and_db(*, company_managed: bool, **sharing):
    """An agent bound to everything a non-manager must not receive, next to
    what they still get; plus the ids ``bindable_ids`` reports visible."""
    owner = uuid.uuid4()
    variable = Variable(
        id=uuid.uuid4(), user_id=owner, name="REGION", value="emea-private", is_secret=False
    )
    narrowed = {"is_company_managed": True, "access_restricted": True}
    personal_tool = CustomTool(
        id=uuid.uuid4(),
        user_id=owner,
        name="Owner scraper",
        js_function="return 'owner-only code'",
        is_company_managed=False,
    )
    open_tool = CustomTool(
        id=uuid.uuid4(), name="Company lookup", js_function="return 1", is_company_managed=True
    )
    narrowed_tool = CustomTool(
        id=uuid.uuid4(), name="Audience tool", js_function="return 'audience code'", **narrowed
    )
    server = _server()
    narrowed_server = MCPServer(
        id=uuid.uuid4(),
        name="Audience MCP",
        transport="http",
        url="https://audience.invalid/mcp",
        **narrowed,
    )
    narrowed_skill = AgentSkill(
        id=uuid.uuid4(), name="Audience skill", content="audience playbook", **narrowed
    )
    open_prompt = Prompt(id=uuid.uuid4(), name="Tone", content="Be brief", is_company_managed=True)
    narrowed_prompt = Prompt(
        id=uuid.uuid4(), name="Audience prompt", content="audience tone", **narrowed
    )
    agent = Agent(
        id=uuid.uuid4(),
        user_id=owner,
        name="Analyst",
        persona=f"Region: {{{{var_id.{variable.id}}}}}",
        is_company_managed=company_managed,
        # Its value once inserted; a new or never-published agent is off.
        catalog_include_personal_tools=False,
        handover_types={str(uuid.uuid4()): "consult"},
        custom_tool_ids=[str(t.id) for t in (personal_tool, open_tool, narrowed_tool)],
        mcp_server_ids=[str(server.id), str(narrowed_server.id)],
        skill_ids=[str(narrowed_skill.id)],
        prompt_ids=[str(open_prompt.id), str(narrowed_prompt.id)],
        **sharing,
    )
    assignment = Assignment(
        id=uuid.uuid4(),
        agent_id=agent.id,
        user_id=owner,
        name="Hook",
        trigger_type="webhook",
        trigger_config={"path": "hook", "webhook_secret": "live-webhook-secret"},
        prompt="Summarise.",
    )
    db = _BundleDB(
        {
            Agent: [agent],
            Assignment: [assignment],
            CustomTool: [personal_tool, open_tool, narrowed_tool],
            MCPServer: [server, narrowed_server],
            AgentSkill: [narrowed_skill],
            Prompt: [open_prompt, narrowed_prompt],
            Variable: [variable],
        }
    )
    return agent, db, {str(open_tool.id), str(server.id), str(open_prompt.id)}


def _stub_visibility(monkeypatch, visible: set[str]) -> None:
    async def _bindable_ids(_db, _model, raw_ids, _user, _group_ids):
        return {str(i) for i in raw_ids or [] if str(i) in visible}

    monkeypatch.setattr(agent_bundle_projection, "bindable_ids", _bindable_ids)


async def _export(monkeypatch, agent, db, user, *, fmt: str = "native"):
    audit: list[dict] = []

    async def _log_audit(_db, **kwargs):
        audit.append(kwargs)

    monkeypatch.setattr(agents_api, "log_audit", _log_audit)
    monkeypatch.setattr(agent_bundle_projection, "datetime", _FrozenDatetime)
    response = await agents_api.export_agent(
        agent_id=str(agent.id), request=None, format=fmt, user=user, db=db
    )
    return response, audit


# --- An agent export by a caller who cannot manage the agent ----------------


@pytest.mark.parametrize("case", ["company_managed", "read_only_share"])
async def test_a_caller_who_cannot_manage_the_agent_exports_the_catalog_projection(
    monkeypatch, case
):
    reader = _user()
    if case == "company_managed":
        agent, db, visible = _agent_and_db(company_managed=True)
    else:
        agent, db, visible = _agent_and_db(company_managed=False, **_share(reader, "read_only"))
    _stub_visibility(monkeypatch, visible)

    response, audit = await _export(monkeypatch, agent, db, reader)

    bundle = json.loads(response.body)
    text = json.dumps(bundle)
    for withheld in _WITHHELD:
        assert withheld not in text, withheld
    [server] = bundle["mcp_servers"]
    assert (server["args"], server["url"]) == (_MASKED_ARGS, _MASKED_URL)
    assert [t["name"] for t in bundle["custom_tools"]] == ["Company lookup"]
    assert bundle["omitted_custom_tools"] == [{"name": "Owner scraper"}]
    assert [p["name"] for p in bundle["prompts"]] == ["Tone"]
    assert bundle["skills"] == []
    # The variable still travels, by name: the importer fills in its value.
    [variable] = bundle["variables"]
    assert variable["name"] == "REGION" and "value" not in variable
    assert "live-webhook-secret" not in text
    assert "handover_types" not in bundle["agent"]
    assert audit[0]["details"] == {"format": "native", "bundle_version": 3, "redacted": True}


async def test_a_non_manager_agentschema_export_is_the_same_projection(monkeypatch):
    agent, db, visible = _agent_and_db(company_managed=True)
    _stub_visibility(monkeypatch, visible)

    response, audit = await _export(monkeypatch, agent, db, _user(), fmt="agentschema")

    text = response.body.decode("utf-8")
    for withheld in _WITHHELD:
        assert withheld not in text, withheld
    assert yaml.safe_load(text)["kind"] == "prompt"
    prepared = prepare_agent_import(response.body, filename="agent.agent.yaml")
    assert prepared.bundle["agent"]["name"] == "Analyst"
    assert audit[0]["details"]["redacted"] is True


async def test_a_projected_export_still_imports(monkeypatch):
    """The installer re-enters the masked values and fills in the variables, as
    after a catalog download."""
    agent, db, visible = _agent_and_db(company_managed=True)
    _stub_visibility(monkeypatch, visible)
    response, _audit = await _export(monkeypatch, agent, db, _user())

    prepared = prepare_agent_import(response.body, filename="agent.json")
    target = _BundleDB()
    imported = await _import(monkeypatch, target, prepared.bundle)

    assert [t.name for t in target.added if isinstance(t, CustomTool)] == ["Company lookup"]
    [server] = [s for s in target.added if isinstance(s, MCPServer)]
    assert (server.args, server.url) == (_MASKED_ARGS, _MASKED_URL)
    [variable] = [v for v in target.added if isinstance(v, Variable)]
    assert (variable.name, variable.value) == ("REGION", None)
    assert imported.agent.persona == f"Region: {{{{var_id.{variable.id}}}}}"


async def test_the_portability_preview_reads_the_bundle_the_caller_exports(monkeypatch):
    agent, db, visible = _agent_and_db(company_managed=True)
    _stub_visibility(monkeypatch, visible)

    async def _labels(user) -> dict[str, str]:
        response = await agents_api.agent_export_portability(
            agent_id=str(agent.id), format="agentschema", user=user, db=db
        )
        return {entry.key: entry.label for entry in response.entries}

    managed = await _labels(_user(role="admin"))
    projected = await _labels(_user())

    assert (managed["prompts"], projected["prompts"]) == ("Prompts (2)", "Prompts (1)")
    assert "skills" in managed and "skills" not in projected
    assert "handover" in managed and "handover" not in projected


# --- An agent export by a caller who manages it ------------------------------


@pytest.mark.parametrize(
    "case", ["admin_company_managed", "admin_colleague_private", "owner", "config_grantee"]
)
async def test_a_caller_who_manages_the_agent_keeps_the_full_bundle_byte_for_byte(
    monkeypatch, case
):
    """An admin reaching a colleague's private agent by navigation keeps what
    ``main`` gives an admin (decision 9), like every other manager."""
    user = _user(role="admin" if case.startswith("admin") else "user")
    if case == "admin_company_managed":
        agent, db, _visible = _agent_and_db(company_managed=True)
    elif case == "config_grantee":
        agent, db, _visible = _agent_and_db(company_managed=False, **_share(user, "config"))
    else:
        agent, db, _visible = _agent_and_db(company_managed=False)
        if case == "owner":
            agent.user_id = user.id

    response, audit = await _export(monkeypatch, agent, db, user)

    full = await agent_bundle_projection.build_agent_export_bundle(db, agent)
    assert response.body == JSONResponse(content=full).body
    assert audit[0]["details"] == {"format": "native", "bundle_version": 3}
    text = response.body.decode("utf-8")
    for carried in (_TOKEN, "owner-only code", "audience playbook", "emea-private"):
        assert carried in text, carried


# --- The MCP server export -----------------------------------------------------


async def _export_server(monkeypatch, server, user):
    monkeypatch.setattr(mcp_servers_api, "datetime", _FrozenDatetime)
    return await mcp_servers_api.export_mcp_server(
        server_id=str(server.id), user=user, db=_BundleDB({MCPServer: [server]})
    )


def _full_server_bundle(server: MCPServer) -> dict:
    """The bundle ``export_mcp_server`` hands a manager, field for field."""
    return {
        "version": 1,
        "export_type": "mcp_server",
        "exported_at": _NOW.isoformat(),
        "mcp_server": {
            "name": server.name,
            "transport": server.transport,
            "command": server.command,
            "args": server.args,
            "url": server.url,
            "auth_mode": server.auth_mode,
            "scopes": server.scopes,
            "authorize_url": server.authorize_url,
            "token_url": server.token_url,
            "registration_url": server.registration_url,
            "tags": server.tags or [],
        },
    }


@pytest.mark.parametrize("case", ["company_managed", "read_only_share"])
async def test_a_caller_who_cannot_manage_the_server_exports_it_masked(monkeypatch, case):
    reader = _user()
    if case == "company_managed":
        server = _server()
    else:
        server = _server(user_id=uuid.uuid4(), is_company_managed=False, **_share(reader, "read_only"))

    response = await _export_server(monkeypatch, server, reader)

    exported = json.loads(response.body)["mcp_server"]
    assert _TOKEN not in response.body.decode() and _URL_KEY not in response.body.decode()
    assert (exported["args"], exported["url"]) == (_MASKED_ARGS, _MASKED_URL)
    assert exported["registration_url"] == "https://auth.example.com/register?token=***"
    # What is not a credential still travels, so the server imports configured.
    assert (exported["auth_mode"], exported["scopes"]) == ("oauth", "repo read:org")
    assert exported["authorize_url"] == server.authorize_url
    assert exported["token_url"] == server.token_url


@pytest.mark.parametrize(
    "case", ["admin_company_managed", "admin_colleague_private", "owner", "config_grantee"]
)
async def test_a_caller_who_manages_the_server_keeps_the_full_bundle_byte_for_byte(
    monkeypatch, case
):
    user = _user(role="admin" if case.startswith("admin") else "user")
    if case == "admin_company_managed":
        server = _server()
    elif case == "admin_colleague_private":
        server = _server(user_id=uuid.uuid4(), is_company_managed=False)
    elif case == "owner":
        server = _server(user_id=user.id, is_company_managed=False)
    else:
        server = _server(user_id=uuid.uuid4(), is_company_managed=False, **_share(user, "config"))

    response = await _export_server(monkeypatch, server, user)

    assert response.body == JSONResponse(content=_full_server_bundle(server)).body


async def test_a_masked_server_export_still_imports(monkeypatch):
    response = await _export_server(monkeypatch, _server(), _user())

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mcp_servers_api, "require_allowed_token_url", _noop)
    monkeypatch.setattr(mcp_servers_api, "log_audit", _noop)
    monkeypatch.setattr(mcp_servers_api, "_to_response", lambda srv, **_kwargs: srv)
    installer = _user()
    db = _BundleDB()
    await mcp_servers_api.import_mcp_server(
        body=ImportMCPServerRequest(**json.loads(response.body)),
        request=None,
        user=installer,
        db=db,
    )

    [copy] = db.added
    assert (copy.args, copy.url) == (_MASKED_ARGS, _MASKED_URL)
    assert (copy.auth_mode, copy.token_url) == ("oauth", "https://github.com/login/oauth/access_token")
    assert copy.user_id == installer.id and copy.is_company_managed is False
