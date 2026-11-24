"""Duplicating / exporting an agent must not silently drop its access config.

Regression tests for a report from a design partner that duplicated the
built-in ``OpenCTI Dashboard Creator`` and found the copy "kept losing access to
OpenCTI": the duplicate handler never carried ``integration_template_bindings``,
the per-user integration resolution list every built-in agent relies on, so the
copy came out with no OpenCTI tools at all. The same list also carried no
``tool_deny_list``, so a copy re-granted the tools the source had explicitly
blocked.

These tests drive the real handlers against a stubbed DB layer, the same way
``test_agent_update_partial_payload`` does — the assertions are about which
field values reach the new ``Agent`` row, not about SQL.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import agent_import as agent_import_api
from app.api.v1 import agents as agents_api
from app.models.agent import Agent
from app.models.agent_copy import AGENT_COPY_SKIP_FIELDS
from app.models.assignment import Assignment
from app.models.mcp_server import MCPServer
from app.services import agent_bundle_projection


class _Result:
    """Stands in for an ``await db.execute(...)`` result."""

    def __init__(self, rows=()):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _FakeDB:
    def __init__(self):
        self.added: list[object] = []
        self.committed = False

    async def execute(self, _stmt):
        return _Result()

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        return None


def _user(user_id: uuid.UUID | None = None, *, admin: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id or uuid.uuid4(),
        email="analyst@example.com",
        full_name="Analyst",
        is_admin=admin,
        role="admin" if admin else "user",
    )


def _opencti_agent(**overrides) -> Agent:
    """A built-in agent shaped like ``opencti-dashboard-creator``."""
    values = {
        "id": uuid.uuid4(),
        "user_id": None,
        "is_builtin": True,
        "is_company_managed": True,
        "name": "OpenCTI Dashboard Creator",
        "slug": "opencti-dashboard-creator",
        "description": "Creates OpenCTI dashboards",
        "icon": "LayoutDashboard",
        "persona": "You build dashboards.",
        # The field that was lost: per-user integration resolution.
        "integration_template_bindings": ["opencti"],
        # Deny takes precedence over every allow list.
        "tool_deny_list": ["delete_opencti_entity"],
        "integration_allowed_tools": {"opencti": ["list_reports", "create_dashboard"]},
        "supported_intents": {"cti.dashboard": {"cti": 0}},
        "supported_verticals": ["cti"],
        "handover_prompt": "Hand over to the CTI assistant when asked to hunt.",
        "human_in_the_loop": True,
        "human_in_the_loop_prompt": "Confirm before writing to OpenCTI.",
        "hide_runs": True,
        "web_suggested_prompts": ["Build me a dashboard"],
        "tags": ["cti"],
        "builtin_tool_ids": ["inspect_my_traces"],
        "max_iterations": 40,
        "enabled": True,
        "web_enabled": True,
        "web_token": "live-secret-token",
        "is_public_catalog": True,
        "shared_group_ids": [str(uuid.uuid4())],
        "shared_user_ids": [str(uuid.uuid4())],
        "access_restricted": True,
        "available_channel_types": ["web", "slack"],
    }
    values.update(overrides)
    return Agent(**values)


def _stub_duplicate_dependencies(monkeypatch, source: Agent):
    async def _group_ids(_db, _user_id):
        return []

    async def _get_user_agent(_db, _user_id, _agent_id, _group_ids, **_kwargs):
        return source

    async def _unique_slug(_db, name):
        return name.lower().replace(" ", "-")

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agents_api, "get_user_group_ids", _group_ids)
    monkeypatch.setattr(agents_api, "get_user_agent", _get_user_agent)
    monkeypatch.setattr(agents_api, "generate_unique_agent_slug", _unique_slug)
    monkeypatch.setattr(agents_api, "seed_initial_agent_versions", _noop)
    monkeypatch.setattr(agents_api, "log_audit", _noop)
    monkeypatch.setattr(
        agents_api, "_to_response", lambda row, *a, **k: SimpleNamespace(model_dump=dict)
    )
    monkeypatch.setattr(agents_api, "DuplicateAgentResponse", lambda **kwargs: kwargs)


async def _duplicate(monkeypatch, source: Agent) -> Agent:
    _stub_duplicate_dependencies(monkeypatch, source)
    db = _FakeDB()
    user = _user()
    request = SimpleNamespace(client=None, headers={}, url=None)
    await agents_api.duplicate_agent(
        str(source.id), request, user=user, db=db, _agentic_gate=None
    )
    return next(obj for obj in db.added if isinstance(obj, Agent))


@pytest.mark.asyncio
async def test_duplicate_keeps_per_user_integration_bindings(monkeypatch):
    """The headline: the copy must still resolve OpenCTI per user."""
    source = _opencti_agent()
    copy = await _duplicate(monkeypatch, source)

    assert copy.integration_template_bindings == ["opencti"]
    assert copy.integration_allowed_tools == {
        "opencti": ["list_reports", "create_dashboard"]
    }


@pytest.mark.asyncio
async def test_duplicate_keeps_tool_deny_list(monkeypatch):
    """A copy that forgot the deny list silently re-granted denied tools."""
    source = _opencti_agent()
    copy = await _duplicate(monkeypatch, source)

    assert copy.tool_deny_list == ["delete_opencti_entity"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("handover_prompt", "Hand over to the CTI assistant when asked to hunt."),
        ("supported_verticals", ["cti"]),
        ("human_in_the_loop", True),
        ("human_in_the_loop_prompt", "Confirm before writing to OpenCTI."),
        ("hide_runs", True),
        ("web_suggested_prompts", ["Build me a dashboard"]),
        ("supported_intents", {"cti.dashboard": {"cti": 0}}),
        ("available_channel_types", ["web", "slack"]),
    ],
)
async def test_duplicate_carries_every_configured_field(monkeypatch, field, expected):
    source = _opencti_agent()
    copy = await _duplicate(monkeypatch, source)

    assert getattr(copy, field) == expected


@pytest.mark.asyncio
async def test_duplicate_resets_identity_ownership_and_exposure(monkeypatch):
    """The deliberate omissions stay omitted — a copy is a private new row."""
    source = _opencti_agent()
    user_id = uuid.uuid4()
    _stub_duplicate_dependencies(monkeypatch, source)
    db = _FakeDB()
    await agents_api.duplicate_agent(
        str(source.id),
        SimpleNamespace(client=None, headers={}, url=None),
        user=_user(user_id),
        db=db,
        _agentic_gate=None,
    )
    copy = next(obj for obj in db.added if isinstance(obj, Agent))

    assert copy.user_id == user_id
    assert copy.is_company_managed is False
    assert copy.name == "Copy of OpenCTI Dashboard Creator"
    assert copy.slug == "copy-of-opencti-dashboard-creator"
    assert copy.web_enabled is False
    assert copy.web_token is None
    assert copy.is_builtin in (False, None)
    assert copy.is_public_catalog in (False, None)
    assert copy.shared_group_ids is None
    assert copy.shared_user_ids is None
    assert copy.access_restricted in (False, None)


@pytest.mark.asyncio
async def test_duplicate_never_shares_a_mutable_container_with_the_source(monkeypatch):
    source = _opencti_agent()
    copy = await _duplicate(monkeypatch, source)

    copy.integration_template_bindings.append("openaev")
    copy.integration_allowed_tools["opencti"].append("delete_dashboard")

    assert source.integration_template_bindings == ["opencti"]
    assert source.integration_allowed_tools["opencti"] == [
        "list_reports",
        "create_dashboard",
    ]


@pytest.mark.asyncio
async def test_duplicate_clamps_an_out_of_range_max_iterations(monkeypatch):
    source = _opencti_agent(max_iterations=100_000)
    copy = await _duplicate(monkeypatch, source)

    assert copy.max_iterations < 100_000


@pytest.mark.asyncio
async def test_duplicate_defaults_a_legacy_null_channel_list_to_web(monkeypatch):
    source = _opencti_agent(available_channel_types=None)
    copy = await _duplicate(monkeypatch, source)

    assert copy.available_channel_types == ["web"]


def test_every_skipped_copy_field_is_identity_ownership_or_activation():
    """Guards the skip list against becoming a dumping ground.

    The bindings were lost because "not copied" was the silent default. Anything added
    to the skip list from now on has to be one of the four kinds of thing that
    genuinely cannot be inherited.
    """
    allowed = {
        "id",
        "created_at",
        "updated_at",
        "name",
        "slug",
        "user_id",
        "is_company_managed",
        "shared_group_ids",
        "shared_group_permissions",
        "shared_user_ids",
        "shared_user_permissions",
        "access_restricted",
        "is_builtin",
        "is_public_catalog",
        # The publishing choice of what the catalog download carries:
        # meaningless on a copy that is not published.
        "catalog_include_personal_tools",
        "enabled",
        "web_enabled",
        # Visitor uploads on the public widget: activation state,
        # re-allowed on the copy's own widget.
        "web_uploads_enabled",
        "web_token",
        # The owner's grant to lend their personal integrations through this
        # agent: an ownership/sharing grant, never inherited by a copy.
        "integration_lending",
        # Platform Health (#8254): the managers' consent (a grant, like a
        # share) and the fleet role (platform-managed identity) never travel.
        "health_enrolled",
        "health_content_opt_in",
        "health_role",
        # A sandbox copy (#8255) is Platform Health's own row: a duplicate is an
        # ordinary agent, whatever it was copied from.
        "is_sandbox",
        # Activity hidden from non-admins (#11624): an administrator's switch
        # on a company-managed agent (activation state); a personal copy never
        # hides its activity.
        "hide_activity_from_non_admins",
        # The customer layer of a built-in (#914): this instance's organization
        # notes and the seed's bookkeeping; a copy is never seeded.
        "customer_notes",
        "customer_fields",
    }
    assert set(AGENT_COPY_SKIP_FIELDS) == allowed, (
        "The copy skip list changed. A field only belongs here if it is an "
        "identity, an ownership/sharing grant, platform-managed state, or "
        "activation state — otherwise the copy loses configuration."
    )


# --- Export / import round-trip ---------------------------------------------


@pytest.mark.asyncio
async def test_export_bundle_carries_the_portable_access_config():
    bundle = await agent_bundle_projection.build_agent_export_bundle(_FakeDB(), _opencti_agent())
    exported = bundle["agent"]

    assert exported["integration_template_bindings"] == ["opencti"]
    assert exported["tool_deny_list"] == ["delete_opencti_entity"]
    assert exported["integration_allowed_tools"] == {
        "opencti": ["list_reports", "create_dashboard"]
    }
    assert exported["hide_runs"] is True
    assert exported["human_in_the_loop"] is True
    assert exported["supported_verticals"] == ["cti"]
    # Secrets and cross-tenant references never leave the platform.
    assert "web_token" not in exported
    assert "integration_bindings" not in exported
    assert "channel_config" not in exported


@pytest.mark.asyncio
async def test_import_restores_per_user_integration_bindings(monkeypatch):
    async def _unique_slug(_db, name):
        return name.lower().replace(" ", "-")

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent_import_api, "generate_unique_agent_slug", _unique_slug)
    monkeypatch.setattr(agent_import_api, "seed_initial_agent_versions", _noop)
    monkeypatch.setattr(agent_import_api, "log_audit", _noop)

    db = _FakeDB()
    bundle = {
        "version": 3,
        "export_type": "agent",
        "agent": {
            "name": "OpenCTI Dashboard Creator",
            "persona": "You build dashboards.",
            "integration_template_bindings": ["opencti"],
            "tool_deny_list": ["delete_opencti_entity"],
            "integration_allowed_tools": {"opencti": ["list_reports"]},
            "handover_prompt": "Hand over when asked to hunt.",
            "supported_verticals": ["cti"],
            "human_in_the_loop": True,
            "hide_runs": True,
        },
    }
    imported = await agent_import_api.create_agent_from_bundle(
        db,
        user=_user(),
        bundle=bundle,
    )
    agent = imported.agent

    assert agent.integration_template_bindings == ["opencti"]
    assert agent.tool_deny_list == ["delete_opencti_entity"]
    assert agent.integration_allowed_tools == {"opencti": ["list_reports"]}
    assert agent.handover_prompt == "Hand over when asked to hunt."
    assert agent.supported_verticals == ["cti"]
    assert agent.human_in_the_loop is True
    assert agent.hide_runs is True
    # Cross-instance references are still dropped.
    assert agent.integration_bindings == {}
    assert agent.available_channel_types == ["web"]


@pytest.mark.asyncio
async def test_import_ignores_non_portable_fields_smuggled_into_a_bundle(monkeypatch):
    """A bundle is untrusted input: skipped fields must not be writable."""

    async def _unique_slug(_db, name):
        return "imported"

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent_import_api, "generate_unique_agent_slug", _unique_slug)
    monkeypatch.setattr(agent_import_api, "seed_initial_agent_versions", _noop)
    monkeypatch.setattr(agent_import_api, "log_audit", _noop)

    imported = await agent_import_api.create_agent_from_bundle(
        _FakeDB(),
        user=_user(),
        bundle={
            "version": 3,
            "export_type": "agent",
            "agent": {
                "name": "Trojan",
                "is_builtin": True,
                "is_company_managed": True,
                "is_public_catalog": True,
                "web_enabled": True,
                "web_token": "attacker-token",
                "shared_group_ids": [str(uuid.uuid4())],
                "integration_bindings": {"opencti": [str(uuid.uuid4())]},
                "channel_config": {"slack": {"integration_id": str(uuid.uuid4())}},
            },
        },
    )
    agent = imported.agent

    assert agent.is_builtin in (False, None)
    assert agent.is_company_managed is False
    assert agent.is_public_catalog in (False, None)
    assert agent.web_enabled in (False, None)
    assert agent.web_token is None
    assert agent.shared_group_ids is None
    assert agent.channel_config is None
    assert agent.integration_bindings == {}


@pytest.mark.asyncio
async def test_mcp_tool_scope_survives_an_export_import_round_trip(monkeypatch):
    """An id-keyed map cannot travel, so it rides inside each server entry."""
    server_id = uuid.uuid4()
    source = _opencti_agent(
        mcp_server_ids=[str(server_id)],
        mcp_allowed_tools={str(server_id): ["fetch_url"]},
    )

    class _ServerDB(_FakeDB):
        async def execute(self, _stmt):
            if "mcp_servers" in str(_stmt):
                return _Result(
                    [
                        SimpleNamespace(
                            id=server_id,
                            name="Docs",
                            transport="http",
                            command=None,
                            args=[],
                            url="https://example.invalid/mcp",
                            tags=[],
                        )
                    ]
                )
            return _Result()

    bundle = await agent_bundle_projection.build_agent_export_bundle(_ServerDB(), source)
    assert bundle["mcp_servers"][0]["allowed_tools"] == ["fetch_url"]

    async def _unique_slug(_db, name):
        return "imported"

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent_import_api, "generate_unique_agent_slug", _unique_slug)
    monkeypatch.setattr(agent_import_api, "seed_initial_agent_versions", _noop)
    monkeypatch.setattr(agent_import_api, "log_audit", _noop)

    imported = await agent_import_api.create_agent_from_bundle(
        _FakeDB(),
        user=_user(),
        bundle=bundle,
    )
    agent = imported.agent

    # Re-keyed onto the server the import created, never onto the exporter's id.
    assert str(server_id) not in (agent.mcp_allowed_tools or {})
    assert list((agent.mcp_allowed_tools or {}).values()) == [["fetch_url"]]
    assert list((agent.mcp_allowed_tools or {}).keys()) == agent.mcp_server_ids


@pytest.mark.asyncio
async def test_assignment_mcp_scope_survives_an_export_import_round_trip(monkeypatch):
    """An assignment scoped to one of the agent's MCP servers re-imports
    scoped to the copy of that server the import creates - not to the
    exporter's server id, which resolves to nothing on the importing side."""
    server_id = uuid.uuid4()
    outside_server = str(uuid.uuid4())  # a server the agent does not bind
    source = _opencti_agent(mcp_server_ids=[str(server_id)])
    scoped = _assignment(source.id, tool_sources=[str(server_id), outside_server])

    class _BundleDB(_FakeDB):
        async def execute(self, _stmt):
            sql = str(_stmt)
            if "mcp_servers" in sql:
                return _Result(
                    [
                        SimpleNamespace(
                            id=server_id,
                            name="Docs",
                            transport="http",
                            command=None,
                            args=[],
                            url="https://example.invalid/mcp",
                            tags=[],
                        )
                    ]
                )
            if "assignments" in sql:
                return _Result([scoped])
            return _Result()

    bundle = await agent_bundle_projection.build_agent_export_bundle(_BundleDB(), source)
    assert bundle["mcp_servers"][0]["ref"] == "mcp-0"
    # The bundle names the agent's server by its ref, never by the source id.
    assert bundle["assignments"][0]["tool_sources"] == ["mcp-0", outside_server]

    imported, [new_server], [wf], _db = await _import_bundle(monkeypatch, bundle)
    # Re-keyed onto the recreated server. The id from outside the bundle names
    # no server the importer may use, so it is dropped - and said so.
    assert wf.tool_sources == [str(new_server.id)]
    assert any("'Nightly sweep'" in w for w in imported.warnings)


