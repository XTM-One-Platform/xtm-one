"""The lab serves a @filigran/chatbot branch named by LAB_CHATBOT (#10928).

lab.py is standard library only and starts the stores when imported, so the functions under test are
compiled one by one from its syntax tree (as ``test_lab_org_ai_draft.py`` does), with CHECKOUT pointed
at a temporary directory. dev-docs/reference/agentic-labs.md, "A @filigran/chatbot branch in the lab".
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
CHECKS = ROOT / ".xtm-one" / "checks.toml"

pytestmark = pytest.mark.skipif(not LAB.is_file() or not CHECKS.is_file(), reason="no .xtm-one next to the backend")

_NAMES = (
    "chatbot_source",
    "chatbot_origin",
    "link_chatbot",
    "semver_tuple",
    "semver_satisfies",
    "check_chatbot_peers",
    "workspace_peer",
    "serve_workspace_peer",
    "CHATBOT_PACKAGE",
    "CHATBOT_NAME",
    "YARN_ENV",
    "PNPM",
    "PNPM_MONOREPO_FILES",
    "fetch_pnpm",
    "build_chatbot_pnpm",
)


def _lab(checkout: Path, logged: list[str], ran: list[tuple[str, list[str]]]) -> dict:
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    body = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in _NAMES)
        or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in _NAMES for t in node.targets))
    ]
    namespace: dict = {
        "Path": Path,
        "json": json,
        "os": os,
        "re": re,
        "shutil": shutil,
        "CHECKOUT": checkout,
        "CACHE": checkout / ".cache",
        "log": logged.append,
        "run": lambda name, argv, **_: ran.append((name, argv)),
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(LAB), "exec"), namespace)  # noqa: S102 - the repo's own file
    return namespace


@pytest.fixture(autouse=True)
def _no_platform_mode(monkeypatch):
    """The log names PLATFORM_MODE when set (#11300): each test sets it when it asserts on it."""
    monkeypatch.delenv("PLATFORM_MODE", raising=False)


def _package(where: Path, *, built: bool, version: str = "3.22.0-branch", peers: dict | None = None) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    manifest = {"name": "@filigran/chatbot", "version": version, **({"peerDependencies": peers} if peers else {})}
    (where / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    if built:
        (where / "dist").mkdir()
        for name in ("index.js", "markdown.js", "workspaces.js", "styles.css"):
            (where / "dist" / name).write_text(f"/* {name} of the branch */", encoding="utf-8")
    return where


def _installed(checkout: Path) -> Path:
    target = checkout / "frontend" / "node_modules" / "@filigran" / "chatbot"
    (target / "dist").mkdir(parents=True)
    (target / "package.json").write_text(
        json.dumps({"name": "@filigran/chatbot", "version": "3.21.0"}), encoding="utf-8"
    )
    (target / "dist" / "index.js").write_text("/* pinned */", encoding="utf-8")
    (target / "dist" / "stale.js").write_text("/* pinned only */", encoding="utf-8")
    (checkout / "frontend" / "node_modules" / ".vite").mkdir()
    return target


def test_unset_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("LAB_CHATBOT", raising=False)
    target = _installed(tmp_path)
    logged, ran = [], []
    _lab(tmp_path, logged, ran)["link_chatbot"](Path("node"), Path("yarn.js"))
    assert (target / "dist" / "index.js").read_text() == "/* pinned */" and not logged and not ran


def test_a_built_package_replaces_the_pinned_one_and_the_log_names_its_commit(tmp_path, monkeypatch):
    root = tmp_path / "lab-chatbot"
    _package(root / "packages" / "filigran-chatbot", built=True)
    (root / ".xtm-checkout.json").write_text(
        json.dumps({"repository": "FiligranHQ/filigran-ui", "ref": "feat/x", "commit": "2480f8c47124230ea19cecc0"}),
        encoding="utf-8",
    )
    target = _installed(tmp_path)
    monkeypatch.setenv("LAB_CHATBOT", "lab-chatbot")
    logged, ran = [], []
    _lab(tmp_path, logged, ran)["link_chatbot"](Path("node"), Path("yarn.js"))
    assert not ran  # dist/ present: no install, no build
    assert json.loads((target / "package.json").read_text())["version"] == "3.22.0-branch"
    assert sorted(p.name for p in (target / "dist").iterdir()) == [
        "index.js",
        "markdown.js",
        "styles.css",
        "workspaces.js",
    ]
    assert not (tmp_path / "frontend" / "node_modules" / ".vite").exists()
    assert logged == [
        "chatbot: serving @filigran/chatbot 3.22.0-branch from lab-chatbot, FiligranHQ/filigran-ui@2480f8c47124 (feat/x)"
    ]


def test_a_checkout_without_dist_is_installed_and_built_first(tmp_path, monkeypatch):
    root = tmp_path / "lab-chatbot"
    package = _package(root / "packages" / "filigran-chatbot", built=False)
    _installed(tmp_path)
    monkeypatch.setenv("LAB_CHATBOT", "lab-chatbot/")
    logged, ran = [], []
    namespace = _lab(tmp_path, logged, ran)

    envs = {}

    def build(name, argv, **kwargs):
        ran.append((name, argv))
        envs[name] = kwargs.get("env") or {}
        if name == "chatbot-build":
            shutil.rmtree(package, ignore_errors=True)
            _package(package, built=True)

    namespace["run"] = build
    namespace["link_chatbot"](Path("/n/node"), Path("/y/yarn.js"))
    assert [name for name, _ in ran] == ["chatbot-install", "chatbot-build"]
    assert ran[1][1][-4:] == ["workspace", "@filigran/chatbot", "run", "build"]
    assert "--immutable" not in ran[0][1]
    # Yarn turns immutable installs on by itself under CI; a lagging branch lockfile must still install.
    assert envs["chatbot-install"]["YARN_ENABLE_IMMUTABLE_INSTALLS"] == "false"
    assert logged == ["chatbot: serving @filigran/chatbot 3.22.0-branch from lab-chatbot"]


@pytest.mark.parametrize("mode", ["copilot", "xtm_one"])
def test_the_log_names_the_platform_mode_the_accent_follows(tmp_path, monkeypatch, mode):
    _package(tmp_path / "lab-chatbot" / "packages" / "filigran-chatbot", built=True)
    _installed(tmp_path)
    monkeypatch.setenv("LAB_CHATBOT", "lab-chatbot")
    monkeypatch.setenv("PLATFORM_MODE", mode)
    logged, ran = [], []
    _lab(tmp_path, logged, ran)["link_chatbot"](Path("node"), Path("yarn.js"))
    assert logged == [f"chatbot: serving @filigran/chatbot 3.22.0-branch from lab-chatbot, in {mode} mode"]


def _pnpm_monorepo(root: Path, *, engines: str = "10.10.0", leave_out: tuple[str, ...] = ()) -> Path:
    """A filigran-design-system checkout as the documented sub_paths write it (#11175)."""
    package = _package(root / "packages" / "filigran-chatbot", built=False)
    (root / "packages" / "filigran-design-system").mkdir(parents=True)
    files = {
        "package.json": json.dumps({"name": "filigran-design-system-monorepo", "engines": {"pnpm": engines}}),
        "pnpm-workspace.yaml": 'packages:\n  - "packages/*"\n',
        "pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
        "llms.txt": "llms",
        "llms-full.txt": "llms full",
        "packages/filigran-design-system/package.json": json.dumps({"name": "@filigran/design-system"}),
    }
    for name, text in files.items():
        if name not in leave_out:
            (root / name).write_text(text, encoding="utf-8")
    return package


def test_a_pnpm_monorepo_is_installed_frozen_and_built_with_a_pnpm_filter(tmp_path, monkeypatch):
    root = tmp_path / "lab-chatbot"
    package = _pnpm_monorepo(root)
    (root / ".xtm-checkout.json").write_text(
        json.dumps({"repository": "XTM-Foundation/filigran-design-system", "commit": "f0b8a915aa11bb22cc33dd44"}),
        encoding="utf-8",
    )
    _installed(tmp_path)
    monkeypatch.setenv("LAB_CHATBOT", "lab-chatbot")
    logged, ran, cwds = [], [], {}
    namespace = _lab(tmp_path, logged, ran)

    def build(name, argv, **kwargs):
        ran.append((name, argv))
        cwds[name] = kwargs.get("cwd")
        if name == "chatbot-build":
            shutil.rmtree(package, ignore_errors=True)
            _package(package, built=True)

    namespace["run"] = build
    namespace["fetch_pnpm"] = lambda node: Path("/p/pnpm.cjs")
    namespace["link_chatbot"](Path("/n/node"), Path("/y/yarn.js"))
    assert ran == [
        ("chatbot-install", ["/n/node", "/p/pnpm.cjs", "install", "--frozen-lockfile"]),
        ("chatbot-build", ["/n/node", "/p/pnpm.cjs", "--filter", "@filigran/chatbot", "run", "build"]),
    ]
    assert cwds == {"chatbot-install": root, "chatbot-build": root}
    assert logged == [
        "chatbot: serving @filigran/chatbot 3.22.0-branch from lab-chatbot, "
        "XTM-Foundation/filigran-design-system@f0b8a915aa11"
    ]


def test_a_pnpm_monorepo_missing_a_sub_path_stops_the_lab_naming_the_sub_paths(tmp_path, monkeypatch):
    _pnpm_monorepo(tmp_path / "lab-chatbot", leave_out=("llms.txt", "packages/filigran-design-system/package.json"))
    _installed(tmp_path)
    monkeypatch.setenv("LAB_CHATBOT", "lab-chatbot")
    logged, ran = [], []
    with pytest.raises(SystemExit) as stop:
        _lab(tmp_path, logged, ran)["link_chatbot"](Path("node"), Path("yarn.js"))
    says = str(stop.value.code)
    assert "without llms.txt, packages/filigran-design-system/package.json" in says
    assert '"packages/filigran-design-system"' in says and '"pnpm-lock.yaml"' in says
    assert not ran and not logged


def test_a_pnpm_monorepo_pinning_another_pnpm_stops_the_lab_with_both_versions(tmp_path, monkeypatch):
    _pnpm_monorepo(tmp_path / "lab-chatbot", engines="11.0.0")
    _installed(tmp_path)
    monkeypatch.setenv("LAB_CHATBOT", "lab-chatbot")
    ran: list = []
    with pytest.raises(SystemExit, match=r"pins pnpm 11\.0\.0, the lab ships pnpm 10\.10\.0"):
        _lab(tmp_path, [], ran)["link_chatbot"](Path("node"), Path("yarn.js"))
    assert not ran


def _design_system(checkout: Path, version: str) -> None:
    where = checkout / "frontend" / "node_modules" / "@filigran" / "design-system"
    where.mkdir(parents=True, exist_ok=True)
    (where / "package.json").write_text(json.dumps({"name": "@filigran/design-system", "version": version}))


def test_an_unsatisfied_design_system_peer_stops_the_lab_with_the_versions(tmp_path, monkeypatch):
    root = tmp_path / "lab-chatbot"
    _package(
        root / "packages" / "filigran-chatbot",
        built=False,
        version="3.22.0",
        peers={"@filigran/design-system": "^1.5.0", "react": "^19.0.0"},
    )
    target = _installed(tmp_path)
    _design_system(tmp_path, "1.4.0")
    monkeypatch.setenv("LAB_CHATBOT", "lab-chatbot")
    logged, ran = [], []
    with pytest.raises(SystemExit) as stop:
        _lab(tmp_path, logged, ran)["link_chatbot"](Path("node"), Path("yarn.js"))
    assert str(stop.value.code).startswith(
        "chatbot: @filigran/chatbot 3.22.0 needs @filigran/design-system ^1.5.0, checkout pins 1.4.0: "
    )
    # Stopped before the install and build, the pinned package untouched.
    assert not ran and not logged and (target / "dist" / "index.js").read_text() == "/* pinned */"


def test_a_design_system_the_monorepo_holds_in_range_is_served_over_the_pin(tmp_path, monkeypatch):
    """DS main moves the chatbot's peer range ahead of xtm-one's pin: the checkout's own design system is served."""
    root = tmp_path / "lab-chatbot"
    package = _pnpm_monorepo(root)
    _package(package, built=False, version="3.29.1", peers={"@filigran/design-system": "^1.5.1"})
    ds = root / "packages" / "filigran-design-system"
    (ds / "package.json").write_text(json.dumps({"name": "@filigran/design-system", "version": "1.5.1"}))
    _installed(tmp_path)
    _design_system(tmp_path, "1.5.0")
    pinned_ds = tmp_path / "frontend" / "node_modules" / "@filigran" / "design-system"
    (pinned_ds / "dist").mkdir()
    (pinned_ds / "dist" / "stale.js").write_text("/* 1.5.0 */", encoding="utf-8")
    monkeypatch.setenv("LAB_CHATBOT", "lab-chatbot")
    logged, ran = [], []
    namespace = _lab(tmp_path, logged, ran)

    def build(name, argv, **kwargs):
        ran.append((name, argv))
        if name == "chatbot-install":  # the root prepare builds the design system
            (ds / "dist").mkdir()
            (ds / "dist" / "index.mjs").write_text("/* 1.5.1 */", encoding="utf-8")
        if name == "chatbot-build":
            (package / "dist").mkdir()
            (package / "dist" / "index.js").write_text("/* 3.29.1 */", encoding="utf-8")

    namespace["run"] = build
    namespace["fetch_pnpm"] = lambda node: Path("/p/pnpm.cjs")
    namespace["link_chatbot"](Path("/n/node"), Path("/y/yarn.js"))
    assert json.loads((pinned_ds / "package.json").read_text())["version"] == "1.5.1"
    assert sorted(p.name for p in (pinned_ds / "dist").iterdir()) == ["index.mjs"]
    assert logged == [
        "chatbot: serving peer @filigran/design-system 1.5.1 from lab-chatbot/packages/filigran-design-system "
        "over the pinned 1.5.0",
        "chatbot: serving @filigran/chatbot 3.29.1 from lab-chatbot",
    ]


def test_a_monorepo_design_system_out_of_range_still_stops_the_lab(tmp_path, monkeypatch):
    root = tmp_path / "lab-chatbot"
    package = _pnpm_monorepo(root)
    _package(package, built=False, version="3.29.1", peers={"@filigran/design-system": "^1.6.0"})
    (root / "packages" / "filigran-design-system" / "package.json").write_text(
        json.dumps({"name": "@filigran/design-system", "version": "1.5.1"})
    )
    _installed(tmp_path)
    _design_system(tmp_path, "1.5.0")
    monkeypatch.setenv("LAB_CHATBOT", "lab-chatbot")
    ran: list = []
    with pytest.raises(SystemExit, match=r"needs @filigran/design-system \^1\.6\.0, checkout pins 1\.5\.0"):
        _lab(tmp_path, [], ran)["link_chatbot"](Path("node"), Path("yarn.js"))
    assert not ran


def test_a_satisfied_or_absent_peer_lets_the_package_through(tmp_path, monkeypatch):
    root = tmp_path / "lab-chatbot"
    _package(
        root / "packages" / "filigran-chatbot",
        built=True,
        version="3.22.0",
        peers={"@filigran/design-system": "^1.5.0", "react": "^19.0.0"},
    )
    target = _installed(tmp_path)
    _design_system(tmp_path, "1.5.2")  # react is not installed in this fixture: let through
    monkeypatch.setenv("LAB_CHATBOT", "lab-chatbot")
    logged, ran = [], []
    _lab(tmp_path, logged, ran)["link_chatbot"](Path("node"), Path("yarn.js"))
    assert json.loads((target / "package.json").read_text())["version"] == "3.22.0"


@pytest.mark.parametrize(
    ("version", "spec", "verdict"),
    [
        ("1.5.0", "^1.5.0", True),
        ("1.9.3", "^1.5.0", True),
        ("1.4.0", "^1.5.0", False),
        ("2.0.0", "^1.5.0", False),
        ("1.5.0-rc.1", "^1.5.0", False),
        ("0.3.4", "^0.3.1", True),
        ("0.4.0", "^0.3.1", False),
        ("1.5.9", "~1.5.0", True),
        ("1.6.0", "~1.5.0", False),
        ("1.7.0", ">=1.5.0 <2.0.0", True),
        ("2.0.0", ">=1.5.0 <2.0.0", False),
        ("2.1.0", "^1.5.0 || ^2.0.0", True),
        ("1.8.0", "1.x", True),
        ("2.0.0", "1.x", False),
        ("1.5.0", "1.5.0", True),
        ("1.5.1", "1.5.0", False),
        ("3.0.0", "*", True),
        ("1.5.0", "workspace:^", None),
        ("1.5.0", "1.0.0 - 2.0.0", None),
    ],
)
def test_the_range_reader_follows_npm(tmp_path, version, spec, verdict):
    assert _lab(tmp_path, [], [])["semver_satisfies"](version, spec) is verdict


@pytest.mark.parametrize(
    ("value", "says"),
    [
        ("missing", "is not a directory of the checkout"),
        ("../outside", "is not a directory of the checkout"),
        ("empty", "holds no @filigran/chatbot package"),
    ],
)
def test_a_wrong_path_stops_the_lab_with_the_reason(tmp_path, value, says):
    (tmp_path / "empty").mkdir()
    (tmp_path.parent / "outside").mkdir(exist_ok=True)
    with pytest.raises(SystemExit, match=says):
        _lab(tmp_path, [], [])["chatbot_source"](value)


def test_the_declaration_is_the_default_lab_with_the_chatbot_switch():
    services = tomllib.loads(CHECKS.read_text(encoding="utf-8"))["service"]
    lab, chatbot = services["lab"], services["lab-chatbot"]
    assert chatbot["env"] == {**lab["env"], "LAB_CHATBOT": "lab-chatbot"}
    assert chatbot["needs"] == [*lab["needs"], "lab-chatbot"]
    assert "LAB_CHATBOT" not in lab["env"]
    # The build of the package runs before Vite: a longer readiness budget, nothing else differs.
    same = {k: v for k, v in chatbot.items() if k not in ("env", "description", "needs", "timeout")}
    assert same == {k: v for k, v in lab.items() if k not in ("env", "description", "needs", "timeout")}
    assert chatbot["timeout"] > lab["timeout"]


def test_the_copilot_declaration_is_lab_copilot_with_the_chatbot_switch():
    """The same branch in Copilot mode (#11300): only the mode, and the description, differ from lab-chatbot."""
    services = tomllib.loads(CHECKS.read_text(encoding="utf-8"))["service"]
    copilot, chatbot, both = services["lab-copilot"], services["lab-chatbot"], services["lab-chatbot-copilot"]
    assert both["env"] == {**copilot["env"], "LAB_CHATBOT": "lab-chatbot"}
    assert both["env"]["PLATFORM_MODE"] == "copilot"
    same = {k: v for k, v in both.items() if k not in ("env", "description")}
    assert same == {k: v for k, v in chatbot.items() if k not in ("env", "description")}
