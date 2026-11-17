"""Sender authentication for the shared Slack and Graph notification endpoints.

``app/services/integration_notification_auth.py`` binds every Slack integration to the
signing secret of the app it runs on, which is what proves a delivery to
``/api/v1/integrations/notifications/slack`` and decides which integrations it may reach,
and it owns the Graph subscription record whose ``clientState`` a change
notification to ``/api/v1/integrations/notifications/microsoft`` must echo. The endpoint
behaviour itself is pinned through the ASGI stack in ``test_notification_endpoint_auth``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import string
import time
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.api.v1 import security_audit
from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.core import log_once
from app.core.config import settings
from app.core.security import decrypt_value, encrypt_value
from app.services import integration_notification_auth as auth
from app.services.integration_notification_auth import (
    GRAPH_NOTIFICATION_PATH,
    SLACK_NOTIFICATION_PATH,
    SlackAppSecret,
    SlackSigningSecrets,
)
from tests.notice_store import new_process, share_notices

LOGGER = "app.services.integration_notification_auth"
BODY = b'{"type": "event_callback", "event": {"type": "message", "text": "hello"}}'


@pytest.fixture(autouse=True)
def _fresh_process_state():
    auth._log_state.clear()
    auth.invalidate_slack_signing_secrets()
    yield
    auth._log_state.clear()
    auth.invalidate_slack_signing_secrets()


@pytest.fixture(autouse=True)
def _require_verification(monkeypatch):
    """Unless a test asks for ``compat``, it pins INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION=true."""
    monkeypatch.setattr(settings, "integration_notifications_require_verification", True)


@pytest.fixture
def compat(_require_verification, monkeypatch):
    """The default: unverifiable integrations keep receiving what they received before verification."""
    monkeypatch.setattr(settings, "integration_notifications_require_verification", False)


def _sign(body: bytes, secret: str, *, timestamp: str | None = None) -> tuple[str, str]:
    ts = str(int(time.time())) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return f"v0={digest}", ts


def _config(**values) -> str:
    return encrypt_value(json.dumps(values))


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _Session:
    """Answers each ``select`` with the rows of its table.

    ``integrations`` rows are ``(id, client_id_enc, extra_config_enc,
    uses_admin_oauth_credentials)``, the ``oauth_defaults`` row is
    ``(client_id_enc, extra_config_enc)``.
    """

    def __init__(self, tables: dict[str, list]):
        self.tables = tables

    async def execute(self, statement, *_args, **_kwargs):
        table = statement.column_descriptions[0]["entity"].__tablename__
        return _Rows(self.tables.get(table, []))


async def _load(**tables) -> SlackSigningSecrets:
    return await auth.load_slack_signing_secrets(_Session(tables))


# ---------------------------------------------------------------------------
# Every Slack integration is bound to the signing secret of the app it runs on
# ---------------------------------------------------------------------------

ADMIN_CLIENT_ID = "1111.2222"


def _slack_row(
    secret: object = None,
    *,
    client_id: str = "3333.4444",
    config: str | None = None,
    recorded: bool | None = None,
):
    """A Slack integration row; *recorded* is ``uses_admin_oauth_credentials`` (``None``: a
    row created before it was recorded, judged by its client id)."""
    integration_id = uuid.uuid4()
    extra = config if config is not None else (_config(webhook_secret=secret) if secret is not None else None)
    return integration_id, encrypt_value(client_id), extra, recorded


def _oauth_default(secret: object = None, *, client_id: str = ADMIN_CLIENT_ID, config: str | None = None):
    extra = config if config is not None else (_config(webhook_secret=secret) if secret is not None else None)
    return encrypt_value(client_id), extra


async def test_an_integration_is_bound_to_its_own_app_secret():
    row = _slack_row("own-secret")
    state = await _load(integrations=[row])
    assert state.apps == (SlackAppSecret("own-secret", (str(row[0]),)),)
    assert state.unknown == ()


async def test_an_integration_on_the_admin_app_is_bound_to_the_oauth_default_secret():
    on_admin_app = _slack_row(client_id=ADMIN_CLIENT_ID)
    own_app_no_secret = _slack_row()
    state = await _load(
        integrations=[on_admin_app, own_app_no_secret],
        oauth_defaults=[_oauth_default("admin-secret")],
    )
    assert state.secret_for == {str(on_admin_app[0]): "admin-secret"}
    assert state.unknown == (str(own_app_no_secret[0]),), "another app's secret never signs for it"


async def test_the_recorded_choice_decides_the_app_whatever_the_client_id_says():
    """The integration API's rule: the recorded ``uses_admin_oauth_credentials``
    first, the client id only for a row created before it was recorded."""
    created_on_the_admin_app = _slack_row("copied-secret", client_id="old-admin-app", recorded=True)
    typed_the_admin_client_id = _slack_row("own-secret", client_id=ADMIN_CLIENT_ID, recorded=False)
    state = await _load(
        integrations=[created_on_the_admin_app, typed_the_admin_client_id],
        oauth_defaults=[_oauth_default("admin-secret")],
    )
    assert state.secret_for == {
        str(created_on_the_admin_app[0]): "admin-secret",
        str(typed_the_admin_client_id[0]): "own-secret",
    }


async def test_without_an_oauth_default_every_integration_uses_its_own_secret():
    row = _slack_row("own-secret", recorded=True)
    state = await _load(integrations=[row])
    assert state.secret_for == {str(row[0]): "own-secret"}


async def test_an_integration_on_the_admin_app_follows_the_current_oauth_default_secret():
    """Creation copies the default's extra config into the integration: a rotated secret wins over that copy."""
    row = _slack_row("secret-before-rotation", client_id=ADMIN_CLIENT_ID)
    state = await _load(integrations=[row], oauth_defaults=[_oauth_default("secret-after-rotation")])
    assert state.secret_for == {str(row[0]): "secret-after-rotation"}
    assert all(app.secret != "secret-before-rotation" for app in state.apps)


