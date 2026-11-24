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

"""The tests a checkout's changes affect, for ``sandbox_run_command(affected=true)``.

A targeted test run chosen by hand misses the test that reads a changed
symbol or schema key (a 13-file run missed the one test asserting on a tool
schema: one red CI round). :func:`changed_identifiers` reads what a change
touches in one file: the definitions whose lines it adds, edits or removes
(:func:`reading.symbols`: Python by syntax tree, the outline languages by
declaration scan), the identifier-like string literals on the changed lines
(a schema key), and, for a Python module, its import path.
:func:`select_tests` keeps the test files that name one of them, word for
word, plus the changed test files themselves, ranked so the argv bound keeps
the best. :func:`affected_tests` runs both over a checkout and its record.

A name a changed Python module defines selects a test file only when that
file references the module (:func:`references_module`: its import path, or
``from <package> import <module>``): a definition or a constant named in a
test that never imports the module is another module's (a change to a
``reading`` module selected 62 files on its constants' names, 59 of them
failing on files outside a narrow checkout). A test file that imports the
module is selected by it, even when it reaches the change as
``module.name`` only.

Names too generic to select by are set aside: a definition nested in a
function (its enclosing definition already covers the change) and a plain
lowercase word shorter than :data:`MIN_PLAIN_WORD_CHARS` (``done``, ``step``),
which selected hundreds of unrelated files. A string literal counts only when
the change adds or removes it, and only when distinctive
(:func:`distinctive_literal`): a JSON-schema keyword (:data:`SCHEMA_KEYWORDS`:
``description`` selected 134 test files for one tool-schema edit) or a plain
key under :data:`MIN_LITERAL_CHARS` characters without an underscore
(``sources``) selects nothing.

A changed test file runs because it changed, and its own names (its test
functions, the literals it asserts on: ``'Authorization'``, ``'default'``)
select no other test file: one new test file selected 123 others through
the header names and words it quoted.

Two more filters keep the run close to the change. A name found only in a
string literal on a changed line (a schema key) selects a test file only
where the test quotes it (:func:`quotes`, a template literal of an e2e
assertion included): a parameter or a variable of the same name is another
thing. And a free name found in more than
:data:`COMMON_SHARE` of the test files read (above :data:`COMMON_FLOOR`
files) is set aside as too common and named in the answer: a fixture or a
helper every file calls selected most of the suite, and the run timed out.
The stop list is the repository's own counts, never a list kept here.

The selection runs inside one tool call, before the command it selects for:
:func:`affected_tests` takes a ``deadline`` (a ``time.monotonic()``
instant) and stops reading test files there, naming how many it did not
search, so a large check directory never spends the call's whole budget
before the command starts (a foreground call on a 2,600-file test tree
reached the agent loop's tool timeout in the read alone).

A source-scan gate (:func:`is_source_scan`: a JavaScript or TypeScript test
that walks a directory, ``readdirSync``, ``readdir``, ``import.meta.glob``,
``globSync``, or any test carrying :data:`SOURCE_SCAN_MARKER`; a Python
``rglob`` is too common in large modules to count alone) names no definition: it reads
every source file for a forbidden pattern (a focus ring class, a raw
control). It is selected whenever a non-test, non-prose file under the
check's directory changes, ranked after the files a name selected, and
named ``source-scan gate`` in the answer: a class added to one ``.tsx`` was
first caught by CI one cycle later because no name selected the gate.
A check may add its own gate patterns in ``affected_scan_patterns`` (regular
expressions on the test file's text, ``.xtm-one/checks.toml``): a Python
tripwire reading every module through a shared helper (``app_sources``)
names no changed identifier, and a guard on a new company-managed read went
red only in the merge queue.

A check may name its ratchet gates in ``affected_always`` (globs on the test
path under its directory, ``.xtm-one/checks.toml``): on a source change they
are selected with the hit :data:`ALWAYS_HIT` and ranked right after the
changed tests, so the argv bound never cuts them. A frontend holding 65
directory-walking tests passed the bound with a few named hits, and the
form-controls gate, ranked last, was cut before it ran.

A changed prose file (:data:`PROSE_EXTENSIONS`: markdown, reStructuredText,
plain text) is no identifier source: its outline is its headings, and a
heading (``API``, ``Decisions``) selected 25 unrelated test files beside the
one change of code. It is named in the selection's notes.

A check that declares ``affected = "files"`` or ``"lines"`` (a formatter, a
linter) takes the changed files themselves instead (:func:`changed_files`):
the files added or modified under its directory that match its
``affected_globs``, and with ``lines`` a modified file as ``PATH:A-B,C-D``,
the line ranges the change wrote. A formatter on a repository that is not
format-clean then answers on the change's own lines, never on the 3,000
files of drift around them.

Three more rules keep the selection precise (gap family
``affected-selection-precision``):

- A changed prose file names no test by its headings, but a test may read
  it: :func:`reads_prose` selects the test files naming the file
  (``presentations.md``) or walking its top directory for its extension
  (``"docs"`` beside ``rglob("*.md")``), with the hit ``reads <path>``. A
  docs tripwire banning a word went red in CI while the affected run was
  green.
- Test files are read most related first (:func:`affinity`: the singular
  name tokens a test file's name shares with the changed files' names,
  ``flows.md`` relating to ``test_flow_builder_feature_flag.py``), after the
  ratchet gates, so a time or byte budget ending leaves the least related
  unread; one note states the budget in seconds and names the first files it
  did not search. A path-ordered read stopped 1,700 files short of the audit
  test quoting a removed literal, and a plural page name left its tripwire
  among the 1,600 files unread. The selection is ranked the same way within
  each tier, never alphabetically, and the note on a changed prose file names
  the tests that read it.
- A top-level directory name of the repository (``frontend``) is no
  selector: a checks list quoting it selected 53 test files. A selected test
  file reading a top-level directory the checkout does not hold
  (:func:`missing_directories`) is ranked last, marked ``[needs DIR/] (not
  run)`` in the selection and named with the exact ``materialize_repository
  add_paths`` call: its failures there are a missing file, not the change, so
  ``affected=true`` runs only :meth:`Selection.runnable` (32 of 46 selected
  files of a backend checkout failed 21 times on FileNotFoundError in 590 s).

A wording-only change of a Python file (:func:`wording_words`: its code
tokens equal on both sides, comments and docstrings aside; any other string
is code) names neither its definitions nor its module path: the distinctive
words it replaced select the tests pinning the former or new text, and a
note names the files. A product rename in docstrings and comments of 17
modules selected 55 test files through the edited functions and module
paths, about 60 minutes of run for no code change.

Generic on purpose: no tool, repository or framework name is known here
(invariant 18); a test file is recognised by its name only.
"""

from __future__ import annotations

import asyncio
import difflib
import fnmatch
import io
import posixpath
import re
import time
import tokenize
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field

from app.services.sandbox_workspace import reading
from app.services.sandbox_workspace.checkout import CheckoutChanges, CheckoutRecord
from app.services.sandbox_workspace.manifest import format_size
from app.services.sandbox_workspace.paths import is_under
from app.services.sandbox_workspace.session import WorkspaceFileUnreadableError, WorkspaceSession

