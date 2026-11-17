"""A Google-issued token, through the ACTUAL inbound route, end to end.

The composition nothing covered. ``test_a2a_google_federation`` proves the
verification — pins, claims, keyset, the refusals — and
``test_a2a_v1_binding`` proves the route, but every route test patches
``_authenticate`` wholesale. So "a token Google minted reaches a task, and the
task records which credential form admitted it" was asserted nowhere, and that
composition is the whole of what an inbound Google registration is.

Only two things are stubbed: the JWKS fetch (the network is the one thing these
tests must not touch) and the bindings read that finds the pins. Issuer
routing, RS256 verification, audience and caller matching, the uniform
refusal, and the derived partner identity all run for real.
"""

import uuid
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from fastapi import FastAPI

from app.a2a import google_federation as gf
from app.a2a import protocol as proto
from app.a2a import providers
from app.api.v1 import a2a_server
from app.core.database import get_db
from app.services import ai_state as _ai_state
from tests.a2a_chat_gate import stub_chat_gate
from tests.a2a_server_stubs import partner_client

AUDIENCE = "https://xtm.example.com/a2a"
CALLER = "consumer@partner-project.iam.gserviceaccount.com"
CALLER_SUB = "104729384756102938475"
INTEGRATION_ID = uuid.uuid4()

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
_PUBLIC_PEM = _key.public_key().public_bytes(
    Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
)


class _Key:
    key = _PUBLIC_PEM


def _token(**overrides) -> str:
    now = datetime.now(UTC)
    claims = {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "sub": CALLER_SUB,
        "email": CALLER,
        "email_verified": True,
        "iat": now,
        "exp": now + timedelta(minutes=10),
    }
    claims.update(overrides)
    return pyjwt.encode(claims, _PRIVATE_PEM, algorithm="RS256")


@pytest.fixture(autouse=True)
def _ai_enabled(monkeypatch):
    async def _enabled(_db):
        return True

    monkeypatch.setattr(_ai_state, "is_ai_enabled", _enabled)


class _Exposure:
    def __init__(self):
        self.id = uuid.uuid4()
        self.agent_id = uuid.uuid4()
        self.run_as_user_id = uuid.uuid4()
        self.revoked_at = None
        self.allowed_tools = None


class _Agent:
    def __init__(self):
        self.id = uuid.uuid4()
        self.name = "Exposed agent"
        self.enabled = True
        self.is_company_managed = False
        self.output_format = "markdown"


class _Task:
    def __init__(self):
        self.id = uuid.uuid4()
        self.context_id = uuid.uuid4()
        self.run_id = None
        self.rejected_reason = None
        self.cancel_requested_at = None
        self.client_id = "client-1"


class _AgentDB:
    def __init__(self, agent):
        self._agent = agent

    async def execute(self, _stmt):
        agent = self._agent

        class _R:
            def scalar_one_or_none(self):
                return agent

        return _R()

    async def commit(self):
        pass

    async def flush(self):
        pass

    async def rollback(self):
        pass

    async def refresh(self, _obj):
        pass


def _harness(monkeypatch, *, pins):
    """The route with the Google auth path REAL and everything after it faked.

    ``pins`` is what the bindings read returns — an empty list is an agent
    that never opted in, which is the case that must refuse before any
    keyset work happens.
    """
    exposure, agent, task = _Exposure(), _Agent(), _Task()
    observed: dict = {"jwks_calls": 0, "create_kwargs": None}

    stub_chat_gate(monkeypatch)

    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "a2a_blocking_poll_interval_seconds", 0.001)
    monkeypatch.setattr(app_settings, "a2a_blocking_send_timeout_seconds", 5)

    import app.a2a.exposure as exposure_mod
    import app.a2a.tasks as tasks_mod
    import app.services.audit as audit_mod
    import app.services.quota as quota_mod
    from app.core import rate_limit as rate_limit_mod

    async def _get_exposure(_db, _token):
        return exposure

    monkeypatch.setattr(exposure_mod, "get_exposure_by_token", _get_exposure)

    async def _noop_preauth(_request):
        pass

    monkeypatch.setattr(a2a_server, "_enforce_preauth_rate_limit", _noop_preauth)

    async def _per_client(_client_id, _op):
        pass

    monkeypatch.setattr(a2a_server, "_enforce_a2a_rate_limit", _per_client)

    async def _refund(_request, _key):
        pass

    monkeypatch.setattr(rate_limit_mod, "refund_rate_limit", _refund)

    # --- the two stubs, and only these two -------------------------------
    async def _resolve(_jwks_url, _token, *, timeout=30):
        observed["jwks_calls"] += 1
        return _Key()

    monkeypatch.setattr(gf, "resolve_signing_key", _resolve)

    async def _pins(_db, _agent_id):
        return list(pins)

    monkeypatch.setattr(gf, "load_agent_google_pins", _pins)
    # ---------------------------------------------------------------------

    async def _no_audit(*_a, **_k):
        pass

    monkeypatch.setattr(audit_mod, "log_audit", _no_audit)

    async def _quota_ok(_db, _user_id):
        pass

    monkeypatch.setattr(quota_mod, "check_agentic_quota", _quota_ok)

    async def _no_existing(_db, _client_id, _message_id):
        return None

    monkeypatch.setattr(tasks_mod, "find_existing_task", _no_existing)

    async def _create(_db, **kw):
        observed["create_kwargs"] = kw
        return task, True

    monkeypatch.setattr(tasks_mod, "create_task", _create)

    async def _run(_task_id):
        pass

    monkeypatch.setattr(tasks_mod, "execute_task_run", _run)

    async def _announce(_task_id):
        pass

    monkeypatch.setattr(tasks_mod, "announce_state_by_id", _announce)

    async def _get_task(_db, _task_id, _client_id):
        return task

    monkeypatch.setattr(tasks_mod, "get_task", _get_task)

    async def _state(_db, _task):
        return proto.A2AState.COMPLETED

    monkeypatch.setattr(tasks_mod, "resolve_state", _state)

    async def _artifacts(_db, _task, _agent, version):
        return [
            {
                "artifactId": "a1",
                "name": "result",
                "parts": [proto.text_part("the answer", version)],
            }
        ]

    monkeypatch.setattr(tasks_mod, "build_artifacts", _artifacts)

    app = FastAPI()
    app.include_router(a2a_server.router)
    app.dependency_overrides[get_db] = lambda: _AgentDB(agent)
    return partner_client(app), task, observed


