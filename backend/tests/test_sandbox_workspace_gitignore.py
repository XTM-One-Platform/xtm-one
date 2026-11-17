"""A checkout's change set honours its .gitignore files (#10650).

A build in a checkout (``yarn build``) writes ``frontend/dist`` and
``frontend/tsconfig.tsbuildinfo``: untracked files the repository ignores.
They are never a change of ``sandbox_checkout_status``, ``sandbox_checkout_diff``
or ``create_github_commit(from_checkout=...)``, only counted; a tracked file
stays a change whatever ``.gitignore`` says.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pytest

from app.services.sandbox_workspace import detect
from app.services.sandbox_workspace.checkout import CheckoutRecord, build_record, changes_since, format_status
from app.services.sandbox_workspace.gitignore import GitIgnore, gitignore_paths

_ROOT_RULES = b"node_modules/\nfrontend/dist/\nfrontend/tsconfig.tsbuildinfo\n*.log\n!keep.log\n"


@dataclass(frozen=True)
class _Entry:
    sha256: str


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record(files: dict[str, bytes]) -> CheckoutRecord:
    return build_record(
        provider="github", repository="acme/widgets", ref="main", commit="0123abcd", path="co", sub_paths=(), files=files.items()
    )


def _manifest(record: CheckoutRecord, extra: dict[str, bytes]) -> dict[str, _Entry]:
    entries = {record.workspace_path(p): _Entry(f.sha256) for p, f in record.files.items()}
    entries[record.record_path] = _Entry("record")
    entries.update({record.workspace_path(p): _Entry(_sha(data)) for p, data in extra.items()})
    return entries


@pytest.mark.parametrize(
    ("rules", "path", "ignored"),
    [
        (b"dist/\n", "frontend/dist/index.js", True),
        (b"dist/\n", "dist", False),
        (b"/build\n", "build/a.js", True),
        (b"/build\n", "src/build/a.js", False),
        (b"frontend/dist/\n", "frontend/dist/assets/x.css", True),
        (b"frontend/dist/\n", "other/frontend/dist/x.css", False),
        (b"*.tsbuildinfo\n", "frontend/tsconfig.tsbuildinfo", True),
        (b"*.log\n!keep.log\n", "logs/keep.log", False),
        (b"*.log\n!keep.log\n", "logs/run.log", True),
        (b"out/\n!out/keep.txt\n", "out/keep.txt", True),
        (b"**/cache/**\n", "a/b/cache/c/d", True),
        (b"a/**/z.txt\n", "a/z.txt", True),
        (b"a/**/z.txt\n", "a/b/c/z.txt", True),
        (b"file?.txt\n", "file1.txt", True),
        (b"file[0-9].txt\n", "filex.txt", False),
        (b"# comment\n\n\\#hash\n", "#hash", True),
        (b"*.py\n", "src/a.py", True),
    ],
    ids=[
        "dir-rule-any-depth",
        "dir-rule-not-a-file",
        "anchored",
        "anchored-not-deeper",
        "slash-anchors",
        "slash-anchors-to-root",
        "glob-any-depth",
        "negation-reincludes",
        "negation-keeps-others",
        "no-reinclude-under-excluded-dir",
        "double-star-middle",
        "double-star-zero-dirs",
        "double-star-many-dirs",
        "question-mark",
        "class-no-match",
        "comment-and-escaped-hash",
        "plain-glob",
    ],
)
def test_one_gitignore_reads_as_git_reads_it(rules: bytes, path: str, ignored: bool) -> None:
    assert GitIgnore([(".gitignore", rules)]).ignores(path) is ignored


def test_a_nested_gitignore_applies_below_its_directory_and_wins_over_the_root() -> None:
    rules = GitIgnore([("pkg/.gitignore", b"!special.log\ngenerated/\n"), (".gitignore", b"*.log\n")])
    assert rules.ignores("pkg/generated/a.py")
    assert not rules.ignores("generated/a.py"), "a nested rule applies below its own directory only"
    assert not rules.ignores("pkg/special.log"), "the deeper file is read last, so its negation wins"
    assert rules.ignores("special.log")


def test_no_gitignore_ignores_nothing() -> None:
    assert not GitIgnore()
    assert not GitIgnore().ignores("frontend/dist/index.js")
    assert gitignore_paths(["b/c/.gitignore", "x.py", ".gitignore", "a/.gitignore"]) == [
        ".gitignore",
        "a/.gitignore",
        "b/c/.gitignore",
    ]


def test_a_build_leaves_the_checkout_clean_with_the_ignored_count() -> None:
    record = _record({".gitignore": _ROOT_RULES, "frontend/src/app.ts": b"x\n"})
    build = {f"frontend/dist/assets/chunk{i}.js": b"js" for i in range(1135)}
    build["frontend/tsconfig.tsbuildinfo"] = b"{}"
    entries = _manifest(record, build)
    ignore = GitIgnore([(".gitignore", _ROOT_RULES)])
    changes = changes_since(record, entries, ignore=ignore)
    assert changes.empty
    assert changes.ignored == 1136
    status = format_status(record, changes, shown_as="co")
    assert "is clean" in status
    assert "1136 ignored file(s) not listed" in status
    assert "dist" not in status


def test_a_tracked_file_under_an_ignored_directory_stays_a_change() -> None:
    record = _record({".gitignore": _ROOT_RULES, "frontend/dist/tracked.js": b"old\n"})
    entries = _manifest(record, {"frontend/dist/tracked.js": b"new\n", "frontend/dist/built.js": b"b"})
    changes = changes_since(record, entries, ignore=GitIgnore([(".gitignore", _ROOT_RULES)]))
    assert changes.modified == ("frontend/dist/tracked.js",)
    assert changes.added == ()
    assert changes.ignored == 1


def test_a_negated_file_and_a_new_source_file_are_still_added() -> None:
    record = _record({".gitignore": _ROOT_RULES})
    entries = _manifest(record, {"keep.log": b"k", "run.log": b"r", "src/new.py": b"n"})
    changes = changes_since(record, entries, ignore=GitIgnore([(".gitignore", _ROOT_RULES)]))
    assert changes.added == ("keep.log", "src/new.py")
    assert changes.ignored == 1


def test_without_rules_the_change_set_is_unchanged() -> None:
    record = _record({"src/a.py": b"a\n"})
    entries = _manifest(record, {"frontend/dist/x.js": b"x"})
    assert changes_since(record, entries).added == ("frontend/dist/x.js",)
    assert changes_since(record, entries, ignore=GitIgnore()).added == ("frontend/dist/x.js",)


def test_a_narrow_checkout_keeps_the_root_gitignore() -> None:
    sizes = {".gitignore": 10, "frontend/.gitignore": 10, "frontend/src/a.ts": 10, "docs/.gitignore": 10}
    kept = detect.companions(sizes, ["frontend/src"])
    assert ".gitignore" in kept and "frontend/.gitignore" in kept
    assert "docs/.gitignore" not in kept, "a sibling's rules do not apply to the checkout's sub_paths"
