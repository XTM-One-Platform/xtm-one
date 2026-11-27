# SPDX-License-Identifier: Apache-2.0

"""Platform signal sources: provisioned on enable with a chosen run-as, never by an assignment (#2434).

A source a trigger path reads from a source platform that XTM One must
provision there (:data:`PROVISIONED`) is a ``ctem_signal_sources`` row
(``models/ctem_signal_source.py``). This module is its whole life:

- :func:`enable` takes the integration and the run-as, checks the run-as (an
  active user with a connection to that integration), provisions at once and
  answers Connected, or refuses with the exact reason (missing right, no
  webhook secret, integration not connected) and leaves nothing behind;
- :func:`disable` removes the provisioned objects;
- :func:`maintain` (the scheduler tick, ``trigger_provisioning``) rechecks every
  enabled source every ``RECHECK_SECONDS``, repairs an edited or deleted
  object, and marks a source whose run-as is gone or disconnected in error
  with that reason, never a silent stop;
- deliveries reach :func:`receiver_url`, the platform receiver of
  ``api/v1/webhooks.receive_signal_source_webhook``, which records the signal.

Disabling or deleting a fleet, an agent or an assignment never touches a row.
The provider modules keep the provider-specific work (``reconcile``,
``remove``, ``integration_connection``); this layer names no provider beyond
the catalog that maps a source to its module and its spec.
"""

from __future__ import annotations

import importlib
import logging
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: The name a platform source gives its objects in the source platform.
OBJECT_FLEET = "Signals"

#: The state of an enabled source whose paths are all off: no object held, rechecked at every pass.
IDLE = "idle"

#: The run-as a source needs when it has none (a row enabled before the run-as was required).
NO_RUN_AS = "no run-as user: enable the source again and pick the user it provisions as"


#: The OpenAEV events provisioned whatever path is on: they move an open case (validation, re-test).
OPENAEV_CASE_EVENTS: tuple[str, ...] = ("simulation_finished", "finding_updated", "finding_deleted")


def _listened(excluded: dict[str, set[tuple[str, ...]]] | None, path: str, provider: str, name: str) -> bool:
    """Whether the ``provider:listener`` source named *name* records on *path*; ``None``: every source is on."""
    from app.services.ctem_signal_sources import is_excluded

    return excluded is None or not is_excluded(excluded, path, provider, "listener", name)


def openaev_events(excluded: dict[str, set[tuple[str, ...]]] | None = None) -> list[str]:
    """The OpenAEV events the listener source provisions: the case events, plus each listener row's events
    while its source is on its path (#2583, Sam 27 Nov 2026). *excluded* is ``excluded_sources``."""
    from app.agents.builtin_agents import CTEM_OPENAEV_LOOP_EVENTS
    from app.services.ctem_signal_listeners import LISTENERS

    wanted = set(OPENAEV_CASE_EVENTS)
    for row in LISTENERS:
        if row.provider == "openaev" and _listened(excluded, row.trigger_path, "openaev", row.source):
            wanted.update(row.event_keys)
    return sorted(wanted & set(CTEM_OPENAEV_LOOP_EVENTS))


def _openaev_spec(excluded: dict[str, set[tuple[str, ...]]] | None = None) -> dict[str, Any]:
    return {"fleet": OBJECT_FLEET, "purpose": "OpenAEV listener", "events": openaev_events(excluded)}


#: The entity types the single OpenCTI PIR playbook (before #2436) delivers: the threats flagged in a
#: PIR. A vulnerability is never flagged itself (#2583 point 3): the per-path playbook of
#: ``new_vulnerability`` delivers the vulnerabilities linked to a flagged entity.
PIR_PLAYBOOK_ENTITY_TYPES: tuple[str, ...] = (
    "Intrusion-Set",
    "Threat-Actor-Group",
    "Threat-Actor-Individual",
    "Campaign",
    "Malware",
)


def path_label(path: str) -> str:
    """The label of a trigger path as the Sources view shows it (``ctem_signals.PATH_CATALOG``)."""
    from app.services.ctem_signals import PATH_CATALOG

    return PATH_CATALOG.get(path, (path, ""))[0]


def _opencti_pir_spec(path: str = "") -> dict[str, Any]:
    """The PIR playbook spec; for *path* (#2436), the playbook of that path alone, over its entity types."""
    from app.services import opencti_trigger_provisioning as prov
    from app.services.ctem_signal_sources import PIR_PATH_RELATIONS, PIR_PATHS

    spec = prov.read_provision_spec(
        {
            "provider": "opencti",
            "provision": {
                "fleet": OBJECT_FLEET,
                # #2583.1: <Path> is the path label the Sources view shows, never its internal key.
                "purpose": f"OpenCTI PIR playbook - {path_label(path)}" if path else "OpenCTI PIR playbook",
                "mode": prov.PIR_PLAYBOOK_MODE,
                "pir_relation": PIR_PATH_RELATIONS.get(path, prov.PIR_FLAGGED),
                "entity_types": list(PIR_PATHS[path] if path else PIR_PLAYBOOK_ENTITY_TYPES),
                "organization_context": ["pir_ids"],
                "pir_min_score": 1,
            },
        }
    )
    if spec is None:  # the block above always declares one
        raise RuntimeError("the OpenCTI PIR playbook spec is empty")
    return spec


