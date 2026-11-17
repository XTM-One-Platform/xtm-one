# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Provider web search, normalised into text plus span-level citations.

Anthropic, OpenAI and Mistral all expose a server-side web search that returns
a written answer WITH the passages it drew on. The platform already calls two
of them (`app/integrations/providers/*.py`), and both formatters throw the
useful half away:

    for cite in block.get("citations") or []:
        sources.setdefault(cite["url"], cite.get("title") or cite["url"])
    ...
    answer += "\\n\\nSources:\\n" + ...

What survives is a flat list of URLs appended to the end. What is discarded is
which SPAN of the answer each URL supports — `cited_text` on Anthropic,
`start_index`/`end_index` on OpenAI. That is precisely the material an inline
citation is made of, and without it a reader cannot tell which sentence a
source backs, only that the search touched it.

Those formatters are right for what they do: they build a string for a model
to read. This module exists for the other consumer — an investigation that has
to show a reader where a claim came from — so it returns structure and lets the
caller decide how to render it.

The two providers describe the same thing in different shapes:

    Anthropic   content[].citations[] -> {url, title, cited_text}
    OpenAI      output[].content[].annotations[] -> {url, title,
                                                     start_index, end_index}

Anthropic hands over the quote; OpenAI hands over offsets into its own text
block. Normalising both to `(quote, start, end)` against ONE accumulated answer
is what lets the rest of the feature stop caring which provider ran.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace

from app.core.redaction import redact_secrets

logger = logging.getLogger(__name__)

#: Longest quote retained per citation. A provider may cite several sentences;
#: the point of the quote is to let a reader recognise the claim in the source,
#: not to reproduce the source.
MAX_QUOTE_CHARS = 400

SEARCH_PROVIDERS = ("anthropic", "openai")


@dataclass(frozen=True)
class SearchCitation:
    """One source, and the span of the answer it supports."""

    url: str
    title: str
    #: The cited passage, verbatim from the source where the provider gives it
    #: (Anthropic) or sliced out of the answer where it gives offsets (OpenAI).
    #: Empty when the provider offered neither — a citation with no quote is
    #: still worth keeping, because the URL is real; it just cannot be shown
    #: as a quotation.
    quote: str = ""
    #: Half-open range into `SearchAnswer.text`. None when the provider does
    #: not say which part of its answer the source supports, which is the
    #: difference between "this source backs this sentence" and "this source
    #: was consulted" — a difference the renderer must not blur.
    start: int | None = None
    end: int | None = None

    def as_json(self) -> dict:
        data: dict = {"url": self.url, "title": self.title}
        if self.quote:
            data["quote"] = self.quote
        if self.start is not None and self.end is not None:
            data["start"], data["end"] = self.start, self.end
        return data


@dataclass(frozen=True)
class SearchAnswer:
    """A written answer and the citations attached to its spans."""

    text: str
    citations: tuple[SearchCitation, ...] = ()
    provider: str = ""
    model: str | None = None

    @property
    def anchored(self) -> tuple[SearchCitation, ...]:
        """Citations that name a span, in reading order.

        The ones that do not are still real sources; they simply cannot be
        rendered inline, and a caller that mixed the two would produce markers
        pointing at arbitrary places in the prose.
        """
        located = [c for c in self.citations if c.start is not None]
        return tuple(sorted(located, key=lambda c: (c.start or 0, c.end or 0)))


