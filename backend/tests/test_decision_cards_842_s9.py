"""The Teams decision card of every human gate (#842, S9), without a database.

What the card shows to whom, and that an answer goes through the one router
as the clicker with surface ``teams_card``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.services import decision_cards as cards
from app.services import teams_link_cards
from app.services.channel_notices import notice

ROW_ID = uuid.UUID("5f0c2a1e-3b4d-4e5f-8a9b-0c1d2e3f4a5b")
SOURCE_ID = uuid.UUID("0b8e6f8c-2d5c-4c55-9a51-8a3c1d2e3f40")
ASK = {
    "question": "Ship the fix tonight?",
    "context": "CI is green.\nThe change is backend only.",
    "options": [
        {"key": "A", "label": "Ship", "effect": "deploys at 22:00"},
        {"key": "B", "label": "Wait", "effect": "ships Monday"},
    ],
    "recommendation": "A",
    "deadline": "2026-11-23T09:00:00+00:00",
    "links": [{"label": "PR", "url": "https://github.com/x/y/pull/1"}],
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


def _view(row=None, *, may_answer=True, **fields) -> cards.DecisionView:
    return cards.DecisionView(
        decision=row or _row(),
        may_answer=may_answer,
        recipient_name="Ada Lovelace",
        agent_name="Mail Triage",
        page_path=f"/tasks/work-items/{SOURCE_ID}",
        **fields,
    )


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _texts(card: dict) -> list[str]:
    return [n["text"] for n in _walk(card) if n.get("type") in ("TextRun", "TextBlock")]


def _executes(card: dict) -> list[dict]:
    return [a for a in card["actions"] if a["type"] == "Action.Execute"]


@pytest.fixture(autouse=True)
def _platform_address(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "frontend_url", "https://xtm.example.com/one")


def _choices(card: dict) -> list[dict]:
    sets = [n for n in _walk(card) if n.get("type") == "Input.ChoiceSet"]
    return sets[0]["choices"] if sets else []


def test_the_recipient_picks_an_option_and_sends_it_with_one_button():
    card = cards.render_decision_card(_view(), "en")

    executes = _executes(card)
    assert [a["title"] for a in executes] == [notice("card_send", "en")]
    assert executes[0]["verb"] == cards.VERB_DECISION_ANSWER
    assert executes[0]["data"] == {"kind": "decision", "id": f"work_item:{SOURCE_ID}", "d": str(ROW_ID)}
    sets = [n for n in _walk(card) if n.get("type") == "Input.ChoiceSet"]
    assert [(s["id"], s["style"]) for s in sets] == [("option", "expanded")]
    recommended = f"({notice('card_recommended', 'en')})"
    assert _choices(card) == [{"title": f"Ship {recommended}", "value": "A"}, {"title": "Wait", "value": "B"}]
    assert [n["id"] for n in _walk(card) if n.get("type") == "Input.Text"] == ["text"]
    texts = _texts(card)
    assert "Ship the fix tonight?" in texts
    assert "The change is backend only." in texts
    assert any(recommended in t for t in texts)
    assert any("{{DATE(2026-11-23T09:00:00Z, SHORT)}}" in t for t in texts)
    opens = [a["url"] for a in card["actions"] if a["type"] == "Action.OpenUrl"]
    assert opens == ["https://github.com/x/y/pull/1", f"https://xtm.example.com/one/tasks/work-items/{SOURCE_ID}"]
    assert card["refresh"]["action"]["verb"] == cards.VERB_REFRESH
    assert card["msteams"] == {"width": "Full"}


def test_recommended_is_never_coloured():
    """Teams' good green is 2.07:1 on the light theme (UX round 1, B1)."""
    card = cards.render_decision_card(_view(may_answer=False), "en")
    assert not [n for n in _walk(card) if n.get("type") == "TextRun" and n.get("color") == "good"]


