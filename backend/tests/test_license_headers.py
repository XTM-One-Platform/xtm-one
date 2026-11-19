"""The Enterprise Edition list and the rules that tag a file with its license header (#5493).

``licensing/editions.yml`` lists the EE files; ``scripts/license_headers.py``
is the one module that reads it. The real tree is checked by the License edition
workflow (``--check``), in seconds and outside this suite; these tests pin the
rules on small trees: the classification, the header, ``copyright.md``, the
manifests, ``--which``, the pre-commit check and the files a pull request makes
CE. The few that read the real repository skip on a checkout that lacks it (an
agent sandbox narrowed to ``backend/``).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from scripts import license_headers as lh

NOTICE = (
    "Copyright (c) 2025-2026 Filigran SAS",
    "",
    'This file is part of the XTM One Enterprise Edition ("EE") and is',
    'licensed under the XTM One Enterprise Edition License (the "License");',
    "you may not use this file except in compliance with the License.",
)

LICENSE = "\n".join(
    [
        "Apache text",
        "-----------------------------------------------------------------",
        "",
        "XTM One Enterprise Edition License",
        "-----------------------------------------------------------------",
        *NOTICE,
        "-----------------------------------------------------------------",
        "",
    ]
)

EDITIONS = """
ee:
  - backend/app/agents/**
  - backend/app/tools/**
  - frontend/src/pages/Chat.tsx
except:
  - backend/app/tools/base.py
third_party:
  - backend/app/agents/vendor/*.json
no_header:
  - "**/*.test.*"
