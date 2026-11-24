#!/usr/bin/env python3
"""Enterprise Edition license headers: the one module that reads the edition list (#5493).

``licensing/editions.yml`` lists the Enterprise Edition (EE) files; every other
file is Community Edition (CE), as LICENSE states. An EE source file starts with
the notice of the "XTM One Enterprise Edition License" section of LICENSE, as a
``#`` block in Python, shell and PowerShell and a ``/* */`` block in TypeScript,
JavaScript, Rust and CSS, the OpenCTI format. The notice is read from LICENSE,
never copied: changing the copyright year is a LICENSE edit followed by ``--fix``. An EE directory also
holds a ``copyright.md`` saying the whole directory is EE, which covers the
files a comment cannot mark, and names the third-party files it holds, which
keep their own license. A package manifest under an EE glob (``Cargo.toml``,
``package.json``, a ``Dockerfile`` label) declares :data:`EE_LICENSE_ID`.

Nothing becomes CE without a decision: each directory under :data:`SCOPE` is
wholly EE, wholly CE (``ce``) or mixed (``mixed``), and in a mixed one each
source file and each child directory is classified: a source file by an
``ee`` glob, or by the CE marker (:data:`CE_MARKER`) on its first line, the
decision written in the file so no shared list collects them. A new one that
is not classified fails the build.

    uv run python scripts/license_headers.py --check # exit 1 and list what is wrong
    uv run python scripts/license_headers.py --fix   # add, update or remove headers
    uv run python scripts/license_headers.py --staged  # the pre-commit hook: check what the commit holds
    uv run python scripts/license_headers.py --which PATH...  # the edition of a file, even one not written yet
    uv run python scripts/license_headers.py --mark-ce PATH...  # decide these files are CE
    uv run python scripts/license_headers.py --new-ce BASE  # the files HEAD makes CE since BASE

``--fix`` keeps each file's line endings (CRLF or LF), byte order mark,
shebang and Python encoding cookie, and inserts the header before the module
docstring, so ``from __future__`` stays the first statement. Running it twice
changes nothing.

Contract: dev-docs/reference/editions.md.
"""

from __future__ import annotations

import argparse
import functools
import glob as globlib
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
EDITIONS_FILE = "licensing/editions.yml"
#: What a CE source file of a mixed directory starts with: the decision that it is
#: CE, made where the file is, so no shared list collects every decision. The
#: SPDX form, which licence tooling reads.
CE_MARKER = "SPDX-License-Identifier: Apache-2.0"
#: The trees the edition list covers.
SCOPE = ("backend/app", "frontend/src", "apps")
DIRECTORY_NOTICE = "copyright.md"

#: File names without a suffix that name a file, not a directory, for ``--which``.
_EXTENSIONLESS_FILES = frozenset({"Dockerfile", "Containerfile", "Makefile", "Procfile", "LICENSE", "NOTICE", "README"})

#: Comment style per source suffix: ``#`` lines or one ``/* */`` block.
LINE_COMMENT = {".py", ".sh", ".ps1"}
BLOCK_COMMENT = {".ts", ".tsx", ".js", ".mjs", ".rs", ".css"}
SOURCE_SUFFIXES = LINE_COMMENT | BLOCK_COMMENT

#: Present in every form of the notice: how an existing header is recognised.
MARKER = 'Enterprise Edition ("EE")'
#: The SPDX identifier of the EE license, for the manifests that declare one.
EE_LICENSE_ID = "LicenseRef-XTM-One-Enterprise-Edition"
#: What a package or an image holding both editions declares: the backend and
#: frontend projects and the main images ship CE and EE files together.
MIXED_LICENSE = f"Apache-2.0 AND {EE_LICENSE_ID}"
#: The package manifests that declare a license.
MANIFESTS = ("Cargo.toml", "package.json", "Dockerfile")
_CARGO_PACKAGE = re.compile(r"\[(?:workspace\.)?package\]\s*$", re.MULTILINE)
_CARGO_LICENSE = re.compile(r'^(license\s*=\s*")([^"]*)(")', re.MULTILINE)
_JSON_LICENSE = re.compile(r'([{,]\s*"license"\s*:\s*")([^"]*)(")')
_LABEL_LICENSE = re.compile(r'(org\.opencontainers\.image\.licenses=")([^"]*)(")')
#: The last words of the notice: where a Python header ends, so a comment of
#: the module's own right after it is never taken for part of it.
_HEADER_END = "either express or implied."
_THIRD_PARTY_INTRO = (
    "The following files are not part of the XTM One Enterprise Edition: they",
    "belong to their respective owners and remain under their own license or terms.",
)
_SECTION_TITLE = "XTM One Enterprise Edition License"
_SECTION_RULE = re.compile(r"^-{20,}$")
_CODING_COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+")

# What :func:`file_problems` reports after ``<path>: ``; the pre-commit hook
# chooses its advice by them, so each is spelled here once.
EE_HEADER_MISSING = "missing or outdated EE header"
CE_WITH_EE_HEADER = "CE file carries the EE header"
NOT_UTF8 = "not UTF-8 text, so its license header cannot be checked: save it as UTF-8"
NOT_JSON = "not valid UTF-8 JSON, so its license cannot be checked"
#: In every problem of a file or directory that nobody classified.
NOT_CLASSIFIED = "not classified"
#: What the pre-commit hook adds to :data:`CE_WITH_EE_HEADER`: it sees renames.
MOVED_OUT_OF_EE = " (an EE file moved out of EE stays EE: classify its new path)"
HEADER_END_UNKNOWN = (
    "starts with an EE header that does not end as the notice does, so where it ends cannot be told: "
    "remove that header by hand, then run --fix"
)


