# SPDX-License-Identifier: Apache-2.0

"""NewsAPI integration — search and aggregate news from 100 000+ sources.

API docs: https://newsapi.org/docs
Auth: API key (passed as ``apiKey`` query parameter)
Base URL: https://newsapi.org/v2

Endpoints used:
  - /v2/everything — full-text article search
  - /v2/top-headlines — headlines by category / country
  - /v2/top-headlines/sources — list available sources
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.http import safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "NewsAPI"
_BASE_URL = "https://newsapi.org/v2"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_article(article: dict) -> str:
    """Format a NewsAPI article dict into a readable string."""
    lines: list[str] = []
    title = article.get("title") or "Untitled"
    source = (article.get("source") or {}).get("name", "Unknown source")
    lines.append(f"**{title}** — {source}")
    if article.get("author"):
        lines.append(f"  Author: {article['author']}")
    if article.get("publishedAt"):
        lines.append(f"  Published: {article['publishedAt']}")
    if article.get("description"):
        lines.append(f"  {article['description']}")
    if article.get("url"):
        lines.append(f"  URL: {article['url']}")
    if article.get("urlToImage"):
        lines.append(f"  Image URL: {article['urlToImage']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# NewsAPI tools
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    """Build NewsAPI integration tools.

    ``token`` is the NewsAPI API key.
    """
    api_key = token.strip()

    async def _get(
        client: httpx.AsyncClient, endpoint: str, params: dict
    ) -> tuple[dict | None, str | None]:
        """Issue a GET request to the NewsAPI and return ``(data, None)`` or ``(None, error)``."""
        params["apiKey"] = api_key
        resp, err = await safe_request(
            client, "get", f"{_BASE_URL}/{endpoint}", _PROVIDER, endpoint, params=params
        )
        if err:
            return None, err
        data = resp.json()
        if data.get("status") == "error":
            return None, data.get("message", "Unknown NewsAPI error")
        return data, None

    # ── Search articles ───────────────────────────────────────────────────

    async def search_newsapi_articles(
        query: str,
        language: str = "en",
        sort_by: str = "publishedAt",
        from_date: str = "",
        to_date: str = "",
        page_size: int = 15,
    ) -> str:
        """Search news articles by keyword across 100 000+ sources."""
        params: dict[str, Any] = {
            "q": query,
            "language": language,
            "sortBy": sort_by,
            "pageSize": min(page_size, 50),
        }
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date

        async with egress_client(timeout=30) as client:
            data, err = await _get(client, "everything", params)
        if err:
            return err
        articles = data.get("articles", [])
        if not articles:
            return f"No articles found for '{query}'."
        results = [_format_article(a) for a in articles]
        total = data.get("totalResults", len(articles))
        return (
            f"Found {total} articles for '{query}' (showing {len(results)}):\n\n"
            + "\n\n".join(results)
        )

    # ── Top headlines ─────────────────────────────────────────────────────

    async def get_newsapi_headlines(
        category: str = "",
        country: str = "us",
        query: str = "",
        page_size: int = 15,
    ) -> str:
        """Get current top headlines by category and/or country.

        Categories: business, entertainment, general, health, science, sports, technology.
        Countries: ISO 3166-1 alpha-2 codes (us, gb, fr, de, etc.).
        """
        params: dict[str, Any] = {"pageSize": min(page_size, 50)}
        if category:
            params["category"] = category
        if country:
            params["country"] = country
        if query:
            params["q"] = query

        async with egress_client(timeout=30) as client:
            data, err = await _get(client, "top-headlines", params)
        if err:
            return err
        articles = data.get("articles", [])
        if not articles:
            label = f"category='{category}'" if category else f"country='{country}'"
            return f"No top headlines found for {label}."
        results = [_format_article(a) for a in articles]
        header = "Top headlines"
        if category:
            header += f" — {category.title()}"
        if country:
            header += f" ({country.upper()})"
        return f"{header} ({len(results)} articles):\n\n" + "\n\n".join(results)

    # ── List sources ──────────────────────────────────────────────────────

    async def list_newsapi_sources(
        category: str = "",
        language: str = "en",
        country: str = "",
    ) -> str:
        """List available news sources. Optionally filter by category, language, or country."""
        params: dict[str, Any] = {}
        if category:
            params["category"] = category
        if language:
            params["language"] = language
        if country:
            params["country"] = country

        async with egress_client(timeout=30) as client:
            data, err = await _get(client, "top-headlines/sources", params)
        if err:
            return err
        sources = data.get("sources", [])
        if not sources:
            return "No sources found matching the filters."
        lines: list[str] = []
        for src in sources[:40]:
            line = f"• **{src.get('name', 'Unknown')}** ({src.get('category', '?')})"
            if src.get("description"):
                line += f" — {src['description'][:120]}"
            if src.get("url"):
                line += f"\n  {src['url']}"
            lines.append(line)
        return f"Available sources ({len(sources)}):\n\n" + "\n\n".join(lines)

    # ── Register tools ────────────────────────────────────────────────────

    executor.register("search_newsapi_articles", search_newsapi_articles)
    executor.register("get_newsapi_headlines", get_newsapi_headlines)
    executor.register("list_newsapi_sources", list_newsapi_sources)

    return [
        ToolDefinition(
            name="search_newsapi_articles",
            description=(
                "Search news articles by keyword across 100 000+ sources worldwide. "
                "Returns articles with title, author, source, date, description, and URL. "
                "Supports date range filtering, language, and sort order (relevancy, popularity, publishedAt)."
            ),
            priority=0,
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keyword or phrase (e.g. 'cybersecurity', 'AI regulation', 'Federal Reserve')",
                    },
                    "language": {
                        "type": "string",
                        "description": "ISO 639-1 language code (default: en)",
                        "default": "en",
                    },
                    "sort_by": {
                        "type": "string",
                        "description": "Sort order: relevancy, popularity, or publishedAt (default)",
                        "default": "publishedAt",
                        "enum": ["relevancy", "popularity", "publishedAt"],
                    },
                    "from_date": {
                        "type": "string",
                        "description": "Oldest article date (ISO 8601 format, e.g. 2025-01-01). Optional.",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "Newest article date (ISO 8601 format). Optional.",
                    },
                    "page_size": {
                        "type": "integer",
                        "description": "Number of articles to return (max 50)",
                        "default": 15,
                    },
                },
                "required": ["query"],
            },
            source="integration:newsapi",
        ),
        ToolDefinition(
            name="get_newsapi_headlines",
            description=(
                "Get current top headlines by category and/or country. "
                "Categories: business, entertainment, general, health, science, sports, technology. "
                "Countries: us, gb, fr, de, jp, etc. (ISO 3166-1 alpha-2)."
            ),
            priority=0,
            input_schema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "News category: business, entertainment, general, health, science, sports, technology",
                        "enum": [
                            "business",
                            "entertainment",
                            "general",
                            "health",
                            "science",
                            "sports",
                            "technology",
                        ],
                    },
                    "country": {
                        "type": "string",
                        "description": "Country code (ISO 3166-1 alpha-2, e.g. us, gb, fr, de)",
                        "default": "us",
                    },
                    "query": {
                        "type": "string",
                        "description": "Optional keyword to filter headlines",
                    },
                    "page_size": {
                        "type": "integer",
                        "description": "Number of headlines to return (max 50)",
                        "default": 15,
                    },
                },
                "required": [],
            },
            source="integration:newsapi",
        ),
        ToolDefinition(
            name="list_newsapi_sources",
            description=(
                "List available news sources. Filter by category (business, technology, science, etc.), "
                "language (en, fr, de), or country. Useful to discover which sources cover a topic."
            ),
            priority=2,
            input_schema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Filter by category: business, entertainment, general, health, science, sports, technology",
                    },
                    "language": {
                        "type": "string",
                        "description": "Filter by language (ISO 639-1, e.g. en, fr)",
                        "default": "en",
                    },
                    "country": {
                        "type": "string",
                        "description": "Filter by country (ISO 3166-1 alpha-2)",
                    },
                },
                "required": [],
            },
            source="integration:newsapi",
        ),
    ]
