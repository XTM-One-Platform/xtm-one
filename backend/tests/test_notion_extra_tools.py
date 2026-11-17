"""Tests for the Notion gap-closing tools added alongside the package split:

- ``get_notion_me`` (GET /v1/users/me - the connected bot/token owner),
- ``restore_notion_page`` (un-trash, the reverse of trash_notion_page),
- ``add_notion_comment`` replying into an existing discussion thread via
  ``discussion_id`` (+ ``list_notion_comments`` surfacing the discussion_id).

``get_notion_me`` / ``restore_notion_page`` go through ``NotionContext.request``
(-> ``_common.safe_request``); the comment tools use the error-metadata path
(-> ``_common.AutoRefreshClient`` verbs), matching ``test_notion_comments.py``.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.integrations.providers.notion import build_tools
from app.integrations.providers.notion.search import _MAX_CONTAINER_NESTING
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall


def _ok(payload: dict) -> tuple[httpx.Response, None]:
    return (httpx.Response(200, json=payload), None)


def _err(message: str) -> tuple[None, str]:
    """Mirror ``safe_request``'s failure return: ``(None, error_string)``."""
    return (None, message)


# ── get_notion_me ────────────────────────────────────────────────────────


def test_get_notion_me_schema_takes_no_args():
    executor = _SafeIntegrationExecutor()
    defs = build_tools("dummy-token", executor)
    tool = next(t for t in defs if t.name == "get_notion_me")
    assert tool.input_schema["required"] == []
    assert tool.input_schema["properties"] == {}


@pytest.mark.asyncio
async def test_get_notion_me_reports_bot_identity():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = _ok(
            {
                "id": "bot-1",
                "name": "XTM One Bot",
                "type": "bot",
                "bot": {"workspace_name": "Filigran HQ", "owner": {"type": "workspace"}},
            }
        )
        result = await executor.execute_tool(
            ToolCall(id="m1", name="get_notion_me", arguments={})
        )
    assert not result.is_error, result.content
    assert "XTM One Bot" in result.content
    assert "Filigran HQ" in result.content
    assert mock_request.call_args.args[2].endswith("/users/me")


# ── restore_notion_page ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restore_notion_page_sets_in_trash_false():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = _ok({"id": "page-1", "in_trash": False})
        result = await executor.execute_tool(
            ToolCall(
                id="r1", name="restore_notion_page", arguments={"page_id": "page-1"}
            )
        )
    assert not result.is_error, result.content
    assert "restored from trash" in result.content
    call = mock_request.call_args
    assert call.args[1] == "patch"
    assert call.args[2].endswith("/pages/page-1")
    assert call.kwargs["json"] == {"in_trash": False}


# ── add_notion_comment: reply into a discussion thread ────────────────────


@pytest.mark.asyncio
async def test_add_notion_comment_replies_into_discussion():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    captured: dict = {}

    async def _mock_post(*args, **kwargs):
        captured.update(kwargs.get("json") or {})
        return httpx.Response(200, json={"id": "comment-9"})

    with patch(
        "app.integrations.providers.notion._common.AutoRefreshClient.post",
        new_callable=AsyncMock,
    ) as mock_post:
        mock_post.side_effect = _mock_post
        result = await executor.execute_tool(
            ToolCall(
                id="d1",
                name="add_notion_comment",
                arguments={"discussion_id": "disc-123", "text": "Replying in thread"},
            )
        )
    assert not result.is_error, result.content
    assert "discussion disc-123" in result.content
    assert captured["discussion_id"] == "disc-123"
    assert "parent" not in captured
    assert captured["rich_text"][0]["text"]["content"] == "Replying in thread"


@pytest.mark.asyncio
async def test_add_notion_comment_page_level_still_uses_parent():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    captured: dict = {}

    async def _mock_post(*args, **kwargs):
        captured.update(kwargs.get("json") or {})
        return httpx.Response(200, json={"id": "comment-1"})

    with patch(
        "app.integrations.providers.notion._common.AutoRefreshClient.post",
        new_callable=AsyncMock,
    ) as mock_post:
        mock_post.side_effect = _mock_post
        result = await executor.execute_tool(
            ToolCall(
                id="d2",
                name="add_notion_comment",
                arguments={"page_id": "page-7", "text": "New page comment"},
            )
        )
    assert not result.is_error, result.content
    assert "page page-7" in result.content
    assert captured["parent"] == {"page_id": "page-7"}
    assert "discussion_id" not in captured


