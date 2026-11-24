"""A sustained stale integration reference fails the run and pauses the assignment.

An hourly assignment kept naming a Notion database its integration could no
longer see (404 object_not_found) and failed every run, forever - the ID even
changed between reports, so it was the assignment's configuration. The
decision: a SUSTAINED stale reference becomes a structured run failure the
owner is told about once, and the assignment pauses after N consecutive such
scheduled runs.

Covered:
  * the channel (``app.integrations.stale_references``): a no-op outside a
    collecting run, one entry per resource, withdrawn once the resource
    answers, scoped to the run's own integrations;
  * the Notion detector reports only a SUSTAINED miss (the same integration +
    resource missing in two separate runs), once per run per resource, never a
    first-time 404, and a chat turn (no collector) sees exactly what it did;
    never an id the run only read (relation targets, child blocks, previews),
    never a page parent the create / move type probes missed, never an id
    Notion calls "a page, not a database"; the awaited bump is bounded and
    holds no concurrency slot;
  * the classifier: a structural ``stale_integration_reference`` cause;
  * the engine: the run is recorded failed through the classifier with the
    agent's summary and files kept, the action goes back through the bounded
    pool (not free), nothing is retried, the streak is tracked on a session of
    its own; a run without the signal is unchanged;
  * the streak: N-1 -> reported, not paused; N -> paused, audited, notified
    once; a scheduled success or another cause resets it; 0 disables;
    overlapping runs pause once; resuming starts afresh; manual, webhook and
    event runs neither extend nor end it, nor restart an episode; a per-user
    fan-out reads each user's runs apart;
  * the pause state (``last_paused_at`` survives a resume) and its system-actor
    audit row; the setting's bounds.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.providers.notion._common import (
    _STALE_REF_REDIS_TTL_SECS,
    _STALE_REF_WARN_THRESHOLD,
    NotionContext,
)
from app.integrations.stale_references import (
    StaleIntegrationReferenceError,
    StaleReference,
    collect_stale_references,
    confirm_reference_alive,
    report_stale_reference,
)
from app.services.failure_causes import (
    BILLABLE_CANDIDATE_CAUSES,
    FailureCause,
    FailureDetermination,
    classify_run_failure,
)
from tests.agentic_gate_stub import stub_agentic_gate


@pytest.fixture(autouse=True)
def _agentic_ai_available(monkeypatch):
    """A run asks the background gate at its start; these tests are
    about what the run does once agentic AI may run."""
    stub_agentic_gate(monkeypatch)


INTEGRATION_ID = "ea21ab20-36fc-4ed1-acd3-fa78138110bb"
DATABASE_ID = "26f8fce1-7f2a-8042-ac18-000b8646904c"
DATABASE_KEY = DATABASE_ID.replace("-", "")
DATABASE_URL = f"https://api.notion.com/v1/databases/{DATABASE_ID}"


def _reference(**overrides) -> StaleReference:
    values = {
        "provider": "notion",
        "provider_label": "Notion",
        "integration_id": INTEGRATION_ID,
        "resource_type": "database",
        "resource_id": DATABASE_ID,
        "failing_runs": 3,
    }
    values.update(overrides)
    return StaleReference(**values)


def _report(**overrides) -> None:
    ref = _reference(**overrides)
    report_stale_reference(
        provider=ref.provider,
        provider_label=ref.provider_label,
        integration_id=ref.integration_id,
        resource_type=ref.resource_type,
        resource_id=ref.resource_id,
        failing_runs=ref.failing_runs,
        key=overrides.get("key", ref.resource_id.replace("-", "")),
    )


def _error(**overrides) -> StaleIntegrationReferenceError:
    return StaleIntegrationReferenceError(
        [_reference(**overrides)],
        integration_names={INTEGRATION_ID: "Filigran Copilot - Prod"},
    )


# ---------------------------------------------------------------------------
# The channel
# ---------------------------------------------------------------------------


def test_a_report_outside_a_collecting_run_is_a_no_op():
    """A chat turn binds nothing: reporting must neither raise nor leak."""
    _report()
    confirm_reference_alive(provider="notion", integration_id=INTEGRATION_ID, key=DATABASE_KEY)
    with collect_stale_references() as collector:
        pass
    assert collector.references == ()


def test_one_entry_per_resource_with_its_highest_count():
    with collect_stale_references() as collector:
        _report(failing_runs=2)
        _report(resource_type="page", failing_runs=4)
        _report(failing_runs=3)
    assert len(collector.references) == 1
    assert collector.references[0].failing_runs == 4


def test_a_resource_that_answered_is_withdrawn_and_stays_withdrawn():
    with collect_stale_references() as collector:
        _report()
        confirm_reference_alive(provider="notion", integration_id=INTEGRATION_ID, key=DATABASE_KEY)
        _report()
    assert collector.references == ()


def test_only_the_runs_own_integrations_are_collected():
    """A consulted agent's integrations are its configuration, not this run's."""
    other = str(uuid.uuid4())
    with collect_stale_references([INTEGRATION_ID]) as collector:
        _report(integration_id=other)
        _report()
    assert [ref.integration_id for ref in collector.references] == [INTEGRATION_ID]


@pytest.mark.asyncio
async def test_a_parallel_tool_batch_reports_into_the_same_collector():
    """Tasks copy the context at creation, so each call of a batch reaches the run's collector."""

    async def _tool(resource_id: str) -> None:
        await asyncio.sleep(0)
        _report(resource_id=resource_id)

    with collect_stale_references() as collector:
        await asyncio.gather(
            asyncio.ensure_future(_tool(DATABASE_ID)),
            asyncio.ensure_future(_tool(str(uuid.uuid4()))),
        )
    assert len(collector.references) == 2


def test_the_error_names_provider_resource_integration_and_remedy():
    message = str(_error())
    assert "Stale integration reference" in message
    assert "Notion database" in message
    assert DATABASE_ID in message
    assert '"Filigran Copilot - Prod"' in message
    assert "not found in 3 separate runs" in message
    assert "Share it with that integration in Notion" in message
    assert "update the assignment" in message


def test_the_error_lists_every_reference_and_falls_back_to_the_integration_id():
    other_integration = str(uuid.uuid4())
    error = StaleIntegrationReferenceError(
        [_reference(), _reference(resource_type="page", resource_id="p-1", integration_id=other_integration)],
        integration_names={INTEGRATION_ID: "Prod"},
    )
    message = str(error)
    assert f"- the Notion database {DATABASE_ID}" in message
    assert f'the Notion page p-1 through the integration "{other_integration}"' in message


# ---------------------------------------------------------------------------
# The Notion detector
# ---------------------------------------------------------------------------


def _counting_redis(initial: int):
    """Fake Redis: the INCR counter starts at *initial*; no counter yet at pre-check."""
    counts: dict[str, int] = {}

    async def _incr(key: str) -> int:
        counts[key] = counts.get(key, initial) + 1
        return counts[key]

    pipe = MagicMock()
    pipe.execute = AsyncMock(return_value=[str(initial).encode() if initial else None, _STALE_REF_REDIS_TTL_SECS])
    fake = MagicMock()
    fake.incr = _incr
    fake.expire = AsyncMock()
    fake.delete = AsyncMock(return_value=1)
    fake.pipeline = MagicMock(return_value=pipe)
    fake._counts = counts
    return fake


def _redis(fake):
    return patch("app.core.redis.redis_client", new_callable=AsyncMock, return_value=fake)


async def _call(ctx: NotionContext, url: str, *, result):
    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
        return_value=result,
    ):
        async with ctx.client() as client:
            return await ctx.request(client, "get", url, "Notion", "resolve data source")


_NOT_FOUND = (None, "Error: Notion resource not found while trying to resolve data source.")


@pytest.mark.asyncio
async def test_a_first_time_404_is_not_reported():
    """It can be the agent's own typo: one failing run is not a stale reference."""
    fake_redis = _counting_redis(initial=0)
    with _redis(fake_redis), collect_stale_references([INTEGRATION_ID]) as collector:
        ctx = NotionContext("token", integration_id=INTEGRATION_ID)
        _resp, err = await _call(ctx, DATABASE_URL, result=_NOT_FOUND)
    assert err
    assert collector.references == ()