@dataclass(frozen=True)
class Editions:
    """The globs of ``licensing/editions.yml``, relative to the repository root."""

    ee: tuple[str, ...]
    except_: tuple[str, ...]
    no_header: tuple[str, ...]
    third_party: tuple[str, ...] = ()
    #: The mixed directories: each of their source files and child directories
    #: is classified one by one (the trees of :data:`SCOPE` first).
    mixed: tuple[str, ...] = ()
    #: The wholly Community Edition directories: every file under them is CE,
    #: in their child directories too.
    ce: tuple[str, ...] = ()
    #: The edition of each path asked once: every check asks it of the whole tree.
    _ee_of: dict[str, bool] = field(default_factory=dict, init=False, repr=False, compare=False, hash=False)

    def is_ee(self, path: str) -> bool:
        ee = self._ee_of.get(path)
        if ee is None:
            ee = _matches(path, self.ee) and not _matches(path, self.except_ + self.third_party)
            self._ee_of[path] = ee
        return ee

    def needs_header(self, path: str) -> bool:
        return PurePosixPath(path).suffix in SOURCE_SUFFIXES and self.is_ee(path) and not _matches(path, self.no_header)

    def ee_directories(self) -> tuple[str, ...]:
        """The ``<dir>/**`` globs with no ``except`` entry under them: wholly EE."""
        directories = []
        for glob in self.ee:
            directory = glob.removesuffix("/**")
            if directory == glob or any(c in directory for c in "*?["):
                continue
            if not any(e.startswith(directory + "/") for e in self.except_):
                directories.append(directory)
        return tuple(directories)

    def third_party_in(self, directory: str) -> tuple[str, ...]:
        """The ``third_party`` globs under ``directory``, relative to it."""
        prefix = directory + "/"
        return tuple(g.removeprefix(prefix) for g in self.third_party if g.startswith(prefix))


@functools.cache
def _matcher(glob: str):
    """``glob`` compiled once, exactly as ``PurePosixPath.full_match`` compiles it.

    ``full_match`` keeps 512 compiled patterns; the list holds more than that,
    so every call compiled its pattern again and checking the tree took minutes."""
    pattern = PurePosixPath(glob)
    text = str(pattern) if pattern.parts else ""
    return re.compile(globlib.translate(text, recursive=True, include_hidden=True, seps="/")).match


def _full_match(path: str, glob: str) -> bool:
    """``PurePosixPath(path).full_match(glob)``, with ``glob`` compiled once."""
    return _matcher(glob)(path) is not None


def _matches(path: str, globs: tuple[str, ...]) -> bool:
    return any(_full_match(path, glob) for glob in globs)


#: Reads a file of the repository by its path relative to the root: its text, or
#: None when there is no such file. Raises UnicodeDecodeError on a file that is
#: not UTF-8. The disk for CI and ``--fix``, the index for the pre-commit hook.
Reader = Callable[[str], str | None]


def disk_reader(root: Path) -> Reader:
    """A :data:`Reader` of the working tree under ``root``."""

    def read(path: str) -> str | None:
        target = root / path
        return _read(target) if target.is_file() else None

    return read


def load_editions(root: Path = REPO_ROOT, read: Reader | None = None) -> Editions:
    read = read or disk_reader(root)
    data = yaml.safe_load(read(EDITIONS_FILE) or "") or {}
    return Editions(
        ee=tuple(data.get("ee") or ()),
        except_=tuple(data.get("except") or ()),
        no_header=tuple(data.get("no_header") or ()),
        third_party=tuple(data.get("third_party") or ()),
        mixed=tuple(data.get("mixed") or ()),
        ce=tuple(data.get("ce") or ()),
    )


def mixed_directories(editions: Editions, files: tuple[str, ...]) -> frozenset[str]:
    """The directories where the edition of each new source file is a decision,
    not a given: the ``mixed`` ones, and any other that holds EE source files
    beside other files (not wholly EE)."""
    wholly_ee = editions.ee_directories()
    return frozenset(editions.mixed) | frozenset(
        str(PurePosixPath(path).parent)
        for path in files
        if editions.needs_header(path) and not any(path.startswith(d + "/") for d in wholly_ee)
    )


def ce_marker_line(suffix: str, newline: str = "\n") -> str:
    """The :data:`CE_MARKER` comment line of a ``suffix`` file, ending with its newline."""
    if suffix in LINE_COMMENT:
        return f"# {CE_MARKER}{newline}"
    if suffix == ".css":
        return f"/* {CE_MARKER} */{newline}"
    return f"// {CE_MARKER}{newline}"


def has_ce_marker(text: str, suffix: str) -> bool:
    """Whether ``text`` starts, after its shebang or other prelude (and an EE header,
    so an EE file that also carries the marker is still seen), with the CE marker."""
    _, body = _split_prelude(without_header(text, suffix), suffix)
    first = body.lstrip("\r\n").split("\n", 1)[0].strip()
    # The exact line: `Apache-2.0 AND LicenseRef-...` or any other expression is no CE decision.
    return first == ce_marker_line(suffix).strip()


def ce_marked(text: str, suffix: str) -> str:
    """``text`` starting with the CE marker, after its prelude; unchanged when it does."""
    if has_ce_marker(text, suffix):
        return text
    newline = "\r\n" if "\r\n" in text else "\n"
    prelude, body = _split_prelude(text, suffix)
    return prelude + ce_marker_line(suffix, newline) + (newline + body if body else "")


def unmarked_ce_files(editions: Editions, files: tuple[str, ...], read: Reader) -> list[str]:
    """The source files of the mixed directories that nobody classified, and the EE
    files that carry the CE marker.

    A file matched by no ``ee`` glob is CE, as LICENSE states, so a new agentic
    file in a mixed directory (``app/services``, ``src/lib``) would ship under
    Apache without anyone deciding it, and CE cannot become EE once a version
    holds it. Each such file is either matched by an ``ee`` glob or starts with
    the CE marker (:data:`CE_MARKER`), the decision written in the file itself."""
    mixed = mixed_directories(editions, files)
    problems = []
    for path in files:
        pure = PurePosixPath(path)
        if pure.suffix not in SOURCE_SUFFIXES or _matches(path, editions.no_header + editions.third_party):
            continue
        ee = editions.is_ee(path)
        in_mixed = str(pure.parent) in mixed and not _matches(path, editions.except_)
        if not ee and not in_mixed:
            continue
        try:
            text = read(path)
        except UnicodeDecodeError:
            continue  # file_problems reports it
        if text is None:
            continue
        marked = has_ce_marker(text, pure.suffix)
        if ee and marked:
            problems.append(
                f"{path}: carries the CE marker but an `ee` glob covers it, {NOT_CLASSIFIED}: decide "
                f"its edition (EE: remove the marker; CE: narrow the glob in {EDITIONS_FILE})"
            )
        elif not ee and not marked:
            problems.append(
                f"{path}: CE by default in a mixed directory, {NOT_CLASSIFIED}. Agentic: add a glob to `ee` in "
                f"{EDITIONS_FILE}; otherwise start it with `{ce_marker_line(pure.suffix).strip()}` "
                f"(`license_headers.py --mark-ce {path}`)"
            )
    return problems


