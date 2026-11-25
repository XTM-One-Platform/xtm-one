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

"""The native web chat over HTTP + SSE (``/api/v1/web``).

The routes the ``@filigran/chatbot`` package speaks, for XTM One's own web
conversations (channel ``web``). A send streams its turn back as SSE frames
served by ``run_chat_connection``, and the turn outlives its stream. Stop,
"Continue now", steering and approvals are POSTs that reach the turn on this
replica directly, on another through Redis.
``GET /chat/conversations/{id}/events`` follows the conversation (a reload,
another device) through the turn relay. The paths are the embedded chat's
under ``/api/v1/platform``, so one endpoint map serves both.

The embedded chat's routes (``platform.py``, channel ``api``) serve OpenCTI,
OpenAEV and OpenCRQ.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_chat_enabled
from app.api.v1 import chat as chat_api
from app.api.v1 import platform as platform_api
from app.api.v1 import platform_chat_workspaces as workspaces_api
from app.api.v1.helpers import owned_conversation
from app.core.database import get_db, release_db_connection_before_loop
from app.core.redaction import known_secrets_scoped
from app.core.sessions import SESSION_EXPIRED_CODE, SESSION_RECHECK_SECONDS, SessionEndedError, recheck_session
from app.llm.provider_access import bind_acting_user
from app.models.chat import Conversation
from app.models.user import User
from app.services import chat_turn_guard, chat_turn_stream
from app.services.chat_messages import sent_message_text
from app.services.chat_web_transport import (
    APP_LOCKED_FRAME_TYPE,
    SSE_KEEPALIVE,
    DetachedTurnStreamResponse,
    EventStreamResponse,
    HeadlessTurnConnection,
    keepalive_delay,
    live_connection,
    session_lock_state,
    sse_data,
)

logger = logging.getLogger(__name__)

#: Where ``main.py`` mounts the router.
WEB_CHAT_PREFIX = "/api/v1/web"
#: The two routes that stream for as long as a turn, or a conversation view, lasts.
SEND_PATH = "/chat/messages"
EVENTS_PATH = "/chat/conversations/{conversation_id}/events"


def is_long_lived_stream(method: str, path: str) -> bool:
    """Whether *method* *path* opens a send's stream (``POST``) or an events stream (``GET``)."""
    if not path.startswith(WEB_CHAT_PREFIX):
        return False
    rest = path[len(WEB_CHAT_PREFIX) :]
    if method == "POST":
        return rest == SEND_PATH
    head, _, tail = EVENTS_PATH.partition("{conversation_id}")
    return method == "GET" and rest.startswith(head) and rest.endswith(tail)

#: The header naming the browser tab a request comes from: its frames are never
#: relayed back to its own events stream.
CLIENT_ID_HEADER = "x-chat-client-id"
_CLIENT_ID_MAX_LENGTH = 128
#: An events stream ends after this long and the client reconnects, so a
#: worker recycle never waits on it and its session is checked again.
EVENTS_MAX_LIFETIME_SECONDS = 15 * 60
#: The frames an events stream holds for a client that has not read them yet:
#: room for the relay hub's whole backlog and a resync's snapshot. Past it the
#: stream ends.
EVENTS_QUEUE_SIZE = 2 * chat_turn_stream.LISTENER_QUEUE_SIZE
#: How often an open events stream says somebody is there (approval presence).
CLIENT_SEEN_REFRESH_SECONDS = chat_turn_guard.CLIENT_SEEN_TTL_SECONDS / 3
#: Stop and "Continue now" with the turn on another replica and Redis not answering:
#: nothing would reach it, and the client must not believe it did.
CONTROL_UNAVAILABLE_DETAIL = "The running response cannot be reached right now. Try again shortly."


def _client_id(value: str | None) -> str:
    value = (value or "").strip()
    if value and len(value) <= _CLIENT_ID_MAX_LENGTH and value.isprintable():
        return value
    return uuid.uuid4().hex


async def web_chat_user(user: User = Depends(get_current_user)) -> User:
    """The caller, who must hold a web session: an API key or platform token chats elsewhere."""
    if getattr(user, "auth_credential_kind", None) != "session_jwt":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="The web chat needs a web session")
    return user


#: Every route under :data:`WEB_CHAT_PREFIX`, the handlers re-mounted from other
#: routers included, answers web sessions only. A route that reads the caller
#: takes ``Depends(web_chat_user)`` too: FastAPI resolves it once per request.
router = APIRouter(dependencies=[Depends(web_chat_user)])


async def _readable_conversation(db: AsyncSession, user: User, conversation_id: str) -> Conversation | None:
    """A conversation the caller may read - their own or one shared with them - or None."""
    try:
        conv, _access = await chat_api.resolve_conversation_for_read(conversation_id, user, db)
    except HTTPException:
        return None
    return conv


