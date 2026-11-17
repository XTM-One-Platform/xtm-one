"""The disinformation infrastructure pack and its two source kinds.

The pack turns CheckFirst's method for mapping the Storm-1516 news-site
network ("Noise without effect", June 2026, chapter 5) into an investigation:
fingerprint a newsroom-named domain, read the IPs it resolved to, read back the
newsroom-named domains each IP hosted. What is pinned here is what makes the
result honest: a fingerprint reports the signals it saw and never a verdict,
a filter says how much it set aside, a key the caller cannot see is never
used, and every finding carries a citation the report can quote.
"""

from __future__ import annotations

import asyncio
import dataclasses

import httpx
import pytest

from app.models.investigation import DETAIL_CODE_PARAMS
from app.services import investigation_packs as packs
from app.services import investigation_queriers as queriers
from app.services.investigation_packs import (
    DISINFO_INFRASTRUCTURE_SLUG,
    NEWS_OUTLET_KEYWORDS,
    SourceDefinition,
    disinfo_infrastructure_pack,
    plan_for,
    recommend_packs,
)
from app.services.investigation_report import is_quotable
from app.services.web_fetch import FetchResult

VT = ("11111111-1111-1111-1111-111111111111", "Team VirusTotal")


def _sources(pack, subject_type: str, value: str) -> list[str]:
    _planned, executable = plan_for([pack], subject_type, value)
    return [source.name for _slug, source in executable]


def _source(pack, name: str) -> SourceDefinition:
    return next(s for s in pack.sources if s.name == name)


# ── The pack ────────────────────────────────────────────────────────────────


def test_a_domain_is_qualified_then_read_from_passive_dns():
    pack = disinfo_infrastructure_pack(VT)
    assert _sources(pack, "domain", "yerevandaily.am") == [
        "Site fingerprint",
        "RDAP registration (domain)",
        "Web research",
        "VirusTotal passive DNS",
    ]


def test_a_url_reaches_the_same_sources_through_its_host():
    pack = disinfo_infrastructure_pack(VT)
    assert _sources(pack, "url", "https://yerevandaily.am/1489-2/") == [
        "Site fingerprint",
        "RDAP registration (host)",
        "Web research",
        "VirusTotal passive DNS",
    ]
    fingerprint = _source(pack, "Site fingerprint")
    assert fingerprint.fingerprint_url("https://yerevandaily.am/1489-2/") == (
        "https://yerevandaily.am/"
    )
    passive = _source(pack, "VirusTotal passive DNS")
    assert passive.query_value("https://yerevandaily.am/1489-2/") == "yerevandaily.am"


@pytest.mark.parametrize(
    ("seed", "a_labels"),
    [
        ("հայլրատվություն.հայ", "xn--y9aayks5adipc6dll6ad.xn--y9a3aq"),
        ("այսօր.հայ", "xn--y9a4byamy.xn--y9a3aq"),
        ("xn--y9a4byamy.xn--y9a3aq", "xn--y9a4byamy.xn--y9a3aq"),
    ],
)
def test_an_internationalised_domain_is_investigated_like_any_other(seed, a_labels):
    """Eight of the report's domains are written in Armenian script: they are
    domains, and every registry is asked for their A-labels."""
    pack = disinfo_infrastructure_pack(VT)
    assert packs.classify_subject(seed) == "domain"
    assert _sources(pack, "domain", seed) == _sources(pack, "domain", "yerevandaily.am")
    assert _source(pack, "VirusTotal passive DNS").query_value(seed) == a_labels
    assert _source(pack, "Site fingerprint").fingerprint_url(seed) == f"https://{a_labels}/"
    rdap = _source(pack, "RDAP registration (host)")
    assert rdap.build_url(seed).endswith(f"/{a_labels}")


@pytest.mark.parametrize("value", ["Ազատ մամուլ", "Storm-1516", "մամուլ", "foo bar.am"])
def test_text_that_is_not_a_host_name_stays_unknown(value):
    assert packs.classify_subject(value) == "unknown"


def test_an_ip_names_its_network_then_the_news_domains_it_hosted():
    pack = disinfo_infrastructure_pack(VT)
    assert _sources(pack, "ip", "91.239.146.131") == [
        "RDAP registration (IP)",
        "VirusTotal co-hosted news domains",
        "Shodan InternetDB",
    ]


def test_without_virustotal_the_pack_still_qualifies_and_says_what_is_missing():
    pack = disinfo_infrastructure_pack(None)
    assert "passive_dns" not in {s.kind for s in pack.sources}
    assert _sources(pack, "domain", "yerevandaily.am") == [
        "Site fingerprint",
        "RDAP registration (domain)",
        "Web research",
    ]
    assert "Connect a VirusTotal integration" in pack.description
    # The integration actually used is named, so the quota spent is not a
    # surprise.
    assert "'Team VirusTotal'" in disinfo_infrastructure_pack(VT).description


def test_the_pack_is_never_chosen_for_the_analyst():
    """The fingerprint contacts the investigated site and passive DNS spends a
    person's quota: both are a decision taken in the picker."""
    pack = disinfo_infrastructure_pack(VT)
    assert pack.auto_select is False
    assert DISINFO_INFRASTRUCTURE_SLUG not in packs.BUILTIN_PACKS
    for subject_type in ("domain", "url", "ip"):
        assert DISINFO_INFRASTRUCTURE_SLUG not in {
            p.slug for p in recommend_packs(subject_type)
        }
    assert "fetches the sites themselves" in pack.description


def test_lookups_shared_with_the_general_pack_are_planned_once():
    pack = disinfo_infrastructure_pack(VT)
    _planned, executable = plan_for(
        [pack, packs.WEB_GENERAL], "domain", "yerevandaily.am"
    )
    names = [source.name for _slug, source in executable]
    assert names.count("RDAP registration (domain)") == 1
    assert names.count("Certificate Transparency (crt.sh)") == 1


