# SPDX-License-Identifier: Apache-2.0

"""Image generation shared by the providers.

``BaseLLMProvider.generate_image(prompt, model, size)`` is the contract: the
callers ask for a ``WxH`` size. A model that draws at aspect ratios instead is
asked for the ratio it offers nearest that size (:func:`nearest_aspect_ratio`).

A drawing the provider could not take now - a rate limit, a server error - is
asked again within the call (:func:`draw_retrying`), and a failure is said
with the model, the HTTP status and the provider's own words
(:func:`failure_message`).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Sequence

from app.llm.provider_errors import api_error_message, error_status, retry_delay

#: A drawing the provider refused as rate limited or failed with a server error is asked again this many times ...
IMAGE_RETRIES = 2
#: ... after the wait it named, else this long, doubling ...
IMAGE_RETRY_FIRST_WAIT_SECONDS = 4.0
#: ... never longer than this (a provider asking more is not asked again in the call) ...
IMAGE_RETRY_MAX_WAIT_SECONDS = 30.0
#: ... and only while this long is left before the call's deadline for the drawing itself.
IMAGE_DRAW_SECONDS = 30.0


class ImageGenerationError(RuntimeError):
    """An image the provider could not make; ``str()`` is safe to show."""


def _asked_again(exc: BaseException) -> bool:
    status = error_status(exc)
    return status is not None and (status == 429 or status >= 500)


async def draw_retrying(draw: Callable[[], Awaitable[bytes]], *, deadline: float | None = None) -> bytes:
    """*draw*, asked again when its provider rate limited it or failed with a server error.

    At most :data:`IMAGE_RETRIES` times, after the wait the provider named or
    a back-off, each up to a quarter longer at random; never when the
    provider asks to wait longer than :data:`IMAGE_RETRY_MAX_WAIT_SECONDS`,
    nor once *deadline* (``time.monotonic()``) leaves no room for another
    drawing. The last failure is raised.
    """
    attempt = 0
    while True:
        try:
            return await draw()
        except Exception as exc:
            asked = retry_delay(exc)
            if attempt >= IMAGE_RETRIES or not _asked_again(exc) or (asked or 0) > IMAGE_RETRY_MAX_WAIT_SECONDS:
                raise
            wait = asked if asked is not None else IMAGE_RETRY_FIRST_WAIT_SECONDS * 2**attempt
            wait *= random.uniform(1.0, 1.25)  # noqa: S311 - spreading retries, not a secret
            if deadline is not None and time.monotonic() + wait + IMAGE_DRAW_SECONDS > deadline:
                raise
        await asyncio.sleep(wait)
        attempt += 1


def failure_message(exc: BaseException, model: str | None) -> str:
    """What a failed drawing tells the caller: the model, the HTTP status its provider answered, the provider's words."""
    status = error_status(exc)
    answered = f" (HTTP {status})" if status is not None else ""
    if status == 413:
        # Any provider's 413; the AI Gateway's message names no size. Said, so the pictures are sent smaller.
        answered = " (HTTP 413, the request is too large)"
    return f"Image generation failed with {model or 'the image model'}{answered}: {api_error_message(exc)}"


def _ratio(text: str) -> float:
    width, height = text.split(":")
    return int(width) / int(height)


def nearest_aspect_ratio(size: str, ratios: Sequence[str], *, default: str = "1:1") -> str:
    """The ratio of *ratios* nearest a ``WxH`` *size*; *default* for anything else (``auto``)."""
    try:
        width, height = (int(value) for value in str(size).lower().split("x", 1))
    except ValueError:
        return default
    if width <= 0 or height <= 0:
        return default
    return min(ratios, key=lambda text: abs(_ratio(text) - width / height))


def accepted_image(data: bytes, mime: str | None, accepted: Sequence[str]) -> tuple[bytes, str]:
    """*data* as a type the provider takes: as it is when *mime* is one, else re-encoded as PNG.

    A start image is a file someone uploaded, so its type is whatever they had.
    Raises :class:`ImageGenerationError` when the bytes are not an image at all.
    """
    media_type = (mime or "").split(";", 1)[0].strip().lower()
    if media_type in accepted:
        return data, media_type
    import io

    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.seek(0)
            converted = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            out = io.BytesIO()
            converted.save(out, format="PNG")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageGenerationError(f"The start image could not be read as an image ({type(exc).__name__}).") from None
    return out.getvalue(), "image/png"
