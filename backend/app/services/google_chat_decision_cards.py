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

"""The Google Chat decision card of every human gate (#842, S11, human-gates.md I.4 path (d)).

App auth (the Chat app is in the space): one ``cardsV2`` message per
decision, rendered from the ask contract the ``decisions`` row holds (run
HITL, tool approval, flow Ask-user, work item): a header, the source, the
question and its context, the options, one button per option (the
recommendation filled), a text input with its "Send" button, and the links.
A click comes back as a ``CARD_CLICKED`` event on
``/api/channels/googlechat/webhook``, whose bearer JWT ``channels/google_chat.py``
verifies (and refuses a click when no project number is configured), and is
answered by ``decision_router.answer_decision`` as the person who clicked
(surface ``google_chat_card``): the recipient rule, the row lock and the
audit are the router's. The message is the same for every member of the
space: a click from anyone but the recipient answers a private message to
the clicker and leaves the card as it is (I.5); an answer replaces the
buttons with the decided state (``UPDATE_MESSAGE``).

User auth (a message posted as a person through the Google integration):
Chat renders no interactive card for a user, so the message carries the
signed answer links of S9b (:func:`link_message`, surface ``google_chat``).

The view (``DecisionView``) and its loading are the Teams card's
(``decision_cards``); only the drawing differs.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.models.decision import Decision
from app.services.channel_notices import notice
from app.services.decision_cards import (
    _MAX_CONTEXT,
    _MAX_EFFECT,
    _MAX_NAME,
    _MAX_QUESTION,
    _MAX_TEXT,
    _SOURCE_NOTICE,
    DecisionView,
    _clip,
    _decides_unshown_calls,
    _label,
    _option_label,
    _page_base,
    _row_for,
    load_view,
)

logger = logging.getLogger(__name__)

CHAT_API_BASE = "https://chat.googleapis.com/v1"

#: ``actionMethodName`` of an option button; its ``option`` parameter names the option.
ACTION_ANSWER = "xtm.decision.answer"
#: ``actionMethodName`` of the "Send" button under the text input.
ACTION_TEXT = "xtm.decision.text"
#: ``name`` of the text input; a ``CARD_CLICKED`` event carries it in ``common.formInputs``.
TEXT_INPUT = "xtm_decision_text"
SURFACE = "google_chat_card"
#: The surface the answer links of a message posted as a user record (S9b).
LINK_SURFACE = "google_chat"
CARD_ID = "xtm-decision"

#: Google Chat's limits: 6 buttons read well in one button list, 25 characters
#: fit a button on a phone, 4,000 characters per text paragraph.
_MAX_BUTTONS = 6
_MAX_BUTTON = 25
_MAX_PARAGRAPH = 4000
_MAX_LINKS = 3
#: The recommended option's button colour (Material blue 700), the card's primary.
_PRIMARY = {"red": 0.098, "green": 0.463, "blue": 0.824, "alpha": 1}


def _escape(text: str) -> str:
    """*text* safe inside a Chat text paragraph (a subset of HTML): ``&``, ``<`` and ``>`` escaped."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _bold(text: str) -> str:
    return f"<b>{_escape(text)}</b>"


def _paragraph(html: str) -> dict[str, Any]:
    return {"textParagraph": {"text": html[:_MAX_PARAGRAPH] or " "}}


def _runs(template: str, **values: str) -> str:
    """A catalog sentence with its ``{name}`` values in bold, each escaped; a value is never read as a slot."""
    out: list[str] = []
    for index, piece in enumerate(re.split(r"(\{[a-z_]+\})", template)):
        key = piece[1:-1] if index % 2 else None
        out.append(_bold(values[key]) if key is not None and key in values else _escape(piece))
    return "".join(out)


