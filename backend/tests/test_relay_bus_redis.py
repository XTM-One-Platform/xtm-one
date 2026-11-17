"""The relay bus end to end on a real Redis.

A caller sends an ``httpx`` request through :class:`RelayTransport`; a fake
relay claims it with the same calls the WebSocket and long-poll endpoints
make (``claim_opens``, ``accept_from_relay``, ``drain_down``) and answers.
Nothing is mocked between the two but the request log, so streaming, the
credit window, cancellation, timeouts, fail-closed refusals and load
balancing across the relays of a group are exercised as they run.

Needs ``REDIS_URL`` to reach a Redis it may write to (the integration job's
service, or a throwaway container) and skips otherwise. Every key it writes
belongs to a fresh group, relay or request id and is deleted afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid

import httpx
import pytest
import redis.asyncio as redis_asyncio

from app.services import relay_transport as rt
from app.services import relays

pytestmark = pytest.mark.integration

_CHUNK = rt.DATA_CHUNK_BYTES


class _Bus:
    def __init__(self, client: redis_asyncio.Redis) -> None:
        self.client = client
        self.logs: list[dict] = []
        self.groups: list[uuid.UUID] = []
        self.relay_ids: list[uuid.UUID] = []
        self.sids: list[str] = []

    def group(self) -> uuid.UUID:
        gid = uuid.uuid4()
        self.groups.append(gid)
        return gid

    def relay(self, group_id: uuid.UUID) -> _FakeRelay:
        relay = _FakeRelay(group_id)
        self.relay_ids.append(relay.id)
        return relay

    def transport(self, group_id: uuid.UUID, **kwargs) -> rt.RelayTransport:
        kwargs.setdefault("group_name", "Paris DC")
        return rt.RelayTransport(group_id=group_id, **kwargs)

    def keys(self) -> list[str]:
        keys: list[str] = []
        for gid in self.groups:
            keys += [rt.open_stream_key(gid), rt.presence_key(gid)]
        for rid in self.relay_ids:
            keys += [rt.inflight_key(rid), rt.direct_open_key(rid)]
        for sid in self.sids:
            keys += [rt.up_key(sid), rt.down_key(sid), rt.owner_key(sid), rt.cancel_key(sid), rt.credit_key(sid)]
        return keys


class _FakeRelay:
    """A relay as the endpoints drive one: claim, answer, read what comes down."""

    def __init__(self, group_id: uuid.UUID) -> None:
        self.id = uuid.uuid4()
        self.group_id = group_id
        self.member = f"{self.id}:{uuid.uuid4().hex[:12]}"

    async def online(self) -> None:
        await rt.touch_presence(self.group_id, self.member)

    async def claim(self, limit: int = 16, *, wait: float = 5.0) -> list[rt.Frame]:
        end = time.monotonic() + wait
        while True:
            frames = await rt.claim_opens(self.id, self.group_id, limit)
            if frames or time.monotonic() >= end:
                return frames
            await asyncio.sleep(0.05)

    async def send(self, sid: str, kind: str, payload: bytes = b"", **header) -> bool:
        return await rt.accept_from_relay(self.id, rt.Frame({"type": kind, "sid": sid, **header}, payload))

    async def respond(self, sid: str, status: int = 200, headers=(), chunks=()) -> None:
        assert await self.send(sid, "response_head", status=status, headers=[list(h) for h in headers])
        for chunk in chunks:
            assert await self.send(sid, "data", chunk)
        assert await self.send(sid, "end")

    async def down(self, sids, *, until: str | None = None, wait: float = 5.0) -> list[rt.Frame]:
        end = time.monotonic() + wait
        frames: list[rt.Frame] = []
        while True:
            frames += await rt.drain_down(self.id, sids)
            if (frames and (until is None or any(f.type == until for f in frames))) or time.monotonic() >= end:
                return frames
            await asyncio.sleep(0.05)


@pytest.fixture
async def bus(monkeypatch):
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        pytest.skip("REDIS_URL is not set")
    client = redis_asyncio.Redis.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:  # pragma: no cover - environment dependent
        await client.aclose()
        pytest.skip(f"Redis unreachable at {url}: {exc}")
    state = _Bus(client)

    async def _client():
        return client

    async def _pubsub_client():
        return redis_asyncio.Redis.from_url(url, decode_responses=True)

    real_open = rt.open_relay_stream

    async def _recording_open(*args, **kwargs):
        stream = await real_open(*args, **kwargs)
        state.sids.append(stream.sid)
        return stream

    monkeypatch.setattr(rt, "redis_client", _client)
    monkeypatch.setattr(rt, "redis_pubsub_client", _pubsub_client)
    monkeypatch.setattr(rt, "open_relay_stream", _recording_open)
    monkeypatch.setattr(relays, "log_relay_request", state.logs.append)
    monkeypatch.setattr(rt.settings, "relay_queue_timeout_seconds", 5)
    try:
        yield state
    finally:
        task = rt._WAKER._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        rt._WAKER._task = None
        keys = state.keys()
        if keys:
            await client.delete(*keys)
        await client.aclose()


async def _bounded(awaitable, seconds: float = 20.0):
    return await asyncio.wait_for(awaitable, seconds)


# -- A request through the relay -------------------------------------------------


async def test_a_request_goes_through_the_relay_that_claims_it(bus):
    gid = bus.group()
    relay = bus.relay(gid)
    await relay.online()
    source_id = uuid.uuid4()

    async def serve():
        (opened,) = await relay.claim()
        await relay.respond(
            opened.sid, 201, [("Content-Type", "application/json")], [b'{"ok":', b"true}"]
        )
        return opened

    server = asyncio.create_task(serve())
    async with httpx.AsyncClient(
        transport=bus.transport(gid, source_type="integration", source_id=source_id)
    ) as client:
        response = await _bounded(
            client.post(
                "https://splunk.corp.local:8089/services/search/jobs?output_mode=json",
                content=b"search=index%3Dmain",
                headers={"Authorization": "Bearer t0ken", "Connection": "keep-alive"},
            )
        )
    opened = await _bounded(server)

    assert response.status_code == 201
    assert response.json() == {"ok": True}
    assert response.extensions["relay_id"] == str(relay.id)

    assert opened.header["kind"] == "http"
    assert opened.header["method"] == "POST"
    assert opened.header["url"] == "https://splunk.corp.local:8089/services/search/jobs?output_mode=json"
    assert opened.header["body_len"] == len(b"search=index%3Dmain")
    assert opened.payload == b"search=index%3Dmain"
    names = {name.lower() for name, _ in opened.header["headers"]}
    assert "authorization" in names
    assert not names & {"connection", "host", "content-length"}

    (entry,) = bus.logs
    assert entry["group_id"] == gid
    assert entry["relay_id"] == relay.id
    assert entry["source_type"] == "integration"
    assert entry["source_id"] == source_id
    assert entry["status_code"] == 201
    assert "error_code" not in entry
    assert (entry["host"], entry["port"], entry["path"]) == ("splunk.corp.local", 8089, "/services/search/jobs")
    assert entry["bytes_sent"] == len(b"search=index%3Dmain")
    assert entry["bytes_received"] == len(b'{"ok":true}')
    assert await rt.relay_inflight_count(relay.id) == 0


async def test_a_large_request_body_follows_the_open_down_the_request_stream(bus):
    gid = bus.group()
    relay = bus.relay(gid)
    await relay.online()
    body = os.urandom(rt.INLINE_BODY_BYTES + 300_000)

    async def serve():
        (opened,) = await relay.claim()
        rest = b""
        end = time.monotonic() + 5
        while len(opened.payload) + len(rest) < opened.header["body_len"] and time.monotonic() < end:
            for frame in await relay.down([opened.sid]):
                assert frame.type == "data"
                rest += frame.payload
        await relay.respond(opened.sid, 204)
        return opened, rest

    server = asyncio.create_task(serve())
    async with httpx.AsyncClient(transport=bus.transport(gid)) as client:
        response = await _bounded(client.put("https://tanium.corp.local/api/v2/upload", content=body))
    opened, rest = await _bounded(server)

    assert response.status_code == 204
    assert opened.header["body_len"] == len(body)
    assert len(opened.payload) == rt.INLINE_BODY_BYTES
    assert opened.payload + rest == body


async def test_frames_waiting_in_redis_are_encrypted(bus):
    gid = bus.group()
    await rt.open_relay_stream(
        gid,
        method="GET",
        url="https://splunk.corp.local/secret-path",
        headers=[["Authorization", "Bearer very-secret-token"]],
        body=b"",
        timeout_s=10,
        connect_timeout_s=None,
        queue_timeout_s=5,
    )
    entries = await bus.client.xrange(rt.open_stream_key(gid))
    assert len(entries) == 1
    raw = repr(entries)
    assert "very-secret-token" not in raw
    assert "secret-path" not in raw
    frame = rt._unseal_entry(entries[0][1])
    assert frame is not None and frame.header["url"] == "https://splunk.corp.local/secret-path"


# -- Fail closed -------------------------------------------------------------------


async def test_a_group_with_no_relay_online_fails_closed(bus):
    gid = bus.group()
    async with httpx.AsyncClient(transport=bus.transport(gid)) as client:
        with pytest.raises(rt.RelayConnectError) as caught:
            await _bounded(client.get("https://splunk.corp.local/"))
    assert caught.value.code == "no_relay_online"
    assert "Paris DC" in str(caught.value)
    assert await bus.client.xlen(rt.open_stream_key(gid)) == 0
    assert bus.logs[0]["error_code"] == "no_relay_online"


async def test_a_relay_heard_from_before_the_offline_window_is_not_online(bus):
    gid = bus.group()
    relay = bus.relay(gid)
    stale = time.time() - rt.settings.relay_offline_after_seconds - 5
    await bus.client.zadd(rt.presence_key(gid), {relay.member: stale})
    async with httpx.AsyncClient(transport=bus.transport(gid)) as client:
        with pytest.raises(rt.RelayConnectError):
            await _bounded(client.get("https://splunk.corp.local/"))
    assert await bus.client.xlen(rt.open_stream_key(gid)) == 0


async def test_a_destination_outside_the_group_allow_list_never_reaches_a_relay(bus):
    gid = bus.group()
    relay = bus.relay(gid)
    await relay.online()
    transport = bus.transport(gid, allowed_destinations=["*.corp.local:443"])
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(rt.RelayError) as caught:
            await _bounded(client.get("https://exfil.example/"))
        assert caught.value.code == "destination_denied"
        with pytest.raises(rt.RelayError):
            await _bounded(client.get("http://splunk.corp.local:8089/"))
    assert await bus.client.xlen(rt.open_stream_key(gid)) == 0


async def test_a_request_body_past_the_limit_is_refused_before_it_is_queued(bus, monkeypatch):
    gid = bus.group()
    relay = bus.relay(gid)
    await relay.online()
    monkeypatch.setattr(rt.settings, "relay_max_request_bytes", 1000)
    async with httpx.AsyncClient(transport=bus.transport(gid)) as client:
        with pytest.raises(rt.RelayError) as caught:
            await _bounded(client.post("https://splunk.corp.local/", content=b"x" * 1001))
    assert caught.value.code == "too_large"
    assert await bus.client.xlen(rt.open_stream_key(gid)) == 0


async def test_a_request_no_relay_picks_up_times_out_and_is_never_started(bus, monkeypatch):
    gid = bus.group()
    relay = bus.relay(gid)
    await relay.online()
    monkeypatch.setattr(rt.settings, "relay_queue_timeout_seconds", 1)
    async with httpx.AsyncClient(transport=bus.transport(gid)) as client:
        with pytest.raises(rt.RelayTimeoutError) as caught:
            await _bounded(client.get("https://splunk.corp.local/"))
    assert caught.value.code == "timeout"
    assert "picked the request up" in str(caught.value)
    (sid,) = bus.sids
    assert await bus.client.exists(rt.cancel_key(sid))
    assert await relay.claim(wait=0) == []
    assert not await bus.client.exists(rt.owner_key(sid))


# -- Errors, streaming and backpressure -----------------------------------------


async def test_the_relay_error_is_the_caller_error(bus):
    gid = bus.group()
    relay = bus.relay(gid)
    await relay.online()

    async def serve():
        (opened,) = await relay.claim()
        assert await relay.send(opened.sid, "error", code="connect_failed", message="connection refused")

    server = asyncio.create_task(serve())
    async with httpx.AsyncClient(transport=bus.transport(gid)) as client:
        with pytest.raises(httpx.ConnectError) as caught:
            await _bounded(client.get("https://splunk.corp.local/"))
    await _bounded(server)
    assert caught.value.code == "connect_failed"
    assert "connection refused" in str(caught.value)
    (entry,) = bus.logs
    assert entry["error_code"] == "connect_failed"
    assert entry["relay_id"] == relay.id
    assert await rt.relay_inflight_count(relay.id) == 0


async def test_the_response_streams_and_the_relay_gets_more_window_as_it_is_read(bus):
    gid = bus.group()
    relay = bus.relay(gid)
    await relay.online()
    chunk = b"z" * _CHUNK
    first_batch = 12
    second_batch = 8
    assert (first_batch + second_batch) * _CHUNK > rt.INITIAL_WINDOW_BYTES

    async def serve():
        (opened,) = await relay.claim()
        sid = opened.sid
        assert await relay.send(sid, "response_head", status=200, headers=[])
        for _ in range(first_batch):
            assert await relay.send(sid, "data", chunk)
        grants = [f for f in await relay.down([sid], until="window") if f.type == "window"]
        assert grants, "the caller granted no window"
        assert grants[0].header["increment"] >= rt.INITIAL_WINDOW_BYTES // 2
        for _ in range(second_batch):
            assert await relay.send(sid, "data", chunk)
        assert await relay.send(sid, "end")

    server = asyncio.create_task(serve())
    received = 0
    async with (
        httpx.AsyncClient(transport=bus.transport(gid)) as client,
        client.stream("GET", "https://splunk.corp.local/export") as response,
    ):
        async for part in response.aiter_raw():
            received += len(part)
    await _bounded(server)
    assert received == (first_batch + second_batch) * _CHUNK
    assert bus.logs[0]["bytes_received"] == received
    assert "error_code" not in bus.logs[0]


async def test_a_relay_sending_past_its_window_fails_the_request(bus):
    gid = bus.group()
    relay = bus.relay(gid)
    stream = await rt.open_relay_stream(
        gid,
        method="GET",
        url="https://splunk.corp.local/",
        headers=[],
        body=b"",
        timeout_s=10,
        connect_timeout_s=None,
        queue_timeout_s=5,
    )
    (opened,) = await relay.claim()
    assert await relay.send(opened.sid, "response_head", status=200, headers=[])
    in_window = rt.INITIAL_WINDOW_BYTES // _CHUNK
    for _ in range(in_window):
        assert await relay.send(opened.sid, "data", b"y" * _CHUNK)
    assert not await relay.send(opened.sid, "data", b"y" * _CHUNK)

    kinds = []
    while True:
        frame = await stream.next_frame(2)
        assert frame is not None
        kinds.append(frame.type)
        if frame.type == "error":
            assert frame.header["code"] == "protocol_error"
            assert "credit window" in frame.header["message"]
            break
    assert kinds == ["claimed", "response_head", *["data"] * in_window, "error"]
    assert stream.relay_id == str(relay.id)


async def test_a_response_past_the_limit_is_cut_and_cancelled(bus, monkeypatch):
    gid = bus.group()
    relay = bus.relay(gid)
    await relay.online()
    monkeypatch.setattr(rt.settings, "relay_max_response_bytes", 100_000)

    async def serve():
        (opened,) = await relay.claim()
        assert await relay.send(opened.sid, "response_head", status=200, headers=[])
        for _ in range(2):
            assert await relay.send(opened.sid, "data", b"q" * _CHUNK)
        return await relay.down([opened.sid], until="cancel")

    server = asyncio.create_task(serve())
    async with httpx.AsyncClient(transport=bus.transport(gid)) as client:
        with pytest.raises(rt.RelayError) as caught:
            await _bounded(client.get("https://splunk.corp.local/huge"))
    assert caught.value.code == "too_large"
    down = await _bounded(server)
    assert any(f.type == "cancel" for f in down)
    assert bus.logs[0]["error_code"] == "too_large"


async def test_closing_the_response_early_cancels_the_request_on_the_relay(bus):
    gid = bus.group()
    relay = bus.relay(gid)
    await relay.online()

    async def serve():
        (opened,) = await relay.claim()
        assert await relay.send(opened.sid, "response_head", status=200, headers=[])
        assert await relay.send(opened.sid, "data", b"first part")
        return opened

    server = asyncio.create_task(serve())
    async with (
        httpx.AsyncClient(transport=bus.transport(gid)) as client,
        client.stream("GET", "https://splunk.corp.local/stream") as response,
    ):
        assert response.status_code == 200
    opened = await _bounded(server)

    down = await relay.down([opened.sid], until="cancel")
    assert [f.type for f in down] == ["cancel"]
    assert await bus.client.exists(rt.cancel_key(opened.sid))
    assert await rt.relay_inflight_count(relay.id) == 0
    (entry,) = bus.logs
    assert entry["status_code"] == 200
    assert "error_code" not in entry


async def test_a_destination_that_stops_answering_hits_the_read_timeout(bus):
    gid = bus.group()
    relay = bus.relay(gid)
    await relay.online()

    async def serve():
        (opened,) = await relay.claim()
        assert opened.header["timeout_ms"] == 1000
        assert await relay.send(opened.sid, "response_head", status=200, headers=[])
        return await relay.down([opened.sid], until="cancel")

    server = asyncio.create_task(serve())
    async with httpx.AsyncClient(transport=bus.transport(gid), timeout=httpx.Timeout(1.0)) as client:
        with pytest.raises(rt.RelayTimeoutError) as caught:
            await _bounded(client.get("https://splunk.corp.local/slow"))
    assert caught.value.code == "timeout"
    down = await _bounded(server)
    assert any(f.type == "cancel" for f in down)


async def test_a_dropped_relay_fails_its_requests(bus):
    gid = bus.group()
    relay = bus.relay(gid)
    await relay.online()

    async def serve():
        (opened,) = await relay.claim()
        assert await rt.inflight_sids(relay.id) == [opened.sid]
        await rt.fail_requests([opened.sid], "relay_disconnected", "connection lost")

    server = asyncio.create_task(serve())
    async with httpx.AsyncClient(transport=bus.transport(gid)) as client:
        with pytest.raises(httpx.RemoteProtocolError) as caught:
            await _bounded(client.get("https://splunk.corp.local/"))
    await _bounded(server)
    assert caught.value.code == "relay_disconnected"


# -- Load balancing and targeting -------------------------------------------------


async def _queue(gid, count: int, **kwargs) -> list[rt.RelayStream]:
    return [
        await rt.open_relay_stream(
            gid,
            method="GET",
            url=f"https://splunk.corp.local/{i}",
            headers=[],
            body=b"",
            timeout_s=10,
            connect_timeout_s=None,
            queue_timeout_s=10,
            **kwargs,
        )
        for i in range(count)
    ]


async def test_the_relays_of_a_group_share_its_requests_and_never_both_get_one(bus):
    gid = bus.group()
    first, second = bus.relay(gid), bus.relay(gid)
    streams = await _queue(gid, 6)

    taken_first = await first.claim(2)
    taken_second = await second.claim(10)
    assert len(taken_first) == 2
    assert len(taken_second) == 4
    assert await first.claim(10, wait=0) == []
    sids_first = {f.sid for f in taken_first}
    sids_second = {f.sid for f in taken_second}
    assert not sids_first & sids_second
    assert sids_first | sids_second == {s.sid for s in streams}

    for stream in streams:
        claimed = await stream.next_frame(2)
        assert claimed is not None and claimed.type == "claimed"
        owner = first.id if stream.sid in sids_first else second.id
        assert stream.relay_id == str(owner)


async def test_relays_claiming_at_once_never_take_the_same_request(bus):
    gid = bus.group()
    fleet = [bus.relay(gid) for _ in range(4)]
    streams = await _queue(gid, 40)

    async def drain(relay):
        taken = []
        while batch := await rt.claim_opens(relay.id, gid, 3):
            taken += [f.sid for f in batch]
        return taken

    results = await _bounded(asyncio.gather(*(drain(r) for r in fleet)))
    claimed = [sid for taken in results for sid in taken]
    assert sorted(claimed) == sorted(s.sid for s in streams)
    assert len(set(claimed)) == len(claimed)


async def test_a_request_addressed_to_one_relay_is_claimed_by_it_alone(bus):
    gid = bus.group()
    first, second = bus.relay(gid), bus.relay(gid)
    (stream,) = await _queue(gid, 1, relay_id=second.id)

    assert await first.claim(wait=0.3) == []
    (opened,) = await second.claim()
    assert opened.sid == stream.sid


async def test_a_request_for_a_relay_that_is_not_connected_fails_closed(bus):
    gid = bus.group()
    connected, absent = bus.relay(gid), bus.relay(gid)
    await connected.online()
    async with httpx.AsyncClient(transport=bus.transport(gid, relay_id=absent.id)) as client:
        with pytest.raises(rt.RelayConnectError) as caught:
            await _bounded(client.get("https://splunk.corp.local/"))
    assert caught.value.code == "no_relay_online"
    assert await bus.client.xlen(rt.direct_open_key(absent.id)) == 0


async def test_frames_from_a_relay_that_did_not_claim_the_request_are_refused(bus):
    gid = bus.group()
    owner, intruder = bus.relay(gid), bus.relay(gid)
    (stream,) = await _queue(gid, 1)
    (opened,) = await owner.claim()

    assert not await intruder.send(opened.sid, "response_head", status=200, headers=[])
    assert not await intruder.send(opened.sid, "end")
    claimed = await stream.next_frame(1)
    assert claimed is not None and claimed.type == "claimed"
    assert await stream.next_frame(0.2) is None