"""


#: The tests that read the real repository, not a scratch tree.
needs_repository = pytest.mark.skipif(
    not all((lh.REPO_ROOT / p).exists() for p in ("LICENSE", lh.EDITIONS_FILE, ".git", "frontend", "apps", "docs")),
    reason="needs the whole repository, not a narrowed checkout",
)


@needs_repository
def test_the_notice_is_read_from_the_license() -> None:
    notice = lh.notice(lh.REPO_ROOT)
    assert notice[0].startswith("Copyright (c) ") and notice[0].endswith(" Filigran SAS")
    assert any(lh.MARKER in line for line in notice)
    assert notice[-1].endswith("either express or implied.")


@pytest.fixture
def tree(tmp_path: Path):
    (tmp_path / "licensing").mkdir()
    (tmp_path / "licensing/editions.yml").write_text(EDITIONS)
    (tmp_path / "LICENSE").write_text(LICENSE)

    def write(files: dict[str, str]) -> tuple[str, ...]:
        for path, text in files.items():
            target = tmp_path / path
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("w", newline="") as handle:
                handle.write(text)
        return tuple(sorted(files))

    return tmp_path, write


def _read(path: Path) -> str:
    with path.open(newline="") as handle:
        return handle.read()


def _baseline(write) -> tuple[str, ...]:
    """One file per glob, so no glob is reported as matching nothing."""
    return write(
        {
            "backend/app/agents/__init__.py": "",
            "backend/app/tools/loop.py": "x = 1\n",
            "backend/app/tools/base.py": "x = 1\n",
            "frontend/src/pages/Chat.tsx": "export {};\n",
            "backend/app/agents/vendor/prices.json": "{}\n",
            "frontend/src/pages/Chat.test.tsx": "export {};\n",
        }
    )


def test_fix_tags_ee_files_only_and_check_then_passes(tree) -> None:
    root, write = tree
    files = _baseline(write) + write({"backend/app/core/config.py": "x = 1\n"})

    problems = lh.run(root, files, fix=True)

    assert "backend/app/tools/loop.py: missing or outdated EE header" in problems
    assert _read(root / "backend/app/tools/loop.py") == lh.header(NOTICE, ".py") + "\nx = 1\n"
    assert _read(root / "frontend/src/pages/Chat.tsx") == lh.header(NOTICE, ".tsx") + "\nexport {};\n"
    assert _read(root / "backend/app/tools/base.py") == "x = 1\n"
    assert _read(root / "backend/app/core/config.py") == "x = 1\n"
    assert lh.run(root, files, fix=False) == []


def test_python_header_is_a_comment_block_and_js_an_opencti_style_block() -> None:
    assert lh.header(NOTICE, ".py").splitlines()[:2] == ["# Copyright (c) 2025-2026 Filigran SAS", "#"]
    assert lh.header(NOTICE, ".ts").splitlines()[0] == "/*"
    assert lh.header(NOTICE, ".ts").splitlines()[-1] == "*/"


def test_check_reports_a_missing_header(tree) -> None:
    root, write = tree
    files = _baseline(write)
    lh.run(root, files, fix=True)
    files += write({"backend/app/agents/new.py": "x = 1\n"})

    assert lh.run(root, files, fix=False) == ["backend/app/agents/new.py: missing or outdated EE header"]


def test_check_reports_a_ce_file_with_the_header_and_fix_removes_it(tree) -> None:
    root, write = tree
    files = _baseline(write)
    lh.run(root, files, fix=True)
    files += write({"backend/app/core/config.py": lh.header(NOTICE, ".py") + "\nx = 1\n"})

    assert lh.run(root, files, fix=True) == ["backend/app/core/config.py: CE file carries the EE header"]
    assert _read(root / "backend/app/core/config.py") == "x = 1\n"


def test_a_header_that_no_longer_matches_the_license_is_replaced(tree) -> None:
    root, write = tree
    files = _baseline(write)
    lh.run(root, files, fix=True)
    old = lh.header(("Copyright (c) 2025 Filigran SAS", *NOTICE[1:]), ".py")
    write({"backend/app/tools/loop.py": old + "\nx = 1\n"})

    assert lh.run(root, files, fix=True) == ["backend/app/tools/loop.py: missing or outdated EE header"]
    assert _read(root / "backend/app/tools/loop.py") == lh.header(NOTICE, ".py") + "\nx = 1\n"


def test_a_glob_that_matches_nothing_is_reported(tree) -> None:
    root, write = tree
    files = write({"backend/app/agents/__init__.py": "", "backend/app/tools/loop.py": ""})

    problems = lh.run(root, files, fix=False)

    assert "licensing/editions.yml: `frontend/src/pages/Chat.tsx` matches no file (renamed or removed?)" in problems


def test_a_partial_checkout_judges_the_files_it_holds_only(tree) -> None:
    """A sandbox checkout of backend/app/tools holds no frontend/: no glob is stale, no other directory judged."""
    root, write = tree
    files = _baseline(write)
    lh.run(root, files, fix=True)
    (root / "backend/app/agents/copyright.md").unlink()
    held = tuple(p for p in files if p.startswith("backend/app/tools/"))

    assert lh.run(root, held, fix=False, scope=("backend/app/tools",)) == []
    # A planted EE header miss in what the checkout holds still fails.
    held += write({"backend/app/tools/new.py": "x = 1\n"})
    assert lh.run(root, held, fix=False, scope=("backend/app/tools",)) == [
        "backend/app/tools/new.py: missing or outdated EE header"
    ]
    # Judged whole, the same files report the stale globs.
    assert any("matches no file" in p for p in lh.run(root, held, fix=False))


def test_the_checkout_scope_is_read_from_the_sandbox_record(tmp_path: Path) -> None:
    assert lh.checkout_scope(tmp_path) == ()
    (tmp_path / lh.CHECKOUT_RECORD).write_text('{"sub_paths": ["backend/", "", 3, "LICENSE"]}')
    assert lh.checkout_scope(tmp_path) == ("backend", "LICENSE")
    (tmp_path / lh.CHECKOUT_RECORD).write_text('{"sub_paths": []}')
    assert lh.checkout_scope(tmp_path) == ()
    (tmp_path / lh.CHECKOUT_RECORD).write_text("not json")
    assert lh.checkout_scope(tmp_path) == ()


def test_tests_never_carry_the_header(tree) -> None:
    root, write = tree
    files = _baseline(write) + write({"backend/app/agents/loop.test.py": "x = 1\n"})

    lh.run(root, files, fix=True)

    assert _read(root / "backend/app/agents/loop.test.py") == "x = 1\n"


def test_copyright_md_marks_wholly_ee_directories_only(tree) -> None:
    root, write = tree
    files = _baseline(write)

    lh.run(root, files, fix=True)

    notice = _read(root / "backend/app/agents/copyright.md")
    assert "This directory is part of the XTM One Enterprise Edition" in notice
    assert "you may not use the files in this directory" in notice
    assert "this file" not in notice
    # tools/ holds a CE exception (base.py): no directory-wide notice.
    assert not (root / "backend/app/tools/copyright.md").exists()


def test_copyright_md_names_the_third_party_files_of_its_directory(tree) -> None:
    root, write = tree
    files = _baseline(write) + write({"backend/app/agents/vendor/notes.py": "x = 1\n"})

    lh.run(root, files, fix=True)

    notice = _read(root / "backend/app/agents/copyright.md")
    assert notice.endswith("remain under their own license or terms.\n\n- `vendor/*.json`\n")
    assert _read(root / "backend/app/agents/vendor/prices.json") == "{}\n"
    # A file next to them but not listed is EE as usual.
    assert _read(root / "backend/app/agents/vendor/notes.py").startswith(lh.header(NOTICE, ".py"))


def test_a_third_party_glob_that_matches_nothing_is_reported(tree) -> None:
    root, write = tree
    files = write(
        {
            "backend/app/agents/__init__.py": "",
            "backend/app/tools/loop.py": "",
            "frontend/src/pages/Chat.tsx": "",
        }
    )

    problems = lh.run(root, files, fix=False)

    assert (
        "licensing/editions.yml: `backend/app/agents/vendor/*.json` matches no file (renamed or removed?)" in problems
    )


def test_an_ee_package_manifest_declares_the_ee_license(tree) -> None:
    root, write = tree
    cargo = '[package]\nname = "relay"\nlicense = "Apache-2.0"\n'
    files = _baseline(write) + write(
        {
            "backend/app/agents/Cargo.toml": cargo,
            "backend/app/agents/Dockerfile": 'LABEL org.opencontainers.image.licenses="Apache-2.0"\n',
            "backend/app/agents/package.json": '{\n  "name": "addin",\n  "private": true\n}\n',
            "backend/app/core/Cargo.toml": cargo,
        }
    )

    problems = lh.run(root, files, fix=True)

    assert f"backend/app/agents/Cargo.toml: declares a license other than {lh.EE_LICENSE_ID}" in problems
    assert _read(root / "backend/app/agents/Cargo.toml") == cargo.replace("Apache-2.0", lh.EE_LICENSE_ID)
    assert lh.EE_LICENSE_ID in _read(root / "backend/app/agents/Dockerfile")
    # A manifest that declares no license, or a CE one, is left as it is.
    assert "license" not in _read(root / "backend/app/agents/package.json")
    assert _read(root / "backend/app/core/Cargo.toml") == cargo
    assert lh.run(root, files, fix=False) == []


@pytest.mark.parametrize(
    ("path", "original", "kept_first"),
    [
        ("backend/app/agents/crlf.py", '"""Doc."""\r\nfrom __future__ import annotations\r\n', None),
        ("backend/app/agents/bom.py", '\ufeff"""Doc."""\n', "\ufeff"),
        (
            "backend/app/agents/script.py",
            "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\nx = 1\n",
            "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n",
        ),
        ("backend/app/agents/proxy.js", "#!/usr/bin/env node\nconsole.log(1);\n", "#!/usr/bin/env node\n"),
    ],
)
def test_line_endings_bom_shebang_and_cookie_are_kept(tree, path: str, original: str, kept_first: str | None) -> None:
    root, write = tree
    files = _baseline(write) + write({path: original})

    lh.run(root, files, fix=True)
    tagged = _read(root / path)

    assert tagged.endswith(original.removeprefix(kept_first or ""))
    if kept_first:
        assert tagged.startswith(kept_first)
    if "\r\n" in original:
        assert "\n" not in tagged.replace("\r\n", "")
    # Once the file is CE again, removing the header gives it back byte for byte.
    write({"licensing/editions.yml": "ee:\n  - backend/app/agents/__init__.py\n"})
    lh.run(root, files, fix=True)
    assert _read(root / path) == original


def test_python_header_keeps_the_docstring_and_future_import(tree) -> None:
    root, write = tree
    source = '"""Module doc."""\n\nfrom __future__ import annotations\n\nx: int = 1\n'
    files = _baseline(write) + write({"backend/app/agents/mod.py": source})

    lh.run(root, files, fix=True)
    namespace: dict = {}
    code = compile(_read(root / "backend/app/agents/mod.py"), "mod.py", "exec")
    exec(code, namespace)  # noqa: S102 - a fixed literal module

    assert namespace["__doc__"] == "Module doc."


def test_fix_is_idempotent(tree) -> None:
    root, write = tree
    files = _baseline(write)
    lh.run(root, files, fix=True)
    snapshot = {path: _read(root / path) for path in files}

    assert lh.run(root, files, fix=True) == []
    assert {path: _read(root / path) for path in files} == snapshot


def test_a_dead_except_entry_is_reported(tree) -> None:
    root, write = tree
    files = write(
        {
            "backend/app/agents/__init__.py": "",
            "backend/app/tools/loop.py": "",
            "frontend/src/pages/Chat.tsx": "",
            "backend/app/agents/vendor/prices.json": "{}\n",
        }
    )

    problems = lh.run(root, files, fix=False)

    assert "licensing/editions.yml: `backend/app/tools/base.py` matches no file (renamed or removed?)" in problems


def test_removing_the_header_keeps_the_module_s_own_first_comment() -> None:
    own = "# Module notes, the module's own.\nx = 1\n"
    tagged = lh.fixed(own, ".py", ee=True, notice_lines=NOTICE)

    assert lh.fixed(tagged, ".py", ee=True, notice_lines=NOTICE) == tagged
    assert lh.fixed(tagged, ".py", ee=False, notice_lines=NOTICE) == own


def test_a_manifest_s_own_license_only_is_rewritten() -> None:
    cargo = '[package]\nname = "runner"\nlicense = "Apache-2.0"\n\n[package.metadata.wix]\nlicense = "License.rtf"\n'
    package = '{\n    "name": "addin",\n    "license": "MIT",\n    "config": {"license": "MIT"}\n}\n'

    assert lh.manifest_fixed("Cargo.toml", cargo) == cargo.replace("Apache-2.0", lh.EE_LICENSE_ID)
    assert lh.manifest_fixed("package.json", package) == package.replace('"MIT"', f'"{lh.EE_LICENSE_ID}"', 1)
    assert lh.manifest_fixed("package.json", '{\n  "config": {"license": "MIT"}\n}\n') == (
        '{\n  "config": {"license": "MIT"}\n}\n'
    )
    # A nested "license" that comes before the top-level one is left as it is.
    nested_first = '{\n  "name": "x",\n  "config": {\n    "license": "MIT"\n  },\n  "license": "MIT"\n}\n'
    assert lh.manifest_fixed("package.json", nested_first) == (
        f'{{\n  "name": "x",\n  "config": {{\n    "license": "MIT"\n  }},\n  "license": "{lh.EE_LICENSE_ID}"\n}}\n'
    )


def test_repository_files_lists_new_files_and_skips_deleted_ones(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)  # noqa: S603, S607 - git, as the script runs it, on a scratch repo

    git("init", "-q")
    (tmp_path / "backend/app").mkdir(parents=True)
    (tmp_path / "backend/app/kept.py").write_text("")
    (tmp_path / "backend/app/gone.py").write_text("")
    (tmp_path / ".gitignore").write_text("*.pyc\n")
    git("add", ".")
    (tmp_path / "backend/app/gone.py").unlink()
    (tmp_path / "backend/app/new.py").write_text("")
    (tmp_path / "backend/app/cache.pyc").write_text("")

    assert lh.repository_files(tmp_path) == ("backend/app/kept.py", "backend/app/new.py")


@needs_repository
def test_the_package_metadata_declares_what_each_package_holds() -> None:
    """The docs (CE only) declare Apache; the backend and frontend projects and the
    main images hold both editions, so they declare both, never Apache alone: that
    would read as Apache for the EE files they ship (#5493). Never "all rights
    reserved". The EE apps' manifests are the other test's business: they declare
    the EE license."""
    root = Path(__file__).resolve().parents[2]
    for path in ("docs/mkdocs.yml", "docs/mkdocs_serve.yml"):
        footer = next(
            line for line in (root / path).read_text(encoding="utf-8").splitlines() if line.startswith("copyright:")
        )
        assert "Apache License 2.0" in footer and "rights reserved" not in footer.lower(), path
    assert 'license = "Apache-2.0"' in (root / "docs/pyproject.toml").read_text(encoding="utf-8")
    assert f'license = "{lh.MIXED_LICENSE}"' in (root / "backend/pyproject.toml").read_text(encoding="utf-8")
    assert f'"license": "{lh.MIXED_LICENSE}"' in (root / "frontend/package.json").read_text(encoding="utf-8")
    for path in ("Dockerfile", "Dockerfile.worker"):
        label = f'org.opencontainers.image.licenses="{lh.MIXED_LICENSE}"'
        assert label in (root / path).read_text(encoding="utf-8"), path


def test_a_new_file_next_to_ee_files_must_be_classified(tree) -> None:
    root, write = tree
    files = _baseline(write) + write(
        {
            "frontend/src/pages/Settings.tsx": "export {};\n",
            "frontend/src/pages/Settings.test.tsx": "export {};\n",
            "backend/app/core/config.py": "x = 1\n",
        }
    )

    problems = lh.run(root, files, fix=True)

    # pages/ holds Chat.tsx (EE): Settings.tsx would be CE by default, a decision nobody made.
    # tools/base.py is CE by `except`, core/ holds no EE file, a test follows no edition.
    assert [p for p in problems if "not classified" in p] == [
        "frontend/src/pages/Settings.tsx: CE by default in a mixed directory, not classified. Agentic: add a glob "
        "to `ee` in licensing/editions.yml; otherwise start it with `// SPDX-License-Identifier: Apache-2.0` "
        "(`license_headers.py --mark-ce frontend/src/pages/Settings.tsx`)"
    ]
    # --fix cannot decide it: the file is left as it is.
    assert _read(root / "frontend/src/pages/Settings.tsx") == "export {};\n"

    # The decision is written in the file itself.
    write({"frontend/src/pages/Settings.tsx": "// SPDX-License-Identifier: Apache-2.0\n\nexport {};\n"})
    assert lh.run(root, files, fix=False) == []
    assert lh.edition_of("frontend/src/pages/Settings.tsx", lh.load_editions(root), marked=True)[0] == "CE"


MIXED = "mixed:\n  - backend/app\nce:\n  - backend/app/core\n"


def test_a_new_directory_must_be_classified(tree) -> None:
    root, write = tree
    (root / "licensing/editions.yml").write_text(EDITIONS + MIXED)
    files = _baseline(write) + write(
        {
            "backend/app/main.py": "# SPDX-License-Identifier: Apache-2.0\n\nx = 1\n",
            "backend/app/core/config.py": "x = 1\n",
            "backend/app/core/db/session.py": "x = 1\n",
            "backend/app/agents/sub/run.py": "x = 1\n",
            "backend/app/newthing/run.py": "x = 1\n",
            "backend/app/newthing/deep/more.py": "x = 1\n",
            "backend/app/newtests/x.test.py": "x = 1\n",
        }
    )

    problems = lh.run(root, files, fix=False)

    # `agents` is wholly EE and `core` wholly CE: their child directories follow them. A
    # directory of tests only follows no edition. Only the new directory is asked about, once.
    assert [p for p in problems if "not classified" in p] == [
        "backend/app/newthing: new directory, not classified. Agentic: add `backend/app/newthing/**` to `ee`; "
        "otherwise add `backend/app/newthing` to `ce`, or to `mixed` if it holds both, in licensing/editions.yml"
    ]
    # --fix cannot decide it: the file is left as it is.
    lh.run(root, files, fix=True)
    assert _read(root / "backend/app/newthing/run.py") == "x = 1\n"

    # Classified mixed, its own files and child directories are asked about in turn.
    (root / "licensing/editions.yml").write_text(
        EDITIONS
        + "mixed:\n  - backend/app\n  - backend/app/newthing\n"
        + "ce:\n  - backend/app/core\n  - backend/app/newthing/deep\n"
    )
    assert [p.split(":")[0] for p in lh.run(root, files, fix=False) if "not classified" in p] == [
        "backend/app/newthing/run.py"
    ]


def test_an_entry_that_names_no_directory_or_contradicts_another_is_reported(tree) -> None:
    root, write = tree
    (root / "licensing/editions.yml").write_text(
        EDITIONS
        + "mixed:\n  - backend/app\n  - backend/app/gone\n  - backend/app/agents\n"
        + "ce:\n  - backend/app/core\n  - backend/app/core/db\n  - backend/app/tools\n"
    )
    files = _baseline(write) + write(
        {"backend/app/core/config.py": "x = 1\n", "backend/app/core/db/session.py": "x = 1\n"}
    )
    lh.run(root, files, fix=True)

    problems = lh.run(root, files, fix=False)

    assert problems == [
        "licensing/editions.yml: `mixed` entry `backend/app/gone` is no directory (renamed or removed?)",
        "licensing/editions.yml: `ce` entry `backend/app/core/db` is not in a `mixed` directory: its parent decides",
        "licensing/editions.yml: `backend/app/agents` is classified twice (`ee`, `mixed`, `ce`)",
        "licensing/editions.yml: `backend/app/tools` is classified twice (`ee`, `mixed`, `ce`)",
        "licensing/editions.yml: `ce` directory `backend/app/tools` holds an EE file (backend/app/tools/loop.py): "
        "it is `mixed`",
    ]


def test_the_guard_is_off_when_no_directory_is_mixed(tree) -> None:
    root, write = tree
    files = _baseline(write) + write({"backend/app/newthing/run.py": "x = 1\n"})
    lh.run(root, files, fix=True)

    assert lh.run(root, files, fix=False) == []


def test_edition_of_tells_the_edition_of_a_file_not_written_yet() -> None:
    editions = lh.Editions(
        ee=("backend/app/agents/**", "backend/app/tools/**"),
        except_=("backend/app/tools/base.py",),
        no_header=("**/tests/**",),
        third_party=("backend/app/agents/vendor/*.json",),
    )

    def edition(path: str) -> str:
        return lh.edition_of(path, editions)[0]

    assert edition("backend/app/agents/new_agent.py") == "EE"
    assert edition("backend/app/tools/base.py") == "CE"
    assert edition("backend/app/models/user.py") == "CE (default)"
    assert edition("backend/app/agents/vendor/prices.json") == "third-party"
    assert edition("backend/app/agents/tests/test_x.py") == "unmarked"
    assert edition("docs/docs/index.md") == "outside"
    assert "backend/app/agents/**" in lh.edition_of("backend/app/agents/a.py", editions)[1]


def test_edition_of_tells_what_is_still_to_classify() -> None:
    editions = lh.Editions(
        ee=("backend/app/agents/**", "backend/app/services/agent_*.py"),
        except_=(),
        no_header=(),
        mixed=("backend/app", "backend/app/services"),
        ce=("backend/app/core",),
    )

    def edition(path: str) -> tuple[str, str]:
        return lh.edition_of(path, editions)

    assert edition("backend/app/services/agent_new.py")[0] == "EE"
    assert edition("backend/app/agents/sub/new.py")[0] == "EE"
    assert edition("backend/app/core/db/new.py") == (
        "CE",
        "under `backend/app/core`, a CE directory (`ce` in licensing/editions.yml)",
    )
    assert lh.edition_of("backend/app/services/vault.py", editions, marked=True)[0] == "CE"
    assert edition("backend/app/services/new_thing.py") == (
        "to classify",
        "`backend/app/services` is a mixed directory: each source file there is classified",
    )
    assert edition("backend/app/services/newpkg/deep/run.py") == (
        "to classify",
        "`backend/app/services/newpkg` is a new directory in the mixed directory `backend/app/services`",
    )
    # A file of a mixed directory that nothing marks (not a source file) stays CE by default.
    assert edition("backend/app/services/data.json")[0] == "CE (default)"


def test_which_warns_about_a_file_still_to_classify() -> None:
    editions = lh.Editions(ee=("backend/app/services/agent_*.py",), except_=(), no_header=())
    files = ("backend/app/services/agent_run.py", "backend/app/services/vault.py")

    # services/ holds EE files: it is mixed even before the list says so.
    mixed = lh.edition_report("backend/app/services/new_thing.py", editions, files)
    assert mixed[0].startswith("backend/app/services/new_thing.py: to classify")
    assert any("WARNING" in line and "cannot be taken back" in line for line in mixed)

    alone = lh.edition_report("backend/app/models/new.py", editions, files)
    assert alone[0].startswith("backend/app/models/new.py: CE (default)")
    assert not any("WARNING" in line for line in alone)

    # An EE file and a non-source file raise no question.
    assert len(lh.edition_report("backend/app/services/agent_new.py", editions, files)) == 1
    assert len(lh.edition_report("backend/app/services/data.json", editions, files)) == 1


def test_an_ee_glob_cannot_mark_a_non_source_file_outside_a_wholly_ee_directory() -> None:
    editions = lh.Editions(ee=("backend/app/tools/**",), except_=("backend/app/tools/base.py",), no_header=())

    assert lh.edition_of("backend/app/tools/AGENTS.md", editions)[0] == "EE (unmarked)"
    assert lh.edition_of("backend/app/tools/loop.py", editions)[0] == "EE"
    wholly = lh.Editions(ee=("backend/app/agents/**",), except_=(), no_header=())
    assert lh.edition_of("backend/app/agents/AGENTS.md", wholly)[0] == "EE"  # its copyright.md marks it


def test_which_reads_relative_paths_from_the_given_directory(tree) -> None:
    root, write = tree
    _baseline(write)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)  # noqa: S607 - git, as the script runs it, on a scratch repo

    from_backend = lh.which(["app/agents/new agent.py"], root / "backend", root)
    assert from_backend[0].startswith("backend/app/agents/new agent.py: EE")
    outside = root.parent / "elsewhere.py"
    assert lh.which([str(outside)], root, root) == [f"{outside}: outside the repository"]


@pytest.mark.parametrize(
    "glob",
    [
        "backend/app/agents/**",
        "backend/app/services/agent_*.py",
        "backend/app/services/*trigger_provisioning.py",
        "frontend/src/pages/Agents.*",
        "frontend/src/pages/CustomTool*",
        "**/tests/**",
        "**/*.test.*",
        "backend/app/agents/seed_data/skill_resources/cti-*/**",
        "frontend/src/components/ui/*family-badge.tsx",
    ],
)
def test_a_glob_is_compiled_once_and_matches_as_full_match(glob: str) -> None:
    """The list holds more globs than ``full_match`` keeps compiled (512), so the
    script compiles each one once; it must answer exactly as ``full_match``."""
    paths = (
        "backend/app/agents/__init__.py",
        "backend/app/agents/seed/x/y.py",
        "backend/app/agents",
        "backend/app/services/agent_run.py",
        "backend/app/services/agent/run.py",
        "backend/app/services/opencti_trigger_provisioning.py",
        "frontend/src/pages/Agents.tsx",
        "frontend/src/pages/Agents.commands.ts",
        "frontend/src/pages/CustomToolDetail.tsx",
        "backend/tests/test_x.py",
        "frontend/src/lib/a.test.ts",
        "frontend/src/.hidden/x.test.ts",
        "backend/app/agents/seed_data/skill_resources/cti-hunt/a/b.md",
        "frontend/src/components/ui/needs-family-badge.tsx",
        "frontend/src/components/ui/family-badge.tsx",
    )
    for path in paths:
        assert lh._full_match(path, glob) == PurePosixPath(path).full_match(glob), (path, glob)


def test_edition_of_a_directory_follows_its_entry_or_the_nearest_one_above() -> None:
    editions = lh.Editions(
        ee=("backend/app/agents/**",),
        except_=(),
        no_header=(),
        mixed=("backend/app", "backend/app/services"),
        ce=("backend/app/core",),
    )

    def edition(path: str) -> str:
        return lh.edition_of(path, editions, directory=True)[0]

    assert edition("backend/app/agents") == "EE"
    assert edition("backend/app/agents/sub") == "EE"
    assert edition("backend/app/core/db") == "CE"
    assert edition("backend/app/services") == "mixed"
    # A new directory in a mixed one is a decision, at any depth below it.
    assert edition("backend/app/newpkg") == "to classify"
    assert lh.edition_of("backend/app/services/newpkg/deep", editions, directory=True)[1] == (
        "`backend/app/services/newpkg` is a new directory in the mixed directory `backend/app/services`"
    )


@needs_repository
def test_which_tells_a_directory_from_a_file_and_fails_on_one_to_classify() -> None:
    root = lh.REPO_ROOT
    new_directory = lh.which(["backend/app/zz_not_a_package"], root, root)
    assert new_directory[0].startswith("backend/app/zz_not_a_package: to classify")
    assert lh.which(["backend/app/agents/"], root, root)[0].startswith("backend/app/agents: EE")
    assert lh.main(["--from", str(root), "--which", "backend/app/zz_not_a_package"]) == 1
    assert lh.main(["--from", str(root), "--which", "backend/app/agents/zz_new_agent.py"]) == 0


@pytest.mark.parametrize(
    ("path", "original", "kept_first", "comment"),
    [
        ("backend/app/agents/lib.rs", "//! Crate docs.\nfn main() {}\n", None, "/*"),
        ("backend/app/agents/style.css", "/* Fonts */\nbody { margin: 0; }\n", None, "/*"),
        ("backend/app/agents/install.sh", "#!/usr/bin/env bash\nset -eu\n", "#!/usr/bin/env bash\n", "# "),
        (
            "backend/app/agents/install.ps1",
            "#Requires -Version 5.1\r\n<#\r\n.SYNOPSIS\r\n#>\r\nparam()\r\n",
            "#Requires -Version 5.1\r\n",
            "# ",
        ),
    ],
)
def test_rust_css_shell_and_powershell_files_carry_the_header(
    tree, path: str, original: str, kept_first: str | None, comment: str
) -> None:
    """LICENSE reads a file without a header as CE, so every source file of an EE
    app takes one, not only the Python and TypeScript ones."""
    root, write = tree
    files = _baseline(write) + write({path: original})

    lh.run(root, files, fix=True)
    tagged = _read(root / path)

    assert tagged.startswith((kept_first or "") + comment)
    assert lh.MARKER in tagged and tagged.endswith(original.removeprefix(kept_first or ""))
    assert lh.run(root, files, fix=False) == []
    write({"licensing/editions.yml": "ee:\n  - backend/app/agents/__init__.py\n"})
    lh.run(root, files, fix=True)
    assert _read(root / path) == original


def test_a_file_that_cannot_be_read_is_reported_not_raised(tree) -> None:
    root, write = tree
    files = _baseline(write) + write({"backend/app/agents/package.json": '{"name": "x", "license": '})
    (root / "backend/app/agents/latin1.py").write_bytes("x = 'caf\xe9'\n".encode("latin-1"))
    files += ("backend/app/agents/latin1.py",)

    problems = lh.run(root, files, fix=False)

    assert "backend/app/agents/package.json: not valid UTF-8 JSON, so its license cannot be checked" in problems
    assert any(p.startswith("backend/app/agents/latin1.py: not UTF-8 text") for p in problems)


def test_a_directory_with_an_except_is_ee_but_not_wholly() -> None:
    editions = lh.Editions(ee=("backend/app/tools/**",), except_=("backend/app/tools/base.py",), no_header=())

    edition, reason = lh.edition_of("backend/app/tools", editions, directory=True)

    assert edition == "EE" and "except `backend/app/tools/base.py`" in reason


@needs_repository
def test_which_reads_a_dockerfile_as_a_file_and_a_one_line_package_json_is_rewritten() -> None:
    root = lh.REPO_ROOT
    assert lh.which(["backend/app/services/Dockerfile"], root, root)[0].startswith(
        "backend/app/services/Dockerfile: CE (default)"
    )
    one_line = '{"name":"x","config":{"license":"MIT"},"license":"MIT"}'
    assert lh.manifest_fixed("package.json", one_line) == (
        '{"name":"x","config":{"license":"MIT"},"license":"' + lh.EE_LICENSE_ID + '"}'
    )


@pytest.mark.parametrize(
    ("suffix", "original", "expected"),
    [
        (".py", '"""Doc."""\n', '# SPDX-License-Identifier: Apache-2.0\n\n"""Doc."""\n'),
        (".tsx", "export {};\n", "// SPDX-License-Identifier: Apache-2.0\n\nexport {};\n"),
        (".css", "body {}\n", "/* SPDX-License-Identifier: Apache-2.0 */\n\nbody {}\n"),
        (".sh", "#!/bin/sh\nset -eu\n", "#!/bin/sh\n# SPDX-License-Identifier: Apache-2.0\n\nset -eu\n"),
        (
            ".ps1",
            "#Requires -Version 5.1\r\nparam()\r\n",
            "#Requires -Version 5.1\r\n# SPDX-License-Identifier: Apache-2.0\r\n\r\nparam()\r\n",
        ),
    ],
)
def test_the_ce_marker_goes_first_after_the_prelude_once(suffix: str, original: str, expected: str) -> None:
    assert lh.ce_marked(original, suffix) == expected
    assert lh.ce_marked(expected, suffix) == expected
    assert lh.has_ce_marker(expected, suffix) and not lh.has_ce_marker(original, suffix)


def test_mark_ce_writes_the_decision_and_refuses_an_ee_file(tree) -> None:
    root, write = tree
    files = _baseline(write) + write({"frontend/src/pages/Settings.tsx": "export {};\n"})

    write({"README.md": "x\n"})
    lines = lh.mark_ce(
        ["frontend/src/pages/Settings.tsx", "backend/app/tools/loop.py", "README.md", "backend/app/core/later.py"],
        root,
        root,
    )

    assert lines[0] == "frontend/src/pages/Settings.tsx: CE marker added"
    assert lines[1] == "backend/app/tools/loop.py: refused, an `ee` glob covers it: it is EE"
    assert lines[2].startswith("README.md: refused, not a source file")
    assert lines[3].startswith("backend/app/core/later.py: refused, no such file: write it first")
    assert not [p for p in lh.run(root, files, fix=False) if "Settings.tsx" in p]


def test_an_ee_file_that_carries_the_ce_marker_is_a_decision_to_make(tree) -> None:
    root, write = tree
    files = _baseline(write) + write(
        {"backend/app/tools/marked.py": "# SPDX-License-Identifier: Apache-2.0\n\nx = 1\n"}
    )

    problems = lh.run(root, files, fix=True)

    assert any(
        p.startswith("backend/app/tools/marked.py: carries the CE marker but an `ee` glob covers it") for p in problems
    )
    # --fix adds the header but leaves the decision: the marker stays and is still reported.
    assert any("carries the CE marker" in p for p in lh.run(root, files, fix=False))


def test_the_ce_marker_is_the_exact_line_not_an_expression_that_contains_it() -> None:
    mixed = "// SPDX-License-Identifier: Apache-2.0 AND LicenseRef-XTM-One-Enterprise-Edition\nexport {};\n"

    assert not lh.has_ce_marker(mixed, ".ts")
    assert lh.has_ce_marker("// SPDX-License-Identifier: Apache-2.0\nexport {};\n", ".ts")


def test_a_marked_file_in_a_new_directory_is_still_to_classify() -> None:
    editions = lh.Editions(ee=(), except_=(), no_header=(), mixed=("backend/app/services",))

    assert lh.edition_of("backend/app/services/newpkg/x.py", editions, marked=True)[0] == "to classify"
    assert lh.edition_of("backend/app/services/x.py", editions, marked=True)[0] == "CE"


def test_mark_ce_refuses_third_party_and_test_files(tree) -> None:
    root, write = tree
    _baseline(write)
    write({"licensing/editions.yml": EDITIONS.replace("vendor/*.json", "vendor/*.py")})
    write({"backend/app/agents/vendor/lib.py": "x = 1\n", "backend/app/core/x.test.py": "x = 1\n"})

    lines = lh.mark_ce(["backend/app/agents/vendor/lib.py", "backend/app/core/x.test.py"], root, root)

    assert lines == [
        "backend/app/agents/vendor/lib.py: refused, listed under `third_party`: it keeps its owner's license",
        "backend/app/core/x.test.py: refused, matched by `no_header`: tests follow no edition",
    ]


GATE_EDITIONS = """
ee:
  - backend/app/agents/**
  - backend/app/services/agent_*.py
mixed:
  - backend/app
  - frontend/src
  - apps
  - backend/app/services
ce:
  - backend/app/core
"""


@pytest.fixture
def repo(tmp_path: Path):
    """A scratch repository with a base commit; returns ``(root, write, commit)``."""

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True).stdout  # noqa: S603, S607 - git, as the script runs it, on a scratch repo

    def write(files: dict[str, str | None]) -> None:
        for path, text in files.items():
            target = tmp_path / path
            if text is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

    def commit(message: str) -> str:
        git("add", "-A")
        git(
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            message,
        )
        return git("rev-parse", "HEAD").strip()

    git("init", "-q")
    write(
        {
            "licensing/editions.yml": GATE_EDITIONS,
            "backend/app/agents/loop.py": "x = 1\n",
            "backend/app/core/config.py": "x = 1\n",
            "backend/app/services/agent_runner.py": "x = 1\n",
            "backend/app/services/users.py": MARKED + "x = 1\n",
        }
    )
    return tmp_path, write, commit


MARKED = "# SPDX-License-Identifier: Apache-2.0\n\n"


def test_becoming_ce_lists_a_copy_but_not_a_rename_nor_a_file_of_a_ce_directory(repo) -> None:
    root, write, commit = repo
    base = commit("base")
    lh._git(root, "mv", "backend/app/services/users.py", "backend/app/services/accounts.py")
    write(
        {
            "backend/app/services/copy.py": MARKED + "x = 1\ny = 3\n",  # copied, then edited
            "backend/app/core/settings.py": "x = 2\n",  # `core` is CE: decided
            "backend/app/services/agent_memory.py": "x = 2\n",  # EE by its glob
        }
    )
    commit("pull request")

    # The copy carries a decision nobody made; the renamed file keeps its own.
    assert lh.becoming_ce(root, base) == ["backend/app/services/copy.py"]


@pytest.mark.parametrize(
    ("change", "listed"),
    [
        # A new `ce` directory: its files need no marker, so only the list shows the decision.
        (
            {
                "licensing/editions.yml": GATE_EDITIONS + "  - backend/app/newpkg\n",
                "backend/app/newpkg/agent_loop.py": "x = 1\n",
            },
            ["backend/app/newpkg/agent_loop.py"],
        ),
        # A new `except` entry, for a new file or an existing one.
        (
            {
                "licensing/editions.yml": GATE_EDITIONS + "except:\n  - backend/app/agents/loop.py\n",
            },
            ["backend/app/agents/loop.py"],
        ),
        # A narrowed `ee` glob, the file it no longer matches then marked CE.
        (
            {
                "licensing/editions.yml": GATE_EDITIONS.replace("agent_*.py", "agent_memory.py"),
                "backend/app/services/agent_runner.py": MARKED + "x = 1\n",
            },
            ["backend/app/services/agent_runner.py"],
        ),
        # A `no_header` entry that now takes an EE file: no header, so it reads as CE.
        (
            {"licensing/editions.yml": GATE_EDITIONS + "no_header:\n  - backend/app/agents/*.py\n"},
            ["backend/app/agents/loop.py"],
        ),
        # Nothing becomes CE: an edit of a CE file, a new EE file.
        (
            {"backend/app/services/users.py": MARKED + "x = 2\n", "backend/app/agents/new.py": "x = 1\n"},
            [],
        ),
    ],
)
def test_becoming_ce_lists_what_the_list_makes_ce_too(repo, change: dict[str, str], listed: list[str]) -> None:
    root, write, commit = repo
    base = commit("base")
    write(change)
    commit("pull request")

    assert lh.becoming_ce(root, base) == listed


def test_a_header_whose_end_cannot_be_told_is_reported_not_stripped(tree) -> None:
    """A header from an older notice whose last line changed: a module comment glued
    to it must not be taken for part of it."""
    root, write = tree
    files = _baseline(write)
    lh.run(root, files, fix=True)
    old = lh.header((*NOTICE[:-1], "you may not use this file at all."), ".py")
    reworded = old + "# The module's own note.\nx = 1\n"
    write({"backend/app/tools/loop.py": reworded})

    problems = lh.run(root, files, fix=True)

    assert problems == [f"backend/app/tools/loop.py: {lh.HEADER_END_UNKNOWN}"]
    assert _read(root / "backend/app/tools/loop.py") == reworded
    # Same length as the current header, a blank line after it: replaced.
    write({"backend/app/tools/loop.py": old + "\n# The module's own note.\nx = 1\n"})
    assert lh.run(root, files, fix=True) == ["backend/app/tools/loop.py: missing or outdated EE header"]
    assert _read(root / "backend/app/tools/loop.py") == lh.header(NOTICE, ".py") + "\n# The module's own note.\nx = 1\n"


def test_a_stale_no_header_glob_is_reported(tree) -> None:
    root, write = tree
    files = tuple(p for p in _baseline(write) if not p.endswith(".test.tsx"))

    assert "licensing/editions.yml: `**/*.test.*` matches no file (renamed or removed?)" in lh.run(
        root, files, fix=False
    )


@pytest.fixture
def staged_repo(tree):
    """A git repository holding the baseline, every file tagged and committed."""
    root, write = tree
    _baseline(write)
    lh.run(root, tuple(sorted(_baseline(write))), fix=True)

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout  # noqa: S603, S607 - git, as the script runs it, on a scratch repo

    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", "commit", "-qm", "base")
    return root, write, git


def test_staged_passes_a_commit_whose_files_are_tagged(staged_repo) -> None:
    root, write, git = staged_repo
    write(
        {"backend/app/agents/new.py": lh.header(NOTICE, ".py") + "\nx = 1\n", "backend/app/core/config.py": "x = 1\n"}
    )
    git("add", ".")

    assert lh.check_staged(root) == []


def test_staged_reads_the_index_not_the_working_tree(staged_repo) -> None:
    root, write, git = staged_repo
    write({"backend/app/agents/new.py": "x = 1\n"})
    git("add", "backend/app/agents/new.py")
    lh.run(root, lh.repository_files(root), fix=True)  # tagged on disk, never staged

    assert lh.check_staged(root) == ["backend/app/agents/new.py: missing or outdated EE header"]
    assert lh.MARKER not in git("show", ":backend/app/agents/new.py")  # the hook wrote nothing


def test_staged_stops_an_ee_file_moved_out_of_ee_with_its_header(staged_repo) -> None:
    root, _, git = staged_repo
    (root / "backend/app/core").mkdir(parents=True)
    git("mv", "backend/app/tools/loop.py", "backend/app/core/loop.py")

    problems = lh.check_staged(root)

    assert any(p.startswith("backend/app/core/loop.py: CE file carries the EE header") for p in problems)
    # The advice is to classify the new path, never `make license`, which would strip the header.
    steps = lh._staged_steps(problems)
    assert "classify" in steps and "make license" not in steps


def test_staged_checks_the_copyright_md_of_the_directories_it_touches(staged_repo) -> None:
    root, write, git = staged_repo
    git("rm", "-q", "--cached", "backend/app/agents/copyright.md")
    write({"backend/app/agents/new.py": lh.header(NOTICE, ".py") + "\nx = 1\n"})
    git("add", "backend/app/agents/new.py")

    assert lh.check_staged(root) == ["backend/app/agents/copyright.md: missing or outdated"]


def test_staged_reports_a_file_that_is_not_utf8_without_crashing(staged_repo) -> None:
    root, _, git = staged_repo
    (root / "backend/app/tools/latin.py").write_bytes("x = '\xe9'\n".encode("latin-1"))
    git("add", "backend/app/tools/latin.py")

    assert lh.check_staged(root) == [
        "backend/app/tools/latin.py: not UTF-8 text, so its license header cannot be checked: save it as UTF-8"
    ]


def test_staged_ignores_untracked_files_and_list_problems_the_commit_does_not_touch(staged_repo) -> None:
    root, write, git = staged_repo
    (root / "licensing/editions.yml").write_text(EDITIONS + "mixed:\n  - backend/app\nce:\n  - backend/app/core\n")
    write({"backend/app/core/config.py": "x = 1\n"})
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", "commit", "-qm", "list")
    write({"backend/app/wip/draft.py": "x = 1\n"})  # untracked: nobody's decision yet
    write({"backend/app/core/config.py": "x = 2\n"})
    git("add", "backend/app/core/config.py")

    assert lh.check_staged(root) == []
    git("add", "backend/app/wip/draft.py")  # staged: now the commit adds a directory nobody classified
    assert any(p.startswith("backend/app/wip: new directory, not classified") for p in lh.check_staged(root))


def test_staged_reads_the_edition_list_and_license_from_the_index(staged_repo) -> None:
    root, write, git = staged_repo
    write({"backend/app/core/config.py": "x = 1\n"})
    git("add", "backend/app/core/config.py")
    # An unstaged glob that would make the file EE does not count: the commit holds the list as staged.
    editions = (root / "licensing/editions.yml").read_text()
    write({"licensing/editions.yml": editions.replace("ee:\n", "ee:\n  - backend/app/core/**\n")})

    assert lh.check_staged(root) == []
    git("add", "licensing/editions.yml")
    assert "backend/app/core/config.py: missing or outdated EE header" in lh.check_staged(root)


def test_staged_checks_every_ee_file_when_the_commit_changes_the_license(staged_repo) -> None:
    root, write, git = staged_repo
    write({"LICENSE": LICENSE.replace("2025-2026", "2025-2027")})
    git("add", "LICENSE")

    problems = lh.check_staged(root)

    assert "backend/app/tools/loop.py: missing or outdated EE header" in problems
    assert "backend/app/agents/copyright.md: missing or outdated" in problems


def test_staged_reports_a_copyright_md_outside_an_ee_directory_and_an_invalid_manifest(staged_repo) -> None:
    root, write, git = staged_repo
    write({"backend/app/core/copyright.md": "x\n", "backend/app/agents/package.json": '{"license": '})
    git("add", ".")

    problems = lh.check_staged(root)

    assert "backend/app/core/copyright.md: not an EE directory" in problems
    assert "backend/app/agents/package.json: not valid UTF-8 JSON, so its license cannot be checked" in problems
    assert "not valid JSON" in lh._staged_steps(problems)


@needs_repository
def test_the_hook_runs_on_what_the_check_reads_with_the_locked_pyyaml() -> None:
    """The hook's quick filter skips commits that touch none of SCOPE, licensing/
    or LICENSE: it must not drift from SCOPE. Its PyYAML is the backend's locked one."""
    root = lh.REPO_ROOT
    hook = (root / ".githooks/pre-commit").read_text(encoding="utf-8")
    pattern = re.search(r"grep -qE '([^']+)'", hook)
    assert pattern
    for path in (*(f"{tree}/x.py" for tree in lh.SCOPE), "licensing/editions.yml", "LICENSE"):
        assert re.search(pattern.group(1), path), path
    assert not re.search(pattern.group(1), "docs/index.md")
    lock = (root / "backend/uv.lock").read_text(encoding="utf-8")
    locked = re.search(r'name = "pyyaml"\nversion = "([^"]+)"', lock)
    assert locked and f'"pyyaml=={locked.group(1)}"' in hook


def test_staged_asks_for_the_ce_marker_on_a_new_file_of_a_mixed_directory(staged_repo) -> None:
    root, write, git = staged_repo
    (root / "licensing/editions.yml").write_text(EDITIONS + "mixed:\n  - backend/app\n")
    write({"backend/app/newmod.py": "x = 1\n"})
    git("add", ".")

    problems = lh.check_staged(root)

    assert any(p.startswith("backend/app/newmod.py: CE by default in a mixed directory") for p in problems)
    assert "make mark-ce" in lh._staged_steps(problems) and "make license" not in lh._staged_steps(problems)
    assert lh.mark_ce(["backend/app/newmod.py"], root, root) == ["backend/app/newmod.py: CE marker added"]
    git("add", "backend/app/newmod.py")
    assert not [p for p in lh.check_staged(root) if "newmod" in p]


def test_staged_checks_every_file_when_the_commit_changes_the_edition_list(staged_repo) -> None:
    """Moving a directory into EE changes no file of it, yet each one now needs the header."""
    root, write, git = staged_repo
    write({"backend/app/core/config.py": "x = 1\n"})
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", "commit", "-qm", "core")
    editions = (root / "licensing/editions.yml").read_text()
    write({"licensing/editions.yml": editions.replace("ee:\n", "ee:\n  - backend/app/core/**\n")})
    git("add", "licensing/editions.yml")

    problems = lh.check_staged(root)

    assert "backend/app/core/config.py: missing or outdated EE header" in problems
    assert "backend/app/core/copyright.md: missing or outdated" in problems


def test_staged_advice_for_a_stale_entry_is_to_fix_the_entry(staged_repo) -> None:
    root, write, git = staged_repo
    editions = (root / "licensing/editions.yml").read_text()
    write({"licensing/editions.yml": editions.replace("ee:\n", "ee:\n  - backend/app/gone/**\n")})
    git("add", "licensing/editions.yml")

    problems = lh.check_staged(root)
    steps = lh._staged_steps(problems)

    assert "licensing/editions.yml: `backend/app/gone/**` matches no file (renamed or removed?)" in problems
    assert "fix the entries of licensing/editions.yml" in steps
    assert "classify what is not classified" not in steps and "make license" not in steps


@needs_repository
@pytest.mark.parametrize(("uv_status", "hook_status", "skipped"), [(0, 0, False), (1, 1, False), (2, 0, True)])
def test_the_hook_starts_uv_once_and_skips_when_it_cannot_start(
    tmp_path: Path, uv_status: int, hook_status: int, skipped: bool
) -> None:
    """0 and 1 are the check's answer; any other status is uv failing to set the environment up."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    (bin_dir / "uv").write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\nexit {uv_status}\n')
    (bin_dir / "uv").chmod(0o755)
    (bin_dir / "git").write_text("#!/bin/sh\necho backend/app/x.py\n")
    (bin_dir / "git").chmod(0o755)

    result = subprocess.run(  # noqa: S603 - the repository's own hook
        ["/bin/sh", str(lh.REPO_ROOT / ".githooks/pre-commit")],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == hook_status
    assert ("could not start" in result.stderr) is skipped
    assert len(calls.read_text().splitlines()) == 1


def test_staged_stops_a_commit_that_only_removes_a_copyright_md(staged_repo) -> None:
    root, _, git = staged_repo
    git("rm", "-q", "backend/app/agents/copyright.md")

    assert lh.check_staged(root) == ["backend/app/agents/copyright.md: missing or outdated"]


def test_staged_reports_every_list_problem_when_the_commit_changes_the_list(staged_repo) -> None:
    """Moving a CE directory to `mixed` leaves its files unclassified though none is staged."""
    root, write, git = staged_repo
    write({"backend/app/core/config.py": "x = 1\n"})
    (root / "licensing/editions.yml").write_text(EDITIONS + "mixed:\n  - backend/app\nce:\n  - backend/app/core\n")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", "commit", "-qm", "ce")
    (root / "licensing/editions.yml").write_text(EDITIONS + "mixed:\n  - backend/app\n  - backend/app/core\n")
    git("add", "licensing/editions.yml")

    problems = lh.check_staged(root)

    assert any(p.startswith("backend/app/core/config.py: CE by default in a mixed directory") for p in problems)


def test_staged_reads_regular_files_only_and_survives_a_symlink_and_a_gitlink(staged_repo) -> None:
    root, _, git = staged_repo
    (root / "backend/app/agents/linked.py").symlink_to("../tools/loop.py")
    sub = git("rev-parse", "HEAD").strip()
    git("update-index", "--add", "--cacheinfo", f"160000,{sub},backend/app/agents/vendored")
    git("add", "backend/app/agents/linked.py")

    assert lh.check_staged(root) == []


def test_staged_tells_a_ce_decision_from_a_move(staged_repo) -> None:
    """Narrowing an `ee` glob is a decision: `make license` strips the header, nothing moved."""
    root, write, git = staged_repo
    editions = (root / "licensing/editions.yml").read_text()
    write({"licensing/editions.yml": editions.replace("backend/app/tools/**", "backend/app/tools/base.py")})
    git("add", "licensing/editions.yml")

    problems = lh.check_staged(root)

    assert "backend/app/tools/loop.py: CE file carries the EE header" in problems
    assert not any(p.endswith(lh.MOVED_OUT_OF_EE) for p in problems)
    assert "make license" in lh._staged_steps(problems)


def test_staged_explains_a_list_it_cannot_read_instead_of_a_traceback(staged_repo, monkeypatch, capsys) -> None:
    root, write, git = staged_repo
    write({"licensing/editions.yml": "ee: [unclosed\n"})
    git("add", "licensing/editions.yml")
    monkeypatch.setattr(lh, "REPO_ROOT", root)

    assert lh.main(["--staged"]) == 1
    out = capsys.readouterr().out
    assert "could not read what the commit holds" in out and "Traceback" not in out


def test_the_icon_generator_imports_as_a_module_and_as_a_file() -> None:
    """The generator check runs it as ``scripts.refresh_lucide_icons``; a person, as a file."""
    import importlib
    import sys

    module = importlib.import_module("scripts.refresh_lucide_icons")
    assert module.tagged is lh.tagged
    backend = Path(lh.__file__).resolve().parents[1]
    as_file = subprocess.run(
        [sys.executable, "scripts/refresh_lucide_icons.py", "--help"],
        cwd=backend,
        capture_output=True,
        text=True,
        check=False,
    )
    assert as_file.returncode == 0, as_file.stderr
