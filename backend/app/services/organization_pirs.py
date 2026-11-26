# SPDX-License-Identifier: Apache-2.0

"""The Intelligence policy: OpenCTI PIRs stored in XTM One, one minimum score each (#2435).

Every connected OpenCTI's PIRs are read in the background (:func:`refresh_all`,
called by the scheduler tick through :func:`refresh_if_due`) and stored as
:class:`~app.models.organization_pir.OrganizationPir` rows: no sync to
configure, no import to run. A PIR OpenCTI no longer answers keeps its row,
marked gone; it is never dropped from the policy silently.

The organization's policy lives on the same rows: the question, owner, review
cadence and primary assets served (the former profile ``pir_details``), the PIR's
own minimum score, and whether it drives signals (``enabled``).

Readers:

- :func:`overlay_profile` answers the profile's ``pir_ids`` / ``pir_names`` /
  ``pir_details`` / ``pir_min_scores`` from the ENABLED rows, so every reader of
  the profile (the API, the agents' organization context, the CTEM fleets'
  ``organization_context: ["pir_ids"]``) sees the enabled PIRs. A platform with
  no stored PIR answers the profile as saved (nothing to overlay). The
  provisioning and the receiver's PIR score rule read ``pir_min_scores``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.models.organization_pir import PIR_REVIEW_CADENCES, OrganizationPir

logger = logging.getLogger(__name__)

#: How often the scheduler tick re-reads the PIRs of every connected OpenCTI.
REFRESH_INTERVAL = timedelta(hours=1)
#: The minimum score a PIR first seen in OpenCTI gets (the former threat-activity default).
DEFAULT_MIN_SCORE = 75
#: Pages of 100 PIRs read at most per OpenCTI.
MAX_PAGES = 50

_PIRS_QUERY = """
query XtmIntelligencePolicyPirs($first: Int, $after: ID) {
  pirs(first: $first, after: $after) {
    edges { node { id name description pir_type pir_criteria { weight } } }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_last_refresh: datetime | None = None


def criteria_summary(criteria: object) -> str:
    """``"3 criteria, weights 1-3"``: what the table shows of a PIR's criteria (never the raw filters). Pure."""
    weights = [
        c.get("weight")
        for c in (criteria if isinstance(criteria, list) else [])
        if isinstance(c, Mapping) and isinstance(c.get("weight"), int) and not isinstance(c.get("weight"), bool)
    ]
    if not weights:
        return ""
    count = f"{len(weights)} criterion" if len(weights) == 1 else f"{len(weights)} criteria"
    low, high = min(weights), max(weights)
    return f"{count}, weight {low}" if low == high else f"{count}, weights {low}-{high}"


def pir_nodes(data: object) -> list[dict[str, Any]]:
    """The PIR nodes of one ``pirs`` page, each once, with an id. Pure."""
    edges = ((data or {}).get("pirs") or {}).get("edges") if isinstance(data, Mapping) else None
    nodes: list[dict[str, Any]] = []
    for edge in edges if isinstance(edges, list) else []:
        node = edge.get("node") if isinstance(edge, Mapping) else None
        if isinstance(node, Mapping) and str(node.get("id") or "").strip():
            nodes.append(dict(node))
    return nodes


def apply_read(
    rows: list[OrganizationPir],
    integration_id: uuid.UUID,
    nodes: list[dict[str, Any]],
    now: datetime,
) -> list[OrganizationPir]:
    """Merge one OpenCTI's PIRs into *rows* (that integration's and the unsourced ones). Pure on the rows.

    A PIR read is updated (or adopted when it was carried from a profile with no
    source); a new one is answered as a new row, disabled, with
    :data:`DEFAULT_MIN_SCORE`; a row of this integration not read is marked gone,
    never deleted. Answers the new rows for the caller to add.
    """
    by_id = {r.pir_id: r for r in rows if r.integration_id in (integration_id, None)}
    seen: set[str] = set()
    created: list[OrganizationPir] = []
    for node in nodes:
        pir_id = str(node["id"]).strip()
        if pir_id in seen:
            continue
        seen.add(pir_id)
        row = by_id.get(pir_id)
        if row is None:
            row = OrganizationPir(
                id=uuid.uuid4(),
                pir_id=pir_id,
                min_score=DEFAULT_MIN_SCORE,
                enabled=False,
                question="",
                owner="",
                review_cadence="",
                primary_assets=[],
                created_at=now,
            )
            created.append(row)
        row.integration_id = integration_id
        row.name = (str(node.get("name") or "").strip() or pir_id)[:500]
        row.description = str(node.get("description") or "")[:5000]
        row.pir_type = str(node.get("pir_type") or "")[:40]
        row.criteria_summary = criteria_summary(node.get("pir_criteria"))[:500]
        row.last_seen_at = now
        row.gone_at = None
        row.updated_at = now
    for row in by_id.values():
        if row.integration_id == integration_id and row.pir_id not in seen and row.gone_at is None:
            row.gone_at = now
            row.updated_at = now
    return created


#: Identities tried at most per OpenCTI before its read is reported failed.
MAX_IDENTITIES = 5


def is_seed_account(user: Any) -> bool:
    """Whether *user* is the technical seed account (``ADMIN_EMAIL``): never the identity of a PIR read. Pure."""
    from app.core.config import settings

    email = str(getattr(user, "email", "") or "").strip().lower()
    return bool(email) and email == str(settings.admin_email or "").strip().lower()


def ordered_identities(
    acting: Any,
    run_as: list[Any],
    admins: list[Any],
    owner: Any,
    *,
    per_user: bool = True,
) -> list[Any]:
    """The users a PIR read tries, in order, each once, the seed account and inactive users left out. Pure.

    The administrator who clicked Refresh now first; then the run-as users of
    the integration's OpenCTI signal sources; then the active administrators;
    the integration's owner last. The seed account owns company-managed rows
    but has no OpenCTI user, so a read as it is refused (same rule as #2422).
    An integration that does not call OpenCTI per user (*per_user* false: one
    static key) keeps the seed account last, when nobody else is left.
    """
    chosen: list[Any] = []
    seen: set[Any] = set()
    seed: Any = None
    for user in [acting, *run_as, *admins, owner]:
        if user is None or user.id in seen or not getattr(user, "is_active", False):
            continue
        if is_seed_account(user):
            seed = seed or user
            continue
        seen.add(user.id)
        chosen.append(user)
    if not per_user and not chosen and seed is not None:
        chosen.append(seed)
    return chosen[:MAX_IDENTITIES]


async def _identities(db: AsyncSession, integration: Any, acting_user_id: uuid.UUID | None) -> list[Any]:
    from app.models.ctem_signal_source import CtemSignalSource
    from app.models.user import User

    async def user(user_id: Any) -> Any:
        return await db.get(User, user_id) if user_id else None

    sources = (
        (
            await db.execute(
                select(CtemSignalSource.run_as_user_id)
                .where(
                    CtemSignalSource.provider == "opencti",
                    CtemSignalSource.integration_id == integration.id,
                    CtemSignalSource.run_as_user_id.is_not(None),
                )
                .order_by(CtemSignalSource.enabled.desc(), CtemSignalSource.created_at)
            )
        )
        .scalars()
        .all()
    )
    admins = (
        (
            await db.execute(
                select(User).where(User.role == "admin", User.is_active.is_(True)).order_by(User.created_at).limit(20)
            )
        )
        .scalars()
        .all()
    )
    run_as = [await user(user_id) for user_id in dict.fromkeys(sources)]
    return ordered_identities(
        await user(acting_user_id),
        run_as,
        list(admins),
        await user(getattr(integration, "user_id", None)),
        per_user=getattr(integration, "auth_type", None) == "platform_managed",
    )


async def _read_as(db: AsyncSession, integration: Any, user_id: Any) -> list[dict[str, Any]] | str:
    """Every PIR one OpenCTI integration answers to *user_id*, or why it could not be read."""
    from app.services.opencti_trigger_provisioning import integration_gql

    connection = await integration_gql(db, integration, user_id, timeout=30)
    if isinstance(connection, str):
        return connection
    gql = connection[0]
    nodes: list[dict[str, Any]] = []
    after: str | None = None
    for _ in range(MAX_PAGES):
        data, err = await gql(_PIRS_QUERY, {"first": 100, "after": after})
        if err:
            # PIRs are Enterprise Edition: an OpenCTI without them answers an error here.
            return f"PIRs unreadable: {str(err)[:200]}"
        nodes += pir_nodes(data)
        info = ((data or {}).get("pirs") or {}).get("pageInfo") or {}
        after = info.get("endCursor")
        if not info.get("hasNextPage") or not after:
            break
    return nodes


async def _read_integration(
    db: AsyncSession, integration: Any, acting_user_id: uuid.UUID | None = None
) -> list[dict[str, Any]] | str:
    """Every PIR one OpenCTI answers, read as the first identity it accepts, or why none could read it."""
    reasons: list[str] = []
    for person in await _identities(db, integration, acting_user_id):
        try:
            nodes = await _read_as(db, integration, person.id)
        except Exception as exc:  # one identity's failure never stops the next one
            nodes = f"unreachable: {format_exception_message(exc)[:200]}"
        if not isinstance(nodes, str):
            return nodes
        reasons.append(f"{person.email}: {nodes}")
    if not reasons:
        return (
            "no identity to read the PIRs with: enable an OpenCTI signal source with a run-as user, "
            "or refresh as an administrator who has an OpenCTI account"
        )
    return "; ".join(reasons)[:1000]


async def _opencti_integrations(db: AsyncSession) -> list[Any]:
    from app.models.integration import Integration

    return list(
        (
            await db.execute(
                select(Integration)
                .where(
                    Integration.provider == "opencti",
                    # access_restricted: bypass - scheduled system refresh of the platform-wide PIR policy, no acting user.
                    Integration.is_company_managed.is_(True),
                )
                .order_by(Integration.created_at)
            )
        )
        .scalars()
        .all()
    )


async def refresh_all(
    db: AsyncSession, *, now: datetime | None = None, acting_user_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """Read the PIRs of every connected OpenCTI and store them; the caller commits.

    *acting_user_id* is the administrator who asked for the refresh, tried
    first (:func:`ordered_identities`). One integration that cannot be read
    never touches its rows (no PIR is marked gone on a failed read) and never
    stops the others. Answers ``{"read": n, "added": n, "errors":
    {integration_id: reason}, "names": {integration_id: name}}``.
    """
    now = now or datetime.now(UTC)
    rows = list((await db.execute(select(OrganizationPir))).scalars().all())
    read = added = 0
    errors: dict[str, str] = {}
    names: dict[str, str] = {}
    for integration in await _opencti_integrations(db):
        names[str(integration.id)] = str(getattr(integration, "name", "") or "OpenCTI")
        try:
            nodes = await _read_integration(db, integration, acting_user_id)
        except Exception as exc:  # one OpenCTI never stops the refresh of another
            nodes = f"unreachable: {format_exception_message(exc)[:200]}"
        if isinstance(nodes, str):
            errors[str(integration.id)] = nodes
            continue
        created = apply_read(rows, integration.id, nodes, now)
        for row in created:
            db.add(row)
        rows += created
        read += len(nodes)
        added += len(created)
    return {"read": read, "added": added, "errors": errors, "names": names}


async def refresh_if_due(db: AsyncSession, *, now: datetime | None = None) -> bool:
    """The scheduler tick's refresh: at most once per :data:`REFRESH_INTERVAL` per process. Commits."""
    global _last_refresh
    now = now or datetime.now(UTC)
    if _last_refresh is not None and now - _last_refresh < REFRESH_INTERVAL:
        return False
    _last_refresh = now
    try:
        result = await refresh_all(db, now=now)
        await db.commit()
        for integration_id, reason in result["errors"].items():
            logger.warning("Intelligence policy: PIRs of OpenCTI %s not read: %s", integration_id, reason)
    except Exception as exc:
        await db.rollback()
        logger.warning("Intelligence policy: PIR refresh failed: %s", format_exception_message(exc))
        return False
    return True


async def list_rows(db: AsyncSession) -> list[OrganizationPir]:
    """Every stored PIR, enabled first, then by name."""
    rows = (await db.execute(select(OrganizationPir))).scalars().all()
    return sorted(rows, key=lambda r: (not r.enabled, (r.name or r.pir_id).casefold()))


class PirPolicyError(ValueError):
    """A policy edit the Intelligence policy refuses (the API answers 422)."""


def apply_edit(row: OrganizationPir, fields: Mapping[str, Any], now: datetime) -> None:
    """Write the policy fields of one PIR (the OpenCTI fields are never written here). Pure on the row."""
    if "question" in fields:
        row.question = str(fields["question"] or "")[:1000]
    if "owner" in fields:
        row.owner = str(fields["owner"] or "")[:200]
    if "review_cadence" in fields:
        cadence = str(fields["review_cadence"] or "")
        if cadence not in PIR_REVIEW_CADENCES:
            raise PirPolicyError(f"review_cadence: use one of {list(PIR_REVIEW_CADENCES[1:])}")
        row.review_cadence = cadence
    if "primary_assets" in fields:
        assets = fields["primary_assets"] or []
        if not isinstance(assets, list) or len(assets) > 50:
            raise PirPolicyError("primary_assets: a list of at most 50 primary asset names")
        row.primary_assets = list(dict.fromkeys(str(a).strip()[:200] for a in assets if str(a).strip()))
    if "min_score" in fields:
        score = fields["min_score"]
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
            raise PirPolicyError("min_score: an integer from 0 to 100")
        row.min_score = score
    if "enabled" in fields:
        if not isinstance(fields["enabled"], bool):
            raise PirPolicyError("enabled: true or false")
        row.enabled = fields["enabled"]
    row.updated_at = now


def overlay(profile: Any, rows: list[OrganizationPir]) -> Any:
    """*profile* with its PIR fields answered from the enabled *rows* (unchanged when *rows* is empty). Pure."""
    if not rows:
        return profile
    from app.services.organization_context import PirDetail

    enabled = sorted((r for r in rows if r.enabled), key=lambda r: r.pir_id)
    details: dict[str, PirDetail] = {}
    for r in enabled:
        if r.question or r.owner or r.review_cadence or r.primary_assets:
            details[r.pir_id] = PirDetail(
                question=r.question,
                owner=r.owner,
                review_cadence=r.review_cadence,  # type: ignore[arg-type]
                primary_assets=list(r.primary_assets or []),
            )
    return profile.model_copy(
        update={
            "pir_ids": [r.pir_id for r in enabled],
            "pir_names": {r.pir_id: r.name for r in enabled if r.name},
            "pir_details": details,
            "pir_min_scores": {r.pir_id: int(r.min_score) for r in enabled},
        }
    )


async def overlay_profile(db: AsyncSession, profile: Any) -> Any:
    """:func:`overlay` with the stored rows; a failed read answers *profile* as saved."""
    try:
        rows = list((await db.execute(select(OrganizationPir))).scalars().all())
    except Exception as exc:  # the profile is still answered when the table cannot be read
        logger.warning("Intelligence policy: PIRs unreadable: %s", format_exception_message(exc))
        return profile
    return overlay(profile, rows)