@pytest.mark.parametrize(
    "default",
    [
        pytest.param({}, id="no-secret"),
        pytest.param({"secret": True}, id="unusable"),
        pytest.param({"config": "not-a-fernet-token"}, id="unreadable"),
    ],
)
async def test_an_admin_app_integration_never_falls_back_to_its_copied_secret(default):
    """With no usable secret on the default, the creation-time copy is not read either:
    a cleared or broken secret must not leave a stale one proving deliveries."""
    row = _slack_row("copied-secret", client_id=ADMIN_CLIENT_ID)
    state = await _load(integrations=[row], oauth_defaults=[_oauth_default(**default)])
    assert state.secret_for == {}
    assert all(app.secret != "copied-secret" for app in state.apps)
    assert state.unknown == (str(row[0]),)


async def test_integrations_of_one_app_share_its_secret():
    first, second = _slack_row("shared"), _slack_row("shared")
    state = await _load(integrations=[first, second])
    assert state.apps == (SlackAppSecret("shared", (str(first[0]), str(second[0]))),)


async def test_nothing_configured_binds_nothing():
    row = _slack_row("")
    state = await _load(integrations=[row])
    assert state.apps == ()
    assert state.unknown == (str(row[0]),)


async def test_a_numeric_secret_stands_for_its_digits():
    row = _slack_row(123456)
    state = await _load(integrations=[row])
    assert state.secret_for == {str(row[0]): "123456"}


async def test_an_unusable_or_unreadable_secret_binds_nothing_and_is_reported():
    """A value no sender can prove is never read as "no secret"."""
    unusable = _slack_row(True)
    unreadable = _slack_row(config="not-a-fernet-token")
    not_an_object = _slack_row(config=encrypt_value("[1, 2]"))
    state = await _load(
        integrations=[unusable, unreadable, not_an_object],
        oauth_defaults=[_oauth_default("admin-secret")],
    )
    assert state.secret_for == {}
    assert state.unusable == (str(unusable[0]),)
    assert state.unreadable == (str(unreadable[0]), str(not_an_object[0]))


async def test_an_admin_app_integration_needs_no_readable_secret_of_its_own():
    """Its own field is not its app's secret, so a broken one neither blocks nor is reported."""
    unusable = _slack_row(True, client_id=ADMIN_CLIENT_ID)
    unreadable = _slack_row(config="not-a-fernet-token", client_id=ADMIN_CLIENT_ID)
    state = await _load(integrations=[unusable, unreadable], oauth_defaults=[_oauth_default("admin-secret")])
    assert state.secret_for == {str(unusable[0]): "admin-secret", str(unreadable[0]): "admin-secret"}
    assert state.unusable == ()
    assert state.unreadable == ()


