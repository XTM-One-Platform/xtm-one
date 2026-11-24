"""the profile's tech stack rows become hand-typed software (#1495, slice 3b)

Each row of the current profile version's ``tech_stack`` of every
organization becomes a Software of ``origin`` ``manual`` (vendor, product,
the display name, its version when typed), unless a software with the same
vendor and product already exists. Nothing is lost:

- a CPE typed by hand (source ``manual``, ``csv`` or ``api``) is the
  administrator's override: kept as ``cpe_override`` on the new software, or
  set on the existing one when it carries none (a synced software included:
  the next sync resolves its CPE from the override, as before);
- a CPE another source reported stays the software's reported CPE;
- a row of kind ``saas`` or ``cloud_service`` becomes a Software of type
  ``saas``, every other row ``application`` (the administrator reclassifies).

The stored organization-context category selections (platform settings,
agents, assignments) naming ``tech_stack`` name ``software``, the category
that replaces it (``tech_stack`` stays a read alias for one release).

The profile versions are history and are not touched, so the downgrade
loses nothing the upgrade did not add: it deletes the hand-typed software
matching a migrated row that no installation links, and clears the overrides
it set on synced software.

Data only, no DDL: rows are inserted and updated under the lock timeout,
organization by organization. The plan is computed here, not by the
services, so the revision keeps meaning what it meant on the day it ran.

Revision ID: s1495swprofile01
Revises: u1595nopastel01
Create Date: 2026-11-24 00:20:00.000000
"""

import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "s1495swprofile01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "u1595nopastel01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Sources whose typed CPE is the administrator's own (``organization_context.HAND_SOURCES`` that day).
_HAND_SOURCES = frozenset({"manual", "csv", "api"})
#: Tech stack kinds that are a SaaS product rather than installed software.
_SAAS_KINDS = frozenset({"saas", "cloud_service"})