@pytest.mark.asyncio
async def test_a_sustained_404_is_reported_once_per_run_per_resource():
    """The second separate run crosses the detector's threshold and reports.

    Probing the same dead id as a database, a page and a block is still one
    stale reference - and the report is in the collector the moment the call
    returns, not whenever a fire-and-forget task gets scheduled.
    """
    fake_redis = _counting_redis(initial=_STALE_REF_WARN_THRESHOLD - 1)
    with _redis(fake_redis), collect_stale_references([INTEGRATION_ID]) as collector:
        ctx = NotionContext("token", integration_id=INTEGRATION_ID)
        await _call(ctx, DATABASE_URL, result=_NOT_FOUND)
        reported_on_return = list(collector.references)
        await _call(ctx, f"https://api.notion.com/v1/pages/{DATABASE_ID}", result=_NOT_FOUND)
        await _call(ctx, f"https://api.notion.com/v1/blocks/{DATABASE_ID}/children", result=_NOT_FOUND)
    assert len(reported_on_return) == 1
    (ref,) = collector.references
    assert ref.provider == "notion"
    assert ref.integration_id == INTEGRATION_ID
    assert ref.resource_type == "database"
    assert ref.resource_id == DATABASE_ID
    assert ref.failing_runs == _STALE_REF_WARN_THRESHOLD
    assert list(fake_redis._counts.values()) == [_STALE_REF_WARN_THRESHOLD]


@pytest.mark.asyncio
async def test_a_suppressed_call_reports_the_sustained_reference():
    """Past the threshold the API is not even called - the run used it again all the same."""
    fake_redis = _counting_redis(initial=_STALE_REF_WARN_THRESHOLD + 1)
    with (
        _redis(fake_redis),
        collect_stale_references([INTEGRATION_ID]) as collector,
        patch(
            "app.integrations.providers.notion._common.safe_request",
            new_callable=AsyncMock,
        ) as live_call,
    ):
        ctx = NotionContext("token", integration_id=INTEGRATION_ID)
        async with ctx.client() as client:
            _resp, err = await ctx.request(client, "get", DATABASE_URL, "Notion", "resolve data source")
    live_call.assert_not_called()
    assert "suppressed" in err
    (ref,) = collector.references
    assert ref.failing_runs == _STALE_REF_WARN_THRESHOLD + 1


@pytest.mark.asyncio
async def test_an_id_that_answers_as_another_type_is_withdrawn_and_its_counter_cleared():
    """A data source id is first probed as a database (404) - it is alive, not stale."""
    fake_redis = _counting_redis(initial=_STALE_REF_WARN_THRESHOLD - 1)
    ok = httpx.Response(200, json={"object": "data_source"})
    with _redis(fake_redis), collect_stale_references([INTEGRATION_ID]) as collector:
        ctx = NotionContext("token", integration_id=INTEGRATION_ID)
        await _call(ctx, DATABASE_URL, result=_NOT_FOUND)
        await _call(ctx, f"https://api.notion.com/v1/data_sources/{DATABASE_ID}", result=(ok, None))
        await asyncio.sleep(0)
    assert collector.references == ()
    fake_redis.delete.assert_awaited()


@pytest.mark.asyncio
async def test_a_first_404_that_answers_later_in_the_run_leaves_no_counter():
    """The bump creates the counter the self-heal then clears (it used to linger)."""
    fake_redis = _counting_redis(initial=0)
    ok = httpx.Response(200, json={"object": "data_source"})
    with _redis(fake_redis):
        ctx = NotionContext("token", integration_id=INTEGRATION_ID)
        await _call(ctx, DATABASE_URL, result=_NOT_FOUND)
        await _call(ctx, f"https://api.notion.com/v1/data_sources/{DATABASE_ID}", result=(ok, None))
        await asyncio.sleep(0)
    fake_redis.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_chat_turn_gets_exactly_the_tool_result_it_always_got():
    """No collector is bound in chat: the sustained 404 changes nothing it sees."""
    fake_redis = _counting_redis(initial=_STALE_REF_WARN_THRESHOLD - 1)
    with _redis(fake_redis):
        ctx = NotionContext("token", integration_id=INTEGRATION_ID)
        _resp, err = await _call(ctx, DATABASE_URL, result=_NOT_FOUND)
    assert err.startswith(_NOT_FOUND[1])
    assert "Action required" in err


# ---------------------------------------------------------------------------
# The Notion detector: a reference the run addressed, and only a missing one
# ---------------------------------------------------------------------------

RELATED_PAGE_ID = "1bb8fce1-7f2a-8034-a80a-e6765e22ab22"
PARENT_PAGE_ID = "2cc8fce1-7f2a-8034-a80a-e6765e22ab33"
MOVED_PAGE_ID = "3dd8fce1-7f2a-8034-a80a-e6765e22ab44"


def _notion_tool(tool_name: str):
    from app.integrations.providers.notion import build_tools
    from app.services.integration_tools import _SafeIntegrationExecutor

    executor = _SafeIntegrationExecutor()
    build_tools("token", executor, integration_id=INTEGRATION_ID)
    return executor._handlers[tool_name]


def _patched_notion(fake_safe_request):
    return patch("app.integrations.providers.notion._common.safe_request", new=fake_safe_request)


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", [_STALE_REF_WARN_THRESHOLD - 1, _STALE_REF_WARN_THRESHOLD + 1])
async def test_a_relation_target_the_integration_cannot_see_is_never_reported(initial):
    """A related page in a database not shared with the integration 404s on every run.

    It is data read off the row, not a reference the run addressed - the
    lookup even swallows the miss, so the agent never sees it - and reporting
    it would fail, then pause, a healthy assignment.
    """
    fake_redis = _counting_redis(initial=initial)
    props = {"Client": {"type": "relation", "relation": [{"id": RELATED_PAGE_ID}]}}
    with (
        _redis(fake_redis),
        collect_stale_references([INTEGRATION_ID]) as collector,
        patch(
            "app.integrations.providers.notion._common.safe_request",
            new_callable=AsyncMock,
            return_value=_NOT_FOUND,
        ),
    ):
        ctx = NotionContext("token", integration_id=INTEGRATION_ID)
        async with ctx.client() as client:
            titles = await ctx.resolve_relation_titles(client, props)
    assert titles == {RELATED_PAGE_ID: RELATED_PAGE_ID[:8]}
    assert collector.references == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", [_STALE_REF_WARN_THRESHOLD - 1, _STALE_REF_WARN_THRESHOLD + 1])
@pytest.mark.parametrize("incidental", ["relation_title", "page_preview"])
async def test_a_page_the_run_addresses_is_reported_whatever_an_incidental_read_found_first(
    initial, incidental
):
    """An incidental read of a sustained missing page (a relation title, a
    preview) is not reported - but the same page ADDRESSED later in the run is,
    whether the read left it in the in-run missing cache (same path), bumped
    the once-per-run counter (another path) or had it suppressed."""
    fake_redis = _counting_redis(initial=initial)
    props = {"Client": {"type": "relation", "relation": [{"id": RELATED_PAGE_ID}]}}
    page_url = f"https://api.notion.com/v1/pages/{RELATED_PAGE_ID}"
    with (
        _redis(fake_redis),
        collect_stale_references([INTEGRATION_ID]) as collector,
        patch(
            "app.integrations.providers.notion._common.safe_request",
            new_callable=AsyncMock,
            return_value=_NOT_FOUND,
        ),
    ):
        ctx = NotionContext("token", integration_id=INTEGRATION_ID)
        async with ctx.client() as client:
            if incidental == "relation_title":
                await ctx.resolve_relation_titles(client, props)
            else:
                await ctx.fetch_page_preview(client, RELATED_PAGE_ID)
            assert collector.references == ()
            _resp, err = await ctx.request(client, "get", page_url, "Notion", "read page")
    assert err
    (ref,) = collector.references
    assert ref.resource_id == RELATED_PAGE_ID
    assert ref.resource_type == "page"


