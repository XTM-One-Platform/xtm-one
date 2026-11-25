"""A work item blocked on the first tag of a repository carrying a commit (#2209).

Four partner items waited on a stable xtm-one tag carrying a merge; no reference
kind could release them when the tag was cut (a ``workflow_run`` on deploy.yml is
also satisfied by the main push). Unit level: the normaliser (the kind was refused
on main), the labels, the GitHub tags answer and reader, the ``create`` delivery
parse, and the release applied by the polling fallback and the delivery.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.integrations import reference_state
from app.integrations.providers.github import reference_state as gh
from app.integrations.providers.github.pr_events import parse_github_pull_request_event
from app.services import work_item_blocked_refs as refs_mod
from app.services.work_item_blocked_refs import (
    BlockedRefError,
    named_by_tag,
    normalize_blocked_on_refs,
    read_kind,
    ref_label,
)

REPO = "xtm-one-platform/xtm-one"
SHA = "3360a61c" + "0" * 32
NOW = datetime(2026, 11, 25, 16, 0, tzinfo=UTC)


def _tag(**extra: Any) -> dict[str, Any]:
    return {"repo": REPO, "kind": "tag", "pattern": "*.*.*", "carries_sha": SHA, "release_on": "created", **extra}


# -- the normaliser ---------------------------------------------------------------


def test_a_tag_reference_is_normalised() -> None:
    refs = normalize_blocked_on_refs(
        [{"repo": "XTM-One-Platform/xtm-one", "kind": "tag", "pattern": " *.*.* ", "carries": SHA.upper()}]
    )
    assert refs == [_tag()]


def test_the_pattern_defaults_to_any_tag() -> None:
    (ref,) = normalize_blocked_on_refs([{"repo": REPO, "kind": "tag", "carries": SHA}])
    assert ref["pattern"] == "*"


def test_the_recorded_tag_survives_a_rewrite() -> None:
    ref = _tag(state="created", at="x", tag="1.261125.0", tag_commit=SHA)
    assert normalize_blocked_on_refs([ref]) == [ref]


@pytest.mark.parametrize(
    "entry",
    [
        {"repo": REPO, "kind": "tag"},
        {"repo": REPO, "kind": "tag", "carries": "zz"},
        {"repo": REPO, "kind": "tag", "carries": "other/repo@" + SHA},
        {"repo": REPO, "kind": "tag", "carries": SHA, "pattern": "a b"},
        {"repo": REPO, "kind": "tag", "carries": SHA, "release_on": "merged"},
        {"repo": REPO, "kind": "pull_request", "number": 1, "release_on": "created"},
    ],
)
def test_a_tag_reference_the_platform_cannot_match_is_refused(entry: dict[str, Any]) -> None:
    with pytest.raises(BlockedRefError):
        normalize_blocked_on_refs([entry])


def test_two_patterns_are_two_references() -> None:
    assert len(normalize_blocked_on_refs([_tag(), _tag(pattern="v*"), _tag()])) == 2


def test_the_label_names_the_pattern_and_the_commit_and_no_pull_request_read() -> None:
    assert ref_label(_tag()) == f"a {REPO} tag matching *.*.* carrying 3360a61"
    assert read_kind(_tag()) is None


# -- the GitHub reader ------------------------------------------------------------


def test_matching_tags_sorts_by_version_and_filters_by_pattern() -> None:
    data = [
        {"name": "1.9.2", "commit": {"sha": "a" * 40}},
        {"name": "1.10.0", "commit": {"sha": "b" * 40}},
        {"name": "nightly", "commit": {"sha": "c" * 40}},
        {"name": "1.10.0-rc1", "commit": {"sha": "d" * 40}},
        {"name": "bad"},
    ]
    assert [t["tag"] for t in gh.matching_tags(data, "*.*.*")] == ["1.10.0-rc1", "1.10.0", "1.9.2"]


class _Resp:
    status_code = 200

    def __init__(self, data: Any) -> None:
        self._data = data

    def json(self) -> Any:
        return self._data


def _fake(monkeypatch: pytest.MonkeyPatch, tags: list[Any], status: dict[str, str]) -> list[str]:
    asked: list[str] = []

    async def fake_request(
        _client: object, _method: str, url: str, *_a: object, **_k: object
    ) -> tuple[_Resp | None, str | None]:
        asked.append(url)
        if url.endswith("/tags"):
            return _Resp(tags), None
        if "/compare/" in url:
            head = url.split("...")[1].split("?")[0]
            return (_Resp({"status": status[head]}), None) if head in status else (None, "error")
        return None, "unexpected"

    @contextlib.asynccontextmanager
    async def fake_client(**_k: object) -> AsyncIterator[object]:
        yield object()

    monkeypatch.setattr("app.integrations.providers.github._common.safe_request", fake_request)
    monkeypatch.setattr(gh, "egress_client", fake_client)
    return asked


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "carried"), [("ahead", True), ("identical", True), ("behind", False)])
async def test_the_reader_answers_the_tag_only_when_it_carries_the_commit(
    monkeypatch: pytest.MonkeyPatch, status: str, carried: bool
) -> None:
    _fake(monkeypatch, [{"name": "1.2.0", "commit": {"sha": "b" * 40}}], {"b" * 40: status})
    found = await gh.read_tag_carrying(REPO, "*.*.*", SHA, "t")
    assert found == ({"tag": "1.2.0", "commit": "b" * 40} if carried else None)


@pytest.mark.asyncio
async def test_no_matching_tag_reads_no_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _fake(monkeypatch, [{"name": "nightly", "commit": {"sha": "b" * 40}}], {})
    assert await gh.read_tag_carrying(REPO, "*.*.*", SHA, "t") is None
    assert not any("/compare/" in u for u in asked)


def test_github_registers_its_tag_reader() -> None:
    import app.integrations.providers.github  # noqa: F401 - registers the reader

    assert "github" in reference_state.tag_readers()


# -- the delivery -----------------------------------------------------------------


def test_a_tag_creation_is_parsed_and_a_branch_creation_is_not() -> None:
    payload = {"ref": "1.261125.0", "ref_type": "tag", "repository": {"full_name": "XTM-One-Platform/xtm-one"}}
    event = parse_github_pull_request_event("create", payload)
    assert event is not None and event.kind == "tag_created" and event.repo == REPO
    assert event.extra == {"tag": "1.261125.0"}
    assert parse_github_pull_request_event("create", {**payload, "ref_type": "branch"}) is None


def test_the_delivery_names_the_references_its_tag_name_matches() -> None:
    event = parse_github_pull_request_event(
        "create", {"ref": "1.2.3", "ref_type": "tag", "repository": {"full_name": REPO}}
    )
    assert event is not None
    refs = [_tag(), _tag(pattern="v*"), _tag(repo="o/other"), _tag(state="created")]
    assert named_by_tag(refs, event) == [_tag()]


# -- the release ------------------------------------------------------------------


class _Result:
    def __init__(self, item: Any) -> None:
        self._item = item

    def one_or_none(self) -> Any:
        return (self._item.user_id, self._item.status, list(self._item.blocked_on_refs or []))

    def all(self) -> list[Any]:
        return [(self._item.id, self._item.user_id, list(self._item.blocked_on_refs or []))]

    def scalar_one_or_none(self) -> Any:
        return self._item


class _Session:
    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, _statement: object) -> _Result:
        return _Result(self.store["item"])

    async def commit(self) -> None:
        self.store["commits"] += 1


@pytest.fixture
def poll(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    item = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="blocked",
        blocked_on=None,
        blocked_on_refs=[_tag()],
        blocked_reason=None,
        needs_human=None,
        claim_notes=[],
        description="brief",
        not_before=None,
        claimed_by_agent_id=uuid.uuid4(),
        claimed_by_run_id=None,
        claimed_at=None,
        last_activity_at=None,
    )
    store: dict[str, Any] = {"item": item, "commits": 0, "reads": [], "tag": None, "published": []}

    async def token(_db: object, _provider: str, _user_id: object) -> str:
        return "tok"

    async def reader(repo: str, pattern: str, carries: str, _tok: str) -> dict[str, str] | None:
        store["reads"].append((repo, pattern, carries))
        found: dict[str, str] | None = store["tag"]
        return found

    async def published(item_id: object, note: str, tag: dict[str, Any], matched: list[Any]) -> None:
        store["published"].append((item_id, note))

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: _Session(store))
    monkeypatch.setattr(refs_mod, "reference_token", token)
    monkeypatch.setattr(refs_mod, "_publish_tag", published)
    monkeypatch.setattr(refs_mod, "_last_polled", {})
    monkeypatch.setattr(refs_mod, "_budget_skipped", set())
    monkeypatch.setattr(reference_state, "_READERS", {})
    monkeypatch.setattr(reference_state, "_WORKFLOW_RUN_READERS", {})
    monkeypatch.setattr(reference_state, "_TAG_READERS", {"github": reader})
    return store


@pytest.mark.asyncio
async def test_no_carrying_tag_keeps_it_blocked(poll: dict[str, Any]) -> None:
    counts = await refs_mod.poll_blocked_on_refs(NOW)
    assert counts == {"read": 1, "released": 0, "noted": 0}
    assert poll["reads"] == [(REPO, "*.*.*", SHA)]
    assert poll["item"].status == "blocked" and poll["commits"] == 0


@pytest.mark.asyncio
async def test_a_carrying_tag_releases_the_item_by_the_poller(poll: dict[str, Any]) -> None:
    poll["tag"] = {"tag": "1.261125.0", "commit": "b" * 40}
    counts = await refs_mod.poll_blocked_on_refs(NOW)
    item = poll["item"]
    assert counts["released"] == 1
    assert item.status == "open" and item.blocked_on_refs is None and item.claimed_by_agent_id is None
    assert item.description.startswith(f"released: a {REPO} tag matching *.*.* carrying 3360a61: tag 1.261125.0")
    assert len(poll["published"]) == 1


@pytest.mark.asyncio
async def test_a_carrying_tag_releases_the_item_at_the_write(poll: dict[str, Any]) -> None:
    poll["tag"] = {"tag": "1.261125.0", "commit": "b" * 40}
    counts = await refs_mod.check_item_tags(poll["item"].id)
    assert counts["released"] == 1 and poll["item"].status == "open"


@pytest.mark.asyncio
async def test_a_tag_delivery_releases_the_item_after_its_read_back(poll: dict[str, Any]) -> None:
    poll["tag"] = {"tag": "1.261125.0", "commit": "b" * 40}
    event = parse_github_pull_request_event(
        "create", {"ref": "1.261125.0", "ref_type": "tag", "repository": {"full_name": REPO}}
    )
    assert event is not None
    counts = await refs_mod.release_from_tag_delivery(event)
    assert counts["read"] == 1 and counts["released"] == 1


@pytest.mark.asyncio
async def test_the_read_back_decides_not_the_delivery(poll: dict[str, Any]) -> None:
    event = parse_github_pull_request_event(
        "create", {"ref": "1.261125.0", "ref_type": "tag", "repository": {"full_name": REPO}}
    )
    assert event is not None
    counts = await refs_mod.release_from_tag_delivery(event)
    assert counts["read"] == 1 and counts["released"] == 0 and poll["item"].status == "blocked"
