"""Zero-configuration checks: a checkout's checks read from its own files, ``.xtm-one/checks.toml`` an override."""

from __future__ import annotations

import json
import uuid

import pytest

from app.services.sandbox_workspace import checks, commands, detect
from app.services.sandbox_workspace.manifest import Limits
from app.services.sandbox_workspace.session import WorkspaceSession
from app.services.sandbox_workspace.store import MemoryWorkspaceStore, WorkspaceScope

IMAGES = {"python", "python-dev", "nodejs-dev", "jvm-dev", "rust-dev"}


def _detect(files: dict[str, str], extra: tuple[str, ...] = (), images=IMAGES) -> detect.Detection:
    listing = [*files, *extra]
    picked = detect.candidates(listing)
    return detect.detect(listing, {p: files[p].encode() for p in picked if p in files}, images=images)


def _argv(found: detect.Detection) -> dict[str, tuple[str, ...]]:
    """What a call without args runs: each check's argv, then its default args."""
    return {name: (*spec.argv, *spec.default_args) for name, spec in found.checks.items()}


# -- the workflows: what CI runs is the source of truth ------------------------

WORKFLOW = r"""
name: ci
on: [push]
defaults:
  run:
    working-directory: app
jobs:
  test:
    runs-on: ubuntu-latest
    services:
      db:
        image: postgres:16
      cache: redis:7
    steps:
      - uses: actions/checkout@v4
      - run: yarn install --immutable
      - run: |
          yarn lint
          yarn run check-ts
          # a comment
          echo done
      - run: yarn test --coverage
        working-directory: sub
      - run: NODE_OPTIONS=--max-old-space-size=8192 yarn build
      - run: yarn deploy
      - run: yarn test:e2e
      - run: cd ../tools && cargo test --locked
      - run: ${{ matrix.command }}
  py:
    defaults:
      run:
        working-directory: client
    steps:
      - run: pip install -r requirements.txt -r test-requirements.txt
      - run: |
          python -m pytest tests/ \
            -q
      - run: black --check .
      - run: black .
"""


def test_a_detected_tree_target_is_a_default_arg_the_call_args_replace():
    """black --check . named one file ran black --check . x.py: the whole tree again (gap run-command-args-keep-default-args)."""
    from app.services.sandbox_workspace import checks

    found = _detect({".github/workflows/ci.yml": WORKFLOW})
    fmt = found.checks["client:python-format"]
    assert fmt.argv == ("black", "--check") and fmt.default_args == (".",)
    assert checks.command_argv(fmt) == ("black", "--check", ".")
    assert checks.command_argv(fmt, ["x.py"]) == ("black", "--check", "x.py")
    # Other kinds keep their argv whole: a test or an install target is no tree target.
    assert found.checks["client:python-unit"].default_args == ()
    assert detect._split_tree_target("unit", ["pytest", "."]) == (["pytest", "."], ())
    assert detect._split_tree_target("format-black", ["black", "."]) == (["black"], (".",))
    assert detect._split_tree_target("lint", ["."]) == (["."], ())


def test_workflow_run_lines_become_checks_in_their_working_directory():
    found = _detect({".github/workflows/ci.yml": WORKFLOW})
    argv = _argv(found)
    assert argv["app:node-install"] == ("yarn", "install", "--immutable")
    assert argv["app:node-lint"] == ("yarn", "lint")
    assert argv["app:node-types"] == ("yarn", "run", "check-ts")
    assert argv["app/sub:node-unit"] == ("yarn", "test", "--coverage")
    # An environment assignment runs through the shell, classified by its command.
    assert argv["app:node-build"] == ("sh", "-c", "NODE_OPTIONS=--max-old-space-size=8192 yarn build")
    # cd DIR && ... moves the check to that directory and runs the rest there.
    assert argv["tools:rust-unit"] == ("cargo", "test", "--locked")
    assert found.checks["tools:rust-unit"].cwd == "tools"
    assert argv["client:python-install"] == ("pip", "install", "-r", "requirements.txt", "-r", "test-requirements.txt")
    assert argv["client:python-unit"] == ("python", "-m", "pytest", "tests/", "-q")
    assert argv["client:python-format"] == ("black", "--check", ".")
    # Deploys, end-to-end runs, echoes and expressions are not checks.
    assert not any("deploy" in " ".join(a) or "e2e" in " ".join(a) or "echo" in a for a in argv.values())
    assert not any("matrix" in " ".join(a) for a in argv.values())
    assert found.checks["app:node-lint"].description == ".github/workflows/ci.yml job test: yarn lint"
    assert found.checks["app:node-lint"].image == "nodejs-dev" and found.checks["client:python-unit"].image == "python-dev"
    assert [(i.name, i.image) for i in found.infra] == [("db", "postgres:16"), ("cache", "redis:7")]


CIRCLECI = r"""
version: 2.1
jobs:
  ensure_formatting:
    docker:
      - image: cimg/python:3.13
      - image: redis:7
        name: cache
    working_directory: ~/repo
    environment:
      LANG: C.UTF-8
    steps:
      - checkout
      - run:
          name: install dependencies
          command: pip install black isort --user
      - run:
          name: run isort check
          command: isort --profile black --check .
      - run: black --check .
  linter:
    docker:
      - image: alpine/flake8
    working_directory: ~/repo
    steps:
      - checkout
      - run: flake8 --ignore=E,W ~/repo
  test:
    working_directory: ~/openaev
    docker:
      - image: cimg/python:3.13
    parameters:
      injector_name:
        type: string
    steps:
      - checkout
      - run:
          working_directory: ~/openaev/<< parameters.injector_name >>
          command: python -m unittest
      - run:
          working_directory: ~/
          command: python -m pytest
      - run:
          working_directory: ~/openaev/client
          command: python -m pytest tests
  deploy_images:
    docker:
      - image: cimg/base:current
    steps:
      - run: yarn build
"""


def test_circleci_run_lines_become_checks_at_the_checkout_root() -> None:
    """isort --profile black ran from the root on CircleCI and no detected check reproduced it (gap sandbox-poetry-checks-and-root-format-checks)."""
    found = _detect({".circleci/config.yml": CIRCLECI}, extra=("client/tests/test_x.py", "client/pyproject.toml"))
    argv = _argv(found)
    formats = {name: a for name, a in argv.items() if name.startswith("python-format")}
    # `pip install black isort --user` before them: the tools run in the environment that holds them (#714).
    tools = ("uv", "run", "--no-project", "--with", "black", "--with", "isort")
    assert (*tools, "isort", "--profile", "black", "--check", ".") in formats.values()
    assert (*tools, "black", "--check", ".") in formats.values()
    # The job directory written in a line is the checkout root; the alpine/flake8 job image carries flake8.
    assert argv["python-lint"] == ("uv", "run", "--no-project", "--with", "flake8", "flake8", "--ignore=E,W", ".")
    # A step below the job directory runs in that directory; a parameter or a directory outside the checkout is skipped.
    assert argv["client:python-unit"] == ("python", "-m", "pytest", "tests")
    assert not any("unittest" in a or a == ("python", "-m", "pytest") for a in argv.values())
    # A deploy job ships the code: not a check.
    assert not any("build" in a for a in argv.values())
    assert found.checks["python-lint"].description == ".circleci/config.yml job linter: flake8 --ignore=E,W ."
    assert ("LANG", "C.UTF-8") in found.checks["python-format"].env
    assert detect.Infra("cache", "redis:7", ".circleci/config.yml job ensure_formatting") in found.infra


def test_a_narrow_checkout_keeps_the_circleci_configuration() -> None:
    sizes = {".circleci/config.yml": 900, "client/pyproject.toml": 100, "other/x.py": 10}
    assert ".circleci/config.yml" in detect.companions(sizes, ["client"])
    assert detect.candidates(["other/x.py", ".circleci/config.yml"])[0] == ".circleci/config.yml"


def test_a_workflow_repository_script_is_a_check_named_by_its_kind() -> None:
    # XTM-Flow/filigran-flow: the lint job runs `uv run plugin/scripts/flow_lint.py` (a PEP 723 script
    # that carries its own dependencies); before, only the pytest line of the job was detected.
    wf = (
        "jobs:\n  lint:\n    steps:\n"
        "      - run: uv run plugin/scripts/flow_lint.py --format github\n"
        "      - run: uv run plugin/scripts/flow_index.py --check\n"
        "      - run: uv run --with pytest pytest plugin/tests -q\n"
    )
    argv = _argv(_detect({".github/workflows/flow.yml": wf}))
    assert argv["python-lint"] == ("uv", "run", "plugin/scripts/flow_lint.py", "--format", "github")
    assert argv["python-unit"] == ("uv", "run", "--with", "pytest", "pytest", "plugin/tests", "-q")
    assert not any("flow_index" in " ".join(a) for a in argv.values())


def test_a_repository_gate_script_with_an_expression_option_is_a_check_the_call_completes() -> None:
    # OpenAEV-Platform/collectors tests-collectors.yml (gap repo-script-run-without-declared-check): the
    # catalog gate passes its base ref as an expression; the line was dropped whole and no check ran it.
    wf = (
        "jobs:\n  catalog-contract:\n    steps:\n"
        '      - run: python .github/scripts/check_catalog_contract.py --changed-since "origin/${{ github.base_ref }}"\n'
        "      - run: uv run scripts/validate_manifest.py --ref=${{ github.sha }} --strict\n"
        "      - run: python .github/scripts/build_test_matrix.py ${{ matrix.collector }}\n"
        "      - run: ${{ matrix.command }}\n"
        "      - run: python .github/scripts/check_release_notes.py\n"
        "      - run: verify.py\n"
    )
    found = _detect({".github/workflows/tests.yml": wf})
    argv = _argv(found)
    assert argv["python-check-catalog-contract"] == ("python", ".github/scripts/check_catalog_contract.py")
    assert argv["python-check-manifest"] == ("uv", "run", "scripts/validate_manifest.py", "--strict")
    # A positional expression, a whole-expression command, a shipping name and a bare verb stay out.
    assert not any("build_test_matrix" in " ".join(a) or "release" in " ".join(a) for a in argv.values())
    assert not any("verify.py" in a for a in argv.values())
    assert detect._without_expression_options('python x.py "${{ a }}"') is None
    assert detect._without_expression_options("echo ${{ a }} && python x.py") is None
    assert detect._gate_script_kind("check_catalog_contract") == "check-catalog-contract"
    assert detect._gate_script_kind("checkout") is None
    assert detect._gate_script_kind("check_publish") is None


def test_a_workflow_wins_over_the_manifest_for_the_same_check():
    pkg = json.dumps({"scripts": {"lint": "eslint src", "test": "vitest"}})
    wf = "jobs:\n  a:\n    steps:\n      - run: yarn lint --max-warnings 0\n"
    found = _detect({".github/workflows/a.yml": wf, "package.json": pkg}, extra=("yarn.lock",))
    argv = _argv(found)
    assert argv["node-lint"] == ("yarn", "lint", "--max-warnings", "0")
    assert argv["node-unit"] == ("yarn", "test") and argv["node-install"] == ("yarn", "install", "--frozen-lockfile")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("npm ci", ("node", "install")),
        ("pnpm run typecheck", ("node", "types")),
        ("npx eslint .", ("node", "lint")),
        ("uv sync --locked", ("python", "install")),
        ("uv run --frozen pytest -x", ("python", "unit")),
        ("poetry run mypy .", ("python", "types")),
        ("ruff format --check .", ("python", "format")),
        ("ruff format .", None),
        ("mvn -B clean verify", ("jvm", "unit")),
        ("./mvnw spotless:check", ("jvm", "lint")),
        ("./gradlew build", ("jvm", "build")),
        ("cargo clippy -- -D warnings", ("rust", "lint")),
        ("cargo fmt", None),
        ("dbt deps", ("dbt", "install")),
        ("dbt parse", ("dbt", "parse")),
        ("sqlfluff lint models", ("dbt", "lint")),
        ("make test", ("make", "unit")),
        ("uv run plugin/scripts/flow_lint.py --format github", ("python", "lint")),
        ("python3 scripts/run_tests.py -q", ("python", "unit")),
        ("uv run plugin/scripts/flow_index.py --check", None),
        ("python scripts/release.py", None),
        ("docker build .", None),
        ("echo hi", None),
    ],
)
def test_classify_reads_the_tool_and_its_subcommand(line, expected):
    assert detect.classify(line.split()) == expected


# -- the manifests -------------------------------------------------------------


def test_node_workspaces_install_once_at_the_lock_and_run_scripts_per_package():
    root = json.dumps({"packageManager": "yarn@4.5.0", "workspaces": ["front", "back"]})
    front = json.dumps({"scripts": {"lint": "eslint .", "typecheck": "tsc --noEmit", "test": "vitest run", "build": "vite build"}})
    back = json.dumps({"scripts": {"test": "echo \"Error: no test specified\" && exit 1", "check-ts": "tsc"}})
    found = _detect({"package.json": root, "front/package.json": front, "back/package.json": back}, extra=("yarn.lock", ".yarnrc.yml"))
    argv = _argv(found)
    assert argv["node-install"] == ("yarn", "install", "--immutable")
    assert "front:node-install" not in argv and "back:node-install" not in argv
    assert argv["front:node-lint"] == ("yarn", "lint") and argv["front:node-types"] == ("yarn", "typecheck")
    assert argv["front:node-unit"] == ("yarn", "test") and argv["front:node-build"] == ("yarn", "build")
    assert argv["back:node-types"] == ("yarn", "check-ts") and "back:node-unit" not in argv


def test_a_node_wrapper_unit_script_gets_a_check_that_runs_only_the_files_given():
    root = json.dumps({"packageManager": "pnpm@10.0.0", "workspaces": ["packages/*"]})
    pkg = json.dumps({"scripts": {"test": "node test/run.mjs"}})
    files = {"package.json": root, "packages/chat/package.json": pkg}
    extra = ("pnpm-lock.yaml", "packages/chat/test/run.mjs", "packages/chat/test/register.mjs")
    found = _detect(files, extra=extra)
    argv = _argv(found)
    assert argv["packages/chat:node-unit"] == ("corepack", "pnpm@10.0.0", "run", "test")
    assert argv["packages/chat:node-unit-file"] == ("node", "--import", "./test/register.mjs", "--test")
    spec = found.checks["packages/chat:node-unit-file"]
    assert spec.cwd == "packages/chat" and "pass the test files as args" in spec.description
    # Listed after the package's unit check, and its args are the files run.
    names = list(found.checks)
    assert names.index("packages/chat:node-unit-file") > names.index("packages/chat:node-unit")
    assert commands.command_argv(spec, ["src/a.test.ts"]) == (*spec.argv, "src/a.test.ts")


@pytest.mark.parametrize(
    ("script", "extra"),
    [
        ("node test/run.mjs", ("test/run.mjs",)),  # no loader beside the wrapper
        ("node test/run.mjs", ("test/register.mjs",)),  # the wrapper is not in the checkout
        ("node --test", ("test/register.mjs",)),  # already the runner: args are the files
        ("vitest run", ("test/run.mjs", "test/register.mjs")),
        ("node ../run.mjs", ("register.mjs",)),
    ],
)
def test_no_node_unit_file_check_without_a_wrapper_and_its_loader(script, extra):
    pkg = json.dumps({"scripts": {"test": script}})
    assert "node-unit-file" not in _argv(_detect({"package.json": pkg}, extra=("package-lock.json", *extra)))


