"""Tests for the inherited-Enterprise-Edition Filigran AI entitlement fix.

When Enterprise Edition is inherited from a connected EE OpenCTI/OpenAEV (no
Filigran licence of its own - the ``ee_platform`` agentic source) "import doc
AI" must be entitled to the included allowance just like chat. The extraction
path achieves this by forwarding a connected EE
platform's stored licence PEM. When no PEM is resolvable *anywhere*, the refusal
must surface as a real tool error (logged + ``is_error``) instead of a silent
success string — the false-200 that hid the denial from operators.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

import app.integrations.providers.filigran_ai as filigran_ai
from app.tools.base import ToolResult


class _FakeReg:
    def __init__(self, *, pem_enc, healthy=True, identifier="opencti"):
        self.enterprise_license_pem_enc = pem_enc
        self.platform_identifier = identifier
        self._healthy = healthy

    def is_healthy(self):
        return self._healthy


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalars(self._rows)


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _statement):
        return _FakeResult(self._rows)


@pytest.mark.asyncio
async def test_resolve_connected_platform_pem_returns_healthy_validated_pem(
    monkeypatch,
):
    """The most-recent healthy, validated registration's PEM is decrypted
    and returned."""
    monkeypatch.setattr(
        "app.core.security.decrypt_value",
        lambda blob: "-----BEGIN CERTIFICATE-----\nDECRYPTED\n-----END CERTIFICATE-----"
        if blob == "enc-good"
        else "",
    )
    db = _FakeDB([_FakeReg(pem_enc="enc-good")])

    pem = await filigran_ai._resolve_connected_platform_pem(db)

    assert pem is not None
    assert "DECRYPTED" in pem


@pytest.mark.asyncio
async def test_resolve_connected_platform_pem_skips_unhealthy(monkeypatch):
    """An unhealthy (stale heartbeat) registration is not used even when it
    carries a stored PEM — a disconnected platform must not keep granting AI."""
    monkeypatch.setattr(
        "app.core.security.decrypt_value", lambda blob: "SHOULD-NOT-BE-USED"
    )
    db = _FakeDB([_FakeReg(pem_enc="enc-good", healthy=False)])

    pem = await filigran_ai._resolve_connected_platform_pem(db)

    assert pem is None


@pytest.mark.asyncio
async def test_resolve_connected_platform_pem_skips_undecryptable(monkeypatch):
    """A row whose PEM can't be decrypted is skipped in favour of the next
    valid one instead of aborting resolution."""

    def _decrypt(blob):
        if blob == "enc-bad":
            raise ValueError("corrupt")
        return "GOOD-PEM"

    monkeypatch.setattr("app.core.security.decrypt_value", _decrypt)
    db = _FakeDB(
        [
            _FakeReg(pem_enc="enc-bad", identifier="openaev"),
            _FakeReg(pem_enc="enc-ok", identifier="opencti"),
        ]
    )

    pem = await filigran_ai._resolve_connected_platform_pem(db)

    assert pem == "GOOD-PEM"


@pytest.mark.asyncio
async def test_call_api_refusal_is_tool_error_and_logged(monkeypatch, caplog):
    """When no licence resolves anywhere, ``_call_api`` returns a
    ``ToolResult`` with ``is_error=True`` (not a silent success string) and
    logs a WARNING so the denial is visible."""

    async def _no_pem():
        return None

    monkeypatch.setattr(filigran_ai, "_resolve_pem", _no_pem)

    # Non-empty file context so we reach the PEM check (past the "no files"
    # early return) without needing a real upload.
    file_ctx = SimpleNamespace(is_empty=False)

    with caplog.at_level(logging.WARNING, logger=filigran_ai.logger.name):
        result = await filigran_ai._call_api(
            "https://stix.example.com",
            "/stix",
            file_ctx,  # type: ignore[arg-type]
            "extract_stix",
        )

    assert isinstance(result, ToolResult)
    assert result.is_error is True
    payload = json.loads(result.content)
    assert "error" in payload
    assert "enterprise licence" in payload["error"].lower()
    assert any(
        r.levelname == "WARNING" and "no enterprise licence" in r.getMessage().lower()
        for r in caplog.records
    ), f"expected a WARNING log; observed {[r.getMessage() for r in caplog.records]}"


@pytest.mark.asyncio
async def test_call_api_no_files_is_tool_error(monkeypatch, caplog):
    """A no-input call (no files, no usable text) is refused before any work,
    so ``_call_api`` returns a ``ToolResult`` with ``is_error=True`` — not a
    silent success string — so the pipeline can skip charging the agentic quota
    for a call that never ran."""

    # Guard: even if we somehow got past the early return, a resolvable PEM
    # must NOT turn this into a success — the no-input check comes first.
    async def _has_pem():  # pragma: no cover - should never be reached
        raise AssertionError("_resolve_pem must not be called for a no-input call")

    monkeypatch.setattr(filigran_ai, "_resolve_pem", _has_pem)

    empty_ctx = SimpleNamespace(is_empty=True)

    with caplog.at_level(logging.WARNING, logger=filigran_ai.logger.name):
        result = await filigran_ai._call_api(
            "https://stix.example.com",
            "/stix",
            empty_ctx,  # type: ignore[arg-type]
            "extract_stix",
        )

    assert isinstance(result, ToolResult)
    assert result.is_error is True
    payload = json.loads(result.content)
    assert "no files attached" in payload["error"].lower()
    assert any(
        r.levelname == "WARNING" and "no files attached" in r.getMessage().lower()
        for r in caplog.records
    ), f"expected a WARNING log; observed {[r.getMessage() for r in caplog.records]}"


@pytest.mark.parametrize(
    ("product", "expected_header"),
    [
        ("opencti", "X-OpenCTI-Certificate"),
        ("OpenCTI", "X-OpenCTI-Certificate"),
        ("openaev", "X-OpenAEV-Certificate"),
        ("obas", "X-OpenAEV-Certificate"),
        ("openbas", "X-OpenAEV-Certificate"),
        ("xtm", "X-XTMOne-Certificate"),  # unknown product -> XTM One fallback
        ("", "X-XTMOne-Certificate"),  # no product OID -> XTM One fallback
    ],
)
def test_header_for_platform_pem_maps_product(monkeypatch, product, expected_header):
    """A connected platform's PEM is forwarded under the header matching the
    product that issued it, so the webservice validates it against the right
    signing key (an OpenCTI licence under ``X-XTMOne-Certificate`` is rejected
    with 401 Invalid certificate)."""
    monkeypatch.setattr(
        "app.enterprise.licensing.extract_license_product",
        lambda _pem: product,
    )

    assert filigran_ai._header_for_platform_pem("PEM") == expected_header


def test_header_for_platform_pem_defaults_on_parse_error(monkeypatch):
    """If the product can't be read, default to the XTM One header rather than
    failing resolution."""

    def _boom(_pem):
        raise ValueError("unparseable")

    monkeypatch.setattr(
        "app.enterprise.licensing.extract_license_product", _boom
    )

    assert filigran_ai._header_for_platform_pem("PEM") == "X-XTMOne-Certificate"
