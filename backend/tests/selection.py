"""Which tests a change can break: ``python -m tests.selection {ci,e2e}``.

The merge queue runs a suite only where its change reaches. The ``select``
job of ``ci.yml`` and of ``e2e.yml`` calls this module on the merge group's
own change (``merge_group.base_sha..sha``) and the jobs read its answer. Any
other event runs everything (``--full``), and the nightly run on main is the
full pass that catches what the rules below cannot see.

Backend tests are read from the source, not from a coverage run. A module
reaches what it imports as it loads, at any depth; an ``app`` function's own
import counts one step, from a changed module only (followed further, the one
function that imports ``app.main`` would put every module in every closure).
A test file reaches everything it imports or names as a dotted string
(``mock.patch("app.x.y")``). Code that runs at import - a Pydantic schema, a
constant, a registry - is reached the same way, where a coverage map would
miss it: its lines run during collection, under no test. On top of the graph a
test file runs when:

- the change adds or edits it;
- the test names the changed file as a path (``"agent_import.py"``), a module
  as well as a data file: a tripwire reads a module's source by its path;
- the change edits a ``.py`` file of ``app/`` and the file reads the whole
  tree rather than importing it (``tests/app_sources.py``, a package walk,
  ``Path(app.__file__).rglob``);
- the change adds, edits or deletes a test file and the file walks the test
  files (``glob("test_*.py")``);
- the change edits any other file and the test names its directory or its
  area as a path (``frontend``, ``.github``, a data file's own directories);
- the test lists every tracked file (``git ls-files``), whatever the change.

``scripts/`` is read with ``app/`` and ``tests/``: a test that imports a script
reaches what the script imports.

A change to what every test runs under - a ``conftest.py`` and the helpers of
``tests/`` it loads, the dependencies, this module, the workflow - runs the
whole suite. So does a data file under ``app/`` that no module names, and a
file the graph cannot parse.

``dev-docs/ci-test-selection.md`` holds the rules for every suite.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import re
import subprocess
import sys
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent

#: Every backend test runs under these: a change to one runs the whole suite.
BACKEND_FULL_INPUTS = re.compile(
    r"^(backend/tests/(.+/)?conftest\.py"
    r"|backend/(pyproject\.toml|uv\.lock)"
    r"|backend/tests/(selection|sharding|marked_files)\.py"
    r"|\.github/workflows/ci\.yml"
    r"|\.github/actions/(backend-venv|uv-sync)/.*)$"
)

#: What the integration suite's database is built from: a change here runs it whole.
DATABASE_INPUTS = re.compile(r"^backend/(alembic/|app/models/|app/core/(database|migrations)\.py$)")

#: What the Migrations job reads besides the ``app`` modules the chain loads (``migration_chain_modules``):
#: it runs only when one changes (the chain is otherwise main's, green). The models stay listed for a deletion.
MIGRATION_INPUTS = re.compile(
    r"^(backend/alembic/|backend/app/models/"
    r"|backend/tests/(test_migration_integrity|test_fresh_install|selection)\.py$"
    r"|backend/(pyproject\.toml|uv\.lock)$"
    r"|\.github/workflows/ci\.yml$|\.github/actions/(backend-venv|uv-sync)/)"
)

#: What the Migrations job calls besides the chain (``Prepare shared DB``, ``Idempotent re-run``).
MIGRATION_RUNNER = "app.core.migrations"

#: Every vitest file runs under these, and under what the setup files import (``setup_closure``).
FRONTEND_UNIT_FULL_INPUTS = re.compile(
    r"^(frontend/(package\.json|yarn\.lock|\.yarnrc\.yml|vite\.config\.ts|vitest\.config\.ts|vitest\.setup\.ts"
    r"|tests_unit/setup\.ts|tsconfig[^/]*\.json|scripts/node-test-files\.mjs)"
    r"|\.github/workflows/ci\.yml|backend/tests/selection\.py)$"
)

#: The vitest setup files, from ``frontend/`` (``setupFiles`` in ``vitest.config.ts``): they run before every test.
FRONTEND_SETUP_FILES = ("vitest.setup.ts", "tests_unit/setup.ts")
#: A relative import of a script: ``from "./x"``, ``import "../y"``, ``import("./z")``.
_RELATIVE_IMPORT = re.compile(r"""(?:\bfrom|\bimport)\s*\(?\s*["'](\.{1,2}/[^"']+)["']""")

#: What a vitest run can import: the files ``vitest related`` follows.
FRONTEND_UNIT_SOURCES = re.compile(r"^frontend/(src|tests_unit)/")

#: A vitest file, or a helper of one, that reads files from disk, which ``vitest related`` cannot see.
_READS_FILES = re.compile(r"""(?:from|require\()\s*["'](?:node:)?fs(?:/promises)?["']""")
#: The files a vitest run can load.
_SCRIPT_SUFFIXES = frozenset({".ts", ".tsx", ".mts", ".js", ".jsx", ".mjs"})
#: A string literal of a script: ``"..."``, ``'...'`` or a template.
_LITERAL = re.compile(r""""([^"\n]*)"|'([^'\n]*)'|`([^`]*)`""")
#: A script that lists a directory: it reads every file of the area it names, whatever their names.
_LISTS_A_DIRECTORY = re.compile(r"\breaddir(?:Sync)?\(|\bglob(?:Sync)?\(|\bopendir(?:Sync)?\(")

#: A change that never reaches the E2E stack: what the images do not carry, tests other than E2E, and
#: ``docs/``, which the images carry only as the text of a knowledge base (``bundled_docs_dir`` in
#: ``app/agents/seed.py``): the boot reads it as UTF-8, which ``tests/test_repo_text_hygiene.py`` checks.
#: A Markdown file of ``backend/app`` is not decided here (``_e2e_inert``): the boot seeds personas and
#: skills from some.
E2E_INERT = re.compile(
    r"^(docs/|dev-docs/|k8s/|mcp-registry/|licensing/|scripts/|\.claude/|\.cursor/|\.agents/|\.xtm-one/"
    r"|\.betterleaks\.toml$|backend/tests/(?!selection\.py$)|frontend/tests_unit/|frontend/src/.+\.test\.tsx?$"
    r"|apps/(runner|relay|agent-core)/(?!dist/|release-signing\.json$)"
    r"|\.github/(?!workflows/e2e\.yml$|actions/ci-license/|scripts/ci-license-|ci-license-url$))"
    r"|^(?!backend/app/).*\.md$"
)

#: What every E2E project runs on: the images, the stack (the image's start script, the database's init
#: script that compose mounts), the suite's own harness (every file of ``tests_e2e/`` outside the areas'
#: directories but a core spec, which only core runs).
E2E_EVERY_PROJECT = re.compile(
    r"^(Dockerfile[^/]*|\.dockerignore|docker-compose[^/]*\.yml|docker/.*"
    r"|backend/(pyproject\.toml|uv\.lock|start-uvicorn\.sh|init-pgvector\.sql)"
    r"|frontend/(package\.json|yarn\.lock|\.yarnrc\.yml|playwright\.config\.ts)"
    r"|frontend/tests_e2e/(?!a2a/|evaluation/).+(?<!\.spec\.ts)"
    r"|\.github/workflows/e2e\.yml|\.github/actions/ci-license/.*|\.github/scripts/ci-license-.*|\.github/ci-license-url"
    r"|backend/tests/selection\.py)$"
)

#: The E2E projects that cover one area, by the ``app`` modules of that area and their spec directory.
E2E_AREAS = {
    "a2a": (re.compile(r"(^|[./_-])a2a([./_-]|$)"), "frontend/tests_e2e/a2a/"),
    "evaluation": (
        re.compile(r"(^|[./_-])evaluations?([./_-]|$)|evaluation_metrics"),
        "frontend/tests_e2e/evaluation/",
    ),
}

#: A test file that walks ``app/`` itself rather than importing what it needs: through ``tests/app_sources.py``,
#: a package walk, or a directory walk (``_WALKS``) of a path it names ``app`` (``_NAMES_APP``): a tripwire
#: that runs ``APP.rglob("*.py")`` reads every module (``tests/test_egress_funnel.py``).
_READS_THE_TREE = re.compile(r"\bapp_sources\b|pkgutil\.(walk_packages|iter_modules)|\bimport_module\(")
_WALKS = re.compile(r"\.r?glob\(|\.iterdir\(|os\.(walk|scandir|listdir)\(")
_NAMES_APP = re.compile(r"""["'](?:\.\./)*app/?["']|["']app/[^"'\s]*\*|\bapp\.__file__\b""")

#: A test file that walks the test files (``tests_dir.glob("test_*.py")``): a new or edited test can break it.
_READS_THE_TESTS = re.compile(r"""\.r?glob\(\s*["'](?:\*\*/)?test_""")

#: A test file that lists every tracked file of the repository (``git ls-files``): any change reaches it.
_READS_THE_REPO = re.compile(r"""["']ls-files["']""")

#: A dotted name written as a string: ``"app.services.x.y"``.
_DOTTED = re.compile(r"\b(?:app|scripts|tests)(?:\.[A-Za-z_]\w*)+")

#: The directories of ``backend/`` the graph reads: a test imports a script as ``scripts.x``.
GRAPH_ROOTS = ("app", "scripts", "tests")

#: A package whose ``__init__`` imports its own submodules by computed name.
_IMPORTS_ITS_SUBMODULES = re.compile(r"import_module\(\s*f[\"']\{__name__\}\.|pkgutil\.\w+\(\s*__path__")

#: Worker-seconds of unit tests one shard takes on, by ``shard_durations.json``: an eighth of the table
#: (about 1080 s) ran in 4.7 to 5.8 minutes, setup included, on 19 Nov 2026 (run 44534423144); the whole
#: suite keeps its eight shards.
UNIT_SECONDS_PER_SHARD = 1050.0
#: At most this many shards a suite takes, whole or selected. The Browser tool runs its tests one at a
#: time, about half an hour of them, and a shard's setup takes about a minute: five keep it under ten.
MAX_SHARDS = {"unit": 8, "integration": 4, "chromium": 5, "frontend-unit": 3}


def module_name(path: str) -> str | None:
    """``app/a/b.py`` -> ``app.a.b``, ``app/a/__init__.py`` -> ``app.a``; ``None`` outside ``GRAPH_ROOTS``."""
    if not path.endswith(".py") or not path.startswith(tuple(f"{root}/" for root in GRAPH_ROOTS)):
        return None
    parts = path[:-3].split("/")
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def is_test_file(path: str) -> bool:
    """Whether ``path`` (from the backend root) is a file pytest collects."""
    return path.startswith("tests/") and path.endswith(".py") and Path(path).name.startswith("test_")


#: The fields of a node that hold statements: an import is a statement, so no expression can hold one.
_STATEMENT_LISTS = ("body", "orelse", "finalbody", "handlers", "cases")


def _imports(tree: ast.AST) -> Iterable[tuple[ast.Import | ast.ImportFrom, bool]]:
    """Every import of ``tree`` but those under ``if TYPE_CHECKING:``, and whether it sits in a function.

    Only the statement lists are walked (an expression holds no statement): walking
    every node took two thirds of ``Graph.read``.
    """
    stack: list[tuple[ast.AST, bool]] = [(tree, False)]
    while stack:
        node, deferred = stack.pop()
        if isinstance(node, ast.Import | ast.ImportFrom):
            yield node, deferred
            continue
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            stack.extend((child, deferred) for child in node.orelse)
            continue
        inner = deferred or isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        for statements in _STATEMENT_LISTS:
            stack.extend((child, inner) for child in getattr(node, statements, ()))


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _reads_the_tree(source: str) -> bool:
    """Whether a file of ``tests/`` reads the whole of ``app/`` rather than the modules it imports."""
    return bool(_READS_THE_TREE.search(source) or (_WALKS.search(source) and _NAMES_APP.search(source)))


def _parents(name: str) -> list[str]:
    parts = name.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts))]


