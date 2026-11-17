"""A voice note's transcript is judged before a turn trusts it (#9920).

A long note whose audio was cut, whose speech the model mostly lost, or
whose words were decoded unsure reaches the agent as a low-confidence line
with the reason, never as plausible text; the language heard is stated; a
segment written where nobody speaks is dropped. No user content: synthetic
words only.
"""

from __future__ import annotations

import uuid

from app.llm import transcription
from app.llm.transcription import TranscribedSegment, Transcript
from app.services import whatsapp_gateway_inbound as gi
from app.services import whatsapp_inbound
from app.services.agent_accounts import voice_notes


def _words(n: int) -> str:
    return " ".join(f"mot{i}" for i in range(n))


def _seg(start: float, end: float, words: int, *, logprob: float = -0.2, no_speech: float = 0.01, ratio: float = 1.4):
    return TranscribedSegment(
        _words(words), start, end, avg_logprob=logprob, no_speech_prob=no_speech, compression_ratio=ratio
    )


def _transcript(segments, duration, language="french"):
    text = " ".join(s.text for s in segments)
    return Transcript(text=text, language=language, segments=tuple(segments), duration=duration)


# -- the verbose answer --------------------------------------------------------


def test_the_verbose_answer_carries_each_segment_confidence():
    result = {
        "text": "a b",
        "duration": 60.0,
        "language": "french",
        "segments": [
            {"text": "a", "start": 0, "end": 30, "avg_logprob": -0.3, "no_speech_prob": 0.02, "compression_ratio": 1.2},
            {"text": "b", "start": 30, "end": 60},
        ],
    }
    t = transcription._transcript(result, "whisper-1")
    assert t.segments[0].avg_logprob == -0.3 and t.segments[0].no_speech_prob == 0.02
    assert t.segments[0].compression_ratio == 1.2
    assert t.segments[1].avg_logprob is None, "a segment without confidence is kept, unjudged"


# -- the judge -----------------------------------------------------------------


def test_a_full_confident_transcript_is_trusted_and_names_its_language():
    t = _transcript([_seg(0, 30, 70), _seg(30, 60, 70)], 60.0)
    line = voice_notes.judged_line(t, 60, say_language=True)
    assert line.startswith("[voice note, 1:00, transcribed, heard in french] mot0 ")
    assert "low confidence" not in line


def test_far_too_few_words_for_the_length_is_low_confidence():
    # 56 s of speech, 15 words: most of the speech was lost
    t = _transcript([_seg(0, 56, 15)], 56.0)
    line = voice_notes.judged_line(t, 56, say_language=True)
    assert line.startswith("[voice note, 0:56, heard in french: transcription low confidence: 15 words heard in 0:56")
    assert "mot0" not in line, "the doubtful text is never passed off as the note"


def test_audio_cut_short_is_low_confidence():
    t = _transcript([_seg(0, 12, 30)], 12.0)
    line = voice_notes.judged_line(t, 56)
    assert "transcription low confidence: only 0:12 of the audio was heard" in line


def test_speech_decoded_unsure_is_low_confidence():
    segs = [_seg(0, 20, 50, logprob=-1.4), _seg(20, 40, 50, ratio=2.9), _seg(40, 60, 50)]
    line = voice_notes.judged_line(_transcript(segs, 60.0), 60)
    assert "transcription low confidence: 67% of the speech was decoded unsure" in line


def test_a_segment_written_over_silence_is_dropped():
    segs = [
        _seg(0, 40, 100),
        TranscribedSegment("Sous-titres réalisés par la communauté", 40, 45, avg_logprob=-0.3, no_speech_prob=0.1),
        TranscribedSegment("merci", 45, 50, avg_logprob=-1.3, no_speech_prob=0.9),
    ]
    text, doubt = voice_notes.judge(_transcript(segs, 50.0), 50)
    assert doubt is None
    assert "Sous-titres" not in text and "merci" not in text and text.startswith("mot0")


def test_a_short_note_is_not_judged_by_its_word_rate():
    t = Transcript(text="oui", language="french", duration=4.0)
    assert voice_notes.judged_line(t, 4) == "[voice note, 0:04, transcribed] oui"


def test_a_plain_answer_without_segments_is_judged_on_length_alone():
    t = Transcript(text=_words(10), duration=55.0)
    assert "low confidence" in voice_notes.judged_line(t, 55)
    assert "low confidence" not in voice_notes.judged_line(Transcript(text=_words(120), duration=55.0), 55)


# -- the WhatsApp paths ----------------------------------------------------------


class _Transcriber:
    def __init__(self, transcript):
        self.transcript = transcript
        self.calls: list[dict] = []

    async def transcribe(self, audio, *, filename="audio.wav", language=None, words=False):
        self.calls.append({"filename": filename, "language": language, "words": words})
        return self.transcript


class _Db:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _gateway():
    target = whatsapp_inbound.InboundIntegration(
        integration_id=uuid.uuid4(), app_secret="", verify_token="", retention_days=30
    )
    return gi.GatewayIntegration(
        target=target, vendor="whapi", webhook_secret="s", channel_token="tok", owner_id=uuid.uuid4()
    )


def _wire(monkeypatch, transcript):
    from app.integrations.providers import whatsapp_gateway as wg

    transcriber = _Transcriber(transcript)

    async def media(self, media_id, max_bytes):
        return b"OGG", None

    async def resolve(_db, _user_id):
        return transcriber

    monkeypatch.setattr(wg.WhapiClient, "download_media", media)
    monkeypatch.setattr(transcription, "resolve_transcriber", resolve)
    monkeypatch.setattr(gi, "async_session", lambda: _Db())
    return transcriber


_LONG_VOICE = {
    "id": "V9",
    "type": "voice",
    "voice": {"id": "oga-long", "seconds": 56, "mime_type": "audio/ogg; codecs=opus"},
    "timestamp": 1_800_000_000,
    "from": "33600000000",
    "chat_id": "33600000000@s.whatsapp.net",
}


async def test_gateway_asks_the_verbose_answer_and_flags_a_lost_note(monkeypatch):
    transcriber = _wire(monkeypatch, _transcript([_seg(0, 56, 15)], 56.0))
    delivery = gi.parse_whapi({"messages": [_LONG_VOICE]})
    assert await gi.transcribe_voice(_gateway(), delivery) == 0
    assert transcriber.calls[0]["words"] is True, "segments and confidence are asked for"
    text = delivery.rows[0]["text"]
    assert text.startswith("[voice note, 0:56, heard in french: transcription low confidence:")
    assert "mot0" not in text


async def test_gateway_passes_a_trusted_note_with_its_language(monkeypatch):
    _wire(monkeypatch, _transcript([_seg(0, 28, 65), _seg(28, 56, 65)], 56.0))
    delivery = gi.parse_whapi({"messages": [_LONG_VOICE]})
    assert await gi.transcribe_voice(_gateway(), delivery) == 1
    assert delivery.rows[0]["text"].startswith("[voice note, 0:56, transcribed, heard in french] mot0")
