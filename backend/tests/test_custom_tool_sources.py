"""Custom tool repository sources (``services/custom_tool_sources.py``).

Unit part: repository coordinates, manifest discovery and reading.

PostgreSQL part (``integration``, skipped when unreachable), every call made
as a NON-admin: a sync creates the tools, skips an unchanged one, keeps the
active version when a new one fails its tests, disables a tool gone from the
repository and leaves a detached one alone; a synced tool's content is
read-only on the REST routes while its sharing stays editable; a personal
source is invisible to another user.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Literal, get_args

import pytest
from fastapi import HTTPException
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import custom_tool_sources as sources_api
from app.api.v1 import custom_tools as tools_api
from app.api.v1.schemas.custom_tool import CustomToolUpdate
from app.models.custom_tool import CustomTool
from app.models.custom_tool_source import (
    CUSTOM_TOOL_SOURCE_INTERVALS,
    CUSTOM_TOOL_SOURCE_PROVIDERS,
    CustomToolSource,
)
from app.models.user import User
from app.services import custom_tool_sources as sync
from app.services import custom_tool_tests
from tests.scratch_database import ALL_TABLES, scratch_database

# ---------------------------------------------------------------------------
# Unit
# ---------------------------------------------------------------------------


def test_api_vocabularies_match_the_model():
    assert set(get_args(sources_api.Provider)) == set(CUSTOM_TOOL_SOURCE_PROVIDERS)
    assert set(get_args(sources_api.Interval)) == set(CUSTOM_TOOL_SOURCE_INTERVALS)
    assert sources_api.Provider is not Literal  # a real Literal alias


@pytest.mark.parametrize(
    ("provider", "raw", "expected"),
    [
        ("github", "acme/tools", "acme/tools"),
        ("github", "https://github.com/acme/tools.git", "acme/tools"),
        ("github", "https://github.com/acme/tools/tree/main/sub", "acme/tools"),
        ("gitlab", "https://gitlab.example.com/grp/sub/proj", "grp/sub/proj"),
        ("gitlab", "grp/proj/-/tree/main", "grp/proj"),
    ],
)
def test_normalize_repository(provider, raw, expected):
    assert sync.normalize_repository(provider, raw) == expected


@pytest.mark.parametrize(
    ("provider", "raw"),
    [("github", "acme"), ("github", "a/b/c"), ("gitlab", "proj"), ("github", "acme/../x"), ("github", "a/b c")],
)
def test_normalize_repository_refuses(provider, raw):
    with pytest.raises(ValueError):
        sync.normalize_repository(provider, raw)


def test_normalize_subpath():
    assert sync.normalize_subpath(" /tools/ ") == "tools"
    assert sync.normalize_subpath(None) == ""
    for bad in ("../x", "a//b", "a/./b"):
        with pytest.raises(ValueError):
            sync.normalize_subpath(bad)


def test_find_manifests_prefers_tool_yaml():
    assert sync.find_manifests(
        ["a/tool.yml", "a/tool.yaml", "b/tool.yml", "c/main.py", "tool.yaml", "d/e/tool.yaml"]
    ) == {"": "tool.yaml", "a": "a/tool.yaml", "b": "b/tool.yml", "d/e": "d/e/tool.yaml"}


class FakeReader(sync.RepoReader):
    """An in-memory repository: ``{commit: {path: text}}``."""

    def __init__(self, commits: dict[str, dict[str, str]], head: str) -> None:
        self.commits = commits
        self.head = head

    async def head_commit(self, ref):
        return self.head

    async def list_files(self, sha, subpath):
        prefix = f"{subpath}/" if subpath else ""
        return [p for p in self.commits[sha] if p.startswith(prefix)]

    async def read_file(self, sha, path):
        try:
            return self.commits[sha][path].encode()
        except KeyError:
            raise sync.SourceSyncError(f"no {path}") from None

    def web_url(self, sha, path):
        return f"https://example/{sha}/{path}"


MANIFEST = """\
name: {name}
description: Looks a domain up
input_schema:
  type: object
  properties:
    domain: {{type: string}}
  required: [domain]
timeout: 30
tags: [osint]
tests:
  - name: dry
    inputs: {{domain: example.com}}
    dry_run: true
    expect: {{contains: DRY}}
