# SPDX-License-Identifier: Apache-2.0

"""Brave Search integration: web, news, images, videos, suggest, spellcheck,
LLM context, AI answers and local POIs from Brave's own independent index.

API docs: https://api-dashboard.search.brave.com/documentation
Auth: one subscription token sent as the ``X-Subscription-Token`` header.
Base URL: https://api.search.brave.com/res/v1/

One tool per endpoint. Each answers readable markdown (title, source, age,
snippet, extra snippets, URL; image and video thumbnails as markdown images)
and every failure as one line: an invalid key, an endpoint the subscription
does not include, a rate limit with the wait Brave asks for, never a trace.

The web and image tools also answer to the names of the Google Custom Search
tools (``search_google_web``, ``search_google_images``) as declared aliases:
an agent whose persona names the Google tools keeps searching the web through
Brave once Google is gone (Google's Custom Search JSON API stops on
1 January 2027). An exact name always wins, so a deployment that still binds
Google keeps calling Google.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.http import safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "Brave Search"
#: A decoded JSON object of a Brave response.
_Json = dict[str, Any]
BASE_URL = "https://api.search.brave.com/res/v1"
TOKEN_HEADER = "X-Subscription-Token"  # noqa: S105 - a header name, not a secret
_SOURCE = "integration:brave_search"
_TIMEOUT = 30
#: Wall-clock budget of one call, 429 retries included.
_CALL_BUDGET_SECONDS = 25.0

_FRESHNESS_RE = re.compile(r"^(pd|pw|pm|py|\d{4}-\d{2}-\d{2}to\d{4}-\d{2}-\d{2})$")
_SAFESEARCH = ("off", "moderate", "strict")
_WEB_RESULT_FILTERS = (
    "discussions",
    "faq",
    "infobox",
    "news",
    "query",
    "summarizer",
    "videos",
    "web",
    "locations",
)


# ---------------------------------------------------------------------------
# Request and error handling
# ---------------------------------------------------------------------------


def _rate_limit_wait(resp: httpx.Response) -> str:
    """The wait Brave asks for, from ``Retry-After`` or ``X-RateLimit-Reset``."""
    retry_after = (resp.headers.get("Retry-After") or "").strip()
    if retry_after.isdigit():
        return f" Retry after {retry_after} s."
    reset = resp.headers.get("X-RateLimit-Reset") or ""
    # "1, 1419704": the per-second window first, then the monthly one.
    windows = [w.strip() for w in reset.split(",") if w.strip().isdigit()]
    remaining = [r.strip() for r in (resp.headers.get("X-RateLimit-Remaining") or "").split(",")]
    if len(windows) > 1 and len(remaining) > 1 and remaining[1] == "0":
        hours = int(windows[1]) // 3600
        return f" The monthly quota of the plan is used up; it resets in about {hours} h."
    if windows:
        return f" Retry after {windows[0]} s."
    return ""


def _error_detail(resp: httpx.Response) -> str:
    """Brave's ``{"type": "ErrorResponse", "error": {"detail", "code"}}`` detail."""
    try:
        body = resp.json()
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return ""
    detail = str(error.get("detail") or error.get("code") or "").strip()
    return detail[:200]


def format_brave_error(resp: httpx.Response, action: str) -> str:
    """One readable line for a failed Brave Search call, never a trace."""
    status = resp.status_code
    detail = _error_detail(resp)
    suffix = f" Detail: {detail}" if detail else ""
    if status == 401:
        return (
            f"Error: {_PROVIDER} rejected the API key while trying to {action}: "
            "the key is invalid or revoked. Check the key of the integration."
        )
    if status == 403:
        return (
            f"Error: {_PROVIDER} refused to {action}: the subscription of this API key "
            f"does not include this endpoint. Choose a plan that does in the Brave "
            f"Search API dashboard.{suffix}"
        )
    if status == 422:
        return f"Error: {_PROVIDER} rejected the parameters while trying to {action}.{suffix}"
    if status == 429:
        return f"Error: {_PROVIDER} rate limit reached while trying to {action}.{_rate_limit_wait(resp)}"
    if status >= 500:
        return f"Error: {_PROVIDER} is unavailable (HTTP {status}) while trying to {action}. Try again later."
    return f"Error: {_PROVIDER} returned HTTP {status} while trying to {action}.{suffix}"


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        TOKEN_HEADER: api_key,
    }


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _validate_common(freshness: str, safesearch: str) -> str | None:
    if freshness and not _FRESHNESS_RE.match(freshness):
        return (
            "Error: freshness must be pd (24 h), pw (7 days), pm (31 days), py (365 days) "
            "or a range YYYY-MM-DDtoYYYY-MM-DD."
        )
    if safesearch and safesearch not in _SAFESEARCH:
        return "Error: safesearch must be off, moderate or strict."
    return None


