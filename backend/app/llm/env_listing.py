# SPDX-License-Identifier: Apache-2.0

"""Env-seeded providers whose model list is their own listing, not ``*_MODELS``.

Shared by the boot seed (``main._seed_llm_providers_from_env``) and the
Settings refresh (``POST /settings/llm-providers/{id}/refresh-models``), so a
refresh lists exactly the catalogs a boot would.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Iterable
from contextlib import suppress
from typing import Any, NamedTuple

from app.core.utils import format_exception_message
from app.llm.env_models import ENV_PREFIX, env_models_for

logger = logging.getLogger(__name__)

# The only provider whose listing marks a chat default (#1421).
_GATEWAY = "filigran_gateway"
# Providers whose chat models the seed lists when ``LLM_PROVIDER_{P}_MODELS``
# is unset: the gateway's catalog is curated upstream, so "all" is meaningful.
SEED_LISTED_PROVIDERS = frozenset({_GATEWAY})
# Short: it runs on every boot and under the AI toggle's bootstrap lock.
SEED_LISTING_TIMEOUT_SECONDS = 5.0

_KEY_PATTERN = re.compile(rf"^{ENV_PREFIX}([A-Z][A-Z0-9_]*)_API_KEY$")


class SeedListing(NamedTuple):
    """A provider's chat models, and the one the AI Gateway marks ``default_for: ["chat"]``."""

    models: list[str]
    chat_default: str | None = None


def env_provider_keys(*, warn: bool = True) -> list[tuple[str, str]]:
    """``(provider, key)`` of every ``LLM_PROVIDER_{P}_API_KEY`` the seed applies."""
    from app.llm.router import PROVIDER_MAP
    from app.services.vault import reference_problem

    keys: list[tuple[str, str]] = []
    for name, value in os.environ.items():
        m = _KEY_PATTERN.match(name)
        if not m or not value.strip():
            continue
        provider = m.group(1).lower()
        if provider not in PROVIDER_MAP:
            if warn:
                logger.warning("LLM env seed: unknown provider %r (from %s), skipped", provider, name)
            continue
        # A vault reference is stored as it is and read where the key is used.
        if warn and (problem := reference_problem(value.strip())):
            logger.warning("LLM env seed: %s holds an unusable vault reference: %s", name, problem)
        keys.append((provider, value.strip()))
    return keys


def catalogs_to_list(providers: list[tuple[str, str]], rows: Iterable[Any], models_editable: bool) -> set[str]:
    """The seeded providers whose catalog the seed replaces with their listing.

    A catalog still equal to its last listing (``env_models``) is refreshed;
    one an admin edited is left alone. A provider never listed is listed only
    while no other provider offers a chat model (next to one, a gateway key is
    there for its other models), and in editable mode only while it has no
    catalog.
    """
    from app.llm.router import is_chat_capable_model

    names = {n for n, _ in providers if n in SEED_LISTED_PROVIDERS and env_models_for(n) is None}
    if not names:
        return set()
    rows = list(rows)
    # The seeded rows of the providers to list, and every other config's models.
    own = {r.provider: r for r in rows if r.provider in names and r.user_id is None}
    offered = [m for r in rows if not (r.provider in names and r.user_id is None) for m in r.available_models or []]
    offered += [m for n, _ in providers if n not in names for m in env_models_for(n) or []]
    sole = not any(is_chat_capable_model(m) for m in offered)

    def _to_list(name: str) -> bool:
        row = own.get(name)
        if row is not None and row.env_models is not None:
            return row.available_models == row.env_models
        return sole and (not models_editable or row is None or row.available_models is None)

    return {n for n in names if _to_list(n)}


def catalogs_to_follow(providers: list[tuple[str, str]], rows: Iterable[Any], models_editable: bool) -> set[str]:
    """The listed providers whose catalog an admin owns (editable mode only) and
    may hand back to the listing; pinned, the environment decides alone.

    Only a catalog that exists: the seed never writes one for a provider it
    keeps unlisted, and the boot drops the snapshot of an edited one, so a
    catalog, not ``env_models``, is what says an admin or a listing set it.
    """
    if not models_editable:
        return set()
    eligible = {n for n, _ in providers if n in SEED_LISTED_PROVIDERS and env_models_for(n) is None}
    if not eligible:
        return set()
    rows = list(rows)
    has_catalog = {r.provider for r in rows if r.user_id is None and r.available_models is not None}
    return (eligible & has_catalog) - catalogs_to_list(providers, rows, models_editable)


async def list_models_for_seed(provider_name: str, api_key: str) -> SeedListing | None:
    """The chat models *provider_name* lists, or ``None`` when it cannot answer."""
    from app.llm.router import PROVIDER_MAP, is_chat_capable_model, provider_instance
    from app.services.vault import resolve_secret_text

    provider = None
    try:
        provider = provider_instance(PROVIDER_MAP[provider_name], await resolve_secret_text(api_key))
        listed = await asyncio.wait_for(provider.list_models(), timeout=SEED_LISTING_TIMEOUT_SECONDS)
        # Answered from the listing just made, no second call.
        chat_default = await provider.marked_default_model("chat") if provider_name == _GATEWAY else None
    except Exception as exc:
        logger.warning(
            "LLM env seed: could not list the %s models, its model list is left as it is: %s",
            provider_name,
            format_exception_message(exc),
        )
        return None
    finally:
        if close := getattr(getattr(provider, "client", None), "close", None):
            with suppress(Exception):
                await close()
    models = [m for m in listed if is_chat_capable_model(m)]
    return SeedListing(models, chat_default) if models else None


def declared_default(provider_name: str, chat_default: str | None, listed: list[str]) -> str | None:
    """The default a listing of *provider_name* declares: the provider's own (the gateway's
    ``default`` alias), or, for the AI Gateway, the chat model it marks while listed."""
    from app.llm.router import PROVIDER_MAP

    if provider_name == _GATEWAY and chat_default and chat_default in listed:
        return chat_default
    return getattr(PROVIDER_MAP.get(provider_name), "default_model", None)


def replaced_alias(provider_name: str, declared: str | None) -> str | None:
    """The gateway's ``default`` alias when its marked chat model replaces it, else ``None``:
    a saved alias then counts as unset (#1421)."""
    from app.llm.router import PROVIDER_MAP

    alias = getattr(PROVIDER_MAP.get(provider_name), "default_model", None)
    return alias if provider_name == _GATEWAY and declared != alias else None


def listing_default(
    current: str | None, declared: str | None, listed: list[str], replaced: str | None = None
) -> str | None:
    """The default for a listed catalog: *current* (unless it is *replaced*), else *declared*,
    while listed; else the first."""
    from app.llm.env_models import resolve_default_model

    keep = current in listed and current != replaced
    return resolve_default_model(current if keep else declared, listed)


def apply_listing(cfg: Any, listed: list[str], chat_default: str | None = None) -> None:
    """Make *listed* the catalog of *cfg*, and its snapshot (``env_models``)."""
    declared = declared_default(cfg.provider, chat_default, listed)
    cfg.available_models = listed
    cfg.default_model = listing_default(cfg.default_model, declared, listed, replaced_alias(cfg.provider, declared))
    cfg.env_models = listed


def listing_platform_model(
    listed: list[str], provider_default: str | None, platform_default: str | None, declared: str | None
) -> str | None:
    """The model a listing may pin platform-wide: ``DEFAULT_MODEL`` or the
    provider's declared default, while listed. A first-in-alphabet pick is no
    one's choice."""
    model = platform_default if platform_default and platform_default in listed else provider_default
    return model if model and model in (platform_default, declared) else None
