"""The backend's own source, read and parsed once per test process.

A tripwire - a test asserting a property of every module under ``app/`` (no
module reads X but through Y, every writer of Z is known) - walks the whole
tree: some 850 files, about 4 seconds to parse. Dozens of suites do, so they
read it here rather than each parsing its own copy.

The trees are shared between tests: a scan reads them, and never mutates a
node or keeps state on one.

The cache costs about 1.5 GB of resident memory per process (some 1,800
files). Under xdist every worker that runs one tripwire builds its own copy:
on the 8 GB runner of a private repository two copies left a few hundred MB
free and got the runner killed (#10437), so every reader ran on one worker,
which ended each job alone for a minute while the others idled. The public
repository's runners have 16 GB: the suite's conftest deals the files that
read this module into :data:`XDIST_GROUPS` xdist groups (``--dist
loadgroup``, :func:`xdist_groups`), longest first by the shard weights, so
that many workers per job hold a copy and share the tripwires.
"""

from __future__ import annotations

import ast
import functools
import re
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BACKEND_DIR / "app"
REPO_ROOT = BACKEND_DIR.parent

#: The prefix of the xdist groups of the tests whose file reads the parsed source.
XDIST_GROUP = "app_sources"
#: How many groups the readers are dealt into: one copy of the cache per group, on its own worker.
XDIST_GROUPS = 2

_READS_SOURCES = re.compile(r"\bapp_sources\b")


@functools.cache
def _helpers_reading_sources(tests_dir: Path) -> frozenset[str]:
    """Module names of the non-test helpers under ``tests_dir`` that read this module."""
    names = set()
    for path in tests_dir.rglob("*.py"):
        if path.name.startswith("test_") or path.name in {"conftest.py", "app_sources.py"}:
            continue
        if _READS_SOURCES.search(path.read_text(encoding="utf-8")):
            names.add(path.stem)
    return frozenset(names)


@functools.cache
def reads_sources(test_file: Path) -> bool:
    """Whether ``test_file`` reads this module, itself or through a helper of its directory."""
    try:
        text = test_file.read_text(encoding="utf-8")
    except OSError:
        return False
    if _READS_SOURCES.search(text):
        return True
    return any(re.search(rf"\b{name}\b", text) for name in _helpers_reading_sources(test_file.parent))


def xdist_groups(files: Iterable[str], durations: Mapping[str, float]) -> dict[str, str]:
    """The xdist group of each reader file (paths from the backend root), dealt longest first by ``durations``.

    Every worker collects the same files and reads the same table, so each
    names the same group for a file, as ``--dist loadgroup`` needs.
    """
    from tests.sharding import shard_files

    files = sorted(set(files))
    groups: dict[str, str] = {}
    for index in range(1, XDIST_GROUPS + 1):
        for name in sorted(shard_files(files, durations, index, XDIST_GROUPS)):
            groups.setdefault(name, f"{XDIST_GROUP}-{index}")
    return groups


@functools.cache
def python_files(root: Path = APP_DIR) -> tuple[Path, ...]:
    """Every ``*.py`` file under ``root``, sorted."""
    return tuple(sorted(root.rglob("*.py")))


@functools.cache
def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@functools.cache
def tree(path: Path) -> ast.Module:
    return ast.parse(source(path), filename=str(path))


@functools.cache
def walk(module: ast.Module) -> tuple[ast.AST, ...]:
    """``tuple(ast.walk(module))`` for a tree from :func:`tree`, kept.

    Iterating the tuple is several times faster than walking again. Meant for
    whole modules: every tree passed here stays in memory for the session.
    """
    return tuple(ast.walk(module))


@functools.cache
def parents(module: ast.Module) -> Mapping[ast.AST, ast.AST]:
    """Each node of a tree from :func:`tree` -> the node holding it."""
    return {child: node for node in walk(module) for child in ast.iter_child_nodes(node)}


def relative(path: Path, root: Path = APP_DIR) -> str:
    """``path`` relative to ``root``, with forward slashes."""
    return path.relative_to(root).as_posix()


def modules(root: Path = APP_DIR) -> Iterator[tuple[str, ast.Module]]:
    """``(path relative to root, tree)`` for every Python file under ``root``."""
    for path in python_files(root):
        yield relative(path, root), tree(path)
