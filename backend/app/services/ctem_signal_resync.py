# SPDX-License-Identifier: Apache-2.0

"""Step 3 of the purge of every signal (#2528, #2578): resync the signals from their sources.

Run by ``ctem_signal_purge.run_job`` only when the administrator ticked the box,
with intake still paused for every other caller (``RESYNC_BYPASS``). Each signal
goes through the same recording point as live intake, so the same exclusions,
dedupe keys and scope gate apply, with the current threshold, and the
case-opening switch stays as the administrator left it.

Every route has its own read window in days, chosen in the purge dialog
(:data:`WINDOWS`, defaults in one place, validated by :func:`validate_windows`),
and its own cap, so no type starves another (#2578: one query over every type
with one shared cap let vulnerabilities, enriched daily, fill it before any
threat actor was read):

- **World events from OpenCTI** (paths 1, 2, 10): one query and one cap per
  entity type the live listener reads, fed to ``ctem_signal_listeners.note_event``
  as an ``update`` delivery. Vulnerabilities default to 7 days, the rest to 30.
- **PIR matches** (paths 1, 2): the entities flagged in each PIR of OpenCTI, with
  their PIR score, delivered as the PIR playbook delivers them, so the relevance
  floor is computed as live.
- **Third-party events** (path 11): the reports naming a tracked organization's
  OpenCTI identity, through ``note_tracked_organizations``.
- **Inside state** (paths 3, 10): the open Aikido issues and the open Dependabot
  alerts of the connected GitHub organizations, listed by API whatever their age
  unless the administrator set a window.
- **Scheduled passes** (paths 5, 6, 7, 9, 12): the attack surface and evidence
  health passes run once now, their outcome (counts, off, skipped and why) reported.

A route that cannot be listed is reported with its reason (GitLab findings come
with the GitLab asset sync; OpenAEV delivers simulation results and score
degradations by event only). The job result counts, per route, what was read
and recorded with the window used, and per trigger path the signals and cases.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

logger = logging.getLogger(__name__)

#: OpenCTI page size, and the most entities one route reads per source.
PAGE = 200
MAX_PER_ROUTE = 5000
#: The longest window an administrator may set, in days.
MAX_WINDOW_DAYS = 365


@dataclass(frozen=True)
class Window:
    """One resync route the dialog shows: its group, its default window (``None``: no age limit)."""

    key: str
    group: str  # world | inside | pass
    label: str
    default: int | None
    #: ``False`` for a pass: it reads the current state, no window applies.
    windowed: bool = True
    #: The OpenCTI entity type a world route reads.
    entity_type: str = ""


#: Every resync route, in the dialog's order; the defaults live here only.
WINDOWS: tuple[Window, ...] = (
    Window("vulnerability", "world", "Vulnerabilities", 7, entity_type="Vulnerability"),
    Window("intrusion_set", "world", "Intrusion sets", 30, entity_type="Intrusion-Set"),
    Window("threat_actor_group", "world", "Threat actor groups", 30, entity_type="Threat-Actor-Group"),
    Window("threat_actor_individual", "world", "Threat actor individuals", 30, entity_type="Threat-Actor-Individual"),
    Window("campaign", "world", "Campaigns", 30, entity_type="Campaign"),
    Window("malware", "world", "Malware", 30, entity_type="Malware"),
    Window("user_account", "world", "Leaked user accounts", 30, entity_type="User-Account"),
    Window("email_addr", "world", "Leaked email addresses", 30, entity_type="Email-Addr"),
    Window("pir", "world", "PIR matches", 30),
    Window("third_party", "world", "Third-party and sub-processor events", 30),
    Window("aikido", "inside", "Aikido open issues", None),
    Window("github", "inside", "GitHub Dependabot alerts", None),
    Window("gitlab", "inside", "GitLab dependency findings", None),
    Window("openaev", "inside", "OpenAEV simulation results", None),
    Window("attack_surface", "pass", "Attack surface pass", None, windowed=False),
    Window("evidence_health", "pass", "Evidence health pass", None, windowed=False),
)
_BY_KEY: dict[str, Window] = {w.key: w for w in WINDOWS}

#: The OpenCTI entity types the live listener reads as entities.
WORLD_TYPES: tuple[str, ...] = tuple(w.entity_type for w in WINDOWS if w.entity_type)
#: The listener object type of each OpenCTI entity type (``ctem_signal_listeners.LISTENERS``).
_STIX_TYPE: dict[str, str] = {
    "Threat-Actor-Individual": "threat-actor",
}

#: The routes no listing exists for, with the reason the job reports.
UNLISTED: dict[str, str] = {
    "gitlab": "GitLab dependency findings are recorded by the GitLab asset sync: run a sync of the GitLab source",
    "openaev": "OpenAEV delivers simulation results and score degradations by event only; no listing API is wired",
}


class WindowError(ValueError):
    """A resync window the server refuses."""


def default_windows() -> dict[str, int | None]:
    """Every windowed route with its default (``None``: no age limit)."""
    return {w.key: w.default for w in WINDOWS if w.windowed}


def describe_windows() -> list[dict[str, Any]]:
    """The routes as the purge dialog draws them: key, group, label, default, windowed."""
    return [
        {"key": w.key, "group": w.group, "label": w.label, "default": w.default, "windowed": w.windowed}
        for w in WINDOWS
    ]


def validate_windows(raw: dict[str, Any] | None) -> dict[str, int | None]:
    """The defaults overlaid with *raw*; :class:`WindowError` on an unknown route or a bad value.

    A world route takes a whole number of days from 1 to :data:`MAX_WINDOW_DAYS`; an
    inside route takes that or ``None`` (no age limit); a pass takes no window.
    """
    out = default_windows()
    for key, value in (raw or {}).items():
        window = _BY_KEY.get(key)
        if window is None:
            raise WindowError(f"unknown resync route: {key}")
        if not window.windowed:
            raise WindowError(f"{key} reads the current state and takes no window")
        if value is None:
            if window.group != "inside":
                raise WindowError(f"{key} needs a window in days")
            out[key] = None
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_WINDOW_DAYS:
            raise WindowError(f"{key}: a whole number of days from 1 to {MAX_WINDOW_DAYS}")
        out[key] = value
    return out


def _since(now: datetime, days: int | None) -> datetime | None:
    return None if days is None else now - timedelta(days=days)


_QUERY = """
query CtemResync($types: [String], $filters: FilterGroup, $first: Int, $after: ID) {
  stixCoreObjects(types: $types, filters: $filters, first: $first, after: $after, orderBy: updated_at, orderMode: desc) {
    edges { node {
      id entity_type standard_id
      ... on Vulnerability { name x_opencti_cvss_base_score x_opencti_epss_score x_opencti_cisa_kev }
      ... on IntrusionSet { name }
      ... on ThreatActor { name }
      ... on Campaign { name }
      ... on Malware { name }
      ... on UserAccount { account_login user_id display_name is_disabled }
      ... on EmailAddr { value }
    } }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_PIRS = "query CtemResyncPirs { pirs(first: 100) { edges { node { id name } } } }"

_PIR_ENTITIES = """
query CtemResyncPir($pirId: ID!, $types: [String], $filters: FilterGroup, $first: Int, $after: ID) {
  stixDomainObjects(pirId: $pirId, types: $types, filters: $filters, first: $first, after: $after) {
    edges { node {
      id entity_type standard_id
      ... on Vulnerability { name x_opencti_cvss_base_score x_opencti_epss_score x_opencti_cisa_kev }
      ... on IntrusionSet { name }
      ... on ThreatActor { name }
      ... on Campaign { name }
      ... on Malware { name }
      pirInformation(pirId: $pirId) { pir_score }
    } }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_REPORTS = """
query CtemResyncReports($filters: FilterGroup, $first: Int, $after: ID) {
  reports(filters: $filters, first: $first, after: $after, orderBy: updated_at, orderMode: desc) {
    edges { node { id standard_id name } }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def _filters(since: datetime | None, *extra: dict[str, Any]) -> dict[str, Any]:
    filters = list(extra)
    if since is not None:
        filters.append({"key": ["updated_at"], "values": [since.isoformat()], "operator": "gt"})
    return {"mode": "and", "filters": filters, "filterGroups": []}


def delivery(node: dict[str, Any], *, pir_id: str = "") -> dict[str, Any] | None:
    """The live listener's delivery for one OpenCTI entity, ``None`` for a type it does not read.

    With *pir_id*, the PIR playbook's delivery: the entity's PIR score rides in its
    OpenCTI extension and ``xtm_source`` names the playbook, so the listener reads
    the PIR match and computes its relevance floor as live.
    """
    from app.services.ctem_signal_listeners import OPENCTI_EXTENSION

    entity_type = str(node.get("entity_type") or "")
    if entity_type not in WORLD_TYPES or not node.get("id"):
        return None
    skipped = ("id", "entity_type", "standard_id", "pirInformation")
    data: dict[str, Any] = {k: v for k, v in node.items() if k not in skipped and v not in (None, "")}
    data["type"] = _STIX_TYPE.get(entity_type, entity_type.lower())
    data["id"] = node.get("standard_id") or node["id"]
    extension: dict[str, Any] = {"id": node["id"]}
    payload: dict[str, Any] = {"type": "update", "data": data}
    if pir_id:
        info = node.get("pirInformation") or {}
        info = info[0] if isinstance(info, list) and info else info
        score = info.get("pir_score") if isinstance(info, dict) else None
        if isinstance(score, int | float) and not isinstance(score, bool):
            extension["pir_information"] = [{"pir_id": pir_id, "pir_score": score}]
        payload["xtm_source"] = "pir_playbook"
    data["extensions"] = {OPENCTI_EXTENSION: extension}
    payload["_opencti_event"] = {"message": "resync after a purge of every signal"}
    return payload


def _route(key: str, windows: dict[str, int | None]) -> dict[str, Any]:
    return {"window_days": windows.get(key), "read": 0, "recorded": 0, "errors": []}


async def _opencti_sources(db: Any) -> list[Any]:
    from app.models.ctem_signal_source import CtemSignalSource

    rows = (await db.execute(select(CtemSignalSource).where(CtemSignalSource.provider == "opencti"))).scalars()
    seen: set[str] = set()
    out = []
    for row in rows:
        if row.enabled and str(row.integration_id) not in seen:
            seen.add(str(row.integration_id))
            out.append(row)
    return out


async def _opencti_connections(sessions: Any) -> tuple[list[Any], list[str]]:
    from app.services import opencti_trigger_provisioning as prov

    connections, errors = [], []
    async with sessions() as db:
        for row in await _opencti_sources(db):
            conn = await prov.integration_connection(db, str(row.integration_id), row.run_as_user_id)
            if isinstance(conn, str):
                errors.append(conn)
            else:
                connections.append(conn[0])
    return connections, errors


async def _pages(gql: Any, query: str, variables: dict[str, Any], root: str, out: dict[str, Any]) -> Any:
    """Yield the nodes of *root*, page by page, up to :data:`MAX_PER_ROUTE`; an error lands in *out*."""
    after: str | None = None
    taken = 0
    while taken < MAX_PER_ROUTE:
        data, err = await gql(query, {**variables, "first": PAGE, "after": after})
        if err:
            out["errors"].append(f"OpenCTI: {err}"[:300])
            return
        block = (data or {}).get(root) or {}
        for edge in block.get("edges") or []:
            node = (edge or {}).get("node") or {}
            taken += 1
            yield node
        page = block.get("pageInfo") or {}
        if not page.get("hasNextPage") or not page.get("endCursor"):
            return
        after = page["endCursor"]
    out["capped"] = True


async def resync_world(
    sessions: Any,
    *,
    windows: dict[str, int | None] | None = None,
    now: datetime | None = None,
    note: Callable[..., Awaitable[bool]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Each OpenCTI entity type with its own window and cap, from every enabled OpenCTI source."""
    from app.services.ctem_signal_listeners import note_event

    notify = note or note_event
    windows = windows or default_windows()
    at = now or datetime.now(UTC)
    connections, errors = await _opencti_connections(sessions)
    routes: dict[str, dict[str, Any]] = {}
    for window in WINDOWS:
        if not window.entity_type:
            continue
        out = routes[window.key] = _route(window.key, windows)
        out["errors"] += errors
        variables = {"types": [window.entity_type], "filters": _filters(_since(at, windows.get(window.key)))}
        for gql in connections:
            async for node in _pages(gql, _QUERY, variables, "stixCoreObjects", out):
                payload = delivery(node)
                if payload is None:
                    continue
                out["read"] += 1
                if await notify("opencti", "update", payload, verified=True):
                    out["recorded"] += 1
    return routes


async def resync_pir(
    sessions: Any,
    *,
    windows: dict[str, int | None] | None = None,
    now: datetime | None = None,
    note: Callable[..., Awaitable[bool]] | None = None,
) -> dict[str, Any]:
    """The entities flagged in each PIR, delivered as the PIR playbook delivers them."""
    from app.integrations.providers.opencti.pir import _build_pir_context_filters
    from app.services.ctem_signal_listeners import note_event
    from app.services.ctem_signal_sources import PIR_PATHS

    notify = note or note_event
    windows = windows or default_windows()
    out = _route("pir", windows)
    since = _since(now or datetime.now(UTC), windows.get("pir"))
    types = sorted({t for entity_types in PIR_PATHS.values() for t in entity_types})
    connections, errors = await _opencti_connections(sessions)
    out["errors"] += errors
    for gql in connections:
        data, err = await gql(_PIRS, {})
        if err:
            out["errors"].append(f"OpenCTI PIRs: {err}"[:300])
            continue
        pirs = [((e or {}).get("node") or {}).get("id") for e in ((data or {}).get("pirs") or {}).get("edges") or []]
        for pir_id in [p for p in pirs if p]:
            scope = _filters(since) if since is not None else None
            variables = {"pirId": pir_id, "types": types, "filters": _build_pir_context_filters(pir_id, scope)}
            async for node in _pages(gql, _PIR_ENTITIES, variables, "stixDomainObjects", out):
                payload = delivery(node, pir_id=pir_id)
                if payload is None:
                    continue
                out["read"] += 1
                if await notify("opencti", "update", payload, verified=True):
                    out["recorded"] += 1
    return out


async def resync_third_party(
    sessions: Any,
    *,
    windows: dict[str, int | None] | None = None,
    now: datetime | None = None,
    note: Callable[..., Awaitable[int]] | None = None,
) -> dict[str, Any]:
    """Path 11: the reports naming a tracked organization's OpenCTI identity, one signal per organization."""
    from app.services.ctem_signal_listeners import OPENCTI_EXTENSION, note_tracked_organizations
    from app.services.organizations import list_organizations

    notify = note or note_tracked_organizations
    windows = windows or default_windows()
    out = _route("third_party", windows)
    since = _since(now or datetime.now(UTC), windows.get("third_party"))
    async with sessions() as db:
        tracked = [
            (str(o.id), o.opencti_organization_id)
            for o in await list_organizations(db)
            if o.role != "primary" and o.opencti_organization_id
        ]
    if not tracked:
        out["skipped"] = "no tracked organization is linked to an OpenCTI organization"
        return out
    connections, errors = await _opencti_connections(sessions)
    out["errors"] += errors
    for gql in connections:
        for org_id, opencti_id in tracked:
            named = {"key": ["objects"], "values": [opencti_id], "operator": "eq"}
            variables = {"filters": _filters(since, named)}
            async for node in _pages(gql, _REPORTS, variables, "reports", out):
                if not node.get("id"):
                    continue
                out["read"] += 1
                data = {
                    "type": "report",
                    "id": node.get("standard_id") or node["id"],
                    "name": node.get("name") or "",
                    "extensions": {OPENCTI_EXTENSION: {"id": node["id"]}},
                }
                out["recorded"] += await notify({"type": "update", "data": data}, [org_id], verified=True)
    return out


def _issue_time(item: dict[str, Any], keys: tuple[str, ...]) -> datetime | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
            return datetime.fromtimestamp(value, UTC)
        if isinstance(value, str) and value:
            try:
                when = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            return when if when.tzinfo else when.replace(tzinfo=UTC)
    return None


def _in_window(item: dict[str, Any], since: datetime | None, keys: tuple[str, ...]) -> bool:
    if since is None:
        return True
    when = _issue_time(item, keys)
    return when is None or when >= since


async def _integrations(sessions: Any, provider: str, actor_id: Any) -> list[Any]:
    """The *provider* integrations the administrator who started the purge may see (none without one)."""
    from app.api.v1.helpers import integration_visible_filter
    from app.models.integration import Integration

    if actor_id is None:
        return []
    async with sessions() as db:
        query = select(Integration).where(
            Integration.provider == provider, await integration_visible_filter(db, actor_id)
        )
        return list((await db.execute(query)).scalars())


async def _token(sessions: Any, integration: Any, provider: str, actor_id: Any) -> str | None:
    """A token for the purge's administrator on one integration *integrations* returned to them."""
    from app.services.integration_tools import _get_token

    async with sessions() as db:
        return await _get_token(db, str(integration.id), provider, user_id=str(actor_id), integ=integration)


async def resync_aikido(
    sessions: Any,
    *,
    actor_id: Any = None,
    windows: dict[str, int | None] | None = None,
    now: datetime | None = None,
    note: Callable[..., Awaitable[bool]] | None = None,
) -> dict[str, Any]:
    """Paths 3 and 10: the open issues of every Aikido integration, as their ``issue.open.created`` delivery."""
    from app.integrations.providers.aikido._client import AikidoClient, parse_aikido_token
    from app.services.ctem_signal_listeners import note_event

    notify = note or note_event
    windows = windows or default_windows()
    out = _route("aikido", windows)
    since = _since(now or datetime.now(UTC), windows.get("aikido"))
    integrations = await _integrations(sessions, "aikido", actor_id)
    if not integrations:
        out["skipped"] = "no Aikido integration is visible to the administrator who started the purge"
        return out
    for integration in integrations:
        token = await _token(sessions, integration, "aikido", actor_id)
        if not token:
            out["errors"].append(f"Aikido {integration.name}: no credential")
            continue
        client = AikidoClient(parse_aikido_token(token))
        resp, err = await client.request(
            "GET", "/issues/export", "export the open issues", params={"filter_status": "open", "format": "json"}
        )
        if err or resp is None:
            out["errors"].append(f"Aikido {integration.name}: {err or 'no answer'}"[:300])
            continue
        issues = resp.data if isinstance(resp.data, list) else []
        for issue in issues[:MAX_PER_ROUTE]:
            if not isinstance(issue, dict) or not _in_window(
                issue, since, ("first_detected_at", "created_at", "detected_at")
            ):
                continue
            out["read"] += 1
            payload = {"event_type": "issue.open.created", "payload": issue}
            if await notify("aikido", "issue_open_created", payload, verified=True):
                out["recorded"] += 1
    return out


def github_organizations(integration: Any) -> list[str]:
    """The organization logins a GitHub integration is installed on, read per installation.

    Each installation's login and account type are read together (the binding metadata
    sorts the two lists apart, so pairing them would mismatch); ``[]`` on any read failure.
    """
    from app.services.integration_tools import _decrypt_dict

    if not getattr(integration, "extra_config_enc", None):
        return []
    try:
        installs = _decrypt_dict(integration.extra_config_enc).get("installations") or []
    except Exception:
        logger.info("CTEM resync: GitHub installations not read", exc_info=True)
        return []
    logins = [
        str(inst.get("account_login") or "").strip()
        for inst in installs
        if isinstance(inst, dict) and str(inst.get("account_type") or "").lower() == "organization"
    ]
    return list(dict.fromkeys(login for login in logins if login))


async def resync_github(
    sessions: Any,
    *,
    actor_id: Any = None,
    windows: dict[str, int | None] | None = None,
    now: datetime | None = None,
    note: Callable[..., Awaitable[bool]] | None = None,
) -> dict[str, Any]:
    """Path 3: the open Dependabot alerts of each GitHub organization an integration is installed on."""
    from app.core.egress import egress_client
    from app.integrations.providers.github._common import _BASE_URL, _PROVIDER, _headers, safe_request
    from app.services.ctem_signal_listeners import note_event

    notify = note or note_event
    windows = windows or default_windows()
    out = _route("github", windows)
    since = _since(now or datetime.now(UTC), windows.get("github"))
    integrations = await _integrations(sessions, "github", actor_id)
    if not integrations:
        out["skipped"] = "no GitHub integration is visible to the administrator who started the purge"
        return out
    for integration in integrations:
        logins = github_organizations(integration)
        if not logins:
            out["errors"].append(f"GitHub {integration.name}: installed on no organization")
            continue
        token = await _token(sessions, integration, "github", actor_id)
        if not token:
            out["errors"].append(f"GitHub {integration.name}: no credential")
            continue
        async with egress_client(timeout=30) as client:
            for login in logins:
                url: str | None = f"{_BASE_URL}/orgs/{login}/dependabot/alerts?state=open&per_page=100"
                while url and out["read"] < MAX_PER_ROUTE:
                    resp, err = await safe_request(
                        client, "get", url, _PROVIDER, f"list the Dependabot alerts of {login}", headers=_headers(token)
                    )
                    if err or resp is None:
                        out["errors"].append(f"GitHub {login}: {err or 'no answer'}"[:300])
                        break
                    for alert in resp.json() or []:
                        if not isinstance(alert, dict) or not _in_window(alert, since, ("created_at",)):
                            continue
                        out["read"] += 1
                        payload = {"action": "reopened", "alert": alert, "repository": alert.get("repository") or {}}
                        if await notify("github", "dependabot_alert.reopened", payload, verified=True):
                            out["recorded"] += 1
                    url = (getattr(resp, "links", None) or {}).get("next", {}).get("url")
    return out


def pass_outcome(result: Any) -> dict[str, Any]:
    """A pass's answer as the job reports it: its counts, or why it raised nothing."""
    if result is None:
        return {"recorded": 0, "skipped": "off in the profile, or no owned domain or range to read"}
    if not isinstance(result, dict):
        return {"recorded": 0}
    out = dict(result)
    out.setdefault("recorded", 0)
    if not out.get("skipped") and not out.get("error") and not out["recorded"]:
        out["note"] = "the pass ran and found no difference to raise"
    return out


async def resync_passes(sessions: Any) -> dict[str, Any]:
    """The attack surface (paths 5, 6, 7) and evidence health (9, 12) passes, once each, now."""
    from app.services import ctem_attack_surface, ctem_evidence_health

    out: dict[str, Any] = {}
    for name, module in (("attack_surface", ctem_attack_surface), ("evidence_health", ctem_evidence_health)):
        await module.release_pass_slot()
        async with sessions() as db:
            try:
                out[name] = pass_outcome(await module.scheduled_pass(db))
            except Exception as exc:
                await db.rollback()
                logger.warning("CTEM resync: the %s pass failed", name, exc_info=True)
                out[name] = {"recorded": 0, "error": f"{type(exc).__name__}: {exc}"[:300]}
    return out


async def by_trigger_path(sessions: Any) -> dict[str, dict[str, int]]:
    """Per trigger path, the signals the resync recorded and the cases they opened or joined."""
    from app.models.ctem_signal import TRIGGER_PATHS, CtemSignal

    out = {p: {"signals": 0, "cases": 0} for p in TRIGGER_PATHS}
    async with sessions() as db:
        rows = await db.execute(
            select(CtemSignal.trigger_path, func.count(), func.count(func.distinct(CtemSignal.case_id))).group_by(
                CtemSignal.trigger_path
            )
        )
        for path, signals, cases in rows.all():
            if path in out:
                out[path] = {"signals": int(signals), "cases": int(cases)}
    return out


async def resync(
    *,
    sessions: Any,
    actor_id: uuid.UUID,
    progress: Callable[[str, int], Awaitable[None]],
    windows: dict[str, int | None] | None = None,
) -> dict[str, Any]:
    """Step 3: every route with its window, then the passes; counts per route and per trigger path."""
    windows = validate_windows(windows)
    routes: dict[str, Any] = {}
    steps: tuple[tuple[str, Callable[..., Awaitable[Any]]], ...] = (
        ("world", resync_world),
        ("pir", resync_pir),
        ("third_party", resync_third_party),
        ("aikido", resync_aikido),
        ("github", resync_github),
    )
    # The inside-state routes read with the administrator's own access to each integration.
    as_actor = {"aikido", "github"}
    for name, step in steps:
        try:
            extra = {"actor_id": actor_id} if name in as_actor else {}
            answer = await step(sessions, windows=windows, **extra)
            found = answer if name == "world" else {name: answer}
        except Exception as exc:
            logger.warning("CTEM resync: the %s route failed", name, exc_info=True)
            found = {name: {**_route(name, windows), "errors": [f"{type(exc).__name__}: {exc}"[:300]]}}
        for key, counts in found.items():
            routes[key] = counts
            await progress(key, int(counts.get("recorded") or 0))
    for key, reason in UNLISTED.items():
        routes[key] = {**_route(key, windows), "skipped": reason}
    passes = await resync_passes(sessions)
    for name, counts in passes.items():
        routes[name] = {"window_days": None, **counts}
        await progress(name, int(counts.get("recorded") or 0))
    return {"windows": windows, "routes": routes, "by_path": await by_trigger_path(sessions)}