_APPROVAL_ASK = {
    "question": "A person's input is needed",
    "options": [
        {"key": "approve", "label": "Approve"},
        {"key": "approve_always", "label": "Approve always"},
        {"key": "reject", "label": "Reject"},
    ],
}
_PROPOSALS = ({"tool_call_id": "c1", "tool_name": "send_email", "arguments": {"to": "a@b.c", "subject": "Hi"}},)


@pytest.mark.parametrize("language", ["en", "es", "fr", "ja"])
def test_a_tool_approval_reads_its_options_in_the_viewer_language(language):
    """UX round 1, B3: the row stores English labels; the card never shows them in another language."""
    row = _row(source_type="run", kind="tool_approval", ask=_APPROVAL_ASK)
    card = cards.render_decision_card(_view(row, proposals=_PROPOSALS), language)
    assert [c["value"] for c in _choices(card)] == ["approve", "approve_always", "reject"]
    assert [c["title"] for c in _choices(card)] == [
        notice("card_approve", language),
        notice("card_approve_always", language),
        notice("card_reject", language),
    ]
    texts = _texts(card)
    # The choices name the options: no bold line repeats them (A2).
    assert notice("card_approve_always", language) not in texts
    placeholder = [n["placeholder"] for n in _walk(card) if n.get("type") == "Input.Text"]
    assert placeholder == [notice("card_reason", language)]


@pytest.mark.parametrize("language", ["en", "es", "fr", "ja"])
def test_a_tool_approval_choice_is_required_before_send(language):
    """UX round 2, B1: Teams checks the choice before sending, with a message saying which one to make."""
    row = _row(source_type="run", kind="tool_approval", ask=_APPROVAL_ASK)
    sets = [n for n in _walk(cards.render_decision_card(_view(row), language)) if n.get("type") == "Input.ChoiceSet"]
    assert [(s.get("isRequired"), s.get("errorMessage")) for s in sets] == [
        (True, notice("card_choose_approval", language))
    ]
    other = [n for n in _walk(cards.render_decision_card(_view(), language)) if n.get("type") == "Input.ChoiceSet"]
    assert [s.get("isRequired") for s in other] == [None]


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        ({"c1": "approve", "c2": "approve"}, ("card_decided_option", {"option": "card_approve"})),
        ({"c1": "approve_always"}, ("card_decided_option", {"option": "card_approve_always"})),
        ({"c1": "reject"}, ("card_decided_option", {"option": "card_reject"})),
        ({"c1": "reject", "c2": "approve", "c3": "approve"}, ("card_decided_tools", None)),
    ],
    ids=["all-approved", "approved-always", "rejected", "mixed"],
)
@pytest.mark.parametrize("language", ["en", "fr", "ja"])
def test_an_answered_tool_approval_says_its_outcome(verdicts, expected, language):
    """UX round 2, B2: the decided card says approved, approved always or rejected, never only who answered."""
    row = _row(
        source_type="run",
        kind="tool_approval",
        ask=_APPROVAL_ASK,
        status="answered",
        answer={"decisions": verdicts},
        answered_at=datetime(2026, 11, 22, 1, 0, tzinfo=UTC),
    )
    card = cards.render_decision_card(_view(row, answered_by_name="Ada Lovelace"), language)
    texts = _texts(card)
    key, values = expected
    if values:
        assert notice(values["option"], language) in texts
    else:
        assert f"{notice('card_approve', language)} (2), {notice('card_reject', language)} (1)" in texts
    assert notice(key, language).split("{name}")[1].split("{")[0] in texts
    assert _executes(card) == []


def test_a_tool_approval_shows_the_calls_it_decides():
    """UX round 1, B4: the tool and its arguments for the decider, the tool alone for another viewer."""
    row = _row(source_type="run", kind="tool_approval", ask=_APPROVAL_ASK)
    texts = _texts(cards.render_decision_card(_view(row, proposals=_PROPOSALS), "en"))
    assert notice("card_tools_awaiting", "en") in texts
    assert "send_email" in texts
    assert "to: a@b.c" in texts
    other = _texts(cards.render_decision_card(_view(row, may_answer=False, proposals=_PROPOSALS), "en"))
    assert "send_email" in other
    assert "to: a@b.c" not in other


