"""Idle parent resume when a background task becomes terminal.

The parent loop is parked only while the same turn is still running.
These tests cover the remaining gap: a finishing child must wake an idle
parent (or fold into an in-flight parent) and must not wake cancelled,
suppressed, or incomplete tasks.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.parent_wake import (
    PARENT_WAKE_SOURCE,
    format_parent_wake_prompt,
    should_wake_parent,
)
from tests.agentic_gate_stub import stub_agentic_gate


@pytest.fixture(autouse=True)
def _agentic_ai_available(monkeypatch):
    """A wake turn asks the background gate once its text is persisted;
    these tests are about what the wake does once it may run."""
    stub_agentic_gate(monkeypatch)


def _task(
    *,
    status: str = "completed",
    conversation_id: uuid.UUID | None = None,
    metadata: dict | None = None,
    user_id: uuid.UUID | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        conversation_id=conversation_id if conversation_id is not None else uuid.uuid4(),
        user_id=user_id or uuid.uuid4(),
        metadata_json=metadata,
        result=None,
        error=None,
    )


def test_format_parent_wake_prompt_completed():
    task_id = str(uuid.uuid4())
    text = format_parent_wake_prompt(
        task_id=task_id,
        agent_name="Researcher",
        terminal_status="completed",
        result_text="CNIL dossier filed. Judicial PDF is on the private page.",
    )
    assert task_id in text
    assert "Researcher" in text
    assert "completed" in text
    assert "CNIL dossier filed" in text
    assert "Do not wait passively" in text
    assert "Do not mention extracts" in text
    assert "full result available on the task" not in text
    assert "get_task_result only if" not in text


def test_format_parent_wake_prompt_includes_full_result():
    text = format_parent_wake_prompt(
        task_id="abc",
        agent_name="CSIRT Orchestrator",
        terminal_status="completed",
        result_text="All four work packages executed.",
    )
    assert "Full specialist result:" in text
    assert "All four work packages executed." in text
    assert "get_full_task_result" not in text


def test_format_parent_wake_prompt_truncated_requires_full_read():
    from app.services.parent_wake import _PARENT_WAKE_RESULT_CHARS

    body = "Z" * (_PARENT_WAKE_RESULT_CHARS + 50)
    text = format_parent_wake_prompt(
        task_id="abc",
        agent_name="CSIRT",
        terminal_status="completed",
        result_text=body,
    )
    assert "get_full_task_result(abc)" in text
    assert "Z" * 40 in text
    assert ("Z" * (_PARENT_WAKE_RESULT_CHARS + 1)) not in text


def test_format_parent_wake_prompt_failed():
    text = format_parent_wake_prompt(
        task_id="abc",
        agent_name="Writer",
        terminal_status="failed",
        error_text="Grafana 502 on the 200-status sweep",
    )
    assert "failed" in text
    assert "Writer" in text
    assert "Grafana 502" in text


def test_should_wake_parent_completed_with_conversation():
    assert should_wake_parent(_task(status="completed")) is True


def test_should_wake_parent_failed_still_wakes():
    assert should_wake_parent(_task(status="failed")) is True


def test_should_wake_parent_skips_no_conversation():
    t = _task()
    t.conversation_id = None
    assert should_wake_parent(t) is False


def test_should_wake_parent_skips_none():
    assert should_wake_parent(None) is False


def test_should_wake_parent_skips_cancelled():
    assert should_wake_parent(_task(status="cancelled")) is False


def test_should_wake_parent_skips_incomplete():
    assert should_wake_parent(_task(status="incomplete")) is False


def test_should_wake_parent_skips_delivery_suppressed():
    assert (
        should_wake_parent(
            _task(status="completed", metadata={"delivery_suppressed": True})
        )
        is False
    )


def test_should_wake_parent_skips_already_woken():
    assert (
        should_wake_parent(_task(status="completed", metadata={"parent_woken": True}))
        is False
    )


def test_should_wake_parent_disable_chat_child_still_wakes():
    """disable_chat skips the user-facing dump, not the parent resume."""
    assert should_wake_parent(_task(status="completed", metadata={"other": 1})) is True


def test_should_not_dump_extract_when_parent_will_wake():
    from app.services.sub_agent import _should_deliver_completion_notice

    task = _task(status="completed")
    assert (
        _should_deliver_completion_notice(
            task, disable_chat=False, has_response=True
        )
        is False
    )


def test_should_dump_extract_only_when_parent_will_not_wake():
    from app.services.sub_agent import _should_deliver_completion_notice

    already_woken = _task(status="completed", metadata={"parent_woken": True})
    assert (
        _should_deliver_completion_notice(
            already_woken, disable_chat=False, has_response=True
        )
        is True
    )
    assert (
        _should_deliver_completion_notice(
            _task(status="completed"), disable_chat=True, has_response=True
        )
        is False
    )
    no_conv = _task(status="completed")
    no_conv.conversation_id = None
    assert (
        _should_deliver_completion_notice(
            no_conv, disable_chat=False, has_response=True
        )
        is False
    )


class _FakeResult:
    def __init__(self, one_row=None):
        self._one = one_row

    def scalar_one_or_none(self):
        return self._one


class _FakeSession:
    def __init__(self, results: list):
        self._results = list(results)
        self.committed = False
        self.execute_calls = 0

    async def execute(self, _stmt):
        self.execute_calls += 1
        if self._results:
            return self._results.pop(0)
        return _FakeResult(None)

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _conv(**kwargs):
    defaults = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        channel="web",
        channel_thread_id=None,
        tool_overrides_json=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_wake_skips_when_task_already_woken(monkeypatch):
    from app.services.parent_wake import _wake_parent_conversation

    task = _task(metadata={"parent_woken": True})
    session = _FakeSession([_FakeResult(task)])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    run_mock = AsyncMock()
    monkeypatch.setattr(
        "app.services.parent_wake._run_parent_wake_turn", run_mock
    )

    await _wake_parent_conversation(
        task_id=str(task.id),
        conversation_id=str(task.conversation_id),
        user_id=str(task.user_id),
        agent_name="Agent",
        terminal_status="completed",
    )
    run_mock.assert_not_called()


@pytest.mark.asyncio
async def test_wake_skips_missing_conversation(monkeypatch):
    from app.services.parent_wake import _wake_parent_conversation

    task = _task()
    session = _FakeSession([_FakeResult(task), _FakeResult(None)])
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    run_mock = AsyncMock()
    monkeypatch.setattr(
        "app.services.parent_wake._run_parent_wake_turn", run_mock
    )

    await _wake_parent_conversation(
        task_id=str(task.id),
        conversation_id=str(task.conversation_id),
        user_id=str(task.user_id),
        agent_name="Agent",
        terminal_status="completed",
    )
    run_mock.assert_not_called()


@pytest.mark.asyncio
async def test_idle_wake_runs_parent_chat(monkeypatch):
    from app.services.parent_wake import _run_parent_wake_turn, format_parent_wake_prompt

    conv = _conv()
    user_id = str(conv.user_id)
    task_id = str(uuid.uuid4())
    wake_text = format_parent_wake_prompt(
        task_id=task_id, agent_name="Researcher", terminal_status="completed"
    )
    history_msg = SimpleNamespace(role="user", content=wake_text)

    store_calls: list[str] = []

    async def _store(*_args, **kwargs):
        store_calls.append(str(kwargs.get("role") or "unknown"))

    async def _release(*_args, **_kwargs):
        store_calls.append("release")
        return []

    store = AsyncMock(side_effect=_store)
    acquire = AsyncMock(return_value=("token-1", False))
    release = AsyncMock(side_effect=_release)
    run_chat = AsyncMock(return_value=("Assessed the result.", None))
    history = AsyncMock(return_value=[history_msg])
    publish = AsyncMock()
    agent = SimpleNamespace(id=conv.agent_id, enabled=True, name="Parent")

    session = _FakeSession([_FakeResult(agent)])
    # Trace commits too: polling only sees the assistant row once it is
    # COMMITTED, so a release slipped between store and commit would
    # reopen the stuck-Thinking window while still passing a
    # store-then-release assertion.
    orig_commit = session.commit

    async def _commit():
        store_calls.append("commit")
        await orig_commit()

    session.commit = _commit

    monkeypatch.setattr("app.channels.router.store_message", store)
    monkeypatch.setattr(
        "app.services.chat_turn_guard.acquire_or_fold", acquire
    )
    monkeypatch.setattr(
        "app.services.chat_turn_guard.release_turn_and_drain", release
    )
    monkeypatch.setattr(
        "app.core.database.release_db_connection_before_loop", AsyncMock()
    )
    monkeypatch.setattr(
        "app.channels.router.get_conversation_history", history
    )
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", run_chat)
    monkeypatch.setattr("app.services.sub_agent.publish_task_event", publish)

    await _run_parent_wake_turn(
        session,
        conversation=conv,
        user_id=user_id,
        wake_text=wake_text,
        persist_user=True,
        followup_depth=0,
        task_id=task_id,
        activity_user_id=user_id,
    )

    assert store.await_count == 2
    user_call = store.await_args_list[0]
    assert user_call.kwargs["role"] == "user"
    assert json.loads(user_call.kwargs["metadata_json"])["hidden"] is True
    assert json.loads(user_call.kwargs["metadata_json"])["source"] == PARENT_WAKE_SOURCE
    assert store.await_args_list[1].kwargs["role"] == "assistant"
    assert store.await_args_list[1].kwargs["content"] == "Assessed the result."
    assert store_calls == ["user", "commit", "assistant", "commit", "release"]
    run_chat.assert_awaited_once()
    acquire.assert_awaited_once()
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_wake_turn_registers_every_stored_file_of_the_conversation(monkeypatch):
    """A file uploaded with a folded message is in the follow-up turn's file list.

    A follow-up answers the first folded message; the uploads of the
    message being answered live in the file store, so the turn registers
    the conversation's stored files like the REST and web turns do, or the
    model sees the images with no file_id to pass to a tool.
    """
    from app.llm.base import FileRef
    from app.services.conversation_files import StoredConversationFile
    from app.services.parent_wake import _run_parent_wake_turn

    conv = _conv()
    user_id = str(conv.user_id)
    earlier = FileRef(
        file_id=str(uuid.uuid4()),
        filename="earlier.pdf",
        content_type="application/pdf",
        size=10,
        s3_object_name="chat/x",
    )
    shot_a = FileRef(
        file_id=str(uuid.uuid4()), filename="a.png", content_type="image/png", size=19471, s3_object_name="chat/x"
    )
    shot_b = FileRef(
        file_id=str(uuid.uuid4()), filename="b.png", content_type="image/png", size=150515, s3_object_name="chat/x"
    )
    stored = [
        StoredConversationFile(ref=earlier, bound=True),
        StoredConversationFile(ref=shot_a, bound=True),
        StoredConversationFile(ref=shot_b, bound=True),
    ]
    read_store = AsyncMock(return_value=stored)
    seen: dict = {}

    async def _run_chat(*_args, **kwargs):
        ctx = kwargs["file_context"]
        seen["ids"] = {ref.file_id for ref in ctx.list_files()}
        return ("Answered.", None)

    agent = SimpleNamespace(id=conv.agent_id, enabled=True, name="Parent")
    session = _FakeSession([_FakeResult(agent)])
    monkeypatch.setattr("app.services.parent_wake.conversation_file_refs", read_store)
    monkeypatch.setattr("app.channels.router.store_message", AsyncMock())
    monkeypatch.setattr("app.services.chat_turn_guard.acquire_or_fold", AsyncMock(return_value=("token-1", False)))
    monkeypatch.setattr("app.services.chat_turn_guard.release_turn_and_drain", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.core.database.release_db_connection_before_loop", AsyncMock())
    monkeypatch.setattr(
        "app.channels.router.get_conversation_history",
        AsyncMock(return_value=[SimpleNamespace(role="user", content="Look at these")]),
    )
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", AsyncMock(side_effect=_run_chat))
    monkeypatch.setattr("app.services.sub_agent.publish_task_event", AsyncMock())

    await _run_parent_wake_turn(
        session,
        conversation=conv,
        user_id=user_id,
        wake_text="Look at these",
        persist_user=False,
        followup_depth=0,
        task_id="steering-follow-up",
        activity_user_id=user_id,
        wake_from_platform=False,
    )

    read_store.assert_awaited_once_with(conv.id)
    assert {earlier.file_id, shot_a.file_id, shot_b.file_id} <= seen["ids"]


@pytest.mark.asyncio
async def test_a_wake_turn_relays_each_answer_chunk_as_a_stream_frame(monkeypatch):
    """A resumed turn (a cut turn's resume, any wake) has no socket: its page
    follows it through the publisher. It handed the loop ``on_event`` only, so
    the page drew the resumed turn's steps live but its text only once stored,
    minutes later (#10910). Each answer chunk is now offered as a ``stream``
    frame, as a web chat send relays it."""
    from app.services import chat_turn_stream
    from app.services.parent_wake import _run_parent_wake_turn

    conv = _conv()
    offered: list[dict] = []

    class _Publisher:
        def __init__(self, *_a, **_kw):
            self.closed = False

        def start(self, ack: dict[str, object] | None = None) -> None:
            return None

        def offer(self, frame):
            offered.append(frame)

        async def on_event(self, _event):
            return None

        async def aclose(self, **_kw):
            self.closed = True

    async def _run_chat(*_args, **kwargs):
        on_token = kwargs.get("on_token")
        assert on_token is not None, "the wake turn must stream its answer"
        for chunk in ("Resumed ", "answer."):
            await on_token(chunk)
        return ("Resumed answer.", None)

    agent = SimpleNamespace(id=conv.agent_id, enabled=True, name="Parent")
    monkeypatch.setattr(chat_turn_stream, "TurnPublisher", _Publisher)
    monkeypatch.setattr("app.services.parent_wake.conversation_file_refs", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.channels.router.store_message", AsyncMock())
    monkeypatch.setattr("app.services.chat_turn_guard.acquire_or_fold", AsyncMock(return_value=("token-1", False)))
    monkeypatch.setattr("app.services.chat_turn_guard.release_turn_and_drain", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.core.database.release_db_connection_before_loop", AsyncMock())
    monkeypatch.setattr(
        "app.channels.router.get_conversation_history",
        AsyncMock(return_value=[SimpleNamespace(role="user", content="go on")]),
    )
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", AsyncMock(side_effect=_run_chat))
    monkeypatch.setattr("app.services.sub_agent.publish_task_event", AsyncMock())

    await _run_parent_wake_turn(
        _FakeSession([_FakeResult(agent)]),
        conversation=conv,
        user_id=str(conv.user_id),
        wake_text="go on",
        persist_user=False,
        followup_depth=0,
        task_id="cut-turn-resume",
        activity_user_id=str(conv.user_id),
        wake_from_platform=True,
    )

    assert [f["content"] for f in offered if f.get("type") == "stream"] == ["Resumed ", "answer."]


@pytest.mark.asyncio
async def test_a_channel_wake_turn_is_not_streamed(monkeypatch):
    """A channel conversation's answer is delivered whole, after the output
    guard: the wake streams a web conversation only (#10910)."""
    from app.services.parent_wake import _run_parent_wake_turn

    conv = _conv(channel="teams")
    seen: dict = {}

    async def _run_chat(*_args, **kwargs):
        seen["on_token"] = kwargs.get("on_token")
        return ("Answered.", None)

    agent = SimpleNamespace(id=conv.agent_id, enabled=True, name="Parent")
    monkeypatch.setattr("app.services.parent_wake.conversation_file_refs", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.channels.router.store_message", AsyncMock())
    monkeypatch.setattr("app.services.chat_turn_guard.acquire_or_fold", AsyncMock(return_value=("token-1", False)))
    monkeypatch.setattr("app.services.chat_turn_guard.release_turn_and_drain", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.core.database.release_db_connection_before_loop", AsyncMock())
    monkeypatch.setattr(
        "app.channels.router.get_conversation_history",
        AsyncMock(return_value=[SimpleNamespace(role="user", content="go on")]),
    )
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", AsyncMock(side_effect=_run_chat))
    monkeypatch.setattr("app.services.sub_agent.publish_task_event", AsyncMock())

    await _run_parent_wake_turn(
        _FakeSession([_FakeResult(agent)]),
        conversation=conv,
        user_id=str(conv.user_id),
        wake_text="go on",
        persist_user=False,
        followup_depth=0,
        task_id="cut-turn-resume",
        activity_user_id=str(conv.user_id),
        wake_from_platform=True,
    )

    assert "on_token" in seen and seen["on_token"] is None


@pytest.mark.asyncio
async def test_a_resume_cut_again_is_queued_before_its_interrupted_close_and_release(monkeypatch):
    """The resume is queued while it holds the conversation, so the
    conversation never reads as interrupted with nothing queued (the window
    in which the notice offered the Resume click that made it stand down)."""
    from app.core.worker_shutdown import WIND_DOWN_CANCEL_MESSAGE, TurnWoundDown
    from app.services import chat_turn_stream
    from app.services.parent_wake import _run_parent_wake_turn

    conv = _conv()
    order: list[str] = []

    class _Publisher:
        def __init__(self, *_a, **_kw):
            self.closed = False

        def start(self, ack: dict[str, object] | None = None) -> None:
            return None

        def offer(self, _frame):
            return None

        def __getattr__(self, _name):
            return lambda *_a, **_kw: None

        async def aclose(self, *, terminal=None):
            order.append("interrupted" if terminal else "closed")
            self.closed = True

    async def _release(*_args, **_kwargs):
        order.append("release")
        return []

    async def _on_cut(token):
        order.append(f"queued:{token}")

    agent = SimpleNamespace(id=conv.agent_id, enabled=True, name="Parent")
    monkeypatch.setattr(chat_turn_stream, "TurnPublisher", _Publisher)
    monkeypatch.setattr("app.channels.router.store_message", AsyncMock())
    monkeypatch.setattr("app.services.chat_turn_guard.acquire_or_fold", AsyncMock(return_value=("token-1", False)))
    monkeypatch.setattr("app.services.chat_turn_guard.release_turn_and_drain", AsyncMock(side_effect=_release))
    monkeypatch.setattr("app.core.database.release_db_connection_before_loop", AsyncMock())
    monkeypatch.setattr("app.channels.router.get_conversation_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "app.services.agent_chat.run_agent_chat", AsyncMock(side_effect=TurnWoundDown(WIND_DOWN_CANCEL_MESSAGE))
    )
    monkeypatch.setattr("app.services.sub_agent.publish_task_event", AsyncMock())

    with pytest.raises(asyncio.CancelledError):
        await _run_parent_wake_turn(
            _FakeSession([_FakeResult(agent)]),
            conversation=conv,
            user_id=str(conv.user_id),
            wake_text="research the quarterly risks",
            persist_user=False,
            followup_depth=0,
            task_id="cut-turn-resume",
            activity_user_id=str(conv.user_id),
            on_cut=_on_cut,
        )

    assert order[:3] == ["queued:token-1", "interrupted", "release"]


@pytest.mark.asyncio
async def test_a_resume_that_failed_is_not_queued_again(monkeypatch):
    """Only a platform stop's cancellation queues the resume again."""
    from app.services.parent_wake import _run_parent_wake_turn

    conv = _conv()
    on_cut = AsyncMock()
    agent = SimpleNamespace(id=conv.agent_id, enabled=True, name="Parent")
    monkeypatch.setattr("app.channels.router.store_message", AsyncMock())
    monkeypatch.setattr("app.services.chat_turn_guard.acquire_or_fold", AsyncMock(return_value=("token-1", False)))
    monkeypatch.setattr("app.services.chat_turn_guard.release_turn_and_drain", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.core.database.release_db_connection_before_loop", AsyncMock())
    monkeypatch.setattr("app.channels.router.get_conversation_history", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", AsyncMock(side_effect=asyncio.CancelledError()))
    monkeypatch.setattr("app.services.sub_agent.publish_task_event", AsyncMock())

    with pytest.raises(asyncio.CancelledError):
        await _run_parent_wake_turn(
            _FakeSession([_FakeResult(agent)]),
            conversation=conv,
            user_id=str(conv.user_id),
            wake_text="research the quarterly risks",
            persist_user=False,
            followup_depth=0,
            task_id="cut-turn-resume",
            activity_user_id=str(conv.user_id),
            on_cut=on_cut,
        )

    on_cut.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_wake_shows_the_integrations_its_turn_did_not_use(monkeypatch):
    """A wake replays the conversation's saved selection; an
    integration the turn did not use for its user is stored on the reply and
    sent in its ``done`` frame, never dropped silently."""
    from app.services.integration_credentials import (
        INTEGRATION_NOT_VISIBLE_REASON,
        UNAVAILABLE_INTEGRATIONS_META_KEY,
    )
    from app.services.parent_wake import _run_parent_wake_turn, format_parent_wake_prompt

    conv = _conv()
    conv.tool_overrides_json = json.dumps(
        {"mcp_server_ids": [str(uuid.uuid4())], "agent_id": str(conv.agent_id)}
    )
    user_id = str(conv.user_id)
    task_id = str(uuid.uuid4())
    wake_text = format_parent_wake_prompt(
        task_id=task_id, agent_name="Researcher", terminal_status="completed"
    )
    withheld = {"provider": "jira", "reason": INTEGRATION_NOT_VISIBLE_REASON}

    async def _run_chat(*_args, **kwargs):
        kwargs["unavailable_integrations_out"].extend([withheld, dict(withheld)])
        return "Assessed.", None

    run_chat = AsyncMock(side_effect=_run_chat)
    store = AsyncMock()
    offered: list[dict] = []
    agent = SimpleNamespace(id=conv.agent_id, enabled=True, name="Parent")
    session = _FakeSession([_FakeResult(agent)])
    monkeypatch.setattr("app.channels.router.store_message", store)
    monkeypatch.setattr(
        "app.services.chat_turn_guard.acquire_or_fold", AsyncMock(return_value=("token-1", False))
    )
    monkeypatch.setattr(
        "app.services.chat_turn_guard.release_turn_and_drain", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("app.core.database.release_db_connection_before_loop", AsyncMock())
    monkeypatch.setattr(
        "app.channels.router.get_conversation_history",
        AsyncMock(return_value=[SimpleNamespace(role="user", content=wake_text)]),
    )
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", run_chat)
    monkeypatch.setattr("app.services.sub_agent.publish_task_event", AsyncMock())
    # The saved server is one the owner may still load.
    monkeypatch.setattr(
        "app.services.chat_selection._refused_ids",
        AsyncMock(return_value={"mcp_server_ids": set(), "custom_tool_ids": set(),
                                "knowledge_base_ids": set(), "integration_ids": set()}),
    )
    monkeypatch.setattr(
        "app.services.chat_turn_stream.TurnPublisher.offer",
        lambda self, frame: offered.append(frame),
    )

    await _run_parent_wake_turn(
        session,
        conversation=conv,
        user_id=user_id,
        wake_text=wake_text,
        persist_user=True,
        followup_depth=0,
        task_id=task_id,
        activity_user_id=user_id,
    )

    assert run_chat.await_args.kwargs["session_mcp_server_ids"] == json.loads(
        conv.tool_overrides_json
    )["mcp_server_ids"]
    assistant = store.await_args_list[-1].kwargs
    assert assistant["role"] == "assistant"
    # Reported once, even when a nested turn reports the same integration.
    assert json.loads(assistant["metadata_json"]) == {
        UNAVAILABLE_INTEGRATIONS_META_KEY: [withheld]
    }
    [done] = [f for f in offered if f.get("type") == "done"]
    assert done[UNAVAILABLE_INTEGRATIONS_META_KEY] == [withheld]


@pytest.mark.asyncio
async def test_channel_wake_releases_the_turn_before_the_outbound_delivery(monkeypatch):
    """A wake on a channel conversation delivers the reply outbound (an HTTP
    call that can take seconds). The marker used to be released only in the
    ``finally``, AFTER that call: a relayed web viewer that handled ``done``
    and refetched saw ``is_processing`` still true and re-adopted a phantom
    "Thinking" for the duration. The turn is released right after ``done``
    is published, before the delivery, and exactly once."""
    from app.services.parent_wake import _run_parent_wake_turn, format_parent_wake_prompt

    conv = _conv(channel="slack", channel_thread_id="C123:1.2")
    user_id = str(conv.user_id)
    task_id = str(uuid.uuid4())
    wake_text = format_parent_wake_prompt(
        task_id=task_id, agent_name="Researcher", terminal_status="completed"
    )
    order: list[str] = []

    async def _store(*_args, **kwargs):
        order.append(str(kwargs.get("role") or "unknown"))

    async def _release(*_args, **_kwargs):
        order.append("release")
        return []

    async def _proactive(**_kwargs):
        order.append("proactive")

    agent = SimpleNamespace(id=conv.agent_id, enabled=True, name="Parent")
    session = _FakeSession([_FakeResult(agent)])
    monkeypatch.setattr("app.channels.router.store_message", AsyncMock(side_effect=_store))
    monkeypatch.setattr(
        "app.services.chat_turn_guard.acquire_or_fold", AsyncMock(return_value=("token-1", False))
    )
    monkeypatch.setattr(
        "app.services.chat_turn_guard.release_turn_and_drain", AsyncMock(side_effect=_release)
    )
    monkeypatch.setattr("app.core.database.release_db_connection_before_loop", AsyncMock())
    monkeypatch.setattr(
        "app.channels.router.get_conversation_history",
        AsyncMock(return_value=[SimpleNamespace(role="user", content=wake_text)]),
    )
    monkeypatch.setattr(
        "app.services.agent_chat.run_agent_chat", AsyncMock(return_value=("Assessed.", None))
    )
    monkeypatch.setattr("app.services.sub_agent.publish_task_event", AsyncMock())
    monkeypatch.setattr(
        "app.services.sub_agent._send_proactive_channel_message", AsyncMock(side_effect=_proactive)
    )

    await _run_parent_wake_turn(
        session,
        conversation=conv,
        user_id=user_id,
        wake_text=wake_text,
        persist_user=True,
        followup_depth=0,
        task_id=task_id,
        activity_user_id=user_id,
    )

    assert order == ["user", "assistant", "release", "proactive"]


@pytest.mark.asyncio
async def test_wake_retries_a_failed_early_release_in_its_finally(monkeypatch):
    """The early release right after ``done`` can fail on a transient Redis
    error. The turn must not be marked released on that failure, or the
    ``finally`` skips its own attempt and the marker stays held until its
    TTL: the ``finally`` retries, and the release happens exactly once."""
    from app.services.parent_wake import _run_parent_wake_turn, format_parent_wake_prompt

    conv = _conv()
    user_id = str(conv.user_id)
    task_id = str(uuid.uuid4())
    wake_text = format_parent_wake_prompt(
        task_id=task_id, agent_name="Researcher", terminal_status="completed"
    )
    attempts: list[str] = []

    async def _release(*_args, **_kwargs):
        attempts.append("release")
        if len(attempts) == 1:
            raise ConnectionError("redis blip")
        return []

    agent = SimpleNamespace(id=conv.agent_id, enabled=True, name="Parent")
    session = _FakeSession([_FakeResult(agent)])
    monkeypatch.setattr("app.channels.router.store_message", AsyncMock())
    monkeypatch.setattr(
        "app.services.chat_turn_guard.acquire_or_fold", AsyncMock(return_value=("token-1", False))
    )
    monkeypatch.setattr(
        "app.services.chat_turn_guard.release_turn_and_drain", AsyncMock(side_effect=_release)
    )
    monkeypatch.setattr("app.core.database.release_db_connection_before_loop", AsyncMock())
    monkeypatch.setattr(
        "app.channels.router.get_conversation_history",
        AsyncMock(return_value=[SimpleNamespace(role="user", content=wake_text)]),
    )
    monkeypatch.setattr(
        "app.services.agent_chat.run_agent_chat", AsyncMock(return_value=("Assessed.", None))
    )
    monkeypatch.setattr("app.services.sub_agent.publish_task_event", AsyncMock())

    await _run_parent_wake_turn(
        session,
        conversation=conv,
        user_id=user_id,
        wake_text=wake_text,
        persist_user=True,
        followup_depth=0,
        task_id=task_id,
        activity_user_id=user_id,
    )

    assert attempts == ["release", "release"]


@pytest.mark.asyncio
async def test_cancelled_wake_still_releases_turn_lock(monkeypatch):
    """A CancelledError mid-loop (worker shutdown) must still release the
    turn lock — ``except Exception`` does not catch it, and a stranded
    marker keeps the frontend Thinking indicator up for a full TTL."""
    from app.services.parent_wake import _run_parent_wake_turn

    conv = _conv()
    store = AsyncMock()
    acquire = AsyncMock(return_value=("token-1", False))
    release = AsyncMock(return_value=[])
    run_chat = AsyncMock(side_effect=asyncio.CancelledError())
    history = AsyncMock(return_value=[])
    publish = AsyncMock()
    agent = SimpleNamespace(id=conv.agent_id, enabled=True, name="Parent")
    session = _FakeSession([_FakeResult(agent)])

    monkeypatch.setattr("app.channels.router.store_message", store)
    monkeypatch.setattr(
        "app.services.chat_turn_guard.acquire_or_fold", acquire
    )
    monkeypatch.setattr(
        "app.services.chat_turn_guard.release_turn_and_drain", release
    )
    monkeypatch.setattr(
        "app.core.database.release_db_connection_before_loop", AsyncMock()
    )
    monkeypatch.setattr(
        "app.channels.router.get_conversation_history", history
    )
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", run_chat)
    monkeypatch.setattr("app.services.sub_agent.publish_task_event", publish)

    with pytest.raises(asyncio.CancelledError):
        await _run_parent_wake_turn(
            session,
            conversation=conv,
            user_id=str(conv.user_id),
            wake_text="wake",
            persist_user=False,
            followup_depth=0,
            task_id=str(uuid.uuid4()),
            activity_user_id=str(conv.user_id),
        )

    release.assert_awaited_once()
    # No assistant row on cancellation — the turn never produced one.
    store.assert_not_awaited()


@pytest.mark.asyncio
async def test_in_flight_wake_folds_without_running_chat(monkeypatch):
    from app.services.parent_wake import _run_parent_wake_turn

    conv = _conv()
    store = AsyncMock()
    acquire = AsyncMock(return_value=(None, True))
    run_chat = AsyncMock()

    monkeypatch.setattr("app.channels.router.store_message", store)
    monkeypatch.setattr(
        "app.services.chat_turn_guard.acquire_or_fold", acquire
    )
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", run_chat)

    session = _FakeSession([])
    await _run_parent_wake_turn(
        session,
        conversation=conv,
        user_id=str(conv.user_id),
        wake_text="wake",
        persist_user=True,
        followup_depth=0,
        task_id=str(uuid.uuid4()),
        activity_user_id=str(conv.user_id),
    )

    store.assert_awaited_once()
    assert store.await_args.kwargs["role"] == "user"
    run_chat.assert_not_called()


@pytest.mark.asyncio
async def test_a_wake_holds_channel_turns_off_until_it_folds(monkeypatch):
    """A channel turn drains nothing folded into it: the wake waits it
    out and holds the next one off from before its message is stored until
    its fold is handed over."""
    from app.services import channel_continuation
    from app.services.parent_wake import _run_parent_wake_turn

    conv = _conv()
    order: list[str] = []

    async def _hold(conversation_id, *, wait_seconds=0.0, before_wait=None):
        order.append(f"hold:{wait_seconds}")
        return "hold-1", None

    async def _release_hold(_conversation_id, hold):
        order.append(f"release:{hold}")

    async def _store(*_args, **_kwargs):
        order.append("store")

    async def _acquire(*_args, **_kwargs):
        order.append("fold")
        return None, True

    monkeypatch.setattr(channel_continuation, "hold_off_channel_turns", _hold)
    monkeypatch.setattr(channel_continuation, "release_channel_hold", _release_hold)
    monkeypatch.setattr("app.channels.router.store_message", AsyncMock(side_effect=_store))
    monkeypatch.setattr("app.services.chat_turn_guard.acquire_or_fold", AsyncMock(side_effect=_acquire))
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", AsyncMock())

    await _run_parent_wake_turn(
        _FakeSession([]),
        conversation=conv,
        user_id=str(conv.user_id),
        wake_text="wake",
        persist_user=True,
        followup_depth=0,
        task_id=str(uuid.uuid4()),
        activity_user_id=str(conv.user_id),
    )

    assert order == [
        f"hold:{channel_continuation.WAKE_CHANNEL_TURN_WAIT_SECONDS}",
        "store",
        "fold",
        "release:hold-1",
    ]


@pytest.mark.asyncio
async def test_schedule_parent_wake_if_needed_skips_incomplete(monkeypatch):
    from app.services.sub_agent import _schedule_parent_wake_if_needed

    sched = MagicMock()
    monkeypatch.setattr("app.services.parent_wake.schedule_parent_wake", sched)
    _schedule_parent_wake_if_needed(
        _task(status="incomplete"),
        agent_name="Agent",
        terminal_status="incomplete",
    )
    sched.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_parent_wake_if_needed_schedules_completed(monkeypatch):
    from app.services.sub_agent import _schedule_parent_wake_if_needed

    sched = MagicMock()
    monkeypatch.setattr("app.services.parent_wake.schedule_parent_wake", sched)
    task = _task(status="completed")
    _schedule_parent_wake_if_needed(
        task, agent_name="CSIRT", terminal_status="completed"
    )
    sched.assert_called_once()
    kwargs = sched.call_args.kwargs
    assert kwargs["task_id"] == str(task.id)
    assert kwargs["conversation_id"] == str(task.conversation_id)
    assert kwargs["agent_name"] == "CSIRT"
    assert kwargs["terminal_status"] == "completed"


def test_execute_skips_extract_dump_when_parent_will_wake():
    import inspect

    from app.services import sub_agent as sub_agent_module

    source = inspect.getsource(sub_agent_module._execute_sub_agent_task)
    assert "_should_deliver_completion_notice(" in source


def test_parent_wake_steering_skips_in_flight_wait():
    """A finishing child folded into an in-flight parent must skip
    ``wait()`` immediately, not sit behind a 5-minute pause."""
    import inspect

    from app.services import parent_wake

    source = inspect.getsource(parent_wake._run_parent_wake_turn)
    assert "wait_skip_event.set()" in source
    assert "wait_skip_event=wait_skip_event" in source


def test_a_resumed_cut_turn_takes_a_steer_like_any_running_turn():
    """A message sent while a cut turn resumes reaches that turn (#9650).

    The resume runs through ``_run_parent_wake_turn``, whose bridge drains the
    conversation's steering inbox (where a send folded into the running turn
    lands) into the loop's steering queue and ends any pause, so the next model
    step sees the message instead of a separate turn answering it later.
    """
    import inspect

    from app.services import parent_wake

    resume = inspect.getsource(parent_wake.resume_cut_turn)
    assert "_run_parent_wake_turn(" in resume
    turn = inspect.getsource(parent_wake._run_parent_wake_turn)
    assert "chat_turn_guard.drain_steering_inbox(" in turn
    assert "steering_queue.put_fold(" in turn
    assert "steering_queue=steering_queue" in turn


def test_message_is_hidden_from_metadata():
    from app.api.v1.chat import _message_is_hidden

    hidden = SimpleNamespace(
        metadata_json=json.dumps({"hidden": True, "source": PARENT_WAKE_SOURCE})
    )
    visible = SimpleNamespace(metadata_json=None)
    assert _message_is_hidden(hidden) is True
    assert _message_is_hidden(visible) is False


# ---------------------------------------------------------------------------
# A message folded into a wake turn names agents of its own
# ---------------------------------------------------------------------------

BETA = str(uuid.uuid4())
FOLDED = "@beta your view?"


class _AgentSession:
    """A session whose every lookup finds the conversation's agent."""

    def __init__(self, agent):
        self._agent = agent

    async def execute(self, _stmt):
        return _FakeResult(self._agent)

    async def commit(self):
        return None

    async def rollback(self):
        return None


@pytest.fixture
def folded_wake(monkeypatch):
    """The wake turn on the real turn guard (a fake Redis), its dependencies
    faked: the runs are scripted, what is stored and published is recorded."""
    import app.core.redis as redis_module
    from app.services import chat_turn_guard
    from app.services.mention_dispatch import MentionTarget
    from tests.test_chat_web_steering import _FakeRedisKV

    redis = _FakeRedisKV()

    async def _redis():
        return redis

    monkeypatch.setattr(redis_module, "redis_client", _redis)
    monkeypatch.setattr(chat_turn_guard, "BRIDGE_POLL_SECONDS", 0.02)

    conv = _conv()
    env = SimpleNamespace(
        conv=conv,
        conv_id=str(conv.id),
        user_id=str(conv.user_id),
        stored=[],
        frames=[],
        calls=[],
        on_conversation_run=None,
        beta_gap=None,
    )

    class _Publisher:
        def __init__(self, *_a, **_kw):
            self.closed = False

        def start(self, ack: dict[str, object] | None = None) -> None:
            return None

        async def on_event(self, _event):
            return None

        def offer(self, frame):
            env.frames.append(frame)

        async def aclose(self, terminal=None):
            if terminal is not None and not self.closed:
                env.frames.append(terminal)
            self.closed = True

    async def _store(_db, _conv_id, *, role, content, channel="web", metadata_json=None, **_kw):
        env.stored.append((role, content, json.loads(metadata_json) if metadata_json else {}))
        return SimpleNamespace(id=uuid.uuid4(), metadata_json=metadata_json)

    async def _targets(_db, *, user_id, raw_ids, answering_agent_id, language="en"):
        return [
            MentionTarget(agent_id=BETA, name="Beta", agent=SimpleNamespace(id=uuid.UUID(BETA), name="Beta"))
            for agent_id in raw_ids
            if agent_id == BETA
        ]

    async def _own_session(agent, run):
        return await run(object(), agent)

    async def _conversation_target(_db, agent_id):
        return MentionTarget(agent_id=str(agent_id), name="Parent")

    async def _run(_db, _uid, _agent, _history, content, **kwargs):
        who = "beta" if "allowed_handover_types" in kwargs else "conversation"
        env.calls.append((who, content, kwargs))
        if who == "beta":
            if env.beta_gap:
                kwargs["unavailable_integrations_out"].append(dict(env.beta_gap))
            return "beta says", None
        return await env.on_conversation_run(content, kwargs), None

    async def _no_trace(_conv_id):
        return {}

    monkeypatch.setattr("app.services.chat_turn_stream.TurnPublisher", _Publisher)
    monkeypatch.setattr("app.channels.router.store_message", _store)
    monkeypatch.setattr("app.channels.router.get_conversation_history", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.core.database.release_db_connection_before_loop", AsyncMock())
    monkeypatch.setattr("app.services.sub_agent.publish_task_event", AsyncMock())
    monkeypatch.setattr("app.services.parent_wake.load_result_file_refs", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.services.parent_wake.load_attachment_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.services.mention_dispatch.resolve_mention_targets", _targets)
    monkeypatch.setattr("app.services.parent_wake.run_on_own_session", _own_session)
    monkeypatch.setattr("app.services.parent_wake.conversation_agent_target", _conversation_target)
    monkeypatch.setattr("app.services.langfuse_tracing.load_latest_langfuse_ids", _no_trace)
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _run)
    env.guard = chat_turn_guard
    env.run_turn = lambda wake_text: _run_wake(env, wake_text)
    return env


async def _run_wake(env, wake_text: str) -> None:
    from app.services.parent_wake import _run_parent_wake_turn

    agent = SimpleNamespace(id=env.conv.agent_id, enabled=True, name="Parent")
    await _run_parent_wake_turn(
        _AgentSession(agent),
        conversation=env.conv,
        user_id=env.user_id,
        wake_text=wake_text,
        persist_user=True,
        followup_depth=0,
        task_id=str(uuid.uuid4()),
        activity_user_id=env.user_id,
    )


def _strand_one_fold(monkeypatch, guard) -> None:
    """A fold lands after the wake turn's last drain: stranded at its release."""
    real_release = guard.release_turn_and_drain
    racing = [True]

    async def _release(conversation_id, token):
        if racing:
            racing.clear()
            assert await guard.fold_into_inflight_turn(conversation_id, FOLDED, mentioned_agent_ids=[BETA])
        return await real_release(conversation_id, token)

    monkeypatch.setattr(guard, "release_turn_and_drain", _release)


@pytest.mark.asyncio
async def test_a_message_folded_into_a_wake_turn_is_answered_by_the_agents_it_mentions(folded_wake):
    """The wake's agent reads the folded message as steering; the agent it
    mentions answers it after the wake's own reply, each reply its own message
    and ``done`` frame, attributed."""
    env = folded_wake

    async def _conversation(_content, kwargs):
        assert await env.guard.fold_into_inflight_turn(env.conv_id, FOLDED, mentioned_agent_ids=[BETA])
        await asyncio.wait_for(kwargs["steering_queue"].get(), timeout=2)
        return "Assessed."

    env.on_conversation_run = _conversation
    await env.run_turn("wake")

    assert [(who, content) for who, content, _ in env.calls] == [("conversation", "wake"), ("beta", FOLDED)]
    beta_kwargs = env.calls[1][2]
    assert isinstance(beta_kwargs["quota_out"], dict) and "meter_quota" not in beta_kwargs
    assert [(role, content) for role, content, _ in env.stored] == [
        ("user", "wake"),
        ("assistant", "Assessed."),
        ("assistant", "beta says"),
    ]
    assert env.stored[1][2]["answered_by"]["agent_name"] == "Parent"
    assert env.stored[2][2]["answered_by"]["agent_name"] == "Beta"
    done = [f for f in env.frames if f.get("type") == "done"]
    assert [(f["content"], f.get("intermediate")) for f in done] == [("Assessed.", True), ("beta says", None)]
    assert await env.guard.drain_steering_inbox(env.conv_id) == []
    assert not await env.guard.is_turn_active(env.conv_id)


@pytest.mark.asyncio
async def test_the_wake_of_a_channel_conversation_is_the_turn_of_the_general_assistants_stand_in(
    monkeypatch, folded_wake
):
    """A channel conversation the General Assistant owns stays unbound:
    its wake runs the agent linked to it, with the conversation's saved tools
    as its Slack and web turns do, and that agent is the answerer a
    folded message's mentions are read against - named in the fold, it does
    not answer twice, and the wake's reply is attributed to it."""
    from app.services.mention_dispatch import MentionTarget

    env = folded_wake
    stand_in = SimpleNamespace(id=uuid.uuid4(), enabled=True, disable_chat=False, name="CTEM Assistant")
    env.conv = _conv(
        agent_id=None,
        channel="slack",
        channel_thread_id="C123:1.2",
        tool_overrides_json=json.dumps({"builtin_tool_ids": ["browser"]}),
    )
    env.conv_id, env.user_id = str(env.conv.id), str(env.conv.user_id)
    names = {str(stand_in.id): stand_in.name, BETA: "Beta"}
    answering: list = []
    ran: list = []

    async def _targets(_db, *, user_id, raw_ids, answering_agent_id, language="en"):
        answering.append(answering_agent_id)
        return [
            MentionTarget(agent_id=i, name=names[i], agent=SimpleNamespace(id=uuid.UUID(i), name=names[i]))
            for i in raw_ids
            if i != answering_agent_id
        ]

    async def _conversation_target(_db, agent_id):
        return MentionTarget(agent_id=str(agent_id or ""), name=names.get(str(agent_id)))

    async def _run(_db, _uid, agent, _history, content, **kwargs):
        ran.append((agent.name, content, kwargs))
        if agent is stand_in:
            mentioned = [str(stand_in.id), BETA]
            assert await env.guard.fold_into_inflight_turn(env.conv_id, FOLDED, mentioned_agent_ids=mentioned)
            await asyncio.wait_for(kwargs["steering_queue"].get(), timeout=2)
            return "Assessed.", None
        return f"{agent.name.lower()} says", None

    monkeypatch.setattr(
        "app.services.general_assistant_link.general_assistant_stand_in", AsyncMock(return_value=stand_in)
    )
    monkeypatch.setattr("app.services.general_assistant_link.may_chat_with", AsyncMock(return_value=True))
    monkeypatch.setattr("app.services.mention_dispatch.resolve_mention_targets", _targets)
    monkeypatch.setattr("app.services.parent_wake.conversation_agent_target", _conversation_target)
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _run)
    monkeypatch.setattr("app.services.sub_agent._send_proactive_channel_message", AsyncMock())

    await env.run_turn("wake")

    assert [(name, content) for name, content, _ in ran] == [("CTEM Assistant", "wake"), ("Beta", FOLDED)]
    assert ran[0][2]["session_builtin_tool_ids"] == ["browser"]
    assert answering == [str(stand_in.id)]
    assert env.stored[1][2]["answered_by"]["agent_name"] == "CTEM Assistant"
    assert env.stored[2][2]["answered_by"]["agent_name"] == "Beta"


@pytest.mark.asyncio
async def test_each_wake_reply_names_the_integrations_its_own_agent_could_not_use(folded_wake):
    """The note naming an integration the agent could not use rides the reply
    of the run that met it, never another agent's."""
    env = folded_wake
    parent_gap = {"provider": "github", "reason": "integration_not_visible"}
    env.beta_gap = {"provider": "jira", "reason": "integration_not_visible"}

    async def _conversation(_content, kwargs):
        kwargs["unavailable_integrations_out"].append(dict(parent_gap))
        assert await env.guard.fold_into_inflight_turn(env.conv_id, FOLDED, mentioned_agent_ids=[BETA])
        await asyncio.wait_for(kwargs["steering_queue"].get(), timeout=2)
        return "Assessed."

    env.on_conversation_run = _conversation
    await env.run_turn("wake")

    stored = [meta.get("unavailable_integrations") for role, _content, meta in env.stored if role == "assistant"]
    assert stored == [[parent_gap], [env.beta_gap]]
    done = [f for f in env.frames if f.get("type") == "done"]
    assert [f.get("unavailable_integrations") for f in done] == [[parent_gap], [env.beta_gap]]


@pytest.mark.asyncio
async def test_a_fold_stranded_at_a_wake_turns_end_is_answered_by_its_follow_up(monkeypatch, folded_wake):
    """The follow-up turn answers the stranded message, then the agent it
    mentions does - handed to it by its own acquisition, so the conversation
    run reports its charge and a refusal would ask nobody."""
    env = folded_wake
    _strand_one_fold(monkeypatch, env.guard)
    answers = iter(["Assessed.", "Followed up."])

    async def _conversation(_content, _kwargs):
        return next(answers)

    env.on_conversation_run = _conversation
    await env.run_turn("wake")

    assert [(who, content) for who, content, _ in env.calls] == [
        ("conversation", "wake"),
        ("conversation", FOLDED),
        ("beta", FOLDED),
    ]
    assert env.calls[0][2]["quota_out"] is None
    assert isinstance(env.calls[1][2]["quota_out"], dict)
    assert [content for role, content, _ in env.stored if role == "assistant"] == [
        "Assessed.",
        "Followed up.",
        "beta says",
    ]
    assert await env.guard.drain_steering_inbox(env.conv_id) == []


@pytest.mark.asyncio
async def test_a_fold_left_on_a_wake_turns_queue_is_answered_by_its_follow_up(folded_wake):
    """Queued by the bridge while the loop gave its final answer, a fold is
    one the loop never took: the follow-up answers it, as one stranded at the
    release, and the agent it mentions answers it after."""
    env = folded_wake
    answers = iter(["Assessed.", "Followed up."])

    async def _conversation(_content, kwargs):
        if not env.calls[1:]:
            kwargs["steering_queue"].put_fold(
                FOLDED, env.guard.SteeringPayload(text=FOLDED, mentioned_agent_ids=[BETA])
            )
        return next(answers)

    env.on_conversation_run = _conversation
    await env.run_turn("wake")

    assert [(who, content) for who, content, _ in env.calls] == [
        ("conversation", "wake"),
        ("conversation", FOLDED),
        ("beta", FOLDED),
    ]
    assert [content for role, content, _ in env.stored if role == "assistant"] == [
        "Assessed.",
        "Followed up.",
        "beta says",
    ]
    assert await env.guard.drain_steering_inbox(env.conv_id) == []


@pytest.mark.asyncio
async def test_a_torn_down_wake_turn_hands_its_folds_agents_back(folded_wake):
    """Cancelled mid-run (worker shutdown), the turn cannot ask the agents a
    fold mentions: they go back to the inbox, handed back mentions-only, and
    the lock is released."""
    env = folded_wake

    async def _conversation(_content, kwargs):
        assert await env.guard.fold_into_inflight_turn(env.conv_id, FOLDED, mentioned_agent_ids=[BETA])
        await asyncio.wait_for(kwargs["steering_queue"].get(), timeout=2)
        raise asyncio.CancelledError

    env.on_conversation_run = _conversation
    with pytest.raises(asyncio.CancelledError):
        await env.run_turn("wake")

    assert [who for who, _, _ in env.calls] == ["conversation"]
    [payload] = await env.guard.drain_steering_inbox(env.conv_id)
    assert (payload.text, payload.mentioned_agent_ids, payload.mentions_only) == (FOLDED, [BETA], True)
    assert not await env.guard.is_turn_active(env.conv_id)


@pytest.mark.asyncio
async def test_a_fold_stranded_at_a_torn_down_wake_turns_release_is_handed_back(monkeypatch, folded_wake):
    """A torn-down turn starts no follow-up: the agents a fold stranded at its
    release mentions go back to the inbox, as on a WebSocket or REST turn."""
    env = folded_wake
    _strand_one_fold(monkeypatch, env.guard)

    async def _conversation(_content, _kwargs):
        raise asyncio.CancelledError

    env.on_conversation_run = _conversation
    with pytest.raises(asyncio.CancelledError):
        await env.run_turn("wake")

    [payload] = await env.guard.drain_steering_inbox(env.conv_id)
    assert (payload.text, payload.mentioned_agent_ids, payload.mentions_only) == (FOLDED, [BETA], True)
    assert not await env.guard.is_turn_active(env.conv_id)


@pytest.mark.asyncio
async def test_a_refused_follow_up_keeps_the_stranded_mentions_for_the_next_turn(monkeypatch, folded_wake):
    """Agentic AI went away before the follow-up: it starts nothing, and the
    stranded message's agents wait in the inbox for the next turn to acquire,
    which reads the message in its history."""
    env = folded_wake
    _strand_one_fold(monkeypatch, env.guard)
    asked: list[bool] = []

    async def _refusal(_db):
        asked.append(True)
        return None if len(asked) == 1 else "ai_disabled"

    monkeypatch.setattr("app.services.agentic_gate.agentic_refusal", _refusal)

    async def _conversation(_content, _kwargs):
        return "Assessed."

    env.on_conversation_run = _conversation
    await env.run_turn("wake")

    assert [(who, content) for who, content, _ in env.calls] == [("conversation", "wake")]
    assert len(asked) == 2
    purged: list = []
    token, folded = await env.guard.acquire_or_fold(env.conv_id, "next message", purged_mentions=purged)
    assert token is not None and folded is False
    assert [(p.text, p.mentioned_agent_ids, p.mentions_only) for p in purged] == [(FOLDED, [BETA], True)]


class _StopAwareSession:
    """Answers the stop query with ``stopped_at`` and anything else with the
    bound agent, so a follow-up turn finds its agent again."""

    def __init__(self, *, agent, stopped_at):
        self._agent = agent
        self._stopped_at = stopped_at
        self.stop_reads = 0

    async def execute(self, stmt):
        if [c.name for c in getattr(stmt, "selected_columns", [])] == ["stopped_at"]:
            self.stop_reads += 1
            return _FakeResult(self._stopped_at)
        return _FakeResult(self._agent)

    async def commit(self):
        return None

    async def rollback(self):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(("platform_text", "restored"), [(False, False), (True, True)])
async def test_a_follow_up_restores_only_the_platforms_own_wake_text(monkeypatch, platform_text, restored):
    """The follow-up's text is the first stranded fold's. A message a person
    folded is defused like any they write; a wake the platform wrote is restored."""
    from app.services import chat_turn_guard

    run_chat, _requeue, _stranded, _session = await _run_wake_with_a_stranded_fold(
        monkeypatch,
        stopped_at=None,
        stranded=[chat_turn_guard.SteeringPayload(text="folded text", platform_text=platform_text)],
    )

    assert run_chat.await_count == 2
    first, follow_up = run_chat.await_args_list
    assert first.kwargs["restore_user_message"] is True
    assert follow_up.args[4] == "folded text"
    assert follow_up.kwargs["restore_user_message"] is restored


def test_the_platform_text_flag_survives_the_inbox():
    """Only the wake paths set it; a person's message and an entry from an older replica read as not the platform's."""
    from app.services import chat_turn_guard as guard

    wake, person = guard._decode_entries(
        [guard._encode_entry("[Platform] wake", platform_text=True), guard._encode_entry("hello")]
    )
    assert (wake.platform_text, person.platform_text) == (True, False)
    assert "platform_text" not in json.loads(guard._encode_entry("hello"))


@pytest.mark.asyncio
async def test_a_requeued_wake_keeps_its_platform_text_flag(monkeypatch):
    from app.services import chat_turn_guard as guard

    pushed: list[str] = []

    class _Redis:
        async def rpush(self, _key, value):
            pushed.append(value)

        async def expire(self, *_args):
            return True

    async def _redis():
        return _Redis()

    monkeypatch.setattr(guard, "_redis", _redis)
    await guard.requeue_stranded(
        "c1", [guard.SteeringPayload(text="[Platform] wake", platform_text=True), guard.SteeringPayload(text="hi")]
    )

    assert [payload.platform_text for payload in guard._decode_entries(pushed)] == [True, False]


async def _run_wake_with_a_stranded_fold(monkeypatch, *, stopped_at, stranded=None):
    """One wake turn whose release hands back stranded folds - by default a
    partner's answer, as when it folds into a wake turn the user then stops."""
    from datetime import UTC, datetime, timedelta

    from app.services import chat_turn_guard
    from app.services.parent_wake import _run_parent_wake_turn

    # One clock read for both moments: ``stopped_at`` and ``chain_started_at``
    # are offsets from it, so their order does not depend on how long the
    # patching below takes (a cold import on a loaded worker took over 4 s).
    now = datetime.now(UTC)
    conv = _conv()
    agent = SimpleNamespace(id=conv.agent_id, enabled=True, name="Parent")
    session = _StopAwareSession(
        agent=agent,
        stopped_at=None if stopped_at is None else now + stopped_at,
    )
    if stranded is None:
        stranded = [
            chat_turn_guard.SteeringPayload(
                text="[Platform] late partner answer", partner_wake=True
            )
        ]
    releases = iter([stranded, [], []])

    run_chat = AsyncMock(return_value=("Assessed.", None))
    requeue = AsyncMock()
    monkeypatch.setattr("app.channels.router.store_message", AsyncMock())
    monkeypatch.setattr(
        "app.services.chat_turn_guard.acquire_or_fold",
        AsyncMock(return_value=("token-1", False)),
    )
    monkeypatch.setattr(
        "app.services.chat_turn_guard.release_turn_and_drain",
        AsyncMock(side_effect=lambda *_a, **_k: next(releases)),
    )
    monkeypatch.setattr("app.services.chat_turn_guard.requeue_stranded", requeue)
    monkeypatch.setattr("app.core.database.release_db_connection_before_loop", AsyncMock())
    monkeypatch.setattr(
        "app.channels.router.get_conversation_history", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", run_chat)
    monkeypatch.setattr("app.services.sub_agent.publish_task_event", AsyncMock())
    monkeypatch.setattr(
        "app.services.parent_wake.load_result_file_refs", AsyncMock(return_value=[])
    )

    await _run_parent_wake_turn(
        session,
        conversation=conv,
        user_id=str(conv.user_id),
        wake_text="[Platform] first wake",
        persist_user=True,
        followup_depth=0,
        task_id="a2a-partner",
        activity_user_id=str(conv.user_id),
        chain_started_at=now - timedelta(seconds=5),
    )
    return run_chat, requeue, stranded, session


@pytest.mark.asyncio
async def test_a_stopped_wake_turn_starts_no_follow_up(monkeypatch):
    """The user stopped this wake turn, and a partner answer folded into
    it was stranded. It stays in the history for the next turn the user
    starts; no follow-up turn delivers it on the user's behalf."""
    from datetime import timedelta

    run_chat, requeue, stranded, session = await _run_wake_with_a_stranded_fold(
        monkeypatch, stopped_at=timedelta(seconds=-1)
    )
    assert run_chat.await_count == 1, "a follow-up turn resumed the stopped conversation"
    requeue.assert_awaited_once()
    assert requeue.await_args.args[1] == stranded
    assert session.stop_reads == 1


@pytest.mark.asyncio
async def test_a_stop_before_the_chain_began_blocks_no_follow_up(monkeypatch):
    """A Stop older than the chain of wake turns belongs to an earlier turn:
    the marker must never block later work."""
    from datetime import timedelta

    for stopped_at in (None, timedelta(days=-1)):
        run_chat, requeue, _stranded, _session = await _run_wake_with_a_stranded_fold(
            monkeypatch, stopped_at=stopped_at
        )
        assert run_chat.await_count == 2, "the stranded fold was not followed up"
        requeue.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        # Written while the stopped turn was still unwinding: the send path
        # folded it and told the user this turn would answer it.
        "Actually, look at the other host",
        # A sub-agent task that finished while this turn ran.
        "[Platform] Background task t-2 (CSIRT) completed.",
    ],
)
async def test_a_stop_holds_back_only_a_partner_answer(monkeypatch, text):
    """A message the user sent, or a task's wake, stranded in a wake turn the
    user stopped is followed up as it always was (only a partner's
    answer is held back)."""
    from datetime import timedelta

    from app.services import chat_turn_guard

    run_chat, requeue, _stranded, session = await _run_wake_with_a_stranded_fold(
        monkeypatch,
        stopped_at=timedelta(seconds=-1),
        stranded=[chat_turn_guard.SteeringPayload(text=text)],
    )
    assert run_chat.await_count == 2, "the stranded fold was never answered"
    assert run_chat.await_args_list[1].args[4] == text
    requeue.assert_not_awaited()
    assert session.stop_reads == 0


@pytest.mark.asyncio
async def test_a_stopped_wake_turn_answers_the_user_before_the_partner(monkeypatch):
    """Both stranded: the partner's answer is held back, and the follow-up that
    answers the user leads with the user's message, not with the answer the
    user stopped."""
    from datetime import timedelta

    from app.services import chat_turn_guard

    partner = chat_turn_guard.SteeringPayload(
        text="[Platform] late partner answer", partner_wake=True
    )
    user = chat_turn_guard.SteeringPayload(text="Stop that, check the logs instead")
    run_chat, requeue, _stranded, session = await _run_wake_with_a_stranded_fold(
        monkeypatch, stopped_at=timedelta(seconds=-1), stranded=[partner, user]
    )
    requeue.assert_awaited_once()
    assert requeue.await_args.args[1] == [partner]
    assert run_chat.await_count == 2
    assert run_chat.await_args_list[1].args[4] == user.text
    assert session.stop_reads == 1


@pytest.mark.asyncio
async def test_a_stop_at_the_submission_instant_drops_the_answer(monkeypatch):
    """The cascade cancels work submitted at or before the Stop, so the wake
    must drop an answer on the same boundary: equal timestamps are stopped."""
    from datetime import UTC, datetime

    instant = datetime.now(UTC)
    run_turn = await _wake_before_delivery(
        monkeypatch, db_stopped_at=instant, recorded_stop=None, delegated_at=instant
    )
    run_turn.assert_not_awaited()


async def _wake_before_delivery(monkeypatch, *, db_stopped_at, recorded_stop, delegated_at):
    """Drive the partner wake's pre-delivery gate with the Stop readable from
    the database column and/or the turn guard."""
    from app.services import chat_turn_guard, parent_wake

    conv = SimpleNamespace(id=uuid.uuid4(), stopped_at=db_stopped_at)

    class _Db:
        async def execute(self, stmt):
            if [c.name for c in getattr(stmt, "selected_columns", [])] == ["stopped_at"]:
                return _FakeResult(db_stopped_at)
            return _FakeResult(conv)

        async def rollback(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    run_turn = AsyncMock()
    monkeypatch.setattr("app.core.database.async_session", lambda: _Db())
    monkeypatch.setattr(parent_wake, "_run_turn_for", run_turn)
    monkeypatch.setattr(
        chat_turn_guard,
        "recorded_stop",
        AsyncMock(return_value=recorded_stop),
        raising=False,
    )
    await parent_wake._wake_conversation_with(
        conversation_id=str(conv.id),
        user_id=str(uuid.uuid4()),
        wake_text="[Platform] partner answer",
        delegated_at=delegated_at,
    )
    return run_turn


@pytest.mark.asyncio
async def test_a_stop_only_in_the_turn_guard_keeps_the_answer_out(monkeypatch):
    """The cascade has not committed ``stopped_at`` yet, but the Stop is in the
    turn guard: the answer is neither run nor folded into a turn the user
    started meanwhile."""
    from datetime import UTC, datetime, timedelta

    run_turn = await _wake_before_delivery(
        monkeypatch,
        db_stopped_at=None,
        recorded_stop=datetime.now(UTC),
        delegated_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    run_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_work_sent_after_the_recorded_stop_is_still_delivered(monkeypatch):
    from datetime import UTC, datetime, timedelta

    run_turn = await _wake_before_delivery(
        monkeypatch,
        db_stopped_at=None,
        recorded_stop=datetime.now(UTC) - timedelta(minutes=1),
        delegated_at=datetime.now(UTC),
    )
    run_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_stop_landing_before_the_wake_owns_the_turn_starts_no_turn(monkeypatch):
    """The Stop committed after the wake's first check but before it took the
    turn marker, so ``request_turn_cancel`` found nothing to stop. Once the
    wake owns the turn it re-reads the Stop: the marker is released, what was
    stranded in it is requeued, and no model turn starts."""
    from datetime import UTC, datetime, timedelta

    from app.services import chat_turn_guard
    from app.services.parent_wake import _run_parent_wake_turn

    conv = _conv()
    agent = SimpleNamespace(id=conv.agent_id, enabled=True, name="Parent")
    delegated_at = datetime.now(UTC) - timedelta(minutes=1)
    session = _StopAwareSession(agent=agent, stopped_at=datetime.now(UTC))
    stranded = [chat_turn_guard.SteeringPayload(text="[Platform] folded meanwhile")]
    release = AsyncMock(return_value=stranded)
    run_chat = AsyncMock(return_value=("Assessed.", None))
    requeue = AsyncMock()
    monkeypatch.setattr("app.channels.router.store_message", AsyncMock())
    monkeypatch.setattr(
        "app.services.chat_turn_guard.acquire_or_fold",
        AsyncMock(return_value=("token-1", False)),
    )
    monkeypatch.setattr("app.services.chat_turn_guard.release_turn_and_drain", release)
    monkeypatch.setattr("app.services.chat_turn_guard.requeue_stranded", requeue)
    monkeypatch.setattr("app.core.database.release_db_connection_before_loop", AsyncMock())
    monkeypatch.setattr(
        "app.channels.router.get_conversation_history", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", run_chat)
    monkeypatch.setattr("app.services.sub_agent.publish_task_event", AsyncMock())

    await _run_parent_wake_turn(
        session,
        conversation=conv,
        user_id=str(conv.user_id),
        wake_text="[Platform] partner answer",
        persist_user=True,
        followup_depth=0,
        task_id="a2a-partner",
        activity_user_id=str(conv.user_id),
        delegated_at=delegated_at,
    )
    run_chat.assert_not_awaited()
    release.assert_awaited_once()
    requeue.assert_awaited_once()
    assert requeue.await_args.args[1] == stranded


@pytest.mark.asyncio
async def test_a_stop_at_the_fence_hands_back_the_mentions_the_acquisition_took(monkeypatch):
    """A fold an earlier turn handed back is taken off the inbox by the
    partner wake's acquisition. The Stop turns the partner's answer away, not
    the user's message: its agents are still owed their answer by the next turn."""
    from datetime import UTC, datetime, timedelta

    from app.services import chat_turn_guard
    from app.services.parent_wake import _run_parent_wake_turn

    conv = _conv()
    agent = SimpleNamespace(id=conv.agent_id, enabled=True, name="Parent")
    owed = chat_turn_guard.SteeringPayload(text=FOLDED, mentioned_agent_ids=[BETA], mentions_only=True)

    async def _acquire(*_a, purged_mentions=None, **_kw):
        purged_mentions.append(owed)
        return "token-1", False

    requeue = AsyncMock()
    run_chat = AsyncMock(return_value=("Assessed.", None))
    monkeypatch.setattr("app.channels.router.store_message", AsyncMock())
    monkeypatch.setattr("app.services.chat_turn_guard.acquire_or_fold", _acquire)
    monkeypatch.setattr("app.services.chat_turn_guard.release_turn_and_drain", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.services.chat_turn_guard.requeue_stranded", requeue)
    monkeypatch.setattr("app.core.database.release_db_connection_before_loop", AsyncMock())
    monkeypatch.setattr("app.channels.router.get_conversation_history", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", run_chat)
    monkeypatch.setattr("app.services.sub_agent.publish_task_event", AsyncMock())

    await _run_parent_wake_turn(
        _StopAwareSession(agent=agent, stopped_at=datetime.now(UTC)),
        conversation=conv,
        user_id=str(conv.user_id),
        wake_text="[Platform] partner answer",
        persist_user=True,
        followup_depth=0,
        task_id="a2a-partner",
        activity_user_id=str(conv.user_id),
        delegated_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    run_chat.assert_not_awaited()
    requeue.assert_awaited_once()
    [handed_back] = requeue.await_args.args[1]
    assert (handed_back.text, handed_back.mentioned_agent_ids, handed_back.mentions_only) == (FOLDED, [BETA], True)


async def _run_fenced_wake(monkeypatch, *, db_stopped_at, recorded_stop, **kwargs):
    """A wake turn that takes the marker, with the Stop readable from the
    database (``db_stopped_at``) and/or the turn guard (``recorded_stop``)."""
    from app.services import chat_turn_guard
    from app.services.parent_wake import _run_parent_wake_turn

    conv = _conv()
    agent = SimpleNamespace(id=conv.agent_id, enabled=True, name="Parent")
    session = _StopAwareSession(agent=agent, stopped_at=db_stopped_at)
    run_chat = AsyncMock(return_value=("Assessed.", None))
    monkeypatch.setattr("app.channels.router.store_message", AsyncMock())
    monkeypatch.setattr(
        "app.services.chat_turn_guard.acquire_or_fold",
        AsyncMock(return_value=("token-1", False)),
    )
    monkeypatch.setattr(
        "app.services.chat_turn_guard.release_turn_and_drain", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("app.services.chat_turn_guard.requeue_stranded", AsyncMock())
    monkeypatch.setattr(
        chat_turn_guard,
        "recorded_stop",
        AsyncMock(return_value=recorded_stop),
        raising=False,
    )
    monkeypatch.setattr("app.core.database.release_db_connection_before_loop", AsyncMock())
    monkeypatch.setattr(
        "app.channels.router.get_conversation_history", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", run_chat)
    monkeypatch.setattr("app.services.sub_agent.publish_task_event", AsyncMock())
    monkeypatch.setattr(
        "app.services.parent_wake.load_result_file_refs", AsyncMock(return_value=[])
    )
    await _run_parent_wake_turn(
        session,
        conversation=conv,
        user_id=str(conv.user_id),
        wake_text="[Platform] partner answer",
        task_id="a2a-partner",
        activity_user_id=str(conv.user_id),
        **kwargs,
    )
    return run_chat


@pytest.mark.asyncio
async def test_a_stop_not_committed_yet_is_honoured_once_the_wake_owns_the_turn(
    monkeypatch,
):
    """The Stop was acknowledged and recorded in the turn guard, the cascade
    has not committed ``stopped_at`` yet, and the cross-process cancel found
    no marker. The wake that then takes the turn reads the turn guard's
    record and starts nothing."""
    from datetime import UTC, datetime, timedelta

    run_chat = await _run_fenced_wake(
        monkeypatch,
        db_stopped_at=None,
        recorded_stop=datetime.now(UTC),
        persist_user=True,
        followup_depth=0,
        delegated_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    run_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_partner_only_follow_up_is_fenced_by_its_chain(monkeypatch):
    """A follow-up made only of partner answers carries no submission time: a
    Stop after its chain began, landing between the guard and the follow-up's
    acquisition, stops it all the same."""
    from datetime import UTC, datetime, timedelta

    run_chat = await _run_fenced_wake(
        monkeypatch,
        db_stopped_at=datetime.now(UTC),
        recorded_stop=None,
        persist_user=False,
        followup_depth=1,
        partner_wake=True,
        chain_started_at=datetime.now(UTC) - timedelta(seconds=30),
    )
    run_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_follow_up_with_a_message_the_user_sent_is_not_fenced(monkeypatch):
    """Not a partner-only follow-up: whatever the Stop, a message the user sent
    is answered, as on main."""
    from datetime import UTC, datetime, timedelta

    run_chat = await _run_fenced_wake(
        monkeypatch,
        db_stopped_at=datetime.now(UTC),
        recorded_stop=None,
        persist_user=False,
        followup_depth=1,
        partner_wake=False,
        chain_started_at=datetime.now(UTC) - timedelta(seconds=30),
    )
    run_chat.assert_awaited_once()


def _partner_answer(*, sent_ago):
    """A partner's answer as its fold carries it: marked, with its work's
    submission time."""
    from datetime import UTC, datetime

    from app.services import chat_turn_guard

    return chat_turn_guard.SteeringPayload(
        text="[Platform] partner answer",
        partner_wake=True,
        delegated_at=datetime.now(UTC) - sent_ago,
    )


@pytest.mark.asyncio
async def test_a_stop_after_the_work_was_sent_holds_back_its_stranded_answer(
    monkeypatch,
):
    """The Stop predates this chain of wake turns, so the chain's fence lets
    its folds through - but it came after the work this partner's answer
    delivers was sent. The answer is requeued instead of followed up; the user's
    message stranded in the same drain is still answered."""
    from datetime import timedelta

    from app.services import chat_turn_guard

    monkeypatch.setattr(chat_turn_guard, "recorded_stop", AsyncMock(return_value=None))
    partner = _partner_answer(sent_ago=timedelta(minutes=1))
    user = chat_turn_guard.SteeringPayload(text="Check the logs instead")
    run_chat, requeue, _stranded, session = await _run_wake_with_a_stranded_fold(
        monkeypatch, stopped_at=timedelta(seconds=-30), stranded=[partner, user]
    )
    requeue.assert_awaited_once()
    assert requeue.await_args.args[1] == [partner]
    assert run_chat.await_count == 2
    assert run_chat.await_args_list[1].args[4] == user.text
    assert session.stop_reads == 1


@pytest.mark.asyncio
async def test_the_answer_of_work_sent_after_the_stop_is_followed_up(monkeypatch):
    """Work sent after the Stop is not the work the user stopped."""
    from datetime import timedelta

    from app.services import chat_turn_guard

    monkeypatch.setattr(chat_turn_guard, "recorded_stop", AsyncMock(return_value=None))
    partner = _partner_answer(sent_ago=timedelta(seconds=10))
    run_chat, requeue, _stranded, _session = await _run_wake_with_a_stranded_fold(
        monkeypatch, stopped_at=timedelta(seconds=-30), stranded=[partner]
    )
    requeue.assert_not_awaited()
    assert run_chat.await_count == 2
    assert run_chat.await_args_list[1].args[4] == partner.text


@pytest.mark.asyncio
async def test_a_partner_wake_folds_with_its_submission_time(monkeypatch):
    """The fold carries when the work was sent, what the turn guard
    arbitrates the Stop against. A refused fold starts no turn and leaves no
    lock behind; the answer was persisted before either hand-off."""
    from datetime import UTC, datetime, timedelta

    from app.services import chat_turn_guard
    from app.services.parent_wake import _run_parent_wake_turn

    conv = _conv()
    sent = datetime.now(UTC) - timedelta(minutes=1)
    store = AsyncMock()
    run_chat = AsyncMock(return_value=("Assessed.", None))
    release = AsyncMock(return_value=[])
    monkeypatch.setattr("app.channels.router.store_message", store)
    monkeypatch.setattr("app.services.chat_turn_guard.release_turn_and_drain", release)
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", run_chat)

    async def _wake() -> None:
        await _run_parent_wake_turn(
            _FakeSession([]),
            conversation=conv,
            user_id=str(conv.user_id),
            wake_text="[Platform] partner answer",
            persist_user=True,
            followup_depth=0,
            task_id="a2a-partner",
            activity_user_id=str(conv.user_id),
            partner_wake=True,
            delegated_at=sent,
        )

    folded = AsyncMock(return_value=(None, True))
    monkeypatch.setattr("app.services.chat_turn_guard.acquire_or_fold", folded)
    await _wake()
    assert folded.await_args.kwargs["partner_wake"] is True
    assert folded.await_args.kwargs["delegated_at"] == sent

    refused = AsyncMock(side_effect=chat_turn_guard.FoldRefusedError("stopped"))
    monkeypatch.setattr("app.services.chat_turn_guard.acquire_or_fold", refused)
    await _wake()
    refused.assert_awaited_once()
    run_chat.assert_not_awaited()
    release.assert_not_awaited()
    assert store.await_count == 2


@pytest.mark.asyncio
async def test_the_wake_bridge_does_not_inject_a_stopped_partner_answer(monkeypatch):
    """Folded into a running wake turn before the Stop and drained after
    it, a partner's answer is not injected; the message drained with it is."""
    from datetime import UTC, datetime, timedelta

    from app.services import chat_turn_guard, parent_wake
    from app.services.parent_wake import _run_parent_wake_turn
    from app.tools.agent_loop import steering_text

    conv = _conv()
    agent = SimpleNamespace(id=conv.agent_id, enabled=True, name="Parent")
    stopped = datetime.now(UTC)
    drains = iter(
        [
            [
                chat_turn_guard.SteeringPayload(
                    text="[Platform] partner answer",
                    partner_wake=True,
                    delegated_at=stopped - timedelta(minutes=1),
                ),
                chat_turn_guard.SteeringPayload(text="user message"),
            ]
        ]
    )
    received: list[str] = []

    async def _run_chat(*_args, steering_queue, **_kwargs):
        item = await asyncio.wait_for(steering_queue.get(), timeout=2)
        received.append(steering_text(item))
        return ("Assessed.", None)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(chat_turn_guard, "BRIDGE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(
        chat_turn_guard,
        "drain_steering_inbox",
        AsyncMock(side_effect=lambda *_a: next(drains, [])),
    )
    monkeypatch.setattr(chat_turn_guard, "consume_turn_cancel", AsyncMock(return_value=False))
    monkeypatch.setattr(chat_turn_guard, "consume_wait_skip", AsyncMock(return_value=False))
    monkeypatch.setattr(parent_wake, "_latest_stop", AsyncMock(return_value=stopped))
    monkeypatch.setattr("app.core.database.async_session", _Session)
    monkeypatch.setattr("app.channels.router.store_message", AsyncMock())
    monkeypatch.setattr(
        "app.services.chat_turn_guard.acquire_or_fold",
        AsyncMock(return_value=("token-1", False)),
    )
    monkeypatch.setattr(
        "app.services.chat_turn_guard.release_turn_and_drain", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("app.core.database.release_db_connection_before_loop", AsyncMock())
    monkeypatch.setattr(
        "app.channels.router.get_conversation_history", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _run_chat)
    monkeypatch.setattr("app.services.sub_agent.publish_task_event", AsyncMock())
    monkeypatch.setattr(
        "app.services.parent_wake.load_result_file_refs", AsyncMock(return_value=[])
    )

    await _run_parent_wake_turn(
        _StopAwareSession(agent=agent, stopped_at=None),
        conversation=conv,
        user_id=str(conv.user_id),
        wake_text="[Platform] Background task t-1 (CSIRT) completed.",
        persist_user=True,
        followup_depth=0,
        task_id="t-1",
        activity_user_id=str(conv.user_id),
    )
    assert received == ["user message"]


@pytest.mark.asyncio
async def test_a_drain_holds_back_only_the_answers_of_stopped_work(monkeypatch):
    """The one re-check every drain runs: inclusive at the Stop, like the
    cascade; nothing else held back; no Stop read when nothing could be; and a
    Stop that cannot be read holds nothing back."""
    from datetime import UTC, datetime, timedelta

    from app.services import chat_turn_guard, parent_wake

    stopped = datetime.now(UTC)

    def _partner(text, delegated_at=None):
        return chat_turn_guard.SteeringPayload(
            text=text, partner_wake=True, delegated_at=delegated_at
        )

    at_stop = _partner("sent at the stop", stopped)
    before = _partner("sent before", stopped - timedelta(seconds=1))
    after = _partner("sent after", stopped + timedelta(seconds=1))
    unstamped = _partner("from an older replica")
    user = chat_turn_guard.SteeringPayload(text="user message")
    task_wake = chat_turn_guard.SteeringPayload(
        text="[Platform] Background task t-2 (CSIRT) completed."
    )
    opened: list[object] = []

    class _Session:
        async def __aenter__(self):
            opened.append(self)
            return self

        async def __aexit__(self, *_exc):
            return False

    latest_stop = AsyncMock(return_value=stopped)
    monkeypatch.setattr("app.core.database.async_session", _Session)
    monkeypatch.setattr(parent_wake, "_latest_stop", latest_stop)
    conv_id = str(uuid.uuid4())

    kept = await parent_wake.without_stopped_partner_answers(
        conv_id, [at_stop, user, before, after, unstamped, task_wake]
    )
    assert kept == [user, after, unstamped, task_wake]
    assert len(opened) == 1
    # Read by its UUID, like every other lookup of the conversation.
    assert latest_stop.await_args.args[1] == uuid.UUID(conv_id)

    opened.clear()
    assert await parent_wake.without_stopped_partner_answers(
        conv_id, [user, unstamped, task_wake]
    ) == [user, unstamped, task_wake]
    assert opened == [], "the Stop was read for a drain nothing could be held from"

    monkeypatch.setattr(
        parent_wake, "_latest_stop", AsyncMock(side_effect=RuntimeError("db down"))
    )
    assert await parent_wake.without_stopped_partner_answers(conv_id, [before]) == [before]


@pytest.mark.asyncio
async def test_the_steering_queue_checks_a_partner_answer_again_as_it_is_taken(
    monkeypatch,
):
    """The loop takes a queued answer only at its next iteration boundary,
    and a Stop sent from another socket may be recorded meanwhile. The queue
    admits what it held once more as it is taken, holds back the answers of
    stopped work, and hands them to the caller that replays the pass."""
    from datetime import UTC, datetime, timedelta

    from app.services import chat_turn_guard, parent_wake
    from app.tools.agent_loop import SteeringQueue, drain_steering_queue

    stopped = datetime.now(UTC)
    latest_stop = AsyncMock(return_value=stopped)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr("app.core.database.async_session", _Session)
    monkeypatch.setattr(parent_wake, "_latest_stop", latest_stop)

    queue = parent_wake.PartnerAnswerQueue(str(uuid.uuid4()))
    assert isinstance(queue, SteeringQueue)
    stopped_answer = "[Platform] partner answer"
    fresh_answer = "[Platform] fresh partner answer"
    queue.put_fold(
        stopped_answer,
        chat_turn_guard.SteeringPayload(
            text=stopped_answer,
            partner_wake=True,
            delegated_at=stopped - timedelta(seconds=1),
        ),
    )
    queue.put_fold(
        fresh_answer,
        chat_turn_guard.SteeringPayload(
            text=fresh_answer,
            partner_wake=True,
            delegated_at=stopped + timedelta(seconds=1),
        ),
    )
    queue.put_fold("user message", chat_turn_guard.SteeringPayload(text="user message"))
    queue.put_nowait("same-socket steer")

    admitted = await queue.admit(drain_steering_queue(queue))
    assert admitted == [fresh_answer, "user message", "same-socket steer"]
    assert queue.take_held_back() == [stopped_answer]
    assert queue.take_held_back() == []

    # Nothing queued is a partner's answer any more: no Stop read at all.
    latest_stop.reset_mock()
    queue.put_nowait("another steer")
    assert await queue.admit(drain_steering_queue(queue)) == ["another steer"]
    latest_stop.assert_not_awaited()


class _ConversationThenAgentSession(_AgentSession):
    """Finds the conversation for a lookup of one, the agent for any other."""

    def __init__(self, conv, agent):
        super().__init__(agent)
        self._conv = conv

    async def execute(self, stmt):
        from app.models.chat import Conversation

        if stmt.column_descriptions[0].get("entity") is Conversation:
            return _FakeResult(self._conv)
        return await super().execute(stmt)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


@pytest.mark.asyncio
async def test_a_chat_turns_unanswered_fold_is_answered_by_a_follow_up_turn(monkeypatch, folded_wake):
    """A message folded into a web or REST turn after that turn's last look:
once the turn released the lock, a follow-up turn answers it,
    stores nothing of it again, reads it as a person's message (defused,
    never restored) and leaves the conversation free."""
    from app.services import parent_wake

    env = folded_wake
    agent = SimpleNamespace(id=env.conv.agent_id, enabled=True, name="Parent")
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: _ConversationThenAgentSession(env.conv, agent)
    )

    async def _conversation(_content, _kwargs):
        return "Compared."

    env.on_conversation_run = _conversation
    late = "and compare it with last quarter"

    await parent_wake._answer_unanswered_folds(
        conversation_id=env.conv_id,
        user_id=None,
        folds=[env.guard.SteeringPayload(text=late)],
    )

    assert [(who, content) for who, content, _ in env.calls] == [("conversation", late)]
    assert env.calls[0][2]["restore_user_message"] is False
    assert [(role, content) for role, content, _ in env.stored] == [("assistant", "Compared.")]
    assert not await env.guard.is_turn_active(env.conv_id)
