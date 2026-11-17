"""Every narration heard back before it is placed. Synthetic speech and a scripted speech-to-text.

* the words compared: case, punctuation, numbers written either way, an
  acronym spelled out, a compound heard as two words;
* the word error rate and its threshold, the repeated token sequences a
  narration does not say itself; a proper noun heard by its sound;
* a segment that fails is placed with the take of its voice that passed on
  the same words before, else spoken again with the same voice (not
  from the cache), then slower, then with the closest voice of the same
  speech model - a failure of the video's voice, said with the voice
  used and why, tried again next time, never the platform's default voice;
  one that still fails is failed with its own closest take; a record kept
  beside the cached WAV places a narration heard once without transcribing it
  again; with no speech-to-text a narration is placed unchecked.
"""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.llm.speech import Voice
from app.llm.transcription import TranscribedWord, Transcript
from app.services.video_studio import lexicon, voice, voice_check
from app.services.video_studio.spec import Voiceover


def test_the_words_are_compared_as_said_not_as_typed():
    assert voice_check.normalized_tokens("It's 1,000 users - twenty-three of them, 10%!") == [
        "its",
        "1000",
        "users",
        "23",
        "of",
        "them",
        "10",
        "percent",
    ]
    assert voice_check.normalized_tokens("the X T M platform") == ["the", "xtm", "platform"]
    assert voice_check.normalized_tokens("two hundred and five") == ["200", "and", "5"]


def test_the_word_error_rate_counts_substitutions_deletions_and_insertions():
    ref = ["the", "platform", "hunts", "threats", "every", "day"]
    assert voice_check.align(ref, ref).wer == 0.0
    heard = ["the", "platform", "hunts", "the", "threats", "every"]
    alignment = voice_check.align(ref, heard)
    assert alignment.errors == 2 and alignment.wer == pytest.approx(2 / 6)
    assert alignment.reference_heard == (True, True, True, True, True, False)


def test_a_compound_heard_as_two_words_is_no_error():
    assert voice_check.align(["opencti", "is", "open"], ["open", "cti", "is", "open"]).wer == 0.0
    assert voice_check.align(["open", "cti", "rocks"], ["opencti", "rocks"]).wer == 0.0


def test_a_repeated_sequence_counts_only_when_the_narration_does_not_say_it():
    heard = ["we", "hunt", "the", "threats", "the", "threats", "every", "day"]
    repeats = voice_check.repeated_sequences(heard, ["we", "hunt", "the", "threats", "every", "day"])
    assert [(r.text(), r.times) for r in repeats] == [("the threats", 2)]
    assert voice_check.repeated_sequences(["very", "very", "good"], ["very", "very", "good"]) == []
    assert [r.text() for r in voice_check.repeated_sequences(["the", "the", "end"], ["the", "end"])] == ["the"]


def test_the_check_fails_over_ten_percent_or_on_a_loop():
    text = "Every alert is triaged by an agent before an analyst opens it."
    assert voice_check.compare(text, "every alert is triaged by an agent before an analyst opens it").passed
    # One word of twelve wrong: 8 %, under the threshold.
    assert voice_check.compare(text, "every alert is triaged by an agent before the analyst opens it").passed
    wrong = voice_check.compare(text, "every alert is triaged by agents before analysts open")
    assert not wrong.passed and wrong.wer > voice_check.MAX_WER and "differ" in wrong.reason()
    looped = voice_check.compare(text, "every alert is triaged by an agent an agent before an analyst opens it")
    assert looped.repeated is not None and not looped.passed and "said 2 times in a row" in looped.reason()


def test_the_check_reads_the_narration_as_its_voice_says_it():
    words = lexicon.merge((), [SimpleNamespace(text="OpenCTI", say="Open C-T-I")])
    check = voice_check.compare("Open OpenCTI now.", "open open c t i now", lexicon=words)
    assert check.passed and [(t.term, t.passed) for t in check.terms] == [("OpenCTI", True)]


MOSCONE = "Moscone Center. The cows walk in like they own the place."


