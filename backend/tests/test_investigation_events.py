"""Wire format for a live investigation: frames and the marker.

Two properties are load-bearing here and neither is visible at a glance:

* **Frames only ever describe committed state**, and carry the run's
  ``revision``, so a client that missed frames rebuilds the block from the
  record and a duplicate/out-of-order frame is discardable rather than
  corrupting.
* **The marker has one shape across three consumers** (web renderer, channel
  stripper, backend emitter). A divergence leaks raw ``[[INVESTIGATION:…]]``
  text to users, which is exactly what the parity test below prevents.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.models.investigation import Investigation, InvestigationStep
from app.services.investigation_events import (
    INVESTIGATION_FRAME_STATUSES,
    INVESTIGATION_MARKER_RE,
    build_investigation_marker,
    complete_frame,
    planned_frame,
    step_frame,
)
from app.services.investigation_state import summarize

FRONTEND_ROOT = Path(__file__).resolve().parents[2] / "frontend"


def _run(**kwargs) -> Investigation:
    return Investigation(
        id=kwargs.pop("id", uuid.uuid4()),
        subject_type=kwargs.pop("subject_type", "ip"),
        subject_value=kwargs.pop("subject_value", "8.8.8.8"),
        source_pack_slugs=kwargs.pop("source_pack_slugs", ["web-general"]),
        status=kwargs.pop("status", "running"),
        revision=kwargs.pop("revision", 3),
        iterations_used=kwargs.pop("iterations_used", 1),
        max_iterations=kwargs.pop("max_iterations", 10),
        **kwargs,
    )


def _step(**kwargs) -> InvestigationStep:
    return InvestigationStep(
        id=kwargs.pop("id", uuid.uuid4()),
        position=kwargs.pop("position", 0),
        source_name=kwargs.pop("source_name", "RDAP registration"),
        status=kwargs.pop("status", "pending"),
        findings_count=kwargs.pop("findings_count", 0),
        evidence=kwargs.pop("evidence", []),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The marker
# ---------------------------------------------------------------------------


def test_the_marker_builder_produces_a_matchable_marker():
    run_id = uuid.uuid4()
    marker = build_investigation_marker(run_id)
    match = INVESTIGATION_MARKER_RE.search(marker)
    assert match
    assert match.group(1) == str(run_id)


def test_a_marker_echoed_back_in_mixed_case_still_matches():
    """An LLM re-emitting the marker uppercased must not leak visible text."""
    run_id = uuid.uuid4()
    shouted = build_investigation_marker(str(run_id).upper()).replace("INVESTIGATION", "Investigation")
    assert INVESTIGATION_MARKER_RE.search(shouted)


def test_the_marker_shape_matches_the_channel_and_frontend_regexes():
    """One marker shape, three consumers: emitter, channel stripper, renderer.

    A divergence leaks raw ``[[INVESTIGATION:…]]`` text to users.
    """
    from app.channels.base import _INVESTIGATION_MARKER_RE

    marker = build_investigation_marker(uuid.uuid4())
    assert _INVESTIGATION_MARKER_RE.search(marker)

    # The web app reads markers with the chat panel's grammar, tested on the
    # same samples (frontend/src/pages/chat/answer-markers.contract.json).
    contract = json.loads((FRONTEND_ROOT / "src" / "pages" / "chat" / "answer-markers.contract.json").read_text())["markers"]
    samples = [m for m in contract if m["kind"] == "INVESTIGATION"]
    assert samples
    for sample in samples:
        match = _INVESTIGATION_MARKER_RE.fullmatch(sample["marker"])
        assert match, sample["marker"]
        assert match.group(1).lower() == sample["id"]


def test_the_channel_stripper_removes_the_marker_and_leaves_the_prose():
    """External channels have no viewer route to link — see the helper's docstring."""
    from app.channels.base import strip_channel_investigation_markers

    marker = build_investigation_marker(uuid.uuid4())
    text = f"Here is what I found.\n\n{marker}\n\nNothing conclusive."
    stripped = strip_channel_investigation_markers(text)
    assert "INVESTIGATION" not in stripped.upper()
    assert "Here is what I found." in stripped
    assert "Nothing conclusive." in stripped


def test_the_channel_stripper_is_a_no_op_without_a_marker():
    from app.channels.base import strip_channel_investigation_markers

    text = "No markers here."
    assert strip_channel_investigation_markers(text) is text
    assert strip_channel_investigation_markers("") == ""


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------


