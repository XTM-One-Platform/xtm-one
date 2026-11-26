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

SOURCE_KINDS: tuple[str, ...] = ("listener", "pass", "pir_playbook")
PATH_STATUSES: tuple[str, ...] = ("connected", "error", "not_set_up", "off", "not_connected", "agents_api")


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


def path_status(sources: list[dict[str, Any]]) -> str:
    """``agents_api`` with no source; ``error`` when a source is in error; ``connected`` when one is bound;
    ``not_set_up`` when a source of a connected provider is not set up; ``off`` when one is off; else
    ``not_connected``. One status per path, the one its sources say (UX round 1 on #2442, B1)."""
    if not sources:
        return "agents_api"
    if any(s.get("error_count") for s in sources):
        return "error"
    if any(s.get("bound") for s in sources):
        return "connected"
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


def is_excluded(excluded: dict[str, set[tuple[str, ...]]], path: str, provider: str, kind: str, name: str = "") -> bool:
    """Whether ``provider:kind`` (``provider:kind:name`` for a named source, #2436) is excluded from *path*."""
    return _key_tuple(source_key(provider, kind, name)) in excluded.get(path, set())


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
    provisioned = await provisioned_pairs(db, connected, errors=bound.error)
    bound.update(provisioned)
    # A connected provider whose provisioned source feeds nothing and is in no error: Not set up, never Not connected.
    bound.not_set_up = {
        pair for pair in PROVISIONED_PAIRS if connected.get(pair[0]) and pair not in bound and pair not in bound.error
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
    db: AsyncSession, connected: dict[str, set[str]], *, errors: dict[tuple[str, str], int] | None = None
) -> set[tuple[str, str]]:
    """The provisioned sources on the caller's connected integrations: platform sources, then the
    assignments that still hold a provisioned trigger until the migration moves them (#2434).

    *errors*, when given, receives per source the number of its integrations whose platform source
    reads ``error`` (``ctem_platform_sources.status_of``: enabled and not provisioned, or a refused enable)."""
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
    for provider, kind, integration_id, state, enabled in rows:
        if str(integration_id) not in connected.get(provider, set()):
            continue
        if enabled and is_connected(state):
            out.add((provider, kind))
        elif errors is not None and status_of(SimpleNamespace(enabled=enabled, state=state))[0] == "error":
            errors[(provider, kind)] = errors.get((provider, kind), 0) + 1
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
        if pair and str((state or {}).get("integration_id") or "") in connected.get(provider, set()):
            out.add(pair)
    return out