def test_a_proper_noun_is_heard_by_its_sound_not_its_spelling():
    """Render 2527a4b6: one word of eleven is 9 %, two are 18 %; a name the speech-to-text spells as it
    heard it is no word error."""
    for heard in (
        "Mosconi Center. The cows walk in like they own the place.",
        "Moscow knee center, the cows walk in like they own the place.",
        "Mascone Centre. The cows walk in like they own the place.",
    ):
        check = voice_check.compare(MOSCONE, heard)
        assert check.passed and check.wer == 0.0, heard
    check = voice_check.compare(MOSCONE, "Moscow knee center, the cows walk in like they own the place.")
    # Each word heard is the name it says: "knee" is Moscone's, never Center's.
    assert [n.as_dict() for n in check.names] == [{"name": "Moscone", "heard": "Moscow knee"}]
    assert voice_check.compare(MOSCONE, MOSCONE).names == ()


def test_every_other_word_is_held_to_its_spelling_and_a_name_unsaid_is_missing():
    def wer(heard: str, text: str = MOSCONE) -> float:
        return voice_check.compare(text, heard).wer

    assert wer("Mosconi Center. The cars walk in like they own a place.") == pytest.approx(2 / 11, abs=1e-4)
    # Left out, or another name: still an error.
    assert wer("Center. The cows walk in like they own the place.") == pytest.approx(1 / 11, abs=1e-4)
    assert wer("Boston Center. The cows walk in like they own the place.") == pytest.approx(1 / 11, abs=1e-4)
    # A word that starts a sentence is no name for its capital: "The" heard "A" counts.
    assert wer("A cows walk in.", "The cows walk in.") == pytest.approx(1 / 4, abs=1e-4)


def test_a_proper_noun_is_a_capital_where_no_sentence_starts_with_it():
    from app.services.video_studio import pronunciation_check

    text = "The cows walk to Moscone. Then they leave, and SecOps stays. Moscone Center waits."
    flags = pronunciation_check.name_flags(text)
    words = [piece.text for piece in pronunciation_check.pieces_of(text)]
    assert [w for w, named in zip(words, flags, strict=True) if named] == ["Moscone", "SecOps", "Moscone", "Center"]
    # A name of the lexicon is its own token, held by its readings, never by its sound alone.
    terms = lexicon.merge((), [SimpleNamespace(text="Moscone", say="Mos-coh-nee")])
    tokens = pronunciation_check.reference_tokens([MOSCONE], terms)
    assert [t.entry is not None for t in tokens][:2] == [True, False] and [t.name for t in tokens][:2] == [False, True]


# ── The attempts ──────────────────────────────────────────────────────────


def _wav(path: str, seconds: float = 1.0, rate: int = 48000) -> None:
    rng = np.random.default_rng(len(path))
    pcm = (0.2 * rng.standard_normal(round(seconds * rate)) * 32767).astype("<i2").tobytes()
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    with open(path, "wb") as out:
        out.write(header + b"data" + struct.pack("<I", len(pcm)) + pcm)


