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

"""The Teams card of a decision, answered in place (#842, S9; human-gates.md I.1 to I.5).

One renderer, :func:`render_decision_card`, turns a ``decisions`` row (any
source: run HITL, tool approval, flow Ask-user, work item) into an Adaptive
Card for the person who views it:

* the recipient sees the question, the context, one button per option (the
  recommendation as the positive style), an optional text field, the
  deadline and the links, and answers with verb ``xtm.decision.answer``;
* any other viewer sees the same content and "Waiting for <name>" in place of
  the buttons (I.5);
* once answered, every viewer sees who answered, when and which option.

An answer goes through :func:`app.services.decision_router.answer_decision`
as the person who clicked, so the recipient rule, the row lock and the audit
are the router's; the surface recorded is ``teams_card``. Agents never write
this JSON: they pass a decision.

Third-party text (the question, labels, names) is rendered as ``TextRun``,
never markdown; the only URLs are the platform's own pages and the ask's
links, which :mod:`app.services.decision_ask` already restricts to http(s).
Card data comes back from the client and only names what to look up.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.decision import Decision
from app.services.channel_notices import notice
from app.services.teams_format import ensure_full_width_card

logger = logging.getLogger(__name__)

CARD_VERSION = "1.4"
ADAPTIVE_CARD = "application/vnd.microsoft.card.adaptive"

#: The one answer verb of a decision card; the run verbs of a pasted run card
#: (``xtm.run.decide``, ``xtm.run.answer``) stay answered for cards already posted.
VERB_DECISION_ANSWER = "xtm.decision.answer"
VERB_REFRESH = "xtm.refresh"
DATA_KIND = "decision"

_MAX_QUESTION = 1500
_MAX_CONTEXT = 2000
_MAX_LABEL = 120
_MAX_EFFECT = 300
_MAX_NAME = 200
_MAX_TEXT = 4000

_SOURCE_NOTICE = {
    "hitl": "card_source_run",
    "tool_approval": "card_source_tool_approval",
    "flow_ask": "card_source_flow",
    "work_item": "card_source_work_item",
}


def _clip(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _text(text: str, **style: Any) -> dict[str, Any]:
    """A paragraph never read as markdown (``TextRun``)."""
    return {"type": "RichTextBlock", "inlines": [{"type": "TextRun", "text": text, **style}]}


def _runs(template: str, **values: str) -> dict[str, Any]:
    """A catalog sentence whose ``{name}`` values are runs of their own, never formatted into it."""
    inlines: list[dict[str, Any]] = []
    for piece in re.split(r"(\{[a-z_]+\})", template):
        if not piece:
            continue
        key = piece[1:-1] if piece.startswith("{") and piece.endswith("}") else None
        if key in values:
            inlines.append({"type": "TextRun", "text": values[key], "weight": "bolder"})
        else:
            inlines.append({"type": "TextRun", "text": piece})
    return {"type": "RichTextBlock", "inlines": inlines}


def _page_base() -> str:
    return (settings.frontend_url or settings.base_url).rstrip("/")


def _utc(value: str | None) -> str | None:
    """An ISO time as the Adaptive Card DATE / TIME functions read it (UTC)."""
    if not value:
        return None
    from datetime import UTC, datetime

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class DecisionView:
    """What one viewer sees of one decision."""

    decision: Decision
    #: True: the viewer is the recipient; False: another viewer; None: unknown to the platform.
    may_answer: bool | None
    recipient_name: str
    answered_by_name: str | None = None
    agent_name: str | None = None
    page_path: str | None = None


def _data(view: DecisionView, **extra: str) -> dict[str, Any]:
    row = view.decision
    # ``d`` pins the row the card showed: a re-ask opens a new row and the old card answers nothing.
    return {"kind": DATA_KIND, "id": row.public_id, "d": str(row.id), **extra}


def _option_label(ask: dict[str, Any], key: str | None) -> str | None:
    if not key:
        return None
    for option in ask.get("options") or []:
        if str(option.get("key", "")).casefold() == str(key).casefold():
            return _clip(option.get("label") or option.get("key"), _MAX_LABEL)
    return _clip(key, _MAX_LABEL)


def _header(view: DecisionView, language: str) -> list[dict[str, Any]]:
    source = notice(_SOURCE_NOTICE.get(view.decision.kind, "card_source_run"), language)
    body = [_text(notice("card_header_decision", language), isSubtle=True, size="small")]
    if view.agent_name:
        body.append(_runs(notice("card_from_by", language), source=source, agent=_clip(view.agent_name, _MAX_NAME)))
    else:
        body.append(_runs(notice("card_from", language), source=source))
    return body


def _ask_body(ask: dict[str, Any], language: str, *, show_effects: bool) -> list[dict[str, Any]]:
    body: list[dict[str, Any]] = [_text(_clip(ask.get("question"), _MAX_QUESTION), weight="bolder", size="medium")]
    context = str(ask.get("context") or "").strip()
    for line in context.splitlines()[:20]:
        body.append(_text(_clip(line, _MAX_CONTEXT), isSubtle=True))
    recommendation = str(ask.get("recommendation") or "")
    for option in ask.get("options") or []:
        label = _clip(option.get("label") or option.get("key"), _MAX_LABEL)
        inlines = [{"type": "TextRun", "text": label, "weight": "bolder"}]
        if recommendation and str(option.get("key")) == recommendation:
            inlines.append({"type": "TextRun", "text": f"  {notice('card_recommended', language)}", "color": "good"})
        if show_effects and option.get("effect"):
            inlines.append({"type": "TextRun", "text": f": {_clip(option['effect'], _MAX_EFFECT)}"})
        body.append({"type": "RichTextBlock", "inlines": inlines})
    deadline = _utc(ask.get("deadline"))
    if deadline:
        body.append(
            {
                "type": "TextBlock",
                # DATE / TIME render in the viewer's locale; the value is UTC.
                "text": f"{notice('card_label_deadline', language)}: {{{{DATE({deadline}, SHORT)}}}} {{{{TIME({deadline})}}}}",
                "isSubtle": True,
                "wrap": True,
            }
        )
    return body


def _link_actions(view: DecisionView, ask: dict[str, Any], language: str) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for link in (ask.get("links") or [])[:3]:
        url = str(link.get("url") or "")
        if url.startswith(("https://", "http://")):
            actions.append({"type": "Action.OpenUrl", "title": _clip(link.get("label") or url, 60), "url": url})
    if view.page_path:
        actions.append(
            {"type": "Action.OpenUrl", "title": notice("card_open", language), "url": f"{_page_base()}{view.page_path}"}
        )
    return actions


def _answer_actions(
    view: DecisionView, ask: dict[str, Any], language: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The text field and one ``Action.Execute`` per option, the recommendation positive."""
    body = [
        {
            "type": "Input.Text",
            "id": "text",
            "isMultiline": True,
            "placeholder": notice("card_answer_text", language),
            "maxLength": _MAX_TEXT,
        }
    ]
    recommendation = str(ask.get("recommendation") or "")
    actions: list[dict[str, Any]] = []
    for option in ask.get("options") or []:
        key = str(option.get("key") or "")
        action = {
            "type": "Action.Execute",
            "verb": VERB_DECISION_ANSWER,
            "title": _clip(option.get("label") or key, 60),
            "data": _data(view, option=key),
        }
        if key == recommendation:
            action["style"] = "positive"
        elif key == "reject":
            action["style"] = "destructive"
        actions.append(action)
    if view.decision.kind != "tool_approval":
        actions.append(
            {
                "type": "Action.Execute",
                "verb": VERB_DECISION_ANSWER,
                "title": notice("card_send_text", language),
                "data": _data(view),
            }
        )
    return body, actions