def _locale_params(country: str, search_lang: str, ui_lang: str) -> dict[str, str]:
    params: dict[str, str] = {}
    if country:
        params["country"] = country.strip().upper()
    if search_lang:
        params["search_lang"] = search_lang.strip().lower()
    if ui_lang:
        params["ui_lang"] = ui_lang.strip()
    return params


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _clean(text: Any) -> str:
    """Strip the ``<strong>`` decorations Brave may leave in snippets."""
    return re.sub(r"</?strong>", "", str(text or "")).strip()


def _source(item: _Json) -> str:
    meta = item.get("meta_url") or {}
    profile = item.get("profile") or {}
    return str(profile.get("long_name") or profile.get("name") or meta.get("hostname") or "").strip()


def _format_web_result(item: _Json) -> str:
    lines = [f"**{_clean(item.get('title')) or 'Untitled'}**"]
    meta = " | ".join(p for p in (_source(item), str(item.get("age") or "").strip()) if p)
    if meta:
        lines.append(f"  Source: {meta}")
    if item.get("description"):
        lines.append(f"  {_clean(item['description'])}")
    for extra in (item.get("extra_snippets") or [])[:5]:
        lines.append(f"  - {_clean(extra)}")
    if item.get("url"):
        lines.append(f"  URL: {item['url']}")
    return "\n".join(lines)


def _format_thumbnail(item: _Json, title: str) -> str | None:
    thumb = (item.get("thumbnail") or {}).get("src")
    return f"![{title}]({thumb})" if thumb else None


def _format_news_result(item: _Json) -> str:
    title = _clean(item.get("title")) or "Untitled"
    lines = [f"**{title}**"]
    meta = [p for p in (_source(item), str(item.get("age") or "").strip()) if p]
    if item.get("breaking"):
        meta.append("breaking")
    if meta:
        lines.append(f"  Source: {' | '.join(meta)}")
    if item.get("description"):
        lines.append(f"  {_clean(item['description'])}")
    for extra in (item.get("extra_snippets") or [])[:3]:
        lines.append(f"  - {_clean(extra)}")
    if item.get("url"):
        lines.append(f"  URL: {item['url']}")
    return "\n".join(lines)


def _format_image_result(item: _Json) -> str:
    title = _clean(item.get("title")) or "Untitled"
    lines = [f"**{title}**"]
    thumbnail = _format_thumbnail(item, title)
    if thumbnail:
        lines.append(thumbnail)
    source = str(item.get("source") or _source(item)).strip()
    if source:
        lines.append(f"  Source: {source}")
    props = item.get("properties") or {}
    if props.get("width") and props.get("height"):
        lines.append(f"  Dimensions: {props['width']}x{props['height']}")
    if props.get("url"):
        lines.append(f"  Full-size image: {props['url']}")
    if item.get("url"):
        lines.append(f"  Page URL: {item['url']}")
    return "\n".join(lines)


def _format_video_result(item: _Json) -> str:
    title = _clean(item.get("title")) or "Untitled"
    lines = [f"**{title}**"]
    thumbnail = _format_thumbnail(item, title)
    if thumbnail:
        lines.append(thumbnail)
    video = item.get("video") or {}
    meta = [
        p
        for p in (
            str(video.get("creator") or video.get("publisher") or _source(item)).strip(),
            str(item.get("age") or "").strip(),
            f"duration {video['duration']}" if video.get("duration") else "",
            f"{video['views']} views" if video.get("views") else "",
        )
        if p
    ]
    if meta:
        lines.append(f"  Source: {' | '.join(meta)}")
    if item.get("description"):
        lines.append(f"  {_clean(item['description'])}")
    if item.get("url"):
        lines.append(f"  URL: {item['url']}")
    return "\n".join(lines)


