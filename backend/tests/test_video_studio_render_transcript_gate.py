"""The whole render's transcript check grades each narration on its own placed span, never on what the speech-to-text
writes where nobody speaks. Synthetic audio only.

A 181 s render whose 24 narrations each passed their own check was refused by the audio gate: whisper-1 wrote
"Diolch am wylio'r fideo" (Welsh, "thanks for watching the video") over music-only stretches, the render's one
alignment of every word against every narration charged those words to the neighbouring scenes ("Available now" 500%
wrong while heard exactly), and a brand term heard two seconds late was reported "not heard".

Each word is a tone of its own pitch, said by takes placed on a stem; a speech-to-text decodes the tones back and
hallucinates as whisper-1 does - a second or more of what it is sent with no speech is "Diolch am wylio'r fideo", in
Welsh when nothing else was said:

* 2 s of music-only before and after each line: the whole render and every scene preview pass, no transcript finding;
* a Welsh segment planted on the stem at a music-only moment is dropped as hallucinated, counted, never graded - by the
  model's known phrases, or by its language alone;
* scenes on screen offset by one scene against their narrations: every narration 0 % wrong, graded on its own span;
* a brand term planned at t and heard at t + 2.3 s passes, at its time; past 3 s, or heard as a neighbour's own, not;
* a scene's word error rate is at most 100 %, and a scene its own check passed is an advisory naming both transcripts.
"""

from __future__ import annotations

import asyncio
import io
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from app.llm import transcription
from app.llm.transcription import TranscribedSegment, TranscribedWord, Transcript
from app.services.video_studio import (
    audio_qc,
    encoder,
    frame_freeze,
    render,
    render_checks,
    speech_timing,
    stitch,
    voice_check,
)
from app.services.video_studio import lexicon as lex

RATE = audio_qc.ANALYSIS_RATE
WORD_SECONDS, WORD_GAP = 0.22, 0.06
#: Music only, before and after each line: the stem silent there, the mix playing the bed.
MUSIC_ONLY = 2.0
SAID = {
    "s1": "Findings, ranked and filtered in a second.",
    "s2": "What to fix first.",
    "s3": "Available now.",
}
VOCABULARY = sorted({word for text in SAID.values() for word in speech_timing.tokens(text)})
#: whisper-1's own words over silence or music, and another Welsh phrase it is not known for.
WELSH = "Diolch am wylio'r fideo"
OTHER_WELSH = "Mae hi'n bwrw glaw"
PLANTED = {2900.0: WELSH, 3400.0: OTHER_WELSH}
#: A second or more with no speech in what the speech-to-text is sent: it writes its Welsh line there.
HALLUCINATES_AFTER = 1.0
ENGLISH = audio_qc.OwnCheck(True, "", "english")


def _frequency(word: str) -> float:
    return 300.0 + 45.0 * VOCABULARY.index(word)


def _tone(frequency: float, seconds: float = WORD_SECONDS) -> np.ndarray:
    t = np.arange(round(seconds * RATE)) / RATE
    ramp = np.minimum(1.0, np.minimum(t, seconds - t) / 0.01)
    return (0.4 * np.sin(2 * np.pi * frequency * t) * ramp).astype(np.float32)


def _line(text: str) -> np.ndarray:
    gap = np.zeros(round(WORD_GAP * RATE), np.float32)
    parts = []
    for index, word in enumerate(speech_timing.tokens(text)):
        parts += [gap] if index else []
        parts.append(_tone(_frequency(word)))
    return np.concatenate(parts)


