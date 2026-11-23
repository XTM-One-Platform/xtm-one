"""Every tracked text file is UTF-8 without BOM, stored with LF.

The checks read the staged blobs, never the work tree. A file whose bytes are
the test is marked ``-text`` in ``.gitattributes`` and skipped. A file that
quotes mojibake on purpose is listed in :data:`KNOWN_MOJIBAKE_QUOTES`; the
list only shrinks, so an entry that no longer matches fails until removed.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

import pytest

from tests import app_sources

REPO_ROOT = app_sources.REPO_ROOT

#: Files that quote mojibake on purpose, as paths relative to the repository.
KNOWN_MOJIBAKE_QUOTES: dict[str, str] = {
    "backend/app/services/nova_guard.py": "explains how UTF-8 read as Latin-1 turns 'café' into mojibake",
}

#: A UTF-8 lead byte read as cp1252 (U+00C2-U+00F4) followed by continuation
#: bytes read as cp1252: U+0080-U+00BF, the cp1252 punctuation that replaces
#: ``\x80``-``\x9f``, and the C1 code points .NET keeps for the five bytes
#: cp1252 leaves undefined (they sit in U+0080-U+00BF too).
_MOJIBAKE_RUN = re.compile(
    "[\u00c2-\u00f4]"
    "[\u0080-\u00bf\u0152\u0153\u0160\u0161\u0178\u017d\u017e\u0192\u02c6\u02dc"
    "\u2013\u2014\u2018-\u201e\u2020-\u2022\u2026\u2030\u2039\u203a\u20ac\u2122]+"
)
_CP1252_UNDEFINED = frozenset("\x81\x8d\x8f\x90\x9d")
_BARE_CR = re.compile(rb"\r(?!\n)")
_BOMS = (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")
_REPORTED = 40


@functools.cache
def _git() -> str:
    """The git executable. Fails on GitHub Actions when there is none or no
    checkout: a skipped tripwire is a green build that checked nothing.

    The job is told by ``GITHUB_ACTIONS``, not ``CI``: the sandbox command
    containers set ``CI=1`` for non-interactive tools and run on a checkout
    without ``.git`` (``materialize_repository`` writes none), where the test
    has nothing to read and skips."""
    git = shutil.which("git")
    if not git or not (REPO_ROOT / ".git").exists():
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("the Backend job must run from a git checkout with git installed")
        pytest.skip("not a git checkout")
    return git


def _run_git(*args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(  # noqa: S603
        [_git(), *args], cwd=REPO_ROOT, input=stdin, capture_output=True, check=True
    ).stdout


@dataclass(frozen=True)
class _Entry:
    path: str
    #: ``lf``, ``crlf``, ``mixed``, ``none``, or ``-text`` when Git takes the content for a binary.
    index_eol: str
    attributes: frozenset[str]
    #: The staged blob, ``""`` for a symlink or a submodule.
    blob: str

    @property
    def opted_out(self) -> bool:
        """``-text`` (``binary`` included) in ``.gitattributes``: stored byte for byte, never judged."""
        return "-text" in self.attributes


@functools.cache
def _tracked() -> tuple[_Entry, ...]:
    """Every tracked file: how the index stores it, its attributes and its blob."""
    blobs: dict[str, str] = {}
    for record in filter(None, _run_git("ls-files", "--stage", "-z").decode("utf-8").split("\0")):
        info, path = record.split("\t", 1)
        mode, blob, _stage = info.split()
        if mode in ("100644", "100755"):
            blobs.setdefault(path, blob)
    entries = []
    for record in filter(None, _run_git("ls-files", "--eol", "-z").decode("utf-8").split("\0")):
        info, path = record.split("\t", 1)
        index_eol, _worktree_eol, attributes = info.split(maxsplit=2)
        entries.append(
            _Entry(
                path=path,
                index_eol=index_eol.removeprefix("i/"),
                attributes=frozenset(attributes.removeprefix("attr/").split()),
                blob=blobs.get(path, ""),
            )
        )
    return tuple(entries)


@dataclass(frozen=True)
class _Scan:
    path: str
    index_eol: str
    attributes: frozenset[str]
    bom: bool
    bare_cr: bool
    ascii: bool
    utf8: bool
    #: Each mojibake sequence found, with the character it restores to.
    mojibake: tuple[tuple[str, str], ...]


def _restore(sequence: str) -> str | None:
    """The character a mojibake sequence spelled, or None when it is not one."""
    octets = bytearray()
    for char in sequence:
        if char in _CP1252_UNDEFINED:
            octets.append(ord(char))
            continue
        try:
            octets += char.encode("cp1252")
        except UnicodeEncodeError:
            return None
    try:
        return octets.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _mojibake(text: str) -> tuple[tuple[str, str], ...]:
    """Judge each sequence on its own: its lead byte says how many continuation
    bytes follow, so a real ``”`` right after mojibake does not hide it."""
    found = set()
    for run in _MOJIBAKE_RUN.findall(text):
        lead = ord(run[0])
        sequence = run[: 2 + (lead >= 0xE0) + (lead >= 0xF0)]
        if (restored := _restore(sequence)) is not None:
            found.add((sequence, restored))
    return tuple(sorted(found))


@functools.cache
def _scans() -> tuple[_Scan, ...]:
    """Every staged text blob, read once through ``git cat-file --batch``.

    Binaries (a NUL byte) and the files marked ``-text`` are left out, except a
    UTF-16 file: Git takes it for a binary, and it is reported for its BOM.
    """
    entries = [entry for entry in _tracked() if entry.blob and not entry.opted_out]
    out = _run_git("cat-file", "--batch", stdin="".join(f"{entry.blob}\n" for entry in entries).encode())
    scans = []
    position = 0
    for entry in entries:
        header_end = out.index(b"\n", position)
        size = int(out[position:header_end].split()[2])
        data = out[header_end + 1 : header_end + 1 + size]
        position = header_end + 1 + size + 1  # the content is followed by a newline
        if b"\0" in data and not data.startswith(_BOMS[1:]):
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        scans.append(
            _Scan(
                path=entry.path,
                index_eol=entry.index_eol,
                attributes=entry.attributes,
                bom=data.startswith(_BOMS),
                bare_cr=bool(_BARE_CR.search(data)),
                ascii=data.isascii(),
                utf8=text is not None,
                mojibake=_mojibake(text) if text is not None else (),
            )
        )
    return tuple(scans)


def _fail(problem: str, paths: list[str], fix: str) -> None:
    shown = "\n".join(f"  {path}" for path in paths[:_REPORTED])
    more = f"\n  ... and {len(paths) - _REPORTED} more" if len(paths) > _REPORTED else ""
    pytest.fail(f"{len(paths)} tracked file(s) {problem}:\n{shown}{more}\nFix: {fix}")


def test_text_files_are_stored_as_lf_by_default():
    attributes = next(entry.attributes for entry in _tracked() if entry.path == "backend/app/main.py")
    assert attributes == {"text=auto", "eol=lf"}, (
        f"backend/app/main.py has attributes {sorted(attributes)}: .gitattributes must keep `* text=auto eol=lf`"
    )


def test_no_text_file_is_stored_with_crlf():
    offenders = [entry.path for entry in _tracked() if entry.index_eol in ("crlf", "mixed") and not entry.opted_out]
    if offenders:
        _fail("are stored with CRLF or mixed line endings", offenders, "git add --renormalize . && git commit")


def test_no_bare_carriage_return():
    offenders = [scan.path for scan in _scans() if scan.bare_cr]
    if offenders:
        _fail("contain a carriage return not followed by a line feed", offenders, "replace each `\\r\\r\\n` with `\\n`")


def test_no_byte_order_mark():
    offenders = [scan.path for scan in _scans() if scan.bom]
    if offenders:
        _fail(
            "start with a byte order mark (UTF-8 or UTF-16)",
            offenders,
            "write them as UTF-8 without BOM: drop a UTF-8 BOM, re-encode a UTF-16 file",
        )


def test_text_files_are_valid_utf8():
    # A file Git itself takes for a binary (``-text``) may hold any bytes.
    offenders = [scan.path for scan in _scans() if not scan.utf8 and scan.index_eol != "-text"]
    if offenders:
        _fail("are not valid UTF-8", offenders, "re-encode them as UTF-8")


def test_windows_scripts_are_ascii():
    # Windows PowerShell 5.1 reads BOM-less UTF-8 as cp1252, and the BOM is
    # banned: a non-ASCII character in a script it runs comes out garbled.
    offenders = [scan.path for scan in _scans() if "eol=crlf" in scan.attributes and not scan.ascii]
    if offenders:
        _fail("checked out with CRLF for Windows are not ASCII", offenders, "spell each non-ASCII character in ASCII")


def test_no_mojibake():
    offenders = [
        f"{scan.path}: {', '.join(f'{sequence} -> {restored}' for sequence, restored in scan.mojibake[:5])}"
        for scan in _scans()
        if scan.mojibake and scan.path not in KNOWN_MOJIBAKE_QUOTES
    ]
    if offenders:
        _fail("contain UTF-8 read back as cp1252", offenders, "replace each sequence with the character it restores to")


@pytest.mark.parametrize("path", sorted(KNOWN_MOJIBAKE_QUOTES))
def test_known_mojibake_quotes_still_match(path):
    assert any(scan.mojibake for scan in _scans() if scan.path == path), (
        f"{path} no longer quotes mojibake: remove it from KNOWN_MOJIBAKE_QUOTES"
    )
