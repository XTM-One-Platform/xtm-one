# SPDX-License-Identifier: Apache-2.0

"""CTEM signals API (#826): record, list and read the changes in exposure that start the loop.

XTM One mode only, no model call. ``POST`` records a signal for the caller,
idempotent on its dedupe key (the same key again answers ``created: false``
with ``last_seen_at`` moved). An administrator reads every signal of the
organization, as does a member of a group with a CTEM view or decide right;
anyone else gets 403 on every route (#2441). The rules are
``services/ctem_signals.py``; the design ``dev-docs/ctem-signals.md``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.config import settings
from app.core.database import get_db
from app.models.ctem_signal import LENSES, SIGNAL_STATES, SUBJECT_KINDS, TRIGGER_PATHS
from app.models.user import User
from app.services import ctem_access
from app.services import ctem_case_loop as case_loop
from app.services import ctem_scope_gate_store as scope_gate
from app.services import ctem_signals as signals
from app.services.ctem_loop_stream import publish_signal_update
from app.services.organizations import get_primary

router = APIRouter()

TriggerPath = Literal[TRIGGER_PATHS]  # type: ignore[valid-type]
Lens = Literal[LENSES]  # type: ignore[valid-type]
SignalState = Literal[SIGNAL_STATES]  # type: ignore[valid-type]
SubjectKind = Literal[SUBJECT_KINDS]  # type: ignore[valid-type]


class SignalSource(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    external_id: str | None = Field(default=None, max_length=300)


class SignalSubject(BaseModel):
    kind: SubjectKind
    ref: str = Field(min_length=1, max_length=300)
    label: str | None = Field(default=None, max_length=300)


class SignalCreate(BaseModel):
    """One change in exposure; the lens defaults to the path's usual one."""

    trigger_path: TriggerPath
    lens: Lens | None = None
    source: SignalSource
    subject: SignalSubject
    observed_at: datetime | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str | None = Field(default=None, max_length=200)


def _require_xtm_one() -> None:
    if settings.is_copilot_like:
        raise HTTPException(status_code=404, detail="CTEM signals are only available in XTM One mode")


