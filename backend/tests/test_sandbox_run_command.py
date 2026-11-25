"""``sandbox_run_command``: the checks declaration, the report parsers, the per-key resources, the runner.

The runner is tested against a fake container that keeps ``/tmp`` in a local
directory and runs the platform's own workspace helper with the test's
interpreter (as ``test_sandbox_workspace_remote.py`` does); the command itself
is run by the fake with ``subprocess`` from the argv the runner joined, so the
working directory, the environment, the exit code, the report file and the
read-back of what the command wrote are what is tested, not the transport.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import _per_key_value, settings
from app.services.sandbox_workspace import checks, commands, remote
from app.services.sandbox_workspace.manifest import Limits
from app.services.sandbox_workspace.session import WorkspaceSession
from app.services.sandbox_workspace.store import MemoryWorkspaceStore, WorkspaceScope

_CONTAINER_TMP = "/tmp"  # noqa: S108 - the container's own /tmp, kept in a local directory

CHECKS = """
[check.lint]
description = "a linter"
argv = ["python", "-c", "print('lint')"]
cwd = "backend"
timeout = 30
image = "python-dev"
report = "sarif"
report_path = ".xtm/lint.sarif"

[check.unit]
argv = ["python", "-c", "print('unit')"]
report = "junit"
report_path = ".xtm/unit.xml"

[check.plain]
argv = ["python", "-c", "print('plain')"]
timeout = 99999
"""


# -- the declaration -----------------------------------------------------------


def test_load_checks_reads_every_field_and_bounds_the_timeout():
    specs = checks.load_checks(CHECKS)
    assert list(specs) == ["lint", "unit", "plain"]
    lint = specs["lint"]
    assert lint.argv == ("python", "-c", "print('lint')") and lint.cwd == "backend" and lint.timeout == 30
    assert lint.image == "python-dev" and lint.report == "sarif" and lint.report_path == ".xtm/lint.sarif"
    assert specs["unit"].cwd == "" and specs["unit"].image == "" and specs["unit"].timeout == checks.DEFAULT_TIMEOUT
    assert specs["plain"].timeout == checks.MAX_TIMEOUT and specs["plain"].report == "lines"
    assert "- lint (a linter): python -c print('lint') in backend/" in checks.describe_checks(specs)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not toml [", "not valid TOML"),
        ("[other]\nx = 1\n", r"declares no \[check\.NAME\] table"),
        ("[check.a]\nargv = []\n", "needs argv"),
        ("[check.a]\nargv = ['x']\nreport = 'yaml'\n", "report must be one of"),
        ("[check.a]\nargv = ['x']\nreport = 'junit'\n", "needs report_path"),
        ("[check.a]\nargv = ['x']\ncwd = '../etc'\n", "must stay inside the checkout"),
        ("[check.a]\nargv = ['x']\ncwd = '/etc'\n", "must be a relative path"),
        ("[check.a]\nargv = ['x']\ntimeout = 0\n", "timeout must be a positive"),
        ("[check.a]\nargv = ['x']\nreport_path = 3\n", "must be a string"),
    ],
)
def test_a_bad_declaration_is_refused_with_the_reason(text, message):
    with pytest.raises(checks.ChecksError, match=message):
        checks.load_checks(text)


def test_a_calls_args_replace_the_declared_default_args():
    spec = checks.load_checks('[check.lint]\nargv = ["yarn", "eslint"]\ndefault_args = ["src/"]\n')["lint"]
    assert spec.default_args == ("src/",)
    assert checks.command_argv(spec) == ("yarn", "eslint", "src/")
    assert checks.command_argv(spec, []) == ("yarn", "eslint", "src/")
    assert checks.command_argv(spec, ["src/x.tsx"]) == ("yarn", "eslint", "src/x.tsx")
    assert checks.command_argv(spec, "src/a.ts") == ("yarn", "eslint", "src/a.ts")
    assert "[default args, replaced by args: src/]" in checks.describe_checks({"lint": spec})
    plain = checks.load_checks('[check.unit]\nargv = ["pytest", "-q"]\n')["unit"]
    assert plain.default_args == () and checks.command_argv(plain, ["t.py"]) == ("pytest", "-q", "t.py")


@pytest.mark.parametrize("value", ["'src/'", "[1]", "['']"])
def test_a_bad_default_args_is_refused(value):
    with pytest.raises(checks.ChecksError, match="default_args must be a list"):
        checks.load_checks(f"[check.a]\nargv = ['x']\ndefault_args = {value}\n")


def test_a_check_declares_its_env_like_a_service():
    spec = checks.load_checks('[check.unit]\nargv = ["pytest"]\nenv = { SECRET_KEY = "k", B = 1 }\n')["unit"]
    assert spec.env == (("B", "1"), ("SECRET_KEY", "k"))
    assert checks.load_checks('[check.unit]\nargv = ["pytest"]\n')["unit"].env == ()
    with pytest.raises(checks.ChecksError, match=r"\[check.unit\] env name 'lower'"):
        checks.load_checks('[check.unit]\nargv = ["pytest"]\nenv = { lower = "x" }\n')
    with pytest.raises(checks.ChecksError, match=r"\[check.unit\] env must be a table"):
        checks.load_checks('[check.unit]\nargv = ["pytest"]\nenv = "SECRET_KEY=x"\n')


def test_the_backend_unit_check_runs_with_the_test_secret_key_ci_exports():
    """An empty SECRET_KEY failed every JWT test of an affected run (HMAC key must not be empty)."""
    root = Path(__file__).resolve().parents[2]
    spec = checks.load_checks((root / checks.CHECKS_FILE).read_text(encoding="utf-8"))["backend-unit"]
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    key = dict(spec.env)["SECRET_KEY"]
    assert key and f'SECRET_KEY: "{key}"' in workflow


def test_the_frontend_lint_check_lints_only_the_named_file():
    root = Path(__file__).resolve().parents[2]
    spec = checks.load_checks((root / checks.CHECKS_FILE).read_text(encoding="utf-8"))["frontend-lint"]
    assert checks.command_argv(spec, ["src/x.tsx"]) == ("yarn", "oxlint", "src/x.tsx")
    assert checks.command_argv(spec) == ("yarn", "oxlint", "src/")


def test_the_lint_checks_take_the_changed_sources_with_affected():
    """affected=true hands a linter the changed sources, never the test files that mention them."""
    root = Path(__file__).resolve().parents[2]
    specs = checks.load_checks((root / checks.CHECKS_FILE).read_text(encoding="utf-8"))
    frontend, backend = specs["frontend-lint"], specs["backend-lint"]
    assert frontend.affected == "files" and {"*.ts", "*.tsx"} <= set(frontend.affected_globs)
    assert "*.json" not in frontend.affected_globs
    assert backend.affected == "files" and backend.affected_globs == ("*.py", "*.pyi")
    assert not frontend.affected_by_default and not backend.affected_by_default


def test_extra_args_are_bounded():
    assert checks.extra_args(None) == [] and checks.extra_args("tests/x.py") == ["tests/x.py"]
    assert checks.extra_args(["-k", "name"]) == ["-k", "name"]
    with pytest.raises(checks.ChecksError, match="list of strings"):
        checks.extra_args({"a": 1})
    with pytest.raises(checks.ChecksError, match="at most"):
        checks.extra_args(["x"] * (checks.MAX_ARGS + 1))
    with pytest.raises(checks.ChecksError, match="control character"):
        checks.extra_args(["a\x00b"])


def test_a_call_arg_carries_an_inline_text_past_the_declared_argv_bound():
    """An inline SQL or JSON a call passes outgrows the 512 characters of a declared argv item (#788)."""
    sql = "SELECT " + "x" * (checks.MAX_ARG_CHARS + 100)
    assert checks.extra_args(["--sql", sql]) == ["--sql", sql]
    too_long = "x" * (checks.CALL_MAX_ARG_CHARS + 1)
    with pytest.raises(checks.ChecksError) as refused:
        checks.extra_args(["--sql", too_long])
    text = str(refused.value)
    assert f"items of {checks.CALL_MAX_ARG_CHARS} characters" in text
    assert f"the longest {checks.CALL_MAX_ARG_CHARS + 1} characters" in text and "file of the checkout" in text
    # A declared check argv keeps its own bound.
    with pytest.raises(checks.ChecksError, match=f"items of {checks.MAX_ARG_CHARS} characters"):
        checks.load_checks(f'[check.a]\nargv = ["{"x" * (checks.MAX_ARG_CHARS + 1)}"]\n')


# -- the reports ---------------------------------------------------------------

JUNIT = """<testsuites><testsuite name="pytest" tests="4">
<testcase classname="tests.test_a" name="test_ok" file="tests/test_a.py" line="3"/>
<testcase classname="tests.test_a" name="test_bad" file="tests/test_a.py" line="7"><failure message="assert 1 == 2">Traceback
E assert 1 == 2</failure></testcase>
<testcase classname="tests.test_a" name="test_broken"><error>fixture 'nope' not found</error></testcase>
<testcase classname="tests.test_a" name="test_skip"><skipped/></testcase>
</testsuite></testsuites>"""

SARIF = json.dumps(
    {
        "runs": [
            {
                "results": [
                    {
                        "ruleId": "F401",
                        "level": "warning",
                        "message": {"text": "unused import"},
                        "locations": [{"physicalLocation": {"artifactLocation": {"uri": "app/x.py"}, "region": {"startLine": 3}}}],
                    },
                    {
                        "ruleId": "E999",
                        "level": "error",
                        "message": {"text": "syntax error"},
                        "locations": [{"physicalLocation": {"artifactLocation": {"uri": "file:///w/app/y.py"}, "region": {"startLine": 1}}}],
                    },
                ]
            }
        ]
    }
)


def test_junit_counts_the_tests_and_lists_the_failed_and_errored_ones():
    summary = checks.parse_junit(JUNIT)
    assert (summary.tests, summary.passed, summary.failed, summary.errors, summary.skipped) == (4, 1, 1, 1, 1)
    assert summary.headline() == "junit: 4 test(s), 1 passed, 1 failed, 1 error(s), 1 skipped"
    assert [f.render() for f in summary.findings] == [
        "tests.test_a::test_broken: error fixture 'nope' not found",
        "tests/test_a.py:8: error [tests.test_a::test_bad] assert 1 == 2",
    ]


def test_sarif_lists_every_result_errors_first():
    summary = checks.parse_sarif(SARIF)
    assert summary.headline() == "sarif: 2 finding(s)"
    assert [f.render() for f in summary.findings] == [
        "/w/app/y.py:1: error [E999] syntax error",
        "app/x.py:3: warning [F401] unused import",
    ]
    assert checks.parse_sarif("{}").problem == "no runs[] in the SARIF document"
    assert checks.parse_junit("<broken").problem.startswith("not valid JUnit XML")
    assert checks.parse_report("lines", "x") is None


def test_junit_with_junk_after_its_document_is_read_from_the_first_one():
    summary = checks.parse_junit(JUNIT + "\n<testsuites><testcase name='late'/></testsuites>\n")
    assert not summary.problem
    assert (summary.tests, summary.passed, summary.failed, summary.errors) == (4, 1, 1, 1)
    assert "junk after the first document ignored" in summary.headline()


def test_unreadable_junit_degrades_to_its_raw_text_and_the_runner_output():
    raw = "<testsuites><testcase name='a'>"
    summary = checks.parse_junit(raw)
    assert summary.problem.startswith("not valid JUnit XML")
    assert summary.failures == [("raw JUnit report", raw)]
    assert raw in summary.failures_text()
    body = checks.format_result(
        check="frontend-unit",
        argv=["yarn", "vitest"],
        cwd="frontend",
        exit_code=1,
        seconds=1.0,
        timed_out=False,
        stdout="",
        stderr="FAIL src/a.test.tsx > renders\nError: render exploded",
        run_dir="workspace/.xtm/runs/r1",
        report=summary,
        failures_saved=True,
    )
    assert "junit report: not valid JUnit XML" in body
    assert "Raw report: workspace/.xtm/runs/r1/failures.txt" in body
    assert "Error: render exploded" in body


def test_vitest_checks_print_the_console_and_errors_beside_the_junit_file():
    """The default reporter writes each test's console output and the error behind a failure to the output."""
    root = Path(__file__).resolve().parents[2]
    specs = checks.load_checks((root / ".xtm-one" / "checks.toml").read_text(encoding="utf-8"))
    for name in ("frontend-unit", "frontend-bump-unit"):
        argv = specs[name].argv
        assert "--reporter=default" in argv and "--reporter=junit" in argv, name
        assert f"--outputFile.junit={specs[name].report_path}" in argv, name


def test_findings_are_bounded_and_the_rest_counted():
    many = {"runs": [{"results": [{"ruleId": f"R{i}", "message": {"text": "m"}} for i in range(checks.MAX_FINDINGS + 7)]}]}
    summary = checks.parse_sarif(json.dumps(many))
    assert len(summary.findings) == checks.MAX_FINDINGS and summary.more == 7
    text = checks.format_result(
        check="lint", argv=["x"], cwd="backend", exit_code=1, seconds=1.0, timed_out=False, stdout="", stderr="", run_dir="workspace/.xtm/runs/a", report=summary
    )
    assert "... 7 more in the report file" in text and "(no output)" in text


def test_the_output_is_clipped_head_and_tail():
    text = checks.clip("a" * 100 + "b" * 100, head=10, tail=10)
    assert text.startswith("aaaaaaaaaa\n[... 180 characters elided") and text.endswith("bbbbbbbbbb")
    assert checks.clip("short", head=10, tail=10) == "short"


# -- per-key resources -----------------------------------------------------------


def test_sandbox_resources_read_a_bare_value_or_a_per_key_list(monkeypatch):
    assert _per_key_value("1", "python-dev", "1") == "1"
    assert _per_key_value("1,python-dev:2", "python-dev", "1") == "2"
    assert _per_key_value("1,python-dev:2", "python", "1") == "1"
    assert _per_key_value("python-dev:2Gi", "python", "128Mi") == "128Mi"
    assert _per_key_value("", "x", "128Mi") == "128Mi"
    monkeypatch.setattr(settings, "sandbox_cpu", "1,python-dev:2")
    monkeypatch.setattr(settings, "sandbox_ram", "256Mi,python-dev:2Gi")
    assert settings.sandbox_resources("python") == {"cpu": "1", "memory": "256Mi"}
    assert settings.sandbox_resources("python-dev") == {"cpu": "2", "memory": "2Gi"}


# -- the runner ------------------------------------------------------------------


class _Container:
    """A container whose ``/tmp`` is a local directory; commands run with the test's interpreter."""

    def __init__(self, root: Path, image: str) -> None:
        self.id = f"sbx-{uuid.uuid4().hex[:8]}"
        self.root = root
        self.image = image
        self.files = self
        self.commands = self
        self.killed = False
        self.last_opts = None
        self.launched: list[list[str]] = []
        self.processes: list[subprocess.Popen] = []
        self.renewals: list[float] = []
        root.mkdir(parents=True, exist_ok=True)

    def local(self, path: str) -> Path:
        assert path.startswith(_CONTAINER_TMP), path
        return self.root / path[len(_CONTAINER_TMP) :].lstrip("/")

    async def create_directories(self, entries) -> None:
        for entry in entries:
            self.local(entry.path).mkdir(parents=True, exist_ok=True)

    async def write_file(self, path: str, data: bytes) -> None:
        target = self.local(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def read_bytes(self, path: str) -> bytes:
        return self.local(path).read_bytes()

    async def run(self, command: str, opts=None):
        argv = shlex.split(command)
        if opts is not None and getattr(opts, "background", False):
            # The detached runner (remote.py ``exec``): started and left running, as execd does.
            assert argv[0] == "python" and argv[2] == "exec", argv
            self.last_opts = opts
            run_dir = self.local(argv[3])
            argv_file = run_dir / "argv.json"
            launched = json.loads(argv_file.read_text(encoding="utf-8"))
            self.launched.append(list(launched))
            if launched and launched[0] == "python":
                argv_file.write_text(json.dumps([sys.executable, *launched[1:]]), encoding="utf-8")
            env = {**os.environ, **(opts.envs or {})}
            args = [str(self.local(a)) if a.startswith(_CONTAINER_TMP) else a for a in argv[1:]]
            self.processes.append(subprocess.Popen([sys.executable, *args], env=env))  # noqa: S603
            return SimpleNamespace(exit_code=None, logs=SimpleNamespace(stdout=[], stderr=[]))
        if argv[0] == "python" and argv[1].startswith(_CONTAINER_TMP):
            # The workspace helper, as remote.py runs it.
            args = [str(self.local(a)) if a.startswith(_CONTAINER_TMP) else a for a in argv[1:]]
            done = subprocess.run([sys.executable, *args], capture_output=True, text=True, check=False)  # noqa: S603
        else:
            self.last_opts = opts
            cwd = self.local(opts.working_directory)
            env = {**os.environ, **(opts.envs or {})}
            if argv[0] == "python":
                argv[0] = sys.executable
            try:
                done = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=opts.timeout.total_seconds())  # noqa: S603
            except subprocess.TimeoutExpired:
                return SimpleNamespace(exit_code=-1, logs=SimpleNamespace(stdout=[], stderr=[]))
        return SimpleNamespace(
            exit_code=done.returncode,
            logs=SimpleNamespace(
                stdout=[SimpleNamespace(text=line) for line in done.stdout.splitlines()],
                stderr=[SimpleNamespace(text=line) for line in done.stderr.splitlines()],
            ),
        )

    async def renew(self, timeout) -> None:
        self.renewals.append(timeout.total_seconds())

    async def kill(self) -> None:
        self.killed = True
        for process in self.processes:
            if process.poll() is None:
                process.kill()

    async def close(self) -> None:
        return None


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "custom_tool_sandbox", "opensandbox")
    monkeypatch.setattr(settings, "code_interpreter_kernel_idle_seconds", 120)
    monkeypatch.setattr(settings, "opensandbox_images", {"python": "p", "python-dev": "pd"})

    async def _no_quota(*_a, **_k):
        return None

    monkeypatch.setattr("app.services.quota.enforce_combined_sandbox_quota", _no_quota)
    monkeypatch.setattr("app.services.quota.increment_sandbox_usage", _no_quota)
    containers: dict[str, _Container] = {}

    async def create(image: str, _lifetime):
        container = _Container(tmp_path / f"c{len(containers)}", image)
        containers[container.id] = container
        return container

    async def connect(sandbox_id: str):
        container = containers.get(sandbox_id)
        if container is None or container.killed:
            raise RuntimeError("no such sandbox")
        return container

    return SimpleNamespace(create=create, connect=connect, containers=containers)


