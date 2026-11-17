"""Tests for ``import_opencti_dashboard`` STIX-ID verification.

Dashboard creation is LLM-driven, and the agents occasionally embed
hallucinated / stale STIX ``standard_id``s into widget filters
(``regardingOf.id``, ``toId``, ``fromId``, ``objects`` …). ``import_opencti_
dashboard`` is the single choke point every generated dashboard passes
through, so it batch-verifies every embedded STIX ID against the live
OpenCTI instance before uploading.

These tests pin that guard:
  * Confirmed-missing IDs ⇒ the import is rejected (with the offending IDs)
    and nothing is uploaded.
  * All IDs resolve ⇒ the dashboard is uploaded normally.
  * A verification error (connectivity / GraphQL) ⇒ fail open: the import
    proceeds rather than being blocked by an infra hiccup.
  * An unparseable pre-encoded ``configuration.manifest`` (invalid base64)
    ⇒ no crash; verification is skipped and the upload still runs.
  * The ``_collect_dashboard_stix_ids`` collector matches genuine entity
    STIX IDs while ignoring bare widget UUIDs and relationship-like IDs.
"""

from __future__ import annotations

import json

import pytest

from app.integrations.providers import opencti
from app.integrations.providers.opencti import knowledge as opencti_knowledge
from app.integrations.providers.opencti.knowledge import (
    _collect_dashboard_stix_ids,
)
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://demo.opencti.io||test-token"

_REAL_ID = "intrusion-set--7e3b0c2a-1111-2222-3333-444455556666"
_FAKE_ID = "intrusion-set--deadbeef-0000-0000-0000-000000000000"
_WIDGET_UUID = "11111111-1111-1111-1111-111111111111"


def _build_executor() -> CallableToolExecutor:
    executor = CallableToolExecutor()
    opencti.build_tools(_TOKEN, executor)
    return executor


def _raw_dashboard_json(*stix_ids: str) -> str:
    """A raw_widgets dashboard payload scoped to *stix_ids* via regardingOf."""
    return json.dumps(
        {
            "name": "Threat Dashboard",
            "description": "test",
            "widgets": {
                _WIDGET_UUID: {
                    "id": _WIDGET_UUID,
                    "layout": {"i": _WIDGET_UUID, "w": 4, "h": 4, "x": 0, "y": 0},
                    "dataSelection": [
                        {
                            "filters": {
                                "mode": "and",
                                "filters": [
                                    {
                                        "key": "regardingOf",
                                        "values": [
                                            {"key": "id", "values": list(stix_ids)}
                                        ],
                                    }
                                ],
                                "filterGroups": [],
                            }
                        }
                    ],
                }
            },
        }
    )


def _patch_verify(monkeypatch):
    """Patch ``_graphql`` so each aliased ``stixCoreObject`` lookup resolves
    unless the STIX ID is ``_FAKE_ID`` (which comes back missing)."""

    async def _fake_graphql(client, base_url, headers, query, variables):
        data = {
            alias: (None if sid == _FAKE_ID else {"id": "x", "standard_id": sid})
            for alias, sid in variables.items()
        }
        return data, None

    monkeypatch.setattr(opencti_knowledge, "_graphql", _fake_graphql)


def _patch_upload(monkeypatch) -> dict:
    """Capture the dashboard upload so tests can assert whether it ran."""
    captured: dict = {}

    async def _fake_upload(
        client,
        base_url,
        auth_headers,
        query,
        variables,
        file_var_path,
        file_name,
        file_bytes,
        mime_type,
    ):
        captured["file_name"] = file_name
        captured["file_bytes"] = file_bytes
        return ({"workspaceConfigurationImport": "ws-123"}, None)

    monkeypatch.setattr(opencti_knowledge, "_graphql_upload", _fake_upload)
    return captured


# ── _collect_dashboard_stix_ids (pure collector) ───────────────────────