#: The OpenCTI types the live listener source delivers: every OpenCTI row of
#: ``ctem_signal_listeners.LISTENERS`` (``has`` is the relationship type, which
#: OpenCTI's ``entity_type`` filter matches on a relationship).
LIVE_LISTENER_ENTITY_TYPES: tuple[str, ...] = (
    "Vulnerability",
    "has",
    "stix-sighting-relationship",
    "Intrusion-Set",
    "Threat-Actor-Group",
    "Threat-Actor-Individual",
    "Campaign",
    "Malware",
    "User-Account",
    "Email-Addr",
)


#: The OpenCTI entity types the live listener delivers for each source it feeds, ``(path, source name)``:
#: the trigger's filter is the union of the sources that are on (#2583 point 7, Sam 27 Nov 2026).
LIVE_LISTENER_TYPES_BY_SOURCE: dict[tuple[str, str], tuple[str, ...]] = {
    ("new_vulnerability", ""): ("Vulnerability", "has", "stix-sighting-relationship"),
    ("relevant_threat", ""): (
        "Intrusion-Set",
        "Threat-Actor-Group",
        "Threat-Actor-Individual",
        "Campaign",
        "Malware",
    ),
    ("relevant_threat", "indicator_sighting"): ("stix-sighting-relationship",),
    ("credential_leak", ""): ("User-Account", "Email-Addr"),
}


def opencti_listener_types(excluded: dict[str, set[tuple[str, ...]]] | None = None) -> list[str]:
    """The entity types of the live listener trigger: those of the sources that are on, in table order."""
    on = {
        t
        for (path, name), types in LIVE_LISTENER_TYPES_BY_SOURCE.items()
        if _listened(excluded, path, "opencti", name)
        for t in types
    }
    return [t for t in LIVE_LISTENER_ENTITY_TYPES if t in on]


def _opencti_listener_spec(excluded: dict[str, set[tuple[str, ...]]] | None = None) -> dict[str, Any]:
    from app.services import opencti_trigger_provisioning as prov

    entity_types = opencti_listener_types(excluded)
    if not entity_types:
        raise NothingToListenError(
            "every path the OpenCTI live listener feeds is set to Not used here: nothing to listen to"
        )
    spec = prov.read_provision_spec(
        {
            "provider": "opencti",
            "provision": {
                "fleet": OBJECT_FLEET,
                "purpose": "OpenCTI live listener",
                "entity_types": entity_types,
                "event_types": ["create", "update"],
            },
        }
    )
    if spec is None:  # the block above always declares one
        raise RuntimeError("the OpenCTI live listener spec is empty")
    return spec


#: Path 11 (#2583 comment point 5): the reports and incidents naming a tracked organization reach
#: the live listener source through a trigger of their own, scoped to the tracked organizations'
#: identities, so the vulnerability intake is never widened to every report.
THIRD_PARTY_PATH = "third_party_event"
THIRD_PARTY_ENTITY_TYPES: tuple[str, ...] = ("Report", "Incident")


def _opencti_third_party_spec() -> dict[str, Any]:
    from app.services import opencti_trigger_provisioning as prov

    spec = prov.read_provision_spec(
        {
            "provider": "opencti",
            "provision": {
                "fleet": OBJECT_FLEET,
                "purpose": f"OpenCTI live listener - {path_label(THIRD_PARTY_PATH)}",
                "entity_types": list(THIRD_PARTY_ENTITY_TYPES),
                "event_types": ["create", "update"],
                "organizations": prov.TRACKED_SCOPE,
            },
        }
    )
    if spec is None:  # the block above always declares one
        raise RuntimeError("the OpenCTI third-party listener spec is empty")
    return spec


@dataclass(frozen=True)
class Provisioned:
    """How one source kind of one provider is provisioned."""

    module: str
    spec: Callable[..., dict[str, Any]]
    label: str
    #: One set of objects per trigger path and PIR selection (#2436): ``spec(path)`` builds each.
    per_pir_path: bool = False
    #: What it provisions follows the sources on per path (#2583): ``spec(excluded=...)`` builds it.
    follows_paths: bool = False
    #: Paths that need objects of their own next to the main ones, ``(path, spec)`` (#2583 point 5):
    #: held under ``state["paths"][path]``, provisioned while the source is on that path.
    path_objects: tuple[tuple[str, Callable[[], dict[str, Any]]], ...] = ()


#: ``(provider, kind)`` -> how it is provisioned.
PROVISIONED: dict[tuple[str, str], Provisioned] = {
    ("openaev", "listener"): Provisioned(
        "app.services.openaev_trigger_provisioning", _openaev_spec, "OpenAEV", follows_paths=True
    ),
    ("opencti", "listener"): Provisioned(
        "app.services.opencti_trigger_provisioning",
        _opencti_listener_spec,
        "OpenCTI",
        follows_paths=True,
        path_objects=((THIRD_PARTY_PATH, _opencti_third_party_spec),),
    ),
    ("opencti", "pir_playbook"): Provisioned(
        "app.services.opencti_trigger_provisioning", _opencti_pir_spec, "OpenCTI", per_pir_path=True
    ),
}


