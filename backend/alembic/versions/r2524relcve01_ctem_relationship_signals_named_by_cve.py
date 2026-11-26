"""name the CTEM signals of an OpenCTI relationship by their CVE, not the vulnerability's STIX id (#2524)

A signal recorded from an OpenCTI ``has`` relationship or a sighting of a
vulnerability carried the target's STIX id (``vulnerability--<uuid>``) as
``subject_ref`` and no label. Each such row whose evidence names a CVE (its
``cve``, or the event ``message``, ``[relationship] <software> has CVE-...``)
gets that CVE as ``subject_ref`` and ``subject_label``, and the STIX id it
replaces is kept in ``evidence.target_ref`` (what the listener now writes), so
the downgrade restores it. A row with no CVE found stays as it is and is
counted in the log. State, decision, seen_count, case and dedupe key stay.

Revision ID: r2524relcve01
Revises: w2506webhooksrc01
Create Date: 2026-11-26 16:30:00.000000
"""

import json
import logging
import re
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "r2524relcve01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "w2506webhooksrc01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.r2524relcve01")

_BATCH = 500
_STIX_LIKE = "vulnerability--%"
_CVE = re.compile(r"\bCVE-\d{4}-\d{4,}\b", re.IGNORECASE)


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def cve_of_evidence(evidence: Any) -> str:
    """The CVE id a signal's evidence names (``cve``, ``cves``, then ``message``), upper case, ``""`` when none."""
    evidence = _json(evidence)
    for key in ("cve", "cves", "message"):
        value = evidence.get(key)
        texts = value if isinstance(value, list) else [value]
        for text in texts:
            found = _CVE.search(text) if isinstance(text, str) else None
            if found:
                return found.group(0).upper()
    return ""


def rename_in_batches(conn: sa.engine.Connection, batch_size: int = _BATCH) -> tuple[int, int]:
    """Every ``vulnerability--`` named signal renamed to its CVE, in ``id`` pages: (renamed, left as is)."""
    if not sa.inspect(conn).has_table("ctem_signals"):
        return 0, 0
    head = "SELECT id, subject_ref, evidence FROM ctem_signals WHERE subject_ref LIKE :stix"
    update = sa.text(
        "UPDATE ctem_signals SET subject_ref = :cve, subject_label = :cve, evidence = CAST(:evidence AS json) "
        "WHERE id = :id"
    )
    renamed = left = 0
    after: Any = None
    while True:
        page = head + (" AND id > :after" if after is not None else "") + " ORDER BY id LIMIT :limit"
        params: dict[str, Any] = {"limit": batch_size, "stix": _STIX_LIKE}
        if after is not None:
            params["after"] = after
        rows = conn.execute(sa.text(page), params).all()
        if not rows:
            return renamed, left
        for row_id, subject_ref, evidence in rows:
            evidence = _json(evidence)
            cve = cve_of_evidence(evidence)
            if cve:
                kept = {**evidence, "target_ref": evidence.get("target_ref") or subject_ref}
                conn.execute(update, {"cve": cve, "evidence": json.dumps(kept), "id": row_id})
                renamed += 1
            else:
                left += 1
        after = rows[-1][0]


def upgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    renamed, left = rename_in_batches(conn)
    logger.info("#2524: %d signal(s) renamed to their CVE, %d left named by STIX id (no CVE found)", renamed, left)


def restore_in_batches(conn: sa.engine.Connection, batch_size: int = _BATCH) -> int:
    """Every relationship or sighting signal named by its CVE back to its target's STIX id, no label."""
    if not sa.inspect(conn).has_table("ctem_signals"):
        return 0
    head = "SELECT id, subject_ref, evidence FROM ctem_signals WHERE subject_ref NOT LIKE :stix"
    head += " AND evidence::text LIKE :marker"
    update = sa.text("UPDATE ctem_signals SET subject_ref = :ref, subject_label = NULL WHERE id = :id")
    restored = 0
    after: Any = None
    while True:
        page = head + (" AND id > :after" if after is not None else "") + " ORDER BY id LIMIT :limit"
        params: dict[str, Any] = {"limit": batch_size, "stix": _STIX_LIKE, "marker": '%"target_ref"%'}
        if after is not None:
            params["after"] = after
        rows = conn.execute(sa.text(page), params).all()
        if not rows:
            return restored
        for row_id, _subject_ref, evidence in rows:
            target = _json(evidence).get("target_ref")
            if isinstance(target, str) and target.startswith("vulnerability--"):
                conn.execute(update, {"ref": target, "id": row_id})
                restored += 1
        after = rows[-1][0]


def downgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    restored = restore_in_batches(conn)
    logger.info("#2524 downgrade: %d signal(s) named by their target's STIX id again", restored)