def test_a_tool_approval_with_more_calls_than_shown_is_decided_in_xtm_one():
    row = _row(source_type="run", kind="tool_approval", ask=_APPROVAL_ASK)
    many = tuple({"tool_call_id": f"c{i}", "tool_name": f"t{i}"} for i in range(teams_link_cards.MAX_TOOLS_SHOWN + 1))
    card = cards.render_decision_card(_view(row, proposals=many), "en")
    assert _executes(card) == []
    assert _choices(card) == []
    assert notice("card_too_many_tools", "en", count=len(many)) in _texts(card)


@pytest.mark.parametrize("may_answer", [True, False, None])
@pytest.mark.parametrize("status", ["open", "answered", "expired", "cancelled"])
@pytest.mark.parametrize("kind", ["work_item", "hitl", "flow_ask", "tool_approval"])
def test_no_state_has_more_actions_than_teams_shows(kind, status, may_answer):
    """UX round 1, B5: Teams drops card actions past six, silently; six options and three links stay at five."""
    ask = {
        "question": "Pick one",
        "options": [{"key": k, "label": f"Option {k}", "effect": "does it"} for k in "ABCDEF"],
        "recommendation": "A",
        "links": [{"label": f"L{i}", "url": f"https://example.com/{i}"} for i in range(3)],
    }
    row = _row(kind=kind, status=status, ask=ask, answer={"option": "A"} if status == "answered" else None)
    card = cards.render_decision_card(_view(row, may_answer=may_answer), "en", banner="x")
    assert len(card["actions"]) <= 6
    if status == "open" and may_answer:
        assert len(_choices(card)) == 6


def test_a_sent_answer_is_not_painted_as_an_error_and_a_refusal_is():
    """UX round 1, B2: attention (red) only for a refusal or a failure."""
    ok = cards.render_decision_card(_view(), "en", banner="sent", banner_ok=True)
    refused = cards.render_decision_card(_view(), "en", banner="refused")

    def colour(card, text):
        return [n.get("color") for n in _walk(card) if n.get("type") == "TextRun" and n.get("text") == text]

    assert colour(ok, "sent") == [None]
    assert colour(refused, "refused") == ["attention"]


def test_another_viewer_sees_the_question_and_who_it_waits_for_never_a_button():
    card = cards.render_decision_card(_view(may_answer=False), "en")
    assert _executes(card) == []
    assert "Ada Lovelace" in _texts(card)
    assert not [n for n in _walk(card) if n.get("type") == "Input.Text"]


def test_an_unknown_teams_user_is_asked_to_sign_in():
    card = cards.render_decision_card(_view(may_answer=None), "en")
    assert _executes(card) == []
    assert notice("card_not_linked_decision", "en") in _texts(card)


def test_a_decided_card_shows_who_chose_what_and_offers_nothing():
    row = _row(
        status="answered",
        answer={"option": "A", "text": "go"},
        answered_by=uuid.uuid4(),
        answered_at=datetime(2026, 11, 22, 1, 0, tzinfo=UTC),
    )
    card = cards.render_decision_card(_view(row, answered_by_name="Grace Hopper"), "en")
    assert _executes(card) == []
    texts = _texts(card)
    assert "Grace Hopper" in texts
    assert "Ship" in texts
    assert "go" in texts


@pytest.mark.parametrize(
    ("status", "key"), [("expired", "card_decision_expired"), ("cancelled", "card_decision_cancelled")]
)
def test_a_closed_card_says_why(status, key):
    card = cards.render_decision_card(_view(_row(status=status)), "fr")
    assert notice(key, "fr") in _texts(card)
    assert _executes(card) == []


