"""Regression tests for the admin ``users`` API.

Currently pins one behaviour: the XTM One default-agent assignment for
admin-created accounts. The platform's default-agent contract for new
users is enforced in four places (self-service ``/register``,
invitation consumption, SSO first login, and the admin ``POST /users``
endpoint). The first three have implicit / direct coverage; these tests
pin the admin path so a future refactor that drops the CTEM-agent assignment
from ``create_user`` trips loudly.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest


async def _no_audit(*_a, **_kw) -> None:
    return None


def _make_db(ctem_id: uuid.UUID | None):
    """Return a stub ``AsyncSession`` for ``create_user``.

    The route runs three queries in order:

    1. Case-insensitive duplicate-email lookup (``select(User)`` with
       ``func.lower``) — must return ``None`` so we hit the create path.
    2. (XTM One only) CTEM agent slug lookup (``select(Agent.id) ...
       slug == 'ctem-assistant'``) — return ``ctem_id`` so the route
       sets ``user.default_agent_id`` from it.
    3. Anything else — return ``None``. The route doesn't run any other
       SELECT before ``db.flush()``.

    After ``db.flush()`` the route joins the new account to "CTEM Viewers"
    (#2441): those statements are counted apart in ``post_flush_count`` and
    answer ``None`` (the group is not seeded), so ``execute_count`` keeps
    pinning the SELECTs that run before the flush.

    The stub captures (a) the ``User`` object passed to ``db.add`` so
    the test can assert on its fields, (b) whether ``db.flush()`` and
    ``db.commit()`` were awaited, and (c) the live ``execute_count`` so
    tests can pin the "the CTEM-id lookup must / must-not fire" contract
    explicitly (Copilot would otherwise have to take the docstring's word
    for it). The commit's position relative to the response is pinned in
    ``test_account_writes_commit_before_response.py``.
    """

    captured: dict[str, object] = {
        "user": None,
        "flush_called": False,
        "commit_called": False,
        "execute_count": 0,
        "post_flush_count": 0,
    }

    class _Result:
        def __init__(self, value: object) -> None:
            self._value = value

        def scalar_one_or_none(self) -> object:
            return self._value

    class _DB:
        async def execute(self, _stmt):
            if captured["flush_called"]:
                # CTEM Viewers join (#2441): the group is not seeded here.
                captured["post_flush_count"] += 1  # type: ignore[operator]
                return _Result(None)
            captured["execute_count"] += 1  # type: ignore[operator]
            call = captured["execute_count"]
            if call == 1:
                # Duplicate-email check.
                return _Result(None)
            if call == 2:
                # CTEM agent id lookup (only fires in XTM One mode).
                return _Result(ctem_id)
            return _Result(None)

        def add(self, obj):
            captured["user"] = obj
            # Mimic the autoflush that would assign the server-side id
            # so the audit-log call after add() works.
            obj.id = uuid.uuid4()

        async def flush(self):
            captured["flush_called"] = True

        async def commit(self):
            captured["commit_called"] = True

    return _DB(), captured


@pytest.fixture
def admin():
    return SimpleNamespace(id=uuid.uuid4(), role="admin", is_admin=True)


async def test_create_user_assigns_ctem_assistant_in_xtm_one_mode(monkeypatch, admin):
    """In XTM One mode, the admin ``POST /users`` endpoint must set
    ``user.default_agent_id`` to the CTEM Assistant — keeping the
    admin path in lockstep with the self-service ``/register``,
    invitation consumption, and SSO first-login paths.

    Pin via the route function directly: a stubbed DB returns the
    CTEM agent id from the slug lookup, and we assert that the
    ``User`` object handed to ``db.add`` carries the same id on
    ``default_agent_id``. A regression that drops the assignment
    block from ``create_user`` trips here.
    """
    from app.api.v1 import users as users_module
    from app.api.v1.users import CreateUserRequest, create_user
    from app.core import config as core_config

    monkeypatch.setattr(core_config.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(users_module, "log_audit", _no_audit)

    ctem_id = uuid.uuid4()
    db, captured = _make_db(ctem_id=ctem_id)

    body = CreateUserRequest(
        email="alice@example.com",
        full_name="Alice Example",
        password="Sup3rSecret",
        role="user",
        account_type="local",
    )
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})

    resp = await create_user(body=body, request=request, admin=admin, db=db)

    assert captured["flush_called"], "user row must be flushed"
    assert captured["commit_called"], "user row must be committed before the route answers"
    assert captured["user"] is not None, "user row must be added to the session"
    assert captured["user"].default_agent_id == ctem_id, (
        "XTM One mode must default admin-created users to the CTEM Assistant"
    )
    # The route must have run BOTH the duplicate-email check AND the
    # CTEM-id lookup — exactly two SELECTs before ``db.flush()``.
    # Anything else means the slug-resolution block was bypassed or
    # ran additional unintended queries.
    assert captured["execute_count"] == 2, (
        f"XTM One mode must run exactly 2 SELECTs (duplicate-email + "
        f"CTEM slug lookup); got {captured['execute_count']}"
    )
    assert resp.email == "alice@example.com"
    # The new account is offered to "CTEM Viewers" once, after the flush.
    assert captured["post_flush_count"] == 1, (
        f"the CTEM Viewers group lookup must run once after the flush; got {captured['post_flush_count']}"
    )


async def test_create_user_skips_ctem_assignment_in_copilot_mode(monkeypatch, admin):
    """In Copilot mode, the admin ``POST /users`` endpoint must NOT
    touch ``default_agent_id`` — Copilot users are routed to the
    General Assistant via the platform's separate fallback chain, and
    silently planting the CTEM Assistant on Copilot rows would
    over-ride the user's explicit profile choice on first login.

    Pin via the same stubbed DB: the duplicate-email lookup runs, but
    the CTEM-id lookup MUST NOT fire — asserted explicitly by the
    ``execute_count`` captured from the stub staying at ``1``. The
    user row must be flushed with ``default_agent_id`` left at its
    default ``None``.
    """
    from app.api.v1 import users as users_module
    from app.api.v1.users import CreateUserRequest, create_user
    from app.core import config as core_config

    monkeypatch.setattr(core_config.settings, "platform_mode", "copilot")
    monkeypatch.setattr(users_module, "log_audit", _no_audit)

    db, captured = _make_db(ctem_id=uuid.uuid4())  # ctem_id supplied but unused

    body = CreateUserRequest(
        email="bob@example.com",
        full_name="Bob Example",
        password="Sup3rSecret",
        role="user",
        account_type="local",
    )
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})

    await create_user(body=body, request=request, admin=admin, db=db)

    assert captured["user"] is not None
    assert captured["user"].default_agent_id is None, (
        "Copilot mode must not assign default_agent_id on admin-created users"
    )
    # Explicit pin for the "CTEM-id lookup must NOT fire" contract:
    # only the duplicate-email check should have run before
    # ``db.flush()`` — anything higher means the Copilot branch
    # silently leaked into the XTM-One-only slug query.
    assert captured["execute_count"] == 1, (
        f"Copilot mode must run exactly 1 SELECT (duplicate-email check "
        f"only); the CTEM slug lookup leaked: got {captured['execute_count']}"
    )


async def test_create_user_handles_missing_ctem_agent_in_xtm_one_mode(
    monkeypatch, admin
):
    """In XTM One mode, if the CTEM Assistant agent has not yet been
    seeded (e.g. a fresh boot before ``seed_builtin_agents`` ran), the
    admin-create path must still succeed and leave ``default_agent_id``
    at ``None``. The boot-time bulk sweep on the next restart, or a
    later admin retry, will then back-fill the value.

    Pin via the stubbed DB returning ``None`` for the slug lookup.
    A regression that 500s, or that crashes on a NULL ctem id, trips
    here.
    """
    from app.api.v1 import users as users_module
    from app.api.v1.users import CreateUserRequest, create_user
    from app.core import config as core_config

    monkeypatch.setattr(core_config.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(users_module, "log_audit", _no_audit)

    db, captured = _make_db(ctem_id=None)

    body = CreateUserRequest(
        email="carol@example.com",
        full_name="Carol Example",
        password="Sup3rSecret",
        role="user",
        account_type="local",
    )
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})

    await create_user(body=body, request=request, admin=admin, db=db)

    assert captured["user"] is not None
    assert captured["user"].default_agent_id is None
    # The route MUST have attempted the CTEM lookup (otherwise this
    # test isn't exercising the missing-agent path at all) and MUST
    # NOT have run additional queries before flush — exactly two
    # SELECTs, mirroring the happy-path XTM-One test above.
    assert captured["execute_count"] == 2, (
        f"XTM One mode must still attempt the CTEM slug lookup even "
        f"when the agent is missing; got {captured['execute_count']}"
    )
