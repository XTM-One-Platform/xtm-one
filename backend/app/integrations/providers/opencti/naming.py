"""OpenCTI integration - how the platform names the objects it already holds.

A STIX bundle merges into the platform's objects only when it names them as the
platform does: OpenCTI derives the standard id of an intrusion set, a malware or
a sector from its name, so "Fancy Bear" lands as a new intrusion set beside the
"APT28" that holds it as an alias, and "Russia" beside "Russian Federation".
:func:`platform_names_lookup` answers, for names the extraction read, the name
of the one object the platform holds under that name or alias, with its aliases.

It reads with the connection the provider's own tools use (``build_tools``
hands it over through its ``platform_names_sink`` keyword): the same base URL,
the same headers - token re-mints, draft scope and the unattended runs'
capability probe included - through :func:`_client` and :func:`_graphql`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ._common import _client, _edges, _graphql

logger = logging.getLogger(__name__)

#: An extraction kind -> the OpenCTI entity type the bundle writes for it, and the
#: attribute that holds that type's aliases. A threat actor is written as an
#: individual (``Threat-Actor-Individual``): a group is an intrusion set.
ENTITY_TYPES: dict[str, tuple[str, str]] = {
    "intrusion-set": ("Intrusion-Set", "aliases"),
    "threat-actor": ("Threat-Actor-Individual", "aliases"),
    "campaign": ("Campaign", "aliases"),
    "malware": ("Malware", "aliases"),
    "tool": ("Tool", "aliases"),
    "sector": ("Sector", "x_opencti_aliases"),
    "organization": ("Organization", "x_opencti_aliases"),
    "country": ("Country", "x_opencti_aliases"),
    "region": ("Region", "x_opencti_aliases"),
    "city": ("City", "x_opencti_aliases"),
}

#: Names sent in one query, queries in flight at once, names read in one lookup,
#: and the time one lookup may take in all.
BATCH_NAMES = 25
CONCURRENCY = 3
MAX_NAMES = 300
TIMEOUT_SECONDS = 30.0
#: Objects one query may answer: a page that does not hold them all leaves its
#: names unanswered, since a name could match an object past the page.
_PAGE = 100

_QUERY = """
query XtmPlatformNames($types: [String], $filters: FilterGroup, $first: Int) {
  stixDomainObjects(types: $types, filters: $filters, first: $first) {
    edges { node {
      id
      entity_type
      ... on IntrusionSet { name aliases }
      ... on ThreatActor { name aliases }
      ... on Campaign { name aliases }
      ... on Malware { name aliases }
      ... on Tool { name aliases }
      ... on Sector { name x_opencti_aliases }
      ... on Organization { name x_opencti_aliases }
      ... on Country { name x_opencti_aliases }
      ... on Region { name x_opencti_aliases }
      ... on City { name x_opencti_aliases }
    } }
    pageInfo { globalCount }
  }
}
"""

PlatformNames = Callable[[list[tuple[str, str]]], Awaitable[dict[tuple[str, str], tuple[str, list[str]]] | None]]


def _fold(value: object) -> str:
    return " ".join(str(value).split()).casefold() if isinstance(value, str) else ""


def _filters(names: list[str], alias_key: str) -> dict[str, Any]:
    """Exact matches (``eq``) of the name or of an alias, never a fuzzy ``search``."""
    return {
        "mode": "or",
        "filters": [
            {"key": ["name"], "values": names, "operator": "eq", "mode": "or"},
            {"key": [alias_key], "values": names, "operator": "eq", "mode": "or"},
        ],
        "filterGroups": [],
    }


def fair_share(pairs: list[tuple[str, str]], limit: int) -> list[tuple[str, str]]:
    """At most *limit* of *pairs*, taken in turn from each kind (in the order kinds first
    appear): a long report's late kinds (sectors, tools) keep a share instead of being
    cut whole."""
    by_kind: dict[str, list[tuple[str, str]]] = {}
    for pair in pairs:
        by_kind.setdefault(pair[0], []).append(pair)
    kept: list[tuple[str, str]] = []
    queues = list(by_kind.values())
    while len(kept) < limit and any(queues):
        for queue in queues:
            if queue and len(kept) < limit:
                kept.append(queue.pop(0))
    return kept


def platform_names_lookup(base_url: str, headers: Any) -> PlatformNames:
    """The lookup of names on the OpenCTI at *base_url*, read with *headers*.

    It takes ``(kind, name)`` pairs (an extraction kind of :data:`ENTITY_TYPES`;
    other kinds are not asked) and answers, for each name exactly one object of
    that type holds as its name or an alias (case-folded), that object's name and
    aliases. A name no object or several objects hold has no answer. ``None``
    when the platform could not be read: a lookup enriches, it never fails.
    """

    async def _query(client: Any, entity_type: str, alias_key: str, names: list[str]) -> list[dict[str, Any]] | None:
        variables = {"types": [entity_type], "filters": _filters(names, alias_key), "first": _PAGE}
        data, err = await _graphql(client, base_url, headers, _QUERY, variables)
        if err:
            logger.info("OpenCTI platform names (%s) not read: %s", entity_type, err[:200])
            return None
        nodes = [node for node in _edges(data, "stixDomainObjects") if isinstance(node, dict)]
        total = ((data.get("stixDomainObjects") or {}).get("pageInfo") or {}).get("globalCount")
        if isinstance(total, int) and total > len(nodes):
            return None
        return nodes

    async def lookup(pairs: list[tuple[str, str]]) -> dict[tuple[str, str], tuple[str, list[str]]] | None:
        wanted: dict[str, dict[str, list[tuple[str, str]]]] = {}
        for kind, name in fair_share(pairs, MAX_NAMES):
            if kind in ENTITY_TYPES and _fold(name):
                wanted.setdefault(kind, {}).setdefault(_fold(name), []).append((kind, name))
        if not wanted:
            return {}
        jobs = [
            (kind, list(folded)[start : start + BATCH_NAMES])
            for kind, folded in wanted.items()
            for start in range(0, len(folded), BATCH_NAMES)
        ]
        gate = asyncio.Semaphore(CONCURRENCY)

        async def _job(client: Any, kind: str, folded: list[str]) -> tuple[str, list[str], list[dict[str, Any]] | None]:
            entity_type, alias_key = ENTITY_TYPES[kind]
            # Each name as the text writes it and in its usual casings ("finance" is
            # the platform's "Finance", "apt28" its "APT28"), whether or not the
            # platform's ``eq`` ignores case; the answer is matched case-folded below.
            names = list(dict.fromkeys(
                form
                for f in folded
                for written in [" ".join(wanted[kind][f][0][1].split())]
                for form in (written, written.lower(), written.title(), written.upper())
            ))
            async with gate:
                return kind, folded, await _query(client, entity_type, alias_key, names)

        async def _all() -> list[tuple[str, list[str], list[dict[str, Any]] | None]]:
            # One client for the lookup's batches: one connection (and TLS handshake,
            # through a relay too) instead of one per batch.
            async with _client(headers, timeout=20) as client:
                return list(await asyncio.gather(*(_job(client, kind, folded) for kind, folded in jobs)))

        try:
            results = await asyncio.wait_for(_all(), TIMEOUT_SECONDS)
        except Exception as exc:  # a timeout or a transport failure: no names, never a failure
            logger.info("OpenCTI platform names not read: %s", exc)
            return None
        if all(nodes is None for _, _, nodes in results):
            return None
        found: dict[tuple[str, str], tuple[str, list[str]]] = {}
        for kind, folded, nodes in results:
            if nodes is None:
                continue
            for key in folded:
                holders = {
                    node.get("id"): node
                    for node in nodes
                    if isinstance(node.get("name"), str)
                    and key in {_fold(node["name"]), *(_fold(a) for a in node.get(ENTITY_TYPES[kind][1]) or [])}
                }
                if len(holders) != 1:
                    continue
                node = next(iter(holders.values()))
                aliases = [a for a in node.get(ENTITY_TYPES[kind][1]) or [] if isinstance(a, str)]
                for pair in wanted[kind][key]:
                    found[pair] = (" ".join(node["name"].split()), aliases)
        return found

    return lookup


__all__ = ["ENTITY_TYPES", "PlatformNames", "platform_names_lookup"]
