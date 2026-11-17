"""A GitHub App installation lands only on its installer's own integrations.

A GitHub App installed from GitHub's own UI redirects to ``/api/oauth/callback``
with ``code``, ``installation_id`` and ``setup_action=install`` but no
``state``. The callback used to write the installer's token and the
installation onto the first GitHub integration it found - a configured one,
else a company-managed one, else the most recent one - whoever owned it, and
to record whatever installation id the query string named. Now GitHub's own
answers about the exchanged token are the only proof: the installation is
recorded only when the installer's token sees it, only on connected
integrations of the same app whose own token is the installer's GitHub account
and that already record installations, each such write audited, and no token
is stored anywhere. Every owner here is an ordinary user.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BinaryExpression, BindParameter, False_, Null, True_

import app.api.oauth_callbacks as callbacks
import app.core.rate_limit as rate_limit_module
from app.core.database import get_db
from app.core.rate_limit import enforce_rate_limit as real_enforce_rate_limit
from app.core.security import decrypt_value, encrypt_value
from app.models.integration import Integration
from app.models.oauth_default import OAuthDefault
from app.models.user import User

# The ``github`` fixture routes every ``httpx.AsyncClient`` to the fake GitHub;
# the client that calls the mounted route is taken before it does.
_REAL_ASYNC_CLIENT = httpx.AsyncClient

ADMIN_CLIENT_ID = "admin-github-app-client-id"
INSTALLER_ACCOUNT = 2002
COLLEAGUE_ACCOUNT = 1001
INSTALLER_TOKEN = "ghu_installer"  # what the install code exchanges for

ALICE = uuid.uuid4()  # the installer's XTM One user
BOB = uuid.uuid4()  # a colleague


# ---------------------------------------------------------------------------
# A session that answers the callback's queries from in-memory rows
# ---------------------------------------------------------------------------


def _value(expr):
    if isinstance(expr, BindParameter):
        return expr.value
    if isinstance(expr, True_):
        return True
    if isinstance(expr, False_):
        return False
    if isinstance(expr, Null):
        return None
    raise NotImplementedError(repr(expr))


def _matches(row, criteria) -> bool:
    for criterion in criteria:
        if not isinstance(criterion, BinaryExpression):
            raise NotImplementedError(repr(criterion))
        actual = getattr(row, criterion.left.key)
        expected = _value(criterion.right)
        if criterion.operator is operators.eq:
            ok = actual == expected
        elif criterion.operator is operators.is_:
            ok = actual is expected or actual == expected
        elif criterion.operator is operators.is_not:
            ok = actual is not expected and actual != expected
        elif criterion.operator is operators.in_op:
            ok = actual in expected
        else:
            raise NotImplementedError(repr(criterion.operator))
        if not ok:
            return False
    return True


class _Session:
    def __init__(self, integrations, oauth_default, events=None, users=()):
        self.rows = {
            Integration: list(integrations),
            OAuthDefault: [oauth_default] if oauth_default else [],
            User: list(users),
        }
        self.commits = 0
        self.statements = []
        self.events = events if events is not None else []

    async def execute(self, stmt):
        self.statements.append(stmt)
        self.events.append("query")
        column = stmt.column_descriptions[0]
        entity = column["entity"]
        rows = [r for r in self.rows[entity] if _matches(r, stmt._where_criteria)]
        if stmt._order_by_clauses:
            descending = getattr(stmt._order_by_clauses[0], "modifier", None) is operators.desc_op
            rows.sort(key=lambda r: r.created_at, reverse=descending)
        if column["expr"] is not entity:
            rows = [getattr(r, column["name"]) for r in rows]
        return SimpleNamespace(
            scalar_one_or_none=lambda: rows[0] if rows else None,
            scalars=lambda: SimpleNamespace(first=lambda: rows[0] if rows else None, all=lambda: list(rows)),
        )

    async def get(self, model, key):
        """The user the connect audit names (not recorded as a query event)."""
        return next((r for r in self.rows.get(model, []) if r.id == key), None)

    def in_transaction(self):
        return True

    async def commit(self):
        self.commits += 1
        self.events.append("commit")

    async def rollback(self):
        self.events.append("rollback")

    async def refresh(self, _obj):
        self.events.append("query")


def _default():
    return OAuthDefault(
        provider="github",
        client_id_enc=encrypt_value(ADMIN_CLIENT_ID),
        client_secret_enc=encrypt_value("admin-client-secret"),
    )


_created = iter(range(1, 10_000))


def _integration(
    owner,
    *,
    token=None,
    installations=None,
    client_id=ADMIN_CLIENT_ID,
    status="connected",
    company_managed=False,
):
    extra = {"installations": [{"id": str(i)} for i in installations]} if installations else None
    return Integration(
        id=uuid.uuid4(),
        provider="github",
        name="GitHub",
        user_id=owner,
        auth_type="oauth",
        status=status,
        is_company_managed=company_managed,
        client_id_enc=encrypt_value(client_id) if client_id else None,
        client_secret_enc=encrypt_value("client-secret"),
        access_token_enc=encrypt_value(token) if token else None,
        refresh_token_enc=encrypt_value(f"refresh-{token}") if token else None,
        extra_config_enc=encrypt_value(json.dumps(extra)) if extra else None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=next(_created)),
    )


# Everything a state-less install must leave as it is, but the installations.
_OWN_FIELDS = (
    "access_token_enc",
    "refresh_token_enc",
    "status",
    "user_id",
    "token_expires_at",
    "oauth_share_acknowledged",
)


def _snapshot(integration, fields=(*_OWN_FIELDS, "extra_config_enc")):
    return tuple(getattr(integration, field) for field in fields)


def _recorded(integration):
    if not integration.extra_config_enc:
        return None
    extra = json.loads(decrypt_value(integration.extra_config_enc))
    return [i["id"] for i in extra.get("installations", [])]


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


class _GitHub:
    """The token exchange, ``GET /user`` and ``GET /user/installations``."""

    def __init__(
        self,
        *,
        accounts,
        visible,
        installer_token=INSTALLER_TOKEN,
        user_status=200,
        on_user=None,
        installations_delay=0.0,
        slow_users=(),
        events=None,
        installations_status=200,
    ):
        self.installations_status = installations_status
        self.accounts = accounts  # token -> GitHub account id
        self.visible = visible  # token -> installation ids it sees
        self.installer_token = installer_token
        self.user_status = user_status
        self.on_user = on_user  # called with the token of every GET /user
        self.installations_delay = installations_delay
        self.slow_users = set(slow_users)  # tokens whose GET /user never answers in time
        self.events = events if events is not None else []
        self.exchanges: list[dict] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.events.append("github")
        if request.url.host == "github.com" and request.url.path == "/login/oauth/access_token":
            self.exchanges.append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(
                200, json={"access_token": self.installer_token, "refresh_token": "ghr_installer"}
            )
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if request.url.path == "/user":
            if self.on_user is not None:
                self.on_user(token)
            if token in self.slow_users:
                await asyncio.sleep(5.0)
            if self.user_status != 200 or token not in self.accounts:
                return httpx.Response(self.user_status if self.user_status != 200 else 401)
            return httpx.Response(200, json={"id": self.accounts[token], "login": f"user-{token}"})
        if request.url.path == "/user/installations":
            if self.installations_delay:
                await asyncio.sleep(self.installations_delay)
            if self.installations_status != 200:
                return httpx.Response(self.installations_status)
            ids = self.visible.get(token, [])
            return httpx.Response(
                200,
                json={
                    "total_count": len(ids),
                    "installations": [
                        {"id": i, "account": {"login": f"org-{i}", "type": "Organization"}} for i in ids
                    ],
                },
            )
        return httpx.Response(404)


@pytest.fixture
def github(monkeypatch):
    holder: dict = {}
    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs.pop("limits", None)
        return real_client(*args, transport=httpx.MockTransport(holder["gh"].handler), **kwargs)

    monkeypatch.setattr(callbacks.httpx, "AsyncClient", _client)

    def _configure(**kwargs):
        holder["gh"] = _GitHub(**kwargs)
        return holder["gh"]

    return _configure


@pytest.fixture(autouse=True)
def audits(monkeypatch):
    """The system audit entries the callback writes (the session here has no add / flush)."""
    entries: list[dict] = []

    async def _record(_db, **kwargs):
        entries.append(kwargs)

    monkeypatch.setattr(callbacks, "log_system_audit", _record)
    return entries


@pytest.fixture(autouse=True)
def rate_limits(monkeypatch):
    """The rate limits the callback asks for; the limiter itself is tested with a fake Redis below."""
    calls: list[tuple[str, str | None]] = []

    async def _record(_request, name, **kwargs):
        calls.append((name, kwargs.get("bucket")))

    monkeypatch.setattr(callbacks, "enforce_rate_limit", _record)
    return calls


async def _install(db, installation_id="222"):
    request = SimpleNamespace(
        query_params={"code": "install-code", "installation_id": installation_id, "setup_action": "install"}
    )
    return await callbacks.oauth_callback(request, db=db)


def _location(response):
    return response.headers["location"]


# ---------------------------------------------------------------------------
# Never another user's integration
# ---------------------------------------------------------------------------


async def test_a_stateless_install_never_lands_on_a_colleagues_waiting_integration(github):
    gh = github(accounts={"ghu_installer": INSTALLER_ACCOUNT}, visible={"ghu_installer": [222]})
    waiting = _integration(BOB, status="configured")
    before = _snapshot(waiting)
    db = _Session([waiting], _default())

    response = await _install(db)

    assert _snapshot(waiting) == before
    assert _location(response).endswith("/integrations?github_installed=true")
    # The code is still exchanged with the admin app's credentials.
    assert gh.exchanges[0]["client_id"] == ADMIN_CLIENT_ID


async def test_a_stateless_install_never_takes_over_a_colleagues_connected_integration(github, audits):
    github(
        accounts={"ghu_installer": INSTALLER_ACCOUNT, "ghu_bob": COLLEAGUE_ACCOUNT},
        visible={"ghu_installer": [222]},
    )
    colleague = _integration(BOB, token="ghu_bob", installations=[111])
    before = _snapshot(colleague)

    response = await _install(_Session([colleague], _default()))

    assert _snapshot(colleague) == before
    assert _location(response).endswith("/integrations?github_installed=true")
    assert audits == []


@pytest.mark.parametrize("owner", [None, BOB], ids=["owner_less", "owned_by_a_colleague"])
async def test_a_stateless_install_never_takes_over_a_company_managed_integration(github, audits, owner):
    github(
        accounts={"ghu_installer": INSTALLER_ACCOUNT, "ghu_bob": COLLEAGUE_ACCOUNT},
        visible={"ghu_installer": [222]},
    )
    shared = _integration(owner, token="ghu_bob", installations=[111], company_managed=True)
    before = _snapshot(shared)

    await _install(_Session([shared], _default()))

    assert _snapshot(shared) == before
    assert audits == []


async def test_an_integration_of_another_app_is_never_a_target(github):
    """Same GitHub account, but the integration runs on the user's own app:
    the code was exchanged for the admin app, whose installation it is not."""
    github(accounts={"ghu_installer": INSTALLER_ACCOUNT}, visible={"ghu_installer": [222]})
    own_app = _integration(ALICE, token="ghu_installer", installations=[111], client_id="own-app")
    before = _snapshot(own_app)

    await _install(_Session([own_app], _default()))

    assert _snapshot(own_app) == before


async def test_an_installation_the_installer_cannot_see_is_recorded_nowhere(github):
    """A crafted callback: the installer's own code with another organisation's
    installation id."""
    github(accounts={"ghu_installer": INSTALLER_ACCOUNT}, visible={"ghu_installer": [222]})
    own = _integration(ALICE, token="ghu_installer", installations=[111])
    before = _snapshot(own)

    response = await _install(_Session([own], _default()), installation_id="999")

    assert _snapshot(own) == before
    assert _location(response).endswith("/integrations?github_installed=true")


async def test_nothing_is_written_when_github_cannot_say_who_installed(github, audits):
    github(accounts={}, visible={"ghu_installer": [222]}, user_status=503)
    own = _integration(ALICE, token="ghu_installer", installations=[111])
    before = _snapshot(own)

    await _install(_Session([own], _default()))

    assert _snapshot(own) == before
    assert audits == []


@pytest.mark.parametrize(
    "installation_id",
    ["abc", "222\nforged log line", "1" * 21, "\u0662\u0662\u0662"],
    ids=["letters", "newline", "too_long", "non_ascii_digits"],
)
async def test_a_malformed_installation_id_is_refused_before_github_is_asked(github, installation_id):
    events: list[str] = []
    gh = github(accounts={"ghu_installer": INSTALLER_ACCOUNT}, visible={"ghu_installer": [222]}, events=events)
    own = _integration(ALICE, token="ghu_installer", installations=[111])
    before = _snapshot(own)

    response = await _install(_Session([own], _default()), installation_id=installation_id)

    assert _snapshot(own) == before
    assert gh.exchanges == [] and events == []
    assert _location(response).endswith("/integrations?github_installed=true")


@pytest.mark.parametrize(
    "status",
    ["error", "reauthorize", "disconnected", "configured"],
    ids=["error", "reauthorize", "disconnected", "configured"],
)
async def test_an_integration_that_is_not_connected_is_never_a_target(github, status):
    """Its old token may still be stored and still answer the installer's
    account: it is reconnected from its page, never by an install."""
    github(accounts={"ghu_installer": INSTALLER_ACCOUNT, "ghu_alice": INSTALLER_ACCOUNT}, visible={"ghu_installer": [222]})
    own = _integration(ALICE, token="ghu_alice", installations=[111], status=status)
    before = _snapshot(own)

    response = await _install(_Session([own], _default()))

    assert _snapshot(own) == before
    assert _location(response).endswith("/integrations?github_installed=true")


async def test_an_integration_whose_token_github_refuses_is_left_alone(github):
    github(accounts={"ghu_installer": INSTALLER_ACCOUNT}, visible={"ghu_installer": [222]})
    own = _integration(ALICE, token="ghu_revoked", installations=[111])
    before = _snapshot(own)

    await _install(_Session([own], _default()))

    assert _snapshot(own) == before


# ---------------------------------------------------------------------------
# The installer's own integrations
# ---------------------------------------------------------------------------


async def test_a_reinstall_is_recorded_on_the_installers_own_integration(github, audits):
    """No regression: the integration connected with the installer's GitHub
    account keeps its token and records the new installation, whatever
    colleagues' integrations exist."""
    github(
        accounts={"ghu_installer": INSTALLER_ACCOUNT, "ghu_alice": INSTALLER_ACCOUNT, "ghu_bob": COLLEAGUE_ACCOUNT},
        visible={"ghu_installer": [222]},
    )
    own = _integration(ALICE, token="ghu_alice", installations=[111])
    colleague = _integration(BOB, token="ghu_bob", installations=[111])
    waiting = _integration(BOB, status="configured")
    colleague_before, waiting_before = _snapshot(colleague), _snapshot(waiting)
    own_before = _snapshot(own, _OWN_FIELDS)
    db = _Session([own, colleague, waiting], _default())

    response = await _install(db)

    assert _recorded(own) == ["111", "222"]
    extra = json.loads(decrypt_value(own.extra_config_enc))
    assert extra["installations"][-1] == {"id": "222", "account_login": "org-222", "account_type": "Organization"}
    assert _snapshot(own, _OWN_FIELDS) == own_before
    assert _snapshot(colleague) == colleague_before
    assert _snapshot(waiting) == waiting_before
    assert db.commits >= 1
    assert _location(response).endswith("/integrations?connected=github")
    (audit,) = audits
    assert audit["actor"] == "github_app_install"
    assert audit["anchor_user_id"] == ALICE
    assert (audit["entity_type"], audit["entity_id"], audit["action"]) == ("integration", own.id, "update")
    assert audit["details"] == {
        "installation_id": "222",
        "installation_account": "org-222",
        "installation_account_type": "Organization",
    }


