# SPDX-License-Identifier: Apache-2.0

"""CTEM Cases as one viewer sees them: the source products are the only judge (#11613).

Cases are shared by the organization and store no source content
(``models/ctem_case.py``). What a viewer sees is decided here, at read time,
from the viewer's OWN read of OpenCTI and OpenAEV (their own JWT, the same
reads recompute makes, ``ctem_cases_source.read_view``). XTM One mirrors no
group, role, marking, organization or scope: whatever the source returns to
that viewer is what they see.

- :func:`viewer_view` is the read, cached per user and vertical in Redis for
  :data:`VIEW_TTL_SECONDS`; the member ids it found are also kept, ids only,
  for :data:`LAST_SEEN_TTL_SECONDS`, so a source that is unreachable later
  still answers which cases the viewer may see (shown with their details
  marked unavailable, never empty and never someone else's).
- :func:`hydrate` renders one case for one viewer: ``None`` when none of its
  member issues is visible to them (absent from lists and counts, 404 on
  read); otherwise every visible issue in full, every other member as a
  ``{"restricted": true}`` row (the OpenCTI "Restricted" pattern), the
  priority, scores, assets and primary assets computed on the visible issues
  only, the member count total with the restricted count, and the history
  with "Restricted" in place of an issue the viewer cannot see.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.models.ctem_case import CtemCase, CtemCaseEvent, CtemCaseIssue
from app.models.ctem_signal import CtemSignal
from app.models.user import User
from app.services import ctem_case_audit as audit
from app.services import ctem_cases as rules
from app.services import ctem_version_match as version_match

logger = logging.getLogger(__name__)

#: How long one viewer's source read answers before it is read again.
VIEW_TTL_SECONDS = 300
#: How long the ids a viewer was shown decide visibility while a source is unreachable.
LAST_SEEN_TTL_SECONDS = 7 * 24 * 3600
#: The label of an issue the viewer cannot see, as OpenCTI shows it.
RESTRICTED = "Restricted"
#: How long a list read with no view to answer from waits for the background build (#2092):
#: short, a 503 held 10 s on ctem.one was no fail-fast answer (#2139).
LIST_BUILD_WAIT_SECONDS = 2.0
#: The retry delay a list read names when the view is still building after that wait.
LIST_RETRY_AFTER_SECONDS = 30
#: How often a list read waiting on a build another pod holds looks for its result (#2232).
LIST_POLL_SECONDS = 0.25
#: Decoded views kept per worker, by the digest of their stored JSON (#2232).
DECODED_VIEWS_MAX = 16
#: How long a viewer's previous view answers a list read, marked stale, while the new one builds (#2232):
#: a build after a case-opening drain outlived :data:`VIEW_TTL_SECONDS` on ctem.one, leaving only a 503,
#: and an hour still left a 503 when rollouts killed the builds in flight: any age beats an empty list.
STALE_VIEW_TTL_SECONDS = 7 * 24 * 3600
#: How long one pod's build lock lives without a refresh (#2232): short, so a pod a rollout stops while it
#: builds leaves the lock to the next pod within a minute instead of blocking every build for 15.
BUILD_LOCK_SECONDS = 60
#: How often the building pod extends its lock, well inside :data:`BUILD_LOCK_SECONDS`.
BUILD_LOCK_REFRESH_SECONDS = 20
#: At most one generation bump per vertical in this window, across pods (#2555): a burst of case
#: events (93 cases opened in 14 minutes on ctem.one) bumped once per case and made every viewer
#: rebuild a full view (8 to 56 s) per event until the database pool ran out. A change inside the
#: window marks the vertical pending and a trailing bump retires the views at the window's end.
VIEW_BUMP_INTERVAL_SECONDS = 30.0
#: The reasons a product gives about one identity (``ctem_cases_source.IDENTITY_REASONS``): the
#: viewer has no access there, so what it did not return is refused, not unjudged (#2113).
_IDENTITY_REASONS = frozenset({"rejected", "not_connected"})


class ViewRebuildingError(Exception):
    """The viewer's view is being rebuilt and no older one can answer (#2092): retry later.

    A list read never builds the whole view on the request: after a case-opening
    drain that build took 400 s on ctem.one.
    """

    def __init__(self, retry_after: int = LIST_RETRY_AFTER_SECONDS) -> None:
        self.retry_after = retry_after
        # #2287: whatever changed the cases (opened, deleted, reset, reconciled), never a guessed cause.
        super().__init__(f"The case list is being rebuilt after a change to the cases; retry in {retry_after} s.")


@dataclass
class ViewerView:
    """What one viewer's own source read returned.

    ``issues`` are the issues they can read (open or closed) with their
    details; ``open_ids`` the open ones among them; ``unavailable`` the
    member ids they were shown before from a product that did not answer now;
    ``missing`` those products; ``complete`` false when a read hit its cap
    (recompute then resolves nothing).
    """

    issues: dict[str, rules.SourceIssue] = field(default_factory=dict)
    open_ids: set[str] = field(default_factory=set)
    unavailable: set[str] = field(default_factory=set)
    missing: list[str] = field(default_factory=list)
    #: Why each product of ``missing`` did not answer this viewer's read (#1852,
    #: ``ctem_cases_source.REASON_*``); a view cached before the field reads ``unavailable``.
    reasons: dict[str, str] = field(default_factory=dict)
    complete: bool = True
    no_access: bool = False
    #: The signal members (``signal:<id>``) this viewer may read, read fresh on
    #: every call and never cached: the signals API judges them, not a product.
    signals: set[str] = field(default_factory=set)
    #: What each signal member of ``signals`` is (#1936): its name, path, lens and
    #: source, read fresh with ``signals`` so a row never shows a bare id.
    signal_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: True when this viewer's own OpenCRQ read is available (#936): only then
    #: are the OpenCRQ asset names and scenario ids of a case's exploitation shown.
    opencrq: bool = False
    #: True when this viewer's own OpenCTI read is available (CTEM-16-UI, #1115): only
    #: then are the name, ids and link of a case's threat package shown.
    opencti: bool = False
    #: True when this viewer's own OpenAEV read is available (#2468): only then are the
    #: name, id and link of a case's validation scenario shown.
    openaev: bool = False
    #: The member ids of the shared cases this view's read was given (#2113): a product member
    #: outside it belongs to a case opened after the view was built, never judged by it. ``None``
    #: for a view cached before the field (judged as before).
    judged: set[str] | None = None
    #: When the source read of this view ended (UTC, #2113).
    built_at: datetime | None = None
    #: True when a list read answered from an older generation's view (#2092, #2113).
    stale: bool = False

    def sees(self, member_id: str) -> bool:
        return member_id in self.issues or member_id in self.unavailable or member_id in self.signals

    def judges(self, member_id: str) -> bool:
        """Whether this view's read decided *member_id* (#2113): seen, or refused by a product that answered.

        A product member is NOT judged when its product did not answer for a reason of the
        product (timeout, too large, unavailable: never the identity reasons, which say the
        viewer has no access), when a read hit its cap, or when the member joined the cases
        after the view was built. Signal members are read fresh on every call: always judged.
        """
        if self.sees(member_id):
            return True
        source = member_id.split(":", 1)[0]
        if source == SIGNAL_SOURCE:
            return True
        if source in self.missing and self.reasons.get(source, "unavailable") not in _IDENTITY_REASONS:
            return False
        if not self.complete:
            return False
        if self.judged is None:  # a view cached before #2113: only an older view may predate a case
            return not self.stale
        return member_id in self.judged

    def condition(self) -> dict[str, Any]:
        """What this view could read (#2113), so a count it leaves out is never read as hidden."""
        age = int((datetime.now(UTC) - self.built_at).total_seconds()) if self.built_at else None
        return {
            "complete": self.complete and not self.missing,
            "capped": not self.complete,
            "stale": self.stale,
            "age_seconds": max(0, age) if age is not None else None,
            "products_missing": {p: self.reasons.get(p, "unavailable") for p in self.missing},
        }

    def to_json(self) -> str:
        return json.dumps(
            {
                "issues": [_issue_json(i) for i in self.issues.values()],
                "open_ids": sorted(self.open_ids),
                "unavailable": sorted(self.unavailable),
                "missing": self.missing,
                "reasons": self.reasons,
                "complete": self.complete,
                "no_access": self.no_access,
                "opencrq": self.opencrq,
                "opencti": self.opencti,
                "openaev": self.openaev,
                "judged": sorted(self.judged) if self.judged is not None else None,
                "built_at": self.built_at.isoformat() if self.built_at else None,
            }
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> ViewerView:
        data = json.loads(raw)
        issues = [_issue_from_json(i) for i in data.get("issues") or []]
        missing = list(data.get("missing") or [])
        stored = data.get("reasons") if isinstance(data.get("reasons"), dict) else {}
        judged = data.get("judged")
        built = data.get("built_at")
        return cls(
            issues={i.member_id: i for i in issues},
            open_ids=set(data.get("open_ids") or []),
            unavailable=set(data.get("unavailable") or []),
            missing=missing,
            reasons={p: str(stored.get(p) or "unavailable") for p in missing},
            complete=bool(data.get("complete", True)),
            no_access=bool(data.get("no_access", False)),
            opencrq=bool(data.get("opencrq", False)),
            opencti=bool(data.get("opencti", False)),
            openaev=bool(data.get("openaev", False)),
            judged=set(judged) if isinstance(judged, list) else None,
            built_at=datetime.fromisoformat(built) if isinstance(built, str) and built else None,
        )


def _issue_json(issue: rules.SourceIssue) -> dict[str, Any]:
    out = asdict(issue)
    out["assets"] = list(issue.assets)
    out["primary_assets"] = list(issue.primary_assets)
    out["inferred_primary_assets"] = list(issue.inferred_primary_assets)
    out["first_seen_at"] = issue.first_seen_at.isoformat() if issue.first_seen_at else None
    return out


def _issue_from_json(data: dict[str, Any]) -> rules.SourceIssue:
    data = dict(data)
    data["assets"] = tuple(data.get("assets") or ())
    data["primary_assets"] = tuple(data.get("primary_assets") or ())
    data["inferred_primary_assets"] = tuple(data.get("inferred_primary_assets") or ())
    seen = data.get("first_seen_at")
    data["first_seen_at"] = datetime.fromisoformat(seen) if seen else None
    # Typed facts (#2038); a view cached before it holds (version, outcome) pairs, read the same way.
    data["version_outcomes"] = rules.version_facts(data.get("version_outcomes"))
    data["kev_cves"] = tuple(data.get("kev_cves") or ())
    data["unknown_cves"] = tuple(data.get("unknown_cves") or ())
    return rules.SourceIssue(**data)


def _view_key(user_id: str, vertical: str, generation: str = "0") -> str:
    return f"ctem:cases:view:{user_id}:{vertical}:{generation}"


def _generation_key(vertical: str) -> str:
    return f"ctem:cases:view-generation:{vertical}"


async def _generation(client: Any, vertical: str) -> str:
    """The current generation of every cached view of *vertical* (``0`` before the first bump)."""
    try:
        raw = await client.get(_generation_key(vertical))
    except Exception:
        logger.info("CTEM cases: view generation unreadable for %s", vertical, exc_info=True)
        return "0"
    if isinstance(raw, bytes):
        raw = raw.decode()
    return str(raw) if raw else "0"


def _seen_key(user_id: str, vertical: str) -> str:
    return f"ctem:cases:seen:{user_id}:{vertical}"


def _latest_key(user_id: str, vertical: str) -> str:
    """The generation of the viewer's newest cached view (#1986): the stale copy a case read may serve."""
    return f"ctem:cases:view-latest:{user_id}:{vertical}"


def _previous_key(user_id: str, vertical: str) -> str:
    """The viewer's newest built view, whatever its generation (#2232), kept :data:`STALE_VIEW_TTL_SECONDS`."""
    return f"ctem:cases:view-previous:{user_id}:{vertical}"


def _build_lock_key(user_id: str, vertical: str, generation: str) -> str:
    """The cross-pod lock of one background build (#2232)."""
    return f"ctem:cases:view-build:{user_id}:{vertical}:{generation}"


async def _redis() -> Any | None:
    try:
        from app.core.redis import redis_client

        return await redis_client()
    except Exception:  # no Redis: every read goes to the source
        return None


SIGNAL_SOURCE = "signal"


async def signal_members(db: Any, user_id: str) -> set[str]:
    """The ``signal:<id>`` members *user_id* may read: every case-bound signal for an
    administrator or a CTEM view or decide right (``services/ctem_access.py``), else none
    (#2441: no CTEM without a right, the rule of ``GET /ctem-signals``).

    A case opened from a signal with no product issue (a path without a CVE) is
    then visible to whoever sees the signal that opened it, never to nobody.
    """
    from app.services import ctem_access

    try:
        user = await db.get(User, uuid.UUID(str(user_id)))
    except (ValueError, TypeError):
        user = None
    query = select(CtemSignal.id).where(CtemSignal.case_id.is_not(None))
    whole = bool(getattr(user, "is_admin", False)) or (
        user is not None and await ctem_access.group_right(db, user_id) is not None
    )
    if not whole:
        return set()
    try:
        ids = (await db.execute(query)).scalars()
    except Exception:
        logger.info("CTEM cases: signal members unreadable for %s", user_id, exc_info=True)
        return set()
    return {f"{SIGNAL_SOURCE}:{i}" for i in ids}


#: The phases a request timed (#1986): name -> milliseconds, set by :func:`start_timing`.
_timings: ContextVar[dict[str, float] | None] = ContextVar("ctem_cases_timings", default=None)


def start_timing() -> dict[str, float]:
    """Start timing the phases of this request; the dict fills as :func:`timed` phases end."""
    phases: dict[str, float] = {}
    _timings.set(phases)
    return phases


@asynccontextmanager
async def timed(phase: str) -> AsyncIterator[None]:
    """Add the duration of the block to *phase* of the request's timings, when they are started."""
    with timed_sync(phase):
        yield


@contextmanager
def timed_sync(phase: str) -> Iterator[None]:
    """:func:`timed` for a block with no await (a decode, a filter, a sort)."""
    start = time.perf_counter()
    try:
        yield
    finally:
        phases = _timings.get()
        if phases is not None:
            phases[phase] = phases.get(phase, 0.0) + (time.perf_counter() - start) * 1000


def mark(phase: str, value: str) -> None:
    """Record a fact of the request (``cache=hit``) beside its timings, as a zero-length phase."""
    phases = _timings.get()
    if phases is not None:
        phases[f"{phase}-{value}"] = 0.0


def server_timing(phases: dict[str, float]) -> str:
    """The ``Server-Timing`` header of *phases* (``name;dur=12.3``), in the order they ended."""
    return ", ".join(f"{re.sub(r'[^A-Za-z0-9_-]', '_', name)};dur={ms:.1f}" for name, ms in phases.items())


async def viewer_view(
    db: Any,
    user_id: str,
    vertical: str,
    *,
    fresh: bool = False,
    members: list[str] | None = None,
    stale_ok: bool = False,
) -> ViewerView:
    """The viewer's own view of the source, from the per-user cache unless *fresh*.

    The source read is ``ctem_cases_source.read_view``; it is given the member
    ids of the shared cases so the closed members the viewer can still read
    are found by id. The signal members are read fresh on every call.

    With *members* (the ``source:external_id`` of the one case a read renders,
    #1986), a view retired by a generation bump still answers while it is in its
    TTL when it already judged every product member of that case, and the view of
    the new generation is built in the background (:func:`_stale_view`).

    With *stale_ok* (a list read, #2092), the viewer's newest view of an older
    generation still in its TTL answers while the new one builds in the background;
    with none, the read waits :data:`LIST_BUILD_WAIT_SECONDS` for that build and
    raises :class:`ViewRebuildingError` when it is not done, never a whole build on the request.
    """
    async with timed("view"):
        view = await _product_view(db, user_id, vertical, fresh=fresh, members=members, stale_ok=stale_ok)
    seen = set(view.issues) | set(view.unavailable)
    async with timed("signals"):
        signals = await signal_members(db, user_id) | await source_signal_members(db, seen)
        details = await signal_details(db, signals)
    return replace(view, signals=signals, signal_details=details)


def _pivot_id(signal: Any, evidence: dict[str, Any]) -> str | None:
    """The module id a row pivots to when the signal carries no link, or None (never a guessed page).

    An OpenCTI signal names an OpenCTI object by its id; an OpenAEV one is a finding only when a
    source reader recorded it from an OpenAEV issue (``evidence.source_issue``): a listener delivery
    names a scenario or a run, which carries its own ``platform_url`` instead.
    """
    external_id = (signal.source_external_id or "").strip() or None
    provider = (signal.source_provider or "").lower()
    if provider == "opencti":
        return external_id
    if provider in ("openaev", "openbas") and evidence.get("source_issue"):
        return external_id
    return None


def signal_detail(signal: Any) -> dict[str, Any]:
    """What a signal member is, as a case row names it (#1936): never its id as a label."""
    from app.services.ctem_signals import PATH_CATALOG, provider_name

    # What the row's "Open in <Module>" pivot needs (#1357): the source's own id of the object, and
    # the link to it the delivery carried (OpenCRQ, OpenCTI: link; OpenAEV: platform_url), http(s) only.
    evidence = signal.evidence if isinstance(signal.evidence, dict) else {}
    link = next(
        (
            value
            for value in (evidence.get("link"), evidence.get("platform_url"))
            if isinstance(value, str) and value.lower().startswith(("https://", "http://"))
        ),
        None,
    )
    return {
        "name": (signal.subject_label or signal.subject_ref or None),
        "trigger_path": signal.trigger_path,
        "trigger_path_label": PATH_CATALOG.get(signal.trigger_path, ("", ""))[0] or None,
        "lens": signal.lens,
        "source_provider": signal.source_provider,
        "source_name": provider_name(signal.source_provider),
        "source_external_id": _pivot_id(signal, evidence),
        "source_link": link,
        "subject_kind": signal.subject_kind,
        "assets": [signal.subject_ref] if signal.subject_kind == "asset" and signal.subject_ref else [],
        "first_seen_at": signal.first_seen_at.isoformat() if signal.first_seen_at else None,
    }


async def signal_details(db: Any, members: set[str]) -> dict[str, dict[str, Any]]:
    """The details of the ``signal:<id>`` *members* a viewer may read, by member id (#1936).

    Only the members already judged visible are read; an unreadable store answers
    nothing and the row falls back to its trigger path label, never the id.
    """
    ids: list[uuid.UUID] = []
    for member in members:
        try:
            ids.append(uuid.UUID(member.split(":", 1)[1]))
        except (IndexError, ValueError):
            continue
    if not ids:
        return {}
    try:
        # The columns a row names, never whole ORM objects (#2232): every case-bound signal is read
        # on each list read, and building thousands of tracked instances blocked the event loop.
        query = select(*(getattr(CtemSignal, name) for name in _DETAIL_COLUMNS)).where(CtemSignal.id.in_(ids))
        rows = (await db.execute(query)).all()
    except Exception:
        logger.info("CTEM cases: signal details unreadable", exc_info=True)
        return {}
    with timed_sync("signal-details"):
        return {f"{SIGNAL_SOURCE}:{s.id}": signal_detail(s) for s in rows}


#: The ``CtemSignal`` columns :func:`signal_detail` reads.
_DETAIL_COLUMNS = (
    "id",
    "subject_label",
    "subject_ref",
    "subject_kind",
    "trigger_path",
    "lens",
    "source_provider",
    "source_external_id",
    "evidence",
    "first_seen_at",
)


#: Issue ids per ``IN`` list of the source signal read (#1986): far under every engine's parameter cap.
_SEEN_CHUNK = 900


async def source_signal_members(db: Any, seen: set[str]) -> set[str]:
    """The ``signal:<id>`` members a source reader recorded for issues this viewer sees (#1529).

    A signal of an OpenCTI or OpenAEV issue is recorded by the system; it is
    as visible as its issue (``evidence.source_issue``), never more. The issue
    ids are matched in SQL (#1986), not by loading every case-bound signal's evidence.
    """
    if not seen:
        return set()
    from app.services.ctem_source_signals import SOURCE_PATHS

    issue = CtemSignal.evidence["source_issue"].as_string()
    wanted = sorted(seen)
    out: set[str] = set()
    try:
        for start in range(0, len(wanted), _SEEN_CHUNK):
            query = select(CtemSignal.id).where(
                CtemSignal.case_id.is_not(None),
                CtemSignal.source_provider.in_(tuple(SOURCE_PATHS)),
                issue.in_(wanted[start : start + _SEEN_CHUNK]),
            )
            out.update(f"{SIGNAL_SOURCE}:{signal_id}" for signal_id in (await db.execute(query)).scalars())
    except Exception:
        logger.info("CTEM cases: source signal members unreadable", exc_info=True)
        return set()
    return out


#: One build of a viewer's view at a time per (user, vertical, generation) on this worker (#1986):
#: the case page reads the case and the list together, both wait for the one source read.
_build_locks: dict[str, asyncio.Lock] = {}
_build_lock_refs: dict[str, int] = {}


@asynccontextmanager
async def _singleflight(key: str) -> AsyncIterator[None]:
    """Hold the build lock of *key*, dropped once no request waits on it (no growth on a long worker)."""
    lock = _build_locks.setdefault(key, asyncio.Lock())
    _build_lock_refs[key] = _build_lock_refs.get(key, 0) + 1
    try:
        async with lock:
            yield
    finally:
        remaining = _build_lock_refs.get(key, 1) - 1
        if remaining > 0:
            _build_lock_refs[key] = remaining
        else:
            _build_lock_refs.pop(key, None)
            _build_locks.pop(key, None)


#: Decoded views by the digest of their stored JSON (#2232): a view of ~2.5k issues is decoded once
#: per worker, not on every list read; a rebuilt view has another digest and is decoded again.
_decoded: dict[bytes, ViewerView] = {}


def _copy_view(view: ViewerView) -> ViewerView:
    """A copy whose collections a caller may change without touching the memo (the issues are immutable)."""
    return replace(
        view,
        issues=dict(view.issues),
        open_ids=set(view.open_ids),
        unavailable=set(view.unavailable),
        missing=list(view.missing),
        reasons=dict(view.reasons),
        judged=set(view.judged) if view.judged is not None else None,
    )


def _decode_view(raw: str | bytes) -> ViewerView:
    """The view stored as *raw*, decoded once per worker (#2232); phase ``decode``, or ``decode-memo``."""
    data = raw.encode() if isinstance(raw, str) else raw
    digest = hashlib.blake2b(data, digest_size=16).digest()
    found = _decoded.get(digest)
    if found is None:
        with timed_sync("decode"):
            found = ViewerView.from_json(data)
        while len(_decoded) >= DECODED_VIEWS_MAX:
            _decoded.pop(next(iter(_decoded)))
        _decoded[digest] = found
    else:
        mark("decode", "memo")
    with timed_sync("copy"):
        return _copy_view(found)


async def _cached_view(client: Any, user_id: str, vertical: str, generation: str) -> ViewerView | None:
    try:
        async with timed("redis"):
            cached = await client.get(_view_key(user_id, vertical, generation))
        if cached:
            return _decode_view(cached)
    except Exception:
        logger.info("CTEM cases: view cache unreadable for %s", user_id, exc_info=True)
    return None


#: The background builds of a view a stale read answered (#1986), one per view key on this worker.
_revalidations: dict[str, asyncio.Task[None]] = {}


async def _stale_view(
    client: Any, user_id: str, vertical: str, generation: str, members: list[str]
) -> ViewerView | None:
    """The viewer's newest view of an older generation, when it judged every product member of *members*.

    A generation bump says the cases changed (a case opened, a signal joined one),
    not that the viewer's access did: their own source read of the older generation,
    still in its TTL (the age a same-generation hit may have), answers for the
    members it already read or marked unavailable. A member it never judged (a
    closed member, one restricted to this viewer) makes the read wait for the new
    view, so nothing is shown that the view of today would not show.
    """
    view = await _older_view(client, user_id, vertical, generation)
    if view is None or _age_seconds(view) > VIEW_TTL_SECONDS:
        return None
    product = [m for m in members if not m.startswith(f"{SIGNAL_SOURCE}:")]
    if not all(m in view.issues or m in view.unavailable for m in product):
        return None
    return view


def _age_seconds(view: ViewerView) -> float:
    """How old *view* is; a view with no ``built_at`` (cached before #2113) counts as fresh."""
    if view.built_at is None:
        return 0.0
    return max(0.0, (datetime.now(UTC) - view.built_at).total_seconds())


async def _older_view(
    client: Any, user_id: str, vertical: str, generation: str, *, same_generation: bool = False
) -> ViewerView | None:
    """The viewer's own newest view of a generation older than *generation*.

    Kept :data:`STALE_VIEW_TTL_SECONDS` (#2232) under its own key, so a build longer than
    :data:`VIEW_TTL_SECONDS` still leaves the list a view to answer from; a case read takes it
    only within :data:`VIEW_TTL_SECONDS` of its build (:func:`_stale_view`).

    With *same_generation* (the list), the kept view of *generation* itself is taken too: with
    no case change, its own copy expires after :data:`VIEW_TTL_SECONDS` while the kept one stays,
    and refusing it made every list read a 503 for the whole rebuild, every five minutes.
    """
    try:
        latest = await client.get(_latest_key(user_id, vertical))
    except Exception:
        return None
    if isinstance(latest, bytes):
        latest = latest.decode()
    if not latest or (str(latest) == generation and not same_generation):
        return None
    try:
        async with timed("redis"):
            previous = await client.get(_previous_key(user_id, vertical))
    except Exception:
        logger.info("CTEM cases: previous view unreadable for %s", user_id, exc_info=True)
        previous = None
    if previous:
        try:
            return _decode_view(previous)
        except Exception:
            logger.info("CTEM cases: previous view unreadable for %s", user_id, exc_info=True)
    # A pointer written before #2232: its view sits under the generation's own key.
    return await _cached_view(client, user_id, vertical, str(latest))


async def _list_view(client: Any, user_id: str, vertical: str, generation: str) -> ViewerView:
    """A list read's view on a cache miss (#2092): the older view, else a bounded wait for the build.

    A generation bump says the cases changed, not that the viewer's access did: their
    own older view answers the list (the cases are read fresh from the store, only the
    issue details come from it) while the new one builds in the background. The kept
    view of the current generation past :data:`VIEW_TTL_SECONDS` answers the same way.
    With no kept view at all, the background build is awaited for
    :data:`LIST_BUILD_WAIT_SECONDS`.
    """
    older = await _older_view(client, user_id, vertical, generation, same_generation=True)
    if older is not None:
        mark("cache", "stale")
        _revalidate(user_id, vertical, generation)
        # The cases opened since it was built have members it never judged (#2113): unjudged, never hidden.
        older.stale = True
        return older
    deadline = time.perf_counter() + LIST_BUILD_WAIT_SECONDS
    task = _revalidate(user_id, vertical, generation)
    if task is not None:
        # Waited on, never cancelled nor raised: a failed or slow build leaves the answer to the cache.
        await asyncio.wait({task}, timeout=LIST_BUILD_WAIT_SECONDS)
    built = await _cached_view(client, user_id, vertical, generation)
    # Another pod holds the build (#2232): its task here ends at once, so the read looks for the
    # view that pod writes for the rest of the wait, instead of a 503 while it lands (ctem.one,
    # overview at 22:39:20Z on 25 Nov 2026, the view written one second later).
    while built is None and (left := deadline - time.perf_counter()) > 0:
        await asyncio.sleep(min(LIST_POLL_SECONDS, left))
        built = await _cached_view(client, user_id, vertical, generation)
    if built is not None:
        mark("cache", "built")
        return built
    mark("cache", "rebuilding")
    raise ViewRebuildingError()


def _revalidate(user_id: str, vertical: str, generation: str) -> asyncio.Task[None] | None:
    """Build the view of *generation* in the background, once per view key on this worker."""
    key = _view_key(user_id, vertical, generation)
    if key in _revalidations:
        return _revalidations[key]

    async def build() -> None:
        _timings.set(None)  # the request that started it has answered: its timings are closed
        client = await _redis()
        lock = _build_lock_key(user_id, vertical, generation)
        if not await _take_build_lock(client, lock):
            # Another pod builds this view (#2232): the list answers the previous one meanwhile.
            logger.info("CTEM cases: view of %s (generation %s) built by another pod", user_id, generation)
            return
        keep = asyncio.create_task(_keep_build_lock(client, lock))
        try:
            from app.core.database import async_session

            async with async_session() as db:
                await _product_view(db, user_id, vertical, fresh=False)
        except Exception:
            logger.info("CTEM cases: background view build failed for %s", user_id, exc_info=True)
        finally:
            keep.cancel()
            await _release_build_lock(client, lock)

    task = asyncio.create_task(build())
    _revalidations[key] = task
    task.add_done_callback(lambda _t: _revalidations.pop(key, None))
    return task


async def _take_build_lock(client: Any, key: str) -> bool:
    """Whether this pod builds the view of *key* (#2232); no Redis, or Redis failing, builds here."""
    if client is None:
        return True
    try:
        return bool(await client.set(key, "1", ex=BUILD_LOCK_SECONDS, nx=True))
    except Exception:
        logger.info("CTEM cases: view build lock unreadable for %s", key, exc_info=True)
        return True


async def _keep_build_lock(client: Any, key: str) -> None:
    """Extend the build lock of *key* every :data:`BUILD_LOCK_REFRESH_SECONDS` while this pod builds (#2232)."""
    if client is None:
        return
    while True:
        await asyncio.sleep(BUILD_LOCK_REFRESH_SECONDS)
        try:
            await client.expire(key, BUILD_LOCK_SECONDS)
        except Exception:
            logger.info("CTEM cases: view build lock not extended for %s", key, exc_info=True)


async def _release_build_lock(client: Any, key: str) -> None:
    if client is None:
        return
    try:
        await client.delete(key)
    except Exception:
        logger.info("CTEM cases: view build lock not released for %s", key, exc_info=True)


async def _product_view(
    db: Any,
    user_id: str,
    vertical: str,
    *,
    fresh: bool,
    members: list[str] | None = None,
    stale_ok: bool = False,
) -> ViewerView:
    client = await _redis()
    generation = await _generation(client, vertical) if client is not None else "0"
    if client is not None and not fresh:
        cached = await _cached_view(client, user_id, vertical, generation)
        if cached is not None:
            mark("cache", "hit")
            return cached
        if members is not None:
            stale = await _stale_view(client, user_id, vertical, generation, members)
            if stale is not None:
                mark("cache", "stale")
                _revalidate(user_id, vertical, generation)
                return stale
            key = _view_key(user_id, vertical, generation)
            if _build_locks.get(key) is None or key in _revalidations:
                # No view to answer from and no request building one: read only this case's members and
                # build the whole view in the background (#1986), never the whole world on the request.
                mark("cache", "scoped")
                scoped = await _case_view(db, client, user_id, vertical, members)
                _revalidate(user_id, vertical, generation)
                return scoped
        elif stale_ok:
            return await _list_view(client, user_id, vertical, generation)
    if client is None:
        return await _build_view(db, client, user_id, vertical, generation)
    async with _singleflight(_view_key(user_id, vertical, generation)):
        # A request that waited on the lock finds the view the first one built.
        if not fresh:
            cached = await _cached_view(client, user_id, vertical, generation)
            if cached is not None:
                mark("cache", "shared")
                return cached
        mark("cache", "miss")
        return await _build_view(db, client, user_id, vertical, generation)


async def _build_view(db: Any, client: Any, user_id: str, vertical: str, generation: str) -> ViewerView:
    from app.services import ctem_cases_source as source

    started = time.perf_counter()
    async with timed("source"):
        view: ViewerView = await source.read_view(db, user_id, vertical)
    logger.info(
        "CTEM cases: view of %s built in %.0f ms (vertical %s, generation %s, %d issues, missing %s)",
        user_id,
        (time.perf_counter() - started) * 1000,
        vertical,
        generation,
        len(view.issues),
        view.missing,
    )
    await _seen_while_missing(client, user_id, vertical, view)
    if client is not None:
        try:
            raw = view.to_json()
            await client.set(_view_key(user_id, vertical, generation), raw, ex=VIEW_TTL_SECONDS)
            # The previous view the list answers from while the next generation builds (#2232).
            await client.set(_previous_key(user_id, vertical), raw, ex=STALE_VIEW_TTL_SECONDS)
            await client.set(_latest_key(user_id, vertical), generation, ex=STALE_VIEW_TTL_SECONDS)
            visible = sorted(set(view.issues) | view.unavailable)
            await client.set(_seen_key(user_id, vertical), json.dumps(visible), ex=LAST_SEEN_TTL_SECONDS)
        except Exception:
            logger.info("CTEM cases: view cache not written for %s", user_id, exc_info=True)
    return view


async def _seen_while_missing(client: Any, user_id: str, vertical: str, view: ViewerView) -> None:
    """Ids the viewer was shown from a product that does not answer now: still theirs to see, marked unavailable."""
    if not view.missing or client is None:
        return
    try:
        seen = await client.get(_seen_key(user_id, vertical))
        for member_id in json.loads(seen) if seen else []:
            if member_id.split(":", 1)[0] in view.missing and member_id not in view.issues:
                view.unavailable.add(member_id)
    except Exception:
        logger.info("CTEM cases: last-seen ids unreadable for %s", user_id, exc_info=True)


async def _case_view(db: Any, client: Any, user_id: str, vertical: str, members: list[str]) -> ViewerView:
    """The viewer's own view of one case's *members* (``ctem_cases_source.read_case_view``), never cached.

    Same rules as a whole view: what the viewer cannot read stays out (Restricted),
    an id seen before from a product that does not answer now is marked unavailable.
    """
    from app.services import ctem_cases_source as source

    async with timed("source"):
        view: ViewerView = await source.read_case_view(db, user_id, vertical, members)
    await _seen_while_missing(client, user_id, vertical, view)
    view.unavailable &= set(members)
    return view


async def forget_view(user_id: str, vertical: str) -> None:
    """Drop the cached view (after a recompute, so the next read is the new one)."""
    client = await _redis()
    if client is None:
        return
    try:
        await client.delete(_view_key(user_id, vertical, await _generation(client, vertical)))
    except Exception:
        logger.info("CTEM cases: view cache not dropped for %s", user_id, exc_info=True)


def _bump_window_key(vertical: str) -> str:
    return f"ctem:cases:view-bump-window:{vertical}"


def _bump_pending_key(vertical: str) -> str:
    return f"ctem:cases:view-bump-pending:{vertical}"


#: The trailing bump of each vertical this worker waits to run (#2555), one per vertical.
_TRAILING_BUMPS: dict[str, asyncio.Task[None]] = {}


async def forget_all_views(vertical: str) -> None:
    """Drop every viewer's cached view of *vertical* (a case opened, a signal joined one).

    The cases changed for everyone, not only for the caller, so the views are
    retired by bumping their generation: the next read of each viewer misses
    and reads its own source again; the old entries expire with their TTL.

    Bumps are coalesced (#2555): the first change of a window bumps at once; a
    change inside :data:`VIEW_BUMP_INTERVAL_SECONDS` of a bump marks the
    vertical pending and one trailing bump runs at the end of the window, so a
    burst of N events retires the views about once per window, never N times,
    and the last change is never lost.
    """
    client = await _redis()
    if client is None:
        return
    try:
        if await client.set(_bump_window_key(vertical), "1", nx=True, ex=_bump_window_seconds()):
            await client.incr(_generation_key(vertical))
            return
        await client.set(_bump_pending_key(vertical), "1", ex=_bump_window_seconds() * 4)
    except Exception:
        logger.info("CTEM cases: view generation not bumped for %s", vertical, exc_info=True)
        return
    _schedule_trailing_bump(vertical)


def _bump_window_seconds() -> int:
    return max(1, int(VIEW_BUMP_INTERVAL_SECONDS))


def _schedule_trailing_bump(vertical: str) -> None:
    """Start this worker's trailing bump of *vertical* unless one already waits."""
    running = _TRAILING_BUMPS.get(vertical)
    if running is not None and not running.done():
        return
    task = asyncio.create_task(_trailing_bump(vertical), name=f"ctem-view-trailing-bump:{vertical}")
    _TRAILING_BUMPS[vertical] = task


async def _trailing_bump(vertical: str) -> None:
    """Bump *vertical* once its window ends, while a change is pending; never raises.

    Any pod may run it: the pending mark is shared, so the first trailing bump that takes the
    next window retires the views and clears the mark; the others find nothing pending.
    """
    try:
        while True:
            await asyncio.sleep(VIEW_BUMP_INTERVAL_SECONDS)
            client = await _redis()
            if client is None or not await client.get(_bump_pending_key(vertical)):
                return
            if await client.set(_bump_window_key(vertical), "1", nx=True, ex=_bump_window_seconds()):
                await client.delete(_bump_pending_key(vertical))
                await client.incr(_generation_key(vertical))
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.info("CTEM cases: trailing view generation bump failed for %s", vertical, exc_info=True)


def _member_id(row: CtemCaseIssue) -> str:
    return f"{row.source}:{row.external_id}"


def _visible_issue(
    row: CtemCaseIssue, issue: rules.SourceIssue | None, signal: dict[str, Any] | None = None
) -> dict[str, Any]:
    if row.source == SIGNAL_SOURCE:  # the signal that opened or joined the case: what it is, by name (#1936)
        out = {"source": row.source, "external_id": row.external_id, "open": row.open, **(signal or {})}
        if issue is not None:  # #2396: the primary assets its gate decision reached, which the case counts
            out["primary_assets"] = list(issue.primary_assets)
            out["primary_asset_links"] = primary_asset_links(issue)
        return out
    if issue is None:  # seen before, its product does not answer now
        return {"source": row.source, "external_id": row.external_id, "open": row.open, "details_unavailable": True}
    return {
        "source": row.source,
        "external_id": row.external_id,
        "name": issue.name,
        "cve": issue.cve,
        "cvss": issue.cvss,
        "epss": issue.epss,
        "kev": issue.kev,
        "assets": list(issue.assets),
        "primary_assets": list(issue.primary_assets),
        # #2396: each primary asset with its link basis, proven (manual or OpenCRQ link) or inferred.
        "primary_asset_links": primary_asset_links(issue),
        "first_seen_at": issue.first_seen_at.isoformat() if issue.first_seen_at else None,
        "open": row.open,
        # The version knowledge of this viewer's own OpenCTI read (CTEM-11, #946; shown by #928).
        "installed_version": issue.installed_version,
        "fixed_version": issue.fixed_version,
        "fixed_by": issue.fixed_by,
        "course_of_action_name": issue.course_of_action_name if issue.fixed_version and not issue.fixed_by else None,
        "not_affected": bool(issue.not_affected),
        "declared_version_known": issue.declared_version_known,
        # Each installed version and what OpenCTI says of it (#1783): affected, not_affected or unknown,
        # with where the version was read, its asset count and its fix (#2038).
        "version_outcomes": [fact.as_dict() for fact in issue.version_outcomes],
        # The OpenAEV finding lifecycle (CTEM-14-UI): absent when the source carries none.
        **(
            {
                "retest_status": issue.retest_status,
                "last_tested_at": issue.last_tested_at,
                "retest_pending": issue.retest_pending,
            }
            if issue.retest_status or issue.retest_pending
            else {}
        ),
    }


def primary_asset_links(issue: rules.SourceIssue) -> list[dict[str, str]]:
    """``[{name, basis}]`` of the primary assets *issue* reaches, ``proven`` or ``inferred`` (#2396)."""
    inferred = set(issue.inferred_primary_assets)
    return [{"name": j, "basis": "inferred" if j in inferred else "proven"} for j in issue.primary_assets]


#: How a case counts its primary assets (#2396): the reader repeats it beside the counts.
PRIMARY_ASSET_BASIS = (
    "primary_asset_count counts the primary assets a manual or OpenCRQ link proves; "
    "inferred_primary_asset_count those reached only through an inventory rule, a name match "
    "or the evidence; each issue row lists its primary assets with their basis"
)


def _member_label(member: str, view: ViewerView) -> str | None:
    signal = view.signal_details.get(member)
    if signal is not None:
        return signal.get("name") or None
    issue = view.issues.get(member)
    return (issue.cve or issue.name or None) if issue is not None else None


def visible_members(rows: list[CtemCaseIssue], view: ViewerView) -> list[CtemCaseIssue]:
    return [r for r in rows if view.sees(_member_id(r))]


#: The ``signal_type`` filter value of a case no signal member of which the viewer sees (split, legacy).
NO_SIGNAL_TYPE = "none"


def signal_types(case: CtemCase, rows: list[CtemCaseIssue], view: ViewerView) -> list[str]:
    """The trigger paths of the signals behind *case* this viewer sees, sorted (#2556).

    Read from ``view.signal_details``, already read for the row: no query. A visible
    signal member whose details were unreadable falls back to the case's own trigger
    path; a case with no visible signal member answers ``[]`` (the "No signal" value).
    """
    members = [_member_id(r) for r in rows if r.source == SIGNAL_SOURCE and view.sees(_member_id(r))]
    if not members:
        return []
    paths = {str(path) for m in members if (path := (view.signal_details.get(m) or {}).get("trigger_path"))}
    if not paths and case.trigger_path:
        paths.add(case.trigger_path)
    return sorted(paths)


def matches_signal_types(row: dict[str, Any], wanted: Iterable[str]) -> bool:
    """Whether a list row carries one of *wanted* (a trigger path, or ``none`` for no signal; OR inside the filter)."""
    wanted = set(wanted)
    if not wanted:
        return True
    types = row.get("signal_types") or []
    if not types:
        return NO_SIGNAL_TYPE in wanted
    return any(t in wanted for t in types)


SEVERITY_PRIORITY: dict[str, str] = {"P1": "critical", "P2": "high", "P3": "medium"}

#: The aggregates computed from member details: null for a viewer missing the details of an open member.
DETAIL_AGGREGATES: tuple[str, ...] = (
    "asset_count",
    "primary_asset_count",
    "inferred_primary_asset_count",
    "kev_count",
    "max_cvss",
    "max_epss",
)
#: Why they are null: the reader repeats it instead of reading "none".
AGGREGATES_RESTRICTED_REASON = (
    "unknown for this reader: some open issues of the case are not readable with their details "
    "(outside your scope, or their product did not answer); this is not a zero"
)
#: The aggregates an open signal member leaves unknown when its CVEs' facts were not read (#2225).
FACT_AGGREGATES: tuple[str, ...] = ("kev_count", "max_cvss", "max_epss")
#: Why they are null: OpenCTI was not read for this reader (no access, or it failed), or it does not know the CVE.
FACTS_UNREAD_REASON = (
    "unknown for this reader: OpenCTI facts (KEV, CVSS, EPSS) were not read for some CVEs the case's "
    "signals name (no OpenCTI access for this reader, OpenCTI did not answer, or it does not know "
    "the CVE); this is not a zero"
)


def facts_unread(details: list[Any]) -> int:
    """The open signal members naming a CVE whose facts were not read (#2225): their KEV and scores are unknown."""
    return sum(1 for d in details if d.source == SIGNAL_SOURCE and tuple(getattr(d, "unknown_cves", ()) or ()))


def kev_count(details: list[Any]) -> int:
    """The KEV count of a case: each KEV CVE once (#2041).

    A product member counts when it is KEV. A signal member counts each KEV CVE
    it names (every one, not only its first) that no product member of the case
    carries and no other signal already counted: a signal enriched from OpenCTI
    and the OpenCTI issue of the same CVE are one vulnerability.
    """
    products = [d for d in details if d.source != SIGNAL_SOURCE]
    counted = {str(d.cve).upper() for d in products if d.cve}
    total = sum(1 for d in products if d.kev)
    for d in details:
        if d.source != SIGNAL_SOURCE:
            continue
        named = tuple(getattr(d, "kev_cves", ()) or ())
        if not named and d.kev and d.cve:  # a member cached before kev_cves
            named = (str(d.cve).upper(),)
        for cve in named:
            if cve not in counted:
                counted.add(cve)
                total += 1
    return total


def case_priority(severity: str | None, details: list[Any]) -> str:
    """The priority a case shows: its loop severity when set, else the default policy."""
    if severity in SEVERITY_PRIORITY:
        return SEVERITY_PRIORITY[severity]
    return rules.default_priority(details) if details else "low"


def summary(case: CtemCase, rows: list[CtemCaseIssue], view: ViewerView) -> dict[str, Any] | None:
    """The list row of *case* for this viewer, ``None`` when they see none of its issues."""
    visible = visible_members(rows, view)
    if not visible:
        return None
    details = [view.issues[_member_id(r)] for r in visible if r.open and _member_id(r) in view.issues]
    agg = rules.aggregates(details)
    # One KEV CVE is counted once, from its product member, when a signal names it too (#2041).
    agg["kev_count"] = kev_count(details)
    # Open product members this viewer has no details of (hidden by scope, or seen before from a
    # product that does not answer now): the detail aggregates would count only part of the case,
    # so they are null with a reason, never a silent 0 a reader takes for "none". Signal members
    # carry no issue details for anyone and never restrict them.
    without_details = sum(1 for r in rows if r.open and r.source != SIGNAL_SOURCE and _member_id(r) not in view.issues)
    if without_details:
        agg = {**agg, **{key: None for key in DETAIL_AGGREGATES}}
    # A signal member whose CVE facts were not read: KEV and scores unknown, never "0 KEV" (#2225).
    unread = facts_unread(details)
    if unread:
        agg = {**agg, **{key: None for key in FACT_AGGREGATES}}
    unavailable = sorted({r.source for r in visible if r.source != SIGNAL_SOURCE and _member_id(r) not in view.issues})
    # Kept and marked, never discarded (#2004): the running version is to validate first.
    unverified = version_match.unverified_reason(details)
    return {
        "id": str(case.id),
        "title": case.title,
        "remediation_kind": case.remediation_kind,
        "product_label": case.product_label,
        "fixed_version": case.fixed_version,
        # The fix of each platform when the remediation names it (#1572), else {}.
        "fixed_versions": dict(getattr(case, "fixed_versions", None) or {}),
        "state": case.state,
        "state_since": case.state_since.isoformat() if case.state_since else None,
        "owner_user_id": str(case.owner_user_id) if case.owner_user_id else None,
        "due_at": case.due_at.isoformat() if case.due_at else None,
        "accepted_reason": case.accepted_reason,
        "accepted_until": case.accepted_until.isoformat() if case.accepted_until else None,
        # One case, one urgency (UX round 1 on #897, B3): a loop severity sets the
        # priority; otherwise computed on what this viewer sees.
        "priority": case_priority(case.severity, details),
        "priority_basis": (
            "loop severity" if case.severity in SEVERITY_PRIORITY else "default policy, on the issues you can see"
        ),
        "issue_count": len(rows),
        "open_issue_count": sum(1 for r in rows if r.open),
        "restricted_count": len(rows) - len(visible),
        # The open members read without their details, and whether the detail aggregates are
        # therefore unknown for this viewer (null, never 0).
        "members_without_details": without_details,
        "aggregates_restricted": bool(without_details),
        "aggregates_reason": AGGREGATES_RESTRICTED_REASON if without_details else None,
        # The open signal members whose CVE facts were not read (#2225), and why kev_count, max_cvss
        # and max_epss are then null.
        "members_facts_unread": unread,
        "facts_reason": FACTS_UNREAD_REASON if unread else None,
        "asset_count": agg["asset_count"],
        "primary_asset_count": agg["primary_asset_count"],
        "inferred_primary_asset_count": agg["inferred_primary_asset_count"],
        "primary_asset_basis": PRIMARY_ASSET_BASIS,
        "kev_count": agg["kev_count"],
        "max_cvss": agg["max_cvss"],
        "max_epss": agg["max_epss"],
        "first_seen_at": agg["first_seen_at"].isoformat() if agg["first_seen_at"] else None,
        # When the case was opened and last changed (#1176): the list's Created, Age and Last change columns.
        "created_at": case.created_at.isoformat() if case.created_at else None,
        "updated_at": case.updated_at.isoformat() if case.updated_at else None,
        # The signals that opened or joined the case, counted on every member, seen or not.
        "signal_count": sum(1 for r in rows if r.source == SIGNAL_SOURCE),
        # The trigger paths of the signals this viewer sees (#2556): the list's Signal type filter.
        "signal_types": signal_types(case, rows, view),
        "details_unavailable": unavailable,
        # The loop (#857): ids, enums, dates and references only, the same for every viewer.
        "trigger_path": case.trigger_path,
        "severity": case.severity,
        "sla_due_at": case.sla_due_at.isoformat() if case.sla_due_at else None,
        "sla_breached_at": case.sla_breached_at.isoformat() if case.sla_breached_at else None,
        "risk_scenario_ids": list(case.risk_scenario_ids or []),
        "tickets": list(case.tickets or []),
        "validations": list(case.validations or []),
        "validation_plan": bool(case.validation_plan),
        "resolution_kind": case.resolution_kind,
        # A running version OpenCTI cannot place (#2004): to validate; priority and SLA unchanged.
        "version_unverified": unverified is not None,
        "version_unverified_reason": unverified,
    }


#: The running-versions record of a case (``ctem_case_version_history.KIND``, #1694).
VERSIONS_KIND = "versions"


def _event(row: CtemCaseEvent, view: ViewerView) -> dict[str, Any]:
    issue = row.issue
    if issue is not None and not view.sees(issue):
        issue = RESTRICTED
    # A member the viewer sees is named, never shown by its id (#1936): a signal by its subject,
    # a product issue by its CVE or name.
    label = _member_label(issue, view) if issue and issue != RESTRICTED else None
    # A signal with no name falls back to its trigger path label, as its Issues row does
    # (UX round 1 B2 on #1945).
    signal = view.signal_details.get(issue) if issue and issue != RESTRICTED else None
    path_label = signal.get("trigger_path_label") if signal is not None else None
    # A treatment plan's name and OpenCRQ link go to a viewer who reads OpenCRQ only (#1068).
    # A threat package's Grouping and coverage go to a viewer who reads OpenCTI only (#1115).
    # A validation scenario's name and OpenAEV link go to a viewer who reads OpenAEV only (#2468).
    hidden = (
        (row.kind == "treatment_plan" and not view.opencrq)
        or (row.kind == "threat_package" and not view.opencti)
        or (row.kind == "scenario" and not view.openaev)
    )
    named: dict[str, Any] = {}
    if row.kind == "threat_package":
        # ``from`` holds the coverage id, never a previous value (#2429): named alongside.
        named = {
            "grouping_id": None if hidden else row.to_value,
            "coverage_id": None if hidden else row.from_value,
        }
    return {
        **named,
        "at": row.at.isoformat() if row.at else None,
        "actor": row.actor,
        "kind": row.kind,
        "issue": issue,
        "issue_label": label,
        "issue_path_label": path_label,
        "from": None if hidden else row.from_value,
        "to": None if hidden else row.to_value,
        "reason": None if hidden else row.reason,
        "evidence": None if hidden else row.evidence,
    }


def case_position(events: list[CtemCaseEvent], view: ViewerView, owner: object = None) -> dict[str, Any]:
    """Where one case stands, from the history this viewer is served (#1503).

    ``phase``: its CTEM phase (the list's grouping and the Kanban by phase);
    ``waiting_on``: the gate it waits on, who and since when (``None`` when none); who is a person's
    id: the one the gate names, else the case *owner* (#2452, ``ctem_case_audit.open_wait``);
    ``stalled_since``: when the sweep found it waiting on nobody, while that is still its newest
    event (``None`` once anything happened to the case after it, #1736).
    """
    served = [_event(row, view) for row in events if row.kind != VERSIONS_KIND]
    return {
        "phase": audit.current_phase(served),
        "waiting_on": audit.open_wait(served, owner),
        "stalled_since": audit.stalled_since(served),
    }


#: A detection gap the detection desk proposed on the case (#2393): from the gap key
#: ``<inject>:<asset>``, to the root cause (or ``answered:<outcome>``), reason the remediation,
#: evidence ``{technique, simulation_id, evidence, rule_draft, rule_language, item_id}`` as JSON.
DETECTION_GAP_KIND = "detection_gap"


def detection_gaps_of(events: list[CtemCaseEvent]) -> list[dict[str, Any]]:
    """The detection gaps proposed on the case, one per gap key, oldest first (#2393).

    Each carries its proposal (root cause, remediation, drafted rule, decision item)
    and its answer once given; a gap proposed again replaces its proposal.
    """
    gaps: dict[str, dict[str, Any]] = {}
    rows = [e for e in events if e.kind == DETECTION_GAP_KIND and e.from_value]
    for row in sorted(rows, key=lambda e: e.at or datetime.min.replace(tzinfo=UTC)):
        key = str(row.from_value)
        at = row.at.isoformat() if row.at else None
        to = row.to_value or ""
        if to.startswith("answered:"):
            if key in gaps:
                gaps[key]["answer"] = {"outcome": to.split(":", 1)[1], "by": row.reason, "at": at}
            continue
        try:
            detail = json.loads(row.evidence or "{}")
        except ValueError:
            detail = {}
        gaps[key] = {
            "key": key,
            "root_cause": to,
            "remediation": row.reason,
            "technique": detail.get("technique"),
            "simulation_id": detail.get("simulation_id"),
            "evidence": detail.get("evidence"),
            "rule_draft": detail.get("rule_draft"),
            "rule_language": detail.get("rule_language"),
            "decision_item_id": detail.get("item_id"),
            "proposed_at": at,
            "answer": None,
        }
    return list(gaps.values())


def treatment_plan_of(events: list[CtemCaseEvent]) -> dict[str, Any] | None:
    """The treatment plan attached to the case: its newest ``treatment_plan`` event (#1068), whatever the order."""
    plans = [row for row in events if row.kind == "treatment_plan" and row.at is not None]
    newest = max(plans, key=lambda row: row.at) if plans else None
    if newest is None or not newest.to_value:
        return None
    return {
        "id": newest.to_value,
        "name": newest.reason,
        "url": newest.evidence,
        "at": newest.at.isoformat() if newest.at else None,
    }


def treatment_plan_for(events: list[CtemCaseEvent], view: ViewerView) -> dict[str, Any] | None:
    """The case's treatment plan as this viewer may read it (#1068).

    Whether a plan is attached, and when, goes to every viewer of the case; the
    plan's id, name and OpenCRQ link only to a viewer whose own OpenCRQ read is
    available (as the exploitation reading, #936), else ``restricted``.
    """
    plan = treatment_plan_of(events)
    if plan is None:
        return None
    if not view.opencrq:
        return {"restricted": True, "at": plan["at"]}
    return {**plan, "restricted": False}


def threat_package_of(events: list[CtemCaseEvent]) -> dict[str, Any] | None:
    """The threat package attached to the case: its newest ``threat_package`` event (#1115), whatever the order."""
    packages = [row for row in events if row.kind == "threat_package" and row.at is not None]
    newest = max(packages, key=lambda row: row.at) if packages else None
    if newest is None or not newest.to_value:
        return None
    return {
        "grouping_id": newest.to_value,
        "name": newest.reason,
        "url": newest.evidence,
        "coverage_id": newest.from_value,
        "at": newest.at.isoformat() if newest.at else None,
    }


#: An OpenCTI draft page: the package still sits in a draft (#1453, #1480); the same
#: pattern ``ctem_case_loop.record_threat_package`` accepts.
_DRAFT_PAGE = re.compile(
    r"^https?://[^\s/?#]+(?:/[A-Za-z0-9._-]+)*/data/import/draft/[0-9a-fA-F-]{36}(?:/[A-Za-z0-9._-]*)?$"
)


#: The review gate of the CTEM loop: its answer decides whether the package may be published (#1573).
REVIEW_GATE = "ctem.analyst_review"

#: Outcomes of the newest review gate of a case (:func:`review_outcome`).
REVIEW_APPROVED = "approved"
REVIEW_WAITING = "waiting"
REVIEW_REJECTED = "rejected"
REVIEW_REPLAN = "replan"
#: Answered with no option recorded: the answer cannot be read, the page rule alone decides.
REVIEW_UNREAD = "unread"

#: An answered option that approves the package, or refuses it, matched on the WHOLE answer:
#: ``record_gate`` stores free text as typed, so "Yes, but split the package first" or
#: "No problem, publish it" must stay a re-plan, never a leading-word match (#1576 review).
_APPROVE_OPTION = re.compile(
    r"^\s*(approve|approved|accept|accepted|validate|validated|publish)"
    r"(\s+(and\s+)?(request\s+(the\s+)?coverage|publish(\s+it)?|it|the\s+package))?\s*\.?\s*$",
    re.I,
)
_REJECT_OPTION = re.compile(
    r"^\s*(reject|rejected|refuse|refused|deny|denied|decline|declined)(\s+(it|the\s+package))?\s*\.?\s*$",
    re.I,
)
#: The analyst's mark in OpenCTI answers by approving (``ctem_analyst_mark``).
_MARK_ANSWER = re.compile(r"analyst'?s? mark", re.I)


def review_outcome(events: list[CtemCaseEvent]) -> str | None:
    """How the newest ``ctem.analyst_review`` gate of the case ended (#1573), ``None`` when none was written.

    ``approved``: passed automatically, answered by the analyst's mark, or
    answered with an approving option; ``rejected``: answered with a refusal;
    ``replan``: answered with free text or any other option; ``waiting``: the
    newest gate waits; ``unread``: answered with no option recorded.
    """
    newest = _newest_review_gate(events)
    if newest is None:
        return None
    if newest.to_value == "passed_automatically":
        return REVIEW_APPROVED
    if newest.to_value != "answered":
        return REVIEW_WAITING
    option = (newest.evidence or "").strip()
    if _APPROVE_OPTION.fullmatch(option) or (not option and _MARK_ANSWER.search(newest.reason or "")):
        return REVIEW_APPROVED
    if _REJECT_OPTION.fullmatch(option) or rules.closes_case(option):
        return REVIEW_REJECTED
    return REVIEW_REPLAN if option else REVIEW_UNREAD


def _newest_review_gate(events: list[CtemCaseEvent]) -> CtemCaseEvent | None:
    gates = [row for row in events if row.kind == "gate" and row.from_value == REVIEW_GATE and row.at is not None]
    return max(gates, key=lambda row: row.at) if gates else None


def package_review(events: list[CtemCaseEvent]) -> str | None:
    """The review outcome that applies to the case's newest threat package (#1677).

    :func:`review_outcome`, except that a refusal or a re-plan answer stops only
    the package recorded before or at that answer: a package with no page or the
    draft page recorded AFTER it is the new package the loop drafted under the
    answer, which waits for its own review, so ``None`` is answered and the page
    rule reads it ``drafted``. A Grouping page recorded after such an answer
    keeps the stopped state: no approval ever covered it (``verify_package``
    refuses it at record time).
    """
    review = review_outcome(events)
    if review not in (REVIEW_REJECTED, REVIEW_REPLAN):
        return review
    gate = _newest_review_gate(events)
    packages = [row for row in events if row.kind == "threat_package" and row.at is not None]
    newest = max(packages, key=lambda row: row.at) if packages else None
    if gate is None or gate.at is None or newest is None or newest.at is None:
        return review
    if _aware(newest.at) <= _aware(gate.at):
        return review
    url = str(newest.evidence or "").strip()
    return None if not url or _DRAFT_PAGE.match(url) else review


#: The package state of a review that stops it, whatever its page says (#1573).
#: A refusal or a re-plan stops only the package recorded before its answer (#1677, :func:`package_review`).
_STOPPED_BY_REVIEW = {REVIEW_WAITING: "awaiting_review", REVIEW_REJECTED: "rejected", REVIEW_REPLAN: "replan"}


def threat_package_state(package: dict[str, Any], review: str | None = None) -> str:
    """The package's state from its page and the case's review gate (#1480, #1547, #1573).

    ``validated`` only when the page is the live Grouping page AND the review
    approved it (or no review gate was ever written on the case, the cases of
    before the gate). A review that waits reads ``awaiting_review``, a refusal
    ``rejected``, an answer with free text or another option ``replan``: the
    draft was not applied, so the package is not published whatever page was
    recorded. Else a package with no page, or the draft page, reads ``drafted``.
    *review* is :func:`package_review` of the case's events.
    """
    stopped = _STOPPED_BY_REVIEW.get(review or "")
    if stopped:
        return stopped
    url = str(package.get("url") or "").strip()
    return "validated" if url and not _DRAFT_PAGE.match(url) else "drafted"


def threat_package_for(
    events: list[CtemCaseEvent], view: ViewerView, *, sla_due_at: datetime | None = None, now: datetime | None = None
) -> dict[str, Any] | None:
    """The case's threat package as this viewer may read it (CTEM-16-UI, #1115).

    Whether a package was drafted or validated, whether its coverage was
    requested, and when, go to every viewer of the case; the Grouping's name,
    ids and OpenCTI link only to a viewer whose own OpenCTI read is available,
    else ``restricted``. ``coverage_requested`` is true only once the draft is
    validated (#1480): a coverage written in a draft never reached OpenAEV, and
    reads ``coverage_drafted``. ``draft_overdue``: still drafted (or waiting for
    its review) past the case SLA. A refused or re-planned review (#1573) reads
    neither: the package and its coverage stop there, unless a new drafted
    package was recorded after the answer (#1677, :func:`package_review`).
    """
    package = threat_package_of(events)
    if package is None:
        return None
    state = threat_package_state(package, package_review(events))
    has_coverage = bool(package["coverage_id"])
    pending = state in ("drafted", "awaiting_review")
    at = now or datetime.now(UTC)
    status = {
        "state": state,
        "coverage_requested": has_coverage and state == "validated",
        "coverage_drafted": has_coverage and pending,
        "draft_overdue": pending and sla_due_at is not None and _aware(sla_due_at) < at,
    }
    if not view.opencti:
        return {"restricted": True, **status, "at": package["at"]}
    return {**package, **status, "restricted": False}


def _served_history(events: list[CtemCaseEvent], page: int | None = None) -> list[CtemCaseEvent]:
    """The history rows a case read serves: the versions record left out, then the newest *page* (#2429)."""
    rows = [e for e in events if e.kind != VERSIONS_KIND]
    return rows if page is None else rows[:page]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _rank(row: CtemCaseIssue, view: ViewerView) -> tuple:
    issue = view.issues.get(_member_id(row))
    if issue is None:
        return (1, not row.open, 0.0, 0.0)
    return (0, not row.open, -(issue.cvss or 0.0), -(issue.epss or 0.0))


def hydrate(
    case: CtemCase,
    rows: list[CtemCaseIssue],
    events: list[CtemCaseEvent],
    view: ViewerView,
    *,
    history_page: int | None = None,
) -> dict[str, Any] | None:
    """One case for this viewer, ``None`` when they see none of its issues.

    A member they cannot see is ``{"restricted": true}`` and nothing else: no
    id, source, name, score, asset or link. *events* is the case's whole history,
    newest first: the treatment plan, the threat package and its review state are
    derived from all of it, and only the served ``history`` is cut to the newest
    *history_page* entries (#2429), so a package older than the page still reads.
    """
    head = summary(case, rows, view)
    if head is None:
        return None
    visible = [r for r in sorted(rows, key=lambda r: _rank(r, view)) if view.sees(_member_id(r))]
    restricted = len(rows) - len(visible)
    issues = [
        _visible_issue(r, view.issues.get(_member_id(r)), view.signal_details.get(_member_id(r))) for r in visible
    ] + [{"restricted": True} for _ in range(restricted)]
    from app.services import ctem_case_assets as case_assets
    from app.services import ctem_case_owner
    from app.services import ctem_validation_scenario as case_scenario

    return {
        **head,
        # Why the case has its owner and the other asset group owners who contribute (#2560).
        **ctem_case_owner.owner_details(events),
        "keys": list(case.keys or []),
        "issues": issues,
        # The issues' assets, then the hosts a validation ran on, each with its provenance (#2395).
        "assets": case_assets.merged_assets(issues, case_assets.attached_assets(case.validations)),
        # The running-versions record (#1694) is served by the case read's ``running_versions.history``.
        "history": [_event(e, view) for e in _served_history(events, history_page)],
        "exploitation": exploitation_for(case, view),
        "attack_path": attack_path_for(case, view),
        "treatment_plan": treatment_plan_for(events, view),
        "threat_package": threat_package_for(events, view, sla_due_at=case.sla_due_at),
        "scenario": case_scenario.scenario_for(events, view),
    }


def exploitation_for(case: CtemCase, view: ViewerView) -> list[dict[str, Any]]:
    """The case's exploitation reading as this viewer may read it (#936).

    Techniques, exposure class and reading are served to every viewer of the
    case; the OpenCRQ asset name and scenario id only to a viewer whose own
    OpenCRQ read is available, else the asset is ``Restricted`` and the
    scenario absent.
    """
    out: list[dict[str, Any]] = []
    for entry in getattr(case, "exploitation", None) or []:
        if not isinstance(entry, dict):
            continue
        out.append(
            {
                "asset": entry.get("asset") if view.opencrq else RESTRICTED,
                "risk_scenario_id": entry.get("risk_scenario_id") if view.opencrq else None,
                "exposure": entry.get("exposure") or "unknown",
                "techniques": list(entry.get("techniques") or []),
                "reading": entry.get("reading") or "",
                "at": entry.get("at"),
            }
        )
    return out


def attack_path_for(case: CtemCase, view: ViewerView) -> dict[str, Any] | None:
    """The case's attack-path reach as this viewer may read it (#1289); ``None`` before the first read.

    The shape (hops nearest first with proven or inferred, depths, criticality,
    exposure, counts) is served to every viewer of the case; the OpenCRQ asset
    names and the simulation (name, page, date) only to a viewer whose own
    OpenCRQ read is available, else each name is ``Restricted``. The raw
    simulation id is never served: it is kept for the trace (#1296, B2).
    """
    stored = getattr(case, "attack_path", None) or {}
    if not isinstance(stored, dict) or not isinstance(stored.get("origin"), dict):
        return None

    def name(value: object) -> str:
        return str(value or "") if view.opencrq else RESTRICTED

    origin = stored["origin"]
    return {
        "origin": {"asset": name(origin.get("asset")), "exposure": origin.get("exposure") or "unknown"},
        "hops": [
            {
                "from": name(h.get("from")),
                "to": name(h.get("to")),
                "nature": "proven" if h.get("nature") == "proven" else "inferred",
                "simulation_name": h.get("simulation_name") if view.opencrq else None,
                "simulation_url": h.get("simulation_url") if view.opencrq else None,
                "seen_at": h.get("seen_at"),
            }
            for h in stored.get("hops") or []
            if isinstance(h, dict)
        ],
        "reached": [
            {"asset": name(a.get("asset")), "depth": a.get("depth"), "criticality": a.get("criticality")}
            for a in stored.get("reached") or []
            if isinstance(a, dict)
        ],
        "primary": [name(p) for p in stored.get("primary") or []],
        "at": stored.get("at"),
    }


_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def list_for_viewer(
    cases: list[tuple[CtemCase, list[CtemCaseIssue]]],
    view: ViewerView,
    *,
    state: str | None = None,
    owner_user_id: str | None = None,
    priority: str | None = None,
    asset: str | None = None,
    primary_asset: str | None = None,
    signal_type: Iterable[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """One page of the cases this viewer sees and their counts by state.

    A case with no issue visible to the viewer is in neither: a count is data.
    *asset* keeps the cases one of whose issues the viewer sees names that asset
    (case-insensitive), the counts included: the cases on one asset.
    *primary_asset* keeps the cases one of whose visible issues reaches that
    primary asset (#1980 S2): the primary asset page's cases.
    *signal_type* keeps the cases one of whose visible signals has one of those
    trigger paths, ``none`` the cases with no visible signal (#2556); like *state*,
    it narrows the page, not the counts by state.
    """
    if asset and asset.strip():
        cases = [(c, members) for c, members in cases if names_asset(members, view, asset)]
    if primary_asset and primary_asset.strip():
        cases = [(c, members) for c, members in cases if reaches_primary_asset(members, view, primary_asset)]
    rows = [s for s in (summary(c, members, view) for c, members in cases) if s is not None]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    if state:
        rows = [r for r in rows if r["state"] == state]
    if owner_user_id:
        rows = [r for r in rows if r["owner_user_id"] == owner_user_id]
    if priority:
        rows = [r for r in rows if r["priority"] == priority]
    if signal_type:
        rows = [r for r in rows if matches_signal_types(r, signal_type)]
    rows.sort(key=lambda r: (_PRIORITY_RANK.get(r["priority"], 9), -r["open_issue_count"], r["id"]))
    start = max(0, offset)
    return rows[start : start + max(1, limit)], counts


def unseen_cases(cases: list[tuple[CtemCase, list[CtemCaseIssue]]], view: ViewerView) -> dict[str, dict[str, int]]:
    """The cases this viewer sees none of, by state, split into ``hidden`` and ``unjudged`` (#2113).

    ``hidden``: every member was judged by the view and none is the viewer's to see (refused).
    ``unjudged``: a member the view did not judge (its product did not answer, a read hit its cap,
    or the case opened after the view was built): not counted as hidden, never silently dropped.
    For each state, the cases of the store = the list's count + hidden + unjudged.
    """
    out: dict[str, dict[str, int]] = {"hidden": {}, "unjudged": {}}
    for case, members in cases:
        if visible_members(members, view):
            continue
        kind = "unjudged" if any(not view.judges(_member_id(r)) for r in members) else "hidden"
        out[kind][case.state] = out[kind].get(case.state, 0) + 1
    return out


def _open_total(by_state: dict[str, int]) -> int:
    from app.models.ctem_case import OPEN_STATES

    return sum(n for state, n in by_state.items() if state in OPEN_STATES)


async def drop_gone(
    db: Any, cases: list[tuple[CtemCase, list[CtemCaseIssue]]], view: ViewerView
) -> list[tuple[CtemCase, list[CtemCaseIssue]]]:
    """*cases* without the ones deleted after they were read (#2113), read again by id.

    A list reads the cases, then the viewer's view (its signal members in later statements).
    A delete committed in between (a bulk delete releases the case's signals in its own
    transaction) leaves a case read first with no visible member: it would count as hidden.
    Only those candidates are re-read; an unreadable store keeps the list as read.
    """
    candidates = [case.id for case, members in cases if not visible_members(members, view)]
    if not candidates:
        return cases
    try:
        rows = await db.execute(select(CtemCase.id).where(CtemCase.id.in_(candidates)))
        present = set(rows.scalars())
    except Exception:
        logger.info("CTEM cases: deleted-case re-check unreadable", exc_info=True)
        return cases
    gone = set(candidates) - present
    return [(case, members) for case, members in cases if case.id not in gone] if gone else cases


def list_visibility(cases: list[tuple[CtemCase, list[CtemCaseIssue]]], view: ViewerView) -> dict[str, Any]:
    """What a list answer says beside its counts (#2113), so its length is never read as the org total.

    ``hidden_cases``: the open cases this viewer is refused (a count only, never named);
    ``unjudged_cases``: the open cases the view could not judge now (neither listed nor hidden);
    both by state, ``counts_partial`` when some are unjudged, and the view's condition.
    """
    unseen = unseen_cases(cases, view)
    return {
        "hidden_cases": _open_total(unseen["hidden"]),
        "unjudged_cases": _open_total(unseen["unjudged"]),
        "hidden_by_state": unseen["hidden"],
        "unjudged_by_state": unseen["unjudged"],
        "counts_partial": bool(unseen["unjudged"]),
        "view": view.condition(),
    }


def names_asset(rows: list[CtemCaseIssue], view: ViewerView, asset: str) -> bool:
    """Whether an issue of the case this viewer sees names *asset* (case-insensitive, spaces folded)."""
    wanted = " ".join(asset.split()).casefold()
    for row in visible_members(rows, view):
        issue = view.issues.get(_member_id(row))
        if issue is not None and any(" ".join(a.split()).casefold() == wanted for a in issue.assets):
            return True
    return False


def reaches_primary_asset(rows: list[CtemCaseIssue], view: ViewerView, primary_asset: str) -> bool:
    """Whether an issue of the case this viewer sees reaches *primary_asset* (case-insensitive, spaces folded)."""
    wanted = " ".join(primary_asset.split()).casefold()
    for row in visible_members(rows, view):
        issue = view.issues.get(_member_id(row))
        if issue is not None and any(" ".join(p.split()).casefold() == wanted for p in issue.primary_assets):
            return True
    return False


async def acts_on_case(db: Any, user: Any, rows: list[CtemCaseIssue], vertical: str) -> bool:
    """Whether *user_id* may change a case they see (#1748, #2441).

    An administrator or a CTEM decide right only: a view right is strictly
    read only (Sam, 24 Nov 2026) and no right is no CTEM at all (#2441).
    *rows* and *vertical* are kept for the callers' symmetry with the read.
    """
    from app.services import ctem_access

    return await ctem_access.may_decide(db, user)


def owner_sees(rows: list[CtemCaseIssue], owner_view: ViewerView) -> bool:
    """Whether the would-be owner sees at least one issue of the case in the source products."""
    return bool(visible_members(rows, owner_view))


async def user_sees_case(db: Any, user_id: Any, case_id: Any, vertical: Any = None) -> bool:
    """Whether *user_id* may open case *case_id*: the API's rule (a 404 otherwise), from their own view.

    The platform events of a case reach only such users (#933). *vertical* is
    ignored: the case's own vertical decides the view read. Any unknown or
    unreadable input answers ``False``.
    """
    import uuid

    from app.services import ctem_cases_store as store
    from app.services.organizations import get_primary

    try:
        case_uuid = uuid.UUID(str(case_id))
        uuid.UUID(str(user_id))
    except (ValueError, TypeError):
        return False
    from app.services import ctem_access

    # No CTEM without a right (#2441), re-read at delivery time.
    if await ctem_access.level(db, str(user_id)) is None:
        return False
    primary = await get_primary(db)
    if primary is None:
        return False
    found = await store.get_case(db, case_uuid, organization_id=primary.id)
    if found is None:
        return False
    case, rows, _events = found
    # One case's members (#2092): an older view or that case's own read, never a whole
    # view build per event while a drain bumps the generation.
    members = [_member_id(r) for r in rows]
    return bool(visible_members(rows, await viewer_view(db, str(user_id), case.vertical, members=members)))
