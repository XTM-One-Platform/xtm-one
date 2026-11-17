"""A new built-in tool an agent's allowlist leaves out is told to its owner once per release (#12192).

Nothing is granted: ``builtin_allowed_tools`` keeps least privilege. The first
boot of a build stores its built-in catalog on its ``platform_builds`` row and
diffs it against the previous build's; each owner of an agent scoped to a
group that gained a function gets one bell entry naming the functions and the
agents. A second writer of the same build, and a release with no baseline,
send nothing.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import builtin_tool_release_notice as notice_mod

_FIXTURE_TOOL = "fixture_new_platform_tool"
_PREVIOUS = {"platform": ["list_agents", "get_agent", "update_agent"], "web": ["web_fetch"]}
_CURRENT = {
    "platform": ["list_agents", "get_agent", "update_agent", _FIXTURE_TOOL],
    "web": ["web_fetch"],
    "brand_new": ["x"],
}


def _agent(
    name: str, owner: str, ids: list[str], allowed: dict[str, list[str]] | None, config: dict[str, object] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(), name=name, user_id=owner, builtin_tool_ids=ids, builtin_allowed_tools=allowed, config=config
    )


def test_new_functions_diffs_existing_groups_only() -> None:
    assert notice_mod.new_functions(_PREVIOUS, _CURRENT) == {"platform": [_FIXTURE_TOOL]}
    assert notice_mod.new_functions(None, _CURRENT) == {}  # no baseline, nothing to tell
    assert notice_mod.new_functions(_CURRENT, _CURRENT) == {}


def test_only_agents_scoped_to_the_group_are_left_out() -> None:
    owner = str(uuid.uuid4())
    scoped = _agent("Scoped", owner, ["platform"], {"platform": ["list_agents", "get_agent"]})
    unscoped = _agent("Unscoped", owner, ["platform"], None)
    other_group = _agent("Web only", owner, ["web", "platform"], {"web": ["web_fetch"]})
    granted = _agent("Granted", owner, ["platform"], {"platform": ["list_agents", _FIXTURE_TOOL]})
    no_group = _agent("No group", owner, ["web"], {"platform": ["list_agents"]})
    no_owner = _agent("No owner", "", ["platform"], {"platform": ["list_agents"]})
    added = notice_mod.new_functions(_PREVIOUS, _CURRENT)
    out = notice_mod.agents_left_out(added, [scoped, unscoped, other_group, granted, no_group, no_owner])
    assert [(a.name, a.groups) for a in out] == [("Scoped", {"platform": [_FIXTURE_TOOL]})]


def test_a_dismissed_function_is_never_named_again_for_that_agent() -> None:
    """#12245: a later release that still ships it does not tell the owner again; a newer function does."""
    owner = str(uuid.uuid4())
    config = notice_mod.with_dismissed(None, [f"platform.{_FIXTURE_TOOL}"])
    dismissed = _agent("Dismissed", owner, ["platform"], {"platform": ["list_agents"]}, config)
    other = _agent("Other", owner, ["platform"], {"platform": ["list_agents"]})
    out = notice_mod.agents_left_out({"platform": [_FIXTURE_TOOL]}, [dismissed, other])
    assert [a.name for a in out] == ["Other"]
    later = notice_mod.agents_left_out({"platform": [_FIXTURE_TOOL, "newer_tool"]}, [dismissed])
    assert [(a.name, a.groups) for a in later] == [("Dismissed", {"platform": ["newer_tool"]})]


def test_dismissed_names_are_kept_deduplicated_and_bounded() -> None:
    config = notice_mod.with_dismissed({"workset_chat_scope": "conversation"}, ["platform.a", "platform.a"])
    assert config == {"workset_chat_scope": "conversation", notice_mod.DISMISSED_CONFIG_KEY: ["platform.a"]}
    again = notice_mod.with_dismissed(config, ["platform.b", "platform.a"])
    assert notice_mod.dismissed_functions(again) == ["platform.a", "platform.b"]
    assert notice_mod.dismissed_functions({notice_mod.DISMISSED_CONFIG_KEY: "x"}) == []
    assert notice_mod.dismissed_functions(None) == []
    many = notice_mod.with_dismissed(None, [f"g.f{i}" for i in range(notice_mod.MAX_DISMISSED + 5)])
    kept = notice_mod.dismissed_functions(many)
    assert len(kept) == notice_mod.MAX_DISMISSED and kept[-1] == f"g.f{notice_mod.MAX_DISMISSED + 4}"


