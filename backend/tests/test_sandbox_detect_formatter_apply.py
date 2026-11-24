"""A detected formatter check in check mode gets a ``-apply`` sibling that writes (gap sandbox-python-formatter-write-mode-missing)."""

import json

from app.services.sandbox_workspace import checks
from app.services.sandbox_workspace.detect import PYTHON_FORMAT_GLOBS, Detection, _write_argv, detect

WORKFLOW = """
name: ci
on: [pull_request]
jobs:
  format:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install black isort
      - run: isort --check-only --diff .
      - run: black --check .
"""


def _detect(files: dict[str, str]) -> Detection:
    return detect(list(files), {path: text.encode() for path, text in files.items()})


def test_a_python_formatter_check_gets_a_write_sibling_on_the_changed_files() -> None:
    found = _detect({".github/workflows/ci.yml": WORKFLOW, "pyproject.toml": "[project]\nname = 'x'\n"})
    isort = found.checks["python-format-apply"]
    black = found.checks["python-format-black-apply"]
    # The job installed both tools: they run in the ephemeral environment that holds them (#714).
    tools = ("uv", "run", "--no-project", "--with", "black", "--with", "isort")
    assert checks.command_argv(isort) == (*tools, "isort", ".")
    assert checks.command_argv(black) == (*tools, "black", ".")
    assert checks.command_argv(black, ["a.py"]) == (*tools, "black", "a.py")
    for spec in (isort, black):
        assert spec.affected == "files" and spec.affected_by_default
        assert spec.affected_globs == PYTHON_FORMAT_GLOBS
    # The check itself is unchanged: CI's gate stays the gate.
    assert found.checks["python-format-black"].argv == (*tools, "black", "--check")


def test_write_argv_by_tool() -> None:
    assert _write_argv(("uv", "run", "--with", "black", "black", "--check", "--diff")) == (
        ("uv", "run", "--with", "black", "black"),
        "black",
    )
    assert _write_argv(("ruff", "format", "--check")) == (("ruff", "format"), "ruff")
    assert _write_argv(("ruff", "check", "--diff")) == ((), "")
    assert _write_argv(("cargo", "fmt", "--all", "--check")) == (("cargo", "fmt", "--all"), "cargo")
    assert _write_argv(("npx", "prettier", "--check", "src")) == (("npx", "prettier", "--write", "src"), "prettier")
    assert _write_argv(("mvn", "-B", "spotless:check")) == (("mvn", "-B", "spotless:apply"), "spotless")
    assert _write_argv(("./gradlew", "--no-daemon", "spotlessCheck")) == (
        ("./gradlew", "--no-daemon", "spotlessApply"),
        "spotless",
    )
    # A formatter already in write mode, or a test, has no write sibling.
    assert _write_argv(("black", ".")) == ((), "")
    assert _write_argv(("python", "-m", "pytest", "-q")) == ((), "")


def test_a_prettier_format_check_script_gets_a_write_sibling() -> None:
    """Gap no-format-write-check: ``format:check`` runs ``prettier --check .``; its write mode is a check."""
    manifest = '{"engines": {"pnpm": "10.10.0"}, "scripts": {"format:check": "prettier --check ."}}'
    found = _detect({"package.json": manifest, "pnpm-lock.yaml": "lockfileVersion: '9.0'\n"})
    apply = found.checks["node-format-apply"]
    # The pinned pnpm runs the checkout's prettier binary: no corepack call to write by hand.
    assert apply.argv == ("corepack", "pnpm@10.10.0", "exec", "prettier", "--write")
    assert apply.default_args == (".",)
    assert checks.command_argv(apply, ["src/a.ts"])[-2:] == ("--write", "src/a.ts")
    assert apply.affected == "files" and apply.affected_by_default
    # The gate is unchanged.
    assert found.checks["node-format"].argv[-2:] == ("run", "format:check")


def test_a_format_script_that_is_not_a_plain_prettier_check_gets_no_write_sibling() -> None:
    for body in ("prettier --check . && eslint .", "biome format .", "prettier --write ."):
        manifest = json.dumps({"scripts": {"format:check": body}})
        found = _detect({"package.json": manifest, "package-lock.json": "{}"})
        assert "node-format-apply" not in found.checks, body


def test_a_yarn_prettier_format_check_runs_the_binary_through_yarn() -> None:
    manifest = '{"scripts": {"check-format": "prettier -c src"}}'
    found = _detect({"package.json": manifest, "yarn.lock": ""})
    assert found.checks["node-format-apply"].argv == ("yarn", "prettier", "--write", "src")


def test_an_npm_prettier_format_check_runs_the_binary_through_npx() -> None:
    """npm runs the checkout's prettier with the same bin runner as every other npm check."""
    manifest = '{"scripts": {"format:check": "prettier --list-different ."}}'
    found = _detect({"package.json": manifest, "package-lock.json": "{}"})
    apply = found.checks["node-format-apply"]
    assert apply.argv == ("npx", "--no-install", "prettier", "--write")
    assert apply.default_args == (".",)


def test_a_jvm_spotless_check_gets_an_apply_sibling_on_the_whole_module() -> None:
    pom = "<project><build><plugins><plugin><artifactId>spotless-maven-plugin</artifactId></plugin></plugins></build></project>"
    found = _detect({"pom.xml": pom})
    lint = next(spec for name, spec in found.checks.items() if name.endswith("jvm-lint"))
    apply = found.checks[f"{lint.name}-apply"]
    assert apply.argv[-1] == "spotless:apply"
    assert apply.affected == lint.affected and not apply.affected_by_default
