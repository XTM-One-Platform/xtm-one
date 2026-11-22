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

"""Adaptive Cards for XTM One links pasted in Microsoft Teams.

A link to a page of this platform pasted in Teams unfurls into a card
(``composeExtension/queryLink``), and the card is then rendered for each person
who views it (Universal Actions ``refresh``, ``adaptiveCard/action``). The
contract is ``dev-docs/teams-link-unfurl.md``:

* The card the paste posts is **neutral** (:func:`neutral_card`): it names no
  run, agent or state, because that stored content is what Teams shows outside
  a refresh - the activity feed, search, a client without Universal Actions.
* Every refresh renders the card **for its viewer** (:func:`viewer_card`) with
  the web's own rules: ``run_visible_filter`` to read, ``require_run_access``
  to answer. A viewer without access learns that and nothing else, and a
  missing run reads the same as a forbidden one.
* An answer goes through :func:`app.api.v1.assignments.answer_paused_run`, the
  function the respond endpoint calls, as the person who clicked.

Everything a third party controls (names, the question, tool names and
arguments) is rendered as ``TextRun`` elements, which Adaptive Cards never read
as markdown, and truncated. The only URLs are built from the platform's own
address and an id. Card data comes back from the client and is hostile: it only
ever names what to look up, never who is asking.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.url_identity import url_identity
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.user import User
from app.services.channel_notices import notice
from app.services.teams_format import ensure_full_width_card

logger = logging.getLogger(__name__)

CARD_VERSION = "1.4"
ADAPTIVE_CARD = "application/vnd.microsoft.card.adaptive"

VERB_REFRESH = "xtm.refresh"
VERB_DECIDE = "xtm.run.decide"
VERB_ANSWER = "xtm.run.answer"
VERB_REQUEST = "xtm.run.request"

#: Tools listed with their own choice; a pause with more is decided in the web
#: app, since a card must never decide calls it did not show.
MAX_TOOLS_SHOWN = 10
_MAX_NAME = 200
_MAX_QUESTION = 1500
_MAX_ARGUMENTS = 8
_MAX_ARGUMENT_VALUE = 200
_MAX_REASON = 500
_MAX_ANSWER = 4000

_RUN_PATH = re.compile(r"/operations/executions/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/?")
_STATUSES = frozenset(
    {"pending", "delayed", "running", "awaiting_human", "completed", "failed", "skipped"}
)


@dataclass(frozen=True)
class LinkTarget:
    """What a pasted link points to. Only assignment runs unfurl for now."""

    kind: str
    id: uuid.UUID


# --- Links -------------------------------------------------------------------


def _page_base() -> str:
    """Where the web app's pages are, as every link the platform hands out."""
    return (settings.frontend_url or settings.base_url).rstrip("/")


def _page_identities() -> set[str]:
    """The canonical addresses a link to one of our pages may start with."""
    found = set()
    for candidate in (settings.frontend_url, settings.base_url):
        identity = url_identity(candidate or "")
        if identity:
            found.add(identity)
    return found


def parse_link(url: object) -> LinkTarget | None:
    """The platform page *url* points to, or ``None`` when it is not one of ours.

    Ours means: an http(s) URL on the platform's own address (``FRONTEND_URL``
    or ``BASE_URL``, base path included), without userinfo, whose remaining
    path is a run page. A query string or a fragment is ignored.
    """
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    identity = url_identity(f"{parts.scheme}://{parts.netloc}{parts.path}")
    if identity is None:
        return None
    # ``url_identity`` drops the trailing slash; the run path pattern allows it.
    for base in _page_identities():
        if not identity.startswith(base + "/"):
            continue
        match = _RUN_PATH.fullmatch(identity[len(base):])
        if match:
            return LinkTarget(kind="run", id=uuid.UUID(match.group(1)))
    return None


def page_url(target: LinkTarget) -> str:
    """The web page of *target*, built from the platform's address and the id only."""
    return f"{_page_base()}/operations/executions/{target.id}"


# --- Card pieces -------------------------------------------------------------


