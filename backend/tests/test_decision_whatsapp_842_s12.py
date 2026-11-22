"""The WhatsApp decision message of every human gate (#842, S12), without a database.

Reply buttons for up to three options, a list up to ten, text with the link
beyond; a reply maps to the decision by its payload id and answers through
the one router as the linked account with surface ``whatsapp_card``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.channels import whatsapp as channel
from app.services import decision_router
from app.services import decision_whatsapp as wa
from app.services.channel_notices import notice

ROW_ID = uuid.UUID("5f0c2a1e-3b4d-4e5f-8a9b-0c1d2e3f4a5b")
SOURCE_ID = uuid.UUID("0b8e6f8c-2d5c-4c55-9a51-8a3c1d2e3f40")


def _ask(n: int = 2, **extra) -> dict:
    return {
        "question": "Ship the fix tonight?",
        "context": "CI is green.",
        "options": [{"key": chr(65 + i), "label": f"Option {i}", "effect": f"effect {i}"} for i in range(n)],
        "recommendation": "A",
        **extra,
    }


def _row(**fields) -> SimpleNamespace:
    values = {
        "id": ROW_ID,
        "source_type": "work_item",
        "source_id": SOURCE_ID,
        "kind": "work_item",
        "ask": _ask(),
        "status": "open",
    }
    values.update(fields)
    row = SimpleNamespace(**values)
    row.public_id = f"{row.source_type}:{row.source_id}"
    return row


def _view(row=None, **fields) -> SimpleNamespace:
    values = {
        "decision": row or _row(),
        "may_answer": None,
        "recipient_name": "Ada",
        "answered_by_name": None,
        "agent_name": "Mail Triage",
        "page_path": f"/tasks/work-items/{SOURCE_ID}",
        "proposals": (),
    }
    values.update(fields)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def _platform_address(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "frontend_url", "https://xtm.example.com")


def test_up_to_three_options_are_reply_buttons_carrying_the_row_and_key():
    message = wa.render_whatsapp_decision(_view(_row(ask=_ask(3))), "en")
    assert message["type"] == "button"
    buttons = message["action"]["buttons"]
    assert [b["reply"]["id"] for b in buttons] == [f"xtm.d:{ROW_ID}:{k}" for k in "ABC"]
    assert all(len(b["reply"]["title"]) <= 20 for b in buttons)
    body = message["body"]["text"]
    assert body.startswith("Ship the fix tonight?")
    assert "(Recommended)" in body
    assert f"https://xtm.example.com/tasks/work-items/{SOURCE_ID}" in body
    assert len(body) <= 1024


def test_four_to_ten_options_are_a_list():
    message = wa.render_whatsapp_decision(_view(_row(ask=_ask(7))), "fr")
    assert message["type"] == "list"
    rows = message["action"]["sections"][0]["rows"]
    assert len(rows) == 7
    assert rows[0]["description"] == "effect 0"
    assert message["action"]["button"] == notice("card_choose", "fr")


@pytest.mark.parametrize(
    "row",
    [
        _row(ask=_ask(11)),
        _row(ask={"question": "Why?", "options": []}),
    ],
)
def test_what_buttons_cannot_carry_is_text_with_the_link(row):
    view = _view(row)
    assert wa.render_whatsapp_decision(view, "en") is None
    text = wa.decision_text(view, "en")
    assert text.startswith("Ship" if row.ask.get("options") else "Why?")
    assert notice("card_decide_in_xtm", "en") in text
    assert str(SOURCE_ID) in text


def test_an_answered_decision_states_its_outcome_and_never_asks_again():
    row = _row(
        status="answered",
        answer={"option": "B", "text": "Tonight is fine."},
        ask=_ask(2, deadline="2026-11-23T09:00:00Z"),
    )
    view = _view(row, answered_by_name="Ada Lovelace")
    assert wa.render_whatsapp_decision(view, "en") is None
    text = wa.decision_text(view, "en")
    assert notice("card_decided_option", "en", name="Ada Lovelace", option="Option 1") in text
    assert "Tonight is fine." in text
    assert notice("card_decide_in_xtm", "en") not in text
    assert "(Recommended)" not in text
    assert notice("card_label_deadline", "en") not in text
    assert text.endswith(f"https://xtm.example.com/tasks/work-items/{SOURCE_ID}")


@pytest.mark.parametrize(
    ("status", "key"), [("expired", "card_decision_expired"), ("cancelled", "card_decision_cancelled")]
)
def test_a_closed_unanswered_decision_says_so(status, key):
    view = _view(_row(status=status))
    text = wa.decision_text(view, "fr")
    assert notice(key, "fr") in text
    assert notice("card_decide_in_xtm", "fr") not in text
    assert str(SOURCE_ID) in text


def test_an_answered_tool_approval_states_the_verdicts():
    ask = {"question": "Run it?", "options": [{"key": k, "label": k} for k in ("approve", "reject")]}
    row = _row(kind="tool_approval", ask=ask, status="answered", answer={"decisions": {"c1": "approve"}})
    text = wa.decision_text(_view(row, answered_by_name="Ada"), "en")
    assert notice("card_decided_option", "en", name="Ada", option=notice("card_approve", "en")) in text
    assert notice("card_decide_in_xtm", "en") not in text


def test_an_answered_decision_says_when_it_was_answered():
    from datetime import UTC, datetime

    row = _row(status="answered", answer={"option": "A"}, answered_at=datetime(2026, 11, 22, 8, 41, tzinfo=UTC))
    text = wa.decision_text(_view(row, answered_by_name="Ada"), "en")
    lines = text.splitlines()
    assert "2026-11-22 08:41:00 UTC" in lines
    assert lines[-1].endswith(str(SOURCE_ID))


def test_buttons_whose_clipped_titles_collide_become_a_list():
    labels = ["Escalate to on-call now", "Escalate to on-call later"]
    ask = _ask(2)
    ask["options"] = [{"key": k, "label": label} for k, label in zip("AB", labels, strict=True)]
    message = wa.render_whatsapp_decision(_view(_row(ask=ask)), "en")
    assert message["type"] == "list"
    titles = [r["title"] for r in message["action"]["sections"][0]["rows"]]
    assert len(set(titles)) == 2


def test_rows_that_still_collide_are_text_with_the_link():
    ask = _ask(2)
    ask["options"] = [{"key": k, "label": "Escalate to the on-call security engineer " + k} for k in "AB"]
    view = _view(_row(ask=ask))
    assert wa.render_whatsapp_decision(view, "en") is None
    assert str(SOURCE_ID) in wa.decision_text(view, "en")


def test_a_long_body_keeps_the_link_line_whole():
    row = _row(ask=_ask(2, context="x " * 2000))
    body = wa.render_whatsapp_decision(_view(row), "en")["body"]["text"]
    assert len(body) <= 1024
    assert body.endswith(f"https://xtm.example.com/tasks/work-items/{SOURCE_ID}")


def test_tool_approval_buttons_use_the_catalog_labels():
    ask = {"question": "Run it?", "options": [{"key": k, "label": k} for k in ("approve", "approve_always", "reject")]}
    message = wa.render_whatsapp_decision(_view(_row(kind="tool_approval", ask=ask)), "en")
    titles = [b["reply"]["title"] for b in message["action"]["buttons"]]
    assert titles[0] == notice("card_approve", "en")[:20]


def _tool_approval(calls: int, **fields) -> SimpleNamespace:
    from app.models.decision_hooks import run_ask

    _kind, ask = run_ask("Release Manager wants to deploy.", [], [{"id": "c1"}])
    proposals = tuple(
        {"tool_name": f"deploy_service_{i}", "arguments": {"service": "auth-api", "token_hint": f"v{i}"}}
        for i in range(calls)
    )
    return _view(_row(kind="tool_approval", ask=ask, **fields), page_path=f"/runs/{SOURCE_ID}", proposals=proposals)


@pytest.mark.parametrize("language", ["en", "fr"])
def test_a_tool_approval_names_every_tool_it_approves(language):
    """UX round 3, B1: the message lists the calls as the Teams card does (S9)."""
    view = _tool_approval(3)
    message = wa.render_whatsapp_decision(view, language)
    assert message["type"] == "button"
    body = message["body"]["text"]
    assert notice("card_tools_awaiting", language) in body
    for i in range(3):
        assert f"`deploy_service_{i}`" in body
    # The send tool has no reader: names only, never the arguments.
    assert "auth-api" not in body


def test_a_decider_reads_the_arguments_of_each_call():
    view = _tool_approval(1)
    view.may_answer = True
    body = wa.render_whatsapp_decision(view, "en")["body"]["text"]
    assert "service: auth-api" in body


def test_a_tool_approval_of_more_calls_than_a_card_lists_is_counted_and_decided_in_xtm_one():
    from app.services.teams_link_cards import MAX_TOOLS_SHOWN

    view = _tool_approval(MAX_TOOLS_SHOWN + 1)
    assert wa.render_whatsapp_decision(view, "en") is None
    text = wa.decision_text(view, "en")
    assert notice("card_too_many_tools", "en", count=MAX_TOOLS_SHOWN + 1) in text
    assert notice("card_decide_in_xtm", "en") not in text
    assert text.endswith(f"https://xtm.example.com/runs/{SOURCE_ID}")


def test_a_tool_approval_too_long_for_the_body_drops_arguments_then_buttons():
    view = _tool_approval(10)
    view.may_answer = True
    view.proposals = tuple({**p, "arguments": {"blob": "x" * 200}} for p in view.proposals)
    message = wa.render_whatsapp_decision(view, "en")
    body = message["body"]["text"]
    assert len(body) <= 1024
    assert "xxxx" not in body
    assert all(f"`deploy_service_{i}`" in body for i in range(10))
    view.decision.ask = {**view.decision.ask, "question": "q " * 300, "context": "y " * 600}
    assert wa.render_whatsapp_decision(view, "en") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"xtm.d:{ROW_ID}:A", (ROW_ID, "A")),
        (f"xtm.d:{ROW_ID}:approve_always", (ROW_ID, "approve_always")),
        ("xtm.d:not-a-uuid:A", None),
        (f"xtm.d:{ROW_ID}:", None),
        ("A", None),
        (None, None),
    ],
)
def test_reply_ids_are_parsed_strictly(raw, expected):
    assert wa.parse_reply_id(raw) == expected


def test_only_a_one_to_one_interactive_reply_is_a_decision_reply():
    reply = {
        "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": f"xtm.d:{ROW_ID}:A"}},
    }
    assert channel._decision_reply_id(reply) == f"xtm.d:{ROW_ID}:A"
    listed = {"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": f"xtm.d:{ROW_ID}:B"}}}
    assert channel._decision_reply_id(listed) == f"xtm.d:{ROW_ID}:B"
    assert channel._decision_reply_id({**reply, "group_id": "g1"}) is None
    assert channel._decision_reply_id({"type": "text", "text": {"body": f"xtm.d:{ROW_ID}:A"}}) is None
    other = {"type": "interactive", "interactive": {"button_reply": {"id": "something-else"}}}
    assert channel._decision_reply_id(other) is None


class _Db:
    def __init__(self, row):
        self.row = row
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def get(self, _model, _id):
        return self.row


@pytest.fixture
def linked(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4(), full_name="Ada Lovelace", is_admin=False)
    resolve = AsyncMock(return_value=user)
    monkeypatch.setattr("app.channels.router.resolve_user_by_channel_identity", resolve)

    async def _load_view(_db, row, _user, _language):
        return _view(row, answered_by_name="Ada Lovelace")

    monkeypatch.setattr("app.services.decision_cards.load_view", _load_view)
    return user, resolve


@pytest.mark.asyncio
async def test_a_reply_answers_through_the_router_as_the_linked_account(monkeypatch, linked):
    user, resolve = linked
    answer = AsyncMock(return_value={})
    monkeypatch.setattr(decision_router, "answer_decision", answer)
    db = _Db(_row())
    text = await wa.answer_reply(db, "33600000000", f"xtm.d:{ROW_ID}:B", "en")
    resolve.assert_awaited_once_with(db, "whatsapp", "33600000000")
    answer.assert_awaited_once_with(db, f"work_item:{SOURCE_ID}", user, option="B", surface="whatsapp_card")
    db.commit.assert_awaited_once()
    assert text == notice("card_decided_option", "en", name="Ada Lovelace", option="Option 1")


@pytest.mark.asyncio
async def test_an_unlinked_sender_answers_nothing(monkeypatch, linked):
    _user, resolve = linked
    resolve.return_value = None
    answer = AsyncMock()
    monkeypatch.setattr(decision_router, "answer_decision", answer)
    text = await wa.answer_reply(_Db(_row()), "1", f"xtm.d:{ROW_ID}:A", "en")
    assert text == notice("card_not_linked_decision", "en")
    answer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "raw", "key"),
    [
        (_row(status="answered"), f"xtm.d:{ROW_ID}:A", "card_decision_already_answered"),
        (_row(), f"xtm.d:{ROW_ID}:Z", "card_changed"),
        (None, f"xtm.d:{ROW_ID}:A", "card_changed"),
        (_row(), "garbage", "card_decision_failed"),
    ],
)
async def test_a_reply_the_decision_no_longer_takes_answers_nothing(monkeypatch, linked, row, raw, key):
    answer = AsyncMock()
    monkeypatch.setattr(decision_router, "answer_decision", answer)
    assert await wa.answer_reply(_Db(row), "1", raw, "en") == notice(key, "en")
    answer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "key"), [(403, "card_decision_refused"), (409, "card_decision_already_answered")])
async def test_a_refused_answer_says_why(monkeypatch, linked, status, key):
    monkeypatch.setattr(decision_router, "answer_decision", AsyncMock(side_effect=HTTPException(status_code=status)))
    db = _Db(_row())
    assert await wa.answer_reply(db, "1", f"xtm.d:{ROW_ID}:A", "en") == notice(key, "en")
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


def test_the_surface_is_one_the_router_records():
    assert wa.SURFACE in decision_router.SURFACES
