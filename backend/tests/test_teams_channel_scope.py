"""A Microsoft Teams channel Scope(s) is a hard boundary on the Teams tools.

Same contract as the GitHub repository scope (``test_github_repo_scope``):
a call on a channel outside the selection is refused before any request, the
listing shows only the selection, the tools that reach Teams conversations
outside any channel selection are withdrawn, an empty selection allows
nothing, and the selection follows the work handed to other agents. The rest
of the Microsoft integration (mail, calendar, files, ...) is not a Teams
channel and is left alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import build_tools
from app.integrations.providers.microsoft.teams_scope import (
    DROPPED_WHEN_SCOPED,
    TEAMS_CHANNEL_SCOPE_KEY,
    ScopedChannel,
    teams_channel_scope_from_bindings,
)
from app.services.element_scope import agent_run_scope, assignment_element_scope
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall

TEAM = "11111111-aaaa-bbbb-cccc-000000000001"
GENERAL_ID = "19:General0@thread.tacv2"
OPS_ID = "19:Ops0000@thread.tacv2"


def _element(team_id: str, channel_id: str, channel: str, team: str = "Security") -> dict:
    return {
        "id": f"{team_id}:{channel_id}",
        "name": channel,
        "group": team,
        "metadata": {"team_id": team_id, "team_name": team, "channel_id": channel_id},
    }


GENERAL = _element(TEAM, GENERAL_ID, "General")
OPS = _element(TEAM, OPS_ID, "Ops")
SCOPE = teams_channel_scope_from_bindings({TEAMS_CHANNEL_SCOPE_KEY: [GENERAL]})


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture
def graph(monkeypatch):
    """Record every Graph request the Microsoft tools send."""
    sent: list[tuple[str, str, dict]] = []

    async def fake_safe_request(_client, method, url, _provider, _op, **kwargs):
        sent.append((method, url, kwargs))
        return _Response({"value": [], "id": "m1"}), None

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)
    return sent


def _tools(scope=SCOPE):
    executor = _SafeIntegrationExecutor()
    defs = build_tools("token", executor, teams_channel_scope=scope)
    return {d.name: d for d in defs}, executor


async def _call(executor, tool, **arguments):
    return await executor.execute_tool(ToolCall(id="c1", name=tool, arguments=arguments))


# ---------------------------------------------------------------------------
# What a Teams channel scope is
# ---------------------------------------------------------------------------


def test_no_teams_binding_means_no_scope():
    assert teams_channel_scope_from_bindings(None) is None
    assert teams_channel_scope_from_bindings({}) is None
    assert teams_channel_scope_from_bindings({"slack:slack_channel": [{"id": "C1"}]}) is None


def test_a_selection_is_read_from_the_element_id_and_an_unusable_one_allows_nothing():
    expected = (ScopedChannel(TEAM, GENERAL_ID, "Security", "General"),)
    assert expected == SCOPE
    # The metadata never overrides the ID: the ID is what the picker keyed.
    forged = {**GENERAL, "metadata": {"team_id": "other-team", "channel_id": "19:secret"}}
    assert teams_channel_scope_from_bindings({TEAMS_CHANNEL_SCOPE_KEY: [forged]}) == SCOPE
    assert teams_channel_scope_from_bindings({TEAMS_CHANNEL_SCOPE_KEY: []}) == ()
    assert teams_channel_scope_from_bindings({TEAMS_CHANNEL_SCOPE_KEY: [{"id": "no-colon"}, {"id": ":x"}, "junk"]}) == ()
    assert teams_channel_scope_from_bindings({TEAMS_CHANNEL_SCOPE_KEY: "junk"}) == ()


def test_assignments_and_handovers_narrow_the_selection():
    agent = {TEAMS_CHANNEL_SCOPE_KEY: [GENERAL, OPS]}
    narrowed = assignment_element_scope(agent, {TEAMS_CHANNEL_SCOPE_KEY: [OPS]})
    assert [c.channel_id for c in teams_channel_scope_from_bindings(narrowed)] == [OPS_ID]
    handed = {"element_scope": {TEAMS_CHANNEL_SCOPE_KEY: [GENERAL]}}
    assert teams_channel_scope_from_bindings(agent_run_scope({TEAMS_CHANNEL_SCOPE_KEY: [OPS]}, None, handed).enforced) == ()
    assert teams_channel_scope_from_bindings(agent_run_scope(None, None, handed).enforced) == SCOPE


# ---------------------------------------------------------------------------
# The toolbox under a scope
# ---------------------------------------------------------------------------


def test_chats_and_channel_creation_are_withdrawn_and_the_rest_of_microsoft_stays():
    defs, executor = _tools()
    for name in DROPPED_WHEN_SCOPED:
        assert name not in defs
        assert name not in executor.tool_names
    for name in ("send_microsoft_channel_message", "list_microsoft_emails", "send_microsoft_email"):
        assert name in defs


def test_without_a_scope_the_toolbox_is_unchanged():
    executor = _SafeIntegrationExecutor()
    names = {d.name for d in build_tools("token", executor)}
    assert names >= DROPPED_WHEN_SCOPED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("list_microsoft_channel_messages", {"team_id": TEAM, "channel_id": OPS_ID}),
        ("send_microsoft_channel_message", {"team_id": "other-team", "channel_id": GENERAL_ID, "message": "hi"}),
        ("reply_microsoft_channel_message", {"team_id": TEAM, "channel_id": OPS_ID, "message_id": "1", "message": "hi"}),
        ("update_microsoft_channel_message", {"team_id": TEAM, "channel_id": OPS_ID, "message_id": "1", "message": "x"}),
        ("list_microsoft_channel_members", {"team_id": TEAM, "channel_id": OPS_ID}),
        ("download_microsoft_channel_message_image", {"team_id": TEAM, "channel_id": OPS_ID, "message_id": "1"}),
        ("get_microsoft_channel_meeting_transcript", {"team": "Security", "channel": "Ops"}),
        ("get_microsoft_channel_meeting_recordings", {"team": "Finance", "channel": "General"}),
    ],
)
async def test_a_channel_outside_the_scope_is_refused_before_any_request(graph, tool, arguments):
    _, executor = _tools()
    result = await _call(executor, tool, **arguments)
    assert result.is_error is True
    assert "outside the Scope(s)" in result.content
    assert f"Security / General (team_id: {TEAM}, channel_id: {GENERAL_ID})" in result.content
    assert graph == []


@pytest.mark.asyncio
async def test_a_selected_channel_runs_and_is_sent_with_its_own_ids(graph):
    _, executor = _tools()
    result = await _call(
        executor,
        "send_microsoft_channel_message",
        team_id=TEAM.upper(),
        channel_id=GENERAL_ID.lower(),
        message="hi",
    )
    assert result.is_error is False
    assert f"/teams/{TEAM}/channels/{GENERAL_ID}/messages" in graph[0][1]


@pytest.mark.asyncio
async def test_a_channel_meeting_is_resolved_by_name_to_the_selected_ids():
    """The meeting tools take names or IDs; the guard hands them the selected IDs."""
    from app.integrations.providers.microsoft.teams_scope import _guarded

    seen: dict[str, str] = {}

    async def handler(team: str, channel: str, count: int = 1) -> str:
        seen.update(team=team, channel=channel)
        return "ok"

    guarded = _guarded("get_microsoft_channel_meeting_transcript", handler, SCOPE)
    assert await guarded(team="security", channel="general") == "ok"
    assert seen == {"team": TEAM, "channel": GENERAL_ID}


@pytest.mark.asyncio
async def test_the_listing_is_the_selection_and_sends_nothing(graph):
    _, executor = _tools(teams_channel_scope_from_bindings({TEAMS_CHANNEL_SCOPE_KEY: [GENERAL, OPS]}))
    result = await _call(executor, "list_microsoft_teams_and_channels")
    assert "## Team: Security" in result.content
    assert f"General (ID: {GENERAL_ID})" in result.content
    assert f"Ops (ID: {OPS_ID})" in result.content
    assert graph == []


@pytest.mark.asyncio
async def test_an_empty_selection_allows_nothing(graph):
    _, executor = _tools(())
    post = await _call(executor, "send_microsoft_channel_message", team_id=TEAM, channel_id=GENERAL_ID, message="hi")
    listing = await _call(executor, "list_microsoft_teams_and_channels")
    assert post.is_error is True and "none (no valid channel is selected)" in post.content
    assert "No channel is available" in listing.content
    assert graph == []


@pytest.mark.asyncio
async def test_the_rest_of_the_microsoft_integration_is_not_guarded(graph):
    _, executor = _tools()
    result = await _call(executor, "list_microsoft_emails")
    assert result.is_error is False
    assert graph, "mail tools must still reach Graph under a Teams channel scope"


def test_every_run_builds_the_teams_tools_under_its_scope():
    source = (Path(__file__).resolve().parents[1] / "app/services/integration_tools.py").read_text(encoding="utf-8")
    assert '_builder_kwargs["teams_channel_scope"] = _teams_scope' in source
