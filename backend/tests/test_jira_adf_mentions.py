"""Jira rich text: a [~accountid:ID] token becomes an ADF mention node (#544)."""

from __future__ import annotations

from app.integrations.providers.jira._common import adf_to_text, text_to_adf


def _paragraphs(doc: dict) -> list[list[dict]]:
    return [p["content"] for p in doc["content"]]


def test_plain_text_is_unchanged():
    doc = text_to_adf("hello\n\nworld")
    assert doc["type"] == "doc" and doc["version"] == 1
    assert _paragraphs(doc) == [
        [{"type": "text", "text": "hello"}],
        [],
        [{"type": "text", "text": "world"}],
    ]


def test_mention_token_becomes_a_mention_node():
    doc = text_to_adf("Hi [~accountid:5b10a2844c20165700ede21g], please review.")
    assert _paragraphs(doc) == [
        [
            {"type": "text", "text": "Hi "},
            {"type": "mention", "attrs": {"id": "5b10a2844c20165700ede21g"}},
            {"type": "text", "text": ", please review."},
        ]
    ]


def test_several_mentions_and_cloud_style_ids():
    doc = text_to_adf("[~accountid:712020:ab-cd_1][~accountid:abc]\nnext")
    first, second = _paragraphs(doc)
    assert first == [
        {"type": "mention", "attrs": {"id": "712020:ab-cd_1"}},
        {"type": "mention", "attrs": {"id": "abc"}},
    ]
    assert second == [{"type": "text", "text": "next"}]


def test_malformed_token_stays_text():
    doc = text_to_adf("@Alice and [~accountid:] and [~bob]")
    assert _paragraphs(doc) == [[{"type": "text", "text": "@Alice and [~accountid:] and [~bob]"}]]


def test_mention_reads_back_as_text():
    doc = text_to_adf("ping [~accountid:abc]")
    assert adf_to_text(doc).strip() == "ping @abc"


def test_empty_text_is_one_empty_paragraph():
    assert text_to_adf("") == {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": []}]}