# ── Sends ───────────────────────────────────────────────────────────────
class WebChatSendRequest(BaseModel):
    """One send: the frame ``run_chat_connection`` reads."""

    content: str
    conversation_id: str
    file_ids: list[str] | None = None
    builtin_tool_ids: list[str] | None = None
    custom_tool_ids: list[str] | None = None
    integration_ids: list[str] | None = None
    mcp_server_ids: list[str] | None = None
    knowledge_base_ids: list[str] | None = None
    default_tool_ids: list[str] | None = None
    scope_headers: dict[str, Any] | None = None
    tool_selection_scope: Any = None
    mode: str | None = None
    source_pack_slugs: list[str] | None = None
    source_pack_options: Any = None
    studio_slide: Any = None
    studio_scene: Any = None
    studio_widget: Any = None
    desktop_runner_id: str | None = None
    mentioned_agent_ids: list[str] | None = None
    referenced_conversation_ids: list[str] | None = None
    supports_tool_approval: bool = False
    # Will this client offer the user's likely next message
    # (``reply_suggestion``, ``services/chat_reply_suggestions.py``)? A
    # promise like ``supports_tool_approval``: absent, none is generated and
    # the stream closes at the turn's end, so a script reading to the end of
    # the stream never waits on one.
    supports_reply_suggestions: bool = False

    def frame(self) -> dict:
        return self.model_dump(exclude_none=True, exclude={"conversation_id"})


@known_secrets_scoped("conversation_id")
async def _run_web_send(
    connection: HeadlessTurnConnection, *, conversation_id: str, auth: chat_api.ChatConnectionAuth
) -> None:
    bind_acting_user(auth.user_id)
    await chat_api.run_chat_connection(connection, conversation_id, auth)


@router.post(SEND_PATH)
async def send_web_chat_message(
    body: WebChatSendRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(web_chat_user),
    _chat_gate: None = Depends(require_chat_enabled),
):
    """Send a message and stream its turn back as SSE frames.

    When a turn is already running, the message is folded into it: the stream
    says ``folded_into_active_turn`` and relays that turn up to its end. A
    client that goes away leaves the turn running; it stores its answer.
    """
    conv = await owned_conversation(db, user, body.conversation_id)
    conversation_id = str(conv.id)
    # The claims ``get_current_user`` verified: the token is not decoded twice.
    claims = user.web_session.claims
    issued_at = claims.get("iat")
    auth = chat_api.ChatConnectionAuth(
        user_id=user.id,
        session=user.web_session,
        impersonated_by=claims.get("impersonated_by"),
        token_issued_at=issued_at if isinstance(issued_at, int) else None,
        origin=_client_id(request.headers.get(CLIENT_ID_HEADER)),
    )
    # The turn opens its own sessions; the request's would stay checked out
    # for the whole stream.
    await release_db_connection_before_loop(db)
    connection = HeadlessTurnConnection(conversation_id, body.frame())
    return DetachedTurnStreamResponse(
        connection, functools.partial(_run_web_send, conversation_id=conversation_id, auth=auth)
    )


#: Steering with Redis not answering, the turn elsewhere, before anything is
#: stored: a 503 always means the client keeps its text.
STEERING_UNAVAILABLE_DETAIL = "Steering is temporarily unavailable"
NO_RUNNING_TURN_DETAIL = "No response is currently being generated for this conversation"


class WebChatSteerRequest(BaseModel):
    conversation_id: str
    content: str
    steer_id: str | None = Field(default=None, max_length=128)
    file_ids: list[str] | None = None