def test_a_notice_is_resolved_once_every_named_function_is_added_or_dismissed() -> None:
    owner = str(uuid.uuid4())
    one = _agent("One", owner, ["platform"], {"platform": ["list_agents"]})
    two = _agent("Two", owner, ["platform"], {"platform": ["list_agents"]})
    grouped = notice_mod.by_owner(notice_mod.agents_left_out({"platform": [_FIXTURE_TOOL, "b"]}, [one, two]))
    _title, _summary, extra = notice_mod.notice("1.2.3", grouped[owner])
    rows = {str(one.id): one, str(two.id): two}
    assert not notice_mod.notice_resolved(extra, rows)
    # One dismisses one function, adds the other; Two is still left out.
    one.config = notice_mod.with_dismissed(None, [f"platform.{_FIXTURE_TOOL}"])
    one.builtin_allowed_tools = {"platform": ["list_agents", "b"]}
    assert not notice_mod.notice_resolved(extra, rows)
    two.config = notice_mod.with_dismissed(None, [f"platform.{_FIXTURE_TOOL}", "platform.b"])
    assert notice_mod.notice_resolved(extra, rows)
    # An agent deleted since, or unscoped since, has nothing left to act on.
    assert notice_mod.notice_resolved(extra, {})
    one.config, two.config = None, None
    one.builtin_allowed_tools, two.builtin_allowed_tools = None, None
    assert notice_mod.notice_resolved(extra, rows)


class _Result:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def all(self) -> list[SimpleNamespace]:
        return self._rows


class _ResolveDb:
    """Answers the notifications query, then the agents query."""

    def __init__(self, notices: list[SimpleNamespace], agents: list[SimpleNamespace]) -> None:
        self._answers = [notices, agents]

    async def execute(self, _statement: object) -> _Result:
        return _Result(self._answers.pop(0))


@pytest.mark.asyncio
async def test_resolve_notices_marks_read_only_the_resolved_notices_about_the_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import notification_feed as feed

    owner = uuid.uuid4()
    agent = _agent("A", str(owner), ["platform"], {"platform": ["list_agents"]})
    grouped = notice_mod.by_owner(notice_mod.agents_left_out({"platform": [_FIXTURE_TOOL]}, [agent]))
    _title, _summary, extra = notice_mod.notice("1.2.3", grouped[str(owner)])
    resolved_id, other_id = uuid.uuid4(), uuid.uuid4()
    notices = [
        SimpleNamespace(id=resolved_id, extra_data=extra),
        SimpleNamespace(id=other_id, extra_data={"notification_source": "agent"}),
    ]
    agent.config = notice_mod.with_dismissed(None, [f"platform.{_FIXTURE_TOOL}"])
    saved: list[list[str]] = []

    async def load(_db: object, _user: object) -> tuple[None, list[str]]:
        return None, ["earlier"]

    async def save(_db: object, _user: object, *, read_at: object, dismissed: list[str]) -> None:
        saved.append(dismissed)

    monkeypatch.setattr(feed, "load_read_state", load)
    monkeypatch.setattr(feed, "save_read_state", save)
    out = await notice_mod.resolve_notices(_ResolveDb(notices, [agent]), owner, str(agent.id))
    assert out == [str(resolved_id)]
    assert saved == [["earlier", str(resolved_id)]]

    # Still left out: nothing is marked read.
    agent.config = None
    saved.clear()
    out = await notice_mod.resolve_notices(_ResolveDb(notices, [agent]), owner, str(agent.id))
    assert out == [] and saved == []


def test_one_notice_per_owner_names_tools_and_agents() -> None:
    owner = str(uuid.uuid4())
    agents = [
        _agent("Beta", owner, ["platform"], {"platform": ["list_agents"]}),
        _agent("alpha", owner, ["platform"], {"platform": ["get_agent"]}),
    ]
    grouped = notice_mod.by_owner(notice_mod.agents_left_out({"platform": [_FIXTURE_TOOL]}, agents))
    assert list(grouped) == [owner]
    title, summary, extra = notice_mod.notice("1.2.3 (abcdef0)", grouped[owner])
    assert title == "1 new built-in tool is outside the allowlist of 2 of your agents"
    assert f"platform.{_FIXTURE_TOOL}" in summary and "'alpha', 'Beta'" in summary
    assert "once per release" in summary
    assert "builtin_allowed_tools" not in summary and "those agents" not in summary
    assert "Agents 'alpha', 'Beta' are limited to some tools of these groups, so they do not get it." in summary
    assert extra["notification_source"] == notice_mod.NOTIFICATION_SOURCE
    assert [a["name"] for a in extra["agents"]] == ["alpha", "Beta"]
    first = grouped[owner][0]
    assert extra["path"] == f"/agents/{first.agent_id}/config/tooling?added=platform.{_FIXTURE_TOOL}"