def test_frame_statuses_are_the_declared_vocabulary():
    run = _run()
    steps = [_step()]
    statuses = {
        planned_frame(run, steps)["status"],
        step_frame(run, steps[0])["status"],
        complete_frame(run, summarize(steps))["status"],
    }
    assert statuses == set(INVESTIGATION_FRAME_STATUSES)


def test_the_planned_frame_carries_the_whole_skeleton_in_one_frame():
    """Applying the plan step-by-step would animate a list assembling itself."""
    run = _run()
    steps = [_step(position=i, source_name=f"S{i}") for i in range(3)]
    frame = planned_frame(run, steps)
    assert [s["position"] for s in frame["steps"]] == [0, 1, 2]
    assert all(s["step_status"] == "pending" for s in frame["steps"])
    assert frame["subject_type"] == "ip"
    assert frame["subject_value"] == "8.8.8.8"
    assert frame["source_pack_slugs"] == ["web-general"]
    assert frame["max_iterations"] == 10


@pytest.mark.parametrize(
    "builder",
    [
        lambda r, s: planned_frame(r, [s]),
        lambda r, s: step_frame(r, s),
        lambda r, s: complete_frame(r, summarize([s])),
    ],
)
def test_every_frame_carries_the_run_id_and_revision(builder):
    """The client discards a frame older than the revision it already holds."""
    run = _run(revision=7)
    frame = builder(run, _step())
    assert frame["investigation_id"] == str(run.id)
    assert frame["revision"] == 7


def test_a_step_frame_is_json_safe():
    """UUIDs must reach the wire as strings, not objects."""
    import json

    run = _run()
    parent = _step()
    step = _step(
        position=1,
        status="completed",
        findings_count=2,
        evidence=[{"kind": "url", "label": "https://x.test/a", "href": "https://x.test/a"}],
        lead_from_step_id=parent.id,
        iteration=2,
    )
    frame = step_frame(run, step)
    assert isinstance(frame["step_id"], str)
    assert frame["lead_from_step_id"] == str(parent.id)
    json.dumps(frame)


def test_an_absent_confidence_is_sent_as_null_not_omitted():
    """The client must tell "no confidence" from "not sent yet"."""
    frame = step_frame(_run(), _step(status="empty"))
    assert "confidence" in frame
    assert frame["confidence"] is None


def test_a_failed_step_frame_carries_its_error_type_and_detail():
    step = _step(status="error", error_type="rate_limited", status_detail="HTTP 429")
    frame = step_frame(_run(), step)
    # The frame's `status` names the frame kind; the step's own state lives in
    # `step_status`. Collapsing the two would make `frame.status` unreadable.
    assert frame["status"] == "investigation_step"
    assert frame["step_status"] == "error"
    assert frame["error_type"] == "rate_limited"
    assert frame["status_detail"] == "HTTP 429"


def test_the_complete_frame_reports_measured_duration_only():
    """Never derived from the step count — the invariant on the wire."""
    run = _run(status="completed")
    run.started_at = datetime(2026, 9, 3, 10, 0, 0, tzinfo=UTC)
    run.completed_at = run.started_at + timedelta(seconds=247)
    frame = complete_frame(run, summarize([_step(status="completed")]))
    assert frame["duration_seconds"] == pytest.approx(247.0)


def test_an_unfinished_run_reports_no_duration():
    frame = complete_frame(_run(), summarize([]))
    assert frame["duration_seconds"] is None


def test_the_complete_frame_carries_all_succeeded_rather_than_leaving_it_client_side():
    """One server-side rule gates the "everything passed" affordance."""
    ok = complete_frame(_run(), summarize([_step(status="completed", findings_count=1)]))
    assert ok["summary"]["all_succeeded"] is True

    mixed = complete_frame(
        _run(),
        summarize(
            [
                _step(status="completed", findings_count=1),
                _step(position=1, status="error", error_type="timeout"),
            ]
        ),
    )
    assert mixed["summary"]["all_succeeded"] is False
    assert mixed["summary"]["failed"] == 1
    assert mixed["summary"]["by_status"]["error"] == 1


def test_the_complete_frame_reports_why_the_run_ended():
    run = _run(status="completed")
    run.end_reason = "iteration budget spent (2)"
    frame = complete_frame(run, summarize([]))
    assert frame["run_status"] == "completed"
    assert frame["end_reason"] == "iteration budget spent (2)"
    assert frame["iterations_used"] == 1
    assert frame["max_iterations"] == 10