def _clean(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _title(value: object) -> str:
    """A page title, ready to show to a person.

    Unescaped for the same reason quotes are: providers pass the page's own
    HTML through, and a source list reading ``MITRE ATT&CK&reg;`` shows the
    markup instead of the name. Not applied to URLs — an entity in a URL is
    part of the address, and "fixing" it would break the link.
    """
    import html as _html

    text = _clean(value)
    return _html.unescape(text) if text else ""


def _quote(value: object) -> str:
    """A citation's passage, ready to show to a person.

    Unescaped because providers return the source's HTML entities verbatim —
    a quote reaching the reader as ``Russia&#x27;s`` is the source's own words
    rendered as markup nobody typed.
    """
    return _title(value)[:MAX_QUOTE_CHARS]


def fit_answer(
    text: str, citations: Sequence[SearchCitation], limit: int
) -> tuple[str, tuple[SearchCitation, ...]]:
    """Cut the answer to ``limit`` and drop the spans that no longer fit.

    The same class of bug `_anchor` exists to prevent, one layer up: the
    querier stored `text[:MAX_ANSWER_CHARS]` and passed the offsets through
    untouched, so any citation anchored past the cut pointed outside the
    stored answer. The renderer clamps rather than drops, so a stale `end`
    collapsed to the end of the prose and the closing sentence was attributed
    to a source that supported something thousands of characters later — a
    fabricated attribution rendered as a confident citation.

    Dropped rather than narrowed, which is where this differs from `_anchor`.
    There the removed characters are whitespace, so a span may safely shrink;
    here the cut removes CONTENT, so a span that survives only in part no
    longer selects what its `quote` names. The source is kept and the span
    goes, which is the rule the parsers already apply to an offset they
    cannot trust — and `quote` goes with it, exactly as the parsers pair them.
    """
    if len(text) <= limit:
        return text, tuple(citations)

    cut = text[:limit]
    kept: list[SearchCitation] = []
    for cite in citations:
        if cite.start is None or cite.end is None or cite.end <= len(cut):
            kept.append(cite)
            continue
        logger.debug(
            "search citation for %s dropped its span: [%s, %s) is past the "
            "%d-character answer cap; the source is kept",
            cite.url, cite.start, cite.end, limit,
        )
        kept.append(replace(cite, quote="", start=None, end=None))
    return cut, tuple(kept)


#: Schemes a citation URL may carry. An allowlist, because the set of
#: dangerous ones is open-ended and grows with whatever renders it.
_CITATION_SCHEMES = ("http://", "https://")


def _linkable(url: str) -> bool:
    """Whether a citation URL may be stored at all.

    Rejected HERE rather than at either renderer. The value is a URL a
    third-party page persuaded the provider to cite, it is persisted as step
    evidence, and it reaches two different render paths — one of which put it
    straight into `<a href>`, where React does not sanitise and a
    `javascript:` URL executes on click. Storing it makes that stored XSS
    rather than reflected, so the parser is the place to refuse: neither
    renderer then has to be trusted, and the bad value never reaches the
    database.

    A citation that fails this is dropped entirely rather than kept without
    its link — unlike a bad OFFSET, a bad URL leaves nothing worth showing:
    the source cannot be opened and cannot be checked.
    """
    return url.lower().startswith(_CITATION_SCHEMES)


def _anchor(
    raw: str, citations: list[SearchCitation]
) -> tuple[str, tuple[SearchCitation, ...]]:
    """Return the trimmed answer and its citations re-anchored to it.

    Offsets are accumulated against the RAW concatenation of the provider's
    blocks, and the answer is returned trimmed. Those are two different
    strings whenever a provider emits leading whitespace before its first
    block — which they routinely do before a leading `##` heading — and the
    difference is silent: every span shifts left by the number of characters
    `strip()` removed, so a marker lands mid-word and `quote` no longer names
    the text its span selects.

    The bounds check does not catch it. A shifted offset is still in range; it
    simply selects the wrong characters. So the shift is corrected here, once,
    on the way out, and anything that no longer fits the trimmed text loses
    its span rather than being clamped to a guess — the same rule the parsers
    already apply to an offset they cannot trust.
    """
    text = raw.strip()
    lead = len(raw) - len(raw.lstrip())
    if not lead and len(text) == len(raw):
        return text, tuple(citations)

    anchored: list[SearchCitation] = []
    for cite in citations:
        if cite.start is None or cite.end is None:
            anchored.append(cite)
            continue
        # Intersected with the trimmed text, not merely shifted. The
        # whitespace `strip()` removed was PART of the first and last blocks,
        # so their spans shrink rather than disappear — dropping them would
        # lose a citation the provider placed correctly. A span with nothing
        # left (a whitespace-only block) loses its offsets and keeps its
        # source, the same rule the parsers apply to an untrustworthy offset.
        start = max(0, cite.start - lead)
        end = min(len(text), cite.end - lead)
        if start < end:
            anchored.append(replace(cite, start=start, end=end))
        else:
            logger.debug(
                "citation span %r/%r survives no part of the trimmed answer "
                "for %s",
                cite.start, cite.end, cite.url,
            )
            anchored.append(replace(cite, start=None, end=None))
    return text, tuple(anchored)


def parse_anthropic(data: dict) -> SearchAnswer:
    """Normalise a Claude Messages response that used the web_search tool.

    Citations hang off each `text` block, so a block's offset inside the
    assembled answer has to be tracked as the blocks are concatenated —
    otherwise every citation in the second and later blocks would point into
    the first one.
    """
    parts: list[str] = []
    citations: list[SearchCitation] = []
    cursor = 0

    for block in data.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str) or not text:
            continue
        parts.append(text)
        span_start, span_end = cursor, cursor + len(text)
        cursor = span_end

        for cite in block.get("citations") or []:
            if not isinstance(cite, dict):
                continue
            url = _clean(cite.get("url"))
            if not url or not _linkable(url):
                continue
            citations.append(
                SearchCitation(
                    url=url,
                    title=_title(cite.get("title")) or url,
                    quote=_quote(cite.get("cited_text")),
                    # Anthropic attaches a citation to the whole block, not to
                    # a character range inside it, so the block IS the span.
                    start=span_start,
                    end=span_end,
                )
            )

    text, anchored = _anchor("".join(parts), citations)
    return SearchAnswer(
        text=text,
        citations=anchored,
        provider="anthropic",
        model=_clean(data.get("model")) or None,
    )


