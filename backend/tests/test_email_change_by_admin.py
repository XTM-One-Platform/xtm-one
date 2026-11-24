"""A user's email changes only by an administrator.

The email is the identity XTM One asserts to other products (the cross-platform
JWT and the product sign-in carry it, and they find their account by it), so a
user never sets it themselves: ``PUT /settings/preferences`` refuses a new
address and ``PUT /users/{id}/email`` (administrators) changes it, audited, with
a notice to the old and the new address. A password reset link sent before a
change no longer works after it.

Mirrors the repo's fake-session convention: the endpoint coroutines are driven
directly.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

import app.api.v1.settings as settings_api
import app.api.v1.users as users_api
from app.api.v1.schemas.settings import UserPreferencesRequest
from app.models.user import User
from app.services import token_store

_REQUEST = SimpleNamespace(headers={}, client=None)


class _NoReadDB:
    async def execute(self, _stmt):
        raise AssertionError("a refused change reads nothing")


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _AdminDB:
    """Answers the target lookup, then the "taken by someone else" lookup."""

    def __init__(self, target, taken=None, *, flush_conflict=False):
        self._answers = [target, taken]
        self.flush_conflict = flush_conflict
        self.committed = False
        self.rolled_back = False

    async def execute(self, _stmt):
        return _Result(self._answers.pop(0) if self._answers else None)

    async def flush(self):
        if self.flush_conflict:
            raise IntegrityError("UPDATE users", {}, Exception("ix_users_email_lower"))

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


@pytest.fixture
def audits(monkeypatch):
    seen: list[dict] = []

    async def _audit(_db, **kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(users_api, "log_audit", _audit)
    monkeypatch.setattr(settings_api, "log_audit", _audit)
    return seen


@pytest.fixture
def mail(monkeypatch):
    sent: list[dict] = []

    async def _send(**kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr("app.services.email.is_email_configured", lambda: True)
    monkeypatch.setattr("app.services.email.send_email", _send)
    return sent


@pytest.fixture
def open_domains(monkeypatch):
    import app.core.sso_helpers as sso_helpers

    async def _cfg(_db):
        return SimpleNamespace(allowed_domain_list=[])

    monkeypatch.setattr(sso_helpers, "get_sso_config", _cfg)


def _user(email="jane@corp.com", **extra) -> User:
    return User(id=uuid.uuid4(), email=email, full_name="Jane", role="user", is_active=True, **extra)


def _admin() -> User:
    return User(id=uuid.uuid4(), email="boss@corp.com", full_name="Boss", role="admin")


async def _change(db, target, email):
    return await users_api.update_user_email(
        user_id=str(target.id),
        body=users_api.UpdateEmailRequest(email=email),
        request=_REQUEST,
        admin=_admin(),
        db=db,
    )


class TestTheProfileNeverChangesTheEmail:
    @pytest.mark.parametrize("email", ["boss@corp.com", "free.address@corp.com"])
    async def test_a_new_address_is_refused_naming_the_administrator_path(self, audits, email):
        me = _user()
        with pytest.raises(HTTPException) as exc:
            await settings_api.update_preferences(
                body=UserPreferencesRequest(email=email, full_name="Renamed"), request=_REQUEST, user=me, db=_NoReadDB()
            )
        assert exc.value.status_code == 422
        assert "/api/v1/users/{user_id}/email" in exc.value.detail
        assert (me.email, me.full_name) == ("jane@corp.com", "Jane")


class TestAnAdministratorChangesIt:
    async def test_the_change_is_normalised_audited_and_announced_to_both_addresses(
        self, audits, mail, open_domains
    ):
        target = _user()
        db = _AdminDB(target)

        answer = await _change(db, target, " Jane.Doe@Corp.com ")

        assert target.email == "jane.doe@corp.com" and answer["email"] == "jane.doe@corp.com"
        assert db.committed
        (audit,) = audits
        assert audit["entity_type"] == "email" and audit["entity_id"] == target.id
        assert audit["details"] == {"old_email": "jane@corp.com", "new_email": "jane.doe@corp.com"}
        assert sorted(m["to"] for m in mail) == ["jane.doe@corp.com", "jane@corp.com"]
        assert all("jane@corp.com" in m["text"] and "jane.doe@corp.com" in m["text"] for m in mail)
        assert not any("http" in m["text"] for m in mail), "the notice carries no link to act on"

    async def test_an_sso_account_changes_too(self, audits, mail, open_domains):
        target = _user(sso_provider="oidc")
        await _change(_AdminDB(target), target, "jane.doe@corp.com")
        assert target.email == "jane.doe@corp.com"

    async def test_without_smtp_the_change_still_applies(self, audits, monkeypatch, open_domains):
        monkeypatch.setattr("app.services.email.is_email_configured", lambda: False)

        async def _never(**_kwargs):  # pragma: no cover
            raise AssertionError("no mail without SMTP")

        monkeypatch.setattr("app.services.email.send_email", _never)
        target = _user()
        await _change(_AdminDB(target), target, "jane.doe@corp.com")
        assert target.email == "jane.doe@corp.com"

    async def test_the_admin_email_account_never_changes(self, audits, monkeypatch, open_domains):
        monkeypatch.setattr("app.core.config.settings.admin_email", "jane@corp.com")
        target = _user()
        with pytest.raises(HTTPException) as exc:
            await _change(_AdminDB(target), target, "jane.doe@corp.com")
        assert exc.value.status_code == 400 and "ADMIN_EMAIL" in exc.value.detail
        assert target.email == "jane@corp.com" and not audits

    async def test_an_address_another_account_holds_is_a_conflict(self, audits, open_domains):
        target = _user()
        with pytest.raises(HTTPException) as exc:
            await _change(_AdminDB(target, taken=_user("other@corp.com")), target, "Other@Corp.com")
        assert exc.value.status_code == 409
        assert target.email == "jane@corp.com" and not audits

    async def test_an_address_taken_in_between_is_a_conflict(self, audits, open_domains):
        target = _user()
        db = _AdminDB(target, flush_conflict=True)
        with pytest.raises(HTTPException) as exc:
            await _change(db, target, "other@corp.com")
        assert exc.value.status_code == 409 and db.rolled_back and not audits

    async def test_a_domain_registration_refuses_is_refused(self, audits, monkeypatch):
        import app.core.sso_helpers as sso_helpers

        async def _cfg(_db):
            return SimpleNamespace(allowed_domain_list=["corp.com"])

        monkeypatch.setattr(sso_helpers, "get_sso_config", _cfg)
        target = _user()
        with pytest.raises(HTTPException) as exc:
            await _change(_AdminDB(target), target, "jane@elsewhere.example")
        assert exc.value.status_code == 400
        assert target.email == "jane@corp.com"

    async def test_the_same_address_changes_nothing(self, audits, mail, open_domains):
        target = _user()
        db = _AdminDB(target)
        await _change(db, target, "JANE@corp.com")
        assert target.email == "jane@corp.com" and not audits and not mail


class TestAResetLinkFollowsTheAddressItWasSentTo:
    async def test_the_link_names_its_address(self, monkeypatch):
        stored: dict = {}

        class _Redis:
            async def set(self, key, value, ex):
                stored[key] = value

        async def _client():
            return _Redis()

        monkeypatch.setattr(token_store, "redis_client", _client)
        await token_store.create_password_reset(
            user_id=uuid.uuid4(), created_by_id=None, ttl_hours=1, email="Jane@Corp.com"
        )
        (value,) = stored.values()
        assert '"email": "jane@corp.com"' in value

    def test_a_link_sent_to_a_former_address_is_refused(self):
        user = _user("jane.doe@corp.com")
        assert token_store.password_reset_addressed_to({"email": "jane@corp.com"}, user) is False
        assert token_store.password_reset_addressed_to({"email": "jane.doe@corp.com"}, user) is True

    def test_a_link_minted_before_links_named_their_address_still_works(self):
        assert token_store.password_reset_addressed_to({"user_id": "x"}, _user()) is True

    async def test_the_reset_refuses_it(self, monkeypatch):
        import app.api.v1.auth as auth_api

        user = _user("jane.doe@corp.com")

        async def _consume(_token):
            return {"user_id": str(user.id), "email": "jane@corp.com"}

        class _DB:
            async def get(self, _model, _id):
                return user

        async def _no_limit(*_a, **_k):
            return None

        monkeypatch.setattr(auth_api, "enforce_rate_limit", _no_limit)
        monkeypatch.setattr(token_store, "consume_password_reset", _consume)
        with pytest.raises(HTTPException) as exc:
            await auth_api.reset_password(
                body=auth_api.ResetPasswordRequest(token="t", new_password="Str0ng-enough-pass!"),
                request=_REQUEST,
                db=_DB(),
            )
        assert exc.value.status_code == 410

    async def test_the_reset_page_shows_it_as_invalid(self, monkeypatch):
        import app.api.v1.auth as auth_api

        user = _user("jane.doe@corp.com")

        async def _peek(_token):
            return {"user_id": str(user.id), "email": "jane@corp.com"}

        class _DB:
            async def get(self, _model, _id):
                return user

        monkeypatch.setattr(token_store, "get_password_reset", _peek)
        answer = await auth_api.validate_password_reset(token="t", db=_DB())
        assert answer.valid is False
