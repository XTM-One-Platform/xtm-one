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

"""Integration-event filters: which events of a trigger create a run at all.

An ``integration_event`` trigger may narrow its events with
``trigger_config.filter``, evaluated by the platform on every event - polled
or pushed - before the run is created and before any gate, so an event it
drops costs no run, no gate model call and no token. Each dropped event is
counted on the assignment (``Assignment.events_filtered_count``).

Mail events (``new_email``, ``email_flag_changed``):

* ``from``: ``{"allow": [...], "deny": [...]}`` - addresses
  (``ceo@example.com``) or domains (``example.com``, which also matches its
  subdomains). With an allow list, only its senders pass; a deny entry always
  wins. ``from_contains`` (a substring of the sender address) is kept for the
  triggers that saved it.
* ``to_me`` / ``cc_me``: only mail where the mailbox owner is a To (resp. Cc)
  recipient; both set, either one.
* ``subject_contains``: a string or a list; the subject holds one of them
  (case-insensitive).
* ``exclude_automated``: drop mail no person wrote - an ``Auto-Submitted``
  header other than ``no``, a ``Precedence`` of bulk / list / junk /
  auto_reply, a mailing-list header (``List-Id`` / ``List-Unsubscribe``), an
  ``X-Auto-Response-Suppress`` header, a delivery or read report
  (``multipart/report``), a null return path, a calendar response, or a
  conventional no-reply / mailer-daemon / postmaster sender.
* ``exclude_meeting_messages``: drop meeting messages (a request, a
  cancellation, an answer), told by the payload's ``message_type``; a mail
  without that field (Gmail) passes.
* ``folder`` is applied where the mail is read (the poller's folder, the
  subscription's resource), not here.

Teams events (``new_teams_message``, ``new_teams_reply``,
``new_teams_chat_message``): ``channels`` (``team_id:channel_id``, as the
poller reads them), ``chat_ids`` and ``exclude_authors`` (a display name or a
user id). Calendar: ``change_kinds`` (``created`` / ``updated`` /
``cancelled``). Flags: ``flag_status`` (``flagged`` / ``complete`` /
``notFlagged``).

Microsoft To Do (``todo_task_changed``): ``lists`` (list names or ids; the
name ``Tasks`` also matches the default list whatever its display name),
``exclude_lists`` (same, always winning), ``change_kinds`` (``created`` /
``updated`` / ``completed`` / ``deleted``). ``exclude_self`` is applied where
the task is read (the poller, the notification), not here.

Jira issue events (``issue_assigned``, ``issue_created``, ``issue_updated``,
``issue_transitioned``, ``comment_created``): ``assignee_account_id`` (``self``:
the connected account), ``project_key``, ``issue_type``, ``status``; ``jql``
narrows the search where the issues are read (``jira_events``).

DocuSign envelope events (``envelope_completed``, ``envelope_declined``,
``envelope_voided``, ``recipient_completed``): ``envelope_ids``,
``signer_emails`` (addresses or domains, as ``from``: for
``recipient_completed`` the signer who signed, for an envelope event any of its
signers), ``sender_emails`` and ``subject_contains``. The status is the
event's own.

Every other event - the webhook-delivered ones above all (GitHub, LinkedIn,
HubSpot, a generic provider), whose payload is the provider's own body - is
matched by the payload itself (``payload_filter_reason``): each key of the
filter names a payload field and its value the value wanted. Subset equality:
an object recurses, a list means any of its values, and a plain value against
a list field holds when it is one of the list's elements (``fleet_impact.targets``
= ``"<agent id>"`` matches ``["<agent id>", ...]``). A key holding a dot is a
path (``workflow_run.conclusion`` reads ``payload["workflow_run"]["conclusion"]``),
unless the payload carries that literal key. ``scope`` and ``channels`` are
applied where the events are read (a poller's channels, a HubSpot object type)
and are left out here.

The payload filter also takes expressions, on top of those values:

* an operator object as a key's value, every operator holding:
  ``{"eq": v}``, ``{"ne": v}``, ``{"in": [...]}``, ``{"not_in": [...]}``,
  ``{"starts_with": s | [...]}``, ``{"ends_with": s | [...]}``,
  ``{"contains": v | [...]}`` (a substring of a text, an element of a list),
  ``{"matches": re | [...]}`` (a whole-value regular expression, bounded by
  ``MAX_PATTERN_CHARS`` and ``MAX_MATCHED_CHARS``) and ``{"exists": bool}``
  (``null`` counts as absent). ``ne``, ``not_in`` and ``exists: false`` pass
  an event without the field; every other operator drops it;
* the combinators ``all`` (a list of filters, each holding), ``any`` (a list,
  one holding) and ``not`` (a filter, not holding), nesting freely.

An object whose keys are all operators is an operator object, ``all`` / ``any``
/ ``not`` holding a list of objects (resp. an object) are combinators:
anything else keeps the meaning it had, so a filter saved before keeps its
result on every event. ``filter_expression_error`` refuses at save time an
expression it cannot evaluate (an unknown operator, a bad operand), naming the
key.

A trigger without a filter passes every event, as before filters existed.
"""

from __future__ import annotations

import contextvars
import functools
import logging
import re
import time
from collections.abc import Iterable, Mapping
from email.utils import getaddresses, parseaddr
from typing import Any

logger = logging.getLogger(__name__)

#: Key under which a poller hands the filter what it needs but the run must
#: not see (raw headers, the resource type); removed before the run is created.
FILTER_SIGNALS_KEY = "_filter_signals"

MAIL_EVENTS = frozenset({"new_email", "email_flag_changed"})
TEAMS_EVENTS = frozenset({"new_teams_message", "new_teams_reply", "new_teams_chat_message"})
DOCUSIGN_EVENTS = frozenset({"envelope_completed", "envelope_declined", "envelope_voided", "recipient_completed"})
JIRA_EVENTS = frozenset({"issue_assigned", "issue_created", "issue_updated", "issue_transitioned", "comment_created"})

