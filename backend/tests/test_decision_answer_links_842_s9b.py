# SPDX-License-Identifier: Apache-2.0

"""Human gates S9b (#842): the signed answer links of a decision (human-gates.md I.4).

The token rules (signature, recipient, expiry, rotation) and the cards built
from them are pure, tested here without a database; the answer through the
link against PostgreSQL is in ``test_decision_answer_links_842_s9b_postgres.py``.
"""

import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.integrations.providers.microsoft._cards import parse_card
from app.services import decision_answer_links as links
from app.services.decision_router import SURFACES

DID = uuid.UUID("0f1d2c3b-4a59-4687-9a1b-2c3d4e5f6a7b")
ALICE = uuid.UUID("11111111-2222-4333-8444-555555555555")
BOB = uuid.UUID("99999999-8888-4777-8666-555555555555")


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "secret_key", "s9b-test-secret-key-0123456789abcdef")
    monkeypatch.setattr(settings, "decision_link_previous_secrets", "")
    monkeypatch.setattr(settings, "frontend_url", "https://one.example.test/")


def _decision(**over: Any) -> Any:
    base = {
        "id": DID,
        "ask": {
            "question": "Merge the pull request now?",
            "context": "CI is green.",
            "options": [
                {"key": "A", "label": "Merge", "effect": "squash-merged now"},
                {"key": "B", "label": "Wait", "effect": "kept open"},
            ],
            "recommendation": "A",
        },
        "deadline": None,
        "status": "open",
        "answer": None,
        "answered_at": None,
        "surface": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _token(
    option: str = "A", recipient: uuid.UUID = ALICE, surface: str = "teams_chat", expires: int | None = None
) -> str:
    return links.mint_token(DID, option, recipient, surface, expires or int(time.time()) + 60)


def test_a_minted_token_reads_back() -> None:
    link = links.read_token(_token())
    assert (link.decision_id, link.option, link.recipient_id, link.surface) == (DID, "A", ALICE, "teams_chat")


def test_a_tampered_token_is_not_valid() -> None:
    body, _, sig = _token().partition(".")
    other = _token(option="B").partition(".")[0]
    with pytest.raises(HTTPException) as exc:
        links.read_token(f"{other}.{sig}")
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException):
        links.read_token(body)


def test_an_expired_token_is_refused() -> None:
    with pytest.raises(HTTPException) as exc:
        links.read_token(_token(expires=int(time.time()) - 1))
    assert exc.value.status_code == 410


def test_a_forwarded_link_answers_nothing() -> None:
    link = links.read_token(_token(recipient=ALICE))
    links.check_reader(link, SimpleNamespace(id=ALICE))
    with pytest.raises(HTTPException) as exc:
        links.check_reader(link, SimpleNamespace(id=BOB))
    assert exc.value.status_code == 403


def test_a_link_signed_before_a_key_rotation_still_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    token = _token()
    monkeypatch.setattr(settings, "secret_key", "the-new-secret-key-after-rotation-xyz")
    with pytest.raises(HTTPException):
        links.read_token(token)
    monkeypatch.setattr(settings, "decision_link_previous_secrets", "s9b-test-secret-key-0123456789abcdef")
    assert links.read_token(token).option == "A"


def test_no_secret_refuses_to_mint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "secret_key", "")
    with pytest.raises(ValueError):
        _token()


def test_unknown_surface_is_refused() -> None:
    with pytest.raises(ValueError):
        _token(surface="carrier_pigeon")


def test_every_link_surface_is_a_router_surface() -> None:
    assert links.LINK_SURFACES <= SURFACES


def test_expiry_is_the_deadline_else_seven_days() -> None:
    deadline = datetime(2026, 12, 1, 9, 0, tzinfo=UTC)
    assert links.expiry_for(deadline) == int(deadline.timestamp())
    assert links.expiry_for(None, now=1000.0) == 1000 + 7 * 24 * 3600


def test_one_link_per_option_then_text_per_recipient() -> None:
    rows = links.answer_links(_decision(), ALICE, "teams_chat")
    assert [r["key"] for r in rows] == ["A", "B", links.TEXT_OPTION]
    assert all(r["url"].startswith("https://one.example.test/decide/") for r in rows)
    other = links.answer_links(_decision(), BOB, "teams_chat")
    assert rows[0]["url"] != other[0]["url"]
    assert links.read_token(rows[0]["url"].rsplit("/", 1)[1]).recipient_id == ALICE


