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

"""The structured ask of a work item waiting for a person (#9437).

An item with ``needs_human`` says in ``blocked_reason`` what it waits for, in
free text; the views had to show that text, or the whole description, and the
person had to find the question in it. ``ask`` carries the decision itself, so
a view renders it as a card with one button per option:

``{"question": str, "context"?: str, "options": [{"key", "label", "effect"?}],
"recommendation"?: option key, "deadline"?: ISO-8601}``

* ``question``: one sentence, required.
* ``context``: 2 to 4 short plain lines for the reader (what the matter is,
  what was found, what each answer leads to) (#776).
* ``options``: up to :data:`MAX_OPTIONS`; ``key`` short and unique (``A``,
  ``merge``), ``label`` the button text, ``effect`` what choosing it does.
  None at all: the person answers in free text.
* ``recommendation``: the key of the option the agent recommends.
* ``deadline``: when the answer is needed, normalized to ISO-8601 UTC.

The question, the context and the option texts are read by a person: they
are refused when they carry what only an agent reads (a timestamp, an id, a
message id, a correlation or timer key, "card sent"), with a message saying
how to rewrite them (:func:`unreadable_reasons`, #776). Those details belong
in ``blocked_reason``, which the card shows under "Technical details".

:func:`normalize_ask` validates and trims a written ask (``ValueError`` with
the reason); ``{}`` or ``null`` clears it. Stored in ``work_items.ask``
(JSON, SQL NULL when none, migration ``a9437wiask01``). A legacy item has no
ask: the views show its ``blocked_reason`` as the question.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

MAX_QUESTION_CHARS = 500
MAX_CONTEXT_CHARS = 800
MAX_OPTIONS = 6
MAX_KEY_CHARS = 40
MAX_LABEL_CHARS = 120
MAX_EFFECT_CHARS = 400

ASK_FIELDS_MESSAGE = (
    "ask is {question, context?, options?: [{key, label, effect?}], recommendation?: an option key, "
    "deadline?: ISO-8601}"
)

# What a person never reads on a decision card (#776): each pattern with what
# to write instead. Checked on the question, the context and every option text.
_UNREADABLE: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?\b|\b\d{1,2}:\d{2}Z\b"
        ),
        "a machine timestamp: put the time in ask.deadline, the card shows it in the reader's time zone",
    ),
    (
        re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE),
        "a UUID: name the thing in words (its title), ids go in blocked_reason",
    ),
    (
        re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b", re.IGNORECASE),
        "a hexadecimal id or SHA: name the thing in words, ids go in blocked_reason",
    ),
    (
        re.compile(r"\b\d{10,}\b"),
        "a message or object id (a long run of digits): say what it is in words, ids go in blocked_reason",
    ),
    (
        re.compile(r"\b[a-z][a-z0-9_]*:[a-z0-9][a-z0-9_.]*-[a-z0-9][a-z0-9_.-]*", re.IGNORECASE),
        "a correlation or timer key: describe the matter in plain words, keys go in blocked_reason",
    ),
    (
        re.compile(r"\bcard sent\b|\bmsg\b", re.IGNORECASE),
        "delivery bookkeeping ('card sent', 'msg'): the reader needs the decision, not how it reached them",
    ),
)

READABLE_ASK_EXAMPLE = (
    'good: {"question": "Post Legal\'s comment asking the supplier for the 4 missing documents?", '
    '"context": "The contract review found 4 documents missing. Go posts the prepared comment; '
    'Edit lets you change it first; No closes the review as is."}; bad: {"question": "CARD SENT '
    '2026-11-20T11:43:58Z (msg 1795175038574): waiting for GO / EDIT / NO, timer legal:fin-796-reminder"}'
)


def unreadable_reasons(text: str) -> list[str]:
    """What in *text* a person should not have to read, one reason per pattern found."""
    return [reason for pattern, reason in _UNREADABLE if pattern.search(text or "")]


def _refuse_unreadable(field: str, text: str) -> None:
    reasons = unreadable_reasons(text)
    if reasons:
        raise ValueError(
            f"ask.{field} is read by a person and holds {'; '.join(reasons)}. Rewrite it as the decision in "
            f"the reader's terms; technical details go in blocked_reason ({READABLE_ASK_EXAMPLE})"
        )


def _text(value: object, limit: int) -> str:
    return " ".join(str(value).split())[:limit] if value is not None else ""


def _deadline(value: object) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"ask.deadline is not an ISO-8601 date or time: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _context(value: object) -> str:
    """The context lines, each trimmed, blank lines dropped, line breaks kept."""
    if value in (None, ""):
        return ""
    if isinstance(value, list):
        value = "\n".join(str(line) for line in value)
    lines = [" ".join(line.split()) for line in str(value).splitlines()]
    return "\n".join(line for line in lines if line)[:MAX_CONTEXT_CHARS]


def _options(value: object) -> list[dict[str, str]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("ask.options must be a list of {key, label, effect?}")
    if len(value) > MAX_OPTIONS:
        raise ValueError(f"ask.options lists at most {MAX_OPTIONS} options ({len(value)} given)")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("ask.options must be a list of {key, label, effect?}")
        key = _text(raw.get("key"), MAX_KEY_CHARS)
        label = _text(raw.get("label"), MAX_LABEL_CHARS) or key
        if not key:
            raise ValueError("every ask option needs a key")
        if key.casefold() in seen:
            raise ValueError(f"ask option key {key!r} is given twice")
        seen.add(key.casefold())
        option = {"key": key, "label": label}
        effect = _text(raw.get("effect"), MAX_EFFECT_CHARS)
        if effect:
            option["effect"] = effect
        out.append(option)
    return out


def normalize_ask(value: object) -> dict[str, Any] | None:
    """The ask as stored, ``None`` when cleared (``{}``, ``null``, blank text).

    Accepts the object or its JSON text; raises ``ValueError`` naming what is
    wrong. A recommendation that names no option is refused.
    """
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ask is not valid JSON ({exc})") from exc
    if not isinstance(value, dict):
        raise ValueError(ASK_FIELDS_MESSAGE)
    if not value:
        return None
    unknown = sorted(set(value) - {"question", "context", "options", "recommendation", "deadline"})
    if unknown:
        raise ValueError(f"ask has unknown field(s) {', '.join(unknown)}: {ASK_FIELDS_MESSAGE}")
    question = _text(value.get("question"), MAX_QUESTION_CHARS)
    if not question:
        raise ValueError("ask.question is required: the decision in one sentence")
    _refuse_unreadable("question", question)
    ask: dict[str, Any] = {"question": question}
    context = _context(value.get("context"))
    if context:
        _refuse_unreadable("context", context)
        ask["context"] = context
    options = _options(value.get("options"))
    for option in options:
        _refuse_unreadable("options", f"{option['label']} {option.get('effect', '')}")
    if options:
        ask["options"] = options
    recommendation = _text(value.get("recommendation"), MAX_KEY_CHARS)
    if recommendation:
        match = next((o["key"] for o in options if o["key"].casefold() == recommendation.casefold()), None)
        if match is None:
            raise ValueError(f"ask.recommendation {recommendation!r} is not the key of one of its options")
        ask["recommendation"] = match
    deadline = _deadline(value.get("deadline"))
    if deadline:
        ask["deadline"] = deadline
    return ask


def ask_lines(ask: object) -> list[str]:
    """What a tool result says of an item's ask (nothing when it has none)."""
    if not isinstance(ask, dict) or not ask.get("question"):
        return []
    lines = [f"  - ask: {ask['question']}"]
    if ask.get("context"):
        lines.append(f"    context: {' / '.join(str(ask['context']).splitlines())}")
    recommended = ask.get("recommendation")
    for option in ask.get("options") or []:
        mark = " (recommended)" if option.get("key") == recommended else ""
        effect = f": {option['effect']}" if option.get("effect") else ""
        lines.append(f"    - [{option.get('key')}] {option.get('label')}{mark}{effect}")
    if ask.get("deadline"):
        lines.append(f"    - answer needed by {ask['deadline']}")
    return lines
