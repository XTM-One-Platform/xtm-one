# SPDX-License-Identifier: Apache-2.0

"""Cascade file cleanup — delete FileAttachment records + S3 objects.

Called from entity deletion endpoints to ensure files don't become orphaned.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _filter_conditions(filters: dict[str, object]) -> list | None:
    """The WHERE clauses for *filters*, or ``None`` when they can match no row.

    Column-name -> value pairs; a collection value is an ``IN``, and an empty one
    matches nothing, like no filter at all - a delete never widens to the table.
    """
    from app.models.file_attachment import FileAttachment

    if not filters:
        return None
    conditions = []
    for key, value in filters.items():
        column = getattr(FileAttachment, key)
        if isinstance(value, (list, tuple, set, frozenset)):
            values = list(value)
            if not values:
                return None
            conditions.append(column.in_(values))
        else:
            conditions.append(column == value)
    return conditions


async def delete_file_attachments(db: AsyncSession, **filters: object) -> int:
    """Delete FileAttachment records + their S3 objects for the given filters.

    Args:
        db: Active async session (caller is responsible for commit).
        **filters: Column-name → value pairs to match (e.g.
            ``conversation_id=conv_uuid``).

    Returns:
        Number of file attachments deleted.
    """
    from app.models.file_attachment import FileAttachment
    from app.services.file_storage import file_storage

    conditions = _filter_conditions(filters)
    if conditions is None:
        return 0
    result = await db.execute(select(FileAttachment).where(*conditions))
    attachments = list(result.scalars())

    for fa in attachments:
        try:
            await file_storage.delete(fa.s3_object_name)
        except Exception:
            logger.warning("Failed to delete S3 object %s", fa.s3_object_name)
        await db.delete(fa)

    if attachments:
        logger.info(
            "Cascade-deleted %d file attachment(s) for %s",
            len(attachments),
            filters,
        )
    return len(attachments)


async def delete_file_attachment_rows(db: AsyncSession, **filters: object) -> list[str]:
    """Delete the FileAttachment rows matching *filters*; return their object keys.

    The database half of :func:`delete_file_attachments`, for a caller that
    must not touch the object store inside its transaction: it hands the keys
    to :func:`purge_storage_objects` once that transaction has COMMITTED, so an
    object is never removed from under a row a rollback brings back. The caller
    is responsible for the commit.
    """
    from app.models.file_attachment import FileAttachment

    conditions = _filter_conditions(filters)
    if conditions is None:
        return []
    result = await db.execute(
        delete(FileAttachment)
        .where(*conditions)
        .returning(FileAttachment.s3_object_name)
        .execution_options(synchronize_session=False)
    )
    keys = [key for key in result.scalars().all() if key]
    if keys:
        logger.info("Deleted %d file attachment row(s) for %s", len(keys), filters)
    return keys


async def purge_storage_objects(keys: Iterable[str]) -> int:
    """Remove the storage objects of rows already deleted and committed. Returns objects removed.

    ``file_storage.delete`` answers a refusal from the store (a missing object,
    a denied key) itself; what reaches this function is a store that does not
    answer, where every further delete would wait out its own connect timeout
    and retries. The purge therefore stops at the first such failure and logs
    what is left: unreferenced objects, never a caller - a boot holding the
    bootstrap lock - stalled for one timeout per object.
    """
    from app.services.file_storage import file_storage

    pending = list(keys)
    for index, key in enumerate(pending):
        try:
            await file_storage.delete(key)
        except Exception as exc:
            logger.warning(
                "Object store unreachable while purging deleted files (%s); %d object(s) left "
                "in the bucket, starting with %s",
                type(exc).__name__,
                len(pending) - index,
                key,
            )
            return index
    return len(pending)
