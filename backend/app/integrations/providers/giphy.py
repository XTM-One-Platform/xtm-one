# SPDX-License-Identifier: Apache-2.0

"""GIPHY integration: GIFs and stickers an agent can drop into a conversation.

API docs: https://developers.giphy.com/docs/api/endpoint/ (objects:
https://developers.giphy.com/docs/api/schema/)
Auth: one API key sent as the ``api_key`` query parameter, the only form GIPHY
accepts. It always rides in ``params``, never in a URL string, so it never
lands in a URL the platform logs or answers, and ``safe_request`` redacts a
provider error that would echo it.
Base URL: https://api.giphy.com/v1

Four read-only tools: search, the best single GIF for a phrase (translate),
a random one for a tag, and the trending ones. Each GIF is answered with a
Markdown image line on its ``downsized`` rendition (under 2 MB, the size the
channels accept), so the agent copies that line into its answer: the web chat
draws it, and :data:`MEDIA_HOSTS` lets the external channels (Teams, Slack,
Telegram, Discord, Google Chat, WhatsApp) send it as an image
(``channels.base.resolve_channel_images``).

Every call sends a content rating, ``g`` by default and never above ``pg-13``:
without one GIPHY answers every rating, ``r`` included.
"""

from __future__ import annotations

import re
from typing import Any

from app.core.egress import egress_client
from app.integrations.http import TRANSIENT_RETRY_STATUSES, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "GIPHY"
_SOURCE = "integration:giphy"
BASE_URL = "https://api.giphy.com/v1"
#: The query parameter that carries the key (GIPHY has no header form).
KEY_PARAM = "api_key"
_TIMEOUT = 20

#: The hosts GIPHY serves its media from (``media0.giphy.com`` to
#: ``media4.giphy.com``, ``i.giphy.com``): a host or any of its subdomains.
#: An external channel sends a Markdown image at one of them as an image.
MEDIA_HOSTS: tuple[str, ...] = ("giphy.com",)

#: The ratings an agent may ask for, most restrictive first; ``r`` is never sent.
RATINGS: tuple[str, ...] = ("g", "pg", "pg-13")
DEFAULT_RATING = "g"
#: GIPHY refuses a longer ``q``.
MAX_QUERY_CHARS = 50
MAX_RESULTS = 25
MAX_OFFSET = 4999
DEFAULT_RESULTS = 5

#: Renditions tried in order for the image shown: ``downsized`` is under 2 MB.
_RENDITIONS: tuple[str, ...] = ("downsized", "downsized_medium", "fixed_height", "original")
_ALT_MAX_CHARS = 120
_ALT_UNSAFE_RE = re.compile(r"[\[\]\r\n\t]+")
_SPACES_RE = re.compile(r"\s+")


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _rating(value: Any) -> str | None:
    """The rating to send, ``None`` when *value* is not one of :data:`RATINGS`."""
    rating = str(value or DEFAULT_RATING).strip().lower()
    return rating if rating in RATINGS else None


def _rating_error() -> str:
    return f"Error: rating must be one of {', '.join(RATINGS)}."


def _kind(stickers: Any) -> str:
    return "stickers" if stickers is True or str(stickers).strip().lower() == "true" else "gifs"


def _alt(text: Any) -> str:
    """Alt text safe inside ``![...]``: no bracket, no line break, bounded."""
    cleaned = _SPACES_RE.sub(" ", _ALT_UNSAFE_RE.sub(" ", str(text or ""))).strip()
    return cleaned[:_ALT_MAX_CHARS].rstrip()


def pick_rendition(gif: dict[str, Any]) -> dict[str, Any] | None:
    """The first rendition of :data:`_RENDITIONS` with an https URL, or ``None``."""
    images = gif.get("images")
    if not isinstance(images, dict):
        return None
    for name in _RENDITIONS:
        rendition = images.get(name)
        if isinstance(rendition, dict) and str(rendition.get("url") or "").startswith("https://"):
            return rendition
    return None