def _refresh(view: DecisionView) -> dict[str, Any]:
    # No ``userIds``: Teams refreshes for every member of a conversation up to 60 people.
    return {"action": {"type": "Action.Execute", "verb": VERB_REFRESH, "data": _data(view)}}


def render_decision_card(view: DecisionView, language: str, *, banner: str | None = None) -> dict[str, Any]:
    """The Adaptive Card of *view*'s decision, as its viewer may see and answer it."""
    row = view.decision
    ask = row.ask if isinstance(row.ask, dict) else {}
    body = _header(view, language)
    if banner:
        body.append(_text(banner, color="attention", weight="bolder"))
    actions: list[dict[str, Any]] = []
    if row.status == "open":
        body.extend(_ask_body(ask, language, show_effects=True))
        if view.may_answer:
            answer_body, actions = _answer_actions(view, ask, language)
            body.extend(answer_body)
        elif view.may_answer is False:
            body.append(_runs(notice("card_waiting_for", language), name=_clip(view.recipient_name, _MAX_NAME)))
        else:
            body.append(_text(notice("card_not_linked_decision", language), isSubtle=True))
    else:
        body.extend(_ask_body(ask, language, show_effects=False))
        body.extend(_decided_body(view, ask, language))
    return ensure_full_width_card(
        {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": CARD_VERSION,
            "refresh": _refresh(view),
            "body": body,
            "actions": [*actions, *_link_actions(view, ask, language)],
        }
    )


