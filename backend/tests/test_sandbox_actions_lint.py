"""The ``actions-lint`` check: bash -n on the run: blocks of workflows and composite actions, detected from the listing."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.sandbox_workspace import actions_lint, checks, detect

ACTION = """name: ci image handoff
description: hands the image over
inputs:
  tag:
    required: true
runs:
  using: composite
  steps:
    - name: Load
      shell: bash
      run: |
        set -euo pipefail
        if [ -n "${{ inputs.tag }}" ]; then
          echo "tag ${{ inputs.tag }}"
        fi
    - name: Windows only
      shell: pwsh
      run: |
        if ($true) { Write-Host "ok"
    - run: echo "one line ${{ github.sha }}"
      shell: bash
"""

WORKFLOW = """name: ci
on: [push]
defaults:
  run:
    shell: bash
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: >-
          echo folded
          && echo line
      - name: Plain
        run: echo "plain"   # a comment
      - name: Python
        shell: python
        run: |
          print("not bash"
"""


def test_run_blocks_take_bash_and_default_shells_and_leave_others_out():
    blocks = actions_lint.run_blocks(ACTION)
    assert [first for first, _ in blocks] == [12, 20]
    assert blocks[0][1].splitlines()[0] == "set -euo pipefail" and blocks[0][1].endswith("fi")
    assert blocks[1][1] == 'echo "one line ${{ github.sha }}"'
    wf = actions_lint.run_blocks(WORKFLOW)
    assert [script for _, script in wf] == ["echo folded && echo line", 'echo "plain"']


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_a_clean_file_passes_and_a_syntax_error_is_reported_at_its_yaml_line():
    assert actions_lint.bash_findings(".github/actions/x/action.yml", ACTION) == []
    broken = ACTION.replace("        fi\n", "        echo no fi\n")
    findings = actions_lint.bash_findings(".github/actions/x/action.yml", broken)
    assert findings and all(f["path"] == ".github/actions/x/action.yml" and f["rule"] == "bash-n" for f in findings)
    assert all(12 <= f["line"] <= 16 for f in findings)
    quoted = WORKFLOW.replace('run: echo "plain"', 'run: echo "plain')
    lines = [f["line"] for f in actions_lint.bash_findings("w.yml", quoted)]
    assert lines and all(15 <= line <= 16 for line in lines)


def test_is_actions_file():
    assert actions_lint.is_actions_file(".github/workflows/ci.yml")
    assert actions_lint.is_actions_file(".github/actions/handoff/action.yml")
    assert actions_lint.is_actions_file("tools/my-action/action.yaml")
    assert not actions_lint.is_actions_file(".github/workflows/sub/x.yml")
    assert not actions_lint.is_actions_file("docker-compose.yml")


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_the_script_runs_inline_in_a_checkout_and_writes_its_sarif_report(tmp_path: Path):
    action = tmp_path / ".github/actions/handoff/action.yml"
    action.parent.mkdir(parents=True)
    action.write_text(ACTION.replace("        fi\n", "        echo no fi\n"))
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/ci.yml").write_text(WORKFLOW)
    # Only bash and python on PATH: the bash -n path is the one under test, whatever the host has installed.
    env = {"PATH": os.pathsep.join({os.path.dirname(shutil.which("bash") or ""), os.path.dirname(sys.executable)})}
    done = subprocess.run(
        [sys.executable, "-c", detect.ACTIONS_LINT_SCRIPT],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 1, done.stderr
    assert ".github/actions/handoff/action.yml:" in done.stdout and "2 file(s)" in done.stdout
    report = checks.parse_report("sarif", (tmp_path / actions_lint.REPORT_PATH).read_text())
    assert report is not None and report.findings and report.findings[0].path == ".github/actions/handoff/action.yml"
    clean = subprocess.run(
        [sys.executable, "-c", detect.ACTIONS_LINT_SCRIPT, "--allow-missing-layers", ".github/workflows/ci.yml"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert clean.returncode == 0, clean.stdout
    assert json.loads((tmp_path / actions_lint.REPORT_PATH).read_text())["runs"][0]["results"] == []


def test_a_layer_the_image_cannot_run_is_a_finding_not_a_silent_skip(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(actions_lint.shutil, "which", lambda name: None)
    monkeypatch.setattr(actions_lint, "_has_yaml", lambda: False)
    files = [".github/actions/x/action.yml", ".github/workflows/ci.yml"]
    findings = actions_lint.missing_layer_findings(files, files[1:])
    assert [(f["path"], f["line"], f["rule"]) for f in findings] == [
        (".github/actions/x/action.yml", 1, "missing-layer"),
        (".github/workflows/ci.yml", 1, "missing-layer"),
    ]
    assert "PyYAML" in findings[0]["message"] and "actionlint" in findings[1]["message"]
    # An action file alone needs no actionlint (it does not read action files).
    assert [f["message"] for f in actions_lint.missing_layer_findings(files[:1], [])] == [findings[0]["message"]]
    monkeypatch.setattr(actions_lint.shutil, "which", lambda name: "/usr/local/bin/actionlint")
    monkeypatch.setattr(actions_lint, "_has_yaml", lambda: True)
    assert actions_lint.missing_layer_findings(files, files[1:]) == []


def test_actionlint_reads_the_repository_config_without_a_git_directory(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A checkout has no .git, so actionlint would not find .github/actionlint.yaml (custom runner labels) by itself."""
    seen: list[tuple[list[str], str]] = []

    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs.get("cwd")))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(actions_lint.shutil, "which", lambda name: "/usr/local/bin/actionlint")
    monkeypatch.setattr(actions_lint.subprocess, "run", fake_run)
    workflows = [".github/workflows/ci.yml"]
    assert actions_lint.actionlint_findings(workflows, root=str(tmp_path)) == []
    assert "-config-file" not in seen[-1][0] and seen[-1][1] == str(tmp_path)
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "actionlint.yaml").write_text("self-hosted-runner:\n  labels: [ubuntu-26.04]\n")
    actions_lint.actionlint_findings(workflows, root=str(tmp_path))
    argv = seen[-1][0]
    assert argv[argv.index("-config-file") + 1] == ".github/actionlint.yaml" and argv[-1] == workflows[0]