def test_the_filter_is_shown_in_what_the_step_asked():
    source = _source(disinfo_infrastructure_pack(VT), "VirusTotal co-hosted news domains")
    asked = source.describe("91.239.146.131")
    assert asked.startswith("VirusTotal resolutions of 91.239.146.131")
    assert NEWS_OUTLET_KEYWORDS.pattern in asked


@pytest.mark.parametrize(
    ("host", "kept"),
    [
        # 91.239.146.131 in the CheckFirst report: three Armenia-focused and
        # two France-focused Storm-1516 domains.
        ("haynewsnow.com", True),
        ("yerevandaily.am", True),
        ("lratu.com", True),
        ("journaldujour.net", True),
        ("lespartisans.net", True),
        # French words the France-campaign domains are named with.
        ("republiquelibre.fr", True),
        ("marseillematch.fr", True),
        ("prisonnierdumacronisme.fr", True),
        ("figaroquotidien.com", True),
        ("armenianpress.am", True),
        # `actu` is inside too many words to say anything about a site.
        ("trailermanufacturerdubai.com", False),
        # Internationalised names are read decoded, in Armenian script:
        # հայլրատվություն.հայ and հայմեդիա.հայ from the report's annex.
        ("xn--y9aayks5adipc6dll6ad.xn--y9a3aq", True),
        ("հայմեդիա.հայ".encode("idna").decode(), True),
    ],
)
def test_the_newsroom_words_keep_the_names_that_read_like_a_newsroom(host, kept):
    assert NEWS_OUTLET_KEYWORDS.matches(host) is kept


# ── The two kinds: identity and stored address ──────────────────────────────


def test_two_fingerprints_of_one_set_are_one_source():
    a = SourceDefinition(
        name="A", subject_types=("domain",), kind="site_fingerprint",
        fingerprint_set="wordpress-media-network",
    )
    b = SourceDefinition(
        name="B", subject_types=("domain",), kind="site_fingerprint",
        fingerprint_set="wordpress-media-network",
    )
    assert a.identity() == b.identity()


def test_passive_dns_differs_by_integration_and_by_filter():
    base = {"name": "P", "subject_types": ("ip",), "kind": "passive_dns"}
    one = SourceDefinition(**base, integration_id="a")
    other_key = SourceDefinition(**base, integration_id="b")
    filtered = SourceDefinition(**base, integration_id="a", hostname_keywords="news-outlet")
    assert len({one.identity(), other_key.identity(), filtered.identity()}) == 3


@pytest.mark.parametrize("subject_type", ["domain", "url", "ip"])
def test_every_source_of_the_pack_survives_its_stored_address(subject_type):
    for source in disinfo_infrastructure_pack(VT).sources_for(subject_type):
        rebuilt = SourceDefinition.from_ref(source.to_ref(), subject_types=(subject_type,))
        assert rebuilt.identity() == source.identity()
        assert rebuilt.kind == source.kind


@pytest.mark.parametrize(
    "ref",
    [
        {"kind": "site_fingerprint", "name": "F"},
        {"kind": "site_fingerprint", "name": "F", "fingerprint_set": "gone"},
        {"kind": "passive_dns", "name": "P"},
        {"kind": "passive_dns", "name": "P", "integration_id": "a", "hostname_keywords": "gone"},
    ],
)
def test_an_address_this_build_cannot_follow_is_refused(ref):
    """Refused, so a continuation says the step is not continuable instead of
    querying something else under its name."""
    with pytest.raises(ValueError):
        SourceDefinition.from_ref(ref, subject_types=("domain",))


# ── Site fingerprint ────────────────────────────────────────────────────────

_PAGE = """<!DOCTYPE html><html lang="hy"><head>
<!-- Search Engine Optimization by Rank Math - https://rankmath.com/ -->
<link rel='stylesheet' id='fox-style-css' href='https://yerevandaily.am/wp-content/themes/fox/style.css?ver=4.6' media='all' />
<link rel="https://api.w.org/" href="https://yerevandaily.am/wp-json/" />
</head><body><article>Լուրեր</article></body></html>
<!-- Page optimized by LiteSpeed Cache @2026-05-28 10:12:01 -->"""

_HOSTINGER = {
    "server": "LiteSpeed",
    "platform": "hostinger",
    "panel": "hpanel",
    "x-litespeed-cache": "hit",
}


def _page(body: str = _PAGE, headers: dict | None = None, url: str = "https://yerevandaily.am/") -> FetchResult:
    raw = body.encode()
    return FetchResult(
        url=url, title=None, text="", content_type="text/html", status_code=200,
        char_count=0, body=raw, headers=dict(_HOSTINGER if headers is None else headers),
    )


async def _fingerprint(monkeypatch, result=None, *, raises=None, subject="yerevandaily.am"):
    requested: list[str] = []

    async def _fetch(url, **_kwargs):
        requested.append(url)
        if raises is not None:
            raise raises
        return result

    monkeypatch.setattr(queriers, "fetch_and_extract", _fetch)
    source = _source(disinfo_infrastructure_pack(None), "Site fingerprint")
    outcome = await queriers.make_site_fingerprint_querier()(
        source, subject, source.describe(subject)
    )
    return outcome, requested


@pytest.mark.asyncio
async def test_every_signal_the_site_shows_is_its_own_cited_finding(monkeypatch):
    outcome, requested = await _fingerprint(monkeypatch, _page())
    assert requested == ["https://yerevandaily.am/"]
    labels = [e.label.split(" — ")[0] for e in outcome.evidence]
    assert labels == [
        "WordPress theme Fox",
        "Rank Math SEO plugin",
        "LiteSpeed Cache plugin",
        "LiteSpeed Cache response header",
        "LiteSpeed web server",
        "Hostinger hosting platform",
        "Hostinger control panel (hPanel)",
        "WordPress REST API",
    ]
    assert outcome.findings_count == 8
    assert outcome.error_type is None and outcome.partial is False
    assert outcome.confidence is None
    assert outcome.detail_code == "source.fingerprint_matched"
    assert outcome.detail_params == {"matched": 8, "tested": 8}
    assert set(outcome.detail_params) == DETAIL_CODE_PARAMS[outcome.detail_code]
    for evidence in outcome.evidence:
        assert evidence.href == "https://yerevandaily.am/"
        # Quotable, or the report cannot cite a single one of them.
        assert is_quotable(evidence.quote), evidence.quote


