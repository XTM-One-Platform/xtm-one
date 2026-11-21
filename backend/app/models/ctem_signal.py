# SPDX-License-Identifier: Apache-2.0

"""CTEM signals: one intake for every trigger path of the CTEM loop (#826).

A signal is a change in exposure seen through one lens: a new vulnerability on
software the organization runs, a relevant threat, a code flaw, a proven
attack path, a new exposure, a stale asset, drift, a regression, a decayed
control, a leaked credential, a third-party event or a blind spot. It is the
first box of the loop (signal, reconcile, scope gate, case); the design is
``dev-docs/ctem-signals.md``.

- One row per ``(organization_id, recorded_by, dedupe_key)``: the same
  recorder recording the same signal again moves ``last_seen_at`` and counts
  it, never a second row. Another recorder's key is never reached (its row is
  neither returned nor updated); the reconcile step merges what several
  recorders saw.
- ``state`` starts at ``new``; the reconcile and scope gate step writes
  ``confirmed``, ``to_validate``, ``discarded`` or ``watchlist`` with its
  ``decision`` record, and the case step ``case_opened`` with ``case_id``.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: The 12 trigger paths, in the order of the CTEM trigger model (path 1 first).
TRIGGER_PATHS: tuple[str, ...] = (
    "new_vulnerability",
    "relevant_threat",
    "code_flaw",
    "proven_exploitable",
    "new_exposure",
    "stale_asset",
    "surface_drift",
    "mitigation_regression",
    "control_decay",
    "credential_leak",
    "third_party_event",
    "blind_spot",
)
#: The three points of view a signal is seen through.
LENSES: tuple[str, ...] = ("inside", "outside", "world")
#: What a signal is about.
SUBJECT_KINDS: tuple[str, ...] = ("asset", "software", "identity", "third_party", "vulnerability", "threat", "code")
#: The signal's place in the loop.
SIGNAL_STATES: tuple[str, ...] = ("new", "confirmed", "to_validate", "discarded", "watchlist", "case_opened")
#: How it came in.
INTAKES: tuple[str, ...] = ("api", "tool", "listener")


def _now() -> datetime:
    return datetime.now(UTC)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class CtemSignal(Base):
    __tablename__ = "ctem_signals"
    __table_args__ = (
        UniqueConstraint("organization_id", "recorded_by", "dedupe_key", name="uq_ctem_signals_org_recorder_dedupe"),
        CheckConstraint(_in("trigger_path", TRIGGER_PATHS), name="ck_ctem_signals_path_valid"),
        CheckConstraint(_in("lens", LENSES), name="ck_ctem_signals_lens_valid"),
        CheckConstraint(_in("state", SIGNAL_STATES), name="ck_ctem_signals_state_valid"),
        CheckConstraint("state <> 'case_opened' OR case_id IS NOT NULL", name="ck_ctem_signals_case_opened_has_case"),
        Index("ix_ctem_signals_org_state", "organization_id", "state"),
        Index("ix_ctem_signals_org_path", "organization_id", "trigger_path"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    trigger_path: Mapped[str] = mapped_column(String(30), nullable=False)
    lens: Mapped[str] = mapped_column(String(10), nullable=False)
    #: The product or integration that saw it (``opencti``, ``openaev``, ``aikido``, ``manual``...).
    source_provider: Mapped[str] = mapped_column(String(40), nullable=False)
    #: The object's id in that product, when it has one.
    source_external_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    subject_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    #: A stable reference: hostname, IP, URL, CPE, CVE id, account, organization name...
    subject_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    subject_label: Mapped[str | None] = mapped_column(String(300), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    seen_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: What was seen, with a ``link`` to it in its product when there is one.
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="new")
    #: The reconcile and scope gate decision: its inputs, outcome and reason.
    decision: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    case_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ctem_cases.id", ondelete="SET NULL"), nullable=True
    )
    #: ``api``, ``tool`` or ``listener``.
    recorded_via: Mapped[str] = mapped_column(String(10), nullable=False)
    #: The user the signal was recorded for, or ``system`` for a listener.
    recorded_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