def test_a_vitest_package_gets_a_coverage_free_check_on_the_files_given() -> None:
    """opencti-front: `yarn test:coverage` in CI; the file check runs only the args, coverage off."""
    root = json.dumps({"packageManager": "yarn@4.5.0", "workspaces": ["front", "back"]})
    front = json.dumps({"scripts": {"test": "vitest run"}, "devDependencies": {"vitest": "5.1.0"}})
    back = json.dumps(
        {
            "scripts": {"test": "vitest run --config vitest.config.integration.ts --coverage"},
            "devDependencies": {"vitest": "5"},
        }
    )
    extra = (
        "yarn.lock",
        ".yarnrc.yml",
        "back/vitest.config.integration.ts",
        "back/vitest.config.ci-unit.ts",
        "back/vitest.config.ci-integration.ts",
        "front/vitest.config.ts",
    )
    found = _detect({"package.json": root, "front/package.json": front, "back/package.json": back}, extra=extra)
    argv = _argv(found)
    assert argv["front:node-unit-file"] == ("yarn", "vitest", "run", "--coverage.enabled=false")
    assert argv["back:node-unit-file"] == (
        "yarn",
        "vitest",
        "run",
        "--coverage.enabled=false",
        "--config",
        "vitest.config.ci-unit.ts",
    )
    spec = found.checks["front:node-unit-file"]
    assert spec.cwd == "front" and "coverage off" in spec.description
    assert commands.command_argv(spec, ["src/a.test.ts"]) == (*spec.argv, "src/a.test.ts")
    names = list(found.checks)
    assert names.index("front:node-unit-file") > names.index("front:node-unit")


def test_the_unit_check_runs_the_unit_script_when_test_runs_the_integration_suite() -> None:
    """opencti-graphql: `test` runs the integration config with coverage, `test:ci-unit` the unit config."""
    scripts = {
        "test": "vitest --silent run --config vitest.config.integration.ts --coverage",
        "test:ci-unit": "vitest --bail 1 run --config vitest.config.ci-unit.ts",
        "test:ci-integration": "vitest --bail 1 run --config vitest.config.ci-integration.ts",
        "test:watch": "vitest watch",
    }
    pkg = json.dumps({"packageManager": "yarn@4.5.0", "scripts": scripts})
    found = _detect({"package.json": pkg}, extra=("yarn.lock", ".yarnrc.yml"))
    assert _argv(found)["node-unit"] == ("yarn", "test:ci-unit")
    assert "scripts.test:ci-unit" in found.checks["node-unit"].description
    plain = json.dumps({"packageManager": "yarn@4.5.0", "scripts": {"test": "vitest run", "test:watch": "vitest"}})
    assert _argv(_detect({"package.json": plain}, extra=("yarn.lock", ".yarnrc.yml")))["node-unit"] == ("yarn", "test")


def test_a_workspace_member_linted_from_the_root_gets_an_eslint_check_on_the_files_given_alone() -> None:
    """A member's `lint` reaches a root `eslint .`: two files given as args linted the whole tree out of memory."""
    root = json.dumps(
        {
            "packageManager": "yarn@4.18.1",
            "workspaces": ["front", "back"],
            "scripts": {"lint:lint": 'cd "${INIT_CWD}" && eslint --max-warnings 0 .'},
            "devDependencies": {"eslint": "10.14.0"},
        }
    )
    front = json.dumps({"scripts": {"lint": "run lint:lint", "test": "vitest run"}, "devDependencies": {"vitest": "5"}})
    back = json.dumps({"scripts": {"test": "vitest run"}})
    extra = ("yarn.lock", ".yarnrc.yml", "eslint.config.mjs", "front/src/a.tsx", "front/src/b.tsx")
    found = _detect({"package.json": root, "front/package.json": front, "back/package.json": back}, extra=extra)
    argv = _argv(found)
    # The lint script ignores its args: the whole tree is linted whatever files a call names.
    assert argv["front:node-lint"] == ("yarn", "lint")
    spec = found.checks["front:node-lint-file"]
    assert spec.argv == ("yarn", "run", "-T", "eslint") and spec.cwd == "front"
    ran = commands.command_argv(spec, ["src/a.tsx", "src/b.tsx"])
    assert ran == ("yarn", "run", "-T", "eslint", "src/a.tsx", "src/b.tsx") and "." not in ran
    assert spec.affected == "files" and spec.affected_by_default and "*.tsx" in spec.affected_globs
    assert "never the whole tree" in spec.description
    names = list(found.checks)
    assert names.index("front:node-lint-file") > names.index("front:node-lint")
    # back holds no lint script but is linted by the same root config: it gets the check too.
    assert argv["back:node-lint-file"] == ("yarn", "run", "-T", "eslint")


@pytest.mark.parametrize(
    ("lock", "runner"),
    [
        ("pnpm-lock.yaml", ("pnpm", "exec")),
        ("package-lock.json", ("npx", "--no-install")),
        ("yarn.lock", ("yarn",)),
    ],
)
def test_the_eslint_file_check_calls_the_package_managers_runner(lock: str, runner: tuple[str, ...]) -> None:
    pkg = json.dumps({"scripts": {"lint": "eslint ."}, "devDependencies": {"eslint": "10"}})
    assert _argv(_detect({"package.json": pkg}, extra=(lock, "eslint.config.js")))["node-lint-file"] == (
        *runner,
        "eslint",
    )


def test_no_eslint_file_check_without_an_eslint_config_or_dependency() -> None:
    with_dep = json.dumps({"scripts": {"lint": "eslint ."}, "devDependencies": {"eslint": "10"}})
    assert "node-lint-file" not in _argv(_detect({"package.json": with_dep}, extra=("package-lock.json",)))
    no_dep = json.dumps({"scripts": {"lint": "biome check ."}})
    assert "node-lint-file" not in _argv(
        _detect({"package.json": no_dep}, extra=("package-lock.json", "eslint.config.js"))
    )


def test_a_relay_package_generates_its_artifacts_before_the_eslint_file_check() -> None:
    front = json.dumps(
        {
            "packageManager": "yarn@4.5.0",
            "scripts": {"relay": "relay-compiler relay.config.json", "lint": "eslint --quiet ."},
            "devDependencies": {"relay-compiler": "21.0.1", "eslint": "10"},
        }
    )
    found = _detect(
        {"front/package.json": front}, extra=("front/yarn.lock", "front/.yarnrc.yml", "front/eslint.config.js")
    )
    shell = ("sh", "-c", 'yarn relay && exec "$@"', "xtm-relay")
    assert _argv(found)["front:node-lint-file"] == (*shell, "yarn", "eslint")


@pytest.mark.parametrize(
    ("lock", "exec_"),
    [("pnpm-lock.yaml", ("pnpm", "exec")), ("package-lock.json", ("npx", "--no-install"))],
)
def test_the_vitest_file_check_calls_the_package_managers_runner(lock: str, exec_: tuple[str, ...]) -> None:
    pkg = json.dumps({"scripts": {"test": "vitest"}, "dependencies": {"vitest": "5"}})
    assert _argv(_detect({"package.json": pkg}, extra=(lock,)))["node-unit-file"] == (
        *exec_,
        "vitest",
        "run",
        "--coverage.enabled=false",
    )


def test_a_relay_package_generates_its_artifacts_before_lint_types_and_unit() -> None:
    """opencti-front: `__generated__` is git-ignored and written by `yarn relay`; its checks run it first."""
    front = json.dumps(
        {
            "packageManager": "yarn@4.5.0",
            "scripts": {
                "relay": "relay-compiler relay.config.json",
                "build": "yarn relay && vite build",
                "lint": "eslint --quiet .",
                "check-ts": "tsc -b",
                "test": "vitest run",
            },
            "devDependencies": {"relay-compiler": "21.0.1", "vitest": "5.1.0"},
        }
    )
    found = _detect({"front/package.json": front}, extra=("front/yarn.lock", "front/.yarnrc.yml"))
    argv = _argv(found)
    shell = ("sh", "-c", 'yarn relay && exec "$@"', "xtm-relay")
    assert argv["front:node-lint"] == (*shell, "yarn", "lint")
    assert argv["front:node-types"] == (*shell, "yarn", "check-ts")
    assert argv["front:node-unit"] == (*shell, "yarn", "test")
    assert argv["front:node-unit-file"] == (*shell, "yarn", "vitest", "run", "--coverage.enabled=false")
    assert argv["front:node-install"] == ("yarn", "install", "--immutable")
    assert argv["front:node-build"] == ("yarn", "build")
    spec = found.checks["front:node-unit-file"]
    assert "Relay artifacts generated first: yarn relay" in spec.description and len(spec.description) <= 200
    assert commands.command_argv(spec, ["src/a.test.ts"]) == (*spec.argv, "src/a.test.ts")


def test_no_relay_step_without_the_compiler_or_its_script() -> None:
    scripts = {"lint": "eslint .", "relay": "relay-compiler"}
    plain = json.dumps({"scripts": scripts, "devDependencies": {"eslint": "9"}})
    assert _argv(_detect({"package.json": plain}, extra=("package-lock.json",)))["node-lint"] == ("npm", "run", "lint")
    unscripted = json.dumps({"scripts": {"lint": "eslint ."}, "devDependencies": {"relay-compiler": "21"}})
    assert _argv(_detect({"package.json": unscripted}, extra=("package-lock.json",)))["node-lint"] == (
        "npm",
        "run",
        "lint",
    )
    npm = json.dumps({"scripts": scripts, "devDependencies": {"relay-compiler": "21"}})
    assert _argv(_detect({"package.json": npm}, extra=("package-lock.json",)))["node-lint"] == (
        "sh",
        "-c",
        'npm run relay && exec "$@"',
        "xtm-relay",
        "npm",
        "run",
        "lint",
    )


def test_a_relay_step_never_runs_a_build_that_calls_the_compiler() -> None:
    """xtm-hub: `build` is `relay-compiler && next build`; the checks ran the whole Next build, offline."""
    hub = {
        "relay": "relay-compiler",
        "build": "relay-compiler && next build",
        "lint": "eslint .",
        "test": "vitest run",
    }
    manifest = {"packageManager": "yarn@4.18.1", "devDependencies": {"relay-compiler": "21", "vitest": "4"}}
    found = _detect({"front/package.json": json.dumps({**manifest, "scripts": hub})}, extra=("front/yarn.lock",))
    argv = _argv(found)
    shell = ("sh", "-c", 'yarn relay && exec "$@"', "xtm-relay")
    assert argv["front:node-unit-file"][:4] == shell and argv["front:node-lint"] == (*shell, "yarn", "lint")
    assert argv["front:node-build"] == ("yarn", "build")
    chained = {"build": "relay-compiler --validate && next build", "lint": "eslint ."}
    for lock, prefix in (("yarn.lock", "yarn"), ("package-lock.json", "npx --no-install")):
        only = json.dumps({"devDependencies": {"relay-compiler": "21"}, "scripts": chained})
        lint = _argv(_detect({"package.json": only}, extra=(lock,)))["node-lint"]
        assert lint[:3] == ("sh", "-c", f'{prefix} relay-compiler --validate && exec "$@"'), lint


def _graphql_like() -> detect.Detection:
    """opencti-graphql: a yarn workspace member with a native add-on, a Python bridge and a fetched manifest."""
    root = json.dumps(
        {
            "packageManager": "yarn@4.18.1",
            "workspaces": ["back"],
            "dependenciesMeta": {"node-calls-python": {"built": True}, "re2": {"built": True}, "nx": {"built": False}},
        }
    )
    back = json.dumps(
        {
            "scripts": {
                "check-ts": "tsc -b",
                "lint": "eslint --quiet .",
                "install:python": "pip3 install -r src/python/requirements.txt",
                "build:prod": "run get-connectors-manifest && run build:types && node builder/builder.ts",
                "get-connectors-manifest": "node script/get-connectors-manifest.ts",
                "test": "vitest run --config vitest.config.integration.ts --coverage",
                "test:ci-unit": "vitest --bail 1 run --config vitest.config.ci-unit.ts",
            },
            "dependencies": {"node-calls-python": "1.13.0", "nx-free": "1"},
            "devDependencies": {"vitest": "5.1.0", "re2": "1.27.1"},
        }
    )
    extra = ("yarn.lock", ".yarnrc.yml", "back/vitest.config.ci-unit.ts", "back/vitest.config.integration.ts")
    return _detect({"package.json": root, "back/package.json": back}, extra=extra)


def test_the_build_get_steps_run_before_lint_types_and_unit() -> None:
    """`build:prod` fetches a git-ignored manifest the sources import: types, lint and unit fetch it first."""
    found = _graphql_like()
    argv = _argv(found)
    assert argv["back:node-types"] == (
        "sh",
        "-c",
        'yarn get-connectors-manifest && exec "$@"',
        "xtm-relay",
        "yarn",
        "check-ts",
    )
    assert argv["back:node-lint"][2] == 'yarn get-connectors-manifest && exec "$@"'
    assert "build steps run first: get-connectors-manifest" in found.checks["back:node-types"].description


def test_the_unit_checks_build_the_native_addons_and_run_the_install_scripts_first() -> None:
    """The install skips builds: the add-ons the root marks built, then `install:python`, before the unit run."""
    found = _graphql_like()
    argv = _argv(found)
    program = 'yarn rebuild node-calls-python re2 && yarn install:python && yarn get-connectors-manifest && exec "$@"'
    assert argv["back:node-unit-file"] == (
        "sh",
        "-c",
        program,
        "xtm-relay",
        "yarn",
        "vitest",
        "run",
        "--coverage.enabled=false",
        "--config",
        "vitest.config.ci-unit.ts",
    )
    assert argv["back:node-unit"] == ("sh", "-c", program, "xtm-relay", "yarn", "test:ci-unit")
    assert "rebuild" not in argv["back:node-types"][2] and "install:python" not in argv["back:node-types"][2]
    spec = found.checks["back:node-unit-file"]
    assert len(spec.description) <= 200
    assert commands.command_argv(spec, ["tests/a-test.ts"]) == (*spec.argv, "tests/a-test.ts")


def test_no_runtime_or_fetch_step_without_a_built_addon_an_install_script_or_a_get_step() -> None:
    pkg = json.dumps(
        {
            "packageManager": "yarn@4.5.0",
            "scripts": {"check-ts": "tsc", "test": "vitest run", "build": "yarn get-x && vite build"},
            "dependencies": {"re2": "1"},
            "devDependencies": {"vitest": "5"},
        }
    )
    argv = _argv(_detect({"package.json": pkg}, extra=("yarn.lock", ".yarnrc.yml")))
    assert argv["node-types"] == ("yarn", "check-ts")
    assert argv["node-unit"] == ("yarn", "test")
    assert argv["node-unit-file"] == ("yarn", "vitest", "run", "--coverage.enabled=false")


def test_no_vitest_file_check_without_vitest_and_the_wrapper_check_wins() -> None:
    assert "node-unit-file" not in _argv(
        _detect({"package.json": json.dumps({"scripts": {"test": "jest"}})}, extra=("package-lock.json",))
    )
    pkg = json.dumps({"scripts": {"test": "node test/run.mjs"}, "devDependencies": {"vitest": "5"}})
    found = _argv(_detect({"package.json": pkg}, extra=("package-lock.json", "test/run.mjs", "test/register.mjs")))
    assert found["node-unit-file"] == ("node", "--import", "./test/register.mjs", "--test")


def test_npm_and_pnpm_use_their_lock_and_run():
    pkg = json.dumps({"scripts": {"test": "jest"}})
    assert _argv(_detect({"package.json": pkg}, extra=("package-lock.json",))) == {
        "node-install": ("npm", "ci"),
        "node-relock": ("npm", "install", "--package-lock-only", "--ignore-scripts"),
        "node-unit": ("npm", "run", "test"),
    }
    assert _argv(_detect({"package.json": pkg}, extra=("pnpm-lock.yaml",)))["node-install"] == (
        "pnpm",
        "install",
        "--frozen-lockfile",
        "--config.strict-dep-builds=false",
    )
    assert _argv(_detect({"package.json": pkg}))["node-install"] == ("npm", "install")