@pytest.mark.asyncio
async def test_the_quote_carries_the_matched_source_verbatim(monkeypatch):
    outcome, _ = await _fingerprint(monkeypatch, _page())
    theme = outcome.evidence[0].quote
    assert "/wp-content/themes/fox/style.css" in theme
    assert "answered HTTP 200" in theme
    header = next(e for e in outcome.evidence if e.label.startswith("Hostinger hosting"))
    assert "platform: hostinger" in header.quote


@pytest.mark.asyncio
async def test_a_site_built_otherwise_is_empty_not_an_error(monkeypatch):
    outcome, _ = await _fingerprint(
        monkeypatch,
        _page("<html><head><title>News</title></head><body>Hello</body></html>", headers={}),
    )
    assert outcome.findings_count == 0
    assert outcome.error_type is None
    assert outcome.detail_params == {"matched": 0, "tested": 8}
    # What was read is still named.
    assert [e.href for e in outcome.evidence] == ["https://yerevandaily.am/"]


@pytest.mark.asyncio
async def test_a_page_too_large_to_test_whole_is_degraded(monkeypatch):
    big = _PAGE + " " * (queriers.FINGERPRINT_MAX_BYTES + 1)
    outcome, _ = await _fingerprint(monkeypatch, _page(big))
    assert outcome.partial is True
    assert outcome.detail_code == "source.fingerprint_capped"
    assert set(outcome.detail_params) == DETAIL_CODE_PARAMS["source.fingerprint_capped"]


@pytest.mark.asyncio
async def test_a_site_that_is_gone_fails_like_any_page(monkeypatch):
    request = httpx.Request("GET", "https://yerevandaily.am/")
    gone = httpx.HTTPStatusError(
        "HTTP 404", request=request, response=httpx.Response(404, request=request)
    )
    outcome, _ = await _fingerprint(monkeypatch, raises=gone)
    assert outcome.error_type == "not_found"
    assert outcome.detail_code == "source.http_status"


@pytest.mark.asyncio
async def test_the_fingerprint_reads_no_more_than_it_tests(monkeypatch):
    """The investigated site decides the response size: the fetch is capped."""
    asked: list[dict] = []

    async def _fetch(url, **kwargs):
        asked.append(kwargs)
        result = _page()
        result.body_capped = True
        return result

    monkeypatch.setattr(queriers, "fetch_and_extract", _fetch)
    source = _source(disinfo_infrastructure_pack(None), "Site fingerprint")
    outcome = await queriers.make_site_fingerprint_querier()(source, "yerevandaily.am", "")
    assert [kwargs["max_bytes"] for kwargs in asked] == [queriers.FINGERPRINT_MAX_BYTES]
    assert outcome.partial is True
    assert outcome.detail_code == "source.fingerprint_capped"


def _schemes(monkeypatch, answers: dict[str, object]) -> list[str]:
    """Answer per scheme: a FetchResult, or an exception to raise."""
    requested: list[str] = []

    async def _fetch(url, **_kwargs):
        requested.append(url)
        answer = answers[url.split("://", 1)[0]]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(queriers, "fetch_and_extract", _fetch)
    return requested


async def _run_fingerprint(subject: str):
    source = _source(disinfo_infrastructure_pack(None), "Site fingerprint")
    return await queriers.make_site_fingerprint_querier()(source, subject, source.describe(subject))


@pytest.mark.asyncio
async def test_a_bare_host_unreachable_over_https_is_read_over_http(monkeypatch):
    requested = _schemes(
        monkeypatch,
        {"https": httpx.ConnectError("refused"), "http": _page(url="http://yerevandaily.am/")},
    )
    outcome = await _run_fingerprint("yerevandaily.am")
    assert requested == ["https://yerevandaily.am/", "http://yerevandaily.am/"]
    assert outcome.error_type is None
    assert {e.href for e in outcome.evidence} == {"http://yerevandaily.am/"}


@pytest.mark.asyncio
async def test_unreachable_over_both_schemes_says_both(monkeypatch):
    requested = _schemes(
        monkeypatch, {"https": httpx.ConnectError("tls refused"), "http": httpx.ConnectError("port closed")}
    )
    outcome = await _run_fingerprint("yerevandaily.am")
    assert len(requested) == 2
    assert outcome.error_type == "unreachable"
    assert outcome.detail == "over HTTPS: tls refused; over HTTP: port closed"


@pytest.mark.asyncio
async def test_a_site_that_answered_over_https_is_not_retried(monkeypatch):
    request = httpx.Request("GET", "https://yerevandaily.am/")
    forbidden = httpx.HTTPStatusError("HTTP 403", request=request, response=httpx.Response(403, request=request))
    requested = _schemes(monkeypatch, {"https": forbidden, "http": _page()})
    outcome = await _run_fingerprint("yerevandaily.am")
    assert requested == ["https://yerevandaily.am/"]
    assert outcome.detail_code == "source.http_status"


def _status_error(status: int, url: str = "https://yerevandaily.am/") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=httpx.Response(status, request=request))


@pytest.mark.asyncio
async def test_a_site_that_answered_5xx_over_https_is_not_retried(monkeypatch):
    """A 503 is `unreachable` in the taxonomy, but it is an answer."""
    requested = _schemes(monkeypatch, {"https": _status_error(503), "http": _page()})
    outcome = await _run_fingerprint("yerevandaily.am")
    assert requested == ["https://yerevandaily.am/"]
    assert outcome.detail_code == "source.http_status"
    assert outcome.detail_params == {"status": 503}


