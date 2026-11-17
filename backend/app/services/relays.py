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

"""Relay service - credentials, liveness, destinations, routed resources.

Single source of truth for what the relay API router, the relay transport and
the routing surfaces must agree on about a :class:`~app.models.relay.Relay`
and its :class:`~app.models.relay.RelayGroup`:

* whether relays are on at all (:func:`relays_active`);
* credential generation + resolution (the runner's helpers, other prefixes);
* the authoritative online signal (``last_seen_at`` recency, multi-replica);
* the destination pattern grammar shared with the relay binary
  (``apps/relay/src/policy.rs``): ``host``, ``*.suffix``, a CIDR, each with an
  optional ``:port``; ``*`` alone allows everything;
* which resources a group routes (the 409 of a deletion, the detail page);
* the metadata log of relayed requests (batched, never a header or a body);
* JSON serialization for the REST surface.

Deliberately does **not** import :mod:`app.api.deps` - ``deps`` imports the
resolver from here, so the dependency only ever points one way.
"""

from __future__ import annotations

import asyncio
import contextvars
import ipaddress
import logging
import re
import secrets
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import String, case, cast, delete, func, select, update

from app.core.config import settings
from app.core.egress import EgressRoute
from app.core.redaction import redact_known_secrets
from app.core.security import bcrypt_checkpw_async
from app.core.utils import format_exception_message, slugify
from app.models.relay import (
    RELAY_ENROLLMENT_KEY_PREFIX,
    RELAY_KEY_PREFIX,
    Relay,
    RelayEnrollmentKey,
    RelayGroup,
    RelayRequestLog,
)
from app.services.runners import hash_key, key_display_prefix, runner_version_below

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

__all__ = [
    "RELAY_STATUS_OFFLINE",
    "RELAY_STATUS_ONLINE",
    "RELAY_STATUS_REVOKED",
    "DestinationPatternError",
    "RoutedResources",
    "apply_relay_reenrollment",
    "destination_allowed",
    "egress_route_for",
    "flow_relay_group_ids",
    "flush_relay_request_logs",
    "generate_relay_enrollment_key",
    "generate_relay_key",
    "group_relay_counts",
    "group_request_stats",
    "hash_key",
    "integration_egress_route",
    "integration_egress_route_by_id",
    "is_relay_online",
    "key_display_prefix",
    "load_egress_routes",
    "log_relay_request",
    "mcp_server_egress_route",
    "normalize_destination_patterns",
    "online_relay_count",
    "purge_expired_request_logs",
    "purge_expired_request_logs_if_due",
    "relay_status",
    "relay_status_expression",
    "relay_version_below",
    "relays_active",
    "resolve_relay_credential",
    "resolve_relay_enrollment_key",
    "routed_resource_counts",
    "routed_resources",
    "serialize_enrollment_key",
    "serialize_group",
    "serialize_relay",
    "serialize_request_log",
    "unique_group_slug",
]


def relays_active() -> bool:
    """Whether relays are on (``RELAYS_ENABLED``, true by default)."""
    return bool(settings.relays_enabled)


# The relay versions use the runner's MAJOR.YYMMDD.SEQ scheme, so the same
# fail-open comparison applies.
relay_version_below = runner_version_below


# -- Credentials ------------------------------------------------------------


def generate_relay_key() -> str:
    """A durable relay credential: ``rly-<48 hex>``."""
    return RELAY_KEY_PREFIX + secrets.token_hex(24)


def generate_relay_enrollment_key() -> str:
    """An enrollment credential: ``rlenr-<48 hex>``."""
    return RELAY_ENROLLMENT_KEY_PREFIX + secrets.token_hex(24)


