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

"""Microsoft Defender XDR alert events for ``integration_event`` assignments.

Defender XDR has no push subscription on ``alerts_v2`` or incidents (Graph
change notifications covered only the legacy ``/security/alerts``). Its native
stream is the **Streaming API**: the tenant sends the ``AlertInfo`` and
``AlertEvidence`` tables to an Azure Event Hub it owns (Defender portal >
Settings > Microsoft Defender XDR > Streaming API). The platform reads that
Event Hub with the Azure Event Hubs SDK; nothing is polled from Defender.

* **Connection.** The integration's extra config names the Event Hub
  (``event_hub_namespace``, the fully qualified ``<ns>.servicebus.windows.net``,
  and ``event_hub_name``) and its consumer group (``event_hub_consumer_group``,
  ``$Default`` when empty). The consumer signs in with the integration's Entra
  app (``tenant_id``, client id and client secret; the app needs the *Azure Event
  Hubs Data Receiver* role on the hub), or with ``event_hub_connection_string``
  when one is set. AMQP over WebSockets, port 443.
* **One reader per integration.** Every replica runs :func:`supervise`; a Redis
  lease per integration (``LEASE_TTL_SECONDS``, renewed while the consumer runs)
  lets one replica read it. A consumer runs only while an enabled assignment
  listens to one of :data:`EVENTS` on the integration.
* **Checkpoints.** The sequence number of the last event dispatched is kept per
  partition in Redis; a restart resumes after it, a first start reads from the
  newest event (enabling a trigger never replays history).
* **Once.** Each alert (``alert_created``) or evidence row (``alert_evidence``)
  is claimed per assignment on its own id, so a redelivered batch runs once.
* **Relay groups.** The SDK's AMQP connection cannot go through a relay group:
  an integration routed through one gets no consumer, and the reason is logged.

Filter keys (:func:`filter_reason`): ``severity``, ``category``,
``service_source`` (lists, case-insensitive).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import secrets
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from app.core.utils import format_exception_message

PROVIDER = "defender"

#: Extra config fields of the Event Hub the tenant streams to.
NAMESPACE_FIELD = "event_hub_namespace"
HUB_FIELD = "event_hub_name"
CONSUMER_GROUP_FIELD = "event_hub_consumer_group"
CONNECTION_STRING_FIELD = "event_hub_connection_string"
DEFAULT_CONSUMER_GROUP = "$Default"

#: Streaming API record categories -> trigger event.
_CATEGORY_EVENTS = {
    "advancedhunting-alertinfo": "alert_created",
    "advancedhunting-alertevidence": "alert_evidence",
}
EVENTS = frozenset(_CATEGORY_EVENTS.values())

LEASE_TTL_SECONDS = 90
LEASE_RENEW_SECONDS = 30
SUPERVISE_SECONDS = 60
CHECKPOINT_TTL_SECONDS = 86400 * 30
MAX_BATCH_SIZE = 100

logger = logging.getLogger(__name__)

FILTER_KEYS = {
    "severity": "Only alerts of these severities (High, Medium, Low, Informational)",
    "category": "Only alerts of these categories (e.g. Malware, CredentialAccess, InitialAccess)",
    "service_source": "Only alerts raised by these Defender services (e.g. Microsoft Defender for Endpoint)",
}

ALERT_CREATED_DESCRIPTION = (
    "Carries alert_id, title, severity, category, service_source, detection_source and the attack "
    "techniques. Real time through the Defender XDR Streaming API to your Azure Event Hub. Each alert runs once."
)
ALERT_EVIDENCE_DESCRIPTION = (
    "Carries alert_id, the entity type, its evidence role and the entity fields (device, file, IP, URL, "
    "account). Real time through the Defender XDR Streaming API to your Azure Event Hub. Each row runs once."
)


def catalog_events() -> list[dict[str, Any]]:
    """The ``available_events`` of the ``defender`` catalog entry."""
    return [
        {
            "event": "alert_created",
            "label": "Defender XDR alert raised",
            "description": ALERT_CREATED_DESCRIPTION,
            "mechanism": "stream",
            "filter_keys": dict(FILTER_KEYS),
        },
        {
            "event": "alert_evidence",
            "label": "Defender XDR alert evidence",
            "description": ALERT_EVIDENCE_DESCRIPTION,
            "mechanism": "stream",
            "filter_keys": dict(FILTER_KEYS),
        },
    ]


# ---------------------------------------------------------------------------
# Records -> payloads
# ---------------------------------------------------------------------------


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def records_of(body: object) -> list[Mapping[str, Any]]:
    """The Streaming API records an Event Hub message carries (``{"records": [...]}``)."""
    if isinstance(body, (bytes, bytearray)):
        try:
            body = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return []
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return []
    if not isinstance(body, Mapping):
        return []
    records = body.get("records")
    return [r for r in records if isinstance(r, Mapping)] if isinstance(records, list) else []


def _techniques(value: object) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [str(value)] if value else []
    return [str(v) for v in value] if isinstance(value, list) else []


def record_payload(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The run payload of one record, ``None`` for a table this platform does not read."""
    event = _CATEGORY_EVENTS.get(_text(record.get("category")).casefold())
    props = record.get("properties")
    if event is None or not isinstance(props, Mapping):
        return None
    alert_id = _text(props.get("AlertId"))
    if not alert_id:
        return None
    payload: dict[str, Any] = {
        "provider": PROVIDER,
        "event": event,
        "alert_id": alert_id,
        "tenant_id": _text(record.get("tenantId")),
        "timestamp": _text(props.get("Timestamp") or record.get("time")),
        "title": _text(props.get("Title")),
        "severity": _text(props.get("Severity")),
        "category": _text(props.get("Category")),
        "service_source": _text(props.get("ServiceSource")),
        "detection_source": _text(props.get("DetectionSource")),
        "read_with": "get_defender_alert",
    }
    if event == "alert_created":
        payload["attack_techniques"] = _techniques(props.get("AttackTechniques"))
        payload["message_id"] = f"defender:alert:{alert_id}"
        return payload
    entity = {
        key: props[key]
        for key in (
            "EntityType",
            "EvidenceRole",
            "DeviceId",
            "DeviceName",
            "FileName",
            "FolderPath",
            "SHA256",
            "RemoteIP",
            "RemoteUrl",
            "AccountName",
            "AccountDomain",
            "AccountUpn",
            "EmailSubject",
        )
        if props.get(key) not in (None, "")
    }
    payload["entity_type"] = _text(props.get("EntityType"))
    payload["evidence_role"] = _text(props.get("EvidenceRole"))
    payload["entity"] = entity
    digest = hashlib.sha256(json.dumps(dict(props), sort_keys=True, default=str).encode()).hexdigest()[:24]
    payload["message_id"] = f"defender:evidence:{alert_id}:{digest}"
    return payload