#: Changed source files read for their identifiers; the rest are named in the answer.
MAX_SOURCES = 60
#: Identifiers kept for the search (the longest first: the most specific).
MAX_IDENTIFIERS = 300
#: Bytes of test files read for one selection (the search budget of sandbox_grep).
MAX_SCAN_BYTES = reading.MAX_GREP_BYTES
#: Files read at once: test files are small, and each read is one store round trip (8 at once read
#: about 1,000 of 3,084 backend test files in the 90 s budget).
READ_AT_ONCE = 32
#: Shortest identifier searched: shorter names match everywhere.
MIN_IDENTIFIER_CHARS = 4
#: Shortest plain lowercase word (no ``_``, digit or capital) searched: ``done``, ``finish`` match everywhere.
MIN_PLAIN_WORD_CHARS = 7
#: Names too common to select a test by.
COMMON_NAMES = frozenset(
    {"self", "None", "True", "False", "main", "test", "tests", "setup", "teardown", "name", "value", "data", "type", "path"}
)
#: Shortest string literal without an underscore searched: ``sources`` or ``currency``-like dict keys are in every test.
MIN_LITERAL_CHARS = 8
#: JSON-schema keywords: as string literals they are in every tool schema and its tests, never a distinctive change.
SCHEMA_KEYWORDS = frozenset(
    {
        "description",
        "type",
        "properties",
        "default",
        "required",
        "items",
        "enum",
        "title",
        "format",
        "pattern",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "additionalProperties",
        "anyOf",
        "oneOf",
        "allOf",
        "examples",
        "const",
        "nullable",
        "parameters",
    }
)
#: Share of the test files read past which a free name is too common to select by.
COMMON_SHARE = 0.25
#: Files a free name may select before :data:`COMMON_SHARE` applies: a small suite keeps every hit.
COMMON_FLOOR = 20
_LITERAL = re.compile(r"""["']([A-Za-z_][A-Za-z0-9_]*)["']""")
#: A file name or path in a string literal (``".xtm-checkout.json"``, ``"dev-docs/x.md"``): tests assert it inside
#: longer text (a message), so it is searched as a free name, never only where quoted whole.
_PATH_LITERAL = re.compile(r"""["']([A-Za-z0-9_.\-/]*[A-Za-z][A-Za-z0-9_.\-/]*)["']""")
_QUOTED = re.compile(r"""(["'`])([A-Za-z_][A-Za-z0-9_]*)\1""")
_PLAIN_WORD = re.compile(r"[a-z]+")
_TEST_NAME = re.compile(r"(^test_.+\.py$)|(.+_test\.py$)|(.+\.(test|spec)\.(t|j)sx?$)|(.+\.(test|spec)\.m?js$)")
#: The comment a test file of any language carries to declare itself a source-scan gate.
SOURCE_SCAN_MARKER = "@source-scan-gate"
#: A JavaScript or TypeScript test walking a directory, or the marker. A Python tree walk (``rglob``) is
#: too common in large test modules to select them on every change: such a gate declares the marker.
_SOURCE_SCAN = re.compile(
    r"\breaddirSync\s*\(|\breaddir\s*\(|\bimport\.meta\.glob\s*\(|\bglobSync\s*\(|" + re.escape(SOURCE_SCAN_MARKER)
)
#: The hit a source-scan gate is selected by, in place of the names it carries.
SOURCE_SCAN_HIT = "source-scan gate"
#: The hit a test file an ``affected_always`` glob names is selected by.
ALWAYS_HIT = "ratchet gate (always)"
#: The hit a test file naming a changed source file by its file name is selected by (followed by the name).
NAMES_HIT = "names "
#: Extensions of prose: their outline is headings, never a name a test reads.
PROSE_EXTENSIONS = frozenset({"md", "mdx", "markdown", "rst", "txt", "adoc"})
#: The hit a test file reading a changed prose file is selected by (followed by its path).
PROSE_HIT = "reads "
#: Name tokens that say nothing about what a file covers: never an affinity.
AFFINITY_STOP = frozenset({"test", "tests", "spec", "index", "init", "main", "utils", "helpers", "conftest"})
#: Files named in a note about what was not searched or not held.
NAMED_IN_NOTE = 5
_TOKEN = re.compile(r"[A-Za-z0-9]+")
_MARKER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Reads the file as checked out (``bytes``) or says why it cannot (``str``).
BaseReader = Callable[[str], Awaitable[bytes | str]]


def is_test_file(path: str) -> bool:
    """Whether *path* is a test file by its name (``test_x.py``, ``x_test.py``, ``x.test.ts``, ``x.spec.tsx``...)."""
    return bool(_TEST_NAME.match(path.rsplit("/", 1)[-1]))


def is_prose_file(path: str) -> bool:
    """Whether *path* is prose by its extension (``.md``, ``.rst``, ``.txt``...): no identifier source."""
    name = path.rsplit("/", 1)[-1]
    return "." in name and name.rsplit(".", 1)[-1].casefold() in PROSE_EXTENSIONS


def excluded_markers(argv: Iterable[str]) -> tuple[str, ...]:
    """The pytest markers a check's *argv* deselects with ``-m "not X"`` (``-m "not integration"``).

    Only a plain ``not X`` (or ``not X and not Y``) expression is read; any other expression answers none.
    """
    args = list(argv)
    found: list[str] = []
    for i, arg in enumerate(args):
        if arg == "-m" and i + 1 < len(args):
            expression = args[i + 1]
        elif arg.startswith("-m") and len(arg) > 2:
            expression = arg[2:].lstrip("=")
        else:
            continue
        parts = [p.strip() for p in expression.split(" and ")]
        names = [p[4:].strip() for p in parts if p.startswith("not ")]
        if len(names) == len(parts) and all(_MARKER_NAME.fullmatch(n) for n in names):
            found.extend(names)
    return tuple(dict.fromkeys(found))


def marked_with(text: str, markers: Iterable[str]) -> list[str]:
    """The *markers* a test file's *text* applies (``pytest.mark.X``, ``pytestmark = pytest.mark.X``)."""
    return [m for m in markers if re.search(r"\bpytest\.mark\." + re.escape(m) + r"\b", text)]


def selected_markers(argv: Iterable[str]) -> tuple[str, ...]:
    """The pytest markers a check's *argv* selects with a plain ``-m X`` (or ``-m "X or Y"``).

    A ``not``, an ``and`` or any other expression answers none: only a check that runs every file marked X
    is named to run them.
    """
    args = list(argv)
    found: list[str] = []
    for i, arg in enumerate(args):
        if i and posixpath.basename(args[i - 1]).startswith("python"):
            continue  # `python -m pytest`: a module, not a marker
        if arg == "-m" and i + 1 < len(args):
            expression = args[i + 1]
        elif arg.startswith("-m") and len(arg) > 2:
            expression = arg[2:].lstrip("=")
        else:
            continue
        names = [p.strip() for p in expression.split(" or ")]
        if all(_MARKER_NAME.fullmatch(n) and n not in ("not", "and", "or") for n in names):
            found.extend(names)
    return tuple(dict.fromkeys(found))


def marker_runners(
    checks: Iterable[tuple[str, Iterable[str], str]], cwd: str, markers: Iterable[str]
) -> dict[str, list[str]]:
    """Marker -> the checks (*checks*: name, argv, directory) of the directory *cwd* that select it with ``-m``.

    What the answer names for a file a check's ``-m "not X"`` deselects (``integration`` -> ``backend-integration``).
    """
    wanted = list(markers)
    here = cwd.strip("/")
    runners: dict[str, list[str]] = {}
    for name, argv, directory in checks:
        if directory.strip("/") != here:
            continue
        for marker in selected_markers(argv):
            if marker in wanted:
                runners.setdefault(marker, []).append(name)
    return runners


