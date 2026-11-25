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

"""Platform events: what happens inside the platform, as assignment triggers.

An assignment with ``trigger_type = "platform_event"`` reacts to an event the
platform itself raises - a work item getting a note, changing status, being
claimed, released or completed; a run failing or being orphaned (its holder
gone with its worker); a background task ending - instead of polling for it
on a schedule::

    trigger_config = {
        "event": "work_item.note_added",      # one name, or a list of names
        "filter": {"correlation_key": "dev:x"},  # optional, dotted paths
        "scope": "own",                        # "own" (default), "fleet" or "all"
        "once": False,                         # True: a one-off watch
        "expires_at": "2026-11-02T09:00:00Z",  # optional deadline
        "role": "observer",                    # optional: watches, never works the object
    }

**Role.** ``role = "observer"`` declares that the assignment watches the
objects of its events (a supervisor reading a case lifecycle) without working
them. Delivery is unchanged; readers that ask who works an object (the CTEM
case page) leave observers out. Absent (or ``"worker"``), the assignment works
what it hears.

**One-off watch.** ``once = true`` makes the trigger a watch: the first event
that matches starts one run, and the subscription ends there. The delivery
writes ``fired_event_id`` and ``fired_at`` into the trigger config in the
same transaction as the run (under a row lock, so two events raised together
start one run), and the dispatcher skips a fired watch. Saving the trigger
config again (without ``fired_event_id``) arms it again.

**Deadline.** ``expires_at`` (an ISO 8601 date-time with its offset, ``Z``
for UTC) ends a subscription at that time: an event raised after it never
starts a run, standing trigger or one-off watch, and the check is made again
under the row lock at delivery. Saving the trigger with a later deadline (or
without one) brings it back.

**Publish.** :func:`publish` stamps an ``event_id`` and the event name on the
payload and emits it on the in-process :data:`~app.services.event_bus.event_bus`
(fire-and-forget, after the writer's commit). Every name of
:data:`PLATFORM_EVENTS` has one listener, :func:`dispatch`, registered by
:func:`register_listeners` on the first publish of the process (the web
workers, the scheduler and the task workers all raise events).

**Scope.** By default an assignment receives only the events of objects its
own agent takes part in: the agent created, owns, holds or is the parent of
the object (``participant_agent_ids`` of the payload). ``scope = "fleet"``
widens it to the events of every home member of the assignment agent's
fleet (the event's ``agent_id`` or one of its participants is a member),
membership read once per dispatch, so a member added or removed is honoured
without editing the trigger; an agent with no home fleet is refused at save
(:func:`fleet_scope_error`). ``scope = "all"`` is honoured only when the
assignment's owner is a platform administrator.

**Filter.** ``filter`` has the semantics of the ``integration_event`` payload
filter (:func:`~app.services.integration_event_filters.payload_filter_reason`):
each key a payload field (dotted path), each value the value wanted (list =
any of, object = subset). A plain value against a list-valued field holds when
it is one of the list's elements: ``{"fleet_impact.targets": "<agent
id>"}`` or ``["<agent id>", ...]`` matches an item whose targets include it.

**Platform-wide events.** ``platform.release.deployed`` is about the
platform itself, not an object an agent takes part in: every subscriber hears
it whatever its scope (:data:`PLATFORM_WIDE_EVENTS`). It is raised once per
build, by the process whose boot first recorded it (``services/platform_builds``),
with a stable event id.

**Task watches.** A one-off watch can also wake a background task of an
agent instead of an assignment run (``services/platform_event_task_watches``):
:func:`dispatch_event` delivers each event to the armed task watches after
the assignments.

**Flows.** A flow's ``trigger.platform_event`` node takes the same events
(``flow_triggers.dispatch_flow_platform_event``, after the assignments and
the task watches): one run per node and event, scoped to the flow owner's
objects and the objects its agent nodes take part in.

**Dispatch.** At once, through
:func:`~app.services.assignment_engine.trigger_or_queue_assignment` (the
integration-event path: bounded queue when every slot is taken). Dedupe per
(assignment, event id): the run carries ``dedup_key = platform_event:<event
id>:<assignment id>`` and a second delivery of the same event is refused as a
duplicate. An event about a work item that a live run of the same assignment
already holds is not a second run: a dated note goes on that item's
``claim_notes`` instead (the live-holder pattern of
``services/work_item_pull_requests.py``).

Everything here is best-effort: a failure is logged and never fails the
write that raised the event.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.scopes import (
    ASSIGNMENT_SUBSCRIPTION_SCOPES,
    SUBSCRIPTION_SCOPE_ALL,
    SUBSCRIPTION_SCOPE_FLEET,
    SUBSCRIPTION_SCOPE_OWN,
)
from app.core.utils import format_exception_message, safe_uuid

logger = logging.getLogger(__name__)

#: The assignment trigger type.
TRIGGER_TYPE = "platform_event"
#: ``trigger_context.type`` of a run a platform event started.
CONTEXT_TYPE = "platform_event"
#: ``trigger_config.scope`` values (the subscription vocabulary of ``app.core.scopes``).
SCOPE_OWN = SUBSCRIPTION_SCOPE_OWN
SCOPE_ALL = SUBSCRIPTION_SCOPE_ALL
SCOPE_FLEET = SUBSCRIPTION_SCOPE_FLEET
#: ``trigger_config`` keys of a one-off watch: the flag, and the marks the first delivery writes.
ONCE_KEY = "once"
FIRED_EVENT_KEY = "fired_event_id"
FIRED_AT_KEY = "fired_at"
#: ``trigger_config`` key of the subscription's deadline (ISO 8601 date-time with its offset).
EXPIRES_AT_KEY = "expires_at"
#: ``trigger_config`` key of the assignment's role on its events, and its values.
ROLE_KEY = "role"
ROLE_OBSERVER = "observer"
ROLE_WORKER = "worker"
TRIGGER_ROLES: frozenset[str] = frozenset({ROLE_OBSERVER, ROLE_WORKER})
#: Dedupe window of a platform event's run (seconds).
DEDUP_WINDOW_SECONDS = 24 * 3600
#: Bound of a note text carried in a payload.
_NOTE_MAX_CHARS = 2000
#: Bound of a work item's blocked_reason carried in a payload.
_BLOCKED_REASON_MAX_CHARS = 500
#: Bound of an outcome preview carried in a payload.
_PREVIEW_MAX_CHARS = 500

_WORK_ITEM_KEYS = (
    "work_item_id",
    "correlation_key",
    "title",
    "status",
    "previous_status",
    "priority",
    "owner_agent_id",
    "created_by_agent_id",
    "claimed_by_agent_id",
    "holder_run_id",
    "note",
    "actor_agent_id",
    "actor_run_id",
    # The agents the item's completion concerns: filter on
    # fleet_impact.targets (an agent id, one of the list's elements)
    # and fleet_impact.kind.
    "fleet_impact.targets",
    "fleet_impact.kind",
    "fleet_impact.instructions",
    "result",
    # What a blocked item waits for, so a listener drops at zero tokens the
    # blocks that already name their release: blocked_reason (bounded),
    # blocked_on (the work item ids, null when none) and blocked_on_refs (the
    # kinds of the references not satisfied yet, null when none), e.g.
    # {"status": "blocked", "blocked_on_refs": {"exists": false}}.
    "blocked_reason",
    "blocked_on",
    "blocked_on_refs",
    # true on the status change a person's answer made (answer_work_item):
    # a decision listener hears the answer, not another release of the item.
    "answered",
)
_FRICTION_KEYS = (
    "friction_id",
    "kind",
    "tool",
    "run_id",
    "agent_id",
    "assignment_id",
    "work_item_id",
)
_RUN_KEYS = (
    "run_id",
    "agent_id",
    "assignment_id",
    "parent_run_id",
    "work_item_ids",
    "reason",
    "stop_reason",
    "partial",
)
_TASK_KEYS = ("task_id", "agent_id", "parent_run_id", "outcome_preview")
_A2A_DELEGATION_KEYS = (
    "delegation_id",
    "agent_id",
    "run_id",
    "assignment_id",
    "partner",
    "partner_task_id",
    "outcome",
    "succeeded",
)

#: An A2A delegation whose calling run had ended got its answer (#12079).
A2A_DELEGATION_COMPLETED = "a2a.delegation.completed"
_AGENT_ACCOUNT_KEYS = (
    "account_id",
    "provider",
    "agent_id",
    "conversation_id",
    "thread_id",
    "message_id",
    "internet_message_id",
    "sent_at",
)
_RELEASE_KEYS = ("version", "git_sha", "previous_version", "previous_git_sha", "first_seen_at", "release")
#: A CTEM case event carries the case id and its routing only (#11613, #857, #933): the
#: run reads the rest with get_ctem_case, under its own user's view of the case.
_CTEM_CASE_KEYS = (
    "case_id",
    "vertical",
    "state",
    "trigger_path",
    # The fleet that runs the case (#1390, services/ctem_case_routing.py): the CTEM loop
    # unless an active use-case fleet takes the case's path.
    "owner_fleet",
)
#: ctem_case.validation_finished adds the OpenAEV delivery it reports (#918).
_CTEM_CASE_VALIDATION_KEYS = (*_CTEM_CASE_KEYS, "openaev.event", "openaev.resource_type", "openaev.resource_id")
#: A CTEM signal event carries the signal id and its routing only (#1409): the run reads the
#: read the signal with list_ctem_signals signal_id, under its own permissions.
_CTEM_SIGNAL_KEYS = (
    "signal_id",
    "trigger_path",
    "lens",
    "source_provider",
    "subject_kind",
    "verdict",
    "case_id",
    "pir_floor_met",
)
_VIDEO_KEYS = (
    "job_kind",
    "job_id",
    "video_id",
    "scene_id",
    "title",
    "revision",
    "quality",
    "status",
    "findings_count",
    "file_id",
    "owner_user_id",
)

#: Every platform event an assignment may subscribe to, with the payload keys
#: its ``filter`` may name. Read by ``list_platform_events`` and the API.
PLATFORM_EVENTS: dict[str, dict[str, Any]] = {
    "work_item.note_added": {
        "description": "A progress note was added to a work item.",
        "filter_keys": _WORK_ITEM_KEYS,
    },
    "work_item.status_changed": {
        "description": "A work item's status changed (open, claimed, in_progress, blocked, done, cancelled).",
        "filter_keys": _WORK_ITEM_KEYS,
    },
    "work_item.claimed": {
        "description": "A work item was claimed by an agent or a run.",
        "filter_keys": _WORK_ITEM_KEYS,
    },
    "work_item.claim_released": {
        "description": "A work item's claim was released (its holder ended or gave it back).",
        "filter_keys": _WORK_ITEM_KEYS,
    },
    "work_item.completed": {
        "description": (
            "A work item reached done or cancelled. Also delivered to the agents its fleet_impact "
            "names (scope own: an impacted agent takes part); the payload carries fleet_impact and result."
        ),
        "filter_keys": _WORK_ITEM_KEYS,
    },
    "work_item.released": {
        "description": (
            "A blocked work item went back to open because the pull requests or issues "
            "it waited for (blocked_on_refs) merged or closed."
        ),
        "filter_keys": _WORK_ITEM_KEYS,
    },
    "work_item.wait_cycle": {
        "description": (
            "Blocked work items waited on each other (through blocked_on or a pull request a review item "
            "gates): the platform released the one blocked most recently. The payload's cycle names the "
            "items, the pull requests and the item released."
        ),
        "filter_keys": _WORK_ITEM_KEYS,
    },
    "work_item.release_exhausted": {
        "description": (
            "Inbox runs ended without completing a work item work_item_max_dispatch_attempts times: it is "
            "blocked and no longer dispatched. The payload names the runs (released_by_runs), the attempts and "
            "what still releases it (release_conditions, empty when nothing does): the owner's fleet decides."
        ),
        "filter_keys": _WORK_ITEM_KEYS,
    },
    "work_item.past_due": {
        "description": (
            "An open work item crossed its due date (services/work_item_past_due.py): raised once per item "
            "and due date by the scheduler tick (a new due_at crossed raises again); scheduled one-offs are "
            "left out. The payload carries due_at and overdue_seconds. Filter on owner_agent_id, "
            "correlation_key (starts_with) or priority."
        ),
        "filter_keys": (*_WORK_ITEM_KEYS, "due_at", "overdue_seconds"),
    },
    "work_item.needs_human": {
        "description": (
            "A work item started waiting for a person: needs_human became true, or the blocked_reason "
            "of an item no agent owns changed. Raised once per transition; the payload carries "
            "blocked_reason and waiting_since."
        ),
        "filter_keys": _WORK_ITEM_KEYS,
    },
    "run.completed": {
        "description": (
            "A run completed: an assignment run, or a delegated child run (no assignment; parent_run_id "
            "names the run that delegated it). stop_reason names why a run stopped before its task was done "
            "(deadline, budget_exhausted, max_iterations, timeout, waiting_on_tasks...): the run still "
            "completes with a partial result; null when it ended on its own terms. partial is true exactly "
            "when stop_reason is set."
        ),
        "filter_keys": _RUN_KEYS,
    },
    "run.failed": {
        "description": (
            "A run failed: an assignment run, or a delegated child run (no assignment; parent_run_id "
            "names the run that delegated it)."
        ),
        "filter_keys": (*_RUN_KEYS, "error"),
    },
    "run.orphaned": {
        "description": "A run or background task ended without finishing (its worker gone) while holding work items.",
        "filter_keys": _RUN_KEYS,
    },
    "run.friction_recorded": {
        "description": (
            "A friction of a run was first recorded (a tool error, an approval pause, a resumed turn, or the "
            "agent's own record_friction): the fleet's orchestrator classifies and disposes of it."
        ),
        "filter_keys": _FRICTION_KEYS,
    },
    "run.completed_with_undisposed_frictions": {
        "description": "An assignment run ended leaving frictions with no disposition (their ids, count and kinds).",
        "filter_keys": (*_RUN_KEYS, "count", "kinds"),
    },
    A2A_DELEGATION_COMPLETED: {
        "description": (
            "An A2A partner finished work an agent delegated from a run that has since ended (the tool had "
            "handed back still_running): the answer is no longer resumed into that run. Heard by the "
            "delegating agent's assignments (scope own) for the person it ran for; the payload carries the "
            "partner, the task id, the outcome and a bounded preview. Collect the whole answer with the "
            "partner's tool, continue_task_id = partner_task_id and no message."
        ),
        "filter_keys": _A2A_DELEGATION_KEYS,
    },
    "background_task.completed": {
        "description": "A background task completed.",
        "filter_keys": _TASK_KEYS,
    },
    "background_task.failed": {
        "description": "A background task failed.",
        "filter_keys": (*_TASK_KEYS, "error"),
    },
    "video.render.finished": {
        "description": "A whole render of a video ended ready (with its checks' findings count).",
        "filter_keys": _VIDEO_KEYS,
    },
    "video.render.failed": {
        "description": "A whole render of a video failed.",
        "filter_keys": (*_VIDEO_KEYS, "error"),
    },
    "video.scene_preview.finished": {
        "description": "A scene preview of a video ended ready.",
        "filter_keys": _VIDEO_KEYS,
    },
    "video.scene_preview.failed": {
        "description": "A scene preview of a video failed.",
        "filter_keys": (*_VIDEO_KEYS, "error"),
    },
    "video.clip.ready": {
        "description": "An AI shot ended ready.",
        "filter_keys": _VIDEO_KEYS,
    },
    "video.clip.failed": {
        "description": "An AI shot failed.",
        "filter_keys": (*_VIDEO_KEYS, "error"),
    },
    "video.music.ready": {
        "description": "An AI music track ended ready.",
        "filter_keys": _VIDEO_KEYS,
    },
    "video.music.failed": {
        "description": "An AI music track failed.",
        "filter_keys": (*_VIDEO_KEYS, "error"),
    },
    "agent_account.owner_sent": {
        "description": (
            "The owner of a mailbox agent account sent an email from that mailbox themselves (not the agent): "
            "clear the flag or star of the thread it answers. Heard by the account's agent (scope own) and its "
            "fleet (scope fleet), for the account's owner only; once per message."
        ),
        "filter_keys": _AGENT_ACCOUNT_KEYS,
    },
    "platform.release.deployed": {
        "description": (
            "A new build of the platform was first seen serving (once per build, whatever the number of "
            "pods): wait for a deployment on it instead of polling the release history. Platform-wide: "
            "heard by every subscriber whatever its scope."
        ),
        "filter_keys": _RELEASE_KEYS,
    },
    "integration.version.changed": {
        "description": (
            "A connected OpenCTI, OpenAEV or OpenCRQ platform reported a version other than the one recorded, "
            "on its registration heartbeat (once per platform and upgrade; not on a first registration): wait "
            "for a module upgrade instead of polling its version. Payload: provider, platform_identifier, "
            "integration_id, previous_version, version, first_seen_at. Also raised when a connector of a "
            "connected OpenCTI reports another version (read every 5 minutes while a subscriber exists): "
            "the payload then adds component_kind, component_id, component_name, component_slug. "
            "Platform-wide: heard by every subscriber whatever its scope."
        ),
        "filter_keys": (
            "provider",
            "platform_identifier",
            "integration_id",
            "previous_version",
            "version",
            "first_seen_at",
            "component_kind",
            "component_id",
            "component_name",
            "component_slug",
        ),
    },
    "platform.default_model_route.changed": {
        "description": (
            "A default model route of Settings > AI and models > Models (agent_default, routing, fallback, "
            "decision, image, embedding) changed provider or model, once per route and change, whatever the "
            "cause (settings_page, agent_tool, provider_deleted, env_provider_prune, system): watch the "
            "platform defaults without polling get_platform_ai_settings. Payload: route, before and after "
            "{provider_id, provider, model}, cause, actor_user_id; no credential. Platform-wide: heard by "
            "every subscriber whatever its scope."
        ),
        "filter_keys": (
            "route",
            "cause",
            "actor_user_id",
            "before.provider_id",
            "before.provider",
            "before.model",
            "after.provider_id",
            "after.provider",
            "after.model",
        ),
    },
    "ctem_case.opened": {
        "description": (
            "A CTEM case was opened from a confirmed, in-scope exposure signal (services/ctem_case_loop.py): "
            "start a fleet run from the case. Payload: case_id, vertical, state, trigger_path, owner_fleet "
            "(the fleet that runs it); read the "
            "case with get_ctem_case. Heard only when the user the subscription runs as sees the case "
            "(an administrator's scope all assignment running as itself hears every case)."
        ),
        "filter_keys": _CTEM_CASE_KEYS,
    },
    "ctem_case.reopened": {
        "description": (
            "A resolved or watched CTEM case reopened because a re-test failed: restart the remediation. "
            "Payload: case_id, vertical, state, trigger_path. Heard only by readers of the case."
        ),
        "filter_keys": _CTEM_CASE_KEYS,
    },
    "ctem_case.resolved": {
        "description": (
            "A CTEM case resolved on a passing validation reference (not exposed, or fixed): close the run. "
            "Payload: case_id, vertical, state, trigger_path. Heard only by readers of the case."
        ),
        "filter_keys": _CTEM_CASE_KEYS,
    },
    "ctem_case.sla_breached": {
        "description": (
            "An open CTEM case passed its SLA (once per case): escalate. Payload: case_id, vertical, "
            "state, trigger_path. Heard only by readers of the case."
        ),
        "filter_keys": _CTEM_CASE_KEYS,
    },
    "ctem_case.stalled": {
        "description": (
            "An open CTEM case waits on nobody (#1736): no open gate work item of the case, no armed "
            "ctem-loop-reminder timer and no case activity for CTEM_CASE_STALL_HOURS; raised by the case sweep "
            "once per stall window. Re-run the loop and end it on an ask to a named person with a due date, "
            "an armed reminder, or a terminal state. Payload: case_id, vertical, state, trigger_path. Heard only "
            "by readers of the case."
        ),
        "filter_keys": _CTEM_CASE_KEYS,
    },
    "ctem_case.validation_finished": {
        "description": (
            "An OpenAEV result a CTEM case waits on arrived (a simulation it references finished, a finding it "
            "holds changed or was deleted; services/ctem_openaev_events.py), once per case and occurrence: read "
            "the result and write it on the case with record_ctem_case_validation. Payload: the case fields and "
            "openaev {event, resource_type, resource_id, platform_url}. Heard only by readers of the case."
        ),
        "filter_keys": _CTEM_CASE_VALIDATION_KEYS,
    },
    "ctem_case.retest_requested": {
        "description": (
            "The remediation ticket of an open CTEM case reached done in the declared ticketing system "
            "(services/ctem_ticketing.py): run the re-test; the case closes on its result, never on the ticket. "
            "Payload: the case fields, ticket_integration, ticket_key. Heard only by readers of the case."
        ),
        "filter_keys": _CTEM_CASE_KEYS,
    },
    "ctem_case.opening_changed": {
        "description": (
            "An administrator turned CTEM case opening on or off (#1951, services/ctem_case_opening.py). "
            "Off: no confirmed signal opens a case, they wait; on: the waiting signals open their cases "
            "at once. Payload: vertical, on, changed_by (user id), waiting (confirmed signals with no case)."
        ),
        "filter_keys": ("vertical", "on", "changed_by"),
    },
    "ctem_signal.recorded": {
        "description": (
            "A CTEM signal was seen for the first time (services/ctem_signals.py), after the scope gate gave it "
            "its first verdict; a re-sighting raises nothing. Payload: signal_id, trigger_path, lens, "
            "source_provider, subject_kind, verdict (confirmed, to_validate, watchlist, discarded, or null when "
            "the gate did not run), case_id, pir_floor_met (a relevant threat's PIR score reached the policy threshold); "
            "read the signal with list_ctem_signals signal_id. Heard only when the user the "
            "subscription runs as reads the signal (an administrator, or its recorder; a listener's signal is "
            "an administrator's only)."
        ),
        "filter_keys": _CTEM_SIGNAL_KEYS,
    },
    "ctem_signal.triaged": {
        "description": (
            "The scope gate changed the verdict of a CTEM signal after its first sighting (an on-demand or "
            "bulk evaluation, services/ctem_scope_gate_store.py): confirmed, to_validate, watchlist or "
            "discarded. Payload: the signal fields, verdict and previous_verdict. Heard only by readers of "
            "the signal."
        ),
        "filter_keys": (*_CTEM_SIGNAL_KEYS, "previous_verdict"),
    },
    "timer.due": {
        "description": (
            "A timer an agent armed with arm_timer reached its due time (services/platform_timers.py): "
            "act later on one object without a cron. Heard by the arming agent's assignments (scope own) "
            "for the person it ran for; never raised when its unless condition was met, at most once."
        ),
        "filter_keys": ("correlation_key", "timer_id", "due_at", "armed_by_run_id", "payload.*"),
    },
}

#: The events about the platform itself rather than an object someone takes
#: part in: a build belongs to no one and its version is shown to every user,
#: so a ``scope: own`` subscriber hears them too.
PLATFORM_WIDE_EVENTS: frozenset[str] = frozenset(
    {
        "platform.release.deployed",
        "platform.default_model_route.changed",
        "integration.version.changed",
        "ctem_case.opening_changed",
    }
)

#: The events about an object whose readers only the object's own view decides
#: (#933): a CTEM case is shared, its visibility is per viewer
#: (``services/ctem_cases_view``); a CTEM signal is read by administrators and
#: its recorder (#1409). Any scope may subscribe; each delivery is then
#: checked by :func:`can_read_object` with the identity the run executes as.
VIEWER_CHECKED_EVENTS: frozenset[str] = frozenset(
    {
        "ctem_case.opened",
        "ctem_case.reopened",
        "ctem_case.resolved",
        "ctem_case.sla_breached",
        "ctem_case.stalled",
        "ctem_case.validation_finished",
        "ctem_case.retest_requested",
        "ctem_signal.recorded",
        "ctem_signal.triaged",
    }
)


def event_names(config: Mapping[str, Any] | None) -> list[str]:
    """The event names a ``platform_event`` trigger subscribes to."""
    raw = (config or {}).get("event")
    names = [raw] if isinstance(raw, str) else list(raw) if isinstance(raw, list) else []
    return [n for n in names if isinstance(n, str) and n in PLATFORM_EVENTS]


def trigger_config_error(trigger_type: object, trigger_config: object) -> str | None:
    """The refusal for a ``platform_event`` trigger as it will be saved, else ``None``."""
    if trigger_type != TRIGGER_TYPE:
        return None
    if not isinstance(trigger_config, dict):
        return "Invalid trigger_config: a platform_event trigger takes a JSON object."
    raw = trigger_config.get("event")
    names = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else None
    if not names:
        return "Invalid trigger_config: 'event' must name a platform event (see list_platform_events)."
    unknown = [n for n in names if not isinstance(n, str) or n not in PLATFORM_EVENTS]
    if unknown:
        return (
            f"Invalid trigger_config: unknown platform event(s) {unknown!r}; "
            f"known: {', '.join(sorted(PLATFORM_EVENTS))}."
        )
    flt = trigger_config.get("filter")
    if flt is not None and not isinstance(flt, dict):
        return "Invalid trigger_config: 'filter' must be a JSON object."
    scope = trigger_config.get("scope")
    if scope is not None and scope not in ASSIGNMENT_SUBSCRIPTION_SCOPES:
        return "Invalid trigger_config: 'scope' must be 'own', 'fleet' or 'all'."
    if not isinstance(trigger_config.get(ONCE_KEY, False), bool):
        return "Invalid trigger_config: 'once' must be true or false."
    role = trigger_config.get(ROLE_KEY)
    if role is not None and not (isinstance(role, str) and role in TRIGGER_ROLES):
        return "Invalid trigger_config: 'role' must be 'observer' or 'worker'."
    deadline = trigger_config.get(EXPIRES_AT_KEY)
    if deadline is not None and parse_deadline(deadline) is None:
        return (
            "Invalid trigger_config: 'expires_at' must be an ISO 8601 date-time with its offset, "
            "for example 2026-11-02T09:00:00Z."
        )
    return None


async def fleet_scope_error(trigger_type: object, trigger_config: object, agent_id: object) -> str | None:
    """The refusal for a ``scope: fleet`` trigger on an agent with no home fleet, else ``None``.

    Judged on the trigger as it will be saved, after :func:`trigger_config_error`.
    """
    if trigger_type != TRIGGER_TYPE or not isinstance(trigger_config, dict):
        return None
    if trigger_config.get("scope") != SCOPE_FLEET:
        return None
    from app.services.coordination_scope import read_home_fleet

    aid = safe_uuid(str(agent_id)) if agent_id else None
    if aid is not None and await read_home_fleet(aid) is not None:
        return None
    return (
        f"Invalid trigger_config: scope 'fleet' needs an agent with a home fleet; "
        f"agent {agent_id} is in none (use 'own', or add the agent to a fleet)."
    )


def parse_deadline(value: object) -> datetime | None:
    """*value* as an aware date-time, ``None`` when it is not an ISO 8601 date-time with an offset."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def expired(config: Mapping[str, Any] | None, now: datetime | None = None) -> bool:
    """Whether *config*'s deadline has passed (no deadline: never).

    A deadline that does not parse counts as passed: it was refused on save,
    so only a hand-edited row carries one, and a subscription with an unknown
    end is not delivered.
    """
    raw = (config or {}).get(EXPIRES_AT_KEY)
    if raw is None:
        return False
    deadline = parse_deadline(raw)
    return deadline is None or deadline <= (now or datetime.now(UTC))