def _user(role, minutes):
    return SimpleNamespace(
        id=uuid.uuid4(), role=role, created_at=datetime(2025, 1, 1, tzinfo=UTC) + timedelta(minutes=minutes)
    )


async def test_an_owner_less_integration_is_audited_on_the_oldest_admin(github, audits):
    """An owner-less company-managed row connected with the installer's account
    records it too, audited on the oldest admin: admins answer for
    company-managed configuration, as for the platform's other actions on it."""
    github(accounts={"ghu_installer": INSTALLER_ACCOUNT, "ghu_admin": INSTALLER_ACCOUNT}, visible={"ghu_installer": [222]})
    shared = _integration(None, token="ghu_admin", installations=[111], company_managed=True)
    newer_admin, oldest_admin, member = _user("admin", 20), _user("admin", 10), _user("user", 0)

    await _install(_Session([shared], _default(), users=[newer_admin, oldest_admin, member]))

    assert _recorded(shared) == ["111", "222"]
    (audit,) = audits
    assert audit["anchor_user_id"] == oldest_admin.id
    assert audit["entity_id"] == shared.id


async def test_an_owner_less_integration_is_audited_on_whoever_connected_its_token(github, audits):
    github(accounts={"ghu_installer": INSTALLER_ACCOUNT, "ghu_admin": INSTALLER_ACCOUNT}, visible={"ghu_installer": [222]})
    holder, admin = _user("user", 30), _user("admin", 10)
    shared = _integration(None, token="ghu_admin", installations=[111], company_managed=True)
    shared.oauth_token_owner_id = holder.id

    await _install(_Session([shared], _default(), users=[holder, admin]))

    (audit,) = audits
    assert audit["anchor_user_id"] == holder.id