_NO_REPLY_LOCAL_PART = re.compile(
    r"^(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer[-_.]?daemon|postmaster|bounces?)(?:[-_.+].*)?$",
    re.IGNORECASE,
)
_BULK_PRECEDENCE = frozenset({"bulk", "list", "junk", "auto_reply"})
_CALENDAR_RESPONSE_TYPES = frozenset({"#microsoft.graph.eventmessageresponse"})


def trigger_filter(trigger_config: Any) -> dict:
    """The ``filter`` object of a trigger config, ``{}`` when it has none."""
    config = trigger_config if isinstance(trigger_config, Mapping) else {}
    raw = config.get("filter")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def address_of(value: Any) -> str:
    """The bare, lower-cased address of *value* (``Name <a@b>`` or ``a@b``)."""
    if not isinstance(value, str):
        return ""
    return parseaddr(value)[1].strip().lower()


def addresses_of(values: Any) -> list[str]:
    """Every bare address in *values*: header text or a list of addresses."""
    if isinstance(values, str):
        return [addr.strip().lower() for _name, addr in getaddresses([values]) if addr.strip()]
    return [addr for addr in (address_of(v) for v in _strings(values)) if addr]


def _matches_sender(address: str, entry: str) -> bool:
    entry = entry.strip().lower()
    if not entry or not address:
        return False
    if "@" in entry and not entry.startswith("@"):
        return address == entry
    domain = entry.lstrip("@")
    sender_domain = address.rpartition("@")[2]
    return sender_domain == domain or sender_domain.endswith("." + domain)


def needs_mailbox(filter_config: Mapping[str, Any]) -> bool:
    """Whether the filter compares recipients with the mailbox owner's addresses."""
    return bool(filter_config.get("to_me") or filter_config.get("cc_me"))


def needs_mail_headers(filter_config: Mapping[str, Any]) -> bool:
    """Whether the filter reads the mail's internet headers."""
    return bool(filter_config.get("exclude_automated"))


def _header_map(headers: Any) -> dict[str, str]:
    """Header name (lower-cased) to value, from Graph's ``[{name, value}]`` or a mapping."""
    found: dict[str, str] = {}
    if isinstance(headers, Mapping):
        items = headers.items()
    elif isinstance(headers, list):
        items = ((h.get("name"), h.get("value")) for h in headers if isinstance(h, Mapping))
    else:
        return found
    for name, value in items:
        if isinstance(name, str) and isinstance(value, str):
            found.setdefault(name.strip().lower(), value.strip())
    return found


def automated_reason(payload: Mapping[str, Any]) -> str | None:
    """Why the mail *payload* reads as written by no person, ``None`` when it does not."""
    signals = payload.get(FILTER_SIGNALS_KEY)
    signals = signals if isinstance(signals, Mapping) else {}
    headers = _header_map(signals.get("headers"))
    auto_submitted = headers.get("auto-submitted", "")
    if auto_submitted and auto_submitted.lower() != "no":
        return "Auto-Submitted header"
    if headers.get("precedence", "").lower() in _BULK_PRECEDENCE:
        return "bulk Precedence header"
    if "list-id" in headers or "list-unsubscribe" in headers:
        return "mailing-list header"
    if "x-auto-response-suppress" in headers:
        return "auto-response-suppress header"
    if "multipart/report" in headers.get("content-type", "").lower():
        return "delivery or read report"
    if headers.get("return-path", "").strip() == "<>":
        return "null return path"
    if str(signals.get("resource_type") or "").lower() in _CALENDAR_RESPONSE_TYPES:
        return "calendar response"
    sender = address_of(payload.get("from"))
    if sender and _NO_REPLY_LOCAL_PART.match(sender.partition("@")[0]):
        return "no-reply sender"
    return None


def _mail_reason(payload: Mapping[str, Any], filter_config: Mapping[str, Any], mailbox: set[str]) -> str | None:
    sender = address_of(payload.get("from"))
    contains = filter_config.get("from_contains")
    if isinstance(contains, str) and contains.strip() and contains.strip().lower() not in sender:
        return "sender does not match from_contains"
    senders = filter_config.get("from")
    if isinstance(senders, Mapping):
        if any(_matches_sender(sender, entry) for entry in _strings(senders.get("deny"))):
            return "sender is on the deny list"
        allow = _strings(senders.get("allow"))
        if allow and not any(_matches_sender(sender, entry) for entry in allow):
            return "sender is not on the allow list"
    if needs_mailbox(filter_config) and mailbox:
        signals = payload.get(FILTER_SIGNALS_KEY)
        signals = signals if isinstance(signals, Mapping) else {}
        to_me = bool(set(addresses_of(signals.get("to", payload.get("to")))) & mailbox)
        cc_me = bool(set(addresses_of(signals.get("cc", payload.get("cc")))) & mailbox)
        wanted_to, wanted_cc = bool(filter_config.get("to_me")), bool(filter_config.get("cc_me"))
        if not ((wanted_to and to_me) or (wanted_cc and cc_me)):
            return "the mailbox is not a " + " or ".join(
                kind for kind, wanted in (("To", wanted_to), ("Cc", wanted_cc)) if wanted
            ) + " recipient"
    subjects = _strings(filter_config.get("subject_contains"))
    if subjects:
        subject = str(payload.get("subject") or "").casefold()
        if not any(needle.casefold() in subject for needle in subjects):
            return "subject does not contain a subject_contains value"
    if filter_config.get("exclude_meeting_messages"):
        kind = str(payload.get("message_type") or "message")
        if kind != "message":
            return f"meeting message ({kind})"
    if filter_config.get("exclude_automated"):
        reason = automated_reason(payload)
        if reason is not None:
            return f"automated mail ({reason})"
    return None


