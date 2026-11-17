# SPDX-License-Identifier: Apache-2.0

"""ThreatQuotient (ThreatQ) integration – Threat Intelligence Platform (TIP).

Full-fledged ThreatQ integration covering the Threat Library curation loop —
search → enrich → curate → relate → operationalize — designed as a
best-in-class building block for an agentic CTI team:

  - Indicators (*the IOC lifecycle*): list/search (basic cross-object search,
    wildcard value search, and the SOLR-style advanced Threat Library query),
    get with full context, create (type/status resolved by name), update,
    status triage (activate / expire / whitelist — i.e. deprecate), expiration
    bumps, and manual score overrides
  - Events, Adversaries, Attachments (Files), Signatures: list / get / create
  - Files: attachments listed with their download URL, downloaded as
    files of the run (a malware-locked one stays locked, a malware-typed one
    is stored only as an encrypted ZIP), added from any file of the run; an
    export feed generated and stored as a file
  - STIX-style objects (attack_pattern, campaign, course_of_action,
    exploit_target, identity, incident, intrusion_set, malware, report, tool,
    ttp, vulnerability, asset, tasks, investigations): generic list / get /
    create / update
  - Object context (*enrichment*): attributes, comments, sources, tags on ANY
    object type, plus relationships (link/pivot between any two objects)
  - Threat Library data collections (saved searches), scoring configuration,
    export feed definitions, platform tags and intelligence sources
  - A consolidated ``manage_threatq_object`` tool for the destructive long
    tail (delete object, remove tag/attribute/comment, unlink, watchlist,
    delete data collection)

Authentication
--------------
ThreatQ is self-hosted and authenticates with an **OAuth2 password grant**:
``POST {base_url}/api/token`` with ``{grant_type: "password", client_id,
email, password}`` returns a short-lived (~1 h) bearer token used on every
subsequent request. ``integration_tools._get_token`` assembles the compound
``base_url||client_id||email||password`` token this builder parses (the
password is the final, greedy segment). Minted bearer tokens are cached
in-process via the shared app-token cache (mirroring defender / sentinel /
crowdstrike) so a multi-tool agentic run does not re-mint on every call.

Exports ``threatq_api_base()`` and ``validate_connection()`` shared with the
connectivity test.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.file_delivery import (
    FetchedFile,
    PersistFileFn,
    deliver_file,
    deliver_live_artefact,
    fetch_file,
    file_content_type,
    live_artefact_links,
    read_pipe_file,
    untrusted_file_facts,
    uploaded_file_answer,
)
from app.integrations.http import (
    app_token_cache_key,
    format_entity_list,
    get_cached_app_token,
    parse_compound_token,
    safe_request,
    set_cached_app_token,
)
from app.services.content_safety import single_line
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_PROVIDER = "ThreatQ"
_SOURCE = "integration:threatq"
_DEFAULT_TIMEOUT = 30
_FILE_TIMEOUT = 120
_MAX_FILE_BYTES = 100 * 1024 * 1024

_AUTH_ERROR = (
    "Error: Failed to authenticate with ThreatQ. Check that the base URL, "
    "OAuth client ID, API user email, and password are correct, that the "
    "appliance is reachable, and that the API user's role grants API access."
)

# Canonical Threat Library object collections (URL path segments). The ThreatQ
# API exposes the same generic context endpoints (attributes, comments,
# sources, tags, watchlist, relationships) for every collection.
_SYSTEM_OBJECTS: frozenset[str] = frozenset(
    {"indicators", "events", "adversaries", "attachments", "signatures"}
)
_STIX_OBJECTS: frozenset[str] = frozenset(
    {
        "attack_pattern",
        "campaign",
        "course_of_action",
        "exploit_target",
        "identity",
        "incident",
        "intrusion_set",
        "malware",
        "report",
        "tool",
        "ttp",
        "vulnerability",
        "asset",
        "tasks",
        "investigations",
    }
)
_ALL_OBJECTS: frozenset[str] = _SYSTEM_OBJECTS | _STIX_OBJECTS

# Friendly aliases → canonical collection names, so the agent can say
# "files" or "indicator" without memorising ThreatQ's exact path segments.
_OBJECT_ALIASES: dict[str, str] = {
    "indicator": "indicators",
    "event": "events",
    "adversary": "adversaries",
    "attachment": "attachments",
    "file": "attachments",
    "files": "attachments",
    "signature": "signatures",
    "attack_patterns": "attack_pattern",
    "campaigns": "campaign",
    "courses_of_action": "course_of_action",
    "exploit_targets": "exploit_target",
    "identities": "identity",
    "incidents": "incident",
    "intrusion_sets": "intrusion_set",
    "malwares": "malware",
    "reports": "report",
    "tools": "tool",
    "ttps": "ttp",
    "vulnerabilities": "vulnerability",
    "assets": "asset",
    "task": "tasks",
    "investigation": "investigations",
}

_OBJECT_TYPE_HINT = ", ".join(sorted(_ALL_OBJECTS))


def _normalize_object_type(object_type: str) -> str | None:
    """Resolve a caller-supplied object type to a canonical collection name."""
    key = (object_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key in _ALL_OBJECTS:
        return key
    return _OBJECT_ALIASES.get(key)


def _object_type_error(object_type: str) -> str:
    return (
        f"Error: unknown ThreatQ object type '{object_type}'. "
        f"Valid types: {_OBJECT_TYPE_HINT}."
    )


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text)
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _clamp(value: Any, default: int, lo: int = 1, hi: int = 200) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(lo, min(value, hi))


def _is_malware_type(type_name: str) -> bool:
    """Whether an attachment type holds live malware ('Malware Sample', ...)."""
    return "malware" in (type_name or "").lower()


def _score_str(score: Any) -> str:
    """Render an indicator score (dict with generated/manual, or a plain value)."""
    if isinstance(score, dict):
        gen = score.get("generated_score")
        manual = score.get("manual_score")
        if manual is not None:
            return f"{manual} (manual, generated {gen})"
        return str(gen) if gen is not None else "?"
    return str(score) if score is not None else "?"


def _data_list(resp: httpx.Response) -> tuple[list[dict[str, Any]], int | None]:
    """Unwrap ThreatQ's ``{"data": [...], "total": N}`` list envelope."""
    body = resp.json() or {}
    if isinstance(body, list):
        return body, None
    data = body.get("data")
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        data = []
    total = body.get("total")
    return data, total if isinstance(total, int) else None


def _data_obj(resp: httpx.Response) -> dict[str, Any]:
    """Unwrap ThreatQ's ``{"data": {...}}`` single-object envelope."""
    body = resp.json() or {}
    if isinstance(body, dict):
        data = body.get("data", body)
        if isinstance(data, list):
            return data[0] if data else {}
        if isinstance(data, dict):
            return data
    return {}


# ---------------------------------------------------------------------------
# Auth + URL helpers (shared with integration_testing / integrations.py)
# ---------------------------------------------------------------------------


def threatq_api_base(base_url: str) -> str:
    """Return the ThreatQ REST API base URL (``{base_url}/api``)."""
    return f"{base_url.rstrip('/')}/api"