def watch_fired(config: Mapping[str, Any] | None) -> bool:
    """Whether *config* is a one-off watch that already started its run."""
    config = config or {}
    return config.get(ONCE_KEY) is True and bool(config.get(FIRED_EVENT_KEY))


def rearm_watch(config: Any) -> Any:
    """*config* without the fired mark of its one-off watch: a copy's watch starts armed.

    A value that is not an object is returned unchanged.
    """
    if not isinstance(config, Mapping):
        return config
    return {k: v for k, v in config.items() if k not in (FIRED_EVENT_KEY, FIRED_AT_KEY)}


def catalog() -> list[dict[str, Any]]:
    """The platform events, sorted, each with its description and filter keys."""
    return [
        {"event": name, "description": spec["description"], "filter_keys": list(spec["filter_keys"])}
        for name, spec in sorted(PLATFORM_EVENTS.items())
    ]


# -- Payload builders ------------------------------------------------------------


def _str(value: object) -> str | None:
    return str(value) if value is not None else None


def work_item_data(
    item: Any,
    *,
    previous_status: str | None = None,
    note: str | None = None,
    actor_agent_id: object = None,
    actor_run_id: object = None,
    actor_flow_id: object = None,
    completed: bool = False,
) -> dict[str, Any]:
    """The payload of a ``work_item.*`` event for *item* (a ``WorkItem`` row).

    *completed* builds the ``work_item.completed`` payload: the agents
    the item's fleet impact targets join its participants, so that event (and
    only that one) reaches them, and the payload carries the impact and the
    item's result.
    """
    from app.services.work_item_fleet_impact import effective_fleet_impact

    participants = {
        _str(getattr(item, "created_by_agent_id", None)),
        _str(getattr(item, "owner_agent_id", None)),
        _str(getattr(item, "claimed_by_agent_id", None)),
    }
    impact = effective_fleet_impact(item) if completed else None
    if impact:
        participants.update(impact["targets"])
    participants.discard(None)
    return {
        **_blocker_data(item),
        "work_item_id": _str(item.id),
        "correlation_key": getattr(item, "correlation_key", None),
        "title": getattr(item, "title", None),
        "status": getattr(item, "status", None),
        "previous_status": previous_status,
        "priority": getattr(item, "priority", None),
        "owner_agent_id": _str(getattr(item, "owner_agent_id", None)),
        "created_by_agent_id": _str(getattr(item, "created_by_agent_id", None)),
        "claimed_by_agent_id": _str(getattr(item, "claimed_by_agent_id", None)),
        "holder_run_id": _str(getattr(item, "claimed_by_run_id", None)),
        "note": (note or "")[:_NOTE_MAX_CHARS] or None,
        "actor_agent_id": _str(actor_agent_id),
        "actor_run_id": _str(actor_run_id),
        # The flow whose node wrote it: that flow's platform-event trigger
        # never hears it (no flow re-triggers itself on its own writes).
        "actor_flow_id": _str(actor_flow_id),
        "participant_agent_ids": sorted(participants),
        "fleet_impact": impact,
        "result": ((getattr(item, "result", None) or "")[:_NOTE_MAX_CHARS] or None) if completed else None,
        "object_type": "work_item",
        "object_id": _str(item.id),
        "user_id": _str(getattr(item, "user_id", None)),
        # Set for a personal item only: a company-managed or workspace item is shared.
        "personal_owner_id": (
            _str(getattr(item, "user_id", None))
            if not getattr(item, "is_company_managed", False) and getattr(item, "workspace_id", None) is None
            else None
        ),
        # A workspace item reaches only assignments whose owner can read the workspace.
        "workspace_id": _str(getattr(item, "workspace_id", None)),
    }


