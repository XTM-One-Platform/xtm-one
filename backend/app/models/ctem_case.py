# SPDX-License-Identifier: Apache-2.0

"""CTEM Cases: open exposures grouped by the one remediation that closes them (#11545).

A case is one fix (upgrade a product to a fixed version, apply a course of
action) and the open issues it closes, read from OpenCTI (vulnerabilities on
the organization's software) and OpenAEV (CVE findings on its assets). The
grouping and the state rules live in ``services/ctem_cases.py``; the design is
``dev-docs/ctem-cases.md``.

- ``ctem_cases``: the case, its fix (kind, title, product, fixed version),
  its status (new, analyzing, waiting for decision, remediating, validating,
  mitigated watching, resolved, accepted risk; #1503), owner,
  due date, the accepted-risk reason and expiry. Shared by the organization.
- ``ctem_case_issues``: one row per member issue, its source id and whether it
  is still open; ``open`` false once a source read confirmed it closed.
- ``ctem_case_events``: the workflow history; an event about one issue names
  its member id only.

No source content is stored (#11613): the name, CVE, scores, KEV, assets,
primary assets, priority and counts of a case are read at display time from the
viewer's own source read (``services/ctem_cases_view.py``), so each viewer
sees exactly what the source products show them, and an issue they cannot see
is a "Restricted" row.

One exception, bounded (#936): ``exploitation`` keeps the OpenCRQ reading the
triage step wrote (techniques, exposure class, reading, and the OpenCRQ asset
name and scenario id). The view serves the asset name and scenario id only to
a viewer who reads OpenCRQ; anyone else gets the techniques and a
"Restricted" asset. ``attack_path`` (#1289) is bounded the same way: the
attack-path reach of the affected asset, its asset names shown only to a
viewer who reads OpenCRQ.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: The statuses of a case, in lifecycle (board) order (#1503). The loop moves a case through
#: them itself (``services/ctem_case_status.py``): ``new`` (opened, triage not started),
#: ``analyzing`` (agents scope, enrich, prioritize), ``waiting_decision`` (a gate waits on a
#: person), ``remediating`` (fix decided, ticket or change in flight), ``validating`` (re-test
#: pending). ``mitigated_watching``: the exposure is mitigated and proven so, the permanent fix
#: is not in yet; the re-test stays on (#857).
CASE_STATES: tuple[str, ...] = (
    "new",
    "analyzing",
    "waiting_decision",
    "remediating",
    "validating",
    "mitigated_watching",
    "resolved",
    "accepted_risk",
)
#: The statuses a case is still worked in (an SLA runs, a new signal joins it).
OPEN_STATES: tuple[str, ...] = ("new", "analyzing", "waiting_decision", "remediating", "validating")
#: The statuses a case is closed in (a re-test or a source read may reopen it).
CLOSED_STATES: tuple[str, ...] = ("mitigated_watching", "resolved", "accepted_risk")
#: The two statuses of the first case model, kept only to read old history events.
LEGACY_STATES: dict[str, str] = {"needs_attention": "new", "in_progress": "remediating"}
#: What the fix is. The last six are the fixes of the paths that carry no CVE (#857).
REMEDIATION_KINDS: tuple[str, ...] = (
    "upgrade",
    "patch",
    "config",
    "course_of_action",
    "manual",
    "close_exposure",
    "decommission",
    "rotate_credential",
    "restore_control",
    "supplier_action",
    "restore_coverage",
)
CASE_PRIORITIES: tuple[str, ...] = ("critical", "high", "medium", "low")
#: ``signal``: a CTEM signal attached to the case (``external_id`` is its id).
ISSUE_SOURCES: tuple[str, ...] = ("opencti", "openaev", "signal")
#: How a resolved case was closed.
RESOLUTION_KINDS: tuple[str, ...] = ("not_exposed", "mitigated", "fixed", "accepted_risk")
#: Severity from the scope gate (P1 most urgent).
CASE_SEVERITIES: tuple[str, ...] = ("P1", "P2", "P3")
#: What a validation reference says.
VALIDATION_RESULTS: tuple[str, ...] = ("passed", "not_exposed", "failed", "pending")
#: The stored result of a pass that proves nothing (``conclusive`` false, #1458): never shown or counted as passed.
INCONCLUSIVE_RESULT = "inconclusive"


def _now() -> datetime:
    return datetime.now(UTC)


class CtemCase(Base):
    __tablename__ = "ctem_cases"
    __table_args__ = (
        CheckConstraint(
            "state IN ('new', 'analyzing', 'waiting_decision', 'remediating', 'validating',"
            " 'mitigated_watching', 'resolved', 'accepted_risk')",
            name="ck_ctem_cases_state_valid",
        ),
        CheckConstraint(
            "state <> 'accepted_risk' OR (owner_user_id IS NOT NULL"
            " AND accepted_reason IS NOT NULL AND accepted_until IS NOT NULL)",
            name="ck_ctem_cases_accepted_risk_complete",
        ),
        Index("ix_ctem_cases_org_vertical_state", "organization_id", "vertical", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    vertical: Mapped[str] = mapped_column(String(20), nullable=False, default="cyber")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    remediation_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    product_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: ``None`` when the data names the product but no fixed version.
    fixed_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    #: ``{platform: version}`` when the remediation names the fix of each platform (#1572), else ``{}``:
    #: each asset is then compared with the fix of its own platform (``ctem_case_versions``).
    fixed_versions: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    #: Every remediation key mapped to this case (several after a manual merge).
    keys: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    #: ``source:external_id`` of the issues a manual split or a manual case owns.
    pinned_issues: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="new")
    state_since: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    accepted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # -- The loop (#857): where the case came from, how urgent, how it is proven.
    #: The trigger path of the signal that opened it (``new_vulnerability`` for a reader case).
    trigger_path: Mapped[str] = mapped_column(String(30), nullable=False, default="new_vulnerability")
    #: OpenCRQ risk scenario ids the scope gate found holding the subject.
    risk_scenario_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    severity: Mapped[str | None] = mapped_column(String(2), nullable=True)
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Set once, when the SLA was first found breached (the escalation is raised once).
    sla_breached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: ``[{integration, key, url, status, at}]``.
    tickets: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    #: ``[{provider, kind, ref, result, at, mitigation_only, note}]``, newest last; one entry per
    #: ``(provider, ref)`` while it is pending or repeats its outcome; ``result`` ``inconclusive``
    #: (with ``reported_result``) for a pass that proves nothing.
    validations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    #: True: the case resolves only on a passing validation reference.
    validation_plan: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resolution_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #: How the CVE would be exploited on the scenario's assets (CTEM-24, #936), written by the
    #: triage step from OpenCRQ: ``[{risk_scenario_id, asset, exposure, techniques, reading, at}]``,
    #: one entry per supporting asset, replaced whole by each write.
    exploitation: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    #: What a finding on the affected asset reaches along the OpenAEV attack paths (CTEM-23, #1289),
    #: written by the triage step from OpenCRQ: ``{origin, hops, reached, primary, at}``, replaced
    #: whole by each write; ``{}`` until the first read.
    attack_path: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class CtemCaseIssue(Base):
    __tablename__ = "ctem_case_issues"
    __table_args__ = (
        UniqueConstraint("case_id", "source", "external_id", name="uq_ctem_case_issues_member"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ctem_cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CtemCaseEvent(Base):
    __tablename__ = "ctem_case_events"
    # The merged-away case ids (#2289): a read of a merged id finds the survivor by
    # one probe of the ``merge`` rows, by full id or by an 8+ character prefix.
    __table_args__ = (
        Index(
            "ix_ctem_case_events_merged_from",
            "from_value",
            postgresql_where=text("kind = 'merge'"),
            postgresql_ops={"from_value": "varchar_pattern_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ctem_cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    #: A user id, an agent id, or ``system``.
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    #: created | state | owner | due | issue_added | issue_closed | issue_reopened | merge | split | note
    #: | retargeted (a folded upgrade target moved, CTEM-11)
    #: | signal | validation | ticket | sla_breached (#857)
    #: | exploitation (the OpenCRQ reading written by triage, #936)
    #: | attack_path (the OpenCRQ attack-path reach written by triage, #1289)
    #: | gate (a loop gate passed automatically, waited or answered, CTEM-34 #916)
    #: | treatment_plan (the OpenCRQ plan of the decision: to the id, reason the name, evidence the link, #1068)
    #: | versions (the per-device running versions moved: to the summary, reason the lines, evidence the snapshot, #1694)
    #: | stalled (the case waited on nobody past the stall window: to ``nobody``, reason since when, #1736)
    #: | applied_change (a fix applied through an integration under ctem.production_change: from the
    #:   integration, to the kind, reason what was pushed, evidence ``{ids, rollback}`` as JSON, #2061)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    #: The member id (``source:external_id``) an issue event is about, else ``None``.
    issue: Mapped[str | None] = mapped_column(String(220), nullable=True)
    from_value: Mapped[str | None] = mapped_column(String(300), nullable=True)
    to_value: Mapped[str | None] = mapped_column(String(300), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
