"""Tests for the AWS Security provider (GuardDuty / Security Hub / CloudTrail).

Pins the provider's core contracts: compound-token parsing, credential
short-circuits before any request is signed, GuardDuty detector
auto-discovery + caching, structured filter -> API criteria translation
(GuardDuty ``findingCriteria`` and Security Hub ASFF ``Filters``), the
CloudTrail JSON 1.1 ``X-Amz-Target`` dispatch, and the Security Hub
BatchUpdateFindings payload shape.
"""

from __future__ import annotations

import json as _json

import pytest

from app.integrations.providers import aws_security
from app.integrations.providers.aws_security import build_tools
from app.services.integration_tools import _SafeIntegrationExecutor

# region||access_key_id||secret_access_key
_TOKEN = "us-east-1||AKIA_TEST||secret_test"


class _FakeResp:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = _json.dumps(self._payload).encode()

    def json(self) -> dict:
        return self._payload


def _handlers(token: str = _TOKEN) -> dict:
    executor = _SafeIntegrationExecutor()
    build_tools(token, executor)
    return executor._handlers


def _capture(monkeypatch, payloads: list[dict] | dict | None = None) -> list[dict]:
    """Patch _signed_request to capture call kwargs and return queued 200s."""
    captured: list[dict] = []
    queue = list(payloads) if isinstance(payloads, list) else None
    static = payloads if isinstance(payloads, dict) else {}

    async def _fake_signed_request(**kwargs):
        captured.append(kwargs)
        payload = queue.pop(0) if queue else static
        return _FakeResp(200, payload), None

    monkeypatch.setattr(aws_security, "_signed_request", _fake_signed_request)
    return captured


# ---------------------------------------------------------------------------
# Tool registration + credential short-circuit
# ---------------------------------------------------------------------------


def test_build_tools_registers_all_sixteen_tools():
    executor = _SafeIntegrationExecutor()
    definitions = build_tools(_TOKEN, executor)
    assert len(definitions) == 16
    assert set(executor._handlers) == {d.name for d in definitions}
    # Reads sort before the triage/write tools.
    priorities = {d.name: d.priority for d in definitions}
    assert priorities["list_guardduty_findings"] < priorities["archive_guardduty_findings"]
    assert (
        priorities["get_securityhub_findings"]
        < priorities["update_securityhub_findings_workflow"]
    )


@pytest.mark.asyncio
async def test_missing_credentials_short_circuits(monkeypatch):
    async def _must_not_call(**kwargs):
        raise AssertionError("_signed_request must not run without credentials")

    monkeypatch.setattr(aws_security, "_signed_request", _must_not_call)

    # Empty access_key_id + secret in the compound token.
    handler = _handlers("us-east-1||||")["list_guardduty_findings"]
    result = await handler()
    assert "credentials are missing" in result.lower()


# ---------------------------------------------------------------------------
# GuardDuty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_findings_discovers_detector_and_builds_criteria(monkeypatch):
    captured = _capture(
        monkeypatch,
        [
            {"detectorIds": ["det-1"]},
            {"findingIds": ["f-1"], "nextToken": "tok-2"},
            {
                "findings": [
                    {
                        "id": "f-1",
                        "type": "UnauthorizedAccess:IAMUser/MaliciousIPCaller",
                        "severity": 8,
                        "title": "API called from a known malicious IP.",
                        "updatedAt": "2026-07-01T00:00:00Z",
                        "service": {"archived": False, "count": 3},
                        "resource": {"resourceType": "AccessKey"},
                    }
                ]
            },
        ],
    )
    handler = _handlers()["list_guardduty_findings"]
    result = await handler(min_severity=7, archived="false", limit=10)

    # 3 calls: ListDetectors -> ListFindings -> GetFindings.
    assert len(captured) == 3
    assert captured[0]["path"] == "/detector"
    assert captured[1]["path"] == "/detector/det-1/findings"
    criteria = captured[1]["body"]["findingCriteria"]["criterion"]
    assert criteria["severity"] == {"greaterThanOrEqual": 7}
    assert criteria["service.archived"] == {"equals": ["false"]}
    assert captured[1]["body"]["maxResults"] == 10
    assert captured[2]["path"] == "/detector/det-1/findings/get"
    assert captured[2]["body"]["findingIds"] == ["f-1"]

    assert "HIGH" in result
    assert "UnauthorizedAccess:IAMUser/MaliciousIPCaller" in result
    assert 'next_token="tok-2"' in result


