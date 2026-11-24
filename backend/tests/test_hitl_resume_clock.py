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

"""A HITL resume keeps the run's clocks honest (issue #1645)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services import hitl_resume_clock as clock
from app.services.builtin_tools.prompting import format_datetime_prompt
from app.services.schedule_overlap import wall_time_refusal

T0 = datetime(2026, 11, 24, 4, 0, tzinfo=UTC)


def _run(**context):
    return SimpleNamespace(trigger_context=dict(context), started_at=T0)


def test_a_pause_is_counted_apart_and_summed():
    run = _run(type="work_item")
    clock.mark_paused(run, now=T0)
    assert clock.close_pause(run, now=T0 + timedelta(minutes=10)) == 600
    clock.mark_paused(run, now=T0 + timedelta(minutes=20))
    clock.close_pause(run, now=T0 + timedelta(minutes=21))
    assert clock.awaiting_human_seconds(run) == 660
    assert clock.AWAITING_HUMAN_SINCE_KEY not in run.trigger_context
    assert run.trigger_context["type"] == "work_item"


def test_a_run_paused_before_the_fix_adds_nothing():
    run = _run()
    assert clock.close_pause(run, now=T0) is None
    assert clock.awaiting_human_seconds(run) is None


def test_an_unreadable_start_of_pause_never_raises():
    run = _run(**{clock.AWAITING_HUMAN_SINCE_KEY: "not a time"})
    assert clock.close_pause(run, now=T0) is None
    assert clock.AWAITING_HUMAN_SINCE_KEY not in run.trigger_context


def test_the_wall_time_leaves_the_pause_out():
    run = _run(**{clock.AWAITING_HUMAN_SECONDS_KEY: 3000})
    now = T0 + timedelta(seconds=3300)
    assert wall_time_refusal(run, effective_timeout=600, now=now) is None
    assert wall_time_refusal(_run(), effective_timeout=600, now=now) is not None


def test_the_prompt_clock_is_restamped_with_its_local_line():
    stale = (
        "persona\n\nCurrent date and time (UTC): Monday, November 23, 2026 at 01:00 UTC\n"
        "User's local time (Europe/Paris): Monday, November 23, 2026 at 02:00\n\nRun context"
    )
    fresh = clock.restamp_prompt_clock(stale)
    assert "November 23, 2026 at 01:00" not in fresh
    assert "November 23, 2026 at 02:00" not in fresh
    expected = format_datetime_prompt("Europe/Paris").lstrip("\n")
    # Minute granularity: the line written now equals a fresh one.
    assert expected.split("\n")[0][:40] in fresh
    assert "User's local time (Europe/Paris): " in fresh
    assert fresh.startswith("persona\n\n") and fresh.endswith("\n\nRun context")
    assert fresh.count("Current date and time (UTC): ") == 1


def test_a_prompt_without_clock_is_unchanged():
    assert clock.restamp_prompt_clock("no clock here") == "no clock here"


def test_a_utc_only_clock_is_restamped():
    stale = "x\nCurrent date and time (UTC): Monday, November 23, 2026 at 01:00 UTC\ny"
    fresh = clock.restamp_prompt_clock(stale)
    assert "at 01:00 UTC" not in fresh
    assert fresh.startswith("x\nCurrent date and time (UTC): ") and fresh.endswith("\ny")
