"""The LinkedIn notification endpoint requires its signature once a secret exists.

``POST /api/v1/integrations/notifications/linkedin`` checked ``X-LI-Signature``
only when the header was present, so an unsigned notification was accepted and
fanned out to assignments even with a LinkedIn client secret configured - the
gap ``_verify_linkedin_provider_signature`` already closed for the webhook
routes. With a secret configured, a missing signature is now refused with the
same 401 as a wrong one, and the body is not parsed before the check.

Requests go through the real ASGI stack (``TestClient``), so headers are real
Starlette headers decoded from wire bytes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import integration_notifications as notifications
from app.core.security import encrypt_value

URL = "/api/v1/integrations/notifications/linkedin"
SECRET = "linkedin-client-secret"
ADMIN_CLIENT_ID = "linkedin-admin-app-client-id"
EMPTY_BATCH = json.dumps({"type": "ORGANIZATION_SOCIAL_ACTION_NOTIFICATIONS", "notifications": []}).encode()


def _signature(body: bytes, secret: str = SECRET) -> bytes:
    return b"hmacsha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest().encode()


class _Session:
    """``async_session()`` answering the OAuthDefault lookup with *row*."""

    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, *_args, **_kwargs):
        row = self._row
        return SimpleNamespace(scalar_one_or_none=lambda: row)


@pytest.fixture
def client_for(monkeypatch):
    """A client whose LinkedIn OAuth default carries *client_secret_enc*."""

    def _build(client_secret_enc: str | None, decrypt=lambda _enc: SECRET) -> TestClient:
        row = SimpleNamespace(id=uuid.uuid4(), client_secret_enc=client_secret_enc)
        monkeypatch.setattr(notifications, "async_session", lambda: _Session(row))
        # The client secret is read through the vault resolver, which decrypts the stored text.
        monkeypatch.setattr("app.core.security.decrypt_stored_value", decrypt)
        app = FastAPI()
        app.include_router(notifications.notification_router, prefix="/api/v1/integrations")
        return TestClient(app)

    return _build


def test_an_unsigned_notification_is_refused_when_a_secret_is_configured(client_for):
    response = client_for("enc").post(URL, content=EMPTY_BATCH, headers={"Content-Type": "application/json"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid LinkedIn signature"


def test_a_wrong_signature_gets_the_same_answer(client_for):
    response = client_for("enc").post(
        URL, content=EMPTY_BATCH, headers={"X-LI-Signature": _signature(EMPTY_BATCH, "not-the-secret")}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid LinkedIn signature"


def test_a_non_ascii_signature_gets_the_same_answer(client_for):
    response = client_for("enc").post(URL, content=EMPTY_BATCH, headers={"X-LI-Signature": b"hmacsha256=caf\xc3\xa9"})
    assert response.status_code == 401


def test_a_valid_signature_is_accepted(client_for):
    response = client_for("enc").post(URL, content=EMPTY_BATCH, headers={"X-LI-Signature": _signature(EMPTY_BATCH)})
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "triggered": 0}


def test_the_signature_linkedin_documents_is_accepted(client_for):
    """Bare hex HMAC-SHA256 of ``hmacsha256=`` + body, as LinkedIn sends it."""
    documented = hmac.new(SECRET.encode(), b"hmacsha256=" + EMPTY_BATCH, hashlib.sha256).hexdigest().encode()
    response = client_for("enc").post(URL, content=EMPTY_BATCH, headers={"X-LI-Signature": documented})
    assert response.status_code == 200


def test_an_unsigned_body_is_refused_before_it_is_parsed(client_for):
    """Garbage without a signature is a 401, not a JSON error."""
    response = client_for("enc").post(URL, content=b"not json")
    assert response.status_code == 401


def test_a_signed_body_that_is_not_a_json_object_is_a_400(client_for):
    for body in (b"not json", b"[1, 2]"):
        response = client_for("enc").post(URL, content=body, headers={"X-LI-Signature": _signature(body)})
        assert response.status_code == 400


def test_an_unreadable_secret_refuses_the_delivery(client_for, caplog):
    def _undecryptable(_enc):
        raise ValueError("InvalidToken")

    with caplog.at_level("ERROR", logger=notifications.logger.name):
        response = client_for("enc", decrypt=_undecryptable).post(
            URL, content=EMPTY_BATCH, headers={"X-LI-Signature": _signature(EMPTY_BATCH)}
        )
    assert response.status_code == 503
    assert any("cannot be read: InvalidToken" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("client_secret_enc", [None, ""])
def test_without_a_configured_secret_there_is_nothing_to_verify(client_for, client_secret_enc):
    """Unchanged: no secret, no signature to check."""
    response = client_for(client_secret_enc).post(URL, content=EMPTY_BATCH)
    assert response.status_code == 200


def test_a_client_secret_saved_empty_refuses_even_a_signature_made_with_it(client_for):
    """Saved empty, the secret still has a ciphertext, so it is "configured" - and
    an HMAC keyed with ``b""`` is anyone's to compute. No LinkedIn webhook can run
    on it either way: LinkedIn signs with the app's real secret."""
    forged = hmac.new(b"", b"hmacsha256=" + EMPTY_BATCH, hashlib.sha256).hexdigest().encode()
    response = client_for("enc", decrypt=lambda _enc: "").post(
        URL, content=EMPTY_BATCH, headers={"X-LI-Signature": forged}
    )
    assert response.status_code == 401