def test_the_fallback_reads_whether_the_site_answered_not_a_detail_code():
    from app.services.investigation_orchestrator import SourceOutcome

    no_code = SourceOutcome(error_type="unreachable", detail="HTTP 503")
    assert queriers._not_reached(no_code, answered=True) is False
    assert queriers._not_reached(no_code, answered=False) is True
    assert queriers._not_reached(SourceOutcome(error_type="timeout"), answered=False) is False
    assert queriers._not_reached(None, answered=False) is False


@pytest.mark.asyncio
async def test_a_5xx_over_http_is_the_answer_the_site_gave(monkeypatch):
    requested = _schemes(
        monkeypatch,
        {"https": httpx.ConnectError("refused"), "http": _status_error(502, "http://yerevandaily.am/")},
    )
    outcome = await _run_fingerprint("yerevandaily.am")
    assert len(requested) == 2
    assert outcome.detail_code == "source.http_status"
    assert outcome.detail_params == {"status": 502}


@pytest.mark.asyncio
async def test_a_url_subject_is_read_where_it_names_and_only_there(monkeypatch):
    source = _source(disinfo_infrastructure_pack(None), "Site fingerprint")
    assert source.fingerprint_url("http://yerevandaily.am:8080/news/1") == "http://yerevandaily.am:8080/"
    assert source.fingerprint_url("https://yerevandaily.am:99999/") == "https://yerevandaily.am/"
    assert source.fingerprint_url("ftp://yerevandaily.am/") == "https://yerevandaily.am/"
    assert source.fingerprint_url("http://[2001:db8::1]:8080/x") == "http://[2001:db8::1]:8080/"
    requested = _schemes(monkeypatch, {"https": httpx.ConnectError("refused"), "http": _page()})
    outcome = await _run_fingerprint("https://yerevandaily.am/1489-2/")
    assert requested == ["https://yerevandaily.am/"]
    assert outcome.error_type == "unreachable"


@pytest.mark.asyncio
async def test_a_redirect_is_reported_where_it_landed(monkeypatch):
    outcome, _ = await _fingerprint(monkeypatch, _page(url="https://www.yerevandaily.am/"))
    assert {e.href for e in outcome.evidence} == {"https://www.yerevandaily.am/"}


# ── Passive DNS ─────────────────────────────────────────────────────────────


def _resolution(host: str, ip: str, date: int = 1780000000, resolver: str = "VirusTotal") -> dict:
    return {
        "type": "resolution",
        "id": f"{ip}{host}",
        "attributes": {"host_name": host, "ip_address": ip, "date": date, "resolver": resolver},
    }


def _vt(status: int = 200, payload=None, *, seen: list | None = None, raises=None):
    def _handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if raises is not None:
            raise raises
        if isinstance(payload, str):
            return httpx.Response(status, text=payload)
        return httpx.Response(status, json=payload if payload is not None else {"data": []})

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler))


async def _key(_integration_id, _user_id):
    return "vt-key"


async def _no_key(_integration_id, _user_id):
    return None


async def _passive(name: str, subject: str, client_factory, key_loader=_key):
    source = _source(disinfo_infrastructure_pack(VT), name)
    querier = queriers.make_passive_dns_querier(
        user_id="u", key_loader=key_loader, client_factory=client_factory
    )
    return await querier(source, subject, source.describe(subject))


@pytest.mark.asyncio
async def test_a_domain_finds_each_ip_it_resolved_to(monkeypatch):
    seen: list[httpx.Request] = []
    payload = {
        "data": [
            _resolution("yerevandaily.am", "91.239.146.131"),
            _resolution("yerevandaily.am", "91.239.146.131", date=1770000000),
            _resolution("yerevandaily.am", "45.84.205.141"),
        ]
    }
    outcome = await _passive("VirusTotal passive DNS", "yerevandaily.am", _vt(payload=payload, seen=seen))
    request = seen[0]
    assert request.url.path == "/api/v3/domains/yerevandaily.am/resolutions"
    assert request.url.params["limit"] == str(queriers.PASSIVE_DNS_PAGE_SIZE)
    assert request.headers["x-apikey"] == "vt-key"
    # One finding per distinct resolution, linked to the next subject.
    assert [e.label for e in outcome.evidence] == ["91.239.146.131", "45.84.205.141"]
    assert outcome.findings_count == 2
    assert outcome.evidence[0].href == "https://www.virustotal.com/gui/ip-address/91.239.146.131"
    assert outcome.partial is False and outcome.detail_code is None
    for evidence in outcome.evidence:
        assert is_quotable(evidence.quote), evidence.quote
    assert "last observed on 2026-05-28" in outcome.evidence[0].quote


@pytest.mark.asyncio
async def test_an_ip_keeps_the_newsroom_names_and_counts_the_rest():
    seen: list[httpx.Request] = []
    payload = {
        "data": [
            _resolution("haynewsnow.com", "91.239.146.131"),
            _resolution("yerevandaily.am", "91.239.146.131"),
            _resolution("journaldujour.net", "91.239.146.131"),
            _resolution("lespartisans.net", "91.239.146.131"),
            _resolution("plumber-yerevan.com", "91.239.146.131"),
        ],
        "meta": {"cursor": "next-page"},
    }
    outcome = await _passive(
        "VirusTotal co-hosted news domains", "91.239.146.131", _vt(payload=payload, seen=seen)
    )
    assert seen[0].url.path == "/api/v3/ip_addresses/91.239.146.131/resolutions"
    assert [e.label for e in outcome.evidence] == [
        "haynewsnow.com",
        "yerevandaily.am",
        "journaldujour.net",
        "lespartisans.net",
    ]
    assert outcome.evidence[2].href == "https://www.virustotal.com/gui/domain/journaldujour.net"
    # VirusTotal holds more than the pages read: both facts are said, so the
    # match count never reads as the whole shared IP.
    assert outcome.detail_code == "source.passive_dns_filtered_more"
    assert outcome.detail_params == {"kept": 4, "fetched": 5}
    assert set(outcome.detail_params) == DETAIL_CODE_PARAMS[outcome.detail_code]
    assert outcome.detail == (
        "4 of 5 resolved sites match the keywords; VirusTotal holds more that were not read"
    )
    assert outcome.partial is True