@pytest.mark.asyncio
async def test_detector_id_is_cached_across_calls(monkeypatch):
    captured = _capture(
        monkeypatch,
        [
            {"detectorIds": ["det-1"]},
            {"findingIds": []},
            # Second tool call must NOT re-list detectors.
            {"findingIds": []},
        ],
    )
    handlers = _handlers()
    await handlers["list_guardduty_findings"]()
    await handlers["list_guardduty_findings"]()

    detector_calls = [c for c in captured if c["path"] == "/detector"]
    assert len(detector_calls) == 1


@pytest.mark.asyncio
async def test_no_detector_reports_actionable_error(monkeypatch):
    _capture(monkeypatch, [{"detectorIds": []}])
    handler = _handlers()["list_guardduty_findings"]
    result = await handler()
    assert "no GuardDuty detector" in result


@pytest.mark.asyncio
async def test_archive_findings_builds_expected_payload(monkeypatch):
    captured = _capture(monkeypatch, [{}])
    handler = _handlers()["archive_guardduty_findings"]
    result = await handler(finding_ids="f-1, f-2", detector_id="det-9")

    assert "Archived 2" in result
    assert captured[0]["path"] == "/detector/det-9/findings/archive"
    assert captured[0]["body"] == {"findingIds": ["f-1", "f-2"]}


@pytest.mark.asyncio
async def test_archive_findings_requires_ids(monkeypatch):
    async def _must_not_call(**kwargs):
        raise AssertionError("must validate finding_ids before signing")

    monkeypatch.setattr(aws_security, "_signed_request", _must_not_call)
    handler = _handlers()["archive_guardduty_findings"]
    result = await handler(finding_ids="", detector_id="det-9")
    assert "finding ID is required" in result


@pytest.mark.asyncio
async def test_fractional_min_severity_floors_criteria_and_refines_client_side(
    monkeypatch,
):
    """The GuardDuty API only accepts integer severity bounds, so a fractional
    min_severity must be floored on the wire and refined client-side."""
    captured = _capture(
        monkeypatch,
        [
            {"detectorIds": ["det-1"]},
            {"findingIds": ["f-low", "f-high"]},
            {
                "findings": [
                    {"id": "f-low", "type": "T/Low", "severity": 7.2, "title": "low"},
                    {"id": "f-high", "type": "T/High", "severity": 8.1, "title": "high"},
                ]
            },
        ],
    )
    handler = _handlers()["list_guardduty_findings"]
    result = await handler(min_severity=7.9)

    # Wire filter is the integer floor (API constraint) ...
    criteria = captured[1]["body"]["findingCriteria"]["criterion"]
    assert criteria["severity"] == {"greaterThanOrEqual": 7}
    # ... and the fractional threshold is applied to the fetched findings.
    assert "f-high" in result
    assert "f-low" not in result


@pytest.mark.asyncio
async def test_fractional_min_severity_refines_statistics(monkeypatch):
    _capture(
        monkeypatch,
        [
            {
                "findingStatistics": {
                    "countBySeverity": {"7.0": 4, "8.0": 2, "9.0": 1}
                }
            },
        ],
    )
    handler = _handlers()["get_guardduty_statistics"]
    result = await handler(detector_id="det-1", min_severity=7.5)

    assert "severity 7.0" not in result
    assert "severity 8.0 (HIGH): 2" in result
    assert "severity 9.0 (CRITICAL): 1" in result
    assert "total 3" in result


