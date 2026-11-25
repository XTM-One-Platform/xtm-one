"""A detected node check whose script calls git runs in a throwaway repository of the checkout (#1958).

A checkout has no ``.git``: a package.json script that runs ``git status`` (itself, or
through a repository file it runs with ``tsx`` / ``node`` / ``bash``) failed in the
sandbox and passed in CI. Detection now sets ``git_repo`` on such a check, as a declared
``git_repo = true`` check carries it.
"""

from __future__ import annotations

import json

from app.services.sandbox_workspace import checks, detect

GIT_SCRIPT = b"""import { execSync } from 'node:child_process';
const status = execSync('git status --porcelain -- src/db/migrations/').toString();
"""
PLAIN_SCRIPT = b"console.log('no git here');\n"


def _manifest(name: str, scripts: dict[str, str]) -> bytes:
    return json.dumps({"name": name, "scripts": scripts}).encode()


def _detect(files: dict[str, bytes], extra: tuple[str, ...] = ()) -> dict[str, checks.CheckSpec]:
    listing = [*files, *extra]
    read = {p: files[p] for p in detect.candidates(listing) if p in files}
    for rel in detect.script_files(listing, read):
        if rel in files:
            read[rel] = files[rel]
    return detect.detect(listing, read).checks


def test_script_files_names_the_files_a_manifest_script_runs_and_only_those_present():
    files = {
        "module/package.json": _manifest(
            "@app/module", {"db:check": "tsx scripts/check-migrations.ts", "x": "node ../outside.js"}
        ),
        "module/scripts/check-migrations.ts": GIT_SCRIPT,
    }
    listing = [*files, "module/yarn.lock"]
    read = {p: files[p] for p in detect.candidates(listing) if p in files}
    assert detect.script_files(listing, read) == ["module/scripts/check-migrations.ts"]


def test_a_check_whose_script_file_calls_git_is_detected_with_git_repo():
    files = {
        "module/package.json": _manifest(
            "@app/module", {"db:check": "tsx scripts/check-migrations.ts", "gen:check": "tsx scripts/plain.ts"}
        ),
        "module/scripts/check-migrations.ts": GIT_SCRIPT,
        "module/scripts/plain.ts": PLAIN_SCRIPT,
    }
    specs = _detect(files, ("module/yarn.lock",))
    assert specs["module:node-db-check"].git_repo is True
    assert specs["module:node-gen-check"].git_repo is False
    wrapped = checks.command_argv(specs["module:node-db-check"])
    assert wrapped[:3] == ("sh", "-c", checks.GIT_REPO_SHELL) and wrapped[4] == ".."


def test_a_script_body_that_calls_git_itself_is_detected_with_git_repo():
    files = {"package.json": _manifest("root", {"lint": "eslint . && git diff --exit-code -- src/styles.css"})}
    specs = _detect(files, ("yarn.lock",))
    assert specs["node-lint"].git_repo is True


def test_a_workflow_line_yarn_workspace_resolves_the_member_script():
    workflow = b"""
on: push
jobs:
  migrations:
    runs-on: ubuntu-latest
    steps:
      - run: yarn workspace @app/module db:check
"""
    files = {
        ".github/workflows/ci.yml": workflow,
        "package.json": _manifest("root", {}),
        "module/package.json": _manifest("@app/module", {"db:check": "tsx scripts/check-migrations.ts"}),
        "module/scripts/check-migrations.ts": GIT_SCRIPT,
    }
    specs = _detect(files, ("yarn.lock",))
    git_checks = {name for name, spec in specs.items() if spec.git_repo}
    assert git_checks, specs
    assert all("db" in name for name in git_checks)


def test_no_git_call_leaves_every_check_as_it_was():
    files = {"package.json": _manifest("root", {"test": "vitest run", "lint": "eslint ."})}
    specs = _detect(files, ("yarn.lock",))
    assert not any(spec.git_repo for spec in specs.values())


def test_a_word_holding_git_is_no_git_call():
    assert not detect._GIT_CALL.search("const digits = 'github status'; legit diff")
    assert detect._GIT_CALL.search("run('git status --porcelain')")
    assert detect._GIT_CALL.search("git -C front diff --exit-code")
