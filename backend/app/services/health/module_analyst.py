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

"""Platform Health Module Analyst (#101 S14, ``dev-docs/platform-health.md`` section 12).

The member that investigates a registered OpenCTI, OpenAEV or OpenCRQ through the
integration its registration created, read-only, and attaches recommendations to
that module's findings. This module holds what decides its reach, so the rule lives
in code and never in a prompt sentence:

* :data:`MODULE_CHECKER_TOOLS`, the explicit allow-list of read tools per provider
  (section 12.3, lock 2). A read tool added later to a module integration is not
  reachable until it is listed here; a name outside the list is refused.
* :func:`allowed_module_tools` and :func:`refuse_module_tool`, applied where the
  member's tools are built and where they execute.
* :func:`registration_in_scope`, the ``module`` target check of ``cycles.start_cycle``.
* :func:`module_analyst_tick`, the triggers of section 12.2 and the orchestrator's
  daily module round of section 13.2: a cycle per registration when its open finding
  set changed (debounced by ``change_min_interval_minutes``), and the round, once a UTC
  day, which hands every registration not covered that day to its module's checker,
  findings or not. The state of each registration is a ``HealthSignalState`` row
  (kind :data:`STATE_KIND`, target type ``module``); the pass mark is
  ``health_schedule_marks`` key :data:`TRIGGER_MARK`.

S14a-1 shipped the store, the role, the target type and the allow-list, S14a-2a the
seed, S14a-2b its tools and triggers; the quota exemption and its ceiling follow in S14b.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.tool_classification import infer_tool_type

logger = logging.getLogger(__name__)

#: The modules the analyst investigates: ``PlatformRegistration.platform_identifier``,
#: also the provider key of the integration the registration created.
MODULE_PROVIDERS: Final[tuple[str, ...]] = ("opencti", "openaev", "opencrq")

#: The read tools a module checker may call, per provider (section 12.3, lock 2;
#: the list of section 13.5). Every entry must classify as a read
#: (``tool_classification.infer_tool_type``): ``tests/test_health_module_analyst.py``
#: fails on one that does not. Module-only (section 13.4 scope, Sam, 22 Nov 2026): no
#: tool here reads a specific connector, feed, collector, injector, executor or
#: third-party data source; those tools stay in their providers for other agents.
MODULE_CHECKER_TOOLS: Final[dict[str, tuple[str, ...]]] = {
    # Playbooks through the read-only state tool of ``providers/opencti/health_reads.py``
    # (S14a-2b-3): ``manage_opencti_playbook`` reaches the same objects but is a write.
    "opencti": (
        "get_opencti_platform_version",
        "list_opencti_playbook_states",
        # The module health reads of S15b-2 (same file), platform-wide.
        "list_opencti_background_tasks",
        "get_opencti_managers",
        "get_opencti_platform_queues",
        "list_opencti_stream_consumers",
        "get_opencti_licence",
        # S15b-2b: data health and error rate, platform-wide (same file).
        "get_opencti_data_health",
        "get_opencti_error_rate",
        "get_opencti_entity_stats",
        "query_opencti_activity",
        "list_opencti_drafts",
    ),
    "openaev": (
        "search_openaev_exercises",
        "get_openaev_exercise",
        "search_openaev_scenarios",
        "search_openaev_findings",
        "search_openaev_assets",
        "search_openaev_asset_groups",
        # S15b-2c: module health reads of ``providers/openaev/health_reads.py``.
        "get_openaev_platform_health",
        "list_openaev_expectations_due",
        # S15b-2c2: data health and error rate of the same file.
        "get_openaev_data_health",
        "get_openaev_error_rate",
    ),
    "opencrq": (
        "get_opencrq_platform_version",
        "list_opencrq_jobs",
        "get_opencrq_job",
        "list_opencrq_crq_run_history",
        "get_opencrq_crq_run",
        "get_opencrq_crq_settings",
        "list_opencrq_risk_scenarios",
        "get_opencrq_tef_input",
        "get_opencrq_financial_profile",
        # S15b-2d: module health reads of ``providers/opencrq/health_reads.py``.
        "get_opencrq_data_health",
        "get_opencrq_platform_health",
    ),
}

#: The refusal a tool outside the allow-list answers, the read-only reason.
REFUSAL_REASON: Final = (
    "A module health checker reads modules only through its allow-listed read tools; this tool is not one of them."
)


def allowed_module_tools(provider: str) -> frozenset[str]:
    """The allow-listed read tools of *provider*; empty for a provider the analyst does not read."""
    return frozenset(MODULE_CHECKER_TOOLS.get(provider, ()))


def filter_module_tools(provider: str, tool_names: Iterable[str]) -> list[str]:
    """*tool_names* the member may see for *provider*: allow-listed and read, in order."""
    allowed = allowed_module_tools(provider)
    return [name for name in tool_names if name in allowed and infer_tool_type(name) == "read"]


def refuse_module_tool(provider: str, tool_name: str) -> str | None:
    """The refusal for a call of *tool_name* on *provider*, ``None`` when it may run.

    Both locks of section 12.3 are checked: the name is on the allow-list, and the
    shared classifier reads it as a read (so a mislisted write is still refused).
    """
    if tool_name not in allowed_module_tools(provider) or infer_tool_type(tool_name) != "read":
        return REFUSAL_REASON
    return None


async def registration_in_scope(db: AsyncSession, registration_id: uuid.UUID) -> bool | None:
    """Whether a ``module`` target may get a cycle; ``None`` when it is no module registration.

    In scope: an active registration of a module the analyst reads, with the
    integration it reads through (section 12.2: an inactive one gets no cycle).
    """
    from app.models.platform_registration import PlatformRegistration

    row = (
        await db.execute(
            select(
                PlatformRegistration.platform_identifier,
                PlatformRegistration.status,
                PlatformRegistration.integration_id,
            ).where(PlatformRegistration.id == registration_id)
        )
    ).one_or_none()
    if row is None or row.platform_identifier not in MODULE_PROVIDERS:
        return None
    return row.status == "active" and row.integration_id is not None


# ── Triggers (section 12.2) ──────────────────────────────────────────────────

#: The ``HealthSignalState.kind`` of a registration's trigger state: the finding set
#: hash the analyst last took (``last_value["hash"]``) and when it last fired.
STATE_KIND: Final = "module_findings"
#: The ``health_schedule_marks`` key of the trigger pass.
TRIGGER_MARK: Final = "module_analyst"
#: How often the trigger pass reads the registrations and their findings.
PASS_CADENCE: Final = timedelta(minutes=5)
#: Most cycles one pass starts; the rest start on the next pass.
MAX_STARTS_PER_PASS: Final = 6
#: A refusal after which the trigger is tried again on the next pass (as signals do).
RETRY_REFUSALS: Final = frozenset(
    {
        "inactive",
        "paused",
        "no_assignment",
        "start_failed",
        "concierge_disabled",
        "ai_disabled",
        "license_required",
        "transient",
        "cycle_budget_reached",
        "month_budget_reached",
    }
)

#: ``change``: the open finding set changed (12.2). ``round``: the orchestrator's daily
#: module round (13.2), which replaced the S14 ``daily`` branch. ``deploy``: an open
#: agentic finding comes from another check logic than the running build's (13.6, #1732).
ModuleTrigger = Literal["change", "round", "deploy"]
#: ``last_value`` key of the build a ``deploy`` round was last taken for.
DEPLOY_KEY: Final = "deploy_build"


def empty_set_hash() -> str:
    """The hash of no open finding: the baseline of a registration seen for the first time."""
    from app.services.health.analysis import finding_set_hash

    return finding_set_hash([])


def due_trigger(
    *,
    current_hash: str,
    last_value: Mapping[str, Any] | None,
    fired_at: datetime | None,
    now: datetime,
    change_min_interval: timedelta,
) -> ModuleTrigger | None:
    """``change`` when a registration's finding set calls for a cycle, ``None`` otherwise.

    Its open finding set differs from the one the checker last took, and no cycle
    started on it within *change_min_interval* (the debounce: the next allowed cycle
    reads the set as it is then). The daily look is the module round's
    (:func:`round_due`), never this trigger's.
    """
    last_hash = (last_value or {}).get("hash") or empty_set_hash()
    if current_hash != last_hash and (fired_at is None or now - fired_at >= change_min_interval):
        return "change"
    return None


def deploy_due(
    *,
    stale: bool,
    build: str,
    last_value: Mapping[str, Any] | None,
    fired_at: datetime | None,
    now: datetime,
    change_min_interval: timedelta,
) -> bool:
    """Whether a registration is due its ``deploy`` round: an open agentic finding is *stale*
    (another check logic produced it) and no deploy round was taken yet for *build*, past the
    same debounce as a change. Taken once per build: a finding the new playbook no longer
    reports stays marked stale, never re-triggers."""
    if not stale or (last_value or {}).get(DEPLOY_KEY) == build:
        return False
    return fired_at is None or now - fired_at >= change_min_interval


def round_due(fired_at: datetime | None, now: datetime) -> bool:
    """Whether *fired_at*, a registration's last cycle, is before this UTC day (the round is due).

    A registration any cycle covered this UTC day (a change, an on-demand request)
    is not handed to the round again: the round is the check list, run once a day.
    """
    return fired_at is None or fired_at.astimezone(UTC).date() < now.astimezone(UTC).date()


async def _module_registrations(db: AsyncSession, excluded: Iterable[uuid.UUID]) -> list[tuple[uuid.UUID, str]]:
    """``(registration id, provider)`` of every active module registration with its integration."""
    from app.models.platform_registration import PlatformRegistration

    query = select(PlatformRegistration.id, PlatformRegistration.platform_identifier).where(
        PlatformRegistration.platform_identifier.in_(MODULE_PROVIDERS),
        PlatformRegistration.status == "active",
        PlatformRegistration.integration_id.is_not(None),
    )
    skip = set(excluded)
    rows = (await db.execute(query.order_by(PlatformRegistration.id))).all()
    return [(row.id, str(row.platform_identifier)) for row in rows if row.id not in skip]


async def checker_reads(db: AsyncSession) -> dict[str, list[tuple[str, uuid.UUID]]]:
    """The integrations each module checker reads, by checker role (section 13.8, the fleet's Data flow).

    ``{checker role: [(provider, integration id)]}`` from the active module registrations
    with their integration: what a round of that checker is wired to at run start. No
    binding is written; the read is configuration only, no run, no model.
    """
    from app.models.platform_registration import PlatformRegistration
    from app.services.health.identity import CHECKER_ROLE_BY_PROVIDER

    rows = (
        await db.execute(
            select(PlatformRegistration.platform_identifier, PlatformRegistration.integration_id)
            .where(
                PlatformRegistration.platform_identifier.in_(MODULE_PROVIDERS),
                PlatformRegistration.status == "active",
                PlatformRegistration.integration_id.is_not(None),
            )
            .order_by(PlatformRegistration.id)
        )
    ).all()
    reads: dict[str, list[tuple[str, uuid.UUID]]] = {}
    for provider, integration_id in rows:
        role = CHECKER_ROLE_BY_PROVIDER.get(str(provider))
        pair = (str(provider), integration_id)
        if role is not None and pair not in reads.get(role, []):
            reads.setdefault(role, []).append(pair)
    return reads


async def active_registrations(db: AsyncSession, provider: str, excluded: Iterable[uuid.UUID] = ()) -> list[uuid.UUID]:
    """The active registrations of *provider* the analyst may investigate ("Investigate now", section 12.7)."""
    return [rid for rid, identifier in await _module_registrations(db, excluded) if identifier == provider]


async def _finding_set_hashes(db: AsyncSession) -> tuple[dict[str, str], set[str]]:
    """The open finding set hash per registration id (as text), registrations with an open finding
    only, and the registrations holding a stale agentic finding (another check logic, #1732)."""
    from app.models.health_check import STATUS_OPEN, HealthCheckFinding
    from app.services.health.analysis import finding_set_hash
    from app.services.health.check_reports import is_stale

    rows = (
        await db.execute(
            select(HealthCheckFinding).where(
                HealthCheckFinding.module.in_(MODULE_PROVIDERS),
                HealthCheckFinding.status == STATUS_OPEN,
            )
        )
    ).scalars()
    grouped: dict[str, list[HealthCheckFinding]] = {}
    stale: set[str] = set()
    for row in rows:
        subject = row.subject if isinstance(row.subject, dict) else {}
        registration = subject.get("registration_id")
        if registration:
            grouped.setdefault(str(registration), []).append(row)
            if is_stale(row.module, row.evidence):
                stale.add(str(registration))
    return {registration: finding_set_hash(findings) for registration, findings in grouped.items()}, stale


async def _start(
    db: AsyncSession,
    registration_id: uuid.UUID,
    provider: str,
    trigger: ModuleTrigger,
    current_hash: str,
    now: datetime,
    build: str = "",
) -> bool:
    """Start one cycle of *provider*'s checker on *registration_id* and record what came of it;
    whether it started. Commits. A ``deploy`` round records *build* as taken."""
    from app.models.health_cycle import HealthSignalState
    from app.services.health.cycles import start_cycle
    from app.services.health.fleet_seed import CHECKS_KEY_BY_PROVIDER

    result = await start_cycle(
        db,
        key=CHECKS_KEY_BY_PROVIDER[provider],
        trigger="signal" if trigger == "change" else "schedule",
        target_type="module",
        target_id=registration_id,
        payload={"module_trigger": trigger},
    )
    state = (
        await db.execute(
            select(HealthSignalState).where(
                HealthSignalState.kind == STATE_KIND,
                HealthSignalState.target_type == "module",
                HealthSignalState.target_id == registration_id,
            )
        )
    ).scalar_one_or_none()
    if state is None:
        state = HealthSignalState(kind=STATE_KIND, target_type="module", target_id=registration_id, active=False)
        db.add(state)
    state.evaluated_at = now
    state.refusal = result.refusal
    if result.started or result.refusal not in RETRY_REFUSALS:
        # Started, or nothing a later pass would change (a cycle already open on it
        # covers the set, the registration left the scope): the set is taken.
        state.fired_at = now
        state.cycle_id = result.cycle_id if result.started else result.open_cycle_id
        taken = (state.last_value or {}).get(DEPLOY_KEY)
        state.last_value = {"hash": current_hash, "trigger": trigger}
        if trigger == "deploy" or taken:
            state.last_value[DEPLOY_KEY] = build if trigger == "deploy" else taken
    await db.commit()
    if not result.started:
        logger.info("Platform Health module analyst on %s (%s): no cycle (%s)", registration_id, trigger, result.refusal)
    return result.started


async def run_module_triggers(db: AsyncSession, *, now: datetime) -> int:
    """One trigger pass: start the cycles the registrations are due; the number started. Commits.

    Two sources, changes first: a registration whose finding set changed (12.2), then
    the daily module round (13.2), which hands every registration no cycle covered this
    UTC day to its module's checker, with zero open findings too. Nothing in Copilot
    mode, with Platform Health off or with the module checks switched off (12.1);
    ``start_cycle`` refuses the rest (AI off, licence, budget, a cycle already open).
    """
    from app.core.config import settings as app_settings
    from app.models.health_cycle import HealthScheduleMark, HealthSignalState
    from app.services.health.settings import load_health_settings

    if app_settings.is_copilot_like:
        return 0
    health = await load_health_settings(db)
    if not health.enabled or not health.module_analyst.enabled:
        return 0
    mark = await db.get(HealthScheduleMark, TRIGGER_MARK)
    if mark is not None and now - mark.fired_at < PASS_CADENCE:
        return 0
    if mark is None:
        db.add(HealthScheduleMark(schedule=TRIGGER_MARK, fired_at=now))
    else:
        mark.fired_at = now
    await db.commit()

    registrations = await _module_registrations(db, health.module_analyst.excluded_registration_ids)
    if not registrations:
        return 0
    hashes, stale = await _finding_set_hashes(db)
    build = str(app_settings.app_version or "")
    states = {
        state.target_id: state
        for state in (
            await db.execute(
                select(HealthSignalState).where(
                    HealthSignalState.kind == STATE_KIND, HealthSignalState.target_type == "module"
                )
            )
        ).scalars()
    }
    interval = timedelta(minutes=health.module_analyst.change_min_interval_minutes)
    due: list[tuple[uuid.UUID, str, ModuleTrigger, str]] = []
    for registration_id, provider in registrations:
        state = states.get(registration_id)
        current = hashes.get(str(registration_id), empty_set_hash())
        fired_at = state.fired_at if state is not None else None
        trigger = due_trigger(
            current_hash=current,
            last_value=state.last_value if state is not None else None,
            fired_at=fired_at,
            now=now,
            change_min_interval=interval,
        )
        if trigger is None and deploy_due(
            stale=str(registration_id) in stale,
            build=build,
            last_value=state.last_value if state is not None else None,
            fired_at=fired_at,
            now=now,
            change_min_interval=interval,
        ):
            trigger = "deploy"
        if trigger is None and round_due(fired_at, now):
            trigger = "round"
        if trigger is not None:
            due.append((registration_id, provider, trigger, current))
    # Changes first, then deploys: a round entry waits for the next pass when the pass is full.
    due.sort(key=lambda item: {"change": 0, "deploy": 1}.get(item[2], 2))
    started = 0
    for registration_id, provider, trigger, current in due[:MAX_STARTS_PER_PASS]:
        started += int(await _start(db, registration_id, provider, trigger, current, now, build))
    return started


async def module_analyst_tick(*, now: datetime | None = None) -> int:
    """The scheduler's Module Analyst trigger pass (``cycles.health_tick``), after the signals."""
    from app.core.database import async_session

    async with async_session() as db:
        try:
            return await run_module_triggers(db, now=now or datetime.now(UTC))
        except Exception:
            logger.exception("Platform Health module analyst trigger pass failed")
            await db.rollback()
            return 0
