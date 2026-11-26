# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Auto-provisioned OpenAEV notification triggers for ``integration_event`` assignments.

An assignment listening to OpenAEV (``trigger_type == "integration_event"``,
``trigger_config.provider == "openaev"``) that declares
``trigger_config.provision`` gets the OpenAEV objects that make OpenAEV call
it, created under the integration's own identity through OpenAEV's
notifications engine (``/api/notifiers``, ``/api/notification-triggers``):

* one WEBHOOK notifier posting to the assignment's receiver
  (``/api/hooks/integrations/{integration_id}/assignments/{assignment_id}``),
  authenticated with the integration's ``webhook_secret`` sent as the static
  header ``X-OpenAEV-Token``. No secret, no provisioning: an unauthenticated
  receiver is never wired;
* one LIVE trigger per declared event, carrying that notifier.

OpenAEV needs no change for this: its live triggers already watch
simulations, injects and findings. The events map to triggers as follows
(:data:`EVENTS`):

* ``simulation_finished``: resource ``SIMULATION``, ``UPDATE``, filtered on
  ``exercise_status`` = ``FINISHED``;
* ``atomic_test_finished``: resource ``INJECT``, ``UPDATE``, filtered on a
  closed ``inject_status`` (``EXECUTED``, ``PARTIAL``, ``ERROR``);
