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

"""OpenAEV autonomous attack-path run-coordination tools.

These are the run-scoped callbacks the ``openaev-attack-path-orchestrator`` agent
uses to drive a live autonomous attack-path run: narrate the timeline, transition
the run status (including asking the operator a question and parking), consume
operator steering directives, and resolve capability gaps against the installed
threat arsenal.

They target the OpenAEV ``/api/autonomous-runs`` controller
(``AutonomousRunApi``), which is Enterprise-Edition gated on the OpenAEV side and
authenticated with the same integration bearer token as every other OpenAEV tool.
The ``run_id`` is supplied by the orchestration cycle prompt (the decision cycle
always carries the OpenAEV run id), so the tools take it as an explicit argument
rather than capturing it at build time - the same tool set is safe to build once
per integration bind.
"""

from __future__ import annotations

import html
import json
import logging
import re

from app.core.utils import strip_null_bytes
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _api

logger = logging.getLogger(__name__)

# The model occasionally leaks its own tool-call framing INTO a string parameter: the operator-facing
# narration ends and is followed by the literal markup of the next calls (</content>, <invoke ...>,
# <parameter ...>, </invoke>, ...). That raw XML must never reach the operator's reasoning panel, so
# we cut every recorded title/content at the first such marker - everything from there on is machine
# framing, not prose. Matches Anthropic-style and antml:-prefixed tags, opening or closing.
_TOOL_MARKUP_CUTOFF = re.compile(
    r"</?\s*(?:antml:)?(?:invoke|parameter|function_calls|function_result|function|content)\b",
    re.IGNORECASE,
)


def _strip_tool_markup(text: str) -> str:
    """Remove leaked tool-call markup from an operator-facing string, keeping only the prose."""
    if not text:
        return text
    match = _TOOL_MARKUP_CUTOFF.search(text)
    if match:
        text = text[: match.start()]
    return text.strip()


def _decode_entities(text: str) -> str:
    """Undo HTML/XML entity encoding on an operator-facing string.

    The tool-call transport serialises parameters as XML, so a literal ``&`` in a title the model
    wrote (``Credential & secret sweep``) arrives here as ``Credential &amp; secret sweep`` and,
    left as-is, is persisted verbatim into OpenAEV and rendered literally in the attack-path map.
    Decoding folds ``&amp;`` -> ``&``, ``&lt;`` -> ``<`` etc. so human-facing titles/descriptions
    read naturally. Applied only to prose fields, never to a JSON ``content``/``data`` payload.
    """
    if not text:
        return text
    return html.unescape(text)


def _clean_prose(text: str) -> str:
    """Strip leaked tool markup AND decode entities - the standard scrub for a human-facing field."""
    return _decode_entities(_strip_tool_markup(text))


def _build_inject(
    title: str,
    injector_contract_id: str,
    content: str,
    description: str,
    team_ids: list[str] | None,
    asset_ids: list[str] | None,
    asset_group_ids: list[str] | None,
    all_teams: bool | None,
    tag_ids: list[str] | None,
) -> tuple[dict | None, str | None]:
    """Build the OpenAEV inject body shared by authoring and in-place update.

    Returns ``(inject, None)`` on success or ``(None, error_message)`` when ``content`` is not a
    valid JSON OBJECT. Human-facing title/description are scrubbed (tool markup stripped, entities
    decoded) AND NUL-stripped - OpenAEV stores them in text columns and a NUL byte
    fails the write and loses the step. The JSON ``content`` payload is otherwise passed through.
    """
    inject: dict = {
        "inject_title": strip_null_bytes(_clean_prose(title)),
        "inject_injector_contract": injector_contract_id,
        "inject_enabled": True,
    }
    cleaned_description = strip_null_bytes(_clean_prose(description))
    if cleaned_description:
        inject["inject_description"] = cleaned_description
    if content:
        try:
            parsed_content = json.loads(content)
        except ValueError as exc:
            return None, f"content must be a valid JSON object string: {exc}"
        # The server ``inject_content`` is an ObjectNode, so a non-object (a bare
        # array/string/number) is accepted by ``json.loads`` yet 400s opaquely on
        # the author/update path. Reject it here with an actionable message
        # (mirrors ``record_openaev_event``'s data guard) so the failure is
        # explained before the doomed call rather than surfacing as a raw 400.
        if not isinstance(parsed_content, dict):
            return None, (
                "content must be a JSON OBJECT string (the inject's fields, e.g. "
                'content=\'{"command": "..."}\'), not a bare array, string, or '
                "number - OpenAEV's inject_content is an object. Wrap your value "
                "in an object or omit content."
            )
        inject["inject_content"] = parsed_content
    # Targeting: a step normally inherits the run scope, but the orchestrator may narrow it.
    inject["inject_all_teams"] = all_teams if all_teams is not None else False
    if team_ids is not None:
        inject["inject_teams"] = team_ids
    if asset_ids is not None:
        inject["inject_assets"] = asset_ids
    if asset_group_ids is not None:
        inject["inject_asset_groups"] = asset_group_ids
    if tag_ids is not None:
        inject["inject_tags"] = tag_ids
    return inject, None


#: Finding primitive labels the engine understands (mirrors
#: ``io.openaev.database.model.PrimitiveType`` wire labels). A finding-driven trigger references
#: these by their lowercase label.
_PRIMITIVE_LABELS = {
    "account_with_password_not_required",
    "action_output",
    "admin_username",
    "asreproastable_account",
    "asset_group_id",
    "asset_id",
    "computer_name",
    "cve",
    "delegation_account",
    "document",
    "domain",
    "email",
    "file_name",
    "file_path",
    "group_name",
    "hash",
    "host",
    "ipv4",
    "ipv6",
    "ip_subnet",
    "kerberoastable_account",
    "key",
    "number",
    "password",
    "permissions",
    "port",
    "service",
    "severity",
    "share_name",
    "sid",
    "targeted-asset",
    "text",
    "username",
    "value",
    "vulnerability_name",
    "vulnerability_status",
}

#: Friendly aliases (the finding TYPE names) folded onto their primary primitive, mirroring the
#: legacy-label map on ``PrimitiveType`` so the agent can say ``credentials`` and mean ``username``.
_PRIMITIVE_ALIASES = {
    "credentials": "username",
    "portscan": "port",
    "share": "share_name",
    "group": "group_name",
    "computer": "computer_name",
    "password_policy": "text",
    "delegation": "delegation_account",
    "vulnerability": "cve",
}

#: Leaf operators the orchestrator may use in a trigger filter (the structural AND/OR/MAPPER/
#: DEPEND_ON types are built server-side, not passed here).
_TRIGGER_OPERATORS = {"EQ", "NEQ", "IS_NULL", "IS_NOT_NULL", "GT", "GTE", "LT", "LTE", "IN", "NIN"}

#: Operators that compare against a value; only IS_NULL / IS_NOT_NULL take none. A comparison
#: filter with no value (e.g. ``port EQ`` with nothing to match) is malformed - OpenAEV would
#: either reject it or silently never fire - so reject it here with an actionable message.
_VALUE_REQUIRED_OPERATORS = {"EQ", "NEQ", "GT", "GTE", "LT", "LTE", "IN", "NIN"}

_MAPPING_TYPES = {"DEFAULT", "LOCAL", "GLOBAL"}


def _norm_primitive(value: object) -> str | None:
    label = str(value or "").strip().lower()
    label = _PRIMITIVE_ALIASES.get(label, label)
    return label if label in _PRIMITIVE_LABELS else None


def _clean_trigger(trigger: dict | None) -> tuple[dict | None, str | None]:
    """Normalise / validate a finding-driven trigger for the OpenAEV author API.

    A trigger is how a step reacts to findings instead of being hard-wired after another step:
    ``filters`` say WHICH finding fires it (e.g. a finding carrying ``port`` equal to ``445``) and
    ``mappings`` bind discovered finding values into this step's inject inputs (e.g. the discovered
    ``host`` and ``port``). This lets one seed scan fan out onto every host/port it finds, so the
    attack path draws itself.

    ``event_id`` (OPTIONAL) attaches this step to an EXISTING event already on the run's workflow
    instead of minting a new one, so several actions fire off the SAME event rather than each
    duplicating it. Read it from another step's ``event_id`` in the attack-path state. When set,
    ``match`` / ``filters`` / ``event_name`` are dropped (the event already exists and is not
    re-described); only ``mappings`` still apply, binding this step's inject inputs from that event's
    finding values. It is never required - reusing an event is a choice, not an obligation, and a
    seed or standalone action legitimately has no event at all.

    Returns ``(clean, None)`` with a JSON-ready trigger (or ``(None, None)`` when there is nothing
    to send), or ``(None, error)`` describing the first problem so the agent can fix it.
    """
    if not trigger:
        return None, None
    if not isinstance(trigger, dict):
        return None, (
            "trigger must be an object with optional 'event_id', 'match', 'filters' and 'mappings'."
        )
    clean: dict = {}
    # A reused event id short-circuits the event DESCRIPTION (match / filters / event_name): the
    # event already exists on the workflow and is linked by id server-side, so re-describing it is
    # both redundant and, if it disagreed, misleading. Only the step's own mappings still apply.
    # event_id stays OPTIONAL - a blank/absent one is the normal "mint a fresh event (or none)" path.
    reuse_event_id = str(trigger.get("event_id") or "").strip()
    if reuse_event_id:
        clean["event_id"] = reuse_event_id
    match = str(trigger.get("match") or "").strip().upper()
    if match and not reuse_event_id:
        # Reject an out-of-set match rather than silently dropping it: silently
        # ignoring e.g. "ALL" would flip a step the model meant as OR to the
        # server default AND, changing which findings fire it with no error.
        if match not in {"AND", "OR"}:
            return None, (
                f"trigger 'match' must be AND or OR, got '{trigger.get('match')}'. "
                "Omit it to default to AND (all filters must match)."
            )
        clean["match"] = match

    filters_out: list[dict] = []
    for f in [] if reuse_event_id else (trigger.get("filters") or []):
        if not isinstance(f, dict):
            continue
        key_type = _norm_primitive(f.get("key_type"))
        if not key_type:
            return None, (
                f"trigger filter has an unknown key_type '{f.get('key_type')}'. Use a finding "
                "primitive label such as port, host, ipv4, cve, username, password, hash, "
                "share_name, service or severity."
            )
        entry: dict = {"key_type": key_type}
        op = str(f.get("operator") or "").strip().upper()
        value = f.get("value")
        has_value = value is not None and str(value).strip() != ""
        # A concrete value with NO operator would be sent operator-less, and
        # OpenAEV defaults a leaf with no operator to IS_NOT_NULL - which IGNORES
        # the value and fires on ANY finding carrying the primitive (a `port`
        # filter with value 445 but no operator matches EVERY port, not 445). So
        # default to EQ when a value is present, so the value the model supplied
        # is actually compared. With no value, an operator-less filter still means
        # IS_NOT_NULL (fire as soon as the primitive exists) - the documented default.
        if not op and has_value:
            op = "EQ"
        if op:
            if op not in _TRIGGER_OPERATORS:
                return None, (
                    f"trigger filter operator '{op}' is invalid. Use one of: "
                    f"{', '.join(sorted(_TRIGGER_OPERATORS))}."
                )
            if op in _VALUE_REQUIRED_OPERATORS and not has_value:
                return None, (
                    f"trigger filter operator '{op}' on key_type '{key_type}' needs a "
                    "'value' to compare against (only IS_NULL / IS_NOT_NULL take no value). "
                    "Set value, or drop the operator to default to IS_NOT_NULL (fires as soon "
                    "as the finding carries that primitive)."
                )
            entry["operator"] = op
        if value is not None:
            entry["value"] = str(value)
        if isinstance(f.get("case_sensitive"), bool):
            entry["case_sensitive"] = f.get("case_sensitive")
        filters_out.append(entry)
    if filters_out:
        clean["filters"] = filters_out

    mappings_out: list[dict] = []
    for m in trigger.get("mappings") or []:
        if not isinstance(m, dict):
            continue
        input_key = str(m.get("input_key") or "").strip()
        key_type = _norm_primitive(m.get("key_type"))
        if not input_key or not key_type:
            return None, (
                "each trigger mapping needs an 'input_key' (the inject field to fill) and a valid "
                f"'key_type' finding primitive; got input_key='{m.get('input_key')}', "
                f"key_type='{m.get('key_type')}'."
            )
        entry = {"input_key": input_key, "key_type": key_type}
        mt = str(m.get("mapping_type") or "").strip().upper()
        if mt:
            if mt not in _MAPPING_TYPES:
                return None, f"mapping_type '{mt}' is invalid. Use DEFAULT, LOCAL or GLOBAL."
            entry["mapping_type"] = mt
        mappings_out.append(entry)
    if mappings_out:
        clean["mappings"] = mappings_out

    # A reused event needs neither filters nor mappings (the step then simply fires on the shared
    # event with no value binding), so only enforce the "describe something" rule when MINTING an
    # event. Without a reused id, a trigger carrying neither is meaningless and should be omitted.
    if not reuse_event_id and not clean.get("filters") and not clean.get("mappings"):
        return None, (
            "trigger must contain at least one filter or one mapping, or an event_id to reuse an "
            "existing event; otherwise omit the trigger entirely (a step with no trigger and no "
            "parent is a seed that readies immediately against the scope)."
        )

    # event_name titles the EVENT node this trigger becomes in the Logic graph. Without it the node
    # renders as "Untitled event", which makes the authored logic unreadable - so always pass a
    # short, human phrase for the discovery it fires on. OpenAEV derives one from the filters when
    # this is omitted, but an explicit operator-grade name is far clearer. Skipped when reusing an
    # event: the existing event already has its name and is not re-titled.
    if not reuse_event_id:
        event_name = str(trigger.get("event_name") or "").strip()
        if event_name:
            clean["event_name"] = event_name
    return clean, None


