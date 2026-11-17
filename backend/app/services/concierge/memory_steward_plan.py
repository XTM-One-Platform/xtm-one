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

"""Memory Steward planning (#8258): what a pass decides for one lane, pure.

The model's answer kept to what is well formed; merges (newest kept),
contradictions (newest wins unless the older is confirmed more often, then
flagged), demotions past the perishable window, promotions of a medium-term
bullet seen in ``PROMOTION_SEEN`` summary versions; nothing a lane already
recorded (undone or flagged) planned twice. ``memory_steward`` reads the lane
and applies the plan.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from app.services.concierge.memory_history import normalized_text, operation_fingerprint, text_digest
from app.services.structured_output import extract_json_text
from app.services.user_memory_entries import PERISHABLE_TTL_SECONDS, bullet_lines

#: Summary versions an observation must survive to be promoted.
PROMOTION_SEEN = 3
#: The persistent tier's own dedup threshold (``persistent_memory``).
DUPLICATE_SIMILARITY = 0.92
#: The largest duplicate group the model's answer may make: one bad or
#: injected answer must not merge a whole window.
MAX_MODEL_GROUP = 5
_MIN_TEXT = 10
_MAX_PROMOTED_TEXT = 500
_MAX_OBSERVATIONS = 200


@dataclass(frozen=True)
class Candidate:
    id: uuid.UUID
    content: str
    created_at: datetime
    embedding: tuple[float, ...] | None = None
    embedding_space: tuple[str | None, int | None] = (None, None)
    confirmations: int = 1


@dataclass(frozen=True)
class Verdict:
    """The model's reading of a lane, indices into the candidate list (0-based)."""

    duplicates: tuple[tuple[int, ...], ...] = ()
    contradictions: tuple[tuple[int, int], ...] = ()
    time_bound: tuple[int, ...] = ()


@dataclass(frozen=True)
class PlannedOp:
    operation: str
    reason: str
    before: tuple[uuid.UUID, ...]
    after: uuid.UUID | None
    fingerprint: str
    promote_text: str | None = None


def _newest(candidates: Iterable[Candidate]) -> Candidate:
    return max(candidates, key=lambda c: (c.created_at, str(c.id)))




def parse_verdict(raw: str, size: int) -> Verdict:
    """The model's answer, kept to what is well formed: 1-based numbers in
    range, groups of distinct entries. Anything else is dropped."""
    try:
        data = json.loads(extract_json_text(raw or ""))
    except (TypeError, ValueError):
        return Verdict()
    if not isinstance(data, dict):
        return Verdict()

    def index(value: object) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value - 1 if 1 <= value <= size else None

    def group(value: object, *, exact: int | None = None) -> tuple[int, ...] | None:
        if not isinstance(value, list):
            return None
        indices = [index(item) for item in value]
        if any(i is None for i in indices) or len(set(indices)) != len(indices) or len(indices) < 2:
            return None
        if exact is not None and len(indices) != exact:
            return None
        return tuple(i for i in indices if i is not None)

    def groups(key: str, **kwargs) -> list[tuple[int, ...]]:
        items = data.get(key)
        if not isinstance(items, list):
            return []
        return [g for g in (group(item, **kwargs) for item in items) if g is not None]

    time_bound = data.get("time_bound")
    return Verdict(
        duplicates=tuple(groups("duplicates")),
        contradictions=tuple((g[0], g[1]) for g in groups("contradictions", exact=2)),
        time_bound=tuple(
            sorted({i for i in (index(v) for v in time_bound) if i is not None})
        )
        if isinstance(time_bound, list)
        else (),
    )