@router.post("/chat/messages/steer")
async def steer_web_chat_message(
    body: WebChatSteerRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(web_chat_user),
    _chat_gate: None = Depends(require_chat_enabled),
):
    """Hand a message to the conversation's running turn, at its next iteration.

    409 when no turn takes it, the turn having ended on the way included: the
    client sends it as the next turn. The message is stored before it is
    handed over (a crash after the hand-off leaves it in the thread), and
    shown on the conversation's other clients only once it stays there.
    ``stored`` (no ``turn_id``) when it stays with no turn known to have
    taken it, with ``answered: false`` when no turn took it for certain; a
    503 always means nothing was stored.
    """
    content = sent_message_text(body.content, body.file_ids)
    if not content:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    conv = await owned_conversation(db, user, body.conversation_id)
    conversation_id = str(conv.id)
    local = live_connection(conversation_id)
    if local is not None and local.steering_closed:
        raise HTTPException(status_code=409, detail=NO_RUNNING_TURN_DETAIL)
    if local is None or not local.accepts_steering:
        try:
            # A channel turn has no steering bridge: nothing would read it.
            steerable = await chat_turn_guard.is_turn_active(conversation_id) and not (
                await chat_turn_guard.channel_turn_holder(conversation_id)
            )
        except Exception as exc:
            logger.warning("Steering check failed for conv %s: %s", conversation_id, exc)
            raise HTTPException(status_code=503, detail=STEERING_UNAVAILABLE_DETAIL) from exc
        if not steerable:
            raise HTTPException(status_code=409, detail=NO_RUNNING_TURN_DETAIL)
    try:
        steer = await chat_api.persist_web_steer(
            conversation_id=conversation_id,
            user_id=user.id,
            content=content,
            file_ids=body.file_ids,
        )
    except chat_api.SteerRefusedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message, headers=exc.headers or None) from None
    origin = _client_id(request.headers.get(CLIENT_ID_HEADER))
    file_ids = [ref.file_id for ref in steer.file_refs if getattr(ref, "file_id", None)]
    # The turn runs here: handed to it now, not through the inbox its bridge polls.
    local = live_connection(conversation_id)
    steer_message_id = str(steer.message.id)
    turn_id = (
        local.steer(chat_turn_guard.SteeringPayload(text=content, file_ids=file_ids, message_id=steer_message_id))
        if local
        else None
    )
    if turn_id is not None:
        chat_turn_stream.publish_user_message(conversation_id, steer.message, origin=origin)
        return {"status": "steered", "steer_id": body.steer_id, "turn_id": turn_id}
    if local is not None and local.steering_closed:
        # The turn here stopped reading steering while this was stored: the
        # inbox would hold it past the turn's last look.
        return await _withdraw_unanswered_steer(steer, conversation_id, origin, body.steer_id)
    accepted: list[str] = []
    try:
        folded = await chat_turn_guard.fold_into_inflight_turn(
            conversation_id, content, file_ids=file_ids, accepted_by=accepted, message_id=steer_message_id
        )
    except chat_turn_guard.FoldRefusedError:
        return await _withdraw_unanswered_steer(
            steer, conversation_id, origin, body.steer_id, detail="The conversation was stopped"
        )
    except Exception as exc:
        logger.warning("Steering hand-off failed for conv %s: %s", conversation_id, exc)
        # Whether the push landed is unknown, so the message is not withdrawn
        # (the turn may read it): it stays in the thread, and the client keeps it.
        chat_turn_stream.publish_user_message(conversation_id, steer.message, origin=origin)
        return _stored_steer(body.steer_id)
    if not folded:
        # The turn ended in between and the push was taken back.
        return await _withdraw_unanswered_steer(steer, conversation_id, origin, body.steer_id)
    chat_turn_stream.publish_user_message(conversation_id, steer.message, origin=origin)
    return {
        "status": "steered",
        "steer_id": body.steer_id,
        "turn_id": chat_turn_stream.public_turn_id(accepted[0]) if accepted else None,
    }


def _stored_steer(steer_id: str | None, *, answered: bool | None = None) -> dict:
    """The answer for a steer left in the thread with no turn known to have taken it.

    ``answered=False`` when no turn took it for certain: it shapes the next
    turn but has no answer of its own. Left out when that is unknown.
    """
    answer: dict = {"status": "stored", "steer_id": steer_id, "turn_id": None}
    if answered is not None:
        answer["answered"] = answered
    return answer


async def _withdraw_unanswered_steer(
    steer: chat_api.PersistedSteer,
    conversation_id: str,
    origin: str,
    steer_id: str | None,
    *,
    detail: str = NO_RUNNING_TURN_DETAIL,
) -> dict:
    """Refuse a stored steer no turn took (409), deleting it: the client sends the text as the next turn.

    ``stored`` with ``answered: false`` when it cannot be deleted: it is in
    the thread and shapes the next turn, and a resend would store it twice.
    """
    try:
        await chat_api.withdraw_web_steer(steer)
    except Exception:
        logger.exception("Could not withdraw the unanswered steer of conv %s", conversation_id)
        chat_turn_stream.publish_user_message(conversation_id, steer.message, origin=origin)
        return _stored_steer(steer_id, answered=False)
    raise HTTPException(status_code=409, detail=detail)


class WebChatApprovalRequest(BaseModel):
    conversation_id: str
    decisions: list[platform_api.PlatformChatApprovalDecision]


@router.post("/chat/messages/approve")
async def approve_web_chat_tools(
    body: WebChatApprovalRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(web_chat_user),
    _chat_gate: None = Depends(require_chat_enabled),
):
    """Answer a running turn that paused for tool approval; 409 when none is waiting, 503 when Redis cannot carry it."""
    if not body.decisions:
        raise HTTPException(status_code=400, detail="Send at least one decision")
    decisions = [d for d in body.decisions if d.tool_call_id]
    if not decisions:
        raise HTTPException(status_code=400, detail="Send at least one decision naming a tool call")
    decided = await platform_api.apply_approval_decisions(
        db, user, body.conversation_id, decisions, surface="web_stream", channel=None
    )
    return {"status": "accepted", "decided": decided}


class WebChatTurnRequest(BaseModel):
    conversation_id: str


class WebChatCancelRequest(WebChatTurnRequest):
    #: The client's own send is on its way and its turn has not started.
    awaiting_send: bool = False