def test_collect_dashboard_stix_ids_matches_entity_ids_only():
    widget_map = {
        _WIDGET_UUID: {
            "id": _WIDGET_UUID,  # bare widget UUID — must NOT be collected
            "layout": {"i": _WIDGET_UUID},  # ditto
            "dataSelection": [
                {
                    "filters": {
                        "filters": [
                            {"key": "regardingOf", "values": [{"key": "id", "values": [_REAL_ID]}]},
                            {"key": "toId", "values": ["malware--aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]},
                            {"key": "objects", "values": ["report--12121212-3434-5656-7878-909090909090"]},
                            # relationship-like IDs are excluded from the check.
                            {"key": "fromId", "values": ["relationship--99999999-8888-7777-6666-555555555555"]},
                            {"key": "x", "values": ["sighting--11112222-3333-4444-5555-666677778888"]},
                        ]
                    }
                }
            ],
        }
    }

    found = _collect_dashboard_stix_ids(widget_map)

    assert _REAL_ID in found
    assert "malware--aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in found
    assert "report--12121212-3434-5656-7878-909090909090" in found
    # Bare widget UUIDs (no ``type--`` prefix) are never matched.
    assert _WIDGET_UUID not in found
    # relationship-- / sighting-- are explicitly excluded.
    assert not any(sid.startswith(("relationship--", "sighting--")) for sid in found)


# ── import_opencti_dashboard verification behaviour ────────────────────


@pytest.mark.asyncio
async def test_import_rejects_missing_stix_ids(monkeypatch):
    _patch_verify(monkeypatch)
    captured = _patch_upload(monkeypatch)

    executor = _build_executor()
    result = await executor.execute_tool(
        ToolCall(
            id="d1",
            name="import_opencti_dashboard",
            arguments={
                "dashboard_json": _raw_dashboard_json(_FAKE_ID),
                "raw_widgets": True,
            },
        )
    )

    assert result.is_error is False
    assert "Dashboard NOT imported" in result.content
    assert _FAKE_ID in result.content
    # A rejected dashboard must never be uploaded.
    assert "file_bytes" not in captured


@pytest.mark.asyncio
async def test_import_proceeds_when_all_ids_resolve(monkeypatch):
    _patch_verify(monkeypatch)
    captured = _patch_upload(monkeypatch)

    executor = _build_executor()
    result = await executor.execute_tool(
        ToolCall(
            id="d2",
            name="import_opencti_dashboard",
            arguments={
                "dashboard_json": _raw_dashboard_json(_REAL_ID),
                "raw_widgets": True,
            },
        )
    )

    assert result.is_error is False
    assert "imported successfully" in result.content.lower()
    # The verified dashboard was uploaded.
    assert captured.get("file_bytes") is not None


@pytest.mark.asyncio
async def test_import_fails_open_on_verification_error(monkeypatch):
    """A verification error (connectivity / GraphQL) must not block a
    potentially valid import — the dashboard is uploaded anyway."""

    async def _erroring_graphql(client, base_url, headers, query, variables):
        return {}, "GraphQL connection refused"

    monkeypatch.setattr(opencti_knowledge, "_graphql", _erroring_graphql)
    captured = _patch_upload(monkeypatch)

    executor = _build_executor()
    result = await executor.execute_tool(
        ToolCall(
            id="d3",
            name="import_opencti_dashboard",
            arguments={
                # _FAKE_ID would normally be rejected, but the verification
                # itself errors, so the guard fails open and uploads.
                "dashboard_json": _raw_dashboard_json(_FAKE_ID),
                "raw_widgets": True,
            },
        )
    )

    assert result.is_error is False
    assert "imported successfully" in result.content.lower()
    assert captured.get("file_bytes") is not None


@pytest.mark.asyncio
async def test_import_with_unparseable_manifest_skips_verification(monkeypatch):
    """An invalid base64 ``configuration.manifest`` must not crash the tool;
    verification is skipped (no widget map) and the upload still runs."""

    verify_calls: list = []

    async def _tracking_graphql(client, base_url, headers, query, variables):
        verify_calls.append(variables)
        return {}, None

    monkeypatch.setattr(opencti_knowledge, "_graphql", _tracking_graphql)
    captured = _patch_upload(monkeypatch)

    dashboard_json = json.dumps(
        {
            "type": "dashboard",
            "openCTI_version": "6.0",
            "configuration": {
                "name": "Broken Manifest",
                # Not valid base64 — decoding raises binascii.Error (ValueError).
                "manifest": "not valid base64 %%%",
            },
        }
    )

    executor = _build_executor()
    result = await executor.execute_tool(
        ToolCall(
            id="d4",
            name="import_opencti_dashboard",
            arguments={"dashboard_json": dashboard_json},
        )
    )

    assert result.is_error is False
    assert "imported successfully" in result.content.lower()
    # Verification never ran (no widget map could be resolved) ...
    assert verify_calls == []
    # ... but the upload still happened (fail open, no crash).
    assert captured.get("file_bytes") is not None


# ── A dashboard export read from a file of the run ─────────────────────


@pytest.mark.asyncio
async def test_import_reads_the_dashboard_from_a_file_of_the_run(monkeypatch):
    """A file the run holds (attached, or downloaded from the XTM Hub) is
    imported like the same JSON passed inline, verification included."""
    from tests.integration_file_fakes import file_context_with

    _patch_verify(monkeypatch)
    captured = _patch_upload(monkeypatch)
    data = _raw_dashboard_json(_REAL_ID).encode("utf-8")
    ctx, file_id = file_context_with(b"\xef\xbb\xbf" + data, "tracker.json", "application/json")
    executor = _build_executor()
    executor.file_context = ctx

    result = await executor.execute_tool(
        ToolCall(id="f1", name="import_opencti_dashboard", arguments={"file_id": file_id})
    )

    assert result.is_error is False, result.content
    assert "imported successfully" in result.content.lower()
    uploaded = json.loads(captured["file_bytes"])
    assert uploaded["configuration"]["name"] == "Threat Dashboard"


@pytest.mark.asyncio
async def test_a_dashboard_file_is_verified_like_inline_json(monkeypatch):
    from tests.integration_file_fakes import file_context_with

    _patch_verify(monkeypatch)
    captured = _patch_upload(monkeypatch)
    ctx, file_id = file_context_with(
        _raw_dashboard_json(_FAKE_ID).encode("utf-8"), "hub.json", "application/json"
    )
    executor = _build_executor()
    executor.file_context = ctx

    result = await executor.execute_tool(
        ToolCall(id="f2", name="import_opencti_dashboard", arguments={"file_id": file_id})
    )

    assert "NOT imported" in result.content
    assert _FAKE_ID in result.content
    assert captured == {}


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({}, "Error: provide dashboard_json or the file_id of a dashboard export."),
        (
            {"dashboard_json": "{}", "file_id": "f-1"},
            "Error: provide only one configuration - dashboard_json or file_id, not both.",
        ),
    ],
    ids=["neither", "both"],
)
@pytest.mark.asyncio
async def test_exactly_one_configuration_is_imported(monkeypatch, arguments, expected):
    captured = _patch_upload(monkeypatch)
    result = await _build_executor().execute_tool(
        ToolCall(id="f3", name="import_opencti_dashboard", arguments=arguments)
    )
    assert result.content == expected
    assert captured == {}


@pytest.mark.asyncio
async def test_a_dashboard_file_that_is_not_utf8_is_refused(monkeypatch):
    from tests.integration_file_fakes import file_context_with

    captured = _patch_upload(monkeypatch)
    ctx, file_id = file_context_with(b"\xff\xfe\x00binary", "x.json", "application/json")
    executor = _build_executor()
    executor.file_context = ctx
    result = await executor.execute_tool(
        ToolCall(id="f4", name="import_opencti_dashboard", arguments={"file_id": file_id})
    )
    assert result.content == "Error: the dashboard file is not UTF-8 JSON."
    assert captured == {}
