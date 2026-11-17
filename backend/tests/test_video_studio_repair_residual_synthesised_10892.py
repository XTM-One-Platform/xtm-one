"""A repair never says "no residual" on a transition the save check still refuses as synthesised (#10892).

A redraw from keyframes draws no frame its page did not paint: a hover tooltip painted at 12.9 fps stays 78%
synthesised at 60 fps once repaired, so the save check's ``synthesised_transition`` still finds it. The repair's
residual names that span, in the save check's words, as one to record again.
"""

from __future__ import annotations

from app.services.video_studio import take_repair
from app.services.video_studio.scroll_pacing import synthesised_transitions, synthesised_words


def _tooltip_take(painted: float) -> list[dict]:
    return [
        {
            "step": 1,
            "action": "hover",
            "motion": [{"paced": True, "fps": 60, "painted": painted, "t": [3.17, 3.33], "element": "tooltip"}],
        }
    ]


def test_a_repaired_tooltip_painted_at_12_9_fps_is_a_residual_in_the_lint_s_words() -> None:
    events = _tooltip_take(12.9)
    found = synthesised_transitions(events, fps=60)
    assert len(found) == 1
    assert found[0]["t"] == [3.17, 3.33]
    assert 0.78 <= found[0]["share"] <= 0.79

    left = take_repair.residual(take_repair.FROM_KEYFRAMES, recorded={}, notes=[], events=events, fps=60)

    assert len(left) == 1
    assert synthesised_words(found) in left[0]
    assert "record it again" in left[0]
    said = take_repair.repair_words(
        {take_repair.REPAIRED_KEY: {"of": "x", "from": take_repair.FROM_KEYFRAMES, "spans": [], "residual": left}}
    )
    assert "No residual" not in said
    assert "Residual, record it again for these: step 1 at 3.17-3.33 s" in said


def test_a_transition_painted_near_the_take_s_rate_leaves_no_residual() -> None:
    events = _tooltip_take(58.0)
    assert synthesised_transitions(events, fps=60) == []
    assert take_repair.residual(take_repair.FROM_KEYFRAMES, recorded={}, notes=[], events=events, fps=60) == []


def test_without_events_the_residual_is_unchanged() -> None:
    assert take_repair.residual(take_repair.FROM_KEYFRAMES, recorded={}, notes=[]) == []
    assert take_repair.residual(take_repair.FROM_KEYFRAMES, recorded={}, notes=["a note"]) == ["a note"]
