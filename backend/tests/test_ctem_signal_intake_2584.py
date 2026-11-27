# SPDX-License-Identifier: Apache-2.0

"""#2584: the signal inbox survives an OpenCTI catch-up without taking the web pool.

* repeated updates of one OpenCTI object are folded into the latest one;
* a delete, an OpenAEV delivery or one without an object id is never folded;
* a verified delivery is never throttled; an unsigned one, or one that fails verification, still is;
* the signal is recorded on the intake engine, never the web pool.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.services import ctem_signal_inbox as inbox
from app.services.ctem_signal_listeners import OPENCTI_EXTENSION


def _row(object_id: str, description: str, *, provider: str = "opencti", event_key: str = "update") -> tuple[Any, ...]:
    payload: dict[str, Any] = {
        "data": {
            "type": "vulnerability",
            "description": description,
            "extensions": {OPENCTI_EXTENSION: {"id": object_id}},
        }
    }
    return (uuid.uuid4(), provider, "integration-1", event_key, payload, True, 0)


def test_repeated_updates_of_one_object_fold_into_the_latest() -> None:
    first, other, latest = _row("obj-1", "first"), _row("obj-2", "other"), _row("obj-1", "latest")
    kept, folded = inbox.coalesce([first, other, latest])
    assert [row[4]["data"]["description"] for row in kept] == ["other", "latest"]
    assert folded == [first[0]]


def test_deletes_openaev_and_unidentified_deliveries_are_never_folded() -> None:
    deletes = [_row("obj-1", "a", event_key="delete"), _row("obj-1", "b", event_key="delete")]
    openaev = [_row("obj-1", "a", provider="openaev"), _row("obj-1", "b", provider="openaev")]
    unidentified = [_row("", "a"), _row("", "b")]
    for batch in (deletes, openaev, unidentified):
        kept, folded = inbox.coalesce(batch)
        assert kept == batch and folded == []


class _Db:
    def __init__(self, integration: Any, source: Any) -> None:
        self._integration, self._source = integration, source

    async def __aenter__(self) -> _Db:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, _query: Any) -> Any:
        integration = self._integration

        class _Result:
            def scalar_one_or_none(self) -> Any:
                return integration

        return _Result()

    async def get(self, _model: Any, _key: Any) -> Any:
        return self._source


async def _deliver(monkeypatch: Any, *, signed: bool, verify_fails: bool) -> tuple[list[str], int | None]:
    """One delivery to the signal-source receiver with its gates stubbed: the throttles taken and the error code."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.api.v1 import webhooks

    integration_id, source_id = str(uuid.uuid4()), str(uuid.uuid4())
    integration = SimpleNamespace(id=integration_id, provider="opencti", extra_config_enc="enc")
    source = SimpleNamespace(integration_id=integration_id, enabled=False)
    throttles: list[str] = []

    async def throttle(_request: Any, scope: str, **_kw: Any) -> None:
        throttles.append(scope)

    def verify(*_a: Any, **_kw: Any) -> None:
        if verify_fails:
            raise HTTPException(status_code=401, detail="Invalid OpenCTI token")

    async def body() -> bytes:
        return b"{}"

    monkeypatch.setattr(webhooks, "async_session", lambda: _Db(integration, source))
    monkeypatch.setattr(webhooks, "enforce_rate_limit", throttle)
    monkeypatch.setattr(webhooks, "_try_verify_webhook_signature", verify)
    monkeypatch.setattr(webhooks, "_verify_opencti_token_header", lambda *_a, **_kw: None)
    monkeypatch.setattr(webhooks, "_signed_with_webhook_secret", lambda _enc: signed)
    request = SimpleNamespace(body=body)
    try:
        await webhooks.receive_signal_source_webhook(integration_id, source_id, request, SimpleNamespace())  # type: ignore[arg-type]
    except HTTPException as exc:
        return throttles, exc.status_code
    return throttles, None


async def test_a_verified_delivery_is_never_throttled(monkeypatch: Any) -> None:
    assert await _deliver(monkeypatch, signed=True, verify_fails=False) == ([], None)


async def test_an_unsigned_delivery_is_still_throttled(monkeypatch: Any) -> None:
    assert await _deliver(monkeypatch, signed=False, verify_fails=False) == (["webhook-signal-source"], None)


async def test_a_delivery_that_fails_verification_is_throttled_then_refused(monkeypatch: Any) -> None:
    # Review of #2592: a bad token must not escape the per-source limit.
    assert await _deliver(monkeypatch, signed=True, verify_fails=True) == (["webhook-signal-source"], 401)


def test_the_intake_engine_is_its_own_pool() -> None:
    from app.core import database

    session = database.intake_session()
    try:
        bind: Any = session.bind
        assert bind is not database.engine
        assert bind.pool.size() >= 1
        assert bind.pool._max_overflow == 0
    finally:
        session.sync_session.close()
