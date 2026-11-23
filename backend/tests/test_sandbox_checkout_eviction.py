"""Stale checkouts evicted when a materialize reaches the workspace file limit.

``eviction.py`` (pure) decides which checkout roots may go: clean (no change
since the checkout, no stray file) and whose recorded ref the provider says is
gone (a branch deleted at its merge). ``materialize_repository`` refused for
the file limit retries once without them, and its refusal names every root by
size, ref and state with the exact ``sandbox_remove_files`` call.
"""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from types import SimpleNamespace

from app.services.builtin_tools import sandbox_workspace as workspace_tools
from app.services.builtin_tools.media import build_code_interpreter_tool
from app.services.sandbox_workspace import eviction
from app.services.sandbox_workspace.checkout import RECORD_NAME, CheckoutRecord, build_record
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    FileContext,
    ToolCall,
    ToolDefinition,
    ToolResult,
    offering_tools,
)
from tests.test_sandbox_workspace_tools import (  # noqa: F401  (fixture)
    _archive_tool,
    _companion_archive,
    _composite,
    store,
)


def _entries(record, extra: dict[str, bytes] | None = None) -> dict[str, SimpleNamespace]:
    out = {record.workspace_path(p): SimpleNamespace(size=f.size, sha256=f.sha256) for p, f in record.files.items()}
    out[record.record_path] = SimpleNamespace(size=10, sha256="r")
    for path, data in (extra or {}).items():
        out[path] = SimpleNamespace(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    return out


def _record(path: str, ref: str = "feat/x", sub_paths: tuple[str, ...] = ()):
    return build_record(
        provider="github",
        repository="acme/repo",
        ref=ref,
        commit="0a1b2c3d4e",
        path=path,
        sub_paths=sub_paths,
        files=[("src/a.py", b"A\n"), ("src/b.py", b"B\n")],
    )


def test_a_clean_checkout_of_a_gone_ref_is_evictable_and_a_dirty_one_never():
    old, dirty = _record("old"), _record("dirty")
    entries = {**_entries(old), **_entries(dirty)}
    entries["dirty/src/a.py"] = SimpleNamespace(size=3, sha256="edited")
    found = eviction.footprints(entries, {"old": old, "dirty": dirty})
    by_path = {f.path: f for f in found}
    assert by_path["old"].clean and by_path["old"].files == 3
    assert not by_path["dirty"].clean and by_path["dirty"].changes == 1
    marked = [dataclasses.replace(f, ref_state=eviction.REF_GONE) for f in found]
    assert [f.path for f in marked if f.evictable] == ["old"], "a checkout with a local change is never evicted"
    assert sorted(eviction.eviction_paths([m for m in marked if m.evictable], entries)) == [
        f"old/{RECORD_NAME}",
        "old/src/a.py",
        "old/src/b.py",
    ]


def test_evicting_an_outer_checkout_never_takes_a_nested_one_with_it():
    outer = _record("prod")
    inner = build_record(
        provider="github",
        repository="acme/lab",
        ref="feat/lab",
        commit="0a1b2c3d4e",
        path="prod/lab",
        sub_paths=(),
        files=[("lab.py", b"L\n")],
    )
    entries = {**_entries(outer), **_entries(inner)}
    entries["prod/lab/lab.py"] = SimpleNamespace(size=3, sha256="edited by the agent")
    found = {f.path: f for f in eviction.footprints(entries, {"prod": outer, "prod/lab": inner})}
    assert found["prod"].clean and not found["prod/lab"].clean
    gone = dataclasses.replace(found["prod"], ref_state=eviction.REF_GONE)
    removed = sorted(eviction.eviction_paths([gone], entries))
    assert removed == [f"prod/{RECORD_NAME}", "prod/src/a.py", "prod/src/b.py"], (
        "the nested checkout's edit and record stay: only the deepest root a file sits under decides"
    )
    both = sorted(eviction.eviction_paths([gone, dataclasses.replace(found["prod/lab"], changes=0)], entries))
    assert f"prod/lab/{RECORD_NAME}" in both and "prod/lab/lab.py" in both


def test_a_stray_file_beside_a_narrow_checkout_keeps_it():
    narrow = _record("n", sub_paths=("src",))
    entries = _entries(narrow, {"n/notes/todo.md": b"mine"})
    (footprint,) = eviction.footprints(entries, {"n": narrow})
    assert not footprint.clean, "a file the record does not cover would be lost with the root"


def test_a_build_output_the_gitignore_excludes_never_keeps_a_checkout_of_an_ended_run():
    from app.services.sandbox_workspace.gitignore import GitIgnore

    built = _record("b")
    extra = {"b/frontend/dist/app.js": b"bundle", "b/backend/app.egg-info/PKG-INFO": b"pkg"}
    entries = _entries(built, extra)
    (dirty,) = eviction.footprints(entries, {"b": built})
    assert not dirty.clean, "without the rules the build output reads as added files"
    rules = GitIgnore([(".gitignore", b"dist/\n*.egg-info/\n")])
    (footprint,) = eviction.footprints(entries, {"b": built}, ignores={"b": rules})
    assert footprint.clean, "what sandbox_checkout_status calls clean is clean for the eviction too"
    ended = dataclasses.replace(footprint, run_state=eviction.RUN_ENDED)
    assert ended.evictable

    edited = _entries(built, {**extra, "b/src/new.py": b"mine"})
    (kept,) = eviction.footprints(edited, {"b": built}, ignores={"b": rules})
    assert not kept.clean and kept.changes == 1, "a file the rules do not exclude is still the agent's work"

    narrow = _record("n", sub_paths=("src",))
    stray = _entries(narrow, {"n/frontend/dist/app.js": b"bundle"})
    (outside,) = eviction.footprints(stray, {"n": narrow}, ignores={"n": rules})
    assert outside.clean, "an ignored file outside the sub_paths is no stray work either"


def test_a_leftover_of_an_earlier_wider_tree_never_keeps_a_clean_checkout():
    leftover = hashlib.sha256(b"old tree").hexdigest()
    narrow = dataclasses.replace(
        _record("n", sub_paths=("src",)), outside_baseline={"docs/old.md": leftover, "docs/gone.md": "x"}
    )
    entries = _entries(narrow, {"n/docs/old.md": b"old tree"})
    (footprint,) = eviction.footprints(entries, {"n": narrow})
    assert footprint.clean, "a file still as the earlier tree left it holds nothing of the agent"
    assert eviction.chosen([footprint], prune=True) == [footprint]

    edited = _entries(narrow, {"n/docs/old.md": b"edited by the agent"})
    (kept,) = eviction.footprints(edited, {"n": narrow})
    assert not kept.clean and kept.changes == 1, "an edited leftover is the agent's work: the checkout is kept"
    assert eviction.chosen([kept], prune=True) == []


def test_the_destination_and_a_pinned_ref_are_never_candidates():
    dest, pinned, default = _record("dest"), _record("pin", ref="0a1b2c3d4e"), _record("main", ref="")
    entries = {**_entries(dest), **_entries(pinned), **_entries(default)}
    found = eviction.footprints(entries, {"dest": dest, "pin": pinned, "main": default}, exclude="dest")
    assert {f.path for f in found} == {"pin", "main"}
    assert {f.ref_state for f in found} == {eviction.REF_PINNED}


def test_the_refusal_hint_names_each_root_and_the_removal_call():
    old, dirty = _record("old"), _record("dirty")
    entries = {**_entries(old), **_entries(dirty)}
    entries["dirty/src/a.py"] = SimpleNamespace(size=3, sha256="edited")
    hint = eviction.refusal_hint(eviction.footprints(entries, {"old": old, "dirty": dirty}))
    assert "workspace/old/: 3 files" in hint and "acme/repo@feat/x" in hint
    assert "1 local change(s), kept" in hint
    assert 'sandbox_remove_files(paths=["old"])' in hint and '"dirty"' not in hint


# -- materialize_repository --------------------------------------------------


def _compare_tool(gone: set[str], seen: list) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    executor = CallableToolExecutor()

    async def compare_github_commits(repo: str, base: str, head: str, file_offset: int = 0) -> ToolResult:
        seen.append((repo, base, head))
        if head in gone:
            return ToolResult(
                tool_call_id="", content="ref_not_found", structured={"status": "ref_not_found", "merge_base": ""}
            )
        return ToolResult(tool_call_id="", content="identical", structured={"status": "identical", "merge_base": "x"})

    executor.register("compare_github_commits", compare_github_commits)
    return [
        ToolDefinition(
            name="compare_github_commits",
            description="compare",
            input_schema={"type": "object", "properties": {"repo": {"type": "string"}}},
            source="integration:github",
            source_label="GitHub",
            repository_compare=True,
            read_only=True,
        )
    ], executor


def _limited(monkeypatch, max_files: int) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "code_interpreter_workspace_max_files", max_files)