def _duplicate_groups(candidates: Sequence[Candidate], verdict: Verdict) -> list[list[int]]:
    parent = list(range(len(candidates)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def join(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    by_text: dict[str, int] = {}
    spaces: dict[tuple, list[int]] = {}
    for i, candidate in enumerate(candidates):
        first = by_text.setdefault(normalized_text(candidate.content), i)
        if first != i:
            join(first, i)
        if candidate.embedding is not None:
            spaces.setdefault((candidate.embedding_space, len(candidate.embedding)), []).append(i)
    # Cosine per embedding space, one matrix product: never two vectors of
    # different models or sizes.
    for members in spaces.values():
        if len(members) < 2:
            continue
        vectors = np.asarray([candidates[i].embedding for i in members], dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        unit = np.divide(vectors, norms, out=np.zeros_like(vectors), where=norms > 0)
        similar = np.argwhere(np.triu(unit @ unit.T >= DUPLICATE_SIMILARITY, k=1))
        for a, b in similar:
            join(members[a], members[b])
    for members in verdict.duplicates:
        roots = {find(i) for i in members}
        if sum(1 for i in range(len(candidates)) if find(i) in roots) > MAX_MODEL_GROUP:
            continue
        for other in members[1:]:
            join(members[0], other)
    found: dict[int, list[int]] = {}
    for i in range(len(candidates)):
        found.setdefault(find(i), []).append(i)
    return [members for members in found.values() if len(members) > 1]


def plan_entry_operations(
    candidates: Sequence[Candidate],
    verdict: Verdict,
    *,
    now: datetime,
    known_fingerprints: frozenset[str] = frozenset(),
) -> list[PlannedOp]:
    """Merges, then contradictions, then demotions; an entry takes part in one
    operation per pass, and an operation already recorded in the lane (undone
    or flagged) is not planned again."""
    planned: list[PlannedOp] = []
    used: set[int] = set()

    def keep(op: PlannedOp, members: Iterable[int]) -> None:
        if op.fingerprint in known_fingerprints:
            return
        planned.append(op)
        used.update(members)

    for members in _duplicate_groups(candidates, verdict):
        kept = _newest(candidates[i] for i in members)
        others = tuple(sorted((candidates[i].id for i in members if candidates[i] is not kept), key=str))
        keep(
            PlannedOp("merge", "duplicate", others, kept.id, operation_fingerprint("merge", kept.id, *others)),
            members,
        )

    for i, j in verdict.contradictions:
        if i in used or j in used:
            continue
        newer = _newest((candidates[i], candidates[j]))
        older = candidates[j] if newer is candidates[i] else candidates[i]
        if older.confirmations > newer.confirmations:
            op = PlannedOp(
                "flag", "contradiction", (older.id, newer.id), None, operation_fingerprint("flag", older.id, newer.id)
            )
        else:
            op = PlannedOp(
                "remove", "contradiction", (older.id,), newer.id, operation_fingerprint("remove", older.id, newer.id)
            )
        keep(op, (i, j))

    perishable_before = now - timedelta(seconds=PERISHABLE_TTL_SECONDS)
    for i in verdict.time_bound:
        if i in used or candidates[i].created_at >= perishable_before:
            continue
        entry_id = candidates[i].id
        keep(PlannedOp("demote", "stale", (entry_id,), None, operation_fingerprint("demote", entry_id)), (i,))
    return planned


def observe_summary(medium_term: str, observations: dict) -> tuple[dict, list[tuple[str, str]]]:
    """Count each bullet of the medium-term summary once per summary version.

    Returns the updated observations (digests only) and the ``(digest, text)``
    of the bullets now seen ``PROMOTION_SEEN`` times. A bullet absent from the
    summary is forgotten: it did not recur."""
    summary_digest = text_digest(medium_term)
    updated: dict = {}
    ready: list[tuple[str, str]] = []
    for line in bullet_lines(medium_term):
        text = " ".join(line.split())
        if not _MIN_TEXT <= len(text) <= _MAX_PROMOTED_TEXT:
            continue
        key = text_digest(normalized_text(text))
        if key in updated:
            continue
        seen = observations.get(key) if isinstance(observations.get(key), dict) else None
        if seen is None:
            entry = {"seen": 1, "summary": summary_digest}
        elif seen.get("summary") != summary_digest:
            entry = {"seen": int(seen.get("seen", 0)) + 1, "summary": summary_digest}
        else:
            entry = seen
        updated[key] = entry
        if entry["seen"] >= PROMOTION_SEEN:
            ready.append((key, text))
        if len(updated) >= _MAX_OBSERVATIONS:
            break
    return updated, ready


def plan_promotions(
    ready: Sequence[tuple[str, str]],
    lane_texts: Iterable[str],
    *,
    known_fingerprints: frozenset[str] = frozenset(),
) -> list[PlannedOp]:
    present = {normalized_text(text) for text in lane_texts}
    planned = []
    for key, text in ready:
        fingerprint = operation_fingerprint("promote", key)
        if fingerprint in known_fingerprints or normalized_text(text) in present:
            continue
        planned.append(PlannedOp("promote", "recurring", (), None, fingerprint, promote_text=text))
    return planned