@pytest.mark.asyncio
async def test_export_names_an_agent_server_by_its_ref_whatever_its_spelling():
    """The assignment API stores the spelling of a server id it was sent: an
    agent's server named in uppercase is still exported as its ref, so the
    scope survives an import on another instance instead of being dropped."""
    server_id = uuid.uuid4()
    source = _opencti_agent(mcp_server_ids=[str(server_id)])
    scoped = _assignment(source.id, tool_sources=[str(server_id).upper()])

    class _BundleDB(_FakeDB):
        async def execute(self, _stmt):
            sql = str(_stmt)
            if "mcp_servers" in sql:
                return _Result(
                    [
                        SimpleNamespace(
                            id=server_id,
                            name="Docs",
                            transport="http",
                            command=None,
                            args=[],
                            url="https://example.invalid/mcp",
                            tags=[],
                        )
                    ]
                )
            if "assignments" in sql:
                return _Result([scoped])
            return _Result()

    bundle = await agent_bundle_projection.build_agent_export_bundle(_BundleDB(), source)

    assert bundle["assignments"][0]["tool_sources"] == ["mcp-0"]


#: The nil UUID: never the id of a server.
_NO_SERVER = "00000000-0000-0000-0000-000000000000"


class _ImportDB(_FakeDB):
    """Answers the importer's MCP server visibility lookup with ``visible``,
    and a lookup without the three-tier predicate (an admin's) with
    ``existing``, as PostgreSQL would."""

    def __init__(self, visible=(), existing=()):
        super().__init__()
        self._visible = [uuid.UUID(str(server_id)) for server_id in visible]
        self._existing = [uuid.UUID(str(server_id)) for server_id in existing]

    async def execute(self, stmt):
        sql = str(stmt)
        if "FROM mcp_servers" in sql:
            return _Result(self._visible if "mcp_servers.user_id" in sql else self._existing)
        return _Result()