class _Transcriber:
    model = "fake-stt"

    def __init__(self, heard: list[str]) -> None:
        self.heard = list(heard)
        self.calls = 0

    async def transcribe(self, audio, *, filename="audio.wav", language=None, words=False):
        assert audio[:4] == b"RIFF"
        self.calls += 1
        text = self.heard.pop(0)
        timed = tuple(TranscribedWord(w, round(0.3 * i, 3), round(0.3 * (i + 1), 3)) for i, w in enumerate(text.split()))
        return Transcript(text, timed if words else ())


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Speech, its cache, the check records and the takes kept by the words they passed on, all in memory."""
    state = SimpleNamespace(spoken=[], speeds=[], cache={}, blobs={}, records={}, passed={}, workdir=str(tmp_path))

    def keep(name, out_path):
        """The take as the cache keeps it: its bytes, given back as they were stored."""
        if name not in state.blobs:
            _wav(out_path, state.cache[name])
            state.blobs[name] = Path(out_path).read_bytes()
        else:
            Path(out_path).write_bytes(state.blobs[name])

    def key(name, voice_name, text, previous=None, following=None):
        """A narration as the cache tells it: its voice, its words and, when it has any, its neighbours."""
        return f"{name}|{voice_name}|{text}" + (f"|{previous}|{following}" if previous or following else "")

    def digest(target, line, voiceover, _lexicon=None, *, level="", reframe=()):
        return key(target.name, voiceover.voice, line.text, line.previous_text, line.next_text)

    def said(target, line, voiceover, _lexicon=None):
        return f"said|{target.name}|{voiceover.voice}|{line.text}"

    def rendition(_target, line, _voiceover, _lexicon=None, *, level="", reframe=()):
        return lexicon.Rendition(line.text)

    async def synthesize(target, *, text, voice, out_path, previous_text=None, next_text=None, speed=1.0, **_kwargs):
        """The cached take: spoken on a miss, kept under the narration's own name, never written over."""
        name = f"cache/{key(target.name, voice, text, previous_text, next_text)}.wav"
        state.spoken.append((target.name, voice, False))
        state.speeds.append(speed)
        if name not in state.cache:
            state.cache[name] = 1.0 + 0.1 * len(state.spoken)
        keep(name, out_path)
        return voice or "", []

    async def speak_take(target, *, text, voice, out_path, previous_text=None, next_text=None, speed=1.0, **_kwargs):
        """A new take, kept beside the cached one under a name of its own."""
        state.spoken.append((target.name, voice, True))
        state.speeds.append(speed)
        name = f"cache/{key(target.name, voice, text, previous_text, next_text)}.take-{len(state.spoken)}.wav"
        state.cache[name] = 1.0 + 0.1 * len(state.spoken)
        keep(name, out_path)
        return voice or "", [], name

    async def fetch(name, out_path):
        if name not in state.cache:
            return False
        keep(name, out_path)
        return True

    async def load(digest_):
        return state.records.get(digest_)

    async def save(digest_, record):
        state.records[digest_] = {"version": 1, "digest": digest_, **record}

    async def passed_take(said_, scene_id):
        kept = state.passed.get(said_, [])
        return max(kept, key=lambda r: (r.get("scene_id") == scene_id, r["checked_at"]), default=None)

    async def keep_passed_take(said_, record):
        state.passed.setdefault(said_, []).append({"said": said_, **record})

    monkeypatch.setattr(voice, "digest_of", digest)
    monkeypatch.setattr(voice, "text_digest_of", said)
    monkeypatch.setattr(voice, "rendition_of", rendition)
    monkeypatch.setattr(voice, "object_name", lambda digest_: f"cache/{digest_}.wav")
    monkeypatch.setattr(voice, "synthesize", synthesize)
    monkeypatch.setattr(voice, "speak_take", speak_take)
    monkeypatch.setattr(voice, "fetch", fetch)
    monkeypatch.setattr(voice_check, "load_record", load)
    monkeypatch.setattr(voice_check, "save_record", save)
    monkeypatch.setattr(voice_check, "passed_take", passed_take)
    monkeypatch.setattr(voice_check, "keep_passed_take", keep_passed_take)
    return state


LINE = voice.Line("s1", "Every alert is triaged by an agent first.", None, None)
GOOD = "every alert is triaged by an agent first"
BAD = "every alert is tried by agents"


class _Model:
    """The voices a speech model lists, as its provider describes them (``speech.Voice``)."""

    def __init__(self, voices: list[Voice]) -> None:
        self.voices = voices

    async def list_voices(self, model=None):
        return list(self.voices)


#: Cedar is listed before Coral, but Coral shares more of Marin's traits.
VOICES = [
    Voice("marin", "Marin", "", "American, female, warm"),
    Voice("cedar", "Cedar", "", "American, male, warm"),
    Voice("coral", "Coral", "", "american, female, warm, bright"),
]


def _speaker(
    heard: list[str] | None,
    *,
    voice_name: str | None = "marin",
    origin: str | None = None,
    voices: list[Voice] = VOICES,
    speed: float = 1.0,
) -> voice_check.Speaker:
    return voice_check.Speaker(
        SimpleNamespace(name="video", voice=None, model="tts-test", provider=_Model(voices)),
        Voiceover(voice=voice_name, speed=speed),
        _Transcriber(heard) if heard is not None else None,
        None if heard is not None else "An OpenAI provider available to you is required for speech-to-text.",
        origin=origin,
    )


def test_a_narration_that_passes_is_placed_and_remembered(world):
    speaker = _speaker([GOOD])
    spoken = asyncio.run(voice_check.speak_checked(speaker, LINE, workdir=world.workdir))
    assert spoken.status == voice_check.PASSED and spoken.duration == pytest.approx(1.1)
    assert world.spoken == [("video", "marin", False)]
    record = world.records["video|marin|" + LINE.text]
    assert record["status"] == "passed" and record["placed"] == f"cache/video|marin|{LINE.text}.wav"
    assert record["duration"] == pytest.approx(1.1) and record["transcript"] == GOOD
    # Heard once: the next render places it from the cache without transcribing it again.
    again = _speaker([])
    replay = asyncio.run(voice_check.speak_checked(again, LINE, workdir=world.workdir))
    assert replay.status == voice_check.PASSED and again.transcriber.calls == 0 and len(world.spoken) == 1