async def resolve_relay_credential(token: str, db: AsyncSession) -> Relay | None:
    """Resolve an ``rly-`` credential to its active :class:`Relay`, else ``None``."""
    if not token or not token.startswith(RELAY_KEY_PREFIX):
        return None
    prefix = key_display_prefix(token)
    rows = await db.execute(
        select(Relay).where(Relay.key_prefix == prefix, Relay.is_active.is_(True))
    )
    for relay in rows.scalars().all():
        if await bcrypt_checkpw_async(token.encode(), relay.key_hash.encode()):
            return relay
    return None


async def resolve_relay_enrollment_key(token: str, db: AsyncSession) -> RelayEnrollmentKey | None:
    """Resolve an ``rlenr-`` credential to an active, unexpired, unexhausted key."""
    if not token or not token.startswith(RELAY_ENROLLMENT_KEY_PREFIX):
        return None
    prefix = key_display_prefix(token)
    rows = await db.execute(
        select(RelayEnrollmentKey).where(
            RelayEnrollmentKey.key_prefix == prefix,
            RelayEnrollmentKey.is_active.is_(True),
        )
    )
    for key in rows.scalars().all():
        if not await bcrypt_checkpw_async(token.encode(), key.key_hash.encode()):
            continue
        return key if enrollment_key_usable(key) else None
    return None


def enrollment_key_usable(key: RelayEnrollmentKey) -> bool:
    """Active, unexpired and not used up."""
    if not key.is_active:
        return False
    if key.expires_at is not None:
        expires = key.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires < datetime.now(UTC):
            return False
    return key.max_uses is None or key.use_count < key.max_uses


def apply_relay_reenrollment(
    relay: Relay,
    *,
    key_prefix: str,
    key_hash: str,
    hostname: str | None,
    host_facts: dict | None,
    agent_version: str | None,
    now: datetime,
) -> None:
    """Fold a re-enrollment of an already-enrolled machine into its row.

    Rotates the credential (one machine, one live credential) and refreshes
    what the machine reports; the name an operator gave it is kept.
    """
    relay.key_prefix = key_prefix
    relay.key_hash = key_hash
    relay.is_active = True
    if hostname:
        relay.hostname = hostname
    if host_facts is not None:
        relay.host_facts = host_facts
    if agent_version:
        relay.agent_version = agent_version
    relay.status = RELAY_STATUS_ONLINE
    relay.last_seen_at = now
    relay.enrolled_at = now


# -- Liveness ---------------------------------------------------------------

RELAY_STATUS_REVOKED = "revoked"
RELAY_STATUS_ONLINE = "online"
RELAY_STATUS_OFFLINE = "offline"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def is_relay_online(relay: Relay, now: datetime | None = None) -> bool:
    """Active and seen within ``RELAY_OFFLINE_AFTER_SECONDS``."""
    if not relay.is_active or relay.last_seen_at is None:
        return False
    now = now or datetime.now(UTC)
    delta = (now - _aware(relay.last_seen_at)).total_seconds()
    return delta <= settings.relay_offline_after_seconds


def relay_status(relay: Relay, now: datetime | None = None) -> str:
    if not relay.is_active:
        return RELAY_STATUS_REVOKED
    return RELAY_STATUS_ONLINE if is_relay_online(relay, now) else RELAY_STATUS_OFFLINE


def relay_status_expression(now: datetime | None = None):
    """The SQL twin of :func:`relay_status` for set queries."""
    now = now or datetime.now(UTC)
    online_since = now - timedelta(seconds=settings.relay_offline_after_seconds)
    return case(
        (Relay.is_active.is_(False), RELAY_STATUS_REVOKED),
        (Relay.last_seen_at >= online_since, RELAY_STATUS_ONLINE),
        else_=RELAY_STATUS_OFFLINE,
    )


async def online_relay_count(db: AsyncSession, group_id: uuid.UUID) -> int:
    """How many relays of *group_id* are online right now."""
    online_since = datetime.now(UTC) - timedelta(
        seconds=settings.relay_offline_after_seconds
    )
    return int(
        (
            await db.execute(
                select(func.count(Relay.id)).where(
                    Relay.group_id == group_id,
                    Relay.is_active.is_(True),
                    Relay.last_seen_at >= online_since,
                )
            )
        ).scalar_one()
    )