def _workspace() -> WorkspaceSession:
    scope = WorkspaceScope(user_id=uuid.UUID(int=7), conversation_id=uuid.uuid4())
    return WorkspaceSession(scope, store=MemoryWorkspaceStore(), limits=Limits(quota_bytes=8 * 1024 * 1024, max_files=200))


SARIF_WRITER = (
    "import json, os, sys; os.makedirs('.xtm', exist_ok=True); "
    "json.dump({'runs': [{'results': [{'ruleId': 'F401', 'level': 'warning', 'message': {'text': 'unused'}, "
    "'locations': [{'physicalLocation': {'artifactLocation': {'uri': 'app/x.py'}, 'region': {'startLine': 2}}}]}]}]}, "
    "open('.xtm/lint.sarif', 'w')); open('out.txt', 'w').write('built'); print('one finding'); sys.exit(1)"
)


async def _resolve(workspace, **kwargs):
    return await commands.resolve(
        workspace,
        checkout="co",
        allow_argv=kwargs.pop("allow_argv", False),
        max_timeout=kwargs.pop("max_timeout", 3600),
        known_images=kwargs.pop("known_images", {"python", "python-dev"}),
        **kwargs,
    )


async def test_a_check_affected_by_default_runs_on_the_change_without_args_and_on_args_when_given():
    """A checkout has no .git: backend-format without args takes the changed lines, never the whole tree."""
    workspace = _workspace()
    toml = (
        '[check.fmt]\nargv = ["python", "-m", "fmt"]\ndefault_args = ["."]\ncwd = "backend"\n'
        'image = "python-dev"\naffected = "lines"\naffected_by_default = true\n'
    )
    await workspace.put_tree([("co/.xtm-one/checks.toml", toml.encode())])
    bare = await _resolve(workspace, check="fmt")
    assert bare.affected_by_default and bare.default_args == (".",)
    assert bare.without_defaults() == ("python", "-m", "fmt")
    named = await _resolve(workspace, check="fmt", args=["."])
    assert not named.affected_by_default and named.argv[-1] == "."
    assert named.narrow_args == ()


async def test_a_tests_check_affected_by_default_runs_the_whole_suite_only_on_whole_and_bounded():
    """frontend-unit without args runs the affected tests; --whole alone runs the suite within whole_timeout."""
    workspace = _workspace()
    toml = (
        '[check.unit]\nargv = ["yarn", "vitest", "run"]\ncwd = "frontend"\nimage = "python-dev"\n'
        "timeout = 1800\naffected_by_default = true\nwhole_timeout = 900\ninstall_first = false\n"
    )
    await workspace.put_tree([("co/.xtm-one/checks.toml", toml.encode())])
    bare = await _resolve(workspace, check="unit")
    assert bare.affected_by_default and bare.affected == "tests" and bare.timeout == 1800
    assert bare.argv == ("yarn", "vitest", "run")
    whole = await _resolve(workspace, check="unit", args=["--whole"])
    assert not whole.affected_by_default and whole.argv == ("yarn", "vitest", "run") and whole.timeout == 900
    named = await _resolve(workspace, check="unit", args=["src/a.test.ts"])
    assert not named.affected_by_default and named.argv[-1] == "src/a.test.ts" and named.timeout == 1800
    # --whole beside paths is the platform's switch, never passed on: the paths run with the check's timeout.
    both = await _resolve(workspace, check="unit", args=["--whole", "src/a.test.ts"])
    assert both.argv == ("yarn", "vitest", "run", "src/a.test.ts") and both.timeout == 1800
    # The platform ceiling still wins over whole_timeout.
    capped = await _resolve(workspace, check="unit", args=["--whole"], max_timeout=300)
    assert capped.timeout == 300


async def test_a_check_that_narrows_args_marks_bare_args_and_whole_turns_it_off():
    """backend-format-fix named a file runs on the change's lines of it; --whole runs the file as given."""
    workspace = _workspace()
    toml = (
        '[check.fix]\nargv = ["python", "-m", "fmt", "--fix"]\ncwd = "backend"\nimage = "python-dev"\n'
        'affected = "lines"\naffected_by_default = true\naffected_narrows_args = true\n'
    )
    await workspace.put_tree([("co/.xtm-one/checks.toml", toml.encode())])
    named = await _resolve(workspace, check="fix", args=["app/x.py"])
    assert named.narrow_args == ("app/x.py",) and named.argv == ("python", "-m", "fmt", "--fix", "app/x.py")
    assert not named.affected_by_default
    whole = await _resolve(workspace, check="fix", args=["--whole", "app/x.py"])
    assert whole.narrow_args == () and whole.argv == ("python", "-m", "fmt", "--fix", "app/x.py")
    with pytest.raises(checks.ChecksError, match="--whole takes the paths"):
        await _resolve(workspace, check="fix", args=["--whole"])
    bare = await _resolve(workspace, check="fix")
    assert bare.affected_by_default and bare.narrow_args == ()


async def test_a_declared_check_runs_in_its_cwd_with_the_report_parsed_and_the_outputs_saved(api):
    workspace = _workspace()
    toml = f'[check.lint]\nargv = ["python", "-c", {json.dumps(SARIF_WRITER)}]\ncwd = "backend"\nimage = "python-dev"\nreport = "sarif"\nreport_path = ".xtm/lint.sarif"\n'
    await workspace.put_tree([("co/.xtm-one/checks.toml", toml.encode()), ("co/backend/app/x.py", b"import os\n")])
    command = await _resolve(workspace, check="lint", args=["--extra"])
    assert command.argv[-1] == "--extra" and command.cwd == "co/backend" and command.image == "python-dev"
    assert command.report_path == "co/backend/.xtm/lint.sarif"

    outcome = await commands.run_command(command, workspace=workspace, sandbox_api=api)
    assert outcome.exit_code == 1 and not outcome.timed_out and outcome.container == "new"
    assert outcome.report is not None and [f.render() for f in outcome.report.findings] == ["app/x.py:2: warning [F401] unused"]
    text = outcome.text
    assert text.startswith("lint: exit 1 in ") and "sarif: 1 finding(s)" in text and "stdout:\none finding" in text
    (container,) = api.containers.values()
    assert container.image == "python-dev"
    # Started detached through the helper, in the check's directory, with its timeout and the safe environment.
    assert container.last_opts.background and container.last_opts.envs["CI"] == "true"
    assert container.launched[0][:2] == ["python", "-c"]
    assert outcome.run_id and outcome.command == command
    # What the command wrote under the workspace is saved; the report directory is not.
    entries = await workspace.entries(refresh=True)
    assert "co/backend/out.txt" in entries and not any(".xtm/lint.sarif" in p for p in entries)
    run_files = sorted(p for p in entries if p.startswith(commands.RUNS_DIR))
    assert [p.rsplit("/", 1)[1] for p in run_files] == ["run.json", "stderr.txt", "stdout.txt"]
    _entry, meta = await workspace.get_bytes(run_files[0])
    assert json.loads(meta)["exit_code"] == 1 and json.loads(meta)["check"] == "lint"
    assert json.loads(meta)["stopped"] is False
    assert "Workspace:" in text and "out.txt" in text


async def test_resolve_replaces_the_default_args_and_records_them_only_when_used():
    workspace = _workspace()
    toml = b'[check.lint]\nargv = ["yarn", "eslint"]\ndefault_args = ["src/"]\ncwd = "frontend"\n'
    await workspace.put_tree([("co/.xtm-one/checks.toml", toml)])
    whole = await _resolve(workspace, check="lint")
    assert whole.argv == ("yarn", "eslint", "src/") and whole.default_args == ("src/",)
    assert whole.without_defaults() == ("yarn", "eslint")
    one = await _resolve(workspace, check="lint", args=["src/x.tsx"])
    assert one.argv == ("yarn", "eslint", "src/x.tsx") and one.default_args == ()
    assert one.without_defaults() == one.argv


async def test_a_finished_long_check_gives_its_container_back_to_the_idle_window(api):
    # The run lifetime (timeout + margin) is for the command; once it ended the container
    # keeps only the idle window, so a 30 s run of an 1800 s check does not hold a Pool slot for 31 min.
    workspace = _workspace()
    await workspace.put_tree([("co/.xtm-one/checks.toml", b'[check.long]\nargv = ["python", "-c", "print(1)"]\ntimeout = 1800\n')])
    first = await commands.run_command(await _resolve(workspace, check="long"), workspace=workspace, sandbox_api=api)
    assert first.exit_code == 0
    (container,) = api.containers.values()
    assert container.renewals == [120.0]
    second = await commands.run_command(await _resolve(workspace, check="long"), workspace=workspace, sandbox_api=api)
    assert second.container == "reused"
    # Renewed to the run lifetime for the command, then back to the idle window after it.
    assert container.renewals == [120.0, 1800.0 + commands.HARD_TIMEOUT_MARGIN + 60, 120.0]


async def test_a_short_check_keeps_the_idle_window_without_a_second_renew(api):
    workspace = _workspace()
    await workspace.put_tree([("co/.xtm-one/checks.toml", b'[check.a]\nargv = ["python", "-c", "print(1)"]\ntimeout = 10\n')])
    outcome = await commands.run_command(await _resolve(workspace, check="a"), workspace=workspace, sandbox_api=api)
    assert outcome.exit_code == 0
    (container,) = api.containers.values()
    assert container.renewals == []


async def test_the_container_is_kept_per_image_and_found_again(api):
    workspace = _workspace()
    await workspace.put_tree([("co/.xtm-one/checks.toml", b'[check.a]\nargv = ["python", "-c", "print(1)"]\n')])
    first = await commands.run_command(await _resolve(workspace, check="a"), workspace=workspace, sandbox_api=api)
    second = await commands.run_command(await _resolve(workspace, check="a"), workspace=workspace, sandbox_api=api)
    assert (first.container, second.container) == ("new", "reused") and len(api.containers) == 1
    record = await workspace.record(create=False)
    assert set(record.kernel_state["commands"]) == {"python"}
    (container,) = api.containers.values()
    await container.kill()
    third = await commands.run_command(await _resolve(workspace, check="a"), workspace=workspace, sandbox_api=api)
    assert third.container == "restarted" and len(api.containers) == 2


async def test_a_check_past_its_timeout_is_stopped_and_the_container_discarded(api, monkeypatch):
    monkeypatch.setattr(commands, "HARD_TIMEOUT_MARGIN", 5)
    workspace = _workspace()
    await workspace.put_tree([("co/.xtm-one/checks.toml", b'[check.slow]\nargv = ["python", "-c", "import time; time.sleep(30)"]\ntimeout = 1\n')])
    outcome = await commands.run_command(await _resolve(workspace, check="slow"), workspace=workspace, sandbox_api=api)
    assert outcome.timed_out and outcome.text.startswith("slow: timed out")
    assert "exceeded its timeout (1s)" in outcome.text and "process group" in outcome.text


# -- stage timings -------------------------------------------------------


def _stage_count(stage: str, outcome: str) -> float:
    from prometheus_client import REGISTRY

    value = REGISTRY.get_sample_value(
        "xtmone_sandbox_command_stage_seconds_count", {"stage": stage, "outcome": outcome}
    )
    return value or 0.0


async def test_a_finished_command_observes_its_start_command_and_teardown(api):
    workspace = _workspace()
    await workspace.put_tree([("co/.xtm-one/checks.toml", b'[check.a]\nargv = ["python", "-c", "print(1)"]\n')])
    before = {key: _stage_count(*key) for key in (("start", "ok"), ("command", "ok"), ("teardown", "ok"))}
    span = {"local_span_type": "TOOL", "local_metadata": {}}
    from app.services.langfuse_tracing import reset_current_trace_ctx, set_current_trace_ctx

    token = set_current_trace_ctx(span)
    try:
        outcome = await commands.run_command(await _resolve(workspace, check="a"), workspace=workspace, sandbox_api=api)
    finally:
        reset_current_trace_ctx(token)
    assert outcome.exit_code == 0
    for key, count in before.items():
        assert _stage_count(*key) == count + 1, key
    figures = span["local_metadata"]
    assert figures["sandbox.container"] == "new" and figures["sandbox.command_outcome"] == "ok"
    assert {"sandbox.create_ms", "sandbox.upload_ms", "sandbox.start_ms", "sandbox.command_ms"} <= set(figures)


def test_a_command_outcome_is_labelled_ok_failed_timeout_or_lost():
    from app.services.sandbox_workspace.commands import CommandOutcome, _command_outcome

    assert _command_outcome(CommandOutcome(text="", exit_code=1), lost=False) == "failed"
    assert _command_outcome(CommandOutcome(text="", exit_code=0, timed_out=True), lost=False) == "timeout"
    assert _command_outcome(CommandOutcome(text="", exit_code=0), lost=True) == "lost"
    assert _command_outcome(CommandOutcome(text="", exit_code=0), lost=False) == "ok"


def test_observing_a_stage_costs_microseconds():
    """The program's gate: three observations per command, against seconds of command."""
    import time

    from app.services.sandbox_workspace import metrics

    started = time.perf_counter()
    for _ in range(3000):
        metrics.observe_command_stage("command", "ok", 1.0)
    per_observation_ms = (time.perf_counter() - started) * 1000.0 / 3000
    # Three per command: under 0.3 ms in all, far under 1 % of a one-second command.
    assert per_observation_ms < 0.1, per_observation_ms


# -- detached runs -------------------------------------------------------


async def test_a_command_longer_than_the_call_answers_running_and_is_waited_for_with_its_run_id(api, monkeypatch):
    """The call returns within its budget with a run_id; a poll with it returns the exit, the output and the changes."""
    workspace = _workspace()
    script = "import time; time.sleep(2); open('built.txt', 'w').write('x'); print('installed')"
    await workspace.put_tree([("co/.xtm-one/checks.toml", f'[check.install]\nargv = ["python", "-c", {json.dumps(script)}]\n'.encode())])
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 0.3)
    first = await commands.run_command(await _resolve(workspace, check="install"), workspace=workspace, sandbox_api=api)
    assert first.running and first.run_id and not first.refused
    assert first.text.startswith("install: running for ") and f"run_id {first.run_id}" in first.text
    record = await workspace.record(create=False)
    assert record.kernel_state["commands"]["python"]["run"]["run_id"] == first.run_id

    # The same check asked again while it runs (a resumed turn): that run is answered, never refused.
    same = await commands.run_command(await _resolve(workspace, check="install"), workspace=workspace, sandbox_api=api)
    assert same.running and same.run_id == first.run_id and not same.refused and not same.busy, same.text

    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    done = await commands.poll_run(first.run_id, workspace=workspace, sandbox_api=api)
    assert not done.running and done.exit_code == 0, done.text
    assert "installed" in done.text and "built.txt" in done.text
    assert done.command is not None and done.command.check == "install"
    entries = await workspace.entries(refresh=True)
    assert "co/built.txt" in entries
    assert any(p.startswith(f"{commands.RUNS_DIR}/{first.run_id}/") for p in entries)
    record = await workspace.record(create=False)
    assert "run" not in record.kernel_state["commands"]["python"]
    again = await commands.poll_run(first.run_id, workspace=workspace, sandbox_api=api)
    # Polled again after its end (a resumed turn): its saved result, never a refusal.
    assert not again.refused and again.exit_code == 0 and "ended (exit code 0" in again.text, again.text