class SourceRefusedError(ValueError):
    """The source cannot be enabled; the message is the exact reason shown to the administrator."""


class NothingToListenError(SourceRefusedError):
    """Every path the source feeds is off: its objects are removed so the platform stops delivering."""


def receiver_url(base_url: str, integration_id: str, source_id: str) -> str:
    return f"{base_url.rstrip('/')}/api/hooks/integrations/{integration_id}/signal-sources/{source_id}"


def is_connected(state: Any) -> bool:
    """Connected: the objects exist in the source platform and the last check passed."""
    return (
        isinstance(state, dict)
        and state.get("status") == "provisioned"
        and bool(
            state.get("trigger_ids") or state.get("trigger_id") or state.get("playbook_id") or state.get("playbook_ids")
        )
    )


def status_of(row: Any) -> tuple[str, str | None]:
    """``(status, reason)`` of a row: ``connected``, ``off`` or ``error`` with its reason."""
    state = row.state if isinstance(getattr(row, "state", None), dict) else {}
    if row.enabled and state.get("status") == IDLE:
        # Switched on, every path it feeds off: it holds nothing and is never an error (#2583 point 7).
        # A path with objects of its own still delivers (#2583 point 5): connected through it.
        subs = [s for s in (state.get("paths") or {}).values() if isinstance(s, dict)]
        if any(is_connected(s) for s in subs):
            return "connected", None
        return "off", state.get("error")
    if not row.enabled:
        return ("error", state.get("error")) if state.get("status") == "error" else ("off", None)
    if is_connected(state):
        return "connected", None
    return "error", state.get("error") or "not provisioned yet"


def _held(state: Any) -> bool:
    return isinstance(state, dict) and bool(
        state.get("notifier_id")
        or state.get("trigger_ids")
        or state.get("trigger_id")
        or state.get("playbook_id")
        or any(_held(sub) for sub in (state.get("paths") or {}).values())
    )


def _module(provider: str, kind: str) -> Any:
    return importlib.import_module(PROVISIONED[(provider, kind)].module)


async def _connection(db: Any, row: Any) -> tuple[Any, str, str] | str:
    """``(client, integration_id, secret)`` of *row* as its run-as, or the reason it cannot be built.

    Called by name per provider (never through the imported module) so the session
    each one reads stays visible to the write-route analysis: both only read.
    """
    if row.provider == "openaev":
        from app.services.openaev_trigger_provisioning import integration_connection as openaev_connection

        return await openaev_connection(db, str(row.integration_id), row.run_as_user_id)
    if row.provider == "opencti":
        from app.services.opencti_trigger_provisioning import integration_connection as opencti_connection

        return await opencti_connection(db, str(row.integration_id), row.run_as_user_id)
    return f"{row.provider} is not a provider XTM One provisions sources in"


async def _run_as_reason(db: Any, provider: str, user_id: Any) -> str | None:
    """Why *user_id* cannot provision a *provider* source, ``None`` when it can."""
    from app.models.user import User
    from app.services.fleet_run_as import missing_providers, missing_refusal

    if user_id is None:
        return NO_RUN_AS
    user = await db.get(User, user_id)
    if user is None or not getattr(user, "is_active", False):
        return "the run-as user is deactivated or no longer exists: pick another run-as"
    missing = await missing_providers(db, user.id, [provider])
    if missing:
        return missing_refusal(missing).replace(
            "a fleet automation runs as that user", "the source provisions as that user"
        )
    return None


async def _spec(
    db: Any, provider: str, kind: str, *, path: str = "", pir_ids: list[str] | None = None
) -> dict[str, Any]:
    """The spec of a source, its organization context derived; raises :class:`SourceRefusedError` naming what is missing.

    *path* and *pir_ids* build one PIR playbook of a per-path source (#2436): its
    PIRs are that selection, each at its Intelligence policy minimum.
    """
    entry = PROVISIONED[(provider, kind)]
    if path:
        spec = entry.spec(path)
    elif entry.follows_paths:
        from app.services.ctem_signal_sources import excluded_sources

        spec = entry.spec(excluded=await excluded_sources(db, None))
    else:
        spec = entry.spec()
    if pir_ids is not None:
        spec = {**spec, "pir_subset": sorted(pir_ids)}
    if spec.get("organization_context"):
        from app.services import opencti_trigger_provisioning as prov
        from app.services import organization_context as org_context
        from app.services.organization_pirs import overlay_profile

        row = await org_context.current_version(db)
        profile = org_context.parse_profile(row.profile) if row is not None else None
        if profile is not None and path:
            # The enabled PIRs and their minimum scores come from the Intelligence policy (#2435).
            profile = await overlay_profile(db, profile)
        spec, missing = prov.derive_from_organization_context(spec, profile)
        if missing:
            raise SourceRefusedError(
                f"organization context: {', '.join(missing)} not filled (Settings > Organization); "
                "the source is not provisioned without them"
            )
    return spec


