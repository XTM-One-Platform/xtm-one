# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Knowledge bases the extraction links names to.

ATT&CK is the reference catalogue of the CTI domain (techniques, groups, software,
campaigns), so the extraction engine reads it directly from the MITRE ATT&CK
integration's cached store (``lookup_attack_names``: the official STIX bundles, through
the platform's egress client). The source is the one the deployment's company-managed
MITRE ATT&CK integration configures (its mirror, for an air-gapped deployment, and its
pinned release), else the official latest release. A failed load is not retried for
:data:`_KNOWLEDGE_RETRY_SECONDS`: the extraction goes on unlinked.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.egress import EgressRoute

logger = logging.getLogger(__name__)

# When ATT&CK could not be loaded (no network, no mirror), how long extractions skip it.
_KNOWLEDGE_RETRY_SECONDS = 600.0
# How long the configured source is kept before it is read again.
_SOURCE_TTL_SECONDS = 300.0
_source: tuple[float, str, str] | None = None


async def _configured_source() -> tuple[str, str]:
    """``(source_url, version)`` of the most recently updated company-managed MITRE ATT&CK
    integration, sanitized as its own tools read them; the official latest release when
    there is none or its configuration cannot be read."""
    from sqlalchemy import select

    from app.core.database import async_session
    from app.integrations.providers.mitre_attack import DEFAULT_SOURCE_URL, parse_attack_config
    from app.models.integration import Integration
    from app.services.vault import reveal_json_field

    try:
        async with async_session() as db:
            integration = (
                await db.execute(
                    select(Integration)
                    # access_restricted: bypass - the deployment's ATT&CK source, read for no acting user; only its public URL and release are used.
                    .where(Integration.provider == "mitre_attack", Integration.is_company_managed.is_(True))
                    .order_by(Integration.updated_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            extra = await reveal_json_field(integration, "extra_config_enc") if integration else None
    except Exception as exc:  # a knowledge base is an enrichment, never a failure
        logger.warning("ATT&CK source configuration unreadable: %s", exc)
        return DEFAULT_SOURCE_URL, ""
    extra = extra if isinstance(extra, dict) else {}
    _, version, source = parse_attack_config(
        "||".join(str(extra.get(key) or "").strip() for key in ("domains", "version", "source_url"))
    )
    return source, version


async def _attack_source() -> tuple[str, str]:
    global _source
    now = time.monotonic()
    if _source is None or now - _source[0] > _SOURCE_TTL_SECONDS:
        source_url, version = await _configured_source()
        _source = (now, source_url, version)
    return _source[1], _source[2]


#: Per source (``(source_url, version)``): until when its loading is skipped after a failure.
_down_until: dict[tuple[str, str], float] = {}


def attack_functions(
    source: tuple[str, str, EgressRoute | None] | None = None,
) -> tuple[Callable[[list[str]], Awaitable[dict[str, list] | None]], Callable[[], Awaitable[list | None]]]:
    """``(knowledge, catalogue)``: names → ATT&CK entries and every live technique, read from
    *source* — the run's bound MITRE ATT&CK integration: its ``(source_url, version)`` and
    its relay route, as its own tools read it — or, with none, the deployment's configured
    source. Each source is skipped for :data:`_KNOWLEDGE_RETRY_SECONDS` after a failure;
    both return ``None`` while it cannot be loaded (an enrichment, never a failure)."""
    from app.core.egress import egress_route
    from app.integrations.providers.mitre_attack import attack_techniques, lookup_attack_names

    async def _where() -> tuple[str, str, EgressRoute | None]:
        if source is not None:
            return source
        source_url, version = await _attack_source()
        return source_url, version, None

    async def _load(read: Callable[[str, str], Awaitable[object]]) -> object:
        source_url, version, route = await _where()
        key = (source_url, version)
        if time.monotonic() < _down_until.get(key, 0.0):
            return None
        with egress_route(route):
            found = await read(source_url, version)
        if found is None:
            _down_until[key] = time.monotonic() + _KNOWLEDGE_RETRY_SECONDS
        return found

    async def knowledge(names: list[str]) -> dict[str, list] | None:
        return await _load(lambda url, ver: lookup_attack_names(names, source_url=url, version=ver))  # type: ignore[return-value]

    async def catalogue() -> list | None:
        return await _load(lambda url, ver: attack_techniques(source_url=url, version=ver))  # type: ignore[return-value]

    return knowledge, catalogue


async def attack_knowledge(names: list[str]) -> dict[str, list] | None:
    """Names → ATT&CK entries from the deployment's configured source (:func:`attack_functions`)."""
    return await attack_functions()[0](names)


async def attack_catalogue() -> list | None:
    """Every live ATT&CK enterprise technique (``mitre_attack.attack_techniques``) from the
    deployment's configured source (:func:`attack_functions`): what behaviours are
    classified against."""
    return await attack_functions()[1]()
