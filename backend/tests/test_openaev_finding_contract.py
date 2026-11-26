"""The OpenAEV finding contract (OpenAEV #9233): one parser, every reader through it.

The field names below are the ones OpenAEV answers since #9233 (chunks 4 and 5 of
OpenAEV #7848): ``POST /api/findings/search`` returns ``RelatedFindingOutput`` (assets
as objects, the latest occurrence as ``finding_inject``, no ``finding_status``) and
``GET /api/findings/{id}`` returns ``FindingOutput`` (assets as id strings,
``finding_status``, ``finding_inject_id``). A later OpenAEV change of the finding payload
is a change of ``parse_finding`` and of this file only.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.integrations.providers.openaev._common import (
    FINDING_CLOSED_STATUSES,
    FINDINGS_KEPT_AS_HISTORY,
    _fmt_finding,
    parse_finding,
)
from app.services import ctem_cases_source as source

SEARCH_ROW = {
    "finding_id": "8f1c",
    "finding_type": "cve",
    "finding_value": "CVE-2026-1234",
    "finding_created_at": "2026-11-01T10:00:00Z",
    "finding_updated_at": "2026-11-20T10:00:00Z",
    "finding_assets": [{"asset_id": "a-1", "asset_name": "web-1"}, {"asset_id": "a-2", "asset_name": ""}],
    "finding_asset_groups": [],
    "finding_inject": {"inject_id": "inj-7", "inject_title": "Scan"},
    "finding_simulation": None,
    "finding_scenario": None,
    "finding_teams": [],
    "finding_users": [],
}

GET_ANSWER = {
    "finding_id": "8f1c",
    "finding_type": "cve",
    "finding_value": "CVE-2026-1234",
    "finding_created_at": "2026-11-01T10:00:00Z",
    "finding_updated_at": "2026-11-20T10:00:00Z",
    "finding_status": "RESOLVED",
    "finding_last_tested": "2026-11-21T09:00:00Z",
    "finding_status_inject_id": "inj-8",
    "finding_retest_inject_id": None,
    "finding_inject_id": "inj-7",
    "finding_assets": ["a-1", "a-2"],
}


def test_a_search_row_parses_with_asset_objects_and_the_latest_occurrence_inject():
    f = parse_finding(SEARCH_ROW)
    assert f is not None
    assert (f.id, f.type, f.value) == ("8f1c", "cve", "CVE-2026-1234")
    assert f.status is None and not f.closed
    assert f.assets == (("a-1", "web-1"), ("a-2", ""))
    assert f.asset_ids == ("a-1", "a-2") and f.asset_labels == ("web-1", "a-2")
    assert f.inject_id == "inj-7"
    assert (f.created_at, f.updated_at) == ("2026-11-01T10:00:00Z", "2026-11-20T10:00:00Z")


def test_a_get_answer_parses_with_asset_id_strings_and_the_status():
    f = parse_finding(GET_ANSWER)
    assert f is not None
    assert f.status == "RESOLVED" and f.closed
    assert f.assets == (("a-1", ""), ("a-2", "")) and f.asset_ids == ("a-1", "a-2")
    assert f.inject_id == "inj-7" and f.last_tested == "2026-11-21T09:00:00Z"


def test_only_not_reproduced_and_resolved_close_a_finding():
    assert set(FINDING_CLOSED_STATUSES) == {"NOT_REPRODUCED", "RESOLVED"}
    for status, closed in (("OPEN", False), ("not_reproduced", True), ("RESOLVED", True), (None, False)):
        f = parse_finding({**GET_ANSWER, "finding_status": status})
        assert f is not None and f.closed is closed, status


def test_a_finding_without_id_or_location_parses_safely():
    assert parse_finding({"finding_value": "CVE-1"}) is None
    assert parse_finding("not a dict") is None
    f = parse_finding({"finding_id": "u1", "finding_value": "CVE-1", "finding_assets": []})
    assert f is not None and f.assets == () and f.inject_id is None


def test_every_reader_goes_through_the_adapter():
    """The CTEM case reader and the tool answer read the same fields from both shapes."""
    issue = source.openaev_issues([SEARCH_ROW])[0]
    assert issue.external_id == "8f1c" and issue.assets == ("web-1", "a-2") and issue.cve == "CVE-2026-1234"
    assert issue.retest_status is None
    closed = source.openaev_issues([GET_ANSWER])[0]
    assert closed.retest_status == "resolved" and closed.assets == ("a-1", "a-2")
    text = _fmt_finding(SEARCH_ROW)
    assert "Inject: inj-7" in text and "Assets: web-1, a-2" in text
    assert "Status: RESOLVED" in _fmt_finding(GET_ANSWER)


def test_the_delete_answer_says_findings_are_kept_as_history():
    assert FINDINGS_KEPT_AS_HISTORY == "Its findings are kept in OpenAEV as history."


#: A search row of an OpenAEV that answers the detection times (OpenAEV #9846): last detected on
#: 3 Nov, the row rewritten on 20 Nov (a status, an asset link, a migration).
SEEN_ROW = {
    **SEARCH_ROW,
    "finding_first_seen": "2026-10-30T08:00:00Z",
    "finding_last_seen": "2026-11-03T08:00:00Z",
}


def test_a_row_with_detection_times_reads_first_and_last_seen() -> None:
    f = parse_finding(SEEN_ROW)
    assert f is not None and f.seen_reported
    assert (f.first_seen, f.last_seen) == ("2026-10-30T08:00:00Z", "2026-11-03T08:00:00Z")
    assert f.updated_at == "2026-11-20T10:00:00Z"
    text = _fmt_finding(SEEN_ROW)
    assert "Last seen: 2026-11-03T08:00:00Z" in text and "Updated:" not in text
    assert source.openaev_issues([SEEN_ROW])[0].first_seen_at == datetime(2026, 10, 30, 8, tzinfo=UTC)


def test_a_row_without_detection_times_falls_back_and_says_updated() -> None:
    """An OpenAEV older than #9846: the row times stand in, labelled as what they are."""
    f = parse_finding(SEARCH_ROW)
    assert f is not None and not f.seen_reported
    assert (f.first_seen, f.last_seen) == ("2026-11-01T10:00:00Z", "2026-11-20T10:00:00Z")
    text = _fmt_finding(SEARCH_ROW)
    assert "Updated: 2026-11-20T10:00:00Z" in text and "Last seen" not in text
    assert source.openaev_issues([SEARCH_ROW])[0].first_seen_at == datetime(2026, 11, 1, 10, tzinfo=UTC)


def test_the_case_window_reads_last_seen_when_present() -> None:
    since = datetime(2026, 11, 10, tzinfo=UTC)
    # Detected before the window, kept by the server only because a later write moved its update time.
    assert not source._seen_since(SEEN_ROW, since)
    assert source._seen_since({**SEEN_ROW, "finding_last_seen": "2026-11-12T08:00:00Z"}, since)
    # No detection time: the server's filter on finding_updated_at stands.
    assert source._seen_since(SEARCH_ROW, since)
