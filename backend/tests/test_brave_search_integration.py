"""Brave Search provider: every tool against a fake Brave API.

The tools run for real through ``httpx.MockTransport``: ``safe_request`` and
``egress_client`` are the production code paths. The fake answers each
documented endpoint of https://api.search.brave.com/res/v1/ with the response
shape of the Brave Search API reference and records the requests, so the
tests pin both the parameters sent and the markdown answered.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.integrations.providers import brave_search
from app.tools.base import CallableToolExecutor, ToolCall, ToolNameResolver, ToolResult, declared_aliases

KEY = "BSA-test-key"
QUERY = "open source threat intelligence platform"

WEB = {
    "type": "search",
    "query": {"original": QUERY, "more_results_available": True},
    "web": {
        "results": [
            {
                "title": "<strong>OpenCTI</strong>",
                "url": "https://filigran.io/opencti",
                "description": "An open source <strong>threat intelligence</strong> platform.",
                "age": "2 days ago",
                "meta_url": {"hostname": "filigran.io"},
                "extra_snippets": ["Knowledge graph of threats.", "STIX 2.1 native."],
            }
        ]
    },
    "news": {
        "results": [
            {
                "title": "New release",
                "url": "https://news.example/a",
                "age": "1 hour ago",
                "meta_url": {"hostname": "news.example"},
                "breaking": True,
            }
        ]
    },
    "locations": {
        "results": [
            {
                "id": "poi-1",
                "title": "Filigran office",
                "postal_address": {"displayAddress": "Paris, France"},
            }
        ]
    },
}
NEWS = {
    "type": "news",
    "query": {"original": QUERY},
    "results": [
        {
            "title": "Threat intel platform raises",
            "url": "https://news.example/b",
            "description": "Funding news.",
            "age": "3 hours ago",
            "meta_url": {"hostname": "news.example"},
        }
    ],
}
IMAGES = {
    "type": "images",
    "results": [
        {
            "title": "OpenCTI logo",
            "url": "https://filigran.io/page",
            "source": "filigran.io",
            "thumbnail": {"src": "https://imgs.search.brave.com/thumb.png"},
            "properties": {"url": "https://filigran.io/logo.png", "width": 512, "height": 256},
        }
    ],
}
VIDEOS = {
    "type": "videos",
    "query": {"original": QUERY},
    "results": [
        {
            "title": "OpenCTI demo",
            "url": "https://video.example/v",
            "age": "1 month ago",
            "thumbnail": {"src": "https://imgs.search.brave.com/v.png"},
            "video": {"duration": "12:34", "views": 1500, "creator": "Filigran"},
        }
    ],
}
SUGGEST = {
    "type": "suggest",
    "results": [
        {"query": "opencti docs"},
        {"query": "opencti", "is_entity": True, "title": "OpenCTI", "description": "CTI platform"},
    ],
}
SPELL = {"type": "spellcheck", "query": {"original": "opnecti"}, "results": [{"query": "opencti"}]}
LLM = {
    "grounding": {
        "generic": [
            {"url": "https://filigran.io/opencti", "title": "OpenCTI", "snippets": ["Chunk one.", "Chunk two."]}
        ],
        "map": [],
    },
    "sources": {},
}
ANSWER = {"choices": [{"message": {"role": "assistant", "content": "OpenCTI is a CTI platform [1]."}}]}
POIS = {
    "results": [
        {
            "id": "poi-1",
            "title": "Filigran office",
            "postal_address": {"displayAddress": "Paris, France"},
            "rating": {"ratingValue": 4.8, "reviewCount": 12},
            "contact": {"telephone": "+33 1 00 00 00 00"},
            "url": "https://filigran.io",
        }
    ]
}
DESCRIPTIONS = {"results": [{"id": "poi-1", "description": "Headquarters of Filigran."}]}

ROUTES: dict[str, dict[str, Any]] = {
    "/res/v1/web/search": WEB,
    "/res/v1/news/search": NEWS,
    "/res/v1/images/search": IMAGES,
    "/res/v1/videos/search": VIDEOS,
    "/res/v1/suggest/search": SUGGEST,
    "/res/v1/spellcheck/search": SPELL,
    "/res/v1/llm/context": LLM,
    "/res/v1/chat/completions": ANSWER,
    "/res/v1/local/pois": POIS,
    "/res/v1/local/descriptions": DESCRIPTIONS,
}


class FakeBrave:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status: int = 200
        self.headers: dict[str, str] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get(brave_search.TOKEN_HEADER) != KEY:
            return httpx.Response(
                401,
                json={"type": "ErrorResponse", "error": {"code": "SUBSCRIPTION_TOKEN_INVALID", "status": 401}},
            )
        if self.status != 200:
            return httpx.Response(
                self.status,
                headers=self.headers,
                json={
                    "type": "ErrorResponse",
                    "error": {"detail": "Request rate limit exceeded", "status": self.status},
                },
            )
        body = ROUTES.get(request.url.path)
        if body is None:
            return httpx.Response(404, json={"type": "ErrorResponse", "error": {"detail": "not found"}})
        return httpx.Response(200, json=body)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


@pytest.fixture
def brave(monkeypatch: pytest.MonkeyPatch) -> FakeBrave:
    fake = FakeBrave()
    real_client = httpx.AsyncClient

    class _Client(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(fake)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return fake


async def _call(name: str, key: str = KEY, **arguments: Any) -> ToolResult:
    executor = CallableToolExecutor()
    brave_search.build_tools(key, executor)
    return await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))


def test_one_tool_per_endpoint_all_read_only() -> None:
    definitions = brave_search.build_tools(KEY, CallableToolExecutor())
    names = {d.name for d in definitions}
    assert names == {
        "search_brave_web",
        "search_brave_news",
        "search_brave_images",
        "search_brave_videos",
        "get_brave_suggestions",
        "check_brave_spelling",
        "get_brave_llm_context",
        "ask_brave_answer",
        "get_brave_places",
    }
    assert all(d.read_only for d in definitions)
    assert all(d.source == "integration:brave_search" for d in definitions)


def test_google_tool_names_resolve_to_brave() -> None:
    """A persona naming the Google tools reaches Brave once Google is gone."""
    definitions = brave_search.build_tools(KEY, CallableToolExecutor())
    resolver = ToolNameResolver((d.name for d in definitions), aliases=declared_aliases(definitions))
    assert resolver.canonical("search_google_web") == "search_brave_web"
    assert resolver.canonical("search_google_images") == "search_brave_images"


def test_exact_google_name_wins_when_google_is_bound() -> None:
    definitions = brave_search.build_tools(KEY, CallableToolExecutor())
    names = [d.name for d in definitions] + ["search_google_web"]
    resolver = ToolNameResolver(names, aliases=declared_aliases(definitions))
    assert resolver.canonical("search_google_web") == "search_google_web"


@pytest.mark.asyncio
async def test_web_search_formats_results_and_sends_params(brave: FakeBrave) -> None:
    result = await _call(
        "search_brave_web",
        query=QUERY,
        count=50,
        offset=1,
        country="fr",
        search_lang="EN",
        freshness="2026-01-01to2026-02-01",
        result_filter="web,news,locations",
    )

    assert not result.is_error
    params = brave.last.url.params
    assert brave.last.url.path == "/res/v1/web/search"
    assert brave.last.headers[brave_search.TOKEN_HEADER] == KEY
    assert params["q"] == QUERY
    assert params["count"] == "20"  # clamped to the documented maximum
    assert params["offset"] == "1"
    assert params["country"] == "FR"
    assert params["search_lang"] == "en"
    assert params["freshness"] == "2026-01-01to2026-02-01"
    assert params["result_filter"] == "web,news,locations"
    assert params["extra_snippets"] == "true"
    text = result.content
    assert "**OpenCTI**" in text
    assert "<strong>" not in text
    assert "Source: filigran.io | 2 days ago" in text
    assert "  - STIX 2.1 native." in text
    assert "URL: https://filigran.io/opencti" in text
    assert "breaking" in text
    assert "POI id: poi-1" in text
    assert "offset=2" in text


@pytest.mark.asyncio
async def test_web_search_refuses_bad_freshness_and_filter_before_any_call(brave: FakeBrave) -> None:
    bad_freshness = await _call("search_brave_web", query=QUERY, freshness="last week")
    bad_filter = await _call("search_brave_web", query=QUERY, result_filter="web,maps")

    assert bad_freshness.content.startswith("Error: freshness must be")
    assert "unknown result_filter" in bad_filter.content
    assert brave.requests == []


@pytest.mark.asyncio
async def test_news_search(brave: FakeBrave) -> None:
    result = await _call("search_brave_news", query=QUERY, freshness="pd")

    assert brave.last.url.path == "/res/v1/news/search"
    assert brave.last.url.params["freshness"] == "pd"
    assert "**Threat intel platform raises**" in result.content
    assert "news.example | 3 hours ago" in result.content


@pytest.mark.asyncio
async def test_image_search_renders_thumbnails(brave: FakeBrave) -> None:
    result = await _call("search_brave_images", query="OpenCTI logo", count=500)

    assert brave.last.url.params["count"] == "200"
    assert "![OpenCTI logo](https://imgs.search.brave.com/thumb.png)" in result.content
    assert "Dimensions: 512x256" in result.content
    assert "Full-size image: https://filigran.io/logo.png" in result.content


@pytest.mark.asyncio
async def test_video_search(brave: FakeBrave) -> None:
    result = await _call("search_brave_videos", query="OpenCTI demo")

    assert brave.last.url.path == "/res/v1/videos/search"
    assert "![OpenCTI demo](https://imgs.search.brave.com/v.png)" in result.content
    assert "Filigran | 1 month ago | duration 12:34 | 1500 views" in result.content


@pytest.mark.asyncio
async def test_suggest_marks_entities(brave: FakeBrave) -> None:
    result = await _call("get_brave_suggestions", query="openc", rich=True)

    assert brave.last.url.params["rich"] == "true"
    assert "- opencti docs" in result.content
    assert "(entity: OpenCTI, CTI platform)" in result.content


@pytest.mark.asyncio
async def test_spellcheck(brave: FakeBrave) -> None:
    result = await _call("check_brave_spelling", query="opnecti")

    assert result.content == "Corrected query for 'opnecti': opencti"


@pytest.mark.asyncio
async def test_llm_context_quotes_chunks_with_urls(brave: FakeBrave) -> None:
    result = await _call("get_brave_llm_context", query=QUERY, maximum_number_of_tokens=100)

    assert brave.last.url.path == "/res/v1/llm/context"
    assert brave.last.url.params["maximum_number_of_tokens"] == "1024"
    assert "### OpenCTI" in result.content
    assert "URL: https://filigran.io/opencti" in result.content
    assert "> Chunk two." in result.content


@pytest.mark.asyncio
async def test_answer_posts_a_chat_completion(brave: FakeBrave) -> None:
    result = await _call("ask_brave_answer", question="What is OpenCTI?")

    assert brave.last.method == "POST"
    assert brave.last.url.path == "/res/v1/chat/completions"
    assert b'"stream":false' in brave.last.content.replace(b" ", b"")
    assert "OpenCTI is a CTI platform [1]." in result.content


@pytest.mark.asyncio
async def test_local_pois_joins_descriptions(brave: FakeBrave) -> None:
    result = await _call("get_brave_places", ids=["poi-1"])

    paths = [r.url.path for r in brave.requests]
    assert paths == ["/res/v1/local/pois", "/res/v1/local/descriptions"]
    assert brave.requests[0].url.params.get_list("ids") == ["poi-1"]
    assert "Rating: 4.8 (12 reviews)" in result.content
    assert "About: Headquarters of Filigran." in result.content


@pytest.mark.asyncio
async def test_invalid_key_is_one_line(brave: FakeBrave) -> None:
    result = await _call("search_brave_web", key="wrong", query=QUERY)

    assert result.content.startswith("Error: Brave Search rejected the API key")
    assert "\n" not in result.content
    assert "Traceback" not in result.content


@pytest.mark.asyncio
async def test_endpoint_outside_the_plan_is_named(brave: FakeBrave) -> None:
    brave.status = 403
    result = await _call("get_brave_llm_context", query=QUERY)

    assert "subscription of this API key does not include this endpoint" in result.content


@pytest.mark.asyncio
async def test_monthly_quota_used_up_names_the_reset(brave: FakeBrave) -> None:
    brave.status = 402  # any non-429 failure path is a one-liner too
    result = await _call("search_brave_news", query=QUERY)
    assert result.content.startswith("Error: Brave Search returned HTTP 402")


def test_rate_limit_message_carries_the_wait() -> None:
    resp = httpx.Response(
        429,
        headers={"X-RateLimit-Reset": "1, 7200", "X-RateLimit-Remaining": "0, 0"},
        json={"type": "ErrorResponse", "error": {"detail": "Request rate limit exceeded"}},
    )
    message = brave_search.format_brave_error(resp, "search the web")
    assert message.startswith("Error: Brave Search rate limit reached while trying to search the web.")
    assert "monthly quota" in message and "2 h" in message
    assert "\n" not in message

    burst = httpx.Response(429, headers={"Retry-After": "3"})
    assert brave_search.format_brave_error(burst, "search news").endswith("Retry after 3 s.")


@pytest.mark.asyncio
async def test_missing_key_answers_without_a_call(brave: FakeBrave) -> None:
    result = await _call("search_brave_web", key="  ", query=QUERY)

    assert "has no API key" in result.content
    assert brave.requests == []