def format_gif(gif: dict[str, Any], index: int | None = None) -> str | None:
    """One GIF as readable lines with its Markdown image line, ``None`` without an image."""
    rendition = pick_rendition(gif)
    if rendition is None:
        return None
    title = _alt(gif.get("title")) or "Untitled GIF"
    alt = _alt(gif.get("alt_text")) or title
    head = f"{index}. " if index is not None else ""
    meta = [f"id {gif['id']}"] if gif.get("id") else []
    if gif.get("rating"):
        meta.append(f"rated {gif['rating']}")
    lines = [f"{head}**{title}**" + (f" ({', '.join(meta)})" if meta else "")]
    lines.append(f"   ![{alt}]({rendition['url']})")
    details = []
    if rendition.get("width") and rendition.get("height"):
        details.append(f"{rendition['width']}x{rendition['height']}")
    size = str(rendition.get("size") or "")
    if size.isdigit():
        details.append(f"{int(size) // 1024} KB")
    if gif.get("url"):
        details.append(f"page {gif['url']}")
    if details:
        lines.append(f"   {' - '.join(details)}")
    return "\n".join(lines)


_USAGE = (
    "To show a GIF, copy its Markdown image line into your answer as is: the web chat draws it and "
    "the external channels (Teams, Slack...) send it as an image. Powered by GIPHY."
)


def format_gifs(gifs: list[Any], what: str) -> str:
    blocks = [
        block
        for index, gif in enumerate((g for g in gifs if isinstance(g, dict)), start=1)
        if (block := format_gif(gif, index)) is not None
    ]
    if not blocks:
        return f"No GIF found for {what}."
    return f"{len(blocks)} GIF(s) for {what}:\n\n" + "\n\n".join(blocks) + f"\n\n{_USAGE}"


