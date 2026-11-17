"""Resolving whether AI is enabled (FR-003/FR-004).

The interesting case is `NULL` following a *changed* mode. That is the failure
with no visible symptom: a stored default would look identical on day one and
silently ignore every later change to `PLATFORM_MODE`.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from app.services import ai_state


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Savepoint:
    """The ``begin_nested()`` context the resolver reads inside.

    Present because a failed read must roll back only itself: the real session
    aborts its whole transaction on a failed SELECT, which would otherwise
    discard whatever the calling request had staged.
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _Connection:
    """The Core connection the resolver takes its savepoint on."""

    def __init__(self, session):
        self._session = session

    def begin_nested(self):
        self._session.savepoints += 1
        return _Savepoint()


class _Session:
    """Returns one stored value, or raises to simulate an unreadable state.

    Models the **Core** savepoint the resolver uses: ``connection()`` then
    ``begin_nested()`` on that connection, under ``no_autoflush``. Deliberately
    not ``Session.begin_nested``, which flushes the caller's pending ORM state —
    ``flush`` here records any call so the test can assert it never happens.
    """

    def __init__(self, value=None, boom: Exception | None = None):
        self._value = value
        self._boom = boom
        self.savepoints = 0
        self.flushes = 0

    async def connection(self):
        return _Connection(self)

    @property
    def no_autoflush(self):
        return _NullContext()

    async def flush(self):
        self.flushes += 1

    async def execute(self, _query):
        if self._boom is not None:
            raise self._boom
        return _Result(self._value)


@pytest.mark.parametrize(
    ("mode", "stored", "expected"),
    [
        # NULL follows the mode — both ways.
        ("xtm_one", None, True),
        ("ai_disabled", None, False),
        # An explicit choice wins over the mode — both ways.
        ("ai_disabled", False, True),
        ("xtm_one", True, False),
        ("xtm_one", False, True),
        ("ai_disabled", True, False),
    ],
)
@pytest.mark.asyncio
async def test_resolution(monkeypatch, mode, stored, expected):
    monkeypatch.setattr(ai_state.settings, "platform_mode", mode)
    assert await ai_state.is_ai_enabled(_Session(stored)) is expected


@pytest.mark.asyncio
async def test_null_follows_a_changed_mode(monkeypatch):
    """The regression a stored default would cause: identical on day one, and
    deaf to every later change to PLATFORM_MODE."""
    db = _Session(None)
    monkeypatch.setattr(ai_state.settings, "platform_mode", "xtm_one")
    assert await ai_state.is_ai_enabled(db) is True

    # The answer is cached for a few seconds, so a mode change needs the cache
    # dropped — which a restart does, and PLATFORM_MODE changes need one anyway.
    monkeypatch.setattr(ai_state.settings, "platform_mode", "ai_disabled")
    ai_state.invalidate_cache()
    assert await ai_state.is_ai_enabled(db) is False


@pytest.mark.asyncio
async def test_an_explicit_choice_is_deaf_to_the_mode(monkeypatch):
    """The other half of FR-003: once set, the toggle wins until it is reset."""
    db = _Session(False)
    for mode in ("xtm_one", "ai_disabled", "copilot"):
        monkeypatch.setattr(ai_state.settings, "platform_mode", mode)
        ai_state.invalidate_cache()
        assert await ai_state.is_ai_enabled(db) is True


@pytest.mark.asyncio
async def test_unreadable_gates_disabled(monkeypatch):
    """Constitution II. Refusing is recoverable; assuming enabled runs a model
    on a deployment that may forbid it."""
    monkeypatch.setattr(ai_state.settings, "platform_mode", "xtm_one")
    db = _Session(boom=OperationalError("SELECT", {}, Exception("gone")))
    assert await ai_state.is_ai_enabled(db) is False


@pytest.mark.asyncio
async def test_unreadable_raises_for_boot(monkeypatch):
    """The opposite policy, deliberately: failing closed at boot would skip
    seeding on a normal platform and leave it silently empty."""
    monkeypatch.setattr(ai_state.settings, "platform_mode", "xtm_one")
    db = _Session(boom=OperationalError("SELECT", {}, Exception("gone")))
    with pytest.raises(OperationalError):
        await ai_state.is_ai_enabled_strict(db)


