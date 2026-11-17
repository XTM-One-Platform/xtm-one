"""A webhook secret that cannot be read refuses the delivery.

The webhook gates read an integration's secret out of its encrypted
``extra_config`` (or an OAuth default's ``client_secret_enc``). When that could
not be decrypted - a changed ``SECRET_KEY`` - the gates logged a warning and
accepted the delivery as if no secret were configured. A config that cannot be
read cannot be told apart from one that holds a secret, so the delivery is now
refused with a 503 (``Retry-After``) and an ERROR log naming the integration.
503, not 401: the sender did nothing wrong, the operator must act, and
providers retry a 5xx once the key is restored.

Every gated route is exercised: the provider route (webhook secret and the
LinkedIn client secret), the integration route and the per-assignment route
(generic and HubSpot gates, before and after the dedup claim), and the two
HubSpot batch handlers.
"""

from __future__ import annotations

import json
import logging
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers

from app.api.v1 import webhooks

SECRET = "s3cr3t"


def _undecryptable(_enc):
    raise ValueError("InvalidToken")


class _Delivery(SimpleNamespace):
    async def body(self) -> bytes:
        return self._body


def _delivery(pairs=(), body: bytes = b'{"action":"opened"}') -> _Delivery:
    headers = Headers(raw=[(name.lower().encode(), value) for name, value in pairs])
    return _Delivery(headers=headers, _body=body, method="POST", url="https://xtm.example/api/hooks/x")


class _Session:
    """``async_session()`` answering each query with the next row given (the
    last one repeats); ``all()`` is the provider loader's agent query."""

    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, *_args, **_kwargs):
        row = self._rows.pop(0) if len(self._rows) > 1 else self._rows[0]
        return SimpleNamespace(scalar_one_or_none=lambda: row, all=lambda: [])


def _serve(monkeypatch, *rows) -> None:
    session = _Session(list(rows))
    monkeypatch.setattr(webhooks, "async_session", lambda: session)

    async def _allow(*_args, **_kwargs):
        return None

    monkeypatch.setattr(webhooks, "enforce_rate_limit", _allow)


def _forbid_dedup_claim(monkeypatch) -> None:
    async def _claim(*_args, **_kwargs):
        raise AssertionError("the dedup slot was claimed: the delivery was accepted unverified")

    monkeypatch.setattr(webhooks, "_claim_webhook_dedup_slot", _claim)


def _assert_refused(exc_info) -> None:
    assert exc_info.value.status_code == 503
    assert exc_info.value.headers == {"Retry-After": "300"}


# ── The shared reader ───────────────────────────────────────────────────────


def test_no_config_means_nothing_to_verify():
    assert webhooks._read_webhook_config(None, context="integration=x") == {}
    assert webhooks._read_webhook_config("", context="integration=x") == {}


@pytest.mark.parametrize(
    "decrypt",
    [_undecryptable, lambda _enc: "not json", lambda _enc: json.dumps(["a", "list"])],
    ids=["undecryptable", "not-json", "not-an-object"],
)
def test_an_unreadable_config_is_refused_with_an_error_naming_it(monkeypatch, caplog, decrypt):
    monkeypatch.setattr(webhooks, "decrypt_value", decrypt)
    with caplog.at_level(logging.ERROR, logger=webhooks.logger.name), pytest.raises(HTTPException) as exc_info:
        webhooks._read_webhook_config("enc", context="integration=abc-123")
    _assert_refused(exc_info)
    assert any(
        record.levelno == logging.ERROR and "integration=abc-123" in record.getMessage()
        for record in caplog.records
    )


def test_a_readable_config_without_a_secret_is_still_let_through(monkeypatch):
    """Unchanged: no ``webhook_secret`` means unauthenticated by design."""
    monkeypatch.setattr(webhooks, "decrypt_value", lambda _enc: json.dumps({"base_url": "https://x"}))
    webhooks._try_verify_webhook_signature(b"{}", "enc", _delivery())


def test_the_generic_gate_refuses_instead_of_skipping(monkeypatch):
    monkeypatch.setattr(webhooks, "decrypt_value", _undecryptable)
    with pytest.raises(HTTPException) as exc_info:
        webhooks._try_verify_webhook_signature(b"{}", "enc", _delivery(), context="integration=x")
    _assert_refused(exc_info)


async def test_the_linkedin_provider_gate_refuses_an_unreadable_client_secret(monkeypatch):
    monkeypatch.setattr("app.core.security.decrypt_stored_value", _undecryptable)
    with pytest.raises(HTTPException) as exc_info:
        await webhooks._verify_linkedin_provider_signature(
            b"{}", "linkedin", "enc", _delivery([("X-LI-Signature", b"hmacsha256=00")])
        )
    _assert_refused(exc_info)


# ── Routes, before the dedup claim ──────────────────────────────────────────


async def test_the_provider_route_refuses_before_the_dedup_cache(monkeypatch):
    webhooks._invalidate_provider_routing_cache()
    _serve(monkeypatch, SimpleNamespace(id=uuid.uuid4(), extra_config_enc="enc", client_secret_enc=None))
    monkeypatch.setattr(webhooks, "decrypt_value", _undecryptable)
    _forbid_dedup_claim(monkeypatch)
    try:
        with pytest.raises(HTTPException) as exc_info:
            await webhooks.receive_provider_webhook("github", _delivery())
        _assert_refused(exc_info)
    finally:
        webhooks._invalidate_provider_routing_cache()


