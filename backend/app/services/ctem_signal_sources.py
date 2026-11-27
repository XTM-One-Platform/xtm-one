# SPDX-License-Identifier: Apache-2.0

"""Where each CTEM trigger path gets its signals, and whether that source is bound (#1189).

The signal sources dialog of the command center (``dev-docs/ctem-signals.md``
section 2bis) lists, per trigger path, the sources that can fire it and whether
each is bound for the caller. Three kinds of source:

- ``listener``: an integration's webhook delivery a row of
  ``ctem_signal_listeners.LISTENERS`` maps to the path. Bound when an
  integration of that provider is connected and visible to the caller.
- ``pass``: a scheduled XTM One pass reading a provider (the attack-surface
  pass of CTEM-26, the evidence health pass of CTEM-28). Bound when the pass
  runs on a connected integration of the provider; ``off`` when the provider is
  connected but the pass is off or names none of its integrations (#1344: the
  evidence health pass runs by default on the single connected OpenCRQ
  integration, ``ctem_evidence_health.resolve_pass``).
- ``pir_playbook``: the OpenCTI PIR playbook (CTEM-05) delivering flagged
  entities through the OpenCTI listener.

A source XTM One provisions in its platform (:data:`PROVISIONED_PAIRS`: the
OpenCTI and OpenAEV listeners, the PIR playbook) is bound only when its objects
exist and the last check passed (#2434): an enabled platform signal source
(``services/ctem_platform_sources.py``) or, until the migration moves it, an
enabled assignment holding a provisioned trigger on that integration. A
connected integration alone never reads connected.

A path with no source at all is recorded by agents and the API only: it never
reads connected. No model call; read-only.

An administrator can take one connected source off one path without turning it
off for the others (#1445): ``signal_sources.excluded`` of the organization
profile names, per path, the excluded ``provider:kind`` keys. An excluded source
is not bound on that path (its status and the connected count follow) and the
recording points (:func:`excluded_sources`) record nothing from it there.

A path may hold several sources of one provider and kind (#2436): such a source
carries a ``name`` and its key is ``provider:kind:name``. A source added after
#2436 is off by default (``default_on`` false): it records nothing until an
administrator lists its key in ``signal_sources.included`` for that path, and
:func:`parse_excluded` folds every default-off source not included into the
excluded map, so each recording point keeps reading one map.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

#: The providers a scheduled pass reads, by the profile field that switches it on.
ATTACK_SURFACE_PROVIDERS: tuple[str, ...] = ("shodan", "gti")
EVIDENCE_HEALTH_PROVIDERS: tuple[str, ...] = ("opencrq",)

#: ``webhook``: a generic signed webhook source an administrator creates on one path
#: (``ctem_webhook_sources``, #2506); never in :data:`PATH_SOURCES`, read from its table.
#: ``cases_reader``: the scheduled cases recompute's reader of a provider's open issues (#2583 note of
#: 27 Nov 2026), its own source so the row lists it and its switch stops it.
SOURCE_KINDS: tuple[str, ...] = ("listener", "pass", "pir_playbook", "webhook", "cases_reader")
#: A path's status (#2436, Sam 26 Nov 2026): read from its sources' on/off state, never from the
#: integration connection alone: ``enabled`` (every source used on the path is on), ``partly_enabled``
#: (some are), then ``error``, ``not_set_up``, ``off``, ``not_connected`` and ``agents_api``.
PATH_STATUSES: tuple[str, ...] = (
    "enabled",
    "partly_enabled",
    "error",
    "not_set_up",
    "off",
    "not_connected",
    "agents_api",
)
#: The statuses of a path that records from at least one source.
LIVE_PATH_STATUSES: frozenset[str] = frozenset({"enabled", "partly_enabled"})


class BoundSources(set[tuple[str, str]]):
    """The ``(provider, kind)`` pairs bound for the caller, plus ``off``: the passes whose provider is connected but which do not run."""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.off: set[tuple[str, str]] = set()
        #: Per trigger path, the ``(provider, kind)`` an administrator excluded from it (#1445).
        self.excluded: dict[str, set[tuple[str, ...]]] = {}
        #: The sources XTM One provisions whose provider is connected but which feed nothing yet (#2434).
        self.not_set_up: set[tuple[str, str]] = set()
        #: Per provisioned source, the integrations on which it is in error (#2434, UX round 1 on #2442).
        self.error: dict[tuple[str, str], int] = {}


def source_key(provider: str, kind: str, name: str = "") -> str:
    """The key of one source in ``signal_sources.excluded``: ``provider:kind``, ``provider:kind:name`` when named."""
    return f"{provider}:{kind}:{name}" if name else f"{provider}:{kind}"


def _key_tuple(key: str) -> tuple[str, ...]:
    return tuple(key.split(":", 2))


@dataclass(frozen=True)
class Source:
    """One way a trigger path is fired; ``event`` is the English line the UI translates.

    ``name`` tells apart two sources of one provider and kind on one path (#2436);
    ``default_on`` false: the source records nothing until an administrator includes it.
    """

    provider: str
    kind: str
    event: str
    name: str = ""
    default_on: bool = True

    @property
    def key(self) -> str:
        return source_key(self.provider, self.kind, self.name)


#: One plain sentence per path: what fires it, as the dialog says it.
PATH_DESCRIPTIONS: dict[str, str] = {
    "new_vulnerability": "A new or updated vulnerability that may affect what you run",
    "relevant_threat": "A threat actor, campaign or malware relevant to your sector or stack",
    "code_flaw": "A flaw in your code or dependencies, before it reaches production",
    "proven_exploitable": "A simulation proved an attack path to a critical asset",
    "new_exposure": "Something you own answers on the internet when it should not",
    "stale_asset": "An asset that no longer resolves or has not been seen for days",
    "surface_drift": "A host or port on your attack surface that nobody declared",
    "mitigation_regression": "A security control that used to block an attack no longer does",
    "control_decay": "Evidence that a control works is older than its re-test delay",
    "credential_leak": "A secret or an account of yours found in a leak",
    "third_party_event": "A report or incident naming a supplier or sub-processor you track",
    "blind_spot": "An asset that supports a risk scenario with no evidence at all",
}

#: The fleet capabilities (``fleet_capability_slots.CAPABILITIES``) whose tools fire a
#: path, so a fleet without them says why the path is silent (#1434).
PATH_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "code_flaw": ("application_security", "source_code"),
    "credential_leak": ("application_security",),
}

_ATTACK_SURFACE = tuple((p, "pass") for p in ATTACK_SURFACE_PROVIDERS)

#: The sources of each path. Keep in step with ``ctem_signal_listeners.LISTENERS``
#: (``test_ctem_signal_sources.py`` fails on a listener with no source row).
PATH_SOURCES: dict[str, tuple[Source, ...]] = {
    "new_vulnerability": (
        Source("opencti", "listener", "A vulnerability is created or updated, or linked to one of your assets"),
        Source("opencti", "pir_playbook", "The PIR playbook flags a vulnerability in one of your PIRs"),
        Source("opencti", "cases_reader", "The cases recompute reads a vulnerability on your inventory software"),
        Source("github", "listener", "A security advisory is published on one of your repositories"),
    ),
    "relevant_threat": (
        Source("opencti", "listener", "An intrusion set, threat actor, campaign or malware is created or updated"),
        Source("opencti", "pir_playbook", "The PIR playbook flags a threat in one of your PIRs"),
        Source(
            "opencti",
            "listener",
            "An indicator is sighted in your data",
            name="indicator_sighting",
            default_on=False,
        ),
    ),
    "code_flaw": (
        Source("aikido", "listener", "A new issue, an SLA breach, a failed CI gate or an image scan with issues"),
        Source("github", "listener", "A Dependabot alert is opened or reopened on one of your repositories"),
        Source(
            "github",
            "listener",
            "A code scanning alert is opened or reopened on one of your repositories",
            name="code_scanning",
            default_on=False,
        ),
    ),
    "proven_exploitable": (
        Source("openaev", "listener", "A finished simulation reached a high or very high criticality asset"),
        Source("openaev", "cases_reader", "The cases recompute reads a CVE finding on one of your assets"),
    ),
    "new_exposure": (
        Source("openaev", "listener", "A reachability assertion failed: a target that should not answer answers"),
        Source(
            "openaev",
            "listener",
            "A finding is created on one of your assets",
            name="finding_created",
            default_on=False,
        ),
        *(
            Source(p, k, "The attack-surface pass sees a declared asset answer on a new port")
            for p, k in _ATTACK_SURFACE
        ),
    ),
    "stale_asset": (
        Source("openaev", "listener", "An asset turned stale in OpenAEV"),
        *(
            Source(p, k, "The attack-surface pass sees a name resolve to nothing or a host unseen")
            for p, k in _ATTACK_SURFACE
        ),
    ),
    "surface_drift": (
        *(Source(p, k, "The attack-surface pass sees an undeclared host or a new port") for p, k in _ATTACK_SURFACE),
        Source(
            "openaev",
            "listener",
            "An OpenAEV scan finds a new asset",
            name="asset_created",
            default_on=False,
        ),
    ),
    "mitigation_regression": (Source("openaev", "listener", "A scenario's score degraded"),),
    "control_decay": tuple(
        Source(p, "pass", "The evidence health pass finds evidence past its re-test delay")
        for p in EVIDENCE_HEALTH_PROVIDERS
    ),
    "credential_leak": (
        Source("aikido", "listener", "A leaked secret issue"),
        Source("opencti", "listener", "An account or an email address of your domains is written by a leak source"),
        Source(
            "github",
            "listener",
            "A secret scanning alert is opened or reopened on one of your repositories",
            name="secret_scanning",
            default_on=False,
        ),
    ),
    "third_party_event": (
        Source("opencti", "listener", "A report or incident names one of your tracked organizations"),
    ),
    "blind_spot": tuple(
        Source(p, "pass", "The evidence health pass finds a supporting asset with no evidence")
        for p in EVIDENCE_HEALTH_PROVIDERS
    ),
}


#: The paths a PIR playbook feeds (#2436), with the OpenCTI entity types its playbook delivers.
PIR_PATHS: dict[str, tuple[str, ...]] = {
    "new_vulnerability": ("Vulnerability",),
    "relevant_threat": ("Intrusion-Set", "Threat-Actor-Group", "Threat-Actor-Individual", "Campaign", "Malware"),
}

#: How each path's playbook reaches its entities (#2583 point 3): a threat is flagged in a PIR
#: itself; a vulnerability never is, so it arrives when a new relationship links it to a flagged
#: entity (``provision.pir_relation`` ``linked``, OpenCTI's ``create_rel``).
PIR_PATH_RELATIONS: dict[str, str] = {"new_vulnerability": "linked", "relevant_threat": "flagged"}

#: The OpenCTI ``pir_type`` values a path takes by default (#2436). OpenCTI offers threat PIRs today;
#: a vulnerability PIR type is matched by name as soon as OpenCTI offers one.
PIR_PATH_TYPES: dict[str, tuple[str, ...]] = {
    "new_vulnerability": ("VULNERABILITY",),
    "relevant_threat": ("THREAT_LANDSCAPE", "THREAT_ORIGIN", "THREAT_CUSTOM"),
}


def _of_path_type(path: str, pir_type: str) -> bool:
    wanted = PIR_PATH_TYPES.get(path, ())
    value = (pir_type or "").upper()
    return value in wanted or (path == "new_vulnerability" and "VULNERAB" in value)


def pir_selection(path: str, enabled: dict[str, str], chosen: dict[str, list[str]] | None) -> dict[str, Any]:
    """The PIRs the PIR playbook of *path* listens to (#2436). Pure.

    *enabled* maps each enabled PIR id to its OpenCTI ``pir_type``; *chosen* is
    ``signal_sources.pirs``. A path the administrator set takes its list (a PIR
    no longer enabled drops out; an empty list provisions nothing). Otherwise the
    PIRs of the path's type; when none is of that type, every enabled PIR, as
    before #2436, so no path goes silent on the upgrade. ``available`` lists the
    PIRs of the path's type, empty when OpenCTI offers none (the UI says so).
    """
    available = sorted(pid for pid, kind in enabled.items() if _of_path_type(path, kind))
    if chosen is not None and path in chosen:
        return {
            "pir_ids": [pid for pid in chosen[path] if pid in enabled],
            "available": available,
            "default": False,
        }
    return {"pir_ids": available or sorted(enabled), "available": available, "default": True}


def path_status(sources: list[dict[str, Any]]) -> str:
    """``agents_api`` with no source; ``error`` when a source is in error; ``enabled`` when every source
    used on the path (not taken off it) of a connected provider is on, ``partly_enabled`` when some are;
    ``not_set_up`` when a source of a connected provider is not set up; ``off`` when one is off; else
    ``not_connected``. One status per path, the one its sources say (UX round 1 on #2442, B1); never
    the integration connection alone (#2436, Sam 26 Nov 2026)."""
    if not sources:
        return "agents_api"
    if any(s.get("error_count") for s in sources):
        return "error"
    if any(s.get("bound") for s in sources):
        # The sources used here of a connected provider: a source taken off the path is a choice, and a
        # source of a provider with no integration here is a possible one, neither makes the path partial.
        used = [s for s in sources if not s.get("excluded") and (s.get("bound") or s.get("off") or s.get("not_set_up"))]
        return "enabled" if all(s.get("bound") for s in used) else "partly_enabled"
    if any(s.get("not_set_up") for s in sources):
        return "not_set_up"
    return "off" if any(s.get("off") for s in sources) else "not_connected"


def path_sources(path: str, bound: set[tuple[str, str]] | None) -> list[dict[str, Any]]:
    """The sources of *path* with their ``bound`` flag (``(provider, kind)`` in *bound*), ``off`` flag (in ``bound.off``)
    and ``excluded`` flag (in ``bound.excluded[path]``: an excluded source is never bound on the path, #1445)."""
    known = bound or set()
    off = getattr(bound, "off", None) or set()
    excluded = (getattr(bound, "excluded", None) or {}).get(path, set())
    not_set_up = getattr(bound, "not_set_up", None) or set()
    errors = getattr(bound, "error", None) or {}
    out: list[dict[str, Any]] = []
    for s in PATH_SOURCES.get(path, ()):
        pair = (s.provider, s.kind)
        own = _key_tuple(s.key)
        out.append(
            {
                "provider": s.provider,
                "kind": s.kind,
                # #2436: the key the profile names it by, its stable name and whether it is on by default.
                "key": s.key,
                "name": s.name,
                "default_on": s.default_on,
                "event": s.event,
                "bound": pair in known and own not in excluded,
                "off": pair not in known and pair in off,
                # A source XTM One provisions, its provider connected, switched on nowhere yet (#2434).
                "not_set_up": pair not in known and pair in not_set_up,
                # The integrations on which this provisioned source is in error (#2434).
                "error_count": int(errors.get(pair, 0)),
                "excluded": own in excluded,
                # Bound before the exclusion: an excluded source of a connected provider still reads connected.
                "available": pair in known,
                # The paths this source also feeds (a pass is organization-wide: one switch feeds them all).
                "also_feeds": [
                    other
                    for other, sources in PATH_SOURCES.items()
                    if other != path and any(o.key == s.key for o in sources)
                ],
            }
        )
    return out


def parse_excluded(profile: Any) -> dict[str, set[tuple[str, ...]]]:
    """The sources that record nothing per path: ``{path: {(provider, kind) or (provider, kind, name)}}``.

    ``signal_sources.excluded`` of a parsed profile, plus every default-off source (#2436)
    its ``signal_sources.included`` does not name for that path.
    """
    settings = getattr(profile, "signal_sources", None)
    raw = getattr(settings, "excluded", None) or {}
    included = getattr(settings, "included", None) or {}
    out: dict[str, set[tuple[str, ...]]] = {}
    for path, keys in raw.items():
        tuples = {_key_tuple(k) for k in keys if ":" in k}
        if tuples:
            out[path] = tuples
    for path, sources in PATH_SOURCES.items():
        on = set(included.get(path) or ())
        off = {_key_tuple(s.key) for s in sources if not s.default_on and s.key not in on}
        if off:
            out.setdefault(path, set()).update(off)
    return out


async def excluded_sources(db: AsyncSession, organization_id: Any) -> dict[str, set[tuple[str, ...]]]:
    """The sources excluded per path in the organization's current profile; ``{}`` on any read failure.

    Read by every system recording point (listeners, PIR playbook, the scheduled
    passes) before it records: an excluded source records nothing on that path.
    """
    from app.services import organization_context as org_context

    try:
        version = await org_context.current_version(db, organization_id)
        return parse_excluded(org_context.parse_profile(version.profile if version else {}))
    except Exception:  # a profile that cannot be read never stops a signal; a default-off source stays off
        return parse_excluded(None)


#: Seconds a read exclusion map is trusted by the recorders (#2583 point 7): a source switched off
#: stops recording within this delay, and an excluded delivery is dropped with no database session.
EXCLUSIONS_TTL_SECONDS = 30.0
_exclusions_cache: tuple[float, Any, dict[str, set[tuple[str, ...]]]] | None = None


def cached_exclusions() -> tuple[Any, dict[str, set[tuple[str, ...]]]] | None:
    """The primary organization's id and its exclusion map as last read, while fresh; else ``None``."""
    import time

    cached = _exclusions_cache
    if cached is None or time.monotonic() - cached[0] > EXCLUSIONS_TTL_SECONDS:
        return None
    return cached[1], cached[2]


def remember_exclusions(organization_id: Any, excluded: dict[str, set[tuple[str, ...]]]) -> None:
    """Keep the exclusion map just read for :data:`EXCLUSIONS_TTL_SECONDS`."""
    import time

    global _exclusions_cache
    _exclusions_cache = (time.monotonic(), organization_id, excluded)


def forget_exclusions() -> None:
    """Drop the cached exclusion map: the next delivery reads the profile again."""
    global _exclusions_cache
    _exclusions_cache = None


def is_excluded(excluded: dict[str, set[tuple[str, ...]]], path: str, provider: str, kind: str, name: str = "") -> bool:
    """Whether ``provider:kind`` (``provider:kind:name`` for a named source, #2436) is excluded from *path*."""
    return _key_tuple(source_key(provider, kind, name)) in excluded.get(path, set())


def reader_excluded(excluded: dict[str, set[tuple[str, ...]]], path: str, provider: str) -> bool:
    """Whether the cases recompute's reader of *provider* records nothing on *path*.

    Off when its own ``provider:cases_reader`` source is excluded, or when the path is off for the
    provider: every other source of that provider on the path excluded ("Not used here").
    """
    if is_excluded(excluded, path, provider, "cases_reader"):
        return True
    others = [s for s in PATH_SOURCES.get(path, ()) if s.provider == provider and s.kind != "cases_reader"]
    return bool(others) and all(is_excluded(excluded, path, s.provider, s.kind, s.name) for s in others)


def reader_pairs(connected: Any) -> set[tuple[str, str]]:
    """The ``(provider, "cases_reader")`` sources bound for the connected *providers*.

    The cases recompute reads a connected provider's open issues with nothing to set up in its
    platform, so its reader is on as soon as the provider is connected; only an exclusion on a
    path turns it off there ("Not used here"). It never reads Not set up (UX round 1 on #2592, B1).
    """
    readers = {s.provider for row in PATH_SOURCES.values() for s in row if s.kind == "cases_reader"}
    return {(provider, "cases_reader") for provider in connected if provider in readers}


def _connected_ids(rows: list[tuple[Any, str]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for integration_id, provider in rows:
        out.setdefault(provider, set()).add(str(integration_id))
    return out


async def platform_connected_ids(db: AsyncSession, providers: tuple[str, ...]) -> dict[str, set[str]]:
    """The connected integration ids of the platform per provider, whoever can see them.

    A scheduled pass is organization-wide: it runs on an administrator's
    integration for every reader, so its status is read on the platform's
    connected integrations, never on the caller's visible ones (#1344, UX round 2).
    """
    from app.models.integration import Integration

    rows = (
        await db.execute(
            select(Integration.id, Integration.provider).where(
                Integration.provider.in_(providers), Integration.status == "connected"
            )
        )
    ).all()
    return _connected_ids([(r[0], r[1]) for r in rows])


async def bound_sources(db: AsyncSession, user_id: Any) -> BoundSources:
    """The ``(provider, kind)`` pairs bound for the caller, and the passes ``off`` (module docstring).

    Listeners are read on the integrations visible to the caller; the passes on
    the platform's connected integrations (:func:`platform_connected_ids`), so
    every role reads the same status for an organization-wide pass.
    """
    from app.api.v1.helpers import integration_visible_filter
    from app.models.integration import Integration
    from app.services import ctem_evidence_health as evidence_health
    from app.services import organization_context as org_context
    from app.services.organizations import get_primary

    providers = {s.provider for sources in PATH_SOURCES.values() for s in sources}
    rows = (
        await db.execute(
            select(Integration.id, Integration.provider).where(
                Integration.provider.in_(providers),
                Integration.status == "connected",
                await integration_visible_filter(db, user_id),
            )
        )
    ).all()
    connected = _connected_ids([(r[0], r[1]) for r in rows])
    bound = BoundSources((p, "listener") for p in connected)
    bound.update(reader_pairs(connected))

    primary = await get_primary(db)
    if primary is not None:
        platform = await platform_connected_ids(db, ATTACK_SURFACE_PROVIDERS + EVIDENCE_HEALTH_PROVIDERS)
        version = await org_context.current_version(db, primary.id)
        profile = org_context.parse_profile(version.profile if version else {})
        bound.excluded = parse_excluded(profile)
        surface = profile.attack_surface
        ids = {i for i in [surface.integration, *surface.integrations] if i}
        for provider in ATTACK_SURFACE_PROVIDERS:
            if surface.enabled and ids & platform.get(provider, set()):
                bound.add((provider, "pass"))
            elif platform.get(provider):
                bound.off.add((provider, "pass"))
        # The tick's rule, on the platform's connected integrations (the pass is organization-wide).
        state, picked = evidence_health.resolve_pass(
            profile.evidence_health, await evidence_health.connected_integration_ids(db)
        )
        for provider in EVIDENCE_HEALTH_PROVIDERS:
            if state == evidence_health.PASS_ON and picked in platform.get(provider, set()):
                bound.add((provider, "pass"))
            elif state != evidence_health.PASS_NONE and platform.get(provider):
                bound.off.add((provider, "pass"))

    # A source XTM One provisions in its platform reads connected only when its objects
    # exist and the last check passed (#2434): a connected integration alone feeds nothing.
    for pair in PROVISIONED_PAIRS:
        bound.discard(pair)
    turned_off: set[tuple[str, str]] = set()
    provisioned = await provisioned_pairs(db, connected, errors=bound.error, turned_off=turned_off)
    bound.update(provisioned)
    # A source an administrator switched off reads Off, as its switch does (#2436, Sam 26 Nov 2026).
    bound.off |= {pair for pair in turned_off if pair not in bound and pair not in bound.error}
    # A connected provider whose provisioned source was never set up and is in no error: Not set up, never Not connected.
    bound.not_set_up = {
        pair
        for pair in PROVISIONED_PAIRS
        if connected.get(pair[0]) and pair not in bound and pair not in bound.error and pair not in bound.off
    }
    return bound


#: The ``(provider, kind)`` sources XTM One must provision in the source platform before they deliver.
PROVISIONED_PAIRS: tuple[tuple[str, str], ...] = (
    ("opencti", "listener"),
    ("opencti", "pir_playbook"),
    ("openaev", "listener"),
)


def provisioned_pair(provider: str, state: Any) -> tuple[str, str] | None:
    """The source a provisioning *state* feeds when it is provisioned and its last check passed, else ``None``."""
    from app.services.ctem_platform_sources import is_connected

    if not is_connected(state):
        return None
    if provider == "opencti":
        return ("opencti", "pir_playbook" if state.get("playbook_id") else "listener")
    if provider == "openaev":
        return ("openaev", "listener")
    return None


async def provisioned_pairs(
    db: AsyncSession,
    connected: dict[str, set[str]],
    *,
    errors: dict[tuple[str, str], int] | None = None,
    turned_off: set[tuple[str, str]] | None = None,
) -> set[tuple[str, str]]:
    """The provisioned sources on the caller's connected integrations: platform sources, then the
    assignments that still hold a provisioned trigger until the migration moves them (#2434).

    *errors*, when given, receives per source the number of its integrations whose platform source
    reads ``error`` (``ctem_platform_sources.status_of``: enabled and not provisioned, or a refused enable).
    *turned_off*, when given, receives the sources whose platform source exists switched off. An
    integration with a platform source row is read from that row alone: an assignment trigger left on
    it never makes a source switched off read on (#2436, Sam 26 Nov 2026)."""
    from app.models.assignment import Assignment
    from app.models.ctem_signal_source import CtemSignalSource
    from app.services.ctem_platform_sources import is_connected, status_of
    from app.services.openaev_trigger_provisioning import STATE_KEY as OPENAEV_STATE_KEY
    from app.services.opencti_trigger_provisioning import STATE_KEY as OPENCTI_STATE_KEY

    out: set[tuple[str, str]] = set()
    if not ({provider for provider, _ in PROVISIONED_PAIRS} & set(connected)):
        return out
    rows = (
        await db.execute(
            select(
                CtemSignalSource.provider,
                CtemSignalSource.kind,
                CtemSignalSource.integration_id,
                CtemSignalSource.state,
                CtemSignalSource.enabled,
            )
        )
    ).all()
    owned: set[tuple[str, str, str]] = set()
    for provider, kind, integration_id, state, enabled in rows:
        if str(integration_id) not in connected.get(provider, set()):
            continue
        owned.add((provider, kind, str(integration_id)))
        status = status_of(SimpleNamespace(enabled=enabled, state=state))[0]
        if enabled and is_connected(state):
            out.add((provider, kind))
        elif status == "error":
            if errors is not None:
                errors[(provider, kind)] = errors.get((provider, kind), 0) + 1
        elif not enabled and turned_off is not None:
            turned_off.add((provider, kind))
    configs = (
        await db.execute(
            select(Assignment.trigger_config).where(
                Assignment.trigger_type == "integration_event", Assignment.enabled.is_(True)
            )
        )
    ).scalars()
    keys = {"opencti": OPENCTI_STATE_KEY, "openaev": OPENAEV_STATE_KEY}
    for config in configs:
        if not isinstance(config, dict):
            continue
        provider = config.get("provider")
        if provider not in keys:
            continue
        state = config.get(keys[provider])
        pair = provisioned_pair(provider, state)
        integration_id = str((state or {}).get("integration_id") or "")
        if pair and integration_id in connected.get(provider, set()) and (*pair, integration_id) not in owned:
            out.add(pair)
    return out