def _decided_body(view: DecisionView, ask: dict[str, Any], language: str) -> list[dict[str, Any]]:
    row = view.decision
    if row.status != "answered":
        key = "card_decision_expired" if row.status == "expired" else "card_decision_cancelled"
        return [_text(notice(key, language), isSubtle=True)]
    answer = row.answer if isinstance(row.answer, dict) else {}
    who = _clip(view.answered_by_name or notice("card_someone", language), _MAX_NAME)
    label = answer.get("label")
    chosen = _clip(label, _MAX_LABEL) if label else _option_label(ask, answer.get("option"))
    body = [
        _runs(notice("card_decided_option", language), name=who, option=chosen)
        if chosen
        else _runs(notice("card_answered_by", language), name=who)
    ]
    if answer.get("text"):
        body.append(_text(_clip(answer.get("text"), _MAX_QUESTION), isSubtle=True))
    when = _utc(row.answered_at.isoformat() if row.answered_at else None)
    if when:
        body.append(
            {
                "type": "TextBlock",
                "text": f"{{{{DATE({when}, SHORT)}}}} {{{{TIME({when})}}}}",
                "isSubtle": True,
                "wrap": True,
            }
        )
    return body


# --- Reading a decision as a viewer ------------------------------------------


async def may_answer(db: AsyncSession, decision: Decision, user: Any) -> bool:
    """Whether *user* is the decision's recipient (the one rule of section 6, read side).

    The engine applies its own rule again when the answer is written; this only
    decides whether the card offers buttons or "Waiting for <name>".
    """
    if user is None or not getattr(user, "is_active", True):
        return False
    recipient = str(decision.recipient or "")
    kind, _, ref = recipient.partition(":")
    if recipient == "company":
        return True
    if recipient == "admins":
        return bool(getattr(user, "is_admin", False))
    if kind == "user":
        return ref == str(user.id)
    if kind == "group":
        from app.models.group import GroupMember

        try:
            group_id = uuid.UUID(ref)
        except ValueError:
            return False
        found = await db.scalar(
            select(GroupMember.id).where(GroupMember.group_id == group_id, GroupMember.user_id == user.id)
        )
        return found is not None
    return False


async def recipient_name(db: AsyncSession, recipient: str, language: str) -> str:
    """Who a decision waits for, in words: a person's name, a group, administrators, everyone."""
    kind, _, ref = str(recipient or "").partition(":")
    if recipient == "company":
        return notice("card_recipient_company", language)
    if recipient == "admins" or not ref:
        return notice("card_recipient_admins", language)
    try:
        uid = uuid.UUID(ref)
    except ValueError:
        return notice("card_recipient_admins", language)
    if kind == "user":
        from app.models.user import User

        return await db.scalar(select(User.full_name).where(User.id == uid)) or notice("card_someone", language)
    from app.models.group import Group

    return await db.scalar(select(Group.name).where(Group.id == uid)) or notice("card_someone", language)


