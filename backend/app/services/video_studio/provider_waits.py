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

"""A shot whose provider could not take it now waits for it: never failed for it, never asked of another model.

One model per round: a shot is made on the model its author chose. A provider busy - its load (an operation it ended
``RESOURCE_EXHAUSTED`` or ``UNAVAILABLE``), an HTTP 503, no answer at all, a 429 that could not be recorded as a
cooling (``provider_cooling``, the batch's shared state) - left the shot unmade, nothing paid: it goes back to the
queue, ``waiting`` on its row with the time of its next try (:func:`next_wait`: 30 s, then 1, 2, 5 and 10 min, then
every 10 min - or the wait the provider named), for as long as ``provider_cooling.MAX_HOLD`` (24 h, the adapter's
bound). No attempt is spent: the adapter holds it (``orchestration_engine.defer``). Each try is counted on the shot
(``params[KEY]``), so the job line says ``provider busy, next try at 17:45 UTC (in 37 s), 3 tries`` and the record
stays once the provider took the shot. The author may cancel it meanwhile (``jobs.cancel``).
"""

from __future__ import annotations

import math
import zlib
from datetime import UTC, datetime
from typing import Any

#: The params key of a shot's waits for its provider: ``{tries, since, last, next_at, label, model}``.
KEY = "provider_waits"
#: ``provider_state`` of the row of a shot waiting in the queue for a busy provider (``jobs.job_state``: queued, held).
WAITING = "waiting"
#: The wait before each next try, the last one repeated: 30 s, 1, 2, 5, 10 min, then every 10 min.
SCHEDULE_SECONDS = (30.0, 60.0, 120.0, 300.0, 600.0)
#: A shot's own share of its wait added to it (at most this share, and at most :data:`SPREAD_MAX_SECONDS`), read off its
#: id: a batch refused together is asked again spread out, each shot at a time its row can say exactly.
SPREAD_SHARE = 0.1
SPREAD_MAX_SECONDS = 30.0


def _now() -> datetime:
    return datetime.now(UTC)


def next_wait(tries: int, asked: float | None, shot_id: str) -> float:
    """Seconds until the next try of a shot its provider turned away *tries* times: the wait the provider *asked*, else
    the schedule's (:data:`SCHEDULE_SECONDS`), plus the shot's own spread."""
    if asked is not None and asked > 0:
        base = float(asked)
    else:
        base = SCHEDULE_SECONDS[min(max(tries, 1), len(SCHEDULE_SECONDS)) - 1]
    share = (zlib.crc32(str(shot_id).encode()) % 1000) / 1000.0
    return round(base + share * min(base * SPREAD_SHARE, SPREAD_MAX_SECONDS), 3)


def _pending(params: dict) -> dict:
    """The shot's waits still running (a next try due), else nothing: the record of waits a provider ended is done."""
    record = params.get(KEY)
    return record if isinstance(record, dict) and record.get("next_at") else {}


def tries(params: dict) -> int:
    """How many times the provider turned the shot away in the waits still running."""
    return int(_pending(params).get("tries") or 0)


def waited(params: dict, *, reason: str, label: str | None, model: str | None, until: datetime, now: datetime | None = None) -> dict:
    """The shot's waits (``params[KEY]``) with one more try turned away for *reason*, its next one at *until*."""
    moment = now or _now()
    ongoing = _pending(params)
    return {
        "tries": tries(params) + 1,
        "since": ongoing.get("since") or moment.isoformat(),
        "last": reason[:300],
        "label": label,
        "model": model,
        "next_at": until.isoformat(),
    }


def taken(params: dict, now: datetime | None = None) -> bool:
    """The provider took the shot: its waits stay on it as a record (``taken_at``), none of them pending. Whether it had
    any."""
    record = _pending(params)
    if record:
        params[KEY] = {**{k: v for k, v in record.items() if k != "next_at"}, "taken_at": (now or _now()).isoformat()}
    return bool(record)


def row_changes(record: dict, until: datetime) -> dict[str, Any]:
    """The row of a shot waiting in the queue for its busy provider (``jobs.job_state``: queued, held)."""
    who = f"{record.get('label')} ({record.get('model')})" if record.get("model") else str(record.get("label") or "The provider")
    return {
        "stage": f"Waiting for the provider: {who} is busy"[:200],
        "provider_state": WAITING,
        "provider_eta_at": until,
        "progress_at": None,
    }


def line(params: dict | None, until: datetime | None, now: datetime | None = None) -> str:
    """A waiting shot's job line, read at every call: ``provider busy, next try at 17:45 UTC (in 37 s), 3 tries:
    Vertex AI (veo-3.1-generate-001): the service is currently experiencing high load``."""
    record = (params or {}).get(KEY) if isinstance((params or {}).get(KEY), dict) else {}
    tries = int(record.get("tries") or 0)
    said = "provider busy"
    if until is not None:
        left = max(0, math.ceil((until - (now or _now())).total_seconds()))
        said += f", next try at {until.astimezone(UTC):%H:%M} UTC (in {left} s)"
    if tries:
        said += f", {tries} {'try' if tries == 1 else 'tries'}"
    who = f"{record.get('label')} ({record.get('model')})" if record.get("model") else record.get("label")
    last = str(record.get("last") or "")
    if who or last:
        said += f": {who}" if who else ""
        said += f"{' - ' if who else ': '}{last}" if last else ""
    return said
