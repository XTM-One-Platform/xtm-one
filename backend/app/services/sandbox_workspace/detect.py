# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Zero-configuration checks: what a checkout can run, read from its own files.

A repository rarely declares its checks for an agent, but it already says how
it is installed, linted, type-checked, tested and built: in its own CI and in
its manifests. :func:`candidates` picks, out of a checkout's listing, the few
files worth reading (bounded in number, depth and size); :func:`detect` turns
the listing and those contents into :class:`~checks.CheckSpec` rows the
``sandbox_run_command`` runner already knows, named ``<stack>-<kind>``
(``node-unit``, ``python-lint``) and prefixed with their directory below the
checkout root (``front:node-unit``), and lists the service infrastructure the
repository's CI and compose files start (:class:`Infra`). Sources, strongest
first, the first one to name a (directory, stack, kind) wins:

1. the repository's workflows (``.github/workflows/*.yml``): every ``run:``
   line that installs, lints, type-checks, tests or builds, in its
   ``working-directory``: what CI runs is the source of truth; then the
   CircleCI configuration (``.circleci/config.yml``), every job's ``run``
   steps read the same way at the checkout root its ``working_directory`` names;
2. the manifests: ``package.json`` scripts with the package manager its lock
   file or ``packageManager`` names; ``pyproject.toml``, ``uv.lock``,
   ``poetry.lock``, ``requirements*.txt`` and the tools their configuration
   names; ``pom.xml`` and the Maven wrapper; ``build.gradle(.kts)`` and the
   Gradle wrapper; ``Cargo.toml``; ``dbt_project.yml``;
3. ``Makefile`` targets that carry a check's name.

``.xtm-one/checks.toml`` stays an override: a checkout that carries it runs what
it declares and nothing is detected (``commands.resolve``). Pure functions: no
sandbox, no store. No product or repository is known here (invariant 18): the
build tools named are the public conventions of the manifests themselves, the
way a person reading an unknown repository finds its commands.
"""

from __future__ import annotations

import fnmatch
import functools
import json
import posixpath
import re
import shlex
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from app.services.sandbox_workspace import actions_lint
from app.services.sandbox_workspace.checks import CHECKS_FILE, MAX_TIMEOUT, CheckSpec

#: Files read at most, and bytes per file: a listing names thousands of files, detection reads a handful.
MAX_CANDIDATES = 300
MAX_FILE_BYTES = 1024 * 1024
#: Directory depth (below the checkout root) a manifest is looked for at.
MAX_DEPTH = 4
#: Checks and infrastructure rows a detection returns at most, and workflow lines it reads.
MAX_CHECKS = 120
MAX_INFRA = 40
MAX_RUN_LINES = 600
#: Directories never looked into: what a build or an install writes.
SKIP_DIRS = frozenset(
    {"node_modules", ".venv", "venv", ".tox", "target", "dist", "build", ".git", ".xtm", "__pycache__", "vendor", ".yarn"}
)
WORKFLOWS_DIR = ".github/workflows"
#: A CircleCI configuration: its jobs' ``run`` steps are read like a workflow's (a repository CI'd on CircleCI).
CIRCLECI_CONFIGS = frozenset({".circleci/config.yml", ".circleci/config.yaml"})
#: Manifests whose content is read (the others only count by their presence in the listing).
CONTENT_FILES = frozenset(
    {
        "package.json",
        "pyproject.toml",
        "setup.cfg",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "Cargo.toml",
        "dbt_project.yml",
        "Makefile",
        "makefile",
        "GNUmakefile",
        ".python-version",
    }
)
_COMPOSE = re.compile(r"^(docker-)?compose([.-][\w.-]+)?\.ya?ml$")
_REQUIREMENTS = re.compile(r"^((test|dev)[-_])?requirements([-_][\w.-]+)?\.txt$")
#: A project's test requirements: the unit a per-project test script runs (``bash run_test.sh <file>``).
_TEST_REQUIREMENTS = re.compile(r"^test[-_]requirements\.txt$")
#: A repository's test script, kept beside a narrow checkout when it lies above a sub_path.
_TEST_SCRIPT_NAME = re.compile(r"^(run[-_])?tests?([-_][\w.-]+)?\.sh$")
#: A workflow line calling a repository shell script on one variable (``bash run_test.sh "$req_file"``).
_SCRIPT_CALL = re.compile(r"^(?:bash|sh)\s+(?:\./)?(?P<script>[\w./-]+\.sh)\s+\"?\$\{?\w+\}?\"?\s*$")
#: A requirement installed from a directory of a repository (``git+https://...#subdirectory=connectors-sdk``).
_SUBDIRECTORY = re.compile(r"#subdirectory=([\w][\w./-]*)")
#: Directories a narrow checkout adds for its requirements' ``#subdirectory=`` at most, and bytes read per file.
MAX_DEPENDENCY_DIRS = 10
MAX_REQUIREMENTS_BYTES = 256 * 1024
#: What a narrow checkout keeps above its sub_paths (:func:`companions`): the manifests detection
#: reads, the lock files and wrappers an install needs, the tool configuration a check names.
COMPANION_FILES = CONTENT_FILES | frozenset(
    {
        "setup.py",
        "uv.lock",
        "poetry.lock",
        ".python-version",
        "ruff.toml",
        ".ruff.toml",
        "mypy.ini",
        ".mypy.ini",
        "pytest.ini",
        "conftest.py",
        ".flake8",
        "tox.ini",
        "yarn.lock",
        ".yarnrc.yml",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "package-lock.json",
        ".npmrc",
        ".nvmrc",
        ".node-version",
        "tsconfig.json",
        "settings.gradle",
        "settings.gradle.kts",
        "gradlew",
        "mvnw",
        "Cargo.lock",
        "packages.yml",
        "dependencies.yml",
        ".sqlfluff",
        # The checkout's change set leaves out what it excludes (a build's output, #10650).
        ".gitignore",
    }
)
#: The tool configuration a check of a directory reads from the directories above it: formatters,
#: linters, TypeScript bases and monorepo task runners (``.prettierrc``, ``eslint.config.mjs``,
#: ``tsconfig.base.json``, ``biome.json``, ``.editorconfig``...). A narrow checkout without them
#: reports a whole tree as unformatted or fails to resolve its compiler options.
_TOOL_CONFIG = re.compile(
    r"^(\.prettierrc(\.[\w.]+)?|prettier\.config\.\w+|\.prettierignore|biome\.jsonc?|\.editorconfig"
    r"|\.oxfmtrc(\.[\w.]+)?|\.oxlintrc(\.[\w.]+)?|\.eslintrc(\.[\w.]+)?|eslint\.config\.\w+|\.eslintignore"
    r"|tsconfig(\.[\w-]+)*\.json|jsconfig\.json|turbo\.json|nx\.json|lerna\.json|babel\.config\.\w+|\.babelrc"
    r"|\.browserslistrc|\.stylelintrc(\.[\w.]+)?|stylelint\.config\.\w+"
    # Test runner configurations a unit check of a sub_path loads from its package root (vitest.config.ts,
    # vitest.config.ci-unit.ts, vite.config.ts, jest.config.js, playwright.config.ts).
    r"|(vite|vitest|jest|playwright)\.config(\.[\w-]+)*\.(ts|mts|cts|js|mjs|cjs|json)|vitest\.workspace\.\w+"
    # Python formatters and linters that read their own file, not pyproject.toml (isort, pylint, pre-commit).
    r"|\.isort\.cfg|\.?pylintrc|\.pre-commit-config\.ya?ml|\.coveragerc|\.yamllint(\.ya?ml)?"
    r"|\.markdownlint(\.[\w.]+)?|\.markdownlintignore|\.sqlfluffignore"
    # Secret scanners read their rules and allow-list at the root they scan from (betterleaks, gitleaks): a
    # narrow checkout without them reports the allow-listed test fixtures as leaks.
    r"|\.(better|git)leaks\.toml|\.(better|git)leaksignore)$"
)
#: Companion files a narrow checkout keeps at most, and bytes per companion (a lock file runs to megabytes).
MAX_COMPANIONS = 200
MAX_COMPANION_BYTES = 16 * 1024 * 1024
_MAKE_TARGET = re.compile(r"^([A-Za-z0-9][\w.-]*)\s*:(?![=:])")
_SHELL_CHARS = frozenset("|&;<>$`*(){}")
#: Order of the kinds in a listing, and the timeout each gets.
KINDS = ("install", "lint", "format", "types", "parse", "unit", "test", "build")
_TIMEOUTS = {
    "install": 900,
    "lint": 900,
    "format": 600,
    "types": 900,
    "parse": 600,
    "unit": 1800,
    "test": 1800,
    "build": 1800,
}
#: How a per-project test script runs (``<dir>:python-test``): ``$1`` the script, ``$2`` the
#: project's test requirements. Such scripts compare the branch with its base (``git merge-base
#: origin/master HEAD``, ``git diff``) and a local package build lists files through git, while a
#: checkout has no ``.git``: the run makes a throwaway repository, a base commit of everything but
#: the project under ``origin/main`` and ``origin/master`` with the whole checkout committed on top,
#: so the project, and only it, counts as changed and the script runs its tests (an empty base
#: would make every file a change, the shared package's diff included, megabytes of output). The
#: JUnit and coverage reports the run wrote are copied to ``.xtm/`` (the JUnit one is parsed into
#: findings), then what the run created and the throwaway repository are removed, so a commit from
#: the checkout never carries them. A checkout that holds a ``.git`` of its own is run as it is.
SCRIPT_TEST_SHELL = """set -u
script="$1"; requirements="$2"; project=$(dirname "$requirements")
mkdir -p .xtm && rm -f .xtm/junit.xml .xtm/coverage.xml
if [ -e .git ]; then bash "$script" "$requirements"; exit $?; fi
trap 'rm -rf .git' EXIT
g() { git -c user.name=xtm-one -c user.email=xtm-one@localhost -c init.defaultBranch=main "$@"; }
g init -q && g add -A -f -- . ':!.xtm' ":!$project" && g commit -q --allow-empty -m base \\
  && g update-ref refs/remotes/origin/main HEAD && g update-ref refs/remotes/origin/master HEAD \\
  && g add -A -f -- . ':!.xtm' && g commit -q -m checkout || exit 1
python -c "$3" "$project" || exit 1
bash "$script" "$requirements"
status=$?
g checkout -q HEAD -- "$(dirname "$project")"
created=$(g ls-files --others -- . ':!.xtm')
junit=$(printf '%s\\n' "$created" | grep -E '(^|/)junit[^/]*[.]xml$' | head -n 1)
coverage=$(printf '%s\\n' "$created" | grep -E '(^|/)coverage[.]xml$' | head -n 1)
[ -n "$junit" ] && cp "$junit" .xtm/junit.xml
[ -n "$coverage" ] && cp "$coverage" .xtm/coverage.xml
g clean -fdxq -e .xtm
exit $status
"""
#: Run before the script by :data:`SCRIPT_TEST_SHELL` (``$3``, the project directory its argument): a
#: requirement that installs a package of the same repository from its remote branch (``sdk @
#: git+https://.../r.git@master#subdirectory=sdk``) is pointed at that directory of the checkout
#: (``sdk @ file:///workspace/.../sdk``), as CI installs the branch's own copy: the remote copy
#: pins what the branch may have moved, and resolution fails. The requirements files of the project
#: (the directory above its test requirements) are rewritten in place for the run and restored from
#: the throwaway repository after it; a directory the checkout does not hold is left as written.
LOCAL_REQUIREMENTS_SCRIPT = r'''import pathlib, re, sys
root = pathlib.Path(sys.argv[1]).parent
line = re.compile(r"^(\s*[A-Za-z0-9][\w.-]*(?:\[[^\]]*\])?)\s*@\s*git\+\S*#subdirectory=([\w][\w./-]*)\S*", re.M)
def local(m):
    target = pathlib.Path(m.group(2))
    return f"{m.group(1)} @ {target.resolve().as_uri()}" if target.is_dir() else m.group(0)
for path in sorted(root.rglob("*requirements*.txt")):
    if ".venv" in path.parts or ".temp_venv" in path.parts:
        continue
    text = path.read_text(encoding="utf-8", errors="replace")
    new = line.sub(local, text)
    if new != text:
        path.write_text(new, encoding="utf-8")
        print(f"xtm-one: {path} installs the checkout's own packages")
'''
#: The Python interpreters (``major.minor``) an image key carries, the default first: a check of a
#: project that declares another one (:func:`_python_version`) runs on it when the image has it.
IMAGE_PYTHONS: dict[str, tuple[str, ...]] = {"toolchain-python": ("3.14", "3.12"), "toolchain-dbt": ("3.14", "3.12")}
#: The ``actions-lint`` check's script (:mod:`actions_lint`), run inline with ``python -c``: the
#: checkout under test does not hold the platform's sources, and the script needs only the stdlib.
ACTIONS_LINT_SCRIPT = Path(actions_lint.__file__).read_text(encoding="utf-8")
#: Image keys a stack runs on, preferred first: the first one ``OPENSANDBOX_IMAGES`` configures is used.
#: The generic toolchain images (``Dockerfile.sandbox.toolchain``, no lock baked) come first: a
#: detected checkout installs its own dependencies; the ``-dev`` images bake one repository's locks.
STACK_IMAGES: dict[str, tuple[str, ...]] = {
    "node": ("toolchain-node", "nodejs-dev", "nodejs"),
    "python": ("toolchain-python", "python-dev", "python"),
    "jvm": ("toolchain-jvm",),
    "rust": ("toolchain-rust",),
    "dbt": ("toolchain-dbt", "toolchain-python"),
    # helm is a binary of every toolchain image (Dockerfile.sandbox.toolchain, toolchain-base), never of a -dev one.
    "helm": ("toolchain-python", "toolchain-node", "toolchain-jvm", "toolchain-rust", "toolchain-dbt"),
}
#: ``package.json`` script names per kind, preferred first.
_NODE_SCRIPTS = (
    ("lint", ("lint", "eslint", "lint:check")),
    ("format", ("format:check", "prettier:check", "check-format")),
    ("types", ("typecheck", "type-check", "check-ts", "check:types", "types", "tsc")),
    ("unit", ("test:unit", "unit", "test")),
    ("build", ("build",)),
)
#: ``package.json`` scripts offered as checks of their own (``node-<script>``) whatever the workflows
#: call, at the lock and in every workspace member: a namespaced script (``a:b``) one of whose
#: segments is a generator verb (``generate:icons``, ``i18n:extract``) or a gate verb
#: (``check:bundle-size``, ``i18n:check``) is otherwise met in CI only. The words of
#: :data:`_SCRIPT_SKIP` leave a script out.
_NODE_SCRIPT_VERBS = frozenset({"generate", "extract", "compile", "check", "test", "validate", "verify"})
#: A hyphenated script name (``i18n-checker``, ``generate-i18n-checker-constant``, ``check-licenses``)
#: is read the same way, its segments split on ``-`` too, a gate noun counting as its verb.
_NODE_SCRIPT_NOUNS = frozenset({"checker", "validator", "verifier"})
_NODE_SCRIPT_SPLIT = re.compile(r"[:-]")
#: A script that reaches a URL (``swagger-typescript-api --path=http://localhost:8080``) needs a
#: service the sandbox does not run: it is not offered.
_NODE_SCRIPT_URL = re.compile(r"https?://")
#: A script whose body runs a code generator (``"relay": "relay-compiler relay.config.json"``,
#: ``"build:types": "graphql-codegen ..."``) is a check whatever its name: a narrow checkout carries
#: no generated sources (``src/generated``, ``__generated__``), so the types or unit check needs it first.
_NODE_CODEGEN = re.compile(
    r"(?:^|[\s;&|(])(?:graphql-codegen|gql-gen|relay-compiler|openapi-typescript|orval|prisma\s+generate|buf\s+generate)(?=\s|$|[;&|)])"
)
#: A unit script that is a Node wrapper of its own (``node test/run.mjs``), and the loader files beside
#: it the Node test runner is given with ``--import`` by the ``node-unit-file`` check.
_NODE_WRAPPER = re.compile(r"^(?:\./)?[\w./-]+\.m?js$")
_NODE_TEST_LOADERS = ("register.mjs", "register.js", "setup.mjs", "setup.js")
_SCRIPT_SKIP = ("e2e", "playwright", "cypress", "watch", "deploy", "release", "publish")
#: pnpm 11+ fails an install whose dependencies' build scripts are not approved
#: (``ERR_PNPM_IGNORED_BUILDS``) once every package is linked; a CI runner reads it as a warning.
_PNPM_LENIENT_BUILDS = "--config.strict-dep-builds=false"
#: ``Makefile`` targets taken as checks, with their kind.
_MAKE_KINDS = {
    "install": "install",
    "deps": "install",
    "lint": "lint",
    "format-check": "format",
    "fmt-check": "format",
    "typecheck": "types",
    "types": "types",
    "test": "unit",
    "tests": "unit",
    "unit": "unit",
    "build": "build",
}


@dataclass(frozen=True)
class Infra:
    """A service the repository's CI or compose file starts beside the code (a database, a broker)."""

    name: str
    image: str
    source: str


@dataclass
class Detection:
    checks: dict[str, CheckSpec] = field(default_factory=dict)
    infra: list[Infra] = field(default_factory=list)


def _skipped(path: str) -> bool:
    return any(part in SKIP_DIRS for part in path.split("/")[:-1])


def _is_workflow(path: str) -> bool:
    return posixpath.dirname(path) == WORKFLOWS_DIR and path.endswith((".yml", ".yaml"))


def _is_ci_config(path: str) -> bool:
    """Whether *path* is a CI definition detection reads: a GitHub Actions workflow or the CircleCI configuration."""
    return _is_workflow(path) or path in CIRCLECI_CONFIGS


def candidates(paths: Iterable[str]) -> list[str]:
    """The paths of a checkout listing (relative to its root) whose content :func:`detect` reads, bounded."""
    picked = []
    for path in paths:
        name = posixpath.basename(path)
        manifest = (name in CONTENT_FILES or _COMPOSE.match(name)) and path.count("/") <= MAX_DEPTH and not _skipped(path)
        if _is_ci_config(path) or manifest:
            picked.append(path)
    picked.sort(key=lambda p: (not _is_ci_config(p), p.count("/"), p))
    return picked[:MAX_CANDIDATES]


def _companion_name(name: str) -> bool:
    return name in COMPANION_FILES or bool(
        _COMPOSE.match(name) or _REQUIREMENTS.match(name) or _TEST_SCRIPT_NAME.match(name) or _TOOL_CONFIG.match(name)
    )


def is_companion_name(name: str) -> bool:
    """Whether a file named *name* (a manifest, lock file, compose file, requirements file, test script or tool
    configuration) is one :func:`companions` keeps in a directory above a sub_path."""
    return _companion_name(name)


