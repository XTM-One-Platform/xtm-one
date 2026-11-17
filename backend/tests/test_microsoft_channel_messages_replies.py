"""Regression tests for Teams channel message reply retrieval."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from app.integrations.providers.microsoft import build_tools
from app.tools.base import CallableToolExecutor, ToolCall


class _StubResponse:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload
        self.status_code = 200

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.mark.asyncio
async def test_list_microsoft_channel_messages_includes_thread_replies(monkeypatch):
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    calls: list[str] = []

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        calls.append(url)
        if url.endswith("/messages"):
            return (
                _StubResponse(
                    {
                        "value": [
                            {
                                "id": "root-msg-1",
                                "createdDateTime": now_iso,
                                "from": {"user": {"displayName": "Root Sender"}},
                                "body": {"content": "<p>Top-level content</p>"},
                            }
                        ]
                    }
                ),
                None,
            )
        if url.endswith("/messages/root-msg-1/replies"):
            return (
                _StubResponse(
                    {
                        "value": [
                            {
                                "id": "reply-msg-1",
                                "createdDateTime": now_iso,
                                "from": {"user": {"displayName": "Reply Sender"}},
                                "body": {"content": "<p>Threaded answer keyword</p>"},
                            }
                        ]
                    }
                ),
                None,
            )
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-1",
            name="list_microsoft_channel_messages",
            arguments={"team_id": "team-1", "channel_id": "channel-1", "hours": 24, "count": 10},
        )
    )

    assert not result.is_error
    assert "Top-level content" in result.content
    assert "Threaded answer keyword" in result.content
    assert "[reply to root-msg-1]" in result.content
    assert any(url.endswith("/messages/root-msg-1/replies") for url in calls)


@pytest.mark.asyncio
async def test_list_microsoft_channel_messages_keeps_roots_when_replies_call_fails(monkeypatch):
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        if url.endswith("/messages"):
            return (
                _StubResponse(
                    {
                        "value": [
                            {
                                "id": "root-msg-2",
                                "createdDateTime": now_iso,
                                "from": {"user": {"displayName": "Root Sender"}},
                                "body": {"content": "<p>Root still shown</p>"},
                            }
                        ]
                    }
                ),
                None,
            )
        if url.endswith("/messages/root-msg-2/replies"):
            return None, "reply fetch failed"
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-2",
            name="list_microsoft_channel_messages",
            arguments={"team_id": "team-1", "channel_id": "channel-1"},
        )
    )

    assert not result.is_error
    assert "Root still shown" in result.content


@pytest.mark.asyncio
async def test_list_microsoft_channel_messages_caps_reply_fan_out(monkeypatch):
    """Regression for the N+1-HTTP comment.

    With 25 root messages in the channel and ``max_reply_threads=3``,
    only 3 ``/replies`` requests must fire — NOT one per root.  Without
    the cap this would have triggered 25 sequential Graph API calls
    and risk throttling.
    """
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    reply_urls_called: list[str] = []
    root_count = 25

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        if url.endswith("/messages"):
            return (
                _StubResponse(
                    {
                        "value": [
                            {
                                "id": f"root-msg-{i}",
                                "createdDateTime": now_iso,
                                "from": {"user": {"displayName": f"Sender {i}"}},
                                "body": {"content": f"<p>root {i}</p>"},
                            }
                            for i in range(root_count)
                        ]
                    }
                ),
                None,
            )
        if "/replies" in url:
            reply_urls_called.append(url)
            return _StubResponse({"value": []}), None
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-cap",
            name="list_microsoft_channel_messages",
            arguments={
                "team_id": "team-1",
                "channel_id": "channel-1",
                "max_reply_threads": 3,
            },
        )
    )

    assert not result.is_error
    assert len(reply_urls_called) == 3, (
        "max_reply_threads=3 must cap the number of /replies requests "
        f"to exactly 3 even when the channel has {root_count} root "
        f"messages; observed {len(reply_urls_called)}: {reply_urls_called!r}"
    )


@pytest.mark.asyncio
async def test_list_microsoft_channel_messages_disables_replies_when_threads_zero(
    monkeypatch,
):
    """``max_reply_threads=0`` is the documented opt-out for callers
    that only care about root messages — no ``/replies`` call must
    fire.  Locks the contract so a future refactor that accidentally
    falls back to a non-zero default would fail this assertion.
    """
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    reply_calls: list[str] = []

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        if url.endswith("/messages"):
            return (
                _StubResponse(
                    {
                        "value": [
                            {
                                "id": "root-msg-only",
                                "createdDateTime": now_iso,
                                "from": {"user": {"displayName": "Solo"}},
                                "body": {"content": "<p>only root</p>"},
                            }
                        ]
                    }
                ),
                None,
            )
        if "/replies" in url:
            reply_calls.append(url)
            return _StubResponse({"value": []}), None
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-zero",
            name="list_microsoft_channel_messages",
            arguments={
                "team_id": "team-1",
                "channel_id": "channel-1",
                "max_reply_threads": 0,
            },
        )
    )

    assert not result.is_error
    assert reply_calls == [], (
        "max_reply_threads=0 must short-circuit the entire reply "
        f"fetch path; observed {reply_calls!r}"
    )


@pytest.mark.asyncio
async def test_list_microsoft_channel_messages_count_budget_is_roots_only(
    monkeypatch,
):
    """Regression for the ``count`` semantics comment.

    Before the review fix, ``count`` was a shared budget for roots +
    replies, so a noisy thread could push the response into mostly
    replies and a single root.  After the fix, ``count`` controls
    ROOT messages only — replies are tracked under their own
    ``max_replies_per_thread`` budget.

    This test passes ``count=2`` against a 5-root channel where each
    root has 3 replies and asserts:
    * Exactly 2 root messages are returned (the budget is honoured).
    * BOTH roots have their replies attached (replies don't compete
      with the root budget for slots).
    """
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        if url.endswith("/messages"):
            return (
                _StubResponse(
                    {
                        "value": [
                            {
                                "id": f"root-{i}",
                                "createdDateTime": now_iso,
                                "from": {"user": {"displayName": f"Root {i}"}},
                                "body": {"content": f"<p>root body {i}</p>"},
                            }
                            for i in range(5)
                        ]
                    }
                ),
                None,
            )
        if "/replies" in url:
            # Extract root id from URL.
            root_id = url.split("/messages/")[1].split("/replies")[0]
            return (
                _StubResponse(
                    {
                        "value": [
                            {
                                "id": f"{root_id}-reply-{i}",
                                "createdDateTime": now_iso,
                                "from": {"user": {"displayName": f"Replier {i}"}},
                                "body": {"content": f"<p>{root_id} reply {i}</p>"},
                            }
                            for i in range(3)
                        ]
                    }
                ),
                None,
            )
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-budget",
            name="list_microsoft_channel_messages",
            arguments={
                "team_id": "team-1",
                "channel_id": "channel-1",
                "count": 2,
                "max_reply_threads": 10,
                "max_replies_per_thread": 5,
            },
        )
    )

    assert not result.is_error
    body = result.content
    # Header reports the split: 2 roots + N replies (replies appear in
    # the body and the summary line).
    assert "Found 2 root message(s)" in body, (
        f"count=2 must yield exactly 2 root messages in the header; "
        f"body was {body!r}"
    )
    assert "6 reply(ies)" in body, (
        f"both selected roots should each surface 3 replies (=6 total) "
        f"because replies don't compete with the root count budget; "
        f"body was {body!r}"
    )


@pytest.mark.asyncio
async def test_list_microsoft_channel_messages_parallelises_reply_fetches(
    monkeypatch,
):
    """Reply fetches must run CONCURRENTLY (bounded by the in-module
    semaphore) — not sequentially.  Each fixtured ``/replies`` call tracks
    how many fetches are in flight at once; the assert requires observed
    overlap (max concurrent > 1) while staying within the semaphore cap.
    Deterministic — no wall-clock timing, so slow CI runners can't
    false-fail it.
    """
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    fetch_started_count = 0
    max_concurrent_observed = 0
    total_fetches = 0

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        nonlocal fetch_started_count, max_concurrent_observed, total_fetches
        if url.endswith("/messages"):
            return (
                _StubResponse(
                    {
                        "value": [
                            {
                                "id": f"root-par-{i}",
                                "createdDateTime": now_iso,
                                "from": {"user": {"displayName": f"R{i}"}},
                                "body": {"content": f"<p>r{i}</p>"},
                            }
                            for i in range(10)
                        ]
                    }
                ),
                None,
            )
        if "/replies" in url:
            fetch_started_count += 1
            total_fetches += 1
            max_concurrent_observed = max(
                max_concurrent_observed, fetch_started_count
            )
            try:
                await asyncio.sleep(0.05)
                return _StubResponse({"value": []}), None
            finally:
                fetch_started_count -= 1
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)

    result = await executor.execute_tool(
        ToolCall(
            id="call-parallel",
            name="list_microsoft_channel_messages",
            arguments={
                "team_id": "team-1",
                "channel_id": "channel-1",
                "max_reply_threads": 10,
            },
        )
    )

    assert not result.is_error
    # Sanity check: all 10 reply fetches happened.
    assert total_fetches == 10, (
        f"expected all 10 reply fetches to run; observed {total_fetches}"
    )
    # Concurrency is asserted deterministically via the observed overlap
    # instead of wall-clock time (a timing bound is flaky under full-suite
    # load on slow runners): a sequential regression that drops the
    # asyncio.gather would never overlap two fetches (peak == 1), while the
    # parallel implementation overlaps up to the semaphore cap.
    assert max_concurrent_observed >= 2, (
        f"reply fetches should run concurrently — a regression that drops "
        f"the asyncio.gather and falls back to sequential awaits would "
        f"never overlap fetches (observed peak "
        f"{max_concurrent_observed})"
    )
    # And confirm the semaphore actually capped concurrency rather
    # than letting all 10 fetches go in parallel (which would defeat
    # the purpose of the throttle-protection cap).
    assert max_concurrent_observed <= 5, (
        f"reply-fetch concurrency must be capped by the semaphore at "
        f"5 to stay below the Graph API throttle threshold; observed "
        f"a peak of {max_concurrent_observed} simultaneous fetches"
    )


# ---------------------------------------------------------------------------
# Regression tests for placeholder teamId/channelId
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "team_id,channel_id",
    [
        ("na", "na"),
        ("NA", "NA"),
        ("n/a", "n/a"),
        ("none", "none"),
        ("null", "null"),
        ("unknown", "channel-1"),
        ("", "channel-1"),
        ("team-1", ""),
        ("team-1", "placeholder"),
        # Unsubstituted template-style tokens.
        ("{{team_id}}", "channel-1"),
        ("<team_id>", "channel-1"),
        ("[team_id]", "channel-1"),
        ("&lt;team_id&gt;", "channel-1"),
        ("team-1", "{{channel_id}}"),
        ("team-1", "<channel_id>"),
    ],
)
async def test_list_microsoft_channel_messages_rejects_placeholder_ids(
    monkeypatch, team_id, channel_id
):
    """list_microsoft_channel_messages must fail fast with a clear error message
    when team_id or channel_id is a placeholder string rather than a real
    Microsoft Teams identifier (a team GUID / a "19:...@thread.tacv2" channel
    ID) — avoids burning a Graph round-trip on a guaranteed 400 BadRequest.
    """
    http_calls: list[str] = []

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        http_calls.append(url)
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-placeholder",
            name="list_microsoft_channel_messages",
            arguments={"team_id": team_id, "channel_id": channel_id},
        )
    )

    # Must not make any HTTP requests — the validation must short-circuit
    assert http_calls == [], (
        f"No HTTP calls should be made for placeholder IDs "
        f"(team_id={team_id!r}, channel_id={channel_id!r}); "
        f"observed: {http_calls}"
    )
    # The returned message must be an actionable error, not a raw Graph error
    assert "Error:" in result.content, (
        f"Expected an 'Error:' prefix in the result for placeholder IDs "
        f"(team_id={team_id!r}, channel_id={channel_id!r}); "
        f"got: {result.content!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "team_id,channel_id",
    [
        ("na", "na"),
        ("n/a", "n/a"),
        ("none", "none"),
        ("", "channel-1"),
        ("team-1", ""),
    ],
)
async def test_send_microsoft_channel_message_rejects_placeholder_ids(
    monkeypatch, team_id, channel_id
):
    """send_microsoft_channel_message must reject placeholder team/channel IDs
    without making a Graph API call."""
    http_calls: list[str] = []

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        http_calls.append(url)
        return _StubResponse({"id": "msg-1", "createdDateTime": "2026-01-01T00:00:00Z"}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-send-placeholder",
            name="send_microsoft_channel_message",
            arguments={"team_id": team_id, "channel_id": channel_id, "message": "hello"},
        )
    )

    assert http_calls == [], f"No HTTP calls should be made for placeholder IDs; observed: {http_calls}"
    assert "Error:" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "team_id,channel_id",
    [
        ("na", "na"),
        ("", "channel-1"),
        ("team-1", "n/a"),
        ("{{team_id}}", "channel-1"),
    ],
)
async def test_send_microsoft_channel_file_rejects_placeholder_ids(
    monkeypatch, team_id, channel_id
):
    """send_microsoft_channel_file addresses /teams/{id}/channels/{id}/filesFolder
    with the same IDs, so it must be guarded like the other channel tools —
    without resolving the platform file or making any Graph call."""
    http_calls: list[str] = []

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        http_calls.append(url)
        return _StubResponse({}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-file-placeholder",
            name="send_microsoft_channel_file",
            arguments={
                "team_id": team_id,
                "channel_id": channel_id,
                "file_id": "file-1",
            },
        )
    )

    assert http_calls == [], f"No HTTP calls should be made for placeholder IDs; observed: {http_calls}"
    assert "Error:" in result.content


@pytest.mark.asyncio
async def test_list_microsoft_channel_messages_passes_valid_ids(monkeypatch):
    """Valid team_id and channel_id must pass the placeholder check and reach
    the Graph API — regression guard so the fast-fail does not block real calls.
    """
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    http_calls: list[str] = []

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        http_calls.append(url)
        return (
            _StubResponse(
                {
                    "value": [
                        {
                            "id": "root-1",
                            "createdDateTime": now_iso,
                            "from": {"user": {"displayName": "Alice"}},
                            "body": {"content": "<p>hello</p>"},
                        }
                    ]
                }
            ),
            None,
        )

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-valid",
            name="list_microsoft_channel_messages",
            arguments={
                "team_id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
                "channel_id": "19:generalchannel@thread.tacv2",
            },
        )
    )

    assert http_calls, "At least one HTTP call must be made for valid IDs"
    assert "Error:" not in result.content
    assert "hello" in result.content


@pytest.mark.asyncio
async def test_update_microsoft_channel_message_patches_graph(monkeypatch):
    calls: list[tuple[str, str, dict | None]] = []

    async def fake_safe_request(_client, method, url, _provider, _op, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        return (
            _StubResponse(
                {"id": "msg-9", "lastModifiedDateTime": "2026-08-31T16:42:00Z"}
            ),
            None,
        )

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-update",
            name="update_microsoft_channel_message",
            arguments={
                "team_id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
                "channel_id": "19:generalchannel@thread.tacv2",
                "message_id": "1746034567890",
                "message": "See https://example.com",
            },
        )
    )

    # The edit reads the message first to keep its file cards.
    assert calls[0][0] == "get"
    patches = [call for call in calls if call[0] != "get"]
    assert patches, "Graph PATCH must be issued"
    method, url, payload = patches[0]
    assert method == "patch"
    assert url.endswith(
        "/teams/3f2504e0-4f89-11d3-9a0c-0305e82c3301"
        "/channels/19:generalchannel@thread.tacv2/messages/1746034567890"
    )
    assert payload is not None
    assert payload["body"]["contentType"] == "html"
    assert '<a href="https://example.com"' in payload["body"]["content"]
    assert "Channel message updated" in result.content
    assert "msg-9" in result.content


@pytest.mark.asyncio
async def test_update_microsoft_channel_reply_uses_replies_path(monkeypatch):
    calls: list[str] = []

    async def fake_safe_request(_client, method, url, _provider, _op, **_kwargs):
        if method == "patch":
            calls.append(url)
        else:
            assert method == "get"
        return _StubResponse({"id": "reply-2"}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    await executor.execute_tool(
        ToolCall(
            id="call-update-reply",
            name="update_microsoft_channel_message",
            arguments={
                "team_id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
                "channel_id": "19:generalchannel@thread.tacv2",
                "message_id": "1746034567891",
                "parent_message_id": "1746034567890",
                "message": "updated reply",
            },
        )
    )

    assert calls[0].endswith(
        "/messages/1746034567890/replies/1746034567891"
    )


@pytest.mark.asyncio
async def test_update_microsoft_chat_message_patches_graph(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def fake_safe_request(_client, method, url, _provider, _op, **_kwargs):
        if method != "get":
            calls.append((method, url))
        return _StubResponse({"id": "chat-msg-1"}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-update-chat",
            name="update_microsoft_chat_message",
            arguments={
                "chat_id": "19:1f2e3d4c5b6a79880716253443526170@thread.v2",
                "message_id": "1746034567890",
                "message": "updated chat body",
            },
        )
    )

    method, url = calls[0]
    assert method == "patch"
    assert url.endswith(
        "/chats/19:1f2e3d4c5b6a79880716253443526170@thread.v2"
        "/messages/1746034567890"
    )
    assert "Chat message updated" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message_id",
    ["", "n/a", "message_id", "{{message_id}}"],
)
async def test_update_microsoft_channel_message_rejects_placeholder_ids(
    monkeypatch, message_id
):
    http_calls: list[str] = []

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kwargs):
        http_calls.append(url)
        return _StubResponse({}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(
        ToolCall(
            id="call-update-placeholder",
            name="update_microsoft_channel_message",
            arguments={
                "team_id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
                "channel_id": "19:generalchannel@thread.tacv2",
                "message_id": message_id,
                "message": "hello",
            },
        )
    )

    assert http_calls == []
    assert "Error:" in result.content
