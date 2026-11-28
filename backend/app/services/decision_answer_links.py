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

"""Signed answer links of a decision (#842, S9b; human-gates.md I.4).

A message posted as a user, a service account or an agent account (Graph
chat and channel tools), a Google Chat message sent as a user and an email
cannot carry an interactive card: Teams runs only ``Action.OpenUrl`` there.
Each option is then one link ``/decide/<token>``: the token names the
decision row, the option (or ``text`` for a free answer), the ONE recipient
it was minted for, the channel it is sent on and an expiry (the decision's
deadline, else 7 days), and is signed with an HMAC of the platform secret.

The link is never a bearer credential alone: opening it needs the
recipient's session (:func:`check_reader`), the engine's own recipient rule
still applies when it answers (:mod:`app.services.decision_router`), and it
answers once: a decision no longer open shows its decided state and answers
nothing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from app.core.config import settings
from app.core.security import timing_safe_str_eq
from app.models.decision import Decision

#: Where an answer link is sent; recorded as the answer's ``surface``.
LINK_SURFACES: frozenset[str] = frozenset({"teams_chat", "teams_channel", "google_chat", "email"})
#: The option slot of a link that opens the card with its text field.
TEXT_OPTION = "text"
#: The expiry of a link on a decision without a deadline.
DEFAULT_TTL_SECONDS = 7 * 24 * 3600
#: The page a link opens (the frontend route ``/decide/:token``).
LINK_ROUTE = "/decide/{token}"
_CONTEXT = b"xtm-one decision answer link v1"


@dataclass(frozen=True)
class AnswerLink:
    """What a verified token says."""

    decision_id: uuid.UUID
    option: str
    recipient_id: uuid.UUID
    surface: str
    expires: int


def _key(secret: str) -> bytes:
    # Derived per use, so the link key is never the session signing key itself.
    return hmac.new(secret.encode(), _CONTEXT, hashlib.sha256).digest()


def _secrets() -> list[str]:
    """The current platform secret first, then the previous ones (key rotation)."""
    keys = [settings.secret_key]
    keys += [k.strip() for k in (settings.decision_link_previous_secrets or "").split(",") if k.strip()]
    return [k for k in keys if k]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: str, secret: str) -> str:
    return _b64(hmac.new(_key(secret), body.encode(), hashlib.sha256).digest())


def expiry_for(deadline: datetime | None, now: float | None = None) -> int:
    """The link's expiry: the decision's deadline, else 7 days from now."""
    if deadline is not None:
        return int(deadline.timestamp())
    return int((now if now is not None else time.time()) + DEFAULT_TTL_SECONDS)


def mint_token(decision_id: uuid.UUID, option: str, recipient_id: uuid.UUID, surface: str, expires: int) -> str:
    """One signed token for one option of one decision, for one recipient."""
    if surface not in LINK_SURFACES:
        raise ValueError(f"unknown answer link surface {surface!r}")
    secrets = _secrets()
    if not secrets:
        raise ValueError("SECRET_KEY is not set: answer links cannot be signed")
    payload = {"d": str(decision_id), "o": option, "r": str(recipient_id), "s": surface, "e": int(expires)}
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{body}.{_sign(body, secrets[0])}"


def read_token(token: str, now: float | None = None) -> AnswerLink:
    """The link a token names; 404 when unsigned or malformed, 410 when expired."""
    body, sep, signature = (token or "").partition(".")
    if not sep or not body or not signature or len(token) > 1024:
        raise HTTPException(status_code=404, detail="This answer link is not valid.")
    if not any(timing_safe_str_eq(signature, _sign(body, s)) for s in _secrets()):
        raise HTTPException(status_code=404, detail="This answer link is not valid.")
    try:
        data = json.loads(_unb64(body))
        link = AnswerLink(
            decision_id=uuid.UUID(str(data["d"])),
            option=str(data["o"]),
            recipient_id=uuid.UUID(str(data["r"])),
            surface=str(data["s"]),
            expires=int(data["e"]),
        )
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=404, detail="This answer link is not valid.") from None
    if (now if now is not None else time.time()) > link.expires:
        raise HTTPException(status_code=410, detail="This answer link has expired.")
    return link


def check_reader(link: AnswerLink, user: Any) -> None:
    """Only the person the link was minted for opens it: a forwarded link answers nothing."""
    if getattr(user, "id", None) != link.recipient_id:
        raise HTTPException(status_code=403, detail="This answer link was sent to someone else.")


def option_label(ask: dict[str, Any] | None, key: str) -> str:
    """The label of option *key* of the ask (the key itself when it has none)."""
    for option in (ask or {}).get("options") or []:
        if str(option.get("key", "")).casefold() == key.casefold():
            return str(option.get("label") or option.get("key"))
    return key


def answer_links(
    decision: Decision, recipient_id: uuid.UUID, surface: str, language: str | None = None
) -> list[dict[str, str]]:
    """One ``{key, label, url}`` per option, then the free-text link (labelled in *language*), for one recipient."""
    from app.services.channel_notices import notice

    base = settings.frontend_url.rstrip("/")
    expires = expiry_for(decision.deadline)
    options = [o for o in (decision.ask or {}).get("options") or [] if o.get("key")]
    rows = [(str(o["key"]), str(o.get("label") or o["key"])) for o in options]
    rows.append((TEXT_OPTION, notice("card_answer_with_text", language)))
    return [
        {
            "key": key,
            "label": label,
            "url": base + LINK_ROUTE.format(token=mint_token(decision.id, key, recipient_id, surface, expires)),
        }
        for key, label in rows
    ]


def _source_url(decision: Decision) -> str | None:
    """The page of the decision's source in XTM One, absolute; ``None`` when the row names none."""
    from app.services.decision_links import decision_link

    source_type = getattr(decision, "source_type", None)
    source_id = getattr(decision, "source_id", None)
    if not source_type or not source_id:
        return None
    try:
        route = decision_link(str(source_type), source_id)
    except ValueError:
        return None
    return settings.frontend_url.rstrip("/") + route


