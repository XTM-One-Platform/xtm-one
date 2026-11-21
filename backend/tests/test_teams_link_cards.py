"""The Teams card of a pasted XTM One link, without a database.

What a card says to whom is decided by the web's own rules, proven against
PostgreSQL in ``test_teams_link_cards_postgres.py``. This file pins what does
not need a database: which links are ours, that the posted card names nothing,
that a hostile value is never read as markdown nor becomes a URL, what each
view offers, and that an answer reaches ``answer_paused_run`` as the clicker.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.channels.teams import teams_messages
from app.services import teams_link_cards as cards
from app.services.channel_notices import notice

RUN_ID = uuid.UUID("0b8e6f8c-2d5c-4c55-9a51-8a3c1d2e3f40")
OTHER_ID = uuid.UUID("7f1d2c3b-4a59-4e68-8b7a-6c5d4e3f2a10")


@pytest.fixture(autouse=True)
def _platform_address(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "frontend_url", "https://xtm.example.com/one")
    monkeypatch.setattr(settings, "base_url", "https://api.example.com")


# --- Which links are ours ------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://xtm.example.com/one/operations/executions/{RUN_ID}", RUN_ID),
        (f"https://xtm.example.com/one/operations/executions/{RUN_ID}/", RUN_ID),
        (f"https://XTM.Example.com:443/one/operations/executions/{RUN_ID}", RUN_ID),
        (f"https://xtm.example.com/one/operations/executions/{RUN_ID}?tab=log#top", RUN_ID),
        (f"https://xtm.example.com/one/operations/executions/{str(RUN_ID).upper()}", RUN_ID),
        (f"https://api.example.com/operations/executions/{RUN_ID}", RUN_ID),
        (f"  https://xtm.example.com/one/operations/executions/{RUN_ID}  ", RUN_ID),
    ],
    ids=["frontend", "trailing-slash", "case-and-default-port", "query-and-fragment", "upper-id", "base-url", "spaces"],
)
def test_a_run_page_of_ours_unfurls(url, expected):
    assert cards.parse_link(url) == cards.LinkTarget(kind="run", id=expected)


@pytest.mark.parametrize(
    "url",
    [
        f"https://xtm.example.com/operations/executions/{RUN_ID}",  # outside the base path
        f"https://xtm.example.com/onex/operations/executions/{RUN_ID}",  # a sibling path
        f"https://xtm.example.com.evil.test/one/operations/executions/{RUN_ID}",  # lookalike host
        f"https://evil.test/one/operations/executions/{RUN_ID}",
        f"http://xtm.example.com/one/operations/executions/{RUN_ID}",  # another scheme
        f"https://xtm.example.com:8443/one/operations/executions/{RUN_ID}",  # another port
        f"https://user@xtm.example.com/one/operations/executions/{RUN_ID}",  # userinfo
        f"https://xtm.example.com/one/operations/executions/{RUN_ID}/log",
        "https://xtm.example.com/one/operations/executions/not-a-uuid",
        "https://xtm.example.com/one/agents/123",
        f"javascript:https://xtm.example.com/one/operations/executions/{RUN_ID}",
        "",
        None,
        42,
        "https://xtm.example.com/one/operations/executions/" + "a" * 3000,
    ],
    ids=[
        "outside-base-path",
        "sibling-path",
        "lookalike-host",
        "other-host",
        "other-scheme",
        "other-port",
        "userinfo",
        "sub-page",
        "bad-id",
        "other-page",
        "javascript",
        "empty",
        "none",
        "not-a-string",
        "too-long",
    ],
)
def test_anything_else_does_not_unfurl(url):
    assert cards.parse_link(url) is None


def test_the_open_link_is_built_from_the_platform_address_and_the_id_only():
    assert (
        cards.page_url(cards.LinkTarget("run", RUN_ID)) == f"https://xtm.example.com/one/operations/executions/{RUN_ID}"
    )


# --- The posted card -------------------------------------------------------------


def _strip_id(card: dict) -> str:
    return json.dumps(card, sort_keys=True).replace(str(RUN_ID), "ID").replace(str(OTHER_ID), "ID")


def test_the_posted_card_is_the_same_for_every_run():
    """The paste reads nothing, so the card cannot say whether a run exists."""
    first = cards.query_link_response(cards.LinkTarget("run", RUN_ID), "en")
    second = cards.query_link_response(cards.LinkTarget("run", OTHER_ID), "en")

    assert _strip_id(first) == _strip_id(second)
    attachment = first["composeExtension"]["attachments"][0]
    assert attachment["contentType"] == cards.ADAPTIVE_CARD
    assert attachment["preview"]["content"] == {"title": notice("card_header_run", "en")}
    content = attachment["content"]
    assert content["version"] == "1.4"
    assert content["refresh"] == {
        "action": {"type": "Action.Execute", "verb": cards.VERB_REFRESH, "data": {"kind": "run", "id": str(RUN_ID)}}
    }
    assert "userIds" not in content["refresh"]
    assert content["actions"] == [
        {
            "type": "Action.OpenUrl",
            "title": "Open in XTM One",
            "url": f"https://xtm.example.com/one/operations/executions/{RUN_ID}",
        }
    ]


# --- What one viewer sees -------------------------------------------------------


def _run(**fields) -> SimpleNamespace:
    values = {
        "id": RUN_ID,
        "status": "awaiting_human",
        "started_at": datetime(2026, 10, 27, 10, 42, tzinfo=UTC),
        "pending_approvals": None,
        "approval_decisions": None,
        "human_input_question": None,
        "human_input_response": None,
        "human_input_history": None,
    }
    values.update(fields)
    return SimpleNamespace(**values)


def _proposal(call_id: str, name: str = "send_email", **arguments) -> dict:
    return {"tool_call_id": call_id, "tool_name": name, "arguments": arguments or {"to": "soc@example.com"}}


def _view(run, *, can_respond=True, agent="Mail Triage", assignment="Phishing triage") -> cards.RunView:
    return cards.RunView(run=run, agent_name=agent, assignment_name=assignment, can_respond=can_respond)


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _of_type(card: dict, kind: str) -> list[dict]:
    return [node for node in _walk(card) if node.get("type") == kind]


def _verbs(card: dict) -> list[str]:
    return [action.get("verb") for action in card["actions"] if action["type"] == "Action.Execute"]


def test_a_responder_decides_each_tool_it_is_shown():
    run = _run(pending_approvals=[_proposal("c1"), _proposal("c2", "create_indicator", pattern="x")])
    card = cards.run_card(_view(run), "en")

    choices = _of_type(card, "Input.ChoiceSet")
    assert [c["id"] for c in choices] == ["d0", "d1"]
    assert [[o["value"] for o in c["choices"]] for c in choices] == [["approve", "reject"]] * 2
    assert _verbs(card) == [cards.VERB_DECIDE] * 3
    decide = [a for a in card["actions"] if a["type"] == "Action.Execute"]
    assert [a["data"] for a in decide] == [
        {"kind": "run", "id": str(RUN_ID), "calls": ["c1", "c2"]},
        {"kind": "run", "id": str(RUN_ID), "calls": ["c1", "c2"], "all": "approve"},
        {"kind": "run", "id": str(RUN_ID), "calls": ["c1", "c2"], "all": "reject"},
    ]
    texts = [run["text"] for block in _of_type(card, "RichTextBlock") for run in block["inlines"]]
    assert "to: soc@example.com" in texts
    assert "pattern: x" in texts


def test_a_reader_who_cannot_respond_sees_the_tools_without_their_arguments_nor_buttons():
    run = _run(pending_approvals=[_proposal("c1", "send_email", to="secret@example.com")])
    card = cards.run_card(_view(run, can_respond=False), "en")

    assert _verbs(card) == [cards.VERB_REQUEST]
    assert [a["title"] for a in card["actions"] if a["type"] == "Action.Execute"] == ["Ask an admin to act"]
    assert _of_type(card, "Input.ChoiceSet") == []
    assert _of_type(card, "Input.Text") == []
    dumped = json.dumps(card)
    assert "secret@example.com" not in dumped
    assert "send_email" in dumped
    assert notice("card_waiting_admin", "en") in dumped


def test_more_tools_than_a_card_shows_are_decided_in_the_web_app():
    run = _run(pending_approvals=[_proposal(f"c{i}") for i in range(cards.MAX_TOOLS_SHOWN + 1)])
    card = cards.run_card(_view(run), "en")

    assert _verbs(card) == []
    assert notice("card_too_many_tools", "en", count=cards.MAX_TOOLS_SHOWN + 1) in json.dumps(card, ensure_ascii=False)


def test_a_question_gets_an_answer_box_bound_to_that_question():
    run = _run(human_input_question="Deploy to production?")
    card = cards.run_card(_view(run), "en")

    assert [i["id"] for i in _of_type(card, "Input.Text")] == ["answer"]
    (send,) = [a for a in card["actions"] if a["type"] == "Action.Execute"]
    assert send["verb"] == cards.VERB_ANSWER
    assert send["data"] == {"kind": "run", "id": str(RUN_ID), "q": cards.question_digest("Deploy to production?")}


def test_an_answered_pause_offers_nothing_more():
    run = _run(
        pending_approvals=[_proposal("c1")],
        approval_decisions={"c1": {"decision": "approve"}},
        human_input_history=[{"responded_by": "Jane Doe"}],
    )
    card = cards.run_card(_view(run), "en")

    assert _verbs(card) == []
    assert _of_type(card, "Input.ChoiceSet") == []
    assert {"type": "TextRun", "text": "Jane Doe", "weight": "bolder"} in [
        inline for block in _of_type(card, "RichTextBlock") for inline in block["inlines"]
    ]


def test_a_finished_run_shows_its_state_and_who_answered():
    run = _run(status="completed", human_input_history=[{"responded_by": "Jane Doe"}])
    card = cards.run_card(_view(run), "fr")

    texts = [inline["text"] for block in _of_type(card, "RichTextBlock") for inline in block["inlines"]]
    assert "Terminée" in texts
    assert "Jane Doe" in texts
    assert _verbs(card) == []


@pytest.mark.parametrize(
    "hostile",
    [
        "[click](https://evil.test)",
        "**bold** _it_",
        "line1\nline2\r\n- item",
        "<script>alert(1)</script>",
        "![img](https://evil.test/x.png)",
        "{{DATE(2026-01-01T00:00:00Z, SHORT)}}",
        "‮evil",
    ],
    ids=["link", "emphasis", "newlines", "html", "image", "card-function", "bidi"],
)
def test_a_hostile_value_is_never_markdown_nor_a_url(hostile):
    """Names, questions, tool names and arguments only ever reach ``TextRun``s."""
    run = _run(
        pending_approvals=[_proposal("c1", hostile, note=hostile)],
        human_input_history=[{"responded_by": hostile}],
    )
    for card in (
        cards.run_card(_view(run, agent=hostile, assignment=hostile), "en"),
        cards.run_card(_view(run, agent=hostile, assignment=hostile, can_respond=False), "en"),
        cards.run_card(_view(_run(human_input_question=hostile), agent=hostile), "en"),
    ):
        for block in _of_type(card, "TextBlock"):
            assert hostile.split()[0] not in block["text"]
        urls = [node["url"] for node in _walk(card) if "url" in node]
        assert urls == [f"https://xtm.example.com/one/operations/executions/{RUN_ID}"]
        for inline in (inline for block in _of_type(card, "RichTextBlock") for inline in block["inlines"]):
            assert inline["type"] == "TextRun"
            assert "\n" not in inline["text"]


def test_a_long_value_is_clipped():
    run = _run(human_input_question="x" * 10_000)
    card = cards.run_card(_view(run, assignment="y" * 10_000), "en")

    lengths = [len(inline["text"]) for block in _of_type(card, "RichTextBlock") for inline in block["inlines"]]
    assert max(lengths) == 1500


def test_a_known_viewer_without_access_may_ask_the_owner_and_an_unknown_one_may_not():
    target = cards.LinkTarget("run", RUN_ID)
    linked = cards.no_access_card(target, "en", linked=True)
    unknown = cards.no_access_card(target, "en", linked=False)

    (ask,) = [a for a in linked["actions"] if a["type"] == "Action.Execute"]
    assert (ask["verb"], ask["title"], ask["data"]) == (cards.VERB_REQUEST, "Ask the owner to act", {"kind": "run", "id": str(RUN_ID)})
    assert _verbs(unknown) == []


# --- What a click submits -------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"calls": ["c1", "c2"], "d0": "approve", "d1": "reject", "reason": "no"}, [("c1", "approve", None), ("c2", "reject", "no")]),
        ({"calls": ["c1", "c2"], "all": "approve"}, [("c1", "approve", None), ("c2", "approve", None)]),
        ({"calls": ["c1", "c2"], "all": "reject", "reason": " too\nrisky "}, [("c1", "reject", "too risky"), ("c2", "reject", "too risky")]),
        ({"calls": ["c1", "c2"], "d0": "approve"}, "card_decide_every"),
        ({"calls": ["c1", "c2"], "d0": "approve", "d1": "approve_always"}, "card_decide_every"),
        ({"calls": ["c2", "c1"], "all": "approve"}, "card_changed"),
        ({"calls": ["c1"], "all": "approve"}, "card_changed"),
        ({"calls": "c1,c2", "all": "approve"}, "card_changed"),
        ({"all": "approve"}, "card_changed"),
    ],
    ids=["each", "approve-all", "reject-all-reason", "partial", "approve-always", "reordered", "fewer", "not-a-list", "no-calls"],
)
def test_a_submission_becomes_a_complete_decision_set_or_is_refused(data, expected):
    result = cards._decisions(data, [_proposal("c1"), _proposal("c2")])
    if isinstance(expected, str):
        assert result == expected
    else:
        assert [(d["tool_call_id"], d["decision"], d["rejection_reason"]) for d in result] == expected


class _Db:
    def __init__(self):
        self.commit = AsyncMock()
        self.rollback = AsyncMock()
        self.refresh = AsyncMock()


@pytest.mark.asyncio
async def test_a_decision_is_answered_as_the_clicker_through_the_respond_path(monkeypatch):
    clicker = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    run = _run(pending_approvals=[_proposal("c1")])
    monkeypatch.setattr(cards, "load_run_view", AsyncMock(return_value=_view(run)))
    answer = AsyncMock()
    db = _Db()

    with patch("app.api.v1.assignments.answer_paused_run", answer):
        card = await cards.act(
            db, cards.VERB_DECIDE, {"kind": "run", "id": str(RUN_ID), "calls": ["c1"], "d0": "reject", "reason": "no"}, clicker, "en"
        )

    assert answer.await_count == 1
    (called_db, run_id, user, body), kwargs = answer.await_args
    assert (called_db, run_id, user, kwargs) == (db, RUN_ID, clicker, {"surface": "teams_card", "answered_from": "teams_card"})
    assert body.response is None
    assert [d.model_dump() for d in body.decisions] == [
        {"tool_call_id": "c1", "decision": "reject", "rejection_reason": "no"}
    ]
    assert db.commit.await_count == 1
    assert _banner_text(card) == notice("card_answer_sent", "en")


@pytest.mark.asyncio
async def test_an_answer_is_sent_as_free_text(monkeypatch):
    clicker = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    run = _run(human_input_question="Deploy?")
    monkeypatch.setattr(cards, "load_run_view", AsyncMock(return_value=_view(run)))
    answer = AsyncMock()

    with patch("app.api.v1.assignments.answer_paused_run", answer):
        await cards.act(
            _Db(), cards.VERB_ANSWER, {"kind": "run", "id": str(RUN_ID), "q": cards.question_digest("Deploy?"), "answer": "  yes "}, clicker, "en"
        )

    body = answer.await_args.args[3]
    assert (body.response, body.decisions) == ("yes", None)


@pytest.mark.parametrize(
    ("data", "key"),
    [
        ({"q": "stale", "answer": "yes"}, "card_changed"),
        ({"answer": "   "}, "card_answer_empty"),
    ],
    ids=["other-question", "blank"],
)
@pytest.mark.asyncio
async def test_an_answer_that_cannot_be_sent_is_not(monkeypatch, data, key):
    run = _run(human_input_question="Deploy?")
    monkeypatch.setattr(cards, "load_run_view", AsyncMock(return_value=_view(run)))
    answer = AsyncMock()
    payload = {"kind": "run", "id": str(RUN_ID), "q": cards.question_digest("Deploy?"), **data}

    with patch("app.api.v1.assignments.answer_paused_run", answer):
        card = await cards.act(_Db(), cards.VERB_ANSWER, payload, SimpleNamespace(id=uuid.uuid4()), "en")

    assert answer.await_count == 0
    assert _banner_text(card) == notice(key, "en")


def _banner_text(card: dict) -> str | None:
    for block in card["body"]:
        inlines = block.get("inlines") or []
        if inlines and inlines[0].get("color") == "attention":
            return inlines[0]["text"]
    return None


@pytest.mark.parametrize(
    ("status_code", "key"),
    [(403, "card_refused"), (404, "card_refused"), (409, "card_already_answered"), (400, "card_failed"), (422, "card_failed")],
    ids=["forbidden", "missing", "answered", "bad-request", "unprocessable"],
)
@pytest.mark.asyncio
async def test_a_refused_answer_is_told_in_generic_words(monkeypatch, status_code, key):
    run = _run(pending_approvals=[_proposal("c1")])
    monkeypatch.setattr(cards, "load_run_view", AsyncMock(return_value=_view(run)))
    viewer = AsyncMock(return_value={"refreshed": True})
    monkeypatch.setattr(cards, "viewer_card", viewer)
    db = _Db()
    refused = AsyncMock(side_effect=HTTPException(status_code=status_code, detail="Only admins can respond"))

    with patch("app.api.v1.assignments.answer_paused_run", refused):
        await cards.act(db, cards.VERB_DECIDE, {"kind": "run", "id": str(RUN_ID), "calls": ["c1"], "all": "approve"}, SimpleNamespace(id=uuid.uuid4()), "en")

    assert db.rollback.await_count == 1
    assert db.commit.await_count == 0
    assert viewer.await_args.kwargs == {"banner": notice(key, "en")}


@pytest.mark.asyncio
async def test_a_run_the_clicker_cannot_read_gets_the_no_access_card(monkeypatch):
    monkeypatch.setattr(cards, "load_run_view", AsyncMock(return_value=None))
    refused = AsyncMock()
    monkeypatch.setattr(cards, "_record_refused_action", refused)
    answer = AsyncMock()
    db = _Db()
    clicker = SimpleNamespace(id=uuid.uuid4())

    with patch("app.api.v1.assignments.answer_paused_run", answer):
        card = await cards.act(
            db, cards.VERB_DECIDE, {"kind": "run", "id": str(RUN_ID), "calls": ["c1"], "all": "approve"}, clicker, "en"
        )

    assert answer.await_count == 0
    assert refused.await_args.args == (db, RUN_ID, clicker)
    assert card == cards.no_access_card(cards.LinkTarget("run", RUN_ID), "en", linked=True)


@pytest.mark.asyncio
async def test_an_unknown_teams_user_is_asked_to_sign_in_and_acts_on_nothing():
    answer = AsyncMock()
    with patch("app.api.v1.assignments.answer_paused_run", answer):
        card = await cards.act(_Db(), cards.VERB_DECIDE, {"kind": "run", "id": str(RUN_ID), "all": "approve"}, None, "en")

    assert answer.await_count == 0
    assert card == cards.no_access_card(cards.LinkTarget("run", RUN_ID), "en", linked=False)
    assert notice("card_not_linked", "en") in json.dumps(card)


@pytest.mark.asyncio
async def test_a_request_is_made_as_the_clicker_and_answered_the_same_whatever_happened(monkeypatch):
    clicker = SimpleNamespace(id=uuid.uuid4())
    viewer = AsyncMock(return_value={"refreshed": True})
    monkeypatch.setattr(cards, "viewer_card", viewer)
    db = _Db()

    with patch("app.services.access_requests.request_run_action", new=AsyncMock()) as request:
        await cards.act(db, cards.VERB_REQUEST, {"kind": "run", "id": str(RUN_ID)}, clicker, "en")

    assert request.await_args.args == (db, RUN_ID, clicker)
    assert request.await_args.kwargs == {"source": "teams_card"}
    assert viewer.await_args.args == (db, cards.LinkTarget("run", RUN_ID), clicker, "en")
    assert viewer.await_args.kwargs == {"banner": notice("card_request_sent", "en")}


@pytest.mark.asyncio
async def test_a_throttled_request_is_told_in_generic_words(monkeypatch):
    viewer = AsyncMock(return_value={"refreshed": True})
    monkeypatch.setattr(cards, "viewer_card", viewer)

    with patch("app.services.access_requests.request_run_action", new=AsyncMock(side_effect=HTTPException(status_code=429))):
        await cards.act(_Db(), cards.VERB_REQUEST, {"kind": "run", "id": str(RUN_ID)}, SimpleNamespace(id=uuid.uuid4()), "en")

    assert viewer.await_args.kwargs == {"banner": notice("card_failed", "en")}


@pytest.mark.parametrize(
    ("verb", "data"),
    [
        (cards.VERB_DECIDE, {"kind": "agent", "id": str(RUN_ID)}),
        (cards.VERB_DECIDE, {"kind": "run", "id": "../../etc"}),
        (cards.VERB_DECIDE, "not-a-dict"),
        ("xtm.unknown", {"kind": "run", "id": str(RUN_ID)}),
    ],
    ids=["other-kind", "bad-id", "not-a-dict", "unknown-verb"],
)
@pytest.mark.asyncio
async def test_card_data_naming_nothing_we_know_is_refused(monkeypatch, verb, data):
    monkeypatch.setattr(cards, "load_run_view", AsyncMock(return_value=_view(_run())))
    with pytest.raises(ValueError):
        await cards.act(_Db(), verb, data, SimpleNamespace(id=uuid.uuid4()), "en")


# --- The webhook ------------------------------------------------------------------


def _invoke(name: str, value: dict, **extra) -> Request:
    body = json.dumps(
        {"type": "invoke", "name": name, "value": value, "from": {"id": "29:x", "aadObjectId": "aad-1"}, **extra}
    ).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/channels/teams/messages",
        "query_string": b"",
        "headers": [(b"authorization", b"Bearer token")],
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


@pytest.mark.asyncio
async def test_an_invoke_is_refused_when_no_bot_can_vouch_for_it():
    with (
        patch("app.channels.teams._get_teams_app_id", new=AsyncMock(return_value="")),
        patch("app.channels.teams.warn_unauthenticated_once"),
        patch("app.services.teams_link_cards.act", new=AsyncMock()) as act,
    ):
        response = await teams_messages(_invoke("adaptiveCard/action", {"action": {"verb": cards.VERB_REFRESH}}))

    assert response.status_code == 401
    assert act.await_count == 0


@pytest.mark.asyncio
async def test_a_pasted_link_unfurls_without_reading_anything():
    with (
        patch("app.channels.teams._get_teams_app_id", new=AsyncMock(return_value="app-123")),
        patch("app.channels.teams._verify_botframework_jwt", return_value=True),
        patch("app.core.database.async_session") as sessions,
    ):
        response = await teams_messages(
            _invoke(
                "composeExtension/queryLink", {"url": f"https://xtm.example.com/one/operations/executions/{RUN_ID}"}
            )
        )

    assert response.status_code == 200
    assert json.loads(response.body) == cards.query_link_response(cards.LinkTarget("run", RUN_ID), "en")
    assert sessions.call_count == 0


@pytest.mark.asyncio
async def test_a_link_that_is_not_ours_gets_no_card():
    with (
        patch("app.channels.teams._get_teams_app_id", new=AsyncMock(return_value="app-123")),
        patch("app.channels.teams._verify_botframework_jwt", return_value=True),
    ):
        response = await teams_messages(
            _invoke("composeExtension/queryLink", {"url": "https://evil.test/operations/executions/x"})
        )

    assert response.status_code == 200
    assert response.body == b""


@pytest.mark.asyncio
async def test_a_throttled_viewer_gets_a_generic_refusal():
    with (
        patch("app.channels.teams._get_teams_app_id", new=AsyncMock(return_value="app-123")),
        patch("app.channels.teams._verify_botframework_jwt", return_value=True),
        patch("app.channels.teams._configured_teams_tenant", new=AsyncMock(return_value="tenant-a")),
        patch("app.core.rate_limit.enforce_rate_limit", new=AsyncMock(side_effect=HTTPException(status_code=429))),
        patch("app.services.teams_link_cards.act", new=AsyncMock()) as act,
    ):
        response = await teams_messages(
            _invoke(
                "adaptiveCard/action",
                {"action": {"verb": cards.VERB_REFRESH, "data": {"kind": "run", "id": str(RUN_ID)}}},
                channelData={"tenant": {"id": "tenant-a"}},
            )
        )

    assert json.loads(response.body) == {
        "statusCode": 429,
        "type": "application/vnd.microsoft.error",
        "value": {"code": "TooManyRequests", "message": "The card could not be updated."},
    }
    assert act.await_count == 0


@pytest.mark.asyncio
async def test_a_click_on_an_answered_run_is_told_so_and_decides_nothing(monkeypatch):
    run = _run(pending_approvals=[_proposal("c1")], approval_decisions={"c1": {"decision": "approve"}})
    monkeypatch.setattr(cards, "load_run_view", AsyncMock(return_value=_view(run)))
    answer = AsyncMock()

    with patch("app.api.v1.assignments.answer_paused_run", answer):
        card = await cards.act(
            _Db(), cards.VERB_DECIDE, {"kind": "run", "id": str(RUN_ID), "calls": ["c1"]}, SimpleNamespace(id=uuid.uuid4()), "en"
        )

    assert answer.await_count == 0
    assert _banner_text(card) == notice("card_already_answered", "en")


@pytest.mark.asyncio
async def test_card_data_never_decides_calls_no_card_shows(monkeypatch):
    proposals = [_proposal(f"c{i}") for i in range(cards.MAX_TOOLS_SHOWN + 1)]
    monkeypatch.setattr(cards, "load_run_view", AsyncMock(return_value=_view(_run(pending_approvals=proposals))))
    answer = AsyncMock()
    forged = {"kind": "run", "id": str(RUN_ID), "calls": [p["tool_call_id"] for p in proposals], "all": "approve"}

    with patch("app.api.v1.assignments.answer_paused_run", answer):
        card = await cards.act(_Db(), cards.VERB_DECIDE, forged, SimpleNamespace(id=uuid.uuid4()), "en")

    assert answer.await_count == 0
    assert _verbs(card) == []


@pytest.mark.parametrize(
    ("configured", "activity", "refused"),
    [
        ("tenant-a", "tenant-b", True),
        ("tenant-a", "", True),
        ("tenant-a", "TENANT-A", False),
        ("", "tenant-b", True),
        ("", "", True),
    ],
    ids=["other-tenant", "no-tenant", "same-tenant", "none-configured", "none-at-all"],
)
@pytest.mark.asyncio
async def test_a_card_action_from_another_tenant_is_refused(configured, activity, refused):
    request = _invoke(
        "adaptiveCard/action",
        {"action": {"verb": cards.VERB_REFRESH, "data": {"kind": "run", "id": str(RUN_ID)}}},
        channelData={"tenant": {"id": activity}} if activity else {},
    )
    with (
        patch("app.channels.teams._get_teams_app_id", new=AsyncMock(return_value="app-123")),
        patch("app.channels.teams._verify_botframework_jwt", return_value=True),
        patch("app.channels.teams._configured_teams_tenant", new=AsyncMock(return_value=configured)),
        patch("app.core.rate_limit.enforce_rate_limit", new=AsyncMock()),
        patch("app.channels.teams._invoke_user", new=AsyncMock(return_value=None)),
        patch("app.core.database.async_session"),
        patch("app.services.teams_link_cards.act", new=AsyncMock(return_value={"card": True})) as act,
    ):
        response = await teams_messages(request)

    payload = json.loads(response.body)
    if refused:
        assert (payload["statusCode"], payload["value"]["code"], act.await_count) == (403, "Forbidden", 0)
    else:
        assert (payload["statusCode"], payload["value"], act.await_count) == (200, {"card": True}, 1)


@pytest.mark.asyncio
async def test_the_bot_framework_token_comes_from_the_shared_app_token_cache():
    from app.channels import teams

    bot = SimpleNamespace(client_id_enc="enc-id", extra_config_enc=None)
    with (
        patch("app.channels.teams.get_channel_bot", new=AsyncMock(return_value=bot)),
        patch("app.channels.teams.decrypt_value", return_value="app-123"),
        patch("app.channels.teams.reveal_field", new=AsyncMock(return_value="secret")),
        patch("app.integrations.providers.teams_bot._acquire_token", new=AsyncMock(return_value="tok")) as acquire,
    ):
        creds = await teams._get_teams_creds()

    assert creds == ("tok", "app-123")
    assert acquire.await_args.args == ("", "app-123", "secret")
    assert acquire.await_args.kwargs == {"scope": "https://api.botframework.com/.default"}