def _blocker_data(item: Any) -> dict[str, Any]:
    """What *item* waits for, bounded: ``blocked_reason``, ``blocked_on``, ``blocked_on_refs``.

    ``blocked_on`` is the list of work item ids it waits for and
    ``blocked_on_refs`` the sorted kinds of its references not satisfied yet
    (``pull_request``, ``issue``, ``deployment``, ``workflow_run``); each is
    ``None`` when empty, so a filter's ``exists: false`` holds on an item that
    names no release. A malformed column reads as absent, never raises.
    """
    from app.services.work_item_blocked_refs import open_refs

    reason = getattr(item, "blocked_reason", None)
    reason = reason.strip()[:_BLOCKED_REASON_MAX_CHARS] if isinstance(reason, str) else ""
    raw_on = getattr(item, "blocked_on", None)
    blocked_on = [str(i) for i in raw_on if i] if isinstance(raw_on, list) else []
    raw_refs = getattr(item, "blocked_on_refs", None)
    pending = open_refs(raw_refs if isinstance(raw_refs, list) else None)
    kinds = sorted({str(r.get("kind") or "pull_request") for r in pending})
    return {
        "blocked_reason": reason or None,
        "blocked_on": blocked_on or None,
        "blocked_on_refs": kinds or None,
    }