async def _import_bundle(monkeypatch, bundle, *, visible=(), existing=(), user=None):
    async def _unique_slug(_db, name):
        return "imported"

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent_import_api, "generate_unique_agent_slug", _unique_slug)
    monkeypatch.setattr(agent_import_api, "seed_initial_agent_versions", _noop)
    monkeypatch.setattr(agent_import_api, "log_audit", _noop)

    db = _ImportDB(visible, existing)
    imported = await agent_import_api.create_agent_from_bundle(
        db, user=user or _user(), bundle=bundle
    )
    servers = [o for o in db.added if isinstance(o, MCPServer)]
    assignments = [o for o in db.added if isinstance(o, Assignment)]
    return imported, servers, assignments, db


def _scoped_bundle(*scopes, servers=None) -> dict:
    """One MCP server (``ref`` ``mcp-0``) and one assignment per scope."""
    if servers is None:
        servers = [
            {
                "ref": "mcp-0",
                "name": "Docs",
                "transport": "streamable_http",
                "url": "https://example.invalid/mcp",
            }
        ]
    return {
        "version": 3,
        "export_type": "agent",
        "agent": {"name": "Scoped"},
        "mcp_servers": servers,
        "assignments": [
            {"name": f"Scope {i}", "prompt": "Work", "tool_sources": scope}
            for i, scope in enumerate(scopes)
        ],
    }