async def test_an_unreadable_oauth_default_is_reported():
    row = _slack_row(client_id=ADMIN_CLIENT_ID)
    state = await _load(integrations=[row], oauth_defaults=[_oauth_default(config=encrypt_value("not json"))])
    assert state.unreadable == ("Slack OAuth default",)
    assert state.unknown == (str(row[0]),)


async def test_the_secrets_are_cached_briefly_and_reloaded_after_invalidation(monkeypatch):
    loads: list[object] = []

    async def _loader(db):
        loads.append(db)
        return SlackSigningSecrets(apps=(SlackAppSecret("s", ("i",)),))

    class _Opened:
        async def __aenter__(self):
            return "db"

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(auth, "load_slack_signing_secrets", _loader)
    monkeypatch.setattr(auth, "async_session", _Opened)
    first = await auth.slack_signing_secrets()
    second = await auth.slack_signing_secrets()
    assert first is second
    assert len(loads) == 1
    auth.invalidate_slack_signing_secrets()
    await auth.slack_signing_secrets()
    assert len(loads) == 2
    monkeypatch.setattr(auth, "_SLACK_SECRETS_TTL_SECONDS", 0.0)
    await auth.slack_signing_secrets()
    assert len(loads) == 3


# ---------------------------------------------------------------------------
# The verdict on one delivery
# ---------------------------------------------------------------------------


STATE = SlackSigningSecrets(
    apps=(SlackAppSecret("app-secret", ("a1", "a2")), SlackAppSecret("other-secret", ("b1",))),
    unknown=("c1",),
)


def test_a_delivery_reaches_only_the_integrations_of_the_app_that_signed_it():
    verdict = auth.check_slack_delivery(STATE, BODY, *_sign(BODY, "app-secret"))
    assert verdict.verified is True
    assert verdict.integration_ids == frozenset({"a1", "a2"})
    other = auth.check_slack_delivery(STATE, BODY, *_sign(BODY, "other-secret"))
    assert other.integration_ids == frozenset({"b1"})


def test_the_admin_app_secret_alone_proves_its_url_verification():
    state = SlackSigningSecrets(apps=(SlackAppSecret("admin-secret", ()),))
    verdict = auth.check_slack_delivery(state, BODY, *_sign(BODY, "admin-secret"))
    assert verdict.verified is True
    assert verdict.integration_ids == frozenset()


@pytest.mark.parametrize(
    ("signature", "timestamp"),
    [
        pytest.param("", "", id="unsigned"),
        pytest.param(*_sign(BODY, "wrong"), id="wrong"),
        pytest.param(*_sign(BODY, "app-secret", timestamp=str(int(time.time()) - 301)), id="stale"),
        pytest.param(*_sign(BODY, "app-secret", timestamp="inf"), id="infinite-timestamp"),
        pytest.param("v0=caf\u00c3\u00a9", str(int(time.time())), id="non-ascii"),
    ],
)
def test_an_unproven_delivery_is_refused(signature, timestamp):
    verdict = auth.check_slack_delivery(STATE, BODY, signature, timestamp)
    assert verdict.verified is False
    assert verdict.integration_ids == frozenset()


def test_with_no_secret_stored_anywhere_every_delivery_is_refused():
    """Fail closed: an install without a signing secret no longer accepts unsigned events."""
    assert auth.check_slack_delivery(SlackSigningSecrets(unknown=("c1",)), BODY, "", "").verified is False
    assert auth.check_slack_delivery(SlackSigningSecrets(), BODY, *_sign(BODY, "anything")).verified is False