#: Bounds on the attack-path state render so a long-running path with many steps
#: and verbose traces cannot balloon the per-cycle prompt (the module fights
#: prompt bloat elsewhere via the tool allow-list). Cap the traces shown per step
#: and truncate each one; the ERROR call-out above the list keeps failures visible.
_STATE_TRACE_CAP = 5
_STATE_TRACE_MAXLEN = 300


def _summarize_trigger_filters(filters: object) -> str:
    """Compact one-line summary of a step's trigger filters, e.g. ``port EQ 445``.

    Accepts BOTH element shapes so the detail renders whatever the source:

    * ``str`` -> the OpenAEV state DTO read-back
      (``AutonomousAttackPathStepState.trigger_filters``, OpenAEV PR #7473) is a
      ``List<String>`` whose elements are ALREADY pre-rendered like ``"port EQ 445"``;
      use each verbatim (stripped; empty skipped).
    * ``dict`` -> the write-side / legacy shape (``{key_type, operator, value}``);
      render it as ``"<key_type> <OP> <value>"``.

    Inert-safe: returns ``""`` for anything that is not a non-empty list, so a state
    DTO that does not (yet) carry ``trigger_filters`` renders no detail rather than
    raising.
    """
    if not isinstance(filters, list):
        return ""
    parts: list[str] = []
    for f in filters:
        # OpenAEV read-back: already a rendered string ("port EQ 445").
        if isinstance(f, str):
            text = f.strip()
            if text:
                parts.append(text)
            continue
        if not isinstance(f, dict):
            continue
        key_type = str(f.get("key_type") or "").strip()
        if not key_type:
            continue
        op = str(f.get("operator") or "IS_NOT_NULL").strip().upper()
        value = f.get("value")
        if value is not None and str(value).strip() != "":
            parts.append(f"{key_type} {op} {value}")
        else:
            parts.append(f"{key_type} {op}")
    return ", ".join(parts)


def _summarize_trigger_mappings(mappings: object) -> str:
    """Compact summary of a step's trigger mappings, e.g. ``host<-host, port<-port``.

    Accepts BOTH element shapes, like :func:`_summarize_trigger_filters`:

    * ``str`` -> the OpenAEV state DTO read-back
      (``AutonomousAttackPathStepState.trigger_mappings``, OpenAEV PR #7473) is a
      ``List<String>`` whose elements are ALREADY pre-rendered like
      ``"ipv4 -> target_host"``; use each verbatim (stripped; empty skipped).
    * ``dict`` -> the write-side / legacy shape (``{input_key, key_type}``); render
      it as ``"<input_key><-<key_type>"``.

    Inert-safe like :func:`_summarize_trigger_filters`.
    """
    if not isinstance(mappings, list):
        return ""
    parts: list[str] = []
    for m in mappings:
        # OpenAEV read-back: already a rendered string ("ipv4 -> target_host").
        if isinstance(m, str):
            text = m.strip()
            if text:
                parts.append(text)
            continue
        if not isinstance(m, dict):
            continue
        input_key = str(m.get("input_key") or "").strip()
        key_type = str(m.get("key_type") or "").strip()
        if input_key and key_type:
            parts.append(f"{input_key}<-{key_type}")
        elif key_type:
            parts.append(f"<-{key_type}")
    return ", ".join(parts)


def _step_wiring(step: dict) -> str:
    """Return a per-step WIRING label: seed / finding-driven / ordering (item 1b).

    Surfaces HOW a step is placed in the logic graph so the orchestrator can tell a
    finding-driven step from a seed at a glance - the invisible-wiring gap that let a
    discovery gate nothing while looking identical to a seed ("root"):

    * ``event_name`` / ``trigger_filters`` / ``trigger_mappings`` present -> a
      FINDING-DRIVEN step; show the event it fires on plus a compact filters/mappings
      summary (the causal wiring that used to be hidden).
    * a ``parent_step_template_id`` and NO trigger -> pure ORDERING (a DEPEND_ON that
      consumes no findings).
    * neither -> a SEED that readies immediately against the run scope.

    Inert-safe: the trigger fields depend on OpenAEV returning them on the state DTO
    (OpenAEV PR #7473 punch-list item 6). Until they land, a finding-driven step carries
    none of them here, so it degrades to the seed/ordering label - the render never
    breaks and lights up automatically once the DTO fields arrive.
    """
    event_name = str(step.get("event_name") or "").strip()
    event_id = str(step.get("event_id") or "").strip()
    filters_summary = _summarize_trigger_filters(step.get("trigger_filters"))
    mappings_summary = _summarize_trigger_mappings(step.get("trigger_mappings"))
    parent = str(step.get("parent_step_template_id") or "").strip()
    if event_name or event_id or filters_summary or mappings_summary:
        wiring = "finding-driven"
        if event_name:
            wiring += f' on "{event_name}"'
        detail: list[str] = []
        # Surface the event_id so the orchestrator can attach ANOTHER action to this SAME event
        # (pass it as the next step's trigger.event_id) instead of duplicating the event.
        if event_id:
            detail.append(f"event_id: {event_id}")
        if filters_summary:
            detail.append(f"filters: {filters_summary}")
        if mappings_summary:
            detail.append(f"maps: {mappings_summary}")
        if detail:
            wiring += " [" + "; ".join(detail) + "]"
        return wiring
    if parent:
        return f"ordering (after {parent})"
    return "seed (fires immediately vs the run scope)"


def _events_overview(steps: object) -> str:
    """Roll up the DISTINCT finding events across the path, so the orchestrator gets the
    "all events" overview it needs to REUSE one instead of duplicating it (the gap the
    operator flagged: unable to see every event or link a step to an existing one).

    Groups steps by their ``event_id`` and lists each event once as
    ``event_id (name) <- N step(s)``, so the model can copy an id straight into a new
    step's ``trigger.event_id`` to attach another action to that SAME event. Returns ``""``
    when no step carries an event (an all-seed / standalone path has nothing to share) - so
    event-less paths add no noise and nothing pushes the model to invent an event.

    Inert-safe: depends only on ``event_id`` / ``event_name`` the state DTO surfaces; a step
    without them is simply not an event and is skipped.
    """
    if not isinstance(steps, list):
        return ""
    # Preserve first-seen order (dict insertion order) so the overview reads top-down like the
    # path; count how many steps fire on each event to show which discoveries are shared hubs.
    events: dict[str, dict] = {}
    for step in steps:
        if not isinstance(step, dict):
            continue
        event_id = str(step.get("event_id") or "").strip()
        if not event_id:
            continue
        entry = events.setdefault(event_id, {"name": "", "count": 0})
        entry["count"] += 1
        if not entry["name"]:
            entry["name"] = str(step.get("event_name") or "").strip()
    if not events:
        return ""
    rows = []
    for event_id, info in events.items():
        name = info["name"] or "(unnamed event)"
        count = info["count"]
        rows.append(f"  - {event_id} ({name}) <- {count} step(s)")
    return (
        "EVENTS in this path (reuse one by passing its id as a new step's trigger.event_id, "
        "with optional mappings, so several actions share it instead of duplicating it):\n"
        + "\n".join(rows)
    )