#: The files whose relative path literals :func:`referenced_files` follows: the tests and the
#: tool configuration of a JavaScript or TypeScript project (``tsconfig.json`` ``extends``, the
#: ``relay.config.json`` ``schema`` the Relay compiler and its ESLint plugin read).
_REFERENCING_CONFIG = re.compile(
    r"^((tsconfig[\w.-]*|jsconfig)\.json|(vite|vitest)\.config(\.[\w-]+)*\.\w+|relay\.config\.(json|js|cjs|mjs|ts))$"
)
_REFERENCING_FILE = re.compile(r"\.(test|spec)\.(ts|tsx|mts|js|jsx|mjs|cjs)$")
_PATH_LITERAL = re.compile(r"[\"'`](\.{1,2}/[^\"'`\s$]*)[\"'`]")
_RESOLVE_SUFFIXES = ("", ".ts", ".tsx", ".js", ".mjs", ".json", "/index.ts", "/index.tsx", "/index.js")
#: A directory a test reads whole (``readdirSync``) is kept when it holds at most this many files and bytes.
MAX_REFERENCED_DIR_FILES = 50
MAX_REFERENCED_DIR_BYTES = 2 * 1024 * 1024


def is_workflow(path: str) -> bool:
    """Whether *path* is a GitHub Actions workflow of the repository (``.github/workflows/*.yml``)."""
    return _is_workflow(path)


def is_referencing_file(path: str) -> bool:
    """Whether :func:`referenced_files` reads *path* (a test file or a TypeScript, Vite or Relay configuration)."""
    return is_referencing_config(path) or bool(_REFERENCING_FILE.search(posixpath.basename(path)))


def is_referencing_config(path: str) -> bool:
    """Whether *path* is a tool configuration :func:`referenced_files` reads (not a test): it is read in a
    directory above a sub_path too, where the checks of the sub_path run from (``relay.config.json`` of a
    frontend whose ``src/schema`` the sub_path leaves out)."""
    return bool(_REFERENCING_CONFIG.match(posixpath.basename(path)))


def referenced_files(sources: Mapping[str, bytes], sizes: Mapping[str, int], sub_paths: Iterable[str]) -> list[str]:
    """The repository files outside *sub_paths* that the tests and configs under them name by a relative path.

    A frontend test reads a backend fixture (``../../../../backend/tests/fixtures/x.json``),
    an Office add-in test imports the web app's constants
    (``../../../../frontend/src/lib/constants.ts?raw``), a ``tsconfig.json``
    extends a base two levels up: a checkout of the test's directory alone
    fails on a missing file that CI has. *sources* maps the referencing files
    under the sub_paths (:func:`is_referencing_file`) to their content,
    *sizes* every regular file of the repository. Each quoted literal that
    starts with ``./`` or ``../`` (no ``${``) is resolved from the file's
    directory and from the sub_path that holds it (a test run from its
    project root that reads ``../backend/x``), with the module suffixes an
    import omits; a regular file outside the sub_paths is kept, and a
    directory of at most :data:`MAX_REFERENCED_DIR_FILES` files and
    :data:`MAX_REFERENCED_DIR_BYTES` gives its direct files. Paths out of the
    repository, under a sub_path or under a directory a build or an install
    writes add nothing; :data:`MAX_COMPANIONS` at most, sorted.
    """
    wanted = [s.strip("/") for s in sub_paths if s and s.strip("/")]

    def kept(path: str) -> bool:
        return any(path == s or path.startswith(s + "/") for s in wanted)

    by_dir: dict[str, list[str]] = {}
    for path in sizes:
        by_dir.setdefault(posixpath.dirname(path), []).append(path)
    found: set[str] = set()
    for source, data in sorted(sources.items()):
        bases = [posixpath.dirname(source), *(s for s in wanted if source.startswith(s + "/"))]
        for match in _PATH_LITERAL.finditer(_text(data[:MAX_REQUIREMENTS_BYTES])):
            literal = match.group(1).split("?", 1)[0].split("#", 1)[0].rstrip("/")
            for base in dict.fromkeys(bases):
                target = _script_path(base, literal)
                if not target or _skipped(target + "/x"):
                    continue
                hit = next((target + s for s in _RESOLVE_SUFFIXES if target + s in sizes), None)
                members = [] if hit is not None else by_dir.get(target, [])
                if hit is None and not members:
                    continue
                # The first base that names an existing file or directory wins, kept or not.
                if hit is not None:
                    if sizes[hit] <= MAX_COMPANION_BYTES and not kept(hit):
                        found.add(hit)
                elif (
                    len(members) <= MAX_REFERENCED_DIR_FILES
                    and sum(sizes[m] for m in members) <= MAX_REFERENCED_DIR_BYTES
                ):
                    found.update(m for m in members if not kept(m) and sizes[m] <= MAX_COMPANION_BYTES)
                break
    return sorted(found)[:MAX_COMPANIONS]


def is_requirements(path: str) -> bool:
    """Whether *path* names a pip requirements file (``requirements.txt``, ``test-requirements.txt``...)."""
    return bool(_REQUIREMENTS.match(posixpath.basename(path)))


def subdirectory_dependencies(
    requirements: Mapping[str, bytes], directories: Iterable[str], sub_paths: Iterable[str]
) -> list[str]:
    """The repository directories the *requirements* install from (``#subdirectory=``), outside *sub_paths*.

    A project of a monorepo may depend on a package of the same repository
    (``connectors-sdk @ git+https://...connectors.git@master#subdirectory=connectors-sdk``),
    which its test script installs from the checkout (``uv pip install
    ./connectors-sdk``): a narrow checkout of the project keeps that
    directory too. *requirements* maps the requirements files under the
    sub_paths to their content, *directories* lists the repository's
    directories; a name that is not one of them, or lies under a sub_path,
    adds nothing. :data:`MAX_DEPENDENCY_DIRS` at most, sorted.
    """
    known = set(directories)
    wanted = [s.strip("/") for s in sub_paths if s and s.strip("/")]
    found: set[str] = set()
    for _path, data in sorted(requirements.items()):
        for match in _SUBDIRECTORY.finditer(_text(data[:MAX_REQUIREMENTS_BYTES])):
            directory = posixpath.normpath(match.group(1)).strip("/")
            if directory in known and not any(directory == s or directory.startswith(s + "/") for s in wanted):
                found.add(directory)
    return sorted(found)[:MAX_DEPENDENCY_DIRS]


#: Maven reactor modules a narrow checkout adds for its sub_paths' in-repository artifacts at most.
MAX_REACTOR_MODULES = 30
_POM_NAMESPACE = re.compile(r"^\{[^}]*\}")


def _pom_tag(element: Any) -> str:
    return _POM_NAMESPACE.sub("", str(getattr(element, "tag", "")))


def _pom_child(element: Any, name: str) -> Any:
    return next((child for child in list(element) if _pom_tag(child) == name), None)


def _pom_text(element: Any, name: str) -> str:
    child = _pom_child(element, name)
    return (getattr(child, "text", None) or "").strip() if child is not None else ""


def _pom_coordinates(project: Any) -> tuple[str, str]:
    """``(groupId, artifactId)`` of a POM, the groupId inherited from its ``<parent>`` when it declares none."""
    parent = _pom_child(project, "parent")
    group = _pom_text(project, "groupId") or (_pom_text(parent, "groupId") if parent is not None else "")
    return group, _pom_text(project, "artifactId")


def _pom_references(project: Any) -> set[tuple[str, str]]:
    """Every ``(groupId, artifactId)`` a POM names: dependencies, plugins, annotation processor paths, its parent."""
    found: set[tuple[str, str]] = set()
    stack = [project]
    while stack:
        element = stack.pop()
        stack.extend(list(element))
        if _pom_tag(element) in {"dependency", "plugin", "path", "parent", "annotationProcessorPath"}:
            artifact = _pom_text(element, "artifactId")
            if artifact:
                found.add((_pom_text(element, "groupId"), artifact))
    return found


def maven_reactor_paths(poms: Mapping[str, bytes], sub_paths: Iterable[str]) -> tuple[list[str], list[str]]:
    """The Maven reactor modules and module POMs a narrow checkout of *sub_paths* needs to build.

    A module of a multi-module Maven project depends on its siblings
    (``io.x:x-client``) and its parent POM lists every module (``<module>``):
    a checkout of one module alone fails with "Child module ... does not
    exist", and with ``-pl``, it looks for the siblings in every remote
    repository the project declares, where they are not published. *poms*
    maps every ``pom.xml`` of the repository to its content. Returns (1) the
    module directories outside *sub_paths* that a POM under them names as a
    dependency, plugin or annotation processor, followed transitively,
    :data:`MAX_REACTOR_MODULES` at most, sorted; (2) the ``pom.xml`` of every
    module a POM above a sub_path lists, so the reactor reads. An
    unparseable POM adds nothing.
    """
    from defusedxml.ElementTree import ParseError, fromstring  # type: ignore[import-untyped]

    wanted = [s.strip("/") for s in sub_paths if s and s.strip("/")]
    if not wanted:
        return [], []
    projects: dict[str, Any] = {}
    for path, data in poms.items():
        if posixpath.basename(path) != "pom.xml":
            continue
        try:
            projects[posixpath.dirname(path)] = fromstring(_text(data[:MAX_REQUIREMENTS_BYTES]))
        except (ParseError, ValueError):
            continue
    by_artifact: dict[tuple[str, str], str] = {}
    by_name: dict[str, str] = {}
    for directory, project in projects.items():
        group, artifact = _pom_coordinates(project)
        if artifact:
            by_artifact[(group, artifact)] = directory
            by_name.setdefault(artifact, directory)

    def inside(directory: str) -> bool:
        return any(directory == s or directory.startswith(s + "/") for s in wanted)

    added: list[str] = []
    queue = [d for d in projects if inside(d)]
    seen = set(queue)
    while queue and len(added) < MAX_REACTOR_MODULES:
        for group, artifact in sorted(_pom_references(projects[queue.pop(0)])):
            # A groupId left to a property (``${project.groupId}``) or inherited is matched by artifactId.
            resolved = group and "${" not in group
            target = by_artifact.get((group, artifact)) if resolved else by_name.get(artifact)
            if target is None or target in seen or not target:
                continue
            seen.add(target)
            queue.append(target)
            if not inside(target) and not any(target.startswith(a + "/") for a in added):
                added.append(target)
    above = {a for sub in wanted for a in _ancestors(sub)}
    module_poms: list[str] = []
    for directory in sorted(above & projects.keys()):
        modules = _pom_child(projects[directory], "modules")
        for module in list(modules) if modules is not None else []:
            name = (getattr(module, "text", None) or "").strip().strip("/")
            child = posixpath.normpath(_join(directory, name)) if name else ""
            pom = _join(child, "pom.xml") if not child.endswith(".xml") else child
            if child and not child.startswith("..") and pom in poms and pom not in module_poms:
                module_poms.append(pom)
    return sorted(added)[:MAX_REACTOR_MODULES], module_poms


#: Workspace members a narrow checkout keeps the manifest of at most (:func:`workspace_member_manifests`).
MAX_WORKSPACE_MEMBERS = 200
#: The manifests that declare a JavaScript workspaces root: ``package.json`` ``workspaces``, ``pnpm-workspace.yaml``.
WORKSPACE_ROOT_FILES = ("package.json", "pnpm-workspace.yaml")


def _glob_segments(pattern: str, parts: list[str]) -> bool:
    """Whether the directory *parts* match the workspace glob split in segments (``**`` spans any number)."""
    segments = [s for s in pattern.split("/") if s and s != "."]

    def match(i: int, j: int) -> bool:
        if i == len(segments):
            return j == len(parts)
        if segments[i] == "**":
            return any(match(i + 1, k) for k in range(j, len(parts) + 1))
        return j < len(parts) and fnmatch.fnmatchcase(parts[j], segments[i]) and match(i + 1, j + 1)

    return match(0, 0)


def _workspace_globs(name: str, data: bytes) -> list[str]:
    """The member globs a ``package.json`` (``workspaces``, a list or ``{packages: [...]}``) or a ``pnpm-workspace.yaml`` declares."""
    if name == "pnpm-workspace.yaml":
        value: object = _yaml(data).get("packages")
    else:
        value = _json(data).get("workspaces")
        if isinstance(value, dict):
            value = value.get("packages")
    return [g.strip() for g in value if isinstance(g, str) and g.strip()] if isinstance(value, list) else []


def workspace_member_manifests(
    roots: Mapping[str, bytes], sizes: Mapping[str, int], sub_paths: Iterable[str]
) -> list[str]:
    """The ``package.json`` of every member of the JavaScript workspaces a narrow checkout sits in.

    An install of a workspaces root (``yarn install --immutable``, ``pnpm
    install --frozen-lockfile``, ``npm ci``) resolves every member the root's
    globs match against the lock file: a checkout of one member alone makes
    it rewrite the lock (yarn YN0028) and fail. *roots* maps the
    ``package.json`` and ``pnpm-workspace.yaml`` files of the directories
    above a sub_path to their content, *sizes* every regular file of the
    repository. The manifests of the members their globs match (``!``
    patterns exclude), outside the sub_paths and the directories a build or
    an install writes; :data:`MAX_WORKSPACE_MEMBERS` at most, sorted.
    """
    wanted = [s.strip("/") for s in sub_paths if s and s.strip("/")]
    found: set[str] = set()
    for path, data in sorted(roots.items()):
        globs = _workspace_globs(posixpath.basename(path), data)
        if not globs:
            continue
        root = posixpath.dirname(path)
        include = [g for g in globs if not g.startswith("!")]
        exclude = [g[1:] for g in globs if g.startswith("!")]
        for candidate in sizes:
            if posixpath.basename(candidate) != "package.json" or _skipped(candidate):
                continue
            member = posixpath.dirname(candidate)
            if root and not member.startswith(root + "/"):
                continue
            if member == root or any(candidate.startswith(sub + "/") for sub in wanted):
                continue
            parts = (member[len(root) + 1 :] if root else member).split("/")
            if any(_glob_segments(g, parts) for g in include) and not any(_glob_segments(g, parts) for g in exclude):
                found.add(candidate)
    return sorted(found)[:MAX_WORKSPACE_MEMBERS]


#: The ``package.json`` scripts an install runs by itself (npm, pnpm and yarn run them on ``install``).
INSTALL_LIFECYCLE_SCRIPTS = ("preinstall", "install", "postinstall", "preprepare", "prepare", "postprepare")
_SCRIPT_SEPARATOR = re.compile(r"&&|\|\||;")
_SCRIPT_CD = re.compile(r"^\s*cd\s+(['\"]?)([^\s'\"]+)\1\s*$")
_SCRIPT_PATH = re.compile(r"[A-Za-z0-9_.@/-]+")


def _script_path(cwd: str, token: str) -> str | None:
    """*token* resolved from the repository directory *cwd*, ``""`` for the root, ``None`` out of the repository."""
    if token.startswith("/"):
        return None
    path = posixpath.normpath(posixpath.join(cwd, token) if cwd else token)
    if path == ".":
        return ""
    return None if path == ".." or path.startswith("../") else path


def install_lifecycle_paths(
    roots: Mapping[str, bytes], sizes: Mapping[str, int], sub_paths: Iterable[str]
) -> tuple[list[str], list[str]]:
    """The directories and files the install lifecycle scripts of a narrow checkout's workspace roots use.

    An install runs the ``prepare`` (and ``preinstall``, ``postinstall``...)
    script of the root it installs: a JavaScript workspaces root may build a
    sibling member there (``cd packages/core && tsup``) or copy a root file
    (``../../llms.txt``), so a checkout of another member alone fails the
    install. *roots* maps the ``package.json`` files of the directories above
    a sub_path to their content, *sizes* every regular file of the
    repository. A ``cd`` into a repository directory adds that directory; a
    word of the script that, resolved from the directory the script is in at
    that point, names a regular file of the repository adds that file. The
    root, the sub_paths, what lies under them and the directories a build or
    an install writes add nothing. Returns (directories, files), sorted,
    :data:`MAX_DEPENDENCY_DIRS` and :data:`MAX_COMPANIONS` at most.
    """
    wanted = [s.strip("/") for s in sub_paths if s and s.strip("/")]
    directories = {d for path in sizes for d in _ancestors(posixpath.dirname(path)) if d}

    def kept(path: str) -> bool:
        return any(path == s or path.startswith(s + "/") for s in wanted)

    found_dirs: set[str] = set()
    found_files: set[str] = set()
    for path, data in sorted(roots.items()):
        if posixpath.basename(path) != "package.json":
            continue
        scripts = _json(data).get("scripts")
        if not isinstance(scripts, dict):
            continue
        for name in INSTALL_LIFECYCLE_SCRIPTS:
            script = scripts.get(name)
            if not isinstance(script, str):
                continue
            cwd: str | None = posixpath.dirname(path)
            for segment in _SCRIPT_SEPARATOR.split(script[:MAX_REQUIREMENTS_BYTES]):
                cd = _SCRIPT_CD.match(segment)
                if cd:
                    cwd = _script_path(cwd, cd.group(2)) if cwd is not None else None
                    if cwd and cwd in directories and not kept(cwd) and not _skipped(cwd + "/x"):
                        found_dirs.add(cwd)
                    continue
                if cwd is None:
                    continue
                for token in _SCRIPT_PATH.findall(segment):
                    if token.startswith("-") or ("/" not in token and "." not in token):
                        continue
                    target = _script_path(cwd, token)
                    if (
                        target
                        and target in sizes
                        and sizes[target] <= MAX_COMPANION_BYTES
                        and not kept(target)
                        and not _skipped(target)
                    ):
                        found_files.add(target)
    dirs = sorted(d for d in found_dirs if not any(d != o and d.startswith(o + "/") for o in found_dirs))[
        :MAX_DEPENDENCY_DIRS
    ]
    files = sorted(f for f in found_files if not any(f.startswith(d + "/") for d in dirs))[:MAX_COMPANIONS]
    return dirs, files


#: The dependency fields a ``workspace:`` protocol value links a sibling workspace member from.
_WORKSPACE_LINK_FIELDS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")