async def _reader(db: AsyncSession, user: User) -> str | None:
    """``None``: the organization's signals (an administrator, a CTEM view or decide right); 403 without a right (#2441)."""
    try:
        return await ctem_access.signal_reader(db, user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None


async def _writer(db: AsyncSession, user: User) -> str | None:
    """``None``: acts on the organization's signals (an administrator, a CTEM decide right); else 403 (#2441)."""
    try:
        return await ctem_access.signal_writer(db, user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None


async def _require_ctem_right(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Every route of the signal API: 403 for a person with no CTEM right (#2441), in XTM One mode."""
    if settings.is_copilot_like:
        return  # each route answers its own 404 there
    reason = await ctem_access.refusal(db, user, write=False)
    if reason is not None:
        raise HTTPException(status_code=403, detail=reason)


router.dependencies.append(Depends(_require_ctem_right))


async def _organization_id(db: AsyncSession) -> uuid.UUID:
    primary = await get_primary(db)
    if primary is None:
        raise HTTPException(status_code=404, detail="No organization set up yet")
    return primary.id


@router.get("/paths")
async def list_ctem_signal_paths(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """The 12 trigger paths, their default lens, whether a listener feeds them, and the signals the caller sees per path.

    Each path also names its sources and whether each is bound for the caller,
    its status, and its signals of the last 30 days (#1189, the signal sources dialog).
    """
    _require_xtm_one()
    from app.services import ctem_webhook_sources as webhook_sources
    from app.services.ctem_signal_sources import bound_sources

    primary = await get_primary(db)
    counts: dict[str, dict[str, int]] = {}
    activity: dict[str, dict[str, Any]] = {}
    by_source: dict[str, dict[str, dict[str, Any]]] = {}
    quiet: dict[str, dict[str, Any]] = {}
    if primary is not None:
        from app.services.ctem_attack_surface import quiet_reasons

        reader = await _reader(db, user)
        counts = await signals.counts_by_path(db, organization_id=primary.id, recorded_by=reader)
        since = datetime.now(UTC) - timedelta(days=30)
        activity = await signals.activity_by_path(db, organization_id=primary.id, recorded_by=reader, since=since)
        by_source = await signals.activity_by_source(db, organization_id=primary.id, recorded_by=reader, since=since)
        from app.services import ctem_signal_purge

        job = await ctem_signal_purge.read_status() or {}
        replaying = job.get("state") == "running" and bool(job.get("resync"))
        quiet = await quiet_reasons(db, primary.id, resync_running=replaying)
    bound = await bound_sources(db, user.id)
    webhooks = webhook_sources.path_sources_of(await webhook_sources.list_sources(db))
    # #2642: path 6's own inventory evaluation says why the path records nothing.
    from app.services.ctem_inventory_staleness import last_outcome

    evaluations = {"stale_asset": await last_outcome()}
    return {
        "paths": signals.path_catalog(
            counts,
            bound=bound,
            activity=activity,
            by_source=by_source,
            webhooks=webhooks,
            quiet=quiet,
            evaluations=evaluations,
        )
    }


class SignalSourceEnable(BaseModel):
    """Switch one platform signal source on (#2434): its integration and the user it provisions as."""

    provider: str = Field(min_length=1, max_length=40)
    kind: str = Field(min_length=1, max_length=20)
    integration_id: uuid.UUID
    run_as_user_id: uuid.UUID


@router.get("/sources")
async def list_ctem_signal_sources(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """The platform signal sources XTM One provisions, with switch, run-as, status and reason (#2434).

    One row per provisioned ``(provider, kind)`` and connected integration of
    that provider; a source never switched on reads ``off``.
    """
    _require_xtm_one()
    from sqlalchemy import select

    from app.models.assignment import Assignment
    from app.models.ctem_signal_source import CtemSignalSource
    from app.models.integration import Integration
    from app.services import ctem_platform_sources as platform_sources
    from app.services.integration_tools import get_integration_public_url
    from app.services.opencti_trigger_provisioning import legacy_pending

    providers = sorted({p for p, _ in platform_sources.PROVISIONED})
    # Legacy Threat response objects XTM One still holds, per OpenCTI integration (#2627): the
    # "Remove legacy OpenCTI objects" action shows only while some are left.
    pending = await legacy_pending(db)
    integrations = (
        (
            await db.execute(
                select(Integration).where(Integration.provider.in_(providers)).order_by(Integration.created_at)
            )
        )
        .scalars()
        .all()
    )
    stored = {
        (r.provider, r.kind, str(r.integration_id)): r
        for r in (await db.execute(select(CtemSignalSource))).scalars().all()
    }
    configs = (
        (await db.execute(select(Assignment.trigger_config).where(Assignment.trigger_type == "integration_event")))
        .scalars()
        .all()
    )
    out: list[dict[str, Any]] = []
    names: dict[str, str | None] = {}
    for (provider, kind), spec in platform_sources.PROVISIONED.items():
        for integration in integrations:
            if integration.provider != provider:
                continue
            row = stored.get((provider, kind, str(integration.id)))
            item = (
                platform_sources.to_dict(row)
                if row is not None
                else {
                    "id": None,
                    "provider": provider,
                    "kind": kind,
                    "integration_id": str(integration.id),
                    "enabled": False,
                    "run_as_user_id": None,
                    "status": "off",
                    "reason": None,
                    "reason_code": None,
                    "reason_params": {},
                    "checked_at": None,
                    "objects": [],
                    "feeds": platform_sources.fed_paths(provider, kind),
                }
            )
            # "Runs as" of the source row (#2650).
            run_as_id = item.get("run_as_user_id")
            if run_as_id and run_as_id not in names:
                person = await db.get(User, uuid.UUID(run_as_id))
                names[run_as_id] = (getattr(person, "full_name", None) or person.email) if person else None
            item["run_as_name"] = names.get(run_as_id) if run_as_id else None
            item["integration_name"] = integration.name
            # The platform's web UI host, for the Open in links of its objects (#2583 point 2).
            item["integration_url"] = get_integration_public_url(integration)
            item["integration_connected"] = integration.status == "connected"
            item["label"] = spec.label
            # #2640: the objects the fleets provisioned on the same integration are listed apart, with
            # their owner, once per integration (on its listener row); a source row counts only its own.
            others = platform_sources.fleet_objects(configs, provider, str(integration.id))
            item["objects"] = platform_sources.with_shared_events(item.get("objects") or [], others)
            item["fleet_objects"] = others if kind == "listener" else []
            if provider == "opencti":
                item["legacy_pending"] = pending.get(str(integration.id), 0)
            out.append(item)
    return {"sources": out}


@router.post("/sources/enable")
async def enable_ctem_signal_source(
    body: SignalSourceEnable,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Switch a source on as the run-as user and provision it at once; 400 with the exact reason when it cannot.

    The 400 ``detail`` is ``{code, params, message}`` (``ctem_platform_sources.reason_code``):
    the UI renders the code in the reader's language, ``message`` is the English line.
    """
    _require_xtm_one()
    from app.services import ctem_platform_sources as platform_sources
    from app.services import module_run_as

    # #2650: the user chosen in the "Act in <module> as" dialog, checked before any object is made.
    try:
        run_as = await module_run_as.resolve(db, actor=user, raw=body.run_as_user_id, providers=[body.provider])
    except module_run_as.RunAsRefusedError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    try:
        row = await platform_sources.enable(
            db,
            provider=body.provider,
            kind=body.kind,
            integration_id=body.integration_id,
            run_as_user_id=run_as,
        )
    except platform_sources.SourceRefusedError as exc:
        await db.commit()
        code, params = platform_sources.reason_code(str(exc))
        raise HTTPException(status_code=400, detail={"code": code, "params": params, "message": str(exc)}) from exc
    await db.commit()
    return platform_sources.to_dict(row)


@router.post("/sources/{source_id}/disable")
async def disable_ctem_signal_source(
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Switch a source off and remove what it provisioned in the source platform."""
    _require_xtm_one()
    from app.models.ctem_signal_source import CtemSignalSource
    from app.services import ctem_platform_sources as platform_sources

    row = await db.get(CtemSignalSource, source_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Signal source not found")
    await platform_sources.disable(db, row)
    await db.commit()
    return platform_sources.to_dict(row)


@router.post("/sources/legacy-opencti-objects")
async def remove_legacy_opencti_objects(
    dry_run: bool = Query(True, description="List what would be removed without removing it"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Remove legacy OpenCTI objects (#2583 point 4): the Threat response triggers, playbooks and
    notifiers left in OpenCTI, per integration a Signals source is on for; ``dry_run`` (default) lists them.

    A holder whose objects are removed while its Signals source is not connected yet provisions them
    again at its next pass, so no delivery is lost.
    """
    _require_xtm_one()
    from app.services.opencti_trigger_provisioning import legacy_cleanup

    report = await legacy_cleanup(db, dry_run=dry_run)
    await db.commit()
    return {"dry_run": dry_run, "integrations": report}


class WebhookMapping(BaseModel):
    """Where each signal field is read in the posted JSON: dotted paths (``event.id``, ``items.0.name``)."""

    id: str = Field(min_length=1, max_length=200)
    label: str | None = Field(default=None, max_length=200)
    severity: str | None = Field(default=None, max_length=200)


class WebhookSourceCreate(BaseModel):
    """A generic signed webhook source of one trigger path (#2506): created off."""

    trigger_path: TriggerPath
    name: str = Field(min_length=1, max_length=120)
    subject_kind: SubjectKind = "asset"
    mapping: WebhookMapping


class WebhookSourceUpdate(BaseModel):
    """The switch, name or mapping of a webhook source; the fields left out are kept."""

    enabled: bool | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    subject_kind: SubjectKind | None = None
    mapping: WebhookMapping | None = None


async def _webhook_source(db: AsyncSession, source_id: uuid.UUID) -> Any:
    from app.models.ctem_webhook_source import CtemWebhookSource

    row = await db.get(CtemWebhookSource, source_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Webhook source not found")
    return row


def _with_url(item: dict[str, Any]) -> dict[str, Any]:
    item["receiver_url"] = f"{settings.base_url.rstrip('/')}{item['receiver_path']}"
    return item


@router.get("/webhook-sources")
async def list_ctem_webhook_sources(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """The generic signed webhook sources of the trigger paths (#2506), never their secrets."""
    _require_xtm_one()
    from app.services import ctem_webhook_sources as webhook_sources

    return {"sources": [_with_url(webhook_sources.to_dict(r)) for r in await webhook_sources.list_sources(db)]}


@router.post("/webhook-sources")
async def create_ctem_webhook_source(
    body: WebhookSourceCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Create a webhook source, off; the answer carries its ``secret`` once, never again."""
    _require_xtm_one()
    from app.services import ctem_webhook_sources as webhook_sources

    try:
        row, secret = await webhook_sources.create(
            db,
            trigger_path=body.trigger_path,
            name=body.name,
            subject_kind=body.subject_kind,
            mapping=body.mapping.model_dump(),
            created_by=user.id,
        )
    except webhook_sources.WebhookSourceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await db.commit()
    return {**_with_url(webhook_sources.to_dict(row)), "secret": secret}


@router.patch("/webhook-sources/{source_id}")
async def update_ctem_webhook_source(
    source_id: uuid.UUID,
    body: WebhookSourceUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Switch a webhook source on or off, rename it or change its mapping."""
    _require_xtm_one()
    from app.services import ctem_webhook_sources as webhook_sources

    row = await _webhook_source(db, source_id)
    try:
        if body.mapping is not None:
            row.mapping = webhook_sources.clean_mapping(body.mapping.model_dump())
        if body.name is not None or body.subject_kind is not None:
            row.name = webhook_sources.check_new(
                row.trigger_path, body.name or row.name, body.subject_kind or row.subject_kind
            )
            row.subject_kind = body.subject_kind or row.subject_kind
    except webhook_sources.WebhookSourceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.enabled is not None:
        row.enabled = body.enabled
    await db.commit()
    return _with_url(webhook_sources.to_dict(row))


@router.post("/webhook-sources/{source_id}/rotate")
async def rotate_ctem_webhook_source(
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """A new secret for the source; the old one is refused from now on. The answer carries it once."""
    _require_xtm_one()
    from app.services import ctem_webhook_sources as webhook_sources

    row = await _webhook_source(db, source_id)
    secret = webhook_sources.rotate(row)
    await db.commit()
    return {**_with_url(webhook_sources.to_dict(row)), "secret": secret}


class WebhookSourceTest(BaseModel):
    """A sample of the JSON the tool posts, mapped without recording anything."""

    sample: dict[str, Any] | list[Any]


@router.post("/webhook-sources/{source_id}/test")
async def test_ctem_webhook_source(
    source_id: uuid.UUID,
    body: WebhookSourceTest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """What the source's mapping reads in *sample*: the id, label and severity, or why it reads no id. Records nothing."""
    _require_xtm_one()
    from app.services import ctem_webhook_sources as webhook_sources

    row = await _webhook_source(db, source_id)
    try:
        mapped = webhook_sources.map_payload(row.mapping, body.sample)
    except webhook_sources.WebhookSourceError as exc:
        return {"ok": False, "reason": str(exc)}
    return {"ok": True, "id": mapped.external_id, "label": mapped.label, "severity": mapped.severity}


@router.delete("/webhook-sources/{source_id}")
async def delete_ctem_webhook_source(
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Delete a webhook source: its receiver answers 404 from now on; the signals it recorded stay."""
    _require_xtm_one()
    row = await _webhook_source(db, source_id)
    await db.delete(row)
    await db.commit()
    return {"deleted": True}


@router.get("")
async def list_ctem_signals(
    state: SignalState | None = Query(default=None),
    trigger_path: TriggerPath | None = Query(default=None),
    lens: Lens | None = Query(default=None),
    case_id: uuid.UUID | None = Query(default=None),
    source_provider: str | None = Query(default=None, max_length=40),
    since: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=signals.MAX_PAGE),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """One page of signals, newest sighting first, with the total and the counts by state.

    ``source_provider`` keeps one provider; ``since`` keeps the signals last seen at or after it (#1782).
    """
    _require_xtm_one()
    primary = await get_primary(db)
    if primary is None:
        return {"total": 0, "counts": {s: 0 for s in SIGNAL_STATES}, "signals": []}
    rows, total, counts = await signals.list_signals(
        db,
        organization_id=primary.id,
        recorded_by=await _reader(db, user),
        state=state,
        trigger_path=trigger_path,
        lens=lens,
        case_id=case_id,
        source_provider=source_provider,
        since=since if since is None or since.tzinfo else since.replace(tzinfo=UTC),
        limit=limit,
        offset=offset,
    )
    return {"total": total, "counts": counts, "signals": [signals.to_dict(r) for r in rows]}


@router.post("")
async def record_ctem_signal(
    body: SignalCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Record one signal for the caller; the same dedupe key again moves ``last_seen_at``.

    A CTEM decide right (or an administrator) only (#2441): a view right is read only.
    """
    _require_xtm_one()
    reason = await ctem_access.refusal(db, user, write=True)
    if reason is not None:
        raise HTTPException(status_code=403, detail=reason)
    organization_id = await _organization_id(db)
    try:
        row, created = await signals.record_signal(
            db,
            organization_id=organization_id,
            signal=signals.SignalInput(
                trigger_path=body.trigger_path,
                lens=body.lens or "",
                source_provider=body.source.provider,
                source_external_id=body.source.external_id,
                subject_kind=body.subject.kind,
                subject_ref=body.subject.ref,
                subject_label=body.subject.label,
                observed_at=body.observed_at,
                evidence=body.evidence,
                dedupe_key=body.dedupe_key,
            ),
            recorded_via="api",
            recorded_by=str(user.id),
        )
    except signals.SignalError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    pending = await scope_gate.evaluate_after_record(db, row, created=created)
    await db.commit()
    await case_loop.publish_pending(pending)
    await publish_signal_update(db, row)
    return {"created": created, "signal": signals.to_dict(row)}


@router.post("/evaluate")
async def evaluate_ctem_signals(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Run the reconcile and scope gate again on every open signal (after a policy change); counts by state."""
    _require_xtm_one()
    pending = case_loop.PendingEvents()
    counts = await scope_gate.evaluate_open(
        db, organization_id=await _organization_id(db), user_id=admin.id, events=pending
    )
    await db.commit()
    await case_loop.publish_pending(pending)  # ctem_signal.triaged per changed verdict (#1409)
    return {"evaluated": sum(counts.values()), "counts": counts}


class SignalPurge(BaseModel):
    """Start the purge of every signal (#2528): the typed confirmation word, and whether to resync after it."""

    confirm: str = Field(max_length=40)
    #: Resync the signals from their sources after the purge (default off).
    resync: bool = False
    #: The resync read window per route in days (#2578); a route left out takes its default,
    #: ``null`` on an inside-state route is no age limit. Validated by the server.
    resync_windows: dict[str, int | None] | None = None
    #: Per resync route or pass (#2745): ``{enabled, window, unit}``, each optional. ``enabled``
    #: false leaves it out of the resync (not queried at all); ``unit`` is ``days`` (default) or
    #: ``hours``. Overlaid on ``resync_windows``; validated by the server.
    resync_sources: dict[str, dict[str, Any]] | None = None


@router.get("/purge")
async def get_ctem_signal_purge(
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """What a purge would delete now (``{signals, cases}``), the confirmation word, the resync routes
    with their default windows (#2578), and the running or last job."""
    _require_xtm_one()
    from app.services import ctem_signal_purge as purge
    from app.services import ctem_signal_resync as resync

    return {
        "counts": await purge.counts(db),
        "confirm_word": purge.CONFIRM_WORD,
        "resync_routes": resync.describe_windows(),
        "resync_max_days": resync.MAX_WINDOW_DAYS,
        "resync_units": list(resync.UNITS),
        "job": await purge.read_status(),
    }


@router.post("/purge", status_code=202)
async def purge_ctem_signals(
    body: SignalPurge,
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Purge every signal and the cases they opened or joined, administrators only (#2528).

    One job at a time, in the background: intake paused, cases then signals deleted
    in batches with the raised-key state of the passes, an optional resync, intake
    resumed whatever happened. 422 without the confirmation word, 409 while a job runs.
    Answers the job's first status; ``GET /purge`` follows it.
    """
    _require_xtm_one()
    from app.services import ctem_signal_purge as purge

    if body.confirm.strip() != purge.CONFIRM_WORD:
        raise HTTPException(status_code=422, detail=f"Type {purge.CONFIRM_WORD} to confirm the purge")
    from app.services import ctem_signal_resync

    windows: dict[str, int | None] | None = None
    planned: dict[str, Any] = {}
    try:
        if body.resync and body.resync_sources is not None:
            planned = ctem_signal_resync.plan(body.resync_windows, body.resync_sources)
            windows = planned["windows"]
        elif body.resync:
            windows = ctem_signal_resync.validate_windows(body.resync_windows)
    except ctem_signal_resync.WindowError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    try:
        job = await purge.start(
            admin, resync=body.resync, windows=windows, spans=planned.get("spans"), skip=planned.get("skip")
        )
    except purge.PurgeBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"job": job}


@router.get("/attack-surface/passes")
async def list_attack_surface_passes(
    limit: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """The latest attack surface passes of the outside lens (#1498), newest first (administrators).

    Each pass names its providers, what each read covered (pages, records,
    truncated) per owned domain or range, the hosts observed and the signals per
    path, including a pass that found nothing. No pass at all answers ``why_empty``.
    """
    _require_xtm_one()
    from app.services import ctem_attack_surface as surface

    organization_id = await _organization_id(db)
    rows = await surface.list_passes(db, organization_id, limit=limit)
    answer: dict[str, Any] = {"passes": [surface.pass_to_dict(r) for r in rows]}
    if not rows:
        answer["why_empty"] = await surface.why_no_pass(db, organization_id)
    return answer


class SurfaceRepair(BaseModel):
    #: ``true`` (default) answers what the repair would discard and writes nothing.
    dry_run: bool = True


@router.post("/attack-surface/repair-new-hosts")
async def repair_attack_surface_new_hosts(
    body: SurfaceRepair,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Discard the path 7 new-host signals raised without a complete previous read (#1498).

    One-off repair, administrators: the hosts stay in the baseline; a signal
    that opened a case is left to the case step. ``dry_run`` answers the hosts.
    """
    _require_xtm_one()
    from app.services import ctem_attack_surface as surface

    result = await surface.repair_partial_read_new_hosts(db, await _organization_id(db), dry_run=body.dry_run)
    # A dry run writes nothing, so the commit is a no-op there; committing on every path
    # keeps the writes visible before the response is sent.
    await db.commit()
    return result


@router.post("/{signal_id}/evaluate")
async def evaluate_ctem_signal(
    signal_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Run the reconcile and scope gate again on one signal, OpenCRQ risk scenarios read with its
    recorder's rights (a system signal: the administrator's, #933); 409 once it opened a case."""
    _require_xtm_one()
    row = await signals.get_signal(db, signal_id, organization_id=await _organization_id(db), recorded_by=None)
    if row is None:
        raise HTTPException(status_code=404, detail="No such signal")
    pending = case_loop.PendingEvents()
    decision = await scope_gate.evaluate_one(db, row, evaluator_id=admin.id, events=pending)
    if decision is None:
        raise HTTPException(status_code=409, detail="This signal opened a case: the case step owns it now")
    await db.commit()
    await case_loop.publish_pending(pending)  # ctem_signal.triaged when the verdict changed (#1409)
    return signals.to_dict(row)


class TriageFilter(BaseModel):
    """Every signal the caller sees in one state (and trigger path), as the Signals page filters them."""

    state: SignalState | None = None
    trigger_path: TriggerPath | None = None


class SignalTriage(BaseModel):
    """Dismiss, restore or re-check signals (#1505): by id, or every signal a filter matches."""

    action: Literal[scope_gate.TRIAGE_ACTIONS]  # type: ignore[valid-type]
    ids: list[uuid.UUID] = Field(default_factory=list, max_length=scope_gate.MAX_BULK)
    filter: TriageFilter | None = None
    #: Run the same selection and decisions, answer each signal's outcome, write nothing (#2341).
    dry_run: bool = False


@router.post("/triage")
async def triage_ctem_signals(
    body: SignalTriage,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Dismiss, restore or re-check the signals the caller may read (#1505), at most 2000 per call.

    Anyone who reads signals triages the ones they read (an administrator: the
    organization's). A signal that opened a case is skipped. A confirmed signal
    opens its case at the next sweep, so a bulk re-check never floods cases.
    """
    _require_xtm_one()
    if bool(body.ids) == (body.filter is not None):
        raise HTTPException(status_code=422, detail="Give either ids or a filter")
    organization_id = await _organization_id(db)
    rows = await signals.select_signals(
        db,
        organization_id=organization_id,
        recorded_by=await _writer(db, user),
        ids=body.ids or None,
        state=body.filter.state if body.filter else None,
        trigger_path=body.filter.trigger_path if body.filter else None,
        limit=scope_gate.MAX_BULK,
    )
    actor = (getattr(user, "name", None) or getattr(user, "email", None) or str(user.id))[:200]
    if body.dry_run:
        outcomes: list[dict[str, Any]] = []
        counts = await scope_gate.triage(
            db, rows, action=body.action, actor=actor, evaluator_id=user.id, outcomes=outcomes
        )
        await db.rollback()
        return {"dry_run": True, "matched": len(rows), "counts": counts, "signals": outcomes}
    counts = await scope_gate.triage(db, rows, action=body.action, actor=actor, evaluator_id=user.id)
    await db.commit()
    return {"matched": len(rows), "counts": counts}


class SignalInventoryAdd(BaseModel):
    """Add the subjects of unmatched signals to the inventory (#1505): by id, or every signal a filter matches."""

    ids: list[uuid.UUID] = Field(default_factory=list, max_length=scope_gate.MAX_BULK)
    filter: TriageFilter | None = None


@router.post("/add-to-inventory")
async def add_ctem_signals_to_inventory(
    body: SignalInventoryAdd,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Declare the subjects of the selected unmatched signals in the organization profile, then decide them again.

    An administrator only: it writes one profile version (a host becomes an
    asset, a software a software product, an identity its email domain as an
    owned domain). Only unmatched (``to_validate``) signals count; the others
    are left as they are. A signal the gate confirms opens its case at the
    next sweep, never one case per signal here. 422 when the profile would
    not validate.
    """
    _require_xtm_one()
    if bool(body.ids) == (body.filter is not None):
        raise HTTPException(status_code=422, detail="Give either ids or a filter")
    organization_id = await _organization_id(db)
    rows = await signals.select_signals(
        db,
        organization_id=organization_id,
        recorded_by=None,
        ids=body.ids or None,
        state=body.filter.state if body.filter else None,
        trigger_path=body.filter.trigger_path if body.filter else None,
        limit=scope_gate.MAX_BULK,
    )
    actor = (getattr(admin, "name", None) or getattr(admin, "email", None) or str(admin.id))[:200]
    try:
        result = await scope_gate.add_to_inventory(
            db, rows, organization_id=organization_id, admin_id=admin.id, actor=actor
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await db.commit()
    return {"matched": len(rows), **result}


#: The scope gate outcomes a scope proposal answers: the subject's criticality was known and below the threshold.
SCOPE_PROPOSAL_CODES = frozenset({"below_scope_threshold"})


@router.post("/{signal_id}/propose-scope")
async def propose_ctem_signal_scope(
    signal_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Propose to lower the scope threshold to this watchlisted signal's criticality (CTEM-30, #937).

    Stores one organization context proposal and nothing else: the profile
    changes only when an administrator accepts it in Settings > Organization.
    It is a suggestion, never a gate. 404 for a signal the caller cannot read,
    409 when the signal is not on the watchlist below the threshold.
    """
    _require_xtm_one()
    from app.services import organization_context_proposals as proposals

    row = await signals.get_signal(
        db, signal_id, organization_id=await _organization_id(db), recorded_by=await _writer(db, user)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No such signal")
    decision = row.decision or {}
    criticality = ((decision.get("scope") or {}).get("criticality")) or ""
    if row.state != "watchlist" or decision.get("reason_code") not in SCOPE_PROPOSAL_CODES or not criticality:
        raise HTTPException(
            status_code=409, detail="Only a watchlisted signal below the scope threshold can propose a scope change"
        )
    label = row.subject_label or row.subject_ref
    try:
        proposal = await proposals.propose(
            db,
            author_user_id=user.id,
            changes=[
                {
                    "field": "prioritization_policy.scope_min_criticality",
                    "value": criticality,
                    "basis": f"Watchlisted CTEM signal on {label}: criticality {criticality} below the scope threshold",
                }
            ],
        )
    except proposals.ProposalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return {
        "proposal_id": str(proposal.id),
        "status": proposal.status,
        "field": "prioritization_policy.scope_min_criticality",
        "value": criticality,
    }


@router.get("/{signal_id}/primary-assets/{primary_asset}/supporting-assets")
async def list_ctem_signal_supporting_assets(
    signal_id: uuid.UUID,
    primary_asset: str,
    q: str = Query(default="", max_length=200),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=scope_gate.SUPPORTING_PAGE_MAX),
    sort: Literal["asset", "through"] = Query(default="asset"),
    direction: Literal["asc", "desc"] = Query(default="asc"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """The inventory assets through which one signal reaches one primary asset, paged (#1979).

    The loop trace's primary asset card lists them with their real total, where
    the decision record keeps only the first chains. Same read rule as the
    signal itself: 404 for a signal the caller cannot read. *primary_asset* is
    its name or key; *q* filters by asset or group name; *sort* and *direction*
    order the whole list before the page is cut (UX r1 B3 on #1995).
    """
    _require_xtm_one()
    row = await signals.get_signal(
        db, signal_id, organization_id=await _organization_id(db), recorded_by=await _reader(db, user)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No such signal")
    return await scope_gate.supporting_assets(
        db, row, primary_asset, query=q, offset=offset, limit=limit, sort=sort, direction=direction
    )


@router.get("/{signal_id}")
async def get_ctem_signal(
    signal_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """One signal the caller may see; 404 otherwise.

    An unmatched (``to_validate``) signal also carries ``unmatched_retention_days``
    (#2726), the window after which an unseen one is deleted, for its next step.
    """
    _require_xtm_one()
    row = await signals.get_signal(
        db, signal_id, organization_id=await _organization_id(db), recorded_by=await _reader(db, user)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No such signal")
    shape = signals.to_dict(row)
    if row.state == "to_validate":
        from app.services.coordination_retention import unmatched_signal_retention_days

        shape["unmatched_retention_days"] = await unmatched_signal_retention_days(db)
    return shape
