# SPDX-License-Identifier: Apache-2.0

"""Where the Grouping of a CTEM threat package really is in OpenCTI (#1547).

A threat package recorded with the Grouping page reads ``validated``
(``ctem_cases_view.threat_package_state``), so that page is only true once the
Grouping resolves in OpenCTI's live graph. A Grouping still inside an open
draft has no live page: its package must carry the draft page and read
``drafted``. This module reads it with an acting user's own OpenCTI access
(``ctem_metrics_collector._user_creds``, never a system token):

- :func:`grouping_placement`: ``live``, ``draft`` (with the open draft holding
  it), ``absent`` (not in the live graph nor in an open draft the user reads)
  or ``unknown`` (OpenCTI did not answer, or no access).
- :func:`verify_package`: the record-time check of ``update_ctem_case``: a
  Grouping page is kept when live, replaced by the draft page when the
  Grouping sits in an open draft, refused otherwise.
- :func:`repair_stored_packages`: the one-off administrator repair of the
  packages already stored with a Grouping page (dry run by default).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.integrations.providers.opencti._common import _DRAFT_HEADER, _graphql
from app.integrations.providers.opencti.capabilities import attach_probe
from app.models.ctem_case import CtemCase, CtemCaseEvent
from app.services import ctem_cases as rules
from app.services.ctem_metrics_collector import _user_creds

logger = logging.getLogger(__name__)

LIVE = "live"
DRAFT = "draft"
ABSENT = "absent"
UNKNOWN = "unknown"

#: Most open drafts searched for one Grouping.
DRAFT_SEARCH_CAP = 25
#: Most stored packages one repair call re-reads.
REPAIR_CAP = 500

_GROUPING = "query CtemGroupingPlacement($id: String!) { grouping(id: $id) { id } }"
_OPEN_DRAFTS = (
    "query CtemOpenDrafts { draftWorkspaces(filters: { mode: and, filters: "
    '[{ key: "draft_status", values: ["open"] }], filterGroups: [] }) { edges { node { id } } } }'
)


@dataclass(frozen=True)
class Placement:
    """Where one Grouping is: :data:`LIVE`, :data:`DRAFT`, :data:`ABSENT` or :data:`UNKNOWN`."""

    where: str
    base_url: str = ""
    draft_id: str = ""
    detail: str = ""

    @property
    def draft_page(self) -> str:
        return f"{self.base_url.rstrip('/')}/dashboard/data/import/draft/{self.draft_id}" if self.draft_id else ""


async def _resolves(
    client: Any, base_url: str, headers: dict[str, str], grouping_id: str, *, draft_id: str = ""
) -> bool | None:
    """Whether *grouping_id* resolves in the live graph (no *draft_id*) or inside that draft; ``None`` unread.

    The headers carry the capability probe (``attach_probe``) as every background
    CTEM read does: a read the user's role cannot pass is never sent.
    """
    sent = {k: v for k, v in headers.items() if k != _DRAFT_HEADER}
    if draft_id:
        sent[_DRAFT_HEADER] = draft_id
    probed = attach_probe(sent, base_url)
    data, err = await _graphql(client, base_url, probed, _GROUPING, {"id": grouping_id}, live=not draft_id)
    if err:
        logger.info("CTEM threat package: Grouping read failed (draft %s): %s", draft_id or "live", err)
        return None
    return bool(((data or {}).get("grouping") or {}).get("id"))


async def grouping_placement(
    db: AsyncSession, user_id: str, grouping_id: str, *, draft_first: bool = False
) -> Placement:
    """Where *grouping_id* is for *user_id*'s own OpenCTI read (see the module docstring).

    *draft_first* (#1573): an open draft holding the Grouping wins over the live
    graph, for a package whose review did not approve it: an entity that exists
    live and is edited in a draft is still a drafted package.
    """
    try:
        cred = await _user_creds(db, user_id, "opencti")
    except Exception as exc:
        logger.info("CTEM threat package: OpenCTI access unreadable: %s", format_exception_message(exc))
        cred = None
    if cred is None:
        return Placement(UNKNOWN, detail="no OpenCTI access for this user")
    base_url, headers = cred
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            live = await _resolves(client, base_url, headers, grouping_id)
            if live and not draft_first:
                return Placement(LIVE, base_url)
            if live is None:
                return Placement(UNKNOWN, base_url, detail="OpenCTI did not answer the live graph read")
            data, err = await _graphql(
                client, base_url, attach_probe(dict(headers), base_url), _OPEN_DRAFTS, {}, live=True
            )
            if err:
                return Placement(UNKNOWN, base_url, detail="OpenCTI did not list its open drafts")
            edges = ((data or {}).get("draftWorkspaces") or {}).get("edges") or []
            drafts = [str((e or {}).get("node", {}).get("id") or "") for e in edges][:DRAFT_SEARCH_CAP]
            for draft_id in filter(None, drafts):
                if await _resolves(client, base_url, headers, grouping_id, draft_id=draft_id):
                    return Placement(DRAFT, base_url, draft_id=draft_id)
    except Exception as exc:
        logger.warning("CTEM threat package: OpenCTI read failed: %s", format_exception_message(exc))
        return Placement(UNKNOWN, base_url, detail="OpenCTI could not be read")
    return Placement(LIVE if live else ABSENT, base_url)


def _grouping_page_of(package: dict[str, Any]) -> bool:
    from app.services import ctem_case_loop as loop

    url = str(package.get("url") or "").strip()
    return bool(url) and loop._DRAFT_URL.match(url) is None and loop._GROUPING_URL.match(url) is not None


def _not_approved(review: str | None) -> bool:
    from app.services import ctem_cases_view as view_of

    return review in (view_of.REVIEW_WAITING, view_of.REVIEW_REJECTED, view_of.REVIEW_REPLAN)


async def verify_package(db: AsyncSession, user_id: str, package: Any, review: str | None = None) -> Any:
    """*package* as it may be recorded: a Grouping page only once the Grouping is live.

    A Grouping found inside an open draft is recorded with that draft's page
    (``drafted``); one OpenCTI cannot place is refused (:class:`CaseRuleError`),
    so a Grouping page is never stored on trust. Anything but a Grouping page
    is returned as given (``record_threat_package`` validates it). *review* is
    the case's ``ctem_cases_view.review_outcome`` (#1573): while the review
    waits, was refused or asked for a re-plan, an open draft holding the
    Grouping wins over the live graph, and a Grouping found in none is refused.
    """
    if not isinstance(package, dict) or not _grouping_page_of(package):
        return package
    held = _not_approved(review)
    placement = await grouping_placement(db, user_id, str(package.get("grouping_id") or "").strip(), draft_first=held)
    if placement.where == DRAFT:
        return {**package, "url": placement.draft_page}
    if placement.where == LIVE and held:
        raise rules.CaseRuleError(
            "threat_package.url is the Grouping page but the case's analyst review (ctem.analyst_review) did not "
            "approve the package: record the draft page while the review waits, is refused or asks for a re-plan, "
            "and the Grouping page once the review is approved and the draft applied"
        )
    if placement.where == LIVE:
        return package
    if placement.where == ABSENT:
        raise rules.CaseRuleError(
            "threat_package.url is the Grouping page but the Grouping is neither in OpenCTI's live graph nor in "
            "an open draft you can read: record the draft page (/dashboard/data/import/draft/ then the draft id) "
            "while the package is drafted, and the Grouping page once the draft is validated"
        )
    raise rules.CaseRuleError(
        "threat_package.url is the Grouping page but whether the Grouping is in OpenCTI's live graph could not "
        f"be read ({placement.detail}): record the draft page while the package is drafted, or try again"
    )


async def repair_stored_packages(
    db: AsyncSession, organization_id: uuid.UUID, user_id: str, *, dry_run: bool = True, now: datetime | None = None
) -> dict[str, Any]:
    """Re-read every case's newest package stored with a Grouping page and correct the wrong ones (#1547, #1573).

    Read with *user_id*'s own OpenCTI access (the administrator who runs it). A
    Grouping inside an open draft gets a new ``threat_package`` event with the
    draft page; one absent from the live graph and from every open draft gets
    one with no page (both read ``drafted``); a live one, or one that cannot be
    read, is left as it is (``unreadable`` counts those). A case whose analyst
    review did not approve the package (waiting, refused, re-plan; #1573) is
    read draft first, and a live Grouping with no draft gets no page either:
    its page was never the approved one. ``review`` names that outcome per
    case. *dry_run* writes nothing. The caller commits.
    """
    from app.services import ctem_cases_view as view_of

    rows = (
        await db.execute(
            select(CtemCaseEvent)
            .join(CtemCase, CtemCase.id == CtemCaseEvent.case_id)
            .where(CtemCase.organization_id == organization_id, CtemCaseEvent.kind.in_(("threat_package", "gate")))
            .order_by(CtemCaseEvent.at.asc())
        )
    ).scalars()
    newest: dict[uuid.UUID, CtemCaseEvent] = {}
    gates: dict[uuid.UUID, list[CtemCaseEvent]] = {}
    for row in rows:
        if row.kind == "gate":
            gates.setdefault(row.case_id, []).append(row)
        elif row.kind == "threat_package":
            newest[row.case_id] = row
    candidates = [row for row in newest.values() if row.to_value and _grouping_page_of({"url": row.evidence})]
    corrected: list[dict[str, Any]] = []
    unreadable = 0
    at = now or datetime.now(UTC)
    for row in candidates[:REPAIR_CAP]:
        review = view_of.review_outcome(gates.get(row.case_id, []))
        held = _not_approved(review)
        placement = await grouping_placement(db, user_id, str(row.to_value), draft_first=held)
        if placement.where == UNKNOWN or (placement.where == LIVE and not held):
            unreadable += placement.where == UNKNOWN
            continue
        page = placement.draft_page or None
        corrected.append(
            {
                "case_id": str(row.case_id),
                "grouping_id": row.to_value,
                "placement": placement.where,
                "review": review,
                "url": page,
            }
        )
        if not dry_run:
            db.add(
                CtemCaseEvent(
                    case_id=row.case_id,
                    actor=rules.SYSTEM_ACTOR,
                    kind="threat_package",
                    at=at,
                    from_value=row.from_value,
                    to_value=row.to_value,
                    reason=row.reason,
                    evidence=page,
                )
            )
    return {
        "dry_run": dry_run,
        "checked": min(len(candidates), REPAIR_CAP),
        "remaining": max(len(candidates) - REPAIR_CAP, 0),
        "corrected": len(corrected),
        "unreadable": unreadable,
        "cases": corrected[:200],
    }
