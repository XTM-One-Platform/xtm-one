# SPDX-License-Identifier: Apache-2.0

"""Google Custom Search integration — search the web via Google's Custom Search JSON API.

API docs: https://developers.google.com/custom-search/v1/overview
Auth: API key + Custom Search Engine ID (cx)
Base URL: https://www.googleapis.com/customsearch/v1

The token is passed as ``search_engine_id||api_key`` (separated by ``||``).
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.http import parse_compound_token, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "Google Search"
_BASE_URL = "https://www.googleapis.com/customsearch/v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_result(item: dict) -> str:
    """Format a Google Custom Search result into a readable string."""
    lines: list[str] = []
    title = item.get("title", "Untitled")
    link = item.get("link", "")
    display_link = item.get("displayLink", "")
    lines.append(f"**{title}**")
    if display_link:
        lines.append(f"  Source: {display_link}")
    if item.get("snippet"):
        lines.append(f"  {item['snippet']}")
    if link:
        lines.append(f"  URL: {link}")
    return "\n".join(lines)


def _format_image_result(item: dict) -> str:
    """Format a Google Custom Search image result into a readable string.

    Includes a markdown image tag for the thumbnail so chat UIs render a
    visual preview alongside the metadata.
    """
    lines: list[str] = []
    title = item.get("title", "Untitled")
    link = item.get("link", "")  # Direct image URL
    display_link = item.get("displayLink", "")
    image_meta = item.get("image", {})
    context_link = image_meta.get("contextLink", "")  # Page containing the image
    width = image_meta.get("width", "")
    height = image_meta.get("height", "")
    thumbnail = image_meta.get("thumbnailLink", "")
    lines.append(f"**{title}**")
    # Render thumbnail as inline markdown image so chat renders a preview
    if thumbnail:
        lines.append(f"![{title}]({thumbnail})")
    if display_link:
        lines.append(f"  Source: {display_link}")
    if item.get("snippet"):
        lines.append(f"  {item['snippet']}")
    if width and height:
        lines.append(f"  Dimensions: {width}x{height}")
    if link:
        lines.append(f"  Full-size image: {link}")
    if context_link:
        lines.append(f"  Page URL: {context_link}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Google Search tools
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    """Build Google Custom Search integration tools.

    ``token`` is formatted as ``search_engine_id||api_key``.
    """
    cx, api_key = parse_compound_token(token, 2)
    cx, api_key = cx.strip(), api_key.strip()

    async def _get(
        client: httpx.AsyncClient, params: dict, action: str = "search"
    ) -> tuple[dict | None, str | None]:
        """Issue a GET and return ``(data, None)`` or ``(None, error)``."""
        params["key"] = api_key
        if cx:
            params["cx"] = cx
        resp, err = await safe_request(
            client, "get", _BASE_URL, _PROVIDER, action, params=params
        )
        if err:
            return None, err
        return resp.json(), None

    # ── Web search ────────────────────────────────────────────────────────

    async def search_google_web(
        query: str,
        num_results: int = 10,
        start_index: int = 1,
        language: str = "",
        date_restrict: str = "",
        site: str = "",
        file_type: str = "",
        exact_terms: str = "",
        exclude_terms: str = "",
    ) -> str:
        """Search the web using Google Search. Returns titles, snippets, and URLs."""
        params: dict[str, Any] = {
            "q": query,
            "num": min(num_results, 10),  # Google API max is 10 per request
        }
        if start_index > 1:
            params["start"] = start_index
        if language:
            params["lr"] = (
                f"lang_{language}" if not language.startswith("lang_") else language
            )
        if date_restrict:
            params["dateRestrict"] = date_restrict
        if site:
            params["siteSearch"] = site
            params["siteSearchFilter"] = "i"  # include
        if file_type:
            params["fileType"] = file_type
        if exact_terms:
            params["exactTerms"] = exact_terms
        if exclude_terms:
            params["excludeTerms"] = exclude_terms

        async with egress_client(timeout=30) as client:
            data, err = await _get(client, params, "search the web")
        if err:
            return err
        items = data.get("items", [])
        if not items:
            return f"No results found for '{query}'."
        results = [_format_result(item) for item in items]
        total = data.get("searchInformation", {}).get("totalResults", str(len(items)))
        formatted_total = data.get("searchInformation", {}).get(
            "formattedTotalResults", total
        )
        search_time = data.get("searchInformation", {}).get("formattedSearchTime", "")
        header = f"Google Search: ~{formatted_total} results for '{query}'"
        if search_time:
            header += f" ({search_time}s)"
        header += f" — showing {len(results)}:"
        return header + "\n\n" + "\n\n".join(results)

    # ── Image search ─────────────────────────────────────────────────────

    async def search_google_images(
        query: str,
        num_results: int = 10,
        size: str = "",
        image_type: str = "",
        color_type: str = "",
        dominant_color: str = "",
        safe: str = "active",
    ) -> str:
        """Search for images and photos on the web using Google Image Search.

        Returns image URLs, inline thumbnail previews (markdown), dimensions,
        and source pages.  Supports filtering by size, type (photo, face,
        clipart, etc.), color, and safety level.

        Requires "Image search" to be enabled in the Programmable Search Engine
        settings (programmablesearchengine.google.com).
        """
        params: dict[str, Any] = {
            "q": query,
            "num": min(num_results, 10),
            "searchType": "image",
        }
        if size:
            params["imgSize"] = size
        if image_type:
            params["imgType"] = image_type
        if color_type:
            params["imgColorType"] = color_type
        if dominant_color:
            params["imgDominantColor"] = dominant_color
        if safe:
            params["safe"] = safe

        async with egress_client(timeout=30) as client:
            data, err = await _get(client, params, "search images")
        if err:
            return err
        items = data.get("items", [])
        if not items:
            return f"No images found for '{query}'."
        results = [_format_image_result(item) for item in items]
        total = data.get("searchInformation", {}).get("totalResults", str(len(items)))
        formatted_total = data.get("searchInformation", {}).get(
            "formattedTotalResults", total
        )
        header = f"Google Image Search: ~{formatted_total} images for '{query}' — showing {len(results)}:"
        return header + "\n\n" + "\n\n".join(results)

    # ── Register tools ────────────────────────────────────────────────────

    executor.register("search_google_web", search_google_web)
    executor.register("search_google_images", search_google_images)

    return [
        ToolDefinition(
            name="search_google_web",
            description=(
                "Search the web using Google Search. Returns web page titles, snippets, and URLs. "
                "Supports filtering by language, date range, file type, site restriction, and exclusions. "
                "Use this for any internet lookup: articles, documentation, news, current events, products, "
                "companies, people, or research. Use the 'site' parameter to restrict to a specific domain, "
                "and 'date_restrict' (e.g. 'd7', 'w1', 'm3') to limit to recent content."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (e.g. 'latest cybersecurity threats 2025', 'Python FastAPI tutorial', 'company name funding round')",
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "Number of results to return (max 10)",
                        "default": 10,
                    },
                    "start_index": {
                        "type": "integer",
                        "description": "Start index for pagination (1-based). Use 11 for page 2, 21 for page 3, etc.",
                        "default": 1,
                    },
                    "language": {
                        "type": "string",
                        "description": "Restrict results to a language (ISO 639-1 code, e.g. 'en', 'fr', 'de'). Optional.",
                    },
                    "date_restrict": {
                        "type": "string",
                        "description": "Restrict by age: d[N] (days), w[N] (weeks), m[N] (months), y[N] (years). E.g. 'd7' = past 7 days, 'm3' = past 3 months.",
                    },
                    "site": {
                        "type": "string",
                        "description": "Restrict results to a specific site/domain (e.g. 'github.com', 'stackoverflow.com').",
                    },
                    "file_type": {
                        "type": "string",
                        "description": "Filter by file type (e.g. 'pdf', 'doc', 'xls', 'ppt').",
                    },
                    "exact_terms": {
                        "type": "string",
                        "description": "Phrase that must appear in all results.",
                    },
                    "exclude_terms": {
                        "type": "string",
                        "description": "Terms to exclude from results.",
                    },
                },
                "required": ["query"],
            },
            source="integration:google_search",
            priority=0,
        ),
        ToolDefinition(
            name="search_google_images",
            description=(
                "Search for images on the web using Google Image Search. Returns image URLs, inline thumbnail "
                "previews, dimensions, and source pages. Use this when asked to find, look up, or show images "
                "or photos: logos, portraits, product shots, infographics, diagrams, screenshots, etc. "
                "Supports filtering by size, type (photo, face, clipart), and color."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Image search query (e.g. 'cybersecurity infographic', 'Python logo', 'CEO portrait professional')",
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "Number of image results to return (max 10)",
                        "default": 10,
                    },
                    "size": {
                        "type": "string",
                        "description": "Filter by image size",
                        "enum": [
                            "huge",
                            "icon",
                            "large",
                            "medium",
                            "small",
                            "xlarge",
                            "xxlarge",
                        ],
                    },
                    "image_type": {
                        "type": "string",
                        "description": "Filter by image type",
                        "enum": [
                            "clipart",
                            "face",
                            "lineart",
                            "stock",
                            "photo",
                            "animated",
                        ],
                    },
                    "color_type": {
                        "type": "string",
                        "description": "Filter by color properties",
                        "enum": ["color", "gray", "mono", "trans"],
                    },
                    "dominant_color": {
                        "type": "string",
                        "description": "Filter by dominant color",
                        "enum": [
                            "black",
                            "blue",
                            "brown",
                            "gray",
                            "green",
                            "orange",
                            "pink",
                            "purple",
                            "red",
                            "teal",
                            "white",
                            "yellow",
                        ],
                    },
                    "safe": {
                        "type": "string",
                        "description": "SafeSearch level (default: active)",
                        "enum": ["active", "off"],
                        "default": "active",
                    },
                },
                "required": ["query"],
            },
            source="integration:google_search",
            priority=1,
        ),
    ]
