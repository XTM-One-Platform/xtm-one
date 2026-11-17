"""Regression tests for ``build_tool_overrides_payload`` (chat_saved_tool_selection.py).

The reopen merge in the chat frontend distinguishes a *missing* baseline
(``default_tool_ids`` is ``null`` -> fall back to the saved enabled set) from an
*explicit empty* baseline (``[]`` -> authoritative "no defaults"). If the persist
path coerced an omitted baseline to ``[]``, reopening a conversation would treat
every current default as newly added and re-enable tools the user had explicitly
disabled. These tests pin the ``None`` vs ``[]`` contract and the round-trip
through ``json`` + the ``ToolOverrides`` schema.
"""

from __future__ import annotations

import json

from app.api.v1.chat import ToolOverrides
from app.services.chat_saved_tool_selection import build_tool_overrides_payload


def test_omitted_baseline_is_preserved_as_none_not_empty_list():
    """An omitted ``default_tool_ids`` must stay ``None`` (JSON ``null``), never
    be coerced to ``[]`` — that is the bug the reviewer flagged."""
    payload = build_tool_overrides_payload(
        builtin_tool_ids=["b1"],
        custom_tool_ids=[],
        integration_ids=["i1"],
        mcp_server_ids=[],
        knowledge_base_ids=[],
        default_tool_ids=None,
    )
    assert payload is not None
    assert payload["default_tool_ids"] is None
    # The tool-selection arrays are still normalised to lists.
    assert payload["builtin_tool_ids"] == ["b1"]
    assert payload["integration_ids"] == ["i1"]
    assert payload["custom_tool_ids"] == []


def test_explicit_empty_baseline_is_distinct_from_missing():
    """An explicit empty baseline is preserved as ``[]`` and is NOT collapsed
    into the missing-baseline (``None``) case."""
    payload = build_tool_overrides_payload(
        builtin_tool_ids=["b1"],
        custom_tool_ids=None,
        integration_ids=None,
        mcp_server_ids=None,
        knowledge_base_ids=None,
        default_tool_ids=[],
    )
    assert payload is not None
    assert payload["default_tool_ids"] == []


def test_non_empty_baseline_is_passed_through():
    payload = build_tool_overrides_payload(
        builtin_tool_ids=["b1"],
        custom_tool_ids=["c1"],
        integration_ids=["i1"],
        mcp_server_ids=["m1"],
        knowledge_base_ids=["k1"],
        default_tool_ids=["b1", "c1", "i1", "m1", "k1"],
    )
    assert payload is not None
    assert payload["default_tool_ids"] == ["b1", "c1", "i1", "m1", "k1"]


def test_returns_none_when_no_fields_supplied():
    """No tool-selection field at all -> caller skips the write entirely."""
    assert (
        build_tool_overrides_payload(
            builtin_tool_ids=None,
            custom_tool_ids=None,
            integration_ids=None,
            mcp_server_ids=None,
            knowledge_base_ids=None,
            default_tool_ids=None,
        )
        is None
    )


def test_all_empty_lists_persist_an_explicit_empty_selection():
    """A user who disabled every tool sends all-empty arrays (and, for older
    clients, no baseline). That is an *explicit empty selection*, not "no fields
    provided" - it must be persisted (not collapsed to a skipped write) so reopen
    honours the disable instead of falling back to the agent defaults. This is
    the contract the WS parse relies on by preserving ``[]`` instead of coercing
    it to ``None`` (matching the REST path)."""
    payload = build_tool_overrides_payload(
        builtin_tool_ids=[],
        custom_tool_ids=[],
        integration_ids=[],
        mcp_server_ids=[],
        knowledge_base_ids=[],
        default_tool_ids=None,
    )
    assert payload is not None
    assert payload["builtin_tool_ids"] == []
    assert payload["integration_ids"] == []
    assert payload["default_tool_ids"] is None


def test_baseline_only_payload_is_a_noop():
    """A bare baseline (``default_tool_ids`` only, no tool-selection arrays) is
    metadata, not a selection — it must NOT trigger a write. Persisting it would
    normalize every category to ``[]`` and disable all tools on reopen, clobbering
    any previously saved overrides. The real client always sends the selection
    arrays alongside the baseline, so this only guards the degenerate case."""
    assert (
        build_tool_overrides_payload(
            builtin_tool_ids=None,
            custom_tool_ids=None,
            integration_ids=None,
            mcp_server_ids=None,
            knowledge_base_ids=None,
            default_tool_ids=["b1"],
        )
        is None
    )


def test_json_roundtrip_preserves_null_vs_empty_via_tool_overrides_schema():
    """The persisted JSON round-trips through ``ToolOverrides`` keeping the
    ``null`` -> ``None`` and ``[]`` -> ``[]`` distinction the frontend relies on."""
    missing = build_tool_overrides_payload(
        builtin_tool_ids=["b1"],
        custom_tool_ids=[],
        integration_ids=[],
        mcp_server_ids=[],
        knowledge_base_ids=[],
        default_tool_ids=None,
    )
    assert missing is not None
    restored_missing = ToolOverrides(**json.loads(json.dumps(missing)))
    assert restored_missing.default_tool_ids is None

    empty = build_tool_overrides_payload(
        builtin_tool_ids=["b1"],
        custom_tool_ids=[],
        integration_ids=[],
        mcp_server_ids=[],
        knowledge_base_ids=[],
        default_tool_ids=[],
    )
    assert empty is not None
    restored_empty = ToolOverrides(**json.loads(json.dumps(empty)))
    assert restored_empty.default_tool_ids == []