_RUN = re.compile(r"[\w.-]+")


def path_pieces(source: str) -> set[str]:
    """Every name ``source`` writes as a whole path piece.

    ``"frontend/src"`` writes ``frontend`` and ``src``; ``frontend_dist`` writes
    neither; ``en.json`` writes ``en.json`` and ``en``. Indexed once per file, so
    a change of hundreds of files looks each name up instead of scanning every
    source again.
    """
    pieces: set[str] = set()
    for run in _RUN.findall(source):
        pieces.add(run)
        dot = run.find(".")
        while dot > 0:
            pieces.add(run[:dot])
            dot = run.find(".", dot + 1)
    return pieces


def _index(sources: Mapping[str, str]) -> dict[str, set[str]]:
    """Path piece -> the files that write it."""
    return _reverse({path: path_pieces(source) for path, source in sources.items()})


def _reverse(edges: Mapping[str, set[str]]) -> dict[str, set[str]]:
    reverse: dict[str, set[str]] = defaultdict(set)
    for path, names in edges.items():
        for name in names:
            reverse[name].add(path)
    return reverse


#: What a file's source writes: the module names it imports as it loads, those it imports
#: once one of its functions runs, and whether it imports its own submodules by name.
WrittenImports = tuple[frozenset[str], frozenset[str], bool]

