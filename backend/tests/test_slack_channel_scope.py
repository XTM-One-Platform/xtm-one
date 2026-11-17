"""A Slack channel Scope(s) is a hard boundary on the Slack tools.

Same contract as the GitHub repository scope (``test_github_repo_scope``):
a call on a conversation outside the selection is refused before any request,
listings and searches keep only the selected channels, the tools that reach
conversations outside any channel selection are withdrawn, an empty selection
allows nothing, and the selection follows the work handed to other agents.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.slack import build_tools
from app.integrations.providers.slack_scope import (
    DROPPED_WHEN_SCOPED,
    SLACK_CHANNEL_SCOPE_KEY,
    channel_scope_from_bindings,
)
from app.services.element_scope import agent_run_scope, assignment_element_scope
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall

SUPPORT = {"id": "C0SUPPORT", "name": "support"}
ALERTS = {"id": "C0ALERTS", "name": "alerts"}
SCOPE = {"C0SUPPORT": "support"}


def _tools(scope=SCOPE):
    executor = _SafeIntegrationExecutor()
    defs = build_tools("xoxp-token", executor, channel_scope=scope)
    return {d.name: d for d in defs}, executor


async def _call(executor, tool, **arguments):
    return await executor.execute_tool(ToolCall(id="c1", name=tool, arguments=arguments))


_RealAsyncClient = httpx.AsyncClient


@contextlib.contextmanager
def _slack_api(responder: Callable[[httpx.Request], httpx.Response]) -> Iterator[list[httpx.Request]]:
    """Serve the Slack Web API from *responder* and record what is sent."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return responder(request)

    class _Client(_RealAsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    with patch("httpx.AsyncClient", _Client):
        yield sent


def _ok(**payload) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, **payload})


def _body(request: httpx.Request) -> dict:
    if request.method == "GET":
        return dict(request.url.params)
    content = request.content.decode()
    try:
        return json.loads(content)
    except ValueError:
        return dict(httpx.QueryParams(content))


# ---------------------------------------------------------------------------
# What a channel scope is
# ---------------------------------------------------------------------------


def test_no_channel_binding_means_no_scope():
    assert channel_scope_from_bindings(None) is None
    assert channel_scope_from_bindings({}) is None
    assert channel_scope_from_bindings({"github:repository": [{"id": "a/b"}]}) is None


def test_a_selection_is_read_by_channel_id_and_an_unusable_one_allows_nothing():
    assert channel_scope_from_bindings({SLACK_CHANNEL_SCOPE_KEY: [SUPPORT, {"id": "c0alerts", "name": "#alerts"}]}) == {
        "C0SUPPORT": "support",
        "C0ALERTS": "alerts",
    }
    # The empty list a narrowing produces, junk entries, a non-list: nothing.
    assert channel_scope_from_bindings({SLACK_CHANNEL_SCOPE_KEY: []}) == {}
    assert channel_scope_from_bindings({SLACK_CHANNEL_SCOPE_KEY: ["C0X", {"name": "x"}, {"id": 7}]}) == {}
    assert channel_scope_from_bindings({SLACK_CHANNEL_SCOPE_KEY: "C0X"}) == {}


def test_assignments_and_handovers_narrow_the_selection_with_the_outer_entry():
    agent = {SLACK_CHANNEL_SCOPE_KEY: [SUPPORT, ALERTS]}
    assert channel_scope_from_bindings(assignment_element_scope(agent, {SLACK_CHANNEL_SCOPE_KEY: [ALERTS]})) == {
        "C0ALERTS": "alerts"
    }
    # A delegated agent scoped elsewhere is left with nothing.
    handed = {"element_scope": {SLACK_CHANNEL_SCOPE_KEY: [SUPPORT]}}
    assert channel_scope_from_bindings(agent_run_scope({SLACK_CHANNEL_SCOPE_KEY: [ALERTS]}, None, handed).enforced) == {}
    # An unscoped one inherits it; a forged name on an allowed id keeps the
    # outer entry, so a name cannot be smuggled in either.
    forged = {"id": "C0SUPPORT", "name": "general"}
    assert channel_scope_from_bindings(agent_run_scope({SLACK_CHANNEL_SCOPE_KEY: [forged]}, None, handed).enforced) == SCOPE
    assert channel_scope_from_bindings(agent_run_scope(None, None, handed).enforced) == SCOPE


# ---------------------------------------------------------------------------
# The toolbox under a scope
# ---------------------------------------------------------------------------


def test_tools_that_reach_other_conversations_are_withdrawn():
    defs, executor = _tools()
    for name in DROPPED_WHEN_SCOPED:
        assert name not in defs
        assert name not in executor.tool_names
    assert "send_slack_message" in defs


