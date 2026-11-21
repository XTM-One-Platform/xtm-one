# SPDX-License-Identifier: Apache-2.0

"""AWS Security integration – GuardDuty, Security Hub, and CloudTrail.

Full-fledged AWS-native security integration designed as a best-in-class
building block for autonomous SOC agents. It covers the cloud threat-detection
and audit surface of an AWS account:

  - Amazon GuardDuty: detectors, findings (list with severity/type/archived
    filters, detail lookup, severity statistics), archive/unarchive triage
  - AWS Security Hub: aggregated ASFF findings (rich filters), single-finding
    detail, insights + grouped insight results, hub/standards posture summary,
    and workflow triage (NEW / NOTIFIED / RESOLVED / SUPPRESSED + analyst note)
  - AWS CloudTrail: LookupEvents for audit investigations (who did what, when)
    over the last 90 days of management events
  - AWS STS: caller-identity for account/principal context

Tool categories (16 tools):
  - Account: get caller identity (account ID, principal ARN)
  - GuardDuty: list detectors, list findings, get finding(s), severity
    statistics, archive findings, unarchive findings
  - Security Hub: posture summary, get findings, get one finding, list
    insights, get insight results, update findings workflow
  - CloudTrail: look up audit events
  - EC2 containment: isolate an instance behind an empty security group and
    release it (both approval-gated on every call; needs ec2:DescribeInstances,
    ec2:DescribeSecurityGroups, ec2:CreateSecurityGroup, ec2:CreateTags,
    ec2:RevokeSecurityGroupEgress, ec2:ModifyInstanceAttribute,
    ec2:DeleteSecurityGroup)

Authentication is **AWS IAM credentials** (an access key). The integration
stores an Access Key ID + Secret Access Key + region; the compound
``region||access_key_id||secret_access_key`` token is assembled by
``integration_tools._get_token`` from the dedicated API-key column (secret)
and the encrypted ``extra_config`` (access key ID, region). Every request is
signed with AWS Signature Version 4 via :mod:`app.integrations.aws_sigv4`
over the shared async ``httpx`` path — no synchronous ``boto3`` calls in the
event loop and no AWS SDK dependency.

Wire protocols per service (all regional endpoints):
  - GuardDuty  — REST-JSON at ``guardduty.{region}.amazonaws.com``
  - SecurityHub — REST-JSON at ``securityhub.{region}.amazonaws.com``
  - CloudTrail — JSON 1.1 RPC at ``cloudtrail.{region}.amazonaws.com`` with
    the ``X-Amz-Target`` header
  - STS        — query protocol (form POST) at ``sts.{region}.amazonaws.com``
"""

from __future__ import annotations

import contextlib
import json as _json
import logging
import math
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.egress import egress_client
from app.integrations.aws_sigv4 import (
    build_canonical_querystring,
    encode_path,
    sign_request,
)
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "AWS Security"
_DEFAULT_REGION = "us-east-1"
_DEFAULT_TIMEOUT = 30

_CLOUDTRAIL_TARGET_PREFIX = "com.amazonaws.cloudtrail.v20131101.CloudTrail_20131101"

# EC2 query API (XML answers from AWS, trusted; ElementTree resolves no
# external entity). The isolation group records the replaced groups here.
_EC2_API_VERSION = "2016-11-15"
_EC2_INSTANCE_ID = re.compile(r"^i-[0-9a-f]{8,17}$")
_ISOLATION_TAG = "xtm-one:isolated-from-groups"
# EC2 accepts only these characters in a security group description, 255 at most.
_SG_DESCRIPTION_UNSUPPORTED = re.compile(r"[^a-zA-Z0-9 ._\-:/()#,@\[\]+=&;{}!$*]")
_SG_DESCRIPTION_MAX = 255


def _sg_description(text: str) -> str:
    """Fit free text to the EC2 security group description charset, cut without an ellipsis."""
    cleaned = " ".join(_SG_DESCRIPTION_UNSUPPORTED.sub(" ", text).split())
    return cleaned[:_SG_DESCRIPTION_MAX].rstrip()


# Security Hub ASFF workflow statuses accepted by BatchUpdateFindings.
_WORKFLOW_STATUSES = ("NEW", "NOTIFIED", "RESOLVED", "SUPPRESSED")
# Security Hub ASFF severity labels.
_SEVERITY_LABELS = ("INFORMATIONAL", "LOW", "MEDIUM", "HIGH", "CRITICAL")
# CloudTrail LookupEvents attribute keys.
_LOOKUP_ATTRIBUTE_KEYS = (
    "EventId",
    "EventName",
    "ReadOnly",
    "Username",
    "ResourceType",
    "ResourceName",
    "EventSource",
    "AccessKeyId",
)

_AUTH_ERROR = (
    "Error: AWS credentials are missing or incomplete. Ensure the integration "
    "has an AWS Access Key ID, Secret Access Key, and region configured. See "
    "the integration setup guide for the least-privilege IAM policy (read-only "
    "GuardDuty / Security Hub / CloudTrail access, plus the explicit "
    "guardduty:ArchiveFindings, guardduty:UnarchiveFindings, and "
    "securityhub:BatchUpdateFindings actions only if the agent should triage "
    "findings)."
)


# ---------------------------------------------------------------------------
# Endpoint + credential helpers
# ---------------------------------------------------------------------------


def resolve_host(service: str, region: str) -> str:
    """Return the regional endpoint host for an AWS *service* (e.g. ``guardduty.us-east-1.amazonaws.com``)."""
    region = (region or _DEFAULT_REGION).strip() or _DEFAULT_REGION
    return f"{service}.{region}.amazonaws.com"


async def _signed_request(
    *,
    method: str,
    service: str,
    region: str,
    access_key_id: str,
    secret_access_key: str,
    path: str,
    query: dict[str, Any] | None = None,
    body: Any | None = None,
    raw_body: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
    content_type: str = "application/json",
    action: str,
    timeout: int = _DEFAULT_TIMEOUT,
    expected_statuses: set[int] | None = None,
) -> tuple[httpx.Response | None, str | None]:
    """Sign an AWS request with SigV4 and execute it via ``safe_request``.

    Returns the ``(response, error)`` tuple from :func:`safe_request`. The same
    canonical (encoded) path / query / body bytes that are signed are sent on
    the wire so the signature always matches. ``body`` is JSON-serialised;
    ``raw_body`` (e.g. the STS form payload) is sent verbatim.
    """
    region = (region or _DEFAULT_REGION).strip() or _DEFAULT_REGION
    host = resolve_host(service, region)
    canonical_uri = encode_path(path)
    canonical_qs = build_canonical_querystring(query)
    if raw_body is not None:
        payload = raw_body
    elif body is not None:
        payload = _json.dumps(body).encode("utf-8")
    else:
        payload = b""

    signed_headers = sign_request(
        method=method,
        service=service,
        region=region,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        host=host,
        canonical_uri=canonical_uri,
        canonical_querystring=canonical_qs,
        payload=payload,
    )
    headers = {**signed_headers, "Accept": "application/json"}
    if payload:
        headers["Content-Type"] = content_type
    if extra_headers:
        headers.update(extra_headers)

    url = f"https://{host}{canonical_uri}"
    if canonical_qs:
        url = f"{url}?{canonical_qs}"

    request_kwargs: dict[str, Any] = {"headers": headers}
    if payload:
        request_kwargs["content"] = payload

    async with egress_client(timeout=timeout) as client:
        return await safe_request(
            client,
            method.lower(),
            url,
            _PROVIDER,
            action,
            expected_statuses=expected_statuses,
            **request_kwargs,
        )


