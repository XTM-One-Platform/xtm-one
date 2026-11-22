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

"""The WhatsApp decision message of a human gate, answered in place (#842, S12; human-gates.md I.1 to I.4).

WhatsApp has no card: a decision is an interactive message of the Cloud API,
sent in a 1:1 chat only (interactive messages are not offered in groups):

* up to three options are reply buttons, four to ten a list;
* the question, the context, the recommendation and the deadline are the
  message body; the link to the decision in XTM One is its last line, for a
  text answer or a decision the buttons cannot carry (more than ten options,
  a tool approval listing more calls than a card shows).

Each button's reply id is ``xtm.d:<decisions row id>:<option key>``: the
webhook maps a reply to the decision by that payload, never by the reply
text, and a re-asked decision (a new row) leaves the old buttons answering
nothing. An answer goes through
:func:`app.services.decision_router.answer_decision` as the person whose
linked WhatsApp identity sent the reply, so the recipient rule, the row lock
and the audit are the router's; the surface recorded is ``whatsapp_card``.
WhatsApp cannot edit a sent interactive message: the decided state is sent
back as a reply in the same chat.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.decision import Decision
from app.services.channel_notices import notice

logger = logging.getLogger(__name__)

SURFACE = "whatsapp_card"
REPLY_PREFIX = "xtm.d:"

#: Cloud API limits of an interactive message.
MAX_BUTTONS = 3
MAX_ROWS = 10
_MAX_BODY = 1024
_MAX_BUTTON_TITLE = 20
_MAX_ROW_TITLE = 24
_MAX_ROW_DESCRIPTION = 72
_MAX_LIST_BUTTON = 20
_MAX_FOOTER = 60
_MAX_REPLY_ID = 256


def _clip(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def reply_id(row_id: uuid.UUID | str, key: str) -> str:
    """The reply id of one option's button or list row."""
    return f"{REPLY_PREFIX}{row_id}:{key}"[:_MAX_REPLY_ID]


def parse_reply_id(raw: object) -> tuple[uuid.UUID, str] | None:
    """The decisions row and option a reply id names, or ``None`` for any other reply."""
    value = str(raw or "")
    if not value.startswith(REPLY_PREFIX):
        return None
    row, sep, key = value[len(REPLY_PREFIX) :].partition(":")
    if not sep or not key:
        return None
    try:
        return uuid.UUID(row), key
    except ValueError:
        return None


def _options(ask: dict[str, Any]) -> list[dict[str, Any]]:
    return [o for o in ask.get("options") or [] if str(o.get("key") or "")]


def _outcome(view: Any, ask: dict[str, Any], language: str) -> list[str]:
    """The decided state of a closed decision, said as the Teams and Slack decided cards say it (UX round 1, B1).

    Who chose which option (or the tool verdicts), the answer's text, or that
    the decision expired or is no longer needed: never an invitation to answer.
    """
    from app.services.decision_cards import _approval_verdicts, _option_label

    row = view.decision
    if row.status != "answered":
        key = "card_decision_expired" if row.status == "expired" else "card_decision_cancelled"
        return [notice(key, language)]
    answer = row.answer if isinstance(getattr(row, "answer", None), dict) else {}
    who = _clip(view.answered_by_name or notice("card_someone", language), 60)
    verdicts = _approval_verdicts(answer, ask, row.kind, language, who)
    if verdicts is not None:
        lines = ["".join(str(run.get("text") or "") for run in verdicts.get("inlines") or [])]
    else:
        label = answer.get("label")
        chosen = _clip(label, 60) if label else _option_label(ask, answer.get("option"), row.kind, language)
        lines = [
            notice("card_decided_option", language, name=who, option=chosen)
            if chosen
            else notice("card_answered_by", language, name=who)
        ]
    if answer.get("text"):
        lines.append(_clip(answer.get("text"), 400))
    return lines


def _body(view: Any, ask: dict[str, Any], language: str, *, answerable: bool) -> str:
    from app.services.decision_cards import _label, _page_base, _utc

    kind = view.decision.kind
    closed = view.decision.status != "open"
    lines = [_clip(ask.get("question"), 400)]
    context = str(ask.get("context") or "").strip()
    if context:
        lines.append(_clip(context, 400))
    if closed:
        lines.extend(_outcome(view, ask, language))
    recommendation = str(ask.get("recommendation") or "")
    for option in [] if closed else _options(ask):
        if kind == "tool_approval" and not option.get("effect"):
            continue
        line = f"- {_label(option, kind, language, 60)}"
        if recommendation and str(option.get("key")) == recommendation:
            line += f" ({notice('card_recommended', language)})"
        if option.get("effect"):
            line += f": {_clip(option['effect'], 120)}"
        lines.append(line)
    deadline = None if closed else _utc(ask.get("deadline"))
    if deadline:
        lines.append(f"{notice('card_label_deadline', language)}: {deadline.replace('T', ' ').rstrip('Z')} UTC")
    if view.page_path:
        prefix = "" if answerable or closed else f"{notice('card_decide_in_xtm', language)} "
        lines.append(f"{prefix}{notice('card_open', language)}: {_page_base()}{view.page_path}")
    text = "\n".join(line for line in lines if line)
    if len(text) <= _MAX_BODY:
        return text
    # The link line is what a reader needs to answer otherwise: keep it whole.
    tail = lines[-1] if view.page_path else ""
    head = "\n".join(lines[:-1] if tail else lines)
    room = _MAX_BODY - len(tail) - 2
    return f"{head[: max(room, 0)].rstrip()}…\n{tail}" if tail else text[: _MAX_BODY - 1] + "…"