#: Fewer files than this are parsed in the calling process: a pool costs more than it saves.
_POOL_FROM_FILES = 200


def _written_imports(path: str, source: str) -> WrittenImports | None:
    """The module names ``path``'s source writes (``None``: ``ast`` cannot parse it).

    A test file, or a helper of ``tests/``, runs every import it writes, so
    all of them count as it loads. An ``app`` module's import inside a
    function runs only when the function does; a dotted string resolves at
    run time too.
    """
    own = module_name(path) or ""
    package = own if path.endswith("__init__.py") else own.rpartition(".")[0]
    in_tests = path.startswith("tests/")
    eager: set[str] = set()
    lazy: set[str] = set()
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return None
    for node, deferred in _imports(tree):
        names = eager if in_tests or not deferred else lazy
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
            continue
        base = node.module or ""
        if node.level:
            anchor = package.split(".")
            if node.level > 1:
                anchor = anchor[: len(anchor) - (node.level - 1)]
            base = ".".join([*anchor, base] if base else anchor)
        names.add(base)
        names.update(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    (eager if in_tests else lazy).update(_DOTTED.findall(source))
    submodules = path.endswith("__init__.py") and bool(_IMPORTS_ITS_SUBMODULES.search(source))
    return frozenset(eager), frozenset(lazy), submodules


def _read_written_imports(backend: str, path: str) -> WrittenImports | None:
    return _written_imports(path, (Path(backend) / path).read_text(encoding="utf-8", errors="replace"))


def _written_imports_of(backend: Path, paths: list[str]) -> list[WrittenImports | None]:
    """:func:`_written_imports` of every path, parsed on every CPU: ``ast`` takes most of the select job's time."""
    if len(paths) < _POOL_FROM_FILES:
        return [_read_written_imports(str(backend), path) for path in paths]
    from concurrent.futures import ProcessPoolExecutor
    from itertools import repeat

    with ProcessPoolExecutor() as pool:
        return list(pool.map(_read_written_imports, repeat(str(backend)), paths, chunksize=64))


@dataclass
class Graph:
    """Every ``.py`` file of ``GRAPH_ROOTS``: its source and the modules it reaches directly."""

    sources: dict[str, str]
    #: module name -> path from the backend root.
    modules: dict[str, str]
    #: path -> the module names it reaches directly as it loads.
    reaches: dict[str, set[str]] = field(default_factory=dict)
    #: path -> the module names one of its functions imports (``app`` modules only).
    reaches_later: dict[str, set[str]] = field(default_factory=dict)
    #: path -> the modules it names in an import as it loads, without the packages above them.
    names: dict[str, set[str]] = field(default_factory=dict)
    #: The files ``ast`` could not parse: what they reach is unknown.
    unparsed: list[str] = field(default_factory=list)

    @classmethod
    def read(cls, backend: Path = BACKEND_DIR) -> Graph:
        sources: dict[str, str] = {}
        for top in GRAPH_ROOTS:
            for path in sorted((backend / top).rglob("*.py")):
                if "__pycache__" not in path.parts:
                    sources[path.relative_to(backend).as_posix()] = path.read_text(encoding="utf-8", errors="replace")
        modules = {name: path for path in sources if (name := module_name(path)) is not None}
        graph = cls(sources=sources, modules=modules)
        for path, written in zip(sources, _written_imports_of(backend, list(sources)), strict=True):
            graph.reaches[path], graph.reaches_later[path], graph.names[path] = graph._direct(path, written)
        return graph

    def resolve(self, dotted: str) -> list[str]:
        """The modules importing ``dotted`` runs: its longest known prefix and every package above it."""
        parts = dotted.split(".")
        for end in range(len(parts), 0, -1):
            candidate = ".".join(parts[:end])
            if candidate in self.modules:
                return [candidate, *(p for p in _parents(candidate) if p in self.modules)]
        return []

    def _direct(self, path: str, written: WrittenImports | None) -> tuple[set[str], set[str], set[str]]:
        """What ``path`` reaches as it loads, what it reaches once one of its functions runs, and what it names as it loads.

        ``written``: the module names its source writes (:func:`_written_imports`),
        ``None`` when ``ast`` could not parse it.
        """
        if written is None:
            self.unparsed.append(path)
            return set(), set(), set()
        own = module_name(path) or ""
        eager, lazy, imports_its_submodules = written
        reached = {module for name in eager for module in self.resolve(name)}
        if imports_its_submodules:
            reached.update(name for name in self.modules if name.startswith(f"{own}."))
        reached.update(p for p in _parents(own) if p in self.modules)
        reached.discard(own)
        later = {module for name in lazy for module in self.resolve(name)} - reached - {own}
        named = {resolved[0] for name in eager if (resolved := self.resolve(name))} - {own}
        return reached, later, named

    @cached_property
    def reached_by(self) -> dict[str, set[str]]:
        """Module name -> the paths that reach it directly as they load."""
        return _reverse(self.reaches)

    @cached_property
    def reached_later_by(self) -> dict[str, set[str]]:
        """Module name -> the ``app`` paths one of whose functions imports it."""
        return _reverse(self.reaches_later)

    def dependents(self, seeds: Iterable[str]) -> set[str]:
        """Every path that reaches one of the ``seeds`` module names (module docstring: one step for a function's import)."""
        start = set(seeds)
        for name in list(start):
            start.update(own for path in self.reached_later_by.get(name, ()) if (own := module_name(path)) is not None)
        seen: set[str] = set()
        queue = deque(start)
        done: set[str] = set()
        while queue:
            name = queue.popleft()
            if name in done:
                continue
            done.add(name)
            for path in self.reached_by.get(name, set()) - seen:
                seen.add(path)
                if (own := module_name(path)) is not None:
                    queue.append(own)
        return seen

    def used_by(self, roots: Iterable[str]) -> set[str]:
        """The ``roots`` and the modules they import by name, as they load or in a function: one step, no deeper.

        Deeper, an area's code reaches the agentic loop, its tool registry and
        every integration behind it - most of ``app`` - and every change would
        run every area's E2E project.
        """
        used = set(roots)
        for name in roots:
            if (path := self.modules.get(name)) is not None:
                used |= self.names[path] | self.reaches_later[path]
        return used

    @cached_property
    def test_files(self) -> list[str]:
        return [path for path in self.sources if is_test_file(path)]

    def _readers(self, reads: Callable[[str], bool]) -> set[str]:
        """The test files whose source ``reads``, themselves or through a helper of ``tests/``."""
        helpers = {
            name
            for path, source in self.sources.items()
            if path.startswith("tests/") and not is_test_file(path) and reads(source)
            if (name := module_name(path)) is not None
        }
        return {path for path in self.test_files if reads(self.sources[path]) or self.reaches[path] & helpers}

    @cached_property
    def tree_readers(self) -> set[str]:
        """The test files that read the whole of ``app/``."""
        return self._readers(_reads_the_tree)

    @cached_property
    def test_readers(self) -> set[str]:
        """The test files that walk the test files (``tests/test_evaluation_guards.py``)."""
        return self._readers(lambda source: bool(_READS_THE_TESTS.search(source)))

    @cached_property
    def runs_under_every_test(self) -> set[str]:
        """Every ``conftest.py`` and the helpers of ``tests/`` it loads, at any depth (``tests/frozen_clock.py``).

        An autouse fixture applies a helper to every test, and a test that
        depends on it (the frozen instant) does not import it.
        """
        stack = [path for path in self.sources if path.startswith("tests/") and Path(path).name == "conftest.py"]
        seen: set[str] = set()
        while stack:
            path = stack.pop()
            if path not in seen:
                seen.add(path)
                stack.extend(self.modules[name] for name in self.reaches[path] if name.startswith("tests."))
        return seen

    @cached_property
    def repo_readers(self) -> set[str]:
        """The test files that read every tracked file (``tests/test_repo_text_hygiene.py``)."""
        return {path for path in self.test_files if _READS_THE_REPO.search(self.sources[path])}

    @cached_property
    def _tests_by_piece(self) -> dict[str, set[str]]:
        return _index({path: self.sources[path] for path in self.test_files})

    @cached_property
    def _modules_by_piece(self) -> dict[str, set[str]]:
        return _index({name: self.sources[path] for name, path in self.modules.items() if path.startswith("app/")})

    @cached_property
    def _files_by_piece(self) -> dict[str, set[str]]:
        return _index(self.sources)

    def naming_module(self, dotted: str) -> set[str]:
        """The files that name module ``dotted`` in an import: ``import a.b.c`` or ``from a.b import c``."""
        package, _, stem = dotted.rpartition(".")
        by_name = self._files_by_piece.get(dotted, set())
        by_stem = self._files_by_piece.get(stem, set()) & self._files_by_piece.get(package, set())
        return by_name | by_stem

    def tests_naming(self, token: str) -> set[str]:
        """The test files whose source writes ``token`` as a path piece: ``"frontend"``, ``/docs/``, ``x.json``."""
        return self._tests_by_piece.get(token, set())

    def modules_naming(self, token: str) -> set[str]:
        """The ``app`` modules whose source writes ``token`` as a path piece."""
        return self._modules_by_piece.get(token, set())


@dataclass
class Selection:
    """The test files to run, each with the first reason it was picked; or ``full``, why every one runs."""

    full: str | None = None
    #: The suites that run whole although the rest is selected: ``{"integration": why}``.
    full_suites: dict[str, str] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)

    def add(self, paths: Iterable[str], reason: str) -> None:
        for path in sorted(paths):
            self.reasons.setdefault(path, reason)

    @property
    def files(self) -> list[str]:
        return sorted(self.reasons)


