"""The integration-only exposure routes.

``app.services.integration_exposure`` answers ``/integrations/{id}/exposure``,
``/integrations/exposure/summary`` and ``/integrations/exposure/preview`` with
the one exposure engine's answer (``app.services.resource_exposure``), renamed.
These tests pin the renaming and the routes' permissions; what the engine
answers is pinned by ``test_resource_exposure.py`` and, against PostgreSQL,
``test_resource_exposure_postgres.py`` (which also compares both routes).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import integration_exposure as routes
from app.services import integration_exposure as adapter

ALICE = uuid.uuid4()
BOB = uuid.uuid4()


def _audience(**fields):
    base = {
        "everyone": False,
        "people_count": 0,
        "people": [],
        "groups": [],
        "public_link": False,
        "anonymous_channels": [],
        "mcp_key_holders": False,
        "a2a_partners": False,
        "webhook_callers": False,
    }
    base.update(fields)
    return base


def _secret(**fields):
    base = {"kind": "api_key", "holder_id": str(ALICE), "is_company": False, "account_shared": True, "scopes": []}
    base.update(fields)
    return base


def test_the_exposure_keeps_the_route_s_names():
    integration = SimpleNamespace(id=uuid.uuid4(), auth_type="oauth")
    result = {
        "resource": {"kind": "integration"},
        "secret": _secret(kind="oauth_account", holder_name="Alice"),
        "direct": _audience(people_count=1),
        "paths": [
            {"kind": "people", "reasons": [], "usable": True, "audience": _audience(people_count=1)},
            {"kind": "agent", "reasons": ["binding"], "usable": True, "audience": _audience(a2a_partners=True)},
        ],
        "reached": _audience(people_count=1, a2a_partners=True),
        "truncated": False,
    }
    answer = adapter.integration_answer(result, integration)
    assert answer["integration_id"] == str(integration.id)
    assert answer["credential"]["auth_type"] == "oauth" and answer["credential"]["holder_name"] == "Alice"
    assert [p["credential_usable"] for p in answer["paths"]] == [True, True]
    assert all("usable" not in p for p in answer["paths"])
    # The engine's audiences, A2A partners included.
    assert answer["paths"][1]["audience"]["a2a_partners"] is True


def test_the_preview_lists_the_integrations_a_change_lends():
    via = {"type": "agent", "id": str(uuid.uuid4()), "name": "Helper"}
    lent = {
        "resource": {"kind": "integration", "id": "i", "name": "GitHub", "provider": "github", "auth_type": "api_key"},
        "secret": _secret(holder_name="Alice", is_yours=True),
        "via": via,
        "gained": _audience(people_count=2),
        "lost": _audience(),
        "notes": [],
        "opt_outs": ["per_user_binding", "company_resource"],
    }
    taken = {**lent, "gained": _audience(), "lost": _audience(people_count=1)}
    server = {**lent, "resource": {**lent["resource"], "kind": "mcp_server"}}
    answer = adapter.preview_answer({"items": [lent, taken, server]})
    assert answer["requires_confirmation"] is True
    [item] = answer["credentials"]
    assert item["integration"] == {"id": "i", "name": "GitHub", "provider": "github", "auth_type": "api_key"}
    assert item["reached"]["people_count"] == 2 and item["via"] == via
    assert item["credential"]["is_yours"] is True and item["credential"]["auth_type"] == "api_key"
    assert item["opt_outs"] == ["per_user_binding", "company_integration"]
    assert adapter.preview_answer({"items": [taken]}) == {
        "credentials": [],
        "requires_confirmation": False,
        "truncated": False,
    }
    # A cut lookup may hide what the change lends: the change still asks.
    assert adapter.preview_answer({"items": [taken], "truncated": True}) == {
        "credentials": [],
        "requires_confirmation": True,
        "truncated": True,
    }


@pytest.mark.parametrize(
    ("entity_type", "change", "expected"),
    [
        ("agent", {"integration_ids": ["a"]}, {"integration_bindings": {"preview": ["a"]}}),
        ("assignment", {"integration_ids": []}, {"integration_bindings": {"preview": []}}),
        ("knowledge_base", {"integration_ids": ["a"]}, {"sync_source_integration_ids": ["a"]}),
        ("skill", {"integration_ids": ["a"]}, {"source_integration_id": "a"}),
        ("skill", {"integration_ids": []}, {"source_integration_id": None}),
        ("integration", {"integration_ids": ["a"], "shared_group_ids": ["g"]}, {"shared_group_ids": ["g"]}),
        ("integration", {"withhold_connected_account": True}, {"keep_oauth_credentials_on_share": False}),
        ("integration", {"withhold_connected_account": False}, {"keep_oauth_credentials_on_share": True}),
        # No choice made: the REST default of decision 15c decides.
        ("integration", {"withhold_connected_account": None}, {}),
        ("agent", {"web_enabled": True, "new_owner_id": "u"}, {"web_enabled": True, "new_owner_id": "u"}),
        (
            "flow",
            {"integration_ids": ["a"]},
            {"graph": {"nodes": [{"id": "integration-0", "kind": "integration", "config": {"integration_id": "a"}}]}},
        ),
        ("flow", {"integration_ids": ["a"], "graph": {"nodes": []}}, {"graph": {"nodes": []}}),
    ],
)
def test_the_route_s_fields_are_the_engine_s_change(entity_type, change, expected):
    assert adapter.generic_change(entity_type, change) == expected


async def test_summaries_keep_the_route_s_names(monkeypatch):
    async def _summaries(_db, kind, rows):
        assert kind == "integration"
        return (
            {
                "x": {
                    "id": "x",
                    "kind": "integration",
                    "secret_kind": "oauth_account",
                    "is_company": False,
                    "account_shared": False,
                    "ways": 2,
                    "counts": {"group": 1, "agent": 1},
                    "public_link": True,
                }
            },
            True,
        )

    monkeypatch.setattr(adapter.engine, "exposure_summaries", _summaries)
    summaries, truncated = await adapter.exposure_summaries(None, [])  # type: ignore[arg-type]
    assert truncated is True
    assert summaries["x"] == {
        "integration_id": "x",
        "credential_kind": "oauth_account",
        "is_company": False,
        "account_shared": False,
        "ways": 2,
        "counts": {"group": 1, "agent": 1},
        "public_link": True,
    }


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


async def test_the_exposure_is_for_the_integration_s_managers(monkeypatch):
    pat = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=ALICE,
        is_company_managed=False,
        shared_group_ids=None,
        shared_user_ids=[str(BOB)],
        shared_user_permissions=None,
        shared_group_permissions=None,
        is_builtin=False,
    )

    async def _groups(_db, _uid):
        return []

    async def _load(_db, _uid, _iid, _groups, *, is_admin):
        return pat

    async def _exposure(_db, integration, *, viewer_id, viewer_is_admin):
        return {"integration_id": str(integration.id), "viewer": viewer_id}

    monkeypatch.setattr(routes, "get_user_group_ids", _groups)
    monkeypatch.setattr(routes, "get_user_integration", _load)
    monkeypatch.setattr(routes, "integration_exposure", _exposure)
    bob = SimpleNamespace(id=BOB, role="user", is_admin=False)
    with pytest.raises(HTTPException) as refused:
        await routes.get_integration_exposure(str(pat.id), user=bob, db=None)
    assert refused.value.status_code == 403
    alice = SimpleNamespace(id=ALICE, role="user", is_admin=False)
    answer = await routes.get_integration_exposure(str(pat.id), user=alice, db=None)
    assert answer == {"integration_id": str(pat.id), "viewer": ALICE}


async def test_a_malformed_id_is_answered_like_an_unknown_one():
    with pytest.raises(HTTPException) as missing:
        await routes.get_integration_exposure("not-a-uuid", user=SimpleNamespace(id=ALICE, is_admin=False), db=None)
    assert missing.value.status_code == 404


async def test_only_admins_preview_the_general_assistant():
    body = routes.ExposurePreviewRequest(entity_type="general_assistant", integration_ids=[])
    with pytest.raises(HTTPException) as refused:
        await routes.post_exposure_preview(body, user=SimpleNamespace(id=ALICE, is_admin=False), db=None)
    assert refused.value.status_code == 403


def test_an_omitted_sharing_list_in_a_preview_means_cleared():
    body = routes.ExposurePreviewRequest(entity_type="agent", entity_id=str(uuid.uuid4()), shared_group_ids=None)
    assert routes._change(body) == {"shared_group_ids": []}