@pytest.mark.asyncio
async def test_request_meta_reports_an_addressed_page_an_incidental_read_cached():
    """Same ordering through ``request_meta`` (the comments tools)."""
    fake_redis = _counting_redis(initial=_STALE_REF_WARN_THRESHOLD - 1)
    props = {"Client": {"type": "relation", "relation": [{"id": RELATED_PAGE_ID}]}}
    with (
        _redis(fake_redis),
        collect_stale_references([INTEGRATION_ID]) as collector,
        patch(
            "app.integrations.providers.notion._common.safe_request",
            new_callable=AsyncMock,
            return_value=_NOT_FOUND,
        ),
    ):
        ctx = NotionContext("token", integration_id=INTEGRATION_ID)
        async with ctx.client() as client:
            await ctx.resolve_relation_titles(client, props)
            _resp, err, status, code = await ctx.request_meta(
                client, "get", f"https://api.notion.com/v1/pages/{RELATED_PAGE_ID}", "Notion", "read"
            )
    assert err
    assert (status, code) == (404, "object_not_found")
    (ref,) = collector.references
    assert ref.resource_id == RELATED_PAGE_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("read", ["fetch_page_preview", "fetch_all_blocks"])
async def test_incidental_reads_are_never_reported(read):
    """Search hits, query rows and child blocks are ids the run read, not ones it addressed."""
    fake_redis = _counting_redis(initial=_STALE_REF_WARN_THRESHOLD - 1)
    with (
        _redis(fake_redis),
        collect_stale_references([INTEGRATION_ID]) as collector,
        patch(
            "app.integrations.providers.notion._common.safe_request",
            new_callable=AsyncMock,
            return_value=_NOT_FOUND,
        ),
    ):
        ctx = NotionContext("token", integration_id=INTEGRATION_ID)
        async with ctx.client() as client:
            await getattr(ctx, read)(client, RELATED_PAGE_ID)
    assert collector.references == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("database_probe", ["page_400", "not_found_404"])
async def test_creating_a_page_under_a_parent_page_is_never_a_stale_reference(database_probe):
    """The parent-type probes miss on every run for a PAGE parent (the
    data-source probe 404s) - and the create itself proves the parent exists.

    The daily "create today's report under page X" assignment must not fail
    from its second run and pause from its fourth.
    """
    fake_redis = _counting_redis(initial=_STALE_REF_WARN_THRESHOLD - 1)
    dashless = PARENT_PAGE_ID.replace("-", "")

    async def fake_safe_request(_client, method, url, _provider, _action, **_kwargs):
        if method == "get" and f"/databases/{PARENT_PAGE_ID}" in url:
            if database_probe == "page_400":
                return None, (
                    "Error: Notion rejected the request to detect parent type - bad request. "
                    f"Provided database_id {dashless} is a page, not a database."
                )
            return None, "Error: Notion resource not found while trying to detect parent type."
        if method == "get" and f"/data_sources/{PARENT_PAGE_ID}" in url:
            return None, "Error: Notion resource not found while trying to check data source."
        if method == "get" and url.endswith(f"/pages/{PARENT_PAGE_ID}"):
            return httpx.Response(200, json={"object": "page", "id": PARENT_PAGE_ID}), None
        if method == "post" and url.endswith("/v1/pages"):
            return httpx.Response(200, json={"id": "new-page", "url": "https://notion.so/new-page"}), None
        raise AssertionError(f"Unexpected request: {method} {url}")

    handler = _notion_tool("create_notion_page")
    with _redis(fake_redis), collect_stale_references([INTEGRATION_ID]) as collector, _patched_notion(fake_safe_request):
        result = await handler(parent_id=PARENT_PAGE_ID, title="Daily report")
        await asyncio.sleep(0)
    assert result.startswith("Page created:"), result
    assert collector.references == ()
    # The counter the probes grew is cleared, so it never reaches the threshold.
    fake_redis.delete.assert_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["create_notion_page", "move_notion_page"])
async def test_a_page_parent_is_verified_before_the_write_so_a_failed_write_reports_nothing(tool):
    """Both type probes miss for a page parent; if the write then fails for an
    unrelated reason (a bad property), the parent must not stay reported - it
    is checked with a GET on the page itself, the one miss that means gone."""
    fake_redis = _counting_redis(initial=_STALE_REF_WARN_THRESHOLD - 1)
    verified: list[str] = []

    async def fake_safe_request(_client, method, url, _provider, _action, **_kwargs):
        if method == "get" and url.endswith(f"/pages/{PARENT_PAGE_ID}"):
            verified.append(url)
            return httpx.Response(200, json={"object": "page", "id": PARENT_PAGE_ID}), None
        if method == "get" and ("/databases/" in url or "/data_sources/" in url):
            miss = "Error: Notion resource not found while trying to detect parent type."
            if _kwargs.get("expected_statuses") is not None:
                return httpx.Response(404, json={"object": "error", "status": 404}), miss
            return None, miss
        if method == "post":
            return None, "Error: Notion rejected the request - bad request. Status is not a property."
        raise AssertionError(f"Unexpected request: {method} {url}")

    handler = _notion_tool(tool)
    kwargs = (
        {"parent_id": PARENT_PAGE_ID, "title": "Daily report"}
        if tool == "create_notion_page"
        else {"page_id": MOVED_PAGE_ID, "new_parent_id": PARENT_PAGE_ID}
    )
    with _redis(fake_redis), collect_stale_references([INTEGRATION_ID]) as collector, _patched_notion(fake_safe_request):
        result = await handler(**kwargs)
        await asyncio.sleep(0)
    assert result.startswith("Error"), result
    assert verified, "the page parent was not verified"
    assert collector.references == ()


@pytest.mark.asyncio
async def test_a_parent_that_is_really_gone_is_still_reported():
    """Both probes miss and so does the create: that parent IS a stale reference."""
    fake_redis = _counting_redis(initial=_STALE_REF_WARN_THRESHOLD - 1)

    async def fake_safe_request(_client, method, url, _provider, _action, **_kwargs):
        if method == "get":
            return None, "Error: Notion resource not found while trying to detect parent type."
        return None, "Error: Notion resource not found while trying to create page."

    handler = _notion_tool("create_notion_page")
    with _redis(fake_redis), collect_stale_references([INTEGRATION_ID]) as collector, _patched_notion(fake_safe_request):
        result = await handler(parent_id=PARENT_PAGE_ID, title="Daily report")
    assert result.startswith("Error"), result
    (ref,) = collector.references
    assert ref.resource_id == PARENT_PAGE_ID


@pytest.mark.asyncio
async def test_moving_a_page_under_a_parent_page_is_never_a_stale_reference():
    """Both move-parent probes are EXPECTED to 404 for a page parent; the move proves it exists."""
    fake_redis = _counting_redis(initial=_STALE_REF_WARN_THRESHOLD - 1)

    async def fake_safe_request(_client, method, url, _provider, _action, **_kwargs):
        if method == "get" and ("/databases/" in url or "/data_sources/" in url):
            return (
                httpx.Response(404, json={"object": "error", "status": 404}),
                "Error: Notion resource not found while trying to detect move parent type.",
            )
        if method == "get" and url.endswith(f"/pages/{PARENT_PAGE_ID}"):
            return httpx.Response(200, json={"object": "page", "id": PARENT_PAGE_ID}), None
        if method == "post" and url.endswith(f"/pages/{MOVED_PAGE_ID}/move"):
            return httpx.Response(200, json={"id": MOVED_PAGE_ID, "url": "https://notion.so/moved"}), None
        raise AssertionError(f"Unexpected request: {method} {url}")

    handler = _notion_tool("move_notion_page")
    with _redis(fake_redis), collect_stale_references([INTEGRATION_ID]) as collector, _patched_notion(fake_safe_request):
        result = await handler(page_id=MOVED_PAGE_ID, new_parent_id=PARENT_PAGE_ID)
        await asyncio.sleep(0)
    assert result.startswith("Page moved:"), result
    assert collector.references == ()