async def test_an_owner_less_integration_with_no_admin_to_anchor_on_is_logged(github, audits, caplog):
    """Only a platform with no admin at all: the entry needs a user, none is invented."""
    github(accounts={"ghu_installer": INSTALLER_ACCOUNT, "ghu_admin": INSTALLER_ACCOUNT}, visible={"ghu_installer": [222]})
    shared = _integration(None, token="ghu_admin", installations=[111], company_managed=True)
    shared.oauth_token_owner_id = uuid.uuid4()  # a deleted user

    with caplog.at_level("WARNING", logger=callbacks.logger.name):
        await _install(_Session([shared], _default(), users=[_user("user", 0)]))

    assert _recorded(shared) == ["111", "222"]
    assert audits == []
    assert "no admin exists to anchor its audit entry on" in caplog.text


async def test_every_integration_of_the_installers_account_records_it(github, audits):
    github(
        accounts={"ghu_installer": INSTALLER_ACCOUNT, "ghu_alice_1": INSTALLER_ACCOUNT, "ghu_alice_2": INSTALLER_ACCOUNT},
        visible={"ghu_installer": [222]},
    )
    first = _integration(ALICE, token="ghu_alice_1", installations=[111])
    second = _integration(ALICE, token="ghu_alice_2", installations=[333])

    response = await _install(_Session([first, second], _default()))

    assert _recorded(first) == ["111", "222"]
    assert _recorded(second) == ["333", "222"]
    assert _location(response).endswith("/integrations?connected=github")
    assert sorted(str(a["entity_id"]) for a in audits) == sorted([str(first.id), str(second.id)])