async def pir_path_selections(db: Any) -> dict[str, list[str]]:
    """Per PIR path, the PIRs its playbook listens to (#2436): ``signal_sources.pirs`` over the enabled PIRs."""
    from sqlalchemy import select

    from app.models.organization_pir import OrganizationPir
    from app.services import organization_context as org_context
    from app.services.ctem_signal_sources import PIR_PATHS, pir_selection

    row = await org_context.current_version(db)
    profile = org_context.parse_profile(row.profile if row is not None else {})
    rows = (await db.execute(select(OrganizationPir).where(OrganizationPir.enabled.is_(True)))).scalars().all()
    # Before the Intelligence policy stores PIRs, the profile's own ids, of no known type.
    enabled = {r.pir_id: r.pir_type or "" for r in rows} if rows else dict.fromkeys(profile.pir_ids or [], "")
    chosen = profile.signal_sources.pirs
    return {path: pir_selection(path, enabled, chosen)["pir_ids"] for path in PIR_PATHS}


async def _reconcile_pir_paths(db: Any, row: Any, conn: tuple[Any, str, str], now: datetime) -> dict[str, Any]:
    """One playbook per PIR path over its selection, each PIR at its own minimum (#2436).

    The per-path states live under ``paths``; a path whose selection is empty holds
    nothing (its objects are removed). A single playbook provisioned before #2436
    is removed once every path is provisioned, so no delivery is lost.
    """
    from app.core.config import settings

    stamp = now.isoformat()
    state = dict(row.state or {})
    module = _module(row.provider, row.kind)
    client, integration_id, secret = conn
    url = receiver_url(settings.base_url, integration_id, str(row.id))
    selections = await pir_path_selections(db)
    previous = dict(state.get("paths") or {})
    paths: dict[str, Any] = {}
    errors: list[str] = []
    for path, pir_ids in selections.items():
        sub = dict(previous.pop(path, None) or {})
        if not pir_ids:
            if _held(sub):
                leftover = await module.remove(client, sub)
                if leftover:
                    errors.append(f"{path}: objects not removed: {leftover}")
                    paths[path] = sub
            continue
        try:
            spec = await _spec(db, row.provider, row.kind, path=path, pir_ids=pir_ids)
        except SourceRefusedError as exc:
            errors.append(f"{path}: {exc}")
            paths[path] = {**sub, "status": "error", "error": str(exc), "checked_at": stamp}
            continue
        spec = {**spec, "origin": {"source_id": str(row.id)}}
        sub = await module.reconcile(client, spec, sub, url=url, secret=secret, now=now)
        sub["pir_ids"] = list(pir_ids)
        paths[path] = sub
        if not is_connected(sub):
            errors.append(f"{path}: {sub.get('error') or 'not provisioned'}")
    for gone in previous.values():  # a path no longer fed by a PIR playbook
        if _held(gone):
            await module.remove(client, gone)
    legacy = {k: state[k] for k in ("notifier_id", "playbook_id", "trigger_id") if state.get(k)}
    if legacy and not errors:
        leftover = await module.remove(client, legacy)
        if leftover:
            errors.append(f"objects not removed: {leftover}")
    out: dict[str, Any] = {
        "status": "error" if errors else "provisioned",
        "error": "; ".join(errors)[:500] if errors else None,
        "paths": paths,
        "playbook_ids": sorted(s["playbook_id"] for s in paths.values() if s.get("playbook_id")),
        "fingerprint": ",".join(f"{p}={s.get('fingerprint')}" for p, s in sorted(paths.items())),
        "checked_at": stamp,
        "integration_id": integration_id,
    }
    if errors:
        out.update(legacy)  # kept until the per-path playbooks deliver
    elif not paths:
        out["status"], out["error"] = "error", "no PIR selected on any path: the PIR playbook feeds nothing"
    return out


async def _reconcile(db: Any, row: Any, now: datetime) -> dict[str, Any]:
    """Provision or repair *row* as its run-as; the new state (``status`` error with the reason on failure)."""
    from app.core.config import settings

    stamp = now.isoformat()
    state = dict(row.state or {})
    reason = await _run_as_reason(db, row.provider, row.run_as_user_id)
    if reason:
        return {**state, "status": "error", "error": reason, "checked_at": stamp}
    module = _module(row.provider, row.kind)
    conn = await _connection(db, row)
    if isinstance(conn, str):
        return {**state, "status": "error", "error": conn, "checked_at": stamp}
    if PROVISIONED[(row.provider, row.kind)].per_pir_path:
        return await _reconcile_pir_paths(db, row, conn, now)
    client, integration_id, secret = conn
    url = receiver_url(settings.base_url, integration_id, str(row.id))
    top = {k: v for k, v in state.items() if k != "paths"}
    paths = await _reconcile_path_objects(db, row, client, dict(state.get("paths") or {}), url, secret, now)
    try:
        spec = await _spec(db, row.provider, row.kind)
    except NothingToListenError as exc:
        # Every path is off: the objects go, so the source platform stops delivering (#2583 point 7).
        leftover = await module.remove(client, top) if _held(top) else None
        if leftover:
            return {
                **state,
                "paths": paths,
                "status": "error",
                "error": f"{exc}; objects not removed: {leftover}",
                "checked_at": stamp,
            }
        out = {"status": IDLE, "error": str(exc), "checked_at": stamp, "integration_id": integration_id}
        return {**out, "paths": paths} if paths else out
    except SourceRefusedError as exc:
        return {**state, "status": "error", "error": str(exc), "checked_at": stamp}
    spec = {**spec, "origin": {"source_id": str(row.id)}}
    new_state = await module.reconcile(client, spec, top, url=url, secret=secret, now=now)
    new_state["integration_id"] = integration_id
    if paths:
        new_state["paths"] = paths
    return new_state