async def test_another_command_on_a_busy_container_is_a_plain_answer_naming_the_run_id(api, monkeypatch):
    """One command per container stays; the other check is answered busy (no Error, so no friction)."""
    workspace = _workspace()
    slow = "import time; time.sleep(2); print('slow')"
    checks = f'[check.slow]\nargv = ["python", "-c", {json.dumps(slow)}]\n[check.quick]\nargv = ["python", "-c", "print(1)"]\n'
    await workspace.put_tree([("co/.xtm-one/checks.toml", checks.encode())])
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 0.3)
    first = await commands.run_command(await _resolve(workspace, check="slow"), workspace=workspace, sandbox_api=api)
    assert first.running and first.run_id
    other = await commands.run_command(await _resolve(workspace, check="quick"), workspace=workspace, sandbox_api=api)
    assert other.busy and not other.refused and other.run_id == first.run_id
    assert not other.text.startswith("Error") and f'run_id="{first.run_id}"' in other.text and "quick did not start" in other.text
    record = await workspace.record(create=False)
    assert commands.in_flight_lines(record.kernel_state) == [
        f"slow on image 'python', run_id {first.run_id}, running for {commands._since(record.kernel_state['commands']['python']['run']):.0f} s"
    ]
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    done = await commands.poll_run(first.run_id, workspace=workspace, sandbox_api=api)
    assert done.exit_code == 0, done.text
    record = await workspace.record(create=False)
    assert commands.in_flight_lines(record.kernel_state) == []


async def test_a_lock_held_by_an_exited_run_is_collected_and_the_other_check_starts(api, monkeypatch):
    """The held run's process exited but nobody polled it (its turn was cut): the next check collects it and starts."""
    workspace = _workspace()
    slow = "import time; time.sleep(0.5); print('slow-done')"
    checks = f'[check.slow]\nargv = ["python", "-c", {json.dumps(slow)}]\n[check.quick]\nargv = ["python", "-c", "print(\'quick-done\')"]\n'
    await workspace.put_tree([("co/.xtm-one/checks.toml", checks.encode())])
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 0.1)
    first = await commands.run_command(await _resolve(workspace, check="slow"), workspace=workspace, sandbox_api=api)
    assert first.running and first.run_id
    await asyncio.sleep(1.5)  # the process exits; the cut turn never polls it
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    other = await commands.run_command(await _resolve(workspace, check="quick"), workspace=workspace, sandbox_api=api)
    assert not other.busy and not other.refused and other.exit_code == 0, other.text
    assert "quick-done" in other.text and f"Run {first.run_id} had ended without being collected" in other.text
    entries = await workspace.entries(refresh=True)
    assert f"{commands.RUNS_DIR}/{first.run_id}/run.json" in entries
    assert commands.in_flight_lines(await workspace.latest_kernel_state()) == []


async def test_a_held_run_whose_result_was_saved_is_released_by_its_poll(api, monkeypatch):
    """The record still names a run whose result is saved (its clear was lost): the poll answers it and frees the image."""
    workspace = _workspace()
    await workspace.put_tree([("co/.xtm-one/checks.toml", b'[check.quick]\nargv = ["python", "-c", "print(\'hello-run\')"]\n')])
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    done = await commands.run_command(await _resolve(workspace, check="quick"), workspace=workspace, sandbox_api=api)
    assert done.exit_code == 0 and done.run_id, done.text
    stale = {"run_id": done.run_id, "started_at": time.time(), "container": "reused", "command": commands._command_record(done.command)}
    await commands._remember_run(workspace, done.command.image, stale)  # the clear that a lost worker never wrote
    again = await commands.poll_run(done.run_id, workspace=workspace, sandbox_api=api)
    assert not again.refused and not again.lost and again.exit_code == 0 and "ended (exit code 0" in again.text, again.text
    assert commands.in_flight_lines(await workspace.latest_kernel_state()) == []
    assert await commands._held_run(workspace, done.command.image) is None


async def test_a_stop_after_a_container_restart_answers_lost_never_an_error(api, monkeypatch):
    """The run's container went with a restart and the call asked to stop it: lost (no Error) and nothing reruns."""
    workspace = _workspace()
    slow = "import time; time.sleep(5); print('slow')"
    await workspace.put_tree([("co/.xtm-one/checks.toml", f'[check.slow]\nargv = ["python", "-c", {json.dumps(slow)}]\n'.encode())])
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 0.3)
    first = await commands.run_command(await _resolve(workspace, check="slow"), workspace=workspace, sandbox_api=api)
    assert first.running and first.run_id
    for container in list(api.containers.values()):
        await container.kill()  # a restart: the container and its run go together
    lost = await commands.poll_run(first.run_id, workspace=workspace, sandbox_api=api, stop=True)
    assert lost.lost and not lost.refused and lost.run_id == first.run_id
    assert not lost.text.startswith("Error") and "lost" in lost.text and "run the check again" in lost.text.lower()
    record = await workspace.record(create=False)
    assert commands.in_flight_lines(record.kernel_state) == []
    # Polled again: no saved result and nothing running, still lost, still no Error.
    again = await commands.poll_run(first.run_id, workspace=workspace, sandbox_api=api)
    assert again.lost and not again.refused and not again.text.startswith("Error")


async def test_a_poll_after_a_container_restart_reruns_the_check_once_and_answers_its_result(api, monkeypatch):
    """The run's container went with a restart: the poll starts the same check once more and answers its end."""
    workspace = _workspace()
    script = "import os, time; time.sleep(0 if os.path.exists('again') else 5); print('rerun-done')"
    await workspace.put_tree(
        [("co/.xtm-one/checks.toml", f'[check.slow]\nargv = ["python", "-c", {json.dumps(script)}]\n'.encode())]
    )
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 0.3)
    first = await commands.run_command(await _resolve(workspace, check="slow"), workspace=workspace, sandbox_api=api)
    assert first.running and first.run_id
    for container in list(api.containers.values()):
        await container.kill()  # a restart: the container and its run go together
    await workspace.put_tree([("co/again", b"1")])
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    polled = await commands.poll_run(first.run_id, workspace=workspace, sandbox_api=api)
    assert not polled.lost and not polled.refused and not polled.busy, polled.text
    assert polled.exit_code == 0 and "rerun-done" in polled.text, polled.text
    assert polled.run_id and polled.run_id != first.run_id
    assert polled.text.startswith(f"(Run {first.run_id} of slow was lost:") and f"as run {polled.run_id}" in polled.text
    assert polled.command is not None and polled.command.check == "slow"
    assert commands.in_flight_lines(await workspace.latest_kernel_state()) == []


async def test_a_later_poll_of_a_lost_run_id_is_answered_by_its_rerun(api, monkeypatch):
    """The lost run_id is the only one a resumed turn knows: it is answered by the run that replaced it (#11764)."""
    workspace = _workspace()
    script = "import os, time; time.sleep(0 if os.path.exists('again') else 5); print('rerun-done')"
    await workspace.put_tree(
        [("co/.xtm-one/checks.toml", f'[check.slow]\nargv = ["python", "-c", {json.dumps(script)}]\n'.encode())]
    )
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 0.3)
    first = await commands.run_command(await _resolve(workspace, check="slow"), workspace=workspace, sandbox_api=api)
    assert first.running and first.run_id
    for container in list(api.containers.values()):
        await container.kill()
    await workspace.put_tree([("co/again", b"1")])
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    rerun = await commands.poll_run(first.run_id, workspace=workspace, sandbox_api=api)
    assert rerun.exit_code == 0 and rerun.run_id != first.run_id, rerun.text
    assert (await workspace.latest_kernel_state())[commands.RERUNS][first.run_id] == rerun.run_id
    # The answer was lost again: the old run_id is polled once more and answers the rerun's saved result.
    again = await commands.poll_run(first.run_id, workspace=workspace, sandbox_api=api)
    assert not again.lost and not again.refused and again.exit_code == 0, again.text
    assert again.text.startswith(
        f"(Run {first.run_id} was lost with its container and started again as run {rerun.run_id}"
    )
    assert "rerun-done" in again.text


async def test_a_run_whose_container_another_call_replaced_is_rerun_once_by_its_poll(api, monkeypatch):
    """Another check met the restart first and created the new container: the lost run_id still reruns, never 'lost'."""
    workspace = _workspace()
    script = "import os, time; time.sleep(0 if os.path.exists('again') else 5); print('rerun-done')"
    checks = f'[check.slow]\nargv = ["python", "-c", {json.dumps(script)}]\n[check.quick]\nargv = ["python", "-c", "print(1)"]\n'
    await workspace.put_tree([("co/.xtm-one/checks.toml", checks.encode())])
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 0.3)
    first = await commands.run_command(await _resolve(workspace, check="slow"), workspace=workspace, sandbox_api=api)
    assert first.running and first.run_id
    for container in list(api.containers.values()):
        await container.kill()
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    other = await commands.run_command(await _resolve(workspace, check="quick"), workspace=workspace, sandbox_api=api)
    assert other.exit_code == 0, other.text
    assert first.run_id in (await workspace.latest_kernel_state())[commands.LOST_RUNS]
    await workspace.put_tree([("co/again", b"1")])
    polled = await commands.poll_run(first.run_id, workspace=workspace, sandbox_api=api)
    assert not polled.lost and polled.exit_code == 0 and "rerun-done" in polled.text, polled.text
    assert polled.text.startswith(f"(Run {first.run_id} of slow was lost: its container was replaced")
    assert first.run_id not in (await workspace.latest_kernel_state()).get(commands.LOST_RUNS, {})
    # Polled again: answered by the rerun's saved result, still never lost.
    again = await commands.poll_run(first.run_id, workspace=workspace, sandbox_api=api)
    assert not again.lost and again.exit_code == 0, again.text


async def test_a_backgrounded_install_first_runs_its_check_when_polled(api, monkeypatch):
    """The install a check needs is answered running: the poll of its run_id runs the check after it, in one answer."""
    workspace = _workspace()
    slow_install = "import time; time.sleep(1); " + _YARN_LIKE_INSTALL
    await _yarn_like_checkout(workspace, install_first=True, install=slow_install)
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 0.3)
    first = await commands.run_command(await _resolve(workspace, check="ok"), workspace=workspace, sandbox_api=api)
    assert first.running and first.run_id and "ok runs after it" in first.text, first.text
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    done = await commands.poll_run(first.run_id, workspace=workspace, sandbox_api=api)
    assert done.exit_code == 0 and done.command is not None and done.command.check == "ok", done.text
    assert "that waited for it ran next" in done.text
    (container,) = api.containers.values()
    assert (container.root / "workspace/co/count").read_text() == "1"


async def test_a_lost_install_first_reruns_the_install_then_its_check(api, monkeypatch):
    """The container went while the install a check waited for ran: the rerun installs in the new one, then checks."""
    workspace = _workspace()
    slow_install = "import os, time; time.sleep(0 if os.path.exists('fast') else 5); " + _YARN_LIKE_INSTALL
    await _yarn_like_checkout(workspace, install_first=True, install=slow_install)
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 0.3)
    first = await commands.run_command(await _resolve(workspace, check="ok"), workspace=workspace, sandbox_api=api)
    assert first.running and first.run_id, first.text
    for container in list(api.containers.values()):
        await container.kill()
    await workspace.put_tree([("co/fast", b"1")])  # the rerun's install is quick
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    polled = await commands.poll_run(first.run_id, workspace=workspace, sandbox_api=api)
    assert not polled.lost and polled.exit_code == 0, polled.text
    assert polled.command is not None and polled.command.check == "ok"
    assert f"(Run {first.run_id} of ok was lost" in polled.text and "install ran first" in polled.text


async def test_a_check_lost_after_its_install_ran_first_reinstalls_in_the_new_container(api, monkeypatch):
    """The install ran first and ended; the check itself was running when the container restarted: the rerun
    installs the new container before the check, never runs it with no dependencies."""
    workspace = _workspace()
    needs_deps = (
        "import os, sys, time; "
        "sys.exit('no dependencies') if not os.path.exists('node_modules/.yarn-state.yml') else None; "
        "time.sleep(0 if os.path.exists('again') else 5); print('check-done')"
    )
    toml = (
        f'[check.install]\nargv = ["python", "-c", {json.dumps(_YARN_LIKE_INSTALL)}]\n'
        f'[check.unit]\nargv = ["python", "-c", {json.dumps(needs_deps)}]\n'
    )
    await workspace.put_tree(
        [("co/.xtm-one/checks.toml", toml.encode()), ("co/yarn.lock", b"# lock\n"), ("co/package.json", b"{}\n")]
    )
    # The install is waited for whatever the runner's speed (it must end inside this call for the check to run
    # after it); only the check, which sleeps 5 s, is answered running. A fixed 1.5 s budget for both timed the
    # install out on a loaded runner (xtm-one main red, ci.yml run 44137451621).
    state = "workspace/co/node_modules/.yarn-state.yml"

    def budget(*_a):
        return 1.5 if any((c.root / state).exists() for c in api.containers.values()) else 30.0

    monkeypatch.setattr(commands, "wait_budget", budget)
    first = await commands.run_command(await _resolve(workspace, check="unit"), workspace=workspace, sandbox_api=api)
    assert first.running and first.run_id and "install ran first" in first.text, first.text
    held = (await workspace.latest_kernel_state())["commands"]["python"]["run"]
    assert held["command"]["check"] == "unit" and held["command"]["install"]["check"] == "install"
    for container in list(api.containers.values()):
        await container.kill()  # a restart: the new container has no node_modules
    await workspace.put_tree([("co/again", b"1")])
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    polled = await commands.poll_run(first.run_id, workspace=workspace, sandbox_api=api)
    assert not polled.lost and polled.exit_code == 0 and "check-done" in polled.text, polled.text
    assert f"(Run {first.run_id} of unit was lost" in polled.text and "install ran first" in polled.text


async def test_two_concurrent_checks_lost_to_one_restart_are_both_rerun(api, monkeypatch):
    """One restart takes two background checks (two images): each poll reruns its own, none answers lost."""
    workspace = _workspace()
    script = "import os, time; time.sleep(0 if os.path.exists('again') else 5); print('rerun-done')"
    checks = (
        f'[check.a]\nargv = ["python", "-c", {json.dumps(script)}]\n'
        f'[check.b]\nargv = ["python", "-c", {json.dumps(script)}]\nimage = "python-dev"\n'
    )
    await workspace.put_tree([("co/.xtm-one/checks.toml", checks.encode())])
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 0.3)
    first = await commands.run_command(await _resolve(workspace, check="a"), workspace=workspace, sandbox_api=api, background=True)
    second = await commands.run_command(await _resolve(workspace, check="b"), workspace=workspace, sandbox_api=api, background=True)
    assert first.running and second.running and first.run_id != second.run_id
    for container in list(api.containers.values()):
        await container.kill()
    await workspace.put_tree([("co/again", b"1")])
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    for lost in (first, second):
        polled = await commands.poll_run(lost.run_id, workspace=workspace, sandbox_api=api)
        assert not polled.lost and polled.exit_code == 0 and "rerun-done" in polled.text, polled.text
        assert polled.text.startswith(f"(Run {lost.run_id} of ")


async def test_a_rerun_that_could_not_start_is_tried_again_by_the_next_poll(api, monkeypatch):
    """The new container's create failed right after the restart: the lost run stays known and the next poll reruns it."""
    workspace = _workspace()
    script = "import os, time; time.sleep(0 if os.path.exists('again') else 5); print('rerun-done')"
    await workspace.put_tree([("co/.xtm-one/checks.toml", f'[check.slow]\nargv = ["python", "-c", {json.dumps(script)}]\n'.encode())])
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 0.3)
    first = await commands.run_command(await _resolve(workspace, check="slow"), workspace=workspace, sandbox_api=api)
    assert first.running and first.run_id
    for container in list(api.containers.values()):
        await container.kill()
    await workspace.put_tree([("co/again", b"1")])
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    create = api.create

    async def failing_create(*_a):
        raise RuntimeError("pod not ready")

    api.create = failing_create
    refused = await commands.poll_run(first.run_id, workspace=workspace, sandbox_api=api)
    assert not refused.lost and "did not start yet (attempt 1" in refused.text, refused.text
    assert first.run_id in (await workspace.latest_kernel_state())[commands.LOST_RUNS]
    api.create = create
    polled = await commands.poll_run(first.run_id, workspace=workspace, sandbox_api=api)
    assert not polled.lost and polled.exit_code == 0 and "rerun-done" in polled.text, polled.text
    assert first.run_id not in (await workspace.latest_kernel_state()).get(commands.LOST_RUNS, {})


async def test_a_second_check_of_the_same_container_queues_behind_the_running_one(api, monkeypatch):
    """Two checks of one checkout in one round: the second waits for the first within its budget, then runs."""
    workspace = _workspace()
    slow = "import time; time.sleep(1); print('slow-done')"
    checks = f'[check.slow]\nargv = ["python", "-c", {json.dumps(slow)}]\n[check.quick]\nargv = ["python", "-c", "print(\'quick-done\')"]\n'
    await workspace.put_tree([("co/.xtm-one/checks.toml", checks.encode())])
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 0.2)
    first = await commands.run_command(await _resolve(workspace, check="slow"), workspace=workspace, sandbox_api=api)
    assert first.running and first.run_id
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    second = await commands.run_command(await _resolve(workspace, check="quick"), workspace=workspace, sandbox_api=api)
    assert not second.busy and second.exit_code == 0 and "quick-done" in second.text, second.text
    assert f"(Queued behind slow, run {first.run_id}" in second.text
    assert len(api.containers) == 1
    saved = await commands.saved_outcome(workspace, first.run_id)
    assert saved is not None and saved.exit_code == 0 and "slow-done" in saved.text