@router.post("/chat/messages/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_web_chat_turn(
    body: WebChatCancelRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(web_chat_user),
):
    """Stop the conversation's turn and the background work it started, wherever it runs.

    With no turn (``turn: "none"``) and ``awaiting_send``, the Stop is kept
    for the caller's send that has not taken the lock yet: that turn ends as
    soon as it starts. Without it nothing is kept, so a Stop that came as a
    turn ended never ends the caller's next send.
    A Stop after the turn's last pass leaves its answer as it is (the stream
    still ends with ``done``) and stops the background work.
    503 when the turn is not on this replica and Redis cannot carry the Stop.
    """
    conv = await owned_conversation(db, user, body.conversation_id)
    conversation_id = str(conv.id)
    client_id = _client_id(request.headers.get(CLIENT_ID_HEADER))
    connection = live_connection(conversation_id)
    if connection is not None:

        async def _cancel_local_turn() -> bool:
            connection.inject({"type": "cancel"})
            return True

        await chat_api.stop_conversation_work(conversation_id, _cancel_local_turn)
        try:
            await chat_turn_guard.drop_pending_cancel(conversation_id, client_id)
        except Exception:
            # The Stop reached the turn; a kept one left behind expires shortly.
            logger.warning("Could not drop the kept Stop of conv %s", conversation_id, exc_info=True)
        return {"status": "cancelled", "turn": "local"}

    async def _request_cancel() -> bool:
        return await chat_turn_guard.request_turn_cancel(
            conversation_id, client_id=client_id, keep_for_send=body.awaiting_send
        )

    try:
        remote = await chat_api.stop_conversation_work(conversation_id, _request_cancel)
    except Exception as exc:
        logger.warning("Failed to request a cross-process cancel for %s", conversation_id, exc_info=True)
        raise HTTPException(status_code=503, detail=CONTROL_UNAVAILABLE_DETAIL) from exc
    return {"status": "cancelled", "turn": "remote" if remote else "none"}


@router.post("/chat/messages/skip-wait", status_code=status.HTTP_202_ACCEPTED)
async def skip_web_chat_wait(
    body: WebChatTurnRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(web_chat_user),
):
    """End the running turn's current pause ("Continue now"); the turn carries on.

    503 when the turn is not on this replica and Redis cannot carry the request.
    """
    conv = await owned_conversation(db, user, body.conversation_id)
    conversation_id = str(conv.id)
    connection = live_connection(conversation_id)
    if connection is not None:
        connection.inject({"type": "skip_wait"})
        return {"status": "wait_skipped", "turn": "local"}
    try:
        remote = await chat_turn_guard.request_wait_skip(conversation_id)
    except Exception as exc:
        logger.warning("Failed to request a cross-process skip-wait for %s", conversation_id, exc_info=True)
        raise HTTPException(status_code=503, detail=CONTROL_UNAVAILABLE_DETAIL) from exc
    return {"status": "wait_skipped", "turn": "remote" if remote else "none"}


@router.get("/chat/conversations/{conversation_id}/pending-approvals")
async def get_web_chat_pending_approvals(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(web_chat_user),
):
    """What a paused turn of this conversation still waits on, for a client that reloaded.

    503 when Redis cannot say: an ``idle`` the client trusted would drop a
    prompt the turn still waits on.
    """
    conv = await owned_conversation(db, user, conversation_id)
    conversation_id = str(conv.id)
    try:
        await chat_turn_guard.mark_client_seen(conversation_id)
        running = await chat_turn_guard.is_turn_active(conversation_id)
        proposals = await chat_turn_guard.get_pending_approvals(conversation_id) or []
    except Exception as exc:
        logger.warning("Turn-state check failed for conv %s: %s", conversation_id, exc)
        raise HTTPException(status_code=503, detail="The conversation's turn cannot be read right now") from exc
    return {
        "conversation_id": conversation_id,
        "proposals": proposals,
        "turn": "running" if running else "idle",
    }