#: JSON-schema for the finding-driven ``trigger`` object, shared by the author and
#: update step tools so their trigger shape can never drift. Both route the trigger
#: through :func:`_clean_trigger`, so the update tool can CORRECT a mis-wired step in
#: place (change which finding fires it, or convert a step wrongly authored as a free
#: seed into a finding-driven one) without re-authoring a twin.
_TRIGGER_PROPERTY_SCHEMA: dict = {
    "type": "object",
    "description": (
        "Finding-driven trigger (PREFERRED way to place a finding-reactive step). Makes the step "
        "fire off findings and consume their values, so the attack path draws itself. OMIT it "
        "entirely for a seed or standalone action (no event) - many actions legitimately have no "
        "event and you must NOT invent one. To attach this step to an event that ALREADY exists "
        "(so several actions share one event instead of duplicating it), set 'event_id' to that "
        "event's id from the attack-path state with optional 'mappings'."
    ),
    "properties": {
        "event_id": {
            "type": "string",
            "description": (
                "OPTIONAL. Reuse an EXISTING event instead of minting a new one: pass an event_id "
                "read from another step's 'event_id' in the attack-path state, so this step fires "
                "off the SAME event (e.g. one 'SMB service exposed' event feeding many follow-on "
                "actions). When set, event_name / match / filters are ignored (the event already "
                "exists); only mappings still apply. Leave it unset to mint a new event from "
                "filters, or omit the whole trigger for an event-less seed/standalone action. "
                "NEVER required - reuse is a choice, and forcing every action onto an event is wrong."
            ),
        },
        "event_name": {
            "type": "string",
            "description": (
                "Short, human-readable name for the EVENT this trigger represents - the "
                "discovery it fires on, as an operator reads it (e.g. 'SMB service exposed', "
                "'Valid credentials found', 'Open web port discovered'). Titles the event node "
                "in the Logic graph. Set it whenever you MINT a new event (an unnamed event "
                "renders as 'Untitled event'); ignored when 'event_id' reuses an existing event."
            ),
        },
        "match": {
            "type": "string",
            "description": "How to combine filters: AND (all) or OR (any). Default AND.",
        },
        "filters": {
            "type": "array",
            "description": (
                "Predicates that make this step fire. Each: {key_type, operator, value}. "
                "Empty => fire as soon as the mapped key_types exist."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "key_type": {
                        "type": "string",
                        "description": "Finding primitive to test (e.g. port, host, ipv4, cve, username, password, hash, share_name, service, severity).",
                    },
                    "operator": {
                        "type": "string",
                        "description": "EQ, NEQ, IS_NULL, IS_NOT_NULL, GT, GTE, LT, LTE, IN, NIN. Defaults to EQ when a value is given, else IS_NOT_NULL.",
                    },
                    "value": {
                        "type": "string",
                        "description": "Value to compare against (omit for IS_NULL / IS_NOT_NULL).",
                    },
                },
            },
        },
        "mappings": {
            "type": "array",
            "description": "Bind discovered finding values into this step's inject inputs. Each: {input_key, key_type}. This is how the step attacks what upstream steps found.",
            "items": {
                "type": "object",
                "properties": {
                    "input_key": {
                        "type": "string",
                        "description": "Inject content field to fill (e.g. host, port, username, password).",
                    },
                    "key_type": {
                        "type": "string",
                        "description": "Finding primitive to pull the value from (e.g. host, port, username, password, hash).",
                    },
                    "mapping_type": {
                        "type": "string",
                        "description": "GLOBAL (default, workflow-wide finding pool), LOCAL, or DEFAULT.",
                    },
                },
            },
        },
    },
}


_SOURCE = "integration:openaev"

_AUTONOMOUS_URI = "/api/autonomous-runs"

#: Mirrors ``io.openaev.database.model.autonomous.AutonomousEventType``.
_EVENT_TYPES = [
    "NARRATION",
    "DECISION",
    "TOOL_ACTION",
    "HANDOVER",
    "AGENT_DELEGATION",
    "GAP",
    "STATUS",
    "DIRECTIVE",
    "QUESTION",
    "PROOF",
]

#: Mirrors ``io.openaev.database.model.autonomous.AutonomousRunStatus``. The
#: orchestrator drives the run to these from within a cycle; CREATED / PAUSED /
#: CANCELED are operator-driven lifecycle states. PLANNED is set only from a
#: build (non-executing logic authoring) session when the logic is complete and
#: awaits the operator launching the scenario (in normal or autonomous mode).
_RUN_STATUSES = [
    "PLANNING",
    "RUNNING",
    "WAITING_INPUT",
    "COMPLETED",
    "FAILED",
    "PLANNED",
]