async def test_two_checks_called_in_one_round_never_start_together_in_one_container(api, monkeypatch):
    """Issued in the same round (gathered), the second check reads the first's run and queues behind it.

    Both used to read "nothing runs here" before either recorded its run, so both started in the container at
    once (two installs racing in one node_modules: ENOENT, EEXIST in the link step).
    """
    workspace = _workspace()
    slow = "import time; time.sleep(1); print('slow-done')"
    checks = f'[check.slow]\nargv = ["python", "-c", {json.dumps(slow)}]\n[check.quick]\nargv = ["python", "-c", "print(\'quick-done\')"]\n'
    await workspace.put_tree([("co/.xtm-one/checks.toml", checks.encode())])
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    slow_cmd = await _resolve(workspace, check="slow")
    quick_cmd = await _resolve(workspace, check="quick")
    first, second = await asyncio.gather(
        commands.run_command(slow_cmd, workspace=workspace, sandbox_api=api),
        commands.run_command(quick_cmd, workspace=workspace, sandbox_api=api),
    )
    assert first.exit_code == 0 and "slow-done" in first.text, first.text
    assert second.exit_code == 0 and "quick-done" in second.text, second.text
    assert "(Queued behind slow, run " in second.text, second.text
    assert len(api.containers) == 1
    assert commands._CLAIMS == {}


async def test_a_container_claim_waits_its_turn_and_gives_up_past_its_timeout():
    workspace = _workspace()
    await workspace.put_tree([("co/x", b"1")])
    held = commands._Claim()
    assert await held.take(workspace, "node", 1.0)
    late = commands._Claim()
    assert not await late.take(workspace, "node", 0.1)
    other_image = commands._Claim()
    assert await other_image.take(workspace, "python", 0.1)
    other_image.release()
    next_claim = commands._Claim()
    waiting = asyncio.ensure_future(next_claim.take(workspace, "node", 5.0))
    await asyncio.sleep(0)
    held.release()
    held.release()  # idempotent
    assert await waiting
    assert len(commands._CLAIMS) == 1
    next_claim.release()
    assert commands._CLAIMS == {}


async def test_a_kernel_start_keeps_the_jobs_and_the_rerun_records():
    from app.services.sandbox_workspace import kernel

    workspace = _workspace()
    await workspace.put_tree([("co/x", b"1")])
    kept = {"jobs": {"k": {"run_id": "r"}}, commands.RERUNS: {"a": "b"}, commands.LOST_RUNS: {"c": {"run_id": "c"}}}
    await workspace.update_kernel_state(lambda state: {**state, **kept})
    await kernel._record_kernel(workspace, {"sandbox_id": "kernel"}, restarted=True)
    state = await workspace.latest_kernel_state()
    assert state["sandbox_id"] == "kernel" and all(state[name] == value for name, value in kept.items())


async def test_the_rerun_links_are_bounded():
    workspace = _workspace()
    await workspace.put_tree([("co/x", b"1")])
    for i in range(commands._MAX_RERUNS + 4):
        await commands._remember_rerun(workspace, f"lost{i}", f"new{i}")
    links = (await workspace.latest_kernel_state())[commands.RERUNS]
    assert len(links) == commands._MAX_RERUNS and "lost0" not in links and links[f"lost{commands._MAX_RERUNS + 3}"]


async def test_a_poll_whose_run_directory_went_with_a_pod_recycle_reruns_it_once(api, monkeypatch):
    """Same sandbox id, run directory gone: the poll reruns the check once (still running: its new run_id)."""
    workspace = _workspace()
    slow = "import time; time.sleep(5); print('slow')"
    await workspace.put_tree(
        [("co/.xtm-one/checks.toml", f'[check.slow]\nargv = ["python", "-c", {json.dumps(slow)}]\n'.encode())]
    )
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 0.3)
    first = await commands.run_command(await _resolve(workspace, check="slow"), workspace=workspace, sandbox_api=api)
    assert first.running and first.run_id
    for container in list(api.containers.values()):
        for process in container.processes:
            process.kill()
        shutil.rmtree(container.local(commands.remote_run_dir(first.run_id)), ignore_errors=True)
    polled = await commands.poll_run(first.run_id, workspace=workspace, sandbox_api=api)
    assert polled.running and not polled.lost and polled.run_id and polled.run_id != first.run_id, polled.text
    assert f"Run {first.run_id} of slow was lost" in polled.text and "started again once" in polled.text
    held = await commands._held_run(workspace, polled.command.image)
    assert held is not None and held["run_id"] == polled.run_id


def test_a_stored_command_record_keeps_its_install_check():
    """A run record carries the check's install, so a rerun in a new container installs first."""
    install = commands.ResolvedCommand(
        check="frontend-install", argv=("yarn", "install"), cwd="frontend", timeout=600, image="node"
    )
    check = commands.ResolvedCommand(
        check="frontend-unit",
        argv=("yarn", "test"),
        cwd="frontend",
        timeout=900,
        image="node",
        env=(("CI", "1"),),
        install=install,
    )
    record = json.loads(json.dumps(commands._command_record(check)))
    assert commands._command_from_record(record) == check
    legacy = {key: value for key, value in record.items() if key != "install"}
    assert commands._command_from_record(legacy).install is None


async def test_a_poll_of_a_run_that_ended_meanwhile_answers_its_saved_result(api, monkeypatch):
    """A resumed turn polls a run_id whose result another call already answered: read back from .xtm/runs, no Error."""
    workspace = _workspace()
    await workspace.put_tree([("co/.xtm-one/checks.toml", b'[check.quick]\nargv = ["python", "-c", "print(\'hello-run\')"]\n')])
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    done = await commands.run_command(await _resolve(workspace, check="quick"), workspace=workspace, sandbox_api=api)
    assert done.exit_code == 0 and done.run_id, done.text
    again = await commands.poll_run(done.run_id, workspace=workspace, sandbox_api=api)
    assert not again.refused and not again.lost and again.exit_code == 0, again.text
    assert "ended (exit code 0" in again.text and "hello-run" in again.text


async def test_the_replayed_check_after_a_container_restart_starts_it_anew(api, monkeypatch):
    """The same check asked again by a resumed turn whose run was lost: it runs once more, never refused or busy."""
    workspace = _workspace()
    script = "import os, time; time.sleep(0 if os.path.exists('again') else 5); print('done')"
    await workspace.put_tree([("co/.xtm-one/checks.toml", f'[check.slow]\nargv = ["python", "-c", {json.dumps(script)}]\n'.encode())])
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 0.3)
    first = await commands.run_command(await _resolve(workspace, check="slow"), workspace=workspace, sandbox_api=api)
    assert first.running and first.run_id
    # The pod was recycled under the same sandbox id: the container answers, the run and its directory are gone.
    for container in list(api.containers.values()):
        for process in container.processes:
            process.kill()
        shutil.rmtree(container.local(commands.remote_run_dir(first.run_id)), ignore_errors=True)
    await workspace.put_tree([("co/again", b"1")])
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    replay = await commands.run_command(await _resolve(workspace, check="slow"), workspace=workspace, sandbox_api=api)
    assert not replay.refused and not replay.busy and not replay.lost, replay.text
    assert replay.exit_code == 0 and replay.run_id != first.run_id and "done" in replay.text


async def test_the_same_check_asked_after_an_edit_of_its_checkout_runs_on_the_new_tree(api, monkeypatch):
    """A resumed turn edits a file, then asks the held check again: a new run on the edited tree, never the stale one."""
    workspace = _workspace()
    script = "import os, time; time.sleep(2); print('fixed' if os.path.exists('fix.txt') else 'stale')"
    await workspace.put_tree(
        [("co/.xtm-one/checks.toml", f'[check.lint]\nargv = ["python", "-c", {json.dumps(script)}]\n'.encode())]
    )
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 0.3)
    first = await commands.run_command(await _resolve(workspace, check="lint"), workspace=workspace, sandbox_api=api)
    assert first.running and first.run_id
    monkeypatch.setattr(commands.jobs, "CLOCK_SLACK", 0.0)
    await asyncio.sleep(0.05)
    await workspace.put_tree([("co/fix.txt", b"1")])
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    again = await commands.run_command(await _resolve(workspace, check="lint"), workspace=workspace, sandbox_api=api)
    assert not again.refused and not again.busy and again.exit_code == 0, again.text
    assert again.run_id != first.run_id and "fixed" in again.text, again.text


def test_the_in_flight_lines_skip_a_run_past_its_timeout_and_are_bounded():
    now = time.time()
    run = lambda rid, started, timeout=600: {  # noqa: E731
        "run": {"run_id": rid, "started_at": started, "command": {"check": "", "argv": ["pytest", "-q", "tests/a.py", "x"], "timeout": timeout}}
    }
    state = {"commands": {"python": run("r1", now - 5), "old": run("r2", now - 10_000, timeout=60), "idle": {"sandbox_id": "s"}}}
    lines = commands.in_flight_lines(state)
    assert len(lines) == 1 and lines[0].startswith("pytest -q tests/a.py on image 'python', run_id r1, running for ")
    many = {"commands": {f"img{i}": run(f"r{i}", now) for i in range(9)}}
    assert len(commands.in_flight_lines(many)) == commands._IN_FLIGHT_MAX
    assert commands.in_flight_lines(None) == [] and commands.in_flight_lines({}) == []


def test_the_resume_manifest_names_the_commands_still_running_with_their_run_ids():
    from app.services.sandbox_workspace.session import with_commands_in_flight

    state = {"commands": {"python-dev": {"run": {"run_id": "abc123", "started_at": time.time(), "command": {"check": "backend-unit", "timeout": 900}}}}}
    text = with_commands_in_flight("co/ (3 files)", state)
    assert text.startswith("co/ (3 files)\n1 sandbox command(s) started before the interruption are still running: ")
    assert "backend-unit on image 'python-dev', run_id abc123" in text and "sandbox_run_command(run_id=...)" in text
    assert with_commands_in_flight("co/ (3 files)", {"commands": {}}) == "co/ (3 files)"
    assert with_commands_in_flight(None, None) is None
    assert with_commands_in_flight(None, state).startswith("1 sandbox command(s)")


async def test_a_background_command_answers_running_at_once_and_is_followed_with_its_run_id(api):
    """background=True: the call answers running with the first lines, a poll waits for the end."""
    workspace = _workspace()
    script = "import time; print('first', flush=True); time.sleep(2); open('bg.txt', 'w').write('x'); print('done')"
    await workspace.put_tree([("co/.xtm-one/checks.toml", f'[check.bg]\nargv = ["python", "-c", {json.dumps(script)}]\n'.encode())])
    started = time.monotonic()
    first = await commands.run_command(await _resolve(workspace, check="bg"), workspace=workspace, sandbox_api=api, background=True)
    assert first.running and first.run_id and not first.refused, first.text
    assert time.monotonic() - started < 10
    done = await commands.poll_run(first.run_id, workspace=workspace, sandbox_api=api)
    assert not done.running and done.exit_code == 0 and not done.stopped, done.text
    assert "done" in done.text and "co/bg.txt" in await workspace.entries(refresh=True)


async def test_a_running_command_stopped_by_its_run_id_ends_with_its_process_group_and_the_output_so_far(api):
    """stop=True: the helper ends the group within a second; the answer says stopped, with the output."""
    workspace = _workspace()
    script = (
        "import subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "open('child.pid', 'w').write(str(child.pid)); print('started', flush=True); time.sleep(60)"
    )
    await workspace.put_tree([("co/.xtm-one/checks.toml", f'[check.long]\nargv = ["python", "-c", {json.dumps(script)}]\ntimeout = 120\n'.encode())])
    first = await commands.run_command(await _resolve(workspace, check="long"), workspace=workspace, sandbox_api=api, background=True)
    assert first.running, first.text
    # Let the child start and print.
    container = next(iter(api.containers.values()))
    for _ in range(40):
        if list(container.root.rglob("child.pid")):
            break
        await asyncio.sleep(0.1)
    started = time.monotonic()
    stopped = await commands.poll_run(first.run_id, workspace=workspace, sandbox_api=api, stop=True)
    assert time.monotonic() - started < 8, "the stop took too long"
    assert stopped.stopped and not stopped.timed_out and not stopped.running, stopped.text
    assert "Stopped on request" in stopped.text and "started" in stopped.text
    (pid_file,) = container.root.rglob("child.pid")
    child_pid = int(pid_file.read_text())
    for _ in range(20):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("the command's child outlived the stop")
    record = await workspace.record(create=False)
    assert "run" not in record.kernel_state["commands"]["python"]
    # The saved run.json says it was stopped: a reader of the workspace tells a stop from a crash.
    entries = await workspace.entries(refresh=True)
    (run_json,) = [p for p in entries if p.startswith(commands.RUNS_DIR) and p.endswith(f"{first.run_id}/run.json")]
    _entry, meta = await workspace.get_bytes(run_json)
    assert json.loads(meta)["stopped"] is True and json.loads(meta)["timed_out"] is False


async def test_a_stop_ends_a_child_that_left_the_process_group(api):
    """A child in its own session (setsid, a detached worker) is ended by the stop too, never left running (#11764)."""
    if not os.path.isdir("/proc"):
        pytest.skip("needs /proc")
    workspace = _workspace()
    script = (
        "import subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True); "
        "open('child.pid', 'w').write(str(child.pid)); print('started', flush=True); time.sleep(60)"
    )
    await workspace.put_tree(
        [
            (
                "co/.xtm-one/checks.toml",
                f'[check.long]\nargv = ["python", "-c", {json.dumps(script)}]\ntimeout = 120\n'.encode(),
            )
        ]
    )
    first = await commands.run_command(
        await _resolve(workspace, check="long"), workspace=workspace, sandbox_api=api, background=True
    )
    assert first.running, first.text
    container = next(iter(api.containers.values()))
    for _ in range(40):
        if list(container.root.rglob("child.pid")):
            break
        await asyncio.sleep(0.1)
    stopped = await commands.poll_run(first.run_id, workspace=workspace, sandbox_api=api, stop=True)
    assert stopped.stopped and not stopped.running, stopped.text
    (pid_file,) = container.root.rglob("child.pid")
    child_pid = int(pid_file.read_text())
    for _ in range(30):
        try:
            with open(f"/proc/{child_pid}/stat", encoding="utf-8") as fh:
                if fh.read().rsplit(")", 1)[1].split()[0] in ("Z", "X"):
                    break
        except OSError:
            break
        await asyncio.sleep(0.1)
    else:
        os.kill(child_pid, 9)
        pytest.fail("the detached child outlived the stop")


async def test_survivors_of_a_stop_discard_the_container_and_are_named(api, monkeypatch):
    """The helper reports a process still alive after its kill: the answer names it and the container goes."""
    workspace = _workspace()
    await workspace.put_tree([("co/.xtm-one/checks.toml", b'[check.quick]\nargv = ["python", "-c", "print(1)"]\n')])
    ended = commands._ended

    async def with_survivor(sandbox, rdir):
        found = await ended(sandbox, rdir)
        return None if found is None else {**found, "stopped": True, "survivors": [4242]}

    monkeypatch.setattr(commands, "_ended", with_survivor)
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    done = await commands.run_command(await _resolve(workspace, check="quick"), workspace=workspace, sandbox_api=api)
    assert "outlived the kill of its process tree (pids 4242)" in done.text, done.text
    (container,) = api.containers.values()
    assert container.killed
    record = await workspace.record(create=False)
    assert "python" not in (record.kernel_state.get("commands") or {})


def test_a_stopped_command_is_labelled_stopped():
    from app.services.sandbox_workspace.commands import CommandOutcome, _command_outcome

    assert _command_outcome(CommandOutcome(text="", exit_code=-9, stopped=True), lost=False) == "stopped"


def test_the_wait_budget_is_the_tool_call_deadline_less_the_finish_reserve():
    from app.tools.tool_events import bind_tool_event_sink, reset_tool_event_sink

    assert commands.wait_budget() == commands.COMMAND_WAIT_SECONDS
    binding = bind_tool_event_sink(None, tool_name="sandbox_run_command", timeout=300)
    try:
        budget = commands.wait_budget()
    finally:
        reset_tool_event_sink(binding)
    assert 300 - commands.FINISH_RESERVE_SECONDS - 1 < budget <= 300 - commands.FINISH_RESERVE_SECONDS