async def _tracked_organizations(db: Any, provider: str, spec: dict[str, Any]) -> dict[str, Any]:
    """*spec* with the tracked organizations of its scope; called by name so the session stays visible: it only reads."""
    if provider == "opencti":
        from app.services.opencti_trigger_provisioning import with_tracked_organizations as opencti_tracked

        return await opencti_tracked(db, spec)
    return spec


async def _reconcile_path_objects(
    db: Any, row: Any, client: Any, previous: dict[str, Any], url: str, secret: str, now: datetime
) -> dict[str, Any]:
    """The objects of the paths that need their own (#2583 point 5), as ``{path: state}``.

    A path set to Not used here, or with nothing to scope it to (no tracked organization
    with monitoring on and an OpenCTI identity), holds nothing: its objects are removed and
    the path is quiet, never an error of the source.
    """
    entry = PROVISIONED[(row.provider, row.kind)]
    if not entry.path_objects:
        return {}
    from app.services.ctem_signal_sources import excluded_sources

    module = _module(row.provider, row.kind)
    excluded = await excluded_sources(db, None)
    out: dict[str, Any] = {}
    for path, build in entry.path_objects:
        sub = dict(previous.pop(path, None) or {})
        spec = build()
        try:
            if _listened(excluded, path, row.provider, "") and spec.get("organizations") not in (None, "primary"):
                spec = await _tracked_organizations(db, row.provider, spec)
        except Exception as exc:  # one path never breaks the source
            logger.warning("signal source %s: the tracked organizations of %s were not read", row.id, path)
            out[path] = {**sub, "status": "error", "error": str(exc)[:500], "checked_at": now.isoformat()}
            continue
        wanted = _listened(excluded, path, row.provider, "") and (
            spec.get("organizations") in (None, "primary") or bool(spec.get("organization_refs"))
        )
        if not wanted:
            leftover = await module.remove(client, sub) if _held(sub) else None
            if leftover:
                out[path] = {**sub, "status": "error", "error": f"objects not removed: {leftover}"}
            continue
        spec = {**spec, "origin": {"source_id": str(row.id)}}
        out[path] = await module.reconcile(client, spec, sub, url=url, secret=secret, now=now)
    for gone in previous.values():
        if _held(gone):
            await module.remove(client, gone)
    return out


async def _remove(db: Any, row: Any) -> str | None:
    """Delete what *row* holds in the source platform; the error text, ``None`` when nothing is left."""
    state = row.state if isinstance(row.state, dict) else {}
    if not _held(state):
        return None
    conn = await _connection(db, row)
    if isinstance(conn, str):
        return conn
    module = _module(row.provider, row.kind)
    errors: list[str] = []
    for sub in (state.get("paths") or {}).values():
        if _held(sub):
            err = await module.remove(conn[0], sub)
            if err:
                errors.append(err)
    top = {k: v for k, v in state.items() if k != "paths"}
    if _held(top):
        err = await module.remove(conn[0], top)
        if err:
            errors.append(err)
    return "; ".join(errors) if errors else None