@pytest.mark.asyncio
async def test_import_never_binds_a_server_the_importer_cannot_see(monkeypatch):
    """The engine loads every id of ``tool_sources`` with no visibility check,
    so an id carried by a bundle (a same-instance export, a crafted file) must
    not reach an imported assignment unless the importer may use that server."""
    foreign = str(uuid.uuid4())
    imported, [server], [wf], _db = await _import_bundle(
        monkeypatch, _scoped_bundle(["mcp-0", foreign])
    )

    assert wf.tool_sources == [str(server.id)]
    assert len(imported.warnings) == 1
    assert "'Scope 0'" in imported.warnings[0]


@pytest.mark.asyncio
async def test_import_keeps_a_server_outside_the_bundle_the_importer_can_see(
    monkeypatch,
):
    """A same-instance round trip keeps a scope entry the importer may use,
    written as the server id the engine matches."""
    own = str(uuid.uuid4())
    imported, [server], [wf], _db = await _import_bundle(
        monkeypatch, _scoped_bundle(["mcp-0", own.upper()]), visible=[own]
    )

    assert wf.tool_sources == [str(server.id), own]
    assert imported.warnings == []


@pytest.mark.asyncio
async def test_an_admin_import_keeps_a_colleagues_private_server(monkeypatch):
    """An admin may add any server to an assignment by hand, so an
    admin's import keeps a colleague's private server and a non-admin's does
    not. An id naming no server here is dropped for both."""
    private = str(uuid.uuid4())
    missing = str(uuid.uuid4())

    imported, [server], [wf], _db = await _import_bundle(
        monkeypatch,
        _scoped_bundle(["mcp-0", private, missing]),
        existing=[private],
        user=_user(admin=True),
    )
    assert wf.tool_sources == [str(server.id), private]
    assert len(imported.warnings) == 1

    imported, [server], [wf], _db = await _import_bundle(
        monkeypatch, _scoped_bundle(["mcp-0", private, missing]), existing=[private]
    )
    assert wf.tool_sources == [str(server.id)]
    assert len(imported.warnings) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    [
        "mcp-7",  # a ref no server of the bundle carries
        "redacted-mcp-server",  # the public catalog download's placeholder
        "11111111-1111-1111-1111-111111111111",  # a pre-ref bundle's source id
    ],
)
async def test_a_scope_with_nothing_left_stays_scoped_to_no_server(monkeypatch, entry):
    """An empty ``tool_sources`` means every server of the agent: a scope
    whose entries all fail to resolve keeps a marker that names no server."""
    imported, _servers, [wf], _db = await _import_bundle(
        monkeypatch, _scoped_bundle([entry])
    )

    assert wf.tool_sources == [_NO_SERVER]
    assert agent_import_api.UNRESOLVED_TOOL_SOURCE == _NO_SERVER
    assert len(imported.warnings) == 1