@pytest.mark.asyncio
async def test_add_notion_comment_requires_a_target():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    result = await executor.execute_tool(
        ToolCall(id="d3", name="add_notion_comment", arguments={"text": "orphan"})
    )
    # comments.py returns plain "Error: ..." strings (same convention as its
    # scope errors), so check the message rather than the is_error flag.
    assert "page_id" in result.content and "discussion_id" in result.content
    assert result.content.startswith("Error:")


@pytest.mark.asyncio
async def test_add_notion_comment_rejects_both_targets():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    result = await executor.execute_tool(
        ToolCall(
            id="d4",
            name="add_notion_comment",
            arguments={"page_id": "p", "discussion_id": "d", "text": "x"},
        )
    )
    assert "not both" in result.content
    assert result.content.startswith("Error:")


@pytest.mark.asyncio
async def test_append_notion_blocks_after_block_id_inserts_after():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = _ok({"results": [{"id": "b1"}]})
        result = await executor.execute_tool(
            ToolCall(
                id="ab1",
                name="append_notion_blocks",
                arguments={
                    "block_id": "page-1",
                    "content": "Under the heading",
                    "after_block_id": "head-1",
                },
            )
        )
    assert not result.is_error, result.content
    assert "after head-1" in result.content
    call = mock_request.call_args_list[0]
    assert call.args[2].endswith("/blocks/page-1/children")
    assert call.kwargs["json"]["after"] == "head-1"


@pytest.mark.asyncio
async def test_append_notion_blocks_without_after_omits_position():
    """Backwards-compat: no after_block_id -> no `after` key in the body."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = _ok({"results": [{"id": "b1"}]})
        result = await executor.execute_tool(
            ToolCall(
                id="ab2",
                name="append_notion_blocks",
                arguments={"block_id": "page-1", "content": "Just text"},
            )
        )
    assert not result.is_error, result.content
    assert "after" not in mock_request.call_args_list[0].kwargs["json"]
    assert result.content == "Appended 1 block(s) to page-1."


@pytest.mark.asyncio
async def test_append_notion_blocks_json_after_block_id_inserts_after():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = _ok({"results": [{"id": "b1"}]})
        result = await executor.execute_tool(
            ToolCall(
                id="ab3",
                name="append_notion_blocks_json",
                arguments={
                    "block_id": "page-1",
                    "children_json": (
                        '[{"type": "paragraph", "paragraph": '
                        '{"rich_text": [{"type": "text", "text": {"content": "x"}}]}}]'
                    ),
                    "after_block_id": "head-9",
                },
            )
        )
    assert not result.is_error, result.content
    assert "after head-9" in result.content
    assert mock_request.call_args_list[0].kwargs["json"]["after"] == "head-9"


@pytest.mark.asyncio
async def test_list_notion_comments_surfaces_discussion_id():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    async def _mock_get(*args, **kwargs):
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "comment-1",
                        "discussion_id": "disc-abc",
                        "created_time": "2026-06-01T00:00:00.000Z",
                        "created_by": {"name": "Reviewer"},
                        "rich_text": [{"plain_text": "Thread root"}],
                    }
                ],
                "has_more": False,
            },
        )

    with patch(
        "app.integrations.providers.notion._common.AutoRefreshClient.get",
        new_callable=AsyncMock,
    ) as mock_get:
        mock_get.side_effect = _mock_get
        result = await executor.execute_tool(
            ToolCall(
                id="d5", name="list_notion_comments", arguments={"page_id": "page-1"}
            )
        )
    assert not result.is_error, result.content
    assert "Discussion: disc-abc" in result.content


# ── list_notion_children: surface API errors instead of "no children" ──────

_PAGE_META = {"properties": {"Name": {"type": "title", "title": [{"plain_text": "Roadmap"}]}}}


@pytest.mark.asyncio
async def test_list_notion_children_surfaces_api_error_instead_of_empty():
    """A failed children fetch must NOT be reported as "no child pages" — that
    would mislead the agent into treating an API error as an exhaustive empty
    result. The error is surfaced like every sibling ``list_*`` Notion tool."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _ok(_PAGE_META),  # GET /pages/{id} -> page title resolves
            _err("Notion API error (403 Forbidden): restricted_resource"),  # walk fails
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="c1", name="list_notion_children", arguments={"page_id": "page-1"}
            )
        )
    assert "Could not browse 'Roadmap'" in result.content
    assert "restricted_resource" in result.content
    assert "No child pages" not in result.content


