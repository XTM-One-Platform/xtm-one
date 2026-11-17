"""Coverage for the OpenCTI ↔ OpenAEV link status in ``GET /platform/config``.

The public config payload carries ``integration_status`` so the state of the
integration link between OpenCTI and OpenAEV — and how fresh that answer is —
can be read in a single unauthenticated call (status page, external probe,
support triage) without hitting the authenticated
``/platform/registrations`` endpoint.

These tests pin:
  * the tri-state mapping per product (connected / disconnected / not_registered),
  * ``linked`` being true only when BOTH products are connected,
  * ``last_checked_at`` = the last heartbeat (the de-facto verification), with
    the aggregate taking the freshest of the two,
  * the payload staying free of the URL / title / version / license details
    that belong to the authenticated endpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.api.v1.platform import (
    PLATFORM_LINK_CONNECTED,
    PLATFORM_LINK_DISCONNECTED,
    PLATFORM_LINK_IDENTIFIERS,
    PLATFORM_LINK_NOT_REGISTERED,
    PlatformConfigResponse,
    PlatformIntegrationStatus,
    PlatformLinkStatus,
    _build_integration_status,
    _load_integration_status,
)
from app.models.platform_registration import (
    HEARTBEAT_HEALTHY_WINDOW,
    PlatformRegistration,
)

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def _reg(identifier: str, **overrides) -> PlatformRegistration:
    """An in-memory registration (no DB session needed for the pure helper)."""
    defaults = {
        "platform_identifier": identifier,
        "platform_url": f"https://{identifier}.example",
        "platform_title": identifier.upper(),
        "platform_version": "6.0",
        "status": "active",
        "last_heartbeat_at": NOW,
        "registered_at": NOW - timedelta(days=1),
    }
    defaults.update(overrides)
    return PlatformRegistration(**defaults)


def test_no_registrations_reports_not_registered_and_unlinked():
    status = _build_integration_status([], NOW)
    assert status.linked is False
    assert status.last_checked_at is None
    for product in (status.opencti, status.openaev):
        assert product.status == PLATFORM_LINK_NOT_REGISTERED
        assert product.connected is False
        assert product.last_checked_at is None


def test_both_fresh_is_linked():
    status = _build_integration_status(
        [_reg("opencti"), _reg("openaev", last_heartbeat_at=NOW - timedelta(minutes=1))],
        NOW,
    )
    assert status.opencti.status == PLATFORM_LINK_CONNECTED
    assert status.openaev.status == PLATFORM_LINK_CONNECTED
    assert status.linked is True
    # The aggregate reports the freshest of the two verifications.
    assert status.last_checked_at == NOW.isoformat()


def test_stale_heartbeat_is_disconnected_and_breaks_the_link():
    stale = NOW - HEARTBEAT_HEALTHY_WINDOW - timedelta(minutes=1)
    status = _build_integration_status([_reg("opencti"), _reg("openaev", last_heartbeat_at=stale)], NOW)
    assert status.opencti.connected is True
    assert status.openaev.status == PLATFORM_LINK_DISCONNECTED
    assert status.openaev.connected is False
    # Still reports WHEN the stale product was last seen — that timestamp is
    # the whole point of the field for triage.
    assert status.openaev.last_checked_at == stale.isoformat()
    assert status.linked is False


def test_inactive_registration_is_disconnected():
    status = _build_integration_status([_reg("opencti", status="inactive"), _reg("openaev")], NOW)
    assert status.opencti.status == PLATFORM_LINK_DISCONNECTED
    assert status.linked is False


def test_one_product_missing_is_not_linked():
    status = _build_integration_status([_reg("opencti")], NOW)
    assert status.opencti.status == PLATFORM_LINK_CONNECTED
    assert status.openaev.status == PLATFORM_LINK_NOT_REGISTERED
    assert status.linked is False
    assert status.last_checked_at == NOW.isoformat()


def test_other_products_are_ignored():
    """An OpenCRQ registration must not leak into the OpenCTI/OpenAEV link."""
    status = _build_integration_status([_reg("opencrq"), _reg("opencti"), _reg("openaev")], NOW)
    assert status.linked is True
    assert set(PLATFORM_LINK_IDENTIFIERS) == {"opencti", "openaev"}
    assert not hasattr(status, "opencrq")


def test_missing_heartbeat_falls_back_to_registration_time():
    registered = NOW - timedelta(hours=2)
    status = _build_integration_status([_reg("opencti", last_heartbeat_at=None, registered_at=registered)], NOW)
    # ``is_healthy()`` is false without a heartbeat, but the row is still
    # reported as registered-and-last-seen rather than 500ing the payload.
    assert status.opencti.status == PLATFORM_LINK_DISCONNECTED
    assert status.opencti.last_checked_at == registered.isoformat()


def test_naive_heartbeat_is_normalized_to_utc():
    naive = NOW.replace(tzinfo=None)
    status = _build_integration_status([_reg("opencti", last_heartbeat_at=naive)], NOW)
    assert status.opencti.last_checked_at == NOW.isoformat()


def test_non_utc_offset_is_converted_to_utc():
    """The contract says UTC — a non-UTC offset must be converted, not passed through.

    ``last_heartbeat_at`` is a ``timestamptz``; which offset a driver/session
    hands back is not the API's business, but the serialized value must always
    carry ``+00:00`` so consumers can compare timestamps without parsing
    offsets.
    """
    paris = NOW.astimezone(timezone(timedelta(hours=2)))
    assert paris.utcoffset() != timedelta(0)  # guard: the input really is offset
    status = _build_integration_status([_reg("opencti", last_heartbeat_at=paris)], NOW)
    assert status.opencti.last_checked_at == NOW.isoformat()
    assert status.opencti.last_checked_at.endswith("+00:00")
    assert status.last_checked_at.endswith("+00:00")


def test_aggregate_timestamp_compares_datetimes_not_strings():
    """A naive-then-normalized value must not sort above an offset-suffixed one.

    ``"2026-07-30T12:00:00" > "2026-07-30T13:00:00+00:00"`` is false as a string
    compare only by luck of digit ordering — the aggregate must be a datetime
    max, so the newer (naive) heartbeat still wins.
    """
    newer_naive = (NOW - timedelta(minutes=1)).replace(tzinfo=None)
    older_aware = NOW - timedelta(hours=3)
    status = _build_integration_status(
        [
            _reg("opencti", last_heartbeat_at=newer_naive),
            _reg("openaev", last_heartbeat_at=older_aware),
        ],
        NOW,
    )
    assert status.last_checked_at == (NOW - timedelta(minutes=1)).isoformat()


def test_status_and_connected_never_disagree():
    status = _build_integration_status([_reg("opencti"), _reg("openaev", status="inactive")], NOW)
    for product in (status.opencti, status.openaev):
        assert product.connected == (product.status == PLATFORM_LINK_CONNECTED)


def test_status_is_a_closed_tri_state_in_the_schema():
    """The public contract must advertise the enum, not a free-form string.

    Consumers generate clients off the OpenAPI schema, so the three allowed
    values have to be visible there — and an out-of-range value must fail
    validation rather than ship silently.
    """
    schema = PlatformLinkStatus.model_json_schema()["properties"]["status"]
    assert set(schema["enum"]) == {
        PLATFORM_LINK_CONNECTED,
        PLATFORM_LINK_DISCONNECTED,
        PLATFORM_LINK_NOT_REGISTERED,
    }
    with pytest.raises(ValidationError):
        PlatformLinkStatus(status="degraded")


def test_payload_exposes_only_state_and_timestamp():
    """The endpoint is unauthenticated — no URLs, titles, versions, licenses."""
    product_fields = set(_build_integration_status([_reg("opencti")], NOW).opencti.model_dump())
    assert product_fields == {"status", "connected", "last_checked_at"}
    assert set(PlatformIntegrationStatus().model_dump()) == {
        "opencti",
        "openaev",
        "linked",
        "last_checked_at",
    }


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


class _StubSession:
    """Minimal ``AsyncSession`` stand-in — records the statement, returns rows."""

    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def execute(self, statement):
        self.statements.append(str(statement))
        return _Result(self.rows)


async def test_loader_filters_on_the_two_products():
    """The query must scope to OpenCTI/OpenAEV rather than scanning every row."""
    # The loader stamps its own ``now``, so the rows must be fresh in wall-clock
    # terms rather than relative to the pinned ``NOW``.
    fresh = datetime.now(UTC)
    db = _StubSession(
        [
            _reg("opencti", last_heartbeat_at=fresh),
            _reg("openaev", last_heartbeat_at=fresh),
        ]
    )
    status = await _load_integration_status(db)
    assert status.linked is True
    assert len(db.statements) == 1
    where = db.statements[0]
    assert "platform_registrations.platform_identifier IN" in where
    # No ``status = 'active'`` prefilter: an explicitly deactivated
    # registration must still be reported as ``disconnected`` (with its last
    # verification time) rather than collapsing to ``not_registered``.
    assert "status" not in where.split("WHERE", 1)[1]


async def test_loader_reports_not_registered_on_empty_table():
    status = await _load_integration_status(_StubSession([]))
    assert status.opencti.status == PLATFORM_LINK_NOT_REGISTERED
    assert status.openaev.status == PLATFORM_LINK_NOT_REGISTERED
    assert status.linked is False
    assert status.last_checked_at is None


def test_config_response_defaults_to_unlinked():
    """``integration_status`` is always present, so consumers can read it
    unconditionally in both XTM One and Copilot modes."""
    field = PlatformConfigResponse.model_fields["integration_status"]
    assert field.is_required() is False
    default = PlatformIntegrationStatus()
    assert default.linked is False
    assert default.opencti.status == PLATFORM_LINK_NOT_REGISTERED
    assert default.openaev.status == PLATFORM_LINK_NOT_REGISTERED
