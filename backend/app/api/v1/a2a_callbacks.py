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

"""Where a partner tells us its task finished.

The one route on this platform a partner may call as part of the OUTBOUND
direction, and `009` ruled it out: *"A new partner-facing surface for a partner
to poll or push into. The outbound direction adds none."* Spec 011 supersedes
that deliberately, which is why callbacks are the last increment and are gated
on an operator's decision rather than on a partner's card.

Every guard here is one the inbound surface already applies, and the ordering
is the point: transport first (``require_secure_transport`` refuses a request
it cannot confirm encrypted, since the secret this route reads travels in
it), then the pre-auth rate limit, then a constant-time secret comparison,
and only then is anything read. A bad token, an unknown
delegation and one already concluded share a single answer, so a partner cannot
learn which delegations exist by asking.

Nothing in the body is trusted. It names a task and a state; the state routes
and does not decide, and the answer comes from reading the partner's task over
the authenticated channel we already hold.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.a2a.client import callbacks as callback_svc
from app.api.deps import require_secure_transport
from app.api.v1.a2a_server import _enforce_preauth_rate_limit
from app.core.database import async_session

router = APIRouter(
    prefix="/a2a/callbacks",
    tags=["a2a"],
    dependencies=[Depends(require_secure_transport)],
)

#: One answer for every refusal. A partner learns whether its callback was
#: accepted and nothing else.
_REFUSED = JSONResponse(status_code=404, content={"error": "not found"})


@router.post("/{delegation_id}")
async def receive_callback(delegation_id: str, request: Request) -> JSONResponse:
    """Accept a partner's notification that a delegated task changed state."""
    await _enforce_preauth_rate_limit(request)

    presented = await _presented_secret(request)
    if not presented:
        return _REFUSED

    # A session of this route's own, not the request dependency. ``get_db``
    # yields and then commits, so returning out of an ``async for`` over it
    # leaves the generator suspended at the yield: the commit never runs and
    # the connection goes back to the pool at finalization rather than at the
    # end of the request. On the one pre-auth, partner-reachable surface in
    # this direction — the one that needed a rate limit because it can be
    # flooded — that is a pool-exhaustion path.
    async with async_session() as db:
        # A2A is offered exactly while agentic chat is, in both directions. A
        # callback arriving while AI is off names work that cannot be resumed,
        # and is refused as everything else here is refused.
        from app.api.v1.platform import is_chat_enabled

        if not await is_chat_enabled(db):
            return _REFUSED
        body = await _json_body(request)
        if callback_svc.is_ticket_push(body):
            return await _receive_ticket_push(db, delegation_id, body, presented)
        row = await callback_svc.addressed_delegation(db, delegation_id)
        # Authenticate BEFORE anything is read, and answer a failure exactly as
        # an unknown delegation is answered.
        if row is None or not callback_svc.presented_secret_matches(row, presented):
            return _REFUSED
        # Answered before it is acted on, deliberately. Acting is a collect
        # bounded by the delegation ceiling, and holding the partner's own
        # response for it inverts what a callback is for. The partner did its
        # part the moment it told us; a failure afterwards is ours, and the
        # polling floor concludes the delegation either way.
        callback_svc.spawn_partner_finished(row.id)
        return JSONResponse(status_code=200, content={"status": "accepted"})


async def _receive_ticket_push(db: AsyncSession, delegation_id: str, body: object, presented: str) -> JSONResponse:
    """A partner says a ticket a delegated task filed changed status (#2276).

    The same guards, in the same order: the delegation must exist and the
    presented secret must be its own before the body is read further, and every
    refusal is the single answer. A concluded delegation is admitted (a ticket
    outlives its task) because nothing is resumed: the push raises a platform
    event for the delegating agent and is never taken as an answer.
    """
    row = await callback_svc.ticket_push_delegation(db, delegation_id)
    if row is None or not callback_svc.presented_secret_matches(row, presented):
        return _REFUSED
    fields = callback_svc.ticket_push_fields(body, row)
    if fields is None:
        return _REFUSED
    await callback_svc.publish_ticket_status(db, row, fields)
    return JSONResponse(status_code=200, content={"status": "accepted"})


async def _json_body(request: Request) -> object:
    """The request's JSON body, ``None`` when it does not parse. Partner-supplied."""
    try:
        return await request.json()
    except Exception:
        return None


async def _presented_secret(request: Request) -> str | None:
    """The secret this partner is presenting, from either place we put it.

    This platform's own sender offers both, and which one it uses depends on
    what the callback was registered with: an ``Authorization`` header when the
    partner registered a scheme and credentials, and an opaque ``token`` echoed
    in the body otherwise. We register a token, so against ourselves it is
    always the body — reading only the header refused every callback this
    platform sent itself, which is how this was found.

    The body is read for this one field. Nothing else in it is trusted, and
    the state it carries decides nothing: the answer comes from reading the
    partner's task afterwards.
    """
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    try:
        body = await request.json()
    except Exception:
        # Partner-supplied and unparsed until here.
        return None
    token = body.get("token") if isinstance(body, dict) else None
    return str(token).strip() or None if token else None