def test_the_take_placed_keeps_the_words_heard_in_it_for_the_render_check(world):
    """The rendered-audio check reads them where the stem places the take, instead of transcribing it again."""
    spoken = asyncio.run(voice_check.speak_checked(_speaker([GOOD]), LINE, workdir=world.workdir))
    assert [w for w, _s, _e in spoken.heard_words] == GOOD.split() and spoken.heard_words[1] == ("alert", 0.3, 0.6)
    record = world.records["video|marin|" + LINE.text]
    assert record["words"][0] == ["every", 0.0, 0.3]
    # Kept with the digest of the very sound they were heard in.
    assert record["words_digest"] == spoken.heard_digest == voice_check.take_digest(spoken.path)
    # Placed from the cache by the next render: the words come with it, still never transcribed again.
    replay = asyncio.run(voice_check.speak_checked(_speaker([]), LINE, workdir=world.workdir))
    assert replay.heard_words == spoken.heard_words and replay.heard_digest == spoken.heard_digest
    # Bytes fetched that are not the ones heard: the words are not theirs.
    record["words_digest"] = "0" * 64
    other = asyncio.run(voice_check.speak_checked(_speaker([]), LINE, workdir=world.workdir))
    assert other.status == voice_check.PASSED and other.heard_words is None
    # A record kept before the words were: the render check transcribes that narration from the stem.
    world.records["video|marin|" + LINE.text]["words"] = None
    older = asyncio.run(voice_check.speak_checked(_speaker([]), LINE, workdir=world.workdir))
    assert older.status == voice_check.PASSED and older.heard_words is None


def test_a_failed_narration_is_spoken_again_then_slower_then_by_the_closest_voice_of_its_model(world):
    """Never the platform's default voice - another voice of the same speech model, said with why."""
    speaker = _speaker([BAD, BAD, BAD, GOOD])
    spoken = asyncio.run(voice_check.speak_checked(speaker, LINE, workdir=world.workdir))
    # The cached take, a new take of the same voice (never over the cached one), the same slower, then Coral - which
    # shares Marin's three traits - of the same model, never another provider's voice.
    assert world.spoken == [
        ("video", "marin", False),
        ("video", "marin", True),
        ("video", "marin", True),
        ("video", "coral", False),
    ]
    assert world.speeds == [1.0, 1.0, 0.85, 1.0]
    assert [a.kind for a in spoken.attempts] == ["same", "again", "slower", "closest"]
    assert spoken.attempts[2].speed == 0.85 and spoken.attempts[3].voice == "Coral"
    assert spoken.status == voice_check.PASSED and spoken.fallback and spoken.path == spoken.attempts[3].path
    change = spoken.voice_change
    assert (change.video_voice, change.voice) == ("Marin", "Coral")
    assert "differ from the narration" in change.reason and "Coral" not in change.reason
    assert change.closeness == "the voice of the same speech model sharing the most with Marin (american, female, warm)"
    sentence = change.sentence("s1")
    assert sentence.startswith("its narration is spoken by Coral, not the video's voice Marin: Marin failed")
    assert "render_video_scene(scene='s1')" in sentence and "voiceover.voice" in sentence
    assert spoken.summary()["voice_change"] == change.as_dict() and spoken.summary()["fallback"]
    # That voice's own pass, and a failure of the video's - never a pass it would place again.
    assert world.records["video|coral|" + LINE.text]["status"] == "passed"
    assert not world.records["video|coral|" + LINE.text]["fallback"]
    record = world.records["video|marin|" + LINE.text]
    assert record["status"] == "failed" and record["fallback"] and record["fallback_voice"] == "Coral"
    assert record["placed"] is None and voice_check.failed(record) and record["voice_change"] == change.as_dict()
    assert record["reason"].endswith("the closest voice of its speech model (Coral) passed in its place")
    # Nor kept as a take of the video's voice.
    assert world.passed == {}