@pytest.mark.asyncio
async def test_an_inherited_scope_stays_inherited(monkeypatch):
    bundle = _scoped_bundle([], None)
    bundle["assignments"].append({"name": "No scope", "prompt": "Work"})
    imported, _servers, assignments, _db = await _import_bundle(monkeypatch, bundle)

    assert [wf.tool_sources for wf in assignments] == [[], None, []]
    assert imported.warnings == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", ["mcp-0", {"mcp-0": True}, 7, ["mcp-0", 7], [None]]
)
async def test_import_refuses_a_malformed_scope(monkeypatch, value):
    """Coercing a malformed scope to ``[]`` would widen it to every server."""
    with pytest.raises(HTTPException) as exc:
        await _import_bundle(monkeypatch, _scoped_bundle(value))

    assert exc.value.status_code == 400
    assert "'Scope 0'" in exc.value.detail


def _server_entry(**fields) -> dict:
    return {
        "name": "Docs",
        "transport": "streamable_http",
        "url": "https://example.invalid/mcp",
        **fields,
    }


@pytest.mark.asyncio
async def test_a_null_server_ref_reads_as_absent(monkeypatch):
    """A bundle field set to ``null`` reads as an absent key: the
    server is imported without a ref, so a scope naming the ref resolves to
    no server instead of failing the import."""
    imported, [server], [wf], _db = await _import_bundle(
        monkeypatch, _scoped_bundle(["mcp-0"], servers=[_server_entry(ref=None)])
    )

    assert server.name == "Docs"
    assert wf.tool_sources == [_NO_SERVER]
    assert len(imported.warnings) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", [0, ["mcp-0"], {"id": "mcp-0"}, True])