def agent_account_message_data(account: Any, event: Any) -> dict[str, Any]:
    """The payload of an ``agent_account.*`` event: *account* (an ``AgentAccount``), *event* (an ``AccountEvent``).

    The account is personal: only its owner's assignments hear it, and its
    agent is the participant a ``scope: own`` subscriber matches.
    """
    agent_id = _str(getattr(account, "agent_id", None))
    created = getattr(event, "created_at", None)
    return {
        "account_id": _str(account.id),
        "provider": getattr(account, "provider", None),
        "agent_id": agent_id,
        "conversation_id": getattr(event, "conversation_id", None),
        "thread_id": getattr(event, "thread_id", None),
        "message_id": getattr(event, "message_id", None),
        "internet_message_id": getattr(event, "internet_message_id", None),
        "subject": getattr(event, "conversation_name", None),
        "sent_at": created.isoformat() if isinstance(created, datetime) else None,
        "participant_agent_ids": [agent_id] if agent_id else [],
        "object_type": "agent_account",
        "object_id": _str(account.id),
        "user_id": _str(getattr(account, "user_id", None)),
        "personal_owner_id": _str(getattr(account, "user_id", None)),
    }


def run_data(
    *,
    run_id: object,
    agent_id: object,
    assignment_id: object = None,
    parent_run_id: object = None,
    parent_agent_id: object = None,
    work_item_ids: Iterable[object] = (),
    reason: str | None = None,
    error: str | None = None,
    stop_reason: str | None = None,
    participant_agent_ids: Iterable[object] = (),
    workspace_ids: Iterable[object] = (),
    personal_owner_ids: Iterable[object] = (),
) -> dict[str, Any]:
    """The payload of a ``run.*`` event."""
    participants = {_str(a) for a in (agent_id, parent_agent_id, *participant_agent_ids)}
    participants.discard(None)
    return {
        "run_id": _str(run_id),
        "agent_id": _str(agent_id),
        "assignment_id": _str(assignment_id),
        "parent_run_id": _str(parent_run_id),
        "work_item_ids": [str(i) for i in work_item_ids],
        "reason": reason,
        "error": (error or "")[:_PREVIEW_MAX_CHARS] or None,
        # Why a completed run stopped before its task was done (the run's
        # stop_reason: deadline, budget, iteration cap...); None otherwise.
        "stop_reason": stop_reason,
        # A completed run that stopped before its task was done, said outright:
        # a listener never has to know the list of unfinished stop reasons.
        "partial": bool(stop_reason),
        # The agent whose run it is raised the event (ignore_self_triggered_events).
        "actor_agent_id": _str(agent_id),
        "participant_agent_ids": sorted(participants),
        "object_type": "run",
        "object_id": _str(run_id),
        # The workspaces of the items the run held: visibility is checked on each.
        "workspace_ids": sorted({str(w) for w in workspace_ids if w}),
        # The owners of the personal items the run held: only their assignments see it.
        "personal_owner_ids": sorted({str(u) for u in personal_owner_ids if u}),
    }