def test_the_refusal_log_is_rate_limited_says_how_to_fix_it_and_carries_no_payload(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for _ in range(3):
            auth.check_slack_delivery(STATE, BODY, "", "")
    (message,) = [record.getMessage() for record in caplog.records]
    assert SLACK_NOTIFICATION_PATH in message
    assert "Signing secret" in message
    assert "polled every 5 minutes" in message
    assert "hello" not in message


def test_the_next_warning_counts_the_ones_it_held_back(caplog):
    auth._log_state["slack-refused"] = (time.monotonic() - auth._LOG_INTERVAL_SECONDS - 1, 4)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        auth.check_slack_delivery(STATE, BODY, "", "")
    (message,) = [record.getMessage() for record in caplog.records]
    assert "4 more" in message


async def test_an_unverified_delivery_accepted_is_said_once_per_window_for_the_whole_platform(
    compat, monkeypatch, caplog
):
    """Each worker, recycle and replica receives the Slack events of a standing configuration."""
    notices = share_notices(monkeypatch)
    state = SlackSigningSecrets(unknown=("c1",))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for _ in range(4):
            new_process()
            assert auth.check_slack_delivery(state, BODY, "", "").compatible
            await log_once.settle()
    accepted = [r for r in caplog.records if "accepted without a verified signature" in r.getMessage()]
    assert len(accepted) == 1
    assert accepted[0].levelno == logging.WARNING
    [claim] = notices.claims()
    assert ":integration_notification:slack-unverified:" in claim


async def test_another_slack_integration_left_without_a_secret_is_said_at_once(compat, monkeypatch, caplog):
    """The notice counts the integrations holding no usable secret: a new one is a new configuration."""
    share_notices(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for unknown in (("c1",), ("c1",), ("c1", "c2")):
            new_process()
            assert auth.check_slack_delivery(SlackSigningSecrets(unknown=unknown), BODY, "", "").compatible
            await log_once.settle()
    accepted = [r.getMessage() for r in caplog.records if "accepted without a verified signature" in r.getMessage()]
    assert len(accepted) == 2
    assert "(2 Slack integration(s) hold no usable secret)" in accepted[1]


def test_the_refusal_names_the_unreadable_sources(caplog):
    state = SlackSigningSecrets(unreadable=("abc",))
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        auth.check_slack_delivery(state, BODY, "", "")
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "abc" in errors[0].getMessage()


def test_by_default_an_unproven_delivery_reaches_only_the_integrations_holding_no_secret(compat, caplog):
    state = SlackSigningSecrets(
        apps=(SlackAppSecret("app-secret", ("a1",)),),
        unknown=("c1",),
        unusable=("u1", auth.OAUTH_DEFAULT_LABEL),
        unreadable=("r1",),
    )
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        verdict = auth.check_slack_delivery(state, BODY, *_sign(BODY, "forged"))
    assert (verdict.verified, verdict.compatible, verdict.accepted) == (False, True, True)
    assert verdict.integration_ids == frozenset({"c1", "u1", "r1"}), "never a1, whose secret is configured"
    messages = [record.getMessage() for record in caplog.records]
    assert any("accepted without a verified signature" in m for m in messages)
    assert any("INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION" in m for m in messages)
    assert all("hello" not in m for m in messages)


def test_by_default_a_delivery_with_nothing_unverifiable_to_reach_proves_no_integration(compat):
    """Accepted as before (assignments connected to no Slack integration still get it), for no integration."""
    state = SlackSigningSecrets(apps=(SlackAppSecret("app-secret", ("a1",)),), unusable=(auth.OAUTH_DEFAULT_LABEL,))
    verdict = auth.check_slack_delivery(state, BODY, "", "")
    assert (verdict.verified, verdict.compatible, verdict.integration_ids) == (False, True, frozenset())


def test_the_setting_is_read_when_the_caller_does_not_say(compat, monkeypatch):
    state = SlackSigningSecrets(unknown=("c1",))
    assert auth.check_slack_delivery(state, BODY, "", "").accepted is True
    monkeypatch.setattr(settings, "integration_notifications_require_verification", True)
    assert auth.check_slack_delivery(state, BODY, "", "").accepted is False
    assert auth.check_slack_delivery(state, BODY, "", "", require_verification=False).accepted is True


# ---------------------------------------------------------------------------
# The Graph subscription record and its clientState
# ---------------------------------------------------------------------------


class _Redis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key, value, ex=None):
        self.values[key] = value
        self.ttls[key] = ex

    async def get(self, key):
        return self.values.get(key)


def test_a_new_client_state_is_secrets_grade_and_fits_graph():
    states = {auth.new_graph_client_state() for _ in range(50)}
    assert len(states) == 50
    alphabet = set(string.ascii_letters + string.digits + "-_")
    for state in states:
        assert 40 <= len(state) <= auth.GRAPH_CLIENT_STATE_MAX_LENGTH
        assert set(state) <= alphabet


async def test_the_record_stores_the_client_state_encrypted_next_to_the_assignment():
    redis = _Redis()
    assignment_id = str(uuid.uuid4())
    client_state = auth.new_graph_client_state()
    await auth.store_graph_subscription(redis, "sub-1", assignment_id, client_state, ttl_seconds=3600)
    raw = redis.values["graph_sub:sub-1"]
    assert client_state not in raw
    record = json.loads(raw)
    assert record["assignment_id"] == assignment_id
    assert decrypt_value(record["client_state"]) == client_state
    assert redis.ttls["graph_sub:sub-1"] == 3600


@pytest.mark.parametrize(
    ("subscription_id", "assignment_id", "client_state"),
    [
        pytest.param("sub-1", str(uuid.uuid4()), "", id="no-client-state"),
        pytest.param("sub-1", str(uuid.uuid4()), "x" * 129, id="too-long-for-graph"),
        pytest.param("sub-1", "not-a-uuid", "state", id="not-an-assignment-id"),
        pytest.param("", str(uuid.uuid4()), "state", id="no-subscription-id"),
    ],
)
async def test_a_record_graph_could_not_authenticate_is_never_written(subscription_id, assignment_id, client_state):
    redis = _Redis()
    with pytest.raises(ValueError):
        await auth.store_graph_subscription(redis, subscription_id, assignment_id, client_state, ttl_seconds=60)
    assert redis.values == {}


async def test_a_notification_echoing_the_stored_client_state_names_its_assignment():
    redis = _Redis()
    assignment_id = str(uuid.uuid4())
    client_state = auth.new_graph_client_state()
    await auth.store_graph_subscription(redis, "sub-1", assignment_id, client_state, ttl_seconds=60)
    notification = {"subscriptionId": "sub-1", "clientState": client_state}
    assert await auth.graph_notification_assignment(redis, notification) == assignment_id


@pytest.mark.parametrize(
    "notification",
    [
        pytest.param({"subscriptionId": "sub-1", "clientState": "guess"}, id="mismatch"),
        pytest.param({"subscriptionId": "sub-1"}, id="missing"),
        pytest.param({"subscriptionId": "sub-1", "clientState": 42}, id="not-a-string"),
        pytest.param({"subscriptionId": "unknown", "clientState": "state"}, id="unknown-subscription"),
        pytest.param({"clientState": "state"}, id="no-subscription-id"),
        pytest.param({"subscriptionId": ["sub-1"], "clientState": "state"}, id="subscription-id-not-a-string"),
    ],
)
async def test_a_notification_that_cannot_prove_its_subscription_names_nothing(notification):
    redis = _Redis()
    await auth.store_graph_subscription(redis, "sub-1", str(uuid.uuid4()), "state", ttl_seconds=60)
    assert await auth.graph_notification_assignment(redis, notification) is None


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(str(uuid.uuid4()), id="legacy-bare-assignment-id"),
        pytest.param(json.dumps({"assignment_id": str(uuid.uuid4())}), id="no-client-state"),
        pytest.param(json.dumps({"assignment_id": "x", "client_state": encrypt_value("state")}), id="bad-id"),
        pytest.param(json.dumps([1]), id="not-an-object"),
        pytest.param("[" * 10_000, id="deeply-nested"),
    ],
)
async def test_a_record_without_a_usable_client_state_authenticates_nothing(raw, caplog):
    redis = _Redis()
    redis.values["graph_sub:sub-1"] = raw
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert (
            await auth.graph_notification_assignment(redis, {"subscriptionId": "sub-1", "clientState": "state"}) is None
        )
    assert any("clientState" in record.getMessage() for record in caplog.records)