def _utc(value: str | None) -> str | None:
    """An ISO time as ``YYYY-MM-DD HH:MM UTC`` (Chat has no per-viewer date token)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _parameters(view: DecisionView, option: str | None = None) -> list[dict[str, str]]:
    """What a button carries: the decision id, the row it showed and the option (I.3)."""
    row = view.decision
    params = [{"key": "id", "value": row.public_id}, {"key": "d", "value": str(row.id)}]
    if option is not None:
        params.append({"key": "option", "value": option})
    return params


def _header(view: DecisionView, language: str) -> dict[str, Any]:
    source = notice(_SOURCE_NOTICE.get(view.decision.kind, "card_source_run"), language)
    if view.agent_name:
        subtitle = notice("card_from_by", language).replace("{source}", source)
        subtitle = subtitle.replace("{agent}", _clip(view.agent_name, _MAX_NAME))
    else:
        subtitle = notice("card_from", language).replace("{source}", source)
    # A header is plain text: Chat draws its own weight, no markup is read there.
    return {"title": notice("card_header_decision", language), "subtitle": _clip(subtitle, 200)}


def _ask_widgets(ask: dict[str, Any], kind: str, language: str, *, show_effects: bool) -> list[dict[str, Any]]:
    widgets: list[dict[str, Any]] = [_paragraph(_bold(_clip(ask.get("question"), _MAX_QUESTION)))]
    lines = [_clip(line, _MAX_CONTEXT) for line in str(ask.get("context") or "").strip().splitlines()[:20]]
    context = "<br>".join(_escape(line) for line in lines if line)
    if context:
        widgets.append(_paragraph(context))
    recommendation = str(ask.get("recommendation") or "")
    options: list[str] = []
    for option in ask.get("options") or []:
        if kind == "tool_approval" and not option.get("effect"):
            continue
        line = _bold(_label(option, kind, language))
        if recommendation and str(option.get("key")) == recommendation:
            line += f" ({_escape(notice('card_recommended', language))})"
        if show_effects and option.get("effect"):
            line += f": {_escape(_clip(option['effect'], _MAX_EFFECT))}"
        options.append(f"• {line}")
    if options:
        widgets.append(_paragraph("<br>".join(options)))
    deadline = _utc(ask.get("deadline"))
    if deadline:
        widgets.append(
            {
                "decoratedText": {
                    "topLabel": notice("card_label_deadline", language),
                    "text": _escape(deadline),
                }
            }
        )
    return widgets


def _proposal_widgets(view: DecisionView, language: str) -> list[dict[str, Any]]:
    """The tool calls a tool approval decides, by name: a space reads the card, the arguments stay in XTM One."""
    from app.services.teams_link_cards import MAX_TOOLS_SHOWN

    if not view.proposals:
        return []
    if len(view.proposals) > MAX_TOOLS_SHOWN:
        return [_paragraph(_escape(notice("card_too_many_tools", language, count=len(view.proposals))))]
    lines = [_bold(notice("card_tools_awaiting", language))]
    lines += [f"<code>{_escape(_clip(p.get('tool_name') or '?', _MAX_NAME))}</code>" for p in view.proposals]
    return [_paragraph("<br>".join(lines))]


def _answer_widgets(view: DecisionView, ask: dict[str, Any], language: str) -> list[dict[str, Any]]:
    """One button per option (the recommendation filled), then a text input and its "Send" button (I.1)."""
    kind = view.decision.kind
    recommendation = str(ask.get("recommendation") or "")
    buttons: list[dict[str, Any]] = []
    for option in ask.get("options") or []:
        key = str(option.get("key") or "")
        if not key or len(buttons) >= _MAX_BUTTONS:
            continue
        button: dict[str, Any] = {
            "text": _label(option, kind, language, _MAX_BUTTON),
            "onClick": {"action": {"function": ACTION_ANSWER, "parameters": _parameters(view, key)}},
        }
        if recommendation and key == recommendation:
            button["color"] = _PRIMARY
        buttons.append(button)
    widgets: list[dict[str, Any]] = [{"buttonList": {"buttons": buttons}}] if buttons else []
    if kind != "tool_approval":
        # A tool approval is answered by its choice alone; a reason without one answers nothing.
        widgets.append(
            {
                "textInput": {
                    "name": TEXT_INPUT,
                    "label": notice("card_answer_with_text", language),
                    "hintText": notice("card_answer_text", language),
                    "type": "MULTIPLE_LINE",
                }
            }
        )
        widgets.append(
            {
                "buttonList": {
                    "buttons": [
                        {
                            "text": notice("card_send", language),
                            "onClick": {"action": {"function": ACTION_TEXT, "parameters": _parameters(view)}},
                        }
                    ]
                }
            }
        )
    return widgets


def _link_widgets(view: DecisionView, ask: dict[str, Any], language: str) -> list[dict[str, Any]]:
    buttons: list[dict[str, Any]] = []
    for link in (ask.get("links") or [])[:_MAX_LINKS]:
        url = str(link.get("url") or "")
        if url.startswith(("https://", "http://")):
            buttons.append(
                {"text": _clip(link.get("label") or url, _MAX_BUTTON), "onClick": {"openLink": {"url": url}}}
            )
    if view.page_path:
        buttons.append(
            {
                "text": notice("card_open", language),
                "onClick": {"openLink": {"url": f"{_page_base()}{view.page_path}"}},
            }
        )
    return [{"buttonList": {"buttons": buttons}}] if buttons else []


def _decided_widgets(view: DecisionView, ask: dict[str, Any], language: str) -> list[dict[str, Any]]:
    row = view.decision
    if row.status != "answered":
        key = "card_decision_expired" if row.status == "expired" else "card_decision_cancelled"
        return [_paragraph(_escape(notice(key, language)))]
    answer = row.answer if isinstance(row.answer, dict) else {}
    who = _clip(view.answered_by_name or notice("card_someone", language), _MAX_NAME)
    decisions = answer.get("decisions")
    if row.kind == "tool_approval" and isinstance(decisions, dict) and decisions:
        counts: dict[str, int] = {}
        for verdict in decisions.values():
            if verdict:
                counts[str(verdict)] = counts.get(str(verdict), 0) + 1
        labels = {key: _option_label(ask, key, row.kind, language) or key for key in counts}
        if len(counts) == 1:
            (only,) = labels.values()
            line = _runs(notice("card_decided_option", language), name=who, option=only)
        else:
            summary = ", ".join(f"{labels[key]} ({counts[key]})" for key in sorted(counts))
            line = _runs(notice("card_decided_tools", language), name=who, verdicts=summary)
    else:
        label = answer.get("label")
        chosen = _clip(label, 120) if label else _option_label(ask, answer.get("option"), row.kind, language)
        line = (
            _runs(notice("card_decided_option", language), name=who, option=chosen)
            if chosen
            else _runs(notice("card_answered_by", language), name=who)
        )
    widgets = [_paragraph(line)]
    if answer.get("text"):
        widgets.append(_paragraph(_escape(_clip(answer.get("text"), _MAX_QUESTION))))
    when = _utc(row.answered_at.isoformat() if row.answered_at else None)
    if when:
        widgets.append(_paragraph(f'<font color="#5f6368">{_escape(when)}</font>'))
    return widgets


def render_google_chat_decision(view: DecisionView, language: str, *, banner: str | None = None) -> dict[str, Any]:
    """The Chat message of *view*'s decision: ``{"text", "cardsV2"}``.

    ``text`` is the notification preview and what a client without cards
    shows. The card is the same for every member of the space, so an open
    decision always carries its buttons and the line "Waiting for <name>":
    the router refuses anyone else's click (I.5).
    """
    row = view.decision
    ask = row.ask if isinstance(row.ask, dict) else {}
    widgets: list[dict[str, Any]] = []
    if banner:
        widgets.append(_paragraph(_bold(banner)))
    if row.status == "open":
        widgets.extend(_ask_widgets(ask, row.kind, language, show_effects=True))
        widgets.extend(_proposal_widgets(view, language))
        if not _decides_unshown_calls(view):
            widgets.extend(_answer_widgets(view, ask, language))
        widgets.append(
            _paragraph(_runs(notice("card_waiting_for", language), name=_clip(view.recipient_name, _MAX_NAME)))
        )
    else:
        widgets.extend(_ask_widgets(ask, row.kind, language, show_effects=False))
        widgets.extend(_decided_widgets(view, ask, language))
    widgets.extend(_link_widgets(view, ask, language))
    preview = _clip(ask.get("question") or notice("card_header_decision", language), 300)
    card = {"header": _header(view, language), "sections": [{"widgets": widgets}]}
    return {"text": preview, "cardsV2": [{"cardId": CARD_ID, "card": card}]}


# --- Answering ----------------------------------------------------------------


def parse_click(event: dict[str, Any]) -> tuple[str, dict[str, str], str | None] | None:
    """A ``CARD_CLICKED`` event as ``(method, parameters, text)``, else ``None`` (a client may send anything).

    Reads both event shapes Chat sends: ``action`` (``actionMethodName`` and
    ``parameters``) and ``common`` (``invokedFunction`` and ``parameters``).
    """
    raw_action = event.get("action")
    raw_common = event.get("common")
    action: dict[str, Any] = raw_action if isinstance(raw_action, dict) else {}
    common: dict[str, Any] = raw_common if isinstance(raw_common, dict) else {}
    method = str(action.get("actionMethodName") or common.get("invokedFunction") or "")
    if method not in (ACTION_ANSWER, ACTION_TEXT):
        return None
    params: dict[str, str] = {}
    for item in action.get("parameters") or []:
        if isinstance(item, dict) and item.get("key"):
            params[str(item["key"])] = str(item.get("value") or "")
    for key, value in (common.get("parameters") or {}).items():
        params.setdefault(str(key), str(value or ""))
    if not params.get("id") or not params.get("d"):
        return None
    text = None
    inputs = (common.get("formInputs") or {}).get(TEXT_INPUT) or {}
    values = ((inputs.get("stringInputs") or {}).get("value") or []) if isinstance(inputs, dict) else []
    if values and isinstance(values[0], str):
        text = values[0]
    return method, params, text


def _refusal_key(exc: HTTPException) -> str:
    if exc.status_code in (403, 404):
        return "card_decision_refused"
    if exc.status_code == 409:
        return "card_decision_already_answered"
    return "card_decision_failed"


async def answer(
    db: AsyncSession,
    value: dict[str, str],
    user: Any,
    language: str,
    *,
    option: str | None = None,
    text: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Answer the decision *value* names as *user*: ``(message for everyone, private notice for the clicker)``.

    The message is the card to put in place of the posted one (``None``:
    leave it as is); the notice is what only the clicker reads (``None``:
    none). A refusal never changes the card others see (I.5).
    """
    from app.services.decision_router import answer_decision

    row = await _row_for(db, {"id": value.get("id")})
    if row is None:
        return None, notice("card_decision_failed", language)
    if user is None:
        return None, notice("card_not_linked_decision", language)
    if str(value.get("d") or "") != str(row.id):
        # Asked again since this card was posted: the old card answers nothing.
        return None, notice("card_changed", language)
    if row.status != "open":
        view = await load_view(db, row, user, language)
        return render_google_chat_decision(view, language), notice("card_decision_already_answered", language)
    option = (option or "").strip() or None
    text = (text or "")[:_MAX_TEXT].strip() or None
    if option is None and row.kind == "tool_approval":
        return None, notice("card_choose_approval", language)
    if option is None and text is None:
        return None, notice("card_answer_empty", language)
    public = row.public_id
    try:
        await answer_decision(db, public, user, option=option, text=text, surface=SURFACE)
    except HTTPException as exc:
        logger.info("Google Chat decision card answer on %s refused: %s", public, exc.status_code)
        await db.rollback()
        return None, notice(_refusal_key(exc), language)
    await db.commit()
    row = await _row_for(db, {"id": public})
    if row is None:
        return None, None
    view = await load_view(db, row, user, language)
    return render_google_chat_decision(view, language), None