def test_a_workflow_pnpm_install_ignores_unapproved_builds_unless_it_says_otherwise():
    flow = "jobs:\n  ci:\n    steps:\n      - run: pnpm install --frozen-lockfile\n      - run: pnpm run lint\n"
    found = _argv(_detect({".github/workflows/ci.yml": flow, "package.json": "{}"}, extra=("pnpm-lock.yaml",)))
    assert found["node-install"] == ("pnpm", "install", "--frozen-lockfile", "--config.strict-dep-builds=false")
    strict = flow.replace("--frozen-lockfile", "--frozen-lockfile --config.strict-dep-builds=true")
    found = _argv(_detect({".github/workflows/ci.yml": strict, "package.json": "{}"}, extra=("pnpm-lock.yaml",)))
    assert found["node-install"] == ("pnpm", "install", "--frozen-lockfile", "--config.strict-dep-builds=true")


def test_root_generate_check_and_test_scripts_are_checks_of_their_own():
    scripts = {
        "build": "vite build",
        "test": "vitest run",
        "generate:icons": "node scripts/icons.mjs",
        "check:icons": "node scripts/icons.mjs --check",
        "check:artifacts": "node scripts/artifacts.mjs",
        "check:bundle-size": "size-limit",
        "check:types": "tsc --noEmit",
        "test:e2e": "playwright test",
        "test:watch": "vitest",
        "check:install": "node x.js",
        "release:publish": "changeset publish",
    }
    pkg = json.dumps({"packageManager": "pnpm@12.18.0", "scripts": scripts})
    member = json.dumps({"scripts": {"check:size": "size-limit"}})
    found = _detect({"package.json": pkg, "packages/ui/package.json": member}, extra=("pnpm-lock.yaml", "pnpm-workspace.yaml"))
    argv = _argv(found)
    pnpm = ("corepack", "pnpm@12.18.0", "run")
    assert argv["node-generate-icons"] == (*pnpm, "generate:icons")
    assert argv["node-check-icons"] == (*pnpm, "check:icons")
    assert argv["node-check-artifacts"] == (*pnpm, "check:artifacts")
    assert argv["node-check-bundle-size"] == (*pnpm, "check:bundle-size")
    # check:types is the types kind already, end-to-end, watch, install-like and release scripts are left out.
    assert argv["node-types"] == (*pnpm, "check:types") and "node-check-types" not in argv
    assert not {"node-test-e2e", "node-test-watch", "node-check-install"} & set(argv)
    assert not any("release" in name for name in argv)
    # A workspace member's gates are checks too, run from its directory.
    assert argv["packages/ui:node-check-size"] == (*pnpm, "check:size")
    names = list(found.checks)
    assert names.index("node-install") < names.index("node-check-bundle-size")
    assert found.checks["node-check-bundle-size"].image == found.checks["node-install"].image


def test_a_code_generator_script_is_a_check_whatever_its_name():
    """A narrow checkout has no generated sources: the codegen script must be offered before types or unit."""
    root = json.dumps({"packageManager": "yarn@4.18.1", "workspaces": ["graphql", "front"]})
    graphql = json.dumps(
        {
            "scripts": {
                "check-ts": "tsc -b",
                "build:types": "graphql-codegen --config graphql-codegen.ts",
                "build:prod": "run build:types && node builder/builder.ts",
                "test": "vitest run",
            }
        }
    )
    front = json.dumps(
        {
            "scripts": {
                "relay": "relay-compiler relay.config.json",
                "build": "yarn relay && vite build",
                "dev": "yarn relay && vite",
                "lint": "eslint .",
            }
        }
    )
    found = _detect(
        {"package.json": root, "graphql/package.json": graphql, "front/package.json": front}, extra=("yarn.lock",)
    )
    argv = _argv(found)
    assert argv["graphql:node-build-types"] == ("yarn", "build:types")
    assert argv["front:node-relay"] == ("yarn", "relay")
    # A script that only calls another script by name is not a generator of its own.
    assert not {"graphql:node-build-prod", "front:node-dev"} & set(argv)
    names = list(found.checks)
    assert names.index("node-install") < names.index("front:node-relay")


def test_a_workspace_member_namespaced_generator_and_gate_scripts_are_checks():
    """A front package of a Yarn workspace: its i18n scripts were met in CI only, and argv is refused."""
    root = json.dumps(
        {
            "packageManager": "yarn@4.18.1",
            "workspaces": ["front"],
            "scripts": {"lint:lint": "eslint .", "knip:fix": "knip --fix"},
        }
    )
    front = json.dumps(
        {
            "scripts": {
                "build": "vite build",
                "check-ts": "tsc --noEmit",
                "i18n:extract": "lingui extract",
                "i18n:extract:clean": "lingui extract --clean",
                "i18n:compile": "lingui compile --strict",
                "i18n:check": "tsx scripts/i18n-check.ts",
                "fds:css": "tsx scripts/generate-fds-css.ts",
                "lint:fix": "eslint --fix .",
                "dev": "vite",
            }
        }
    )
    argv = _argv(_detect({"package.json": root, "front/package.json": front}, extra=("yarn.lock",)))
    assert argv["front:node-i18n-check"] == ("yarn", "i18n:check")
    assert argv["front:node-i18n-extract"] == ("yarn", "i18n:extract")
    assert argv["front:node-i18n-extract-clean"] == ("yarn", "i18n:extract:clean")
    assert argv["front:node-i18n-compile"] == ("yarn", "i18n:compile")
    # The kinds stay, and a namespaced script with no generator or gate verb is not a check.
    assert argv["front:node-types"] == ("yarn", "check-ts")
    assert not {"front:node-fds-css", "front:node-lint-fix", "node-lint-lint", "node-knip-fix"} & set(argv)


def test_a_hyphenated_gate_or_generator_script_is_a_check() -> None:
    """OpenAEV's front names its i18n gate ``i18n-checker``: it was met in CI only, and argv is refused."""
    front = json.dumps(
        {
            "packageManager": "yarn@4.18.1",
            "scripts": {
                "i18n-checker": "npm run generate-i18n-checker-constant && node scripts/i18n-checker.js",
                "generate-i18n-checker-constant": "tsc src/constants/Lang.ts --outDir scripts/constants",
                "extract-translation": "node scripts/extract-i18n-keyword.js",
                "check-licenses": "license-checker-rseidelsohn --production --summary",
                "check-ts": "tsc",
                "sort-translation": "node scripts/sort-translation-files.js",
                "generate-types-from-api": "swagger-typescript-api generate --path=${API_URL:-http://localhost:8080}/api-docs",
                "generate-test-e2e": "yarn playwright codegen http://localhost:3001/",
                "show-report-e2e": "start test-results/report.html",
            },
        }
    )
    argv = _argv(_detect({"openaev-front/package.json": front}, extra=("openaev-front/yarn.lock",)))
    assert argv["openaev-front:node-i18n-checker"] == ("yarn", "i18n-checker")
    assert argv["openaev-front:node-generate-i18n-checker-constant"] == ("yarn", "generate-i18n-checker-constant")
    assert argv["openaev-front:node-extract-translation"] == ("yarn", "extract-translation")
    assert argv["openaev-front:node-check-licenses"] == ("yarn", "check-licenses")
    # check-ts is the types kind; no gate word, a URL (a live API) or an end-to-end word leaves a script out.
    assert argv["openaev-front:node-types"] == ("yarn", "check-ts") and "openaev-front:node-check-ts" not in argv
    left_out = {"sort-translation", "generate-types-from-api", "generate-test-e2e", "show-report-e2e"}
    assert not {f"openaev-front:node-{name}" for name in left_out} & set(argv)


def test_a_named_script_a_workflow_already_runs_is_not_offered_twice():
    flow = "jobs:\n  ci:\n    steps:\n      - run: npm ci\n      - run: npm run check:lint\n"
    pkg = json.dumps({"scripts": {"check:lint": "eslint .", "generate:docs": "typedoc"}})
    argv = _argv(_detect({".github/workflows/ci.yml": flow, "package.json": pkg}, extra=("package-lock.json",)))
    assert argv["node-lint"] == ("npm", "run", "check:lint") and "node-check-lint" not in argv
    assert argv["node-generate-docs"] == ("npm", "run", "generate:docs")


def test_python_uv_poetry_and_requirements():
    uv = "[project]\nname='x'\n[tool.ruff]\nline-length=100\n[tool.mypy]\nfiles=['src']\n"
    found = _argv(_detect({"pyproject.toml": uv}, extra=("uv.lock", "tests/test_a.py")))
    assert found["python-install"] == ("uv", "sync", "--locked")
    assert found["python-lint"][:4] == ("uv", "run", "ruff", "check") and found["python-types"] == ("uv", "run", "mypy")
    assert found["python-unit"] == ("uv", "run", "python", "-m", "pytest", "-q", "--junitxml=.xtm/pytest.xml")
    poetry = _argv(_detect({"svc/pyproject.toml": "[tool.poetry]\nname='x'\n"}, extra=("svc/poetry.lock", "svc/tests/test_b.py")))
    assert poetry["svc:python-install"] == ("poetry", "install", "--no-interaction") and poetry["svc:python-unit"][:2] == ("poetry", "run")
    reqs = _argv(_detect({}, extra=("requirements.txt", "requirements-dev.txt", ".flake8", "conftest.py")))
    assert reqs["python-install"] == ("python", "-m", "pip", "install", "-r", "requirements-dev.txt", "-r", "requirements.txt")
    assert reqs["python-lint"] == ("flake8", ".") and "python-unit" in reqs


def test_a_django_project_without_pytest_gets_its_manage_py_test_check() -> None:
    django = _argv(_detect({}, extra=("requirements.txt", "manage.py", "app/tests.py")))
    assert django["python-unit"] == ("python", "manage.py", "test", "--noinput")
    with_pytest = _argv(_detect({}, extra=("requirements.txt", "manage.py", "conftest.py")))
    assert with_pytest["python-unit"][:3] == ("python", "-m", "pytest")
    assert detect.classify(["python", "manage.py", "test", "--noinput"]) == ("python", "unit")
    assert detect.classify(["python", "manage.py", "makemigrations", "--check"]) is None


def test_python_reports_are_parsed():
    found = _detect({"pyproject.toml": "[tool.ruff]\n"}, extra=("tests/test_a.py",))
    assert found.checks["python-lint"].report == "sarif" and found.checks["python-lint"].report_path == ".xtm/ruff.sarif"
    assert found.checks["python-unit"].report == "junit" and found.checks["python-unit"].report_path == ".xtm/pytest.xml"


def test_jvm_maven_root_only_and_gradle_wrapper():
    pom = "<project><build><plugins><plugin><artifactId>spotless-maven-plugin</artifactId></plugin></plugins></build></project>"
    found = _argv(_detect({"pom.xml": pom, "api/pom.xml": "<project/>"}, extra=("mvnw",)))
    assert found["jvm-install"] == ("sh", "mvnw", "-B", "dependency:go-offline")
    assert found["jvm-lint"] == ("sh", "mvnw", "-B", "spotless:check") and found["jvm-unit"] == ("sh", "mvnw", "-B", "test")
    assert not any(name.startswith("api:") for name in found)
    gradle = _argv(_detect({"build.gradle.kts": "plugins { id(\"com.diffplug.spotless\") }"}, extra=("gradlew",)))
    assert gradle["jvm-lint"] == ("sh", "gradlew", "--no-daemon", "spotlessCheck")
    assert gradle["jvm-build"] == ("sh", "gradlew", "--no-daemon", "build", "-x", "test")


def test_rust_and_dbt():
    rust = _argv(_detect({"apps/runner/Cargo.toml": "[package]\nname='r'\n"}, extra=("apps/runner/Cargo.lock",)))
    assert rust["apps/runner:rust-unit"] == ("cargo", "test", "--locked")
    assert rust["apps/runner:rust-format"] == ("cargo", "fmt", "--all", "--check")
    dbt = _detect({"dbt/dbt_project.yml": "name: x\n"}, extra=("dbt/packages.yml", ".sqlfluff"))
    assert _argv(dbt)["dbt:dbt-install"] == ("dbt", "deps") and _argv(dbt)["dbt:dbt-lint"] == ("sqlfluff", "lint", ".")
    assert "profile" in dbt.checks["dbt:dbt-parse"].description


ENV_WORKFLOW = r"""
env:
  PYTHON_VERSION: "3.12"
  SHARED: workflow
  PATH: /opt/runner/bin
jobs:
  dbt:
    defaults:
      run:
        working-directory: kube-deployment/dbt
    env:
      PG_HOST: "localhost"
      PG_PORT: 5432
      PG_PASSWORD: ${{ secrets.PG_PASSWORD }}
      SHARED: ${{ vars.SHARED }}
      GITHUB_TOKEN: x
      DEBUG: false
    steps:
      - run: dbt deps --profiles-dir profiles
      - run: dbt parse --profiles-dir profiles
        env:
          DBT_TARGET: ci
  lint:
    steps:
      - run: ruff check .
"""


def test_a_workflow_check_carries_the_literal_env_of_its_workflow_job_and_step():
    found = _detect({".github/workflows/ci.yml": ENV_WORKFLOW})
    parse = dict(found.checks["kube-deployment/dbt:dbt-parse"].env)
    # Literal values travel, numbers and booleans as CI writes them into the environment.
    assert parse["PG_HOST"] == "localhost" and parse["PG_PORT"] == "5432" and parse["DEBUG"] == "false"
    assert parse["PYTHON_VERSION"] == "3.12" and parse["DBT_TARGET"] == "ci"
    # An expression (a secret, a variable) stays out and drops the outer literal of the same name.
    assert "PG_PASSWORD" not in parse and "SHARED" not in parse
    # The runner's own variables are the container's, never the workflow's.
    assert "PATH" not in parse and "GITHUB_TOKEN" not in parse
    # A step's env is its own: the sibling step and another job do not see it.
    assert "DBT_TARGET" not in dict(found.checks["kube-deployment/dbt:dbt-install"].env)
    lint = dict(found.checks["python-lint"].env)
    assert "PG_HOST" not in lint and lint["SHARED"] == "workflow"


def test_makefile_targets_run_on_the_stack_of_their_directory():
    make = "install:\n\tpip install .\nlint: install\n\truff .\nVAR := 1\nrelease:\n\ttwine upload\ntest:\n\tpytest\n"
    found = _detect({"lib/Makefile": make, "lib/package.json": "{}"})
    assert found.checks["lib:make-lint"].argv == ("make", "lint") and found.checks["lib:make-unit"].argv == ("make", "test")
    assert found.checks["lib:make-lint"].image == "nodejs-dev"
    assert not any("release" in " ".join(s.argv) for s in found.checks.values())


def test_compose_services_are_reported_as_infrastructure():
    compose = "services:\n  search:\n    image: opensearch:2\n  app:\n    build: .\n"
    found = _detect({"dev/docker-compose.yml": compose})
    assert [(i.name, i.image, i.source) for i in found.infra] == [
        ("search", "opensearch:2", "dev/docker-compose.yml"),
        ("app", "(built from the repository)", "dev/docker-compose.yml"),
    ]
    assert "- search: opensearch:2 (dev/docker-compose.yml)" in detect.describe_infra(found.infra)


def test_an_image_key_that_is_not_configured_falls_back_to_the_next_then_names_the_first():
    pkg = json.dumps({"scripts": {"test": "jest"}})
    assert _detect({"package.json": pkg}, images={"nodejs"}).checks["node-unit"].image == "nodejs"
    assert _detect({"Cargo.toml": ""}, images={"python"}).checks["rust-unit"].image == "toolchain-rust"


