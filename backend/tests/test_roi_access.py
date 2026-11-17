"""Who reads a fleet's ROI and reviews its runs (issue #9350, decision D1 of ``dev-docs/roi-outcomes.md``).

Pure checks of :mod:`app.services.roi_access` on a fake session, of
:func:`roi_records.may_review` with ``fleet_reader``, and of the ROI read
routes' dependencies: a non-administrator reads a fleet's scope only.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.deps import get_current_user, require_admin
from app.api.v1 import roi as roi_api
from app.services import roi_access, roi_records
from app.services.group_managers import ManagedGroup

OWNER = uuid.uuid4()
GRANTEE = uuid.uuid4()
MANAGER = uuid.uuid4()
STRANGER = uuid.uuid4()
GROUP = uuid.uuid4()
WORKSPACE = uuid.uuid4()
FLEET = uuid.uuid4()


def _fleet(**kw):
    base = {
        "id": FLEET,
        "user_id": OWNER,
        "workspace_id": WORKSPACE,
        "is_company_managed": False,
        "shared_user_ids": [str(GRANTEE)],
        "shared_user_permissions": {str(GRANTEE): "config"},
        "shared_group_ids": None,
        "shared_group_permissions": None,
    }
    return SimpleNamespace(**{**base, **kw})


class _FakeDB:
    def __init__(self, fleet=None, workspace=None):
        self.rows = {}
        if fleet is not None:
            self.rows[("Fleet", fleet.id)] = fleet
        if workspace is not None:
            self.rows[("Workspace", workspace.id)] = workspace

    async def get(self, model, key):
        return self.rows.get((model.__name__, key))


@pytest.fixture
def _no_groups(monkeypatch):
    async def _groups(_db, _user_id):
        return []

    async def _managed(_db, user_id):
        return [ManagedGroup(id=str(GROUP), name="Team")] if user_id == MANAGER else []

    import app.api.v1.helpers as helpers
    import app.services.group_managers as group_managers

    monkeypatch.setattr(helpers, "get_user_group_ids", _groups)
    monkeypatch.setattr(group_managers, "managed_groups", _managed)


async def _reads(db, user_id, *, is_admin=False, fleet_id=FLEET):
    return await roi_access.fleet_roi_reader(db, user_id=user_id, is_admin=is_admin, fleet_id=fleet_id)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_groups")
async def test_an_administrator_reads_every_fleet_even_an_absent_one():
    assert await _reads(_FakeDB(), STRANGER, is_admin=True)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_groups")
async def test_the_owner_and_a_config_grantee_read_the_fleet():
    db = _FakeDB(_fleet(), SimpleNamespace(id=WORKSPACE, shared_group_ids=None))
    assert await _reads(db, OWNER)
    assert await _reads(db, GRANTEE)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_groups")
async def test_a_group_manager_of_the_fleet_workspace_reads_the_fleet():
    db = _FakeDB(_fleet(), SimpleNamespace(id=WORKSPACE, shared_group_ids=[str(GROUP)]))
    assert await _reads(db, MANAGER)
    # The same manager, the workspace not shared with their group: no.
    other = _FakeDB(_fleet(), SimpleNamespace(id=WORKSPACE, shared_group_ids=[str(uuid.uuid4())]))
    assert not await _reads(other, MANAGER)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_groups")
async def test_nobody_else_reads():
    db = _FakeDB(_fleet(), SimpleNamespace(id=WORKSPACE, shared_group_ids=[str(GROUP)]))
    assert not await _reads(db, STRANGER)
    assert not await _reads(db, None)
    assert not await _reads(db, OWNER, fleet_id=None)
    assert not await _reads(_FakeDB(), OWNER)
    # A company-managed fleet is managed by administrators only.
    company = _FakeDB(_fleet(is_company_managed=True), SimpleNamespace(id=WORKSPACE, shared_group_ids=None))
    assert not await _reads(company, OWNER)


@pytest.mark.asyncio
async def test_a_failed_read_refuses_and_never_raises():
    class _Broken:
        async def get(self, *_a):
            raise RuntimeError("database gone")

    assert not await _reads(_Broken(), OWNER)


def test_shares_a_managed_group():
    assert roi_access.shares_a_managed_group([str(GROUP)], {str(GROUP)})
    assert not roi_access.shares_a_managed_group(None, {str(GROUP)})
    assert not roi_access.shares_a_managed_group([str(GROUP)], set())


def test_a_fleet_reader_reviews_an_unreviewed_record_but_never_another_reviewer_s():
    row = SimpleNamespace(reviewed_by_user_id=None)
    assert roi_records.may_review(row, user_id=MANAGER, is_admin=False, subject_owner_id=OWNER, fleet_reader=True)
    assert not roi_records.may_review(row, user_id=MANAGER, is_admin=False, subject_owner_id=OWNER)
    assert not roi_records.may_review(
        row, user_id=MANAGER, is_admin=False, subject_owner_id=OWNER, fleet_reader=True, company_managed=True
    )
    reviewed = SimpleNamespace(reviewed_by_user_id=OWNER)
    assert not roi_records.may_review(
        reviewed, user_id=MANAGER, is_admin=False, subject_owner_id=OWNER, fleet_reader=True
    )


@pytest.mark.parametrize("route", ["get_roi_report", "export_roi_report", "list_roi_records", "count_roi_records"])
def test_the_read_routes_take_any_user_and_check_the_rule(route):
    fn = getattr(roi_api, route)
    deps = [p.default.dependency for p in inspect.signature(fn).parameters.values() if hasattr(p.default, "dependency")]
    assert get_current_user in deps and require_admin not in deps
    assert "_require_roi_reader" in inspect.getsource(fn)


@pytest.mark.asyncio
async def test_without_a_fleet_a_non_administrator_is_refused(monkeypatch):
    async def _never(*_a, **_kw):
        raise AssertionError("no fleet, no fleet read")

    monkeypatch.setattr(roi_access, "fleet_roi_reader", _never)
    user = SimpleNamespace(id=OWNER, is_admin=False)
    with pytest.raises(HTTPException) as refused:
        await roi_api._require_roi_reader(None, user, fleet_id=None)
    assert refused.value.status_code == 403


@pytest.mark.asyncio
async def test_a_fleet_scope_asks_the_rule(monkeypatch):
    async def _reader(_db, *, user_id, is_admin, fleet_id):
        return user_id == OWNER and fleet_id == FLEET

    monkeypatch.setattr(roi_access, "fleet_roi_reader", _reader)
    await roi_api._require_roi_reader(None, SimpleNamespace(id=OWNER, is_admin=False), fleet_id=FLEET)
    with pytest.raises(HTTPException):
        await roi_api._require_roi_reader(None, SimpleNamespace(id=STRANGER, is_admin=False), fleet_id=FLEET)
    await roi_api._require_roi_reader(None, SimpleNamespace(id=STRANGER, is_admin=True), fleet_id=None)


AGENT = uuid.uuid4()


@pytest.mark.asyncio
async def test_an_agent_scope_asks_the_agent_rule(monkeypatch):
    """A non-administrator who sees the agent reads its ROI (#9807, UX #9810 r2 B2); a stranger does not."""

    async def _never(*_a, **_kw):
        raise AssertionError("an agent scope never asks the fleet rule")

    async def _reader(_db, *, user_id, is_admin, agent_id):
        return user_id == GRANTEE and agent_id == AGENT

    monkeypatch.setattr(roi_access, "fleet_roi_reader", _never)
    monkeypatch.setattr(roi_access, "agent_roi_reader", _reader)
    await roi_api._require_roi_reader(None, SimpleNamespace(id=GRANTEE, is_admin=False), fleet_id=None, agent_id=AGENT)
    with pytest.raises(HTTPException) as refused:
        await roi_api._require_roi_reader(
            None, SimpleNamespace(id=STRANGER, is_admin=False), fleet_id=None, agent_id=AGENT
        )
    assert refused.value.status_code == 403


@pytest.mark.asyncio
async def test_the_report_route_passes_the_agent_of_an_agent_scope(monkeypatch):
    seen = {}

    async def _require(_db, _user, *, fleet_id, agent_id=None, fleet_viewers=False):
        seen.update(fleet_id=fleet_id, agent_id=agent_id)

    async def _load(*_a, **_kw):
        return object()

    monkeypatch.setattr(roi_api, "_require_roi_reader", _require)
    monkeypatch.setattr(roi_api.roi_report, "load_report", _load)
    monkeypatch.setattr(roi_api.roi_report, "report_dict", lambda _r: {"ok": True})

    async def _same(answer: dict[str, object], *_a: object, **_kw: object) -> dict[str, object]:
        return answer

    monkeypatch.setattr(roi_api, "_with_ledger_figures", _same)
    user = SimpleNamespace(id=GRANTEE, is_admin=False)
    assert await roi_api.get_roi_report(window="7d", currency=None, scope="agent", id=AGENT, db=None, user=user) == {
        "ok": True
    }
    assert seen == {"fleet_id": None, "agent_id": AGENT}


class _AgentDB:
    def __init__(self, found):
        self.found = found

    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self.found)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_groups")
async def test_agent_roi_reader_follows_the_agent_visibility():
    assert await roi_access.agent_roi_reader(_AgentDB(AGENT), user_id=GRANTEE, is_admin=False, agent_id=AGENT)
    assert not await roi_access.agent_roi_reader(_AgentDB(None), user_id=STRANGER, is_admin=False, agent_id=AGENT)
    assert not await roi_access.agent_roi_reader(_AgentDB(AGENT), user_id=None, is_admin=False, agent_id=AGENT)
    assert await roi_access.agent_roi_reader(_AgentDB(None), user_id=None, is_admin=True, agent_id=None)


@pytest.mark.asyncio
async def test_agent_roi_reader_refuses_and_never_raises_on_a_failed_read():
    class _Broken:
        async def execute(self, *_a):
            raise RuntimeError("database gone")

    assert not await roi_access.agent_roi_reader(_Broken(), user_id=GRANTEE, is_admin=False, agent_id=AGENT)