def friction_data(friction: Mapping[str, Any], *, user_id: object = None) -> dict[str, Any]:
    """The payload of ``run.friction_recorded``, from a serialised friction (``services/frictions.py``).

    *user_id* is the person the friction was recorded for: only that person's
    assignments hear it (the evidence may quote what the run read).
    """
    agent_id = _str(friction.get("agent_id"))
    return {
        "friction_id": _str(friction.get("id")),
        "kind": friction.get("kind"),
        "tool": friction.get("tool") or "",
        "evidence": (friction.get("evidence") or "")[:_PREVIEW_MAX_CHARS],
        "run_id": _str(friction.get("run_id")),
        "agent_id": agent_id,
        "assignment_id": _str(friction.get("assignment_id")),
        "work_item_id": _str(friction.get("work_item_id")),
        "actor_agent_id": agent_id,
        "participant_agent_ids": [agent_id] if agent_id else [],
        "object_type": "friction",
        "object_id": _str(friction.get("id")),
        "personal_owner_ids": [str(user_id)] if user_id else [],
    }


def undisposed_frictions_data(
    *,
    run_id: object,
    agent_id: object,
    assignment_id: object = None,
    friction_ids: Iterable[object] = (),
    kinds: Iterable[object] = (),
) -> dict[str, Any]:
    """The payload of ``run.completed_with_undisposed_frictions``; ``kinds`` lists the undisposed kinds."""
    ids = [str(i) for i in friction_ids]
    data = run_data(run_id=run_id, agent_id=agent_id, assignment_id=assignment_id, reason="undisposed_frictions")
    data["count"] = len(ids)
    data["friction_ids"] = ids
    data["kinds"] = sorted({str(k) for k in kinds})
    return data


def a2a_delegation_data(
    *,
    delegation_id: object,
    agent_id: object,
    user_id: object,
    run_id: object = None,
    assignment_id: object = None,
    partner: str | None = None,
    partner_task_id: str | None = None,
    outcome: str | None = None,
    succeeded: bool = False,
    detail: str | None = None,
    result_preview: str | None = None,
) -> dict[str, Any]:
    """The payload of ``a2a.delegation.completed``: a personal object of the delegating person."""
    agent = _str(agent_id)
    return {
        "delegation_id": _str(delegation_id),
        "agent_id": agent,
        "run_id": _str(run_id),
        "assignment_id": _str(assignment_id),
        "partner": (partner or "")[:200] or None,
        "partner_task_id": partner_task_id,
        "outcome": outcome,
        "succeeded": bool(succeeded),
        "detail": (detail or "")[:_PREVIEW_MAX_CHARS] or None,
        "result_preview": (result_preview or "")[:_PREVIEW_MAX_CHARS] or None,
        # Raised by the platform on the partner's behalf, not by the agent:
        # its own assignments must hear it (ignore_self_triggered_events).
        "actor_agent_id": None,
        "participant_agent_ids": [agent] if agent else [],
        "object_type": "a2a_delegation",
        "object_id": _str(delegation_id),
        "user_id": _str(user_id),
        "personal_owner_id": _str(user_id),
    }


