"""GIPHY provider: every tool against a fake GIPHY API.

The tools run for real through ``httpx.MockTransport``: ``safe_request`` and
``egress_client`` are the production code paths. The fake answers the
documented endpoints of https://api.giphy.com/v1 (search, translate, random,
trending; GIF and sticker paths) with the GIF Object shape of the schema
reference (https://developers.giphy.com/docs/api/schema/) and records the
requests, so the tests pin the parameters sent, the key kept out of every
answer, and the Markdown image line the agent copies into its reply.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.integrations.providers import giphy
from app.services.integration_tools import PROVIDER_MEDIA_HOSTS, PROVIDER_TOOLS
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

KEY = "giphy-test-key-0123456789"
DOWNSIZED = "https://media2.giphy.com/media/abc123/giphy-downsized.gif?cid=x&rid=giphy-downsized.gif&ct=g"


def _gif(gif_id: str = "abc123", title: str = "Party [Parrot] GIF", **images: Any) -> dict[str, Any]:
    return {
        "type": "gif",
        "id": gif_id,
        "url": f"https://giphy.com/gifs/party-parrot-{gif_id}",
        "rating": "g",
        "title": title,
        "alt_text": "",
        "images": images
        or {
            "original": {"url": f"https://media2.giphy.com/media/{gif_id}/giphy.gif", "width": "480", "height": "270"},
            "downsized": {"url": DOWNSIZED, "width": "480", "height": "270", "size": "1048576"},
        },
    }


def _listing(*gifs: dict[str, Any]) -> dict[str, Any]:
    return {
        "data": list(gifs),
        "pagination": {"total_count": len(gifs), "count": len(gifs), "offset": 0},
        "meta": {"status": 200, "msg": "OK", "response_id": "r1"},
    }


def _single(gif: dict[str, Any] | list[Any]) -> dict[str, Any]:
    return {"data": gif, "meta": {"status": 200, "msg": "OK", "response_id": "r1"}}


ROUTES: dict[str, dict[str, Any]] = {
    "/v1/gifs/search": _listing(_gif(), _gif("def456", title="Celebrate")),
    "/v1/stickers/search": _listing(_gif("stk1", title="Sticker")),
    "/v1/gifs/translate": _single(_gif("tr1", title="Well done")),
    "/v1/gifs/random": _single(_gif("rnd1", title="Random cat")),
    "/v1/gifs/trending": _listing(_gif("trd1", title="Trending")),
}


class FakeGiphy:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.routes = dict(ROUTES)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.params.get(giphy.KEY_PARAM) != KEY:
            # A gateway that echoes the key it refused: the answer must not.
            sent = request.url.params.get(giphy.KEY_PARAM)
            return httpx.Response(401, json={"message": f"Invalid authentication credentials: {sent}"})
        if self.status != 200:
            return httpx.Response(self.status, json={"meta": {"status": self.status, "msg": "Error"}})
        body = self.routes.get(request.url.path)
        if body is None:
            return httpx.Response(404, json={"meta": {"status": 404, "msg": "Not Found"}})
        return httpx.Response(200, json=body)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGiphy:
    api = FakeGiphy()
    real_client = httpx.AsyncClient

    class _Client(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(api)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return api


async def _call(name: str, key: str = KEY, **arguments: Any) -> ToolResult:
    executor = CallableToolExecutor()
    giphy.build_tools(key, executor)
    return await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))


def test_registered_read_only_tools() -> None:
    definitions = giphy.build_tools(KEY, CallableToolExecutor())
    assert {d.name for d in definitions} == {
        "search_giphy_gifs",
        "get_giphy_reaction_gif",
        "get_random_giphy_gif",
        "get_trending_giphy_gifs",
    }
    assert all(d.read_only for d in definitions)
    assert all(d.source == "integration:giphy" for d in definitions)
    assert PROVIDER_TOOLS["giphy"] is giphy.build_tools
    assert PROVIDER_MEDIA_HOSTS["giphy"] == giphy.MEDIA_HOSTS


def test_no_key_no_tools() -> None:
    assert giphy.build_tools("  ", CallableToolExecutor()) == []


@pytest.mark.asyncio
async def test_search_sends_params_and_answers_markdown_images(fake: FakeGiphy) -> None:
    result = await _call("search_giphy_gifs", query="party", limit=99, offset=-3, lang="FR-fr")

    assert not result.is_error
    request = fake.last
    assert request.url.path == "/v1/gifs/search"
    params = request.url.params
    assert params["q"] == "party"
    assert params["limit"] == str(giphy.MAX_RESULTS)  # clamped
    assert params["offset"] == "0"
    assert params["rating"] == "g"  # always sent: without it GIPHY answers every rating
    assert params["lang"] == "fr"
    assert params[giphy.KEY_PARAM] == KEY
    text = result.content
    # Brackets in a title never break the image line.
    assert f"   ![Party Parrot GIF]({DOWNSIZED})" in text
    assert "1. **Party Parrot GIF** (id abc123, rated g)" in text
    assert "480x270 - 1024 KB - page https://giphy.com/gifs/party-parrot-abc123" in text
    assert "2. **Celebrate**" in text
    assert "Powered by GIPHY" in text
    assert KEY not in text


@pytest.mark.asyncio
async def test_stickers_use_the_sticker_endpoint(fake: FakeGiphy) -> None:
    result = await _call("search_giphy_gifs", query="thumbs up", stickers=True)
    assert not result.is_error
    assert fake.last.url.path == "/v1/stickers/search"


@pytest.mark.asyncio
async def test_reaction_random_and_trending(fake: FakeGiphy) -> None:
    reaction = await _call("get_giphy_reaction_gif", phrase="well done", rating="pg-13")
    assert fake.last.url.path == "/v1/gifs/translate"
    assert fake.last.url.params["s"] == "well done"
    assert fake.last.url.params["rating"] == "pg-13"
    assert "**Well done**" in reaction.content

    random_gif = await _call("get_random_giphy_gif", tag="cat")
    assert fake.last.url.path == "/v1/gifs/random"
    assert fake.last.url.params["tag"] == "cat"
    assert "**Random cat**" in random_gif.content

    no_tag = await _call("get_random_giphy_gif")
    assert "tag" not in fake.last.url.params
    assert not no_tag.is_error

    trending = await _call("get_trending_giphy_gifs", limit=2)
    assert fake.last.url.path == "/v1/gifs/trending"
    assert fake.last.url.params["limit"] == "2"
    assert "**Trending**" in trending.content


@pytest.mark.asyncio
async def test_r_rating_is_refused_before_any_call(fake: FakeGiphy) -> None:
    result = await _call("search_giphy_gifs", query="party", rating="r")
    assert result.content.startswith("Error: rating must be one of g, pg, pg-13")
    assert fake.requests == []


@pytest.mark.asyncio
async def test_query_bounds(fake: FakeGiphy) -> None:
    assert (await _call("search_giphy_gifs", query="  ")).content == "Error: query is required."
    too_long = await _call("search_giphy_gifs", query="x" * (giphy.MAX_QUERY_CHARS + 1))
    assert too_long.content.startswith("Error: query must be at most 50 characters")
    assert fake.requests == []


@pytest.mark.asyncio
async def test_rendition_falls_back_and_skips_gifs_without_image(fake: FakeGiphy) -> None:
    fixed = "https://media1.giphy.com/media/fx/200.gif"
    fake.routes["/v1/gifs/search"] = _listing(
        _gif("fx", fixed_height={"url": fixed, "width": "356", "height": "200"}),
        _gif("none", original={"url": "http://insecure.example/x.gif"}),
    )
    result = await _call("search_giphy_gifs", query="party")
    assert f"]({fixed})" in result.content
    assert "id none" not in result.content
    assert result.content.startswith("1 GIF(s) for 'party'")


@pytest.mark.asyncio
async def test_empty_answers(fake: FakeGiphy) -> None:
    fake.routes["/v1/gifs/search"] = _listing()
    fake.routes["/v1/gifs/translate"] = _single([])
    assert (await _call("search_giphy_gifs", query="zzz")).content == "No GIF found for 'zzz'."
    assert (await _call("get_giphy_reaction_gif", phrase="zzz")).content == "No GIF found for 'zzz'."


@pytest.mark.asyncio
async def test_a_rejected_key_never_reaches_the_answer(fake: FakeGiphy) -> None:
    result = await _call("search_giphy_gifs", key="wrong-key-value", query="party")
    assert result.content.startswith("Error: ")
    assert "wrong-key-value" not in result.content
    assert KEY not in result.content