def _option_row(option: dict[str, Any], recommended: bool, language: str | None) -> dict[str, Any]:
    """One option of the link card as the web card shows it: label, effect, the recommended one highlighted."""
    from app.services.channel_notices import notice
    from app.services.decision_ask import without_recommended_mark

    items: list[dict[str, Any]] = []
    if recommended:
        items.append(
            {
                "type": "TextBlock",
                "text": notice("card_recommended", language).upper(),
                "size": "Small",
                "weight": "Bolder",
                "color": "Good",
                "spacing": "None",
            }
        )
    items.append(
        {
            "type": "TextBlock",
            "text": f"**{option.get('key')}.** {without_recommended_mark(option.get('label')) or option.get('key')}"[:300],
            "wrap": True,
            "spacing": "None",
        }
    )
    if option.get("effect"):
        items.append(
            {
                "type": "TextBlock",
                "text": without_recommended_mark(option["effect"])[:600],
                "wrap": True,
                "isSubtle": True,
                "spacing": "None",
            }
        )
    return {
        "type": "Container",
        "style": "good" if recommended else "emphasis",
        "spacing": "Small",
        "items": items,
    }


def link_card(
    decision: Decision,
    recipient_id: uuid.UUID,
    surface: str,
    language: str | None = None,
    kicker: str | None = None,
) -> dict[str, Any]:
    """The Adaptive Card a Graph message posted as a user carries: ``Action.OpenUrl`` only.

    The layout of the in-app ``DecisionCard``: the kicker (where the decision
    comes from), the question, the context, one row per option (label and
    effect, the recommended one highlighted), the deadline, then one button
    per option, "Answer with text", one button per object of the ask (a pull
    request, a ticket: :func:`decision_ask.card_links`, #1177) and "Open in XTM
    One". Each option button
    answers it in one click from the recipient's session. Its words are in
    *language* (en, fr, es, ja). ``providers/microsoft/_cards.py``
    ``parse_card`` accepts it as is.
    """
    from app.services.channel_notices import notice

    ask = decision.ask or {}
    body: list[dict[str, Any]] = []
    if kicker:
        body.append({"type": "TextBlock", "text": kicker[:120], "size": "Small", "weight": "Bolder", "isSubtle": True})
    body.append(
        {
            "type": "TextBlock",
            "text": _question(ask, language),
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
            "spacing": "None" if kicker else "Default",
        }
    )
    if ask.get("context"):
        body.append({"type": "TextBlock", "text": str(ask["context"])[:1500], "wrap": True, "isSubtle": True})
    recommended = str(ask.get("recommendation") or "")
    for option in ask.get("options") or []:
        if option.get("key"):
            body.append(_option_row(option, bool(recommended) and str(option.get("key")) == recommended, language))
    if decision.deadline is not None:
        body.append(
            {
                "type": "TextBlock",
                "text": f"{notice('card_label_deadline', language)}: {decision.deadline:%Y-%m-%d %H:%M} UTC",
                "isSubtle": True,
                "wrap": True,
            }
        )
    actions = [
        {
            "type": "Action.OpenUrl",
            "title": link["label"][:40],
            "url": link["url"],
            **({"style": "positive"} if recommended and link["key"] == recommended else {}),
        }
        for link in answer_links(decision, recipient_id, surface, language)
    ]
    actions.extend(_object_actions(decision, language))
    if (source := _source_url(decision)) is not None:
        actions.append({"type": "Action.OpenUrl", "title": notice("card_open", language), "url": source})
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
        "actions": actions,
    }


