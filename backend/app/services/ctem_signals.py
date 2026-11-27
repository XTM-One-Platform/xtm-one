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

#: The platform events a signal raises (#1409; registered in ``platform_events.PLATFORM_EVENTS``):
#: its first sighting, and a later change of the scope gate's verdict. A re-sighting raises nothing.
SIGNAL_RECORDED = "ctem_signal.recorded"
SIGNAL_TRIAGED = "ctem_signal.triaged"
SIGNAL_EVENTS: tuple[str, ...] = (SIGNAL_RECORDED, SIGNAL_TRIAGED)
#: The gate verdicts ``ctem_signal.triaged`` reports (``new`` and ``case_opened`` are no verdict).
VERDICTS: tuple[str, ...] = ("confirmed", "to_validate", "watchlist", "discarded")

#: Each path: its label and the lens that usually sees it (a caller may name another).
PATH_CATALOG: dict[str, tuple[str, str]] = {
    "new_vulnerability": ("New vulnerability on a running version", "world"),
    "relevant_threat": ("Relevant new threat or actor", "world"),
    "code_flaw": ("Code or dependency flaw before production", "inside"),
    "proven_exploitable": ("Proven exploitable to a primary asset", "inside"),
    "new_exposure": ("New exposure", "outside"),
    "stale_asset": ("Stale or orphaned asset", "outside"),
    "surface_drift": ("Attack-surface drift", "outside"),
    "mitigation_regression": ("Mitigation regression", "inside"),
    "control_decay": ("Control or evidence decay", "inside"),
    "credential_leak": ("Credential or secret leak", "world"),
    "third_party_event": ("Third-party or sub-processor event", "world"),
    "blind_spot": ("Blind spot or coverage gap", "inside"),
}

#: Product names of the providers a signal or a validation names by key (#1782); any other key reads as written.
PROVIDER_NAMES: dict[str, str] = {
    "openaev": "OpenAEV",
    "opencti": "OpenCTI",
    "opencrq": "OpenCRQ",
    "aikido": "Aikido Security",
    "manual": "Manual",
}


def provider_name(key: str | None) -> str:
    """The name a person reads for a provider key: the product's name when known, else the key."""
    value = (key or "").strip()
    return PROVIDER_NAMES.get(value.lower(), value)


class SignalError(ValueError):
    """A signal refused before anything is written; the message says which field and why."""


class SignalIntakePausedError(SignalError):
    """An administrator's purge of every signal holds intake paused (#2528): nothing is recorded."""


#: What a source is answered while a purge holds intake paused (#2528).
INTAKE_PAUSED_DETAIL = "Signal intake is paused while an administrator purges every signal; nothing was recorded."


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


#: Evidence keys that may hold a credential or secret value (CTEM-29): never stored as given.
SECRET_KEYS: frozenset[str] = frozenset(
    {"password", "secret", "token", "api_key", "apikey", "private_key", "credential", "credentials", "hash"}
)


def fingerprint(value: object) -> str:
    """The SHA-256 fingerprint a secret is kept as, enough to see the same secret twice."""
    return "sha256:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def mask_secrets(evidence: dict[str, Any]) -> dict[str, Any]:
    """*evidence* with every secret value replaced by its fingerprint, at any depth."""

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for key, item in value.items():
                if str(key).lower() in SECRET_KEYS and item not in (None, "") and not isinstance(item, dict | list):
                    out[f"{key}_fingerprint"] = fingerprint(item)
                else:
                    out[key] = walk(item)
            return out
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    masked: dict[str, Any] = walk(evidence)
    return masked


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
    if inp.trigger_path == "credential_leak":
        # CTEM-29: a leak signal keeps a fingerprint and the source link, never the secret.
        evidence = mask_secrets(evidence)
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


#: The evidence keys a sighting never takes away from the stored signal (#2543): the PIR matches,
#: the relevance floor and the set of sources that saw it. Every other key is the latest sighting's.
PIR_EVIDENCE = "pir"
PIR_FLOOR_EVIDENCE = "pir_floor_met"
SOURCES_EVIDENCE = "xtm_sources"