def is_source_scan(text: str, scans: Iterable[re.Pattern[str]] = ()) -> bool:
    """Whether a test file's *text* walks a directory (``readdirSync``, ``import.meta.glob``...), carries
    :data:`SOURCE_SCAN_MARKER`, or matches one of the check's own *scans* (``affected_scan_patterns``)."""
    return bool(_SOURCE_SCAN.search(text)) or any(pattern.search(text) for pattern in scans)


def _singular(token: str) -> str:
    """*token* without a plural ``s`` (``flows`` -> ``flow``), so ``flows.md`` relates to ``test_flow_builder.py``."""
    if len(token) > 4 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def name_tokens(path: str) -> set[str]:
    """The lower-cased singular tokens of *path*'s file name (``snake``, ``kebab`` and ``camelCase`` split), without :data:`AFFINITY_STOP`."""
    stem = path.rsplit("/", 1)[-1].split(".", 1)[0]
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", stem)
    return {
        _singular(t.casefold()) for t in _TOKEN.findall(spaced) if len(t) >= 3 and t.casefold() not in AFFINITY_STOP
    }


def change_tokens(paths: Iterable[str]) -> set[str]:
    """The name tokens of the changed *paths* and of their directories: what a related test file's name shares."""
    out: set[str] = set()
    for path in paths:
        out |= name_tokens(path)
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if parent:
            out |= name_tokens(parent)
    return out


def affinity(path: str, tokens: set[str]) -> int:
    """How many name tokens the test file *path* shares with the changed files (*tokens*): read first when higher."""
    return len(name_tokens(path) & tokens)


def _quoted_directory(directory: str) -> re.Pattern[str]:
    return re.compile(r"([\"'`])" + re.escape(directory) + r"(?:\1|/)")


def reads_prose(text: str, repo_path: str) -> bool:
    """Whether a test file's *text* reads the prose file *repo_path*: it names the file, or walks its top directory for its extension.

    ``presentations.md`` named in a path, or ``"docs"`` quoted beside a
    ``*.md`` glob (``rglob("*.md")``): a docs tripwire reads every page.
    """
    name = repo_path.rsplit("/", 1)[-1]
    if re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(name) + r"(?![A-Za-z0-9_])", text):
        return True
    if "/" not in repo_path or "." not in name:
        return False
    top = repo_path.split("/", 1)[0]
    extension = name.rsplit(".", 1)[-1]
    glob = re.compile(r"\*\." + re.escape(extension) + r"(?![A-Za-z0-9])")
    return bool(glob.search(text) and _quoted_directory(top).search(text))


def top_directories(record: CheckoutRecord) -> set[str]:
    """The top-level directory names of the repository the *record* knows (its files and sub_paths)."""
    paths = [*record.files, *record.sub_paths]
    return {p.split("/", 1)[0] for p in paths if "/" in p}


def missing_directories(record: CheckoutRecord) -> set[str]:
    """The top-level directories a narrow checkout does not hold: no sub_path is in them (empty for a whole tree)."""
    if not record.sub_paths:
        return set()
    held = {s.split("/", 1)[0] for s in record.sub_paths}
    return top_directories(record) - held


def reads_directories(text: str, directories: Iterable[str]) -> list[str]:
    """Every one of *directories* a test file's *text* reads (``"frontend"`` or ``"frontend/src"`` quoted), sorted.

    All of them, not the first: a test reading ``.xtm-one`` and ``frontend`` needs both to run, and
    the ``add_paths`` hint naming ``.xtm-one`` alone left it failing on the frontend file.
    """
    return [d for d in sorted(directories) if _quoted_directory(d).search(text)]


def names_file(text: str, repo_path: str) -> bool:
    """Whether a test file's *text* names the changed file *repo_path* by its file name (``"Flows.tsx"``).

    A source pin reads the file it guards by name (``_read("src", "pages", "Flows.tsx")``) and asserts
    on its text, not on an identifier the change touches. A stem that says nothing (``index``,
    ``utils``, ``__init__``) never selects.
    """
    name = repo_path.rsplit("/", 1)[-1]
    stem = name.split(".", 1)[0]
    if "." not in name or not stem or stem.casefold() in AFFINITY_STOP or stem.startswith("__"):
        return False
    return bool(re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(name) + r"(?![A-Za-z0-9_]|\.[A-Za-z0-9])", text))


def _kept(name: str) -> bool:
    if _PLAIN_WORD.fullmatch(name) and len(name) < MIN_PLAIN_WORD_CHARS:
        return False
    return len(name) >= MIN_IDENTIFIER_CHARS and name not in COMMON_NAMES and not (name.startswith("__") and name.endswith("__"))


def _searchable(found: list[reading.Symbol]) -> list[reading.Symbol]:
    """*found* without the definitions nested in a function: their enclosing definition covers the same lines."""
    kinds = {s.name: s.kind for s in found}
    return [s for s in found if "." not in s.name or not kinds.get(s.name.rsplit(".", 1)[0], "").endswith("def")]


def _changed_lines(before: list[str], after: list[str]) -> tuple[set[int], set[int]]:
    """1-based line numbers changed in *before* (removed or replaced) and in *after* (added or replaced)."""
    old: set[int] = set()
    new: set[int] = set()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        old.update(range(i1 + 1, i2 + 1))
        new.update(range(j1 + 1, j2 + 1))
    return old, new


def distinctive_literal(name: str) -> bool:
    """Whether a string literal is specific enough to select tests by: not a JSON-schema keyword, and an underscore or :data:`MIN_LITERAL_CHARS` characters."""
    if name in SCHEMA_KEYWORDS or not _kept(name):
        return False
    return "_" in name.strip("_") or len(name) >= MIN_LITERAL_CHARS


def _touched(path: str, text: str, lines: set[int], literals: set[str], defined: set[str]) -> set[str]:
    """The definitions of *text* whose span holds one of *lines* (also added to *defined*); the literals on those lines go to *literals*."""
    out: set[str] = set()
    found = _searchable(reading.symbols(path, text) or [])
    for symbol in found:
        if any(symbol.first <= n <= symbol.last for n in lines):
            out.add(symbol.name.rsplit(".", 1)[-1])
    defined |= out
    rows = text.splitlines()
    for n in lines:
        if 0 < n <= len(rows):
            literals.update(_LITERAL.findall(rows[n - 1]))
            literals.update(p for p in _PATH_LITERAL.findall(rows[n - 1]) if path_literal(p))
    return out


def _dotted_module(name: str) -> bool:
    """Whether *name* is a module import path (``app.tool``): never too common to select by."""
    return "." in name and all(part.isidentifier() for part in name.split("."))


def path_literal(text: str) -> bool:
    """Whether a string literal is a file name or path worth selecting tests by.

    It holds a ``.``, ``-`` or ``/`` (an identifier-like literal is :data:`_LITERAL`'s),
    :data:`MIN_LITERAL_CHARS` characters and four letters or digits. #1812 renamed
    ``".xtm-checkout.json"``: the five tests asserting the old name in an error message
    were not selected and failed in the merge queue.
    """
    if text.isidentifier() or len(text) < MIN_LITERAL_CHARS or not any(c in text for c in ".-/"):
        return False
    return sum(c.isalnum() for c in text) >= 4