def _clip(value: object, limit: int) -> str:
    """*value* as one line of at most *limit* characters."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _text(text: str, **style: Any) -> dict:
    """A paragraph whose content is never read as markdown.

    ``TextRun`` has no markdown, unlike ``TextBlock``: a name like
    ``[click](https://evil)`` stays those characters.
    """
    return {"type": "RichTextBlock", "inlines": [{"type": "TextRun", "text": text, **style}]}


def _runs(template: str, **values: str) -> dict:
    """A catalog sentence with its ``{name}`` values set as runs of their own.

    The sentence is split around its placeholders rather than formatted, so a
    value is never parsed with the text around it.
    """
    inlines: list[dict] = []
    for piece in re.split(r"(\{[a-z_]+\})", template):
        if not piece:
            continue
        key = piece[1:-1] if piece.startswith("{") and piece.endswith("}") else None
        if key in values:
            inlines.append({"type": "TextRun", "text": values[key], "weight": "bolder"})
        else:
            inlines.append({"type": "TextRun", "text": piece})
    return {"type": "RichTextBlock", "inlines": inlines}


def _labelled(label: str, value: str) -> dict:
    return {
        "type": "RichTextBlock",
        "inlines": [
            {"type": "TextRun", "text": f"{label}: ", "isSubtle": True},
            {"type": "TextRun", "text": value},
        ],
    }


def _header(language: str) -> dict:
    return _text(notice("card_header_run", language), isSubtle=True, size="small")


def _banner(text: str) -> dict:
    return _text(text, color="attention", weight="bolder")


def _refresh(target: LinkTarget) -> dict:
    # No ``userIds``: Teams refreshes automatically for every member of a
    # conversation of up to 60 people, and from the message menu above that.
    return {
        "action": {
            "type": "Action.Execute",
            "verb": VERB_REFRESH,
            "data": {"kind": target.kind, "id": str(target.id)},
        }
    }


def _open(target: LinkTarget, language: str) -> dict:
    return {"type": "Action.OpenUrl", "title": notice("card_open", language), "url": page_url(target)}


def _card(target: LinkTarget, body: list[dict], actions: list[dict], language: str) -> dict:
    return ensure_full_width_card(
        {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": CARD_VERSION,
            "refresh": _refresh(target),
            "body": body,
            "actions": [*actions, _open(target, language)],
        }
    )


# --- The views ---------------------------------------------------------------


def neutral_card(target: LinkTarget, language: str) -> dict:
    """What the paste posts: the same for every run, existing or not."""
    return _card(
        target,
        [_header(language), _text(notice("card_neutral_hint", language), isSubtle=True)],
        [],
        language,
    )


def _request_action(target: LinkTarget, title_key: str, language: str) -> dict:
    return {
        "type": "Action.Execute",
        "verb": VERB_REQUEST,
        "title": notice(title_key, language),
        "data": {"kind": target.kind, "id": str(target.id)},
    }


def no_access_card(target: LinkTarget, language: str, *, linked: bool, banner: str | None = None) -> dict:
    """A viewer who may not read the run, or whom the platform does not know.

    A known viewer may ask whoever can answer the run to act on it (D7); the
    button is the same for every run, existing or not.
    """
    key = "card_no_access" if linked else "card_not_linked"
    body = [_header(language), *([_banner(banner)] if banner else []), _text(notice(key, language))]
    actions = [_request_action(target, "card_ask_owner", language)] if linked else []
    return _card(target, body, actions, language)


def query_link_response(target: LinkTarget, language: str) -> dict:
    """The answer to ``composeExtension/queryLink``: the neutral card and its preview."""
    return {
        "composeExtension": {
            "type": "result",
            "attachmentLayout": "list",
            "attachments": [
                {
                    "contentType": ADAPTIVE_CARD,
                    "content": neutral_card(target, language),
                    "preview": {
                        "contentType": "application/vnd.microsoft.card.thumbnail",
                        "content": {"title": notice("card_header_run", language)},
                    },
                }
            ],
        }
    }


def _status_label(status: str, language: str) -> str:
    return notice(f"card_status_{status}", language) if status in _STATUSES else _clip(status, 40)


def argument_lines(arguments: object) -> list[str]:
    if not isinstance(arguments, dict):
        return [_clip(json.dumps(arguments, default=str), _MAX_ARGUMENT_VALUE)] if arguments else []
    lines = []
    for key, value in list(arguments.items())[:_MAX_ARGUMENTS]:
        shown = value if isinstance(value, str) else json.dumps(value, default=str)
        lines.append(f"{_clip(key, 60)}: {_clip(shown, _MAX_ARGUMENT_VALUE)}")
    if len(arguments) > _MAX_ARGUMENTS:
        lines.append("…")
    return lines


def question_digest(question: object) -> str:
    """A short digest of the question a free-text card showed, to refuse a stale answer."""
    return hashlib.sha256(str(question or "").encode()).hexdigest()[:16]


@dataclass(frozen=True)
class RunView:
    """What one viewer may see of one run."""

    run: AssignmentRun
    agent_name: str | None
    assignment_name: str | None
    can_respond: bool


def _last_responder(run: AssignmentRun) -> str | None:
    history = run.human_input_history or []
    last = history[-1] if history and isinstance(history[-1], dict) else None
    return _clip(last.get("responded_by"), _MAX_NAME) if last and last.get("responded_by") else None


def _decision_body(target: LinkTarget, proposals: list[dict], language: str) -> tuple[list[dict], list[dict]]:
    body: list[dict] = [_text(notice("card_tools_awaiting", language), weight="bolder")]
    calls = [str(p.get("tool_call_id") or "") for p in proposals]
    for index, proposal in enumerate(proposals):
        body.append(_text(_clip(proposal.get("tool_name") or "?", _MAX_NAME), weight="bolder", fontType="monospace"))
        for line in argument_lines(proposal.get("arguments")):
            body.append(_text(line, isSubtle=True, fontType="monospace"))
        body.append(
            {
                "type": "Input.ChoiceSet",
                "id": f"d{index}",
                "style": "expanded",
                "choices": [
                    {"title": notice("card_approve", language), "value": "approve"},
                    {"title": notice("card_reject", language), "value": "reject"},
                ],
            }
        )
    body.append(
        {
            "type": "Input.Text",
            "id": "reason",
            "placeholder": notice("card_reason", language),
            "maxLength": _MAX_REASON,
        }
    )
    data = {"kind": target.kind, "id": str(target.id), "calls": calls}
    actions = [
        {"type": "Action.Execute", "verb": VERB_DECIDE, "title": notice("card_submit", language), "data": data},
        {
            "type": "Action.Execute",
            "verb": VERB_DECIDE,
            "title": notice("card_approve_all", language),
            "data": {**data, "all": "approve"},
        },
        {
            "type": "Action.Execute",
            "verb": VERB_DECIDE,
            "title": notice("card_reject_all", language),
            "data": {**data, "all": "reject"},
        },
    ]
    return body, actions


def _answer_body(target: LinkTarget, question: str, language: str) -> tuple[list[dict], list[dict]]:
    body = [
        {
            "type": "Input.Text",
            "id": "answer",
            "isMultiline": True,
            "placeholder": notice("card_answer", language),
            "maxLength": _MAX_ANSWER,
        }
    ]
    actions = [
        {
            "type": "Action.Execute",
            "verb": VERB_ANSWER,
            "title": notice("card_send", language),
            "data": {"kind": target.kind, "id": str(target.id), "q": question_digest(question)},
        }
    ]
    return body, actions


def run_card(view: RunView, language: str, *, banner: str | None = None) -> dict:
    """The run as *view*'s viewer may see it, with the actions they may take."""
    from app.core.redaction import redact_known_secrets_for_display
    from app.tools.approval import redact_proposals

    run = view.run
    target = LinkTarget(kind="run", id=run.id)
    body: list[dict] = [_header(language)]
    if banner:
        body.append(_banner(banner))
    if view.assignment_name:
        body.append(_text(_clip(view.assignment_name, _MAX_NAME), weight="bolder", size="medium"))
    if view.agent_name:
        body.append(_labelled(notice("card_label_agent", language), _clip(view.agent_name, _MAX_NAME)))
    body.append(_labelled(notice("card_label_status", language), _status_label(run.status, language)))
    if run.started_at:
        started = run.started_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        # DATE / TIME are Adaptive Card functions, read in the viewer's locale;
        # the value is the platform's own timestamp.
        body.append(
            {
                "type": "TextBlock",
                "text": f"{notice('card_label_started', language)}: {{{{DATE({started}, SHORT)}}}} {{{{TIME({started})}}}}",
                "isSubtle": True,
                "wrap": True,
            }
        )

    actions: list[dict] = []
    if run.status == "awaiting_human":
        answered = bool(run.approval_decisions) or bool(run.human_input_response)
        proposals = [p for p in (run.pending_approvals or []) if isinstance(p, dict)]
        responder = _last_responder(run)
        if answered:
            # Answered, waiting for the worker to resume it: nothing left to decide.
            if responder:
                body.append(_runs(notice("card_answered_by", language), name=responder))
        elif proposals:
            if not view.can_respond:
                for proposal in redact_proposals(proposals) or []:
                    body.append(_text(_clip(proposal.get("tool_name") or "?", _MAX_NAME), fontType="monospace"))
                body.append(_text(notice("card_waiting_admin", language), isSubtle=True))
                actions = [_request_action(target, "card_ask_admin", language)]
            elif len(proposals) > MAX_TOOLS_SHOWN:
                body.append(_text(notice("card_too_many_tools", language, count=len(proposals))))
            else:
                shown = redact_known_secrets_for_display({"proposals": proposals})["proposals"]
                decision_body, actions = _decision_body(target, shown, language)
                body.extend(decision_body)
        else:
            question = run.human_input_question or ""
            body.append(_text(notice("card_question", language), weight="bolder"))
            body.append(_text(_clip(redact_known_secrets_for_display(question), _MAX_QUESTION)))
            if view.can_respond:
                answer_body, actions = _answer_body(target, question, language)
                body.extend(answer_body)
            else:
                body.append(_text(notice("card_waiting_admin", language), isSubtle=True))
                actions = [_request_action(target, "card_ask_admin", language)]
    else:
        responder = _last_responder(run)
        if responder:
            body.append(_runs(notice("card_answered_by", language), name=responder))
    return _card(target, body, actions, language)


# --- Reading a run as a viewer -------------------------------------------------


async def load_run_view(db: AsyncSession, run_id: uuid.UUID, user: User) -> RunView | None:
    """The run *user* may read, with what they may do on it; ``None`` when they may not.

    The read rule is the run detail page's (``GET /assignments/runs/{id}``):
    ``run_visible_filter`` for everyone but an administrator. The answer rule
    is the respond endpoint's, ``require_run_access``.
    """
    from app.api.v1.helpers import require_run_access, run_visible_filter

    # One round trip: every viewer of a pasted card refreshes it.
    query = (
        select(AssignmentRun, Agent.name, Assignment.name)
        .outerjoin(Agent, Agent.id == AssignmentRun.agent_id)
        .outerjoin(Assignment, Assignment.id == AssignmentRun.assignment_id)
        .where(AssignmentRun.id == run_id)
    )
    if not user.is_admin:
        query = query.where(run_visible_filter(user.id))
    row = (await db.execute(query)).one_or_none()
    if row is None:
        return None
    run, agent_name, assignment_name = row
    try:
        require_run_access(run, user, action="respond to")
        can_respond = True
    except HTTPException:
        can_respond = False
    return RunView(run=run, agent_name=agent_name, assignment_name=assignment_name, can_respond=can_respond)


async def viewer_card(db: AsyncSession, target: LinkTarget, user: User | None, language: str, *, banner: str | None = None) -> dict:
    """The card *user* sees for *target* (``None``: a Teams user the platform does not know)."""
    if user is None:
        return no_access_card(target, language, linked=False)
    view = await load_run_view(db, target.id, user)
    if view is None:
        return no_access_card(target, language, linked=True, banner=banner)
    return run_card(view, language, banner=banner)


# --- Acting from the card ----------------------------------------------------


def target_from_data(data: object) -> LinkTarget | None:
    """The target named by card data, which the client sends back and may have altered."""
    if not isinstance(data, dict) or data.get("kind") != "run":
        return None
    try:
        return LinkTarget(kind="run", id=uuid.UUID(str(data.get("id"))))
    except ValueError:
        return None


def _decisions(data: dict, pending: list[dict]) -> list[dict] | str:
    """The decision set the card submitted, or the notice key of why it is refused."""
    calls = data.get("calls")
    pending_ids = [str(p.get("tool_call_id") or "") for p in pending]
    if not isinstance(calls, list) or [str(c) for c in calls] != pending_ids:
        return "card_changed"
    choice_all = data.get("all")
    reason = _clip(data.get("reason"), _MAX_REASON) or None
    decisions = []
    for index, call_id in enumerate(pending_ids):
        verdict = choice_all if choice_all in ("approve", "reject") else data.get(f"d{index}")
        if verdict not in ("approve", "reject"):
            return "card_decide_every"
        decisions.append(
            {
                "tool_call_id": call_id,
                "decision": verdict,
                "rejection_reason": reason if verdict == "reject" else None,
            }
        )
    return decisions


async def _request(db: AsyncSession, target: LinkTarget, user: User, language: str) -> dict:
    """Ask whoever may answer the run to act on it; the card says the same whatever happened."""
    from app.services.access_requests import request_run_action

    try:
        await request_run_action(db, target.id, user, source="teams_card")
    except HTTPException as exc:
        logger.info("Teams card request on run %s refused: %s", target.id, exc.status_code)
        return await viewer_card(db, target, user, language, banner=notice("card_failed", language))
    return await viewer_card(db, target, user, language, banner=notice("card_request_sent", language))


async def _record_refused_action(db: AsyncSession, run_id: uuid.UUID, user: User) -> None:
    """Audit an answer from someone who may not even read the run, as the web does.

    The respond endpoint records a refusal for a run that exists and leaves no
    trace for an id that names nothing; the same here, so walking run ids from
    a card is as visible as walking them against the API.
    """
    from app.services.chat_approval_resume import record_refused_decision

    if await db.scalar(select(AssignmentRun.id).where(AssignmentRun.id == run_id)) is None:
        return
    await record_refused_decision(
        db,
        user_id=str(user.id),
        conversation_id=run_id,
        reason="forbidden",
        surface="teams_card",
        summary=f"Refused an approval decision on run {run_id}",
        entity_type="assignment_run_approval",
    )


def _refusal_key(exc: HTTPException) -> str:
    if exc.status_code in (403, 404):
        return "card_refused"
    if exc.status_code == 409:
        return "card_already_answered"
    return "card_failed"


async def act(db: AsyncSession, verb: str, data: dict, user: User | None, language: str) -> dict:
    """Carry out a card action as *user* and answer with their refreshed card.

    The decision itself is :func:`answer_paused_run`'s, which locks the run,
    applies ``require_run_access`` and records a refusal; this only turns what
    the card submitted into its request, and what it refused into a sentence.
    """
    from app.api.v1.assignments import answer_paused_run
    from app.api.v1.schemas.assignment import RespondToHumanInputRequest
    from app.services import decision_cards

    if decision_cards.is_decision_data(data):
        # A decision card of any source (#842, S9): one verb, answered through the router.
        return await decision_cards.act(db, verb, data, user, language)
    target = target_from_data(data)
    if target is None:
        raise ValueError("card data names no run")
    if verb == VERB_REFRESH or user is None:
        return await viewer_card(db, target, user, language)

    if verb == VERB_REQUEST:
        return await _request(db, target, user, language)
    if verb not in (VERB_DECIDE, VERB_ANSWER):
        raise ValueError("unknown card verb")
    view = await load_run_view(db, target.id, user)
    if view is None:
        await _record_refused_action(db, target.id, user)
        return no_access_card(target, language, linked=True)
    run = view.run

    if run.status == "awaiting_human" and (run.approval_decisions or run.human_input_response):
        # Answered already, waiting for the worker: say so rather than judge the click.
        return run_card(view, language, banner=notice("card_already_answered", language))

    if verb == VERB_DECIDE:
        pending = [p for p in (run.pending_approvals or []) if isinstance(p, dict)]
        if len(pending) > MAX_TOOLS_SHOWN:
            # No card shows these calls, so no card decides them, whatever its data says.
            return run_card(view, language)
        decided = _decisions(data, pending) if pending else "card_changed"
        if isinstance(decided, str):
            return run_card(view, language, banner=notice(decided, language))
        body = RespondToHumanInputRequest.model_validate({"decisions": decided})
    elif verb == VERB_ANSWER:
        if run.pending_approvals or data.get("q") != question_digest(run.human_input_question):
            return run_card(view, language, banner=notice("card_changed", language))
        answer = str(data.get("answer") or "")[:_MAX_ANSWER].strip()
        if not answer:
            return run_card(view, language, banner=notice("card_answer_empty", language))
        body = RespondToHumanInputRequest(response=answer)

    try:
        await answer_paused_run(db, target.id, user, body, surface="teams_card", answered_from="teams_card")
    except HTTPException as exc:
        logger.info("Teams card answer on run %s refused: %s %r", target.id, exc.status_code, _clip(exc.detail, 200))
        await db.rollback()
        # The rollback expired every row of the session, the viewer's included.
        await db.refresh(user)
        return await viewer_card(db, target, user, language, banner=notice(_refusal_key(exc), language))
    await db.commit()
    return await viewer_card(db, target, user, language, banner=notice("card_answer_sent", language))
