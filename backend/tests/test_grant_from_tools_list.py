"""Granting a standing approval from the tools list (FR-045).

The same decision as "Yes, always" inside a pause, reached from a list. What
needs pinning is the refusal: a standing approval is fingerprinted against the
tool's schema so that changing its parameters withdraws consent (FR-019a), and
an MCP or integration tool has no schema until its server is bound. Granting one
anyway would promise "this will not ask again" and then ask again on the first
real call — so it is refused, with the reason.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import tool_approvals as api

USER = SimpleNamespace(
    id=uuid.uuid4(), role="user", email="u@example.com", impersonated_by=None
)


class _DB:
    """Answers the custom-tool lookup and the MCP sweep, swallows the commit."""

    def __init__(self, custom_schema=None, mcp_tools=()):
        self.custom_id = uuid.uuid4()
        self._custom_schema = custom_schema
        self.server_id = uuid.uuid4()
        self._servers = [
            SimpleNamespace(
                id=self.server_id,
                name="Wiki server",
                discovered_tools=list(mcp_tools),
            )
        ]

    async def execute(self, _stmt):
        rows = self._servers
        custom = (
            SimpleNamespace(id=self.custom_id, input_schema=self._custom_schema)
            if self._custom_schema is not None
            else None
        )
        return SimpleNamespace(
            first=lambda: custom,
            scalars=lambda: SimpleNamespace(all=lambda: rows),
        )

    async def commit(self):
        pass


@pytest.fixture(autouse=True)
def _no_groups(monkeypatch):
    """The MCP sweep resolves the caller's groups; the stub has none."""
    import app.api.v1.tool_approvals as mod

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(mod, "get_user_group_ids", _groups, raising=False)
    import app.api.v1.helpers as helpers

    monkeypatch.setattr(helpers, "get_user_group_ids", _groups)


@pytest.fixture
def _granting(monkeypatch):
    granted: list[dict] = []

    async def _grant(_db, **kwargs):
        granted.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    async def _audit(*_a, **_k):
        return None

    monkeypatch.setattr(api, "grant_exception", _grant)
    monkeypatch.setattr(api, "log_audit", _audit)
    return granted


@pytest.mark.asyncio
async def test_a_builtin_is_granted_against_its_own_schema(_granting):
    result = await api.grant_my_tool_approval(
        api.GrantRequest(tool_key="update_agent"),
        request=None,
        user=USER,
        db=_DB(),
    )

    assert result["tool_source"] == "builtin"
    assert _granting[0]["tool_key"] == "update_agent"
    # Fingerprinted, not left blank: that is what makes the grant withdrawable
    # when the tool's parameters change.
    assert _granting[0]["schema_fingerprint"]


@pytest.mark.asyncio
async def test_a_custom_tool_is_granted_too(_granting):
    db = _DB(custom_schema={"type": "object", "properties": {}})
    result = await api.grant_my_tool_approval(
        api.GrantRequest(tool_key="my_script"),
        request=None,
        user=USER,
        db=db,
    )

    # The tool's own id: the gate covers a custom tool only by an approval
    # granted to that very tool.
    assert result["tool_source"] == f"custom:{db.custom_id}"
    assert _granting[0]["tool_source"] == f"custom:{db.custom_id}"


@pytest.mark.asyncio
async def test_a_tool_with_no_definition_yet_is_refused_with_the_reason(_granting):
    with pytest.raises(HTTPException) as exc:
        await api.grant_my_tool_approval(
            api.GrantRequest(tool_key="mcp_search_wiki"),
            request=None,
            user=USER,
            db=_DB(),
        )

    assert exc.value.status_code == 400
    assert "whitelist" in exc.value.detail
    assert not _granting, "nothing may be written when the shape is unknown"


@pytest.mark.asyncio
async def test_an_empty_tool_key_is_refused(_granting):
    with pytest.raises(HTTPException) as exc:
        await api.grant_my_tool_approval(
            api.GrantRequest(tool_key="   "), request=None, user=USER, db=_DB()
        )

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_an_mcp_tool_is_granted_against_what_the_server_discovered(_granting):
    """The definition was there all along.

    These were refused on the belief that an MCP or integration tool has no
    schema until bound — but a bound server's ``discovered_tools`` carry one,
    and so do the provider and bot builders. The refusal now means what it says:
    nothing connected still claims this name.
    """
    db = _DB(
        mcp_tools=[{"name": "search_wiki", "input_schema": {"type": "object"}}]
    )

    result = await api.grant_my_tool_approval(
        api.GrantRequest(tool_key="search_wiki"), request=None, user=USER, db=db
    )

    assert result["tool_source"] == "mcp:Wiki server"
    # Keyed by the server's id, not its name: the name is the owner's to choose.
    assert _granting[0]["tool_entity_id"] == str(db.server_id)
    assert _granting[0]["schema_fingerprint"]


@pytest.mark.asyncio
async def test_an_integration_tool_is_granted_against_its_builder(_granting):
    """Built by dummy credential, touching no connection."""
    result = await api.grant_my_tool_approval(
        api.GrantRequest(tool_key="create_notion_page"),
        request=None,
        user=USER,
        db=_DB(),
    )

    assert result["tool_source"] == "integration:notion"