_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
#: Tokens that never carry behaviour between two code tokens: comments and blank lines.
_TRIVIA = frozenset({tokenize.COMMENT, tokenize.NL})
#: Tokens a statement starts after: a string run between one of them and the end of the statement is a docstring.
_STATEMENT_START = frozenset({tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING})


def _docstrings(tokens: list[tokenize.TokenInfo]) -> set[int]:
    """Indexes of the STRING tokens of *tokens* that form a statement alone (a docstring or a bare string).

    Any other string (an argument, a constant, a dict key, a format string, an
    f-string part) is a value the code acts on: its change is a code change.
    """
    out: set[int] = set()
    i = 0
    while i < len(tokens):
        if tokens[i].type != tokenize.STRING:
            i += 1
            continue
        run = [i]
        j = i + 1
        while j < len(tokens) and (tokens[j].type in _TRIVIA or tokens[j].type == tokenize.STRING):
            if tokens[j].type == tokenize.STRING:
                run.append(j)
            j += 1
        k = i - 1
        while k >= 0 and tokens[k].type in _TRIVIA:
            k -= 1
        starts = k < 0 or tokens[k].type in _STATEMENT_START
        ends = j >= len(tokens) or tokens[j].type in (tokenize.NEWLINE, tokenize.ENDMARKER)
        if starts and ends:
            out.update(run)
        i = j
    return out


