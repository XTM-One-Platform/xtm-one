"""The companion's PIN lock: which PINs are accepted, which requests are refused.

The rules alone (``services/app_lock.py``), on stand-ins for the two rows they
read; the routes, the real dependency chain and the push content run against
PostgreSQL in ``test_app_lock_postgres.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import app_lock

# The companion is behind MOBILE_COMPANION: these run with it on.
pytestmark = pytest.mark.usefixtures("mobile_companion_enabled")

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("pin", "problem"),
    [
        ("482915", None),
        ("48291573", None),
        ("135790", None),
        ("12345", "A PIN has 6 to 8 digits."),
        ("482915736", "A PIN has 6 to 8 digits."),
        ("", "A PIN is made of digits only."),
        ("48291a", "A PIN is made of digits only."),
        ("4829 15", "A PIN is made of digits only."),
        ("٤٨٢٩١٥", "A PIN is made of digits only."),
        (482915, "A PIN is made of digits only."),
        (None, "A PIN is made of digits only."),
        ("000000", "Choose a PIN that is not one digit repeated."),
        ("77777777", "Choose a PIN that is not one digit repeated."),
        ("123456", "Choose a PIN that is not a run of consecutive digits."),
        ("98765432", "Choose a PIN that is not a run of consecutive digits."),
    ],
)
def test_a_pin_is_six_to_eight_digits_and_not_guessable_at_a_glance(pin, problem):
    assert app_lock.pin_problem(pin) == problem


def _user(sid: str | None) -> SimpleNamespace:
    session = SimpleNamespace(claims={"sid": sid} if sid is not None else {})
    return SimpleNamespace(id=uuid.UUID(int=1), web_session=session)


class _Db:
    """``db.get(AppLockSession, sid)``: the session's row, or ``None``."""

    def __init__(self, row):
        self.row = row

    async def get(self, _model, _sid):
        return self.row


def _row(*, user_id=uuid.UUID(int=1), unlocked_until=None, ended=False):
    return SimpleNamespace(user_id=user_id, unlocked_until=unlocked_until, ended=ended, failed_attempts=0)


def _pin(enabled: bool):
    return SimpleNamespace(enabled=enabled, pin_hash="x")


UNLOCKED = NOW + timedelta(minutes=5)
EXPIRED = NOW - timedelta(seconds=1)


@pytest.mark.parametrize(
    ("sid", "row", "pin", "required", "expected"),
    [
        # Not a companion session: a computer, an API key, someone else's row.
        (None, _row(), _pin(True), True, None),
        ("s", None, _pin(True), True, None),
        ("s", _row(user_id=uuid.UUID(int=2)), _pin(True), True, None),
        # A companion session without a PIN that applies answers.
        ("s", _row(), None, False, None),
        ("s", _row(), _pin(False), False, None),
        # The PIN applies: locked until unlocked, and again once the unlock ran out.
        ("s", _row(), _pin(True), False, (423, app_lock.LOCKED)),
        ("s", _row(unlocked_until=EXPIRED), _pin(True), False, (423, app_lock.LOCKED)),
        ("s", _row(unlocked_until=NOW), _pin(True), False, (423, app_lock.LOCKED)),
        ("s", _row(unlocked_until=UNLOCKED), _pin(True), False, None),
        # The policy requires it: a PIN turned off still applies, none at all is chosen first.
        ("s", _row(), _pin(False), True, (423, app_lock.LOCKED)),
        ("s", _row(unlocked_until=UNLOCKED), _pin(False), True, None),
        ("s", _row(), None, True, (423, app_lock.SETUP_REQUIRED)),
        # Too many wrong PINs ended the session, whatever else holds.
        ("s", _row(ended=True), None, False, (401, app_lock.ENDED)),
        ("s", _row(ended=True, unlocked_until=UNLOCKED), _pin(True), False, (401, app_lock.ENDED)),
    ],
)
async def test_a_locked_companion_session_is_refused_and_nothing_else_is(monkeypatch, sid, row, pin, required, expected):
    async def _load_pin(_db, _user_id):
        return pin

    monkeypatch.setattr(app_lock, "load_pin", _load_pin)
    monkeypatch.setattr(app_lock, "pin_required", lambda: required)
    if expected is None:
        assert await app_lock.enforce(_Db(row), _user(sid), now=NOW) is None
        return
    with pytest.raises(HTTPException) as refused:
        await app_lock.enforce(_Db(row), _user(sid), now=NOW)
    assert (refused.value.status_code, refused.value.detail) == (expected[0], {"code": expected[1]})


@pytest.mark.parametrize(
    "session",
    [None, SimpleNamespace(claims={}), SimpleNamespace(claims={"sid": ""}), SimpleNamespace(claims={"sid": "x" * 65})],
)
def test_only_a_web_session_with_a_usable_sid_is_a_companion_candidate(session):
    assert app_lock.session_id(SimpleNamespace(id=uuid.UUID(int=1), web_session=session)) is None


def test_the_lock_follows_its_security_option(monkeypatch):
    from app.core import security_options as so

    option = so.SECURITY_OPTIONS["companion_pin_required"]
    assert (option.kind, option.group, option.default()) == ("bool", "authentication", False)
    monkeypatch.setattr(so, "_environment_fields", frozenset)
    monkeypatch.setattr(so, "_stored", {"companion_pin_required": True})
    assert app_lock.pin_required() is True
    monkeypatch.setattr(so, "_stored", {"companion_pin_required": False})
    assert app_lock.pin_required() is False