async def test_materialize_over_the_limit_evicts_a_clean_checkout_of_a_merged_branch(store, monkeypatch):  # noqa: F811
    _limited(monkeypatch, 8)
    composite, _ = _composite(uuid.uuid4(), uuid.uuid4())
    composite.add_tools(*_archive_tool([]))
    seen: list = []
    composite.add_tools(*_compare_tool({"feat/merged"}, seen))
    with offering_tools(composite.all_tools):
        first = await composite.execute_tool(
            ToolCall(
                id="m1",
                name="materialize_repository",
                arguments={"provider": "github", "repository": "acme/repo", "ref": "feat/merged", "path": "old"},
            )
        )
        assert not first.is_error, first.content
        second = await composite.execute_tool(
            ToolCall(
                id="m2",
                name="materialize_repository",
                arguments={"provider": "github", "repository": "acme/repo", "ref": "v1", "path": "new"},
            )
        )
    assert not second.is_error, second.content
    assert "Evicted to stay under the file limit" in second.content and "workspace/old/" in second.content
    (record,) = store.records.values()
    paths = sorted(store.files[record.id])
    assert not any(p.startswith("old/") for p in paths)
    assert f"new/{RECORD_NAME}" in paths
    assert seen == [("acme/repo", "feat/merged", "feat/merged")]