* ``finding_created``: resource ``FINDING``, ``CREATE``;
* ``finding_updated`` / ``finding_deleted``: resource ``FINDING``, ``UPDATE``
  / ``DELETE`` (CTEM-15, #918);
* ``asset_created`` / ``asset_updated``: resource ``ASSET``, ``CREATE`` /
  ``UPDATE``;
* ``asset_stale``: resource ``ASSET``, ``UPDATE``, filtered on
  ``asset_status`` = ``STALE``: OpenAEV's freshness job raises one asset
  update when an asset stays unseen past the tenant's
  ``asset_stale_after_days`` (off at 0), a path 6 signal of the CTEM loop;
* ``vulnerability_created``: resource ``VULNERABILITY``, ``CREATE``;
* ``scenario_score_degraded``: resource ``SCENARIO``, ``SCORE_DEGRADATION``
  (OpenAEV's semantic event, valid on scenarios only): a mitigation that was
  proven no longer holds, a path 8 signal of the CTEM loop.
* ``critical_asset_reached``: resource ``SIMULATION``, ``CRITICAL_ASSET_REACHED``
  (CTEM-13): a finished simulation's attack path reached an asset of high or
  very high criticality, a path 4 signal. Optional (:data:`OPTIONAL_EVENTS`):
  an OpenAEV that refuses it keeps the other triggers.

Every object is named ``XTM One - <fleet> - <purpose>``; a trigger adds
`` - <event>``, which is how the receiver names the event of a delivery
(:func:`event_from_notification_name`): OpenAEV's default webhook body
carries the trigger name, the operation and the resource, not our event.

The pass (:func:`maintain`, run by ``services/trigger_provisioning.py``) is
idempotent: an assignment without state is provisioned, a provisioned one is
checked again every :data:`RECHECK_SECONDS` and repaired when an object was
deleted or edited in OpenAEV, and one that no longer declares ``provision``,
is disabled or deleted (:func:`deprovision`) has its objects removed. The
state lives in ``trigger_config.openaev_provisioning``.

``provision`` contract::

    {"fleet": "Exposure validation", "purpose": "Finished simulations",
     "events": ["simulation_finished", "finding_created"]}
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

PROVIDER = "openaev"
PROVISION_KEY = "provision"
STATE_KEY = "openaev_provisioning"
NAME_PREFIX = "XTM One"
AUTH_HEADER = "X-OpenAEV-Token"
RECHECK_SECONDS = 900

#: event -> (resource type, OpenAEV operation, filter key, filter values).
EVENTS: dict[str, tuple[str, str, str | None, tuple[str, ...]]] = {
    "simulation_finished": ("SIMULATION", "UPDATE", "exercise_status", ("FINISHED",)),
    "atomic_test_finished": ("INJECT", "UPDATE", "inject_status", ("EXECUTED", "PARTIAL", "ERROR")),
    "finding_created": ("FINDING", "CREATE", None, ()),
    "finding_updated": ("FINDING", "UPDATE", None, ()),
    "finding_deleted": ("FINDING", "DELETE", None, ()),
    "asset_created": ("ASSET", "CREATE", None, ()),
    "asset_updated": ("ASSET", "UPDATE", None, ()),
    "asset_stale": ("ASSET", "UPDATE", "asset_status", ("STALE",)),
    "vulnerability_created": ("VULNERABILITY", "CREATE", None, ()),
    "scenario_score_degraded": ("SCENARIO", "SCORE_DEGRADATION", None, ()),
    "critical_asset_reached": ("SIMULATION", "CRITICAL_ASSET_REACHED", None, ()),
}
#: Events an older OpenAEV may not know: a refused trigger is recorded as unsupported and
#: never fails the assignment's other triggers (``critical_asset_reached``: CTEM-13).
OPTIONAL_EVENTS: frozenset[str] = frozenset({"critical_asset_reached"})

#: ``(method, path, json)`` -> ``(status, body)``; the body is ``None`` on 404.
Api = Callable[[str, str, Any], Awaitable[Any]]


class ProvisionSpecError(ValueError):
    """``trigger_config.provision`` cannot be turned into OpenAEV triggers."""


class OpenAEVApiError(RuntimeError):
    """An OpenAEV call failed (the message is the API's own error)."""


def read_provision_spec(trigger_config: Any) -> dict[str, Any] | None:
    """The normalised ``provision`` block, ``None`` when absent; raises on a bad one."""
    if not isinstance(trigger_config, dict) or PROVISION_KEY not in trigger_config:
        return None
    raw = trigger_config[PROVISION_KEY]
    if not isinstance(raw, dict):
        raise ProvisionSpecError("provision must be an object")
    fleet = raw.get("fleet")
    purpose = raw.get("purpose")
    if not isinstance(fleet, str) or not fleet.strip() or not isinstance(purpose, str) or not purpose.strip():
        raise ProvisionSpecError("provision.fleet and provision.purpose are required")
    events = raw.get("events")
    if isinstance(events, str):
        events = [events]
    if not isinstance(events, list) or not events:
        raise ProvisionSpecError(f"provision.events must list one or more of: {', '.join(EVENTS)}")
    unknown = sorted({str(e) for e in events if e not in EVENTS})
    if unknown:
        raise ProvisionSpecError(f"provision.events: unknown {unknown}; one of: {', '.join(EVENTS)}")
    return {"fleet": fleet.strip(), "purpose": purpose.strip(), "events": sorted(set(events))}


def object_name(spec: dict[str, Any]) -> str:
    return f"{NAME_PREFIX} - {spec['fleet']} - {spec['purpose']}"


def trigger_name(spec: dict[str, Any], event: str) -> str:
    return f"{object_name(spec)} - {event}"


def event_from_notification_name(name: Any) -> str | None:
    """The provisioned event a delivery names (its trigger's name suffix), else ``None``."""
    if not isinstance(name, str) or not name.startswith(f"{NAME_PREFIX} - "):
        return None
    suffix = name.rsplit(" - ", 1)[-1]
    return suffix if suffix in EVENTS else None


def receiver_url(base_url: str, integration_id: str, assignment_id: str) -> str:
    return f"{base_url.rstrip('/')}/api/hooks/integrations/{integration_id}/assignments/{assignment_id}"


def notifier_body(name: str, url: str, secret: str) -> dict[str, Any]:
    """A WEBHOOK notifier; no template, so OpenAEV posts its default JSON body."""
    return {
        "notifier_name": name,
        "notifier_description": "Provisioned by XTM One: do not edit, it is repaired.",
        "notifier_type": "WEBHOOK",
        "notifier_configuration": {"url": url, "verb": "POST", "headers": {AUTH_HEADER: secret}},
    }


def trigger_filters(event: str) -> dict[str, Any] | None:
    _resource, _operation, key, values = EVENTS[event]
    if not key:
        return None
    return {"mode": "and", "filters": [{"key": key, "operator": "eq", "mode": "or", "values": list(values)}]}


def trigger_body(spec: dict[str, Any], event: str, notifier_id: str) -> dict[str, Any]:
    resource, operation, _key, _values = EVENTS[event]
    body: dict[str, Any] = {
        "notification_trigger_name": trigger_name(spec, event),
        "notification_trigger_type": "LIVE",
        "notification_trigger_enabled": True,
        "notification_trigger_resource_type": resource,
        "notification_trigger_event_types": [operation],
        "notification_trigger_notifiers": [notifier_id],
    }
    filters = trigger_filters(event)
    if filters is not None:
        body["notification_trigger_filters"] = filters
    return body


def fingerprint(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]


def _notifier_drifted(stored: Any, desired: dict[str, Any]) -> bool:
    if not isinstance(stored, dict):
        return True
    config = stored.get("notifier_configuration")
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError:
            return True
    want = desired["notifier_configuration"]
    return (
        stored.get("notifier_name") != desired["notifier_name"]
        or stored.get("notifier_type") != "WEBHOOK"
        or not isinstance(config, dict)
        or config.get("url") != want["url"]
        or (config.get("headers") or {}).get(AUTH_HEADER) != want["headers"][AUTH_HEADER]
    )


def _trigger_drifted(stored: Any, desired: dict[str, Any]) -> bool:
    if not isinstance(stored, dict):
        return True
    keys = (
        "notification_trigger_name",
        "notification_trigger_type",
        "notification_trigger_resource_type",
        "notification_trigger_event_types",
        "notification_trigger_notifiers",
    )
    if any(stored.get(k) != desired.get(k) for k in keys):
        return True
    if stored.get("notification_trigger_enabled") is False:
        return True
    # OpenAEV may echo a filter group with extra keys: only a missing or
    # different filter clause of a filtered event is drift.
    want = desired.get("notification_trigger_filters")
    if want is None:
        return False
    have = stored.get("notification_trigger_filters")
    clauses = have.get("filters") if isinstance(have, dict) else None
    if not isinstance(clauses, list) or not clauses or not isinstance(clauses[0], dict):
        return True
    clause, wanted = clauses[0], want["filters"][0]
    return clause.get("key") != wanted["key"] or sorted(clause.get("values") or []) != sorted(wanted["values"])


async def _ensure_notifier(api: Api, notifier_id: str | None, body: dict[str, Any]) -> str:
    if notifier_id:
        stored = await api("get", f"/api/notifiers/{notifier_id}", None)
        if stored is not None:
            if _notifier_drifted(stored, body):
                await api("put", f"/api/notifiers/{notifier_id}", body)
            return notifier_id
    created = await api("post", "/api/notifiers", body)
    new_id = created.get("notifier_id") if isinstance(created, dict) else None
    if not new_id:
        raise OpenAEVApiError("OpenAEV returned no notifier_id")
    return str(new_id)


async def _ensure_trigger(api: Api, trigger_id: str | None, body: dict[str, Any]) -> str:
    if trigger_id:
        stored = await api("get", f"/api/notification-triggers/{trigger_id}", None)
        if stored is not None:
            if _trigger_drifted(stored, body):
                await api("put", f"/api/notification-triggers/{trigger_id}", body)
            return trigger_id
    created = await api("post", "/api/notification-triggers", body)
    new_id = created.get("notification_trigger_id") if isinstance(created, dict) else None
    if not new_id:
        raise OpenAEVApiError("OpenAEV returned no notification_trigger_id")
    return str(new_id)


async def reconcile(
    api: Api,
    spec: dict[str, Any],
    state: dict[str, Any] | None,
    *,
    url: str,
    secret: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Bring OpenAEV to *spec*; returns the new state (never raises)."""
    state = dict(state or {})
    stamp = (now or datetime.now(UTC)).isoformat()
    name = object_name(spec)
    held = state.get("trigger_ids") if isinstance(state.get("trigger_ids"), dict) else {}
    notifier_id: str | None = None
    trigger_ids: dict[str, str] = {}
    try:
        notifier_id = await _ensure_notifier(api, state.get("notifier_id"), notifier_body(name, url, secret))
        unsupported: list[str] = []
        for event in spec["events"]:
            try:
                trigger_ids[event] = await _ensure_trigger(api, held.get(event), trigger_body(spec, event, notifier_id))
            except OpenAEVApiError:
                if event not in OPTIONAL_EVENTS:
                    raise
                unsupported.append(event)
        for event, stale_id in held.items():
            if event not in trigger_ids and stale_id:
                await api("delete", f"/api/notification-triggers/{stale_id}", None)
    except Exception as exc:  # one assignment never breaks the pass
        # The objects created before the failure stay named, so the next pass reuses
        # them and a removal deletes them, never an orphan in OpenAEV (#2434).
        partial: dict[str, Any] = {}
        if notifier_id:
            partial["notifier_id"] = notifier_id
        if trigger_ids:
            partial["trigger_ids"] = {**held, **trigger_ids}
        return {**state, **partial, "status": "error", "error": str(exc)[:500], "checked_at": stamp}
    out: dict[str, Any] = {
        "status": "provisioned",
        "error": None,
        "name": name,
        "notifier_id": notifier_id,
        "trigger_ids": trigger_ids,
        "fingerprint": fingerprint(name, ",".join(spec["events"]), url),
        "checked_at": stamp,
    }
    if unsupported:
        # The OpenAEV instance predates these events: retried at the next check.
        out["unsupported_events"] = unsupported
    return out


async def remove(api: Api, state: dict[str, Any]) -> str | None:
    """Delete the provisioned triggers then the notifier; the error text, ``None`` when done."""
    try:
        for trigger_id in (state.get("trigger_ids") or {}).values():
            if trigger_id:
                await api("delete", f"/api/notification-triggers/{trigger_id}", None)
        if state.get("notifier_id"):
            await api("delete", f"/api/notifiers/{state['notifier_id']}", None)
    except Exception as exc:
        return str(exc)[:500]
    return None


def due_for_check(state: Any, now: datetime) -> bool:
    if not isinstance(state, dict) or state.get("status") != "provisioned":
        return True
    try:
        checked = datetime.fromisoformat(str(state.get("checked_at")))
    except ValueError:
        return True
    return now - checked >= timedelta(seconds=RECHECK_SECONDS)


# ── Database side ─────────────────────────────────────────────────────────────


def bound_integration_id(assignment: Any, agent: Any) -> str | None:
    """The OpenAEV integration the assignment binds (its own binding, else the agent's)."""
    from app.services.trigger_provisioning import direct_binding_id

    return direct_binding_id(assignment, agent, PROVIDER)


async def openaev_integration_id(db: Any, assignment: Any, agent: Any) -> tuple[str | None, str]:
    """``(integration_id, "")`` the assignment provisions through, else ``(None, reason)``.

    A direct binding first; else the agent's ``openaev`` template binding,
    resolved for the provisioning run-as user (never the seed owner, #9188).
    """
    from app.services.fleet_run_as import NO_RUN_AS_REASON, provisioning_user_id
    from app.services.trigger_provisioning import template_integration_id, uses_template

    integration_id = bound_integration_id(assignment, agent)
    if integration_id:
        return integration_id, ""
    if not uses_template(agent, PROVIDER):
        return None, "no OpenAEV integration bound to the assignment or its agent"
    user_id = provisioning_user_id(assignment, agent)
    if user_id is None:
        return None, NO_RUN_AS_REASON
    return await template_integration_id(db, agent, PROVIDER, "OpenAEV", user_id)


async def _connection(db: Any, assignment: Any, agent: Any) -> tuple[Api, str, str] | str:
    """``(api, integration_id, secret)`` or the reason it cannot be built."""
    integration_id, reason = await openaev_integration_id(db, assignment, agent)
    if not integration_id:
        return reason
    from app.services.fleet_run_as import NO_RUN_AS_REASON, provisioning_user_id

    # A fleet automation never provisions as the seed owner (#9188).
    user_id = provisioning_user_id(assignment, agent)
    if user_id is None:
        logger.warning("OpenAEV provisioning of assignment %s skipped: %s", assignment.id, NO_RUN_AS_REASON)
        return NO_RUN_AS_REASON
    return await integration_connection(db, integration_id, user_id)


async def integration_connection(db: Any, integration_id: str, user_id: Any) -> tuple[Api, str, str] | str:
    """``(api, integration_id, secret)`` on one OpenAEV integration as *user_id*, or the reason it cannot be built.

    Shared by the assignment provisioning above and the platform signal sources
    (``services/ctem_platform_sources.py``, #2434), which name their integration
    and run-as themselves.
    """
    from app.integrations.providers.openaev._common import _api, _headers, is_not_found_error
    from app.models.integration import Integration
    from app.services.integration_tools import _get_token
    from app.services.trigger_provisioning import receiver_secret

    try:
        integration = await db.get(Integration, uuid.UUID(str(integration_id)))
    except ValueError:
        integration = None
    if integration is None or integration.provider != PROVIDER:
        return f"OpenAEV integration {integration_id} not found"
    # The receiver checks X-OpenAEV-Token against this reading, a vault reference resolved (#6080).
    try:
        # A platform-managed integration gets its secret generated here (#970).
        secret = await receiver_secret(integration)
    except Exception as exc:  # unreadable config, unreadable vault secret or unusable secret
        return f"the OpenAEV integration's webhook_secret cannot be read: {type(exc).__name__}"
    if not secret:
        return "the OpenAEV integration has no webhook_secret: set one so the receiver authenticates OpenAEV"
    integration_id = str(integration.id)
    token = await _get_token(db, integration_id, PROVIDER, user_id=str(user_id) if user_id else None, integ=integration)
    if not token or "||" not in token:
        return "no OpenAEV token could be resolved for the integration"
    base_url, _, api_token = token.partition("||")
    base_url = base_url.strip().rstrip("/")
    headers = _headers(api_token.strip())

    async def api(method: str, path: str, body: Any) -> Any:
        resp, err = await _api(base_url, headers, method, path, f"{method} {path}", json=body)
        if err:
            if method == "get" and is_not_found_error(err):
                return None
            raise OpenAEVApiError(err)
        if resp is None or not resp.content:
            return {}
        return resp.json()

    return api, integration_id, secret


def _with_state(trigger_config: dict, state: dict | None) -> dict:
    updated = dict(trigger_config)
    if state is None:
        updated.pop(STATE_KEY, None)
    else:
        updated[STATE_KEY] = state
    return updated


def _held(state: Any) -> bool:
    return isinstance(state, dict) and bool(state.get("notifier_id") or state.get("trigger_ids"))


async def provision_one(db: Any, assignment: Any, agent: Any, *, now: datetime | None = None) -> dict | None:
    """Reconcile one assignment; writes and returns its new state (``None`` = nothing held)."""
    from app.core.config import settings

    now = now or datetime.now(UTC)
    config = assignment.trigger_config if isinstance(assignment.trigger_config, dict) else {}
    state = config.get(STATE_KEY) if isinstance(config.get(STATE_KEY), dict) else None
    try:
        spec = read_provision_spec(config) if assignment.enabled else None
    except ProvisionSpecError as exc:
        new_state = {**(state or {}), "status": "error", "error": str(exc), "checked_at": now.isoformat()}
        assignment.trigger_config = _with_state(config, new_state)
        return new_state
    if spec is None and not _held(state):
        if state is not None:
            assignment.trigger_config = _with_state(config, None)
        return None
    conn = await _connection(db, assignment, agent)
    if isinstance(conn, str):
        status = "orphaned" if spec is None else "error"
        new_state = {**(state or {}), "status": status, "error": conn, "checked_at": now.isoformat()}
        assignment.trigger_config = _with_state(config, new_state)
        return new_state
    api, integration_id, secret = conn
    if spec is None:
        error = await remove(api, state or {})
        new_state = None if error is None else {**(state or {}), "status": "error", "error": error, "checked_at": now.isoformat()}
        assignment.trigger_config = _with_state(config, new_state)
        return new_state
    url = receiver_url(settings.base_url, integration_id, str(assignment.id))
    new_state = await reconcile(api, spec, state, url=url, secret=secret, now=now)
    new_state["integration_id"] = integration_id
    assignment.trigger_config = _with_state(config, new_state)
    return new_state


async def deprovision(db: Any, assignment: Any) -> None:
    """Best-effort removal before an assignment is deleted; never raises."""
    from app.models.agent import Agent

    config = assignment.trigger_config if isinstance(assignment.trigger_config, dict) else {}
    state = config.get(STATE_KEY)
    if not _held(state):
        return
    try:
        agent = await db.get(Agent, assignment.agent_id)
        conn = await _connection(db, assignment, agent)
        if isinstance(conn, str):
            logger.warning("OpenAEV provisioning of assignment %s not removed: %s", assignment.id, conn)
            return
        error = await remove(conn[0], state)
        if error:
            logger.warning("OpenAEV provisioning of assignment %s not removed: %s", assignment.id, error)
    except Exception as exc:
        logger.warning("OpenAEV provisioning of assignment %s not removed: %s", assignment.id, exc)


async def maintain(db: Any, *, now: datetime | None = None) -> int:
    """Scheduler pass: reconcile every due OpenAEV assignment; returns how many changed."""
    from sqlalchemy import select

    from app.models.agent import Agent
    from app.models.assignment import Assignment

    now = now or datetime.now(UTC)
    rows = (
        await db.execute(
            select(Assignment, Agent)
            .join(Agent, Agent.id == Assignment.agent_id)
            .where(Assignment.trigger_type == "integration_event")
        )
    ).all()
    changed = 0
    for assignment, agent in rows:
        config = assignment.trigger_config if isinstance(assignment.trigger_config, dict) else {}
        if config.get("provider") != PROVIDER:
            continue
        if PROVISION_KEY not in config and STATE_KEY not in config:
            continue
        wants = PROVISION_KEY in config and assignment.enabled
        if wants and not due_for_check(config.get(STATE_KEY), now):
            continue
        before = config.get(STATE_KEY) or {}
        after = await provision_one(db, assignment, agent, now=now) or {}
        if before.get("fingerprint") != after.get("fingerprint") or before.get("status") != after.get("status"):
            changed += 1
    await db.commit()
    return changed