def _key(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _cut(value: Any, size: int) -> str:
    return str(value or "").strip()[:size]


def _display_name(vendor: str, product: str) -> str:
    if not vendor or _key(product).startswith(_key(vendor)):
        return product
    return f"{vendor} {product}"


def plan(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """The software the ``tech_stack`` *rows* become, one per vendor and product, the first row winning.

    A later row of the same software only adds a hand CPE the first one lacked.
    """
    planned: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        product = _cut(row.get("product"), 200)
        if not product:
            continue
        vendor = _cut(row.get("vendor"), 200)
        key = (_key(vendor)[:200], _key(product)[:200])
        source = _cut(row.get("source"), 80) or "manual"
        cpe = _cut(row.get("cpe"), 300)
        override = cpe if cpe and source in _HAND_SOURCES else ""
        found = planned.get(key)
        if found is not None:
            if override and not found["cpe_override"]:
                found["cpe_override"] = override
            continue
        planned[key] = {
            "vendor": vendor,
            "product": product,
            "vendor_key": key[0],
            "product_key": key[1],
            "name": _display_name(vendor, product)[:300],
            "software_type": "saas" if str(row.get("kind") or "") in _SAAS_KINDS else "application",
            "cpe_override": override,
            "reported_cpe": "" if override else cpe,
            "source": source,
            "version": _cut(row.get("version"), 100),
        }
    return list(planned.values())


def _cpe(item: Mapping[str, Any]) -> tuple[str, str]:
    """The CPE and its state of a new software: the override wins, then a reported one."""
    if item["cpe_override"]:
        return item["cpe_override"], "override"
    if item["reported_cpe"]:
        return item["reported_cpe"], "reported"
    return "", "unresolved"


def _current_profiles(conn: sa.engine.Connection) -> list[tuple[uuid.UUID, Any]]:
    return [
        (row[0], row[1])
        for row in conn.execute(
            sa.text(
                "SELECT DISTINCT ON (organization_id) organization_id, profile "
                "FROM organization_context_versions WHERE organization_id IS NOT NULL "
                "ORDER BY organization_id, version DESC"
            )
        ).all()
    ]


def _tech_stack(profile: Any) -> list[Any]:
    if isinstance(profile, str):  # a driver that answers JSONB as text
        profile = json.loads(profile or "{}")
    rows = (profile or {}).get("tech_stack") if isinstance(profile, Mapping) else None
    return rows if isinstance(rows, list) else []


#: Tables whose ``organization_context_categories`` JSON list names categories.
_SELECTION_TABLES = ("platform_settings", "agents", "assignments")


def _rename_selection(conn: sa.engine.Connection, old: str, new: str) -> None:
    """Every stored category selection naming *old* names *new*, in place, duplicates dropped."""
    inspector = sa.inspect(conn)
    for table in _SELECTION_TABLES:
        if not inspector.has_table(table):
            continue
        if "organization_context_categories" not in {c["name"] for c in inspector.get_columns(table)}:
            continue
        rows = conn.execute(
            sa.text(
                f"SELECT id, organization_context_categories FROM {table} "  # noqa: S608 - a constant table name
                "WHERE organization_context_categories::text LIKE :needle"
            ),
            {"needle": f'%"{old}"%'},
        ).all()
        for row_id, value in rows:
            names = json.loads(value) if isinstance(value, str) else value
            if not isinstance(names, list):
                continue
            renamed = list(dict.fromkeys(new if name == old else name for name in names))
            conn.execute(
                sa.text(
                    f"UPDATE {table} SET organization_context_categories = CAST(:value AS json) "  # noqa: S608
                    "WHERE id = :id"
                ),
                {"value": json.dumps(renamed), "id": row_id},
            )


def _has_tables(conn: sa.engine.Connection) -> bool:
    inspector = sa.inspect(conn)
    return all(
        inspector.has_table(name)
        for name in ("organization_context_versions", "organization_software", "organization_software_versions")
    )


def upgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    _rename_selection(conn, "tech_stack", "software")
    if not _has_tables(conn):
        return
    for organization_id, profile in _current_profiles(conn):
        items = plan(_tech_stack(profile))
        if not items:
            continue
        existing = {
            (row[1], row[2]): (row[0], row[3])
            for row in conn.execute(
                sa.text(
                    "SELECT id, vendor_key, product_key, cpe_override FROM organization_software "
                    "WHERE organization_id = :org"
                ),
                {"org": organization_id},
            ).all()
        }
        for item in items:
            found = existing.get((item["vendor_key"], item["product_key"]))
            if found is not None:
                software_id, override = found
                if item["cpe_override"] and not override:
                    conn.execute(
                        sa.text(
                            "UPDATE organization_software SET cpe_override = :cpe, cpe = :cpe, "
                            "cpe_state = 'override', updated_at = now() WHERE id = :id"
                        ),
                        {"cpe": item["cpe_override"], "id": software_id},
                    )
                continue
            software_id = uuid.uuid4()
            cpe, state = _cpe(item)
            conn.execute(
                sa.text(
                    "INSERT INTO organization_software (id, organization_id, vendor, product, vendor_key, "
                    "product_key, name, name_key, software_type, description, cpe, cpe_state, cpe_override, "
                    "purl, origin, sources, created_at, updated_at) VALUES (:id, :org, :vendor, :product, "
                    ":vendor_key, :product_key, :name, :name_key, :software_type, '', :cpe, :state, :override, "
                    "'', 'manual', CAST(:sources AS jsonb), now(), now())"
                ),
                {
                    "id": software_id,
                    "org": organization_id,
                    "vendor": item["vendor"],
                    "product": item["product"],
                    "vendor_key": item["vendor_key"],
                    "product_key": item["product_key"],
                    "name": item["name"],
                    "name_key": _key(item["name"])[:300],
                    "software_type": item["software_type"],
                    "cpe": cpe,
                    "state": state,
                    "override": item["cpe_override"],
                    "sources": json.dumps(sorted({"manual", item["source"]})),
                },
            )
            if item["version"]:
                conn.execute(
                    sa.text(
                        "INSERT INTO organization_software_versions (id, organization_id, software_id, version, "
                        "cpe, purl, eol) VALUES (:id, :org, :software, :version, '', '', '')"
                    ),
                    {"id": uuid.uuid4(), "org": organization_id, "software": software_id, "version": item["version"]},
                )
            existing[(item["vendor_key"], item["product_key"])] = (software_id, item["cpe_override"])


def downgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    _rename_selection(conn, "software", "tech_stack")
    if not _has_tables(conn):
        return
    for organization_id, profile in _current_profiles(conn):
        for item in plan(_tech_stack(profile)):
            keys = {"org": organization_id, "vk": item["vendor_key"], "pk": item["product_key"]}
            # A hand-typed software no installation links goes (an attached one is an administrator's since).
            conn.execute(
                sa.text(
                    "DELETE FROM organization_software s WHERE s.organization_id = :org AND s.vendor_key = :vk "
                    "AND s.product_key = :pk AND s.origin = 'manual' AND NOT EXISTS (SELECT 1 FROM "
                    "organization_asset_software i JOIN organization_software_versions v "
                    "ON v.id = i.software_version_id WHERE v.software_id = s.id)"
                ),
                keys,
            )
            if item["cpe_override"]:
                conn.execute(
                    sa.text(
                        "UPDATE organization_software SET cpe_override = '', "
                        "cpe = CASE WHEN cpe_state = 'override' THEN '' ELSE cpe END, "
                        "cpe_state = CASE WHEN cpe_state = 'override' THEN 'unresolved' ELSE cpe_state END, "
                        "updated_at = now() WHERE organization_id = :org AND vendor_key = :vk AND product_key = :pk "
                        "AND origin = 'synced' AND cpe_override = :cpe"
                    ),
                    {**keys, "cpe": item["cpe_override"]},
                )