def test_a_helm_chart_gets_lint_and_template_checks_on_a_toolchain_image() -> None:
    found = _detect(
        {"charts/app/Chart.yaml": "name: app\n", "charts/app/charts/sub/Chart.yaml": "name: sub\n"},
        images={"python-dev", "toolchain-node"},
    )
    lint, template = found.checks["charts/app:helm-lint"], found.checks["charts/app:helm-parse"]
    assert (lint.argv, lint.default_args, lint.cwd) == (("helm", "lint"), (".",), "charts/app")
    assert template.argv == ("helm", "template", "xtm-check", ".")
    assert lint.image == template.image == "toolchain-node"
    assert not any(name.startswith("charts/app/charts/sub:") for name in found.checks)


def test_the_generic_toolchain_images_are_preferred_over_a_repositorys_locked_dev_images():
    pkg = json.dumps({"scripts": {"test": "jest"}})
    keys = {"python", "python-dev", "nodejs-dev", "toolchain-node", "toolchain-python", "toolchain-dbt"}
    assert _detect({"package.json": pkg}, images=keys).checks["node-unit"].image == "toolchain-node"
    assert _detect({"dbt_project.yml": "name: x\n"}, images=keys).checks["dbt-parse"].image == "toolchain-dbt"


# -- bounds and robustness -----------------------------------------------------


def test_candidates_skip_build_output_and_deep_paths_and_are_bounded():
    listing = [
        "node_modules/x/package.json",
        "a/b/c/d/e/f/package.json",
        "target/pom.xml",
        ".github/workflows/ci.yml",
        ".github/workflows/notes.md",
        "package.json",
        "src/app.py",
    ]
    assert detect.candidates(listing) == [".github/workflows/ci.yml", "package.json"]
    many = [f"p{i}/package.json" for i in range(detect.MAX_CANDIDATES + 50)]
    assert len(detect.candidates(many)) == detect.MAX_CANDIDATES


# -- a narrow checkout keeps what its checks need ------------------------


def test_companions_are_the_manifests_above_a_sub_path_the_workflows_and_the_checks_file():
    sizes = {
        "backend/pyproject.toml": 10,
        "backend/uv.lock": 10,
        "backend/ruff.toml": 10,
        "backend/app/core/config.py": 10,
        "backend/app/pytest.ini": 10,
        "backend/node_modules/x/package.json": 10,
        "package.json": 10,
        "yarn.lock": 10,
        "frontend/package.json": 10,
        "docs/Makefile": 10,
        ".github/workflows/ci.yml": 10,
        ".github/workflows/notes.md": 10,
        ".xtm-one/checks.toml": 10,
        "README.md": 10,
    }
    picked = detect.companions(sizes, ["backend/app/core"])
    assert picked == [
        ".github/workflows/ci.yml",
        "package.json",
        "yarn.lock",
        ".xtm-one/checks.toml",
        "backend/pyproject.toml",
        "backend/ruff.toml",
        "backend/uv.lock",
        "backend/app/pytest.ini",
    ], "a sibling's manifest, build output and plain files are not companions"


def test_a_test_runner_config_above_a_sub_path_is_a_companion():
    """A checkout of front/src/utils keeps the vitest, vite, jest and playwright configs its unit check loads."""
    sizes = {
        "front/package.json": 10,
        "front/vitest.config.ts": 10,
        "front/vitest.config.ci-unit.ts": 10,
        "front/vite.config.mts": 10,
        "front/jest.config.js": 10,
        "front/playwright.config.ts": 10,
        "front/vitest.workspace.ts": 10,
        "front/src/utils/a.test.ts": 10,
        "back/vitest.config.ts": 10,
        "front/vitest.config.ts.bak": 10,
    }
    assert detect.companions(sizes, ["front/src/utils"]) == [
        "front/jest.config.js",
        "front/package.json",
        "front/playwright.config.ts",
        "front/vite.config.mts",
        "front/vitest.config.ci-unit.ts",
        "front/vitest.config.ts",
        "front/vitest.workspace.ts",
    ], "a sibling package's config and a backup file are not companions"
    assert detect.is_referencing_config("front/vitest.config.ci-unit.ts")
    assert detect.is_referencing_config("front/vite.config.mts")


def test_the_setup_file_a_vitest_config_above_a_sub_path_names_is_kept():
    """setupFiles of a config above the sub_path resolve from the config's directory."""
    sizes = {"front/vitest.config.ci-unit.ts": 10, "front/src/setupTests.ts": 10, "front/src/utils/a.ts": 10}
    sources = {"front/vitest.config.ci-unit.ts": b"export default { test: { setupFiles: ['./src/setupTests.ts'] } }"}
    assert detect.referenced_files(sources, sizes, ["front/src/utils"]) == ["front/src/setupTests.ts"]


_DECLARED = checks.load_checks(
    '[check.unit]\nargv = ["pytest"]\ncwd = "backend"\n'
    '[check.lint]\nargv = ["ruff"]\ncwd = "backend"\n'
    '[check.pkg]\nargv = ["pytest"]\ncwd = "backend/app"\n'
    '[check.front]\nargv = ["yarn"]\ncwd = "frontend"\n'
    '[check.root]\nargv = ["make"]\n'
)


@pytest.mark.parametrize(
    ("subs", "kept", "widened"),
    [
        (["backend/app/services/x"], ["backend"], [("backend/app/services/x", "backend", ("lint", "unit"))]),
        (["backend"], ["backend"], []),
        (["docs", "frontend/src"], ["docs", "frontend"], [("frontend/src", "frontend", ("front",))]),
        (["backendish/x"], ["backendish/x"], []),
        (["backend/app/a", "backend/app/b", "backend"], ["backend"], [("backend/app/a", "backend", ("lint", "unit")), ("backend/app/b", "backend", ("lint", "unit"))]),
    ],
    ids=["inside", "equal", "one-of-two", "prefix-not-a-directory", "merged"],
)
def test_a_sub_path_inside_a_declared_check_directory_is_widened(subs, kept, widened):
    """The outermost directory a check runs from wins; the root cwd never widens."""
    got, reasons = detect.widen_to_check_dirs(subs, _DECLARED)
    assert got == kept
    assert [(w.sub_path, w.directory, w.checks) for w in reasons] == widened


def test_a_checkout_of_a_check_directory_takes_the_paths_its_checks_need():
    """frontend/tests_unit imports ../../apps/office/src/lib: a frontend/ checkout takes it too."""
    declared = checks.load_checks(
        '[check.types]\nargv = ["tsc"]\ncwd = "frontend"\nneeds = ["apps/office/src/lib", ".xtm-one/lab/host"]\n'
        '[check.unit]\nargv = ["vitest"]\ncwd = "frontend"\nneeds = ["apps/office/src/lib", "apps/office"]\n'
        '[check.py]\nargv = ["pytest"]\ncwd = "backend"\nneeds = ["docs/usage"]\n'
        '[check.root]\nargv = ["make"]\nneeds = ["scripts"]\n'
    )
    assert declared["types"].needs == ("apps/office/src/lib", ".xtm-one/lab/host")
    kept, _ = detect.widen_to_check_dirs(["frontend/package.json"], declared)
    # A need under another need is merged; another directory's needs are not taken. A root check runs
    # on every narrow checkout, so its needs always are (the editions gate reads LICENSE).
    assert detect.check_needs(kept, declared) == [".xtm-one/lab/host", "apps/office", "scripts"]
    # A need the sub_paths already hold is not added again; only the root check's without a check directory held.
    assert detect.check_needs(["frontend", "apps"], declared) == [".xtm-one/lab/host", "scripts"]
    assert detect.check_needs(["docs"], declared) == ["scripts"]
    # A checkout of a parent of the check directory holds the directory too.
    assert detect.check_needs(["backend"], declared) == ["docs/usage", "scripts"]
    # A whole-tree checkout (no sub_paths) holds everything: nothing is added.
    assert detect.check_needs([], declared) == []
    with pytest.raises(checks.ChecksError, match=r"\[check.x\] needs '\.\./out' must be a relative path"):
        checks.load_checks('[check.x]\nargv = ["x"]\nneeds = ["../out"]\n')


def test_nothing_is_widened_without_a_declaration_or_a_check_directory():
    assert detect.widen_to_check_dirs(["backend/app"], {}) == (["backend/app"], [])
    root_only = checks.load_checks('[check.root]\nargv = ["make"]\n')
    assert detect.widen_to_check_dirs(["backend/app"], root_only) == (["backend/app"], [])


def test_no_companion_without_sub_paths_or_under_them_or_past_the_bounds():
    assert detect.companions({"pyproject.toml": 1}, []) == []
    assert detect.companions({"src/pyproject.toml": 1, "pyproject.toml": 1}, ["src"]) == ["pyproject.toml"]
    assert detect.companions({"uv.lock": detect.MAX_COMPANION_BYTES + 1}, ["src"]) == []
    many = {f".github/workflows/w{i}.yml": 1 for i in range(detect.MAX_COMPANIONS + 20)}
    assert len(detect.companions(many, ["src"])) == detect.MAX_COMPANIONS


def test_a_narrow_checkout_keeps_the_actionlint_config_beside_the_workflows():
    sizes = {
        ".github/workflows/ci.yml": 1,
        ".github/actionlint.yaml": 1,
        ".github/actionlint.yml": 1,
        ".github/CODEOWNERS": 1,
        "docs/actionlint.yaml": 1,
    }
    kept = detect.companions(sizes, [".github/workflows/ci.yml"])
    assert kept == [".github/actionlint.yaml", ".github/actionlint.yml"]
    many = {f".github/workflows/w{i}.yml": 1 for i in range(detect.MAX_COMPANIONS + 20)}
    assert ".github/actionlint.yaml" in detect.companions({**many, ".github/actionlint.yaml": 1}, ["src"])


def test_a_narrow_checkout_with_its_companions_detects_its_checks():
    files = {"backend/pyproject.toml": "[tool.ruff]\n[tool.mypy]\n", "backend/app/core/x.py": ""}
    listing = [*files, "backend/uv.lock", "backend/tests/test_x.py"]
    narrow = [p for p in listing if p.startswith("backend/app/core/")]
    kept = detect.companions(dict.fromkeys(listing, 1), ["backend/app/core"])
    assert detect.detect(narrow, {}).checks == {}
    found = detect.detect([*narrow, *kept], {p: files[p].encode() for p in kept if p in files}, images=IMAGES)
    assert {"backend:python-install", "backend:python-lint", "backend:python-types"} <= set(found.checks)


# -- a per-project test script: bash run_test.sh <project>/test-requirements.txt --

SCRIPT_WORKFLOW = r"""
name: tests
on: [pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: astral-sh/setup-uv@v7
      - name: Run tests
        run: |
          for req_file in $(echo "${{ matrix.requirements }}" | tr ',' ' '); do
            bash run_test.sh "$req_file"
          done
"""


def test_a_per_project_test_script_gives_one_test_check_per_test_requirements_file():
    found = _detect(
        {
            ".github/workflows/ci-tests.yml": SCRIPT_WORKFLOW,
            "run_test.sh": "#!/bin/bash\n",
            "stream/a/tests/test-requirements.txt": "pytest\n",
            "stream/b/tests/test-requirements.txt": "pytest\n",
            "stream/c/src/requirements.txt": "pycti\n",
        },
        images={*IMAGES, "toolchain-python"},
    )
    test_checks = {name: spec for name, spec in found.checks.items() if name.endswith(":python-test")}
    assert set(test_checks) == {"stream/a/tests:python-test", "stream/b/tests:python-test"}
    spec = test_checks["stream/a/tests:python-test"]
    assert spec.argv == (
        "sh",
        "-c",
        detect.SCRIPT_TEST_SHELL,
        "xtm-test",
        "run_test.sh",
        "stream/a/tests/test-requirements.txt",
        detect.LOCAL_REQUIREMENTS_SCRIPT,
    )
    assert spec.cwd == "" and spec.image == "toolchain-python" and spec.report == "junit" and spec.report_path == ".xtm/junit.xml"
    assert "bash run_test.sh stream/a/tests/test-requirements.txt" in spec.description
    assert checks.describe_checks({spec.name: spec}).count("\n") == 0, "the inline script is named, not printed"


def test_no_test_check_without_the_script_or_a_requirements_step():
    files = {"stream/a/tests/test-requirements.txt": "pytest\n"}
    assert not any(n.endswith(":python-test") for n in _detect({".github/workflows/t.yml": SCRIPT_WORKFLOW, **files}).checks)
    plain = SCRIPT_WORKFLOW.replace("${{ matrix.requirements }}", "${{ matrix.dirs }}").replace("req_file", "d")
    found = _detect({".github/workflows/t.yml": plain, "run_test.sh": "", **files})
    assert not any(n.endswith(":python-test") for n in found.checks), "a step that names no requirements is not a per-project test"


def test_a_test_script_above_a_sub_path_is_a_companion():
    sizes = {"run_test.sh": 10, "scripts/release.sh": 10, "stream/a/src/main.py": 10, "stream/run-tests.sh": 10}
    assert detect.companions(sizes, ["stream/a"]) == ["run_test.sh", "stream/run-tests.sh"]


def test_subdirectory_dependencies_name_the_repository_directories_a_requirement_installs_from():
    requirements = {
        "stream/a/src/requirements.txt": b"pycti==1\nsdk @ git+https://github.com/o/r.git@master#subdirectory=connectors-sdk\n",
        "stream/a/tests/test-requirements.txt": b"x @ git+https://github.com/o/other.git#subdirectory=client-python\n",
    }
    dirs = {"connectors-sdk", "stream", "stream/a", "stream/a/src"}
    assert detect.subdirectory_dependencies(requirements, dirs, ["stream/a"]) == ["connectors-sdk"]
    assert detect.subdirectory_dependencies(requirements, dirs, ["stream/a", "connectors-sdk"]) == []
    assert detect.is_requirements("a/test-requirements.txt") and not detect.is_requirements("a/README.md")


def _pom(artifact: str, *, deps: tuple[str, ...] = (), processors: tuple[str, ...] = (), modules=()) -> bytes:
    ns = 'xmlns="http://maven.apache.org/POM/4.0.0"'
    parent = "" if artifact == "root" else "<parent><groupId>io.x</groupId><artifactId>root</artifactId></parent>"
    group = "<groupId>io.x</groupId>" if artifact == "root" else ""
    body = "".join(f"<dependency><groupId>io.x</groupId><artifactId>{d}</artifactId></dependency>" for d in deps)
    paths = "".join(
        f"<path><groupId>${{project.groupId}}</groupId><artifactId>{p}</artifactId></path>" for p in processors
    )
    mods = "".join(f"<module>{m}</module>" for m in modules)
    return (
        f"<project {ns}>{parent}{group}<artifactId>{artifact}</artifactId>"
        f"<modules>{mods}</modules><dependencies>{body}"
        "<dependency><groupId>org.other</groupId><artifactId>lib</artifactId></dependency></dependencies>"
        f"<build><plugins><plugin><configuration><annotationProcessorPaths>{paths}"
        "</annotationProcessorPaths></configuration></plugin></plugins></build></project>"
    ).encode()


_MAVEN_POMS = {
    "pom.xml": _pom("root", modules=("model", "client", "tools", "api", "missing")),
    "model/pom.xml": _pom("model", deps=("client",), processors=("tools",)),
    "client/pom.xml": _pom("client", processors=("tools",)),
    "tools/pom.xml": _pom("tools"),
    "api/pom.xml": _pom("api", deps=("model",)),
}


