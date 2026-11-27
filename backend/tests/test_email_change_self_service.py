"""A person changes their own email once a code proves they hold the new address.

``POST /settings/email-change`` checks the current password and sends a
6-digit code to a free address (a taken one gets no code; its holder is told,
naming nobody), answering the same 202 either way. ``POST
/settings/email-change/confirm`` writes the address on the right code. The code
is stored as an HMAC bound to the user and the address, lives 10 minutes,
works once, is replaced by a new request and dropped after 5 wrong tries.
Requests are limited per user and per target address.

The endpoint coroutines are driven directly against an in-memory Redis that
runs the real rate limiter and token store.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.exc import IntegrityError

import app.api.v1.settings as settings_api
import app.core.rate_limit as rate_limit
from app.api.v1.schemas.settings import EmailChangeConfirmRequest, EmailChangeRequest, UserPreferencesRequest
from app.core.rate_limit import INCR_EXPIRE_LUA
from app.models.user import User
from app.services import token_store

_REQUEST = SimpleNamespace(headers={}, client=None)
_SECRET = "test-secret-key-for-email-change-codes"
_PASSWORD = "the-right-password"


class _FakeRedis:
    """The Redis calls the rate limiter and the token store make, and the two scripts they run."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.ttls[key] = ex

    async def delete(self, key):
        self.ttls.pop(key, None)
        return 1 if self.store.pop(key, None) is not None else 0

    async def ttl(self, key):
        return self.ttls.get(key, -2)

    async def eval(self, script, _numkeys, key, *args):
        if script == INCR_EXPIRE_LUA:
            count = int(self.store.get(key, 0)) + 1
            self.store[key] = str(count)
            self.ttls.setdefault(key, int(args[0]))
            return count
        if script == token_store.DELETE_IF_EQUAL_LUA:
            return await self.delete(key) if self.store.get(key) == args[0] else 0
        raise AssertionError(f"unexpected script: {script!r}")


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _DB:
    """Answers each lookup with the next of *answers* (``None`` once they run out)."""

    def __init__(self, *answers, flush_conflict=False):
        self._answers = list(answers)
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
def redis(monkeypatch):
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(token_store, "redis_client", _client)
    monkeypatch.setattr(rate_limit, "redis_client", _client)
    monkeypatch.setattr("app.core.config.settings.secret_key", _SECRET)
    return fake


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
def audits(monkeypatch):
    seen: list[dict] = []

    async def _audit(_db, **kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(settings_api, "log_audit", _audit)
    return seen


@pytest.fixture
def domains(monkeypatch):
    allowed: list[str] = []
    import app.core.sso_helpers as sso_helpers

    async def _cfg(_db):
        return SimpleNamespace(allowed_domain_list=allowed)

    monkeypatch.setattr(sso_helpers, "get_sso_config", _cfg)
    return allowed


@pytest.fixture(autouse=True)
def passwords(monkeypatch):
    async def _verify(plain, _hashed):
        return plain == _PASSWORD

    monkeypatch.setattr(settings_api, "verify_password_async", _verify)


@pytest.fixture
def env(redis, mail, audits, domains):
    return SimpleNamespace(redis=redis, mail=mail, audits=audits, domains=domains)


def _user(email="jane@corp.com", **extra) -> User:
    extra.setdefault("hashed_password", "stored-hash")
    return User(id=uuid.uuid4(), email=email, full_name="Jane Roe", role="user", is_active=True, **extra)


async def _ask(user, email, *, password=_PASSWORD, holder=None):
    background = BackgroundTasks()
    answer = await settings_api.request_email_change(
        body=EmailChangeRequest(email=email, current_password=password),
        request=_REQUEST,
        background=background,
        user=user,
        db=_DB(holder),
    )
    await background()
    return answer


async def _confirm(user, code, db=None):
    background = BackgroundTasks()
    answer = await settings_api.confirm_email_change(
        body=EmailChangeConfirmRequest(code=code),
        request=_REQUEST,
        background=background,
        user=user,
        db=db or _DB(),
    )
    await background()
    return answer


def _code_in(message: dict) -> str:
    match = re.search(r"\b(\d{6})\b", message["text"])
    assert match, message["text"]
    return match.group(1)


def _wrong(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


async def _refused(coro, status: int) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        await coro
    assert exc.value.status_code == status, exc.value.detail
    return exc.value


class TestARequestIsRefused:
    async def test_for_an_sso_account(self, env):
        await _refused(_ask(_user(sso_provider="oidc"), "jane.doe@corp.com"), 400)
        assert not env.mail and not env.redis.store

    async def test_for_an_account_scim_provisioned_or_linked(self, env):
        # Its identity provider's next sync would write its own address back.
        await _refused(_ask(_user(scim_external_id="idp-42"), "jane.doe@corp.com"), 400)
        assert not env.mail and not env.redis.store

    async def test_for_an_account_scim_linked_without_an_external_id(self, env):
        await _refused(_ask(_user(scim_linked_at=datetime.now(UTC)), "jane.doe@corp.com"), 400)
        assert not env.mail and not env.redis.store

    async def test_under_impersonation(self, env):
        user = _user()
        user.impersonated_by = str(uuid.uuid4())
        await _refused(_ask(user, "jane.doe@corp.com"), 403)
        assert not env.mail and not env.redis.store

    async def test_for_the_admin_email_account(self, env, monkeypatch):
        monkeypatch.setattr("app.core.config.settings.admin_email", "jane@corp.com")
        error = await _refused(_ask(_user(), "jane.doe@corp.com"), 400)
        assert "ADMIN_EMAIL" in error.detail

    async def test_without_smtp(self, env, monkeypatch):
        monkeypatch.setattr("app.services.email.is_email_configured", lambda: False)
        await _refused(_ask(_user(), "jane.doe@corp.com"), 503)
        assert not env.redis.store

    @pytest.mark.parametrize("email", ["not-an-email", "jane@", "@corp.com", ""])
    async def test_for_an_invalid_address(self, env, email):
        await _refused(_ask(_user(), email), 400)
        assert not env.mail and not env.redis.store

    async def test_for_a_domain_registration_refuses(self, env):
        env.domains.append("corp.com")
        await _refused(_ask(_user(), "jane@elsewhere.example"), 400)
        assert not env.mail and not env.redis.store

    async def test_for_the_current_address(self, env):
        await _refused(_ask(_user(), " JANE@corp.com "), 400)
        assert not env.mail

    async def test_with_a_wrong_password(self, env):
        await _refused(_ask(_user(), "jane.doe@corp.com", password="a-guess"), 401)
        assert not env.mail
        assert not any(k.startswith("emailchange:") for k in env.redis.store)

    async def test_for_an_account_without_a_password(self, env):
        await _refused(_ask(_user(hashed_password=None), "jane.doe@corp.com"), 401)


class TestAFreeAddressGetsACode:
    async def test_the_code_changes_the_email_audits_it_and_tells_the_old_address(self, env):
        user = _user()

        answer = await _ask(user, " Jane.Doe@Corp.com ")

        assert answer == {"status": "pending"}
        (code_mail,) = env.mail
        assert code_mail["to"] == "jane.doe@corp.com"
        assert user.email == "jane@corp.com", "nothing changes before the code"

        db = _DB()
        confirmed = await _confirm(user, _code_in(code_mail), db)

        assert confirmed == {"status": "ok", "email": "jane.doe@corp.com"}
        assert user.email == "jane.doe@corp.com" and db.committed
        (audit,) = env.audits
        assert audit["user"] is user and audit["entity_type"] == "email" and audit["entity_id"] == user.id
        assert audit["details"] == {"old_email": "jane@corp.com", "new_email": "jane.doe@corp.com"}
        notice = env.mail[-1]
        assert notice["to"] == "jane@corp.com"
        assert "jane@corp.com" in notice["text"] and "jane.doe@corp.com" in notice["text"]
        assert "http" not in notice["text"], "the notice carries no link to act on"

    async def test_reset_links_sent_to_the_old_address_stop_working(self, env):
        user = _user()
        await _ask(user, "jane.doe@corp.com")
        await _confirm(user, _code_in(env.mail[0]))
        assert token_store.password_reset_addressed_to({"email": "jane@corp.com"}, user) is False

    async def test_the_code_works_once(self, env):
        user = _user()
        await _ask(user, "jane.doe@corp.com")
        code = _code_in(env.mail[0])
        await _confirm(user, code)
        await _refused(_confirm(user, code), 400)

    @pytest.mark.parametrize(
        "typed",
        [
            lambda code: f"{code[:3]} {code[3:]}",
            lambda code: f" {code[:3]}-{code[3:]} ",
            lambda code: "".join(chr(ord(c) + 0xFEE0) for c in code),  # full-width digits
        ],
        ids=["space", "dash", "full-width"],
    )
    async def test_a_code_typed_with_separators_or_full_width_digits_matches(self, env, typed):
        user = _user()
        await _ask(user, "jane.doe@corp.com")
        await _confirm(user, typed(_code_in(env.mail[0])))
        assert user.email == "jane.doe@corp.com"

    async def test_redis_holds_an_hmac_bound_to_the_user_and_the_address_for_ten_minutes(self, env):
        user = _user()
        await _ask(user, "jane.doe@corp.com")
        code = _code_in(env.mail[0])
        key = f"emailchange:{user.id}"
        stored = json.loads(env.redis.store[key])
        assert code not in env.redis.store[key]
        expected = hmac.new(
            _SECRET.encode(), f"email-change\x00{user.id}\x00jane.doe@corp.com\x00{code}".encode(), hashlib.sha256
        ).hexdigest()
        assert stored["mac"] == expected and stored["email"] == "jane.doe@corp.com"
        assert stored["from"] == "jane@corp.com"
        assert env.redis.ttls[key] == 600

    async def test_another_users_code_does_not_confirm(self, env):
        jane, john = _user(), _user("john@corp.com")
        await _ask(jane, "shared.inbox@corp.com")
        await _refused(_confirm(john, _code_in(env.mail[0])), 400)
        assert john.email == "john@corp.com"

    async def test_neither_the_code_nor_a_link_is_logged(self, env, caplog):
        caplog.set_level(logging.DEBUG)
        user = _user()
        await _ask(user, "jane.doe@corp.com")
        code = _code_in(env.mail[0])
        await _confirm(user, code)
        assert code not in caplog.text and "http" not in caplog.text


class TestATakenAddressGetsNoCode:
    async def test_the_answer_is_the_same_and_the_holder_is_told_naming_nobody(self, env):
        user = _user()

        answer = await _ask(user, "Boss@Corp.com", holder="boss@corp.com")

        assert answer == {"status": "pending"}
        (notice,) = env.mail
        assert notice["to"] == "boss@corp.com"
        assert not re.search(r"\b\d{6}\b", notice["text"]), "no code"
        assert "http" not in notice["text"], "no link"
        for identity in (user.email, user.full_name, "Jane"):
            assert identity not in notice["text"] and identity not in notice["html"]
        assert not any(k.startswith("emailchange:") for k in env.redis.store)

    async def test_it_drops_the_pending_change_as_a_free_address_replaces_it(self, env):
        user = _user()
        await _ask(user, "jane.doe@corp.com")
        first = _code_in(env.mail[0])

        await _ask(user, "boss@corp.com", holder="boss@corp.com")

        await _refused(_confirm(user, first), 400)
        assert user.email == "jane@corp.com"

    async def test_an_address_exempt_from_the_terms_of_use_answers_the_same_and_gets_no_code(self, env, monkeypatch):
        monkeypatch.setattr("app.core.config.settings.terms_of_use_exempt_emails", "robot@corp.com")
        user = _user()
        await _ask(user, "jane.doe@corp.com")

        answer = await _ask(user, "Robot@Corp.com")

        assert answer == {"status": "pending"}
        assert len(env.mail) == 1, "no code, no notice"
        assert not any(k.startswith("emailchange:") for k in env.redis.store), "the pending change is dropped"

    async def test_an_exempt_address_is_not_told_apart_before_the_password_and_the_limits(self, env, monkeypatch):
        monkeypatch.setattr("app.core.config.settings.terms_of_use_exempt_emails", "robot@corp.com")
        user = _user()
        await _refused(_ask(user, "robot@corp.com", password="a-guess"), 401)
        await _ask(user, "robot@corp.com")
        await _ask(user, "robot@corp.com")
        await _refused(_ask(user, "robot@corp.com"), 429)

    async def test_the_admin_email_address_is_taken_even_with_no_account_holding_it(self, env, monkeypatch):
        monkeypatch.setattr("app.core.config.settings.admin_email", "admin@corp.com")

        answer = await _ask(_user(), "Admin@Corp.com")

        assert answer == {"status": "pending"}
        assert not env.mail
        assert not any(k.startswith("emailchange:") for k in env.redis.store)


class TestTheConfirmation:
    async def test_a_new_request_replaces_the_pending_one(self, env):
        user = _user()
        await _ask(user, "jane.doe@corp.com")
        first = _code_in(env.mail[0])
        await _ask(user, "jane.doe@corp.com")
        second = _code_in(env.mail[1])
        if first != second:
            await _refused(_confirm(user, first), 400)
        await _confirm(user, second)
        assert user.email == "jane.doe@corp.com"

    async def test_five_wrong_codes_drop_the_request(self, env):
        user = _user()
        await _ask(user, "jane.doe@corp.com")
        code = _code_in(env.mail[0])
        for _ in range(5):
            await _refused(_confirm(user, _wrong(code)), 400)
        await _refused(_confirm(user, code), 400)
        assert user.email == "jane@corp.com"
        assert f"emailchange:{user.id}" not in env.redis.store

    async def test_four_wrong_codes_leave_the_right_one_working(self, env):
        user = _user()
        await _ask(user, "jane.doe@corp.com")
        code = _code_in(env.mail[0])
        for _ in range(4):
            await _refused(_confirm(user, _wrong(code)), 400)
        await _confirm(user, code)
        assert user.email == "jane.doe@corp.com"

    async def test_wrong_codes_of_a_replaced_request_do_not_count_against_the_new_one(self, env):
        user = _user()
        await _ask(user, "jane.doe@corp.com")
        for _ in range(4):
            await _refused(_confirm(user, _wrong(_code_in(env.mail[0]))), 400)
        await _ask(user, "jane.doe@corp.com")
        code = _code_in(env.mail[1])
        for _ in range(4):
            await _refused(_confirm(user, _wrong(code)), 400)
        await _confirm(user, code)
        assert user.email == "jane.doe@corp.com"

    async def test_without_a_pending_request(self, env):
        await _refused(_confirm(_user(), "123456"), 400)

    async def test_an_address_taken_in_between_is_refused(self, env):
        user = _user()
        await _ask(user, "jane.doe@corp.com")
        await _refused(_confirm(user, _code_in(env.mail[0]), _DB(uuid.uuid4())), 400)
        assert user.email == "jane@corp.com" and not env.audits

    async def test_an_address_made_admin_email_in_between_is_refused(self, env, monkeypatch):
        user = _user()
        await _ask(user, "jane.doe@corp.com")
        monkeypatch.setattr("app.core.config.settings.admin_email", "jane.doe@corp.com")
        await _refused(_confirm(user, _code_in(env.mail[0])), 400)
        assert user.email == "jane@corp.com" and not env.audits

    async def test_an_address_exempt_from_the_terms_of_use_in_between_is_refused(self, env, monkeypatch):
        user = _user()
        await _ask(user, "jane.doe@corp.com")
        monkeypatch.setattr("app.core.config.settings.terms_of_use_exempt_emails", "jane.doe@corp.com")
        await _refused(_confirm(user, _code_in(env.mail[0])), 400)
        assert user.email == "jane@corp.com" and not env.audits

    async def test_a_domain_no_longer_allowed_is_refused(self, env):
        user = _user()
        await _ask(user, "jane.doe@elsewhere.example")
        env.domains.append("corp.com")
        await _refused(_confirm(user, _code_in(env.mail[0])), 400)
        assert user.email == "jane@corp.com" and not env.audits

    async def test_a_change_started_from_an_address_changed_since_is_dropped(self, env):
        # An administrator changed the email while the code was on its way.
        user = _user()
        await _ask(user, "jane.doe@corp.com")
        user.email = "jane.roe@corp.com"
        await _refused(_confirm(user, _code_in(env.mail[0])), 400)
        assert user.email == "jane.roe@corp.com" and not env.audits
        assert f"emailchange:{user.id}" not in env.redis.store

    async def test_the_unique_index_refuses_a_race(self, env):
        user = _user()
        await _ask(user, "jane.doe@corp.com")
        db = _DB(flush_conflict=True)
        await _refused(_confirm(user, _code_in(env.mail[0]), db), 400)
        assert db.rolled_back and not db.committed and not env.audits
        assert len(env.mail) == 1, "the old address is told of no change"

    async def test_under_impersonation(self, env):
        user = _user()
        await _ask(user, "jane.doe@corp.com")
        user.impersonated_by = str(uuid.uuid4())
        await _refused(_confirm(user, _code_in(env.mail[0])), 403)
        assert user.email == "jane@corp.com"

    async def test_for_an_sso_account(self, env):
        await _refused(_confirm(_user(sso_provider="saml"), "123456"), 400)

    async def test_for_an_account_scim_linked_since_the_request(self, env):
        user = _user()
        await _ask(user, "jane.doe@corp.com")
        user.scim_external_id = "idp-42"
        await _refused(_confirm(user, _code_in(env.mail[0])), 400)
        assert user.email == "jane@corp.com"


class TestTheProfileIsTold:
    """``GET /auth/me`` reports ``email_change_blocked``, so the profile offers no change the routes refuse."""

    @pytest.mark.parametrize(
        ("extra", "blocked"),
        [
            ({}, None),
            ({"sso_provider": "oidc"}, "identity_provider"),
            ({"scim_external_id": "idp-42"}, "identity_provider"),
        ],
    )
    def test_the_reason_follows_the_account(self, env, extra, blocked):
        assert settings_api.email_change_blocked(_user(**extra)) == blocked

    def test_the_platform_admin(self, env, monkeypatch):
        monkeypatch.setattr("app.core.config.settings.admin_email", "jane@corp.com")
        assert settings_api.email_change_blocked(_user()) == "platform_admin"

    def test_without_smtp(self, env, monkeypatch):
        monkeypatch.setattr("app.services.email.is_email_configured", lambda: False)
        assert settings_api.email_change_blocked(_user()) == "no_email"


class TestTheRateLimits:
    async def test_three_requests_per_hour_per_user(self, env):
        user = _user()
        await _ask(user, "a@corp.com")
        await _refused(_ask(user, "b@corp.com", password="a-guess"), 401)
        await _ask(user, "c@corp.com")
        await _refused(_ask(user, "d@corp.com"), 429)
        assert [m["to"] for m in env.mail] == ["a@corp.com", "c@corp.com"]

    async def test_three_requests_per_hour_per_target_address(self, env):
        for name in ("ann", "bob", "cat"):
            await _ask(_user(f"{name}@corp.com"), "inbox@corp.com")
        await _refused(_ask(_user("dan@corp.com"), "Inbox@Corp.com"), 429)
        assert len(env.mail) == 3

    async def test_a_taken_address_counts_against_its_budget_too(self, env):
        for name in ("ann", "bob", "cat"):
            await _ask(_user(f"{name}@corp.com"), "boss@corp.com", holder="boss@corp.com")
        await _refused(_ask(_user("dan@corp.com"), "boss@corp.com", holder="boss@corp.com"), 429)
        assert len(env.mail) == 3

    async def test_the_address_bucket_does_not_hold_the_address(self, env):
        await _ask(_user(), "jane.doe@corp.com")
        limiter_keys = [k for k in env.redis.store if k.startswith("rl:")]
        assert limiter_keys and not any("jane.doe" in k for k in limiter_keys)


class TestThePreferencesRoute:
    async def test_a_new_address_is_refused_naming_the_code_route(self, env):
        class _NoReadDB:
            async def execute(self, _stmt):
                raise AssertionError("a refused change reads nothing")

        user = _user()
        error = await _refused(
            settings_api.update_preferences(
                body=UserPreferencesRequest(email="jane.doe@corp.com"), request=_REQUEST, user=user, db=_NoReadDB()
            ),
            422,
        )
        assert "/api/v1/settings/email-change" in error.detail
        assert user.email == "jane@corp.com"
