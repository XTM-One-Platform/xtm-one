"""Regression tests for list_microsoft_teams_and_channels.

Covers:
  1. Session-level cache — a second call within the same build_tools()
     session returns the cached result without issuing any new Graph API
     requests (including the "no teams" case).
  2. Concurrent channels fetch — channel lookups for all teams are
     dispatched in parallel so a 429 Retry-After delay on one team does
     not stall the rest.
  3. Cancellation safety — CancelledError raised inside a channel fetch
     propagates instead of being downgraded to a per-team placeholder.
  4. Single-flight — concurrent duplicate tool calls share one Graph
     batch instead of both missing the cache.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.integrations.providers.microsoft import build_tools
from app.tools.base import CallableToolExecutor, ToolCall


class _StubResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def _make_teams(*names: str) -> list[dict]:
    return [{"id": f"team-{i}", "displayName": name} for i, name in enumerate(names, 1)]


def _make_channels(team_idx: int) -> list[dict]:
    return [
        {
            "id": f"chan-{team_idx}-1",
            "displayName": f"General-{team_idx}",
            "membershipType": "standard",
        }
    ]


# ---------------------------------------------------------------------------
# Test 1 – caching: second call must not re-issue Graph requests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_teams_and_channels_cached_on_second_call(monkeypatch):
    """Calling the tool twice must return identical output and only issue
    Graph requests on the FIRST call."""
    call_log: list[str] = []

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kw):
        call_log.append(url)
        if "/joinedTeams" in url:
            return _StubResponse({"value": _make_teams("Alpha")}), None
        if "/channels" in url:
            return _StubResponse({"value": _make_channels(1)}), None
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)

    result1 = await executor.execute_tool(
        ToolCall(id="c1", name="list_microsoft_teams_and_channels", arguments={})
    )
    calls_after_first = len(call_log)

    result2 = await executor.execute_tool(
        ToolCall(id="c2", name="list_microsoft_teams_and_channels", arguments={})
    )
    calls_after_second = len(call_log)

    assert not result1.is_error
    assert not result2.is_error
    assert result1.content == result2.content, "Second call must return cached content"
    assert calls_after_second == calls_after_first, (
        "No additional Graph calls should be made for the cached second invocation"
    )
    assert "Alpha" in result1.content
    assert "General-1" in result1.content


# ---------------------------------------------------------------------------
# Test 2 – concurrent fetch: all teams' channels are collected; ordering
#          follows teams list order regardless of completion order.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_teams_and_channels_fetches_all_teams(monkeypatch):
    """Channels for all teams must appear in the output even when fetched
    concurrently."""
    teams = _make_teams("Alpha", "Beta", "Gamma")

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kw):
        if "/joinedTeams" in url:
            return _StubResponse({"value": teams}), None
        # Identify team index from URL path: /teams/team-N/channels
        for i in range(1, len(teams) + 1):
            if f"/teams/team-{i}/channels" in url:
                return _StubResponse({"value": _make_channels(i)}), None
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)

    result = await executor.execute_tool(
        ToolCall(id="c1", name="list_microsoft_teams_and_channels", arguments={})
    )

    assert not result.is_error
    for team in teams:
        assert team["displayName"] in result.content, (
            f"Expected team {team['displayName']} in output"
        )
    # All three channel lines must be present
    for i in range(1, len(teams) + 1):
        assert f"General-{i}" in result.content


# ---------------------------------------------------------------------------
# Test 3 – partial failure: a 429 / error on one team's channels call must
#          not suppress the other teams (error-tolerant gather).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_teams_and_channels_partial_channel_error(monkeypatch):
    """If one team's /channels call fails the other teams are still listed."""
    teams = _make_teams("Alpha", "Beta")

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kw):
        if "/joinedTeams" in url:
            return _StubResponse({"value": teams}), None
        if "/teams/team-1/channels" in url:
            # Simulate a transient error (safe_request returns error string)
            return None, "Microsoft rate limit exceeded after retries"
        if "/teams/team-2/channels" in url:
            return _StubResponse({"value": _make_channels(2)}), None
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)

    result = await executor.execute_tool(
        ToolCall(id="c1", name="list_microsoft_teams_and_channels", arguments={})
    )

    assert not result.is_error
    # Alpha's channels failed — placeholder must appear
    assert "Alpha" in result.content
    assert "Could not list channels" in result.content
    # Beta's channels must still be present
    assert "Beta" in result.content
    assert "General-2" in result.content


