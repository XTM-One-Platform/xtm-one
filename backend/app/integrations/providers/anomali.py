# SPDX-License-Identifier: Apache-2.0

"""Anomali ThreatStream integration - Agentic threat intelligence management.

Full-fledged Anomali ThreatStream (threat intelligence platform) integration
covering the CTI loop for an agentic security team:

  - Intelligence / observables (*enrichment + hunting*): search with rich
    filters or the advanced query language, get full IOC detail, passive DNS
    pivoting, update / delete observables
  - Importing intelligence (*creating intelligence*): import observables into
    the review queue (with approval) or directly (without approval), review
    and approve import sessions, bulk tag / untag observables
  - Threat model entities (*context*): actors, campaigns, incidents,
    signatures, TTPs, vulnerabilities, threat bulletins (tipreport), and
    attack patterns - list, get, cross-entity search, create, update, tag,
    associate, comment, delete, and list associated observables
  - Investigations (*case management*): list, create, update, add elements
    (observables + threat model entities), delete
  - Rules (*detection*): keyword rules that match incoming intelligence -
    list, create, update, delete
  - Org whitelist (*noise control*): list, add, annotate, remove entries
  - Sandbox (*detonation*): list platforms, submit URLs, poll status, fetch
    reports
  - Users: list org users to resolve assignee IDs

Authentication
--------------
Every request carries the ``Authorization: apikey <username>:<api_key>``
header (no OAuth). The API key is generated in the ThreatStream UI
(Settings -> My Profile). The API user's ThreatStream role bounds what the
agent can do - importing without approval and approving import sessions
require the "Approve Intel" permission.

``integration_tools._get_token`` assembles the compound
``base_url||username||api_key`` token this builder parses. The base URL
defaults to the ThreatStream SaaS host (``https://api.threatstream.com``);
customer on-prem / AirGap instances override it. The API key (the credential)
lives in ``api_key_enc``; the username and base URL live in ``extra_config``.
All calls target ``{base_url}/api/v1/...`` and ``{base_url}/api/v2/...``.

Pagination is offset-based (``limit`` + ``offset``); list responses carry an
``objects`` array and a ``meta`` envelope (``next`` / ``total_count``).
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

import httpx

from app.core.egress import egress_client
from app.integrations.http import (
    format_entity_list,
    format_pagination_hint,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "Anomali ThreatStream"
_SOURCE = "integration:anomali"

# Default ThreatStream SaaS API host. Customer on-prem / AirGap instances
# store their own host as base_url; this default keeps the common SaaS case
# configuration-free. NOTE: this is the API host, NOT the web UI host
# (https://ui.threatstream.com), so the provider is intentionally excluded
# from _USER_FACING_BASE_URL_PROVIDERS.
_DEFAULT_BASE_URL = "https://api.threatstream.com"

# Upper bound on a single page so an unbounded caller limit can't pull an
# enormous JSON document (ThreatStream allows up to 1000 per page).
_MAX_LIMIT = 1000

# Threat model entity types addressable via /api/v1/{model}/. ``tipreport``
# is a Threat Bulletin in the UI; ``attackpattern`` maps to Attack Patterns.
_THREAT_MODELS: frozenset[str] = frozenset(
    {
        "actor",
        "campaign",
        "incident",
        "signature",
        "ttp",
        "vulnerability",
        "tipreport",
        "attackpattern",
    }
)

# Investigation element types accepted by /api/v1/investigationelement/.
# Observables use the internal ``intelligence2`` r_type.
_ELEMENT_TYPES: dict[str, str] = {
    "observable": "intelligence2",
    "actor": "actor",
    "campaign": "campaign",
    "incident": "incident",
    "signature": "signature",
    "tipreport": "tipreport",
    "ttp": "ttp",
    "vulnerability": "vulnerability",
}

_SEVERITIES: frozenset[str] = frozenset({"low", "medium", "high", "very-high"})
_CLASSIFICATIONS: frozenset[str] = frozenset({"private", "public"})

# Value types accepted by the org whitelist add endpoint
# (/api/v1/orgwhitelist/bulk/).
_WHITELIST_VALUE_TYPES: frozenset[str] = frozenset(
    {"cidr", "domain", "email", "ip", "md5", "url", "user-agent"}
)


def _clamp(value: Any, default: int, lo: int = 1, hi: int = _MAX_LIMIT) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


def _int_or(value: Any, default: int) -> int:
    """Coerce an LLM-provided value to int, tolerating explicit JSON null.

    Optional integer params arrive as ``None`` when the LLM sends an explicit
    ``null``; comparing ``None >= 0`` would raise ``TypeError``.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _seg(value: Any) -> str:
    """Percent-encode an LLM-provided value used as a URL path segment.

    IDs and pivot values (IPs, domains) are interpolated into endpoint paths;
    a value containing ``/``, ``?``, or ``#`` would otherwise rewrite the
    request path.
    """
    return quote(str(value or "").strip(), safe="")


def _model_error(model: str) -> str | None:
    if model not in _THREAT_MODELS:
        return (
            f"Error: model must be one of {', '.join(sorted(_THREAT_MODELS))} "
            f"(got '{model}')."
        )
    return None


# ---------------------------------------------------------------------------
# Auth + URL helpers (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def anomali_api_base(base_url: str) -> str:
    """Return the ThreatStream API host base URL (no trailing slash).

    The value is stripped first: a base URL pasted with a trailing
    newline/space would otherwise yield an invalid host. A trailing ``/api``,
    ``/api/v1``, or ``/api/v2`` path segment (ThreatStream docs often present
    the base that way) is trimmed so a pasted URL can never produce a doubled
    ``/api/api/v2/...`` path. Falls back to the ThreatStream SaaS host when
    empty. Endpoints are appended as ``/api/v1/...`` / ``/api/v2/...``.
    """
    cleaned = (base_url or "").strip().rstrip("/")
    lowered = cleaned.lower()
    for suffix in ("/api/v1", "/api/v2", "/api"):
        if lowered.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].rstrip("/")
            break
    return cleaned or _DEFAULT_BASE_URL


def build_anomali_headers(username: str, api_key: str) -> dict[str, str]:
    """Build ThreatStream request headers (``Authorization: apikey user:key``).

    The username and API key are stripped: a credential pasted with a trailing
    newline/space would otherwise produce an Authorization header that always
    fails (or that httpx rejects as an illegal header value).
    """
    return {
        "Authorization": f"apikey {username.strip()}:{api_key.strip()}",
        "Accept": "application/json",
    }


# ---------------------------------------------------------------------------
# Response parsing helpers
# ---------------------------------------------------------------------------


def _json_body(resp: httpx.Response) -> Any:
    """Parse a JSON body, returning ``None`` on empty/non-JSON responses.

    Several write endpoints (import without approval, approve_all, bulk
    tagging) return 202/204 with an empty or plain-text body.
    """
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError):
        return None


def _objects(data: Any) -> list[dict]:
    if isinstance(data, dict):
        objs = data.get("objects")
        if isinstance(objs, list):
            return [o for o in objs if isinstance(o, dict)]
    return []


def _page_hint(data: Any, offset: int, count: int) -> str:
    """Offset pagination hint driven by the ThreatStream ``meta.next`` field."""
    if isinstance(data, dict) and (data.get("meta") or {}).get("next"):
        return format_pagination_hint(str(offset + count), param_name="offset")
    return ""


def _ts(value: Any) -> str:
    """Compact an ISO timestamp to minute precision for list output."""
    text = str(value or "")
    return text[:16].replace("T", " ") if text else ""


def _tag_names(obj: dict) -> str:
    tags = obj.get("tags")
    if not isinstance(tags, list):
        return ""
    names = [
        t.get("name", "") if isinstance(t, dict) else str(t) for t in tags if t
    ]
    return ", ".join(n for n in names if n)


def _tag_payload(tags: str, tlp: str) -> list[dict[str, str]]:
    """Build the ``[{"name": ..., "tlp": ...}]`` tag list from a CSV string."""
    tlp_val = (tlp or "").strip().lower()
    out: list[dict[str, str]] = []
    for name in (t.strip() for t in (tags or "").split(",")):
        if not name:
            continue
        entry: dict[str, str] = {"name": name}
        if tlp_val:
            entry["tlp"] = tlp_val
        out.append(entry)
    return out


def _format_observable(obj: dict) -> str:
    """One observable as a compact multi-line summary."""
    value = obj.get("value", "?")
    itype = obj.get("itype", "?")
    confidence = obj.get("confidence", "?")
    status = obj.get("status", "?")
    severity = (obj.get("meta") or {}).get("severity") or obj.get("severity") or ""
    lines = [f"#{obj.get('id', '?')} {value} ({itype})"]
    ctx = f"  Confidence: {confidence} | Status: {status}"
    if severity:
        ctx += f" | Severity: {severity}"
    source = obj.get("source")
    if source:
        ctx += f" | Source: {source}"
    lines.append(ctx)
    tags = _tag_names(obj)
    if tags:
        lines.append(f"  Tags: {tags}")
    modified = _ts(obj.get("modified_ts"))
    if modified:
        lines.append(f"  Modified: {modified}")
    return "\n".join(lines)


def _format_threat_model(obj: dict) -> str:
    """One threat model entity as a compact summary line pair."""
    model = obj.get("model_type", "")
    header = f"#{obj.get('id', '?')} {obj.get('name', 'Unnamed')}"
    if model:
        header += f" [{model}]"
    parts = []
    visibility = "public" if obj.get("is_public") else "private"
    parts.append(f"Visibility: {visibility}")
    if obj.get("publication_status"):
        parts.append(f"Publication: {obj['publication_status']}")
    if obj.get("tlp"):
        parts.append(f"TLP: {obj['tlp']}")
    created = _ts(obj.get("created_ts"))
    if created:
        parts.append(f"Created: {created}")
    lines = [header, "  " + " | ".join(parts)]
    tags = _tag_names(obj)
    if tags:
        lines.append(f"  Tags: {tags}")
    return "\n".join(lines)


