"""A merge group stops at its first red job.

The required checks (``CI verdict``, ``E2E (Playwright / Chromium)``) need every
job of their workflow, so a group whose first job failed after a few minutes
still ran to its slowest shard before the queue ejected it, and rebuilt the
groups behind it that much later. Every job a required check reads now ends
with ``.github/actions/fail-fast``, run only when the job failed in a merge
group: it cancels the run, the aggregator (``if: always()``) still runs, sees
the failed and cancelled jobs and reports red at once. A pull request, a
manual run and the nightly run keep running every job to its end, so their
reports stay whole. Cancelling a run takes ``actions: write``: only the jobs
that carry the step get it, next to ``contents: read``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
ACTION = REPO_ROOT / ".github" / "actions" / "fail-fast" / "action.yml"
USES = "./.github/actions/fail-fast"
CONDITION = "failure() && github.event_name == 'merge_group'"
# The required aggregator of each workflow.
REQUIRED = {"ci.yml": "verdict", "e2e.yml": "e2e"}
# Read by the aggregator, but runs on a nightly or manual run only: no merge group to stop.
NIGHTLY_ONLY = {"shard-weights"}

WORKFLOW_PARAMS = [pytest.param("ci.yml", id="ciwf"), pytest.param("e2e.yml", id="e2ewf")]


def _jobs(workflow: str) -> dict[str, dict]:
    return yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))["jobs"]


def _stopping_jobs(workflow: str) -> list[str]:
    jobs = _jobs(workflow)
    needs = jobs[REQUIRED[workflow]]["needs"]
    return [name for name in ([needs] if isinstance(needs, str) else needs) if name not in NIGHTLY_ONLY]


def _bare(condition: object) -> str:
    text = str(condition).strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    return text


@pytest.mark.parametrize("workflow", WORKFLOW_PARAMS)
def test_every_job_the_required_check_reads_ends_by_stopping_a_red_merge_group(workflow: str) -> None:
    jobs = _jobs(workflow)
    names = _stopping_jobs(workflow)
    assert names, f"{workflow}: the required check reads no job"
    for name in names:
        last = jobs[name]["steps"][-1]
        assert last.get("uses") == USES, f"{workflow}: job {name!r} must end with `uses: {USES}`"
        assert _bare(last.get("if")) == CONDITION, f"{workflow}: job {name!r}: the step runs only `if: {CONDITION}`"
        others = [step for step in jobs[name]["steps"][:-1] if step.get("uses") == USES]
        assert not others, f"{workflow}: job {name!r} runs the step once, last"


@pytest.mark.parametrize("workflow", WORKFLOW_PARAMS)
def test_only_the_jobs_that_stop_the_group_may_cancel_the_run(workflow: str) -> None:
    jobs = _jobs(workflow)
    stopping = set(_stopping_jobs(workflow))
    for name, job in jobs.items():
        permissions = job.get("permissions") or {}
        if name in stopping:
            assert permissions == {"contents": "read", "actions": "write"}, (
                f"{workflow}: job {name!r} needs exactly `contents: read` and `actions: write`, got {permissions}"
            )
        else:
            assert permissions.get("actions") != "write", f"{workflow}: job {name!r} must not get `actions: write`"


@pytest.mark.parametrize("workflow", WORKFLOW_PARAMS)
def test_the_workflow_default_stays_read_only(workflow: str) -> None:
    document = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    assert document["permissions"] == {"contents": "read"}


@pytest.mark.parametrize("workflow", WORKFLOW_PARAMS)
def test_the_required_check_still_runs_on_a_cancelled_run(workflow: str) -> None:
    assert _bare(_jobs(workflow)[REQUIRED[workflow]]["if"]).startswith("always()")


def test_the_action_cancels_its_own_run_and_never_fails_the_job() -> None:
    action = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    assert action["runs"]["using"] == "composite"
    steps = action["runs"]["steps"]
    assert len(steps) == 1, "one step: cancel the run"
    step = steps[0]
    assert step.get("shell") == "bash"
    env = step.get("env") or {}
    assert _bare(env.get("GH_TOKEN")) == "github.token"
    assert _bare(env.get("RUN_ID")) == "github.run_id"
    assert _bare(env.get("REPO")) == "github.repository"
    run = step["run"]
    assert 'gh run cancel "$RUN_ID" --repo "$REPO"' in run
    # The job is already red: a cancel that fails (the run ended meanwhile) only warns.
    assert "::warning::" in run
    assert step.get("continue-on-error") is True or "||" in run
