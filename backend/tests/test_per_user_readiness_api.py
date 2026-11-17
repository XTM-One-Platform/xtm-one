"""Coverage for ``GET /integrations/per-user-readiness``.

The agent config UI cannot tell "a company-wide fallback exists" from "there is
none" out of a flat integrations list — both look like one connected instance of
the provider. This endpoint draws that line, and additionally flags the shape
behind the original incident: a *personal* instance group-shared to the team
while the connector has no company-managed instance at all. It looks like the
shared one in the list but per-user resolution never uses it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import integrations as integrations_api
from app.core.database import get_db
from app.models.integration import Integration

ACTING_ID = uuid.uuid4()
COLLEAGUE_ID = uuid.uuid4()
ADMIN_ID = uuid.uuid4()


@dataclass
class _Result:
    rows: list

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)


class _FakeDb:
    """Returns every seeded integration for the visibility query.

    The endpoint's own classification is what's under test, so the stub stands
    in for ``user_visible_query`` (own + group-shared + company-managed) by
    handing back exactly the rows a test declared as visible.
    """

    def __init__(self, integrations: list[Integration]):
        self.integrations = integrations

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0].get("entity")
        if entity is Integration:
            return _Result(rows=self.integrations)
        return _Result(rows=[])


def _integ(
    *,
    provider: str,
    user_id: uuid.UUID,
    is_company_managed: bool = False,
    auth_type: str = "oauth",
    oauth_share_acknowledged: bool = False,
    shared_group_ids: list[str] | None = None,
    status: str = "connected",
) -> Integration:
    return Integration(
        id=uuid.uuid4(),
        provider=provider,
        user_id=user_id,
        is_company_managed=is_company_managed,
        auth_type=auth_type,
        oauth_share_acknowledged=oauth_share_acknowledged,
        shared_group_ids=shared_group_ids,
        status=status,
        name=f"{provider}-{'company' if is_company_managed else 'personal'}",
    )


def _client(integrations: list[Integration], monkeypatch):
    fake_db = _FakeDb(integrations)

    async def _get_db():
        yield fake_db

    async def _group_ids(*_a, **_k):
        return []

    async def _owner_names(_db, rows):
        names = {str(COLLEAGUE_ID): "Eric Ford", str(ADMIN_ID): "Admin"}
        return {k: v for k, v in names.items() if any(str(r.user_id) == k for r in rows)}

    monkeypatch.setattr(integrations_api, "get_user_group_ids", _group_ids)
    monkeypatch.setattr(integrations_api, "resolve_owner_names", _owner_names)

    app = FastAPI()
    app.include_router(integrations_api.router, prefix="/api/v1/integrations")
    app.dependency_overrides[deps.get_current_user] = lambda: SimpleNamespace(
        id=ACTING_ID, role="user"
    )
    app.dependency_overrides[get_db] = _get_db
    return TestClient(app)


def _get(client) -> dict[str, dict]:
    resp = client.get("/api/v1/integrations/per-user-readiness")
    assert resp.status_code == 200, resp.text
    return {p["provider"]: p for p in resp.json()["providers"]}


def test_company_managed_instance_is_reported_as_a_fallback(monkeypatch):
    client = _client(
        [_integ(provider="notion", user_id=ADMIN_ID, is_company_managed=True,
                auth_type="api_key")],
        monkeypatch,
    )
    entry = _get(client)["notion"]
    assert entry["has_company_wide_fallback"] is True
    assert entry["company_wide_instance_count"] == 1
    assert entry["user_has_own_instance"] is False
    assert entry["resolvable_for_user"] is True
    assert entry["de_facto_shared_owners"] == []


def test_colleagues_shared_personal_instance_is_not_a_fallback(monkeypatch):
    """The incident: the only Microsoft instance is a colleague's personal
    account, group-shared. Per-user mode resolves to nothing, and the owner is
    named so an admin can promote a real company-managed instance."""
    client = _client(
        [_integ(provider="microsoft", user_id=COLLEAGUE_ID,
                shared_group_ids=[str(uuid.uuid4())],
                oauth_share_acknowledged=True)],
        monkeypatch,
    )
    entry = _get(client)["microsoft"]
    assert entry["has_company_wide_fallback"] is False
    assert entry["resolvable_for_user"] is False
    assert entry["user_has_own_instance"] is False
    assert entry["de_facto_shared_owners"] == ["Eric Ford"]


def test_own_instance_makes_the_provider_resolvable_without_a_fallback(monkeypatch):
    client = _client([_integ(provider="microsoft", user_id=ACTING_ID)], monkeypatch)
    entry = _get(client)["microsoft"]
    assert entry["user_has_own_instance"] is True
    assert entry["has_company_wide_fallback"] is False
    assert entry["resolvable_for_user"] is True


def test_unacknowledged_shared_oauth_is_not_counted_as_a_fallback(monkeypatch):
    """A shared OAuth row without the owner's acknowledgement has had its token
    stripped, so it is not usable by anyone but its owner."""
    client = _client(
        [_integ(provider="google", user_id=ADMIN_ID, is_company_managed=True,
                auth_type="oauth", oauth_share_acknowledged=False)],
        monkeypatch,
    )
    entry = _get(client)["google"]
    assert entry["has_company_wide_fallback"] is False
    assert entry["company_wide_instance_count"] == 0


def test_platform_managed_counts_as_a_company_wide_fallback(monkeypatch):
    """Its credentials are minted per acting user, so any user resolves it."""
    client = _client(
        [_integ(provider="opencti", user_id=ADMIN_ID, auth_type="platform_managed")],
        monkeypatch,
    )
    entry = _get(client)["opencti"]
    assert entry["has_company_wide_fallback"] is True
    assert entry["resolvable_for_user"] is True


def test_de_facto_owners_suppressed_once_a_company_instance_exists(monkeypatch):
    """With a real company-managed instance, a colleague's shared personal row
    is just an extra personal instance — not a mislabelled shared one."""
    group = [str(uuid.uuid4())]
    client = _client(
        [
            _integ(provider="hubspot", user_id=ADMIN_ID, is_company_managed=True,
                   auth_type="api_key"),
            _integ(provider="hubspot", user_id=COLLEAGUE_ID, shared_group_ids=group),
        ],
        monkeypatch,
    )
    entry = _get(client)["hubspot"]
    assert entry["has_company_wide_fallback"] is True
    assert entry["de_facto_shared_owners"] == []


def test_disconnected_instances_are_ignored(monkeypatch):
    client = _client(
        [_integ(provider="notion", user_id=ADMIN_ID, is_company_managed=True,
                auth_type="api_key", status="disconnected")],
        monkeypatch,
    )
    assert _get(client) == {}


@pytest.mark.parametrize("provider", ["microsoft", "notion"])
def test_every_visible_provider_is_reported(monkeypatch, provider):
    """Question 4 of the issue — "are other connectors in the same state?" — is
    answered by the endpoint covering every provider the user can see."""
    client = _client(
        [
            _integ(provider="microsoft", user_id=COLLEAGUE_ID,
                   shared_group_ids=[str(uuid.uuid4())], oauth_share_acknowledged=True),
            _integ(provider="notion", user_id=ADMIN_ID, is_company_managed=True,
                   auth_type="api_key"),
        ],
        monkeypatch,
    )
    assert provider in _get(client)



def test_openapi_integrations_have_no_per_user_readiness(monkeypatch):
    """Per-user resolution never picks an OpenAPI integration (each one is a
    different API), so the endpoint reports none for that provider."""
    client = _client(
        [
            _integ(provider="openapi", user_id=ACTING_ID, auth_type="api_key"),
            _integ(provider="openapi", user_id=ADMIN_ID, is_company_managed=True, auth_type="api_key"),
            _integ(provider="notion", user_id=ACTING_ID, auth_type="api_key"),
        ],
        monkeypatch,
    )
    assert set(_get(client)) == {"notion"}
