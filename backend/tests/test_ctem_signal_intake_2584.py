# SPDX-License-Identifier: Apache-2.0

"""#2584: the signal inbox survives an OpenCTI catch-up without taking the web pool.

* repeated updates of one OpenCTI object are folded into the latest one;
* a delete, an OpenAEV delivery or one without an object id is never folded;
* a verified delivery is never throttled, an unsigned one still is;
* the signal is recorded on the intake engine, never the web pool.
"""

from __future__ import annotations

import inspect
import uuid
from typing import Any

from app.services import ctem_signal_inbox as inbox
from app.services.ctem_signal_listeners import OPENCTI_EXTENSION


def _row(object_id: str, description: str, *, provider: str = "opencti", event_key: str = "update") -> tuple:
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


def test_a_verified_delivery_is_never_throttled() -> None:
    from app.api.v1 import webhooks

    body = inspect.getsource(webhooks.receive_signal_source_webhook).split('"""', 2)[2]
    throttle = body.index('"webhook-signal-source"')
    # The per-source limit sits under "if not verified": an OpenCTI notifier never retries.
    assert "if not verified:" in body[:throttle]
    assert body.index("verified = _signed_with_webhook_secret") < throttle


def test_the_intake_engine_is_its_own_pool() -> None:
    from app.core import database

    session = database.intake_session()
    try:
        assert session.bind is not database.engine
        assert session.bind.pool.size() >= 1
        assert session.bind.pool._max_overflow == 0
    finally:
        session.sync_session.close()