def _object_actions(decision: Decision, language: str | None) -> list[dict[str, Any]]:
    """One ``Action.OpenUrl`` per object of the ask (a pull request, a ticket), at most four (#1177)."""
    from app.services.decision_ask import card_links

    return [
        {"type": "Action.OpenUrl", "title": link["label"][:40], "url": link["url"]}
        for link in card_links(decision.ask, language)
    ]


def _question(ask: dict[str, Any], language: str | None) -> str:
    """The ask's question, else the catalog's "a decision is waiting" line in *language*."""
    from app.services.channel_notices import notice

    return str(ask.get("question") or notice("card_decision_waiting", language))


def question_lines(decision: Decision, language: str | None = None) -> list[str]:
    """The lines above the links of a text form: the question, the context, then a blank line."""
    ask = decision.ask or {}
    lines = [_question(ask, language)]
    if ask.get("context"):
        lines += ["", str(ask["context"])]
    lines.append("")
    return lines


def link_text(decision: Decision, recipient_id: uuid.UUID, surface: str, language: str | None = None) -> str:
    """The plain-text form (email): the question and one link per option, in *language*."""
    from app.services.decision_ask import card_links

    lines = question_lines(decision, language)
    for link in answer_links(decision, recipient_id, surface, language):
        lines.append(f"{link['label']}: {link['url']}")
    # The objects of the ask (a pull request, a ticket) after the answers (#1177).
    for link in card_links(decision.ask, language):
        lines.append(f"{link['label']}: {link['url']}")
    return "\n".join(lines)


def decided_card(decision: Decision, answered_by_name: str | None, language: str | None = None) -> dict[str, Any]:
    """The card a sent link card is replaced with once decided: who, which option, when.

    No answer button; the buttons of the objects of the ask (a pull request, a
    ticket) stay, so the person still reaches what was decided on (#1177).
    """
    ask = decision.ask or {}
    state = decided_state(decision, answered_by_name)
    if decision.status == "answered":
        chosen = state["label"] or (state["text"] and "a written answer") or "an answer"
        line = f"Answered: {chosen}"
        if state["answered_by"]:
            line += f", by {state['answered_by']}"
        if decision.answered_at is not None:
            line += f", {decision.answered_at:%Y-%m-%d %H:%M} UTC"
    else:
        line = f"This decision is {decision.status}: it takes no answer."
    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": str(ask.get("question") or "A decision"),
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
        },
        {"type": "TextBlock", "text": line[:600], "wrap": True},
    ]
    if state["text"]:
        body.append({"type": "TextBlock", "text": str(state["text"])[:1500], "wrap": True, "isSubtle": True})
    card: dict[str, Any] = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
    }
    actions = _object_actions(decision, language)
    if actions:
        card["actions"] = actions
    return card


def decided_state(decision: Decision, answered_by_name: str | None) -> dict[str, Any]:
    """What a decided link page and an updated card show: who, which option, when."""
    answer = decision.answer or {}
    option = str(answer.get("option") or "")
    return {
        "status": decision.status,
        "option": option or None,
        "label": option_label(decision.ask, option) if option else None,
        "text": answer.get("text"),
        "answered_by": answered_by_name,
        "answered_at": decision.answered_at.isoformat() if decision.answered_at else None,
        "surface": decision.surface,
    }