def _python_tokens(text: str) -> tuple[list[tuple[int, str]], list[str]] | None:
    """The code tokens of *text* (type, text) and the text of its comments and docstrings; ``None`` when it does not tokenize."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return None
    docs = _docstrings(tokens)
    code: list[tuple[int, str]] = []
    wording: list[str] = []
    for n, token in enumerate(tokens):
        if token.type == tokenize.NL:
            continue
        if token.type == tokenize.COMMENT or n in docs:
            wording.append(token.string)
        else:
            code.append((token.type, token.string))
    return code, wording


def wording_words(path: str, old_text: str, new_text: str) -> set[str] | None:
    """The words a wording-only change of a Python file replaces, or ``None`` when the change touches code.

    Wording-only: the code tokens of both sides are equal (comments and
    docstrings aside) and their text differs. Every other string literal (a
    status value, a key, a regex, a message, an f-string) is code: a test
    asserting the new value imports the module, so its change keeps the
    definition and module selection. The answer is the words removed or added
    in the comments and docstrings (a word diff), kept when distinctive
    (:func:`_kept`); no distinctive word left means ``None``, the code path,
    never an empty selection. A rename of a product in docstrings and comments
    across 17 modules selected 55 test files through the edited definitions and
    module paths, about 60 minutes of run for no code change.
    """
    if not path.endswith(".py") or old_text == new_text:
        return None
    old = _python_tokens(old_text)
    new = _python_tokens(new_text)
    if old is None or new is None or old[0] != new[0]:
        return None
    before = _WORD.findall(" ".join(old[1]))
    after = _WORD.findall(" ".join(new[1]))
    words: set[str] = set()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if tag != "equal":
            words.update(before[i1:i2])
            words.update(after[j1:j2])
    kept = {w for w in words if _kept(w)}
    return kept or None


def python_module(path: str, base: str) -> str:
    """The import path of the Python module *path* seen from *base* (the check's directory), ``""`` otherwise."""
    if not path.endswith(".py") or (base and not is_under(path, base)):
        return ""
    rel = path[len(base) + 1 :] if base else path
    dotted = rel[:-3].replace("/", ".")
    return dotted.removesuffix(".__init__") if "." in dotted else ""


def changed_identifiers(
    path: str,
    before: bytes | None,
    after: bytes | None,
    *,
    base: str = "",
    generic: set[str] | None = None,
    literals: set[str] | None = None,
    defined: set[str] | None = None,
    wording: set[str] | None = None,
) -> set[str]:
    """What a change of *path* touches: definitions, literals on changed lines, and its module path.

    *before* is the file as checked out (``None``: added, or unreadable, so
    every definition of *after* counts), *after* the workspace's (``None``:
    removed, every definition of *before* counts). The names set aside as too
    generic (short, common, plain words) are added to *generic* when given;
    the kept names that come from a string literal on a changed line (a schema
    key, named by tests that never import the module) to *literals*; the
    definitions found to *defined*.

    A string literal counts only when the change adds or removes it (it is on
    the changed lines of one side only: a key next to an edited description
    stays out) and when it is distinctive (:func:`distinctive_literal`): a
    JSON-schema keyword (``description``, ``properties``) or a short plain dict
    key (``sources``) is set aside as generic, never searched.

    A wording-only change of a Python file (:func:`wording_words`: comments
    and docstrings alone) names neither its definitions nor its module
    path: the words it replaced are the answer, also added to *wording* when
    given, so they select the tests pinning that text and no test importing the
    module for another reason.
    """
    old_text = reading.decode(before) if before is not None and reading.is_text(before) else None
    new_text = reading.decode(after) if after is not None and reading.is_text(after) else None
    if old_text is not None and new_text is not None:
        words = wording_words(path, old_text, new_text)
        if words is not None:
            if wording is not None:
                wording |= words
            return words
    names: set[str] = set()
    if old_text is not None and new_text is not None:
        old_lines, new_lines = _changed_lines(old_text.splitlines(), new_text.splitlines())
        removed: set[str] = set()
        added: set[str] = set()
        found: set[str] = set()
        names |= _touched(path, old_text, old_lines, removed, found) | _touched(path, new_text, new_lines, added, found)
        if defined is not None:
            defined |= found
        # A literal on both sides of the change (a key beside an edited description) is not what changed.
        changed = (removed ^ added) - found
        kept_literals = {n for n in changed if distinctive_literal(n)}
        names |= kept_literals
        if literals is not None:
            literals |= kept_literals
        if generic is not None:
            generic |= {n for n in changed - kept_literals if len(n) >= MIN_IDENTIFIER_CHARS}
    else:
        for text in (old_text, new_text):
            if text is not None:
                every = {s.name.rsplit(".", 1)[-1] for s in _searchable(reading.symbols(path, text) or [])}
                names |= every
                if defined is not None:
                    defined |= every
    out = {n for n in names if _kept(n)}
    if generic is not None:
        generic |= {n for n in names - out if len(n) >= MIN_IDENTIFIER_CHARS}
    module = python_module(path, base)
    if module:
        out.add(module)
    return out


def quotes(text: str) -> set[str]:
    """The identifier-like names *text* quotes whole: ``'key'``, ``"key"`` or ```key```."""
    return {m.group(2) for m in _QUOTED.finditer(text)}


def matcher(identifiers: Iterable[str]) -> re.Pattern[str] | None:
    """One word-bounded pattern over *identifiers* (the longest first, at most :data:`MAX_IDENTIFIERS`)."""
    kept = sorted(set(identifiers), key=lambda n: (-len(n), n))[:MAX_IDENTIFIERS]
    if not kept:
        return None
    return re.compile(r"(?<![A-Za-z0-9_.])(?:" + "|".join(re.escape(n) for n in kept) + r")(?![A-Za-z0-9_])")


def module_reference(module: str) -> Callable[[str], bool]:
    """Whether a text names the Python *module*: its import path, or ``from <package> import ... <module>``."""
    path = re.compile(r"(?<![A-Za-z0-9_.])" + re.escape(module) + r"(?![A-Za-z0-9_])")
    package, _, leaf = module.rpartition(".")
    imports = re.compile(r"^[ \t]*from[ \t]+" + re.escape(package) + r"[ \t]+import[ \t]+(\([^)]*\)|[^\n]*)", re.M)
    word = re.compile(r"(?<![A-Za-z0-9_.])" + re.escape(leaf) + r"(?![A-Za-z0-9_])")

    def names(text: str) -> bool:
        if path.search(text):
            return True
        return bool(package) and any(word.search(found.group(1)) for found in imports.finditer(text))

    return names


def select_tests(
    tests: dict[str, str],
    identifiers: Iterable[str],
    *,
    changed: Iterable[str] = (),
    scoped: Mapping[str, Iterable[str]] | None = None,
    quoted_only: Iterable[str] = (),
    common: set[str] | None = None,
    source_changed: bool = False,
    always: Iterable[str] = (),
    prose: Iterable[str] = (),
    tokens: set[str] | None = None,
    named: Iterable[str] = (),
    scans: Iterable[str] = (),
) -> dict[str, list[str]]:
    """The test files of *tests* (path -> text) that name an identifier, with the names found; *changed* tests always.

    *scoped* maps a changed Python module's import path to the names it
    defines: a test file referencing the module (:func:`module_reference`) is
    selected by it, and such a name counts only in a file that references its
    module, unless another source named it too (it is then in *identifiers*).
    A name of *quoted_only* (it came from a string literal alone) counts only
    in a file that quotes it (:func:`quotes`). A free name (no ``.``) found in
    more than :data:`COMMON_SHARE` of *tests* and more than
    :data:`COMMON_FLOOR` files selects nothing: it is added to *common* when
    given, and a file it alone selected is dropped.
    With *source_changed* (a non-test source file changed under the check's
    directory) every source-scan gate (:func:`is_source_scan`) not already
    selected is added with the hit :data:`SOURCE_SCAN_HIT` (*scans*: the check's own gate patterns, matched
    too), and every test file an *always* glob matches (:func:`matches_globs`) with :data:`ALWAYS_HIT`.
    A test file reading a changed *prose* file (repository paths, :func:`reads_prose`)
    is selected with the hit :data:`PROSE_HIT` and the path, ranked with the named hits.
    A test file naming a changed source file of *named* by its file name (:func:`names_file`) is selected
    with the hit :data:`NAMES_HIT` and the file name, unless more than the common bound of files name it.
    Ranked best first, so a bound on the files run keeps the most relevant: the
    changed tests, then the *always* gates, then the files naming a changed module's import path, then
    the files naming the most identifiers, then the source-scan gates; within a tier the files
    sharing the most name tokens with the change (*tokens*, :func:`affinity`) first, then by path.
    """
    free = set(identifiers)
    owners: dict[str, set[str]] = {}
    for module, names in (scoped or {}).items():
        for name in names:
            if name not in free:
                owners.setdefault(name, set()).add(module)
    references = {module: module_reference(module) for module in scoped or {}}
    selected: dict[str, list[str]] = {path: ["changed"] for path in changed}
    pattern = matcher(free | set(owners))
    literal = set(quoted_only) - set(owners)
    counts: dict[str, int] = {}
    for path, text in tests.items():
        if path in selected:
            continue
        seen = {module for module, names in references.items() if names(text)}
        found = set(pattern.findall(text)) if pattern is not None else set()
        if found & literal:
            found -= literal - quotes(text)
        hits = sorted({n for n in found if n in free or owners.get(n, set()) & seen} | seen)
        for name in hits:
            if name in free and not _dotted_module(name):
                counts[name] = counts.get(name, 0) + 1
        if hits:
            selected[path] = hits
    bound = max(COMMON_FLOOR, COMMON_SHARE * len(tests))
    too_common = {name for name, n in counts.items() if n > bound}
    if too_common:
        if common is not None:
            common |= too_common
        for path in [p for p, hits in selected.items() if hits != ["changed"]]:
            kept = [h for h in selected[path] if h not in too_common]
            if kept:
                selected[path] = kept
            else:
                del selected[path]
    pages = tuple(prose)
    if pages:
        for path, text in tests.items():
            if selected.get(path) == ["changed"]:
                continue
            read = [PROSE_HIT + page for page in pages if reads_prose(text, page)]
            if read:
                selected[path] = sorted({*selected.get(path, []), *read})
    sources = tuple(named)
    if sources:
        naming: dict[str, list[str]] = {}
        for path, text in tests.items():
            if selected.get(path) == ["changed"]:
                continue
            for source in sources:
                if names_file(text, source):
                    naming.setdefault(source, []).append(path)
        for source, readers in naming.items():
            hit = NAMES_HIT + source.rsplit("/", 1)[-1]
            if len(readers) > bound:
                if common is not None:
                    common.add(hit)
                continue
            for path in readers:
                selected[path] = sorted({*selected.get(path, []), hit})
    pinned: set[str] = set()
    if source_changed:
        gate_globs = tuple(always)
        gate_patterns = tuple(re.compile(pattern) for pattern in scans)
        for path, text in tests.items():
            if gate_globs and selected.get(path) != ["changed"] and matches_globs(path, gate_globs):
                selected[path] = [ALWAYS_HIT]
                pinned.add(path)
            elif path not in selected and is_source_scan(text, gate_patterns):
                selected[path] = [SOURCE_SCAN_HIT]

    related = tokens or set()

    def rank(item: tuple[str, list[str]]) -> tuple[int, int, int, str]:
        path, hits = item
        near = -affinity(path, related)
        if hits == ["changed"]:
            return (0, 0, near, path)
        if path in pinned:
            return (1, 0, near, path)
        if hits == [SOURCE_SCAN_HIT]:
            return (4, 0, near, path)
        return (2 if any("." in h for h in hits) else 3, -len(hits), near, path)

    return dict(sorted(selected.items(), key=rank))


def _prose_note(prose: list[str], tests: Mapping[str, list[str]], *, searched_all: bool) -> str:
    """The note on the changed prose files: never read for identifiers, and the tests that read each one."""
    parts = []
    for page in prose[:NAMED_IN_NOTE]:
        readers = [p for p, hits in tests.items() if PROSE_HIT + page in hits]
        if readers:
            parts.append(f"{page} (read by {', '.join(readers[:3])}" + (" ...)" if len(readers) > 3 else ")"))
        else:
            parts.append(f"{page} (no test read " + ("names it)" if searched_all else "so far names it)"))
    more = f" +{len(prose) - NAMED_IN_NOTE} more" if len(prose) > NAMED_IN_NOTE else ""
    return (
        f"{len(prose)} changed prose file(s) not read for identifiers (their headings name no test; the tests "
        "naming them or walking their directory are selected): " + "; ".join(parts) + more + "."
    )


@dataclass
class Selection:
    """The test files a checkout's changes affect, and how they were found."""

    #: Test file (relative to the check's directory) -> the identifiers it names, or ``["changed"]``.
    tests: dict[str, list[str]] = field(default_factory=dict)
    identifiers: set[str] = field(default_factory=set)
    sources: list[str] = field(default_factory=list)
    #: Import path of a changed Python module -> the names it defines, which select only the tests referencing it.
    scoped: dict[str, set[str]] = field(default_factory=dict)
    #: Names found only in a string literal on a changed line: they select only the tests quoting them.
    quoted_only: set[str] = field(default_factory=set)
    #: Free names set aside because too many of the test files read name them.
    common: set[str] = field(default_factory=set)
    #: Sentences about what was not read (a base unreadable, sources over the bound, the scan budget).
    notes: list[str] = field(default_factory=list)
    scanned: int = 0
    #: Test files left unread because the selection's time budget ended.
    unsearched: int = 0
    #: Selected test file -> the top-level directories it reads that the checkout does not hold (ranked last,
    #: never run by ``affected=true``: :meth:`runnable`).
    outside: dict[str, list[str]] = field(default_factory=dict)
    #: Selected test file -> the markers it applies that the check's ``-m "not X"`` deselects: pytest collects
    #: the file and runs none (or only part) of its tests, so the answer names them and the marker.
    deselected: dict[str, list[str]] = field(default_factory=dict)
    #: Deselected marker -> the checks of the same directory that select it (:func:`marker_runners`).
    runners: dict[str, list[str]] = field(default_factory=dict)

    def runnable(self) -> list[str]:
        """The selected test files this checkout can run: every one but those reading a directory it lacks."""
        return [path for path in self.tests if path not in self.outside]

    def describe(self, *, shown: int = 25, names: int = 12) -> str:
        ids = sorted(self.identifiers | {n for defined in self.scoped.values() for n in defined}, key=lambda n: (-len(n), n))
        head = (
            f"Affected tests: {len(self.tests)} test file(s) of {self.scanned} read, selected by "
            f"{len(ids)} identifier(s) from {len(self.sources)} changed file(s): "
            + ", ".join(ids[:names])
            + (f", +{len(ids) - names} more" if len(ids) > names else "")
            + "."
        )
        pinned = sum(1 for hits in self.tests.values() if hits == [ALWAYS_HIT])
        if pinned:
            head += (
                f" {pinned} of them ratchet gate(s) (always): the check's affected_always names them, they run first."
            )
        gates = sum(1 for hits in self.tests.values() if hits == [SOURCE_SCAN_HIT])
        if gates:
            head += f" {gates} of them source-scan gate(s): they read the whole source tree, so any source change selects them."
        rows = [
            f"- {path}: {', '.join(hits[:4])}"
            + (" ..." if len(hits) > 4 else "")
            + (f" [needs {', '.join(d + '/' for d in self.outside[path])}] (not run)" if path in self.outside else "")
            for path, hits in list(self.tests.items())[:shown]
        ]
        if len(self.tests) > shown:
            rows.append(f"- +{len(self.tests) - shown} more")
        if self.quoted_only:
            rows.append(
                "Names from a string literal alone select only the tests quoting them: "
                + ", ".join(sorted(self.quoted_only)[:5])
                + (" ..." if len(self.quoted_only) > 5 else "")
                + "."
            )
        if self.deselected:
            markers = sorted({m for found in self.deselected.values() for m in found})
            files = list(self.deselected)
            rows.append(
                f"Deselected by this check's -m: {len(files)} selected test file(s) are marked "
                + ", ".join(markers)
                + ", which this check does not run: "
                + ", ".join(files[:NAMED_IN_NOTE])
                + (f", +{len(files) - NAMED_IN_NOTE} more" if len(files) > NAMED_IN_NOTE else "")
                + ". "
                + self._runner_sentence(markers)
            )
        if self.scoped:
            rows.append(
                "Names defined by a changed Python module select only the tests that import it: "
                + ", ".join(sorted(self.scoped)[:5])
                + (" ..." if len(self.scoped) > 5 else "")
                + "."
            )
        return "\n".join([head, *rows, *self.notes])

    def _runner_sentence(self, markers: list[str]) -> str:
        """How to run the deselected files: the check naming each marker, else the generic advice."""
        named = [f"{', '.join(self.runners[m])} ({m})" for m in markers if self.runners.get(m)]
        if not named:
            return "Run them with the check that selects that marker (args: these files) before the push."
        missing = [m for m in markers if not self.runners.get(m)]
        return (
            "Run them with "
            + "; ".join(named)
            + " (args: these files) before the push."
            + (f" No check of this directory selects {', '.join(missing)}." if missing else "")
        )


async def affected_tests(
    session: WorkspaceSession,
    record: CheckoutRecord,
    changes: CheckoutChanges,
    *,
    cwd: str,
    read_base: BaseReader,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    always: Iterable[str] = (),
    scans: Iterable[str] = (),
    excluded: Iterable[str] = (),
) -> Selection:
    """The test files under the check directory *cwd* (workspace path) that the checkout's *changes* affect.

    *always* (the check's ``affected_always`` globs) names the test files selected on every source change;
    *scans* (its ``affected_scan_patterns``) the text of the source-scan gates selected on it.
    *excluded* (:func:`excluded_markers` of its argv) the pytest markers it deselects: a selected file
    applying one is listed in :attr:`Selection.deselected`.

    With *deadline* (a *clock* instant) the test files are read until then only: the
    rest are not searched and the selection's notes say how many.
    """
    out = Selection()
    started = clock()
    prefix = record.path + "/"
    base = cwd[len(prefix) :] if cwd.startswith(prefix) else ("" if cwd == record.path else cwd)
    marked = [(p, "M") for p in changes.modified] + [(p, "A") for p in changes.added] + [(p, "D") for p in changes.removed]
    prose = sorted(p for p, _mark in marked if is_prose_file(p))
    if prose:
        # A heading ("API", "Decisions") selected 25 unrelated test files: prose names no test.
        marked = [(p, m) for p, m in marked if not is_prose_file(p)]
    if len(marked) > MAX_SOURCES:
        out.notes.append(f"{len(marked) - MAX_SOURCES} changed file(s) past {MAX_SOURCES} were not read for identifiers.")
        marked = marked[:MAX_SOURCES]
    gate = asyncio.Semaphore(READ_AT_ONCE)
    unread_bases: list[str] = []
    generic: set[str] = set()
    quoted: dict[str, set[str]] = {}
    definitions: set[str] = set()
    #: Changed Python file -> the words of a wording-only change (comments and docstrings alone).
    worded: dict[str, set[str]] = {}

    async def one(repo_path: str, mark: str) -> set[str]:
        after = before = None
        async with gate:
            if mark != "D":
                try:
                    _entry, after = await session.get_bytes(record.workspace_path(repo_path))
                except (FileNotFoundError, ValueError):
                    after = None
            if mark != "A":
                got = await read_base(repo_path)
                if isinstance(got, bytes):
                    before = got
                else:
                    unread_bases.append(repo_path)
        literals = quoted.setdefault(repo_path, set())
        words = None if is_test_file(repo_path) else generic
        if before is not None and after is not None and reading.is_text(before) and reading.is_text(after):
            replaced = wording_words(repo_path, reading.decode(before), reading.decode(after))
            if replaced is not None:
                worded[repo_path] = replaced
                return replaced
        return changed_identifiers(repo_path, before, after, base=base, generic=words, literals=literals, defined=definitions)

    found = await asyncio.gather(*(one(p, m) for p, m in marked))
    own_names = 0
    for (repo_path, _mark), names in zip(marked, found, strict=True):
        out.sources.append(repo_path)
        module = python_module(repo_path, base)
        if is_test_file(repo_path):
            # A changed test runs because it changed: its literals and test names select no other file.
            own_names += 1
            quoted.pop(repo_path, None)
        elif repo_path in worded:
            # Wording alone: the replaced words select the tests pinning the text, the module path selects nothing.
            quoted.pop(repo_path, None)
            out.identifiers |= names
        elif module:
            # The literals stay free: a schema key is asserted by tests that never import its module.
            literals = quoted.get(repo_path, set())
            out.identifiers |= {module} | (names & literals)
            out.scoped.setdefault(module, set()).update(names - literals - {module})
        else:
            out.identifiers |= names
    if own_names:
        out.notes.append(f"{own_names} changed test file(s) run as changed; their own names select no other test file.")
    wording_only = sorted(p for p in worded if not is_test_file(p))
    if wording_only:
        out.notes.append(
            f"{len(wording_only)} changed file(s) change wording only (comments, docstrings): "
            + ", ".join(wording_only[:NAMED_IN_NOTE])
            + (f", +{len(wording_only) - NAMED_IN_NOTE} more" if len(wording_only) > NAMED_IN_NOTE else "")
            + "; only the tests naming a replaced word are selected, never by their definitions or module path."
        )
    for module in out.scoped:
        out.scoped[module] -= out.identifiers
    # A top-level directory name (``"frontend"`` in a checks list) is in every test reading that tree: no selector.
    tops = {t for t in top_directories(record) if t in out.identifiers}
    if tops:
        out.identifiers -= tops
        out.notes.append(
            f"{len(tops)} top-level directory name(s) of the repository select no test: "
            + ", ".join(sorted(tops))
            + "."
        )
    # A path literal is asserted inside longer text: it selects wherever it is written, quoted whole or not.
    out.quoted_only = {n for names in quoted.values() for n in names if n.isidentifier()} & (
        out.identifiers - definitions
    )
    generic -= out.identifiers | {n for defined in out.scoped.values() for n in defined}
    if generic:
        shown = sorted(generic)[:8]
        out.notes.append(
            f"{len(generic)} name(s) set aside as too generic to select tests by (plain words under "
            f"{MIN_PLAIN_WORD_CHARS} letters, common names, JSON-schema keywords, string literals under "
            f"{MIN_LITERAL_CHARS} letters without an underscore): "
            + ", ".join(shown)
            + (" ..." if len(generic) > len(shown) else "")
            + "."
        )
    if unread_bases:
        out.notes.append(
            f"Not read as checked out ({len(unread_bases)}): {', '.join(sorted(unread_bases)[:5])}; every definition of these files counts."
        )
    changed_tests = [
        posixpath.relpath(p, base) if base else p
        for p in (*changes.modified, *changes.added)
        if is_test_file(p) and (not base or is_under(p, base))
    ]
    entries = await session.entries(refresh=True)
    pins = tuple(always)
    tokens = change_tokens(p for p, _mark in marked) | change_tokens(prose)

    def pinned_first(item: tuple[str, int]) -> tuple[bool, int, str]:
        # The always gates are read first, then the test files most related by name: a time or byte
        # budget ending leaves the least related unread, never the gates.
        rel = posixpath.relpath(item[0], cwd) if cwd else item[0]
        return (not (pins and matches_globs(rel, pins)), -affinity(rel, tokens), item[0])

    candidates = sorted(
        (
            (path, entry.size)
            for path, entry in entries.items()
            if is_under(path, cwd)
            and is_test_file(path)
            and entry.size <= reading.MAX_TEXT_FILE_BYTES
            and "/node_modules/" not in path
        ),
        key=pinned_first,
    )
    texts: dict[str, str] = {}
    total = 0
    left = 0
    late = 0
    skipped: list[str] = []
    batch: list[str] = []

    async def read(path: str) -> tuple[str, bytes | None]:
        # Through the manifest read once above: get_bytes reads the whole manifest again for each file,
        # which spent a 90 s budget on 840 of 3,084 backend test files.
        try:
            _entry, data = await session.read_entry(entries[path])
        except (FileNotFoundError, WorkspaceFileUnreadableError):
            return path, None
        return path, data

    async def flush(paths: list[str]) -> None:
        for path, data in await asyncio.gather(*(read(p) for p in paths)):
            if data is not None and reading.is_text(data):
                texts[posixpath.relpath(path, cwd) if cwd else path] = reading.decode(data)

    for path, size in candidates:
        if deadline is not None and clock() >= deadline:
            late += 1
            skipped.append(path)
            continue
        if total + size > MAX_SCAN_BYTES:
            left += 1
            skipped.append(path)
            continue
        total += size
        batch.append(path)
        if len(batch) >= READ_AT_ONCE:
            await flush(batch)
            batch = []
    if batch:
        await flush(batch)
    if skipped:
        shown = [posixpath.relpath(p, cwd) if cwd else p for p in skipped[:NAMED_IN_NOTE]]
        first = ", ".join(shown) + (f", +{len(skipped) - len(shown)} more" if len(skipped) > len(shown) else "")
        why = []
        if late:
            budget = max(0.0, deadline - started) if deadline is not None else 0.0
            why.append(
                f"{late} test file(s) not searched: the selection's time budget ended (budget {budget:.0f} s; "
                f"{len(texts)} of {len(candidates)} read; the command keeps the rest of the call)"
            )
        if left:
            why.append(f"{left} test file(s) past the {format_size(MAX_SCAN_BYTES)} read budget were not searched")
        out.notes.append(
            "; ".join(why)
            + ". Read most related first (name tokens shared with the changed files); the first not searched: "
            + first
            + ". Name test files in args, or narrow the checkout, to search them."
        )
    out.unsearched = late
    out.scanned = len(texts)
    # A gate reading the whole tree is affected by any source file under the check's directory, whatever it names.
    source_changed = any(not is_test_file(p) and (not base or is_under(p, base)) for p, mark in marked if mark != "D")
    out.tests = select_tests(
        texts,
        out.identifiers,
        changed=changed_tests,
        scoped=out.scoped,
        quoted_only=out.quoted_only,
        common=out.common,
        source_changed=source_changed,
        always=pins,
        prose=prose,
        tokens=tokens,
        named=[p for p, mark in marked if mark != "D" and not is_test_file(p)],
        scans=scans,
    )
    if prose:
        out.notes.append(_prose_note(prose, out.tests, searched_all=not skipped))
    deselecting = tuple(excluded)
    if deselecting:
        out.deselected = {path: hit for path in out.tests if (hit := marked_with(texts.get(path, ""), deselecting))}
    absent = missing_directories(record)
    if absent:
        # A test reading a tree the checkout lacks fails on a missing file, not on the change: run it last.
        outside = {
            path: where
            for path, hits in out.tests.items()
            if hits != ["changed"] and (where := reads_directories(texts.get(path, ""), absent))
        }
        if outside:
            out.tests = {p: h for p, h in out.tests.items() if p not in outside} | {p: out.tests[p] for p in outside}
            out.outside = {p: outside[p] for p in out.tests if p in outside}
            grow = ", ".join(f'"{d}"' for d in sorted({d for dirs in outside.values() for d in dirs}))
            out.notes.append(
                f"Not runnable here: {len(outside)} selected test file(s) read a directory this checkout does not hold "
                f"(marked 'needs'), so they are not run: their failures would be a missing file, not the change. "
                f'To run them on the change: materialize_repository(path="{record.path}", add_paths=[{grow}]), '
                "or name them in args."
            )
    if out.common:
        shown = sorted(out.common)[:8]
        out.notes.append(
            f"{len(out.common)} name(s) set aside as named by more than {int(COMMON_SHARE * 100)}% of the test files read: "
            + ", ".join(shown)
            + (" ..." if len(out.common) > len(shown) else "")
            + "."
        )
    return out


# -- the changed files themselves (affected = "files" | "lines") ---------------


def line_ranges(before: str, after: str) -> list[tuple[int, int]]:
    """The 1-based inclusive line ranges of *after* that the change wrote (added or replaced), adjacent ones merged."""
    _old, new = _changed_lines(before.splitlines(), after.splitlines())
    ranges: list[tuple[int, int]] = []
    for n in sorted(new):
        if ranges and n == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], n)
        else:
            ranges.append((n, n))
    return ranges