def test_a_narration_passing_slower_keeps_the_video_s_voice_and_says_so(world):
    spoken = asyncio.run(voice_check.speak_checked(_speaker([BAD, BAD, GOOD]), LINE, workdir=world.workdir))
    assert spoken.status == voice_check.PASSED and not spoken.fallback and spoken.voice_change is None
    assert [a.kind for a in spoken.attempts] == ["same", "again", "slower"] and world.speeds[-1] == 0.85
    assert any("spoken slower (0.85, the video's speed is 1)" in note for note in spoken.notes)
    record = world.records["video|marin|" + LINE.text]
    assert record["speed"] == 0.85 and record["placed"] == spoken.attempts[2].object_name
    # A take of the video's own voice: kept by its words like any other.
    assert list(world.passed) == ["said|video|marin|" + LINE.text]
    replay = asyncio.run(voice_check.speak_checked(_speaker([]), LINE, workdir=world.workdir))
    assert replay.attempts[0].speed == 0.85 and any("spoken slower" in note for note in replay.notes)


def test_a_voice_already_at_the_slowest_speed_skips_the_slower_take(world):
    spoken = asyncio.run(voice_check.speak_checked(_speaker([BAD, BAD, GOOD], speed=0.7), LINE, workdir=world.workdir))
    assert [a.kind for a in spoken.attempts] == ["same", "again", "closest"] and spoken.voice_change is not None


def test_a_model_listing_no_other_voice_leaves_the_narration_failed_and_says_why(world):
    spoken = asyncio.run(voice_check.speak_checked(_speaker([BAD, BAD, BAD], voices=VOICES[:1]), LINE, workdir=world.workdir))
    assert spoken.status == voice_check.FAILED and not spoken.fallback and spoken.voice_change is None
    assert spoken.reason.endswith("no other voice tried: tts-test lists no other voice")


def test_the_closest_voice_shares_the_most_traits_and_the_provider_order_breaks_a_tie():
    def target(voices, voice_name=None):
        return SimpleNamespace(provider=_Model(voices), model="tts-test", voice=voice_name)

    found = asyncio.run(voice.closest_voice(target(VOICES), "marin"))
    assert (found.voice, found.shared, found.of) == ("coral", ("american", "female", "warm"), "Marin")
    # A tie goes to the voice the provider lists first.
    tied = [VOICES[0], Voice("ash", "Ash", "", "american, male"), Voice("ballad", "Ballad", "", "american, male")]
    assert asyncio.run(voice.closest_voice(target(tied), "Marin")).voice == "ash"
    # A model describing no voice: the first other voice it lists, said as such.
    bare = [Voice("marin", "Marin"), Voice("cedar", "Cedar"), Voice("ash", "Ash")]
    plain = asyncio.run(voice.closest_voice(target(bare), "marin"))
    assert plain.voice == "cedar" and plain.why() == "the first other voice its speech model lists, which describes neither it nor Marin"
    # No voice named: the target's, else the model's first, is the video's.
    assert asyncio.run(voice.closest_voice(target(VOICES, "cedar"), None)).of == "Cedar"
    assert asyncio.run(voice.closest_voice(target(VOICES), None)).of == "Marin"
    # An id unlike its name is named with both.
    named = [Voice("v1", "Rachel", "", "american, female"), Voice("v2", "Bella", "", "american, female")]
    assert asyncio.run(voice.closest_voice(target(named), "v1")).label == "Bella (v2)"
    assert asyncio.run(voice.closest_voice(target([VOICES[0]]), "marin")) is None


def test_the_slower_speed_is_a_share_of_the_video_s_never_below_the_slowest():
    assert voice.slower_speed(1.0) == 0.85 and voice.slower_speed(1.2) == 1.02
    assert voice.slower_speed(0.75) == 0.7 and voice.slower_speed(0.7) is None


def test_after_another_voice_said_it_the_videos_own_voice_is_tried_again(world):
    """The next render speaks a new take of the video's voice first, and places it once it passes."""
    asyncio.run(voice_check.speak_checked(_speaker([BAD, BAD, BAD, GOOD]), LINE, workdir=world.workdir))
    world.spoken.clear()
    again = asyncio.run(voice_check.speak_checked(_speaker([GOOD]), LINE, workdir=world.workdir))
    assert again.status == voice_check.PASSED and not again.fallback and world.spoken == [("video", "marin", True)]
    assert world.records["video|marin|" + LINE.text]["status"] == "passed"
    # A record of the rules before, a pass said by the default voice, is a failure of the video's voice all the same.
    world.records["video|marin|" + LINE.text] = {"status": "passed", "placed": "cache/x.wav", "fallback": True}
    # The editor's preview never plays the other voice's take.
    voice._PLACED.clear()
    assert asyncio.run(voice.placed("video|marin|" + LINE.text)) == f"cache/video|marin|{LINE.text}.wav"
    world.spoken.clear()
    asyncio.run(voice_check.speak_checked(_speaker([GOOD]), LINE, workdir=world.workdir))
    assert world.spoken == [("video", "marin", True)]