def _pir_score(entry: Any) -> float:
    score = entry.get("score_at_delivery") if isinstance(entry, dict) else None
    return float(score) if isinstance(score, int | float) and not isinstance(score, bool) else float("-inf")


def _merge_pirs(stored: Any, incoming: Any) -> list[Any]:
    """The PIR matches of both sightings, one per PIR id, the highest ``score_at_delivery`` kept."""
    merged: dict[str, Any] = {}
    loose: list[Any] = []
    for entry in [*(stored if isinstance(stored, list) else []), *(incoming if isinstance(incoming, list) else [])]:
        pir_id = entry.get("pir_id") if isinstance(entry, dict) else None
        if not pir_id:
            if entry not in loose:
                loose.append(entry)
            continue
        kept = merged.get(str(pir_id))
        if kept is None or _pir_score(entry) >= _pir_score(kept):
            merged[str(pir_id)] = entry
    return [*merged.values(), *loose]


def merge_evidence(stored: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """The evidence a known signal keeps after a new sighting (#2543). Pure.

    The latest sighting's facts win, except what a sighting without PIR data must
    not erase: the PIR matches are merged by PIR id (highest score kept), the
    relevance floor is never downgraded (stored or incoming), and every source key
    that saw the signal is kept in ``xtm_sources``. Trimmed to :data:`MAX_EVIDENCE_CHARS`
    (the lowest PIR scores first, then the incoming evidence alone with the floor).
    """
    before = dict(stored or {})
    merged = dict(incoming)
    if PIR_EVIDENCE in before or PIR_EVIDENCE in incoming:
        pirs = _merge_pirs(before.get(PIR_EVIDENCE), incoming.get(PIR_EVIDENCE))
        if pirs:
            merged[PIR_EVIDENCE] = pirs
    if PIR_FLOOR_EVIDENCE in before or PIR_FLOOR_EVIDENCE in incoming:
        merged[PIR_FLOOR_EVIDENCE] = before.get(PIR_FLOOR_EVIDENCE) is True or incoming.get(PIR_FLOOR_EVIDENCE) is True
    sources: list[str] = []
    for value in (
        *(before.get(SOURCES_EVIDENCE) or []),
        before.get(SOURCE_KEY_EVIDENCE),
        *(incoming.get(SOURCES_EVIDENCE) or []),
        incoming.get(SOURCE_KEY_EVIDENCE),
    ):
        if isinstance(value, str) and value and value not in sources:
            sources.append(value)
    if len(sources) > 1:
        merged[SOURCES_EVIDENCE] = sources
    else:
        merged.pop(SOURCES_EVIDENCE, None)
    kept = merged.get(PIR_EVIDENCE)
    while len(repr(merged)) > MAX_EVIDENCE_CHARS and isinstance(kept, list) and len(kept) > 1:
        kept = sorted(kept, key=_pir_score, reverse=True)[:-1]
        merged[PIR_EVIDENCE] = kept
    if len(repr(merged)) > MAX_EVIDENCE_CHARS:
        merged = dict(incoming)
        if PIR_FLOOR_EVIDENCE in before or PIR_FLOOR_EVIDENCE in incoming:
            merged[PIR_FLOOR_EVIDENCE] = (
                before.get(PIR_FLOOR_EVIDENCE) is True or incoming.get(PIR_FLOOR_EVIDENCE) is True
            )
    return merged


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
    sighting, adds one to ``seen_count`` and merges this sighting's evidence into
    the stored one (:func:`merge_evidence`, #2543: a sighting without PIR data
    never erases the PIR matches nor downgrades the floor); the first sighting's
    recorder, state, decision and case stay.
    """
    if recorded_via not in INTAKES:
        raise SignalError(f"recorded_via must be one of: {', '.join(INTAKES)}")
    from app.services import ctem_signal_purge as purge

    # #2528: every source records through here, so one check pauses them all during a purge;
    # the purge job's own resync step records through its bypass.
    if not purge.RESYNC_BYPASS.get() and await purge.intake_paused():
        raise SignalIntakePausedError(INTAKE_PAUSED_DETAIL)
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
    known = (
        select(CtemSignal.evidence)
        .where(
            CtemSignal.organization_id == organization_id,
            CtemSignal.recorded_by == values["recorded_by"],
            CtemSignal.dedupe_key == clean.dedupe_key,
        )
        .with_for_update()
    )
    found: Any = None
    for _attempt in range(2):
        # #2543: the stored evidence is read under a row lock and merged, never replaced.
        stored = (await db.execute(known)).first()
        if stored is None:
            # A first sighting; a writer that inserts the same key meanwhile sends this one to the merge.
            found = (
                await db.execute(
                    insert.on_conflict_do_nothing(constraint="uq_ctem_signals_org_recorder_dedupe").returning(
                        row.id, row.seen_count
                    )
                )
            ).first()
            if found is not None:
                break
            continue
        statement = insert.on_conflict_do_update(
            constraint="uq_ctem_signals_org_recorder_dedupe",
            set_={
                "last_seen_at": func.greatest(row.last_seen_at, insert.excluded.last_seen_at),
                "observed_at": insert.excluded.observed_at,
                "seen_count": row.seen_count + 1,
                "evidence": merge_evidence(stored[0], clean.evidence),
                "subject_label": func.coalesce(insert.excluded.subject_label, row.subject_label),
                "updated_at": insert.excluded.updated_at,
            },
        ).returning(row.id, row.seen_count)
        found = (await db.execute(statement)).one()
        break
    if found is None:
        raise RuntimeError(f"CTEM signal {clean.dedupe_key} neither inserted nor found after a concurrent insert")
    signal_id, seen = found
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
    case_id: uuid.UUID | None = None,
    source_provider: str | None = None,
    since: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[CtemSignal], int, dict[str, int]]:
    """One page of signals, newest sighting first, the total and the counts by state.

    ``recorded_by`` ``None`` reads the whole organization (an administrator);
    a user id reads only the signals recorded for that user. The counts are of
    the same filters except ``state``. ``source_provider`` keeps one provider
    (case-insensitive, as stored lower-case); ``since`` keeps the signals last
    seen at or after it (#1782).
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
    if case_id is not None:
        # The signals that opened or joined one case (CTEM-03-UI #892: its "Opened by" card).
        base = base.where(CtemSignal.case_id == case_id)
    if source_provider and source_provider.strip():
        base = base.where(CtemSignal.source_provider == source_provider.strip().lower())
    if since is not None:
        base = base.where(CtemSignal.last_seen_at >= since)
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


async def counts_by_path(
    db: AsyncSession, *, organization_id: uuid.UUID, recorded_by: str | None
) -> dict[str, dict[str, int]]:
    """The signals the reader may see, counted by trigger path then state (CTEM-30, #937).

    Same visibility as :func:`list_signals`, so a path never counts a signal
    the reader cannot open. Every path and state is present, zeros included.
    """
    statement = _visible(
        select(CtemSignal.trigger_path, CtemSignal.state, func.count()),
        organization_id=organization_id,
        recorded_by=recorded_by,
    ).group_by(CtemSignal.trigger_path, CtemSignal.state)
    counts = {path: {s: 0 for s in SIGNAL_STATES} for path in TRIGGER_PATHS}
    for path, state, number in (await db.execute(statement)).all():
        if path in counts and state in counts[path]:
            counts[path][state] = int(number)
    return counts


async def activity_by_path(
    db: AsyncSession, *, organization_id: uuid.UUID, recorded_by: str | None, since: datetime
) -> dict[str, dict[str, Any]]:
    """Per trigger path, the signals the reader may see last seen since *since* and the newest sighting (#1189).

    Same visibility as :func:`counts_by_path`. Every path is present:
    ``{"count_30d": 0, "last_signal_at": None}`` for a path with nothing.
    """
    out: dict[str, dict[str, Any]] = {p: {"count_30d": 0, "last_signal_at": None} for p in TRIGGER_PATHS}
    recent = _visible(
        select(CtemSignal.trigger_path, func.count()).where(CtemSignal.last_seen_at >= since),
        organization_id=organization_id,
        recorded_by=recorded_by,
    ).group_by(CtemSignal.trigger_path)
    for path, number in (await db.execute(recent)).all():
        if path in out:
            out[path]["count_30d"] = int(number)
    newest = _visible(
        select(CtemSignal.trigger_path, func.max(CtemSignal.last_seen_at)),
        organization_id=organization_id,
        recorded_by=recorded_by,
    ).group_by(CtemSignal.trigger_path)
    for path, last in (await db.execute(newest)).all():
        if path in out:
            out[path]["last_signal_at"] = _iso(last)
    return out


#: The evidence key a system recording point stamps with its source key (``ctem_signal_sources.source_key``, #2436),
#: so the signal sources view counts each source of a path apart. Stored, never reconciled by the gate.
SOURCE_KEY_EVIDENCE = "xtm_source_key"


async def activity_by_source(
    db: AsyncSession, *, organization_id: uuid.UUID, recorded_by: str | None, since: datetime
) -> dict[str, dict[str, dict[str, Any]]]:
    """Per trigger path and source key, the signals last seen since *since* and the newest sighting (#2436).

    Same visibility as :func:`activity_by_path`. A signal recorded before its
    source stamped :data:`SOURCE_KEY_EVIDENCE` counts under its ``source_provider``;
    a signal several sources saw (``xtm_sources``, #2543) counts under each of them.
    """
    own = func.coalesce(CtemSignal.evidence[SOURCE_KEY_EVIDENCE].as_string(), CtemSignal.source_provider)
    every = func.coalesce(CtemSignal.evidence[SOURCES_EVIDENCE], func.json_build_array(own))
    seen = _visible(
        select(
            CtemSignal.trigger_path.label("trigger_path"),
            func.json_array_elements_text(every).label("source"),
            CtemSignal.last_seen_at.label("last_seen_at"),
        ),
        organization_id=organization_id,
        recorded_by=recorded_by,
    ).subquery()
    statement = select(
        seen.c.trigger_path,
        seen.c.source,
        func.count().filter(seen.c.last_seen_at >= since),
        func.max(seen.c.last_seen_at),
    ).group_by(seen.c.trigger_path, seen.c.source)
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for path, source, number, last in (await db.execute(statement)).all():
        out.setdefault(path, {})[str(source)] = {"count_30d": int(number or 0), "last_signal_at": _iso(last)}
    return out


def source_activity(
    path: str, source: dict[str, Any], activity: dict[str, dict[str, dict[str, Any]]]
) -> dict[str, Any]:
    """The activity of one source of *path* (:func:`activity_by_source`). Pure.

    Its own key first; an unnamed source also takes the signals of its provider
    recorded before #2436, when it is the only unnamed source of that provider on the path.
    """
    from app.services.ctem_signal_sources import PATH_SOURCES

    seen = activity.get(path) or {}
    own = seen.get(source["key"]) or {}
    count = int(own.get("count_30d") or 0)
    last = own.get("last_signal_at")
    provider = source["provider"]
    alone = sum(1 for s in PATH_SOURCES.get(path, ()) if s.provider == provider and not s.name) == 1
    if not source.get("name") and alone:
        legacy = seen.get(provider) or {}
        count += int(legacy.get("count_30d") or 0)
        other = legacy.get("last_signal_at")
        if other and (last is None or other > last):
            last = other
    return {"count_30d": count, "last_signal_at": last}


def _available_here(source: dict[str, Any]) -> bool:
    """A source that is or could be on here: on, off, taken off the path, not set up or in error (#2506)."""
    return bool(
        source.get("bound")
        or source.get("off")
        or source.get("excluded")
        or source.get("available")
        or source.get("not_set_up")
        or source.get("error_count")
    )


async def observed_by_case(
    db: AsyncSession, case_ids: list[uuid.UUID], *, organization_id: uuid.UUID, recorded_by: str | None
) -> dict[uuid.UUID, list[datetime]]:
    """When the signals that opened or joined each case were observed, the reader's own only (CTEM-30, #937)."""
    out: dict[uuid.UUID, list[datetime]] = {cid: [] for cid in case_ids}
    if not case_ids:
        return out
    statement = _visible(
        select(CtemSignal.case_id, CtemSignal.observed_at).where(CtemSignal.case_id.in_(case_ids)),
        organization_id=organization_id,
        recorded_by=recorded_by,
    )
    for case_id, observed in (await db.execute(statement)).all():
        out.setdefault(case_id, []).append(observed)
    return out


async def select_signals(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    recorded_by: str | None,
    ids: list[uuid.UUID] | None = None,
    state: str | None = None,
    trigger_path: str | None = None,
    limit: int = 2000,
) -> list[CtemSignal]:
    """The signals the reader may see, by id or by filter, newest sighting first (#1505 triage)."""
    statement = _visible(select(CtemSignal), organization_id=organization_id, recorded_by=recorded_by)
    if ids is not None:
        statement = statement.where(CtemSignal.id.in_(ids))
    if state:
        statement = statement.where(CtemSignal.state == state)
    if trigger_path:
        statement = statement.where(CtemSignal.trigger_path == trigger_path)
    statement = statement.order_by(CtemSignal.last_seen_at.desc(), CtemSignal.id).limit(max(1, int(limit)))
    return list((await db.execute(statement)).scalars())


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


#: The evidence key that names the OpenCTI object a signal came from when its ``source_external_id`` is a
#: composite dedupe key (path 11: ``<OpenCTI id>:<tracked organization id>``, one signal per organization).
OPENCTI_ID_EVIDENCE = "opencti_id"


def module_entity_id(provider: str | None, external_id: str | None, evidence: Any = None) -> str | None:
    """The id of the signal's object in its source module, the one an "Open in <Module>" link opens.

    ``source_external_id`` is a dedupe key: path 11 stores ``<OpenCTI id>:<tracked organization id>``, so
    the OpenCTI id is the evidence ``opencti_id`` when recorded, else the part before the first colon of an
    OpenCTI key (an OpenCTI internal id or a STIX id never holds one). Any other key is returned as it is.
    """
    value = (external_id or "").strip() or None
    if (provider or "").lower() != "opencti":
        return value
    recorded = evidence.get(OPENCTI_ID_EVIDENCE) if isinstance(evidence, dict) else None
    if isinstance(recorded, str) and recorded.strip():
        return recorded.strip()
    if value and ":" in value:
        return value.split(":", 1)[0].strip() or None
    return value


def _source_shape(signal: CtemSignal) -> dict[str, Any]:
    """The signal's source; ``entity_id`` only when the module object's id is not the key itself."""
    shape: dict[str, Any] = {
        "provider": signal.source_provider,
        "name": provider_name(signal.source_provider),
        "external_id": signal.source_external_id,
    }
    entity_id = module_entity_id(signal.source_provider, signal.source_external_id, signal.evidence)
    if entity_id != signal.source_external_id:
        shape["entity_id"] = entity_id
    return shape


def to_dict(signal: CtemSignal) -> dict[str, Any]:
    """The API and tool shape of one signal."""
    return {
        "id": str(signal.id),
        "trigger_path": signal.trigger_path,
        "trigger_path_label": PATH_CATALOG.get(signal.trigger_path, ("", ""))[0],
        "lens": signal.lens,
        "source": _source_shape(signal),
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


def verdict_of(state: str | None) -> str | None:
    """The gate verdict a signal state is, ``None`` for ``new`` and ``case_opened``."""
    return state if state in VERDICTS else None


#: The EPSS score from which a vulnerability counts as a jump (the former trigger's ``min_epss_score``).
EPSS_JUMP = 0.5


def routing_facts(evidence: Any) -> dict[str, bool]:
    """The yes/no facts a fleet routes a signal on (#2434), read from its evidence; no value travels.

    ``kev`` (CISA KEV), ``epss_high`` (EPSS at or above :data:`EPSS_JUMP`), ``sighted``
    (an OpenCTI sighting of the vulnerability), ``software_named`` (the delivery names the
    software or asset it starts from) and ``pir_flagged`` (an organization PIR flags it).
    """
    ev = evidence if isinstance(evidence, dict) else {}
    epss = ev.get("epss_score")
    try:
        epss_high = not isinstance(epss, bool) and float(epss) >= EPSS_JUMP
    except (TypeError, ValueError):
        epss_high = False
    kev = ev.get("cisa_kev")
    return {
        "kev": kev is True or (isinstance(kev, str) and kev.strip().lower() == "true"),
        "epss_high": epss_high,
        "sighted": ev.get("stix_type") == "sighting",
        "software_named": bool(ev.get("software")),
        "pir_flagged": bool(ev.get("pir")),
    }


def signal_event_data(
    signal: CtemSignal, *, verdict: str | None = None, triaged: bool = False, previous_verdict: str | None = None
) -> dict[str, Any]:
    """The payload of a signal event: the signal id and its routing, nothing else (#1409).

    Subject, evidence and decision stay on the signal, read the signal with list_ctem_signals signal_id
    under the run's own permissions. The event reaches only subscribers whose
    run-as user reads the signal (``platform_events.can_read_object``).
    *verdict* is the gate's verdict when the state already moved on (a case opened
    from a confirmed signal); else the signal's own. A ``triaged`` payload always
    carries ``previous_verdict`` (``null``: the signal had none yet).
    """
    evidence = getattr(signal, "evidence", None) or {}
    data: dict[str, Any] = {
        "signal_id": str(signal.id),
        "trigger_path": signal.trigger_path,
        "lens": signal.lens,
        "source_provider": signal.source_provider,
        "subject_kind": signal.subject_kind,
        "verdict": verdict or verdict_of(signal.state),
        "case_id": str(signal.case_id) if signal.case_id else None,
        # #1678: the relevance floor a seeded run filters on (true only for a relevant threat
        # whose PIR score at delivery reached the policy threshold); no evidence value travels.
        "pir_floor_met": evidence.get("pir_floor_met") is True,
        # #2434: the routing facts a fleet filters on instead of a provisioned trigger of its own.
        "facts": routing_facts(evidence),
        "object_type": "ctem_signal",
        "object_id": str(signal.id),
        "participant_agent_ids": [],
    }
    if triaged:
        data["previous_verdict"] = previous_verdict
    return data


def signal_event_id(name: str, signal: CtemSignal, marker: str) -> str:
    """A stable event id: one per signal for ``recorded``, one per verdict change for ``triaged``."""
    digest = hashlib.sha256(f"{name}\n{signal.id}\n{marker}".encode()).hexdigest()[:32]
    return f"ctem-signal-{digest}"


async def user_reads_signal(db: AsyncSession, user_id: str, signal_id: Any) -> bool:
    """Whether *user_id* reads the signal *signal_id*: an administrator or a CTEM view or decide
    right (``services/ctem_access.py``), re-read at delivery time.

    The signal API's rule (#2441): no CTEM without a right, a person's own signals included.
    """
    from app.models.user import User
    from app.services import ctem_access

    try:
        uid = uuid.UUID(str(user_id))
        sid = uuid.UUID(str(signal_id))
    except (ValueError, TypeError):
        return False
    recorded_by = (await db.execute(select(CtemSignal.recorded_by).where(CtemSignal.id == sid))).scalar_one_or_none()
    if recorded_by is None:
        return False
    user = await db.get(User, uid)
    if bool(getattr(user, "is_admin", False)):
        return True
    return user is not None and await ctem_access.group_right(db, uid) is not None


#: Why a path that is on records nothing (#2642), the codes ``silent_reason`` answers.
SILENT_REASONS: dict[str, str] = {
    "never_evaluated": "The inventory has not been evaluated yet; it is evaluated daily and after a purge",
    "no_asset_past_threshold": "No asset is past the threshold and every synced asset still has its sync",
    "upstream_sync_failed": "An inventory sync failed: the assets it owns are not judged stale until it reads again",
    "inventory_truncated": "No evaluated asset is past the threshold, but the inventory is larger than one evaluation reads",
    "listener_never_fired": "No source of this path has sent an event yet",
}


def silent_reason(row: dict[str, Any], evaluation: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Why a path that is on recorded no signal in 30 days, or ``None`` when it did or is not on (#2642).

    *evaluation* is the last outcome of the path's own evaluation (``ctem_inventory_staleness.last_outcome``
    for path 6): it tells a sync failure from an inventory with nothing past the threshold.
    """
    from app.services.ctem_signal_sources import LIVE_PATH_STATUSES

    if row.get("status") not in LIVE_PATH_STATUSES or int(row.get("count_30d") or 0) > 0:
        return None
    if row["id"] == "stale_asset":
        if not evaluation:
            code = "never_evaluated"
        elif int(evaluation.get("withheld_sync_failed") or 0) or int(evaluation.get("failed_syncs") or 0):
            code = "upstream_sync_failed"
        elif int(evaluation.get("truncated") or 0):
            code = "inventory_truncated"
        else:
            code = "no_asset_past_threshold"
        out: dict[str, Any] = {"code": code, "detail": SILENT_REASONS[code]}
        if evaluation:
            keys = ("at", "evaluated", "truncated", "stale_after_days", "failed_syncs", "withheld_sync_failed")
            out["evaluation"] = {k: evaluation[k] for k in keys if k in evaluation}
        return out
    if not any(s.get("last_signal_at") for s in row.get("sources") or []) and not row.get("last_signal_at"):
        return {"code": "listener_never_fired", "detail": SILENT_REASONS["listener_never_fired"]}
    return None


def path_catalog(
    counts: dict[str, dict[str, int]] | None = None,
    *,
    bound: set[tuple[str, str]] | None = None,
    activity: dict[str, dict[str, Any]] | None = None,
    by_source: dict[str, dict[str, dict[str, Any]]] | None = None,
    webhooks: dict[str, list[dict[str, Any]]] | None = None,
    evaluations: dict[str, dict[str, Any] | None] | None = None,
) -> list[dict[str, Any]]:
    """The 12 trigger paths, with their default lens and whether a listener maps an event to them.

    With *counts* (:func:`counts_by_path`), each path also carries its signals
    by state and ``configured``: a listener feeds it or it holds a signal. A
    path that is neither reads "not configured", never 0 (CTEM-30, #937).

    Every path also carries ``description`` and its ``sources``
    (``ctem_signal_sources``), each ``bound`` per *bound*, and ``status``
    (``ctem_signal_sources.path_status``): ``error``, ``connected``,
    ``not_set_up``, ``off``, ``not_connected`` or ``agents_api`` (no source:
    agents and the API only, never connected). With *activity* (:func:`activity_by_path`):
    ``count_30d`` and ``last_signal_at`` (#1189). With *by_source* (:func:`activity_by_source`),
    each source carries its own ``count_30d`` and ``last_signal_at`` (#2436). With *webhooks*
    (``ctem_webhook_sources.path_sources_of``), each path also lists its signed webhook sources (#2506).
    Every path carries ``sources_enabled`` and ``sources_available``: its sources on, and its sources
    that could be on here (a source of a provider with no integration here never counts, #2506).
    With *activity*, a path that is on with no signal in 30 days carries ``silent_reason``
    (:func:`silent_reason`, #2642), read with its own evaluation in *evaluations*.
    """
    from app.services.ctem_signal_listeners import paths_with_listener
    from app.services.ctem_signal_sources import PATH_CAPABILITIES, PATH_DESCRIPTIONS, path_sources, path_status

    listened = paths_with_listener()
    out: list[dict[str, Any]] = []
    for path, (label, lens) in PATH_CATALOG.items():
        sources = path_sources(path, bound) + [dict(w) for w in (webhooks or {}).get(path, [])]
        if by_source is not None:
            for source in sources:
                source.update(source_activity(path, source, by_source))
        row: dict[str, Any] = {
            "id": path,
            "label": label,
            "default_lens": lens,
            "listener": path in listened,
            "description": PATH_DESCRIPTIONS.get(path, ""),
            "sources": sources,
            "status": path_status(sources),
            "capabilities": list(PATH_CAPABILITIES.get(path, ())),
            "sources_enabled": sum(1 for s in sources if s.get("bound")),
            "sources_available": sum(1 for s in sources if _available_here(s)),
        }
        if counts is not None:
            by_state = counts.get(path) or {s: 0 for s in SIGNAL_STATES}
            row["counts"] = by_state
            row["total"] = sum(by_state.values())
            row["configured"] = row["listener"] or row["total"] > 0
        if activity is not None:
            seen = activity.get(path) or {}
            row["count_30d"] = int(seen.get("count_30d") or 0)
            row["last_signal_at"] = seen.get("last_signal_at")
            row["silent_reason"] = silent_reason(row, (evaluations or {}).get(path))
        out.append(row)
    return out
