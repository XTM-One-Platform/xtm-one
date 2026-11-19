"""A running fleet documentation generation is visible to the fleet tools (start, elapsed, stuck window)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.services.builtin_tools.fleets import generation_text
from app.services.fleet_doc_on_create import GENERATING_TTL_SECONDS, DocGeneration


def test_no_generation_says_nothing() -> None:
    assert generation_text(DocGeneration()) == ""
    assert generation_text(DocGeneration(failure="The model could not answer.")) == ""


def test_a_running_generation_names_its_start_and_elapsed_time() -> None:
    now = datetime(2026, 11, 19, 10, 5, 30, tzinfo=UTC)
    text = generation_text(DocGeneration(generating=True, started_at="2026-11-19T10:02:00+00:00"), now=now)
    assert "documentation_generating: true" in text
    assert "started 2026-11-19T10:02:00+00:00 UTC, running for 3 min 30 s" in text
    assert "No ETA" in text
    assert f"within {GENERATING_TTL_SECONDS // 60} min" in text
    assert "regenerate_fleet_documentation starts a new one" in text


def test_a_legacy_marker_reads_with_an_unknown_start() -> None:
    text = generation_text(DocGeneration(generating=True))
    assert "start time unknown" in text


def test_an_unparsable_start_is_shown_as_stored() -> None:
    text = generation_text(DocGeneration(generating=True, started_at="T-garbled"))
    assert "started T-garbled" in text
