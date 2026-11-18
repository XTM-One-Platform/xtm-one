# SPDX-License-Identifier: Apache-2.0

"""MITRE ATT&CK integration — the adversary tactics & techniques knowledge base.

Full-fledged agentic access to the MITRE ATT&CK® knowledge base
(https://attack.mitre.org) for CTI analysts, SOC teams, detection engineers,
and red teams. ATT&CK is free and public — this integration needs NO API key.

Data source: the official STIX 2.1 bundles published by MITRE in the
``mitre-attack/attack-stix-data`` GitHub repository (the same dataset served
by the official TAXII 2.1 server at ``attack-taxii.mitre.org``). Bundles are
downloaded on first use, indexed in-process (by ATT&CK ID, STIX ID, type,
name/alias, and relationship), and cached with a 24h refresh for the rolling
"latest" release. Air-gapped deployments can point ``source_url`` at any
mirror that serves the same ``{domain}/{domain}.json`` layout.

Domains (each is a separate STIX collection):
  - ``enterprise-attack`` — Windows/macOS/Linux/cloud/network/containers
  - ``mobile-attack``     — Android / iOS
  - ``ics-attack``        — industrial control systems

Object model (ATT&CK type → STIX type → ATT&CK ID prefix):
  - technique / sub-technique → ``attack-pattern``          → T#### / T####.###
  - tactic                    → ``x-mitre-tactic``          → TA####
  - group (intrusion set)     → ``intrusion-set``           → G####
  - software                  → ``malware`` / ``tool``      → S####
  - mitigation                → ``course-of-action``        → M####
  - campaign                  → ``campaign``                → C####
  - data source               → ``x-mitre-data-source``     → DS####
  - data component            → ``x-mitre-data-component``  → DC####
  - asset (ICS)               → ``x-mitre-asset``           → A####
  - detection strategy        → ``x-mitre-detection-strategy`` → DET####
  - analytic                  → ``x-mitre-analytic``        → AN####

Relationship traversal (``relationship`` objects) powers the classic ATT&CK
questions: techniques used by a group (``uses``, incl. procedure-example
descriptions), groups/software/campaigns using a technique, mitigations for a
technique (``mitigates``), detection strategies for a technique (``detects``),
sub-technique/parent links (``subtechnique-of``), campaigns attributed to a
group (``attributed-to``), techniques targeting ICS assets (``targets``), and
replacement objects (``revoked-by``).

The knowledge base is read-only (the official TAXII collections advertise
``can_write: false``), so every tool here is a read.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import format_network_error, parse_compound_token
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "MITRE ATT&CK"
_SOURCE = "integration:mitre_attack"

#: Official STIX 2.1 dataset (master branch always carries the latest release).
DEFAULT_SOURCE_URL = (
    "https://raw.githubusercontent.com/mitre-attack/attack-stix-data/master"
)

#: The three ATT&CK domains (collection names double as file names).
ATTACK_DOMAINS: tuple[str, ...] = ("enterprise-attack", "mobile-attack", "ics-attack")

_DOMAIN_ALIASES: dict[str, str] = {
    "enterprise": "enterprise-attack",
    "enterprise-attack": "enterprise-attack",
    "mobile": "mobile-attack",
    "mobile-attack": "mobile-attack",
    "ics": "ics-attack",
    "ics-attack": "ics-attack",
}

#: Friendly object-type names (exposed in tool enums) → STIX types.
OBJECT_TYPES: dict[str, tuple[str, ...]] = {
    "techniques": ("attack-pattern",),
    "tactics": ("x-mitre-tactic",),
    "groups": ("intrusion-set",),
    "software": ("malware", "tool"),
    "mitigations": ("course-of-action",),
    "campaigns": ("campaign",),
    "data_sources": ("x-mitre-data-source",),
    "data_components": ("x-mitre-data-component",),
    "assets": ("x-mitre-asset",),
    "detection_strategies": ("x-mitre-detection-strategy",),
    "analytics": ("x-mitre-analytic",),
}

_STIX_TO_FRIENDLY: dict[str, str] = {
    "attack-pattern": "technique",
    "x-mitre-tactic": "tactic",
    "intrusion-set": "group",
    "malware": "software (malware)",
    "tool": "software (tool)",
    "course-of-action": "mitigation",
    "campaign": "campaign",
    "x-mitre-data-source": "data source",
    "x-mitre-data-component": "data component",
    "x-mitre-asset": "asset",
    "x-mitre-detection-strategy": "detection strategy",
    "x-mitre-analytic": "analytic",
    "x-mitre-matrix": "matrix",
}

#: Relationship types indexed for the ATT&CK pivots. Anything outside this
#: whitelist is dropped at index time — no tool can reach it, so keeping it
#: would only inflate the in-memory store.
_RELATIONSHIP_TYPES: frozenset[str] = frozenset(
    {
        "uses",
        "mitigates",
        "detects",
        "subtechnique-of",
        "attributed-to",
        "targets",
        "revoked-by",
    }
)

#: ATT&CK external IDs — e.g. T1059, T1059.001, TA0002, G0016, S0154, M1038,
#: C0028, DS0029, DC0045, A0007, DET0903, AN1881.
_ATTACK_ID_RE = re.compile(r"^(?:T\d{4}(?:\.\d{3})?|TA\d{4}|[GSMCA]\d{4}|DS\d{4}|DC\d{4}|DET\d{4}|AN\d{4})$")
_STIX_ID_RE = re.compile(r"^[a-z0-9-]+--[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_VERSION_RE = re.compile(r"^\d+\.\d+$")

#: Refresh window for the rolling "latest" bundles (pinned versions never expire).
_CACHE_TTL_SECONDS = 24 * 60 * 60
#: Back-off before retrying a failed refresh while serving a stale cache —
#: prevents every tool call from hammering an unhealthy source.
_STALE_RETRY_SECONDS = 15 * 60
#: Max domain stores kept in memory (least recently used evicted first).
_CACHE_MAX_STORES = 4
#: Bound stored description sizes so a full Enterprise bundle stays lean.
_MAX_OBJ_DESCRIPTION = 6000
_MAX_REL_DESCRIPTION = 800
_HTTP_TIMEOUT = 180
#: Hard cap on a fetched bundle body. The largest official bundle (Enterprise)
#: is ~50 MB, so anything wildly beyond that means a misconfigured or hostile
#: mirror — reject it before the CPU-heavy json.loads + index build (same
#: guard as the MITRE ATLAS provider's _MAX_DATASET_BYTES).
_MAX_BUNDLE_BYTES = 300 * 1024 * 1024


def normalize_domain(value: str) -> str:
    """Map a domain shorthand (``enterprise``/``mobile``/``ics``) to its full name.

    Returns ``""`` for unrecognized values.
    """
    return _DOMAIN_ALIASES.get(str(value or "").strip().lower(), "")


def parse_attack_config(token: str) -> tuple[list[str], str, str]:
    """Parse the compound config token ``domains||version||source_url``.

    Lenient by design — the integration is credential-less, so a malformed or
    dummy token (catalog introspection) falls back to sane defaults instead of
    disabling the tools:
      - domains: comma-separated; unknown entries dropped; empty → all three.
      - version: must look like ``19.1``; anything else → latest.
      - source_url: must be http(s); anything else → the official GitHub repo.
    """
    raw_domains, raw_version, raw_source = parse_compound_token(token or "", 3)
    domains = [d for d in (normalize_domain(p) for p in raw_domains.split(",")) if d]
    # Preserve order, drop duplicates.
    domains = list(dict.fromkeys(domains)) or list(ATTACK_DOMAINS)
    version = raw_version.strip().lstrip("vV")
    if not _VERSION_RE.fullmatch(version):
        version = ""
    source = sanitize_source_url(raw_source) or DEFAULT_SOURCE_URL
    return domains, version, source


def sanitize_source_url(raw: str) -> str:
    """Validate + sanitize a custom mirror URL (it can surface in tool output).

    Only http(s), no userinfo, no control characters; query and fragment are
    stripped. Returns ``""`` when the value is unusable (caller falls back to
    the official source). Shared with the connectivity test so "Test
    Connection" probes exactly the URL the tools will use at runtime.
    """
    candidate = str(raw or "").strip()
    if not candidate or any(ord(c) < 33 or ord(c) == 127 for c in candidate):
        return ""
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc or "@" in parts.netloc:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")).rstrip("/")


def bundle_url(source_url: str, domain: str, version: str) -> str:
    """URL of a domain's STIX bundle (latest, or a pinned release)."""
    if version:
        return f"{source_url}/{domain}/{domain}-{version}.json"
    return f"{source_url}/{domain}/{domain}.json"


