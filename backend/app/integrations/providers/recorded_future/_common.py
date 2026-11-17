"""Shared helpers for the Recorded Future provider package.

Recorded Future exposes several sibling APIs under one host
(``https://api.recordedfuture.com``), all authenticated with the same
``X-RFToken`` header:

* Connect API v2 (``/v2``)        - entity lookup / search / risk rules
* SOAR API v3 (``/soar/v3``)      - batch enrichment + verdict triage
* Entity Match (``/entity-match``)- resolve a name to an RF entity id
* Alert API (``/alert``)          - classic alerts + alert rules
* Playbook Alert API (``/playbook-alert``)
* Analyst Note API (``/analyst-note``)
* Detection Rule API (``/detection-rule``)
* Links API (``/links``)
* List API (``/list``)
* Collective Insights API (``/collective-insights``)
* Identity API (``/identity``)
* Threat (actors / maps) API (``/threat``)

The dispatcher (``integration_tools._get_token``) hands this builder the
compound ``base_url||api_token`` token. ``base_url`` is optional and defaults
to the public host; it only needs setting for a sovereign / dedicated tenant.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.egress import egress_client

_PROVIDER = "Recorded Future"
_SOURCE = "integration:recorded_future"

# Public Connect API host. A stored base_url overrides this (e.g. a sovereign
# deployment); it never points at a user-facing UI, so it is not registered in
# integration_tools._USER_FACING_BASE_URL_PROVIDERS.
DEFAULT_BASE_URL = "https://api.recordedfuture.com"

# Entity types accepted by the Connect API v2 lookup / search paths.
ENTITY_TYPES: frozenset[str] = frozenset(
    {"ip", "domain", "url", "hash", "vulnerability", "malware", "company"}
)

# Entity types whose Connect API v2 exposes a ``/v2/{type}/riskrules`` endpoint.
# Malware entities are scored differently and have no risk-rule catalogue, so
# ``/v2/malware/riskrules`` just errors - exclude it from the risk-rule tool.
RISK_RULE_ENTITY_TYPES: frozenset[str] = ENTITY_TYPES - {"malware"}

# SOAR triage risk contexts (each yields a boolean verdict).
TRIAGE_CONTEXTS: frozenset[str] = frozenset({"malware", "phishing", "c2"})

# Per-type default ``fields`` selection. Recorded Future charges response size
# by fields, so we request a compact, high-signal set and let callers override.
DEFAULT_FIELDS: dict[str, str] = {
    "ip": "entity,risk,intelCard,location,relatedEntities,timestamps",
    "domain": "entity,risk,intelCard,relatedEntities,timestamps",
    "url": "entity,risk,intelCard,relatedEntities,timestamps",
    "hash": "entity,risk,intelCard,hashAlgorithm,relatedEntities,timestamps",
    "vulnerability": (
        "entity,risk,intelCard,cvssv3,cvss,nvdDescription,"
        "lifecycleStage,relatedEntities,timestamps"
    ),
    "malware": "entity,intelCard,relatedEntities,timestamps",
    "company": "entity,risk,intelCard,relatedEntities,timestamps",
}

# Risk band thresholds (0-99 score) mapped to a label + indicator emoji. The
# API usually returns its own ``criticalityLabel`` which we prefer; this is the
# fallback when only a numeric score is available (e.g. SOAR results).
_RISK_EMOJI: dict[str, str] = {
    "none": "\u26aa",  # white circle
    "informational": "\u26aa",
    "unusual": "\U0001f535",  # blue circle
    "moderate": "\U0001f535",
    "suspicious": "\U0001f7e1",  # yellow circle
    "malicious": "\U0001f7e0",  # orange circle
    "high": "\U0001f7e0",
    "very malicious": "\U0001f534",  # red circle
    "very high": "\U0001f534",
    "critical": "\U0001f534",
}


def rf_api_base(base_url: str | None) -> str:
    """Return the Recorded Future API base URL (no trailing slash).

    Whitespace is stripped before the default fallback and the trailing-slash
    trim so a pasted ``base_url`` with surrounding whitespace (or a blank one)
    normalises to a valid host. Shared by the tool builder and the connectivity
    test so both target the same host.
    """
    return ((base_url or "").strip() or DEFAULT_BASE_URL).rstrip("/")


def build_rf_headers(api_token: str) -> dict[str, str]:
    """Build Recorded Future request headers (``X-RFToken`` + JSON negotiation).

    The token is stripped so trailing/leading whitespace or newlines (common
    when pasting credentials) can never produce a malformed ``X-RFToken`` header
    (httpx/h11 reject header values containing newlines). Single chokepoint
    shared by the tool builder and the connectivity test.
    """
    return {
        "X-RFToken": (api_token or "").strip(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def client(timeout: int = 30) -> httpx.AsyncClient:
    """Return a fresh async HTTP client for a single Recorded Future call."""
    return egress_client(timeout=timeout)


def clamp(limit: Any, default: int = 25, hi: int = 100, lo: int = 1) -> int:
    """Clamp a caller-supplied ``limit`` into ``[lo, hi]``; fall back on garbage."""
    try:
        return max(lo, min(int(limit), hi))
    except (TypeError, ValueError):
        return default


def trunc(text: Any, max_len: int = 300) -> str:
    """Collapse whitespace and truncate ``text`` to ``max_len`` chars."""
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("\u2026" if len(text) > max_len else "")


def dt(value: Any) -> str:
    """Trim an ISO timestamp to second precision for display."""
    return str(value or "")[:19]


def csv_list(value: str) -> list[str]:
    """Split a comma-separated string into trimmed, non-empty items."""
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def risk_emoji(label: Any) -> str:
    """Return the indicator emoji for a Recorded Future criticality label."""
    return _RISK_EMOJI.get(str(label or "").strip().lower(), "\u26aa")


def band_from_score(score: Any) -> str:
    """Map a 0-99 (or 100) risk score to its Recorded Future criticality label.

    Used as a fallback when the API returns a raw score without a label (SOAR
    enrichment). Thresholds follow Recorded Future's documented bands.
    """
    try:
        s = int(score)
    except (TypeError, ValueError):
        return "None"
    if s >= 90:
        return "Very Malicious"
    if s >= 65:
        return "Malicious"
    if s >= 25:
        return "Suspicious"
    if s >= 1:
        return "Unusual"
    return "None"


def intel_card_url(record: dict[str, Any]) -> str:
    """Return the Recorded Future Intelligence Card URL the API supplied.

    Reads the ``intelCard`` URL Recorded Future returns when it is requested via
    the ``fields`` parameter (``DEFAULT_FIELDS`` always includes ``intelCard``).
    Returns ``""`` when the API did not supply one - it never constructs or
    guesses a portal URL, so the agent is never handed a fabricated link (the
    portal path scheme is not part of the API contract).
    """
    if not isinstance(record, dict):
        return ""
    intel = record.get("intelCard")
    if isinstance(intel, str) and intel.startswith("http"):
        return intel
    return ""


def format_risk_block(risk: dict[str, Any], *, max_evidence: int = 6) -> list[str]:
    """Render an entity ``risk`` object into compact human-readable lines.

    Handles the Connect API shape (``criticalityLabel`` / ``riskString`` /
    ``evidenceDetails``) used by lookup and search responses.
    """
    if not isinstance(risk, dict) or not risk:
        return ["Risk: no Recorded Future risk data available"]
    score = risk.get("score", "?")
    label = risk.get("criticalityLabel") or band_from_score(risk.get("score"))
    rule_count = risk.get("rules")
    risk_string = risk.get("riskString")
    summary = risk.get("riskSummary")
    head = f"Risk: {risk_emoji(label)} {score}/99 ({label})"
    if risk_string:
        head += f" - {risk_string} rules triggered"
    elif rule_count is not None:
        head += f" - {rule_count} rules triggered"
    lines = [head]
    if summary and summary != risk_string:
        lines.append(f"  {trunc(summary, 200)}")
    evidence = risk.get("evidenceDetails")
    if isinstance(evidence, list) and evidence:
        # Surface the most critical rules first.
        ordered = sorted(
            evidence,
            key=lambda e: e.get("criticality", 0) if isinstance(e, dict) else 0,
            reverse=True,
        )
        lines.append("Triggered risk rules:")
        for ev in ordered[:max_evidence]:
            if not isinstance(ev, dict):
                continue
            lines.append(
                f"  {risk_emoji(ev.get('criticalityLabel'))} "
                f"{ev.get('rule', '?')} - {trunc(ev.get('evidenceString'), 200)}"
            )
        if len(evidence) > max_evidence:
            lines.append(f"  ... and {len(evidence) - max_evidence} more rule(s)")
    return lines


def format_related_entities(record: dict[str, Any], *, max_groups: int = 6) -> str:
    """Summarise the ``relatedEntities`` block of a lookup response, if present."""
    related = record.get("relatedEntities")
    if not isinstance(related, list) or not related:
        return ""
    parts: list[str] = []
    for group in related[:max_groups]:
        if not isinstance(group, dict):
            continue
        rtype = group.get("type", "Related")
        entities = group.get("entities")
        if not isinstance(entities, list) or not entities:
            continue
        names = []
        for item in entities[:5]:
            ent = item.get("entity") if isinstance(item, dict) else None
            name = ent.get("name") if isinstance(ent, dict) else None
            if name:
                names.append(str(name))
        if names:
            parts.append(f"  {rtype}: {', '.join(names)}")
    if not parts:
        return ""
    return "Related entities:\n" + "\n".join(parts)


def format_timestamps(record: dict[str, Any]) -> str:
    """Render the ``timestamps`` block (first/last seen) of a lookup response."""
    ts = record.get("timestamps")
    if not isinstance(ts, dict):
        return ""
    first = dt(ts.get("firstSeen"))
    last = dt(ts.get("lastSeen"))
    if not first and not last:
        return ""
    return f"Seen: first {first or 'N/A'} | last {last or 'N/A'}"