async def test_a_non_string_server_ref_is_refused(monkeypatch, ref):
    """Any other non-string is a 400 naming the field, never a server
    silently left unnamed."""
    with pytest.raises(HTTPException) as exc:
        await _import_bundle(
            monkeypatch, _scoped_bundle(["mcp-0"], servers=[_server_entry(ref=ref)])
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == (
        f"Imported MCP server entry 'ref' must be a string, got {type(ref).__name__}."
    )


@pytest.mark.asyncio
async def test_a_null_assignment_name_is_named_by_its_default_in_scope_messages(
    monkeypatch,
):
    """The scope refusal and the dropped-scope warning name the assignment the
    way the import names it: an explicit ``null`` name is the default, never
    ``'None'``."""
    bundle = _scoped_bundle(7)
    bundle["assignments"][0]["name"] = None
    with pytest.raises(HTTPException) as exc:
        await _import_bundle(monkeypatch, bundle)
    assert "'Imported Assignment'" in exc.value.detail
    assert "'None'" not in exc.value.detail

    bundle = _scoped_bundle(["mcp-9"])
    bundle["assignments"][0]["name"] = None
    imported, _servers, [wf], _db = await _import_bundle(monkeypatch, bundle)
    assert wf.name == "Imported Assignment"
    assert len(imported.warnings) == 1
    assert "'Imported Assignment'" in imported.warnings[0]


@pytest.mark.asyncio
async def test_a_dropped_placeholder_server_leaves_its_assignment_on_no_server(
    monkeypatch,
):
    """The check step may drop an unconfigured MCP placeholder the file's
    assignments name by ref: they must not fall back to every server."""
    from app.services.agentschema import apply_corrections

    bundle = _scoped_bundle(
        ["mcp-0"],
        servers=[
            {
                "ref": "mcp-0",
                "name": "Placeholder",
                "transport": "stdio",
                "command": None,
                "url": None,
                "tags": ["agentschema-import"],
            }
        ],
    )
    corrected = apply_corrections(bundle, ["drop_mcp_server:0"])
    imported, servers, [wf], _db = await _import_bundle(monkeypatch, corrected)

    assert servers == []
    assert wf.tool_sources == [_NO_SERVER]
    assert len(imported.warnings) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "exported"),
    [
        (None, None),
        ([], []),
        # Legacy values the API never writes, exported the way the engine
        # reads them so the file re-imports: empty = inherit, else no server.
        ("", None),
        ("garbage", ["garbage"]),
    ],
)
async def test_export_keeps_the_shape_of_the_scope(stored, exported):
    source = _opencti_agent(mcp_server_ids=[])
    wf = _assignment(source.id, tool_sources=stored)

    bundle = await agent_bundle_projection.build_agent_export_bundle(_AssignmentDB([wf]), source)

    assert bundle["assignments"][0]["tool_sources"] == exported


