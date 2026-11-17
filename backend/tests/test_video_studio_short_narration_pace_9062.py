"""Issue #9062: a short narration on a short scene always has a speed that clears the subtitle and the pace checks.

A 5-word line (about 44 characters) on a 2.7 s recording trim: the render's mix check flagged it ``pace_slow`` at
speed 1.0, and every faster speed made the save's ``caption_speed`` refuse it as ``dense`` - judged on the reading
pace, the take at the new speed not spoken yet. Now a narration under ``PACE_MIN_WORDS`` is never read slow, a subtitle
speed judged on the reading pace is an advisory the gate does not refuse on, and a narration whose words leave no
length for both checks says so with the fix that is not its speed.
"""

from __future__ import annotations

import pytest

from app.services.video_studio import mix_check, quality, render_checks

SHORT_44 = "Configure the ingestion connector instantly"  # 5 words, 43 characters


def _document(cues: list[list]) -> dict:
    scenes = [{"id": "rec", "type": "recording", "start": 0.0, "end": 2.7, "overlays": []}]
    return {"width": 1920, "height": 1080, "scenes": scenes, "captions": {"enabled": True, "cues": cues}}


def _pace(words: int, seconds: float, speed: float) -> mix_check.ScenePace:
    return mix_check.ScenePace("rec", words, seconds, speed, (0.2, 0.2 + seconds))


def _mix(paces: list[mix_check.ScenePace]) -> mix_check.MixReport:
    blocks = [(round(0.4 + k * 0.1, 3), -16.0) for k in range(int((2.7 - 0.4) / 0.1) + 1)]
    return mix_check.analyse(blocks, duration=2.7, scenes=[("rec", 0.0, 2.7)], paces=paces)


def test_the_thresholds_of_both_checks_agree():
    assert quality.CAPTION_SLOW_WPM == mix_check.SLOW_WPM
    assert quality.CAPTION_PACE_MIN_WORDS == mix_check.PACE_MIN_WORDS
    assert mix_check.count_words(SHORT_44) == 5 and len(SHORT_44) == 43


@pytest.mark.parametrize("speed", [1.0, 1.1, 1.25])
def test_a_five_word_line_is_never_read_slow(speed: float):
    # 5 words over 3.4 s / speed of voice: 88 wpm at 1.0, 97 at 1.1, 110 at 1.25.
    report = _mix([_pace(5, round(3.4 / speed, 2), speed)])
    assert [p.code for p in report.problems] == []
    [row] = report.scenes
    assert row["words"] == 5 and row["wpm"] is not None


def test_a_normal_narration_is_still_read_slow_and_a_short_one_still_fast():
    assert [p.code for p in _mix([_pace(12, 8.0, 1.0)]).problems] == ["pace_slow"]
    assert [p.code for p in _mix([_pace(5, 1.4, 1.25)]).problems] == ["pace_fast"]


@pytest.mark.parametrize("speed", [1.0, 1.1, 1.25])
def test_subtitles_planned_at_the_reading_pace_are_an_advisory_the_gate_does_not_refuse(speed: float):
    # The reading pace (2.6 words a second, 0.3 s added) spreads the line over 5 / (2.6 * speed) + 0.3 s.
    span = 5 / (2.6 * speed) + 0.3
    found = quality.document_findings(_document([[0.2, 0.2 + span, SHORT_44]]), None, estimated={"rec"})
    if speed == 1.0:  # 43 characters over 2.2 s: 19 a second, under the limit
        assert found == []
        return
    [finding] = found
    assert finding.kind == "caption_speed" and finding.severity == "advisory"
    assert finding.severity not in render_checks.BLOCKING_SEVERITIES
    assert "at the reading pace, its voice not spoken yet" in finding.message
    assert "narration_speed" in finding.message


def test_subtitles_of_the_spoken_voice_are_judged_as_before():
    [finding] = quality.document_findings(_document([[0.2, 2.2, SHORT_44]]), None)
    assert finding.severity == "dense" and "22 characters a second" in finding.message
    assert "shorten its narration or slow the voice (narration_speed, voiceover.speed)" in finding.message
    # At the speed the take was spoken at, 1.0: 43 characters over 3.4 s, 13 a second - nothing to say.
    assert quality.document_findings(_document([[0.2, 3.6, SHORT_44]]), None) == []


def test_a_narration_with_no_length_for_both_checks_names_the_conflict_and_the_fix():
    # 6 long words, 95 characters: readable from 4.75 s on, slow past 3.27 s - no speed clears both.
    line = "Interoperability internationalization authentication administration orchestration documentation"
    assert mix_check.count_words(line) == 6 and len(line) == 95
    [finding] = quality.document_findings(_document([[0.0, 2.5, line]]), None)
    assert "need 4.8 s on screen to read, but read over more than 3.3 s fall under 110 words" in finding.message
    assert finding.severity == "dense"
    assert "no speed clears both its subtitles and its pace" in finding.message
    assert "reword it with shorter words, or more of them" in finding.message
    assert "narration_speed" not in finding.message


def test_estimated_narrations_reads_the_preview():
    preview = {
        "voice": {"state": "on"},
        "narration": [
            {"scene_id": "a", "state": "ready"},
            {"scene_id": "b", "state": "none"},
            {"scene_id": "c", "state": "speaking"},
        ],
    }
    assert quality.estimated_narrations(preview) == {"b", "c"}
    assert quality.estimated_narrations({**preview, "voice": {"state": "off"}}) == frozenset()
    assert quality.estimated_narrations({"voice": None, "narration": []}) == frozenset()


def test_the_report_version_is_bumped():
    assert quality.QUALITY_REPORT_VERSION >= 51
