"""A cross-platform JWT acts as the XTM One account holding its email, admins included.

Registering a platform delegates to it the assertion of who its user is, by
email (``dev-docs/cross-platform-identity.md``, "Who an incoming token acts
as"): the account keeps its own role, never capped below admin. This pins
that rule so a change to it is a decision, not an accident.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import jwt as _jwt
import pytest

from app.api import deps as deps_module


class _OneUserDB:
    def __init__(self, user):
        self._user = user

    async def execute(self, _stmt):
        user = self._user
        return SimpleNamespace(scalar_one_or_none=lambda: user)


@pytest.mark.asyncio
async def test_platform_jwt_reaches_an_admin_account_with_its_role(monkeypatch):
    admin = SimpleNamespace(
        id=uuid.uuid4(), email="admin@example.com", role="admin", is_active=True
    )

    async def _verify(_token, _db):
        return SimpleNamespace(
            email="Admin@Example.com",
            iss="https://opencti.example",
            sub="opencti-user-id",
            platform_identifier="opencti",
        )

    monkeypatch.setattr("app.core.xtm_auth.verify_xtm_jwt", _verify)
    monkeypatch.setattr(_jwt, "get_unverified_header", lambda _t: {"alg": "EdDSA"})

    user = await deps_module._resolve_trusted_platform_jwt("token", _OneUserDB(admin))

    assert user is admin
    assert user.role == "admin"
    assert user.platform_issuer == "https://opencti.example"