def build_tools(token: str, executor: CallableToolExecutor, **_kwargs: Any) -> list[ToolDefinition]:
    """GIPHY tools; ``token`` is the API key."""
    api_key = (token or "").strip()
    if not api_key:
        return []

    async def _get(path: str, action: str, params: dict[str, Any]) -> tuple[Any, str | None]:
        query = {**params, KEY_PARAM: api_key}
        async with egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{BASE_URL}{path}",
                _PROVIDER,
                action,
                params=query,
                headers={"Accept": "application/json"},
                retry_statuses=TRANSIENT_RETRY_STATUSES,  # every call is a read
            )
        if err:
            return None, err
        if resp is None:
            return None, f"Error: {_PROVIDER} returned no response while trying to {action}."
        try:
            body = resp.json()
        except ValueError:
            return None, f"Error: {_PROVIDER} returned a non-JSON response while trying to {action}."
        if not isinstance(body, dict):
            return None, f"Error: {_PROVIDER} returned an unexpected response while trying to {action}."
        return body.get("data"), None

    async def search_giphy_gifs(
        query: str,
        limit: int = DEFAULT_RESULTS,
        offset: int = 0,
        rating: str = DEFAULT_RATING,
        lang: str = "",
        stickers: bool = False,
    ) -> str:
        """Search GIFs or stickers by words."""
        q = (query or "").strip()
        if not q:
            return "Error: query is required."
        if len(q) > MAX_QUERY_CHARS:
            return f"Error: query must be at most {MAX_QUERY_CHARS} characters (GIPHY refuses longer ones)."
        sent_rating = _rating(rating)
        if sent_rating is None:
            return _rating_error()
        params: dict[str, Any] = {
            "q": q,
            "limit": _clamp(limit, 1, MAX_RESULTS, DEFAULT_RESULTS),
            "offset": _clamp(offset, 0, MAX_OFFSET, 0),
            "rating": sent_rating,
        }
        if (lang or "").strip():
            params["lang"] = lang.strip().lower()[:2]
        kind = _kind(stickers)
        data, err = await _get(f"/{kind}/search", f"search {kind} for '{q}'", params)
        if err:
            return err
        return format_gifs(data if isinstance(data, list) else [], f"'{q}'")

    async def get_giphy_reaction_gif(phrase: str, rating: str = DEFAULT_RATING, stickers: bool = False) -> str:
        """The single GIF GIPHY finds best for a phrase (translate)."""
        s = (phrase or "").strip()
        if not s:
            return "Error: phrase is required."
        sent_rating = _rating(rating)
        if sent_rating is None:
            return _rating_error()
        kind = _kind(stickers)
        data, err = await _get(f"/{kind}/translate", f"find a GIF for '{s}'", {"s": s, "rating": sent_rating})
        if err:
            return err
        return format_gifs([data] if isinstance(data, dict) and data else [], f"'{s}'")

    async def get_random_giphy_gif(tag: str = "", rating: str = DEFAULT_RATING, stickers: bool = False) -> str:
        """One random GIF, for a tag when one is given."""
        sent_rating = _rating(rating)
        if sent_rating is None:
            return _rating_error()
        params: dict[str, Any] = {"rating": sent_rating}
        t = (tag or "").strip()
        if t:
            params["tag"] = t
        kind = _kind(stickers)
        what = f"tag '{t}'" if t else "a random pick"
        data, err = await _get(f"/{kind}/random", f"get a random GIF for {what}", params)
        if err:
            return err
        return format_gifs([data] if isinstance(data, dict) and data else [], what)

    async def get_trending_giphy_gifs(
        limit: int = DEFAULT_RESULTS, rating: str = DEFAULT_RATING, stickers: bool = False
    ) -> str:
        """The GIFs trending on GIPHY now."""
        sent_rating = _rating(rating)
        if sent_rating is None:
            return _rating_error()
        kind = _kind(stickers)
        params = {"limit": _clamp(limit, 1, MAX_RESULTS, DEFAULT_RESULTS), "rating": sent_rating}
        data, err = await _get(f"/{kind}/trending", f"list trending {kind}", params)
        if err:
            return err
        return format_gifs(data if isinstance(data, list) else [], "trending")

    executor.register("search_giphy_gifs", search_giphy_gifs)
    executor.register("get_giphy_reaction_gif", get_giphy_reaction_gif)
    executor.register("get_random_giphy_gif", get_random_giphy_gif)
    executor.register("get_trending_giphy_gifs", get_trending_giphy_gifs)

    rating_prop = {
        "type": "string",
        "enum": list(RATINGS),
        "default": DEFAULT_RATING,
        "description": "Content rating ceiling: g (all audiences, the default), pg or pg-13.",
    }
    stickers_prop = {
        "type": "boolean",
        "default": False,
        "description": "True for stickers (transparent background) instead of GIFs.",
    }
    limit_prop = {
        "type": "integer",
        "default": DEFAULT_RESULTS,
        "description": f"GIFs to return (1 to {MAX_RESULTS}).",
    }
    answer_note = (
        " Each GIF comes with a Markdown image line to copy into the answer as is; it shows in the web chat "
        "and as an image in Teams, Slack and the other channels."
    )
    return [
        ToolDefinition(
            name="search_giphy_gifs",
            description=(
                "Search GIPHY for GIFs or stickers matching words ('celebrate', 'facepalm', 'deploy friday'): "
                "title, rating, size and page of each." + answer_note
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": f"Words to search, at most {MAX_QUERY_CHARS} characters; '@user' searches one GIPHY channel.",
                    },
                    "limit": limit_prop,
                    "offset": {"type": "integer", "default": 0, "description": "Results to skip (paging)."},
                    "rating": rating_prop,
                    "lang": {"type": "string", "description": "2-letter language of the query (en, fr...)."},
                    "stickers": stickers_prop,
                },
                "required": ["query"],
            },
            source=_SOURCE,
            read_only=True,
        ),
        ToolDefinition(
            name="get_giphy_reaction_gif",
            description=(
                "The single GIF GIPHY finds best for a phrase or a feeling ('well done', 'mind blown'): the "
                "quickest way to react with one GIF." + answer_note
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "phrase": {"type": "string", "description": "The phrase or feeling to illustrate."},
                    "rating": rating_prop,
                    "stickers": stickers_prop,
                },
                "required": ["phrase"],
            },
            source=_SOURCE,
            read_only=True,
        ),
        ToolDefinition(
            name="get_random_giphy_gif",
            description="One random GIF from GIPHY, for a tag when one is given ('cat', 'coffee')." + answer_note,
            input_schema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Tag to draw from; empty for any GIF."},
                    "rating": rating_prop,
                    "stickers": stickers_prop,
                },
            },
            source=_SOURCE,
            read_only=True,
        ),
        ToolDefinition(
            name="get_trending_giphy_gifs",
            description="The GIFs trending on GIPHY right now." + answer_note,
            input_schema={
                "type": "object",
                "properties": {"limit": limit_prop, "rating": rating_prop, "stickers": stickers_prop},
            },
            source=_SOURCE,
            read_only=True,
        ),
    ]
