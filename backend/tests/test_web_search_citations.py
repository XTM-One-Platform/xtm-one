"""Normalising provider web search into span-level citations.

The whole point of this module is the half the existing formatters discard:
WHICH SENTENCE a source supports. So these tests are mostly about spans — that
they land on the right part of a multi-block answer, that an unusable offset
loses the span rather than pointing somewhere plausible and wrong, and that a
citation with no span is never silently treated as one that has a span.
"""

from __future__ import annotations

import pytest

from app.services.web_search import (
    MAX_QUOTE_CHARS,
    SearchAnswer,
    SearchCitation,
    parse,
    parse_anthropic,
    parse_openai,
)

# ── Anthropic ───────────────────────────────────────────────────────────────


def _a_block(text: str, *cites: dict) -> dict:
    return {"type": "text", "text": text, "citations": list(cites)}


def _a_cite(url: str, title: str = "", cited: str = "") -> dict:
    return {
        "type": "web_search_result_location",
        "url": url,
        "title": title,
        "cited_text": cited,
    }


def test_a_citation_carries_the_quote_the_provider_gave() -> None:
    answer = parse_anthropic({
        "model": "claude-x",
        "content": [
            _a_block(
                "FIMI is a doctrine term.",
                _a_cite("https://ecteg.eu/", "ECTEG", "FIMI refers to Foreign Information Manipulation"),
            )
        ],
    })
    assert answer.text == "FIMI is a doctrine term."
    assert answer.provider == "anthropic"
    assert answer.model == "claude-x"
    assert len(answer.citations) == 1
    cite = answer.citations[0]
    assert cite.url == "https://ecteg.eu/"
    assert cite.title == "ECTEG"
    assert cite.quote.startswith("FIMI refers to")


def test_a_citation_in_a_later_block_points_at_that_block() -> None:
    """The bug this exists to prevent.

    Citations hang off their own text block, so without tracking the offset of
    each block inside the assembled answer, every citation after the first
    would claim to support the opening sentence.
    """
    answer = parse_anthropic({
        "content": [
            _a_block("First sentence. ", _a_cite("https://a.example/")),
            _a_block("Second sentence.", _a_cite("https://b.example/")),
        ]
    })
    assert answer.text == "First sentence. Second sentence."
    first, second = answer.citations
    assert answer.text[first.start : first.end] == "First sentence. "
    assert answer.text[second.start : second.end] == "Second sentence."
    assert second.start > first.start


def test_a_citation_without_a_url_is_dropped() -> None:
    """A source with no address cannot be checked by anyone."""
    answer = parse_anthropic({
        "content": [_a_block("Text.", _a_cite(""), _a_cite("   "), _a_cite("https://ok.example/"))]
    })
    assert [c.url for c in answer.citations] == ["https://ok.example/"]


def test_a_citation_with_no_title_falls_back_to_its_url() -> None:
    answer = parse_anthropic({"content": [_a_block("Text.", _a_cite("https://x.example/"))]})
    assert answer.citations[0].title == "https://x.example/"


def test_non_text_blocks_do_not_shift_the_offsets() -> None:
    """A `web_search_tool_result` block contributes no prose.

    Counting it would push every later span past where its sentence is.
    """
    answer = parse_anthropic({
        "content": [
            {"type": "web_search_tool_result", "content": [{"url": "https://a.example/"}]},
            _a_block("Only prose.", _a_cite("https://b.example/")),
        ]
    })
    assert answer.text == "Only prose."
    assert answer.text[answer.citations[0].start : answer.citations[0].end] == "Only prose."


# ── OpenAI ──────────────────────────────────────────────────────────────────


def _o_message(text: str, *anns: dict) -> dict:
    return {
        "type": "message",
        "content": [{"type": "output_text", "text": text, "annotations": list(anns)}],
    }


def _o_ann(url: str, start, end, title: str = "") -> dict:
    return {
        "type": "url_citation",
        "url": url,
        "title": title,
        "start_index": start,
        "end_index": end,
    }