# ---------------------------------------------------------------------------
# Test 4 – concurrent speedup: channel fetches are dispatched in parallel
#          (verified by checking that asyncio.gather is called rather than
#          sequential awaits by observing overlapping in-flight calls).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_teams_and_channels_dispatches_concurrently(monkeypatch):
    """Channel fetches for multiple teams must be concurrent, not sequential.

    We verify concurrency by tracking the maximum number of in-flight
    /channels requests at any single point: if fetches were sequential the
    peak would be 1; concurrent dispatch should show a peak > 1 (bounded
    only by the semaphore).
    """
    teams = _make_teams("T1", "T2", "T3")
    in_flight: list[int] = []
    current: list[int] = [0]
    gate = asyncio.Event()

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kw):
        if "/joinedTeams" in url:
            return _StubResponse({"value": teams}), None
        if "/channels" in url:
            current[0] += 1
            in_flight.append(current[0])
            # All coroutines park here until the last one arrives,
            # so concurrent dispatch means all three increment
            # ``current`` before any of them return.
            if current[0] >= len(teams):
                gate.set()
            await asyncio.wait_for(asyncio.shield(gate.wait()), timeout=5.0)
            current[0] -= 1
            # Extract team index for response
            for i in range(1, len(teams) + 1):
                if f"team-{i}" in url:
                    return _StubResponse({"value": _make_channels(i)}), None
            return _StubResponse({"value": []}), None
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)

    result = await executor.execute_tool(
        ToolCall(id="c1", name="list_microsoft_teams_and_channels", arguments={})
    )

    assert not result.is_error
    assert max(in_flight) > 1, (
        f"Expected concurrent in-flight /channels calls > 1, got peak={max(in_flight)}. "
        "Channel fetches are not running concurrently."
    )


# ---------------------------------------------------------------------------
# Test 5 – cache invalidation: creating a channel must drop the session
#          cache so the next listing reflects the new channel.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_channel_invalidates_teams_channels_cache(monkeypatch):
    """create_microsoft_channel must invalidate the cached listing."""
    channels_by_team: dict[str, list[dict]] = {"team-1": _make_channels(1)}
    call_log: list[str] = []

    async def fake_safe_request(_client, method, url, _provider, _op, **_kw):
        call_log.append(url)
        if "/joinedTeams" in url:
            return _StubResponse({"value": _make_teams("Alpha")}), None
        if method == "post" and url.endswith("/teams/team-1/channels"):
            new_ch = {
                "id": "chan-1-new",
                "displayName": "Fresh",
                "membershipType": "standard",
            }
            channels_by_team["team-1"] = channels_by_team["team-1"] + [new_ch]
            return _StubResponse(new_ch), None
        if "/teams/team-1/channels" in url:
            return _StubResponse({"value": channels_by_team["team-1"]}), None
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)

    first = await executor.execute_tool(
        ToolCall(id="c1", name="list_microsoft_teams_and_channels", arguments={})
    )
    assert not first.is_error
    assert "Fresh" not in first.content

    created = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="create_microsoft_channel",
            arguments={"team_id": "team-1", "display_name": "Fresh"},
        )
    )
    assert not created.is_error

    second = await executor.execute_tool(
        ToolCall(id="c3", name="list_microsoft_teams_and_channels", arguments={})
    )
    assert not second.is_error
    assert "Fresh" in second.content, (
        "Cache must be invalidated after channel creation so the new channel appears"
    )


# ---------------------------------------------------------------------------
# Test 6 – "no teams" result is cached too: repeat calls must not keep
#          hitting /me/joinedTeams (the tool contract promises cached
#          repeat calls for the whole session).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_teams_and_channels_caches_no_teams_result(monkeypatch):
    """The 'no teams' sentinel must be cached like any other listing."""
    call_log: list[str] = []

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kw):
        call_log.append(url)
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)

    first = await executor.execute_tool(
        ToolCall(id="c1", name="list_microsoft_teams_and_channels", arguments={})
    )
    calls_after_first = len(call_log)
    second = await executor.execute_tool(
        ToolCall(id="c2", name="list_microsoft_teams_and_channels", arguments={})
    )

    assert not first.is_error
    assert not second.is_error
    assert "No Microsoft Teams teams found" in first.content
    assert second.content == first.content
    assert len(call_log) == calls_after_first, (
        "The 'no teams' result must be served from the cache on repeat calls"
    )


# ---------------------------------------------------------------------------
# Test 7 – transient top-level failure is NOT cached: after the joinedTeams
#          call errors, a retry must hit Graph again and can succeed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_teams_and_channels_does_not_cache_error(monkeypatch):
    """An error result from the joinedTeams call must stay retryable."""
    attempts: list[str] = []

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kw):
        if "/joinedTeams" in url:
            attempts.append(url)
            if len(attempts) == 1:
                return None, "Microsoft rate limit exceeded after retries"
            return _StubResponse({"value": _make_teams("Alpha")}), None
        if "/channels" in url:
            return _StubResponse({"value": _make_channels(1)}), None
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)

    first = await executor.execute_tool(
        ToolCall(id="c1", name="list_microsoft_teams_and_channels", arguments={})
    )
    assert "rate limit" in first.content

    second = await executor.execute_tool(
        ToolCall(id="c2", name="list_microsoft_teams_and_channels", arguments={})
    )
    assert not second.is_error
    assert "Alpha" in second.content, "Retry after a transient error must succeed"
    assert len(attempts) == 2