def click_response(message: dict[str, Any] | None, private: str | None, clicker: str) -> dict[str, Any]:
    """The synchronous answer to a ``CARD_CLICKED`` event.

    The decided card replaces the posted one (``UPDATE_MESSAGE``); a notice
    goes to the clicker alone (``privateMessageViewer``), the card unchanged.
    """
    if message is not None and private is None:
        return {"actionResponse": {"type": "UPDATE_MESSAGE"}, **message}
    if private:
        response: dict[str, Any] = {"actionResponse": {"type": "NEW_MESSAGE"}, "text": private}
        if clicker:
            response["privateMessageViewer"] = {"name": clicker}
        return response
    return {}


# --- Sending ------------------------------------------------------------------


async def post_message(token: str, space: str, message: dict[str, Any]) -> dict[str, Any] | None:
    """``spaces.messages.create`` of a card message as the Chat app; Chat's answer, else ``None``."""
    parent = space if space.startswith("spaces/") else f"spaces/{space}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{CHAT_API_BASE}/{parent}/messages",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=message,
            )
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Google Chat decision card not sent: %s", format_exception_message(exc))
        return None
    if resp.status_code != 200 or not isinstance(data, dict) or not data.get("name"):
        logger.warning("Google Chat decision card refused: HTTP %s", resp.status_code)
        return None
    return data


async def send_decision_card(db: AsyncSession, decision: Decision, token: str, space: str, *, language: str) -> bool:
    """Post *decision*'s card into *space* as the Chat app; ``True`` when Chat took it."""
    # No viewer: the card the whole space reads (proposals redacted), answered per click.
    view = await load_view(db, decision, None, language)
    sent = await post_message(token, space, render_google_chat_decision(view, language))
    if sent is None:
        return False
    logger.info("Google Chat decision card for %s sent", decision.public_id)
    return True


def link_message(decision: Decision, recipient_id: Any) -> str:
    """The text a message posted as a user carries: the question and one signed answer link per option (S9b)."""
    from app.services.decision_answer_links import link_text

    return link_text(decision, recipient_id, LINK_SURFACE)
