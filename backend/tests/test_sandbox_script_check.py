"""The built-in ``script`` check of ``sandbox_run_command``: one workspace file run without an arbitrary argv."""

from __future__ import annotations

import shutil

import pytest

from app.services.sandbox_workspace import checks, commands
from tests.test_sandbox_run_command import _resolve, _workspace, api  # noqa: F401 - the fake container fixture

DECLARED = b"""
[check.backend-install]
argv = ["python", "-m", "pip", "install", "-r", "requirements.txt"]
cwd = "backend"
image = "python-dev"

[check.backend-unit]
argv = ["python", "-m", "pytest"]
cwd = "backend"
image = "python-dev"
env = { SECRET_KEY = "ci" }

[check.frontend-unit]
argv = ["yarn", "test"]
cwd = "frontend"
image = "nodejs-dev"
"""

IMAGES = {"python", "python-dev", "nodejs-dev"}


async def _declared():
    workspace = _workspace()
    await workspace.put_tree(
        [
            ("co/.xtm-one/checks.toml", DECLARED),
            ("co/backend/repro.py", b"print('repro')\n"),
            ("co/frontend/read.mjs", b"console.log('read')\n"),
            ("co/tools/gen.ts", b"console.log('gen')\n"),
            ("work/scratch.py", b"print('scratch')\n"),
            ("co/notes.txt", b"x\n"),
        ]
    )
    return workspace


async def test_a_python_script_runs_on_the_image_and_env_of_its_directory_with_its_install_bound():
    workspace = await _declared()
    command = await _resolve(workspace, check="script", args=["backend/repro.py", "--n", "2"], known_images=IMAGES)
    assert command.argv == ("python", "backend/repro.py", "--n", "2")
    assert command.cwd == "co" and command.image == "python-dev" and command.label == "script"
    assert dict(command.env) == {"SECRET_KEY": "ci"}
    assert command.install is not None and command.install.check == "backend-install"


async def test_a_node_script_runs_with_node_on_the_node_image():
    workspace = await _declared()
    command = await _resolve(workspace, check="script", args=["frontend/read.mjs"], known_images=IMAGES)
    assert command.argv == ("node", "frontend/read.mjs") and command.image == "nodejs-dev"
    typed = await _resolve(workspace, check="script", args=["tools/gen.ts"], known_images=IMAGES)
    assert typed.argv == ("node", "--experimental-strip-types", "tools/gen.ts") and typed.image == "nodejs-dev"


async def test_a_workspace_file_outside_the_checkout_runs_with_the_checkout_as_cwd():
    workspace = await _declared()
    command = await _resolve(workspace, check="script", args=["workspace/work/scratch.py"], known_images=IMAGES)
    assert command.argv == ("python", "../work/scratch.py") and command.cwd == "co"


@pytest.mark.parametrize("path", ["/etc/passwd.py", "../outside.py", "backend/../../x.py"])
async def test_a_script_outside_the_workspace_is_refused(path):
    workspace = await _declared()
    with pytest.raises(checks.ChecksError, match="check 'script'"):
        await _resolve(workspace, check="script", args=[path], known_images=IMAGES)


async def test_a_missing_file_an_unknown_kind_or_no_file_is_refused():
    workspace = await _declared()
    with pytest.raises(checks.ChecksError, match="is not a file of the workspace"):
        await _resolve(workspace, check="script", args=["backend/absent.py"], known_images=IMAGES)
    with pytest.raises(checks.ChecksError, match="is none of them"):
        await _resolve(workspace, check="script", args=["notes.txt"], known_images=IMAGES)
    with pytest.raises(checks.ChecksError, match="takes the file to run"):
        await _resolve(workspace, check="script", known_images=IMAGES)


async def test_the_script_check_needs_no_argv_permission_and_argv_stays_refused():
    workspace = await _declared()
    await _resolve(workspace, check="script", args=["backend/repro.py"], allow_argv=False, known_images=IMAGES)
    with pytest.raises(checks.ChecksError, match="not allowed on this platform"):
        await _resolve(workspace, argv=["python", "backend/repro.py"], known_images=IMAGES)


