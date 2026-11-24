"""``scripts/shard_weights.py``: the tables the sharded CI jobs deal by, measured nightly, checked before use."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
import yaml

from scripts import shard_weights
from scripts.shard_weights import KINDS, measure, median, pick, problem

REPO_ROOT = Path(__file__).resolve().parents[2]


def _junit(path: Path, cases: list[tuple[str, float]]) -> Path:
    body = "".join(f'<testcase file="{file}" name="t{i}" time="{seconds}"/>' for i, (file, seconds) in enumerate(cases))
    path.write_text(f'<testsuites><testsuite name="pytest">{body}</testsuite></testsuites>', encoding="utf-8")
    return path


def _table(kind: str, count: int, seconds: float = 10) -> dict[str, float]:
    name = "spec{}.spec.ts" if kind == "e2e" else "tests/test_{}.py"
    return {name.format(i): seconds for i in range(count)}


def test_a_run_s_junit_reports_sum_by_file_and_leave_out_the_short_files(tmp_path):
    reports = [
        _junit(tmp_path / "a.xml", [("tests/test_a.py", 1.4), ("tests/test_a.py", 2.0), ("tests/test_b.py", 0.5)]),
        _junit(tmp_path / "b.xml", [("tests/test_c.py", 9.6), ("conftest.py", 50)]),
    ]
    assert measure("unit", reports) == {"tests/test_a.py": 3, "tests/test_c.py": 10}


def test_a_playwright_report_counts_each_spec_s_first_attempts(tmp_path):
    report = tmp_path / "timings.json"
    spec = {
        "file": "agents.spec.ts",
        "tests": [{"results": [{"duration": 3000}, {"duration": 9000}]}, {"results": [{"duration": 1500}]}],
    }
    nested = {"specs": [{"file": "a2a/01-oauth.spec.ts", "tests": [{"results": [{"duration": 2500}]}]}]}
    report.write_text(json.dumps({"suites": [{"specs": [spec], "suites": [nested]}]}), encoding="utf-8")
    # The retry measures the flake, not the spec: 3 + 1.5 s, not 12 + 1.5.
    assert measure("e2e", [report]) == {"a2a/01-oauth.spec.ts": 2.5, "agents.spec.ts": 4.5}


def test_every_spec_keeps_its_line_however_short(tmp_path):
    # An unnamed spec weighs the median of the others (tests_e2e/shards.ts), not the 1 s an unnamed test file does.
    report = tmp_path / "timings.json"
    specs = [
        {"file": "short.spec.ts", "tests": [{"results": [{"duration": 140}]}]},
        {"file": "skipped.spec.ts", "tests": [{"results": [{"duration": 0}]}]},
    ]
    report.write_text(json.dumps({"suites": [{"specs": specs}]}), encoding="utf-8")
    assert measure("e2e", [report]) == {"short.spec.ts": 0.1, "skipped.spec.ts": 0}


def test_the_median_takes_each_file_over_the_runs_that_measured_it():
    runs = [{"tests/test_a.py": 10, "tests/test_b.py": 4}, {"tests/test_a.py": 30}, {"tests/test_a.py": 12}]
    assert median("unit", runs) == {"tests/test_a.py": 12, "tests/test_b.py": 4}


def test_a_file_the_newest_measure_leaves_out_leaves_the_table_at_once():
    # Split down below the threshold, or deleted: its old weight must not outlive four earlier measures.
    runs = [{"tests/test_a.py": 10}, {"tests/test_a.py": 10, "tests/test_split.py": 120}, {"tests/test_split.py": 120}]
    assert median("unit", runs) == {"tests/test_a.py": 10}


def test_the_e2e_median_keeps_the_tenths():
    assert median("e2e", [{"a.spec.ts": 0.4}, {"a.spec.ts": 0.6}, {"a.spec.ts": 0.5}]) == {"a.spec.ts": 0.5}


@pytest.mark.parametrize(
    ("table", "reason"),
    [
        ([1, 2], "not a JSON object"),
        (_table("unit", 99), "fewer than 100"),
        ({**_table("unit", 100), "app/main.py": 3}, "not a unit file"),
        ({**_table("unit", 100), "tests/../../etc/x.py": 3}, "not a unit file"),
        ({**_table("unit", 100), "tests/test_x.py": "3"}, "not a number"),
        ({**_table("unit", 100), "tests/test_x.py": True}, "not a number"),
        ({**_table("unit", 100), "tests/test_x.py": math.nan}, "not a number"),
        ({**_table("unit", 100), "tests/test_x.py": -1}, "weighs -1 s"),
        ({**_table("unit", 100), "tests/test_x.py": 10**6}, "weighs 1000000 s"),
    ],
)
def test_a_table_that_is_not_a_measure_is_refused(table, reason):
    assert reason in (problem("unit", table) or "")


def test_an_e2e_table_names_spec_files_only():
    assert problem("e2e", _table("e2e", 40)) is None
    assert "not a e2e file" in (problem("e2e", {**_table("e2e", 40), "tests/test_a.py": 3}) or "")


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_every_committed_table_passes_the_checks_a_nightly_one_must(kind):
    # The committed table is what a refused nightly one gives way to.
    assert problem(kind, json.loads(KINDS[kind].committed.read_text(encoding="utf-8"))) is None


def test_a_nightly_table_that_passes_is_the_run_s(tmp_path):
    candidate = tmp_path / "unit.json"
    candidate.write_text(json.dumps(_table("unit", 120, 7)), encoding="utf-8")
    out = tmp_path / "picked" / "unit.json"
    assert "the nightly table (120 files)" in pick("unit", candidate, out)
    assert json.loads(out.read_text(encoding="utf-8")) == _table("unit", 120, 7)


@pytest.mark.parametrize(
    ("content", "why"),
    [
        (None, "no nightly table was fetched"),
        ("{not json", "does not read"),
        (json.dumps(_table("unit", 3)), "refused"),
    ],
)
def test_without_a_sound_nightly_table_the_committed_one_deals(tmp_path, content, why):
    candidate = tmp_path / "unit.json"
    if content is not None:
        candidate.write_text(content, encoding="utf-8")
    out = tmp_path / "picked" / "unit.json"
    said = pick("unit", candidate if content is not None else None, out)
    assert "the committed table" in said and why in said
    assert out.read_text(encoding="utf-8") == KINDS["unit"].committed.read_text(encoding="utf-8")


def test_a_run_that_lost_shards_publishes_nothing(tmp_path):
    # Two reports' worth of files is not the suite: the median would carry the gap for days.
    report = _junit(tmp_path / "a.xml", [("tests/test_a.py", 30)])
    out = tmp_path / "unit.json"
    assert shard_weights.main(["measure", "--kind", "unit", "--out", str(out), str(report)]) == 1
    assert not out.exists()


def test_refresh_rebuilds_a_table_from_runs_downloaded_by_hand(tmp_path):
    newest, older = tmp_path / "2", tmp_path / "1"
    for run, seconds in ((newest, 12), (older, 30)):
        (run / "unit-junit-1").mkdir(parents=True)
        _junit(run / "unit-junit-1" / "report.xml", [(name, seconds) for name in _table("unit", 100)])
    (older / "unit-junit-1" / "report.xml").write_text(
        (older / "unit-junit-1" / "report.xml")
        .read_text()
        .replace("</testsuite>", '<testcase file="tests/test_gone.py" name="x" time="9"/></testsuite>'),
        encoding="utf-8",
    )
    out = tmp_path / "unit.json"
    assert shard_weights.main(["refresh", "--kind", "unit", "--out", str(out), str(newest), str(older)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == _table("unit", 100, 21)


def test_refresh_refuses_a_run_that_is_not_whole(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    _junit(run / "report.xml", [("tests/test_a.py", 30)])
    out = tmp_path / "unit.json"
    assert shard_weights.main(["refresh", "--kind", "unit", "--out", str(out), str(run)]) == 1
    assert not out.exists()


def test_the_median_leaves_out_an_earlier_table_that_fails_its_checks(tmp_path):
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text(json.dumps(_table("unit", 100, 10)), encoding="utf-8")
    bad.write_text(json.dumps({"tests/test_0.py": 10**6}), encoding="utf-8")
    out = tmp_path / "unit.json"
    assert shard_weights.main(["median", "--kind", "unit", "--out", str(out), str(good), str(bad)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == _table("unit", 100, 10)


def _workflow(name: str) -> dict:
    return yaml.safe_load((REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def test_every_sharded_job_deals_by_the_table_its_run_s_select_job_handed_out():
    # A job that read the nightly table on its own could deal by another table than its siblings.
    ci = _workflow("ci.yml")["jobs"]
    for job, table in (("backend", "unit.json"), ("browser-tool", "chromium.json")):
        env = next(step["env"] for step in ci[job]["steps"] if "PYTEST_SHARD" in step.get("env", {}))
        assert env["PYTEST_SHARD_DURATIONS"] == "${{ runner.temp }}/selection/weights/" + table
        assert env["PYTEST_SHARD_SPLIT"] == "files"
    e2e = _workflow("e2e.yml")["jobs"]["shard"]
    env = next(step["env"] for step in e2e["steps"] if "E2E_SHARD" in step.get("env", {}))
    assert env["E2E_SHARD_TIMINGS"] == "${{ runner.temp }}/shard-weights/e2e.json"


@pytest.mark.parametrize("workflow", ["ci.yml", "e2e.yml"])
def test_the_weights_are_published_as_an_artifact_never_a_write(workflow):
    publisher = _workflow(workflow)["jobs"]["shard-weights"]
    assert "github.event_name == 'schedule'" in publisher["if"] and "pull_request" not in publisher["if"]
    # Read-only: the weights are an artifact of the run, never a write to the repository.
    assert publisher["permissions"] == {"contents": "read", "actions": "read"}


def test_only_the_nightly_runs_on_main_are_read():
    # A pull request, a merge group or a manual run on a branch can publish nothing a run deals by.
    action = yaml.safe_load((REPO_ROOT / ".github" / "actions" / "fetch-shard-weights" / "action.yml").read_text())
    script = action["runs"]["steps"][0]["run"]
    assert "runs?branch=main&event=schedule&status=completed" in script