async def test_materialize_over_the_limit_never_removes_a_dirty_checkout(store, monkeypatch):  # noqa: F811
    _limited(monkeypatch, 8)
    composite, executor = _composite(uuid.uuid4(), uuid.uuid4())
    composite.add_tools(*_archive_tool([]))
    composite.add_tools(*_compare_tool({"feat/merged"}, []))
    with offering_tools(composite.all_tools):
        await composite.execute_tool(
            ToolCall(
                id="m1",
                name="materialize_repository",
                arguments={"provider": "github", "repository": "acme/repo", "ref": "feat/merged", "path": "old"},
            )
        )
        await executor.session.put_bytes("old/src/app.py", b"edited\n")
        refused = await composite.execute_tool(
            ToolCall(
                id="m2",
                name="materialize_repository",
                arguments={"provider": "github", "repository": "acme/repo", "ref": "v1", "path": "new"},
            )
        )
    assert refused.is_error
    assert "file limit" in refused.content
    assert "workspace/old/" in refused.content and "1 local change(s), kept" in refused.content
    (record,) = store.records.values()
    assert store.files[record.id]["old/src/app.py"] is not None, "the edit is kept"
    assert not any(p.startswith("new/") for p in store.files[record.id])


async def test_materialize_over_the_limit_keeps_a_checkout_whose_ref_exists_and_names_it(store, monkeypatch):  # noqa: F811
    _limited(monkeypatch, 8)
    composite, _ = _composite(uuid.uuid4(), uuid.uuid4())
    composite.add_tools(*_archive_tool([]))
    composite.add_tools(*_compare_tool(set(), []))
    with offering_tools(composite.all_tools):
        await composite.execute_tool(
            ToolCall(
                id="m1",
                name="materialize_repository",
                arguments={"provider": "github", "repository": "acme/repo", "ref": "feat/open", "path": "old"},
            )
        )
        refused = await composite.execute_tool(
            ToolCall(
                id="m2",
                name="materialize_repository",
                arguments={"provider": "github", "repository": "acme/repo", "ref": "v1", "path": "new"},
            )
        )
    assert refused.is_error
    assert "ref present, clean" in refused.content
    assert 'sandbox_remove_files(paths=["old"])' in refused.content
    assert "prune_clean_checkouts=true" in refused.content, "the refusal names the explicit flag"


def test_prune_chooses_every_clean_checkout_whatever_its_ref():
    present, dirty = _record("present"), _record("dirty")
    entries = {**_entries(present), **_entries(dirty)}
    entries["dirty/src/a.py"] = SimpleNamespace(size=3, sha256="edited")
    found = [
        dataclasses.replace(f, ref_state=eviction.REF_PRESENT)
        for f in eviction.footprints(entries, {"present": present, "dirty": dirty})
    ]
    assert eviction.chosen(found) == []
    assert [f.path for f in eviction.chosen(found, prune=True)] == ["present"], "a dirty checkout never goes"


