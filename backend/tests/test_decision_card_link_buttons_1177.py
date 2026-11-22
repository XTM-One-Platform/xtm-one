# SPDX-License-Identifier: Apache-2.0

"""#1177: one link button per object of a decision's ask, on every card surface.

The ask's ``links`` (a pull request, a ticket, a page) become open-url buttons
before "Open in XTM One" on the Graph link card, the decided card, the email
text, the Teams bot card, Google Chat and Slack; a label the platform derived
from the URL is worded in the reader's language, a label the agent wrote is
kept. A work item whose ask names no source gets the pull requests and issues
it tracks. Pure, no database.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import settings
from app.integrations.providers.microsoft._cards import parse_card
from app.models.decision_hooks import work_item_opening
from app.services import decision_answer_links as links
from app.services.channel_notices import notice
from app.services.decision_ask import (
    MAX_CARD_LINKS,
    card_link_label,
    card_links,
    reference_links,
    source_links,
    with_reference_links,
)

DID = uuid.UUID("0f1d2c3b-4a59-4687-9a1b-2c3d4e5f6a7b")
ALICE = uuid.UUID("11111111-2222-4333-8444-555555555555")
PR = "https://github.com/OpenCTI-Platform/opencti/pull/20153"
ISSUE = "https://github.com/XTM-One-Platform/xtm-one/issues/1177"
TICKET = "https://filigran.atlassian.net/browse/ITS-42"


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "secret_key", "link-buttons-test-secret-0123456789abcdef")
    monkeypatch.setattr(settings, "decision_link_previous_secrets", "")
    monkeypatch.setattr(settings, "frontend_url", "https://one.example.test/")


def _ask(**over: Any) -> dict[str, Any]:
    ask: dict[str, Any] = {
        "question": "Merge the pull request now?",
        "options": [{"key": "A", "label": "Merge"}, {"key": "B", "label": "Wait"}],
        "recommendation": "A",
        "links": [{"label": "Open PR #20153", "url": PR}, {"label": "Open the ticket", "url": TICKET}],
    }
    ask.update(over)
    return ask


def _decision(**over: Any) -> Any:
    base = {
        "id": DID,
        "ask": _ask(),
        "deadline": None,
        "status": "open",
        "answer": None,
        "answered_at": None,
        "surface": None,
        "source_type": "work_item",
        "source_id": DID,
    }
    base.update(over)
    return SimpleNamespace(**base)


# --- The labels ----------------------------------------------------------------


def test_a_label_the_agent_wrote_is_kept() -> None:
    assert card_link_label({"label": "Open PR #20153", "url": PR}, "fr") == "Open PR #20153"


@pytest.mark.parametrize("language", ["en", "es", "fr", "ja"])
def test_a_derived_label_is_worded_in_the_reader_language(language: str) -> None:
    derived = source_links(f"see {PR}, {ISSUE} and {TICKET}")
    labels = [card_link_label(link, language) for link in derived]
    assert labels == [
        notice("card_link_pull_request", language, number="20153"),
        notice("card_link_issue", language, number="1177"),
        notice("card_link_ticket_key", language, key="ITS-42"),
    ]


def test_card_links_keeps_http_links_only_and_at_most_four() -> None:
    many = [{"label": f"Doc {i}", "url": f"https://example.test/{i}"} for i in range(6)]
    assert len(card_links({"links": many})) == MAX_CARD_LINKS
    assert card_links({"links": [{"label": "x", "url": "javascript:alert(1)"}]}) == []
    assert card_links(None) == []


# --- Derived from the item's references --------------------------------------


def test_reference_links_reads_refs_and_payload_without_duplicates() -> None:
    refs = [
        {"repo": "opencti-platform/opencti", "kind": "pull_request", "number": 20153},
        {"repo": "xtm-one-platform/xtm-one", "kind": "issue", "number": 1177},
        {"repo": "xtm-one-platform/xtm-one", "kind": "workflow_run", "workflow": "ci.yml"},
    ]
    payload = {"pull_requests": [{"repo": "opencti-platform/opencti", "number": 20153, "url": PR}]}
    urls = [link["url"] for link in reference_links(refs, payload)]
    assert urls == [
        "https://github.com/opencti-platform/opencti/pull/20153",
        "https://github.com/xtm-one-platform/xtm-one/issues/1177",
    ]


def test_with_reference_links_never_replaces_the_ask_links() -> None:
    refs = [{"repo": "a/b", "kind": "pull_request", "number": 3}]
    assert with_reference_links(_ask(), refs)["links"][0]["url"] == PR
    bare = with_reference_links({"question": "Go?"}, refs)
    assert bare == {"question": "Go?", "links": [{"label": "b PR #3", "url": "https://github.com/a/b/pull/3"}]}
    assert with_reference_links({"question": "Go?"}, []) == {"question": "Go?"}


def test_a_work_item_decision_gets_the_pull_requests_it_tracks() -> None:
    item = SimpleNamespace(
        ask={"question": "Merge it?", "options": [{"key": "A", "label": "Merge"}, {"key": "B", "label": "Wait"}]},
        blocked_reason=None,
        title="t",
        waits_for_user_id=ALICE,
        user_id=ALICE,
        blocked_on_refs=[{"repo": "a/b", "kind": "pull_request", "number": 3}],
        payload={},
    )
    kind, ask, _, _ = work_item_opening(item)
    assert kind == "work_item"
    assert ask["links"] == [{"label": "b PR #3", "url": "https://github.com/a/b/pull/3"}]
    assert card_links(ask, "en") == [{"label": "Open PR #3", "url": "https://github.com/a/b/pull/3"}]


# --- The surfaces --------------------------------------------------------------


def test_the_link_card_shows_the_objects_before_open_in_xtm_one() -> None:
    card = links.link_card(_decision(), ALICE, "teams_chat", "en")
    titles = [a["title"] for a in card["actions"]]
    assert titles == ["Merge", "Wait", "Answer with text", "Open PR #20153", "Open the ticket", "Open in XTM One"]
    assert card["actions"][3]["url"] == PR
    assert card["actions"][4]["url"] == TICKET
    parsed, err = parse_card(card)
    assert err is None and parsed is not None


def test_the_decided_card_keeps_the_object_buttons() -> None:
    row = _decision(status="answered", answer={"option": "A"}, answered_at=datetime(2026, 11, 22, 1, 0, tzinfo=UTC))
    card = links.decided_card(row, "Alice")
    assert [a["title"] for a in card["actions"]] == ["Open PR #20153", "Open the ticket"]
    assert all(a["type"] == "Action.OpenUrl" for a in card["actions"])
    parsed, err = parse_card(card)
    assert err is None and parsed is not None


async def test_the_sent_cards_are_replaced_in_the_recipient_language() -> None:
    """``update_sent_cards`` words the kept object buttons in the recipient's language."""
    from contextlib import asynccontextmanager

    from app.services import decision_graph_cards as cards

    derived = {"question": "Merge it?", "options": [{"key": "A", "label": "Merge"}], "links": source_links(PR)}
    row = _decision(
        ask=derived,
        status="answered",
        answer={"option": "A"},
        answered_at=datetime(2026, 11, 22, 1, 0, tzinfo=UTC),
        recipient=f"user:{ALICE}",
    )
    sent = {"via": cards.VIA, "message_id": "7", "chat_id": "19:abc@thread.v2", "integration": "i"}

    class _Db:
        async def get(self, model: Any, key: Any) -> Any:
            return row

        async def scalar(self, stmt: Any) -> str:
            return "fr-fr"  # User.language as stored

        async def execute(self, stmt: Any) -> Any:
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [sent]))

    seen: list[dict[str, Any]] = []

    @asynccontextmanager
    async def factory(sender: Any, integration: str):
        async def call(tool: str, arguments: dict[str, Any]) -> tuple[bool, str]:
            seen.append(arguments)
            return True, "updated"

        yield call

    await cards.update_sent_cards(_Db(), DID, answered_by_name="Alice", caller=factory)
    assert seen, "the sent card was not updated"
    titles = [a["title"] for a in seen[0]["card"]["actions"]]
    assert titles == [notice("card_link_pull_request", "fr", number="20153")]


def test_a_decided_card_without_links_still_has_no_button() -> None:
    row = _decision(ask=_ask(links=[]), status="cancelled")
    assert "actions" not in links.decided_card(row, None)


def test_the_email_text_lists_the_objects_after_the_answers() -> None:
    text = links.link_text(_decision(), ALICE, "email")
    lines = text.splitlines()
    assert lines[-2:] == [f"Open PR #20153: {PR}", f"Open the ticket: {TICKET}"]


def test_the_google_chat_message_lists_the_objects() -> None:
    from app.services.google_chat_decision_cards import link_message

    message = link_message(_decision(), ALICE, "en")
    assert f"<{PR}|Open PR #20153>" in message
    assert f"<{TICKET}|Open the ticket>" in message
