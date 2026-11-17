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

"""The mode a run executed in, and which of its entries fell back to LLM Only.

A run's label is read from what the run recorded, never from its evaluation's
current setting, which can change after the run. A run records its
mode when it starts (``EvaluationRun.execution_mode``); an older run without
one takes the mode its results recorded (``mode_from_results``). Only an older
run can hold a fallback, so only those runs are read from their
results: a page of newer runs costs no query.

An entry "fell back" when its ``full_loop`` agent run failed and the entry was
answered in LLM Only instead, as an older release did: its result
recorded ``llm_only_fallback``, or a later run reused such an answer and
recorded it as its own (``reused_from_result_id``).

One reader for the run responses and the agent tools, so the rule exists once.
``execution_details`` is a ``json`` column: a row PostgreSQL cannot parse
(``core/json_sql.py``) is read from its stored text in Python instead of
aborting the statement for the whole page.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import Text, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.json_sql import json_unparseable, when_json_parseable
from app.models.evaluation import EvaluationResult, EvaluationRun

#: The mode a result recorded when its ``full_loop`` entry was answered in LLM Only.
FALLBACK_MODE = "llm_only_fallback"


@dataclass(frozen=True)
class RunMode:
    """What a run's label and its agent-tool summary say about its mode."""

    execution_mode: str | None
    fallback_entries: int


def mode_from_results(recorded: Iterable[str | None]) -> str | None:
    """The mode of an older run, from the modes its results recorded.

    ``skip_llm`` and ``full_loop`` as recorded, a fallback being a ``full_loop``
    entry. Any other value is ``llm_only``: the engine ran every mode but
    ``full_loop`` and ``skip_llm`` as LLM Only, a stored value that is neither
    included. ``None`` when no result recorded a mode: a failed entry records
    none.
    """
    modes = {mode for mode in recorded if mode}
    if not modes:
        return None
    if "skip_llm" in modes:
        return "skip_llm"
    if modes & {"full_loop", FALLBACK_MODE}:
        return "full_loop"
    return "llm_only"


def _recorded_mode(details_text: str | None) -> str | None:
    """``execution_details ->> 'execution_mode'`` read from the stored text, as PostgreSQL would."""
    try:
        details = json.loads(details_text or "")
    except ValueError:
        return None
    mode = details.get("execution_mode") if isinstance(details, dict) else None
    return mode if isinstance(mode, str) else None


def _mode_columns():
    """A result's recorded mode and its reuse source's, with the source joined in."""
    source = aliased(EvaluationResult)
    mode = when_json_parseable(
        EvaluationResult.execution_details,
        EvaluationResult.execution_details["execution_mode"].as_string(),
        otherwise=None,
    )
    source_mode = when_json_parseable(
        source.execution_details,
        source.execution_details["execution_mode"].as_string(),
        otherwise=None,
    )
    unreadable = or_(
        json_unparseable(EvaluationResult.execution_details),
        json_unparseable(source.execution_details),
    )
    return source, mode, source_mode, unreadable


async def load_run_modes(
    db: AsyncSession, runs: Iterable[EvaluationRun]
) -> dict[uuid.UUID, RunMode]:
    """``RunMode`` for each of *runs*: its column, else its results, read in one grouped query plus the rare unreadable rows."""
    runs = list(runs)
    modes: dict[uuid.UUID, set[str]] = defaultdict(set)
    fallbacks: dict[uuid.UUID, int] = defaultdict(int)
    run_ids = [run.id for run in runs if run.execution_mode is None]
    if run_ids:
        await _read_results(db, run_ids, modes, fallbacks)
    return {
        run.id: RunMode(
            execution_mode=run.execution_mode or mode_from_results(modes.get(run.id, ())),
            fallback_entries=fallbacks.get(run.id, 0),
        )
        for run in runs
    }


async def _read_results(
    db: AsyncSession,
    run_ids: list[uuid.UUID],
    modes: dict[uuid.UUID, set[str]],
    fallbacks: dict[uuid.UUID, int],
) -> None:
    """The modes the results of *run_ids* recorded, and how many fell back, into *modes* and *fallbacks*."""
    source, mode, source_mode, unreadable = _mode_columns()
    grouped = await db.execute(
        select(
            EvaluationResult.run_id,
            mode.label("mode"),
            source_mode.label("source_mode"),
            func.count().label("n"),
        )
        .outerjoin(source, source.id == EvaluationResult.reused_from_result_id)
        .where(EvaluationResult.run_id.in_(run_ids), ~func.coalesce(unreadable, False))
        .group_by(EvaluationResult.run_id, mode, source_mode)
    )
    for row in grouped:
        if row.mode:
            modes[row.run_id].add(row.mode)
        if FALLBACK_MODE in (row.mode, row.source_mode):
            fallbacks[row.run_id] += row.n

    for run_id, _result_id, recorded, source_recorded in await _unreadable_rows(
        db, run_ids
    ):
        if recorded:
            modes[run_id].add(recorded)
        if FALLBACK_MODE in (recorded, source_recorded):
            fallbacks[run_id] += 1


async def load_run_detail_mode(
    db: AsyncSession, run: EvaluationRun
) -> tuple[RunMode, set[uuid.UUID]]:
    """*run*'s ``RunMode`` and the ids of its results that fell back, from one read of its results.

    The same rule as ``load_run_modes``, per result: the run page marks each
    entry, and reads the run once rather than once for the label and again for
    the marks. A run that recorded its mode holds no fallback and is not read.
    """
    if run.execution_mode is not None:
        return RunMode(run.execution_mode, 0), set()
    source, mode, source_mode, unreadable = _mode_columns()
    rows = await db.execute(
        select(
            EvaluationResult.id,
            mode.label("mode"),
            source_mode.label("source_mode"),
            # A row PostgreSQL cannot parse is decoded from its text instead.
            case((unreadable, cast(EvaluationResult.execution_details, Text))).label("details"),
            case((unreadable, cast(source.execution_details, Text))).label("source_details"),
        )
        .outerjoin(source, source.id == EvaluationResult.reused_from_result_id)
        .where(EvaluationResult.run_id == run.id)
    )
    recorded: list[str | None] = []
    fallen_back: set[uuid.UUID] = set()
    for row in rows:
        if row.details is not None or row.source_details is not None:
            row_mode, row_source_mode = _recorded_mode(row.details), _recorded_mode(row.source_details)
        else:
            row_mode, row_source_mode = row.mode, row.source_mode
        recorded.append(row_mode)
        if FALLBACK_MODE in (row_mode, row_source_mode):
            fallen_back.add(row.id)
    return RunMode(mode_from_results(recorded), len(fallen_back)), fallen_back


async def _unreadable_rows(
    db: AsyncSession, run_ids: list[uuid.UUID]
) -> list[tuple[uuid.UUID, uuid.UUID, str | None, str | None]]:
    """``(run_id, result_id, mode, source_mode)`` for the rows the SQL read sets aside."""
    source, _mode, _source_mode, unreadable = _mode_columns()
    rows = await db.execute(
        select(
            EvaluationResult.run_id,
            EvaluationResult.id,
            cast(EvaluationResult.execution_details, Text).label("details"),
            cast(source.execution_details, Text).label("source_details"),
        )
        .outerjoin(source, source.id == EvaluationResult.reused_from_result_id)
        .where(EvaluationResult.run_id.in_(run_ids), func.coalesce(unreadable, False))
    )
    return [
        (row.run_id, row.id, _recorded_mode(row.details), _recorded_mode(row.source_details))
        for row in rows
    ]