"""


def _github_reader(trees: dict[str, dict], requests: list[str]) -> sync.GitHubReader:
    """A GitHub reader whose API answers ``git/trees/{sha}`` from *trees*."""
    import httpx

    def answer(request: httpx.Request) -> httpx.Response:
        recursive = request.url.params.get("recursive") == "1"
        tree_sha = request.url.path.rsplit("/", 1)[-1]
        requests.append(f"{tree_sha}{' recursive' if recursive else ''}")
        key = f"{tree_sha}:recursive" if recursive else tree_sha
        if key not in trees:
            return httpx.Response(404)
        return httpx.Response(200, json=trees[key])

    access = sync.RepoAccess(provider="github", api_base="https://api.github.com", web_base="https://github.com")
    return sync.GitHubReader(access, "acme/mono", httpx.AsyncClient(transport=httpx.MockTransport(answer)))


async def test_github_reads_the_tree_of_the_sub_directory_only():
    trees = {
        "c1": {"tree": [{"path": "README.md", "type": "blob"}, {"path": "infra", "type": "tree", "sha": "t1"}]},
        "t1": {"tree": [{"path": "tools", "type": "tree", "sha": "t2"}]},
        "t2:recursive": {
            "tree": [
                {"path": "whois", "type": "tree", "sha": "t3"},
                {"path": "whois/tool.yaml", "type": "blob"},
                {"path": "whois/main.py", "type": "blob"},
            ]
        },
    }
    requests: list[str] = []
    reader = _github_reader(trees, requests)
    assert await reader.list_files("c1", "infra/tools") == ["infra/tools/whois/tool.yaml", "infra/tools/whois/main.py"]
    # The whole repository is never read recursively: its tree may be truncated.
    assert requests == ["c1", "t1", "t2 recursive"]
    assert await reader.list_files("c1", "infra/nope") == []

    trees["t2:recursive"]["truncated"] = True
    with pytest.raises(sync.SourceSyncError, match=r"'infra/tools' is too large .* narrower"):
        await reader.list_files("c1", "infra/tools")
    trees["c1:recursive"] = {"tree": [], "truncated": True}
    with pytest.raises(sync.SourceSyncError, match="Set a sub-directory"):
        await reader.list_files("c1", "")


async def _read(files: dict[str, str], directory: str):
    reader = FakeReader({"c1": files}, "c1")
    manifests = sync.find_manifests(list(files))
    return await sync.read_tool(reader, "c1", directory, manifests[directory], set(files))


async def test_read_tool_defaults():
    bundle = await _read(
        {
            "tools/whois/tool.yaml": MANIFEST.format(name="whois"),
            "tools/whois/main.py": "print('x')",
            "tools/whois/test_main.py": "def test_x():\n    pass\n",
        },
        "tools/whois",
    )
    assert bundle.language == "python"
    assert bundle.manifest.timeout == 30
    assert bundle.test_cases == [
        {"name": "dry", "inputs": {"domain": "example.com"}, "dry_run": True,
         "expect": {"contains": ["DRY"]}, "expect_error": False}
    ]
    assert bundle.test_code.startswith("def test_x")
    assert bundle.files == ["tools/whois/tool.yaml", "tools/whois/main.py", "tools/whois/test_main.py"]


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"t/tool.yaml": "name: t\n"}, "No entry file"),
        ({"t/tool.yaml": "name: t\nentry: ../x.py\n", "x.py": ""}, "inside the tool's directory"),
        ({"t/tool.yaml": "name: t\nbogus: 1\n", "t/main.py": ""}, "bogus"),
        ({"t/tool.yaml": "name: t\nentry: run.sh\n", "t/run.sh": ""}, "language"),
        ({"t/tool.yaml": "- a list\n", "t/main.py": ""}, "mapping"),
        ({"t/tool.yaml": "name: t\ntests: [{name: a}, {name: a}]\n", "t/main.py": ""}, "Two test cases"),
    ],
)
async def test_read_tool_refuses(files, message):
    with pytest.raises(ValueError, match=message):
        await _read(files, "t")


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


def _user(role: str, name: str) -> User:
    return User(
        email=f"{name}-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name=name.title(),
        role=role,
    )


@pytest.fixture
async def db():
    async with scratch_database(
        "ctsrc", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch, scratch.sessions() as session:
        session.info["sessions"] = scratch.sessions  # a second connection, for the lock test
        yield session


@pytest.fixture
def repo(monkeypatch):
    """A fake repository the sync reads, and a fake test runner: a tool whose
    code holds ``FAIL`` fails its tests."""
    state = SimpleNamespace(commits={}, head="")

    async def access(_db, source):
        return sync.RepoAccess(provider=source.provider, api_base="https://api", web_base="https://web")

    monkeypatch.setattr(sync, "resolve_access", access)
    monkeypatch.setattr(sync, "make_reader", lambda *_a: FakeReader(state.commits, state.head))

    async def fake_tests(*, code, test_cases, test_code, **_kw):
        has_tests = bool(test_cases or test_code)
        status = "failed" if "FAIL" in code else ("passed" if has_tests else "no_tests")
        results = [{"kind": "case", "name": "dry", "status": status, "message": "boom" if status == "failed" else None}]
        return custom_tool_tests.summarize(
            results if has_tests else [], started=0, dry_run_only=False
        )

    monkeypatch.setattr(custom_tool_tests, "run_tool_tests", fake_tests)

    def commit(sha: str, files: dict[str, str]) -> None:
        state.commits[sha] = files
        state.head = sha

    state.commit = commit
    return state


def _tool_files(name: str, code: str = "print('ok')") -> dict[str, str]:
    return {f"tools/{name}/tool.yaml": MANIFEST.format(name=name), f"tools/{name}/main.py": code}


@pytest.mark.integration
async def test_sync_lifecycle(db, repo):
    alice = _user("user", "alice")
    db.add(alice)
    await db.flush()
    source = CustomToolSource(user_id=alice.id, name="tools", provider="github", repository="acme/tools", path="tools")
    db.add(source)
    await db.commit()

    # 1. Two tools appear.
    repo.commit("c1", {**_tool_files("whois"), **_tool_files("dns"), "README.md": "x"})
    report = await sync.run_sync(db, source, actor=alice)
    assert sorted(e["path"] for e in report.created) == ["tools/dns", "tools/whois"]
    tools = {t.source_path: t for t in (await db.execute(select(CustomTool))).scalars()}
    whois = tools["tools/whois"]
    assert whois.user_id == alice.id and whois.enabled and whois.language == "python"
    assert whois.source_commit_sha == "c1" and whois.last_test_status == "passed"
    assert source.last_sync_status == "ok" and source.last_commit_sha == "c1"

    # 2. Same files at a new commit: unchanged, nothing re-tested.
    repo.commit("c2", {**_tool_files("whois"), **_tool_files("dns")})
    report = await sync.run_sync(db, source)
    assert len(report.unchanged) == 2 and not report.created and not report.updated
    assert whois.source_commit_sha == "c2"

    # 3. A new version of whois fails its tests: rejected, the active one stays.
    repo.commit("c3", {**_tool_files("whois", "print('FAIL')"), **_tool_files("dns", "print('v2')")})
    report = await sync.run_sync(db, source)
    assert [e["path"] for e in report.rejected] == ["tools/whois"]
    assert report.rejected[0]["tests"]["failures"][0]["message"] == "boom"
    assert [e["path"] for e in report.updated] == ["tools/dns"]
    await db.refresh(whois)
    assert whois.js_function == "print('ok')" and whois.source_commit_sha == "c2"
    assert source.last_sync_status == "partial"

    # 4. dns leaves the repository: disabled, not deleted.
    repo.commit("c4", _tool_files("whois"))
    report = await sync.run_sync(db, source)
    assert [e["path"] for e in report.disabled] == ["tools/dns"]
    dns = tools["tools/dns"]
    await db.refresh(dns)
    assert dns.enabled is False

    # 5. A detached tool is left alone, and a broken manifest is reported.
    source.detached_paths = ["tools/whois"]
    await db.commit()
    repo.commit("c5", {**_tool_files("whois", "print('v3')"), "tools/bad/tool.yaml": "name: bad\n"})
    report = await sync.run_sync(db, source)
    assert report.skipped_detached == ["tools/whois"]
    assert report.errors[0]["path"] == "tools/bad"
    await db.refresh(whois)
    assert whois.js_function == "print('ok')"

    # 6. A repository that cannot be read: the source says why, nothing changes.
    async def unreadable(_db, _source):
        raise sync.SourceSyncError("GitHub found no the repository (HTTP 404).")

    sync_resolve = sync.resolve_access
    sync.resolve_access = unreadable
    try:
        assert await sync.run_sync(db, source) is None
    finally:
        sync.resolve_access = sync_resolve
    assert source.last_sync_status == "error" and "HTTP 404" in source.last_sync_error
    assert source.sync_failure_count == 1 and source.sync_next_retry_at is not None

    # 7. A sub-directory with nothing in it is an error, not "every tool was removed".
    repo.commit("c6", {"other/x.txt": "x"})
    assert await sync.run_sync(db, source) is None
    assert "sub-directory 'tools'" in source.last_sync_error
    await db.refresh(whois)
    assert whois.enabled is True


@pytest.mark.integration
async def test_a_tool_back_in_the_repository_is_enabled_again(db, repo):
    alice = _user("user", "alice")
    db.add(alice)
    await db.flush()
    source = CustomToolSource(user_id=alice.id, name="tools", provider="github", repository="acme/tools", path="tools")
    db.add(source)
    await db.commit()
    repo.commit("c1", {**_tool_files("whois"), **_tool_files("dns"), **_tool_files("ping")})
    await sync.run_sync(db, source)
    tools = {t.source_path: t for t in (await db.execute(select(CustomTool))).scalars()}
    dns, ping = tools["tools/dns"], tools["tools/ping"]
    ping.enabled = False  # by hand
    await db.commit()

    # dns and ping leave: only dns is the sync's to disable.
    repo.commit("c2", _tool_files("whois"))
    report = await sync.run_sync(db, source)
    assert [e["path"] for e in report.disabled] == ["tools/dns"]
    await db.refresh(dns)
    assert dns.enabled is False and dns.source_removed_at is not None

    # They come back unchanged (a revert): dns is enabled again, ping stays as it was set.
    repo.commit("c3", {**_tool_files("whois"), **_tool_files("dns"), **_tool_files("ping")})
    report = await sync.run_sync(db, source)
    assert [e["path"] for e in report.restored] == ["tools/dns"]
    assert sorted(e["path"] for e in report.unchanged) == ["tools/dns", "tools/ping", "tools/whois"]
    await db.refresh(dns)
    await db.refresh(ping)
    assert dns.enabled is True and dns.source_removed_at is None
    assert ping.enabled is False

    # Back with changes: enabled once the new version passes its tests...
    repo.commit("c4", _tool_files("whois"))
    await sync.run_sync(db, source)
    repo.commit("c5", {**_tool_files("whois"), **_tool_files("dns", "print('v2')")})
    report = await sync.run_sync(db, source)
    assert [e["path"] for e in report.updated] == [e["path"] for e in report.restored] == ["tools/dns"]
    await db.refresh(dns)
    assert dns.enabled is True and dns.js_function == "print('v2')"

    # ...and not when it fails them.
    repo.commit("c6", _tool_files("whois"))
    await sync.run_sync(db, source)
    repo.commit("c7", {**_tool_files("whois"), **_tool_files("dns", "print('FAIL')")})
    report = await sync.run_sync(db, source)
    assert [e["path"] for e in report.rejected] == ["tools/dns"] and not report.restored
    await db.refresh(dns)
    assert dns.enabled is False and dns.source_removed_at is not None


@pytest.mark.integration
async def test_one_sync_of_a_source_at_a_time(db, repo, monkeypatch):
    from app.core import security_options
    from app.services import agentic_gate

    async def licensed():
        return True

    monkeypatch.setattr(sources_api, "security_option", lambda _name: False)
    monkeypatch.setattr(security_options, "security_option", lambda _name: False)
    monkeypatch.setattr(agentic_gate, "is_licensed", licensed)
    alice = _user("user", "alice")
    db.add(alice)
    await db.flush()
    source = CustomToolSource(
        user_id=alice.id, name="tools", provider="github", repository="acme/tools", path="tools", sync_interval="6h"
    )
    db.add(source)
    await db.commit()
    repo.commit("c1", _tool_files("whois"))

    async with db.info["sessions"]() as other:
        # Another sync holds the source.
        await other.execute(select(CustomToolSource).where(CustomToolSource.id == source.id).with_for_update())
        with pytest.raises(sync.SourceSyncBusyError):
            await sync.run_sync(db, source)
        with pytest.raises(HTTPException) as busy:
            await sources_api.sync_source_now(str(source.id), alice, db, None)
        assert busy.value.status_code == 409
        assert await sync.process_scheduled_syncs(db) == 0  # the scheduler skips it
        await other.rollback()

    assert await sync.process_scheduled_syncs(db) == 1
    await db.refresh(source)
    assert source.last_sync_report["created"][0]["path"] == "tools/whois"
    assert len((await db.execute(select(CustomTool))).scalars().all()) == 1


@pytest.mark.integration
async def test_a_synced_tool_is_read_only_but_shareable(db, repo):
    alice = _user("user", "alice")
    db.add(alice)
    await db.flush()
    source = CustomToolSource(user_id=alice.id, name="tools", provider="github", repository="acme/tools", path="tools")
    db.add(source)
    await db.commit()
    repo.commit("c1", _tool_files("whois"))
    await sync.run_sync(db, source)
    tool = (await db.execute(select(CustomTool))).scalar_one()
    tool_id = str(tool.id)

    # The configuration form sends every field back: unchanged content passes.
    same = CustomToolUpdate(name=tool.name, js_function=tool.js_function, hide_from_chat=True)
    response = await tools_api.update_custom_tool(tool_id, same, None, alice, db)
    assert response.hide_from_chat is True
    assert response.source_id == str(source.id) and response.source_path == "tools/whois"

    for change in (
        CustomToolUpdate(js_function="print('edited')"),
        CustomToolUpdate(test_cases=[]),
        CustomToolUpdate(is_company_managed=True),
    ):
        with pytest.raises(HTTPException) as refused:
            await tools_api.update_custom_tool(tool_id, change, None, alice, db)
        assert refused.value.status_code == 403
    with pytest.raises(HTTPException) as refused:
        await tools_api.delete_custom_tool(tool_id, None, alice, db)
    assert refused.value.status_code == 403

    # Detached: editable, and the sync no longer touches it.
    detached = await tools_api.detach_custom_tool(tool_id, None, alice, db)
    assert detached.source_id is None
    await db.refresh(source)
    assert source.detached_paths == ["tools/whois"]
    edited = await tools_api.update_custom_tool(tool_id, CustomToolUpdate(js_function="print('mine')"), None, alice, db)
    assert edited.js_function == "print('mine')"
    report = await sync.run_sync(db, source)
    assert report.skipped_detached == ["tools/whois"] and not report.created


@pytest.mark.integration
async def test_a_personal_source_is_its_owners_only(db, repo, monkeypatch):
    monkeypatch.setattr(sources_api, "security_option", lambda _name: False)
    alice, bob = _user("user", "alice"), _user("user", "bob")
    db.add_all([alice, bob])
    await db.commit()

    created = await sources_api.create_source(
        sources_api.CustomToolSourceCreate(
            name="mine", provider="gitlab", repository="https://gitlab.com/grp/proj.git", path="/tools/"
        ),
        None, alice, db, None,
    )
    assert created.repository == "grp/proj" and created.path == "tools" and created.can_manage
    assert created.repository_url == "https://gitlab.com/grp/proj"

    assert [s.id for s in await sources_api.list_sources(bob, db)] == []
    with pytest.raises(HTTPException) as hidden:
        await sources_api.get_source(created.id, bob, db)
    assert hidden.value.status_code == 404
    with pytest.raises(HTTPException) as hidden:
        await sources_api.sync_source_now(created.id, bob, db, None)
    assert hidden.value.status_code == 404

    with pytest.raises(HTTPException) as refused:
        await sources_api.create_source(
            sources_api.CustomToolSourceCreate(
                name="company", provider="github", repository="acme/tools", is_company_managed=True
            ),
            None, bob, db, None,
        )
    assert refused.value.status_code == 403

    repo.commit("c1", _tool_files("whois"))
    synced = await sources_api.sync_source_now(created.id, alice, db, None)
    assert synced.report["created"][0]["path"] == "tools/whois"
    assert synced.source.tool_count == 1

    # Deleting the source keeps its tools, detached.
    await sources_api.delete_source(created.id, None, False, alice, db)
    tool = (await db.execute(select(CustomTool))).scalar_one()
    assert tool.source_id is None
