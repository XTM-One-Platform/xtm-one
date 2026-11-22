# SPDX-License-Identifier: Apache-2.0

"""Human gates S9b (#842): link cards sent through a Microsoft account and their decided update.

human-gates.md I.4 paths (b) and (c): the card is posted by the agent's own
Graph tool (``agent_graph_caller``), a ``notified`` event names the message,
and once decided each message is edited to the decided card as its sender; a
refused edit leaves the link page as the decided view. Graph and the
database are stubbed here; the PostgreSQL path is in the e2e slice.
"""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import settings
from app.integrations.providers.microsoft._cards import parse_card
from app.services import decision_graph_cards as cards
from app.services import work_item_notify as notify

DID = uuid.UUID("0f1d2c3b-4a59-4687-9a1b-2c3d4e5f6a7b")
AGENT = uuid.UUID("aaaaaaaa-1111-4222-8333-444444444444")
OWNER = uuid.UUID("bbbbbbbb-1111-4222-8333-444444444444")
ALICE = uuid.UUID("11111111-2222-4333-8444-555555555555")


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
            "options": [{"key": "A", "label": "Merge"}, {"key": "B", "label": "Wait"}],
            "recommendation": "A",
        },
        "deadline": None,
        "status": "open",
        "answer": None,
        "answered_at": None,
        "answered_by": None,
        "surface": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


class _Db:
    def __init__(self, decision: Any = None, notified: list[dict[str, Any]] | None = None) -> None:
        self.decision = decision
        self.notified = notified or []
        self.added: list[Any] = []
        self.commits = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def get(self, model: Any, key: Any) -> Any:
        return self.decision

    async def execute(self, stmt: Any) -> Any:
        rows = self.notified
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


def _caller(answers: dict[str, tuple[bool, str]], seen: list[tuple[Any, str, str, dict[str, Any]]]):
    @asynccontextmanager
    async def factory(sender: Any, integration: str):
        async def call(tool: str, arguments: dict[str, Any]) -> tuple[bool, str]:
            seen.append((sender, integration, tool, arguments))
            if tool not in answers:
                raise notify.NotifyError(f"{tool} denied")
            return answers[tool]

        yield call

    return factory


# --- send ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "tool", "surface"),
    [
        ({"chat_id": "19:abc@thread.v2"}, "send_microsoft_chat_message", "teams_chat"),
        ({"team_id": "t1", "channel_id": "19:c@thread.tacv2"}, "send_microsoft_channel_message", "teams_channel"),
    ],
)
async def test_send_posts_the_link_card_as_the_agent_and_records_notified(target, tool, surface):
    db, seen = _Db(), []
    answer = (
        "Message sent to channel. Message ID: 1716. Sent at: x."
        if "channel" in tool
        else "Chat message sent. Message ID: 1716. Sent at: x."
    )
    message_id = await cards.send_link_card(
        db,
        _decision(),
        agent_id=AGENT,
        user_id=OWNER,
        integration="Office account",
        recipient_id=ALICE,
        target=target,
        caller=_caller({tool: (True, answer)}, seen),
    )
    assert message_id == "1716"
    sender, integration, used, arguments = seen[0]
    assert (sender.created_by_agent_id, sender.user_id, integration, used) == (AGENT, OWNER, "Office account", tool)
    # The card passes the Graph check of a message posted as a user: Action.OpenUrl alone.
    parsed, err = parse_card(arguments["card"])
    assert err is None and parsed is not None
    assert {a["type"] for a in arguments["card"]["actions"]} == {"Action.OpenUrl"}
    assert all("/decide/" in a["url"] for a in arguments["card"]["actions"])
    event = db.added[0]
    assert (event.event, event.surface) == ("notified", surface)
    assert event.detail["via"] == cards.VIA and event.detail["message_id"] == "1716"
    assert event.detail["recipient_id"] == str(ALICE) and db.commits == 1


async def test_refused_send_records_nothing():
    db, seen = _Db(), []
    with pytest.raises(notify.NotifyError):
        await cards.send_link_card(
            db,
            _decision(),
            agent_id=AGENT,
            user_id=OWNER,
            integration="x",
            recipient_id=ALICE,
            target={"chat_id": "19:abc@thread.v2"},
            caller=_caller({"send_microsoft_chat_message": (False, "Bad request: card refused")}, seen),
        )
    assert db.added == [] and db.commits == 0


async def test_send_without_a_target_is_refused_before_any_call():
    with pytest.raises(notify.PermanentNotifyError):
        await cards.send_link_card(
            _Db(), _decision(), agent_id=AGENT, user_id=OWNER, integration="x", recipient_id=ALICE, target={}
        )


# --- decided update ------------------------------------------------------------------


def _sent(**over: Any) -> dict[str, Any]:
    base = {
        "via": cards.VIA,
        "agent_id": str(AGENT),
        "user_id": str(OWNER),
        "integration": "Office account",
        "chat_id": "19:abc@thread.v2",
        "message_id": "1716",
    }
    base.update(over)
    return base


async def test_answered_decision_patches_every_sent_card_to_the_decided_card():
    decided = _decision(status="answered", answer={"option": "A"}, answered_at=datetime(2026, 11, 22, 3, 0, tzinfo=UTC))
    channel = _sent(chat_id=None, team_id="t1", channel_id="19:c@thread.tacv2", message_id="99")
    channel.pop("chat_id")
    db, seen = _Db(decided, [_sent(), channel, {"via": "other", "message_id": "5"}]), []
    counts = await cards.update_sent_cards(
        db,
        DID,
        answered_by_name="Alice",
        caller=_caller(
            {
                "update_microsoft_chat_message": (True, "Chat message updated. Message ID: 1716."),
                "update_microsoft_channel_message": (True, "Channel message updated. Message ID: 99."),
            },
            seen,
        ),
    )
    assert counts == {"updated": 2, "refused": 0}
    assert [s[2] for s in seen] == ["update_microsoft_chat_message", "update_microsoft_channel_message"]
    chat_args = seen[0][3]
    assert chat_args["message_id"] == "1716" and chat_args["chat_id"] == "19:abc@thread.v2"
    card = chat_args["card"]
    assert "actions" not in card
    assert any("Merge" in b["text"] and "Alice" in b["text"] for b in card["body"])
    assert seen[1][3]["team_id"] == "t1" and seen[1][3]["message_id"] == "99"
    # Edited as their sender: the same agent, person and integration.
    assert seen[0][0].created_by_agent_id == str(AGENT) and seen[0][1] == "Office account"


async def test_refused_edit_is_counted_and_never_raised():
    decided = _decision(status="cancelled")
    db, seen = _Db(decided, [_sent(), _sent(message_id="2")]), []
    counts = await cards.update_sent_cards(
        db,
        DID,
        caller=_caller({"update_microsoft_chat_message": (False, "Forbidden: not the sender")}, seen),
    )
    assert counts == {"updated": 0, "refused": 2}


async def test_open_decision_is_never_patched():
    db, seen = _Db(_decision(), [_sent()]), []
    counts = await cards.update_sent_cards(db, DID, caller=_caller({}, seen))
    assert counts == {"updated": 0, "refused": 0} and seen == []


async def test_has_sent_cards_reads_only_this_module_events():
    assert await cards.has_sent_cards(_Db(notified=[_sent()]), DID) is True
    assert await cards.has_sent_cards(_Db(notified=[{"via": "other", "message_id": "1"}]), DID) is False


def test_message_id_of():
    assert cards.message_id_of("Chat message sent. Message ID: 1716. Sent at: now.") == "1716"
    assert cards.message_id_of("nothing") is None