async def test_by_default_a_legacy_record_keeps_serving_its_assignment(compat, caplog):
    redis = _Redis()
    assignment_id = str(uuid.uuid4())
    redis.values["graph_sub:sub-1"] = assignment_id
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert await auth.graph_notification_assignment(redis, {"subscriptionId": "sub-1"}) == assignment_id
    assert any("accepted without a clientState" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(json.dumps({"assignment_id": str(uuid.uuid4())}), id="no-client-state"),
        pytest.param("not-a-uuid", id="not-an-assignment-id"),
        pytest.param(json.dumps({"assignment_id": str(uuid.uuid4()), "client_state": "not-a-fernet-token"}), id="undecryptable"),
    ],
)
async def test_by_default_only_the_legacy_shape_is_accepted_without_a_client_state(compat, raw):
    redis = _Redis()
    redis.values["graph_sub:sub-1"] = raw
    assert await auth.graph_notification_assignment(redis, {"subscriptionId": "sub-1", "clientState": "x"}) is None


async def test_by_default_a_client_state_mismatch_is_still_refused(compat):
    redis = _Redis()
    await auth.store_graph_subscription(redis, "sub-1", str(uuid.uuid4()), "state", ttl_seconds=60)
    assert await auth.graph_notification_assignment(redis, {"subscriptionId": "sub-1", "clientState": "guess"}) is None