def test_a_take_that_passed_on_the_same_words_is_placed_before_another_voice(world):
    """Render 2527a4b6: the preview passed the narration; the whole render, its neighbours read otherwise,
    spoke it anew, failed, and handed it to the platform's voice. The preview's take of the video's voice is placed."""
    preview = _speaker([GOOD], origin="render c1f90ad0 (a preview of scene s1)")
    asyncio.run(voice_check.speak_checked(preview, LINE, workdir=world.workdir))
    first = world.records["video|marin|" + LINE.text]["placed"]
    (kept,) = world.passed["said|video|marin|" + LINE.text]
    assert kept["placed"] == first and kept["scene_id"] == "s1" and kept["origin"] == "render c1f90ad0 (a preview of scene s1)"
    wider = voice.Line(LINE.scene_id, LINE.text, "The scene before it says this.", None)
    whole = _speaker([BAD])
    spoken = asyncio.run(voice_check.speak_checked(whole, wider, workdir=world.workdir))
    assert spoken.status == voice_check.PASSED and not spoken.fallback
    assert [a.kind for a in spoken.attempts] == ["same", "earlier"]
    # Heard once in the preview: placed as heard there - never transcribed again, no other take, no other voice.
    assert whole.transcriber.calls == 1 and world.spoken == [("video", "marin", False), ("video", "marin", False)]
    assert spoken.attempts[1].object_name == first and spoken.attempts[1].origin == kept["origin"]
    assert [w for w, _s, _e in spoken.heard_words] == GOOD.split()
    assert any("passed its check on the same words in render c1f90ad0 (a preview of scene s1)" in n for n in spoken.notes)
    record = world.records[f"video|marin|{LINE.text}|The scene before it says this.|None"]
    assert record["status"] == "passed" and record["placed"] == first
    # The next render places it from its own record.
    replay = asyncio.run(voice_check.speak_checked(_speaker([]), wider, workdir=world.workdir))
    assert replay.status == voice_check.PASSED and replay.attempts[0].object_name == first


def test_a_take_of_another_voice_is_never_placed_as_the_videos(world):
    asyncio.run(voice_check.speak_checked(_speaker([GOOD], voice_name="cedar"), LINE, workdir=world.workdir))
    spoken = asyncio.run(voice_check.speak_checked(_speaker([BAD, BAD, BAD, GOOD]), LINE, workdir=world.workdir))
    assert [a.kind for a in spoken.attempts] == ["same", "again", "slower", "closest"] and spoken.fallback


def test_the_same_voice_spoken_again_can_pass_and_its_take_is_the_one_placed(world):
    spoken = asyncio.run(voice_check.speak_checked(_speaker([BAD, GOOD]), LINE, workdir=world.workdir))
    assert spoken.status == voice_check.PASSED and not spoken.fallback and len(spoken.attempts) == 2
    placed = world.records["video|marin|" + LINE.text]["placed"]
    assert placed == spoken.attempts[1].object_name and ".take-" in placed
    # The next render places that take, never the first one that failed.
    replay = asyncio.run(voice_check.speak_checked(_speaker([]), LINE, workdir=world.workdir))
    assert replay.status == voice_check.PASSED and replay.attempts[0].object_name == placed


def test_a_narration_failing_every_attempt_is_failed_with_its_closest_take(world):
    closest = "every alert is triaged by an agent"
    # The closest voice comes as near, yet a failed narration is never placed in another voice.
    speaker = _speaker([BAD, closest, "nothing like it", "every alert is triaged by an agent firs"])
    spoken = asyncio.run(voice_check.speak_checked(speaker, LINE, workdir=world.workdir))
    assert spoken.status == voice_check.FAILED and spoken.path == spoken.attempts[1].path and not spoken.fallback
    assert "differ from the narration" in spoken.reason
    assert spoken.attempts[3].tried() == "no lexicon term (the closest voice, Coral)"
    record = world.records["video|marin|" + LINE.text]
    assert record["status"] == "failed" and len(record["attempts"]) == 4 and record["placed"] is None
    # The cached take of a narration that failed is known bad: the next render speaks a new take first.
    world.spoken.clear()
    asyncio.run(voice_check.speak_checked(_speaker([GOOD]), LINE, workdir=world.workdir))
    assert world.spoken[0] == ("video", "marin", True)


