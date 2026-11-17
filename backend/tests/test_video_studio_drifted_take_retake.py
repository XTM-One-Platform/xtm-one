"""Issue #11428 point 3: a narration take that said another scene's sentence is spoken again once before the render fails.

``audio_qc.repeated_sentences`` marks a hearing in a scene that does not write the sentence as ``drifted``: that scene's
take said another scene's text. The render speaks that take again once (``render._retake_drifted``), mixes and hears
its sound again (``check_render(remix=...)``); a sentence written once and said twice in its own scene is a real
repeat, never retaken, and still blocks.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services.video_studio import audio_qc, frame_freeze, render, voice, voice_check
from app.services.video_studio.render_checks import FAIL, PASSED, REPEATED_SENTENCE, Finding


def _heard(text: str, start: float, step: float = 0.3) -> list[tuple[str, float, float]]:
    return [(word, start + index * step, start + (index + 1) * step) for index, word in enumerate(text.split())]


NARRATIONS = [
    audio_qc.Narration("a", "Product X proves your defenses.", 0.0, 3.0),
    audio_qc.Narration("b", "Product X scores your defenses against real attacks.", 60.0, 64.0),
]


def _repeat(scene: str, *, drifted: bool) -> Finding:
    return Finding(REPEATED_SENTENCE, 60.0, 62.0, scene, "heard twice", FAIL, {"drifted": drifted})


def test_a_take_that_said_another_scene_s_sentence_is_marked_drifted():
    heard = _heard("product x proves your defenses", 0.0) + _heard("product x proves your defenses", 60.0)
    [finding] = audio_qc.repeated_sentences(heard, NARRATIONS)
    assert finding.data["drifted"] is True and audio_qc.drifted_takes([finding]) == ["b"]


def test_a_sentence_its_own_scene_says_twice_is_never_a_drifted_take():
    narrations = [audio_qc.Narration("s1", "We hunt threats every day. Then we report.", 0.0, 6.0)]
    heard = (
        _heard("we hunt threats every day", 0.0)
        + _heard("we hunt threats every day", 2.0)
        + _heard("then we report", 4.0)
    )
    [finding] = audio_qc.repeated_sentences(heard, narrations)
    assert finding.data["drifted"] is False and audio_qc.drifted_takes([finding]) == []


def test_drifted_takes_names_each_scene_once_and_skips_warnings():
    warned = Finding(REPEATED_SENTENCE, 1.0, 2.0, "c", "heard twice", "warn", {"drifted": True})
    found = [_repeat("b", drifted=True), _repeat("b", drifted=True), warned, _repeat("a", drifted=False)]
    assert audio_qc.drifted_takes(found) == ["b"]


def _media(tmp_path) -> render._Media:
    media = render._Media()
    media.speaker = object()
    media.lines = [voice.Line("a", NARRATIONS[0].text, None, None), voice.Line("b", NARRATIONS[1].text, None, None)]
    for scene in ("a", "b"):
        path = str(tmp_path / f"voice-{scene}.wav")
        media.spoken.append(voice_check.SpokenSegment(scene, path, 3.0, voice_check.PASSED))
        media.voice_paths[scene] = path
    return media


def _speaks(monkeypatch, tmp_path, *, status: str = voice_check.PASSED, duration: float = 2.5) -> list[str]:
    said: list[str] = []

    async def speak_checked(_speaker, line, *, workdir: str, fresh: bool = False):
        assert fresh
        said.append(line.scene_id)
        return voice_check.SpokenSegment(
            line.scene_id, f"{workdir}/retaken.wav", duration, status, reason="heard wrong"
        )

    monkeypatch.setattr(voice_check, "speak_checked", speak_checked)
    return said


def test_a_drifted_take_is_spoken_again_once_and_placed(monkeypatch, tmp_path):
    media = _media(tmp_path)
    said = _speaks(monkeypatch, tmp_path)
    retaken = asyncio.run(render._retake_drifted(media, [_repeat("b", drifted=True)], workdir=tmp_path))
    assert retaken == ["b"] and said == ["b"]
    assert media.voice_paths["b"].endswith("retake-b/retaken.wav") and media.voice_paths["a"].endswith("voice-a.wav")
    assert [s.scene_id for s in media.spoken] == ["a", "b"] and media.spoken[1].path == media.voice_paths["b"]
    assert any("spoken again once" in note for note in media.notes)


@pytest.mark.parametrize(("status", "duration", "why"), [("failed", 2.5, "did not pass"), (PASSED, 3.5, "past the")])
def test_a_retake_that_fails_or_overruns_its_time_is_not_placed(monkeypatch, tmp_path, status, duration, why):
    media = _media(tmp_path)
    _speaks(monkeypatch, tmp_path, status=status, duration=duration)
    before = dict(media.voice_paths)
    assert asyncio.run(render._retake_drifted(media, [_repeat("b", drifted=True)], workdir=tmp_path)) == []
    assert media.voice_paths == before and any(why in note for note in media.notes)


def test_a_real_repeat_is_never_spoken_again(monkeypatch, tmp_path):
    media = _media(tmp_path)
    said = _speaks(monkeypatch, tmp_path)
    assert asyncio.run(render._retake_drifted(media, [_repeat("a", drifted=False)], workdir=tmp_path)) == []
    assert said == []


def _check(monkeypatch, tmp_path, heard_findings: list[list[Finding]]):
    media = _media(tmp_path)
    said = _speaks(monkeypatch, tmp_path)
    hearings = iter(heard_findings)

    async def check_audio(**_kwargs):
        return audio_qc.AudioCheck(PASSED, list(next(hearings)))

    async def watched(*_args, **_kwargs):
        return SimpleNamespace(as_summary=lambda: {"status": PASSED}, findings=[])

    monkeypatch.setattr(audio_qc, "check_audio", check_audio)
    monkeypatch.setattr(frame_freeze, "check_frames", watched)
    remixed: list[bool] = []

    async def remix():
        remixed.append(True)
        return None

    plan = SimpleNamespace(narration_starts={"a": 0.0, "b": 60.0}, document={"scenes": [{"id": "a", "start": 0.0}]})
    checks = asyncio.run(
        render.check_render(
            final_path=str(tmp_path / "render.mp4"),
            stem=None,
            plan=plan,
            media=media,
            size=(1280, 720),
            fps=30,
            workdir=tmp_path,
            remix=remix,
        )
    )
    return checks, said, remixed


def test_a_drifted_take_is_retaken_mixed_and_heard_again_then_passes(monkeypatch, tmp_path):
    checks, said, remixed = _check(monkeypatch, tmp_path, [[_repeat("b", drifted=True)], []])
    assert said == ["b"] and remixed == [True]
    assert [f.kind for f in checks.findings] == [] and not checks.failed


def test_a_real_written_repeat_still_fails_with_no_retake(monkeypatch, tmp_path):
    checks, said, remixed = _check(monkeypatch, tmp_path, [[_repeat("a", drifted=False)]])
    assert said == [] and remixed == []
    assert [f.kind for f in checks.findings] == [REPEATED_SENTENCE] and checks.failed


def test_a_take_that_drifts_again_is_retaken_only_once_and_fails(monkeypatch, tmp_path):
    checks, said, remixed = _check(monkeypatch, tmp_path, [[_repeat("b", drifted=True)], [_repeat("b", drifted=True)]])
    assert said == ["b"] and remixed == [True]
    assert [f.kind for f in checks.findings] == [REPEATED_SENTENCE] and checks.failed