async def test_a_check_the_repository_declares_under_that_name_wins():
    workspace = _workspace()
    await workspace.put_tree([("co/.xtm-one/checks.toml", b'[check.script]\nargv = ["python", "-c", "print(1)"]\n')])
    command = await _resolve(workspace, check="script")
    assert command.argv == ("python", "-c", "print(1)")


async def test_a_detected_uv_project_runs_the_script_in_its_environment():
    workspace = _workspace()
    await workspace.put_tree(
        [
            ("co/.xtm-checkout.json", b"{}"),
            ("co/pyproject.toml", b'[project]\nname = "x"\n[tool.pytest.ini_options]\n'),
            ("co/uv.lock", b""),
            ("co/repro.py", b"print(1)\n"),
        ]
    )
    command = await _resolve(workspace, check="script", args=["repro.py"], known_images={"python"})
    assert command.argv == ("uv", "run", "python", "repro.py")


async def test_a_uv_script_gets_the_with_dependencies_of_its_directory_s_uv_checks():
    # XTM-Flow/filigran-flow: CI runs `uv run --with pytest --with pyyaml --with jsonschema ... pytest`; the script
    # check ran `uv run python plugin/scripts/flow_lint.py` without them and failed on `import jsonschema`.
    declared = (
        b'[check.python-unit]\nargv = ["uv", "run", "--with", "pytest", "--with=pyyaml", "--with", "jsonschema", '
        b'"pytest", "plugin/tests", "--with", "not-an-option"]\nimage = "python"\n'
        b'[check.plugin-lint]\nargv = ["uv", "run", "--frozen", "--with", "pyyaml", "--with-requirements", '
        b'"reqs.txt", "ruff", "check"]\ncwd = "plugin"\nimage = "python"\n'
    )
    workspace = _workspace()
    await workspace.put_tree(
        [
            ("co/.xtm-one/checks.toml", declared),
            ("co/plugin/scripts/flow_lint.py", b"import jsonschema\n"),
            ("co/repro.py", b"print(1)\n"),
        ]
    )
    root = await _resolve(workspace, check="script", args=["repro.py", "-q"], known_images={"python"})
    assert root.argv == (
        "uv", "run", "--with", "pytest", "--with", "pyyaml", "--with", "jsonschema", "python", "repro.py", "-q"
    )  # fmt: skip
    nested = await _resolve(workspace, check="script", args=["plugin/scripts/flow_lint.py"], known_images={"python"})
    assert nested.argv == (
        "uv", "run", "--with", "pyyaml", "--with-requirements", "plugin/reqs.txt", "python", "plugin/scripts/flow_lint.py"
    )  # fmt: skip


async def test_a_script_runs_on_the_interpreter_its_directory_s_install_used():
    # A project pinned to 3.12 installs with ``python3.12 -m pip``: the image's default python sees none
    # of those packages, so the script ran on it and failed with "No module named ...".
    workspace = _workspace()
    await workspace.put_tree(
        [
            ("co/.xtm-checkout.json", b"{}"),
            ("co/pkg/tests/test-requirements.txt", b"pytest\n"),
            ("co/pkg/tests/.python-version", b"3.12\n"),
            ("co/pkg/tests/test_x.py", b""),
            ("co/pkg/tests/probe.py", b"import pytest\n"),
        ]
    )
    command = await _resolve(workspace, check="script", args=["pkg/tests/probe.py"], known_images={"toolchain-python"})
    assert command.install is not None and command.install.argv[0] == "python3.12"
    assert command.argv == ("python3.12", "pkg/tests/probe.py")


async def test_a_declared_check_on_the_image_s_python_keeps_the_plain_interpreter():
    command = await _resolve(await _declared(), check="script", args=["backend/repro.py"], known_images=IMAGES)
    assert command.argv[0] == "python"


async def test_without_a_check_of_its_stack_the_script_runs_on_the_stack_image():
    workspace = _workspace()
    await workspace.put_tree([("co/.xtm-one/checks.toml", b'[check.a]\nargv = ["echo"]\n'), ("co/x.py", b"")])
    command = await _resolve(workspace, check="script", args=["x.py"], known_images={"python"})
    assert command.image == "python" and command.install is None
    with pytest.raises(checks.ChecksError, match="OPENSANDBOX_IMAGES does not configure"):
        await _resolve(workspace, check="script", args=["x.py"], known_images={"nodejs"})