def _format_import_session(obj: dict) -> str:
    sid = obj.get("id", "?")
    status = obj.get("status", "?")
    date = _ts(obj.get("date") or obj.get("created_ts"))
    num = obj.get("numIndicators", obj.get("num_indicators", "?"))
    rejected = obj.get("numRejected", obj.get("num_rejected", ""))
    line = f"Import session #{sid}: status={status} | indicators={num}"
    if rejected != "":
        line += f" | rejected={rejected}"
    if date:
        line += f" | date={date}"
    approved_by = obj.get("approved_by")
    if isinstance(approved_by, dict) and approved_by.get("email"):
        line += f" | approved by {approved_by['email']}"
    return line


def _format_investigation(obj: dict) -> str:
    lines = [
        f"#{obj.get('id', '?')} {obj.get('name', 'Unnamed')}",
        (
            f"  Status: {obj.get('status', '?')} | "
            f"Priority: {obj.get('priority', '?')} | "
            f"Elements: {obj.get('elements_count', obj.get('num_elements', '?'))}"
        ),
    ]
    assignee = obj.get("assignee")
    if isinstance(assignee, dict) and (assignee.get("name") or assignee.get("email")):
        lines.append(f"  Assignee: {assignee.get('name') or assignee.get('email')}")
    created = _ts(obj.get("created_ts"))
    if created:
        lines.append(f"  Created: {created}")
    return "\n".join(lines)


def _format_rule(obj: dict) -> str:
    keywords = obj.get("keywords")
    kw = ", ".join(str(k) for k in keywords) if isinstance(keywords, list) else ""
    enabled = "enabled" if obj.get("is_enabled") else "disabled"
    line = f"Rule #{obj.get('id', '?')} '{obj.get('name', 'Unnamed')}' ({enabled})"
    if kw:
        line += f"\n  Keywords: {kw}"
    matches = obj.get("matches")
    if matches is not None:
        line += f"\n  Matches: {matches}"
    return line


def _format_whitelist_entry(obj: dict) -> str:
    line = (
        f"Entry #{obj.get('id', '?')}: {obj.get('value', '?')} "
        f"({obj.get('value_type', '?')})"
    )
    if obj.get("notes"):
        line += f" - {obj['notes']}"
    created = _ts(obj.get("created_ts"))
    if created:
        line += f" | created {created}"
    return line