@pytest.mark.asyncio
async def test_a_page_used_as_a_database_is_not_a_missing_resource():
    """Notion's 400 "is a page, not a database" proves the id EXISTS - as a page.

    The data-source probe before it 404s all the same; reporting that id as
    missing would tell the owner to share a page that is shared.
    """
    fake_redis = _counting_redis(initial=_STALE_REF_WARN_THRESHOLD - 1)
    dashless = PARENT_PAGE_ID.replace("-", "")

    async def fake_safe_request(_client, method, url, _provider, _action, **_kwargs):
        if f"/data_sources/{PARENT_PAGE_ID}" in url:
            return (
                httpx.Response(404, json={"object": "error", "status": 404}),
                "Error: Notion resource not found while trying to retrieve data source.",
            )
        if f"/databases/{PARENT_PAGE_ID}" in url:
            message = f"Provided database_id {dashless} is a page, not a database."
            return (
                httpx.Response(
                    400,
                    json={"object": "error", "status": 400, "code": "validation_error", "message": message},
                ),
                f"Error: Notion rejected the request to retrieve database - bad request. {message}",
            )
        raise AssertionError(f"Unexpected request: {method} {url}")

    handler = _notion_tool("get_notion_database")
    with _redis(fake_redis), collect_stale_references([INTEGRATION_ID]) as collector, _patched_notion(fake_safe_request):
        result = await handler(database_id=PARENT_PAGE_ID)
        await asyncio.sleep(0)
    assert "is a Notion page, not a database" in result
    assert collector.references == ()


@pytest.mark.asyncio
async def test_a_hung_redis_never_stalls_the_notion_call(monkeypatch):
    """The bump is awaited now: a Redis that never answers costs the report, never the call."""
    from app.integrations.providers.notion import _common

    monkeypatch.setattr(_common, "_STALE_REF_BUMP_TIMEOUT_SECS", 0.05)
    fake_redis = _counting_redis(initial=0)

    async def _hang(_key):
        await asyncio.Event().wait()

    fake_redis.incr = _hang
    with _redis(fake_redis), collect_stale_references([INTEGRATION_ID]) as collector:
        ctx = NotionContext("token", integration_id=INTEGRATION_ID)
        _resp, err = await asyncio.wait_for(_call(ctx, DATABASE_URL, result=_NOT_FOUND), timeout=2)
    assert err.startswith(_NOT_FOUND[1])
    assert collector.references == ()


@pytest.mark.asyncio
async def test_the_bump_does_not_hold_a_notion_concurrency_slot():
    """A slow counter write must not queue the next Notion call behind it."""
    fake_redis = _counting_redis(initial=0)
    entered, release = asyncio.Event(), asyncio.Event()
    slot_free_during_bump: list[bool] = []
    ctx = NotionContext("token", integration_id=INTEGRATION_ID)
    ctx._sem = asyncio.Semaphore(1)

    async def _slow_incr(_key):
        entered.set()
        slot_free_during_bump.append(not ctx._sem.locked())
        await release.wait()
        return 1

    fake_redis.incr = _slow_incr
    with _redis(fake_redis):
        task = asyncio.ensure_future(_call(ctx, DATABASE_URL, result=_NOT_FOUND))
        await asyncio.wait_for(entered.wait(), timeout=2)
        release.set()
        await asyncio.wait_for(task, timeout=2)
    assert slot_free_during_bump == [True]


# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------


def test_the_error_is_a_structural_configuration_cause():
    result = classify_run_failure(_error())
    assert result.cause is FailureCause.STALE_INTEGRATION_REFERENCE
    assert result.determination is FailureDetermination.STRUCTURAL
    assert result.detail == "notion:database"
    assert FailureCause.STALE_INTEGRATION_REFERENCE in BILLABLE_CANDIDATE_CAUSES


def test_a_provider_sounding_integration_name_changes_nothing():
    """The cause is read off the exception class, never off its text."""
    error = StaleIntegrationReferenceError(
        [_reference()], integration_names={INTEGRATION_ID: "Connection error - 503 overloaded"}
    )
    assert classify_run_failure(error).cause is FailureCause.STALE_INTEGRATION_REFERENCE


# ---------------------------------------------------------------------------
# The engine: the run outcome
# ---------------------------------------------------------------------------

SUMMARY = "Synced 42 rows from the Excel sheet; the prod tracker could not be updated."


class _EngineResult:
    def __init__(self, scalar=None, rows=(), one=None):
        self._scalar = scalar
        self._rows = list(rows)
        self._one = one

    def scalar_one_or_none(self):
        return self._scalar

    def one_or_none(self):
        return self._one

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))

    def all(self):
        return list(self._rows)


class _EngineSession:
    """Serves the run, its assignment and its agent, then the integration names.

    The run's charge record is read back under its row lock: answered
    with the run as this session holds it, the only attempt there is.
    """

    def __init__(self, run, assignment, agent):
        self._run = run
        self._queue = [run, assignment, agent]
        self.added: list = []
        self.commits = 0
        self.statements: list = []

    async def execute(self, _stmt, *_args, **_kwargs):
        self.statements.append(_stmt)
        if str(_stmt).startswith("SELECT assignment_runs.trigger_context"):
            return _EngineResult(rows=[self._run.trigger_context])
        if str(_stmt).startswith("SELECT users.role, users.is_active"):
            # The run's user, as a retry asks for it: it exists and is active.
            return _EngineResult(one=SimpleNamespace(role="user", is_active=True))
        if str(_stmt).startswith("SELECT tool_approval_preferences.tool_key"):
            # No standing approvals. Left to the catch-all below it read the
            # integration name as a tool's fingerprint.
            return _EngineResult()
        if self._queue:
            return _EngineResult(scalar=self._queue.pop(0))
        return _EngineResult(rows=[(uuid.UUID(INTEGRATION_ID), "Filigran Copilot - Prod")])

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None

    async def flush(self):
        return None

    def add(self, obj):
        self.added.append(obj)

    def in_transaction(self):
        return False

    def begin_nested(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _engine_objects():
    from app.models.agent import Agent
    from app.models.assignment import Assignment, AssignmentRun

    owner = uuid.uuid4()
    agent = Agent(
        id=uuid.uuid4(),
        name="OCTI SaaS Alerts -> Notion Sync",
        slug="saas-alerts-notion-sync",
        enabled=True,
        user_id=owner,
        is_builtin=False,
        is_company_managed=False,
        integration_bindings={"notion": [INTEGRATION_ID]},
        builtin_tool_ids=[],
        handover_agent_ids=[],
        output_format="markdown",
    )
    assignment = Assignment(
        id=uuid.uuid4(),
        agent_id=agent.id,
        user_id=owner,
        is_company_managed=False,
        name="Sync Excel -> Notion prod-tracker (hourly)",
        trigger_type="schedule",
        trigger_config={"cron": "0 * * * *"},
        prompt="Sync the Excel export into the Notion prod tracker.",
        enabled=True,
        dry_run=False,
        timeout_seconds=300,
        retry_on_failure=True,
        max_retries=3,
        notify_on_complete=False,
        execution_count=0,
    )
    run = AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=assignment.id,
        agent_id=agent.id,
        user_id=owner,
        is_company_managed=False,
        status="pending",
        trigger_payload={},
        trigger_context={"type": "schedule"},
        retry_count=0,
    )
    return run, assignment, agent