def parse_openai(data: dict) -> SearchAnswer:
    """Normalise an OpenAI Responses payload that used the web_search tool.

    `url_citation` annotations carry offsets into their own `output_text`
    block, which have to be shifted by everything already accumulated and then
    bounds-checked: a malformed or stale index would otherwise slice an
    unrelated part of the answer and present it as a quotation.
    """
    parts: list[str] = []
    citations: list[SearchCitation] = []
    cursor = 0

    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for block in item.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "output_text":
                continue
            text = block.get("text")
            if not isinstance(text, str) or not text:
                continue
            parts.append(text)
            base = cursor
            cursor += len(text)

            for ann in block.get("annotations") or []:
                if not isinstance(ann, dict) or ann.get("type") != "url_citation":
                    continue
                url = _clean(ann.get("url"))
                if not url or not _linkable(url):
                    continue
                start, end = ann.get("start_index"), ann.get("end_index")
                quote, abs_start, abs_end = "", None, None
                if (
                    isinstance(start, int)
                    and isinstance(end, int)
                    and 0 <= start < end <= len(text)
                ):
                    quote = _quote(text[start:end])
                    abs_start, abs_end = base + start, base + end
                else:
                    if start is not None or end is not None:
                        # Keep the source, drop the span: a citation that
                        # cannot be placed must not be placed at a guess.
                        logger.debug(
                            "discarding unusable citation offsets %r/%r for %s",
                            start, end, url,
                        )
                citations.append(
                    SearchCitation(
                        url=url,
                        title=_title(ann.get("title")) or url,
                        quote=quote,
                        start=abs_start,
                        end=abs_end,
                    )
                )

    text, anchored = _anchor("".join(parts), citations)
    return SearchAnswer(
        text=text,
        citations=anchored,
        provider="openai",
        model=_clean(data.get("model")) or None,
    )


PARSERS = {"anthropic": parse_anthropic, "openai": parse_openai}


def parse(provider: str, data: dict) -> SearchAnswer:
    """Normalise a raw provider payload, by provider name."""
    try:
        parser = PARSERS[provider]
    except KeyError as exc:
        raise ValueError(
            f"no web-search parser for provider {provider!r}; "
            f"known: {sorted(PARSERS)}"
        ) from exc
    return parser(data)


# ── Calling a provider ──────────────────────────────────────────────────────
#
# Deliberately separate from the parsers above, which stay pure: a payload can
# be normalised without a key, a network, or a database, and that is what makes
# the span logic cheap to pin down.

ANTHROPIC_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_SEARCH_TOOL = "web_search_20250305"
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-5"
OPENAI_BASE = "https://api.openai.com/v1"
OPENAI_DEFAULT_MODEL = "gpt-4o"

SEARCH_TIMEOUT = 120.0
#: Ceiling on the searches one query may trigger. The provider decides how many
#: it actually needs; this bounds the cost of a single investigation step.
#:
#: **Anthropic only.** The Responses API exposes no per-request search ceiling,
#: so on OpenAI this is not sent and the provider decides alone. Said here
#: rather than left implied: the constant read as a guarantee across both
#: paths, and `max_output_tokens` below is the only bound that path has.
MAX_SEARCHES = 5
#: Cap on the answer itself, sent on BOTH paths. On OpenAI it is the only cost
#: bound there is, and it is also what keeps an answer inside
#: `MAX_ANSWER_CHARS` — without it an unbounded response made a citation
#: anchored past that cap reachable rather than theoretical.
MAX_ANSWER_TOKENS = 2048