def test_the_affected_selection_leaves_the_command_its_share_of_the_call(monkeypatch):
    # A 300 s call: the selection reads for SELECTION_MAX_SECONDS at most.
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 255.0)
    assert commands.selection_deadline(lambda: 100.0) == 100.0 + commands.SELECTION_MAX_SECONDS
    # A short call: what leaves SELECTION_LEAVES_SECONDS to the create, the upload, the start and a first wait.
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: commands.SELECTION_LEAVES_SECONDS + 30.0)
    assert commands.selection_deadline(lambda: 100.0) == 130.0
    # A call with almost nothing left still reads for a few seconds, never a deadline in the past.
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 1.0)
    assert commands.selection_deadline(lambda: 100.0) == 105.0
    # The selection and the command fit together inside the loop's tool timeout.
    assert commands.SELECTION_MAX_SECONDS + commands.SELECTION_LEAVES_SECONDS + commands.FINISH_RESERVE_SECONDS < 300


async def test_a_failure_before_the_start_names_the_stage_the_size_and_the_time(api, monkeypatch):
    workspace = _workspace()
    await workspace.put_tree([("co/.xtm-one/checks.toml", b'[check.a]\nargv = ["python", "-c", "print(1)"]\n')])

    async def broken(_sandbox):
        raise RuntimeError("disk full")

    monkeypatch.setattr(remote, "install_tool", broken)
    outcome = await commands.run_command(await _resolve(workspace, check="a"), workspace=workspace, sandbox_api=api)
    assert outcome.refused
    assert outcome.text.startswith("Error: the workspace could not be put in the container (stage upload, workspace 0.0 MB, ")
    assert outcome.text.endswith("s): disk full")


# -- the install cache ---------------------------------------------------


def _command(check: str, cwd: str = "co", argv: tuple[str, ...] = ("npm", "ci")) -> commands.ResolvedCommand:
    return commands.ResolvedCommand(check=check, argv=argv, cwd=cwd, timeout=60, image="nodejs-dev")


def test_the_install_cache_key_is_the_image_the_directory_the_argv_and_the_lockfile_hashes():
    from app.services.sandbox_workspace.manifest import ManifestEntry

    def entries(lock_sha: str) -> dict:
        rows = {
            "co/package-lock.json": lock_sha,
            "co/package.json": "b" * 64,
            "co/src/index.js": "c" * 64,
            "co/sub/package-lock.json": "d" * 64,
        }
        return {p: ManifestEntry(path=p, size=1, sha256=s, mtime=0.0) for p, s in rows.items()}

    key = commands.install_cache_key(_command("install"), entries("a" * 64))
    assert key and len(key) == 32
    # The source files and another directory's lockfile do not count; a lockfile change does.
    changed = entries("a" * 64)
    changed["co/src/index.js"] = ManifestEntry(path="co/src/index.js", size=2, sha256="e" * 64, mtime=1.0)
    changed["co/sub/package-lock.json"] = ManifestEntry(path="co/sub/package-lock.json", size=2, sha256="f" * 64, mtime=1.0)
    assert commands.install_cache_key(_command("install"), changed) == key
    assert commands.install_cache_key(_command("install"), entries("9" * 64)) != key
    assert commands.install_cache_key(_command("install", argv=("npm", "install")), entries("a" * 64)) != key
    # The check's name is not part of it: the same argv in the same directory on the same locks is the same install.
    assert commands.install_cache_key(_command("opencti-platform/opencti-graphql:node-install"), entries("a" * 64)) == key
    assert commands.install_cache_key(_command("install", cwd="co/sub"), entries("a" * 64)) != key
    # Not an install, or nothing pinned: no key.
    assert commands.install_cache_key(_command("node-lint"), entries("a" * 64)) is None
    assert commands.install_cache_key(_command("install", cwd="co/src"), entries("a" * 64)) is None
    assert commands.is_install(_command("python-install")) and commands.is_install(_command("x:install"))
    assert not commands.is_install(_command("installer")) and not commands.is_install(_command(""))


def test_a_workspace_member_is_a_node_dir_keyed_and_probed_at_the_root_lockfile():
    """A Yarn workspace member (opencti-graphql) has no lockfile of its own: the root's pins its install."""
    from app.services.sandbox_workspace.manifest import ManifestEntry

    rows = {
        "octi/.xtm-checkout.json": "0" * 64,
        "octi/package.json": "1" * 64,
        "octi/yarn.lock": "2" * 64,
        "octi/opencti-platform/opencti-graphql/package.json": "3" * 64,
        "octi/opencti-platform/opencti-graphql/src/a.ts": "4" * 64,
        "other/package.json": "5" * 64,
    }
    entries = {p: ManifestEntry(path=p, size=1, sha256=s, mtime=0.0) for p, s in rows.items()}
    member = _command("opencti-platform/opencti-graphql:node-install", cwd="octi/opencti-platform/opencti-graphql")
    assert commands.node_lock_dir(member, entries) == "octi"
    assert commands.is_node_dir(member, entries)
    assert commands.node_lock_dir(_command("node-install", cwd="octi"), entries) == "octi"
    # A source directory with no package.json, and a project with no lockfile anywhere in its checkout: not Node.
    assert commands.node_lock_dir(_command("x", cwd="octi/opencti-platform/opencti-graphql/src"), entries) is None
    assert commands.node_lock_dir(_command("x", cwd="other"), entries) is None
    key = commands.install_cache_key(member, entries)
    assert key and len(key) == 32
    changed = dict(entries)
    changed["octi/yarn.lock"] = ManifestEntry(path="octi/yarn.lock", size=2, sha256="9" * 64, mtime=1.0)
    assert commands.install_cache_key(member, changed) != key


async def test_an_install_with_unchanged_lockfiles_is_answered_from_the_container_cache(api):
    workspace = _workspace()
    script = "import pathlib; p = pathlib.Path('count'); p.write_text(str(int(p.read_text()) + 1 if p.exists() else 1))"
    toml = f'[check.install]\nargv = ["python", "-c", {json.dumps(script)}]\n'.encode()
    await workspace.put_tree([("co/.xtm-one/checks.toml", toml), ("co/requirements.txt", b"a==1\n")])
    first = await commands.run_command(await _resolve(workspace, check="install"), workspace=workspace, sandbox_api=api)
    assert first.exit_code == 0 and "cached" not in first.text, first.text
    second = await commands.run_command(await _resolve(workspace, check="install"), workspace=workspace, sandbox_api=api)
    assert second.exit_code == 0 and "cached: the same lockfiles were installed by run " in second.text
    (container,) = api.containers.values()
    assert len(container.launched) == 1
    await workspace.put_tree([("co/requirements.txt", b"a==2\n")])
    third = await commands.run_command(await _resolve(workspace, check="install"), workspace=workspace, sandbox_api=api)
    assert third.exit_code == 0 and "cached" not in third.text and len(container.launched) == 2


# An install that leaves a Yarn state file in its directory, as ``yarn install`` does.
_YARN_LIKE_INSTALL = (
    "import pathlib; p = pathlib.Path('node_modules'); p.mkdir(exist_ok=True); (p / '.yarn-state.yml').write_text('x'); "
    "c = pathlib.Path('count'); c.write_text(str(int(c.read_text()) + 1 if c.exists() else 1))"
)


# Yarn 4 on a never-installed directory: it writes .yarn/install-state.gz, then fails with no node_modules.
_YARN4_FAILED_RUN = (
    "import os, sys; os.makedirs('.yarn', exist_ok=True); open('.yarn/install-state.gz', 'wb').write(b'x'); "
    "sys.stderr.write('Usage Error: Couldn' + chr(39) + 't find the node_modules state file - running an install might help\\n'); "
    "sys.exit(1)"
)
# Yarn's own message, whatever the files say (a node_modules left half-written, a state file in another directory).
_YARN_SAYS_NO_INSTALL = (
    "import sys; print('Usage Error: Couldn' + chr(39) + 't find the node_modules state file - running an install might help'); "
    "sys.exit(1)"
)


async def _yarn_like_checkout(workspace, *, install_first: bool = False, install: str = _YARN_LIKE_INSTALL) -> None:
    """A Yarn-like checkout; its checks opt out of the install-first step unless *install_first*."""
    first = "" if install_first else "install_first = false\n"
    toml = (
        f'[check.install]\nargv = ["python", "-c", {json.dumps(install)}]\n'
        f'[check.types]\nargv = ["python", "-c", "import sys; sys.exit(2)"]\n{first}'
        f'[check.ok]\nargv = ["python", "-c", "print(1)"]\n{first}'
        f'[check.yarn4]\nargv = ["python", "-c", {json.dumps(_YARN4_FAILED_RUN)}]\n{first}'
        f'[check.yarnsays]\nargv = ["python", "-c", {json.dumps(_YARN_SAYS_NO_INSTALL)}]\n{first}'
    )
    await workspace.put_tree([("co/.xtm-one/checks.toml", toml.encode()), ("co/yarn.lock", b"# lock\n"), ("co/package.json", b"{}\n")])


async def test_an_install_cache_hit_is_kept_while_the_state_files_it_left_are_there(api):
    workspace = _workspace()
    await _yarn_like_checkout(workspace)
    first = await commands.run_command(await _resolve(workspace, check="install"), workspace=workspace, sandbox_api=api)
    assert first.exit_code == 0 and "cached" not in first.text, first.text
    (container,) = api.containers.values()
    (record,) = (container.root / ".xtm/cache/installs").glob("*.json")
    assert json.loads(record.read_text())["state"] == ["node_modules/.yarn-state.yml"]
    second = await commands.run_command(await _resolve(workspace, check="install"), workspace=workspace, sandbox_api=api)
    assert second.exit_code == 0 and "cached: the same lockfiles were installed by run " in second.text
    assert len(container.launched) == 1


async def test_an_install_cache_hit_whose_state_file_is_gone_runs_the_install_again_and_names_it(api):
    """A container that lost node_modules (a crash, a re-materialize) is never answered 'cached'."""
    workspace = _workspace()
    await _yarn_like_checkout(workspace)
    await commands.run_command(await _resolve(workspace, check="install"), workspace=workspace, sandbox_api=api)
    (container,) = api.containers.values()
    (container.root / "workspace/co/node_modules/.yarn-state.yml").unlink()
    again = await commands.run_command(await _resolve(workspace, check="install"), workspace=workspace, sandbox_api=api)
    assert again.exit_code == 0 and "cached:" not in again.text, again.text
    assert len(container.launched) == 2
    assert "Not answered from the install cache: workspace/co/node_modules/.yarn-state.yml (left by run " in again.text
    assert "is no longer in this container, so the install ran again." in again.text
    # The install put it back: the next call is a hit again.
    third = await commands.run_command(await _resolve(workspace, check="install"), workspace=workspace, sandbox_api=api)
    assert "cached: the same lockfiles were installed by run " in third.text and len(container.launched) == 2


async def test_a_cache_record_with_no_state_files_keeps_answering_cached(api):
    """A record naming no state file (an older record, or an install that leaves nothing in its directory) keeps answering cached."""
    workspace = _workspace()
    script = "print('installed system-wide')"
    toml = f'[check.install]\nargv = ["python", "-c", {json.dumps(script)}]\n'.encode()
    await workspace.put_tree([("co/.xtm-one/checks.toml", toml), ("co/requirements.txt", b"a==1\n")])
    await commands.run_command(await _resolve(workspace, check="install"), workspace=workspace, sandbox_api=api)
    (container,) = api.containers.values()
    (record,) = (container.root / ".xtm/cache/installs").glob("*.json")
    legacy = json.loads(record.read_text())
    assert legacy["state"] == []
    legacy.pop("state")
    record.write_text(json.dumps(legacy))
    second = await commands.run_command(await _resolve(workspace, check="install"), workspace=workspace, sandbox_api=api)
    assert "cached: the same lockfiles were installed by run " in second.text and len(container.launched) == 1


async def test_a_failed_check_in_a_node_directory_with_no_install_says_to_run_the_install_first(api):
    workspace = _workspace()
    await _yarn_like_checkout(workspace)
    failed = await commands.run_command(await _resolve(workspace, check="types"), workspace=workspace, sandbox_api=api)
    assert failed.exit_code == 2
    assert "No installed dependencies in workspace/co in this container (no node_modules or Yarn PnP state): run the checkout's install check first" in failed.text
    # A passing check gets no hint, and after the install a failure gets none either.
    ok = await commands.run_command(await _resolve(workspace, check="ok"), workspace=workspace, sandbox_api=api)
    assert ok.exit_code == 0 and "No installed dependencies" not in ok.text
    await commands.run_command(await _resolve(workspace, check="install"), workspace=workspace, sandbox_api=api)
    again = await commands.run_command(await _resolve(workspace, check="types"), workspace=workspace, sandbox_api=api)
    assert again.exit_code == 2 and "No installed dependencies" not in again.text


async def test_a_failed_check_of_a_workspace_member_reads_the_install_state_at_the_workspace_root(api):
    """A member (package.json, no lockfile) keeps its install state at the root: a failure after the root install is not 'no install'."""
    workspace = _workspace()
    toml = (
        f'[check.install]\nargv = ["python", "-c", {json.dumps(_YARN_LIKE_INSTALL)}]\n'
        '[check.member-types]\nargv = ["python", "-c", "import sys; sys.exit(2)"]\ncwd = "pkg"\ninstall_first = false\n'
    )
    await workspace.put_tree(
        [
            ("co/.xtm-one/checks.toml", toml.encode()),
            ("co/yarn.lock", b"# lock\n"),
            ("co/package.json", b"{}\n"),
            ("co/pkg/package.json", b"{}\n"),
        ]
    )
    before = await commands.run_command(
        await _resolve(workspace, check="member-types"), workspace=workspace, sandbox_api=api
    )
    assert before.exit_code == 2 and "No installed dependencies in workspace/co/pkg" in before.text
    await commands.run_command(await _resolve(workspace, check="install"), workspace=workspace, sandbox_api=api)
    after = await commands.run_command(
        await _resolve(workspace, check="member-types"), workspace=workspace, sandbox_api=api
    )
    assert after.exit_code == 2 and "No installed dependencies" not in after.text


async def test_a_yarn_4_failure_that_left_its_install_state_gz_still_says_to_run_the_install_first(api):
    """Yarn 4 writes .yarn/install-state.gz on the failed run itself: that file is not installed dependencies."""
    workspace = _workspace()
    await _yarn_like_checkout(workspace)
    first = await commands.run_command(await _resolve(workspace, check="yarn4"), workspace=workspace, sandbox_api=api)
    (container,) = api.containers.values()
    assert (container.root / "workspace/co/.yarn/install-state.gz").exists()
    assert not (container.root / "workspace/co/node_modules").exists()
    assert first.exit_code == 1 and "run the checkout's install check first" in first.text, first.text
    # The second failure, with install-state.gz already there from the first, says it again.
    second = await commands.run_command(await _resolve(workspace, check="types"), workspace=workspace, sandbox_api=api)
    assert second.exit_code == 2 and "run the checkout's install check first" in second.text, second.text


async def test_yarns_no_node_modules_message_gives_the_hint_even_with_state_files_present(api):
    workspace = _workspace()
    await _yarn_like_checkout(workspace)
    await commands.run_command(await _resolve(workspace, check="install"), workspace=workspace, sandbox_api=api)
    failed = await commands.run_command(await _resolve(workspace, check="yarnsays"), workspace=workspace, sandbox_api=api)
    assert failed.exit_code == 1 and "run the checkout's install check first" in failed.text, failed.text


# -- the install-first step ---------------------------------------------------


def test_install_first_is_read_from_the_declaration_and_defaults_to_true():
    specs = checks.load_checks('[check.a]\nargv = ["x"]\n[check.b]\nargv = ["x"]\ninstall_first = false\n')
    assert specs["a"].install_first is True and specs["b"].install_first is False
    with pytest.raises(checks.ChecksError, match="install_first must be true or false"):
        checks.load_checks('[check.a]\nargv = ["x"]\ninstall_first = "no"\n')


async def test_resolve_binds_the_install_of_the_same_directory_and_image_only():
    workspace = _workspace()
    toml = (
        '[check.web-install]\nargv = ["npm", "ci"]\ncwd = "web"\n'
        '[check.web-unit]\nargv = ["npm", "test"]\ncwd = "web"\n'
        '[check.web-guard]\nargv = ["node", "guard.mjs"]\ncwd = "web"\ninstall_first = false\n'
        '[check.web-dev]\nargv = ["npm", "test"]\ncwd = "web"\nimage = "python-dev"\n'
        '[check.api-unit]\nargv = ["pytest"]\ncwd = "api"\n'
    )
    await workspace.put_tree([("co/.xtm-one/checks.toml", toml.encode())])
    unit = await _resolve(workspace, check="web-unit", args=["a.test.ts"])
    assert unit.install is not None and unit.install.check == "web-install"
    assert unit.install.argv == ("npm", "ci") and unit.install.cwd == "co/web" and unit.install.install is None
    for name in ("web-install", "web-guard", "web-dev", "api-unit"):
        assert (await _resolve(workspace, check=name)).install is None, name