def register(base_url: str, headers: dict[str, str], executor: CallableToolExecutor) -> list[ToolDefinition]:
    # step_template_ids OpenAEV has already handed back this bind. OpenAEV makes authoring
    # idempotent WHILE a step is still pending: a second author of the same inject + same parent
    # returns the SAME id instead of minting a twin (the storm guard). Seeing an id twice therefore
    # means the last author was a no-op de-duplication, so we nudge the agent to evaluate/inspect
    # instead of re-firing. This is advisory only - it NEVER blocks the call. The intended
    # try -> tweak -> re-fire loop does NOT rely on re-authoring: to change or re-run an existing
    # step the agent uses update_openaev_attack_path_step (which re-arms it on the engine) + evaluate,
    # keeping one node. Re-authoring an already-executed twin would still mint a fresh id at the
    # engine level, but the agent is steered away from it to avoid duplicate nodes.
    _authored_step_ids: set[str] = set()

    async def record_openaev_event(
        run_id: str = "",
        type: str = "",
        title: str = "",
        content: str = "",
        data: str = "",
    ) -> str:
        run_id = (run_id or "").strip()
        event_type = (type or "").strip().upper()
        # OpenAEV stores autonomous_event_data in a jsonb column and title/content
        # in text columns. PostgreSQL rejects a NUL byte (\u0000)
        # in either, and a jsonb write also rejects non-JSON data - and a failed
        # write loses the whole event, including legitimate narration. Strip NUL
        # from every field (reusing the shared sanitizer) and require data
        # to be valid JSON for EVERY event type, not just PROOF, before sending.
        title = strip_null_bytes(_clean_prose(title))
        content = strip_null_bytes(_clean_prose(content))
        data = strip_null_bytes(data)
        if not run_id:
            return "run_id is required (the OpenAEV autonomous run id from the cycle context)."
        if event_type not in _EVENT_TYPES:
            return f"Unknown event type '{type}'. Use one of: {', '.join(_EVENT_TYPES)}."
        if not title and not content:
            return "Provide a title and/or content so the timeline entry is meaningful."
        parsed_data: dict | None = None
        if data:
            try:
                loaded = json.loads(data)
            except (ValueError, TypeError) as exc:
                return (
                    "data must be a valid JSON object string - OpenAEV stores it in "
                    f"a jsonb column and a non-JSON value fails the write ({exc}). "
                    'Pass structured data like data=\'{"key": "value"}\', or omit '
                    "it."
                )
            # The jsonb column accepts any JSON, but the tool contract and the
            # timeline consumers (options, phase, findings) expect key/value
            # metadata - a bare array/string/number would slip through json.loads
            # yet break them. Require an OBJECT so the failure is explained here.
            if not isinstance(loaded, dict):
                return (
                    "data must be a JSON OBJECT string (key/value metadata like "
                    'data=\'{"key": "value"}\'), not a bare array, string, or '
                    "number - the timeline consumers (options, phase, findings) "
                    "expect an object. Wrap your value in an object or omit data."
                )
            parsed_data = loaded
        # A proof of exploitation is only valid when backed by at least one finding. Guard it
        # client-side too (the OpenAEV API rejects it as well) so the failure is explained
        # precisely rather than surfacing as a raw 400.
        if event_type == "PROOF":
            findings = parsed_data.get("findings") if parsed_data else None
            findings_ok = isinstance(findings, list) and len(findings) > 0
            if not findings_ok:
                return (
                    "A PROOF event must be backed by at least one finding. There is no valid "
                    "proof of exploitation without an associated finding. First find (or "
                    "produce) the substantiating finding with search_openaev_findings, then pass "
                    'it in data as a non-empty JSON array, e.g. data=\'{"findings": '
                    '[{"finding_id": "FINDING_ID", "type": "credentials", "value": "VALUE"}]}\'.'
                )
        body: dict = {"type": event_type}
        if title:
            body["title"] = title
        if content:
            body["content"] = content
        if data:
            body["data"] = data
        _, err = await _api(
            base_url,
            headers,
            "post",
            f"{_AUTONOMOUS_URI}/{run_id}/events",
            "record autonomous event",
            json=body,
        )
        if err:
            return err
        return f"Recorded {event_type} timeline event on run {run_id}."

    async def update_openaev_run_status(
        run_id: str = "",
        status: str = "",
        last_error: str = "",
        title: str = "",
        content: str = "",
    ) -> str:
        run_id = (run_id or "").strip()
        new_status = (status or "").strip().upper()
        title = _clean_prose(title)
        content = _clean_prose(content)
        if not run_id:
            return "run_id is required (the OpenAEV autonomous run id from the cycle context)."
        if new_status not in _RUN_STATUSES:
            return (
                f"Unknown status '{status}'. From a decision cycle drive the run to "
                f"one of: {', '.join(_RUN_STATUSES)}. Use PLANNING (build mode) when "
                "you have resumed designing after operator input and are still "
                "authoring the logic, WAITING_INPUT (with a QUESTION event) when you "
                "need the operator, COMPLETED when the objective is proven or "
                "provably unreachable, FAILED on an unrecoverable error, PLANNED "
                "(build mode only) when the logic is complete and awaits the "
                "operator launching the scenario."
            )
        body: dict = {"status": new_status}
        if last_error:
            body["last_error"] = last_error
        if title:
            body["title"] = title
        if content:
            body["content"] = content
        resp, err = await _api(
            base_url,
            headers,
            "post",
            f"{_AUTONOMOUS_URI}/{run_id}/status",
            "update autonomous run status",
            json=body,
        )
        if err:
            return err
        # OpenAEV returns HTTP 200 with the run object even when it IGNORED the
        # transition: an illegal / stale / terminal transition is a no-op that
        # leaves the run UNCHANGED. Read the returned status back and report
        # honestly instead of claiming a transition that never happened (firing
        # and assuming success is exactly what the operator flagged). Being
        # already in the requested state is a benign no-op, not an error; only a
        # genuinely ignored transition (a DIFFERENT status came back) is surfaced.
        applied = ""
        run: object = None
        if resp is not None:
            try:
                run = resp.json()
            except ValueError:
                # OpenAEV normally returns the run object as JSON; if a 200 ever
                # carries an empty or non-JSON body the status cannot be read
                # back. Degrade to reporting success rather than raising - the
                # transition itself succeeded, only the read-back is unavailable.
                run = None
        if isinstance(run, dict):
            applied = str(run.get("autonomous_run_status") or "").strip().upper()
        if applied and applied != new_status:
            return (
                f"Run {run_id} is still {applied}: OpenAEV did NOT apply the "
                f"requested transition to {new_status} (an illegal, stale, or "
                "terminal transition is a no-op that leaves the run unchanged). "
                f"Do NOT assume the run is {new_status} - re-read the live state "
                "and decide from the ACTUAL status."
            )
        return f"Run {run_id} status set to {new_status}."

    async def consume_openaev_directives(run_id: str = "") -> str:
        run_id = (run_id or "").strip()
        if not run_id:
            return "run_id is required (the OpenAEV autonomous run id from the cycle context)."
        resp, err = await _api(
            base_url,
            headers,
            "post",
            f"{_AUTONOMOUS_URI}/{run_id}/directives/consume",
            "consume autonomous directives",
        )
        if err:
            return err
        directives = resp.json() if resp is not None else []
        if not isinstance(directives, list) or not directives:
            return "No pending operator directives."
        lines = [f"{len(directives)} pending operator directive(s) - honor them now:"]
        for d in directives:
            if not isinstance(d, dict):
                continue
            content = str(d.get("autonomous_directive_content") or "").strip()
            created = d.get("autonomous_directive_created_at") or ""
            if content:
                lines.append(f"- {content}" + (f"  (queued {created})" if created else ""))
        return "\n".join(lines)

    async def resolve_openaev_capabilities(
        techniques: list[str] | None = None,
        output_types: list[str] | None = None,
        platforms: list[str] | None = None,
        objective_template_key: str = "",
    ) -> str:
        body: dict = {}
        if techniques:
            body["techniques"] = techniques
        if output_types:
            body["output_types"] = output_types
        if platforms:
            body["platforms"] = platforms
        if objective_template_key:
            body["objective_template_key"] = objective_template_key
        if not body:
            return (
                "Provide at least one of: techniques (MITRE ATT&CK external ids like "
                "T1110), output_types (credentials, cve, port, kerberoastable_account, "
                "share...), or objective_template_key."
            )
        resp, err = await _api(
            base_url,
            headers,
            "post",
            f"{_AUTONOMOUS_URI}/capabilities/resolve",
            "resolve autonomous capabilities",
            json=body,
            retry_transient=True,
        )
        if err:
            return err
        report = resp.json() if resp is not None else {}
        return _fmt_capability_report(report)

    async def author_openaev_attack_path_step(
        run_id: str = "",
        title: str = "",
        injector_contract_id: str = "",
        content: str = "",
        description: str = "",
        parent_step_template_id: str = "",
        trigger: dict | None = None,
        team_ids: list[str] | None = None,
        asset_ids: list[str] | None = None,
        asset_group_ids: list[str] | None = None,
        all_teams: bool | None = None,
        tag_ids: list[str] | None = None,
    ) -> str:
        run_id = (run_id or "").strip()
        if not run_id:
            return "run_id is required (the OpenAEV autonomous run id from the cycle context)."
        if not title or not injector_contract_id:
            return (
                "title and injector_contract_id are required. Resolve a real "
                "injector_contract_id first with resolve_openaev_capabilities / "
                "search_openaev_injector_contracts - never invent one."
            )
        inject, build_err = _build_inject(
            title,
            injector_contract_id,
            content,
            description,
            team_ids,
            asset_ids,
            asset_group_ids,
            all_teams,
            tag_ids,
        )
        if build_err:
            return build_err

        body: dict = {"inject": inject}
        parent = (parent_step_template_id or "").strip()
        if parent:
            body["parent_step_template_id"] = parent
        trigger_clean, trigger_err = _clean_trigger(trigger)
        if trigger_err:
            return trigger_err
        if trigger_clean:
            body["trigger"] = trigger_clean

        resp, err = await _api(
            base_url,
            headers,
            "post",
            f"{_AUTONOMOUS_URI}/{run_id}/attack-path/steps",
            "author attack-path step",
            json=body,
            # Idempotent while the step is still pending (OpenAEV dedups a repeat
            # of the same inject+parent to the same step_template_id), so a
            # transient 5xx can retry without minting a duplicate node.
            retry_transient=True,
        )
        if err:
            return err
        result = resp.json() if resp is not None else {}
        step_template_id = ""
        if isinstance(result, dict):
            step_template_id = str(result.get("step_template_id") or "")
        # A returned id we have already seen this bind means OpenAEV de-duplicated a still-pending
        # twin (idempotent authoring), i.e. the previous author of this exact step + parent was NOT
        # yet executed. Re-authoring it again achieves nothing - steer the agent to evaluate and
        # inspect instead of looping. Advisory only; the call already went through.
        is_duplicate = bool(step_template_id) and step_template_id in _authored_step_ids
        if step_template_id:
            _authored_step_ids.add(step_template_id)
        if is_duplicate:
            return (
                f"NOTE - no new step created: OpenAEV returned the SAME "
                f"step_template_id={step_template_id} you already authored this session for "
                f"'{title}'. Authoring is idempotent while a step is still pending, so this was a "
                "no-op de-duplication, not a second inject. Do NOT keep re-authoring it: call "
                "evaluate_openaev_attack_path, then read_openaev_attack_path_state to run and "
                "inspect it. To change this step OR to run another pass of it, use "
                "update_openaev_attack_path_step on this step_template_id, then "
                "evaluate_openaev_attack_path (updating re-arms the step so it re-fires) - never "
                "author it again, that only makes a duplicate node."
            )
        # Relieve the step-before-more phase gate: a genuinely new step (not the
        # idempotent de-dup no-op above, which already returned) lets the
        # orchestrator fork/create again. No-op outside the orchestrator's own
        # run (marker not set / counter None).
        from app.services.builtin_tools.filtering import (
            record_orchestrator_step_authored,
        )

        record_orchestrator_step_authored()
        if trigger_clean:
            placement = " as a finding-driven step (fires on a finding and consumes its values)"
        elif parent:
            placement = f" ordered after step {parent} (pure DEPEND_ON, consumes no findings)"
        else:
            placement = " as a seed step (readies immediately against the run scope)"
        return (
            f"Authored attack-path step '{_clean_prose(title)}'{placement}. "
            f"step_template_id={step_template_id}. Prefer building the rest of the path with "
            "finding-driven triggers (so it draws itself from what executes) rather than a linear "
            "chain of parent_step_template_id. On a LIVE run, call evaluate_openaev_attack_path to "
            "execute the ready steps now, then read_openaev_attack_path_state to verify; in AUTHOR "
            "SCENARIO / plan mode there is nothing to execute - keep authoring the logic."
        )

    async def update_openaev_attack_path_step(
        run_id: str = "",
        step_template_id: str = "",
        title: str = "",
        injector_contract_id: str = "",
        content: str = "",
        description: str = "",
        trigger: dict | None = None,
        team_ids: list[str] | None = None,
        asset_ids: list[str] | None = None,
        asset_group_ids: list[str] | None = None,
        all_teams: bool | None = None,
        tag_ids: list[str] | None = None,
    ) -> str:
        run_id = (run_id or "").strip()
        step_template_id = (step_template_id or "").strip()
        if not run_id:
            return "run_id is required (the OpenAEV autonomous run id from the cycle context)."
        if not step_template_id:
            return (
                "step_template_id is required - the id of the EXISTING step to update, taken from "
                "read_openaev_attack_path_state. To CREATE a new step, use "
                "author_openaev_attack_path_step instead."
            )
        if not title or not injector_contract_id:
            return (
                "title and injector_contract_id are required. Resolve a real "
                "injector_contract_id first with resolve_openaev_capabilities / "
                "search_openaev_injector_contracts - never invent one."
            )
        inject, build_err = _build_inject(
            title,
            injector_contract_id,
            content,
            description,
            team_ids,
            asset_ids,
            asset_group_ids,
            all_teams,
            tag_ids,
        )
        if build_err:
            return build_err
        # A mis-wired step is corrected IN PLACE: pass a `trigger` to change WHICH
        # finding fires it (or add one to convert a step wrongly authored as a free
        # seed into a finding-driven step) without re-authoring a twin. Validated
        # through the same `_clean_trigger` as authoring so the shape is identical.
        body: dict = {"inject": inject}
        trigger_clean, trigger_err = _clean_trigger(trigger)
        if trigger_err:
            return trigger_err
        if trigger_clean:
            body["trigger"] = trigger_clean
        _, err = await _api(
            base_url,
            headers,
            "put",
            f"{_AUTONOMOUS_URI}/{run_id}/attack-path/steps/{step_template_id}",
            "update attack-path step",
            json=body,
            retry_transient=True,
        )
        if err:
            return err
        # Relieve the step-before-more phase gate on a successful UPDATE too, not
        # only on author. In launch-from-scenario / prior-plan mode the loop is
        # "fork a payload, then UPDATE an existing seeded step to use it", so a run
        # that only ever updates (never authors) would otherwise leave the gate's
        # step counter at 0 and wedge further forks. A completed in-place update
        # advances the attack path exactly as authoring a new step does.
        from app.services.builtin_tools.filtering import (
            record_orchestrator_step_authored,
        )

        record_orchestrator_step_authored()
        trigger_note = (
            " Its trigger was updated, so it now fires on the corrected finding."
            if trigger_clean
            else ""
        )
        return (
            f"Updated attack-path step {step_template_id} in place ('{_clean_prose(title)}')."
            f"{trigger_note} No new step was created and its existing wiring (trigger/parent) is "
            "preserved unless you passed a new trigger. Updating re-arms the step, so on a LIVE "
            "run, if it had already executed, call evaluate_openaev_attack_path to re-run this "
            "(corrected or unchanged) definition now, then read_openaev_attack_path_state to "
            "verify. Wait for a PENDING/EXECUTING step to finish before re-arming it."
        )

    async def delete_openaev_attack_path_step(
        run_id: str = "", step_template_id: str = ""
    ) -> str:
        run_id = (run_id or "").strip()
        step_template_id = (step_template_id or "").strip()
        if not run_id or not step_template_id:
            return (
                "run_id and step_template_id are required. Read the id of the step to remove "
                "from read_openaev_attack_path_state first."
            )
        _, err = await _api(
            base_url,
            headers,
            "delete",
            f"{_AUTONOMOUS_URI}/{run_id}/attack-path/steps/{step_template_id}",
            "delete attack-path step",
            # Deleting the same step twice is a no-op, so a transient 5xx is safe
            # to retry.
            retry_transient=True,
        )
        if err:
            return err
        return (
            f"Deleted attack-path step {step_template_id} from run {run_id}. Its node and any "
            "pure-ordering edges to it are gone; steps that reacted to a DISCOVERY via a trigger "
            "keep firing on that finding independently. Use this to remove a step you authored by "
            "mistake (a duplicate, a dependent step wrongly created as a free seed you could not "
            "re-wire) - prefer update_openaev_attack_path_step to CORRECT a step in place; only "
            "delete when the node itself should not exist. Then read_openaev_attack_path_state to "
            "confirm the logic is coherent."
        )

    async def _read_attack_path_state(run_id: str):
        """Fetch the run's live step state. Returns ``(steps, error)`` - one is always None."""
        resp, err = await _api(
            base_url,
            headers,
            "get",
            f"{_AUTONOMOUS_URI}/{run_id}/attack-path/state",
            "read attack-path state",
            retry_transient=True,
        )
        if err:
            return None, err
        steps = resp.json() if resp is not None else []
        return (steps if isinstance(steps, list) else []), None

    def _format_attack_path_state(run_id: str, steps: list, header: str) -> str:
        """Render each step's status, WIRING, and traces - finding-driven-first (item 1).

        Surfaces every step's placement (seed / finding-driven / ordering) and, for a
        finding-driven step, the discovery it fires on and how it consumes findings, so the
        orchestrator reads the LOGIC (not a flat list) and can tell a discovery that ignited
        a reactor from one that gated nothing. Calls out FAILED steps so a delivery ERROR is
        never mistaken for an in-flight step, and bounds the render (traces capped +
        truncated) so a long path cannot balloon the per-cycle prompt.
        """
        if not steps:
            return (
                "No steps authored yet on this run. Author your first small recon SEED "
                "(no trigger, no parent - it fires immediately against the scope and MUST "
                "emit findings), evaluate, then read this state again to verify before "
                "continuing."
            )
        errored = [s for s in steps if isinstance(s, dict) and str(s.get("status") or "").upper() == "ERROR"]
        lines = [header]
        lines.append(
            "Each step shows its WIRING (seed / finding-driven / ordering) and its "
            "step_template_id (its STABLE handle). To EXTEND the logic, author a NEW "
            "finding-driven step whose trigger={event_name, filters, mappings} fires on a "
            "discovery an existing step emits; reserve parent_step_template_id for pure "
            "ordering that consumes NO findings. Every discovery MUST have at least one "
            "downstream step whose trigger fires on what it emits (a scan that found port "
            "445 needs the SMB follow-ups triggered ON that finding, not seeded beside it). "
            "When several actions react to the SAME discovery, attach them to the SAME event: "
            "pass that event's event_id (shown below in each finding-driven step's wiring, and "
            "in the EVENTS overview) as the new step's trigger.event_id, with optional mappings - do "
            "NOT re-describe filters/event_name. Reusing an event is OPTIONAL and only for shared "
            "discoveries: seeds and standalone actions have NO event and you must not invent one. "
            "To CHANGE a step (payload / target / contract / title / trigger), call "
            "update_openaev_attack_path_step with its step_template_id - do NOT author a "
            "new one to edit it."
        )
        events_overview = _events_overview(steps)
        if events_overview:
            lines.append(events_overview)
        if errored:
            lines.append(
                f"WARNING: {len(errored)} step(s) FAILED to execute (status=ERROR). A failed step "
                "was NOT delivered - do NOT wait on it or park the run as 'awaiting human "
                "interaction'. Read its trace, diagnose the cause, fix it (e.g. re-run "
                "ensure_openaev_target_team for an 'Email needs at least one user' failure) and "
                "update_openaev_attack_path_step / re-evaluate, or craft an alternative."
            )
        for step in steps:
            if not isinstance(step, dict):
                continue
            step_template_id = step.get("step_template_id") or ""
            target = step.get("target") or "inherits run scope"
            title = step.get("title") or "(untitled)"
            status = str(step.get("status") or "PENDING")
            inject_id = step.get("inject_id") or ""
            contract = step.get("injector_contract_id") or ""
            wiring = _step_wiring(step)
            lines.append(
                f"- [{status}] {title} | {wiring} | step_template_id={step_template_id}, "
                f"target={target}, inject_id={inject_id}, contract={contract}"
            )
            traces = list(step.get("traces") or [])
            for trace in traces[:_STATE_TRACE_CAP]:
                text = str(trace)
                if len(text) > _STATE_TRACE_MAXLEN:
                    text = text[:_STATE_TRACE_MAXLEN] + "... (truncated)"
                lines.append(f"    trace: {text}")
            if len(traces) > _STATE_TRACE_CAP:
                lines.append(f"    trace: (+{len(traces) - _STATE_TRACE_CAP} more)")
        return "\n".join(lines)

    async def evaluate_openaev_attack_path(run_id: str = "") -> str:
        run_id = (run_id or "").strip()
        if not run_id:
            return "run_id is required (the OpenAEV autonomous run id from the cycle context)."
        _, err = await _api(
            base_url,
            headers,
            "post",
            f"{_AUTONOMOUS_URI}/{run_id}/attack-path/evaluate",
            "evaluate attack path",
            # Re-evaluating only readies not-yet-run steps, so it is safe to retry.
            retry_transient=True,
        )
        if err:
            return err
        # Fold the live post-evaluation state straight into the result so the orchestrator SEES what
        # actually happened (statuses + traces, incl. immediate delivery errors like "Email needs at
        # least one user") in this same tool call. Firing and assuming success - the exact behaviour
        # the operator flagged - is no longer possible: a synchronous ERROR is surfaced right here.
        steps, state_err = await _read_attack_path_state(run_id)
        header = (
            f"Evaluated the attack path for run {run_id}. Live step state now (VERIFY before you "
            "narrate, park, or decide - nothing is 'done' or 'in flight' unless its status says so):"
        )
        if state_err:
            return (
                f"Evaluated the attack path for run {run_id}: ready steps are executing. Could not "
                f"read back the state automatically ({state_err}) - call "
                "read_openaev_attack_path_state now and inspect each step's status and traces "
                "before assuming anything."
            )
        return _format_attack_path_state(run_id, steps, header)

    async def read_openaev_attack_path_state(run_id: str = "") -> str:
        run_id = (run_id or "").strip()
        if not run_id:
            return "run_id is required (the OpenAEV autonomous run id from the cycle context)."
        steps, err = await _read_attack_path_state(run_id)
        if err:
            return err
        header = (
            f"Attack-path state for run {run_id} - {len(steps)} step(s) already authored. "
            "Do NOT re-author any of these; wait for PENDING/EXECUTING steps and read their "
            "traces before your next move:"
        )
        return _format_attack_path_state(run_id, steps, header)

    async def promote_openaev_finding_to_asset(
        run_id: str = "", finding_id: str = "", acting_agent_id: str = ""
    ) -> str:
        run_id = (run_id or "").strip()
        finding_id = (finding_id or "").strip()
        if not run_id or not finding_id:
            return (
                "run_id and finding_id are required. Find the finding first with "
                "search_openaev_findings, then pass its finding_id here."
            )
        # Attribute the creation to the agent whose recon produced the value so OpenAEV applies that
        # agent's discovery mode (EXISTING_ONLY forbids it, SCOPED requires it in-scope, EXPANSIVE
        # allows anywhere). Omitted -> the orchestrator's own configured mode.
        params = (
            {"acting_agent_id": acting_agent_id.strip()}
            if (acting_agent_id or "").strip()
            else None
        )
        resp, err = await _api(
            base_url,
            headers,
            "post",
            f"{_AUTONOMOUS_URI}/{run_id}/findings/{finding_id}/promote-to-asset",
            "promote finding to asset",
            params=params,
        )
        if err:
            return err
        result = resp.json() if resp is not None else {}
        asset_id = ""
        asset_name = ""
        if isinstance(result, dict):
            asset_id = str(result.get("asset_id") or "")
            asset_name = str(result.get("asset_name") or "")
        return (
            f"Promoted finding {finding_id} to a targetable asset '{asset_name}' "
            f"(asset_id={asset_id}). The original finding is kept. Target this asset in "
            "your next author_openaev_attack_path_step by passing asset_ids=["
            f'"{asset_id}"] to pivot into attacking it.'
        )

    async def ensure_openaev_target_team(
        run_id: str = "",
        player_ids: list[str] | None = None,
        name: str = "",
        team_id: str = "",
        acting_agent_id: str = "",
    ) -> str:
        run_id = (run_id or "").strip()
        ids = [p.strip() for p in (player_ids or []) if p and p.strip()]
        if not run_id:
            return "run_id is required (the OpenAEV autonomous run id from the cycle context)."
        if not ids:
            return (
                "player_ids is required and must be non-empty. Resolve the person(s) first with "
                "search_openaev_players (or create them with manage_openaev_player), then pass "
                "their user ids here. A team with no members cannot receive a human-targeted inject."
            )
        body: dict = {"player_ids": ids}
        if name:
            body["name"] = name
        if team_id:
            body["team_id"] = team_id.strip()
        # Attribute to the agent whose recon surfaced these recipients so OpenAEV applies that
        # agent's discovery mode: EXISTING_ONLY / SCOPED require them inside the identity
        # allow-scope, EXPANSIVE may reach beyond it. Omitted -> the orchestrator's own configured mode.
        if (acting_agent_id or "").strip():
            body["acting_agent_id"] = acting_agent_id.strip()
        resp, err = await _api(
            base_url,
            headers,
            "post",
            f"{_AUTONOMOUS_URI}/{run_id}/target-teams",
            "ensure target team",
            json=body,
        )
        if err:
            return err
        result = resp.json() if resp is not None else {}
        team = ""
        team_name = ""
        enabled: list = []
        if isinstance(result, dict):
            team = str(result.get("team_id") or "")
            team_name = str(result.get("team_name") or "")
            enabled = result.get("player_ids") or []
        return (
            f"Team '{team_name}' (team_id={team}) is ready: it wraps {len(enabled)} enabled "
            "recipient(s) on the run's simulation. Target it directly in your next "
            f'author_openaev_attack_path_step by passing team_ids=["{team}"] - do NOT author a '
            "human-targeted inject against any other (empty) team."
        )

    def _sync_tradecraft_guard(view: object) -> bool:
        """Teach the tradecraft guard about targets adopted mid-cycle.

        The guard's target set is seeded once, at cycle start. The standard first
        cycle starts with an EMPTY allow-list and only adopts a perimeter after
        the operator answers the scoping question, so without this a lesson
        written later in that same cycle could still name the host the run had
        just taken on.

        Returns whether the view was usable enough to trust. A malformed response
        teaches the guard nothing, so the caller must not treat it as a confirmed
        perimeter.
        """
        try:
            from app.services.autonomous_pentest import (
                scope_view_identifiers,
                scope_view_is_usable,
            )
            from app.services.persistent_memory import extend_tradecraft_run_context

            if not scope_view_is_usable(view):
                return False
            extend_tradecraft_run_context(scope_view_identifiers(view))
            return True
        except Exception:
            logger.debug("Could not sync tradecraft targets from scope", exc_info=True)
            return False

    def _suspend_tradecraft() -> None:
        try:
            from app.services.persistent_memory import suspend_tradecraft_writes

            suspend_tradecraft_writes()
        except Exception:  # pragma: no cover - defensive
            logger.debug("Could not suspend tradecraft writes", exc_info=True)

    def _resume_tradecraft() -> None:
        try:
            from app.services.persistent_memory import resume_tradecraft_writes

            resume_tradecraft_writes()
        except Exception:  # pragma: no cover - defensive
            logger.debug("Could not resume tradecraft writes", exc_info=True)

    async def get_openaev_run_scope(run_id: str = "") -> str:
        run_id = (run_id or "").strip()
        if not run_id:
            return "run_id is required (the OpenAEV autonomous run id from the cycle context)."
        resp, err = await _api(
            base_url,
            headers,
            "get",
            f"{_AUTONOMOUS_URI}/{run_id}/scope",
            "read run scope",
            retry_transient=True,
        )
        if err:
            return err
        view = resp.json() if resp is not None else {}
        _sync_tradecraft_guard(view)
        return _format_scope_view(run_id, view)

    async def set_openaev_run_scope(
        run_id: str = "",
        asset_ids: list[str] | None = None,
        asset_group_ids: list[str] | None = None,
        team_ids: list[str] | None = None,
        player_ids: list[str] | None = None,
    ) -> str:
        run_id = (run_id or "").strip()
        if not run_id:
            return "run_id is required (the OpenAEV autonomous run id from the cycle context)."
        targets: list[dict] = []
        for kind, ids in (
            ("ASSETS", asset_ids),
            ("ASSETS_GROUPS", asset_group_ids),
            ("TEAMS", team_ids),
            ("PLAYERS", player_ids),
        ):
            for raw in ids or []:
                value = (raw or "").strip()
                if value:
                    targets.append({"type": kind, "id": value})
        # Close the window BEFORE the perimeter moves. Tool calls can run
        # concurrently, so a store_memory racing this replacement would otherwise
        # be checked against the outgoing scope; and if the confirming re-read
        # below never succeeds, staying suspended is the fail-closed outcome
        # rather than continuing against a set we know is stale.
        _suspend_tradecraft()
        _, err = await _api(
            base_url,
            headers,
            "put",
            f"{_AUTONOMOUS_URI}/{run_id}/scope",
            "set run scope",
            json={"scope": targets},
            # PUT replaces the allow-list with the same body, so a transient 5xx
            # is safe to retry - the exact scope-PUT 500 the operator hit.
            retry_transient=True,
        )
        # Re-read whether or not the PUT reported success, and re-authorize only
        # on a confirmed read. On success this is what teaches the guard the
        # RESOLVED names rather than the ids sent here: an entity rule carries a
        # UUID the guard already refuses by pattern, while the single-label host
        # behind it is only in the view. On failure it is what makes the outcome
        # safe at all - ``_api`` reports a timeout or a dropped connection exactly
        # like a rejection, so an error does NOT prove the replacement was not
        # committed, and resuming against the outgoing set would authorize a
        # lesson naming a host the run had just taken on.
        view_resp, view_err = await _api(
            base_url,
            headers,
            "get",
            f"{_AUTONOMOUS_URI}/{run_id}/scope",
            "read run scope",
            retry_transient=True,
        )
        if (
            not view_err
            and view_resp is not None
            and _sync_tradecraft_guard(view_resp.json())
        ):
            _resume_tradecraft()
        else:
            logger.warning(
                "Tradecraft writes stay suspended for run %s: the scope could "
                "not be confirmed, so the guard cannot know its targets",
                run_id,
            )
        if err:
            return err
        if not targets:
            return (
                f"Cleared the scope for run {run_id}: the allow-list is now empty (no restricted "
                "perimeter). Record an explicit scope as soon as you have resolved one."
            )
        return (
            f"Recorded the resolved scope on run {run_id}: {len(targets)} target(s) are now the "
            "run's ALLOWLIST perimeter - enforced on the scenario and the live simulation and "
            "shown in the Scope tab (this REPLACES any previous scope and clears OpenAEV's "
            "'scope definition required' warning). Team members in scope are made deliverable. "
            "Call this again whenever the operator or your own analysis changes the perimeter."
        )

    executor.register("record_openaev_event", record_openaev_event)
    executor.register("update_openaev_run_status", update_openaev_run_status)
    executor.register("consume_openaev_directives", consume_openaev_directives)
    executor.register("resolve_openaev_capabilities", resolve_openaev_capabilities)
    executor.register("author_openaev_attack_path_step", author_openaev_attack_path_step)
    executor.register("update_openaev_attack_path_step", update_openaev_attack_path_step)
    executor.register("delete_openaev_attack_path_step", delete_openaev_attack_path_step)
    executor.register("evaluate_openaev_attack_path", evaluate_openaev_attack_path)
    executor.register("read_openaev_attack_path_state", read_openaev_attack_path_state)
    executor.register("promote_openaev_finding_to_asset", promote_openaev_finding_to_asset)
    executor.register("ensure_openaev_target_team", ensure_openaev_target_team)
    executor.register("get_openaev_run_scope", get_openaev_run_scope)
    executor.register("set_openaev_run_scope", set_openaev_run_scope)

    return [
        ToolDefinition(
            name="record_openaev_event",
            description=(
                "Append a timeline event to the live OpenAEV autonomous run so the "
                "operator sees your reasoning and actions animate in real time. Use "
                "type=DECISION (why you chose the next action), TOOL_ACTION (what you "
                "executed and against which targets), GAP (a missing capability + how "
                "to close it), QUESTION (a precise HITL question when you must ask the "
                "operator - pair it with update_openaev_run_status status=WAITING_INPUT), "
                "AGENT_DELEGATION (you are consulting a specialist agent - emit one with "
                'data={"phase":"start","agent_name":"AGENT_NAME"} BEFORE the consult and a paired '
                'data={"phase":"result","agent_name":"AGENT_NAME","status":"ok"|"empty"} after, so the '
                "panel shows a 'delegating to / waiting for AGENT_NAME' state), "
                "PROOF (evidence an objective was achieved), or NARRATION. Write 'content' "
                "in GitHub-flavored Markdown so the reasoning panel reads like the XTM One "
                "chat instead of a text wall: use **bold** for key entities (hosts, accounts, "
                "objectives), `inline code` for identifiers, contract ids, file paths, commands "
                "and finding values, fenced ```code blocks``` for multi-line commands or captured "
                "output, Markdown tables for any multi-column data (kill-chain steps, harvested "
                "credentials, targets, gaps), and bullet or numbered lists for sequences. Be concise: "
                "one or two sentences then a list/table, never a multi-paragraph wall, and for a "
                "QUESTION keep each choice to one short line. Keep the prose operator-grade; put "
                "structured metadata in 'data' (JSON string). A "
                "PROOF is REJECTED unless 'data' carries a non-empty \"findings\" array (each "
                "entry a substantiating finding: finding_id + type + value) - there is no valid "
                "proof without an associated finding, so gather it with search_openaev_findings "
                "first. For a GAP, put the concrete platform next steps in data as "
                '"next_steps" (title + optional description/link) so the operator sees how to '
                "close it. For a QUESTION that offers discrete choices, ALWAYS put them in data as "
                'an "options" array (each entry {"id": short id, "label": what the operator sees, '
                'optional "value": the exact directive text sent when picked}) so the panel renders '
                "beautiful selectable radio buttons; give AT MOST 3 options (the run panel shows "
                "only the first 3 as one-click buttons - extra entries are dropped, so put the 3 "
                "most useful choices first), keep them short and mutually exclusive, and still write "
                "the full prose in 'content'. The operator can always answer with free text instead, "
                "so do NOT force a choice - just offer them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "OpenAEV autonomous run id (from the cycle context).",
                    },
                    "type": {"type": "string", "enum": _EVENT_TYPES},
                    "title": {"type": "string", "description": "Short human title."},
                    "content": {
                        "type": "string",
                        "description": (
                            "Human-readable narration/body in GitHub-flavored Markdown "
                            "(**bold**, `inline code`, fenced code blocks, tables, lists)."
                        ),
                    },
                    "data": {
                        "type": "string",
                        "description": "Optional structured JSON payload (tool i/o, gap suggestions, proof metadata).",
                    },
                },
                "required": ["run_id", "type"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="update_openaev_run_status",
            description=(
                "Transition the OpenAEV autonomous run's lifecycle status. Set "
                "WAITING_INPUT when you are genuinely blocked and need the operator "
                "(always accompany it with a QUESTION event carrying the precise "
                "question); the run parks until a directive arrives. Set COMPLETED when "
                "the objective is proven achieved (or provably unreachable and "
                "reported), FAILED on an unrecoverable error (pass last_error). RUNNING "
                "re-affirms active progress. PLANNED is build mode only: set it when the "
                "logic is complete (pass a concise plan summary as content) so the "
                "operator can review it and launch the scenario."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "OpenAEV autonomous run id (from the cycle context).",
                    },
                    "status": {"type": "string", "enum": _RUN_STATUSES},
                    "last_error": {
                        "type": "string",
                        "description": "Error message when status=FAILED.",
                    },
                    "title": {"type": "string", "description": "Optional short title for the status timeline entry."},
                    "content": {"type": "string", "description": "Optional narration for the status timeline entry."},
                },
                "required": ["run_id", "status"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="consume_openaev_directives",
            description=(
                "Fetch and consume the operator's pending steering directives for the "
                "run (scope changes, 'focus on the finance subnet', 'avoid host X', "
                "answers to a question you asked). Call this at the START of every "
                "decision cycle and honor the directives before anything else. "
                "Consuming marks them handled so they are not returned again."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "OpenAEV autonomous run id (from the cycle context).",
                    },
                },
                "required": ["run_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="resolve_openaev_capabilities",
            description=(
                "Before committing to a technique, check whether the installed OpenAEV "
                "threat arsenal can perform it. Pass the MITRE ATT&CK technique ids "
                "and/or the output/primitive types you need (credentials, cve, port, "
                "kerberoastable_account, share...). Returns, per token, the installed "
                "contracts that satisfy it or - when unsatisfied - the specific catalog "
                "connectors/injectors an operator could enable to close the gap, PLUS a "
                "'Delivery substrate' block (is an HTTP-request injector active for web "
                "payloads, is a live agent available for Command payloads). Follow the "
                "precedence strictly: (1) installed+active contract -> execute now; (2) "
                "nothing installed fits -> craft your own custom arsenal action (payload or "
                "script) on a substrate that EXISTS (HTTP-request injector for web payloads, "
                "or a Command action on an in-scope endpoint with an active agent) and author "
                "it as a chained step - this is your normal second move, do it even when the "
                "catalog lists a tool that would also fit; (3) record a GAP ONLY when you "
                "genuinely cannot self-serve - the technique needs a specialized "
                "connector/injector you cannot replicate as a custom action (e.g. a dedicated "
                "vulnerability scanner), or the substrate a craft would need is missing (then "
                "name exactly what to enable/install, e.g. 'start the HTTP query injector' or "
                "'add an in-scope endpoint with an agent'). A craftable payload is NEVER a gap, "
                "and neither is a branch whose target KIND the operator left out of scope - an "
                "endpoint-only scope makes the human/phishing vector out of scope, not a gap."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "techniques": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "MITRE ATT&CK technique external ids (e.g. T1110, T1566).",
                    },
                    "output_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Desired output/primitive types (credentials, cve, port...).",
                    },
                    "platforms": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional platform filter (Windows, Linux, MacOS).",
                    },
                    "objective_template_key": {
                        "type": "string",
                        "description": "Optional objective template key to seed the resolution.",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="author_openaev_attack_path_step",
            description=(
                "THE ONLY way to build the attack path. Appends ONE chained attack "
                "action (an inject wrapped as a chaining-engine step) to the "
                "autonomous run's workflow so it executes through the chaining engine "
                "and RENDERS in the animated attack-path map. In AUTHOR SCENARIO "
                "(AI planning) mode the run has no simulation, so the step is written "
                "onto the SCENARIO's reusable workflow template instead and nothing "
                "executes - the call shape is identical either way. Never use standalone "
                "injects or atomic tests (they run outside the engine, leave the map "
                "EMPTY, and are denied to you anyway). "
                "BUILD IT FINDING-DRIVEN, not as a linear chain. The attack path draws "
                "ITSELF from what actually executes and what findings come back - you "
                "author the LOGIC, the engine draws the graph. So: "
                "(1) Author one or a few SEED steps with NO parent and NO trigger - broad "
                "recon (e.g. an Nmap sweep) that readies immediately against the scope. A "
                "seed's payload/injector contract must emit findings (an output parser), "
                "because those findings are what drive everything downstream. "
                "(2) For every deeper step, pass a 'trigger' instead of chaining by parent: "
                "'filters' declare WHICH finding fires it (e.g. a finding carrying port==445), "
                "'mappings' bind the discovered finding values into this step's inject "
                "inputs (e.g. host, port, username, password), and 'event_name' names the event "
                "node it becomes in the Logic graph - ALWAYS set a short operator-grade "
                "event_name (e.g. 'SMB service exposed'), or the node shows as 'Untitled event'. "
                "The engine then readies the "
                "step once per matching finding, against every target that finding pointed at "
                "- one seed scan fans out onto every host/port/credential it discovered, "
                "exactly like a hand-built chained scenario. "
                "(3) When several actions react to the SAME discovery, ATTACH them to the same "
                "event instead of each minting its own: read that event's event_id from "
                "read_openaev_attack_path_state (shown in the step wiring and the EVENTS overview) "
                "and pass it as trigger.event_id with optional 'mappings' - event_name/match/filters "
                "are then ignored because the event already exists. This is OPTIONAL and ONLY for "
                "genuinely shared discoveries: event_id is never required, and a seed or a "
                "standalone action legitimately has NO event - do NOT force every action onto an "
                "event or invent one to satisfy a rule. "
                "Use parent_step_template_id ONLY for pure ordering when a step genuinely does "
                "not consume the parent's findings; do NOT model the whole path as a chain of "
                "DEPEND_ONs (that hard-draws the path instead of letting findings shape it). "
                "TWO HARD RULES. (a) CONSUMPTION: every discovery/recon step that emits findings "
                "MUST have at least one downstream step whose trigger fires on what it emits - a "
                "scan that finds port 445 needs the SMB/NetExec follow-ups triggered ON that "
                "finding, never seeded beside it; a discovery whose findings nothing consumes is "
                "thrown-away work (the 'ran a scan, gated nothing on it' failure). (b) A "
                "PRECONDITION IS NOT A SEED: a step that needs something another step must "
                "discover first - SMB/NetExec enumeration needs an open port 445, a "
                "SAM/LSASS/DPAPI credential dump needs a host you can already authenticate to, a "
                "password spray needs an enumerated user - is NOT a seed. Author it finding-driven "
                "with a trigger on that precondition, NEVER as a second free-standing seed next to "
                "the discovery. "
                "Resolve injector_contract_id first with resolve_openaev_capabilities or "
                "search_openaev_injector_contracts - never invent one. After authoring, on a LIVE "
                "run call evaluate_openaev_attack_path to execute the ready steps now; in AUTHOR "
                "SCENARIO / plan mode there is nothing to execute - keep authoring the logic."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "OpenAEV autonomous run id (from the cycle context).",
                    },
                    "title": {"type": "string", "description": "Short name of the attack action / step."},
                    "injector_contract_id": {
                        "type": "string",
                        "description": "Real injector contract id for the technique (resolve it first; never invent).",
                    },
                    "content": {
                        "type": "string",
                        "description": "Optional inject content as a JSON object string (fields for the contract).",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional operator-grade description of the step.",
                    },
                    "parent_step_template_id": {
                        "type": "string",
                        "description": "OPTIONAL, pure-ordering only: step_template_id of a step this one must run AFTER when it does NOT consume that step's findings. Prefer 'trigger' for real attack-path edges. Omit for a seed or a finding-driven step.",
                    },
                    "trigger": _TRIGGER_PROPERTY_SCHEMA,
                    "team_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional target teams (else inherits run scope). An inject can only target TEAMS, never an individual person: for any human-targeted technique (phishing, SMS, credential harvesting, ...) you must first ASK the operator, then wrap the person in a team via ensure_openaev_target_team (creating the person first with manage_openaev_player if needed) and pass the returned team id here.",
                    },
                    "asset_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional target assets (else inherits run scope).",
                    },
                    "asset_group_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional target asset groups (else inherits run scope).",
                    },
                    "all_teams": {"type": "boolean", "description": "Target all teams (default false)."},
                    "tag_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["run_id", "title", "injector_contract_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="update_openaev_attack_path_step",
            description=(
                "Edit a step you ALREADY authored, IN PLACE - do not re-author it. Use it to CHANGE "
                "an existing step's payload, target, injector contract, title, OR its finding-driven "
                "'trigger' (e.g. after it ran and produced nothing, the operator steered you, you "
                "resolved a better target, or you must CORRECT its wiring). Call it with the step's "
                "step_template_id from read_openaev_attack_path_state and the FULL new definition "
                "(title + injector_contract_id are required, same fields as "
                "author_openaev_attack_path_step). It updates the SAME step - preserving its id and "
                "its existing wiring (trigger/parent) unless you pass a NEW 'trigger' - instead of "
                "creating a duplicate, so the graph stays intact and the map does not fill with twin "
                "steps. This is the fix for the 're-creates the same logic instead of updating it' "
                "failure: to CHANGE (or fix a mis-wired) step use THIS; to EXTEND the logic author a "
                "NEW finding-driven step whose 'trigger' fires on a discovery an existing step emits "
                "(reserve parent_step_template_id for pure ordering that consumes no findings). Pass "
                "'trigger' here to re-point WHICH finding fires the step, or to convert a step "
                "wrongly authored as a free SEED into a finding-driven one - every discovery MUST "
                "ignite at least one downstream reactor, and a step that needs a discovered "
                "precondition (an open port, an authenticable host, an enumerated user) is "
                "finding-driven, NEVER a seed. To re-point this step onto an event that ALREADY "
                "exists (so it shares that event instead of duplicating it), pass trigger.event_id "
                "read from the attack-path state, with optional 'mappings'; event_id stays OPTIONAL and "
                "correcting a step never requires an event. After updating, on a LIVE run call "
                "evaluate_openaev_attack_path to re-run it (a step only re-executes once its "
                "previous run has completed), then read the state to verify."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "OpenAEV autonomous run id (from the cycle context).",
                    },
                    "step_template_id": {
                        "type": "string",
                        "description": "Id of the EXISTING step to update (from read_openaev_attack_path_state).",
                    },
                    "title": {"type": "string", "description": "New short name of the attack action / step."},
                    "injector_contract_id": {
                        "type": "string",
                        "description": "Real injector contract id for the technique (resolve it first; never invent).",
                    },
                    "content": {
                        "type": "string",
                        "description": "New inject content as a JSON object string (fields for the contract).",
                    },
                    "description": {
                        "type": "string",
                        "description": "New operator-grade description of the step.",
                    },
                    "trigger": _TRIGGER_PROPERTY_SCHEMA,
                    "team_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "New target teams (else inherits run scope). Same team-only rule as authoring: wrap a person in a team first.",
                    },
                    "asset_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "New target assets (else inherits run scope).",
                    },
                    "asset_group_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "New target asset groups (else inherits run scope).",
                    },
                    "all_teams": {"type": "boolean", "description": "Target all teams (default false)."},
                    "tag_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["run_id", "step_template_id", "title", "injector_contract_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="delete_openaev_attack_path_step",
            description=(
                "Remove a step you authored by MISTAKE from the attack path (a duplicate node, or a "
                "dependent step wrongly created as a free seed that you cannot re-wire in place). "
                "Prefer update_openaev_attack_path_step to CORRECT a step (retarget, swap the "
                "contract, or fix its 'trigger') - it keeps the node and its history. Delete ONLY "
                "when the node itself should not exist. Deleting removes the step's node and any "
                "pure-ordering (DEPEND_ON) edges to it; other steps that react to a DISCOVERY via a "
                "trigger keep firing on that finding independently. Read the id from "
                "read_openaev_attack_path_state first, and read the state again afterwards to "
                "confirm the logic is still coherent."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "OpenAEV autonomous run id (from the cycle context).",
                    },
                    "step_template_id": {
                        "type": "string",
                        "description": "Id of the step to remove (from read_openaev_attack_path_state).",
                    },
                },
                "required": ["run_id", "step_template_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="evaluate_openaev_attack_path",
            description=(
                "Execute the attack path NOW: re-evaluates the run's workflow so every "
                "step you just authored with author_openaev_attack_path_step whose "
                "dependency (parent step) is satisfied readies and fires against the "
                "live simulation immediately, animating the attack-path map. Call it "
                "once after authoring one or more steps in a decision cycle; without it "
                "a freshly authored root step would wait for an in-flight step to finish."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "OpenAEV autonomous run id (from the cycle context).",
                    },
                },
                "required": ["run_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="read_openaev_attack_path_state",
            description=(
                "Read the live state of every step you have ALREADY authored on this run: "
                "each backing inject, its current execution status (PENDING / EXECUTING / "
                "SUCCESS / ERROR / ...), and its execution traces. This is your dedup + "
                "verify read path - call it BEFORE authoring anything so you never re-author "
                "an existing step (the #1 failure mode: duplicate-inject storms), AND call it "
                "again AFTER every evaluate_openaev_attack_path to confirm what actually "
                "happened before you assume anything. Nothing is done, delivered, or 'in "
                "flight' until this read says so. Read each step's status AND traces: SUCCESS = "
                "it ran (now verify what it PRODUCED with search_openaev_findings); ERROR (or a "
                "trace like 'Email needs at least one user') = it FAILED - diagnose, fix the "
                "target/team/payload and re-run, never wait on it or park the run; "
                "PENDING/EXECUTING = still running, wait and re-read, do not pile on duplicates. "
                "Author small batches, evaluate, read this state, verify, then continue."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "OpenAEV autonomous run id (from the cycle context).",
                    },
                },
                "required": ["run_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="promote_openaev_finding_to_asset",
            description=(
                "Promote a discovered finding (an IP / hostname / asset-type finding) into a "
                "real, targetable endpoint asset so you can pivot into attacking it - the "
                "lateral-movement move. The ORIGINAL finding is KEPT and linked to the new "
                "asset. Use it when recon yields a host worth targeting: find the finding "
                "with search_openaev_findings, promote it here, then target the returned "
                "asset_id in your next author_openaev_attack_path_step (asset_ids=[...])."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "OpenAEV autonomous run id (from the cycle context).",
                    },
                    "finding_id": {
                        "type": "string",
                        "description": "Id of the finding to promote (from search_openaev_findings).",
                    },
                    "acting_agent_id": {
                        "type": "string",
                        "description": (
                            "Id of the agent whose recon produced this host (a specialist you "
                            "consulted, else yourself). OpenAEV applies THAT agent's discovery mode: "
                            "EXISTING_ONLY rejects promotion, SCOPED requires the host inside the "
                            "allow-scope, EXPANSIVE allows it anywhere. Omitted -> the orchestrator's "
                            "own configured mode."
                        ),
                    },
                },
                "required": ["run_id", "finding_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="ensure_openaev_target_team",
            description=(
                "Guarantee a targetable team wrapping one or more persons for a human-in-the-loop "
                "step (phishing, smishing, vishing, credential-harvesting, MFA fatigue, malicious "
                "link/attachment). An OpenAEV inject can NEVER target a person directly - it targets "
                "a TEAM, and email/SMS only reaches players ENABLED on the run's simulation. This is "
                "the ONE reliable way to prepare that target: pass the person(s) as player_ids and "
                "it atomically reuses-or-creates a contextual team on the simulation, sets its "
                "members, and enables them for delivery, returning a team_id you target directly. "
                "ALWAYS use this before authoring any human-targeted step - never hand-stitch a team "
                "and enable its members yourself (that is what left teams empty and caused "
                "'Email needs at least one user'). Resolve the person(s) first with "
                "search_openaev_players (or create with manage_openaev_player). Pass team_id to "
                "augment a wrapper you already created instead of making another."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "OpenAEV autonomous run id (from the cycle context).",
                    },
                    "player_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ids of the person(s) to make reachable (from search_openaev_players).",
                    },
                    "name": {
                        "type": "string",
                        "description": "Optional team name; a readable default is derived when omitted.",
                    },
                    "team_id": {
                        "type": "string",
                        "description": "Optional existing wrapper team id to augment instead of creating a new one.",
                    },
                    "acting_agent_id": {
                        "type": "string",
                        "description": (
                            "Id of the agent whose recon surfaced these recipients (a specialist "
                            "you consulted, else yourself). OpenAEV applies THAT agent's discovery "
                            "mode: EXISTING_ONLY / SCOPED require the recipients inside the identity "
                            "allow-scope, EXPANSIVE may reach beyond it. Omitted -> the "
                            "orchestrator's own configured mode."
                        ),
                    },
                },
                "required": ["run_id", "player_ids"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_openaev_run_scope",
            description=(
                "Read the run's LIVE, authoritative scope from OpenAEV: the allow-list (what you "
                "may attack) and the deny-list (carve-outs that always win), across every source "
                "(assets, asset groups, teams, persons, and manual IP / CIDR / hostname rules), "
                "with resolved names. This is the perimeter defined in the database - by the "
                "operator at launch, by the Scope tab, or by your own set_openaev_run_scope call - "
                "NOT the possibly-stale snapshot in your cycle context. Call this FIRST, before "
                "authoring or executing anything, and again whenever you suspect the perimeter "
                "changed. If it comes back EMPTY, scoping is mandatory: ask the operator, then "
                "record it with set_openaev_run_scope."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "OpenAEV autonomous run id (from the cycle context).",
                    },
                },
                "required": ["run_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="set_openaev_run_scope",
            description=(
                "Persist the run's RESOLVED scope onto OpenAEV so it is enforced and visible in "
                "the Scope tab instead of living only in your reasoning. Call this the moment you "
                "have a concrete authorized perimeter - e.g. right after the operator answers a "
                "scoping question ('DESKTOP-81B7CHJ only'), or when you deliberately narrow the "
                "target set yourself. Pass the targets by kind (assets, asset groups, teams, "
                "persons); they REPLACE the run's current allow-list. An empty call clears the "
                "scope. Recording scope also clears OpenAEV's 'scope definition required' warning "
                "and makes team members in scope deliverable. This does NOT execute anything - it "
                "only defines the perimeter your authored steps inherit when they omit an explicit "
                "target."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "OpenAEV autonomous run id (from the cycle context).",
                    },
                    "asset_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Asset (endpoint) ids that make up the in-scope perimeter.",
                    },
                    "asset_group_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Asset group ids in scope.",
                    },
                    "team_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Team ids in scope (the authorized audience).",
                    },
                    "player_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Person (user) ids in scope (the authorized audience).",
                    },
                },
                "required": ["run_id"],
            },
            source=_SOURCE,
        ),
    ]