async def test_an_integration_recording_no_installation_is_left_as_is(github):
    """It is not scoped to installations: recording one would narrow the events
    it receives to that installation."""
    github(accounts={"ghu_installer": INSTALLER_ACCOUNT, "ghu_alice": INSTALLER_ACCOUNT}, visible={"ghu_installer": [222]})
    own = _integration(ALICE, token="ghu_alice")
    before = _snapshot(own)

    response = await _install(_Session([own], _default()))

    assert _snapshot(own) == before
    assert _location(response).endswith("/integrations?github_installed=true")


async def test_a_legacy_single_installation_is_migrated_with_the_new_one(github):
    github(accounts={"ghu_installer": INSTALLER_ACCOUNT, "ghu_alice": INSTALLER_ACCOUNT}, visible={"ghu_installer": [222]})
    own = _integration(ALICE, token="ghu_alice")
    own.extra_config_enc = encrypt_value(json.dumps({"installation_id": "111"}))

    await _install(_Session([own], _default()))

    extra = json.loads(decrypt_value(own.extra_config_enc))
    assert "installation_id" not in extra
    assert [i["id"] for i in extra["installations"]] == ["111", "222"]


# ---------------------------------------------------------------------------
# The proof holds no connection and no stale row, and is bounded
# ---------------------------------------------------------------------------