class SearchError(Exception):
    """A search that could not be completed, typed for the step vocabulary.

    Carries an ``error_type`` from ``STEP_ERROR_TYPES`` rather than a message
    alone, because the block, the report and the metrics all branch on it —
    and "the key is wrong" and "the provider is down" need different actions
    from whoever reads the step.
    """

    def __init__(self, error_type: str, detail: str):
        super().__init__(detail)
        self.error_type = error_type
        self.detail = detail


def _classify(status: int, body: str) -> SearchError:
    """Map an HTTP failure onto the step vocabulary.

    The same reading as the web fetcher's, so a failed search and a failed
    fetch describe themselves the same way: 401/403 is a credential problem,
    429 is a quota, 5xx means the provider could not answer at all, and a 4xx
    that is none of those is a request this code got wrong.
    """
    # Redacted, not merely truncated. This excerpt is a provider's own body,
    # and a provider echoing the request it refused is the ordinary way an API
    # key reaches a place it should not be — a 400 quoting the Authorization
    # header, say. The cap limits volume; it does nothing about sensitivity,
    # since a bearer token fits comfortably inside 300 characters.
    excerpt = redact_secrets((body or "").strip())[:300]
    if status in (401, 403):
        return SearchError("auth_error", f"the search provider rejected the key ({status})")
    if status == 429:
        return SearchError("rate_limited", "the search provider is rate-limiting this key")
    if status >= 500:
        return SearchError("unreachable", f"the search provider answered {status}")
    return SearchError("invalid_response", f"the search request was refused ({status}): {excerpt}")


async def search(
    provider: str,
    api_key: str,
    query: str,
    *,
    model: str | None = None,
    max_searches: int = MAX_SEARCHES,
) -> SearchAnswer:
    """Ask a provider to search the web and answer, and keep its citations.

    The provider does the searching AND the writing: that is the whole reason
    to go through it rather than a bare search API. What comes back is prose
    with its sources attached to spans — the material an inline citation needs,
    and material this code could not reconstruct from a list of links.
    """
    import httpx

    if provider not in PARSERS:
        raise SearchError(
            "invalid_response", f"{provider!r} has no web search this code can read"
        )
    cleaned = (query or "").strip()
    if not cleaned:
        raise SearchError("invalid_response", "a web search needs a question")
    if not (api_key or "").strip():
        raise SearchError("auth_error", f"no API key configured for {provider}")

    if provider == "anthropic":
        url = f"{ANTHROPIC_BASE}/messages"
        headers = {
            "x-api-key": api_key.strip(),
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        payload: dict = {
            "model": model or ANTHROPIC_DEFAULT_MODEL,
            "max_tokens": MAX_ANSWER_TOKENS,
            "messages": [{"role": "user", "content": cleaned}],
            "tools": [
                {
                    "type": ANTHROPIC_SEARCH_TOOL,
                    "name": "web_search",
                    "max_uses": max(1, min(int(max_searches), 10)),
                }
            ],
        }
    else:
        url = f"{OPENAI_BASE}/responses"
        headers = {
            "Authorization": f"Bearer {api_key.strip()}",
            "content-type": "application/json",
        }
        payload = {
            "model": model or OPENAI_DEFAULT_MODEL,
            "input": cleaned,
            "tools": [{"type": "web_search"}],
            "max_output_tokens": MAX_ANSWER_TOKENS,
        }

    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT) as client:
            response = await client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException as exc:
        raise SearchError("timeout", f"the search provider did not answer in time: {exc}") from exc
    except httpx.HTTPError as exc:
        raise SearchError("unreachable", f"the search provider could not be reached: {exc}") from exc

    if response.status_code >= 400:
        raise _classify(response.status_code, response.text)

    try:
        data = response.json()
    except ValueError as exc:
        raise SearchError("invalid_response", "the search provider did not return JSON") from exc
    if not isinstance(data, dict):
        raise SearchError("invalid_response", "the search provider returned an unexpected shape")

    return parse(provider, data)
