"""sandbox_codemod: a transform module run by the checkout's codemod check, written all or nothing."""

from __future__ import annotations

import json
import posixpath
import uuid

import pytest

from app.services.sandbox_workspace import codemod, commands
from tests.test_sandbox_workspace_tools import _call, _composite, _put, _workspace_bytes, store  # noqa: F401

CHECKS = b"""
[check.frontend-codemod]
argv = ["node", "scripts/codemod.mjs"]
cwd = "frontend"
image = "nodejs-dev"
install_first = false
"""


def test_plan_keeps_changes_and_refuses_throws_and_parse_failures():
    befores = [("a.tsx", "a"), ("b.tsx", "b"), ("c.tsx", "c"), ("d.tsx", "d"), ("e.tsx", "e")]
    results = {
        "a.tsx": {"path": "a.tsx", "changed": True, "parse_errors": []},
        "b.tsx": {"path": "b.tsx", "changed": False},
        "c.tsx": {"path": "c.tsx", "changed": True, "parse_errors": ["Unexpected token"]},
        "d.tsx": {"path": "d.tsx", "changed": False, "error": "boom"},
    }
    planned = codemod.plan(befores, results, {"a.tsx": "A", "c.tsx": "C"})
    assert [c.path for c in planned.changes] == ["a.tsx"]
    assert planned.files_scanned == 5
    assert dict(planned.refused) == {
        "c.tsx": "no longer parses: Unexpected token",
        "d.tsx": "the transform threw: boom",
        "e.tsx": "the runner reported nothing for it",
    }
    text = codemod.refusal(planned, lambda p: p)
    assert text.startswith("the codemod is refused on 3 file(s) of 5: nothing was written.")


def test_result_and_request_shapes():
    assert codemod.root_from("repo/frontend") == "../.."
    assert codemod.root_from("") == "."
    assert json.loads(codemod.request(transform="w/t.mjs", files=["a"], root=".."))["files"] == ["a"]
    with pytest.raises(codemod.CodemodError, match="the transform could not run: no default"):
        codemod.read_result(b'{"error": "no default", "files": []}')
    with pytest.raises(codemod.CodemodError, match="unreadable"):
        codemod.read_result(b"{")


def _runner(transform):
    """A stand-in for run_with_fallback: the runner's contract, in Python, over the request the tool staged."""
    seen: list[commands.ResolvedCommand] = []

    async def run(command, *, workspace, **_kwargs):
        seen.append(command)
        staging = posixpath.normpath(posixpath.join(command.cwd, command.argv[-1]))
        _entry, raw = await workspace.get_bytes(f"{staging}/{codemod.REQUEST_FILE}")
        request = json.loads(raw)
        files, staged = [], []
        for path in request["files"]:
            _e, data = await workspace.get_bytes(path)
            source = data.decode()
            after, parse_errors = transform(path, source)
            if after is None or after == source:
                files.append({"path": path, "changed": False})
                continue
            staged.append((f"{staging}/{codemod.OUTPUT_DIR}/{path}", after.encode()))
            files.append({"path": path, "changed": True, "parse_errors": parse_errors})
        staged.append((f"{staging}/{codemod.RESULT_FILE}", json.dumps({"files": files}).encode()))
        await workspace.put_tree(staged)
        return commands.CommandOutcome(text="frontend-codemod: exit 0", exit_code=0)

    return run, seen


@pytest.fixture
def offered(monkeypatch, store):  # noqa: F811
    from app.core.config import settings

    monkeypatch.setattr("app.services.builtin_tools.sandbox_workspace.commands_offered", lambda: True)
    monkeypatch.setattr(settings, "opensandbox_images", {"nodejs-dev": "n"})
    return store


async def _seed(composite):
    await _put(composite, "repo/.xtm-one/checks.toml", CHECKS)
    await _put(composite, "repo/frontend/src/a.tsx", b"const a = x ? <c /> : null;\n")
    await _put(composite, "repo/frontend/src/b.tsx", b"const b = 1;\n")
    await _put(composite, "work/t.mjs", b"export default () => null;\n")


async def test_dry_run_reports_then_the_write_lands_in_one_commit(offered, monkeypatch):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _seed(composite)
    run, seen = _runner(lambda _p, s: (s.replace("x ? <c /> : null", "x && <c />"), []))
    monkeypatch.setattr(commands, "run_with_fallback", run)
    args = {"checkout": "repo", "transform": "work/t.mjs", "path_prefix": "repo/frontend/src"}
    preview = await _call(composite, "sandbox_codemod", dry_run=True, **args)
    assert not preview.is_error, preview.content
    assert preview.content.startswith("Dry run, nothing written: 1 file(s) changed of 2 transformed")
    assert "workspace/repo/frontend/src/a.tsx: +1 -1" in preview.content
    assert seen[0].check == "frontend-codemod" and seen[0].argv[:2] == ("node", "scripts/codemod.mjs")
    assert seen[0].argv[-1].startswith("../../.xtm-codemod/")
    assert await _workspace_bytes(offered, conversation, "repo/frontend/src/a.tsx") == b"const a = x ? <c /> : null;\n"
    done = await _call(composite, "sandbox_codemod", **args)
    assert not done.is_error, done.content
    assert done.content.startswith("Transformed: 1 file(s) changed of 2")
    assert "+const a = x && <c />;" in done.content
    assert await _workspace_bytes(offered, conversation, "repo/frontend/src/a.tsx") == b"const a = x && <c />;\n"
    (record,) = [r for r in offered.records.values() if r.conversation_id == conversation]
    assert not [p for p in offered.files[record.id] if p.startswith(codemod.STAGING_DIR)]


async def test_a_parse_failure_refuses_the_whole_write(offered, monkeypatch):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _seed(composite)

    def transform(path, source):
        if path.endswith("a.tsx"):
            return source.replace("<c />", "<c>"), ["Unexpected token (1:20)"]
        return source.replace("1", "2"), []

    run, _seen = _runner(transform)
    monkeypatch.setattr(commands, "run_with_fallback", run)
    refused = await _call(
        composite, "sandbox_codemod", checkout="repo", transform="work/t.mjs", include_globs=["*.tsx"]
    )
    assert refused.is_error
    assert "refused on 1 file(s) of 2: nothing was written" in refused.content
    assert "a.tsx: no longer parses: Unexpected token (1:20)" in refused.content
    assert await _workspace_bytes(offered, conversation, "repo/frontend/src/b.tsx") == b"const b = 1;\n"
    (record,) = [r for r in offered.records.values() if r.conversation_id == conversation]
    assert not [p for p in offered.files[record.id] if p.startswith(codemod.STAGING_DIR)]


async def test_refusals_before_anything_runs(offered, monkeypatch):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _put(composite, "repo/src/a.tsx", b"x\n")
    await _put(composite, "work/t.mjs", b"export default () => null;\n")

    async def never(*_a, **_k):
        raise AssertionError("nothing may run")

    monkeypatch.setattr(commands, "run_with_fallback", never)
    missing = await _call(composite, "sandbox_codemod", checkout="repo", transform="work/none.mjs", path_prefix="repo")
    assert missing.is_error and "is not in the workspace" in missing.content
    unscoped = await _call(composite, "sandbox_codemod", checkout="repo", transform="work/t.mjs")
    assert unscoped.is_error and "path_prefix or include_globs" in unscoped.content
    undeclared = await _call(composite, "sandbox_codemod", checkout="repo", transform="work/t.mjs", path_prefix="repo")
    assert undeclared.is_error and "declares no codemod check" in undeclared.content
