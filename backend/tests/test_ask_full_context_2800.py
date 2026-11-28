# SPDX-License-Identifier: Apache-2.0

"""A gate ask context over the card's cap stays reachable (#2800).

The card keeps its 800-character cap; the whole context is kept on the ask as
``context_full``, the card shows it under "Full context", and the writer's
result says where the rest lives. A context that fits is stored as before.
"""

from __future__ import annotations

from typing import Any

from app.services import ctem_control_measurement, ctem_version_match
from app.services.decision_ask import (
    CUT_MARK,
    FULL_CONTEXT_FIELD,
    MAX_CONTEXT_CHARS,
    MAX_FULL_CONTEXT_CHARS,
    cut_warnings,
    full_context,
    lead_full_context,
    normalize_ask,
    with_full_context,
)

_EVIDENCE = " ".join(f"Host {n} runs nginx 1.24 and is exposed on port 443." for n in range(30))


def _stored(value: dict[str, Any]) -> dict[str, Any]:
    stored = normalize_ask(value)
    assert stored is not None
    return stored


def _long_ask() -> dict[str, Any]:
    assert 1100 < len(_EVIDENCE) <= MAX_FULL_CONTEXT_CHARS
    return {"question": "Close the case?", "context": _EVIDENCE}


def test_a_long_context_is_cut_on_the_card_and_kept_whole() -> None:
    stored = normalize_ask(_long_ask())
    assert stored is not None
    assert len(stored["context"]) <= MAX_CONTEXT_CHARS and stored["context"].endswith(CUT_MARK)
    assert stored[FULL_CONTEXT_FIELD] == _EVIDENCE
    assert full_context(stored) == _EVIDENCE


def test_a_short_context_is_stored_as_before() -> None:
    stored = normalize_ask({"question": "Close the case?", "context": "Two hosts patched."})
    assert stored == {"question": "Close the case?", "context": "Two hosts patched."}
    assert full_context(stored) == "Two hosts patched."


def test_a_stored_ask_read_again_keeps_its_whole_context() -> None:
    stored = normalize_ask(_long_ask())
    assert normalize_ask(stored) == stored


def test_the_write_warning_names_where_the_rest_lives() -> None:
    warnings = cut_warnings(_long_ask())
    assert len(warnings) == 1
    assert f"ask.{FULL_CONTEXT_FIELD}" in warnings[0] and "Full context" in warnings[0]
    assert str(MAX_CONTEXT_CHARS) in warnings[0]


def test_a_platform_line_never_costs_the_agent_its_evidence() -> None:
    stored = _stored(_long_ask())
    out = lead_full_context(stored, "Platform line.", full_context(stored), MAX_CONTEXT_CHARS)
    assert out["context"].startswith("Platform line.\n") and len(out["context"]) <= MAX_CONTEXT_CHARS
    assert out[FULL_CONTEXT_FIELD] == f"Platform line.\n{_EVIDENCE}"


def test_a_platform_line_on_a_short_context_adds_no_full_context() -> None:
    out = lead_full_context({"question": "Go?"}, "Platform line.", "Short.", MAX_CONTEXT_CHARS)
    assert out == {"question": "Go?", "context": "Platform line.\nShort."}


def test_with_full_context_drops_a_stale_copy_when_the_card_shows_it_whole() -> None:
    assert with_full_context({"context": "x", FULL_CONTEXT_FIELD: "old"}, "x") == {"context": "x"}


def test_the_control_measurement_line_keeps_the_agent_context_reachable() -> None:
    stored = _stored(_long_ask())
    out = ctem_control_measurement.complete_ask(stored, 30)
    assert len(out["context"]) <= MAX_CONTEXT_CHARS
    assert ctem_control_measurement.has_line(out)
    assert out[FULL_CONTEXT_FIELD].endswith(_EVIDENCE)


def test_the_version_match_line_keeps_the_agent_context_reachable() -> None:
    stored = _stored(_long_ask())
    match = {"running": "1.24.0", "affected": "< 1.25.3", "fixed": "1.25.3"}
    out = ctem_version_match.complete_ask(stored, match)
    assert len(out["context"]) <= MAX_CONTEXT_CHARS
    assert out[FULL_CONTEXT_FIELD].startswith(ctem_version_match.LINE_LEAD)
    assert out[FULL_CONTEXT_FIELD].endswith(_EVIDENCE)