def render_whatsapp_decision(view: Any, language: str) -> dict[str, Any] | None:
    """The ``interactive`` object of *view*'s open decision, or ``None`` when buttons cannot carry it.

    ``None`` means the caller sends :func:`decision_text` instead (an answered
    decision, more than ten options, a free-text question, a tool approval
    with more calls than a card shows).
    """
    from app.services.decision_cards import _decides_unshown_calls, _label

    row = view.decision
    ask = row.ask if isinstance(row.ask, dict) else {}
    options = _options(ask)
    if row.status != "open" or not options or len(options) > MAX_ROWS or _decides_unshown_calls(view):
        return None
    kind = row.kind
    body = {"text": _body(view, ask, language, answerable=True)}
    footer = {"text": _clip(notice("card_header_decision", language), _MAX_FOOTER)}
    if len(options) <= MAX_BUTTONS:
        return {
            "type": "button",
            "body": body,
            "footer": footer,
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": reply_id(row.id, str(o["key"])),
                            "title": _label(o, kind, language, _MAX_BUTTON_TITLE),
                        },
                    }
                    for o in options
                ]
            },
        }
    rows = []
    for option in options:
        entry: dict[str, Any] = {
            "id": reply_id(row.id, str(option["key"])),
            "title": _label(option, kind, language, _MAX_ROW_TITLE),
        }
        if option.get("effect"):
            entry["description"] = _clip(option["effect"], _MAX_ROW_DESCRIPTION)
        rows.append(entry)
    return {
        "type": "list",
        "body": body,
        "footer": footer,
        "action": {
            "button": _clip(notice("card_choose", language), _MAX_LIST_BUTTON),
            "sections": [{"title": _clip(notice("card_header_decision", language), _MAX_ROW_TITLE), "rows": rows}],
        },
    }


def decision_text(view: Any, language: str) -> str:
    """The plain text of a decision the buttons cannot carry, with the link to it in XTM One.

    An open decision invites the reader to answer there; a closed one states
    its outcome instead (who chose what, expired, no longer needed).
    """
    row = view.decision
    ask = row.ask if isinstance(row.ask, dict) else {}
    return _body(view, ask, language, answerable=False)


async def decision_message_for_tool(raw_id: str) -> dict[str, Any] | str:
    """The Cloud API message fields of the decision *raw_id* names, for the WhatsApp send tool.

    The answer is ``{"type": "interactive", "interactive": ...}`` or
    ``{"type": "text", "text": ...}``, or the error text the tool returns when
    the id names no decision. Agents never write the buttons: they pass the
    decision id (human-gates.md I.2).
    """
    from app.core.database import async_session
    from app.services.channel_notices import notice_language
    from app.services.decision_cards import load_view
    from app.services.decision_router import resolve

    language = notice_language()
    try:
        async with async_session() as db:
            _ref, row = await resolve(db, raw_id)
            if row is None:
                return f"Error: no decision found for {raw_id!r}."
            view = await load_view(db, row, None, language)
    except HTTPException:
        return f"Error: no decision found for {raw_id!r}; pass its source and id, e.g. run: then the run's UUID."
    interactive = render_whatsapp_decision(view, language)
    if interactive is None:
        return {"type": "text", "text": {"body": decision_text(view, language)}}
    return {"type": "interactive", "interactive": interactive}


async def answer_reply(db: AsyncSession, channel_user_id: str, raw_reply_id: str, language: str) -> str:
    """Answer the decision a button reply names, as the sender's linked account; the reply text to send back."""
    from app.channels.router import resolve_user_by_channel_identity
    from app.services.decision_cards import _option_label, _refusal_key, load_view
    from app.services.decision_router import answer_decision

    parsed = parse_reply_id(raw_reply_id)
    if parsed is None:
        return notice("card_decision_failed", language)
    row_id, key = parsed
    row = await db.get(Decision, row_id)
    if row is None:
        return notice("card_changed", language)
    user = await resolve_user_by_channel_identity(db, "whatsapp", channel_user_id)
    if user is None:
        return notice("card_not_linked_decision", language)
    if row.status != "open":
        return notice("card_decision_already_answered", language)
    ask = row.ask if isinstance(row.ask, dict) else {}
    if not any(str(o.get("key", "")) == key for o in _options(ask)):
        return notice("card_changed", language)
    public = row.public_id
    try:
        await answer_decision(db, public, user, option=key, surface=SURFACE)
    except HTTPException as exc:
        logger.info("WhatsApp decision reply on %s refused: %s", public, exc.status_code)
        await db.rollback()
        return notice(_refusal_key(exc), language)
    await db.commit()
    fresh = await db.get(Decision, row_id)
    view = await load_view(db, fresh or row, user, language)
    who = view.answered_by_name or getattr(user, "full_name", None) or notice("card_someone", language)
    label = _option_label(ask, key, row.kind, language) or key
    return notice("card_decided_option", language, name=who, option=label)
