"""``scripts/ci-local.sh`` runs the frontend jobs of CI, step for step.

The files say so ("change both together"), and they still drifted: the
design-system guards step went into the workflow only, so a local run
passed what CI then failed. This holds the lists equal - the frontend
group's commands against every ``run:`` of the Frontend lint job of
``ci.yml``, then of its Frontend job (whose install the local group runs
once), then of its Office add-in lint job, in order, each in its working directory. One command differs on purpose:
the unit tests run in CI jobs of their own (``frontend-unit``,
#9017), and the local group runs them unsharded (``yarn test:unit``) right
before the build.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


UNIT_TESTS = ("frontend", "yarn test:unit")


def _workflow_commands(job: str = "frontend") -> list[tuple[str, str]]:
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    default = workflow["jobs"][job].get("defaults", {}).get("run", {}).get("working-directory", ".")
    commands: list[tuple[str, str]] = []
    for step in workflow["jobs"][job]["steps"]:
        if "run" not in step:
            continue
        directory = step.get("working-directory", default)
        for line in step["run"].splitlines():
            commands.extend((directory, part.strip()) for part in line.split(" && ") if part.strip())
    return commands


def _local_commands() -> list[tuple[str, str]]:
    script = (REPO_ROOT / "scripts" / "ci-local.sh").read_text(encoding="utf-8")
    # The group the comment introduces, not the earlier prerequisite check
    # under the same condition.
    start = script.index(
        "if [[ $RUN_FRONTEND -eq 1 ]]; then",
        script.index("# Runs every `run:` command of the Frontend lint, Frontend (TypeScript) and"),
    )
    group = script[start : script.index("\nfi\n", start)]
    commands: list[tuple[str, str]] = []
    for directory, chain in re.findall(r'^\s*\(cd "\$ROOT/([^"]+)" && (.+)\)\s*$', group, re.MULTILINE):
        commands.extend((directory, part.strip()) for part in chain.split(" && "))
    return commands


def test_the_local_frontend_group_runs_the_workflow_jobs_in_order():
    light = _workflow_commands("frontend-lint")
    install = light[:2]
    assert install == [("frontend", "corepack enable"), ("frontend", "yarn install --immutable")]
    full = _workflow_commands()
    assert full[:2] == install
    workflow = light + full[2:] + _workflow_commands("office-addin-lint")
    assert len(workflow) == 13
    assert UNIT_TESTS not in workflow
    local = _local_commands()
    unit = local.index(UNIT_TESTS)
    assert local[:unit] + local[unit + 1 :] == workflow
    assert local[unit + 1] == ("frontend", "yarn build")


def test_the_shard_job_runs_both_halves_of_the_unit_tests():
    shard = _workflow_commands("frontend-unit")
    assert ("frontend", "node scripts/node-test-files.mjs") in shard
    assert ("frontend", "yarn vitest run --shard=${{ matrix.shard }}") in shard


def test_both_run_the_design_system_guards_after_the_build():
    guards = [
        ("frontend", "yarn build"),
        ("frontend", "node scripts/fds-migration/verify-classes.mjs"),
        ("frontend", "node scripts/fds-migration/count-legacy.mjs --check"),
    ]
    for commands in (_workflow_commands(), _local_commands()):
        build = commands.index(guards[0])
        assert commands[build : build + 3] == guards
