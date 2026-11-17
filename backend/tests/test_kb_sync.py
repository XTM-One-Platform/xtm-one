import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.utils import format_exception_message
from app.services import kb_sync


class _AsyncNullContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Result:
    def __init__(self, rowcount: int = 0):
        self.rowcount = rowcount


def _extract_batch_ids(statement) -> list[str]:
    for value in statement.compile().params.values():
        if isinstance(value, list):
            return value
    return []


def test_is_retryable_embedding_delete_error_detects_deadlocks():
    err = Exception("driver error")
    err.orig = SimpleNamespace(pgcode="40P01")
    assert kb_sync._is_retryable_embedding_delete_error(err)
    assert kb_sync._is_retryable_embedding_delete_error(Exception("deadlock detected"))
    assert not kb_sync._is_retryable_embedding_delete_error(Exception("validation failed"))


@pytest.mark.asyncio
async def test_delete_embeddings_for_source_ids_batches_and_sorts():
    kb_id = uuid.uuid4()
    source_ids = [f"doc-{i:03d}" for i in range(120, -1, -1)] + ["doc-010", "doc-020"]
    ordered = sorted(set(source_ids))

    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_Result(), _Result(1), _Result(2), _Result(3)]),
        begin_nested=_AsyncNullContext,
    )

    deleted = await kb_sync._delete_embeddings_for_source_ids(db, kb_id, source_ids)

    assert deleted == 6
    assert db.execute.await_count == 4  # advisory lock + 3 delete batches

    lock_call = db.execute.await_args_list[0]
    assert "pg_advisory_xact_lock" in str(lock_call.args[0])
    assert lock_call.args[1]["kb_key"] == str(kb_id)

    batch_calls = db.execute.await_args_list[1:]
    assert len(batch_calls) == 3
    assert _extract_batch_ids(batch_calls[0].args[0]) == ordered[:50]
    assert _extract_batch_ids(batch_calls[1].args[0]) == ordered[50:100]
    assert _extract_batch_ids(batch_calls[2].args[0]) == ordered[100:]
    assert all("embeddings.knowledge_base_id" in str(c.args[0]) for c in batch_calls)


@pytest.mark.asyncio
async def test_delete_embeddings_for_source_ids_retries_deadlock(monkeypatch):
    kb_id = uuid.uuid4()
    sleep = AsyncMock()
    monkeypatch.setattr(kb_sync.asyncio, "sleep", sleep)

    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[_Result(), Exception("deadlock detected"), _Result(2)]
        ),
        begin_nested=_AsyncNullContext,
    )

    deleted = await kb_sync._delete_embeddings_for_source_ids(db, kb_id, ["doc-1"])

    assert deleted == 2
    assert db.execute.await_count == 3
    sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_embeddings_for_source_ids_no_op_on_empty_input():
    """Empty source_ids must short-circuit before acquiring the advisory lock.

    Acquiring the lock for a no-op delete would still serialise unrelated
    concurrent syncs and pin a backend slot for the duration of the outer
    transaction, so the early return matters for throughput.
    """
    db = SimpleNamespace(execute=AsyncMock(), begin_nested=_AsyncNullContext)

    deleted = await kb_sync._delete_embeddings_for_source_ids(db, uuid.uuid4(), [])

    assert deleted == 0
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_embeddings_for_source_ids_does_not_retry_non_retryable(
    monkeypatch,
):
    """Non-retryable errors (e.g. validation, constraint violations) must
    surface immediately on the first attempt without sleeping/looping."""
    sleep = AsyncMock()
    monkeypatch.setattr(kb_sync.asyncio, "sleep", sleep)

    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[_Result(), ValueError("bad parameter binding")]
        ),
        begin_nested=_AsyncNullContext,
    )

    with pytest.raises(ValueError, match="bad parameter binding"):
        await kb_sync._delete_embeddings_for_source_ids(
            db, uuid.uuid4(), ["doc-1"]
        )

    # Only the lock + the single failing DELETE attempt should run.
    assert db.execute.await_count == 2
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_embeddings_for_source_ids_raises_after_retries_exhausted(
    monkeypatch,
):
    """After ``_EMBEDDING_DELETE_MAX_RETRIES`` consecutive retryable failures
    the last exception must propagate so the caller can decide what to do."""
    sleep = AsyncMock()
    monkeypatch.setattr(kb_sync.asyncio, "sleep", sleep)

    last_error = Exception("deadlock detected (final)")
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(),  # advisory lock
                Exception("deadlock detected (1)"),
                Exception("deadlock detected (2)"),
                last_error,
            ]
        ),
        begin_nested=_AsyncNullContext,
    )

    with pytest.raises(Exception) as excinfo:
        await kb_sync._delete_embeddings_for_source_ids(
            db, uuid.uuid4(), ["doc-1"]
        )

    assert excinfo.value is last_error
    # Sleeps only happen between attempts 1→2 and 2→3 (not after the final one).
    assert sleep.await_count == kb_sync._EMBEDDING_DELETE_MAX_RETRIES - 1
    assert db.execute.await_count == 1 + kb_sync._EMBEDDING_DELETE_MAX_RETRIES


# ---------------------------------------------------------------------------
# Website crawl: transient vs permanent failure handling + adaptive throttle
# ---------------------------------------------------------------------------


def _resp(status: int, *, headers: dict | None = None, text: str = "") -> httpx.Response:
    return httpx.Response(
        status,
        request=httpx.Request("GET", "https://docs.xtmone.io/"),
        headers=headers or {},
        text=text,
    )


class _QueueClient:
    """Minimal httpx-like client returning queued responses / exceptions."""

    def __init__(self, items):
        self._items = list(items)
        self.calls = 0

    async def get(self, url):
        self.calls += 1
        item = self._items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_is_permanent_http_status_classification():
    # Permanent: 4xx that won't change on retry.
    assert kb_sync._is_permanent_http_status(404)
    assert kb_sync._is_permanent_http_status(403)
    assert kb_sync._is_permanent_http_status(401)
    assert kb_sync._is_permanent_http_status(410)
    # Transient: rate-limit / timing / server errors.
    assert not kb_sync._is_permanent_http_status(429)
    assert not kb_sync._is_permanent_http_status(408)
    assert not kb_sync._is_permanent_http_status(425)
    assert not kb_sync._is_permanent_http_status(500)
    assert not kb_sync._is_permanent_http_status(503)
    assert not kb_sync._is_permanent_http_status(200)


@pytest.mark.asyncio
async def test_fetch_with_retry_404_is_permanent_without_retry():
    client = _QueueClient([_resp(404)])
    resp, is_permanent, _throttled = await kb_sync._fetch_with_retry(
        client, "https://docs.xtmone.io/missing", max_attempts=3, base_delay=0
    )
    assert resp is None
    assert is_permanent is True
    assert client.calls == 1  # permanent → not retried


@pytest.mark.asyncio
async def test_fetch_with_retry_429_is_transient_and_retried():
    client = _QueueClient([_resp(429), _resp(429), _resp(429)])
    resp, is_permanent, throttled = await kb_sync._fetch_with_retry(
        client, "https://docs.xtmone.io/rl", max_attempts=3, base_delay=0
    )
    assert resp is None
    assert is_permanent is False  # rate-limit is transient, must NOT abort crawl
    assert throttled is True
    assert client.calls == 3


@pytest.mark.asyncio
async def test_fetch_with_retry_recovers_after_transient_5xx():
    client = _QueueClient(
        [_resp(503), _resp(200, headers={"content-type": "text/html"})]
    )
    resp, is_permanent, throttled = await kb_sync._fetch_with_retry(
        client, "https://docs.xtmone.io/p", max_attempts=3, base_delay=0
    )
    assert resp is not None and resp.status_code == 200
    assert is_permanent is False
    # Recovered only after a transient 503 → caller must HOLD its rate, not decay.
    assert throttled is True
    assert client.calls == 2


@pytest.mark.asyncio
async def test_fetch_with_retry_clean_success_is_not_throttled():
    client = _QueueClient([_resp(200, headers={"content-type": "text/html"})])
    resp, is_permanent, throttled = await kb_sync._fetch_with_retry(
        client, "https://docs.xtmone.io/p", max_attempts=3, base_delay=0
    )
    assert resp is not None and resp.status_code == 200
    assert is_permanent is False
    assert throttled is False  # no back-off observed → caller may relax the delay
    assert client.calls == 1


@pytest.mark.asyncio
async def test_fetch_with_retry_network_error_is_transient():
    client = _QueueClient(
        [httpx.ConnectError("reset"), httpx.ConnectError("reset"), httpx.ConnectError("reset")]
    )
    resp, is_permanent, throttled = await kb_sync._fetch_with_retry(
        client, "https://docs.xtmone.io/p", max_attempts=3, base_delay=0
    )
    assert resp is None
    assert is_permanent is False  # network errors no longer count as permanent
    assert throttled is True
    assert client.calls == 3


@pytest.mark.parametrize(
    "exc",
    [
        httpx.UnsupportedProtocol("unsupported scheme"),
        httpx.TooManyRedirects("redirect loop"),
        httpx.InvalidURL("malformed url"),
    ],
)
@pytest.mark.asyncio
async def test_fetch_with_retry_permanent_request_errors_not_retried(exc):
    """Malformed/unsupported URLs and redirect loops won't change on retry, so
    they must be classified permanent (no retry, no transient-abort), not as
    transient network errors. ``InvalidURL`` is not an ``HTTPError`` subclass,
    so this also proves a bad URL no longer escapes and aborts the whole sync."""
    client = _QueueClient([exc, exc, exc])
    resp, is_permanent, throttled = await kb_sync._fetch_with_retry(
        client, "ftp://bad.example/x", max_attempts=3, base_delay=0
    )
    assert resp is None
    assert is_permanent is True  # permanent → counts toward permanent-abort
    assert throttled is False  # no transient back-off observed
    assert client.calls == 1  # not retried