def _teams_reason(event: str, payload: Mapping[str, Any], filter_config: Mapping[str, Any]) -> str | None:
    if event == "new_teams_chat_message":
        chats = _strings(filter_config.get("chat_ids"))
        if chats and str(payload.get("chat_id") or "") not in chats:
            return "chat is not in chat_ids"
    else:
        scope = filter_config.get("scope")
        channels = _strings((scope or {}).get("teams_channel") if isinstance(scope, Mapping) else None) or _strings(
            filter_config.get("channels")
        )
        here = f"{payload.get('team_id') or ''}:{payload.get('channel_id') or ''}"
        if channels and payload.get("channel_id") and here not in channels:
            return "channel is not in channels"
    authors = {entry.casefold() for entry in _strings(filter_config.get("exclude_authors"))}
    if authors:
        signals = payload.get(FILTER_SIGNALS_KEY)
        signals = signals if isinstance(signals, Mapping) else {}
        author = {
            str(payload.get("from_name") or "").casefold(),
            str(payload.get("from_id") or signals.get("from_id") or "").casefold(),
        } - {""}
        if author & authors:
            return "author is excluded"
    return None


def _docusign_reason(event: str, payload: Mapping[str, Any], filter_config: Mapping[str, Any]) -> str | None:
    envelopes = {e.casefold() for e in _strings(filter_config.get("envelope_ids"))}
    if envelopes and str(payload.get("envelope_id") or "").casefold() not in envelopes:
        return "envelope is not in envelope_ids"
    wanted_signers = _strings(filter_config.get("signer_emails"))
    if wanted_signers:
        if event == "recipient_completed":
            signers = addresses_of([payload.get("signer_email") or ""])
        else:
            signers = addresses_of(payload.get("signer_emails") or [])
        if not any(_matches_sender(s, entry) for s in signers for entry in wanted_signers):
            return "no signer is in signer_emails"
    senders = _strings(filter_config.get("sender_emails"))
    if senders:
        sender = address_of(payload.get("sender_email"))
        if not any(_matches_sender(sender, entry) for entry in senders):
            return "sender is not in sender_emails"
    subjects = _strings(filter_config.get("subject_contains"))
    if subjects:
        subject = str(payload.get("subject") or "").casefold()
        if not any(needle.casefold() in subject for needle in subjects):
            return "subject does not contain a subject_contains value"
    return None


#: Filter keys applied where the events are read, never compared with the payload.
_READ_SIDE_KEYS = frozenset({"scope", "channels"})

_MISSING = object()

#: The operators of an operator object.
FILTER_OPERATORS = frozenset(
    {"eq", "ne", "in", "not_in", "starts_with", "ends_with", "contains", "matches", "exists"}
)
#: The combinators a filter takes at its top level and inside another one.
FILTER_COMBINATORS = frozenset({"all", "any", "not"})
#: The operators that hold on an event without the field.
_HOLD_WHEN_MISSING = frozenset({"ne", "not_in"})
#: ``matches``: the longest pattern accepted, and the longest text tested.
MAX_PATTERN_CHARS = 256
MAX_MATCHED_CHARS = 4096
#: How deep combinators nest.
MAX_FILTER_DEPTH = 8
#: A group repeated by a quantifier that itself holds a quantifier or an
#: alternation, ``(a+)+``, ``(a|aa)+``: the shapes of catastrophic
#: backtracking, refused at save time. The evaluation is bounded anyway
#: (``MATCH_TIMEOUT_SECONDS``): this only refuses early what would time out.
_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*}|](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d*,)")
#: ``matches`` runs on ``schema_guard.bounded_search`` with this timeout: a pattern
#: that backtracks past it does not match (the event is dropped).
MATCH_TIMEOUT_SECONDS = 0.05
#: Every ``matches`` of one filter evaluation together, and the number of
#: patterns one filter may hold (refused past it at save).
MATCH_BUDGET_SECONDS = 0.2
MAX_PATTERNS_PER_FILTER = 16
#: The namespace a provider's enrichment writes (``services/event_enrichment.py``).
ENRICHED_KEY = "enriched"
#: Set under ``enriched`` when the enrichment could not be read: every
#: predicate on an enriched field then drops the event, naming why.
ENRICHMENT_UNAVAILABLE_KEY = "unavailable"


def _payload_value(payload: Any, key: str) -> Any:
    """The value *key* names in *payload*: the literal key first, else a dotted path.

    A path segment ending in ``[]`` (``labels[].name``) maps the rest of the
    path over the list it names: the value is the list of what each element
    holds there (elements without it are left out).
    """
    if not isinstance(payload, Mapping):
        return _MISSING
    if key in payload:
        return payload[key]
    if "." not in key and not key.endswith("[]"):
        return _MISSING
    return _walk(payload, key.split("."))


def _walk(current: Any, parts: list[str]) -> Any:
    for index, part in enumerate(parts):
        each = part.endswith("[]")
        name = part[:-2] if each else part
        if not isinstance(current, Mapping) or name not in current:
            return _MISSING
        current = current[name]
        if each:
            if not isinstance(current, list):
                return _MISSING
            rest = parts[index + 1 :]
            if not rest:
                return list(current)
            values = (_walk(item, rest) for item in current)
            return [v for v in values if v is not _MISSING]
    return current


def _is_operator_object(value: Any) -> bool:
    """An object every key of which is an operator: ``{"ne": "x"}``."""
    return isinstance(value, Mapping) and bool(value) and all(k in FILTER_OPERATORS for k in value)