@pytest.mark.asyncio
async def test_more_resolutions_than_one_page_is_degraded_and_said():
    payload = {"data": [_resolution("yerevandaily.am", "91.239.146.131")], "links": {"next": "x"}}
    outcome = await _passive("VirusTotal passive DNS", "yerevandaily.am", _vt(payload=payload))
    assert outcome.partial is True
    assert outcome.detail_code == "source.passive_dns_more"
    assert set(outcome.detail_params) == DETAIL_CODE_PARAMS["source.passive_dns_more"]
    # The page size is a bound, not a count: one record was shown here.
    assert outcome.findings_count == 1
    assert outcome.detail.startswith("showing up to the ")


@pytest.mark.asyncio
async def test_no_resolution_is_empty_not_an_error():
    outcome = await _passive("VirusTotal passive DNS", "yerevandaily.am", _vt(payload={"data": None}))
    assert outcome.findings_count == 0 and outcome.error_type is None


@pytest.mark.asyncio
async def test_without_a_key_the_caller_may_use_nothing_is_called():
    seen: list[httpx.Request] = []
    outcome = await _passive(
        "VirusTotal passive DNS", "yerevandaily.am", _vt(seen=seen), key_loader=_no_key
    )
    assert seen == []
    assert outcome.error_type == "not_found"
    assert outcome.detail_code == "source.vt_unavailable"


@pytest.mark.asyncio
async def test_no_caller_means_no_key():
    """A person's quota is never spent on behalf of nobody."""
    assert await queriers.load_virustotal_api_key(VT[0], None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "auth_error"), (403, "auth_error"), (404, "not_found"), (429, "rate_limited"), (503, "unreachable")],
)
async def test_virustotal_failures_map_onto_the_taxonomy(status, expected):
    outcome = await _passive(
        "VirusTotal passive DNS", "yerevandaily.am", _vt(status, {"error": {"code": "X"}})
    )
    assert outcome.error_type == expected
    assert outcome.detail_code == "source.http_status"


@pytest.mark.asyncio
async def test_an_answer_that_is_not_json_is_unusable():
    outcome = await _passive("VirusTotal passive DNS", "yerevandaily.am", _vt(200, "<html>"))
    assert outcome.error_type == "invalid_response"


@pytest.mark.asyncio
async def test_a_virustotal_that_does_not_answer_is_a_timeout():
    outcome = await _passive(
        "VirusTotal passive DNS",
        "yerevandaily.am",
        _vt(raises=httpx.ReadTimeout("slow")),
    )
    assert outcome.error_type == "timeout"


@pytest.mark.asyncio
async def test_markup_in_a_host_name_cannot_reach_a_label():
    payload = {"data": [_resolution("evil[x](javascript:alert)news.com", "91.239.146.131")]}
    outcome = await _passive("VirusTotal co-hosted news domains", "91.239.146.131", _vt(payload=payload))
    assert "[" not in outcome.evidence[0].label and "]" not in outcome.evidence[0].label


# ── Routing ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_dispatcher_answers_both_kinds(monkeypatch):
    async def _fetch(url, **_kwargs):
        return _page()

    monkeypatch.setattr(queriers, "fetch_and_extract", _fetch)
    querier = queriers.make_dispatching_querier(db=None, user_id=None)
    fingerprint = _source(disinfo_infrastructure_pack(VT), "Site fingerprint")
    outcome = await querier(fingerprint, "yerevandaily.am", fingerprint.describe("yerevandaily.am"))
    assert outcome.findings_count == 8

    # No caller: the passive-DNS source answers, and answers that it may not.
    passive = _source(disinfo_infrastructure_pack(VT), "VirusTotal passive DNS")
    outcome = await querier(passive, "yerevandaily.am", passive.describe("yerevandaily.am"))
    assert outcome.detail_code == "source.vt_unavailable"


# ── Passive DNS paging (filtered sources) ───────────────────────────────────


def _paged_vt(pages: dict, *, seen: list, fail_on: str | None = None):
    """A VirusTotal that serves ``pages[cursor]`` and fails on ``fail_on``."""

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        cursor = request.url.params.get("cursor", "")
        if fail_on is not None and cursor == fail_on:
            return httpx.Response(429, json={"error": {"code": "QuotaExceededError"}})
        return httpx.Response(200, json=pages[cursor])

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler))


def _vt_page(hosts: list[str], cursor: str | None) -> dict:
    body: dict = {"data": [_resolution(h, "2.56.204.6") for h in hosts]}
    if cursor:
        body["meta"] = {"cursor": cursor}
    return body


@pytest.mark.asyncio
async def test_a_filtered_ip_reads_past_the_newest_page():
    """The network's domains sit under the newest, unrelated resolutions."""
    seen: list[httpx.Request] = []
    pages = {
        "": _vt_page(["plumber-yerevan.com", "dentist.example"], "p2"),
        "p2": _vt_page(["hayinfo.am", "xn--y9aayks5adipc6dll6ad.xn--y9a3aq"], None),
    }
    outcome = await _passive("VirusTotal co-hosted news domains", "2.56.204.6", _paged_vt(pages, seen=seen))
    assert [r.url.params.get("cursor") for r in seen] == [None, "p2"]
    # Read as people read it, linked as VirusTotal names it.
    assert [e.label for e in outcome.evidence] == ["hayinfo.am", "հայլրատվություն.հայ"]
    assert outcome.evidence[1].href.endswith("/domain/xn--y9aayks5adipc6dll6ad.xn--y9a3aq")
    assert "հայլրատվություն.հայ (xn--y9aayks5adipc6dll6ad.xn--y9a3aq)" in outcome.evidence[1].quote
    assert outcome.detail_params == {"kept": 2, "fetched": 4}
    assert outcome.detail_code == "source.passive_dns_filtered"
    assert outcome.partial is False