def _format_scope_view(run_id: str, view: object) -> str:
    """Render the live scope (allow-list + deny-list) read from OpenAEV into operator-grade text.

    Groups entries by target kind so the orchestrator sees, unambiguously, exactly what it may
    attack and what is carved out - the authoritative perimeter from the database, not its own
    memory or the (possibly stale) start-time snapshot.
    """
    allow: list = []
    deny: list = []
    if isinstance(view, dict):
        allow = view.get("allowlist") or []
        deny = view.get("denylist") or []

    def render(entries: list) -> str:
        if not entries:
            return "  (none)"
        by_kind: dict[str, list[str]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("type") or entry.get("source") or "OTHER")
            ident = str(entry.get("id") or "")
            name = str(entry.get("name") or ident)
            label = f"{name} ({ident})" if name and name != ident else ident
            by_kind.setdefault(kind, []).append(label)
        return "\n".join(f"  {kind}: {', '.join(by_kind[kind])}" for kind in sorted(by_kind))

    if not allow and not deny:
        return (
            f"Scope for run {run_id} is EMPTY - OpenAEV has no allow-list and no deny-list defined "
            "yet. Target selection is mandatory: ask the operator which assets / asset groups / "
            "teams / persons are in scope, then record it with set_openaev_run_scope before "
            "authoring or executing anything."
        )
    return (
        f"Authoritative scope for run {run_id} (read live from OpenAEV - respect THIS, not your "
        "memory):\n"
        f"ALLOW-LIST (attack ONLY these):\n{render(allow)}\n"
        f"DENY-LIST (never touch - deny always wins):\n{render(deny)}"
    )