@pytest.mark.asyncio
async def test_fetch_with_retry_honors_retry_after_floor(monkeypatch):
    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(kb_sync.asyncio, "sleep", _fake_sleep)
    client = _QueueClient(
        [
            _resp(429, headers={"Retry-After": "7"}),
            _resp(200, headers={"content-type": "text/html"}),
        ]
    )
    resp, _is_permanent, _throttled = await kb_sync._fetch_with_retry(
        client, "https://docs.xtmone.io/p", max_attempts=3, base_delay=0
    )
    assert resp is not None and resp.status_code == 200
    # Retry-After is used as a floor for the back-off.
    assert slept and slept[0] >= 7.0


class _DummyClientContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _AsyncSessionContext:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


async def _run_website_crawl(monkeypatch, *, child_count: int, is_permanent: bool):
    """Drive ``sync_website`` with a seed page that links to ``child_count``
    pages which all fail (transient or permanent).

    Returns ``(docs, fetch_calls, sleeps, exc)`` where ``exc`` is the exception
    raised by ``sync_website`` (if any) or ``None`` on a clean return.
    """
    start_url = "https://docs.xtmone.io"
    children = [f"{start_url}/page-{i}" for i in range(child_count)]
    html = "".join(f'<a href="{u}">x</a>' for u in children)
    seed = _resp(200, headers={"content-type": "text/html"}, text=html)

    # First fetch = seed (clean success); the rest fail with the given
    # classification (a transient failure also reports observed_transient=True).
    results = [(seed, False, False)] + [
        (None, is_permanent, not is_permanent)
    ] * child_count
    fetch_calls: list[str] = []

    async def _fake_fetch(_client, current_url, **_kwargs):
        fetch_calls.append(current_url)
        return results.pop(0)

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    cleanup_db = SimpleNamespace(get=AsyncMock(return_value=None), commit=AsyncMock())

    monkeypatch.setattr(kb_sync.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(kb_sync.httpx, "AsyncClient", lambda **_k: _DummyClientContext())
    monkeypatch.setattr(kb_sync, "_fetch_with_retry", _fake_fetch)
    monkeypatch.setattr(kb_sync, "enqueue_documents", AsyncMock())
    monkeypatch.setattr(
        "app.services.web_fetch.extract_from_html", lambda _html, url: ("", {})
    )
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: _AsyncSessionContext(cleanup_db)
    )

    raised: Exception | None = None
    try:
        docs = await kb_sync.sync_website(
            db=SimpleNamespace(),
            kb=SimpleNamespace(id=uuid.uuid4(), name="docs"),
            url=start_url,
            max_pages=200,
            max_depth=1,
        )
    except Exception as e:
        docs = []
        raised = e
    return docs, fetch_calls, sleeps, raised


@pytest.mark.asyncio
async def test_sync_website_does_not_abort_early_on_transient_and_throttles(monkeypatch):
    """Transient failures (rate-limit) must not abort at the old 5-failure
    limit; the crawl keeps going, adaptively slowing, until the much higher
    consecutive-transient safety valve."""
    _docs, fetch_calls, sleeps, exc = await _run_website_crawl(
        monkeypatch, child_count=20, is_permanent=False
    )

    # Transient-only failures must not raise the perm-fail exception.
    assert exc is None

    # seed + exactly _WEBSITE_CONSECUTIVE_TRANSIENT_ABORT children before abort
    assert len(fetch_calls) == 1 + kb_sync._WEBSITE_CONSECUTIVE_TRANSIENT_ABORT
    # Proves it did NOT abort at the old 5-failure threshold.
    assert kb_sync._WEBSITE_CONSECUTIVE_TRANSIENT_ABORT > 5
    # Adaptive throttle grew the inter-request delay up to the cap.
    assert max(sleeps) == kb_sync._WEBSITE_MAX_REQUEST_DELAY
    assert any(s > kb_sync._WEBSITE_REQUEST_DELAY for s in sleeps)


@pytest.mark.asyncio
async def test_sync_website_parses_links_on_the_parse_executor(monkeypatch):
    """The crawl's link discovery is a full HTML parse: it goes through
    ``run_parse`` like the text extraction, never inline on the event loop,
    where a large page stalled the web process for seconds (#10740)."""
    offloaded: list[object] = []

    async def _recording_run_parse(fn, *args):
        offloaded.append(getattr(fn, "func", fn))
        return fn(*args)

    monkeypatch.setattr("app.core.parse_offload.run_parse", _recording_run_parse)
    _docs, fetch_calls, _sleeps, exc = await _run_website_crawl(
        monkeypatch, child_count=2, is_permanent=False
    )

    assert exc is None
    assert kb_sync._page_hrefs in offloaded
    # The links found off-loop are the ones the crawl went on to fetch.
    assert fetch_calls[1:] == ["https://docs.xtmone.io/page-0", "https://docs.xtmone.io/page-1"]


def test_page_hrefs_keeps_non_empty_stripped_hrefs_in_order():
    html = '<a href=" /a ">a</a><a>no href</a><a href="">empty</a><a href="https://x.io/b">b</a>'

    assert kb_sync._page_hrefs(html) == ["/a", "https://x.io/b"]


@pytest.mark.asyncio
async def test_sync_website_aborts_on_consecutive_permanent_failures(monkeypatch):
    """Permanent failures (404/403) still abort quickly — a broken base URL
    shouldn't crawl forever.  Because no documents were synced and all failures
    were permanent, sync_website must also raise a ValueError so the scheduler's
    perm-fail quarantine can track the source."""
    _docs, fetch_calls, sleeps, exc = await _run_website_crawl(
        monkeypatch, child_count=20, is_permanent=True
    )

    # seed + exactly _WEBSITE_CONSECUTIVE_PERMANENT_ABORT children before abort
    assert len(fetch_calls) == 1 + kb_sync._WEBSITE_CONSECUTIVE_PERMANENT_ABORT
    # The crawl must NOT sleep before giving up: the seed success sleeps once, then
    # PERMANENT_ABORT-1 failures each sleep, and the threshold-tripping failure
    # aborts immediately (no sleep). So total sleeps == 1 + (PERMANENT_ABORT - 1)
    # == PERMANENT_ABORT, never PERMANENT_ABORT + 1.
    assert len(sleeps) == kb_sync._WEBSITE_CONSECUTIVE_PERMANENT_ABORT
    # No docs + permanent failures → must raise so the perm-fail quarantine fires.
    assert isinstance(exc, ValueError)
    assert kb_sync.WEBSITE_ALL_PERMANENT_FAILURES_MARKER in str(exc)


@pytest.mark.asyncio
async def test_sync_website_holds_delay_after_throttled_recovery(monkeypatch):
    """A page that only succeeds *after* a back-off (throttled) must HOLD the
    elevated inter-request delay rather than decay it — otherwise the crawler
    speeds straight back up into the limiter."""
    start_url = "https://docs.xtmone.io"
    children = [f"{start_url}/a", f"{start_url}/b"]
    html = "".join(f'<a href="{u}">x</a>' for u in children)
    seed = _resp(200, headers={"content-type": "text/html"}, text=html)
    page = _resp(200, headers={"content-type": "text/html"}, text="")

    # seed: clean success; child a: transient failure → grows the delay;
    # child b: success but throttled (recovered after back-off) → must hold.
    results = [
        (seed, False, False),
        (None, False, True),
        (page, False, True),
    ]

    async def _fake_fetch(_client, _current_url, **_kwargs):
        return results.pop(0)

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    cleanup_db = SimpleNamespace(get=AsyncMock(return_value=None), commit=AsyncMock())
    monkeypatch.setattr(kb_sync.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(kb_sync.httpx, "AsyncClient", lambda **_k: _DummyClientContext())
    monkeypatch.setattr(kb_sync, "_fetch_with_retry", _fake_fetch)
    monkeypatch.setattr(kb_sync, "enqueue_documents", AsyncMock())
    monkeypatch.setattr(
        "app.services.web_fetch.extract_from_html", lambda _html, url: ("", {})
    )
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: _AsyncSessionContext(cleanup_db)
    )

    await kb_sync.sync_website(
        db=SimpleNamespace(),
        kb=SimpleNamespace(id=uuid.uuid4(), name="docs"),
        url=start_url,
        max_pages=200,
        max_depth=1,
    )

    base = kb_sync._WEBSITE_REQUEST_DELAY
    grown = min(
        kb_sync._WEBSITE_MAX_REQUEST_DELAY, base * kb_sync._WEBSITE_THROTTLE_BACKOFF
    )
    # seed clean → base; child a transient → grow to `grown`; child b throttled
    # success → HOLD at `grown` (no decay).
    assert sleeps == [base, grown, grown]
    # The decayed value must never appear — that would prove it relaxed after a
    # throttled recovery (the oscillation this fix prevents).
    assert grown * kb_sync._WEBSITE_DELAY_DECAY not in sleeps


# ---------------------------------------------------------------------------
# Website sync: per-page + cleanup sessions must inherit the elevated
# statement_timeout
# ---------------------------------------------------------------------------


class _RecordingConn:
    def __init__(self):
        self.execute = AsyncMock()


class _RecordingSession:
    """Fake async session that records ``set_config`` calls on its connection."""

    def __init__(self, kb):
        self._conn = _RecordingConn()
        self.get = AsyncMock(return_value=kb)
        self.commit = AsyncMock()

    async def connection(self):
        return self._conn


def _statement_timeout_calls(session: _RecordingSession) -> list:
    return [
        call
        for call in session._conn.execute.await_args_list
        if "statement_timeout" in str(call.args[0])
        and "set_config" in str(call.args[0])
    ]