def test_offsets_are_sliced_into_a_quote() -> None:
    """OpenAI gives a range, not the passage; the quote is cut from the text."""
    answer = parse_openai({
        "model": "gpt-x",
        "output": [_o_message("Alpha beta gamma.", _o_ann("https://a.example/", 6, 10, "A"))],
    })
    cite = answer.citations[0]
    assert cite.quote == "beta"
    assert answer.text[cite.start : cite.end] == "beta"
    assert answer.provider == "openai"


def test_offsets_are_shifted_across_messages() -> None:
    answer = parse_openai({
        "output": [
            _o_message("Alpha. ", _o_ann("https://a.example/", 0, 5)),
            _o_message("Beta.", _o_ann("https://b.example/", 0, 4)),
        ]
    })
    assert answer.text == "Alpha. Beta."
    a, b = answer.citations
    assert answer.text[a.start : a.end] == "Alpha"
    assert answer.text[b.start : b.end] == "Beta"


@pytest.mark.parametrize(
    "start,end",
    [(-1, 4), (4, 2), (0, 999), (None, 4), (4, None), ("0", "4"), (3, 3)],
)
def test_an_unusable_offset_loses_the_span_not_the_source(start, end) -> None:
    """Out of range, inverted, empty or non-numeric.

    Slicing on any of these would present an unrelated fragment of the answer
    as a quotation from the source — a fabricated quote, which is worse than
    no quote. The URL is real, so the citation survives without a span.
    """
    answer = parse_openai({
        "output": [_o_message("Alpha beta.", _o_ann("https://a.example/", start, end))]
    })
    cite = answer.citations[0]
    assert cite.url == "https://a.example/"
    assert cite.quote == ""
    assert cite.start is None and cite.end is None


def test_a_non_url_citation_annotation_is_ignored() -> None:
    """Responses carries other annotation types (file citations, and more)."""
    answer = parse_openai({
        "output": [
            _o_message(
                "Text.",
                {"type": "file_citation", "file_id": "f1", "start_index": 0, "end_index": 4},
                _o_ann("https://a.example/", 0, 4),
            )
        ]
    })
    assert [c.url for c in answer.citations] == ["https://a.example/"]


# ── Shared behaviour ────────────────────────────────────────────────────────


def test_only_anchored_citations_can_be_rendered_inline() -> None:
    """Mixing the two would put markers at arbitrary places in the prose."""
    answer = SearchAnswer(
        text="Alpha beta.",
        citations=(
            SearchCitation(url="https://b.example/", title="B", start=6, end=10),
            SearchCitation(url="https://n.example/", title="N"),
            SearchCitation(url="https://a.example/", title="A", start=0, end=5),
        ),
    )
    assert [c.url for c in answer.anchored] == ["https://a.example/", "https://b.example/"]
    assert len(answer.citations) == 3


def test_a_quote_is_bounded() -> None:
    long = "x" * (MAX_QUOTE_CHARS * 3)
    answer = parse_anthropic({"content": [_a_block("T.", _a_cite("https://a.example/", cited=long))]})
    assert len(answer.citations[0].quote) == MAX_QUOTE_CHARS


def test_an_answer_with_no_citations_is_not_an_error() -> None:
    """A search can legitimately return prose it did not source."""
    answer = parse_anthropic({"content": [{"type": "text", "text": "Nothing found."}]})
    assert answer.text == "Nothing found."
    assert answer.citations == ()
    assert answer.anchored == ()


def test_an_empty_payload_yields_an_empty_answer() -> None:
    for payload in ({}, {"content": []}, {"content": None}, {"output": []}):
        for provider in ("anthropic", "openai"):
            answer = parse(provider, payload)
            assert answer.text == ""
            assert answer.citations == ()


def test_an_unknown_provider_raises_rather_than_returning_nothing() -> None:
    """Silently returning an empty answer would read as "the web had nothing"."""
    with pytest.raises(ValueError, match="no web-search parser"):
        parse("perplexity", {})


def test_a_citation_serialises_only_what_it_has() -> None:
    assert SearchCitation(url="u", title="t").as_json() == {"url": "u", "title": "t"}
    full = SearchCitation(url="u", title="t", quote="q", start=1, end=2).as_json()
    assert full == {"url": "u", "title": "t", "quote": "q", "start": 1, "end": 2}


