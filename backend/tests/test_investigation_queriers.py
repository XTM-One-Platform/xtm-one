"""Mapping a source fetch onto the step error taxonomy.

Every failure has to land on exactly one of ``STEP_ERROR_TYPES``, because that
vocabulary is shared by the live block, the report and the
``investigation.source_error`` metrics. A mis-mapped failure does not crash
anything — it just tells the analyst the wrong thing about why a source is
missing from the report, which is precisely the class of silent wrongness
the investigation feature exists to prevent.
"""

import httpx
import pytest

from app.services import investigation_queriers as queriers
from app.services.investigation_orchestrator import Evidence
from app.services.investigation_packs import SourceDefinition
from app.services.web_fetch import FetchResult

SOURCE = SourceDefinition(
    name="Test source",
    url_template="https://source.test/{value}",
    subject_types=("unknown",),
)


def _fetch_result(
    text: str,
    *,
    truncated: bool = False,
    url: str | None = None,
    content_type: str = "text/html",
) -> FetchResult:
    return FetchResult(
        url=url or "https://source.test/final",
        title=None,
        text=text,
        content_type=content_type,
        status_code=200,
        char_count=len(text),
        truncated=truncated,
    )


def _raise(exc):
    async def _fetch(*_args, **_kwargs):
        raise exc

    return _fetch


def _return(result):
    async def _fetch(*_args, **_kwargs):
        return result

    return _fetch


def _status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://source.test/x")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=request, response=response)


async def _query(monkeypatch, fetch) -> "queriers.SourceOutcome":
    monkeypatch.setattr(queriers, "fetch_and_extract", fetch)
    querier = queriers.make_web_fetch_querier()
    return await querier(SOURCE, "8.8.8.8", SOURCE.build_url("8.8.8.8"))


# ---------------------------------------------------------------------------
# HTTP status mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (429, "rate_limited"),
        (404, "not_found"),
        (410, "not_found"),
        (401, "auth_error"),
        (403, "auth_error"),
        (500, "unreachable"),
        (503, "unreachable"),
        (418, "invalid_response"),
    ],
)
async def test_status_codes_map_onto_the_taxonomy(monkeypatch, status_code, expected):
    outcome = await _query(monkeypatch, _raise(_status_error(status_code)))
    assert outcome.error_type == expected
    assert outcome.findings_count == 0


@pytest.mark.asyncio
async def test_a_5xx_is_unreachable_rather_than_invalid_response(monkeypatch):
    """The remediation differs: a 500 means the source could not answer at all.

    ``invalid_response`` is reserved for a source that DID answer with
    something unusable, which the analyst chases differently.
    """
    outcome = await _query(monkeypatch, _raise(_status_error(502)))
    assert outcome.error_type == "unreachable"


@pytest.mark.asyncio
async def test_every_mapped_error_type_is_in_the_shared_vocabulary(monkeypatch):
    from app.models.investigation import STEP_ERROR_TYPES

    for status_code in (429, 404, 401, 500, 418):
        outcome = await _query(monkeypatch, _raise(_status_error(status_code)))
        assert outcome.error_type in STEP_ERROR_TYPES


# ---------------------------------------------------------------------------
# Transport failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_timeout_is_reported_as_a_timeout(monkeypatch):
    outcome = await _query(monkeypatch, _raise(httpx.TimeoutException("slow")))
    assert outcome.error_type == "timeout"
    assert "15s" in (outcome.detail or "")


@pytest.mark.asyncio
async def test_an_asyncio_timeout_is_reported_as_a_timeout(monkeypatch):
    outcome = await _query(monkeypatch, _raise(TimeoutError()))
    assert outcome.error_type == "timeout"


@pytest.mark.asyncio
async def test_a_connection_failure_is_unreachable(monkeypatch):
    outcome = await _query(monkeypatch, _raise(httpx.ConnectError("dns")))
    assert outcome.error_type == "unreachable"


@pytest.mark.asyncio
async def test_any_other_transport_failure_is_unreachable(monkeypatch):
    outcome = await _query(monkeypatch, _raise(httpx.ReadError("reset")))
    assert outcome.error_type == "unreachable"


@pytest.mark.asyncio
async def test_an_ssrf_refusal_is_an_invalid_response(monkeypatch):
    """A refused or over-redirecting URL is a pack problem, not a transient one."""
    outcome = await _query(monkeypatch, _raise(ValueError("blocked private address")))
    assert outcome.error_type == "invalid_response"
    assert "blocked private address" in (outcome.detail or "")


