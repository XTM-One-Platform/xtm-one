# SPDX-License-Identifier: Apache-2.0

"""GitLab dependency vulnerabilities as CTEM signals (#1331, slice 3).

The GitLab asset source reads each project's dependency list
(``organization_context_code``); GitLab lists the vulnerabilities of each
dependency there when the token may read the project's security dashboard.
After a sync saved, each one is recorded once through the one CTEM intake as a
path 3 signal (``code_flaw``, lens ``inside``, subject ``code``) on the
repository asset, as a GitHub Dependabot alert is (slice 2): its evidence
names the package, version, PURL, manifest, severity and link, and writes the
repository's name, id and identity key (``<host>:repository:<id>``) to
``evidence.assets`` so the scope gate resolves it to the repository.

A vulnerability the project no longer lists (fixed or dismissed in GitLab)
closes its case members, as Dependabot's ``fixed`` does, but only for a
project whose dependency list was read whole in this run. Rules only, no
model call; nothing is written to GitLab.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

PROVIDER = "gitlab"
TRIGGER_PATH = "code_flaw"
LENS = "inside"
SUBJECT_KIND = "code"


def signal_fields(finding: Mapping[str, Any]) -> dict[str, Any] | None:
    """The signal of one GitLab dependency finding, ``None`` without its key (pure)."""
    external_id = str(finding.get("external_id") or "").strip()
    if not external_id:
        return None
    package = str(finding.get("package") or "").strip()
    repository = str(finding.get("repository") or "").strip()
    evidence: dict[str, Any] = {"event": "gitlab_dependency_vulnerability"}
    facts = {
        "affected_package": package,
        "installed_version": str(finding.get("version") or ""),
        "purl": str(finding.get("purl") or ""),
        "manifest_path": str(finding.get("manifest_path") or ""),
        "severity": str(finding.get("severity") or ""),
        "summary": str(finding.get("name") or "")[:300],
        "repository": repository,
        "finding_url": str(finding.get("url") or ""),
    }
    evidence.update({k: v for k, v in facts.items() if v and v != "unknown"})
    assets = [repository, str(finding.get("repository_id") or ""), str(finding.get("identity_key") or "")]
    evidence["assets"] = list(dict.fromkeys(a for a in assets if a))
    ref = package or repository or f"gitlab finding {external_id}"
    return {"external_id": external_id, "ref": ref, "label": ref, "evidence": evidence}


def gone_ids(stored: set[str], current: set[str], read_projects: set[str]) -> set[str]:
    """Stored finding keys of a project read whole in this run that it no longer lists (pure)."""
    return {key for key in stored - current if key.split(":", 1)[0] in read_projects}


async def _stored_ids(db: AsyncSession, organization_id: uuid.UUID, projects: set[str]) -> set[str]:
    from app.models.ctem_signal import CtemSignal

    if not projects:
        return set()
    rows = await db.execute(
        select(CtemSignal.source_external_id).where(
            CtemSignal.organization_id == organization_id,
            CtemSignal.source_provider == PROVIDER,
            CtemSignal.case_id.is_not(None),
        )
    )
    return {key for key in rows.scalars().all() if key and key.split(":", 1)[0] in projects}


def _publish_after_commit(db: AsyncSession, pending: Any) -> None:
    """Raise the case events and live frames once the caller's commit lands."""
    from sqlalchemy import event

    from app.services import ctem_case_loop

    def _after_commit(_session: Any) -> None:
        try:
            asyncio.get_running_loop().create_task(ctem_case_loop.publish_pending(pending))
        except RuntimeError:  # no loop: the scheduled sweep raises them
            logger.debug("CTEM GitLab finding events left to the sweep")

    event.listen(db.sync_session, "after_commit", _after_commit, once=True)


async def record_findings(
    db: AsyncSession, findings: Mapping[str, list[dict[str, Any]]], *, organization_id: uuid.UUID | None = None
) -> dict[str, int]:
    """Record the GitLab findings of a sync (per project id) and close the gone ones; never raises.

    The caller commits; the case events are raised after that commit.
    """
    from app.core.config import settings

    counts = {"recorded": 0, "closed": 0}
    if settings.is_copilot_like or not findings:
        return counts
    from app.services import ctem_case_loop, ctem_scope_gate_store, ctem_signals
    from app.services.organizations import get_primary

    try:
        if organization_id is None:
            primary = await get_primary(db)
            if primary is None:
                return counts
            organization_id = primary.id
        pending = ctem_case_loop.PendingEvents()
        current: set[str] = set()
        for items in findings.values():
            for finding in items:
                fields = signal_fields(finding)
                if fields is None:
                    continue
                current.add(fields["external_id"])
                async with db.begin_nested():
                    signal, created = await ctem_signals.record_signal(
                        db,
                        organization_id=organization_id,
                        signal=ctem_signals.SignalInput(
                            trigger_path=TRIGGER_PATH,
                            lens=LENS,
                            source_provider=PROVIDER,
                            source_external_id=fields["external_id"],
                            subject_kind=SUBJECT_KIND,
                            subject_ref=fields["ref"],
                            subject_label=fields["label"],
                            evidence=fields["evidence"],
                        ),
                        recorded_via="listener",
                        recorded_by=ctem_signals.SYSTEM_ACTOR,
                    )
                found = await ctem_scope_gate_store.evaluate_after_record(db, signal, created=created)
                pending.items.extend(found.items)
                pending.verticals |= found.verticals
                pending.frames.extend(found.frames)
                counts["recorded"] += int(created)
        projects = set(findings)
        for key in sorted(gone_ids(await _stored_ids(db, organization_id, projects), current, projects)):
            counts["closed"] += await ctem_case_loop.close_signal_members(
                db, organization_id=organization_id, provider=PROVIDER, external_id=key, events=pending
            )
        _publish_after_commit(db, pending)
    except Exception:
        logger.warning("CTEM GitLab dependency findings not recorded", exc_info=True)
    return counts