# -- materialize_repository(add_paths=...) -----------------------------------


async def _grow_setup(composite, executor, monkeypatch, *, others: list[tuple[str, str]], edit: str = "") -> None:
    """Checkouts *others* (path, ref) and a narrow ``repo`` checkout, then the file limit set at what they hold."""
    with offering_tools(composite.all_tools):
        for path, ref in others:
            done = await composite.execute_tool(
                ToolCall(
                    id=f"o-{path}",
                    name="materialize_repository",
                    arguments={"provider": "github", "repository": "acme/repo", "ref": ref, "path": path},
                )
            )
            assert not done.is_error, done.content
        narrow = await composite.execute_tool(
            ToolCall(
                id="n",
                name="materialize_repository",
                arguments={
                    "provider": "github",
                    "repository": "acme/repo",
                    "ref": "main",
                    "sub_paths": ["backend/app/core"],
                    "path": "repo",
                },
            )
        )
        assert not narrow.is_error, narrow.content
    if edit:
        await executor.session.put_bytes(edit, b"edited\n")
    # Full: the grow's first new file goes over the limit.
    _limited(monkeypatch, len(await executor.session.entries(refresh=True)))
    executor.session._limits = None  # read from the settings again


_GROW = {"provider": "github", "repository": "acme/repo", "path": "repo", "add_paths": ["frontend"]}


async def test_add_paths_over_the_limit_evicts_a_clean_checkout_of_a_merged_branch(store, monkeypatch):  # noqa: F811
    composite, executor = _composite(uuid.uuid4(), uuid.uuid4())
    composite.add_tools(*_archive_tool([], _companion_archive))
    seen: list = []
    composite.add_tools(*_compare_tool({"feat/merged"}, seen))
    await _grow_setup(composite, executor, monkeypatch, others=[("old", "feat/merged")])
    with offering_tools(composite.all_tools):
        grown = await composite.execute_tool(ToolCall(id="g", name="materialize_repository", arguments=_GROW))
    assert not grown.is_error, grown.content
    assert "Grew the checkout at workspace/repo/ by frontend" in grown.content
    assert (
        "Evicted to stay under the file limit (clean, ref gone)" in grown.content and "workspace/old/" in grown.content
    )
    (record,) = store.records.values()
    paths = set(store.files[record.id])
    assert not any(p.startswith("old/") for p in paths)
    assert "repo/frontend/package.json" in paths and "repo/backend/app/core/x.py" in paths
    assert seen == [("acme/repo", "feat/merged", "feat/merged")]


async def test_add_paths_over_the_limit_refusal_names_each_checkout(store, monkeypatch):  # noqa: F811
    composite, executor = _composite(uuid.uuid4(), uuid.uuid4())
    composite.add_tools(*_archive_tool([], _companion_archive))
    composite.add_tools(*_compare_tool(set(), []))
    await _grow_setup(composite, executor, monkeypatch, others=[("old", "feat/open")])
    with offering_tools(composite.all_tools):
        refused = await composite.execute_tool(ToolCall(id="g", name="materialize_repository", arguments=_GROW))
    assert refused.is_error
    assert "workspace/repo/ was not grown" in refused.content
    assert "workspace/old/" in refused.content and "ref present, clean" in refused.content
    assert 'sandbox_remove_files(paths=["old"])' in refused.content and "prune_clean_checkouts=true" in refused.content
    (record,) = store.records.values()
    assert "repo/frontend/package.json" not in store.files[record.id]


