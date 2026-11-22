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

"""#842 S9b slice 4: decision answer links by email (human-gates.md I.4, row "Email")."""

from __future__ import annotations

from typing import Any

import pytest

from app.services import decision_graph_cards as cards
from app.services import work_item_notify as notify
from tests.test_decision_graph_cards_842_s9b import (  # noqa: F401  (the autouse secret fixture)
    AGENT,
    ALICE,
    OWNER,
    _caller,
    _Db,
    _decision,
    _item,
    _secret,
    _tick,
    _TickDb,
)

EMAIL = {"channel": "email", "user": "alice@example.com", "integration": "Office account"}
SENT = "Email sent to alice@example.com. Subject: 'Merge the pull request now?'."


# --- the notify ask -------------------------------------------------------------------


def test_email_notify_ask_is_accepted_and_checked():
    payload = notify.normalize_notify_payload({"notify": dict(EMAIL)})
    assert payload["notify"] == {"channel": "email", "user": "alice@example.com", "integration": "Office account"}
    for bad in ("", "alice", "a@x.com, b@x.com", "a b@x.com"):
        with pytest.raises(notify.NotifyError):
            notify.normalize_notify_payload({"notify": {**EMAIL, "user": bad}})
    assert notify.notify_payload_for(integration="x", user="a@x.com", email=True)["channel"] == "email"
    assert "email address" in notify.target_label(EMAIL)


def test_end_of_item_email_uses_send_microsoft_email():
    tool, arguments = notify._tool_call(EMAIL, "The item is done.", subject="Ship the release")
    assert tool == "send_microsoft_email"
    assert arguments == {
        "to": "alice@example.com",
        "subject": "Ship the release",
        "body": "The item is done.",
        "content_type": "text",
    }
    # Only the tool's own success sentence counts as sent.
    assert notify._SENT[tool] in SENT


# --- the decision links ---------------------------------------------------------------


async def test_email_carries_one_answer_link_per_option_and_records_notified():
    db, seen = _Db(), []
    message_id = await cards.send_link_card(
        db,
        _decision(),
        agent_id=AGENT,
        user_id=OWNER,
        integration="Office account",
        recipient_id=ALICE,
        target={"email": "alice@example.com"},
        caller=_caller({"send_microsoft_email": (True, SENT)}, seen),
    )
    assert message_id == ""
    _, integration, tool, arguments = seen[0]
    assert (integration, tool) == ("Office account", "send_microsoft_email")
    assert arguments["to"] == "alice@example.com" and arguments["content_type"] == "text"
    assert arguments["subject"] == "Merge the pull request now?"
    links = [line for line in arguments["body"].splitlines() if "/decide/" in line]
    # One link per option, then the written-answer link (I.4).
    assert [line.split(":")[0] for line in links] == ["Merge", "Wait", "Answer with text"]
    event = db.added[0]
    assert (event.event, event.surface) == ("notified", "email")
    assert event.detail["sent"] is True and "message_id" not in event.detail
    # An email cannot be edited: it is not a card update_sent_cards PATCHes.
    assert await cards.has_sent_cards(_Db(notified=[event.detail]), event.decision_id) is False


async def test_refused_email_records_nothing():
    db, seen = _Db(), []
    with pytest.raises(notify.NotifyError):
        await cards.send_link_card(
            db,
            _decision(),
            agent_id=AGENT,
            user_id=OWNER,
            integration="x",
            recipient_id=ALICE,
            target={"email": "alice@example.com"},
            caller=_caller({"send_microsoft_email": (False, "Error: send guard refused")}, seen),
        )
    assert db.added == [] and db.commits == 0


def test_card_target_of_an_email_ask():
    assert cards.card_target(_item(EMAIL)) == {"email": "alice@example.com"}
    # A Teams ask naming a person by address still has no conversation for a card.
    assert cards.card_target(_item({"user": "a@example.com", "integration": "x"})) is None


def test_tick_emails_the_links_once(monkeypatch: pytest.MonkeyPatch):
    seen: list[Any] = []
    caller = _caller({"send_microsoft_email": (True, SENT)}, seen)
    db = _TickDb([(_decision(recipient=f"user:{ALICE}"), _item(EMAIL))])
    assert _tick(monkeypatch, db, caller) == 1
    assert seen[0][2] == "send_microsoft_email"
    again = _TickDb(
        [(_decision(recipient=f"user:{ALICE}"), _item(EMAIL))],
        notified=[{"via": cards.VIA, "email": "alice@example.com", "sent": True}],
    )
    assert _tick(monkeypatch, again, caller) == 0 and len(seen) == 1
