# SPDX-License-Identifier: Apache-2.0

"""CTEM signals (#826): record, list and read the changes in exposure that start the loop.

Three ways in, one rule: the API (``api/v1/ctem_signals.py``), the agent tools
(``builtin_tools/ctem_signals.py``) and the integration listeners
(``services/ctem_signal_listeners.py``) all go through :func:`record_signal`,
idempotent on ``(organization_id, recorded_by, dedupe_key)``: a signal seen
again by the same recorder moves its
``last_seen_at``, counts the sighting and keeps its latest evidence; its state,
decision and case are never touched by a new sighting.

Visibility: the signals of the organization are read whole by an
administrator; anyone else reads the signals recorded for them (their API
calls and their agents' tool calls). A listener's signal (recorded by
``system``) is an administrator's read only. Design: ``dev-docs/ctem-signals.md``.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ctem_signal import INTAKES, LENSES, SIGNAL_STATES, SUBJECT_KINDS, TRIGGER_PATHS, CtemSignal

#: Largest page one list answers.
MAX_PAGE = 200
#: The evidence object a signal keeps, serialized, at most.
MAX_EVIDENCE_CHARS = 8000
#: Who records a listener's signal.
SYSTEM_ACTOR = "system"

#: Each path: its label and the lens that usually sees it (a caller may name another).
PATH_CATALOG: dict[str, tuple[str, str]] = {
    "new_vulnerability": ("New vulnerability on a running version", "world"),
    "relevant_threat": ("Relevant new threat or actor", "world"),
    "code_flaw": ("Code or dependency flaw before production", "inside"),
    "proven_exploitable": ("Proven exploitable to a crown jewel", "inside"),
    "new_exposure": ("New exposure", "outside"),
    "stale_asset": ("Stale or orphaned asset", "outside"),
    "surface_drift": ("Attack-surface drift", "outside"),
    "mitigation_regression": ("Mitigation regression", "inside"),
    "control_decay": ("Control or evidence decay", "inside"),
    "credential_leak": ("Credential or secret leak", "world"),
    "third_party_event": ("Third-party or sub-processor event", "world"),
    "blind_spot": ("Blind spot or coverage gap", "inside"),
}


class SignalError(ValueError):
    """A signal refused before anything is written; the message says which field and why."""


@dataclass(frozen=True)
class SignalInput:
    """One change in exposure, as a caller describes it."""

    trigger_path: str
    source_provider: str
    subject_kind: str
    subject_ref: str
    lens: str = ""
    source_external_id: str | None = None
    subject_label: str | None = None
    observed_at: datetime | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    dedupe_key: str | None = None


def _clean(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def default_dedupe_key(trigger_path: str, provider: str, external_id: str | None, kind: str, ref: str) -> str:
    """The same change seen again: one path, one source object (else one subject)."""
    tail = f"{provider}:{external_id}" if external_id else f"{kind}:{ref.lower()}"
    key = f"{trigger_path}:{tail}"
    if len(key) <= 200:
        return key
    return f"{trigger_path}:sha256:{hashlib.sha256(tail.encode('utf-8')).hexdigest()}"


def normalize(inp: SignalInput) -> SignalInput:
    """The input checked and trimmed, its lens and dedupe key filled; :class:`SignalError` otherwise."""
    if inp.trigger_path not in PATH_CATALOG:
        raise SignalError(f"trigger_path must be one of: {', '.join(TRIGGER_PATHS)}")
    lens = inp.lens or PATH_CATALOG[inp.trigger_path][1]
    if lens not in LENSES:
        raise SignalError(f"lens must be one of: {', '.join(LENSES)}")
    if inp.subject_kind not in SUBJECT_KINDS:
        raise SignalError(f"subject_kind must be one of: {', '.join(SUBJECT_KINDS)}")
    provider = _clean(inp.source_provider, 40).lower()
    if not provider:
        raise SignalError("source_provider is required (the product or integration that saw it, or 'manual')")
    ref = _clean(inp.subject_ref, 300)
    if not ref:
        raise SignalError("subject_ref is required (hostname, IP, URL, CPE, CVE id, account, organization...)")
    external_id = _clean(inp.source_external_id, 300) or None
    evidence = dict(inp.evidence or {})
    if len(repr(evidence)) > MAX_EVIDENCE_CHARS:
        raise SignalError(f"evidence is larger than {MAX_EVIDENCE_CHARS} characters: keep the facts and a link")
    observed = inp.observed_at
    if observed is not None and observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    key = _clean(inp.dedupe_key, 200) or default_dedupe_key(
        inp.trigger_path, provider, external_id, inp.subject_kind, ref
    )
    return SignalInput(
        trigger_path=inp.trigger_path,
        source_provider=provider,
        subject_kind=inp.subject_kind,
        subject_ref=ref,
        lens=lens,
        source_external_id=external_id,
        subject_label=_clean(inp.subject_label, 300) or None,
        observed_at=observed,
        evidence=evidence,
        dedupe_key=key,
    )


async def record_signal(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    signal: SignalInput,
    recorded_via: str,
    recorded_by: str,
    now: datetime | None = None,
) -> tuple[CtemSignal, bool]:
    """Record *signal* once per recorder and dedupe key; ``(row, created)``. The caller commits.

    The conflict scope holds the recorder: a caller never reaches, reads or
    overwrites a row another user or a listener recorded under the same key.

    A known key moves ``last_seen_at`` to the later of the stored one and this
    sighting, adds one to ``seen_count`` and keeps this sighting's evidence;
    the first sighting's recorder, state, decision and case stay.
    """
    if recorded_via not in INTAKES:
        raise SignalError(f"recorded_via must be one of: {', '.join(INTAKES)}")
    clean = normalize(signal)
    at = now or datetime.now(UTC)
    observed = clean.observed_at or at
    values = {
        "id": uuid.uuid4(),
        "organization_id": organization_id,
        "trigger_path": clean.trigger_path,
        "lens": clean.lens,
        "source_provider": clean.source_provider,
        "source_external_id": clean.source_external_id,
        "subject_kind": clean.subject_kind,
        "subject_ref": clean.subject_ref,
        "subject_label": clean.subject_label,
        "observed_at": observed,
        "first_seen_at": observed,
        "last_seen_at": observed,
        "seen_count": 1,
        "evidence": clean.evidence,
        "dedupe_key": clean.dedupe_key,
        "state": "new",
        "recorded_via": recorded_via,
        "recorded_by": _clean(recorded_by, 64) or SYSTEM_ACTOR,
        "created_at": at,
        "updated_at": at,
    }
    insert = pg_insert(CtemSignal).values(**values)
    row = CtemSignal.__table__.c
    statement = insert.on_conflict_do_update(
        constraint="uq_ctem_signals_org_recorder_dedupe",
        set_={
            "last_seen_at": func.greatest(row.last_seen_at, insert.excluded.last_seen_at),
            "observed_at": insert.excluded.observed_at,
            "seen_count": row.seen_count + 1,
            "evidence": insert.excluded.evidence,
            "subject_label": func.coalesce(insert.excluded.subject_label, row.subject_label),
            "updated_at": insert.excluded.updated_at,
        },
    ).returning(row.id, row.seen_count)
    signal_id, seen = (await db.execute(statement)).one()
    stored = await db.get(CtemSignal, signal_id, populate_existing=True)
    if stored is None:  # the row was just written in this transaction
        raise RuntimeError(f"CTEM signal {signal_id} not readable after its upsert")
    return stored, seen == 1


def _visible(statement: Any, *, organization_id: uuid.UUID, recorded_by: str | None) -> Any:
    statement = statement.where(CtemSignal.organization_id == organization_id)
    if recorded_by is not None:
        statement = statement.where(CtemSignal.recorded_by == recorded_by)
    return statement


async def list_signals(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    recorded_by: str | None,
    state: str | None = None,
    trigger_path: str | None = None,
    lens: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[CtemSignal], int, dict[str, int]]:
    """One page of signals, newest sighting first, the total and the counts by state.

    ``recorded_by`` ``None`` reads the whole organization (an administrator);
    a user id reads only the signals recorded for that user. The counts are of
    the same filters except ``state``.
    """
    for name, value, allowed in (
        ("state", state, SIGNAL_STATES),
        ("trigger_path", trigger_path, TRIGGER_PATHS),
        ("lens", lens, LENSES),
    ):
        if value is not None and value not in allowed:
            raise SignalError(f"{name} must be one of: {', '.join(allowed)}")
    base = _visible(select(CtemSignal), organization_id=organization_id, recorded_by=recorded_by)
    if trigger_path:
        base = base.where(CtemSignal.trigger_path == trigger_path)
    if lens:
        base = base.where(CtemSignal.lens == lens)
    counted = base.with_only_columns(CtemSignal.state, func.count()).group_by(CtemSignal.state)
    counts = {s: 0 for s in SIGNAL_STATES}
    for value, number in (await db.execute(counted)).all():
        counts[value] = int(number)
    page = base.where(CtemSignal.state == state) if state else base
    total = counts.get(state, 0) if state else sum(counts.values())
    rows = (
        await db.execute(
            page.order_by(CtemSignal.last_seen_at.desc(), CtemSignal.id)
            .limit(max(1, min(int(limit), MAX_PAGE)))
            .offset(max(0, int(offset)))
        )
    ).scalars()
    return list(rows), total, counts


async def get_signal(
    db: AsyncSession, signal_id: uuid.UUID, *, organization_id: uuid.UUID, recorded_by: str | None
) -> CtemSignal | None:
    """One signal of the organization the reader may see, ``None`` otherwise."""
    statement = _visible(
        select(CtemSignal).where(CtemSignal.id == signal_id),
        organization_id=organization_id,
        recorded_by=recorded_by,
    )
    return (await db.execute(statement)).scalar_one_or_none()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def to_dict(signal: CtemSignal) -> dict[str, Any]:
    """The API and tool shape of one signal."""
    return {
        "id": str(signal.id),
        "trigger_path": signal.trigger_path,
        "trigger_path_label": PATH_CATALOG.get(signal.trigger_path, ("", ""))[0],
        "lens": signal.lens,
        "source": {"provider": signal.source_provider, "external_id": signal.source_external_id},
        "subject": {"kind": signal.subject_kind, "ref": signal.subject_ref, "label": signal.subject_label},
        "observed_at": _iso(signal.observed_at),
        "first_seen_at": _iso(signal.first_seen_at),
        "last_seen_at": _iso(signal.last_seen_at),
        "seen_count": signal.seen_count,
        "evidence": signal.evidence or {},
        "dedupe_key": signal.dedupe_key,
        "state": signal.state,
        "decision": signal.decision,
        "case_id": str(signal.case_id) if signal.case_id else None,
        "recorded_via": signal.recorded_via,
    }


def path_catalog() -> list[dict[str, Any]]:
    """The 12 trigger paths, with their default lens and whether a listener maps an event to them."""
    from app.services.ctem_signal_listeners import paths_with_listener

    listened = paths_with_listener()
    return [
        {"id": path, "label": label, "default_lens": lens, "listener": path in listened}
        for path, (label, lens) in PATH_CATALOG.items()
    ]