def test_mode_only_resolution_needs_no_database(monkeypatch):
    monkeypatch.setattr(ai_state.settings, "platform_mode", "ai_disabled")
    assert ai_state.ai_enabled_by_mode() is False
    monkeypatch.setattr(ai_state.settings, "platform_mode", "xtm_one")
    assert ai_state.ai_enabled_by_mode() is True


def test_only_this_module_decides_ai_from_the_mode():
    """FR-004 is enforceable by grep, and this is the grep.

    Matches a *comparison* of ``platform_mode`` against the ai_disabled value,
    not mere co-occurrence: ``platform.py`` legitimately reads the mode to
    report it, and an earlier version of this test failed on a comment.
    """
    import pathlib
    import re

    root = pathlib.Path(ai_state.__file__).parent.parent
    pattern = re.compile(r"platform_mode\s*[!=]=\s*[\"']ai_disabled|ai_disabled[\"']\s*[!=]=\s*.*platform_mode")
    offenders = [
        str(f.relative_to(root))
        for f in root.rglob("*.py")
        if f.name != "ai_state.py" and pattern.search(f.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "these decide AI state from platform_mode directly — resolve through "
        f"services/ai_state.py instead: {offenders}"
    )


@pytest.mark.asyncio
async def test_a_programming_error_is_not_reported_as_ai_disabled(monkeypatch):
    """Only database trouble fails closed.

    A bare ``except Exception`` here swallowed an ``AssertionError`` from a test
    fake and answered "AI is disabled" — so a bug in this query would have
    disabled AI platform-wide, quietly, with a message blaming configuration.
    """
    monkeypatch.setattr(ai_state.settings, "platform_mode", "xtm_one")
    db = _Session(boom=AttributeError("typo in the query"))
    with pytest.raises(AttributeError):
        await ai_state.is_ai_enabled(db)


@pytest.mark.asyncio
async def test_the_cached_answer_is_reused_without_touching_the_database(monkeypatch):
    """Why the cache exists: the gate runs on ~83 routes, and
    ``require_chat_enabled`` is tested against a DB that raises on any access.
    """
    monkeypatch.setattr(ai_state.settings, "platform_mode", "xtm_one")
    ai_state.prime_cache(True)

    class _Explodes:
        async def execute(self, _query):
            raise AssertionError("the cache must answer without a query")

    assert await ai_state.is_ai_enabled(_Explodes()) is True


@pytest.mark.asyncio
async def test_a_failure_is_not_cached(monkeypatch):
    """A blip must not keep AI off for a whole TTL after the database recovers."""
    monkeypatch.setattr(ai_state.settings, "platform_mode", "xtm_one")
    ai_state.invalidate_cache()

    boom = _Session(boom=OperationalError("SELECT", {}, Exception("gone")))
    assert await ai_state.is_ai_enabled(boom) is False
    # Recovered: answered fresh, not from a cached "disabled".
    assert await ai_state.is_ai_enabled(_Session(None)) is True


async def test_the_read_happens_inside_a_savepoint():
    """A failed read must not poison the caller's transaction.

    Postgres aborts the whole transaction on a failed SELECT. A bare
    ``db.rollback()`` fixed the resulting misattributed error but threw away the
    caller's staged work, which is wrong for the mid-request callers
    (``is_chat_enabled``, ``is_agentic_ai_enabled``).
    """
    from app.services import ai_state

    ai_state.invalidate_cache()
    session = _Session(value=None)
    await ai_state.is_ai_enabled(session)
    assert session.savepoints == 1
    # And it must not flush on the way in. ``Session.begin_nested`` does
    # (``_take_snapshot`` calls ``session.flush()``), which flushed the caller's
    # half-built entities on every cache miss and, on a failed read, expired
    # their modified instances so the next attribute access raised
    # ``MissingGreenlet`` — the opposite of the graceful gating promised above
    # (/code-review, 2026-08-28). Hence a Core savepoint plus ``no_autoflush``.
    assert session.flushes == 0