async def _source_meta(db: AsyncSession, decision: Decision) -> tuple[str | None, str | None]:
    """The agent that asked (when one did) and the page the decision opens (I.7)."""
    from app.services.decision_links import decision_link

    if decision.source_type == "run":
        from app.models.agent import Agent
        from app.models.assignment import AssignmentRun

        name = await db.scalar(
            select(Agent.name)
            .join(AssignmentRun, AssignmentRun.agent_id == Agent.id)
            .where(AssignmentRun.id == decision.source_id)
        )
        return name, decision_link("run", decision.source_id)
    if decision.source_type == "flow_run":
        from app.models.flow_run import FlowRun

        flow_id = await db.scalar(select(FlowRun.flow_id).where(FlowRun.id == decision.source_id))
        return None, decision_link("flow_run", decision.source_id, flow_id=flow_id)
    from app.models.agent import Agent
    from app.models.work_item import WorkItem

    name = await db.scalar(
        select(Agent.name)
        .join(WorkItem, WorkItem.created_by_agent_id == Agent.id)
        .where(WorkItem.id == decision.source_id)
    )
    return name, decision_link("work_item", decision.source_id)


async def load_view(db: AsyncSession, decision: Decision, user: Any, language: str) -> DecisionView:
    """The decision as *user* sees it (``None``: a Teams user the platform does not know)."""
    from app.models.user import User

    answered_by = None
    if decision.answered_by is not None:
        answered_by = await db.scalar(select(User.full_name).where(User.id == decision.answered_by))
    agent_name, page_path = await _source_meta(db, decision)
    return DecisionView(
        decision=decision,
        may_answer=None if user is None else await may_answer(db, decision, user),
        recipient_name=await recipient_name(db, decision.recipient, language),
        answered_by_name=answered_by,
        agent_name=agent_name,
        page_path=page_path,
    )


async def _row_for(db: AsyncSession, data: dict) -> Decision | None:
    """The decision row the card data names, or ``None``: a client may send anything."""
    from app.services.decision_router import resolve

    try:
        _ref, row = await resolve(db, str(data.get("id") or ""))
    except HTTPException:
        return None
    return row


def is_decision_data(data: object) -> bool:
    return isinstance(data, dict) and data.get("kind") == DATA_KIND


def _refusal_key(exc: HTTPException) -> str:
    if exc.status_code in (403, 404):
        return "card_decision_refused"
    if exc.status_code == 409:
        return "card_decision_already_answered"
    return "card_decision_failed"


async def act(db: AsyncSession, verb: str, data: dict[str, Any], user: Any, language: str) -> dict[str, Any]:
    """Refresh or answer a decision card as *user*, and answer with their card."""
    from app.services.decision_router import answer_decision

    row = await _row_for(db, data)
    if row is None:
        raise ValueError("card data names no decision")
    if verb == VERB_REFRESH or user is None:
        return render_decision_card(await load_view(db, row, user, language), language)
    if verb != VERB_DECISION_ANSWER:
        raise ValueError("unknown card verb")
    if str(data.get("d") or "") != str(row.id):
        # Asked again since this card was posted: the old card answers nothing.
        return render_decision_card(
            await load_view(db, row, user, language), language, banner=notice("card_changed", language)
        )
    if row.status != "open":
        return render_decision_card(
            await load_view(db, row, user, language),
            language,
            banner=notice("card_decision_already_answered", language),
        )
    option = str(data.get("option") or "").strip() or None
    text = str(data.get("text") or "")[:_MAX_TEXT].strip() or None
    if option is None and text is None:
        return render_decision_card(
            await load_view(db, row, user, language), language, banner=notice("card_answer_empty", language)
        )
    public = row.public_id
    try:
        await answer_decision(db, public, user, option=option, text=text, surface="teams_card")
    except HTTPException as exc:
        logger.info("Teams decision card answer on %s refused: %s", public, exc.status_code)
        await db.rollback()
        row = await _row_for(db, {"id": public})
        if row is None:
            raise ValueError("card data names no decision") from exc
        return render_decision_card(
            await load_view(db, row, user, language), language, banner=notice(_refusal_key(exc), language)
        )
    await db.commit()
    row = await _row_for(db, {"id": public})
    if row is None:
        raise ValueError("card data names no decision")
    return render_decision_card(
        await load_view(db, row, user, language), language, banner=notice("card_answer_sent", language)
    )