def _fmt_capability_report(report: object) -> str:
    """Render a ``CapabilityReport`` into operator-grade text."""
    if not isinstance(report, dict):
        return "No capability report returned."
    resolutions = report.get("resolutions") or []
    gaps = report.get("gaps") or []
    fully = bool(report.get("fully_satisfied"))
    if not resolutions:
        return "The capability query resolved to nothing (no techniques/output types matched)."

    lines: list[str] = []
    header = (
        "All queried capabilities are satisfied by the installed arsenal."
        if fully
        else f"{len(gaps)} of {len(resolutions)} queried capabilities are NOT satisfied."
    )
    lines.append(header)
    for r in resolutions:
        if not isinstance(r, dict):
            continue
        token = r.get("token") or "?"
        label = r.get("label") or token
        satisfied = bool(r.get("satisfied"))
        mark = "OK" if satisfied else "GAP"
        lines.append(f"[{mark}] {label} ({token})")
        if satisfied:
            contracts = r.get("contracts") or []
            for c in contracts[:5]:
                if isinstance(c, dict):
                    lines.append(
                        f"    - contract {c.get('injector_contract_id', '?')}"
                        f" ({c.get('label', '?')}) via {c.get('injector_type', '?')}"
                    )
        else:
            suggestions = r.get("suggested_connectors") or []
            # Not satisfied by an installed contract is NOT automatically a gap. A gap is only
            # what you genuinely cannot do yourself. First try to craft a custom arsenal action
            # (payload/script) on a delivery substrate that exists; a catalog connector is merely
            # a fallback the operator could enable if crafting is not viable, NOT a reason to stop.
            lines.append(
                "    Not satisfied by an installed contract. This is NOT a gap yet: first craft a "
                "custom arsenal action on a delivery substrate that exists (see 'Delivery "
                "substrate' below)."
            )
            if suggestions:
                lines.append(
                    "    If (and only if) crafting is not viable - the technique needs a "
                    "specialized connector/injector you cannot replicate as a custom action, or "
                    "the substrate is missing - record ONE GAP naming one of these for the "
                    "operator to enable/install:"
                )
                for s in suggestions[:5]:
                    if isinstance(s, dict):
                        title = s.get("title") or s.get("slug") or s.get("connector_id") or "?"
                        link = s.get("subscription_link") or s.get("source_code") or ""
                        lines.append(f"    - {title}" + (f"  {link}" if link else ""))

    lines.append(_fmt_arsenal(report.get("arsenal")))
    return "\n".join(line for line in lines if line)