@pytest.mark.asyncio
async def test_list_notion_children_flags_partial_results_on_branch_error():
    """When the first page of children is read but a follow-up page fails, the
    partial listing is returned WITH an explicit incompleteness note (so the
    caller doesn't treat it as exhaustive). Uses a child_database (which is not
    recursed into) + a paginated second fetch that fails, keeping the mock
    call sequence deterministic."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    root_page_1 = {
        "results": [
            {
                "id": "db-1",
                "type": "child_database",
                "child_database": {"title": "Reports"},
            }
        ],
        "has_more": True,
        "next_cursor": "cursor-1",
    }
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _ok(_PAGE_META),  # GET /pages/{id} -> page title
            _ok(root_page_1),  # GET children page 1 -> child_database found
            _err("Notion API error (429): rate_limited"),  # children page 2 fails
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="c2",
                name="list_notion_children",
                arguments={"page_id": "page-1", "max_depth": 3},
            )
        )
    assert "Reports" in result.content  # the part we did read is returned
    assert "may be incomplete" in result.content
    assert "rate_limited" in result.content


@pytest.mark.asyncio
async def test_list_notion_children_bounds_nested_container_recursion():
    """Container blocks (toggles/columns/callouts) are traversed transparently -
    they do NOT count toward ``max_depth`` so child pages wrapped in them are
    still found - but the container recursion itself is bounded by an absolute
    ceiling so a pathologically deep container chain can't drive unbounded
    recursion / API calls. The non-empty listing is flagged as incomplete when
    the ceiling is hit."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    calls = {"children": 0}

    def _side_effect(client, method, url, *args, **kwargs):
        if url.endswith("/pages/page-1"):
            return _ok(_PAGE_META)  # GET /pages/{id} -> page title
        # Every children fetch returns a fresh container that itself has more
        # children -> an effectively infinite chain absent a recursion ceiling.
        calls["children"] += 1
        n = calls["children"]
        results = [{"id": f"toggle-{n}", "type": "toggle", "has_children": True}]
        if n == 1:
            # A real child at the top level so the listing is non-empty and we
            # exercise the found-results-with-incompleteness-note path.
            results.append(
                {
                    "id": "db-1",
                    "type": "child_database",
                    "child_database": {"title": "Reports"},
                }
            )
        return _ok({"results": results, "has_more": False})

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = _side_effect
        result = await executor.execute_tool(
            ToolCall(
                id="c3",
                name="list_notion_children",
                arguments={"page_id": "page-1", "max_depth": 3},
            )
        )

    assert not result.is_error, result.content
    assert "Reports" in result.content  # the real child was still discovered
    assert "may be incomplete" in result.content  # ceiling surfaced, not silent
    # Recursion stopped exactly at the ceiling instead of looping forever: one
    # fetch per container level for depths 0.._MAX_CONTAINER_NESTING inclusive,
    # then the next level returns before issuing a request.
    assert calls["children"] == _MAX_CONTAINER_NESTING + 1


# ── update_notion_database: legacy fallback fires in the common case ───────