async def test_prune_removes_a_clean_checkout_of_a_live_ref_keeps_a_dirty_one(store, monkeypatch):  # noqa: F811
    composite, executor = _composite(uuid.uuid4(), uuid.uuid4())
    composite.add_tools(*_archive_tool([], _companion_archive))
    seen: list = []
    composite.add_tools(*_compare_tool(set(), seen))
    await _grow_setup(
        composite,
        executor,
        monkeypatch,
        others=[("old", "feat/open"), ("dirty", "feat/open")],
        edit="dirty/frontend/package.json",
    )
    with offering_tools(composite.all_tools):
        grown = await composite.execute_tool(
            ToolCall(id="g", name="materialize_repository", arguments={**_GROW, "prune_clean_checkouts": True})
        )
    assert not grown.is_error, grown.content
    assert "Evicted to stay under the file limit (clean, prune_clean_checkouts)" in grown.content
    (record,) = store.records.values()
    paths = set(store.files[record.id])
    assert not any(p.startswith("old/") for p in paths)
    assert store.files[record.id]["dirty/frontend/package.json"] is not None, "the checkout with an edit is kept"
    assert "repo/frontend/package.json" in paths
    assert seen == [], "with prune the ref is not read"


# -- checkouts carry their run (#11500) ---------------------------------------

RUN_OLD = str(uuid.uuid4())
RUN_LIVE = str(uuid.uuid4())


def test_the_record_carries_the_run_that_wrote_it():
    record = dataclasses.replace(_record("r"), run_id=RUN_OLD)
    again = CheckoutRecord.from_json(record.to_json(), path="r")
    assert again.run_id == RUN_OLD
    assert CheckoutRecord.from_json(_record("r").to_json(), path="r").run_id == "", "an older record reads no run"
    moved = again.carried(files=again.files, ref="feat/y")
    assert moved.run_id == RUN_OLD, "a commit or a rebase keeps the run"


def test_a_clean_checkout_of_an_ended_run_is_evictable_whatever_its_ref():
    old, dirty = (
        dataclasses.replace(_record("old"), run_id=RUN_OLD),
        dataclasses.replace(_record("dirty"), run_id=RUN_OLD),
    )
    entries = {**_entries(old), **_entries(dirty)}
    entries["dirty/src/a.py"] = SimpleNamespace(size=3, sha256="edited")
    found = [
        dataclasses.replace(f, ref_state=eviction.REF_PRESENT, run_state=eviction.RUN_ENDED)
        for f in eviction.footprints(entries, {"old": old, "dirty": dirty})
    ]
    assert [f.path for f in eviction.chosen(found)] == ["old"], "a checkout with a local change never goes"
    assert "run ended" in eviction.evicted_line(eviction.chosen(found))


def test_a_checkout_of_the_live_run_is_never_stale_an_idle_one_is():
    mine = dataclasses.replace(_record("mine"), run_id=RUN_LIVE)
    idle = _record("idle")
    entries = {**_entries(mine), **_entries(idle)}
    for path in list(entries):
        entries[path] = SimpleNamespace(size=entries[path].size, sha256=entries[path].sha256, mtime=0.0)
    now = float(eviction.IDLE_SECONDS) + 10
    found = {
        f.path: f for f in eviction.footprints(entries, {"mine": mine, "idle": idle}, current_run=RUN_LIVE, now=now)
    }
    assert found["mine"].run_state == eviction.RUN_LIVE and not found["mine"].evictable
    assert found["idle"].evictable and found["idle"].reason() == "idle past the TTL"
    hint = eviction.refusal_hint(list(found.values()))
    assert f"run {RUN_LIVE[:8]} (this run)" in hint and "idle 24 h" in hint, "the refusal names what holds the space"


def _run_composite(run_id: str) -> tuple[CompositeToolExecutor, object]:
    composite = CompositeToolExecutor()
    composite.set_file_context(FileContext())
    definitions, executor = build_code_interpreter_tool(user_id=str(uuid.uuid4()), assignment_run_id=run_id)
    composite.add_tools(definitions, executor)
    return composite, executor


async def _materialize(composite, path: str, ref: str):
    return await composite.execute_tool(
        ToolCall(
            id=f"m-{path}",
            name="materialize_repository",
            arguments={"provider": "github", "repository": "acme/repo", "ref": ref, "path": path},
        )
    )


async def _handed_to(executor, path: str, run_id: str) -> None:
    """The checkout at *path* as an earlier run of the item left it: its record names *run_id*."""
    _entry, data = await executor.session.get_bytes(f"{path}/{RECORD_NAME}")
    record = CheckoutRecord.from_json(data, path=path)
    await executor.session.put_bytes(f"{path}/{RECORD_NAME}", dataclasses.replace(record, run_id=run_id).to_json())


