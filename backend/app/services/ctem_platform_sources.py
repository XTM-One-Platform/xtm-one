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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: The name a platform source gives its objects in the source platform.
OBJECT_FLEET = "Signals"

#: The run-as a source needs when it has none (a row enabled before the run-as was required).
NO_RUN_AS = "no run-as user: enable the source again and pick the user it provisions as"


def _openaev_spec() -> dict[str, Any]:
    from app.agents.builtin_agents import CTEM_OPENAEV_LOOP_EVENTS

    return {"fleet": OBJECT_FLEET, "purpose": "OpenAEV listener", "events": sorted(CTEM_OPENAEV_LOOP_EVENTS)}


#: The entity types the OpenCTI PIR playbook delivers: the vulnerability and the threat paths.
PIR_PLAYBOOK_ENTITY_TYPES: tuple[str, ...] = (
    "Vulnerability",
    "Intrusion-Set",
    "Threat-Actor-Group",
    "Threat-Actor-Individual",
    "Campaign",
    "Malware",
)


def _opencti_pir_spec() -> dict[str, Any]:
    from app.services import opencti_trigger_provisioning as prov

    spec = prov.read_provision_spec(
        {
            "provider": "opencti",
            "provision": {
                "fleet": OBJECT_FLEET,
                "purpose": "OpenCTI PIR playbook",
                "mode": prov.PIR_PLAYBOOK_MODE,
                "entity_types": list(PIR_PLAYBOOK_ENTITY_TYPES),
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
    "Intrusion-Set",
    "Threat-Actor-Group",
    "Threat-Actor-Individual",
    "Campaign",
    "Malware",
    "User-Account",
    "Email-Addr",
)


def _opencti_listener_spec() -> dict[str, Any]:
    from app.services import opencti_trigger_provisioning as prov

    spec = prov.read_provision_spec(
        {
            "provider": "opencti",
            "provision": {
                "fleet": OBJECT_FLEET,
                "purpose": "OpenCTI live listener",
                "entity_types": list(LIVE_LISTENER_ENTITY_TYPES),
                "event_types": ["create", "update"],
            },
        }
    )
    if spec is None:  # the block above always declares one
        raise RuntimeError("the OpenCTI live listener spec is empty")
    return spec


@dataclass(frozen=True)
class Provisioned:
    """How one source kind of one provider is provisioned."""

    module: str
    spec: Callable[[], dict[str, Any]]
    label: str


#: ``(provider, kind)`` -> how it is provisioned.
PROVISIONED: dict[tuple[str, str], Provisioned] = {
    ("openaev", "listener"): Provisioned("app.services.openaev_trigger_provisioning", _openaev_spec, "OpenAEV"),
    ("opencti", "listener"): Provisioned(
        "app.services.opencti_trigger_provisioning", _opencti_listener_spec, "OpenCTI"
    ),
    ("opencti", "pir_playbook"): Provisioned("app.services.opencti_trigger_provisioning", _opencti_pir_spec, "OpenCTI"),
}


class SourceRefusedError(ValueError):
    """The source cannot be enabled; the message is the exact reason shown to the administrator."""


def receiver_url(base_url: str, integration_id: str, source_id: str) -> str:
    return f"{base_url.rstrip('/')}/api/hooks/integrations/{integration_id}/signal-sources/{source_id}"


def is_connected(state: Any) -> bool:
    """Connected: the objects exist in the source platform and the last check passed."""
    return (
        isinstance(state, dict)
        and state.get("status") == "provisioned"
        and bool(state.get("trigger_ids") or state.get("trigger_id") or state.get("playbook_id"))
    )


def status_of(row: Any) -> tuple[str, str | None]:
    """``(status, reason)`` of a row: ``connected``, ``off`` or ``error`` with its reason."""
    state = row.state if isinstance(getattr(row, "state", None), dict) else {}
    if not row.enabled:
        return ("error", state.get("error")) if state.get("status") == "error" else ("off", None)
    if is_connected(state):
        return "connected", None
    return "error", state.get("error") or "not provisioned yet"


def _held(state: Any) -> bool:
    return isinstance(state, dict) and bool(
        state.get("notifier_id") or state.get("trigger_ids") or state.get("trigger_id") or state.get("playbook_id")
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


async def _spec(db: Any, provider: str, kind: str) -> dict[str, Any]:
    """The spec of a source, its organization context derived; raises :class:`SourceRefusedError` naming what is missing."""
    spec = PROVISIONED[(provider, kind)].spec()
    if spec.get("organization_context"):
        from app.services import opencti_trigger_provisioning as prov
        from app.services import organization_context as org_context

        row = await org_context.current_version(db)
        profile = org_context.parse_profile(row.profile) if row is not None else None
        spec, missing = prov.derive_from_organization_context(spec, profile)
        if missing:
            raise SourceRefusedError(
                f"organization context: {', '.join(missing)} not filled (Settings > Organization); "
                "the source is not provisioned without them"
            )
    return spec


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
    try:
        spec = await _spec(db, row.provider, row.kind)
    except SourceRefusedError as exc:
        return {**state, "status": "error", "error": str(exc), "checked_at": stamp}
    client, integration_id, secret = conn
    url = receiver_url(settings.base_url, integration_id, str(row.id))
    new_state = await module.reconcile(client, spec, state, url=url, secret=secret, now=now)
    new_state["integration_id"] = integration_id
    return new_state


async def _remove(db: Any, row: Any) -> str | None:
    """Delete what *row* holds in the source platform; the error text, ``None`` when nothing is left."""
    state = row.state if isinstance(row.state, dict) else {}
    if not _held(state):
        return None
    conn = await _connection(db, row)
    if isinstance(conn, str):
        return conn
    return await _module(row.provider, row.kind).remove(conn[0], state)


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
            .where(Assignment.trigger_type == "integration_event", Assignment.enabled.is_(True))
        )
    ).all()
    adopted = 0
    for assignment, slug in rows:
        if (slug, assignment.name) not in HOLDER_ASSIGNMENTS:
            continue
        config = assignment.trigger_config if isinstance(assignment.trigger_config, dict) else {}
        provider = config.get("provider")
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


def to_dict(row: Any) -> dict[str, Any]:
    status, reason = status_of(row)
    return {
        "id": str(row.id),
        "provider": row.provider,
        "kind": row.kind,
        "integration_id": str(row.integration_id),
        "enabled": bool(row.enabled),
        "run_as_user_id": str(row.run_as_user_id) if row.run_as_user_id else None,
        "status": status,
        "reason": reason,
        "checked_at": (row.state or {}).get("checked_at"),
    }