async def group_relay_counts(
    db: AsyncSession, group_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, int]]:
    """``{group_id: (online, active)}`` for each group, in one query."""
    if not group_ids:
        return {}
    online_since = datetime.now(UTC) - timedelta(
        seconds=settings.relay_offline_after_seconds
    )
    rows = await db.execute(
        select(
            Relay.group_id,
            func.count(Relay.id).filter(Relay.last_seen_at >= online_since),
            func.count(Relay.id),
        )
        .where(Relay.group_id.in_(group_ids), Relay.is_active.is_(True))
        .group_by(Relay.group_id)
    )
    return {gid: (int(online), int(total)) for gid, online, total in rows.all()}


# -- Slugs --------------------------------------------------------------------


async def unique_group_slug(
    db: AsyncSession, name: str, *, exclude_id: uuid.UUID | None = None
) -> str:
    """``slugify(name)``, suffixed ``-2``, ``-3``, ... until it is unused."""
    base = slugify(name)[:180] or "relay-group"
    stmt = select(RelayGroup.slug).where(
        (RelayGroup.slug == base) | RelayGroup.slug.like(f"{base}-%")
    )
    if exclude_id is not None:
        stmt = stmt.where(RelayGroup.id != exclude_id)
    taken = set((await db.execute(stmt)).scalars().all())
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


# -- Destination patterns -----------------------------------------------------
#
# The grammar is shared with ``apps/relay/src/policy.rs`` (the relay's local
# allow-list, the HARD boundary, checked after DNS resolution). On the platform
# side a group's optional ``allowed_destinations`` is defence in depth, checked
# before a request is queued: a CIDR there only matches an IP literal host,
# since the platform never resolves on-premise names.

MAX_DESTINATION_PATTERNS = 200
_HOST_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


class DestinationPatternError(ValueError):
    """A destination pattern the grammar refuses; the message names it."""


@dataclass(frozen=True)
class _Pattern:
    host: str | None = None
    suffix: str | None = None
    network: ipaddress.IPv4Network | ipaddress.IPv6Network | None = None
    port: int | None = None
    any_host: bool = False


def _split_port(raw: str) -> tuple[str, int | None]:
    if raw.startswith("["):
        end = raw.find("]")
        if end < 0:
            raise DestinationPatternError(f"'{raw}': unclosed '['")
        host, rest = raw[1:end], raw[end + 1 :]
        if not rest:
            return host, None
        if not rest.startswith(":"):
            raise DestinationPatternError(f"'{raw}': unexpected text after ']'")
        port_text = rest[1:]
    elif raw.count(":") == 1:
        host, port_text = raw.split(":", 1)
    else:
        return raw, None
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise DestinationPatternError(f"'{raw}': the port must be 1-65535")
    return host, int(port_text)


def _valid_hostname(host: str) -> bool:
    labels = host.split(".")
    return all(_HOST_LABEL_RE.match(label) for label in labels) and len(host) <= 253


def _parse_pattern(raw: str) -> _Pattern:
    text = raw.strip().lower()
    if not text:
        raise DestinationPatternError("an empty destination pattern")
    if text == "*":
        return _Pattern(any_host=True)
    if "://" in text:
        raise DestinationPatternError(
            f"'{raw}': a destination is a host, '*.domain' or a CIDR, not a URL"
        )
    if _looks_like_cidr(text):
        cidr, port = _split_cidr_port(raw, text)
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError as exc:
            raise DestinationPatternError(f"'{raw}': {exc}") from None
        return _Pattern(network=network, port=port)
    host, port = _split_port(text)
    if host == "*":
        return _Pattern(any_host=True, port=port)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        return _Pattern(
            network=ipaddress.ip_network(address, strict=False), port=port
        )
    if host.startswith("*."):
        suffix = host[2:]
        if not _valid_hostname(suffix):
            raise DestinationPatternError(f"'{raw}': invalid domain after '*.'")
        return _Pattern(suffix=suffix, port=port)
    if "*" in host:
        raise DestinationPatternError(
            f"'{raw}': '*' is only allowed alone or as a leading '*.'"
        )
    if not _valid_hostname(host):
        raise DestinationPatternError(f"'{raw}': not a valid host name")
    return _Pattern(host=host, port=port)


