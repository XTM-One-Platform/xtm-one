"""The test suites run in the merge queue, and skip on a pull request.

A pull request runs the static checks of ``ci.yml`` (the lint jobs and the
secret scan) and, when it changes what they read, its database checks; the
suites and builds of ``ci.yml`` and the shards of ``e2e.yml`` skip there and
run on a merge group and by hand. The queue tests the exact commit main moves
to, so main runs both workflows again only nightly and when a file their
caches follow changes: only main can save the caches the queue reads. Both
workflows start on every pull request and merge group: the merge queue takes a
pull request only once its required checks are green on it, under the names
they have in the queue, where a workflow that never starts (no trigger, or a
``paths`` filter) leaves the check waiting forever. A required aggregator runs
even when the run is cancelled (skipped, it would count as passed); on a pull
request the e2e one skips with its shards, which reports its check as passed.
"""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SUITES = ["ci.yml", "e2e.yml"]
REQUIRED = {"ci.yml": "verdict", "e2e.yml": "e2e"}
# What a pull request runs: the static checks, the ROI ledger's invariant
# suites (#12077 L7d), the frontend ratchet gates, and the database checks
# behind their routing job.
ON_PULL_REQUESTS = {
    "backend-lint",
    "roi-invariants",
    "frontend-lint",
    "frontend-gates",
    "office-addin-lint",
    "secrets",
    "migration-changes",
}
DATABASE_CHECKS = {"bootstrap", "migrations"}
IN_THE_QUEUE = "github.event_name != 'pull_request'"
# Every file a cache key, or a dependency layer of the e2e images, follows:
# main must run when one changes, or the queue restores stale caches until
# the nightly run.
CACHE_INPUTS = {
    "ci.yml": {
        "backend/pyproject.toml",
        "backend/uv.lock",
        ".github/actions/backend-venv/**",
        ".github/actions/uv-sync/**",
        ".github/workflows/ci.yml",
    },
    "e2e.yml": {
        "Dockerfile",
        "Dockerfile.worker",
        ".dockerignore",
        "docker/chromium-distrusted-roots/**",
        "backend/pyproject.toml",
        "backend/uv.lock",
        "frontend/package.json",
        "frontend/yarn.lock",
        "frontend/.yarnrc.yml",
        "apps/microsoft-office/package.json",
        "apps/microsoft-office/package-lock.json",
        ".github/workflows/e2e.yml",
    },
}


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(name: str) -> dict:
    workflow = _workflow(name)
    # PyYAML reads the bare key ``on`` as the boolean True (YAML 1.1).
    return workflow.get("on", workflow.get(True))


@pytest.mark.parametrize("name", SUITES)
def test_the_suite_reports_on_pull_requests_and_runs_in_the_queue_and_by_hand(name):
    triggers = _triggers(name)
    assert {"pull_request", "merge_group", "workflow_dispatch"} <= set(triggers), name


@pytest.mark.parametrize("name", SUITES)
def test_main_runs_the_suite_only_to_refresh_its_caches(name):
    triggers = _triggers(name)
    assert triggers["push"]["branches"] == ["main"], name
    # Not every merge: the queue already tested the commit main moves to.
    assert set(triggers["push"]["paths"]) == CACHE_INPUTS[name], name
    assert triggers["schedule"], name


def test_only_main_saves_the_image_cache():
    text = (WORKFLOWS / "e2e.yml").read_text(encoding="utf-8")
    cache_to = next(line for line in text.splitlines() if "cache-to:" in line)
    assert "github.ref == 'refs/heads/main'" in cache_to and "event_name" not in cache_to
    # A manual run from main testing another ref saves nothing.
    assert "!inputs.ref" in cache_to


@pytest.mark.parametrize("name", SUITES)
def test_no_path_filter_keeps_a_required_check_waiting(name):
    for event in ("pull_request", "merge_group"):
        assert not (_triggers(name)[event] or {}).get("paths"), f"{name}: {event}"


@pytest.mark.parametrize("name", SUITES)
def test_a_pull_request_runs_the_static_checks_and_the_queue_the_suites(name):
    jobs = _workflow(name)["jobs"]
    for job_name, job in jobs.items():
        if job_name == REQUIRED[name] or job_name in DATABASE_CHECKS:
            continue
        if job_name in ON_PULL_REQUESTS:
            assert "if" not in job, f"{name}: {job_name}"
        else:
            assert job.get("if") == IN_THE_QUEUE, f"{name}: {job_name}"


def test_a_pull_request_that_changes_the_database_runs_the_database_checks():
    # migration-changes answers true on every event but a pull request that
    # touches none of what they read.
    jobs = _workflow("ci.yml")["jobs"]
    for job_name in DATABASE_CHECKS:
        assert jobs[job_name]["needs"] == "migration-changes", job_name
        assert jobs[job_name]["if"] == "needs.migration-changes.outputs.run == 'true'", job_name


def test_the_required_checks_report_even_a_cancelled_run():
    assert _workflow("ci.yml")["jobs"]["verdict"]["if"] == "always()"
    # Skipped on a pull request, like its shards: a job its condition skips
    # reports the required check as passed.
    assert _workflow("e2e.yml")["jobs"]["e2e"]["if"] == f"always() && {IN_THE_QUEUE}"


def _gates_command() -> str:
    steps = _workflow("ci.yml")["jobs"]["frontend-gates"]["steps"]
    return next(step["run"] for step in steps if "vitest" in step.get("run", ""))


def _covered_by(gate: str, filters: list[str]) -> bool:
    for pattern in filters:
        if pattern.endswith("/") and gate.startswith(pattern):
            return True
        if "*" in pattern and fnmatch.fnmatch(gate, pattern) and "/" not in gate[len(pattern.split("*")[0]) :]:
            return True
        if gate == pattern:
            return True
    return False


def test_a_pull_request_runs_every_whole_tree_frontend_gate():
    # A gate scans the whole tree, so the change that breaks it names none of
    # its files: skipped on a pull request, it went red only after the merge
    # while main showed green. Every gate the sandbox's frontend-unit check
    # runs on every change (affected_always) runs on a pull request too.
    checks = tomllib.loads((REPO_ROOT / ".xtm-one" / "checks.toml").read_text(encoding="utf-8"))
    gates = checks["check"]["frontend-unit"]["affected_always"]
    assert gates
    filters = _gates_command().split("--project unit", 1)[1].split()
    missing = [gate for gate in gates if not _covered_by(gate, filters)]
    assert not missing, f"frontend gates a pull request never runs: {missing}"


def test_the_gate_filter_reads_a_shallow_glob_only():
    assert _covered_by("src/no-x.test.ts", ["src/*.test.ts"])
    assert not _covered_by("src/pages/a.test.ts", ["src/*.test.ts"])
    assert _covered_by("tests_unit/a.test.ts", ["tests_unit/"])
    assert not _covered_by("src/pages/a.test.ts", ["tests_unit/"])


def test_the_verdict_reads_every_job():
    jobs = _workflow("ci.yml")["jobs"]
    assert set(jobs["verdict"]["needs"]) == set(jobs) - {"verdict"}
