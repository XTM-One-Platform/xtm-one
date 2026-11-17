# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""``query_platform_objects``: the Admin Objects union, filtered, counted and bucketed.

The rows are the ones ``GET /admin/objects`` lists (``admin_objects.ENTITY_MAP``
through ``objects_union``: the arms this deployment offers), with what that
endpoint lacks: creation and update windows, an owner by id or email, a group
(objects owned by its members today), the company-managed flag, counts per
type, and counts per day / week / month / deployed build of their creation.
Outside the administrator scope the rows are the caller's own objects.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import String, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.group import GroupMember
from app.models.user import User
from app.services.platform_builds import history_note, release_case, release_order, release_windows
from app.services.platform_insights.scope import (
    InsightRefusedError,
    InsightScope,
    as_list,
    parse_instant,
    resolve_groups,
    resolve_users,
)

BUCKETS: tuple[str, ...] = ("day", "week", "month", "release")
MAX_LIMIT = 100
MAX_BUCKET_ROWS = 1000


def _bucket_expression(column, bucket: str, windows: list):
    if bucket == "release":
        return release_case(column, windows)
    utc = func.timezone(literal_column("'UTC'"), column)
    pattern = "YYYY-MM" if bucket == "month" else "YYYY-MM-DD"
    return func.to_char(func.date_trunc(bucket, utc), pattern)


async def query_platform_objects(db: AsyncSession, scope: InsightScope, args: dict[str, Any]) -> dict[str, Any]:
    from app.api.v1.admin_objects import DETAIL_COLUMNS, active_entity_arms, objects_union

    arms = active_entity_arms()
    requested = as_list(args.get("object_types"))
    unknown = [value for value in requested if value not in arms]
    if unknown:
        raise InsightRefusedError(
            f"Error: unknown object type(s) {', '.join(map(repr, unknown))}. "
            f"Types on this platform: {', '.join(sorted(arms))}."
        )
    selected = [arms[key] for key in requested] if requested else list(arms.values())
    bucket = (str(args.get("bucket") or "").strip().lower()) or None
    if bucket is not None and bucket not in BUCKETS:
        raise InsightRefusedError(f"Error: bucket must be one of {', '.join(BUCKETS)}.")
    created_from = parse_instant(args.get("created_from"), label="created_from")
    created_to = parse_instant(args.get("created_to"), label="created_to")
    updated_from = parse_instant(args.get("updated_from"), label="updated_from")

    now = datetime.now(UTC)
    sub = objects_union(selected, now=now)
    clauses = []
    owners = await resolve_users(db, args.get("owner"), scope)
    if owners:
        clauses.append(sub.c.user_id.in_([owner.id for owner in owners]))
    if not scope.platform:
        clauses.append(sub.c.user_id == str(scope.user.id))
    groups = await resolve_groups(db, args.get("group_id"), scope)
    if groups:
        members = select(GroupMember.user_id.cast(String)).where(
            GroupMember.group_id.in_([uuid.UUID(group.id) for group in groups])
        )
        clauses.append(sub.c.user_id.in_(members))
    managed = args.get("is_company_managed")
    if isinstance(managed, bool):
        clauses.append(sub.c.is_company_managed.is_(managed))
    if created_from:
        clauses.append(sub.c.created_at >= created_from)
    if created_to:
        clauses.append(sub.c.created_at < created_to)
    if updated_from:
        clauses.append(sub.c.updated_at >= updated_from)
    search = str(args.get("search") or "").strip()
    if search:
        needle = search.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append(func.lower(sub.c.name).like(f"%{needle}%", escape="\\"))

    result: dict[str, Any] = {
        **scope.header(),
        "object_types": [arm.label for arm in selected],
        "filters": {
            "owner": [owner.email for owner in owners],
            "groups": [group.name for group in groups],
            "is_company_managed": managed if isinstance(managed, bool) else None,
            "created_from": created_from.isoformat() if created_from else None,
            "created_to": created_to.isoformat() if created_to else None,
            "updated_from": updated_from.isoformat() if updated_from else None,
            "search": search or None,
        },
        "caveats": [],
    }
    if groups:
        result["caveats"].append(
            "A group filter reads the group's members TODAY: objects of people who left it are not counted."
        )

    per_type = select(sub.c.object_type, func.count().label("count")).where(*clauses).group_by(sub.c.object_type)
    counts = {row.object_type: int(row.count) for row in (await db.execute(per_type)).all()}
    result["counts_by_type"] = {arm.label: counts.get(arm.label, 0) for arm in selected}
    result["total"] = sum(counts.values())

    if bucket is not None:
        windows = await release_windows(db) if bucket == "release" else []
        expression = _bucket_expression(sub.c.created_at, bucket, windows).label("bucket")
        statement = (
            select(expression, sub.c.object_type, func.count().label("count"))
            .where(*clauses)
            .group_by(literal_column("bucket"), sub.c.object_type)
            .order_by(literal_column("bucket"), sub.c.object_type)
            .limit(MAX_BUCKET_ROWS + 1)
        )
        rows = [
            {"bucket": row.bucket, "dimension": row.object_type, "value": int(row.count)}
            for row in (await db.execute(statement)).all()
        ]
        if bucket == "release":
            rows.sort(key=lambda row: (release_order(row["bucket"], windows), row["dimension"]))
            result["release_history"] = [window.payload() for window in windows]
            result["caveats"].append(history_note(windows, created_from))
        result.update(
            bucket=bucket,
            bucket_of="created_at",
            rows=rows[:MAX_BUCKET_ROWS],
            truncated=len(rows) > MAX_BUCKET_ROWS,
        )
        if bucket in {"week", "month"} and created_from:
            result["caveats"].append(f"The first and last {bucket} cover only the part of it inside the window.")
        return result

    if args.get("count_only"):
        result["rows"] = [
            {"bucket": None, "dimension": label, "value": count} for label, count in result["counts_by_type"].items()
        ]
        return result

    limit = max(1, min(int(args.get("limit") or 25), MAX_LIMIT))
    offset = max(0, int(args.get("offset") or 0))
    owners_alias = select(User.id.cast(String).label("uid"), User.email.label("owner_email")).subquery("owners")
    page = (
        select(
            sub.c.id,
            sub.c.name,
            sub.c.object_type,
            sub.c.user_id,
            owners_alias.c.owner_email,
            sub.c.is_company_managed,
            sub.c.created_at,
            sub.c.updated_at,
            *(sub.c[column] for column in DETAIL_COLUMNS),
        )
        .outerjoin(owners_alias, sub.c.user_id == owners_alias.c.uid)
        .where(*clauses)
        .order_by(sub.c.created_at.desc(), sub.c.object_type, sub.c.id)
        .offset(offset)
        .limit(limit)
    )
    items = []
    for row in (await db.execute(page)).all():
        item = {
            "id": row.id,
            "name": row.name,
            "object_type": row.object_type,
            "owner_id": row.user_id,
            "owner_email": row.owner_email,
            "is_company_managed": bool(row.is_company_managed),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for column in DETAIL_COLUMNS:
            value = getattr(row, column)
            if value is not None:
                item[column] = value.isoformat() if isinstance(value, datetime) else value
        items.append(item)
    result.update(
        items=items,
        offset=offset,
        next_offset=offset + len(items) if offset + len(items) < result["total"] else None,
    )
    return result