async def _drive_run(
    monkeypatch,
    *,
    report_integration_id: str | None,
    tracking_session=None,
    produced_file_id: str | None = None,
    engine_objects: tuple | None = None,
    on_loop=None,
    attempt: int | None = None,
    deadline_at=None,
    loop_stop_reason: str = "end_turn",
    patches: dict | None = None,
):
    """Run ``execute_assignment_run`` end to end with a fake agent loop.

    The loop reports a sustained stale reference through the provider channel
    (as the Notion detector does mid-run) when *report_integration_id* is set,
    and produces a file when *produced_file_id* is set. Every session opened
    after the run's own is *tracking_session* when one is given.
    *engine_objects* replaces the ``(run, assignment, agent)`` rows and
    *on_loop* is called inside the loop, where the run's tools execute.
    *attempt* is the orchestration attempt running it, *deadline_at* its
    execution's deadline. *loop_stop_reason* is the ``stop_reason`` the loop
    returns its answer with. *patches* replaces some of the default stubs, by
    target.
    """
    from app.core.config import settings
    from app.llm.base import ChatMessage, FileRef
    from app.services import assignment_engine
    from app.tools.wait_policy import WaitPolicy

    run, assignment, agent = engine_objects or _engine_objects()
    session = _EngineSession(run, assignment, agent)
    opened: list = []

    def _open_session():
        opened.append(session if not opened or tracking_session is None else tracking_session)
        return opened[-1]

    async def _fake_loop(**kwargs):
        if on_loop is not None:
            on_loop()
        if report_integration_id is not None:
            _report(integration_id=report_integration_id)
        if produced_file_id is not None:
            kwargs["file_context"].add(
                FileRef(
                    file_id=produced_file_id,
                    filename="prod-tracker.csv",
                    content_type="text/csv",
                    size=42,
                    s3_object_name="files/prod-tracker.csv",
                )
            )
        kwargs["messages"].append(ChatMessage(role="assistant", content=SUMMARY))
        return SimpleNamespace(
            content=SUMMARY,
            usage={"input_tokens": 1200, "output_tokens": 340},
            model="model-x",
            stop_reason=loop_stop_reason,
        )

    mocks = {
        "refund_pool": AsyncMock(return_value=True),
        "refund_free": AsyncMock(return_value=True),
        "event_bus": SimpleNamespace(emit=AsyncMock()),
        "track": AsyncMock(),
        "create_execution": AsyncMock(),
    }
    llm = SimpleNamespace(provider_name="anthropic", default_model="model-x", last_usage=None)
    for target, value in ({
        "app.services.assignment_engine.async_session": _open_session,
        "app.services.quota.check_agentic_quota": AsyncMock(),
        "app.services.quota.increment_agentic_usage": AsyncMock(),
        "app.services.quota.refund_agentic_usage_for_failure": mocks["refund_pool"],
        "app.services.quota.refund_agentic_usage_for_provider_outage": mocks["refund_free"],
        "app.services.assignment_engine.record_agentic_execution": AsyncMock(),
        "app.services.assignment_engine._publish_run_status": AsyncMock(),
        "app.services.assignment_engine.event_bus": mocks["event_bus"],
        "app.api.v1.helpers.get_user_group_ids": AsyncMock(return_value=[]),
        "app.services.variable_resolver.load_variables": AsyncMock(return_value={}),
        "app.llm.router.get_llm_for_user": AsyncMock(return_value=(llm, "model-x")),
        "app.services.langfuse_tracing.trace_llm_call": AsyncMock(return_value=None),
        "app.services.assignment_engine._build_trigger_information": lambda _run: "",
        "app.services.assignment_engine._load_memory_context": AsyncMock(return_value=("", "")),
        "app.services.builtin_tools.load_kb_names": AsyncMock(return_value={}),
        "app.services.builtin_tools.load_write_locked_kb_ids": AsyncMock(return_value=set()),
        "app.services.assignment_engine._build_system_message": AsyncMock(return_value="You are an agent."),
        "app.services.assignment_engine.load_authorized_trigger_files": AsyncMock(return_value=[]),
        "app.services.builtin_tools.load_mcp_tools": AsyncMock(return_value=None),
        "app.services.builtin_tools.load_custom_tools": AsyncMock(return_value=None),
        "app.services.integration_tools.build_integration_tools": AsyncMock(return_value=([], None)),
        # The owner's access is judged by tests/test_orphaned_integration_bindings.py.
        "app.services.integration_credentials.drop_orphaned_bindings": AsyncMock(return_value=[]),
        # Who may use them is judged by tests/test_assignment_run_bindings_postgres.py.
        "app.services.integration_tools.shadow_invisible_bindings": AsyncMock(return_value=[]),
        "app.services.assignment_engine.resolve_run_max_iterations": AsyncMock(return_value=10),
        "app.services.assignment_engine.resolve_run_wait_policy": AsyncMock(return_value=WaitPolicy()),
        "app.tools.run_budget.build_run_budget_for_run": AsyncMock(return_value=None),
        "app.tools.agent_loop.run_agent_loop": _fake_loop,
        "app.services.tool_tracking.track_tool_usage": AsyncMock(),
        "app.services.assignment_engine._track_assignment_auth_failures": AsyncMock(),
        "app.services.assignment_engine._track_stale_reference_failure": mocks["track"],
        "app.services.integration_tools.cancel_run_partner_tasks": AsyncMock(),
        "app.services.builtin_tools.cleanup_mcp_manager": AsyncMock(),
        "app.services.assignment_engine.create_execution": mocks["create_execution"],
    } | (patches or {})).items():
        monkeypatch.setattr(target, value)
    monkeypatch.setattr(settings, "agent_self_fanout_enabled", False)

    await assignment_engine.execute_assignment_run(
        str(run.id), attempt=attempt, deadline_at=deadline_at
    )
    return run, session, mocks


@pytest.mark.asyncio
async def test_a_run_that_hit_a_sustained_stale_reference_fails_with_its_summary_kept(monkeypatch):
    from app.services.event_bus import ASSIGNMENT_RUN_COMPLETED, ASSIGNMENT_RUN_FAILED

    run, session, mocks = await _drive_run(monkeypatch, report_integration_id=INTEGRATION_ID)

    assert run.status == "failed", run.error
    assert run.failure_cause == FailureCause.STALE_INTEGRATION_REFERENCE.value
    assert run.failure_determination == FailureDetermination.STRUCTURAL.value
    assert DATABASE_ID in run.error
    assert '"Filigran Copilot - Prod"' in run.error
    assert "Share it with that integration in Notion" in run.error
    # What the agent did is not hidden behind the failure.
    assert run.result_summary == SUMMARY
    assert run.total_input_tokens == 1200 and run.total_output_tokens == 340
    assert run.messages

    # A broken configuration spends a slot of the bounded pool, never the free credit.
    mocks["refund_pool"].assert_awaited_once()
    assert mocks["refund_pool"].await_args.kwargs["cause"] == "stale_integration_reference"
    mocks["refund_free"].assert_not_awaited()

    events = [call.args[0] for call in mocks["event_bus"].emit.await_args_list]
    assert ASSIGNMENT_RUN_FAILED in events and ASSIGNMENT_RUN_COMPLETED not in events

    # The streak is read once the failure is committed, with the references.
    mocks["track"].assert_awaited_once()
    error = mocks["track"].await_args.kwargs["error"]
    assert isinstance(error, StaleIntegrationReferenceError)
    assert [ref.resource_id for ref in error.references] == [DATABASE_ID]

    # retry_on_failure is on, but a retry would hit the same missing resource.
    mocks["create_execution"].assert_not_awaited()
    assert session.added == []


@pytest.mark.asyncio
async def test_a_run_without_the_signal_completes_as_before(monkeypatch):
    from app.services.event_bus import ASSIGNMENT_RUN_COMPLETED

    run, _session, mocks = await _drive_run(monkeypatch, report_integration_id=None)

    assert run.status == "completed", run.error
    assert run.failure_cause is None
    assert run.error is None
    assert run.result_summary == SUMMARY
    mocks["track"].assert_not_awaited()
    mocks["refund_pool"].assert_not_awaited()
    events = [call.args[0] for call in mocks["event_bus"].emit.await_args_list]
    assert ASSIGNMENT_RUN_COMPLETED in events


@pytest.mark.asyncio
async def test_a_reference_through_an_integration_the_run_is_not_bound_to_is_ignored(monkeypatch):
    run, _session, mocks = await _drive_run(monkeypatch, report_integration_id=str(uuid.uuid4()))

    assert run.status == "completed", run.error
    mocks["track"].assert_not_awaited()