def test_bundle_tool_sources_resolve_refs_and_visible_ids_only():
    resolve = agent_import_api.resolve_bundle_tool_sources
    refs = {"mcp-0": "new-a", "mcp-1": "new-b"}
    own = str(uuid.uuid4())

    assert resolve(["mcp-0", "mcp-1"], id_by_ref=refs, visible_ids=set()) == (
        ["new-a", "new-b"],
        0,
    )
    assert resolve(
        [own.upper(), "mcp-9", str(uuid.uuid4())], id_by_ref=refs, visible_ids={own}
    ) == ([own], 2)
    assert resolve(["mcp-9"], id_by_ref=refs, visible_ids=set()) == ([_NO_SERVER], 1)
    assert resolve(["mcp-0", "mcp-0"], id_by_ref=refs, visible_ids=set()) == (
        ["new-a"],
        0,
    )
    # The no-server marker round-trips silently; inherit stays inherit.
    assert resolve([_NO_SERVER], id_by_ref=refs, visible_ids=set()) == ([_NO_SERVER], 0)
    assert resolve([], id_by_ref=refs, visible_ids=set()) == ([], 0)
    assert resolve(None, id_by_ref=refs, visible_ids=set()) == (None, 0)


# --- Assignments ride along with the copy -----------------------------------