def test_actions_lint_is_detected_from_the_listing_alone():
    listing = ["pom.xml", ".github/actions/ci-image-handoff/action.yml", "src/a.java"]
    found = detect.detect(listing, {}, images={"toolchain-python", "toolchain-jvm"})
    spec = found.checks["actions-lint"]
    assert spec.argv[:2] == ("python", "-c") and spec.cwd == "" and spec.image == "toolchain-python"
    assert (
        spec.report == "sarif" and spec.report_path == actions_lint.REPORT_PATH and spec.report in checks.REPORT_KINDS
    )
    assert "(inline script of" in checks.describe_checks({"actions-lint": spec})
    assert "actions-lint" not in detect.detect(["pom.xml", "docker-compose.yml"], {}).checks
    assert spec.affected == "files" and spec.install_first is False
    assert spec.affected_globs == detect.ACTIONS_LINT_GLOBS and spec.affected in checks.AFFECTED_MODES


def test_affected_keeps_only_the_workflow_and_action_files():
    from app.services.sandbox_workspace.affected import matches_globs

    kept = [
        p
        for p in (".github/workflows/ci.yml", ".github/actions/x/action.yml", "src/a.py", ".github/dependabot.yml")
        if matches_globs(p, detect.ACTIONS_LINT_GLOBS)
    ]
    assert kept == [".github/workflows/ci.yml", ".github/actions/x/action.yml"]