@pytest.mark.parametrize(("provider", "body"), [("github", b'{"action":"opened"}'), ("hubspot", b"[]")])
async def test_the_integration_route_refuses_before_the_dedup_cache(monkeypatch, caplog, provider, body):
    integration_id = str(uuid.uuid4())
    _serve(monkeypatch, SimpleNamespace(id=integration_id, provider=provider, extra_config_enc="enc"))
    monkeypatch.setattr(webhooks, "decrypt_value", _undecryptable)
    _forbid_dedup_claim(monkeypatch)
    with caplog.at_level(logging.ERROR, logger=webhooks.logger.name), pytest.raises(HTTPException) as exc_info:
        await webhooks.receive_integration_webhook(integration_id, _delivery(body=body))
    _assert_refused(exc_info)
    assert any(integration_id in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    ("provider", "pairs", "body"),
    [
        ("opencti", [], b'{"type":"create"}'),
        ("opencti", [("X-OpenCTI-Token", SECRET.encode())], b'{"type":"create"}'),
        ("hubspot", [], b"[]"),
    ],
    ids=["generic", "opencti-token", "hubspot"],
)
async def test_the_per_assignment_route_refuses_before_the_dedup_cache(monkeypatch, caplog, provider, pairs, body):
    integration_id = str(uuid.uuid4())
    _serve(monkeypatch, SimpleNamespace(id=integration_id, provider=provider, extra_config_enc="enc"))
    monkeypatch.setattr(webhooks, "decrypt_value", _undecryptable)
    _forbid_dedup_claim(monkeypatch)
    with caplog.at_level(logging.ERROR, logger=webhooks.logger.name), pytest.raises(HTTPException) as exc_info:
        await webhooks.receive_assignment_webhook(integration_id, str(uuid.uuid4()), _delivery(pairs, body=body))
    _assert_refused(exc_info)
    assert any(integration_id in record.getMessage() for record in caplog.records)


# ── Routes, after the dedup claim ───────────────────────────────────────────


def _claim_and_record_release(monkeypatch) -> list:
    released: list = []

    async def _claim(*_args, **_kwargs):
        return False, None, "dedup-key"

    async def _release(key):
        released.append(key)

    monkeypatch.setattr(webhooks, "_claim_webhook_dedup_slot", _claim)
    monkeypatch.setattr(webhooks, "_release_webhook_dedup_slot", _release)
    return released


@pytest.mark.parametrize(("provider", "body"), [("github", b'{"action":"opened"}'), ("hubspot", b"[]")])
async def test_the_integration_route_re_check_refuses_and_releases_the_slot(monkeypatch, provider, body):
    """The first read finds no config (it was set in between), the re-read an
    unreadable one: the re-check refuses and the dedup slot is given back."""
    integration_id = str(uuid.uuid4())
    _serve(
        monkeypatch,
        SimpleNamespace(id=integration_id, provider=provider, extra_config_enc=None),
        SimpleNamespace(id=integration_id, provider=provider, extra_config_enc="enc"),
    )
    monkeypatch.setattr(webhooks, "decrypt_value", _undecryptable)
    released = _claim_and_record_release(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        await webhooks.receive_integration_webhook(integration_id, _delivery(body=body))
    _assert_refused(exc_info)
    assert released == ["dedup-key"]


@pytest.mark.parametrize(("provider", "body"), [("opencti", b'{"type":"create"}'), ("hubspot", b"[]")])
async def test_the_per_assignment_route_re_check_refuses_and_releases_the_slot(monkeypatch, provider, body):
    integration_id = str(uuid.uuid4())
    _serve(
        monkeypatch,
        SimpleNamespace(id=integration_id, provider=provider, extra_config_enc=None),
        SimpleNamespace(id=integration_id, provider=provider, extra_config_enc="enc"),
    )
    monkeypatch.setattr(webhooks, "decrypt_value", _undecryptable)
    released = _claim_and_record_release(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        await webhooks.receive_assignment_webhook(integration_id, str(uuid.uuid4()), _delivery(body=body))
    _assert_refused(exc_info)
    assert released == ["dedup-key"]


# ── The HubSpot batch handlers ──────────────────────────────────────────────


async def test_the_hubspot_batch_handlers_refuse_an_unreadable_config(monkeypatch):
    monkeypatch.setattr(webhooks, "decrypt_value", _undecryptable)

    def _no_session():
        raise AssertionError("an unverifiable batch must not reach the database")

    monkeypatch.setattr(webhooks, "async_session", _no_session)
    integration = SimpleNamespace(id=uuid.uuid4(), provider="hubspot", extra_config_enc="enc")
    with pytest.raises(HTTPException) as exc_info:
        await webhooks._handle_hubspot_webhook(str(integration.id), integration, b"[]", [], _delivery())
    _assert_refused(exc_info)
    with pytest.raises(HTTPException) as exc_info:
        await webhooks._handle_hubspot_assignment_webhook(
            str(integration.id), integration, b"[]", [], str(uuid.uuid4()), _delivery()
        )
    _assert_refused(exc_info)
