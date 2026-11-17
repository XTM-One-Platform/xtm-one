# SPDX-License-Identifier: Apache-2.0

"""RSS / Atom feed integration — read news from any RSS or Atom source.

No external API required. Users provide feed URLs when creating the integration
(stored in ``extra_config.feed_urls``, one URL per line). The tools fetch and
parse all configured feeds using ``feedparser``.

Ideal for curated news sources:
  - Cybersecurity: The Hacker News, Krebs on Security, BleepingComputer, CISA, DarkReading
  - Economy: Reuters, Bloomberg, FT, The Economist
  - Technology: Ars Technica, TechCrunch, The Verge, Wired
  - General: BBC, AP News, NPR
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import re
from typing import Any

import feedparser
import httpx

from app.core.egress import egress_client
from app.core.url_identity import redact_url
from app.integrations.http import PLATFORM_USER_AGENT, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "RSS"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_feed_urls(token: str) -> list[str]:
    """Extract feed URLs from the token (newline or comma separated)."""
    urls: list[str] = []
    for line in token.replace(",", "\n").splitlines():
        url = line.strip()
        if url and url.startswith(("http://", "https://")):
            urls.append(url)
    return urls


def _format_entry(entry: dict, feed_title: str = "") -> str:
    """Format a single feed entry into a readable string."""
    lines: list[str] = []
    title = entry.get("title", "Untitled")
    source = feed_title or entry.get("source", {}).get("title", "")
    header = f"**{title}**"
    if source:
        header += f" — {source}"
    lines.append(header)

    if entry.get("author"):
        lines.append(f"  Author: {entry['author']}")

    # Published date
    published = entry.get("published") or entry.get("updated") or ""
    if published:
        lines.append(f"  Published: {published}")

    # Summary / description
    summary = entry.get("summary") or entry.get("description") or ""
    if summary:
        clean = re.sub(r"<[^>]+>", "", summary).strip()
        if len(clean) > 400:
            clean = clean[:400] + "..."
        lines.append(f"  {clean}")

    # Link
    link = entry.get("link") or ""
    if link:
        lines.append(f"  URL: {link}")

    # Enclosures (a podcast episode, an attached PDF or image)
    for enclosure in (entry.get("enclosures") or [])[:5]:
        href = enclosure.get("href") or enclosure.get("url") if isinstance(enclosure, dict) else None
        if href:
            kind = enclosure.get("type") or "file"
            lines.append(f"  Enclosure ({kind}): {href}")

    # Tags / categories
    tags = entry.get("tags", [])
    if tags:
        tag_names = [t.get("term", "") for t in tags if t.get("term")]
        if tag_names:
            lines.append(f"  Tags: {', '.join(tag_names[:10])}")

    return "\n".join(lines)


async def _fetch_and_parse(
    client: httpx.AsyncClient,
    url: str,
) -> tuple[str, list[dict]]:
    """Fetch a single RSS/Atom feed URL and parse it.

    Returns (feed_title, list_of_entries).
    Network I/O is async (httpx), parsing is synchronous (feedparser)
    but runs in a thread pool to avoid blocking the event loop.
    """
    # A private feed may carry its credential in the URL (``?key=``, userinfo):
    # the action lands in the error ``safe_request`` logs, so it names the
    # feed without them.
    log_url = redact_url(url)
    resp, err = await safe_request(
        client, "get", url, _PROVIDER, f"fetch {log_url}", follow_redirects=True
    )
    if err:
        logger.warning("Failed to fetch RSS feed %s: %s", log_url, err)
        return url, []

    # feedparser is synchronous — run in thread pool
    loop = asyncio.get_running_loop()
    feed = await loop.run_in_executor(None, feedparser.parse, resp.text)
    feed_title = feed.get("feed", {}).get("title", url)
    entries = feed.get("entries", [])
    return feed_title, entries


# ---------------------------------------------------------------------------
# RSS tools
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    """Build RSS/Atom feed tools.

    ``token`` is the feed URLs string (one URL per line).
    """
    feed_urls = _parse_feed_urls(token)

    if not feed_urls:
        logger.debug(
            "RSS integration has no valid feed URLs configured — returning no tools. "
            "Edit the integration and add feed URLs (one per line)."
        )
        return []

    # ── Fetch all feeds ───────────────────────────────────────────────────

    async def fetch_rss_feeds(
        max_entries_per_feed: int = 10,
    ) -> str:
        """Fetch the latest entries from all configured RSS/Atom feeds."""
        if not feed_urls:
            return "No RSS feed URLs configured. Edit the integration and add feed URLs (one per line)."

        all_entries: list[tuple[str, dict]] = []
        async with egress_client(
            timeout=30, headers={"User-Agent": PLATFORM_USER_AGENT}
        ) as client:
            tasks = [_fetch_and_parse(client, url) for url in feed_urls]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        feed_summaries: list[str] = []
        for result in results:
            if isinstance(result, Exception):
                continue
            feed_title, entries = result
            if not entries:
                feed_summaries.append(f"**{feed_title}**: no entries")
                continue
            feed_summaries.append(f"**{feed_title}**: {len(entries)} entries")
            for entry in entries[:max_entries_per_feed]:
                all_entries.append((feed_title, entry))

        if not all_entries:
            return (
                f"Fetched {len(feed_urls)} feed(s) but found no entries.\n"
                + "\n".join(feed_summaries)
            )

        # Sort by published date (newest first). Use feedparser's parsed
        # struct_time fields — the raw ``published`` strings are RFC-822
        # dates ("Tue, 01 Jul 2026 ...") that sort alphabetically, not
        # chronologically. Entries without any parseable date sort last.
        def _sort_key(item: tuple[str, dict]) -> float:
            entry = item[1]
            parsed = entry.get("published_parsed") or entry.get("updated_parsed")
            if parsed:
                try:
                    return calendar.timegm(parsed)
                except (TypeError, ValueError, OverflowError):
                    pass
            return float("-inf")

        all_entries.sort(key=_sort_key, reverse=True)

        formatted = [
            _format_entry(entry, feed_title) for feed_title, entry in all_entries
        ]
        return (
            f"Fetched {len(feed_urls)} feed(s), {len(all_entries)} total entries:\n"
            + " | ".join(feed_summaries)
            + "\n\n"
            + "\n\n".join(formatted)
        )

    # ── Search entries ────────────────────────────────────────────────────

    async def search_rss_entries(
        query: str,
        max_entries_per_feed: int = 20,
    ) -> str:
        """Search across all configured RSS/Atom feeds by keyword."""
        if not feed_urls:
            return "No RSS feed URLs configured. Edit the integration and add feed URLs (one per line)."

        query_lower = query.lower()
        matching: list[str] = []

        async with egress_client(
            timeout=30, headers={"User-Agent": PLATFORM_USER_AGENT}
        ) as client:
            tasks = [_fetch_and_parse(client, url) for url in feed_urls]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                continue
            feed_title, entries = result
            for entry in entries[:max_entries_per_feed]:
                text = " ".join(
                    [
                        entry.get("title", ""),
                        entry.get("summary", ""),
                        entry.get("description", ""),
                        " ".join(t.get("term", "") for t in entry.get("tags", [])),
                    ]
                ).lower()
                if query_lower in text:
                    matching.append(_format_entry(entry, feed_title))

        if not matching:
            return (
                f"No entries matching '{query}' found across {len(feed_urls)} feed(s)."
            )
        return f"Found {len(matching)} entries matching '{query}':\n\n" + "\n\n".join(
            matching[:30]
        )  # Cap at 30 results

    # ── List configured feeds ─────────────────────────────────────────────

    async def list_rss_feeds() -> str:
        """List all configured RSS/Atom feed URLs and their current status."""
        if not feed_urls:
            return "No RSS feed URLs configured."

        lines: list[str] = []
        async with egress_client(
            timeout=20, headers={"User-Agent": PLATFORM_USER_AGENT}
        ) as client:
            # Fetch all feeds concurrently (mirrors the sibling tools) —
            # a sequential loop makes status listing O(feeds x latency).
            results = await asyncio.gather(
                *(_fetch_and_parse(client, url) for url in feed_urls),
                return_exceptions=True,
            )

        for url, result in zip(feed_urls, results, strict=True):
            if isinstance(result, BaseException):
                lines.append(f"• ❌ {url} — failed to fetch")
                continue
            feed_title, entries = result
            latest = ""
            if entries:
                latest_date = (
                    entries[0].get("published")
                    or entries[0].get("updated")
                    or "unknown"
                )
                latest = f" | Latest: {latest_date}"
            lines.append(
                f"• **{feed_title}** — {len(entries)} entries{latest}\n  {url}"
            )

        return f"Configured feeds ({len(feed_urls)}):\n\n" + "\n\n".join(lines)

    # ── Register tools ────────────────────────────────────────────────────

    executor.register("fetch_rss_feeds", fetch_rss_feeds)
    executor.register("search_rss_entries", search_rss_entries)
    executor.register("list_rss_feeds", list_rss_feeds)

    return [
        ToolDefinition(
            name="fetch_rss_feeds",
            description=(
                "Fetch the latest entries from all configured RSS/Atom news feeds. "
                "Returns articles with title, author, date, summary, URL, and tags. "
                "Use this for daily digests or catching up on the latest news."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "max_entries_per_feed": {
                        "type": "integer",
                        "description": "Maximum entries to retrieve per feed (default 10)",
                        "default": 10,
                    },
                },
                "required": [],
            },
            source="integration:rss",
            priority=0,
        ),
        ToolDefinition(
            name="search_rss_entries",
            description=(
                "Search across all configured RSS/Atom feeds by keyword. "
                "Finds entries where the title, summary, or tags contain the search term. "
                "Useful for finding specific topics across multiple news sources."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keyword or phrase to search for in feed entries",
                    },
                    "max_entries_per_feed": {
                        "type": "integer",
                        "description": "Max entries to scan per feed (default 20)",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
            source="integration:rss",
            priority=0,
        ),
        ToolDefinition(
            name="list_rss_feeds",
            description=(
                "List all configured RSS/Atom feed URLs, their titles, entry count, "
                "and latest article date. Useful for checking feed health and coverage."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            source="integration:rss",
            priority=2,
        ),
    ]