async def test_a_package_check_of_a_workspace_binds_the_install_of_the_nearest_enclosing_directory():
    """A Yarn workspace installs from its root: packages/x:node-unit runs the root install first.

    Before, only an install of the same directory was bound, so a package check of a monorepo ran on a
    container with no node_modules (a fresh or restarted one) and failed with Yarn's state-file error.
    """
    workspace = _workspace()
    toml = (
        '[check.node-install]\nargv = ["yarn", "install"]\n'
        '[check."packages/a:node-unit"]\nargv = ["yarn", "test"]\ncwd = "packages/a"\n'
        '[check."packages/b:node-install"]\nargv = ["yarn", "install"]\ncwd = "packages/b"\n'
        '[check."packages/b:node-unit"]\nargv = ["yarn", "test"]\ncwd = "packages/b"\n'
        '[check."packagesx:node-unit"]\nargv = ["yarn", "test"]\ncwd = "packagesx"\n'
        '[check."other:node-unit"]\nargv = ["yarn", "test"]\ncwd = "other"\nimage = "python-dev"\n'
    )
    await workspace.put_tree([("co/.xtm-one/checks.toml", toml.encode())])
    a = await _resolve(workspace, check="packages/a:node-unit")
    assert a.install is not None and a.install.check == "node-install" and a.install.cwd == "co"
    # The install of the package's own directory wins over the root's.
    b = await _resolve(workspace, check="packages/b:node-unit")
    assert b.install is not None and b.install.check == "packages/b:node-install"
    # A sibling directory sharing a prefix is enclosed by the root, not by packages/.
    x = await _resolve(workspace, check="packagesx:node-unit")
    assert x.install is not None and x.install.check == "node-install"
    # Another image never binds an install of the default one; an install binds none.
    assert (await _resolve(workspace, check="other:node-unit")).install is None
    assert (await _resolve(workspace, check="node-install")).install is None


def test_encloses_is_strict_and_segment_aware():
    assert commands._encloses("", "packages/a") and commands._encloses("packages", "packages/a")
    assert not commands._encloses("packages", "packagesx/a") and not commands._encloses("a", "a")
    assert not commands._encloses("packages/a", "packages")


async def test_a_check_on_a_fresh_container_runs_its_install_first_then_itself_in_one_call(api):
    workspace = _workspace()
    await _yarn_like_checkout(workspace, install_first=True)
    first = await commands.run_command(await _resolve(workspace, check="ok"), workspace=workspace, sandbox_api=api)
    (container,) = api.containers.values()
    assert first.exit_code == 0, first.text
    assert first.text.startswith(
        "install ran first (no installed dependencies in workspace/co in this container): exit 0 in "
    )
    assert len(container.launched) == 2 and (container.root / "workspace/co/count").read_text() == "1"
    assert "No installed dependencies" not in first.text
    # The second call finds the install there and runs the check alone.
    second = await commands.run_command(await _resolve(workspace, check="ok"), workspace=workspace, sandbox_api=api)
    assert second.exit_code == 0 and "ran first" not in second.text, second.text
    assert len(container.launched) == 3 and (container.root / "workspace/co/count").read_text() == "1"


async def test_a_lockfile_change_runs_the_install_first_again(api):
    workspace = _workspace()
    await _yarn_like_checkout(workspace, install_first=True)
    await commands.run_command(await _resolve(workspace, check="ok"), workspace=workspace, sandbox_api=api)
    (container,) = api.containers.values()
    await workspace.put_tree([("co/yarn.lock", b"# lock 2\n")])
    again = await commands.run_command(await _resolve(workspace, check="types"), workspace=workspace, sandbox_api=api)
    assert again.exit_code == 2 and again.text.startswith("install ran first "), again.text
    assert len(container.launched) == 4 and (container.root / "workspace/co/count").read_text() == "2"


async def test_a_failed_install_first_stops_before_the_check_and_says_so(api):
    workspace = _workspace()
    await _yarn_like_checkout(workspace, install_first=True, install="import sys; print('no cache'); sys.exit(3)")
    failed = await commands.run_command(await _resolve(workspace, check="ok"), workspace=workspace, sandbox_api=api)
    (container,) = api.containers.values()
    assert failed.exit_code == 3 and len(container.launched) == 1, failed.text
    assert failed.text.startswith("ok did not run: its install check install ran first ")
    assert failed.command is not None and failed.command.check == "install"


# -- the install fallback (#10657) -------------------------------------------


def _fallback_toml(marker: Path, *, image: str = "python-dev", fallback_image: str = "python", delay: float = 0) -> str:
    """An install that fails with YN0080 the first time (the offline image), then succeeds (the marker exists).

    *delay* seconds pass before it ends, so a ``background=True`` call answers ``running`` first."""
    install = (
        f"import pathlib, sys, time; time.sleep({delay}); m = pathlib.Path({str(marker)!r}); "
        'exec(\'if not m.exists():\\n m.write_text("x"); print("YN0080: blocked"); sys.exit(1)\'); '
        "p = pathlib.Path('node_modules'); p.mkdir(exist_ok=True); (p / '.yarn-state.yml').write_text('x')"
    )
    return (
        f'[check.web-install]\nargv = ["python", "-c", {json.dumps(install)}]\nimage = "{image}"\n'
        f'fallback = {{ image = "{fallback_image}", when = "YN0080", reason = "lock newer than the image" }}\n'
        f'[check.web-unit]\nargv = ["python", "-c", "print(42)"]\nimage = "{image}"\n'
    )


def test_fallback_is_read_for_an_install_check_only():
    specs = checks.load_checks(
        '[check.x-install]\nargv = ["y"]\nimage = "a"\nfallback = { image = "b", when = "YN0080", reason = "r" }\n'
    )
    spec = specs["x-install"]
    assert (spec.fallback_image, spec.fallback_when, spec.fallback_reason) == ("b", "YN0080", "r")
    assert checks.load_checks('[check.a]\nargv = ["x"]\n')["a"].fallback_image == ""
    bad = {
        '[check.unit]\nargv = ["y"]\nfallback = { image = "b", when = "w" }\n': "install check only",
        '[check.x-install]\nargv = ["y"]\nfallback = { image = "b" }\n': "fallback needs image",
        '[check.x-install]\nargv = ["y"]\nfallback = { image = "b", when = "w", other = "z" }\n': "table of image",
        '[check.x-install]\nargv = ["y"]\nimage = "b"\nfallback = { image = "b", when = "w" }\n': "must differ",
    }
    for toml, message in bad.items():
        with pytest.raises(checks.ChecksError, match=message):
            checks.load_checks(toml)


async def test_an_install_that_fails_as_its_fallback_declares_runs_again_on_the_fallback_image(api, tmp_path):
    workspace = _workspace()
    toml = _fallback_toml(tmp_path / "marker")
    await workspace.put_tree([("co/.xtm-one/checks.toml", toml.encode()), ("co/yarn.lock", b"# lock\n"), ("co/package.json", b"{}\n")])
    command = await _resolve(workspace, check="web-install")
    assert command.fallback_image == "python" and command.fallback_configured
    outcome = await commands.run_with_fallback(command, workspace=workspace, sandbox_api=api)
    assert outcome.exit_code == 0, outcome.text
    assert outcome.text.startswith("web-install failed on python-dev (output in "), outcome.text
    assert ": lock newer than the image. Ran web-install again on python, the fallback web-install declares." in outcome.text
    assert sorted(c.image for c in api.containers.values()) == ["python", "python-dev"]


async def test_a_background_install_that_fails_as_its_fallback_declares_falls_back_when_its_poll_sees_the_end(
    api, tmp_path
):
    """background=true (or a run answered running): the poll applies the declared fallback, never a bare YN0080."""
    workspace = _workspace()
    toml = _fallback_toml(tmp_path / "marker", delay=3)
    await workspace.put_tree(
        [("co/.xtm-one/checks.toml", toml.encode()), ("co/yarn.lock", b"# lock\n"), ("co/package.json", b"{}\n")]
    )
    command = await _resolve(workspace, check="web-install")
    first = await commands.run_with_fallback(command, workspace=workspace, sandbox_api=api, background=True)
    assert first.running and first.run_id, first.text
    outcome = await commands.poll_run(first.run_id, workspace=workspace, sandbox_api=api)
    assert outcome.exit_code == 0 and not outcome.running, outcome.text
    assert "web-install failed on python-dev" in outcome.text, outcome.text
    assert "Ran web-install again on python, the fallback web-install declares." in outcome.text
    assert sorted(c.image for c in api.containers.values()) == ["python", "python-dev"]


async def test_a_check_whose_install_first_falls_back_runs_with_its_install_on_the_fallback_image(api, tmp_path):
    workspace = _workspace()
    toml = _fallback_toml(tmp_path / "marker")
    await workspace.put_tree([("co/.xtm-one/checks.toml", toml.encode()), ("co/yarn.lock", b"# lock\n"), ("co/package.json", b"{}\n")])
    command = await _resolve(workspace, check="web-unit")
    assert command.install is not None and command.fallback_image == "python"
    outcome = await commands.run_with_fallback(command, workspace=workspace, sandbox_api=api)
    assert outcome.exit_code == 0 and "42" in outcome.text, outcome.text
    assert "web-install failed on python-dev" in outcome.text and "Ran web-unit again on python" in outcome.text
    assert "web-install ran first" in outcome.text


async def test_an_install_that_passes_on_its_own_image_never_falls_back(api, tmp_path):
    """The fast path: a lock the image knows installs offline, on the declared image alone."""
    marker = tmp_path / "marker"
    marker.write_text("x")
    workspace = _workspace()
    await workspace.put_tree([("co/.xtm-one/checks.toml", _fallback_toml(marker).encode()), ("co/yarn.lock", b"# lock\n")])
    outcome = await commands.run_with_fallback(await _resolve(workspace, check="web-install"), workspace=workspace, sandbox_api=api)
    assert outcome.exit_code == 0 and "fallback" not in outcome.text, outcome.text
    assert [c.image for c in api.containers.values()] == ["python-dev"]


async def test_an_unconfigured_fallback_image_names_the_cause_in_one_line_and_does_not_rerun(api, tmp_path):
    workspace = _workspace()
    toml = _fallback_toml(tmp_path / "marker", fallback_image="toolchain-node")
    await workspace.put_tree([("co/.xtm-one/checks.toml", toml.encode()), ("co/yarn.lock", b"# lock\n")])
    command = await _resolve(workspace, check="web-install")
    assert not command.fallback_configured
    outcome = await commands.run_with_fallback(command, workspace=workspace, sandbox_api=api)
    assert outcome.exit_code == 1
    assert outcome.text.startswith(
        "web-install failed on python-dev: lock newer than the image. Its declared fallback image 'toolchain-node' "
        "is not configured on this platform"
    ), outcome.text
    assert len(api.containers) == 1


async def test_a_check_after_a_fallback_goes_to_the_fallback_image_at_once(api, tmp_path):
    """Once an install fell back, every check bound to it starts on the fallback image (where its install and build are)."""
    workspace = _workspace()
    toml = _fallback_toml(tmp_path / "marker")
    await workspace.put_tree(
        [("co/.xtm-one/checks.toml", toml.encode()), ("co/yarn.lock", b"# lock\n"), ("co/package.json", b"{}\n")]
    )
    first = await commands.run_with_fallback(
        await _resolve(workspace, check="web-unit"), workspace=workspace, sandbox_api=api
    )
    assert first.exit_code == 0 and "Ran web-unit again on python" in first.text, first.text
    state = await workspace.latest_kernel_state()
    assert [v["image"] for v in state[commands.INSTALL_FALLBACKS_KEY].values()] == ["python"]
    launched_on_dev = [len(c.launched) for c in api.containers.values() if c.image == "python-dev"]
    second = await commands.run_with_fallback(
        await _resolve(workspace, check="web-unit"), workspace=workspace, sandbox_api=api
    )
    assert second.exit_code == 0 and "42" in second.text, second.text
    assert second.text.startswith("Ran web-unit on python: web-install fell back there earlier in this workspace "), (
        second.text
    )
    assert [len(c.launched) for c in api.containers.values() if c.image == "python-dev"] == launched_on_dev


async def test_a_changed_lockfile_tries_the_declared_image_again(api, tmp_path):
    workspace = _workspace()
    toml = _fallback_toml(tmp_path / "marker")
    await workspace.put_tree(
        [("co/.xtm-one/checks.toml", toml.encode()), ("co/yarn.lock", b"# lock\n"), ("co/package.json", b"{}\n")]
    )
    await commands.run_with_fallback(
        await _resolve(workspace, check="web-install"), workspace=workspace, sandbox_api=api
    )
    await workspace.put_tree([("co/yarn.lock", b"# lock 2\n")])
    again = await commands.run_with_fallback(
        await _resolve(workspace, check="web-install"), workspace=workspace, sandbox_api=api
    )
    # The marker exists now: the declared image installs, no fallback line.
    assert again.exit_code == 0 and "fell back" not in again.text and "fallback" not in again.text, again.text


