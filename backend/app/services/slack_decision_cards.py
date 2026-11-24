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

"""The Slack decision card of every human gate (#842, S10, human-gates.md I.1 to I.5).

One Block Kit message per decision, rendered from the ask contract the
``decisions`` row holds (run HITL, tool approval, flow Ask-user, work item):
a header, the source, the question and its context, one button per option
(the recommendation ``primary``), "Answer with text" opening a modal, and the
links. A click comes back through ``/api/channels/slack/interactions``
(signing-secret checked in ``channels/slack.py``) and is answered by
``decision_router.answer_decision`` as the person who clicked (surface
``slack_card``), so the recipient rule, the row lock and the audit are the
router's. A Slack message is the same for every viewer: a click from anyone
but the recipient is refused with an ephemeral message and the card stays as
it is (I.5); an answer replaces the buttons with the decided state.

The view (``DecisionView``) and its loading are the Teams card's
(``decision_cards``); only the drawing differs.
"""

from __future__ import annotations

import json
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

SLACK_API_BASE = "https://slack.com/api"

#: ``action_id`` prefix of an option button (``xtm.decision.answer:<key>``).
ACTION_ANSWER = "xtm.decision.answer"
#: ``action_id`` of the button that opens the free-text modal.
ACTION_TEXT = "xtm.decision.text"
#: ``callback_id`` of the free-text modal.
MODAL_CALLBACK = "xtm.decision.text_modal"
MODAL_BLOCK = "answer"
MODAL_INPUT = "text"
SURFACE = "slack_card"

#: Slack's limits: 25 elements per actions block, 75 characters per button
#: text, 2,000 per button value, 3,000 per section text, 150 per header.
_MAX_BUTTONS = 25
_MAX_BUTTON = 75
_MAX_SECTION = 3000
_MAX_HEADER = 150
_MAX_LINKS = 3


def _plain(text: str, limit: int = _MAX_SECTION, *, keep_lines: bool = False) -> dict[str, Any]:
    """A plain-text object: a value from an ask is never read as mrkdwn."""
    value = _clip_lines(str(text or ""), limit) if keep_lines else _clip(text, limit)
    return {"type": "plain_text", "text": value or " ", "emoji": False}


def _clip_lines(text: str, limit: int = _MAX_SECTION) -> str:
    """*text* cut at *limit* with its line breaks kept (``_clip`` folds every space, newlines included)."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _escape(text: str) -> str:
    """*text* safe inside mrkdwn: ``&``, ``<`` and ``>`` escaped as Slack asks."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _context(*texts: dict[str, Any]) -> dict[str, Any]:
    return {"type": "context", "elements": list(texts)}


def _section(text: dict[str, Any]) -> dict[str, Any]:
    return {"type": "section", "text": text}


_ZWSP = "\u200b"
_SLOT = re.compile(r"(\{[a-z_]+\})")


def _bold_runs(template: str, **values: str) -> dict[str, Any]:
    """A catalog sentence with its ``{name}`` values in bold, each value escaped (never formatted raw).

    The template is split once on its slots and each slot is substituted once, so a value that holds
    a slot's own text (an agent named ``{agent}``) is never read as a slot again.
    """
    pieces = _SLOT.split(_escape(template))
    out: list[str] = []
    for index, piece in enumerate(pieces):
        key = piece[1:-1] if index % 2 else None
        if key is None or key not in values:
            out.append(piece)
            continue
        before = out[-1][-1:] if out else ""
        after = pieces[index + 1][:1] if index + 1 < len(pieces) else ""
        # Slack reads *bold* only at a word boundary: next to a letter (a Japanese sentence has no
        # spaces) a zero-width space makes one, else the asterisks show (UX round 1 of #1086, ja).
        lead = _ZWSP if before.isalnum() else ""
        tail = _ZWSP if after.isalnum() else ""
        out.append(f"{lead}*{_escape(values[key])}*{tail}")
    return {"type": "mrkdwn", "text": "".join(out), "verbatim": True}


