# SPDX-License-Identifier: Apache-2.0

"""The software OpenCTI says a threat targets or uses (#2523).

The OpenCTI listener records a threat (an intrusion set, a threat actor, a
campaign, a malware) from the event payload alone: its evidence never holds
the threat's ``targets`` / ``uses`` relationships, so the scope gate
(``ctem_scope_gate.resolve``) has no software to match and the signal falls
to ``subject_unresolved``. This module reads, with the recorder's own OpenCTI
access (the segregation rule of #933, as :mod:`ctem_cve_software`), the
Software the threat reaches through ``targets`` and ``uses`` (the threat as
source), and the Software that ``has`` each Vulnerability it targets, and
answers their names and CPEs; the gate matches them like any software the
evidence names.

Never raises: a read that cannot answer says why in the second value, and
the gate records it (``reconcile.threat_lookup``) instead of guessing.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.services.ctem_cve_software import (
    _OCTI_CVE_SOFTWARE,
    LOOKUP_FAILED,
    LOOKUP_NO_ACCESS,
    LOOKUP_NO_READER,
    LOOKUP_READ,
    MAX_RELATIONS,
    software_names,
)

logger = logging.getLogger(__name__)

#: Seconds the whole OpenCTI read of one threat may take; past it the gate decides without it.
LOOKUP_TIMEOUT_SECONDS = 15.0
#: Most targeted vulnerabilities followed to their software.
MAX_VULNERABILITIES = 50
#: Most software names answered for one threat.
MAX_SOFTWARE = 200

_OCTI_THREAT_TARGETS = """
query CtemGateThreatSoftware($from: [String], $first: Int) {
  stixCoreRelationships(
    fromId: $from
    relationship_type: ["targets", "uses"]
    toTypes: ["Software", "Vulnerability"]
    first: $first
  ) {
    edges { node { to { ... on Software { name cpe } ... on Vulnerability { vulnerability_id: id } } } }
  }
}
"""
# The Vulnerability fragment reads its id under an alias and never ``name``: sibling inline
# fragments that share a response key with different nullability (``name`` is ``String!`` on
# one type and ``String`` on the other) fail OpenCTI's validation (GRAPHQL_VALIDATION_FAILED, #2523).


def _targets(relations: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """The Software nodes (as ``{"from": node}`` for :func:`software_names`) and the Vulnerability ids (pure)."""
    software: list[dict[str, Any]] = []
    vulns: list[str] = []
    for rel in relations:
        node = (rel or {}).get("to") or {}
        if node.get("vulnerability_id"):
            vid = str(node["vulnerability_id"])
            if vid not in vulns:
                vulns.append(vid)
        elif node.get("name") or node.get("cpe"):
            software.append({"from": node})
    return software, vulns


async def read_threat_software(
    client: httpx.AsyncClient, base_url: str, headers: dict[str, str], threat_id: str
) -> list[str] | None:
    """The software OpenCTI relates to *threat_id* (``targets`` / ``uses``, then the targeted CVEs' ``has``).

    ``None`` when OpenCTI fails on the first read; a failure on the vulnerability
    follow-up keeps the software already read.
    """
    from app.integrations.providers.opencti._common import _graphql

    data, err = await _graphql(
        client, base_url, headers, _OCTI_THREAT_TARGETS, {"from": [threat_id], "first": MAX_RELATIONS}
    )
    if err:
        logger.info("CTEM scope gate: OpenCTI threat lookup unavailable: %s", err)
        return None
    relations = [
        (e or {}).get("node") or {} for e in ((data or {}).get("stixCoreRelationships") or {}).get("edges") or []
    ]
    software, vulns = _targets(relations)
    found = software_names(software)
    if vulns:
        data, err = await _graphql(
            client, base_url, headers, _OCTI_CVE_SOFTWARE, {"to": vulns[:MAX_VULNERABILITIES], "first": MAX_RELATIONS}
        )
        if err:
            logger.info("CTEM scope gate: OpenCTI targeted vulnerability software unavailable: %s", err)
        else:
            more = [
                (e or {}).get("node") or {}
                for e in ((data or {}).get("stixCoreRelationships") or {}).get("edges") or []
            ]
            found += [name for name in software_names(more) if name not in found]
    return found[:MAX_SOFTWARE]


async def threat_software(
    db: AsyncSession, reader: uuid.UUID | None, threat_id: str, *, release: bool = False
) -> tuple[tuple[str, ...], str]:
    """``(software, how the read went)`` for *threat_id*, read with *reader*'s own OpenCTI access.

    *release* True hands the pooled connection back before the OpenCTI call: only a
    caller with no savepoint open (the bulk prefetch, ``prepare_cve_reads``) passes it (#2516).
    """
    if reader is None:
        return (), LOOKUP_NO_READER
    from app.integrations.providers.opencti.capabilities import attach_probe
    from app.services.ctem_metrics_collector import _user_creds

    try:
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
                read_threat_software(client, base_url, attach_probe(headers, base_url), threat_id),
                timeout=LOOKUP_TIMEOUT_SECONDS,
            )
    except TimeoutError:
        logger.warning("CTEM scope gate: OpenCTI threat lookup timed out after %ss", LOOKUP_TIMEOUT_SECONDS)
        found = None
    except Exception as exc:
        logger.warning("CTEM scope gate: OpenCTI threat lookup failed: %s", format_exception_message(exc))
        found = None
    if found is None:
        return (), LOOKUP_FAILED
    return tuple(found), LOOKUP_READ
