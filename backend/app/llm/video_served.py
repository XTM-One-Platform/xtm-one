# SPDX-License-Identifier: Apache-2.0

"""Which video models a provider still serves, as the provider itself answered.

A provider's listing is not always what it serves: Kling AI, Runway, Seedance and ElevenLabs list a catalog of
their own once the key reads the account, and Gemini lists its built-in video model when its listing fails. A
model such a provider dropped stayed offered by ``video_studio_capabilities`` and accepted as a shot's model or
fallback, then met only when its generation answered HTTP 404 - for a fallback, after the primary take failed its
check, the regeneration lost.

So what the provider answers about a model is kept. A probe that makes no video
(``BaseLLMProvider.probe_video_model``, on the providers that have one) answering HTTP 404, or a generation
answering HTTP 404 for the model (``video.VideoModelNotServedError``), records it as not served on that provider
configuration (:func:`note`): one Redis key per configuration and model, read by every worker and the API, for
:data:`RECORD_SECONDS`. Every reader asks :func:`problem` / :func:`problems`: the router's resolution
(``get_video_provider``: a model named, the platform's, a shot's fallback), and the listing
``video_studio_capabilities`` shows (``router.video_model_listing``: such a model removed, named apart with why).
A probe's answer is kept in the process for :data:`PROBE_SECONDS`; a job the provider then creates for the model
clears the record (:func:`clear`). With Redis unreachable nothing is shared: the probe still answers, and a model
no probe can ask about is asked as before.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.core.redis import redis_client
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

_PREFIX = "video_studio:not_served:"
#: How long a model the provider answered 404 for stays off its listing: past it, the provider is asked again.
RECORD_SECONDS = 24 * 3600
#: How long a probe's answer is trusted in the process; an answer that was no answer (a timeout) is asked sooner.
PROBE_SECONDS = 3600.0
PROBE_RETRY_SECONDS = 300.0
PROBE_TIMEOUT_SECONDS = 10.0
#: The probes asked at once for one listing.
_PROBES_AT_ONCE = 4
_probed: dict[tuple[str, str], tuple[float, str | None]] = {}
_MAX_PROBED = 1024


def label_of(config: Any) -> str:
    """How a provider configuration is named to the person: its display name, else its provider."""
    return getattr(config, "display_name", None) or getattr(config, "provider", None) or "its provider"


def sentence(model: str, label: str, detail: str | None = None) -> str:
    """``kling-v2-1 is not served by Kling (HTTP 404: model not found)``."""
    said = (detail or "").strip()
    return f"{model} is not served by {label} (HTTP 404{': ' + said[:200] if said else ''})"


def answered_not_found(exc: BaseException) -> bool:
    """Whether a provider's refusal is HTTP 404, read from the status its error carries (SDK or own API)."""
    for name in ("status_code", "code"):
        value = getattr(exc, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value == 404
    return False


def _key(provider_id: object, model: str) -> str:
    return f"{_PREFIX}{provider_id}:{model}"


async def recorded(provider_id: object, models: list[str]) -> dict[str, str]:
    """``{model: why}`` of the *models* the provider configuration answered 404 for, while the record lasts."""
    if not provider_id or not models:
        return {}
    try:
        values = await (await redis_client()).mget([_key(provider_id, model) for model in models])
    except Exception as exc:
        logger.debug("Video provider %s: its unserved models were not read: %s", provider_id, format_exception_message(exc))
        return {}
    return {
        model: value.decode() if isinstance(value, bytes) else str(value)
        for model, value in zip(models, values or [], strict=False)
        if value
    }


async def note(provider_id: object, model: str, reason: str) -> None:
    """Record that the provider configuration does not serve *model*, *reason* saying so (:func:`sentence`)."""
    if not provider_id or not model:
        return
    _remember(str(provider_id), model, reason, PROBE_SECONDS)
    try:
        await (await redis_client()).set(_key(provider_id, model), reason[:600], ex=RECORD_SECONDS)
        logger.info("Video model %s recorded as not served by provider %s: %s", model, provider_id, reason)
    except Exception as exc:
        logger.warning("Video model %s not recorded as not served: %s", model, format_exception_message(exc))


async def clear(provider_id: object, model: str) -> None:
    """The provider created a job for *model*: it serves it."""
    if not provider_id or not model:
        return
    _probed.pop((str(provider_id), model), None)
    try:
        await (await redis_client()).delete(_key(provider_id, model))
    except Exception as exc:
        logger.debug("Video model %s of provider %s not cleared: %s", model, provider_id, format_exception_message(exc))


def _remember(provider_id: str, model: str, reason: str | None, seconds: float) -> None:
    if len(_probed) >= _MAX_PROBED:
        now = time.monotonic()
        stale = [key for key, (until, _reason) in _probed.items() if until <= now]
        for key in stale or list(_probed)[: _MAX_PROBED // 2]:
            _probed.pop(key, None)
    _probed[(provider_id, model)] = (time.monotonic() + seconds, reason)


async def _probe(provider: Any, config: Any, model: str) -> str | None:
    """Why the provider says it does not serve *model* (it answered 404), or ``None``: it serves it, or cannot say."""
    provider_id = str(getattr(config, "id", "") or "")
    kept = _probed.get((provider_id, model))
    if kept is not None and kept[0] > time.monotonic():
        return kept[1]
    probe = getattr(provider, "probe_video_model", None)
    if probe is None:
        _remember(provider_id, model, None, PROBE_SECONDS)
        return None
    try:
        await asyncio.wait_for(probe(model), timeout=PROBE_TIMEOUT_SECONDS)
    except NotImplementedError:
        _remember(provider_id, model, None, PROBE_SECONDS)
        return None
    except Exception as exc:
        if not answered_not_found(exc):
            logger.debug("Video model %s not probed on %s: %s", model, label_of(config), format_exception_message(exc))
            _remember(provider_id, model, None, PROBE_RETRY_SECONDS)
            return None
        reason = sentence(model, label_of(config), format_exception_message(exc))
        await note(provider_id, model, reason)
        return reason
    _remember(provider_id, model, None, PROBE_SECONDS)
    return None


async def problem(provider: Any, config: Any, model: str) -> str | None:
    """Why *config*'s provider does not serve *model* - recorded, or its probe answering 404 -, or ``None``."""
    return (await problems(provider, config, [model])).get(model)


async def problems(provider: Any, config: Any, models: list[str]) -> dict[str, str]:
    """``{model: why}`` of the *models* *config*'s provider does not serve: what it answered before, else its probe."""
    provider_id = getattr(config, "id", None)
    found = await recorded(provider_id, list(models))
    asked = [model for model in dict.fromkeys(models) if model and model not in found]
    if provider is None or not asked:
        return found
    gate = asyncio.Semaphore(_PROBES_AT_ONCE)

    async def probed(model: str) -> tuple[str, str | None]:
        async with gate:
            return model, await _probe(provider, config, model)

    for model, reason in await asyncio.gather(*(probed(model) for model in asked)):
        if reason:
            found[model] = reason
    return found