def _is_combinator(key: Any, value: Any) -> bool:
    """``all`` / ``any`` holding a list of objects, ``not`` holding an object.

    ``not`` holding an operator object (``{"not": {"eq": 1}}``) stays the
    payload field ``not``, as ``all`` / ``any`` holding anything else do: the
    way to filter a field so named.
    """
    if key in ("all", "any"):
        return isinstance(value, list) and bool(value) and all(isinstance(v, Mapping) for v in value)
    return key == "not" and isinstance(value, Mapping) and not _is_operator_object(value)


def _scalar_matches(actual: Any, expected: Any) -> bool:
    """A plain value against a field: equal, or one element of a list field."""
    if actual is _MISSING:
        return False
    if actual == expected:
        return True
    return isinstance(actual, list) and expected in actual


def _value_matches(actual: Any, expected: Any) -> bool:
    """Whether *actual* is what *expected* asks for.

    An object is a subset, a list means any of its values, and a plain value
    against a list field holds when it is one of the list's elements.
    """
    if _is_operator_object(expected):
        return _operators_reason("", actual, expected) is None
    if isinstance(expected, Mapping):
        return isinstance(actual, Mapping) and all(
            _value_matches(_payload_value(actual, str(key)), value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        # A scalar option compares directly: a bare None option must not match
        # every value (the semantics of flow_runner.matches_event_filter).
        return any(
            _value_matches(actual, option) if isinstance(option, Mapping) else _scalar_matches(actual, option)
            for option in expected
        )
    return _scalar_matches(actual, expected)


def _options(operand: Any) -> list[Any]:
    return list(operand) if isinstance(operand, list | tuple) else [operand]


_LEADING_GLOBAL_FLAGS = re.compile(r"(?:\(\?[aiLmsux]+\))+")


@functools.lru_cache(maxsize=512)
def _anchored(pattern: str) -> str | None:
    """*pattern* anchored to the whole value, ``None`` when ``re`` refuses it or it is too long."""
    if len(pattern) > MAX_PATTERN_CHARS:
        return None
    # A leading global flag group (``(?i)``) must stay at the very start.
    flags = _LEADING_GLOBAL_FLAGS.match(pattern)
    head = flags.group(0) if flags else ""
    anchored = rf"{head}\A(?:{pattern[len(head):]})\Z"
    try:
        re.compile(anchored)
    except re.error:
        return None
    return anchored


#: The deadline (``time.monotonic``) of the filter being evaluated: every
#: ``matches`` of one evaluation shares ``MATCH_BUDGET_SECONDS``, so many
#: operands or branches cannot add their timeouts up.
_MATCH_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar("_MATCH_DEADLINE", default=None)


def _fullmatch(pattern: Any, actual: Any) -> bool:
    """Whether *pattern* matches all of *actual*, within ``MATCH_TIMEOUT_SECONDS`` and the evaluation's budget."""
    if not isinstance(pattern, str) or not isinstance(actual, str) or len(actual) > MAX_MATCHED_CHARS:
        return False
    anchored = _anchored(pattern)
    if anchored is None:
        return False
    timeout = MATCH_TIMEOUT_SECONDS
    deadline = _MATCH_DEADLINE.get()
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            logger.info("The filter's pattern budget is spent: not a match")
            return False
    # The repository's one bounded engine (``schema_guard.bounded_search``):
    # a pattern with no verdict in time, or one the engines could read
    # differently, is not a match (the event is dropped).
    from app.services.schema_guard import REGEX_ERROR, PatternUnjudgedError, bounded_search

    refused: tuple[type[BaseException], ...] = (re.error,) + (
        REGEX_ERROR if isinstance(REGEX_ERROR, tuple) else (REGEX_ERROR,)
    )
    try:
        return bounded_search(anchored, actual, remaining=timeout)
    except PatternUnjudgedError as exc:
        logger.info("A filter pattern gave no verdict on a %d-character value (%s): not a match", len(actual), exc.why)
        return False
    except refused:
        return False


def _contains(actual: Any, operand: Any) -> bool:
    if isinstance(actual, str):
        return isinstance(operand, str) and operand in actual
    if isinstance(actual, list):
        return operand in actual
    return False


def _operator_holds(op: str, actual: Any, operand: Any) -> bool:
    """Whether one operator holds on *actual* (``_MISSING`` when the event has no field)."""
    present = actual is not _MISSING and actual is not None
    if op == "exists":
        return present is bool(operand)
    if actual is _MISSING:
        return op in _HOLD_WHEN_MISSING
    if op == "eq":
        return actual == operand
    if op == "ne":
        return actual != operand
    if op == "in":
        return any(actual == option for option in _options(operand))
    if op == "not_in":
        return all(actual != option for option in _options(operand))
    if op == "starts_with":
        return isinstance(actual, str) and any(
            isinstance(o, str) and actual.startswith(o) for o in _options(operand)
        )
    if op == "ends_with":
        return isinstance(actual, str) and any(isinstance(o, str) and actual.endswith(o) for o in _options(operand))
    if op == "contains":
        return any(_contains(actual, option) for option in _options(operand))
    if op == "matches":
        return any(_fullmatch(option, actual) for option in _options(operand))
    return False


def _operators_reason(key: str, actual: Any, operators: Mapping[str, Any]) -> str | None:
    for op, operand in operators.items():
        if not _operator_holds(op, actual, operand):
            if actual is _MISSING and op != "exists":
                return f"the event has no {key}" if key else f"the field is missing ({op})"
            return f"{key} fails {op}" if key else f"{op} does not hold"
    return None


def _enrichment_unavailable(payload: Mapping[str, Any], key: str) -> str | None:
    """Why an ``enriched.`` key cannot be read on *payload*, ``None`` when it can."""
    if not key.startswith(f"{ENRICHED_KEY}."):
        return None
    enriched = payload.get(ENRICHED_KEY) if isinstance(payload, Mapping) else None
    if isinstance(enriched, Mapping) and enriched.get(ENRICHMENT_UNAVAILABLE_KEY):
        return f"{key} could not be read: {enriched[ENRICHMENT_UNAVAILABLE_KEY]}"
    return None


def _carries(payload: Any, key: str) -> bool:
    return isinstance(payload, Mapping) and key in payload


def field_matches(item: Mapping[str, Any], key: str, expected: Any) -> bool:
    """One field of an object against a match value, outside any event.

    The shared state list match (``where_json``, ``remove_json``,
    ``update_json``) reuses this grammar: an operator object is evaluated as
    in a filter, a missing field included (``exists: false``, ``ne`` and
    ``not_in`` hold on it); any other value is plain equality on a present
    field, with no subset or any-of reading.
    """
    actual = item.get(key, _MISSING)
    if _is_operator_object(expected):
        return _operators_reason(key, actual, expected) is None
    return actual is not _MISSING and actual == expected


def match_value_error(key: str, value: Any) -> str | None:
    """Why *value*, a match value of *key*, cannot be evaluated; ``None`` when it can.

    An object mixing operators with other keys, a misspelled operator
    (``startswith``) or a bad operand is refused, naming the operators.
    """
    error = _value_error(key, value, 0)
    if error is not None and "Operators:" not in error:
        error = f"{error} Operators: {', '.join(sorted(FILTER_OPERATORS))}."
    return error


def _expression_reason(payload: Mapping[str, Any], filter_config: Mapping[str, Any], depth: int) -> str | None:
    for key, expected in filter_config.items():
        if not isinstance(key, str) or key in _READ_SIDE_KEYS:
            continue
        # A field the payload carries under the combinator's own name keeps
        # the legacy meaning (subset / any of) of a filter saved before.
        if _is_combinator(key, expected) and depth < MAX_FILTER_DEPTH and not _carries(payload, key):
            if key == "all":
                for index, branch in enumerate(expected):
                    reason = _expression_reason(payload, branch, depth + 1)
                    if reason is not None:
                        return f"all[{index}]: {reason}"
            elif key == "any":
                reasons = [_expression_reason(payload, branch, depth + 1) for branch in expected]
                if all(r is not None for r in reasons):
                    return f"any: no branch holds ({'; '.join(r for r in reasons if r)})"
            elif _expression_reason(payload, expected, depth + 1) is None:
                return "not: the negated filter holds"
            continue
        if (unavailable := _enrichment_unavailable(payload, key)) is not None:
            return unavailable
        actual = _payload_value(payload, key)
        if _is_operator_object(expected):
            reason = _operators_reason(key, actual, expected)
            if reason is not None:
                return reason
            continue
        if actual is _MISSING:
            return f"the event has no {key}"
        if not _value_matches(actual, expected):
            return f"{key} does not match the filter"
    return None


def payload_filter_reason(payload: Mapping[str, Any], filter_config: Mapping[str, Any] | None) -> str | None:
    """Why the payload filter drops *payload*, ``None`` when every key matches.

    The generic filter of the events outside the mail, Teams, DocuSign and
    calendar families: each key a payload field (a dotted path reaches a nested
    one, a literal key holding a dot wins), each value the value wanted (an
    object recurses, a list means any of, an operator object applies its
    operators), and the ``all`` / ``any`` / ``not`` combinators. The reason
    names the first key that failed.
    """
    if not filter_config:
        return None
    # A filter naming the ``enriched.`` facts drops the event when they could
    # not be read, whatever the combinators around them: under ``not`` or
    # ``any`` a missing fact would otherwise widen the filter on an outage.
    if needs_enrichment(filter_config):
        enriched = payload.get(ENRICHED_KEY) if isinstance(payload, Mapping) else None
        if isinstance(enriched, Mapping) and enriched.get(ENRICHMENT_UNAVAILABLE_KEY):
            return f"the enriched facts could not be read: {enriched[ENRICHMENT_UNAVAILABLE_KEY]}"
    token = _MATCH_DEADLINE.set(time.monotonic() + MATCH_BUDGET_SECONDS)
    try:
        return _expression_reason(payload, filter_config, 0)
    finally:
        _MATCH_DEADLINE.reset(token)


def filter_keys(filter_config: Any, depth: int = 0) -> list[str]:
    """Every payload key a filter reads, combinators walked (``needs`` checks)."""
    if not isinstance(filter_config, Mapping) or depth > MAX_FILTER_DEPTH:
        return []
    keys: list[str] = []
    for key, value in filter_config.items():
        if not isinstance(key, str):
            continue
        if _is_combinator(key, value):
            for branch in value if isinstance(value, list) else [value]:
                keys.extend(filter_keys(branch, depth + 1))
        else:
            keys.append(key)
    return keys


def needs_enrichment(filter_config: Any) -> bool:
    """Whether a filter reads the ``enriched`` namespace a provider fills before filtering."""
    return any(k == ENRICHED_KEY or k.startswith(f"{ENRICHED_KEY}.") for k in filter_keys(filter_config))


TODO_EVENT = "todo_task_changed"


def _list_names(value: Any) -> set[str]:
    return {s.casefold() for s in _strings(value)}


def todo_list_matches(list_id: str, list_name: str, wellknown: str, filter_config: Mapping[str, Any]) -> bool:
    """Whether a To Do list passes ``lists`` / ``exclude_lists`` (ids or names; ``Tasks``
    also names the default list, ``wellknownListName`` ``defaultList``)."""
    names = {list_id.casefold(), list_name.casefold()} - {""}
    if wellknown == "defaultList":
        names.add("tasks")
    elif wellknown == "flaggedEmails":
        names.add("flagged emails")
    if names & _list_names(filter_config.get("exclude_lists")):
        return False
    wanted = _list_names(filter_config.get("lists"))
    return not wanted or bool(names & wanted)


def _todo_reason(payload: Mapping[str, Any], filter_config: Mapping[str, Any]) -> str | None:
    signals = payload.get(FILTER_SIGNALS_KEY) or {}
    wellknown = str(signals.get("list_wellknown") or "") if isinstance(signals, Mapping) else ""
    if not todo_list_matches(
        str(payload.get("list_id") or ""), str(payload.get("list_name") or ""), wellknown, filter_config
    ):
        return "task list is excluded or not in lists"
    kinds = {k.casefold() for k in _strings(filter_config.get("change_kinds"))}
    if kinds and str(payload.get("change_kind") or "").casefold() not in kinds:
        return "change kind is not in change_kinds"
    return None


def plain_filter_reason(event: str, payload: Mapping[str, Any], filter_config: Any) -> str | None:
    """Why the entries of a filter reading no ``enriched.`` key drop *payload*, before any fact is read.

    The top-level entries of a filter all hold for it to hold, so a drop by
    the entries that read the delivery alone (``action``, a conclusion, a
    repository, a combinator naming no enriched key) is the filter's drop:
    the enriched facts are then never read, and the reason names the key
    rather than an enrichment outage. ``None`` when the filter reads no
    enriched key (the caller evaluates it whole) or those entries all hold.
    The mail, Teams, DocuSign and calendar families read their own keys and
    are left to :func:`event_filter_reason` whole.
    """
    if (
        event in MAIL_EVENTS
        or event in TEAMS_EVENTS
        or event in DOCUSIGN_EVENTS
        or event in ("calendar_event_changed", TODO_EVENT)
    ):
        return None
    if not isinstance(filter_config, Mapping) or not isinstance(payload, Mapping) or not needs_enrichment(filter_config):
        return None
    plain = {key: value for key, value in filter_config.items() if not needs_enrichment({key: value})}
    if not plain:
        return None
    return payload_filter_reason(payload, plain)


def _operand_error(key: str, op: str, operand: Any) -> str | None:
    where = f"filter key '{key}', operator '{op}'"
    if op == "exists":
        return None if isinstance(operand, bool) else f"{where}: takes true or false."
    if op in ("in", "not_in"):
        return None if isinstance(operand, list) else f"{where}: takes a list of values."
    if op in ("eq", "ne"):
        return None if not isinstance(operand, Mapping) else f"{where}: takes a value, not an object."
    options = _options(operand)
    if op in ("starts_with", "ends_with", "matches") and (
        not options or not all(isinstance(o, str) and o for o in options)
    ):
        return f"{where}: takes a text or a list of texts."
    if op == "contains" and (not options or any(isinstance(o, Mapping | list) for o in options)):
        return f"{where}: takes a value or a list of values."
    if op == "matches":
        for pattern in options:
            if len(pattern) > MAX_PATTERN_CHARS:
                return f"{where}: a pattern holds at most {MAX_PATTERN_CHARS} characters."
            if _NESTED_QUANTIFIER.search(_without_character_classes(pattern)):
                return (
                    f"{where}: a repeated group holding a repetition or an alternation "
                    "('(a+)+', '(a|aa)+') is refused."
                )
            try:
                re.compile(pattern)
            except re.error as exc:
                return f"{where}: not a regular expression ({exc})."
            # Saved only when it is evaluated the way it is written: anchored
            # to the whole value (a global flag anywhere but the start is not).
            if _anchored(pattern) is None:
                return f"{where}: put inline flags such as (?i) at the very start of the pattern."
    return None


def _normalized(name: str) -> str:
    return name.replace("_", "").replace("-", "").casefold()


#: An operator as it is often misspelled (``notIn``, ``starts-with``, ``NE``):
#: case, ``_`` and ``-`` aside. Refused rather than read as a payload field.
_OPERATOR_SPELLINGS = {_normalized(op): op for op in FILTER_OPERATORS}
_COMBINATOR_SPELLINGS = {_normalized(op): op for op in FILTER_COMBINATORS}


def _misspelled(name: str, spellings: Mapping[str, str]) -> str | None:
    """The operator *name* misspells, ``None`` when it is one or none."""
    found = spellings.get(_normalized(name))
    return found if found is not None and found != name else None


def _value_error(key: str, value: Any, depth: int) -> str | None:
    if not isinstance(value, Mapping) or depth > MAX_FILTER_DEPTH:
        return None
    operators = [k for k in value if k in FILTER_OPERATORS]
    if operators and len(operators) != len(value):
        unknown = next(k for k in value if k not in FILTER_OPERATORS)
        return (
            f"filter key '{key}': unknown operator '{unknown}' next to '{operators[0]}'. "
            f"Operators: {', '.join(sorted(FILTER_OPERATORS))}."
        )
    if operators:
        for op, operand in value.items():
            if (error := _operand_error(key, op, operand)) is not None:
                return error
        return None
    for sub_key, sub_value in value.items():
        if isinstance(sub_key, str) and (close := _misspelled(sub_key, _OPERATOR_SPELLINGS)) is not None:
            return f"filter key '{key}': unknown operator '{sub_key}' (did you mean '{close}'?)."
        if (error := _value_error(f"{key}.{sub_key}", sub_value, depth + 1)) is not None:
            return error
    return None


def _misshapen_combinator(key: str, value: Any) -> bool:
    """A combinator written with objects in the wrong shape: ``{"not": [{...}]}``,
    ``{"any": []}``, ``{"all": [{...}, "x"]}``. A key ``all`` / ``any`` / ``not``
    holding values only keeps its meaning of a payload field."""
    if key not in FILTER_COMBINATORS or not isinstance(value, list):
        return False
    if not value:
        return key != "not"
    return any(isinstance(v, Mapping) for v in value)


def _without_character_classes(pattern: str) -> str:
    """*pattern* with each unescaped character class (``[|+]``) replaced by ``x``.

    Its metacharacters are literals, left out of the nested-quantifier scan.
    An escaped bracket (``\\[``) opens nothing; a ``]`` right after ``[`` or
    ``[^`` is a literal of the class.
    """
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        char = pattern[i]
        if char == "\\":
            out.append(pattern[i : i + 2])
            i += 2
            continue
        if char != "[":
            out.append(char)
            i += 1
            continue
        j = i + 1
        if j < n and pattern[j] == "^":
            j += 1
        if j < n and pattern[j] == "]":
            j += 1
        while j < n and pattern[j] != "]":
            j += 2 if pattern[j] == "\\" else 1
        if j >= n:
            # Unclosed: left as it is (the compile check refuses it).
            out.append(pattern[i:])
            break
        out.append("x")
        i = j + 1
    return "".join(out)


def _value_pattern_count(value: Any, depth: int) -> int:
    """The ``matches`` patterns of one key's expected value (operator objects only)."""
    if depth > MAX_FILTER_DEPTH * 2:
        return 0
    if _is_operator_object(value):
        return len(_options(value["matches"])) if "matches" in value else 0
    if isinstance(value, Mapping):
        return sum(_value_pattern_count(sub, depth + 1) for sub in value.values())
    if isinstance(value, list):
        return sum(_value_pattern_count(v, depth + 1) for v in value if isinstance(v, Mapping))
    return 0


def _pattern_count(filter_config: Any, depth: int = 0) -> int:
    """How many ``matches`` patterns a filter holds, combinators walked.

    A filter's own keys are payload fields: a field named ``matches`` holding
    values (legacy equality / any of) is not a pattern.
    """
    if not isinstance(filter_config, Mapping) or depth > MAX_FILTER_DEPTH:
        return 0
    count = 0
    for key, value in filter_config.items():
        if _is_combinator(key, value):
            branches = value if isinstance(value, list) else [value]
            count += sum(_pattern_count(branch, depth + 1) for branch in branches)
        else:
            count += _value_pattern_count(value, depth)
    return count


def filter_expression_error(filter_config: Any, depth: int = 0) -> str | None:
    """The refusal of a ``trigger_config.filter`` the payload filter cannot evaluate, else ``None``.

    Checked at save time (``create_assignment`` / ``update_assignment`` and
    the REST API): an operator object mixing an unknown key with operators, an
    operand of the wrong shape, a regular expression that does not compile or may backtrack
    without end, a combinator of the wrong shape, nesting past
    ``MAX_FILTER_DEPTH``, an operator misspelled (``notIn``, ``starts-with``,
    ``Any``: ``_misspelled``). The error names the key. A filter saved before the
    operators existed passes unchanged: none of them holds such a shape.
    """
    if filter_config is None:
        return None
    if not isinstance(filter_config, Mapping):
        return "Invalid trigger_config: 'filter' must be a JSON object."
    if depth == 0 and _pattern_count(filter_config) > MAX_PATTERNS_PER_FILTER:
        return (
            f"Invalid trigger_config.filter: at most {MAX_PATTERNS_PER_FILTER} 'matches' patterns "
            "per filter (use starts_with, ends_with or in instead)."
        )
    if depth > MAX_FILTER_DEPTH:
        return f"Invalid trigger_config.filter: combinators nest at most {MAX_FILTER_DEPTH} deep."
    for key, value in filter_config.items():
        if not isinstance(key, str):
            continue
        if (close := _misspelled(key, _COMBINATOR_SPELLINGS)) is not None:
            return f"Invalid trigger_config.filter: unknown combinator '{key}' (did you mean '{close}'?)."
        if _is_combinator(key, value):
            for branch in value if isinstance(value, list) else [value]:
                if (error := filter_expression_error(branch, depth + 1)) is not None:
                    return error
            continue
        if _misshapen_combinator(key, value):
            shape = "an object" if key == "not" else "a non-empty list of objects"
            return f"Invalid trigger_config.filter: '{key}' takes {shape}."
        if (error := _value_error(key, value, depth)) is not None:
            return f"Invalid trigger_config.filter: {error}"
    return None


#: The trigger types whose ``filter`` is this payload filter: the integration
#: events, and the platform events (``platform_events.mismatch_reason``).
_FILTERED_TRIGGER_TYPES = frozenset({"integration_event", "platform_event"})


def trigger_filter_error(trigger_type: object, trigger_config: object) -> str | None:
    """The refusal for a trigger's ``filter`` as it will be saved, else ``None``.

    *trigger_type* ``None`` is a partial update that keeps the stored type: its
    filter is checked too (a filter of another type is never read, and a valid
    one passes).
    """
    if trigger_type is not None and trigger_type not in _FILTERED_TRIGGER_TYPES:
        return None
    if not isinstance(trigger_config, Mapping):
        return None
    # The dispatch dedup key of an event trigger (#10622), checked with its filter.
    from app.services.event_run_dedup import dedup_config_error

    return filter_expression_error(trigger_config.get("filter")) or dedup_config_error(trigger_config)


def event_filter_reason(
    event: str,
    payload: Mapping[str, Any],
    filter_config: Mapping[str, Any] | None,
    *,
    mailbox: Iterable[str] = (),
) -> str | None:
    """Why the trigger's filter drops *payload*, ``None`` when it creates a run.

    *mailbox* holds the mailbox owner's addresses, read only when
    ``needs_mailbox``; without them ``to_me`` / ``cc_me`` pass every mail
    rather than dropping every one.
    """
    if not filter_config:
        return None
    if event in MAIL_EVENTS:
        reason = _mail_reason(payload, filter_config, {a.lower() for a in mailbox if a})
        if reason is not None:
            return reason
        statuses = {s.casefold() for s in _strings(filter_config.get("flag_status"))}
        if event == "email_flag_changed" and statuses and str(payload.get("flag_status") or "").casefold() not in statuses:
            return "flag status is not in flag_status"
        return None
    if event in TEAMS_EVENTS:
        return _teams_reason(event, payload, filter_config)
    if event in DOCUSIGN_EVENTS:
        return _docusign_reason(event, payload, filter_config)
    if payload.get("provider") == "dealhub":
        from app.services.dealhub_events import EVENTS as DEALHUB_EVENTS
        from app.services.dealhub_events import filter_reason as dealhub_filter_reason

        if event in DEALHUB_EVENTS:
            return dealhub_filter_reason(payload, filter_config)
    if event in JIRA_EVENTS and payload.get("provider") == "jira":
        from app.services.jira_events import filter_reason as jira_filter_reason

        signals = payload.get(FILTER_SIGNALS_KEY)
        signals = signals if isinstance(signals, Mapping) else {}
        return jira_filter_reason(
            payload, filter_config, self_account_id=str(signals.get("jira_self_account_id") or "")
        )
    if payload.get("provider") == "odoo":
        from app.integrations.providers.odoo.events import EVENTS as ODOO_EVENTS
        from app.integrations.providers.odoo.events import filter_reason as odoo_filter_reason

        if event in ODOO_EVENTS:
            return odoo_filter_reason(payload, filter_config)
    if payload.get("provider") == "pennylane":
        from app.services.pennylane_events import EVENTS as PENNYLANE_EVENTS
        from app.services.pennylane_events import filter_reason as pennylane_filter_reason

        if event in PENNYLANE_EVENTS:
            return pennylane_filter_reason(payload, filter_config)
    if payload.get("provider") == "netsuite":
        from app.services.netsuite_events import EVENTS as NETSUITE_EVENTS
        from app.services.netsuite_events import filter_reason as netsuite_filter_reason

        if event in NETSUITE_EVENTS:
            return netsuite_filter_reason(payload, filter_config)
    if event == "calendar_event_changed":
        kinds = {k.casefold() for k in _strings(filter_config.get("change_kinds"))}
        if kinds and str(payload.get("change_kind") or "").casefold() not in kinds:
            return "change kind is not in change_kinds"
        return None
    if event == TODO_EVENT:
        return _todo_reason(payload, filter_config)
    return payload_filter_reason(payload, filter_config)


def without_filter_signals(payload: dict) -> dict:
    """*payload* without what only the filter reads (in place, returned)."""
    payload.pop(FILTER_SIGNALS_KEY, None)
    return payload


#: Why each assignment's events were dropped: a Redis hash per assignment,
#: reason -> count, shown by ``get_assignment``. Reasons name filter
#: keys and operators, never a payload value.
FILTER_REASONS_KEY_PREFIX = "integration_event:filtered_reasons:"
FILTER_REASONS_TTL_SECONDS = 7 * 86400
#: The distinct reasons kept per assignment; past it, one ``other`` bucket.
MAX_FILTER_REASONS = 50
MAX_REASON_CHARS = 160
_REDIS_TIMEOUT_SECONDS = 0.5


def reason_label(reason: str) -> str:
    """*reason* as it is counted: one line, bounded."""
    text = " ".join(str(reason).split())
    return text if len(text) <= MAX_REASON_CHARS else text[: MAX_REASON_CHARS - 3] + "..."


async def record_filter_reasons(assignment_id: Any, reasons: Mapping[str, int]) -> None:
    """Add *reasons* (reason -> count) to the assignment's dropped-event reasons (best effort)."""
    if not reasons:
        return
    import asyncio

    from app.core.redis import redis_client

    key = f"{FILTER_REASONS_KEY_PREFIX}{assignment_id}"
    try:
        redis = await asyncio.wait_for(redis_client(), _REDIS_TIMEOUT_SECONDS)
        known = set(await asyncio.wait_for(redis.hkeys(key), _REDIS_TIMEOUT_SECONDS))
        for reason, count in reasons.items():
            label = reason_label(reason)
            if label not in known and len(known) >= MAX_FILTER_REASONS:
                label = "other"
            known.add(label)
            await asyncio.wait_for(redis.hincrby(key, label, int(count)), _REDIS_TIMEOUT_SECONDS)
        await asyncio.wait_for(redis.expire(key, FILTER_REASONS_TTL_SECONDS), _REDIS_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.debug("Could not record the filter reasons of assignment %s: %r", assignment_id, exc)


async def filtered_reasons(assignment_id: Any) -> dict[str, int]:
    """The assignment's dropped-event reasons over the last week, most frequent first."""
    import asyncio

    from app.core.redis import redis_client

    try:
        redis = await asyncio.wait_for(redis_client(), _REDIS_TIMEOUT_SECONDS)
        raw = await asyncio.wait_for(
            redis.hgetall(f"{FILTER_REASONS_KEY_PREFIX}{assignment_id}"), _REDIS_TIMEOUT_SECONDS
        )
    except Exception as exc:
        logger.debug("Could not read the filter reasons of assignment %s: %r", assignment_id, exc)
        return {}
    counts: dict[str, int] = {}
    for reason, value in (raw or {}).items():
        try:
            counts[str(reason)] = int(value)
        except (TypeError, ValueError):
            continue
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


async def record_filtered_events(assignment_id: Any, count: int) -> None:
    """Add *count* to the assignment's filtered-events counter (best effort)."""
    if count <= 0:
        return
    from sqlalchemy import func, update

    from app.core.database import async_session
    from app.models.assignment import Assignment

    try:
        async with async_session() as db:
            await db.execute(
                update(Assignment)
                .where(Assignment.id == assignment_id)
                .values(
                    events_filtered_count=func.coalesce(Assignment.events_filtered_count, 0) + count,
                    # A counted event is not an edit of the assignment.
                    updated_at=Assignment.updated_at,
                )
            )
            await db.commit()
    except Exception as exc:
        logger.warning("Could not count %d filtered event(s) of assignment %s: %r", count, assignment_id, exc)