def background_task_data(
    *,
    task_id: object,
    agent_id: object,
    parent_run_id: object = None,
    parent_agent_id: object = None,
    outcome: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """The payload of a ``background_task.*`` event."""
    participants = {_str(agent_id), _str(parent_agent_id)}
    participants.discard(None)
    return {
        "task_id": _str(task_id),
        "agent_id": _str(agent_id),
        "parent_run_id": _str(parent_run_id),
        "outcome_preview": (outcome or "")[:_PREVIEW_MAX_CHARS] or None,
        "error": (error or "")[:_PREVIEW_MAX_CHARS] or None,
        # The agent that ran the task raised the event (ignore_self_triggered_events).
        "actor_agent_id": _str(agent_id),
        "participant_agent_ids": sorted(participants),
        "object_type": "background_task",
        "object_id": _str(task_id),
    }


# -- Publish ---------------------------------------------------------------------


async def publish(name: str, data: Mapping[str, Any], *, event_id: str | None = None) -> str | None:
    """Raise platform event *name* with *data*; its ``event_id``, ``None`` when unknown.

    Call it after the write that raised it is committed. Never raises. A
    publisher that may report the same occurrence twice passes a stable
    *event_id* (derived from the object and its state): the dispatcher's
    dedupe per (assignment, event id) then starts one run.
    """
    if name not in PLATFORM_EVENTS:
        logger.warning("Platform event %r is not in the catalog: not published", name)
        return None
    from app.services.event_bus import event_bus

    # The bus is in-process and events are raised in every process that writes
    # (web, scheduler, workers): the process that publishes dispatches.
    register_listeners()
    event_id = event_id or str(uuid.uuid4())
    payload = {
        **dict(data),
        "event": name,
        "event_id": event_id,
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    try:
        await event_bus.emit(name, payload)
    except Exception as exc:  # the bus never raises, but a publisher must not fail its writer
        logger.warning("Platform event %s not published: %s", name, format_exception_message(exc))
        return None
    return event_id


async def publish_many(events: Iterable[tuple[str, Mapping[str, Any]]]) -> None:
    """:func:`publish` each ``(name, data)`` in order."""
    for name, data in events:
        await publish(name, data)


# -- Scope and match -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Subscription:
    """What dispatch needs of one subscribed assignment."""

    assignment_id: uuid.UUID
    agent_id: uuid.UUID
    events: tuple[str, ...]
    filter: Mapping[str, Any]
    scope_all: bool
    #: The assignment's owner: a personal object's events reach only its owner's assignments.
    user_id: uuid.UUID | None = None
    #: The user the assignment runs as, when not its owner: must read a workspace object too.
    run_as_user_id: uuid.UUID | None = None
    #: A one-off watch: the first matching event starts one run, then the subscription ends.
    once: bool = False
    #: ``scope: fleet``: the home members of the agent's fleet, read at dispatch
    #: (:func:`_fleet_members`); empty for any other scope or an agent in no fleet.
    fleet_agent_ids: frozenset[str] = frozenset()


def is_platform_wide(payload: Mapping[str, Any]) -> bool:
    """Whether *payload* is a platform-wide event (:data:`PLATFORM_WIDE_EVENTS`): every scope hears it."""
    return payload.get("event") in PLATFORM_WIDE_EVENTS


def personal_owners(payload: Mapping[str, Any]) -> set[str]:
    """The users a personal object of *payload* belongs to (empty: a shared object)."""
    return {str(o) for o in (payload.get("personal_owner_id"), *(payload.get("personal_owner_ids") or ())) if o}


def touched_workspaces(payload: Mapping[str, Any]) -> set[str]:
    """The workspaces an event with *payload* touches: its reader must read each one."""
    return {str(w) for w in (payload.get("workspace_id"), *(payload.get("workspace_ids") or ())) if w}


def is_viewer_checked(payload: Mapping[str, Any]) -> bool:
    """Whether *payload* is about an object whose own view decides its readers (:data:`VIEWER_CHECKED_EVENTS`)."""
    return payload.get("event") in VIEWER_CHECKED_EVENTS


def in_scope(sub: Subscription, payload: Mapping[str, Any]) -> bool:
    """Whether *sub* may see an event with *payload*.

    A viewer-checked event passes here whatever the scope: :func:`can_read_object`
    then decides with the identity the run executes as.
    """
    if sub.scope_all or is_platform_wide(payload) or is_viewer_checked(payload):
        return True
    # A personal object (a work item filed by a user) reaches only that user's
    # assignments: naming another user's agent as its owner does not wake it.
    owners = personal_owners(payload)
    if owners and owners != {str(sub.user_id)}:
        return False
    participants = {str(a) for a in payload.get("participant_agent_ids") or ()}
    if sub.fleet_agent_ids:
        agent = payload.get("agent_id")
        return bool(sub.fleet_agent_ids & (participants | ({str(agent)} if agent else set())))
    return str(sub.agent_id) in participants


def mismatch_reason(sub: Subscription, name: str, payload: Mapping[str, Any]) -> str | None:
    """Why *sub* does not take event *name*, ``None`` when it does."""
    from app.services.integration_event_filters import payload_filter_reason

    if name not in sub.events:
        return "not subscribed to this event"
    if not in_scope(sub, payload):
        return "outside the assignment's scope"
    return payload_filter_reason(payload, sub.filter)


def dedup_key(event_id: str, assignment_id: object) -> str:
    """The run dedupe key of event *event_id* for one assignment."""
    return f"platform_event:{event_id}:{assignment_id}"


def trigger_context(
    name: str, payload: Mapping[str, Any], assignment_id: object, *, once: bool = False
) -> dict[str, Any]:
    """The ``trigger_context`` of the run event *name* starts.

    Platform event runs share their assignment's slots up to its cap
    (``assignment_dispatch.SHARED_SLOT_TRIGGER_TYPES``): the ordering key
    ``<object_type>:<object_id>`` keeps two events about one object in order,
    one at a time, while events about other objects run beside them. A
    configured ``trigger_config.ordering_key`` that resolves on the payload wins.
    """
    from app.services.assignment_dispatch import ORDERING_KEY_CONTEXT
    from app.services.event_run_dedup import DEDUP_KEY_CONTEXT

    object_key = object_ordering_key(payload)
    return {
        "type": CONTEXT_TYPE,
        "event": name,
        "event_id": payload.get("event_id"),
        "object_type": payload.get("object_type"),
        "object_id": payload.get("object_id"),
        # Ids only: every text field (correlation key, title, note) stays in the
        # payload, which the run reads quarantined.
        # When the event was raised and when it was delivered: with the run's own
        # start time, the event-to-run latency is a measure, not a bound.
        "occurred_at": payload.get("occurred_at"),
        "triggered_at": datetime.now(UTC).isoformat(),
        **({ONCE_KEY: True} if once else {}),
        **({ORDERING_KEY_CONTEXT: object_key} if object_key else {}),
        DEDUP_KEY_CONTEXT: dedup_key(str(payload.get("event_id")), assignment_id),
    }


def object_ordering_key(payload: Mapping[str, Any]) -> str | None:
    """``<object_type>:<object_id>`` of the object event *payload* is about, ``None`` without one."""
    object_type, object_id = payload.get("object_type"), payload.get("object_id")
    if not object_type or not object_id:
        return None
    return f"{object_type}:{object_id}"[:200]


def live_note(name: str, payload: Mapping[str, Any], run_id: object, now: datetime) -> dict[str, Any]:
    """The claim note left on a work item a live run of the subscriber holds."""
    stamp = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    detail = payload.get("note") or payload.get("status") or ""
    text = f"Platform event {name} ({stamp}); the live run {run_id} holding the item is told"
    if detail:
        text += f": {str(detail)[:300]}"
    return {"at": now.isoformat(), "text": text}


# -- Dispatch --------------------------------------------------------------------


async def _subscriptions(db: Any, name: str) -> list[tuple[Subscription, Any, Any]]:
    """Every enabled ``platform_event`` assignment subscribed to *name*, with its row and agent."""
    from sqlalchemy import select

    from app.models.agent import Agent
    from app.models.assignment import Assignment
    from app.models.user import User

    rows = (
        await db.execute(
            select(Assignment, Agent, User.role)
            .join(Agent, Agent.id == Assignment.agent_id)
            .outerjoin(User, User.id == Assignment.user_id)
            .where(
                Assignment.trigger_type == TRIGGER_TYPE,
                Assignment.enabled.is_(True),
                Agent.enabled.is_(True),
            )
        )
    ).all()
    live = []
    for assignment, agent, role in rows:
        config = assignment.trigger_config if isinstance(assignment.trigger_config, dict) else {}
        names = event_names(config)
        if name not in names or watch_fired(config) or expired(config):
            continue
        live.append((assignment, agent, role, config, names))
    fleet_scoped = {agent.id for _, agent, _, config, _ in live if config.get("scope") == SCOPE_FLEET}
    fleets = await _fleet_members(db, fleet_scoped)
    out = []
    for assignment, agent, role, config, names in live:
        raw_filter = config.get("filter")
        flt: dict[str, Any] = raw_filter if isinstance(raw_filter, dict) else {}
        sub = Subscription(
            assignment_id=assignment.id,
            agent_id=agent.id,
            events=tuple(names),
            filter=flt,
            scope_all=config.get("scope") == SCOPE_ALL and role == "admin",
            user_id=assignment.user_id,
            run_as_user_id=getattr(assignment, "run_as_user_id", None),
            once=config.get(ONCE_KEY) is True,
            fleet_agent_ids=fleets.get(agent.id, frozenset()) if agent.id in fleet_scoped else frozenset(),
        )
        out.append((sub, assignment, agent))
    return out


async def live_subscriptions(name: str) -> list[Subscription]:
    """Every live subscription to event *name*: the ones :func:`dispatch_event` would test.

    For a publisher that must know, before or as it raises the event, whether
    any assignment can take it (a timer armed under a key no listener hears).
    Assignments only: task watches and flows read their own subscriptions.
    """
    from app.core.database import async_session

    async with async_session() as db:
        return [sub for sub, _assignment, _agent in await _subscriptions(db, name)]


async def _fleet_members(db: Any, agent_ids: set[uuid.UUID]) -> dict[uuid.UUID, frozenset[str]]:
    """The home members of each agent's home fleet, for the ``scope: fleet`` subscribers of one dispatch.

    Two indexed reads for the whole batch, whatever its size: the agents' home
    rows, then every home member of those fleets. An agent no longer in any
    fleet maps to nothing, and its subscription falls back to ``own``
    (``in_scope``): it never widens to every agent.
    """
    if not agent_ids:
        return {}
    from sqlalchemy import select

    from app.models.fleet import FLEET_ROLE_MEMBER, FleetMember

    homes = (
        await db.execute(
            select(FleetMember.agent_id, FleetMember.fleet_id).where(
                FleetMember.agent_id.in_(list(agent_ids)), FleetMember.role == FLEET_ROLE_MEMBER
            )
        )
    ).all()
    fleet_of = {agent_id: fleet_id for agent_id, fleet_id in homes}
    if not fleet_of:
        return {}
    rows = (
        await db.execute(
            select(FleetMember.fleet_id, FleetMember.agent_id).where(
                FleetMember.fleet_id.in_(set(fleet_of.values())),
                FleetMember.role == FLEET_ROLE_MEMBER,
                FleetMember.agent_id.isnot(None),
            )
        )
    ).all()
    members: dict[uuid.UUID, set[str]] = {}
    for fleet_id, member_id in rows:
        members.setdefault(fleet_id, set()).add(str(member_id))
    return {agent_id: frozenset(members.get(fleet_id, ())) for agent_id, fleet_id in fleet_of.items()}


async def _note_live_holder(
    db: Any, assignment_id: uuid.UUID, name: str, payload: Mapping[str, Any], *, pending: bool = False
) -> str | None:
    """Note the event on the work item a live run of the assignment holds; the run id, else ``None``.

    *pending*: the caller's transaction carries a write (a one-off watch's
    fired mark) that must land with the note or with the run, so nothing is
    rolled back here; the note's commit commits it too.
    """
    from sqlalchemy import select

    from app.models.assignment import AssignmentRun
    from app.models.work_item import WorkItem
    from app.services.assignment_engine import ACTIVE_RUN_STATUSES

    if payload.get("object_type") != "work_item":
        return None
    item_id = safe_uuid(payload.get("object_id"))
    if item_id is None:
        return None
    item = (
        await db.execute(select(WorkItem).where(WorkItem.id == item_id).with_for_update())
    ).scalar_one_or_none()
    holder_id = item.claimed_by_run_id if item is not None else None
    # The holder itself, whichever of the assignment's live runs it is.
    live = (
        (
            await db.execute(
                select(AssignmentRun.id).where(
                    AssignmentRun.id == holder_id,
                    AssignmentRun.assignment_id == assignment_id,
                    AssignmentRun.status.in_(list(ACTIVE_RUN_STATUSES)),
                )
            )
        ).scalar_one_or_none()
        if holder_id is not None
        else None
    )
    if item is None or live is None:
        if not pending:
            await db.rollback()
        return None
    marker = f"[event {payload.get('event_id')}]"
    notes = list(item.claim_notes or [])
    if any(marker in str((n or {}).get("text", "")) for n in notes if isinstance(n, dict)):
        # The same event, delivered again: noted once.
        await (db.commit() if pending else db.rollback())
        return str(live)
    now = datetime.now(UTC)
    note = live_note(name, payload, live, now)
    note["text"] = f"{note['text']} {marker}"
    item.claim_notes = [*notes, note]
    item.last_activity_at = now
    await db.commit()
    return str(live)


async def _lock_current(
    db: Any, assignment_id: uuid.UUID, name: str, payload: Mapping[str, Any]
) -> tuple[Any, bool] | None:
    """Lock the subscriber's row and check it still takes event *name*; (row, once), ``None`` when it does not.

    Every delivery goes through it, standing or one-off: the subscriptions were
    read before the lock, so the locked row must still be an enabled
    platform_event trigger on *name* whose filter matches the payload, before
    its deadline, and a one-off watch must not have fired. An edit made in
    between wins. A
    one-off watch is marked fired by this event; nothing is committed here:
    the caller commits the mark with the run (or with the live holder's note),
    and a failure rolls both back, so the watch stays armed.
    """
    from sqlalchemy import select

    from app.models.assignment import Assignment
    from app.services.integration_event_filters import payload_filter_reason

    row = (
        await db.execute(select(Assignment).where(Assignment.id == assignment_id).with_for_update())
    ).scalar_one_or_none()
    config = row.trigger_config if row is not None and isinstance(row.trigger_config, dict) else None
    if (
        row is None
        or not row.enabled
        or config is None
        or watch_fired(config)
        or expired(config)
        # The subscription read before the lock may predate an edit: the locked
        # row must still be a platform_event trigger on this event and filter.
        or row.trigger_type != TRIGGER_TYPE
        or str(name) not in event_names(config)
        or payload_filter_reason(payload, config.get("filter") if isinstance(config.get("filter"), dict) else {})
        is not None
    ):
        await db.rollback()
        return None
    once = config.get(ONCE_KEY) is True
    if once:
        row.trigger_config = {
            **config,
            FIRED_EVENT_KEY: str(payload.get("event_id")),
            FIRED_AT_KEY: datetime.now(UTC).isoformat(),
        }
    return row, once


async def _deliver(name: str, payload: Mapping[str, Any], sub: Subscription, assignment: Any, agent: Any) -> str:
    """Start (or note, or queue) one subscriber; what happened."""
    from app.core.database import async_session
    from app.services.assignment_engine import duplicate_event_run, trigger_or_queue_assignment

    async with async_session() as db:
        # Dedupe first: an event a run already took is never noted or run again.
        original = await duplicate_event_run(
            db,
            sub.agent_id,
            dedup_key(str(payload.get("event_id")), sub.assignment_id),
            window_seconds=DEDUP_WINDOW_SECONDS,
        )
        if original is not None:
            return f"duplicate of run {original}"
        # Every delivery re-reads its row under lock: the locked config, not the
        # snapshot, decides. A one-off watch is consumed by its first matching
        # event, before the live-holder shortcut: in the same transaction as the
        # run it starts, or as the note told to the live run holding the object.
        current = await _lock_current(db, sub.assignment_id, name, payload)
        if current is None:
            # Fired, past its deadline, disabled or edited since the snapshot.
            return "watch already fired or changed"
        assignment, once = current
        context = trigger_context(name, payload, sub.assignment_id, once=once)
        # A live holder gets a note tagged with the event id, which is that
        # event's consumed mark: the same event is never noted twice.
        live = await _note_live_holder(db, sub.assignment_id, name, payload, pending=True)
        if live is not None:
            return f"noted for live run {live}"
        result = await trigger_or_queue_assignment(
            assignment,
            agent,
            payload={"trigger_type": TRIGGER_TYPE, **dict(payload)},
            db=db,
            trigger_context=context,
            overflow="persist",
        )
    return f"{result.status} run {result.run_id}"


async def dispatch_event(name: str, payload: Mapping[str, Any]) -> list[str]:
    """Deliver event *name* to every subscribed assignment in scope; one outcome per delivery."""
    from app.core.database import async_session

    if name not in PLATFORM_EVENTS:
        return []
    try:
        async with async_session() as db:
            subs = await _subscriptions(db, name)
    except Exception as exc:
        # The assignments only: the task watches and the flows read their own.
        logger.warning("Platform event %s: subscriptions unreadable: %s", name, format_exception_message(exc))
        subs = []
    outcomes: list[str] = []
    readable: dict[str, set[str]] = {}
    object_readers: dict[str, bool] = {}
    for sub, assignment, agent in subs:
        if mismatch_reason(sub, name, payload) is not None:
            continue
        if not await can_read_workspace(sub, payload, readable):
            continue
        if not await can_read_object(sub, payload, object_readers):
            continue
        # An assignment never wakes on the end of one of its own runs: a
        # run.completed subscriber would otherwise start itself forever.
        if name.startswith("run.") and payload.get("assignment_id") == str(sub.assignment_id):
            continue
        # Never wake the run that raised the event about itself.
        if payload.get("actor_agent_id") and payload.get("actor_agent_id") == str(sub.agent_id) and (
            getattr(assignment, "ignore_self_triggered_events", False)
        ):
            continue
        try:
            outcomes.append(await _deliver(name, payload, sub, assignment, agent))
        except Exception as exc:
            logger.warning(
                "Platform event %s not delivered to assignment %s: %s",
                name,
                sub.assignment_id,
                format_exception_message(exc),
            )
    # The one-off watches that wake a background task instead of a run.
    from app.services import platform_event_task_watches

    outcomes.extend(await platform_event_task_watches.dispatch_event(name, payload))
    # The flows whose platform-event trigger takes it.
    from app.services.flow_triggers import dispatch_flow_platform_event

    outcomes.extend(await dispatch_flow_platform_event(name, dict(payload)))
    if outcomes:
        logger.info("Platform event %s %s: %s", name, payload.get("event_id"), outcomes)
    return outcomes


async def can_read_workspace(sub: Subscription, payload: Mapping[str, Any], cache: dict[str, set[str]]) -> bool:
    """Whether the subscriber can read every workspace the event touches (none: yes).

    Both the assignment's owner and the user it runs as must read each one: a
    work item event names its ``workspace_id``, a run event the
    ``workspace_ids`` of the items it held. An administrator's ``scope: all``
    assignment skips the owner's check, never the run-as user's: the run, and
    the payload it reads, belong to the identity it executes as.
    """
    wanted = touched_workspaces(payload)
    if not wanted:
        return True
    if sub.scope_all:
        if not sub.run_as_user_id or str(sub.run_as_user_id) == str(sub.user_id):
            return True
        users = {str(sub.run_as_user_id)}
    else:
        users = {str(u) for u in (sub.user_id, sub.run_as_user_id) if u}
    if not users:
        return False
    from app.core.database import async_session
    from app.services.workspaces import workspace_scope_tiers

    for key in users:
        if key not in cache:
            try:
                async with async_session() as db:
                    readable_ids, _ = await workspace_scope_tiers(db, key)
                cache[key] = {str(w) for w in readable_ids}
            except Exception as exc:  # unreadable: refuse, never leak
                logger.warning("Platform event: workspaces of %s unreadable: %s", key, format_exception_message(exc))
                cache[key] = set()
        if not wanted <= cache[key]:
            return False
    return True


def object_identities(sub: Subscription) -> set[str]:
    """The users who must read the object of a viewer-checked event for *sub* to hear it.

    The workspace rule (:func:`can_read_workspace`): the owner and the user the
    run executes as; an administrator's ``scope: all`` subscription skips the
    owner's check, never the run-as user's. Empty: nothing to check.
    """
    if sub.scope_all:
        if not sub.run_as_user_id or str(sub.run_as_user_id) == str(sub.user_id):
            return set()
        return {str(sub.run_as_user_id)}
    return {str(u) for u in (sub.user_id, sub.run_as_user_id) if u}


async def user_reads_object(user_id: str, payload: Mapping[str, Any]) -> bool:
    """Whether *user_id* reads the object of a viewer-checked event; any failure refuses."""
    kind = payload.get("object_type")
    if kind not in ("ctem_case", "ctem_signal"):
        return False  # a viewer-checked event of no known object reaches no one
    from app.core.database import async_session

    try:
        async with async_session() as db:
            if kind == "ctem_signal":
                from app.services.ctem_signals import user_reads_signal

                return await user_reads_signal(db, user_id, payload.get("signal_id"))
            from app.services.ctem_cases_view import user_sees_case

            return await user_sees_case(db, user_id, payload.get("case_id"), payload.get("vertical"))
    except Exception as exc:  # unreadable: refuse, never leak
        logger.warning(
            "Platform event: %s visibility of %s unreadable: %s", kind, user_id, format_exception_message(exc)
        )
        return False


async def can_read_object(sub: Subscription, payload: Mapping[str, Any], cache: dict[str, bool]) -> bool:
    """Whether *sub* may hear a viewer-checked event (#933); any other event: yes.

    Each user of :func:`object_identities` must read the object, decided by
    the object's own per-viewer view; *cache* keeps one answer per user and
    event across the subscriptions of one dispatch.
    """
    if not is_viewer_checked(payload):
        return True
    for user in object_identities(sub):
        if user not in cache:
            cache[user] = await user_reads_object(user, payload)
        if not cache[user]:
            return False
    return True


async def dispatch(event: Any) -> None:
    """Event-bus listener: deliver a platform event to its subscribers."""
    await dispatch_event(event.name, event.data)


_registered = False


def register_listeners() -> None:
    """Register :func:`dispatch` on every platform event name (idempotent)."""
    global _registered
    if _registered:
        return
    from app.services.event_bus import event_bus

    for name in PLATFORM_EVENTS:
        event_bus.add_listener(name, dispatch)
    _registered = True