@pytest.mark.asyncio
async def test_the_streak_is_tracked_in_a_session_of_its_own(monkeypatch):
    """The tracker's failure path rolls back its session, and a rollback expires
    EVERY instance of that session: on the run's own session the engine's next
    read of the run or the assignment would be a lazy load, which an
    AsyncSession refuses (MissingGreenlet) - after the run was committed."""
    tracking_session = _EngineSession(None, None, None)

    run, session, mocks = await _drive_run(
        monkeypatch, report_integration_id=INTEGRATION_ID, tracking_session=tracking_session
    )

    assert run.status == "failed", run.error
    mocks["track"].assert_awaited_once()
    assert mocks["track"].await_args.args[0] is tracking_session
    assert mocks["track"].await_args.args[0] is not session


@pytest.mark.asyncio
async def test_a_stale_failed_run_keeps_the_files_it_produced(monkeypatch):
    """What the agent produced stays with the run, files included - the kept
    summary points at them, and run-scoped access reads the link."""
    produced = str(uuid.uuid4())

    run, session, _mocks = await _drive_run(
        monkeypatch, report_integration_id=INTEGRATION_ID, produced_file_id=produced
    )

    assert run.status == "failed", run.error
    links = [
        stmt
        for stmt in session.statements
        if getattr(getattr(stmt, "table", None), "name", None) == "file_attachments"
    ]
    assert len(links) == 1
    params = links[0].compile().params
    assert run.id in params.values()
    assert uuid.UUID(produced) in params.values()


# ---------------------------------------------------------------------------
# The streak
# ---------------------------------------------------------------------------


def _row(status="failed", cause="stale_integration_reference", trigger="schedule", *, gate=False, created=None, run_id=None):
    return (
        run_id or uuid.uuid4(),
        status,
        cause if status == "failed" else None,
        trigger,
        gate,
        created,
    )


def _streak_runs(rows):
    from app.services.assignment_engine import _StreakRun

    return [
        _StreakRun(id=r[0], status=r[1], failure_cause=r[2], trigger_type=r[3], gate_skipped=r[4], created_at=r[5])
        for r in rows
    ]


def test_the_streak_counts_consecutive_scheduled_stale_failures():
    from app.services.assignment_engine import _stale_reference_streak

    runs = _streak_runs([_row(), _row(trigger="schedule_fanout"), _row(), _row(status="completed"), _row()])
    assert _stale_reference_streak(runs, since=None) == 3


def test_a_success_or_another_cause_ends_the_streak():
    from app.services.assignment_engine import _stale_reference_streak

    assert _stale_reference_streak(_streak_runs([_row(), _row(status="completed"), _row()]), since=None) == 1
    assert _stale_reference_streak(_streak_runs([_row(), _row(cause="provider_timeout"), _row()]), since=None) == 1


def test_manual_runs_and_gate_skips_neither_extend_nor_end_the_streak():
    from app.services.assignment_engine import _stale_reference_streak

    runs = _streak_runs(
        [
            _row(),
            _row(trigger="manual"),
            _row(status="completed", trigger="schedule", gate=True),
            _row(trigger="webhook"),
            _row(),
        ]
    )
    assert _stale_reference_streak(runs, since=None) == 2


def test_a_non_scheduled_run_neither_extends_nor_ends_the_streak_whatever_its_outcome():
    """Scheduled stale -> manual context overflow -> scheduled stale is two
    scheduled stale failures in a row: a manual, webhook or event run is
    neutral whether it failed another way or succeeded."""
    from app.services.assignment_engine import _stale_reference_streak

    runs = _streak_runs([_row(), _row(cause="context_overflow", trigger="manual"), _row()])
    assert _stale_reference_streak(runs, since=None) == 2
    runs = _streak_runs(
        [
            _row(),
            _row(status="completed", trigger="webhook"),
            _row(cause="provider_timeout", trigger="integration_event"),
            _row(),
        ]
    )
    assert _stale_reference_streak(runs, since=None) == 2


def test_runs_before_the_last_pause_never_count():
    from app.services.assignment_engine import _stale_reference_streak

    paused_at = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
    runs = _streak_runs(
        [
            _row(created=paused_at + timedelta(hours=2)),
            _row(created=paused_at - timedelta(hours=1)),
            _row(created=paused_at - timedelta(hours=2)),
        ]
    )
    assert _stale_reference_streak(runs, since=paused_at) == 1


def test_only_the_first_failure_of_an_episode_starts_it():
    from app.services.assignment_engine import _starts_stale_reference_episode

    first, second = uuid.uuid4(), uuid.uuid4()
    runs = _streak_runs([_row(run_id=second), _row(run_id=first), _row(status="completed")])
    assert _starts_stale_reference_episode(runs, first, since=None) is True
    assert _starts_stale_reference_episode(runs, second, since=None) is False
    only = uuid.uuid4()
    assert _starts_stale_reference_episode(_streak_runs([_row(run_id=only)]), only, since=None) is True


def test_a_non_scheduled_run_does_not_restart_an_episode():
    """A manual run that failed another way, or succeeded, is neutral: the
    scheduled failure after it is the same episode, told once."""
    from app.services.assignment_engine import _starts_stale_reference_episode

    newest = uuid.uuid4()
    runs = _streak_runs(
        [_row(run_id=newest), _row(cause="context_overflow", trigger="manual"), _row(), _row(status="completed")]
    )
    assert _starts_stale_reference_episode(runs, newest, since=None) is False
    runs = _streak_runs([_row(run_id=newest), _row(status="completed", trigger="webhook"), _row()])
    assert _starts_stale_reference_episode(runs, newest, since=None) is False
    # A scheduled run that did not fail this way did end the previous episode.
    runs = _streak_runs([_row(run_id=newest), _row(status="completed"), _row()])
    assert _starts_stale_reference_episode(runs, newest, since=None) is True


def test_a_stale_failure_of_any_trigger_belongs_to_the_episode():
    """Its owner was told when that run failed; the scheduled runs after it are the same episode."""
    from app.services.assignment_engine import _starts_stale_reference_episode

    newest = uuid.uuid4()
    runs = _streak_runs([_row(run_id=newest), _row(trigger="manual"), _row(status="completed")])
    assert _starts_stale_reference_episode(runs, newest, since=None) is False


# ---------------------------------------------------------------------------
# Tracking: notices, pause, audit
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, scalar=None, rows=()):
        self._scalar = scalar
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._scalar

    def all(self):
        return self._rows


class _TrackingSession:
    """The two reads ``_track_stale_reference_failure`` makes: the locked row, then the runs."""

    def __init__(self, assignment, rows, *, fail=False):
        self._assignment = assignment
        self._rows = rows
        self._fail = fail
        self.executed = 0
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, _stmt):
        self.executed += 1
        if self._fail:
            raise RuntimeError("database is gone")
        if self.executed == 1:
            return _Result(scalar=self._assignment)
        return _Result(rows=self._rows)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _tracked_assignment(**overrides):
    values = {
        "id": uuid.uuid4(),
        "agent_id": uuid.uuid4(),
        "name": "Sync Excel -> Notion prod-tracker (hourly)",
        "trigger_type": "schedule",
        "enabled": True,
        "user_id": uuid.uuid4(),
        "is_company_managed": False,
        "run_as_all_users": False,
        "paused_at": None,
        "paused_reason": None,
        "paused_by_user_id": None,
        "paused_by_label": None,
        "paused_details": None,
        "last_paused_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _track(
    monkeypatch, *, assignment, rows, run_id, threshold=3, agent=None, fail=False, trigger="schedule"
):
    from app.core.config import settings
    from app.services import assignment_engine

    monkeypatch.setattr(settings, "assignment_stale_reference_pause_after", threshold)
    notice = AsyncMock(return_value=True)
    audit = AsyncMock()
    monkeypatch.setattr(assignment_engine, "_emit_assignment_notice", notice)
    monkeypatch.setattr("app.services.assignment_pause.record_system_pause_audit", audit)
    session = _TrackingSession(assignment, rows, fail=fail)
    run = SimpleNamespace(id=run_id, user_id=uuid.uuid4(), trigger_context={"type": trigger})
    await assignment_engine._track_stale_reference_failure(
        session,
        run=run,
        assignment=assignment,
        agent=agent or SimpleNamespace(is_builtin=False),
        error=_error(),
    )
    return session, notice, audit, run


