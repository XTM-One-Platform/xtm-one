"""An agent links a tracked organization to its OpenCTI Organization (x12-10, #1801).

``link_tracked_organization_to_opencti`` runs the Settings > Organization link
service (``organization_opencti.link_identity``) for the administrator the
agent acts for. An id OpenCTI does not read outside a draft is refused with a
message naming the draft and nothing is stored; ``get_organization_context``
tracked rows show the stored link.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.services import organization_context_categories as cats
from app.services import organization_opencti
from app.services import organizations as orgs
from app.services.builtin_tools import build_organization_context_write_tools
from app.services.builtin_tools import organization_context_write as module
from app.services.organization_context_agent_tools import OPENCTI_LINK_TOOL

_ADMIN_ID = "00000000-0000-4000-8000-000000001801"


class _Session:
    def __init__(self, *, admin: bool = True):
        self.admin, self.commits = admin, 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, _model, _id):
        return SimpleNamespace(id=_id, is_admin=self.admin)

    async def commit(self):
        self.commits += 1


def _tracked():
    return SimpleNamespace(
        id="org-1", name="Acme", role="tracked", relationship="supplier", opencti_organization_id=None
    )


def _link_tool(monkeypatch, session, target, organizations: dict | None = None):
    from app.core import database

    async def _resolve(db, ref):
        return target

    async def _gql(query, variables=None):
        node = (organizations or {}).get((variables or {}).get("id"))
        return {"organization": node}, None

    async def _opencti_gql(db, user_id):
        return _gql

    monkeypatch.setattr(database, "async_session", lambda: session)
    monkeypatch.setattr(orgs, "resolve", _resolve)
    monkeypatch.setattr(organization_opencti, "opencti_gql", _opencti_gql)
    _, executor = build_organization_context_write_tools(user_id=_ADMIN_ID, caller_is_admin=True, agent_name="Ops")
    return executor.handler(OPENCTI_LINK_TOOL)


@pytest.mark.asyncio
async def test_an_existing_opencti_organization_is_linked(monkeypatch):
    session, target = _Session(), _tracked()
    link = _link_tool(
        monkeypatch,
        session,
        target,
        {"cti-1": {"id": "cti-1", "name": "Acme", "x_opencti_organization_type": "vendor"}},
    )

    answer = json.loads(await link(organization="Acme", identity_id="cti-1"))

    assert answer["opencti_organization_id"] == "cti-1"
    assert target.opencti_organization_id == "cti-1"
    assert session.commits == 1


@pytest.mark.asyncio
async def test_an_id_only_in_an_opencti_draft_is_refused_and_not_stored(monkeypatch):
    session, target = _Session(), _tracked()
    link = _link_tool(monkeypatch, session, target, {})

    answer = await link(organization="Acme", identity_id="draft-only")

    assert answer.startswith("Not linked:")
    assert "draft" in answer
    assert target.opencti_organization_id is None
    assert session.commits == 0


@pytest.mark.asyncio
async def test_no_id_and_no_create_is_refused_before_any_read(monkeypatch):
    link = _link_tool(monkeypatch, _Session(), _tracked())

    assert (await link(organization="Acme")).startswith("Not linked:")


@pytest.mark.asyncio
async def test_a_non_administrator_links_nothing(monkeypatch):
    target = _tracked()
    link = _link_tool(monkeypatch, _Session(admin=False), target, {"cti-1": {"id": "cti-1"}})

    assert await link(organization="Acme", identity_id="cti-1") == module.REFUSAL
    assert target.opencti_organization_id is None


@pytest.mark.asyncio
async def test_the_own_organization_is_not_linked(monkeypatch):
    target = SimpleNamespace(id="org-0", name="Us", role="primary", relationship=None, opencti_organization_id=None)
    link = _link_tool(monkeypatch, _Session(), target, {"cti-1": {"id": "cti-1"}})

    assert (await link(organization="Us", identity_id="cti-1")).startswith("Not linked: no tracked organization")


def test_the_tracked_rows_carry_the_opencti_link():
    rows = cats.tracked_rows(
        [
            SimpleNamespace(
                name="Acme",
                id="org-1",
                role="tracked",
                relationship="supplier",
                criticality="high",
                monitoring="on",
                opencti_organization_id="cti-1",
            )
        ]
    )

    assert rows[0]["opencti_organization_id"] == "cti-1"