def edition_of(
    path: str,
    editions: Editions,
    mixed: frozenset[str] | None = None,
    *,
    directory: bool = False,
    marked: bool = False,
) -> tuple[str, str]:
    """``(edition, reason)`` of ``path``, a file (or, with ``directory``, a
    directory) that need not exist yet.

    The edition is ``EE``, ``EE (unmarked)`` (an EE glob covers it, but nothing
    can mark it), ``CE``, ``to classify`` (a source file of a mixed directory, or
    any file of a new directory in one, that nobody classified yet: the build
    fails until someone does), ``third-party``, ``unmarked`` (tests: never a
    header) or ``outside`` (a tree the list does not cover). A ``CE`` that no
    entry decided is worded ``CE (default)``. ``mixed`` defaults to the ``mixed``
    entries (:func:`mixed_directories` adds the ones the files show). A directory
    is ``EE``, ``CE``, ``mixed`` or ``to classify`` (a new one in a mixed directory)."""
    mixed = frozenset(editions.mixed) if mixed is None else mixed
    if not any(path == tree or path.startswith(tree + "/") for tree in SCOPE):
        return "outside", f"not under {', '.join(SCOPE)}: the edition list does not cover it"
    if directory:
        return _directory_edition(path, editions, mixed)
    if _matches(path, editions.third_party):
        return "third-party", "listed under `third_party`: keeps its owner's license"
    if _matches(path, editions.no_header):
        return "unmarked", "matched by `no_header` (tests never carry a header)"
    ee_glob = next((g for g in editions.ee if _full_match(path, g)), None)
    except_glob = next((g for g in editions.except_ if _full_match(path, g)), None)
    if ee_glob and except_glob:
        return "CE", f"`{ee_glob}` is EE, but `except: {except_glob}` leaves this file CE"
    if (
        ee_glob
        and PurePosixPath(path).suffix not in SOURCE_SUFFIXES
        and not any(path.startswith(directory + "/") for directory in editions.ee_directories())
    ):
        return "EE (unmarked)", (
            f"matched by `ee: {ee_glob}`, but neither a header nor a copyright.md marks a file of this kind "
            "here, so it reads as CE: cover its directory with a `<dir>/**` glob to mark it"
        )
    if ee_glob:
        return "EE", f"matched by `ee: {ee_glob}`"
    pure = PurePosixPath(path)
    for ancestor in (str(a) for a in pure.parents):
        if ancestor in editions.ce:
            return "CE", f"under `{ancestor}`, a CE directory (`ce` in {EDITIONS_FILE})"
        if ancestor in mixed:
            if ancestor != str(pure.parent):
                # A new directory is a decision of its own, marker or not.
                new = "/".join(pure.parts[: len(PurePosixPath(ancestor).parts) + 1])
                return "to classify", f"`{new}` is a new directory in the mixed directory `{ancestor}`"
            if marked:
                return "CE", f"it starts with the CE marker (`{CE_MARKER}`)"
            if pure.suffix in SOURCE_SUFFIXES:
                return "to classify", f"`{ancestor}` is a mixed directory: each source file there is classified"
            break
    if marked:
        return "CE", f"it starts with the CE marker (`{CE_MARKER}`)"
    return "CE (default)", "no `ee` glob matches it"


def _directory_edition(path: str, editions: Editions, mixed: frozenset[str]) -> tuple[str, str]:
    """``(edition, reason)`` of the directory ``path``: its own entry, else the
    nearest directory above it that is wholly EE, wholly CE or mixed decides."""
    ee_directories = {g.removesuffix("/**") for g in editions.ee if g.endswith("/**")}
    pure = PurePosixPath(path)
    for directory in (path, *(str(a) for a in pure.parents)):
        if directory in ee_directories:
            excepted = [e for e in editions.except_ if e.startswith(directory + "/")]
            if excepted:
                listed = ", ".join(f"`{e}`" for e in excepted)
                return "EE", f"`{directory}/**` is in `ee`: every file under it is EE except {listed} (`except`, CE)"
            return "EE", f"`{directory}/**` is in `ee`: every file under it is EE"
        if directory in editions.ce:
            return "CE", f"`{directory}` is a CE directory (`ce` in {EDITIONS_FILE}): every file under it is CE"
        if directory in mixed:
            if directory == path:
                return "mixed", f"`{path}` is mixed: each source file and child directory in it is classified"
            new = "/".join(pure.parts[: len(PurePosixPath(directory).parts) + 1])
            return "to classify", f"`{new}` is a new directory in the mixed directory `{directory}`"
    return "CE (default)", "no `ee` glob or `ce` entry covers it"


def edition_report(
    path: str,
    editions: Editions,
    files: tuple[str, ...],
    mixed: frozenset[str] | None = None,
    *,
    directory: bool = False,
    marked: bool = False,
) -> list[str]:
    """The lines ``--which`` prints for ``path``; the second kind of line is a warning.
    ``mixed`` is :func:`mixed_directories` of ``files``, computed here when not given."""
    if mixed is None:
        mixed = mixed_directories(editions, files)
    edition, reason = edition_of(path, editions, mixed, directory=directory, marked=marked)
    lines = [f"{path}: {edition} - {reason}"]
    if edition == "to classify":
        lines.append(
            f"  WARNING: the build fails until it is classified in {EDITIONS_FILE}. Agentic: EE, a glob in `ee` "
            f"(`<dir>/**` for a new directory); otherwise CE, the CE marker at the top of the file "
            f"(`license_headers.py --mark-ce <path>`) or the directory in `ce`; a new directory holding both goes "
            "to `mixed`. Once released as CE (Apache) it cannot "
            "be taken back; the other way round always can."
        )
    elif edition == "CE (default)" and PurePosixPath(path).suffix in SOURCE_SUFFIXES:
        lines.append(f"  Note: CE by default. If this file is agentic it is EE: add a glob to `ee` in {EDITIONS_FILE}.")
    return lines


def notice(root: Path = REPO_ROOT, read: Reader | None = None) -> tuple[str, ...]:
    """The lines of the EE notice: the last section of LICENSE titled
    "XTM One Enterprise Edition License", between its two rules."""
    lines = ((read or disk_reader(root))("LICENSE") or "").splitlines()
    for i in range(len(lines) - 2, -1, -1):
        if lines[i].strip() == _SECTION_TITLE and _SECTION_RULE.match(lines[i + 1].strip()):
            body = lines[i + 2 :]
            end = next((j for j, line in enumerate(body) if _SECTION_RULE.match(line.strip())), None)
            if end:
                return tuple(line.rstrip() for line in body[:end])
    raise ValueError(f'LICENSE has no "{_SECTION_TITLE}" notice section between two rules')