def _area(path: str) -> str:
    """The directory a file outside ``backend/app`` is named by: ``frontend``, ``.github``, ``alembic``."""
    parts = path.split("/")
    if parts[0] == "backend" and len(parts) > 2:
        return parts[1]
    return parts[0]


def _data_readers(path: str, graph: Graph) -> set[str] | None:
    """The ``app`` modules that read the data file ``path`` (from the backend root): they name it or a directory above it.

    A Markdown file beside a package's modules (``AGENTS.md``, ``copyright.md``)
    documents the package: only a module of the package that writes its name
    in a string reads it (``Path(__file__).parent / "AGENTS.md"``) - a comment
    citing "AGENTS.md invariant 16", or a module reading another repository's
    ``README.md``, does not - and none may.
    ``None``: no module names the data file, so what reads it is unknown.
    """
    file = Path(path)
    readers = graph.modules_naming(file.name)
    if file.suffix == ".md" and module_name(f"{file.parent.as_posix()}/__init__.py") in graph.modules:
        quoted = re.compile(rf"""["'][^"'\n]*(?<![\w.-]){re.escape(file.name)}(?![\w.-])""")
        return {
            name
            for name in readers
            if Path(graph.modules[name]).parent == file.parent and quoted.search(graph.sources[graph.modules[name]])
        }
    if readers:
        return readers
    for directory in file.parents:
        if readers or directory.as_posix() in {"app", "."}:
            break
        readers = graph.modules_naming(directory.name)
    return readers or None


