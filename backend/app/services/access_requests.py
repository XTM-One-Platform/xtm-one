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

"""Requests made from the card of a pasted XTM One link.

``dev-docs/teams-link-unfurl.md``, section 9. For now one kind: an **action
request** on an assignment run, from a viewer who cannot answer it, to the
people who can - its owner for a private run, the administrators for a
company-managed run or one whose owner is deactivated. It grants nothing:
runs are never shared (decision D7). The recipients get a notification in the
bell, and the open requests are listed on the run page until the run is
answered or a recipient dismisses them.

Nothing here tells the requester anything about the run: whatever the run's
state, or whether it exists, the card says the same thing (FR-17). A request
is only recorded, and only notifies, for a run that waits for a person.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.access_request import (
    ENTITY_ASSIGNMENT_RUN,
    KIND_ACTION,
    STATUS_CLOSED,
    STATUS_OPEN,
    AccessRequest,
)
from app.models.assignment import Assignment, AssignmentRun
from app.models.user import User

logger = logging.getLogger(__name__)

#: What the bell keys the notification on, to open the run it names.
NOTIFICATION_SOURCE = "run_action_request"
#: Action requests one person may make per hour, across every run.
REQUESTS_PER_HOUR = 20


async def run_action_recipients(db: AsyncSession, run: AssignmentRun) -> list[User]:
    """The active users who may answer *run*: who an action request on it goes to.

    ``require_run_access`` as a set: an administrator answers any run, the
    owner their private run. A private run whose owner is gone or deactivated
    goes to the administrators, who are the only ones left who can answer it.
    """
    if not run.is_company_managed and run.user_id is not None:
        owner = await db.get(User, run.user_id)
        if owner is not None and owner.is_active:
            return [owner]
    admins = await db.execute(select(User).where(User.role == "admin", User.is_active.is_(True)))
    return list(admins.scalars())


async def request_run_action(db: AsyncSession, run_id: uuid.UUID, requester: User, *, source: str) -> None:
    """Ask whoever may answer *run_id* to act on it, on behalf of *requester*.

    Says nothing back on purpose: a missing run, a run that waits for nobody,
    one the requester could answer themselves and a request already open are
    all silent, so the card answers every click the same way. Commits.
    """
    from app.api.v1.helpers import require_run_access
    from app.core.rate_limit import enforce_rate_limit
    from app.services.notify import notify_web_user

    await enforce_rate_limit(
        None, "access_request", limit=REQUESTS_PER_HOUR, window_seconds=3600, bucket=str(requester.id), quiet=True
    )
    run = await db.get(AssignmentRun, run_id)
    if run is None or run.status != "awaiting_human":
        return
    try:
        require_run_access(run, requester, action="respond to")
        return  # They can answer it themselves.
    except HTTPException:
        pass
    recipients = [user for user in await run_action_recipients(db, run) if user.id != requester.id]
    if not recipients:
        return

    request = AccessRequest(
        kind=KIND_ACTION,
        entity_type=ENTITY_ASSIGNMENT_RUN,
        entity_id=run.id,
        requester_id=requester.id,
        status=STATUS_OPEN,
        source=source,
    )
    try:
        async with db.begin_nested():
            db.add(request)
    except IntegrityError:
        return  # One is already open: the recipients were told then.

    assignment_name = (
        await db.scalar(select(Assignment.name).where(Assignment.id == run.assignment_id))
        if run.assignment_id
        else None
    )
    requester_name = requester.full_name or requester.email
    for recipient in recipients:
        await notify_web_user(
            db,
            str(recipient.id),
            title=f"{requester_name} asks you to act on a run",
            summary=assignment_name or "Assignment run",
            extra_data={
                "notification_source": NOTIFICATION_SOURCE,
                "run_id": str(run.id),
                "request_id": str(request.id),
            },
        )
    await db.commit()
    logger.info("Action request %s on run %s sent to %d recipient(s)", request.id, run.id, len(recipients))


@dataclass(frozen=True)
class OpenRequest:
    id: uuid.UUID
    requester_name: str
    source: str
    created_at: datetime


async def open_run_action_requests(db: AsyncSession, run_id: uuid.UUID) -> list[OpenRequest]:
    """The open action requests on *run_id*, oldest first."""
    rows = await db.execute(
        select(AccessRequest, User.full_name, User.email)
        .join(User, User.id == AccessRequest.requester_id)
        .where(
            AccessRequest.entity_type == ENTITY_ASSIGNMENT_RUN,
            AccessRequest.entity_id == run_id,
            AccessRequest.status == STATUS_OPEN,
        )
        .order_by(AccessRequest.created_at)
    )
    return [
        OpenRequest(id=request.id, requester_name=full_name or email, source=request.source, created_at=request.created_at)
        for request, full_name, email in rows
    ]


async def close_run_action_requests(
    db: AsyncSession,
    run_id: uuid.UUID,
    *,
    request_id: uuid.UUID | None = None,
    closed_by: User | None = None,
) -> int:
    """Close the open action requests on *run_id* (only *request_id* when given); how many.

    Called when the run is answered or dismissed - nothing is left to ask for -
    and when a recipient dismisses one. Does not commit: it rides the caller's
    transaction.
    """
    query = (
        update(AccessRequest)
        .where(
            AccessRequest.entity_type == ENTITY_ASSIGNMENT_RUN,
            AccessRequest.entity_id == run_id,
            AccessRequest.status == STATUS_OPEN,
        )
        .values(status=STATUS_CLOSED, closed_at=datetime.now(UTC), closed_by_id=closed_by.id if closed_by else None)
    )
    if request_id is not None:
        query = query.where(AccessRequest.id == request_id)
    result = await db.execute(query)
    return result.rowcount or 0