def header(notice_lines: tuple[str, ...], suffix: str, newline: str = "\n") -> str:
    """The header block for a ``suffix`` file, ending with its newline."""
    if suffix in LINE_COMMENT:
        lines = [f"# {line}" if line else "#" for line in notice_lines]
    else:
        lines = ["/*", *notice_lines, "*/"]
    return newline.join(lines) + newline


def directory_notice(notice_lines: tuple[str, ...], third_party: tuple[str, ...] = ()) -> str:
    """The content of an EE directory's ``copyright.md``: the notice, then the
    third-party files of the directory (globs relative to it), if any."""
    text = (
        "\n".join(notice_lines)
        .replace("This file is part of", "This directory is part of")
        .replace("you may not use this file", "you may not use the files in this directory")
    )
    if third_party:
        text += "\n\n" + "\n".join(_THIRD_PARTY_INTRO) + "\n\n" + "\n".join(f"- `{g}`" for g in third_party)
    return text + "\n"


def manifest_fixed(name: str, text: str) -> str:
    """``text``, a package manifest named ``name``, declaring :data:`EE_LICENSE_ID`
    where it declares its own license: the ``[package]`` table of a ``Cargo.toml``
    (never another table's ``license`` key), the top-level ``license`` of a
    ``package.json``, the image label of a ``Dockerfile``. A manifest that
    declares none is left as it is."""

    def declare(pattern: re.Pattern[str], part: str, count: int = 0) -> str:
        return pattern.sub(lambda m: m.group(1) + EE_LICENSE_ID + m.group(3), part, count=count)

    if name == "Cargo.toml":
        tables = re.split(r"(?m)^(?=\[)", text)
        return "".join(declare(_CARGO_LICENSE, t) if _CARGO_PACKAGE.match(t) else t for t in tables)
    if name == "package.json":
        if "license" not in json.loads(text):
            return text
        # The top-level key only: a nested "license" (in "config", say) may come before it.
        # The match starts on the `{` or `,` before the key, so the key itself sits at depth 1.
        top = next((m for m in _JSON_LICENSE.finditer(text) if _json_depth(text, m.start() + 1) == 1), None)
        if top is None:
            return text
        return text[: top.start()] + top.group(1) + EE_LICENSE_ID + top.group(3) + text[top.end() :]
    return declare(_LABEL_LICENSE, text)


def _json_depth(text: str, end: int) -> int:
    """How many objects and arrays of the JSON ``text`` are open at ``end``,
    brackets inside strings left out."""
    depth, in_string, escaped = 0, False, False
    for char in text[:end]:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
    return depth


def _split_prelude(text: str, suffix: str) -> tuple[str, str]:
    """``(prelude, body)``: the byte order mark, the shebang, a Python encoding
    cookie and PowerShell ``#Requires`` lines must stay first; the header goes
    right after them."""
    end = 1 if text.startswith("\ufeff") else 0
    index = 0
    while True:
        line_end = text.find("\n", end)
        line = text[end : (len(text) if line_end < 0 else line_end + 1)]
        if not line:
            break
        is_shebang = index == 0 and line.startswith("#!")
        is_cookie = suffix == ".py" and index < 2 and _CODING_COOKIE.match(line)
        is_requires = suffix == ".ps1" and line.lower().startswith("#requires")
        if not (is_shebang or is_cookie or is_requires):
            break
        end += len(line)
        index += 1
    return text[:end], text[end:]


def _strip_header(body: str, suffix: str, newline: str, notice_length: int | None = None) -> str:
    """``body`` without a leading comment block that carries :data:`MARKER`,
    nor the blank line that follows it.

    A ``#`` header ends on the last words of the notice. One written from an
    older notice may not hold them. To read a file, the header is then the
    whole leading ``#`` block. To rewrite one (``notice_length`` given, the line
    count of the current header), it is that block only when the block is
    exactly that long; otherwise where it ends cannot be told, and ``body`` is
    returned unchanged rather than losing the module's own leading comments
    (:func:`file_problems` reports it)."""
    if suffix in LINE_COMMENT:
        lines = body.splitlines(keepends=True)
        block_end = next((i for i, line in enumerate(lines) if not line.startswith("#")), len(lines))
        count = next((i + 1 for i, line in enumerate(lines[:block_end]) if _HEADER_END in line), None)
        if count is None:
            if notice_length is not None and block_end != notice_length:
                return body
            count = block_end
        block, rest = "".join(lines[:count]), "".join(lines[count:])
    else:
        if not body.startswith("/*") or "*/" not in body:
            return body
        close = body.index("*/") + 2
        block, rest = body[:close], body[close:].removeprefix(newline)
    if MARKER not in block:
        return body
    return rest.removeprefix(newline)


def without_header(text: str, suffix: str = ".py", notice_length: int | None = None) -> str:
    """``text`` without its EE header: what a test scanning a module's source
    for a word reads, so the license URL is not taken for the module's own.
    With ``notice_length`` (the line count of the current notice), for a
    rewrite, a ``#`` header is only stripped where its end is certain
    (:func:`_strip_header`)."""
    newline = "\r\n" if "\r\n" in text else "\n"
    prelude, body = _split_prelude(text, suffix)
    return prelude + _strip_header(body, suffix, newline, notice_length)


def _header_left(text: str, suffix: str, notice_length: int) -> bool:
    """Whether ``text`` still starts with an EE header once :func:`without_header`
    is done: one whose end cannot be told, left for a person to remove."""
    _, body = _split_prelude(without_header(text, suffix, notice_length), suffix)
    if suffix not in LINE_COMMENT:
        return False
    lines = body.splitlines()
    block = lines[: next((i for i, line in enumerate(lines) if not line.startswith("#")), len(lines))]
    return any(MARKER in line for line in block)


def fixed(text: str, suffix: str, *, ee: bool, notice_lines: tuple[str, ...]) -> str:
    """``text`` with exactly the header its edition calls for at the top."""
    newline = "\r\n" if "\r\n" in text else "\n"
    stripped = without_header(text, suffix, len(notice_lines))
    if not ee:
        return stripped
    prelude, body = _split_prelude(stripped, suffix)
    return prelude + header(notice_lines, suffix, newline) + (newline + body if body else "")


def tagged(path: str, text: str, root: Path = REPO_ROOT) -> str:
    """``text`` with the header the file at ``path`` (relative to the repository
    root) must carry: a generator that writes a source file passes its output
    through this, so regenerating never drops the header."""
    editions = load_editions(root)
    suffix = PurePosixPath(path).suffix
    return fixed(text, suffix, ee=editions.needs_header(path), notice_lines=notice(root))


