"""The Google Chat decision card of every human gate (#842, S11), without a database.

What the cardsV2 message shows for each source (run HITL, tool approval, flow
Ask-user, work item), that a ``CARD_CLICKED`` answers through the one router as
the clicker with surface ``google_chat_card``, that a non-recipient gets a
private notice and the card stays, that the decided state replaces the
buttons, that the webhook refuses a click it cannot verify, and that a message
posted as a user carries the signed answer links of S9b.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.services import decision_cards
from app.services import google_chat_decision_cards as cards
from app.services.channel_notices import notice

ROW_ID = uuid.UUID("5f0c2a1e-3b4d-4e5f-8a9b-0c1d2e3f4a5b")
SOURCE_ID = uuid.UUID("0b8e6f8c-2d5c-4c55-9a51-8a3c1d2e3f40")
RECIPIENT = uuid.UUID("11111111-1111-1111-1111-111111111111")
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
        "recipient": f"user:{RECIPIENT}",
        "status": "open",
        "answer": None,
        "answered_by": None,
        "answered_at": None,
        "deadline": None,
    }
    values.update(fields)
    row = SimpleNamespace(**values)
    row.public_id = f"{row.source_type}:{row.source_id}"
    return row


def _view(row=None, **fields) -> decision_cards.DecisionView:
    values = {
        "may_answer": None,
        "recipient_name": "Ada Lovelace",
        "agent_name": "Mail Triage",
        "page_path": f"/tasks/work-items/{SOURCE_ID}",
        **fields,
    }
    return decision_cards.DecisionView(decision=row or _row(), **values)


@pytest.fixture(autouse=True)
def _platform_address(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "frontend_url", "https://xtm.example.com/one")


def _widgets(message: dict) -> list[dict]:
    return message["cardsV2"][0]["card"]["sections"][0]["widgets"]


def _buttons(message: dict) -> list[dict]:
    return [b for w in _widgets(message) if "buttonList" in w for b in w["buttonList"]["buttons"]]


def _answer_buttons(message: dict) -> list[dict]:
    return [b for b in _buttons(message) if (b["onClick"].get("action") or {}).get("function") == cards.ACTION_ANSWER]


def _params(button: dict) -> dict[str, str]:
    return {p["key"]: p["value"] for p in button["onClick"]["action"]["parameters"]}


def _all_text(message: dict) -> str:
    return json.dumps(message["cardsV2"], ensure_ascii=False)


def test_one_button_per_option_the_recommendation_filled():
    message = cards.render_google_chat_decision(_view(), "en")

    buttons = _answer_buttons(message)
    assert [b["text"] for b in buttons] == ["Ship", "Wait"]
    assert ["color" in b for b in buttons] == [True, False]
    assert _params(buttons[0]) == {"id": f"work_item:{SOURCE_ID}", "d": str(ROW_ID), "option": "A"}
    inputs = [w["textInput"] for w in _widgets(message) if "textInput" in w]
    assert [i["name"] for i in inputs] == [cards.TEXT_INPUT]
    send = [b for b in _buttons(message) if (b["onClick"].get("action") or {}).get("function") == cards.ACTION_TEXT]
    assert [b["text"] for b in send] == [notice("card_send", "en")]
    urls = [b["onClick"]["openLink"]["url"] for b in _buttons(message) if "openLink" in b["onClick"]]
    assert urls == ["https://github.com/x/y/pull/1", f"https://xtm.example.com/one/tasks/work-items/{SOURCE_ID}"]
    assert message["text"] == "Ship the fix tonight?"
    header = message["cardsV2"][0]["card"]["header"]
    assert header["title"] == notice("card_header_decision", "en")
    assert "Mail Triage" in header["subtitle"]


def test_an_open_card_says_whom_it_waits_for_and_escapes_ask_text():
    body = _all_text(cards.render_google_chat_decision(_view(), "en"))
    assert "<b>Ada Lovelace</b>" in body
    assert "&lt;backend&gt;" in body
    assert "<backend>" not in body
    assert "2026-11-23 09:00 UTC" in body


def test_a_value_is_never_read_as_a_slot_or_markup():
    view = _view(recipient_name="{name} <i>x</i>")
    body = _all_text(cards.render_google_chat_decision(view, "en"))
    assert "<b>{name} &lt;i&gt;x&lt;/i&gt;</b>" in body


def test_a_tool_approval_has_no_text_input_and_catalog_labels():
    row = _row(kind="tool_approval", source_type="run", ask=_APPROVAL_ASK)
    message = cards.render_google_chat_decision(_view(row), "fr")
    labels = [b["text"] for b in _answer_buttons(message)]
    assert labels == [notice("card_approve", "fr"), notice("card_approve_always", "fr"), notice("card_reject", "fr")]
    assert not [w for w in _widgets(message) if "textInput" in w]


@pytest.mark.parametrize("kind", ["hitl", "flow_ask", "work_item"])
def test_every_source_draws_its_source_line(kind):
    message = cards.render_google_chat_decision(_view(_row(kind=kind)), "en")
    source = notice(decision_cards._SOURCE_NOTICE[kind], "en")
    assert source in message["cardsV2"][0]["card"]["header"]["subtitle"]


def test_the_decided_card_has_no_answer_button():
    row = _row(
        status="answered",
        answer={"option": "A"},
        answered_by=RECIPIENT,
        answered_at=datetime(2026, 11, 22, 8, 0, tzinfo=UTC),
    )
    message = cards.render_google_chat_decision(_view(row, answered_by_name="Ada Lovelace"), "en")
    assert _answer_buttons(message) == []
    assert not [w for w in _widgets(message) if "textInput" in w]
    body = _all_text(message)
    assert "<b>Ship</b>" in body
    assert "2026-11-22 08:00 UTC" in body


@pytest.mark.parametrize("status", ["expired", "cancelled"])
def test_a_closed_card_says_it_takes_no_answer(status):
    message = cards.render_google_chat_decision(_view(_row(status=status)), "en")
    key = "card_decision_expired" if status == "expired" else "card_decision_cancelled"
    assert notice(key, "en") in _all_text(message)
    assert _answer_buttons(message) == []


def _click(method: str, params: dict[str, str], text: str | None = None) -> dict:
    event: dict = {
        "type": "CARD_CLICKED",
        "user": {"name": "users/123", "email": "ada@example.com"},
        "action": {"actionMethodName": method, "parameters": [{"key": k, "value": v} for k, v in params.items()]},
    }
    if text is not None:
        event["common"] = {"formInputs": {cards.TEXT_INPUT: {"stringInputs": {"value": [text]}}}}
    return event


def test_parse_click_reads_both_event_shapes():
    params = {"id": f"work_item:{SOURCE_ID}", "d": str(ROW_ID), "option": "A"}
    assert cards.parse_click(_click(cards.ACTION_ANSWER, params)) == (cards.ACTION_ANSWER, params, None)
    common = {"common": {"invokedFunction": cards.ACTION_TEXT, "parameters": {"id": "x", "d": "y"}}}
    assert cards.parse_click(common) == (cards.ACTION_TEXT, {"id": "x", "d": "y"}, None)
    assert cards.parse_click(_click("other", params)) is None
    assert cards.parse_click(_click(cards.ACTION_ANSWER, {"id": "x"})) is None
    method, _, text = cards.parse_click(_click(cards.ACTION_TEXT, {"id": "x", "d": "y"}, text="Ship it Monday"))
    assert (method, text) == (cards.ACTION_TEXT, "Ship it Monday")


@pytest.fixture
def router(monkeypatch):
    state = {"row": _row()}

    async def _row_for(_db, data):
        return state["row"] if data.get("id") == state["row"].public_id else None

    async def _load_view(_db, row, _user, _language):
        return _view(row)

    answer = AsyncMock()
    monkeypatch.setattr(cards, "_row_for", _row_for)
    monkeypatch.setattr(cards, "load_view", _load_view)
    monkeypatch.setattr("app.services.decision_router.answer_decision", answer)
    return SimpleNamespace(state=state, answer=answer)


def _db():
    return SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock(), refresh=AsyncMock())


@pytest.mark.asyncio
async def test_a_click_answers_through_the_router_as_the_clicker(router):
    user = SimpleNamespace(id=RECIPIENT)
    value = {"id": f"work_item:{SOURCE_ID}", "d": str(ROW_ID)}
    message, private = await cards.answer(_db(), value, user, "en", option="A")
    router.answer.assert_awaited_once()
    args, kwargs = router.answer.await_args
    assert args[1:] == (f"work_item:{SOURCE_ID}", user)
    assert kwargs == {"option": "A", "text": None, "surface": "google_chat_card"}
    assert private is None and message is not None


@pytest.mark.asyncio
async def test_a_refused_click_leaves_the_card_and_tells_the_clicker_alone(router):
    router.answer.side_effect = HTTPException(status_code=403)
    db = _db()
    value = {"id": f"work_item:{SOURCE_ID}", "d": str(ROW_ID)}
    message, private = await cards.answer(db, value, SimpleNamespace(id=uuid.uuid4()), "en", option="A")
    assert message is None
    assert private == notice("card_decision_refused", "en")
    db.rollback.assert_awaited_once()
    response = cards.click_response(message, private, "users/999")
    assert response["privateMessageViewer"] == {"name": "users/999"}
    assert response["actionResponse"]["type"] == "NEW_MESSAGE"


@pytest.mark.asyncio
async def test_an_unknown_clicker_a_stale_card_and_an_empty_text_answer_nothing(router):
    value = {"id": f"work_item:{SOURCE_ID}", "d": str(ROW_ID)}
    assert await cards.answer(_db(), value, None, "en", option="A") == (None, notice("card_not_linked_decision", "en"))
    stale = {"id": f"work_item:{SOURCE_ID}", "d": str(uuid.uuid4())}
    assert await cards.answer(_db(), stale, SimpleNamespace(id=RECIPIENT), "en", option="A") == (
        None,
        notice("card_changed", "en"),
    )
    assert await cards.answer(_db(), value, SimpleNamespace(id=RECIPIENT), "en", text="  ") == (
        None,
        notice("card_answer_empty", "en"),
    )
    router.answer.assert_not_awaited()


def test_an_answer_updates_the_posted_message():
    message = {"text": "q", "cardsV2": []}
    assert cards.click_response(message, None, "users/1") == {"actionResponse": {"type": "UPDATE_MESSAGE"}, **message}
    assert cards.click_response(None, None, "users/1") == {}


def test_a_message_posted_as_a_user_carries_the_signed_answer_links(monkeypatch):
    from app.core.config import settings
    from app.services import decision_answer_links as links

    monkeypatch.setattr(settings, "secret_key", "test-secret")
    row = _row(id=ROW_ID)
    text = cards.link_message(row, RECIPIENT)
    marked = re.findall(r"<(https?://[^|>]+/decide/[^|>]+)\|([^>]+)>", text)
    assert len(marked) == 3
    token = marked[0][0].rsplit("/", 1)[1]
    link = links.read_token(token)
    assert (link.option, link.surface, link.recipient_id) == ("A", "google_chat", RECIPIENT)


def test_a_message_posted_as_a_user_shows_labels_never_raw_urls(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "secret_key", "test-secret")
    for language in ("en", "es", "fr", "ja"):
        text = cards.link_message(_row(id=ROW_ID), RECIPIENT, language)
        # Every /decide/ URL sits inside <url|label>: none is printed for a person to read.
        assert not re.search(r"(?<!<)https?://\S*/decide/", text)
        assert f"|{notice('card_answer_with_text', language)}>" in text


def test_a_link_label_cannot_close_the_markup():
    assert cards._link_label("Ship <now> | later") == "Ship \u2039now\u203a \u00a6 later"
    assert cards._link_label("  ") == "?"


def test_the_user_link_message_falls_back_to_a_translated_question(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "secret_key", "test-secret")
    row = _row(id=ROW_ID, ask={k: v for k, v in ASK.items() if k != "question"})
    text = cards.link_message(row, RECIPIENT, "fr")
    assert text.splitlines()[0] == notice("card_decision_waiting", "fr")


def test_the_answered_time_is_a_labelled_row_with_no_fixed_colour():
    row = _row(status="answered", answer={"option": "A"}, answered_at=datetime(2026, 11, 22, 8, 0, tzinfo=UTC))
    payload = cards.render_google_chat_decision(_view(row), "fr")
    assert "<font" not in json.dumps(payload)
    rows = [w["decoratedText"] for w in _widgets(payload) if "decoratedText" in w]
    assert {"topLabel": notice("card_label_answered_at", "fr"), "text": "2026-11-22 08:00 UTC"} in rows


@pytest.mark.asyncio
async def test_the_webhook_refuses_a_click_it_cannot_verify(monkeypatch):
    from starlette.requests import Request

    from app.channels import google_chat

    monkeypatch.setattr(google_chat, "_get_googlechat_creds", AsyncMock(return_value={"credentials_json": "{}"}))
    clicked = AsyncMock()
    monkeypatch.setattr(google_chat, "_decision_click", clicked)
    body = json.dumps({"type": "CARD_CLICKED"}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({"type": "http", "method": "POST", "headers": [], "path": "/"}, receive)
    response = await google_chat.googlechat_webhook(request)
    assert response.status_code == 401
    clicked.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_webhook_answers_a_verified_click(monkeypatch):
    from starlette.requests import Request

    from app.channels import google_chat

    creds = {"credentials_json": "{}", "project_number": "42"}
    monkeypatch.setattr(google_chat, "_get_googlechat_creds", AsyncMock(return_value=creds))
    monkeypatch.setattr(google_chat, "_verify_google_chat_jwt", lambda token, project: token == "good")
    monkeypatch.setattr(google_chat, "_decision_click", AsyncMock(return_value={"text": "ok"}))
    body = json.dumps({"type": "CARD_CLICKED"}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    headers = [(b"authorization", b"Bearer good")]
    request = Request({"type": "http", "method": "POST", "headers": headers, "path": "/"}, receive)
    response = await google_chat.googlechat_webhook(request)
    assert response.status_code == 200
    assert json.loads(response.body) == {"text": "ok"}