def matches_globs(path: str, globs: Iterable[str]) -> bool:
    """Whether *path* (relative to the check's directory) matches one of *globs*, by its path or its name; no glob keeps all."""
    kept = tuple(globs)
    if not kept:
        return True
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(path, g) or fnmatch.fnmatchcase(name, g) for g in kept)


#: What an empty change means: nothing changed since the checkout, and no commit made from it changed a file the
#: check reads. A commit made from the checkout (``checkout.advance``) keeps its files in the change (#11122).
COMMITTED_LEFT_OUT = (
    "The change counts from the commit the checkout was made (or last rebased) at, commits made from it "
    "included: pass other files as args to check them."
)


def branch_marks(record: CheckoutRecord, changes: CheckoutChanges) -> dict[str, str]:
    """``{path: "A" | "M"}`` of the branch's change since the checkout's baseline: its commits and the workspace.

    A file a commit made from the checkout added stays ``A`` (checked whole)
    when the workspace edits it again; a file removed from the workspace
    since is left out.
    """
    marks = dict(record.branch_paths())
    for path in changes.modified:
        marks.setdefault(path, "M")
    for path in changes.added:
        marks[path] = "M" if marks.get(path) == "M" or record.branch.get(path) == "D" else "A"
    for path in changes.removed:
        marks.pop(path, None)
    return marks