async def test_a_checkout_of_an_ended_run_is_reclaimed_and_two_live_checkouts_fit(store, monkeypatch):  # noqa: F811
    composite, executor = _run_composite(RUN_LIVE)
    composite.add_tools(*_archive_tool([]))
    seen: list = []
    composite.add_tools(*_compare_tool(set(), seen))
    asked: list = []

    async def ended(run_ids):
        asked.append(set(run_ids))
        return {r for r in run_ids if r == RUN_OLD}

    monkeypatch.setattr(workspace_tools, "_ended_runs", ended)
    with offering_tools(composite.all_tools):
        first = await _materialize(composite, "old", "feat/open")
        assert not first.is_error, first.content
        await _handed_to(executor, "old", RUN_OLD)
        held = len(await executor.session.entries(refresh=True))
        # Room for two checkouts of the live run, not three trees.
        _limited(monkeypatch, held * 2)
        executor.session._limits = None
        one = await _materialize(composite, "one", "v1")
        assert not one.is_error, one.content
        two = await _materialize(composite, "two", "v2")
    assert not two.is_error, two.content
    assert "Evicted to stay under the file limit (clean, run ended)" in two.content and "workspace/old/" in two.content
    assert f"run {RUN_OLD[:8]} ended" in two.content
    (record,) = store.records.values()
    paths = set(store.files[record.id])
    assert not any(p.startswith("old/") for p in paths)
    assert f"one/{RECORD_NAME}" in paths and f"two/{RECORD_NAME}" in paths, "both checkouts of the live run are kept"
    _entry, data = await executor.session.get_bytes(f"two/{RECORD_NAME}")
    assert CheckoutRecord.from_json(data, path="two").run_id == RUN_LIVE
    assert ("acme/repo", "feat/open", "feat/open") not in seen, (
        "a checkout reclaimable for its ended run needs no provider read"
    )
    assert asked and all(RUN_LIVE not in a for a in asked), "the live run is never asked about"


async def test_an_ended_runs_checkout_with_an_ignored_build_output_is_reclaimed(store, monkeypatch):  # noqa: F811
    import io
    import tarfile

    def tracked_gitignore() -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
            for name, data in ((".gitignore", b"dist/\n"), ("src/a.py", b"A\n")):
                info = tarfile.TarInfo(f"acme-repo-0a1b2c3/{name}")
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        return buffer.getvalue()

    composite, executor = _run_composite(RUN_LIVE)
    composite.add_tools(*_archive_tool([], archive=tracked_gitignore))
    composite.add_tools(*_compare_tool(set(), []))

    async def ended(run_ids):
        return {r for r in run_ids if r == RUN_OLD}

    monkeypatch.setattr(workspace_tools, "_ended_runs", ended)
    with offering_tools(composite.all_tools):
        first = await _materialize(composite, "old", "feat/open")
        assert not first.is_error, first.content
        await _handed_to(executor, "old", RUN_OLD)
        # The ended run built in its checkout: an output its tracked .gitignore excludes, as a build leaves.
        await executor.session.put_bytes("old/dist/bundle.js", b"built")
        held = len(await executor.session.entries(refresh=True))
        _limited(monkeypatch, held + 1)
        executor.session._limits = None
        fresh = await _materialize(composite, "fresh", "v1")
    assert not fresh.is_error, fresh.content
    assert "Evicted to stay under the file limit" in fresh.content and "workspace/old/" in fresh.content
    (record,) = store.records.values()
    assert not any(p.startswith("old/") for p in store.files[record.id]), "the ignored build output went with it"


async def test_the_refusal_names_the_live_runs_checkouts_as_holders(store, monkeypatch):  # noqa: F811
    composite, executor = _run_composite(RUN_LIVE)
    composite.add_tools(*_archive_tool([]))
    composite.add_tools(*_compare_tool(set(), []))

    async def nothing_ended(run_ids):
        return set()

    monkeypatch.setattr(workspace_tools, "_ended_runs", nothing_ended)
    with offering_tools(composite.all_tools):
        first = await _materialize(composite, "mine", "feat/open")
        assert not first.is_error, first.content
        _limited(monkeypatch, len(await executor.session.entries(refresh=True)))
        executor.session._limits = None
        refused = await _materialize(composite, "next", "v1")
    assert refused.is_error
    assert f"run {RUN_LIVE[:8]} (this run)" in refused.content and "workspace/mine/" in refused.content
    (record,) = store.records.values()
    assert f"mine/{RECORD_NAME}" in store.files[record.id], "a checkout of the live run is never reclaimed"


