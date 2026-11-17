"""The lab's relay rows: two relays and a request log on the sample relay group (#10763).

Started by ``lab.py`` (``seed_relay_requests``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the backend answers: no API
writes a relay (enrollment needs a running relay binary) or a ``relay_request_logs``
row (the relay transport writes them), so this script writes them through the
platform's own models. Arguments: the relay group, the user who enrolled the relays,
and the JSON file the ids are written to.

* two relays (``relays``): one ``online`` with a 40+ character name, one ``offline``;
  their key hash is a lab placeholder that no key verifies, never a credential;
* :data:`ROW_COUNT` request rows (``relay_request_logs``), metadata only (no header,
  no body), newest first over the last hours: every error code of the frontend's
  ``RELAY_ERROR_LABELS``, 2xx, 4xx and 5xx statuses, a 90+ character host, durations
  of 1,000 ms and more, the source types ``integration``, ``mcp_server``, ``flow``,
  ``test`` and none, and rows with no relay (no relay answered).

So the group's Activity tab shows "Showing 1-50 of 120", and its Outcome filter, host
search, relay filter and source parameter narrow real rows.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: The relays: (name, status, platform, hostname).
RELAYS: tuple[tuple[str, str, str, str], ...] = (
    ("Lab sample relay in the Paris datacenter OT network zone", "online", "linux", "relay-paris-ot-01"),
    ("Lab sample relay", "offline", "windows", "relay-win-02"),
)

#: The error codes of ``RELAY_ERROR_LABELS`` (frontend/src/pages/relay-group-detail/relay-labels.ts).
ERROR_CODES: tuple[str, ...] = (
    "destination_denied", "dns_failed", "connect_failed", "tls_failed", "timeout",
    "too_large", "no_relay_online", "relay_disconnected", "protocol_error",
)
#: Errors raised before any relay took the request: the row carries no relay.
NO_RELAY_ERRORS = frozenset({"no_relay_online", "destination_denied"})

#: (host, port, path) destinations; the third host is over 90 characters.
DESTINATIONS: tuple[tuple[str, int | None, str], ...] = (
    ("splunk.corp.local", 8089, "/services/search/jobs"),
    ("opencti.corp.local", None, "/graphql"),
    (
        "very-long-internal-service-name.observability.monitoring.eu-west-3.datacenter-paris.corp.local",
        8443, "/api/v2/metrics/query",
    ),
    ("10.12.0.45", 9200, "/_search"),
    ("ldap-gateway.ot.corp.local", 443, "/v1/users"),
)

#: The source types, ``None`` included (a request with no recorded source).
SOURCE_TYPES: tuple[str | None, ...] = ("integration", "mcp_server", "flow", "test", None)

#: Statuses of the rows the destination answered: 2xx, 4xx and 5xx.
STATUSES: tuple[int, ...] = (200, 200, 201, 204, 200, 404, 401, 200, 503, 500, 200, 429)

METHODS: tuple[str, ...] = ("GET", "POST", "GET", "PUT", "GET", "DELETE")

ROW_COUNT = 120


def rows(relays: list[uuid.UUID]) -> list[dict[str, object]]:
    """The request rows, oldest first: plain values, no ORM object."""
    now = datetime.now(UTC)
    out: list[dict[str, object]] = []
    for index in range(ROW_COUNT):
        host, port, path = DESTINATIONS[index % len(DESTINATIONS)]
        error = ERROR_CODES[index // 4 % len(ERROR_CODES)] if index % 4 == 3 else None
        status = None if error else STATUSES[index % len(STATUSES)]
        if error in NO_RELAY_ERRORS:
            relay = None
        else:
            relay = relays[index % len(relays)]
        # Every seventh row takes a second or more; the rest a few tens of milliseconds.
        duration = 1000 + index * 17 if index % 7 == 0 else 20 + index * 3 % 400
        if error == "timeout":
            duration = 30000
        out.append({
            "relay_id": relay,
            "source_type": SOURCE_TYPES[index % len(SOURCE_TYPES)],
            "method": METHODS[index % len(METHODS)],
            "host": host,
            "port": port,
            "path": path,
            "status_code": status,
            "error_code": error,
            "bytes_sent": 0 if error else 180 + index * 11,
            "bytes_received": 0 if error else 2048 + index * 997,
            "duration_ms": duration,
            "created_at": now - timedelta(minutes=(ROW_COUNT - index) * 3),
        })
    return out


async def seed(group_id: str, user_id: str) -> dict[str, object]:
    """Create the relays and the request rows; answer their ids and the row count."""
    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.relay import Relay, RelayRequestLog

    group, owner = uuid.UUID(group_id), uuid.UUID(user_id)
    now = datetime.now(UTC)
    async with async_session() as db:
        relays: list[Relay] = []
        for index, (name, status, platform, hostname) in enumerate(RELAYS):
            relay = Relay(
                group_id=group, enrolled_by_user_id=owner, name=name, platform=platform,
                hostname=hostname, key_prefix=f"rly-lab{index}",
                # No key hashes to this: the relay can never authenticate.
                key_hash="lab-seed-placeholder-not-a-key-hash",
                agent_version="0.0.0-lab", protocol_version=1, capabilities=["http"],
                declared_destinations=["*.corp.local", "10.0.0.0/8"], status=status,
                last_seen_at=now if status == "online" else now - timedelta(days=2),
                transport="ws" if status == "online" else None, enrolled_at=now - timedelta(days=7),
            )
            db.add(relay)
            relays.append(relay)
        await db.flush()
        ids = [relay.id for relay in relays]
        values = rows(ids)
        for value in values:
            db.add(RelayRequestLog(group_id=group, scheme="https", **value))
        for relay in relays:
            relay.request_count = sum(1 for value in values if value["relay_id"] == relay.id)
        await db.commit()
    await engine.dispose()
    return {"relays": [str(relay) for relay in ids], "requests": len(values)}


def main() -> None:
    group_id, user_id, out = sys.argv[1:4]
    Path(out).write_text(json.dumps(asyncio.run(seed(group_id, user_id))))


if __name__ == "__main__":
    main()
