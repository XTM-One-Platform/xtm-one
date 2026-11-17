# SPDX-License-Identifier: Apache-2.0

"""Proofpoint Targeted Attack Protection (TAP) integration.

Full agentic access to the TAP Dashboard API (https://tap-api-v2.proofpoint.com/v2)
for email-security agents:

  - SIEM API: blocked/permitted clicks and blocked/delivered messages with full
    threat details (``list_proofpoint_siem_events`` — consolidated event_type
    enum over all six SIEM endpoints).
  - Campaign API: campaign IDs active in a window (``list_proofpoint_campaigns``)
    and campaign details with actors / malware families / techniques
    (``get_proofpoint_campaign``).
  - Forensics API: forensic evidence (IOCs) for a threat or campaign
    (``get_proofpoint_forensics``).
  - People API: Very Attacked People rankings (``list_proofpoint_vap``) and
    top clickers (``list_proofpoint_top_clickers``).
  - Threat API: threat summary details (``get_proofpoint_threat``).
  - URL Decoder: decode Proofpoint-rewritten (URL Defense) links back to their
    original target URLs (``decode_proofpoint_urls``) — essential for agents
    reading emails protected by TAP.

The TAP Dashboard API exposes no write operations, so every tool is read-only.

Authentication is **HTTP Basic** with a service principal (username) and secret
(password) generated in the TAP Dashboard (Settings > Connected Applications).
The integration stores the secret in ``api_key`` and the principal in
``extra_config.principal``; ``integration_tools._get_token`` assembles the
compound ``principal||secret`` that this builder parses (mirrors hackerone /
intel471).

Hard API constraints are encoded in the tool schemas and defaults:

  - SIEM queries cover at most ONE HOUR per request and reach at most 7 days
    into the past (``since_seconds`` is clamped to [30, 3600]).
  - Campaign-ID windows cover at most ONE DAY, and the endpoint has a strict
    50-requests-per-24h quota (campaign *detail* lookups are unthrottled).
  - People endpoints accept only 14 / 30 / 90 day windows (50 requests/24h).
"""

from __future__ import annotations

import base64
import json as _json
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "Proofpoint TAP"
_BASE_URL = "https://tap-api-v2.proofpoint.com/v2"
_DEFAULT_TIMEOUT = 30
_SIEM_TIMEOUT = 60

# SIEM windows: minimum 30 seconds, maximum 1 hour per request (API contract).
_SIEM_MIN_SECONDS = 30
_SIEM_MAX_SECONDS = 3600

# Cap the number of events rendered per SIEM category so a busy hour cannot
# blow up the tool result (the API itself has no paging).
_MAX_EVENTS_SHOWN = 20

_SIEM_ENDPOINTS: dict[str, str] = {
    "all": "/siem/all",
    "issues": "/siem/issues",
    "clicks_blocked": "/siem/clicks/blocked",
    "clicks_permitted": "/siem/clicks/permitted",
    "messages_blocked": "/siem/messages/blocked",
    "messages_delivered": "/siem/messages/delivered",
}

_THREAT_TYPES = ("url", "attachment", "messageText")
_THREAT_STATUSES = ("active", "cleared", "falsePositive")
_PEOPLE_WINDOWS = (14, 30, 90)


# ---------------------------------------------------------------------------
# Auth + formatting helpers
# ---------------------------------------------------------------------------


def _basic_auth_header(principal: str, secret: str) -> str:
    """Build the HTTP Basic ``Authorization`` header for the service credential.

    Both parts are stripped: a value pasted with a trailing newline/space would
    otherwise fail authentication.
    """
    raw = f"{principal.strip()}:{secret.strip()}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _is_unsafe_path_id(value: Any) -> bool:
    """True if *value* would alter the URL path structure if interpolated.

    Campaign and threat ids are dropped into request paths, so an id containing
    a path separator or a ``.``/``..`` traversal token could escape the
    intended endpoint. ``%`` is rejected too so percent-encoded separators
    (e.g. ``%2F``) can't smuggle a traversal past the guard once the server
    decodes the path segment. Reject those (and empty) so a crafted/
    hallucinated id can't widen what a tool calls.
    """
    s = str(value or "").strip()
    return not s or s in (".", "..") or any(c in s for c in "/\\?#%\x00")


def _to_int(value: Any, default: int) -> int | None:
    """Coerce a tool argument to ``int``, or ``None`` when unparseable.

    Blank/missing values fall back to *default* so schema defaults keep
    working; garbage (``"ten"``, dicts, …) returns ``None`` so the tool can
    respond with a self-correcting error string instead of raising into the
    executor's generic fallback.
    """
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _trunc(text: Any, max_len: int = 200) -> str:
    if not text:
        return ""
    text = str(text)
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _dt(value: Any) -> str:
    """Trim an ISO timestamp to minute precision for display."""
    return (str(value) if value else "")[:16] or "N/A"