def _assignment(agent_id: uuid.UUID, **overrides) -> Assignment:
    values = {
        "id": uuid.uuid4(),
        "agent_id": agent_id,
        "user_id": uuid.uuid4(),
        "name": "Nightly sweep",
        "trigger_type": "schedule",
        "trigger_config": {"cron": "0 2 * * *"},
        "prompt": "Review yesterday's reports",
        # Tri-state refinements: the old hand-written copy dropped all three.
        "runner_ids": [],
        "integration_element_bindings": {"github:organization": [{"id": "filigran"}]},
        "hide_runs": True,
        "manual_trigger_parameters": {"since": {"type": "string"}},
        # The self-trigger loop guard.
        "ignore_self_triggered_events": True,
        # Execution identity: admin-only, company-managed only.
        "run_as_user_id": uuid.uuid4(),
        "run_as_all_users": True,
        "is_company_managed": True,
        "enabled": True,
        "execution_count": 17,
    }
    values.update(overrides)
    return Assignment(**values)


class _AssignmentDB(_FakeDB):
    """A DB whose only ``Assignment`` query returns the given rows."""

    def __init__(self, assignments):
        super().__init__()
        self._assignments = assignments

    async def execute(self, stmt):
        if "assignments" in str(stmt):
            return _Result(self._assignments)
        return _Result()


async def _duplicate_with_assignments(monkeypatch, source, assignments):
    _stub_duplicate_dependencies(monkeypatch, source)
    db = _AssignmentDB(assignments)
    await agents_api.duplicate_agent(
        str(source.id),
        SimpleNamespace(client=None, headers={}, url=None),
        user=_user(),
        db=db,
        _agentic_gate=None,
    )
    return [obj for obj in db.added if isinstance(obj, Assignment)]


@pytest.mark.asyncio
async def test_duplicated_assignment_keeps_its_scope_and_loop_guard(monkeypatch):
    source = _opencti_agent()
    src_wf = _assignment(source.id)
    (copy,) = await _duplicate_with_assignments(monkeypatch, source, [src_wf])

    assert copy.runner_ids == []  # [] = "no runners", NOT "inherit the agent"
    assert copy.integration_element_bindings == {
        "github:organization": [{"id": "filigran"}]
    }
    assert copy.hide_runs is True
    assert copy.manual_trigger_parameters == {"since": {"type": "string"}}
    assert copy.ignore_self_triggered_events is True
    assert copy.prompt == "Review yesterday's reports"


@pytest.mark.asyncio
async def test_duplicated_assignment_never_inherits_the_run_as_identity(monkeypatch):
    """Any user who can see a company-managed agent may duplicate it."""
    source = _opencti_agent()
    (copy,) = await _duplicate_with_assignments(
        monkeypatch, source, [_assignment(source.id)]
    )

    assert copy.run_as_user_id is None
    assert copy.run_as_all_users in (False, None)
    assert copy.is_company_managed in (False, None)
    assert copy.enabled is False
    assert copy.execution_count in (0, None)


@pytest.mark.asyncio
async def test_duplicated_assignment_chain_points_at_the_copies(monkeypatch):
    source = _opencti_agent()
    second = _assignment(source.id, name="Follow-up")
    first = _assignment(source.id, next_assignment_id=second.id)
    copies = await _duplicate_with_assignments(monkeypatch, source, [first, second])

    by_name = {wf.name: wf for wf in copies}
    assert by_name["Nightly sweep"].next_assignment_id == by_name["Follow-up"].id
    assert by_name["Follow-up"].next_assignment_id is None


@pytest.mark.asyncio
async def test_a_chain_target_outside_the_copy_is_dropped(monkeypatch):
    """A copy must not wire itself into the source agent's pipeline."""
    source = _opencti_agent()
    (copy,) = await _duplicate_with_assignments(
        monkeypatch, source, [_assignment(source.id, next_assignment_id=uuid.uuid4())]
    )

    assert copy.next_assignment_id is None


@pytest.mark.asyncio
async def test_duplicated_webhook_assignment_gets_a_fresh_secret(monkeypatch):
    source = _opencti_agent()
    (copy,) = await _duplicate_with_assignments(
        monkeypatch,
        source,
        [
            _assignment(
                source.id,
                trigger_type="webhook",
                trigger_config={
                    "webhook_secret": "live-signing-secret",
                    "path": "/hook",
                },
            )
        ],
    )

    assert copy.trigger_config["webhook_secret"] != "live-signing-secret"
    assert copy.trigger_config["path"] == "/hook"