# ── Calling a provider ──────────────────────────────────────────────────────
#
# Through a stub transport rather than the network: what is under test is the
# request this code builds and how it reads a failure, both of which a live
# provider would only make slower and flakier to pin down.


import httpx

from app.services.web_search import SearchError, search


def _transport(handler):
    """Patch httpx.AsyncClient so `search` talks to `handler` instead."""

    class _Client:
        def __init__(self, *a, **k):
            self.kwargs = k

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            return handler(url, headers or {}, json or {})

    return _Client


def _ok(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


@pytest.mark.asyncio
async def test_an_anthropic_search_sends_the_server_side_tool(monkeypatch) -> None:
    seen: dict = {}

    def handler(url, headers, body):
        seen.update(url=url, headers=headers, body=body)
        return _ok({"content": [_a_block("Answer.", _a_cite("https://a.example/", "A", "quote"))]})

    monkeypatch.setattr(httpx, "AsyncClient", _transport(handler))
    answer = await search("anthropic", "sk-test", "what is FIMI?")

    assert seen["url"].endswith("/messages")
    assert seen["headers"]["x-api-key"] == "sk-test"
    assert seen["headers"]["anthropic-version"]
    tool = seen["body"]["tools"][0]
    assert tool["type"].startswith("web_search")
    assert tool["name"] == "web_search"
    assert seen["body"]["messages"][0]["content"] == "what is FIMI?"
    assert answer.citations[0].quote == "quote"


@pytest.mark.asyncio
async def test_an_openai_search_uses_the_responses_endpoint(monkeypatch) -> None:
    seen: dict = {}

    def handler(url, headers, body):
        seen.update(url=url, headers=headers, body=body)
        return _ok({"output": [_o_message("Answer.", _o_ann("https://a.example/", 0, 6))]})

    monkeypatch.setattr(httpx, "AsyncClient", _transport(handler))
    answer = await search("openai", "sk-test", "what is FIMI?")

    assert seen["url"].endswith("/responses")
    assert seen["headers"]["Authorization"] == "Bearer sk-test"
    assert seen["body"]["tools"] == [{"type": "web_search"}]
    assert answer.text == "Answer."


@pytest.mark.asyncio
async def test_the_openai_path_bounds_its_output(monkeypatch) -> None:
    """`MAX_SEARCHES` reads as a ceiling on both providers and is Anthropic's
    alone — the Responses API takes no search cap. So `max_output_tokens` is
    the only bound that path has, and without it an unbounded answer made a
    citation anchored past `MAX_ANSWER_CHARS` reachable rather than
    theoretical."""
    from app.services.web_search import MAX_ANSWER_TOKENS

    seen: dict = {}

    def handler(url, headers, body):
        seen.update(body=body)
        return _ok({"output": []})

    monkeypatch.setattr(httpx, "AsyncClient", _transport(handler))
    await search("openai", "sk", "q")
    assert seen["body"]["max_output_tokens"] == MAX_ANSWER_TOKENS


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "data:text/html,<script>", "vbscript:x"])
def test_a_citation_url_that_is_not_a_link_never_reaches_the_record(bad) -> None:
    """The value is a URL a third-party page persuaded the provider to cite,
    it is persisted as step evidence, and it reaches two renderers — one of
    which put it straight into `<a href>`, where React does not sanitise.
    Refused at the parser, so neither renderer has to be trusted and the bad
    value never reaches the database."""
    from app.services.web_search import parse_anthropic, parse_openai

    anthropic = parse_anthropic(
        {"content": [{"type": "text", "text": "A claim.", "citations": [
            {"url": bad, "title": "Evil"},
            {"url": "https://ok.example/", "title": "Good"},
        ]}]}
    )
    assert [c.url for c in anthropic.citations] == ["https://ok.example/"]

    openai = parse_openai(
        {"output": [{"type": "message", "content": [{"type": "output_text", "text": "A claim.",
            "annotations": [
                {"type": "url_citation", "url": bad, "title": "Evil"},
                {"type": "url_citation", "url": "https://ok.example/", "title": "Good"},
            ]}]}]}
    )
    assert [c.url for c in openai.citations] == ["https://ok.example/"]