def test_without_a_scope_the_toolbox_is_unchanged():
    executor = _SafeIntegrationExecutor()
    names = {d.name for d in build_tools("xoxp-token", executor)}
    assert names >= DROPPED_WHEN_SCOPED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("send_slack_message", {"channel": "C0ALERTS", "text": "hi"}),
        ("read_slack_messages", {"channel": "#alerts"}),
        ("read_slack_thread", {"channel": "C0DM123", "thread_ts": "1.0"}),
        ("add_slack_reaction", {"channel": "C0ALERTS", "timestamp": "1.0", "emoji": "eyes"}),
        ("archive_slack_channel", {"channel": "C0ALERTS"}),
        ("invite_to_slack_channel", {"channel": "C0ALERTS", "users": "U1"}),
        ("add_slack_bookmark", {"channel": "C0ALERTS", "title": "t", "link": "https://x"}),
        ("upload_slack_file", {"channels": "C0SUPPORT,C0ALERTS", "content": "x"}),
        ("list_slack_files", {"channel": "C0ALERTS"}),
    ],
)
async def test_a_conversation_outside_the_scope_is_refused_before_any_request(tool, arguments):
    _, executor = _tools()
    with _slack_api(lambda r: _ok()) as sent:
        result = await _call(executor, tool, **arguments)
    assert result.is_error is True
    assert "outside the Scope(s)" in result.content
    assert "#support (C0SUPPORT)" in result.content
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["C0SUPPORT", "c0support", "#support", "support", "<#C0SUPPORT|support>"])
async def test_a_selected_channel_runs_however_it_is_named_and_is_sent_by_id(channel):
    _, executor = _tools()
    with _slack_api(lambda r: _ok(ts="1.1")) as sent:
        result = await _call(executor, "send_slack_message", channel=channel, text="hi")
    assert result.is_error is False
    assert _body(sent[0])["channel"] == "C0SUPPORT"


@pytest.mark.asyncio
async def test_a_name_that_is_not_a_selected_channel_is_never_looked_up():
    """``#general`` is not resolved through Slack: only a selected name counts."""
    _, executor = _tools()
    with _slack_api(lambda r: _ok()) as sent:
        result = await _call(executor, "send_slack_message", channel="#general", text="hi")
    assert result.is_error is True
    assert sent == []


def _upload_flow(request: httpx.Request) -> httpx.Response:
    """``files.upload`` was retired: the external upload flow."""
    if request.url.path == "/api/files.getUploadURLExternal":
        return _ok(upload_url="https://files.slack.com/upload/v1/X", file_id="F1")
    if request.url.path == "/api/files.completeUploadExternal":
        return _ok(files=[{"id": "F1", "title": "file.txt"}])
    return httpx.Response(200, text="OK")


@pytest.mark.asyncio
async def test_an_upload_to_selected_channels_is_sent_by_id():
    _, executor = _tools({"C0SUPPORT": "support", "C0ALERTS": "alerts"})
    with _slack_api(_upload_flow) as sent:
        result = await _call(executor, "upload_slack_file", channels="#support, C0ALERTS", content="x")
    assert result.is_error is False
    complete = next(r for r in sent if r.url.path == "/api/files.completeUploadExternal")
    assert _body(complete)["channels"] == "C0SUPPORT,C0ALERTS"


@pytest.mark.asyncio
async def test_listing_files_needs_a_selected_channel():
    _, executor = _tools()
    with _slack_api(lambda r: _ok(files=[])) as sent:
        result = await _call(executor, "list_slack_files")
    assert result.is_error is False
    assert sent[0].url.params["channel"] == "C0SUPPORT"

    _, executor = _tools({"C0SUPPORT": "support", "C0ALERTS": "alerts"})
    with _slack_api(lambda r: _ok(files=[])) as sent:
        result = await _call(executor, "list_slack_files")
    assert result.is_error is True
    assert "name one of the allowed channels" in result.content
    assert sent == []


@pytest.mark.asyncio
async def test_the_channel_listing_shows_only_the_selected_channels():
    channels = [
        {"id": "C0SUPPORT", "name": "support", "num_members": 3, "purpose": {"value": ""}},
        {"id": "C0SECRET", "name": "secret-deals", "num_members": 2, "purpose": {"value": ""}},
    ]
    _, executor = _tools({"C0SUPPORT": "support", "C0ALERTS": "alerts"})
    with _slack_api(lambda r: _ok(channels=channels)):
        result = await _call(executor, "list_slack_channels")
    assert "#support (ID: C0SUPPORT" in result.content
    assert "secret-deals" not in result.content
    assert "#alerts (ID: C0ALERTS, in scope; not in this listing)" in result.content