def _fmt_named(items: Any) -> str:
    """Render a list of ``{"id": ..., "name": ...}`` objects as ``name (id)``."""
    if not isinstance(items, list) or not items:
        return ""
    return ", ".join(
        f"{i.get('name', '?')} ({i.get('id', '?')})"
        for i in items
        if isinstance(i, dict)
    )


def _fmt_threat_info(threats: Any) -> str:
    """One line per ``threatsInfoMap`` entry: type, classification, id, artifact."""
    if not isinstance(threats, list) or not threats:
        return ""
    lines = []
    for t in threats:
        if not isinstance(t, dict):
            continue
        threat_id = t.get("threatId") or t.get("threatID") or "?"
        campaign = t.get("campaignId") or t.get("campaignID")
        bits = [
            f"      threat: [{t.get('threatType', '?')}/"
            f"{t.get('classification', '?')}] id={threat_id}"
            f" status={t.get('threatStatus', '?')}"
        ]
        if t.get("threat"):
            bits.append(f"        artifact: {_trunc(t.get('threat'), 150)}")
        if campaign:
            bits.append(f"        campaign: {campaign}")
        lines.append("\n".join(bits))
    return "\n".join(lines)


def _fmt_message_event(evt: dict) -> str:
    """Compact one-block rendering of a SIEM message event."""
    recipients = evt.get("recipient")
    if isinstance(recipients, list):
        recipients = ", ".join(recipients)
    scores = []
    for key, label in (
        ("malwareScore", "malware"),
        ("phishScore", "phish"),
        ("impostorScore", "impostor"),
        ("spamScore", "spam"),
    ):
        value = evt.get(key)
        if isinstance(value, int) and value > 0:
            scores.append(f"{label}={value}")
    lines = [
        f"  • [{_dt(evt.get('messageTime'))}] {_trunc(evt.get('subject'), 120) or '(no subject)'}",
        f"    from: {evt.get('fromAddress') or evt.get('sender', 'N/A')}  "
        f"to: {_trunc(recipients, 150) or 'N/A'}",
        f"    GUID: {evt.get('GUID', 'N/A')}"
        + (f"  scores: {', '.join(scores)}" if scores else ""),
    ]
    if evt.get("quarantineFolder"):
        lines.append(
            f"    quarantine: {evt.get('quarantineFolder')} "
            f"(rule: {evt.get('quarantineRule', 'N/A')})"
        )
    threats = _fmt_threat_info(evt.get("threatsInfoMap"))
    if threats:
        lines.append(threats)
    return "\n".join(lines)


def _fmt_click_event(evt: dict) -> str:
    """Compact one-block rendering of a SIEM click event."""
    lines = [
        f"  • [{_dt(evt.get('clickTime'))}] "
        f"[{evt.get('classification', '?')}] {_trunc(evt.get('url'), 150)}",
        f"    recipient: {evt.get('recipient', 'N/A')}  "
        f"clickIP: {evt.get('clickIP', 'N/A')}  status: {evt.get('threatStatus', '?')}",
        f"    threatID: {evt.get('threatID', 'N/A')}",
    ]
    if evt.get("campaignId"):
        lines.append(f"    campaign: {evt.get('campaignId')}")
    return "\n".join(lines)