@router.get(EVENTS_PATH)
async def stream_web_chat_events(
    conversation_id: str,
    request: Request,
    # Not length-checked here: ``_client_id`` replaces an invalid one.
    client_id: Annotated[str | None, Query()] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(web_chat_user),
):
    """Follow the conversation: the turn in flight (resumed after a reload), then live.

    Every turn frame, ``user_message``, ``conversation_title`` and
    ``approval_resolved`` the conversation's other connections produce. The
    frames ``client_id`` produced itself are never sent back to it.
    """
    conv = await owned_conversation(db, user, conversation_id)
    conversation_id = str(conv.id)
    client_id = _client_id(client_id or request.headers.get(CLIENT_ID_HEADER))
    web_session = user.web_session
    user_id = user.id
    with contextlib.suppress(Exception):
        await chat_turn_guard.mark_client_seen(conversation_id)
    await release_db_connection_before_loop(db)

    async def events() -> AsyncIterator[bytes]:
        nonlocal web_session
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=EVENTS_QUEUE_SIZE)
        closed = False

        async def _send(frame: dict) -> bool:
            nonlocal closed
            if closed:
                return False
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                # The client reads slower than the turn writes: the stream
                # ends, and the client resyncs from the snapshot.
                closed = True
                return False
            return True

        relay = asyncio.create_task(
            chat_turn_stream.relay_turn_frames(
                conversation_id, origin=client_id, send=_send, is_closed=lambda: closed
            ),
            name=f"chat-web-events:{conversation_id}",
        )
        loop = asyncio.get_running_loop()
        started = session_checked = seen_marked = last_sent = loop.time()
        try:
            while not closed and (not relay.done() or not queue.empty()):
                now = loop.time()
                if now - started >= EVENTS_MAX_LIFETIME_SECONDS:
                    return
                if now - session_checked >= SESSION_RECHECK_SECONDS:
                    session_checked = now
                    try:
                        web_session = await recheck_session(web_session)
                    except SessionEndedError as exc:
                        yield sse_data({"type": SESSION_EXPIRED_CODE, "reason": exc.reason})
                        return
                    lock_state = await session_lock_state(user_id, web_session)
                    if lock_state == "locked":
                        yield sse_data({"type": APP_LOCKED_FRAME_TYPE})
                    if lock_state != "open":
                        return
                if now - seen_marked >= CLIENT_SEEN_REFRESH_SECONDS:
                    seen_marked = now
                    with contextlib.suppress(Exception):
                        await chat_turn_guard.mark_client_seen(conversation_id)
                delay = keepalive_delay(last_sent, now)
                if delay == 0:
                    yield SSE_KEEPALIVE
                    last_sent = loop.time()
                    continue
                # Woken for the next session check too, so a quiet stream ends on time.
                delay = min(delay, session_checked + SESSION_RECHECK_SECONDS - now)
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=delay)
                except TimeoutError:
                    continue
                yield sse_data(frame)
                last_sent = loop.time()
        finally:
            closed = True
            relay.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await relay

    return EventStreamResponse(events())


# ── Sessions ────────────────────────────────────────────────────────────
class WebChatSessionRequest(BaseModel):
    conversation_id: str | None = None
    agent_slug: str | None = None
    agent_id: str | None = None
    workspace_id: str | None = None
    supports_artifacts: bool = False


# ── The agent picker ────────────────────────────────────────────────────
#: The picker's entry for the General Assistant (no agent): never an agent slug,
#: which has no ``:``.
GENERAL_ASSISTANT_ENTRY = "general-assistant:"
#: A chat-flow's entry is this prefix and the flow's id.
FLOW_ENTRY_PREFIX = "flow:"
#: The picker's headings, after the chat agents.
FLOWS_GROUP = "Flows"
NOT_FOR_CHAT_GROUP = "Not recommended for chat"
#: The badges after an agent's name: company-managed, and standing in for the
#: General Assistant.
COMPANY_BADGE = "Company"
STAND_IN_BADGE = "Assistant"

#: The picker's own words in each catalog language: the panel shows a heading
#: and a badge as sent, so they are sent in the reader's language.
PICKER_WORDS: dict[str, dict[str, str]] = {
    FLOWS_GROUP: {"en": "Flows", "es": "Flujos", "fr": "Flows", "ja": "フロー"},
    NOT_FOR_CHAT_GROUP: {
        "en": "Not recommended for chat",
        "es": "No recomendado para chat",
        "fr": "Non recommandé pour le chat",
        "ja": "チャットには非推奨",
    },
    COMPANY_BADGE: {"en": "Company", "es": "Empresa", "fr": "Entreprise", "ja": "会社"},
    STAND_IN_BADGE: {"en": "Assistant", "es": "Asistente", "fr": "Assistant", "ja": "アシスタント"},
}


def picker_language(user: Any, accept_language: str | None) -> str:
    """The language the web UI speaks to *user*, as the frontend resolves it.

    Their own preference, else the platform default, else the first catalog
    language of the browser's ``Accept-Language``, else English.
    """
    from app.services.channel_notices import DEFAULT_LANGUAGE, LANGUAGES, catalog_language, platform_language

    chosen = catalog_language(getattr(user, "language", None)) or platform_language()
    if chosen:
        return chosen
    for part in (accept_language or "").split(","):
        primary = part.split(";", 1)[0].strip().lower().split("-", 1)[0]
        if primary in LANGUAGES:
            return primary
    return DEFAULT_LANGUAGE


def picker_word(key: str, language: str) -> str:
    """*key*, one of :data:`PICKER_WORDS`, in *language* (English when it holds none)."""
    words = PICKER_WORDS[key]
    return words.get(language) or words["en"]


class WebChatAgentOut(BaseModel):
    id: str
    name: str
    slug: str | None
    icon: str | None = None
    description: str | None = None
    group: str | None = None
    #: Short labels after the name, already translated (``XtmAgent.badges``).
    badges: list[str] = Field(default_factory=list)
    #: Built for assignments: the panel flags it in the picker and warns in the composer (``XtmAgent.disableChat``).
    disable_chat: bool = False


GENERAL_ASSISTANT_OUT = WebChatAgentOut(
    id=GENERAL_ASSISTANT_ENTRY, name="General Assistant", slug=GENERAL_ASSISTANT_ENTRY
)