def _short(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()[:12]


async def test_a_lock_newer_than_the_image_runs_on_the_checkouts_lock_and_says_which(api, tmp_path):
    """A bump merged on main before the dev image is republished: the answer names the checkout's lock and the image."""
    workspace = _workspace()
    lock = b"# lock bumped on main\n"
    await workspace.put_tree(
        [("co/.xtm-one/checks.toml", _fallback_toml(tmp_path / "marker").encode()), ("co/yarn.lock", lock)]
    )
    outcome = await commands.run_with_fallback(
        await _resolve(workspace, check="web-unit"), workspace=workspace, sandbox_api=api
    )
    assert outcome.exit_code == 0 and "Ran web-unit again on python" in outcome.text, outcome.text
    assert outcome.text.rstrip().endswith(f"Lockfiles: co/yarn.lock {_short(lock)} (dependencies from python)."), (
        outcome.text
    )
    second = await commands.run_with_fallback(
        await _resolve(workspace, check="web-unit"), workspace=workspace, sandbox_api=api
    )
    assert second.text.rstrip().endswith(f"Lockfiles: co/yarn.lock {_short(lock)} (dependencies from python)."), (
        second.text
    )


async def test_a_lock_the_image_knows_reuses_the_cache_and_names_the_lock_once(api, tmp_path):
    """A matching lock: the install runs offline on the declared image, then answers cached, each naming the lock."""
    marker = tmp_path / "marker"
    marker.write_text("x")
    workspace = _workspace()
    lock = b"# lock\n"
    await workspace.put_tree([("co/.xtm-one/checks.toml", _fallback_toml(marker).encode()), ("co/yarn.lock", lock)])
    line = f"Lockfiles: co/yarn.lock {_short(lock)} (dependencies from python-dev)."
    first = await commands.run_with_fallback(
        await _resolve(workspace, check="web-install"), workspace=workspace, sandbox_api=api
    )
    assert first.exit_code == 0 and first.text.count(line) == 1, first.text
    cached = await commands.run_with_fallback(
        await _resolve(workspace, check="web-install"), workspace=workspace, sandbox_api=api
    )
    assert "cached" in cached.text and cached.text.count(line) == 1, cached.text
    assert [c.image for c in api.containers.values()] == ["python-dev"]


def test_a_directory_with_no_lockfile_names_no_lock():
    command = commands.ResolvedCommand(check="web-install", argv=("y",), cwd="co", timeout=60, image="python-dev")
    assert commands.lock_line(command, {}, image="python-dev") == ""


async def test_an_empty_listing_names_its_cause_not_a_json_decode():
    """The list stage ended before writing its output (a large tree): the error says so, never 'Expecting value'."""

    class _Files:
        async def write_file(self, path, data):
            return None

        async def read_bytes(self, path):
            return b""

    class _Commands:
        async def run(self, command, opts=None):
            return SimpleNamespace(
                exit_code=None, logs=SimpleNamespace(stdout=[], stderr=[SimpleNamespace(text="Killed")])
            )

    sandbox = SimpleNamespace(files=_Files(), commands=_Commands())
    with pytest.raises(RuntimeError) as raised:
        await remote._listing(sandbox, {}, 10, missing_ok=True)
    message = str(raised.value)
    assert re.match(
        r"workspace helper failed \(list\): its output file /tmp/\.xtm/list-[0-9a-f]{16}-listing\.json is 0 bytes and not JSON",
        message,
    ), message
    assert (
        "no exit code (the helper was likely ended for its time or memory)" in message and "stderr: Killed" in message
    )
    assert "listing a workspace of 0 files" in message


def _listing_sandbox(outputs: list[bytes], stderr: str = ""):
    """A sandbox double whose list runs exit 0 and leave *outputs* in turn; it counts the runs."""

    class _Files:
        async def write_file(self, path, data):
            return None

        async def read_bytes(self, path):
            return outputs.pop(0)

    class _Commands:
        runs = 0

        async def run(self, command, opts=None):
            _Commands.runs += 1
            lines = [SimpleNamespace(text=stderr)] if stderr else []
            return SimpleNamespace(exit_code=0, logs=SimpleNamespace(stdout=[], stderr=lines))

    return SimpleNamespace(files=_Files(), commands=_Commands())


async def test_an_empty_listing_on_exit_0_is_tried_once_more():
    """Exit 0 with a 0-byte listing is transient: the list stage runs a second time and its listing is used."""
    sandbox = _listing_sandbox([b"", b'{"changed": [], "removed": []}'])
    listing = await remote._listing(sandbox, {}, 10, missing_ok=True)
    assert listing == {"changed": [], "removed": []}
    assert sandbox.commands.runs == 2


def test_a_checkout_gone_with_its_record_is_never_saved_as_removed():
    """The read-back of a container that lost a whole checkout (record included) keeps it in the workspace."""
    removals = [
        "co/.xtm-checkout.json",
        "co/backend/app.py",
        "co/README.md",
        "other/.xtm-checkout.json-not-a-record",
        "loose.txt",
    ]
    kept, vanished = commands.keep_vanished_checkouts(removals)
    assert vanished == ["co"]
    assert kept == ["other/.xtm-checkout.json-not-a-record", "loose.txt"]


def test_a_file_removed_inside_a_checkout_that_keeps_its_record_stays_a_removal():
    kept, vanished = commands.keep_vanished_checkouts(["co/backend/old.py", "co/tests/test_old.py"])
    assert vanished == []
    assert kept == ["co/backend/old.py", "co/tests/test_old.py"]


async def test_an_empty_listing_twice_names_the_second_try_and_the_stderr():
    sandbox = _listing_sandbox([b"", b""], stderr="OSError: disk full")
    with pytest.raises(RuntimeError) as raised:
        await remote._listing(sandbox, {}, 10, missing_ok=True)
    message = str(raised.value)
    assert sandbox.commands.runs == 2
    assert "is 0 bytes and not JSON" in message and "exit 0" in message, message
    assert "on its second try" in message and "stderr: OSError: disk full" in message, message


async def test_a_helper_ended_without_exit_code_is_not_retried():
    sandbox = _listing_sandbox([b"", b"{}"])

    async def _ended(command, opts=None):
        return SimpleNamespace(exit_code=None, logs=SimpleNamespace(stdout=[], stderr=[]))

    sandbox.commands.run = _ended
    with pytest.raises(RuntimeError, match="no exit code"):
        await remote._listing(sandbox, {}, 10, missing_ok=True)


def test_the_repository_frontend_install_falls_back_to_toolchain_node():
    root = Path(__file__).resolve().parents[2]
    path = root / ".xtm-one" / "checks.toml"
    if not path.exists():
        pytest.skip("no .xtm-one/checks.toml in this checkout")
    spec = checks.load_checks(path.read_text())["frontend-install"]
    assert (spec.image, spec.fallback_image, spec.fallback_when) == ("nodejs-dev", "toolchain-node", "YN0080")


async def test_the_probe_answers_only_relative_paths_inside_its_root(api):
    container = await api.create("nodejs-dev", 60)
    await remote.install_tool(container)
    (container.root / "workspace/co/node_modules").mkdir(parents=True)
    (container.root / "workspace/co/node_modules/.yarn-state.yml").write_text("x")
    (container.root / "workspace/secret").write_text("x")
    root = f"{_CONTAINER_TMP}/workspace/co"
    found = await remote.probe_files(
        container, root, ["node_modules/.yarn-state.yml", "node_modules/.modules.yaml", "../secret", f"{_CONTAINER_TMP}/workspace/secret"]
    )
    assert found == {"node_modules/.yarn-state.yml"}


async def test_a_container_that_cannot_be_created_is_reported_with_its_stage(api):
    """The create stage is named (with the image key and the time spent), apart from the upload."""
    workspace = _workspace()
    await workspace.put_tree([("co/.xtm-one/checks.toml", b'[check.a]\nargv = ["python", "-c", "print(1)"]\nimage = "python-dev"\n')])

    async def refuse(_image, _lifetime):
        raise RuntimeError("Create sandbox failed: HTTP 504")

    api.create = refuse
    outcome = await commands.run_command(await _resolve(workspace, check="a"), workspace=workspace, sandbox_api=api)
    assert outcome.refused
    assert outcome.text.startswith("Error: the command container could not be created (stage create, image key 'python-dev', ")
    assert "the workspace was not sent yet" in outcome.text and outcome.text.endswith("Create sandbox failed: HTTP 504")


async def test_a_create_longer_than_the_call_answers_starting_and_the_next_call_uses_its_container(api, monkeypatch):
    """A cold image outliving the call's budget answers ``starting``; the create goes on, once (no second create)."""
    import asyncio

    workspace = _workspace()
    await workspace.put_tree([("co/.xtm-one/checks.toml", b'[check.a]\nargv = ["python", "-c", "print(1)"]\n')])
    release = asyncio.Event()
    creates: list[str] = []
    real_create = api.create

    async def slow_create(image, lifetime):
        creates.append(image)
        await release.wait()
        return await real_create(image, lifetime)

    api.create = slow_create
    monkeypatch.setattr(commands, "UPLOAD_RESERVE_SECONDS", 0)
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 0.2)
    first = await commands.run_command(await _resolve(workspace, check="a"), workspace=workspace, sandbox_api=api)
    assert first.starting and not first.refused and not first.running and first.exit_code is None
    assert first.text.startswith("a: the 'python' container is still starting (") and "nothing ran yet" in first.text
    # Called again while the create still runs: the same create is waited for, not a second one.
    second = await commands.run_command(await _resolve(workspace, check="a"), workspace=workspace, sandbox_api=api)
    assert second.starting and creates == ["python"]

    release.set()
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 30.0)
    third = await commands.run_command(await _resolve(workspace, check="a"), workspace=workspace, sandbox_api=api)
    assert third.exit_code == 0 and not third.starting, third.text
    assert creates == ["python"] and len(api.containers) == 1
    record = await workspace.record(create=False)
    assert record.kernel_state["commands"]["python"]["sandbox_id"] == next(iter(api.containers))
    assert not commands._CREATES


async def test_a_create_that_fails_after_the_call_answered_is_reported_by_the_next_call(api, monkeypatch):
    import asyncio

    workspace = _workspace()
    await workspace.put_tree([("co/.xtm-one/checks.toml", b'[check.a]\nargv = ["python", "-c", "print(1)"]\n')])
    release = asyncio.Event()

    async def failing_create(_image, _lifetime):
        await release.wait()
        raise RuntimeError("Create sandbox failed: HTTP 504")

    api.create = failing_create
    monkeypatch.setattr(commands, "UPLOAD_RESERVE_SECONDS", 0)
    monkeypatch.setattr(commands, "wait_budget", lambda *_a: 0.2)
    first = await commands.run_command(await _resolve(workspace, check="a"), workspace=workspace, sandbox_api=api)
    assert first.starting
    release.set()
    await asyncio.sleep(0)
    second = await commands.run_command(await _resolve(workspace, check="a"), workspace=workspace, sandbox_api=api)
    assert second.refused and "stage create" in second.text and second.text.endswith("Create sandbox failed: HTTP 504")
    assert not commands._CREATES


async def test_the_workspace_blobs_are_read_concurrently_before_the_upload(api, monkeypatch):
    """reconcile reads the blobs in parallel, bounded (one object-store round trip per file in sequence was minutes)."""
    import asyncio

    monkeypatch.setattr(remote, "BLOB_READ_CONCURRENCY", 4)
    workspace = _workspace()
    files = [(f"co/src/f{i}.txt", f"content {i}\n".encode()) for i in range(20)]
    await workspace.put_tree([("co/.xtm-one/checks.toml", b'[check.a]\nargv = ["python", "-c", "print(1)"]\n'), *files])
    store = workspace.store
    original = store.read_blob
    active = peak = 0

    async def slow_read(record_id, sha256):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return await original(record_id, sha256)

    monkeypatch.setattr(store, "read_blob", slow_read)
    outcome = await commands.run_command(await _resolve(workspace, check="a"), workspace=workspace, sandbox_api=api)
    assert outcome.exit_code == 0, outcome.text
    assert peak == 4
    (container,) = api.containers.values()
    for path, data in files:
        assert container.local(f"{remote.REMOTE_WORKSPACE}/{path}").read_bytes() == data


async def test_a_blob_read_that_fails_once_is_retried_before_the_upload(api, monkeypatch):
    """A store hiccup on one blob is tried again: the command runs, nothing is answered."""
    workspace = _workspace()
    await workspace.put_tree(
        [("co/.xtm-one/checks.toml", b'[check.a]\nargv = ["python", "-c", "print(1)"]\n'), ("co/src/f.txt", b"x\n")]
    )
    store = workspace.store
    original = store.read_blob
    failed: list[str] = []

    async def flaky_read(record_id, sha256):
        if sha256 not in failed:
            failed.append(sha256)
            raise ConnectionError("store reset")
        return await original(record_id, sha256)

    command = await _resolve(workspace, check="a")
    monkeypatch.setattr(store, "read_blob", flaky_read)
    outcome = await commands.run_command(command, workspace=workspace, sandbox_api=api)
    assert outcome.exit_code == 0, outcome.text
    assert failed


async def test_a_failed_upload_names_the_file_its_cause_and_the_largest_directories(api, monkeypatch):
    """Not 'unhandled errors in a TaskGroup (1 sub-exception)': the file, the store's reason, what to prune."""
    workspace = _workspace()
    await workspace.put_tree(
        [
            ("co/.xtm-one/checks.toml", b'[check.a]\nargv = ["python", "-c", "print(1)"]\n'),
            ("co/src/big.txt", b"b" * 3000),
            ("other/x.txt", b"o" * 1000),
        ]
    )
    store = workspace.store
    original = store.read_blob
    big = (await workspace.entries(refresh=True))["co/src/big.txt"].sha256
    attempts = 0

    async def broken_read(record_id, sha256):
        nonlocal attempts
        if sha256 == big:
            attempts += 1
            raise TimeoutError()
        return await original(record_id, sha256)

    monkeypatch.setattr(store, "read_blob", broken_read)
    outcome = await commands.run_command(await _resolve(workspace, check="a"), workspace=workspace, sandbox_api=api)
    assert outcome.refused
    assert attempts == remote.BLOB_READ_ATTEMPTS
    assert "TaskGroup" not in outcome.text
    assert "co/src/big.txt could not be read from the workspace store (TimeoutError, after 2 attempts)" in outcome.text
    assert "Largest directories of the workspace: co/ " in outcome.text
    assert outcome.text.index("co/ ") < outcome.text.index("other/ ")


async def test_a_check_is_staged_with_its_own_checkout_not_the_others(api):
    """Three checkouts in the workspace: the container gets the check's one and the loose files, never the other two."""
    workspace = _workspace()
    await workspace.put_tree(
        [
            ("co/.xtm-checkout.json", b"{}"),
            ("co/.xtm-one/checks.toml", b'[check.a]\nargv = ["python", "-c", "print(1)"]\n'),
            ("co/src/f.txt", b"mine"),
            ("big1/.xtm-checkout.json", b"{}"),
            ("big1/blob.bin", b"1" * 200_000),
            ("big2/.xtm-checkout.json", b"{}"),
            ("big2/blob.bin", b"2" * 200_000),
            ("work/plan.md", b"plan"),
        ]
    )
    outcome = await commands.run_command(await _resolve(workspace, check="a"), workspace=workspace, sandbox_api=api)
    assert outcome.exit_code == 0, outcome.text
    (container,) = api.containers.values()
    ws = container.local(remote.REMOTE_WORKSPACE)
    assert (ws / "co/src/f.txt").read_bytes() == b"mine"
    assert (ws / "work/plan.md").exists()
    assert not (ws / "big1").exists() and not (ws / "big2").exists()
    # The read-back did not take the checkouts it never sent as removed.
    entries = await workspace.entries(refresh=True)
    assert "big1/blob.bin" in entries and "big2/blob.bin" in entries


def test_staged_entries_keeps_the_nearest_checkout_and_the_loose_files():
    entries = {
        path: SimpleNamespace(size=1)
        for path in (
            "a/.xtm-checkout.json",
            "a/x",
            "b/.xtm-checkout.json",
            "b/y",
            "loose.md",
            "a/inner/.xtm-checkout.json",
            "a/inner/z",
        )
    }
    # A nested checkout keeps the checkout around it (its parent tree may be read), never a sibling one.
    kept, root = commands.staged_entries(entries, "a/inner/pkg")
    assert root == "a/inner" and "b/y" not in kept and {"a/inner/z", "a/x", "loose.md"} <= set(kept)
    kept, root = commands.staged_entries(entries, "a")
    assert root == "a" and "b/y" not in kept and "a/inner/z" not in kept and "a/x" in kept
    kept, root = commands.staged_entries(entries, "")
    assert root == "" and kept is entries
    # A checkout a manifest's relative path names is staged with the command's own (#1382).
    kept, root = commands.staged_entries(entries, "a", ["b"])
    assert root == "a" and {"a/x", "b/y", "loose.md"} <= set(kept)


def test_linked_checkouts_reads_a_relative_path_dependency_into_a_sibling_checkout() -> None:
    # OpenAEV injectors: nmap/pyproject.toml names pyoaev @ ../../client-python (gap sandbox-poetry-path-dep-outside-checkout).
    manifests = {
        "inj/nmap/pyproject.toml": '[project]\ndependencies = ["injector_common @ ../injector_common"]\n'
        'dev = ["pyoaev @ ../../client-python"]\n'
    }
    roots = ["client-python", "inj", "other"]
    assert commands.linked_checkouts(manifests, "inj", roots) == ["client-python"]
    # A path inside the own checkout, or above the workspace, names no other checkout.
    assert commands.linked_checkouts({"inj/a/package.json": '"x": "file:../b", "y": "../../../up"'}, "inj", roots) == []


def test_link_manifest_paths_walks_up_to_the_checkout_root() -> None:
    entries = dict.fromkeys(("inj/pyproject.toml", "inj/nmap/pyproject.toml", "pyproject.toml", "inj/nmap/x.py"))
    assert commands.link_manifest_paths(entries, "inj/nmap", "inj") == ["inj/nmap/pyproject.toml", "inj/pyproject.toml"]


async def test_an_undeclared_check_or_argv_is_refused_with_the_declared_list(api):
    workspace = _workspace()
    with pytest.raises(checks.ChecksError, match="declares no check"):
        await _resolve(workspace, check="a")
    await workspace.put_tree([("co/.xtm-one/checks.toml", b'[check.a]\nargv = ["python", "-c", "print(1)"]\ncwd = "x"\n[check.b]\nargv = ["echo"]\nimage = "nodejs-dev"\n')])
    with pytest.raises(checks.ChecksError, match=r"'zzz' is not a check of workspace/co/\. Declared in workspace/co/\.xtm-one/checks\.toml:\n- a: python -c print\(1\) in x/\n- b: echo"):
        await _resolve(workspace, check="zzz")
    with pytest.raises(checks.ChecksError, match="image key 'nodejs-dev', which OPENSANDBOX_IMAGES does not configure"):
        await _resolve(workspace, check="b")
    with pytest.raises(checks.ChecksError, match="not allowed on this platform"):
        await _resolve(workspace, argv=["python", "-c", "print(1)"])
    with pytest.raises(checks.ChecksError, match="not both"):
        await _resolve(workspace, check="a", argv=["x"], allow_argv=True)
    command = await _resolve(workspace, argv=["python", "-c", "print('free')"], cwd="x", allow_argv=True, max_timeout=20)
    assert command.check == "" and command.cwd == "co/x" and command.timeout == 20 and command.label == "argv"
    assert (await _resolve(workspace, check="a", max_timeout=7)).timeout == 7


async def test_the_check_listing_is_an_answer_with_image_report_and_services():
    workspace = _workspace()
    with pytest.raises(checks.ChecksError, match="declares no check"):
        await commands.list_checks(workspace, checkout="co", known_images={"python"})
    toml = CHECKS + '\n[service.db]\nargv = ["postgres"]\nport = 5432\n'
    await workspace.put_tree([("co/.xtm-one/checks.toml", toml.encode())])
    listing = await commands.list_checks(workspace, checkout="co", known_images={"python"})
    assert listing.startswith("Declared in workspace/co/.xtm-one/checks.toml:"), listing
    assert "- lint (a linter): python -c print('lint') in backend/ [image python-dev] [report sarif: .xtm/lint.sarif]" in listing
    assert "- unit: python -c print('unit') [report junit: .xtm/unit.xml]" in listing
    assert "- plain: python -c print('plain')\n" in listing
    assert "Services it declares (sandbox_start_service):\n- db: port 5432" in listing
    assert "Error" not in listing


async def test_a_large_lab_is_named_not_described_in_the_check_listing():
    workspace = _workspace()
    services = "".join(
        f'\n[service.lab-{i}]\nargv = ["serve"]\nport = {3100 + i}\ndescription = "{"a seeded lab state " * 6}{i}"\n'
        for i in range(40)
    )
    await workspace.put_tree([("co/.xtm-one/checks.toml", (CHECKS + services).encode())])
    listing = await commands.list_checks(workspace, checkout="co", known_images={"python"})
    assert "Services it declares (sandbox_start_service):\n40 services: lab-0, lab-1," in listing, listing
    assert "lab-39\n(sandbox_start_service without a service lists them" in listing
    assert "a seeded lab state" not in listing and "port 3100" not in listing
    assert len(listing) < checks.LISTING_MAX_CHARS + checks.SERVICES_LISTING_MAX_CHARS
    # The services listing itself keeps every description.
    full = checks.describe_services(checks.load_services(CHECKS + services))
    assert "- lab-39 (a seeded lab state" in full and "port 3139" in full


