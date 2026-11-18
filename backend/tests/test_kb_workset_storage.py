"""``GET /knowledge-bases/{id}`` on a workset's internal knowledge base.

A workset keeps its documents in an ``is_internal`` knowledge base that every
read hides, admins included. An old link or an audit entry to it used to answer
a bare 404, which the detail page sent to its error boundary ("Failed to load
this section"). The 404 now names the workset when the caller may read it, so
the page says what the id is and where to go.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import knowledge_bases


class _Scalars:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Result:
    def __init__(self, row):
        self._row = row

    def scalars(self):
        return _Scalars(self._row)


class _Session:
    def __init__(self, row):
        self._row = row
        self.statements: list = []

    async def execute(self, statement, *a, **k):
        self.statements.append(statement)
        return _Result(self._row)


def _workset():
    return SimpleNamespace(
        id=uuid.uuid4(),
        key="agent-0dd45539-0000-0000-0000-000000000000",
        label=None,
        user_id=uuid.uuid4(),
        is_company_managed=False,
    )


def _user(*, admin=False, user_id=None):
    return SimpleNamespace(id=user_id or uuid.uuid4(), is_admin=admin)


def _storage(session, user):
    return asyncio.run(knowledge_bases._workset_storage_of(session, user, uuid.uuid4()))


def test_an_admin_is_told_which_workset_the_id_stores():
    workset = _workset()
    detail = _storage(_Session(workset), _user(admin=True))
    assert detail is not None
    assert detail["code"] == knowledge_bases.WORKSET_STORAGE_CODE == "workset_storage"
    assert detail["workset_id"] == str(workset.id)
    assert detail["workset_name"] == workset.key


def test_another_person_s_personal_workset_stays_a_plain_not_found():
    assert _storage(_Session(_workset()), _user()) is None


def test_the_owner_and_a_company_managed_workset_are_named_too():
    owner = _user()
    own = _workset()
    own.user_id = owner.id
    own.label = "Weekly reports"
    assert _storage(_Session(own), owner)["workset_name"] == "Weekly reports"
    shared = _workset()
    shared.is_company_managed = True
    assert _storage(_Session(shared), _user()) is not None


def test_a_malformed_id_is_the_unknown_id_s_404_and_reads_nothing():
    session = _Session(_workset())
    with pytest.raises(HTTPException) as caught:
        asyncio.run(knowledge_bases.get_knowledge_base("not-a-uuid", user=_user(admin=True), db=session))
    assert caught.value.status_code == 404
    assert caught.value.detail == "Knowledge base not found"
    assert session.statements == []


def test_an_id_no_workset_stores_stays_a_plain_not_found():
    assert _storage(_Session(None), _user(admin=True)) is None


def test_the_route_answers_the_named_404(monkeypatch):
    workset = _workset()

    async def _hidden(*a, **k):
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    async def _groups(*a, **k):
        return []

    monkeypatch.setattr(knowledge_bases, "get_user_knowledge_base", _hidden)
    monkeypatch.setattr(knowledge_bases, "get_user_group_ids", _groups)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(knowledge_bases.get_knowledge_base(str(uuid.uuid4()), user=_user(admin=True), db=_Session(workset)))
    assert caught.value.status_code == 404
    assert caught.value.detail["code"] == "workset_storage"