def test_a_narration_that_cannot_be_spoken_at_all_is_failed_without_a_take(world, monkeypatch):
    async def refuse(*_args, **_kwargs):
        raise RuntimeError("quota exceeded")

    monkeypatch.setattr(voice, "synthesize", refuse)
    monkeypatch.setattr(voice, "speak_take", refuse)
    spoken = asyncio.run(voice_check.speak_checked(_speaker([]), LINE, workdir=world.workdir))
    assert spoken.status == voice_check.FAILED and spoken.path is None and "quota exceeded" in spoken.reason


def test_without_speech_to_text_a_narration_is_placed_unchecked(world):
    spoken = asyncio.run(voice_check.speak_checked(_speaker(None), LINE, workdir=world.workdir))
    assert spoken.status == voice_check.UNCHECKED and spoken.path and "speech-to-text" in spoken.reason
    assert world.records == {}


def test_a_failing_speech_to_text_leaves_the_narration_unchecked(world):
    speaker = _speaker([GOOD])

    async def broken(*_args, **_kwargs):
        raise ConnectionError("unreachable")

    speaker.transcriber.transcribe = broken
    spoken = asyncio.run(voice_check.speak_checked(speaker, LINE, workdir=world.workdir))
    assert spoken.status == voice_check.UNCHECKED and "unreachable" in spoken.reason and world.records == {}


def test_records_are_added_never_written_over_and_the_newest_decides(monkeypatch):
    from app.services.file_storage import file_storage

    objects: dict[str, bytes] = {}

    async def upload(data, filename, content_type=None, prefix=""):
        name = f"{prefix}/{len(objects)}-{filename}"
        assert name not in objects
        objects[name] = data
        return name

    async def names(prefix, *, recursive=True):
        return [name for name in objects if name.startswith(prefix)]

    async def read(name, offset, length):
        return objects[name][offset : offset + length]

    monkeypatch.setattr(file_storage, "upload", upload)
    monkeypatch.setattr(file_storage, "list_object_names", names)
    monkeypatch.setattr(file_storage, "read_range", read)
    asyncio.run(voice_check.save_record("d1", {"status": "failed", "checked_at": "2026-10-20T00:00:00+00:00"}))
    asyncio.run(
        voice_check.save_record("d1", {"status": "passed", "placed": "v/d1.take-1.wav", "checked_at": "2026-10-20T00:05:00+00:00"})
    )
    assert len(objects) == 2 and all("/checks/d1/" in name for name in objects)
    assert asyncio.run(voice_check.load_record("d1"))["status"] == "passed"
    assert asyncio.run(voice_check.load_record("d2")) is None
    # The editor's preview plays the take a render places.
    voice._PLACED.clear()
    assert asyncio.run(voice.placed("d1")) == "v/d1.take-1.wav"
    assert asyncio.run(voice.placed("d2")) == voice.object_name("d2")


def test_the_gate_reads_the_narrations_that_failed(world):
    from app.services.video_studio.spec import parse_video_spec

    spec = parse_video_spec(
        {
            "title": "Gate",
            "freestyle": True,
            "voiceover": {"voice": "marin"},
            "scenes": [{"id": "s1", "type": "title", "title": "Hi", "narration": LINE.text}],
        }
    )
    world.records["video|marin|" + LINE.text] = {"status": "failed", "reason": "loop"}
    failed = asyncio.run(voice_check.failed_narrations(SimpleNamespace(name="video"), spec))
    assert [scene for scene, _record in failed] == ["s1"]
    # Said by the platform's default voice, as the rules before recorded it: failed in its own.
    world.records["video|marin|" + LINE.text] = {"status": "passed", "placed": "cache/p.wav", "fallback": True}
    assert [s for s, _r in asyncio.run(voice_check.failed_narrations(SimpleNamespace(name="video"), spec))] == ["s1"]
    world.records["video|marin|" + LINE.text] = {"status": "passed", "placed": "cache/p.wav", "fallback": False}
    assert asyncio.run(voice_check.failed_narrations(SimpleNamespace(name="video"), spec)) == []