def test_a_citation_past_the_answer_cap_keeps_its_url_and_loses_its_span() -> None:
    """Truncating the text while passing the offsets through is the same bug
    `_anchor` fixes, one layer up. The renderer CLAMPS rather than drops, so a
    stale `end` collapsed to the end of the prose and attributed the closing
    sentence to a source that supported something thousands of characters
    later — a fabricated attribution rendered as a confident citation."""
    from app.services.web_search import SearchCitation, fit_answer

    text = "A" * 1000
    inside = SearchCitation(url="https://in.example/", title="In", quote="AAA", start=10, end=20)
    past = SearchCitation(url="https://out.example/", title="Out", quote="AAA", start=800, end=900)
    straddling = SearchCitation(url="https://cut.example/", title="Cut", quote="AAA", start=490, end=520)

    fitted, citations = fit_answer(text, [inside, past, straddling], 500)

    assert len(fitted) == 500
    assert [c.url for c in citations] == [
        "https://in.example/", "https://out.example/", "https://cut.example/"
    ], "every source is kept — only the span is in question"
    assert (citations[0].start, citations[0].end) == (10, 20), "a span inside the cut is untouched"
    assert citations[1].start is None and citations[1].end is None
    assert citations[2].start is None and citations[2].end is None, (
        "a span the cut runs through no longer selects what its quote names"
    )
    assert citations[1].quote == "" and citations[2].quote == "", (
        "the quote goes with the span, as the parsers already pair them"
    )
    for cite in citations:
        assert cite.end is None or cite.end <= len(fitted)


def test_an_answer_within_the_cap_is_untouched() -> None:
    from app.services.web_search import SearchCitation, fit_answer

    cite = SearchCitation(url="https://a.example/", title="A", quote="q", start=1, end=4)
    fitted, citations = fit_answer("short", [cite], 500)
    assert fitted == "short"
    assert (citations[0].start, citations[0].end) == (1, 4)


async def test_the_search_budget_is_clamped(monkeypatch) -> None:
    """A caller asking for a hundred searches gets ten, not a hundred."""
    seen: dict = {}

    def handler(url, headers, body):
        seen.update(body=body)
        return _ok({"content": []})

    monkeypatch.setattr(httpx, "AsyncClient", _transport(handler))
    await search("anthropic", "sk", "q", max_searches=100)
    assert seen["body"]["tools"][0]["max_uses"] == 10
    await search("anthropic", "sk", "q", max_searches=0)
    assert seen["body"]["tools"][0]["max_uses"] == 1


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "auth_error"),
        (403, "auth_error"),
        (429, "rate_limited"),
        (500, "unreachable"),
        (503, "unreachable"),
        (400, "invalid_response"),
        (404, "invalid_response"),
    ],
)
@pytest.mark.asyncio
async def test_a_failure_is_typed_for_the_step_vocabulary(monkeypatch, status, expected) -> None:
    """`auth_error` and `unreachable` send whoever reads the step somewhere
    different — a flat "search failed" sends them nowhere."""
    monkeypatch.setattr(
        httpx, "AsyncClient", _transport(lambda u, h, b: httpx.Response(status, text="nope"))
    )
    with pytest.raises(SearchError) as exc:
        await search("anthropic", "sk", "q")
    assert exc.value.error_type == expected


@pytest.mark.asyncio
async def test_a_timeout_is_a_timeout_not_an_outage(monkeypatch) -> None:
    def handler(*_a, **_k):
        raise httpx.TimeoutException("too slow")

    monkeypatch.setattr(httpx, "AsyncClient", _transport(handler))
    with pytest.raises(SearchError) as exc:
        await search("anthropic", "sk", "q")
    assert exc.value.error_type == "timeout"


@pytest.mark.asyncio
async def test_a_network_failure_is_unreachable(monkeypatch) -> None:
    def handler(*_a, **_k):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "AsyncClient", _transport(handler))
    with pytest.raises(SearchError) as exc:
        await search("anthropic", "sk", "q")
    assert exc.value.error_type == "unreachable"


