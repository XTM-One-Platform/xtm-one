"""The OpenCRQ version card's route, ``GET /integrations/{id}/opencrq/compatibility``.

The tools follow the procedure manifest each OpenCRQ instance describes itself with. The route
answers through the functions the tool build gates on, so these tests hold the two together: the tools
it lists as hidden are the ones ``build_tools`` withholds for the same stored manifest. Every manifest
is built here from XTM One's own procedure paths and neutral input shapes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import MappingProxyType, SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

import app.integrations.providers.opencrq as opencrq
from app.api.v1 import opencrq as route
from app.core.platform_identity import ProcedureManifestStatus
from app.integrations.providers.opencrq.procedure_manifest import Manifest, load_tool_procedures
from app.tools.base import CallableToolExecutor

_USER = SimpleNamespace(id=uuid.uuid4(), role="user", is_admin=False)
FINGERPRINT = "ab" * 32
RECEIVED = datetime(2026, 10, 22, 8, 30, tzinfo=UTC)
# create_opencrq_group sends groups.createGroup (the keys id and name) and delete_opencrq_group groups.deleteGroup.
GROUP = "create_opencrq_group"
CREATE, DELETE = "groups.createGroup", "groups.deleteGroup"


def _integration(provider: str = "opencrq") -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), provider=provider)


@pytest.fixture
def opened(monkeypatch):
    """The integration the caller opens; what ``get_user_integration`` gives."""

    def _open(integration) -> None:
        async def _group_ids(db, user_id):
            return []

        async def _visible(db, user_id, integration_id, group_ids=None, *, is_admin=False, lock=False):
            return integration

        monkeypatch.setattr(route, "get_user_group_ids", _group_ids)
        monkeypatch.setattr(route, "get_user_integration", _visible)

    return _open


# --- who may read it ------------------------------------------------------------------


async def test_an_integration_that_is_not_opencrq_is_unknown(opened):
    integration = _integration(provider="openrouter")
    opened(integration)
    with pytest.raises(HTTPException) as raised:
        await route.get_compatibility(str(integration.id), user=_USER, db=None)
    assert raised.value.status_code == 404


async def test_an_integration_the_caller_cannot_see_is_unknown(monkeypatch):
    async def _group_ids(db, user_id):
        return []

    async def _invisible(*args, **kwargs):
        raise HTTPException(status_code=404, detail="Integration not found")

    monkeypatch.setattr(route, "get_user_group_ids", _group_ids)
    monkeypatch.setattr(route, "get_user_integration", _invisible)
    with pytest.raises(HTTPException) as raised:
        await route.get_compatibility(str(uuid.uuid4()), user=_USER, db=None)
    assert raised.value.status_code == 404


def test_the_route_is_open_to_everyone_who_can_open_the_integration():
    from app.api.deps import get_current_user, require_admin

    [compatibility] = [r for r in route.router.routes if isinstance(r, APIRoute)]
    stack, calls = list(compatibility.dependant.dependencies), set()
    while stack:
        dependency = stack.pop()
        calls.add(dependency.call)
        stack.extend(dependency.dependencies)
    assert get_current_user in calls
    assert require_admin not in calls


def test_the_route_is_mounted_under_the_integrations():
    from app.main import application

    paths = {r.path for r in application.routes if isinstance(r, APIRoute)}
    assert "/api/v1/integrations/{integration_id}/opencrq/compatibility" in paths


# --- the answer -----------------------------------------------------------------------


def _manifest(*, without: tuple[str, ...] = (), inputs: dict[str, object] | None = None) -> Manifest:
    """Every procedure a tool sends, with no input schema unless *inputs* gives one, minus *without*."""
    paths = {p for sent in load_tool_procedures().values() for p in sent} - set(without)
    return Manifest(
        FINGERPRINT,
        MappingProxyType(
            {p: MappingProxyType({"type": "query", "input": (inputs or {}).get(p)}) for p in sorted(paths)}
        ),
    )


@pytest.fixture
def registered(monkeypatch, opened):
    """Opens an OpenCRQ integration whose registration says *status*; ``None``: no registration owns it."""

    def _registered(status: ProcedureManifestStatus | None, registration_state: str = "configured") -> SimpleNamespace:
        integration = _integration()
        opened(integration)

        async def _status(db, integration_id):
            assert str(integration_id) == str(integration.id)
            return status

        monkeypatch.setattr(route, "procedure_manifest_status", _status)
        monkeypatch.setattr(route, "registration_token_state", lambda: registration_state)
        return integration

    return _registered


async def _answer(integration) -> dict:
    response = await route.get_compatibility(str(integration.id), user=_USER, db=None)
    return response.model_dump(mode="json")


async def test_an_integration_no_registration_owns_is_not_asked_and_nothing_is_hidden(registered):
    assert await _answer(registered(None)) == {
        "reported_version": None,
        "manifest_source": "none",
        "manifest_state": None,
        "fingerprint": None,
        "received_at": None,
        "tool_count": len(load_tool_procedures()),
        "hidden_tools": [],
        "limited_tools": [],
    }


async def test_a_manifest_that_is_current_hides_the_tools_the_instance_cannot_serve(registered):
    manifest = _manifest(without=(DELETE,))
    status = ProcedureManifestStatus("1.4.0", FINGERPRINT, manifest, RECEIVED)
    answer = await _answer(registered(status))
    assert answer["reported_version"] == "1.4.0"
    assert answer["manifest_source"] == "registration"
    assert answer["manifest_state"] == "current"
    assert answer["fingerprint"] == "ab" * 6
    assert answer["received_at"] == "2026-10-22T08:30:00Z"
    assert answer["tool_count"] == len(load_tool_procedures())
    assert {"name": "delete_opencrq_group", "missing_procedures": [DELETE]} in answer["hidden_tools"]
    assert answer["limited_tools"] == []


async def test_the_hidden_tools_are_exactly_the_ones_build_tools_withholds_for_the_same_manifest(registered):
    manifest = _manifest(without=(DELETE, "riskScenarios.updateRiskScenario"))
    answer = await _answer(registered(ProcedureManifestStatus("", FINGERPRINT, manifest, RECEIVED)))
    offered = {t.name for t in opencrq.build_tools("https://x||t", CallableToolExecutor(), opencrq_manifest=None)}
    kept = {t.name for t in opencrq.build_tools("https://x||t", CallableToolExecutor(), opencrq_manifest=manifest)}
    hidden = {tool["name"] for tool in answer["hidden_tools"]}
    assert hidden and hidden == offered - kept
    for tool in answer["hidden_tools"]:
        assert tool["missing_procedures"] == sorted(
            p for p in load_tool_procedures()[tool["name"]] if not manifest.serves(p)
        )


async def test_the_fields_the_instance_does_not_accept_are_listed_per_tool(registered):
    inputs = {
        CREATE: {
            "type": "object",
            "properties": {"name": {"type": "string"}, "slug": {"type": "string"}},
            "required": ["name", "slug"],
        }
    }
    manifest = _manifest(inputs=inputs)
    answer = await _answer(registered(ProcedureManifestStatus("1.4.0", FINGERPRINT, manifest, RECEIVED)))
    assert answer["limited_tools"] == [
        {
            "name": GROUP,
            "fields": [
                {"procedure": CREATE, "path": "id", "kind": "unknown"},
                {"procedure": CREATE, "path": "slug", "kind": "missing"},
            ],
        }
    ]


async def test_a_tool_withheld_is_not_also_listed_as_limited(registered):
    manifest = _manifest(
        without=(DELETE,), inputs={CREATE: {"type": "object", "properties": {"name": {"type": "string"}}}}
    )
    answer = await _answer(registered(ProcedureManifestStatus("", FINGERPRINT, manifest, RECEIVED)))
    assert {tool["name"] for tool in answer["limited_tools"]}.isdisjoint(
        tool["name"] for tool in answer["hidden_tools"]
    )
    assert [tool["name"] for tool in answer["limited_tools"]] == [GROUP]


async def test_a_fingerprint_reported_without_its_manifest_is_pending_and_hides_nothing(registered):
    answer = await _answer(registered(ProcedureManifestStatus("1.4.0", FINGERPRINT, None, None)))
    assert answer["manifest_state"] == "pending"
    assert answer["fingerprint"] == "ab" * 6
    assert answer["received_at"] is None
    assert answer["hidden_tools"] == [] and answer["limited_tools"] == []


async def test_a_registration_that_reports_no_fingerprint_is_unavailable(registered):
    answer = await _answer(registered(ProcedureManifestStatus("1.4.0", None, None, None)))
    assert answer["manifest_state"] == "unavailable"
    assert answer["fingerprint"] is None


async def test_under_the_published_default_token_nothing_is_stored_and_the_card_says_so(registered):
    status = ProcedureManifestStatus("1.4.0", None, None, None)
    answer = await _answer(registered(status, registration_state="published_default"))
    assert answer["manifest_state"] == "not_accepted_default_token"
    assert answer["fingerprint"] is None
    assert answer["hidden_tools"] == [] and answer["limited_tools"] == []


async def test_a_manifest_that_still_decides_is_current_whatever_the_token(registered):
    status = ProcedureManifestStatus("1.4.0", FINGERPRINT, _manifest(), RECEIVED)
    answer = await _answer(registered(status, registration_state="published_default"))
    assert answer["manifest_state"] == "current"


async def test_a_reported_version_longer_than_the_card_shows_is_cut(registered):
    answer = await _answer(registered(ProcedureManifestStatus("v" * 200, None, None, None)))
    assert answer["reported_version"] == "v" * 64


async def test_a_local_build_reports_an_empty_version(registered):
    answer = await _answer(registered(ProcedureManifestStatus("", None, None, None)))
    assert answer["reported_version"] == ""