def _format_user(obj: dict) -> str:
    active = "active" if obj.get("is_active") else "inactive"
    line = (
        f"User #{obj.get('id', '?')}: {obj.get('name') or obj.get('email', '?')} "
        f"({obj.get('email', '?')}) - {active}"
    )
    last_login = _ts(obj.get("last_login"))
    if last_login:
        line += f" | last login {last_login}"
    return line


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build ThreatStream tools from a ``base_url||username||api_key`` token."""
    base_url, username, api_key = parse_compound_token(token, 3, greedy_last=True)
    # ``base_url`` and ``username`` are populated by the 2-part introspection
    # dummy (``https://dummy.example.com||__dummy__``); the API key is supplied
    # only at runtime, so guarding on these two keeps catalog introspection
    # working while still refusing a half-configured integration at runtime.
    if not base_url or not username:
        return []

    api = anomali_api_base(base_url)
    v1 = f"{api}/api/v1"
    v2 = f"{api}/api/v2"
    headers = build_anomali_headers(username, api_key)

    def _client(timeout: int = 30) -> httpx.AsyncClient:
        return egress_client(timeout=timeout)

    # ══════════════════════════════════════════════════════════════════════
    # 0. CONNECTION
    # ══════════════════════════════════════════════════════════════════════

    async def get_anomali_status() -> str:
        """Validate the ThreatStream connection and report the account."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{v2}/intelligence/",
                _PROVIDER,
                "validate connection",
                headers=headers,
                params={"limit": 1},
            )
            if err:
                return err
            data = _json_body(resp) or {}
            total = (data.get("meta") or {}).get("total_count", "?")
            return (
                f"Connected to Anomali ThreatStream at {api} "
                f"(authenticated as '{username}').\n"
                f"  Intelligence records visible to this account: {total}."
            )

    # ══════════════════════════════════════════════════════════════════════
    # 1. INTELLIGENCE (OBSERVABLES) - enrichment + hunting
    # ══════════════════════════════════════════════════════════════════════

    async def search_anomali_intelligence(
        value: str = "",
        itype: str = "",
        ioc_type: str = "",
        status: str = "",
        confidence_min: int = -1,
        confidence_max: int = -1,
        tags: str = "",
        feed_id: int = 0,
        modified_since: str = "",
        query: str = "",
        order_by: str = "-modified_ts",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Search ThreatStream intelligence (observables) with structured filters.

        Prefer the structured params; use query only for advanced searches the
        params don't cover (it is sent as the raw ``q=`` search string, e.g.
        '(itype="mal_ip" OR itype="c2_ip") AND confidence>80').
        """
        params: dict[str, Any] = {
            "limit": _clamp(limit, 25),
            "offset": max(0, _int_or(offset, 0)),
            "order_by": order_by or "-modified_ts",
        }
        if value:
            params["value"] = value
        if itype:
            params["itype"] = itype
        if ioc_type:
            params["type"] = ioc_type
        if status:
            params["status"] = status
        # Clamp to the documented 0-100 confidence range (consistent with the
        # other confidence params in this provider); out-of-range values would
        # otherwise draw an avoidable 4xx from ThreatStream.
        if _int_or(confidence_min, -1) >= 0:
            params["confidence__gte"] = min(_int_or(confidence_min, -1), 100)
        if _int_or(confidence_max, -1) >= 0:
            params["confidence__lte"] = min(_int_or(confidence_max, -1), 100)
        if tags:
            params["tags.name"] = tags
        if _int_or(feed_id, 0):
            params["feed_id"] = _int_or(feed_id, 0)
        if modified_since:
            params["modified_ts__gte"] = modified_since
        if query:
            params["q"] = query
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{v2}/intelligence/",
                _PROVIDER,
                "search intelligence",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = _json_body(resp) or {}
            objects = _objects(data)
            total = (data.get("meta") or {}).get("total_count")
            result = format_entity_list(
                objects,
                _format_observable,
                noun="observable",
                total=total if isinstance(total, int) else None,
                empty_msg="No observables matched the search.",
            )
            return result + _page_hint(data, params["offset"], len(objects))

    async def get_anomali_observable(observable_id: str) -> str:
        """Get full detail for one ThreatStream observable by its numeric ID."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{v2}/intelligence/{_seg(observable_id)}/",
                _PROVIDER,
                f"get observable {observable_id}",
                headers=headers,
            )
            if err:
                return err
            obj = _json_body(resp) or {}
            lines = [_format_observable(obj)]
            for field in (
                "threat_type",
                "classification",
                "expiration_ts",
                "org",
                "asn",
                "country",
                "rdns",
                "threatscore",
                "uuid",
                "created_ts",
            ):
                val = obj.get(field)
                if val not in (None, ""):
                    lines.append(f"  {field}: {val}")
            desc = obj.get("description")
            if desc:
                lines.append(f"  Description: {str(desc)[:500]}")
            return "\n".join(lines)

    async def get_anomali_passive_dns(value_type: str, value: str) -> str:
        """Get passive DNS resolutions for an IP or domain (infrastructure pivot)."""
        vt = (value_type or "").strip().lower()
        if vt not in ("ip", "domain"):
            return "Error: value_type must be one of ip, domain."
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{v1}/pdns/{vt}/{_seg(value)}/",
                _PROVIDER,
                f"passive DNS for {vt} {value}",
                headers=headers,
            )
            if err:
                return err
            data = _json_body(resp) or {}
            results = data.get("results")
            if not isinstance(results, list) or not results:
                return f"No passive DNS records found for {vt} {value}."
            lines = [f"Passive DNS for {value} ({len(results)} records):"]
            for rec in results[:50]:
                if not isinstance(rec, dict):
                    continue
                lines.append(
                    f"  {rec.get('domain', '?')} -> {rec.get('ip', '?')} "
                    f"(first {_ts(rec.get('first_seen'))}, "
                    f"last {_ts(rec.get('last_seen'))}, "
                    f"source {rec.get('source', '?')})"
                )
            if len(results) > 50:
                lines.append(f"  ... and {len(results) - 50} more records")
            return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 2. IMPORTING INTELLIGENCE - creating intelligence
    # ══════════════════════════════════════════════════════════════════════

    async def import_anomali_observables(
        datatext: str,
        classification: str = "private",
        confidence: int = 50,
        severity: str = "low",
        threat_type: str = "exploit",
        tags: str = "",
        tags_tlp: str = "",
        expiration_ts: str = "",
        default_state: str = "inactive",
    ) -> str:
        """Import observables into the ThreatStream review queue (with approval).

        The imported items land in an import session that must be reviewed and
        approved (see approve_anomali_import_session) before becoming active.
        """
        cls = (classification or "private").strip().lower()
        if cls not in _CLASSIFICATIONS:
            return "Error: classification must be one of private, public."
        sev = (severity or "low").strip().lower()
        if sev not in _SEVERITIES:
            return f"Error: severity must be one of {', '.join(sorted(_SEVERITIES))}."
        state = (default_state or "inactive").strip().lower()
        if state not in ("active", "inactive"):
            return "Error: default_state must be one of active, inactive."
        data: dict[str, Any] = {
            "datatext": datatext,
            "classification": cls,
            "confidence": _clamp(confidence, 50, lo=0, hi=100),
            "severity": sev,
            "threat_type": threat_type or "exploit",
            "default_state": state,
        }
        if expiration_ts:
            data["expiration_ts"] = expiration_ts
        tag_list = _tag_payload(tags, tags_tlp)
        if tag_list:
            data["tags"] = json.dumps(tag_list)
        async with _client(60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{v2}/intelligence/import/",
                _PROVIDER,
                "import observables (with approval)",
                headers=headers,
                data=data,
            )
            if err:
                return err
            body = _json_body(resp) or {}
            if not body.get("success", False):
                return (
                    "Error: ThreatStream did not accept the import. "
                    f"Response: {str(body)[:300]}"
                )
            return (
                "Observables imported into the review queue.\n"
                f"  Import session ID: {body.get('import_session_id', '?')}\n"
                f"  Job ID: {body.get('job_id', '?')}\n"
                "Review and approve via approve_anomali_import_session."
            )

    async def create_anomali_observables(
        objects_json: str,
        classification: str = "private",
        confidence: int = -1,
        severity: str = "",
        tags: str = "",
        tags_tlp: str = "",
        allow_unresolved: bool = False,
    ) -> str:
        """Create observables directly (import WITHOUT approval).

        Requires the "Approve Intel" permission. objects_json is a JSON array
        of observable objects, each with an itype plus the value field, e.g.
        '[{"itype": "mal_ip", "srcip": "1.2.3.4", "severity": "high"}]'
        (value fields: srcip, domain, url, email, md5).
        """
        cls = (classification or "private").strip().lower()
        if cls not in _CLASSIFICATIONS:
            return "Error: classification must be one of private, public."
        try:
            objects = json.loads(objects_json)
        except json.JSONDecodeError as exc:
            return f"Error: objects_json is not valid JSON: {exc}"
        if not isinstance(objects, list) or not objects:
            return "Error: objects_json must be a non-empty JSON array."
        meta: dict[str, Any] = {
            "classification": cls,
            "allow_unresolved": bool(allow_unresolved),
        }
        if _int_or(confidence, -1) >= 0:
            meta["confidence"] = _clamp(confidence, 50, lo=0, hi=100)
        if severity:
            sev = severity.strip().lower()
            if sev not in _SEVERITIES:
                return (
                    f"Error: severity must be one of {', '.join(sorted(_SEVERITIES))}."
                )
            meta["severity"] = sev
        tag_list = _tag_payload(tags, tags_tlp)
        if tag_list:
            meta["tags"] = tag_list
        async with _client(60) as client:
            # 202 Accepted with an empty body on success.
            _resp, err = await safe_request(
                client,
                "patch",
                f"{v2}/intelligence/",
                _PROVIDER,
                "create observables (without approval)",
                headers=headers,
                json={"meta": meta, "objects": objects},
            )
            if err:
                return err
            return (
                f"{len(objects)} observable(s) submitted for direct creation "
                "(no approval required). ThreatStream processes the batch "
                "asynchronously - search for the values shortly to confirm."
            )

    async def update_anomali_observable(
        observable_id: str,
        confidence: int = -1,
        status: str = "",
        severity: str = "",
        expiration_ts: str = "",
    ) -> str:
        """Update fields on an existing observable. Only provided fields change.

        status examples: active, inactive, falsepos.
        """
        payload: dict[str, Any] = {}
        if _int_or(confidence, -1) >= 0:
            payload["confidence"] = _clamp(confidence, 50, lo=0, hi=100)
        if status:
            payload["status"] = status.strip().lower()
        if severity:
            sev = severity.strip().lower()
            if sev not in _SEVERITIES:
                return (
                    f"Error: severity must be one of {', '.join(sorted(_SEVERITIES))}."
                )
            # Top-level key on the single-observable PATCH (the Splunk SOAR
            # reference connector's shape); nesting it under ``meta`` would
            # clobber the observable's other meta fields.
            payload["severity"] = sev
        if expiration_ts:
            payload["expiration_ts"] = expiration_ts
        if not payload:
            return "No fields to update."
        async with _client() as client:
            _resp, err = await safe_request(
                client,
                "patch",
                f"{v2}/intelligence/{_seg(observable_id)}/",
                _PROVIDER,
                f"update observable {observable_id}",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            return f"Observable {observable_id} updated successfully."

    async def delete_anomali_observable(observable_id: str) -> str:
        """Delete a ThreatStream observable permanently. Cannot be undone."""
        async with _client() as client:
            _resp, err = await safe_request(
                client,
                "delete",
                f"{v2}/intelligence/{_seg(observable_id)}/",
                _PROVIDER,
                f"delete observable {observable_id}",
                headers=headers,
            )
            if err:
                return err
            return f"Observable {observable_id} deleted successfully."

    async def manage_anomali_observable_tags(
        action: str,
        observable_ids: str,
        tags: str,
        tlp: str = "red",
    ) -> str:
        """Bulk add or remove tags on observables (action: add or remove)."""
        act = (action or "").strip().lower()
        if act not in ("add", "remove"):
            return "Error: action must be one of add, remove."
        try:
            ids: list[int] = [
                int(i.strip()) for i in (observable_ids or "").split(",") if i.strip()
            ]
        except ValueError:
            return "Error: observable_ids must be a CSV of numeric IDs."
        tag_names = [t.strip() for t in (tags or "").split(",") if t.strip()]
        if not ids or not tag_names:
            return "Error: observable_ids and tags are both required (CSV)."
        if act == "add":
            tlp_val = ((tlp or "").strip() or "red").lower()
            body = {
                "ids": ids,
                "tags": [{"name": t, "tlp": tlp_val} for t in tag_names],
            }
            method, url = "post", f"{v2}/intelligence/bulk_tagging/"
        else:
            body = {"ids": ids, "tags": [{"name": t} for t in tag_names]}
            method, url = "patch", f"{v2}/intelligence/bulk_remove_tags/"
        async with _client() as client:
            _resp, err = await safe_request(
                client,
                method,
                url,
                _PROVIDER,
                f"{act} tags on {len(ids)} observables",
                headers=headers,
                json=body,
            )
            if err:
                return err
            verb = "added to" if act == "add" else "removed from"
            return (
                f"Tags {', '.join(tag_names)} {verb} "
                f"{len(ids)} observable(s) successfully."
            )

    # ══════════════════════════════════════════════════════════════════════
    # 3. IMPORT SESSIONS - review + approval
    # ══════════════════════════════════════════════════════════════════════

    async def list_anomali_import_sessions(
        import_id: str = "",
        status: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """List import sessions (jobs), or get one by ID (the review queue)."""
        async with _client() as client:
            if import_id:
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{v1}/importsession/{_seg(import_id)}/",
                    _PROVIDER,
                    f"get import session {import_id}",
                    headers=headers,
                )
                if err:
                    return err
                return _format_import_session(_json_body(resp) or {})
            # No order_by: the importsession resource's supported ordering
            # fields are undocumented, and the reference connectors list
            # sessions unordered - an unsupported field would 400 every call.
            params: dict[str, Any] = {
                "limit": _clamp(limit, 25),
                "offset": max(0, _int_or(offset, 0)),
            }
            if status:
                params["status"] = status.strip().lower()
            resp, err = await safe_request(
                client,
                "get",
                f"{v1}/importsession/",
                _PROVIDER,
                "list import sessions",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = _json_body(resp) or {}
            objects = _objects(data)
            result = format_entity_list(
                objects, _format_import_session, noun="import session"
            )
            return result + _page_hint(data, params["offset"], len(objects))

    async def approve_anomali_import_session(import_id: str) -> str:
        """Approve ALL observables in an import session, activating them.

        Requires the "Approve Intel" permission. Use list_anomali_import_sessions
        first to review the session contents.
        """
        async with _client() as client:
            _resp, err = await safe_request(
                client,
                "patch",
                f"{v1}/importsession/{_seg(import_id)}/approve_all/",
                _PROVIDER,
                f"approve import session {import_id}",
                headers=headers,
            )
            if err:
                return err
            return (
                f"Import session {import_id} approved - all observables in the "
                "session are being activated."
            )

    # ══════════════════════════════════════════════════════════════════════
    # 4. THREAT MODEL ENTITIES
    # ══════════════════════════════════════════════════════════════════════

    async def list_anomali_threat_models(
        model: str,
        name: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """List threat model entities of one type, optionally filtered by name."""
        model = (model or "").strip().lower()
        if err_msg := _model_error(model):
            return err_msg
        params: dict[str, Any] = {
            "limit": _clamp(limit, 25),
            "offset": max(0, _int_or(offset, 0)),
            "order_by": "-created_ts",
            "skip_intelligence": "true",
            "skip_associations": "true",
        }
        if name:
            params["name__contains"] = name
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{v1}/{model}/",
                _PROVIDER,
                f"list {model} entities",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = _json_body(resp) or {}
            objects = _objects(data)
            result = format_entity_list(objects, _format_threat_model, noun=model)
            return result + _page_hint(data, params["offset"], len(objects))

    async def get_anomali_threat_model(model: str, entity_id: str) -> str:
        """Get full detail for one threat model entity (description included)."""
        model = (model or "").strip().lower()
        if err_msg := _model_error(model):
            return err_msg
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{v1}/{model}/{_seg(entity_id)}/",
                _PROVIDER,
                f"get {model} {entity_id}",
                headers=headers,
                params={"skip_intelligence": "true", "skip_associations": "true"},
            )
            if err:
                return err
            obj = _json_body(resp) or {}
            lines = [_format_threat_model(obj)]
            # tipreport keeps its content in `body`, signatures in `notes`,
            # everything else in `description`.
            content = obj.get("description") or obj.get("body") or obj.get("notes")
            if content:
                text = str(content)
                lines.append(
                    f"  Content: {text[:1500]}{'...' if len(text) > 1500 else ''}"
                )
            for field in ("status", "source", "circles", "modified_ts", "uuid"):
                val = obj.get(field)
                if val not in (None, "", []):
                    lines.append(f"  {field}: {val}")
            return "\n".join(lines)

    async def search_anomali_threat_models(
        keyword: str = "",
        model_types: str = "",
        name: str = "",
        tags: str = "",
        publication_status: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Search across ALL threat model entity types by keyword/name/tags.

        keyword searches aliases, description, name, and tags; model_types is a
        CSV filter (actor, campaign, incident, signature, ttp, vulnerability,
        tipreport, attackpattern, malware, ...).
        """
        params: dict[str, Any] = {
            "limit": _clamp(limit, 25),
            "offset": max(0, _int_or(offset, 0)),
        }
        if keyword:
            params["value"] = keyword
        if model_types:
            params["model_type"] = model_types
        if name:
            params["name"] = name
        if tags:
            params["tags"] = tags
        if publication_status:
            params["publication_status"] = publication_status
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{v1}/threat_model_search/",
                _PROVIDER,
                "search threat models",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = _json_body(resp) or {}
            objects = _objects(data)
            result = format_entity_list(
                objects, _format_threat_model, noun="threat model entity"
            )
            return result + _page_hint(data, params["offset"], len(objects))

    async def get_anomali_threat_model_intelligence(
        model: str,
        entity_id: str,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """List the observables associated with a threat model entity."""
        model = (model or "").strip().lower()
        if err_msg := _model_error(model):
            return err_msg
        params = {
            "limit": _clamp(limit, 25),
            "offset": max(0, _int_or(offset, 0)),
        }
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{v1}/{model}/{_seg(entity_id)}/intelligence/",
                _PROVIDER,
                f"list intelligence for {model} {entity_id}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = _json_body(resp) or {}
            objects = _objects(data)
            result = format_entity_list(
                objects,
                _format_observable,
                noun="associated observable",
                empty_msg=f"No observables associated with {model} {entity_id}.",
            )
            return result + _page_hint(data, params["offset"], len(objects))

    async def create_anomali_threat_model(
        model: str,
        name: str,
        description: str = "",
        is_public: bool = False,
        tlp: str = "",
        tags: str = "",
        tags_tlp: str = "",
        fields_json: str = "",
    ) -> str:
        """Create a threat model entity (actor, campaign, incident, ...).

        Use fields_json for model-specific fields, e.g. threat bulletin body
        ('{"body": "..."}') or signature type ('{"s_type": "YARA", "notes": "..."}').
        """
        model = (model or "").strip().lower()
        if err_msg := _model_error(model):
            return err_msg
        payload: dict[str, Any] = {"name": name, "is_public": bool(is_public)}
        if description:
            payload["description"] = description
        if tlp:
            payload["tlp"] = tlp.strip().lower()
        tag_list = _tag_payload(tags, tags_tlp)
        if tag_list:
            payload["tags"] = tag_list
        if fields_json:
            try:
                extra_fields = json.loads(fields_json)
            except json.JSONDecodeError as exc:
                return f"Error: fields_json is not valid JSON: {exc}"
            if not isinstance(extra_fields, dict):
                return "Error: fields_json must be a JSON object."
            payload.update(extra_fields)
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{v1}/{model}/",
                _PROVIDER,
                f"create {model} '{name}'",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            created = _json_body(resp) or {}
            return (
                f"{model.title()} created: #{created.get('id', '?')} "
                f"{created.get('name', name)}"
            )

    async def update_anomali_threat_model(
        model: str,
        entity_id: str,
        name: str = "",
        description: str = "",
        tlp: str = "",
        tags: str = "",
        tags_tlp: str = "",
        fields_json: str = "",
    ) -> str:
        """Update a threat model entity. Only provided fields are changed."""
        model = (model or "").strip().lower()
        if err_msg := _model_error(model):
            return err_msg
        payload: dict[str, Any] = {}
        if name:
            payload["name"] = name
        if description:
            payload["description"] = description
        if tlp:
            payload["tlp"] = tlp.strip().lower()
        tag_list = _tag_payload(tags, tags_tlp)
        if tag_list:
            payload["tags"] = tag_list
        if fields_json:
            try:
                extra_fields = json.loads(fields_json)
            except json.JSONDecodeError as exc:
                return f"Error: fields_json is not valid JSON: {exc}"
            if not isinstance(extra_fields, dict):
                return "Error: fields_json must be a JSON object."
            payload.update(extra_fields)
        if not payload:
            return "No fields to update."
        async with _client() as client:
            _resp, err = await safe_request(
                client,
                "patch",
                f"{v1}/{model}/{_seg(entity_id)}/",
                _PROVIDER,
                f"update {model} {entity_id}",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            return f"{model.title()} {entity_id} updated successfully."

    async def manage_anomali_threat_model(
        action: str,
        model: str,
        entity_id: str,
        tags: str = "",
        tags_tlp: str = "red",
        associated_model: str = "",
        associated_ids: str = "",
        comment_body: str = "",
    ) -> str:
        """Long-tail threat model operations: add_tags, associate, comment, delete.

        - add_tags: add tags (CSV) to the entity
        - associate: bulk-associate other entities (associated_model +
          associated_ids CSV) with this one
        - comment: add a comment to the entity
        - delete: permanently delete the entity (cannot be undone)
        """
        act = (action or "").strip().lower()
        model = (model or "").strip().lower()
        if err_msg := _model_error(model):
            return err_msg
        if act not in ("add_tags", "associate", "comment", "delete"):
            return "Error: action must be one of add_tags, associate, comment, delete."
        async with _client() as client:
            if act == "add_tags":
                tag_list = _tag_payload(tags, tags_tlp or "red")
                if not tag_list:
                    return "Error: tags is required for add_tags (CSV)."
                _resp, err = await safe_request(
                    client,
                    "post",
                    f"{v1}/{model}/{_seg(entity_id)}/tag/",
                    _PROVIDER,
                    f"tag {model} {entity_id}",
                    headers=headers,
                    json={"tags": tag_list},
                )
                if err:
                    return err
                return f"Tags added to {model} {entity_id}."
            if act == "associate":
                assoc = (associated_model or "").strip().lower()
                if assoc not in _THREAT_MODELS and assoc != "intelligence":
                    return (
                        "Error: associated_model must be one of "
                        f"{', '.join(sorted(_THREAT_MODELS))}, intelligence."
                    )
                try:
                    ids = [
                        int(i.strip())
                        for i in (associated_ids or "").split(",")
                        if i.strip()
                    ]
                except ValueError:
                    return "Error: associated_ids must be a CSV of numeric IDs."
                if not ids:
                    return "Error: associated_ids is required for associate (CSV)."
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{v1}/{model}/{_seg(entity_id)}/{assoc}/bulk_add/",
                    _PROVIDER,
                    f"associate {assoc} with {model} {entity_id}",
                    headers=headers,
                    json={"ids": ids},
                )
                if err:
                    return err
                body = _json_body(resp) or {}
                linked = body.get("ids") if isinstance(body, dict) else None
                count = len(linked) if isinstance(linked, list) else len(ids)
                # ``intelligence`` is the API name for observables; say
                # "observable(s)" in user-facing output instead of the
                # confusing "intelligence entities".
                if assoc == "intelligence":
                    what = "observable" if count == 1 else "observables"
                else:
                    what = f"{assoc} {'entity' if count == 1 else 'entities'}"
                return f"{count} {what} associated with {model} {entity_id}."
            if act == "comment":
                if not comment_body:
                    return "Error: comment_body is required for comment."
                # The comment endpoint consumes the raw comment text as the
                # request body (the Splunk SOAR reference connector's shape),
                # not a JSON envelope.
                _resp, err = await safe_request(
                    client,
                    "post",
                    f"{v1}/{model}/{_seg(entity_id)}/comment/",
                    _PROVIDER,
                    f"comment on {model} {entity_id}",
                    headers=headers,
                    content=comment_body.encode("utf-8"),
                )
                if err:
                    return err
                return f"Comment added to {model} {entity_id}."
            # delete
            _resp, err = await safe_request(
                client,
                "delete",
                f"{v1}/{model}/{_seg(entity_id)}/",
                _PROVIDER,
                f"delete {model} {entity_id}",
                headers=headers,
            )
            if err:
                return err
            return f"{model.title()} {entity_id} deleted permanently."

    # ══════════════════════════════════════════════════════════════════════
    # 5. INVESTIGATIONS
    # ══════════════════════════════════════════════════════════════════════

    async def list_anomali_investigations(
        investigation_id: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """List investigations, or get one by ID."""
        async with _client() as client:
            if investigation_id:
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{v1}/investigation/{_seg(investigation_id)}/",
                    _PROVIDER,
                    f"get investigation {investigation_id}",
                    headers=headers,
                )
                if err:
                    return err
                obj = _json_body(resp) or {}
                lines = [_format_investigation(obj)]
                if obj.get("description"):
                    lines.append(f"  Description: {str(obj['description'])[:500]}")
                tags = obj.get("tags")
                if isinstance(tags, list) and tags:
                    lines.append(
                        "  Tags: "
                        + ", ".join(
                            t.get("name", str(t)) if isinstance(t, dict) else str(t)
                            for t in tags
                        )
                    )
                return "\n".join(lines)
            params = {
                "limit": _clamp(limit, 25),
                "offset": max(0, _int_or(offset, 0)),
                "order_by": "-created_ts",
            }
            resp, err = await safe_request(
                client,
                "get",
                f"{v1}/investigation/",
                _PROVIDER,
                "list investigations",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = _json_body(resp) or {}
            objects = _objects(data)
            result = format_entity_list(
                objects, _format_investigation, noun="investigation"
            )
            return result + _page_hint(data, params["offset"], len(objects))

    async def create_anomali_investigation(
        name: str,
        description: str = "",
        priority: str = "medium",
        status: str = "unassigned",
        tags: str = "",
        tlp: str = "",
        assignee_id: int = 0,
    ) -> str:
        """Create a ThreatStream investigation (case) to track analysis work.

        priority: verylow, low, medium, high, veryhigh.
        status: completed, in-progress, pending, unassigned.
        """
        payload: dict[str, Any] = {
            "name": name,
            "priority": (priority or "medium").strip().lower().replace(" ", ""),
            "status": (status or "unassigned").strip().lower(),
        }
        if description:
            payload["description"] = description
        tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
        if tag_list:
            payload["tags"] = tag_list
        if tlp:
            payload["tlp"] = tlp.strip().lower()
        if _int_or(assignee_id, 0):
            payload["assignee_id"] = _int_or(assignee_id, 0)
            payload["assignee_type"] = "user"
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{v1}/investigation/",
                _PROVIDER,
                f"create investigation '{name}'",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            created = _json_body(resp) or {}
            return (
                f"Investigation created: #{created.get('id', '?')} "
                f"{created.get('name', name)}"
            )

    async def manage_anomali_investigation(
        action: str,
        investigation_id: str,
        description: str = "",
        priority: str = "",
        status: str = "",
        assignee_id: int = 0,
        element_model: str = "",
        element_ids: str = "",
    ) -> str:
        """Long-tail investigation operations: update, add_elements, delete.

        - update: change description / priority / status / assignee
        - add_elements: attach observables or threat model entities
          (element_model: observable, actor, campaign, incident, signature,
          tipreport, ttp, vulnerability; element_ids: CSV of numeric IDs)
        - delete: permanently delete the investigation (cannot be undone)
        """
        act = (action or "").strip().lower()
        if act not in ("update", "add_elements", "delete"):
            return "Error: action must be one of update, add_elements, delete."
        async with _client() as client:
            if act == "update":
                payload: dict[str, Any] = {}
                if description:
                    payload["description"] = description
                if priority:
                    payload["priority"] = priority.strip().lower().replace(" ", "")
                if status:
                    payload["status"] = status.strip().lower()
                if _int_or(assignee_id, 0):
                    payload["assignee_id"] = _int_or(assignee_id, 0)
                    payload["assignee_type"] = "user"
                if not payload:
                    return "No fields to update."
                _resp, err = await safe_request(
                    client,
                    "patch",
                    f"{v1}/investigation/{_seg(investigation_id)}/",
                    _PROVIDER,
                    f"update investigation {investigation_id}",
                    headers=headers,
                    json=payload,
                )
                if err:
                    return err
                return f"Investigation {investigation_id} updated successfully."
            if act == "add_elements":
                r_type = _ELEMENT_TYPES.get((element_model or "").strip().lower())
                if not r_type:
                    return (
                        "Error: element_model must be one of "
                        f"{', '.join(sorted(_ELEMENT_TYPES))}."
                    )
                try:
                    ids = [
                        int(i.strip())
                        for i in (element_ids or "").split(",")
                        if i.strip()
                    ]
                except ValueError:
                    return "Error: element_ids must be a CSV of numeric IDs."
                if not ids:
                    return "Error: element_ids is required for add_elements (CSV)."
                elements = [{"r_type": r_type, "r_id": i} for i in ids]
                _resp, err = await safe_request(
                    client,
                    "post",
                    f"{v1}/investigationelement/",
                    _PROVIDER,
                    f"add elements to investigation {investigation_id}",
                    headers=headers,
                    params={"investigation_id": investigation_id},
                    json={"objects": elements},
                )
                if err:
                    return err
                return (
                    f"{len(ids)} element(s) added to investigation "
                    f"{investigation_id}."
                )
            # delete
            _resp, err = await safe_request(
                client,
                "delete",
                f"{v1}/investigation/{_seg(investigation_id)}/",
                _PROVIDER,
                f"delete investigation {investigation_id}",
                headers=headers,
            )
            if err:
                return err
            return f"Investigation {investigation_id} deleted permanently."

    # ══════════════════════════════════════════════════════════════════════
    # 6. RULES
    # ══════════════════════════════════════════════════════════════════════

    async def list_anomali_rules(
        rule_id: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """List keyword rules that match incoming intelligence, or get one by ID."""
        async with _client() as client:
            if rule_id:
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{v1}/rule/{_seg(rule_id)}/",
                    _PROVIDER,
                    f"get rule {rule_id}",
                    headers=headers,
                )
                if err:
                    return err
                return _format_rule(_json_body(resp) or {})
            params = {
                "limit": _clamp(limit, 25),
                "offset": max(0, _int_or(offset, 0)),
                "order_by": "-created_ts",
            }
            resp, err = await safe_request(
                client,
                "get",
                f"{v1}/rule/",
                _PROVIDER,
                "list rules",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = _json_body(resp) or {}
            objects = _objects(data)
            result = format_entity_list(objects, _format_rule, noun="rule")
            return result + _page_hint(data, params["offset"], len(objects))

    async def manage_anomali_rule(
        action: str,
        rule_id: str = "",
        name: str = "",
        keywords: str = "",
        match_observables: bool | None = None,
        match_threat_bulletins: bool | None = None,
        match_signatures: bool | None = None,
        match_vulnerabilities: bool | None = None,
        tags: str = "",
    ) -> str:
        """Create, update, or delete a ThreatStream keyword rule.

        - create: requires name + keywords (CSV); the match_* flags choose what
          the rule scans (observables, threat bulletins, signatures,
          vulnerabilities) - unset flags default to observables-only
        - update: requires rule_id; only provided fields change (unset match_*
          flags keep the rule's current settings)
        - delete: requires rule_id; permanent, cannot be undone
        """
        act = (action or "").strip().lower()
        if act not in ("create", "update", "delete"):
            return "Error: action must be one of create, update, delete."
        async with _client() as client:
            if act == "delete":
                if not rule_id:
                    return "Error: rule_id is required for delete."
                _resp, err = await safe_request(
                    client,
                    "delete",
                    f"{v1}/rule/{_seg(rule_id)}/",
                    _PROVIDER,
                    f"delete rule {rule_id}",
                    headers=headers,
                )
                if err:
                    return err
                return f"Rule {rule_id} deleted permanently."
            payload: dict[str, Any] = {}
            if name:
                payload["name"] = name
            keyword_list = [k.strip() for k in (keywords or "").split(",") if k.strip()]
            if keyword_list:
                payload["keywords"] = keyword_list
            if act == "create":
                # Defaults only apply on create; an update must not silently
                # overwrite the rule's current match settings (tri-state).
                payload["match_observables"] = (
                    True if match_observables is None else bool(match_observables)
                )
                payload["match_tips"] = bool(match_threat_bulletins)
                payload["match_signatures"] = bool(match_signatures)
                payload["match_vulnerabilities"] = bool(match_vulnerabilities)
            else:
                if match_observables is not None:
                    payload["match_observables"] = bool(match_observables)
                if match_threat_bulletins is not None:
                    payload["match_tips"] = bool(match_threat_bulletins)
                if match_signatures is not None:
                    payload["match_signatures"] = bool(match_signatures)
                if match_vulnerabilities is not None:
                    payload["match_vulnerabilities"] = bool(match_vulnerabilities)
            tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
            if tag_list:
                payload["tags"] = [{"name": t} for t in tag_list]
            if act == "create":
                if not name or not keyword_list:
                    return "Error: name and keywords are required for create."
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{v1}/rule/",
                    _PROVIDER,
                    f"create rule '{name}'",
                    headers=headers,
                    json=payload,
                )
                if err:
                    return err
                created = _json_body(resp) or {}
                return f"Rule created: #{created.get('id', '?')} {name}"
            # update
            if not rule_id:
                return "Error: rule_id is required for update."
            if not payload:
                return "No fields to update."
            _resp, err = await safe_request(
                client,
                "patch",
                f"{v1}/rule/{_seg(rule_id)}/",
                _PROVIDER,
                f"update rule {rule_id}",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            return f"Rule {rule_id} updated successfully."

    # ══════════════════════════════════════════════════════════════════════
    # 7. ORG WHITELIST
    # ══════════════════════════════════════════════════════════════════════

    async def list_anomali_whitelist(limit: int = 25, offset: int = 0) -> str:
        """List org whitelist entries (values excluded from matching/alerting)."""
        params = {
            "limit": _clamp(limit, 25),
            "offset": max(0, _int_or(offset, 0)),
            "format": "json",
            "showNote": "true",
            "order_by": "-created_ts",
        }
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{v1}/orgwhitelist/",
                _PROVIDER,
                "list whitelist entries",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = _json_body(resp) or {}
            objects = _objects(data)
            result = format_entity_list(
                objects, _format_whitelist_entry, noun="whitelist entry"
            )
            return result + _page_hint(data, params["offset"], len(objects))

    async def manage_anomali_whitelist(
        action: str,
        value_type: str = "",
        values: str = "",
        entry_id: str = "",
        note: str = "",
    ) -> str:
        """Add, annotate, or remove org whitelist entries.

        - add: requires value_type (cidr, domain, email, ip, md5, url,
          user-agent) + values (CSV)
        - update_note: requires entry_id + note
        - delete: requires entry_id; permanent, cannot be undone
        """
        act = (action or "").strip().lower()
        if act not in ("add", "update_note", "delete"):
            return "Error: action must be one of add, update_note, delete."
        async with _client() as client:
            if act == "add":
                vt = (value_type or "").strip().lower()
                value_list = [v.strip() for v in (values or "").split(",") if v.strip()]
                if not vt or not value_list:
                    return "Error: value_type and values are required for add."
                if vt not in _WHITELIST_VALUE_TYPES:
                    return (
                        "Error: value_type must be one of "
                        f"{', '.join(sorted(_WHITELIST_VALUE_TYPES))}."
                    )
                entries = [{"value_type": vt, "value": v} for v in value_list]
                _resp, err = await safe_request(
                    client,
                    "post",
                    f"{v1}/orgwhitelist/bulk/",
                    _PROVIDER,
                    f"add {len(entries)} whitelist entries",
                    headers=headers,
                    json={"whitelist": entries},
                )
                if err:
                    return err
                noun = "entry" if len(entries) == 1 else "entries"
                return f"{len(entries)} whitelist {noun} added successfully."
            if not entry_id:
                return f"Error: entry_id is required for {act}."
            if act == "update_note":
                if not note:
                    return "Error: note is required for update_note."
                _resp, err = await safe_request(
                    client,
                    "patch",
                    f"{v1}/orgwhitelist/{_seg(entry_id)}/",
                    _PROVIDER,
                    f"update whitelist entry {entry_id}",
                    headers=headers,
                    json={"notes": note},
                )
                if err:
                    return err
                return f"Whitelist entry {entry_id} note updated."
            # delete
            _resp, err = await safe_request(
                client,
                "delete",
                f"{v1}/orgwhitelist/{_seg(entry_id)}/",
                _PROVIDER,
                f"delete whitelist entry {entry_id}",
                headers=headers,
            )
            if err:
                return err
            return f"Whitelist entry {entry_id} deleted permanently."

    # ══════════════════════════════════════════════════════════════════════
    # 8. SANDBOX
    # ══════════════════════════════════════════════════════════════════════

    async def manage_anomali_sandbox(
        action: str,
        url: str = "",
        platform: str = "",
        report_id: str = "",
        classification: str = "private",
        use_premium: bool = False,
    ) -> str:
        """Drive the ThreatStream sandbox: platforms, submit_url, status, report.

        - platforms: list supported detonation platforms
        - submit_url: detonate a URL (requires url + platform)
        - status: poll a submission (requires report_id)
        - report: fetch the detonation report (requires report_id)
        """
        act = (action or "").strip().lower()
        if act not in ("platforms", "submit_url", "status", "report"):
            return "Error: action must be one of platforms, submit_url, status, report."
        async with _client(60) as client:
            if act == "platforms":
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{v1}/submit/parameters/",
                    _PROVIDER,
                    "list sandbox platforms",
                    headers=headers,
                )
                if err:
                    return err
                data = _json_body(resp) or {}
                default = data.get("platform_choices", [])
                premium = data.get("premium_platform_choices", [])
                lines = ["Sandbox platforms:"]
                if default:
                    lines.append(f"  Default: {', '.join(map(str, default))}")
                if premium:
                    lines.append(f"  Premium: {', '.join(map(str, premium))}")
                if len(lines) == 1:
                    return "No sandbox platforms available for this account."
                return "\n".join(lines)
            if act == "submit_url":
                if not url or not platform:
                    return "Error: url and platform are required for submit_url."
                cls = (classification or "private").strip().lower()
                if cls not in _CLASSIFICATIONS:
                    return "Error: classification must be one of private, public."
                data_payload: dict[str, Any] = {
                    "report_radio-url": url,
                    "report_radio-platform": platform,
                    "report_radio-classification": cls,
                    "use_premium_sandbox": str(bool(use_premium)).lower(),
                }
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{v1}/submit/new/",
                    _PROVIDER,
                    f"submit URL to sandbox ({platform})",
                    headers=headers,
                    data=data_payload,
                )
                if err:
                    return err
                body = _json_body(resp) or {}
                reports = body.get("reports") or {}
                if isinstance(reports, dict) and reports:
                    ids = ", ".join(
                        f"{plat}: {(info or {}).get('id', '?')!s}"
                        for plat, info in reports.items()
                        if isinstance(info, dict)
                    )
                    return (
                        f"URL submitted to the sandbox. Report IDs - {ids}. "
                        "Poll with action=status."
                    )
                return (
                    "URL submitted to the sandbox. Response: "
                    f"{str(body)[:300]}"
                )
            if not report_id:
                return f"Error: report_id is required for {act}."
            if act == "status":
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{v1}/submit/{_seg(report_id)}/",
                    _PROVIDER,
                    f"get sandbox status {report_id}",
                    headers=headers,
                )
                if err:
                    return err
                body = _json_body(resp) or {}
                return (
                    f"Sandbox submission {report_id}: "
                    f"status={body.get('status', 'done')} | "
                    f"verdict={body.get('verdict', '?')} | "
                    f"platform={body.get('platform', '?')}"
                )
            # report
            resp, err = await safe_request(
                client,
                "get",
                f"{v1}/submit/{_seg(report_id)}/report/",
                _PROVIDER,
                f"get sandbox report {report_id}",
                headers=headers,
            )
            if err:
                return err
            body = _json_body(resp) or {}
            # The detonation report rides in a Cuckoo-style ``results``
            # envelope (per the Cortex XSOAR v3 reference connector).
            report = body.get("results") if isinstance(body, dict) else None
            info = (report or {}).get("info") if isinstance(report, dict) else None
            lines = [f"Sandbox report {report_id}:"]
            if isinstance(info, dict):
                for field in ("started", "ended", "duration", "score"):
                    if info.get(field) is not None:
                        lines.append(f"  {field}: {info[field]}")
            text = json.dumps(body, default=str)
            lines.append(f"  Raw (truncated): {text[:1200]}")
            return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 9. USERS
    # ══════════════════════════════════════════════════════════════════════

    async def list_anomali_users(limit: int = 25, offset: int = 0) -> str:
        """List organization users (resolve assignee IDs for investigations)."""
        params = {
            "limit": _clamp(limit, 25),
            "offset": max(0, _int_or(offset, 0)),
        }
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{v1}/orgadmin/",
                _PROVIDER,
                "list org users",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = _json_body(resp) or {}
            objects = _objects(data)
            result = format_entity_list(objects, _format_user, noun="user")
            return result + _page_hint(data, params["offset"], len(objects))

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    # 0. Connection
    executor.register("get_anomali_status", get_anomali_status)
    # 1. Intelligence
    executor.register("search_anomali_intelligence", search_anomali_intelligence)
    executor.register("get_anomali_observable", get_anomali_observable)
    executor.register("get_anomali_passive_dns", get_anomali_passive_dns)
    # 2. Importing intelligence
    executor.register("import_anomali_observables", import_anomali_observables)
    executor.register("create_anomali_observables", create_anomali_observables)
    executor.register("update_anomali_observable", update_anomali_observable)
    executor.register("delete_anomali_observable", delete_anomali_observable)
    executor.register(
        "manage_anomali_observable_tags", manage_anomali_observable_tags
    )
    # 3. Import sessions
    executor.register("list_anomali_import_sessions", list_anomali_import_sessions)
    executor.register(
        "approve_anomali_import_session", approve_anomali_import_session
    )
    # 4. Threat model entities
    executor.register("list_anomali_threat_models", list_anomali_threat_models)
    executor.register("get_anomali_threat_model", get_anomali_threat_model)
    executor.register("search_anomali_threat_models", search_anomali_threat_models)
    executor.register(
        "get_anomali_threat_model_intelligence",
        get_anomali_threat_model_intelligence,
    )
    executor.register("create_anomali_threat_model", create_anomali_threat_model)
    executor.register("update_anomali_threat_model", update_anomali_threat_model)
    executor.register("manage_anomali_threat_model", manage_anomali_threat_model)
    # 5. Investigations
    executor.register("list_anomali_investigations", list_anomali_investigations)
    executor.register("create_anomali_investigation", create_anomali_investigation)
    executor.register("manage_anomali_investigation", manage_anomali_investigation)
    # 6. Rules
    executor.register("list_anomali_rules", list_anomali_rules)
    executor.register("manage_anomali_rule", manage_anomali_rule)
    # 7. Whitelist
    executor.register("list_anomali_whitelist", list_anomali_whitelist)
    executor.register("manage_anomali_whitelist", manage_anomali_whitelist)
    # 8. Sandbox
    executor.register("manage_anomali_sandbox", manage_anomali_sandbox)
    # 9. Users
    executor.register("list_anomali_users", list_anomali_users)

    _int = {"type": "integer"}
    _str = {"type": "string"}

    return [
        # ── 0. Connection ─────────────────────────────────────────────────
        ToolDefinition(
            name="get_anomali_status",
            description=(
                "Validate the Anomali ThreatStream connection and report the "
                "authenticated account and the number of intelligence records "
                "visible to it. Use this first when ThreatStream calls fail."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        # ── 1. Intelligence ───────────────────────────────────────────────
        ToolDefinition(
            name="search_anomali_intelligence",
            description=(
                "Search ThreatStream intelligence (IOC observables) with "
                "structured filters: exact value, itype (e.g. 'mal_ip', "
                "'c2_domain', 'phish_url'), broad type (ip, domain, url, email, "
                "md5), status (active, inactive, falsepos), confidence bounds, "
                "tags, feed_id, and modified-since. Prefer the structured params; "
                "use query only for advanced expressions (sent as the raw q= "
                "search, e.g. '(itype=\"mal_ip\" OR itype=\"c2_ip\") AND "
                "confidence>80'). Returns IDs for follow-up calls. Use offset "
                "for pagination."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "Exact observable value (IP, domain, URL, email, hash)",
                        "default": "",
                    },
                    "itype": {
                        "type": "string",
                        "description": "Indicator type, e.g. 'mal_ip', 'c2_domain', 'phish_url', 'mal_md5'",
                        "default": "",
                    },
                    "ioc_type": {
                        "type": "string",
                        "description": "Broad type filter",
                        "enum": ["", "ip", "ipv6", "domain", "url", "email", "md5", "hash"],
                        "default": "",
                    },
                    "status": {
                        "type": "string",
                        "description": "Observable status",
                        "enum": ["", "active", "inactive", "falsepos"],
                        "default": "",
                    },
                    "confidence_min": {
                        "type": "integer",
                        "description": "Minimum confidence 0-100 (-1 = no filter)",
                        "default": -1,
                    },
                    "confidence_max": {
                        "type": "integer",
                        "description": "Maximum confidence 0-100 (-1 = no filter)",
                        "default": -1,
                    },
                    "tags": {
                        "type": "string",
                        "description": "Tag name filter (CSV)",
                        "default": "",
                    },
                    "feed_id": {
                        "type": "integer",
                        "description": "Numeric feed ID filter (0 = no filter)",
                        "default": 0,
                    },
                    "modified_since": {
                        "type": "string",
                        "description": "Only observables modified at/after this UTC ISO timestamp (e.g. '2026-01-01T00:00:00Z')",
                        "default": "",
                    },
                    "query": {
                        "type": "string",
                        "description": "Advanced raw search expression (q=) for filters the structured params don't cover",
                        "default": "",
                    },
                    "order_by": {
                        "type": "string",
                        "description": "Sort field, prefix '-' for descending",
                        "default": "-modified_ts",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results per page (max 1000)",
                        "default": 25,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Offset for pagination",
                        "default": 0,
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_anomali_observable",
            description=(
                "Get full detail for one ThreatStream observable by numeric ID: "
                "value, itype, confidence, status, severity, threat type, "
                "classification, geo/ASN context, tags, and description. Call "
                "search_anomali_intelligence first to find observable IDs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "observable_id": {
                        "type": "string",
                        "description": "Numeric observable ID",
                    },
                },
                "required": ["observable_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_anomali_passive_dns",
            description=(
                "Get passive DNS resolutions for an IP or a domain from "
                "ThreatStream - pivot from one piece of infrastructure to the "
                "domains/IPs historically associated with it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "value_type": {
                        "type": "string",
                        "description": "Type of the value",
                        "enum": ["ip", "domain"],
                    },
                    "value": {
                        "type": "string",
                        "description": "The IP address or domain to pivot on",
                    },
                },
                "required": ["value_type", "value"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 2. Importing intelligence ─────────────────────────────────────
        ToolDefinition(
            name="import_anomali_observables",
            description=(
                "Import observables into the ThreatStream REVIEW QUEUE (import "
                "with approval). datatext is free text or CSV containing the "
                "IOC values - ThreatStream auto-detects types. The items land "
                "in an import session that must be approved "
                "(approve_anomali_import_session) before becoming active. Use "
                "create_anomali_observables instead to skip the review step."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "datatext": {
                        "type": "string",
                        "description": "Free text / CSV of IOC values to import (auto-detected)",
                    },
                    "classification": {
                        "type": "string",
                        "description": "Visibility of the imported observables",
                        "enum": ["private", "public"],
                        "default": "private",
                    },
                    "confidence": {
                        "type": "integer",
                        "description": "Confidence 0-100 to assign",
                        "default": 50,
                    },
                    "severity": {
                        "type": "string",
                        "description": "Severity to assign",
                        "enum": ["low", "medium", "high", "very-high"],
                        "default": "low",
                    },
                    "threat_type": {
                        "type": "string",
                        "description": "Threat type, e.g. 'malware', 'c2', 'phish', 'exploit'",
                        "default": "exploit",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Tags to apply (CSV)",
                        "default": "",
                    },
                    "tags_tlp": {
                        "type": "string",
                        "description": "TLP for the tags (red = org-only, white = public)",
                        "enum": ["", "red", "white"],
                        "default": "",
                    },
                    "expiration_ts": {
                        "type": "string",
                        "description": "UTC ISO expiration timestamp (optional)",
                        "default": "",
                    },
                    "default_state": {
                        "type": "string",
                        "description": "State after approval",
                        "enum": ["active", "inactive"],
                        "default": "inactive",
                    },
                },
                "required": ["datatext"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="create_anomali_observables",
            description=(
                "Create observables DIRECTLY in ThreatStream (import without "
                "approval - requires the 'Approve Intel' permission). "
                "objects_json is a JSON array of observables, each with an "
                "itype and a value field (srcip, domain, url, email, or md5), "
                "e.g. '[{\"itype\": \"mal_ip\", \"srcip\": \"1.2.3.4\"}]'. "
                "Use import_anomali_observables for the safer review-queue flow."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "objects_json": {
                        "type": "string",
                        "description": "JSON array of observable objects (itype + srcip/domain/url/email/md5)",
                    },
                    "classification": {
                        "type": "string",
                        "enum": ["private", "public"],
                        "default": "private",
                        "description": "Visibility of the created observables",
                    },
                    "confidence": {
                        "type": "integer",
                        "description": "Confidence 0-100 (-1 = ThreatStream default)",
                        "default": -1,
                    },
                    "severity": {
                        "type": "string",
                        "description": "Severity for all objects (optional; can also be set per object)",
                        "enum": ["", "low", "medium", "high", "very-high"],
                        "default": "",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Tags to apply to all objects (CSV)",
                        "default": "",
                    },
                    "tags_tlp": {
                        "type": "string",
                        "description": "TLP for the tags",
                        "enum": ["", "red", "white"],
                        "default": "",
                    },
                    "allow_unresolved": {
                        "type": "boolean",
                        "description": "Accept domains that do not currently resolve",
                        "default": False,
                    },
                },
                "required": ["objects_json"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_anomali_observable",
            description=(
                "Update an existing ThreatStream observable: retune confidence, "
                "flip status (active / inactive / falsepos - use falsepos to "
                "mark a false positive), change severity, or set an expiration "
                "timestamp. Only provided fields change."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "observable_id": {
                        "type": "string",
                        "description": "Numeric observable ID",
                    },
                    "confidence": {
                        "type": "integer",
                        "description": "New confidence 0-100 (-1 = leave unchanged)",
                        "default": -1,
                    },
                    "status": {
                        "type": "string",
                        "description": "New status",
                        "enum": ["", "active", "inactive", "falsepos"],
                        "default": "",
                    },
                    "severity": {
                        "type": "string",
                        "description": "New severity",
                        "enum": ["", "low", "medium", "high", "very-high"],
                        "default": "",
                    },
                    "expiration_ts": {
                        "type": "string",
                        "description": "UTC ISO expiration timestamp",
                        "default": "",
                    },
                },
                "required": ["observable_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_anomali_observable_tags",
            description=(
                "Bulk add or remove tags on ThreatStream observables. "
                "action='add' applies the tags (with a TLP), action='remove' "
                "strips them. observable_ids and tags are CSV lists."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "remove"],
                        "description": "Whether to add or remove the tags",
                    },
                    "observable_ids": {
                        "type": "string",
                        "description": "CSV of numeric observable IDs",
                    },
                    "tags": {
                        "type": "string",
                        "description": "CSV of tag names",
                    },
                    "tlp": {
                        "type": "string",
                        "description": "TLP for added tags (red = org-only, white = public)",
                        "enum": ["red", "white"],
                        "default": "red",
                    },
                },
                "required": ["action", "observable_ids", "tags"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="delete_anomali_observable",
            description=(
                "Delete a ThreatStream observable permanently. This action is "
                "permanent and cannot be undone - consider "
                "update_anomali_observable with status='falsepos' or 'inactive' "
                "instead when you only need to neutralize it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "observable_id": {
                        "type": "string",
                        "description": "Numeric observable ID to delete",
                    },
                },
                "required": ["observable_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        # ── 3. Import sessions ────────────────────────────────────────────
        ToolDefinition(
            name="list_anomali_import_sessions",
            description=(
                "List ThreatStream import sessions (the review queue for "
                "imported observables), or get one session by import_id. Shows "
                "status, indicator counts, and who approved. Use before "
                "approve_anomali_import_session."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "import_id": {
                        "type": "string",
                        "description": "Specific import session ID (empty = list all)",
                        "default": "",
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter by session status (e.g. 'done', 'processing')",
                        "default": "",
                    },
                    "limit": {**_int, "description": "Max results (max 1000)", "default": 25},
                    "offset": {**_int, "description": "Offset for pagination", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="approve_anomali_import_session",
            description=(
                "Approve ALL observables in a ThreatStream import session, "
                "activating them in the platform (requires the 'Approve Intel' "
                "permission). Review the session with "
                "list_anomali_import_sessions first - approval applies to every "
                "item in the job."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "import_id": {
                        "type": "string",
                        "description": "Import session ID to approve",
                    },
                },
                "required": ["import_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 4. Threat model entities ──────────────────────────────────────
        ToolDefinition(
            name="list_anomali_threat_models",
            description=(
                "List ThreatStream threat model entities of one type: actor, "
                "campaign, incident, signature, ttp, vulnerability, tipreport "
                "(threat bulletins), or attackpattern. Optionally filter by "
                "name substring. Returns IDs for follow-up calls. Use "
                "search_anomali_threat_models for a cross-type keyword search."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "description": "Threat model entity type",
                        "enum": sorted(_THREAT_MODELS),
                    },
                    "name": {
                        "type": "string",
                        "description": "Filter by name substring",
                        "default": "",
                    },
                    "limit": {**_int, "description": "Max results (max 1000)", "default": 25},
                    "offset": {**_int, "description": "Offset for pagination", "default": 0},
                },
                "required": ["model"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_anomali_threat_model",
            description=(
                "Get full detail for one threat model entity (description/body, "
                "visibility, publication status, tags). model=tipreport returns "
                "the threat bulletin body; model=signature returns its notes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "description": "Threat model entity type",
                        "enum": sorted(_THREAT_MODELS),
                    },
                    "entity_id": {
                        "type": "string",
                        "description": "Numeric entity ID",
                    },
                },
                "required": ["model", "entity_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_anomali_threat_models",
            description=(
                "Search across ALL ThreatStream threat model entity types at "
                "once. keyword matches aliases, description, name, and tags; "
                "model_types narrows to a CSV of types (actor, campaign, "
                "incident, signature, ttp, vulnerability, tipreport, "
                "attackpattern, malware). Use this to find an entity when you "
                "don't know its type."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Free-text search over aliases, description, name, tags",
                        "default": "",
                    },
                    "model_types": {
                        "type": "string",
                        "description": "CSV of entity types to include (empty = all)",
                        "default": "",
                    },
                    "name": {**_str, "description": "Exact name filter", "default": ""},
                    "tags": {**_str, "description": "CSV of tags", "default": ""},
                    "publication_status": {
                        "type": "string",
                        "description": "CSV of publication statuses (new, pending_review, review_requested, reviewed, published)",
                        "default": "",
                    },
                    "limit": {**_int, "description": "Max results (max 1000)", "default": 25},
                    "offset": {**_int, "description": "Offset for pagination", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_anomali_threat_model_intelligence",
            description=(
                "List the observables (intelligence) associated with a threat "
                "model entity - e.g. all IOCs tied to an actor, campaign, or "
                "incident. Use offset for pagination."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "description": "Threat model entity type",
                        "enum": sorted(_THREAT_MODELS),
                    },
                    "entity_id": {
                        "type": "string",
                        "description": "Numeric entity ID",
                    },
                    "limit": {**_int, "description": "Max results (max 1000)", "default": 25},
                    "offset": {**_int, "description": "Offset for pagination", "default": 0},
                },
                "required": ["model", "entity_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_anomali_threat_model",
            description=(
                "Create a ThreatStream threat model entity (actor, campaign, "
                "incident, signature, ttp, vulnerability, tipreport, "
                "attackpattern). Use fields_json for model-specific fields, "
                "e.g. a threat bulletin body ('{\"body\": \"...\"}') or a "
                "signature type ('{\"s_type\": \"YARA\", \"notes\": \"...\"}')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "description": "Threat model entity type to create",
                        "enum": sorted(_THREAT_MODELS),
                    },
                    "name": {"type": "string", "description": "Entity name"},
                    "description": {
                        "type": "string",
                        "description": "Entity description",
                        "default": "",
                    },
                    "is_public": {
                        "type": "boolean",
                        "description": "Public (visible to the ThreatStream community) or private",
                        "default": False,
                    },
                    "tlp": {
                        "type": "string",
                        "description": "TLP designation",
                        "enum": ["", "red", "amber", "green", "white"],
                        "default": "",
                    },
                    "tags": {**_str, "description": "Tags to apply (CSV)", "default": ""},
                    "tags_tlp": {
                        "type": "string",
                        "description": "TLP for the tags",
                        "enum": ["", "red", "white"],
                        "default": "",
                    },
                    "fields_json": {
                        "type": "string",
                        "description": "JSON object of model-specific fields merged into the payload",
                        "default": "",
                    },
                },
                "required": ["model", "name"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_anomali_threat_model",
            description=(
                "Update a ThreatStream threat model entity. Only provided "
                "fields change. Use fields_json for model-specific fields "
                "(e.g. tipreport body, incident status)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "description": "Threat model entity type",
                        "enum": sorted(_THREAT_MODELS),
                    },
                    "entity_id": {"type": "string", "description": "Numeric entity ID"},
                    "name": {**_str, "description": "New name", "default": ""},
                    "description": {**_str, "description": "New description", "default": ""},
                    "tlp": {
                        "type": "string",
                        "description": "New TLP designation",
                        "enum": ["", "red", "amber", "green", "white"],
                        "default": "",
                    },
                    "tags": {**_str, "description": "Tags to set (CSV)", "default": ""},
                    "tags_tlp": {
                        "type": "string",
                        "description": "TLP for the tags",
                        "enum": ["", "red", "white"],
                        "default": "",
                    },
                    "fields_json": {
                        "type": "string",
                        "description": "JSON object of model-specific fields merged into the payload",
                        "default": "",
                    },
                },
                "required": ["model", "entity_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_anomali_threat_model",
            description=(
                "Long-tail threat model operations. action='add_tags' tags the "
                "entity; action='associate' bulk-links other entities or "
                "intelligence to it (associated_model + associated_ids CSV); "
                "action='comment' adds a comment to the entity "
                "(comment_body); action='delete' permanently deletes "
                "the entity (cannot be undone)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add_tags", "associate", "comment", "delete"],
                        "description": "Operation to perform",
                    },
                    "model": {
                        "type": "string",
                        "description": "Threat model entity type",
                        "enum": sorted(_THREAT_MODELS),
                    },
                    "entity_id": {"type": "string", "description": "Numeric entity ID"},
                    "tags": {
                        "type": "string",
                        "description": "CSV of tags (for add_tags)",
                        "default": "",
                    },
                    "tags_tlp": {
                        "type": "string",
                        "description": "TLP for added tags",
                        "enum": ["red", "white"],
                        "default": "red",
                    },
                    "associated_model": {
                        "type": "string",
                        "description": "Type of the entities to associate (for associate; 'intelligence' = observables)",
                        "default": "",
                    },
                    "associated_ids": {
                        "type": "string",
                        "description": "CSV of numeric IDs to associate (for associate)",
                        "default": "",
                    },
                    "comment_body": {
                        "type": "string",
                        "description": "Comment text (for comment)",
                        "default": "",
                    },
                },
                "required": ["action", "model", "entity_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 5. Investigations ─────────────────────────────────────────────
        ToolDefinition(
            name="list_anomali_investigations",
            description=(
                "List ThreatStream investigations (cases), or get one by ID "
                "with its description and tags. Shows status, priority, "
                "assignee, and element counts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "investigation_id": {
                        "type": "string",
                        "description": "Specific investigation ID (empty = list all)",
                        "default": "",
                    },
                    "limit": {**_int, "description": "Max results (max 1000)", "default": 25},
                    "offset": {**_int, "description": "Offset for pagination", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_anomali_investigation",
            description=(
                "Create a ThreatStream investigation (case) to track analysis "
                "work. Set priority (verylow/low/medium/high/veryhigh), status "
                "(completed/in-progress/pending/unassigned), tags, TLP, and an "
                "assignee (resolve user IDs with list_anomali_users). Attach "
                "elements afterwards with manage_anomali_investigation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Investigation name"},
                    "description": {**_str, "description": "Investigation description", "default": ""},
                    "priority": {
                        "type": "string",
                        "enum": ["verylow", "low", "medium", "high", "veryhigh"],
                        "default": "medium",
                        "description": "Investigation priority",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["completed", "in-progress", "pending", "unassigned"],
                        "default": "unassigned",
                        "description": "Investigation status",
                    },
                    "tags": {**_str, "description": "CSV of tags", "default": ""},
                    "tlp": {
                        "type": "string",
                        "description": "TLP designation",
                        "enum": ["", "red", "amber", "green", "white"],
                        "default": "",
                    },
                    "assignee_id": {
                        "type": "integer",
                        "description": "Numeric user ID to assign (0 = unassigned)",
                        "default": 0,
                    },
                },
                "required": ["name"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_anomali_investigation",
            description=(
                "Manage a ThreatStream investigation. action='update' changes "
                "description/priority/status/assignee; action='add_elements' "
                "attaches observables or threat model entities (element_model + "
                "element_ids CSV); action='delete' permanently deletes the "
                "investigation (cannot be undone)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["update", "add_elements", "delete"],
                        "description": "Operation to perform",
                    },
                    "investigation_id": {
                        "type": "string",
                        "description": "Investigation ID",
                    },
                    "description": {**_str, "description": "New description (update)", "default": ""},
                    "priority": {
                        "type": "string",
                        "description": "New priority (update)",
                        "enum": ["", "verylow", "low", "medium", "high", "veryhigh"],
                        "default": "",
                    },
                    "status": {
                        "type": "string",
                        "description": "New status (update)",
                        "enum": ["", "completed", "in-progress", "pending", "unassigned"],
                        "default": "",
                    },
                    "assignee_id": {
                        "type": "integer",
                        "description": "New assignee user ID (update; 0 = leave unchanged)",
                        "default": 0,
                    },
                    "element_model": {
                        "type": "string",
                        "description": "Element type to attach (add_elements)",
                        "enum": ["", *sorted(_ELEMENT_TYPES)],
                        "default": "",
                    },
                    "element_ids": {
                        "type": "string",
                        "description": "CSV of numeric element IDs to attach (add_elements)",
                        "default": "",
                    },
                },
                "required": ["action", "investigation_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 6. Rules ──────────────────────────────────────────────────────
        ToolDefinition(
            name="list_anomali_rules",
            description=(
                "List ThreatStream keyword rules (they match incoming "
                "intelligence and can trigger alerts/investigations), or get "
                "one rule by ID with its keywords and match count."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "rule_id": {
                        "type": "string",
                        "description": "Specific rule ID (empty = list all)",
                        "default": "",
                    },
                    "limit": {**_int, "description": "Max results (max 1000)", "default": 25},
                    "offset": {**_int, "description": "Offset for pagination", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_anomali_rule",
            description=(
                "Create, update, or delete a ThreatStream keyword rule. "
                "action='create' needs name + keywords (CSV) and the match_* "
                "flags choose what is scanned (unset = observables only); "
                "action='update' changes an existing rule (rule_id required; "
                "only provided fields change - unset match_* flags keep the "
                "rule's current settings); action='delete' permanently "
                "removes it (rule_id required, cannot be undone)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "delete"],
                        "description": "Operation to perform",
                    },
                    "rule_id": {
                        "type": "string",
                        "description": "Rule ID (required for update/delete)",
                        "default": "",
                    },
                    "name": {**_str, "description": "Rule name (required for create)", "default": ""},
                    "keywords": {
                        "type": "string",
                        "description": "CSV of keywords to match (required for create)",
                        "default": "",
                    },
                    "match_observables": {
                        "type": "boolean",
                        "description": "Match incoming observables (create default: true; leave unset on update to keep the current value)",
                    },
                    "match_threat_bulletins": {
                        "type": "boolean",
                        "description": "Match threat bulletins (create default: false; leave unset on update to keep the current value)",
                    },
                    "match_signatures": {
                        "type": "boolean",
                        "description": "Match signatures (create default: false; leave unset on update to keep the current value)",
                    },
                    "match_vulnerabilities": {
                        "type": "boolean",
                        "description": "Match vulnerabilities (create default: false; leave unset on update to keep the current value)",
                    },
                    "tags": {**_str, "description": "CSV of tags", "default": ""},
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 7. Whitelist ──────────────────────────────────────────────────
        ToolDefinition(
            name="list_anomali_whitelist",
            description=(
                "List the org whitelist entries - values ThreatStream excludes "
                "from matching and alerting (e.g. your own infrastructure)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {**_int, "description": "Max results (max 1000)", "default": 25},
                    "offset": {**_int, "description": "Offset for pagination", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_anomali_whitelist",
            description=(
                "Manage the org whitelist. action='add' adds entries "
                "(value_type: cidr, domain, email, ip, md5, url, user-agent; "
                "values: CSV); action='update_note' annotates an entry "
                "(entry_id + note); action='delete' removes an entry "
                "permanently (entry_id, cannot be undone)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "update_note", "delete"],
                        "description": "Operation to perform",
                    },
                    "value_type": {
                        "type": "string",
                        "description": "Type of the values (for add)",
                        "enum": ["", *sorted(_WHITELIST_VALUE_TYPES)],
                        "default": "",
                    },
                    "values": {
                        "type": "string",
                        "description": "CSV of values to whitelist (for add)",
                        "default": "",
                    },
                    "entry_id": {
                        "type": "string",
                        "description": "Whitelist entry ID (for update_note/delete)",
                        "default": "",
                    },
                    "note": {
                        "type": "string",
                        "description": "Note text (for update_note)",
                        "default": "",
                    },
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 8. Sandbox ────────────────────────────────────────────────────
        ToolDefinition(
            name="manage_anomali_sandbox",
            description=(
                "Drive the ThreatStream sandbox. action='platforms' lists "
                "supported detonation platforms; action='submit_url' detonates "
                "a URL (url + platform required); action='status' polls a "
                "submission (report_id); action='report' fetches the detonation "
                "report (report_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["platforms", "submit_url", "status", "report"],
                        "description": "Operation to perform",
                    },
                    "url": {
                        "type": "string",
                        "description": "URL to detonate (for submit_url)",
                        "default": "",
                    },
                    "platform": {
                        "type": "string",
                        "description": "Detonation platform (from action='platforms', e.g. 'WINDOWS10x64')",
                        "default": "",
                    },
                    "report_id": {
                        "type": "string",
                        "description": "Submission/report ID (for status/report)",
                        "default": "",
                    },
                    "classification": {
                        "type": "string",
                        "description": "Visibility of the submission",
                        "enum": ["private", "public"],
                        "default": "private",
                    },
                    "use_premium": {
                        "type": "boolean",
                        "description": "Use the premium sandbox",
                        "default": False,
                    },
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 9. Users ──────────────────────────────────────────────────────
        ToolDefinition(
            name="list_anomali_users",
            description=(
                "List the ThreatStream organization's users (name, email, "
                "active state). Use to resolve numeric assignee IDs for "
                "create_anomali_investigation / manage_anomali_investigation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {**_int, "description": "Max results (max 1000)", "default": 25},
                    "offset": {**_int, "description": "Offset for pagination", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