async def validate_connection(
    region: str, access_key_id: str, secret_access_key: str
) -> tuple[bool, str]:
    """Verify AWS credentials by calling STS GetCallerIdentity.

    Returns ``(ok, message)``. GetCallerIdentity requires no IAM permissions,
    so a 200 confirms the access key, secret, and region are valid without
    depending on GuardDuty / Security Hub being enabled. Used by the
    integration connectivity test.
    """
    if not access_key_id or not secret_access_key:
        return False, "Missing AWS Access Key ID or Secret Access Key."
    resp, err = await _signed_request(
        method="POST",
        service="sts",
        region=region,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        path="/",
        raw_body=b"Action=GetCallerIdentity&Version=2011-06-15",
        content_type="application/x-www-form-urlencoded",
        action="verify caller identity",
        timeout=15,
    )
    if err or resp is None:
        return False, (
            err
            or "Authentication failed — check the access key, secret, and region."
        )
    try:
        result = (
            resp.json()
            .get("GetCallerIdentityResponse", {})
            .get("GetCallerIdentityResult", {})
        )
    except Exception:
        result = {}
    account = result.get("Account")
    arn = result.get("Arn")
    if not (account and arn):
        # An HTTP 200 whose body is not a parseable GetCallerIdentity result
        # (HTML from a proxy/WAF, unexpected shape) is NOT a verified
        # connection - failing closed keeps the connectivity test honest.
        return False, (
            "Error: STS returned an unexpected response (HTTP 200 without a "
            "parseable GetCallerIdentity result). A proxy, WAF, or "
            "misconfigured endpoint may be intercepting the request."
        )
    return True, (
        f"AWS Security is connected (account {account}, principal {arn}, "
        f"region {(region or _DEFAULT_REGION).strip() or _DEFAULT_REGION})."
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _trunc(text: Any, max_len: int = 300) -> str:
    if not text:
        return ""
    text = str(text)
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _severity_value(finding: dict) -> float:
    """Extract a GuardDuty finding's numeric severity (0.0 when absent)."""
    try:
        return float(finding.get("Severity") or finding.get("severity") or 0)
    except (TypeError, ValueError):
        return 0.0


def _gd_severity_label(severity: Any) -> str:
    """Map a GuardDuty numeric severity (1.0-10.0) to its label."""
    try:
        sev = float(severity)
    except (TypeError, ValueError):
        return "UNKNOWN"
    if sev >= 9:
        return "CRITICAL"
    if sev >= 7:
        return "HIGH"
    if sev >= 4:
        return "MEDIUM"
    if sev >= 1:
        return "LOW"
    return "UNKNOWN"


def _to_float(value: Any, default: float = 0.0) -> float | None:
    """Coerce a numeric tool argument to float.

    Tool arguments are not schema-validated at execution time, so an LLM may
    pass ``min_severity`` as a string (``"7.5"``). Returns *default* for
    empty/None input and ``None`` when the value is unparseable so the caller
    can return an actionable validation error instead of raising.
    """
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    # NaN / +-inf are as unusable as garbage strings for a severity bound.
    return parsed if math.isfinite(parsed) else None


def _to_int(value: Any, default: int) -> int:
    """Coerce a numeric tool argument (possibly a string) to int.

    Falls back to *default* on garbage — ``limit`` is always clamped to the
    service bounds afterwards, so a silent default is safe here.
    ``OverflowError`` covers ``int(float("inf"))`` from extreme inputs.
    """
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _to_epoch(value: str, *, millis: bool = False) -> int | None:
    """Parse an ISO-8601 timestamp (or epoch seconds) to epoch seconds/millis."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        # Accept a raw epoch-seconds number for robustness. OverflowError
        # covers int(float("inf")) / extreme scientific notation.
        return int(float(value)) * (1000 if millis else 1)
    except (ValueError, OverflowError):
        pass
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * (1000 if millis else 1))
    except ValueError:
        return None


def _split_ids(value: str | list[str] | None) -> list[str]:
    """Normalise a comma-separated string (or list) of IDs."""
    if not value:
        return []
    items = value if isinstance(value, list) else str(value).split(",")
    return [s for i in items if i and (s := str(i).strip())]


def _format_guardduty_finding(f: dict) -> str:
    """One-line summary of a GuardDuty finding."""
    service = f.get("Service") or f.get("service") or {}
    resource = f.get("Resource") or f.get("resource") or {}
    sev = f.get("Severity") or f.get("severity")
    return (
        f"  [{_gd_severity_label(sev)} {sev}] {f.get('Type') or f.get('type', '?')}\n"
        f"    id: {f.get('Id') or f.get('id', '?')} | "
        f"resource: {resource.get('ResourceType') or resource.get('resourceType', '?')} | "
        f"count: {service.get('Count') or service.get('count', 1)} | "
        f"archived: {service.get('Archived') if service.get('Archived') is not None else service.get('archived', False)} | "
        f"updated: {str(f.get('UpdatedAt') or f.get('updatedAt', ''))[:19]}\n"
        f"    {_trunc(f.get('Title') or f.get('title', ''), 160)}"
    )


def _format_securityhub_finding(f: dict) -> str:
    """One-line summary of a Security Hub ASFF finding."""
    severity = (f.get("Severity") or {}).get("Label", "?")
    workflow = (f.get("Workflow") or {}).get("Status", "?")
    resources = f.get("Resources") or []
    resource_ids = ", ".join(_trunc(r.get("Id", "?"), 100) for r in resources[:2])
    return (
        f"  [{severity}] {_trunc(f.get('Title', '?'), 160)}\n"
        f"    id: {f.get('Id', '?')}\n"
        f"    product: {f.get('ProductName') or f.get('ProductArn', '?')} | "
        f"workflow: {workflow} | record: {f.get('RecordState', '?')} | "
        f"updated: {str(f.get('UpdatedAt', ''))[:19]}\n"
        f"    resources: {resource_ids or '(none)'}"
    )


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build AWS Security tools.

    ``token`` is the compound ``region||access_key_id||secret_access_key``
    assembled by ``integration_tools._get_token`` from the integration's
    stored AWS credentials and region.
    """
    region, access_key_id, secret_access_key = parse_compound_token(token, 3)
    region = (region or _DEFAULT_REGION).strip() or _DEFAULT_REGION

    # Detector ID cache — GuardDuty scopes every findings call to a detector,
    # and virtually all accounts have exactly one per region.  Resolved once
    # per tool-builder lifetime.
    detector_cache: dict[str, str] = {}

    async def _api(
        method: str,
        service: str,
        path: str,
        action: str,
        *,
        query: dict[str, Any] | None = None,
        body: Any | None = None,
        extra_headers: dict[str, str] | None = None,
        content_type: str = "application/json",
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> tuple[dict | None, str | None]:
        """Authenticated AWS request helper returning ``(json, error)``."""
        if not access_key_id or not secret_access_key:
            return None, _AUTH_ERROR
        resp, err = await _signed_request(
            method=method,
            service=service,
            region=region,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            path=path,
            query=query,
            body=body,
            extra_headers=extra_headers,
            content_type=content_type,
            action=action,
            timeout=timeout,
        )
        if err:
            return None, err
        # 200 with an empty body is a legitimate success (ArchiveFindings,
        # UnarchiveFindings, …); any other unparseable body (HTML from a
        # proxy/WAF, truncated payload) must surface an error instead of
        # silently reading as "no findings".
        if not (resp.content or b"").strip():
            return {}, None
        try:
            return resp.json(), None
        except Exception:
            return None, (
                f"Error: {_PROVIDER} returned a non-JSON response for "
                f"'{action}' (HTTP {resp.status_code}). A proxy, WAF, or "
                "misconfigured endpoint may be intercepting the request."
            )

    async def _cloudtrail(
        target: str, action: str, body: dict[str, Any]
    ) -> tuple[dict | None, str | None]:
        """CloudTrail JSON 1.1 RPC helper (``X-Amz-Target`` dispatch)."""
        return await _api(
            "post",
            "cloudtrail",
            "/",
            action,
            body=body,
            extra_headers={"X-Amz-Target": f"{_CLOUDTRAIL_TARGET_PREFIX}.{target}"},
            content_type="application/x-amz-json-1.1",
        )

    async def _resolve_detector(detector_id: str = "") -> tuple[str, str | None]:
        """Resolve the effective GuardDuty detector ID (explicit > discovered)."""
        detector_id = (detector_id or "").strip()
        if detector_id:
            return detector_id, None
        if "detector" in detector_cache:
            return detector_cache["detector"], None
        data, err = await _api(
            "get", "guardduty", "/detector", "list detectors"
        )
        if err:
            return "", err
        ids = data.get("detectorIds") or data.get("DetectorIds") or []
        if not ids:
            return "", (
                "Error: no GuardDuty detector found in region "
                f"{region}. Enable GuardDuty in this region or pass an "
                "explicit detector_id."
            )
        detector_cache["detector"] = ids[0]
        return ids[0], None

    def _guardduty_criteria(
        min_severity: float = 0,
        finding_type: str = "",
        archived: str = "",
        updated_after: str = "",
    ) -> dict[str, Any]:
        """Build a GuardDuty ``findingCriteria`` block from structured params."""
        criterion: dict[str, Any] = {}
        if min_severity:
            # The GuardDuty FindingCriteria condition only accepts integer
            # bounds (the Condition model types greaterThanOrEqual as Long),
            # while finding severities are floats (1.0-10.0). Send the floor
            # as a server-side pre-filter (a superset of the requested range);
            # callers refine fractional thresholds client-side against the
            # actual float severities so e.g. 7.9 never includes 7.0-7.8.
            criterion["severity"] = {"greaterThanOrEqual": int(min_severity)}
        if finding_type:
            criterion["type"] = {"equals": [finding_type.strip()]}
        if archived.strip().lower() in ("true", "false"):
            criterion["service.archived"] = {"equals": [archived.strip().lower()]}
        updated_ms = _to_epoch(updated_after, millis=True)
        if updated_ms:
            criterion["updatedAt"] = {"greaterThanOrEqual": updated_ms}
        return {"criterion": criterion} if criterion else {}

    # ══════════════════════════════════════════════════════════════════════
    # 0. ACCOUNT CONTEXT
    # ══════════════════════════════════════════════════════════════════════

    async def get_aws_caller_identity() -> str:
        """Get the AWS account ID and IAM principal the integration acts as."""
        if not access_key_id or not secret_access_key:
            return _AUTH_ERROR
        _ok, message = await validate_connection(
            region, access_key_id, secret_access_key
        )
        return message

    # ══════════════════════════════════════════════════════════════════════
    # 1. GUARDDUTY
    # ══════════════════════════════════════════════════════════════════════

    async def list_guardduty_detectors() -> str:
        """List GuardDuty detectors in the region with their status."""
        data, err = await _api("get", "guardduty", "/detector", "list detectors")
        if err:
            return err
        ids = data.get("detectorIds") or data.get("DetectorIds") or []
        if not ids:
            return (
                f"No GuardDuty detector found in region {region}. GuardDuty is "
                "not enabled here."
            )
        lines = [f"Found {len(ids)} GuardDuty detector(s) in {region}:"]
        for det_id in ids:
            detail, derr = await _api(
                "get",
                "guardduty",
                f"/detector/{det_id}",
                f"get detector {det_id}",
            )
            if derr:
                lines.append(f"  {det_id} — (detail unavailable: {derr})")
                continue
            lines.append(
                f"  {det_id} — status: {detail.get('status') or detail.get('Status', '?')} | "
                f"publishing frequency: {detail.get('findingPublishingFrequency') or detail.get('FindingPublishingFrequency', '?')} | "
                f"updated: {detail.get('updatedAt') or detail.get('UpdatedAt', '?')}"
            )
        return "\n".join(lines)

    async def list_guardduty_findings(
        detector_id: str = "",
        min_severity: float = 0,
        finding_type: str = "",
        archived: str = "false",
        updated_after: str = "",
        limit: int = 25,
        next_token: str = "",
    ) -> str:
        """List GuardDuty findings (IDs + details) matching the filters."""
        min_severity = _to_float(min_severity)  # tool args may arrive as strings
        if min_severity is None:
            return "Error: min_severity must be a number between 0 and 10."
        det_id, err = await _resolve_detector(detector_id)
        if err:
            return err
        body: dict[str, Any] = {
            "maxResults": min(max(_to_int(limit, 25), 1), 50),
            "sortCriteria": {"attributeName": "severity", "orderBy": "DESC"},
        }
        criteria = _guardduty_criteria(
            min_severity, finding_type, archived, updated_after
        )
        if criteria:
            body["findingCriteria"] = criteria
        if next_token:
            body["nextToken"] = next_token
        data, err = await _api(
            "post",
            "guardduty",
            f"/detector/{det_id}/findings",
            "list findings",
            body=body,
        )
        if err:
            return err
        finding_ids = data.get("findingIds") or data.get("FindingIds") or []
        if not finding_ids:
            return "No GuardDuty findings match the given filters."
        detail, err = await _api(
            "post",
            "guardduty",
            f"/detector/{det_id}/findings/get",
            "get findings",
            body={
                "findingIds": finding_ids,
                "sortCriteria": {"attributeName": "severity", "orderBy": "DESC"},
            },
        )
        if err:
            return err
        findings = detail.get("findings") or detail.get("Findings") or []
        if min_severity and min_severity != int(min_severity):
            # Refine the integer-floor server-side filter for fractional
            # thresholds (see _guardduty_criteria).
            findings = [f for f in findings if _severity_value(f) >= min_severity]
        out = format_entity_list(
            findings, _format_guardduty_finding, noun="GuardDuty finding"
        )
        out += f"\n(detector: {det_id})"
        if data.get("nextToken") or data.get("NextToken"):
            token_val = data.get("nextToken") or data.get("NextToken")
            out += f'\n[More results — pass next_token="{token_val}"]'
        return out

    async def get_guardduty_finding(
        finding_ids: str, detector_id: str = ""
    ) -> str:
        """Get the full details of one or more GuardDuty findings by ID."""
        ids = _split_ids(finding_ids)
        if not ids:
            return "Error: at least one finding ID is required."
        if len(ids) > 50:
            return "Error: at most 50 finding IDs can be fetched per call."
        det_id, err = await _resolve_detector(detector_id)
        if err:
            return err
        data, err = await _api(
            "post",
            "guardduty",
            f"/detector/{det_id}/findings/get",
            "get findings",
            body={"findingIds": ids},
        )
        if err:
            return err
        findings = data.get("findings") or data.get("Findings") or []
        if not findings:
            return "No findings found for the given ID(s)."
        blocks: list[str] = []
        for f in findings:
            service = f.get("Service") or f.get("service") or {}
            resource = f.get("Resource") or f.get("resource") or {}
            action = service.get("Action") or service.get("action") or {}
            sev = f.get("Severity") or f.get("severity")
            lines = [
                f"Finding: {f.get('Id') or f.get('id', '?')}",
                f"Type: {f.get('Type') or f.get('type', '?')}",
                f"Severity: {sev} ({_gd_severity_label(sev)})",
                f"Title: {f.get('Title') or f.get('title', '')}",
                f"Description: {_trunc(f.get('Description') or f.get('description', ''), 500)}",
                f"Account: {f.get('AccountId') or f.get('accountId', '?')} | Region: {f.get('Region') or f.get('region', region)}",
                f"Resource type: {resource.get('ResourceType') or resource.get('resourceType', '?')}",
                f"Action type: {action.get('ActionType') or action.get('actionType', '?')}",
                f"First seen: {service.get('EventFirstSeen') or service.get('eventFirstSeen', '?')} | "
                f"Last seen: {service.get('EventLastSeen') or service.get('eventLastSeen', '?')} | "
                f"Count: {service.get('Count') or service.get('count', 1)}",
                f"Archived: {service.get('Archived') if service.get('Archived') is not None else service.get('archived', False)}",
            ]
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    async def get_guardduty_statistics(
        detector_id: str = "",
        min_severity: float = 0,
        finding_type: str = "",
        archived: str = "false",
        updated_after: str = "",
    ) -> str:
        """Get the count of GuardDuty findings grouped by severity."""
        min_severity = _to_float(min_severity)  # tool args may arrive as strings
        if min_severity is None:
            return "Error: min_severity must be a number between 0 and 10."
        det_id, err = await _resolve_detector(detector_id)
        if err:
            return err
        body: dict[str, Any] = {"findingStatisticTypes": ["COUNT_BY_SEVERITY"]}
        criteria = _guardduty_criteria(
            min_severity, finding_type, archived, updated_after
        )
        if criteria:
            body["findingCriteria"] = criteria
        data, err = await _api(
            "post",
            "guardduty",
            f"/detector/{det_id}/findings/statistics",
            "get finding statistics",
            body=body,
        )
        if err:
            return err
        stats = data.get("findingStatistics") or data.get("FindingStatistics") or {}
        by_severity = (
            stats.get("countBySeverity") or stats.get("CountBySeverity") or {}
        )
        if min_severity and min_severity != int(min_severity):
            # Refine the integer-floor server-side filter for fractional
            # thresholds (see _guardduty_criteria). Keys the service returns
            # are numeric severity strings (e.g. "8.0").
            def _key_at_least(key: str) -> bool:
                try:
                    return float(key) >= min_severity
                except (TypeError, ValueError):
                    return True  # keep unparseable buckets visible
            by_severity = {
                k: v for k, v in by_severity.items() if _key_at_least(k)
            }
        if not by_severity:
            return "No GuardDuty findings match the given filters."
        total = sum(by_severity.values())
        lines = [
            f"GuardDuty findings by severity (detector {det_id}, total {total}):"
        ]

        def _numeric_sort_key(key: str) -> float:
            # Unparseable bucket keys are deliberately kept visible (see
            # _key_at_least above); sort them last instead of crashing.
            try:
                return float(key)
            except (TypeError, ValueError):
                return float("-inf")

        for sev in sorted(by_severity, key=_numeric_sort_key, reverse=True):
            lines.append(
                f"  severity {sev} ({_gd_severity_label(sev)}): {by_severity[sev]}"
            )
        return "\n".join(lines)

    async def archive_guardduty_findings(
        finding_ids: str, detector_id: str = ""
    ) -> str:
        """Archive GuardDuty findings (mark as triaged / not actionable)."""
        ids = _split_ids(finding_ids)
        if not ids:
            return "Error: at least one finding ID is required."
        if len(ids) > 50:
            return "Error: at most 50 finding IDs can be archived per call."
        det_id, err = await _resolve_detector(detector_id)
        if err:
            return err
        _data, err = await _api(
            "post",
            "guardduty",
            f"/detector/{det_id}/findings/archive",
            "archive findings",
            body={"findingIds": ids},
        )
        if err:
            return err
        return f"Archived {len(ids)} GuardDuty finding(s) on detector {det_id}."

    async def unarchive_guardduty_findings(
        finding_ids: str, detector_id: str = ""
    ) -> str:
        """Unarchive GuardDuty findings (reopen for investigation)."""
        ids = _split_ids(finding_ids)
        if not ids:
            return "Error: at least one finding ID is required."
        if len(ids) > 50:
            return "Error: at most 50 finding IDs can be unarchived per call."
        det_id, err = await _resolve_detector(detector_id)
        if err:
            return err
        _data, err = await _api(
            "post",
            "guardduty",
            f"/detector/{det_id}/findings/unarchive",
            "unarchive findings",
            body={"findingIds": ids},
        )
        if err:
            return err
        return f"Unarchived {len(ids)} GuardDuty finding(s) on detector {det_id}."

    # ══════════════════════════════════════════════════════════════════════
    # 2. SECURITY HUB
    # ══════════════════════════════════════════════════════════════════════

    async def get_securityhub_summary() -> str:
        """Get the Security Hub status and the enabled security standards."""
        hub, err = await _api(
            "get", "securityhub", "/accounts", "describe hub"
        )
        if err:
            return err
        lines = [
            f"Security Hub: enabled (region {region})",
            f"Hub ARN: {hub.get('HubArn', '?')}",
            f"Subscribed at: {str(hub.get('SubscribedAt', ''))[:19]}",
            f"Auto-enable new controls: {hub.get('AutoEnableControls', '?')}",
            f"Control finding generator: {hub.get('ControlFindingGenerator', '?')}",
        ]
        standards, err = await _api(
            "post",
            "securityhub",
            "/standards/get",
            "get enabled standards",
            body={"MaxResults": 100},
        )
        if err:
            lines.append(f"Enabled standards: (unavailable: {err})")
            return "\n".join(lines)
        subscriptions = standards.get("StandardsSubscriptions") or []
        lines.append(f"Enabled standards ({len(subscriptions)}):")
        for s in subscriptions:
            lines.append(
                f"  - {s.get('StandardsArn', '?').rsplit('/standards/', 1)[-1]} "
                f"(status: {s.get('StandardsStatus', '?')})"
            )
        if not subscriptions:
            lines.append("  (none)")
        return "\n".join(lines)

    def _securityhub_filters(
        severity_label: str = "",
        workflow_status: str = "",
        record_state: str = "ACTIVE",
        product_name: str = "",
        resource_id: str = "",
        title_contains: str = "",
        updated_after: str = "",
        finding_id: str = "",
    ) -> dict[str, Any]:
        """Build a Security Hub ASFF ``Filters`` block from structured params."""
        filters: dict[str, Any] = {}
        if finding_id:
            filters["Id"] = [{"Value": finding_id, "Comparison": "EQUALS"}]
        if severity_label:
            filters["SeverityLabel"] = [
                {"Value": severity_label.strip().upper(), "Comparison": "EQUALS"}
            ]
        if workflow_status:
            filters["WorkflowStatus"] = [
                {"Value": workflow_status.strip().upper(), "Comparison": "EQUALS"}
            ]
        if record_state:
            filters["RecordState"] = [
                {"Value": record_state.strip().upper(), "Comparison": "EQUALS"}
            ]
        if product_name:
            filters["ProductName"] = [
                {"Value": product_name.strip(), "Comparison": "EQUALS"}
            ]
        if resource_id:
            filters["ResourceId"] = [
                {"Value": resource_id.strip(), "Comparison": "EQUALS"}
            ]
        if title_contains:
            filters["Title"] = [
                {"Value": title_contains.strip(), "Comparison": "CONTAINS"}
            ]
        updated = (updated_after or "").strip()
        if updated:
            filters["UpdatedAt"] = [
                {"Start": updated, "End": datetime.now(UTC).isoformat()}
            ]
        return filters

    async def get_securityhub_findings(
        severity_label: str = "",
        workflow_status: str = "",
        record_state: str = "ACTIVE",
        product_name: str = "",
        resource_id: str = "",
        title_contains: str = "",
        updated_after: str = "",
        limit: int = 25,
        next_token: str = "",
    ) -> str:
        """List Security Hub findings matching ASFF filters, newest first."""
        body: dict[str, Any] = {
            "Filters": _securityhub_filters(
                severity_label,
                workflow_status,
                record_state,
                product_name,
                resource_id,
                title_contains,
                updated_after,
            ),
            "SortCriteria": [{"Field": "UpdatedAt", "SortOrder": "desc"}],
            "MaxResults": min(max(_to_int(limit, 25), 1), 100),
        }
        if next_token:
            body["NextToken"] = next_token
        data, err = await _api(
            "post", "securityhub", "/findings", "get findings", body=body
        )
        if err:
            return err
        findings = data.get("Findings") or []
        out = format_entity_list(
            findings, _format_securityhub_finding, noun="Security Hub finding"
        )
        if data.get("NextToken"):
            out += f'\n[More results — pass next_token="{data["NextToken"]}"]'
        return out

    async def get_securityhub_finding(finding_id: str) -> str:
        """Get the full details of a single Security Hub finding by its Id."""
        if not finding_id:
            return "Error: finding_id is required."
        body: dict[str, Any] = {
            "Filters": _securityhub_filters(
                record_state="", finding_id=finding_id.strip()
            ),
            "MaxResults": 1,
        }
        data, err = await _api(
            "post", "securityhub", "/findings", "get finding", body=body
        )
        if err:
            return err
        findings = data.get("Findings") or []
        if not findings:
            return f"No Security Hub finding found with Id '{finding_id}'."
        f = findings[0]
        severity = f.get("Severity") or {}
        workflow = f.get("Workflow") or {}
        compliance = f.get("Compliance") or {}
        remediation = ((f.get("Remediation") or {}).get("Recommendation")) or {}
        resources = f.get("Resources") or []
        lines = [
            f"Finding: {f.get('Id', '?')}",
            f"Product ARN: {f.get('ProductArn', '?')}",
            f"Product: {f.get('ProductName', '?')} | Company: {f.get('CompanyName', '?')}",
            f"Title: {f.get('Title', '')}",
            f"Description: {_trunc(f.get('Description', ''), 600)}",
            f"Severity: {severity.get('Label', '?')} (normalized {severity.get('Normalized', '?')})",
            f"Workflow: {workflow.get('Status', '?')} | Record state: {f.get('RecordState', '?')}",
            f"Types: {', '.join(f.get('Types') or []) or '(none)'}",
            f"Account: {f.get('AwsAccountId', '?')} | Region: {f.get('Region', region)}",
            f"Created: {str(f.get('CreatedAt', ''))[:19]} | Updated: {str(f.get('UpdatedAt', ''))[:19]}",
        ]
        if compliance.get("Status"):
            lines.append(f"Compliance: {compliance.get('Status')}")
        for r in resources[:5]:
            lines.append(f"Resource: {r.get('Type', '?')} — {r.get('Id', '?')}")
        if remediation.get("Text") or remediation.get("Url"):
            lines.append(
                f"Remediation: {_trunc(remediation.get('Text', ''), 300)} "
                f"{remediation.get('Url', '')}".strip()
            )
        note = f.get("Note") or {}
        if note.get("Text"):
            lines.append(
                f"Note: {_trunc(note.get('Text'), 300)} "
                f"(by {note.get('UpdatedBy', '?')} at {str(note.get('UpdatedAt', ''))[:19]})"
            )
        return "\n".join(lines)

    async def list_securityhub_insights(
        insight_arns: str = "", limit: int = 25, next_token: str = ""
    ) -> str:
        """List Security Hub insights (grouped views over findings)."""
        body: dict[str, Any] = {"MaxResults": min(max(_to_int(limit, 25), 1), 100)}
        arns = _split_ids(insight_arns)
        if arns:
            body["InsightArns"] = arns
        if next_token:
            body["NextToken"] = next_token
        data, err = await _api(
            "post", "securityhub", "/insights/get", "get insights", body=body
        )
        if err:
            return err
        insights = data.get("Insights") or []

        def _fmt(i: dict) -> str:
            return (
                f"  {i.get('Name', '?')} — grouped by {i.get('GroupByAttribute', '?')}\n"
                f"    arn: {i.get('InsightArn', '?')}"
            )

        out = format_entity_list(insights, _fmt, noun="insight")
        if data.get("NextToken"):
            out += f'\n[More results — pass next_token="{data["NextToken"]}"]'
        return out

    async def get_securityhub_insight_results(insight_arn: str) -> str:
        """Get an insight's grouped result counts (e.g. top affected resources)."""
        if not insight_arn:
            return "Error: insight_arn is required."
        data, err = await _api(
            "get",
            "securityhub",
            f"/insights/results/{insight_arn.strip()}",
            "get insight results",
        )
        if err:
            return err
        results = data.get("InsightResults") or {}
        values = results.get("ResultValues") or []
        lines = [
            f"Insight results (grouped by {results.get('GroupByAttribute', '?')}):"
        ]
        for v in values[:50]:
            lines.append(
                f"  {v.get('GroupByAttributeValue', '?')}: {v.get('Count', 0)}"
            )
        if not values:
            lines.append("  (no matching findings)")
        return "\n".join(lines)

    async def update_securityhub_findings_workflow(
        findings: list[dict],
        workflow_status: str,
        note: str = "",
    ) -> str:
        """Update the workflow status (and optional note) of Security Hub findings."""
        status = (workflow_status or "").strip().upper()
        if status not in _WORKFLOW_STATUSES:
            return (
                f"Error: workflow_status must be one of {', '.join(_WORKFLOW_STATUSES)}."
            )
        identifiers: list[dict[str, str]] = []
        for f in findings or []:
            if not isinstance(f, dict):
                continue
            fid = str(f.get("id") or f.get("Id") or "").strip()
            arn = str(f.get("product_arn") or f.get("ProductArn") or "").strip()
            if fid and arn:
                identifiers.append({"Id": fid, "ProductArn": arn})
        if not identifiers:
            return (
                "Error: provide at least one finding with both 'id' and "
                "'product_arn' (from get_securityhub_findings)."
            )
        if len(identifiers) > 100:
            return "Error: at most 100 findings can be updated per call."
        body: dict[str, Any] = {
            "FindingIdentifiers": identifiers,
            "Workflow": {"Status": status},
        }
        if note.strip():
            body["Note"] = {"Text": note.strip(), "UpdatedBy": "xtm-one-agent"}
        data, err = await _api(
            "patch",
            "securityhub",
            "/findings/batchupdate",
            "update findings workflow",
            body=body,
        )
        if err:
            return err
        processed = data.get("ProcessedFindings") or []
        unprocessed = data.get("UnprocessedFindings") or []
        summary = (
            f"Workflow set to {status} on {len(processed)}/{len(identifiers)} "
            "finding(s)."
        )
        if unprocessed:
            failures = [
                f"{u.get('FindingIdentifier', {}).get('Id', '?')}: "
                f"{u.get('ErrorCode', '?')} ({_trunc(u.get('ErrorMessage', ''), 120)})"
                for u in unprocessed[:10]
            ]
            summary += "\nFailures:\n  " + "\n  ".join(failures)
        return summary

    # ══════════════════════════════════════════════════════════════════════
    # 3. CLOUDTRAIL
    # ══════════════════════════════════════════════════════════════════════

    async def lookup_cloudtrail_events(
        attribute_key: str = "",
        attribute_value: str = "",
        start_time: str = "",
        end_time: str = "",
        limit: int = 25,
        next_token: str = "",
    ) -> str:
        """Look up CloudTrail management events (last 90 days) for audit investigations."""
        body: dict[str, Any] = {"MaxResults": min(max(_to_int(limit, 25), 1), 50)}
        key = (attribute_key or "").strip()
        if key and key not in _LOOKUP_ATTRIBUTE_KEYS:
            return (
                f"Error: attribute_key must be one of {', '.join(_LOOKUP_ATTRIBUTE_KEYS)}."
            )
        if key and not (attribute_value or "").strip():
            return "Error: attribute_value is required when attribute_key is set."
        if key:
            body["LookupAttributes"] = [
                {"AttributeKey": key, "AttributeValue": attribute_value.strip()}
            ]
        start = _to_epoch(start_time)
        if start:
            body["StartTime"] = start
        end = _to_epoch(end_time)
        if end:
            body["EndTime"] = end
        if next_token:
            body["NextToken"] = next_token
        data, err = await _cloudtrail("LookupEvents", "look up events", body)
        if err:
            return err
        events = data.get("Events") or []

        def _fmt(e: dict) -> str:
            detail: dict[str, Any] = {}
            with contextlib.suppress(TypeError, ValueError):
                detail = _json.loads(e.get("CloudTrailEvent") or "{}")
            error_code = detail.get("errorCode", "")
            resources = e.get("Resources") or []
            resource_str = ", ".join(
                _trunc(r.get("ResourceName", "?"), 80) for r in resources[:2]
            )
            epoch = e.get("EventTime")
            when = (
                datetime.fromtimestamp(epoch, tz=UTC).isoformat()[:19]
                if isinstance(epoch, (int, float))
                else str(epoch or "")[:19]
            )
            line = (
                f"  {when} {e.get('EventName', '?')} "
                f"by {e.get('Username', '?')} via {e.get('EventSource', '?')}\n"
                f"    event id: {e.get('EventId', '?')} | "
                f"source IP: {detail.get('sourceIPAddress', '?')} | "
                f"read-only: {e.get('ReadOnly', detail.get('readOnly', '?'))}"
            )
            if error_code:
                line += f" | ERROR: {error_code}"
            if resource_str:
                line += f"\n    resources: {resource_str}"
            return line

        out = format_entity_list(events, _fmt, noun="CloudTrail event")
        if data.get("NextToken"):
            out += f'\n[More results — pass next_token="{data["NextToken"]}"]'
        return out

    # ══════════════════════════════════════════════════════════════════════
    # 4. EC2 NETWORK CONTAINMENT (approval-gated, reversible)
    # ══════════════════════════════════════════════════════════════════════

    async def _ec2(action: str, params: dict[str, str]) -> tuple[ET.Element | None, str | None]:
        """EC2 query-protocol call; the XML answer's root, namespaces kept."""
        if not access_key_id or not secret_access_key:
            return None, _AUTH_ERROR
        form = {"Action": action, "Version": _EC2_API_VERSION, **params}
        resp, err = await _signed_request(
            method="POST",
            service="ec2",
            region=region,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            path="/",
            raw_body=urlencode(form).encode("utf-8"),
            content_type="application/x-www-form-urlencoded",
            action=f"EC2 {action}",
        )
        if err:
            return None, err
        try:
            # AWS answers (trusted); stdlib ElementTree resolves no external entity.
            return ET.fromstring(resp.content or b"<empty/>"), None  # noqa: S314
        except ET.ParseError:
            return None, f"Error: {_PROVIDER} returned a non-XML response for EC2 {action}."

    async def _instance_network(instance_id: str) -> tuple[dict[str, Any] | None, str | None]:
        root, err = await _ec2("DescribeInstances", {"InstanceId.1": instance_id})
        if err:
            return None, err
        item = root.find(".//{*}instancesSet/{*}item")
        if item is None:
            return None, f"Error: EC2 instance {instance_id} was not found in region {region}."
        enis = item.findall("./{*}networkInterfaceSet/{*}item")
        groups = [g.findtext("{*}groupId") or "" for g in item.findall("./{*}groupSet/{*}item")]
        return {
            "vpc_id": item.findtext("{*}vpcId") or "",
            "enis": len(enis),
            "groups": [g for g in groups if g],
        }, None

    async def isolate_aws_ec2_instance(instance_id: str, reason: str) -> str:
        """Cut an EC2 instance off the network with an empty security group.

        The isolation group has no inbound rule and its default outbound rule
        is revoked; it replaces every group of the instance, and the replaced
        group ids are kept in its tags for release_aws_ec2_instance_isolation.
        Established connections tracked by a replaced group can outlive it.
        """
        iid = (instance_id or "").strip()
        if not _EC2_INSTANCE_ID.match(iid):
            return "Error: instance_id is an EC2 instance id, e.g. i-0123456789abcdef0."
        if not (reason or "").strip():
            return "Error: reason is required (it is written on the isolation group)."
        network, err = await _instance_network(iid)
        if err:
            return err
        if not network["vpc_id"]:
            return f"Error: EC2 instance {iid} is not in a VPC; it cannot be isolated by security group."
        if network["enis"] > 1:
            return (
                f"Error: EC2 instance {iid} has {network['enis']} network interfaces; "
                "isolate it at its subnet network ACL or by hand."
            )
        if any(g for g in network["groups"] if g.startswith("sg-")) and len(network["groups"]) == 1:
            tagged, _ = await _ec2(
                "DescribeSecurityGroups",
                {"GroupId.1": network["groups"][0], "Filter.1.Name": "tag-key", "Filter.1.Value.1": _ISOLATION_TAG},
            )
            if tagged is not None and tagged.find(".//{*}securityGroupInfo/{*}item") is not None:
                return f"EC2 instance {iid} is already isolated (group {network['groups'][0]})."
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        created, err = await _ec2(
            "CreateSecurityGroup",
            {
                "GroupName": f"xtm-isolation-{iid}-{stamp}",
                "GroupDescription": _sg_description(f"Isolation of {iid}: {reason}"),
                "VpcId": network["vpc_id"],
                "TagSpecification.1.ResourceType": "security-group",
                "TagSpecification.1.Tag.1.Key": _ISOLATION_TAG,
                "TagSpecification.1.Tag.1.Value": ",".join(network["groups"]) or "none",
                "TagSpecification.1.Tag.2.Key": "xtm-one:isolated-instance",
                "TagSpecification.1.Tag.2.Value": iid,
            },
        )
        if err:
            return err
        group_id = created.findtext(".//{*}groupId") or ""
        if not group_id:
            return "Error: EC2 CreateSecurityGroup returned no group id."
        _, err = await _ec2(
            "RevokeSecurityGroupEgress",
            {
                "GroupId": group_id,
                "IpPermissions.1.IpProtocol": "-1",
                "IpPermissions.1.IpRanges.1.CidrIp": "0.0.0.0/0",
            },
        )
        if err:
            return f"{err}\nThe isolation group {group_id} was created but still allows outbound traffic; it was NOT applied."
        _, err = await _ec2("ModifyInstanceAttribute", {"InstanceId": iid, "GroupId.1": group_id})
        if err:
            return f"{err}\nThe isolation group {group_id} was created but NOT applied to {iid}."
        return (
            f"EC2 instance {iid} isolated: its security groups "
            f"({', '.join(network['groups']) or 'none'}) were replaced by {group_id}, "
            "which allows no traffic in or out. Connections already tracked may "
            "persist until they close. Undo: release_aws_ec2_instance_isolation."
            f"\nReason: {reason.strip()}"
        )

    async def release_aws_ec2_instance_isolation(instance_id: str) -> str:
        """Give an isolated EC2 instance back the security groups it had."""
        iid = (instance_id or "").strip()
        if not _EC2_INSTANCE_ID.match(iid):
            return "Error: instance_id is an EC2 instance id, e.g. i-0123456789abcdef0."
        network, err = await _instance_network(iid)
        if err:
            return err
        if len(network["groups"]) != 1:
            return f"EC2 instance {iid} is not isolated by this integration (it has {len(network['groups'])} groups)."
        group_id = network["groups"][0]
        described, err = await _ec2("DescribeSecurityGroups", {"GroupId.1": group_id})
        if err:
            return err
        original = ""
        for tag in described.findall(".//{*}securityGroupInfo/{*}item/{*}tagSet/{*}item"):
            if tag.findtext("{*}key") == _ISOLATION_TAG:
                original = tag.findtext("{*}value") or ""
        groups = [g for g in original.split(",") if g.startswith("sg-")]
        if not groups:
            return f"EC2 instance {iid} is not isolated by this integration (group {group_id} carries no record)."
        params = {"InstanceId": iid}
        params.update({f"GroupId.{i}": g for i, g in enumerate(groups, start=1)})
        _, err = await _ec2("ModifyInstanceAttribute", params)
        if err:
            return err
        _, cleanup_err = await _ec2("DeleteSecurityGroup", {"GroupId": group_id})
        cleanup = (
            f" The isolation group {group_id} could not be deleted: {cleanup_err}"
            if cleanup_err
            else f" Isolation group {group_id} deleted."
        )
        return f"EC2 instance {iid} released: security groups {', '.join(groups)} restored.{cleanup}"

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("get_aws_caller_identity", get_aws_caller_identity)
    executor.register("list_guardduty_detectors", list_guardduty_detectors)
    executor.register("list_guardduty_findings", list_guardduty_findings)
    executor.register("get_guardduty_finding", get_guardduty_finding)
    executor.register("get_guardduty_statistics", get_guardduty_statistics)
    executor.register("archive_guardduty_findings", archive_guardduty_findings)
    executor.register("unarchive_guardduty_findings", unarchive_guardduty_findings)
    executor.register("get_securityhub_summary", get_securityhub_summary)
    executor.register("get_securityhub_findings", get_securityhub_findings)
    executor.register("get_securityhub_finding", get_securityhub_finding)
    executor.register("list_securityhub_insights", list_securityhub_insights)
    executor.register(
        "get_securityhub_insight_results", get_securityhub_insight_results
    )
    executor.register(
        "update_securityhub_findings_workflow", update_securityhub_findings_workflow
    )
    executor.register("lookup_cloudtrail_events", lookup_cloudtrail_events)
    executor.register("isolate_aws_ec2_instance", isolate_aws_ec2_instance)
    executor.register("release_aws_ec2_instance_isolation", release_aws_ec2_instance_isolation)

    src = "integration:aws_security"
    _str = {"type": "string"}
    _detector = {
        "type": "string",
        "description": (
            "GuardDuty detector ID (optional — auto-discovered via "
            "ListDetectors when omitted)"
        ),
    }
    return [
        # ── 0. Account context ────────────────────────────────────────────
        ToolDefinition(
            name="get_aws_caller_identity",
            description=(
                "Get the AWS account ID and IAM principal ARN this integration "
                "acts as (STS GetCallerIdentity). Use it first to confirm "
                "which AWS account and region you are investigating."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=src,
            priority=1,
        ),
        # ── 1. GuardDuty ──────────────────────────────────────────────────
        ToolDefinition(
            name="list_guardduty_detectors",
            description=(
                "List Amazon GuardDuty detectors in the configured region with "
                "their status and finding publishing frequency. Other GuardDuty "
                "tools auto-discover the detector, so you rarely need this "
                "except to verify GuardDuty is enabled."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="list_guardduty_findings",
            description=(
                "List Amazon GuardDuty threat-detection findings sorted by "
                "severity (highest first), with full details for each result. "
                "Filter by minimum severity (1-10 numeric: Low 1-3.9, Medium "
                "4-6.9, High 7-8.9, Critical 9-10), exact finding type (e.g. "
                "'UnauthorizedAccess:IAMUser/MaliciousIPCaller'), archived "
                "state, and last-update time. Use for SOC triage of active "
                "cloud threats; follow up with get_guardduty_finding or "
                "archive_guardduty_findings using the returned finding IDs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "detector_id": _detector,
                    "min_severity": {
                        "type": "number",
                        "description": (
                            "Only findings with severity >= this value "
                            "(1-10; e.g. 7 = High and Critical only). 0 = all."
                        ),
                        "default": 0,
                    },
                    "finding_type": {
                        "type": "string",
                        "description": "Exact GuardDuty finding type to match (optional)",
                    },
                    "archived": {
                        "type": "string",
                        "enum": ["false", "true", "all"],
                        "description": (
                            "'false' = active findings only (default), 'true' = "
                            "archived only, 'all' = both"
                        ),
                        "default": "false",
                    },
                    "updated_after": {
                        "type": "string",
                        "description": (
                            "Only findings updated after this ISO-8601 timestamp "
                            "(e.g. '2026-07-01T00:00:00Z')"
                        ),
                    },
                    "limit": {"type": "integer", "default": 25, "description": "Max results (1-50)"},
                    "next_token": {"type": "string", "description": "Pagination cursor from a previous call"},
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_guardduty_finding",
            description=(
                "Get the full details of one or more GuardDuty findings by ID "
                "(comma-separated, up to 50): type, severity, description, "
                "affected resource, action, first/last seen, and occurrence "
                "count. Finding IDs come from list_guardduty_findings."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "finding_ids": {
                        "type": "string",
                        "description": "Finding ID(s), comma-separated (max 50)",
                    },
                    "detector_id": _detector,
                },
                "required": ["finding_ids"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_guardduty_statistics",
            description=(
                "Get the count of GuardDuty findings grouped by severity level "
                "— a fast posture overview before drilling into individual "
                "findings. Accepts the same filters as list_guardduty_findings."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "detector_id": _detector,
                    "min_severity": {"type": "number", "default": 0},
                    "finding_type": _str,
                    "archived": {
                        "type": "string",
                        "enum": ["false", "true", "all"],
                        "default": "false",
                    },
                    "updated_after": {
                        "type": "string",
                        "description": "ISO-8601 lower bound on update time (optional)",
                    },
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="archive_guardduty_findings",
            description=(
                "Archive GuardDuty findings by ID (comma-separated, up to 50) "
                "to mark them as triaged / not actionable. Archived findings "
                "disappear from the active queue but can be reopened with "
                "unarchive_guardduty_findings. Requires a GuardDuty "
                "administrator account."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "finding_ids": {
                        "type": "string",
                        "description": "Finding ID(s) to archive, comma-separated (max 50)",
                    },
                    "detector_id": _detector,
                },
                "required": ["finding_ids"],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="unarchive_guardduty_findings",
            description=(
                "Unarchive previously archived GuardDuty findings by ID "
                "(comma-separated, up to 50), returning them to the active "
                "findings queue for investigation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "finding_ids": {
                        "type": "string",
                        "description": "Finding ID(s) to unarchive, comma-separated (max 50)",
                    },
                    "detector_id": _detector,
                },
                "required": ["finding_ids"],
            },
            source=src,
            priority=3,
        ),
        # ── 2. Security Hub ───────────────────────────────────────────────
        ToolDefinition(
            name="get_securityhub_summary",
            description=(
                "Get the AWS Security Hub status for the account/region and "
                "the list of enabled security standards (e.g. AWS Foundational "
                "Security Best Practices, CIS AWS Foundations). Use it for a "
                "quick compliance-posture overview."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_securityhub_findings",
            description=(
                "List AWS Security Hub findings (aggregated across GuardDuty, "
                "Inspector, Macie, config checks, and partner products) newest "
                "first, with structured ASFF filters: severity label, workflow "
                "status, record state, product name, resource ID, title "
                "keyword, and update time. Returns each finding's Id and "
                "ProductArn, which update_securityhub_findings_workflow needs. "
                "Prefer this over list_guardduty_findings for cross-service "
                "triage."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "severity_label": {
                        "type": "string",
                        "enum": list(_SEVERITY_LABELS),
                        "description": "Only findings with this severity label",
                    },
                    "workflow_status": {
                        "type": "string",
                        "enum": list(_WORKFLOW_STATUSES),
                        "description": "Only findings in this workflow status",
                    },
                    "record_state": {
                        "type": "string",
                        "enum": ["ACTIVE", "ARCHIVED", ""],
                        "description": "'ACTIVE' (default) or 'ARCHIVED'; '' = both",
                        "default": "ACTIVE",
                    },
                    "product_name": {
                        "type": "string",
                        "description": "Only findings from this product (e.g. 'GuardDuty', 'Inspector')",
                    },
                    "resource_id": {
                        "type": "string",
                        "description": "Only findings affecting this exact resource ID/ARN",
                    },
                    "title_contains": {
                        "type": "string",
                        "description": "Only findings whose title contains this text",
                    },
                    "updated_after": {
                        "type": "string",
                        "description": "Only findings updated after this ISO-8601 timestamp",
                    },
                    "limit": {"type": "integer", "default": 25, "description": "Max results (1-100)"},
                    "next_token": {"type": "string", "description": "Pagination cursor from a previous call"},
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_securityhub_finding",
            description=(
                "Get the full detail of a single Security Hub finding by its "
                "Id (from get_securityhub_findings): description, severity, "
                "workflow status, compliance status, affected resources, "
                "remediation recommendation, and analyst notes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "finding_id": {
                        "type": "string",
                        "description": "The finding's Id (usually an ARN)",
                    }
                },
                "required": ["finding_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="list_securityhub_insights",
            description=(
                "List Security Hub custom insights — saved, grouped views over "
                "findings (e.g. 'findings by resource'). Returns each "
                "insight's ARN for get_securityhub_insight_results. Without "
                "insight_arns only custom insights are returned (managed "
                "insights need their explicit ARNs)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "insight_arns": {
                        "type": "string",
                        "description": "Specific insight ARN(s), comma-separated (optional)",
                    },
                    "limit": {"type": "integer", "default": 25},
                    "next_token": {"type": "string"},
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_securityhub_insight_results",
            description=(
                "Get the grouped result counts of a Security Hub insight (e.g. "
                "the resources or accounts with the most matching findings). "
                "Use the ARN from list_securityhub_insights."
            ),
            input_schema={
                "type": "object",
                "properties": {"insight_arn": {"type": "string"}},
                "required": ["insight_arn"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="update_securityhub_findings_workflow",
            description=(
                "Update the workflow status of up to 100 Security Hub findings "
                "in one call — NEW, NOTIFIED, RESOLVED, or SUPPRESSED — with "
                "an optional analyst note. Each finding needs its 'id' and "
                "'product_arn' exactly as returned by get_securityhub_findings. "
                "Use RESOLVED to close remediated findings and SUPPRESSED for "
                "accepted risks / false positives."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "findings": {
                        "type": "array",
                        "description": "Findings to update",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "description": "The finding's Id",
                                },
                                "product_arn": {
                                    "type": "string",
                                    "description": "The finding's ProductArn",
                                },
                            },
                            "required": ["id", "product_arn"],
                        },
                    },
                    "workflow_status": {
                        "type": "string",
                        "enum": list(_WORKFLOW_STATUSES),
                        "description": "New workflow status",
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional analyst note recorded on the findings",
                    },
                },
                "required": ["findings", "workflow_status"],
            },
            source=src,
            priority=3,
        ),
        # ── 3. CloudTrail ─────────────────────────────────────────────────
        ToolDefinition(
            name="lookup_cloudtrail_events",
            description=(
                "Look up AWS CloudTrail management events from the last 90 "
                "days for audit investigations — who performed which API call, "
                "when, from which IP. Filter by ONE attribute (EventName like "
                "'ConsoleLogin' or 'DeleteTrail', Username, EventSource like "
                "'iam.amazonaws.com', ResourceName, ResourceType, AccessKeyId, "
                "EventId, or ReadOnly) plus an optional time window. Note: AWS "
                "rate-limits this API to 2 requests/second."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "attribute_key": {
                        "type": "string",
                        "enum": list(_LOOKUP_ATTRIBUTE_KEYS),
                        "description": "Attribute to filter on (optional; only one supported)",
                    },
                    "attribute_value": {
                        "type": "string",
                        "description": "Value for the attribute filter (required with attribute_key)",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Only events after this ISO-8601 timestamp (optional)",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "Only events before this ISO-8601 timestamp (optional)",
                    },
                    "limit": {"type": "integer", "default": 25, "description": "Max results (1-50)"},
                    "next_token": {"type": "string", "description": "Pagination cursor from a previous call"},
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        # ── 4. EC2 network containment ────────────────────────────────────
        ToolDefinition(
            name="isolate_aws_ec2_instance",
            description=(
                "Isolate an EC2 instance: an empty security group replaces its groups "
                "(each call asks a person). Undo: release_aws_ec2_instance_isolation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "instance_id": {"type": "string", "description": "EC2 instance id (i-...)"},
                    "reason": {"type": "string", "description": "Reason (required)"},
                },
                "required": ["instance_id", "reason"],
            },
            source=src,
            priority=3,
            always_ask=True,
        ),
        ToolDefinition(
            name="release_aws_ec2_instance_isolation",
            description="Release an EC2 instance isolated by isolate_aws_ec2_instance: its security groups are restored.",
            input_schema={
                "type": "object",
                "properties": {
                    "instance_id": {"type": "string", "description": "EC2 instance id (i-...)"},
                },
                "required": ["instance_id"],
            },
            source=src,
            priority=3,
            always_ask=True,
        ),
    ]