@pytest.mark.asyncio
async def test_string_typed_numeric_args_are_coerced(monkeypatch):
    """Tool args are not schema-validated at runtime - an LLM may pass
    min_severity/limit as strings. They must be coerced, not raise."""
    captured = _capture(
        monkeypatch,
        [
            {"detectorIds": ["det-1"]},
            {"findingIds": ["f-1"]},
            {"findings": [{"id": "f-1", "type": "T", "severity": 8, "title": "t"}]},
        ],
    )
    handler = _handlers()["list_guardduty_findings"]
    result = await handler(min_severity="7", limit="10")

    criteria = captured[1]["body"]["findingCriteria"]["criterion"]
    assert criteria["severity"] == {"greaterThanOrEqual": 7}
    assert captured[1]["body"]["maxResults"] == 10
    assert "f-1" in result


@pytest.mark.asyncio
async def test_garbage_min_severity_returns_actionable_error(monkeypatch):
    async def _must_not_call(**kwargs):
        raise AssertionError("must validate min_severity before any request")

    monkeypatch.setattr(aws_security, "_signed_request", _must_not_call)
    handlers = _handlers()

    result = await handlers["list_guardduty_findings"](min_severity="critical")
    assert "min_severity must be a number" in result

    result = await handlers["get_guardduty_statistics"](min_severity="high")
    assert "min_severity must be a number" in result


@pytest.mark.asyncio
async def test_statistics_survive_unparseable_severity_buckets(monkeypatch):
    """Unexpected non-numeric countBySeverity keys must not crash the sort
    (they are kept visible by the fractional refinement filter too)."""
    _capture(
        monkeypatch,
        [
            {
                "findingStatistics": {
                    "countBySeverity": {"8.0": 2, "weird-bucket": 1}
                }
            },
        ],
    )
    handler = _handlers()["get_guardduty_statistics"]
    result = await handler(detector_id="det-1")

    assert "severity 8.0 (HIGH): 2" in result
    assert "weird-bucket" in result
    assert "total 3" in result


@pytest.mark.asyncio
async def test_statistics_formats_severity_counts(monkeypatch):
    captured = _capture(
        monkeypatch,
        [
            {"findingStatistics": {"countBySeverity": {"8.0": 2, "5.0": 7}}},
        ],
    )
    handler = _handlers()["get_guardduty_statistics"]
    result = await handler(detector_id="det-1")

    assert captured[0]["path"] == "/detector/det-1/findings/statistics"
    assert captured[0]["body"]["findingStatisticTypes"] == ["COUNT_BY_SEVERITY"]
    assert "total 9" in result
    assert "severity 8.0 (HIGH): 2" in result
    assert "severity 5.0 (MEDIUM): 7" in result


# ---------------------------------------------------------------------------
# Security Hub
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_securityhub_findings_builds_asff_filters(monkeypatch):
    captured = _capture(monkeypatch, [{"Findings": []}])
    handler = _handlers()["get_securityhub_findings"]
    await handler(
        severity_label="high",
        workflow_status="NEW",
        product_name="GuardDuty",
        title_contains="root",
        limit=5,
    )

    assert captured[0]["service"] == "securityhub"
    assert captured[0]["path"] == "/findings"
    body = captured[0]["body"]
    filters = body["Filters"]
    assert filters["SeverityLabel"] == [{"Value": "HIGH", "Comparison": "EQUALS"}]
    assert filters["WorkflowStatus"] == [{"Value": "NEW", "Comparison": "EQUALS"}]
    assert filters["RecordState"] == [{"Value": "ACTIVE", "Comparison": "EQUALS"}]
    assert filters["ProductName"] == [{"Value": "GuardDuty", "Comparison": "EQUALS"}]
    assert filters["Title"] == [{"Value": "root", "Comparison": "CONTAINS"}]
    assert body["MaxResults"] == 5
    assert body["SortCriteria"] == [{"Field": "UpdatedAt", "SortOrder": "desc"}]


