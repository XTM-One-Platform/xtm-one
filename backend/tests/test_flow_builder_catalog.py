"""The flow-builder tool's node catalog mirrors the REST catalog's filters.

A node the platform cannot run is not offered: the HTTP request node's
"Route through" field needs a relay transport, and the runner command node
needs runners. Both branches of each filter are pinned here, since the
filter and the predicate it reads would otherwise agree by construction.
"""

import json
import uuid
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.builtin_tools.flow_builder import build_flow_builder_tool
from app.tools.base import ToolCall


class _Result:
    def __init__(self, user):
        self._user = user

    def scalar_one(self):
        return self._user


class _Session:
    """Answers the caller lookup; the catalog's sources are stubbed."""

    def __init__(self):
        self.user = SimpleNamespace(id=uuid.uuid4(), role="admin", is_admin=True)

    async def execute(self, _statement):
        return _Result(self.user)

    async def rollback(self):
        return None


async def _catalog(monkeypatch, *, relays: bool, runners: bool) -> dict:
    async def _sources(_db, _user, *, is_admin):
        return {"relay_groups": [{"value": "g1", "label": "Plant"}]}

    monkeypatch.setattr("app.services.flow_access.catalog_sources", _sources)
    monkeypatch.setattr("app.services.relays.relays_active", lambda: relays)
    monkeypatch.setattr(settings, "runners_enabled", runners)
    session = _Session()
    _defs, executor = build_flow_builder_tool(session, str(session.user.id))
    result = await executor.execute_tool(ToolCall(id="1", name="get_node_catalog", arguments={}))
    return json.loads(result.content)


@pytest.mark.asyncio
@pytest.mark.parametrize("relays", [True, False], ids=["relays", "no-relays"])
async def test_the_catalog_offers_the_relay_field_only_with_a_relay_transport(monkeypatch, relays):
    catalog = await _catalog(monkeypatch, relays=relays, runners=True)
    http = next(nk for nk in catalog["node_kinds"] if nk["kind"] == "action.http_request")
    offered = [f["name"] for f in http["config_fields"] if f["name"] == "relay_group_id"]
    assert offered == (["relay_group_id"] if relays else [])
    assert catalog["sources"]["relay_groups"] == [{"value": "g1", "label": "Plant"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("runners", [True, False], ids=["runners", "no-runners"])
async def test_the_catalog_offers_the_runner_node_only_with_runners(monkeypatch, runners):
    catalog = await _catalog(monkeypatch, relays=True, runners=runners)
    kinds = {nk["kind"] for nk in catalog["node_kinds"]}
    assert ("action.runner_command" in kinds) is runners