@pytest.mark.asyncio
async def test_n_minus_one_failures_notify_once_and_do_not_pause(monkeypatch):
    assignment = _tracked_assignment()
    first, second = uuid.uuid4(), uuid.uuid4()

    _, notice, audit, _ = await _track(
        monkeypatch, assignment=assignment, rows=[_row(run_id=first), _row(status="completed")], run_id=first
    )
    assert assignment.enabled is True
    assert notice.await_count == 1
    kwargs = notice.await_args.kwargs
    assert kwargs["activity_type"] == "assignment_stale_reference"
    assert DATABASE_ID in kwargs["summary"]
    assert "paused automatically after 3 consecutive scheduled runs" in kwargs["summary"]
    assert kwargs["extra_data"]["paused"] is False
    audit.assert_not_awaited()

    session, notice, audit, _ = await _track(
        monkeypatch,
        assignment=assignment,
        rows=[_row(run_id=second), _row(run_id=first), _row(status="completed")],
        run_id=second,
    )
    assert assignment.enabled is True
    notice.assert_not_awaited()
    audit.assert_not_awaited()
    assert session.commits == 1


@pytest.mark.asyncio
async def test_the_nth_failure_pauses_audits_and_notifies_once(monkeypatch):
    assignment = _tracked_assignment()
    newest = uuid.uuid4()
    session, notice, audit, run = await _track(
        monkeypatch, assignment=assignment, rows=[_row(run_id=newest), _row(), _row()], run_id=newest
    )

    assert assignment.enabled is False
    assert assignment.paused_reason == "stale_reference"
    assert assignment.paused_by_user_id is None
    assert "engine" in assignment.paused_by_label
    assert "3 consecutive scheduled runs" in assignment.paused_details
    assert DATABASE_ID in assignment.paused_details
    assert "re-enable the assignment" in assignment.paused_details
    assert assignment.last_paused_at == assignment.paused_at

    notice.assert_awaited_once()
    assert notice.await_args.kwargs["activity_type"] == "assignment_stale_reference_pause"
    assert notice.await_args.kwargs["extra_data"]["paused"] is True

    audit.assert_awaited_once()
    audit_kwargs = audit.await_args.kwargs
    assert audit_kwargs["actor"] == "assignment-engine"
    assert audit_kwargs["anchor_user_id"] == assignment.user_id
    assert audit_kwargs["reason"] == "stale_reference"
    assert audit_kwargs["extra"]["run_id"] == str(run.id)
    assert session.commits == 1


@pytest.mark.asyncio
async def test_a_success_in_between_resets_the_streak(monkeypatch):
    assignment = _tracked_assignment()
    newest = uuid.uuid4()
    _, notice, audit, _ = await _track(
        monkeypatch,
        assignment=assignment,
        rows=[_row(run_id=newest), _row(status="completed"), _row(), _row()],
        run_id=newest,
    )
    assert assignment.enabled is True
    assert notice.await_args.kwargs["activity_type"] == "assignment_stale_reference"
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_another_failure_cause_in_between_resets_the_streak(monkeypatch):
    assignment = _tracked_assignment()
    newest = uuid.uuid4()
    await _track(
        monkeypatch,
        assignment=assignment,
        rows=[_row(run_id=newest), _row(cause="context_overflow"), _row(), _row()],
        run_id=newest,
    )
    assert assignment.enabled is True


@pytest.mark.asyncio
async def test_zero_disables_the_pause_but_still_reports(monkeypatch):
    assignment = _tracked_assignment()
    newest = uuid.uuid4()
    _, notice, audit, _ = await _track(
        monkeypatch,
        assignment=assignment,
        rows=[_row(run_id=newest), *[_row() for _ in range(10)], _row(status="completed")],
        run_id=newest,
        threshold=0,
    )
    assert assignment.enabled is True
    audit.assert_not_awaited()
    notice.assert_not_awaited()

    first = uuid.uuid4()
    _, notice, _, _ = await _track(
        monkeypatch, assignment=assignment, rows=[_row(run_id=first), _row(status="completed")], run_id=first, threshold=0
    )
    assert notice.await_args.kwargs["activity_type"] == "assignment_stale_reference"
    assert "paused automatically" not in notice.await_args.kwargs["summary"]


@pytest.mark.asyncio
async def test_overlapping_runs_pause_and_notify_once(monkeypatch):
    """Both runs committed before either evaluated: the second finds it paused."""
    assignment = _tracked_assignment()
    older, newer = uuid.uuid4(), uuid.uuid4()
    rows = [_row(run_id=newer), _row(run_id=older), _row(), _row(status="completed")]

    _, first_notice, first_audit, _ = await _track(monkeypatch, assignment=assignment, rows=rows, run_id=older)
    _, second_notice, second_audit, _ = await _track(monkeypatch, assignment=assignment, rows=rows, run_id=newer)

    assert assignment.enabled is False
    assert first_audit.await_count + second_audit.await_count == 1
    kinds = [
        call.kwargs["activity_type"]
        for notice in (first_notice, second_notice)
        for call in notice.await_args_list
    ]
    assert kinds == ["assignment_stale_reference_pause"]


@pytest.mark.asyncio
async def test_resuming_starts_a_fresh_streak(monkeypatch):
    paused_at = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)
    assignment = _tracked_assignment(last_paused_at=paused_at)
    newest = uuid.uuid4()
    _, notice, audit, _ = await _track(
        monkeypatch,
        assignment=assignment,
        rows=[
            _row(run_id=newest, created=paused_at + timedelta(hours=3)),
            _row(created=paused_at - timedelta(hours=1)),
            _row(created=paused_at - timedelta(hours=2)),
            _row(created=paused_at - timedelta(hours=3)),
        ],
        run_id=newest,
    )
    assert assignment.enabled is True
    audit.assert_not_awaited()
    assert notice.await_args.kwargs["activity_type"] == "assignment_stale_reference"


@pytest.mark.asyncio
async def test_a_manual_run_above_a_full_scheduled_streak_does_not_pause(monkeypatch):
    """The scheduled streak is already at the threshold (the tracking of the
    run that reached it failed, or the threshold was lowered): the manual
    re-run a person makes to look at it is still not the one that pauses."""
    assignment = _tracked_assignment()
    newest = uuid.uuid4()
    _, _, audit, _ = await _track(
        monkeypatch,
        assignment=assignment,
        rows=[_row(run_id=newest, trigger="manual"), _row(), _row(), _row()],
        run_id=newest,
        trigger="manual",
    )
    assert assignment.enabled is True
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_reruns_never_pause(monkeypatch):
    """A person re-running a broken assignment to look at it must not pause it."""
    assignment = _tracked_assignment()
    newest = uuid.uuid4()
    await _track(
        monkeypatch,
        assignment=assignment,
        rows=[_row(run_id=newest, trigger="manual"), _row(trigger="manual"), _row(trigger="manual"), _row()],
        run_id=newest,
    )
    assert assignment.enabled is True


@pytest.mark.asyncio
async def test_a_built_in_assignment_is_reported_never_paused(monkeypatch):
    assignment = _tracked_assignment()
    first = uuid.uuid4()
    _, notice, audit, _ = await _track(
        monkeypatch,
        assignment=assignment,
        rows=[_row(run_id=first), _row(), _row(), _row(status="completed")],
        run_id=first,
        agent=SimpleNamespace(is_builtin=True),
        threshold=1,
    )
    assert assignment.enabled is True
    audit.assert_not_awaited()
    # Not the first failure of the episode: nothing more to say.
    notice.assert_not_awaited()

    only = uuid.uuid4()
    _, notice, _, _ = await _track(
        monkeypatch,
        assignment=assignment,
        rows=[_row(run_id=only), _row(status="completed")],
        run_id=only,
        agent=SimpleNamespace(is_builtin=True),
        threshold=1,
    )
    assert assignment.enabled is True
    assert "will not be paused automatically" in notice.await_args.kwargs["summary"]


