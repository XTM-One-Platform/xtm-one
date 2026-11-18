"""The run pages read where an external run stands in its task's exchange.

A run that called ``ask_the_caller`` is ``completed`` while its task is
``input-required``: read alone, it said "Completed / No result produced", and
the run the caller's answer started stood unlinked to it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.a2a.run_exchange import (
    ANSWERED_BY_RUN_KEY,
    EXCHANGE_ANSWERED,
    EXCHANGE_WAITING,
    EXCHANGE_WITHDRAWN,
    PREVIOUS_RUN_KEY,
    QUESTION_KEY,
    run_exchanges,
)

pytestmark = pytest.mark.asyncio

_A2A = {"source": "a2a", "client_id": "partner-1"}


class _Rows:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def scalars(self):
        return iter(self._rows)


class _Db:
    """Answers statements in order: the asking tasks, the linked runs, the openable ones."""

    def __init__(self, *answers: list[Any]):
        self._answers = list(answers)
        self.statements = 0

    async def execute(self, _stmt):
        self.statements += 1
        return _Rows(self._answers.pop(0) if self._answers else [])


def _run(payload: dict | None = None, context: dict | None = None) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        trigger_context=_A2A if context is None else context,
        trigger_payload={"message": "Convert the workshop budget", **(payload or {})},
    )


async def _exchanges(db: _Db, runs: list[Any], *, is_admin: bool = True):
    return await run_exchanges(db, runs, user_id=uuid.uuid4(), is_admin=is_admin)


async def test_internal_runs_have_no_exchange_and_cost_no_query():
    db = _Db()
    internal = _run(context={"type": "manual"})
    assert await _exchanges(db, [internal]) == {}
    assert db.statements == 0


async def test_a_run_whose_task_asks_is_waiting_on_its_caller():
    asking = _run()
    db = _Db([(asking.id, "Which currency is the budget in?", None)])
    out = await _exchanges(db, [asking])
    assert out[asking.id]["asked"] == {
        "question": "Which currency is the budget in?",
        "state": EXCHANGE_WAITING,
        "answered_by_run_id": None,
    }
    assert "answers" not in out[asking.id]


async def test_a_cancel_before_the_answer_withdraws_the_question():
    asking = _run()
    db = _Db([(asking.id, "Which currency?", datetime.now(UTC))])
    out = await _exchanges(db, [asking])
    assert out[asking.id]["asked"]["state"] == EXCHANGE_WITHDRAWN


async def test_the_two_runs_of_one_answered_question_are_linked_both_ways():
    asking = _run()
    answering = _run(
        {
            "message": "NZD, 9000",
            QUESTION_KEY: "Which currency?",
            PREVIOUS_RUN_KEY: str(asking.id),
        }
    )
    asking.trigger_payload[ANSWERED_BY_RUN_KEY] = str(answering.id)
    db = _Db(
        [],  # no task asks any more
        [(answering.id, answering.trigger_payload), (asking.id, asking.trigger_payload)],
    )
    out = await _exchanges(db, [asking, answering])
    assert out[asking.id]["asked"] == {
        "question": "Which currency?",
        "state": EXCHANGE_ANSWERED,
        "answered_by_run_id": str(answering.id),
    }
    assert out[answering.id]["answers"] == {
        "question": "Which currency?",
        "answer": "NZD, 9000",
        "previous_run_id": str(asking.id),
    }


async def test_a_run_the_viewer_cannot_open_is_not_linked_but_the_question_shows():
    """The question is the asking run's own; only the link is the other run's."""
    asking = _run()
    answering_id = uuid.uuid4()
    asking.trigger_payload[ANSWERED_BY_RUN_KEY] = str(answering_id)
    db = _Db(
        [],
        [(answering_id, {QUESTION_KEY: "Which currency?"})],
        [],  # none of them openable
    )
    out = await _exchanges(db, [asking], is_admin=False)
    assert out[asking.id]["asked"]["question"] == "Which currency?"
    assert out[asking.id]["asked"]["answered_by_run_id"] is None


async def test_a_continuation_from_before_the_links_still_says_what_it_answers():
    legacy = _run({QUESTION_KEY: "Which currency?", "message": "NZD"})
    out = await _exchanges(_Db([]), [legacy])
    assert out[legacy.id]["answers"]["previous_run_id"] is None
    assert out[legacy.id]["answers"]["question"] == "Which currency?"