def test_a_narrow_maven_module_keeps_the_sibling_modules_it_builds_against():
    added, module_poms = detect.maven_reactor_paths(_MAVEN_POMS, ["model"])
    assert added == ["client", "tools"], "a dependency and an annotation processor, transitively; api depends on it"
    assert module_poms == ["model/pom.xml", "client/pom.xml", "tools/pom.xml", "api/pom.xml"], (
        "every module the root lists and the repository has, so the reactor reads"
    )
    assert detect.maven_reactor_paths(_MAVEN_POMS, ["model", "client", "tools"])[0] == []
    assert detect.maven_reactor_paths({"pom.xml": b"<project><unclosed>"}, ["model"]) == ([], [])
    assert detect.maven_reactor_paths(_MAVEN_POMS, []) == ([], [])


def test_a_narrow_checkout_of_a_maven_module_writes_its_reactor():
    import io
    import tarfile

    from app.services.sandbox_workspace.repository import extract_tree

    members = {f"r-sha/{path}": content for path, content in _MAVEN_POMS.items()}
    members.update({f"r-sha/{m}/src/A.java": b"class A {}" for m in ("model", "client", "tools", "api")})
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    tree = extract_tree(buffer.getvalue(), sub_paths=["model"], max_bytes=10 * 1024 * 1024)
    written = {path for path, _ in tree.files}
    assert tree.reactor == ["client", "tools"]
    assert {"model/src/A.java", "client/src/A.java", "tools/src/A.java", "api/pom.xml", "pom.xml"} <= written
    assert "api/src/A.java" not in written, "a module that depends on the sub_path is not built, only listed"


_WORKSPACE_SIZES = {
    "package.json": 10,
    "yarn.lock": 10,
    ".yarnrc.yml": 10,
    "packages/chatbot/package.json": 10,
    "packages/chatbot/src/index.ts": 10,
    "packages/ui/package.json": 10,
    "packages/ui/node_modules/dep/package.json": 10,
    "packages/icon/package.json": 10,
    "projects/website/package.json": 10,
    "projects/website/fixtures/package.json": 10,
    "tools/script/package.json": 10,
}


def test_a_yarn_workspaces_member_keeps_every_member_manifest():
    roots = {"package.json": json.dumps({"workspaces": ["packages/*", "projects/*"]}).encode()}
    assert detect.workspace_member_manifests(roots, _WORKSPACE_SIZES, ["packages/chatbot"]) == [
        "packages/icon/package.json",
        "packages/ui/package.json",
        "projects/website/package.json",
    ], "the sub_path's own, a nested fixture, an install's output and a non-member are left out"
    nested = {"package.json": json.dumps({"workspaces": {"packages": ["packages/**", "!packages/icon"]}}).encode()}
    assert detect.workspace_member_manifests(nested, _WORKSPACE_SIZES, ["packages/chatbot"]) == [
        "packages/ui/package.json"
    ]


def test_a_pnpm_workspace_member_keeps_every_member_manifest():
    roots = {"pnpm-workspace.yaml": b"packages:\n  - 'packages/*'\n  - tools/*\n"}
    assert detect.workspace_member_manifests(roots, _WORKSPACE_SIZES, ["packages/ui"]) == [
        "packages/chatbot/package.json",
        "packages/icon/package.json",
        "tools/script/package.json",
    ]


def test_no_member_manifest_without_a_workspaces_declaration():
    assert (
        detect.workspace_member_manifests({"package.json": b'{"name": "x"}'}, _WORKSPACE_SIZES, ["packages/ui"]) == []
    )
    assert detect.workspace_member_manifests({"package.json": b"not json"}, _WORKSPACE_SIZES, ["packages/ui"]) == []
    assert detect.workspace_member_manifests({"pnpm-workspace.yaml": b"- a\n"}, _WORKSPACE_SIZES, ["packages/ui"]) == []


def test_a_narrow_checkout_of_a_workspaces_member_keeps_the_lock_and_every_member_manifest():
    import io
    import tarfile

    from app.services.sandbox_workspace.repository import extract_tree

    members = {f"r-sha/{path}": b"{}" for path in _WORKSPACE_SIZES}
    members["r-sha/package.json"] = json.dumps({"workspaces": ["packages/*", "projects/*"]}).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    tree = extract_tree(buffer.getvalue(), sub_paths=["packages/chatbot"], max_bytes=10 * 1024 * 1024)
    written = {path for path, _ in tree.files}
    assert {
        "package.json",
        "yarn.lock",
        ".yarnrc.yml",
        "packages/ui/package.json",
        "packages/icon/package.json",
        "projects/website/package.json",
        "packages/chatbot/src/index.ts",
    } <= written
    assert not written & {
        "tools/script/package.json",
        "projects/website/fixtures/package.json",
        "packages/ui/node_modules/dep/package.json",
    }
    assert "packages/ui/package.json" in tree.companions


_PREPARE = (
    "cd packages/core && tsup && node scripts/rewrite.mjs && node -e \"const fs=require('node:fs');"
    " fs.copyFileSync('src/theme.css','dist/theme.css'); fs.copyFileSync('../../llms.txt','dist/llms.txt');\""
)
_LIFECYCLE_SIZES = {
    "package.json": 10,
    "pnpm-lock.yaml": 10,
    "pnpm-workspace.yaml": 10,
    "llms.txt": 10,
    "README.md": 10,
    "packages/core/package.json": 10,
    "packages/core/scripts/rewrite.mjs": 10,
    "packages/core/src/theme.css": 10,
    "packages/core/src/index.ts": 10,
    "packages/chatbot/package.json": 10,
    "packages/chatbot/src/index.ts": 10,
    "packages/docs/package.json": 10,
}


def test_the_root_prepare_script_adds_the_member_it_builds_and_the_root_file_it_copies():
    roots = {
        "package.json": json.dumps(
            {"scripts": {"prepare": _PREPARE, "build": "cd packages/docs && next build"}}
        ).encode()
    }
    dirs, files = detect.install_lifecycle_paths(roots, _LIFECYCLE_SIZES, ["packages/chatbot"])
    assert dirs == ["packages/core"], "a script an install does not run (build) adds nothing"
    assert files == ["llms.txt"], "the files under the added member are written with it"
    assert detect.install_lifecycle_paths(roots, _LIFECYCLE_SIZES, ["packages/core"]) == ([], ["llms.txt"])


def test_a_lifecycle_script_out_of_the_repository_or_without_paths_adds_nothing():
    sizes = {"package.json": 10, "packages/a/package.json": 10}
    for script in ("husky", "cd .. && make", "cd /opt/app && tsup", "cd missing && tsup", "cd node_modules/x && tsup"):
        roots = {"package.json": json.dumps({"scripts": {"postinstall": script}}).encode()}
        assert detect.install_lifecycle_paths(roots, sizes, ["packages/a"]) == ([], []), script
    assert detect.install_lifecycle_paths({"package.json": b"not json"}, sizes, ["packages/a"]) == ([], [])
    assert detect.install_lifecycle_paths({"pnpm-workspace.yaml": b"packages: []"}, sizes, ["packages/a"]) == ([], [])


def test_a_narrow_checkout_of_a_member_keeps_what_the_root_prepare_script_builds():
    import io
    import tarfile

    from app.services.sandbox_workspace.repository import extract_tree

    members = {f"r-sha/{path}": b"{}" for path in _LIFECYCLE_SIZES}
    members["r-sha/package.json"] = json.dumps({"scripts": {"prepare": _PREPARE}}).encode()
    members["r-sha/pnpm-workspace.yaml"] = b"packages:\n  - packages/*\n"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    tree = extract_tree(buffer.getvalue(), sub_paths=["packages/chatbot"], max_bytes=10 * 1024 * 1024)
    written = {path for path, _ in tree.files}
    assert tree.lifecycle == ["packages/core"] and tree.sub_paths == ["packages/chatbot", "packages/core"]
    assert {
        "llms.txt",
        "packages/core/src/index.ts",
        "packages/core/scripts/rewrite.mjs",
        "packages/docs/package.json",
    } <= written
    assert "llms.txt" in tree.companions
    assert "README.md" not in written


def _pkg(name: str, **deps: str) -> bytes:
    return json.dumps({"name": name, "devDependencies": deps}).encode()


def test_a_workspace_protocol_dependency_adds_the_sibling_member_transitively():
    manifests = {
        "package.json": json.dumps({"workspaces": ["platform/*"]}).encode(),
        "platform/front/package.json": _pkg(
            "front", **{"eslint-plugin-custom-rules": "workspace:*", "react": "19.0.0"}
        ),
        "platform/eslint-plugin-custom-rules/package.json": _pkg("eslint-plugin-custom-rules", shared="workspace:^"),
        "platform/shared/package.json": _pkg("shared"),
        "platform/graphql/package.json": _pkg("graphql-api"),
        "node_modules/shared/package.json": _pkg("shared"),
    }
    assert detect.workspace_dependency_dirs(manifests, ["platform/front"]) == [
        "platform/eslint-plugin-custom-rules",
        "platform/shared",
    ]
    assert detect.workspace_dependency_dirs(manifests, ["platform/graphql"]) == []
    assert detect.workspace_dependency_dirs(manifests, ["platform/front/src/utils"]) == [
        "platform/eslint-plugin-custom-rules",
        "platform/shared",
    ], "the member the sub_path lies in links them"
    assert detect.workspace_dependency_dirs(manifests, ["platform/eslint-plugin-custom-rules/lib"]) == [
        "platform/shared"
    ], "the member holding the sub_path is never added"
    assert detect.workspace_dependency_dirs(manifests, ["docs/guide"]) == [], "the root manifest is not an owner"
    assert detect.workspace_dependency_dirs(manifests, ["platform"]) == [], "a member under a sub_path adds nothing"
    broken = {"platform/front/package.json": b"not json", "platform/x/package.json": _pkg("x", y="workspace:*")}
    assert detect.workspace_dependency_dirs(broken, ["platform/front", "platform/x"]) == []