async def test_cwd_runs_a_module_script_from_its_package_directory():
    # openaev-front/scripts/i18n-untranslated.js reads scripts/i18n-glossary.json relative to openaev-front:
    # run from the checkout root it failed with ENOENT, and argv with cwd is refused on the platform.
    workspace = await _declared()
    command = await _resolve(
        workspace, check="script", args=["frontend/read.mjs", "--x"], cwd="frontend", known_images=IMAGES
    )
    assert command.cwd == "co/frontend" and command.argv == ("node", "read.mjs", "--x")
    assert command.image == "nodejs-dev"
    outside = await _resolve(workspace, check="script", args=["backend/repro.py"], cwd="frontend", known_images=IMAGES)
    assert outside.cwd == "co/frontend" and outside.argv == ("python", "../backend/repro.py")
    assert outside.image == "python-dev" and outside.install is not None
    root = await _resolve(workspace, check="script", args=["backend/repro.py"], cwd="/", known_images=IMAGES)
    assert root.cwd == "co" and root.argv == ("python", "backend/repro.py")


@pytest.mark.parametrize("cwd", ["../x", "/etc", "backend/../../x"])
async def test_a_script_cwd_outside_the_checkout_is_refused(cwd):
    workspace = await _declared()
    with pytest.raises(checks.ChecksError, match="check 'script'"):
        await _resolve(workspace, check="script", args=["backend/repro.py"], cwd=cwd, known_images=IMAGES)


async def test_a_script_cwd_that_holds_no_file_is_refused():
    workspace = await _declared()
    with pytest.raises(checks.ChecksError, match="is not a directory of the checkout"):
        await _resolve(workspace, check="script", args=["backend/repro.py"], cwd="absent", known_images=IMAGES)


async def test_a_uv_with_requirements_path_follows_the_script_cwd():
    declared = (
        b'[check.plugin-lint]\nargv = ["uv", "run", "--with-requirements", "reqs.txt", "ruff", "check"]\n'
        b'cwd = "plugin"\nimage = "python"\n'
    )
    workspace = _workspace()
    await workspace.put_tree([("co/.xtm-one/checks.toml", declared), ("co/plugin/scripts/lint.py", b"print(1)\n")])
    command = await _resolve(
        workspace, check="script", args=["plugin/scripts/lint.py"], cwd="plugin", known_images={"python"}
    )
    assert command.cwd == "co/plugin"
    assert command.argv == ("uv", "run", "--with-requirements", "reqs.txt", "python", "scripts/lint.py")


async def test_the_listing_names_the_built_in_script_check():
    workspace = await _declared()
    listing = await commands.list_checks(workspace, checkout="co", known_images=IMAGES)
    assert commands.SCRIPT_LISTING in listing


async def test_a_python_script_runs_and_prints_its_result(api):  # noqa: F811 - the fixture
    workspace = _workspace()
    await workspace.put_tree(
        [
            ("co/.xtm-one/checks.toml", b'[check.a]\nargv = ["python", "-c", "print(1)"]\n'),
            ("co/repro.py", b"import sys, pathlib\nprint('args', sys.argv[1:], pathlib.Path('repro.py').exists())\n"),
        ]
    )
    command = await _resolve(workspace, check="script", args=["repro.py", "one"])
    outcome = await commands.run_command(command, workspace=workspace, sandbox_api=api)
    assert outcome.exit_code == 0 and "args ['one'] True" in outcome.text


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed on this runner")
async def test_a_node_script_reads_a_checkout_file(api):  # noqa: F811 - the fixture
    workspace = _workspace()
    await workspace.put_tree(
        [
            ("co/.xtm-one/checks.toml", b'[check.a]\nargv = ["python", "-c", "print(1)"]\n'),
            ("co/data.txt", b"hello-from-checkout\n"),
            (
                "co/read.mjs",
                b"import { readFileSync } from 'node:fs';\nconsole.log(readFileSync('data.txt', 'utf8').trim());\n",
            ),
        ]
    )
    command = await _resolve(workspace, check="script", args=["read.mjs"], known_images={"python", "nodejs"})
    outcome = await commands.run_command(command, workspace=workspace, sandbox_api=api)
    assert outcome.exit_code == 0 and "hello-from-checkout" in outcome.text