async def test_no_pooled_connection_is_held_while_github_is_asked(github):
    events: list[str] = []
    github(
        accounts={"ghu_installer": INSTALLER_ACCOUNT, "ghu_alice": INSTALLER_ACCOUNT},
        visible={"ghu_installer": [222]},
        events=events,
    )
    own = _integration(ALICE, token="ghu_alice", installations=[111])

    await _install(_Session([own], _default(), events))

    # Every GitHub call sits between a commit (the connection handed back) and
    # the next query: none is made inside a read transaction.
    in_transaction = False
    for event in events:
        if event == "query":
            in_transaction = True
        elif event in ("commit", "rollback"):
            in_transaction = False
        else:
            assert not in_transaction, events
    assert _recorded(own) == ["111", "222"]


async def test_the_rows_are_read_again_under_lock_before_the_write(github):
    github(
        accounts={"ghu_installer": INSTALLER_ACCOUNT, "ghu_alice": INSTALLER_ACCOUNT},
        visible={"ghu_installer": [222]},
    )
    own = _integration(ALICE, token="ghu_alice", installations=[111])
    db = _Session([own], _default())

    await _install(db)

    last_read = db.statements[-1]
    assert last_read._for_update_arg is not None
    assert last_read.get_execution_options().get("populate_existing") is True


async def test_an_integration_reconnected_with_another_account_during_the_proof_is_left_alone(github, audits, caplog):
    """The proof was about the token it held; the new one is asked once, and is not the installer's."""
    own = _integration(ALICE, token="ghu_alice", installations=[111])
    asked: list[str] = []

    def _reconnect(token):
        asked.append(token)
        if token == "ghu_alice":
            own.access_token_enc = encrypt_value("ghu_bob")

    github(
        accounts={"ghu_installer": INSTALLER_ACCOUNT, "ghu_alice": INSTALLER_ACCOUNT, "ghu_bob": COLLEAGUE_ACCOUNT},
        visible={"ghu_installer": [222]},
        on_user=_reconnect,
    )

    with caplog.at_level("INFO", logger=callbacks.logger.name):
        response = await _install(_Session([own], _default()))

    assert _recorded(own) == ["111"]
    assert _location(response).endswith("/integrations?github_installed=true")
    assert audits == []
    assert asked == ["ghu_installer", "ghu_alice", "ghu_bob"]
    assert "is not the installer's GitHub account" in caplog.text


