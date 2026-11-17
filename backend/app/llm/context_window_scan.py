# SPDX-License-Identifier: Apache-2.0

"""Scan the context windows of a provider's models (Settings -> AI Models).

The administrator enabling or editing a model gets its window filled in rather
than looked up by hand. Each model is answered from, in order:

1. ``provider`` - the provider's own model listing
   (:meth:`BaseLLMProvider.list_model_context_windows`), the one source that
   knows a self-hosted or gateway model;
2. ``catalog`` - the bundled LiteLLM snapshot (``max_input_tokens``);
3. ``family`` - the model-family table of :mod:`app.llm.model_limits`.

A model none of them knows is returned without a window: the scan never
invents one, and the provider's flat class default is not knowledge of a model.

The scan only reads. What it found is saved as the per-model override
(``LLMProviderConfig.context_windows``) when the administrator saves the models,
like a value typed by hand.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

from app.core.utils import format_exception_message
from app.llm.base import BaseLLMProvider
from app.llm.model_limits import ContextWindowSource, known_context_window

logger = logging.getLogger(__name__)

#: How long the provider's listing may take before the scan answers from the
#: catalog alone. The SDK clients read for up to 10 minutes, built for chat.
PROVIDER_LISTING_TIMEOUT_S = 30.0

#: ``ok``: the provider's listing was read (it may have reported no window).
#: ``unsupported``: the provider's listing carries no window.
#: ``failed``: the listing could not be read; the catalog answered alone.
LiveLookup = Literal["ok", "unsupported", "failed"]


@dataclass(frozen=True)
class ScannedContextWindow:
    model: str
    tokens: int | None
    source: ContextWindowSource | None


@dataclass(frozen=True)
class ContextWindowScan:
    live_lookup: LiveLookup
    results: list[ScannedContextWindow]


async def _provider_windows(
    provider: BaseLLMProvider,
) -> tuple[LiveLookup, dict[str, int]]:
    try:
        async with asyncio.timeout(PROVIDER_LISTING_TIMEOUT_S):
            windows = await provider.list_model_context_windows()
    except Exception as exc:
        # The reason stays in the logs: the caller learns that the provider
        # was not read, never what it answered.
        logger.warning(
            "Context window scan: the %s model listing could not be read: %r",
            provider.provider_name,
            format_exception_message(exc)[:300],
        )
        return "failed", {}
    if windows is None:
        return "unsupported", {}
    return "ok", windows


async def scan_context_windows(
    provider: BaseLLMProvider, models: list[str]
) -> ContextWindowScan:
    """The context window of each of *models*, and where it was found."""
    live_lookup, reported = await _provider_windows(provider)
    results: list[ScannedContextWindow] = []
    for model in models:
        if tokens := reported.get(model):
            results.append(ScannedContextWindow(model, tokens, "provider"))
        elif known := known_context_window(model):
            results.append(ScannedContextWindow(model, known[0], known[1]))
        else:
            results.append(ScannedContextWindow(model, None, None))
    return ContextWindowScan(live_lookup=live_lookup, results=results)
