"""Regression tests for the variables company-managed admin gate.

``update_variable`` previously called
``require_admin_for_company_managed(user, body.is_company_managed)`` with the
arguments reversed: the helper reads ``obj.is_company_managed`` via
``getattr(obj, "is_company_managed", False)``, so passing the ``User`` as the
object always resolved to ``False`` and the admin gate never fired. A
non-admin could therefore promote their own variable to company-managed and
expose it to every user. These tests pin the corrected call shape
(``require_admin_for_company_managed(body, user, ...)``).
"""

from __future__ import annotations

import datetime
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.v1 import variables as variables_api
from app.api.v1.variables import VariableUpdate


def _make_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/api/v1/variables/x",
            "headers": [],
        }
    )


def _make_variable(*, user_id: uuid.UUID, is_company_managed: bool = False):
    now = datetime.datetime(2026, 5, 29, tzinfo=datetime.UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="MY_VAR",
        value="v",
        value_enc=None,
        is_secret=False,
        description=None,
        tags=[],
        user_id=user_id,
        is_company_managed=is_company_managed,
        shared_group_ids=None,
        created_at=now,
        updated_at=now,
    )


class _FakeResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _FakeDB:
    async def commit(self):
        return None

    async def refresh(self, _obj):
        return None

    async def execute(self, *_args, **_kwargs):
        # ``update_variable`` resolves the caller's group memberships via
        # ``get_user_group_ids``; return an empty membership set.
        return _FakeResult()


def _setup(monkeypatch, var) -> None:
    async def _get_var(_db, _user, _vid):
        return var

    async def _log_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(variables_api, "_get_user_variable", _get_var)
    monkeypatch.setattr(variables_api, "log_audit", _log_audit)


@pytest.mark.asyncio
async def test_update_variable_non_admin_cannot_promote_to_company_managed(monkeypatch):
    """A non-admin promoting their own (private) variable to company-managed
    must be rejected with 403 and the flag must stay unchanged."""
    user_id = uuid.uuid4()
    var = _make_variable(user_id=user_id, is_company_managed=False)
    _setup(monkeypatch, var)
    user = SimpleNamespace(id=user_id, is_admin=False, role="user")
    body = VariableUpdate(is_company_managed=True)

    with pytest.raises(HTTPException) as exc:
        await variables_api.update_variable(
            str(var.id), body, _make_request(), user, _FakeDB()
        )
    assert exc.value.status_code == 403
    assert var.is_company_managed is False


@pytest.mark.asyncio
async def test_update_variable_admin_can_promote_to_company_managed(monkeypatch):
    """An admin can still promote a variable to company-managed — the gate
    only blocks non-admins, so the legitimate path keeps working."""
    user_id = uuid.uuid4()
    var = _make_variable(user_id=user_id, is_company_managed=False)
    _setup(monkeypatch, var)
    user = SimpleNamespace(id=user_id, is_admin=True, role="admin")
    body = VariableUpdate(is_company_managed=True)

    resp = await variables_api.update_variable(
        str(var.id), body, _make_request(), user, _FakeDB()
    )
    assert var.is_company_managed is True
    assert resp.is_company_managed is True