def _looks_like_cidr(text: str) -> bool:
    head = text.split("]", 1)[0] if text.startswith("[") else text
    return "/" in head


def _split_cidr_port(raw: str, text: str) -> tuple[str, int | None]:
    if text.startswith("["):
        return _split_port(text)
    # ``10.0.0.0/8:443`` - a port after an IPv4 CIDR. An IPv6 CIDR carries
    # its port only in the bracketed form.
    if text.count(":") == 1:
        cidr, port_text = text.split(":", 1)
        if not port_text.isdigit() or not 0 < int(port_text) < 65536:
            raise DestinationPatternError(f"'{raw}': the port must be 1-65535")
        return cidr, int(port_text)
    return text, None


def normalize_destination_patterns(values: Any) -> list[str] | None:
    """Validate and canonicalise a list of patterns; ``None`` stays ``None``.

    Raises :class:`DestinationPatternError` naming the first refused entry.
    Duplicates are dropped, order kept.
    """
    if values is None:
        return None
    if isinstance(values, str):
        values = [v for v in re.split(r"[\s,]+", values) if v]
    if not isinstance(values, list):
        raise DestinationPatternError("destinations must be a list of patterns")
    if len(values) > MAX_DESTINATION_PATTERNS:
        raise DestinationPatternError(
            f"at most {MAX_DESTINATION_PATTERNS} destination patterns"
        )
    out: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise DestinationPatternError("every destination pattern is a string")
        _parse_pattern(value)
        canonical = value.strip().lower()
        if canonical not in out:
            out.append(canonical)
    return out


def _host_matches(pattern: _Pattern, host: str) -> bool:
    if pattern.any_host:
        return True
    if pattern.network is not None:
        try:
            return ipaddress.ip_address(host) in pattern.network
        except ValueError:
            return False
    if pattern.suffix is not None:
        return host.endswith("." + pattern.suffix)
    return host == pattern.host


def destination_allowed(patterns: list[str] | None, host: str, port: int) -> bool:
    """Whether ``host:port`` matches a pattern; ``None`` = no restriction.

    An empty list allows nothing. A pattern that no longer parses (stored by
    an older release) matches nothing rather than failing the request.
    """
    if patterns is None:
        return True
    host = host.strip().lower().strip("[]").rstrip(".")
    for raw in patterns:
        try:
            pattern = _parse_pattern(raw)
        except DestinationPatternError:
            continue
        if pattern.port is not None and pattern.port != port:
            continue
        if _host_matches(pattern, host):
            return True
    return False


def allows_all(patterns: list[str] | None) -> bool:
    """True when a relay's declared patterns admit every host (``*``)."""
    return any(
        isinstance(p, str) and p.strip().split(":", 1)[0] == "*"
        for p in patterns or []
    )