@pytest.mark.asyncio
async def test_update_workflow_builds_batchupdate_payload(monkeypatch):
    captured = _capture(
        monkeypatch,
        [
            {
                "ProcessedFindings": [{"Id": "arn:f1", "ProductArn": "arn:p1"}],
                "UnprocessedFindings": [],
            }
        ],
    )
    handler = _handlers()["update_securityhub_findings_workflow"]
    result = await handler(
        findings=[{"id": "arn:f1", "product_arn": "arn:p1"}],
        workflow_status="resolved",
        note="Remediated by rotating the key.",
    )

    assert "Workflow set to RESOLVED on 1/1" in result
    assert captured[0]["method"] == "patch"
    assert captured[0]["path"] == "/findings/batchupdate"
    body = captured[0]["body"]
    assert body["FindingIdentifiers"] == [{"Id": "arn:f1", "ProductArn": "arn:p1"}]
    assert body["Workflow"] == {"Status": "RESOLVED"}
    assert body["Note"]["Text"] == "Remediated by rotating the key."


@pytest.mark.asyncio
async def test_update_workflow_rejects_bad_status_and_missing_ids(monkeypatch):
    async def _must_not_call(**kwargs):
        raise AssertionError("must validate before signing")

    monkeypatch.setattr(aws_security, "_signed_request", _must_not_call)
    handler = _handlers()["update_securityhub_findings_workflow"]

    result = await handler(findings=[{"id": "x", "product_arn": "y"}], workflow_status="CLOSED")
    assert "workflow_status must be one of" in result

    result = await handler(findings=[{"id": "x"}], workflow_status="RESOLVED")
    assert "product_arn" in result


# ---------------------------------------------------------------------------
# CloudTrail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_events_uses_json11_target_and_epoch_times(monkeypatch):
    captured = _capture(
        monkeypatch,
        [
            {
                "Events": [
                    {
                        "EventId": "e-1",
                        "EventName": "ConsoleLogin",
                        "EventTime": 1751328000,
                        "Username": "root",
                        "EventSource": "signin.amazonaws.com",
                        "CloudTrailEvent": '{"sourceIPAddress": "203.0.113.7"}',
                    }
                ]
            }
        ],
    )
    handler = _handlers()["lookup_cloudtrail_events"]
    result = await handler(
        attribute_key="EventName",
        attribute_value="ConsoleLogin",
        start_time="2026-07-01T00:00:00Z",
        limit=5,
    )

    call = captured[0]
    assert call["service"] == "cloudtrail"
    assert call["path"] == "/"
    assert call["extra_headers"]["X-Amz-Target"].endswith(".LookupEvents")
    assert call["content_type"] == "application/x-amz-json-1.1"
    body = call["body"]
    assert body["LookupAttributes"] == [
        {"AttributeKey": "EventName", "AttributeValue": "ConsoleLogin"}
    ]
    assert body["StartTime"] == 1782864000  # 2026-07-01T00:00:00Z as epoch seconds
    assert body["MaxResults"] == 5

    assert "ConsoleLogin" in result
    assert "203.0.113.7" in result


@pytest.mark.asyncio
async def test_lookup_events_validates_attribute_pairing(monkeypatch):
    async def _must_not_call(**kwargs):
        raise AssertionError("must validate before signing")

    monkeypatch.setattr(aws_security, "_signed_request", _must_not_call)
    handler = _handlers()["lookup_cloudtrail_events"]

    result = await handler(attribute_key="BogusKey", attribute_value="x")
    assert "attribute_key must be one of" in result

    result = await handler(attribute_key="EventName", attribute_value="")
    assert "attribute_value is required" in result


# ---------------------------------------------------------------------------
# validate_connection (STS)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_connection_reports_account_and_principal(monkeypatch):
    captured: dict = {}

    async def _fake_signed_request(**kwargs):
        captured.update(kwargs)
        return (
            _FakeResp(
                200,
                {
                    "GetCallerIdentityResponse": {
                        "GetCallerIdentityResult": {
                            "Account": "123456789012",
                            "Arn": "arn:aws:iam::123456789012:user/soc",
                            "UserId": "AIDAEXAMPLE",
                        }
                    }
                },
            ),
            None,
        )

    monkeypatch.setattr(aws_security, "_signed_request", _fake_signed_request)
    ok, message = await aws_security.validate_connection(
        "eu-west-1", "AKIA_TEST", "secret_test"
    )

    assert ok is True
    assert "123456789012" in message
    assert "eu-west-1" in message
    assert captured["service"] == "sts"
    assert captured["raw_body"] == b"Action=GetCallerIdentity&Version=2011-06-15"
    assert captured["content_type"] == "application/x-www-form-urlencoded"


