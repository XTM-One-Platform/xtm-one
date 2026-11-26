# SPDX-License-Identifier: Apache-2.0

"""CTEM Cases: the per-device history of an upgrade case's running versions (#1694).

``ctem_case_versions`` reads the versions a case's product runs live from the
asset inventory; a live read alone cannot say why five devices below the fix in
the morning are three in the evening. Each recompute therefore compares the
per-device state the inventory answers now (versions, status against the fix,
sources) with the state the case last recorded and, on a change, writes one
``versions`` case event:

- ``to_value``: one line, ``"<n> device(s) changed: <b> below the fix -> <a>"``
  (``baseline`` for the first record of a case);
- ``reason``: one line per changed device (at most ``MAX_REASON_LINES``), with
  its before, after and why (upgraded, downgraded, version became unknown,
  no longer reported by the inventory, newly reported, status changed);
- ``evidence``: JSON ``{"devices": {...}, "changes": [...]}``, the snapshot the
  next recompute compares with. Never served in the history (it can be large):
  ``ctem_cases_view`` hides it and serves ``changes`` through :func:`history_of`.

The latest snapshot also gives the Cases list its asset count when the issues
name no asset (:func:`inventory_asset_count`), so a case never shows a silent 0.
Pure functions except :func:`record`; no version-range engine.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

#: The kind of the case event that records a change of the running versions.
KIND = "versions"
#: The most device lines one event's reason carries; the rest are counted.
MAX_REASON_LINES = 20
#: The most changes one event keeps in its evidence; the rest are counted.
MAX_CHANGES = 200
#: The most history entries a case read serves.
MAX_HISTORY = 10

Device = Mapping[str, Any]


def _versions(device: Device | None) -> list[str]:
    return [str(v) for v in (device or {}).get("versions") or ()]


def _why(before: Device | None, after: Device | None) -> str:
    """Why one device changed, in a few words (pure)."""
    from app.services import ctem_fixed_version as fixed_versions

    if before is None:
        return "newly reported by the inventory"
    if after is None:
        return "no longer reported by the inventory"
    old, new = _versions(before), _versions(after)
    if old != new:
        if new and all(v == "unknown" for v in new):
            via = ", ".join(after.get("sources") or ()) or "an unnamed source"
            return f"version became unknown (reported by {via})"
        ranked = [fixed_versions.compare_versions(max(new), max(old))] if old and new else [None]
        if ranked[0] is not None and ranked[0] > 0:
            return "upgraded"
        if ranked[0] is not None and ranked[0] < 0:
            return "downgraded"
        return "versions changed"
    if (before.get("sources") or []) != (after.get("sources") or []):
        return "reporting sources changed"
    return "status against the fix changed"


def _state(device: Device | None) -> dict[str, Any] | None:
    if device is None:
        return None
    return {
        "versions": _versions(device),
        "status": device.get("status"),
        "sources": [str(s) for s in device.get("sources") or ()],
    }


def diff(before: Mapping[str, Device] | None, after: Mapping[str, Device]) -> list[dict[str, Any]]:
    """The devices whose state moved between two snapshots, sorted by asset id (pure)."""
    before = before or {}
    changes: list[dict[str, Any]] = []
    for asset in sorted(set(before) | set(after)):
        old, new = _state(before.get(asset)), _state(after.get(asset))
        if old == new:
            continue
        changes.append({"asset_id": asset, "before": old, "after": new, "reason": _why(old, new)})
    return changes


def _below(devices: Mapping[str, Device]) -> int:
    return sum(1 for d in devices.values() if d.get("status") == "below_fixed")


def _line(change: Mapping[str, Any]) -> str:
    def show(state: Mapping[str, Any] | None) -> str:
        if state is None:
            return "absent"
        text = "/".join(state.get("versions") or ()) or "unknown"
        return f"{text} ({state['status']})" if state.get("status") else text

    return f"{change['asset_id']}: {show(change['before'])} -> {show(change['after'])}, {change['reason']}"


def event_fields(before: Mapping[str, Device] | None, after: Mapping[str, Device]) -> dict[str, str] | None:
    """The ``to_value``, ``reason`` and ``evidence`` of the event to write, ``None`` when nothing changed (pure).

    *before* ``None`` means the case has no record yet: a ``baseline`` event is written.
    """
    changes = diff(before, after)
    if before is not None and not changes:
        return None
    if before is None:
        summary = f"baseline: {len(after)} device(s), {_below(after)} below the fix"
    else:
        summary = f"{len(changes)} device(s) changed: {_below(before)} below the fix -> {_below(after)}"
    lines = [_line(c) for c in changes[:MAX_REASON_LINES]]
    if len(changes) > MAX_REASON_LINES:
        lines.append(f"... and {len(changes) - MAX_REASON_LINES} more")
    evidence = {
        "devices": {k: _state(v) for k, v in sorted(after.items())},
        "changes": changes[:MAX_CHANGES],
        "more_changes": max(0, len(changes) - MAX_CHANGES),
    }
    return {
        "to_value": summary[:300],
        "reason": "\n".join(lines) if before is not None else "",
        "evidence": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
    }


def _evidence(event: Any) -> dict[str, Any] | None:
    raw = getattr(event, "evidence", None)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def latest(events: Iterable[Any]) -> Any | None:
    """The newest ``versions`` event of a case's events (any order), ``None`` when none (pure)."""
    found = [e for e in events if getattr(e, "kind", None) == KIND and getattr(e, "at", None) is not None]
    return max(found, key=lambda e: e.at) if found else None


