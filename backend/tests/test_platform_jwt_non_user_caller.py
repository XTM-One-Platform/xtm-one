"""A cross-platform JWT is minted for a user only, never for a non-user caller.

``/mcp/global`` authenticated with the platform key acts as
``mcp_endpoint.GLOBAL_KEY_CALLER``, no account: the target platform would
match the token's email to one of its users, and there is no such user to
name. The refusal is explicit, before any lookup, not the side effect of a
UUID parse raising inside the caller's ``try``.
"""

from __future__ import annotations

import uuid

import pytest

from app.api.v1.mcp_endpoint import GLOBAL_KEY_CALLER
from app.services import integration_tools


class _NoLookupDB:
    async def execute(self, _stmt):  # pragma: no cover - the refusal comes first
        raise AssertionError("a non-user caller must be refused before any lookup")


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", [GLOBAL_KEY_CALLER, "anonymous", ""])
async def test_a_non_user_caller_gets_no_platform_jwt(monkeypatch, caller):
    def _never(**_kwargs):  # pragma: no cover - the refusal comes first
        raise AssertionError("no token may be signed for a non-user caller")

    monkeypatch.setattr("app.core.xtm_auth.issue_xtm_jwt", _never)

    assert await integration_tools._generate_platform_jwt(_NoLookupDB(), caller, "https://opencti.example") is None


class _Row:
    def __init__(self, email):
        self._email = email

    def one_or_none(self):
        return (self._email,)


class _OneEmailDB:
    async def execute(self, _stmt):
        return _Row("analyst@example.com")


@pytest.mark.asyncio
async def test_a_user_caller_still_gets_one(monkeypatch):
    signed: list[dict] = []

    def _issue(**kwargs):
        signed.append(kwargs)
        return "token"

    monkeypatch.setattr("app.core.xtm_auth.issue_xtm_jwt", _issue)
    user_id = uuid.uuid4()

    assert await integration_tools._generate_platform_jwt(_OneEmailDB(), str(user_id), "https://opencti.example") == "token"
    assert signed == [
        {"user_id": user_id, "user_email": "analyst@example.com", "audience_url": "https://opencti.example"}
    ]