class _Whisper:
    """A speech-to-text for tone-coded speech that hallucinates as whisper-1 does over silence or music."""

    model = transcription.TRANSCRIPTION_MODEL
    provider = "OpenAI (Production)"

    def __init__(self) -> None:
        #: The longest stretch with no speech in each request: what it hallucinated over.
        self.silences: list[float] = []

    @staticmethod
    def _runs(samples: np.ndarray) -> list[tuple[int, int]]:
        frame = round(0.005 * RATE)
        loud = [np.sqrt(np.mean(samples[i : i + frame] ** 2)) > 0.02 for i in range(0, len(samples) - frame + 1, frame)]
        runs, start = [], None
        for index, on in enumerate([*loud, False]):
            if on and start is None:
                start = index
            elif not on and start is not None:
                if (index - start) * frame / RATE >= 0.06:
                    runs.append((start * frame, index * frame))
                start = None
        return runs

    @staticmethod
    def _say(text: str, start: float, end: float) -> list[TranscribedWord]:
        words = text.split()
        step = (end - start) / len(words)
        return [TranscribedWord(word, start + i * step, start + (i + 1) * step) for i, word in enumerate(words)]

    async def transcribe(self, audio, *, filename="audio.wav", language=None, words=False):
        with wave.open(io.BytesIO(audio)) as read:
            samples = np.frombuffer(read.readframes(read.getnframes()), "<i2").astype(np.float32) / 32767.0
        total = len(samples) / RATE
        runs = self._runs(samples)
        events: list[tuple[float, str, list[TranscribedWord]]] = []
        for first, last in runs:
            chunk = samples[first:last]
            spectrum = np.abs(np.fft.rfft(chunk * np.hanning(len(chunk)), n=1 << 15))
            peak = float(np.fft.rfftfreq(1 << 15, 1.0 / RATE)[int(np.argmax(spectrum))])
            planted = next((phrase for frequency, phrase in PLANTED.items() if abs(peak - frequency) < 100), None)
            if planted is not None:
                events.append((first / RATE, planted, self._say(planted, first / RATE, last / RATE)))
            else:
                word = min(VOCABULARY, key=lambda w: abs(_frequency(w) - peak))
                events.append((first / RATE, "", [TranscribedWord(word, first / RATE, last / RATE)]))
        edges = [0.0] + [t for first, last in runs for t in (first / RATE, last / RATE)] + [total]
        quiet = [(edges[i], edges[i + 1]) for i in range(0, len(edges), 2)]
        self.silences.append(max((b - a for a, b in quiet), default=0.0))
        for a, b in quiet:
            if b - a >= HALLUCINATES_AFTER:
                events.append((a, WELSH, self._say(WELSH, a + 0.1, b - 0.1)))
        events.sort(key=lambda event: event[0])
        said: list[TranscribedWord] = []
        segments: list[TranscribedSegment] = []
        speech: list[TranscribedWord] = []
        for _at, phrase, heard in [*events, (total, "end", [])]:
            said += heard
            if phrase and speech:
                segments.append(TranscribedSegment(" ".join(w.text for w in speech), speech[0].start, speech[-1].end))
                speech = []
            if phrase and heard:
                segments.append(TranscribedSegment(phrase, heard[0].start, heard[-1].end))
            elif not phrase:
                speech += heard
        language = "english" if any(not phrase for _at, phrase, _w in events) else "welsh"
        return Transcript(" ".join(w.text for w in said), tuple(said), language, self.model, tuple(segments))


def _render(planted: dict[float, float] | None = None) -> tuple[np.ndarray, np.ndarray, dict[str, tuple[float, float]]]:
    """The stem, the mix and each narration's placed span: every take carries :data:`MUSIC_ONLY` seconds of nothing
    before and after its line, the bed plays under all of it. *planted* ``{at: frequency}`` adds a tone on the stem."""
    spans, at, lines = {}, 0.5, []
    for scene, text in SAID.items():
        line = _line(text)
        seconds = MUSIC_ONLY + len(line) / RATE + MUSIC_ONLY
        spans[scene] = (at, at + seconds)
        lines.append((at + MUSIC_ONLY, line))
        at += seconds + 0.02
    voice = np.zeros(round((at + 0.5) * RATE), np.float32)
    for start, line in lines:
        voice[round(start * RATE) : round(start * RATE) + len(line)] = line
    for start, frequency in (planted or {}).items():
        tone = _tone(frequency, 0.4)
        voice[round(start * RATE) : round(start * RATE) + len(tone)] += tone
    t = np.arange(len(voice)) / RATE
    bed = 0.08 * (np.sin(2 * np.pi * 196.0 * t) + np.sin(2 * np.pi * 247.0 * t)) * (0.7 + 0.3 * np.sin(2 * np.pi * 0.2 * t))
    return voice, (voice + bed).astype(np.float32), spans