def test_the_tool_is_offered_on_the_opensandbox_backend_only(monkeypatch):
    from app.services.builtin_tools.media import build_code_interpreter_tool

    monkeypatch.setattr(settings, "code_interpreter_workspace_enabled", True)
    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    monkeypatch.setattr(settings, "custom_tool_sandbox", "subprocess")
    without, _ = build_code_interpreter_tool(user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    assert "sandbox_run_command" not in {d.name for d in without}
    monkeypatch.setattr(settings, "custom_tool_sandbox", "opensandbox")
    definitions, _ = build_code_interpreter_tool(user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    tool = next(d for d in definitions if d.name == "sandbox_run_command")
    assert tool.input_schema["required"] == ["checkout"]
    assert {"check", "args", "argv", "cwd"} <= set(tool.input_schema["properties"])


def test_every_command_image_runs_the_workspace_helper_as_python():
    """remote.py runs its helper as ``python ...`` in every command container: the nodejs-dev stage must ship it, without pip."""
    assert "python {TOOL_PATH}" in Path(remote.__file__).read_text(encoding="utf-8")
    text = (Path(__file__).resolve().parents[2] / "backend" / "sandbox" / "Dockerfile.sandbox.dev").read_text(encoding="utf-8")
    stage = text.split(" AS nodejs-dev", 1)[1]
    assert "ln -s python3.14 /usr/local/bin/python " in stage
    assert "! python -m pip --version" in stage and "site-packages/pip*" in stage
    for line in stage.splitlines():
        if line.startswith("COPY --from=docker.io/"):
            assert "@sha256:" in line.split()[1], line


def test_the_repository_declares_its_own_checks():
    text = (Path(__file__).resolve().parents[2] / checks.CHECKS_FILE).read_text(encoding="utf-8")
    specs = checks.load_checks(text)
    assert {"backend-lint", "backend-types", "backend-unit", "frontend-types", "frontend-e2e-types", "frontend-lint", "frontend-i18n", "frontend-unit", "frontend-node-unit"} <= set(specs)
    assert specs["frontend-e2e-types"].argv == ("yarn", "typecheck", "-p", "tests_e2e")
    assert {"backend-format", "backend-alembic-heads", "frontend-knip", "github-scripts-node-test"} <= set(specs)
    assert specs["backend-lint"].report == "sarif" and specs["backend-unit"].report == "junit"
    # Every check runs on a locked, offline dev image, except the dependency-bump ones, which must reach the registry.
    # toolchain-rust runs the rustfmt gates and, since #11710 slice 2, the clippy gates on the image that ships
    # the GTK / WebKit / D-Bus libraries CI installs. toolchain-python runs the shellcheck and betterleaks
    # scanners over the checkout's tree, and since #2160 backend-lock-update (uv lock through the registry
    # mirror, writes backend/uv.lock only, installs nothing).
    offline = ("python-dev", "nodejs-dev", "toolchain-rust", "toolchain-python")
    online = {name for name, spec in specs.items() if spec.image not in offline}
    assert {name for name, spec in specs.items() if spec.image == "toolchain-python"} == {
        "shellcheck",
        "secrets",
        "backend-lock-update",
    }
    assert {name for name, spec in specs.items() if spec.image == "toolchain-rust"} == {
        "runner-fmt",
        "relay-fmt",
        "relay-agent-core-fmt",
        "runner-clippy",
        "runner-clippy-headless",
        "relay-clippy",
    }
    assert online == {
        "frontend-lock-update",
        "frontend-bump-install",
        "frontend-bump-types",
        "frontend-bump-safelist",
        "frontend-bump-build",
        "frontend-bump-classes",
        "frontend-bump-unit",
        "frontend-bump-knip",
        "office-addin-lock-update",
        "office-addin-install",
        "office-addin-check",
    }


def test_the_office_addin_checks_run_npm_on_the_registry_image_without_scripts():
    """The Office add-in checks mirror CI's 'Office add-in lint' job on toolchain-node, lifecycle scripts off."""
    root = Path(__file__).resolve().parents[2]
    specs = checks.load_checks((root / checks.CHECKS_FILE).read_text(encoding="utf-8"))
    for name in ("office-addin-lock-update", "office-addin-install", "office-addin-check"):
        assert specs[name].cwd == "apps/microsoft-office" and specs[name].image == "toolchain-node"
    assert "--ignore-scripts" in specs["office-addin-lock-update"].argv and "--package-lock-only" in specs["office-addin-lock-update"].argv
    assert specs["office-addin-install"].argv[:2] == ("npm", "ci") and "--ignore-scripts" in specs["office-addin-install"].argv
    assert specs["office-addin-check"].argv == ("npm", "run", "check")


def test_the_frontend_lock_update_writes_only_the_lockfile_on_the_registry_image():
    """A dependency bump regenerates yarn.lock on toolchain-node (registry allow-list), links nothing (#10540)."""
    root = Path(__file__).resolve().parents[2]
    spec = checks.load_checks((root / checks.CHECKS_FILE).read_text(encoding="utf-8"))["frontend-lock-update"]
    assert spec.argv == ("yarn", "install", "--mode=update-lockfile")
    assert spec.cwd == "frontend" and spec.image == "toolchain-node" and not spec.install_first
    yarnrc = (root / "frontend" / ".yarnrc.yml").read_text(encoding="utf-8")
    assert "enableScripts: false" in yarnrc and "npmMinimalAgeGate:" in yarnrc


def test_a_dependency_bump_is_linked_and_typed_on_the_container_that_fetched_it():
    """nodejs-dev is offline with main's cache: the bumped lock is installed and typed on toolchain-node.

    frontend-bump-types runs frontend-bump-install first (same cwd and image), never the offline
    frontend-install, so the tarballs frontend-lock-update fetched are linked where they were fetched.
    """
    root = Path(__file__).resolve().parents[2]
    specs = checks.load_checks((root / checks.CHECKS_FILE).read_text(encoding="utf-8"))
    install, types = specs["frontend-bump-install"], specs["frontend-bump-types"]
    assert install.argv == ("yarn", "install", "--immutable", "--mode=skip-build")
    assert types.argv == ("yarn", "typecheck") and types.install_first
    for spec in (install, types):
        assert spec.cwd == "frontend" and spec.image == "toolchain-node"
    resolved = commands._sibling_install(specs, types, root="xo", image="toolchain-node", max_timeout=900)
    assert resolved is not None and resolved.check == "frontend-bump-install"
    offline = commands._sibling_install(specs, specs["frontend-types"], root="xo", image="nodejs-dev", max_timeout=900)
    assert offline is not None and offline.check == "frontend-install"


def test_a_dependency_bump_is_built_tested_and_class_checked_on_the_container_that_fetched_it():
    """The offline gates have a toolchain-node twin, each linked by frontend-bump-install (#10591)."""
    root = Path(__file__).resolve().parents[2]
    specs = checks.load_checks((root / checks.CHECKS_FILE).read_text(encoding="utf-8"))
    twins = {
        "frontend-bump-safelist": None,
        "frontend-bump-build": "frontend-build",
        "frontend-bump-classes": "frontend-design-system-classes",
        "frontend-bump-unit": "frontend-unit",
    }
    for name, offline_name in twins.items():
        spec = specs[name]
        assert spec.cwd == "frontend" and spec.image == "toolchain-node" and spec.install_first
        if offline_name is not None:
            # The offline class check compiles in-process (--compile, a pre-push guard); the bump twin reads the
            # bump build's dist, as CI does.
            assert spec.argv == tuple(a for a in specs[offline_name].argv if a != "--compile")
        resolved = commands._sibling_install(specs, spec, root="xo", image="toolchain-node", max_timeout=900)
        assert resolved is not None and resolved.check == "frontend-bump-install"
    assert specs["frontend-bump-safelist"].argv == ("node", "scripts/fds-migration/generate-safelist.mjs")
    assert specs["frontend-bump-unit"].report == "junit"


def test_the_node_test_files_are_a_declared_check():
    """The frontend's node:test files (excluded from vitest) run in the sandbox through the same script as CI's test:unit."""
    root = Path(__file__).resolve().parents[2]
    spec = checks.load_checks((root / checks.CHECKS_FILE).read_text(encoding="utf-8"))["frontend-node-unit"]
    assert spec.argv == ("node", "scripts/node-test-files.mjs") and spec.cwd == "frontend" and spec.image == "nodejs-dev"
    package = (root / "frontend" / "package.json").read_text(encoding="utf-8")
    assert '"test:unit": "node scripts/node-test-files.mjs && vitest run"' in package
    script = (root / "frontend" / "scripts" / "node-test-files.mjs").read_text(encoding="utf-8")
    assert "process.argv.slice(2)" in script


def test_the_snapshot_and_fixture_update_is_a_declared_check():
    """vitest --update=all with ARCHITECTURE_FIXTURE=write: pinned snapshots and the backend's renderer fixtures are rewritable.

    The value is explicit: a bare ``-u`` takes the first file arg as its optional value and the whole suite runs.
    """
    root = Path(__file__).resolve().parents[2]
    specs = checks.load_checks((root / checks.CHECKS_FILE).read_text(encoding="utf-8"))
    spec = specs["frontend-unit-update"]
    assert spec.argv == ("yarn", "vitest", "run", "--update=all") and spec.cwd == "frontend" and spec.image == "nodejs-dev"
    assert dict(spec.env) == {"ARCHITECTURE_FIXTURE": "write"} and spec.default_args == ()
    assert not any(arg.startswith("--update") or arg == "-u" for arg in specs["frontend-unit"].argv)
    assert not specs["frontend-unit"].env
    test = root / "frontend/src/components/presentation/blocks/architecture-bare.test.tsx"
    assert 'process.env.ARCHITECTURE_FIXTURE === "write"' in test.read_text(encoding="utf-8")


def test_the_tripwire_suites_are_a_declared_check():
    """The repository-wide tripwires run in the sandbox, every suite the check names exists."""
    root = Path(__file__).resolve().parents[2]
    spec = checks.load_checks((root / checks.CHECKS_FILE).read_text(encoding="utf-8"))["backend-tripwires"]
    assert spec.argv[:3] == ("python", "-m", "pytest")
    assert spec.image == "python-dev" and spec.report == "junit" and spec.report_path == ".xtm/tripwires.xml"
    suites = [arg for arg in spec.argv if arg.startswith("tests/")]
    assert "tests/test_agent_facing_placeholders.py" in suites and "tests/test_no_workarounds.py" in suites
    # The route-table invariants walk every route of the app: a new write route that
    # answers before it commits fails here, not one CI round later.
    assert "tests/test_write_routes_commit_before_response.py" in suites
    assert "tests/test_ai_disabled_read_surface.py" in suites
    missing = [suite for suite in suites if not (root / spec.cwd / suite).is_file()]
    assert not missing, f"backend-tripwires names suites that no longer exist: {missing}"


def test_the_ci_only_gates_are_declared_checks():
    """knip, the Alembic graph, ruff format and the workflow scripts' node:test suites run in the sandbox, not only in CI."""
    root = Path(__file__).resolve().parents[2]
    specs = checks.load_checks((root / checks.CHECKS_FILE).read_text(encoding="utf-8"))
    knip = specs["frontend-knip"]
    assert knip.argv == ("yarn", "knip") and knip.cwd == "frontend" and knip.image == "nodejs-dev"
    assert '"knip": "knip' in (root / "frontend" / "package.json").read_text(encoding="utf-8")
    fmt = specs["backend-format"]
    assert fmt.argv == ("python", "-m", "scripts.format_changed") and fmt.cwd == "backend" and fmt.image == "python-dev"
    assert fmt.affected == "lines" and fmt.affected_narrows_args
    heads = specs["backend-alembic-heads"]
    assert heads.cwd == "backend" and heads.image == "python-dev" and heads.report == "junit"
    suites = [arg for arg in heads.argv if arg.startswith("tests/")]
    assert "tests/test_alembic_single_head.py" in suites
    assert all((root / heads.cwd / suite).is_file() for suite in suites), suites
    scripts = specs["github-scripts-node-test"]
    assert scripts.argv == ("node", "--test") and scripts.cwd == ".github/scripts" and scripts.image == "nodejs-dev"
    assert list((root / scripts.cwd).glob("*.test.mjs")), "no node:test suite left under .github/scripts"


def test_the_design_system_guards_are_declared_checks():
    """CI's 'Design-system guards' step runs in the sandbox: count-legacy --check, the build, verify-classes."""
    root = Path(__file__).resolve().parents[2]
    specs = checks.load_checks((root / checks.CHECKS_FILE).read_text(encoding="utf-8"))
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    guards = specs["frontend-design-system-guards"]
    assert guards.argv == ("node", "scripts/fds-migration/count-legacy.mjs", "--check")
    build = specs["frontend-build"]
    assert build.argv == ("yarn", "build")
    classes = specs["frontend-design-system-classes"]
    # CI builds then reads dist; the sandbox compiles src/index.css in-process (no build) as a pre-push guard.
    assert classes.argv == ("node", "scripts/fds-migration/verify-classes.mjs", "--compile")
    assert classes.pre_push
    for spec in (guards, build, classes):
        assert spec.cwd == "frontend" and spec.image == "nodejs-dev"
        if spec.argv[0] == "node":
            assert (root / spec.cwd / spec.argv[1]).is_file(), spec.argv
        ci_argv = " ".join(a for a in spec.argv if a != "--compile")
        assert ci_argv in workflow, f"ci.yml no longer runs {ci_argv}"


def test_the_ruff_fixers_write_only_the_files_named():
    """backend-format-fix and backend-lint-fix rewrite the files given as args; backend-format-fix on the changed lines."""
    root = Path(__file__).resolve().parents[2]
    specs = checks.load_checks((root / checks.CHECKS_FILE).read_text(encoding="utf-8"))
    fmt, lint = specs["backend-format-fix"], specs["backend-lint-fix"]
    assert fmt.cwd == lint.cwd == "backend" and fmt.image == lint.image == "python-dev"
    fix = ("python", "-m", "scripts.format_changed", "--fix")
    assert checks.command_argv(fmt, ["app/x.py:3-9"]) == (*fix, "app/x.py:3-9")
    assert fmt.affected == "lines" and "*.py" in fmt.affected_globs
    assert checks.command_argv(lint, ["app/x.py"]) == ("ruff", "check", "--fix", "app/x.py")
    assert checks.command_argv(fmt, []) == fix
    # Without args it runs on the change; a bare path is narrowed to the change's lines of it (#11073).
    assert fmt.affected_by_default and fmt.affected_narrows_args
    assert not lint.affected_narrows_args
    assert checks.command_argv(lint, []) == ("ruff", "check", "--fix", "--diff")


def test_the_snapshot_generator_is_a_declared_check():
    """The HITL snapshots are regenerated by a check, never edited by hand."""
    root = Path(__file__).resolve().parents[2]
    spec = checks.load_checks((root / checks.CHECKS_FILE).read_text(encoding="utf-8"))["backend-snapshots"]
    assert spec.argv[:3] == ("python", "-m", "scripts.dump_platform_read_whitelist")
    assert spec.image == "python-dev" and spec.report == "lines"
    module = root / spec.cwd / (spec.argv[2].replace(".", "/") + ".py")
    assert module.is_file() and "--check" in module.read_text(encoding="utf-8")


def test_a_generator_of_backend_scripts_runs_through_a_declared_check(monkeypatch, capsys):
    """#11483: a generated file is regenerated by backend-generate, never rewritten by code_interpreter."""
    root = Path(__file__).resolve().parents[2]
    spec = checks.load_checks((root / checks.CHECKS_FILE).read_text(encoding="utf-8"))["backend-generate"]
    assert spec.argv[:3] == ("python", "-m", "scripts.run_generator") and spec.cwd == "backend"
    assert checks.command_argv(spec, ["refresh_lucide_icons", "--check"])[-2:] == ("refresh_lucide_icons", "--check")

    from scripts import run_generator

    assert "dump_platform_read_whitelist" in run_generator.generators()
    assert "run_generator" not in run_generator.generators()
    assert run_generator.resolve("dump_platform_read_whitelist") == "scripts.dump_platform_read_whitelist"
    for refused in ("os", "scripts.x", "../app", "", "-c"):
        assert run_generator.resolve(refused) is None
    assert run_generator.main(["nope"]) == 2 and "not a module of backend/scripts/" in capsys.readouterr().err
    assert run_generator.main([]) == 2

    seen = {}
    monkeypatch.setattr(
        run_generator.runpy, "run_module", lambda m, **kw: seen.update(m=m, argv=list(run_generator.sys.argv))
    )
    assert run_generator.main(["dump_platform_read_whitelist", "--check"]) == 0
    assert seen == {
        "m": "scripts.dump_platform_read_whitelist",
        "argv": ["scripts.dump_platform_read_whitelist", "--check"],
    }

    def _exit(m, **kw):
        raise SystemExit(3)

    monkeypatch.setattr(run_generator.runpy, "run_module", _exit)
    assert run_generator.main(["dump_platform_read_whitelist"]) == 3