def _fmt_person(user: dict, stats_key: str) -> str:
    """Render a People API user (VAP or top-clicker) as a compact block."""
    identity = user.get("identity") or {}
    emails = identity.get("emails") or []
    email = emails[0] if emails else "N/A"
    name = identity.get("name") or email
    stats = user.get(stats_key) or user.get("threatStatistics") or {}
    families = stats.get("families") or []
    fam_bits = []
    for fam in families[:5]:
        if not isinstance(fam, dict):
            continue
        metric = fam.get("score", fam.get("clicks", "?"))
        fam_bits.append(f"{fam.get('name', '?')}={metric}")
    meta = []
    if identity.get("title"):
        meta.append(str(identity["title"]))
    if identity.get("department"):
        meta.append(str(identity["department"]))
    if identity.get("vip"):
        meta.append("VIP")
    headline = (
        f"attack index: {stats.get('attackIndex')}"
        if "attackIndex" in stats
        else f"clicks: {stats.get('clickCount', '?')}"
    )
    lines = [f"  • {name} <{email}> — {headline}"]
    if meta:
        lines.append(f"    {' | '.join(meta)}")
    if fam_bits:
        lines.append(f"    families: {', '.join(fam_bits)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Connectivity validation (shared with the integration test endpoint via
# ``test_provider_connectivity``).
# ---------------------------------------------------------------------------


async def validate_connection(principal: str, secret: str) -> tuple[bool, str]:
    """Verify TAP credentials with a minimal SIEM query (last 5 minutes)."""
    # Strip before validating so whitespace-only input gets the friendly
    # missing-credentials message (matching _basic_auth_header's stripping)
    # instead of a confusing authentication failure.
    principal = (principal or "").strip()
    secret = (secret or "").strip()
    if not principal or not secret:
        return False, "Missing TAP service principal or secret."
    headers = {
        "Authorization": _basic_auth_header(principal, secret),
        "Accept": "application/json",
    }
    try:
        async with egress_client(timeout=20) as client:
            resp = await client.get(
                f"{_BASE_URL}/siem/clicks/blocked",
                headers=headers,
                params={"format": "json", "sinceSeconds": 300},
            )
    except httpx.HTTPError as exc:
        return False, f"Connection failed: {exc}"
    if resp.status_code in (200, 204):
        return True, "Proofpoint TAP is connected and authenticated."
    if resp.status_code in (401, 403):
        return False, (
            "Authentication failed — check the TAP service principal and "
            "secret (generated in the TAP Dashboard under Settings > "
            "Connected Applications)."
        )
    if resp.status_code == 429:
        return False, (
            "Proofpoint TAP throttled the request (HTTP 429) — the rolling "
            "24-hour SIEM quota is exhausted. The credentials may still be "
            "valid; try again later."
        )
    return False, f"Proofpoint TAP returned HTTP {resp.status_code}."


class ProofpointIntegration(BaseIntegration):
    """Proofpoint Targeted Attack Protection (TAP) email security integration."""

    provider_name = "proofpoint"
    display_name = "Proofpoint TAP"
    icon = "MailWarning"
    description = (
        "Connect Proofpoint TAP to triage email threat events, pivot to "
        "campaigns, threats, and forensic evidence, rank Very Attacked "
        "People, and decode URL Defense rewritten links."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        principal, secret = parse_compound_token(token, 2, greedy_last=True)
        ok, _ = await validate_connection(principal, secret)
        return ok

    async def get_user_info(self, token: str) -> dict:
        # TAP has no identity endpoint; surface the principal for display.
        principal, _secret = parse_compound_token(token, 2, greedy_last=True)
        return {"principal": principal, "provider": "proofpoint"}


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Proofpoint TAP tools.

    ``token`` is the compound ``principal||secret`` assembled by
    ``integration_tools._get_token`` from the integration's stored service
    principal (extra_config) and secret (api_key).
    """
    principal, secret = parse_compound_token(token, 2, greedy_last=True)
    auth_header = _basic_auth_header(principal, secret)

    def _headers(*, with_body: bool = False) -> dict[str, str]:
        headers = {"Authorization": auth_header, "Accept": "application/json"}
        if with_body:
            headers["Content-Type"] = "application/json"
        return headers

    async def _api(
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> tuple[Any, str | None]:
        """Authenticated JSON request helper returning ``(json, error)``.

        Every TAP read is a GET; only the URL decoder POSTs a JSON body.
        """
        has_body = json is not None
        method = "post" if has_body else "get"
        request_kwargs: dict[str, Any] = {"headers": _headers(with_body=has_body)}
        if params is not None:
            request_kwargs["params"] = params
        if has_body:
            request_kwargs["json"] = json
        async with egress_client(timeout=timeout) as client:
            resp, err = await safe_request(
                client,
                method,
                f"{_BASE_URL}{path}",
                _PROVIDER,
                action,
                **request_kwargs,
            )
            if err:
                return None, err
            # 204 No Content / empty body (empty window) — empty payload.
            if resp.status_code == 204 or not resp.content:
                return {}, None
            try:
                return resp.json(), None
            except ValueError:
                # A 2xx with a garbled/non-JSON body is a real API problem
                # (e.g. an HTML error page during a partial outage) — surface
                # it instead of masking it as an empty result set.
                return None, (
                    f"Error: {_PROVIDER} returned a non-JSON response while "
                    f"trying to {action} (HTTP {resp.status_code}). The "
                    "service may be degraded — try again later."
                )

    def _render_events(events: Any, formatter, label: str) -> list[str]:
        """Render one SIEM event category, capped at _MAX_EVENTS_SHOWN."""
        if not isinstance(events, list) or not events:
            return []
        shown = events[:_MAX_EVENTS_SHOWN]
        out = [f"\n{label} ({len(events)}):"]
        out.extend(formatter(e) for e in shown if isinstance(e, dict))
        if len(events) > _MAX_EVENTS_SHOWN:
            out.append(
                f"  … {len(events) - _MAX_EVENTS_SHOWN} more {label} events not "
                "shown — narrow the time window or filter by threat_type/"
                "threat_status."
            )
        return out

    # ══════════════════════════════════════════════════════════════════════
    # SIEM API
    # ══════════════════════════════════════════════════════════════════════

    async def list_proofpoint_siem_events(
        event_type: str = "all",
        since_seconds: int = 3600,
        interval: str = "",
        threat_type: str = "",
        threat_status: str = "",
    ) -> str:
        """Fetch TAP SIEM threat events (clicks and/or messages) for a time window.

        The API returns at most ONE HOUR of data per request and reaches at
        most 7 days into the past. Use ``interval`` (ISO8601, e.g.
        ``2026-07-10T09:00:00Z/2026-07-10T10:00:00Z`` or ``PT30M/2026-07-10T10:00:00Z``)
        for historical windows; otherwise ``since_seconds`` covers the most
        recent window.
        """
        endpoint = _SIEM_ENDPOINTS.get((event_type or "all").strip().lower())
        if not endpoint:
            return (
                f"Error: unknown event_type '{event_type}'. Supported values: "
                f"{', '.join(sorted(_SIEM_ENDPOINTS))}."
            )
        params: dict[str, Any] = {"format": "json"}
        interval = (str(interval) if interval else "").strip()
        if interval:
            params["interval"] = interval
        else:
            try:
                seconds = int(since_seconds)
            except (TypeError, ValueError):
                return "Error: since_seconds must be an integer number of seconds."
            seconds = max(_SIEM_MIN_SECONDS, min(seconds, _SIEM_MAX_SECONDS))
            params["sinceSeconds"] = seconds
        threat_type = (str(threat_type) if threat_type else "").strip()
        if threat_type:
            if threat_type not in _THREAT_TYPES:
                return (
                    f"Error: threat_type must be one of {', '.join(_THREAT_TYPES)}."
                )
            params["threatType"] = threat_type
        threat_status = (str(threat_status) if threat_status else "").strip()
        if threat_status:
            if threat_status not in _THREAT_STATUSES:
                return (
                    "Error: threat_status must be one of "
                    f"{', '.join(_THREAT_STATUSES)}."
                )
            params["threatStatus"] = threat_status
        data, err = await _api(
            endpoint,
            f"list SIEM events ({event_type})",
            params=params,
            timeout=_SIEM_TIMEOUT,
        )
        if err:
            if "bad request" in err:
                # The most common 400 cause is a window violation — make the
                # constraint actionable instead of leaving a bare error.
                err += (
                    " Reminder: SIEM windows span at most ONE HOUR per "
                    "request and reach at most 7 days into the past — check "
                    "the 'interval' bounds (e.g. "
                    "'2026-07-10T09:00:00Z/2026-07-10T10:00:00Z')."
                )
            return err
        if not isinstance(data, dict):
            data = {}
        categories = (
            ("messagesDelivered", _fmt_message_event, "Messages delivered (with threats)"),
            ("messagesBlocked", _fmt_message_event, "Messages blocked"),
            ("clicksPermitted", _fmt_click_event, "Clicks permitted"),
            ("clicksBlocked", _fmt_click_event, "Clicks blocked"),
        )
        lines: list[str] = []
        total = 0
        for key, formatter, label in categories:
            events = data.get(key)
            if isinstance(events, list):
                total += len(events)
            lines.extend(_render_events(events, formatter, label))
        if total == 0:
            window = params.get("interval") or f"last {params.get('sinceSeconds')}s"
            return (
                f"No TAP threat events found for {window} "
                f"(event_type={event_type}). The window may simply be quiet — "
                "try a different hour with `interval`."
            )
        header = f"Found {total} TAP threat event(s)"
        if data.get("queryEndTime"):
            header += f" (query end time: {data['queryEndTime']})"
        return header + ":" + "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # Campaign API
    # ══════════════════════════════════════════════════════════════════════

    async def list_proofpoint_campaigns(
        interval: str,
        size: int = 100,
        page: int = 1,
    ) -> str:
        """List IDs of campaigns active in a time window (max 1 day per request)."""
        if not (interval or "").strip():
            return (
                "Error: interval is required — an ISO8601 interval of at most "
                "ONE DAY, e.g. 2026-07-09T00:00:00Z/2026-07-10T00:00:00Z."
            )
        size_val = _to_int(size, 100)
        page_val = _to_int(page, 1)
        if size_val is None or page_val is None:
            return "Error: size and page must be integers."
        params: dict[str, Any] = {
            "interval": str(interval).strip(),
            "size": max(1, min(size_val, 200)),
            "page": max(1, page_val),
        }
        data, err = await _api("/campaign/ids", "list campaigns", params=params)
        if err:
            if "bad request" in err:
                err += (
                    " Reminder: campaign-ID windows span at most ONE DAY per "
                    "request — check the 'interval' bounds."
                )
            return err
        campaigns = (data or {}).get("campaigns")
        if not isinstance(campaigns, list):
            campaigns = []

        def _fmt(c: dict) -> str:
            flags = []
            if c.get("notable"):
                flags.append("notable")
            if c.get("verticallyTargeted"):
                flags.append("vertically targeted")
            tail = f"  [{', '.join(flags)}]" if flags else ""
            return (
                f"  • {c.get('id', '?')}  "
                f"(updated: {_dt(c.get('lastUpdatedAt'))}){tail}"
            )

        out = format_entity_list(
            campaigns,
            _fmt,
            noun="campaign",
            empty_msg=f"No campaigns were active in {interval}.",
        )
        if len(campaigns) >= params["size"]:
            out += (
                f"\n\n(Possibly more results — pass page={params['page'] + 1} "
                "to paginate.)"
            )
        return (
            out
            + "\n\nUse get_proofpoint_campaign with a campaign ID for details "
            "(actors, malware, techniques, threat variants)."
        )

    async def get_proofpoint_campaign(campaign_id: str) -> str:
        """Get campaign details: description, actors, malware, techniques, variants."""
        if _is_unsafe_path_id(campaign_id):
            return (
                "Error: campaign_id must be a plain campaign identifier (no "
                "path separators or traversal sequences)."
            )
        data, err = await _api(
            f"/campaign/{campaign_id.strip()}", f"get campaign {campaign_id}"
        )
        if err:
            return err
        if not isinstance(data, dict) or not data.get("id"):
            return f"Campaign '{campaign_id}' was not found."
        lines = [
            f"Campaign: {data.get('name', '?')} (id: {data.get('id')})",
            f"  started: {_dt(data.get('startDate'))}",
        ]
        for key, label in (
            ("actors", "actors"),
            ("malware", "malware families"),
            ("techniques", "techniques"),
            ("families", "campaign families"),
        ):
            rendered = _fmt_named(data.get(key))
            if rendered:
                lines.append(f"  {label}: {rendered}")
        if data.get("description"):
            lines.append(f"  description: {_trunc(data['description'], 1500)}")
        members = data.get("campaignMembers")
        if isinstance(members, list) and members:
            lines.append(f"  threat variants ({len(members)}):")
            for m in members[:_MAX_EVENTS_SHOWN]:
                if not isinstance(m, dict):
                    continue
                lines.append(
                    f"    • [{m.get('type', '?')}/{m.get('subType', '?')}] "
                    f"id={m.get('id', '?')} first seen {_dt(m.get('threatTime'))}"
                )
                if m.get("threat"):
                    lines.append(f"      artifact: {_trunc(m.get('threat'), 150)}")
            if len(members) > _MAX_EVENTS_SHOWN:
                lines.append(
                    f"    … {len(members) - _MAX_EVENTS_SHOWN} more variants not shown."
                )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # Forensics API
    # ══════════════════════════════════════════════════════════════════════

    async def get_proofpoint_forensics(
        threat_id: str = "",
        campaign_id: str = "",
        include_campaign_forensics: bool = False,
    ) -> str:
        """Fetch forensic evidence (IOCs) for a threat or a campaign."""
        threat_id = (threat_id or "").strip()
        campaign_id = (campaign_id or "").strip()
        if bool(threat_id) == bool(campaign_id):
            return (
                "Error: provide exactly ONE of threat_id or campaign_id (they "
                "are mutually exclusive)."
            )
        params: dict[str, Any] = {}
        if threat_id:
            params["threatId"] = threat_id
            if include_campaign_forensics:
                params["includeCampaignForensics"] = "true"
        else:
            params["campaignId"] = campaign_id
        data, err = await _api("/forensics", "get forensics", params=params)
        if err:
            return err
        reports = (data or {}).get("reports")
        if not isinstance(reports, list) or not reports:
            return (
                f"No forensic reports found for "
                f"{'threat ' + threat_id if threat_id else 'campaign ' + campaign_id}."
            )
        lines = [f"Found {len(reports)} forensic report(s):"]
        for report in reports:
            if not isinstance(report, dict):
                continue
            lines.append(
                f"\n• [{report.get('scope', '?')}/{report.get('type', '?')}] "
                f"{_trunc(report.get('name'), 150)} (id: {report.get('id', '?')})"
            )
            evidences = report.get("forensics")
            if not isinstance(evidences, list):
                continue
            malicious = [
                e for e in evidences if isinstance(e, dict) and e.get("malicious")
            ]
            benign_count = len(evidences) - len(malicious)
            lines.append(
                f"  {len(evidences)} evidence item(s), {len(malicious)} malicious:"
            )
            for e in malicious[:_MAX_EVENTS_SHOWN]:
                what = e.get("what") or {}
                detail = _trunc(
                    _json.dumps(what, default=str, sort_keys=True), 300
                )
                lines.append(
                    f"    • [{e.get('type', '?')}] "
                    f"{_trunc(e.get('display'), 150) or '(no display)'}"
                )
                if detail and detail != "{}":
                    lines.append(f"      {detail}")
            if len(malicious) > _MAX_EVENTS_SHOWN:
                lines.append(
                    f"    … {len(malicious) - _MAX_EVENTS_SHOWN} more malicious "
                    "evidence items not shown."
                )
            if benign_count:
                lines.append(
                    f"    ({benign_count} non-malicious contextual evidence "
                    "items omitted)"
                )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # Threat API
    # ══════════════════════════════════════════════════════════════════════

    async def get_proofpoint_threat(threat_id: str) -> str:
        """Get the TAP threat summary for a threat ID (severity, actors, families)."""
        if _is_unsafe_path_id(threat_id):
            return (
                "Error: threat_id must be a plain threat identifier (no path "
                "separators or traversal sequences)."
            )
        data, err = await _api(
            f"/threat/summary/{threat_id.strip()}", f"get threat {threat_id}"
        )
        if err:
            return err
        if not isinstance(data, dict) or not data.get("id"):
            return f"Threat '{threat_id}' was not found."
        lines = [
            f"Threat: {_trunc(data.get('name'), 150)} (id: {data.get('id')})",
            f"  type: {data.get('type', '?')}  category: {data.get('category', '?')}  "
            f"status: {data.get('status', '?')}",
            f"  severity: {data.get('severity', '?')}/1000  "
            f"attack spread: {data.get('attackSpread', '?')} customer(s)",
            f"  identified: {_dt(data.get('identifiedAt'))}",
        ]
        flags = [
            label
            for key, label in (
                ("notable", "notable"),
                ("verticals", "vertically targeted"),
                ("geographies", "geographically targeted"),
            )
            if data.get(key)
        ]
        if flags:
            lines.append(f"  flags: {', '.join(flags)}")
        for key, label in (
            ("actors", "actors"),
            ("families", "threat families"),
            ("malware", "malware"),
            ("techniques", "techniques"),
            ("brands", "abused brands"),
        ):
            rendered = _fmt_named(data.get(key))
            if rendered:
                lines.append(f"  {label}: {rendered}")
        lines.append(
            "\nUse get_proofpoint_forensics with this threat_id for IOC-level "
            "evidence."
        )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # People API
    # ══════════════════════════════════════════════════════════════════════

    def _people_params(
        window: Any, size: Any, page: Any, size_cap: int
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Validate and normalise People API params → ``(params, error)``."""
        window_val = _to_int(window, 90)
        if window_val is None or window_val not in _PEOPLE_WINDOWS:
            return None, (
                "Error: window must be one of "
                f"{', '.join(str(w) for w in _PEOPLE_WINDOWS)} (days)."
            )
        size_val = _to_int(size, 50)
        page_val = _to_int(page, 1)
        if size_val is None or page_val is None:
            return None, "Error: size and page must be integers."
        return {
            "window": window_val,
            "size": max(1, min(size_val, size_cap)),
            "page": max(1, page_val),
        }, None

    async def list_proofpoint_vap(
        window: int = 90,
        size: int = 50,
        page: int = 1,
    ) -> str:
        """Rank the Very Attacked People (VAPs) in the org for a period."""
        params, err_msg = _people_params(window, size, page, size_cap=1000)
        if err_msg:
            return err_msg
        data, err = await _api("/people/vap", "list VAPs", params=params)
        if err:
            return err
        users = (data or {}).get("users")
        if not isinstance(users, list):
            users = []
        out = format_entity_list(
            users,
            lambda u: _fmt_person(u, "threatStatistics"),
            noun="Very Attacked Person",
            total=(data or {}).get("totalVapUsers"),
            empty_msg=f"No Very Attacked People found in the last {window} days.",
        )
        extras = []
        if (data or {}).get("averageAttackIndex") is not None:
            extras.append(f"average attack index: {data['averageAttackIndex']}")
        if (data or {}).get("vapAttackIndexThreshold") is not None:
            extras.append(f"VAP threshold: {data['vapAttackIndexThreshold']}")
        if extras:
            out += f"\n\n({'; '.join(extras)})"
        return out

    async def list_proofpoint_top_clickers(
        window: int = 90,
        size: int = 50,
        page: int = 1,
    ) -> str:
        """Rank users who click malicious URLs the most (training candidates)."""
        params, err_msg = _people_params(window, size, page, size_cap=200)
        if err_msg:
            return err_msg
        data, err = await _api(
            "/people/top-clickers", "list top clickers", params=params
        )
        if err:
            return err
        users = (data or {}).get("users")
        if not isinstance(users, list):
            users = []
        return format_entity_list(
            users,
            lambda u: _fmt_person(u, "clickStatistics"),
            noun="top clicker",
            total=(data or {}).get("totalTopClickers"),
            empty_msg=f"No top clickers found in the last {window} days.",
        )

    # ══════════════════════════════════════════════════════════════════════
    # URL Decoder API
    # ══════════════════════════════════════════════════════════════════════

    async def decode_proofpoint_urls(urls: list[str] | str) -> str:
        """Decode Proofpoint URL Defense rewritten URLs to their original targets."""
        if isinstance(urls, str):
            urls = [urls]
        cleaned = [str(u).strip() for u in (urls or []) if str(u).strip()]
        if not cleaned:
            return "Error: provide at least one rewritten URL to decode."
        if len(cleaned) > 100:
            return (
                f"Error: too many URLs ({len(cleaned)}) — decode at most 100 "
                "URLs per call."
            )
        data, err = await _api(
            "/url/decode", "decode URLs", json={"urls": cleaned}
        )
        if err:
            return err
        results = (data or {}).get("urls")
        if not isinstance(results, list) or not results:
            return "Proofpoint TAP returned no decode results."
        lines = [f"Decoded {len(results)} URL(s):"]
        for i, r in enumerate(results, start=1):
            if not isinstance(r, dict):
                continue
            if r.get("success"):
                lines.append(f"  {i}. {_trunc(r.get('decodedUrl'), 500)}")
                if r.get("recipientEmail"):
                    lines.append(
                        f"     (original recipient: {r['recipientEmail']})"
                    )
            else:
                lines.append(
                    f"  {i}. DECODE FAILED: "
                    f"{_trunc(r.get('error'), 200) or 'unknown error'}"
                )
        return "\n".join(lines)

    # ── Registration ──────────────────────────────────────────────────────
    executor.register("list_proofpoint_siem_events", list_proofpoint_siem_events)
    executor.register("list_proofpoint_campaigns", list_proofpoint_campaigns)
    executor.register("get_proofpoint_campaign", get_proofpoint_campaign)
    executor.register("get_proofpoint_forensics", get_proofpoint_forensics)
    executor.register("get_proofpoint_threat", get_proofpoint_threat)
    executor.register("list_proofpoint_vap", list_proofpoint_vap)
    executor.register("list_proofpoint_top_clickers", list_proofpoint_top_clickers)
    executor.register("decode_proofpoint_urls", decode_proofpoint_urls)

    return _build_definitions("integration:proofpoint")


# ---------------------------------------------------------------------------
# Tool definitions (static metadata; no credentials required)
# ---------------------------------------------------------------------------


def _build_definitions(src: str) -> list[ToolDefinition]:
    """Build the static Proofpoint TAP ToolDefinition list (all read-only)."""

    def _td(
        name: str,
        description: str,
        properties: dict[str, Any],
        required: list[str],
    ) -> ToolDefinition:
        return ToolDefinition(
            name=name,
            description=description,
            input_schema={
                "type": "object",
                "properties": properties,
                "required": required,
            },
            source=src,
            priority=0,
        )

    return [
        _td(
            "list_proofpoint_siem_events",
            "Fetch Proofpoint TAP email threat events (SIEM API): messages "
            "blocked/delivered containing known threats and clicks to "
            "malicious URLs blocked/permitted, with per-threat details "
            "(threat IDs, classification, campaign IDs, scores). IMPORTANT "
            "time constraints: at most ONE HOUR of data per request, and at "
            "most 7 days into the past. Returned threat IDs feed "
            "get_proofpoint_threat and get_proofpoint_forensics; campaign IDs "
            "feed get_proofpoint_campaign.",
            {
                "event_type": {
                    "type": "string",
                    "enum": sorted(_SIEM_ENDPOINTS),
                    "default": "all",
                    "description": (
                        "Which events to fetch: 'all' (everything), 'issues' "
                        "(permitted clicks + delivered messages — the ones "
                        "needing attention), or a single category."
                    ),
                },
                "since_seconds": {
                    "type": "integer",
                    "default": 3600,
                    "description": (
                        "Look-back window in seconds ending now (min 30, max "
                        "3600 = 1 hour). Ignored when 'interval' is set."
                    ),
                },
                "interval": {
                    "type": "string",
                    "description": (
                        "ISO8601 interval for historical windows (max 1 hour "
                        "span, within the last 7 days), e.g. "
                        "'2026-07-10T09:00:00Z/2026-07-10T10:00:00Z' or "
                        "'PT30M/2026-07-10T10:00:00Z'. Overrides "
                        "since_seconds."
                    ),
                },
                "threat_type": {
                    "type": "string",
                    "enum": list(_THREAT_TYPES),
                    "description": "Optional filter by threat type.",
                },
                "threat_status": {
                    "type": "string",
                    "enum": list(_THREAT_STATUSES),
                    "description": (
                        "Optional filter by threat status (default: active "
                        "and cleared)."
                    ),
                },
            },
            [],
        ),
        _td(
            "list_proofpoint_campaigns",
            "List IDs of Proofpoint TAP campaigns active in a time window, "
            "sorted by last update. The interval may span at most ONE DAY. "
            "QUOTA WARNING: this endpoint allows only 50 requests per rolling "
            "24 hours — prefer campaign IDs already present in SIEM events "
            "when possible. Campaign detail lookups "
            "(get_proofpoint_campaign) are unthrottled.",
            {
                "interval": {
                    "type": "string",
                    "description": (
                        "Required ISO8601 interval of at most 1 day, e.g. "
                        "'2026-07-09T00:00:00Z/2026-07-10T00:00:00Z'."
                    ),
                },
                "size": {
                    "type": "integer",
                    "default": 100,
                    "description": "Max campaign IDs per page (max 200).",
                },
                "page": {"type": "integer", "default": 1},
            },
            ["interval"],
        ),
        _td(
            "get_proofpoint_campaign",
            "Get details of a Proofpoint TAP campaign: analyst description, "
            "associated actors, malware families, techniques, and the threat "
            "variants correlated to the campaign. Campaign IDs come from "
            "SIEM events (threatsInfoMap.campaignId) or "
            "list_proofpoint_campaigns. Unthrottled.",
            {
                "campaign_id": {
                    "type": "string",
                    "description": "The campaign identifier (UUID).",
                },
            },
            ["campaign_id"],
        ),
        _td(
            "get_proofpoint_forensics",
            "Fetch forensic evidence (indicators of compromise) for a "
            "Proofpoint TAP threat or campaign: sandbox observations such as "
            "dropped files, network connections, DNS lookups, mutexes, "
            "registry changes, processes, and IDS hits. Provide exactly one "
            "of threat_id or campaign_id. QUOTA: threat-only lookups are "
            "limited to 50 per 24h per threat; campaign-scope lookups share "
            "a 1800/24h pool.",
            {
                "threat_id": {
                    "type": "string",
                    "description": (
                        "Threat identifier (from SIEM events or "
                        "get_proofpoint_threat). Mutually exclusive with "
                        "campaign_id."
                    ),
                },
                "campaign_id": {
                    "type": "string",
                    "description": (
                        "Campaign identifier. Mutually exclusive with "
                        "threat_id."
                    ),
                },
                "include_campaign_forensics": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "With threat_id only: if true and the threat belongs "
                        "to a campaign, return the whole campaign's "
                        "forensics."
                    ),
                },
            },
            [],
        ),
        _td(
            "get_proofpoint_threat",
            "Get the Proofpoint TAP threat summary for a threat ID: severity "
            "score (0-1000), type, category, status, attack spread across "
            "Proofpoint customers, and associated actors, threat families, "
            "malware, techniques, and abused brands. Threat IDs come from "
            "SIEM events (threatsInfoMap.threatId / click threatID). "
            "Unthrottled — the cheapest enrichment pivot.",
            {
                "threat_id": {
                    "type": "string",
                    "description": "The TAP threat identifier.",
                },
            },
            ["threat_id"],
        ),
        _td(
            "list_proofpoint_vap",
            "Rank the Very Attacked People (VAPs) in the organization for a "
            "period: identities (name, email, department, title, VIP flag) "
            "with their attack index and threat-family breakdown. QUOTA: 50 "
            "requests per rolling 24 hours.",
            {
                "window": {
                    "type": "integer",
                    "enum": list(_PEOPLE_WINDOWS),
                    "default": 90,
                    "description": "Look-back window in days (14, 30, or 90).",
                },
                "size": {
                    "type": "integer",
                    "default": 50,
                    "description": "Max users per page (up to 1000).",
                },
                "page": {"type": "integer", "default": 1},
            },
            [],
        ),
        _td(
            "list_proofpoint_top_clickers",
            "Rank the users who clicked malicious URLs the most (blocked or "
            "not) for a period — ideal for targeting security-awareness "
            "training. QUOTA: 50 requests per rolling 24 hours.",
            {
                "window": {
                    "type": "integer",
                    "enum": list(_PEOPLE_WINDOWS),
                    "default": 90,
                    "description": "Look-back window in days (14, 30, or 90).",
                },
                "size": {
                    "type": "integer",
                    "default": 50,
                    "description": "Max users per page (max 200).",
                },
                "page": {"type": "integer", "default": 1},
            },
            [],
        ),
        _td(
            "decode_proofpoint_urls",
            "Decode Proofpoint URL Defense rewritten URLs "
            "(urldefense.proofpoint.com / urldefense.com links commonly found "
            "in emails protected by TAP) back to their original target URLs. "
            "Use this whenever an email body contains rewritten links and the "
            "real destination matters. Accepts up to 100 URLs per call.",
            {
                "urls": {
                    # The handler also accepts a bare string (wrapped in a
                    # list) - keep the schema in sync with that contract.
                    "oneOf": [
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "string"},
                    ],
                    "description": (
                        "The rewritten URL(s) to decode (max 100); a single "
                        "URL string is also accepted."
                    ),
                },
            },
            ["urls"],
        ),
    ]
