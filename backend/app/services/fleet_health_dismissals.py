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

"""Dismissed fleet health findings (#12014).

A finding of :mod:`app.services.fleet_health_signals` the fleet's owner has
knowingly accepted (a cost spike where cost is no concern, a schedule silent on
purpose) is dismissed with a reason, so the fleet reads healthy again and the
weekly health check stops spending a model call on it.

Stored on the fleet (``Fleet.health_dismissals``), never in its documentation:
a list of records ``{id, kind, scope, reason, by, by_name, at, until}``.

* ``scope`` ``finding``: ``id`` is the finding's id, a digest of the fleet, the
  kind and the WHOLE evidence set, so new evidence raises the finding again.
* ``scope`` ``kind``: ``id`` is the kind; every finding of that kind is
  dismissed on this fleet, whatever its evidence.
* ``until`` (ISO-8601 or ``None``): the dismissal lapses then; a lapsed record
  no longer hides anything and is dropped at the next write.

Pure functions: the caller loads the fleet, checks the permission, audits and
commits.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

#: The longest reason kept (characters).
MAX_REASON = 500
#: The most records a fleet keeps; a dismissal past it is refused.
MAX_DISMISSALS = 200
SCOPE_FINDING = "finding"
SCOPE_KIND = "kind"


class DismissalRefusedError(ValueError):
    """A dismissal or restore that cannot be applied; nothing is written."""


def finding_id(fleet_id: object, kind: str, evidence: Iterable[object]) -> str:
    """The stable id of a finding: its kind and a digest of the fleet and the whole evidence set."""
    body = "|".join([str(fleet_id), kind, *sorted(str(e) for e in evidence)])
    return f"{kind}:{hashlib.sha256(body.encode()).hexdigest()[:10]}"


def _parse_time(raw: object) -> datetime | None:
    if raw in (None, ""):
        return None
    try:
        value = datetime.fromisoformat(str(raw).strip())
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def is_live(record: dict[str, Any], now: datetime) -> bool:
    """Whether a record still hides findings (no ``until``, or one in the future)."""
    until = _parse_time(record.get("until"))
    return until is None or until > now


def live_records(raw: object, now: datetime) -> list[dict[str, Any]]:
    """The well-formed records of *raw* that have not lapsed."""
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict) and r.get("id") and is_live(r, now)]


def _matches(record: dict[str, Any], kind: str, fid: str) -> bool:
    if record.get("scope") == SCOPE_KIND:
        return record.get("id") == kind
    return record.get("id") == fid


def split_findings(findings: Sequence[Any], raw: object, now: datetime) -> tuple[list[Any], list[dict[str, Any]]]:
    """``(active findings, live records)``: a finding a live record matches is left out.

    Each record answered carries ``hides``, the number of current findings it hides.
    """
    records = live_records(raw, now)
    active: list[Any] = []
    hides = [0] * len(records)
    for f in findings:
        hit = next((i for i, r in enumerate(records) if _matches(r, f.kind, f.id)), None)
        if hit is None:
            active.append(f)
        else:
            hides[hit] += 1
    return active, [{**r, "hides": n} for r, n in zip(records, hides, strict=True)]


def dismiss(
    raw: object,
    ids: Sequence[str],
    *,
    findings: Sequence[Any],
    known_kinds: Iterable[str],
    reason: str,
    by: str,
    by_name: str = "",
    until: object = None,
    now: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(records to store, records written)`` for dismissing *ids*.

    An id is a current finding's id (that finding) or a known kind (every
    finding of the kind). Refused: no id, no reason, an unknown id, an
    ``until`` that is not a future ISO-8601 time, more than :data:`MAX_DISMISSALS`.
    A finding or kind already dismissed is dismissed again (new reason, by, at).
    """
    reason = str(reason or "").strip()[:MAX_REASON]
    if not ids:
        raise DismissalRefusedError("ids is required: the finding ids get_fleet_health shows, or a kind.")
    if not reason:
        raise DismissalRefusedError("reason is required: say why the finding needs nothing.")
    lapse = _parse_time(until)
    if until not in (None, "") and (lapse is None or lapse <= now):
        raise DismissalRefusedError("until must be a future ISO-8601 time, e.g. 2026-12-31.")
    by_id = {f.id: f for f in findings}
    kinds = set(known_kinds)
    unknown = [i for i in ids if i not in by_id and i not in kinds]
    if unknown:
        raise DismissalRefusedError(
            f"No such health finding on this fleet: {', '.join(unknown)}. Pass an id get_fleet_health shows, or a kind."
        )
    stored = live_records(raw, now)
    written: list[dict[str, Any]] = []
    for i in dict.fromkeys(ids):
        f = by_id.get(i)
        record = {
            "id": i,
            "kind": f.kind if f is not None else i,
            "scope": SCOPE_FINDING if f is not None else SCOPE_KIND,
            "summary": f.summary if f is not None else "",
            "reason": reason,
            "by": by,
            "by_name": by_name,
            "at": now.isoformat(timespec="seconds"),
            "until": lapse.isoformat(timespec="seconds") if lapse else None,
        }
        stored = [r for r in stored if r.get("id") != i]
        stored.append(record)
        written.append(record)
    if len(stored) > MAX_DISMISSALS:
        raise DismissalRefusedError(f"At most {MAX_DISMISSALS} dismissals per fleet: restore some first.")
    return stored, written


def restore(raw: object, ids: Sequence[str] | None, now: datetime) -> tuple[list[dict[str, Any]], int]:
    """``(records to store, how many restored)``: *ids* (every one when ``None``) brought back.

    Refused when an id names no live dismissal.
    """
    stored = live_records(raw, now)
    if ids is None:
        return [], len(stored)
    known = {r.get("id") for r in stored}
    unknown = [i for i in ids if i not in known]
    if not ids or unknown:
        raise DismissalRefusedError(
            f"No such dismissed health finding on this fleet: {', '.join(unknown) or '(none)'}."
        )
    wanted = set(ids)
    kept = [r for r in stored if r.get("id") not in wanted]
    return kept, len(stored) - len(kept)
