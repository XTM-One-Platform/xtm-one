"""A narration's transcript check, heard in preview as the render hears it (dev:preview-voiceover-transcript-match).

- ``voice_check.compare`` keeps the word-level diff behind its word error rate (``pronunciation_check.word_diff``):
  each word of the narration heard as written or otherwise, and what was heard instead; a failure's reason lists the
  words heard otherwise, and a take kept in a record keeps them for the fallback note.
- ``preview_voiceover`` runs that very check on its line, a line with no lexicon term too: the share of words heard
  as written, the verdict, the line word by word; ``transcript_only`` leaves the audio out. The share it reports is
  the one the render's voice check (``voice_check._heard``) computes on the same transcript: both call the one
  ``voice_check.hear_back``, and the answer says what a render adds (the neighbours as context).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.llm.transcription import TranscribedWord, Transcript
from app.models.user import User
from app.services.video_studio import lexicon, pronunciation_check, voice, voice_check
from app.services.video_studio.spec import Voiceover

#: A homophone at the start of a sentence: "Their" heard "There", one word of eleven (9 %), under the threshold.
TEXT = "Their platform hunts threats every day, and every analyst sees it."
HEARD = "There platform hunts threats every day and every analyst sees it."
#: Six words of twelve heard otherwise: over the threshold.
FAILING_TEXT = "Every alert is triaged by an agent before an analyst opens it."
FAILING_HEARD = "every alert is triaged by agents before analysts open now"


def _timed(text: str) -> tuple[TranscribedWord, ...]:
    return tuple(TranscribedWord(w, round(0.3 * i, 3), round(0.3 * (i + 1), 3)) for i, w in enumerate(text.split()))


# ── The diff behind the word error rate ───────────────────────────────────


def test_the_diff_flags_a_homophone_that_starts_a_sentence_and_the_share_is_the_gate_s():
    check = voice_check.compare(TEXT, HEARD)
    assert check.passed and check.wer == pytest.approx(1 / 11, abs=1e-4)
    assert check.heard_share == pytest.approx(1 - check.wer)
    assert [w.as_dict() for w in check.otherwise()] == [{"word": "Their", "heard": False, "said": "There"}]
    assert pronunciation_check.diff_line(check.words) == (
        "['Their' heard as 'There'] platform hunts threats every day and every analyst sees it"
    )
    # Every word of the narration is in the diff, in its order, once.
    assert [w.written for w in check.words if w.written] == [p.text for p in pronunciation_check.pieces_of(TEXT)]


def test_a_word_left_out_and_a_word_added_are_both_said():
    check = voice_check.compare("We hunt the threats every day.", "we hunt the threats every single day")
    assert [w.text() for w in check.otherwise()] == ["'single' added"]
    check = voice_check.compare("We hunt the threats every day.", "we hunt threats every day")
    assert [w.text() for w in check.otherwise()] == ["'the' not heard"]


def test_a_lexicon_term_and_a_number_stand_as_one_word_of_the_diff():
    words = lexicon.merge((), [SimpleNamespace(text="OpenCTI", say="Open C-T-I")])
    check = voice_check.compare("OpenCTI hunts 23 threats.", "open c t i hunts twenty-three threads", lexicon=words)
    assert [(t.term, t.passed) for t in check.terms] == [("OpenCTI", True)]
    assert pronunciation_check.diff_line(check.words) == "OpenCTI hunts 23 ['threats' heard as 'threads']"


def test_a_failed_check_names_the_words_heard_otherwise():
    check = voice_check.compare(FAILING_TEXT, FAILING_HEARD)
    assert not check.passed
    assert check.reason() == (
        "50% of its words differ from the narration (at most 10%): 'an' not heard, 'agent' heard as 'agents', "
        "'an' not heard, 'analyst' heard as 'analysts', 'opens' heard as 'open', 'it' heard as 'now'"
    )


def test_a_take_kept_keeps_its_words_heard_otherwise_and_the_fallback_note_lists_them():
    failed = voice_check.Attempt("fable", "same", "/s1-0.wav", 2.0, voice_check.compare(FAILING_TEXT, FAILING_HEARD))
    kept = failed.as_dict()
    assert kept["otherwise"][:2] == [
        {"word": "an", "heard": False, "said": ""},
        {"word": "agent", "heard": False, "said": "agents"},
    ]
    record = {
        "status": voice_check.PASSED,
        "speed": 0.85,
        "attempts": [kept, {"kind": "slower", "speed": 0.85, "terms": [], "wer": 0.0}],
    }
    take = voice_check.FallbackTake.of_record("s1", record, 1.0)
    assert take.why() == (
        "the video's voice said 50% of its words otherwise ['an' not heard, 'agent' heard as 'agents', 'an' not "
        "heard, 'analyst' heard as 'analysts', 'opens' heard as 'open', 'it' heard as 'now'] at its speed 1"
    )
    # A record kept before the diff was: said as before.
    del record["attempts"][0]["otherwise"]
    assert voice_check.FallbackTake.of_record("s1", record, 1.0).why() == (
        "the video's voice said 50% of its words otherwise at its speed 1"
    )


def test_a_check_replayed_from_a_record_has_no_diff_and_its_reason_is_unchanged():
    replayed = voice_check.TranscriptCheck(0.18, "heard")
    assert replayed.words == () and replayed.otherwise() == []
    assert replayed.reason() == "18% of its words differ from the narration (at most 10%)"


# ── preview_voiceover ─────────────────────────────────────────────────────


def _preview(monkeypatch, tmp_path, arguments: dict, heard: str):
    from app.llm import transcription
    from app.services.builtin_tools import video_studio as tool
    from app.services.video_studio import assets, encoder, lexicon_store
    from app.tools.base import ToolCall

    maker = User(id=uuid.uuid4(), email="maker@example.com", is_active=True)
    target = SimpleNamespace(
        model="gpt-4o-mini-tts",
        config=SimpleNamespace(id=uuid.uuid4(), provider="filigran_gateway", display_name="Filigran AI Gateway"),
        voice="marin",
    )
    stored: list = []

    @asynccontextmanager
    async def session():
        yield object()

    async def find_user(_db, _user_id):
        return maker

    async def resolve(_db, _user_id, _wanted):
        return target

    async def synthesize(_target, *, voice=None, out_path, **_kwargs):
        Path(out_path).write_bytes(b"RIFF")
        return voice or "marin", []

    async def probe(_path):
        return SimpleNamespace(duration=3.1)

    async def store_path(_path, **_kwargs):
        stored.append(_path)
        return SimpleNamespace(
            id=uuid.uuid4(), filename="voiceover-preview.wav", content_type="audio/wav", size=4, s3_object_name="v.wav"
        )

    async def platform_lexicon(_db, wanted):
        return lexicon.merge((), wanted.pronunciations or ())

    async def resolve_transcriber(_db, _uid):
        return SimpleNamespace(model="scripted-stt")

    async def transcribe(_transcriber, _path, *, words=False):
        return Transcript(heard, _timed(heard) if words else ())

    monkeypatch.setattr(tool, "async_session", session)
    monkeypatch.setattr(tool, "_user", find_user)
    monkeypatch.setattr(voice, "resolve", resolve)
    monkeypatch.setattr(voice, "synthesize", synthesize)
    monkeypatch.setattr(encoder, "probe", probe)
    monkeypatch.setattr(assets, "store_path", store_path)
    monkeypatch.setattr(lexicon_store, "for_voiceover", platform_lexicon)
    monkeypatch.setattr(transcription, "resolve_transcriber", resolve_transcriber)
    monkeypatch.setattr(voice_check, "transcribe_wav", transcribe)
    monkeypatch.setattr(voice_check, "pauses_of_wav", lambda _path: [])
    monkeypatch.setattr(voice_check, "nuclei_of_wav", lambda _path: [])
    _definitions, executor = tool.build_video_studio_tools(None, str(maker.id))
    result = asyncio.run(executor.execute_tool(ToolCall(id="c1", name="preview_voiceover", arguments=arguments)))
    assert not result.is_error, result.content
    return result, stored


_SHARE = re.compile(r"Transcript check \(render_video's voice check[^)]*\): (\d+)% of its words heard as written")


def _share(content: str) -> int:
    found = _SHARE.search(content)
    assert found, content
    return int(found.group(1))


def test_a_preview_with_no_lexicon_term_says_the_share_heard_and_the_diff(monkeypatch, tmp_path):
    result, stored = _preview(monkeypatch, tmp_path, {"text": TEXT}, HEARD)
    content = result.content
    assert _share(content) == 91 and "(passes from 90%, word error rate 9%); it passes." in content
    assert (
        "Word by word, each word heard otherwise in brackets: ['Their' heard as 'There'] platform hunts threats every "
        "day and every analyst sees it"
    ) in content
    # No lexicon term: no term line, the audio attached as before.
    assert "Lexicon terms said with" not in content and "Heard back by the speech-to-text" not in content
    assert len(stored) == 1 and result.files


def test_a_failing_preview_says_it_fails_and_why(monkeypatch, tmp_path):
    result, _stored = _preview(monkeypatch, tmp_path, {"text": FAILING_TEXT}, FAILING_HEARD)
    assert _share(result.content) == 50
    assert "it FAILS: 50% of its words differ from the narration (at most 10%): 'an' not heard" in result.content


def test_transcript_only_leaves_the_audio_out(monkeypatch, tmp_path):
    result, stored = _preview(monkeypatch, tmp_path, {"text": TEXT, "transcript_only": True}, HEARD)
    assert stored == [] and not result.files
    assert "the audio not attached (transcript_only)" in result.content and _share(result.content) == 91


def test_the_lexicon_lines_are_unchanged_beside_the_transcript_check(monkeypatch, tmp_path):
    entries = [{"text": "OpenCTI", "say": "Open C-T-I"}]
    result, _stored = _preview(
        monkeypatch, tmp_path, {"text": "OpenCTI hunts threats.", "pronunciations": entries}, "open c t i hunts threats"
    )
    content = result.content
    assert "Lexicon terms said with" in content
    assert "Heard back by the speech-to-text:\n- OpenCTI: heard \"open c t i\" (accepted" in content
    assert _share(content) == 100 and "Every word heard as written." in content


def test_the_preview_s_share_is_the_one_the_render_s_voice_check_computes(monkeypatch, tmp_path):
    """Same text, same transcript, same lexicon: the share preview_voiceover reports is 1 - the WER the render's
    voice check (``voice_check._heard``, what ``speak_checked`` runs on each take) records, to the point."""
    result, _stored = _preview(monkeypatch, tmp_path, {"text": FAILING_TEXT}, FAILING_HEARD)
    take = tmp_path / "take.wav"
    take.write_bytes(b"RIFF")
    speaker = voice_check.Speaker(SimpleNamespace(), Voiceover(), SimpleNamespace(model="scripted-stt"))
    attempt = voice_check.Attempt("marin", "same", str(take))
    asyncio.run(voice_check._heard(speaker, voice.Line("s1", FAILING_TEXT, None, None), attempt))
    assert abs(_share(result.content) - round(100 * (1 - attempt.check.wer))) <= 1
    assert not attempt.check.passed
    assert [w.as_dict() for w in attempt.check.otherwise()][:1] == [{"word": "an", "heard": False, "said": ""}]


def test_preview_and_render_share_one_check_function_on_a_recorded_transcription(monkeypatch, tmp_path):
    """A recorded transcription, replayed to both: preview_voiceover and the render's voice check each call
    ``voice_check.hear_back`` once, with the same text and lexicon, and report the same share - 100% here."""
    recorded = "Their platform hunts threats every day and every analyst sees it"
    calls: list = []
    real = voice_check.hear_back

    async def counted(transcriber, text, path, lexicon=None):
        calls.append((text, lexicon))
        return await real(transcriber, text, path, lexicon)

    monkeypatch.setattr(voice_check, "hear_back", counted)
    result, _stored = _preview(monkeypatch, tmp_path, {"text": TEXT}, recorded)
    assert _share(result.content) == 100 and "it passes" in result.content
    assert "What a render adds: it speaks each narration at the video's speed with the narrations around it" in (
        result.content
    )
    take = tmp_path / "take.wav"
    take.write_bytes(b"RIFF")
    speaker = voice_check.Speaker(SimpleNamespace(), Voiceover(), SimpleNamespace(model="scripted-stt"))
    attempt = voice_check.Attempt("marin", "same", str(take))
    asyncio.run(voice_check._heard(speaker, voice.Line("s1", TEXT, None, None), attempt))
    assert attempt.check.passed and round(100 * attempt.check.heard_share) == 100
    assert [text for text, _lexicon in calls] == [TEXT, TEXT]