# -- Routes --------------------------------------------------------------------


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def load_egress_routes(
    db: AsyncSession,
    targets: list[tuple[str, Any, Any]],
) -> dict[str, EgressRoute]:
    """The route of each ``(source_type, source_id, relay_group_id)`` target.

    Keyed by ``str(source_id)``; a target with no group is direct and absent.
    One query for every group. A group that cannot be read still yields a
    route to its id - the request then fails as "no relay online" - because a
    resource bound to a relay group never falls back to direct egress.
    """
    wanted: list[tuple[str, Any, uuid.UUID]] = []
    for source_type, source_id, group_id in targets:
        gid = _as_uuid(group_id)
        if gid is not None:
            wanted.append((source_type, source_id, gid))
    if not wanted:
        return {}
    rows = (
        await db.execute(
            select(RelayGroup.id, RelayGroup.name, RelayGroup.allowed_destinations).where(
                RelayGroup.id.in_({gid for _, _, gid in wanted})
            )
        )
    ).all()
    groups = {row.id: row for row in rows}
    routes: dict[str, EgressRoute] = {}
    for source_type, source_id, gid in wanted:
        group = groups.get(gid)
        allowed = group.allowed_destinations if group is not None else None
        routes[str(source_id)] = EgressRoute(
            group_id=gid,
            group_name=group.name if group is not None else None,
            allowed_destinations=tuple(allowed) if isinstance(allowed, list) else None,
            source_type=source_type,
            source_id=str(source_id),
        )
    return routes


async def egress_route_for(
    db: AsyncSession, source_type: str, source_id: Any, relay_group_id: Any
) -> EgressRoute | None:
    """The route of one resource; ``None`` when it is direct."""
    if _as_uuid(relay_group_id) is None:
        return None
    routes = await load_egress_routes(db, [(source_type, source_id, relay_group_id)])
    return routes.get(str(source_id))


async def integration_egress_route(db: AsyncSession, integration: Any) -> EgressRoute | None:
    """The route of an integration row (``None``: direct)."""
    group_id = getattr(integration, "relay_group_id", None)
    if group_id is None:
        return None
    return await egress_route_for(db, "integration", getattr(integration, "id", None), group_id)


async def integration_egress_route_by_id(
    db: AsyncSession, integration_id: Any
) -> EgressRoute | None:
    """The route of the integration *integration_id* (``None``: direct or unknown)."""
    from app.models.integration import Integration

    iid = _as_uuid(integration_id)
    if iid is None:
        return None
    group_id = await db.scalar(select(Integration.relay_group_id).where(Integration.id == iid))
    return await egress_route_for(db, "integration", iid, group_id)


async def mcp_server_egress_route(db: AsyncSession, server: Any) -> EgressRoute | None:
    """The route of an MCP server row (``None``: direct)."""
    group_id = getattr(server, "relay_group_id", None)
    if group_id is None:
        return None
    return await egress_route_for(db, "mcp_server", getattr(server, "id", None), group_id)


# -- Routed resources ----------------------------------------------------------


@dataclass
class RoutedResources:
    """What routes through one relay group."""

    integrations: list[dict] = field(default_factory=list)
    mcp_servers: list[dict] = field(default_factory=list)
    flows: list[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.integrations) + len(self.mcp_servers) + len(self.flows)

    def as_dict(self) -> dict:
        return {
            "integrations": self.integrations,
            "mcp_servers": self.mcp_servers,
            "flows": self.flows,
            "total": self.total,
        }


def flow_relay_group_ids(graph: Any) -> set[str]:
    """The relay group ids the nodes of a flow graph route through."""
    found: set[str] = set()
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        config = node.get("config")
        if not isinstance(config, dict):
            data = node.get("data")
            config = data.get("config") if isinstance(data, dict) else None
        if isinstance(config, dict):
            value = config.get("relay_group_id")
            if isinstance(value, str) and value.strip():
                found.add(value.strip().lower())
    return found