def _check(monkeypatch, tmp_path, voice, mix, spans, *, span=None, scene_id=None, own=None, transcriber=None):
    """The audio check of the whole render (*span* ``None``) or of a scene preview cut to *span*, as ``check_render``
    asks it."""
    cut = slice(None) if span is None else slice(round(span[0] * RATE), round((span[0] + span[1]) * RATE))

    async def demux(_video, _out):
        return SimpleNamespace(duration=len(mix[cut]) / RATE, has_audio=True)

    async def decode(path, *, rate):
        chosen = voice[cut] if path.endswith("voice-encoded.m4a") else voice if path == "stem.wav" else mix[cut]
        return chosen.astype("<f4").tobytes()

    async def encode(_stem, _out, *, bitrate, span):
        return None

    monkeypatch.setattr(encoder, "demux_audio", demux)
    monkeypatch.setattr(encoder, "decode_mono", decode)
    monkeypatch.setattr(encoder, "encode_voice_like_mix", encode)
    narrations = audio_qc.graded_narrations(spans, SAID, offset=span[0] if span else 0.0, scene_id=scene_id)
    return asyncio.run(
        audio_qc.check_audio(
            final_path="final.mp4",
            stem_path="stem.wav",
            narrations=narrations,
            transcriber=transcriber or _Whisper(),
            unchecked_reason=None,
            scene_of=lambda at: None,
            workdir=str(tmp_path),
            span=span,
            known={},
            own=dict.fromkeys(SAID, ENGLISH) if own is None else own,
        )
    )


def _graded(check) -> render_checks.Checks:
    return render_checks.Checks(audio=check.as_summary(), pronunciation=check.pronunciation, findings=check.findings)


def test_music_only_before_and_after_each_line_passes_the_gate_with_no_transcript_finding(monkeypatch, tmp_path):
    voice, mix, spans = _render()
    whisper = _Whisper()
    whole = _check(monkeypatch, tmp_path, voice, mix, spans, transcriber=whisper)
    assert whole.status == render_checks.PASSED and whole.findings == [], [f.message for f in whole.findings]
    assert [scene["wer"] for scene in whole.summary["scenes"]] == [0.0, 0.0, 0.0] and whole.summary["wer"] == 0.0
    assert "hallucinated" not in whole.summary and whole.summary["transcribed_parts"] == len(SAID)
    # Nothing it was sent held a second without speech: the music-only stretches were never transcribed.
    assert max(whisper.silences) < HALLUCINATES_AFTER
    assert render_checks.open_findings(_graded(whole).as_dict(), same_quality=True) == []
    # Each scene's preview too, from its transition in to past the next one's start.
    for scene, (start, end) in spans.items():
        preview = _check(monkeypatch, tmp_path, voice, mix, spans, span=(max(0.0, start - 1.0), end - start + 2.0), scene_id=scene)
        assert preview.findings == [] and [s["scene_id"] for s in preview.summary["scenes"]] == [scene]
        assert preview.summary["scenes"][0]["wer"] == 0.0 and "hallucinated" not in preview.summary


