"""An edit tool warns when the file it wrote lies outside the checkout record and sub_paths.

Such a file is never a change of the checkout: sandbox_checkout_status says clean and
create_github_commit(from_checkout) leaves it out, so the edit went unseen until the commit.
"""

from __future__ import annotations

import uuid

from app.services.sandbox_workspace.checkout import RECORD_NAME, build_record, outside_record_note
from tests.test_sandbox_workspace_tools import _call, _composite, _put, store  # noqa: F401


def _record(path: str = "repo", sub_paths: tuple[str, ...] = ("docs",)):
    return build_record(
        provider="github",
        repository="acme/repo",
        ref="main",
        commit="0a1b2c3",
        path=path,
        sub_paths=sub_paths,
        files=[("docs/a.md", b"a\n")],
    )


def test_note_names_only_the_files_outside_the_record():
    record = _record()
    assert outside_record_note(record, ["repo/docs/a.md", "repo/docs/new.md"]) == ""
    assert outside_record_note(record, ["elsewhere/x.py"]) == ""
    assert outside_record_note(record, ["repo/backend/node_modules/x.js"]) == ""
    note = outside_record_note(record, ["repo/docs/a.md", "repo/backend/app/x.py"])
    assert "1 written file(s) lie outside the checkout record of repo/" in note
    assert "backend/app/x.py" in note and "docs/a.md" not in note
    assert 'materialize_repository(path="repo", add_paths=[...])' in note


def test_a_whole_tree_checkout_never_warns():
    assert outside_record_note(_record(sub_paths=()), ["repo/backend/app/x.py"]) == ""


async def _seed(composite) -> None:
    await _put(composite, f"repo/{RECORD_NAME}", _record().to_json())
    await _put(composite, "repo/docs/a.md", b"a\n")
    await _put(composite, "repo/backend/app/x.py", b"VALUE = 1\n")


async def test_edit_write_and_replace_warn_outside_the_record(store):  # noqa: F811
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _seed(composite)

    inside = await _call(composite, "sandbox_edit_file", path="repo/docs/a.md", edits=[{"find": "a", "replace": "b"}])
    assert not inside.is_error, inside.content
    assert "outside the checkout record" not in inside.content

    edited = await _call(
        composite, "sandbox_edit_file", path="repo/backend/app/x.py", edits=[{"find": "= 1", "replace": "= 2"}]
    )
    assert not edited.is_error, edited.content
    assert "lie outside the checkout record of repo/" in edited.content and "backend/app/x.py" in edited.content

    written = await _call(composite, "sandbox_write_file", path="repo/backend/app/y.py", content="Y = 1\n")
    assert not written.is_error, written.content
    assert "backend/app/y.py" in written.content and "outside the checkout record" in written.content

    replaced = await _call(
        composite, "sandbox_replace_in_files", pattern="VALUE", replacement="NAME", path_prefix="repo/backend"
    )
    assert not replaced.is_error, replaced.content
    assert "outside the checkout record" in replaced.content


async def test_a_file_outside_every_checkout_says_nothing(store):  # noqa: F811
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _seed(composite)
    written = await _call(composite, "sandbox_write_file", path="work/plan.md", content="plan\n")
    assert not written.is_error, written.content
    assert "outside the checkout record" not in written.content