def test_link_card_holds_only_open_url_and_passes_the_graph_card_check() -> None:
    card = links.link_card(_decision(deadline=datetime.now(UTC) + timedelta(days=1)), ALICE, "teams_channel")
    assert {a["type"] for a in card["actions"]} == {"Action.OpenUrl"}
    assert [a["title"] for a in card["actions"]] == ["Merge", "Wait", "Answer with text"]
    assert card["actions"][0].get("style") == "positive"
    parsed, err = parse_card(card)
    assert err is None and parsed is not None
    rows = [b for b in card["body"] if b["type"] == "Container"]
    assert [r["style"] for r in rows] == ["good", "emphasis"]
    assert rows[0]["items"][0]["text"] == "RECOMMENDED"


def test_link_card_has_the_web_card_layout_in_the_recipient_language() -> None:
    """#1156: kicker, question, context, one row per option with its effect, deadline, open action; fr."""
    row = _decision(deadline=datetime(2026, 11, 30, 9, 0, tzinfo=UTC), source_type="work_item", source_id=DID)
    card = links.link_card(row, ALICE, "teams_chat", "fr", "Décision sur une tâche de Assistant")
    texts = [b.get("text") for b in card["body"] if b["type"] == "TextBlock"]
    assert texts[:3] == ["Décision sur une tâche de Assistant", "Merge the pull request now?", "CI is green."]
    assert texts[-1] == "Répondre avant: 2026-11-30 09:00 UTC"
    rows = [b for b in card["body"] if b["type"] == "Container"]
    assert [i["text"] for i in rows[0]["items"]] == ["RECOMMANDÉE", "**A.** Merge", "squash-merged now"]
    assert [i["text"] for i in rows[1]["items"]] == ["**B.** Wait", "kept open"]
    titles = [a["title"] for a in card["actions"]]
    assert titles == ["Merge", "Wait", "Répondre par écrit", "Ouvrir dans XTM One"]
    assert card["actions"][-1]["url"] == f"https://one.example.test/tasks/work-items/{DID}"
    parsed, err = parse_card(card)
    assert err is None and parsed is not None


@pytest.mark.parametrize("language", ["en", "es", "ja"])
def test_link_card_words_follow_the_language(language: str) -> None:
    from app.services.channel_notices import notice

    card = links.link_card(_decision(), ALICE, "teams_chat", language)
    assert card["actions"][-1]["title"] == notice("card_answer_with_text", language)
    rows = [b for b in card["body"] if b["type"] == "Container"]
    assert rows[0]["items"][0]["text"] == notice("card_recommended", language).upper()


def test_link_text_carries_one_link_per_option() -> None:
    text = links.link_text(_decision(), ALICE, "email")
    assert text.startswith("Merge the pull request now?")
    assert text.count("https://one.example.test/decide/") == 3


def test_decided_state_names_who_which_and_when() -> None:
    when = datetime(2026, 11, 22, 1, 0, tzinfo=UTC)
    row = _decision(status="answered", answer={"option": "A"}, answered_at=when, surface="teams_chat")
    state = links.decided_state(row, "Alice")
    assert state == {
        "status": "answered",
        "option": "A",
        "label": "Merge",
        "text": None,
        "answered_by": "Alice",
        "answered_at": when.isoformat(),
        "surface": "teams_chat",
    }


def test_decided_card_names_the_answer_and_carries_no_button() -> None:
    when = datetime(2026, 11, 22, 1, 0, tzinfo=UTC)
    row = _decision(status="answered", answer={"option": "A"}, answered_at=when, surface="teams_chat")
    card = links.decided_card(row, "Alice")
    assert "actions" not in card
    assert card["body"][1]["text"] == "Answered: Merge, by Alice, 2026-11-22 01:00 UTC"
    parsed, err = parse_card(card)
    assert err is None and parsed is not None


def test_decided_card_of_a_written_answer_shows_the_text() -> None:
    row = _decision(status="answered", answer={"text": "Wait for Monday"}, answered_at=None)
    card = links.decided_card(row, None)
    assert card["body"][1]["text"] == "Answered: a written answer"
    assert card["body"][2]["text"] == "Wait for Monday"


def test_decided_card_of_a_cancelled_decision_takes_no_answer() -> None:
    card = links.decided_card(_decision(status="cancelled"), None)
    assert card["body"][1]["text"] == "This decision is cancelled: it takes no answer."