def test_a_welsh_segment_planted_at_a_music_only_moment_is_dropped_as_hallucinated(monkeypatch, tmp_path):
    _voice, _mix, spans = _render()
    line_end = spans["s2"][1] - MUSIC_ONLY
    # 0.8 s after the line: transcribed with it, the model's known closing line in its own segment.
    voice, mix, spans = _render({line_end + 0.8: 2900.0})
    check = _check(monkeypatch, tmp_path, voice, mix, spans)
    assert check.status == render_checks.PASSED and check.findings == [], [f.message for f in check.findings]
    assert check.summary["wer"] == 0.0 and {s["scene_id"]: s["wer"] for s in check.summary["scenes"]}["s2"] == 0.0
    hallucinated = check.summary["hallucinated"]
    assert (hallucinated["segments"], hallucinated["words"]) == (1, 4)
    (dropped,) = hallucinated["dropped"]
    assert dropped["scene_id"] == "s2" and dropped["text"] == WELSH and dropped["why"].startswith("what its speech-to-text")
    assert line_end + 0.75 <= dropped["at"] < dropped["end"] <= line_end + 1.25
    # 1.6 s after: a window of its own, heard in Welsh - dropped by its language alone, a phrase no list knows.
    voice, mix, spans = _render({line_end + 1.55: 3400.0})
    check = _check(monkeypatch, tmp_path, voice, mix, spans)
    assert check.findings == [] and check.summary["wer"] == 0.0 and check.summary["language"] == "english"
    (dropped,) = check.summary["hallucinated"]["dropped"]
    assert dropped["text"] == OTHER_WELSH and dropped["why"] == "heard in welsh, the voice-over in english"
    # Without its own checks' language, the voice-over's is the one most of its words were heard in.
    check = _check(monkeypatch, tmp_path, voice, mix, spans, own={})
    assert check.findings == [] and check.summary["language"] == "english"
    assert check.summary["hallucinated"]["dropped"][0]["text"] == OTHER_WELSH


def test_scene_windows_offset_by_one_scene_score_every_narration_at_zero(monkeypatch, tmp_path):
    """The render's own check: each narration graded where the stem placed its take, never under its scene's picture."""
    voice, mix, spans = _render()

    async def demux(_video, _out):
        return SimpleNamespace(duration=len(mix) / RATE, has_audio=True)

    async def decode(path, *, rate):
        return (voice if path == "stem.wav" or path.endswith("voice-encoded.m4a") else mix).astype("<f4").tobytes()

    async def encode(_stem, _out, *, bitrate, span):
        return None

    async def watched(*_args, **_kwargs):
        return SimpleNamespace(as_summary=dict, findings=[])

    monkeypatch.setattr(encoder, "demux_audio", demux)
    monkeypatch.setattr(encoder, "decode_mono", decode)
    monkeypatch.setattr(encoder, "encode_voice_like_mix", encode)
    monkeypatch.setattr(frame_freeze, "check_frames", watched)
    scenes = list(SAID)
    # Each scene on screen one narration late: under s1's narration the picture is s2's, and so on.
    document = {"scenes": [{"id": scenes[0], "start": 0.0}] + [{"id": scenes[i + 1], "start": spans[scenes[i]][0]} for i in range(len(scenes) - 1)]}
    assert render_checks.scene_at(document, spans["s1"][0] + 1.0)["id"] == "s2"
    stem = stitch.Stem(
        "stem.wav",
        RATE,
        len(voice),
        tuple(stitch.Placement(s, round(a * RATE), round(b * RATE), RATE, False) for s, (a, b) in spans.items()),
    )
    plan = SimpleNamespace(
        document=document,
        narration_starts={s: a for s, (a, _b) in spans.items()},
        narration_spans=spans,
        voice_windows=lambda **_: list(spans.values()),
        duration=len(voice) / RATE,
    )
    media = render._Media()
    media.lines = [SimpleNamespace(scene_id=scene, text=text) for scene, text in SAID.items()]
    media.transcriber = _Whisper()
    for scene, (start, end) in spans.items():
        take = voice_check.Attempt("marin", "same", f"voice-{scene}.wav", end - start)
        take.check, take.language = voice_check.TranscriptCheck(0.0, SAID[scene].lower()), "english"
        media.spoken.append(voice_check.SpokenSegment(scene, take.path, end - start, voice_check.PASSED, [take]))
        media.voice_paths[scene] = take.path
    checks = asyncio.run(
        render.check_render(final_path="final.mp4", stem=stem, plan=plan, media=media, size=(640, 360), fps=30, workdir=tmp_path)
    )
    assert [(s["scene_id"], s["wer"]) for s in checks.audio["scenes"]] == [(s, 0.0) for s in scenes]
    assert [(s["at"], s["end"]) for s in checks.audio["scenes"]] == [(round(a, 3), round(b, 3)) for a, b in spans.values()]
    assert checks.audio["wer"] == 0.0 and checks.audio["language"] == "english" and checks.findings == []
    assert not checks.failed and checks.audio["transcribed_parts"] == len(SAID)