@pytest.mark.asyncio
async def test_update_notion_database_falls_back_to_legacy_when_data_sources_fails():
    """Regression for the inverted fallback gate: when the input is a real
    database_id (so ``resolve_data_source_id`` returns a DIFFERENT data_source
    id) and the modern ``PATCH /data_sources/{id}`` fails, the tool MUST retry
    the legacy ``PATCH /databases/{database_id}`` endpoint. The old
    ``ds_id == database_id`` guard skipped it in exactly this (common) case."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _ok({"data_sources": [{"id": "ds-1"}]}),  # resolve db-1 -> ds-1 (different)
            _err("Notion API error (400): validation_error"),  # PATCH /data_sources/ds-1
            _ok({"id": "db-1"}),  # PATCH /databases/db-1 (legacy) succeeds
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="u1",
                name="update_notion_database",
                arguments={
                    "database_id": "db-1",
                    "properties_json": {"New Column": {"rich_text": {}}},
                },
            )
        )
    assert not result.is_error, result.content
    assert "properties updated" in result.content
    assert mock_request.call_count == 3
    legacy_call = mock_request.call_args_list[-1]
    assert legacy_call.args[1] == "patch"
    assert legacy_call.args[2].endswith("/databases/db-1")


@pytest.mark.asyncio
async def test_update_notion_database_keeps_modern_error_when_legacy_also_fails():
    """When both the modern data_sources PATCH and the legacy databases PATCH
    fail, the tool returns the MODERN error (the canonical endpoint's
    diagnostic), not the legacy one."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _ok({"data_sources": [{"id": "ds-1"}]}),  # resolve db-1 -> ds-1
            _err("MODERN data_sources error"),  # PATCH /data_sources/ds-1
            _err("LEGACY databases error"),  # PATCH /databases/db-1
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="u2",
                name="update_notion_database",
                arguments={
                    "database_id": "db-1",
                    "properties_json": {"New Column": {"rich_text": {}}},
                },
            )
        )
    assert "MODERN data_sources error" in result.content
    assert "LEGACY databases error" not in result.content
    assert mock_request.call_count == 3


@pytest.mark.asyncio
async def test_query_notion_database_caches_missing_data_source_per_run():
    """A missing database/data_source ID should be marked as non-retryable for
    the current invocation so repeated tool calls stop re-hitting Notion."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    missing = "missing-db-1"

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _err(
                "Error: Notion resource not found while trying to resolve data source. "
                f"Detail: Could not find database with ID: {missing}."
            ),
            _err(
                "Error: Notion resource not found while trying to check data source. "
                f"Detail: Could not find data source with ID: {missing}."
            ),
        ]
        first = await executor.execute_tool(
            ToolCall(
                id="q1",
                name="query_notion_database",
                arguments={"database_id": missing},
            )
        )
        second = await executor.execute_tool(
            ToolCall(
                id="q2",
                name="query_notion_database",
                arguments={"database_id": missing},
            )
        )

    # resolve_data_source_id now returns _RESOLUTION_FAILED_SENTINEL when both
    # probes fail, so query_notion_database returns an actionable search-guidance
    # error instead of propagating the raw 404.  The sentinel short-circuits the
    # POST /query entirely, so the first call issues exactly two GETs and the
    # second call is fully short-circuited by the in-run missing-resource cache.
    assert "unavailable" in first.content.lower()
    assert "search_notion" in first.content or "list_notion_databases" in first.content
    assert "unavailable" in second.content.lower()
    # First call does two lookup attempts (/databases + /data_sources). The
    # second call is fully short-circuited by the missing-resource cache.
    assert mock_request.call_count == 2


@pytest.mark.asyncio
async def test_list_notion_block_children_caches_missing_block_per_run():
    """When a block ID is gone/unshared, follow-up calls in the same run should
    return the cached actionable error instead of repeating the API call."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    missing = "missing-block-1"

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = _err(
            "Error: Notion resource not found while trying to list block children. "
            f"Detail: Could not find block with ID: {missing}."
        )
        first = await executor.execute_tool(
            ToolCall(
                id="b1",
                name="list_notion_block_children",
                arguments={"block_id": missing},
            )
        )
        second = await executor.execute_tool(
            ToolCall(
                id="b2",
                name="list_notion_block_children",
                arguments={"block_id": missing},
            )
        )

    assert "Action required:" in first.content
    assert "skipped for this run" in first.content
    assert "Action required:" in second.content
    assert mock_request.call_count == 1