def snapshot_of(event: Any | None) -> dict[str, Any] | None:
    """The device snapshot a ``versions`` event recorded, ``None`` when unreadable (pure)."""
    data = _evidence(event) if event is not None else None
    devices = (data or {}).get("devices")
    return devices if isinstance(devices, dict) else None


def history_of(events: Iterable[Any], limit: int = MAX_HISTORY) -> list[dict[str, Any]]:
    """The newest ``versions`` events first: ``{at, summary, changes, more_changes}`` (pure)."""
    found = sorted(
        (e for e in events if getattr(e, "kind", None) == KIND and getattr(e, "at", None) is not None),
        key=lambda e: e.at,
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    for event in found[: max(0, limit)]:
        data = _evidence(event) or {}
        raw = data.get("changes")
        changes: list[Any] = raw if isinstance(raw, list) else []
        out.append(
            {
                "at": event.at.isoformat() if isinstance(event.at, datetime) else event.at,
                "summary": event.to_value,
                "changes": changes[:MAX_REASON_LINES],
                "more_changes": max(0, len(changes) - MAX_REASON_LINES) + int(data.get("more_changes") or 0),
            }
        )
    return out


def inventory_asset_count(events: Iterable[Any]) -> dict[str, Any] | None:
    """``{asset_count, as_of}`` from the latest recorded snapshot, ``None`` when none (pure)."""
    event = latest(events)
    devices = snapshot_of(event)
    if event is None or devices is None:
        return None
    return {"asset_count": len(devices), "as_of": event.at.isoformat() if isinstance(event.at, datetime) else None}


#: Why a case with no asset on its issues has no count.
NO_COUNT_NOT_UPGRADE = "the visible issues name no asset and the fix names no product the inventory matches"
NO_COUNT_NOT_READ = "the visible issues name no asset and the inventory was not read for this case yet"
NO_COUNT_RESTRICTED = (
    "some open issues of the case are not readable with their details for this reader and the "
    "inventory holds no count for it: unknown, not zero"
)


def with_asset_count(row: dict[str, Any], events: Iterable[Any]) -> dict[str, Any]:
    """*row* (a case list row) with ``asset_count_source`` and, when its issues name no asset, the inventory count.

    ``attached`` (#2395): neither the issues nor the inventory count an asset, and the case's
    validations attached some (``ctem_case_assets``): their count.

    ``issues``: the issues name the assets (unchanged count). ``inventory``: the latest
    snapshot counts the devices running the product (``asset_count_as_of``). ``unknown``:
    ``asset_count`` is ``None`` with ``asset_count_reason``; never a silent 0. A row whose
    ``aggregates_restricted`` is true (an open member read without its details) never counts
    from the issues: the inventory count when there is one, else ``unknown``.
    """
    restricted = bool(row.get("aggregates_restricted"))
    if not restricted and int(row.get("asset_count") or 0) > 0:
        row["asset_count_source"] = "issues"
        return row
    found = inventory_asset_count(events) if row.get("remediation_kind") == "upgrade" else None
    if found is not None:
        row["asset_count"] = found["asset_count"]
        row["asset_count_source"] = "inventory"
        row["asset_count_as_of"] = found["as_of"]
        return row
    from app.services.ctem_case_assets import ATTACHED_SOURCE, attached_assets

    # The hosts a validation ran on, recorded on the case (#2395): never a silent 0 while one ran.
    attached = attached_assets(row.get("validations"))
    if attached and not restricted:
        row["asset_count"] = len(attached)
        row["asset_count_source"] = ATTACHED_SOURCE
        return row
    row["asset_count"] = None
    row["asset_count_source"] = "unknown"
    if restricted:
        row["asset_count_reason"] = NO_COUNT_RESTRICTED
    else:
        row["asset_count_reason"] = (
            NO_COUNT_NOT_UPGRADE if row.get("remediation_kind") != "upgrade" else NO_COUNT_NOT_READ
        )
    return row


def with_live_asset_count(row: dict[str, Any], running: Mapping[str, Any] | None, *, at: datetime) -> dict[str, Any]:
    """*row* (a case read) counted from the live *running* versions the same read answers (#2225).

    The case read reads ``ctem_case_versions.for_case`` live and serves it as ``running_versions``;
    its ``asset_count`` (the distinct assets running the product) wins over a recorded snapshot or an
    ``unknown`` count, so the aggregate and ``running_versions`` never disagree. A count from the
    issues stays; an unread or empty inventory changes nothing. ``asset_count_as_of`` is *at*.
    """
    if row.get("asset_count_source") == "issues" or not running:
        return row
    count = running.get("asset_count")
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return row
    row["asset_count"] = count
    row["asset_count_source"] = "inventory"
    row["asset_count_as_of"] = at.isoformat()
    row.pop("asset_count_reason", None)
    return row


async def record(db: Any, case: Any, answer: Mapping[str, Any] | None, events: Iterable[Any], *, at: datetime) -> bool:
    """Write the ``versions`` event of *case* when its devices moved since the last record.

    *answer* is ``ctem_case_versions.for_case(..., with_devices=True)``; ``None`` (not an
    upgrade, no product, inventory unreadable) writes nothing: an unread inventory is
    never recorded as every device gone. Answers whether an event was added.
    """
    if not answer or not isinstance(answer.get("devices"), dict):
        return False
    previous = latest(events)
    before = snapshot_of(previous) if previous is not None else None
    if previous is not None and before is None:
        before = {}
    fields = event_fields(before, answer["devices"])
    if fields is None:
        return False
    from app.models.ctem_case import CtemCaseEvent
    from app.services.ctem_cases import SYSTEM_ACTOR

    db.add(CtemCaseEvent(case_id=case.id, actor=SYSTEM_ACTOR, kind=KIND, at=at, **fields))
    return True