def _post_batch(client_for, batch: dict, signed: bool):
    body = json.dumps(batch).encode()
    if signed:
        return client_for("enc").post(URL, content=body, headers={"X-LI-Signature": _signature(body)})
    return client_for(None).post(URL, content=body)


@pytest.mark.parametrize(
    "notifications",
    [
        [1], ["a"], [None], [[]], [{"action": "LIKE"}, 2], "not a list", {"action": "LIKE"}, 5, True,
        # Falsy ones too: malformed, however empty.
        "", {}, 0, False,
    ],
)
@pytest.mark.parametrize("signed", [True, False], ids=["secret-configured", "no-secret"])
def test_notifications_that_are_not_a_list_of_objects_are_a_400(client_for, notifications, signed):
    """Refused before any work, rather than a 500 on the first ``notif.get``."""
    batch = {"type": "ORGANIZATION_SOCIAL_ACTION_NOTIFICATIONS", "notifications": notifications}
    response = _post_batch(client_for, batch, signed)
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid JSON payload"


@pytest.mark.parametrize(
    "batch",
    [{"type": "ORGANIZATION_SOCIAL_ACTION_NOTIFICATIONS", "notifications": []}, {"type": "OTHER"}, {"notifications": None}],
    ids=["empty-list", "absent", "null"],
)
@pytest.mark.parametrize("signed", [True, False], ids=["secret-configured", "no-secret"])
def test_an_empty_absent_or_null_batch_is_nothing_to_do(client_for, batch, signed):
    response = _post_batch(client_for, batch, signed)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "triggered": 0}


class _FanOutSession:
    """The OAuthDefault lookup, then one LinkedIn assignment, its agent and the
    integration it is bound to: its owner's, on the admin app."""

    def __init__(self, assignment, agent, integration_row):
        self._rows = {
            notifications.OAuthDefault: SimpleNamespace(
                id=uuid.uuid4(),
                client_id_enc=encrypt_value(ADMIN_CLIENT_ID),
                client_secret_enc=encrypt_value(SECRET),
            ),
            notifications.Assignment: assignment,
            notifications.Agent: agent,
            notifications.Integration: integration_row,
        }
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, statement, *_args, **_kwargs):
        row = self._rows[statement.column_descriptions[0]["entity"]]
        return SimpleNamespace(
            scalar_one_or_none=lambda: row,
            scalars=lambda: SimpleNamespace(all=lambda: [row]),
            all=lambda: [row],
        )

    async def commit(self):
        self.committed = True


def test_a_malformed_field_of_a_notification_reads_as_absent(monkeypatch):
    """A decoration that is not an object, or an ``action`` no dict can be keyed
    by, was a 500 halfway through the fan-out. A well-formed notification of the
    same batch is processed exactly as before."""
    owner, integration_id = uuid.uuid4(), uuid.uuid4()
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        enabled=True,
        user_id=owner,
        integration_bindings={"linkedin": [str(integration_id)]},
    )
    session = _FanOutSession(
        SimpleNamespace(
            id=uuid.uuid4(), trigger_config={"provider": "linkedin"}, agent_id=agent.id, name="WF"
        ),
        agent,
        (integration_id, "linkedin", owner, "oauth", False, encrypt_value(ADMIN_CLIENT_ID), None),
    )
    triggered: list[dict] = []

    async def _trigger(**kwargs):
        triggered.append(kwargs["payload"])
        return "run-id"

    monkeypatch.setattr(notifications, "async_session", lambda: session)
    monkeypatch.setattr(notifications, "trigger_assignment", _trigger)
    app = FastAPI()
    app.include_router(notifications.notification_router, prefix="/api/v1/integrations")
    body = json.dumps(
        {
            "type": "ORGANIZATION_SOCIAL_ACTION_NOTIFICATIONS",
            "notifications": [
                {
                    "action": ["LIKE"],
                    "decoratedSourcePost": "not an object",
                    "decoratedGeneratedActivity": {"comment": "not an object", "share": []},
                },
                {
                    "action": "COMMENT",
                    "decoratedSourcePost": {"share": {"text": "the post", "owner": "urn:li:organization:1"}},
                    "decoratedGeneratedActivity": {"comment": {"text": "hi", "owner": "urn:li:person:1"}},
                },
            ],
        }
    ).encode()

    response = TestClient(app).post(URL, content=body, headers={"X-LI-Signature": _signature(body)})

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "triggered": 2}
    assert session.committed
    malformed, well_formed = triggered
    assert malformed["event"] == "linkedin_social_action"
    assert not {"source_post_text", "comment_text", "share_text"} & malformed.keys()
    assert well_formed["event"] == "linkedin_comment"
    assert (well_formed["source_post_text"], well_formed["source_post_owner"]) == ("the post", "urn:li:organization:1")
    assert (well_formed["comment_text"], well_formed["comment_owner"]) == ("hi", "urn:li:person:1")