def repository_files(root: Path = REPO_ROOT) -> tuple[str, ...]:
    """The files under :data:`SCOPE`, relative to ``root``: tracked or new but
    not ignored, so ``--fix`` tags a file before its first ``git add``, and
    still on disk."""
    output = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *SCOPE],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout.decode()
    return tuple(sorted({path for path in output.split("\0") if path and (root / path).is_file()}))


def _read(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def _write(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def unclassified_directories(editions: Editions, files: tuple[str, ...], *, whole: bool = True) -> list[str]:
    """The child directories of the mixed ones that nobody classified, and the
    ``ce`` and ``mixed`` entries that no longer hold.

    A directory defaults to CE when no ``ee`` glob matches it, so a new agentic
    directory would be published under Apache without anyone deciding it. Each
    child directory of a ``mixed`` one is wholly EE (a ``<dir>/**`` glob in
    ``ee``), wholly CE (``ce``) or mixed itself (``mixed``); a directory under a
    wholly EE or wholly CE one follows it. Tests follow no edition, so a
    directory holding only ``no_header`` files is no question."""
    ee_directories = {g.removesuffix("/**") for g in editions.ee if g.endswith("/**")}
    mixed, ce = set(editions.mixed), set(editions.ce)
    present: set[str] = set()
    children: dict[str, set[str]] = {}
    for path in files:
        if _matches(path, editions.no_header):
            continue
        parts = PurePosixPath(path).parts
        for depth in range(1, len(parts)):
            directory = "/".join(parts[:depth])
            present.add(directory)
            if depth > 1:
                children.setdefault("/".join(parts[: depth - 1]), set()).add(directory)
    problems = []
    for parent in sorted(mixed - ee_directories - ce):  # a contradictory entry is reported below
        for directory in sorted(children.get(parent, set()) - ee_directories - mixed - ce):
            problems.append(
                f"{directory}: new directory, {NOT_CLASSIFIED}. Agentic: add `{directory}/**` to `ee`; otherwise "
                f"add `{directory}` to `ce`, or to `mixed` if it holds both, in {EDITIONS_FILE}"
            )
    for key, entries in (("mixed", editions.mixed), ("ce", editions.ce)):
        if len(set(entries)) != len(entries):
            problems.append(f"{EDITIONS_FILE}: an entry of `{key}` is listed twice")
        for directory in sorted(set(entries)):
            if directory not in present:
                if not whole:
                    continue
                problems.append(f"{EDITIONS_FILE}: `{key}` entry `{directory}` is no directory (renamed or removed?)")
            elif directory not in SCOPE and str(PurePosixPath(directory).parent) not in mixed:
                problems.append(
                    f"{EDITIONS_FILE}: `{key}` entry `{directory}` is not in a `mixed` directory: its parent decides"
                )
    for directory in sorted((mixed & ce) | ((mixed | ce) & ee_directories)):
        problems.append(f"{EDITIONS_FILE}: `{directory}` is classified twice (`ee`, `mixed`, `ce`)")
    for directory in sorted(ce):
        held = next((p for p in files if p.startswith(directory + "/") and editions.is_ee(p)), None)
        if held:
            problems.append(f"{EDITIONS_FILE}: `ce` directory `{directory}` holds an EE file ({held}): it is `mixed`")
    return problems


def list_problems(editions: Editions, files: tuple[str, ...], read: Reader, *, whole: bool = True) -> list[str]:
    """What is wrong with the classification itself, whatever headers the files
    carry: a new directory nobody classified, a new file of a mixed directory
    with neither an `ee` glob nor the CE marker, an EE file with the marker, a
    glob or an entry that matches nothing (a stale ``no_header`` glob too: it
    wins over ``ee``, so a leftover one would leave EE files without the header).
    Not ``whole`` (a partial checkout): what only the whole tree can tell, a glob
    or an entry that matches nothing, is not reported."""
    problems = unclassified_directories(editions, files, whole=whole) + unmarked_ce_files(editions, files, read)
    if not whole:
        return problems
    for glob in editions.ee + editions.except_ + editions.third_party + editions.no_header:
        if not any(_full_match(path, glob) for path in files):
            problems.append(f"{EDITIONS_FILE}: `{glob}` matches no file (renamed or removed?)")
    return problems


def scope_problems(editions: Editions) -> list[str]:
    """The trees of :data:`SCOPE` the real list leaves out of ``mixed``: nothing
    would be asked there. (A scratch list in a test may leave them out.)"""
    return [
        f"{EDITIONS_FILE}: `{tree}` is not in `mixed`: its new files and directories would not be asked about"
        for tree in SCOPE
        if tree not in editions.mixed
    ]


def file_problems(
    editions: Editions,
    notice_lines: tuple[str, ...],
    files: tuple[str, ...],
    read: Reader,
    *,
    write: Callable[[str, str | None], None] | None = None,
    held: Callable[[str], bool] | None = None,
) -> list[str]:
    """What the files of ``files`` carry wrong: an EE manifest declaring another
    license, an EE source file without the exact header, a CE one with it, a
    ``copyright.md`` missing, outdated or outside an EE directory. The one check
    CI (every file, from the disk) and the pre-commit hook (the files a commit
    holds, from the index) share. With ``write``, each fix is written (``None``
    removes the file). With ``held`` (a partial checkout), the ``copyright.md``
    of a directory the checkout does not hold is not judged."""
    problems = []
    for path in files:
        pure = PurePosixPath(path)
        if pure.name in MANIFESTS and editions.is_ee(path):
            try:
                text = read(path)
                expected = manifest_fixed(pure.name, text) if text is not None else None
            except (UnicodeDecodeError, ValueError):
                problems.append(f"{path}: {NOT_JSON}")
                expected = text = None
            if expected != text:
                problems.append(f"{path}: declares a license other than {EE_LICENSE_ID}")
                if write:
                    write(path, expected)
        suffix = pure.suffix
        if suffix not in SOURCE_SUFFIXES:
            continue
        try:
            text = read(path)
        except UnicodeDecodeError:
            problems.append(f"{path}: {NOT_UTF8}")
            continue
        if text is None:
            continue
        ee = editions.needs_header(path)
        if MARKER in text and _header_left(text, suffix, len(notice_lines)):
            problems.append(f"{path}: {HEADER_END_UNKNOWN}")
            continue
        if ee or MARKER in text:
            expected = fixed(text, suffix, ee=ee, notice_lines=notice_lines)
            if expected != text:
                problems.append(f"{path}: {EE_HEADER_MISSING if ee else CE_WITH_EE_HEADER}")
                if write:
                    write(path, expected)

    directories = {d for d in editions.ee_directories() if any(p.startswith(d + "/") for p in files)}
    for directory in sorted(directories):
        if held is not None and not held(f"{directory}/{DIRECTORY_NOTICE}"):
            continue
        expected_notice = directory_notice(notice_lines, editions.third_party_in(directory))
        target = f"{directory}/{DIRECTORY_NOTICE}"
        if read(target) != expected_notice:
            problems.append(f"{target}: missing or outdated")
            if write:
                write(target, expected_notice)
    for path in files:
        pure = PurePosixPath(path)
        if pure.name == DIRECTORY_NOTICE and str(pure.parent) not in directories:
            problems.append(f"{path}: not an EE directory")
            if write:
                write(path, None)
    return problems


def run(
    root: Path,
    files: tuple[str, ...],
    *,
    fix: bool,
    scope: tuple[str, ...] = (),
) -> list[str]:
    """Check (or fix) every file of ``files``; return what is (or was) wrong.

    ``scope`` (the sub_paths of a partial checkout, :func:`checkout_scope`)
    keeps the files under it and drops what only the whole tree can tell."""
    held = _holder(scope)
    if held is not None:
        files = tuple(p for p in files if held(p))

    editions = load_editions(root)
    notice_lines = notice(root)

    def write(path: str, text: str | None) -> None:
        if text is None:
            (root / path).unlink()
        else:
            _write(root / path, text)

    read = disk_reader(root)
    problems = list_problems(editions, files, read, whole=held is None)
    problems += file_problems(editions, notice_lines, files, read, write=write if fix else None, held=held)
    return problems


#: What the sandbox writes at the root of a checkout it took from the repository
#: (``materialize_repository``): its ``sub_paths`` say which part of the tree it holds.
CHECKOUT_RECORD = ".xtm-checkout"
#: Its name on a checkout written before the rename.
LEGACY_CHECKOUT_RECORD = ".xtm-checkout.json"


def checkout_scope(root: Path = REPO_ROOT) -> tuple[str, ...]:
    """The sub_paths of a partial sandbox checkout at ``root``; ``()`` for a whole tree (a clone, CI).

    A partial checkout lacks most of the tree: judged whole, every glob of the
    list would match nothing and every EE directory miss its ``copyright.md``."""
    record = root / CHECKOUT_RECORD
    if not record.is_file():
        record = root / LEGACY_CHECKOUT_RECORD
    try:
        payload = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    subs = payload.get("sub_paths") if isinstance(payload, dict) else None
    if not isinstance(subs, list):
        return ()
    return tuple(s.strip("/") for s in subs if isinstance(s, str) and s.strip("/"))


def _holder(scope: tuple[str, ...]) -> Callable[[str], bool] | None:
    """Whether a repository path lies under ``scope``; ``None`` for a whole tree."""
    if not scope:
        return None
    return lambda path: any(path == s or path.startswith(s + "/") for s in scope)


def which(paths: list[str], base: Path, root: Path = REPO_ROOT) -> list[str]:
    """The ``--which`` report of ``paths``, relative ones read from ``base``. A path
    is a directory when it ends with ``/``, is one on disk, or names no file and
    has no suffix (``backend/app/newpkg``), a ``Dockerfile`` or a ``Makefile`` aside."""
    editions = load_editions(root)
    files = repository_files(root)
    known = set(files)
    mixed = mixed_directories(editions, files)
    lines: list[str] = []
    for path in paths:
        target = base / path
        try:
            relative = target.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            lines.append(f"{path}: outside the repository")
            continue
        name = PurePosixPath(relative).name
        directory = (
            path.endswith("/")
            or target.is_dir()
            or (relative not in known and not PurePosixPath(relative).suffix and name not in _EXTENSIONLESS_FILES)
        )
        marked = False
        if not directory and relative in known and PurePosixPath(relative).suffix in SOURCE_SUFFIXES:
            try:
                marked = has_ce_marker(_read(root / relative), PurePosixPath(relative).suffix)
            except UnicodeDecodeError:
                marked = False
        lines.extend(edition_report(relative, editions, files, mixed, directory=directory, marked=marked))
    return lines


#: The editions under which a file reads as CE, or as no edition of ours: what
#: cannot be taken back once released. Every other one keeps the file EE, or
#: stops the build until it is classified.
_NOT_EE = frozenset({"CE", "CE (default)", "EE (unmarked)", "unmarked", "third-party"})


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout.decode()


class _RevisionReader:
    """A :data:`Reader` of the files of one commit, through a single ``git cat-file --batch``."""

    def __init__(self, root: Path, revision: str) -> None:
        self._revision = revision
        self._batch = subprocess.Popen(
            ["git", "cat-file", "--batch"], cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )

    def __call__(self, path: str) -> str | None:
        if self._batch.stdin is None or self._batch.stdout is None:
            raise RuntimeError("git cat-file --batch has no pipes")
        self._batch.stdin.write(f"{self._revision}:{path}\n".encode())
        self._batch.stdin.flush()
        header = self._batch.stdout.readline().decode().split()
        if len(header) < 3 or header[1] != "blob":
            return None
        data = self._batch.stdout.read(int(header[2]))
        self._batch.stdout.read(1)  # the newline after each object
        return data.decode("utf-8")

    def close(self) -> None:
        if self._batch.stdin:
            self._batch.stdin.close()
        self._batch.wait()


def _marked(path: str, read: Reader) -> bool:
    suffix = PurePosixPath(path).suffix
    if suffix not in SOURCE_SUFFIXES:
        return False
    try:
        text = read(path)
    except UnicodeDecodeError:
        return False
    return text is not None and has_ce_marker(text, suffix)


def becoming_ce(root: Path, base: str) -> list[str]:
    """The files under :data:`SCOPE` that ``HEAD`` makes CE since its merge base
    with ``base``: each a decision that cannot be taken back once released.

    A file is listed when it reads as CE at ``HEAD`` and did not before: an
    existing file that an ``ee`` glob no longer covers, that an ``except``,
    ``third_party`` or ``no_header`` entry now takes, or that gains the CE marker;
    a new file that the list of the merge base did not already make CE, so one
    with the CE marker (a copied CE file carries the marker of the file it came
    from), one in a new ``ce`` directory, one under a new ``except`` entry. A
    new file in a directory already ``ce`` is not listed: that directory was
    decided. A renamed file keeps the decision of its old path."""
    merge_base = _git(root, "merge-base", base, "HEAD").strip()
    diff = _git(root, "diff", "-z", "--name-status", "-M", merge_base, "HEAD", "--", *SCOPE, EDITIONS_FILE)
    fields = [f for f in diff.split("\0") if f]
    changed: set[str] = set()
    renamed: dict[str, str] = {}
    index = 0
    while index < len(fields):
        status = fields[index]
        if status.startswith(("R", "C")):
            old, new = fields[index + 1], fields[index + 2]
            if status.startswith("R"):
                renamed[new] = old
            changed.add(new)
            index += 3
        else:
            changed.add(fields[index + 1])
            index += 2
    list_changed = EDITIONS_FILE in changed
    if not changed:
        return []

    head_files = tuple(
        p for p in _git(root, "ls-tree", "-r", "-z", "--name-only", "HEAD", "--", *SCOPE).split("\0") if p
    )
    base_files = tuple(
        p for p in _git(root, "ls-tree", "-r", "-z", "--name-only", merge_base, "--", *SCOPE).split("\0") if p
    )
    base_known = set(base_files)
    head_read = _RevisionReader(root, "HEAD")
    base_read = _RevisionReader(root, merge_base)
    try:
        head_editions, base_editions = load_editions(root, head_read), load_editions(root, base_read)
        head_mixed = mixed_directories(head_editions, head_files)
        base_mixed = mixed_directories(base_editions, base_files)
        listed = []
        for path in head_files:
            if not list_changed and path not in changed:
                continue  # same content, same list: same edition
            head_marked = _marked(path, head_read)
            if edition_of(path, head_editions, head_mixed, marked=head_marked)[0] not in _NOT_EE:
                continue
            old = renamed.get(path, path)
            if old in base_known:
                base_marked = head_marked if path not in changed else _marked(old, base_read)
                before = edition_of(old, base_editions, base_mixed, marked=base_marked)[0]
            else:
                # A new file is CE only by what the list already decided, never by its own marker.
                before = edition_of(path, base_editions, base_mixed)[0]
            if before not in _NOT_EE:
                listed.append(path)
        return listed
    finally:
        head_read.close()
        base_read.close()


def mark_ce(paths: list[str], base: Path, root: Path = REPO_ROOT) -> list[str]:
    """Start each source file of ``paths`` (relative ones read from ``base``) with
    the CE marker: the decision that it is CE, by whoever runs this. An EE file,
    a non-source file or a file out of the repository is refused, not marked."""
    editions = load_editions(root)
    lines: list[str] = []
    for path in paths:
        try:
            relative = (base / path).resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            lines.append(f"{path}: refused, outside the repository")
            continue
        suffix = PurePosixPath(relative).suffix
        target = root / relative
        if not target.exists():
            lines.append(f"{relative}: refused, no such file: write it first (`make edition` answers before)")
        elif not target.is_file() or suffix not in SOURCE_SUFFIXES:
            lines.append(f"{relative}: refused, not a source file ({', '.join(sorted(SOURCE_SUFFIXES))})")
        elif editions.is_ee(relative):
            lines.append(f"{relative}: refused, an `ee` glob covers it: it is EE")
        elif _matches(relative, editions.third_party):
            lines.append(f"{relative}: refused, listed under `third_party`: it keeps its owner's license")
        elif _matches(relative, editions.no_header):
            lines.append(f"{relative}: refused, matched by `no_header`: tests follow no edition")
        else:
            text = _read(target)
            marked = ce_marked(text, suffix)
            if marked != text:
                _write(target, marked)
            lines.append(f"{relative}: CE marker {'added' if marked != text else 'already there'}")
    return lines


def _to_classify(lines: list[str]) -> bool:
    """Whether a ``--which`` report names a path the build would stop on."""
    return any(": to classify - " in line for line in lines)


def _git_z(root: Path, *args: str) -> list[str]:
    """The entries of a ``git ... -z`` listing."""
    return [entry for entry in _git(root, *args).split("\0") if entry]


class IndexReader:
    """A :data:`Reader` of the git index: the content a commit is about to hold.

    Only regular files are read (modes 100644 and 100755): a symlink's blob is
    its target path and a gitlink has no blob, neither is a file to check. One
    ``git cat-file --batch`` process (:class:`_RevisionReader` on ``:path``, the
    index's stage-0 entry) serves every read, so a commit of thousands of files
    costs one process, not one per file. Use it as a context manager."""

    def __init__(self, root: Path) -> None:
        self.blobs: dict[str, str] = {}
        for entry in _git_z(root, "ls-files", "-z", "--stage"):
            meta, path = entry.split("\t", 1)
            mode, blob, stage = meta.split(" ")
            if stage == "0" and mode in ("100644", "100755"):
                self.blobs[path] = blob
        self._read = _RevisionReader(root, "")

    def __enter__(self) -> IndexReader:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._read.close()

    def paths(self) -> tuple[str, ...]:
        """The files of the index under :data:`SCOPE`."""
        return tuple(sorted(p for p in self.blobs if any(p.startswith(tree + "/") for tree in SCOPE)))

    def __call__(self, path: str) -> str | None:
        return self._read(path) if path in self.blobs else None


def check_staged(root: Path = REPO_ROOT) -> list[str]:
    """What the commit being made holds wrong: the pre-commit hook.

    It reads the index, never the working tree, and writes nothing: under
    ``git commit <paths>`` git hands the hook a temporary index, and a hook that
    staged a fix there would leave the real one behind. The edition list and
    LICENSE are read from the index too, so an unstaged edit neither passes nor
    blocks a commit. The files the commit holds go through the same check as
    CI (:func:`file_problems`); a commit that changes LICENSE or the edition
    list checks every file, since the notice of each header comes from the one
    and the edition of each file from the other. The list itself is checked
    against the files of the index (an untracked file is no one's decision
    yet), and a problem stops the commit only when the commit touches it."""
    scope = (*SCOPE, "licensing", "LICENSE")
    staged = _git_z(root, "diff", "--cached", "-z", "--name-only", "--diff-filter=ACMR", "--", *scope)
    removed = _git_z(root, "diff", "--cached", "-z", "--name-only", "--diff-filter=DR", "--", *scope)
    if not staged and not removed:
        return []
    # The new path of each renamed file: only there can a CE file carry an EE header by moving.
    renamed = _git_z(root, "diff", "--cached", "-z", "--name-only", "-M", "--diff-filter=R", "--", *scope)
    with IndexReader(root) as read:
        editions = load_editions(root, read)
        notice_lines = notice(root, read)
        files = read.paths()
        # LICENSE gives every header its notice, the list every file its edition.
        everything = "LICENSE" in staged or EDITIONS_FILE in staged
        # A removed file (a `copyright.md` deleted alone) leaves its directory to check.
        emptied = {str(PurePosixPath(p).parent) for p in removed}
        touched = {p for p in staged if p in read.blobs and p != "LICENSE"}
        touched |= {p for p in files if str(PurePosixPath(p).parent) in emptied}
        checked = files if everything else tuple(sorted(touched))
        problems = file_problems(editions, notice_lines, checked, read)
        listed = list_problems(editions, files, read)
    moved = set(renamed)
    for index, problem in enumerate(problems):
        if problem.endswith(f": {CE_WITH_EE_HEADER}") and problem.split(":", 1)[0] in moved:
            problems[index] = problem + MOVED_OUT_OF_EE
    list_touched = bool(removed) or any(p.startswith("licensing/") for p in staged)
    for problem in listed:
        subject = problem.split(":", 1)[0]
        if EDITIONS_FILE in staged:
            problems.append(problem)  # a change to the list can leave any file or directory unclassified
        elif subject == EDITIONS_FILE:
            if list_touched:
                problems.append(problem)
        elif any(p == subject or p.startswith(subject + "/") for p in staged):
            problems.append(problem)
    return problems


def _staged_steps(problems: list[str]) -> str:
    """What to do about the problems the pre-commit hook found, in order. A
    decision never comes from `make license`: it would strip the header of an EE
    file that moved out of EE and ship it as CE, which cannot be taken back."""
    entries = [p for p in problems if p.startswith(f"{EDITIONS_FILE}:")]
    decide = [p for p in problems if p not in entries and (NOT_CLASSIFIED in p or p.endswith(MOVED_OUT_OF_EE))]
    manual = [p for p in problems if p.endswith((NOT_UTF8, NOT_JSON, HEADER_END_UNKNOWN))]
    steps = []
    if entries:
        steps.append(
            f"fix the entries of {EDITIONS_FILE} reported: remove or rename one that matches no file or names no "
            "directory, list each once, and make a directory classified twice, or a `ce` one holding an EE file, "
            "one thing (`mixed` when it holds both)"
        )
    if decide:
        steps.append(
            f"classify what is not classified: agentic, a glob in {EDITIONS_FILE}; otherwise `make mark-ce P=<path>`, which a code owner confirms on the pull request (`make edition P=<path>` "
            "says how; agentic is EE, and an EE file that moved stays EE: remove its header only to make it CE)"
        )
    if manual:
        steps.append(
            "save the files reported as not UTF-8 or not valid JSON as valid UTF-8, and remove by hand an EE header "
            "whose end cannot be told"
        )
    if len(entries) + len(decide) + len(manual) < len(problems):
        steps.append("run `make license` (or `cd backend && uv run python scripts/license_headers.py --fix`)")
    return (
        f"{', then '.join(steps)}, stage what changed, and commit again. "
        "`git commit --no-verify` skips this; CI does not."
    )


def _undecided(problems: list[str]) -> list[str]:
    """The problems of the classification: they need a decision, not a rewrite."""
    return [p for p in problems if p.startswith(EDITIONS_FILE) or NOT_CLASSIFIED in p]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="list what is wrong, exit 1 if anything is")
    mode.add_argument("--fix", action="store_true", help="add, update or remove headers and copyright.md files")
    mode.add_argument("--which", nargs="+", metavar="PATH", help="the edition of these paths, written or not yet")
    mode.add_argument("--mark-ce", nargs="+", metavar="PATH", help="decide these files are CE: add the CE marker")
    mode.add_argument("--check-list", action="store_true", help="check the classification only, not the headers")
    mode.add_argument("--new-ce", metavar="BASE", help="list the files HEAD makes CE since its merge base with BASE")
    mode.add_argument("--staged", action="store_true", help="pre-commit: check what the commit holds, write nothing")
    parser.add_argument(
        "--from",
        dest="base",
        metavar="DIR",
        help="with --which or --mark-ce: read relative paths from DIR (default: .)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="with --new-ce: the checkout to read",
    )
    args = parser.parse_args(argv)

    if args.new_ce:
        print("\n".join(becoming_ce(args.root, args.new_ce)))
        return 0
    if args.check_list:
        editions = load_editions(REPO_ROOT)
        files = repository_files(REPO_ROOT)
        listed = scope_problems(editions) + list_problems(editions, files, disk_reader(REPO_ROOT))
        print("\n".join(listed))
        return 1 if listed else 0
    if args.mark_ce:
        lines = mark_ce(args.mark_ce, Path(args.base) if args.base else Path.cwd())
        print("\n".join(lines))
        return 1 if any(": refused" in line for line in lines) else 0
    if args.which:
        lines = which(args.which, Path(args.base) if args.base else Path.cwd())
        print("\n".join(lines))
        # A path the build would stop on fails, so a hook or an agent can tell.
        return 1 if _to_classify(lines) else 0
    if args.staged:
        try:
            staged_problems = check_staged(REPO_ROOT)
        except (yaml.YAMLError, ValueError, UnicodeDecodeError, subprocess.CalledProcessError) as exc:
            # Still 1, the commit stops, but with what to do rather than a traceback.
            print(
                f"The EE license header check could not read what the commit holds: {exc}\n"
                f"Fix the file it names ({EDITIONS_FILE} that is not valid YAML, a LICENSE without its "
                "EE notice section) and commit again; `git commit --no-verify` skips this, CI does not."
            )
            return 1
        if staged_problems:
            print("\n".join(staged_problems))
            print(f"\nThe commit stops: {_staged_steps(staged_problems)}")
        return 1 if staged_problems else 0
    scope = checkout_scope(REPO_ROOT)
    problems = scope_problems(load_editions(REPO_ROOT)) + run(
        REPO_ROOT, repository_files(REPO_ROOT), fix=args.fix, scope=scope
    )
    if scope:
        print(f"Partial checkout ({', '.join(scope)}): its files only; CI checks the whole tree.")
    for problem in problems:
        print(problem)
    undecided = _undecided(problems)
    if args.fix:
        print(f"{len(problems) - len(undecided)} file(s) fixed" if len(problems) > len(undecided) else "Nothing to fix")
        return 1 if undecided else 0
    if problems:
        print(f"\n{len(problems)} problem(s). Run: cd backend && uv run python scripts/license_headers.py --fix")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