def test_a_yaml_syntax_error_is_reported_at_its_line():
    pytest.importorskip("yaml")
    broken = WORKFLOW.replace("    runs-on: ubuntu-latest\n", "    runs-on: ubuntu-latest\n\t  bad: tab\n")
    findings = actions_lint.yaml_findings(".github/workflows/ci.yml", broken)
    assert [f["rule"] for f in findings] == ["yaml-syntax"] and findings[0]["line"] == 9, findings
    assert actions_lint.yaml_findings(".github/workflows/ci.yml", WORKFLOW) == []
    assert actions_lint.yaml_findings(".github/actions/x/action.yml", ACTION) == []


def test_the_workflow_and_composite_action_shape_is_checked():
    pytest.importorskip("yaml")
    no_jobs = actions_lint.yaml_findings(".github/workflows/x.yml", "name: x\non: [push]\n")
    assert [f["message"] for f in no_jobs] == ["a workflow needs a 'jobs' mapping"]
    action = """name: a
runs:
  using: composite
  steps:
    - name: no shell
      run: echo hi
    - name: both
      run: echo hi
      uses: actions/checkout@v4
      shell: bash
    - uses: ./.github/actions/other
"""
    findings = actions_lint.yaml_findings(".github/actions/a/action.yml", action)
    assert [(f["line"], f["message"]) for f in findings] == [
        (5, "a composite 'run' step needs 'shell'"),
        (7, "a step has exactly one of 'run' and 'uses'"),
    ]
    assert all(f["rule"] == "actions-schema" for f in findings)
    docker = "name: d\nruns:\n  using: docker\n  image: Dockerfile\n"
    assert actions_lint.yaml_findings("action.yml", docker) == []
    assert actions_lint.yaml_findings("action.yml", "name: d\n")[0]["message"] == "an action needs 'runs.using'"


class _Session:
    """The two reads :func:`commands.checkout_checks` makes on a declared checkout."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    async def entries(self, refresh: bool = False):
        return dict.fromkeys(self.files)

    async def get_bytes(self, path: str):
        return None, self.files[path]


async def test_a_declared_checkout_holding_workflows_gets_actions_lint_too():
    from app.services.sandbox_workspace import commands

    toml = b'[check.unit]\nargv = ["pytest"]\n'
    session = _Session({"repo/.xtm-one/checks.toml": toml, "repo/.github/workflows/ci.yml": b"on: push\n"})
    found = await commands.checkout_checks(session, "repo", images={"python-dev"})
    assert found.origin == "declared" and set(found.specs) == {"unit", "actions-lint"}
    assert found.specs["actions-lint"].image == "python-dev"
    own = b'[check.actions-lint]\nargv = ["make", "lint-ci"]\n'
    session.files["repo/.xtm-one/checks.toml"] = own
    found = await commands.checkout_checks(session, "repo", images={"python-dev"})
    assert found.specs["actions-lint"].argv == ("make", "lint-ci"), "a declared check of that name wins"
    bare = _Session({"repo/.xtm-one/checks.toml": toml, "repo/src/a.py": b""})
    assert set((await commands.checkout_checks(bare, "repo", images={"python-dev"})).specs) == {"unit"}


class _UnrecordedSession(_Session):
    """A checkout without a record: detection reads the listing alone."""

    async def record(self, create: bool = False):
        return None


async def test_a_checks_toml_declaring_services_only_keeps_the_detected_checks_and_lists_its_services():
    from app.services.sandbox_workspace import commands

    toml = b'[service.lab]\nargv = ["python", "lab.py"]\nport = 3500\nimage = "python-dev"\n'
    session = _UnrecordedSession({"repo/.xtm-one/checks.toml": toml, "repo/.github/workflows/ci.yml": b"on: push\n"})
    found = await commands.checkout_checks(session, "repo", images={"python-dev"})
    assert found.origin == "detected" and "actions-lint" in found.specs, "a lab alone never hides the detected CI"
    listing = await commands.list_checks(session, checkout="repo", known_images={"python-dev"})
    assert "Services it declares (sandbox_start_service):" in listing and "- lab: port 3500" in listing
