"""A hostname, URL or email on a decision card is not read as an id (#2254)."""

from __future__ import annotations

import pytest

from app.services.decision_ask import normalize_ask, unreadable_reasons

_HEX = "a hexadecimal id or SHA: name the thing in words, ids go in blocked_reason"


@pytest.mark.parametrize(
    "text",
    [
        "Run one scan of langfuse-141a8160.one.example.com?",
        "Open https://pr-12-ab12cd34.preview.example.com/path?",
        "Write to ops-1a2b3c4d@alerts.example.com about the host?",
        "See https://example.com/issues/12345678901 before approving?",
    ],
)
def test_an_address_with_a_hex_or_numeric_label_is_readable(text: str) -> None:
    assert unreadable_reasons(text) == []


def test_a_bare_sha_is_still_refused() -> None:
    assert unreadable_reasons("Revert commit 3f118ce7?") == [_HEX]


def test_a_uuid_is_still_refused() -> None:
    reasons = unreadable_reasons("Item 8a1f0c2e-1b2c-4d5e-8f90-123456789abc?")
    assert any(reason.startswith("a UUID") for reason in reasons)


def test_a_bare_sha_next_to_a_hostname_is_refused_once() -> None:
    assert unreadable_reasons("Deploy 3f118ce7 to app-ab12cd34.example.com?") == [_HEX]


def test_the_ask_with_a_hex_hostname_is_accepted() -> None:
    ask = normalize_ask(
        {
            "question": "Run one vulnerability scan of langfuse-141a8160.one.example.com to measure it for the first time?",
            "context": "The host was found by the last enumeration and was never scanned.",
            "options": [
                {"key": "A", "label": "Scan it now"},
                {"key": "B", "label": "Do nothing"},
            ],
            "recommendation": "A",
        }
    )
    assert ask is not None
    assert "langfuse-141a8160.one.example.com" in ask["question"]