def _data_folders(path: str, graph: Graph) -> list[str]:
    """The directories of a data file under ``app/`` (from the backend root) that a test globbing it names.

    Its own directory and those above it, up to the package holding them:
    ``seed_data/agent_persona/x.md`` gives ``agent_persona`` and ``seed_data``,
    which ``tests/test_seeded_loop_caps.py`` globs. A file beside a package's
    modules gives none: a package's name (``services``) is too common a word,
    and a test reading that file names the file.
    """
    folders: list[str] = []
    for directory in Path(path).parents:
        if directory.as_posix() in {"app", "."} or module_name(f"{directory.as_posix()}/__init__.py") in graph.modules:
            break
        folders.append(directory.name)
    return folders


def select_backend(changed: Iterable[str], graph: Graph) -> Selection:
    """The backend test files ``changed`` (paths from the repository root) can break."""
    selection = Selection()
    if graph.unparsed:
        selection.full = f"{graph.unparsed[0]} does not parse, so what it imports is unknown"
        return selection
    seeds: set[str] = set()
    app_python_changed = False
    for repo_path in sorted(set(changed)):
        path = repo_path.removeprefix("backend/")
        if BACKEND_FULL_INPUTS.search(repo_path) or (
            repo_path.startswith("backend/") and path in graph.runs_under_every_test
        ):
            selection.full = f"{repo_path} runs under every test"
            return selection
        if DATABASE_INPUTS.search(repo_path):
            selection.full_suites.setdefault("integration", f"{repo_path} builds the database")
        name = Path(repo_path).name
        own = module_name(path) if repo_path.startswith("backend/") else None
        # A tripwire reads a module's source by its path, as a test reads a data file.
        selection.add(graph.tests_naming(name), f"names {name}")
        if own is not None:
            seeds.add(own)
            if own not in graph.modules:
                # A deleted module: nothing resolves to it any more, but what still imports it names it.
                importers = graph.naming_module(own)
                selection.add((p for p in importers if is_test_file(p)), f"imports {own}, which the change deletes")
                seeds.update(importer for p in importers if (importer := module_name(p)) is not None)
            if is_test_file(path):
                if path in graph.sources:
                    selection.add([path], "changed")
                selection.add(graph.test_readers, f"walks the test files ({repo_path})")
            app_python_changed |= path.startswith("app/")
            continue
        if repo_path.startswith("backend/app/"):
            readers = _data_readers(path, graph)
            if readers is None:
                selection.full = f"{repo_path} is read by no module that names it"
                return selection
            seeds |= readers
            for folder in _data_folders(path, graph):
                selection.add(graph.tests_naming(folder), f"reads {folder}/ ({repo_path})")
        elif repo_path.startswith("backend/tests/"):
            # A file of tests/ itself has only its name: every test writes "tests" (``from tests.x import``).
            folder = Path(path).parent.name
            if Path(path).parent != Path("tests"):
                selection.add(graph.tests_naming(folder), f"reads {folder}/ ({repo_path})")
        else:
            area = _area(repo_path)
            selection.add(graph.tests_naming(area), f"reads {area}/ ({repo_path})")
    selection.add((p for p in graph.dependents(seeds) if is_test_file(p)), "imports a changed module")
    if app_python_changed:
        selection.add(graph.tree_readers, "reads the whole app/ tree")
    if changed:
        selection.add(graph.repo_readers, "reads every tracked file")
    return selection


@dataclass
class FrontendUnit:
    """What the vitest jobs run: nothing, every file, or ``vitest related`` on ``related``."""

    mode: str
    reason: str
    #: Paths from ``frontend/``: changed sources and tests, and the tests that read a changed file from disk.
    related: list[str] = field(default_factory=list)