def workspace_dependency_dirs(manifests: Mapping[str, bytes], sub_paths: Iterable[str]) -> list[str]:
    """The workspace member directories a narrow checkout's packages link by the ``workspace:`` protocol.

    ``'eslint-plugin-custom-rules': 'workspace:*'`` in a member's ``package.json``
    makes its install link the sibling member of that name and its checks
    import it (an ``eslint.config.ts`` loading the plugin): a checkout of the
    member alone keeps the sibling's manifest but not its source, and the
    check fails on a missing module. *manifests* maps every ``package.json``
    of the repository (outside ``node_modules``) to its content. A member
    under a sub_path, or the member a sub_path lies in (the nearest
    ``package.json`` above it, never the repository root: a checkout of
    ``openaev-front/src/utils`` lints with ``openaev-front``'s config), that
    depends on a ``workspace:`` package adds the directory of the manifest
    whose ``name`` it is, then that member's own ``workspace:`` dependencies,
    transitively. The root, the sub_paths, what lies under them, a directory
    holding a sub_path and the directories a build or an install writes add
    nothing; :data:`MAX_DEPENDENCY_DIRS` at most, sorted.
    """
    wanted = [s.strip("/") for s in sub_paths if s and s.strip("/")]

    def kept(path: str) -> bool:
        return any(path == s or path.startswith(s + "/") for s in wanted)

    def holds_sub_path(directory: str) -> bool:
        return any(s.startswith(directory + "/") for s in wanted)

    docs = {path: _json(data) for path, data in manifests.items() if posixpath.basename(path) == "package.json"}
    by_name: dict[str, str] = {}
    for path, doc in sorted(docs.items()):
        name = doc.get("name")
        directory = posixpath.dirname(path)
        if isinstance(name, str) and directory and not _skipped(path) and name not in by_name:
            by_name[name] = directory
    found: set[str] = set()
    owners: set[str] = set()
    for sub_path in wanted:
        directory = posixpath.dirname(sub_path)
        while directory:
            manifest = directory + "/package.json"
            if manifest in docs and not _skipped(manifest):
                owners.add(manifest)
                break
            directory = posixpath.dirname(directory)
    queue = sorted({*(path for path in docs if kept(path)), *owners})
    seen: set[str] = set()
    while queue and len(found) < MAX_DEPENDENCY_DIRS:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        doc = docs.get(path, {})
        for field_name in _WORKSPACE_LINK_FIELDS:
            deps = doc.get(field_name)
            for name, value in sorted(deps.items()) if isinstance(deps, dict) else ():
                if not (isinstance(value, str) and value.strip().startswith("workspace:")):
                    continue
                directory = by_name.get(name)
                if not directory or kept(directory) or holds_sub_path(directory) or directory in found:
                    continue
                found.add(directory)
                queue.append(directory + "/package.json")
    return sorted(d for d in found if not any(d != o and d.startswith(o + "/") for o in found))[:MAX_DEPENDENCY_DIRS]


#: An ESLint flat configuration, a workspace's own included (``eslint.config.ts``, ``local.eslint.config.ts``).
_LINT_CONFIG = re.compile(r"^([\w-]+\.)*eslint\.config\.(js|cjs|mjs|ts|cts|mts)$")
#: A bare module specifier a configuration imports (``from 'eslint-rules/src/rules'``, ``import('x')``, ``require('x')``).
_BARE_IMPORT = re.compile(
    r"(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+|\brequire\s*\(\s*)[\"'`]((?:@[\w.-]+/)?[\w][\w.-]*)(?:/[^\"'`]*)?[\"'`]"
)


def is_lint_config(path: str) -> bool:
    """Whether *path* is an ESLint flat configuration whose imports :func:`lint_config_member_dirs` follows."""
    return bool(_LINT_CONFIG.match(posixpath.basename(path)))


def lint_config_member_dirs(
    configs: Mapping[str, bytes], manifests: Mapping[str, bytes], sub_paths: Iterable[str]
) -> list[str]:
    """The workspace member directories an ESLint configuration of a narrow checkout imports by package name.

    ``import { linguiRules } from 'eslint-rules/src/rules'`` in the root
    ``eslint.config.ts`` or a member's ``local.eslint.config.ts`` resolves to
    the sibling member named ``eslint-rules`` through the workspace link, with
    no ``workspace:`` dependency declaring it: a checkout of ``front`` keeps the
    configuration and fails on a missing module. *configs* maps the
    configurations read in or above the sub_paths (:func:`is_lint_config`) to
    their content, *manifests* every ``package.json`` of the repository. Each
    bare specifier whose package name is a member's ``name`` adds that
    member's directory when it is not the root, under a sub_path or holding
    one; :data:`MAX_DEPENDENCY_DIRS` at most, sorted.
    """
    wanted = [s.strip("/") for s in sub_paths if s and s.strip("/")]

    def kept(path: str) -> bool:
        return any(path == s or path.startswith(s + "/") for s in wanted)

    by_name: dict[str, str] = {}
    for path, data in sorted(manifests.items()):
        if posixpath.basename(path) != "package.json" or _skipped(path):
            continue
        name = _json(data).get("name")
        directory = posixpath.dirname(path)
        if isinstance(name, str) and directory and name not in by_name:
            by_name[name] = directory
    found: set[str] = set()
    for path, data in sorted(configs.items()):
        if not is_lint_config(path) or _skipped(path):
            continue
        for name in _BARE_IMPORT.findall(_text(data[:MAX_REQUIREMENTS_BYTES])):
            member_dir = by_name.get(name)
            if not member_dir or kept(member_dir) or any(s.startswith(member_dir + "/") for s in wanted):
                continue
            found.add(member_dir)
    return sorted(d for d in found if not any(d != o and d.startswith(o + "/") for o in found))[:MAX_DEPENDENCY_DIRS]


#: A Relay ``src`` larger than this is not added to a narrow checkout (OpenCTI's front is about 24 MB).
MAX_RELAY_SOURCE_BYTES = 64 * 1024 * 1024
#: The ``src`` of a ``relay.config.js`` / ``.ts`` (``src: './src'``); a JSON config is parsed.
_RELAY_JS_SRC = re.compile(r"\bsrc\s*:\s*[\"'`]([^\"'`$]+)[\"'`]")
#: The source files of an added Relay ``src`` whose relative imports outside it are kept.
RELAY_SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs")


def relay_source_dirs(configs: Mapping[str, bytes], sizes: Mapping[str, int], sub_paths: Iterable[str]) -> list[str]:
    """The Relay ``src`` directories a narrow checkout adds: the compiler reads every fragment of them.

    A checkout of ``opencti-front/src/utils/hooks`` keeps the project's
    ``relay.config.json`` and the schema it names, but the Relay compiler
    and the test transform read every ``graphql`` tag of the config's
    ``src``: a query spreading a fragment defined elsewhere fails as an
    undefined fragment. *configs* maps the configurations read in or above
    the sub_paths (:func:`is_referencing_config`) to their content, *sizes*
    every regular file of the repository. The ``src`` of each
    ``relay.config.*`` whose directory holds a sub_path is added when it is
    not under a sub_path already, lies in the repository (never its root)
    and holds at most :data:`MAX_RELAY_SOURCE_BYTES`; sorted.
    """
    wanted = [s.strip("/") for s in sub_paths if s and s.strip("/")]

    def kept(path: str) -> bool:
        return any(path == s or path.startswith(s + "/") for s in wanted)

    found: set[str] = set()
    for path, data in sorted(configs.items()):
        name = posixpath.basename(path)
        if not name.startswith("relay.config.") or _skipped(path):
            continue
        directory = posixpath.dirname(path)
        if not any(not directory or s == directory or s.startswith(directory + "/") for s in wanted):
            continue
        if name.endswith(".json"):
            src = _json(data).get("src")
        else:
            match = _RELAY_JS_SRC.search(_text(data[:MAX_REQUIREMENTS_BYTES]))
            src = match.group(1) if match else None
        target = _script_path(directory, src.strip()) if isinstance(src, str) and src.strip() else None
        if not target or kept(target) or _skipped(target + "/x"):
            continue
        total = sum(size for file, size in sizes.items() if file.startswith(target + "/"))
        if 0 < total <= MAX_RELAY_SOURCE_BYTES:
            found.add(target)
    return sorted(d for d in found if not any(d != o and d.startswith(o + "/") for o in found))


def is_relay_source(path: str) -> bool:
    """Whether *path* is a source file of an added Relay ``src`` whose relative imports are followed."""
    return path.endswith(RELAY_SOURCE_SUFFIXES) and not _skipped(path)


def companions(sizes: Mapping[str, int], sub_paths: Iterable[str]) -> list[str]:
    """The files outside *sub_paths* a narrow checkout keeps so its checks are detected.

    *sizes* maps every regular file of the repository (relative to its root)
    to its size. Detection reads the manifests, lock files and tool
    configuration of a directory and of the directories above it, the
    repository's workflows and its ``.xtm-one/checks.toml``: a checkout of
    ``backend/app`` alone has none of them. The companions are those files in
    every directory above a sub_path (up to the root), the workflows, the
    checks file and the actionlint configuration the actions-lint check reads
    (:data:`actions_lint.ACTIONLINT_CONFIGS`, so a checkout of one workflow
    lints it with the repository's runner labels); never a file under a sub_path (already written), under a
    directory a build or an install writes, or past
    :data:`MAX_COMPANION_BYTES`; :data:`MAX_COMPANIONS` at most, the
    workflows and the shallowest first. No sub_path: nothing (the whole tree
    is written).
    """
    wanted = [s.strip("/") for s in sub_paths if s and s.strip("/")]
    if not wanted:
        return []
    above = {a for sub in wanted for a in _ancestors(sub)}
    picked: list[str] = []
    for path, size in sizes.items():
        if any(path == sub or path.startswith(sub + "/") for sub in wanted):
            continue
        if size > MAX_COMPANION_BYTES or _skipped(path):
            continue
        name = posixpath.basename(path)
        if (
            path == CHECKS_FILE
            or _is_ci_config(path)
            or path in actions_lint.ACTIONLINT_CONFIGS
            or (posixpath.dirname(path) in above and _companion_name(name))
        ):
            picked.append(path)
    picked.sort(key=lambda p: (not (_is_ci_config(p) or p in actions_lint.ACTIONLINT_CONFIGS), p.count("/"), p))
    return picked[:MAX_COMPANIONS]


def check_support_files(specs: Mapping[str, CheckSpec], sizes: Mapping[str, int]) -> list[str]:
    """The repository files a declared check's command names, so a narrow checkout keeps them.

    A check may run a script of the repository that lies outside every
    sub_path (``python ../.xtm-one/stores.py -m integration`` from
    ``backend/``): an argument of ``argv`` or ``default_args`` that, resolved
    from the check's ``cwd``, names a regular file of the repository (a key
    of *sizes*) is kept. An absolute path, a path out of the repository, a
    directory or a file past :data:`MAX_COMPANION_BYTES` adds nothing;
    :data:`MAX_COMPANIONS` at most, sorted.
    """
    found: set[str] = set()
    for spec in specs.values():
        base = (spec.cwd or "").strip("/")
        for arg in (*spec.argv, *spec.default_args):
            if not arg or arg.startswith(("-", "/")) or "=" in arg:
                continue
            path = posixpath.normpath(posixpath.join(base, arg)) if base else posixpath.normpath(arg)
            if path.startswith("../") or path in (".", ".."):
                continue
            if path in sizes and sizes[path] <= MAX_COMPANION_BYTES and not _skipped(path):
                found.add(path)
    return sorted(found)[:MAX_COMPANIONS]


def command_named_files(
    workflows: Mapping[str, bytes], manifests: Mapping[str, bytes], sizes: Mapping[str, int], sub_paths: Iterable[str]
) -> list[str]:
    """The repository files outside *sub_paths* that a workflow step or a ``package.json`` script names.

    A detected check runs what CI runs: ``uv pip install -r ci-requirements.txt``
    at the root before ``black --check .``, ``node ../scripts/check-icons.mjs``
    from a package. A narrow checkout without that file installs nothing or
    fails on a missing module, a false red. *workflows* maps the repository's
    workflows to their content, *manifests* the ``package.json`` files above and
    under the sub_paths, *sizes* every regular file of the repository. Each word
    of a ``run`` line (from the step's working directory, after a ``cd``) or of a
    script (from its manifest's directory) that names a regular file of the
    repository is kept. A workflow step that ``cd``s into a directory of the
    repository runs from it and imports what lies there (``cd shared/plugin &&
    PYTHONPATH=. pylint --load-plugins my_plugin ..``): the direct files of that
    directory are kept when it holds at most :data:`MAX_REFERENCED_DIR_FILES`
    files and :data:`MAX_REFERENCED_DIR_BYTES`. Release, publish and deploy workflows and jobs are not
    read (they ship, they do not check); a file under a sub_path, under a
    directory a build or an install writes, out of the repository or past
    :data:`MAX_COMPANION_BYTES` adds nothing; :data:`MAX_COMPANIONS` at most, sorted.
    """
    wanted = [s.strip("/") for s in sub_paths if s and s.strip("/")]
    found: set[str] = set()
    by_dir: dict[str, list[str]] = {}
    for path in sizes:
        by_dir.setdefault(posixpath.dirname(path), []).append(path)

    def keep(target: str | None) -> None:
        if (
            target
            and target in sizes
            and sizes[target] <= MAX_COMPANION_BYTES
            and not any(target == s or target.startswith(s + "/") for s in wanted)
            and not _skipped(target)
        ):
            found.add(target)

    def keep_directory(directory: str) -> None:
        members = by_dir.get(directory, []) if directory else []
        if (
            members
            and len(members) <= MAX_REFERENCED_DIR_FILES
            and sum(sizes[m] for m in members) <= MAX_REFERENCED_DIR_BYTES
        ):
            for member in members:
                keep(member)

    def scan(line: str, cwd: str | None, *, step: bool = False) -> None:
        for segment in _SCRIPT_SEPARATOR.split(line[:MAX_REQUIREMENTS_BYTES]):
            cd = _SCRIPT_CD.match(segment)
            if cd:
                cwd = _script_path(cwd, cd.group(2)) if cwd is not None else None
                if step and cwd:
                    keep_directory(cwd)
                continue
            if cwd is None:
                continue
            for token in _SCRIPT_PATH.findall(segment):
                if token.startswith("-") or ("/" not in token and "." not in token):
                    continue
                keep(_script_path(cwd, token))

    for path, data in sorted(workflows.items()):
        if not _is_workflow(path) or any(word in posixpath.basename(path).lower() for word in _SHIPPING):
            continue
        doc = _yaml(data)
        top = str(_dig(doc, "defaults", "run", "working-directory") or "")
        jobs = doc.get("jobs")
        for job_id, job in jobs.items() if isinstance(jobs, dict) else ():
            if not isinstance(job, dict) or any(word in str(job_id).lower() for word in _SHIPPING):
                continue
            job_dir = _clean_dir("", str(_dig(job, "defaults", "run", "working-directory") or top or ""))
            steps = job.get("steps")
            for step in steps if isinstance(steps, list) else ():
                if not isinstance(step, dict) or not isinstance(step.get("run"), str) or job_dir is None:
                    continue
                cwd = _clean_dir(job_dir, str(step.get("working-directory", "") or ""))
                for line in _run_lines(step["run"]):
                    scan(line, cwd, step=True)
    for path, data in sorted(manifests.items()):
        if posixpath.basename(path) != "package.json":
            continue
        scripts = _json(data).get("scripts")
        for body in scripts.values() if isinstance(scripts, dict) else ():
            if isinstance(body, str):
                scan(body, posixpath.dirname(path))
    return sorted(found)[:MAX_COMPANIONS]


#: The file that declares a release tool's directory of pending notes, and that directory: a note a
#: change adds there (``.changeset/brave-cats-run.md``) belongs to the change wherever the checkout is.
RELEASE_NOTE_DIRS = {
    ".changeset/config.json": ".changeset",
    ".changie.yaml": ".changes",
    ".changie.yml": ".changes",
}
#: A release-note directory larger than this (files, bytes) is not added to a narrow checkout.
MAX_RELEASE_NOTE_FILES = 200
MAX_RELEASE_NOTE_BYTES = 2 * 1024 * 1024


def release_note_dirs(sizes: Mapping[str, int], sub_paths: Iterable[str]) -> list[str]:
    """The release-note directories a narrow checkout adds to its sub_paths (:data:`RELEASE_NOTE_DIRS`).

    A repository that versions with changesets (or changie) asks every change
    for a note under its root ``.changeset/``: outside the sub_paths, a note
    written in a narrow checkout was neither recorded nor committed by
    ``from_checkout``. The directory is added when its declaring file exists,
    it is not under a sub_path already and it holds at most
    :data:`MAX_RELEASE_NOTE_FILES` files and :data:`MAX_RELEASE_NOTE_BYTES`.
    """
    wanted = [s.strip("/") for s in sub_paths if s and s.strip("/")]
    if not wanted:
        return []
    found: list[str] = []
    for marker, directory in RELEASE_NOTE_DIRS.items():
        if marker not in sizes or directory in found:
            continue
        if any(directory == s or directory.startswith(s + "/") for s in wanted):
            continue
        members = [size for path, size in sizes.items() if path.startswith(directory + "/")]
        if len(members) <= MAX_RELEASE_NOTE_FILES and sum(members) <= MAX_RELEASE_NOTE_BYTES:
            found.append(directory)
    return sorted(found)


#: The ``package.json`` fields whose values may name a local file by a Yarn or npm protocol.
_MANIFEST_DEPENDENCY_FIELDS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
    "resolutions",
    "overrides",
)
#: The protocols of a dependency value that point at a local path (``patch:`` after its ``#``).
_LOCAL_PROTOCOLS = ("file:", "link:", "portal:")


def _local_targets(value: str) -> list[str]:
    """The local paths a dependency value names: ``patch:x@1#./p.patch``, ``file:../lib``, ``portal:./pkg``."""
    value = value.strip()
    if value.startswith("patch:"):
        _source, sep, rest = value.partition("#")
        if not sep:
            return []
        # ``#a.patch&b.patch::version=1.0.0&hash=x``: the locator parameters after ``::`` are not paths.
        return [p for p in (part.strip() for part in rest.split("::", 1)[0].split("&")) if p]
    for protocol in _LOCAL_PROTOCOLS:
        if value.startswith(protocol):
            return [value[len(protocol) :].split("#", 1)[0].strip()]
    return []


