# SPDX-License-Identifier: Apache-2.0

"""Upgrades of the components a connected platform runs, read on a schedule.

A registered OpenCTI reports its own version on each registration heartbeat
(``integration_versions``), but not the versions of its connectors: a rolling
connector image upgraded in place changes nothing XTM One hears. This step of
the autonomous tick reads each registered OpenCTI's ``connectors {id name slug
version}``, keeps the last version seen per platform and connector, and raises
``integration.version.changed`` with a ``component_*`` object when a known
connector reports another version.

Cost and identity:

* nothing is read while no enabled assignment subscribes to the event
  (:func:`platform_events.live_subscriptions`), and one read per platform runs
  at most every :data:`INTERVAL_SECONDS` across replicas (a Redis slot);
* a module call acts as a person (``module_identity``): the read is made as a
  subscriber's run-as user (else its owner) through
  :func:`platform_access.resolve_platform_client` (the integration visible to
  that person, its credential usable by them, their own minted JWT on a
  registered platform), and the capability probe never sends the query
  when their role lacks the ``MODULES`` capability ``connectors`` needs;
* a first sight of a connector records its version and raises nothing; a
  connector that reports no version is skipped.

OpenAEV injectors and collectors report no version today: only OpenCTI
connectors are read.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from app.services.integration_versions import (
    INTEGRATION_VERSION_CHANGED_EVENT,
    version_change,
    version_changed_data,
)

logger = logging.getLogger(__name__)

#: Seconds between two reads of one platform's components (all replicas).
INTERVAL_SECONDS = 300
#: The component kind an OpenCTI connector is reported as.
COMPONENT_CONNECTOR = "connector"
#: The providers whose components are read.
COMPONENT_PROVIDERS: tuple[str, ...] = ("opencti",)

_SLOT_KEY = "integration-component-versions:slot:{platform}"
_SEEN_KEY = "integration-component-versions:seen:{platform}"

_OPENCTI_CONNECTORS = "query XtmOneConnectorVersions { connectors { id name slug version } }"


def component_event_id(platform_identifier: str, component_id: str, previous_version: str, version: str) -> str:
    """The stable event id of one upgrade of one component (one run per subscriber)."""
    digest = hashlib.sha256(
        f"{platform_identifier}\n{component_id}\n{previous_version.strip()}\n{version.strip()}".encode()
    ).hexdigest()[:32]
    return f"integration-component-version-{digest}"


def component_changed_data(
    *,
    provider: str,
    platform_identifier: str,
    integration_id: str | None,
    component: dict[str, str],
    previous_version: str,
    version: str,
) -> dict[str, Any]:
    """The payload of ``integration.version.changed`` for a component upgrade."""
    data = version_changed_data(
        provider=provider,
        platform_identifier=platform_identifier,
        integration_id=integration_id,
        previous_version=previous_version,
        version=version,
    )
    data.update(
        {
            "component_kind": component["kind"],
            "component_id": component["id"],
            "component_name": component["name"],
            "component_slug": component.get("slug") or "",
        }
    )
    return data


def component_changes(
    seen: dict[str, str], components: list[dict[str, str]]
) -> tuple[dict[str, str], list[tuple[dict[str, str], str]]]:
    """The versions to record and the ``(component, previous_version)`` pairs that moved.

    *seen* maps a component id to the version last recorded. A component without
    a version is neither recorded nor compared; a first sight is recorded only.
    """
    record: dict[str, str] = {}
    moved: list[tuple[dict[str, str], str]] = []
    for component in components:
        version = (component.get("version") or "").strip()
        if not component.get("id") or not version:
            continue
        previous = seen.get(component["id"])
        if previous != version:
            record[component["id"]] = version
        if version_change(previous, version):
            moved.append((component, previous or ""))
    return record, moved


def opencti_components(data: dict[str, Any]) -> list[dict[str, str]]:
    """The connectors of an OpenCTI ``connectors`` answer, as components."""
    rows = data.get("connectors") if isinstance(data, dict) else None
    out: list[dict[str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        out.append(
            {
                "kind": COMPONENT_CONNECTOR,
                "id": str(row["id"]),
                "name": str(row.get("name") or ""),
                "slug": str(row.get("slug") or ""),
                "version": str(row.get("version") or ""),
            }
        )
    return out


async def _take_slot(platform_identifier: str) -> bool:
    """Whether this replica reads *platform_identifier* now (one read per interval)."""
    from app.core.redis import redis_client

    client = await redis_client()
    return bool(await client.set(_SLOT_KEY.format(platform=platform_identifier), "1", nx=True, ex=INTERVAL_SECONDS))


async def _read_opencti(db: Any, integration: Any, user_ids: list[Any]) -> list[dict[str, str]] | None:
    """The connectors of the OpenCTI behind *integration*, read as the first of *user_ids* that can."""
    from app.integrations.providers.opencti._common import _client, _graphql
    from app.integrations.providers.opencti.capabilities import attach_probe
    from app.services.platform_access import PlatformAccessError, resolve_platform_client

    for user_id in user_ids:
        # The person-scoped resolver: the integration visible to *user_id* as a
        # non-admin, its credential usable by them, a JWT minted for them on a
        # registered platform. Never the tool funnel's private token resolver.
        try:
            platform = await resolve_platform_client(db, user_id, integration.id, "opencti")
        except PlatformAccessError as exc:
            logger.info(
                "Component versions of integration %s not readable as %s: %s", integration.id, user_id, exc.code
            )
            continue
        api_base = platform.base_url
        headers: Any = {**platform.headers, "Content-Type": "application/json"}
        if getattr(integration, "auth_type", None) == "platform_managed":
            headers = attach_probe(headers, api_base)
        async with _client(headers, timeout=30) as client:
            data, error = await _graphql(client, api_base, headers, _OPENCTI_CONNECTORS)
        if error:
            logger.info("Component versions of integration %s not read as %s: %s", integration.id, user_id, error)
            continue
        return opencti_components(data)
    return None


async def component_versions_tick() -> int:
    """Read the components of each registered platform due a read; the events raised.

    Never raises.
    """
    from app.services.platform_events import live_subscriptions

    try:
        subs = await live_subscriptions(INTEGRATION_VERSION_CHANGED_EVENT)
    except Exception as exc:
        logger.warning("Component versions: subscriptions unreadable: %r", exc)
        return 0
    user_ids: list[Any] = []
    for sub in subs:
        for candidate in (sub.run_as_user_id, sub.user_id):
            if candidate is not None and candidate not in user_ids:
                user_ids.append(candidate)
    if not user_ids:
        return 0
    from sqlalchemy import select

    from app.core.database import async_session
    from app.models.integration import Integration
    from app.models.platform_registration import PlatformRegistration

    raised = 0
    async with async_session() as db:
        rows = (
            await db.execute(
                select(PlatformRegistration, Integration)
                .join(Integration, Integration.id == PlatformRegistration.integration_id)
                .where(
                    PlatformRegistration.status == "active",
                    PlatformRegistration.platform_identifier.in_(COMPONENT_PROVIDERS),
                )
            )
        ).all()
        for reg, integration in rows:
            try:
                raised += await _platform_tick(db, reg, integration, user_ids)
            except Exception as exc:
                logger.warning("Component versions of %s failed: %r", reg.platform_identifier, exc)
    return raised


async def _platform_tick(db: Any, reg: Any, integration: Any, user_ids: list[Any]) -> int:
    if not await _take_slot(reg.platform_identifier):
        return 0
    components = await _read_opencti(db, integration, user_ids)
    if components is None:
        return 0
    from app.core.redis import redis_client
    from app.services.platform_events import publish

    # Any: redis-py types its hash commands as sync-or-async.
    client: Any = await redis_client()
    key = _SEEN_KEY.format(platform=reg.platform_identifier)
    stored = await client.hgetall(key)
    seen = {
        (k.decode() if isinstance(k, bytes) else str(k)): (v.decode() if isinstance(v, bytes) else str(v))
        for k, v in (stored or {}).items()
    }
    record, moved = component_changes(seen, components)
    if record:
        await client.hset(key, mapping=record)
    for component, previous in moved:
        version = component["version"].strip()
        await publish(
            INTEGRATION_VERSION_CHANGED_EVENT,
            component_changed_data(
                provider=reg.platform_identifier,
                platform_identifier=reg.platform_identifier,
                integration_id=str(integration.id),
                component=component,
                previous_version=previous,
                version=version,
            ),
            event_id=component_event_id(reg.platform_identifier, component["id"], previous, version),
        )
    return len(moved)
