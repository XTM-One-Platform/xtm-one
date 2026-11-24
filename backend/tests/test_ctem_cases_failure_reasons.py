# SPDX-License-Identifier: Apache-2.0
"""CTEM Cases name why a product did not answer (#1852).

A missing product carries a reason (rejected, too_large, timeout,
not_connected, unavailable) from the read to the cached viewer view and the
list API; one identity's rejected token is never a product outage on the
shared freshness line; the log context names the reader, never the
integration of an earlier token resolution.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import structlog

from app.services import ctem_cases_refresh as refresh
from app.services import ctem_cases_source as source
from app.services.ctem_cases_view import ViewerView

STACK = [{"name": "Tomcat", "vendor": "Apache", "version": "9.0.1"}]
DB = cast(Any, SimpleNamespace())


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        ("OpenCTI: AUTH_REQUIRED 401 You must be logged in", source.REASON_REJECTED),
        ("403 Forbidden", source.REASON_REJECTED),
        ("OpenAEV search CVE findings failed: Forbidden", source.REASON_REJECTED),
        ("500 DATABASE_ERROR too_many_clauses: maxClauseCount is set to 1024", source.REASON_TOO_LARGE),
        ("GraphQL error: too many clauses", source.REASON_TOO_LARGE),
        ("request timed out", source.REASON_TIMEOUT),
        (httpx.ReadTimeout("slow"), source.REASON_TIMEOUT),
        ("502 Bad Gateway", source.REASON_UNAVAILABLE),
        (None, source.REASON_UNAVAILABLE),
        ("", source.REASON_UNAVAILABLE),
    ],
)
def test_failure_reason_maps_each_kind(error: str | BaseException | None, reason: str) -> None:
    assert source.failure_reason(error) == reason


def _patch_reads(
    monkeypatch: pytest.MonkeyPatch,
    *,
    octi_err: str | None,
    aev_err: str | None,
    creds_for: tuple[str, ...] = ("opencti", "openaev"),
) -> None:
    async def stack(db: Any) -> list[dict[str, str]]:
        return STACK

    async def creds(db: Any, user_id: str, provider: str) -> tuple[str, dict[str, str]] | None:
        if provider not in creds_for:
            return None
        # What integration_tools._get_token leaves on the log context.
        structlog.contextvars.bind_contextvars(integration_name=f"{provider} integration")
        return (f"https://{provider}", {})

    async def read_opencti(client: Any, base_url: str, headers: Any, stack: Any) -> source.OpenCtiRead | None:
        if octi_err:
            source._failed(octi_err)
            return None
        return source.OpenCtiRead([])

    async def read_openaev(client: Any, base_url: str, headers: Any, now: Any) -> list[Any] | None:
        if aev_err:
            source._failed(aev_err)
            return None
        return []

    monkeypatch.setattr(source, "_organization_software", stack)
    monkeypatch.setattr(source, "_user_creds", creds)
    monkeypatch.setattr(source, "_read_opencti", read_opencti)
    monkeypatch.setattr(source, "_read_openaev", read_openaev)
    monkeypatch.setattr(source, "attach_probe", lambda headers, base_url: headers)


@pytest.mark.asyncio
async def test_read_records_a_reason_per_missing_product(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_reads(monkeypatch, octi_err="AUTH_REQUIRED 401", aev_err="500 DATABASE_ERROR too_many_clauses")
    read = await source.read_open_issues(DB, "u-1", "cyber")
    assert read.missing == ["opencti", "openaev"]
    assert read.reasons == {"opencti": "rejected", "openaev": "too_large"}


@pytest.mark.asyncio
async def test_no_credentials_is_not_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_reads(monkeypatch, octi_err=None, aev_err=None, creds_for=("opencti",))
    read = await source.read_open_issues(DB, "u-1", "cyber")
    assert read.missing == ["openaev"] and read.reasons == {"openaev": "not_connected"}


@pytest.mark.asyncio
async def test_a_raised_timeout_is_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_reads(monkeypatch, octi_err=None, aev_err=None)

    async def slow(client: Any, base_url: str, headers: Any, stack: Any) -> source.OpenCtiRead | None:
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(source, "_read_opencti", slow)
    read = await source.read_open_issues(DB, "u-1", "cyber")
    assert read.reasons == {"opencti": "timeout"}


@pytest.mark.asyncio
async def test_log_context_names_the_reader_and_leaks_no_integration(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    _patch_reads(monkeypatch, octi_err=None, aev_err=None)

    async def read_opencti(client: Any, base_url: str, headers: Any, stack: Any) -> source.OpenCtiRead | None:
        seen.append(dict(structlog.contextvars.get_contextvars()))
        return source.OpenCtiRead([])

    monkeypatch.setattr(source, "_read_opencti", read_opencti)
    structlog.contextvars.clear_contextvars()
    await source.read_open_issues(DB, "u-42", "cyber")
    # The OpenAEV token resolution ran last before the OpenCTI read: its name is gone.
    assert seen == [{"ctem_read_user_id": "u-42", "ctem_read_provider": "opencti"}]
    assert structlog.contextvars.get_contextvars() == {}


def test_viewer_view_keeps_reasons_and_reads_an_old_cache_as_unavailable() -> None:
    view = ViewerView(missing=["opencti"], reasons={"opencti": "rejected"})
    assert ViewerView.from_json(view.to_json()).reasons == {"opencti": "rejected"}
    old = '{"issues": [], "missing": ["openaev"], "complete": true}'
    assert ViewerView.from_json(old).reasons == {"openaev": "unavailable"}


def test_an_identity_rejection_is_not_an_outage() -> None:
    both = {"missing": ["opencti", "openaev"], "reasons": {"opencti": "rejected", "openaev": "not_connected"}}
    assert refresh.outage_products(both) == []
    assert refresh.outage_products({"missing": ["opencti"], "reasons": {"opencti": "too_large"}}) == ["opencti"]
    # An answer without reasons (an older caller) keeps naming the product.
    assert refresh.outage_products({"missing": ["opencti"]}) == ["opencti"]


@pytest.mark.asyncio
async def test_record_freshness_skips_an_identity_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def redis() -> Any:
        calls.append("redis")
        raise AssertionError("an identity failure must not write the shared line")

    monkeypatch.setattr(refresh, "_redis", redis)
    answer = {
        "applied": False,
        "missing": ["opencti", "openaev"],
        "reasons": {"opencti": "rejected", "openaev": "rejected"},
    }
    await refresh.record_freshness("cyber", answer, datetime.now(UTC))
    assert calls == []
