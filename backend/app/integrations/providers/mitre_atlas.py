# SPDX-License-Identifier: Apache-2.0

"""MITRE ATLAS integration - the AI/ML adversarial threat knowledge base.

MITRE ATLAS (Adversarial Threat Landscape for AI Systems, atlas.mitre.org)
catalogs how adversaries attack AI-enabled systems: tactics, techniques,
mitigations, and real-world case studies, modeled after ATT&CK but focused on
AI/ML. This provider is the AI-security knowledge building block for an
agentic CTI / AI red-team / MLSecOps workflow:

  - Matrix + tactics (*orientation*): the ATLAS matrix in official column
    order, tactic detail with the techniques that achieve it
  - Techniques (*threat knowledge*): browse/filter by tactic, platform
    (Predictive AI / Generative AI / Agentic AI / Enterprise), and maturity
    (Feasible / Demonstrated / Realized); full per-technique dossier with
    ATT&CK crosswalk, parent/sub-techniques, mitigations, and case studies
  - Search (*discovery*): keyword search across every ATLAS object type
  - Mitigations (*defense planning*): browse/filter AI security mitigations
    by category and AI lifecycle phase; per-mitigation detail with the
    techniques it addresses
  - Case studies (*real-world incidents*): the corpus of documented AI
    attacks and exercises, with the full ordered attack chain per study
  - Relationship traversal + ATT&CK crosswalk (*pivoting*): typed graph
    traversal from any ATLAS id, and ATT&CK -> ATLAS id resolution

Data source (no API, no API key)
--------------------------------
ATLAS has no REST API. MITRE publishes the knowledge base openly in the
``mitre-atlas/atlas-data`` GitHub repository as a single YAML release file
(data format v6, content releases roughly monthly). This provider fetches the
distributed YAML over plain HTTPS, parses and indexes it in-process with a
6-hour TTL cache (a failed refresh serves the stale index and defers the
next retry by 15 minutes), and answers every tool call locally - so the
integration needs NO credentials. The only configurable knob is an optional
``source_url`` override in ``extra_config`` for mirrors / air-gapped hosts;
``integration_tools._get_token`` resolves it (or the official default)
through :func:`resolve_atlas_source_url` so the runtime never skips this
auth-none provider for lack of a token.

The ``dist/v6/ATLAS-latest.yaml`` path in the upstream repo is a git symlink:
its raw content is the *file name* of the current release (e.g.
``ATLAS-2026.06.yaml``). The fetcher resolves one level of that indirection
by re-fetching the named file relative to the same directory.

Deep links always use the fixed public host ``https://atlas.mitre.org``.
A configured ``source_url`` override may point at internal infrastructure,
so it is never surfaced in agent output - ``get_atlas_status`` only names
the data source when it is the well-known official URL and reports
"custom mirror" otherwise.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
import yaml

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.http import safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "MITRE ATLAS"
_SOURCE = "integration:mitre_atlas"

# The public ATLAS website - the ONLY host ever surfaced in deep links.
ATLAS_PUBLIC_URL = "https://atlas.mitre.org"

# Official distributed data location (data format v6). The path is a git
# symlink whose raw content names the current release file - see the module
# docstring and _fetch_yaml_text() for the one-level indirection resolution.
ATLAS_DEFAULT_SOURCE_URL = (
    "https://raw.githubusercontent.com/mitre-atlas/atlas-data/main/dist/v6/ATLAS-latest.yaml"
)

# The content updates roughly monthly - a 6 h TTL keeps every worker within
# a few upstream fetches per day while staying fresh after a new release.
_CACHE_TTL_SECONDS = 6 * 60 * 60

# Back-off before retrying a failed refresh while serving the stale cache -
# without it every tool call after TTL expiry would hammer an unhealthy
# source (same contract as the MITRE ATT&CK provider).
_STALE_RETRY_SECONDS = 15 * 60

_DEFAULT_TIMEOUT = 60
_MAX_LIMIT = 100
_DESC_TRUNC = 700

# Hard cap on the fetched document size. The real release file is ~600 KB;
# the cap exists so a misconfigured/compromised mirror URL cannot push an
# arbitrarily large body into the CPU-heavy YAML parse + index build (the
# parsed object tree costs a large multiple of the raw text size).
_MAX_DATASET_BYTES = 50 * 1024 * 1024

# A git-symlink raw body is a short single-line relative file name (e.g.
# "ATLAS-2026.06.yaml" or "v6/ATLAS-latest.yaml"), never real YAML.
_SYMLINK_MAX_LEN = 256

# ATLAS id conventions (atlas-data README): tactic AML.TA####, technique
# AML.T#### (+ .### sub-technique), mitigation AML.M####, case study AML.CS####.
_ID_PATTERNS: dict[str, re.Pattern[str]] = {
    "tactic": re.compile(r"^AML\.TA\d{4}$"),
    "technique": re.compile(r"^AML\.T\d{4}(?:\.\d{3})?$"),
    "mitigation": re.compile(r"^AML\.M\d{4}$"),
    "case-study": re.compile(r"^AML\.CS\d{4}$"),
}

# object-type -> atlas.mitre.org path segment for deep links.
_DEEP_LINK_PATHS = {
    "tactic": "tactics",
    "technique": "techniques",
    "mitigation": "mitigations",
    "case-study": "studies",
}

_OBJECT_TYPES = ("tactic", "technique", "mitigation", "case-study")

# The five typed relationship kinds of the v6 data format.
_REL_TYPES = ("sequences", "achieves", "specializes", "mitigates", "employs")

# MITRE ATT&CK id shapes accepted by the crosswalk tool: tactic TA####,
# technique T#### (+ .### sub-technique), mitigation M####.
_ATTACK_ID_RE = re.compile(r"^(TA\d{4}|T\d{4}(?:\.\d{3})?|M\d{4})$")

# Site-relative markdown links inside ATLAS descriptions, e.g.
# "[Create Proxy AI Model](/techniques/AML.T0005)" - rewritten to absolute
# atlas.mitre.org links so agent output never contains dead relative paths.
_RELATIVE_LINK_RE = re.compile(r"\]\(/(tactics|techniques|mitigations|studies)/")


def resolve_atlas_source_url(override: str | None = None) -> str:
    """Resolve the effective ATLAS data source URL.

    Returns the ``source_url`` override when it is a well-formed http(s)
    URL, otherwise the official default. The override is validated the same
    way ``get_integration_public_url`` validates user-facing base URLs -
    control characters and ``user:pass@host`` userinfo are rejected (an
    ATLAS mirror is an unauthenticated static file host by design, so
    credentials in the URL would only leak into fetch logs), and the query /
    fragment are stripped so the per-URL cache key stays stable. Single
    chokepoint shared by ``integration_tools._get_token``, the connectivity
    test, and ``build_tools`` - a dummy/blank token can never disable the
    provider.
    """
    candidate = (override or "").strip()
    if not candidate.startswith(("http://", "https://")):
        return ATLAS_DEFAULT_SOURCE_URL
    # ASCII control characters (incl. CR/LF) would corrupt the fetch
    # request and the server logs - reject outright.
    if any(ch < " " or ch == "\x7f" for ch in candidate):
        logger.warning(
            "MITRE ATLAS: ignoring source_url override containing control "
            "characters - using the official default"
        )
        return ATLAS_DEFAULT_SOURCE_URL
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ATLAS_DEFAULT_SOURCE_URL
    if not parsed.hostname:
        return ATLAS_DEFAULT_SOURCE_URL
    if parsed.username or parsed.password:
        logger.warning(
            "MITRE ATLAS: ignoring source_url override carrying userinfo "
            "credentials - using the official default"
        )
        return ATLAS_DEFAULT_SOURCE_URL
    # A static release file has no use for a query or fragment; dropping
    # them keeps tokens out of logs and the cache key canonical.
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


# ---------------------------------------------------------------------------
# Data layer: fetch + parse + index with a TTL cache
# ---------------------------------------------------------------------------


@dataclass
class _Edge:
    """One typed relationship edge (flattened from the v6 relationships map)."""

    source: str
    target: str
    rel_type: str
    description: str = ""
    tactic: str = ""
    step_id: str = ""
    leads_to: list[str] = field(default_factory=list)
    position: int | None = None


@dataclass
class _AtlasIndex:
    """Parsed + indexed ATLAS dataset (one release)."""

    version: str
    format_version: str
    modified_date: str
    source_url: str
    tactics: dict[str, dict]
    techniques: dict[str, dict]
    mitigations: dict[str, dict]
    case_studies: dict[str, dict]
    # id -> object for every addressable object (incl. matrix/collection)
    objects: dict[str, dict]
    # forward[source_id] / reverse[target_id] -> flattened typed edges
    forward: dict[str, list[_Edge]]
    reverse: dict[str, list[_Edge]]
    # ATT&CK id (upper-cased, e.g. "T1595") -> ATLAS ids referencing it
    attack_index: dict[str, list[str]]
    # tactic ids in official matrix column order
    matrix_tactic_order: list[str]
    fetched_at: float = field(default_factory=time.monotonic)
    # When a refresh fails and this stale index is served instead, the next
    # implicit retry is deferred until this monotonic deadline
    # (anti-hammering; an explicit force_refresh always refetches).
    retry_after: float = 0.0

    def object_type(self, object_id: str) -> str:
        return str((self.objects.get(object_id) or {}).get("object-type", ""))


# source_url -> _AtlasIndex; guarded per-URL so concurrent tool calls never
# stampede the upstream host with duplicate fetches.
_CACHE: dict[str, _AtlasIndex] = {}
_CACHE_LOCKS: dict[str, asyncio.Lock] = {}


def _cache_lock(source_url: str) -> asyncio.Lock:
    lock = _CACHE_LOCKS.get(source_url)
    if lock is None:
        lock = asyncio.Lock()
        _CACHE_LOCKS[source_url] = lock
    return lock


def _looks_like_symlink_body(text: str) -> bool:
    """True when the fetched body is a git-symlink target file name.

    A git-symlink target is always a short RELATIVE file name/path. Anything
    absolute (``/x.yaml``), a network-path reference (``//host/x.yaml`` -
    which ``urljoin`` would resolve onto a DIFFERENT host than the
    configured source_url, an SSRF footgun), a full URL (``:`` rejected),
    a backslashed Windows-style path, or a path containing a ``..``
    traversal segment (``../ATLAS.yaml`` would climb OUT of the source
    directory via ``urljoin``) is refused so the second fetch can never
    leave the configured source's directory tree.
    """
    body = (text or "").strip()
    if not body or len(body) > _SYMLINK_MAX_LEN or "\n" in body:
        return False
    if body.startswith("/") or "\\" in body or ":" in body:
        return False
    if any(segment == ".." for segment in body.split("/")):
        return False
    return body.endswith((".yaml", ".yml"))


async def _fetch_yaml_text(
    client: httpx.AsyncClient, source_url: str
) -> tuple[str | None, str | None]:
    """Fetch the ATLAS YAML, resolving one level of git-symlink indirection.

    Returns ``(yaml_text, None)`` or ``(None, error_string)``.
    """
    resp, err = await safe_request(
        client,
        "get",
        source_url,
        _PROVIDER,
        "fetch ATLAS data",
        follow_redirects=True,
    )
    if err:
        return None, err
    text = resp.text
    if _looks_like_symlink_body(text):
        # dist/ATLAS-latest.yaml and dist/v6/ATLAS-latest.yaml are git
        # symlinks: the raw body is the target file name, relative to the
        # same directory. Resolve exactly one level (the target is a real
        # file in every published release).
        target_url = urljoin(source_url, text.strip())
        resp, err = await safe_request(
            client,
            "get",
            target_url,
            _PROVIDER,
            "fetch ATLAS data (resolved symlink)",
            follow_redirects=True,
        )
        if err:
            return None, err
        text = resp.text
    # Compare the raw BODY BYTE length, not len(text): decoded characters
    # under-count multi-byte UTF-8 content and would misreport the size.
    size_bytes = len(resp.content)
    if size_bytes > _MAX_DATASET_BYTES:
        return None, (
            f"Error: the MITRE ATLAS source returned "
            f"{size_bytes // (1024 * 1024)} MB - far larger than any ATLAS "
            f"release (~1 MB). Check the integration's source_url: it must "
            f"point to an ATLAS.yaml release file from "
            f"github.com/mitre-atlas/atlas-data."
        )
    return text, None


def _flatten_relationships(raw: Any) -> list[_Edge]:
    """Flatten the v6 relationships map (source id -> {rel type: [edges]})."""
    edges: list[_Edge] = []
    if not isinstance(raw, dict):
        return edges
    for source_id, groups in raw.items():
        if not isinstance(groups, dict):
            continue
        for rel_type, group_edges in groups.items():
            if not isinstance(group_edges, list):
                continue
            for e in group_edges:
                if not isinstance(e, dict):
                    continue
                target = str(e.get("target") or "")
                if not target:
                    continue
                leads_to = e.get("leads-to")
                edges.append(
                    _Edge(
                        source=str(e.get("source") or source_id),
                        target=target,
                        rel_type=str(e.get("relationship-type") or rel_type),
                        description=str(e.get("description") or ""),
                        tactic=str(e.get("tactic") or ""),
                        step_id=str(e.get("step-id") or ""),
                        leads_to=[str(s) for s in leads_to]
                        if isinstance(leads_to, list)
                        else [],
                        position=e.get("position")
                        if isinstance(e.get("position"), int)
                        else None,
                    )
                )
    return edges


def _validate_section_shapes(data: dict) -> str | None:
    """Return an error label when a v6 section has the wrong container type.

    ``_build_index()`` (and every formatter downstream of it) assumes
    ``collection``/``matrix`` are mappings and the four object sections are
    mappings of id -> object mapping. A misconfigured mirror could serve
    YAML where e.g. ``tactics`` is a list - validating here keeps
    :func:`fetch_atlas_dataset`'s ``(index, err)`` contract intact instead
    of letting ``_build_index()`` raise into tool execution.
    """
    for key in ("collection", "matrix"):
        if data.get(key) is not None and not isinstance(data[key], dict):
            return f"'{key}' is not a mapping"
    for key in ("tactics", "techniques", "mitigations", "case-studies"):
        section = data.get(key)
        if section is None:
            continue
        if not isinstance(section, dict):
            return f"'{key}' is not a mapping"
        if not all(isinstance(obj, dict) for obj in section.values()):
            return f"'{key}' contains non-mapping entries"
    return None


def _build_index(data: dict, source_url: str) -> _AtlasIndex:
    """Build the in-memory index from a parsed v6 ATLAS dataset."""
    collection = data.get("collection") or {}
    matrix = data.get("matrix") or {}
    # str(k): every downstream consumer (id lookups, search's oid.lower())
    # assumes string keys - YAML happily yields non-string mapping keys.
    tactics: dict[str, dict] = {
        str(k): v for k, v in (data.get("tactics") or {}).items()
    }
    techniques: dict[str, dict] = {
        str(k): v for k, v in (data.get("techniques") or {}).items()
    }
    mitigations: dict[str, dict] = {
        str(k): v for k, v in (data.get("mitigations") or {}).items()
    }
    case_studies: dict[str, dict] = {
        str(k): v for k, v in (data.get("case-studies") or {}).items()
    }

    objects: dict[str, dict] = {}
    for bucket in (tactics, techniques, mitigations, case_studies):
        objects.update(bucket)
    for obj in (collection, matrix):
        oid = str(obj.get("id") or "")
        if oid:
            objects[oid] = obj

    forward: dict[str, list[_Edge]] = {}
    reverse: dict[str, list[_Edge]] = {}
    for edge in _flatten_relationships(data.get("relationships")):
        forward.setdefault(edge.source, []).append(edge)
        reverse.setdefault(edge.target, []).append(edge)

    attack_index: dict[str, list[str]] = {}
    for oid, obj in objects.items():
        ref = obj.get("attack-reference")
        if isinstance(ref, dict) and ref.get("id"):
            attack_index.setdefault(str(ref["id"]).upper(), []).append(oid)

    matrix_id = str(matrix.get("id") or "ATLAS-matrix")
    sequence_edges = [
        e for e in forward.get(matrix_id, []) if e.rel_type == "sequences"
    ]
    sequence_edges.sort(key=lambda e: (e.position is None, e.position or 0))
    matrix_tactic_order = [e.target for e in sequence_edges if e.target in tactics]
    # Defensive: any tactic missing from the sequences layout still shows up.
    for tid in tactics:
        if tid not in matrix_tactic_order:
            matrix_tactic_order.append(tid)

    return _AtlasIndex(
        version=str(collection.get("version") or "unknown"),
        format_version=str(data.get("format-version") or "unknown"),
        modified_date=str(collection.get("modified-date") or ""),
        source_url=source_url,
        tactics=tactics,
        techniques=techniques,
        mitigations=mitigations,
        case_studies=case_studies,
        objects=objects,
        forward=forward,
        reverse=reverse,
        attack_index=attack_index,
        matrix_tactic_order=matrix_tactic_order,
    )


def _serve_cached(cached: _AtlasIndex) -> bool:
    """True while *cached* needs no refetch (fresh TTL, or backing off)."""
    now = time.monotonic()
    return (
        now - cached.fetched_at < _CACHE_TTL_SECONDS or now < cached.retry_after
    )


def _defer_and_serve_stale(
    cached: _AtlasIndex, reason: str
) -> tuple[_AtlasIndex, None]:
    """Serve stale data over a hard failure and back off the next retry."""
    cached.retry_after = time.monotonic() + _STALE_RETRY_SECONDS
    logger.warning(
        "MITRE ATLAS refresh failed (%s) - serving cached release %s, "
        "next retry in %d min",
        reason,
        cached.version,
        _STALE_RETRY_SECONDS // 60,
    )
    return cached, None


async def fetch_atlas_dataset(
    source_url: str, *, force_refresh: bool = False
) -> tuple[_AtlasIndex | None, str | None]:
    """Fetch, parse, and index the ATLAS dataset (TTL-cached per source URL).

    Returns ``(index, None)`` on success or ``(None, error_string)`` on
    failure. When a refresh fails and a stale index exists, the stale data is
    served and the next implicit retry is deferred by ``_STALE_RETRY_SECONDS``
    so tool calls never hammer an unhealthy source (``force_refresh`` always
    bypasses the back-off). Shared by every tool, ``validate_connection`` /
    ``get_user_info``, and the connectivity test.
    """
    cached = _CACHE.get(source_url)
    if cached is not None and not force_refresh and _serve_cached(cached):
        return cached, None

    async with _cache_lock(source_url):
        # Re-check under the lock - a concurrent caller may have refreshed.
        cached = _CACHE.get(source_url)
        if cached is not None and not force_refresh and _serve_cached(cached):
            return cached, None

        async with egress_client(timeout=_DEFAULT_TIMEOUT) as client:
            text, err = await _fetch_yaml_text(client, source_url)
        if err:
            if cached is not None:
                return _defer_and_serve_stale(cached, err)
            return None, err

        # yaml.safe_load on a ~600 KB document is CPU-bound - keep it off
        # the event loop.
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(None, yaml.safe_load, text)
        except yaml.YAMLError as exc:
            if cached is not None:
                return _defer_and_serve_stale(cached, f"invalid YAML: {exc.__class__.__name__}")
            return None, (
                f"Error: the MITRE ATLAS source did not return valid YAML "
                f"({exc.__class__.__name__}). Check the integration's "
                f"source_url - it must point to an ATLAS.yaml release file "
                f"from github.com/mitre-atlas/atlas-data."
            )

        if not isinstance(data, dict) or "tactics" not in data:
            if cached is not None:
                return _defer_and_serve_stale(cached, "unexpected document shape")
            return None, (
                "Error: the MITRE ATLAS source returned an unexpected document "
                "(no 'tactics' key). The source_url must point to a data "
                "format v6 ATLAS.yaml release file from "
                "github.com/mitre-atlas/atlas-data (dist/v6/)."
            )
        format_version = str(data.get("format-version") or "")
        if not format_version.startswith("6."):
            if cached is not None:
                return _defer_and_serve_stale(
                    cached, f"unsupported format version '{format_version}'"
                )
            return None, (
                f"Error: unsupported ATLAS data format version "
                f"'{format_version or 'unknown'}' - this integration reads "
                f"data format v6 (dist/v6/ATLAS-latest.yaml in "
                f"github.com/mitre-atlas/atlas-data). Update the source_url."
            )

        shape_err = _validate_section_shapes(data)
        if shape_err:
            if cached is not None:
                return _defer_and_serve_stale(
                    cached, f"malformed dataset: {shape_err}"
                )
            return None, (
                f"Error: the MITRE ATLAS source returned a malformed dataset "
                f"({shape_err}). The source_url must point to a data format "
                f"v6 ATLAS.yaml release file from "
                f"github.com/mitre-atlas/atlas-data (dist/v6/)."
            )

        index = _build_index(data, source_url)
        _CACHE[source_url] = index
        logger.info(
            "MITRE ATLAS release %s loaded from %s: %d tactics, %d techniques, "
            "%d mitigations, %d case studies",
            index.version,
            source_url,
            len(index.tactics),
            len(index.techniques),
            len(index.mitigations),
            len(index.case_studies),
        )
        return index, None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _rewrite_links(text: str) -> str:
    """Rewrite site-relative markdown links to absolute atlas.mitre.org URLs."""
    return _RELATIVE_LINK_RE.sub(f"]({ATLAS_PUBLIC_URL}/\\1/", text or "")


def _count_noun(count: int, singular: str, plural: str | None = None) -> str:
    """Format a count with a grammatically correct noun ("1 tactic", "2 tactics")."""
    noun = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {noun}"


def _trunc(text: Any, max_len: int = _DESC_TRUNC) -> str:
    if text is None:
        return ""
    text = _rewrite_links(str(text).strip())
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


def _deep_link(obj: dict) -> str:
    path = _DEEP_LINK_PATHS.get(str(obj.get("object-type") or ""))
    oid = obj.get("id")
    if not path or not oid:
        return ""
    return f"{ATLAS_PUBLIC_URL}/{path}/{oid}"


def _normalize_atlas_id(raw: str) -> str:
    """Normalize an agent-supplied ATLAS id (case, whitespace, AML. prefix)."""
    value = (raw or "").strip().upper()
    if not value:
        return ""
    if not value.startswith("AML.") and re.match(
        r"^(TA\d{4}|T\d{4}(\.\d{3})?|M\d{4}|CS\d{4})$", value
    ):
        value = f"AML.{value}"
    return value


def _id_error(raw: str, expected: str) -> str:
    pattern_hints = {
        "tactic": "AML.TA#### (e.g. AML.TA0000)",
        "technique": "AML.T#### or AML.T####.### (e.g. AML.T0051 or AML.T0051.000)",
        "mitigation": "AML.M#### (e.g. AML.M0005)",
        "case-study": "AML.CS#### (e.g. AML.CS0016)",
    }
    return (
        f"Error: '{raw}' is not a valid ATLAS {expected} id - expected "
        f"{pattern_hints[expected]}. Use search_atlas to find objects by name."
    )


def _paginate(items: list, limit: Any, offset: Any) -> tuple[list, int, int, int]:
    """Slice *items* -> (page, total, limit, offset).

    The offset is only floored at 0, never clamped to ``total``: Python
    slicing handles out-of-range offsets safely, and the returned value must
    stay the caller-supplied one so the out-of-range pagination hint
    (:func:`_offset_error`) reports the offset the user actually passed.
    """
    total = len(items)
    lim = _clamp(limit, 1, _MAX_LIMIT, 25)
    try:
        off = max(int(offset), 0)
    except (TypeError, ValueError):
        off = 0
    return items[off : off + lim], total, lim, off


def _offset_error(total: int, offset: int) -> str:
    """Hint for an empty page caused by an out-of-range offset (not by the
    filters): there ARE matches, just none at/after *offset*."""
    return (
        f"No more results at offset={offset} - only {total} result(s) match. "
        f"Pass offset=0 to restart from the first page."
    )


def _more_hint(total: int, limit: int, offset: int) -> str:
    shown_up_to = offset + limit
    if total > shown_up_to:
        return (
            f"\n[{total - shown_up_to} more result(s) - "
            f"pass offset={shown_up_to} to continue]"
        )
    return ""


def _summary_line(obj: dict) -> str:
    """One-line summary for any ATLAS object (used by search/traversal)."""
    obj_type = str(obj.get("object-type") or "?")
    parts = [f"[{obj.get('id', '?')}] {obj.get('name', 'Unnamed')} ({obj_type})"]
    if obj_type == "technique":
        plats = obj.get("platforms") or []
        extras = []
        if obj.get("maturity"):
            extras.append(str(obj["maturity"]))
        if plats:
            extras.append(", ".join(str(p) for p in plats))
        if extras:
            parts.append(f" - {' | '.join(extras)}")
    elif obj_type == "case-study":
        extras = [str(obj.get("type") or "")]
        if obj.get("target"):
            extras.append(f"target: {obj['target']}")
        if obj.get("date"):
            extras.append(str(obj["date"]))
        parts.append(f" - {' | '.join(e for e in extras if e)}")
    elif obj_type == "mitigation":
        cats = obj.get("categories") or []
        if cats:
            parts.append(f" - {', '.join(str(c) for c in cats)}")
    return "".join(parts)


def _technique_tactics(index: _AtlasIndex, technique_id: str) -> list[str]:
    """Tactic ids a technique achieves (directly, from its forward edges)."""
    return [
        e.target
        for e in index.forward.get(technique_id, [])
        if e.rel_type == "achieves"
    ]


def _techniques_for_tactic(index: _AtlasIndex, tactic_id: str) -> list[str]:
    """Technique ids that achieve a tactic (reverse edges), in id order."""
    ids = [
        e.source
        for e in index.reverse.get(tactic_id, [])
        if e.rel_type == "achieves" and e.source in index.techniques
    ]
    return sorted(set(ids))


def _parent_technique(index: _AtlasIndex, technique_id: str) -> str:
    for e in index.forward.get(technique_id, []):
        if e.rel_type == "specializes":
            return e.target
    return ""


def _sub_techniques(index: _AtlasIndex, technique_id: str) -> list[str]:
    return sorted(
        e.source
        for e in index.reverse.get(technique_id, [])
        if e.rel_type == "specializes"
    )


def _mitigations_for_technique(index: _AtlasIndex, technique_id: str) -> list[_Edge]:
    return [
        e
        for e in index.reverse.get(technique_id, [])
        if e.rel_type == "mitigates" and e.source in index.mitigations
    ]


def _case_studies_for_technique(index: _AtlasIndex, technique_id: str) -> list[str]:
    ids = [
        e.source
        for e in index.reverse.get(technique_id, [])
        if e.rel_type == "employs" and e.source in index.case_studies
    ]
    return sorted(set(ids))


# ---------------------------------------------------------------------------
# Integration class
# ---------------------------------------------------------------------------


class MitreAtlasIntegration(BaseIntegration):
    provider_name = "mitre_atlas"
    display_name = "MITRE ATLAS"
    icon = "BrainCircuit"
    description = (
        "Connect MITRE ATLAS (Adversarial Threat Landscape for AI Systems) "
        "for AI/ML attack tactics, techniques, mitigations, and real-world "
        "case studies. Open data - no API key required."
    )
    auth_types = ["none"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        index, _err = await fetch_atlas_dataset(resolve_atlas_source_url(token))
        return index is not None

    async def get_user_info(self, token: str) -> dict:
        source_url = resolve_atlas_source_url(token)
        index, err = await fetch_atlas_dataset(source_url)
        if index is None:
            return {"error": err}
        return {
            "release": index.version,
            "format_version": index.format_version,
            "tactics": len(index.tactics),
            "techniques": len(index.techniques),
            "mitigations": len(index.mitigations),
            "case_studies": len(index.case_studies),
        }


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build MITRE ATLAS tools.

    ``token`` is the resolved data source URL (``_get_token`` maps the
    optional ``extra_config.source_url`` override or the official default).
    Tools always build - the dataset is fetched lazily on first call, so the
    catalog's dummy-token introspection never touches the network.
    """
    source_url = resolve_atlas_source_url(token)

    async def _index() -> tuple[_AtlasIndex | None, str | None]:
        return await fetch_atlas_dataset(source_url)

    # ══════════════════════════════════════════════════════════════════════
    # 1. MATRIX + TACTICS (orientation)
    # ══════════════════════════════════════════════════════════════════════

    async def get_atlas_matrix() -> str:
        """Show the ATLAS matrix: tactics in official order with technique counts."""
        index, err = await _index()
        if err:
            return err
        lines = [
            f"MITRE ATLAS matrix - release {index.version} "
            f"({len(index.techniques)} techniques across "
            f"{len(index.tactics)} tactics):",
        ]
        for pos, tid in enumerate(index.matrix_tactic_order, start=1):
            tactic = index.tactics.get(tid) or {}
            technique_ids = _techniques_for_tactic(index, tid)
            top_level = [t for t in technique_ids if "." not in t.split("AML.T")[-1]]
            subs = len(technique_ids) - len(top_level)
            sub_part = f" + {subs} sub-techniques" if subs else ""
            lines.append(
                f"  {pos}. [{tid}] {tactic.get('name', '?')} - "
                f"{len(top_level)} techniques{sub_part}"
            )
        lines.append(
            "\nUse get_atlas_tactic for a tactic's techniques, "
            "list_atlas_techniques to filter techniques, or search_atlas "
            f"for keyword search. Matrix: {ATLAS_PUBLIC_URL}/matrices/ATLAS"
        )
        return "\n".join(lines)

    async def list_atlas_tactics(limit: int = 25, offset: int = 0) -> str:
        """List all ATLAS tactics (adversary goals) in matrix order."""
        index, err = await _index()
        if err:
            return err
        ordered = [
            index.tactics[tid] for tid in index.matrix_tactic_order
            if tid in index.tactics
        ]
        page, total, lim, off = _paginate(ordered, limit, offset)
        if not page and total:
            return _offset_error(total, off)
        lines = [f"Found {total} ATLAS tactics (matrix order):"]
        for tactic in page:
            tid = tactic.get("id", "?")
            n_techniques = len(_techniques_for_tactic(index, tid))
            desc = _trunc(tactic.get("description"), 160)
            first_sentence = desc.split(". ")[0].rstrip(".") if desc else ""
            summary = f" {first_sentence}." if first_sentence else ""
            lines.append(
                f"  [{tid}] {tactic.get('name', '?')} - "
                f"{n_techniques} technique(s).{summary}"
            )
        lines.append(
            "\nUse get_atlas_tactic(tactic_id) for detail and the technique list."
        )
        return "\n".join(lines) + _more_hint(total, lim, off)

    async def get_atlas_tactic(tactic_id: str) -> str:
        """Get one tactic's detail plus every technique that achieves it."""
        index, err = await _index()
        if err:
            return err
        tid = _normalize_atlas_id(tactic_id)
        if not _ID_PATTERNS["tactic"].match(tid):
            return _id_error(tactic_id, "tactic")
        tactic = index.tactics.get(tid)
        if tactic is None:
            return (
                f"Error: tactic {tid} was not found in ATLAS release "
                f"{index.version}. Use list_atlas_tactics to see valid ids."
            )
        lines = [
            f"[{tid}] {tactic.get('name', '?')} (tactic)",
            f"Link: {_deep_link(tactic)}",
        ]
        ref = tactic.get("attack-reference")
        if isinstance(ref, dict) and ref.get("id"):
            lines.append(
                f"ATT&CK counterpart: {ref['id']} ({ref.get('url', 'no url')})"
            )
        lines.append(f"\n{_trunc(tactic.get('description'))}")
        technique_ids = _techniques_for_tactic(index, tid)
        if technique_ids:
            lines.append(f"\nTechniques achieving this tactic ({len(technique_ids)}):")
            for t_id in technique_ids:
                technique = index.techniques.get(t_id) or {}
                maturity = technique.get("maturity") or "?"
                lines.append(
                    f"  [{t_id}] {technique.get('name', '?')} ({maturity})"
                )
        lines.append("\nUse get_atlas_technique(technique_id) for a full dossier.")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 2. TECHNIQUES (threat knowledge)
    # ══════════════════════════════════════════════════════════════════════

    async def list_atlas_techniques(
        tactic_id: str = "",
        platform: str = "",
        maturity: str = "",
        parent_only: bool = False,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Browse/filter ATLAS techniques by tactic, platform, and maturity."""
        index, err = await _index()
        if err:
            return err
        candidates = list(index.techniques.values())

        if tactic_id:
            tid = _normalize_atlas_id(tactic_id)
            if not _ID_PATTERNS["tactic"].match(tid):
                return _id_error(tactic_id, "tactic")
            allowed = set(_techniques_for_tactic(index, tid))
            candidates = [t for t in candidates if t.get("id") in allowed]

        if platform:
            wanted = platform.strip().lower()
            candidates = [
                t
                for t in candidates
                if any(wanted == str(p).lower() for p in (t.get("platforms") or []))
            ]

        if maturity:
            wanted = maturity.strip().lower()
            candidates = [
                t for t in candidates if str(t.get("maturity", "")).lower() == wanted
            ]

        if parent_only:
            candidates = [
                t for t in candidates if "." not in str(t.get("id", ""))[5:]
            ]

        candidates.sort(key=lambda t: str(t.get("id", "")))
        page, total, lim, off = _paginate(candidates, limit, offset)
        if not page:
            if total:
                return _offset_error(total, off)
            return (
                "No ATLAS techniques match these filters. Valid platforms: "
                "Predictive AI, Generative AI, Agentic AI, Enterprise. Valid "
                "maturity levels: Feasible, Demonstrated, Realized."
            )
        lines = [f"Found {total} ATLAS technique(s):"]
        for technique in page:
            t_id = str(technique.get("id", "?"))
            plats = ", ".join(str(p) for p in (technique.get("platforms") or []))
            tactic_ids = _technique_tactics(index, t_id)
            lines.append(
                f"  [{t_id}] {technique.get('name', '?')} - "
                f"{technique.get('maturity', '?')}"
                + (f" | {plats}" if plats else "")
                + (f" | tactics: {', '.join(tactic_ids)}" if tactic_ids else "")
            )
        lines.append("\nUse get_atlas_technique(technique_id) for a full dossier.")
        return "\n".join(lines) + _more_hint(total, lim, off)

    async def get_atlas_technique(technique_id: str) -> str:
        """Full technique dossier: description, context, defenses, incidents."""
        index, err = await _index()
        if err:
            return err
        t_id = _normalize_atlas_id(technique_id)
        if not _ID_PATTERNS["technique"].match(t_id):
            return _id_error(technique_id, "technique")
        technique = index.techniques.get(t_id)
        if technique is None:
            return (
                f"Error: technique {t_id} was not found in ATLAS release "
                f"{index.version}. Use search_atlas or list_atlas_techniques "
                f"to find valid ids."
            )
        lines = [
            f"[{t_id}] {technique.get('name', '?')} (technique)",
            f"Link: {_deep_link(technique)}",
            f"Maturity: {technique.get('maturity', '?')} | Platforms: "
            + (
                ", ".join(str(p) for p in (technique.get("platforms") or []))
                or "N/A"
            )
            + f" | Modified: {technique.get('modified-date', '?')}",
        ]
        ref = technique.get("attack-reference")
        if isinstance(ref, dict) and ref.get("id"):
            lines.append(
                f"ATT&CK counterpart: {ref['id']} ({ref.get('url', 'no url')})"
            )
        parent = _parent_technique(index, t_id)
        if parent:
            parent_obj = index.techniques.get(parent) or {}
            lines.append(
                f"Sub-technique of: [{parent}] {parent_obj.get('name', '?')}"
            )
        tactic_ids = _technique_tactics(index, t_id)
        if tactic_ids:
            named = [
                f"[{tid}] {(index.tactics.get(tid) or {}).get('name', '?')}"
                for tid in tactic_ids
            ]
            lines.append(f"Tactics: {', '.join(named)}")
        lines.append(f"\n{_trunc(technique.get('description'), 1400)}")

        subs = _sub_techniques(index, t_id)
        if subs:
            lines.append(f"\nSub-techniques ({len(subs)}):")
            for sid in subs:
                sub = index.techniques.get(sid) or {}
                lines.append(f"  [{sid}] {sub.get('name', '?')}")

        mitigation_edges = _mitigations_for_technique(index, t_id)
        if mitigation_edges:
            lines.append(f"\nMitigations ({len(mitigation_edges)}):")
            for e in mitigation_edges:
                mitigation = index.mitigations.get(e.source) or {}
                lines.append(f"  [{e.source}] {mitigation.get('name', '?')}")

        cs_ids = _case_studies_for_technique(index, t_id)
        if cs_ids:
            lines.append(f"\nCase studies employing this technique ({len(cs_ids)}):")
            for cid in cs_ids[:10]:
                cs = index.case_studies.get(cid) or {}
                lines.append(
                    f"  [{cid}] {cs.get('name', '?')} "
                    f"({cs.get('type', '?')}, {cs.get('date', '?')})"
                )
            if len(cs_ids) > 10:
                lines.append(f"  ... and {len(cs_ids) - 10} more")

        refs = technique.get("references") or []
        if refs:
            lines.append(f"\nReferences ({min(len(refs), 5)} of {len(refs)}):")
            for r in refs[:5]:
                lines.append(f"  - {r.get('title', '?')} ({r.get('url', 'no url')})")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 3. SEARCH + STATUS
    # ══════════════════════════════════════════════════════════════════════

    async def search_atlas(
        query: str,
        object_types: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Keyword search across all ATLAS tactics/techniques/mitigations/case studies."""
        index, err = await _index()
        if err:
            return err
        needle = (query or "").strip().lower()
        if not needle:
            return "Error: query is required (a keyword or phrase, e.g. 'prompt injection')."

        wanted_types: set[str] = set()
        for raw in (object_types or "").split(","):
            t = raw.strip().lower().replace("_", "-").replace("case-studies", "case-study")
            if not t:
                continue
            if t.endswith("s") and t[:-1] in _OBJECT_TYPES:
                t = t[:-1]
            if t not in _OBJECT_TYPES:
                return (
                    f"Error: '{raw.strip()}' is not a valid object type. Valid "
                    f"values: {', '.join(_OBJECT_TYPES)} (comma-separated)."
                )
            wanted_types.add(t)

        scored: list[tuple[int, str, dict]] = []
        for oid, obj in index.objects.items():
            obj_type = str(obj.get("object-type") or "")
            if obj_type not in _OBJECT_TYPES:
                continue
            if wanted_types and obj_type not in wanted_types:
                continue
            name = str(obj.get("name") or "").lower()
            haystacks = [
                str(obj.get("description") or "").lower(),
                str(obj.get("actor") or "").lower(),
                str(obj.get("target") or "").lower(),
            ]
            if needle in name or needle == oid.lower():
                scored.append((2, oid, obj))
            elif any(needle in h for h in haystacks):
                scored.append((1, oid, obj))
        scored.sort(key=lambda s: (-s[0], s[1]))

        page, total, lim, off = _paginate(scored, limit, offset)
        if not page:
            if total:
                return _offset_error(total, off)
            return (
                f"No ATLAS objects match '{query}'. Try a broader keyword or "
                "list_atlas_techniques / list_atlas_tactics to browse."
            )
        lines = [f"Found {total} ATLAS object(s) matching '{query}':"]
        for _score, _oid, obj in page:
            lines.append(f"  {_summary_line(obj)}")
        lines.append(
            "\nUse the matching get_atlas_* tool with the id for full detail."
        )
        return "\n".join(lines) + _more_hint(total, lim, off)

    async def get_atlas_status(refresh: bool = False) -> str:
        """Report the loaded ATLAS release, object counts, source, and cache age."""
        cached = _CACHE.get(source_url)
        index, err = await fetch_atlas_dataset(source_url, force_refresh=refresh)
        if err:
            return err
        # Freshness reflects what ACTUALLY happened, not the refresh flag: a
        # forced refresh that failed serves the stale cache (same index
        # object), and reporting that as "just refreshed" would hide the
        # failure. A real fetch always builds a new index object.
        age_s = int(time.monotonic() - index.fetched_at)
        fetched_now = index is not cached
        if refresh and not fetched_now:
            freshness = (
                f"refresh FAILED - serving data cached {age_s // 60} min ago "
                f"(the upstream source is unreachable; see server logs)"
            )
        elif fetched_now:
            freshness = "just refreshed" + (
                " (new data loaded)" if refresh and cached is not None else ""
            )
        else:
            freshness = f"cached {age_s // 60} min ago"
        # Only the well-known official URL is surfaced; a configured mirror
        # may be internal infrastructure and never leaves the server.
        source_label = (
            source_url
            if source_url == ATLAS_DEFAULT_SOURCE_URL
            else "custom mirror (configured in the integration)"
        )
        lines = [
            f"MITRE ATLAS release {index.version} "
            f"(data format {index.format_version}, modified "
            f"{index.modified_date or 'unknown'}) - {freshness}",
            f"Objects: {_count_noun(len(index.tactics), 'tactic')}, "
            f"{_count_noun(len(index.techniques), 'technique')} "
            f"(incl. sub-techniques), "
            f"{_count_noun(len(index.mitigations), 'mitigation')}, "
            f"{_count_noun(len(index.case_studies), 'case study', 'case studies')}",
            f"Relationships: "
            f"{_count_noun(sum(len(v) for v in index.forward.values()), 'typed edge')}"
            f" | ATT&CK-mapped objects: "
            # Unique ATLAS objects: one object can reference several ATT&CK
            # ids, so summing per-id list lengths would double-count it.
            f"{len({oid for ids in index.attack_index.values() for oid in ids})}",
            f"Data source: {source_label}",
            f"Website: {ATLAS_PUBLIC_URL}",
        ]
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 4. MITIGATIONS (defense planning)
    # ══════════════════════════════════════════════════════════════════════

    async def list_atlas_mitigations(
        category: str = "",
        lifecycle_phase: str = "",
        technique_id: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Browse/filter ATLAS mitigations by category, lifecycle phase, technique."""
        index, err = await _index()
        if err:
            return err
        candidates = list(index.mitigations.values())

        if category:
            wanted = category.strip().lower()
            candidates = [
                m
                for m in candidates
                if any(wanted == str(c).lower() for c in (m.get("categories") or []))
            ]

        if lifecycle_phase:
            wanted = lifecycle_phase.strip().lower()
            candidates = [
                m
                for m in candidates
                if any(
                    wanted == str(p).lower()
                    for p in (m.get("lifecycle-phases") or [])
                )
            ]

        if technique_id:
            t_id = _normalize_atlas_id(technique_id)
            if not _ID_PATTERNS["technique"].match(t_id):
                return _id_error(technique_id, "technique")
            allowed = {e.source for e in _mitigations_for_technique(index, t_id)}
            candidates = [m for m in candidates if m.get("id") in allowed]

        candidates.sort(key=lambda m: str(m.get("id", "")))
        page, total, lim, off = _paginate(candidates, limit, offset)
        if not page:
            if total:
                return _offset_error(total, off)
            return (
                "No ATLAS mitigations match these filters. Valid categories: "
                "Policy, Technical - AI, Technical - Cyber. Valid lifecycle "
                "phases: Business and Data Understanding, Data Preparation, "
                "AI Model Engineering, AI Model Evaluation, Deployment, "
                "Monitoring and Maintenance."
            )
        lines = [f"Found {total} ATLAS mitigation(s):"]
        for mitigation in page:
            m_id = str(mitigation.get("id", "?"))
            n_techniques = len(
                [
                    e
                    for e in index.forward.get(m_id, [])
                    if e.rel_type == "mitigates"
                ]
            )
            cats = ", ".join(str(c) for c in (mitigation.get("categories") or []))
            phases = ", ".join(
                str(p) for p in (mitigation.get("lifecycle-phases") or [])
            )
            lines.append(
                f"  [{m_id}] {mitigation.get('name', '?')}"
                + (f" - {cats}" if cats else "")
                + (f" | phases: {phases}" if phases else "")
                + f" | mitigates {n_techniques} technique(s)"
            )
        lines.append("\nUse get_atlas_mitigation(mitigation_id) for full detail.")
        return "\n".join(lines) + _more_hint(total, lim, off)

    async def get_atlas_mitigation(mitigation_id: str) -> str:
        """One mitigation's detail plus every technique it mitigates."""
        index, err = await _index()
        if err:
            return err
        m_id = _normalize_atlas_id(mitigation_id)
        if not _ID_PATTERNS["mitigation"].match(m_id):
            return _id_error(mitigation_id, "mitigation")
        mitigation = index.mitigations.get(m_id)
        if mitigation is None:
            return (
                f"Error: mitigation {m_id} was not found in ATLAS release "
                f"{index.version}. Use list_atlas_mitigations to see valid ids."
            )
        lines = [
            f"[{m_id}] {mitigation.get('name', '?')} (mitigation)",
            f"Link: {_deep_link(mitigation)}",
            "Categories: "
            + (
                ", ".join(str(c) for c in (mitigation.get("categories") or []))
                or "N/A"
            )
            + " | Lifecycle phases: "
            + (
                ", ".join(str(p) for p in (mitigation.get("lifecycle-phases") or []))
                or "N/A"
            ),
        ]
        ref = mitigation.get("attack-reference")
        if isinstance(ref, dict) and ref.get("id"):
            lines.append(
                f"ATT&CK counterpart: {ref['id']} ({ref.get('url', 'no url')})"
            )
        lines.append(f"\n{_trunc(mitigation.get('description'))}")
        edges = [
            e for e in index.forward.get(m_id, []) if e.rel_type == "mitigates"
        ]
        if edges:
            lines.append(f"\nMitigated techniques ({len(edges)}):")
            for e in edges:
                technique = index.techniques.get(e.target) or {}
                lines.append(f"  [{e.target}] {technique.get('name', '?')}")
                if e.description:
                    lines.append(f"    Use: {_trunc(e.description, 220)}")
        lines.append("\nUse get_atlas_technique(technique_id) for technique detail.")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 5. CASE STUDIES (real-world incidents + exercises)
    # ══════════════════════════════════════════════════════════════════════

    async def list_atlas_case_studies(
        case_type: str = "",
        technique_id: str = "",
        keyword: str = "",
        since_year: int = 0,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Browse/filter the ATLAS corpus of real-world AI attack case studies."""
        index, err = await _index()
        if err:
            return err
        candidates = list(index.case_studies.values())

        if case_type:
            wanted = case_type.strip().lower()
            if wanted not in ("incident", "exercise"):
                return (
                    f"Error: '{case_type}' is not a valid case study type. "
                    "Valid values: Incident (real-world attack), Exercise "
                    "(red-team / research demonstration)."
                )
            candidates = [
                c for c in candidates if str(c.get("type", "")).lower() == wanted
            ]

        if technique_id:
            t_id = _normalize_atlas_id(technique_id)
            if not _ID_PATTERNS["technique"].match(t_id):
                return _id_error(technique_id, "technique")
            allowed = set(_case_studies_for_technique(index, t_id))
            candidates = [c for c in candidates if c.get("id") in allowed]

        if keyword:
            needle = keyword.strip().lower()
            candidates = [
                c
                for c in candidates
                if needle
                in " ".join(
                    str(c.get(k) or "")
                    for k in ("name", "description", "actor", "target", "reporter")
                ).lower()
            ]

        try:
            since_year = int(since_year)
        except (TypeError, ValueError):
            since_year = 0
        if since_year:
            candidates = [
                c
                for c in candidates
                if str(c.get("date") or "")[:4].isdigit()
                and int(str(c["date"])[:4]) >= since_year
            ]

        candidates.sort(key=lambda c: str(c.get("date") or ""), reverse=True)
        page, total, lim, off = _paginate(candidates, limit, offset)
        if not page:
            if total:
                return _offset_error(total, off)
            return (
                "No ATLAS case studies match these filters. Drop a filter or "
                "use search_atlas for a broader keyword search."
            )
        noun = "case study" if total == 1 else "case studies"
        lines = [f"Found {total} ATLAS {noun} (newest first):"]
        for cs in page:
            c_id = str(cs.get("id", "?"))
            n_steps = len(
                [e for e in index.forward.get(c_id, []) if e.rel_type == "employs"]
            )
            lines.append(
                f"  [{c_id}] {cs.get('name', '?')} - {cs.get('type', '?')} | "
                f"{cs.get('date', '?')} | target: {cs.get('target', '?')} | "
                f"{n_steps} attack step(s)"
            )
        lines.append(
            "\nUse get_atlas_case_study(case_study_id) for the full report "
            "and attack chain."
        )
        return "\n".join(lines) + _more_hint(total, lim, off)

    async def get_atlas_case_study(case_study_id: str) -> str:
        """Full case study report: summary, actor, target, and the attack chain."""
        index, err = await _index()
        if err:
            return err
        c_id = _normalize_atlas_id(case_study_id)
        if not _ID_PATTERNS["case-study"].match(c_id):
            return _id_error(case_study_id, "case-study")
        cs = index.case_studies.get(c_id)
        if cs is None:
            return (
                f"Error: case study {c_id} was not found in ATLAS release "
                f"{index.version}. Use list_atlas_case_studies to see valid ids."
            )
        lines = [
            f"[{c_id}] {cs.get('name', '?')} (case study - {cs.get('type', '?')})",
            f"Link: {_deep_link(cs)}",
            f"Date: {cs.get('date', '?')} | Actor: {cs.get('actor') or 'unknown'} | "
            f"Target: {cs.get('target') or 'unknown'}"
            + (f" | Reporter: {cs['reporter']}" if cs.get("reporter") else ""),
            f"\n{_trunc(cs.get('description'), 1400)}",
        ]

        steps = [
            e for e in index.forward.get(c_id, []) if e.rel_type == "employs"
        ]
        # Follow the documented step order: step-id ("S00", "S01", ...) is the
        # authoring order of the attack chain (leads-to encodes branches).
        steps.sort(key=lambda e: e.step_id or "")
        if steps:
            lines.append(f"\nAttack chain ({len(steps)} steps):")
            for e in steps:
                technique = index.techniques.get(e.target) or {}
                tactic = index.tactics.get(e.tactic) or {}
                branch = (
                    f" -> {', '.join(e.leads_to)}"
                    if e.leads_to and len(e.leads_to) > 1
                    else ""
                )
                lines.append(
                    f"  {e.step_id or '?'}: [{e.target}] "
                    f"{technique.get('name', '?')}"
                    + (f" ({tactic.get('name', '?')})" if tactic else "")
                    + branch
                )
                if e.description:
                    lines.append(f"    {_trunc(e.description, 300)}")

        refs = cs.get("references") or []
        if refs:
            lines.append(f"\nReferences ({min(len(refs), 5)} of {len(refs)}):")
            for r in refs[:5]:
                lines.append(f"  - {r.get('title', '?')} ({r.get('url', 'no url')})")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 6. RELATIONSHIP TRAVERSAL + ATT&CK CROSSWALK (pivoting)
    # ══════════════════════════════════════════════════════════════════════

    async def get_atlas_related_objects(
        object_id: str,
        relationship_type: str = "",
    ) -> str:
        """Traverse typed relationships from any ATLAS id, in both directions."""
        index, err = await _index()
        if err:
            return err
        oid = _normalize_atlas_id(object_id)
        obj = index.objects.get(oid)
        if obj is None:
            return (
                f"Error: '{object_id}' was not found in ATLAS release "
                f"{index.version}. Pass a tactic (AML.TA####), technique "
                f"(AML.T####), mitigation (AML.M####), or case study "
                f"(AML.CS####) id - use search_atlas to find one."
            )
        rel_filter = (relationship_type or "").strip().lower()
        if rel_filter and rel_filter not in _REL_TYPES:
            return (
                f"Error: '{relationship_type}' is not a valid relationship "
                f"type. Valid values: {', '.join(_REL_TYPES)}."
            )

        lines = [f"Relationships of {_summary_line(obj)}:"]
        outgoing = [
            e
            for e in index.forward.get(oid, [])
            if not rel_filter or e.rel_type == rel_filter
        ]
        incoming = [
            e
            for e in index.reverse.get(oid, [])
            if not rel_filter or e.rel_type == rel_filter
        ]
        if outgoing:
            lines.append(f"\nOutgoing ({len(outgoing)}):")
            for e in outgoing:
                target = index.objects.get(e.target) or {"id": e.target}
                lines.append(f"  --{e.rel_type}--> {_summary_line(target)}")
        if incoming:
            lines.append(f"\nIncoming ({len(incoming)}):")
            for e in incoming:
                source = index.objects.get(e.source) or {"id": e.source}
                lines.append(f"  <--{e.rel_type}-- {_summary_line(source)}")
        if not outgoing and not incoming:
            lines.append(
                "  No relationships"
                + (f" of type '{rel_filter}'" if rel_filter else "")
                + " found for this object."
            )
        else:
            lines.append(
                "\nRelationship semantics: technique --achieves--> tactic, "
                "sub-technique --specializes--> technique, mitigation "
                "--mitigates--> technique, case study --employs--> technique, "
                "matrix --sequences--> tactic."
            )
        return "\n".join(lines)

    async def map_attack_to_atlas(attack_id: str = "") -> str:
        """Cross-walk an ATT&CK id to ATLAS objects, or list all mappings."""
        index, err = await _index()
        if err:
            return err
        wanted = (attack_id or "").strip().upper()

        if not wanted:
            if not index.attack_index:
                return "No ATT&CK-mapped objects exist in this ATLAS release."
            lines = [
                f"All ATT&CK -> ATLAS mappings in release {index.version} "
                f"({len(index.attack_index)} ATT&CK ids):"
            ]
            for aid in sorted(index.attack_index):
                mapped = ", ".join(
                    f"[{oid}] {(index.objects.get(oid) or {}).get('name', '?')}"
                    for oid in index.attack_index[aid]
                )
                lines.append(f"  {aid} -> {mapped}")
            return "\n".join(lines)

        if not _ATTACK_ID_RE.match(wanted):
            return (
                f"Error: '{attack_id}' is not a valid MITRE ATT&CK id - "
                "expected TA#### (tactic), T#### / T####.### (technique), or "
                "M#### (mitigation). Leave attack_id empty to list every "
                "mapping."
            )

        atlas_ids = index.attack_index.get(wanted, [])
        if not atlas_ids:
            return (
                f"No ATLAS object references ATT&CK {wanted} in release "
                f"{index.version}. Not every ATT&CK id has an AI-specific "
                "counterpart - leave attack_id empty to list all "
                f"{len(index.attack_index)} mapped ids, or use search_atlas."
            )
        lines = [f"ATT&CK {wanted} maps to {len(atlas_ids)} ATLAS object(s):"]
        for oid in atlas_ids:
            obj = index.objects.get(oid) or {"id": oid}
            lines.append(f"  {_summary_line(obj)}")
            link = _deep_link(obj)
            if link:
                lines.append(f"    Link: {link}")
        lines.append(
            "\nUse the matching get_atlas_* tool with the ATLAS id for detail."
        )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("get_atlas_matrix", get_atlas_matrix)
    executor.register("list_atlas_tactics", list_atlas_tactics)
    executor.register("get_atlas_tactic", get_atlas_tactic)
    executor.register("list_atlas_techniques", list_atlas_techniques)
    executor.register("get_atlas_technique", get_atlas_technique)
    executor.register("search_atlas", search_atlas)
    executor.register("get_atlas_status", get_atlas_status)
    executor.register("list_atlas_mitigations", list_atlas_mitigations)
    executor.register("get_atlas_mitigation", get_atlas_mitigation)
    executor.register("list_atlas_case_studies", list_atlas_case_studies)
    executor.register("get_atlas_case_study", get_atlas_case_study)
    executor.register("get_atlas_related_objects", get_atlas_related_objects)
    executor.register("map_attack_to_atlas", map_attack_to_atlas)

    _pagination_props = {
        "limit": {
            "type": "integer",
            "default": 25,
            "description": "Maximum results to return (1-100).",
        },
        "offset": {
            "type": "integer",
            "default": 0,
            "description": "Number of results to skip (for pagination).",
        },
    }

    return [
        ToolDefinition(
            name="get_atlas_matrix",
            description=(
                "Show the MITRE ATLAS matrix: the release version and every "
                "AI-attack tactic in official column order with its technique "
                "count. Use this first to orient yourself in the ATLAS "
                "knowledge base before drilling into tactics or techniques. "
                "ATLAS covers attacks on AI-enabled systems (Predictive AI, "
                "Generative AI, Agentic AI) - for classic enterprise attacks "
                "use MITRE ATT&CK instead."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_atlas_tactics",
            description=(
                "List all MITRE ATLAS tactics (adversary goals against AI "
                "systems, e.g. AI Model Access, AI Attack Staging, Exfiltration) "
                "in matrix order with technique counts and a one-line summary. "
                "Returns tactic ids (AML.TA####) for get_atlas_tactic."
            ),
            input_schema={
                "type": "object",
                "properties": dict(_pagination_props),
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_atlas_tactic",
            description=(
                "Get one ATLAS tactic's full description, its ATT&CK "
                "counterpart (when one exists), the atlas.mitre.org link, and "
                "every technique that achieves it (with maturity levels). Use "
                "after get_atlas_matrix or list_atlas_tactics to explore an "
                "adversary goal."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tactic_id": {
                        "type": "string",
                        "description": "ATLAS tactic id, e.g. AML.TA0000 (bare TA0000 also accepted).",
                    },
                },
                "required": ["tactic_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_atlas_techniques",
            description=(
                "Browse and filter MITRE ATLAS techniques (how adversaries "
                "attack AI systems). Filter by tactic (tactic_id), platform "
                "(Predictive AI / Generative AI / Agentic AI / Enterprise), "
                "maturity (Feasible = theoretical, Demonstrated = shown in "
                "exercises/research, Realized = observed in real incidents), "
                "or parent_only to hide sub-techniques. Returns technique ids "
                "(AML.T####) for get_atlas_technique. Prefer search_atlas when "
                "you only have a keyword."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tactic_id": {
                        "type": "string",
                        "description": "Only techniques achieving this tactic (e.g. AML.TA0001).",
                    },
                    "platform": {
                        "type": "string",
                        "enum": [
                            "Predictive AI",
                            "Generative AI",
                            "Agentic AI",
                            "Enterprise",
                        ],
                        "description": "Only techniques applicable to this AI platform type.",
                    },
                    "maturity": {
                        "type": "string",
                        "enum": ["Feasible", "Demonstrated", "Realized"],
                        "description": "Only techniques at this maturity level.",
                    },
                    "parent_only": {
                        "type": "boolean",
                        "default": False,
                        "description": "Hide sub-techniques (AML.T####.###) from the list.",
                    },
                    **_pagination_props,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_atlas_technique",
            description=(
                "Get the full MITRE ATLAS dossier for one technique or "
                "sub-technique: description, maturity, AI platforms, ATT&CK "
                "counterpart, tactics achieved, parent/sub-techniques, the "
                "mitigations that address it, the real-world case studies that "
                "employed it, and reference links. This is the deepest single "
                "lookup - use it once you have a technique id from "
                "list_atlas_techniques or search_atlas."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "technique_id": {
                        "type": "string",
                        "description": (
                            "ATLAS technique id, e.g. AML.T0051 or sub-technique "
                            "AML.T0051.000 (bare T0051 also accepted)."
                        ),
                    },
                },
                "required": ["technique_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_atlas",
            description=(
                "Keyword search across ALL MITRE ATLAS objects - tactics, "
                "techniques, mitigations, and case studies - by name, "
                "description, actor, or target (name matches rank first). The "
                "fastest way from a concept ('prompt injection', 'model "
                "poisoning', 'ChatGPT') to ATLAS ids. Optionally restrict to "
                "specific object types."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keyword or phrase, e.g. 'prompt injection'.",
                    },
                    "object_types": {
                        "type": "string",
                        "description": (
                            "Comma-separated types to include: tactic, technique, "
                            "mitigation, case-study. Empty = all types."
                        ),
                    },
                    **_pagination_props,
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_atlas_status",
            description=(
                "Report the loaded MITRE ATLAS release: content version, "
                "object and relationship counts, the data source (the "
                "official URL, or 'custom mirror' when overridden), and "
                "cache freshness. Pass refresh=true to force a re-download of the "
                "dataset (e.g. right after MITRE publishes a new monthly "
                "release). Use to verify the knowledge base is available and "
                "current."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "refresh": {
                        "type": "boolean",
                        "default": False,
                        "description": "Force a fresh download instead of the 6-hour cache.",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_atlas_mitigations",
            description=(
                "Browse and filter MITRE ATLAS mitigations (defenses against "
                "AI/ML attacks). Filter by category (Policy / Technical - AI / "
                "Technical - Cyber), AI lifecycle phase (Business and Data "
                "Understanding, Data Preparation, AI Model Engineering, AI "
                "Model Evaluation, Deployment, Monitoring and Maintenance), or "
                "by the technique they mitigate. Use for AI security hardening "
                "plans and control gap analysis. Returns mitigation ids "
                "(AML.M####) for get_atlas_mitigation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["Policy", "Technical - AI", "Technical - Cyber"],
                        "description": "Only mitigations in this category.",
                    },
                    "lifecycle_phase": {
                        "type": "string",
                        "enum": [
                            "Business and Data Understanding",
                            "Data Preparation",
                            "AI Model Engineering",
                            "AI Model Evaluation",
                            "Deployment",
                            "Monitoring and Maintenance",
                        ],
                        "description": "Only mitigations applied in this AI lifecycle phase.",
                    },
                    "technique_id": {
                        "type": "string",
                        "description": (
                            "Only mitigations addressing this technique "
                            "(e.g. AML.T0051)."
                        ),
                    },
                    **_pagination_props,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_atlas_mitigation",
            description=(
                "Get one MITRE ATLAS mitigation's full detail: description, "
                "categories, AI lifecycle phases, ATT&CK counterpart, and "
                "every technique it mitigates with the per-technique usage "
                "note. Use to turn an identified AI threat into concrete "
                "defensive actions."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "mitigation_id": {
                        "type": "string",
                        "description": "ATLAS mitigation id, e.g. AML.M0005 (bare M0005 also accepted).",
                    },
                },
                "required": ["mitigation_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_atlas_case_studies",
            description=(
                "Browse and filter MITRE ATLAS case studies - the documented "
                "corpus of real-world attacks on AI systems. Filter by type "
                "(Incident = a real attack that happened, Exercise = a "
                "red-team or research demonstration), by technique employed, "
                "by a keyword over name/summary/actor/target, or by year. "
                "Results are newest-first. Use for threat briefings, 'has "
                "this happened in the wild?' questions, and precedent "
                "research. Returns case study ids (AML.CS####)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_type": {
                        "type": "string",
                        "enum": ["Incident", "Exercise"],
                        "description": "Only case studies of this type.",
                    },
                    "technique_id": {
                        "type": "string",
                        "description": (
                            "Only case studies employing this technique "
                            "(e.g. AML.T0051)."
                        ),
                    },
                    "keyword": {
                        "type": "string",
                        "description": (
                            "Substring match over name, summary, actor, target, "
                            "and reporter (e.g. 'ChatGPT')."
                        ),
                    },
                    "since_year": {
                        "type": "integer",
                        "description": "Only case studies dated this year or later (e.g. 2023).",
                    },
                    **_pagination_props,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_atlas_case_study",
            description=(
                "Get one MITRE ATLAS case study's full report: summary, actor, "
                "target, date, reporter, external references, and the ordered "
                "attack chain - each step with its ATLAS technique and tactic "
                "plus what the attacker did. Use to reconstruct how a "
                "real-world AI attack unfolded end to end."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_study_id": {
                        "type": "string",
                        "description": "ATLAS case study id, e.g. AML.CS0016 (bare CS0016 also accepted).",
                    },
                },
                "required": ["case_study_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_atlas_related_objects",
            description=(
                "Traverse the MITRE ATLAS relationship graph from ANY object "
                "id (tactic, technique, mitigation, or case study) in both "
                "directions, optionally filtered by relationship type: "
                "sequences (matrix -> tactic order), achieves (technique -> "
                "tactic), specializes (sub-technique -> parent), mitigates "
                "(mitigation -> technique), employs (case study -> "
                "technique). The generic pivot when the dedicated get_atlas_* "
                "tools don't cover the direction you need."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_id": {
                        "type": "string",
                        "description": (
                            "Any ATLAS id: AML.TA#### / AML.T#### / "
                            "AML.T####.### / AML.M#### / AML.CS####."
                        ),
                    },
                    "relationship_type": {
                        "type": "string",
                        "enum": [
                            "sequences",
                            "achieves",
                            "specializes",
                            "mitigates",
                            "employs",
                        ],
                        "description": "Only edges of this type (omit for all).",
                    },
                },
                "required": ["object_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="map_attack_to_atlas",
            description=(
                "Cross-walk a MITRE ATT&CK id to its ATLAS counterpart(s): "
                "pass an ATT&CK tactic (TA####), technique (T#### or "
                "T####.###), or mitigation (M####) id to find the ATLAS "
                "objects that reference it, or leave attack_id empty to list "
                "every ATT&CK -> ATLAS mapping in the release. Use when "
                "pivoting from ATT&CK-based intel (SIEM detections, threat "
                "reports) into the AI-specific threat landscape."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "attack_id": {
                        "type": "string",
                        "description": (
                            "ATT&CK id, e.g. TA0043, T1595, T1595.002, or "
                            "M1056. Empty = list all mappings."
                        ),
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
