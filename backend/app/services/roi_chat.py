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

"""ROI of chat work: finished deliverables counted automatically, on the low side (#10544).

Decision of 10 Nov 2026 (option A, replaces D2 = B of 6 Nov for chat only): a
deliverable an agent builds in a conversation counts in ROI without any mark,
card, notification or review, and is deliberately undercounted. Automation
counting (``services/roi_records.py``) is unchanged; chat figures are a series
of their own, never inside a fleet's automation count.

Pure module: no database, no I/O. The storage, the recorder hooks, the backfill
and the API build on it.

* **What counts.** One record per finished deliverable (:data:`KINDS`: a
  presentation, a report, a video, a generated document file), keyed on the
  deliverable's own id (:func:`record_key`): every iteration updates the same
  record. Never a message, a quick answer, an email or a lookup.
* **Use replaces confirmation.** A record counts only once it carries a use
  signal (:data:`USE_SIGNALS`: exported, shared, public link, presented, sent
  or attached) or is still there :data:`SETTLE_DAYS` days after its last edit
  (:func:`is_counted`). Deleted before that, it counts nothing.
* **How much, always low.** :func:`estimate` takes the LOW bound of the kind's
  seeded unit (``services.roi.BASELINES_BY_UNIT``), scales it by the
  deliverable's size against the unit's typical size, subtracts the person's
  measured time in the conversation (:func:`measured_user_minutes`: the gaps
  between their messages, each capped at :data:`GAP_CAP_MIN`) plus a
  :data:`MARGIN` margin, caps it per kind (:data:`CAP_MIN`) and floors it at 0.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise

from app.services import roi

#: The deliverable kinds a chat record can carry.
KIND_PRESENTATION = "presentation"
KIND_REPORT = "report"
KIND_VIDEO = "video"
KIND_FILE = "file"
KINDS: tuple[str, ...] = (KIND_PRESENTATION, KIND_REPORT, KIND_VIDEO, KIND_FILE)

#: The seeded unit each kind is valued with (``services.roi.CORPORATE_BASELINES``).
UNIT_BY_KIND: dict[str, str] = {
    KIND_PRESENTATION: "executive_deck_built",
    KIND_REPORT: "consolidated_report_built",
    KIND_VIDEO: "video_produced",
    KIND_FILE: "decision_brief_written",
}

#: The size the seeded minutes of a unit describe (slides, pages, video minutes, pages).
TYPICAL_SIZE: dict[str, float] = {
    "executive_deck_built": 10,
    "consolidated_report_built": 10,
    "video_produced": 2,
    "decision_brief_written": 2,
}

#: What the size of a kind counts, shown next to the figure.
SIZE_LABEL: dict[str, str] = {
    KIND_PRESENTATION: "slides",
    KIND_REPORT: "pages",
    KIND_VIDEO: "minutes of video",
    KIND_FILE: "pages",
}

#: The most one deliverable may count, in minutes (an executive deck: 24 h).
CAP_MIN: dict[str, float] = {
    KIND_PRESENTATION: 24 * 60,
    KIND_REPORT: 24 * 60,
    KIND_VIDEO: 24 * 60,
    KIND_FILE: 8 * 60,
}

#: Signals of use: any one of them makes a record count at once.
SIGNAL_EXPORTED = "exported"
SIGNAL_SHARED = "shared"
SIGNAL_PUBLIC_LINK = "public_link"
SIGNAL_PRESENTED = "presented"
SIGNAL_SENT = "sent"
SIGNAL_ATTACHED = "attached"
USE_SIGNALS: tuple[str, ...] = (
    SIGNAL_EXPORTED,
    SIGNAL_SHARED,
    SIGNAL_PUBLIC_LINK,
    SIGNAL_PRESENTED,
    SIGNAL_SENT,
    SIGNAL_ATTACHED,
)

#: A deliverable still there this many days after its last edit counts without a signal.
SETTLE_DAYS = 7
#: One gap between two of the person's messages counts at most this many minutes.
GAP_CAP_MIN = 10.0
#: The margin added to the person's measured time before it is subtracted.
MARGIN = 0.25

#: The label of the chat series wherever ROI shows it.
SERIES_LABEL = "From chat, estimated"


def record_key(kind: str, deliverable_id: str) -> str:
    """The dedupe key of a chat record: one record per deliverable, whatever the rounds."""
    if kind not in KINDS:
        raise ValueError(f"unknown chat deliverable kind {kind!r}; one of {', '.join(KINDS)}")
    if not str(deliverable_id).strip():
        raise ValueError("a chat record needs the deliverable's own id")
    return f"{kind}:{deliverable_id}"


def is_counted(
    signals: Iterable[str],
    *,
    last_edit_at: datetime,
    now: datetime,
    deleted_at: datetime | None = None,
) -> bool:
    """Whether a deliverable counts: a use signal, or still there :data:`SETTLE_DAYS` after its last edit.

    A deliverable deleted before any signal and before it settled counts nothing; one deleted after
    it counted keeps its record (the work was used).
    """
    used = any(s in USE_SIGNALS for s in signals)
    settled_at = last_edit_at + timedelta(days=SETTLE_DAYS)
    if deleted_at is not None and not used:
        return deleted_at >= settled_at
    return used or now >= settled_at


def measured_user_minutes(message_times: Iterable[datetime], *, gap_cap_min: float = GAP_CAP_MIN) -> float:
    """The person's measured time: the gaps between their consecutive messages, each capped.

    One message alone measures 0 minutes; the cap keeps a pause (a meeting, a night) from counting.
    """
    times = sorted(message_times)
    total = 0.0
    for before, after in pairwise(times):
        total += min((after - before).total_seconds() / 60.0, gap_cap_min)
    return total


@dataclass(frozen=True)
class ChatEstimate:
    """The figures of one chat deliverable, every one in minutes."""

    unit: str
    size: float
    manual_min_low: float
    user_min: float
    subtracted_min: float
    cap_min: float
    counted_min: float


def estimate(
    kind: str,
    size: float,
    *,
    user_min: float,
    baselines: Mapping[str, roi.Baseline] | None = None,
) -> ChatEstimate:
    """The minutes one deliverable counts: low bound by size, minus the person's time plus margin, capped, >= 0."""
    unit = UNIT_BY_KIND[kind]
    catalog = baselines if baselines is not None else roi.BASELINES_BY_UNIT
    base = catalog[unit]
    scale = max(float(size), 0.0) / TYPICAL_SIZE[unit]
    manual_low = base.manual_min_low * scale
    subtracted = max(user_min, 0.0) * (1 + MARGIN)
    cap = CAP_MIN[kind]
    counted = max(min(manual_low - subtracted, cap), 0.0)
    return ChatEstimate(
        unit=unit,
        size=float(size),
        manual_min_low=manual_low,
        user_min=max(user_min, 0.0),
        subtracted_min=subtracted,
        cap_min=cap,
        counted_min=counted,
    )