def _fmt_arsenal(arsenal: object) -> str:
    """Render the delivery-substrate block so the orchestrator grounds custom payloads in reality.

    This is the antidote to the "craft a payload against a phantom host" behaviour: it tells the
    brain whether an HTTP-request injector is actually running (web exploit payloads) and whether a
    live agent exists (Command payloads), plus a deterministic recommendation when neither does.
    """
    if not isinstance(arsenal, dict):
        return ""
    installed = arsenal.get("installed_injectors") or []
    http_ok = bool(arsenal.get("http_delivery_available"))
    cmd_ok = bool(arsenal.get("command_delivery_available"))
    agents = arsenal.get("active_agent_count") or 0
    advice = str(arsenal.get("advice") or "").strip()

    lines = ["", "Delivery substrate (where a crafted payload can run):"]
    lines.append(f"    HTTP-request injector active: {'yes' if http_ok else 'no'}")
    lines.append(f"    Command execution available: {'yes' if cmd_ok else 'no'} ({int(agents)} active agent(s))")
    if installed:
        shown = []
        for inj in installed:
            if not isinstance(inj, dict):
                continue
            state = "active" if inj.get("active") else "INACTIVE"
            shown.append(f"{inj.get('name') or inj.get('injector_type') or '?'} [{state}]")
        if shown:
            lines.append("    Installed injectors: " + ", ".join(shown[:12]))
    if advice:
        lines.append(f"    -> {advice}")
    return "\n".join(lines)


__all__ = ["register"]