def test_a_hostile_value_is_never_markdown_nor_a_url():
    row = _row(
        ask={"question": "[click](javascript:alert(1)) **bold**", "options": [{"key": "A", "label": "<b>x</b>"}]}
    )
    card = cards.render_decision_card(_view(row, may_answer=False), "en")
    assert all(n.get("type") != "TextBlock" or "{{" in n["text"] for n in _walk(card))
    serialized = json.dumps(card)
    assert "javascript:" not in [a.get("url", "") for a in card["actions"]]
    assert "[click](javascript:alert(1)) **bold**" in serialized


@pytest.mark.parametrize(
    ("recipient", "user", "expected"),
    [
        ("user:11111111-1111-1111-1111-111111111111", {"id": uuid.UUID("11111111-1111-1111-1111-111111111111")}, True),
        ("user:11111111-1111-1111-1111-111111111111", {"id": uuid.uuid4()}, False),
        ("admins", {"id": uuid.uuid4(), "is_admin": True}, True),
        ("admins", {"id": uuid.uuid4(), "is_admin": False}, False),
        ("company", {"id": uuid.uuid4()}, True),
        ("company", {"id": uuid.uuid4(), "is_active": False}, False),
        ("nonsense", {"id": uuid.uuid4(), "is_admin": True}, False),
    ],
    ids=["owner", "not-owner", "admin", "not-admin", "company", "inactive", "unknown"],
)
@pytest.mark.asyncio
async def test_the_card_offers_buttons_to_the_recipient_alone(recipient, user, expected):
    assert await cards.may_answer(AsyncMock(), _row(recipient=recipient), SimpleNamespace(**user)) is expected


class _Db:
    def __init__(self):
        self.commit = AsyncMock()
        self.rollback = AsyncMock()


def _patch_reads(monkeypatch, row):
    monkeypatch.setattr(cards, "_row_for", AsyncMock(return_value=row))
    monkeypatch.setattr(cards, "load_view", AsyncMock(side_effect=lambda db, r, u, lang: _view(r)))


@pytest.mark.asyncio
async def test_a_click_answers_through_the_router_as_the_clicker(monkeypatch):
    clicker = SimpleNamespace(id=uuid.uuid4())
    _patch_reads(monkeypatch, _row())
    answer = AsyncMock(return_value={})
    monkeypatch.setattr("app.services.decision_router.answer_decision", answer)
    db = _Db()

    data = {"kind": "decision", "id": f"work_item:{SOURCE_ID}", "d": str(ROW_ID), "option": "A", "text": " ok "}
    card = await teams_link_cards.act(db, cards.VERB_DECISION_ANSWER, data, clicker, "en")

    answer.assert_awaited_once_with(db, f"work_item:{SOURCE_ID}", clicker, option="A", text="ok", surface="teams_card")
    assert db.commit.await_count == 1
    assert notice("card_answer_sent", "en") in _texts(card)
    sent = [n for n in _walk(card) if n.get("type") == "TextRun" and n.get("text") == notice("card_answer_sent", "en")]
    assert [n.get("color") for n in sent] == [None]


@pytest.mark.parametrize(
    ("status_code", "key"),
    [
        (403, "card_decision_refused"),
        (404, "card_decision_refused"),
        (409, "card_decision_already_answered"),
        (400, "card_decision_failed"),
    ],
)
@pytest.mark.asyncio
async def test_a_refused_answer_is_told_in_generic_words(monkeypatch, status_code, key):
    _patch_reads(monkeypatch, _row())
    monkeypatch.setattr(
        "app.services.decision_router.answer_decision",
        AsyncMock(side_effect=HTTPException(status_code, "secret detail")),
    )
    db = _Db()
    data = {"kind": "decision", "id": f"work_item:{SOURCE_ID}", "d": str(ROW_ID), "option": "A"}
    card = await cards.act(db, cards.VERB_DECISION_ANSWER, data, SimpleNamespace(id=uuid.uuid4()), "en")
    assert db.rollback.await_count == 1
    assert notice(key, "en") in _texts(card)
    assert "secret detail" not in json.dumps(card)


