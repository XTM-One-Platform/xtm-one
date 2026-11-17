"""The OAuth scopes a stored token was granted (#9776).

A person can decline a scope at consent (Google's granular consent screen);
the token endpoint's ``scope`` field says what was granted. It is stored at
the code exchange and at refresh, compared with the requested scopes, shown by
get_integration and the REST answer, and audited on every (re)connect.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.api.oauth_callbacks as callbacks
from app.integrations.granted_scopes import (
    granted_scope_string,
    missing_scopes,
    record_granted_scopes,
    split_scopes,
)
from app.models.integration import Integration
from app.models.user import User
from app.services.builtin_tools.agent_builder import _oauth_scope_lines
from tests.integration_credential_fakes import OWNER_ID, FakeSession, make_integration

GMAIL = "https://www.googleapis.com/auth/gmail"
REQUESTED = f"openid email {GMAIL}.readonly {GMAIL}.modify"


# ---------------------------------------------------------------------------
# Parsing the token answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a b  a", ["a", "b"]),
        ("a,b", ["a", "b"]),
        (["a", " b ", "", 3], ["a", "b"]),
        (None, []),
        (42, []),
    ],
)
def test_split_scopes(value, expected):
    assert split_scopes(value) == expected


def test_an_answer_without_scope_says_nothing_about_the_grant():
    assert granted_scope_string({"access_token": "t"}) is None
    assert granted_scope_string({"access_token": "t", "scope": "a  b"}) == "a b"
    assert granted_scope_string({"access_token": "t", "scope": ""}) == ""


def test_the_exchange_replaces_the_grant_and_a_refresh_without_scope_keeps_it():
    holder = SimpleNamespace(granted_scopes="old")

    record_granted_scopes(holder, {"access_token": "t"}, keep_when_absent=True)
    assert holder.granted_scopes == "old"

    record_granted_scopes(holder, {"access_token": "t", "scope": "x y"}, keep_when_absent=True)
    assert holder.granted_scopes == "x y"

    record_granted_scopes(holder, {"access_token": "t"}, keep_when_absent=False)
    assert holder.granted_scopes is None


# ---------------------------------------------------------------------------
# What is missing
# ---------------------------------------------------------------------------


def test_a_declined_scope_is_missing():
    granted = f"{GMAIL}.readonly https://www.googleapis.com/auth/userinfo.email openid"
    assert missing_scopes(REQUESTED, granted) == [f"{GMAIL}.modify"]


def test_an_unknown_grant_reports_nothing_missing():
    assert missing_scopes(REQUESTED, None) is None


def test_identity_scopes_and_a_resource_prefix_are_not_missing():
    """Microsoft grants ``Mail.Read`` for ``https://graph.microsoft.com/Mail.Read``
    and drops ``offline_access``."""
    requested = "https://graph.microsoft.com/Mail.Read offline_access profile"
    assert missing_scopes(requested, "Mail.Read openid") == []


def test_an_optional_scope_the_provider_drops_is_not_missing():
    """A scope the catalog declares optional (#11033) never holds the row in
    re-authorization; a declined required one still does."""
    requested = "read:jira-work read:servicemanagement-insight-objects write:jira-work"
    optional = "read:servicemanagement-insight-objects"
    assert missing_scopes(requested, "read:jira-work write:jira-work", optional) == []
    assert missing_scopes(requested, "read:jira-work", optional) == ["write:jira-work"]
    assert missing_scopes(requested, "read:jira-work", None) == [
        "read:servicemanagement-insight-objects",
        "write:jira-work",
    ]


def test_a_google_scope_declined_at_consent_is_still_missing():
    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    entry = next(e for e in PROVIDER_CATALOG.values() if f"{GMAIL}.modify" in split_scopes(e.get("scopes")))
    granted = " ".join(s for s in entry["scopes"].split() if s != f"{GMAIL}.modify")
    assert missing_scopes(entry["scopes"], granted, entry.get("optional_scopes")) == [f"{GMAIL}.modify"]


def test_jira_no_longer_requests_the_insight_scope():
    from app.api.v1.integration_catalog import PROVIDER_CATALOG
    from app.integrations.providers.jira import JiraIntegration

    scope = "read:servicemanagement-insight-objects"
    entry = PROVIDER_CATALOG["jira"]
    assert scope not in entry["scopes"].split()
    assert scope not in {p["scope"] for p in entry["setup_guide"]["permissions"]}
    assert scope not in JiraIntegration.get_oauth_defaults(None).scopes


def test_every_optional_scope_is_a_requested_scope():
    """optional_scopes only softens scopes the entry requests: a stray name is a typo."""
    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    for provider, entry in PROVIDER_CATALOG.items():
        optional = split_scopes(entry.get("optional_scopes"))
        requested = set(split_scopes(entry.get("scopes")))
        assert set(optional) <= requested, provider


def test_the_migration_strips_the_insight_scope_from_stored_jira_rows():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "x11033jirascope1_drop_jira_insight_scope.py"
    spec = importlib.util.spec_from_file_location("x11033jirascope1", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    rows = [SimpleNamespace(id=1, scopes="read:me read:servicemanagement-insight-objects offline_access")]
    updates: list[dict] = []

    class _Result:
        def fetchall(self):
            return rows

    class _Conn:
        def execute(self, statement, params):
            if str(statement).startswith("UPDATE"):
                updates.append(params)
            else:
                assert "provider = 'jira'" in str(statement)
            return _Result()

    module.op = SimpleNamespace(get_bind=lambda: _Conn())
    module.upgrade()
    assert updates == [{"scopes": "read:me offline_access", "id": 1}]


# ---------------------------------------------------------------------------
# The OAuth callback stores and audits the grant
# ---------------------------------------------------------------------------


class _Session(FakeSession):
    def __init__(self, integrations, users):
        super().__init__({Integration: list(integrations)})
        self.users = {u.id: u for u in users}

    async def get(self, _model, key):
        return self.users.get(key)


@pytest.fixture
def _connect(monkeypatch):
    audits: list[dict] = []

    async def _audit(_db, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(callbacks, "log_audit", _audit)
    monkeypatch.setattr(callbacks, "encrypt_value", lambda value: f"enc:{value}")

    async def _run(integration, answer):
        monkeypatch.setattr(
            callbacks, "exchange_code_for_tokens_with_integration", AsyncMock(return_value=answer)
        )
        owner = User(
            id=OWNER_ID,
            email="owner@example.com",
            full_name="o",
            role="user",
            oauth_share_dismissed_integration_ids=[],
        )
        state = {"integration_id": str(integration.id), "user_id": str(OWNER_ID), "provider": "custom"}
        await callbacks._handle_integration_callback(state, "code", _Session([integration], [owner]))
        return [a for a in audits if a["action"] == "connect"]

    return _run


async def test_a_reconnect_with_a_declined_scope_is_stored_and_audited(_connect, monkeypatch):
    monkeypatch.setattr(callbacks, "integration_owns_oauth_scopes", lambda _provider: True)
    integration = make_integration(provider="custom", scopes=REQUESTED)

    (audit,) = await _connect(
        integration, {"access_token": "t", "refresh_token": "r", "scope": f"openid {GMAIL}.readonly"}
    )

    assert integration.granted_scopes == f"openid {GMAIL}.readonly"
    assert audit["entity_id"] == integration.id
    assert audit["details"]["missing_scopes"] == [f"{GMAIL}.modify"]
    assert audit["details"]["granted_scopes"] == ["openid", f"{GMAIL}.readonly"]
    assert "1 requested scope(s) not granted" in audit["summary"]


async def test_a_provider_that_reports_no_scope_leaves_the_grant_unknown(_connect, monkeypatch):
    monkeypatch.setattr(callbacks, "integration_owns_oauth_scopes", lambda _provider: True)
    integration = make_integration(provider="custom", scopes=REQUESTED, granted_scopes="stale")

    (audit,) = await _connect(integration, {"access_token": "t", "refresh_token": "r"})

    assert integration.granted_scopes is None
    assert audit["details"]["granted_scopes"] is None
    assert audit["details"]["missing_scopes"] is None


# ---------------------------------------------------------------------------
# get_integration
# ---------------------------------------------------------------------------


def test_get_integration_lists_the_missing_scope():
    integ = make_integration(
        provider="custom", scopes=REQUESTED, granted_scopes=f"openid email {GMAIL}.readonly"
    )

    lines = "\n".join(_oauth_scope_lines(integ))

    assert f"Requested scopes: {REQUESTED}" in lines
    assert f"Granted scopes: openid email {GMAIL}.readonly" in lines
    assert f"Missing scopes: {GMAIL}.modify (reconnect" in lines


def test_get_integration_says_when_the_grant_is_unknown():
    integ = make_integration(provider="custom", scopes=REQUESTED, granted_scopes=None)

    lines = "\n".join(_oauth_scope_lines(integ))

    assert "Granted scopes: unknown" in lines
    assert "Missing scopes" not in lines