async def test_a_token_refreshed_during_the_proof_is_proven_again_and_recorded(github, audits):
    """A refresh between GitHub's answer and the write replaces the token with
    another of the same account: asked once more, with no connection held,
    and the installation is recorded and audited."""
    events: list[str] = []
    own = _integration(ALICE, token="ghu_alice", installations=[111])

    def _refresh(token):
        if token == "ghu_alice":
            own.access_token_enc = encrypt_value("ghu_alice_refreshed")

    github(
        accounts={"ghu_installer": INSTALLER_ACCOUNT, "ghu_alice": INSTALLER_ACCOUNT, "ghu_alice_refreshed": INSTALLER_ACCOUNT},
        visible={"ghu_installer": [222]},
        on_user=_refresh,
        events=events,
    )
    db = _Session([own], _default(), events)
    response = await _install(db)

    assert _recorded(own) == ["111", "222"]
    assert decrypt_value(own.access_token_enc) == "ghu_alice_refreshed"
    assert _location(response).endswith("/integrations?connected=github")
    assert [audit["entity_id"] for audit in audits] == [own.id]
    in_transaction = False
    for event in events:
        if event == "query":
            in_transaction = True
        elif event in ("commit", "rollback"):
            in_transaction = False
        else:
            assert not in_transaction, events
    assert db.statements[-1]._for_update_arg is not None


async def test_a_token_that_changes_again_during_the_second_proof_is_left_alone(github, audits, caplog):
    own = _integration(ALICE, token="ghu_alice", installations=[111])
    next_token = {"ghu_alice": "ghu_alice_2", "ghu_alice_2": "ghu_alice_3"}

    def _refresh(token):
        if token in next_token:
            own.access_token_enc = encrypt_value(next_token[token])

    github(
        accounts={
            "ghu_installer": INSTALLER_ACCOUNT,
            "ghu_alice": INSTALLER_ACCOUNT,
            "ghu_alice_2": INSTALLER_ACCOUNT,
            "ghu_alice_3": INSTALLER_ACCOUNT,
        },
        visible={"ghu_installer": [222]},
        on_user=_refresh,
    )

    with caplog.at_level("INFO", logger=callbacks.logger.name):
        response = await _install(_Session([own], _default()))

    assert _recorded(own) == ["111"]
    assert _location(response).endswith("/integrations?github_installed=true")
    assert audits == []
    assert "changed again while GitHub was asked" in caplog.text


async def test_an_integration_marked_during_the_proof_is_left_alone(github, audits):
    """A refresh may mark the row reauthorize while GitHub is asked, keeping its
    token: the row read again under lock is no longer a target."""
    own = _integration(ALICE, token="ghu_alice", installations=[111])

    def _mark(token):
        if token == "ghu_alice":
            own.status = "reauthorize"

    github(
        accounts={"ghu_installer": INSTALLER_ACCOUNT, "ghu_alice": INSTALLER_ACCOUNT},
        visible={"ghu_installer": [222]},
        on_user=_mark,
    )

    response = await _install(_Session([own], _default()))

    assert _recorded(own) == ["111"]
    assert _location(response).endswith("/integrations?github_installed=true")
    assert audits == []


async def test_a_candidate_github_does_not_answer_for_in_time_is_left_alone(github, monkeypatch):
    monkeypatch.setattr(callbacks, "_INSTALL_PROOF_BUDGET_SECS", 0.5)
    github(
        accounts={"ghu_installer": INSTALLER_ACCOUNT, "ghu_alice": INSTALLER_ACCOUNT},
        visible={"ghu_installer": [222]},
        slow_users={"ghu_alice"},
    )
    own = _integration(ALICE, token="ghu_alice", installations=[111])
    before = _snapshot(own)

    started = time.monotonic()
    response = await _install(_Session([own], _default()))

    assert time.monotonic() - started < 3.0
    assert _snapshot(own) == before
    assert _location(response).endswith("/integrations?github_installed=true")


async def test_one_deadline_bounds_the_whole_proof(github, monkeypatch):
    monkeypatch.setattr(callbacks, "_INSTALL_PROOF_BUDGET_SECS", 0.2)
    github(
        accounts={"ghu_installer": INSTALLER_ACCOUNT, "ghu_alice": INSTALLER_ACCOUNT},
        visible={"ghu_installer": [222]},
        installations_delay=5.0,
    )
    own = _integration(ALICE, token="ghu_alice", installations=[111])
    before = _snapshot(own)

    started = time.monotonic()
    response = await _install(_Session([own], _default()))

    assert time.monotonic() - started < 2.0
    assert _snapshot(own) == before
    assert _location(response).endswith("/integrations?github_installed=true")


# ---------------------------------------------------------------------------
# GitHub's redirect, through the mounted route
# ---------------------------------------------------------------------------


def _mounted(db):
    app = FastAPI()
    app.include_router(callbacks.router, prefix="/api/oauth")
    app.dependency_overrides[get_db] = lambda: db
    return _REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