async def _flow_chat_entries(db: AsyncSession, user: User) -> list[Any]:
    """The chat-flows the caller may open, while the flow builder is on."""
    from app.api.v1 import flows as flows_api
    from app.core.feature_flags import is_feature_enabled

    if not is_feature_enabled("FLOW_BUILDER"):
        return []
    return await flows_api.list_flow_chat_entries(user=user, db=db)


#: The built-in tool a studio rail's agent must carry, by ``?studio=``.
STUDIO_PICKER_TOOL: dict[str, str] = {
    "presentation": "generate_presentation",
    "video": "video_studio",
    "dashboard": "custom_dashboards",
    "fleet": "platform",
}
#: The function of that tool the job needs, when it is one sub-area of a merged
#: group: the fleet page's rail edits a fleet (the client's ``FLEET_EDIT_JOB``).
STUDIO_PICKER_FUNCTION: dict[str, str] = {"fleet": "update_fleet"}


def _carries_job(
    tool_ids: list[str] | None, allowed: dict[str, list[str] | None] | None, job: tuple[str, str | None]
) -> bool:
    """Whether a selection carries *job*: its suite, a former id folded in, and the function its allow list keeps."""
    from app.services.builtin_tool_aliases import canonical_builtin_selection

    suite, fn = job
    ids, allowed_map = canonical_builtin_selection(tool_ids, allowed)
    if suite not in ids:
        return False
    kept = (allowed_map or {}).get(suite)
    return fn is None or kept is None or fn in kept


async def _answering_agent_name(db: AsyncSession, conv) -> str:
    """Who answers a conversation without an agent of its own: the agent standing
    in for the General Assistant (an unbound Slack or Teams thread), else the
    General Assistant, under the names the picker gives them."""
    from app.services import general_assistant_link

    stand_in = await general_assistant_link.turn_stand_in(
        db, agent_id=None, conversation_channel=conv.channel, user_id=conv.user_id
    )
    return stand_in.name if stand_in is not None else GENERAL_ASSISTANT_OUT.name


async def _general_assistant_carries(db: AsyncSession, user: User, job: tuple[str, str | None]) -> bool:
    """Whether the General Assistant's tools carry *job* for *user* (its ids carry no allow list)."""
    from app.models.general_assistant_config import GeneralAssistantConfig, general_assistant_builtin_tool_ids

    platform_tool_ids = None
    if user.ga_builtin_tool_ids is None:
        platform_tool_ids = (await db.execute(select(GeneralAssistantConfig.builtin_tool_ids).limit(1))).scalar()
    return _carries_job(general_assistant_builtin_tool_ids(platform_tool_ids, user), None, job)