def _slack_date(value: str | None, fallback_language: str) -> str | None:
    """An ISO time as Slack's ``<!date>`` token, shown in the viewer's own time zone."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    fallback = parsed.strftime("%Y-%m-%d %H:%M UTC")
    return f"<!date^{int(parsed.timestamp())}^{{date_short}} {{time}}|{fallback}>"


def button_value(view: DecisionView, option: str | None = None) -> str:
    """What a button carries: the decision id, the row it showed and the option (I.3)."""
    row = view.decision
    value: dict[str, str] = {"id": row.public_id, "d": str(row.id)}
    if option is not None:
        value["option"] = option
    return json.dumps(value, separators=(",", ":"))


def _header(view: DecisionView, language: str) -> list[dict[str, Any]]:
    source = notice(_SOURCE_NOTICE.get(view.decision.kind, "card_source_run"), language)
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": _plain(notice("card_header_decision", language), _MAX_HEADER)}
    ]
    if view.agent_name:
        line = _bold_runs(notice("card_from_by", language), source=source, agent=_clip(view.agent_name, _MAX_NAME))
    else:
        line = _bold_runs(notice("card_from", language), source=source)
    blocks.append(_context(line))
    return blocks


def _ask_blocks(ask: dict[str, Any], kind: str, language: str, *, show_effects: bool) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        _section(
            {"type": "mrkdwn", "text": f"*{_escape(_clip(ask.get('question'), _MAX_QUESTION))}*", "verbatim": True}
        )
    ]
    context = "\n".join(_clip(line, _MAX_CONTEXT) for line in str(ask.get("context") or "").strip().splitlines()[:20])
    if context:
        blocks.append(_section(_plain(context, keep_lines=True)))
    recommendation = str(ask.get("recommendation") or "")
    lines: list[str] = []
    for option in ask.get("options") or []:
        if kind == "tool_approval" and not option.get("effect"):
            continue
        line = f"*{_escape(_label(option, kind, language))}*"
        if recommendation and str(option.get("key")) == recommendation:
            line += f" ({_escape(notice('card_recommended', language))})"
        if show_effects and option.get("effect"):
            line += f": {_escape(_clip(option['effect'], _MAX_EFFECT))}"
        lines.append(f"• {line}")
    if lines:
        blocks.append(_section({"type": "mrkdwn", "text": _clip_lines("\n".join(lines)), "verbatim": True}))
    deadline = _slack_date(ask.get("deadline"), language)
    if deadline:
        blocks.append(
            _context({"type": "mrkdwn", "text": f"{_escape(notice('card_label_deadline', language))}: {deadline}"})
        )
    return blocks


def _proposal_blocks(view: DecisionView, language: str) -> list[dict[str, Any]]:
    """The tool calls a tool approval decides, by name: a Slack message is read by every
    member of the conversation, so the arguments stay in XTM One (the Open button)."""
    from app.services.teams_link_cards import MAX_TOOLS_SHOWN

    if not view.proposals:
        return []
    if len(view.proposals) > MAX_TOOLS_SHOWN:
        return [_section(_plain(notice("card_too_many_tools", language, count=len(view.proposals))))]
    lines = [f"*{_escape(notice('card_tools_awaiting', language))}*"]
    for proposal in view.proposals:
        lines.append(f"`{_escape(_clip(proposal.get('tool_name') or '?', _MAX_NAME))}`")
    return [_section({"type": "mrkdwn", "text": _clip_lines("\n".join(lines)), "verbatim": True})]


def _answer_block(view: DecisionView, ask: dict[str, Any], language: str) -> dict[str, Any] | None:
    """One button per option (the recommendation ``primary``), then "Answer with text" (I.1)."""
    kind = view.decision.kind
    recommendation = str(ask.get("recommendation") or "")
    elements: list[dict[str, Any]] = []
    for option in ask.get("options") or []:
        key = str(option.get("key") or "")
        if not key or len(elements) >= _MAX_BUTTONS - 1:
            continue
        button: dict[str, Any] = {
            "type": "button",
            "action_id": f"{ACTION_ANSWER}:{key}"[:255],
            "text": _plain(_label(option, kind, language, _MAX_BUTTON), _MAX_BUTTON),
            "value": button_value(view, key),
        }
        if recommendation and key == recommendation:
            button["style"] = "primary"
        elements.append(button)
    if kind != "tool_approval":
        # A tool approval is answered by its choice alone; a reason without one answers nothing.
        elements.append(
            {
                "type": "button",
                "action_id": ACTION_TEXT,
                "text": _plain(notice("card_answer_with_text", language), _MAX_BUTTON),
                "value": button_value(view),
            }
        )
    if not elements:
        return None
    return {"type": "actions", "block_id": f"decision:{view.decision.id}", "elements": elements}


def _link_block(view: DecisionView, ask: dict[str, Any], language: str) -> dict[str, Any] | None:
    from app.services.decision_ask import card_links

    elements: list[dict[str, Any]] = []
    for index, link in enumerate(card_links(ask, language, limit=_MAX_LINKS)):
        elements.append(
            {
                "type": "button",
                "action_id": f"xtm.decision.link:{index}",
                "text": _plain(link["label"], _MAX_BUTTON),
                "url": link["url"][:3000],
            }
        )
    if view.page_path:
        elements.append(
            {
                "type": "button",
                "action_id": "xtm.decision.open",
                "text": _plain(notice("card_open", language), _MAX_BUTTON),
                "url": f"{_page_base()}{view.page_path}",
            }
        )
    return {"type": "actions", "elements": elements} if elements else None


def _decided_blocks(view: DecisionView, ask: dict[str, Any], language: str) -> list[dict[str, Any]]:
    row = view.decision
    if row.status != "answered":
        key = "card_decision_expired" if row.status == "expired" else "card_decision_cancelled"
        return [_context(_plain(notice(key, language)))]
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
            line = _bold_runs(notice("card_decided_option", language), name=who, option=only)
        else:
            summary = ", ".join(f"{labels[key]} ({counts[key]})" for key in sorted(counts))
            line = _bold_runs(notice("card_decided_tools", language), name=who, verdicts=summary)
    else:
        label = answer.get("label")
        chosen = _clip(label, 120) if label else _option_label(ask, answer.get("option"), row.kind, language)
        line = (
            _bold_runs(notice("card_decided_option", language), name=who, option=chosen)
            if chosen
            else _bold_runs(notice("card_answered_by", language), name=who)
        )
    blocks: list[dict[str, Any]] = [_section(line)]
    if answer.get("text"):
        blocks.append(_section(_plain(str(answer.get("text")), _MAX_QUESTION)))
    when = _slack_date(row.answered_at.isoformat() if row.answered_at else None, language)
    if when:
        blocks.append(_context({"type": "mrkdwn", "text": when}))
    return blocks


def render_slack_decision(
    view: DecisionView, language: str, *, banner: str | None = None, banner_ok: bool = False
) -> dict[str, Any]:
    """The Block Kit message of *view*'s decision: ``{"text", "blocks"}``.

    ``text`` is the notification preview and the fallback a client without
    blocks shows. The message is the same for every viewer of a Slack
    conversation, so an open decision always carries its buttons and the line
    "Waiting for <name>": the router refuses anyone else's click (I.5).
    """
    row = view.decision
    ask = row.ask if isinstance(row.ask, dict) else {}
    blocks = _header(view, language)
    if banner:
        prefix = "" if banner_ok else ":warning: "
        blocks.append(_section({"type": "mrkdwn", "text": f"{prefix}*{_escape(banner)}*", "verbatim": True}))
    if row.status == "open":
        blocks.extend(_ask_blocks(ask, row.kind, language, show_effects=True))
        blocks.extend(_proposal_blocks(view, language))
        if not _decides_unshown_calls(view):
            answer = _answer_block(view, ask, language)
            if answer is not None:
                blocks.append(answer)
        blocks.append(
            _context(_bold_runs(notice("card_waiting_for", language), name=_clip(view.recipient_name, _MAX_NAME)))
        )
    else:
        blocks.extend(_ask_blocks(ask, row.kind, language, show_effects=False))
        blocks.extend(_decided_blocks(view, ask, language))
    links = _link_block(view, ask, language)
    if links is not None:
        blocks.append(links)
    preview = _clip(ask.get("question") or notice("card_header_decision", language), 300)
    return {"text": preview, "blocks": blocks}


def text_modal(view: DecisionView, language: str, *, channel: str, ts: str) -> dict[str, Any]:
    """The modal "Answer with text" opens; ``private_metadata`` pins the decision and the message."""
    metadata = json.loads(button_value(view))
    metadata.update({"channel": channel, "ts": ts})
    ask = view.decision.ask if isinstance(view.decision.ask, dict) else {}
    return {
        "type": "modal",
        "callback_id": MODAL_CALLBACK,
        "private_metadata": json.dumps(metadata, separators=(",", ":")),
        "title": _plain(notice("card_modal_title", language), 24),
        "submit": _plain(notice("card_send", language), 24),
        "close": _plain(notice("card_cancel", language), 24),
        "blocks": [
            _section({"type": "mrkdwn", "text": f"*{_escape(_clip(ask.get('question'), _MAX_QUESTION))}*"}),
            {
                "type": "input",
                "block_id": MODAL_BLOCK,
                "label": _plain(notice("card_answer", language), 2000),
                "element": {
                    "type": "plain_text_input",
                    "action_id": MODAL_INPUT,
                    "multiline": True,
                    "max_length": _MAX_TEXT,
                    "placeholder": _plain(notice("card_answer_text", language), 150),
                },
            },
        ],
    }


# --- Answering ----------------------------------------------------------------


def parse_value(raw: object) -> dict[str, str] | None:
    """A button value or modal metadata as the card wrote it, else ``None`` (a client may send anything)."""
    try:
        value = json.loads(str(raw or ""))
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or not value.get("id") or not value.get("d"):
        return None
    return {key: str(item) for key, item in value.items() if isinstance(item, str | int)}


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
    """Answer the decision *value* names as *user*: ``(message for everyone, ephemeral for the clicker)``.

    The message is the card to put in place of the posted one (``None``: leave
    it as is); the ephemeral is the notice only the clicker reads (``None``:
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
        return render_slack_decision(view, language), notice("card_decision_already_answered", language)
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
        logger.info("Slack decision card answer on %s refused: %s", public, exc.status_code)
        await db.rollback()
        return None, notice(_refusal_key(exc), language)
    await db.commit()
    row = await _row_for(db, {"id": public})
    if row is None:
        return None, None
    view = await load_view(db, row, user, language)
    return render_slack_decision(view, language), None


async def view_for(db: AsyncSession, value: dict[str, str], user: Any, language: str) -> DecisionView | None:
    """The decision *value* names, as *user* sees it, or ``None`` when it names none."""
    row = await _row_for(db, {"id": value.get("id")})
    if row is None:
        return None
    return await load_view(db, row, user, language)


# --- Sending ------------------------------------------------------------------


async def post_message(token: str, channel: str, message: dict[str, Any]) -> dict[str, Any] | None:
    """``chat.postMessage`` of a Block Kit message; Slack's answer when it took it, else ``None``."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{SLACK_API_BASE}/chat.postMessage",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
                json={"channel": channel, **message},
            )
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Slack decision card not sent: %s", format_exception_message(exc))
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        logger.warning("Slack decision card refused: %s", (data or {}).get("error", "unknown"))
        return None
    return data


async def send_decision_card(db: AsyncSession, decision: Decision, token: str, channel: str, *, language: str) -> bool:
    """Post *decision*'s card into *channel* (a member id opens the DM with the bot); ``True`` when Slack took it."""
    # No viewer: the card the whole conversation reads (proposals redacted), answered per click.
    view = await load_view(db, decision, None, language)
    sent = await post_message(token, channel, render_slack_decision(view, language))
    if sent is None:
        return False
    logger.info("Slack decision card for %s sent", decision.public_id)
    return True
