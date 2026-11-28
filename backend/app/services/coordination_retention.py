# SPDX-License-Identifier: Apache-2.0

"""Retention of the coordination tables: work items, shared state, worksets.

The windows an administrator sets in Settings (``platform_settings.coordination_retention``,
a JSON object keyed by :data:`RETENTION_KEYS`) win over the environment defaults of
``app.core.config`` (``WORK_ITEM_RESOLVED_RETENTION_DAYS`` and the rest), which stay the
values a platform starts with. A window is a number of days from 0 to 3650; 0 keeps forever,
the convention of the trace and file retentions.

Every window counts from the row's last update (``updated_at``), except the
abandoned-open auto-cancel, which counts from the item's last activity, and the
assignment run window, which counts from the run's completion (a run has no
``updated_at``), and the three CTEM signal windows, which count from the signal's
last sighting (``last_seen_at``, #2581, #2726).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.core.config import settings

#: Lower bound of a stored window: 0 keeps forever.
MIN_COORDINATION_RETENTION_DAYS = 0
#: Upper bound of a stored window: ten years.
MAX_COORDINATION_RETENTION_DAYS = 3650

#: Each window an administrator may set, and the environment setting that is its default.
RETENTION_KEYS: dict[str, str] = {
    # Closed (done, cancelled) work items, deleted N days after their last update.
    "work_item_resolved_days": "work_item_resolved_retention_days",
    # Open work items nobody picked up, auto-cancelled N days after their last activity.
    "work_item_abandoned_days": "work_item_abandoned_after_days",
    # Resolved shared-state entries, deleted N days after their last update.
    "shared_state_resolved_days": "shared_state_resolved_retention_days",
    # List items an archive_key write moved into an archive entry.
    "shared_state_archive_days": "shared_state_archive_retention_days",
    # Resolved worksets, deleted with their documents N days after their resolution.
    "workset_resolved_days": "workset_resolved_retention_days",
    # Finished assignment runs (Operations > Executions), deleted N days after their
    # last activity (completed_at, else created_at): services/run_retention.py.
    "assignment_run_days": "assignment_run_retention_days",
    # CTEM signals the scope gate discarded, deleted N days after their last sighting
    # (``last_seen_at``), a person's dismissal and a signal in a case never: services/ctem_signal_retention.py.
    "ctem_signal_discarded_days": "ctem_signal_discarded_retention_days",
    # Watchlist CTEM signals, deleted N days after their last sighting (#2581).
    "ctem_signal_watchlist_days": "ctem_signal_watchlist_retention_days",
    # Unmatched (``to_validate``) CTEM signals, deleted N days after their last sighting;
    # also the bound of their profile-change re-decision (#2726).
    "ctem_signal_unmatched_days": "ctem_signal_unmatched_retention_days",
}


def _env_default(key: str) -> int:
    value = getattr(settings, RETENTION_KEYS[key], 0)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def coordination_retention_days(stored: Mapping[str, Any] | None) -> dict[str, int]:
    """The effective window of every key: the stored value when valid, else the environment default.

    A stored value that is not a whole number within the bounds is ignored, never clamped,
    so a corrupt row falls back to the default instead of deleting on a guessed window.
    """
    out: dict[str, int] = {}
    for key in RETENTION_KEYS:
        raw = stored.get(key) if isinstance(stored, Mapping) else None
        if (
            isinstance(raw, int)
            and not isinstance(raw, bool)
            and MIN_COORDINATION_RETENTION_DAYS <= raw <= MAX_COORDINATION_RETENTION_DAYS
        ):
            out[key] = raw
        else:
            out[key] = _env_default(key)
    return out


async def unmatched_signal_retention_days(db: Any) -> int:
    """The effective unmatched CTEM signal window (#2726), read on *db*; the default on a read failure."""
    from app.api.v1.platform_settings_helpers import peek_platform_settings

    try:
        ps = await peek_platform_settings(db)
        stored = getattr(ps, "coordination_retention", None) if ps else None
    except Exception:
        stored = None
    return coordination_retention_days(stored if isinstance(stored, Mapping) else None)["ctem_signal_unmatched_days"]


async def effective_coordination_retention() -> dict[str, int]:
    """The effective windows read from the platform settings row (never creates it).

    A read failure answers the environment defaults: a sweep never stops on it.
    """
    from app.api.v1.platform_settings_helpers import peek_platform_settings
    from app.core.database import async_session

    try:
        async with async_session() as db:
            ps = await peek_platform_settings(db)
    except Exception:
        return coordination_retention_days(None)
    return coordination_retention_days(getattr(ps, "coordination_retention", None) if ps else None)