async def get_or_create(db: Any, provider: str, kind: str, integration_id: uuid.UUID) -> Any:
    from sqlalchemy import select

    from app.models.ctem_signal_source import CtemSignalSource

    row = (
        await db.execute(
            select(CtemSignalSource).where(
                CtemSignalSource.provider == provider,
                CtemSignalSource.kind == kind,
                CtemSignalSource.integration_id == integration_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = CtemSignalSource(
            id=uuid.uuid4(), provider=provider, kind=kind, integration_id=integration_id, enabled=False, state={}
        )
        db.add(row)
        await db.flush()
    return row


async def enable(
    db: Any,
    *,
    provider: str,
    kind: str,
    integration_id: uuid.UUID,
    run_as_user_id: uuid.UUID,
    now: datetime | None = None,
) -> Any:
    """Enable one source as *run_as_user_id* and provision it at once; raises :class:`SourceRefusedError` with the reason.

    A refused enable stays off and leaves no object behind in the source platform.
    The caller commits.
    """
    from app.models.integration import Integration

    if (provider, kind) not in PROVISIONED:
        raise SourceRefusedError(f"{provider} {kind} is not a source XTM One provisions")
    integration = await db.get(Integration, integration_id)
    if integration is None or integration.provider != provider:
        raise SourceRefusedError(f"no {PROVISIONED[(provider, kind)].label} integration {integration_id}")
    now = now or datetime.now(UTC)
    row = await get_or_create(db, provider, kind, integration_id)
    row.run_as_user_id = run_as_user_id
    state = await _reconcile(db, row, now)
    if not is_connected(state):
        reason = state.get("error") or "not provisioned"
        row.state = state
        leftover = await _remove(db, row)
        row.enabled = False
        row.state = {"status": "error", "error": reason, "checked_at": now.isoformat()}
        if leftover:
            row.state["leftover"] = {
                k: state.get(k) for k in ("notifier_id", "trigger_ids", "playbook_id") if state.get(k)
            }
        raise SourceRefusedError(reason)
    row.enabled = True
    row.state = state
    return row


async def disable(db: Any, row: Any, *, now: datetime | None = None) -> Any:
    """Turn *row* off and remove its objects; a removal that fails keeps the ids for the next pass. The caller commits."""
    now = now or datetime.now(UTC)
    row.enabled = False
    error = await _remove(db, row)
    if error:
        row.state = {
            **(row.state or {}),
            "status": "error",
            "error": f"objects not removed: {error}",
            "checked_at": now.isoformat(),
        }
    else:
        row.state = {}
    return row


def _due(state: Any, now: datetime) -> bool:
    from datetime import timedelta

    from app.services.openaev_trigger_provisioning import RECHECK_SECONDS

    if not is_connected(state):
        return True
    try:
        checked = datetime.fromisoformat(str(state.get("checked_at")))
    except ValueError:
        return True
    return now - checked >= timedelta(seconds=RECHECK_SECONDS)


async def maintain(db: Any, *, now: datetime | None = None) -> int:
    """Scheduler pass: recheck and repair the enabled sources, finish removing the disabled ones; returns changes."""
    from sqlalchemy import select

    from app.models.ctem_signal_source import CtemSignalSource

    now = now or datetime.now(UTC)
    changed = 0
    rows = (await db.execute(select(CtemSignalSource))).scalars().all()
    for row in rows:
        if (row.provider, row.kind) not in PROVISIONED:
            continue
        before = dict(row.state or {})
        try:
            if row.enabled:
                if not _due(before, now):
                    continue
                row.state = await _reconcile(db, row, now)
            elif _held(before):
                await disable(db, row, now=now)
            else:
                continue
        except Exception as exc:  # one source never breaks the pass
            row.state = {**before, "status": "error", "error": str(exc)[:500], "checked_at": now.isoformat()}
        if before.get("status") != (row.state or {}).get("status") or before.get("fingerprint") != (
            row.state or {}
        ).get("fingerprint"):
            changed += 1
    await db.commit()
    return changed


#: The built-in CTEM assignments that provision a source today, ``(agent slug, name)``: the
#: holders :func:`adopt_from_assignments` migrates (#2434, no breaking change).
HOLDER_ASSIGNMENTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("ctem-vuln-prioritization-orchestrator", "New KEV or EPSS jump"),
        ("ctem-vuln-prioritization-orchestrator", "Vulnerability on software we run"),
        ("ctem-vuln-prioritization-orchestrator", "Vulnerability flagged in a PIR"),
        ("ctem-vuln-prioritization-orchestrator", "Vulnerability sighted"),
        ("ctem-vuln-prioritization-orchestrator", "OpenAEV results into cases"),
        ("ctem-threat-response-orchestrator", "New relevant threat"),
        ("ctem-threat-response-orchestrator", "Threat activity flagged in a PIR"),
    }
)


def held_source(provider: Any, state: Any) -> tuple[str, str] | None:
    """The platform source a holder's connected provisioning *state* stands for, else ``None``."""
    if not is_connected(state):
        return None
    if provider == "opencti":
        return ("opencti", "pir_playbook" if state.get("playbook_id") else "listener")
    if provider == "openaev":
        return ("openaev", "listener")
    return None


async def adopt_from_assignments(db: Any, *, now: datetime | None = None) -> int:
    """Migrate the sources enabled built-in holders provision to platform sources; returns how many were enabled.

    For each enabled holder (:data:`HOLDER_ASSIGNMENTS`) whose objects exist, the
    source of the same integration that is not on yet is enabled with the
    holder's run-as and provisioned at once. The holder's own objects are left
    in place: they are removed by its provider pass once the seed no longer
    declares them, so the new objects are verified before the old ones go and
    no delivery is lost. A source that cannot be provisioned stays off with its
    reason, the holder untouched. Runs before the provider passes
    (``trigger_provisioning.maintain_trigger_provisioning``), which would
    otherwise remove a holder's objects first; the caller commits.
    """
    from sqlalchemy import select

    from app.models.agent import Agent
    from app.models.assignment import Assignment
    from app.services.openaev_trigger_provisioning import STATE_KEY as OPENAEV_STATE_KEY
    from app.services.opencti_trigger_provisioning import STATE_KEY as OPENCTI_STATE_KEY

    now = now or datetime.now(UTC)
    keys = {"opencti": OPENCTI_STATE_KEY, "openaev": OPENAEV_STATE_KEY}
    rows = (
        await db.execute(
            select(Assignment, Agent.slug)
            .join(Agent, Agent.id == Assignment.agent_id)
            # platform_event too: the seed moves a work holder onto ctem_signal.recorded (#2434)
            # before this pass, its old objects kept in its provisioning state.
            .where(
                Assignment.trigger_type.in_(("integration_event", "platform_event")),
                Assignment.enabled.is_(True),
            )
        )
    ).all()
    adopted = 0
    for assignment, slug in rows:
        if (slug, assignment.name) not in HOLDER_ASSIGNMENTS:
            continue
        config = assignment.trigger_config if isinstance(assignment.trigger_config, dict) else {}
        provider = config.get("provider") or next((p for p, k in keys.items() if isinstance(config.get(k), dict)), None)
        state = config.get(keys.get(str(provider), ""))
        pair = held_source(provider, state)
        if pair is None or not state.get("integration_id") or assignment.run_as_user_id is None:
            continue
        try:
            integration_id = uuid.UUID(str(state["integration_id"]))
        except ValueError:
            continue
        row = await get_or_create(db, pair[0], pair[1], integration_id)
        if row.enabled or (row.state or {}).get("adopted_from"):
            continue
        try:
            await enable(
                db,
                provider=pair[0],
                kind=pair[1],
                integration_id=integration_id,
                run_as_user_id=assignment.run_as_user_id,
                now=now,
            )
            adopted += 1
        except SourceRefusedError as exc:
            logger.warning("Signal source %s %s not adopted from %r: %s", pair[0], pair[1], assignment.name, exc)
        # One attempt per source: a refusal is shown on the source, never retried in a loop.
        row.state = {**(row.state or {}), "adopted_from": str(assignment.id)}
    return adopted


async def replaced_by_source(db: Any, provider: str, state: Any) -> bool:
    """Whether a holder's old objects (*state*) may go: the platform source of the same
    integration is on and connected, so removing them loses no delivery (#2434)."""
    pair = held_source(provider, state)
    if pair is None:
        # No object is held (an error before any was created): clearing it loses nothing.
        return isinstance(state, dict) and bool(state) and not _held(state)
    return await source_connected(db, pair[0], pair[1], state.get("integration_id"))


#: The legacy Threat response holders (#2583 point 4): their OpenCTI objects are retired once the
#: Signals source of the same integration is on and connected, ``(agent slug, name)`` -> the kind
#: of the source that replaces them. Their assignment stays (it listens to nothing afterwards).
RETIRED_HOLDERS: dict[tuple[str, str], str] = {
    ("ctem-threat-response-orchestrator", "New relevant threat"): "listener",
    ("ctem-threat-response-orchestrator", "Threat activity flagged in a PIR"): "pir_playbook",
}


async def source_connected(db: Any, provider: str, kind: str, integration_id: Any) -> bool:
    """Whether the platform source ``(provider, kind)`` of *integration_id* is on and connected."""
    from sqlalchemy import select

    from app.models.ctem_signal_source import CtemSignalSource

    try:
        integration_uuid = uuid.UUID(str(integration_id))
    except ValueError:
        return False
    row = (
        await db.execute(
            select(CtemSignalSource).where(
                CtemSignalSource.provider == provider,
                CtemSignalSource.kind == kind,
                CtemSignalSource.integration_id == integration_uuid,
            )
        )
    ).scalar_one_or_none()
    return row is not None and bool(row.enabled) and is_connected(row.state)


#: How a reason reads as data (code, params), so the UI renders it in the reader's language and
#: never shows the platform's English line as is; first match wins, the rest is the source
#: platform's own answer (``provider_error``, its text a parameter).
_REASON_CODES: tuple[tuple[str, str], ...] = (
    (r"^no run-as user", "no_run_as"),
    (r"^the run-as user is deactivated", "run_as_inactive"),
    (r"^The run-as user has no connected (?P<provider>.+?) integration", "run_as_no_integration"),
    (r"webhook_secret", "no_webhook_secret"),
    (r"^no .+ token could be resolved", "no_token"),
    (r"(integration .+ not found$|^no .+ integration )", "integration_not_found"),
    (r"^organization context: (?P<fields>.+?) not filled", "organization_context_missing"),
    (r"^objects not removed: (?P<detail>.+)$", "objects_not_removed"),
    (r"^not provisioned", "not_provisioned_yet"),
)


def reason_code(reason: str | None) -> tuple[str | None, dict[str, str]]:
    """``(code, params)`` of a refusal or error reason; ``(None, {})`` when there is none."""
    import re

    if not reason:
        return None, {}
    for pattern, code in _REASON_CODES:
        found = re.search(pattern, reason)
        if found:
            return code, {k: v for k, v in found.groupdict().items() if v}
    return "provider_error", {"detail": reason[:500]}


def provisioned_objects(state: Any) -> list[dict[str, str]]:
    """The objects a source holds in its platform, ``[{kind, id}]`` (#2583 point 2), for the Open in links.

    ``kind`` is ``notifier``, ``trigger`` or ``playbook``; per-path states (the PIR playbook)
    are read too. An id held twice is listed once, in a stable order.
    """
    if not isinstance(state, dict):
        return []
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: Any, stem: str = "", path: str = "") -> None:
        if isinstance(value, str) and value and (kind, value) not in seen:
            seen.add((kind, value))
            # #2583 point 6: the name the object carries in its platform, so the row says what it holds.
            out.append(
                {
                    "kind": kind,
                    "id": value,
                    **({"name": f"{stem} - {kind.capitalize()}"} if stem else {}),
                    # #2627: the path a per-path object serves, so two links of one kind read apart.
                    **({"path": path, "path_label": path_label(path)} if path else {}),
                }
            )

    def walk(sub: dict[str, Any], path: str = "") -> None:
        raw = sub.get("name")
        stem = raw if isinstance(raw, str) else ""
        add("notifier", sub.get("notifier_id"), stem, path)
        add("trigger", sub.get("trigger_id"), stem, path)
        triggers = sub.get("trigger_ids")
        for value in triggers.values() if isinstance(triggers, dict) else triggers or []:
            add("trigger", value, stem, path)
        add("playbook", sub.get("playbook_id"), stem, path)

    walk(state)
    for path, sub in sorted((state.get("paths") or {}).items()):
        if isinstance(sub, dict):
            walk(sub, str(path))
    return out