LEXICON = lex.merge(lex.load_seed())
BRAND = [audio_qc.Narration("s1", "We ship it with Filigran.", 0.5, 2.5), audio_qc.Narration("s2", "Available now.", 3.0, 6.0)]


def _planned() -> float:
    """When the narration's pace places Filigran: its word paced over the span the stem placed."""
    narration = BRAND[0]
    paced = speech_timing.paced_words(narration.text, narration.start, narration.end - narration.start)
    return paced[4].start


def _brand(at: float):
    """The term missing from its own narration's words and heard at *at* among the next narration's."""
    heard = {
        "s1": [("we", 0.5, 0.7), ("ship", 0.7, 1.0), ("it", 1.0, 1.1), ("with", 1.1, 1.4)],
        "s2": [("available", 3.0, 3.5), ("now", 3.5, 3.8), ("Filigran", at, at + 0.4)],
    }
    hearing = audio_qc.hear_narrations(heard, BRAND, LEXICON)
    return audio_qc.pronunciation_findings(hearing, BRAND), hearing


def test_a_brand_term_heard_two_seconds_past_its_planned_time_passes():
    planned = _planned()
    (section, findings), hearing = _brand(planned + 2.3)
    assert section["status"] == render_checks.PASSED and section["terms"][0]["passed"] == 1
    assert not [f for f in findings if f.severity == render_checks.FAIL] and hearing.found_nearby == 1
    (term,) = hearing.terms
    assert term.passed and term.owner == 0 and term.at == pytest.approx(planned + 2.3) and term.heard == "Filigran"
    # Past the reach: not heard, at its place in its own narration.
    (section, findings), hearing = _brand(planned + audio_qc.TERM_REACH + 0.5)
    assert section["status"] == render_checks.FAILED and hearing.found_nearby == 0
    (failed,) = [f for f in findings if f.severity == render_checks.FAIL]
    assert failed.scene_id == "s1" and failed.message.startswith("brand term mispronounced: Filigran not heard at ")


def test_a_brand_term_its_neighbour_says_and_hears_is_never_taken_for_a_missed_one():
    narrations = [BRAND[0], audio_qc.Narration("s2", "Filigran is available now.", 3.0, 6.0)]
    planned = _planned()
    heard = {
        "s1": [("we", 0.5, 0.7), ("ship", 0.7, 1.0), ("it", 1.0, 1.1), ("with", 1.1, 1.4)],
        "s2": [("Filigran", planned + 1.5, planned + 1.9), ("is", 4.0, 4.2), ("available", 4.2, 4.6), ("now", 4.6, 4.9)],
    }
    hearing = audio_qc.hear_narrations(heard, narrations, LEXICON)
    assert [(t.owner, t.passed) for t in hearing.terms] == [(0, False), (1, True)] and hearing.found_nearby == 0


def test_a_scenes_word_error_rate_is_at_most_all_its_words():
    narrations = [audio_qc.Narration("s3", "Available now.", 0.5, 2.0)]
    heard = {"s3": [(w, 0.5 + 0.1 * i, 0.6 + 0.1 * i) for i, w in enumerate(["diolch", "am", "wylio'r", "fideo", "available", "now", "and", "more", "words"])]}
    wer, scenes, (finding,) = audio_qc.transcript_findings(heard, narrations)
    assert scenes[0]["wer"] == 1.0 and wer == 1.0 and finding.message.startswith("100% of its narration's words")