@pytest.mark.asyncio
async def test_a_company_managed_pause_is_anchored_on_and_told_to_its_run_user(monkeypatch):
    assignment = _tracked_assignment(user_id=None, is_company_managed=True)
    newest = uuid.uuid4()
    _, notice, audit, run = await _track(
        monkeypatch, assignment=assignment, rows=[_row(run_id=newest), _row(), _row()], run_id=newest
    )
    assert assignment.enabled is False
    assert audit.await_args.kwargs["anchor_user_id"] == run.user_id
    assert notice.await_args.kwargs["company_recipient_id"] == run.user_id


@pytest.mark.asyncio
async def test_a_per_user_fanout_reads_only_the_run_users_history(monkeypatch):
    """One user's broken share must not restart the episode on every run just
    because another user's run succeeded in between - a notice per run."""
    from app.services import assignment_engine

    seen: list = []
    real_load = assignment_engine._load_streak_runs

    async def _spy(db, assignment_id, *, limit, user_id=None):
        seen.append(user_id)
        return await real_load(db, assignment_id, limit=limit, user_id=user_id)

    monkeypatch.setattr(assignment_engine, "_load_streak_runs", _spy)
    fanout = _tracked_assignment(user_id=None, is_company_managed=True, run_as_all_users=True)
    newest = uuid.uuid4()
    _, _, _, run = await _track(
        monkeypatch, assignment=fanout, rows=[_row(run_id=newest), _row(status="completed")], run_id=newest
    )
    assert seen == [run.user_id]

    personal = _tracked_assignment()
    await _track(monkeypatch, assignment=personal, rows=[_row(run_id=newest)], run_id=newest)
    assert seen[-1] is None


@pytest.mark.asyncio
async def test_the_streak_query_filters_on_the_run_user_only_when_asked():
    from sqlalchemy.dialects import postgresql

    from app.services.assignment_engine import _load_streak_runs

    captured: list = []

    class _Db:
        async def execute(self, stmt):
            captured.append(stmt)
            return _Result(rows=[])

    await _load_streak_runs(_Db(), uuid.uuid4(), limit=5, user_id=uuid.uuid4())
    await _load_streak_runs(_Db(), uuid.uuid4(), limit=5)
    with_user, without = (str(stmt.compile(dialect=postgresql.dialect())) for stmt in captured)
    assert "assignment_runs.user_id =" in with_user
    assert "assignment_runs.user_id =" not in without


@pytest.mark.asyncio
async def test_neutral_runs_are_filtered_before_the_scan_is_bounded():
    """Fifty webhook runs between two scheduled ones must not crowd the
    scheduled history out of the bounded scan: the streak would never reach
    the threshold, and every scheduled failure would look like the start of
    an episode (a notice each). Only stale failures of any trigger and the
    scheduled runs that ran the agent are read."""
    from sqlalchemy.dialects import postgresql

    from app.services.assignment_engine import _load_streak_runs

    captured: list = []

    class _Db:
        async def execute(self, stmt):
            captured.append(stmt)
            return _Result(rows=[])

    await _load_streak_runs(_Db(), uuid.uuid4(), limit=5)
    (stmt,) = captured
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    where = sql.split("WHERE", 1)[1]
    assert "'stale_integration_reference'" in where
    assert "IN ('schedule', 'schedule_fanout')" in where
    assert "result_summary NOT LIKE 'Skipped: activation check returned'" in where


class _NoticeSavepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["personal", "company", "fanout"])
async def test_a_recipient_scoped_notice_is_published_to_that_recipient_only(monkeypatch, shape):
    """A company-managed run can resolve its run user's PRIVATE integration,
    and the stale-reference notice names it and the resource: the company
    channel, which every connected user listens to, must not carry it. The
    durable row goes to that user alone as well."""
    from app.models.activity import Activity
    from app.services import assignment_engine

    published: list[str] = []

    class _Redis:
        async def publish(self, channel, _payload):
            published.append(channel)

    monkeypatch.setattr("app.core.redis.redis_client", AsyncMock(return_value=_Redis()))
    added: list = []
    db = MagicMock()
    db.add = added.append
    db.flush = AsyncMock()
    db.begin_nested = MagicMock(return_value=_NoticeSavepoint())
    owner, recipient = uuid.uuid4(), uuid.uuid4()
    assignment = _tracked_assignment(
        user_id=owner if shape == "personal" else None,
        is_company_managed=shape != "personal",
        run_as_all_users=shape == "fanout",
    )

    delivered = await assignment_engine._emit_assignment_notice(
        db,
        assignment=assignment,
        agent=SimpleNamespace(id=uuid.uuid4(), name="Notion Sync"),
        activity_type="assignment_stale_reference",
        title="failing",
        summary='the Notion database x through the integration "Private"',
        extra_data={},
        company_recipient_id=recipient,
    )

    assert delivered is True
    expected_user = owner if shape == "personal" else recipient
    assert published == [f"activity:{expected_user}"]
    (activity,) = added
    assert isinstance(activity, Activity)
    assert activity.user_id == expected_user


@pytest.mark.asyncio
async def test_a_tracking_failure_is_swallowed_and_rolled_back(monkeypatch):
    assignment = _tracked_assignment()
    session, notice, audit, _ = await _track(
        monkeypatch, assignment=assignment, rows=[], run_id=uuid.uuid4(), fail=True
    )
    assert session.rollbacks == 1
    assert assignment.enabled is True
    notice.assert_not_awaited()
    audit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Pause state, audit, setting
# ---------------------------------------------------------------------------


def test_the_last_pause_survives_the_resume():
    from app.services.assignment_pause import clear_pause_provenance, pause_assignment

    assignment = _tracked_assignment()
    when = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    pause_assignment(
        assignment, reason="stale_reference", details="Auto-paused.", actor_label="engine", now=when
    )
    assert assignment.paused_reason == "stale_reference"
    clear_pause_provenance(assignment)
    assert assignment.paused_at is None
    assert assignment.last_paused_at == when


def test_the_stale_reference_pause_is_a_system_reason():
    from app.services.assignment_pause import PAUSE_REASONS, SYSTEM_REASONS

    assert "stale_reference" in PAUSE_REASONS
    assert "stale_reference" in SYSTEM_REASONS


@pytest.mark.asyncio
async def test_the_system_pause_audit_row_names_the_component_not_a_person():
    from app.models.audit_log import AuditLog
    from app.services.assignment_pause import record_system_pause_audit

    added: list = []

    class _Savepoint:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    db = MagicMock()
    db.add = added.append
    db.flush = AsyncMock()
    db.begin_nested = MagicMock(return_value=_Savepoint())

    assignment = _tracked_assignment()
    await record_system_pause_audit(
        db,
        assignment=assignment,
        actor="assignment-engine",
        anchor_user_id=assignment.user_id,
        reason="stale_reference",
        details="Auto-paused after 3 consecutive scheduled runs.",
        extra={"run_id": "run-1"},
    )

    (row,) = added
    assert isinstance(row, AuditLog)
    assert row.user_email == "system:assignment-engine"
    assert row.user_id == assignment.user_id
    assert row.action == "pause"
    assert row.entity_type == "assignment"
    assert row.details["actor"] == "system:assignment-engine"
    assert row.details["reason"] == "stale_reference"
    assert row.details["run_id"] == "run-1"


def test_the_pause_after_setting_rejects_a_negative_value():
    from app.core.config import Settings

    with pytest.raises(ValueError, match="ASSIGNMENT_STALE_REFERENCE_PAUSE_AFTER"):
        Settings(assignment_stale_reference_pause_after=-1)
    assert Settings(assignment_stale_reference_pause_after=0).assignment_stale_reference_pause_after == 0
    assert Settings().assignment_stale_reference_pause_after == 3