@pytest.mark.asyncio
async def test_paging_stops_at_its_bound_and_says_more_exists():
    seen: list[httpx.Request] = []
    pages = {"": _vt_page(["a-news.com"], "c1")}
    bound = queriers.PASSIVE_DNS_FILTERED_MAX_PAGES
    pages.update({f"c{i}": _vt_page([f"b{i}-daily.com"], f"c{i + 1}") for i in range(1, bound + 5)})
    outcome = await _passive("VirusTotal co-hosted news domains", "2.56.204.6", _paged_vt(pages, seen=seen))
    assert len(seen) == queriers.PASSIVE_DNS_FILTERED_MAX_PAGES
    assert outcome.partial is True
    assert outcome.detail_code == "source.passive_dns_filtered_more"
    assert outcome.detail_params == {"kept": bound, "fetched": bound}


@pytest.mark.asyncio
async def test_a_page_that_fails_after_the_first_keeps_what_was_read():
    seen: list[httpx.Request] = []
    pages = {"": _vt_page(["hayinfo.am"], "p2")}
    outcome = await _passive(
        "VirusTotal co-hosted news domains", "2.56.204.6", _paged_vt(pages, seen=seen, fail_on="p2")
    )
    assert outcome.error_type is None
    assert [e.label for e in outcome.evidence] == ["hayinfo.am"]
    assert outcome.partial is True
    assert outcome.detail_code == "source.passive_dns_filtered_more"


@pytest.mark.asyncio
async def test_an_unfiltered_domain_reads_one_page():
    seen: list[httpx.Request] = []
    pages = {"": _vt_page(["yerevandaily.am"], "p2"), "p2": _vt_page(["never-read.am"], None)}
    outcome = await _passive("VirusTotal passive DNS", "yerevandaily.am", _paged_vt(pages, seen=seen))
    assert len(seen) == 1
    assert outcome.detail_code == "source.passive_dns_more"


# ── What a filter reads of a host name ──────────────────────────────────────


@pytest.mark.parametrize(
    ("host", "kept"),
    [
        # The TLD is not a word about the site.
        ("smartsettlementconcepts.info", False),
        ("vaisov.live", False),
        # A number counts when it stands alone.
        ("av246810.live", False),
        ("yerkir24.am", True),
        ("24arm.am", True),
        ("lur360.com", True),
        # The panel's host names are read as their site.
        ("autodiscover.euexpresss.com", True),
    ],
)
def test_the_filter_reads_the_site_without_its_tld(host, kept):
    assert NEWS_OUTLET_KEYWORDS.matches(host) is kept


@pytest.mark.asyncio
async def test_a_sites_panel_host_names_are_one_finding():
    payload = {
        "data": [
            _resolution("cpanel.parispresslibre.com", "208.109.215.186", date=1780000300),
            _resolution("www.parispresslibre.com", "208.109.215.186", date=1780000200),
            _resolution("parispresslibre.com", "208.109.215.186", date=1780000100),
            _resolution("webmail.presse-alerte.fr", "208.109.215.186"),
        ]
    }
    outcome = await _passive("VirusTotal co-hosted news domains", "208.109.215.186", _vt(payload=payload))
    assert [e.label for e in outcome.evidence] == ["parispresslibre.com", "presse-alerte.fr"]
    assert outcome.evidence[0].href.endswith("/domain/parispresslibre.com")
    # The record cited is the one VirusTotal holds, named as it holds it.
    assert "cpanel.parispresslibre.com, a host name of the site parispresslibre.com," in outcome.evidence[0].quote
    assert outcome.detail_params == {"kept": 2, "fetched": 2}


def test_the_newsroom_words_keep_most_of_the_reports_domains():
    """Pinned so a change to the words says what it costs in coverage."""
    report = ["armenianview.am", "haypressinfo.com", "reportarmenia.am", "lratvakanaliq.am", "haynewsnow.com", "armbreaking.com", "lernashkharh.am", "tavushmedia.com", "geghamalerner.am", "armarz.am", "hayapaga.am", "yerkir24.am", "yerevandailynews.am", "armcomment.am", "armtrend.am", "hayhotnews.com", "hayinstant.com", "hayreniknews.am", "mamulservice.am", "hayresearch.am", "teghekatu24.am", "yerevandaily.am", "armlive24.com", "armenianpress.am", "arevmedia.am", "armenianjournal.am", "armenianews24.am", "armreporter.am", "lur360.com", "karevorimasin.am", "hayvoice.am", "zhamarzham.am", "lureriashkharh.am", "zrutsakits.am", "armword.am", "nshanaket.am", "azdar.am", "shiraktesutyun.am", "lratu.com", "24arm.am", "armdailynews.com", "armenianway.am", "hayinfo.am", "hayobserver.com", "khosnak.am", "noymedia.am", "yerevanpress.am", "yerevantimes.am", "presse-alerte.fr", "journalnational.fr", "le-principal-hebdomadaire.fr", "france-reportage.fr", "lyonnais-express.fr", "prisonnierdumacronisme.fr", "lire-lhebdo.fr", "marseillematch.fr", "enpleinessorfr.com", "visiondumonde.net", "lobservateurdujour.fr", "lechiquier24.fr", "republiquelibre.fr", "paris-today.fr", "figaroquotidien.com", "journaldujour.net", "lespartisans.net"]
    idn = ["լրաբեր.հայ", "այսօր.հայ", "տեսակետ.հայ", "թարմլուրեր.հայ", "հայլրատվություն.հայ",
           "հայաշխարհ.հայ", "հայմեդիա.հայ", "հայօր.հայ"]
    hosts = report + [name.encode("idna").decode() for name in idn]
    assert len(hosts) == 73
    assert sum(NEWS_OUTLET_KEYWORDS.matches(h) for h in hosts) == 56
    # The pivot's point in the demo: every France-campaign domain of the
    # report survives the triage on the IP it shares with the network.
    french = report[report.index("presse-alerte.fr"):]
    assert len(french) == 17
    assert [h for h in french if not NEWS_OUTLET_KEYWORDS.matches(h)] == []


