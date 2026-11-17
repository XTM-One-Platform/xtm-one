"""Regression tests for the shared max_iterations ceiling.

The Settings and per-agent sliders advertise a ceiling of 1000; the server must
enforce the same ceiling on save. It previously clamped the General Assistant
value to a stale 50, silently snapping admin values >50 back on reload.
``clamp_max_iterations`` is now the single source of truth used by both the GA
save path (``settings.py``) and the per-agent save paths (``agents.py``).
"""

from __future__ import annotations

from app.core import config as config_module
from app.models.general_assistant_config import (
    CHAT_MIN_LOOP_ITERATIONS,
    CHAT_MIN_LOOP_TIMEOUT_SECONDS,
    CHAT_SECS_PER_ITERATION,
    MAX_ITERATIONS_CEILING,
    chat_loop_max_iterations,
    chat_loop_timeout_seconds,
    clamp_max_iterations,
    iteration_reach,
)


def test_ceiling_is_1000():
    # Long runs: 1000 rounds, the reach of a run that keeps extending.
    assert MAX_ITERATIONS_CEILING == 1000


def test_clamps_above_ceiling_down_to_the_ceiling():
    assert clamp_max_iterations(2000) == 1000
    assert clamp_max_iterations(MAX_ITERATIONS_CEILING + 1) == 1000


def test_preserves_values_within_range():
    assert clamp_max_iterations(1) == 1
    assert clamp_max_iterations(25) == 25
    assert clamp_max_iterations(100) == 100
    assert clamp_max_iterations(500) == 500
    assert clamp_max_iterations(1000) == 1000


def test_floors_values_below_one():
    assert clamp_max_iterations(0) == 1
    assert clamp_max_iterations(-5) == 1


def _clamp_or_none(value: int | None) -> int | None:
    """Mirror the clamp-or-inherit expression the import/duplicate paths use.

    ``agents.py`` applies exactly this guard on every persistence path that
    copies a ``max_iterations`` from an untrusted bundle (import) or a possibly
    legacy source row (duplicate): ``None`` means inherit (-> platform default)
    and is preserved verbatim, while a concrete value is clamped to the shared
    ceiling so an out-of-range number can never be persisted.
    """
    return clamp_max_iterations(value) if value is not None else None


def test_import_duplicate_clamp_preserves_none_inherit():
    # null = inherit the platform default; the clamp must not turn it into a 1.
    assert _clamp_or_none(None) is None


def test_import_duplicate_clamp_bounds_out_of_range_values():
    # Imported bundles / legacy duplicated rows must land inside [1, ceiling].
    assert _clamp_or_none(5000) == 1000
    assert _clamp_or_none(0) == 1
    assert _clamp_or_none(-3) == 1
    assert _clamp_or_none(25) == 25


def test_chat_loop_timeout_matches_historical_formula():
    """``max(floor, max_iterations * 120)``. A 100-iteration agent gets
    12 000 s and a 25-iteration agent gets 3 000 s. The floor is 1800 s
    so a 5-iteration specialist doing generate_file /
    code_interpreter is not killed at 10 minutes."""
    assert CHAT_SECS_PER_ITERATION == 120
    assert CHAT_MIN_LOOP_TIMEOUT_SECONDS == 1800
    assert chat_loop_timeout_seconds(100) == 12_000
    assert chat_loop_timeout_seconds(25) == 3_000
    assert chat_loop_timeout_seconds(15) == 1800
    assert chat_loop_timeout_seconds(5) == 1800
    assert chat_loop_timeout_seconds(1) == 1800
    assert chat_loop_timeout_seconds(4) == 1800


def test_chat_loop_max_iterations_floors_low_caps():
    """A 5-iteration specialist is stripped of tools at round 3. Chat
    and chat-spawned runs therefore get at least 15 iterations.
    Assignment / evaluation pins are not this helper."""
    assert CHAT_MIN_LOOP_ITERATIONS == 15
    assert chat_loop_max_iterations(1) == 15
    assert chat_loop_max_iterations(5) == 15
    assert chat_loop_max_iterations(14) == 15
    assert chat_loop_max_iterations(15) == 15
    assert chat_loop_max_iterations(25) == 25
    assert chat_loop_max_iterations(100) == 100
    assert chat_loop_max_iterations(2000) == 1000


def test_a_run_may_reach_the_ceiling_while_it_makes_progress(monkeypatch):
    monkeypatch.setattr(config_module.settings, "agent_loop_auto_extend_iterations", True)
    assert iteration_reach(25) == MAX_ITERATIONS_CEILING
    assert iteration_reach(1000) == 1000


def test_without_extension_the_cap_is_the_reach(monkeypatch):
    monkeypatch.setattr(config_module.settings, "agent_loop_auto_extend_iterations", False)
    assert iteration_reach(25) == 25
    assert iteration_reach(100) == 100