@pytest.mark.asyncio
async def test_validation_error_with_could_not_find_is_not_cached():
    """Notion 400 validation errors reuse the "Could not find" phrasing for
    *properties* ("Could not find property with name or id") on perfectly
    healthy resources. Those must NOT poison the missing-resource cache -
    the next call (e.g. with a corrected payload) must reach Notion again."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = _err(
            "Error: Notion returned HTTP 400 while trying to list block children. "
            "Could not find property with name or id: Status."
        )
        first = await executor.execute_tool(
            ToolCall(
                id="v1",
                name="list_notion_block_children",
                arguments={"block_id": "live-block-1"},
            )
        )
        second = await executor.execute_tool(
            ToolCall(
                id="v2",
                name="list_notion_block_children",
                arguments={"block_id": "live-block-1"},
            )
        )

    assert "Action required:" not in first.content
    assert "Action required:" not in second.content
    # Both calls reached Notion - nothing was cached as missing.
    assert mock_request.call_count == 2


@pytest.mark.asyncio
async def test_non_get_404_does_not_poison_missing_resource_cache():
    """A POST/PATCH 404 can refer to a resource named in the request BODY
    (e.g. move_notion_page with a missing new parent), not the URL-path
    resource. Such failures must not blacklist the path resource: repeated
    calls still reach Notion and the error is surfaced un-rewritten.

    The new parent must be a syntactically valid Notion UUID —
    ``move_notion_page`` now rejects non-UUID parents pre-flight without
    any HTTP call (so a non-UUID parent would never exercise the cache).
    The parent-type GET probes must answer with a 404 *response* (the
    ``expected_statuses={404}`` opt-in contract) — a bare ``(None, err)``
    would make the tool fail fast before ever issuing the move POST."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    missing_parent = "0f52e4f0-9a1b-4c8d-8e2f-3a4b5c6d7e8f"

    async def _fake_request(_client, method, url, _provider, _action, **_kwargs):
        if method == "get":
            return (
                httpx.Response(404, json={"object": "error", "status": 404}),
                "Error: Notion resource not found while probing move parent type.",
            )
        return _err(
            "Error: Notion resource not found while trying to move page. "
            f"Detail: Could not find page with ID: {missing_parent}."
        )

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = _fake_request
        first = await executor.execute_tool(
            ToolCall(
                id="mv1",
                name="move_notion_page",
                arguments={"page_id": "live-page-1", "new_parent_id": missing_parent},
            )
        )
        second = await executor.execute_tool(
            ToolCall(
                id="mv2",
                name="move_notion_page",
                arguments={"page_id": "live-page-1", "new_parent_id": missing_parent},
            )
        )

    assert "Action required:" not in first.content
    assert "Action required:" not in second.content
    # The move POST itself must reach Notion on BOTH calls — the POST 404
    # must not be cached. (The parent-type GET probes ARE cached after the
    # first call, which is fine: they refer to the URL-path resource.)
    move_posts = [
        c
        for c in mock_request.call_args_list
        if c.args[1] == "post" and c.args[2].endswith("/move")
    ]
    assert len(move_posts) == 2