@pytest.mark.asyncio
async def test_passive_dns_follows_the_relay_route_of_its_virustotal_integration(monkeypatch):
    """An admin may route the VirusTotal integration through a relay group:
    the querier's client is an `egress_client` pinned to that route,
    looked up for the integration the source names."""
    import app.core.egress as egress

    route = object()
    asked: list = []
    built: list[dict] = []

    async def _route(integration_id):
        asked.append(integration_id)
        return route

    real = egress.egress_client

    def _spy(*args, **kwargs):
        built.append(dict(kwargs))
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"data": []})))

    monkeypatch.setattr(queriers, "load_virustotal_route", _route)
    monkeypatch.setattr(egress, "egress_client", _spy)
    source = _source(disinfo_infrastructure_pack(VT), "VirusTotal passive DNS")
    querier = queriers.make_passive_dns_querier(user_id="u", key_loader=_key)
    await querier(source, "yerevandaily.am", source.describe("yerevandaily.am"))
    assert asked == [source.integration_id]
    assert len(built) == 1 and built[0]["route"] is route
    assert real is not _spy
    assert built[0]["timeout"] == queriers.DEFAULT_SOURCE_TIMEOUT


@pytest.mark.asyncio
async def test_no_key_means_no_route_lookup_and_no_client(monkeypatch):
    async def _route(_integration_id):
        raise AssertionError("looked up a route for a run that has no key")

    monkeypatch.setattr(queriers, "load_virustotal_route", _route)
    source = _source(disinfo_infrastructure_pack(VT), "VirusTotal passive DNS")
    querier = queriers.make_passive_dns_querier(user_id="u", key_loader=_no_key)
    outcome = await querier(source, "yerevandaily.am", source.describe("yerevandaily.am"))
    assert outcome.detail_code == "source.vt_unavailable"


# ── Wall clock and evidence bounds ──────────────────────────────────


def _trickle(requested: list[str], answers: dict[str, object] | None = None):
    """A fetch whose site sends a byte now and then, never slow enough for a
    per-read timeout, and never done; or answers ``answers[scheme]`` first."""

    async def _fetch(url, **_kwargs):
        requested.append(url)
        answer = (answers or {}).get(url.split("://", 1)[0])
        if isinstance(answer, Exception):
            raise answer
        while True:
            await asyncio.sleep(0.01)

    return _fetch


@pytest.mark.asyncio
async def test_a_site_that_trickles_its_page_past_the_deadline_is_a_timeout(monkeypatch):
    requested: list[str] = []
    monkeypatch.setattr(queriers, "fetch_and_extract", _trickle(requested))
    source = _source(disinfo_infrastructure_pack(None), "Site fingerprint")
    querier = queriers.make_site_fingerprint_querier(deadline=0.05)
    outcome = await querier(source, "yerevandaily.am", source.describe("yerevandaily.am"))
    assert requested == ["https://yerevandaily.am/"]
    assert outcome.error_type == "timeout"
    assert outcome.detail_code == "source.timed_out"
    assert outcome.detail_params == {"seconds": 0.05}
    assert outcome.evidence == ()


@pytest.mark.asyncio
async def test_the_deadline_covers_the_http_fallback_too(monkeypatch):
    requested: list[str] = []
    monkeypatch.setattr(
        queriers, "fetch_and_extract", _trickle(requested, {"https": httpx.ConnectError("refused")})
    )
    source = _source(disinfo_infrastructure_pack(None), "Site fingerprint")
    querier = queriers.make_site_fingerprint_querier(deadline=0.05)
    outcome = await querier(source, "yerevandaily.am", source.describe("yerevandaily.am"))
    assert requested == ["https://yerevandaily.am/", "http://yerevandaily.am/"]
    assert outcome.error_type == "timeout"
    assert outcome.detail_params == {"seconds": 0.05}


def test_the_fingerprint_deadline_is_the_other_sources_budget():
    assert queriers.FINGERPRINT_STEP_TIMEOUT == queriers.SOURCE_CALL_TIMEOUT


def _slow_vt(pages: dict, *, seen: list, slow_on: str):
    """A VirusTotal that serves ``pages[cursor]`` and never answers ``slow_on``."""

    async def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        cursor = request.url.params.get("cursor", "")
        if cursor == slow_on:
            while True:
                await asyncio.sleep(0.01)
        return httpx.Response(200, json=pages[cursor])

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler))


async def _passive_with(source: SourceDefinition, subject: str, client_factory, **options):
    querier = queriers.make_passive_dns_querier(
        user_id="u", key_loader=_key, client_factory=client_factory, **options
    )
    return await querier(source, subject, source.describe(subject))


@pytest.mark.asyncio
async def test_a_paging_read_the_deadline_cuts_keeps_its_pages_and_says_more_exists():
    seen: list[httpx.Request] = []
    pages = {"": _vt_page(["hayinfo.am"], "p2")}
    source = _source(disinfo_infrastructure_pack(VT), "VirusTotal co-hosted news domains")
    outcome = await _passive_with(
        source, "2.56.204.6", _slow_vt(pages, seen=seen, slow_on="p2"), deadline=0.05
    )
    assert [r.url.params.get("cursor") for r in seen] == [None, "p2"]
    assert outcome.error_type is None
    assert [e.label for e in outcome.evidence] == ["hayinfo.am"]
    assert outcome.partial is True
    assert outcome.detail_code == "source.passive_dns_filtered_more"
    assert outcome.detail_params == {"kept": 1, "fetched": 1}