#: Where an assignment keeps the state of what it provisioned in its platform, per provider (#2640).
ASSIGNMENT_STATE_KEYS: dict[str, str] = {"opencti": "opencti_provisioning", "openaev": "openaev_provisioning"}

#: The owner shown for an XTM One object whose name names none.
UNKNOWN_OWNER = "XTM One"


def object_owner(name: Any) -> str:
    """The fleet or desk that owns an XTM One object, read from ``XTM One - <Owner> - <Purpose>`` (#2640)."""
    if not isinstance(name, str):
        return ""
    parts = [p.strip() for p in name.split(" - ")]
    return parts[1] if len(parts) >= 3 and parts[0] == "XTM One" and parts[1] else ""


def owned_objects(state: Any, owner: str) -> list[dict[str, Any]]:
    """``provisioned_objects`` with each object's *owner*, and the module event of each per-event trigger (#2640)."""
    triggers = state.get("trigger_ids") if isinstance(state, dict) else None
    events = {v: k for k, v in triggers.items() if isinstance(v, str) and v} if isinstance(triggers, dict) else {}
    out: list[dict[str, Any]] = []
    for obj in provisioned_objects(state):
        event = events.get(obj["id"]) if obj["kind"] == "trigger" else None
        out.append({**obj, "owner": owner, **({"event": event} if event else {})})
    return out


