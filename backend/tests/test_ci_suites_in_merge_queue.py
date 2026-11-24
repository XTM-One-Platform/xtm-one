"""The test suites run in the merge queue, and skip on a pull request.

A pull request runs the static checks of ``ci.yml`` (the lint jobs, the secret
scan, the ROI ledger's invariants, the tool schema budget, the frontend ratchet gates and catalog
tests) and the frontend build; the suites, the database checks and the shards
of ``e2e.yml`` skip there and run on a merge group and by hand. In the queue each
workflow's ``select`` job sizes them to what the merge group's own change
reaches (``tests/selection.py``), and every other event runs them whole. The queue tests the exact commit main moves
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
# suites (#12077 L7d), the tool schema budget (#1625) and the frontend ratchet gates.
ON_PULL_REQUESTS = {
    "backend-lint",
    "roi-invariants",
    "tool-schema-budget",
    "frontend-lint",
    "frontend-gates",
    "office-addin-lint",
    "secrets",
}
IN_THE_QUEUE = "github.event_name != 'pull_request'"
# Under a minute, so it runs on every pull request as well, and in the queue
# as the selection says: the frontend build and its design-system guards.
ON_PULL_REQUESTS_AND_SELECTED = {
    "frontend": "${{ !cancelled() && (github.event_name == 'pull_request' "
    "|| needs.select.outputs.frontend-build == 'true') }}"
}
# Opens an issue when the nightly run fails: it reads the required check, not the reverse.
NOTIFY = "notify-on-failure"
# Publishes the shard weights, on a nightly run only (scripts/shard_weights.py).
PUBLISH = "shard-weights"
# The jobs a selection sizes, and the output that says whether each runs.
SELECTED = {
    "ci.yml": {
        "backend": "unit-shards",
        "browser-tool": "chromium-shards",
        "bootstrap": "integration-shards",
        "frontend-unit": "frontend-unit-mode",
        "frontend": "frontend-build",
        "migrations": "migrations",
    },
    "e2e.yml": {"shard": "run"},
}
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
        if job_name in {REQUIRED[name], NOTIFY}:
            continue
        if job_name == PUBLISH:
            assert job["if"].startswith(
                "always() && (github.event_name == 'schedule' || github.event_name == 'workflow_dispatch') && "
            ), f"{name}: {job_name}"
            continue
        if job_name in ON_PULL_REQUESTS:
            assert "if" not in job, f"{name}: {job_name}"
        elif job_name in ON_PULL_REQUESTS_AND_SELECTED:
            assert job["if"] == ON_PULL_REQUESTS_AND_SELECTED[job_name], f"{name}: {job_name}"
        elif job_name == "select":
            assert job["if"] == IN_THE_QUEUE, f"{name}: {job_name}"
        else:
            assert job["if"].startswith(f"{IN_THE_QUEUE} && "), f"{name}: {job_name}"


@pytest.mark.parametrize("name", SUITES)
def test_the_queue_runs_what_the_selection_says(name):
    jobs = _workflow(name)["jobs"]
    for job_name, output in SELECTED[name].items():
        job = jobs[job_name]
        assert job["needs"] == "select", f"{name}: {job_name}"
        assert f"needs.select.outputs.{output}" in job["if"], f"{name}: {job_name}"
        matrix = job.get("strategy", {}).get("matrix")
        if matrix is not None:
            # A sharded job takes the shards the selection gave it, none skipping it.
            assert "fromJSON(needs.select.outputs." in str(matrix), f"{name}: {job_name}"


@pytest.mark.parametrize("name", SUITES)
def test_a_job_that_reads_the_selection_downloads_it_first(name):
    # A job reading a selection file it never downloaded fails every selective
    # merge group (#440: bootstrap read backend-tests.txt with no artifact).
    for job_name, job in _workflow(name)["jobs"].items():
        steps = job.get("steps", [])
        if any(
            (step.get("with") or {}).get("name") == "test-selection"
            for step in steps
            if "upload-artifact" in str(step.get("uses", ""))
        ):
            continue  # the select job writes the selection it uploads
        reader = next(
            (i for i, step in enumerate(steps) if "/selection/" in str(step.get("env", {})) + str(step.get("run", ""))),
            None,
        )
        if reader is None:
            continue
        downloads = [
            i
            for i, step in enumerate(steps)
            if str(step.get("uses", "")).startswith("actions/download-artifact@")
            and (step.get("with") or {}).get("name") == "test-selection"
        ]
        assert downloads and downloads[0] < reader, f"{name}: {job_name}"


@pytest.mark.parametrize("name", SUITES)
def test_a_merge_group_measures_its_own_change_and_any_other_run_runs_everything(name):
    steps = _workflow(name)["jobs"]["select"]["steps"]
    script = next(step["run"] for step in steps if step.get("id") == "select")
    env = next(step["env"] for step in steps if step.get("id") == "select")
    assert env["BASE_SHA"] == "${{ github.event.merge_group.base_sha }}"
    assert '[ "$EVENT" = merge_group ]' in script and '--base "$BASE_SHA"' in script and "--full" in script


def test_the_required_checks_report_even_a_cancelled_run():
    assert _workflow("ci.yml")["jobs"]["verdict"]["if"] == "always()"
    # Skipped on a pull request, like its shards: a job its condition skips
    # reports the required check as passed.
    e2e = _workflow("e2e.yml")["jobs"]["e2e"]
    assert e2e["if"] == f"always() && {IN_THE_QUEUE}"
    # A failed selection skips the shards: the aggregator reads it too, and a
    # nightly run's failed publishing of the shard weights.
    assert set(e2e["needs"]) == {"select", "shard", PUBLISH}


@pytest.mark.parametrize("name", SUITES)
def test_only_a_red_nightly_run_opens_an_issue(name):
    notify = _workflow(name)["jobs"][NOTIFY]
    assert notify["needs"] == REQUIRED[name]
    assert "github.event_name == 'schedule'" in notify["if"]


def test_a_red_nightly_ultra_render_opens_an_issue():
    # No pull request runs the render any more: the nightly run is its only alarm.
    notify = _workflow("video-studio-ultra.yml")["jobs"][NOTIFY]
    assert notify["needs"] == "ultra"
    assert "github.event_name == 'schedule'" in notify["if"]
    assert "pull_request" not in _triggers("video-studio-ultra.yml")


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


def test_a_pull_request_runs_the_catalog_tests():
    # They read en.json and fr.json whole: count-plural alone failed eight
    # merge groups on 17-18 Nov 2026, each caught only in the queue.
    frontend = REPO_ROOT / "frontend"
    catalog = sorted(path.relative_to(frontend).as_posix() for path in (frontend / "src" / "i18n").glob("*.test.ts"))
    assert catalog
    filters = _gates_command().split("--project unit", 1)[1].split()
    missing = [test for test in catalog if not _covered_by(test, filters)]
    assert not missing, f"catalog tests a pull request never runs: {missing}"


def test_the_sandbox_frontend_gates_check_runs_the_ci_command_as_a_pre_push_guard():
    # #1080 went red on Frontend gates: frontend-unit affected=true selects the
    # gates only when it runs, and nothing named it before the push. The check
    # runs CI's step verbatim and is a pre-push guard on every frontend source.
    checks = tomllib.loads((REPO_ROOT / ".xtm-one" / "checks.toml").read_text(encoding="utf-8"))
    spec = checks["check"]["frontend-gates"]
    assert spec["cwd"] == "frontend"
    assert spec["argv"][:2] == ["sh", "-c"]
    assert spec["argv"][2] == _gates_command()
    for name in ("no-class-name-assertions.test.ts", "App.tsx", "index.css", "en.json"):
        assert any(fnmatch.fnmatchcase(name, glob) for glob in spec["pre_push"]), name


def test_a_pull_request_runs_every_source_scan_guard():
    # #745 broke src/lib/mode-split-guard.test.ts, a guard that reads the
    # whole tree: vitest related never selects it, so only the merge group
    # went red. The gates job runs every such guard the script lists.
    command = _gates_command()
    assert "&& yarn vitest run $(node scripts/source-scan-tests.mjs)" in command
    assert (REPO_ROOT / "frontend" / "scripts" / "source-scan-tests.mjs").is_file()


def test_the_gate_filter_reads_a_shallow_glob_only():
    assert _covered_by("src/no-x.test.ts", ["src/*.test.ts"])
    assert not _covered_by("src/pages/a.test.ts", ["src/*.test.ts"])
    assert _covered_by("tests_unit/a.test.ts", ["tests_unit/"])
    assert not _covered_by("src/pages/a.test.ts", ["tests_unit/"])


def test_the_verdict_reads_every_job():
    jobs = _workflow("ci.yml")["jobs"]
    assert set(jobs["verdict"]["needs"]) == set(jobs) - {"verdict", NOTIFY}
