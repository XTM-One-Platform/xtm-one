# SPDX-License-Identifier: Apache-2.0
"""A chat turn cut by a rollout and answered by its resume is a ``turn_resumed`` friction (#11924).

Past ``PERSON_VISIBLE_CUT_SECONDS`` of waiting the person saw the cut: the
row is written undisposed, never auto-accepted. Under it, or on a platform
wake, the row is written disposed. The rollout counter counts both.
"""

import contextlib
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import frictions, parent_wake, rollout_counters

AGENT = uuid.uuid4()
USER = uuid.uuid4()
RUN = uuid.uuid4()

Recorded = list[tuple[Any, str, str, str, Any]]


def _capture(monkeypatch: pytest.MonkeyPatch) -> tuple[Recorded, list[bool]]:
    recorded: Recorded = []
    counted: list[bool] = []

    async def fake_record(scope: Any, kind: str, tool: str, evidence: str, disposed: Any = None) -> None:
        recorded.append((scope, kind, tool, evidence, disposed))

    def fake_schedule(coro: Coroutine[Any, Any, Any]) -> None:
        with contextlib.suppress(StopIteration):
            coro.send(None)

    def fake_count(*, visible: bool) -> None:
        counted.append(visible)

    monkeypatch.setattr(frictions, "_record_quietly", fake_record)
    monkeypatch.setattr(frictions, "_schedule", fake_schedule)
    monkeypatch.setattr(rollout_counters, "count_chat_turn_cut", fake_count)
    return recorded, counted


def _note(
    *,
    agent_id: object = AGENT,
    entry: str = "web",
    channel: str | None = None,
    waited_seconds: float | None = 190.0,
) -> bool:
    return frictions.note_chat_turn_resumed(
        agent_id=agent_id,
        user_id=USER,
        run_id=RUN,
        conversation_id="c1",
        entry=entry,
        channel=channel,
        waited_seconds=waited_seconds,
        resumes=0,
    )


def test_a_cut_a_person_waited_on_past_the_bound_is_undisposed(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded, counted = _capture(monkeypatch)
    assert _note(waited_seconds=187.0, channel="teams") is True
    assert counted == [True]
    [(scope, kind, tool, evidence, disposed)] = recorded
    assert kind == "turn_resumed" and tool == "" and disposed is None
    assert scope.agent_id == AGENT and scope.run_id == RUN and scope.user_id == USER
    assert evidence.startswith("person-visible cut:") and "channel teams" in evidence and "187 s" in evidence


def test_a_short_cut_is_written_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded, counted = _capture(monkeypatch)
    assert _note(waited_seconds=frictions.PERSON_VISIBLE_CUT_SECONDS - 1) is False
    assert counted == [False]
    [(_, kind, _, evidence, disposed)] = recorded
    assert kind == "turn_resumed" and not evidence.startswith("person-visible")
    assert disposed is not None and disposed[:2] == ("platform_acceptable", "accepted")


def test_a_platform_wake_is_never_person_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded, counted = _capture(monkeypatch)
    assert _note(entry="wake", waited_seconds=600.0) is False
    assert counted == [False] and recorded[0][4] is not None


def test_an_unknown_wait_is_not_counted_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    _, counted = _capture(monkeypatch)
    assert _note(waited_seconds=None) is False
    assert counted == [False]


def test_no_agent_counts_but_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded, counted = _capture(monkeypatch)
    assert _note(agent_id=None) is True
    assert counted == [True] and recorded == []


def test_the_resume_measures_the_wait_from_the_first_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake_note(**kwargs: Any) -> bool:
        seen.append(kwargs)
        return True

    monkeypatch.setattr(frictions, "note_chat_turn_resumed", fake_note)
    first = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    cut = SimpleNamespace(
        first_cut=first,
        user_id=str(USER),
        parent_run_id=str(RUN),
        conversation_id="c1",
        entry="rest",
        resumes=2,
    )
    conv = SimpleNamespace(agent_id=AGENT, user_id=USER, channel="teams")
    parent_wake._note_resumed_chat_cut(cut, conv)
    [kwargs] = seen
    assert 115 <= kwargs["waited_seconds"] <= 180
    assert kwargs["entry"] == "rest" and kwargs["channel"] == "teams"
    assert kwargs["resumes"] == 2 and kwargs["run_id"] == str(RUN)


def test_the_resume_never_raises_on_a_bad_record(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kwargs: Any) -> bool:
        raise RuntimeError("ledger down")

    monkeypatch.setattr(frictions, "note_chat_turn_resumed", boom)
    cut = SimpleNamespace(
        first_cut="not a date", user_id="u", parent_run_id=None, conversation_id="c", entry="web", resumes=0
    )
    parent_wake._note_resumed_chat_cut(cut, SimpleNamespace())


def test_the_counter_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def labels(self, **kwargs: str) -> None:
            raise RuntimeError("registry")

    monkeypatch.setattr(rollout_counters, "CHAT_TURN_CUTS", Broken())
    rollout_counters.count_chat_turn_cut(visible=True)