async def test_an_undecryptable_client_state_authenticates_nothing(caplog):
    redis = _Redis()
    redis.values["graph_sub:sub-1"] = json.dumps(
        {"assignment_id": str(uuid.uuid4()), "client_state": "not-a-fernet-token"}
    )
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        assert (
            await auth.graph_notification_assignment(redis, {"subscriptionId": "sub-1", "clientState": "state"}) is None
        )
    assert any(record.levelno == logging.ERROR for record in caplog.records)


async def test_a_mismatching_client_state_is_logged_without_the_value_sent(caplog):
    redis = _Redis()
    await auth.store_graph_subscription(redis, "sub-1", str(uuid.uuid4()), "state", ttl_seconds=60)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await auth.graph_notification_assignment(redis, {"subscriptionId": "sub-1", "clientState": "guess-123"})
    (message,) = [record.getMessage() for record in caplog.records]
    assert GRAPH_NOTIFICATION_PATH in message
    assert "guess-123" not in message


# ---------------------------------------------------------------------------
# The security audit finding
# ---------------------------------------------------------------------------


class _AuditDb:
    def __init__(self, slack_assignments: int):
        self.slack_assignments = slack_assignments
        self.statements: list = []

    async def execute(self, statement, *_args, **_kwargs):
        self.statements.append(statement)
        # The rows holding a surrogate escape SQL cannot parse: none here.
        return SimpleNamespace(
            scalar=lambda: self.slack_assignments, scalar_one_or_none=lambda: None, all=lambda: []
        )


def _audit_state(monkeypatch, state: SlackSigningSecrets) -> None:
    async def _loader(_db):
        return state

    monkeypatch.setattr(security_audit, "load_slack_signing_secrets", _loader)


CONTROL = "integrations.notifications_require_verification"


async def _audit(monkeypatch, state: SlackSigningSecrets, assignments: int) -> dict[str, security_audit.AuditFinding]:
    _audit_state(monkeypatch, state)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_integration_notifications(_AuditDb(assignments), findings)
    by_id = {finding.id: finding for finding in findings}
    assert len(by_id) == len(findings)
    # Every finding about the switch names its control.
    assert {finding.control for finding in findings} <= {CONTROL}
    return by_id


async def test_in_strict_mode_the_audit_says_which_slack_integrations_are_polled(monkeypatch):
    _audit_state(monkeypatch, SlackSigningSecrets(apps=(SlackAppSecret("s", ("a1",)),), unknown=("c1", "c2")))
    findings: list[security_audit.AuditFinding] = []
    db = _AuditDb(2)
    await security_audit._check_integration_notifications(db, findings)
    polled = next(f for f in findings if f.id == "integrations.slack_integrations_polled")
    assert polled.severity == "info" and polled.control == CONTROL
    assert SLACK_NOTIFICATION_PATH in polled.description
    assert "2 Slack integration(s)" in polled.description
    assert "polling" in polled.description
    assert "Signing secret" in (polled.recommendation or "")
    assert "integrations.slack_integrations_unverified" not in {f.id for f in findings}
    compiled = str(db.statements[0].compile(dialect=postgresql.dialect()))
    assert "trigger_config ->>" in compiled


async def test_by_default_the_audit_says_the_events_are_accepted_unverified(compat, monkeypatch):
    findings = await _audit(
        monkeypatch, SlackSigningSecrets(apps=(SlackAppSecret("s", ("a1",)),), unknown=("c1", "c2")), 2
    )
    finding = findings["integrations.slack_integrations_unverified"]
    assert finding.severity == "warning"
    assert finding.title == "Slack events are accepted unverified for some integrations"
    assert "without a verified signature" in finding.description
    assert "polling" not in finding.description
    # The Settings > Access and security field first, the variable as the alternative that wins.
    recommendation = finding.recommendation or ""
    assert 'turn on "Require verified integration notifications" in Settings > Access and security' in recommendation
    assert "INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION=true" in recommendation
    assert "integrations.notifications_verified" not in findings


