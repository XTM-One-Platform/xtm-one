"""Notion search + hierarchy browsing tools."""

from __future__ import annotations

import asyncio

from app.tools.base import ToolDefinition

from ._common import (
    _BASE_URL,
    _PROVIDER,
    AutoRefreshClient,
    NotionContext,
    _extract_title,
    logger,
)
from .formatting import _summarize_properties

# Absolute ceiling on how many levels of *nested container* blocks (toggles,
# columns, callouts, ...) ``list_notion_children`` will follow between two page
# levels. Containers are traversed transparently - they do NOT count toward the
# user-facing ``max_depth`` (which bounds *page* nesting) so that child pages
# wrapped in containers are still discovered - but without this ceiling a
# pathologically deep container chain would drive effectively unbounded
# recursion and API calls. It resets whenever the walk descends into a real
# child page (it bounds container nesting *between* page levels).
_MAX_CONTAINER_NESTING = 20


def register(ctx: NotionContext, executor) -> list[ToolDefinition]:
    _client = ctx.client
    safe_request = ctx.request
    _throttled_request = ctx.request
    _fetch_page_preview = ctx.fetch_page_preview

    # ── Search ─────────────────────────────────────────────────────────────

    async def search_notion(query: str, count: int = 20) -> str:
        """Search across all Notion pages and databases the integration has access to.

        Returns titles, IDs, URLs, parent context, database properties
        (for pages inside databases), and a content preview (~500 chars)
        for each page so you can judge relevance before reading the full page.
        Only use ``get_notion_page`` when you need the complete content.

        **Important:** The search is intentionally unfiltered (returns both
        pages and databases) because adding a type filter causes the Notion API
        to miss deeply nested pages.
        """
        page_size = min(count, 100)
        payload: dict = {
            "query": query,
            "page_size": page_size,
        }

        logger.info("search_notion: query=%r count=%d", query, count)

        all_results: list[dict] = []
        async with _client() as client:
            page_num = 0
            while len(all_results) < count:
                page_num += 1
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{_BASE_URL}/search",
                    _PROVIDER,
                    "search",
                    json=payload,
                )
                if err:
                    return err
                data = resp.json()
                results = data.get("results", [])
                logger.info(
                    "search_notion: page %d → %d results (has_more=%s, total=%d)",
                    page_num,
                    len(results),
                    data.get("has_more"),
                    len(all_results) + len(results),
                )
                for r in results:
                    logger.debug(
                        "search_notion: result object=%s title=%r id=%s",
                        r.get("object"),
                        _extract_title(r),
                        r.get("id", "")[:12],
                    )
                all_results.extend(results)
                if not data.get("has_more") or not data.get("next_cursor"):
                    break
                payload["start_cursor"] = data["next_cursor"]

            if not all_results:
                logger.info("search_notion: no results for query=%r", query)
                return f"No results found for '{query}'."

            trimmed = all_results[:count]
            logger.info(
                "search_notion: returning %d results for query=%r",
                len(trimmed),
                query,
            )

            # ── Phase 3: Parallel content previews ─────────────────────
            page_ids = [
                r["id"] for r in trimmed if r.get("object") == "page" and "id" in r
            ]
            previews: dict[str, str] = {}
            if page_ids:
                preview_tasks = {
                    pid: _fetch_page_preview(client, pid) for pid in page_ids
                }
                previews = dict(
                    zip(
                        preview_tasks.keys(),
                        await asyncio.gather(*preview_tasks.values()),
                        strict=True,
                    )
                )

        # ── Format output ──────────────────────────────────────────────
        lines = []
        for r in trimmed:
            obj_type = r.get("object", "")
            title = _extract_title(r)
            url = r.get("url", "")

            display_type = "DATABASE" if obj_type == "data_source" else obj_type.upper()
            entry = f"[{display_type}] {title}\n  ID: {r['id']}\n  URL: {url}"

            parent = r.get("parent", {})
            parent_type = parent.get("type", "")
            if parent_type == "database_id":
                entry += f"\n  Parent database: {parent.get('database_id', '')}"
            elif parent_type == "page_id":
                entry += f"\n  Parent page: {parent.get('page_id', '')}"
            elif parent_type == "block_id":
                entry += f"\n  Parent block: {parent.get('block_id', '')}"
            elif parent_type == "workspace":
                entry += "\n  Parent: workspace (top-level)"

            if obj_type == "page":
                props = r.get("properties", {})
                if props:
                    prop_summary = _summarize_properties(props)
                    if prop_summary and prop_summary != "No properties":
                        entry += f"\n  Properties: {prop_summary}"

            preview = previews.get(r["id"], "")
            if preview:
                entry += f"\n  Preview: {preview}"
            lines.append(entry)
        return "\n\n".join(lines)

    # ── Browse (tree-walk) ────────────────────────────────────────────────

    async def list_notion_children(page_id: str, max_depth: int = 3) -> str:
        """Browse a Notion page's hierarchy and list all child pages and databases.

        The Notion search API does NOT reliably index pages nested inside
        callouts, columns, or toggle blocks.  This tool recursively walks
        the block tree from the given page and returns **only** child_page
        and child_database entries — no text blocks, no dividers.

        Use this when ``search_notion`` returns no results or unexpected
        results for a page you know exists — navigate the parent page
        tree to discover it.
        """
        logger.info("list_notion_children: page_id=%s max_depth=%d", page_id, max_depth)

        found: list[dict] = []  # [{title, id, type, depth}]
        errors: list[str] = []  # API errors hit while walking the tree
        truncated = False  # set when the container-nesting ceiling is reached

        async def _walk(
            client: AutoRefreshClient,
            block_id: str,
            depth: int,
            container_depth: int = 0,
        ) -> None:
            # ``depth`` bounds *page* nesting (the user-facing ``max_depth``).
            # ``container_depth`` bounds traversal through transparent container
            # blocks (toggles / columns / callouts) between two page levels and
            # resets when we descend into a real child page, so a pathologically
            # deep container chain cannot drive unbounded recursion / API calls.
            if depth > max_depth:
                return
            if container_depth > _MAX_CONTAINER_NESTING:
                nonlocal truncated
                truncated = True
                return
            params: dict[str, str] = {"page_size": "100"}
            while True:
                resp, err = await _throttled_request(
                    client,
                    "get",
                    f"{_BASE_URL}/blocks/{block_id}/children",
                    _PROVIDER,
                    "browse children",
                    # Below the root, an id read off the tree: never the
                    # run's stale reference.
                    report_stale=depth == 0 and container_depth == 0,
                    params=params,
                )
                if err:
                    # Record the failure rather than silently stopping: a
                    # failed children fetch must not masquerade as "no
                    # children" - that would mislead the agent into acting on
                    # an incomplete (or empty) listing as if it were complete.
                    errors.append(err)
                    break
                data = resp.json()
                results = data.get("results", [])

                recurse_tasks = []
                for b in results:
                    btype = b.get("type", "")
                    if btype == "child_page":
                        t = b.get("child_page", {}).get("title", "Untitled")
                        found.append(
                            {"title": t, "id": b["id"], "type": "page", "depth": depth}
                        )
                        # Also recurse into child pages
                        if depth < max_depth:
                            recurse_tasks.append(_walk(client, b["id"], depth + 1))
                    elif btype == "child_database":
                        t = b.get("child_database", {}).get("title", "Untitled")
                        found.append(
                            {
                                "title": t,
                                "id": b["id"],
                                "type": "database",
                                "depth": depth,
                            }
                        )
                    elif b.get("has_children"):
                        # Recurse into containers (callouts, columns, toggles,
                        # etc.) transparently: keep the same page ``depth`` but
                        # advance ``container_depth`` so the ceiling still bounds
                        # a pathologically deep container chain.
                        recurse_tasks.append(
                            _walk(client, b["id"], depth, container_depth + 1)
                        )

                if recurse_tasks:
                    await asyncio.gather(*recurse_tasks)

                if not data.get("has_more") or not data.get("next_cursor"):
                    break
                params["start_cursor"] = data["next_cursor"]

        async with _client() as client:
            # Also get the page title for context
            meta_resp, meta_err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/pages/{page_id}",
                _PROVIDER,
                "get page title",
            )
            page_title = _extract_title(meta_resp.json()) if not meta_err else page_id

            await _walk(client, page_id, 0)

        if not found:
            # Surface a real API error instead of a misleading empty result so
            # the agent doesn't conclude the page has no children when the walk
            # actually failed (e.g. the page isn't shared with the integration).
            if errors:
                return f"Could not browse '{page_title}': {errors[0]}"
            if truncated:
                # The walk bailed on the container-nesting ceiling before
                # reaching any page/database - say so rather than implying the
                # page is genuinely empty.
                return (
                    f"No child pages or databases found inside '{page_title}' "
                    f"within the traversal limit ({_MAX_CONTAINER_NESTING} "
                    "levels of nested containers); the page may nest content "
                    "more deeply than this tool follows."
                )
            logger.info("list_notion_children: no child pages/databases in %s", page_id)
            return f"No child pages or databases found inside '{page_title}'."

        logger.info(
            "list_notion_children: found %d child pages/databases in %s (%s)",
            len(found),
            page_id,
            page_title,
        )
        lines = [f"Child pages/databases inside '{page_title}':\n"]
        for item in found:
            indent = "  " * item["depth"]
            icon = "📄" if item["type"] == "page" else "🗃️"
            lines.append(f"{indent}{icon} {item['title']}\n{indent}   ID: {item['id']}")
        # Flag a partial listing so the caller doesn't treat it as exhaustive,
        # whether the gap was a failed branch or the container-nesting ceiling.
        notes: list[str] = []
        if truncated:
            notes.append(
                f"traversal stopped after {_MAX_CONTAINER_NESTING} levels of "
                "nested containers, so deeply-nested children may be missing"
            )
        if errors:
            notes.append(
                f"some branches could not be read in full (first error: {errors[0]})"
            )
        if notes:
            lines.append(
                "\n(Note: this listing may be incomplete - " + "; ".join(notes) + ".)"
            )
        return "\n".join(lines)

    executor.register("search_notion", search_notion)
    executor.register("list_notion_children", list_notion_children)

    return [
        # ── Search ─────────────────────────────────────────────────────
        ToolDefinition(
            name="search_notion",
            priority=0,
            description=(
                "Search Notion pages and databases by title. Returns titles, "
                "IDs, URLs, parent context (which database/page a result "
                "belongs to), database entry properties (metadata columns), "
                "and a **content preview** (~500 chars) for each page so you "
                "can judge relevance without reading the full page. Results "
                "are ranked by relevance to the query. Only call "
                "get_notion_page when you need the complete content of a "
                "specific page."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search text (matches page/database titles)",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max results (default 20).",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
            source="integration:notion",
        ),
        ToolDefinition(
            name="list_notion_children",
            description=(
                "Browse a Notion page's hierarchy — recursively lists all child "
                "pages and databases nested inside a page (including those inside "
                "callouts, columns, and toggles). Use this when ``search_notion`` "
                "doesn't find a page you expect to exist: first search for a "
                "parent/section page, then browse its children to locate the "
                "target page. Returns titles and IDs for each child."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "page_id": {
                        "type": "string",
                        "description": "Notion page ID to browse children of",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Max depth to recurse (default 3)",
                        "default": 3,
                    },
                },
                "required": ["page_id"],
            },
            source="integration:notion",
            priority=1,
        ),
    ]