@pytest.mark.asyncio
async def test_missing_resource_cache_normalises_dashed_and_dashless_ids():
    """The same Notion UUID legitimately appears dashed and dashless across
    tool calls. A miss recorded under one spelling must short-circuit the
    other spelling too."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    dashed = "6c7f3227-eb5f-4b95-92d9-a2e4a3e29e1a"
    dashless = dashed.replace("-", "")

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = _err(
            "Error: Notion resource not found while trying to list block children. "
            f"Detail: Could not find block with ID: {dashed}."
        )
        first = await executor.execute_tool(
            ToolCall(
                id="n1",
                name="list_notion_block_children",
                arguments={"block_id": dashed},
            )
        )
        second = await executor.execute_tool(
            ToolCall(
                id="n2",
                name="list_notion_block_children",
                arguments={"block_id": dashless},
            )
        )

    assert "Action required:" in first.content
    assert "Action required:" in second.content
    # The dashless spelling hit the cache entry created by the dashed call.
    assert mock_request.call_count == 1


@pytest.mark.asyncio
async def test_cached_message_for_later_calls_is_action_agnostic():
    """The cached message must not embed the FIRST call's action text
    ("while trying to list block children") - a later call through a
    different tool would misreport what it was attempting."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    missing = "missing-block-2"

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = _err(
            "Error: Notion resource not found while trying to list block children. "
            f"Detail: Could not find block with ID: {missing}."
        )
        first = await executor.execute_tool(
            ToolCall(
                id="a1",
                name="list_notion_block_children",
                arguments={"block_id": missing},
            )
        )
        # Different tool, same missing block: short-circuited via the cache.
        second = await executor.execute_tool(
            ToolCall(
                id="a2",
                name="get_notion_block",
                arguments={"block_id": missing},
            )
        )

    # First call keeps its own action context plus the guidance.
    assert "list block children" in first.content
    assert "Action required:" in first.content
    # The cached reply names only the resource, never the first call's action.
    assert "list block children" not in second.content
    assert "earlier call in this run" in second.content
    assert "Action required:" in second.content
    assert mock_request.call_count == 1


@pytest.mark.asyncio
async def test_page_property_404_does_not_poison_page_cache():
    """GET /pages/{id}/properties/{prop} returns the same 404 /
    object_not_found when only the PROPERTY is unknown on a healthy page.
    Such sub-resource failures must not blacklist the page itself: repeated
    calls still reach Notion and the error is surfaced un-rewritten."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = _err(
            "Error: Notion resource not found while trying to retrieve page "
            "property. Detail: Could not find property with ID: bogus-prop."
        )
        first = await executor.execute_tool(
            ToolCall(
                id="p1",
                name="get_notion_page_property",
                arguments={"page_id": "live-page-2", "property_id": "bogus-prop"},
            )
        )
        second = await executor.execute_tool(
            ToolCall(
                id="p2",
                name="get_notion_page_property",
                arguments={"page_id": "live-page-2", "property_id": "bogus-prop"},
            )
        )

    assert "Action required:" not in first.content
    assert "Action required:" not in second.content
    # The page was never cached as missing - both calls reached Notion.
    assert mock_request.call_count == 2


@pytest.mark.asyncio
async def test_cached_miss_fast_path_honours_expected_statuses_contract():
    """``safe_request``'s ``expected_statuses`` opt-in returns the failed
    ``httpx.Response`` so callers can branch on ``resp.status_code``. The
    cached-miss fast path in ``NotionContext.request`` must honour the same
    contract: opted-in callers get a synthetic 404 response, non-opted-in
    callers keep the historical ``(None, err)`` shape."""
    from app.integrations.providers.notion._common import NotionContext

    ctx = NotionContext("dummy-token")
    url = "https://api.notion.com/v1/blocks/missing-block-3/children"

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = _err(
            "Error: Notion resource not found while trying to list block children. "
            "Detail: Could not find block with ID: missing-block-3."
        )
        async with ctx.client() as client:
            # First call populates the missing-resource cache.
            resp, err = await ctx.request(
                client, "get", url, "Notion", "list block children"
            )
            assert resp is None and err

            # Cached path without opt-in: historical (None, err) contract.
            resp, err = await ctx.request(
                client, "get", url, "Notion", "list block children"
            )
            assert resp is None
            assert err and "earlier call in this run" in err

            # Cached path with opt-in: synthetic 404 response for branching.
            resp, err = await ctx.request(
                client,
                "get",
                url,
                "Notion",
                "list block children",
                expected_statuses={404},
            )
            assert resp is not None
            assert resp.status_code == 404
            assert resp.json()["code"] == "object_not_found"
            assert err and "earlier call in this run" in err

    # Only the first (cache-populating) call reached Notion.
    assert mock_request.call_count == 1