def test_a_scene_its_own_check_passed_is_an_advisory_naming_both_transcripts():
    narrations = [audio_qc.Narration("s1", "What to fix first.", 0.5, 2.0)]
    heard = {"s1": [("what", 0.5, 0.7), ("the", 0.7, 0.8), ("fix", 0.8, 1.0), ("thirst", 1.0, 1.3)]}
    own = {"s1": audio_qc.OwnCheck(True, "What to fix first.", "english")}
    _wer, _scenes, (finding,) = audio_qc.transcript_findings(heard, narrations, own=own)
    assert finding.severity == render_checks.WARN and finding.kind == render_checks.TRANSCRIPT
    assert 'heard "what the fix thirst"' in finding.message and 'its own check heard "What to fix first."' in finding.message
    assert finding.data == {"wer": 0.5, "advisory": True, "heard": "what the fix thirst", "own_transcript": "What to fix first."}
    checks = render_checks.Checks(findings=[finding])
    assert not checks.failed and render_checks.open_findings(checks.as_dict(), same_quality=True) == []
    # A narration its own check did not pass (never heard, or failed) is held to the render's hearing.
    for unpassed in ({}, {"s1": audio_qc.OwnCheck(False, "what to fix", "english")}):
        _wer, _scenes, (finding,) = audio_qc.transcript_findings(heard, narrations, own=unpassed)
        assert finding.severity == render_checks.FAIL and "advisory" not in finding.data


def test_a_narration_missing_from_the_stem_fails_whatever_its_own_check_heard(monkeypatch, tmp_path):
    voice, mix, spans = _render()
    start, end = spans["s3"]
    voice[round(start * RATE) : round(end * RATE)] = 0.0
    check = _check(monkeypatch, tmp_path, voice, mix, spans)
    assert check.summary["unvoiced"] == ["s3"]
    (finding,) = [f for f in check.findings if f.kind == render_checks.TRANSCRIPT]
    assert finding.scene_id == "s3" and finding.severity == render_checks.FAIL and 'heard ""' in finding.message
    assert check.status == render_checks.FAILED


def test_a_transcript_keeps_its_segments_and_the_model_declares_what_it_writes_over_silence():
    result = {
        "text": "Available now. Diolch am wylio'r fideo.",
        "language": "english",
        "words": [{"word": "Available", "start": 0.0, "end": 0.4}, {"word": "now", "start": 0.4, "end": 0.6}],
        "segments": [{"text": " Available now.", "start": 0.0, "end": 0.6}, {"text": " Diolch am wylio'r fideo.", "start": 1.0, "end": 2.0}],
    }
    transcript = transcription._transcript(result, transcription.TRANSCRIPTION_MODEL)
    assert transcript.segments == (TranscribedSegment("Available now.", 0.0, 0.6), TranscribedSegment("Diolch am wylio'r fideo.", 1.0, 2.0))
    assert transcript.language == "english" and len(transcript.words) == 2
    assert WELSH in transcription.known_hallucinations(transcription.TRANSCRIPTION_MODEL)
    assert transcription.known_hallucinations("another-model") == ()


def test_a_known_phrase_the_narration_writes_itself_is_heard_as_said():
    narration = audio_qc.Narration("s9", "Thanks for watching, see you soon.", 0.0, 3.0)
    words = tuple(audio_qc.HeardWord(w, 0.3 * i, 0.3 * i + 0.25) for i, w in enumerate(["thanks", "for", "watching", "see", "you", "soon"]))
    window = audio_qc.HeardWindow("s9", 0.0, 3.0, "english", (("Thanks for watching, see you soon.", words),), transcription.TRANSCRIPTION_MODEL)
    kept = audio_qc.kept_words([window], [narration], {})
    assert kept.hallucinated == [] and [w.text for w in kept.heard["s9"]] == [w.text for w in words]
    # Said by a narration that does not write it: dropped from its first word to the end of its segment.
    other = audio_qc.Narration("s9", "See you soon.", 0.0, 3.0)
    kept = audio_qc.kept_words([window], [other], {})
    assert [h.text for h in kept.hallucinated] == ["thanks for watching see you soon"] and kept.heard["s9"] == []


def test_the_language_a_narration_was_heard_in_is_kept_with_its_check():
    take = voice_check.Attempt("marin", "same", "voice.wav", 1.0)
    take.check, take.language, take.object_name = voice_check.TranscriptCheck(0.0, "available now"), "english", "obj"
    record = voice_check._record(voice_check.PASSED, take, [take])
    assert record["language"] == "english"
    spoken = voice_check.SpokenSegment("s3", "voice.wav", 1.0, voice_check.PASSED, [take])
    assert (spoken.language, spoken.transcript) == ("english", "available now")