@pytest.mark.asyncio
async def test_validate_connection_missing_credentials():
    ok, message = await aws_security.validate_connection("us-east-1", "", "")
    assert ok is False
    assert "Missing" in message


@pytest.mark.asyncio
async def test_validate_connection_fails_closed_on_unparseable_200(monkeypatch):
    """An HTTP 200 without a parseable GetCallerIdentity result (e.g. HTML
    from a proxy/WAF) must NOT report a successful connection."""

    async def _fake_signed_request(**kwargs):
        return _FakeResp(200, {}), None

    monkeypatch.setattr(aws_security, "_signed_request", _fake_signed_request)
    ok, message = await aws_security.validate_connection(
        "us-east-1", "AKIA_TEST", "secret_test"
    )
    assert ok is False
    assert "unexpected response" in message


# ---------------------------------------------------------------------------
# Helper robustness
# ---------------------------------------------------------------------------


def test_to_epoch_parses_iso_and_epoch_inputs():
    assert aws_security._to_epoch("2026-07-01T00:00:00Z") == 1782864000
    assert aws_security._to_epoch("2026-07-01T00:00:00+00:00") == 1782864000
    assert aws_security._to_epoch("1782864000") == 1782864000
    assert aws_security._to_epoch("2026-07-01T00:00:00Z", millis=True) == 1782864000000
    assert aws_security._to_epoch("") is None
    assert aws_security._to_epoch("not-a-date") is None
    assert aws_security._to_epoch("inf") is None  # OverflowError path
    assert aws_security._to_epoch("1e400") is None


@pytest.mark.asyncio
async def test_api_treats_empty_body_as_success_and_non_json_as_error(monkeypatch):
    class _EmptyResp:
        status_code = 200
        content = b""

        def json(self):
            raise ValueError("no body")

    class _HtmlResp:
        status_code = 200
        content = b"<html>blocked by proxy</html>"

        def json(self):
            raise ValueError("not json")

    responses = [_EmptyResp(), _HtmlResp()]

    async def _fake_signed_request(**kwargs):
        return responses.pop(0), None

    monkeypatch.setattr(aws_security, "_signed_request", _fake_signed_request)
    handler = _handlers()["archive_guardduty_findings"]

    # Empty 200 body (ArchiveFindings contract) is a success.
    result = await handler(finding_ids="f-1", detector_id="det-1")
    assert "Archived 1" in result

    # A non-empty unparseable body must surface an error, not "no findings".
    result = await handler(finding_ids="f-1", detector_id="det-1")
    assert "non-JSON response" in result


def test_numeric_coercion_handles_extreme_inputs():
    assert aws_security._to_int("10", 25) == 10
    assert aws_security._to_int("1e400", 25) == 25  # OverflowError path
    assert aws_security._to_int(None, 25) == 25
    assert aws_security._to_float("7.5") == 7.5
    assert aws_security._to_float("inf") is None
    assert aws_security._to_float("nan") is None
    assert aws_security._to_float("") == 0.0


def test_gd_severity_labels():
    assert aws_security._gd_severity_label(9.5) == "CRITICAL"
    assert aws_security._gd_severity_label(8) == "HIGH"
    assert aws_security._gd_severity_label("5.0") == "MEDIUM"
    assert aws_security._gd_severity_label(2) == "LOW"
    assert aws_security._gd_severity_label(None) == "UNKNOWN"


def test_split_ids_normalises_inputs():
    assert aws_security._split_ids("a, b ,c") == ["a", "b", "c"]
    assert aws_security._split_ids(["a", " b "]) == ["a", "b"]
    assert aws_security._split_ids(None) == []
    assert aws_security._split_ids("") == []