def fleet_objects(configs: Iterable[Any], provider: str, integration_id: str) -> list[dict[str, Any]]:
    """The objects the fleets' assignments provisioned on one integration, each with its owner (#2640).

    They sit beside a source's own objects in the module; the Signal sources page lists them apart,
    so a source row counts only its own. *configs* are assignment ``trigger_config`` values. Pure.
    """
    key = ASSIGNMENT_STATE_KEYS.get(provider)
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    if not key:
        return out
    for config in configs:
        if not isinstance(config, dict) or config.get("provider") != provider:
            continue
        state = config.get(key)
        if not isinstance(state, dict) or str(state.get("integration_id") or "") != str(integration_id):
            continue
        owner = object_owner(state.get("name")) or UNKNOWN_OWNER
        for obj in owned_objects(state, owner):
            if (obj["kind"], obj["id"]) not in seen:
                seen.add((obj["kind"], obj["id"]))
                out.append(obj)
    return out


def with_shared_events(own: list[dict[str, Any]], others: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark each own trigger whose module event another owner also listens to, ``shared_with`` its owners (#2640).

    Each owner keeps its own trigger on such an event: the delivery reaches its own receiver, which
    starts its own run as its own run-as user. The page says so instead of hiding the second trigger.
    """
    owners: dict[str, set[str]] = {}
    for obj in others:
        if obj.get("event"):
            owners.setdefault(obj["event"], set()).add(str(obj.get("owner") or UNKNOWN_OWNER))
    return [{**obj, "shared_with": sorted(owners[obj["event"]])} if obj.get("event") in owners else obj for obj in own]


def fed_paths(provider: str, kind: str) -> list[dict[str, str]]:
    """The trigger paths a source feeds, ``[{path, label}]`` in catalog order (#2583 point 6). Pure.

    One provisioned object can feed several paths (the OpenCTI live listener): turning it off stops
    them all, so the source row names them before an administrator switches it.
    """
    from app.services.ctem_signal_sources import PATH_SOURCES

    return [
        {"path": path, "label": path_label(path)}
        for path, rows in PATH_SOURCES.items()
        if any(s.provider == provider and s.kind == kind for s in rows)
    ]


def to_dict(row: Any) -> dict[str, Any]:
    status, reason = status_of(row)
    code, params = reason_code(reason)
    return {
        "id": str(row.id),
        "provider": row.provider,
        "kind": row.kind,
        "integration_id": str(row.integration_id),
        "enabled": bool(row.enabled),
        "run_as_user_id": str(row.run_as_user_id) if row.run_as_user_id else None,
        "status": status,
        "reason": reason,
        "reason_code": code,
        "reason_params": params,
        "checked_at": (row.state or {}).get("checked_at"),
        "objects": owned_objects(row.state, OBJECT_FLEET),
        "feeds": fed_paths(row.provider, row.kind),
    }