_GITHUB_REDIRECT = {"code": "install-code", "installation_id": "222", "setup_action": "install"}


async def test_githubs_redirect_through_the_mounted_route_lands_on_the_installers_row_only(
    github, audits, rate_limits
):
    """The query string GitHub sends, parsed by FastAPI's routing, not handed to the handler.

    The rows are those an older callback wrote onto: a colleague's
    waiting integration first, else the most recent one - the colleague's
    connected integration, created after the installer's. It stored the
    installer's token on one of them; this fails there and passes now.
    """
    github(
        accounts={"ghu_installer": INSTALLER_ACCOUNT, "ghu_alice": INSTALLER_ACCOUNT, "ghu_bob": COLLEAGUE_ACCOUNT},
        visible={"ghu_installer": [222]},
    )
    own = _integration(ALICE, token="ghu_alice", installations=[111])
    colleague = _integration(BOB, token="ghu_bob", installations=[111])
    waiting = _integration(BOB, status="configured")
    assert colleague.created_at > own.created_at
    own_before = _snapshot(own, _OWN_FIELDS)
    colleague_before, waiting_before = _snapshot(colleague), _snapshot(waiting)
    db = _Session([own, colleague, waiting], _default())

    async with _mounted(db) as client:
        response = await client.get("/api/oauth/callback", params=_GITHUB_REDIRECT)

    assert response.status_code == 307
    assert _snapshot(waiting) == waiting_before
    assert _snapshot(colleague) == colleague_before
    assert _recorded(own) == ["111", "222"]
    assert _snapshot(own, _OWN_FIELDS) == own_before
    assert [audit["entity_id"] for audit in audits] == [own.id]
    # The browser's user is unknown here: no integration is named.
    assert _location(response).endswith("/integrations?connected=github")
    assert rate_limits == [("github-app-install", None), ("github-app-install-installation", "222")]


class _FakeRedis:
    """Counts calls per key; it does not run the Lua script, which is not what is tested here."""

    def __init__(self):
        self.counts: dict[str, int] = {}

    async def eval(self, _script, _numkeys, key, _window):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def ttl(self, _key):
        return 123