def test_a_narrow_checkout_of_a_member_keeps_the_sibling_it_links_by_workspace():
    import io
    import tarfile

    from app.services.sandbox_workspace.repository import extract_tree

    members = {
        "r-sha/package.json": json.dumps({"workspaces": ["platform/*"]}).encode(),
        "r-sha/yarn.lock": b"",
        "r-sha/platform/front/package.json": _pkg("front", **{"eslint-plugin-custom-rules": "workspace:*"}),
        "r-sha/platform/front/eslint.config.ts": b"import custom from 'eslint-plugin-custom-rules';",
        "r-sha/platform/eslint-plugin-custom-rules/package.json": _pkg("eslint-plugin-custom-rules"),
        "r-sha/platform/eslint-plugin-custom-rules/lib/index.ts": b"export default {};",
        "r-sha/platform/graphql/package.json": _pkg("graphql-api"),
        "r-sha/platform/graphql/src/index.ts": b"",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    tree = extract_tree(buffer.getvalue(), sub_paths=["platform/front"], max_bytes=10 * 1024 * 1024)
    written = {path for path, _ in tree.files}
    assert tree.workspace_links == ["platform/eslint-plugin-custom-rules"]
    assert "platform/eslint-plugin-custom-rules" in tree.sub_paths
    assert "platform/eslint-plugin-custom-rules/lib/index.ts" in written
    assert "platform/graphql/package.json" in written and "platform/graphql/src/index.ts" not in written


def test_an_eslint_config_import_by_member_name_adds_that_member() -> None:
    manifests = {
        "package.json": json.dumps({"name": "app", "workspaces": ["eslint-rules", "front", "back"]}).encode(),
        "eslint-rules/package.json": _pkg("eslint-rules"),
        "front/package.json": _pkg("@app/front"),
        "back/package.json": _pkg("@app/back"),
    }
    configs = {
        "eslint.config.ts": b"import json from '@eslint/json';\nimport { x } from 'eslint-rules/src/rules';",
        "front/local.eslint.config.ts": b"import { linguiRules } from \"eslint-rules/src/rules\";\nimport('./local.ts');",
    }
    assert detect.is_lint_config("front/local.eslint.config.ts") and detect.is_lint_config("eslint.config.mjs")
    assert not detect.is_lint_config("front/eslint.config.test.ts")
    assert detect.lint_config_member_dirs(configs, manifests, ["front"]) == ["eslint-rules"]
    assert detect.lint_config_member_dirs(configs, manifests, ["front/src"]) == ["eslint-rules"]
    assert detect.lint_config_member_dirs(configs, manifests, ["front", "eslint-rules"]) == [], "already kept"
    assert detect.lint_config_member_dirs(configs, manifests, ["eslint-rules/src"]) == [], "the member holding it"
    assert detect.lint_config_member_dirs({"eslint.config.ts": b"import x from 'react';"}, manifests, ["front"]) == []


def test_a_narrow_checkout_keeps_the_member_its_eslint_config_imports() -> None:
    import io
    import tarfile

    from app.services.sandbox_workspace.repository import extract_tree

    members = {
        "r-sha/package.json": json.dumps({"name": "app", "workspaces": ["eslint-rules", "front"]}).encode(),
        "r-sha/yarn.lock": b"",
        "r-sha/eslint.config.ts": b"import { rules } from 'eslint-rules/src/rules';",
        "r-sha/eslint-rules/package.json": _pkg("eslint-rules"),
        "r-sha/eslint-rules/src/rules.ts": b"export const rules = [];",
        "r-sha/front/package.json": _pkg("@app/front"),
        "r-sha/front/src/a.ts": b"",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    tree = extract_tree(buffer.getvalue(), sub_paths=["front"], max_bytes=10 * 1024 * 1024)
    written = {path for path, _ in tree.files}
    assert tree.workspace_links == ["eslint-rules"]
    assert "eslint-rules/src/rules.ts" in written


def test_a_patch_resolution_keeps_the_patch_file_it_names():
    sizes = {
        "package.json": 10,
        "patch/react-relay.patch": 10,
        "patch/a.patch": 10,
        "patch/b.patch": 10,
        "libs/shared/index.js": 10,
        "libs/shared/package.json": 10,
        "front/package.json": 10,
        "front/src/a.ts": 10,
        ".yarn/releases/yarn-4.cjs": 10,
        ".yarn/plugins/p.cjs": 10,
        "README.md": 10,
    }
    root = {
        "resolutions": {
            "react-relay": "patch:react-relay@21.0.1#./patch/react-relay.patch",
            "x": "patch:x@1.0.0#~/patch/a.patch&./patch/b.patch::version=1.0.0",
            "axios": "1.21.0",
        },
        "devDependencies": {"shared": "file:./libs/shared", "gone": "link:../outside", "abs": "portal:/opt/x"},
    }
    manifests = {
        "package.json": json.dumps(root).encode(),
        "front/package.json": json.dumps({"dependencies": {"self": "file:./src/a.ts"}}).encode(),
        ".yarnrc.yml": b"yarnPath: .yarn/releases/yarn-4.cjs\nplugins:\n  - path: .yarn/plugins/p.cjs\n  - https://example.invalid/p.js\n",
    }
    found = detect.package_local_files(manifests, sizes, ["front/src"])
    assert found == [
        ".yarn/plugins/p.cjs",
        ".yarn/releases/yarn-4.cjs",
        "libs/shared/index.js",
        "libs/shared/package.json",
        "patch/a.patch",
        "patch/b.patch",
        "patch/react-relay.patch",
    ]
    assert detect.package_local_files({"package.json": b"not json"}, sizes, ["front"]) == []


def test_a_narrow_checkout_keeps_the_root_patch_the_install_applies():
    import io
    import tarfile

    from app.services.sandbox_workspace.repository import extract_tree

    root = {
        "workspaces": ["platform/front", "platform/api"],
        "resolutions": {"react-relay": "patch:react-relay@21.0.1#./patch/react-relay.patch"},
    }
    api = {"dependencies": {"ldap": "patch:ldap@0.3.3#../../patch/ldap.patch"}}
    members = {
        "r-sha/package.json": json.dumps(root).encode(),
        "r-sha/yarn.lock": b"",
        "r-sha/patch/react-relay.patch": b"diff\n",
        "r-sha/patch/ldap.patch": b"diff\n",
        "r-sha/patch/unused.patch": b"diff\n",
        "r-sha/platform/front/package.json": b"{}",
        "r-sha/platform/front/src/utils/a.ts": b"",
        "r-sha/platform/api/package.json": json.dumps(api).encode(),
        "r-sha/platform/api/src/x.ts": b"",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    tree = extract_tree(buffer.getvalue(), sub_paths=["platform/front/src/utils"], max_bytes=1024 * 1024)
    written = {path for path, _ in tree.files}
    assert tree.local == ["patch/ldap.patch", "patch/react-relay.patch"]
    assert {"patch/react-relay.patch", "patch/ldap.patch", "platform/api/package.json"} <= written
    assert "patch/unused.patch" not in written and "platform/api/src/x.ts" not in written


def test_a_narrow_checkout_keeps_the_test_script_and_the_package_a_requirement_installs_from():
    import io
    import tarfile

    from app.services.sandbox_workspace.repository import extract_tree

    members = {
        "r-sha/run_test.sh": b"#!/bin/bash\n",
        "r-sha/.github/workflows/ci-tests.yml": SCRIPT_WORKFLOW.encode(),
        "r-sha/connectors-sdk/pyproject.toml": b"[project]\nname = 'sdk'\n",
        "r-sha/connectors-sdk/sdk/__init__.py": b"",
        "r-sha/stream/a/src/requirements.txt": b"sdk @ git+https://github.com/o/r.git@master#subdirectory=connectors-sdk\n",
        "r-sha/stream/a/tests/test-requirements.txt": b"-r ../src/requirements.txt\npytest\n",
        "r-sha/stream/b/src/main.py": b"",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    tree = extract_tree(buffer.getvalue(), sub_paths=["stream/a"], max_bytes=1024 * 1024)
    written = {path for path, _data in tree.files}
    assert tree.dependencies == ["connectors-sdk"] and tree.sub_paths == ["stream/a", "connectors-sdk"]
    assert {"run_test.sh", "connectors-sdk/sdk/__init__.py", "stream/a/tests/test-requirements.txt"} <= written
    assert "stream/b/src/main.py" not in written
    found = detect.detect(sorted(written), {p: d for p, d in tree.files if p in detect.candidates(written)}, images=IMAGES)
    assert "stream/a/tests:python-test" in found.checks


def test_the_local_requirements_script_points_a_repository_package_at_the_checkout(tmp_path, monkeypatch):
    import subprocess
    import sys

    (tmp_path / "connectors-sdk").mkdir()
    project = tmp_path / "stream" / "a"
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir()
    remote = "sdk[x] @ git+https://github.com/o/r.git@master#subdirectory=connectors-sdk\n"
    absent = "other @ git+https://github.com/o/r.git@master#subdirectory=missing\n"
    (project / "src" / "requirements.txt").write_text("pycti==1\n" + remote + absent)
    (project / "tests" / "test-requirements.txt").write_text("-r ../src/requirements.txt\npytest\n")
    monkeypatch.chdir(tmp_path)
    run = subprocess.run(  # noqa: S603 - the interpreter running the tests on the module's own script
        [sys.executable, "-c", detect.LOCAL_REQUIREMENTS_SCRIPT, "stream/a/tests"], capture_output=True, text=True, check=True
    )
    text = (project / "src" / "requirements.txt").read_text()
    assert f"sdk[x] @ {(tmp_path / 'connectors-sdk').resolve().as_uri()}\n" in text
    assert "pycti==1\n" in text and absent in text, "a directory the checkout does not hold is left as written"
    assert "installs the checkout's own packages" in run.stdout
    assert (project / "tests" / "test-requirements.txt").read_text().startswith("-r ../src/requirements.txt")


LINT_WORKFLOW = r"""
name: Lint
on: [pull_request]
jobs:
  format:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - uses: astral-sh/setup-uv@abc
      - run: uv venv
      - run: uv pip install -r ci-requirements.txt
      - run: uv run isort --profile black --check-only --diff .
  flake:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - run: uv pip install flake8
      - run: uv run flake8 --ignore=E,W .
"""


def test_a_lint_tool_the_job_installed_runs_in_an_ephemeral_environment_on_the_jobs_python():
    found = _detect(
        {".github/workflows/ci-lint.yml": LINT_WORKFLOW, "ci-requirements.txt": "isort\nblack\n"},
        images={*IMAGES, "toolchain-python"},
    )
    lint, fmt = found.checks["python-lint"], found.checks["python-format"]
    assert lint.argv == ("uv", "run", "--no-project", "--with", "flake8", "flake8", "--ignore=E,W")
    assert lint.default_args == (".",)
    assert fmt.argv == (
        "uv", "run", "--no-project", "--with-requirements", "ci-requirements.txt",
        "isort", "--profile", "black", "--check-only", "--diff",
    )  # fmt: skip
    assert dict(lint.env)["UV_PYTHON"] == "3.12" and dict(fmt.env)["UV_PYTHON"] == "3.12"


def test_a_tool_line_with_no_install_before_it_is_kept_as_written():
    workflow = LINT_WORKFLOW.replace("      - run: uv pip install flake8\n", "")
    found = _detect({".github/workflows/ci-lint.yml": workflow}, images={*IMAGES, "toolchain-python"})
    assert found.checks["python-lint"].argv == ("uv", "run", "flake8", "--ignore=E,W")
    # A bare tool line the job installed the tool for runs with it too (#714).
    bare = LINT_WORKFLOW.replace("uv run flake8", "flake8")
    found = _detect({".github/workflows/ci-lint.yml": bare}, images={*IMAGES, "toolchain-python"})
    assert found.checks["python-lint"].argv == (
        "uv",
        "run",
        "--no-project",
        "--with",
        "flake8",
        "flake8",
        "--ignore=E,W",
    )
    # A version-pinned or extras install names the tool by its package name; the pin stays in --with.
    pinned = bare.replace("uv pip install flake8", 'pip install "flake8>=6" black[jupyter]==24.1.0')
    found = _detect({".github/workflows/ci-lint.yml": pinned}, images={*IMAGES, "toolchain-python"})
    assert found.checks["python-lint"].argv[:3] == ("uv", "run", "--no-project")
    assert "flake8>=6" in found.checks["python-lint"].argv
    assert detect._requirement_name("Black[jupyter]==24.1.0") == "black"
    assert detect._requirement_name("pip>=24") == "pip"
    # A bare tool the job did not install, a pip self-upgrade, a types line and a launcher are kept.
    other = LINT_WORKFLOW.replace("uv run flake8", "pylint").replace(
        "uv pip install flake8", "pip3 install -U pip certifi"
    )
    found = _detect({".github/workflows/ci-lint.yml": other}, images={*IMAGES, "toolchain-python"})
    assert found.checks["python-lint"].argv == ("pylint", "--ignore=E,W")
    assert detect._installed(["pip3", "install", "-U", "pip", "setuptools", "wheel", "certifi"], "", frozenset()) == [
        "--with",
        "certifi",
    ]
    tools = ["--with", "mypy"]
    assert detect._with_job_tools("python", "types", ["mypy", "."], ["mypy", "."], tools, "") == ["mypy", "."]
    assert detect._with_job_tools(
        "python", "lint", ["poetry", "run", "flake8"], ["poetry", "run", "flake8"], tools, ""
    ) == [
        "poetry",
        "run",
        "flake8",
    ]


def test_a_project_requiring_an_older_python_runs_on_the_one_the_image_carries():
    pyproject = "[project]\nname = 'sdk'\nrequires-python = '>=3.11, <3.13'\n"
    found = _detect(
        {"sdk/pyproject.toml": pyproject, "sdk/tests/test_a.py": "", "app/pyproject.toml": "[project]\nname = 'app'\n"},
        images={*IMAGES, "toolchain-python"},
    )
    install, unit = found.checks["sdk:python-install"], found.checks["sdk:python-unit"]
    assert dict(install.env)["UV_PYTHON"] == "3.12" and install.argv[0] == "python3.12"
    assert unit.argv[:3] == ("python3.12", "-m", "pytest")
    assert "UV_PYTHON" not in dict(found.checks["app:python-install"].env), "no requirement: the image default"


def test_a_python_version_file_wins_and_an_image_without_the_interpreter_changes_nothing():
    files = {"svc/pyproject.toml": "[project]\nname = 's'\nrequires-python = '>=3.10'\n", "svc/.python-version": "3.12\n"}
    found = _detect(files, images={*IMAGES, "toolchain-python"})
    assert dict(found.checks["svc:python-install"].env)["UV_PYTHON"] == "3.12"
    plain = _detect(files, images=IMAGES)
    assert plain.checks["svc:python-install"].env == () and plain.checks["svc:python-install"].argv[0] == "python"


def test_the_repositorys_ci_python_applies_to_a_project_that_declares_none():
    found = _detect(
        {".github/workflows/ci-lint.yml": LINT_WORKFLOW, "stream/a/src/requirements.txt": "pycti\n"},
        images={*IMAGES, "toolchain-python"},
    )
    assert dict(found.checks["stream/a/src:python-install"].env)["UV_PYTHON"] == "3.12"


def test_check_support_files_are_the_repository_files_a_check_command_names():
    specs = {
        "integration": checks.CheckSpec(
            name="integration",
            argv=("python", "../.xtm-one/stores.py", "-m", "integration", "--junitxml=.xtm/x.xml"),
            cwd="backend",
        ),
        "root": checks.CheckSpec(name="root", argv=("bash", "scripts/lint.sh", "/etc/passwd", "../../out")),
        "dir": checks.CheckSpec(name="dir", argv=("pytest",), cwd="backend", default_args=("tests",)),
    }
    sizes = {".xtm-one/stores.py": 10, "scripts/lint.sh": 10, "backend/tests/test_a.py": 10, "etc/passwd": 1}
    assert detect.check_support_files(specs, sizes) == [".xtm-one/stores.py", "scripts/lint.sh"]
    assert detect.check_support_files(specs, {".xtm-one/stores.py": detect.MAX_COMPANION_BYTES + 1}) == []


def test_a_narrow_checkout_keeps_the_script_a_declared_check_runs():
    import io
    import tarfile

    from app.services.sandbox_workspace.repository import extract_tree

    checks_toml = (
        '[check.backend-integration]\nargv = ["python", "../.xtm-one/stores.py", "-m", "integration"]\n'
        'cwd = "backend"\nimage = "python-dev"\n'
    )
    members = {
        "r-sha/.xtm-one/checks.toml": checks_toml.encode(),
        "r-sha/.xtm-one/stores.py": b"print('stores')\n",
        "r-sha/.xtm-one/lab/lab.py": b"",
        "r-sha/backend/app/__init__.py": b"",
        "r-sha/frontend/src/main.ts": b"",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    tree = extract_tree(buffer.getvalue(), sub_paths=["backend/app"], max_bytes=1024 * 1024)
    written = {path for path, _data in tree.files}
    assert {".xtm-one/checks.toml", ".xtm-one/stores.py", "backend/app/__init__.py"} <= written
    assert ".xtm-one/stores.py" in tree.companions
    assert ".xtm-one/lab/lab.py" not in written and "frontend/src/main.ts" not in written


def test_a_narrow_checkout_takes_what_its_check_directory_needs_outside_it():
    import io
    import tarfile

    from app.services.sandbox_workspace.repository import extract_tree

    checks_toml = '[check.types]\nargv = ["tsc"]\ncwd = "frontend"\nneeds = ["apps/office/src/lib"]\n'
    members = {
        "r-sha/.xtm-one/checks.toml": checks_toml.encode(),
        "r-sha/frontend/package.json": b"{}",
        "r-sha/frontend/tests_unit/a.test.ts": b"import '../../apps/office/src/lib/x.ts'",
        "r-sha/apps/office/src/lib/x.ts": b"export {}",
        "r-sha/apps/office/src/App.tsx": b"",
        "r-sha/backend/app/__init__.py": b"",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    tree = extract_tree(buffer.getvalue(), sub_paths=["frontend/package.json"], max_bytes=1024 * 1024)
    written = {path for path, _data in tree.files}
    assert tree.needed == ["apps/office/src/lib"] and tree.sub_paths == ["frontend", "apps/office/src/lib"]
    assert {"frontend/tests_unit/a.test.ts", "apps/office/src/lib/x.ts"} <= written
    assert "apps/office/src/App.tsx" not in written and "backend/app/__init__.py" not in written
    # A checkout of another directory takes nothing for it.
    assert extract_tree(buffer.getvalue(), sub_paths=["backend"], max_bytes=1024 * 1024).needed == []


def _narrow_tree(members: dict[str, bytes], sub_paths: list[str], checks_toml: str = ""):
    import io
    import tarfile

    from app.services.sandbox_workspace.repository import extract_tree

    if checks_toml:
        members = {".xtm-one/checks.toml": checks_toml.encode(), **members}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo("r-sha/" + name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    tree = extract_tree(buffer.getvalue(), sub_paths=sub_paths, max_bytes=1024 * 1024)
    return tree, {path for path, _data in tree.files}


def test_a_narrow_checkout_keeps_the_formatter_and_linter_configs_above_it():
    # A format check of packages/ui alone read the repository's .prettierrc and
    # .prettierignore from the root: without them it reported the whole tree.
    tree, written = _narrow_tree(
        {
            ".prettierrc.json": b"{}",
            ".prettierignore": b"dist\n",
            ".editorconfig": b"root = true\n",
            "biome.json": b"{}",
            "eslint.config.mjs": b"export default []",
            "tsconfig.base.json": b"{}",
            "packages/ui/src/a.ts": b"export {}",
            "packages/other/.prettierrc": b"{}",
            "docs/tsconfig.json": b"{}",
        },
        ["packages/ui/src"],
    )
    for config in (
        ".prettierrc.json",
        ".prettierignore",
        ".editorconfig",
        "biome.json",
        "eslint.config.mjs",
        "tsconfig.base.json",
    ):
        assert config in written and config in tree.companions
    # A sibling's or another directory's config is not above the sub_path.
    assert "packages/other/.prettierrc" not in written and "docs/tsconfig.json" not in written


_CONNECTORS_LINT = b"""
on: pull_request
jobs:
  ensure_formatting:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - run: uv venv
      - run: uv pip install -r ci-requirements.txt
      - run: uv run isort --profile black --line-length 88 --check-only --diff .
      - run: uv run black --check --diff .
  base_linter:
    runs-on: ubuntu-latest
    steps:
      - run: uv pip install -r ./shared/pylint_plugins/requirements.txt
      - run: uv run flake8 --ignore=E,W .
"""


def test_a_narrow_checkout_keeps_the_root_requirements_and_python_tool_configs_its_ci_reads():
    # connectors: the format job installs isort and black from the root
    # ci-requirements.txt and isort reads .isort.cfg; a checkout of one
    # connector had neither, so the sandbox format check installed nothing
    # and reported a false diff.
    tree, written = _narrow_tree(
        {
            ".github/workflows/ci-lint-format.yml": _CONNECTORS_LINT,
            "ci-requirements.txt": b"black==24.1.0\nisort==5.13.2\n",
            ".isort.cfg": b"[settings]\nprofile=black\n",
            ".pylintrc": b"[MAIN]\n",
            ".pre-commit-config.yaml": b"repos: []\n",
            "shared/pylint_plugins/requirements.txt": b"pylint\n",
            "shared/pylint_plugins/plugin.py": b"",
            "external-import/acme/src/main.py": b"",
            "external-import/other/src/main.py": b"",
        },
        ["external-import/acme"],
    )
    for kept in ("ci-requirements.txt", ".isort.cfg", ".pylintrc", ".pre-commit-config.yaml"):
        assert kept in written and kept in tree.companions
    assert set(tree.named) == {"ci-requirements.txt", "shared/pylint_plugins/requirements.txt"}
    assert "shared/pylint_plugins/plugin.py" not in written
    assert "external-import/other/src/main.py" not in written


def test_a_narrow_checkout_keeps_the_root_secret_scanner_config() -> None:
    # xtm-one: the secrets check runs `betterleaks dir .` from the root, which loads
    # .betterleaks.toml (useDefault plus the allow-list of fake-secret fixtures); a
    # checkout of backend/ alone lacked it and reported the allow-listed fixtures.
    tree, written = _narrow_tree(
        {
            ".betterleaks.toml": b"[extend]\nuseDefault = true\n",
            ".gitleaksignore": b"",
            "backend/app/main.py": b"",
            "other/.betterleaks.toml": b"",
        },
        ["backend"],
    )
    for kept in (".betterleaks.toml", ".gitleaksignore"):
        assert kept in written and kept in tree.companions
    assert "other/.betterleaks.toml" not in written


def test_a_narrow_checkout_keeps_the_directory_a_ci_step_cds_into() -> None:
    # connectors: the STIX ID lint job runs `cd ./shared/pylint_plugins/check_stix_plugin &&
    # PYTHONPATH=. pylint ../../../ --load-plugins linter_stix_id_generator`; a checkout of one
    # connector kept the plugin's requirements.txt but not the plugin, so the sandbox lint
    # passed with "Plugin ... is impossible to load" and CI went red on the same files.
    workflow = b"""
on: pull_request
jobs:
  linter:
    runs-on: ubuntu-latest
    steps:
      - run: uv pip install -r ./shared/pylint_plugins/check_stix_plugin/requirements.txt
      - name: Run STIX ID pylint
        run: |
          cd ./shared/pylint_plugins/check_stix_plugin && \\
            PYTHONPATH=. pylint ../../../ \\
            --load-plugins linter_stix_id_generator
"""
    tree, written = _narrow_tree(
        {
            ".github/workflows/ci-lint-format.yml": workflow,
            "shared/pylint_plugins/check_stix_plugin/requirements.txt": b"pylint\n",
            "shared/pylint_plugins/check_stix_plugin/linter_stix_id_generator.py": b"",
            "shared/pylint_plugins/check_stix_plugin/__init__.py": b"",
            "shared/pylint_plugins/tests/test_plugin.py": b"",
            "external-import/acme/src/main.py": b"",
            "external-import/other/src/main.py": b"",
        },
        ["external-import/acme"],
    )
    plugin = "shared/pylint_plugins/check_stix_plugin/linter_stix_id_generator.py"
    assert plugin in tree.named and plugin in written
    assert "shared/pylint_plugins/check_stix_plugin/__init__.py" in written
    # Only the directory the step runs from: neither its siblings nor the repository it lints.
    assert "shared/pylint_plugins/tests/test_plugin.py" not in written
    assert "external-import/other/src/main.py" not in written


def test_a_step_that_cds_into_a_large_directory_keeps_none_of_it() -> None:
    files = {f"tools/big/f{i}.py": b"" for i in range(detect.MAX_REFERENCED_DIR_FILES + 1)}
    workflow = b"on: pull_request\njobs:\n  a:\n    steps:\n      - run: cd tools/big && python -m lint ../../src\n"
    tree, _written = _narrow_tree({".github/workflows/ci.yml": workflow, **files, "src/a.py": b""}, ["src"])
    assert not any(p.startswith("tools/big/") for p in tree.named)


def test_a_job_running_two_formatters_gives_a_check_for_each():
    # isort then black in one job: CI gates on both, one check per kind dropped black.
    found = detect.detect(
        [".github/workflows/ci-lint-format.yml"], {".github/workflows/ci-lint-format.yml": _CONNECTORS_LINT}
    )
    names = set(found.checks)
    assert {"python-format", "python-format-black", "python-lint"} <= names
    assert "isort" in found.checks["python-format"].argv
    assert "black" in found.checks["python-format-black"].argv


def test_a_second_tool_of_another_job_or_the_same_tool_twice_adds_no_check():
    workflow = b"""
on: pull_request
jobs:
  a:
    steps:
      - run: black --check src
      - run: black --check tests
  b:
    steps:
      - run: isort --check-only .
"""
    found = detect.detect([".github/workflows/ci.yml"], {".github/workflows/ci.yml": workflow})
    # The one check, and its write-mode sibling (test_sandbox_detect_formatter_apply.py).
    assert [n for n in found.checks if n.startswith("python-format")] == ["python-format", "python-format-apply"]


def test_a_narrow_checkout_keeps_the_files_its_package_scripts_name_outside_it():
    # A package's check:icons script runs a root script; the node check of the
    # package alone failed on a missing module.
    tree, written = _narrow_tree(
        {
            "package.json": b'{"scripts": {"lint:all": "node scripts/lint-all.mjs"}}',
            "scripts/lint-all.mjs": b"",
            "scripts/check-icons.mjs": b"",
            "scripts/unused.mjs": b"",
            "packages/ui/package.json": b'{"scripts": {"check:icons": "node ../../scripts/check-icons.mjs && tsc"}}',
            "packages/ui/src/a.ts": b"",
        },
        ["packages/ui"],
    )
    assert set(tree.named) == {"scripts/lint-all.mjs", "scripts/check-icons.mjs"}
    assert {"scripts/lint-all.mjs", "scripts/check-icons.mjs"} <= written
    assert "scripts/unused.mjs" not in written


def test_release_workflows_name_no_companion():
    tree, written = _narrow_tree(
        {
            ".github/workflows/release.yml": b"jobs:\n  ship:\n    steps:\n      - run: python scripts/publish.py\n",
            "scripts/publish.py": b"",
            "src/a.py": b"",
        },
        ["src"],
    )
    assert tree.named == [] and "scripts/publish.py" not in written


def test_a_narrow_checkout_covers_the_root_changeset_directory():
    # A changeset written at the root of a narrow checkout lay outside the
    # record: from_checkout named it untracked and did not commit it.
    tree, written = _narrow_tree(
        {
            ".changeset/config.json": b"{}",
            ".changeset/README.md": b"",
            "packages/ui/src/a.ts": b"",
            "packages/other/src/b.ts": b"",
        },
        ["packages/ui"],
    )
    assert tree.release_notes == [".changeset"] and ".changeset" in tree.sub_paths
    assert {".changeset/config.json", ".changeset/README.md"} <= written
    assert "packages/other/src/b.ts" not in written
    # No changesets config: nothing is added.
    plain, _ = _narrow_tree({"packages/ui/src/a.ts": b"", ".changeset/x.md": b""}, ["packages/ui"])
    assert plain.release_notes == []
    # A release-note directory past the bound is left out.
    big = {f".changeset/{i}.md": b"" for i in range(detect.MAX_RELEASE_NOTE_FILES + 1)}
    over, _ = _narrow_tree({".changeset/config.json": b"{}", **big, "src/a.ts": b""}, ["src"])
    assert over.release_notes == []


def test_a_root_changeset_written_in_a_narrow_checkout_is_a_change_from_checkout_commits():
    from app.services.sandbox_workspace.checkout import build_record, changes_since

    tree, _ = _narrow_tree({".changeset/config.json": b"{}", "packages/ui/src/a.ts": b""}, ["packages/ui"])
    record = build_record(
        provider="github",
        repository="acme/ui",
        ref="main",
        commit="0123abcd",
        path="co",
        sub_paths=tree.sub_paths,
        files=tree.files,
        outside={},
    )

    class _E:
        def __init__(self, data: bytes) -> None:
            import hashlib

            self.sha256 = hashlib.sha256(data).hexdigest()

    entries = {f"co/{path}": _E(data) for path, data in tree.files}
    entries["co/.changeset/brave-cats-run.md"] = _E(b"---\n'@acme/ui': patch\n---\nfix\n")
    changes = changes_since(record, entries)
    assert changes.added == (".changeset/brave-cats-run.md",) and changes.untracked == ()


def test_a_narrow_checkout_keeps_the_lab_fixture_a_frontend_test_reads():
    # frontend/src/pages/xtm-one/ctem/activity-feed.test.tsx reads
    # ../../../../../.xtm-one/lab/ctem_activity_timeline.json through path.resolve(__dirname, ...).
    _tree, written = _narrow_tree(
        {
            "frontend/package.json": b"{}",
            "frontend/src/pages/xtm-one/ctem/activity-feed.test.tsx": (
                b'readFileSync(path.resolve(__dirname, "../../../../../.xtm-one/lab/ctem_activity_timeline.json"))'
            ),
            ".xtm-one/lab/ctem_activity_timeline.json": b"{}",
            ".xtm-one/lab/lab.py": b"",
        },
        ["frontend"],
    )
    assert ".xtm-one/lab/ctem_activity_timeline.json" in written
    assert ".xtm-one/lab/lab.py" not in written


def test_a_narrow_checkout_keeps_the_files_its_tests_name_outside_it():
    # frontend/ tests read backend fixtures and the Office add-in's stylesheet;
    # the add-in's test imports the web app's constants: CI has them, a narrow
    # checkout did not, and the tests failed on ENOENT.
    tree, written = _narrow_tree(
        {
            "frontend/package.json": b"{}",
            "frontend/src/components/tile-icons.test.ts": (
                b'readFileSync(new URL("../../../backend/tests/fixtures/forms.json", import.meta.url))'
            ),
            "frontend/src/blocks/arch.test.tsx": b'resolve(process.cwd(), "../backend/tests/data/arch")',
            "frontend/tests_unit/panel.test.ts": b'new URL("../../apps/office/src/index.css", import.meta.url)',
            "frontend/src/lib/constants.ts": b"export const A = 1",
            "frontend/src/lib/csv.test.ts": b'csvFileName("../../etc"); read(`../../${lang}.json`)',
            "backend/tests/fixtures/forms.json": b"{}",
            "backend/tests/fixtures/other.json": b"{}",
            "backend/tests/data/arch/a.json": b"{}",
            "backend/tests/data/arch/b.html": b"",
            "backend/app/main.py": b"",
            "apps/office/src/index.css": b":root{}",
            "apps/office/src/lib/icons.test.ts": b'import web from "../../../../frontend/src/lib/constants?raw";',
            "apps/office/src/App.tsx": b"",
        },
        ["frontend"],
    )
    assert set(tree.referenced) == {
        "backend/tests/fixtures/forms.json",
        "backend/tests/data/arch/a.json",
        "backend/tests/data/arch/b.html",
        "apps/office/src/index.css",
    }
    assert set(tree.referenced) <= written
    assert not {"backend/tests/fixtures/other.json", "backend/app/main.py", "apps/office/src/App.tsx"} & written
    # The add-in alone takes the web app module its test imports (the suffix an import omits).
    office, office_written = _narrow_tree(
        {
            "apps/office/src/lib/icons.test.ts": b'import web from "../../../../frontend/src/lib/constants.ts?raw";',
            "apps/office/src/lib/types.test.ts": b'import { A } from "../../../../frontend/src/lib/types";',
            "frontend/src/lib/constants.ts": b"export const A = 1",
            "frontend/src/lib/types/index.ts": b"export type A = 1",
            "frontend/src/main.tsx": b"",
        },
        ["apps/office/src"],
    )
    assert office.referenced == ["frontend/src/lib/constants.ts", "frontend/src/lib/types/index.ts"]
    assert "frontend/src/main.tsx" not in office_written


def test_a_narrow_checkout_follows_a_tsconfig_extends_and_never_leaves_the_repository():
    tree, written = _narrow_tree(
        {
            "configs/ts/base.json": b"{}",
            "web/tsconfig.json": b'{"extends": "../configs/ts/base.json"}',
            "web/src/a.test.ts": b'read("../../../outside.txt"); read("./local.ts"); read("../node_modules/x/index.js")',
            "web/src/local.ts": b"",
            "web/node_modules/x/index.js": b"",
        },
        ["web"],
    )
    assert tree.referenced == ["configs/ts/base.json"] and "configs/ts/base.json" in written


def test_a_narrow_checkout_keeps_the_relay_schema_a_config_above_its_sub_path_names():
    # opencti: a checkout of opencti-front/src/utils/hooks keeps relay.config.json (a companion) but its
    # schema ./src/schema/relay.schema.graphql was left out, so the Relay ESLint plugin failed before eslint.
    _, written = _narrow_tree(
        {
            "front/relay.config.json": b'{"src": "./src", "schema": "./src/schema/relay.schema.graphql"}',
            "front/src/schema/relay.schema.graphql": b"type Query { a: Int }",
            "front/src/utils/hooks/useA.ts": b"",
            "front/src/private/b.tsx": b"",
        },
        ["front/src/utils/hooks"],
    )
    assert "front/src/schema/relay.schema.graphql" in written
    assert detect.is_referencing_config("front/relay.config.json") and not detect.is_referencing_config("a.test.ts")


def test_a_narrow_checkout_of_a_relay_project_adds_its_src_and_the_files_its_sources_import() -> None:
    # opencti #19969: node-unit-file failed on an undefined fragment until all of opencti-front/src was
    # grown by hand; src then imports ../../lang/front/*.json outside it.
    tree, written = _narrow_tree(
        {
            "front/relay.config.json": b'{"src": "./src", "schema": "./src/schema/relay.schema.graphql"}',
            "front/src/schema/relay.schema.graphql": b"type Query { a: Int }",
            "front/src/utils/hooks/useA.ts": b"import { b } from '../../private/b';",
            "front/src/private/b.tsx": b"graphql`fragment b_x on Query { a }`;",
            "front/src/components/Intl.tsx": b"import en from '../../lang/front/en.json';",
            "front/lang/front/en.json": b"{}",
            "front/lang/front/fr.json": b"{}",
            "front/builder/big.js": b"",
        },
        ["front/src/utils/hooks"],
    )
    assert tree.relay_sources == ["front/src"]
    assert "front/src/private/b.tsx" in written and "front/src/components/Intl.tsx" in written
    assert "front/lang/front/en.json" in tree.referenced and "front/lang/front/en.json" in written
    assert "front/lang/front/fr.json" not in written and "front/builder/big.js" not in written


def test_a_relay_src_past_its_bound_is_not_added_and_its_schema_still_is(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(detect, "MAX_RELAY_SOURCE_BYTES", 10)
    tree, written = _narrow_tree(
        {
            "front/relay.config.json": b'{"src": "./src", "schema": "./src/schema/relay.schema.graphql"}',
            "front/src/schema/relay.schema.graphql": b"type Query { a: Int }",
            "front/src/utils/hooks/useA.ts": b"",
            "front/src/private/b.tsx": b"",
        },
        ["front/src/utils/hooks"],
    )
    assert tree.relay_sources == []
    assert "front/src/schema/relay.schema.graphql" in tree.referenced
    assert "front/src/schema/relay.schema.graphql" in written and "front/src/private/b.tsx" not in written


def test_relay_source_dirs_reads_a_js_config_and_only_for_its_own_project() -> None:
    sizes = {"web/app/a.ts": 3, "web/app/b.ts": 3, "other/x.ts": 1}
    config = {"web/relay.config.js": b"module.exports = { src: './app', schema: './schema.graphql' };"}
    assert detect.relay_source_dirs(config, sizes, ["web/app/hooks"]) == ["web/app"]
    # A sub_path outside the config's project, or one already holding its src, adds nothing.
    assert detect.relay_source_dirs(config, sizes, ["other"]) == []
    assert detect.relay_source_dirs(config, sizes, ["web"]) == []
    # A src out of the repository or at its root is never added.
    assert detect.relay_source_dirs({"relay.config.json": b'{"src": "."}'}, sizes, ["web/app"]) == []
    assert detect.relay_source_dirs({"web/relay.config.json": b'{"src": "../.."}'}, sizes, ["web/app"]) == []


def test_a_directory_a_test_reads_whole_is_kept_only_within_its_bounds():
    big = {f"backend/tests/data/many/{i}.json": b"{}" for i in range(detect.MAX_REFERENCED_DIR_FILES + 1)}
    tree, written = _narrow_tree(
        {
            **big,
            "frontend/src/a.test.ts": b'readdirSync(new URL("../../backend/tests/data/many", import.meta.url))',
        },
        ["frontend"],
    )
    assert tree.referenced == [] and not any(path.startswith("backend/") for path in written)


def test_referenced_files_spare_an_add_paths_widening_to_the_whole_backend():
    # A frontend test that read a backend fixture used to need
    # add_paths=["backend/tests/fixtures/..."], which widened to the whole
    # backend/ (the directory backend checks run from, ~120 MB). The fixture now
    # comes with the frontend checkout and backend/ stays out.
    checks_toml = (
        '[check.backend-unit]\nargv = ["pytest"]\ncwd = "backend"\n[check.unit]\nargv = ["vitest"]\ncwd = "frontend"\n'
    )
    tree, written = _narrow_tree(
        {
            "frontend/src/x.test.ts": b'read("../../backend/tests/fixtures/f.json")',
            "backend/tests/fixtures/f.json": b"{}",
            "backend/app/main.py": b"",
        },
        ["frontend/src"],
        checks_toml,
    )
    assert tree.sub_paths == ["frontend"] and tree.referenced == ["backend/tests/fixtures/f.json"]
    assert "backend/app/main.py" not in written and "backend" not in tree.sub_paths


@pytest.mark.parametrize(
    ("files", "extra", "relock"),
    [
        (
            {"package.json": '{"packageManager": "yarn@4.5.0"}'},
            ("yarn.lock", ".yarnrc.yml"),
            ("yarn", "install", "--mode=update-lockfile"),
        ),
        (
            {"package.json": "{}"},
            ("pnpm-lock.yaml",),
            ("pnpm", "install", "--lockfile-only", "--config.strict-dep-builds=false"),
        ),
        ({"package.json": "{}"}, ("package-lock.json",), ("npm", "install", "--package-lock-only", "--ignore-scripts")),
    ],
)
def test_a_locked_node_install_gets_a_relock_check_that_rewrites_the_lock(files, extra, relock):
    found = _detect(files, extra=extra)
    spec = found.checks["node-relock"]
    assert (*spec.argv, *spec.default_args) == relock
    assert spec.install_first is False and not commands.is_install_name(spec.name)


def test_an_unlocked_node_install_gets_no_relock_check():
    assert "node-relock" not in _detect({"package.json": "{}"}).checks


def test_broken_files_detect_nothing_instead_of_raising():
    found = _detect(
        {
            ".github/workflows/bad.yml": "jobs: [unclosed",
            ".github/workflows/odd.yml": "- just\n- a list\n",
            "package.json": "{not json",
            "pyproject.toml": "not = [toml",
        }
    )
    assert set(found.checks) == {"node-install", "python-install", "actions-lint"}


def test_a_bare_cd_moves_the_rest_of_the_step_and_a_ci_mount_maps_to_the_checkout():
    wf = (
        "jobs:\n  a:\n    steps:\n      - run: |\n"
        "          npm install -g corepack\n"
        "          pip install --user tox\n"
        "          cd /opt/repo/platform/api\n"
        "          yarn check-ts\n"
        "          cd ../web\n"
        "          yarn lint\n"
        "          cd /somewhere/else\n"
        "          yarn test\n"
    )
    found = _detect({".github/workflows/a.yml": wf}, extra=("platform/api/package.json", "platform/web/package.json"))
    assert {k: v for k, v in _argv(found).items() if k != "actions-lint"} == {"platform/api:node-types": ("yarn", "check-ts"), "platform/web:node-lint": ("yarn", "lint")}


def test_release_publish_and_deploy_jobs_are_not_read():
    job = "jobs:\n  {job}:\n    steps:\n      - run: pip install build twine\n"
    assert set(_detect({".github/workflows/cd-release-on-tag.yml": job.format(job="build")}).checks) == {"actions-lint"}
    assert set(_detect({".github/workflows/ci.yml": job.format(job="publish-pypi")}).checks) == {"actions-lint"}
    assert "python-install" in _detect({".github/workflows/ci.yml": job.format(job="build")}).checks


def test_working_directories_cannot_leave_the_checkout():
    wf = "jobs:\n  a:\n    steps:\n      - run: yarn lint\n        working-directory: ../../etc\n      - run: cd /tmp && yarn test\n"
    assert set(_detect({".github/workflows/a.yml": wf}).checks) == {"actions-lint"}


def test_every_detected_check_is_a_valid_check_spec():
    found = _detect({".github/workflows/ci.yml": WORKFLOW, "pyproject.toml": "[tool.ruff]\n"}, extra=("uv.lock",))
    for spec in found.checks.values():
        assert isinstance(spec, checks.CheckSpec) and spec.argv and spec.timeout <= checks.MAX_TIMEOUT
        assert spec.report in checks.REPORT_KINDS and (spec.report == "lines" or spec.report_path)


# -- the runner reads them -----------------------------------------------------


def _workspace() -> WorkspaceSession:
    scope = WorkspaceScope(user_id=uuid.UUID(int=7), conversation_id=uuid.uuid4())
    return WorkspaceSession(scope, store=MemoryWorkspaceStore(), limits=Limits(quota_bytes=8 * 1024 * 1024, max_files=200))


async def _resolve(workspace, **kwargs):
    return await commands.resolve(
        workspace, checkout="co", allow_argv=False, max_timeout=3600, known_images={"python", "python-dev", "nodejs-dev"}, **kwargs
    )


async def test_a_checkout_without_checks_toml_runs_a_detected_check():
    workspace = _workspace()
    pkg = json.dumps({"scripts": {"test": "vitest run"}})
    await workspace.put_tree([("co/front/package.json", pkg.encode()), ("co/front/yarn.lock", b""), ("co/README.md", b"x")])
    command = await _resolve(workspace, check="front:node-unit", args=["src/a.test.ts"])
    assert command.argv == ("yarn", "test", "src/a.test.ts") and command.cwd == "co/front" and command.image == "nodejs-dev"
    with pytest.raises(checks.ChecksError, match=r"(?s)Detected from the repository's own files.*- front:node-install"):
        await _resolve(workspace, check="nope")
    with pytest.raises(checks.ChecksError, match=r"(?s)check names the check to run.*front:node-unit"):
        await _resolve(workspace)


async def test_a_services_only_checks_toml_is_named_in_the_detected_listing():
    """A checks.toml with services and no check table: the header says so, never 'no .xtm-one/checks.toml'."""
    workspace = _workspace()
    pkg = json.dumps({"scripts": {"test": "vitest run"}})
    services = b'[service.lab]\ndescription = "x"\ncommand = ["true"]\nport = 8080\n'
    await workspace.put_tree(
        [("co/front/package.json", pkg.encode()), ("co/front/yarn.lock", b""), ("co/.xtm-one/checks.toml", services)]
    )
    with pytest.raises(checks.ChecksError) as raised:
        await _resolve(workspace, check="nope")
    assert "checks.toml declares no [check.NAME] table" in str(raised.value)
    assert "(no .xtm-one/checks.toml" not in str(raised.value)


async def test_a_services_only_checks_toml_with_nothing_detected_says_why():
    workspace = _workspace()
    services = b'[service.lab]\ndescription = "x"\ncommand = ["true"]\nport = 8080\n'
    await workspace.put_tree([("co/README.md", b"x"), ("co/.xtm-one/checks.toml", services)])
    with pytest.raises(checks.ChecksError, match=r"has no \[check\.NAME\] table"):
        await _resolve(workspace, check="nope")


# -- pnpm: the release the checkout pins -----------------------------------------


def test_engines_pnpm_pins_the_install_and_every_pnpm_check():
    """A root that pins pnpm in engines.pnpm only (no packageManager) runs that release, said in the listing."""
    root = json.dumps({"engines": {"pnpm": "10.10.0"}, "scripts": {"test": "vitest run", "check:types": "tsc"}})
    member = json.dumps({"scripts": {"test": "vitest run"}, "devDependencies": {"vitest": "5"}})
    found = _detect(
        {"package.json": root, "packages/chat/package.json": member}, extra=("pnpm-lock.yaml", "pnpm-workspace.yaml")
    )
    argv = _argv(found)
    pinned = ("corepack", "pnpm@10.10.0")
    assert argv["node-install"] == (*pinned, "install", "--frozen-lockfile", "--config.strict-dep-builds=false")
    assert argv["node-unit"] == (*pinned, "run", "test")
    assert argv["node-types"] == (*pinned, "run", "check:types")
    assert argv["packages/chat:node-unit"] == (*pinned, "run", "test")
    assert argv["packages/chat:node-unit-file"] == (*pinned, "exec", "vitest", "run", "--coverage.enabled=false")
    assert "pnpm 10.10.0 pinned by package.json engines.pnpm" in found.checks["node-install"].description
    assert len(found.checks["node-install"].description) <= 200


def test_package_manager_wins_over_engines_and_keeps_its_hash():
    pkg = {
        "packageManager": "pnpm@9.15.4+sha512.abc123",
        "engines": {"pnpm": "10.10.0"},
        "scripts": {"lint": "eslint ."},
    }
    found = _detect({"package.json": json.dumps(pkg)}, extra=("pnpm-lock.yaml",))
    assert _argv(found)["node-lint"] == ("corepack", "pnpm@9.15.4+sha512.abc123", "run", "lint")
    assert "pinned by package.json packageManager" in found.checks["node-lint"].description


def test_a_repository_without_a_pnpm_pin_runs_pnpm_as_before():
    pkg = json.dumps({"scripts": {"test": "jest"}, "engines": {"node": ">=20"}})
    argv = _argv(_detect({"package.json": pkg}, extra=("pnpm-lock.yaml",)))
    assert argv["node-install"] == ("pnpm", "install", "--frozen-lockfile", "--config.strict-dep-builds=false")
    assert argv["node-unit"] == ("pnpm", "run", "test")
    # Another manager's packageManager is no pnpm pin, and npm or yarn checks are never rewritten.
    yarn = json.dumps({"packageManager": "yarn@4.5.0", "engines": {"pnpm": "10.10.0"}, "scripts": {"test": "jest"}})
    assert _argv(_detect({"package.json": yarn}, extra=("yarn.lock", ".yarnrc.yml")))["node-unit"] == ("yarn", "test")


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        ({"engines": {"pnpm": "10.10.0"}}, ("10.10.0", "package.json engines.pnpm")),
        ({"engines": {"pnpm": "=v10.10.0"}}, ("10.10.0", "package.json engines.pnpm")),
        ({"engines": {"pnpm": ">=10.10.0  <11"}}, (">=10.10.0 <11", "package.json engines.pnpm")),
        ({"engines": {"pnpm": "^10"}}, ("^10", "package.json engines.pnpm")),
        ({"engines": {"pnpm": "10.x || 11.x"}}, ("10.x || 11.x", "package.json engines.pnpm")),
        ({"engines": {"pnpm": "latest; rm -rf /"}}, ("", "")),
        ({"engines": {"pnpm": 10}}, ("", "")),
        ({"packageManager": "pnpm@10.10.0"}, ("10.10.0", "package.json packageManager")),
        ({"packageManager": "pnpm@latest"}, ("", "")),
        ({"packageManager": "npm@10.0.0", "engines": {"pnpm": "10.10.0"}}, ("", "")),
        ({}, ("", "")),
    ],
)
def test_pnpm_pin_reads_package_manager_then_engines(manifest: dict, expected: tuple[str, str]) -> None:
    assert detect.pnpm_pin("", {"package.json": json.dumps(manifest).encode()}) == expected


def test_pnpm_pin_reads_the_nearest_manifest_up_to_the_root():
    files = {
        "package.json": json.dumps({"engines": {"pnpm": "10.10.0"}}).encode(),
        "packages/a/package.json": json.dumps({"name": "a"}).encode(),
        "packages/b/package.json": json.dumps({"engines": {"pnpm": "^9"}}).encode(),
    }
    assert detect.pnpm_pin("packages/a", files) == ("10.10.0", "package.json engines.pnpm")
    assert detect.pnpm_pin("packages/b", files) == ("^9", "packages/b/package.json engines.pnpm")


def test_a_pinned_relay_package_runs_the_pinned_pnpm_in_both_steps():
    front = json.dumps(
        {
            "engines": {"pnpm": "10.10.0"},
            "scripts": {"relay": "relay-compiler", "lint": "eslint ."},
            "devDependencies": {"relay-compiler": "21"},
        }
    )
    argv = _argv(_detect({"package.json": front}, extra=("pnpm-lock.yaml",)))
    assert argv["node-lint"] == (
        "sh",
        "-c",
        'corepack pnpm@10.10.0 run relay && exec "$@"',
        "xtm-relay",
        "corepack",
        "pnpm@10.10.0",
        "run",
        "lint",
    )


def test_a_pinned_package_runs_the_pinned_pnpm_in_every_chained_step():
    """engines.pnpm with a get-* build step and a Relay script: each step of the program is pinned."""
    front = json.dumps(
        {
            "engines": {"pnpm": "10.10.0"},
            "scripts": {
                "build": "pnpm run get-manifest && tsc",
                "get-manifest": "node fetch.js",
                "relay": "relay-compiler",
                "lint": "eslint .",
            },
            "devDependencies": {"relay-compiler": "21"},
        }
    )
    argv = _argv(_detect({"package.json": front}, extra=("pnpm-lock.yaml",)))
    program = argv["node-lint"][2]
    assert program.count("corepack pnpm@10.10.0 run ") == 2
    assert " pnpm run" not in f" {program}".replace("corepack pnpm@10.10.0 run", "")
    assert "corepack pnpm@10.10.0 run get-manifest" in program
    assert "corepack pnpm@10.10.0 run relay" in program
    assert argv["node-lint"][3:] == ("xtm-relay", "corepack", "pnpm@10.10.0", "run", "lint")


def test_the_install_cache_key_differs_per_pinned_pnpm_version():
    """The key hashes the argv: two pins of one lock never answer each other's install from the cache."""
    keys = set()
    for version in ("10.10.0", "10.11.0"):
        pkg = json.dumps({"engines": {"pnpm": version}})
        keys.add(_argv(_detect({"package.json": pkg}, extra=("pnpm-lock.yaml",)))["node-install"])
    assert len(keys) == 2


async def test_checks_toml_overrides_detection():
    workspace = _workspace()
    await workspace.put_tree(
        [("co/.xtm-one/checks.toml", b'[check.mine]\nargv = ["python", "-c", "print(1)"]\n'), ("co/package.json", b'{"scripts": {"test": "x"}}')]
    )
    assert (await _resolve(workspace, check="mine")).argv == ("python", "-c", "print(1)")
    with pytest.raises(checks.ChecksError, match=r"Declared in workspace/co/\.xtm-one/checks\.toml"):
        await _resolve(workspace, check="node-unit")


async def test_a_checkout_with_nothing_detected_says_what_to_materialize():
    workspace = _workspace()
    await workspace.put_tree([("co/README.md", b"x")])
    with pytest.raises(checks.ChecksError, match=r"declares no check \(no \.xtm-one/checks\.toml\) and none was detected"):
        await _resolve(workspace, check="a")