def _path_words(source: str) -> set[str]:
    """The path pieces a script writes in its string literals: a literal of one word, or a word holding a ``/``.

    A path the script reads is written in a literal; a comment or a test title
    (``"keeps the backend reason"``) names none, and matching them ran a few
    dozen vitest files on every backend change.
    """
    words: list[str] = []
    for groups in _LITERAL.findall(source):
        parts = "".join(groups).split()
        words += parts if len(parts) == 1 else [word for word in parts if "/" in word]
    return path_pieces(" ".join(words))


def _resolve_script(importer: Path, spec: str) -> Path | None:
    """The file a relative import of ``importer`` loads: ``./x`` -> ``x.ts``, ``x.tsx``, ``x/index.ts``..."""
    base = (importer.parent / spec).resolve()
    candidates = [base, *(base.with_name(base.name + suffix) for suffix in _SCRIPT_SUFFIXES)]
    candidates += [base / f"index{suffix}" for suffix in _SCRIPT_SUFFIXES]
    return next((path for path in candidates if path.is_file()), None)


def setup_closure(frontend: Path = REPO_ROOT / "frontend") -> set[str]:
    """The setup files and every file of the repository they import, at any depth: they run before every test.

    ``vitest related`` follows the spec files' imports, not the setup files':
    ``src/testing/design-system-warnings.ts`` installs hooks on every test.
    Paths from the repository root.
    """
    root = frontend.resolve().parent
    stack = [frontend.resolve() / name for name in FRONTEND_SETUP_FILES]
    seen: set[Path] = set()
    while stack:
        file = stack.pop()
        if file in seen or not file.is_file():
            continue
        seen.add(file)
        for spec in _RELATIVE_IMPORT.findall(file.read_text(encoding="utf-8", errors="replace")):
            if (target := _resolve_script(file, spec)) is not None:
                stack.append(target)
    return {path.relative_to(root).as_posix() for path in seen if path.is_relative_to(root)}


def select_frontend_unit(changed: Iterable[str], frontend: Path = REPO_ROOT / "frontend") -> FrontendUnit:
    """What the vitest jobs run for ``changed`` (paths from the repository root)."""
    changed = sorted(set(changed))
    under_every_test = setup_closure(frontend)
    for path in changed:
        if FRONTEND_UNIT_FULL_INPUTS.search(path) or path in under_every_test:
            return FrontendUnit("full", f"{path} runs under every test")
    related = {path.removeprefix("frontend/") for path in changed if FRONTEND_UNIT_SOURCES.search(path)}
    # A reader names a file by its name or its directory; one that lists a directory reads the whole area it
    # names (``src`` or ``tests_unit`` in frontend/, ``_area`` outside: ``docs``, ``app``), whatever the file.
    names: set[str] = set()
    areas: set[str] = set()
    for path in changed:
        parts = path.split("/")
        names.update(parts[-2:])
        areas.add(parts[1] if parts[0] == "frontend" and len(parts) > 2 else _area(path))
    # A test helper that reads the file (``src/testing/``) counts too: ``vitest related`` follows it to its importers.
    for root in ("src", "tests_unit"):
        for file in sorted((frontend / root).rglob("*")):
            if file.suffix not in _SCRIPT_SUFFIXES or "node_modules" in file.parts:
                continue
            source = file.read_text(encoding="utf-8", errors="replace")
            if not _READS_FILES.search(source):
                continue
            words = _path_words(source)
            if not names.isdisjoint(words) or (_LISTS_A_DIRECTORY.search(source) and not areas.isdisjoint(words)):
                related.add(file.relative_to(frontend).as_posix())
    # Only what exists: vitest refuses a path it cannot find, and a deleted file is reached through its importers.
    related = {path for path in related if (frontend / path).exists()}
    if not related:
        return FrontendUnit("skip", "the change reaches no vitest file")
    return FrontendUnit("related", f"{len(related)} changed or reading file(s)", sorted(related))


def _e2e_inert(path: str, graph: Graph) -> bool:
    """Whether the E2E stack never reads ``path``: ``E2E_INERT``, or a package's own Markdown under ``backend/app``.

    The image carries all of ``backend/app``, and the boot seeds personas,
    skills and templates from its Markdown: such a file is inert only when it
    documents its package, which no module reads (``_data_readers``).
    """
    if path.startswith("backend/app/") and path.endswith(".md"):
        return _data_readers(path.removeprefix("backend/"), graph) == set()
    return bool(E2E_INERT.search(path))


def e2e_projects(changed: Iterable[str], graph: Graph) -> dict[str, str | None]:
    """Each E2E project, and why it runs (``None``: it does not)."""
    changed = sorted(set(changed))
    live = [path for path in changed if not _e2e_inert(path, graph)]
    if not live:
        return {"core": None, **dict.fromkeys(E2E_AREAS)}
    projects: dict[str, str | None] = {"core": f"{live[0]} reaches the stack"}
    every = next((path for path in live if E2E_EVERY_PROJECT.search(path) or DATABASE_INPUTS.search(path)), None)
    changed_modules = {
        module
        for path in live
        if (module := module_name(path.removeprefix("backend/"))) is not None and path.startswith("backend/app/")
    }
    for project, (area, specs) in E2E_AREAS.items():
        reason = f"{every} runs every project" if every else None
        if reason is None:
            reason = next((f"{p} is one of its specs" for p in live if p.startswith(specs)), None)
        if reason is None:
            roots = [name for name in graph.modules if name.startswith("app.") and area.search(name)]
            reached = graph.used_by(roots) & changed_modules
            reason = f"it uses {min(reached)}" if reached else None
        if reason is None:
            reason = next((f"{p} is its UI" for p in live if p.startswith("frontend/src/") and area.search(p)), None)
        projects[project] = reason
    return projects


def _loaded(graph: Graph, stack: list[str]) -> set[str]:
    """``stack`` and what it loads at import, at any depth."""
    loaded: set[str] = set()
    while stack:
        name = stack.pop()
        if name not in loaded and name in graph.modules:
            loaded.add(name)
            stack.extend(graph.reaches[graph.modules[name]])
    return loaded