def _bearer(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def _mint_token(
    base_url: str, client_id: str, email: str, password: str
) -> tuple[str | None, int]:
    """Exchange ThreatQ credentials for a bearer token via the password grant.

    Returns ``(access_token, expires_in_seconds)`` on success or ``(None, 0)``
    on failure. Tokens are short-lived (~1 h).
    """
    url = f"{threatq_api_base(base_url)}/token"
    try:
        async with egress_client(timeout=20) as client:
            resp = await client.post(
                url,
                json={
                    "grant_type": "password",
                    "client_id": client_id,
                    "email": email,
                    "password": password,
                },
                headers={"Accept": "application/json"},
            )
            if resp.is_success:
                data = resp.json()
                return data.get("access_token"), int(data.get("expires_in") or 3600)
            logger.warning(
                "ThreatQ token request failed (%s): %s",
                resp.status_code,
                resp.text[:300],
            )
    except httpx.HTTPError as exc:
        logger.warning("ThreatQ token request error: %s", format_exception_message(exc))
    return None, 0


async def _get_access_token(
    base_url: str, client_id: str, email: str, password: str
) -> str | None:
    """Return a valid bearer token, minting + caching in-process as needed."""
    cache_key = app_token_cache_key("threatq", base_url, client_id, email, password)
    cached = get_cached_app_token(cache_key)
    if cached:
        return cached
    access, expires_in = await _mint_token(base_url, client_id, email, password)
    if access:
        set_cached_app_token(cache_key, access, expires_in)
    return access


async def validate_connection(
    base_url: str, client_id: str, email: str, password: str
) -> tuple[bool, str]:
    """Verify ThreatQ credentials via a token exchange + a whoami call."""
    if not (base_url and client_id and email and password):
        return False, (
            "Missing base URL, client ID, email, or password. Set the base URL, "
            "client ID, and email in extra config and the password in the API "
            "Key field."
        )
    access, _ = await _mint_token(base_url, client_id, email, password)
    if not access:
        return False, (
            "Authentication failed — could not obtain a ThreatQ API token. "
            "Check the base URL, client ID, email, and password."
        )
    try:
        async with egress_client(timeout=15) as client:
            resp = await client.get(
                f"{threatq_api_base(base_url)}/users/current",
                headers=_bearer(access),
            )
            if resp.is_success:
                user = (resp.json() or {}).get("data") or {}
                display = user.get("display_name") or user.get("email") or email
                return True, f"Connected to ThreatQ at {base_url} as {display}."
            return False, (
                f"Token obtained but the whoami call failed "
                f"(HTTP {resp.status_code}). Check the API user's permissions."
            )
    except httpx.HTTPError as exc:
        return False, f"Could not reach ThreatQ at {base_url}: {exc}"


# ---------------------------------------------------------------------------
# Integration class
# ---------------------------------------------------------------------------


class ThreatQIntegration(BaseIntegration):
    """ThreatQuotient ThreatQ threat intelligence platform integration."""

    provider_name = "threatq"
    display_name = "ThreatQuotient"
    icon = "ShieldHalf"
    description = (
        "Connect a self-hosted ThreatQuotient ThreatQ appliance to search, "
        "curate, and operationalize threat intelligence."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        base_url, client_id, email, password = parse_compound_token(
            token, 4, greedy_last=True
        )
        ok, _ = await validate_connection(base_url, client_id, email, password)
        return ok

    async def get_user_info(self, token: str) -> dict:
        base_url, client_id, email, password = parse_compound_token(
            token, 4, greedy_last=True
        )
        access = await _get_access_token(base_url, client_id, email, password)
        if not access:
            return {}
        async with egress_client(timeout=15) as client:
            resp = await client.get(
                f"{threatq_api_base(base_url)}/users/current",
                headers=_bearer(access),
            )
            resp.raise_for_status()
            return (resp.json() or {}).get("data") or {}


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    persist_file_fn: PersistFileFn | None = None,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build ThreatQ tools from a ``base_url||client_id||email||password`` token.

    ``persist_file_fn`` (injected by ``integration_tools``) stores a downloaded
    attachment or export as a file of the run.

    Returns no tools only when the appliance base URL is missing (no requests
    can be built without it). Missing client_id / email / password still build
    the full tool surface — ``integration_tools._get_token`` intentionally
    returns a partial compound token so misconfiguration surfaces as a clear,
    actionable call-time error instead of the tools silently disappearing
    (mirrors sumologic / servicenow).
    """
    base_url, client_id, email, password = parse_compound_token(
        token, 4, greedy_last=True
    )
    base_url = base_url.rstrip("/")
    if not base_url:
        logger.debug(
            "ThreatQ base URL is empty - returning no tools. Configure the "
            "appliance URL (https://APPLIANCE) in extra config."
        )
        return []

    api = threatq_api_base(base_url)

    def _creds_error() -> str | None:
        """Validate credentials at call time (tools build with a partial token)."""
        if not (client_id and email and password):
            return (
                "Error: ThreatQ credentials are not configured. Set the OAuth "
                "client ID and API user email in extra config and the password "
                "in the API Key field."
            )
        return None

    async def _token() -> str | None:
        return await _get_access_token(base_url, client_id, email, password)

    async def _request(
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> tuple[httpx.Response | None, str | None]:
        """Authenticated ``safe_request`` against ``{api}{path}``."""
        # Short-circuit before attempting a token mint with empty values so a
        # misconfigured integration reliably returns the actionable error.
        creds_err = _creds_error()
        if creds_err:
            return None, creds_err
        access = await _token()
        if not access:
            return None, _AUTH_ERROR
        kwargs: dict[str, Any] = {"headers": _bearer(access)}
        if params:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body
        async with egress_client(timeout=timeout) as client:
            return await safe_request(
                client, method, f"{api}{path}", _PROVIDER, action, **kwargs
            )

    async def _file_request(
        method: str, path: str, action: str, **kwargs: Any
    ) -> tuple[httpx.Response | None, str | None]:
        """Authenticated request carrying file bytes (no JSON content type)."""
        creds_err = _creds_error()
        if creds_err:
            return None, creds_err
        access = await _token()
        if not access:
            return None, _AUTH_ERROR
        async with egress_client(timeout=_FILE_TIMEOUT) as client:
            return await safe_request(
                client,
                method,
                f"{api}{path}",
                _PROVIDER,
                action,
                headers={"Authorization": f"Bearer {access}", "Accept": "*/*"},
                **kwargs,
            )

    async def _fetch(path: str, action: str, **kwargs: Any) -> FetchedFile | str:
        """Stream a file answer (``fetch_file``) within the download cap."""
        creds_err = _creds_error()
        if creds_err:
            return creds_err
        access = await _token()
        if not access:
            return _AUTH_ERROR
        async with egress_client(timeout=_FILE_TIMEOUT) as client:
            return await fetch_file(
                client,
                "get",
                f"{api}{path}",
                provider=_PROVIDER,
                action=action,
                max_bytes=_MAX_FILE_BYTES,
                headers={"Authorization": f"Bearer {access}", "Accept": "*/*"},
                **kwargs,
            )

    # -- name → id resolution helpers (types/statuses are appliance-specific) --

    async def _resolve_named_id(
        path: str, name: str, noun: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Resolve a type/status *name* to its record via a list endpoint."""
        # Explicit high limit: the appliance's default page size could
        # otherwise truncate the list and miss the wanted name.
        resp, err = await _request(
            "get", path, f"list {noun}s", params={"limit": 500}
        )
        if err:
            return None, err
        items, _ = _data_list(resp)
        wanted = name.strip().lower()
        for item in items:
            if str(item.get("name", "")).strip().lower() == wanted:
                return item, None
        options = ", ".join(str(i.get("name")) for i in items[:30])
        return None, (
            f"Error: unknown ThreatQ {noun} '{name}'. Available: {options or 'none'}."
        )

    # ══════════════════════════════════════════════════════════════════════
    # 0. CONNECTION / IDENTITY
    # ══════════════════════════════════════════════════════════════════════

    async def whoami_threatq() -> str:
        """Validate the ThreatQ connection and report the current API user."""
        resp, err = await _request("get", "/users/current", "get current user")
        if err:
            return err
        user = _data_obj(resp)
        return (
            f"Connected to ThreatQ at {base_url}.\n"
            f"  User: {user.get('display_name', '?')} ({user.get('email', '?')}) | "
            f"id #{user.get('id', '?')}"
        )

    # ══════════════════════════════════════════════════════════════════════
    # 1. SEARCH
    # ══════════════════════════════════════════════════════════════════════

    async def search_threatq_objects(
        query: str, object_types: str = "", limit: int = 25
    ) -> str:
        """Basic cross-object search of the Threat Library by value/name."""
        params: dict[str, Any] = {
            "query": query,
            "limit": _clamp(limit, 25, hi=100),
        }
        if object_types:
            params["with"] = object_types
        resp, err = await _request(
            "get", "/search", f"search objects for '{_trunc(query, 60)}'", params=params
        )
        if err:
            return err
        items, total = _data_list(resp)

        def _fmt(o: dict) -> str:
            return (
                f"  #{o.get('id', '?')} [{o.get('type', '?')}] "
                f"{_trunc(o.get('value'), 120)}"
            )

        return format_entity_list(
            items,
            _fmt,
            noun="object",
            total=total,
            empty_msg=f"No Threat Library objects match '{query}'.",
        )

    async def search_threatq_indicators(
        value_contains: str = "",
        indicator_type: str = "",
        status: str = "",
        min_score: int = -1,
        max_score: int = -1,
        source: str = "",
        tag: str = "",
        created_after: str = "",
        created_before: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Advanced Threat Library indicator search with structured filters."""
        criteria: dict[str, Any] = {}
        if value_contains:
            criteria["value"] = {"+contains": value_contains}
        filters: list[dict[str, Any]] = []
        if indicator_type:
            filters.append({"type_name": indicator_type})
        if status:
            filters.append({"status_name": status})
        score_filter: dict[str, Any] = {}
        if min_score >= 0:
            score_filter["+gte"] = min_score
        if max_score >= 0:
            score_filter["+lte"] = max_score
        if score_filter:
            filters.append({"score": score_filter})
        if source:
            filters.append({"source_name": source})
        if tag:
            filters.append({"tags": tag})
        created: dict[str, Any] = {}
        if created_after:
            created["+gt"] = created_after
        if created_before:
            created["+lt"] = created_before
        if created:
            filters.append({"created_at": created})

        body: dict[str, Any] = {}
        if criteria:
            body["criteria"] = criteria
        if filters:
            body["filters"] = {"+and": filters}
        if not body:
            return (
                "Error: provide at least one filter (value_contains, "
                "indicator_type, status, score range, source, tag, or dates)."
            )

        resp, err = await _request(
            "post",
            "/indicators/query",
            "search indicators",
            params={
                "limit": _clamp(limit, 25, hi=200),
                "offset": max(0, int(offset or 0)),
                "sort": "-updated_at",
                "with": "type,status,score",
            },
            json_body=body,
        )
        if err:
            return err
        items, total = _data_list(resp)
        result = format_entity_list(
            items,
            _fmt_indicator,
            noun="indicator",
            total=total,
            empty_msg="No indicators match the given filters.",
        )
        if total and total > len(items) + max(0, int(offset or 0)):
            result += f"\n[More results available — pass offset={int(offset or 0) + len(items)}]"
        return result

    # ══════════════════════════════════════════════════════════════════════
    # 2. INDICATORS
    # ══════════════════════════════════════════════════════════════════════

    def _fmt_indicator(i: dict) -> str:
        itype = (i.get("type") or {}).get("name") if isinstance(i.get("type"), dict) else i.get("type")
        istatus = (
            (i.get("status") or {}).get("name")
            if isinstance(i.get("status"), dict)
            else i.get("status")
        )
        return (
            f"  #{i.get('id', '?')} {_trunc(i.get('value'), 100)} "
            f"[{itype or f'type {i.get('type_id', '?')}'}] — "
            f"status: {istatus or f'id {i.get('status_id', '?')}'} | "
            f"score: {_score_str(i.get('score'))} | "
            f"expires: {i.get('expires_at') or 'never'} | "
            f"updated: {i.get('updated_at', '?')}"
        )

    async def list_threatq_indicators(
        limit: int = 25, offset: int = 0, sort: str = "-updated_at"
    ) -> str:
        """List Threat Library indicators (paginated, newest first)."""
        resp, err = await _request(
            "get",
            "/indicators",
            "list indicators",
            params={
                "limit": _clamp(limit, 25, hi=200),
                "offset": max(0, int(offset or 0)),
                "sort": sort or "-updated_at",
                "with": "type,status,score",
            },
        )
        if err:
            return err
        items, total = _data_list(resp)
        result = format_entity_list(
            items, _fmt_indicator, noun="indicator", total=total
        )
        if total and total > len(items) + max(0, int(offset or 0)):
            result += f"\n[More results available — pass offset={int(offset or 0) + len(items)}]"
        return result

    async def get_threatq_indicator(indicator_id: int) -> str:
        """Get full details of an indicator including context and relationships."""
        resp, err = await _request(
            "get",
            f"/indicators/{int(indicator_id)}",
            f"get indicator {indicator_id}",
            params={
                "with": "type,status,score,sources,tags,attributes,adversaries,comments"
            },
        )
        if err:
            return err
        i = _data_obj(resp)
        itype = (i.get("type") or {}).get("name", "?") if isinstance(i.get("type"), dict) else i.get("type_id")
        istatus = (
            (i.get("status") or {}).get("name", "?")
            if isinstance(i.get("status"), dict)
            else i.get("status_id")
        )
        lines = [
            f"Indicator #{i.get('id', indicator_id)}: {i.get('value', '?')}",
            f"Type: {itype} | Class: {i.get('class', '?')} | Status: {istatus} | "
            f"Score: {_score_str(i.get('score'))}",
            f"Created: {i.get('created_at', '?')} | Updated: {i.get('updated_at', '?')} | "
            f"Last detected: {i.get('last_detected_at') or 'never'} | "
            f"Expires: {i.get('expires_at') or 'never'}",
        ]
        if i.get("description"):
            lines.append(f"Description: {_trunc(i.get('description'), 400)}")
        sources = i.get("sources") or []
        if sources:
            lines.append(
                "Sources: " + ", ".join(str(s.get("name", "?")) for s in sources[:10])
            )
        tags = i.get("tags") or []
        if tags:
            lines.append("Tags: " + ", ".join(str(t.get("name", "?")) for t in tags[:15]))
        attributes = i.get("attributes") or []
        if attributes:
            lines.append("Attributes:")
            for a in attributes[:15]:
                name = (a.get("attribute") or {}).get("name") or a.get("name", "?")
                lines.append(f"  - {name}: {_trunc(a.get('value'), 120)}")
        adversaries = i.get("adversaries") or []
        if adversaries:
            lines.append(
                "Adversaries: "
                + ", ".join(
                    f"#{a.get('id', '?')} {a.get('name', '?')}"
                    for a in adversaries[:10]
                )
            )
        comments = i.get("comments") or []
        if comments:
            lines.append(f"Comments: {len(comments)} (see list_threatq_object_comments)")
        return "\n".join(lines)

    async def list_threatq_indicator_types() -> str:
        """List indicator types (name, class, id) for create/search calls."""
        resp, err = await _request("get", "/indicator/types", "list indicator types")
        if err:
            return err
        items, _ = _data_list(resp)

        def _fmt(t: dict) -> str:
            return (
                f"  #{t.get('id', '?')} {t.get('name', '?')} "
                f"(class: {t.get('class', '?')})"
            )

        return format_entity_list(items, _fmt, noun="indicator type")

    async def list_threatq_indicator_statuses() -> str:
        """List indicator statuses (Active, Expired, Review, Whitelisted, custom...)."""
        resp, err = await _request(
            "get", "/indicator/statuses", "list indicator statuses"
        )
        if err:
            return err
        items, _ = _data_list(resp)

        def _fmt(s: dict) -> str:
            return (
                f"  #{s.get('id', '?')} {s.get('name', '?')} — "
                f"{_trunc(s.get('description'), 100)}"
            )

        return format_entity_list(items, _fmt, noun="indicator status")

    async def create_threatq_indicator(
        value: str,
        indicator_type: str,
        status: str = "Review",
        description: str = "",
        source: str = "",
        tlp: str = "",
    ) -> str:
        """Create an indicator; type/status names are resolved to appliance ids."""
        type_rec, err = await _resolve_named_id(
            "/indicator/types", indicator_type, "indicator type"
        )
        if err:
            return err
        status_rec, err = await _resolve_named_id(
            "/indicator/statuses", status, "indicator status"
        )
        if err:
            return err
        item: dict[str, Any] = {
            "value": value,
            "type_id": type_rec.get("id"),
            "status_id": status_rec.get("id"),
            "class": type_rec.get("class") or "network",
        }
        if description:
            item["description"] = description
        if source:
            src: dict[str, Any] = {"name": source}
            if tlp:
                src["tlp"] = {"name": tlp.upper()}
            item["sources"] = [src]
        resp, err = await _request(
            "post", "/indicators", "create indicator", json_body=[item]
        )
        if err:
            return err
        created, _ = _data_list(resp)
        rec = created[0] if created else {}
        existing = " (already existed)" if rec.get("existing") == "Y" else ""
        return (
            f"Created indicator #{rec.get('id', '?')}{existing}: {value} "
            f"[{indicator_type}] with status {status}."
        )

    async def update_threatq_indicator(
        indicator_id: int,
        value: str = "",
        description: str = "",
        indicator_type: str = "",
        status: str = "",
    ) -> str:
        """Update an indicator's value, description, type, and/or status."""
        body: dict[str, Any] = {}
        if value:
            body["value"] = value
        if description:
            body["description"] = description
        if indicator_type:
            type_rec, err = await _resolve_named_id(
                "/indicator/types", indicator_type, "indicator type"
            )
            if err:
                return err
            body["type_id"] = type_rec.get("id")
            body["class"] = type_rec.get("class") or "network"
        if status:
            status_rec, err = await _resolve_named_id(
                "/indicator/statuses", status, "indicator status"
            )
            if err:
                return err
            body["status_id"] = status_rec.get("id")
        if not body:
            return "Error: No fields provided to update."
        resp, err = await _request(
            "put",
            f"/indicators/{int(indicator_id)}",
            f"update indicator {indicator_id}",
            json_body=body,
        )
        if err:
            return err
        rec = _data_obj(resp)
        return (
            f"Updated indicator #{rec.get('id', indicator_id)}: "
            + ", ".join(sorted(body)) + " set."
        )

    async def set_threatq_indicator_status(indicator_id: int, status: str) -> str:
        """Set an indicator's status by name (triage / deprecate)."""
        status_rec, err = await _resolve_named_id(
            "/indicator/statuses", status, "indicator status"
        )
        if err:
            return err
        _, err = await _request(
            "put",
            f"/indicators/{int(indicator_id)}",
            f"set status of indicator {indicator_id}",
            json_body={"status_id": status_rec.get("id")},
        )
        if err:
            return err
        return f"Indicator #{indicator_id} status set to {status_rec.get('name')}."

    async def set_threatq_indicator_expiration(indicator_id: int, days: int) -> str:
        """Bump an indicator's expiration date by a number of days."""
        resp, err = await _request(
            "put",
            f"/indicators/{int(indicator_id)}/expiration",
            f"set expiration of indicator {indicator_id}",
            json_body={"days": int(days)},
        )
        if err:
            return err
        data = _data_obj(resp)
        return (
            f"Indicator #{indicator_id} expiration bumped by {days} day(s) — "
            f"now expires at {data.get('expires_at', '?')}."
        )

    async def set_threatq_indicator_score(indicator_id: int, score: int) -> str:
        """Set a manual score override (0-10) on an indicator."""
        score = max(0, min(int(score), 10))
        # NOTE: singular ``/indicator/{id}/scores`` — like ``/indicator/types``
        # and ``/indicator/statuses``, this metadata route uses the singular
        # prefix (the plural form 404s; see the reference ThreatQ v2 client).
        _, err = await _request(
            "put",
            f"/indicator/{int(indicator_id)}/scores",
            f"set score of indicator {indicator_id}",
            json_body={"manual_score": score},
        )
        if err:
            return err
        return (
            f"Indicator #{indicator_id} manual score set to {score}. "
            "The effective score is the highest of the manual and generated scores."
        )

    # ══════════════════════════════════════════════════════════════════════
    # 3. EVENTS
    # ══════════════════════════════════════════════════════════════════════

    def _fmt_event(e: dict) -> str:
        etype = (e.get("type") or {}).get("name") if isinstance(e.get("type"), dict) else None
        return (
            f"  #{e.get('id', '?')} {_trunc(e.get('title'), 100)} "
            f"[{etype or f'type {e.get('type_id', '?')}'}] — "
            f"happened: {e.get('happened_at', '?')} | updated: {e.get('updated_at', '?')}"
        )

    async def list_threatq_events(
        limit: int = 25, offset: int = 0, sort: str = "-happened_at"
    ) -> str:
        """List Threat Library events (spearphish, sightings, incidents...)."""
        resp, err = await _request(
            "get",
            "/events",
            "list events",
            params={
                "limit": _clamp(limit, 25, hi=200),
                "offset": max(0, int(offset or 0)),
                "sort": sort or "-happened_at",
                "with": "type",
            },
        )
        if err:
            return err
        items, total = _data_list(resp)
        return format_entity_list(items, _fmt_event, noun="event", total=total)

    async def get_threatq_event(event_id: int) -> str:
        """Get full details of an event including context and relationships."""
        resp, err = await _request(
            "get",
            f"/events/{int(event_id)}",
            f"get event {event_id}",
            params={"with": "type,sources,tags,attributes,indicators,adversaries"},
        )
        if err:
            return err
        e = _data_obj(resp)
        etype = (e.get("type") or {}).get("name", "?") if isinstance(e.get("type"), dict) else e.get("type_id")
        lines = [
            f"Event #{e.get('id', event_id)}: {e.get('title', '?')}",
            f"Type: {etype} | Happened: {e.get('happened_at', '?')} | "
            f"Created: {e.get('created_at', '?')} | Updated: {e.get('updated_at', '?')}",
        ]
        if e.get("description"):
            lines.append(f"Description: {_trunc(e.get('description'), 400)}")
        sources = e.get("sources") or []
        if sources:
            lines.append(
                "Sources: " + ", ".join(str(s.get("name", "?")) for s in sources[:10])
            )
        tags = e.get("tags") or []
        if tags:
            lines.append("Tags: " + ", ".join(str(t.get("name", "?")) for t in tags[:15]))
        indicators = e.get("indicators") or []
        if indicators:
            lines.append(f"Related indicators ({len(indicators)}):")
            for i in indicators[:10]:
                lines.append(f"  - #{i.get('id', '?')} {_trunc(i.get('value'), 90)}")
        adversaries = e.get("adversaries") or []
        if adversaries:
            lines.append(
                "Adversaries: "
                + ", ".join(
                    f"#{a.get('id', '?')} {a.get('name', '?')}"
                    for a in adversaries[:10]
                )
            )
        return "\n".join(lines)

    async def list_threatq_event_types() -> str:
        """List event types (name + id) for create_threatq_event."""
        resp, err = await _request("get", "/event/types", "list event types")
        if err:
            return err
        items, _ = _data_list(resp)

        def _fmt(t: dict) -> str:
            return f"  #{t.get('id', '?')} {t.get('name', '?')}"

        return format_entity_list(items, _fmt, noun="event type")

    async def create_threatq_event(
        title: str,
        event_type: str,
        happened_at: str = "",
        description: str = "",
        source: str = "",
    ) -> str:
        """Create an event; the event type name is resolved to an appliance id."""
        type_rec, err = await _resolve_named_id("/event/types", event_type, "event type")
        if err:
            return err
        # ThreatQ derives the event hash from title + happened_at, so always
        # send a timestamp — defaulting to now (UTC) as the schema promises.
        item: dict[str, Any] = {
            "title": title,
            "type_id": type_rec.get("id"),
            "happened_at": happened_at
            or datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        }
        if description:
            item["description"] = description
        if source:
            item["sources"] = [{"name": source}]
        resp, err = await _request("post", "/events", "create event", json_body=[item])
        if err:
            return err
        created, _ = _data_list(resp)
        rec = created[0] if created else {}
        return f"Created event #{rec.get('id', '?')}: {title} [{event_type}]."

    # ══════════════════════════════════════════════════════════════════════
    # 4. ADVERSARIES
    # ══════════════════════════════════════════════════════════════════════

    async def list_threatq_adversaries(
        limit: int = 25, offset: int = 0, sort: str = "-updated_at"
    ) -> str:
        """List tracked adversaries (threat actors)."""
        resp, err = await _request(
            "get",
            "/adversaries",
            "list adversaries",
            params={
                "limit": _clamp(limit, 25, hi=200),
                "offset": max(0, int(offset or 0)),
                "sort": sort or "-updated_at",
            },
        )
        if err:
            return err
        items, total = _data_list(resp)

        def _fmt(a: dict) -> str:
            return (
                f"  #{a.get('id', '?')} {a.get('name', '?')} — "
                f"created: {a.get('created_at', '?')} | updated: {a.get('updated_at', '?')}"
            )

        return format_entity_list(items, _fmt, noun="adversary", total=total)

    async def get_threatq_adversary(adversary_id: int) -> str:
        """Get full details of an adversary including context and relationships."""
        resp, err = await _request(
            "get",
            f"/adversaries/{int(adversary_id)}",
            f"get adversary {adversary_id}",
            params={"with": "description,sources,tags,attributes,indicators,events"},
        )
        if err:
            return err
        a = _data_obj(resp)
        lines = [
            f"Adversary #{a.get('id', adversary_id)}: {a.get('name', '?')}",
            f"Created: {a.get('created_at', '?')} | Updated: {a.get('updated_at', '?')}",
        ]
        description = a.get("description")
        if isinstance(description, dict):
            description = description.get("value")
        if description:
            lines.append(f"Description: {_trunc(description, 400)}")
        sources = a.get("sources") or []
        if sources:
            lines.append(
                "Sources: " + ", ".join(str(s.get("name", "?")) for s in sources[:10])
            )
        tags = a.get("tags") or []
        if tags:
            lines.append("Tags: " + ", ".join(str(t.get("name", "?")) for t in tags[:15]))
        attributes = a.get("attributes") or []
        if attributes:
            lines.append("Attributes:")
            for attr in attributes[:15]:
                name = (attr.get("attribute") or {}).get("name") or attr.get("name", "?")
                lines.append(f"  - {name}: {_trunc(attr.get('value'), 120)}")
        indicators = a.get("indicators") or []
        if indicators:
            lines.append(f"Related indicators ({len(indicators)}):")
            for i in indicators[:10]:
                lines.append(f"  - #{i.get('id', '?')} {_trunc(i.get('value'), 90)}")
        events = a.get("events") or []
        if events:
            lines.append(f"Related events ({len(events)}):")
            for e in events[:10]:
                lines.append(f"  - #{e.get('id', '?')} {_trunc(e.get('title'), 90)}")
        return "\n".join(lines)

    async def create_threatq_adversary(
        name: str, description: str = "", source: str = ""
    ) -> str:
        """Create an adversary (threat actor) to track in the Threat Library."""
        item: dict[str, Any] = {"name": name}
        if source:
            item["sources"] = [{"name": source}]
        resp, err = await _request(
            "post", "/adversaries", "create adversary", json_body=[item]
        )
        if err:
            return err
        created, _ = _data_list(resp)
        rec = created[0] if created else {}
        adv_id = rec.get("id")
        if description and adv_id:
            _, derr = await _request(
                "post",
                f"/adversaries/{adv_id}/description",
                f"set description of adversary {adv_id}",
                json_body={"value": description},
            )
            if derr:
                return (
                    f"Created adversary #{adv_id}: {name}, but setting the "
                    f"description failed: {derr}"
                )
        return f"Created adversary #{adv_id or '?'}: {name}."

    # ══════════════════════════════════════════════════════════════════════
    # 5. ATTACHMENTS (FILES)
    # ══════════════════════════════════════════════════════════════════════

    async def list_threatq_files(
        limit: int = 25, offset: int = 0, sort: str = "-updated_at"
    ) -> str:
        """List attachments/files (malware samples, reports, evidence)."""
        resp, err = await _request(
            "get",
            "/attachments",
            "list files",
            params={
                "limit": _clamp(limit, 25, hi=200),
                "offset": max(0, int(offset or 0)),
                "sort": sort or "-updated_at",
                "with": "type",
            },
        )
        if err:
            return err
        items, total = _data_list(resp)

        def _fmt(f: dict) -> str:
            ftype = (f.get("type") or {}).get("name") if isinstance(f.get("type"), dict) else f.get("type_id")
            locked = " [malware-locked]" if f.get("malware_locked") in (1, "1", "Y", True) else ""
            return (
                f"  #{f.get('id', '?')} {_trunc(f.get('name') or f.get('title'), 90)} "
                f"[{ftype or '?'}]{locked} — updated: {f.get('updated_at', '?')}\n"
                f"    Download URL: {api}/attachments/{f.get('id', '?')}/download"
            )

        out = format_entity_list(items, _fmt, noun="file", total=total)
        if items:
            out += "\n[download_threatq_file(attachment_id) stores one as a file]"
        return out

    async def get_threatq_file(attachment_id: int) -> str:
        """Get details of an attachment/file (hash, type, sources, tags)."""
        resp, err = await _request(
            "get",
            f"/attachments/{int(attachment_id)}",
            f"get file {attachment_id}",
            params={"with": "type,sources,tags,attributes"},
        )
        if err:
            return err
        f = _data_obj(resp)
        ftype = (f.get("type") or {}).get("name", "?") if isinstance(f.get("type"), dict) else f.get("type_id")
        lines = [
            f"File #{f.get('id', attachment_id)}: {f.get('name') or f.get('title', '?')}",
            f"Type: {ftype} | Hash: {f.get('hash', '?')} | "
            f"Malware locked: {f.get('malware_locked', '?')}",
            f"Created: {f.get('created_at', '?')} | Updated: {f.get('updated_at', '?')}",
        ]
        if f.get("title") and f.get("name") and f.get("title") != f.get("name"):
            lines.append(f"Title: {f.get('title')}")
        sources = f.get("sources") or []
        if sources:
            lines.append(
                "Sources: " + ", ".join(str(s.get("name", "?")) for s in sources[:10])
            )
        tags = f.get("tags") or []
        if tags:
            lines.append("Tags: " + ", ".join(str(t.get("name", "?")) for t in tags[:15]))
        attributes = f.get("attributes") or []
        if attributes:
            lines.append("Attributes:")
            for a in attributes[:15]:
                name = (a.get("attribute") or {}).get("name") or a.get("name", "?")
                lines.append(f"  - {name}: {_trunc(a.get('value'), 120)}")
        lines.append(f"Download URL: {api}/attachments/{int(attachment_id)}/download")
        return "\n".join(lines)

    async def download_threatq_file(attachment_id: int) -> str | ToolResult:
        """Store an attachment/file of the Threat Library as a file of the run."""
        resp, err = await _request(
            "get",
            f"/attachments/{int(attachment_id)}",
            f"get file {attachment_id}",
            params={"with": "type"},
        )
        if err:
            return err
        meta = _data_obj(resp)
        name = str(meta.get("name") or meta.get("title") or f"threatq-file-{attachment_id}")
        download_url = f"{api}/attachments/{int(attachment_id)}/download"
        links = [("Download URL", download_url)]
        type_name = str((meta.get("type") or {}).get("name") or "") if isinstance(meta.get("type"), dict) else ""
        if meta.get("malware_locked") in (1, "1", "Y", True):
            return live_artefact_links(
                filename=name,
                source=_PROVIDER,
                links=links,
                reason="ThreatQ keeps it malware-locked, a lock that forbids opening it.",
            )
        size = meta.get("file_size")
        if isinstance(size, int) and size > _MAX_FILE_BYTES:
            return (
                f"The file is {round(size / 1024 / 1024, 1)} MB, over the "
                f"{_MAX_FILE_BYTES // 1024 // 1024} MB download limit.\n"
                + untrusted_file_facts(source=_PROVIDER, filename=name, links=links)
            )
        fetched = await _fetch(
            f"/attachments/{int(attachment_id)}/download", f"download file {attachment_id}"
        )
        if isinstance(fetched, str):
            return f"{fetched}\nDownload URL: {download_url}"
        details = [f"Type: {type_name}"] if type_name else []
        if _is_malware_type(type_name):
            return await deliver_live_artefact(
                data=fetched.data,
                filename=name,
                source=_PROVIDER,
                persist_file_fn=persist_file_fn,
                links=links,
                details=details,
            )
        return await deliver_file(
            data=fetched.data,
            filename=name,
            content_type=file_content_type(fetched.content_type, name),
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            links=links,
            details=details,
        )

    async def upload_threatq_file(
        file_id: str,
        file_type: str,
        title: str = "",
        malware_locked: bool = False,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Add a file of the run to the Threat Library as an attachment (upload, then create)."""
        piped = await read_pipe_file(
            file_context, file_id, max_bytes=_MAX_FILE_BYTES, target=_PROVIDER
        )
        if isinstance(piped, str):
            return ToolResult(tool_call_id="", content=piped, is_error=True)
        data, filename, content_type = piped
        file_id = file_id.strip()
        ftype, terr = await _resolve_named_id("/attachments/types", file_type, "file type")
        if terr:
            return terr
        type_name = str((ftype or {}).get("name") or file_type)
        # A file of a malware type is live malware: ThreatQ keeps it locked.
        locked = malware_locked or _is_malware_type(type_name)
        size = str(len(data))
        form = {
            "resumableChunkNumber": "1",
            "resumableChunkSize": size,
            "resumableCurrentChunkSize": size,
            "resumableTotalSize": size,
            "resumableType": content_type,
            "resumableIdentifier": f"{size}-{''.join(c for c in filename if c.isalnum())}",
            "resumableFilename": filename,
            "resumableRelativePath": filename,
            "resumableTotalChunks": "1",
        }
        _, err = await _file_request(
            "post",
            "/attachments/upload",
            f"upload file {file_id}",
            data=form,
            files={"file": ("blob", data, "application/octet-stream")},
        )
        if err:
            return err
        body = {
            "name": filename,
            "title": title or filename,
            "type_id": (ftype or {}).get("id"),
            "malware_locked": 1 if locked else 0,
        }
        resp, err = await _request(
            "post", "/attachments", f"create the attachment of file {file_id}", json_body=body
        )
        if err:
            return err
        created = _data_obj(resp)
        file_ref = created.get("id", "?")
        # The stored name (and the title it defaults to) is a third party's
        # text, and so is the type name ThreatQ answered with: they go in
        # the block, the file is named by its file_id.
        return uploaded_file_answer(
            f"Added file {file_id} to ThreatQ as file #{file_ref}"
            + (" (malware-locked)" if locked else "")
            + ".\nRelate it to an indicator, event or adversary with link_threatq_objects.",
            target=_PROVIDER,
            filename=filename,
            details=[f"Type: {single_line(type_name)}", f"Title: {single_line(title or filename)}"],
            links=[("Download URL", f"{api}/attachments/{file_ref}/download")],
        )

    # ══════════════════════════════════════════════════════════════════════
    # 6. SIGNATURES
    # ══════════════════════════════════════════════════════════════════════

    async def list_threatq_signatures(
        limit: int = 25, offset: int = 0, sort: str = "-updated_at"
    ) -> str:
        """List detection signatures (YARA, Snort, Suricata, OpenIOC...)."""
        resp, err = await _request(
            "get",
            "/signatures",
            "list signatures",
            params={
                "limit": _clamp(limit, 25, hi=200),
                "offset": max(0, int(offset or 0)),
                "sort": sort or "-updated_at",
                "with": "type,status",
            },
        )
        if err:
            return err
        items, total = _data_list(resp)

        def _fmt(s: dict) -> str:
            stype = (s.get("type") or {}).get("name") if isinstance(s.get("type"), dict) else s.get("type_id")
            sstatus = (
                (s.get("status") or {}).get("name")
                if isinstance(s.get("status"), dict)
                else s.get("status_id")
            )
            return (
                f"  #{s.get('id', '?')} {_trunc(s.get('name'), 90)} "
                f"[{stype or '?'}] — status: {sstatus or '?'} | "
                f"updated: {s.get('updated_at', '?')}"
            )

        return format_entity_list(items, _fmt, noun="signature", total=total)

    async def get_threatq_signature(signature_id: int) -> str:
        """Get a signature's details including the full rule body."""
        resp, err = await _request(
            "get",
            f"/signatures/{int(signature_id)}",
            f"get signature {signature_id}",
            params={"with": "type,status,sources,tags"},
        )
        if err:
            return err
        s = _data_obj(resp)
        stype = (s.get("type") or {}).get("name", "?") if isinstance(s.get("type"), dict) else s.get("type_id")
        sstatus = (
            (s.get("status") or {}).get("name", "?")
            if isinstance(s.get("status"), dict)
            else s.get("status_id")
        )
        lines = [
            f"Signature #{s.get('id', signature_id)}: {s.get('name', '?')}",
            f"Type: {stype} | Status: {sstatus} | "
            f"Created: {s.get('created_at', '?')} | Updated: {s.get('updated_at', '?')}",
        ]
        if s.get("description"):
            lines.append(f"Description: {_trunc(s.get('description'), 300)}")
        if s.get("value"):
            lines.append(f"Rule body:\n{_trunc(s.get('value'), 2000)}")
        sources = s.get("sources") or []
        if sources:
            lines.append(
                "Sources: " + ", ".join(str(src.get("name", "?")) for src in sources[:10])
            )
        return "\n".join(lines)

    async def create_threatq_signature(
        name: str,
        signature_type: str,
        value: str,
        status: str = "Active",
        description: str = "",
        source: str = "",
    ) -> str:
        """Create a detection signature; type/status names resolved to ids."""
        type_rec, err = await _resolve_named_id(
            "/signature/types", signature_type, "signature type"
        )
        if err:
            return err
        status_rec, err = await _resolve_named_id(
            "/signature/statuses", status, "signature status"
        )
        if err:
            return err
        item: dict[str, Any] = {
            "name": name,
            "type_id": type_rec.get("id"),
            "status_id": status_rec.get("id"),
            "value": value,
        }
        if description:
            item["description"] = description
        if source:
            item["sources"] = [{"name": source}]
        resp, err = await _request(
            "post", "/signatures", "create signature", json_body=[item]
        )
        if err:
            return err
        created, _ = _data_list(resp)
        rec = created[0] if created else {}
        return (
            f"Created signature #{rec.get('id', '?')}: {name} "
            f"[{signature_type}] with status {status}."
        )

    # ══════════════════════════════════════════════════════════════════════
    # 7. GENERIC STIX-STYLE OBJECTS (ttp, malware, campaign, ...)
    # ══════════════════════════════════════════════════════════════════════

    def _fmt_generic(o: dict) -> str:
        label = o.get("value") or o.get("name") or o.get("title") or "?"
        return (
            f"  #{o.get('id', '?')} {_trunc(label, 100)} — "
            f"updated: {o.get('updated_at', '?')}"
        )

    async def list_threatq_objects(
        object_type: str, limit: int = 25, offset: int = 0
    ) -> str:
        """List objects of any Threat Library collection (ttp, malware...)."""
        collection = _normalize_object_type(object_type)
        if not collection:
            return _object_type_error(object_type)
        resp, err = await _request(
            "get",
            f"/{collection}",
            f"list {collection}",
            params={
                "limit": _clamp(limit, 25, hi=200),
                "offset": max(0, int(offset or 0)),
                "sort": "-updated_at",
            },
        )
        if err:
            return err
        items, total = _data_list(resp)
        return format_entity_list(
            items, _fmt_generic, noun=collection.replace("_", " ").rstrip("s") + " object", total=total
        )

    async def get_threatq_object(object_type: str, object_id: int) -> str:
        """Get any Threat Library object by collection + id."""
        collection = _normalize_object_type(object_type)
        if not collection:
            return _object_type_error(object_type)
        resp, err = await _request(
            "get",
            f"/{collection}/{int(object_id)}",
            f"get {collection} {object_id}",
        )
        if err:
            return err
        o = _data_obj(resp)
        label = o.get("value") or o.get("name") or o.get("title") or "?"
        lines = [
            f"{collection} #{o.get('id', object_id)}: {label}",
            f"Created: {o.get('created_at', '?')} | Updated: {o.get('updated_at', '?')}",
        ]
        if o.get("description"):
            lines.append(f"Description: {_trunc(o.get('description'), 500)}")
        for key in ("started_at", "ended_at", "happened_at", "status_id"):
            if o.get(key):
                lines.append(f"{key}: {o.get(key)}")
        return "\n".join(lines)

    async def create_threatq_object(
        object_type: str, value: str, description: str = "", source: str = ""
    ) -> str:
        """Create a STIX-style Threat Library object (ttp, malware, campaign...)."""
        collection = _normalize_object_type(object_type)
        if not collection:
            return _object_type_error(object_type)
        if collection in _SYSTEM_OBJECTS:
            return (
                f"Error: use the dedicated create tool for {collection} "
                "(create_threatq_indicator / create_threatq_event / "
                "create_threatq_adversary / create_threatq_signature)."
            )
        item: dict[str, Any] = {"value": value}
        if description:
            item["description"] = description
        if source:
            item["sources"] = [{"name": source}]
        resp, err = await _request(
            "post", f"/{collection}", f"create {collection}", json_body=[item]
        )
        if err:
            return err
        created, _ = _data_list(resp)
        rec = created[0] if created else {}
        return f"Created {collection} #{rec.get('id', '?')}: {value}."

    # Label field per collection — most STIX-style objects are keyed by
    # ``value``, but adversaries/attachments/signatures use ``name`` and
    # events use ``title``.
    _update_label_fields = {
        "adversaries": "name",
        "attachments": "name",
        "signatures": "name",
        "events": "title",
    }

    async def update_threatq_object(
        object_type: str, object_id: int, value: str = "", description: str = ""
    ) -> str:
        """Update a STIX-style object's value and/or description."""
        collection = _normalize_object_type(object_type)
        if not collection:
            return _object_type_error(object_type)
        if collection == "indicators":
            return "Error: use update_threatq_indicator for indicators."
        body: dict[str, Any] = {}
        if value:
            body[_update_label_fields.get(collection, "value")] = value
        if description:
            body["description"] = description
        if not body:
            return "Error: No fields provided to update."
        _, err = await _request(
            "put",
            f"/{collection}/{int(object_id)}",
            f"update {collection} {object_id}",
            json_body=body,
        )
        if err:
            return err
        return f"Updated {collection} #{object_id}: " + ", ".join(sorted(body)) + " set."

    # ══════════════════════════════════════════════════════════════════════
    # 8. OBJECT CONTEXT (attributes, comments, sources, tags, relationships)
    # ══════════════════════════════════════════════════════════════════════

    async def list_threatq_related_objects(
        object_type: str, object_id: int, related_type: str, limit: int = 25
    ) -> str:
        """List objects related to a given object (pivot on relationships)."""
        src = _normalize_object_type(object_type)
        if not src:
            return _object_type_error(object_type)
        dest = _normalize_object_type(related_type)
        if not dest:
            return _object_type_error(related_type)
        resp, err = await _request(
            "get",
            f"/{src}/{int(object_id)}/{dest}",
            f"list {dest} related to {src} {object_id}",
            params={"limit": _clamp(limit, 25, hi=200)},
        )
        if err:
            return err
        items, total = _data_list(resp)

        def _fmt(o: dict) -> str:
            label = o.get("value") or o.get("name") or o.get("title") or "?"
            link_id = (o.get("pivot") or {}).get("id", "?")
            return (
                f"  #{o.get('id', '?')} {_trunc(label, 100)} "
                f"(relationship link id {link_id})"
            )

        return format_entity_list(
            items,
            _fmt,
            noun=f"related {dest.replace('_', ' ')} object",
            total=total,
            empty_msg=f"No {dest} related to {src} #{object_id}.",
        )

    async def link_threatq_objects(
        object_type: str, object_id: int, related_type: str, related_id: int
    ) -> str:
        """Create a relationship between two Threat Library objects."""
        src = _normalize_object_type(object_type)
        if not src:
            return _object_type_error(object_type)
        dest = _normalize_object_type(related_type)
        if not dest:
            return _object_type_error(related_type)
        _, err = await _request(
            "post",
            f"/{src}/{int(object_id)}/{dest}",
            f"link {src} {object_id} to {dest} {related_id}",
            json_body={"id": int(related_id)},
        )
        if err:
            return err
        return f"Linked {src} #{object_id} to {dest} #{related_id}."

    async def list_threatq_object_comments(
        object_type: str, object_id: int, limit: int = 25
    ) -> str:
        """List analyst comments on any Threat Library object."""
        collection = _normalize_object_type(object_type)
        if not collection:
            return _object_type_error(object_type)
        resp, err = await _request(
            "get",
            f"/{collection}/{int(object_id)}/comments",
            f"list comments of {collection} {object_id}",
            params={"limit": _clamp(limit, 25, hi=100)},
        )
        if err:
            return err
        items, total = _data_list(resp)

        def _fmt(c: dict) -> str:
            return (
                f"  #{c.get('id', '?')} ({c.get('created_at', '?')}): "
                f"{_trunc(c.get('value'), 200)}"
            )

        return format_entity_list(
            items,
            _fmt,
            noun="comment",
            total=total,
            empty_msg=f"No comments on {collection} #{object_id}.",
        )

    async def add_threatq_comment(
        object_type: str, object_id: int, comment: str
    ) -> str:
        """Add an analyst comment to any Threat Library object."""
        collection = _normalize_object_type(object_type)
        if not collection:
            return _object_type_error(object_type)
        _, err = await _request(
            "post",
            f"/{collection}/{int(object_id)}/comments",
            f"add comment to {collection} {object_id}",
            json_body={"value": comment},
        )
        if err:
            return err
        return f"Comment added to {collection} #{object_id}."

    async def list_threatq_object_attributes(
        object_type: str, object_id: int, limit: int = 50
    ) -> str:
        """List contextual attributes of any Threat Library object."""
        collection = _normalize_object_type(object_type)
        if not collection:
            return _object_type_error(object_type)
        resp, err = await _request(
            "get",
            f"/{collection}/{int(object_id)}/attributes",
            f"list attributes of {collection} {object_id}",
            params={"limit": _clamp(limit, 50, hi=200), "with": "attribute"},
        )
        if err:
            return err
        items, total = _data_list(resp)

        def _fmt(a: dict) -> str:
            name = (a.get("attribute") or {}).get("name") or a.get("name", "?")
            return f"  #{a.get('id', '?')} {name}: {_trunc(a.get('value'), 150)}"

        return format_entity_list(
            items,
            _fmt,
            noun="attribute",
            total=total,
            empty_msg=f"No attributes on {collection} #{object_id}.",
        )

    async def add_threatq_attributes(
        object_type: str, object_id: int, attributes: str, source: str = ""
    ) -> str:
        """Attach attributes (JSON object of name → value pairs) to an object."""
        collection = _normalize_object_type(object_type)
        if not collection:
            return _object_type_error(object_type)
        try:
            parsed = json.loads(attributes)
        except (TypeError, ValueError) as exc:
            return f"Error: attributes must be a JSON object of name/value pairs — {exc}"
        if not isinstance(parsed, dict) or not parsed:
            return "Error: attributes must be a non-empty JSON object like {\"Confidence\": \"High\"}."
        body = []
        for name, value in parsed.items():
            item: dict[str, Any] = {"name": str(name), "value": str(value)}
            if source:
                item["sources"] = [{"name": source}]
            body.append(item)
        _, err = await _request(
            "post",
            f"/{collection}/{int(object_id)}/attributes",
            f"add attributes to {collection} {object_id}",
            json_body=body,
        )
        if err:
            return err
        return (
            f"Added {len(body)} attribute(s) to {collection} #{object_id}: "
            + ", ".join(parsed)
        )

    async def list_threatq_object_sources(object_type: str, object_id: int) -> str:
        """List provenance sources of any Threat Library object."""
        collection = _normalize_object_type(object_type)
        if not collection:
            return _object_type_error(object_type)
        resp, err = await _request(
            "get",
            f"/{collection}/{int(object_id)}/sources",
            f"list sources of {collection} {object_id}",
        )
        if err:
            return err
        items, total = _data_list(resp)

        def _fmt(s: dict) -> str:
            return (
                f"  #{s.get('id', '?')} {s.get('name', '?')} — "
                f"published: {s.get('published_at') or '?'}"
            )

        return format_entity_list(
            items,
            _fmt,
            noun="source",
            total=total,
            empty_msg=f"No sources on {collection} #{object_id}.",
        )

    async def add_threatq_source(
        object_type: str, object_id: int, source: str, tlp: str = ""
    ) -> str:
        """Record a provenance source (with optional TLP) on an object."""
        collection = _normalize_object_type(object_type)
        if not collection:
            return _object_type_error(object_type)
        body: dict[str, Any] = {"name": source}
        if tlp:
            body["tlp"] = {"name": tlp.upper()}
        _, err = await _request(
            "post",
            f"/{collection}/{int(object_id)}/sources",
            f"add source to {collection} {object_id}",
            json_body=body,
        )
        if err:
            return err
        return f"Source '{source}' added to {collection} #{object_id}."

    async def add_threatq_tag(object_type: str, object_id: int, tag: str) -> str:
        """Tag any Threat Library object."""
        collection = _normalize_object_type(object_type)
        if not collection:
            return _object_type_error(object_type)
        _, err = await _request(
            "post",
            f"/{collection}/{int(object_id)}/tags",
            f"tag {collection} {object_id}",
            json_body={"name": tag},
        )
        if err:
            return err
        return f"Tag '{tag}' added to {collection} #{object_id}."

    # ══════════════════════════════════════════════════════════════════════
    # 9. PLATFORM CONTEXT (tags, sources, data collections, scoring, exports)
    # ══════════════════════════════════════════════════════════════════════

    async def list_threatq_tags(limit: int = 50, offset: int = 0) -> str:
        """List Threat Library tags."""
        resp, err = await _request(
            "get",
            "/tags",
            "list tags",
            params={
                "limit": _clamp(limit, 50, hi=200),
                "offset": max(0, int(offset or 0)),
                "sort": "name",
            },
        )
        if err:
            return err
        items, total = _data_list(resp)

        def _fmt(t: dict) -> str:
            return f"  #{t.get('id', '?')} {t.get('name', '?')}"

        return format_entity_list(items, _fmt, noun="tag", total=total)

    async def list_threatq_sources(limit: int = 50, offset: int = 0) -> str:
        """List intelligence sources (feeds, connectors, analysts)."""
        resp, err = await _request(
            "get",
            "/sources",
            "list sources",
            params={
                "limit": _clamp(limit, 50, hi=200),
                "offset": max(0, int(offset or 0)),
                "sort": "name",
            },
        )
        if err:
            return err
        items, total = _data_list(resp)

        def _fmt(s: dict) -> str:
            return f"  #{s.get('id', '?')} {s.get('name', '?')}"

        return format_entity_list(items, _fmt, noun="source", total=total)

    async def list_threatq_data_collections(limit: int = 25, offset: int = 0) -> str:
        """List Threat Library data collections (saved searches)."""
        resp, err = await _request(
            "get",
            "/search/query",
            "list data collections",
            params={
                "limit": _clamp(limit, 25, hi=100),
                "offset": max(0, int(offset or 0)),
                "sort": "name",
            },
        )
        if err:
            return err
        items, total = _data_list(resp)

        def _fmt(c: dict) -> str:
            return (
                f"  {c.get('name', '(unnamed)')} — hash: {c.get('hash', '?')} | "
                f"updated: {c.get('updated_at', '?')}"
            )

        return format_entity_list(items, _fmt, noun="data collection", total=total)

    async def get_threatq_data_collection(collection_hash: str) -> str:
        """Inspect a data collection's saved query definition by hash."""
        resp, err = await _request(
            "get",
            f"/search/query/{collection_hash}",
            f"get data collection {collection_hash}",
        )
        if err:
            return err
        c = _data_obj(resp)
        query_json = c.get("json") or {}
        api_query = query_json.get("api_query") or {}
        objects = ((query_json.get("ui_query") or {}).get("objects") or {}).get(
            "selected"
        )
        lines = [
            f"Data collection: {c.get('name', '(unnamed)')} (hash {c.get('hash', collection_hash)})",
            f"Created: {c.get('created_at', '?')} | Updated: {c.get('updated_at', '?')}",
        ]
        if objects:
            lines.append(f"Object types: {json.dumps(objects, default=str)[:200]}")
        if api_query:
            lines.append(f"API query: {_trunc(json.dumps(api_query, default=str), 1500)}")
        return "\n".join(lines)

    async def get_threatq_score_configuration() -> str:
        """Inspect the indicator scoring algorithm configuration."""
        resp, err = await _request(
            "get", "/score/configuration", "get score configuration"
        )
        if err:
            return err
        cfg = _data_obj(resp)
        lines = ["ThreatQ scoring algorithm configuration:"]
        for section in ("indicator_types", "sources", "attributes", "relations"):
            entries = cfg.get(section) or []
            if entries:
                lines.append(f"{section} ({len(entries)} weighted):")
                for entry in entries[:15]:
                    label = entry.get("name") or entry.get("value") or entry.get(
                        "id", "?"
                    )
                    lines.append(f"  - {label}: score {entry.get('score', '?')}")
        if len(lines) == 1:
            lines.append("  (no weighted criteria configured)")
        return "\n".join(lines)

    async def list_threatq_exports(limit: int = 50) -> str:
        """List export feed definitions (outbound intel feeds)."""
        resp, err = await _request(
            "get",
            "/exporters",
            "list exports",
            params={"limit": _clamp(limit, 50, hi=200)},
        )
        if err:
            return err
        items, total = _data_list(resp)

        def _fmt(e: dict) -> str:
            enabled = "enabled" if e.get("is_enabled") in (1, "1", "Y", True) else "disabled"
            line = (
                f"  #{e.get('id', '?')} {e.get('name', '?')} — {enabled} | "
                f"updated: {e.get('updated_at', '?')}"
            )
            if e.get("url"):
                line += f"\n    Export URL: {api}/export/{e['url']} (plus the export's token)"
            return line

        out = format_entity_list(items, _fmt, noun="export", total=total)
        if items:
            out += "\n[download_threatq_export(exporter_id) generates one and stores it as a file]"
        return out

    async def download_threatq_export(exporter_id: int, limit: int = 0) -> str | ToolResult:
        """Generate an export feed now and store its output as a file of the run."""
        resp, err = await _request(
            "get",
            f"/exporters/{int(exporter_id)}",
            f"get export {exporter_id}",
            params={"with": "config"},
        )
        if err:
            return err
        exporter = _data_obj(resp)
        feed = str(exporter.get("url") or "").strip()
        if not feed:
            return f"Error: export #{exporter_id} has no export URL (it is not a URL-delivered export)."
        config = {
            str(c.get("name", "")).lower(): c.get("value")
            for c in (exporter.get("config") or [])
            if isinstance(c, dict)
        }
        feed_token = str(config.get("token") or "").strip()
        if not feed_token:
            return f"Error: export #{exporter_id} has no token configured."
        params: dict[str, Any] = {"token": feed_token}
        if int(limit or 0) > 0:
            params["limit"] = int(limit)
        fetched = await _fetch(f"/export/{feed}", f"generate export {exporter_id}", params=params)
        if isinstance(fetched, str):
            return fetched
        content_type = (
            str(config.get("contenttype") or "")
            or fetched.content_type.split(";")[0].strip()
            or "text/plain"
        )
        extension = {"text/csv": "csv", "application/json": "json", "application/xml": "xml"}.get(
            content_type, "txt"
        )
        name = "".join(c if c.isalnum() or c in "-_" else "-" for c in str(exporter.get("name") or feed))
        return await deliver_file(
            data=fetched.data,
            filename=f"threatq-export-{name}.{extension}",
            content_type=content_type,
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            links=[("Export URL", f"{api}/export/{feed} (plus the export's token)")],
            details=[f"Export: {_trunc(exporter.get('name'), 120)} (#{exporter_id})"],
        )

    # ══════════════════════════════════════════════════════════════════════
    # 10. CONSOLIDATED LONG-TAIL / DESTRUCTIVE MANAGEMENT
    # ══════════════════════════════════════════════════════════════════════

    async def manage_threatq_object(
        action: str,
        object_type: str = "",
        object_id: int = 0,
        target_type: str = "",
        target_id: int = 0,
        collection_hash: str = "",
    ) -> str:
        """Long-tail management actions (deletes, unlink, watchlist). Destructive."""
        act = (action or "").strip().lower()

        if act == "delete_data_collection":
            if not collection_hash:
                return "Error: delete_data_collection requires collection_hash."
            _, err = await _request(
                "delete",
                f"/search/query/{collection_hash}",
                f"delete data collection {collection_hash}",
            )
            return err or f"Data collection {collection_hash} permanently deleted."

        collection = _normalize_object_type(object_type)
        if not collection:
            return _object_type_error(object_type)
        if not object_id:
            return "Error: object_id is required."
        oid = int(object_id)

        if act == "delete_object":
            _, err = await _request(
                "delete", f"/{collection}/{oid}", f"delete {collection} {oid}"
            )
            return err or f"{collection} #{oid} permanently deleted."
        if act == "remove_tag":
            if not target_id:
                return "Error: remove_tag requires target_id (the tag id)."
            _, err = await _request(
                "delete",
                f"/{collection}/{oid}/tags/{int(target_id)}",
                f"remove tag {target_id} from {collection} {oid}",
            )
            return err or f"Tag #{target_id} removed from {collection} #{oid}."
        if act == "delete_attribute":
            if not target_id:
                return "Error: delete_attribute requires target_id (the attribute id)."
            _, err = await _request(
                "delete",
                f"/{collection}/{oid}/attributes/{int(target_id)}",
                f"delete attribute {target_id} of {collection} {oid}",
            )
            return err or f"Attribute #{target_id} deleted from {collection} #{oid}."
        if act == "delete_comment":
            if not target_id:
                return "Error: delete_comment requires target_id (the comment id)."
            _, err = await _request(
                "delete",
                f"/{collection}/{oid}/comments/{int(target_id)}",
                f"delete comment {target_id} of {collection} {oid}",
            )
            return err or f"Comment #{target_id} deleted from {collection} #{oid}."
        if act == "unlink_objects":
            dest = _normalize_object_type(target_type)
            if not dest:
                return _object_type_error(target_type)
            if not target_id:
                return (
                    "Error: unlink_objects requires target_id (the relationship "
                    "link id from list_threatq_related_objects)."
                )
            _, err = await _request(
                "delete",
                f"/{collection}/{oid}/{dest}/{int(target_id)}",
                f"unlink {dest} link {target_id} from {collection} {oid}",
            )
            return err or (
                f"Relationship link #{target_id} ({dest}) removed from "
                f"{collection} #{oid}."
            )
        if act == "watchlist_add":
            _, err = await _request(
                "post",
                f"/{collection}/{oid}/watchlist",
                f"watch {collection} {oid}",
            )
            return err or f"{collection} #{oid} added to your watchlist."
        if act == "watchlist_remove":
            if not target_id:
                return (
                    "Error: watchlist_remove requires target_id (the watchlist "
                    "entry id — GET the object's watchlist first)."
                )
            _, err = await _request(
                "delete",
                f"/{collection}/{oid}/watchlist/{int(target_id)}",
                f"unwatch {collection} {oid}",
            )
            return err or f"{collection} #{oid} removed from your watchlist."

        return (
            f"Error: unknown action '{action}'. Valid actions: delete_object, "
            "remove_tag, delete_attribute, delete_comment, unlink_objects, "
            "watchlist_add, watchlist_remove, delete_data_collection."
        )

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("whoami_threatq", whoami_threatq)
    executor.register("search_threatq_objects", search_threatq_objects)
    executor.register("search_threatq_indicators", search_threatq_indicators)
    executor.register("list_threatq_indicators", list_threatq_indicators)
    executor.register("get_threatq_indicator", get_threatq_indicator)
    executor.register("list_threatq_indicator_types", list_threatq_indicator_types)
    executor.register(
        "list_threatq_indicator_statuses", list_threatq_indicator_statuses
    )
    executor.register("create_threatq_indicator", create_threatq_indicator)
    executor.register("update_threatq_indicator", update_threatq_indicator)
    executor.register("set_threatq_indicator_status", set_threatq_indicator_status)
    executor.register(
        "set_threatq_indicator_expiration", set_threatq_indicator_expiration
    )
    executor.register("set_threatq_indicator_score", set_threatq_indicator_score)
    executor.register("list_threatq_events", list_threatq_events)
    executor.register("get_threatq_event", get_threatq_event)
    executor.register("list_threatq_event_types", list_threatq_event_types)
    executor.register("create_threatq_event", create_threatq_event)
    executor.register("list_threatq_adversaries", list_threatq_adversaries)
    executor.register("get_threatq_adversary", get_threatq_adversary)
    executor.register("create_threatq_adversary", create_threatq_adversary)
    executor.register("list_threatq_files", list_threatq_files)
    executor.register("get_threatq_file", get_threatq_file)
    executor.register("download_threatq_file", download_threatq_file)
    executor.register("upload_threatq_file", upload_threatq_file)
    executor.register("list_threatq_signatures", list_threatq_signatures)
    executor.register("get_threatq_signature", get_threatq_signature)
    executor.register("create_threatq_signature", create_threatq_signature)
    executor.register("list_threatq_objects", list_threatq_objects)
    executor.register("get_threatq_object", get_threatq_object)
    executor.register("create_threatq_object", create_threatq_object)
    executor.register("update_threatq_object", update_threatq_object)
    executor.register("list_threatq_related_objects", list_threatq_related_objects)
    executor.register("link_threatq_objects", link_threatq_objects)
    executor.register("list_threatq_object_comments", list_threatq_object_comments)
    executor.register("add_threatq_comment", add_threatq_comment)
    executor.register("list_threatq_object_attributes", list_threatq_object_attributes)
    executor.register("add_threatq_attributes", add_threatq_attributes)
    executor.register("list_threatq_object_sources", list_threatq_object_sources)
    executor.register("add_threatq_source", add_threatq_source)
    executor.register("add_threatq_tag", add_threatq_tag)
    executor.register("list_threatq_tags", list_threatq_tags)
    executor.register("list_threatq_sources", list_threatq_sources)
    executor.register("list_threatq_data_collections", list_threatq_data_collections)
    executor.register("get_threatq_data_collection", get_threatq_data_collection)
    executor.register(
        "get_threatq_score_configuration", get_threatq_score_configuration
    )
    executor.register("list_threatq_exports", list_threatq_exports)
    executor.register("download_threatq_export", download_threatq_export)
    executor.register("manage_threatq_object", manage_threatq_object)

    _object_type_schema = {
        "type": "string",
        "description": f"Threat Library collection. One of: {_OBJECT_TYPE_HINT} (aliases like 'files' are accepted)",
    }

    return [
        # ── 0. Connection ───────────────────────────────────────────────────
        ToolDefinition(
            name="whoami_threatq",
            description=(
                "Validate the ThreatQ connection and report the current API "
                "user. Use first if other ThreatQ tools fail."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        # ── 1. Search ───────────────────────────────────────────────────────
        ToolDefinition(
            name="search_threatq_objects",
            description=(
                "Quick cross-object search of the ThreatQ Threat Library by "
                "value or name (like the UI Basic Search). Returns matching "
                "objects of every type with their ids for follow-up calls. Use "
                "search_threatq_indicators for structured indicator hunts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Value/name to look up (e.g. an IP, domain, hash, adversary name)",
                    },
                    "object_types": {
                        "type": "string",
                        "description": "Optional comma-separated collections to restrict to (e.g. 'indicators,malware'). Default: all",
                    },
                    "limit": {"type": "integer", "default": 25},
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_threatq_indicators",
            description=(
                "Advanced Threat Library indicator search with structured "
                "filters: substring value match, type name (e.g. 'IP Address', "
                "'FQDN'), status name (e.g. 'Active'), score range (0-10), "
                "source name, tag, and created-at date range (format "
                "'YYYY-MM-DD HH:MM:SS'). Provide at least one filter. Prefer "
                "this over list_threatq_indicators when hunting."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "value_contains": {"type": "string"},
                    "indicator_type": {
                        "type": "string",
                        "description": "Type name — see list_threatq_indicator_types",
                    },
                    "status": {
                        "type": "string",
                        "description": "Status name — see list_threatq_indicator_statuses",
                    },
                    "min_score": {"type": "integer", "description": "0-10", "default": -1},
                    "max_score": {"type": "integer", "description": "0-10", "default": -1},
                    "source": {"type": "string"},
                    "tag": {"type": "string"},
                    "created_after": {
                        "type": "string",
                        "description": "'YYYY-MM-DD HH:MM:SS'",
                    },
                    "created_before": {
                        "type": "string",
                        "description": "'YYYY-MM-DD HH:MM:SS'",
                    },
                    "limit": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        # ── 2. Indicators ───────────────────────────────────────────────────
        ToolDefinition(
            name="list_threatq_indicators",
            description=(
                "List Threat Library indicators (paginated, newest first) with "
                "type, status, and score. Use search_threatq_indicators for "
                "filtered hunts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                    "sort": {
                        "type": "string",
                        "description": "Sort field(s); prefix with '-' for descending (default -updated_at)",
                        "default": "-updated_at",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_threatq_indicator",
            description=(
                "Get full details of a ThreatQ indicator by id: type, status, "
                "score, expiration, sources, tags, attributes, related "
                "adversaries, and comment count."
            ),
            input_schema={
                "type": "object",
                "properties": {"indicator_id": {"type": "integer"}},
                "required": ["indicator_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_threatq_indicator_types",
            description=(
                "List indicator types (IP Address, FQDN, MD5, URL, ...) with "
                "their ids and classes. Types are appliance-specific — call "
                "before creating indicators with an unusual type."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_threatq_indicator_statuses",
            description=(
                "List indicator statuses (Active, Expired, Indirect, Review, "
                "Whitelisted + custom) with descriptions."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="create_threatq_indicator",
            description=(
                "Create an indicator in the Threat Library. Type and status "
                "are given by NAME (e.g. 'IP Address', 'Review') and resolved "
                "to appliance ids automatically. Optionally record provenance "
                "(source + TLP) and a description. If the indicator already "
                "exists it is deduplicated by ThreatQ."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "value": {"type": "string", "description": "The IOC value"},
                    "indicator_type": {
                        "type": "string",
                        "description": "Type name, e.g. 'IP Address', 'FQDN', 'URL', 'MD5', 'SHA-256'",
                    },
                    "status": {
                        "type": "string",
                        "description": "Status name (default Review)",
                        "default": "Review",
                    },
                    "description": {"type": "string"},
                    "source": {
                        "type": "string",
                        "description": "Provenance source name (e.g. your team or feed name)",
                    },
                    "tlp": {
                        "type": "string",
                        "description": (
                            "TLP marking for the source: RED, AMBER, GREEN, or "
                            "WHITE (case-insensitive)"
                        ),
                    },
                },
                "required": ["value", "indicator_type"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_threatq_indicator",
            description=(
                "Update an indicator's value, description, type, and/or "
                "status (names resolved to ids). Provide only the fields to "
                "change."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "indicator_id": {"type": "integer"},
                    "value": {"type": "string"},
                    "description": {"type": "string"},
                    "indicator_type": {"type": "string"},
                    "status": {"type": "string"},
                },
                "required": ["indicator_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="set_threatq_indicator_status",
            description=(
                "Set an indicator's status by name — the triage action. Use "
                "'Active' to operationalize, 'Expired' to deprecate, "
                "'Whitelisted' to suppress, 'Review' to queue for analysis."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "indicator_id": {"type": "integer"},
                    "status": {
                        "type": "string",
                        "description": "Status name, e.g. Active / Expired / Review / Whitelisted",
                    },
                },
                "required": ["indicator_id", "status"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="set_threatq_indicator_expiration",
            description=(
                "Bump an indicator's expiration date by a number of days from "
                "now (keeps a still-relevant IOC alive past its policy expiry)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "indicator_id": {"type": "integer"},
                    "days": {"type": "integer", "description": "Days to extend"},
                },
                "required": ["indicator_id", "days"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="set_threatq_indicator_score",
            description=(
                "Set a manual score override (0-10) on an indicator. The "
                "effective score becomes the highest of the manual and "
                "algorithm-generated scores."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "indicator_id": {"type": "integer"},
                    "score": {"type": "integer", "description": "0 (benign) to 10 (critical)"},
                },
                "required": ["indicator_id", "score"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 3. Events ───────────────────────────────────────────────────────
        ToolDefinition(
            name="list_threatq_events",
            description=(
                "List Threat Library events (spearphish, sightings, incidents, "
                "watering holes...), newest first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                    "sort": {"type": "string", "default": "-happened_at"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_threatq_event",
            description=(
                "Get full details of a ThreatQ event by id: type, timeline, "
                "sources, tags, related indicators and adversaries."
            ),
            input_schema={
                "type": "object",
                "properties": {"event_id": {"type": "integer"}},
                "required": ["event_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_threatq_event_types",
            description="List event types (Spearphish, Sighting, Incident, ...) with ids.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="create_threatq_event",
            description=(
                "Create an event in the Threat Library (e.g. record a "
                "spearphish or a sighting). The event type is given by NAME "
                "and resolved automatically; happened_at defaults to now."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "event_type": {
                        "type": "string",
                        "description": "Type name — see list_threatq_event_types",
                    },
                    "happened_at": {
                        "type": "string",
                        "description": "'YYYY-MM-DD HH:MM:SS' (default: now)",
                    },
                    "description": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["title", "event_type"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 4. Adversaries ──────────────────────────────────────────────────
        ToolDefinition(
            name="list_threatq_adversaries",
            description="List tracked adversaries (threat actors) in the Threat Library.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                    "sort": {"type": "string", "default": "-updated_at"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_threatq_adversary",
            description=(
                "Get full details of an adversary by id: description, sources, "
                "tags, attributes, related indicators and events."
            ),
            input_schema={
                "type": "object",
                "properties": {"adversary_id": {"type": "integer"}},
                "required": ["adversary_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_threatq_adversary",
            description="Create an adversary (threat actor) with an optional description and source.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["name"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 5. Files ────────────────────────────────────────────────────────
        ToolDefinition(
            name="list_threatq_files",
            description=(
                "List attachments/files in the Threat Library (malware "
                "samples, intel reports, evidence documents) with their id, "
                "type, malware lock and download URL."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                    "sort": {"type": "string", "default": "-updated_at"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_threatq_file",
            description="Get details of an attachment/file by id (hash, type, malware lock, sources, tags, download URL).",
            input_schema={
                "type": "object",
                "properties": {"attachment_id": {"type": "integer"}},
                "required": ["attachment_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="download_threatq_file",
            workspace_ingest=True,
            description=(
                "Store an attachment/file of the Threat Library as a file of the "
                "conversation (download card + file_id to forward, read inline) "
                "with its download URL. A malware-locked file stays locked (its "
                "link only). A file of a malware type can be live malware: it is "
                "stored only when it is a ZIP sealed with the password "
                "'infected', exactly as it came and never read inline; otherwise "
                "its link and the reason are given."
            ),
            input_schema={
                "type": "object",
                "properties": {"attachment_id": {"type": "integer"}},
                "required": ["attachment_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="upload_threatq_file",
            description=(
                "Add a file of the conversation (attached, generated or "
                "downloaded - by file_id) to the Threat Library as an "
                "attachment of the given file type (by NAME, e.g. 'Intelligence "
                "Report', 'Malware Sample' - resolved automatically). A file of a "
                "malware type is always added malware-locked; set malware_locked "
                "for any other live sample. Relate it afterwards with "
                "link_threatq_objects."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "A file attached, generated or downloaded in this conversation",
                    },
                    "file_type": {
                        "type": "string",
                        "description": "Attachment type name, e.g. 'Intelligence Report'",
                    },
                    "title": {"type": "string"},
                    "malware_locked": {"type": "boolean", "default": False},
                },
                "required": ["file_id", "file_type"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 6. Signatures ───────────────────────────────────────────────────
        ToolDefinition(
            name="list_threatq_signatures",
            description="List detection signatures (YARA, Snort, Suricata, OpenIOC...) with type and status.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                    "sort": {"type": "string", "default": "-updated_at"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_threatq_signature",
            description="Get a signature's details by id including the full rule body.",
            input_schema={
                "type": "object",
                "properties": {"signature_id": {"type": "integer"}},
                "required": ["signature_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_threatq_signature",
            description=(
                "Create a detection signature (YARA/Snort/Suricata rule). Type "
                "and status are given by NAME and resolved automatically; the "
                "value is the rule body."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "signature_type": {
                        "type": "string",
                        "description": "Type name, e.g. 'YARA', 'Snort'",
                    },
                    "value": {"type": "string", "description": "The rule body"},
                    "status": {"type": "string", "default": "Active"},
                    "description": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["name", "signature_type", "value"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 7. Generic objects ──────────────────────────────────────────────
        ToolDefinition(
            name="list_threatq_objects",
            description=(
                "List objects of any Threat Library collection — use for "
                "STIX-style types without dedicated tools: attack_pattern, "
                "campaign, course_of_action, exploit_target, identity, "
                "incident, intrusion_set, malware, report, tool, ttp, "
                "vulnerability, asset, tasks, investigations."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": _object_type_schema,
                    "limit": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": ["object_type"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_threatq_object",
            description="Get any Threat Library object by collection + id (ttp, malware, campaign, ...).",
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": _object_type_schema,
                    "object_id": {"type": "integer"},
                },
                "required": ["object_type", "object_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_threatq_object",
            description=(
                "Create a STIX-style Threat Library object (attack_pattern, "
                "campaign, course_of_action, exploit_target, identity, "
                "incident, intrusion_set, malware, report, tool, ttp, "
                "vulnerability). For indicators/events/adversaries/signatures "
                "use their dedicated create tools."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": _object_type_schema,
                    "value": {"type": "string", "description": "The object's name/value"},
                    "description": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["object_type", "value"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_threatq_object",
            description="Update a Threat Library object's value/name and/or description by collection + id.",
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": _object_type_schema,
                    "object_id": {"type": "integer"},
                    "value": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["object_type", "object_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 8. Context + relationships ──────────────────────────────────────
        ToolDefinition(
            name="list_threatq_related_objects",
            description=(
                "List objects related to a given object — the pivot move (e.g. "
                "indicators related to an adversary: object_type='adversaries', "
                "related_type='indicators'). Returns each related object's id "
                "and its relationship link id (needed to unlink)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": _object_type_schema,
                    "object_id": {"type": "integer"},
                    "related_type": {
                        "type": "string",
                        "description": "The related collection to pivot to (same options as object_type)",
                    },
                    "limit": {"type": "integer", "default": 25},
                },
                "required": ["object_type", "object_id", "related_type"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="link_threatq_objects",
            description=(
                "Create a relationship between two Threat Library objects "
                "(e.g. link an indicator to an adversary, a ttp to a malware). "
                "Both objects must already exist."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": _object_type_schema,
                    "object_id": {"type": "integer"},
                    "related_type": {"type": "string"},
                    "related_id": {"type": "integer"},
                },
                "required": ["object_type", "object_id", "related_type", "related_id"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_threatq_object_comments",
            description="List analyst comments on any Threat Library object.",
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": _object_type_schema,
                    "object_id": {"type": "integer"},
                    "limit": {"type": "integer", "default": 25},
                },
                "required": ["object_type", "object_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="add_threatq_comment",
            description="Add an analyst comment to any Threat Library object (audit trail / analysis notes).",
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": _object_type_schema,
                    "object_id": {"type": "integer"},
                    "comment": {"type": "string"},
                },
                "required": ["object_type", "object_id", "comment"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_threatq_object_attributes",
            description=(
                "List contextual attributes (Confidence, Malware Family, "
                "Country, ...) of any Threat Library object, with attribute "
                "ids (needed to delete)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": _object_type_schema,
                    "object_id": {"type": "integer"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["object_type", "object_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="add_threatq_attributes",
            description=(
                "Attach contextual attributes to any Threat Library object. "
                "Pass a JSON object of name/value pairs, e.g. "
                '{"Confidence": "High", "Malware Family": "Emotet"}.'
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": _object_type_schema,
                    "object_id": {"type": "integer"},
                    "attributes": {
                        "type": "string",
                        "description": 'JSON object of attribute name → value, e.g. {"Confidence": "High"}',
                    },
                    "source": {"type": "string"},
                },
                "required": ["object_type", "object_id", "attributes"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_threatq_object_sources",
            description="List provenance sources recorded on any Threat Library object.",
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": _object_type_schema,
                    "object_id": {"type": "integer"},
                },
                "required": ["object_type", "object_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="add_threatq_source",
            description="Record a provenance source (with optional TLP marking) on any Threat Library object.",
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": _object_type_schema,
                    "object_id": {"type": "integer"},
                    "source": {"type": "string", "description": "Source name"},
                    "tlp": {
                        "type": "string",
                        "description": (
                            "TLP marking: RED, AMBER, GREEN, or WHITE "
                            "(case-insensitive)"
                        ),
                    },
                },
                "required": ["object_type", "object_id", "source"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="add_threatq_tag",
            description="Tag any Threat Library object (tags power saved searches and exports).",
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": _object_type_schema,
                    "object_id": {"type": "integer"},
                    "tag": {"type": "string"},
                },
                "required": ["object_type", "object_id", "tag"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # ── 9. Platform context ─────────────────────────────────────────────
        ToolDefinition(
            name="list_threatq_tags",
            description="List Threat Library tags (with ids, needed to remove a tag from an object).",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 50},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_threatq_sources",
            description="List intelligence sources (feeds, connectors, analysts) known to the appliance.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 50},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_threatq_data_collections",
            description=(
                "List Threat Library data collections (saved searches) the API "
                "user can access, with their hashes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 25},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_threatq_data_collection",
            description=(
                "Inspect a data collection (saved search) definition by hash: "
                "targeted object types and the underlying API query."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "collection_hash": {
                        "type": "string",
                        "description": "Hash from list_threatq_data_collections",
                    }
                },
                "required": ["collection_hash"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_threatq_score_configuration",
            description=(
                "Inspect the indicator scoring algorithm configuration "
                "(weighted indicator types, sources, attributes, relations)."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="list_threatq_exports",
            description=(
                "List export feed definitions (outbound intelligence feeds "
                "consumed by SIEMs/firewalls) with enabled state and export URL."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="download_threatq_export",
            workspace_ingest=True,
            description=(
                "Generate an export feed now (GET /export/{name} with its token) "
                "and store the output as a file of the conversation (download "
                "card + file_id to forward, read inline). limit caps the records "
                "(0 = the export's default)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "exporter_id": {"type": "integer"},
                    "limit": {"type": "integer", "default": 0},
                },
                "required": ["exporter_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        # ── 10. Consolidated management ─────────────────────────────────────
        ToolDefinition(
            name="manage_threatq_object",
            description=(
                "Long-tail Threat Library management. Actions: delete_object "
                "(PERMANENT — deletes any object by collection + id), "
                "remove_tag / delete_attribute / delete_comment (target_id = "
                "the tag/attribute/comment id), unlink_objects (target_type + "
                "target_id = the relationship link id from "
                "list_threatq_related_objects), watchlist_add / "
                "watchlist_remove, delete_data_collection (collection_hash). "
                "Deletions are permanent and cannot be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "delete_object",
                            "remove_tag",
                            "delete_attribute",
                            "delete_comment",
                            "unlink_objects",
                            "watchlist_add",
                            "watchlist_remove",
                            "delete_data_collection",
                        ],
                    },
                    "object_type": _object_type_schema,
                    "object_id": {"type": "integer"},
                    "target_type": {
                        "type": "string",
                        "description": "For unlink_objects: the related collection",
                    },
                    "target_id": {
                        "type": "integer",
                        "description": "Tag/attribute/comment/link/watchlist entry id depending on action",
                    },
                    "collection_hash": {
                        "type": "string",
                        "description": "For delete_data_collection",
                    },
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
