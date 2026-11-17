"""Rollout counters: orphaned tasks and resumes by cause and build."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from prometheus_client import REGISTRY

from app.services import background_checkpoint, rollout_counters
from app.services.background_checkpoint import cause_class, lost_cause


def _resumes(entry: str, cause: str, graceful: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "xtmone_run_resumes_total",
            {"entry": entry, "cause": cause, "graceful": graceful, "version": rollout_counters.build_version()},
        )
        or 0.0
    )


def _orphans() -> float:
    return REGISTRY.get_sample_value("xtmone_tasks_orphaned_total", {"version": rollout_counters.build_version()}) or 0.0


def test_every_lost_cause_maps_to_its_class():
    handed = {background_checkpoint.HANDED_BACK_KEY: {"signal": "SIGTERM", "attempt": 1}}
    assert cause_class(lost_cause(handed, attempt=1)) == "handed_back"
    assert cause_class(lost_cause(handed, attempt=2)) in {"handback_lost", "handed_back"}
    assert cause_class(lost_cause({}, taken_back_on_its_word=True)) == "taken_back"
    assert cause_class(lost_cause({"orphaned": {"last_heartbeat_at": "2026-11-02T01:00:00Z"}})) == "lease_lost"
    assert cause_class(lost_cause({})) == "unknown"
    assert cause_class(lost_cause({}, reason="Worker pod-7 (build abc) silent for 90 s")) == "engine_reason"
    assert cause_class(None) == "unknown"
    assert cause_class(
        "the attempt that took it over after its worker was asked to stop (SIGTERM) was lost without a word before it resumed"
    ) == "handback_lost"


def test_the_build_label_is_the_short_sha_else_the_version():
    with patch.object(rollout_counters.settings, "git_sha", "0123456789abcdef"):
        assert rollout_counters.build_version() == "0123456789ab"
    with patch.object(rollout_counters.settings, "git_sha", ""), patch.object(
        rollout_counters.settings, "app_version", "1.2.3"
    ):
        assert rollout_counters.build_version() == "1.2.3"


def test_a_resume_and_an_orphan_are_counted():
    before = _resumes("background_task", "taken_back", "true")
    rollout_counters.count_resume("background_task", "taken_back", graceful=True)
    assert _resumes("background_task", "taken_back", "true") == before + 1
    before_orphans = _orphans()
    rollout_counters.count_orphaned()
    assert _orphans() == before_orphans + 1


def test_note_resume_counts_its_cause_class():
    task = SimpleNamespace(id="t1", metadata_json={"orphaned": {"last_heartbeat_at": "x"}})
    replay = SimpleNamespace(replayed_calls=0, turn_id=None, completed_calls=0)
    before = _resumes("background_task", "lease_lost", "false")
    background_checkpoint.note_resume(task, replay=replay, attempt=2)
    assert _resumes("background_task", "lease_lost", "false") == before + 1


def test_a_counter_failure_never_reaches_the_caller():
    with patch.object(rollout_counters.RUN_RESUMES, "labels", side_effect=RuntimeError("boom")):
        rollout_counters.count_resume("assignment_run", "unknown", graceful=False)
    with patch.object(rollout_counters.TASKS_ORPHANED, "labels", side_effect=RuntimeError("boom")):
        rollout_counters.count_orphaned()


def test_counting_is_one_labelled_increment_and_the_class_is_pure():
    """The program's gate: one increment per resume or orphan, no other work on the path.

    Asserted by call counts, never by a wall-clock budget, which measures the runner's load.
    """
    resumes = MagicMock()
    with patch.object(rollout_counters, "RUN_RESUMES", resumes):
        rollout_counters.count_resume("assignment_run", "unknown", graceful=False)
    version = rollout_counters.build_version()
    assert resumes.mock_calls == [
        call.labels(entry="assignment_run", cause="unknown", graceful="false", version=version),
        call.labels().inc(),
    ]

    orphans = MagicMock()
    with patch.object(rollout_counters, "TASKS_ORPHANED", orphans):
        rollout_counters.count_orphaned()
    orphans.labels.assert_called_once_with(version=rollout_counters.build_version())
    orphans.labels.return_value.inc.assert_called_once_with()

    counters = MagicMock()
    with patch.object(rollout_counters, "RUN_RESUMES", counters), patch.object(
        rollout_counters, "TASKS_ORPHANED", counters
    ), patch.object(background_checkpoint, "logger", counters):
        classes = {cause_class("its worker lost its lease (last heartbeat at x)") for _ in range(3)}
    assert classes == {"lease_lost"}
    assert counters.mock_calls == []