def migration_chain_modules(graph: Graph, alembic: Path = BACKEND_DIR / "alembic") -> set[str]:
    """The ``app`` modules the migration chain runs: what ``alembic/`` imports, anywhere in a file, and what those load.

    A migration body imports its helpers inside ``upgrade()`` (``app.core.migration_ddl``
    in most of them), so every import of the chain counts, as a test's does. The
    job also calls ``app.core.migrations.run_migrations()`` itself. A function of a
    loaded module may run during the import (``Settings()``'s validators import
    ``app.core.url_identity``): what each loaded module's functions import counts
    one step, then what that loads.
    """
    stack: list[str] = [MIGRATION_RUNNER]
    for path in sorted(alembic.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue  # a file of alembic/ is a MIGRATION_INPUT: its own change runs the job
        for node, _ in _imports(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                names = [node.module or "", *(f"{node.module}.{alias.name}" for alias in node.names)]
            stack += [module for name in names for module in graph.resolve(name)]
    loaded = _loaded(graph, stack)
    return loaded | _loaded(graph, [later for name in loaded for later in graph.reaches_later[graph.modules[name]]])


def migrations_reason(changed: Iterable[str], graph: Graph, alembic: Path = BACKEND_DIR / "alembic") -> str | None:
    """Why the Migrations job runs for ``changed``; ``None``: the chain is main's."""
    chain = migration_chain_modules(graph, alembic)
    for path in sorted(changed):
        if MIGRATION_INPUTS.search(path) or (
            path.startswith("backend/app/") and module_name(path.removeprefix("backend/")) in chain
        ):
            return f"{path} changes the migration chain"
    return None


def read_selected_files(value: str | None) -> frozenset[str] | None:
    """``PYTEST_SELECTED_FILES``: a list the select job wrote, one test file per line; unset or empty, every file."""
    if not value:
        return None
    path = Path(value)
    if not path.is_file():
        raise ValueError(f"PYTEST_SELECTED_FILES names {value!r}, which is not a file")
    return frozenset(line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def changed_files(base: str, head: str = "HEAD", repo: Path = REPO_ROOT) -> list[str]:
    """The paths ``base..head`` adds, edits or deletes (a rename counts both names)."""
    out = subprocess.run(  # noqa: S603 - a fixed git command on the workflow's own commits
        ["git", "diff", "--name-only", "--no-renames", base, head],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def shard_list(count: int) -> list[str]:
    """``3`` -> ``["1/3", "2/3", "3/3"]``: the matrix a sharded job takes."""
    return [f"{index}/{count}" for index in range(1, count + 1)]


def _shards(selected: float, total: float, suite: str, files: int | None = None) -> int:
    """The shards a ``selected`` share of ``total`` takes, in proportion to the whole suite's; no more than its ``files``."""
    if selected == 0 or files == 0:
        return 0
    cap = files if files is not None else int(selected)
    return max(1, min(cap, math.ceil(MAX_SHARDS[suite] * selected / max(total, 1))))


#: A marker the unit job leaves out: ``-m "not integration"``, and no Chromium installed.
_NOT_UNIT = re.compile(r"\bmark\.(?:integration|chromium)\b")


def _marks_whole_file(source: str) -> bool:
    """Whether a test file's module-level ``pytestmark`` keeps every one of its tests out of the unit job."""
    if "pytestmark" not in source or not _NOT_UNIT.search(source):
        return False
    return any(
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in node.targets)
        and _NOT_UNIT.search(ast.unparse(node.value)) is not None
        for node in ast.parse(source).body
    )


def plan_backend(selection: Selection, graph: Graph, weights: Path | None = None) -> dict[str, list[str]]:
    """The shards of the unit, integration and Chromium jobs.

    ``weights``: the directory of the tables this run deals by (``unit.json``,
    ``chromium.json``, ``scripts/shard_weights.py``); unset, the committed ones.
    """
    from tests.marked_files import spells_marker
    from tests.sharding import CHROMIUM_DURATIONS_PATH, DURATIONS_PATH, UNTIMED_SECONDS, load_durations

    durations = load_durations(weights / "unit.json" if weights else DURATIONS_PATH)
    chromium = load_durations(weights / "chromium.json" if weights else CHROMIUM_DURATIONS_PATH)
    files = graph.test_files if selection.full else selection.files
    # A file marked integration or chromium as a whole has no test the unit job runs.
    unit_files = [path for path in files if not _marks_whole_file(graph.sources[path])]
    seconds = sum(durations.get(path, UNTIMED_SECONDS) for path in unit_files)
    unit = min(MAX_SHARDS["unit"], len(unit_files), math.ceil(seconds / UNIT_SECONDS_PER_SHARD)) if unit_files else 0
    plan = {"unit": shard_list(unit)}
    for suite in ("integration", "chromium"):
        marked = [path for path in graph.test_files if spells_marker(graph.sources[path], suite)]
        whole = selection.full or suite in selection.full_suites
        picked = marked if whole else [path for path in files if path in set(marked)]
        if suite == "chromium":
            # Its files are dealt by their seconds: the shards follow the share of the seconds selected.
            seconds_of = [sum(chromium.get(path, UNTIMED_SECONDS) for path in paths) for paths in (picked, marked)]
            plan[suite] = shard_list(_shards(seconds_of[0], seconds_of[1], suite, files=len(picked)))
        else:
            plan[suite] = shard_list(_shards(len(picked), len(marked), suite))
    return plan


def _write_outputs(path: Path | None, outputs: Mapping[str, object]) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as out:
        for key, value in outputs.items():
            out.write(f"{key}={value if isinstance(value, str) else json.dumps(value)}\n")


def _summary(path: Path | None, title: str, rows: Sequence[tuple[str, str]]) -> None:
    if path is None:
        return
    lines = [f"### {title}", "", "| Suite | Runs |", "|---|---|", *(f"| {suite} | {why} |" for suite, why in rows), ""]
    with path.open("a", encoding="utf-8") as out:
        out.write("\n".join(lines) + "\n")


def frontend_build_reason(changed: Iterable[str]) -> str | None:
    """Why the frontend build runs for ``changed``; ``None``: it skips."""
    return next(
        (
            f"{p} changes the frontend"
            for p in changed
            if p.startswith("frontend/") or p in {".github/workflows/ci.yml", "backend/tests/selection.py"}
        ),
        None,
    )


def run_ci(
    changed: list[str] | None,
    full_because: str | None,
    outdir: Path,
    weights: Path | None = None,
    graph: Graph | None = None,
) -> dict[str, object]:
    """The answer of ``ci.yml``'s ``select`` job: ``changed`` is ``None`` when every suite runs."""
    if graph is None:
        graph = Graph.read()
    if changed is None:
        selection = Selection(full=full_because or "every suite runs")
        frontend = FrontendUnit("full", selection.full)
        migrations: str | None = selection.full
        build: str | None = selection.full
    else:
        selection = select_backend(changed, graph)
        frontend = select_frontend_unit(changed)
        migrations = migrations_reason(changed, graph)
        build = frontend_build_reason(changed)
    plan = plan_backend(selection, graph, weights)
    frontend_shards = {"skip": 0, "related": 1, "full": MAX_SHARDS["frontend-unit"]}[frontend.mode]
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "backend-tests.txt").write_text("".join(f"{p}\n" for p in selection.files), encoding="utf-8")
    (outdir / "frontend-related.txt").write_text("".join(f"{p}\n" for p in frontend.related), encoding="utf-8")
    details = {
        "backend": {"full": selection.full, "full_suites": selection.full_suites, "reasons": selection.reasons},
        "frontend_unit": {"mode": frontend.mode, "reason": frontend.reason, "related": frontend.related},
        "migrations": migrations,
        "frontend_build": build,
        "shards": plan,
    }
    (outdir / "selection.json").write_text(json.dumps(details, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "backend-full": "true" if selection.full else "false",
        "unit-shards": plan["unit"],
        "integration-shards": plan["integration"],
        "chromium-shards": plan["chromium"],
        "integration-full": "true" if selection.full or "integration" in selection.full_suites else "false",
        "frontend-unit-mode": frontend.mode,
        "frontend-unit-shards": shard_list(frontend_shards),
        "migrations": "true" if migrations else "false",
        "frontend-build": "true" if build else "false",
        "_rows": [
            ("Backend unit", _why(plan["unit"], selection.full, f"{len(selection.files)} selected file(s)")),
            (
                "Integration",
                _why(plan["integration"], selection.full or selection.full_suites.get("integration"), "selected"),
            ),
            ("Browser tool (Chromium)", _why(plan["chromium"], selection.full, "selected")),
            ("Migrations", migrations or "skipped: the migration chain is main's"),
            ("Frontend build", build or "skipped: no frontend change"),
            ("Frontend unit", f"{frontend.mode}: {frontend.reason}"),
        ],
    }


def _why(shards: list[str], full: str | None, selected: str) -> str:
    if not shards:
        return "skipped: the change reaches none of its tests"
    return f"{len(shards)} shard(s), all files: {full}" if full else f"{len(shards)} shard(s), {selected}"


def run_e2e(changed: list[str] | None, full_because: str | None, matrix: list[dict[str, str]]) -> dict[str, object]:
    """The answer of ``e2e.yml``'s ``select`` job: the matrix entries of the projects that run."""
    if changed is None:
        projects: dict[str, str | None] = {entry["project"]: full_because or "every project runs" for entry in matrix}
    else:
        projects = e2e_projects(changed, Graph.read())
    include = [entry for entry in matrix if projects.get(entry["project"])]
    return {
        "matrix": {"include": include},
        "run": "true" if include else "false",
        "_rows": [(project, why or "skipped: the change does not reach it") for project, why in projects.items()],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("workflow", choices=("ci", "e2e"))
    parser.add_argument("--base", help="the commit the change is measured from")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--full", metavar="WHY", help="run every suite, for this reason (no merge group to measure)")
    parser.add_argument("--outdir", type=Path, default=Path("selection"), help="where the ci file lists go")
    parser.add_argument("--matrix", help="e2e: the shard matrix entries, as a JSON list")
    parser.add_argument(
        "--weights", type=Path, help="ci: the shard weights this run deals by (scripts/shard_weights.py)"
    )
    parser.add_argument("--github-output", type=Path, help="append the job outputs here ($GITHUB_OUTPUT)")
    parser.add_argument("--summary", type=Path, help="append a table here ($GITHUB_STEP_SUMMARY)")
    args = parser.parse_args(argv)
    if (args.base is None) == (args.full is None):
        parser.error("give either --base or --full")
    changed = None if args.full else changed_files(args.base, args.head)
    if args.workflow == "ci":
        outputs = run_ci(changed, args.full, args.outdir, args.weights)
    else:
        outputs = run_e2e(changed, args.full, json.loads(args.matrix or "[]"))
    rows = outputs.pop("_rows")
    _write_outputs(args.github_output, outputs)
    title = f"Test selection: {args.full}" if args.full else f"Test selection: {len(changed or [])} changed file(s)"
    _summary(args.summary, title, rows)  # type: ignore[arg-type]
    sys.stdout.write(json.dumps(outputs, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
