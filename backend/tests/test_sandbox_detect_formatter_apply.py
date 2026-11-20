"""A detected formatter check in check mode gets a ``-apply`` sibling that writes (gap sandbox-python-formatter-write-mode-missing)."""

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


def test_a_jvm_spotless_check_gets_an_apply_sibling_on_the_whole_module() -> None:
    pom = "<project><build><plugins><plugin><artifactId>spotless-maven-plugin</artifactId></plugin></plugins></build></project>"
    found = _detect({"pom.xml": pom})
    lint = next(spec for name, spec in found.checks.items() if name.endswith("jvm-lint"))
    apply = found.checks[f"{lint.name}-apply"]
    assert apply.argv[-1] == "spotless:apply"
    assert apply.affected == lint.affected and not apply.affected_by_default