async def test_the_audit_passes_once_every_integration_has_its_app_secret(monkeypatch):
    findings = await _audit(monkeypatch, SlackSigningSecrets(apps=(SlackAppSecret("app-secret", ("a1",)),)), 2)
    assert findings["integrations.slack_events_verified"].severity == "pass"
    assert findings["integrations.notifications_verified"].severity == "pass"
    assert not [f for f in findings.values() if f.severity == "warning"]


async def test_by_default_the_pass_says_what_still_arrives_unverified(compat, monkeypatch):
    findings = await _audit(monkeypatch, SlackSigningSecrets(apps=(SlackAppSecret("app-secret", ("a1",)),)), 2)
    passed = findings["integrations.slack_events_verified"]
    assert passed.severity == "pass"
    assert "connected to no Slack integration" in passed.description
    assert "(INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION)" in passed.description


@pytest.mark.parametrize("strict", [True, False], ids=["strict", "default"])
@pytest.mark.parametrize(
    "state",
    [
        pytest.param(SlackSigningSecrets(unusable=("Slack integration x",)), id="unusable"),
        pytest.param(SlackSigningSecrets(unreadable=("Slack OAuth default",)), id="unreadable"),
    ],
)
async def test_the_audit_flags_a_signing_secret_that_cannot_be_used(monkeypatch, state, strict):
    monkeypatch.setattr(settings, "integration_notifications_require_verification", strict)
    findings = await _audit(monkeypatch, state, 0)
    finding = findings["integrations.slack_signing_secret_unusable"]
    assert finding.severity == "warning"
    assert (state.unusable or state.unreadable)[0] in finding.description
    assert ("polled every 5 minutes" in finding.description) is strict


async def test_by_default_the_audit_says_nothing_when_nothing_is_received(compat, monkeypatch):
    assert await _audit(monkeypatch, SlackSigningSecrets(), 0) == {}


async def test_in_strict_mode_the_audit_reports_the_control_it_enforces(monkeypatch):
    """No silent pass: the switch on is reported, whatever uses it."""
    findings = await _audit(monkeypatch, SlackSigningSecrets(), 0)
    assert list(findings) == ["integrations.notifications_verified"]
    assert findings["integrations.notifications_verified"].severity == "pass"


def test_the_audit_runs_the_check():
    import inspect

    assert "_check_integration_notifications(" in inspect.getsource(security_audit.build_security_audit_report)


# ---------------------------------------------------------------------------
# Where the signing secret is entered
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("recorded", "client_id", "admin_client_id", "expected"),
    [
        pytest.param(True, "old-admin-app", "admin-client-id", True, id="recorded-on-the-admin-app"),
        pytest.param(False, "admin-client-id", "admin-client-id", False, id="recorded-own-app"),
        pytest.param(None, "admin-client-id", "admin-client-id", True, id="legacy-row-created-from-the-default"),
        pytest.param(None, "own-client-id", "admin-client-id", False, id="legacy-row-own-app"),
        pytest.param(None, None, "admin-client-id", False, id="legacy-row-no-client-id"),
        pytest.param(None, "admin-client-id", None, False, id="no-oauth-default"),
    ],
)
def test_one_rule_says_whether_an_integration_runs_on_the_admin_app(recorded, client_id, admin_client_id, expected):
    """``uses_admin_oauth_app``: the integration API, the provider webhook, the LinkedIn
    notifications and the Slack signing secrets all read it."""
    from app.services.integration_state import uses_admin_oauth_app

    client_id_enc = encrypt_value(client_id) if client_id else None
    assert uses_admin_oauth_app(recorded, client_id_enc, admin_client_id) is expected


def test_the_rule_cannot_tell_when_a_legacy_client_id_is_unreadable():
    from app.services.integration_state import UNREADABLE_ADMIN_CLIENT_ID, uses_admin_oauth_app

    assert uses_admin_oauth_app(None, "not-a-fernet-token", "admin-client-id") is None
    assert uses_admin_oauth_app(None, encrypt_value("admin-client-id"), UNREADABLE_ADMIN_CLIENT_ID) is None
    assert uses_admin_oauth_app(True, "not-a-fernet-token", UNREADABLE_ADMIN_CLIENT_ID) is True


