# SPDX-License-Identifier: Apache-2.0

"""The software OpenCTI relates to the CVEs a CTEM signal names (#2171).

A signal recorded from a report or an advisory often names a CVE and nothing
else structured: the scope gate (``ctem_scope_gate.resolve``) then has no
software reference to match against the organization's software, and the
signal falls to ``subject_unresolved``. This module reads, with the
recorder's own OpenCTI access (the segregation rule of #933), the Software
each CVE is related to by ``has`` and answers their names and CPEs; the gate
matches them like any software the evidence names.

Never raises: a read that cannot answer says why in the second value, and
the gate records it (``reconcile.cve_lookup``) instead of guessing.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: Most CVE ids one signal looks up; past it the rest are not read.
MAX_CVES = 20
#: Most ``has`` relations read for those CVEs.
MAX_RELATIONS = 200
#: Seconds the whole OpenCTI read of one signal may take; past it the gate decides without it.
LOOKUP_TIMEOUT_SECONDS = 15.0

LOOKUP_READ = "read"
LOOKUP_NO_READER = "no reader"
LOOKUP_NO_ACCESS = "no OpenCTI access"
LOOKUP_FAILED = "OpenCTI did not answer"

_OCTI_VULNS = """
query CtemGateCveVulns($filters: FilterGroup, $first: Int) {
  vulnerabilities(filters: $filters, first: $first) {
    edges { node { id name } }
  }
}
"""

_OCTI_CVE_SOFTWARE = """
query CtemGateCveSoftware($to: [String], $first: Int) {
  stixCoreRelationships(toId: $to, relationship_type: ["has"], fromTypes: ["Software"], first: $first) {
    edges { node { from { ... on Software { name cpe } } } }
  }
}
"""


def software_names(relations: list[dict[str, Any]]) -> list[str]:
    """The names and CPEs of the Software in *relations*, each once, in order (pure)."""
    out: list[str] = []
    for rel in relations:
        node = (rel or {}).get("from") or {}
        for value in (node.get("name"), node.get("cpe")):
            text = str(value or "").strip()
            if text and text not in out:
                out.append(text)
    return out


async def read_cve_software(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str], cves: list[str]
) -> list[str] | None:
    """The software OpenCTI relates by ``has`` to *cves*; ``None`` when OpenCTI fails."""
    from app.integrations.providers.opencti._common import _graphql
    from app.services.ctem_metrics_collector import _fg

    data, err = await _graphql(
        client,
        base_url,
        headers,
        _OCTI_VULNS,
        {"filters": _fg([{"key": ["name"], "values": cves}]), "first": len(cves) * 2},
    )
    if err:
        logger.info("CTEM scope gate: OpenCTI CVE lookup unavailable: %s", err)
        return None
    ids = sorted(
        {
            str(node["id"])
            for e in ((data or {}).get("vulnerabilities") or {}).get("edges") or []
            if (node := (e or {}).get("node")) and node.get("id")
        }
    )
    if not ids:
        return []
    data, err = await _graphql(client, base_url, headers, _OCTI_CVE_SOFTWARE, {"to": ids, "first": MAX_RELATIONS})
    if err:
        logger.info("CTEM scope gate: OpenCTI CVE software unavailable: %s", err)
        return None
    relations = [
        (e or {}).get("node") or {} for e in ((data or {}).get("stixCoreRelationships") or {}).get("edges") or []
    ]
    return software_names(relations)


async def cve_software(
    db: AsyncSession, reader: uuid.UUID | None, cves: list[str], *, release: bool = False
) -> tuple[tuple[str, ...], str]:
    """``(software, how the read went)`` for *cves*, read with *reader*'s own OpenCTI access.

    *release* True hands the pooled connection back before the OpenCTI call: only a
    caller with no savepoint open (the bulk prefetch, ``prepare_cve_reads``) passes it.
    """
    if reader is None:
        return (), LOOKUP_NO_READER
    from app.integrations.providers.opencti.capabilities import attach_probe
    from app.services.ctem_metrics_collector import _user_creds

    try:
        # release False by default: the record-time gate decides inside a savepoint; a commit
        # here closed it and left every CVE-only signal undecided ("Can't operate on closed
        # transaction"). The bulk prefetch has none open and releases (#2518 review).
        cred = await _user_creds(db, str(reader), "opencti", release=release)
    except Exception as exc:  # unreadable credentials: nothing is guessed
        logger.info("CTEM scope gate: OpenCTI access unreadable: %s", format_exception_message(exc))
        return (), LOOKUP_NO_ACCESS
    if cred is None:
        return (), LOOKUP_NO_ACCESS
    base_url, headers = cred
    try:
        async with httpx.AsyncClient(timeout=LOOKUP_TIMEOUT_SECONDS) as client:
            found = await asyncio.wait_for(
                read_cve_software(client, base_url, attach_probe(headers, base_url), cves[:MAX_CVES]),
                timeout=LOOKUP_TIMEOUT_SECONDS,
            )
    except TimeoutError:
        logger.warning("CTEM scope gate: OpenCTI CVE lookup timed out after %ss", LOOKUP_TIMEOUT_SECONDS)
        found = None
    except Exception as exc:
        logger.warning("CTEM scope gate: OpenCTI CVE lookup failed: %s", format_exception_message(exc))
        found = None
    if found is None:
        return (), LOOKUP_FAILED
    return tuple(found), LOOKUP_READ