@pytest.mark.asyncio
async def test_a_first_page_past_the_deadline_is_a_timeout():
    seen: list[httpx.Request] = []
    source = _source(disinfo_infrastructure_pack(VT), "VirusTotal co-hosted news domains")
    outcome = await _passive_with(source, "2.56.204.6", _slow_vt({}, seen=seen, slow_on=""), deadline=0.05)
    assert len(seen) == 1
    assert outcome.error_type == "timeout"
    assert outcome.detail_code == "source.timed_out"
    assert outcome.detail_params == {"seconds": 0.05}


def _many_pages(hosts: list[str], ip: str | None = None) -> dict:
    """``hosts`` served a page at a time, cursors ``""``, ``p1``, ``p2``...;
    each resolution to ``ip``, or to an IP of its own."""
    size = queriers.PASSIVE_DNS_PAGE_SIZE
    chunks = [hosts[i : i + size] for i in range(0, len(hosts), size)]
    pages = {}
    for n, chunk in enumerate(chunks):
        body: dict = {
            "data": [
                _resolution(host, ip or f"45.84.{(n * size + i) // 250}.{(n * size + i) % 250 + 1}")
                for i, host in enumerate(chunk)
            ]
        }
        if n + 1 < len(chunks):
            body["meta"] = {"cursor": f"p{n + 1}"}
        pages["" if n == 0 else f"p{n}"] = body
    return pages


@pytest.mark.asyncio
async def test_a_filtered_read_past_the_evidence_cap_lists_the_newest_and_says_so():
    seen: list[httpx.Request] = []
    cap = queriers.PASSIVE_DNS_MAX_EVIDENCE
    hosts = [f"n{i}-daily.com" for i in range(cap + 20)]
    pages = _many_pages(hosts, ip="2.56.204.6")
    source = _source(disinfo_infrastructure_pack(VT), "VirusTotal co-hosted news domains")
    outcome = await _passive_with(source, "2.56.204.6", _paged_vt(pages, seen=seen))
    assert len(seen) == len(pages)
    # Every match is counted, the newest `cap` are cited.
    assert outcome.findings_count == cap + 20
    assert [e.label for e in outcome.evidence] == hosts[:cap]
    # VirusTotal's last page was read: not "more", but the list is cut.
    assert outcome.detail_code == "source.passive_dns_filtered"
    assert outcome.detail_params == {"kept": cap + 20, "fetched": cap + 20, "shown": cap}
    assert outcome.detail == (
        f"{cap + 20} of {cap + 20} resolved sites match the keywords; only the {cap} most recent are shown"
    )
    assert outcome.partial is True


@pytest.mark.asyncio
async def test_an_unfiltered_read_past_the_evidence_cap_is_degraded():
    seen: list[httpx.Request] = []
    cap = queriers.PASSIVE_DNS_MAX_EVIDENCE
    pages = _many_pages(["yerevandaily.am"] * (cap + 20))
    source = dataclasses.replace(
        _source(disinfo_infrastructure_pack(VT), "VirusTotal passive DNS"), max_pages=len(pages)
    )
    outcome = await _passive_with(source, "yerevandaily.am", _paged_vt(pages, seen=seen))
    assert len(seen) == len(pages)
    assert outcome.findings_count == cap + 20
    assert len(outcome.evidence) == cap
    assert outcome.detail_code == "source.passive_dns_more"
    assert outcome.detail_params == {"limit": cap}
    assert outcome.partial is True


@pytest.mark.asyncio
async def test_a_read_under_the_cap_lists_every_match():
    seen: list[httpx.Request] = []
    pages = _many_pages([f"n{i}-daily.com" for i in range(50)], ip="2.56.204.6")
    source = _source(disinfo_infrastructure_pack(VT), "VirusTotal co-hosted news domains")
    outcome = await _passive_with(source, "2.56.204.6", _paged_vt(pages, seen=seen))
    assert len(seen) == 2
    assert len(outcome.evidence) == 50
    assert outcome.detail_params == {"kept": 50, "fetched": 50}
    assert outcome.partial is False


def _limited_vt(pages: dict, *, seen: list, retry_after: str, on: str):
    """A VirusTotal whose first answer for ``on`` is a 429 naming ``retry_after``."""
    refused: set[str] = set()

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        cursor = request.url.params.get("cursor", "")
        if cursor == on and cursor not in refused:
            refused.add(cursor)
            return httpx.Response(429, headers={"Retry-After": retry_after}, json={"error": {"code": "Quota"}})
        return httpx.Response(200, json=pages[cursor])

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler))


@pytest.mark.asyncio
async def test_a_short_retry_after_between_pages_is_waited_out():
    seen: list[httpx.Request] = []
    pages = {"": _vt_page(["hayinfo.am"], "p2"), "p2": _vt_page(["lur360.com"], None)}
    source = _source(disinfo_infrastructure_pack(VT), "VirusTotal co-hosted news domains")
    outcome = await _passive_with(
        source, "2.56.204.6", _limited_vt(pages, seen=seen, retry_after="0", on="p2")
    )
    assert [r.url.params.get("cursor") for r in seen] == [None, "p2", "p2"]
    assert [e.label for e in outcome.evidence] == ["hayinfo.am", "lur360.com"]
    assert outcome.partial is False
    assert outcome.detail_code == "source.passive_dns_filtered"


@pytest.mark.asyncio
async def test_a_long_retry_after_keeps_what_was_read():
    """A minute's quota is not waited out: the read stops on it."""
    seen: list[httpx.Request] = []
    pages = {"": _vt_page(["hayinfo.am"], "p2"), "p2": _vt_page(["lur360.com"], None)}
    source = _source(disinfo_infrastructure_pack(VT), "VirusTotal co-hosted news domains")
    outcome = await _passive_with(
        source, "2.56.204.6", _limited_vt(pages, seen=seen, retry_after="60", on="p2")
    )
    assert [r.url.params.get("cursor") for r in seen] == [None, "p2"]
    assert [e.label for e in outcome.evidence] == ["hayinfo.am"]
    assert outcome.partial is True
    assert outcome.detail_code == "source.passive_dns_filtered_more"
