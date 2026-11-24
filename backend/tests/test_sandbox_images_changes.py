"""A pull request or a merge group builds only the sandbox images it changes.

``sandbox-images.yml`` starts on a change to backend/sandbox or to either lock,
and every job used to run: a frontend lock bump rebuilt the sixteen images
when only nodejs-dev bakes that lock. Its ``changes`` job now routes the pull
request's diff: python-dev for the backend's lock, nodejs-dev for the
frontend's, every image for backend/sandbox, .xtm-one/stores.py (baked in
every toolchain image) or the workflow itself. Here its
script runs on a real diff, and every path that starts the workflow builds
something.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "sandbox-images.yml"
BOTH = ["python-dev", "nodejs-dev"]
BASH = shutil.which("bash")
GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or BASH is None or GIT is None, reason="runs the job's bash script on a git diff"
)


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _script() -> str:
    steps = _workflow()["jobs"]["changes"]["steps"]
    return next(step["run"] for step in steps if step.get("id") == "changed")


def _route(tmp_path: Path, changed: list[str], event: str = "pull_request") -> dict[str, str]:
    """The changes job's outputs for a pull request whose diff is ``changed``."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run([GIT, "-C", str(repo), *args], check=True, capture_output=True)  # noqa: S603

    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base")
    for name in changed:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text("changed\n", encoding="utf-8")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "change")
    output = tmp_path / "output"
    output.touch()
    subprocess.run(  # noqa: S603
        [BASH, "--noprofile", "--norc", "-eo", "pipefail", "-c", _script()],
        cwd=repo,
        env={"EVENT": event, "GITHUB_OUTPUT": str(output), "PATH": os.environ["PATH"]},
        check=True,
    )
    return dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())


def _outputs(sandbox: bool, targets: list[str]) -> dict[str, str]:
    return {"sandbox": str(sandbox).lower(), "dev-targets": "[" + ",".join(f'"{t}"' for t in targets) + "]"}


@pytest.mark.parametrize(
    ("changed", "sandbox", "targets"),
    [
        (["frontend/yarn.lock"], False, ["nodejs-dev"]),
        (["frontend/package.json", "frontend/src/app.ts"], False, ["nodejs-dev"]),
        (["backend/uv.lock"], False, ["python-dev"]),
        # Every toolchain image bakes the stores runner (#1858).
        ([".xtm-one/stores.py"], True, BOTH),
        (["backend/pyproject.toml", "frontend/.yarnrc.yml"], False, BOTH),
        (["backend/sandbox/requirements.sandbox.cti.txt"], True, BOTH),
        ([".github/workflows/sandbox-images.yml"], True, BOTH),
        (["backend/app/main.py"], False, []),
    ],
)
@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_a_change_builds_the_images_its_diff_reaches(tmp_path, changed, sandbox, targets, event):
    assert _route(tmp_path, changed, event) == _outputs(sandbox, targets)


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_every_other_run_builds_every_image(tmp_path, event):
    assert _route(tmp_path, ["frontend/yarn.lock"], event) == _outputs(True, BOTH)


def test_every_path_that_starts_the_workflow_builds_an_image(tmp_path):
    # PyYAML reads the bare key ``on`` as the boolean True (YAML 1.1).
    workflow = _workflow()
    paths = workflow.get("on", workflow.get(True))["push"]["paths"]
    for i, path in enumerate(paths):
        case = tmp_path / str(i)
        case.mkdir()
        assert _route(case, [path.replace("**", "Dockerfile")]) != _outputs(False, []), path


def test_every_build_job_waits_for_the_routing():
    jobs = _workflow()["jobs"]
    for name, job in jobs.items():
        if name in {"changes", "notify-on-failure"} or name == "sandbox-image-smoke-test":
            continue
        assert job.get("needs") == "changes", name
        assert "needs.changes.outputs." in job["if"], name
    # The Python images take their matrix from sandbox-image-matrix, skipped with it.
    assert jobs["sandbox-image-smoke-test"]["needs"] == "sandbox-image-matrix"
    dev = jobs["sandbox-dev-image-smoke-test"]
    assert dev["strategy"]["matrix"]["target"] == "${{ fromJson(needs.changes.outputs.dev-targets) }}"