# ---------------------------------------------------------------------------
# Test 8 – cancellation safety: CancelledError inside a channel fetch must
#          propagate, not be recorded as a "(Could not list channels)" row.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_teams_and_channels_propagates_cancellation(monkeypatch):
    """CancelledError from a channel fetch must not be swallowed."""
    teams = _make_teams("Alpha", "Beta")

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kw):
        if "/joinedTeams" in url:
            return _StubResponse({"value": teams}), None
        if "/teams/team-1/channels" in url:
            raise asyncio.CancelledError()
        return _StubResponse({"value": _make_channels(2)}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)

    with pytest.raises(asyncio.CancelledError):
        await executor.execute_tool(
            ToolCall(id="c1", name="list_microsoft_teams_and_channels", arguments={})
        )


# ---------------------------------------------------------------------------
# Test 9 – single-flight: two concurrent invocations must share ONE Graph
#          batch (the second waits on the lock and is served from cache).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_teams_and_channels_single_flight_concurrent_calls(monkeypatch):
    """Concurrent duplicate calls must not both issue the full N+1 batch."""
    joined_calls: list[str] = []
    first_call_started = asyncio.Event()
    release = asyncio.Event()

    async def fake_safe_request(_client, _method, url, _provider, _op, **_kw):
        if "/joinedTeams" in url:
            joined_calls.append(url)
            first_call_started.set()
            # Park until both tool invocations are in flight so the
            # second one demonstrably overlaps with the first.
            await asyncio.wait_for(release.wait(), timeout=5.0)
            return _StubResponse({"value": _make_teams("Alpha")}), None
        if "/channels" in url:
            return _StubResponse({"value": _make_channels(1)}), None
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)

    task1 = asyncio.create_task(
        executor.execute_tool(
            ToolCall(id="c1", name="list_microsoft_teams_and_channels", arguments={})
        )
    )
    task2 = asyncio.create_task(
        executor.execute_tool(
            ToolCall(id="c2", name="list_microsoft_teams_and_channels", arguments={})
        )
    )
    # Deterministically wait until task1 is parked inside its joinedTeams
    # request (holding the single-flight lock), then yield so task2 reaches
    # the lock wait, and only then release the Graph response.
    await asyncio.wait_for(first_call_started.wait(), timeout=5.0)
    for _ in range(10):
        await asyncio.sleep(0)
    release.set()
    result1, result2 = await asyncio.gather(task1, task2)

    assert not result1.is_error
    assert not result2.is_error
    assert result1.content == result2.content
    assert len(joined_calls) == 1, (
        "Concurrent duplicate calls must be single-flighted through the lock"
    )


# ---------------------------------------------------------------------------
# Test 10 – invalidation wins over an in-flight listing: a channel created
#           while a listing is running must not be masked by that listing's
#           (stale) cache write.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_channel_during_inflight_listing_prevents_stale_cache(
    monkeypatch,
):
    """A listing that started before a channel creation must not cache its
    stale snapshot over the invalidation."""
    channels_by_team: dict[str, list[dict]] = {"team-1": _make_channels(1)}
    listing_started = asyncio.Event()
    release_listing = asyncio.Event()
    first_joined_call = [True]

    async def fake_safe_request(_client, method, url, _provider, _op, **_kw):
        if "/joinedTeams" in url:
            if first_joined_call[0]:
                first_joined_call[0] = False
                listing_started.set()
                # Park the FIRST listing so the channel creation below
                # happens while it is still in flight.
                await asyncio.wait_for(release_listing.wait(), timeout=5.0)
            return _StubResponse({"value": _make_teams("Alpha")}), None
        if method == "post" and url.endswith("/teams/team-1/channels"):
            new_ch = {
                "id": "chan-1-new",
                "displayName": "Fresh",
                "membershipType": "standard",
            }
            channels_by_team["team-1"] = channels_by_team["team-1"] + [new_ch]
            return _StubResponse(new_ch), None
        if "/teams/team-1/channels" in url:
            return _StubResponse({"value": channels_by_team["team-1"]}), None
        return _StubResponse({"value": []}), None

    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", fake_safe_request)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)

    inflight = asyncio.create_task(
        executor.execute_tool(
            ToolCall(id="c1", name="list_microsoft_teams_and_channels", arguments={})
        )
    )
    await asyncio.wait_for(listing_started.wait(), timeout=5.0)

    created = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="create_microsoft_channel",
            arguments={"team_id": "team-1", "display_name": "Fresh"},
        )
    )
    assert not created.is_error

    release_listing.set()
    stale = await inflight
    assert not stale.is_error

    # The stale in-flight listing must NOT have been cached: the next call
    # re-fetches and reflects the newly created channel.
    fresh = await executor.execute_tool(
        ToolCall(id="c3", name="list_microsoft_teams_and_channels", arguments={})
    )
    assert not fresh.is_error
    assert "Fresh" in fresh.content, (
        "In-flight listing must not overwrite the create-channel invalidation"
    )
