# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The runs a work item's note stamps name (#776, scope F)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import work_item_note_runs
from app.services.work_item import stamped_note

RUN_A = uuid.UUID("8f240eba-1111-4222-8333-944445555666")
RUN_B = uuid.UUID("19f6aaaa-1111-4222-8333-944445555666")


def test_stamped_run_ids_reads_the_platform_stamp() -> None:
    now = datetime(2026, 11, 20, 23, 22, tzinfo=UTC)
    description = "\n\n".join(
        [
            stamped_note("pushed", now, RUN_A),
            stamped_note(f"mentions run {RUN_B} in its body", now, RUN_A),
            stamped_note("answered", now),
            "Brief naming (run 00000000-0000-4000-8000-000000000000) mid-line.",
        ]
    )
    assert work_item_note_runs.stamped_run_ids(description) == [RUN_A]


def test_stamped_run_ids_keeps_order_and_dedupes() -> None:
    now = datetime(2026, 11, 20, 23, 22, tzinfo=UTC)
    description = "\n\n".join(
        [stamped_note("b", now, RUN_B), stamped_note("a", now, RUN_A), stamped_note("b2", now, RUN_B)]
    )
    assert work_item_note_runs.stamped_run_ids(description) == [RUN_B, RUN_A]
    assert work_item_note_runs.stamped_run_ids(None) == []


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return iter(self._rows)


class _Db:
    """Answers the three reads in order: runs, agents, opened runs."""

    def __init__(self, *answers):
        self.answers = list(answers)

    async def execute(self, _query):
        return _Result(self.answers.pop(0))


@pytest.mark.asyncio
async def test_note_runs_names_the_agent_for_an_admin() -> None:
    agent = uuid.uuid4()
    now = datetime(2026, 11, 20, 23, 22, tzinfo=UTC)
    description = stamped_note("x", now, RUN_A) + "\n\n" + stamped_note("y", now, RUN_B)
    db = _Db([(RUN_A, agent)], [(agent, "Developer")])
    out = await work_item_note_runs.note_runs(db, description, SimpleNamespace(is_admin=True, id=uuid.uuid4()))
    assert out == {
        str(RUN_A): {"agent": {"id": str(agent), "name": "Developer"}, "accessible": True},
        # A run no longer recorded: no agent, not openable.
        str(RUN_B): {"agent": None, "accessible": False},
    }


@pytest.mark.asyncio
async def test_note_runs_without_stamps_reads_nothing() -> None:
    db = _Db()
    assert (
        await work_item_note_runs.note_runs(db, "plain brief", SimpleNamespace(is_admin=False, id=uuid.uuid4())) == {}
    )