async def routed_resources(db: AsyncSession, group_id: uuid.UUID) -> RoutedResources:
    """Every integration, MCP server and flow routed through *group_id*.

    Unfiltered by visibility on purpose: a deletion must be refused whoever
    owns what still depends on the group. The caller decides what of it a
    reader may see by name.
    """
    from app.models.flow import Flow
    from app.models.integration import Integration
    from app.models.mcp_server import MCPServer

    out = RoutedResources()
    for row in (
        await db.execute(
            select(
                Integration.id, Integration.name, Integration.provider, Integration.user_id
            ).where(Integration.relay_group_id == group_id)
        )
    ).all():
        out.integrations.append(
            {
                "id": str(row.id),
                "name": row.name,
                "provider": row.provider,
                "user_id": str(row.user_id) if row.user_id else None,
            }
        )
    for row in (
        await db.execute(
            select(MCPServer.id, MCPServer.name, MCPServer.user_id).where(
                MCPServer.relay_group_id == group_id
            )
        )
    ).all():
        out.mcp_servers.append(
            {
                "id": str(row.id),
                "name": row.name,
                "user_id": str(row.user_id) if row.user_id else None,
            }
        )
    needle = str(group_id).lower()
    candidates = await db.execute(
        select(Flow.id, Flow.name, Flow.user_id, Flow.graph).where(
            func.lower(cast(Flow.graph, String)).contains(needle)
        )
    )
    for row in candidates.all():
        if needle in flow_relay_group_ids(row.graph):
            out.flows.append(
                {
                    "id": str(row.id),
                    "name": row.name,
                    "user_id": str(row.user_id) if row.user_id else None,
                }
            )
    return out


