# SPDX-License-Identifier: Apache-2.0

"""Feedly integration - open-source intelligence feeds + Feedly for Threat Intelligence.

Full-fledged Feedly (https://feedly.com) Enterprise API integration for the
agentic OSINT / CTI loop: read the team's curated intelligence streams, search
across Feedly's AI-enriched article corpus, and (in the threat intelligence
slice) enrich CVEs / threat actors / malware / IoCs from the Insight Cards.

Tool categories:
  - Connection: token identity + plan (``/v3/profile``).
  - Stream discovery: team folders (``/v3/enterprise/collections``), team
    boards (``/v3/enterprise/tags``), and AI Feeds (``/v3/alerts/``) - the
    three sources of stream ids every other tool consumes.
  - Content reading: collect articles from any stream
    (``/v3/streams/contents``), fetch one article with its Feedly AI
    enrichment (``/v3/entries/{id}``), and run a power search with AI layers
    (``POST /v3/search/contents``).
  - Threat intelligence (Feedly for Threat Intelligence package): entity
    autocomplete (``/v3/search/entities``), insight cards for CVEs / threat
    actors / malware / IoCs / companies (``/v3/entities/{id}``, ``.mget``),
    actor/malware relationship graphs (``/v3/ml/relationships/...``),
    STIX 2.1 IoC exports (``/v3/enterprise/ioc``), the Threat Landscape
    (``/v3/memes/vulnerabilities/en``, ``/v3/trends/...``), the Vulnerability
    Agent (``/v3/trends/vulnerability-dashboard``), the Cyberattacks Agent
    (``/v3/ml/relationships/cyber-attacks/dashboard/table``), Emerging Trends
    (``/v3/ml/trend-discovery/trends``), and Ask AI (``/v3/search/rag``,
    separate add-on).
  - Team + curation: enterprise roster (``/v3/enterprise/users``), board
    saves (``PUT /v3/tags/{streamId}``), annotations (``/v3/annotations``),
    and board removal (``DELETE /v3/tags/{streamId}/{entryId}``).

Authentication is a single **API access token** (self-service Enterprise
token from https://feedly.com/i/team/api) sent as ``Authorization: Bearer``.
All calls target ``https://api.feedly.com`` (v3). Responses paginate with
``count`` + ``continuation``; timestamps are epoch milliseconds.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import (
    EmptyBearerTokenError,
    bearer_auth_headers,
    format_entity_list,
    format_pagination_hint,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "Feedly"
_SOURCE = "integration:feedly"

FEEDLY_API = "https://api.feedly.com"

# Streams API caps count at 100 articles per page.
_MAX_ARTICLE_COUNT = 100

# newerThan on /v3/streams/contents cannot be older than 31 days.
_MAX_NEWER_THAN_DAYS = 31

_SEARCH_SEVERITIES: frozenset[str] = frozenset({"LOW", "MEDIUM", "HIGH", "CRITICAL"})

# /v3/ml/relationships/{actor|malware}/{id} intervalType values.
_RELATIONSHIP_INTERVALS: tuple[str, ...] = (
    "LAST_7_DAYS",
    "LAST_30_DAYS",
    "LAST_3_MONTHS",
    "LAST_6_MONTHS",
    "LAST_1_YEAR",
    "FOREVER",
)

# Vulnerability Agent (/v3/trends/vulnerability-dashboard) period presets.
_VULN_PERIODS: tuple[str, ...] = (
    "Last7Days",
    "Last30Days",
    "Last3Months",
    "Last6Months",
)

_VULN_SORT_FIELDS: tuple[str, ...] = ("cveId", "cvssScore", "epssScore", "publishedDate")

# Cyberattacks Agent period values.
_ATTACK_PERIODS: tuple[str, ...] = (
    "Last24Hours",
    "Last7Days",
    "Last30Days",
    "Last3Months",
    "Last6Months",
    "LastYear",
)

# Emerging Trends filter vocabularies.
_TREND_GROWTH_STAGES: tuple[str, ...] = ("Exploding", "Surging", "Growing")
_TREND_SIZES: tuple[str, ...] = ("Mainstream", "Known", "Niche")
_TREND_PERIODS: tuple[str, ...] = ("Last3Months", "Last6Months", "LastYear")

# Entities .mget accepts at most 100 ids per call.
_MAX_ENTITY_IDS = 100


def build_feedly_headers(token: str) -> dict[str, str]:
    """Build Feedly request headers (Bearer token + JSON accept).

    Single credential chokepoint shared by the tool builder, the provider
    class, and the connectivity test. Raises ``EmptyBearerTokenError`` on a
    blank token so an illegal ``"Bearer "`` header can never be produced.
    """
    return bearer_auth_headers(token, extra={"Accept": "application/json"})


def _encode_path(segment: str) -> str:
    """URL-encode a stream/entry/entity id for use in a request path.

    Feedly ids embed ``/``, ``+``, ``=`` and ``:`` (e.g.
    ``enterprise/acme/category/uuid`` or base64 entry ids), so full quoting
    doubles as the path-injection guard.
    """
    return quote(segment, safe="")


def _ms_to_date(ms: Any) -> str:
    """Render a Feedly epoch-milliseconds timestamp as a UTC date-time."""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return "?"


def _hours_to_newer_than(hours: float) -> int | None:
    """Convert a lookback in hours to a Feedly ``newerThan`` epoch-ms value."""
    if not hours or hours <= 0:
        return None
    hours = min(float(hours), _MAX_NEWER_THAN_DAYS * 24)
    cutoff = datetime.now(tz=UTC) - timedelta(hours=hours)
    return int(cutoff.timestamp() * 1000)


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _article_summary_text(entry: dict) -> str:
    """Best-available short text for an article (Leo summary > summary)."""
    leo = entry.get("leoSummary") or {}
    sentences = [
        s.get("text") if isinstance(s, dict) else str(s)
        for s in (leo.get("sentences") or [])
    ]
    sentences = [s for s in sentences if s]
    if sentences:
        return " ".join(sentences[:2])
    summary = entry.get("summary") or {}
    return str(summary.get("content") or "")


def _format_article(entry: dict) -> str:
    """One compact block per article: title, source, date, id, URL, entities."""
    origin = entry.get("origin") or {}
    url = entry.get("canonicalUrl") or next(
        (a.get("href") for a in entry.get("alternate") or [] if a.get("href")),
        "",
    )
    lines = [
        f"{_trunc(entry.get('title'), 140) or '(untitled)'}",
        f"  Source: {origin.get('title') or '?'} | "
        f"Published: {_ms_to_date(entry.get('published'))}"
        + (f" | {url}" if url else ""),
        f"  entryId: {entry.get('id', '?')}",
    ]
    summary = _article_summary_text(entry)
    if summary:
        lines.append(f"  Summary: {_trunc(summary, 280)}")
    entities = entry.get("entities") or []
    if entities:
        shown = ", ".join(
            f"{e.get('label', '?')} [{e.get('type', '?')}]" for e in entities[:6]
        )
        extra = f" (+{len(entities) - 6} more)" if len(entities) > 6 else ""
        lines.append(f"  AI entities: {shown}{extra}")
    ioc = entry.get("indicatorsOfCompromise") or {}
    mention_count = len(ioc.get("mentions") or [])
    if mention_count:
        lines.append(f"  IoC mentions: {mention_count}")
    return "\n".join(lines)


def _format_cve_card(entity: dict) -> str:
    """Compact CVE Insights Card summary: scores, exploit/patch state, threats."""
    cve_id = entity.get("cveid") or entity.get("label") or entity.get("id", "?")
    lines = [f"{cve_id}" + (" [TRENDING]" if entity.get("trending") else "")]
    if entity.get("cveStatus"):
        lines.append(f"  Status: {entity['cveStatus']}")
    desc = entity.get("description")
    if desc:
        lines.append(f"  {_trunc(desc, 320)}")
    products = entity.get("vulnerableProducts") or {}
    if products:
        vendors = products.get("vendors") or (
            [products["vendor"]] if products.get("vendor") else []
        )
        shown_products = ", ".join((products.get("products") or [])[:6])
        lines.append(
            "  Affected: "
            + (", ".join(vendors) if vendors else "?")
            + (f" ({shown_products})" if shown_products else "")
        )
    cvss = entity.get("cvssV3") or {}
    score_parts = []
    if cvss.get("baseScore") is not None:
        score_parts.append(f"CVSS v3 {cvss['baseScore']}")
    if cvss.get("vectorString"):
        score_parts.append(cvss["vectorString"])
    if entity.get("cvssCategoryEstimate"):
        score_parts.append(f"Feedly AI estimate: {entity['cvssCategoryEstimate']}")
    if entity.get("epssScore") is not None:
        score_parts.append(f"EPSS {entity['epssScore']}")
    if score_parts:
        lines.append("  Scores: " + " | ".join(score_parts))
    flags = []
    exploits = entity.get("exploits") or []
    if exploits:
        flags.append(f"{len(exploits)} exploit reference(s)")
    if entity.get("overall_label"):
        flags.append(f"exploit maturity: {entity['overall_label']}")
    if entity.get("proofOfExploits"):
        flags.append("exploited in the wild")
    if entity.get("proofOfConcepts"):
        flags.append("public PoC")
    if entity.get("patched"):
        flags.append("patch available")
    if flags:
        lines.append("  Exploit/patch: " + " | ".join(flags))
    cwes = entity.get("cweIds") or entity.get("cweIdsEstimate") or []
    if cwes:
        lines.append(
            "  CWE: "
            + ", ".join(
                f"{c.get('cweID', '?')} ({_trunc(c.get('name'), 60)})" for c in cwes[:3]
            )
        )
    # "relationhsips" is NOT a typo to fix: some Feedly CVE card payloads
    # really ship the misspelled key, so both spellings are read.
    relationships = entity.get("relationships") or entity.get("relationhsips") or []
    if relationships:
        threats = []
        for rel in relationships[:8]:
            threat = rel.get("threat") or {}
            if threat.get("label"):
                threats.append(f"{threat['label']} [{threat.get('type', '?')}]")
        if threats:
            lines.append("  Linked threats: " + ", ".join(threats))
    return "\n".join(lines)


def _format_entity_card(entity: dict) -> str:
    """Compact insight-card summary for a non-CVE entity (actor/malware/...)."""
    lines = [
        f"{entity.get('label', '(unnamed)')} [{entity.get('type', '?')}]",
        f"  entityId: {entity.get('id', '?')}",
    ]
    if entity.get("aliases"):
        lines.append("  Aliases: " + ", ".join(entity["aliases"][:8]))
    if entity.get("description"):
        lines.append(f"  {_trunc(entity['description'], 320)}")
    if entity.get("malwareTypes"):
        lines.append("  Malware types: " + ", ".join(entity["malwareTypes"][:5]))
    if entity.get("countryOfOrigin"):
        lines.append(f"  Origin: {entity['countryOfOrigin']}")
    stats = entity.get("stats") or {}
    if stats.get("numArticles") is not None:
        lines.append(f"  Article mentions: {stats['numArticles']}")
    return "\n".join(lines)


def _summarize_stix_bundle(bundle: dict) -> list[str]:
    """Summarize a Feedly STIX 2.1 IoC bundle: counts by type + IoC values."""
    objects = bundle.get("objects") or []
    if not objects:
        return []
    counts: dict[str, int] = {}
    for obj in objects:
        obj_type = obj.get("type", "?")
        counts[obj_type] = counts.get(obj_type, 0) + 1
    lines = [
        "STIX bundle: "
        + ", ".join(f"{count} {t}" for t, count in sorted(counts.items()))
    ]
    observable_types = {
        "ipv4-addr",
        "ipv6-addr",
        "domain-name",
        "url",
        "file",
        "email-addr",
    }
    iocs: list[str] = []
    for obj in objects:
        obj_type = obj.get("type", "")
        if obj_type in observable_types and obj.get("value"):
            iocs.append(f"{obj['value']} ({obj_type})")
        elif obj_type == "indicator" and obj.get("pattern"):
            iocs.append(_trunc(obj["pattern"], 120))
    if iocs:
        shown = iocs[:40]
        lines.append("IoCs:")
        lines.extend(f"  {ioc}" for ioc in shown)
        if len(iocs) > len(shown):
            lines.append(f"  (+{len(iocs) - len(shown)} more)")
    reports = [o for o in objects if o.get("type") in {"report", "grouping"}]
    if reports:
        lines.append("Source reports:")
        for report in reports[:8]:
            refs = report.get("external_references") or []
            source_url = next((r.get("url") for r in refs if r.get("url")), "")
            lines.append(
                f"  {_trunc(report.get('name'), 120)}"
                + (f" - {source_url}" if source_url else "")
            )
        if len(reports) > 8:
            lines.append(f"  (+{len(reports) - 8} more)")
    return lines


def _parse_link_continuation(link_header: str) -> str:
    """Extract the continuation token from a ``Link: <...>; rel=next`` header.

    Only the segment whose ``rel`` parameter is exactly ``next`` (quoted or
    bare, possibly space-separated with other relations) is considered - a
    substring check would misfire on URLs that merely contain "next" or on
    other relations like ``rel=prev``.
    """
    if not link_header:
        return ""
    for part in link_header.split(","):
        url_part, _, params = part.partition(";")
        rel_values: set[str] = set()
        for param in params.split(";"):
            name, _, value = param.partition("=")
            if name.strip().lower() == "rel":
                rel_values.update(value.strip().strip('"').lower().split())
        if "next" not in rel_values:
            continue
        url = url_part.strip().strip("<>")
        tokens = parse_qs(urlsplit(url).query).get("continuation")
        if tokens and tokens[0]:
            return tokens[0]
    return ""


# ---------------------------------------------------------------------------
# Provider class
# ---------------------------------------------------------------------------


class FeedlyIntegration(BaseIntegration):
    """Feedly open-source intelligence / threat intelligence integration."""

    provider_name = "feedly"
    display_name = "Feedly"
    icon = "Rss"
    description = (
        "Connect Feedly to read team folders, boards, and AI Feeds, search "
        "AI-enriched articles, and use Feedly for Threat Intelligence: CVE / "
        "threat actor / malware insight cards, STIX IoC exports, and the "
        "threat landscape dashboards."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        try:
            headers = build_feedly_headers(token)
        except EmptyBearerTokenError:
            return False
        try:
            async with egress_client(timeout=15) as client:
                resp = await client.get(f"{FEEDLY_API}/v3/profile", headers=headers)
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def get_user_info(self, token: str) -> dict:
        headers = build_feedly_headers(token)
        async with egress_client(timeout=15) as client:
            resp = await client.get(f"{FEEDLY_API}/v3/profile", headers=headers)
            resp.raise_for_status()
            return resp.json()


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Feedly tools. ``token`` is the Feedly Enterprise API access token."""
    api_key = (token or "").strip()
    if not api_key:
        return []

    headers = build_feedly_headers(api_key)

    def _client(timeout: int = 30) -> httpx.AsyncClient:
        return egress_client(timeout=timeout)

    # ══════════════════════════════════════════════════════════════════════
    # 0. CONNECTION
    # ══════════════════════════════════════════════════════════════════════

    async def get_feedly_profile() -> str:
        """Validate the Feedly connection and report the token identity."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{FEEDLY_API}/v3/profile",
                _PROVIDER,
                "get profile",
                headers=headers,
            )
            if err:
                return err
            data = resp.json() or {}
        parts = [
            f"Connected to Feedly as {data.get('fullName') or data.get('email') or data.get('id', '?')}."
        ]
        details = []
        if data.get("email"):
            details.append(f"email: {data['email']}")
        if data.get("enterpriseName"):
            details.append(f"team: {data['enterpriseName']}")
        if data.get("subscriptionPaymentProvider") or data.get("product"):
            details.append(f"plan: {data.get('product') or 'enterprise'}")
        if details:
            parts.append("  " + " | ".join(details))
        return "\n".join(parts)

    # ══════════════════════════════════════════════════════════════════════
    # 1. STREAM DISCOVERY
    # ══════════════════════════════════════════════════════════════════════

    async def list_feedly_folders(include_archived: bool = False) -> str:
        """List team folders (with their feeds) and the folder stream ids."""
        params = {"includeArchived": "true"} if include_archived else {}
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{FEEDLY_API}/v3/enterprise/collections",
                _PROVIDER,
                "list team folders",
                headers=headers,
                params=params,
            )
            if err:
                return err
            folders = resp.json() or []

        def _fmt(folder: dict) -> str:
            feeds = folder.get("feeds") or []
            flags = []
            if folder.get("private"):
                flags.append("private")
            if folder.get("collectionArchived"):
                flags.append("archived")
            shown = ", ".join(_trunc(f.get("title"), 40) for f in feeds[:5])
            extra = f" (+{len(feeds) - 5} more)" if len(feeds) > 5 else ""
            return (
                f"  {folder.get('label', '(unnamed)')}"
                + (f" [{', '.join(flags)}]" if flags else "")
                + f"\n    streamId: {folder.get('id', '?')}"
                + f"\n    {len(feeds)} feed(s)"
                + (f": {shown}{extra}" if feeds else "")
            )

        return format_entity_list(
            folders,
            _fmt,
            noun="team folder",
            empty_msg="No team folders found in this Feedly account.",
        )

    async def list_feedly_boards() -> str:
        """List team boards and the board stream ids."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{FEEDLY_API}/v3/enterprise/tags",
                _PROVIDER,
                "list team boards",
                headers=headers,
            )
            if err:
                return err
            boards = resp.json() or []

        def _fmt(board: dict) -> str:
            return (
                f"  {board.get('label', '(unnamed)')}"
                + (" [archived]" if board.get("tagArchived") else "")
                + f"\n    streamId: {board.get('id', '?')}"
                + f"\n    {board.get('totalEntries', 0)} article(s)"
                + (
                    f" | last added: {_ms_to_date(board['lastEntryAdded'])}"
                    if board.get("lastEntryAdded")
                    else ""
                )
            )

        return format_entity_list(
            boards,
            _fmt,
            noun="team board",
            empty_msg="No team boards found in this Feedly account.",
        )

    async def list_feedly_ai_feeds() -> str:
        """List AI Feeds (saved AI-powered searches) and their stream ids."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{FEEDLY_API}/v3/alerts/",
                _PROVIDER,
                "list AI Feeds",
                headers=headers,
            )
            if err:
                return err
            data = resp.json() or {}
        feeds = (data.get("enterpriseAlerts") or []) + (data.get("userAlerts") or [])

        def _fmt(feed: dict) -> str:
            layers = feed.get("layers") or []
            terms: list[str] = []
            for layer in layers:
                for part in layer.get("parts") or []:
                    label = part.get("label") or part.get("text")
                    if label:
                        terms.append(str(label))
            shown = ", ".join(terms[:6])
            extra = f" (+{len(terms) - 6} more)" if len(terms) > 6 else ""
            return (
                f"  {feed.get('label', '(unnamed)')}"
                + f"\n    streamId: {feed.get('feedId', '?')}"
                + (f"\n    Watching: {shown}{extra}" if terms else "")
                + (
                    f"\n    ~{feed['velocity']} article(s)/day"
                    if feed.get("velocity")
                    else ""
                )
            )

        return format_entity_list(
            feeds,
            _fmt,
            noun="AI Feed",
            empty_msg="No AI Feeds found in this Feedly account.",
        )

    # ══════════════════════════════════════════════════════════════════════
    # 2. CONTENT READING
    # ══════════════════════════════════════════════════════════════════════

    async def get_feedly_stream_articles(
        stream_id: str,
        count: int = 20,
        newer_than_hours: float = 0,
        continuation: str = "",
    ) -> str:
        """Collect articles from a folder / board / AI Feed / raw feed stream."""
        stream_id = str(stream_id or "").strip()
        if not stream_id:
            return (
                "Error: stream_id is required (find one with list_feedly_folders, "
                "list_feedly_boards, or list_feedly_ai_feeds)."
            )
        params: dict[str, Any] = {
            "streamId": stream_id,
            "count": max(1, min(int(count or 20), _MAX_ARTICLE_COUNT)),
        }
        newer_than = _hours_to_newer_than(newer_than_hours)
        if newer_than is not None:
            params["newerThan"] = newer_than
        if continuation:
            params["continuation"] = str(continuation).strip()
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{FEEDLY_API}/v3/streams/contents",
                _PROVIDER,
                f"collect articles from {stream_id}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        items = data.get("items") or []
        out = format_entity_list(
            items,
            _format_article,
            noun="article",
            empty_msg=f"No articles found in stream {stream_id} for this window.",
        )
        out += format_pagination_hint(data.get("continuation"), param_name="continuation")
        return out

    async def get_feedly_article(entry_id: str) -> str:
        """Fetch one article with its full Feedly AI enrichment."""
        entry_id = str(entry_id or "").strip()
        if not entry_id:
            return "Error: entry_id is required (from a stream/search result)."
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{FEEDLY_API}/v3/entries/{_encode_path(entry_id)}",
                _PROVIDER,
                "get article",
                headers=headers,
                expected_statuses={404},
            )
            if resp is not None and resp.status_code == 404:
                return f"No article found for entryId {entry_id}."
            if err:
                return err
            payload = resp.json() or []
        entry = payload[0] if isinstance(payload, list) and payload else payload
        if not isinstance(entry, dict) or not entry:
            return f"No article found for entryId {entry_id}."
        lines = [_format_article(entry)]
        topics = entry.get("commonTopics") or []
        if topics:
            lines.append(
                "Topics: "
                + ", ".join(t.get("label", "?") for t in topics[:8])
            )
        boards = entry.get("categories") or []
        if boards:
            lines.append(
                "In folders/boards: "
                + ", ".join(c.get("label", "?") for c in boards[:8])
            )
        ioc = entry.get("indicatorsOfCompromise") or {}
        mentions = ioc.get("mentions") or []
        if mentions:
            shown = ", ".join(
                f"{m.get('text', '?')} ({m.get('type', '?')})" for m in mentions[:15]
            )
            extra = f" (+{len(mentions) - 15} more)" if len(mentions) > 15 else ""
            lines.append(f"IoCs mentioned: {shown}{extra}")
        nav = entry.get("attackNavigator") or {}
        if nav.get("ttpCount"):
            lines.append(
                f"MITRE ATT&CK Navigator layer: {nav.get('ttpCount')} TTP(s)"
                + (f" - {nav['url']}" if nav.get("url") else "")
            )
        full = entry.get("fullContent") or ""
        if full:
            lines.append(f"Content (truncated): {_trunc(full, 1200)}")
        return "\n".join(lines)

    async def search_feedly_articles(
        query: str = "",
        entity_ids: list[str] | None = None,
        stream_ids: list[str] | None = None,
        severity: str = "",
        language: str = "",
        count: int = 10,
        newer_than_hours: float = 0,
        continuation: str = "",
    ) -> str:
        """Power search across Feedly content with AI match layers."""
        query = str(query or "").strip()
        entity_ids = [str(e).strip() for e in (entity_ids or []) if str(e or "").strip()]
        stream_ids = [str(s).strip() for s in (stream_ids or []) if str(s or "").strip()]
        if not query and not entity_ids:
            return (
                "Error: provide a text 'query' and/or 'entity_ids' "
                "(resolve names to entity ids with search_feedly_entities)."
            )
        layers: list[dict[str, Any]] = []
        parts: list[dict[str, str]] = [{"id": e} for e in entity_ids]
        if query:
            parts.append({"text": query})
        layers.append({"type": "matches", "parts": parts, "salience": "about"})
        severity = str(severity or "").strip().upper()
        if severity:
            if severity not in _SEARCH_SEVERITIES:
                return (
                    "Error: severity must be one of "
                    f"{', '.join(sorted(_SEARCH_SEVERITIES))}."
                )
            layers.append({"type": "security", "severities": [severity]})
        language = str(language or "").strip().lower()
        if language:
            layers.append({"type": "language", "languages": [language]})
        body: dict[str, Any] = {"layers": layers}
        if stream_ids:
            body["source"] = {
                "items": [{"type": "stream", "id": s} for s in stream_ids]
            }
        params: dict[str, Any] = {"count": max(1, min(int(count or 10), _MAX_ARTICLE_COUNT))}
        newer_than = _hours_to_newer_than(newer_than_hours)
        if newer_than is not None:
            params["newerThan"] = newer_than
        if continuation:
            params["continuation"] = str(continuation).strip()
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{FEEDLY_API}/v3/search/contents",
                _PROVIDER,
                "search articles",
                headers=headers,
                params=params,
                json=body,
            )
            if err:
                return err
            data = resp.json() or {}
        items = data.get("items") or []
        out = format_entity_list(
            items,
            _format_article,
            noun="matching article",
            empty_msg="No articles matched this search.",
        )
        out += format_pagination_hint(data.get("continuation"), param_name="continuation")
        return out

    # ══════════════════════════════════════════════════════════════════════
    # 3. THREAT INTELLIGENCE - ENTITIES & INSIGHT CARDS
    # ══════════════════════════════════════════════════════════════════════

    async def search_feedly_entities(query: str, count: int = 10) -> str:
        """Resolve names / IoCs to Feedly entity ids (autocomplete)."""
        query = str(query or "").strip()
        if not query:
            return "Error: query is required (a name, CVE id, IoC, or keyword)."
        params = {"query": query, "count": max(1, min(int(count or 10), 25))}
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{FEEDLY_API}/v3/search/entities",
                _PROVIDER,
                "search entities",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        entities = (
            data
            if isinstance(data, list)
            else data.get("entities") or data.get("items") or []
        )

        def _fmt(entity: dict) -> str:
            return (
                f"  {entity.get('label', '(unnamed)')} [{entity.get('type', '?')}]"
                f"\n    entityId: {entity.get('id', '?')}"
                + (
                    f"\n    {_trunc(entity.get('description'), 140)}"
                    if entity.get("description")
                    else ""
                )
            )

        return format_entity_list(
            entities,
            _fmt,
            noun="matching entity",
            empty_msg=f"No Feedly entities matched '{query}'.",
        )

    async def lookup_feedly_entity(entity_id: str, with_stats: bool = False) -> str:
        """Insight card for any Feedly entity (actor, malware, IoC, company)."""
        entity_id = str(entity_id or "").strip()
        if not entity_id:
            return (
                "Error: entity_id is required (e.g. 'nlp/f/entity/gz:ta:...' from "
                "search_feedly_entities, or a plain CVE id like 'CVE-2021-26084')."
            )
        params = {"withStats": "true"} if with_stats else {}
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{FEEDLY_API}/v3/entities/{_encode_path(entity_id)}",
                _PROVIDER,
                "lookup entity",
                headers=headers,
                params=params,
                expected_statuses={404},
            )
            if resp is not None and resp.status_code == 404:
                return f"No Feedly entity found for id {entity_id}."
            if err:
                return err
            entity = resp.json() or {}
        if not entity:
            return f"No Feedly entity found for id {entity_id}."
        is_cve = bool(entity.get("cveid")) or entity_id.upper().startswith("CVE-")
        lines = [_format_cve_card(entity) if is_cve else _format_entity_card(entity)]
        stats = entity.get("stats") or {}
        refs = stats.get("referenceEntries") or {}
        if refs:
            ref_counts = ", ".join(
                f"{len(v)} {k}" for k, v in refs.items() if isinstance(v, list) and v
            )
            if ref_counts:
                lines.append(f"References: {ref_counts}")
        return "\n".join(lines)

    async def get_feedly_cve(cve_id: str, with_stats: bool = False) -> str:
        """CVE Insights Card: CVSS/EPSS, exploits, patches, linked threats."""
        cve_id = str(cve_id or "").strip().upper()
        if not cve_id.startswith("CVE-"):
            return "Error: cve_id must be a CVE identifier like 'CVE-2021-26084'."
        return await lookup_feedly_entity(cve_id, with_stats=with_stats)

    async def get_feedly_entities(entity_ids: list[str]) -> str:
        """Bulk entity/CVE metadata lookup (up to 100 ids in one call)."""
        entity_ids = [str(e).strip() for e in (entity_ids or []) if str(e or "").strip()]
        if not entity_ids:
            return (
                "Error: entity_ids is required (a list of Feedly entity ids "
                "and/or CVE ids, max 100)."
            )
        if len(entity_ids) > _MAX_ENTITY_IDS:
            return f"Error: at most {_MAX_ENTITY_IDS} entity ids per call."
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{FEEDLY_API}/v3/entities/.mget",
                _PROVIDER,
                "bulk entity lookup",
                headers=headers,
                json=entity_ids,
            )
            if err:
                return err
            entities = resp.json() or []
        if not isinstance(entities, list):
            entities = [entities]

        def _fmt(entity: dict) -> str:
            is_cve = bool(entity.get("cveid"))
            block = _format_cve_card(entity) if is_cve else _format_entity_card(entity)
            return "\n".join("  " + line for line in block.splitlines())

        return format_entity_list(
            entities,
            _fmt,
            noun="entity",
            empty_msg="None of the requested entity ids were found.",
        )

    async def get_feedly_entity_relationships(
        entity_type: str,
        entity_id: str,
        interval: str = "FOREVER",
    ) -> str:
        """Relationship graph (actors, CVEs, TTPs) for an actor or malware."""
        entity_type = str(entity_type or "").strip().lower()
        if entity_type not in {"actor", "malware"}:
            return "Error: entity_type must be 'actor' or 'malware'."
        entity_id = str(entity_id or "").strip()
        if not entity_id:
            return (
                "Error: entity_id is required (a Feedly entity id like "
                "'nlp/f/entity/gz:ta:...' or 'nlp/f/entity/gz:mal:...')."
            )
        interval = str(interval or "FOREVER").strip().upper()
        if interval not in _RELATIONSHIP_INTERVALS:
            return (
                "Error: interval must be one of "
                f"{', '.join(_RELATIONSHIP_INTERVALS)}."
            )
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{FEEDLY_API}/v3/ml/relationships/{entity_type}/"
                f"{_encode_path(entity_id)}",
                _PROVIDER,
                f"get {entity_type} relationships",
                headers=headers,
                params={"intervalType": interval},
                expected_statuses={404},
            )
            if resp is not None and resp.status_code == 404:
                return f"No relationship data found for {entity_type} {entity_id}."
            if err:
                return err
            data = resp.json() or {}
        lines = [f"Relationships for {entity_type} {entity_id} ({interval}):"]

        def _entity_line(item: dict) -> str:
            entity = item.get("entity") or item
            label = entity.get("label", "?")
            eid = entity.get("id", "")
            count = item.get("count") or item.get("articleCount")
            return (
                f"  {label}"
                + (f" - {count} article(s)" if count is not None else "")
                + (f" [{eid}]" if eid else "")
            )

        for key, noun in (
            ("actors", "Threat actors"),
            ("malwares", "Malware families"),
            ("malware", "Malware families"),
            ("vulnerabilities", "Vulnerabilities"),
            ("industries", "Targeted industries"),
        ):
            items = data.get(key) or []
            if items:
                lines.append(f"{noun} ({len(items)}):")
                lines.extend(_entity_line(i) for i in items[:15])
                if len(items) > 15:
                    lines.append(f"  (+{len(items) - 15} more)")
        ttps = data.get("ttps") or []
        if ttps:
            lines.append(f"MITRE ATT&CK TTPs ({len(ttps)}):")
            for ttp in ttps[:15]:
                mitigations = ttp.get("mitigations") or []
                lines.append(
                    f"  {ttp.get('mitreId', '?')} {ttp.get('technique', '?')}"
                    + (f" - trend: {ttp['trend']}" if ttp.get("trend") else "")
                    + (
                        " - mitigations: "
                        + ", ".join(
                            f"{m.get('mitreId', '?')} {m.get('label', '')}".strip()
                            for m in mitigations[:3]
                        )
                        if mitigations
                        else ""
                    )
                )
            if len(ttps) > 15:
                lines.append(f"  (+{len(ttps) - 15} more)")
        if len(lines) == 1:
            return (
                f"No relationships found for {entity_type} {entity_id} "
                f"in interval {interval}. Try interval='FOREVER'."
            )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 4. THREAT INTELLIGENCE - IOCS, LANDSCAPE & DASHBOARDS
    # ══════════════════════════════════════════════════════════════════════

    async def get_feedly_stream_iocs(
        stream_id: str,
        newer_than_hours: float = 24,
        count: int = 20,
        continuation: str = "",
    ) -> str:
        """Export IoCs from a stream as a summarized STIX 2.1 bundle."""
        stream_id = str(stream_id or "").strip()
        if not stream_id:
            return (
                "Error: stream_id is required (find one with list_feedly_folders, "
                "list_feedly_boards, or list_feedly_ai_feeds)."
            )
        params: dict[str, Any] = {
            "streamId": stream_id,
            "count": max(1, min(int(count or 20), _MAX_ARTICLE_COUNT)),
        }
        newer_than = _hours_to_newer_than(newer_than_hours)
        if newer_than is not None:
            params["newerThan"] = newer_than
        if continuation:
            params["continuation"] = str(continuation).strip()
        async with _client(timeout=90) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{FEEDLY_API}/v3/enterprise/ioc",
                _PROVIDER,
                f"collect IoCs from {stream_id}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            bundle = resp.json() or {}
            # The IoC endpoint returns its continuation in the Link header.
            next_token = _parse_link_continuation(resp.headers.get("Link", ""))
        lines = _summarize_stix_bundle(bundle)
        if not lines:
            return (
                f"No IoCs found in stream {stream_id} for this window. "
                "Try a larger newer_than_hours."
            )
        out = "\n".join(lines)
        out += format_pagination_hint(next_token or None, param_name="continuation")
        return out

    async def get_feedly_trending_threats(
        kind: str = "vulnerabilities",
        count: int = 10,
    ) -> str:
        """Threat Landscape dashboard: trending CVEs, attackers, or new malware."""
        kind = str(kind or "vulnerabilities").strip().lower()
        if kind not in {"vulnerabilities", "threat_actors", "new_malware"}:
            return (
                "Error: kind must be 'vulnerabilities', 'threat_actors', "
                "or 'new_malware'."
            )
        if kind == "vulnerabilities":
            url = f"{FEEDLY_API}/v3/memes/vulnerabilities/en"
            params: dict[str, Any] = {"count": max(1, min(int(count or 10), 25))}
        elif kind == "threat_actors":
            url = f"{FEEDLY_API}/v3/trends/threat-actors"
            params = {}
        else:
            url = f"{FEEDLY_API}/v3/trends/new-malwares"
            params = {}
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                url,
                _PROVIDER,
                f"get trending {kind}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json() or {}
        if kind == "vulnerabilities":
            memes = data.get("memes") or []

            def _fmt_meme(meme: dict) -> str:
                meta = meme.get("vulnerabilitiesMetadata") or {}
                summary = meme.get("summary") or []
                return (
                    f"  #{meme.get('rank', '?')} {meme.get('label', '?')}"
                    + (f" | CVSS {meta['cvss']}" if meta.get("cvss") else "")
                    + (
                        f" | vendor: {', '.join(meta['vendors'][:3])}"
                        if meta.get("vendors")
                        else ""
                    )
                    + (f"\n    {_trunc(summary[0], 220)}" if summary else "")
                    + f"\n    {len(meme.get('items') or [])} related article(s)"
                )

            return format_entity_list(
                memes,
                _fmt_meme,
                noun="trending vulnerability",
                empty_msg="No trending vulnerabilities right now.",
            )
        items = data.get("items") or []
        entity_key = "actor" if kind == "threat_actors" else "new-malware"

        def _fmt_trend(item: dict) -> str:
            entity = item.get(entity_key) or {}
            return (
                f"  {entity.get('label', '?')}"
                f"\n    entityId: {entity.get('id', '?')}"
                f"\n    {len(item.get('entryIds') or [])} related article(s) "
                "(fetch with get_feedly_article)"
            )

        return format_entity_list(
            items,
            _fmt_trend,
            noun=(
                "trending threat actor"
                if kind == "threat_actors"
                else "trending new malware"
            ),
            empty_msg=f"No trending {kind.replace('_', ' ')} right now.",
        )

    async def query_feedly_vulnerability_dashboard(
        period: str = "Last7Days",
        trending: bool = False,
        exploited: bool = False,
        poc: bool = False,
        weaponized: bool = False,
        in_cisa_kev: bool = False,
        cvss_min: float = 0,
        sort_field: str = "publishedDate",
        sort_order: str = "desc",
        count: int = 20,
        continuation: str = "",
    ) -> str:
        """Query the Vulnerability Agent with structured filters."""
        period = str(period or "Last7Days").strip()
        if period not in _VULN_PERIODS:
            return f"Error: period must be one of {', '.join(_VULN_PERIODS)}."
        sort_field = str(sort_field or "publishedDate").strip()
        if sort_field not in _VULN_SORT_FIELDS:
            return f"Error: sort_field must be one of {', '.join(_VULN_SORT_FIELDS)}."
        sort_order = str(sort_order or "desc").strip().lower()
        if sort_order not in {"asc", "desc"}:
            return "Error: sort_order must be 'asc' or 'desc'."
        layers: list[dict[str, Any]] = [
            {"filters": [{"field": "period", "value": {"type": period}}]}
        ]
        # Boolean signal filters live in ONE layer so they are OR'd together
        # (filters within a layer are OR'd, layers are AND'd).
        signal_filters = [
            {"field": field, "value": True}
            for field, enabled in (
                ("exploited", exploited),
                ("poc", poc),
                ("weaponized", weaponized),
                ("inCisaKev", in_cisa_kev),
            )
            if enabled
        ]
        if signal_filters:
            layers.append({"filters": signal_filters})
        if trending:
            layers.append({"filters": [{"field": "trending", "value": True}]})
        if cvss_min and float(cvss_min) > 0:
            layers.append(
                {
                    "filters": [
                        {"field": "cvssScore", "value": {"gte": float(cvss_min)}}
                    ]
                }
            )
        body: dict[str, Any] = {
            "layers": layers,
            "sort": {"field": sort_field, "order": sort_order},
            "count": max(1, min(int(count or 20), _MAX_ARTICLE_COUNT)),
        }
        if continuation:
            body["continuation"] = continuation
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{FEEDLY_API}/v3/trends/vulnerability-dashboard",
                _PROVIDER,
                "query vulnerability dashboard",
                headers=headers,
                json=body,
            )
            if err:
                return err
            data = resp.json() or {}
        vulns = data.get("vulnerabilities") or []
        out = format_entity_list(
            vulns,
            lambda v: "\n".join("  " + line for line in _format_cve_card(v).splitlines()),
            noun="vulnerability",
            total=data.get("total"),
            empty_msg="No vulnerabilities matched these filters.",
        )
        next_token = data.get("continuation")
        if next_token:
            out += format_pagination_hint(str(next_token), param_name="continuation")
        return out

    async def query_feedly_cyberattacks(
        period: str = "Last30Days",
        victim_country: str = "",
        victim_industry_id: str = "",
        threat_actor: str = "",
        malware_family: str = "",
        attack_type: str = "",
        continuation: str = "",
    ) -> str:
        """Query the Cyberattacks Agent for structured attack records."""
        period = str(period or "Last30Days").strip()
        if period not in _ATTACK_PERIODS:
            return f"Error: period must be one of {', '.join(_ATTACK_PERIODS)}."
        layers: list[dict[str, Any]] = []
        for field, value in (
            ("victimCountry", str(victim_country or "").strip().upper()),
            ("victimIndustry", str(victim_industry_id or "").strip()),
            ("threatActor", str(threat_actor or "").strip()),
            ("malwareFamily", str(malware_family or "").strip()),
            ("attackType", str(attack_type or "").strip()),
        ):
            if value:
                layers.append({"filters": [{"field": field, "value": value}]})
        body: dict[str, Any] = {
            "layers": layers,
            "period": {"type": period},
        }
        if continuation:
            body["continuation"] = str(continuation).strip()
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{FEEDLY_API}/v3/ml/relationships/cyber-attacks/dashboard/table",
                _PROVIDER,
                "query cyberattacks agent",
                headers=headers,
                json=body,
            )
            if err:
                return err
            data = resp.json() or {}
        attacks = data.get("attacks") or data.get("items") or data.get("rows") or []

        def _fmt_attack(attack: dict) -> str:
            victim = attack.get("victim") or {}
            victim_label = (
                victim.get("label")
                or victim.get("name")
                or attack.get("victimName", "?")
            )
            actor = attack.get("threatActor") or attack.get("actor") or {}
            malware = attack.get("malwareFamily") or attack.get("malware") or {}
            parts = [f"  {victim_label}"]
            details = []
            if attack.get("attackType"):
                details.append(f"type: {attack['attackType']}")
            if isinstance(actor, dict) and actor.get("label"):
                details.append(f"actor: {actor['label']}")
            elif isinstance(actor, str) and actor:
                details.append(f"actor: {actor}")
            if isinstance(malware, dict) and malware.get("label"):
                details.append(f"malware: {malware['label']}")
            if attack.get("victimCountry"):
                details.append(f"country: {attack['victimCountry']}")
            if attack.get("victimIndustry"):
                industry = attack["victimIndustry"]
                label = (
                    industry.get("label") if isinstance(industry, dict) else industry
                )
                if label:
                    details.append(f"industry: {label}")
            date = attack.get("date") or attack.get("attackDate")
            if date:
                details.append(f"date: {_ms_to_date(date)}")
            if details:
                parts.append("    " + " | ".join(details))
            if attack.get("id"):
                parts.append(f"    attackId: {attack['id']}")
            return "\n".join(parts)

        out = format_entity_list(
            attacks,
            _fmt_attack,
            noun="cyber attack",
            total=data.get("total"),
            empty_msg="No cyber attacks matched these filters for this period.",
        )
        next_token = data.get("continuation")
        if next_token:
            out += format_pagination_hint(str(next_token), param_name="continuation")
        return out

    async def get_feedly_emerging_trends(
        period: str = "Last6Months",
        growth_stages: list[str] | None = None,
        sizes: list[str] | None = None,
        sort: str = "-growth",
        new_only: bool = False,
        count: int = 20,
    ) -> str:
        """Discover emerging market/tech trends from Feedly AI Trend Discovery."""
        period = str(period or "Last6Months").strip()
        if period not in _TREND_PERIODS:
            return f"Error: period must be one of {', '.join(_TREND_PERIODS)}."
        growth_stages = [
            str(g).strip().capitalize() for g in (growth_stages or []) if str(g).strip()
        ]
        invalid = [g for g in growth_stages if g not in _TREND_GROWTH_STAGES]
        if invalid:
            return (
                "Error: growth_stages values must be among "
                f"{', '.join(_TREND_GROWTH_STAGES)}."
            )
        sizes = [str(s).strip().capitalize() for s in (sizes or []) if str(s).strip()]
        invalid = [s for s in sizes if s not in _TREND_SIZES]
        if invalid:
            return f"Error: sizes values must be among {', '.join(_TREND_SIZES)}."
        sort = str(sort or "-growth").strip()
        if sort not in {"-growth", "growth", "-size", "size"}:
            return "Error: sort must be one of -growth, growth, -size, size."
        body: dict[str, Any] = {
            "filters": [
                {
                    "type": "growth",
                    "values": growth_stages or list(_TREND_GROWTH_STAGES),
                },
                {"type": "size", "values": sizes or list(_TREND_SIZES)},
            ],
            "period": period,
        }
        params = {
            "count": max(1, min(int(count or 20), 200)),
            "sort": sort,
            "newOnly": "true" if new_only else "false",
        }
        async with _client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{FEEDLY_API}/v3/ml/trend-discovery/trends",
                _PROVIDER,
                "get emerging trends",
                headers=headers,
                params=params,
                json=body,
            )
            if err:
                return err
            data = resp.json() or {}
        trends = (
            data
            if isinstance(data, list)
            else data.get("trends") or data.get("items") or []
        )

        def _fmt_trend(trend: dict) -> str:
            entity = trend.get("entity") or trend
            details = []
            if trend.get("growth"):
                details.append(f"growth: {trend['growth']}")
            if trend.get("size"):
                details.append(f"size: {trend['size']}")
            return (
                f"  {entity.get('label', '(unnamed)')}"
                + (f" ({' | '.join(details)})" if details else "")
                + (
                    f"\n    entityId: {entity.get('id')}"
                    if entity.get("id")
                    else ""
                )
                + (
                    f"\n    {_trunc(entity.get('description'), 180)}"
                    if entity.get("description")
                    else ""
                )
            )

        return format_entity_list(
            trends,
            _fmt_trend,
            noun="emerging trend",
            empty_msg="No emerging trends matched these filters.",
        )

    async def ask_feedly_ai(
        question: str,
        stream_ids: list[str] | None = None,
        entity_id: str = "",
        newer_than_hours: float = 0,
    ) -> str:
        """Ask Feedly AI a question, answered from Feedly's article corpus."""
        question = str(question or "").strip()
        if not question:
            return "Error: question is required."
        stream_ids = [str(s).strip() for s in (stream_ids or []) if str(s or "").strip()]
        body: dict[str, Any] = {"query": question}
        if stream_ids:
            body["source"] = {
                "items": [{"type": "stream", "id": s} for s in stream_ids]
            }
        entity_id = str(entity_id or "").strip()
        if entity_id:
            # Unlike /v3/entities/{id} (which accepts plain CVE ids), Ask AI
            # requires the full entity path for CVEs - normalize so agents can
            # pass the same 'CVE-...' id they got from the other tools.
            if entity_id.upper().startswith("CVE-"):
                entity_id = f"vulnerability/m/entity/{entity_id.upper()}"
            body["entityIds"] = [entity_id]
        params: dict[str, Any] = {}
        newer_than = _hours_to_newer_than(newer_than_hours)
        if newer_than is not None:
            params["newerThan"] = newer_than
        async with _client(timeout=120) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{FEEDLY_API}/v3/search/rag",
                _PROVIDER,
                "ask Feedly AI",
                headers=headers,
                params=params,
                json=body,
                expected_statuses={403},
            )
            if resp is not None and resp.status_code == 403:
                return (
                    "Feedly Ask AI is not enabled for this account - it requires "
                    "the separate 'Ask AI API' add-on (contact sales@feedly.com). "
                    "Use search_feedly_articles instead."
                )
            if err:
                return err
            data = resp.json() or {}
        answer = str(data.get("answer") or "").strip()
        if not answer:
            return "Feedly AI returned no answer for this question."
        lines = [answer]
        items = data.get("items") or []
        if items:
            lines.append("")
            lines.append(f"Sources ({len(items)} article(s)):")
            for item in items[:8]:
                origin = item.get("origin") or {}
                lines.append(
                    f"  {_trunc(item.get('title'), 120)}"
                    + (f" - {origin['title']}" if origin.get("title") else "")
                    + f" (entryId: {item.get('id', '?')})"
                )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 5. TEAM
    # ══════════════════════════════════════════════════════════════════════

    async def list_feedly_team_users() -> str:
        """List the enterprise team roster with roles and activity."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{FEEDLY_API}/v3/enterprise/users",
                _PROVIDER,
                "list team users",
                headers=headers,
            )
            if err:
                return err
            users = resp.json() or []

        def _fmt_user(user: dict) -> str:
            return (
                f"  {user.get('name') or user.get('email', '?')}"
                + (f" <{user['email']}>" if user.get("email") else "")
                + f" - role: {user.get('role', '?')}"
                + (" [admin]" if user.get("isAdmin") else "")
                + (
                    f" | last active: {_ms_to_date(user['lastActivity'])}"
                    if user.get("lastActivity")
                    else ""
                )
            )

        return format_entity_list(
            users,
            _fmt_user,
            noun="team member",
            empty_msg="No users found in this Feedly enterprise account.",
        )

    # ══════════════════════════════════════════════════════════════════════
    # 6. CURATION (WRITES)
    # ══════════════════════════════════════════════════════════════════════

    async def add_feedly_article_to_board(
        board_stream_id: str,
        entry_id: str,
    ) -> str:
        """Save an article to a team board."""
        board_stream_id = str(board_stream_id or "").strip()
        entry_id = str(entry_id or "").strip()
        if not board_stream_id or not entry_id:
            return (
                "Error: board_stream_id (from list_feedly_boards) and entry_id "
                "(from a stream/search result) are both required."
            )
        async with _client() as client:
            _resp, err = await safe_request(
                client,
                "put",
                f"{FEEDLY_API}/v3/tags/{_encode_path(board_stream_id)}",
                _PROVIDER,
                "add article to board",
                headers=headers,
                json={"entryId": entry_id},
            )
            if err:
                return err
        return f"Article {entry_id} saved to board {board_stream_id}."

    async def annotate_feedly_article(entry_id: str, comment: str) -> str:
        """Add a note/annotation to an article (visible to the team)."""
        entry_id = str(entry_id or "").strip()
        comment = str(comment or "").strip()
        if not entry_id or not comment:
            return "Error: entry_id and comment are both required."
        async with _client() as client:
            _resp, err = await safe_request(
                client,
                "post",
                f"{FEEDLY_API}/v3/annotations",
                _PROVIDER,
                "annotate article",
                headers=headers,
                json={"entryId": entry_id, "comment": comment},
            )
            if err:
                return err
        return f"Annotation added to article {entry_id}."

    async def remove_feedly_article_from_board(
        board_stream_id: str,
        entry_id: str,
    ) -> str:
        """Remove an article from a team board (no undo)."""
        board_stream_id = str(board_stream_id or "").strip()
        entry_id = str(entry_id or "").strip()
        if not board_stream_id or not entry_id:
            return (
                "Error: board_stream_id (from list_feedly_boards) and entry_id "
                "are both required."
            )
        async with _client() as client:
            _resp, err = await safe_request(
                client,
                "delete",
                f"{FEEDLY_API}/v3/tags/{_encode_path(board_stream_id)}/"
                f"{_encode_path(entry_id)}",
                _PROVIDER,
                "remove article from board",
                headers=headers,
            )
            if err:
                return err
        return f"Article {entry_id} removed from board {board_stream_id}."

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("get_feedly_profile", get_feedly_profile)
    executor.register("list_feedly_folders", list_feedly_folders)
    executor.register("list_feedly_boards", list_feedly_boards)
    executor.register("list_feedly_ai_feeds", list_feedly_ai_feeds)
    executor.register("get_feedly_stream_articles", get_feedly_stream_articles)
    executor.register("get_feedly_article", get_feedly_article)
    executor.register("search_feedly_articles", search_feedly_articles)
    executor.register("search_feedly_entities", search_feedly_entities)
    executor.register("lookup_feedly_entity", lookup_feedly_entity)
    executor.register("get_feedly_cve", get_feedly_cve)
    executor.register("get_feedly_entities", get_feedly_entities)
    executor.register(
        "get_feedly_entity_relationships", get_feedly_entity_relationships
    )
    executor.register("get_feedly_stream_iocs", get_feedly_stream_iocs)
    executor.register("get_feedly_trending_threats", get_feedly_trending_threats)
    executor.register(
        "query_feedly_vulnerability_dashboard", query_feedly_vulnerability_dashboard
    )
    executor.register("query_feedly_cyberattacks", query_feedly_cyberattacks)
    executor.register("get_feedly_emerging_trends", get_feedly_emerging_trends)
    executor.register("ask_feedly_ai", ask_feedly_ai)
    executor.register("list_feedly_team_users", list_feedly_team_users)
    executor.register("add_feedly_article_to_board", add_feedly_article_to_board)
    executor.register("annotate_feedly_article", annotate_feedly_article)
    executor.register(
        "remove_feedly_article_from_board", remove_feedly_article_from_board
    )

    return [
        ToolDefinition(
            name="list_feedly_folders",
            description=(
                "List the team's Feedly folders (curated groups of RSS/news "
                "feeds) with their stream ids and member feeds. Use this first "
                "to discover which streamId to pass to "
                "get_feedly_stream_articles or get_feedly_stream_iocs. Folder "
                "stream ids look like 'enterprise/TEAM/category/UUID'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "include_archived": {
                        "type": "boolean",
                        "default": False,
                        "description": "Also include archived folders",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_feedly_boards",
            description=(
                "List the team's Feedly boards (manually curated collections "
                "of saved articles) with their stream ids and article counts. "
                "Board stream ids look like 'enterprise/TEAM/tag/UUID' and "
                "are what add_feedly_article_to_board expects. Use "
                "get_feedly_stream_articles with a board streamId to read the "
                "saved articles."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_feedly_ai_feeds",
            description=(
                "List the team's Feedly AI Feeds (saved AI-powered searches "
                "that continuously match new articles, e.g. 'ransomware "
                "targeting healthcare'). Returns each feed's label, watched "
                "entities/terms, and its streamId "
                "('feed/https://feedly.com/f/alert/UUID') for use with "
                "get_feedly_stream_articles or get_feedly_stream_iocs."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_feedly_stream_articles",
            description=(
                "Collect the latest articles from any Feedly stream: a team "
                "folder, board, AI Feed, or a raw feed id "
                "('feed/RSS_URL'). Returns per article the title, source, "
                "publish date, entryId (needed by get_feedly_article and the "
                "board tools), URL, AI summary, and detected AI entities. Use "
                "newer_than_hours to only fetch recent articles (max 31 days) "
                "and the continuation token to page beyond 'count'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "stream_id": {
                        "type": "string",
                        "description": (
                            "Stream id from list_feedly_folders / list_feedly_boards "
                            "/ list_feedly_ai_feeds, e.g. "
                            "'enterprise/acme/category/UUID'"
                        ),
                    },
                    "count": {
                        "type": "integer",
                        "default": 20,
                        "description": "Articles per page (1-100)",
                    },
                    "newer_than_hours": {
                        "type": "number",
                        "default": 0,
                        "description": (
                            "Only articles from the last N hours (0 = no filter; "
                            "max 744 = 31 days)"
                        ),
                    },
                    "continuation": {
                        "type": "string",
                        "description": "Continuation token from a previous page",
                    },
                },
                "required": ["stream_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_feedly_article",
            description=(
                "Fetch one Feedly article by entryId with the full AI "
                "enrichment: summary, detected entities (threat actors, "
                "malware, CVEs, companies, MITRE ATT&CK techniques), topics, "
                "IoC mentions, the ATT&CK Navigator layer link, the "
                "folders/boards it belongs to, and truncated content. Use "
                "after get_feedly_stream_articles or search_feedly_articles "
                "to deep-dive one result."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entry_id": {
                        "type": "string",
                        "description": "Article entryId from a stream or search result",
                    },
                },
                "required": ["entry_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_feedly_articles",
            description=(
                "Search Feedly's AI-enriched article corpus. Combine a "
                "free-text query with Feedly entity ids (from "
                "search_feedly_entities) for precise matches on threat "
                "actors, malware, CVEs, or companies - entity matches beat "
                "keyword matches because Feedly AI disambiguates mentions. "
                "Optionally restrict to specific streams, a security severity "
                "(LOW/MEDIUM/HIGH/CRITICAL), or a language code (e.g. 'en'). "
                "Returns articles with entryIds; page with the continuation "
                "token."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-text search terms, e.g. 'supply chain attack'",
                    },
                    "entity_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Feedly entity ids to match, e.g. "
                            "'nlp/f/entity/gz:ta:UUID' (use search_feedly_entities)"
                        ),
                    },
                    "stream_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Restrict the search to these stream ids",
                    },
                    "severity": {
                        "type": "string",
                        "description": (
                            "Only articles Feedly AI rates at this security "
                            "severity: LOW, MEDIUM, HIGH, or CRITICAL "
                            "(case-insensitive)"
                        ),
                    },
                    "language": {
                        "type": "string",
                        "description": "ISO language code filter, e.g. 'en'",
                    },
                    "count": {
                        "type": "integer",
                        "default": 10,
                        "description": "Articles per page (1-100)",
                    },
                    "newer_than_hours": {
                        "type": "number",
                        "default": 0,
                        "description": (
                            "Only articles from the last N hours (0 = no filter; "
                            "max 744 = 31 days)"
                        ),
                    },
                    "continuation": {
                        "type": "string",
                        "description": "Continuation token from a previous page",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_feedly_entities",
            description=(
                "Resolve a name, CVE id, IoC, or keyword to Feedly entity ids "
                "(autocomplete). Feedly entities are disambiguated AI concepts: "
                "threat actors (gz:ta:...), malware families (gz:mal:...), "
                "companies (gz:org:...), topics, and vulnerabilities. Use the "
                "returned entityId with search_feedly_articles, "
                "lookup_feedly_entity, get_feedly_entity_relationships, or "
                "ask_feedly_ai for precise, disambiguated results."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Name or keyword to resolve, e.g. 'Scattered Spider', "
                            "'Log4Shell', 'Microsoft'"
                        ),
                    },
                    "count": {
                        "type": "integer",
                        "default": 10,
                        "description": "Max suggestions (1-25)",
                    },
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="lookup_feedly_entity",
            description=(
                "Fetch the Feedly Insight Card for any entity: threat actor, "
                "malware family, IoC, company, or CVE. Returns description, "
                "aliases, and type-specific intelligence (for CVEs the full "
                "CVE card: CVSS/EPSS, exploit and patch state, linked "
                "threats). Accepts a Feedly entity id "
                "('nlp/f/entity/gz:ta:...') or a plain CVE id. Set with_stats "
                "for reference/chatter article counts. For CVEs prefer "
                "get_feedly_cve; for actor/malware TTPs and IoCs use "
                "get_feedly_entity_relationships."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": (
                            "Feedly entity id from search_feedly_entities, or a "
                            "CVE id like 'CVE-2021-26084'"
                        ),
                    },
                    "with_stats": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include reference/chatter article stats",
                    },
                },
                "required": ["entity_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_feedly_cve",
            description=(
                "Fetch the Feedly CVE Insights Card for one CVE: description, "
                "affected vendors/products, CVSS v3 score + vector, Feedly AI "
                "severity estimate, EPSS score, exploit maturity "
                "(PoC/weaponized/exploited in the wild), patch state, CWEs, "
                "and linked malware/threat actors. Use for vulnerability "
                "triage questions like 'is CVE-X exploited?'. Set with_stats "
                "for vendor advisories and threat intel report references."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cve_id": {
                        "type": "string",
                        "description": "CVE identifier, e.g. 'CVE-2021-26084'",
                    },
                    "with_stats": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Include references (vendor advisories, TI reports) "
                            "and chatter stats"
                        ),
                    },
                },
                "required": ["cve_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_feedly_entities",
            description=(
                "Bulk-fetch metadata for up to 100 Feedly entities and/or CVE "
                "ids in ONE call (POST /v3/entities/.mget). Much cheaper than "
                "calling get_feedly_cve/lookup_feedly_entity in a loop - use "
                "this whenever you need insight cards for more than 2-3 "
                "entities (e.g. scoring a list of CVEs from a scan)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Feedly entity ids and/or CVE ids, e.g. "
                            "['CVE-2021-26084', 'nlp/f/entity/gz:mal:...'] (max 100)"
                        ),
                    },
                },
                "required": ["entity_ids"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_feedly_entity_relationships",
            description=(
                "Fetch the relationship graph for a threat actor or malware "
                "family: associated actors/malware, exploited CVEs, targeted "
                "industries, and MITRE ATT&CK TTPs with technique ids and "
                "recommended mitigations. Use after resolving the entity id "
                "with search_feedly_entities. Interval bounds the evidence "
                "window (default FOREVER = all time)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity_type": {
                        "type": "string",
                        "description": (
                            "Whether the entity is a threat actor or malware: "
                            "'actor' or 'malware' (case-insensitive)"
                        ),
                    },
                    "entity_id": {
                        "type": "string",
                        "description": (
                            "Feedly entity id, e.g. 'nlp/f/entity/gz:ta:...' or "
                            "'nlp/f/entity/gz:mal:...'"
                        ),
                    },
                    "interval": {
                        "type": "string",
                        "default": "FOREVER",
                        "description": (
                            "Evidence time window: "
                            + ", ".join(_RELATIONSHIP_INTERVALS)
                            + " (case-insensitive)"
                        ),
                    },
                },
                "required": ["entity_type", "entity_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_feedly_stream_iocs",
            description=(
                "Export the Indicators of Compromise referenced in a Feedly "
                "stream (folder, board, or AI Feed) as a summarized STIX 2.1 "
                "bundle: IoC values (IPs, domains, URLs, hashes) plus their "
                "context (source reports, malware, actors, CVEs, TTPs). Use "
                "for 'pull the latest IoCs from our ransomware feed' "
                "workflows. newer_than_hours bounds the article window (max "
                "744 = 31 days); page with the continuation token."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "stream_id": {
                        "type": "string",
                        "description": (
                            "Stream id from list_feedly_folders / "
                            "list_feedly_boards / list_feedly_ai_feeds"
                        ),
                    },
                    "newer_than_hours": {
                        "type": "number",
                        "default": 24,
                        "description": (
                            "Only articles from the last N hours (max 744 = 31 days)"
                        ),
                    },
                    "count": {
                        "type": "integer",
                        "default": 20,
                        "description": "Articles to process per page (1-100)",
                    },
                    "continuation": {
                        "type": "string",
                        "description": "Continuation token from a previous page",
                    },
                },
                "required": ["stream_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_feedly_trending_threats",
            description=(
                "Read the Feedly Threat Landscape dashboard: the currently "
                "trending vulnerabilities (up to 25 CVEs with CVSS + vendor "
                "context), trending threat actors, or trending new malware "
                "(each with entity ids and related article entryIds). Updated "
                "hourly. Use for 'what is hot right now' situational "
                "awareness; use query_feedly_vulnerability_dashboard for "
                "filtered CVE queries."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "default": "vulnerabilities",
                        "description": (
                            "Which Threat Landscape section to read: "
                            "'vulnerabilities', 'threat_actors', or "
                            "'new_malware' (case-insensitive)"
                        ),
                    },
                    "count": {
                        "type": "integer",
                        "default": 10,
                        "description": "Max trending vulnerabilities (1-25; ignored for the other kinds)",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="query_feedly_vulnerability_dashboard",
            description=(
                "Query the Feedly Vulnerability Agent with structured filters "
                "- the CVE research workhorse. Filter by period, trending "
                "state, exploitation signals (exploited in the wild, public "
                "PoC, weaponized, CISA KEV membership - these OR together), "
                "and minimum CVSS score; sort by cveId, cvssScore, epssScore, "
                "or publishedDate. Returns full CVE cards. Example: 'CVSS>=9 "
                "CVEs exploited or in KEV in the last 7 days'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": list(_VULN_PERIODS),
                        "default": "Last7Days",
                        "description": "Publication window",
                    },
                    "trending": {
                        "type": "boolean",
                        "default": False,
                        "description": "Only CVEs currently trending in Feedly",
                    },
                    "exploited": {
                        "type": "boolean",
                        "default": False,
                        "description": "Exploited in the wild (ORs with other signals)",
                    },
                    "poc": {
                        "type": "boolean",
                        "default": False,
                        "description": "Public proof-of-concept available",
                    },
                    "weaponized": {
                        "type": "boolean",
                        "default": False,
                        "description": "Weaponized exploit available",
                    },
                    "in_cisa_kev": {
                        "type": "boolean",
                        "default": False,
                        "description": "Listed in the CISA Known Exploited Vulnerabilities catalog",
                    },
                    "cvss_min": {
                        "type": "number",
                        "default": 0,
                        "description": "Minimum CVSS v3 base score (0 = no filter)",
                    },
                    "sort_field": {
                        "type": "string",
                        "enum": list(_VULN_SORT_FIELDS),
                        "default": "publishedDate",
                        "description": "Sort field",
                    },
                    "sort_order": {
                        "type": "string",
                        "default": "desc",
                        "description": (
                            "Sort direction: 'asc' or 'desc' (case-insensitive)"
                        ),
                    },
                    "count": {
                        "type": "integer",
                        "default": 20,
                        "description": "Results per page (1-100)",
                    },
                    "continuation": {
                        "type": "string",
                        "description": "Continuation token from a previous page",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="query_feedly_cyberattacks",
            description=(
                "Query the Feedly Cyberattacks Agent for structured cyber "
                "attack records (victim, attack type, threat actor, malware, "
                "date, source articles). Filter by victim country (ISO "
                "3166-1 alpha-2 like 'US'), victim industry (Feedly topic id "
                "like 'nlp/f/topic/4017'), threat actor, malware family, "
                "and/or attack type (e.g. 'Ransomware', "
                "'DataBreachesAndExfiltration'). Use for questions like "
                "'ransomware attacks on US healthcare in the last 30 days'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": list(_ATTACK_PERIODS),
                        "default": "Last30Days",
                        "description": "Attack date window",
                    },
                    "victim_country": {
                        "type": "string",
                        "description": "ISO 3166-1 alpha-2 country code, e.g. 'US'",
                    },
                    "victim_industry_id": {
                        "type": "string",
                        "description": (
                            "Feedly industry topic id, e.g. 'nlp/f/topic/4017' "
                            "(resolve with search_feedly_entities)"
                        ),
                    },
                    "threat_actor": {
                        "type": "string",
                        "description": "Threat actor entity id or name",
                    },
                    "malware_family": {
                        "type": "string",
                        "description": "Malware family entity id or name",
                    },
                    "attack_type": {
                        "type": "string",
                        "description": (
                            "Attack type, e.g. 'Ransomware', "
                            "'DataBreachesAndExfiltration'"
                        ),
                    },
                    "continuation": {
                        "type": "string",
                        "description": "Continuation token from a previous page",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_feedly_emerging_trends",
            description=(
                "Discover emerging trends from Feedly AI Trend Discovery: "
                "topics gaining momentum, classified by growth stage "
                "(Exploding/Surging/Growing) and audience size "
                "(Mainstream/Known/Niche). Use for market/technology "
                "landscape questions ('what is emerging in cybersecurity?'), "
                "not for CVE or attack data (use the dashboards for those)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": list(_TREND_PERIODS),
                        "default": "Last6Months",
                        "description": "Trend evaluation window",
                    },
                    "growth_stages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Growth stages to include (default: all): "
                            + ", ".join(_TREND_GROWTH_STAGES)
                            + " (case-insensitive)"
                        ),
                    },
                    "sizes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Audience sizes to include (default: all): "
                            + ", ".join(_TREND_SIZES)
                            + " (case-insensitive)"
                        ),
                    },
                    "sort": {
                        "type": "string",
                        "enum": ["-growth", "growth", "-size", "size"],
                        "default": "-growth",
                        "description": "Sort ('-' prefix = descending)",
                    },
                    "new_only": {
                        "type": "boolean",
                        "default": False,
                        "description": "Only freshly detected trends",
                    },
                    "count": {
                        "type": "integer",
                        "default": 20,
                        "description": "Max trends to return (1-200)",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="ask_feedly_ai",
            description=(
                "Ask Feedly AI a natural-language question answered from "
                "Feedly's article corpus (RAG) with source citations. "
                "Optionally scope to specific streams, one entity id (CVE / "
                "actor / malware), or a recency window. Requires the 'Ask AI "
                "API' add-on on the Feedly plan - if unavailable, the tool "
                "says so; fall back to search_feedly_articles. Use for "
                "synthesis questions ('what threat actors target "
                "aeronautics?'); use the structured dashboards for lists."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The natural-language question to answer",
                    },
                    "stream_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Prioritize articles from these stream ids",
                    },
                    "entity_id": {
                        "type": "string",
                        "description": (
                            "Focus entity: a Feedly entity id "
                            "('nlp/f/entity/gz:ta:...') or a plain CVE id like "
                            "'CVE-2021-44228' (auto-converted to the "
                            "'vulnerability/m/entity/...' path Ask AI requires)"
                        ),
                    },
                    "newer_than_hours": {
                        "type": "number",
                        "default": 0,
                        "description": (
                            "Only consider articles from the last N hours "
                            "(0 = no filter; max 744 = 31 days)"
                        ),
                    },
                },
                "required": ["question"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_feedly_team_users",
            description=(
                "List the Feedly enterprise team roster: name, email, role "
                "(admin/member), and last activity. Use to identify teammates "
                "when routing intelligence or checking who curates boards."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="add_feedly_article_to_board",
            description=(
                "Save an article to a Feedly team board (triage/curation "
                "write). Boards are shared, manually curated collections - "
                "saving makes the article visible to everyone following the "
                "board. Get the board_stream_id from list_feedly_boards and "
                "the entry_id from a stream or search result."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "board_stream_id": {
                        "type": "string",
                        "description": (
                            "Board stream id, e.g. 'enterprise/acme/tag/UUID'"
                        ),
                    },
                    "entry_id": {
                        "type": "string",
                        "description": "Article entryId to save",
                    },
                },
                "required": ["board_stream_id", "entry_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="annotate_feedly_article",
            description=(
                "Add a text note (annotation) to a Feedly article, visible to "
                "the team. Use to leave triage context ('confirmed relevant "
                "to our stack, escalating'). HTML tags are stripped by Feedly."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entry_id": {
                        "type": "string",
                        "description": "Article entryId to annotate",
                    },
                    "comment": {
                        "type": "string",
                        "description": "The note text",
                    },
                },
                "required": ["entry_id", "comment"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="remove_feedly_article_from_board",
            description=(
                "Remove an article from a Feedly team board. This is "
                "permanent - there is no undo, and the article disappears "
                "from the board for the whole team (the article itself is "
                "not deleted). Only use when explicitly asked to un-save or "
                "clean up a board."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "board_stream_id": {
                        "type": "string",
                        "description": (
                            "Board stream id, e.g. 'enterprise/acme/tag/UUID'"
                        ),
                    },
                    "entry_id": {
                        "type": "string",
                        "description": "Article entryId to remove from the board",
                    },
                },
                "required": ["board_stream_id", "entry_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="get_feedly_profile",
            description=(
                "Validate the Feedly connection and report the token identity "
                "(user, email, team). Use first if other Feedly tools fail "
                "with authentication errors."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
    ]