@pytest.fixture
def real_rate_limit(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(callbacks, "enforce_rate_limit", real_enforce_rate_limit)
    monkeypatch.setattr(rate_limit_module, "redis_client", AsyncMock(return_value=redis))
    return redis


def test_the_limits_are_far_above_a_legitimate_install():
    """One install is one callback; a retried one a few."""
    assert callbacks._INSTALL_CALLBACKS_PER_INSTALLATION >= 10
    assert callbacks._INSTALL_CALLBACKS_PER_CLIENT >= 30
    assert callbacks._INSTALL_CALLBACK_WINDOW_SECS <= 600


@pytest.mark.parametrize(
    ("limit_name", "installation_ids"),
    [("_INSTALL_CALLBACKS_PER_INSTALLATION", ["222", "222", "222"]), ("_INSTALL_CALLBACKS_PER_CLIENT", ["222", "333", "444"])],
    ids=["per_installation", "per_client"],
)
async def test_a_callback_past_the_limit_is_answered_429_before_github_is_asked(
    github, monkeypatch, real_rate_limit, limit_name, installation_ids
):
    monkeypatch.setattr(callbacks, limit_name, 2)
    gh = github(
        accounts={"ghu_installer": INSTALLER_ACCOUNT, "ghu_alice": INSTALLER_ACCOUNT},
        visible={"ghu_installer": [222, 333, 444]},
    )
    own = _integration(ALICE, token="ghu_alice", installations=[111])
    db = _Session([own], _default())

    async with _mounted(db) as client:
        answers = [
            await client.get("/api/oauth/callback", params={**_GITHUB_REDIRECT, "installation_id": installation_id})
            for installation_id in installation_ids
        ]

    assert [answer.status_code for answer in answers] == [307, 307, 429]
    assert answers[-1].headers["Retry-After"] == "123"
    assert len(gh.exchanges) == 2


# ---------------------------------------------------------------------------
# The flows that carry a state, and installation updates, are unchanged
# ---------------------------------------------------------------------------


async def test_an_install_started_from_the_app_touches_only_the_integration_it_names(monkeypatch):
    own = _integration(ALICE, status="configured")
    colleague = _integration(BOB, token="ghu_bob", installations=[111])
    colleague_before = _snapshot(colleague)
    db = _Session([own, colleague], _default())
    monkeypatch.setattr(
        callbacks,
        "get_state_data",
        AsyncMock(return_value={"integration_id": str(own.id), "user_id": str(ALICE), "provider": "github"}),
    )
    monkeypatch.setattr(
        callbacks,
        "exchange_code_for_tokens_with_integration",
        AsyncMock(return_value={"access_token": "ghu_alice_new", "refresh_token": "ghr_alice_new"}),
    )
    request = SimpleNamespace(
        query_params={
            "code": "c",
            "state": "signed-state",
            "installation_id": "222",
            "setup_action": "install",
        }
    )

    response = await callbacks.oauth_callback(request, db=db)

    assert decrypt_value(own.access_token_enc) == "ghu_alice_new"
    assert own.status == "connected"
    assert _snapshot(colleague) == colleague_before
    assert _location(response).endswith(f"/integrations/{own.id}?connected=github")


# ---------------------------------------------------------------------------
# Connect records the installations its token sees, under verified notifications
# ---------------------------------------------------------------------------


async def _connect(monkeypatch, db, integration, *, verified):
    """The Connect callback for *integration*, whose code exchanges for ``ghu_bot_new``."""
    import app.services.integration_notification_auth as notification_auth
    import app.services.token_refresh as token_refresh

    monkeypatch.setattr(notification_auth, "admin_app_scoping_required", lambda: verified)
    monkeypatch.setattr(token_refresh, "clear_refresh_cooldown", AsyncMock())
    monkeypatch.setattr(
        callbacks,
        "get_state_data",
        AsyncMock(return_value={"integration_id": str(integration.id), "user_id": str(ALICE), "provider": "github"}),
    )
    monkeypatch.setattr(
        callbacks,
        "exchange_code_for_tokens_with_integration",
        AsyncMock(return_value={"access_token": "ghu_bot_new", "refresh_token": "ghr_new"}),
    )
    request = SimpleNamespace(query_params={"code": "c", "state": "signed-state"})
    return await callbacks.oauth_callback(request, db=db)


async def test_connect_records_every_installation_its_token_sees_under_verified_notifications(github, monkeypatch):
    """An integration connected with a bot account learns of an installation
    someone else made from GitHub when it is connected again: the installations
    its own new token sees are added, the ones it records are kept."""
    events: list[str] = []
    github(accounts={}, visible={"ghu_bot_new": [111, 222, 333]}, events=events)
    own = _integration(ALICE, token="ghu_bot_old", installations=[111, 999])
    colleague = _integration(BOB, token="ghu_bob", installations=[111])
    colleague_before = _snapshot(colleague)

    response = await _connect(monkeypatch, _Session([own, colleague], _default(), events), own, verified=True)

    assert _recorded(own) == ["111", "999", "222", "333"]
    extra = json.loads(decrypt_value(own.extra_config_enc))
    assert extra["installations"][0] == {"id": "111"}
    assert extra["installations"][2] == {"id": "222", "account_login": "org-222", "account_type": "Organization"}
    assert own.status == "connected" and decrypt_value(own.access_token_enc) == "ghu_bot_new"
    assert _snapshot(colleague) == colleague_before
    # GitHub is asked between a commit (the connection handed back) and the
    # row read again, before anything is written; then the Connect commits.
    assert events == ["query", "commit", "github", "query", "commit"]
    assert _location(response).endswith(f"/integrations/{own.id}?connected=github")


@pytest.mark.parametrize(
    ("verified", "installations"),
    [(False, [111]), (True, None)],
    ids=["verification_off", "records_no_installation"],
)
async def test_connect_asks_github_nothing_otherwise(github, monkeypatch, verified, installations):
    """Off, Connect is what it was; an integration recording none is not narrowed."""
    events: list[str] = []
    github(accounts={}, visible={"ghu_bot_new": [111, 222]}, events=events)
    own = _integration(ALICE, token="ghu_bot_old", installations=installations)
    extra_before = own.extra_config_enc

    await _connect(monkeypatch, _Session([own], _default()), own, verified=verified)

    assert own.extra_config_enc == extra_before
    assert own.status == "connected"
    assert events == []


@pytest.mark.parametrize("installations_status", [200, 503], ids=["sees_none", "github_error"])
async def test_connect_keeps_the_installations_when_github_lists_none(github, monkeypatch, installations_status):
    github(accounts={}, visible={}, installations_status=installations_status)
    own = _integration(ALICE, token="ghu_bot_old", installations=[111])
    extra_before = own.extra_config_enc

    await _connect(monkeypatch, _Session([own], _default()), own, verified=True)

    assert own.extra_config_enc == extra_before
    assert own.status == "connected"


async def test_an_installation_update_is_answered_as_before():
    request = SimpleNamespace(query_params={"installation_id": "222", "setup_action": "update"})

    with pytest.raises(HTTPException) as exc:
        await callbacks.oauth_callback(request, db=_Session([], _default()))

    assert exc.value.status_code == 400
