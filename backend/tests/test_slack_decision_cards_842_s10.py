"""The Slack decision card of every human gate (#842, S10), without a database.

What the Block Kit message shows for each source (run HITL, tool approval,
flow Ask-user, work item), that a click answers through the one router as the
clicker with surface ``slack_card``, that a non-recipient is refused with an
ephemeral and the card stays, that the decided state replaces the buttons,
and that the interactivity endpoint is signing-secret checked and fails closed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.services import decision_cards
from app.services import slack_decision_cards as cards
from app.services.channel_notices import notice

ROW_ID = uuid.UUID("5f0c2a1e-3b4d-4e5f-8a9b-0c1d2e3f4a5b")
SOURCE_ID = uuid.UUID("0b8e6f8c-2d5c-4c55-9a51-8a3c1d2e3f40")
ASK = {
    "question": "Ship the fix tonight?",
    "context": "CI is green.\nThe change is <backend> only.",
    "options": [
        {"key": "A", "label": "Ship", "effect": "deploys at 22:00"},
        {"key": "B", "label": "Wait", "effect": "ships Monday"},
    ],
    "recommendation": "A",
    "deadline": "2026-11-23T09:00:00+00:00",
    "links": [{"label": "PR", "url": "https://github.com/x/y/pull/1"}],
}
_APPROVAL_ASK = {
    "question": "A person's input is needed",
    "options": [
        {"key": "approve", "label": "Approve"},
        {"key": "approve_always", "label": "Approve always"},
        {"key": "reject", "label": "Reject"},
    ],
}


def _row(**fields) -> SimpleNamespace:
    values = {
        "id": ROW_ID,
        "source_type": "work_item",
        "source_id": SOURCE_ID,
        "kind": "work_item",
        "ask": ASK,
        "recipient": "user:11111111-1111-1111-1111-111111111111",
        "status": "open",
        "answer": None,
        "answered_by": None,
        "answered_at": None,
    }
    values.update(fields)
    row = SimpleNamespace(**values)
    row.public_id = f"{row.source_type}:{row.source_id}"
    return row


def _view(row=None, *, may_answer=None, **fields) -> decision_cards.DecisionView:
    return decision_cards.DecisionView(
        decision=row or _row(),
        may_answer=may_answer,
        recipient_name="Ada Lovelace",
        agent_name="Mail Triage",
        page_path=f"/tasks/work-items/{SOURCE_ID}",
        **fields,
    )


@pytest.fixture(autouse=True)
def _platform_address(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "frontend_url", "https://xtm.example.com/one")


def _buttons(message: dict) -> list[dict]:
    return [e for b in message["blocks"] if b["type"] == "actions" for e in b["elements"]]


def _answer_buttons(message: dict) -> list[dict]:
    return [b for b in _buttons(message) if b.get("action_id", "").startswith(cards.ACTION_ANSWER + ":")]


def _all_text(message: dict) -> str:
    return json.dumps(message["blocks"], ensure_ascii=False)


def test_one_button_per_option_the_recommendation_primary():
    message = cards.render_slack_decision(_view(), "en")

    buttons = _answer_buttons(message)
    assert [b["text"]["text"] for b in buttons] == ["Ship", "Wait"]
    assert [b.get("style") for b in buttons] == ["primary", None]
    assert json.loads(buttons[0]["value"]) == {"id": f"work_item:{SOURCE_ID}", "d": str(ROW_ID), "option": "A"}
    text_button = [b for b in _buttons(message) if b.get("action_id") == cards.ACTION_TEXT]
    assert [b["text"]["text"] for b in text_button] == [notice("card_answer_with_text", "en")]
    urls = [b["url"] for b in _buttons(message) if "url" in b]
    assert urls == ["https://github.com/x/y/pull/1", f"https://xtm.example.com/one/tasks/work-items/{SOURCE_ID}"]
    assert message["blocks"][0]["type"] == "header"
    assert message["text"] == "Ship the fix tonight?"


def test_an_open_card_says_whom_it_waits_for_and_escapes_ask_text():
    message = cards.render_slack_decision(_view(), "en")
    body = _all_text(message)
    assert "*Ada Lovelace*" in body
    context = message["blocks"][3]["text"]
    assert context == {"type": "plain_text", "text": "CI is green.\nThe change is <backend> only.", "emoji": False}
    assert "<!date^" in body
    question = message["blocks"][2]
    assert question["text"]["text"] == "*Ship the fix tonight?*"


def test_bold_next_to_japanese_text_gets_a_word_boundary_and_english_stays_plain():
    """Slack reads *bold* at a word boundary only: the ja source line showed its asterisks (UX round 1 of #1086)."""
    ja = cards.render_slack_decision(_view(), "ja")["blocks"][1]["elements"][0]["text"]
    assert "\u200b*" in ja or "*\u200b" in ja
    en = cards.render_slack_decision(_view(), "en")["blocks"][1]["elements"][0]["text"]
    assert "\u200b" not in en


def test_a_value_holding_slot_text_is_substituted_once():
    """A name such as ``{agent}`` looped forever in ``_bold_runs`` (review of #1086 by davidloiret)."""
    line = cards._bold_runs("From {source} by {agent}", source="{agent}", agent="{source} {agent}")
    assert line["text"] == "From *{agent}* by *{source} {agent}*"


def test_bold_runs_keeps_an_unknown_slot_and_adds_word_boundaries():
    line = cards._bold_runs("a{name}b {other}", name="x")
    assert line["text"] == "a\u200b*x*\u200bb {other}"


def test_each_option_keeps_its_own_line():
    """The options list was folded onto one line by ``_clip`` (seen in the render lab, UX round 1 of #1086)."""
    message = cards.render_slack_decision(_view(), "en")
    options = next(b for b in message["blocks"] if b["type"] == "section" and "•" in b["text"]["text"])
    assert options["text"]["text"].split("\n") == [
        "• *Ship* (Recommended): deploys at 22:00",
        "• *Wait*: ships Monday",
    ]


@pytest.mark.parametrize(
    ("source_type", "kind", "ask", "source_notice"),
    [
        ("run", "hitl", ASK, "card_source_run"),
        ("run", "tool_approval", _APPROVAL_ASK, "card_source_tool_approval"),
        ("flow_run", "flow_ask", ASK, "card_source_flow"),
        ("work_item", "work_item", ASK, "card_source_work_item"),
    ],
)
def test_the_four_sources_produce_the_card(source_type, kind, ask, source_notice):
    row = _row(source_type=source_type, kind=kind, ask=ask)
    message = cards.render_slack_decision(_view(row), "fr")
    assert notice(source_notice, "fr") in _all_text(message)
    assert _answer_buttons(message)


@pytest.mark.parametrize("language", ["en", "es", "fr", "ja"])
def test_a_tool_approval_reads_its_options_in_the_viewer_language_and_has_no_text_answer(language):
    row = _row(source_type="run", kind="tool_approval", ask=_APPROVAL_ASK)
    message = cards.render_slack_decision(_view(row, proposals=({"tool_name": "send_email"},)), language)
    labels = [b["text"]["text"] for b in _answer_buttons(message)]
    assert labels == [notice(k, language) for k in ("card_approve", "card_approve_always", "card_reject")]
    assert not [b for b in _buttons(message) if b.get("action_id") == cards.ACTION_TEXT]
    assert "send_email" in _all_text(message)


def test_tool_arguments_never_reach_a_shared_slack_message():
    row = _row(source_type="run", kind="tool_approval", ask=_APPROVAL_ASK)
    proposals = ({"tool_name": "send_email", "arguments": {"to": "secret@example.com"}},)
    message = cards.render_slack_decision(_view(row, may_answer=True, proposals=proposals), "en")
    assert "secret@example.com" not in _all_text(message)


def test_a_decided_card_drops_the_buttons_and_says_who_chose_what():
    row = _row(
        status="answered",
        answer={"option": "B"},
        answered_by=uuid.uuid4(),
        answered_at=datetime(2026, 11, 22, 8, 0, tzinfo=UTC),
    )
    message = cards.render_slack_decision(_view(row, answered_by_name="Ada Lovelace"), "en")
    assert not _answer_buttons(message)
    assert "*Ada Lovelace* chose *Wait*." in _all_text(message)


def test_an_expired_card_says_so():
    message = cards.render_slack_decision(_view(_row(status="expired")), "en")
    assert notice("card_decision_expired", "en") in _all_text(message)
    assert not _answer_buttons(message)


def test_the_text_modal_pins_the_decision_and_the_message():
    modal = cards.text_modal(_view(), "en", channel="C1", ts="1.2")
    meta = json.loads(modal["private_metadata"])
    assert meta == {"id": f"work_item:{SOURCE_ID}", "d": str(ROW_ID), "channel": "C1", "ts": "1.2"}
    assert modal["callback_id"] == cards.MODAL_CALLBACK
    assert modal["blocks"][1]["block_id"] == cards.MODAL_BLOCK
    assert modal["blocks"][1]["element"]["action_id"] == cards.MODAL_INPUT


def test_parse_value_refuses_what_the_card_did_not_write():
    assert cards.parse_value("not json") is None
    assert cards.parse_value(json.dumps({"id": "x"})) is None
    assert cards.parse_value(json.dumps({"id": "run:1", "d": "2", "option": "A"})) == {
        "id": "run:1",
        "d": "2",
        "option": "A",
    }


# --- Answering ------------------------------------------------------------------


class _Db:
    def __init__(self):
        self.commit = AsyncMock()
        self.rollback = AsyncMock()


def _patch_rows(monkeypatch, *rows):
    queue = list(rows)

    async def _row_for(_db, _data):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(cards, "_row_for", _row_for)

    async def _load_view(_db, row, user, language):
        return _view(row, may_answer=user is not None, answered_by_name="Ada Lovelace")

    monkeypatch.setattr(cards, "load_view", _load_view)


VALUE = {"id": f"work_item:{SOURCE_ID}", "d": str(ROW_ID)}
USER = SimpleNamespace(id=uuid.uuid4(), is_active=True)


async def test_a_click_answers_through_the_router_as_the_clicker(monkeypatch):
    from app.services import decision_router

    decided = _row(status="answered", answer={"option": "A"}, answered_at=datetime(2026, 11, 22, tzinfo=UTC))
    _patch_rows(monkeypatch, _row(), decided)
    answer = AsyncMock()
    monkeypatch.setattr(decision_router, "answer_decision", answer)
    db = _Db()

    message, ephemeral = await cards.answer(db, VALUE, USER, "en", option="A")

    answer.assert_awaited_once_with(db, f"work_item:{SOURCE_ID}", USER, option="A", text=None, surface="slack_card")
    db.commit.assert_awaited_once()
    assert ephemeral is None
    assert message is not None and not _answer_buttons(message)


async def test_a_non_recipient_is_refused_with_an_ephemeral_and_the_card_unchanged(monkeypatch):
    from app.services import decision_router

    _patch_rows(monkeypatch, _row())
    monkeypatch.setattr(decision_router, "answer_decision", AsyncMock(side_effect=HTTPException(403, "no")))
    db = _Db()

    message, ephemeral = await cards.answer(db, VALUE, USER, "en", option="A")

    assert message is None
    assert ephemeral == notice("card_decision_refused", "en")
    db.rollback.assert_awaited_once()


async def test_a_second_click_answers_nothing(monkeypatch):
    from app.services import decision_router

    _patch_rows(monkeypatch, _row(status="answered", answer={"option": "A"}))
    answer = AsyncMock()
    monkeypatch.setattr(decision_router, "answer_decision", answer)

    message, ephemeral = await cards.answer(_Db(), VALUE, USER, "en", option="B")

    answer.assert_not_awaited()
    assert ephemeral == notice("card_decision_already_answered", "en")
    assert message is not None and not _answer_buttons(message)


async def test_a_card_from_before_a_re_ask_answers_nothing(monkeypatch):
    from app.services import decision_router

    _patch_rows(monkeypatch, _row(id=uuid.uuid4()))
    answer = AsyncMock()
    monkeypatch.setattr(decision_router, "answer_decision", answer)

    message, ephemeral = await cards.answer(_Db(), VALUE, USER, "en", option="A")

    answer.assert_not_awaited()
    assert (message, ephemeral) == (None, notice("card_changed", "en"))


async def test_an_unlinked_slack_member_is_told_how_to_answer(monkeypatch):
    _patch_rows(monkeypatch, _row())
    message, ephemeral = await cards.answer(_Db(), VALUE, None, "en", option="A")
    assert (message, ephemeral) == (None, notice("card_not_linked_decision", "en"))


async def test_an_empty_text_answer_is_refused(monkeypatch):
    _patch_rows(monkeypatch, _row())
    message, ephemeral = await cards.answer(_Db(), VALUE, USER, "en", text="   ")
    assert (message, ephemeral) == (None, notice("card_answer_empty", "en"))


# --- The interactivity endpoint -------------------------------------------------

SECRET = "slack-signing-secret"


def _signed_request(payload: dict, *, secret: str = SECRET) -> Request:
    body = urlencode({"payload": json.dumps(payload)}).encode()
    ts = str(int(time.time()))
    digest = hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    headers = [
        (b"x-slack-signature", f"v0={digest}".encode()),
        (b"x-slack-request-timestamp", ts.encode()),
        (b"content-type", b"application/x-www-form-urlencoded"),
    ]

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/api/channels/slack/interactions", "headers": headers}
    return Request(scope, receive)


def _click(option: str = "A") -> dict:
    return {
        "type": "block_actions",
        "user": {"id": "U0ADA"},
        "trigger_id": "T1",
        "channel": {"id": "C0SHARED"},
        "container": {"message_ts": "1700000000.000100"},
        "actions": [{"action_id": f"{cards.ACTION_ANSWER}:{option}", "value": json.dumps({**VALUE, "option": option})}],
    }


@pytest.fixture
def slack(monkeypatch):
    from app.channels import slack as slack_module

    spawned: list = []

    def _spawn(coro, *, name=None):
        spawned.append((name, coro))
        coro.close()

    monkeypatch.setattr(slack_module, "spawn_channel_task", _spawn)
    monkeypatch.setattr(slack_module, "_get_slack_bot_creds", AsyncMock(return_value=("xoxb-token", SECRET)))
    return slack_module, spawned


async def test_a_signed_click_is_dispatched_as_an_answer(slack):
    module, spawned = slack
    assert await module.slack_interactions(_signed_request(_click())) == {"ok": True}
    assert [name for name, _ in spawned] == ["slack-decision-answer"]


async def test_a_bad_signature_is_refused_and_nothing_runs(slack):
    module, spawned = slack
    with pytest.raises(HTTPException) as caught:
        await module.slack_interactions(_signed_request(_click(), secret="wrong"))
    assert caught.value.status_code == 401
    assert spawned == []


async def test_no_signing_secret_fails_closed(slack, monkeypatch):
    module, spawned = slack
    monkeypatch.setattr(module, "_get_slack_bot_creds", AsyncMock(return_value=("xoxb-token", "")))
    with pytest.raises(HTTPException) as caught:
        await module.slack_interactions(_signed_request(_click()))
    assert caught.value.status_code == 401
    assert spawned == []


async def test_answer_with_text_opens_the_modal_and_its_submission_answers(slack):
    module, spawned = slack
    click = _click()
    click["actions"] = [{"action_id": cards.ACTION_TEXT, "value": json.dumps(VALUE)}]
    await module.slack_interactions(_signed_request(click))
    submission = {
        "type": "view_submission",
        "user": {"id": "U0ADA"},
        "view": {
            "callback_id": cards.MODAL_CALLBACK,
            "private_metadata": json.dumps({**VALUE, "channel": "C0SHARED", "ts": "1.2"}),
            "state": {"values": {cards.MODAL_BLOCK: {cards.MODAL_INPUT: {"value": "Ship after the demo"}}}},
        },
    }
    assert await module.slack_interactions(_signed_request(submission)) == {}
    assert [name for name, _ in spawned] == ["slack-decision-modal", "slack-decision-text"]


async def test_a_link_button_answers_nothing(slack):
    module, spawned = slack
    click = _click()
    click["actions"] = [{"action_id": "xtm.decision.open"}]
    assert await module.slack_interactions(_signed_request(click)) == {"ok": True}
    assert spawned == []


async def test_settle_click_updates_the_card_or_tells_the_clicker(monkeypatch):
    from app.channels import slack as module

    class _Session:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr("app.core.database.async_session", lambda: _Session())
    monkeypatch.setattr(module, "_interaction_user", AsyncMock(return_value=USER))
    calls = []

    async def _api(_token, method, body):
        calls.append((method, body))

    monkeypatch.setattr(module, "_slack_api", _api)
    monkeypatch.setattr(cards, "answer", AsyncMock(return_value=(None, "refused")))
    await module._settle_click("t", value=VALUE, slack_user_id="U0BOB", channel="C1", ts="1.2", option="A")
    assert calls == [("chat.postEphemeral", {"channel": "C1", "user": "U0BOB", "text": "refused"})]

    calls.clear()
    monkeypatch.setattr(cards, "answer", AsyncMock(return_value=({"text": "q", "blocks": []}, None)))
    await module._settle_click("t", value=VALUE, slack_user_id="U0ADA", channel="C1", ts="1.2", option="A")
    assert calls == [("chat.update", {"channel": "C1", "ts": "1.2", "text": "q", "blocks": []})]