def _flat_strings(value: object, depth: int = 0) -> list[str]:
    """Every string of *value*, nested objects included (npm ``overrides`` nest), a few levels at most."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict) and depth < 4:
        return [s for v in value.values() for s in _flat_strings(v, depth + 1)]
    return []


def package_local_files(
    manifests: Mapping[str, bytes], sizes: Mapping[str, int], sub_paths: Iterable[str]
) -> list[str]:
    """The repository files outside *sub_paths* an install reads because a manifest names them by a local path.

    A root ``package.json`` that resolves ``"react-relay": "patch:react-relay@21.0.1#./patch/react-relay.patch"``
    makes ``yarn install`` open that patch: a narrow checkout without the
    root's ``patch/`` fails the install with ENOENT, a false red. *manifests*
    maps ``package.json`` and ``.yarnrc.yml`` files to their content, *sizes*
    every regular file of the repository. A ``package.json`` dependency,
    resolution or override value with the ``patch:`` (each path after ``#``),
    ``file:``, ``link:`` or ``portal:`` protocol, and a ``.yarnrc.yml``
    ``yarnPath``, ``plugins`` path or ``patchFolder``, is resolved from that
    file's directory (``~/`` from the repository root); a regular file is
    kept, a directory gives its files when it holds at most
    :data:`MAX_REFERENCED_DIR_FILES` files and :data:`MAX_REFERENCED_DIR_BYTES`.
    Paths out of the repository, under a sub_path, under ``node_modules`` or
    past :data:`MAX_COMPANION_BYTES` add nothing; :data:`MAX_COMPANIONS` at most, sorted.
    """
    wanted = [s.strip("/") for s in sub_paths if s and s.strip("/")]

    def kept(path: str) -> bool:
        return any(path == s or path.startswith(s + "/") for s in wanted)

    found: set[str] = set()

    def add(base: str, literal: str) -> None:
        if not literal or literal.startswith("/"):
            return
        if literal.startswith("~/"):
            base, literal = "", literal[2:]
        target = _script_path(base, literal.rstrip("/"))
        if not target or "node_modules" in target.split("/"):
            return
        if target in sizes:
            if sizes[target] <= MAX_COMPANION_BYTES and not kept(target):
                found.add(target)
            return
        members = [p for p in sizes if p.startswith(target + "/") and "node_modules" not in p.split("/")]
        if (
            members
            and len(members) <= MAX_REFERENCED_DIR_FILES
            and sum(sizes[m] for m in members) <= MAX_REFERENCED_DIR_BYTES
        ):
            found.update(m for m in members if not kept(m) and sizes[m] <= MAX_COMPANION_BYTES)

    for path, data in sorted(manifests.items()):
        base = posixpath.dirname(path)
        name = posixpath.basename(path)
        if name == "package.json":
            doc = _json(data)
            for key in _MANIFEST_DEPENDENCY_FIELDS:
                for value in _flat_strings(doc.get(key)):
                    for literal in _local_targets(value):
                        add(base, literal)
        elif name == ".yarnrc.yml":
            doc = _yaml(data)
            for key in ("yarnPath", "patchFolder"):
                if isinstance(doc.get(key), str):
                    add(base, doc[key])
            plugins = doc.get("plugins")
            for plugin in plugins if isinstance(plugins, list) else ():
                spec = plugin.get("path") if isinstance(plugin, dict) else plugin
                if isinstance(spec, str) and not spec.startswith(("http:", "https:")):
                    add(base, spec)
    return sorted(found)[:MAX_COMPANIONS]


@dataclass(frozen=True)
class Widening:
    """A sub_path replaced by the directory a declared check runs from."""

    sub_path: str
    directory: str
    checks: tuple[str, ...]

    def words(self) -> str:
        """Why it was widened, for the answer: the checks run from the directory, or the package it sits in."""
        if self.checks:
            return f"the directory {', '.join(self.checks)} run from"
        return "the package it sits in"


#: Bytes of a package (its files, ``node_modules`` and build output aside) a sub_path inside it is widened to at most.
MAX_PACKAGE_WIDEN_BYTES = 64 * 1024 * 1024


def widen_to_package_roots(
    sub_paths: Iterable[str], sizes: Mapping[str, int], *, max_bytes: int = MAX_PACKAGE_WIDEN_BYTES
) -> tuple[list[str], list[Widening]]:
    """*sub_paths* with each one strictly inside a JavaScript package replaced by the package's directory.

    A unit or type check of ``front/src/x`` runs from the package root ``front/`` and loads what lies
    there (``front/config.ts``, a static asset a test imports, the modules an alias names): a checkout of
    ``front/src/x`` alone fails at collection, and a repository without ``.xtm-one/checks.toml`` declares
    no check directory for :func:`widen_to_check_dirs` to widen to. The nearest directory above the
    sub_path holding a ``package.json`` wins, never the repository root (that is the whole tree), and only
    while the package's files (directories a build or an install writes aside) stay within *max_bytes*.
    *sizes* maps every regular file of the repository to its size. Order is kept, and a sub_path left
    under a widened one is dropped. Returns the sub_paths and what was widened.
    """
    wanted = [s.strip("/") for s in sub_paths if s and s.strip("/")]
    roots = {posixpath.dirname(p) for p in sizes if posixpath.basename(p) == "package.json" and not _skipped(p)}
    roots.discard("")
    weights: dict[str, int] = {}

    def weight(root: str) -> int:
        if root not in weights:
            weights[root] = sum(
                size for path, size in sizes.items() if path.startswith(root + "/") and not _skipped(path)
            )
        return weights[root]

    widened: list[Widening] = []
    result: list[str] = []
    for sub in wanted:
        holder = next((d for d in _ancestors(posixpath.dirname(sub)) if d in roots), None)
        if holder is not None and weight(holder) <= max_bytes:
            widened.append(Widening(sub, holder, ()))
            sub = holder
        if sub not in result:
            result.append(sub)
    kept = [s for s in result if not any(s != o and s.startswith(o + "/") for o in result)]
    return kept, widened


def widen_to_check_dirs(sub_paths: Iterable[str], specs: Mapping[str, CheckSpec]) -> tuple[list[str], list[Widening]]:
    """*sub_paths* with each one strictly inside a declared check's ``cwd`` replaced by that directory.

    A check runs from its ``cwd`` and imports what lies there (``python -m
    pytest`` from ``backend/`` imports the whole ``app`` package): a checkout
    of ``backend/app/services/x`` alone fails at collection. The outermost
    such directory wins; a check run from the repository root never widens
    (that is the whole tree). Order is kept, and a sub_path left under a
    widened one is dropped. Returns the sub_paths and what was widened.
    """
    wanted = [s.strip("/") for s in sub_paths if s and s.strip("/")]
    by_dir: dict[str, list[str]] = {}
    for name, spec in specs.items():
        if spec.cwd:
            by_dir.setdefault(spec.cwd.strip("/"), []).append(name)
    widened: list[Widening] = []
    result: list[str] = []
    for sub in wanted:
        holders = sorted((d for d in by_dir if sub.startswith(d + "/")), key=lambda d: (d.count("/"), d))
        if holders:
            directory = holders[0]
            widened.append(Widening(sub, directory, tuple(sorted(by_dir[directory]))))
            sub = directory
        if sub not in result:
            result.append(sub)
    kept = [s for s in result if not any(s != o and s.startswith(o + "/") for o in result)]
    return kept, widened


def check_needs(sub_paths: Iterable[str], specs: Mapping[str, CheckSpec]) -> list[str]:
    """The ``needs`` of every declared check whose directory *sub_paths* hold, not already under them.

    A check reads what lies outside its ``cwd`` when a test there imports
    ``../../apps/x/src``: a checkout of ``frontend/`` alone then fails its
    type check on a missing module. Called after :func:`widen_to_check_dirs`,
    so a sub_path inside a check's directory is that directory already. A
    check run from the root runs on any narrow checkout, so its needs are
    always added (the editions gate reads LICENSE, the AGENTS.md hygiene
    reads dev-docs/): it would otherwise report what the checkout lacks.
    """
    wanted = [s.strip("/") for s in sub_paths if s and s.strip("/")]
    added: list[str] = []
    if not wanted:
        return added
    for spec in specs.values():
        directory = spec.cwd.strip("/")
        if not spec.needs:
            continue
        if directory and not any(directory == s or directory.startswith(s + "/") for s in wanted):
            continue
        for need in spec.needs:
            if need not in added and not any(need == s or need.startswith(s + "/") for s in wanted):
                added.append(need)
    return [n for n in added if not any(n != o and n.startswith(o + "/") for o in added)]


def _text(data: bytes | None) -> str:
    return data.decode("utf-8", errors="replace") if data else ""


def _json(data: bytes | None) -> dict:
    try:
        value = json.loads(_text(data) or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _toml(data: bytes | None) -> dict:
    try:
        return tomllib.loads(_text(data))
    except tomllib.TOMLDecodeError:
        return {}


def _yaml(data: bytes | None) -> dict:
    try:
        value = yaml.safe_load(_text(data))
    except yaml.YAMLError:
        return {}
    return value if isinstance(value, dict) else {}


def _join(directory: str, name: str) -> str:
    return f"{directory}/{name}" if directory else name


def _ancestors(directory: str) -> list[str]:
    """*directory* and every directory above it, up to the root (``""``), nearest first."""
    out = [directory]
    while directory:
        directory = posixpath.dirname(directory)
        out.append(directory)
    return out


def _clean_dir(base: str, rel: str, dirs: frozenset[str] = frozenset()) -> str | None:
    """*rel* (a workflow's working directory) joined to *base*, inside the checkout, or ``None``.

    An absolute path (a CI container's mount, ``/opt/app/front``) is the
    checkout directory among *dirs* it ends with, longest first.
    """
    if not rel or rel in (".", "./"):
        return base
    if "${{" in rel or "$" in rel or "\\" in rel or "~" in rel:
        return None
    if rel.startswith("/"):
        parts = [p for p in posixpath.normpath(rel).split("/") if p]
        return next(("/".join(parts[i:]) for i in range(len(parts)) if "/".join(parts[i:]) in dirs), None)
    joined = posixpath.normpath(_join(base, rel))
    if joined == ".":
        return ""
    if joined == ".." or joined.startswith("../"):
        return None
    return joined


#: The check kinds whose trailing tree target (``black --check .``) a call's args replace.
_TREE_TARGET_KINDS = ("lint", "format", "types")
#: The tree targets a workflow line ends with: the check's whole directory.
_TREE_TARGETS = frozenset({".", "./"})


def _split_tree_target(kind: str, argv: list[str]) -> tuple[list[str], tuple[str, ...]]:
    """(*argv* without its trailing ``.``, that ``.`` as default args) for a lint, format or types check.

    A detected ``black --check .`` ran ``black --check . x.py`` when a call named one file: the call's
    args followed the tree target, so the whole tree was checked again. Its ``.`` is a default argument
    instead: a call without args runs the whole directory as before, a call with args runs on them alone.
    Other kinds, and an argv of one word, are kept whole.
    """
    if kind.split("-", 1)[0] not in _TREE_TARGET_KINDS or len(argv) < 2 or argv[-1] not in _TREE_TARGETS:
        return argv, ()
    return argv[:-1], (argv[-1],)


class _Collector:
    def __init__(self, images: frozenset[str]) -> None:
        self.images = images
        self.found: dict[tuple[str, str, str], CheckSpec] = {}
        #: Repository shell scripts a workflow runs once per project (``bash run_test.sh "$req"``), with their source.
        self.scripts: dict[str, str] = {}
        #: The Python (``major.minor``) the workflow job a check comes from sets up, by check key.
        self.pythons: dict[tuple[str, str, str], str] = {}
        #: Every Python version a workflow job of the repository sets up, in reading order.
        self.workflow_pythons: list[str] = []
        #: (exact ``major.minor``, ``requires-python`` specifier) a project directory declares.
        self.project_pythons: dict[str, tuple[str, str]] = {}

    def image(self, stack: str) -> str:
        keys = STACK_IMAGES.get(stack, STACK_IMAGES["python"])
        return next((key for key in keys if key in self.images), keys[0])

    def add(
        self,
        directory: str,
        stack: str,
        kind: str,
        argv: list[str],
        source: str,
        *,
        runs_on: str = "",
        env: Mapping[str, str | None] | None = None,
        stores: bool = False,
        **report: str,
    ) -> bool:
        key = (directory, stack, kind)
        if key in self.found or len(self.found) >= MAX_CHECKS or not argv:
            return False
        name = f"{stack}-{kind}" if not directory else f"{directory}:{stack}-{kind}"
        if kind == "install" and posixpath.basename(argv[0]) == "pnpm" and not any("strict-dep-builds" in a for a in argv):
            # A workflow's own `pnpm install` too: the CI runner it was written for does not fail on it.
            argv = [*argv, _PNPM_LENIENT_BUILDS]
        argv, defaults = _split_tree_target(kind, argv)
        self.found[key] = CheckSpec(
            name=name,
            argv=tuple(argv),
            default_args=defaults,
            cwd=directory,
            timeout=min(_TIMEOUTS.get(kind, 900), MAX_TIMEOUT),
            image=self.image(runs_on or stack),
            report=report.get("report", "lines"),
            report_path=report.get("report_path", ""),
            description=source[:200],
            env=tuple(sorted((k, v) for k, v in (env or {}).items() if v is not None))[:MAX_ENV],
            env_unset=tuple(sorted(k for k, v in (env or {}).items() if v is None))[:MAX_ENV],
            stores=stores,
        )
        return True

    def stack_of(self, directory: str) -> str:
        """The stack detected nearest to *directory* (itself, then above), ``python`` when none."""
        for where in _ancestors(directory):
            stack = next((s for (d, s, _kind) in self.found if d == where and s != "make"), "")
            if stack:
                return stack
        return "python"

    def checks(self) -> dict[str, CheckSpec]:
        order = {kind: index for index, kind in enumerate(KINDS)}
        rows = sorted(self.found.items(), key=lambda item: (item[0][0], item[0][1], order.get(item[0][2], len(order))))
        return {spec.name: spec for _key, spec in rows}


# -- workflows -----------------------------------------------------------------


def _script_kind(script: str) -> str | None:
    lowered = script.lower()
    if any(word in lowered for word in ("e2e", "playwright", "cypress", "deploy", "release", "publish")):
        return None
    if "lint" in lowered:
        return "lint"
    if "format" in lowered or "prettier" in lowered:
        return "format" if "check" in lowered else None
    if "type" in lowered or "tsc" in lowered or "check-ts" in lowered:
        return "types"
    if "test" in lowered or lowered in ("vitest", "jest", "unit"):
        return "unit"
    if "build" in lowered:
        return "build"
    return None


_PY_TOOLS = {
    "pytest": "unit",
    "ruff": "lint",
    "flake8": "lint",
    "pylint": "lint",
    "black": "format",
    "isort": "format",
    "mypy": "types",
    "pyright": "types",
}
_JVM_GOALS = (
    ("dependency:go-offline", "install"),
    ("spotless:check", "lint"),
    ("spotlesscheck", "lint"),
    ("checkstyle", "lint"),
    ("test", "unit"),
    ("verify", "unit"),
    ("package", "build"),
    ("install", "build"),
    ("build", "build"),
    ("compile", "build"),
)
_CARGO = {"fetch": "install", "clippy": "lint", "fmt": "format", "test": "unit", "build": "build"}
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: Literal ``env:`` variables a detected check carries from its workflow, job and step, and their size.
MAX_ENV = 64
MAX_ENV_CHARS = 2048
#: Variables the sandbox container owns: a workflow value for them describes the CI runner, never the check.
_RUNNER_ENV = frozenset({"PATH", "HOME", "SHELL", "USER", "PWD", "TMPDIR", "WORKSPACE_DIR"})
_RUNNER_ENV_PREFIXES = ("GITHUB_", "RUNNER_", "ACTIONS_")


def classify(tokens: list[str]) -> tuple[str, str] | None:
    """The (stack, kind) of a command line's argv, or ``None`` when it is not a check (a deploy, an echo)."""
    if not tokens:
        return None
    tool = posixpath.basename(tokens[0])
    rest = tokens[1:]
    words = [t for t in rest if not t.startswith("-")]
    if tool in ("yarn", "npm", "pnpm", "npx", "bun"):
        if "-g" in rest or "--global" in rest or words[:1] == ["global"]:
            return None  # a tool installed for the runner, not the checkout's dependencies
        if tool == "npx":
            return ("node", kind) if words and (kind := _script_kind(words[0])) else None
        if not words or words[0] in ("install", "ci", "i"):
            return ("node", "install")
        script = words[1] if words[0] == "run" and len(words) > 1 else words[0]
        return ("node", kind) if (kind := _script_kind(script)) else None
    if tool in ("python", "python3") and len(rest) >= 2 and rest[0] == "-m":
        return classify(rest[1:])
    if tool in ("python", "python3") and rest and rest[0].endswith(".py"):
        return classify(rest)
    if tool == "manage.py":
        # Django's own runner (`python manage.py test`): the project's unit tests.
        return ("python", "unit") if words[:1] == ["test"] else None
    if tool.endswith(".py") and len(tool) > 3:
        # A repository script (`uv run plugin/scripts/flow_lint.py`, `python scripts/lint_docs.py`): its
        # name says the kind, as a package.json script name does; a PEP 723 header carries its dependencies.
        return ("python", kind) if (kind := _script_kind(tool[:-3]) or _gate_script_kind(tool[:-3])) else None
    if tool in ("pip", "pip3"):
        return ("python", "install") if words[:1] == ["install"] and "--user" not in rest else None
    if tool in ("uv", "poetry"):
        if words[:1] in (["sync"], ["install"]) or words[:2] == ["pip", "install"]:
            return ("python", "install")
        if words[:1] == ["run"] and len(words) > 1:
            tail = rest[rest.index("run") + 1 :]
            while tail and tail[0].startswith("-"):
                tail = tail[1:]
            return classify(tail)
        return None
    if tool in _PY_TOOLS:
        kind = _PY_TOOLS[tool]
        checking = any(a.startswith("--check") or a == "--diff" for a in rest)
        if tool == "ruff" and words[:1] == ["format"]:
            return ("python", "format") if checking else None
        if kind == "format" and not checking:
            return None
        return ("python", kind)
    if tool in ("mvn", "mvnw", "gradle", "gradlew"):
        goals = [w.lower() for w in words]
        for goal, kind in _JVM_GOALS:
            if any(g == goal or g.endswith(f":{goal}") for g in goals):
                return ("jvm", kind)
        return None
    if tool == "cargo":
        if not words or words[0] not in _CARGO or (words[0] == "fmt" and "--check" not in rest):
            return None
        return ("rust", _CARGO[words[0]])
    if tool == "dbt":
        if words[:1] == ["deps"]:
            return ("dbt", "install")
        return ("dbt", "parse") if words[:1] in (["parse"], ["compile"]) else None
    if tool == "sqlfluff":
        return ("dbt", "lint") if words[:1] == ["lint"] else None
    if tool == "make" and words and words[0] in _MAKE_KINDS:
        return ("make", _MAKE_KINDS[words[0]])
    return None



def _run_lines(script: str) -> list[str]:
    lines, pending = [], ""
    for raw in script.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        line = (pending + line).strip()
        pending = ""
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


_CD = re.compile(r"^cd\s+([^\s;&|]+)\s*$")
_SHIPPING = ("release", "publish", "deploy")


#: A repository gate script named for what it checks (``check_catalog_contract.py``, ``validate_manifest.py``).
_GATE_SCRIPT = re.compile(r"^(check|validate|verify)[-_][a-z0-9]")


def _gate_script_kind(name: str) -> str | None:
    """``check-<rest>`` for a repository gate script, its own check: ``check_catalog_contract`` gives ``check-catalog-contract``."""
    lowered = name.lower()
    if not _GATE_SCRIPT.match(lowered) or any(word in lowered for word in _SCRIPT_SKIP):
        return None
    rest = re.sub(r"[^a-z0-9]+", "-", re.sub(r"^(check|validate|verify)", "", lowered)).strip("-")
    return f"check-{rest}" if rest else None


_EXPRESSION = re.compile(r"\$\{\{.*?\}\}")
_EXPRESSION_MARK = "\x00"


def _script_index(tokens: list[str]) -> int | None:
    """The index of the repository ``.py`` script a command runs (``python x.py``, ``uv run x.py``, ``x.py``)."""
    if not tokens:
        return None
    tool = posixpath.basename(tokens[0])
    if tool.endswith(".py"):
        return 0
    if tool in ("python", "python3") and len(tokens) > 1 and tokens[1].endswith(".py"):
        return 1
    if tool == "uv" and len(tokens) > 2 and tokens[1] == "run":
        index = 2
        while index < len(tokens) and tokens[index].startswith("-"):
            index += 1
        return index if index < len(tokens) and tokens[index].endswith(".py") else None
    return None


def _without_expression_options(line: str) -> str | None:
    """*line* with each option of its repository script whose value is a ``${{ }}`` expression left out, or ``None``.

    ``python .github/scripts/check_catalog_contract.py --changed-since "origin/${{ github.base_ref }}"``
    gives ``python .github/scripts/check_catalog_contract.py``: the sandbox has no runner context, the
    caller passes the option in the call's args. An expression anywhere else (the command, a positional
    argument, a shell construct) leaves the line out, as before.
    """
    marked = _EXPRESSION.sub(_EXPRESSION_MARK, line)
    if "${{" in marked or any(c in _SHELL_CHARS for c in marked):
        return None
    try:
        tokens = shlex.split(marked)
    except ValueError:
        return None
    start = _script_index(tokens)
    if start is None or any(_EXPRESSION_MARK in t for t in tokens[: start + 1]):
        return None
    kept = tokens[: start + 1]
    for token in tokens[start + 1 :]:
        if _EXPRESSION_MARK not in token:
            kept.append(token)
        elif token.startswith("-") and "=" in token:
            continue  # --name=${{ ... }}
        elif len(kept) > start + 1 and kept[-1].startswith("-") and "=" not in kept[-1]:
            kept.pop()  # --name ${{ ... }}: the flag goes with its value
        else:
            return None
    return shlex.join(kept)


def _command(line: str, cwd: str, dirs: frozenset[str] = frozenset()) -> tuple[str, list[str], list[str]] | None:
    """(cwd, the argv to run, the tokens to classify) of a workflow line, or ``None``."""
    if "${{" in line:
        stripped = _without_expression_options(line)
        if stripped is None:
            return None
        line = stripped
    match = re.match(r"^cd\s+([^\s;&|]+)\s*&&\s*(.+)$", line)
    if match:
        where = _clean_dir(cwd, match.group(1), dirs)
        if where is None:
            return None
        cwd, line = where, match.group(2)
    try:
        tokens = shlex.split(line)
    except ValueError:
        return None
    if not tokens:
        return None
    shell = any(c in _SHELL_CHARS for c in line) or bool(_ENV_ASSIGNMENT.match(tokens[0]))
    if any(c in _SHELL_CHARS for c in line):
        head = re.split(r"\s*(?:&&|\|\||[|;])\s*", line, maxsplit=1)[0]
        try:
            tokens = shlex.split(head)
        except ValueError:
            return None
    words = list(tokens)
    while words and _ENV_ASSIGNMENT.match(words[0]):
        words = words[1:]
    return cwd, (["sh", "-c", line] if shell else tokens), words


#: A whole value that only reads an outer literal: ``${{ env.PYTHON_VERSION }}``.
_ENV_REFERENCE = re.compile(r"^\$\{\{\s*env\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")


def _layer_env(base: Mapping[str, str | None], block: object) -> dict[str, str | None]:
    """*base* overlaid with the variables of a workflow, job or step ``env:`` *block*.

    A literal value travels as written; ``${{ env.NAME }}`` takes the outer literal it names. Any
    other expression (``${{ secrets.X }}``, ``${{ github.workspace }}``) maps the name to ``None``:
    the sandbox has no secret and no runner context, so the value is never copied, but the name is
    kept so the run can say which variable CI sets that the sandbox leaves unset.
    """
    env = dict(base)
    if not isinstance(block, dict):
        return env
    for key, raw in block.items():
        name = str(key)
        if not _ENV_NAME.match(name) or name in _RUNNER_ENV or name.startswith(_RUNNER_ENV_PREFIXES):
            continue
        text = ("true" if raw else "false") if isinstance(raw, bool) else "" if raw is None else str(raw)
        reference = _ENV_REFERENCE.match(text.strip())
        if reference:
            env[name] = base.get(reference.group(1))
            continue
        if isinstance(raw, dict | list) or "${{" in text or len(text) > MAX_ENV_CHARS or "\x00" in text:
            env[name] = None
            continue
        env[name] = text
    return env


def _dig(value: object, *keys: str) -> object:
    """``value[k1][k2]...`` through mappings only, ``None`` when a level is missing or not a mapping."""
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


#: A CI service image that is a PostgreSQL server (``postgres:16``, ``pgvector/pgvector:0.8.7-pg17``,
#: ``timescale/timescaledb``, ``postgis/postgis``): the job's checks run with the offline stores (#1858).
_POSTGRES_IMAGE = re.compile(r"(?:^|/)(?:postgres|postgresql|pgvector|timescaledb[\w-]*|postgis)(?::|@|$)")


def is_postgres_image(image: str) -> bool:
    """Whether a CI service *image* runs PostgreSQL (its registry, tag and digest read as written)."""
    return bool(_POSTGRES_IMAGE.search(image.strip().lower()))


def _workflows(collector: _Collector, files: Mapping[str, bytes], infra: list[Infra], dirs: frozenset[str]) -> None:
    budget = MAX_RUN_LINES
    for path in sorted(p for p in files if _is_workflow(p)):
        doc = _yaml(files[path])
        top = _dig(doc, "defaults", "run", "working-directory") or ""
        workflow_env = _layer_env({}, doc.get("env"))
        jobs = doc.get("jobs") if isinstance(doc.get("jobs"), dict) else {}
        shipping = any(word in posixpath.basename(path).lower() for word in _SHIPPING)
        for job_id, job in jobs.items():
            if not isinstance(job, dict) or shipping or any(word in str(job_id).lower() for word in _SHIPPING):
                continue  # a release, publish or deploy job ships the code, it does not check it
            services = job.get("services") if isinstance(job.get("services"), dict) else {}
            job_postgres = False
            for name, service in services.items():
                image = service.get("image", "") if isinstance(service, dict) else str(service or "")
                infra.append(Infra(str(name), str(image), f"{path} job {job_id}"))
                job_postgres = job_postgres or is_postgres_image(str(image))
            job_dir = _clean_dir("", str(_dig(job, "defaults", "run", "working-directory") or top or ""))
            job_env = _layer_env(workflow_env, job.get("env"))
            steps = job.get("steps") if isinstance(job.get("steps"), list) else []
            job_python = ""
            tools: list[str] = []  # `--with` arguments of what the job's earlier steps installed
            for step in steps:
                if isinstance(step, dict) and isinstance(step.get("uses"), str) and _SETUP_PYTHON.search(step["uses"]):
                    job_python = _minor(str(_dig(step, "with", "python-version") or "")) or job_python
                    if job_python and job_python not in collector.workflow_pythons:
                        collector.workflow_pythons.append(job_python)
                if not isinstance(step, dict) or not isinstance(step.get("run"), str) or job_dir is None:
                    continue
                cwd: str | None = _clean_dir(job_dir, str(step.get("working-directory", "") or ""), dirs)
                step_env = _layer_env(job_env, step.get("env"))
                per_project = "requirements" in step["run"]
                for line in _run_lines(step["run"]):
                    called = _SCRIPT_CALL.match(line) if per_project and cwd is not None else None
                    if called and cwd is not None:
                        script = _clean_dir(cwd, called.group("script"), dirs)
                        if script:
                            collector.scripts.setdefault(script, f"{path} job {job_id}: {line}")
                    budget -= 1
                    if budget < 0:
                        return
                    moved = _CD.match(line)
                    if moved:
                        # A bare `cd` moves the rest of the step; a directory outside the checkout stops it.
                        cwd = _clean_dir(cwd, moved.group(1), dirs) if cwd is not None else None
                        continue
                    if cwd is None:
                        continue
                    parsed = _command(line, cwd, dirs)
                    found = classify(parsed[2]) if parsed else None
                    if parsed and not found:
                        # `pip install black isort --user` is no check (a user install), but installs the tools.
                        tools.extend(_installed(parsed[2], parsed[0], dirs))
                    if parsed and found:
                        stack, kind = found
                        runs_on = collector.stack_of(parsed[0]) if stack == "make" else ""
                        argv = parsed[1]
                        if stack == "python" and kind == "install":
                            tools.extend(_installed(parsed[2], parsed[0], dirs))
                        else:
                            argv = _with_job_tools(stack, kind, argv, parsed[2], tools, parsed[0])
                        source = f"{path} job {job_id}: {line}"
                        kind = _second_tool_kind(collector, parsed[0], stack, kind, parsed[2], f"{path} job {job_id}: ")
                        added = collector.add(
                            parsed[0],
                            stack,
                            kind,
                            argv,
                            source,
                            runs_on=runs_on,
                            env=step_env,
                            stores=job_postgres and kind != "install",
                        )
                        if added and job_python:
                            collector.pythons[(parsed[0], stack, kind)] = job_python


#: A CircleCI pipeline parameter (``<< parameters.name >>``): a value the sandbox cannot resolve.
_CIRCLECI_PARAMETER = "<<"
#: The directory a CircleCI job runs in when it names none: the checkout step writes the repository there.
_CIRCLECI_HOME = "~/project"
_CIRCLECI_PYTHON_IMAGE = re.compile(r"python:(\d+\.\d+)")


def _circleci_env(base: Mapping[str, str | None], block: object) -> dict[str, str | None]:
    """*base* overlaid with a CircleCI ``environment:`` block; a parameter value is a name the sandbox leaves unset."""
    env = _layer_env(base, block)
    for key, raw in block.items() if isinstance(block, dict) else ():
        if str(key) in env and _CIRCLECI_PARAMETER in str(raw):
            env[str(key)] = None
    return env


def _circleci_cwd(home: str, job_dir: str, rel: str, dirs: frozenset[str]) -> str | None:
    """A ``run`` step's ``working_directory`` in the checkout, or ``None`` (outside it, or a parameter).

    CircleCI checks the repository out into the job's ``working_directory`` (*home*, ``~/repo``):
    a step directory below *home* is that directory of the checkout, any other absolute or
    home-relative one lies outside it, a relative one is joined to the job's directory.
    """
    if not rel:
        return job_dir
    if _CIRCLECI_PARAMETER in rel:
        return None
    if rel.startswith(("~", "/")):
        if rel.rstrip("/") != home and not rel.startswith(home + "/"):
            return None
        return _clean_dir("", rel[len(home) :].strip("/"), dirs)
    return _clean_dir(job_dir, rel, dirs)


def _circleci(collector: _Collector, files: Mapping[str, bytes], infra: list[Infra], dirs: frozenset[str]) -> None:
    """The checks of a CircleCI configuration: every job's ``run`` lines, as :func:`_workflows` reads a workflow's.

    The job's first ``docker`` image is the one its steps run in (its ``python:X.Y`` tag names the
    job's Python); the next ones are its services. A line or a directory that names a pipeline
    parameter is skipped, and the job directory written in a line (``flake8 ~/repo``) is the
    checkout root when the line runs there.
    """
    budget = MAX_RUN_LINES
    for path in sorted(p for p in files if p in CIRCLECI_CONFIGS):
        doc = _yaml(files[path])
        raw_jobs = doc.get("jobs")
        jobs: dict[Any, Any] = raw_jobs if isinstance(raw_jobs, dict) else {}
        for job_id, job in jobs.items():
            if not isinstance(job, dict) or any(word in str(job_id).lower() for word in _SHIPPING):
                continue  # a release, publish or deploy job ships the code, it does not check it
            images = (
                [i for i in job.get("docker") or [] if isinstance(i, dict)]
                if isinstance(job.get("docker"), list)
                else []
            )
            for service in images[1:]:
                image = str(service.get("image") or "")
                infra.append(
                    Infra(
                        str(service.get("name") or image.split(":")[0].rsplit("/", 1)[-1]),
                        image,
                        f"{path} job {job_id}",
                    )
                )
            pinned = _CIRCLECI_PYTHON_IMAGE.search(str(images[0].get("image") or "")) if images else None
            job_python = _minor(pinned.group(1)) if pinned else ""
            if job_python and job_python not in collector.workflow_pythons:
                collector.workflow_pythons.append(job_python)
            # A job image named after its tool (alpine/flake8) carries it; the toolchain image does not.
            image_tool = re.sub(r"[:@].*$", "", str(images[0].get("image") or "")).rsplit("/", 1)[-1] if images else ""
            tools: list[str] = ["--with", image_tool] if image_tool in _IMAGE_TOOLS else []
            home = str(job.get("working_directory") or _CIRCLECI_HOME).rstrip("/")
            if not home.startswith(("~", "/")):
                home = f"~/{home}"  # a relative job directory is below the home directory
            # The checkout step writes the repository into the job directory: the steps run at its root.
            job_dir: str | None = None if _CIRCLECI_PARAMETER in home else ""
            job_env = _circleci_env({}, job.get("environment"))
            raw_steps = job.get("steps")
            steps: list[Any] = raw_steps if isinstance(raw_steps, list) else []
            for step in steps:
                run = step.get("run") if isinstance(step, dict) else None
                if isinstance(run, str):
                    run = {"command": run}
                if not isinstance(run, dict) or not isinstance(run.get("command"), str) or job_dir is None:
                    continue
                cwd = _circleci_cwd(home, job_dir, str(run.get("working_directory") or ""), dirs)
                step_env = _circleci_env(job_env, run.get("environment"))
                for line in _run_lines(run["command"]):
                    budget -= 1
                    if budget < 0:
                        return
                    moved = _CD.match(line)
                    if moved:
                        cwd = _clean_dir(cwd, moved.group(1), dirs) if cwd is not None else None
                        continue
                    if cwd is None or _CIRCLECI_PARAMETER in line:
                        continue
                    if cwd == "":
                        line = re.sub(
                            rf"(?<![\w~/.-]){re.escape(home)}(/|(?=[\s;&|)]|$))",
                            lambda m: "" if m.group(1) else ".",
                            line,
                        )
                    parsed = _command(line, cwd, dirs)
                    found = classify(parsed[2]) if parsed else None
                    if parsed and not found:
                        # `pip install black isort --user` is no check (a user install), but installs the tools.
                        tools.extend(_installed(parsed[2], parsed[0], dirs))
                    if parsed and found:
                        stack, kind = found
                        runs_on = collector.stack_of(parsed[0]) if stack == "make" else ""
                        argv = parsed[1]
                        if stack == "python" and kind == "install":
                            tools.extend(_installed(parsed[2], parsed[0], dirs))
                        else:
                            argv = _with_job_tools(stack, kind, argv, parsed[2], tools, parsed[0])
                        source = f"{path} job {job_id}: {line}"
                        kind = _second_tool_kind(collector, parsed[0], stack, kind, parsed[2], f"{path} job {job_id}: ")
                        if (
                            collector.add(parsed[0], stack, kind, argv, source, runs_on=runs_on, env=step_env)
                            and job_python
                        ):
                            collector.pythons[(parsed[0], stack, kind)] = job_python


def _second_tool_kind(collector: _Collector, directory: str, stack: str, kind: str, tokens: list[str], job: str) -> str:
    """*kind*, or ``<kind>-<tool>`` for a second formatter or linter the same workflow job runs in *directory*.

    A job that runs ``isort --check-only .`` then ``black --check .`` gates on
    both; one check per (directory, stack, kind) kept the first and dropped the
    second, so a sandbox format check passed what CI refused. The second
    tool's check is named after it (``python-format-black``).
    """
    first = collector.found.get((directory, stack, kind))
    if first is None or kind not in _TOOL_KINDS or not first.description.startswith(job):
        return kind
    tool = _tool_name(tokens)
    try:
        first_tool = _tool_name(shlex.split(first.description[len(job) :]))
    except ValueError:
        first_tool = ""
    if not tool or tool == first_tool:
        return kind
    return f"{kind}-{tool}"


def _tool_name(tokens: list[str]) -> str:
    """The tool a classified line runs, as a check name part: ``uv run python -m black .`` gives ``black``."""
    words = _tool_words([t for t in tokens if not _ENV_ASSIGNMENT.match(t)])
    if words[:2] in (["python", "-m"], ["python3", "-m"]):
        words = words[2:]
    return re.sub(r"[^a-z0-9]+", "-", posixpath.basename(words[0]).lower()).strip("-") if words else ""


#: A workflow step that sets up the job's Python (``actions/setup-python``, ``astral-sh/setup-uv``).
_SETUP_PYTHON = re.compile(r"(^|/)(setup-python|setup-uv)@")
#: Python check kinds whose tool a workflow installs in an earlier step of the job.
_TOOL_KINDS = ("lint", "format", "types")
#: ``pip install`` options that take a value: the value is not a package.
_PIP_VALUE_OPTIONS = frozenset(
    {"-c", "--constraint", "-i", "--index-url", "--extra-index-url", "-p", "--python", "--target", "--prefix", "-f", "--find-links"}
)


def _minor(version: str) -> str:
    """``major.minor`` of a declared Python version (``3.12``, ``3.12.4``, ``3.12.*``), ``""`` when it is none."""
    match = re.match(r"^\s*v?(\d+)\.(\d+)(?:[.\d*]*)\s*$", version)
    return f"{match.group(1)}.{match.group(2)}" if match else ""


def _with_job_tools(stack: str, kind: str, argv: list[str], tokens: list[str], tools: list[str], cwd: str) -> list[str]:
    """*argv*, run in an ephemeral uv environment holding the *tools* its job installed before it.

    A lint, format or types line runs where the job's earlier steps installed its tool: ``pip install
    black isort --user`` then ``black --check .`` (OpenAEV-Platform/injectors on CircleCI) gives
    ``uv run --no-project --with black --with isort black --check .``, since the toolchain image carries
    no black (gap sandbox-toolchain-python-missing-formatters). A ``uv run`` line drops its own prefix;
    a ``poetry run`` line, a shell line or a line with no install before it is kept as written. A bare
    types line is kept too: a type checker reads the project's own packages, which ``--no-project``
    leaves out.
    """
    if stack != "python" or kind not in _TOOL_KINDS or not tools or argv[:2] == ["sh", "-c"] or not tokens:
        return argv
    if tokens[:2] != ["uv", "run"]:
        if kind not in _BARE_TOOL_KINDS or posixpath.basename(tokens[0]) in _RUNNERS:
            return argv
        # A bare tool the job did not name in an install (`pip install -U certifi`, or a project's own
        # `-r requirements.txt` the install check already installs) is kept as written.
        named = {_requirement_name(tools[i + 1]) for i in range(0, len(tools) - 1, 2) if tools[i] == "--with"}
        if _tool_name(tokens) not in named:
            return argv
    return ["uv", "run", "--no-project", *_relative_tools(tools, cwd), *_tool_words(tokens)]


#: The check kinds whose bare tool line (``black --check .``) runs with the tools its job installed.
_BARE_TOOL_KINDS = ("lint", "format")
#: Launchers a bare line names first: their own environment, never wrapped in another one.
_RUNNERS = frozenset({"uv", "uvx", "poetry", "pipx", "tox", "nox", "pre-commit", "make"})
#: Python tools a CircleCI job image is named after (``alpine/flake8``): the job runs them bare, installed by the image.
_IMAGE_TOOLS = frozenset({"flake8", "black", "isort", "pylint", "ruff"})
#: The ``uv run`` options whose value is a package or a requirements file, never the command.
_UV_WITH_OPTIONS = frozenset({"--with", "--with-requirements", "--with-editable"})
#: What a job's ``pip install`` upgrades for itself, never a tool a check runs (``pip3 install -U pip certifi``).
_PIP_SELF = frozenset({"pip", "setuptools", "wheel"})


def _installed(tokens: list[str], cwd: str, dirs: frozenset[str]) -> list[str]:
    """The ``uv run --with`` arguments of a ``pip install`` / ``uv pip install`` of named tools.

    A package name gives ``--with <name>``, ``-r <file>`` gives ``--with-requirements <file>``
    (repository-relative); an editable install, a local path or a project sync gives nothing.
    """
    words = list(tokens)
    if words[:2] in (["python", "-m"], ["python3", "-m"]):
        words = words[2:]
    if posixpath.basename(words[0] if words else "") == "uv":
        words = words[1:]
    if words[:2] != ["pip", "install"] and words[:2] != ["pip3", "install"]:
        return []
    out: list[str] = []
    rest = iter(words[2:])
    for word in rest:
        if word in ("-r", "--requirement"):
            target = _clean_dir(cwd, next(rest, ""), dirs)
            if target:
                out += ["--with-requirements", target]
        elif word in ("-e", "--editable") or word in _PIP_VALUE_OPTIONS:
            next(rest, None)
        elif (
            not word.startswith(("-", ".", "/"))
            and "/" not in word
            and "$" not in word
            and _requirement_name(word) not in _PIP_SELF
        ):
            out += ["--with", word]
    return out


#: Where a requirement's name ends: its extras (``black[jupyter]``), a version specifier or a marker.
_REQUIREMENT_NAME_END = re.compile(r"[\[=<>~!;@\s]")


def _requirement_name(requirement: str) -> str:
    """The package name of a pip requirement, lower case: ``black==24.1.0`` and ``flake8>=6`` give ``black``, ``flake8``.

    A version-pinned install (``pip install black==24.1.0``) keeps its pin in ``--with``, which uv reads as a
    requirement, but the tool a line runs is matched by name alone.
    """
    return _REQUIREMENT_NAME_END.split(requirement, maxsplit=1)[0].lower()


def _relative_tools(tools: list[str], cwd: str) -> list[str]:
    """*tools* (:func:`_installed`, requirements files repository-relative) with the files relative to *cwd*."""
    out = list(tools)
    for index in range(1, len(out)):
        if out[index - 1] == "--with-requirements":
            out[index] = posixpath.relpath(out[index], cwd or ".")
    pairs = dict.fromkeys((out[i], out[i + 1]) for i in range(0, len(out) - 1, 2))  # each tool once, in order
    return [word for pair in pairs for word in pair]


def _tool_words(tokens: list[str]) -> list[str]:
    """The tool's own argv of a classified line: ``uv run --frozen flake8 .`` gives ``flake8 .``."""
    if tokens and posixpath.basename(tokens[0]) in ("uv", "poetry") and "run" in tokens:
        tail = tokens[tokens.index("run") + 1 :]
        while tail and tail[0].startswith("-"):
            tail = tail[1:]
        return tail
    return list(tokens)


def _satisfies(version: str, specifier: str) -> bool:
    """Whether ``major.minor`` *version* meets a ``requires-python`` *specifier* (``>=3.11, <3.13``), compared on two parts."""
    have = tuple(int(p) for p in version.split(".")[:2])
    for clause in (c.strip() for c in specifier.split(",")):
        match = re.match(r"^(~=|==|!=|>=|<=|>|<)\s*(\d+)(?:\.(\d+))?", clause)
        if not match:
            continue
        op, want = match.group(1), (int(match.group(2)), int(match.group(3) or 0))
        ok = {
            "~=": have >= want,
            "==": have == want,
            "!=": have != want,
            ">=": have >= want,
            "<=": have <= want,
            ">": have > want,
            "<": have < want,
        }[op]
        if not ok:
            return False
    return True


def _pick_python(available: tuple[str, ...], exact: str, specifier: str) -> str:
    """The interpreter of *available* a project needs when it is not the default (first), ``""`` otherwise."""
    if not available:
        return ""
    if exact:
        return exact if exact in available[1:] else ""
    if not specifier or _satisfies(available[0], specifier):
        return ""
    fitting = sorted((v for v in available[1:] if _satisfies(v, specifier)), key=lambda v: tuple(int(p) for p in v.split(".")))
    return fitting[-1] if fitting else ""


def _apply_pythons(collector: _Collector) -> None:
    """Run each Python check on the interpreter its workflow job, its project or the repository's CI declares.

    The image's default interpreter (3.14) refuses a project that requires
    ``<3.13``. When the check's image carries the interpreter needed
    (:data:`IMAGE_PYTHONS`), the check gets ``UV_PYTHON`` and a ``python``
    argv runs ``python3.12``. Precedence: the workflow job the check comes
    from, the project's ``.python-version`` or ``requires-python`` (its
    directory, then above), the first Python the repository's workflows set up.
    """
    for key, spec in list(collector.found.items()):
        directory, stack, _kind = key
        available = IMAGE_PYTHONS.get(spec.image, ())
        if stack != "python" or not available:
            continue
        exact, specifier = collector.pythons.get(key, ""), ""
        if not exact:
            declared = next((collector.project_pythons[a] for a in _ancestors(directory) if a in collector.project_pythons), ("", ""))
            exact, specifier = declared
        if not exact and not specifier and collector.workflow_pythons:
            exact = collector.workflow_pythons[0]
        version = _pick_python(available, exact, specifier)
        if not version:
            continue
        argv = (f"python{version}", *spec.argv[1:]) if spec.argv[:1] in (("python",), ("python3",)) else spec.argv
        collector.found[key] = replace(spec, argv=argv, env=(*spec.env, ("UV_PYTHON", version)))


# -- manifests -----------------------------------------------------------------


def _node_manager(directory: str, manifest: dict, paths: frozenset[str], files: Mapping[str, bytes]) -> tuple[str, bool, str | None]:
    """(manager, whether yarn is Berry, the directory holding the lock file or ``None``)."""
    declared = ""
    lock_dir: str | None = None
    manager = ""
    for where in _ancestors(directory):
        here = manifest if where == directory else _json(files.get(_join(where, "package.json")))
        declared = declared or str(here.get("packageManager", "") or "")
        if lock_dir is None:
            for lock, name in (("yarn.lock", "yarn"), ("pnpm-lock.yaml", "pnpm"), ("package-lock.json", "npm")):
                if _join(where, lock) in paths:
                    lock_dir, manager = where, name
                    break
    if declared:
        manager = declared.split("@", 1)[0] or manager
    manager = manager if manager in ("yarn", "pnpm", "npm") else "npm"
    major = re.match(r"^yarn@(\d+)", declared)
    berry = manager == "yarn" and (
        (major is not None and int(major.group(1)) >= 2) or (lock_dir is not None and _join(lock_dir, ".yarnrc.yml") in paths)
    )
    return manager, berry, lock_dir


#: A pnpm release a ``packageManager`` field names (``10.10.0``, ``10.10.0+sha512.<hex>``: corepack checks the hash).
_PNPM_REFERENCE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+sha\d+\.[0-9a-fA-F]+)?$")
#: An exact ``engines.pnpm`` version (``10.10.0``, ``=10.10.0``, ``v10.10.0``).
_PNPM_EXACT = re.compile(r"^(?:=\s*)?v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)$")
#: A semver range ``engines.pnpm`` may hold (``>=10 <11``, ``^10.10.0``, ``10.x || 11.x``).
_PNPM_RANGE = re.compile(r"^[0-9xX*.^~<>=| -]+$")


def pnpm_pin(directory: str, files: Mapping[str, bytes]) -> tuple[str, str]:
    """(version, source) of the pnpm release *directory* pins; ``("", "")`` when it pins none.

    The ``packageManager`` of the nearest manifest declaring one decides first: ``pnpm@x.y.z`` (its
    hash kept) is the pin, another manager or an unreadable value means no pnpm pin. Without one,
    the nearest ``engines.pnpm``: an exact version as is, a range as written (``corepack pnpm@<range>``
    runs the highest release satisfying it). The version is the corepack descriptor after ``pnpm@``.
    """
    manifests = [(where, _json(files.get(_join(where, "package.json")))) for where in _ancestors(directory)]
    for where, manifest in manifests:
        declared = str(manifest.get("packageManager", "") or "").strip()
        if not declared:
            continue
        name, _, version = declared.partition("@")
        if name == "pnpm" and _PNPM_REFERENCE.match(version):
            return version, f"{_join(where, 'package.json')} packageManager"
        return "", ""
    for where, manifest in manifests:
        engine = _mapping(manifest.get("engines")).get("pnpm")
        wanted = " ".join(engine.split()) if isinstance(engine, str) else ""
        if not wanted:
            continue
        source = f"{_join(where, 'package.json')} engines.pnpm"
        exact = _PNPM_EXACT.match(wanted)
        if exact:
            return exact.group(1), source
        if _PNPM_RANGE.match(wanted) and any(c.isdigit() for c in wanted):
            return wanted, source
        return "", ""
    return "", ""


#: A ``pnpm`` step of a chained check program: at its start or after a `` && ``.
_PNPM_STEP = re.compile(r"(^|&& )pnpm ")


def _pnpm_step(runner: str, match: re.Match[str]) -> str:
    """A ``pnpm`` step of a chained program (:data:`_PNPM_STEP`) calling *runner* instead."""
    return f"{match.group(1)}{runner} "


def _apply_pnpm(collector: _Collector, files: Mapping[str, bytes]) -> None:
    """Run every pnpm check with the release its checkout pins: ``corepack pnpm@<version> ...``.

    The image's ``pnpm`` shim resolves a ``packageManager`` field but not ``engines.pnpm``: a repository
    pinning pnpm there got corepack's default release and failed its install on
    ``ERR_PNPM_UNSUPPORTED_ENGINE``. The pin goes into the argv (so the run output shows it and the
    install cache key, which hashes the argv, is per version) and into the description. A chained
    check (``sh -c '<step> && ... && exec "$@"'``: Relay, fetched and runtime steps) gets it in every
    ``pnpm`` step of its program and in its check argv.
    """
    for key, spec in list(collector.found.items()):
        argv = spec.argv
        relay = argv[:2] == ("sh", "-c") and len(argv) > 4 and argv[3] == "xtm-relay"
        head = argv[4:] if relay else argv
        if not head or head[0] != "pnpm":
            continue
        version, source = pnpm_pin(key[0], files)
        if not version:
            continue
        runner = ("corepack", f"pnpm@{version}")
        pinned = (*runner, *head[1:])
        if relay:
            program = _PNPM_STEP.sub(functools.partial(_pnpm_step, shlex.join(runner)), argv[2])
            pinned = (*argv[:2], program, argv[3], *pinned)
        note = f" (pnpm {version} pinned by {source})"
        collector.found[key] = replace(
            spec, argv=pinned, description=(spec.description[: max(0, 200 - len(note))] + note)[:200]
        )


def _node(collector: _Collector, paths: frozenset[str], files: Mapping[str, bytes]) -> None:
    named: list[tuple[str, str, str, dict[str, Any], set[str]]] = []
    for path in sorted(p for p in files if posixpath.basename(p) == "package.json"):
        manifest = _json(files[path])
        directory = posixpath.dirname(path)
        manager, berry, lock_dir = _node_manager(directory, manifest, paths, files)
        if lock_dir == directory or (lock_dir is None and not any(_join(a, "package.json") in paths for a in _ancestors(directory)[1:])):
            if manager == "yarn":
                install = ["yarn", "install", "--immutable"] if berry else ["yarn", "install", "--frozen-lockfile"]
            elif manager == "pnpm":
                install = ["pnpm", "install", "--frozen-lockfile"]
            else:
                install = ["npm", "ci"] if lock_dir is not None else ["npm", "install"]
            collector.add(directory, "node", "install", install, f"{path} ({manager}{' lock' if lock_dir is not None else ''})")
        scripts = manifest.get("scripts") if isinstance(manifest.get("scripts"), dict) else {}
        used: set[str] = set()
        for kind, names in _NODE_SCRIPTS:
            script = next((n for n in names if isinstance(scripts.get(n), str) and "no test specified" not in scripts[n]), "")
            if kind == "unit" and script == "test":
                script = _unit_script(_mapping(scripts)) or script
            if script:
                argv = [manager, script] if manager == "yarn" else [manager, "run", script]
                collector.add(directory, "node", kind, argv, f"{path} scripts.{script}")
                used.add(script)
                if kind == "format":
                    _node_format_apply(collector, directory, path, manager, script, str(_mapping(scripts)[script]))
                if kind == "unit":
                    _node_test_files(collector, directory, path, script, scripts[script], paths)
        _node_vitest_files(collector, directory, path, manager, manifest, paths)
        _node_eslint_files(collector, directory, path, manager, berry, manifest, paths, files)
        # After every manifest's kinds, so the MAX_CHECKS bound never drops a kind for a named script.
        named.append((directory, path, manager, scripts or {}, used))
        _relay_first(collector, directory, manager, manifest, scripts)
        _fetched_first(collector, directory, manager, scripts)
        _runtime_first(collector, directory, manager, manifest, files)
    for directory, path, manager, scripts, used in named:
        _node_named_scripts(collector, directory, path, manager, scripts, used)


def _node_format_apply(collector: _Collector, directory: str, path: str, manager: str, script: str, body: str) -> None:
    """``node-format-apply``: the write mode of a ``format:check`` script that runs ``prettier --check``.

    The format kind runs the script (``pnpm run format:check``), whose argv names no formatter, so
    :func:`_apply_formatters` offered no write sibling and a red format check was fixed through a
    hand-written node script (gap no-format-write-check). The script's own prettier call is run in write
    mode through the manager's binary runner (:data:`_NODE_EXEC`: ``yarn prettier``, ``pnpm exec prettier``,
    ``npx --no-install prettier``), on the changed files by default like every prettier write.
    """
    if _SHELL_CHARS & set(body):
        return  # a chained or piped script: its formatter call is not the whole command
    try:
        tokens = tuple(shlex.split(body))
    except ValueError:
        return
    written, tool = _write_argv(tokens)
    runner = _NODE_EXEC.get(manager)
    if tool != "prettier" or not runner or posixpath.basename(tokens[0]) != "prettier":
        return
    key = (directory, "node", "format-apply")
    if not collector.add(
        directory, "node", "format-apply", [*runner, *written], f"{path} scripts.{script} (write mode)"
    ):
        return
    spec = collector.found[key]
    collector.found[key] = replace(
        spec,
        report="lines",
        report_path="",
        description=f"write mode of scripts.{script} (prettier --write): rewrites the files it would refuse"[:200],
        affected="files",
        affected_by_default=True,
    )


#: A ``test:`` script that runs the unit suite under another name (``test:ci-unit``), not a watch or coverage run.
_UNIT_SCRIPT = re.compile(r"^test:(?:[\w.-]+[:-])?unit$")


def _unit_script(scripts: Mapping[str, object]) -> str:
    """The ``test:<...>unit`` script to run the unit kind with when ``test`` is not the unit suite.

    A package whose ``test`` runs its integration config with coverage (opencti-graphql) names its
    unit run ``test:ci-unit``: the unit check runs that one, so it needs no service and no coverage provider.
    """
    names = sorted(n for n in scripts if isinstance(scripts[n], str) and _UNIT_SCRIPT.match(n))
    return names[0] if names else ""


def _node_test_files(
    collector: _Collector, directory: str, path: str, script: str, body: str, paths: frozenset[str]
) -> None:
    """``node-unit-file``: the Node test runner on the test files given as args, for a wrapper unit script.

    A unit script that runs a wrapper (``node test/run.mjs``) collecting every test file and appending
    them after its own arguments cannot run one file: a path given as args runs beside the whole suite.
    When the wrapper's directory holds a loader (``register.mjs``, ``setup.mjs``...), the check runs
    ``node --import <loader> --test <args>``, the call the wrapper makes, so args are the only files run.
    """
    words = body.split()
    if len(words) != 2 or words[0] != "node" or not _NODE_WRAPPER.match(words[1]):
        return
    wrapper = posixpath.normpath(words[1])
    if wrapper.startswith("../") or _join(directory, wrapper) not in paths:
        return
    home = posixpath.dirname(wrapper)
    loaders = [f"./{_join(home, name)}" for name in _NODE_TEST_LOADERS if _join(directory, _join(home, name)) in paths]
    if not loaders:
        return
    argv = ["node", *(arg for loader in loaders for arg in ("--import", loader)), "--test"]
    collector.add(
        directory, "node", "unit-file", argv, f"{path} scripts.{script} ({wrapper}): pass the test files as args"
    )


#: The bin runner of each package manager, so a check calls the package's own ``vitest``.
_NODE_EXEC = {"yarn": ("yarn",), "pnpm": ("pnpm", "exec"), "npm": ("npx", "--no-install")}
_VITEST_CONFIG = re.compile(r"^vitest\.config\.([\w.-]+\.)?(ts|mts|js|mjs|cjs)$")


def _vitest_unit_config(directory: str, paths: frozenset[str]) -> str:
    """The vitest config of *directory* to run unit tests with: the one whose name holds ``unit``, else the default.

    A package with several configs (``vitest.config.ci-unit.ts`` beside ``vitest.config.integration.ts``)
    keeps its unit tests apart from the ones that need services; ``""`` lets vitest pick its default.
    """
    prefix = f"{directory}/" if directory else ""
    names = sorted(p[len(prefix) :] for p in paths if p.startswith(prefix) and "/" not in p[len(prefix) :])
    configs = [n for n in names if _VITEST_CONFIG.match(n)]
    if len(configs) <= 1:
        return ""
    unit = [n for n in configs if "unit" in n and "integration" not in n]
    return unit[0] if unit else ""


def _node_vitest_files(
    collector: _Collector,
    directory: str,
    path: str,
    manager: str,
    manifest: Mapping[str, object],
    paths: frozenset[str],
) -> None:
    """``node-unit-file`` for a package that depends on vitest: the test files given as args, no coverage.

    The unit script may run the whole suite with coverage (``vitest run --coverage``, a coverage
    provider the install may not hold, minutes and hundreds of temporary files); this check runs
    ``vitest run`` on the args only with coverage off, with the package's unit config when it has several.
    """
    deps = {**_mapping(manifest.get("devDependencies")), **_mapping(manifest.get("dependencies"))}
    if "vitest" not in deps:
        return
    argv = [*_NODE_EXEC.get(manager, _NODE_EXEC["npm"]), "vitest", "run", "--coverage.enabled=false"]
    config = _vitest_unit_config(directory, paths)
    if config:
        argv += ["--config", config]
    collector.add(
        directory,
        "node",
        "unit-file",
        argv,
        f"{path} devDependencies.vitest: pass the test files as args, coverage off",
    )


#: An ESLint configuration file, flat (``eslint.config.mjs``) or legacy (``.eslintrc.json``).
_ESLINT_CONFIG = re.compile(
    r"^(?:eslint\.config\.(?:js|mjs|cjs|ts|mts|cts)|\.eslintrc(?:\.(?:js|cjs|json|yaml|yml))?)$"
)
#: The files ``node-lint-file`` takes with ``affected=true``: the sources ESLint reads.
ESLINT_FILE_GLOBS = ("*.js", "*.jsx", "*.mjs", "*.cjs", "*.ts", "*.tsx", "*.mts", "*.cts")


def _node_eslint_files(
    collector: _Collector,
    directory: str,
    path: str,
    manager: str,
    berry: bool,
    manifest: Mapping[str, object],
    paths: frozenset[str],
    files: Mapping[str, bytes],
) -> None:
    """``node-lint-file``: ESLint on the files given as args alone, for a package linted by ESLint.

    A lint script that names its target (``eslint --max-warnings 0 .``, or ``run lint:lint`` reaching a
    root script that does) lints the whole tree whatever the args: ``yarn lint a.tsx b.tsx`` runs
    ``eslint . a.tsx b.tsx``, and a type-aware config on a large front then exhausts the container's heap
    after minutes (exit 129 near 3 GB). This check calls the package's ESLint on the args only, from the
    package directory so the nearest config applies, and takes the changed sources with ``affected=true``.
    It is offered when an ESLint config sits in the package or above it and the package or an enclosing
    manifest depends on ``eslint``; one held by the workspace root alone runs as ``yarn run -T eslint``.
    A call without args runs on the changed sources (``affected_by_default``), never the whole tree.
    """
    if (
        not any(
            _ESLINT_CONFIG.match(posixpath.basename(p)) for p in paths if posixpath.dirname(p) in _ancestors(directory)
        )
        and "eslintConfig" not in manifest
    ):
        return
    own = {**_mapping(manifest.get("devDependencies")), **_mapping(manifest.get("dependencies"))}
    above = False
    if "eslint" not in own:
        for where in _ancestors(directory)[1:]:
            outer = _json(files.get(_join(where, "package.json")))
            deps = {**_mapping(outer.get("devDependencies")), **_mapping(outer.get("dependencies"))}
            if "eslint" in deps:
                above = True
                break
        if not above:
            return
    if manager == "yarn":
        # A Berry member runs only its own dependencies' bins: the root's ESLint is reached with -T.
        runner: tuple[str, ...] = ("yarn", "run", "-T") if above and berry else ("yarn",)
    else:
        runner = _NODE_EXEC.get(manager, _NODE_EXEC["npm"])
    added = collector.add(
        directory,
        "node",
        "lint-file",
        [*runner, "eslint"],
        f"{path} eslint: pass the files to lint as args, never the whole tree",
    )
    if added:
        key = (directory, "node", "lint-file")
        collector.found[key] = replace(
            collector.found[key], affected="files", affected_globs=ESLINT_FILE_GLOBS, affected_by_default=True
        )


#: The node check kinds that read a Relay project's generated artifacts (``__generated__/*.graphql.ts``).
_RELAY_KINDS = ("lint", "lint-file", "types", "unit", "unit-file")
#: The ``sh -c`` program a Relay-first check runs: the generator, then the check with the call's args.
_RELAY_SHELL = '{generate} && exec "$@"'
#: A shell operator that chains a second command after the first in a package script.
_SHELL_CHAIN = re.compile(r"&&|\|\||[;|]")


def _relay_first(
    collector: _Collector, directory: str, manager: str, manifest: Mapping[str, object], scripts: object
) -> None:
    """Run the package's Relay compiler before its lint, types and unit checks.

    A Relay package imports what ``relay-compiler`` writes under ``__generated__/`` (ignored by git,
    produced in CI by the build or a ``relay`` script): without it every ``*.graphql`` import is
    unresolved. Each such check runs ``sh -c '<manager> <script> && exec "$@"'`` with its own argv
    after, so the call's args still reach the check; the output stays out of the change set
    (``.gitignore``). A check whose argv already runs the script is left as is.

    A script that runs the compiler alone (``relay``) is preferred: xtm-hub's ``build`` is
    ``relay-compiler && next build``, and running it before a unit or lint check built the whole app
    (Google Fonts fetched, failing offline). When only a chained script calls the compiler, its first
    command runs alone through the package manager (``yarn relay-compiler ...``).
    """
    deps = {**_mapping(manifest.get("devDependencies")), **_mapping(manifest.get("dependencies"))}
    named = _mapping(scripts)
    if "relay-compiler" not in deps:
        return
    bodies = {n: str(v) for n, v in named.items() if isinstance(v, str) and v.split()[:1] == ["relay-compiler"]}
    if not bodies:
        return
    alone = sorted(n for n, body in bodies.items() if not _SHELL_CHAIN.search(body))
    if alone:
        script = alone[0]
        generate = [manager, script] if manager == "yarn" else [manager, "run", script]
    else:
        script = "relay-compiler"
        first = _SHELL_CHAIN.split(bodies[min(bodies)], maxsplit=1)[0].split()
        generate = [*_NODE_EXEC.get(manager, _NODE_EXEC["npm"]), *first]
    program = _RELAY_SHELL.format(generate=shlex.join(generate))
    for key, spec in list(collector.found.items()):
        where, stack, kind = key
        if where != directory or stack != "node" or kind not in _RELAY_KINDS or script in spec.argv:
            continue
        note = f" (Relay artifacts generated first: {shlex.join(generate)})"
        collector.found[key] = replace(
            spec,
            argv=("sh", "-c", program, "xtm-relay", *spec.argv),
            description=(spec.description[: 200 - len(note)] + note)[:200],
        )


def _chain_first(
    collector: _Collector, directory: str, kinds: tuple[str, ...], steps: list[list[str]], note: str
) -> None:
    """Run *steps* (argv lists) before each node check of *directory* whose kind is in *kinds*.

    The check becomes ``sh -c '<step> && ... && exec "$@"' xtm-relay <its argv>``, so the call's args
    still reach it; a check already Relay-first gets the steps before its generator. A check whose
    argv already runs a step is left as is.
    """
    if not steps:
        return
    prefix = " && ".join(shlex.join(step) for step in steps)
    for key, spec in list(collector.found.items()):
        where, stack, kind = key
        if where != directory or stack != "node" or kind not in kinds:
            continue
        wrapped = spec.argv[:2] == ("sh", "-c") and len(spec.argv) > 4 and spec.argv[3] == "xtm-relay"
        program = spec.argv[2] if wrapped else _RELAY_SHELL.format(generate="").removeprefix(" && ")
        if prefix in program:
            continue
        tail = spec.argv[4:] if wrapped else spec.argv
        program = f"{prefix} && {program}"
        text = f" ({note})"
        collector.found[key] = replace(
            spec,
            argv=("sh", "-c", program, "xtm-relay", *tail),
            description=(spec.description[: max(0, 200 - len(text))] + text)[:200],
        )


#: A build script step that calls another script of the package: ``run get-x``, ``yarn get-x``, ``npm run get-x``.
_SCRIPT_STEP = re.compile(r"(?:^|&&|;)\s*(?:yarn\s+run|yarn|npm\s+run|pnpm\s+run|pnpm|run)\s+(get-[\w:.-]+)")
#: The node check kinds that read a file a build step fetches into the source tree.
_FETCHED_KINDS = ("lint", "lint-file", "types", "unit", "unit-file")


def _fetched_first(collector: _Collector, directory: str, manager: str, scripts: object) -> None:
    """Run the ``get-*`` scripts the package's build runs before its lint, types and unit checks.

    opencti-graphql's ``build:prod`` starts with ``run get-connectors-manifest``, which writes
    ``src/__generated__/opencti-manifest.json`` (git-ignored) that the sources import: without it the
    type check fails on TS2307. Each ``get-*`` script a ``build*`` script calls runs first, in order.
    """
    named = _mapping(scripts)
    found: list[str] = []
    for name in sorted(named):
        body = named[name]
        if not name.startswith("build") or not isinstance(body, str):
            continue
        for step in _SCRIPT_STEP.findall(body):
            if step in named and step not in found:
                found.append(step)
    steps = [[manager, step] if manager == "yarn" else [manager, "run", step] for step in found]
    _chain_first(collector, directory, _FETCHED_KINDS, steps, f"build steps run first: {', '.join(found)}")


#: The node check kinds that load the package at runtime: its native add-ons and interpreter packages.
_RUNTIME_KINDS = ("unit", "unit-file")
#: A package script that installs what its runtime needs beside node_modules (``install:python``).
_INSTALL_SCRIPT = re.compile(r"^install:[\w.-]+$")


def _runtime_first(
    collector: _Collector,
    directory: str,
    manager: str,
    manifest: Mapping[str, object],
    files: Mapping[str, bytes],
) -> None:
    """Build the native add-ons and run the ``install:*`` scripts before the package's unit checks.

    The install runs ``yarn install --mode=skip-build`` (no dependency builds for every check), so an
    add-on the workspace root marks ``dependenciesMeta.<name>.built: true`` (opencti-graphql's
    ``node-calls-python``) has no ``build/Release`` and its import fails. Each such add-on the package
    depends on is rebuilt first (``yarn rebuild <names>``, yarn only), then each ``install:*`` script
    (``install:python``: the pip packages its Python bridge imports), as the CI unit job does.
    """
    deps = {**_mapping(manifest.get("devDependencies")), **_mapping(manifest.get("dependencies"))}
    built: set[str] = set()
    for where in _ancestors(directory):
        meta = _mapping(_json(files.get(_join(where, "package.json"), b"")).get("dependenciesMeta"))
        built |= {name for name, value in meta.items() if _mapping(value).get("built") is True}
    natives = sorted(name for name in built if name in deps)
    steps: list[list[str]] = [["yarn", "rebuild", *natives]] if natives and manager == "yarn" else []
    scripts = _mapping(manifest.get("scripts"))
    installs = sorted(n for n in scripts if isinstance(scripts[n], str) and _INSTALL_SCRIPT.match(n))
    steps += [[manager, n] if manager == "yarn" else [manager, "run", n] for n in installs]
    words = [*(f"rebuild {n}" for n in natives if manager == "yarn"), *installs]
    _chain_first(collector, directory, _RUNTIME_KINDS, steps, f"runtime first: {', '.join(words)}")


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _node_named_scripts(collector: _Collector, directory: str, path: str, manager: str, scripts: dict, used: set[str]) -> None:
    """The namespaced or hyphenated generator and gate scripts of a manifest (:data:`_NODE_SCRIPT_VERBS`), one check each.

    A script that runs a code generator (:data:`_NODE_CODEGEN`) is one too, namespaced or not.

    Named ``node-<script>`` (``check:bundle-size`` gives ``node-check-bundle-size``, a member's
    ``i18n:check`` gives ``front:node-i18n-check``), listed after the kinds and run after the install
    of their directory like any other node check.
    """
    # A script a workflow line or a kind already runs from this directory is not offered twice.
    used = used | {w for (d, s, _k), spec in collector.found.items() if d == directory and s == "node" for w in spec.argv}
    for script in sorted(scripts):
        body = scripts[script]
        lowered = script.lower()
        segments = set(_NODE_SCRIPT_SPLIT.split(lowered))
        if script in used or not isinstance(body, str):
            continue
        codegen = bool(_NODE_CODEGEN.search(body))
        if not codegen and (len(segments) < 2 or not (_NODE_SCRIPT_VERBS | _NODE_SCRIPT_NOUNS) & segments):
            continue
        if (
            any(word in lowered for word in _SCRIPT_SKIP)
            or "no test specified" in body
            or _NODE_SCRIPT_URL.search(body)
        ):
            continue
        kind = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
        if not kind or kind.endswith("install") or kind in KINDS:
            continue
        argv = [manager, script] if manager == "yarn" else [manager, "run", script]
        collector.add(directory, "node", kind, argv, f"{path} scripts.{script}")


def _has(paths: frozenset[str], directory: str, *names: str) -> bool:
    return any(_join(directory, name) in paths for name in names)


def _python(collector: _Collector, paths: frozenset[str], files: Mapping[str, bytes]) -> None:
    dirs = set()
    for path in paths:
        name = posixpath.basename(path)
        if (name in ("pyproject.toml", "setup.py", "setup.cfg") or _REQUIREMENTS.match(name)) and path.count("/") <= MAX_DEPTH and not _skipped(path):
            dirs.add(posixpath.dirname(path))
    for directory in sorted(dirs):
        project = _toml(files.get(_join(directory, "pyproject.toml")))
        tool = project.get("tool") if isinstance(project.get("tool"), dict) else {}
        setup_cfg = _text(files.get(_join(directory, "setup.cfg")))
        above = _ancestors(directory)
        source = _join(directory, "pyproject.toml") if project else _join(directory, "requirements")
        pinned = _minor(next(iter(_text(files.get(_join(directory, ".python-version"))).split()), ""))
        requires = _dig(project, "project", "requires-python")
        if pinned or isinstance(requires, str):
            collector.project_pythons[directory] = (pinned, requires if isinstance(requires, str) and not pinned else "")
        if any(_has(paths, a, "uv.lock") for a in above):
            prefix, install = ["uv", "run"], ["uv", "sync", "--locked"]
        elif _has(paths, directory, "poetry.lock") or "poetry" in tool:
            prefix, install = ["poetry", "run"], ["poetry", "install", "--no-interaction"]
            if _poetry_extras(project):
                # The tests import the optional dependencies CI installs with them (gap poetry-unit-check-no-dev-extras).
                install.append("--all-extras")
        else:
            prefix = []
            requirements = sorted(
                posixpath.basename(p) for p in paths if posixpath.dirname(p) == directory and _REQUIREMENTS.match(posixpath.basename(p))
            )
            if requirements:
                install = ["python", "-m", "pip", "install"] + [arg for r in requirements for arg in ("-r", r)]
            else:
                install = ["python", "-m", "pip", "install", "-e", "."]
        collector.add(directory, "python", "install", install, source)
        if "ruff" in tool or any(_has(paths, a, "ruff.toml", ".ruff.toml") for a in above):
            collector.add(
                directory,
                "python",
                "lint",
                [*prefix, "ruff", "check", ".", "--output-format", "sarif", "-o", ".xtm/ruff.sarif"],
                f"{source} (ruff)",
                report="sarif",
                report_path=".xtm/ruff.sarif",
            )
        elif _has(paths, directory, ".flake8", "tox.ini") or "[flake8]" in setup_cfg:
            collector.add(directory, "python", "lint", [*prefix, "flake8", "."], f"{source} (flake8)")
        if "mypy" in tool or _has(paths, directory, "mypy.ini", ".mypy.ini") or "[mypy" in setup_cfg:
            argv = [*prefix, "mypy"] if isinstance(tool.get("mypy"), dict) and tool["mypy"].get("files") else [*prefix, "mypy", "."]
            collector.add(directory, "python", "types", argv, f"{source} (mypy)")
        tests = any(p.startswith((_join(directory, "tests/"), _join(directory, "test/"))) for p in paths)
        if tests or "pytest" in tool or _has(paths, directory, "pytest.ini", "conftest.py"):
            collector.add(
                directory,
                "python",
                "unit",
                [*prefix, "python", "-m", "pytest", "-q", "--junitxml=.xtm/pytest.xml"],
                f"{source} (pytest)",
                report="junit",
                report_path=".xtm/pytest.xml",
            )
        elif _has(paths, directory, "manage.py"):
            # A Django project with no pytest: its tests run through Django's runner (a database
            # the CI services start is named in the listing's services part).
            collector.add(
                directory,
                "python",
                "unit",
                [*prefix, "python", "manage.py", "test", "--noinput"],
                f"{source} (manage.py test)",
            )


def _poetry_extras(project: dict[str, Any]) -> bool:
    """Whether a Poetry ``pyproject.toml`` declares extras (``[tool.poetry.extras]`` or ``[project.optional-dependencies]``)."""
    extras = (_dig(project, "tool", "poetry", "extras"), _dig(project, "project", "optional-dependencies"))
    return any(isinstance(value, dict) and value for value in extras)


def _roots(paths: Iterable[str], names: tuple[str, ...]) -> list[str]:
    """Directories holding one of *names* with no such file above them (a multi-module build's root)."""
    dirs = {posixpath.dirname(p) for p in paths if posixpath.basename(p) in names and p.count("/") <= MAX_DEPTH and not _skipped(p)}
    return sorted(d for d in dirs if not any(a in dirs for a in _ancestors(d)[1:]))


def _jvm(collector: _Collector, paths: frozenset[str], files: Mapping[str, bytes]) -> None:
    for directory in _roots(paths, ("pom.xml",)):
        pom = _text(files.get(_join(directory, "pom.xml")))
        mvn = ["sh", "mvnw"] if _has(paths, directory, "mvnw") else ["mvn"]
        source = _join(directory, "pom.xml")
        collector.add(directory, "jvm", "install", [*mvn, "-B", "dependency:go-offline"], source)
        if "spotless-maven-plugin" in pom:
            collector.add(directory, "jvm", "lint", [*mvn, "-B", "spotless:check"], f"{source} (spotless)")
        collector.add(directory, "jvm", "unit", [*mvn, "-B", "test"], source)
        collector.add(directory, "jvm", "build", [*mvn, "-B", "-DskipTests", "package"], source)
    gradle_files = ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")
    for directory in _roots(paths, gradle_files):
        script = "".join(_text(files.get(_join(directory, name))) for name in gradle_files[:2])
        gradle = ["sh", "gradlew"] if _has(paths, directory, "gradlew") else ["gradle"]
        source = _join(directory, "build.gradle")
        if "spotless" in script:
            collector.add(directory, "jvm", "lint", [*gradle, "--no-daemon", "spotlessCheck"], f"{source} (spotless)")
        collector.add(directory, "jvm", "unit", [*gradle, "--no-daemon", "test"], source)
        collector.add(directory, "jvm", "build", [*gradle, "--no-daemon", "build", "-x", "test"], source)


#: The files a Rust format write takes with ``affected=true``.
RUST_FORMAT_GLOBS = ("*.rs",)
#: The edition rustfmt parses with when a ``Cargo.toml`` names none (Cargo's own default).
_RUST_DEFAULT_EDITION = "2015"


def _declared_edition(manifest: dict[str, Any]) -> str | None:
    """The edition a ``Cargo.toml`` names itself (``[package]``, else ``[workspace.package]``); ``None`` else.

    A member's ``edition.workspace = true`` is a table, not a name: it inherits the root's and counts as none.
    """
    for edition in (_dig(manifest, "package", "edition"), _dig(manifest, "workspace", "package", "edition")):
        if isinstance(edition, str) and edition.isdigit():
            return edition
    return None


def _rust_edition(directory: str, files: Mapping[str, bytes]) -> str:
    """The edition rustfmt parses the changed files of the Cargo root *directory* with.

    The root manifest's own edition first. A virtual workspace (``[workspace]`` with ``members``, no
    ``[workspace.package].edition``) names none: each member declares its own, so the newest edition among
    the member manifests below *directory* is taken (a newer edition parses the older syntax, 2015 does not
    parse ``async fn`` or ``.await``). Cargo's default (2015) only when no manifest names one.
    """
    root = _declared_edition(_toml(files.get(_join(directory, "Cargo.toml"))))
    if root:
        return root
    prefix = f"{directory}/" if directory else ""
    members = [
        _declared_edition(_toml(data))
        for path, data in files.items()
        if posixpath.basename(path) == "Cargo.toml" and path.startswith(prefix) and not _skipped(path)
    ]
    return max((edition for edition in members if edition), key=int, default=_RUST_DEFAULT_EDITION)


def _rust(collector: _Collector, paths: frozenset[str], files: Mapping[str, bytes]) -> None:
    for directory in _roots(paths, ("Cargo.toml",)):
        source = _join(directory, "Cargo.toml")
        locked = ["--locked"] if _has(paths, directory, "Cargo.lock") else []
        collector.add(directory, "rust", "install", ["cargo", "fetch", *locked], source)
        collector.add(directory, "rust", "lint", ["cargo", "clippy", "--all-targets", "--", "-D", "warnings"], source)
        collector.add(directory, "rust", "format", ["cargo", "fmt", "--all", "--check"], source)
        _rust_format_apply(collector, directory, source, _rust_edition(directory, files))
        collector.add(directory, "rust", "unit", ["cargo", "test", *locked], source)
        collector.add(directory, "rust", "build", ["cargo", "build", *locked], source)


def _rust_format_apply(collector: _Collector, directory: str, source: str, edition: str) -> None:
    """``rust-format-apply``: rustfmt on the changed ``.rs`` files, never ``cargo fmt --all`` over the workspace.

    The write sibling :func:`_apply_formatters` derived from ``cargo fmt --all --check`` dropped ``--check``
    and rewrote every crate: on a repository whose main is not fmt-clean a one-file fix rewrote 33 files
    (xtm-composer; gaps rust-format-apply-rewrites-whole-repo, rust-format-apply-whole-crate). rustfmt takes
    files, reads the same ``rustfmt.toml``, and parses with the manifest's edition (its own default is 2015).
    """
    key = (directory, "rust", "format-apply")
    argv = ["rustfmt", "--edition", edition]
    if not collector.add(directory, "rust", "format-apply", argv, f"{source} (rustfmt write mode)"):
        return
    collector.found[key] = replace(
        collector.found[key],
        description=f"write mode of rust-format: rustfmt --edition {edition} on the changed .rs files; args: other files",
        affected="files",
        affected_globs=RUST_FORMAT_GLOBS,
        affected_by_default=True,
    )


def _dbt(collector: _Collector, paths: frozenset[str]) -> None:
    for directory in _roots(paths, ("dbt_project.yml",)):
        source = _join(directory, "dbt_project.yml")
        if _has(paths, directory, "packages.yml", "dependencies.yml"):
            collector.add(directory, "dbt", "install", ["dbt", "deps"], source)
        collector.add(directory, "dbt", "parse", ["dbt", "parse"], f"{source} (needs a profile: profiles.yml or DBT_PROFILES_DIR)")
        if any(_has(paths, a, ".sqlfluff") for a in _ancestors(directory)):
            collector.add(directory, "dbt", "lint", ["sqlfluff", "lint", "."], f"{source} (sqlfluff)")


def _helm(collector: _Collector, paths: frozenset[str]) -> None:
    """``helm-lint`` and ``helm-parse`` (``helm template``) for every chart root (a ``Chart.yaml``; subcharts under it are its own).

    Both read the chart offline; a chart whose ``dependencies`` are not vendored under ``charts/``
    needs ``helm dependency build`` first, which fetches from the chart repositories.
    """
    for directory in _roots(paths, ("Chart.yaml",)):
        source = _join(directory, "Chart.yaml")
        collector.add(directory, "helm", "lint", ["helm", "lint", "."], source)
        collector.add(directory, "helm", "parse", ["helm", "template", "xtm-check", "."], f"{source} (helm template)")


def _make(collector: _Collector, files: Mapping[str, bytes]) -> None:
    for path in sorted(p for p in files if posixpath.basename(p) in ("Makefile", "makefile", "GNUmakefile")):
        directory = posixpath.dirname(path)
        stack = collector.stack_of(directory)
        targets = []
        for line in _text(files[path]).splitlines():
            match = _MAKE_TARGET.match(line)
            if match and match.group(1) in _MAKE_KINDS and match.group(1) not in targets:
                targets.append(match.group(1))
        for target in targets:
            collector.add(directory, "make", _MAKE_KINDS[target], ["make", target], f"{path} target {target}", runs_on=stack)


def _script_tests(collector: _Collector, paths: frozenset[str]) -> None:
    """One ``<dir>:python-test`` per test requirements file, run through the workflow's per-project script.

    The check runs from the repository root, as CI does, with the script and
    the requirements file as arguments (:data:`SCRIPT_TEST_SHELL`).
    """
    for script, source in sorted(collector.scripts.items()):
        if script not in paths:
            continue
        for requirements in sorted(p for p in paths if _TEST_REQUIREMENTS.match(posixpath.basename(p)) and not _skipped(p)):
            directory = posixpath.dirname(requirements)
            key = (directory, "python", "test")
            if not directory or key in collector.found or len(collector.found) >= MAX_CHECKS:
                continue
            collector.found[key] = CheckSpec(
                name=f"{directory}:python-test",
                argv=("sh", "-c", SCRIPT_TEST_SHELL, "xtm-test", script, requirements, LOCAL_REQUIREMENTS_SCRIPT),
                cwd="",
                timeout=min(_TIMEOUTS["test"], MAX_TIMEOUT),
                image=collector.image("python"),
                report="junit",
                report_path=".xtm/junit.xml",
                description=f"bash {script} {requirements} ({source})"[:200],
            )


#: The files ``actions-lint`` takes with ``affected=true`` (globs on the path from the checkout root, or the name).
ACTIONS_LINT_GLOBS = (".github/workflows/*.yml", ".github/workflows/*.yaml", "action.yml", "action.yaml")


def actions_lint_check(paths: Iterable[str], *, images: Iterable[str] = ()) -> CheckSpec | None:
    """The ``actions-lint`` check of a checkout holding a workflow or a composite action, else ``None``.

    Read from the listing alone, so a checkout that declares its checks in
    ``.xtm-one/checks.toml`` gets it too (:func:`commands.checkout_checks`):
    the YAML, the shape and the shell of the ``run:`` blocks are what no
    manifest check reads. It runs from the checkout root on the Python
    toolchain image (bash and python are in every image), needs no install,
    and takes the changed workflow and action files with ``affected=true``.
    """
    files = sorted(p for p in paths if p and actions_lint.is_actions_file(p) and not _skipped(p))
    if not files:
        return None
    image = _Collector(frozenset(images)).image("python")
    return CheckSpec(
        name="actions-lint",
        argv=("python", "-c", ACTIONS_LINT_SCRIPT),
        cwd="",
        timeout=min(_TIMEOUTS["lint"], MAX_TIMEOUT),
        image=image,
        report="sarif",
        report_path=actions_lint.REPORT_PATH,
        description=f"YAML parse and shape, actionlint and bash -n on the run: blocks of {len(files)} workflow or action file(s)",
        affected="files",
        affected_globs=ACTIONS_LINT_GLOBS,
        install_first=False,
    )


def _actions_lint(collector: _Collector, paths: frozenset[str]) -> None:
    """``actions-lint`` when the checkout holds a workflow or a composite action (:func:`actions_lint_check`)."""
    key = ("", "actions", "lint")
    if key in collector.found or len(collector.found) >= MAX_CHECKS:
        return
    spec = actions_lint_check(paths, images=collector.images)
    if spec is not None:
        collector.found[key] = spec


#: The check-mode options a formatter drops to write (black, isort, ``ruff format``, ``cargo fmt``).
_FORMATTER_CHECK_FLAGS = {
    "black": frozenset({"--check", "--diff"}),
    "isort": frozenset({"--check-only", "--check", "-c", "--diff"}),
    "ruff": frozenset({"--check", "--diff"}),
    "cargo": frozenset({"--check"}),
}
#: The check-mode options prettier swaps for ``--write``.
_PRETTIER_CHECK_FLAGS = frozenset({"--check", "-c", "--list-different", "-l"})
#: The check goals of a JVM formatter and their write goals.
_JVM_APPLY_GOALS = {"spotless:check": "spotless:apply", "spotlessCheck": "spotlessApply"}
#: The files a Python formatter's write check takes with ``affected=true``.
PYTHON_FORMAT_GLOBS = ("*.py", "*.pyi")


def _write_argv(argv: tuple[str, ...]) -> tuple[tuple[str, ...], str]:
    """(the write-mode argv of a formatter's check-mode *argv*, the tool), or ``((), "")`` when it is none.

    ``black --check``, ``isort --check-only``, ``ruff format --check`` and ``cargo fmt --check`` lose their
    check option, ``prettier --check`` gets ``--write``, ``spotless:check`` becomes ``spotless:apply``.
    """
    for index, token in enumerate(argv):
        if index and argv[index - 1] in _UV_WITH_OPTIONS:
            continue  # `uv run --with black --with isort isort --check-only`: a package, not the tool run
        if token in _JVM_APPLY_GOALS:
            return (*argv[:index], _JVM_APPLY_GOALS[token], *argv[index + 1 :]), "spotless"
        tool = posixpath.basename(token)
        rest = argv[index + 1 :]
        if tool == "prettier" and any(t in _PRETTIER_CHECK_FLAGS for t in rest):
            written = tuple(t for t in rest if t not in _PRETTIER_CHECK_FLAGS and t != "--write")
            return (*argv[: index + 1], "--write", *written), tool
        flags = _FORMATTER_CHECK_FLAGS.get(tool)
        if flags is None or (tool == "ruff" and rest[:1] != ("format",)) or (tool == "cargo" and rest[:1] != ("fmt",)):
            continue
        kept = tuple(t for t in rest if t not in flags)
        return ((*argv[: index + 1], *kept), tool) if len(kept) < len(rest) else ((), "")
    return (), ""


def _apply_formatters(collector: _Collector) -> None:
    """A ``<check>-apply`` check next to every detected formatter check that runs in check mode.

    CI runs a formatter with ``--check``; the sandbox ran only that, so a red format check was fixed by
    hand with one edit per hunk (connectors: three black diffs). The write check runs the same tool, in
    the same directory, image and environment, without its check option; a Python or prettier one takes
    the changed files by default (``affected_by_default``), so it never reformats what the change did not
    touch. Its ``args`` name other files, or ``.`` for the whole directory.
    """
    for (directory, stack, kind), spec in list(collector.found.items()):
        key = (directory, stack, f"{kind}-apply")
        if kind.endswith("-apply") or key in collector.found or len(collector.found) >= MAX_CHECKS:
            continue
        argv, tool = _write_argv(spec.argv)
        if not argv:
            continue
        files = tool in ("black", "isort", "ruff", "prettier")
        collector.found[key] = replace(
            spec,
            name=f"{spec.name}-apply",
            argv=argv,
            report="lines",
            report_path="",
            description=f"write mode of {spec.name} ({tool}): rewrites the files it would refuse"[:200],
            affected="files" if files else spec.affected,
            affected_globs=PYTHON_FORMAT_GLOBS if tool in ("black", "isort", "ruff") else (),
            affected_by_default=files,
        )


#: The lockfile-only update of each Node package manager's locked install, by the install's argv head.
_RELOCK_ARGV = {
    ("yarn", "install", "--immutable"): ("yarn", "install", "--mode=update-lockfile"),
    ("yarn", "install", "--frozen-lockfile"): ("yarn", "install", "--ignore-scripts"),
    ("pnpm", "install", "--frozen-lockfile"): ("pnpm", "install", "--lockfile-only"),
    ("npm", "ci"): ("npm", "install", "--package-lock-only", "--ignore-scripts"),
}


def _relock_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    """The lockfile update of a locked Node install *argv*, ``()`` when it is none (a pinned pnpm keeps its runner)."""
    runner: tuple[str, ...] = ()
    head = argv
    if argv[:1] == ("corepack",) and len(argv) > 1 and argv[1].startswith("pnpm@"):
        runner, head = argv[:2], ("pnpm", *argv[2:])
    for locked, relock in _RELOCK_ARGV.items():
        if head[: len(locked)] == locked:
            relocked = (*relock, *(a for a in head[len(locked) :] if a != "--immutable"))
            return (*runner, *relocked[1:]) if runner else relocked
    return ()


def _node_relock(collector: _Collector) -> None:
    """A ``<dir>:node-relock`` check next to every locked Node install (``--immutable``, ``--frozen-lockfile``, ``npm ci``).

    A dependency or pin bump needs its lockfile regenerated, and the install check refuses a lock that
    no longer matches: the lock was edited by hand. The relock runs the same manager in lock-update mode
    (``yarn install --mode=update-lockfile``, ``pnpm install --lockfile-only``, ``npm install
    --package-lock-only``), needs no install first, and is not an install, so the lockfile it rewrites is
    the change ``from_checkout`` commits.
    """
    for (directory, stack, kind), spec in list(collector.found.items()):
        key = (directory, stack, "relock")
        if stack != "node" or kind != "install" or key in collector.found or len(collector.found) >= MAX_CHECKS:
            continue
        argv = _relock_argv(spec.argv)
        if not argv:
            continue
        collector.found[key] = replace(
            spec,
            name=f"{directory}:node-relock" if directory else "node-relock",
            argv=argv,
            default_args=(),
            description=f"lockfile update of {spec.name}: rewrites the lock after a manifest change"[:200],
            install_first=False,
        )


#: A git command that reads or writes the repository (``git status``, ``execSync('git diff ...')``): a script
#: holding one needs a ``.git``, which a checkout has not.
_GIT_CALL = re.compile(
    r"(?<![\w./-])git\s+(?:-C\s+\S+\s+)?"
    r"(?:status|diff|ls-files|rev-parse|log|show|merge-base|describe|stash|add|commit|checkout|blame|grep)\b"
)
#: A repository file a package.json script runs (``tsx scripts/check.ts``, ``node bin/x.mjs``, ``bash run.sh``).
_SCRIPT_FILE = re.compile(
    r"(?:^|[\s;&|(])(?:tsx|ts-node|node|bun|bash|sh|python3?)\s+(?:-{1,2}[\w-]+(?:=\S+)?\s+)*"
    r"([\w@./-]+\.(?:ts|mts|cts|js|mjs|cjs|sh|py))(?![\w.])"
)
#: Repository files :func:`script_files` names at most.
MAX_SCRIPT_FILES = 40


def _manifest_scripts(files: Mapping[str, bytes]) -> dict[str, tuple[str, dict[str, str]]]:
    """Every read ``package.json`` by directory: its package ``name`` and its string scripts."""
    found: dict[str, tuple[str, dict[str, str]]] = {}
    for path in sorted(p for p in files if posixpath.basename(p) == "package.json"):
        manifest = _json(files[path])
        scripts = {k: v for k, v in _mapping(manifest.get("scripts")).items() if isinstance(v, str)}
        name = manifest.get("name") if isinstance(manifest.get("name"), str) else ""
        found[posixpath.dirname(path)] = (name, scripts)
    return found


def _run_files(directory: str, body: str) -> list[str]:
    """The repository paths (from the checkout root) of the files the script *body* of *directory* runs."""
    out = []
    for match in _SCRIPT_FILE.finditer(body):
        rel = posixpath.normpath(_join(directory, match.group(1)))
        if not rel.startswith("../") and rel != "..":
            out.append(rel)
    return out


def script_files(paths: Iterable[str], files: Mapping[str, bytes]) -> list[str]:
    """The repository files the read ``package.json`` scripts run, present in the listing *paths*, bounded.

    Read after :func:`candidates` and given to :func:`detect` with them, so a check whose script runs
    a file that calls git is detected as one (:func:`_apply_git_repo`).
    """
    listing = frozenset(paths)
    picked: list[str] = []
    for directory, (_name, scripts) in _manifest_scripts(files).items():
        for body in scripts.values():
            for rel in _run_files(directory, body):
                if rel in listing and rel not in picked and not _skipped(rel):
                    picked.append(rel)
    return picked[:MAX_SCRIPT_FILES]


def _check_script(spec: CheckSpec, manifests: Mapping[str, tuple[str, dict[str, str]]]) -> tuple[str, str] | None:
    """The (directory, script name) a node check runs: ``yarn x``, ``npm run x``, ``yarn workspace <name> x``."""
    argv = list(spec.argv)
    if len(argv) < 2 or posixpath.basename(argv[0]) not in ("yarn", "npm", "pnpm", "bun"):
        return None
    directory = spec.cwd
    rest = argv[1:]
    if rest[0] == "workspace" and len(rest) >= 3:
        directory = next((d for d, (name, _s) in manifests.items() if name and name == rest[1]), "")
        if not directory:
            return None
        rest = rest[2:]
    if rest[0] in ("run", "run-script") and len(rest) >= 2:
        rest = rest[1:]
    return directory, rest[0]


def _apply_git_repo(collector: _Collector, files: Mapping[str, bytes]) -> None:
    """``git_repo`` on every node check whose script calls git, itself or through a file it runs (#1958).

    A checkout has no ``.git``: the check then runs in a throwaway repository of the checkout
    (:data:`checks.GIT_REPO_SHELL`), as a declared ``git_repo = true`` check does and as CI runs it in a clone.
    """
    manifests = _manifest_scripts(files)
    for key, spec in list(collector.found.items()):
        if key[1] != "node" or spec.git_repo:
            continue
        found = _check_script(spec, manifests)
        if found is None:
            continue
        directory, script = found
        body = manifests.get(directory, ("", {}))[1].get(script, "")
        if not body:
            continue
        sources = [body, *(_text(files.get(rel)) for rel in _run_files(directory, body))]
        if any(_GIT_CALL.search(text) for text in sources):
            collector.found[key] = replace(spec, git_repo=True)


def _compose(files: Mapping[str, bytes], infra: list[Infra]) -> None:
    for path in sorted(p for p in files if _COMPOSE.match(posixpath.basename(p))):
        services = _yaml(files[path]).get("services")
        for name, service in (services or {}).items() if isinstance(services, dict) else ():
            if isinstance(service, dict):
                image = service.get("image") or ("(built from the repository)" if service.get("build") else "")
                infra.append(Infra(str(name), str(image), path))


def detect(paths: Iterable[str], files: Mapping[str, bytes], *, images: Iterable[str] = ()) -> Detection:
    """The checks and service infrastructure of a checkout.

    *paths* lists the checkout's files relative to its root; *files* holds the
    contents of the :func:`candidates` among them; *images* the configured image
    keys (a stack runs on the first of its :data:`STACK_IMAGES` keys configured).
    """
    listing = frozenset(p for p in paths if p)
    collector = _Collector(frozenset(images))
    infra: list[Infra] = []
    dirs = frozenset(d for p in listing for d in _ancestors(posixpath.dirname(p)) if d)
    _workflows(collector, files, infra, dirs)
    _circleci(collector, files, infra, dirs)
    _node(collector, listing, files)
    _python(collector, listing, files)
    _jvm(collector, listing, files)
    _rust(collector, listing, files)
    _dbt(collector, listing)
    _helm(collector, listing)
    _make(collector, files)
    _script_tests(collector, listing)
    _apply_pythons(collector)
    _apply_pnpm(collector, files)
    _apply_formatters(collector)
    _node_relock(collector)
    _apply_git_repo(collector, files)
    _actions_lint(collector, listing)
    _compose(files, infra)
    unique = list(dict.fromkeys(row for row in infra if row.name))[:MAX_INFRA]
    return Detection(checks=collector.checks(), infra=unique)


def describe_infra(infra: list[Infra]) -> str:
    """One line per service the repository's CI or compose files start, for a listing."""
    return "\n".join(f"- {row.name}: {row.image or '(no image)'} ({row.source})" for row in infra)