async def routed_resource_counts(
    db: AsyncSession, group_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Integrations + MCP servers routed per group (the list cards' count)."""
    from app.models.integration import Integration
    from app.models.mcp_server import MCPServer

    counts: dict[uuid.UUID, int] = {}
    if not group_ids:
        return counts
    for model in (Integration, MCPServer):
        rows = await db.execute(
            select(model.relay_group_id, func.count(model.id))
            .where(model.relay_group_id.in_(group_ids))
            .group_by(model.relay_group_id)
        )
        for gid, n in rows.all():
            counts[gid] = counts.get(gid, 0) + int(n)
    return counts


# -- Request log (metadata only, batched) --------------------------------------

_LOG_BUFFER: deque[dict] = deque(maxlen=10_000)
_LOG_FLUSH_INTERVAL_SECONDS = 5.0
_LOG_FLUSH_BATCH = 500
_log_flusher: asyncio.Task | None = None


def log_relay_request(entry: dict) -> None:
    """Queue one request's metadata; flushed in batches on its own session.

    *entry* carries ``group_id`` and any of ``relay_id``, ``source_type``,
    ``source_id``, ``method``, ``scheme``, ``host``, ``port``, ``path``,
    ``status_code``, ``error_code``, ``bytes_sent``, ``bytes_received``,
    ``duration_ms``. The path must already be stripped of its query string.
    Never raises into the request it describes.
    """
    global _log_flusher
    try:
        # Redacted in the request it describes: the flusher writes outside it.
        entry = redact_known_secrets(entry)
        entry.setdefault("created_at", datetime.now(UTC))
        _LOG_BUFFER.append(entry)
        if _log_flusher is None or _log_flusher.done():
            _log_flusher = asyncio.get_running_loop().create_task(_flush_loop(), context=contextvars.Context())
    except Exception:
        logger.debug("relay request log entry dropped", exc_info=True)


async def _flush_loop() -> None:
    while _LOG_BUFFER:
        await asyncio.sleep(_LOG_FLUSH_INTERVAL_SECONDS)
        await flush_relay_request_logs()


def _clip(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


async def flush_relay_request_logs() -> int:
    """Write the queued entries; returns how many were written."""
    from app.core.database import async_session

    written = 0
    while _LOG_BUFFER:
        batch: list[dict] = []
        while _LOG_BUFFER and len(batch) < _LOG_FLUSH_BATCH:
            batch.append(_LOG_BUFFER.popleft())
        rows = []
        per_relay: dict[uuid.UUID, int] = {}
        for entry in batch:
            group_id = entry.get("group_id")
            if group_id is None:
                continue
            relay_id = entry.get("relay_id")
            rows.append(
                {
                    "id": uuid.uuid4(),
                    "group_id": group_id,
                    "relay_id": relay_id,
                    "source_type": entry.get("source_type"),
                    "source_id": entry.get("source_id"),
                    "method": _clip(entry.get("method") or "GET", 10),
                    "scheme": _clip(entry.get("scheme") or "https", 10),
                    "host": _clip(entry.get("host") or "", 255),
                    "port": entry.get("port"),
                    "path": _clip(entry.get("path") or "/", 1000),
                    "status_code": entry.get("status_code"),
                    "error_code": entry.get("error_code"),
                    "bytes_sent": int(entry.get("bytes_sent") or 0),
                    "bytes_received": int(entry.get("bytes_received") or 0),
                    "duration_ms": entry.get("duration_ms"),
                    "created_at": entry.get("created_at") or datetime.now(UTC),
                }
            )
            if relay_id is not None:
                per_relay[relay_id] = per_relay.get(relay_id, 0) + 1
        if not rows:
            continue
        try:
            async with async_session() as db:
                await db.execute(RelayRequestLog.__table__.insert(), rows)
                for relay_id, n in per_relay.items():
                    await db.execute(
                        update(Relay)
                        .where(Relay.id == relay_id)
                        .values(request_count=Relay.request_count + n)
                    )
                await db.commit()
            written += len(rows)
        except Exception as exc:
            logger.warning(
                "Could not write %d relay request logs: %s",
                len(rows),
                format_exception_message(exc),
            )
    return written


async def group_request_stats(
    db: AsyncSession, group_id: uuid.UUID, *, hours: int = 24
) -> dict:
    """Totals and hourly buckets of a group's relayed requests over *hours*."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    failed = (RelayRequestLog.error_code.is_not(None)) | (RelayRequestLog.status_code >= 500)
    where = (RelayRequestLog.group_id == group_id, RelayRequestLog.created_at >= since)
    totals = (
        await db.execute(
            select(
                func.count(RelayRequestLog.id),
                func.count(RelayRequestLog.id).filter(failed),
                func.coalesce(func.sum(RelayRequestLog.bytes_sent), 0),
                func.coalesce(func.sum(RelayRequestLog.bytes_received), 0),
                func.avg(RelayRequestLog.duration_ms),
            ).where(*where)
        )
    ).one()
    bucket = func.date_trunc("hour", RelayRequestLog.created_at)
    rows = await db.execute(
        select(bucket, func.count(RelayRequestLog.id), func.count(RelayRequestLog.id).filter(failed))
        .where(*where)
        .group_by(bucket)
        .order_by(bucket)
    )
    return {
        "hours": hours,
        "requests": int(totals[0] or 0),
        "errors": int(totals[1] or 0),
        "bytes_sent": int(totals[2] or 0),
        "bytes_received": int(totals[3] or 0),
        "avg_duration_ms": round(float(totals[4]), 1) if totals[4] is not None else None,
        "buckets": [
            {"at": _iso(at), "requests": int(n or 0), "errors": int(e or 0)}
            for at, n, e in rows.all()
        ],
    }


async def purge_expired_request_logs(db: AsyncSession) -> int:
    """Delete request logs past ``RELAY_REQUEST_LOG_RETENTION_DAYS``."""
    days = settings.relay_request_log_retention_days
    if days <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=days)
    result = await db.execute(
        delete(RelayRequestLog).where(RelayRequestLog.created_at < cutoff)
    )
    return int(result.rowcount or 0)


_PURGE_LEASE_KEY = "relays:request_log_purge"
_PURGE_INTERVAL_SECONDS = 3600


async def purge_expired_request_logs_if_due() -> int:
    """:func:`purge_expired_request_logs` at most once an hour, install-wide.

    Asked on every scheduler tick; a Redis lease decides. Runs on a session of
    its own, so the tick's transaction is never held by the delete.
    """
    from app.core.database import async_session
    from app.core.redis import redis_client

    try:
        redis = await redis_client()
        if not await redis.set(_PURGE_LEASE_KEY, "1", nx=True, ex=_PURGE_INTERVAL_SECONDS):
            return 0
    except Exception as exc:
        logger.debug("Relay request log purge lease not taken: %s", exc)
        return 0
    async with async_session() as db:
        deleted = await purge_expired_request_logs(db)
        await db.commit()
    return deleted


# -- Serialization ---------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def serialize_group(
    group: RelayGroup,
    *,
    can_manage: bool,
    online: int = 0,
    total: int = 0,
    routed: int = 0,
) -> dict:
    return {
        "id": str(group.id),
        "user_id": str(group.user_id) if group.user_id else None,
        "name": group.name,
        "slug": group.slug,
        "zone": group.zone,
        "description": group.description,
        "allowed_destinations": group.allowed_destinations,
        "tags": group.tags or [],
        "is_company_managed": bool(group.is_company_managed),
        "shared_group_ids": group.shared_group_ids,
        "shared_group_permissions": group.shared_group_permissions,
        "shared_user_ids": group.shared_user_ids,
        "shared_user_permissions": group.shared_user_permissions,
        "access_restricted": bool(group.access_restricted),
        "relays_online": online,
        "relays_total": total,
        "routed_count": routed,
        "can_manage": can_manage,
        "created_at": _iso(group.created_at),
        "updated_at": _iso(group.updated_at),
    }


def serialize_relay(
    relay: Relay, *, now: datetime | None = None, inflight: int | None = None
) -> dict:
    """Never exposes ``key_hash``."""
    return {
        "id": str(relay.id),
        "group_id": str(relay.group_id),
        "name": relay.name,
        "platform": relay.platform,
        "hostname": relay.hostname,
        "key_prefix": relay.key_prefix,
        "host_facts": relay.host_facts,
        "agent_version": relay.agent_version,
        "protocol_version": relay.protocol_version,
        "capabilities": relay.capabilities or [],
        "declared_destinations": relay.declared_destinations or [],
        "allows_all_destinations": bool(relay.allows_all_destinations),
        "max_concurrent_requests": relay.max_concurrent_requests,
        "status": relay_status(relay, now),
        "online": is_relay_online(relay, now),
        "last_seen_at": _iso(relay.last_seen_at),
        "transport": relay.transport,
        "is_active": relay.is_active,
        "request_count": int(relay.request_count or 0),
        "inflight": inflight,
        "outdated": relay_version_below(
            relay.agent_version, settings.relay_min_supported_version
        ),
        "enrolled_at": _iso(relay.enrolled_at),
        "created_at": _iso(relay.created_at),
        "updated_at": _iso(relay.updated_at),
    }


def serialize_enrollment_key(key: RelayEnrollmentKey) -> dict:
    """Never the raw secret."""
    return {
        "id": str(key.id),
        "group_id": str(key.group_id),
        "name": key.name,
        "key_prefix": key.key_prefix,
        "max_uses": key.max_uses,
        "use_count": key.use_count,
        "is_active": key.is_active,
        "expires_at": _iso(key.expires_at),
        "last_used_at": _iso(key.last_used_at),
        "created_at": _iso(key.created_at),
    }


def serialize_request_log(entry: RelayRequestLog) -> dict:
    return {
        "id": str(entry.id),
        "group_id": str(entry.group_id),
        "relay_id": str(entry.relay_id) if entry.relay_id else None,
        "source_type": entry.source_type,
        "source_id": str(entry.source_id) if entry.source_id else None,
        "method": entry.method,
        "scheme": entry.scheme,
        "host": entry.host,
        "port": entry.port,
        "path": entry.path,
        "status_code": entry.status_code,
        "error_code": entry.error_code,
        "bytes_sent": int(entry.bytes_sent or 0),
        "bytes_received": int(entry.bytes_received or 0),
        "duration_ms": entry.duration_ms,
        "created_at": _iso(entry.created_at),
    }
