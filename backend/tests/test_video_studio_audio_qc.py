"""What a render finally sounds like, heard back. Synthetic audio only.

* the stutter detector: a sine burst repeated is flagged with its time and
  its period (on a hop and off it), a single burst, a held tone and
  speech-like noise are not; a known duplicated segment of speech is;
* a stutter of the mix counts when the mix repeats what the voice had said,
  never for a music loop under the voice;
* the voice-over transcribed: the WER over every narration, a finding per
  scene heard wrong at its narration's time; a sentence heard twice;
* where the stem is transcribed: each narration's span where its voice has
  energy, long silences left out, and only the narrations whose words were
  not heard back already;
* the whole check on decoded audio, its times in the MP4's own seconds.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.llm.transcription import TranscribedWord, Transcript
from app.services.video_studio import audio_qc, encoder, voice_check
from app.services.video_studio.render_checks import FAIL, REPEATED_SENTENCE, STUTTER, TRANSCRIPT
from app.services.video_studio.speech_timing import voiced_runs

RATE = audio_qc.ANALYSIS_RATE


def _speech_like(seconds: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = round(seconds * RATE)
    spectrum = np.fft.rfft(rng.standard_normal(n))
    freqs = np.fft.rfftfreq(n, 1.0 / RATE)
    spectrum[(freqs < 150) | (freqs > 4000)] = 0
    # Formant-like peaks that move: the spectrum changes every few tens of ms, as speech does.
    band = np.fft.irfft(spectrum, n)
    t = np.arange(n) / RATE
    envelope = 0.5 + 0.5 * np.abs(np.sin(2 * np.pi * 3.7 * t + rng.uniform(0, 3)))
    signal = band * envelope
    return (0.3 * signal / np.max(np.abs(signal))).astype(np.float32)


def _burst(seconds: float = 0.09, gap: float = 0.035) -> np.ndarray:
    """A chirp under a Hann window, then a short near-silence: one chunk of a stutter."""
    t = np.arange(round(seconds * RATE)) / RATE
    chirp = np.sin(2 * np.pi * (500 * t + 4000 * t * t)) * np.hanning(len(t))
    tail = 0.002 * np.random.default_rng(9).standard_normal(round(gap * RATE))
    return (0.5 * np.concatenate([chirp, tail])).astype(np.float32)


def _with(chunk: np.ndarray, times: int) -> np.ndarray:
    return np.concatenate([_speech_like(1.0, 1), np.tile(chunk, times), _speech_like(1.0, 2)])


def test_a_sine_burst_repeated_is_a_stutter_with_its_time_and_period():
    chunk = _burst()  # 125 ms: on the analysis hop
    found = audio_qc.find_stutters(_with(chunk, 4))
    assert len(found) == 1
    stutter = found[0]
    assert stutter.start == pytest.approx(1.0, abs=0.02) and stutter.times >= 3
    assert stutter.period == pytest.approx(len(chunk) / RATE, abs=0.001) and stutter.correlation > 0.95


def test_a_period_off_the_analysis_hop_is_found_too():
    chunk = _burst(0.093, 0.04)  # 133 ms, not a multiple of the 5 ms hop
    found = audio_qc.find_stutters(_with(chunk, 3))
    assert len(found) == 1 and found[0].period == pytest.approx(len(chunk) / RATE, abs=0.0005)


def test_a_single_burst_a_held_tone_and_speech_are_not_stutters():
    assert audio_qc.find_stutters(_with(_burst(), 1)) == []
    t = np.arange(2 * RATE) / RATE
    held = (0.4 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    assert audio_qc.find_stutters(np.concatenate([_speech_like(0.5, 3), held, _speech_like(0.5, 4)])) == []
    assert audio_qc.find_stutters(_speech_like(6.0, 5)) == []
    # Twice in a row is not a stutter: three times is.
    assert audio_qc.find_stutters(_with(_burst(), 2)) == []


def test_a_known_duplicated_segment_of_speech_is_found():
    speech = _speech_like(3.0, 6)
    chunk = speech[round(1.0 * RATE) : round(1.15 * RATE)]
    doubled = np.concatenate([speech[: round(1.15 * RATE)], chunk, chunk, speech[round(1.15 * RATE) :]])
    found = audio_qc.find_stutters(doubled, voiced=voiced_runs(doubled, RATE))
    assert len(found) == 1 and found[0].start == pytest.approx(1.0, abs=0.03) and found[0].times == 3


def test_a_stutter_outside_a_voiced_region_is_not_the_voices():
    signal = _with(_burst(), 4)
    assert audio_qc.find_stutters(signal, voiced=[(0.0, 0.8), (2.5, 3.5)]) == []


def test_a_stutter_of_the_mix_counts_when_it_repeats_what_the_voice_said():
    voice = _speech_like(3.0, 7)
    chunk = voice[round(1.0 * RATE) : round(1.12 * RATE)]
    # The mix repeats the voice's chunk (a mux or mix fault): the voice itself does not.
    mix = np.concatenate([voice[: round(1.12 * RATE)], chunk, chunk, voice[round(1.12 * RATE) : round(2.76 * RATE)]])
    mix = mix[: len(voice)]
    mix_levels, voice_levels = audio_qc.band_levels(mix), audio_qc.band_levels(voice)
    found = audio_qc.find_stutters(mix, levels=mix_levels)
    assert found and audio_qc.repeats_the_voice(found[0], mix_levels, voice_levels)
    # A music loop under the voice: the mix follows the voice, it never repeats what was said.
    burst = _burst()
    loop = np.tile(burst, -(-len(voice) // len(burst)))[: len(voice)] * 0.2
    looped = voice + loop
    looped_levels = audio_qc.band_levels(looped)
    assert all(not audio_qc.repeats_the_voice(s, looped_levels, voice_levels) for s in audio_qc.find_stutters(looped, levels=looped_levels))


# ── The voice-over transcribed ────────────────────────────────────────────


def _heard(text: str, start: float, step: float = 0.3) -> list[tuple[str, float, float]]:
    return [(w, start + i * step, start + (i + 1) * step) for i, w in enumerate(text.split())]


NARRATIONS = [
    audio_qc.Narration("s1", "Every alert is triaged first.", 0.5, 2.0),
    audio_qc.Narration("s2", "Analysts open only what matters.", 4.0, 6.0),
]


def test_the_voice_over_heard_right_passes():
    heard = {"s1": _heard("every alert is triaged first", 0.5), "s2": _heard("analysts open only what matters", 4.0)}
    wer, scenes, findings = audio_qc.transcript_findings(heard, NARRATIONS)
    assert wer == 0.0 and findings == [] and [s["wer"] for s in scenes] == [0.0, 0.0]


def test_a_scene_heard_wrong_fails_at_its_narrations_time():
    heard = {"s1": _heard("every alert is triaged first", 0.5), "s2": _heard("anal lists opened what", 4.0)}
    wer, scenes, findings = audio_qc.transcript_findings(heard, NARRATIONS)
    assert wer > 0.1 and scenes[1]["wer"] > 0.5
    assert [(f.kind, f.scene_id, f.at, f.end, f.severity) for f in findings] == [(TRANSCRIPT, "s2", 4.0, 6.0, FAIL)]
    assert 'heard "anal lists opened what"' in findings[0].message


def test_a_sentence_heard_twice_is_found_at_its_second_hearing():
    narrations = [audio_qc.Narration("s1", "We hunt threats every day. Then we report.", 0.0, 6.0)]
    heard = _heard("we hunt threats every day", 0.0) + _heard("we hunt threats every day", 2.0) + _heard("then we report", 4.0)
    findings = audio_qc.repeated_sentences(heard, narrations)
    assert len(findings) == 1 and findings[0].kind == REPEATED_SENTENCE
    assert findings[0].at == pytest.approx(2.0) and findings[0].data["times"] == [0.0, 2.0]
    # Written twice, heard twice: no finding.
    twice = [audio_qc.Narration("s1", "We hunt threats every day. We hunt threats every day.", 0.0, 6.0)]
    assert audio_qc.repeated_sentences(heard, twice) == []


def test_another_scene_saying_its_own_different_sentence_is_no_repeat():
    """Two scenes sharing a subject and object with another verb: each says its own text, nothing is heard twice."""
    narrations = [
        audio_qc.Narration("a", "Product X proves your defenses.", 0.0, 3.0),
        audio_qc.Narration("b", "Product X scores your defenses against real attacks.", 60.0, 64.0),
    ]
    heard = _heard("product x proves your defenses", 0.0) + _heard(
        "product x scores your defenses against real attacks", 60.0
    )
    assert audio_qc.repeated_sentences(heard, narrations) == []


def test_a_real_repeat_across_scenes_names_both_scenes_and_the_words_heard():
    """Scene b's take said scene a's sentence instead of its own text: still a blocking repeat, said where it was."""
    narrations = [
        audio_qc.Narration("a", "Product X proves your defenses.", 0.0, 3.0),
        audio_qc.Narration("b", "Product X scores your defenses against real attacks.", 60.0, 64.0),
    ]
    heard = _heard("product x proves your defenses", 0.0) + _heard("product x proves your defenses", 60.0)
    [finding] = audio_qc.repeated_sentences(heard, narrations)
    assert (finding.kind, finding.scene_id, finding.severity) == (REPEATED_SENTENCE, "b", FAIL)
    assert finding.at == pytest.approx(60.0) and finding.data["scenes"] == ["a", "b"]
    assert finding.data["written_in"] == "a" and finding.data["heard"] == "product x proves your defenses"
    assert 'scene `b`, whose take said "product x proves your defenses" instead of its own text' in finding.message
    assert "scene `a`'s" in finding.message


def test_a_sentence_two_scenes_write_is_never_explained_away_by_the_other_scene():
    """Written in each of two scenes: each scene's own text never explains the other's hearing away."""
    narrations = [
        audio_qc.Narration("a", "Product X proves your defenses.", 0.0, 3.0),
        audio_qc.Narration("b", "Product X proves your defenses.", 60.0, 63.0),
    ]
    heard = _heard("product x proves your defenses", 0.0) + _heard("product x proves your defenses", 60.0)
    found = audio_qc.repeated_sentences(heard, narrations)
    # One sentence written twice (once per scene) and heard twice: the save refuses it (shared_sentences), not this.
    assert found == []
    # A near-identical pair (one word apart): the other scene's text never explains it away, it still fails in b.
    first = "Product X proves your defenses against every real attack today."
    second = "Product X proves all your defenses against every real attack today."
    narrations = [audio_qc.Narration("a", first, 0.0, 5.0), audio_qc.Narration("b", second, 60.0, 65.0)]
    heard = _heard(first.lower().rstrip("."), 0.0) + _heard(second.lower().rstrip("."), 60.0)
    found = audio_qc.repeated_sentences(heard, narrations)
    assert found and {f.kind for f in found} == {REPEATED_SENTENCE} and {f.severity for f in found} == {FAIL}
    assert "b" in {f.scene_id for f in found}


def test_a_narration_is_transcribed_where_its_span_holds_voice():
    silence = np.zeros(round(3.0 * RATE), np.float32)
    stem = np.concatenate([silence[: RATE // 2], _speech_like(1.5, 8), silence, _speech_like(1.0, 9)])
    voiced, total = voiced_runs(stem, RATE), len(stem) / RATE
    # Its placed span reaches two seconds past its speech: the silence there is never sent.
    (window,) = audio_qc.narration_windows(audio_qc.Narration("s1", "", 0.0, 4.0), voiced, total)
    assert window[0] == pytest.approx(0.3, abs=0.05) and window[1] == pytest.approx(2.2, abs=0.05)
    # A span starting inside the other take's voice: padded within the span's own padding, never past the stem.
    (window,) = audio_qc.narration_windows(audio_qc.Narration("s2", "", 5.1, 6.0), voiced, total)
    assert window == (pytest.approx(4.9), pytest.approx(6.0, abs=0.01))
    # A span with no voice at all: nothing to transcribe.
    assert audio_qc.narration_windows(audio_qc.Narration("s3", "", 2.4, 4.6), voiced, total) == []


class _Transcriber:
    model = "fake-stt"
    provider = "OpenAI (Production)"

    def __init__(self, parts: list[list[tuple[str, float, float]]]) -> None:
        self.parts = list(parts)
        self.seconds: list[float] = []

    async def transcribe(self, audio, *, filename="audio.wav", language=None, words=False):
        assert words and audio[:4] == b"RIFF"
        self.seconds.append((len(audio) - 44) / 2 / RATE)
        said = self.parts.pop(0)
        return Transcript(" ".join(w for w, _s, _e in said), tuple(TranscribedWord(w, s, e) for w, s, e in said))


TAKES = {"s1": (0.5, _speech_like(1.5, 8)), "s2": (4.0, _speech_like(1.0, 9))}
SAID = {"s1": "every alert is triaged first", "s2": "analysts open only what matters"}


def _placed(changed=None) -> np.ndarray:
    """The two takes of TAKES on one stem; *changed* alters the voice heard after it (an encode, a stitch fault)."""
    voice = np.zeros(7 * RATE, np.float32)
    for start, take in TAKES.values():
        voice[round(start * RATE) : round(start * RATE) + len(take)] = take
    return changed(voice) if changed else voice


def _takes(monkeypatch, tmp_path, changed=None) -> tuple[list, dict]:
    """The takes as voice_check left them - WAV files and the words heard in each - and the voice heard altered."""
    stem, heard = _placed(), _placed(changed)

    async def demux(_video, out):
        return SimpleNamespace(duration=7.0, has_audio=True)

    async def decode(path, *, rate):
        chosen = heard if path.endswith("voice-encoded.m4a") else stem if path == "stem.wav" else stem * 0.8
        return chosen.astype("<f4").tobytes()

    async def encode(_stem, _out, *, bitrate, span):
        return None

    monkeypatch.setattr(encoder, "demux_audio", demux)
    monkeypatch.setattr(encoder, "decode_mono", decode)
    monkeypatch.setattr(encoder, "encode_voice_like_mix", encode)
    narrations, known = [], {}
    for scene, (start, take) in TAKES.items():
        path = tmp_path / f"voice-{scene}.wav"
        path.write_bytes(audio_qc._wav(take, RATE))
        narrations.append(audio_qc.Narration(scene, SAID[scene].capitalize() + ".", start, start + len(take) / RATE))
        words = _heard(SAID[scene], 0.0, step=len(take) / RATE / len(SAID[scene].split()))
        known[scene] = audio_qc.HeardTake(str(path), words, voice_check.take_digest(str(path)))
    return narrations, known


def _check(tmp_path, narrations, transcriber, known):
    return asyncio.run(
        audio_qc.check_audio(
            final_path="final.mp4",
            stem_path="stem.wav",
            narrations=narrations,
            transcriber=transcriber,
            unchecked_reason=None,
            scene_of=lambda at: None,
            workdir=str(tmp_path),
            known=known,
        )
    )


def test_a_narration_heard_back_already_is_read_from_its_own_words_while_the_viewer_hears_that_take(monkeypatch, tmp_path):
    """N transcriptions for N narrations, not 2N - but a take the encode or the stitch changed is heard again."""
    # The encode leaves the voice as it was, up to a gain and its own noise: nothing is transcribed again.
    noise = np.random.default_rng(3).standard_normal(7 * RATE).astype(np.float32) * 0.002
    narrations, known = _takes(monkeypatch, tmp_path, changed=lambda v: v * 0.7 + noise)
    check = _check(tmp_path, narrations, _Transcriber([]), known)
    assert check.summary["transcribed"] and check.summary["wer"] == 0.0 and "changed_since_heard" not in check.summary
    assert (check.summary["reused_transcripts"], check.summary["transcribed_parts"]) == (2, 0)
    # The transcription is said with its model and its provider.
    assert (check.summary["model"], check.summary["provider"]) == ("fake-stt", "OpenAI (Production)")

    # 80 ms of the second take lost on the way to the viewer: that take alone is transcribed again, as heard.
    def dropped(voice):
        voice[round(4.4 * RATE) : round(4.48 * RATE)] = 0.0
        return voice

    narrations, known = _takes(monkeypatch, tmp_path, changed=dropped)
    transcriber = _Transcriber([_heard("analysts open what matters", 0.2)])
    check = _check(tmp_path, narrations, transcriber, known)
    assert check.summary["changed_since_heard"] == ["s2"] and check.summary["reused_transcripts"] == 1
    assert check.summary["transcribed_parts"] == 1 and len(transcriber.seconds) == 1 and transcriber.seconds[0] < 2.0
    # A take placed that is no longer the sound its words were heard in is transcribed, whatever it sounds like.
    narrations, known = _takes(monkeypatch, tmp_path)
    known["s1"] = dataclasses.replace(known["s1"], digest="0" * 64)
    check = _check(tmp_path, narrations, _Transcriber([_heard(SAID["s1"], 0.2)]), known)
    assert check.summary["changed_since_heard"] == ["s1"] and check.summary["transcribed_parts"] == 1
    # A take with no words kept (a record from before they were) is transcribed too.
    narrations, known = _takes(monkeypatch, tmp_path)
    check = _check(tmp_path, narrations, _Transcriber([_heard(SAID["s2"], 0.2, step=0.2)]), {"s1": known["s1"]})
    assert check.summary["wer"] == 0.0 and (check.summary["reused_transcripts"], check.summary["transcribed_parts"]) == (1, 1)


def test_each_narration_heard_is_the_checks_progress_and_what_it_raises_stops_the_check(monkeypatch, tmp_path):
    """Told before the first part is transcribed and after each, so a check that stalls is told from a long one."""
    told: list[tuple[int, int]] = []

    async def heard(done: int, total: int) -> None:
        told.append((done, total))

    def check(narrations, transcriber, known, progress=heard):
        return asyncio.run(
            audio_qc.check_audio(
                final_path="final.mp4",
                stem_path="stem.wav",
                narrations=narrations,
                transcriber=transcriber,
                unchecked_reason=None,
                scene_of=lambda at: None,
                workdir=str(tmp_path),
                known=known,
                heard=progress,
            )
        )

    # Nothing heard before: the two takes, one part each.
    narrations, _known = _takes(monkeypatch, tmp_path)
    done = check(narrations, _Transcriber([_heard(SAID["s1"], 0.2), _heard(SAID["s2"], 0.2)]), {})
    assert told == [(0, 2), (1, 2), (2, 2)] and done.summary["transcribed_parts"] == 2
    # The first read from its own words: heard at once, the second once transcribed.
    def dropped(voice):
        voice[round(4.4 * RATE) : round(4.48 * RATE)] = 0.0
        return voice

    told.clear()
    narrations, known = _takes(monkeypatch, tmp_path, changed=dropped)
    check(narrations, _Transcriber([_heard("analysts open what matters", 0.2)]), known)
    assert told == [(1, 2), (2, 2)]

    # The speech-to-text failing is a note, as before; what the progress raises (the render cancelled) is not.
    class _Refused:
        model, provider = "fake-stt", "OpenAI"

        async def transcribe(self, *_args, **_kwargs):
            raise RuntimeError("the speech-to-text refused")

    told.clear()
    narrations, _known = _takes(monkeypatch, tmp_path)
    refused = check(narrations, _Refused(), {})
    assert told == [(0, 2)] and refused.summary["transcribed"] is False and "refused" in refused.notes[-1]

    class _CancelledError(Exception):
        pass

    async def cancelled(done: int, _total: int) -> None:
        if done:
            raise _CancelledError

    with pytest.raises(_CancelledError):
        check(narrations, _Transcriber([_heard(SAID["s1"], 0.2), _heard(SAID["s2"], 0.2)]), {}, progress=cancelled)


def test_a_take_is_told_apart_from_what_the_viewer_hears_by_its_sound():
    take = _speech_like(1.5, 8)
    voice = _placed()
    assert audio_qc.plays_the_take(voice, take, 0.5)
    # An encoder's delay (20 ms) and a quieter level are no change.
    delayed = np.concatenate([np.zeros(round(0.02 * RATE), np.float32), voice])[: len(voice)] * 0.5
    assert audio_qc.plays_the_take(delayed, take, 0.5)
    # 120 ms of it said twice in a row, a stutter the stitch made: it is no longer the take heard.
    at, chunk = round(1.0 * RATE), round(0.12 * RATE)
    stuttered = np.concatenate([voice[: at + chunk], voice[at : at + chunk], voice[at + chunk :]])[: len(voice)]
    assert not audio_qc.plays_the_take(stuttered, take, 0.5)
    # Another take there, or one past the end of what is heard, is not played either.
    assert not audio_qc.plays_the_take(voice, _speech_like(1.5, 10), 0.5)
    assert not audio_qc.plays_the_take(voice[: round(1.0 * RATE)], take, 0.5)


def test_the_whole_check_times_its_findings_in_the_mp4(monkeypatch, tmp_path):
    voice = np.concatenate([np.zeros(RATE // 2, np.float32), _with(_burst(), 4)])
    mix = voice * 0.8

    async def demux(_video, out):
        return SimpleNamespace(duration=len(mix) / RATE, has_audio=True)

    encoded: list[tuple[str, str, str]] = []

    async def decode(path, *, rate):
        assert rate == RATE
        return (voice if path == "stem.wav" or path.endswith("voice-encoded.m4a") else mix).astype("<f4").tobytes()

    async def encode(stem, out, *, bitrate, span):
        encoded.append((stem, Path(out).name, bitrate))

    monkeypatch.setattr(encoder, "demux_audio", demux)
    monkeypatch.setattr(encoder, "decode_mono", decode)
    monkeypatch.setattr(encoder, "encode_voice_like_mix", encode)
    narrations = [audio_qc.Narration("s1", "We hunt threats every day.", 0.5, 3.5)]
    transcriber = _Transcriber([[("we", 0.1, 0.3), ("hunt", 0.3, 0.6), ("threats", 0.6, 1.0), ("every", 1.0, 1.2), ("day", 1.2, 1.4)]])
    check = asyncio.run(
        audio_qc.check_audio(
            final_path="final.mp4",
            stem_path="stem.wav",
            narrations=narrations,
            transcriber=transcriber,
            unchecked_reason=None,
            scene_of=lambda at: "s1",
            workdir=str(tmp_path),
            bitrate="192k",
        )
    )
    stutters = [f for f in check.findings if f.kind == STUTTER]
    assert check.status == "failed" and len(stutters) == 1
    assert stutters[0].at == pytest.approx(1.5, abs=0.02) and stutters[0].data["source"] == "voice"
    assert check.summary["wer"] == 0.0 and check.summary["transcribed"]
    # The voice heard is the stem as the final mix encodes it, at the render's bitrate.
    assert encoded == [("stem.wav", "voice-encoded.m4a", "192k")] and check.summary["heard"] == "encoded"
    assert check.pronunciation == {"status": "skipped", "reason": "the narration says no lexicon term"}


def test_without_speech_to_text_the_rest_still_runs(monkeypatch, tmp_path):
    clean = _speech_like(2.0, 10)

    async def demux(_video, out):
        return SimpleNamespace(duration=2.0, has_audio=True)

    async def decode(_path, *, rate):
        return clean.astype("<f4").tobytes()

    monkeypatch.setattr(encoder, "demux_audio", demux)
    monkeypatch.setattr(encoder, "decode_mono", decode)
    check = asyncio.run(
        audio_qc.check_audio(
            final_path="final.mp4",
            stem_path="stem.wav",
            narrations=[],
            transcriber=None,
            unchecked_reason="no OpenAI provider",
            scene_of=lambda at: None,
            workdir=str(tmp_path),
        )
    )
    assert check.status == "passed" and check.summary["transcribed"] is False and "no OpenAI provider" in check.notes[0]