def _pins_for(**overrides) -> gf.GooglePins:
    kwargs = {
        "audience": AUDIENCE,
        "caller": CALLER,
        "integration_id": INTEGRATION_ID,
    }
    kwargs.update(overrides)
    return gf.GooglePins(**kwargs)


def _send(client, token: str):
    """One v1.0 send, which is the dialect ADK's client actually speaks."""
    return client.post(
        "/a2a/tok/jsonrpc",
        json={
            "jsonrpc": "2.0",
            "id": "r1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "m-1",
                    "role": "ROLE_USER",
                    "parts": [{"text": "what does CVE-2026-1234 affect?"}],
                }
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )


class TestAGoogleTokenReachesATask:
    def test_a_matching_token_is_admitted_and_the_work_runs(self, monkeypatch):
        client, task, _o = _harness(monkeypatch, pins=[_pins_for()])
        response = _send(client, _token())
        assert response.status_code == 200
        body = response.json()
        assert "error" not in body, body
        assert body["result"]["task"]["id"] == str(task.id)

    def test_the_task_records_google_as_the_credential_form(self, monkeypatch):
        # The one fact only ``_authenticate`` knows, and the reason it is
        # stamped on the task rather than read back off the registration: a
        # federated caller's client id is indistinguishable from an OAuth
        # one, so attribution has to be captured at the moment it is known.
        client, _task, observed = _harness(monkeypatch, pins=[_pins_for()])
        assert _send(client, _token()).status_code == 200
        kwargs = observed["create_kwargs"]
        assert kwargs["provider"] == providers.GOOGLE_A2A_PROVIDER
        # And the partner identity is derived from the integration, not
        # invented: an ``a2a-google-…`` id naming the registration.
        assert kwargs["client_id"] == providers.partner_client_id(
            INTEGRATION_ID, providers.GOOGLE_A2A_PROVIDER
        )

    def test_a_caller_pinned_by_numeric_subject_is_admitted(self, monkeypatch):
        # The pin an operator should prefer: a token minted from a metadata
        # server may carry no verified email, and ``sub`` is always there.
        client, _task, _o = _harness(monkeypatch, pins=[_pins_for(caller=CALLER_SUB)])
        assert _send(client, _token(email=None, email_verified=None)).status_code == 200


    def test_the_v0_3_dialect_works_through_the_same_auth(self, monkeypatch):
        # Both dialects must reach the same auth. v0.3 asks for a blocking
        # send explicitly — the polarity is inverted between versions.
        client, task, _o = _harness(monkeypatch, pins=[_pins_for()])
        response = client.post(
            "/a2a/tok/jsonrpc",
            json={
                "jsonrpc": "2.0",
                "id": "r1",
                "method": "message/send",
                "params": {
                    "message": {
                        "kind": "message",
                        "role": "user",
                        "messageId": "m-2",
                        "parts": [{"kind": "text", "text": "hello"}],
                    },
                    "configuration": {"blocking": True},
                },
            },
            headers={"Authorization": f"Bearer {_token()}"},
        )
        assert response.status_code == 200, response.text
        # v0.3's send result IS the task, with no oneof envelope.
        assert response.json()["result"]["id"] == str(task.id)


class TestEveryRefusalLooksTheSame:
    """The posture that makes this surface safe and configuring it awkward.

    Every cause — wrong audience, wrong caller, unverified email, an agent
    that never bound the integration — produces the SAME 401 carrying the same
    word. The distinction exists only in the audit trail, the metric label and
    the server log: from outside, the causes are indistinguishable.
    """

    @pytest.mark.parametrize(
        "token_kwargs",
        [
            {"aud": "https://someone-elses-service"},
            {"sub": "999", "email": "other@partner.iam.gserviceaccount.com"},
            {"iss": "https://login.microsoftonline.com/tid/v2.0"},
        ],
        ids=["wrong-audience", "wrong-caller", "not-google"],
    )
    def test_a_token_no_pin_admits_gets_the_uniform_not_found(
        self, monkeypatch, token_kwargs
    ):
        client, _task, _o = _harness(monkeypatch, pins=[_pins_for()])
        response = _send(client, _token(**token_kwargs))
        assert response.status_code == 401, response.text
        # One word, and the same word for every cause. Nothing in the response
        # says WHICH pin disagreed, deliberately.
        error = response.json()["error"]
        assert error["message"] == "Unauthenticated"
        assert "aud" not in response.text and "caller" not in response.text

    def test_an_email_pin_needs_a_verified_email(self, monkeypatch):
        client, _task, _o = _harness(monkeypatch, pins=[_pins_for()])
        assert _send(client, _token(email_verified=False)).status_code == 401

    def test_an_agent_that_never_opted_in_costs_no_keyset_work(self, monkeypatch):
        # The cheap-refusal property: one bindings read decides whether any
        # network-touching verification happens at all, so an unbound agent
        # cannot be used to make this platform fetch keysets.
        client, _task, observed = _harness(monkeypatch, pins=[])
        assert _send(client, _token()).status_code == 401
        assert observed["jwks_calls"] == 0