# -- sandbox_checkout_rebase ---------------------------------------------------


_REBASE_V1 = {"src/app.py": b"a\n", "src/old.py": b"OLD\n"}
_REBASE_MAIN = {"src/app.py": b"a\n", "src/new.py": b"NEW\n", "src/more.py": b"MORE\n"}


def _rebase_compare_tool(gone: set[str], seen: list) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """A compare that says a gone ref is gone, and that the recorded commit 0a1b2c3 is behind main."""
    executor = CallableToolExecutor()

    async def compare_github_commits(repo: str, base: str, head: str, file_offset: int = 0) -> ToolResult:
        seen.append((repo, base, head))
        if base == head:
            status = "ref_not_found" if head in gone else "identical"
            return ToolResult(tool_call_id="", content=status, structured={"status": status, "merge_base": ""})
        return ToolResult(tool_call_id="", content="behind", structured={"status": "behind", "merge_base": "0a1b2c3"})

    executor.register("compare_github_commits", compare_github_commits)
    definitions, _ = _compare_tool(set(), [])
    return definitions, executor


async def _rebase_setup(store, monkeypatch, gone: set[str]):  # noqa: F811
    from tests.test_sandbox_workspace_tools import _refs_archive_tool

    composite, executor = _composite(uuid.uuid4(), uuid.uuid4())
    trees = {"v1": ("0a1b2c3", _REBASE_V1), "main": ("9f9f9f9", _REBASE_MAIN), "feat/merged": ("7e7e7e7", _REBASE_V1)}
    composite.add_tools(*_refs_archive_tool(trees, []))
    seen: list = []
    composite.add_tools(*_rebase_compare_tool(gone, seen))
    with offering_tools(composite.all_tools):
        for path, ref in (("old", "feat/merged"), ("repo", "v1")):
            done = await composite.execute_tool(
                ToolCall(
                    id=f"m-{path}",
                    name="materialize_repository",
                    arguments={"provider": "github", "repository": "acme/repo", "ref": ref, "path": path},
                )
            )
            assert not done.is_error, done.content
    # Full: the rebase adds one file net (two added by main, one removed).
    _limited(monkeypatch, len(await executor.session.entries(refresh=True)))
    executor.session._limits = None
    return composite, executor, seen


async def test_rebase_over_the_limit_evicts_a_clean_checkout_of_a_merged_branch(store, monkeypatch):  # noqa: F811
    composite, executor, seen = await _rebase_setup(store, monkeypatch, {"feat/merged"})
    with offering_tools(composite.all_tools):
        rebased = await composite.execute_tool(
            ToolCall(id="r", name="sandbox_checkout_rebase", arguments={"checkout": "repo", "ref": "main"})
        )
    assert not rebased.is_error, rebased.content
    assert "Evicted to stay under the file limit (clean, ref gone)" in rebased.content
    assert "workspace/old/" in rebased.content
    (record,) = store.records.values()
    paths = set(store.files[record.id])
    assert not any(p.startswith("old/") for p in paths)
    assert {"repo/src/new.py", "repo/src/more.py"} <= paths and "repo/src/old.py" not in paths
    _entry, data = await executor.session.get_bytes(f"repo/{RECORD_NAME}")
    assert CheckoutRecord.from_json(data, path="repo").commit == "9f9f9f9"
    assert ("acme/repo", "feat/merged", "feat/merged") in seen


async def test_rebase_over_the_limit_refusal_names_each_checkout(store, monkeypatch):  # noqa: F811
    composite, executor, _seen = await _rebase_setup(store, monkeypatch, set())
    with offering_tools(composite.all_tools):
        refused = await composite.execute_tool(
            ToolCall(id="r", name="sandbox_checkout_rebase", arguments={"checkout": "repo", "ref": "main"})
        )
    assert refused.is_error
    assert "workspace/repo/ was not rebased" in refused.content
    assert "workspace/old/" in refused.content and 'sandbox_remove_files(paths=["old"])' in refused.content
    _entry, data = await executor.session.get_bytes(f"repo/{RECORD_NAME}")
    assert CheckoutRecord.from_json(data, path="repo").commit == "0a1b2c3", "a refused rebase leaves the record"