@dataclass
class FileSelection:
    """The changed files a ``files`` or ``lines`` check runs on, as its arguments, and what was left out."""

    #: One argument per file, relative to the check's directory: ``PATH``, or ``PATH:A-B,C-D`` with ``lines``.
    args: list[str] = field(default_factory=list)
    #: Files of the change under the check's directory that no glob keeps.
    skipped: list[str] = field(default_factory=list)
    #: Modified files whose change only removes lines (no line to check), relative to the check's directory.
    removed_only: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def describe(self, *, mode: str, shown: int = 25, baseline: str = "") -> str:
        head = f"Changed {mode}: {len(self.args)} file(s) of the change, passed to the check."
        if baseline:
            head = head[:-1] + f" (baseline: {baseline})."
        rows = [f"- {a}" for a in self.args[:shown]]
        if len(self.args) > shown:
            rows.append(f"- +{len(self.args) - shown} more")
        if self.skipped:
            rows.append(
                f"{len(self.skipped)} changed file(s) outside the check's globs left out: "
                + ", ".join(self.skipped[:5])
                + (" ..." if len(self.skipped) > 5 else "")
                + "."
            )
        return "\n".join([head, *rows, *self.notes])


async def changed_files(
    session: WorkspaceSession,
    record: CheckoutRecord,
    changes: CheckoutChanges,
    *,
    cwd: str,
    read_base: BaseReader,
    globs: Iterable[str] = (),
    lines: bool = False,
    whole_repository: bool = False,
) -> FileSelection:
    """The files the checkout added or modified under the check directory *cwd*, kept by *globs*.

    With *whole_repository* (``affected_whole_repository``), a changed file outside *cwd* is kept too,
    passed relative to *cwd* (``../docker-compose.yml``): a script that maps the change to what a
    repository-wide selection reads (a test naming a compose file or a workflow) needs every path.

    With *lines*, a modified file is passed as ``PATH:A-B,C-D``, the line ranges
    the change wrote; an added file, or one whose checked-out version cannot be
    read, is passed whole (said in the notes); a modified file whose change only
    removes lines has no line to check and is left out (said in the notes).

    The change counts from the record's baseline (``CheckoutRecord.baseline``):
    the files the commits made from the checkout wrote are part of it, so a
    file the branch added stays whole and a modified one is ranged against
    the commit it was checked out at, never against the branch's own push.
    *read_base* reads a file at that baseline.
    """
    out = FileSelection()
    prefix = record.path + "/"
    base = cwd[len(prefix) :] if cwd.startswith(prefix) else ("" if cwd == record.path else cwd)
    whole: list[str] = []
    removals: list[str] = []
    kept_globs = tuple(globs)
    marked = sorted(branch_marks(record, changes).items())
    for repo_path, mark in marked:
        if base and not whole_repository and not is_under(repo_path, base):
            continue
        rel = posixpath.relpath(repo_path, base) if base else repo_path
        if not matches_globs(rel, kept_globs):
            out.skipped.append(rel)
            continue
        if not lines or mark == "A":
            out.args.append(rel)
            continue
        before = await read_base(repo_path)
        try:
            _entry, after = await session.get_bytes(record.workspace_path(repo_path))
        except (FileNotFoundError, ValueError):
            continue
        if not isinstance(before, bytes) or not reading.is_text(before) or not reading.is_text(after):
            whole.append(rel)
            out.args.append(rel)
            continue
        ranges = line_ranges(reading.decode(before), reading.decode(after))
        if not ranges:
            removals.append(rel)
            continue
        out.args.append(rel + ":" + ",".join(f"{a}-{b}" for a, b in ranges))
    out.removed_only = removals
    if whole:
        out.notes.append(f"Not read as checked out, passed whole ({len(whole)}): {', '.join(whole[:5])}.")
    if removals:
        out.notes.append(f"Only lines removed, nothing to check ({len(removals)}): {', '.join(removals[:5])}.")
    return out


