"""The outcome of a scheduled one-off reads as one sentence, never the raw tool result.

The conversation and the bell get what happened, where and when in the
person's time zone (or, for an item that did not run, why and one way to fix
it); the raw result, with its chat, message and channel ids, stays in
``item.result``, on the card and in the run. An instruction's answer is the
agent's own prose and follows the sentence. The outcome message carries no
second card marker: the scheduling turn's card updates in place.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import scheduled_work_items as swi

PARIS = "Europe/Paris"
DUE = datetime(2026, 11, 9, 6, 0, tzinfo=UTC)  # 07:00 in Paris (CET)
RAW_CHAT = (
    "Chat created. Chat ID: 19:abc123def@unq.gbl.spaces. Type: oneOnOne. "
    "Members: someone@example.com. Initial message sent."
)
RAW_CHANNEL = "Message sent to channel. Message ID: 1794204053146. Sent at: 2026-11-09T06:00:02Z."
_ID_SHAPES = (
    re.compile(r"19:[^\s]+@"),
    re.compile(r"\b\d{10,}\b"),
    re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
    re.compile(r"@unq\.gbl\.spaces"),
)


def _item(**overrides):
    base = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        owner_agent_id=None,
        created_by_agent_id=None,
        title="Send the morning note to Dave",
        payload={
            "action": {
                "mode": "exact",
                "tool": "send_microsoft_chat_message",
                "arguments": {"chat_id": "19:abc123def@unq.gbl.spaces", "message": "Hi"},
                "target": "Teams chat with Dave Giles",
                "notify": True,
            }
        },
        status="done",
        due_at=DUE,
        completed_at=DUE,
        result=RAW_CHAT,
        action_mode="exact",
        run_id=uuid.uuid4(),
        conversation_id=None,
        outcome_code=None,
        needs_human=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _has_no_id(text: str) -> bool:
    return not any(shape.search(text) for shape in _ID_SHAPES)


@pytest.mark.parametrize(
    ("tool", "raw"),
    [
        ("send_microsoft_chat_message", RAW_CHAT),
        ("create_microsoft_chat", RAW_CHAT),
        ("send_microsoft_channel_message", RAW_CHANNEL),
    ],
)
def test_an_exact_done_outcome_is_one_sentence_with_no_id(tool, raw):
    item = _item(result=raw)
    item.payload["action"]["tool"] = tool
    text = swi.outcome_text(item, PARIS)
    assert "\n" not in text
    assert text.startswith("**Done**: Send the morning note to Dave (Teams chat with Dave Giles)")
    assert "07:00 Paris on Mon 9 Nov 2026" in text
    assert _has_no_id(text)
    assert "Chat created" not in text and "Message sent" not in text
    # The raw proof is untouched on the item.
    assert item.result == raw


def test_an_instruction_done_outcome_keeps_the_agent_answer_as_written():
    answer = "Three incidents are open; the summary is posted in the channel."
    item = _item(action_mode="instruction", payload={"action": {"mode": "instruction", "prompt": "x"}}, result=answer)
    text = swi.outcome_text(item, PARIS)
    head, body = text.split("\n\n", 1)
    assert head.startswith("**Done**: Send the morning note to Dave, at 07:00 Paris")
    assert body == answer


@pytest.mark.parametrize("mode", ["exact", "instruction"])
def test_a_blocked_outcome_gives_the_reason_and_one_fix_never_the_raw_error(mode):
    raw = "'send_microsoft_chat_message' returned an error, so it may not have been done: 403 chat 19:abc@unq.gbl.spaces"
    item = _item(status="blocked", outcome_code=swi.OUTCOME_EXECUTION, result=raw, action_mode=mode)
    text = swi.outcome_text(item, PARIS)
    assert text.startswith("**Not run**: ")
    assert "because the tool or the agent returned an error." in text
    assert "Open the run for the error" in text
    assert _has_no_id(text) and "403" not in text


def test_every_blocked_code_has_a_reason_and_a_fix():
    codes = {
        value
        for name, value in vars(swi).items()
        if name.startswith("OUTCOME_") and isinstance(value, str) and value != swi.OUTCOME_MISSED
    }
    assert codes <= set(swi._BLOCKED_REASONS)
    item = _item(status="blocked", outcome_code="something_new")
    assert "Open the run for the details." in swi.outcome_text(item, None)


@pytest.mark.parametrize("mode", ["exact", "instruction"])
def test_a_missed_outcome_says_why_and_how_to_fix_it(mode):
    item = _item(
        status="blocked",
        outcome_code=swi.OUTCOME_MISSED,
        completed_at=None,
        action_mode=mode,
        result="It was due at Mon 9 Nov 2026 07:00 Europe/Paris (CET, UTC+01:00) = 06:00 UTC and was not run.",
    )
    text = swi.outcome_text(item, PARIS)
    assert text.startswith("**Missed**: ")
    assert "due at 07:00 Paris on Mon 9 Nov 2026" in text
    assert "Reschedule it to run it now, or cancel it." in text
    assert "UTC+01:00" not in text


def test_without_a_time_zone_the_time_is_utc():
    assert "06:00 UTC on Mon 9 Nov 2026" in swi.outcome_text(_item(), None)


class _Rows:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Session:
    """Answers the item read, then the conversation read, in order."""

    def __init__(self, *answers):
        self._answers = list(answers)
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _statement):
        return _Rows(self._answers.pop(0))

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_the_conversation_and_the_bell_get_the_same_readable_sentence_once(monkeypatch):
    conversation = SimpleNamespace(id=uuid.uuid4(), channel="web", channel_thread_id=None)
    item = _item(conversation_id=conversation.id)
    posted: list[str] = []
    notified: list[dict] = []

    metadata: list[dict] = []

    async def _store(_db, _conv_id, role, content, channel="web", metadata_json=None, **_kw):
        posted.append(content)
        metadata.append(json.loads(metadata_json))

    async def _notify(_db, _user_id, **kwargs):
        notified.append(kwargs)

    async def _user(_db, _uid):
        return SimpleNamespace(timezone=PARIS)

    async def _no_publish(_item):
        return None

    session = _Session(item, conversation)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    monkeypatch.setattr("app.channels.router.store_message", _store)
    monkeypatch.setattr("app.services.notify.notify_web_user", _notify)
    monkeypatch.setattr(swi, "_user", _user)
    monkeypatch.setattr(swi, "publish_update", _no_publish)
    await swi.deliver_outcome(item.id)

    assert session.committed
    assert len(posted) == 1
    message = posted[0]
    # One readable line and the link to the run; no second card marker.
    assert swi.WORK_ITEM_MARKER_PREFIX not in message
    sentence, link = message.split("\n\n")
    # Absolute: a chat embedded in OpenCTI, OpenAEV or OpenCRQ never resolves
    # it against its own host (#11950).
    assert link == f"[View the run]({swi.run_url(item.run_id)})"
    assert link.startswith("[View the run](http")
    # The run as data, which the web chat draws as its own action.
    assert metadata == [
        {"scheduled_outcome": {"work_item_id": str(item.id), "run_id": str(item.run_id), "state": "done"}}
    ]
    assert _has_no_id(sentence)
    assert len(notified) == 1
    assert notified[0]["summary"] == sentence.replace("**", "")
    assert item.result == RAW_CHAT


def test_the_run_link_is_absolute_on_the_web_app_url(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "frontend_url", "https://one.example.com/")
    assert swi.run_url("r1") == "https://one.example.com/operations/executions/r1"
    assert swi.run_link_markdown("r1") == "\n\n[View the run](https://one.example.com/operations/executions/r1)"
    assert swi.run_link_markdown(None) == ""


@pytest.mark.asyncio
async def test_a_teams_copy_of_the_outcome_carries_the_absolute_run_link(monkeypatch):
    conversation = SimpleNamespace(id=uuid.uuid4(), channel="teams", channel_thread_id="t1")
    item = _item(conversation_id=conversation.id)
    sent: list[str] = []

    async def _store(*_a, **_kw):
        return None

    async def _send(**kwargs):
        sent.append(kwargs["text"])

    async def _user(_db, _uid):
        return SimpleNamespace(timezone=PARIS)

    async def _no_publish(_item):
        return None

    async def _notify(*_a, **_kw):
        return None

    monkeypatch.setattr("app.core.database.async_session", lambda: _Session(item, conversation))
    monkeypatch.setattr("app.channels.router.store_message", _store)
    monkeypatch.setattr("app.services.notify.notify_web_user", _notify)
    monkeypatch.setattr("app.services.sub_agent._send_proactive_channel_message", _send)
    monkeypatch.setattr(swi, "_user", _user)
    monkeypatch.setattr(swi, "publish_update", _no_publish)
    await swi.deliver_outcome(item.id)

    assert len(sent) == 1
    assert sent[0].endswith(f"[View the run]({swi.run_url(item.run_id)})")