def _format_poi(item: _Json) -> str:
    lines = [f"**{_clean(item.get('title')) or 'Unnamed place'}**"]
    address = (item.get("postal_address") or {}).get("displayAddress")
    if address:
        lines.append(f"  Address: {address}")
    rating = item.get("rating") or {}
    if rating.get("ratingValue"):
        count = f" ({rating['reviewCount']} reviews)" if rating.get("reviewCount") else ""
        lines.append(f"  Rating: {rating['ratingValue']}{count}")
    phone = (item.get("contact") or {}).get("telephone")
    if phone:
        lines.append(f"  Phone: {phone}")
    if item.get("categories"):
        lines.append(f"  Categories: {', '.join(str(c) for c in item['categories'][:5])}")
    if item.get("price_range"):
        lines.append(f"  Price range: {item['price_range']}")
    if item.get("url"):
        lines.append(f"  URL: {item['url']}")
    if item.get("id"):
        lines.append(f"  POI id: {item['id']}")
    return "\n".join(lines)


def _page_hint(data: _Json, offset: int) -> str:
    more = (data.get("query") or {}).get("more_results_available")
    if more and offset < 9:
        return f"\n\nMore results: call again with offset={offset + 1}."
    return ""


def _altered_query_line(data: _Json) -> str:
    altered = (data.get("query") or {}).get("altered")
    return f"\n(Brave corrected the query to '{altered}'.)" if altered else ""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def build_tools(token: str, executor: CallableToolExecutor, **_kwargs: Any) -> list[ToolDefinition]:
    """Build the Brave Search tools; ``token`` is the subscription token."""
    api_key = (token or "").strip()

    async def _call(
        method: str, path: str, action: str, *, params: Any = None, json: _Json | None = None
    ) -> tuple[_Json, str | None]:
        """One Brave call: ``(data, None)`` or ``({}, one-line error)``."""
        if not api_key:
            return {}, f"Error: {_PROVIDER} has no API key: add it to the integration."
        kwargs: dict[str, Any] = {"headers": _headers(api_key)}
        if params is not None:
            kwargs["params"] = params
        if json is not None:
            kwargs["json"] = json
        async with egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                method,
                f"{BASE_URL}{path}",
                _PROVIDER,
                action,
                expected_statuses=set(),
                max_total_seconds=_CALL_BUDGET_SECONDS,
                **kwargs,
            )
        if resp is None:
            return {}, err or f"Error: {_PROVIDER} could not be reached while trying to {action}."
        if err or resp.status_code >= 300:
            return {}, format_brave_error(resp, action)
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return {}, f"Error: {_PROVIDER} answered a body that is not a JSON object while trying to {action}."
        return body, None

    # ── Web search ────────────────────────────────────────────────────────

    async def search_brave_web(
        query: str,
        count: int = 10,
        offset: int = 0,
        country: str = "",
        search_lang: str = "",
        ui_lang: str = "",
        safesearch: str = "moderate",
        freshness: str = "",
        result_filter: str = "",
        goggles: str = "",
        extra_snippets: bool = True,
        spellcheck: bool = True,
    ) -> str:
        """Search the web with Brave Search."""
        if not (query or "").strip():
            return "Error: query is required."
        invalid = _validate_common(freshness, safesearch)
        if invalid:
            return invalid
        count = _clamp(count, 1, 20, 10)
        offset = _clamp(offset, 0, 9, 0)
        params: dict[str, Any] = {
            "q": query[:400],
            "count": count,
            "offset": offset,
            "text_decorations": "false",
            "extra_snippets": "true" if extra_snippets else "false",
            "spellcheck": "true" if spellcheck else "false",
            **_locale_params(country, search_lang, ui_lang),
        }
        if safesearch:
            params["safesearch"] = safesearch
        if freshness:
            params["freshness"] = freshness
        if result_filter:
            wanted = [f.strip() for f in result_filter.split(",") if f.strip()]
            unknown = [f for f in wanted if f not in _WEB_RESULT_FILTERS]
            if unknown:
                return f"Error: unknown result_filter value(s) {unknown}; allowed: {', '.join(_WEB_RESULT_FILTERS)}."
            params["result_filter"] = ",".join(wanted)
        if goggles:
            params["goggles"] = goggles
        data, err = await _call("get", "/web/search", "search the web", params=params)
        if err:
            return err
        sections: list[str] = []
        web = (data.get("web") or {}).get("results") or []
        if web:
            sections.append("\n\n".join(_format_web_result(r) for r in web))
        news = (data.get("news") or {}).get("results") or []
        if news:
            sections.append("News:\n\n" + "\n\n".join(_format_news_result(r) for r in news[:5]))
        videos = (data.get("videos") or {}).get("results") or []
        if videos:
            sections.append("Videos:\n\n" + "\n\n".join(_format_video_result(r) for r in videos[:3]))
        faqs = (data.get("faq") or {}).get("results") or []
        if faqs:
            sections.append(
                "FAQ:\n\n"
                + "\n\n".join(
                    f"**{_clean(f.get('question'))}**\n  {_clean(f.get('answer'))}\n  URL: {f.get('url', '')}"
                    for f in faqs[:5]
                )
            )
        locations = (data.get("locations") or {}).get("results") or []
        if locations:
            sections.append(
                "Places (pass the POI ids to get_brave_places for details):\n\n"
                + "\n\n".join(_format_poi(p) for p in locations[:5])
            )
        if not sections:
            return f"No results found for '{query}'." + _altered_query_line(data)
        header = f"Brave Search: {len(web)} web result(s) for '{query}' (page {offset + 1})"
        return header + _altered_query_line(data) + ":\n\n" + "\n\n".join(sections) + _page_hint(data, offset)

    # ── News search ───────────────────────────────────────────────────────

    async def search_brave_news(
        query: str,
        count: int = 20,
        offset: int = 0,
        country: str = "",
        search_lang: str = "",
        ui_lang: str = "",
        safesearch: str = "strict",
        freshness: str = "",
        goggles: str = "",
        extra_snippets: bool = False,
        spellcheck: bool = True,
    ) -> str:
        """Search recent news articles with Brave News Search."""
        if not (query or "").strip():
            return "Error: query is required."
        invalid = _validate_common(freshness, safesearch)
        if invalid:
            return invalid
        offset = _clamp(offset, 0, 9, 0)
        params: dict[str, Any] = {
            "q": query[:400],
            "count": _clamp(count, 1, 50, 20),
            "offset": offset,
            "extra_snippets": "true" if extra_snippets else "false",
            "spellcheck": "true" if spellcheck else "false",
            **_locale_params(country, search_lang, ui_lang),
        }
        if safesearch:
            params["safesearch"] = safesearch
        if freshness:
            params["freshness"] = freshness
        if goggles:
            params["goggles"] = goggles
        data, err = await _call("get", "/news/search", "search news", params=params)
        if err:
            return err
        results = data.get("results") or []
        if not results:
            return f"No news found for '{query}'." + _altered_query_line(data)
        header = f"Brave News: {len(results)} article(s) for '{query}' (page {offset + 1})"
        body = "\n\n".join(_format_news_result(r) for r in results)
        return header + _altered_query_line(data) + ":\n\n" + body + _page_hint(data, offset)

    # ── Image search ──────────────────────────────────────────────────────

    async def search_brave_images(
        query: str,
        count: int = 20,
        country: str = "",
        search_lang: str = "",
        safesearch: str = "strict",
        spellcheck: bool = True,
    ) -> str:
        """Search images with Brave Image Search."""
        if not (query or "").strip():
            return "Error: query is required."
        if safesearch and safesearch not in ("off", "strict"):
            return "Error: safesearch must be off or strict for image search."
        params: dict[str, Any] = {
            "q": query[:400],
            "count": _clamp(count, 1, 200, 20),
            "spellcheck": "true" if spellcheck else "false",
            **_locale_params(country, search_lang, ""),
        }
        if safesearch:
            params["safesearch"] = safesearch
        data, err = await _call("get", "/images/search", "search images", params=params)
        if err:
            return err
        results = data.get("results") or []
        if not results:
            return f"No images found for '{query}'."
        header = f"Brave Image Search: {len(results)} image(s) for '{query}':"
        return header + "\n\n" + "\n\n".join(_format_image_result(r) for r in results)

    # ── Video search ──────────────────────────────────────────────────────

    async def search_brave_videos(
        query: str,
        count: int = 20,
        offset: int = 0,
        country: str = "",
        search_lang: str = "",
        ui_lang: str = "",
        safesearch: str = "moderate",
        freshness: str = "",
        spellcheck: bool = True,
    ) -> str:
        """Search videos with Brave Video Search."""
        if not (query or "").strip():
            return "Error: query is required."
        invalid = _validate_common(freshness, safesearch)
        if invalid:
            return invalid
        offset = _clamp(offset, 0, 9, 0)
        params: dict[str, Any] = {
            "q": query[:400],
            "count": _clamp(count, 1, 50, 20),
            "offset": offset,
            "spellcheck": "true" if spellcheck else "false",
            **_locale_params(country, search_lang, ui_lang),
        }
        if safesearch:
            params["safesearch"] = safesearch
        if freshness:
            params["freshness"] = freshness
        data, err = await _call("get", "/videos/search", "search videos", params=params)
        if err:
            return err
        results = data.get("results") or []
        if not results:
            return f"No videos found for '{query}'."
        header = f"Brave Video Search: {len(results)} video(s) for '{query}' (page {offset + 1}):"
        return header + "\n\n" + "\n\n".join(_format_video_result(r) for r in results) + _page_hint(data, offset)

    # ── Suggest ───────────────────────────────────────────────────────────

    async def get_brave_suggestions(query: str, count: int = 5, country: str = "", rich: bool = False) -> str:
        """Autocomplete suggestions for a partial query."""
        if not (query or "").strip():
            return "Error: query is required."
        params: dict[str, Any] = {"q": query[:400], "count": _clamp(count, 1, 20, 5)}
        if country:
            params["country"] = country.strip().upper()
        if rich:
            params["rich"] = "true"
        data, err = await _call("get", "/suggest/search", "suggest queries", params=params)
        if err:
            return err
        results = data.get("results") or []
        if not results:
            return f"No suggestions for '{query}'."
        lines = []
        for r in results:
            line = f"- {r.get('query', '')}"
            if r.get("is_entity") and r.get("title"):
                line += f" (entity: {r['title']}"
                line += f", {r['description']})" if r.get("description") else ")"
            lines.append(line)
        return f"Brave suggestions for '{query}':\n" + "\n".join(lines)

    # ── Spellcheck ────────────────────────────────────────────────────────

    async def check_brave_spelling(query: str, country: str = "", lang: str = "") -> str:
        """Spelling correction of a query."""
        if not (query or "").strip():
            return "Error: query is required."
        params: dict[str, Any] = {"q": query[:400]}
        if country:
            params["country"] = country.strip().upper()
        if lang:
            params["lang"] = lang.strip().lower()
        data, err = await _call("get", "/spellcheck/search", "check spelling", params=params)
        if err:
            return err
        corrections = [r.get("query") for r in data.get("results") or [] if r.get("query")]
        if not corrections or corrections == [query]:
            return f"'{query}' needs no correction."
        return f"Corrected query for '{query}': " + "; ".join(corrections)

    # ── LLM context (grounding) ───────────────────────────────────────────

    async def get_brave_llm_context(
        query: str,
        count: int = 20,
        country: str = "",
        search_lang: str = "",
        maximum_number_of_urls: int = 10,
        maximum_number_of_tokens: int = 8192,
        maximum_number_of_tokens_per_url: int = 4096,
        freshness: str = "",
        goggles: str = "",
    ) -> str:
        """Pre-extracted page content for grounding an answer (RAG)."""
        if not (query or "").strip():
            return "Error: query is required."
        invalid = _validate_common(freshness, "")
        if invalid:
            return invalid
        params: dict[str, Any] = {
            "q": query[:400],
            "count": _clamp(count, 1, 50, 20),
            "maximum_number_of_urls": _clamp(maximum_number_of_urls, 1, 50, 10),
            "maximum_number_of_tokens": _clamp(maximum_number_of_tokens, 1024, 32768, 8192),
            "maximum_number_of_tokens_per_url": _clamp(maximum_number_of_tokens_per_url, 512, 8192, 4096),
            **_locale_params(country, search_lang, ""),
        }
        if freshness:
            params["freshness"] = freshness
        if goggles:
            params["goggles"] = goggles
        data, err = await _call("get", "/llm/context", "get LLM context", params=params)
        if err:
            return err
        grounding = data.get("grounding") or {}
        items = list(grounding.get("generic") or []) + list(grounding.get("map") or [])
        if grounding.get("poi"):
            items.append(grounding["poi"])
        if not items:
            return f"No context found for '{query}'."
        blocks = []
        for item in items:
            title = item.get("title") or item.get("name") or "Untitled"
            snippets = "\n".join(f"> {_clean(s)}" for s in item.get("snippets") or [])
            blocks.append(f"### {title}\nURL: {item.get('url', '')}\n{snippets}".rstrip())
        return f"Brave LLM context for '{query}' ({len(items)} source(s)):\n\n" + "\n\n".join(blocks)

    # ── Answers (AI, cited) ───────────────────────────────────────────────

    async def ask_brave_answer(question: str, country: str = "", language: str = "") -> str:
        """A cited answer Brave's model writes from live search results."""
        if not (question or "").strip():
            return "Error: question is required."
        body: dict[str, Any] = {
            "model": "brave",
            "stream": False,
            "messages": [{"role": "user", "content": question[:4000]}],
        }
        if country:
            body["country"] = country.strip().lower()
        if language:
            body["language"] = language.strip().lower()
        data, err = await _call("post", "/chat/completions", "answer the question", json=body)
        if err:
            return err
        choices = data.get("choices") or []
        content = ((choices[0] or {}).get("message") or {}).get("content") if choices else ""
        if not content:
            return f"{_PROVIDER} returned no answer for '{question}'."
        return f"Brave answer:\n\n{str(content).strip()}"

    # ── Local POIs ────────────────────────────────────────────────────────

    async def get_brave_places(ids: list[str], search_lang: str = "", ui_lang: str = "") -> str:
        """Details of places (address, rating, contact) by the POI ids a web search answered."""
        wanted = [str(i).strip() for i in (ids or []) if str(i).strip()][:20]
        if not wanted:
            return "Error: ids is required: the POI ids a search_brave_web 'Places' section lists."
        params: list[tuple[str, str]] = [("ids", i) for i in wanted]
        for key, value in _locale_params("", search_lang, ui_lang).items():
            params.append((key, value))
        data, err = await _call("get", "/local/pois", "read places", params=params)
        if err:
            return err
        results = data.get("results") or []
        if not results:
            return "No place found for these ids (POI ids expire after about 8 hours)."
        descriptions, _ = await _call("get", "/local/descriptions", "read place descriptions", params=params)
        by_id = {
            d.get("id"): _clean(d.get("description"))
            for d in (descriptions.get("results") or [])
            if d.get("description")
        }
        blocks = []
        for poi in results:
            block = _format_poi(poi)
            if by_id.get(poi.get("id")):
                block += f"\n  About: {by_id[poi['id']]}"
            blocks.append(block)
        return f"Brave places ({len(results)}):\n\n" + "\n\n".join(blocks)

    # ── Register ──────────────────────────────────────────────────────────

    handlers: dict[str, Callable[..., Awaitable[str]]] = {
        "search_brave_web": search_brave_web,
        "search_brave_news": search_brave_news,
        "search_brave_images": search_brave_images,
        "search_brave_videos": search_brave_videos,
        "get_brave_suggestions": get_brave_suggestions,
        "check_brave_spelling": check_brave_spelling,
        "get_brave_llm_context": get_brave_llm_context,
        "ask_brave_answer": ask_brave_answer,
        "get_brave_places": get_brave_places,
    }
    for name, handler in handlers.items():
        executor.register(name, handler)

    locale_props: dict[str, _Json] = {
        "country": {
            "type": "string",
            "description": "Two-letter country code the results come from (e.g. 'US', 'FR', 'JP'). Optional.",
        },
        "search_lang": {
            "type": "string",
            "description": "Language of the results (e.g. 'en', 'fr', 'de'). Optional.",
        },
        "ui_lang": {
            "type": "string",
            "description": "Language of the response metadata, e.g. 'en-US'. Optional.",
        },
    }
    freshness_prop = {
        "type": "string",
        "description": (
            "Page age: 'pd' (24 h), 'pw' (7 days), 'pm' (31 days), 'py' (365 days), "
            "or a range 'YYYY-MM-DDtoYYYY-MM-DD'. Optional."
        ),
    }
    offset_prop = {
        "type": "integer",
        "description": "Zero-based page index (0 to 9): 1 for the second page of `count` results.",
        "default": 0,
    }
    spellcheck_prop = {"type": "boolean", "description": "Correct the query's spelling first.", "default": True}
    goggles_prop = {
        "type": "string",
        "description": "A Goggle URL or definition to re-rank results with custom rules (advanced). Optional.",
    }

    def _query_prop(example: str) -> _Json:
        return {
            "type": "string",
            "description": (
                f'Search query; search operators pass through: site:, filetype:, "exact phrase", -exclude, '
                f"lang:, loc:, intitle:, inbody: (e.g. {example})."
            ),
        }

    return [
        ToolDefinition(
            name="search_brave_web",
            aliases=("search_google_web",),
            description=(
                "Search the web with Brave Search (independent index). Returns titles, source, age, snippets, "
                "extra snippets and URLs, plus news, videos, FAQ and places when relevant. Use it for any internet "
                "lookup: articles, documentation, vendors, advisories, companies, people, current events. Restrict "
                'with operators in the query (site:github.com, filetype:pdf, "exact phrase", -term) and recency '
                "with freshness ('pd', 'pw', 'pm', 'py' or a date range)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": _query_prop(
                        "'open source threat intelligence platform', 'CVE-2024-3400 site:paloaltonetworks.com'"
                    ),
                    "count": {"type": "integer", "description": "Web results per page (1 to 20).", "default": 10},
                    "offset": offset_prop,
                    **locale_props,
                    "safesearch": {
                        "type": "string",
                        "enum": list(_SAFESEARCH),
                        "description": "Adult content filter.",
                        "default": "moderate",
                    },
                    "freshness": freshness_prop,
                    "result_filter": {
                        "type": "string",
                        "description": (
                            "Comma-separated result types to keep: " + ", ".join(_WEB_RESULT_FILTERS) + ". Optional."
                        ),
                    },
                    "goggles": goggles_prop,
                    "extra_snippets": {
                        "type": "boolean",
                        "description": "Up to 5 more excerpts per result (plan dependent).",
                        "default": True,
                    },
                    "spellcheck": spellcheck_prop,
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
            read_only=True,
        ),
        ToolDefinition(
            name="search_brave_news",
            description=(
                "Search recent news articles with Brave News Search: title, outlet, age, snippet and URL, breaking "
                "news flagged. Use it for current events, incidents, breaches, vendor announcements; narrow with "
                "freshness ('pd' for the last 24 h)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": _query_prop("'ransomware attack hospital'"),
                    "count": {"type": "integer", "description": "Articles per page (1 to 50).", "default": 20},
                    "offset": offset_prop,
                    **locale_props,
                    "safesearch": {
                        "type": "string",
                        "enum": list(_SAFESEARCH),
                        "description": "Adult content filter.",
                        "default": "strict",
                    },
                    "freshness": freshness_prop,
                    "goggles": goggles_prop,
                    "extra_snippets": {
                        "type": "boolean",
                        "description": "More excerpts per article (plan dependent).",
                        "default": False,
                    },
                    "spellcheck": spellcheck_prop,
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
            read_only=True,
        ),
        ToolDefinition(
            name="search_brave_images",
            aliases=("search_google_images",),
            description=(
                "Search images with Brave Image Search. Returns inline thumbnail previews, the full-size image URL, "
                "dimensions and the source page. Use it to find logos, portraits, product shots, diagrams, "
                "infographics or screenshots."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Image search query (e.g. 'Filigran logo')."},
                    "count": {"type": "integer", "description": "Images to return (1 to 200).", "default": 20},
                    "country": locale_props["country"],
                    "search_lang": locale_props["search_lang"],
                    "safesearch": {
                        "type": "string",
                        "enum": ["off", "strict"],
                        "description": "Adult content filter.",
                        "default": "strict",
                    },
                    "spellcheck": spellcheck_prop,
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
            read_only=True,
        ),
        ToolDefinition(
            name="search_brave_videos",
            description=(
                "Search videos with Brave Video Search: thumbnail, title, creator, duration, views, age and URL. "
                "Use it for talks, demos, tutorials and conference recordings."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": _query_prop("'OpenCTI demo'"),
                    "count": {"type": "integer", "description": "Videos per page (1 to 50).", "default": 20},
                    "offset": offset_prop,
                    **locale_props,
                    "safesearch": {
                        "type": "string",
                        "enum": list(_SAFESEARCH),
                        "description": "Adult content filter.",
                        "default": "moderate",
                    },
                    "freshness": freshness_prop,
                    "spellcheck": spellcheck_prop,
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
            read_only=True,
        ),
        ToolDefinition(
            name="get_brave_suggestions",
            description=(
                "Autocomplete suggestions for a partial query from Brave Search (Suggest plan). Use it to expand "
                "or disambiguate a search before running it; rich=true marks the entities."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The partial query."},
                    "count": {"type": "integer", "description": "Suggestions to return (1 to 20).", "default": 5},
                    "country": locale_props["country"],
                    "rich": {
                        "type": "boolean",
                        "description": "Mark entities with a title and description.",
                        "default": False,
                    },
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
            read_only=True,
        ),
        ToolDefinition(
            name="check_brave_spelling",
            description="Spelling correction of a search query from Brave Search (Spellcheck plan).",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The query to correct."},
                    "country": locale_props["country"],
                    "lang": {"type": "string", "description": "Language of the query (e.g. 'en'). Optional."},
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
            read_only=True,
        ),
        ToolDefinition(
            name="get_brave_llm_context",
            description=(
                "Pre-extracted web page content for grounding an answer: the most relevant text chunks of up to "
                "50 pages, with their URLs, in one call (Brave LLM Context, AI plans). Prefer it to search plus "
                "page fetches when the answer must quote or cite the web."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": _query_prop("'MITRE ATT&CK T1059 detection guidance'"),
                    "count": {"type": "integer", "description": "Search results considered (1 to 50).", "default": 20},
                    "country": locale_props["country"],
                    "search_lang": locale_props["search_lang"],
                    "maximum_number_of_urls": {
                        "type": "integer",
                        "description": "Distinct URLs in the context (1 to 50).",
                        "default": 10,
                    },
                    "maximum_number_of_tokens": {
                        "type": "integer",
                        "description": "Approximate size of the whole context in tokens (1024 to 32768).",
                        "default": 8192,
                    },
                    "maximum_number_of_tokens_per_url": {
                        "type": "integer",
                        "description": "Tokens per URL (512 to 8192).",
                        "default": 4096,
                    },
                    "freshness": freshness_prop,
                    "goggles": goggles_prop,
                },
                "required": ["query"],
            },
            source=_SOURCE,
            priority=0,
            read_only=True,
        ),
        ToolDefinition(
            name="ask_brave_answer",
            description=(
                "A cited answer written by Brave's own model from live search results (Brave Answers, AI plans). "
                "Use it for a quick factual answer with sources; use get_brave_llm_context to reason over the pages "
                "yourself."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question to answer."},
                    "country": {"type": "string", "description": "Two-letter country code (e.g. 'us'). Optional."},
                    "language": {"type": "string", "description": "Answer language (e.g. 'en'). Optional."},
                },
                "required": ["question"],
            },
            source=_SOURCE,
            priority=0,
            read_only=True,
        ),
        ToolDefinition(
            name="get_brave_places",
            description=(
                "Details of places (address, rating, phone, categories, description) by the POI ids that a "
                "search_brave_web 'Places' section lists (up to 20 ids; ids expire after about 8 hours)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ids": {"type": "array", "items": {"type": "string"}, "description": "POI ids (1 to 20)."},
                    "search_lang": locale_props["search_lang"],
                    "ui_lang": locale_props["ui_lang"],
                },
                "required": ["ids"],
            },
            source=_SOURCE,
            priority=0,
            read_only=True,
        ),
    ]