_RANGED_ARG = re.compile(r"^.+?:\d+-\d+(?:,\d+-\d+)*$")


def narrow_args(given: Iterable[str], selection: FileSelection) -> tuple[list[str], list[str]]:
    """*given* (a call's args, relative to the check's directory) narrowed to the change *selection* holds.

    A bare path the change modified becomes its ``PATH:A-B,C-D`` entry, a directory
    (``.`` included) the changed files under it; a file the change only removed lines
    from is left out. An argument already ranged (``PATH:A-B``), an option (``-x``) and
    a path the change did not touch pass unchanged, the last said in the notes, so a
    formatter fixing a file main never formatted rewrites the change's lines, not the
    whole file. Options alone (``--write``) run on the whole change, as no args would.
    Returns ``(args, notes)``.
    """
    given = list(given)
    if not any(not a.startswith("-") for a in given):
        return [*given, *selection.args], []
    by_path = {(a.rsplit(":", 1)[0] if _RANGED_ARG.match(a) else a): a for a in selection.args}
    removed = set(selection.removed_only)
    out: list[str] = []
    untouched: list[str] = []
    dropped: list[str] = []
    for arg in given:
        if arg.startswith("-") or _RANGED_ARG.match(arg):
            out.append(arg)
            continue
        path = posixpath.normpath(arg)
        if path in by_path:
            out.append(by_path[path])
            continue
        if path in removed:
            dropped.append(path)
            continue
        under = [entry for p, entry in by_path.items() if path == "." or is_under(p, path)]
        if under:
            out.extend(e for e in under if e not in out)
            continue
        untouched.append(arg)
        out.append(arg)
    notes: list[str] = []
    if untouched:
        notes.append(f"Not changed since the checkout, passed whole ({len(untouched)}): {', '.join(untouched[:5])}.")
    if dropped:
        notes.append(f"Only lines removed, left out ({len(dropped)}): {', '.join(dropped[:5])}.")
    return out, notes