def _strings(value: object) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    return [str(item).strip() for item in items if str(item).strip()]


def filter_reason(payload: Mapping[str, Any], filter_config: Mapping[str, Any]) -> str | None:
    """Why the trigger's filter drops *payload*, ``None`` when it creates a run."""
    for key in ("severity", "category", "service_source"):
        wanted = {s.casefold() for s in _strings(filter_config.get(key))}
        if wanted and _text(payload.get(key)).casefold() not in wanted:
            return f"{key.replace('_', ' ')} is not in {key}"
    return None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def dispatch_records(integration_id: str, records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Start or queue a run per listening assignment for each record; the runs started."""
    from fastapi import HTTPException

    from app.core.database import async_session
    from app.core.redis import redis_client
    from app.services.agentic_gate import refusal_code
    from app.services.assignment_engine import trigger_or_queue_assignment
    from app.services.assignment_pause import AssignmentOwnerDeactivatedError
    from app.services.docusign_connect import provider_assignments
    from app.services.integration_event_filters import (
        event_filter_reason,
        record_filtered_events,
        trigger_filter,
        without_filter_signals,
    )
    from app.services.slack_events import claim_seen, release_seen

    payloads = [p for p in (record_payload(r) for r in records) if p is not None]
    if not payloads:
        return []
    redis = await redis_client()
    triggered: list[dict[str, Any]] = []
    filtered: dict[Any, int] = {}
    async with async_session() as db:
        listeners = await provider_assignments(db, integration_id, PROVIDER, EVENTS)
        for base in payloads:
            event = base["event"]
            for wf, agent in listeners:
                if (wf.trigger_config or {}).get("event") != event:
                    continue
                event_payload = dict(base)
                msg_id = event_payload["message_id"]
                if not await claim_seen(redis, wf.id, msg_id):
                    continue
                filter_config = trigger_filter(wf.trigger_config or {})
                if filter_config and event_filter_reason(event, event_payload, filter_config) is not None:
                    filtered[wf.id] = filtered.get(wf.id, 0) + 1
                    continue
                without_filter_signals(event_payload)
                trigger_context = {
                    "type": "integration_event",
                    "provider": PROVIDER,
                    "event": event,
                    "integration_id": str(integration_id),
                    "message_id": msg_id,
                    "delivery": "defender_stream",
                    "triggered_at": datetime.now(UTC).isoformat(),
                }
                try:
                    result = await trigger_or_queue_assignment(
                        wf, agent, event_payload, db=db, trigger_context=trigger_context, overflow="reject"
                    )
                except HTTPException as exc:
                    await release_seen(redis, wf.id, msg_id)
                    if refusal_code(exc) is None and not isinstance(exc, AssignmentOwnerDeactivatedError):
                        raise
                    continue
                except ValueError:
                    await release_seen(redis, wf.id, msg_id)
                    continue
                except Exception:
                    await release_seen(redis, wf.id, msg_id)
                    raise
                if result.status in ("queue_full", "dropped"):
                    await release_seen(redis, wf.id, msg_id)
                    continue
                triggered.append({"assignment": wf.name, "agent": agent.name, "event": event, "run_id": result.run_id})
    for assignment_id, count in filtered.items():
        await record_filtered_events(assignment_id, count)
    return triggered


# ---------------------------------------------------------------------------
# Checkpoints and leases (Redis)
# ---------------------------------------------------------------------------


def checkpoint_key(integration_id: str, partition_id: str) -> str:
    return f"defender_stream:checkpoint:{integration_id}:{partition_id}"


def lease_key(integration_id: str) -> str:
    return f"defender_stream:lease:{integration_id}"


async def load_checkpoints(redis: Any, integration_id: str, partition_ids: Iterable[str]) -> dict[str, int]:
    """The last sequence number dispatched per partition (partitions never read are absent)."""
    found: dict[str, int] = {}
    for pid in partition_ids:
        raw = await redis.get(checkpoint_key(integration_id, pid))
        if raw is None:
            continue
        with contextlib.suppress(TypeError, ValueError):
            found[pid] = int(raw.decode() if isinstance(raw, bytes) else raw)
    return found


async def save_checkpoint(redis: Any, integration_id: str, partition_id: str, sequence_number: int) -> None:
    await redis.set(checkpoint_key(integration_id, partition_id), str(sequence_number), ex=CHECKPOINT_TTL_SECONDS)


_RENEW_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
)
_RELEASE_LUA = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


async def take_lease(redis: Any, integration_id: str, token: str) -> bool:
    return bool(await redis.set(lease_key(integration_id), token, nx=True, ex=LEASE_TTL_SECONDS))


async def renew_lease(redis: Any, integration_id: str, token: str) -> bool:
    return bool(await redis.eval(_RENEW_LUA, 1, lease_key(integration_id), token, LEASE_TTL_SECONDS))


async def release_lease(redis: Any, integration_id: str, token: str) -> None:
    with contextlib.suppress(Exception):
        await redis.eval(_RELEASE_LUA, 1, lease_key(integration_id), token)


# ---------------------------------------------------------------------------
# Connection settings
# ---------------------------------------------------------------------------


class StreamConfigError(Exception):
    """The integration names no usable Event Hub (the reason is the message)."""


def stream_settings(extra: Mapping[str, Any], client_id: str, client_secret: str) -> dict[str, str]:
    """The consumer's settings, or :class:`StreamConfigError` naming what is missing."""
    connection_string = _text(extra.get(CONNECTION_STRING_FIELD))
    hub = _text(extra.get(HUB_FIELD))
    group = _text(extra.get(CONSUMER_GROUP_FIELD)) or DEFAULT_CONSUMER_GROUP
    if connection_string:
        if not hub and "entitypath=" not in connection_string.casefold():
            raise StreamConfigError("event_hub_name is required when the connection string names no EntityPath")
        return {"connection_string": connection_string, "event_hub_name": hub, "consumer_group": group}
    namespace = _text(extra.get(NAMESPACE_FIELD)).removeprefix("sb://").rstrip("/")
    tenant_id = _text(extra.get("tenant_id"))
    missing = [
        name
        for name, value in (
            (NAMESPACE_FIELD, namespace),
            (HUB_FIELD, hub),
            ("tenant_id", tenant_id),
            ("client_id", client_id),
            ("client_secret", client_secret),
        )
        if not value
    ]
    if missing:
        raise StreamConfigError("missing " + ", ".join(missing))
    if "." not in namespace:
        namespace = f"{namespace}.servicebus.windows.net"
    return {
        "namespace": namespace,
        "event_hub_name": hub,
        "consumer_group": group,
        "tenant_id": tenant_id,
        "client_id": client_id,
        "client_secret": client_secret,
    }


def _consumer_client(settings: Mapping[str, str]) -> Any:
    """An ``EventHubConsumerClient`` (AMQP over WebSockets, 443) for *settings*."""
    from azure.eventhub import TransportType  # type: ignore[import-not-found,unused-ignore]
    from azure.eventhub.aio import EventHubConsumerClient  # type: ignore[import-not-found,unused-ignore]

    if settings.get("connection_string"):
        kwargs: dict[str, Any] = {"consumer_group": settings["consumer_group"]}
        if settings.get("event_hub_name"):
            kwargs["eventhub_name"] = settings["event_hub_name"]
        return EventHubConsumerClient.from_connection_string(
            settings["connection_string"], transport_type=TransportType.AmqpOverWebsocket, **kwargs
        )
    from azure.identity.aio import ClientSecretCredential  # type: ignore[import-not-found,unused-ignore]

    credential = ClientSecretCredential(settings["tenant_id"], settings["client_id"], settings["client_secret"])
    return EventHubConsumerClient(
        fully_qualified_namespace=settings["namespace"],
        eventhub_name=settings["event_hub_name"],
        consumer_group=settings["consumer_group"],
        credential=credential,
        transport_type=TransportType.AmqpOverWebsocket,
    )


# ---------------------------------------------------------------------------
# Consumer and supervisor
# ---------------------------------------------------------------------------


async def consume(
    integration_id: str,
    settings: Mapping[str, str],
    redis: Any,
    client_factory: Callable[[Mapping[str, str]], Any] = _consumer_client,
) -> None:
    """Read the integration's Event Hub until cancelled, dispatching each batch then checkpointing it."""
    client = client_factory(settings)
    async with client:
        partition_ids = await client.get_partition_ids()
        checkpoints = await load_checkpoints(redis, integration_id, partition_ids)
        starting = {pid: checkpoints.get(pid, "@latest") for pid in partition_ids}

        async def on_batch(partition_context: Any, events: list[Any]) -> None:
            if not events:
                return
            records: list[Mapping[str, Any]] = []
            for event in events:
                records.extend(records_of(event.body_as_str(encoding="UTF-8")))
            await dispatch_records(integration_id, records)
            await save_checkpoint(redis, integration_id, partition_context.partition_id, events[-1].sequence_number)

        await client.receive_batch(
            on_event_batch=on_batch,
            starting_position=starting,
            starting_position_inclusive=False,
            max_batch_size=MAX_BATCH_SIZE,
        )


async def _settings_for(integration: Any) -> dict[str, str]:
    from app.core.security import decrypt_value
    from app.services.vault import reveal_field, reveal_json_field

    extra = await reveal_json_field(integration, "extra_config_enc") if integration.extra_config_enc else {}
    extra = extra if isinstance(extra, Mapping) else {}
    client_id = decrypt_value(integration.client_id_enc) if integration.client_id_enc else _text(extra.get("client_id"))
    client_secret = (await reveal_field(integration, "client_secret_enc")) or _text(extra.get("client_secret"))
    return stream_settings(extra, client_id or "", client_secret or "")


async def wanted_integrations() -> dict[str, Any]:
    """The Defender integrations an enabled assignment listens to (id -> integration)."""
    from sqlalchemy import select

    from app.core.database import async_session
    from app.models.integration import Integration
    from app.services.docusign_connect import provider_assignments

    found: dict[str, Any] = {}
    async with async_session() as db:
        rows = await db.execute(select(Integration).where(Integration.provider == PROVIDER))
        for integration in rows.scalars().all():
            if await provider_assignments(db, str(integration.id), PROVIDER, EVENTS):
                found[str(integration.id)] = integration
    return found


async def _run_leased(integration: Any, redis: Any) -> None:
    """Hold the integration's lease and read its stream while the lease holds."""
    integration_id = str(integration.id)
    if getattr(integration, "relay_group_id", None):
        logger.warning(
            "Defender stream of integration %s not read: routed through a relay group, which AMQP cannot use",
            integration_id,
        )
        return
    token = secrets.token_hex(16)
    if not await take_lease(redis, integration_id, token):
        return
    try:
        try:
            settings = await _settings_for(integration)
        except StreamConfigError as exc:
            logger.warning("Defender stream of integration %s not read: %s", integration_id, exc)
            return
        reader = asyncio.create_task(consume(integration_id, settings, redis), name=f"defender-stream-{integration_id}")
        while not reader.done():
            done, _ = await asyncio.wait({reader}, timeout=LEASE_RENEW_SECONDS)
            if done:
                break
            if not await renew_lease(redis, integration_id, token):
                reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Defender stream of integration %s stopped: %s", integration_id, format_exception_message(exc))
    finally:
        await release_lease(redis, integration_id, token)


async def supervise(stop: asyncio.Event) -> None:
    """Keep one reader per listened-to Defender integration on this replica, until *stop*."""
    from app.core.redis import redis_client

    readers: dict[str, asyncio.Task[None]] = {}
    try:
        while not stop.is_set():
            try:
                wanted = await wanted_integrations()
                redis = await redis_client()
                for integration_id, task in list(readers.items()):
                    if task.done() or integration_id not in wanted:
                        task.cancel()
                        readers.pop(integration_id)
                for integration_id, integration in wanted.items():
                    if integration_id not in readers:
                        readers[integration_id] = asyncio.create_task(_run_leased(integration, redis))
            except Exception as exc:
                logger.warning("Defender stream supervisor pass failed: %s", format_exception_message(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), SUPERVISE_SECONDS)
    finally:
        for task in readers.values():
            task.cancel()
        for task in readers.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


_stop: asyncio.Event | None = None
_task: asyncio.Task[None] | None = None


async def start_defender_stream() -> None:
    """Start the supervisor (app startup)."""
    global _stop, _task
    if _task is not None and not _task.done():
        return
    _stop = asyncio.Event()
    _task = asyncio.create_task(supervise(_stop), name="defender-stream-supervisor")


async def stop_defender_stream() -> None:
    """Stop the supervisor and its readers (app shutdown)."""
    global _stop, _task
    if _stop is not None:
        _stop.set()
    if _task is not None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(_task, 10)
    _stop = None
    _task = None