# ---------------------------------------------------------------------------
# Successful fetches — and the empty/partial distinction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_page_with_content_produces_one_finding_and_its_evidence_url(
    monkeypatch,
):
    outcome = await _query(monkeypatch, _return(_fetch_result("x" * 5000, url="https://final.test/page")))
    assert outcome.error_type is None
    assert outcome.findings_count == 1
    assert outcome.evidence == (
        Evidence(kind="url", label="https://final.test/page", href="https://final.test/page"),
    )
    assert outcome.partial is False


@pytest.mark.asyncio
async def test_an_empty_page_is_not_an_error(monkeypatch):
    """The source answered and had nothing — the orchestrator derives `empty`."""
    outcome = await _query(monkeypatch, _return(_fetch_result("")))
    assert outcome.error_type is None
    assert outcome.findings_count == 0


@pytest.mark.asyncio
async def test_a_boilerplate_page_below_the_useful_threshold_counts_as_nothing(
    monkeypatch,
):
    """Cookie walls and "no results" pages must not register as findings.

    Counting them would make `empty` unreachable in practice and put the block
    back into the prior art's "everything looks fine" failure mode.
    """
    outcome = await _query(monkeypatch, _return(_fetch_result("No results found.")))
    assert outcome.findings_count == 0
    assert outcome.error_type is None
    assert "characters of text" in (outcome.detail or "")


@pytest.mark.asyncio
async def test_a_truncated_page_is_partial_not_a_clean_success(monkeypatch):
    """The analyst must know the source was only partly read."""
    outcome = await _query(monkeypatch, _return(_fetch_result("x" * 5000, truncated=True)))
    assert outcome.partial is True
    assert outcome.findings_count == 1
    assert "truncated" in (outcome.detail or "")


# ---------------------------------------------------------------------------
# The slice-wide invariants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_web_querier_never_invents_a_confidence(monkeypatch):
    """A generic page fetch gives no basis to compute one."""
    for fetch in (
        _return(_fetch_result("x" * 5000)),
        _return(_fetch_result("")),
        _raise(_status_error(429)),
        _raise(httpx.TimeoutException("slow")),
    ):
        outcome = await _query(monkeypatch, fetch)
        assert outcome.confidence is None


@pytest.mark.asyncio
async def test_the_web_querier_reports_no_leads(monkeypatch):
    """Multi-hop needs a model pass that is not part of this slice."""
    outcome = await _query(monkeypatch, _return(_fetch_result("x" * 5000)))
    assert outcome.leads == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    ["application/json", "application/rdap+json", "application/json; charset=utf-8"],
)
async def test_a_compact_structured_body_counts_as_a_finding(monkeypatch, content_type):
    """Shodan's InternetDB answers a clean IP in ~109 characters of valid JSON.

    Judged by the HTML boilerplate threshold it would be reported `empty` — a
    working source shown as having found nothing, which is exactly the silent
    wrongness the taxonomy exists to prevent.
    """
    body = '{"ip":"8.8.8.8","ports":[443],"hostnames":["dns.google"],"tags":[]}'
    outcome = await _query(monkeypatch, _return(_fetch_result(body, content_type=content_type)))
    assert outcome.findings_count == 1
    assert outcome.error_type is None


@pytest.mark.asyncio
async def test_an_html_body_of_the_same_size_still_counts_as_nothing(monkeypatch):
    """The distinction is the content type, not the length."""
    body = "x" * 109
    outcome = await _query(monkeypatch, _return(_fetch_result(body, content_type="text/html")))
    assert outcome.findings_count == 0


@pytest.mark.asyncio
async def test_an_essentially_empty_structured_body_is_still_nothing(monkeypatch):
    outcome = await _query(monkeypatch, _return(_fetch_result("{}", content_type="application/json")))
    assert outcome.findings_count == 0


@pytest.mark.asyncio
async def test_the_threshold_is_configurable(monkeypatch):
    monkeypatch.setattr(queriers, "fetch_and_extract", _return(_fetch_result("short")))
    querier = queriers.make_web_fetch_querier(min_useful_chars=1)
    outcome = await querier(SOURCE, "x", "https://source.test/x")
    assert outcome.findings_count == 1
