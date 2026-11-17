# SPDX-License-Identifier: Apache-2.0

"""Platform URL validation and auto-correction for assistant responses.

Deterministic post-processing layer that runs over the FINAL
assistant text before persistence / delivery:

1. **Detect** platform URLs — markdown links ``[text](url)``, raw
   absolute URLs on a known platform origin, and relative
   ``/dashboard/...`` paths.
2. **Validate** the route segments against a maintained manifest of
   known-valid OpenCTI routes (``OPENCTI_ROUTE_MANIFEST``).
3. **Auto-correct** commonly hallucinated segments via data-driven
   rules (``CORRECTION_RULES``), a unique-section reverse lookup
   (e.g. ``threats/indicators`` -> ``observations/indicators``), and a
   singular-to-plural fix. Any UUID-bearing deep link whose path cannot
   be verified is rewritten to OpenCTI's universal ID resolver route
   ``<base>/dashboard/id/<uuid>`` — valid by construction for any
   entity type.
4. **Fall back** when a URL cannot be validated or corrected: markdown
   links with meaningful text become a prefilled search link
   (``<base>/dashboard/search/<text>``); otherwise the hyperlink is
   dropped and the text degrades gracefully to plain/inline-code text.

Design constraints (hard requirements):

* **No system prompt text** — this module fixes output quality with
  deterministic code only; it must never inject prompt instructions.
* **No network calls** — pure CPU regex/string work. Batch entity
  existence checks (FR3) are deferred until the host platforms expose
  an RBAC-aware batch endpoint; the pipeline stages are composable so
  an existence-check stage can be slotted in later.
* **Fail-open** — any internal error returns the original text
  unchanged (with a warning log). A guard bug must never eat a
  response.
* **External URLs are never touched** — only URLs whose origin matches
  a provided platform base URL (or relative ``/dashboard/`` paths) are
  processed. Code blocks and inline code spans are skipped.

Telemetry: every detection / validation / correction / removal is
logged via the standard ``logging`` logger with structured ``extra``
fields (events ``url.detected``, ``url.validated``, ``url.corrected``,
``url.removed``) and conversation/run correlation ids. High-volume
events (``detected`` / ``validated``) log at DEBUG; mutations
(``corrected`` / ``removed``) log at INFO.

The route manifest and correction rules are defined as data below and
can be extended without code changes via an optional JSON file
(``URL_GUARD_MANIFEST_PATH`` setting, ``{"routes": {...},
"corrections": {...}}``) merged over the built-ins at first use.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import quote, urlparse

from app.core.config import settings
from app.core.utils import format_exception_message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.platform_identity import RegisteredPlatform
    from app.models.integration import Integration

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Route manifest (data — extend via URL_GUARD_MANIFEST_PATH, not code)
# ---------------------------------------------------------------------------
#
# Known-valid OpenCTI route sections under ``/dashboard/``. Keys are
# first-level segments; values are the known second-level segments for
# that section. An EMPTY set means "any second-level segment is
# accepted" (sections whose sub-routes are dynamic: ids, queries,
# settings pages, ...). Validation is intentionally limited to the
# first two path levels — deeper segments (entity UUIDs, knowledge
# tabs, ...) are too dynamic to enumerate and are accepted as-is.
#
# This manifest MUST at minimum validate every route pattern this
# repository itself emits (see ``integrations/providers/opencti/``):
# ``/dashboard/id/<uuid>``, ``/dashboard/workspaces/dashboards/<id>``,
# ``/dashboard/workspaces/investigations/<id>``,
# ``/dashboard/data/import/draft/<id>``.
#
# Section list verified against the OpenCTI front router
# (``opencti-front/src/private/Index.tsx``) — a VALID section missing
# from this manifest is worse than a missing correction rule: the
# guard would "fix" a working native link into a resolver/search
# fallback and break in-app navigation in the embedded chatbot.
OPENCTI_ROUTE_MANIFEST: dict[str, set[str]] = {
    "analyses": {
        "reports",
        "groupings",
        "malware_analyses",
        "notes",
        "external_references",
    },
    "cases": {"incidents", "feedbacks", "rfis", "rfts", "tasks"},
    "events": {"incidents", "sightings", "observed_data"},
    "observations": {"indicators", "observables", "artifacts", "infrastructures"},
    "threats": {
        "threat_actors_group",
        "threat_actors_individual",
        "intrusion_sets",
        "campaigns",
    },
    "arsenal": {"malwares", "channels", "tools", "vulnerabilities"},
    "techniques": {
        "attack_patterns",
        "narratives",
        "courses_of_action",
        "data_components",
        "data_sources",
    },
    "entities": {"sectors", "events", "organizations", "systems", "individuals"},
    "locations": {
        "regions",
        "countries",
        "administrative_areas",
        "cities",
        "positions",
    },
    "data": {
        "entities",
        "relationships",
        "ingestion",
        "import",
        "sharing",
        "processing",
        "connectors",
        "restriction",
    },
    "workspaces": {"dashboards", "investigations"},
    # Dynamic sections — any sub-path accepted.
    "dashboards": set(),
    "settings": set(),
    "profile": set(),
    "search": set(),
    "search_bulk": set(),
    "id": set(),
    # PIRs, audits, trash and the XTM Hub page are top-level sections
    # with dynamic sub-paths (ids / queries).
    "pirs": set(),
    "audits": set(),
    "trash": set(),
    "xtm-hub": set(),
}

# Ordered known-bad -> known-good rewrites applied to the route path
# after ``/dashboard/`` (whole leading segments; first match wins).
# Seeded with the hallucinations observed plus obvious
# legacy-section mistakes; extend via the JSON override, not code.
CORRECTION_RULES: dict[str, str] = {
    # The canonical example: indicators live under observations.
    "threats/indicators": "observations/indicators",
    "threats/observables": "observations/observables",
    "threats/malwares": "arsenal/malwares",
    "threats/vulnerabilities": "arsenal/vulnerabilities",
    "arsenal/attack_patterns": "techniques/attack_patterns",
    "arsenal/intrusion_sets": "threats/intrusion_sets",
    "arsenal/campaigns": "threats/campaigns",
    "techniques/malwares": "arsenal/malwares",
    "observations/reports": "analyses/reports",
    "events/reports": "analyses/reports",
    "analyses/incidents": "events/incidents",
    "entities/countries": "locations/countries",
    "entities/regions": "locations/regions",
    "entities/cities": "locations/cities",
    # Trash is a top-level section (Index.tsx ``/trash/*``), not a Data
    # sub-route — the UI just surfaces it inside the Data menu.
    "data/trash": "trash",
}

# ---------------------------------------------------------------------------
# OpenAEV route manifest (``/admin/...``)
#
# OpenAEV's UI uses a different root segment than OpenCTI, so its links were
# never inspected at all before: the fast bail-out and the relative-path
# alternative both looked only for ``/dashboard``. Section list verified
# against the OpenAEV front router (``openaev-front/src/admin/Index.tsx``
# plus the nested ``settings/Index.tsx``), NOT against
# ``constants/BaseUrls.ts`` - that file still exports a ``PAYLOAD_BASE_URL``
# of ``/admin/payloads`` for which no route exists any more (the screen is
# ``/admin/threat-arsenal``).
#
# An EMPTY set means "any second-level segment is accepted", which is the
# common case here: most OpenAEV sections are a flat list at
# ``/admin/<section>`` with a detail page at ``/admin/<section>/<id>``. The
# value of this manifest is therefore mostly in validating the FIRST segment,
# which is exactly where the stale-route mistakes live.
#
# Same warning as the OpenCTI manifest: a VALID section missing from here is
# worse than a missing correction rule, because the guard would demote a
# working in-app link to plain text.
OPENAEV_ROUTE_MANIFEST: dict[str, set[str]] = {
    "results": set(),
    "fulltextsearch": set(),
    "profile": set(),
    "findings": set(),
    "simulations": set(),
    "atomic_testings": set(),
    "scenarios": set(),
    "deploy-scenario": set(),
    "assets": set(),
    "asset_groups": set(),
    "security_platforms": set(),
    "persons": set(),
    "teams": set(),
    "organizations": set(),
    "components": set(),
    "agents": set(),
    "reporting": set(),
    "threat-arsenal": set(),
    "integrations": set(),
    "settings": set(),
    "workspaces": {"custom_dashboards"},
}

#: OpenAEV sections that are a LIST screen with NO per-item detail route, so a
#: trailing id must be dropped rather than carried onto a path that 404s.
#:
#: An empty sub-set in the manifest above means "any second segment is valid",
#: which is right for the many sections whose detail page is
#: ``/<section>/<id>`` but wrong for these: they have no detail route at all.
#:
#: Kept deliberately minimal. Wrongly marking a section list-only would demote
#: a WORKING link, which is the exact failure mode this manifest exists to
#: avoid, so a section only belongs here once verified against the front
#: router. ``threat-arsenal`` is a single ``<Route path="threat-arsenal">``
#: with no ``:id`` child and no ``/*`` wildcard, unlike ``findings`` (which
#: declares ``findings/:findingId``) or the ``components/*`` style sections.
OPENAEV_LIST_ONLY_SECTIONS: frozenset[str] = frozenset({"threat-arsenal"})

# Known-bad -> known-good rewrites for OpenAEV routes, applied to the path
# after ``/admin/``. Corrections only run on routes that FAILED validation, so
# a rule is only useful when it fixes a first segment that is not in the
# manifest (or a second segment under a section with an explicit sub-set).
#
# That is why the v2 aliases under ``assets/`` and ``teams/`` are NOT listed:
# those sections accept any second segment (their detail pages are
# ``/<section>/<id>``), so ``assets/endpoints/<id>`` validates and never
# reaches the correction stage. It happens to be harmless - OpenAEV still
# declares those legacy paths as redirect routes.
OPENAEV_CORRECTION_RULES: dict[str, str] = {
    # v3 renames whose first segment no longer exists at all.
    "payloads": "threat-arsenal",
    "exercises": "simulations",
    "reports": "reporting",
    # ``workspaces`` has an explicit sub-set, so this one does reach the
    # correction stage.
    "workspaces/dashboards": "workspaces/custom_dashboards",
}

#: Root segment -> (manifest, corrections, supports_id_resolver). OpenCTI has a
#: universal ``/dashboard/id/<uuid>`` resolver and a ``/dashboard/search/<text>``
#: route, so an unfixable link can be salvaged. OpenAEV has neither, so an
#: unfixable OpenAEV link can only be demoted to plain text.
_PLATFORM_PREFIXES: tuple[str, ...] = ("dashboard", "admin")

# Integration providers whose configured base URL points at a platform
# UI whose route structure is guarded here (``/dashboard/`` for OpenCTI,
# ``/admin/`` for OpenAEV).
PLATFORM_URL_PROVIDERS: tuple[str, ...] = ("opencti", "openaev")

# UUID (any version) — OpenCTI internal ids are UUIDs.
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

# Markdown link, raw absolute URL, or raw relative /dashboard path.
# The markdown alternative is first so its URL is consumed whole and
# never re-matched by the raw-URL alternatives. The relative
# alternative's lookbehind only excludes word chars and "/" (so paths
# embedded in hosts or longer paths never match) — a preceding "(" is
# fine: prose like "(/dashboard/...)" must still be guarded, and
# well-formed markdown targets are already consumed by the first
# alternative before the relative one is ever tried.
_URL_TOKEN_RE = re.compile(
    r"\[(?P<text>[^\]]*)\]\((?P<target>[^)\s]+)\)"
    r"|(?P<raw><?https?://[^\s<>\"']+>?)"
    r"|(?P<rel>(?<![\w/])/(?:dashboard|admin)/[^\s<>)\]\"',]+)"
)

# Fenced code blocks and inline code spans — never rewritten.
_CODE_SPAN_RE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.DOTALL)

# Trailing punctuation that belongs to prose, not the URL.
_TRAILING_PUNCT = ".,;:!?"


@dataclass
class URLGuardStats:
    """Counters for one ``guard_urls`` pass (useful for tests/telemetry)."""

    detected: int = 0
    validated: int = 0
    corrected: int = 0
    removed: int = 0


@dataclass
class _GuardContext:
    base_urls: list[tuple[str, str, str]]  # (base_url, netloc_lower, base_path)
    routes: dict[str, set[str]]
    corrections: dict[str, str]
    conversation_id: str | None
    run_id: str | None
    stats: URLGuardStats = field(default_factory=URLGuardStats)

    def routes_for(self, prefix: str) -> dict[str, set[str]]:
        """Manifest for a root segment (``dashboard`` -> OpenCTI, ``admin`` -> OpenAEV)."""
        return OPENAEV_ROUTE_MANIFEST if prefix == "admin" else self.routes

    def corrections_for(self, prefix: str) -> dict[str, str]:
        return OPENAEV_CORRECTION_RULES if prefix == "admin" else self.corrections

    def log_extra(self, **kw: object) -> dict[str, object]:
        extra: dict[str, object] = {}
        if self.conversation_id:
            extra["conversation_id"] = self.conversation_id
        if self.run_id:
            extra["run_id"] = self.run_id
        extra.update(kw)
        return extra


@dataclass
class _MatchedURL:
    base_url: str  # "" for relative URLs
    route_path: str  # path relative to the platform base, starts /<prefix>
    suffix: str  # original "?query#fragment" (preserved on rewrites)
    prefix: str = "dashboard"  # root segment: "dashboard" (OpenCTI) | "admin" (OpenAEV)

    @property
    def supports_salvage(self) -> bool:
        """Whether the platform offers an id resolver / search route.

        OpenCTI does (``/dashboard/id/<uuid>``, ``/dashboard/search/<text>``),
        so an unfixable link can be rewritten into something guaranteed valid.
        OpenAEV has neither, so an unfixable OpenAEV link must degrade to text
        rather than be pointed at a route that does not exist.
        """
        return self.prefix == "dashboard"


# ---------------------------------------------------------------------------
# JSON override loading (cached at first use)
# ---------------------------------------------------------------------------

_override_cache: tuple[dict[str, set[str]], dict[str, str]] | None = None


def _load_manifest() -> tuple[dict[str, set[str]], dict[str, str]]:
    """Return (routes, corrections) with the optional JSON override merged.

    The override file shape is ``{"routes": {"section": ["sub", ...]},
    "corrections": {"bad/path": "good/path"}}``. Routes are merged
    per-section (sets unioned, new sections added); corrections are
    merged with override entries winning. Cached after first load.
    """
    global _override_cache
    if _override_cache is not None:
        return _override_cache

    routes = {k: set(v) for k, v in OPENCTI_ROUTE_MANIFEST.items()}
    corrections = dict(CORRECTION_RULES)

    path = settings.url_guard_manifest_path
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            for section, subs in (data.get("routes") or {}).items():
                if not isinstance(section, str):
                    continue
                existing = routes.setdefault(section, set())
                if isinstance(subs, list):
                    existing.update(s for s in subs if isinstance(s, str))
            for bad, good in (data.get("corrections") or {}).items():
                if isinstance(bad, str) and isinstance(good, str):
                    corrections[bad.strip("/")] = good.strip("/")
            logger.info(
                "url_guard: merged manifest override from %s "
                "(%d sections, %d correction rules)",
                path,
                len(routes),
                len(corrections),
            )
        except Exception as exc:
            logger.warning(
                "url_guard: failed to load manifest override %s: %s — "
                "using built-in manifest",
                path,
                format_exception_message(exc),
            )

    _override_cache = (routes, corrections)
    return _override_cache


def reset_manifest_cache() -> None:
    """Drop the cached merged manifest (tests / config reloads)."""
    global _override_cache
    _override_cache = None


# ---------------------------------------------------------------------------
# Base URL collection helpers
# ---------------------------------------------------------------------------


def collect_base_urls_from_integrations(
    integrations: Iterable[Integration],
    platforms: Mapping[str, RegisteredPlatform] | None = None,
) -> list[str]:
    """Extract guarded-platform base URLs from Integration rows.

    Only providers in ``PLATFORM_URL_PROVIDERS`` are considered, and URL
    extraction reuses the hardened ``get_integration_public_url``
    (scheme/userinfo/control-character validation, secret-free).
    *platforms* (integration id -> registration reached on an internal URL,
    ``platforms_reached_internally``) puts such a platform on its registered
    URL, the one its links are shown on.
    """
    from app.services.integration_tools import get_integration_public_url

    urls: list[str] = []
    try:
        for integ in integrations:
            provider = (getattr(integ, "provider", "") or "").lower()
            if provider not in PLATFORM_URL_PROVIDERS:
                continue
            platform = platforms.get(str(integ.id)) if platforms else None
            url = get_integration_public_url(integ, platform)
            if url and url not in urls:
                urls.append(url)
    except Exception as exc:
        logger.warning("url_guard: base URL collection failed: %s", format_exception_message(exc))
    return urls


@dataclass(frozen=True)
class PlatformLinkBases:
    """What an answer's platform links are checked and shown against.

    ``urls`` are the guard's base URLs (:func:`guard_urls`); ``platforms`` the
    bound platforms XTM One reaches on an internal URL, whose links are moved
    onto their registered URL before anything is shown.
    """

    urls: list[str] = field(default_factory=list)
    platforms: tuple[RegisteredPlatform, ...] = ()


# The platforms whose integrations a registration creates. A link on one's
# internal URL is shown on its registered URL; only the first two have routes
# the guard validates.
_REGISTERED_PLATFORM_PROVIDERS: tuple[str, ...] = (*PLATFORM_URL_PROVIDERS, "opencrq")


async def collect_platform_link_bases(db: AsyncSession, integration_bindings: dict | None) -> PlatformLinkBases:
    """Load bound integrations: their guarded-platform base URLs, and the
    platforms among them reached on an internal URL.

    ``integration_bindings`` is the standard ``{provider: [ids] | id}``
    agent/GA bindings dict. Only the registered platforms' bindings are
    loaded. Keys are normalized to their base provider first: a flattened
    dict (``flatten_bindings``) names extra instances of the same provider
    ``opencti#2``, ``opencti#3``, … and every one of those instances'
    /dashboard links must be guardable, not just the first. Empty on any
    failure (the guard still validates relative ``/dashboard/`` paths
    without base URLs).
    """
    if not integration_bindings:
        return PlatformLinkBases()
    raw_ids: list[str] = []
    for provider, integ_ids in integration_bindings.items():
        provider_base = (provider or "").split("#", 1)[0].lower()
        if provider_base not in _REGISTERED_PLATFORM_PROVIDERS or not integ_ids:
            continue
        items = integ_ids if isinstance(integ_ids, list) else [integ_ids]
        raw_ids.extend(str(i) for i in items)
    if not raw_ids:
        return PlatformLinkBases()
    try:
        import uuid as _uuid

        from sqlalchemy import select

        from app.core.platform_identity import platforms_reached_internally
        from app.models.integration import Integration

        uuids = []
        for i in raw_ids:
            try:
                uuids.append(_uuid.UUID(i))
            except (ValueError, TypeError):
                continue
        if not uuids:
            return PlatformLinkBases()
        # One savepoint around the whole best-effort lookup: a failure is
        # rolled back to it, so the caller's transaction stays usable.
        async with db.begin_nested():
            result = await db.execute(select(Integration).where(Integration.id.in_(uuids)))
            integrations = list(result.scalars().all())
            platforms = await platforms_reached_internally(
                db, [i.id for i in integrations if getattr(i, "auth_type", None) == "platform_managed"]
            )
        return PlatformLinkBases(
            urls=collect_base_urls_from_integrations(integrations, platforms),
            platforms=tuple(platforms.values()),
        )
    except Exception as exc:
        logger.warning(
            "url_guard: failed to load integrations for base URLs: %s", format_exception_message(exc)
        )
        return PlatformLinkBases()


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------


def _match_platform_url(url: str, ctx: _GuardContext) -> _MatchedURL | None:
    """Return match info when ``url`` is a guardable platform URL.

    Relative URLs match when the path starts with ``/dashboard/``.
    Absolute URLs match when the origin equals a known platform base
    (host compared case-insensitively, base path honoured) AND the
    remaining path is a ``/dashboard`` route. Anything else returns
    ``None`` — it is never touched.
    """
    if url.startswith("/"):
        # Protocol-relative URLs (``//host/...``) are not platform paths.
        if url.startswith("//"):
            return None
        try:
            rel = urlparse(url)
        except ValueError:
            return None
        rel_prefix = _route_prefix(rel.path)
        if rel_prefix is None:
            return None
        # Carry ``?query#fragment`` separately so validation sees a clean
        # path and corrections preserve the suffix (same as absolute URLs).
        rel_suffix = (f"?{rel.query}" if rel.query else "") + (
            f"#{rel.fragment}" if rel.fragment else ""
        )
        return _MatchedURL(
            base_url="",
            route_path=rel.path,
            suffix=rel_suffix,
            prefix=rel_prefix,
        )
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    netloc = (parsed.netloc or "").lower()
    suffix = (f"?{parsed.query}" if parsed.query else "") + (
        f"#{parsed.fragment}" if parsed.fragment else ""
    )
    for base_url, base_netloc, base_path in ctx.base_urls:
        if netloc != base_netloc:
            continue
        path = parsed.path or "/"
        if base_path:
            if path == base_path:
                route_path = "/"
            elif path.startswith(base_path + "/"):
                route_path = path[len(base_path) :]
            else:
                continue
        else:
            route_path = path
        matched_prefix = _route_prefix(route_path)
        if matched_prefix is not None:
            return _MatchedURL(
                base_url=base_url,
                route_path=route_path,
                suffix=suffix,
                prefix=matched_prefix,
            )
        # Platform origin but not a guarded route under THIS base —
        # keep trying: another configured base on the same host may carry
        # a path prefix that does match (e.g. ``https://host`` and
        # ``https://host/opencti``). If none does, the URL is left
        # untouched.
        continue
    return None


def _route_prefix(path: str) -> str | None:
    """Return the guarded root segment of *path*, or ``None``.

    ``/dashboard/...`` is OpenCTI, ``/admin/...`` is OpenAEV. The bare section
    root (``/dashboard``, ``/admin``) also matches so a platform home link is
    recognised rather than left unguarded.
    """
    for prefix in _PLATFORM_PREFIXES:
        if path.startswith(f"/{prefix}/") or path in (f"/{prefix}", f"/{prefix}/"):
            return prefix
    return None


def _is_valid_route(route_path: str, ctx: _GuardContext, prefix: str) -> bool:
    """Validate the first two ``/<prefix>/<s1>/<s2>`` segments."""
    routes = ctx.routes_for(prefix)
    segments = [s for s in route_path.split("/") if s]
    # segments[0] == prefix guaranteed by _match_platform_url
    if len(segments) == 1:
        return True  # bare platform home
    s1 = segments[1]
    if s1 not in routes:
        return False
    if s1 == "id":
        # Universal resolver: requires a non-empty id payload. We accept
        # any id shape (false negatives are safer than breaking a
        # working resolver link for a non-UUID internal id).
        return len(segments) >= 3
    if prefix == "admin" and s1 in OPENAEV_LIST_ONLY_SECTIONS:
        # A list screen with no detail route: only the bare section is valid.
        # Without this, the empty sub-set below would accept any trailing id
        # and happily leave a link that 404s.
        return len(segments) == 2
    known_subs = routes[s1]
    if len(segments) == 2 or not known_subs:
        return True
    return segments[2] in known_subs


def _try_correct_route(route_path: str, ctx: _GuardContext, prefix: str) -> str | None:
    """Return a corrected route path, or ``None`` when not confidently fixable."""
    routes = ctx.routes_for(prefix)
    corrections = ctx.corrections_for(prefix)
    segments = [s for s in route_path.split("/") if s]
    if len(segments) < 2:
        return None
    inner = "/".join(segments[1:])  # path after "<prefix>/"

    # 1. Explicit data-driven rules (whole leading segments only).
    for bad, good in corrections.items():
        if inner == bad or inner.startswith(bad + "/"):
            remainder = inner[len(bad) :]
            # A rename landing on a LIST-ONLY section must DROP the trailing
            # id: carrying it over just moves the 404 to a new path (e.g.
            # /admin/payloads/<id> -> /admin/threat-arsenal/<id>, where the
            # Threat Arsenal has no per-action detail route).
            if prefix == "admin" and good.split("/")[0] in OPENAEV_LIST_ONLY_SECTIONS:
                remainder = ""
            candidate = f"/{prefix}/" + good + remainder
            if _is_valid_route(candidate, ctx, prefix):
                return candidate

    # 1b. A list-only section carrying extra segments (a hallucinated detail
    #     link such as /admin/threat-arsenal/<id>): truncate to the section
    #     root rather than demote, so the user still lands on the right screen.
    if (
        prefix == "admin"
        and len(segments) >= 3
        and segments[1] in OPENAEV_LIST_ONLY_SECTIONS
    ):
        return f"/{prefix}/{segments[1]}"

    if len(segments) >= 3:
        s1, s2 = segments[1], segments[2]

        # 2. Singular -> plural second segment (e.g. analyses/report).
        if s1 in routes and routes[s1] and s2 not in routes[s1]:
            plural = s2 + "s"
            if plural in routes[s1]:
                return "/" + "/".join([prefix, s1, plural, *segments[3:]])

        # 3. Wrong section, valid sub-section that exists in exactly one
        #    section (e.g. <anything>/indicators -> observations/indicators).
        owners = [sec for sec, subs in routes.items() if subs and s2 in subs]
        if len(owners) == 1 and owners[0] != s1:
            return "/" + "/".join([prefix, owners[0], s2, *segments[3:]])

    return None


def _extract_uuid(route_path: str) -> str | None:
    m = _UUID_RE.search(route_path)
    return m.group(0).lower() if m else None


def _meaningful_link_text(text: str) -> bool:
    """True when markdown link text is usable as a search query."""
    stripped = text.strip()
    if not stripped or len(stripped) < 3:
        return False
    return not stripped.lower().startswith(
        ("http://", "https://", "/dashboard", "/admin")
    )


def _process_platform_url(
    url: str, matched: _MatchedURL, ctx: _GuardContext
) -> str | None:
    """Run validate -> correct -> UUID-resolver fallback for one URL.

    Returns the URL to use (possibly unchanged), or ``None`` when the
    URL could not be salvaged (caller degrades to search link / text).
    """
    ctx.stats.detected += 1
    logger.debug(
        "url.detected", extra=ctx.log_extra(url=url, event="url.detected")
    )

    if _is_valid_route(matched.route_path, ctx, matched.prefix):
        ctx.stats.validated += 1
        logger.debug(
            "url.validated", extra=ctx.log_extra(url=url, event="url.validated")
        )
        return url

    corrected = _try_correct_route(matched.route_path, ctx, matched.prefix)
    if corrected is not None:
        new_url = matched.base_url + corrected + matched.suffix
        ctx.stats.corrected += 1
        logger.info(
            "url.corrected",
            extra=ctx.log_extra(
                original_url=url,
                corrected_url=new_url,
                event="url.corrected",
                strategy="route_rewrite",
            ),
        )
        return new_url

    # Universal fallback: any UUID-bearing deep link can be rewritten to
    # OpenCTI's ID resolver route, valid by construction for any type.
    # OpenAEV has no equivalent resolver, so an unfixable OpenAEV link is
    # demoted instead of being pointed at a route that does not exist.
    entity_uuid = _extract_uuid(matched.route_path) if matched.supports_salvage else None
    if entity_uuid:
        new_url = f"{matched.base_url}/dashboard/id/{entity_uuid}{matched.suffix}"
        ctx.stats.corrected += 1
        logger.info(
            "url.corrected",
            extra=ctx.log_extra(
                original_url=url,
                corrected_url=new_url,
                event="url.corrected",
                strategy="id_resolver",
            ),
        )
        return new_url

    ctx.stats.removed += 1
    logger.info("url.removed", extra=ctx.log_extra(url=url, event="url.removed"))
    return None


def _split_trailing_punct(url: str) -> tuple[str, str]:
    trailing = ""
    while url:
        ch = url[-1]
        if ch in _TRAILING_PUNCT:
            trailing = ch + trailing
            url = url[:-1]
        elif ch == ")" and url.count("(") < url.count(")"):
            # An unbalanced closing paren belongs to the surrounding
            # prose — "(see https://host/dashboard/...)" — not the URL,
            # so it must survive a URL rewrite instead of being eaten.
            trailing = ch + trailing
            url = url[:-1]
        else:
            break
    return url, trailing


def _replace_token(match: re.Match[str], ctx: _GuardContext) -> str:
    full = match.group(0)

    # ── Markdown link ──
    if match.group("target") is not None:
        text = match.group("text")
        target = match.group("target").strip("<>")
        matched = _match_platform_url(target, ctx)
        if matched is None:
            return full  # external or unknown — never touched
        new_url = _process_platform_url(target, matched, ctx)
        if new_url is not None:
            return full if new_url == target else f"[{text}]({new_url})"
        # FR5 fallback: search link from meaningful text, else plain text.
        # Only OpenCTI has a /search/<text> route to fall back on.
        if matched.supports_salvage and _meaningful_link_text(text):
            search_url = (
                f"{matched.base_url}/dashboard/search/"
                f"{quote(text.strip(), safe='')}"
            )
            return f"[{text}]({search_url})"
        return text or ""

    # ── Raw absolute URL ──
    if match.group("raw") is not None:
        raw = match.group("raw")
        wrapped = raw.startswith("<") and raw.endswith(">")
        url, trailing = _split_trailing_punct(raw.strip("<>"))
        matched = _match_platform_url(url, ctx)
        if matched is None:
            return full
        new_url = _process_platform_url(url, matched, ctx)
        if new_url is not None:
            if new_url == url:
                return full
            return (f"<{new_url}>" if wrapped else new_url) + trailing
        # Degrade to inline code so a broken URL never renders as an
        # active hyperlink while the text stays readable (FR5/FR6).
        return f"`{url}`{trailing}"

    # ── Raw relative /dashboard path ──
    url, trailing = _split_trailing_punct(match.group("rel"))
    matched = _match_platform_url(url, ctx)
    if matched is None:
        return full
    new_url = _process_platform_url(url, matched, ctx)
    if new_url is not None:
        return full if new_url == url else new_url + trailing
    return f"`{url}`{trailing}"


def _guard_segment(text: str, ctx: _GuardContext) -> str:
    return _URL_TOKEN_RE.sub(lambda m: _replace_token(m, ctx), text)


def guard_urls(
    text: str,
    base_urls: list[str] | None,
    *,
    conversation_id: str | None = None,
    run_id: str | None = None,
    stats_out: URLGuardStats | None = None,
) -> str:
    """Validate / auto-correct platform URLs in a final assistant response.

    Pipeline per detected platform URL: validate route -> data-driven
    correction -> UUID resolver fallback -> search-link / plain-text
    degradation. External URLs, code blocks, and inline code are never
    modified. Fail-open: any internal error returns ``text`` unchanged.

    Args:
        text: final assistant response (markdown).
        base_urls: known platform base URLs (from the agent's bound
            OpenCTI / OpenAEV integrations). May be empty — relative
            ``/dashboard/...`` paths are still validated.
        conversation_id / run_id: correlation ids for telemetry.
        stats_out: optional mutable stats receiver (tests/telemetry).
    """
    if not settings.url_guard_enabled:
        return text
    if not text or not any(f"/{p}" in text for p in _PLATFORM_PREFIXES):
        return text  # fast bail-out — nothing guardable present (FR7)

    try:
        routes, corrections = _load_manifest()
        parsed_bases: list[tuple[str, str, str]] = []
        for b in base_urls or []:
            if not b:
                continue
            try:
                p = urlparse(b)
            except ValueError:
                continue
            if p.scheme not in ("http", "https") or not p.netloc:
                continue
            parsed_bases.append(
                (b.rstrip("/"), p.netloc.lower(), (p.path or "").rstrip("/"))
            )
        ctx = _GuardContext(
            base_urls=parsed_bases,
            routes=routes,
            corrections=corrections,
            conversation_id=conversation_id,
            run_id=run_id,
        )

        # Skip code blocks / inline code: process only non-code slices.
        out: list[str] = []
        last = 0
        for m in _CODE_SPAN_RE.finditer(text):
            out.append(_guard_segment(text[last : m.start()], ctx))
            out.append(m.group(0))
            last = m.end()
        out.append(_guard_segment(text[last:], ctx))

        if stats_out is not None:
            stats_out.detected = ctx.stats.detected
            stats_out.validated = ctx.stats.validated
            stats_out.corrected = ctx.stats.corrected
            stats_out.removed = ctx.stats.removed
        return "".join(out)
    except Exception as exc:
        logger.warning(
            "url_guard: post-processing failed — returning original text: %s",
            format_exception_message(exc),
            exc_info=True,
        )
        return text


def show_platform_links(text: str, platforms: Iterable[RegisteredPlatform]) -> str:
    """*text* with every link on the internal URL of one of *platforms* moved onto its
    registered URL. Code blocks and inline code are left as written, like
    the guard leaves them: a configuration snippet may name the internal URL on
    purpose."""
    from app.core.platform_identity import show_on_public_urls

    reached = tuple(platforms)
    if not text or not reached:
        return text
    out: list[str] = []
    last = 0
    for m in _CODE_SPAN_RE.finditer(text):
        out.append(show_on_public_urls(text[last : m.start()], reached))
        out.append(m.group(0))
        last = m.end()
    out.append(show_on_public_urls(text[last:], reached))
    return "".join(out)


async def finalize_platform_links(
    text: str,
    resolve_bases: Callable[[], Awaitable[PlatformLinkBases | list[str]]],
    *,
    conversation_id: str | None = None,
    run_id: str | None = None,
    guard: bool = True,
) -> str:
    """The platform links of an answer as a person is shown them.

    Links on the internal URL of a bound platform are moved onto its
    registered URL, then :func:`guard_urls` validates the ``/dashboard``
    links. Both are the answer's URL post-processing, off with
    ``URL_GUARD_ENABLED=false``. *resolve_bases* costs a query, so it only
    runs for a text that carries a link at all. ``guard=False`` only moves
    the links, for a text the guard never rewrote (a structured JSON result,
    a failed run's summary).
    """
    if not text or not settings.url_guard_enabled:
        return text
    guard = guard and "/dashboard" in text
    if not guard and "://" not in text:
        return text
    bases = await resolve_bases()
    if not isinstance(bases, PlatformLinkBases):
        bases = PlatformLinkBases(urls=list(bases or []))
    try:
        text = show_platform_links(text, bases.platforms)
    except Exception as exc:
        logger.warning(
            "url_guard: could not show the platform links on their registered URLs: %s",
            format_exception_message(exc),
            exc_info=True,
        )
    if guard:
        text = guard_urls(text, bases.urls, conversation_id=conversation_id, run_id=run_id)
    return text