@pytest.mark.asyncio
async def test_searches_keep_only_the_selected_channels_and_hide_the_workspace_total():
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("search.messages"):
            return _ok(
                messages={
                    "total": 57,
                    "matches": [
                        {"channel": {"id": "C0SUPPORT", "name": "support"}, "text": "our ticket", "ts": "1"},
                        {"channel": {"id": "C0SECRET", "name": "secret-deals"}, "text": "SECRET price", "ts": "2"},
                    ],
                }
            )
        return _ok(
            files={
                "total": 57,
                "matches": [
                    {"name": "ours.txt", "channels": ["C0SUPPORT"]},
                    {"name": "theirs.txt", "channels": ["C0SECRET"], "groups": ["G0PRIVATE"]},
                ],
            }
        )

    _, executor = _tools()
    with _slack_api(responder):
        messages = await _call(executor, "search_slack_messages", query="price")
        files = await _call(executor, "search_slack_files", query="txt")
    assert "our ticket" in messages.content and "SECRET" not in messages.content
    assert "ours.txt" in files.content and "theirs.txt" not in files.content
    assert "57" not in messages.content and "57" not in files.content


@pytest.mark.asyncio
async def test_a_file_named_by_id_must_be_shared_in_a_selected_channel():
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("files.info"):
            file_id = request.url.params["file"]
            where = ["C0SUPPORT"] if file_id == "F_OURS" else ["C0SECRET"]
            return _ok(file={"id": file_id, "name": "f", "channels": where})
        return _ok()

    _, executor = _tools()
    with _slack_api(responder) as sent:
        theirs = await _call(executor, "get_slack_file_info", file_id="F_THEIRS")
        deleted = await _call(executor, "delete_slack_file", file_id="F_THEIRS")
        ours = await _call(executor, "get_slack_file_info", file_id="F_OURS")
    assert theirs.is_error and "not shared in a channel of the Scope(s)" in theirs.content
    assert deleted.is_error and "cannot be deleted" in deleted.content
    assert "C0SECRET" in deleted.content
    assert "Name: f" in ours.content
    assert not any(r.url.path.endswith("files.delete") for r in sent)


@pytest.mark.asyncio
async def test_a_file_shared_outside_the_scope_too_is_readable_but_never_deleted():
    """``files.delete`` is workspace-wide: a file shared in a selected channel
    AND an unselected one can be read, but deleting it would remove it from
    the unselected channel as well."""

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("files.info"):
            file_id = request.url.params["file"]
            where = {"F_BOTH": ["C0SUPPORT", "C0SECRET"], "F_OURS": ["C0SUPPORT", "C0ALERTS"]}[file_id]
            return _ok(file={"id": file_id, "name": "f", "channels": where})
        return _ok()

    _, executor = _tools({"C0SUPPORT": "support", "C0ALERTS": "alerts"})
    with _slack_api(responder) as sent:
        read = await _call(executor, "get_slack_file_info", file_id="F_BOTH")
        cross_shared = await _call(executor, "delete_slack_file", file_id="F_BOTH")
    assert "Name: f" in read.content
    assert cross_shared.is_error and "cannot be deleted" in cross_shared.content
    assert "C0SECRET" in cross_shared.content
    assert not any(r.url.path.endswith("files.delete") for r in sent)

    with _slack_api(responder) as sent:
        inside = await _call(executor, "delete_slack_file", file_id="F_OURS")
    assert not inside.is_error, inside.content
    deletes = [r for r in sent if r.url.path.endswith("files.delete")]
    assert len(deletes) == 1 and _body(deletes[0])["file"] == "F_OURS"


@pytest.mark.asyncio
async def test_an_empty_selection_allows_nothing():
    _, executor = _tools({})
    with _slack_api(lambda r: _ok(messages={"matches": []})) as sent:
        post = await _call(executor, "send_slack_message", channel="C0SUPPORT", text="hi")
        search = await _call(executor, "search_slack_messages", query="x")
    assert post.is_error is True and "none (no valid channel is selected)" in post.content
    assert search.is_error is True
    assert sent == []


def test_every_run_builds_the_slack_tools_under_its_scope():
    """Pinned at the source level: the builder reads the run's effective
    scope for Slack exactly as it does for GitHub."""
    source = (Path(__file__).resolve().parents[1] / "app/services/integration_tools.py").read_text(encoding="utf-8")
    assert 'if provider == "slack":' in source
    assert '_builder_kwargs["channel_scope"] = _channel_scope' in source
