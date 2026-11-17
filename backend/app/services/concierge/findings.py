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

"""Concierge findings: record a diagnosed pattern once, keep it across cycles (#8259).

A finding's identity is its **fingerprint**, ``sha256`` of the target and the
pattern (kind and normalized key), never of its evidence: the trace and rating
ids change every cycle, the pattern does not. So recording a known pattern
merges the new evidence into the row, bumps ``occurrences`` and ``last_seen_at``
and answers ``new=False`` - the digest counts it as known, not found. A
``resolved`` finding (its proposal applied and kept) that recurs reopens and is
reported once more; a ``dismissed`` one stays silent.

The fingerprint is also the evidence fingerprint of the proposal dedup key
(level 3): one finding, at most one open proposal per type.

The evidence ids reaching this module are already checked against their rows
(``services/concierge/diagnosis.verify_evidence``); this module only stores.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert

from app.models.concierge_finding import (
    FINDING_STATUSES,
    PATTERN_KINDS,
    STATUS_DISMISSED,
    STATUS_OPEN,
    STATUS_RESOLVED,
    TARGET_TYPES,
    ConciergeFinding,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

#: How many ids of each kind a finding keeps, newest first.
MAX_EVIDENCE_IDS = 20
#: The longest hypothesis or suggested experiment kept, in characters.
MAX_TEXT_CHARS = 2_000
MAX_PATTERN_KEY_CHARS = 128
MAX_SLUG_CHARS = 64
EVIDENCE_ID_KINDS = ("trace_ids", "rating_ids", "run_ids")

#: The kinds whose key is an entity id, normalized to its canonical UUID form.
_UUID_KEYED_KINDS = frozenset({"low_criterion", "evaluation_weakness"})
#: The kinds that describe the target as a whole: no key.
_UNKEYED_KINDS = frozenset({"cost_drift", "latency"})
_SLUG_INVALID = re.compile(r"[^a-z0-9_-]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class FindingError(ValueError):
    """An argument the finding store refuses, with a message the caller may read."""


@dataclass(frozen=True, slots=True)
class Evidence:
    """Checked evidence refs: ids as strings, newest first, and counts."""

    trace_ids: tuple[str, ...] = ()
    rating_ids: tuple[str, ...] = ()
    run_ids: tuple[str, ...] = ()
    counts: dict[str, int | float] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "trace_ids": list(self.trace_ids),
            "rating_ids": list(self.rating_ids),
            "run_ids": list(self.run_ids),
            "counts": dict(self.counts),
        }


@dataclass(frozen=True, slots=True)
class RecordResult:
    finding: ConciergeFinding
    #: First time this pattern is recorded, or a resolved one recurring.
    new: bool
    reopened: bool


def normalize_pattern_key(kind: str, key: str | None) -> str:
    """The canonical key of a pattern inside its *kind*; ``FindingError`` when invalid.

    A slug for ``behaviour`` (lowercase ``[a-z0-9_-]``) so the analyst's wording
    does not split one pattern in two; a canonical UUID for an entity key; the
    trimmed text otherwise (a failure cause code, a tool name), without control
    characters.
    """
    if kind not in PATTERN_KINDS:
        raise FindingError(f"pattern_kind must be one of: {', '.join(PATTERN_KINDS)}.")
    raw = (key or "").strip()
    if kind in _UNKEYED_KINDS:
        return ""
    if kind in _UUID_KEYED_KINDS:
        try:
            return str(uuid.UUID(raw))
        except ValueError:
            raise FindingError(f"pattern_key must be the id of the {kind.replace('_', ' ')}'s entity.") from None
    if kind == "behaviour":
        slug = _SLUG_INVALID.sub("-", raw.lower()).strip("-")[:MAX_SLUG_CHARS].strip("-")
        if not slug:
            raise FindingError("pattern_key must name the behaviour as a short slug.")
        return slug
    if kind == "negative_feedback":
        # Optional criterion id: all ratings when absent.
        if not raw:
            return ""
        try:
            return str(uuid.UUID(raw))
        except ValueError:
            raise FindingError("pattern_key must be empty or a feedback criterion id.") from None
    cleaned = _CONTROL.sub("", raw)
    if kind == "failure_cause":
        cleaned = cleaned.lower()
    if not cleaned or len(cleaned) > MAX_PATTERN_KEY_CHARS:
        raise FindingError(f"pattern_key must be 1 to {MAX_PATTERN_KEY_CHARS} characters.")
    return cleaned


def fingerprint(target_type: str, target_id: uuid.UUID, pattern_kind: str, pattern_key: str) -> str:
    """The identity of a pattern on a target, stable across cycles."""
    material = "|".join((target_type, str(target_id), pattern_kind, pattern_key))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def clip_text(value: str | None, limit: int = MAX_TEXT_CHARS) -> str:
    """*value* without control characters (line breaks and tabs kept), clipped."""
    text = (value or "").replace("\r\n", "\n").strip()
    text = "".join(ch for ch in text if ch in "\n\t" or not _CONTROL.match(ch))
    return text if len(text) <= limit else text[:limit].rstrip() + " [...]"


def merge_evidence(stored: dict[str, Any] | None, fresh: Evidence) -> dict[str, Any]:
    """*fresh* ids first, then the stored ones not repeated, capped; counts replaced."""
    stored = stored or {}
    merged: dict[str, Any] = {}
    for kind in EVIDENCE_ID_KINDS:
        seen: set[str] = set()
        ids: list[str] = []
        for value in (*getattr(fresh, kind), *(stored.get(kind) or [])):
            text = str(value)
            if text not in seen:
                seen.add(text)
                ids.append(text)
        merged[kind] = ids[:MAX_EVIDENCE_IDS]
    merged["counts"] = dict(fresh.counts) if fresh.counts else dict(stored.get("counts") or {})
    return merged


def _check_target_type(target_type: str) -> None:
    if target_type not in TARGET_TYPES:
        raise FindingError(f"target_type must be one of: {', '.join(TARGET_TYPES)}.")


async def record_finding(
    db: AsyncSession,
    *,
    target_type: str,
    target_id: uuid.UUID,
    pattern_kind: str,
    pattern_key: str | None,
    hypothesis: str,
    suggested_experiment: str,
    evidence: Evidence,
    content_evidence: bool,
    author_agent_id: uuid.UUID | None,
    now: datetime | None = None,
) -> RecordResult:
    """Record a pattern on a target, once (the caller commits).

    Insert-or-nothing on the fingerprint, then update the row under a row lock:
    two cycles recording the same pattern at once end with one row.
    """
    _check_target_type(target_type)
    key = normalize_pattern_key(pattern_kind, pattern_key)
    finding_print = fingerprint(target_type, target_id, pattern_kind, key)
    now = now or datetime.now(UTC)
    hypothesis_text = clip_text(hypothesis)
    experiment_text = clip_text(suggested_experiment)
    if not hypothesis_text:
        raise FindingError("hypothesis must state what explains the pattern.")

    inserted = await db.execute(
        insert(ConciergeFinding)
        .values(
            id=uuid.uuid4(),
            target_type=target_type,
            target_id=target_id,
            pattern_kind=pattern_kind,
            pattern_key=key,
            fingerprint=finding_print,
            status=STATUS_OPEN,
            hypothesis=hypothesis_text,
            suggested_experiment=experiment_text,
            evidence=merge_evidence(None, evidence),
            content_evidence=content_evidence,
            occurrences=1,
            author_agent_id=author_agent_id,
            first_seen_at=now,
            last_seen_at=now,
            reported_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=[ConciergeFinding.fingerprint])
        .returning(ConciergeFinding.id)
    )
    inserted_id = inserted.scalar_one_or_none()
    finding = (
        await db.execute(
            select(ConciergeFinding)
            .where(ConciergeFinding.fingerprint == finding_print)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if inserted_id is not None:
        return RecordResult(finding=finding, new=True, reopened=False)

    reopened = finding.status == STATUS_RESOLVED
    finding.evidence = merge_evidence(finding.evidence, evidence)
    finding.content_evidence = bool(finding.content_evidence or content_evidence)
    finding.occurrences = (finding.occurrences or 0) + 1
    finding.last_seen_at = now
    if finding.status != STATUS_DISMISSED:
        # The latest reading of the pattern; a dismissed finding keeps the one
        # it was dismissed on.
        finding.hypothesis = hypothesis_text
        finding.suggested_experiment = experiment_text or finding.suggested_experiment
    if reopened:
        finding.status = STATUS_OPEN
        finding.proposal_id = None
        finding.reported_at = now
    await db.flush()
    return RecordResult(finding=finding, new=reopened, reopened=reopened)


async def list_findings(
    db: AsyncSession,
    *,
    target_ids: list[uuid.UUID] | None,
    status: str | None = None,
    limit: int = 20,
) -> list[ConciergeFinding]:
    """Findings on *target_ids* (``None``: every target), latest seen first.

    The caller decides which targets it may read (``services/concierge/access.py``)
    and redacts the content of each row it returns.
    """
    if status is not None and status not in FINDING_STATUSES:
        raise FindingError(f"status must be one of: {', '.join(FINDING_STATUSES)}.")
    query = select(ConciergeFinding)
    if target_ids is not None:
        if not target_ids:
            return []
        query = query.where(ConciergeFinding.target_id.in_(target_ids))
    if status is not None:
        query = query.where(ConciergeFinding.status == status)
    query = query.order_by(ConciergeFinding.last_seen_at.desc(), ConciergeFinding.id).limit(max(1, min(limit, 100)))
    return list((await db.execute(query)).scalars().all())


async def window_counts(
    db: AsyncSession,
    *,
    since: datetime,
    until: datetime | None = None,
    target: tuple[str, uuid.UUID] | None = None,
) -> dict[str, int]:
    """``{"new", "known"}`` findings seen in ``[since, until)``, for a cycle's digest (level 8).

    ``new``: reported in the window (first recorded, or reopened). ``known``:
    seen again in the window but reported before it.
    """
    until = until or datetime.now(UTC)
    clauses = [ConciergeFinding.last_seen_at >= since, ConciergeFinding.last_seen_at < until]
    if target is not None:
        clauses += [ConciergeFinding.target_type == target[0], ConciergeFinding.target_id == target[1]]
    new = func.count().filter(and_(ConciergeFinding.reported_at >= since, ConciergeFinding.reported_at < until))
    known = func.count().filter(ConciergeFinding.reported_at < since)
    row = (await db.execute(select(new, known).where(*clauses))).one()
    return {"new": int(row[0] or 0), "known": int(row[1] or 0)}
