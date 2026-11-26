# SPDX-License-Identifier: Apache-2.0

"""Generic signed inbound webhook sources of the CTEM trigger paths (#2506)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from app.core.security import encrypt_value
from app.services import ctem_signals
from app.services import ctem_webhook_sources as webhooks

SECRET = "s" * 43


def _row(*, enabled: bool = True, mapping: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        trigger_path="new_exposure",
        name="Scanner",
        enabled=enabled,
        subject_kind="asset",
        secret_enc=encrypt_value(SECRET),
        mapping=mapping or {"id": "event.id", "label": "event.host", "severity": "event.severity"},
        last_delivery_at=None,
        last_error=None,
    )


@pytest.fixture
def recorded(monkeypatch) -> list:
    calls: list = []

    async def record_signal(_db, *, organization_id, signal, recorded_via, recorded_by, now=None):
        calls.append(ctem_signals.normalize(signal))
        return SimpleNamespace(id=uuid.uuid4()), True

    async def get_primary(_db):
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(ctem_signals, "record_signal", record_signal)
    monkeypatch.setattr("app.services.organizations.get_primary", get_primary)
    return calls


BODY = json.dumps({"event": {"id": "evt-42", "host": "vpn.acme.example", "severity": "HIGH"}}).encode()


async def test_a_signed_post_records_a_signal_with_the_mapped_fields(recorded) -> None:
    row = _row()
    out = await webhooks.receive(None, row, BODY, webhooks.sign(SECRET, BODY))
    assert out["status"] == "ok" and out["recorded"] is True
    [signal] = recorded
    assert signal.trigger_path == "new_exposure" and signal.source_provider == "webhook"
    assert signal.source_external_id == "evt-42" and signal.subject_label == "vpn.acme.example"
    assert signal.evidence["severity"] == "high"
    assert signal.evidence[ctem_signals.SOURCE_KEY_EVIDENCE] == f"webhook:webhook:{row.id}"
    assert row.last_error is None and row.last_delivery_at is not None


@pytest.mark.parametrize(
    "header", [None, "", "sha256=00", webhooks.sign("another secret entirely, 43 chars long!!", BODY)]
)
async def test_a_wrong_or_missing_signature_is_refused_and_records_nothing(recorded, header) -> None:
    row = _row()
    with pytest.raises(webhooks.SignatureError):
        await webhooks.receive(None, row, BODY, header)
    assert recorded == [] and row.last_delivery_at is None


async def test_the_signature_is_checked_even_on_a_source_that_is_off(recorded) -> None:
    with pytest.raises(webhooks.SignatureError):
        await webhooks.receive(None, _row(enabled=False), BODY, None)
    out = await webhooks.receive(None, _row(enabled=False), BODY, webhooks.sign(SECRET, BODY))
    assert out == {"status": "ignored", "reason": "webhook source is off", "recorded": False}
    assert recorded == []


async def test_a_post_without_the_mapped_id_records_nothing_and_says_why(recorded) -> None:
    row = _row(mapping={"id": "event.uuid"})
    out = await webhooks.receive(None, row, BODY, webhooks.sign(SECRET, BODY))
    assert out["status"] == "unmapped" and recorded == []
    assert "event.uuid" in row.last_error


async def test_a_rotated_secret_refuses_the_old_one(recorded) -> None:
    row = _row()
    new = webhooks.rotate(row)
    assert new != SECRET and row.secret_rotated_at is not None
    with pytest.raises(webhooks.SignatureError):
        await webhooks.receive(None, row, BODY, webhooks.sign(SECRET, BODY))
    assert (await webhooks.receive(None, row, BODY, webhooks.sign(new, BODY)))["recorded"] is True


def test_a_mapping_needs_the_id_and_reads_dotted_paths() -> None:
    with pytest.raises(webhooks.WebhookSourceError):
        webhooks.clean_mapping({"label": "name"})
    assert webhooks.clean_mapping({"id": " a.b ", "label": "", "other": "x"}) == {"id": "a.b"}
    payload = {"items": [{"name": "x", "id": 7}]}
    assert webhooks.read_path(payload, "items.0.id") == 7
    assert webhooks.read_path(payload, "items.3.id") is None
    assert webhooks.map_payload({"id": "items.0.id"}, payload).external_id == "7"


async def test_a_new_source_is_off_and_never_shows_its_secret() -> None:
    added: list = []

    async def flush() -> None:
        return None

    db = SimpleNamespace(add=added.append, flush=flush)
    row, secret = await webhooks.create(
        db,
        trigger_path="new_exposure",
        name=" Scanner ",
        subject_kind="asset",
        mapping={"id": "event.id"},
        created_by=None,
    )
    assert added == [row] and row.enabled is False and row.name == "Scanner"
    assert secret and secret not in row.secret_enc
    out = webhooks.to_dict(row)
    assert out["enabled"] is False and "secret" not in out and "secret_enc" not in out
    assert out["receiver_path"] == f"/api/hooks/ctem-webhook-sources/{row.id}"
    with pytest.raises(webhooks.WebhookSourceError):
        await webhooks.create(
            db, trigger_path="nope", name="x", subject_kind="asset", mapping={"id": "a"}, created_by=None
        )


def test_a_webhook_source_is_listed_on_its_path_with_the_counts() -> None:
    """The path list counts enabled / available sources; a webhook source joins its path (#2506)."""
    on, off = _row(), _row(enabled=False)
    off.trigger_path = "surface_drift"
    rows = {
        r["id"]: r
        for r in ctem_signals.path_catalog({}, bound={("shodan", "pass")}, webhooks=webhooks.path_sources_of([on, off]))
    }
    exposure = rows["new_exposure"]
    hook = next(s for s in exposure["sources"] if s["kind"] == "webhook")
    assert hook["bound"] is True and hook["key"] == f"webhook:webhook:{on.id}" and hook["name"] == "Scanner"
    assert exposure["sources_enabled"] == 2  # shodan pass and the webhook
    drift = rows["surface_drift"]
    assert drift["sources_enabled"] == 1 and drift["sources_available"] == 2
    # A path with nothing connected here: 0 of 0, never the possible sources.
    assert rows["code_flaw"]["sources_enabled"] == 0 and rows["code_flaw"]["sources_available"] == 0


def test_nothing_changes_for_a_tenant_without_webhook_sources() -> None:
    before = ctem_signals.path_catalog({}, bound={("shodan", "pass")})
    after = ctem_signals.path_catalog({}, bound={("shodan", "pass")}, webhooks={})
    assert [(r["id"], r["status"], r["sources"]) for r in before] == [
        (r["id"], r["status"], r["sources"]) for r in after
    ]