@pytest.mark.asyncio
async def test_a_card_of_an_earlier_ask_answers_nothing(monkeypatch):
    _patch_reads(monkeypatch, _row())
    answer = AsyncMock()
    monkeypatch.setattr("app.services.decision_router.answer_decision", answer)
    data = {"kind": "decision", "id": f"work_item:{SOURCE_ID}", "d": str(uuid.uuid4()), "option": "A"}
    card = await cards.act(_Db(), cards.VERB_DECISION_ANSWER, data, SimpleNamespace(id=uuid.uuid4()), "en")
    answer.assert_not_awaited()
    assert notice("card_changed", "en") in _texts(card)


@pytest.mark.asyncio
async def test_an_empty_click_and_an_answered_decision_answer_nothing(monkeypatch):
    answer = AsyncMock()
    monkeypatch.setattr("app.services.decision_router.answer_decision", answer)
    _patch_reads(monkeypatch, _row())
    data = {"kind": "decision", "id": f"work_item:{SOURCE_ID}", "d": str(ROW_ID)}
    card = await cards.act(_Db(), cards.VERB_DECISION_ANSWER, data, SimpleNamespace(id=uuid.uuid4()), "en")
    assert notice("card_answer_empty", "en") in _texts(card)
    _patch_reads(monkeypatch, _row(status="answered", answer={"option": "B"}))
    card = await cards.act(
        _Db(), cards.VERB_DECISION_ANSWER, {**data, "option": "A"}, SimpleNamespace(id=uuid.uuid4()), "en"
    )
    assert notice("card_decision_already_answered", "en") in _texts(card)
    answer.assert_not_awaited()


@pytest.mark.parametrize("text", [None, "looks fine"])
@pytest.mark.asyncio
async def test_a_tool_approval_sent_without_a_choice_says_which_choice(monkeypatch, text):
    """UX round 2, B1: a reason alone never answers an approval and never reads as a generic failure."""
    answer = AsyncMock()
    monkeypatch.setattr("app.services.decision_router.answer_decision", answer)
    _patch_reads(monkeypatch, _row(source_type="run", kind="tool_approval", ask=_APPROVAL_ASK))
    data = {"kind": "decision", "id": f"run:{SOURCE_ID}", "d": str(ROW_ID)}
    if text:
        data["text"] = text
    card = await cards.act(_Db(), cards.VERB_DECISION_ANSWER, data, SimpleNamespace(id=uuid.uuid4()), "fr")
    answer.assert_not_awaited()
    texts = _texts(card)
    assert notice("card_choose_approval", "fr") in texts
    assert notice("card_decision_failed", "fr") not in texts
    assert notice("card_answer_empty", "fr") not in texts


@pytest.mark.asyncio
async def test_card_data_naming_no_decision_is_refused(monkeypatch):
    monkeypatch.setattr(cards, "_row_for", AsyncMock(return_value=None))
    with pytest.raises(ValueError):
        await cards.act(_Db(), cards.VERB_DECISION_ANSWER, {"kind": "decision", "id": "nope"}, None, "en")


@pytest.mark.asyncio
async def test_a_refresh_and_an_unknown_user_never_answer(monkeypatch):
    answer = AsyncMock()
    monkeypatch.setattr("app.services.decision_router.answer_decision", answer)
    _patch_reads(monkeypatch, _row())
    data = {"kind": "decision", "id": f"work_item:{SOURCE_ID}", "d": str(ROW_ID), "option": "A"}
    await cards.act(_Db(), cards.VERB_REFRESH, data, SimpleNamespace(id=uuid.uuid4()), "en")
    await cards.act(_Db(), cards.VERB_DECISION_ANSWER, data, None, "en")
    answer.assert_not_awaited()
