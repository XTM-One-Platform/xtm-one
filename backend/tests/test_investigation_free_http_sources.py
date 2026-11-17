"""A built pack's free HTTP source: a URL its author typed.

Fetched through `pinned_fetch` (resolved once, connected to the checked
address, redirects re-checked, read capped): what is pinned here is that the
builder takes only a well-formed template, the source stays pinned through
its stored ref, the built-in http sources keep the shared fetch, and what a
refusal shows is generic while the precise reason goes to the logs only.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from app.services import investigation_queriers as queriers
from app.services import pinned_fetch
from app.services.investigation_pack_builder import CallerResources, PackDefinition, build_source_pack
from app.services.investigation_packs import BUILTIN_PACKS, SourceDefinition
from app.services.pinned_fetch import PinnedFetchRefusedError, PinnedResponse


def _definition(template: str, **extra) -> PackDefinition:
    return PackDefinition.model_validate(
        {"steps": [{"key": "a", "label": "A", "sources": [{"kind": "http", "url_template": template, **extra}]}]}
    )


def _source(template: str = "https://lookup.example/{value}") -> SourceDefinition:
    pack = build_source_pack(
        pack_id="p", name="P", description="", subject_types=["domain"], recommended=False,
        max_iterations=5, version=1, definition=_definition(template), resources=CallerResources(),
    )
    return pack.sources[0]


@pytest.mark.parametrize(
    "template",
    [
        "ftp://files.example/{value}",
        "file:///etc/{value}",
        "https://lookup.example/",
        "https://lookup.example/{value}/{value}",
        "https://lookup.example/{value.__class__}",
        "https://lookup.example/{value!r}",
        "https://lookup.example/{other}",
        "https://user:secret@lookup.example/{value}",
        "https:///{value}",
        "",
    ],
)
def test_a_template_that_could_not_be_fetched_safely_is_refused(template):
    with pytest.raises(ValidationError):
        _definition(template)


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("https://user@lookup.example/{value}", "carries no credentials"),
        ("https://:secret@lookup.example/{value}", "carries no credentials"),
        # `httpx.URL`, the fetch's parser, reads what precedes the `@` as
        # credentials: refused at build rather than at every run.
        ("https://lookup.example\\@trusted.example/{value}", "carries no credentials"),
        ("https://lookup.example:abc/{value}", "is malformed"),
        ("https://:443/{value}", "names a host"),
    ],
)
def test_the_template_is_judged_on_the_parse_the_fetch_connects_with(template, message):
    with pytest.raises(ValidationError, match=message):
        _definition(template)


def test_a_free_source_is_pinned_and_stays_pinned_when_continued():
    source = _source()
    assert (source.kind, source.pinned) == ("http", True)
    assert source.build_url("x.com") == "https://lookup.example/x.com"
    rebuilt = SourceDefinition.from_ref(source.to_ref(), subject_types=source.subject_types)
    assert rebuilt.pinned is True and rebuilt.identity() == source.identity()


def test_the_built_in_http_sources_keep_the_shared_fetch():
    assert all(not s.pinned for pack in BUILTIN_PACKS.values() for s in pack.sources)


def test_only_an_http_source_is_pinned():
    with pytest.raises(ValueError):
        SourceDefinition(name="X", subject_types=("domain",), kind="site_fingerprint", fingerprint_set="wordpress-media-network", pinned=True)


async def _run(monkeypatch, outcome):
    async def _fake(url, *, timeout, **_kw):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(pinned_fetch, "fetch_pinned", _fake)

    async def _shared(*_a, **_k):
        raise AssertionError("a free source never takes the shared fetch")

    monkeypatch.setattr(queriers, "fetch_and_extract", _shared)
    querier = queriers.make_web_fetch_querier()
    source = _source()
    return await querier(source, "x.com", source.build_url("x.com"))


@pytest.mark.asyncio
async def test_a_refusal_shows_a_generic_reason_and_logs_the_precise_one(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="app.services.investigation_queriers")
    outcome = await _run(monkeypatch, PinnedFetchRefusedError("'lookup.example' resolves to 10.0.0.7 (private address)"))
    assert (outcome.error_type, outcome.detail, outcome.detail_code) == (
        "invalid_response",
        "this address could not be read",
        "source.free_http_refused",
    )
    assert "10.0.0.7" not in repr(outcome), "the step is never an oracle on what resolves where"
    assert any("10.0.0.7" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    ("refused", "expected"),
    [
        (PinnedFetchRefusedError("slow", kind="timeout"), ("timeout", "source.timed_out")),
        (PinnedFetchRefusedError("404", kind="status", status=404), ("not_found", "source.http_status")),
        (PinnedFetchRefusedError("dns", kind="unreachable"), ("invalid_response", "source.free_http_refused")),
        (PinnedFetchRefusedError("pdf", kind="type"), ("invalid_response", "source.free_http_refused")),
    ],
)
@pytest.mark.asyncio
async def test_each_refusal_maps_onto_the_taxonomy(monkeypatch, refused, expected):
    outcome = await _run(monkeypatch, refused)
    assert (outcome.error_type, outcome.detail_code) == expected


@pytest.mark.asyncio
async def test_what_a_free_source_answered_is_a_quotable_finding(monkeypatch):
    text = (
        "Registrar: Example Registrar Inc. The domain was registered on 2024-03-01 and is held by a privacy proxy. "
        "Its name servers are ns1.example.net and ns2.example.net, and it was last updated on 2024-09-12."
    )
    outcome = await _run(
        monkeypatch,
        PinnedResponse(url="https://lookup.example/x.com", status_code=200, content_type="text/plain", text=text, capped=True),
    )
    assert outcome.findings_count == 1 and outcome.partial is True
    assert outcome.detail_code == "source.free_http_capped"
    (evidence,) = outcome.evidence
    assert (evidence.href, evidence.quote) == ("https://lookup.example/x.com", text)


@pytest.mark.asyncio
async def test_an_empty_answer_is_empty_not_an_error(monkeypatch):
    outcome = await _run(
        monkeypatch,
        PinnedResponse(url="https://lookup.example/x.com", status_code=200, content_type="text/plain", text="  ", capped=False),
    )
    assert outcome.findings_count == 0 and outcome.error_type is None


def _answer(text: str, content_type: str = "text/plain") -> PinnedResponse:
    return PinnedResponse(url="https://lookup.example/x.com", status_code=200, content_type=content_type, text=text, capped=False)


#: A JS shell: the markup is long, the text a reader would get is not.
_HTML_SHELL = (
    "<!doctype html><html><head><title>Lookup</title>"
    + "".join(f'<script src="/static/chunk-{n}.js"></script>' for n in range(20))
    + '</head><body><div id="root">Loading…</div></body></html>'
)


@pytest.mark.parametrize(
    ("answer", "chars"),
    [
        (_answer("Not found"), 9),
        (_answer(_HTML_SHELL, "text/html; charset=utf-8"), len("Loading…")),
        (_answer('{"found": false}', "application/json"), 16),
    ],
    ids=["nine-characters", "html-shell", "short-json"],
)
@pytest.mark.asyncio
async def test_a_thin_answer_is_empty_and_says_why(monkeypatch, answer, chars):
    outcome = await _run(monkeypatch, answer)
    assert (outcome.findings_count, outcome.error_type) == (0, None)
    assert (outcome.detail_code, outcome.detail_params) == ("source.thin_response", {"chars": chars})
    (evidence,) = outcome.evidence
    assert evidence.quote is None


@pytest.mark.asyncio
async def test_a_compact_json_answer_is_judged_by_the_structured_threshold(monkeypatch):
    body = '{"domain": "x.com", "registered": "2024-03-01"}'
    outcome = await _run(monkeypatch, _answer(body, "application/json"))
    assert outcome.findings_count == 1
    assert outcome.evidence[0].quote == body


@pytest.mark.asyncio
async def test_an_html_page_is_read_through_the_extractor_never_as_markup(monkeypatch):
    prose = "The domain x.com was registered on 2024-03-01 through Example Registrar Inc. " * 4
    html = f"<html><head><style>p {{color: red}}</style></head><body><article><p>{prose}</p></article></body></html>"
    outcome = await _run(monkeypatch, _answer(html, "text/html"))
    assert outcome.findings_count == 1
    quote = outcome.evidence[0].quote
    assert quote.startswith("The domain x.com was registered on 2024-03-01")
    assert "<" not in quote and "color: red" not in quote


@pytest.mark.asyncio
async def test_the_stored_quote_is_redacted_before_it_is_cut(monkeypatch):
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2"
    body = (
        "Lookup result for x.com: registered 2024-03-01, registrar Example Registrar Inc., status active. "
        f"Debug echo: Authorization: Bearer abcdefghijklmnop0123 and api key {token}. "
        "The record was last updated on 2024-09-12 and lists two name servers."
    )
    outcome = await _run(monkeypatch, _answer(body))
    assert outcome.findings_count == 1
    quote = outcome.evidence[0].quote
    assert "abcdefghijklmnop0123" not in quote and token not in quote
    assert quote.count("<redacted-auth-header>") == 1 and quote.count("<redacted-apikey>") == 1