def index_url(source_url: str) -> str:
    """URL of the collection index (small JSON listing domains + versions)."""
    return f"{source_url}/index.json"


# ---------------------------------------------------------------------------
# In-process ATT&CK data store (one per domain+version+source)
# ---------------------------------------------------------------------------


@dataclass
class _AttackStore:
    """Indexed, slimmed view of one ATT&CK domain bundle."""

    domain: str
    version: str  # collection version actually loaded (e.g. "19.1")
    fetched_at: float
    pinned: bool  # True when loaded from an explicit version pin
    objects: dict[str, dict] = field(default_factory=dict)  # stix_id → slim obj
    by_attack_id: dict[str, str] = field(default_factory=dict)  # ATT&CK id → stix_id
    by_name: dict[str, list[str]] = field(default_factory=dict)  # lowered name/alias → stix_ids
    by_type: dict[str, list[str]] = field(default_factory=dict)  # stix type → ids
    rels_by_source: dict[str, list[dict]] = field(default_factory=dict)
    rels_by_target: dict[str, list[dict]] = field(default_factory=dict)
    tactic_order: dict[str, int] = field(default_factory=dict)  # tactic stix_id → order
    #: When a refresh fails and this stale store is served instead, the next
    #: retry is deferred until this monotonic deadline (anti-hammering).
    retry_after: float = 0.0
    #: Last time a tool call used this store — drives LRU cache eviction
    #: (``fetched_at`` stays load-time-only, driving staleness).
    last_used: float = 0.0

    def touch(self) -> None:
        self.last_used = time.monotonic()

    @property
    def is_stale(self) -> bool:
        if self.pinned:
            return False
        return (time.monotonic() - self.fetched_at) > _CACHE_TTL_SECONDS

    @property
    def needs_refresh(self) -> bool:
        return self.is_stale and time.monotonic() >= self.retry_after

    def defer_retry(self) -> None:
        """Serve stale for a while before the next refresh attempt."""
        self.retry_after = time.monotonic() + _STALE_RETRY_SECONDS


_STORE_CACHE: dict[str, _AttackStore] = {}
_STORE_LOCKS: dict[str, asyncio.Lock] = {}


def _extract_attack_ref(obj: dict) -> tuple[str, str]:
    """Return ``(attack_id, attack_url)`` from an object's external references."""
    for ref in obj.get("external_references") or []:
        source = ref.get("source_name", "")
        if source in ("mitre-attack", "mitre-mobile-attack", "mitre-ics-attack"):
            return ref.get("external_id", ""), ref.get("url", "")
    return "", ""


_KEEP_FIELDS = (
    "name",
    "aliases",
    "x_mitre_aliases",
    "x_mitre_platforms",
    "x_mitre_is_subtechnique",
    "x_mitre_deprecated",
    "x_mitre_shortname",
    "x_mitre_version",
    "x_mitre_detection",
    "x_mitre_data_sources",
    "x_mitre_permissions_required",
    "x_mitre_collection_layers",
    "x_mitre_sectors",
    "x_mitre_contributors",
    "revoked",
    "labels",
    "is_family",
    "first_seen",
    "last_seen",
    "created",
    "modified",
    "tactic_refs",
)


def _slim_object(obj: dict) -> dict:
    """Keep only the fields the tools render — bounds the in-memory footprint."""
    slim: dict[str, Any] = {"id": obj["id"], "type": obj["type"]}
    for key in _KEEP_FIELDS:
        if key in obj:
            slim[key] = obj[key]
    desc = obj.get("description") or ""
    if desc:
        slim["description"] = desc[:_MAX_OBJ_DESCRIPTION]
    if "kill_chain_phases" in obj:
        slim["kill_chain_phases"] = [
            {
                "kill_chain_name": p.get("kill_chain_name", ""),
                "phase_name": p.get("phase_name", ""),
            }
            for p in obj["kill_chain_phases"]
        ]
    attack_id, attack_url = _extract_attack_ref(obj)
    if attack_id:
        slim["attack_id"] = attack_id
    if attack_url:
        slim["attack_url"] = attack_url
    return slim


def _build_store(
    domain: str, requested_version: str, bundle: dict
) -> _AttackStore:
    """Index a raw STIX bundle into an :class:`_AttackStore` (CPU-bound)."""
    store = _AttackStore(
        domain=domain,
        version=requested_version,
        fetched_at=time.monotonic(),
        pinned=bool(requested_version),
    )
    matrices: list[dict] = []
    for obj in bundle.get("objects") or []:
        obj_type = obj.get("type", "")
        if obj_type == "x-mitre-collection":
            store.version = obj.get("x_mitre_version", store.version) or store.version
            continue
        if obj_type == "relationship":
            rel_type = obj.get("relationship_type", "")
            # Only index the relationship types the ATT&CK pivots expose —
            # anything else (e.g. legacy "related-to") would just bloat memory.
            if rel_type not in _RELATIONSHIP_TYPES:
                continue
            rel = {
                "source_ref": obj.get("source_ref", ""),
                "target_ref": obj.get("target_ref", ""),
                "relationship_type": rel_type,
                "deprecated": bool(
                    obj.get("x_mitre_deprecated") or obj.get("revoked")
                ),
            }
            rel_desc = (obj.get("description") or "")[:_MAX_REL_DESCRIPTION]
            if rel_desc:
                rel["description"] = rel_desc
            store.rels_by_source.setdefault(rel["source_ref"], []).append(rel)
            store.rels_by_target.setdefault(rel["target_ref"], []).append(rel)
            continue
        if obj_type in ("identity", "marking-definition"):
            continue
        slim = _slim_object(obj)
        store.objects[slim["id"]] = slim
        store.by_type.setdefault(obj_type, []).append(slim["id"])
        attack_id = slim.get("attack_id", "")
        if attack_id:
            store.by_attack_id[attack_id.upper()] = slim["id"]
        for label in {slim.get("name", ""), *_aliases_of(slim)}:
            if label:
                store.by_name.setdefault(label.lower(), []).append(slim["id"])
        if obj_type == "x-mitre-matrix":
            matrices.append(slim)
    # Tactic display order comes from the matrix object's tactic_refs.
    order = 0
    for matrix in matrices:
        for tactic_ref in matrix.get("tactic_refs") or []:
            if tactic_ref not in store.tactic_order:
                store.tactic_order[tactic_ref] = order
                order += 1
    return store