@pytest.mark.asyncio
async def test_a_missing_key_fails_before_the_network(monkeypatch) -> None:
    """Reported as the configuration problem it is, not as an outage."""

    def handler(*_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("a keyless search reached the network")

    monkeypatch.setattr(httpx, "AsyncClient", _transport(handler))
    for key in ("", "   ", None):
        with pytest.raises(SearchError) as exc:
            await search("anthropic", key, "q")
        assert exc.value.error_type == "auth_error"


@pytest.mark.asyncio
async def test_an_empty_question_is_refused(monkeypatch) -> None:
    def handler(*_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("an empty search reached the network")

    monkeypatch.setattr(httpx, "AsyncClient", _transport(handler))
    with pytest.raises(SearchError):
        await search("anthropic", "sk", "   ")


@pytest.mark.asyncio
async def test_a_provider_without_a_parser_is_refused_up_front(monkeypatch) -> None:
    """Calling it would spend a request to produce an unreadable payload."""

    def handler(*_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("an unreadable provider was called anyway")

    monkeypatch.setattr(httpx, "AsyncClient", _transport(handler))
    with pytest.raises(SearchError) as exc:
        await search("perplexity", "sk", "q")
    assert exc.value.error_type == "invalid_response"


@pytest.mark.asyncio
async def test_a_non_json_body_is_an_invalid_response(monkeypatch) -> None:
    monkeypatch.setattr(
        httpx, "AsyncClient", _transport(lambda u, h, b: httpx.Response(200, text="<html>"))
    )
    with pytest.raises(SearchError) as exc:
        await search("anthropic", "sk", "q")
    assert exc.value.error_type == "invalid_response"


def test_a_title_reaches_the_reader_as_words_not_entities() -> None:
    """The source list is the reader's index; `MITRE ATT&CK&reg;` is not a name."""
    answer = parse_anthropic({
        "content": [_a_block("T.", _a_cite("https://attack.mitre.org/", "MITRE ATT&amp;CK&reg;"))]
    })
    assert answer.citations[0].title == "MITRE ATT&CK\u00ae"


def test_a_url_is_never_unescaped() -> None:
    """An entity in an address is part of the address; "fixing" it breaks it."""
    answer = parse_anthropic({
        "content": [_a_block("T.", _a_cite("https://x.example/?a=1&amp;b=2"))]
    })
    assert answer.citations[0].url == "https://x.example/?a=1&amp;b=2"


def test_a_quote_reaches_the_reader_as_words_not_entities() -> None:
    """Providers return the source's HTML verbatim.

    Left alone, a quotation shows up as `Russia&#x27;s Foreign Intelligence
    Service` — the source's own words rendered as markup nobody typed.
    """
    answer = parse_anthropic({
        "content": [
            _a_block("T.", _a_cite("https://a.example/", cited="Russia&#x27;s SVR &amp; the GRU"))
        ]
    })
    assert answer.citations[0].quote == "Russia's SVR & the GRU"


# ── Offsets are anchored to the text that is actually returned ──────────────
#
# The parsers measure spans against the RAW concatenation of the provider's
# blocks and return the answer trimmed. Those are two different strings the
# moment a provider emits whitespace before its first block — which they
# routinely do before a leading `##` heading — and every span then points `k`
# characters too far right.
#
# The failure is silent in the worst possible way: a shifted offset is still
# in range, so the bounds check passes and the span simply selects the WRONG
# text. A marker one word off still looks like a citation.
#
# Every fixture above starts with a non-whitespace character, so none of them
# could ever have caught it.


def test_offsets_survive_a_leading_newline() -> None:
    answer = parse_anthropic(
        {
            "content": [
                _a_block(
                    "\n\n## Heading\nFIMI is a term.",
                    _a_cite("https://a.example/", cited="FIMI"),
                )
            ]
        }
    )
    assert answer.text.startswith("## Heading")
    c = answer.citations[0]
    assert answer.text[c.start : c.end].startswith("## Heading")
    assert c.end <= len(answer.text), "a span may never run past the answer"


def test_a_later_blocks_span_is_not_shifted_by_trimmed_whitespace() -> None:
    """The case that puts a marker mid-word.

    Two blocks, the first starting with newlines: before anchoring, the second
    citation named `'lta epsilon zeta.'` — three characters into the word it
    was supposed to start at.
    """
    answer = parse_anthropic(
        {
            "content": [
                _a_block(
                    "\n\nAlpha beta gamma. ", _a_cite("https://a.example/", cited="Alpha")
                ),
                _a_block(
                    "Delta epsilon zeta.", _a_cite("https://b.example/", cited="Delta")
                ),
            ]
        }
    )
    assert answer.text == "Alpha beta gamma. Delta epsilon zeta."
    second = answer.citations[1]
    assert answer.text[second.start : second.end] == "Delta epsilon zeta."


def test_the_first_blocks_span_shrinks_rather_than_disappearing() -> None:
    """The trimmed whitespace was part of that block, so its span narrows.

    Dropping it instead would lose a citation the provider placed correctly —
    which is why this is an intersection and not a shift-and-discard.
    """
    answer = parse_anthropic(
        {
            "content": [
                _a_block(
                    "\n\nAlpha beta gamma. ", _a_cite("https://a.example/", cited="Alpha")
                ),
                _a_block("Delta.", _a_cite("https://b.example/", cited="Delta")),
            ]
        }
    )
    first = answer.citations[0]
    assert first.start == 0
    assert answer.text[first.start : first.end] == "Alpha beta gamma. "


def test_a_whitespace_only_block_keeps_its_source_and_loses_its_span() -> None:
    """Nothing of it survives the trim, so there is nowhere to put a marker —
    and a citation that cannot be placed must not be placed at a guess."""
    answer = parse_anthropic(
        {
            "content": [
                _a_block("Body.", _a_cite("https://a.example/", cited="Body")),
                _a_block("   \n", _a_cite("https://b.example/", cited="x")),
            ]
        }
    )
    assert len(answer.citations) == 2, "the source is kept"
    assert answer.citations[1].start is None
    assert answer.citations[1].end is None


def test_an_answer_with_no_leading_whitespace_is_untouched() -> None:
    """The correction must be invisible in the ordinary case."""
    answer = parse_anthropic(
        {
            "content": [
                _a_block("First sentence. ", _a_cite("https://a.example/")),
                _a_block("Second sentence.", _a_cite("https://b.example/")),
            ]
        }
    )
    assert [(c.start, c.end) for c in answer.citations] == [(0, 16), (16, 32)]


def test_openai_offsets_are_anchored_too() -> None:
    """Same invariant, the other provider: its annotations carry explicit
    offsets into their own block, which are shifted by the accumulated text
    and then by the trim."""
    answer = parse_openai(
        {
            "output": [
                _o_message("\n\nAlpha beta gamma. ", _o_ann("https://a.example/", 2, 7)),
                _o_message("Delta epsilon.", _o_ann("https://b.example/", 0, 5)),
            ]
        }
    )
    assert answer.text == "Alpha beta gamma. Delta epsilon."
    first, second = answer.citations
    assert answer.text[first.start : first.end] == "Alpha"
    assert answer.text[second.start : second.end] == "Delta"


def test_a_refusal_that_echoes_the_request_does_not_echo_the_key() -> None:
    """`_classify` puts the provider's own body into the error detail, and a
    provider quoting the request it refused is the ordinary way a key reaches
    a log or a UI. Truncation is no defence — a bearer token is well inside
    300 characters — so the excerpt goes through the same redaction the
    investigation queriers apply."""
    from app.services.web_search import _classify

    err = _classify(
        400,
        '{"error":"unsupported parameter","sent":'
        '{"Authorization":"Bearer sk-live-AbCdEf0123456789ZyXwVu"}}',
    )
    assert "sk-live-AbCdEf0123456789ZyXwVu" not in err.detail
    assert "unsupported parameter" in err.detail, "still a usable diagnostic"
