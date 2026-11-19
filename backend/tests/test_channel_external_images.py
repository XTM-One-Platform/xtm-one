"""A channel reply sends an image of a media integration's host as an image.

The web chat draws any Markdown image an agent writes; an external channel
(Teams, Slack, Telegram, Discord, Google Chat, WhatsApp) drew only the
platform's own images, so a GIF an agent found with the GIPHY integration
reached Teams as a bare Markdown line. ``resolve_channel_images`` now also
extracts an https image whose host a provider declares
(``integration_tools.PROVIDER_MEDIA_HOSTS``), and only those: the address is
LLM-controlled, so any other host is never fetched nor forwarded.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.channels import base as channel_base
from app.channels.base import (
    EXTERNAL_IMAGE_SOURCE,
    MAX_EXTERNAL_CHANNEL_IMAGES,
    external_image_id,
    fetch_embedded_image,
    image_upload_filename,
    is_trusted_media_url,
    resolve_channel_images,
    trusted_media_hosts,
)
from app.integrations.file_delivery import FetchedFile, FetchError

_BASE = "https://xtm.example"
_HOSTS = ("media.example",)
_GIF = "https://media2.media.example/media/abc/giphy-downsized.gif?cid=x&ct=g"
_GIF_BYTES = b"GIF89a" + b"\x00" * 32


def test_the_media_hosts_come_from_the_providers() -> None:
    assert "giphy.com" in trusted_media_hosts()


@pytest.mark.parametrize(
    ("url", "trusted"),
    [
        ("https://media.example/a.gif", True),
        ("https://media2.media.example/a.gif", True),
        ("https://MEDIA.example./a.gif", True),
        ("https://media.example:443/a.gif", True),
        ("http://media.example/a.gif", False),
        ("https://media.example:8443/a.gif", False),
        ("https://media.example@evil.example/a.gif", False),
        ("https://user:pw@media.example/a.gif", False),
        ("https://evilmedia.example/a.gif", False),
        ("https://media.example.evil.example/a.gif", False),
        ("https://evil.example/?u=media.example", False),
        ("https://media.example:notaport/a.gif", False),
        ("", False),
    ],
)
def test_trusted_media_url(url: str, trusted: bool) -> None:
    assert is_trusted_media_url(url, _HOSTS) is trusted


def test_an_image_on_a_media_host_is_extracted_and_kept_in_the_text() -> None:
    text = f"Bravo ! ![party parrot]({_GIF})"
    out, images = resolve_channel_images(text, _BASE, media_hosts=_HOSTS)

    assert out == text
    assert len(images) == 1
    image = images[0]
    assert image.source == EXTERNAL_IMAGE_SOURCE
    assert image.public_url == image.original_path == _GIF
    assert image.alt_text == "party parrot"
    assert image.image_id == external_image_id(_GIF)


def test_any_other_host_is_left_alone() -> None:
    text = "![tracker](https://evil.example/pixel.gif?leak=secret) ![x](http://media.example/a.gif)"
    assert resolve_channel_images(text, _BASE, media_hosts=_HOSTS) == (text, [])
    # And without media hosts, nothing external is ever taken.
    assert resolve_channel_images(f"![g]({_GIF})", _BASE) == (f"![g]({_GIF})", [])


def test_internal_and_external_images_keep_their_order() -> None:
    file_id = str(uuid.uuid4())
    text = f'![first]({_GIF}) then ![chart](/api/v1/chat/files/{file_id}/download) then ![last](<https://media.example/z.gif> "t")'
    _, images = resolve_channel_images(text, _BASE, allowed_ids={file_id}, media_hosts=_HOSTS)

    assert [image.alt_text for image in images] == ["first", "chart", "last"]
    assert [image.source for image in images] == [EXTERNAL_IMAGE_SOURCE, "files", EXTERNAL_IMAGE_SOURCE]
    assert images[2].public_url == "https://media.example/z.gif"


def test_a_signed_platform_link_is_never_taken_for_an_external_image() -> None:
    file_id = str(uuid.uuid4())
    _, images = resolve_channel_images(
        f"![c](/api/v1/chat/files/{file_id})",
        _BASE,
        allowed_ids={file_id},
        media_hosts=("xtm.example",),
    )
    assert [image.source for image in images] == ["files"]


def test_external_images_are_deduplicated_and_bounded() -> None:
    urls = [f"https://media.example/{i}.gif" for i in range(MAX_EXTERNAL_CHANNEL_IMAGES + 3)]
    text = f"![dup]({urls[0]}) " + " ".join(f"![g{i}]({url})" for i, url in enumerate(urls))
    _, images = resolve_channel_images(text, _BASE, media_hosts=_HOSTS)

    assert [image.public_url for image in images] == urls[:MAX_EXTERNAL_CHANNEL_IMAGES]


@pytest.mark.asyncio
async def test_an_external_image_is_downloaded_through_the_public_media_fetcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []

    async def fake_fetch(url: str, *, source: str, max_bytes: int, what: str, **_: Any) -> FetchedFile:
        calls.append((url, max_bytes))
        # The host's declared type is never trusted: the magic number is.
        return FetchedFile(data=_GIF_BYTES, content_type="image/png", filename="")

    monkeypatch.setattr(channel_base, "trusted_media_hosts", lambda: frozenset(_HOSTS))
    monkeypatch.setattr("app.integrations.providers.social_media.fetch_public_media", fake_fetch)

    result = await fetch_embedded_image(
        {"image_id": external_image_id(_GIF), "source": EXTERNAL_IMAGE_SOURCE, "public_url": _GIF}
    )
    assert result == (_GIF_BYTES, "image/gif")
    assert calls == [(_GIF, channel_base.MAX_EXTERNAL_IMAGE_BYTES)]


@pytest.mark.asyncio
async def test_an_untrusted_or_unreadable_external_image_gives_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(url: str, **_: Any) -> FetchError:
        return FetchError("Error: refused")

    async def not_an_image(url: str, **_: Any) -> FetchedFile:
        return FetchedFile(data=b"<html>", content_type="image/gif", filename="")

    async def never(url: str, **_: Any) -> FetchedFile:  # pragma: no cover - must not run
        raise AssertionError("fetched an untrusted host")

    monkeypatch.setattr(channel_base, "trusted_media_hosts", lambda: frozenset(_HOSTS))
    target = "app.integrations.providers.social_media.fetch_public_media"

    monkeypatch.setattr(target, never)
    untrusted = {"source": EXTERNAL_IMAGE_SOURCE, "public_url": "https://evil.example/a.gif"}
    assert await fetch_embedded_image(untrusted) is None

    monkeypatch.setattr(target, refuse)
    assert await fetch_embedded_image({"source": EXTERNAL_IMAGE_SOURCE, "public_url": _GIF}) is None

    monkeypatch.setattr(target, not_an_image)
    assert await fetch_embedded_image({"source": EXTERNAL_IMAGE_SOURCE, "public_url": _GIF}) is None


@pytest.mark.asyncio
async def test_a_platform_image_still_reads_by_its_id(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str]] = []

    async def fake_bytes(resource_id: str, source: str = "images") -> tuple[bytes, str]:
        seen.append((resource_id, source))
        return b"png", "image/png"

    monkeypatch.setattr(channel_base, "fetch_image_bytes", fake_bytes)
    assert await fetch_embedded_image({"image_id": "abc", "source": "files", "public_url": "x"}) == (
        b"png",
        "image/png",
    )
    assert seen == [("abc", "files")]


@pytest.mark.parametrize(
    ("content_type", "name"),
    [
        ("image/gif", "party.gif"),
        ("image/jpeg", "party.jpg"),
        ("image/webp; q=1", "party.webp"),
        ("image/png", "party.png"),
        ("application/octet-stream", "party.png"),
    ],
)
def test_upload_filename_follows_the_type(content_type: str, name: str) -> None:
    assert image_upload_filename("party", content_type) == name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "method", "field", "filename"),
    [
        ("image/gif", "sendAnimation", "animation", "party.gif"),
        ("image/png", "sendPhoto", "photo", "party.png"),
    ],
)
async def test_telegram_sends_a_gif_as_an_animation(
    monkeypatch: pytest.MonkeyPatch, content_type: str, method: str, field: str, filename: str
) -> None:
    """``sendPhoto`` draws a GIF still; ``sendAnimation`` keeps it moving."""
    import httpx

    from app.channels.telegram import TelegramChannel

    async def fake_fetch(img_info: dict[str, Any]) -> tuple[bytes, str]:
        return b"bytes", content_type

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.AsyncClient

    class _Client(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(channel_base, "fetch_embedded_image", fake_fetch)
    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    await TelegramChannel("123:token")._send_photos(
        "42", [{"image_id": "x", "source": EXTERNAL_IMAGE_SOURCE, "public_url": _GIF, "alt_text": "party"}]
    )

    assert len(requests) == 1
    assert requests[0].url.path.endswith(f"/{method}")
    body = requests[0].content.decode("latin-1")
    assert f'name="{field}"; filename="{filename}"' in body