async def _get_store(domain: str, version: str, source_url: str) -> tuple[_AttackStore | None, str | None]:
    """Return the cached (or freshly downloaded + indexed) store for a domain.

    Returns ``(store, None)`` on success or ``(None, error_string)`` on failure.
    """
    cache_key = f"{source_url}|{domain}|{version or 'latest'}"
    cached = _STORE_CACHE.get(cache_key)
    if cached and not cached.needs_refresh:
        cached.touch()
        return cached, None

    lock = _STORE_LOCKS.setdefault(cache_key, asyncio.Lock())
    async with lock:
        cached = _STORE_CACHE.get(cache_key)
        if cached and not cached.needs_refresh:
            cached.touch()
            return cached, None
        url = bundle_url(source_url, domain, version)
        try:
            async with egress_client(
                timeout=_HTTP_TIMEOUT, follow_redirects=True
            ) as client:
                resp = await client.get(url)
        except httpx.HTTPError as exc:
            # A stale cached copy beats a hard failure when the source is down;
            # defer the next retry so callers don't hammer an unhealthy source.
            if cached:
                cached.defer_retry()
                cached.touch()
                logger.warning(
                    "MITRE ATT&CK refresh for %s failed (%s) — serving stale cache",
                    domain,
                    format_exception_message(exc),
                )
                return cached, None
            return None, format_network_error(exc, _PROVIDER, f"download {domain} bundle")
        if resp.status_code != 200:
            if cached:
                cached.defer_retry()
                cached.touch()
                logger.warning(
                    "MITRE ATT&CK refresh for %s returned HTTP %s — serving stale cache",
                    domain,
                    resp.status_code,
                )
                return cached, None
            hint = (
                " Check the pinned 'version' — releases are listed in the collection index."
                if version
                else ""
            )
            return None, (
                f"MITRE ATT&CK error while downloading the {domain} bundle: "
                f"HTTP {resp.status_code} for {url}.{hint}"
            )
        if len(resp.content) > _MAX_BUNDLE_BYTES:
            if cached:
                cached.defer_retry()
                cached.touch()
                logger.warning(
                    "MITRE ATT&CK refresh for %s returned an oversized body "
                    "(%d MB) — serving stale cache",
                    domain,
                    len(resp.content) // (1024 * 1024),
                )
                return cached, None
            return None, (
                f"MITRE ATT&CK error: the {domain} bundle at {url} is "
                f"{len(resp.content) // (1024 * 1024)} MB — far larger than any "
                f"official ATT&CK release (~50 MB). Check the integration's "
                f"source_url: it must point to a mirror of the "
                f"mitre-attack/attack-stix-data repository."
            )
        try:
            # json.loads + indexing on a 50 MB bundle is CPU-bound — keep the
            # event loop responsive by doing both in a worker thread.
            store = await asyncio.to_thread(
                lambda: _build_store(domain, version, json.loads(resp.content))
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            if cached:
                cached.defer_retry()
                cached.touch()
                logger.warning(
                    "MITRE ATT&CK refresh for %s returned an invalid bundle (%s) "
                    "— serving stale cache",
                    domain,
                    format_exception_message(exc),
                )
                return cached, None
            return None, (
                f"MITRE ATT&CK error: the {domain} bundle at {url} is not a valid "
                f"STIX bundle ({exc})."
            )
        store.touch()
        _STORE_CACHE[cache_key] = store
        # Bound total memory: evict the least recently used stores beyond the
        # cap (a hot store loaded long ago survives; ``fetched_at`` only
        # drives staleness), and prune their idle locks so _STORE_LOCKS stays
        # bounded too.
        while len(_STORE_CACHE) > _CACHE_MAX_STORES:
            lru_key = min(_STORE_CACHE, key=lambda k: _STORE_CACHE[k].last_used)
            del _STORE_CACHE[lru_key]
            evicted_lock = _STORE_LOCKS.get(lru_key)
            if evicted_lock is not None and not evicted_lock.locked():
                del _STORE_LOCKS[lru_key]
        logger.info(
            "MITRE ATT&CK %s v%s loaded: %d objects, %d relationship links",
            domain,
            store.version or "?",
            len(store.objects),
            sum(len(v) for v in store.rels_by_source.values()),
        )
        return store, None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _is_active(obj: dict) -> bool:
    return not (obj.get("x_mitre_deprecated") or obj.get("revoked"))


def _trunc(text: Any, max_len: int = 300) -> str:
    text = str(text or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _first_sentence(text: str, max_len: int = 220) -> str:
    text = str(text or "").strip()
    period = text.find(". ")
    if 0 < period < max_len:
        return text[: period + 1]
    return _trunc(text, max_len)


def _obj_label(obj: dict) -> str:
    """``T1059 Command and Scripting Interpreter (technique)`` style label."""
    attack_id = obj.get("attack_id", "")
    friendly = _STIX_TO_FRIENDLY.get(obj.get("type", ""), obj.get("type", "?"))
    prefix = f"{attack_id} " if attack_id else ""
    flags = ""
    if obj.get("revoked"):
        flags = " [REVOKED]"
    elif obj.get("x_mitre_deprecated"):
        flags = " [DEPRECATED]"
    return f"{prefix}{obj.get('name', '?')} ({friendly}){flags}"


def _summary_line(obj: dict) -> str:
    line = f"  - {_obj_label(obj)}"
    desc = obj.get("description", "")
    if desc:
        line += f": {_first_sentence(desc)}"
    return line


def _aliases_of(obj: dict) -> list[str]:
    aliases = obj.get("aliases") or obj.get("x_mitre_aliases") or []
    name = obj.get("name", "")
    return [a for a in aliases if a != name]


@dataclass(frozen=True)
class AttackEntry:
    """An ATT&CK object as a name lookup answers it."""

    attack_id: str
    name: str
    stix_type: str
    aliases: tuple[str, ...]


#: The ATT&CK object types a name may resolve to (techniques, groups, software, campaigns).
_LINKABLE_TYPES = frozenset({"attack-pattern", "intrusion-set", "malware", "tool", "campaign"})


async def lookup_attack_names(
    names: list[str], *, domain: str = "enterprise-attack", source_url: str = DEFAULT_SOURCE_URL, version: str = ""
) -> dict[str, list[AttackEntry]] | None:
    """The live ATT&CK objects each name (case-insensitive) is the name or an alias of,
    from the same cached store the integration's tools read; ``None`` when the store
    cannot be loaded (an air-gapped deployment without a mirror), so a caller degrades
    instead of failing. Revoked and deprecated objects are never answered."""
    store, error = await _get_store(normalize_domain(domain), version, source_url)
    if store is None:
        logger.info("ATT&CK lookup unavailable: %s", error)
        return None
    found: dict[str, list[AttackEntry]] = {}
    for name in names:
        key = name.strip().lower()
        if not key or key in found:
            continue
        entries = []
        for stix_id in store.by_name.get(key, []):
            obj = store.objects.get(stix_id) or {}
            if obj.get("type") not in _LINKABLE_TYPES or not _is_active(obj):
                continue
            if obj.get("attack_id"):
                entries.append(
                    AttackEntry(obj["attack_id"], obj.get("name", ""), obj["type"], tuple(_aliases_of(obj)))
                )
        found[key] = entries
    return found


@dataclass(frozen=True)
class AttackTechnique:
    """A live technique or sub-technique as the catalogue answers it."""

    attack_id: str
    name: str
    tactics: tuple[str, ...]
    #: The definition, citations and links removed.
    description: str


_CITATION = re.compile(r"\(Citation:[^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


async def attack_techniques(
    *, domain: str = "enterprise-attack", source_url: str = DEFAULT_SOURCE_URL, version: str = ""
) -> list[AttackTechnique] | None:
    """Every live technique and sub-technique of *domain*, sorted by id, from the same
    cached store the integration's tools read; ``None`` when the store cannot be loaded.
    Revoked and deprecated techniques are left out."""
    store, error = await _get_store(normalize_domain(domain), version, source_url)
    if store is None:
        logger.info("ATT&CK catalogue unavailable: %s", error)
        return None
    out = []
    for stix_id in store.by_type.get("attack-pattern", []):
        obj = store.objects.get(stix_id) or {}
        if not obj.get("attack_id") or not _is_active(obj):
            continue
        description = _MD_LINK.sub(r"\1", _CITATION.sub("", obj.get("description") or ""))
        out.append(AttackTechnique(
            obj["attack_id"], obj.get("name", ""), tuple(_tactics_of(obj)), " ".join(description.split())
        ))
    return sorted(out, key=lambda t: t.attack_id)


def _tactics_of(obj: dict) -> list[str]:
    return [p.get("phase_name", "") for p in obj.get("kill_chain_phases") or []]


def _format_detail(store: _AttackStore, obj: dict) -> str:
    """Full detail block for one object."""
    lines = [_obj_label(obj)]
    if obj.get("attack_url"):
        lines.append(f"URL: {obj['attack_url']}")
    meta: list[str] = [f"Domain: {store.domain} (ATT&CK v{store.version or '?'})"]
    if obj.get("x_mitre_version"):
        meta.append(f"object version {obj['x_mitre_version']}")
    if obj.get("modified"):
        meta.append(f"modified {str(obj['modified'])[:10]}")
    lines.append(" | ".join(meta))

    aliases = _aliases_of(obj)
    if aliases:
        lines.append(f"Aliases: {', '.join(aliases[:15])}")
    tactics = _tactics_of(obj)
    if tactics:
        lines.append(f"Tactics: {', '.join(tactics)}")
    if obj.get("x_mitre_is_subtechnique"):
        lines.append("Sub-technique: yes")
    if obj.get("x_mitre_platforms"):
        lines.append(f"Platforms: {', '.join(obj['x_mitre_platforms'][:15])}")
    if obj.get("x_mitre_permissions_required"):
        lines.append(
            f"Permissions required: {', '.join(obj['x_mitre_permissions_required'])}"
        )
    if obj.get("x_mitre_shortname"):
        lines.append(f"Tactic shortname (for technique filters): {obj['x_mitre_shortname']}")
    if obj.get("labels") and obj.get("type") == "course-of-action":
        lines.append(f"Labels: {', '.join(obj['labels'])}")
    if obj.get("is_family") is not None and obj.get("type") == "malware":
        lines.append(f"Malware family: {'yes' if obj['is_family'] else 'no'}")
    if obj.get("first_seen"):
        # ``or ""`` (not a .get default) so an explicit null last_seen renders
        # as "?" instead of the string "None".
        last_seen = str(obj.get("last_seen") or "")[:10] or "?"
        lines.append(f"Campaign window: {str(obj['first_seen'])[:10]} -> {last_seen}")
    if obj.get("x_mitre_sectors"):
        lines.append(f"Sectors: {', '.join(obj['x_mitre_sectors'])}")
    if obj.get("x_mitre_collection_layers"):
        lines.append(f"Collection layers: {', '.join(obj['x_mitre_collection_layers'])}")
    desc = obj.get("description", "")
    if desc:
        lines.append("")
        lines.append(_trunc(desc, 3500))
    detection = obj.get("x_mitre_detection", "")
    if detection:
        lines.append("")
        lines.append(f"Detection: {_trunc(detection, 1200)}")
    return "\n".join(lines)


_RELATED_SECTION_ORDER: tuple[tuple[str, str, bool], ...] = (
    # (relationship_type, header, incoming?) — sections rendered for detail view
    ("subtechnique-of", "Parent technique", False),
    ("subtechnique-of", "Sub-techniques", True),
    ("uses", "Uses (techniques/software used by this object)", False),
    ("uses", "Used by (groups/software/campaigns using this object)", True),
    ("mitigates", "Mitigates", False),
    ("mitigates", "Mitigations", True),
    ("detects", "Detects", False),
    ("detects", "Detected by (detection strategies)", True),
    ("attributed-to", "Attributed to (group)", False),
    ("attributed-to", "Campaigns attributed to this group", True),
    ("targets", "Targets (assets)", False),
    ("targets", "Targeted by (techniques)", True),
    ("revoked-by", "Revoked by (replacement)", False),
)


def _related_entries(
    store: _AttackStore,
    stix_id: str,
    rel_type: str,
    incoming: bool,
) -> list[tuple[dict, dict]]:
    """Return ``(related_object, relationship)`` pairs for one direction+type."""
    rels = (store.rels_by_target if incoming else store.rels_by_source).get(stix_id, [])
    out: list[tuple[dict, dict]] = []
    for rel in rels:
        if rel["relationship_type"] != rel_type or rel.get("deprecated"):
            continue
        other_id = rel["source_ref"] if incoming else rel["target_ref"]
        other = store.objects.get(other_id)
        if other is not None and _is_active(other):
            out.append((other, rel))
    out.sort(key=lambda pair: pair[0].get("attack_id") or pair[0].get("name", ""))
    return out


def _format_related_summary(store: _AttackStore, stix_id: str, per_section: int = 8) -> str:
    """Compact related-objects section appended to detail views."""
    sections: list[str] = []
    for rel_type, header, incoming in _RELATED_SECTION_ORDER:
        entries = _related_entries(store, stix_id, rel_type, incoming)
        if not entries:
            continue
        shown = [f"  - {_obj_label(other)}" for other, _rel in entries[:per_section]]
        more = (
            f"\n  ... and {len(entries) - per_section} more (use get_mitre_attack_related)"
            if len(entries) > per_section
            else ""
        )
        sections.append(f"{header} ({len(entries)}):\n" + "\n".join(shown) + more)
    if not sections:
        return ""
    return "\n\nRelationships:\n" + "\n".join(sections)


def _resolve_object(
    store: _AttackStore, identifier: str
) -> tuple[dict | None, str | None]:
    """Resolve an ATT&CK ID, STIX ID, or exact name/alias to an object."""
    ident = str(identifier or "").strip()
    if not ident:
        return None, "Error: provide an ATT&CK ID (e.g. T1059, G0016), STIX ID, or exact name."
    upper = ident.upper()
    if _ATTACK_ID_RE.fullmatch(upper):
        stix_id = store.by_attack_id.get(upper)
        return (store.objects.get(stix_id) if stix_id else None), None
    if _STIX_ID_RE.fullmatch(ident.lower()):
        return store.objects.get(ident.lower()), None
    # Exact (case-insensitive) name or alias match via the name index,
    # active objects preferred.
    fallback: dict | None = None
    for stix_id in store.by_name.get(ident.lower(), []):
        obj = store.objects.get(stix_id)
        if obj is None:
            continue
        if _is_active(obj):
            return obj, None
        fallback = fallback or obj
    return fallback, None


def _coerce_int(value: Any, default: int) -> int:
    """Best-effort int coercion for LLM-supplied tool arguments.

    Tool arguments arrive from LLM tool calls without schema type coercion, so
    a malformed value (``"abc"``, ``None``, a list) must fall back to the
    default instead of raising out of the tool. ``None`` is checked explicitly
    so valid falsy values (``0``) are preserved and clamped by the caller.
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _paginate(
    items: list, limit: Any, offset: Any, *, default_limit: int = 25
) -> tuple[list, str]:
    # default_limit mirrors each tool's documented schema default so a
    # malformed/None limit falls back to that tool's page size, not a global.
    limit = max(1, min(_coerce_int(limit, default_limit), 200))
    offset = max(0, _coerce_int(offset, 0))
    page = items[offset : offset + limit]
    hint = ""
    if offset + len(page) < len(items):
        hint = (
            f"\n[Showing {offset + 1}-{offset + len(page)} of {len(items)} — "
            f"pass offset={offset + len(page)} for more]"
        )
    return page, hint


# ---------------------------------------------------------------------------
# Provider class
# ---------------------------------------------------------------------------


class MitreAttackIntegration(BaseIntegration):
    """MITRE ATT&CK knowledge base integration (free public data, no API key)."""

    provider_name = "mitre_attack"
    display_name = "MITRE ATT&CK"
    icon = "Crosshair"
    description = (
        "Connect the MITRE ATT&CK knowledge base: search and look up techniques, "
        "tactics, groups, software, mitigations, campaigns, data sources, and "
        "detection strategies across the Enterprise, Mobile, and ICS domains, "
        "with full relationship traversal (e.g. techniques used by a group). "
        "Free public STIX data — no API key needed."
    )
    auth_types = ["none"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        _domains, _version, source = parse_attack_config(token)
        try:
            async with egress_client(timeout=30, follow_redirects=True) as client:
                resp = await client.get(index_url(source))
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def get_user_info(self, token: str) -> dict:
        _domains, _version, source = parse_attack_config(token)
        async with egress_client(timeout=30, follow_redirects=True) as client:
            resp = await client.get(index_url(source))
            resp.raise_for_status()
            data = resp.json()
            return {
                "name": data.get("name", "MITRE ATT&CK"),
                "collections": [
                    c.get("name", "?") for c in data.get("collections") or []
                ],
            }


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    *,
    attack_source_sink: Callable[[tuple[str, str]], None] | None = None,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build MITRE ATT&CK tools.

    ``token`` is the compound config ``domains||version||source_url`` assembled
    by the dispatcher from ``extra_config`` — all three parts optional (the
    knowledge base is public, so a blank/dummy token still builds every tool
    with the defaults: all domains, latest release, official GitHub source).
    """
    enabled_domains, pinned_version, source_url = parse_attack_config(token)
    if callable(attack_source_sink):
        # The ATT&CK source this integration's tools read, for a caller that reads ATT&CK
        # as they do (the grounded STIX extraction).
        attack_source_sink((source_url, pinned_version))
    default_domain = enabled_domains[0]

    def _resolve_domain(domain: str) -> tuple[str | None, str | None]:
        """Validate a tool's ``domain`` argument against the enabled domains."""
        if not str(domain or "").strip():
            return default_domain, None
        normalized = normalize_domain(domain)
        if not normalized:
            return None, (
                f"Error: unknown ATT&CK domain '{domain}'. "
                f"Valid domains: {', '.join(ATTACK_DOMAINS)}."
            )
        if normalized not in enabled_domains:
            return None, (
                f"Error: domain '{normalized}' is not enabled on this integration. "
                f"Enabled domains: {', '.join(enabled_domains)}."
            )
        return normalized, None

    async def _store_for(domain: str) -> tuple[_AttackStore | None, str | None]:
        resolved, err = _resolve_domain(domain)
        if err:
            return None, err
        return await _get_store(resolved, pinned_version, source_url)

    # ══════════════════════════════════════════════════════════════════════
    # 1. SEARCH
    # ══════════════════════════════════════════════════════════════════════

    async def search_mitre_attack(
        query: str,
        object_types: list[str] | None = None,
        domain: str = "",
        include_deprecated: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """Keyword search across the ATT&CK knowledge base (names, aliases, IDs, descriptions)."""
        query = str(query or "").strip()
        if not query:
            return "Error: a search query is required (e.g. 'credential dumping', 'Cozy Bear', 'T1003')."
        store, err = await _store_for(domain)
        if err or store is None:
            return err or "Error: failed to load the ATT&CK dataset."

        types_filter: set[str] = set()
        for friendly in object_types or []:
            key = str(friendly or "").strip().lower()
            if key and key not in OBJECT_TYPES:
                return (
                    f"Error: unknown object type '{friendly}'. "
                    f"Valid types: {', '.join(OBJECT_TYPES)}."
                )
            types_filter.update(OBJECT_TYPES.get(key, ()))

        lowered = query.lower()
        name_hits: list[dict] = []
        desc_hits: list[dict] = []
        for obj in store.objects.values():
            if obj.get("type") == "x-mitre-matrix":
                continue
            if types_filter and obj.get("type") not in types_filter:
                continue
            if not include_deprecated and not _is_active(obj):
                continue
            haystack_primary = [obj.get("name", ""), obj.get("attack_id", "")]
            haystack_primary.extend(_aliases_of(obj))
            if any(lowered in h.lower() for h in haystack_primary if h):
                name_hits.append(obj)
            elif lowered in obj.get("description", "").lower():
                desc_hits.append(obj)

        sort_key = lambda o: (o.get("attack_id") or "~", o.get("name", ""))  # noqa: E731
        matches = sorted(name_hits, key=sort_key) + sorted(desc_hits, key=sort_key)
        if not matches:
            return (
                f"No ATT&CK objects matching '{query}' in {store.domain} "
                f"(v{store.version or '?'}). Try another domain or a broader term."
            )
        page, hint = _paginate(matches, limit, offset, default_limit=20)
        header = (
            f"Found {len(matches)} ATT&CK object(s) matching '{query}' in "
            f"{store.domain} (v{store.version or '?'}):"
        )
        return header + "\n" + "\n".join(_summary_line(o) for o in page) + hint

    # ══════════════════════════════════════════════════════════════════════
    # 2. GET OBJECT
    # ══════════════════════════════════════════════════════════════════════

    async def get_mitre_attack_object(
        attack_id: str,
        domain: str = "",
        include_relationships: bool = True,
    ) -> str:
        """Full detail for one ATT&CK object by ATT&CK ID, STIX ID, or exact name/alias."""
        # No explicit domain: try every enabled domain (IDs are domain-scoped).
        candidates = [domain] if str(domain or "").strip() else list(enabled_domains)
        last_err: str | None = None
        for candidate in candidates:
            store, err = await _store_for(candidate)
            if err or store is None:
                last_err = err
                continue
            obj, resolve_err = _resolve_object(store, attack_id)
            if resolve_err:
                return resolve_err
            if obj is None:
                continue
            out = _format_detail(store, obj)
            if obj.get("revoked"):
                replacements = _related_entries(store, obj["id"], "revoked-by", False)
                # Revoked-by links may target the replacing object even when
                # the source is revoked, so surface them explicitly.
                rels = store.rels_by_source.get(obj["id"], [])
                for rel in rels:
                    if rel["relationship_type"] == "revoked-by":
                        replacement = store.objects.get(rel["target_ref"])
                        if replacement and (replacement, rel) not in replacements:
                            replacements.append((replacement, rel))
                if replacements:
                    out += "\n\nThis object was REVOKED and replaced by: " + ", ".join(
                        _obj_label(other) for other, _ in replacements
                    )
            if include_relationships:
                out += _format_related_summary(store, obj["id"])
            return out
        if last_err:
            return last_err
        searched = ", ".join(
            normalize_domain(c) or c for c in candidates if str(c or "").strip()
        )
        return (
            f"No ATT&CK object found for '{attack_id}' (searched: {searched}). "
            "Use search_mitre_attack to find the right ID, or pass a different domain."
        )

    # ══════════════════════════════════════════════════════════════════════
    # 3. TACTICS
    # ══════════════════════════════════════════════════════════════════════

    async def list_mitre_attack_tactics(domain: str = "") -> str:
        """List a domain's tactics in matrix order, with technique counts."""
        store, err = await _store_for(domain)
        if err or store is None:
            return err or "Error: failed to load the ATT&CK dataset."
        tactic_ids = store.by_type.get("x-mitre-tactic", [])
        tactics = [store.objects[i] for i in tactic_ids if i in store.objects]
        tactics = [t for t in tactics if _is_active(t)]
        tactics.sort(key=lambda t: store.tactic_order.get(t["id"], 999))

        # Count active techniques (not sub-techniques) per tactic shortname.
        counts: dict[str, int] = {}
        for tech_id in store.by_type.get("attack-pattern", []):
            tech = store.objects.get(tech_id)
            if not tech or not _is_active(tech) or tech.get("x_mitre_is_subtechnique"):
                continue
            for phase in _tactics_of(tech):
                counts[phase] = counts.get(phase, 0) + 1

        lines = [
            f"{store.domain} tactics (ATT&CK v{store.version or '?'}, matrix order):"
        ]
        for tactic in tactics:
            shortname = tactic.get("x_mitre_shortname", "")
            lines.append(
                f"  - {tactic.get('attack_id', '?')} {tactic.get('name', '?')} "
                f"(shortname: {shortname}, {counts.get(shortname, 0)} techniques): "
                f"{_first_sentence(tactic.get('description', ''))}"
            )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 4. TECHNIQUES
    # ══════════════════════════════════════════════════════════════════════

    async def list_mitre_attack_techniques(
        domain: str = "",
        tactic: str = "",
        platform: str = "",
        subtechniques: str = "exclude",
        modified_since: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> str:
        """List techniques filtered by tactic, platform, sub-technique status, and/or modification date."""
        store, err = await _store_for(domain)
        if err or store is None:
            return err or "Error: failed to load the ATT&CK dataset."
        subtech_mode = str(subtechniques or "exclude").strip().lower()
        if subtech_mode not in ("exclude", "include", "only"):
            return "Error: subtechniques must be one of: exclude, include, only."
        tactic_filter = str(tactic or "").strip().lower().replace(" ", "-")
        platform_filter = str(platform or "").strip().lower()
        since = str(modified_since or "").strip()

        results: list[dict] = []
        for tech_id in store.by_type.get("attack-pattern", []):
            tech = store.objects.get(tech_id)
            if not tech or not _is_active(tech):
                continue
            is_sub = bool(tech.get("x_mitre_is_subtechnique"))
            if (subtech_mode == "exclude" and is_sub) or (
                subtech_mode == "only" and not is_sub
            ):
                continue
            if tactic_filter and tactic_filter not in _tactics_of(tech):
                continue
            if platform_filter and platform_filter not in [
                p.lower() for p in tech.get("x_mitre_platforms") or []
            ]:
                continue
            if since and str(tech.get("modified", ""))[:10] < since[:10]:
                continue
            results.append(tech)

        results.sort(key=lambda t: t.get("attack_id") or "~")
        if not results:
            filters = []
            if tactic_filter:
                filters.append(f"tactic={tactic_filter}")
            if platform_filter:
                filters.append(f"platform={platform_filter}")
            if since:
                filters.append(f"modified_since={since}")
            suffix = f" with filters: {', '.join(filters)}" if filters else ""
            return (
                f"No techniques found in {store.domain} (v{store.version or '?'}){suffix}. "
                "Check tactic shortnames with list_mitre_attack_tactics."
            )
        page, hint = _paginate(results, limit, offset, default_limit=50)
        lines = [
            f"Found {len(results)} technique(s) in {store.domain} (v{store.version or '?'}):"
        ]
        for tech in page:
            tactics = ", ".join(_tactics_of(tech))
            platforms = ", ".join((tech.get("x_mitre_platforms") or [])[:6])
            line = f"  - {tech.get('attack_id', '?')} {tech.get('name', '?')}"
            extras = [x for x in (f"tactics: {tactics}" if tactics else "", f"platforms: {platforms}" if platforms else "") if x]
            if extras:
                line += f" [{' | '.join(extras)}]"
            lines.append(line)
        return "\n".join(lines) + hint

    # ══════════════════════════════════════════════════════════════════════
    # 5. OBJECT LISTINGS (groups / software / campaigns / mitigations / ...)
    # ══════════════════════════════════════════════════════════════════════

    async def list_mitre_attack_objects(
        object_type: str,
        domain: str = "",
        name_filter: str = "",
        modified_since: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """List ATT&CK objects of one type (groups, software, campaigns, mitigations, ...)."""
        key = str(object_type or "").strip().lower()
        if key not in OBJECT_TYPES:
            return (
                f"Error: unknown object type '{object_type}'. "
                f"Valid types: {', '.join(OBJECT_TYPES)}."
            )
        store, err = await _store_for(domain)
        if err or store is None:
            return err or "Error: failed to load the ATT&CK dataset."
        name_lowered = str(name_filter or "").strip().lower()
        since = str(modified_since or "").strip()

        results: list[dict] = []
        for stix_type in OBJECT_TYPES[key]:
            for stix_id in store.by_type.get(stix_type, []):
                obj = store.objects.get(stix_id)
                if not obj or not _is_active(obj):
                    continue
                if name_lowered:
                    names = [obj.get("name", ""), obj.get("attack_id", "")]
                    names.extend(_aliases_of(obj))
                    if not any(name_lowered in n.lower() for n in names if n):
                        continue
                if since and str(obj.get("modified", ""))[:10] < since[:10]:
                    continue
                results.append(obj)

        results.sort(key=lambda o: (o.get("attack_id") or "~", o.get("name", "")))
        if not results:
            return (
                f"No {key} found in {store.domain} (v{store.version or '?'})"
                + (f" matching '{name_filter}'" if name_filter else "")
                + "."
            )
        page, hint = _paginate(results, limit, offset, default_limit=25)
        header = f"Found {len(results)} {key} in {store.domain} (v{store.version or '?'}):"
        return header + "\n" + "\n".join(_summary_line(o) for o in page) + hint

    # ══════════════════════════════════════════════════════════════════════
    # 6. RELATIONSHIP TRAVERSAL
    # ══════════════════════════════════════════════════════════════════════

    async def get_mitre_attack_related(
        attack_id: str,
        relationship: str = "all",
        direction: str = "both",
        related_type: str = "",
        domain: str = "",
        include_descriptions: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> str:
        """Traverse ATT&CK relationships from one object (e.g. all techniques used by a group)."""
        rel_filter = str(relationship or "all").strip().lower()
        if rel_filter != "all" and rel_filter not in _RELATIONSHIP_TYPES:
            return (
                f"Error: unknown relationship '{relationship}'. Valid: "
                f"{', '.join(sorted(_RELATIONSHIP_TYPES))}, all."
            )
        dir_filter = str(direction or "both").strip().lower()
        if dir_filter not in ("outgoing", "incoming", "both"):
            return "Error: direction must be one of: outgoing, incoming, both."
        type_key = str(related_type or "").strip().lower()
        if type_key and type_key not in OBJECT_TYPES:
            return (
                f"Error: unknown related_type '{related_type}'. "
                f"Valid types: {', '.join(OBJECT_TYPES)}."
            )
        type_filter = set(OBJECT_TYPES.get(type_key, ()))

        candidates = [domain] if str(domain or "").strip() else list(enabled_domains)
        store: _AttackStore | None = None
        obj: dict | None = None
        last_err: str | None = None
        for candidate in candidates:
            candidate_store, err = await _store_for(candidate)
            if err or candidate_store is None:
                last_err = err
                continue
            found, resolve_err = _resolve_object(candidate_store, attack_id)
            if resolve_err:
                return resolve_err
            if found is not None:
                store, obj = candidate_store, found
                break
        if store is None or obj is None:
            if last_err:
                return last_err
            searched = ", ".join(
                normalize_domain(c) or c for c in candidates if str(c or "").strip()
            )
            return (
                f"No ATT&CK object found for '{attack_id}' (searched: {searched}). "
                "Use search_mitre_attack to find the right ID, or pass a different domain."
            )

        rel_types = (
            sorted(_RELATIONSHIP_TYPES) if rel_filter == "all" else [rel_filter]
        )
        rows: list[str] = []
        for rel_type in rel_types:
            for incoming in (False, True):
                if (dir_filter == "outgoing" and incoming) or (
                    dir_filter == "incoming" and not incoming
                ):
                    continue
                for other, rel in _related_entries(store, obj["id"], rel_type, incoming):
                    if type_filter and other.get("type") not in type_filter:
                        continue
                    arrow = "<-" if incoming else "->"
                    row = f"  {arrow} [{rel_type}] {_obj_label(other)}"
                    rel_desc = rel.get("description", "")
                    if include_descriptions and rel_desc:
                        row += f"\n     {_trunc(rel_desc, 350)}"
                    rows.append(row)

        if not rows:
            return (
                f"No relationships found for {_obj_label(obj)} with the given filters "
                f"(relationship={rel_filter}, direction={dir_filter}"
                + (f", related_type={type_key}" if type_key else "")
                + ")."
            )
        page, hint = _paginate(rows, limit, offset, default_limit=50)
        header = (
            f"Relationships for {_obj_label(obj)} in {store.domain} "
            f"(v{store.version or '?'}) — {len(rows)} link(s); "
            "'->' = outgoing (this object acts), '<-' = incoming (other object acts):"
        )
        return header + "\n" + "\n".join(page) + hint

    # ══════════════════════════════════════════════════════════════════════
    # 7. STATUS / VERSIONS
    # ══════════════════════════════════════════════════════════════════════

    async def get_mitre_attack_status() -> str:
        """Report the configured domains/version/source and the latest published releases."""
        lines = [
            "MITRE ATT&CK integration status:",
            f"  Enabled domains: {', '.join(enabled_domains)}",
            f"  Pinned version: {pinned_version or 'latest (rolling)'}",
            f"  Source: {source_url}",
        ]
        try:
            async with egress_client(timeout=30, follow_redirects=True) as client:
                resp = await client.get(index_url(source_url))
        except httpx.HTTPError as exc:
            lines.append(
                "  Collection index: UNREACHABLE - "
                + format_network_error(exc, _PROVIDER, "fetch collection index")
            )
            return "\n".join(lines)
        if resp.status_code != 200:
            lines.append(
                f"  Collection index: HTTP {resp.status_code} for {index_url(source_url)}"
            )
            return "\n".join(lines)
        try:
            data = resp.json()
        except json.JSONDecodeError:
            lines.append("  Collection index: invalid JSON")
            return "\n".join(lines)
        lines.append("  Published collections:")
        for coll in data.get("collections") or []:
            versions = coll.get("versions") or []
            latest = versions[0].get("version", "?") if versions else "?"
            released = (
                str(versions[0].get("modified", ""))[:10] if versions else "?"
            )
            lines.append(
                f"    - {coll.get('name', '?')}: latest v{latest} (released {released}, "
                f"{len(versions)} versions available)"
            )
        cached = [
            f"{s.domain} v{s.version or '?'} ({len(s.objects)} objects)"
            for key, s in _STORE_CACHE.items()
            if key.startswith(f"{source_url}|")
        ]
        if cached:
            lines.append(f"  Loaded in cache: {'; '.join(cached)}")
        return "\n".join(lines)

    # ── Register tools ────────────────────────────────────────────────────

    executor.register("search_mitre_attack", search_mitre_attack)
    executor.register("get_mitre_attack_object", get_mitre_attack_object)
    executor.register("list_mitre_attack_tactics", list_mitre_attack_tactics)
    executor.register("list_mitre_attack_techniques", list_mitre_attack_techniques)
    executor.register("list_mitre_attack_objects", list_mitre_attack_objects)
    executor.register("get_mitre_attack_related", get_mitre_attack_related)
    executor.register("get_mitre_attack_status", get_mitre_attack_status)

    domain_schema = {
        "type": "string",
        "description": (
            "ATT&CK domain to query: 'enterprise-attack' (default), "
            "'mobile-attack', or 'ics-attack' (shorthands 'enterprise'/'mobile'/'ics' work). "
            f"Enabled on this integration: {', '.join(enabled_domains)}."
        ),
        "default": default_domain,
    }
    object_type_values = sorted(OBJECT_TYPES)

    return [
        ToolDefinition(
            name="search_mitre_attack",
            description=(
                "Search the MITRE ATT&CK knowledge base by keyword. Matches names, "
                "aliases (e.g. 'Cozy Bear' finds APT29), ATT&CK IDs, and descriptions "
                "across techniques, tactics, groups, software, mitigations, campaigns, "
                "data sources/components, assets, and detection strategies. Returns "
                "compact rows with ATT&CK IDs for follow-up via get_mitre_attack_object. "
                "Use this first when you only have a name, alias, or keyword."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keyword, name, alias, or ATT&CK ID to search for (e.g. 'credential dumping', 'Sandworm', 'T1003')",
                    },
                    "object_types": {
                        "type": "array",
                        "items": {"type": "string", "enum": object_type_values},
                        "description": "Restrict to these object types (default: all types)",
                    },
                    "domain": domain_schema,
                    "include_deprecated": {
                        "type": "boolean",
                        "description": "Include deprecated/revoked objects (default false)",
                        "default": False,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results per page (default 20, max 200)",
                        "default": 20,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Pagination offset (default 0)",
                        "default": 0,
                    },
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_mitre_attack_object",
            description=(
                "Get the full detail of one ATT&CK object by ATT&CK ID (T1059, "
                "T1059.001, TA0002, G0016, S0154, M1038, C0028, DS0029, DC0045, "
                "A0007, DET0903), STIX ID, or exact name/alias. Returns the "
                "description, tactics, platforms, detection guidance, the "
                "attack.mitre.org URL, and a relationship summary (mitigations, "
                "sub-techniques, groups/software using it, detection strategies, "
                "...). When no domain is given, all enabled domains are searched. "
                "Use get_mitre_attack_related for the complete relationship list."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "attack_id": {
                        "type": "string",
                        "description": "ATT&CK ID (e.g. 'T1059.001', 'G0016'), STIX ID, or exact name/alias (e.g. 'Mimikatz')",
                    },
                    "domain": {**domain_schema, "default": ""},
                    "include_relationships": {
                        "type": "boolean",
                        "description": "Append the related-objects summary (default true)",
                        "default": True,
                    },
                },
                "required": ["attack_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_mitre_attack_tactics",
            description=(
                "List an ATT&CK domain's tactics in official matrix order with their "
                "TA IDs, shortnames (used as the 'tactic' filter in "
                "list_mitre_attack_techniques), technique counts, and one-line "
                "descriptions. The fastest way to see a domain's kill-chain layout."
            ),
            input_schema={
                "type": "object",
                "properties": {"domain": domain_schema},
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_mitre_attack_techniques",
            description=(
                "List ATT&CK techniques filtered by tactic shortname (e.g. "
                "'privilege-escalation' — discover shortnames via "
                "list_mitre_attack_tactics), platform (e.g. 'Windows', 'Linux', "
                "'IaaS', 'Android'), sub-technique handling, and/or modification "
                "date. Returns ATT&CK IDs, names, tactics, and platforms. Use "
                "modified_since (YYYY-MM-DD) to triage what changed in a new "
                "ATT&CK release."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "domain": domain_schema,
                    "tactic": {
                        "type": "string",
                        "description": "Tactic shortname filter (e.g. 'initial-access', 'defense-impairment')",
                    },
                    "platform": {
                        "type": "string",
                        "description": "Platform filter (e.g. 'Windows', 'macOS', 'Linux', 'IaaS', 'Android', 'iOS')",
                    },
                    "subtechniques": {
                        "type": "string",
                        "enum": ["exclude", "include", "only"],
                        "description": "Sub-technique handling: 'exclude' parents only (default), 'include' both, 'only' sub-techniques",
                        "default": "exclude",
                    },
                    "modified_since": {
                        "type": "string",
                        "description": "Only techniques modified on/after this date (YYYY-MM-DD)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results per page (default 50, max 200)",
                        "default": 50,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Pagination offset (default 0)",
                        "default": 0,
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_mitre_attack_objects",
            description=(
                "List ATT&CK objects of one type: groups (G####), software (S####, "
                "malware + tools), campaigns (C####), mitigations (M####), "
                "data_sources (DS####), data_components (DC####), assets (A####, "
                "ICS only), detection_strategies (DET####), analytics (AN####), "
                "tactics, or techniques. Supports a name/alias substring filter and "
                "a modified_since date. For techniques prefer "
                "list_mitre_attack_techniques (richer filters)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_type": {
                        "type": "string",
                        "enum": object_type_values,
                        "description": "Which object type to list",
                    },
                    "domain": domain_schema,
                    "name_filter": {
                        "type": "string",
                        "description": "Substring filter on name/alias/ATT&CK ID (e.g. 'APT', 'ransom')",
                    },
                    "modified_since": {
                        "type": "string",
                        "description": "Only objects modified on/after this date (YYYY-MM-DD)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results per page (default 25, max 200)",
                        "default": 25,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Pagination offset (default 0)",
                        "default": 0,
                    },
                },
                "required": ["object_type"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_mitre_attack_related",
            description=(
                "Traverse ATT&CK relationships from one object — the core pivot "
                "tool. Answers questions like: which techniques does group G0016 "
                "use (relationship='uses', direction='outgoing', "
                "related_type='techniques')? Which groups use technique T1566 "
                "(direction='incoming', related_type='groups')? What mitigates "
                "T1059 (relationship='mitigates', direction='incoming')? Which "
                "detection strategies detect it (relationship='detects')? Includes "
                "the per-relationship procedure-example descriptions ATT&CK "
                "documents (how exactly a group used a technique). '->' rows are "
                "outgoing (the object acts), '<-' rows are incoming."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "attack_id": {
                        "type": "string",
                        "description": "ATT&CK ID, STIX ID, or exact name/alias of the pivot object (e.g. 'G0016', 'T1566', 'Mimikatz')",
                    },
                    "relationship": {
                        "type": "string",
                        "enum": [*sorted(_RELATIONSHIP_TYPES), "all"],
                        "description": "Relationship type to follow (default 'all')",
                        "default": "all",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["outgoing", "incoming", "both"],
                        "description": "'outgoing' = this object acts (group uses technique); 'incoming' = others act on it (technique used by groups). Default 'both'.",
                        "default": "both",
                    },
                    "related_type": {
                        "type": "string",
                        "enum": object_type_values,
                        "description": "Only return related objects of this type (e.g. 'techniques', 'groups', 'mitigations')",
                    },
                    "domain": {**domain_schema, "default": ""},
                    "include_descriptions": {
                        "type": "boolean",
                        "description": "Include per-relationship procedure-example text (default true)",
                        "default": True,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max relationship rows per page (default 50, max 200)",
                        "default": 50,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Pagination offset (default 0)",
                        "default": 0,
                    },
                },
                "required": ["attack_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_mitre_attack_status",
            description=(
                "Report the MITRE ATT&CK integration configuration (enabled domains, "
                "pinned version, data source) and the latest published release of "
                "each ATT&CK collection (Enterprise / Mobile / ICS), plus what is "
                "currently loaded in the local cache. Use it to check whether a new "
                "ATT&CK version has been released."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=2,
        ),
    ]
