"""An address in ``TERMS_OF_USE_EXEMPT_EMAILS`` is never self-service.

The exemption skips the Terms of Use gate for the account that carries the
address, so an unverified claim on it would skip the gate too. Self-registration
refuses it, and a user never changes their own email (an administrator does,
``test_email_change_by_admin``); an administrator, an invitation or SSO creates
the account. The refusal comes
before any database read, so it also holds while the address is still free.

Mirrors the repo's fake-session convention: the endpoint coroutines are driven
directly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.v1.settings as settings_api
from app.api.v1.auth import RegisterRequest, register
from app.api.v1.schemas.settings import UserPreferencesRequest
from app.core.config import settings

EXEMPT = "ops@vendor.example"


class _NoReadDB:
    """Counts reads; the refusal must come before any of them."""

    def __init__(self, user_count: int = 1):
        self.user_count = user_count
        self.executed = 0

    async def scalar(self, _stmt):
        return self.user_count

    async def execute(self, _stmt):
        self.executed += 1
        raise AssertionError("the exempt address must be refused before any lookup")


@pytest.fixture
def _exempt(monkeypatch):
    monkeypatch.setattr(settings, "terms_of_use_exempt_emails", EXEMPT)


@pytest.fixture
def _open_registration(monkeypatch):
    import app.core.sso_helpers as sso_helpers

    async def _cfg(_db):
        return SimpleNamespace(
            local_auth_enabled=True, registration_enabled=True, allowed_domain_list=[]
        )

    monkeypatch.setattr(sso_helpers, "get_sso_config", _cfg)


@pytest.mark.parametrize("email", ["ops@vendor.example", "OPS@Vendor.Example"])
async def test_registration_refuses_an_exempt_address(_exempt, _open_registration, email):
    db = _NoReadDB()
    body = RegisterRequest(email=email, password="Str0ng-enough-pass", full_name="X")
    with pytest.raises(HTTPException) as exc:
        await register(body=body, request=SimpleNamespace(), db=db)
    assert exc.value.status_code == 403
    assert exc.value.detail == "This email address cannot be registered"
    assert db.executed == 0


async def _update_email(monkeypatch, user, email):
    async def _no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings_api, "log_audit", _no_audit)
    return await settings_api.update_preferences(
        body=UserPreferencesRequest(email=email),
        request=SimpleNamespace(headers={}, client=None),
        user=user,
        db=_NoReadDB(),
    )


@pytest.mark.parametrize("email", ["ops@vendor.example", " OPS@vendor.example "])
async def test_profile_email_change_refuses_an_exempt_address(monkeypatch, _exempt, email):
    user = SimpleNamespace(email="u@example.com")
    with pytest.raises(HTTPException) as exc:
        await _update_email(monkeypatch, user, email)
    assert exc.value.status_code == 422
    assert user.email == "u@example.com"


async def test_exempt_account_keeps_its_own_address(monkeypatch, _exempt):
    """Saving Preferences with the unchanged address is not a claim on it."""

    class _DB:
        async def flush(self):
            return None

        async def commit(self):
            return None

    async def _no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings_api, "log_audit", _no_audit)
    user = SimpleNamespace(
        email=EXEMPT, full_name="Ops", title=None, preferred_llm_model=None, favorite_agent_ids=None
    )
    await settings_api.update_preferences(
        body=UserPreferencesRequest(email=EXEMPT),
        request=SimpleNamespace(headers={}, client=None),
        user=user,
        db=_DB(),
    )
    assert user.email == EXEMPT