@router.get("/chat/agents", response_model=list[WebChatAgentOut])
async def list_web_chat_agents(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(web_chat_user),
    studio: Annotated[Literal["presentation", "video", "dashboard", "fleet"] | None, Query()] = None,
    accept_language: Annotated[str | None, Header()] = None,
) -> list[WebChatAgentOut]:
    """What the web chat's agent picker offers, in this order:

    The General Assistant first, unless an agent stands in for it; the
    agents open to chat, by name; the chat-flows under their flow's name; then
    the agents built for assignments rather than chat. The General Assistant and
    a flow are entries rather than agents: ``open_web_chat_session`` starts a
    session from one.

    A studio rail (``?studio=``) offers only who can do its job: the chat agents
    carrying the studio's tool, and the General Assistant when its effective
    tools carry it.

    An agent carries its badges: ``Company`` when company-managed, and one for
    the agent standing in for the General Assistant; the badges and headings
    are in the reader's language (:func:`picker_language`).
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.models.agent import Agent
    from app.services.general_assistant_link import general_assistant_stand_in

    group_ids = await get_user_group_ids(db, user.id)
    agents = (
        (
            await db.execute(
                select(Agent)
                .where(Agent.enabled.is_(True), user_visible_filter(Agent, user.id, group_ids))
                .order_by(Agent.name)
            )
        )
        .scalars()
        .all()
    )

    language = picker_language(user, accept_language)
    stand_in = await general_assistant_stand_in(db)

    def entry(agent: Agent, group: str | None = None) -> WebChatAgentOut:
        badges = [
            picker_word(key, language)
            for key, on in (
                (STAND_IN_BADGE, stand_in is not None and agent.id == stand_in.id),
                (COMPANY_BADGE, bool(agent.is_company_managed)),
            )
            if on
        ]
        return WebChatAgentOut(
            id=str(agent.id),
            name=agent.name,
            slug=agent.slug,
            icon=agent.icon,
            description=agent.description,
            group=picker_word(group, language) if group else None,
            badges=badges,
            disable_chat=bool(agent.disable_chat),
        )

    out: list[WebChatAgentOut] = []
    if studio is not None:
        job = (STUDIO_PICKER_TOOL[studio], STUDIO_PICKER_FUNCTION.get(studio))
        if stand_in is None and await _general_assistant_carries(db, user, job):
            out.append(GENERAL_ASSISTANT_OUT)
        out.extend(
            entry(a)
            for a in agents
            if not a.disable_chat and _carries_job(a.builtin_tool_ids, a.builtin_allowed_tools, job)
        )
        return out

    if stand_in is None:
        out.append(GENERAL_ASSISTANT_OUT)
    out.extend(entry(a) for a in agents if not a.disable_chat)
    out.extend(
        WebChatAgentOut(
            id=f"{FLOW_ENTRY_PREFIX}{flow.flow_id}",
            name=flow.flow_name,
            slug=f"{FLOW_ENTRY_PREFIX}{flow.flow_id}",
            description=flow.description,
            group=picker_word(FLOWS_GROUP, language),
        )
        for flow in await _flow_chat_entries(db, user)
    )
    out.extend(entry(a, NOT_FOR_CHAT_GROUP) for a in agents if a.disable_chat)
    return out


async def _new_conversation_target(db: AsyncSession, user: User, slug: str) -> tuple[str | None, str | None]:
    """The agent and title a session picked as *slug* starts with.

    The General Assistant's entry starts one with no agent (bound to its
    stand-in by ``create_conversation`` when there is one); a flow's, one with
    the flow's agent under the flow's name; an agent's slug, one with that agent.
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.models.agent import Agent

    if slug == GENERAL_ASSISTANT_ENTRY:
        return None, None
    if slug.startswith(FLOW_ENTRY_PREFIX):
        flow_id = slug.removeprefix(FLOW_ENTRY_PREFIX)
        flow = next((f for f in await _flow_chat_entries(db, user) if f.flow_id == flow_id), None)
        if flow is None:
            raise HTTPException(status_code=404, detail="Flow not found")
        return flow.agent_id, flow.flow_name
    group_ids = await get_user_group_ids(db, user.id)
    agent = (
        await db.execute(
            select(Agent).where(
                Agent.slug == slug,
                Agent.enabled.is_(True),
                user_visible_filter(Agent, user.id, group_ids),
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return str(agent.id), None


@router.post("/chat/sessions")
async def open_web_chat_session(
    body: WebChatSessionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(web_chat_user),
    presentation_id: Annotated[str | None, Query()] = None,
    video_project_id: Annotated[str | None, Query()] = None,
    custom_dashboard_id: Annotated[str | None, Query()] = None,
    fleet_id: Annotated[str | None, Query()] = None,
) -> platform_api.ChatPanelSessionResponse:
    """Restore a conversation the caller may read (their own, or one shared with them), or start one.

    Only starting one takes the chat gate: a restore is a read, open while
    chat is unavailable like the conversation list.

    A studio rail names its subject in the query (``?presentation_id=``,
    ``?video_project_id=``, ``?custom_dashboard_id=`` or ``?fleet_id=``, the
    panel's ``sessions`` path): a conversation it starts is bound to that deck,
    video, dashboard or fleet before its first turn, under the
    binding rules of ``POST /api/v1/chat/conversations``.
    """
    # A conversation shared with the caller is read, never replaced by a new one:
    # the sends and the events stream stay the owner's.
    conv = await _readable_conversation(db, user, body.conversation_id) if body.conversation_id else None
    if conv is not None:
        # The web panel draws a dashboard's marker as the dashboard: kept, not made a link.
        restored, has_more = await platform_api.restore_chat_session_window(
            db, user, conv, supports_artifacts=body.supports_artifacts, keep_dashboards=True
        )
        if conv.agent_id is None:
            # Here a conversation is answered by its own agent whatever the panel
            # picked (the platform route answers with the agent asked for), so
            # one without names who answers it, as the picker lists it.
            restored = restored.model_copy(update={"agent_name": await _answering_agent_name(db, conv)})
        turn = await chat_api.read_conversation_turn_state(db, conv, owner=conv.user_id == user.id)
        return platform_api.ChatPanelSessionResponse(
            **restored.model_dump(),
            has_more=has_more,
            is_processing=turn.is_processing,
            pending_approvals=turn.pending_approvals,
            interrupted_turn=turn.interrupted_turn,
        )
    await require_chat_enabled(db)
    agent_id, title = body.agent_id, None
    if not agent_id and body.agent_slug:
        agent_id, title = await _new_conversation_target(db, user, body.agent_slug)
    created = await chat_api.create_conversation(
        body=chat_api.ConversationCreate(
            agent_id=agent_id,
            title=title,
            workspace_id=body.workspace_id,
            presentation_id=presentation_id,
            video_project_id=video_project_id,
            custom_dashboard_id=custom_dashboard_id,
            fleet_id=fleet_id,
        ),
        user=user,
        db=db,
        _chat_gate=None,
    )
    agent_name = None
    if created.agent_id:
        from app.models.agent import Agent

        agent_name = (await db.execute(select(Agent.name).where(Agent.id == uuid.UUID(created.agent_id)))).scalar()
    return platform_api.ChatPanelSessionResponse(
        conversation_id=created.id,
        messages=[],
        agent_id=created.agent_id,
        agent_name=agent_name,
        workspace_id=created.workspace_id,
        channel=created.channel,
    )


@router.get("/chat/sessions")
async def list_web_chat_sessions(
    user: User = Depends(web_chat_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    workspace_id: str | None = Query(default=None),
    scope: str = Query(default="own", pattern="^(own|shared)$"),
):
    """The web sidebar's conversations (continued Slack / Teams threads included), in the history menu's shape."""
    rows = await chat_api.list_conversations(
        user=user,
        db=db,
        limit=limit,
        offset=offset,
        archived=False,
        workspace_id=workspace_id,
        scope=scope,
        presentation_id=None,
        video_project_id=None,
        custom_dashboard_id=None,
        fleet_id=None,
    )
    return {"conversations": [{**row.model_dump(), "conversation_id": row.id} for row in rows]}


@router.patch("/chat/sessions/{conversation_id}")
async def update_web_chat_session(
    conversation_id: str,
    body: chat_api.ConversationUpdate,
    user: User = Depends(web_chat_user),
    db: AsyncSession = Depends(get_db),
) -> workspaces_api.PlatformChatSessionUpdated:
    """Rename a conversation, or file it into a workspace."""
    if not body.model_fields_set:
        raise HTTPException(status_code=422, detail="Nothing to update: send title or workspace_id.")
    updated = await chat_api.update_conversation(conversation_id=conversation_id, body=body, user=user, db=db)
    return workspaces_api.PlatformChatSessionUpdated(
        conversation_id=updated.id, title=updated.title, workspace_id=updated.workspace_id
    )


@router.delete("/chat/sessions/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_web_chat_session(
    conversation_id: str,
    user: User = Depends(web_chat_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await chat_api.archive_conversation(conversation_id=conversation_id, user=user, db=db)


@router.delete("/chat/sessions", status_code=status.HTTP_204_NO_CONTENT)
async def archive_all_web_chat_sessions(
    user: User = Depends(web_chat_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Archive every conversation the web sidebar lists."""
    await chat_api.archive_user_conversations(db, user, web_listed_only=True)


@router.get("/chat/sessions/{conversation_id}/messages")
async def list_web_chat_messages(
    conversation_id: str,
    before: str | None = Query(default=None, description="Messages strictly older than this message id."),
    limit: int = Query(default=50, ge=1, le=chat_api.MAX_MESSAGE_PAGE_SIZE),
    supports_artifacts: bool = Query(default=False),
    user: User = Depends(web_chat_user),
    db: AsyncSession = Depends(get_db),
):
    """One page of a conversation the caller may read, older than ``before``, restore-shaped.

    ``{messages, has_more}``.
    """
    conv = await _readable_conversation(db, user, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return await platform_api.read_message_page(
        db, user, conv, before=before, limit=limit, supports_artifacts=supports_artifacts, keep_dashboards=True
    )


@router.post("/chat/upload")
async def upload_web_chat_file(
    conversation_id: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    user: User = Depends(web_chat_user),
    db: AsyncSession = Depends(get_db),
    _chat_gate: None = Depends(require_chat_enabled),
):
    """Store a file for the next message of the conversation: ``{"file_ids": [id]}``."""
    stored = await chat_api.upload_file_to_conversation(
        conversation_id=conversation_id, file=file, create_message=False, user=user, db=db, _ai_gate=None
    )
    return {**stored, "file_ids": [stored["file_id"]]}


# ── The package's other reads, served by the existing handlers ────────
router.add_api_route(
    "/chat/quota", platform_api.get_platform_chat_quota, methods=["GET"], response_model=None
)
router.add_api_route(
    "/chat/prompts", platform_api.list_platform_chat_prompts_route, methods=["GET"]
)
router.add_api_route(
    "/chat/suggestions",
    chat_api.get_chat_suggestions,
    methods=["GET"],
    response_model=list[str],
)
router.add_api_route(
    "/chat/conversation-references",
    chat_api.search_conversation_references,
    methods=["GET"],
)
router.add_api_route(
    "/chat/artifacts/{kind}/{artifact_id}",
    platform_api.get_platform_chat_artifact,
    methods=["GET"],
)
router.add_api_route(
    "/chat/artifacts/{kind}/{artifact_id}/download",
    platform_api.download_platform_chat_artifact,
    methods=["POST"],
)
router.add_api_route(
    "/chat/workspaces", workspaces_api.list_platform_chat_workspaces, methods=["GET"]
)
router.add_api_route(
    "/chat/workspaces",
    workspaces_api.create_platform_chat_workspace,
    methods=["POST"],
    status_code=status.HTTP_201_CREATED,
)
router.add_api_route(
    "/chat/workspaces/{workspace_id}",
    workspaces_api.update_platform_chat_workspace,
    methods=["PATCH"],
)
router.add_api_route(
    "/chat/workspaces/{workspace_id}",
    workspaces_api.delete_platform_chat_workspace,
    methods=["DELETE"],
    status_code=status.HTTP_204_NO_CONTENT,
)
