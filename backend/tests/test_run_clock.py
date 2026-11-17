"""The live clock a run reads where its own clock goes stale (issue #8660).

Covers the stamp itself, the ``wait`` answer and the resume note that end with
it, and the Run context line naming the start time.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

from app.services.run_clock import clock_sentence, utc_stamp
from app.services.run_context import format_run_context
from app.services.turn_checkpoint import ResumedWork, resume_note
from app.tools.wait_policy import WAIT_REASON_ELAPSED, WaitBudget, _format_outcome

_T = datetime(2026, 11, 5, 5, 30, 12, tzinfo=UTC)
_NOW = re.compile(r"It is now (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) \(UTC\)\.")


def _parsed(text: str) -> datetime:
    match = _NOW.search(text)
    assert match is not None, text
    return datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def test_utc_stamp_is_iso_utc_to_the_second():
    assert utc_stamp(_T) == "2026-11-05T05:30:12Z"
    paris = _T.astimezone(timezone(timedelta(hours=1)))
    assert utc_stamp(paris) == "2026-11-05T05:30:12Z"
    assert utc_stamp(_T.replace(tzinfo=None)) == "2026-11-05T05:30:12Z"
    assert clock_sentence(_T) == "It is now 2026-11-05T05:30:12Z (UTC)."


def test_the_wait_answer_hands_back_the_live_clock():
    text = _format_outcome(
        requested=60,
        granted=60,
        elapsed=60,
        clamped_by=None,
        reason=WAIT_REASON_ELAPSED,
        task_ids=[],
        statuses={},
        until_note="",
        budget=WaitBudget(),
    )
    assert text.startswith("Waited ")
    assert abs((_parsed(text) - datetime.now(UTC)).total_seconds()) < 5


def test_the_resume_note_states_the_time_it_resumes_at():
    note = resume_note(ResumedWork(found=True, in_flight=[{"id": "b", "name": "t", "arguments": {}}]))
    assert note is not None
    assert abs((_parsed(note.content) - datetime.now(UTC)).total_seconds()) < 5
    assert note.content.isascii()


def test_the_run_context_names_the_start_time():
    block = format_run_context(run_id="r1", trigger_type="work_item", started_at=_T)
    assert "Started at 2026-11-05T05:30:12Z (UTC)" in block
    assert "It is now" in block