def test_the_integration_api_answers_with_the_same_rule():
    from app.api.v1.integrations import _UNREADABLE, _effective_uses_admin_oauth_credentials

    def _row(recorded, client_id):
        return SimpleNamespace(
            provider="slack", uses_admin_oauth_credentials=recorded, client_id_enc=encrypt_value(client_id)
        )

    admin = {"slack": "admin-client-id"}
    assert _effective_uses_admin_oauth_credentials(_row(None, "admin-client-id"), admin) is True
    assert _effective_uses_admin_oauth_credentials(_row(None, "own"), admin) is False
    assert _effective_uses_admin_oauth_credentials(_row(True, "own"), admin) is True
    assert _effective_uses_admin_oauth_credentials(_row(None, "own"), {"slack": _UNREADABLE}) is None
    assert _effective_uses_admin_oauth_credentials(_row(None, "own"), None) is None


def test_the_slack_integration_offers_a_signing_secret_field():
    """``webhook_secret``: the key every Slack signature gate reads, masked; real time needs it."""
    slack = PROVIDER_CATALOG["slack"]
    assert "Signing secret" in slack["extra_fields"]["webhook_secret"]
    assert "polled every 5 minutes" in slack["extra_fields"]["webhook_secret"]
    steps = "\n".join(slack["setup_guide"]["steps"])
    assert f"{{base_url}}{SLACK_NOTIFICATION_PATH}" in steps
    assert "Signing Secret" in steps


def test_the_setup_guide_subscribes_to_the_user_events_the_endpoint_handles():
    """The integration holds a user token: bot events and app_mention never reach a new_slack_message assignment."""
    steps = "\n".join(PROVIDER_CATALOG["slack"]["setup_guide"]["steps"])
    assert "Subscribe to events on behalf of users" in steps
    for event in ("message.channels", "message.groups", "message.im"):
        assert event in steps
    assert "bot events" not in steps
    assert "app_mention" not in steps


async def test_by_default_the_audit_names_what_is_still_accepted_as_before(compat, monkeypatch):
    """LinkedIn without a client secret, and admin-app deliveries not narrowed."""
    findings = await _audit(monkeypatch, SlackSigningSecrets(), 2)
    assert findings["integrations.slack_events_unverified"].severity == "warning"
    assert findings["integrations.linkedin_notifications_unverified"].severity == "warning"
    assert findings["integrations.admin_app_scope_not_applied"].severity == "info"
    for finding_id in (
        "integrations.slack_events_unverified",
        "integrations.linkedin_notifications_unverified",
        "integrations.admin_app_scope_not_applied",
    ):
        assert "INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION=true" in (findings[finding_id].recommendation or "")


async def test_linkedin_notifications_signed_with_the_admin_app_secret_pass(monkeypatch):
    # The pass of the LinkedIn check: with the client secret configured,
    # every notification must carry its signature, strict mode or not.
    class _LinkedInDb(_AuditDb):
        async def execute(self, statement, *_args, **_kwargs):
            answer = await super().execute(statement)
            return SimpleNamespace(scalar=answer.scalar, scalar_one_or_none=lambda: "enc-secret", all=answer.all)

    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_unscoped_admin_app_deliveries(
        _LinkedInDb(3), findings, 0, SlackSigningSecrets(), strict=False, unparsed=[]
    )
    by_id = {finding.id: finding for finding in findings}
    verified = by_id["integrations.linkedin_notifications_verified"]
    assert (verified.severity, verified.control) == ("pass", CONTROL)
    assert verified.description.startswith("3 active assignment(s) are triggered by LinkedIn notifications")
    assert "integrations.linkedin_notifications_unverified" not in by_id


async def test_in_strict_mode_the_audit_does_not_report_the_compatibility_defaults(monkeypatch):
    monkeypatch.setattr(settings, "integration_notifications_require_verification", True)
    findings = await _audit(monkeypatch, SlackSigningSecrets(), 2)
    assert not {
        "integrations.slack_events_unverified",
        "integrations.linkedin_notifications_unverified",
        "integrations.admin_app_scope_not_applied",
    } & set(findings)
    # What strict mode costs is said instead: LinkedIn assignments that can never trigger.
    assert findings["integrations.linkedin_notifications_dropped"].severity == "info"
    assert findings["integrations.notifications_verified"].severity == "pass"
