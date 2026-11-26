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

"""One run per inbound event on an agent: the dedup key of event-triggered runs.

One mail can reach an agent several times: through two mailboxes it listens to
(a service account's and a person's, both addressed), through a Graph
notification and the poll that backs it, or through a delegated run another
agent started for the same mail. Each assignment's seen set stops the same
Graph message from running twice on that assignment, but the Graph id of a mail
differs per mailbox and a delegated run has no seen set at all, so the same
reply was sent twice from the same agent (30 Oct 2026).

This module holds what names the inbound event independently of the mailbox and
of the delivery path, the *dedup key*:

- ``microsoft`` / ``new_email``: the ``internetMessageId`` of the mail (RFC 5322
  Message-ID, the same in every mailbox); without one, the conversation, the
  received instant and the sender;
- ``microsoft`` / ``calendar_event_changed``: the ``iCalUId`` of the event and
  the digest of its time, cancellation, subject, location and attendees (one key
  per change an agent acts on, #2216; without a digest, its
  ``lastModifiedDateTime``); without an ``iCalUId``, the event id;
- any other integration event: none (its per-assignment seen set stands, two
  assignments of one agent on one Teams channel or Slack channel each run);
- a delegated or work-item run: the Message-ID named in its task text or the
  ``message_id`` of its work item payload, when there is one.

- any integration event whose assignment configures ``trigger_config.dedup``
  (#10622): the key its template renders over the delivered payload, e.g.
  ``{"key": "{{payload.repository.full_name}}@{{payload.check_run.head_sha}}",
  "window_seconds": 120, "group": "green"}``. Without ``group`` the key
  space is the assignment's own; assignments of one agent naming the same
  ``group`` share it, so a ``workflow_run`` listener and a ``check_run``
  listener woken by one head 0.4 s apart start one run. A template naming a
  field the delivery does not carry keys nothing (the run starts, as
  without ``dedup``). No ``dedup``: today's behaviour.

``run_dedup_key`` picks the right one for a run about to start.

The key rides in ``trigger_context[DEDUP_KEY_CONTEXT]``; the engine refuses a
second run of the same key on the same AGENT within the window
(``trigger_config.dedup_window_seconds``, default
``ASSIGNMENT_EVENT_DEDUP_WINDOW_SECONDS``), across its assignments and across
delivery modes, and records the refusal as a ``skipped`` run naming the run it
duplicates (``trigger_context[DUPLICATE_OF_CONTEXT]``), counted in the
assignment's filtered events. Everything here is pure and unit-tested; the SQL
stays with the engine (``assignment_engine.duplicate_event_run``).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

#: ``trigger_context`` key a run's dedup key is stored under.
DEDUP_KEY_CONTEXT = "dedup_key"
#: ``trigger_context`` key of a dedup-skipped run naming the run it duplicates;
#: a run carrying it is never the original another run is refused for.
DUPLICATE_OF_CONTEXT = "dedup_duplicate_of"
#: The payload ``trigger_type`` values of a run another agent or a work item
#: started for one mail (``delegate_to_assignment``, the work-item inbox).
DELEGATED_TRIGGER_TYPES = frozenset({"delegation", "work_item"})
#: ``trigger_config`` key of an assignment's own dedup window, in seconds. ``0``
#: turns the dedup off for that assignment.
DEDUP_WINDOW_CONFIG = "dedup_window_seconds"
#: How a dedup-skipped run's ``result_summary`` starts.
DUPLICATE_SKIP_PREFIX = "skipped: duplicate of run"

MAX_DEDUP_WINDOW_SECONDS = 24 * 3600
_KEY_MAX_CHARS = 200

#: ``trigger_config`` key of an assignment's own dedup key (#10622).
DEDUP_CONFIG = "dedup"
#: The bounds of a configured dedup window: a configured key never holds an
#: event back for longer than an hour.
MAX_CONFIGURED_WINDOW_SECONDS = 3600
DEFAULT_CONFIGURED_WINDOW_SECONDS = 60
MAX_KEY_TEMPLATE_CHARS = 300
#: How a configured key starts: never the prefix of a mail or calendar key.
CONFIGURED_KEY_PREFIX = "cfg:"
#: The trigger context types a configured key applies to: an event delivery
#: (polled or pushed), never a manual, scheduled or delegated run.
_EVENT_CONTEXT_TYPES = frozenset({"integration_event", "webhook"})
_TEMPLATE_PLACEHOLDER = re.compile(r"\{\{\s*payload\.([A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*)\s*\}\}")
_ANY_TEMPLATE_PLACEHOLDER = re.compile(r"\{\{(.*?)\}\}")
_GROUP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
_CONFIG_KEYS = frozenset({"key", "window_seconds", "group"})
#: The reason a configured-key duplicate is counted under (``Dropped by reason``).
CONFIGURED_DROP_REASON = "dedup: same configured key within the window"
#: The reason any other duplicate is counted under.
DUPLICATE_DROP_REASON = "dedup: same inbound event within the window"

# An RFC 5322 Message-ID as a mail client or Graph writes it: ``<left@right>``
# with no whitespace inside. The angle brackets are what keeps a stray
# ``user@host`` address in a task text from being read as a message id.
_MESSAGE_ID_RE = re.compile(r"<([^<>\s@]+@[^<>\s@]+)>")

#: The payload keys a delegated run's work item may carry the mail's id under.
_PAYLOAD_MESSAGE_ID_KEYS = ("internet_message_id", "internetMessageId", "message_id")


def _text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    return str(value).strip()


def _bounded(key: str) -> str:
    """A key within the stored bound; a longer one is replaced by its SHA-256."""
    if len(key) <= _KEY_MAX_CHARS:
        return key
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def normalize_message_id(value: Any) -> str:
    """An RFC 5322 Message-ID without its angle brackets, lower-cased, or ``""``."""
    text = _text(value)
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    if not text or "@" not in text or any(ch.isspace() for ch in text):
        return ""
    return text.lower()


def mail_dedup_key(payload: Mapping[str, Any] | None) -> str | None:
    """The dedup key of a ``new_email`` payload (``event_poller.microsoft_mail_payload``).

    The Message-ID when the payload carries one; else the conversation, the
    received instant and the sender, which name one mail of one thread as well
    as a mailbox can; else nothing (the run is not deduplicated).
    """
    if not isinstance(payload, Mapping):
        return None
    message_id = normalize_message_id(payload.get("internet_message_id"))
    if message_id:
        return _bounded(f"mail:{message_id}")
    conversation = _text(payload.get("conversation_id"))
    received = _text(payload.get("received_at"))
    sender = _text(payload.get("from")).lower()
    if conversation and received:
        return _bounded(f"mail:{conversation}@{received}:{sender}")
    return None


def calendar_dedup_key(payload: Mapping[str, Any] | None) -> str | None:
    """The dedup key of a ``calendar_event_changed`` payload: one per change of what an agent reads.

    The ``iCalUId`` names the event across mailboxes (an attendee's copy and the
    organizer's share it); without one the mailbox's event id stands in. The
    payload's ``content_digest`` (time, cancellation, subject, location,
    attendees) names the change: a save that changes none of them (a body or
    category edit, a client re-save) carries the same digest and is a duplicate
    within the window (#2216). A payload without a digest keys on its
    ``lastModifiedDateTime``, as before.
    """
    if not isinstance(payload, Mapping):
        return None
    uid = _text(payload.get("ical_uid")) or _text(payload.get("event_id"))
    modified = _text(payload.get("content_digest")) or _text(payload.get("last_modified_at"))
    if not uid or not modified:
        return None
    return _bounded(f"calendar:{uid}@{modified}")


def event_dedup_key(trigger_context: Mapping[str, Any] | None, payload: Mapping[str, Any] | None) -> str | None:
    """The dedup key of an integration-event delivery, or ``None``.

    Only a Microsoft mail or calendar delivery has one: those are the events
    one agent receives several times for one occurrence (two mailboxes, a
    notification and its poll, an attendee's and the organizer's copy). Any
    other event keeps its per-assignment seen set and is not deduplicated
    across assignments; a webhook delivery, a scheduled or a manual run is
    left as it is (``delegated_dedup_key`` covers the delegated pick-up of a
    mail).
    """
    if not isinstance(trigger_context, Mapping) or trigger_context.get("type") != "integration_event":
        return None
    provider = _text(trigger_context.get("provider")).lower()
    event = _text(trigger_context.get("event")).lower()
    if provider == "microsoft" and event == "new_email":
        return mail_dedup_key(payload)
    if provider == "microsoft" and event == "calendar_event_changed":
        return calendar_dedup_key(payload)
    return None


def message_ids_in_text(text: Any) -> list[str]:
    """The normalized Message-IDs a task text names, in order, each once."""
    if not isinstance(text, str) or "<" not in text:
        return []
    seen: list[str] = []
    for match in _MESSAGE_ID_RE.finditer(text):
        normalized = normalize_message_id(match.group(0))
        if normalized and normalized not in seen:
            seen.append(normalized)
    return seen


def delegated_dedup_key(task_text: Any, payload: Mapping[str, Any] | None = None) -> str | None:
    """The dedup key of a delegated or manual run started for one mail, or ``None``.

    The work item payload's ``message_id`` (or ``internet_message_id``) first;
    else the single Message-ID the task text names. A text naming several
    mails is about none of them in particular and is not deduplicated.
    """
    if isinstance(payload, Mapping):
        for key in _PAYLOAD_MESSAGE_ID_KEYS:
            message_id = normalize_message_id(payload.get(key))
            if message_id:
                return _bounded(f"mail:{message_id}")
    found = message_ids_in_text(task_text)
    if len(found) == 1:
        return _bounded(f"mail:{found[0]}")
    return None


def _delegated_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Where a delegated run's payload carries the mail's id: the work item's own payload, else the trigger payload."""
    item = payload.get("work_item")
    if isinstance(item, Mapping):
        inner = item.get("payload")
        return inner if isinstance(inner, Mapping) else item
    return payload


def dedup_config_error(trigger_config: Any) -> str | None:
    """The refusal for ``trigger_config.dedup`` as it will be saved, else ``None``."""
    if not isinstance(trigger_config, Mapping) or trigger_config.get(DEDUP_CONFIG) in (None, {}):
        return None
    raw = trigger_config.get(DEDUP_CONFIG)
    if not isinstance(raw, Mapping):
        return 'dedup must be an object: {"key": "{{payload.a.b}}...", "window_seconds": N, "group": "name"}.'
    unknown = sorted(str(key) for key in raw if key not in _CONFIG_KEYS)
    if unknown:
        return f"dedup takes key, window_seconds and group; unknown: {', '.join(unknown)}."
    template = raw.get("key")
    if not isinstance(template, str) or not template.strip():
        return "dedup.key must be a template naming at least one {{payload.a.b}} field."
    if len(template) > MAX_KEY_TEMPLATE_CHARS:
        return f"dedup.key is longer than {MAX_KEY_TEMPLATE_CHARS} characters."
    if not _TEMPLATE_PLACEHOLDER.search(template):
        return "dedup.key must name at least one {{payload.a.b}} field."
    for match in _ANY_TEMPLATE_PLACEHOLDER.finditer(template):
        if not _TEMPLATE_PLACEHOLDER.fullmatch(match.group(0)):
            return f"dedup.key: {match.group(0)} is not a {{{{payload.a.b}}}} placeholder."
    window = raw.get("window_seconds", DEFAULT_CONFIGURED_WINDOW_SECONDS)
    if isinstance(window, bool) or not isinstance(window, int) or not 1 <= window <= MAX_CONFIGURED_WINDOW_SECONDS:
        return f"dedup.window_seconds must be a whole number from 1 to {MAX_CONFIGURED_WINDOW_SECONDS}."
    group = raw.get("group")
    if group is not None and (not isinstance(group, str) or not _GROUP_NAME.fullmatch(group)):
        return "dedup.group must be 1 to 64 letters, digits, dots, dashes or underscores."
    return None


def configured_dedup(trigger_config: Any) -> dict[str, Any] | None:
    """The assignment's valid ``trigger_config.dedup``, ``None`` when it has none (or an unusable one)."""
    if not isinstance(trigger_config, Mapping) or not trigger_config.get(DEDUP_CONFIG):
        return None
    if dedup_config_error(trigger_config) is not None:
        return None
    raw = trigger_config[DEDUP_CONFIG]
    return {
        "key": raw["key"],
        "window_seconds": int(raw.get("window_seconds", DEFAULT_CONFIGURED_WINDOW_SECONDS)),
        "group": raw.get("group"),
    }


def _plain_payload_value(payload: Any, path: str) -> str | None:
    current = payload
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    if current is None or isinstance(current, (Mapping, list)):
        return None
    if isinstance(current, bool):
        return "true" if current else "false"
    text = str(current).strip()
    return text or None


def configured_dedup_key(
    trigger_config: Any,
    trigger_context: Mapping[str, Any] | None,
    payload: Mapping[str, Any] | None,
    *,
    assignment_id: Any = None,
) -> str | None:
    """The key ``trigger_config.dedup`` renders for an event delivery, ``None`` when it keys nothing.

    ``None`` without a configured dedup, for a run that is not an event
    delivery, or when the template names a field the payload does not carry
    as a plain value (the run then starts, as without ``dedup``). The key
    is scoped to the ``group`` when one is named, else to *assignment_id*.
    """
    config = configured_dedup(trigger_config)
    if config is None or not isinstance(trigger_context, Mapping):
        return None
    if trigger_context.get("type") not in _EVENT_CONTEXT_TYPES:
        return None
    missing = False

    def _value(match: re.Match[str]) -> str:
        nonlocal missing
        value = _plain_payload_value(payload, match.group(1))
        if value is None:
            missing = True
            return ""
        return value.lower()

    rendered = _TEMPLATE_PLACEHOLDER.sub(_value, config["key"])
    if missing:
        return None
    scope = f"group:{config['group']}" if config["group"] else f"assignment:{assignment_id}"
    return _bounded(f"{CONFIGURED_KEY_PREFIX}{scope}:{rendered}")


def is_configured_key(key: Any) -> bool:
    """Whether *key* was rendered from a ``trigger_config.dedup`` template."""
    return isinstance(key, str) and key.startswith(CONFIGURED_KEY_PREFIX)


def drop_reason(key: Any) -> str:
    """The reason a duplicate of *key* is counted under, never a payload value."""
    return CONFIGURED_DROP_REASON if is_configured_key(key) else DUPLICATE_DROP_REASON


def run_dedup_key(
    trigger_context: Mapping[str, Any] | None,
    payload: Mapping[str, Any] | None,
    *,
    agent_id: Any = None,
    trigger_config: Any = None,
    assignment_id: Any = None,
) -> str | None:
    """The dedup key of a run about to start, whatever started it, or ``None``.

    An integration event's key (``event_dedup_key``); for a delegated or
    work-item run (``payload.trigger_type``), the mail its task text or work
    item names (``delegated_dedup_key``). A scheduled, manual, webhook or
    follow-up run has none and is never deduplicated.

    A delegation an agent makes to one of its own assignments
    (``payload.delegated_by_agent_id`` is *agent_id*) is that agent handing
    its own work on, not a second pick-up of the mail: it is not keyed, so a
    mail run that delegates the reply to a sibling assignment is never refused
    as a duplicate of itself. Another agent's delegation for the same mail is.

    An assignment configuring ``trigger_config.dedup`` keys its event
    deliveries with it first (``configured_dedup_key``).
    """
    configured = configured_dedup_key(trigger_config, trigger_context, payload, assignment_id=assignment_id)
    if configured is not None:
        return configured
    key = event_dedup_key(trigger_context, payload)
    if key is not None or not isinstance(payload, Mapping):
        return key
    trigger_type = payload.get("trigger_type")
    if trigger_type not in DELEGATED_TRIGGER_TYPES:
        return None
    if trigger_type == "delegation" and agent_id is not None:
        delegated_by = _text(payload.get("delegated_by_agent_id"))
        if delegated_by and delegated_by == str(agent_id):
            return None
    return delegated_dedup_key(payload.get("task"), _delegated_payload(payload))


def with_dedup_key(trigger_context: dict | None, key: str | None) -> dict | None:
    """``trigger_context`` carrying *key*, when there is one and none is set yet."""
    if not key:
        return trigger_context
    if isinstance(trigger_context, dict) and trigger_context.get(DEDUP_KEY_CONTEXT):
        return trigger_context
    return {**(trigger_context or {}), DEDUP_KEY_CONTEXT: key}


def dedup_window_seconds(trigger_config: Mapping[str, Any] | None, default: int, key: Any = None) -> int:
    """The assignment's dedup window: ``trigger_config.dedup_window_seconds``, else *default*.

    Clamped to ``0..MAX_DEDUP_WINDOW_SECONDS``; ``0`` disables the dedup. A
    value that is not a whole number falls back to *default*. A *key* rendered
    from ``trigger_config.dedup`` takes that object's ``window_seconds``.
    """
    if is_configured_key(key):
        config = configured_dedup(trigger_config)
        if config is not None:
            return int(config["window_seconds"])
    fallback = max(0, min(MAX_DEDUP_WINDOW_SECONDS, int(default)))
    if not isinstance(trigger_config, Mapping) or DEDUP_WINDOW_CONFIG not in trigger_config:
        return fallback
    raw = trigger_config.get(DEDUP_WINDOW_CONFIG)
    if raw is None or isinstance(raw, bool):
        return fallback
    try:
        return max(0, min(MAX_DEDUP_WINDOW_SECONDS, int(raw)))
    except (TypeError, ValueError):
        return fallback


def claimed_skip_summary(holder: str, key: str, window_seconds: int) -> str:
    """The ``result_summary`` of a run refused because another pipeline of the agent claimed the event.

    *holder* is the claim's holder in words (``inbound_claims.describe_holder``):
    an agent account of the same agent answered the mail.
    """
    return (
        f"{DUPLICATE_SKIP_PREFIX} {holder}: the same inbound event (dedup key {key}) "
        f"was already taken by {holder} within the last {int(window_seconds)} s"
    )


def duplicate_skip_summary(original_run_id: Any, key: str, window_seconds: int) -> str:
    """The ``result_summary`` of a run refused as a duplicate."""
    return (
        f"{DUPLICATE_SKIP_PREFIX} {original_run_id}: the same inbound event "
        f"(dedup key {key}) already started a run of this agent within the last "
        f"{int(window_seconds)} s"
    )
