"""A new conversation's AI title is generated once its first turn is done.

The title is a second LLM call (~1-3 s). Run before the ``done`` frame, it
kept the composer in its working state after the answer had finished, on the
first exchange of every conversation. ``_generate_title_after_done``
runs it after ``done`` on sessions of its own, replaces only the provisional
title the turn set, and tells the owner's sockets with a
``conversation_title`` frame: the other ones refetched the list at ``done``,
before the title existed, and have no other way to learn it.
"""

from __future__ import annotations

import uuid

import pytest

from app.api.v1 import chat as chat_module


class _Result:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _Session:
    def __init__(self, log: list, rowcount: int) -> None:
        self.log = log
        self.rowcount = rowcount

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, statement):
        self.log.append(statement)
        return _Result(self.rowcount)

    async def commit(self):
        self.log.append("commit")


def _install(monkeypatch, *, rowcount: int, title: str | None, fail: bool = False) -> list:
    log: list = []
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session(log, rowcount))

    async def _generate(llm, user_text, assistant_text, model=None):
        if fail:
            raise RuntimeError("provider down")
        log.append(("llm", user_text, assistant_text, model))
        return title

    monkeypatch.setattr("app.services.conversation_memory.generate_conversation_title", _generate)
    return log


async def _run(send, conversation_id: uuid.UUID) -> None:
    await chat_module._generate_title_after_done(
        conversation_id=conversation_id,
        user_id="user-1",
        provisional_title="hello there",
        user_text="hello there",
        assistant_text="Hi! How can I help?",
        prebuilt_llm=(object(), "model-a"),
        send=send,
        origin="socket-1",
    )


@pytest.fixture
def sent():
    frames: list[dict] = []

    async def _send(frame: dict) -> bool:
        frames.append(frame)
        return True

    _send.frames = frames
    return _send


@pytest.fixture
def published(monkeypatch) -> list[tuple[str, dict, str]]:
    """What the conversation's other sockets are sent through its channel."""
    frames: list[tuple[str, dict, str]] = []
    monkeypatch.setattr(
        chat_module.chat_turn_stream,
        "publish_live",
        lambda conversation_id, frame, *, origin: frames.append((conversation_id, frame, origin)),
    )
    return frames


async def test_the_title_replaces_the_provisional_one_and_every_socket_is_told(
    monkeypatch, sent, published
):
    log = _install(monkeypatch, rowcount=1, title="Greeting")
    conversation_id = uuid.uuid4()

    await _run(sent, conversation_id)

    assert log[0] == ("llm", "hello there", "Hi! How can I help?", "model-a")
    statement, commit = log[1], log[2]
    assert commit == "commit"
    params = statement.compile().params
    assert params["title"] == "Greeting"
    assert "hello there" in params.values(), "only the provisional title is replaced"
    frame = {"type": "conversation_title", "conversation_id": str(conversation_id), "title": "Greeting"}
    assert sent.frames == [frame]
    # The owner's other tab or device refetched the list at ``done`` and still
    # shows the provisional title; the relay skips this socket's own origin.
    assert published == [(str(conversation_id), frame, "socket-1")]


async def test_a_rename_made_meanwhile_is_kept(monkeypatch, sent, published):
    _install(monkeypatch, rowcount=0, title="Greeting")

    await _run(sent, uuid.uuid4())

    assert sent.frames == []
    assert published == []


async def test_a_failed_generation_changes_nothing_and_never_raises(monkeypatch, sent, published):
    log = _install(monkeypatch, rowcount=1, title=None, fail=True)

    await _run(sent, uuid.uuid4())

    assert log == []
    assert sent.frames == []
    assert published == []


async def test_no_title_leaves_the_provisional_one(monkeypatch, sent, published):
    log = _install(monkeypatch, rowcount=1, title="")

    await _run(sent, uuid.uuid4())

    assert [entry for entry in log if entry != "commit" and not isinstance(entry, tuple)] == []
    assert sent.frames == []
    assert published == []
