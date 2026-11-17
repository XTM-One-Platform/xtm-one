"""Runner transport invariants (pure, no DB).

The redelivery lease is the durable backstop's safety knob: a ``dispatched``
command is only reclaimed once its lease expires. If the lease is shorter than
the runner's complete execute-and-upload window, a still-running long command
gets reclaimed and redelivered as a concurrent duplicate of a mutating action,
and a result upload still retrying through a platform restart races the
redelivery of its own command. These tests pin that the lease provably exceeds
that window, read off the runner's own sources (the ``executor.rs``
``budget + 30`` ceiling and the ``api.rs`` ``UPLOAD_RETRY_BUDGET_SECS``), so the
contract with the runner cannot silently regress on either side.

The agent's wait for a result sits between the two: it must outlast
that window, or a command near its ceiling whose upload rode out a restart
reports to the machine's history only, and end before the lease, or the
command is delivered again with nobody waiting for it.
"""

import re
from pathlib import Path

import pytest

from app.core.config import Settings, settings
from app.services.runner_transport import (
    _RESULT_WAIT_MARGIN_SECONDS,
    _RUNNER_EXECUTION_GRACE_SECONDS,
    RUNNER_UPLOAD_RETRY_BUDGET_SECONDS,
    _command_lease_seconds,
    default_result_wait_seconds,
    runner_report_window_seconds,
)

RUNNER_SRC = Path(__file__).resolve().parents[2] / "apps" / "runner" / "src"


def _runner_constant(file_name: str, pattern: str) -> int:
    match = re.search(pattern, (RUNNER_SRC / file_name).read_text(encoding="utf-8"))
    assert match, f"{pattern!r} not found in apps/runner/src/{file_name}"
    return int(match.group(1))


def test_lease_exceeds_runner_execution_ceiling():
    # The runner runs each command under a ``budget + 30s`` ceiling (executor.rs)
    # and only then uploads its result. The lease must be strictly greater than
    # that ceiling so a valid long-running command is never reclaimed mid-flight.
    budget = settings.runner_command_default_timeout_seconds
    runner_execution_ceiling = budget + _RUNNER_EXECUTION_GRACE_SECONDS
    assert _command_lease_seconds() > runner_execution_ceiling


def test_execution_grace_is_the_runner_ceiling_grace():
    grace = _runner_constant("executor.rs", r"Duration::from_secs\(budget \+ (\d+)\)")
    assert grace == _RUNNER_EXECUTION_GRACE_SECONDS


def test_lease_leaves_headroom_for_upload_retries():
    # Beyond the execution ceiling the lease keeps room for the runner's
    # result-upload retries (api.rs), and their budget must fit strictly inside
    # it: an upload still retrying when the lease ends races a redelivery.
    budget = settings.runner_command_default_timeout_seconds
    runner_execution_ceiling = budget + _RUNNER_EXECUTION_GRACE_SECONDS
    headroom = _command_lease_seconds() - runner_execution_ceiling
    upload_budget = _runner_constant(
        "api.rs", r"const UPLOAD_RETRY_BUDGET_SECS: u64 = (\d+);"
    )
    assert 0 < upload_budget < headroom


def test_every_command_runs_under_the_budget_the_platform_hands_it():
    # The ceiling's budget is the command's own timeout, which the claim sets
    # to RUNNER_COMMAND_DEFAULT_TIMEOUT_SECONDS for every command (pinned
    # against the real claim in test_runner_presence_postgres.py). A run_shell
    # ``timeout`` argument only bounds the shell inside the ceiling, which
    # still ends a longer one, so the wait derived from that budget covers
    # every command.
    executor = (RUNNER_SRC / "executor.rs").read_text(encoding="utf-8")
    assert "let budget = cmd.timeout.unwrap_or(self.default_timeout).max(1);" in executor
    assert "tokio::time::timeout(ceiling, work)" in executor


def test_upload_retry_budget_is_the_runners():
    upload_budget = _runner_constant(
        "api.rs", r"const UPLOAD_RETRY_BUDGET_SECS: u64 = (\d+);"
    )
    assert upload_budget == RUNNER_UPLOAD_RETRY_BUDGET_SECONDS


def test_result_wait_outlasts_the_runners_report_window():
    # A command may use its whole ceiling (the wait for the machine's turn
    # included) and then retry its upload for the whole budget: its result
    # lands at most this long after delivery, and the agent must still be
    # waiting for it.
    budget = settings.runner_command_default_timeout_seconds
    grace = _runner_constant("executor.rs", r"Duration::from_secs\(budget \+ (\d+)\)")
    upload_budget = _runner_constant(
        "api.rs", r"const UPLOAD_RETRY_BUDGET_SECS: u64 = (\d+);"
    )
    assert runner_report_window_seconds() == budget + grace + upload_budget
    assert default_result_wait_seconds() > runner_report_window_seconds()


def test_result_wait_ends_before_the_lease():
    # The give-up recorded when the wait ends must land while the command
    # cannot be delivered again yet.
    assert _RESULT_WAIT_MARGIN_SECONDS > 0
    assert _command_lease_seconds() - default_result_wait_seconds() >= _RESULT_WAIT_MARGIN_SECONDS


@pytest.mark.parametrize("budget", [1, 120, 600])
def test_result_wait_follows_the_command_budget(monkeypatch, budget):
    monkeypatch.setattr(settings, "runner_command_default_timeout_seconds", budget)
    assert (
        runner_report_window_seconds()
        < default_result_wait_seconds()
        < _command_lease_seconds()
    )


def test_result_wait_default_is_derived_and_documented(monkeypatch):
    # Unset by default: the wait is derived from the runner contract, and the
    # 205 s the documentation quotes for the default budget is this number.
    fields = Settings.model_fields
    assert fields["runner_result_wait_timeout_seconds"].default is None
    monkeypatch.setattr(
        settings,
        "runner_command_default_timeout_seconds",
        fields["runner_command_default_timeout_seconds"].default,
    )
    assert default_result_wait_seconds() == 205