def _storage(monkeypatch) -> dict[str, bytes]:
    """The object store, in memory: what records are written to and read from."""
    from app.services.file_storage import file_storage

    objects: dict[str, bytes] = {}

    async def upload(data, filename, content_type=None, prefix=""):
        name = f"{prefix}/{len(objects)}-{filename}"
        objects[name] = data
        return name

    async def names(prefix, *, recursive=True):
        return [name for name in objects if name.startswith(prefix)]

    async def read(name, offset, length):
        return objects[name][offset : offset + length]

    monkeypatch.setattr(file_storage, "upload", upload)
    monkeypatch.setattr(file_storage, "list_object_names", names)
    monkeypatch.setattr(file_storage, "read_range", read)
    return objects


def test_a_pass_under_rules_that_refused_less_is_heard_again_and_one_under_these_stands(monkeypatch):
    """Version 6 refuses a term read letter by letter that passed before: a narration that passed under version 5 is
    heard again, as one that failed is; a record of this version stands, passed or failed."""
    _storage(monkeypatch)
    prefix = voice_check._record_prefix

    def kept(digest: str, version: int, status: str) -> None:
        body = {"version": version, "status": status, "checked_at": "2026-10-20T00:00:00+00:00"}
        asyncio.run(voice_check._add(prefix(digest), body))

    kept("v5-passed", 5, "passed")
    kept("v4-failed", 4, "failed")
    kept("v3-passed", 3, "passed")
    kept("now-passed", voice_check.RECORD_VERSION, "passed")
    kept("now-failed", voice_check.RECORD_VERSION, "failed")
    assert voice_check.PASSED_SINCE == voice_check.RECORD_VERSION == 6
    assert asyncio.run(voice_check.load_record("v5-passed")) is None
    assert asyncio.run(voice_check.load_record("v4-failed")) is None
    assert asyncio.run(voice_check.load_record("v3-passed")) is None
    assert asyncio.run(voice_check.load_record("now-passed"))["status"] == "passed"
    assert asyncio.run(voice_check.load_record("now-failed"))["status"] == "failed"


def test_the_takes_that_passed_are_kept_by_their_words_the_scenes_own_first(monkeypatch):
    objects = _storage(monkeypatch)

    def passed(scene: str, at: str) -> dict:
        return {"status": "passed", "placed": f"v/{scene}.wav", "scene_id": scene, "checked_at": at}

    asyncio.run(voice_check.keep_passed_take("w1", passed("s1", "2026-10-20T08:00:00+00:00")))
    asyncio.run(voice_check.keep_passed_take("w1", passed("s2", "2026-10-20T09:00:00+00:00")))
    assert all("/passed/w1/" in name for name in objects)
    assert asyncio.run(voice_check.passed_take("w1", "s1"))["placed"] == "v/s1.wav"
    assert asyncio.run(voice_check.passed_take("w1", "s9"))["placed"] == "v/s2.wav"
    assert asyncio.run(voice_check.passed_take("w2", "s1")) is None


def test_the_words_digest_follows_the_voice_and_the_words_never_the_neighbours():
    target = SimpleNamespace(config=SimpleNamespace(provider="openai", id="c1"), model="tts-1-hd", voice=None)
    fable = Voiceover(voice="fable")
    line = voice.Line("s1", "Moscone Center.", None, None)
    base = voice.text_digest_of(target, line, fable)
    assert base == voice.text_digest_of(target, voice.Line("s1", "Moscone Center.", "Before it.", "After it."), fable)
    assert base != voice.text_digest_of(target, voice.Line("s1", "Moscone Centre.", None, None), fable)
    assert base != voice.text_digest_of(target, line, Voiceover(voice="alloy"))
    assert base != voice.text_digest_of(target, line, Voiceover(voice="fable", speed=1.2))
    assert base != voice.text_digest_of(target, line, fable, lexicon.merge((), [SimpleNamespace(text="Moscone", say="Mos-coh-nee")]))
    # An entry the narration does not write changes nothing.
    assert base == voice.text_digest_of(target, line, fable, lexicon.merge((), [SimpleNamespace(text="OpenCTI", say="Open C-T-I")]))
    # Whereas the narration's cache digest follows its neighbours.
    assert voice.digest_of(target, line, fable) != voice.digest_of(target, voice.Line("s1", line.text, "Before it.", None), fable)
