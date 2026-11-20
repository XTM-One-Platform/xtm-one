# SPDX-License-Identifier: Apache-2.0

"""The platform event a connected platform's upgrade raises.

A registered OpenCTI, OpenAEV or OpenCRQ instance reports its version on every
registration heartbeat (``POST /platform/register``). When a platform already
known reports a version other than the one recorded, the heartbeat raises
``integration.version.changed`` after its commit, so an assignment that retests a
fix shipped in a module waits for the upgrade instead of polling.

Not raised on a first registration (no version to compare with) nor when the
version is unchanged. The registration row is locked for the heartbeat
(``_lock_platform_registration``), so the recorded version read before the
write is the one the previous heartbeat committed; the event id is a digest of
the platform and both versions, so two deliveries of one upgrade start one run
per subscriber.

A registered platform and its version belong to no one and every user sees
them on the registrations list: the event is platform-wide
(``platform_events.PLATFORM_WIDE_EVENTS``) and touches no workspace.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

#: The platform event a connected platform's new version raises.
INTEGRATION_VERSION_CHANGED_EVENT = "integration.version.changed"


def version_change(previous: str | None, current: str | None) -> bool:
    """Whether a heartbeat reporting *current* after *previous* is an upgrade (or a downgrade).

    Blank values are no version: a first registration or a platform that sent
    none raises nothing.
    """
    before = (previous or "").strip()
    after = (current or "").strip()
    return bool(before) and bool(after) and before != after


def version_changed_data(
    *,
    provider: str,
    platform_identifier: str,
    integration_id: str | None,
    previous_version: str,
    version: str,
    first_seen_at: datetime | None = None,
) -> dict:
    """The payload of ``integration.version.changed``."""
    seen = first_seen_at or datetime.now(UTC)
    return {
        "provider": provider,
        "platform_identifier": platform_identifier,
        "integration_id": integration_id,
        "previous_version": previous_version.strip(),
        "version": version.strip(),
        "first_seen_at": seen.isoformat(),
        "object_type": "integration",
        "object_id": integration_id or platform_identifier,
        "participant_agent_ids": [],
    }


def version_changed_event_id(platform_identifier: str, previous_version: str, version: str) -> str:
    """The stable event id of one upgrade of one platform (one run per subscriber)."""
    digest = hashlib.sha256(
        f"{platform_identifier}\n{previous_version.strip()}\n{version.strip()}".encode()
    ).hexdigest()[:32]
    return f"integration-version-{digest}"


async def publish_version_change(
    *,
    provider: str,
    platform_identifier: str,
    integration_id: str | None,
    previous_version: str | None,
    version: str | None,
) -> str | None:
    """Raise ``integration.version.changed`` when the version moved; its event id, else ``None``.

    Call it after the heartbeat's commit. Never raises.
    """
    if not version_change(previous_version, version):
        return None
    from app.services.platform_events import publish

    before, after = (previous_version or "").strip(), (version or "").strip()
    return await publish(
        INTEGRATION_VERSION_CHANGED_EVENT,
        version_changed_data(
            provider=provider,
            platform_identifier=platform_identifier,
            integration_id=integration_id,
            previous_version=before,
            version=after,
        ),
        event_id=version_changed_event_id(platform_identifier, before, after),
    )