async def _run_website_timeout_crawl(monkeypatch, *, statement_timeout_secs):
    """Crawl a single seed page (with storable content) and return the list of
    fresh sessions ``sync_website`` opened (one per page + one for cleanup)."""
    start_url = "https://docs.xtmone.io"
    kb_id = uuid.uuid4()
    seed = _resp(200, headers={"content-type": "text/html"}, text="<html>x</html>")

    async def _fake_fetch(_client, _current_url, **_kwargs):
        return (seed, False, False)

    sessions: list[_RecordingSession] = []

    def _make_session():
        session = _RecordingSession(SimpleNamespace(id=kb_id, name="docs"))
        sessions.append(session)
        return _AsyncSessionContext(session)

    monkeypatch.setattr(kb_sync.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(kb_sync.httpx, "AsyncClient", lambda **_k: _DummyClientContext())
    monkeypatch.setattr(kb_sync, "_fetch_with_retry", _fake_fetch)
    monkeypatch.setattr(kb_sync, "enqueue_documents", AsyncMock())
    monkeypatch.setattr(
        kb_sync,
        "_upsert_document",
        AsyncMock(return_value=(SimpleNamespace(id=uuid.uuid4()), True)),
    )
    monkeypatch.setattr(kb_sync, "_delete_stale_documents", AsyncMock(return_value=0))
    monkeypatch.setattr(kb_sync, "_recount_kb_stats", AsyncMock())
    monkeypatch.setattr(
        "app.services.web_fetch.extract_from_html",
        lambda _html, url: ("This is extracted page content long enough to store.", {}),
    )
    monkeypatch.setattr("app.core.database.async_session", _make_session)

    await kb_sync.sync_website(
        db=SimpleNamespace(),
        kb=SimpleNamespace(id=kb_id, name="docs"),
        url=start_url,
        max_pages=5,
        max_depth=0,
        statement_timeout_secs=statement_timeout_secs,
    )
    return sessions


@pytest.mark.asyncio
async def test_sync_website_applies_statement_timeout_to_isolated_sessions(monkeypatch):
    """The per-page and cleanup sessions are opened fresh from the pool, so they
    must raise ``statement_timeout`` to the configured sync timeout themselves —
    otherwise large INSERT/DELETE on ``knowledge_documents`` are cancelled at the
    30s pool default (SQLSTATE 57014)."""
    sessions = await _run_website_timeout_crawl(
        monkeypatch, statement_timeout_secs=1800
    )

    # One per crawled page (1) + one for the stale-cleanup pass.
    assert len(sessions) == 2
    for session in sessions:
        calls = _statement_timeout_calls(session)
        assert len(calls) == 1, "each fresh sync session must lift statement_timeout"
        # 1800s -> 1_800_000 ms, passed as a string parameter.
        assert calls[0].args[1]["statement_timeout_ms"] == "1800000"


@pytest.mark.asyncio
async def test_sync_website_without_timeout_keeps_pool_default(monkeypatch):
    """A ``None`` timeout is a no-op: direct callers / tests keep the pool
    default and no ``set_config`` is issued."""
    sessions = await _run_website_timeout_crawl(
        monkeypatch, statement_timeout_secs=None
    )

    assert len(sessions) == 2
    for session in sessions:
        assert _statement_timeout_calls(session) == []


# ---------------------------------------------------------------------------
# _resolve_github_default_branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_github_default_branch_returns_default_branch():
    """Returns the 'default_branch' value from the GitHub /repos/{repo} API."""
    response = httpx.Response(200, json={"default_branch": "main"})
    client = SimpleNamespace(get=AsyncMock(return_value=response))
    result = await kb_sync._resolve_github_default_branch(client, "owner/repo", {})
    assert result == "main"


@pytest.mark.asyncio
async def test_resolve_github_default_branch_returns_master():
    """Works for repos whose default branch is still 'master'."""
    response = httpx.Response(200, json={"default_branch": "master"})
    client = SimpleNamespace(get=AsyncMock(return_value=response))
    result = await kb_sync._resolve_github_default_branch(client, "owner/repo", {})
    assert result == "master"


@pytest.mark.asyncio
async def test_resolve_github_default_branch_strips_whitespace():
    """Normalizes a padded default_branch value before returning it."""
    response = httpx.Response(200, json={"default_branch": "  develop  "})
    client = SimpleNamespace(get=AsyncMock(return_value=response))
    result = await kb_sync._resolve_github_default_branch(client, "owner/repo", {})
    assert result == "develop"


@pytest.mark.asyncio
async def test_resolve_github_default_branch_returns_none_on_non_200():
    """Returns None when the API does not return a 200 response."""
    response = httpx.Response(403, json={"message": "Forbidden"})
    client = SimpleNamespace(get=AsyncMock(return_value=response))
    result = await kb_sync._resolve_github_default_branch(client, "owner/repo", {})
    assert result is None


@pytest.mark.asyncio
async def test_resolve_github_default_branch_returns_none_on_exception():
    """Returns None if the HTTP request raises an exception."""
    client = SimpleNamespace(get=AsyncMock(side_effect=httpx.ConnectError("timeout")))
    result = await kb_sync._resolve_github_default_branch(client, "owner/repo", {})
    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("meta", [{}, {"default_branch": None}, {"default_branch": "  "}])
async def test_resolve_github_default_branch_returns_none_on_unusable_field(meta):
    """Returns None when default_branch is missing, null, or blank."""
    response = httpx.Response(200, json=meta)
    client = SimpleNamespace(get=AsyncMock(return_value=response))
    result = await kb_sync._resolve_github_default_branch(client, "owner/repo", {})
    assert result is None


# ---------------------------------------------------------------------------
# sync_github_repo – 404 → default_branch fallback
# ---------------------------------------------------------------------------


def _make_httpx_response(status_code: int, json_body: dict | None = None) -> httpx.Response:
    import json as _json

    content = _json.dumps(json_body or {}).encode()
    resp = httpx.Response(status_code, content=content, headers={"content-type": "application/json"})
    # Attach a dummy request so raise_for_status() works correctly
    resp.request = httpx.Request("GET", "https://api.github.com/test")
    return resp


class _FakeAsyncClient:
    """Minimal async context-manager shim that records every GET call."""

    def __init__(self, responses: list[httpx.Response]):
        self._responses = list(responses)
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def get(self, url: str, **_kwargs):
        self.calls.append(url)
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_sync_github_repo_retries_with_resolved_branch_on_404(monkeypatch):
    """When the configured branch returns 404, sync_github_repo resolves the
    real default_branch via GET /repos/{repo} and retries the tree fetch."""

    tree_response = _make_httpx_response(200, {"tree": []})
    repo_meta = _make_httpx_response(200, {"default_branch": "main"})
    # First call: tree fetch for "master" → 404
    # Second call: repo metadata → 200 with default_branch=main
    # Third call: tree fetch for "main" → 200 (empty tree)
    client = _FakeAsyncClient([
        _make_httpx_response(404, {"message": "Not Found"}),
        repo_meta,
        tree_response,
    ])

    monkeypatch.setattr(kb_sync.httpx, "AsyncClient", lambda **_kw: client)
    monkeypatch.setattr(kb_sync, "_get_integration_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(kb_sync, "_delete_stale_documents", AsyncMock(return_value=0))
    monkeypatch.setattr(kb_sync, "_recount_kb_stats", AsyncMock())
    monkeypatch.setattr(kb_sync, "enqueue_documents", AsyncMock())

    kb = SimpleNamespace(id=uuid.uuid4(), name="test-kb")
    db = SimpleNamespace(commit=AsyncMock())
    doc_ids = await kb_sync.sync_github_repo(
        db=db,
        kb=kb,
        user_id=uuid.uuid4(),
        repo="owner/openaev",
        branch="master",
    )

    assert doc_ids == []
    # Ensure the tree was first attempted with "master", then with "main"
    assert any("master" in url for url in client.calls)
    assert any("main" in url and "trees" in url for url in client.calls)


@pytest.mark.asyncio
async def test_sync_github_repo_does_not_retry_on_non_404_errors(monkeypatch):
    """Non-404 errors (e.g. 403 rate-limit) are not retried via default_branch
    resolution — they should surface as-is for the caller to handle."""

    client = _FakeAsyncClient([
        _make_httpx_response(403, {"message": "rate limit exceeded"}),
    ])

    monkeypatch.setattr(kb_sync.httpx, "AsyncClient", lambda **_kw: client)
    monkeypatch.setattr(
        kb_sync,
        "_get_integration_token",
        AsyncMock(return_value="tok"),
    )

    kb = SimpleNamespace(id=uuid.uuid4(), name="test-kb")
    with pytest.raises(httpx.HTTPStatusError):
        await kb_sync.sync_github_repo(
            db=SimpleNamespace(),
            kb=kb,
            user_id=uuid.uuid4(),
            repo="owner/repo",
            branch="main",
        )

    # Only one GET call — no fallback resolution attempted
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_sync_github_repo_no_retry_for_non_default_branch(monkeypatch):
    """A 404 on an explicitly configured non-default branch (e.g. 'develop')
    must NOT trigger default_branch resolution — silently substituting the
    default branch would sync unintended content and hide the misconfig."""

    client = _FakeAsyncClient([
        _make_httpx_response(404, {"message": "Not Found"}),
    ])

    monkeypatch.setattr(kb_sync.httpx, "AsyncClient", lambda **_kw: client)
    monkeypatch.setattr(
        kb_sync,
        "_get_integration_token",
        AsyncMock(return_value="tok"),
    )

    kb = SimpleNamespace(id=uuid.uuid4(), name="test-kb")
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await kb_sync.sync_github_repo(
            db=SimpleNamespace(),
            kb=kb,
            user_id=uuid.uuid4(),
            repo="owner/repo",
            branch="develop",
        )

    assert excinfo.value.response.status_code == 404
    # Single GET call — no repo-metadata resolution, no retried tree fetch
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_sync_github_repo_no_retry_when_resolved_branch_same(monkeypatch):
    """If the resolved default_branch equals the configured branch, no extra
    tree fetch is issued (avoids a redundant API call and infinite loop)."""

    # 404 for "main" tree → repo meta says default_branch is also "main"
    # So a second tree fetch would also 404; without the guard it would loop.
    client = _FakeAsyncClient([
        _make_httpx_response(404, {"message": "Not Found"}),
        _make_httpx_response(200, {"default_branch": "main"}),
    ])

    monkeypatch.setattr(kb_sync.httpx, "AsyncClient", lambda **_kw: client)
    monkeypatch.setattr(
        kb_sync,
        "_get_integration_token",
        AsyncMock(return_value="tok"),
    )

    kb = SimpleNamespace(id=uuid.uuid4(), name="test-kb")
    with pytest.raises(httpx.HTTPStatusError):
        await kb_sync.sync_github_repo(
            db=SimpleNamespace(),
            kb=kb,
            user_id=uuid.uuid4(),
            repo="owner/repo",
            branch="main",
        )

    # Two calls: tree fetch (404) + repo meta — no second tree fetch
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_sync_github_repo_no_retry_when_resolution_fails(monkeypatch):
    """If the default_branch resolution itself fails (e.g. 403 on the repo
    metadata call), no blind retry is issued — the original 404 for the
    branch the user actually configured propagates unchanged."""

    client = _FakeAsyncClient([
        _make_httpx_response(404, {"message": "Not Found"}),
        _make_httpx_response(403, {"message": "Forbidden"}),
    ])

    monkeypatch.setattr(kb_sync.httpx, "AsyncClient", lambda **_kw: client)
    monkeypatch.setattr(
        kb_sync,
        "_get_integration_token",
        AsyncMock(return_value="tok"),
    )

    kb = SimpleNamespace(id=uuid.uuid4(), name="test-kb")
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await kb_sync.sync_github_repo(
            db=SimpleNamespace(),
            kb=kb,
            user_id=uuid.uuid4(),
            repo="owner/repo",
            branch="master",
        )

    # The surfaced error is the original 404, not a guessed-branch fetch
    assert excinfo.value.response.status_code == 404
    # Two calls: tree fetch (404) + failed repo meta — no second tree fetch
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_sync_github_repo_retry_hitting_empty_repo_returns_empty(monkeypatch):
    """A 409 (empty repo) on the retried tree fetch is treated like a 409 on
    the initial fetch: the sync is skipped cleanly instead of raising."""

    client = _FakeAsyncClient([
        _make_httpx_response(404, {"message": "Not Found"}),
        _make_httpx_response(200, {"default_branch": "main"}),
        _make_httpx_response(409, {"message": "Git Repository is empty."}),
    ])

    monkeypatch.setattr(kb_sync.httpx, "AsyncClient", lambda **_kw: client)
    monkeypatch.setattr(
        kb_sync,
        "_get_integration_token",
        AsyncMock(return_value="tok"),
    )

    kb = SimpleNamespace(id=uuid.uuid4(), name="test-kb")
    doc_ids = await kb_sync.sync_github_repo(
        db=SimpleNamespace(),
        kb=kb,
        user_id=uuid.uuid4(),
        repo="owner/repo",
        branch="master",
    )

    assert doc_ids == []
    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_sync_github_repo_strips_padded_branch(monkeypatch):
    """A configured branch with stray whitespace (e.g. 'master ') is stripped
    before building the tree URL, so a valid config doesn't 404 in the first
    place and the main/master recovery gate still matches when it does."""

    tree_response = _make_httpx_response(200, {"tree": []})
    client = _FakeAsyncClient([
        _make_httpx_response(404, {"message": "Not Found"}),
        _make_httpx_response(200, {"default_branch": "main"}),
        tree_response,
    ])

    monkeypatch.setattr(kb_sync.httpx, "AsyncClient", lambda **_kw: client)
    monkeypatch.setattr(kb_sync, "_get_integration_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(kb_sync, "_delete_stale_documents", AsyncMock(return_value=0))
    monkeypatch.setattr(kb_sync, "_recount_kb_stats", AsyncMock())
    monkeypatch.setattr(kb_sync, "enqueue_documents", AsyncMock())

    kb = SimpleNamespace(id=uuid.uuid4(), name="test-kb")
    db = SimpleNamespace(commit=AsyncMock())
    doc_ids = await kb_sync.sync_github_repo(
        db=db,
        kb=kb,
        user_id=uuid.uuid4(),
        repo="owner/repo",
        branch="  master  ",
    )

    assert doc_ids == []
    # The first tree fetch used the stripped branch (no %20 / spaces in URL)
    assert client.calls[0].endswith("/git/trees/master?recursive=1")
    # And the stripped value passed the main/master gate -> recovery ran
    assert client.calls[2].endswith("/git/trees/main?recursive=1")


@pytest.mark.asyncio
async def test_sync_github_repo_blank_branch_falls_back_to_main(monkeypatch):
    """A blank configured branch falls back to the documented 'main' default
    instead of producing a malformed trees// URL."""

    tree_response = _make_httpx_response(200, {"tree": []})
    client = _FakeAsyncClient([tree_response])

    monkeypatch.setattr(kb_sync.httpx, "AsyncClient", lambda **_kw: client)
    monkeypatch.setattr(kb_sync, "_get_integration_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(kb_sync, "_delete_stale_documents", AsyncMock(return_value=0))
    monkeypatch.setattr(kb_sync, "_recount_kb_stats", AsyncMock())
    monkeypatch.setattr(kb_sync, "enqueue_documents", AsyncMock())

    kb = SimpleNamespace(id=uuid.uuid4(), name="test-kb")
    db = SimpleNamespace(commit=AsyncMock())
    doc_ids = await kb_sync.sync_github_repo(
        db=db,
        kb=kb,
        user_id=uuid.uuid4(),
        repo="owner/repo",
        branch="   ",
    )

    assert doc_ids == []
    assert client.calls[0].endswith("/git/trees/main?recursive=1")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_branch", [None, 0, 1, True, ["main"], {"name": "main"}])
async def test_sync_github_repo_non_string_branch_falls_back_to_main(
    monkeypatch, bad_branch
):
    """A non-string configured branch (the sync-source config is free-form
    user-entered JSON) is treated as blank and falls back to 'main' instead
    of crashing with AttributeError before any GitHub call is made."""

    tree_response = _make_httpx_response(200, {"tree": []})
    client = _FakeAsyncClient([tree_response])

    monkeypatch.setattr(kb_sync.httpx, "AsyncClient", lambda **_kw: client)
    monkeypatch.setattr(kb_sync, "_get_integration_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(kb_sync, "_delete_stale_documents", AsyncMock(return_value=0))
    monkeypatch.setattr(kb_sync, "_recount_kb_stats", AsyncMock())
    monkeypatch.setattr(kb_sync, "enqueue_documents", AsyncMock())

    kb = SimpleNamespace(id=uuid.uuid4(), name="test-kb")
    db = SimpleNamespace(commit=AsyncMock())
    doc_ids = await kb_sync.sync_github_repo(
        db=db,
        kb=kb,
        user_id=uuid.uuid4(),
        repo="owner/repo",
        branch=bad_branch,
    )

    assert doc_ids == []
    assert client.calls[0].endswith("/git/trees/main?recursive=1")


# ---------------------------------------------------------------------------
# Website sync: URL normalization — strip whitespace + prepend scheme
# ---------------------------------------------------------------------------


async def _run_url_normalization_crawl(
    monkeypatch, *, raw_url: str | None | int
) -> list[str]:
    """Run ``sync_website`` with *raw_url* and return the list of URLs that
    ``_fetch_with_retry`` was actually called with (i.e. after normalization).

    ``raw_url`` is deliberately typed wider than ``str``: the non-string
    fail-fast tests pass ``None`` / ``123`` to mirror what a persisted JSON
    null or number looks like when the scheduler forwards it verbatim."""
    # A minimal seed page with no child links so the crawl ends immediately.
    seed = _resp(200, headers={"content-type": "text/html"}, text="<html>hello</html>")
    fetch_calls: list[str] = []

    async def _fake_fetch(_client, current_url, **_kwargs):
        fetch_calls.append(current_url)
        return (seed, False, False)

    cleanup_db = SimpleNamespace(get=AsyncMock(return_value=None), commit=AsyncMock())

    monkeypatch.setattr(kb_sync.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(kb_sync.httpx, "AsyncClient", lambda **_k: _DummyClientContext())
    monkeypatch.setattr(kb_sync, "_fetch_with_retry", _fake_fetch)
    monkeypatch.setattr(kb_sync, "enqueue_documents", AsyncMock())
    monkeypatch.setattr(
        "app.services.web_fetch.extract_from_html", lambda _html, url: ("", {})
    )
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: _AsyncSessionContext(cleanup_db)
    )

    await kb_sync.sync_website(
        db=SimpleNamespace(),
        kb=SimpleNamespace(id=uuid.uuid4(), name="docs"),
        url=raw_url,
        max_pages=5,
        max_depth=0,
    )
    return fetch_calls


@pytest.mark.asyncio
async def test_sync_website_prepends_https_when_scheme_missing(monkeypatch):
    """A URL without any scheme (e.g. ``docs.xtmone.io``) must be normalised to
    ``https://docs.xtmone.io`` before fetching — otherwise httpx raises
    UnsupportedProtocol / InvalidURL and the source is permanently failed."""
    fetch_calls = await _run_url_normalization_crawl(
        monkeypatch, raw_url="docs.xtmone.io"
    )

    assert len(fetch_calls) == 1
    assert fetch_calls[0] == "https://docs.xtmone.io"


@pytest.mark.asyncio
async def test_sync_website_strips_leading_whitespace_from_url(monkeypatch):
    """A URL with leading/trailing whitespace (e.g. pasted with an accidental
    space) must be stripped before fetching — the whitespace alone causes httpx
    to raise InvalidURL and silently marks the source as permanently failed."""
    fetch_calls = await _run_url_normalization_crawl(
        monkeypatch, raw_url="  https://docs.xtmone.io  "
    )

    assert len(fetch_calls) == 1
    assert fetch_calls[0] == "https://docs.xtmone.io"


@pytest.mark.asyncio
async def test_sync_website_strips_whitespace_and_prepends_https(monkeypatch):
    """A schemeless URL with surrounding whitespace (both problems at once) is
    normalised: strip first, then prepend ``https://``."""
    fetch_calls = await _run_url_normalization_crawl(
        monkeypatch, raw_url="  docs.xtmone.io  "
    )

    assert len(fetch_calls) == 1
    assert fetch_calls[0] == "https://docs.xtmone.io"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_url",
    [
        # ``://`` embedded in the query of a path-less URL
        "docs.xtmone.io?next=http://example.com",
        # ``://`` embedded in the query of a URL with a path
        "docs.xtmone.io/redirect?to=https://other.example",
        # ``://`` embedded in the fragment
        "docs.xtmone.io/page#src=http://example.com",
    ],
)
async def test_sync_website_prepends_https_when_query_embeds_scheme(
    monkeypatch, raw_url
):
    """Scheme detection must anchor on the START of the URL: a schemeless URL
    whose query string or fragment embeds another URL contains ``://`` as a
    substring but still needs the prefix."""
    fetch_calls = await _run_url_normalization_crawl(monkeypatch, raw_url=raw_url)

    assert len(fetch_calls) == 1
    assert fetch_calls[0] == "https://" + raw_url


@pytest.mark.asyncio
async def test_sync_website_rejects_explicit_non_http_scheme(monkeypatch):
    """An explicit non-http scheme (``ftp://``) is NOT rewritten to https —
    it must fail fast with a clear ValueError instead. If it reached the
    crawl, every fetch would be a permanent UnsupportedProtocol failure that
    is only logged: the run would finish as a SUCCESS with zero synced ids
    and stale-cleanup would delete every previously synced page."""
    with pytest.raises(ValueError, match="http or https"):
        await _run_url_normalization_crawl(
            monkeypatch, raw_url="ftp://files.xtmone.io/docs"
        )


@pytest.mark.asyncio
async def test_sync_website_rejects_blank_url(monkeypatch):
    """A blank / whitespace-only URL must fail fast with a clear error. If it
    reached the crawl it would end with zero synced ids and the stale-cleanup
    pass would delete every previously synced page of the source."""
    with pytest.raises(ValueError, match="empty"):
        await _run_url_normalization_crawl(monkeypatch, raw_url="   ")


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_url", [None, 123])
async def test_sync_website_rejects_non_string_url(monkeypatch, raw_url):
    """A non-string URL (the sync-source config is user-entered JSON, so a
    persisted ``null`` or number reaches ``sync_website`` as-is via the
    scheduler's ``config.get("url", "")``) must hit the same clear fail-fast
    ValueError instead of crashing with an AttributeError at ``url.strip()``."""
    with pytest.raises(ValueError, match="empty"):
        await _run_url_normalization_crawl(monkeypatch, raw_url=raw_url)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_url", ["https://", "https:///path", "http://?q=1"])
async def test_sync_website_rejects_hostless_url(monkeypatch, raw_url):
    """A hostless absolute URL (empty ``netloc``, e.g. ``https://``) passes the
    scheme check but can never fetch anything — every attempt is a permanent
    InvalidURL failure, and a zero-id finish would let stale-cleanup delete
    every previously synced page. It must fail fast with a clear ValueError."""
    with pytest.raises(ValueError, match="no host"):
        await _run_url_normalization_crawl(monkeypatch, raw_url=raw_url)


# ---------------------------------------------------------------------------
# format_exception_message — sync failure logs must never end with an empty reason
# ---------------------------------------------------------------------------


def test_format_exception_message_preserves_normal_message():
    assert format_exception_message(ValueError("boom")) == "boom"


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError(),  # str() is "" — the "Failed to sync Notion page <id>: " bug
        asyncio.CancelledError(),
        httpx.ConnectError(""),
        Exception(),
        Exception("   "),  # whitespace-only is as useless as empty
    ],
)
def test_format_exception_message_never_returns_empty(exc):
    """Exceptions whose ``str()`` is empty/blank must fall back to the
    exception class name (plus the chained cause when present) so something
    actionable appears in the log instead of nothing after the colon."""
    formatted = format_exception_message(exc)
    assert formatted.strip()
    assert type(exc).__name__ in formatted


def test_format_exception_message_logs_nonempty_reason_in_warning(caplog):
    """End-to-end shape of the production log line: the reason after the
    colon is never blank."""
    import logging

    with caplog.at_level(logging.WARNING, logger=kb_sync.logger.name):
        kb_sync.logger.warning(
            "Failed to sync Notion page %s: %s",
            "2b88fce1-7f2a-8159-83c8-e9bf1d3fca32",
            format_exception_message(TimeoutError()),
        )
    assert not caplog.records[-1].getMessage().endswith(": ")
    assert "TimeoutError" in caplog.records[-1].getMessage()


# ---------------------------------------------------------------------------
# sync_github_repo — per-file transient 5xx retry
# ---------------------------------------------------------------------------


def _make_raw_response(status_code: int, text: str = "") -> httpx.Response:
    resp = httpx.Response(
        status_code, content=text.encode(), headers={"content-type": "text/plain"}
    )
    resp.request = httpx.Request("GET", "https://api.github.com/test")
    return resp


def _github_sync_db():
    return SimpleNamespace(
        commit=AsyncMock(), flush=AsyncMock(), begin_nested=_AsyncNullContext
    )


def _patch_github_sync_plumbing(monkeypatch, client):
    monkeypatch.setattr(kb_sync.httpx, "AsyncClient", lambda **_kw: client)
    monkeypatch.setattr(kb_sync, "_get_integration_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(kb_sync, "_delete_stale_documents", AsyncMock(return_value=0))
    monkeypatch.setattr(kb_sync, "_recount_kb_stats", AsyncMock())
    monkeypatch.setattr(kb_sync, "enqueue_documents", AsyncMock())
    monkeypatch.setattr(kb_sync.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        kb_sync,
        "_upsert_document",
        AsyncMock(return_value=(SimpleNamespace(id=uuid.uuid4()), True)),
    )


@pytest.mark.asyncio
async def test_sync_github_repo_retries_file_fetch_on_transient_5xx(monkeypatch):
    """A transient '502 Bad Gateway' on a per-file content fetch is retried
    with backoff instead of failing the file outright (production bug: every
    5xx blip left a permanent gap in the KB until the next full sync)."""
    tree = _make_httpx_response(
        200, {"tree": [{"type": "blob", "path": "README.md", "size": 100}]}
    )
    client = _FakeAsyncClient(
        [
            tree,
            _make_raw_response(502, "Bad Gateway"),
            _make_raw_response(200, "# README\nlong enough content"),
        ]
    )
    _patch_github_sync_plumbing(monkeypatch, client)

    kb = SimpleNamespace(id=uuid.uuid4(), name="test-kb")
    doc_ids = await kb_sync.sync_github_repo(
        db=_github_sync_db(),
        kb=kb,
        user_id=uuid.uuid4(),
        repo="owner/repo",
        branch="main",
    )

    assert len(doc_ids) == 1
    # tree fetch + failed file fetch + retried file fetch
    assert len(client.calls) == 3
    assert client.calls[1] == client.calls[2]


@pytest.mark.asyncio
async def test_sync_github_repo_skips_file_after_retries_exhausted(
    monkeypatch, caplog
):
    """When a file keeps failing transiently, the file is skipped after the
    bounded retries — the rest of the sync continues instead of aborting."""
    import logging
    tree = _make_httpx_response(
        200,
        {
            "tree": [
                {"type": "blob", "path": "broken.md", "size": 100},
                {"type": "blob", "path": "ok.md", "size": 100},
            ]
        },
    )
    responses = [tree]
    responses += [_make_raw_response(502, "Bad Gateway")] * kb_sync._GITHUB_FILE_RETRY_ATTEMPTS
    responses += [_make_raw_response(200, "fine content here")]
    client = _FakeAsyncClient(responses)
    _patch_github_sync_plumbing(monkeypatch, client)

    kb = SimpleNamespace(id=uuid.uuid4(), name="test-kb")
    with caplog.at_level(logging.WARNING, logger=kb_sync.logger.name):
        doc_ids = await kb_sync.sync_github_repo(
            db=_github_sync_db(),
            kb=kb,
            user_id=uuid.uuid4(),
            repo="owner/repo",
            branch="main",
        )

    # Only the healthy file made it in; the broken one was skipped, not fatal.
    assert len(doc_ids) == 1
    assert len(client.calls) == 1 + kb_sync._GITHUB_FILE_RETRY_ATTEMPTS + 1
    # A transient failure that survived the retries is labelled as such.
    assert any(
        "broken.md: fetch failed after retries" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_sync_github_repo_permanent_404_file_not_retried(monkeypatch, caplog):
    """A permanent 404 on a file fetch is skipped immediately (no retry burn),
    and the skip log says "permanent fetch failure" — not "after retries"."""
    import logging
    tree = _make_httpx_response(
        200,
        {
            "tree": [
                {"type": "blob", "path": "gone.md", "size": 100},
                {"type": "blob", "path": "ok.md", "size": 100},
            ]
        },
    )
    client = _FakeAsyncClient(
        [
            tree,
            _make_raw_response(404, "Not Found"),
            _make_raw_response(200, "fine content here"),
        ]
    )
    _patch_github_sync_plumbing(monkeypatch, client)

    kb = SimpleNamespace(id=uuid.uuid4(), name="test-kb")
    with caplog.at_level(logging.WARNING, logger=kb_sync.logger.name):
        doc_ids = await kb_sync.sync_github_repo(
            db=_github_sync_db(),
            kb=kb,
            user_id=uuid.uuid4(),
            repo="owner/repo",
            branch="main",
        )

    assert len(doc_ids) == 1
    # tree + one 404 attempt (not retried) + one healthy fetch
    assert len(client.calls) == 3
    # The skip log must not claim retries happened for a permanent failure.
    assert any(
        "gone.md: permanent fetch failure" in r.getMessage()
        for r in caplog.records
    )
    assert not any(
        "gone.md: fetch failed after retries" in r.getMessage()
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# sync_github_repo — resolved default branch is persisted to the source config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_resolved_default_branch_updates_config():
    source = SimpleNamespace(config={"repo": "owner/repo", "branch": "master"})
    db = SimpleNamespace(get=AsyncMock(return_value=source), flush=AsyncMock())

    await kb_sync._persist_resolved_default_branch(db, str(uuid.uuid4()), "main")

    assert source.config["branch"] == "main"
    assert source.config["repo"] == "owner/repo"  # other keys untouched
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_resolved_default_branch_is_best_effort():
    """A DB failure while persisting must not break the sync itself."""
    db = SimpleNamespace(
        get=AsyncMock(side_effect=RuntimeError("db down")), flush=AsyncMock()
    )
    # Must not raise.
    await kb_sync._persist_resolved_default_branch(db, str(uuid.uuid4()), "main")


@pytest.mark.asyncio
async def test_sync_github_repo_persists_resolved_branch_to_source(monkeypatch):
    """First sync: 404 on 'master' -> resolve 'main' -> retry AND persist the
    resolved branch into the KBSyncSource config. Second sync (with the
    persisted branch): straight to the tree fetch, no re-resolution."""
    source = SimpleNamespace(config={"repo": "owner/repo", "branch": "master"})
    source_id = str(uuid.uuid4())

    # ── First sync: configured branch 404s ──────────────────────────
    client = _FakeAsyncClient(
        [
            _make_httpx_response(404, {"message": "Not Found"}),
            _make_httpx_response(200, {"default_branch": "main"}),
            _make_httpx_response(200, {"tree": []}),
        ]
    )
    _patch_github_sync_plumbing(monkeypatch, client)
    db = SimpleNamespace(
        commit=AsyncMock(),
        flush=AsyncMock(),
        begin_nested=_AsyncNullContext,
        get=AsyncMock(return_value=source),
    )

    kb = SimpleNamespace(id=uuid.uuid4(), name="test-kb")
    await kb_sync.sync_github_repo(
        db=db,
        kb=kb,
        user_id=uuid.uuid4(),
        repo="owner/repo",
        branch="master",
        source_id=source_id,
    )

    # The resolved default branch was written back to the source config.
    assert source.config["branch"] == "main"

    # ── Second sync: scheduler now passes the persisted branch ──────
    client2 = _FakeAsyncClient([_make_httpx_response(200, {"tree": []})])
    _patch_github_sync_plumbing(monkeypatch, client2)
    db2 = SimpleNamespace(
        commit=AsyncMock(),
        flush=AsyncMock(),
        begin_nested=_AsyncNullContext,
        get=AsyncMock(return_value=source),
    )

    await kb_sync.sync_github_repo(
        db=db2,
        kb=kb,
        user_id=uuid.uuid4(),
        repo="owner/repo",
        branch=source.config["branch"],
        source_id=source_id,
    )

    # Exactly one call: the tree fetch on 'main' — no repo-metadata
    # re-resolution, no second tree fetch.
    assert len(client2.calls) == 1
    assert client2.calls[0].endswith("/git/trees/main?recursive=1")
    db2.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_github_repo_without_source_id_does_not_touch_db_config(
    monkeypatch,
):
    """The manual API path has no KBSyncSource row — resolution still works
    but nothing is persisted."""
    client = _FakeAsyncClient(
        [
            _make_httpx_response(404, {"message": "Not Found"}),
            _make_httpx_response(200, {"default_branch": "main"}),
            _make_httpx_response(200, {"tree": []}),
        ]
    )
    _patch_github_sync_plumbing(monkeypatch, client)
    db = SimpleNamespace(
        commit=AsyncMock(),
        flush=AsyncMock(),
        begin_nested=_AsyncNullContext,
        get=AsyncMock(),
    )

    kb = SimpleNamespace(id=uuid.uuid4(), name="test-kb")
    doc_ids = await kb_sync.sync_github_repo(
        db=db,
        kb=kb,
        user_id=uuid.uuid4(),
        repo="owner/repo",
        branch="master",
    )

    assert doc_ids == []
    db.get.assert_not_awaited()


# ---------------------------------------------------------------------------
# sync_github_repo — per-file DB error session-recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_github_repo_db_error_recovers_session_for_subsequent_files(
    monkeypatch, caplog
):
    """When a per-file DB write fails AND the savepoint rollback leaves the
    session in a deactivated state (PendingRollbackError cascade), the code
    calls rollback() to restore the session so remaining files can still be
    synced.

    Production symptom: one bad DB error poisoned the SQLAlchemy
    session for the rest of the batch, causing 50+ cascade "Can't reconnect
    until invalid transaction is rolled back" warnings — one per file — that
    masked the original root-cause error and caused the whole sync to time out.

    The recovery must also repair the collateral damage of the full rollback:
    pre-rollback embedding ids are dropped (their rows were discarded), the
    keep-set for the stale-delete is retained, the returned document ids are
    re-grounded against the rows that actually persisted, the expired ``kb``
    instance is refreshed, and the SET LOCAL statement_timeout is re-applied.
    """
    import logging

    tree = _make_httpx_response(
        200,
        {
            "tree": [
                {"type": "blob", "path": "before.md", "size": 100},
                {"type": "blob", "path": "broken.md", "size": 100},
                {"type": "blob", "path": "after.md", "size": 100},
            ]
        },
    )
    client = _FakeAsyncClient(
        [
            tree,
            _make_raw_response(200, "content"),
            _make_raw_response(200, "content"),
            _make_raw_response(200, "content"),
        ]
    )
    _patch_github_sync_plumbing(monkeypatch, client)
    apply_timeout = AsyncMock()
    monkeypatch.setattr(kb_sync, "_apply_sync_statement_timeout", apply_timeout)

    # _upsert_document: succeeds for the first file, raises a DB error for the
    # second, succeeds again for the third.
    pre_doc_id = uuid.uuid4()
    post_doc_id = uuid.uuid4()
    upsert_call_count = [0]

    async def _upsert_side_effect(*args, **kwargs):
        upsert_call_count[0] += 1
        if upsert_call_count[0] == 2:
            raise RuntimeError("simulated DB constraint error")
        doc_id = pre_doc_id if upsert_call_count[0] == 1 else post_doc_id
        return SimpleNamespace(id=doc_id), True

    monkeypatch.setattr(kb_sync, "_upsert_document", _upsert_side_effect)

    # Build a db mock whose begin_nested simulates a failed ROLLBACK TO SAVEPOINT:
    # the first exception deactivates sync_session.is_active, as happens when
    # the DB connection is lost mid-write and the savepoint rollback itself fails.
    rollback_calls = [0]
    sync_session = SimpleNamespace(is_active=True)
    nested_fail_count = [0]

    class _DeactivatingBeginNested:
        async def __aenter__(self):
            return None

        async def __aexit__(self, exc_type, exc, tb):
            if exc_type is not None and nested_fail_count[0] == 0:
                # First failure: simulate a broken ROLLBACK TO SAVEPOINT
                sync_session.is_active = False
                nested_fail_count[0] += 1
            return False  # never suppress

    async def _rollback():
        rollback_calls[0] += 1
        sync_session.is_active = True  # rollback restores the session

    # After the recovery rollback, the returned ids are re-grounded against
    # the rows that actually persisted: the pre-failure doc was a NEW insert
    # discarded by the rollback, so only the post-recovery doc remains.
    persisted_ids_result = SimpleNamespace(scalars=lambda: iter([post_doc_id]))

    db = SimpleNamespace(
        commit=AsyncMock(),
        flush=AsyncMock(),
        rollback=_rollback,
        refresh=AsyncMock(),
        execute=AsyncMock(return_value=persisted_ids_result),
        sync_session=sync_session,
        begin_nested=_DeactivatingBeginNested,
    )

    kb = SimpleNamespace(id=uuid.uuid4(), name="test-kb")
    with caplog.at_level(logging.WARNING, logger=kb_sync.logger.name):
        doc_ids = await kb_sync.sync_github_repo(
            db=db,
            kb=kb,
            user_id=uuid.uuid4(),
            repo="owner/repo",
            branch="main",
            statement_timeout_secs=1800,
        )

    # Both ids stay in the stale-delete keep-set (protecting the surviving
    # rows of rolled-back UPDATED docs from deletion) ...
    keep_set = kb_sync._delete_stale_documents.await_args.args[3]
    assert {str(pre_doc_id), str(post_doc_id)} <= keep_set

    # ... but the returned document ids only contain rows that persisted:
    # the rolled-back NEW doc must not leak into SyncResponse.document_ids
    # or inflate documents_synced / last_sync_documents.
    assert doc_ids == [str(post_doc_id)]
    db.execute.assert_awaited_once()

    # rollback() was called exactly once to recover the deactivated session
    assert rollback_calls[0] == 1

    # The rolled-back pre-failure embedding id was dropped — only the
    # post-recovery doc is enqueued for embedding.
    kb_sync.enqueue_documents.assert_awaited_once_with([str(post_doc_id)])

    # The expired kb instance was refreshed so subsequent kb.id / kb.name
    # reads don't raise MissingGreenlet on the async driver.
    db.refresh.assert_awaited_once_with(kb)

    # The transaction-scoped statement_timeout was re-applied after the
    # rollback ended the transaction that carried the SET LOCAL override.
    apply_timeout.assert_awaited_once_with(db, 1800)

    # The original error was logged for the failing file — not a cascade
    # "invalid transaction" error that would mask the root cause
    warning_messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("broken.md" in msg for msg in warning_messages)
    assert not any("invalid transaction" in msg.lower() for msg in warning_messages)


@pytest.mark.asyncio
async def test_sync_github_repo_clean_savepoint_rollback_no_full_rollback(
    monkeypatch,
):
    """When begin_nested() rolls back the savepoint cleanly (the common case),
    the session remains active and no full rollback is issued — preserving the
    uncommitted progress from files that were already processed in this batch."""
    tree = _make_httpx_response(
        200,
        {
            "tree": [
                {"type": "blob", "path": "bad.md", "size": 100},
                {"type": "blob", "path": "good.md", "size": 100},
            ]
        },
    )
    client = _FakeAsyncClient(
        [tree, _make_raw_response(200, "content"), _make_raw_response(200, "content")]
    )
    _patch_github_sync_plumbing(monkeypatch, client)

    good_doc_id = uuid.uuid4()
    upsert_call_count = [0]

    async def _upsert_side_effect(*args, **kwargs):
        upsert_call_count[0] += 1
        if upsert_call_count[0] == 1:
            raise RuntimeError("unique constraint violation")
        return SimpleNamespace(id=good_doc_id), True

    monkeypatch.setattr(kb_sync, "_upsert_document", _upsert_side_effect)

    rollback_calls = [0]

    async def _rollback():
        rollback_calls[0] += 1

    # sync_session.is_active stays True throughout (savepoint cleaned up OK)
    db = SimpleNamespace(
        commit=AsyncMock(),
        flush=AsyncMock(),
        rollback=_rollback,
        sync_session=SimpleNamespace(is_active=True),
        begin_nested=_AsyncNullContext,
    )

    kb = SimpleNamespace(id=uuid.uuid4(), name="test-kb")
    doc_ids = await kb_sync.sync_github_repo(
        db=db,
        kb=kb,
        user_id=uuid.uuid4(),
        repo="owner/repo",
        branch="main",
    )

    # Healthy file was processed
    assert str(good_doc_id) in doc_ids
    # No full rollback — session was healthy, in-session progress is preserved
    assert rollback_calls[0] == 0


# ---------------------------------------------------------------------------
# Website crawler — malformed hrefs are skipped instead of crawled
# ---------------------------------------------------------------------------


async def _run_href_extraction_crawl(
    monkeypatch,
    hrefs: list[str],
    start_url: str = "https://producttalk.org/vibe-coding-best-practices",
) -> list[str]:
    """Crawl a seed page whose anchors are *hrefs* and return every URL the
    crawler actually tried to fetch (beyond the seed)."""
    html = "".join(f'<a href="{h}">x</a>' for h in hrefs)
    seed = _resp(200, headers={"content-type": "text/html"}, text=html)
    page = _resp(200, headers={"content-type": "text/html"}, text="")

    fetch_calls: list[str] = []

    async def _fake_fetch(_client, current_url, **_kwargs):
        fetch_calls.append(current_url)
        return (seed if len(fetch_calls) == 1 else page, False, False)

    cleanup_db = SimpleNamespace(get=AsyncMock(return_value=None), commit=AsyncMock())

    monkeypatch.setattr(kb_sync.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(kb_sync.httpx, "AsyncClient", lambda **_k: _DummyClientContext())
    monkeypatch.setattr(kb_sync, "_fetch_with_retry", _fake_fetch)
    monkeypatch.setattr(kb_sync, "enqueue_documents", AsyncMock())
    monkeypatch.setattr(
        "app.services.web_fetch.extract_from_html", lambda _html, url: ("", {})
    )
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: _AsyncSessionContext(cleanup_db)
    )

    await kb_sync.sync_website(
        db=SimpleNamespace(),
        kb=SimpleNamespace(id=uuid.uuid4(), name="docs"),
        url=start_url,
        max_pages=50,
        max_depth=1,
    )
    return fetch_calls[1:]


@pytest.mark.asyncio
async def test_sync_website_skips_href_with_mangled_scheme(monkeypatch):
    """The production bug: href "https;//cursor.com" (semicolon) is treated by
    urljoin as a relative path, producing
    ".../vibe-coding-best-practices/https;/cursor.com" which 404s. Such hrefs
    must be skipped while healthy links on the same page are still crawled."""
    crawled = await _run_href_extraction_crawl(
        monkeypatch,
        hrefs=[
            "https;//cursor.com",
            "https,//also-broken.example",
            "/vibe-coding-best-practices/good-page",
        ],
    )

    assert crawled == [
        "https://producttalk.org/vibe-coding-best-practices/good-page"
    ]
    assert not any("https;" in u or "https," in u for u in crawled)


@pytest.mark.asyncio
async def test_sync_website_skips_path_with_scheme_garbage_segment(monkeypatch):
    """Defense in depth: even a *relative* href embedding a scheme-garbage
    segment (e.g. "/vibe-coding-best-practices/https;/evil") is never queued."""
    crawled = await _run_href_extraction_crawl(
        monkeypatch,
        hrefs=[
            "/vibe-coding-best-practices/https;/evil",
            "/vibe-coding-best-practices/https:/evil",
            "/vibe-coding-best-practices/ok",
        ],
    )

    assert crawled == ["https://producttalk.org/vibe-coding-best-practices/ok"]


@pytest.mark.asyncio
async def test_sync_website_skips_blank_and_unparseable_hrefs(monkeypatch):
    """Blank hrefs and hrefs that fail URL parsing (bad port) are skipped
    without crashing the crawl."""
    crawled = await _run_href_extraction_crawl(
        monkeypatch,
        hrefs=[
            "",
            "   ",
            "https://producttalk.org:99999999/vibe-coding-best-practices/x",
            "/vibe-coding-best-practices/ok",
        ],
    )

    assert crawled == ["https://producttalk.org/vibe-coding-best-practices/ok"]


@pytest.mark.asyncio
async def test_sync_website_still_crawls_wellformed_absolute_same_domain(monkeypatch):
    """Sanity: a well-formed absolute same-domain link is unaffected by the
    malformed-href guards."""
    crawled = await _run_href_extraction_crawl(
        monkeypatch,
        hrefs=["https://producttalk.org/vibe-coding-best-practices/deep"],
    )

    assert crawled == [
        "https://producttalk.org/vibe-coding-best-practices/deep"
    ]


@pytest.mark.asyncio
async def test_sync_website_skips_github_repo_social_subpages(monkeypatch):
    """GitHub repo UI social links are not content pages and should not be
    enqueued by website sync."""
    crawled = await _run_href_extraction_crawl(
        monkeypatch,
        start_url="https://github.com/OpenCTI-Platform/opencti",
        hrefs=[
            "/OpenCTI-Platform/opencti/stargazers",
            "/OpenCTI-Platform/opencti/forks",
            "/OpenCTI-Platform/opencti/watchers",
            "/OpenCTI-Platform/opencti/blob/main/README.md",
        ],
    )

    assert crawled == ["https://github.com/OpenCTI-Platform/opencti/blob/main/README.md"]


# ---------------------------------------------------------------------------
# Website crawler — doubled consecutive path segments are skipped
# ---------------------------------------------------------------------------


def test_has_doubled_path_segment():
    """Unit tests for the _has_doubled_path_segment helper."""
    # Consecutive identical segments → True
    assert kb_sync._has_doubled_path_segment("/.github/.github/LABELS.md")
    assert kb_sync._has_doubled_path_segment("/docs/docs/intro")
    assert kb_sync._has_doubled_path_segment("/a/b/b/c")
    # Case-insensitive
    assert kb_sync._has_doubled_path_segment("/Docs/docs/")
    # Non-consecutive repetition → False
    assert not kb_sync._has_doubled_path_segment("/a/b/a")
    assert not kb_sync._has_doubled_path_segment("/.github/LABELS.md")
    assert not kb_sync._has_doubled_path_segment("/vibe-coding-best-practices/deep")
    # Edge cases: empty, single segment, trailing slash
    assert not kb_sync._has_doubled_path_segment("/")
    assert not kb_sync._has_doubled_path_segment("/a/")
    assert not kb_sync._has_doubled_path_segment("")
    # Allowed pairs (doubled segments in the source's own base URL) are exempt
    assert not kb_sync._has_doubled_path_segment(
        "/grafana/grafana/blob/main/README.md", allowed={"grafana": 1}
    )
    # ... but only for that exact segment — other doubles still trip the guard
    assert kb_sync._has_doubled_path_segment(
        "/grafana/grafana/docs/docs/intro", allowed={"grafana": 1}
    )
    # The exemption is a per-segment BUDGET, not a blanket name pass: a
    # resolved link that re-doubles the same segment beyond the base URL's
    # own count still trips the guard
    assert kb_sync._has_doubled_path_segment(
        "/grafana/grafana/tree/main/grafana/grafana/x", allowed={"grafana": 1}
    )
    # A triple (two consecutive pairs) also exceeds a budget of one
    assert kb_sync._has_doubled_path_segment(
        "/grafana/grafana/grafana/x", allowed={"grafana": 1}
    )


def test_doubled_path_segments():
    """Unit tests for the _doubled_path_segments base-URL budget helper."""
    assert kb_sync._doubled_path_segments("/grafana/grafana") == {"grafana": 1}
    # Case-normalised to lowercase
    assert kb_sync._doubled_path_segments("/Docs/docs/") == {"docs": 1}
    # A triple yields two consecutive pairs
    assert kb_sync._doubled_path_segments("/a/a/a") == {"a": 2}
    assert kb_sync._doubled_path_segments("/a/b/c") == {}
    assert kb_sync._doubled_path_segments("") == {}


@pytest.mark.asyncio
async def test_sync_website_skips_url_with_doubled_path_segment(monkeypatch):
    """Links whose resolved URL contains consecutive identical path segments
    (e.g. /vibe-coding-best-practices/vibe-coding-best-practices/page, the
    class of bug) are skipped so the crawler never
    fetches an almost-certainly-404 URL.  Other links on the same page are
    still crawled."""
    crawled = await _run_href_extraction_crawl(
        monkeypatch,
        hrefs=[
            # Doubled segment repeating the base path — must be skipped
            "/vibe-coding-best-practices/vibe-coding-best-practices/bad-page",
            # Doubled segment deeper in an in-scope path — also skipped (this
            # one passes the base_path filter, so only the doubled-segment
            # guard can reject it)
            "/vibe-coding-best-practices/sub/sub/also-bad",
            # Non-consecutive repetition is fine — still crawled
            "/vibe-coding-best-practices/clean",
        ],
    )

    assert crawled == ["https://producttalk.org/vibe-coding-best-practices/clean"]


@pytest.mark.asyncio
async def test_sync_website_allows_doubled_segment_from_base_url(monkeypatch):
    """A doubled pair that is part of the source's own base URL (e.g. a
    GitHub repo whose org and repo share a name, github.com/grafana/grafana)
    is legitimate for that site and must NOT be skipped — while doubled pairs
    *introduced* during link resolution are still rejected."""
    crawled = await _run_href_extraction_crawl(
        monkeypatch,
        start_url="https://github.com/grafana/grafana",
        hrefs=[
            # Base-URL doubled pair — allowed, still crawled
            "/grafana/grafana/blob/main/README.md",
            # New doubled pair introduced by a relative href — skipped
            "/grafana/grafana/docs/docs/broken",
            # The SAME segment as the base pair, re-doubled deeper in the
            # path — exceeds the base URL's budget of one pair, skipped
            "/grafana/grafana/tree/main/grafana/grafana/broken",
        ],
    )

    assert crawled == ["https://github.com/grafana/grafana/blob/main/README.md"]


# ---------------------------------------------------------------------------
# Website sync: perm-fail raise when all fetches fail with permanent errors
# ---------------------------------------------------------------------------


def _setup_website_crawl_mocks(monkeypatch, *, results, cleanup_db=None):
    """Wire up monkeypatches for sync_website.  Returns the fetch_calls list,
    populated with each fetched URL as the crawl runs."""
    fetch_calls: list[str] = []

    async def _fake_fetch(_client, current_url, **_kwargs):
        fetch_calls.append(current_url)
        return results.pop(0)

    if cleanup_db is None:
        cleanup_db = SimpleNamespace(get=AsyncMock(return_value=None), commit=AsyncMock())

    monkeypatch.setattr(kb_sync.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(kb_sync.httpx, "AsyncClient", lambda **_k: _DummyClientContext())
    monkeypatch.setattr(kb_sync, "_fetch_with_retry", _fake_fetch)
    monkeypatch.setattr(kb_sync, "enqueue_documents", AsyncMock())
    monkeypatch.setattr(
        "app.services.web_fetch.extract_from_html", lambda _html, url: ("", {})
    )
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: _AsyncSessionContext(cleanup_db)
    )
    return fetch_calls


@pytest.mark.asyncio
async def test_sync_website_raises_permanent_error_when_start_url_404s(monkeypatch):
    """A single-URL source whose start URL returns 404 must raise ValueError
    carrying the all-permanent marker so the scheduler's perm-fail quarantine
    can track and disable the source after N consecutive failures."""
    start_url = "https://github.com/OpenCTI-Platform/opencti/stargazers"
    results = [(None, True, False)]  # 404 → permanent failure

    _setup_website_crawl_mocks(monkeypatch, results=results)

    with pytest.raises(
        ValueError, match=kb_sync.WEBSITE_ALL_PERMANENT_FAILURES_MARKER
    ):
        await kb_sync.sync_website(
            db=SimpleNamespace(),
            kb=SimpleNamespace(id=uuid.uuid4(), name="cia-kb"),
            url=start_url,
        )


@pytest.mark.asyncio
async def test_sync_website_raises_permanent_error_when_all_discovered_urls_404(
    monkeypatch,
):
    """A crawl where the seed page links to several pages that all return 404
    must raise ValueError — no silent 0-doc 'success' that resets the counter.
    """
    start_url = "https://www.customersuccesscollective.com"
    child_urls = [
        f"{start_url}/product-marketing-framework/",
        f"{start_url}/tag/meetup/",
        f"{start_url}/tag/pmm-talks/",
    ]
    html = "".join(f'<a href="{u}">x</a>' for u in child_urls)
    seed = _resp(200, headers={"content-type": "text/html"}, text=html)
    # Seed succeeds (navigation page, no extractable text); all 3 children 404.
    results = [(seed, False, False)] + [(None, True, False)] * len(child_urls)

    _setup_website_crawl_mocks(monkeypatch, results=results)

    with pytest.raises(
        ValueError, match=kb_sync.WEBSITE_ALL_PERMANENT_FAILURES_MARKER
    ):
        await kb_sync.sync_website(
            db=SimpleNamespace(),
            kb=SimpleNamespace(id=uuid.uuid4(), name="cs-kb"),
            url=start_url,
        )


@pytest.mark.asyncio
async def test_sync_website_no_raise_when_any_doc_synced(monkeypatch):
    """If even one page was successfully synced, sync_website must NOT raise
    even though other pages returned 404.  Partial success is not a config
    error."""
    start_url = "https://docs.xtmone.io"
    child_404_url = f"{start_url}/gone"
    html = f'<a href="{child_404_url}">x</a>'
    seed = _resp(200, headers={"content-type": "text/html"}, text=html)
    child_404 = (None, True, False)

    # Seed returns extractable text → one doc synced.
    page_db = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
        commit=AsyncMock(),
    )
    cleanup_db = SimpleNamespace(get=AsyncMock(return_value=None), commit=AsyncMock())

    results = [(seed, False, False), child_404]

    async def _fake_upsert(*_a, **_kw):
        doc = SimpleNamespace(id=uuid.uuid4())
        return doc, True

    fetch_calls: list[str] = []

    async def _fake_fetch(_client, current_url, **_kwargs):
        fetch_calls.append(current_url)
        return results.pop(0)

    monkeypatch.setattr(kb_sync.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(kb_sync.httpx, "AsyncClient", lambda **_k: _DummyClientContext())
    monkeypatch.setattr(kb_sync, "_fetch_with_retry", _fake_fetch)
    monkeypatch.setattr(kb_sync, "_upsert_document", _fake_upsert)
    monkeypatch.setattr(kb_sync, "enqueue_documents", AsyncMock())
    monkeypatch.setattr(
        "app.services.web_fetch.extract_from_html",
        lambda _html, url: ("Some real content here", {}),
    )
    # sync_website opens exactly two fresh sessions in order: one for the
    # single synced page, then one for the stale-cleanup pass. An ordered
    # queue is robust to internal refactors (unlike inferring which session
    # to serve from mock await counts) and fails loudly (StopIteration) if
    # an unexpected extra session is requested.
    session_queue = iter([page_db, cleanup_db])
    monkeypatch.setattr(
        "app.core.database.async_session",
        lambda: _AsyncSessionContext(next(session_queue)),
    )

    # Must NOT raise — one doc was synced successfully.
    doc_ids = await kb_sync.sync_website(
        db=SimpleNamespace(),
        kb=SimpleNamespace(id=uuid.uuid4(), name="partial-kb"),
        url=start_url,
        max_depth=1,
    )
    assert len(doc_ids) >= 1


@pytest.mark.asyncio
async def test_sync_website_no_raise_on_transient_only_failures_no_docs(monkeypatch):
    """All-transient failures (site temporarily down) must NOT raise the
    perm-fail error — only permanent 4xx errors trigger the quarantine guard.
    """
    start_url = "https://docs.xtmone.io"
    # Single URL that returns a transient failure (5xx / timeout).
    results = [(None, False, True)]

    _setup_website_crawl_mocks(monkeypatch, results=results)

    # Must not raise — transient failure has total_permanent_failures == 0.
    doc_ids = await kb_sync.sync_website(
        db=SimpleNamespace(),
        kb=SimpleNamespace(id=uuid.uuid4(), name="down-kb"),
        url=start_url,
    )
    assert doc_ids == []


@pytest.mark.asyncio
async def test_sync_website_mixed_failures_raise_non_permanent_error(monkeypatch):
    """A zero-doc crawl with BOTH permanent and transient failures must still
    raise (no destructive stale-cleanup, run recorded as a failure) but the
    message must NOT carry the "404 Not Found" permanent marker — a run
    tainted by a possible outage may not increment the strict
    sync_perm_fail_count budget."""
    start_url = "https://docs.xtmone.io"
    child_urls = [f"{start_url}/gone", f"{start_url}/flaky"]
    html = "".join(f'<a href="{u}">x</a>' for u in child_urls)
    seed = _resp(200, headers={"content-type": "text/html"}, text=html)
    # Seed succeeds (nav page, no extractable text); one child 404s
    # (permanent), the other times out (transient).
    results = [(seed, False, False), (None, True, False), (None, False, True)]

    _setup_website_crawl_mocks(monkeypatch, results=results)

    with pytest.raises(ValueError) as exc_info:
        await kb_sync.sync_website(
            db=SimpleNamespace(),
            kb=SimpleNamespace(id=uuid.uuid4(), name="mixed-kb"),
            url=start_url,
            max_depth=1,
        )
    # Mirror _is_permanent_sync_error's (case-insensitive) markers: none may
    # appear, so the scheduler classifies this run as non-permanent.
    message = str(exc_info.value).lower()
    assert kb_sync.WEBSITE_ALL_PERMANENT_FAILURES_MARKER.lower() not in message
    assert "404 not found" not in message
    assert "410 gone" not in message
    assert "temporarily unavailable" in message


@pytest.mark.asyncio
async def test_sync_website_throttled_recovery_downgrades_permanent_raise(monkeypatch):
    """A zero-doc crawl whose only *failures* were permanent but which needed a
    throttled recovery (success only after transient back-off) is outage- or
    rate-limit-tainted: it must raise the plain (non-permanent) error, never
    the all-permanent marker that feeds the perm-fail budget."""
    start_url = "https://docs.xtmone.io"
    child_url = f"{start_url}/gone"
    html = f'<a href="{child_url}">x</a>'
    seed = _resp(200, headers={"content-type": "text/html"}, text=html)
    # Seed succeeds only after back-off (throttled=True, nav page with no
    # extractable text); the single child 404s permanently.
    results = [(seed, False, True), (None, True, False)]

    _setup_website_crawl_mocks(monkeypatch, results=results)

    with pytest.raises(ValueError) as exc_info:
        await kb_sync.sync_website(
            db=SimpleNamespace(),
            kb=SimpleNamespace(id=uuid.uuid4(), name="throttled-kb"),
            url=start_url,
            max_depth=1,
        )
    message = str(exc_info.value).lower()
    assert kb_sync.WEBSITE_ALL_PERMANENT_FAILURES_MARKER.lower() not in message
    assert "404 not found" not in message
    assert "temporarily unavailable" in message
