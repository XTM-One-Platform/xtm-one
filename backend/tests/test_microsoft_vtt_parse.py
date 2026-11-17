"""Tests for WEBVTT transcript parsing (`_parse_vtt`).

WEBVTT ``NOTE`` / ``STYLE`` / ``REGION`` blocks span multiple lines until the
next blank line, so the whole block must be skipped — not just its header — or
its body (e.g. CSS under ``STYLE``) leaks into the returned transcript.
"""

from __future__ import annotations

from app.integrations.providers.microsoft.transcripts import _parse_vtt


def test_parses_voice_spans_to_speaker_lines():
    vtt = (
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:04.000\n"
        "<v Alice>Hello everyone</v>\n\n"
        "00:00:05.000 --> 00:00:07.000\n"
        "<v Bob>Hi Alice</v>\n"
    )
    assert _parse_vtt(vtt) == "Alice: Hello everyone\nBob: Hi Alice"


def test_skips_multiline_note_block_body():
    """A NOTE block's body lines must NOT appear in the transcript."""
    vtt = (
        "WEBVTT\n\n"
        "NOTE\n"
        "This is a multi-line note\n"
        "spanning several lines\n\n"
        "00:00:01.000 --> 00:00:04.000\n"
        "<v Alice>Real content</v>\n"
    )
    assert _parse_vtt(vtt) == "Alice: Real content"


def test_skips_style_block_css_body():
    """CSS under a STYLE block must NOT pollute the transcript."""
    vtt = (
        "WEBVTT\n\n"
        "STYLE\n"
        "::cue {\n"
        "  background: yellow;\n"
        "  color: black;\n"
        "}\n\n"
        "00:00:01.000 --> 00:00:04.000\n"
        "<v Alice>Hello</v>\n"
    )
    assert _parse_vtt(vtt) == "Alice: Hello"


def test_skips_note_with_inline_comment_text():
    """A ``NOTE some comment`` header and its continuation are both skipped."""
    vtt = (
        "WEBVTT\n\n"
        "NOTE Recording started by org policy\n"
        "extra note line\n\n"
        "00:00:01.000 --> 00:00:02.000\n"
        "<v Bob>Hi</v>\n"
    )
    assert _parse_vtt(vtt) == "Bob: Hi"


def test_keeps_plain_cue_text_without_voice_tag():
    """Cue payload lines without a ``<v>`` tag are kept as-is."""
    vtt = (
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:04.000\n"
        "Plain spoken line\n"
    )
    assert _parse_vtt(vtt) == "Plain spoken line"