def test_tooling_path_names_each_function_within_the_path_limit() -> None:
    agent = notice_mod.AgentLeftOut(str(uuid.uuid4()), "a", "o", {"web": ["w1"], "platform": ["p1", "p2"]})
    path = notice_mod.tooling_path(agent)
    assert path == f"/agents/{agent.agent_id}/config/tooling?added=platform.p1,platform.p2,web.w1"
    many = notice_mod.AgentLeftOut(agent.agent_id, "a", "o", {"platform": [f"fn_{i:03d}_x" for i in range(60)]})
    long_path = notice_mod.tooling_path(many)
    assert len(long_path) <= 300 and long_path.startswith(
        f"/agents/{agent.agent_id}/config/tooling?added=platform.fn_000_x"
    )
    from app.core.internal_paths import is_internal_path

    assert is_internal_path(path) and is_internal_path(long_path)


def test_the_bell_shows_the_notice_and_opens_the_tooling_section() -> None:
    from datetime import UTC, datetime

    from app.services import notification_feed as feed

    owner = str(uuid.uuid4())
    grouped = notice_mod.by_owner(
        notice_mod.agents_left_out(
            {"platform": [_FIXTURE_TOOL]}, [_agent("a", owner, ["platform"], {"platform": ["get_agent"]})]
        )
    )
    title, summary, extra = notice_mod.notice("1.2.3", grouped[owner])
    assert "builtin_allowed_tools" not in summary
    assert "Agent 'a' is limited to some tools of this group, so it does not get it." in summary
    assert "Add it from the agent page if it needs it." in summary
    activity = SimpleNamespace(
        id=uuid.uuid4(),
        activity_type="notification",
        agent_name=notice_mod.SOURCE_LABEL,
        title=title,
        summary=summary,
        extra_data=extra,
        created_at=datetime.now(UTC),
    )
    item = feed.present_activity(activity, flows_enabled=False)
    assert item is not None
    assert item.kind == "builtin_tools_outside_allowlist"
    assert item.kind in feed.NOTIFICATION_KINDS
    assert item.route == extra["path"]
    activity.extra_data = {**extra, "path": "https://evil.example/x"}
    unsafe = feed.present_activity(activity, flows_enabled=False)
    assert unsafe is not None and unsafe.route == "/agents"


class _Session:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


@pytest.mark.asyncio
@pytest.mark.parametrize("wrote", [True, False])
async def test_release_notifies_each_owner_once_and_only_the_snapshot_writer(
    monkeypatch: pytest.MonkeyPatch, wrote: bool
) -> None:
    owner = str(uuid.uuid4())
    agents = [
        _agent("One", owner, ["platform"], {"platform": ["list_agents"]}),
        _agent("Two", owner, ["platform"], {"platform": ["get_agent"]}),
    ]
    session = _Session()
    sent: list[tuple[str, str]] = []

    async def store(_db: object, _version: str, _sha: str, _catalog: object) -> bool:
        return wrote

    async def previous(_db: object, version: str, sha: str) -> dict[str, list[str]]:
        assert (version, sha) == ("1.0.0", "aaaaaaa")
        return _PREVIOUS

    async def scoped(_db: object) -> list[SimpleNamespace]:
        return agents

    async def notify(_db: object, user_id: str, *, title: str, **_kwargs: object) -> None:
        sent.append((user_id, title))

    import app.core.database as database
    import app.services.notify as notify_module

    monkeypatch.setattr(notice_mod, "_catalog", lambda: _CURRENT)
    monkeypatch.setattr(notice_mod, "_store_snapshot", store)
    monkeypatch.setattr(notice_mod, "_previous_snapshot", previous)
    monkeypatch.setattr(notice_mod, "_scoped_agents", scoped)
    monkeypatch.setattr(database, "async_session", lambda: session)
    monkeypatch.setattr(notify_module, "notify_web_user", notify)

    release = {
        "version": "1.1.0",
        "git_sha": "bbbbbbb",
        "previous_version": "1.0.0",
        "previous_git_sha": "aaaaaaa",
        "release": "1.1.0 (bbbbbbb)",
    }
    count = await notice_mod.on_release_deployed(release)
    if wrote:
        assert count == 1 and len(sent) == 1 and sent[0][0] == owner and session.committed
    else:
        assert count == 0 and sent == [] and session.rolled_back


@pytest.mark.asyncio
async def test_release_notice_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> dict[str, list[str]]:
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(notice_mod, "_catalog", boom)
    assert await notice_mod.on_release_deployed({"version": "1.0.0"}) == 0
