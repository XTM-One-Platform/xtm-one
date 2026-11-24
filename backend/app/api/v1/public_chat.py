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

"""Public chat API – unauthenticated endpoints for web-embedded agents.

These endpoints power the embeddable chat widget (widget.js) that can be
placed on any external website.  Authentication is by ``web_token`` — a
unique secret generated when an agent's web exposure is first enabled.

CORS is wide-open (``*``) because the widget runs on arbitrary third-party
domains.  No JWT required.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import pathlib
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.v1.helpers import parse_conversation_id, parse_path_id
from app.core.config import settings
from app.core.constants import TRUNCATE_LABEL, TRUNCATE_PREVIEW, TRUNCATE_SNIPPET
from app.core.database import async_session
from app.core.rate_limit import enforce_rate_limit
from app.core.utils import chat_processing_key, format_exception_message, safe_uuid
from app.llm.attribution import provider_config_metadata
from app.models.agent import Agent
from app.models.chat import Conversation
from app.models.user import User
from app.services import widget_uploads
from app.services.branding import get_cached_branding
from app.services.chat_turn_stream import slim_frame
from app.services.content_safety import wrap_memory
from app.services.prompt_guard import SAFETY_SYSTEM_PROMPT, scan_user_input

logger = logging.getLogger(__name__)
router = APIRouter()

#: Headers of the widget's SSE answers. ``no-transform`` and
#: ``X-Accel-Buffering: no`` keep a reverse proxy from compressing or
#: buffering the stream, which would land the answer in bursts.
PUBLIC_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
    "Access-Control-Allow-Origin": "*",
}

_STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "static"
WIDGET_JS_PATH = _STATIC_DIR / "widget.js"
LOGO_SVG_PATH = _STATIC_DIR / "images" / "logo.svg"

# ---------------------------------------------------------------------------
# Hardening (SEC-1) — rate limits + prompt injection blocking
#
# These endpoints are unauthenticated and CORS-open, so they get the most
# conservative treatment on the platform: fixed-window rate limits on
# session creation and messaging, and outright rejection of high-severity
# prompt injection attempts (authenticated chat only logs them).
# ---------------------------------------------------------------------------

# Session creation — per client IP.
PUBLIC_SESSION_RATE_LIMIT = 10
PUBLIC_SESSION_RATE_WINDOW_SECONDS = 3600

# Message send — per client IP (across all agents / web tokens).
PUBLIC_MESSAGE_IP_RATE_LIMIT = 30
PUBLIC_MESSAGE_IP_RATE_WINDOW_SECONDS = 60

# Message send — per web_token (the fixed-window helper additionally scopes
# the bucket per client IP, so this caps each token per visitor).
PUBLIC_MESSAGE_TOKEN_RATE_LIMIT = 20
PUBLIC_MESSAGE_TOKEN_RATE_WINDOW_SECONDS = 60

# File download - per client IP.
PUBLIC_FILE_IP_RATE_LIMIT = 60
PUBLIC_FILE_IP_RATE_WINDOW_SECONDS = 60

# Visitor file upload - per client IP, and per widget across all its
# visitors (an address rotated to draw fresh buckets still meets the second).
# Both fail closed: an upload that cannot be counted is refused.
PUBLIC_UPLOAD_IP_RATE_LIMIT = 20
PUBLIC_UPLOAD_IP_RATE_WINDOW_SECONDS = 3600
PUBLIC_UPLOAD_WIDGET_RATE_LIMIT = 200
PUBLIC_UPLOAD_WIDGET_RATE_WINDOW_SECONDS = 3600

# Message metadata key holding the files a widget answer shows. Kept
# apart from ``attachments``: those chips download through the platform's
# message-scoped route, which an owner-less widget conversation never passes.
WIDGET_FILES_KEY = "widget_files"
# What a visitor typed, when the message stored for the model also carries the
# text of the documents they attached: the transcript shows this.
WIDGET_TEXT_KEY = "widget_text"

#: The one answer for a widget file the session may not have - unknown,
#: malformed, another session's or a platform user's.
WIDGET_FILE_NOT_FOUND = "File not found"

# Polite refusal returned when a public message is blocked as a
# high-severity prompt injection attempt.
PUBLIC_INJECTION_REFUSAL = (
    "I'm sorry, but I can't help with that request. "
    "Please rephrase your question and I'll do my best to assist."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cors(response: Response) -> Response:
    """Attach permissive CORS headers for cross-origin widget usage."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


def _json(data: dict, status: int = 200) -> JSONResponse:
    resp = JSONResponse(content=data, status_code=status)
    return _cors(resp)


async def _resolve_agent(token: str, db: AsyncSession | None = None) -> Agent:
    """Lookup an agent by web_token.  Raises 404 / 403 as appropriate.

    When *db* is provided the caller's session is reused, avoiding an
    extra checkout from the connection pool.
    """

    async def _query(session: AsyncSession) -> Agent | None:
        result = await session.execute(select(Agent).where(Agent.web_token == token))
        return result.scalar_one_or_none()

    agent = await _query(db) if db else None
    if agent is None and db is None:
        async with async_session() as fresh:
            agent = await _query(fresh)

    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not agent.web_enabled:
        raise HTTPException(status_code=403, detail="Agent web access is disabled")
    if not agent.enabled:
        raise HTTPException(status_code=403, detail="Agent is disabled")
    return agent


def _widget_file_filter(conversation_id: uuid.UUID):
    """SQL predicate: the files a widget session may download.

    Bound to its web_embed conversation and owned by nobody - what the
    widget's own tools persist. A widget conversation has no owner,
    so it can never be shared or org-linked: on the platform side these rows
    stay readable by admins only.
    """
    from app.models.file_attachment import FileAttachment

    return and_(
        FileAttachment.conversation_id == conversation_id,
        FileAttachment.user_id.is_(None),
    )


def _widget_file_entry(row) -> dict:
    """One file as the widget shows it: a chip downloading through the session's route."""
    return {
        "file_id": str(row.id),
        "filename": row.filename,
        "content_type": row.content_type,
        "size": row.size,
    }


async def _widget_turn_files(
    conversation_id: uuid.UUID,
    file_context,
    known: set[str] | frozenset[str] = frozenset(),
    reply_text: str | None = None,
) -> list[dict]:
    """The files this turn produced that its widget session may download.

    *known* holds the files the session had before the turn (the visitor's
    uploads included, :func:`_load_session_files`): they are not this answer's.
    A file the turn only fetched is shown when *reply_text* names it.
    Read on a short session of its own, so a failed read never poisons the
    one the reply is stored on. Never raises: a failure costs the chips.
    """
    from app.models.file_attachment import FileAttachment
    from app.tools.base import files_for_reply

    ids = [
        file_uuid
        for file_uuid in (
            safe_uuid(file_id)
            for file_id in files_for_reply(
                file_context,
                [ref.file_id for ref in file_context.list_files() if ref.file_id not in known],
                reply_text,
            )
        )
        if file_uuid is not None
    ]
    if not ids:
        return []
    try:
        async with async_session() as db:
            rows = (
                await db.execute(
                    select(FileAttachment)
                    .where(FileAttachment.id.in_(ids), _widget_file_filter(conversation_id))
                    .order_by(FileAttachment.created_at)
                )
            ).scalars().all()
    except Exception:
        logger.warning("Could not load the files of a public chat answer", exc_info=True)
        return []
    return [_widget_file_entry(row) for row in rows]


async def _bind_widget_answer_files(db, message, conversation_id: uuid.UUID, files: list[dict]) -> list[dict]:
    """Link the files *message* shows to it; return the entries it shows.

    A row is shown by one message at most (``sub_agent_files.attachable_filter``)
    and a row no message shows is the one a later write of the same bytes is
    handed (``_persist_generated_file``): left unlinked, a file regenerated in a
    later turn came back as this answer's row, which that turn's session
    already held, and the later answer showed nothing. In a SAVEPOINT, so a
    failed link costs the link, never the answer or its chips; an entry
    another message shows already is dropped. Does not commit.
    """
    from types import SimpleNamespace

    from app.services.sub_agent_files import bind_attachment_rows

    rows = [
        SimpleNamespace(id=file_uuid)
        for file_uuid in (safe_uuid(entry.get("file_id")) for entry in files)
        if file_uuid is not None
    ]
    message_id = getattr(message, "id", None)
    if not rows or message_id is None:
        return files
    try:
        async with db.begin_nested():
            bound = await bind_attachment_rows(
                db, rows, conversation_id=conversation_id, message_id=message_id
            )
    except Exception:
        logger.warning("Could not link the files of a public chat answer to it", exc_info=True)
        return files
    bound_ids = {str(row.id) for row in bound}
    shown = [entry for entry in files if entry.get("file_id") in bound_ids]
    if len(shown) != len(files):
        message.metadata_json = json.dumps({WIDGET_FILES_KEY: shown}) if shown else None
    return shown


async def _load_session_files(conversation_id: uuid.UUID, file_context) -> set[str]:
    """Put the files of this widget session into *file_context*; return their ids.

    What ``load_conversation_files`` does for the web chat: the uploads the
    visitor sent and the files earlier answers produced are the turn's to hand
    to a tool by id. An upload not sent with a message is not: the visitor may
    have taken it back (the turn's own files are handed over by the caller).
    Never raises: a failure costs the earlier files, not the turn.
    """
    from sqlalchemy import or_

    from app.llm.base import FileRef
    from app.models.file_attachment import FileAttachment

    try:
        async with async_session() as db:
            rows = (
                await db.execute(
                    select(FileAttachment)
                    .where(
                        _widget_file_filter(conversation_id),
                        or_(
                            FileAttachment.source.is_distinct_from(widget_uploads.WIDGET_UPLOAD_SOURCE),
                            FileAttachment.message_id.is_not(None),
                        ),
                    )
                    .order_by(FileAttachment.created_at)
                )
            ).scalars().all()
    except Exception:
        logger.warning("Could not load the files of a public chat session", exc_info=True)
        return set()
    for row in rows:
        file_context.add(FileRef.from_attachment(row))
    return {str(row.id) for row in rows}


def _refused(refusal: widget_uploads.UploadRefusedError) -> HTTPException:
    headers = {"Retry-After": str(refusal.retry_after)} if refusal.retry_after else None
    return HTTPException(status_code=refusal.status_code, detail=refusal.detail, headers=headers)


async def _visitor_uploads(
    db: AsyncSession, agent: Agent, conversation_id: uuid.UUID, file_ids: list[str] | None
) -> tuple[list, dict[str, str]]:
    """The uploads a visitor's message attaches and their documents' text, before anything is stored."""
    if not file_ids:
        return [], {}
    if not widget_uploads.uploads_allowed(agent):
        raise HTTPException(status_code=403, detail=widget_uploads.UPLOADS_OFF_DETAIL)
    from app.core.database import release_db_connection_before_loop

    try:
        rows = await widget_uploads.load_pending_uploads(
            db, agent_id=agent.id, conversation_id=conversation_id, file_ids=file_ids
        )
        # The documents are read from the object store: no transaction waits on it.
        await release_db_connection_before_loop(db)
        return rows, await widget_uploads.document_texts(rows)
    except widget_uploads.UploadRefusedError as refusal:
        raise _refused(refusal) from None


def _with_visitor_files(content: str, texts: list[str]) -> str:
    """The visitor's message as the model reads it: what they typed, then the text of each document they attached."""
    return "\n\n".join([content, *texts]) if texts else content


async def _attach_visitor_uploads(
    db: AsyncSession, message, conversation_id: uuid.UUID, rows: list, texts: dict[str, str]
) -> tuple[list, list[str]]:
    """Show *rows* on the visitor's *message*; the refs and document texts the turn hands the agent.

    A document reaches the model as its text alone - the text the upload's
    guard screened (``widget_uploads.document_text``) - never as its native
    bytes, so the message stores that text after what the visitor typed (read
    again from history on the next turns) and the file is only bound to it. An
    image is shown by its pixels: bound with the web chat's
    ``attach_rows_to_message``, whose ``attachments`` block rehydrates it on
    the next turns. The widget's own key shows every file as a chip, the
    visitor's own words under ``widget_text``. When a row cannot be bound
    (another message of the session took it meanwhile) the message is refused
    with ``widget_uploads.UploadRefusedError``, and the caller rolls back what
    this bound: a message never goes out with fewer files than the visitor sent.
    """
    if not rows:
        return [], []
    from app.llm.base import FileRef
    from app.services.sub_agent_files import attach_rows_to_message, bind_attachment_rows

    images = [row for row in rows if widget_uploads.is_image(row.content_type)]
    documents = [row for row in rows if not widget_uploads.is_image(row.content_type)]
    bound = await attach_rows_to_message(db, message, images, conversation_id=conversation_id)
    if documents:
        try:
            async with db.begin_nested():
                bound_documents = await bind_attachment_rows(
                    db, documents, conversation_id=conversation_id, message_id=message.id
                )
        except Exception:
            logger.exception("Failed to bind the documents of a public widget visitor message")
            bound_documents = []
        bound += bound_documents
    if len(bound) != len(rows):
        raise widget_uploads.UploadRefusedError(400, widget_uploads.FILE_GONE_DETAIL)
    bound_ids = {row.id for row in bound}
    ordered = [row for row in rows if row.id in bound_ids]
    shown = [texts[str(row.id)] for row in ordered if str(row.id) in texts]
    try:
        meta = json.loads(message.metadata_json) if message.metadata_json else {}
    except (TypeError, ValueError):
        meta = {}
    meta[WIDGET_FILES_KEY] = [_widget_file_entry(row) for row in ordered]
    if shown:
        meta[WIDGET_TEXT_KEY] = message.content
        message.content = _with_visitor_files(message.content, shown)
    message.metadata_json = json.dumps(meta)
    return [FileRef.from_attachment(row) for row in ordered], shown


async def _turn_history(db: AsyncSession, conversation_id: uuid.UUID, message) -> list:
    """The history a widget turn reads: the messages stored before the visitor's *message*.

    Called once the message is committed, off the conversation lock
    ``store_message`` held until then - the history clears a stale compaction
    watermark in a session of its own that takes that lock - and bounded at
    the message's sequence in the query, so what another send stored after it
    never takes a place in the window (the turn appends the message itself).
    Read in a savepoint: a failed read costs the turn its context, logged,
    never its answer, and leaves the session usable.
    """
    from app.channels.router import get_conversation_history

    try:
        async with db.begin_nested():
            return await get_conversation_history(
                db, conversation_id, before_sequence=getattr(message, "sequence", None)
            )
    except Exception:
        logger.warning(
            "Could not read the history of public chat session %s: the turn runs without it",
            conversation_id,
            exc_info=True,
        )
        return []


def _public_message(message) -> dict:
    """One transcript message as the widget reads it, with its files."""
    out = {"role": message.role, "content": message.content}
    try:
        meta = json.loads(message.metadata_json) if message.metadata_json else None
    except (TypeError, ValueError):
        meta = None
    typed = meta.get(WIDGET_TEXT_KEY) if isinstance(meta, dict) and message.role == "user" else None
    if isinstance(typed, str):
        out["content"] = typed
    files = meta.get(WIDGET_FILES_KEY) if isinstance(meta, dict) else None
    if isinstance(files, list) and files:
        out["files"] = [f for f in files if isinstance(f, dict)]
    return out


async def _get_admin_user_id(db: AsyncSession) -> str | None:
    """Return the platform admin user's ID (from ADMIN_EMAIL env).

    Web-embedded agent consumption is attributed to this user in Langfuse.
    """
    result = await db.execute(
        select(User).where(func.lower(User.email) == settings.admin_email)
    )
    admin = result.scalar_one_or_none()
    return str(admin.id) if admin else None


def _public_tools_allowed(agent: Agent) -> bool:
    """Whether integration / MCP tools may be exposed to public visitors.

    Default-DENY: anonymous internet users must never be able to drive an
    agent's integration credentials (GitHub, Microsoft, OpenCTI, ...) or MCP
    servers unless the agent owner explicitly opted in by setting
    ``channel_config["web_embed"]["allow_tools"] = true``.

    The config is read defensively — any missing key, wrong type, or
    non-``True`` value resolves to ``False``.
    """
    config = agent.channel_config
    if not isinstance(config, dict):
        return False
    web_cfg = config.get("web_embed")
    if not isinstance(web_cfg, dict):
        return False
    return web_cfg.get("allow_tools") is True


def _injection_refusal(content: str, context: dict | None = None) -> str | None:
    """Scan a public message for prompt injection; return a refusal if blocked.

    Unlike authenticated chat (log-only), the public/anonymous context
    justifies blocking: high-severity detections are rejected with a polite
    refusal, lower severities are logged and allowed through (the
    ``SAFETY_SYSTEM_PROMPT`` in the system prompt remains the backstop).

    The optional page *context* is scanned too — it is fully attacker
    controlled (arbitrary JSON from the embedding page) and gets injected
    into the **system prompt**, making it a more privileged injection
    vector than the user message itself.
    """
    texts = [("message", content)]
    if context:
        with contextlib.suppress(TypeError, ValueError):
            texts.append(("context", json.dumps(context, default=str)))
    for label, text in texts:
        result = scan_user_input(text)
        if not result.flagged:
            continue
        if result.severity == "high":
            logger.warning(
                "Blocked public chat %s: prompt injection detected "
                "(severity=%s, reasons=%s)",
                label,
                result.severity,
                result.reasons,
            )
            return PUBLIC_INJECTION_REFUSAL
        logger.warning(
            "Public chat %s flagged as possible prompt injection "
            "(severity=%s, reasons=%s)",
            label,
            result.severity,
            result.reasons,
        )
    return None


def _build_public_system_prompt(
    agent: Agent,
    *,
    visitor_memory_text: str = "",
    context: dict | None = None,
    integration_lines: list[str] | None = None,
    kb_lines: list[str] | None = None,
    files_section: str = "",
    integration_tools: bool = False,
) -> str:
    """Assemble the system prompt for a public (unauthenticated) chat.

    The ``SAFETY_SYSTEM_PROMPT`` is always appended **last** so it survives
    any prompt manipulation attempted through earlier sections (visitor
    memory, page context, etc.) — same placement as authenticated chat.
    *files_section* is the session's files as the web chat lists them
    (``FileContext.to_prompt_section``), empty when it has none.
    *integration_tools* says the turn carries the agent's integration tools,
    whose answers come back quarantined.
    """
    system_parts = [agent.persona or "You are a helpful AI assistant."]

    # The visitor's own chats write this memory: it is fenced.
    if visitor_memory_text:
        system_parts.append(f"\n{wrap_memory(visitor_memory_text)}")

    system_parts.append("\nPublic chat widget. Be concise and professional.")

    if context:
        system_parts.append("\nPage context:\n" + json.dumps(context, default=str))

    if integration_lines:
        system_parts.append(
            "\nConnected integrations:\n" + "\n".join(integration_lines)
        )

    if kb_lines:
        system_parts.append("\nKnowledge bases:\n" + "\n".join(kb_lines))

    if files_section:
        system_parts.append(files_section)

    # The file names are quarantined there, as in the web chat, and
    # integration answers come back quarantined.
    from app.services.content_safety import (
        UNTRUSTED_CONTENT_POLICY,
        untrusted_policy_needed,
    )

    if untrusted_policy_needed(
        quarantined="<<UNTRUSTED-" in files_section,
        integration_tools=integration_tools,
    ):
        system_parts.append(UNTRUSTED_CONTENT_POLICY)

    # ── Prompt injection safety layer (always-on, always last) ──
    system_parts.append(SAFETY_SYSTEM_PROMPT)

    return "\n".join(system_parts)


# ---------------------------------------------------------------------------
# CORS preflight
# ---------------------------------------------------------------------------


@router.options("/{path:path}")
async def cors_preflight(path: str):
    return _json({})


# ---------------------------------------------------------------------------
# Widget JS delivery
# ---------------------------------------------------------------------------


@router.get("/widget.js")
async def serve_widget():
    """Serve the embeddable chat widget script."""
    if not WIDGET_JS_PATH.exists():
        raise HTTPException(status_code=404, detail="Widget not found")
    response = FileResponse(WIDGET_JS_PATH, media_type="application/javascript")
    response.headers["Cache-Control"] = "public, max-age=3600"
    return _cors(response)


@router.get("/logo.svg")
async def serve_logo():
    """Serve the Filigran logo for the widget button."""
    if not LOGO_SVG_PATH.exists():
        raise HTTPException(status_code=404, detail="Logo not found")
    response = FileResponse(LOGO_SVG_PATH, media_type="image/svg+xml")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return _cors(response)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    visitor_id: str | None = None


class PublicMessageRequest(BaseModel):
    content: str
    context: dict | None = None
    # Uploads of this session the message attaches.
    file_ids: list[str] | None = Field(default=None, max_length=widget_uploads.SESSION_MAX_FILES)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/chat/{web_token}/config")
async def get_public_config(web_token: str):
    """Return public info about the agent (no auth required).

    ``platform_name`` comes from :func:`get_cached_branding`, which
    resolves the admin-editable brand (Settings → Branding) and falls
    back to the deployment-mode default. ``strict_rendering`` tells the
    widget how to render answers (Settings > Access and security, or
    ``PUBLIC_WIDGET_STRICT_RENDERING``, which wins). The default is reported
    by the Security Audit, never logged: it is a choice, not an incident.
    ``uploads`` is what a visitor may attach when the widget allows it
    (``widget_uploads.upload_rules``), ``null`` otherwise: the widget offers
    no attach control then.
    """
    from app.core.security_options import security_option

    async with async_session() as db:
        agent = await _resolve_agent(web_token, db)
    strict_rendering = security_option("public_widget_strict_rendering")
    return _json(
        {
            "name": agent.name,
            "description": agent.description,
            "icon": agent.icon,
            "suggested_prompts": agent.web_suggested_prompts or [],
            "platform_name": get_cached_branding().name,
            "strict_rendering": strict_rendering,
            "uploads": widget_uploads.upload_rules() if widget_uploads.uploads_allowed(agent) else None,
        }
    )


@router.post("/chat/{web_token}/sessions")
async def create_session(
    web_token: str,
    request: Request,
    body: CreateSessionRequest | None = None,
):
    """Create a new anonymous chat session with the agent.

    Accepts an optional ``visitor_id`` (a UUID generated and persisted by
    the widget).  The visitor_id is stored in ``channel_thread_id`` so
    that we can later look up the most recent session for a returning
    visitor even if their original session_id was lost from local storage.
    """
    await enforce_rate_limit(
        request,
        "public-chat-session",
        limit=PUBLIC_SESSION_RATE_LIMIT,
        window_seconds=PUBLIC_SESSION_RATE_WINDOW_SECONDS,
    )

    # Inline rather than a dependency, matching how this module already gates
    # chat availability: it takes no ``Depends(get_db)`` and opens its own
    # sessions. A session created with AI off could never be answered.
    from app.services.agentic_gate import refuse_ai_disabled
    from app.services.ai_state import is_ai_enabled

    async with async_session() as _gate_db:
        if not await is_ai_enabled(_gate_db):
            refuse_ai_disabled()

    visitor_id = body.visitor_id if body else None

    async with async_session() as db:
        agent = await _resolve_agent(web_token, db)
        conv = Conversation(
            user_id=None,
            channel="web_embed",
            agent_id=agent.id,
            channel_thread_id=visitor_id,
            title=f"Web chat with {agent.name}",
        )
        db.add(conv)
        await db.commit()
        return _json({"session_id": str(conv.id)}, status=201)


@router.get("/chat/{web_token}/visitor/{visitor_id}/latest")
async def get_latest_visitor_session(web_token: str, visitor_id: str):
    """Find the most recent session for a returning visitor.

    The widget calls this as a **fallback** when it has a visitor_id (from
    localStorage / cookie) but the stored session_id was lost.  This
    enables conversation continuity even after the user clears site data
    or switches browsers on the same device.
    """
    async with async_session() as db:
        agent = await _resolve_agent(web_token, db)

        result = await db.execute(
            select(Conversation)
            .where(
                Conversation.agent_id == agent.id,
                Conversation.channel == "web_embed",
                Conversation.channel_thread_id == visitor_id,
            )
            .options(selectinload(Conversation.messages))
            .order_by(Conversation.updated_at.desc())
            .limit(1)
        )
        conv = result.scalar_one_or_none()
        if not conv:
            return _json({"session_id": None, "messages": []})

        messages = [
            _public_message(m)
            for m in sorted(conv.messages, key=lambda m: m.sequence)
            if m.role in ("user", "assistant")
        ]

        from app.core.redis import redis_client as _get_redis_visitor

        try:
            _r = await _get_redis_visitor()
            _is_proc = bool(await _r.exists(chat_processing_key(conv.id)))
        except Exception:
            _is_proc = False

        return _json(
            {
                "session_id": str(conv.id),
                "messages": messages,
                "is_processing": _is_proc,
            }
        )


@router.get("/chat/{web_token}/sessions/{session_id}")
async def get_session_messages(web_token: str, session_id: str):
    """Return all messages for an existing session."""
    async with async_session() as db:
        agent = await _resolve_agent(web_token, db)
        result = await db.execute(
            select(Conversation)
            .where(
                Conversation.id
                == parse_conversation_id(session_id, not_found_detail="Session not found"),
                Conversation.agent_id == agent.id,
                Conversation.channel == "web_embed",
            )
            .options(selectinload(Conversation.messages))
        )
        conv = result.scalar_one_or_none()
        if not conv:
            raise HTTPException(status_code=404, detail="Session not found")

        messages = [
            _public_message(m)
            for m in sorted(conv.messages, key=lambda m: m.sequence)
            if m.role in ("user", "assistant")
        ]

        from app.core.redis import redis_client as _get_redis_session

        try:
            _r = await _get_redis_session()
            _is_proc = bool(await _r.exists(chat_processing_key(conv.id)))
        except Exception:
            _is_proc = False

        return _json({"messages": messages, "is_processing": _is_proc})


@router.get("/chat/{web_token}/sessions/{session_id}/files/{file_id}")
async def download_session_file(
    web_token: str,
    session_id: str,
    file_id: str,
    request: Request,
):
    """Download a file an answer of this widget session showed.

    The capability is the one that already reads the transcript: the agent's
    ``web_token`` plus the session id. Only a row bound to THIS web_embed
    conversation of THIS agent and owned by nobody is served; another
    session's file, a platform user's file, a malformed or an unknown id all
    get the same 404.
    """
    await enforce_rate_limit(
        request,
        "public-chat-file-ip",
        limit=PUBLIC_FILE_IP_RATE_LIMIT,
        window_seconds=PUBLIC_FILE_IP_RATE_WINDOW_SECONDS,
    )
    from app.models.file_attachment import FileAttachment
    from app.services.file_responses import file_response_headers
    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    conv_uuid = parse_path_id(session_id, not_found_detail=WIDGET_FILE_NOT_FOUND)
    file_uuid = parse_path_id(file_id, not_found_detail=WIDGET_FILE_NOT_FOUND)

    async with async_session() as db:
        agent = await _resolve_agent(web_token, db)
        file_att = (
            await db.execute(
                select(FileAttachment)
                .join(Conversation, Conversation.id == FileAttachment.conversation_id)
                .where(
                    FileAttachment.id == file_uuid,
                    _widget_file_filter(conv_uuid),
                    Conversation.agent_id == agent.id,
                    Conversation.channel == "web_embed",
                )
            )
        ).scalar_one_or_none()
    if file_att is None:
        raise HTTPException(status_code=404, detail=WIDGET_FILE_NOT_FOUND)

    try:
        data = await file_storage.download(file_att.s3_object_name)
    except FileNotFoundInStorageError:
        raise HTTPException(status_code=404, detail=WIDGET_FILE_NOT_FOUND) from None
    except Exception as exc:
        logger.error("Public chat file download failed: %s", format_exception_message(exc))
        raise HTTPException(status_code=500, detail="File download failed") from None

    return _cors(
        Response(
            content=data,
            media_type=file_att.content_type or "application/octet-stream",
            headers=file_response_headers(file_att, store=False),
        )
    )


@router.get("/chat/{web_token}/sessions/{session_id}/images/{image_id}")
async def download_session_image(
    web_token: str,
    session_id: str,
    image_id: str,
    request: Request,
):
    """Serve an image the platform generated for this widget session.

    ``generate_image`` keeps its image in the database when object storage
    fails, and the answer then shows ``/api/v1/chat/images/<id>``, a route
    behind a platform sign-in; the widget loads that address through this one.
    Same capability, budget and answers as ``download_session_file``: only a
    raster image bound to THIS web_embed conversation of THIS agent and owned
    by nobody is served, and anything else gets the same 404.
    """
    await enforce_rate_limit(
        request,
        "public-chat-file-ip",
        limit=PUBLIC_FILE_IP_RATE_LIMIT,
        window_seconds=PUBLIC_FILE_IP_RATE_WINDOW_SECONDS,
    )
    from app.models.chat_image import ChatImage
    from app.services.file_responses import INLINE_FILE_CONTENT_TYPES, file_response_headers

    conv_uuid = parse_path_id(session_id, not_found_detail=WIDGET_FILE_NOT_FOUND)
    image_uuid = parse_path_id(image_id, not_found_detail=WIDGET_FILE_NOT_FOUND)

    async with async_session() as db:
        agent = await _resolve_agent(web_token, db)
        image = (
            await db.execute(
                select(ChatImage)
                .join(Conversation, Conversation.id == ChatImage.conversation_id)
                .where(
                    ChatImage.id == image_uuid,
                    ChatImage.conversation_id == conv_uuid,
                    ChatImage.user_id.is_(None),
                    Conversation.agent_id == agent.id,
                    Conversation.channel == "web_embed",
                )
            )
        ).scalar_one_or_none()
    content_type = ((image.content_type if image else None) or "").split(";", 1)[0].strip().lower()
    if image is None or content_type not in INLINE_FILE_CONTENT_TYPES:
        raise HTTPException(status_code=404, detail=WIDGET_FILE_NOT_FOUND)

    # A ChatImage has no filename: only a raster type reaches here, which the
    # helper answers inline without reading one.
    return _cors(
        Response(
            content=image.image_data,
            media_type=content_type,
            headers=file_response_headers(image, store=False),
        )
    )


async def _read_upload(request: Request) -> tuple[str | None, bytes]:
    """The name and bytes of the one file part of a visitor upload.

    Refused on the declared length before anything is read, and cut off past
    the same bound while the parser reads it (a chunked or lying body):
    nothing past it reaches memory or disk.
    """
    from starlette.datastructures import UploadFile
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from starlette.requests import ClientDisconnect

    from app.core.bounded_receive import BoundedReceive

    limit = widget_uploads.MAX_FILE_BYTES + widget_uploads.UPLOAD_FRAMING_BYTES
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail=widget_uploads.TOO_LARGE_DETAIL)
    body = BoundedReceive(request.receive, limit, refusal=widget_uploads.TOO_LARGE_DETAIL)
    try:
        form = await Request(request.scope, body).form(max_files=1, max_fields=0)
    except ClientDisconnect:
        raise HTTPException(status_code=400, detail="The upload was interrupted.") from None
    except StarletteHTTPException as exc:
        if body.exceeded:
            raise HTTPException(status_code=413, detail=widget_uploads.TOO_LARGE_DETAIL) from None
        raise HTTPException(status_code=400, detail=f"{widget_uploads.FORM_DETAIL} ({exc.detail})") from None
    try:
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise HTTPException(status_code=400, detail=widget_uploads.FORM_DETAIL)
        return file.filename, await file.read(widget_uploads.MAX_FILE_BYTES + 1)
    finally:
        await form.close()


@router.post("/chat/{web_token}/sessions/{session_id}/files")
async def upload_session_file(web_token: str, session_id: str, request: Request):
    """Attach a file to this widget session, when the widget allows it.

    One multipart ``file`` part; the visitor's next message attaches it by the
    ``file_id`` answered here. What bounds an anonymous upload is in
    ``services/widget_uploads.py``; the route runs the cheap checks first -
    the address's budget, the chat gate, the widget's switch and its own
    budget, the session, the room it has left - and only then reads the body,
    in one of the process's upload slots. Every refusal says why.
    """
    await enforce_rate_limit(
        request,
        "public-chat-upload-ip",
        limit=PUBLIC_UPLOAD_IP_RATE_LIMIT,
        window_seconds=PUBLIC_UPLOAD_IP_RATE_WINDOW_SECONDS,
        fail_closed=True,
    )
    conv_uuid = parse_conversation_id(session_id, not_found_detail="Session not found")
    async with async_session() as db:
        from app.api.v1.platform import is_chat_enabled

        if not await is_chat_enabled(db):
            raise HTTPException(status_code=403, detail="Chat is unavailable on this deployment.")
        agent = await _resolve_agent(web_token, db)
        if not widget_uploads.uploads_allowed(agent):
            raise HTTPException(status_code=403, detail=widget_uploads.UPLOADS_OFF_DETAIL)
        session_exists = (
            await db.execute(
                select(Conversation.id).where(
                    Conversation.id == conv_uuid,
                    Conversation.agent_id == agent.id,
                    Conversation.channel == "web_embed",
                )
            )
        ).scalar_one_or_none()
        if session_exists is None:
            raise HTTPException(status_code=404, detail="Session not found")
        # Past the session check: a caller holding only the public token
        # cannot spend the widget's budget for its visitors.
        await enforce_rate_limit(
            None,
            "public-chat-upload-widget",
            bucket=str(agent.id),
            limit=PUBLIC_UPLOAD_WIDGET_RATE_LIMIT,
            window_seconds=PUBLIC_UPLOAD_WIDGET_RATE_WINDOW_SECONDS,
            fail_closed=True,
        )
        admin_user_id = await _get_admin_user_id(db)

    try:
        await widget_uploads.check_room(agent.id, conv_uuid)
        async with widget_uploads.upload_slot() as permit:
            raw_filename, data = await _read_upload(request)
            row = await widget_uploads.accept_upload(
                agent=agent,
                conversation_id=conv_uuid,
                raw_filename=raw_filename,
                data=data,
                audit_anchor_user_id=admin_user_id,
                permit=permit,
            )
    except widget_uploads.UploadRefusedError as refusal:
        raise _refused(refusal) from None
    return _json(_widget_file_entry(row), status=201)


@router.post("/chat/{web_token}/sessions/{session_id}/messages")
async def send_public_message(
    web_token: str,
    session_id: str,
    body: PublicMessageRequest,
    request: Request,
):
    """Send a message in a public chat session and get the agent's response."""
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    await enforce_rate_limit(
        request,
        "public-chat-message-ip",
        limit=PUBLIC_MESSAGE_IP_RATE_LIMIT,
        window_seconds=PUBLIC_MESSAGE_IP_RATE_WINDOW_SECONDS,
    )
    await enforce_rate_limit(
        request,
        f"public-chat-message-token:{web_token}",
        limit=PUBLIC_MESSAGE_TOKEN_RATE_LIMIT,
        window_seconds=PUBLIC_MESSAGE_TOKEN_RATE_WINDOW_SECONDS,
    )

    # ── Prompt injection gate (blocking for anonymous visitors) ──
    refusal = _injection_refusal(content, body.context)
    if refusal:
        return _json({"role": "assistant", "content": refusal})

    async with async_session() as db:
        # Public web-embed chat is the same agentic loop as authenticated
        # chat — gate it on the deployment tier too. The widget renders
        # any non-2xx response as a generic "Service temporarily
        # unavailable" message, which is the right copy here.
        from app.api.v1.platform import is_chat_enabled

        if not await is_chat_enabled(db):
            raise HTTPException(
                status_code=403,
                detail="Chat is unavailable on this deployment.",
            )

        agent = await _resolve_agent(web_token, db)
        result = await db.execute(
            select(Conversation).where(
                Conversation.id
                == parse_conversation_id(session_id, not_found_detail="Session not found"),
                Conversation.agent_id == agent.id,
                Conversation.channel == "web_embed",
            )
        )
        conv = result.scalar_one_or_none()
        if not conv:
            raise HTTPException(status_code=404, detail="Session not found")
        uploads, upload_texts = await _visitor_uploads(db, agent, conv.id, body.file_ids)

        # Store user message
        from app.channels.router import store_message

        user_message = await store_message(
            db,
            conv.id,
            role="user",
            content=content,
            channel="web_embed",
        )
        try:
            turn_files, turn_texts = await _attach_visitor_uploads(db, user_message, conv.id, uploads, upload_texts)
        except widget_uploads.UploadRefusedError as refused:
            # Leaving the session rolls the message and its bindings back.
            raise _refused(refused) from None

        # Auto-title on first message
        if conv.title.startswith("Web chat with"):
            conv.title = content[:TRUNCATE_LABEL]
        # Committed first: the history clears a stale compaction watermark in a
        # session of its own that takes the conversation lock ``store_message``
        # holds until then.
        await db.commit()

        # Memory management for long conversations runs behind the quota
        # gate, in ``_run_public_agent_chat``.
        history = await _turn_history(db, conv.id, user_message)

        # Resolve visitor_id for memory (stored in channel_thread_id)
        visitor_id = conv.channel_thread_id

        # One turn per session: a message sent while a turn runs is
        # stored and answered by that turn's holder, with the next ones, as
        # one next turn.
        answer_now, turn_token = await _take_widget_turn(conv.id)
        if not answer_now:
            return _json({"role": "assistant", "content": "", "queued": True})

        # Run agent chat (re-using the existing agentic pipeline)
        from app.tools.base import FileContext

        file_context = FileContext()
        session_files = await _load_session_files(conv.id, file_context)
        session_files |= {ref.file_id for ref in turn_files}
        try:
            async with _widget_turn_keepalive(conv.id, turn_token):
                assistant_text = await _run_public_agent_chat(
                    db,
                    agent,
                    history,
                    content,
                    visitor_id=visitor_id,
                    session_id=session_id,
                    context=body.context,
                    file_context=file_context,
                    turn_files=turn_files,
                    turn_texts=turn_texts,
                    compact_conversation_id=str(conv.id),
                )
        except Exception as e:
            logger.error("Public chat error: %s", format_exception_message(e), exc_info=True)
            assistant_text = (
                "I'm sorry, I encountered an error processing your request."
            )

        files = await _widget_turn_files(conv.id, file_context, session_files, assistant_text)
        answer = await store_message(
            db,
            conv.id,
            role="assistant",
            content=assistant_text,
            channel="web_embed",
            metadata_json=json.dumps({WIDGET_FILES_KEY: files}) if files else None,
        )
        files = await _bind_widget_answer_files(db, answer, conv.id, files)
        await db.commit()

        await _finish_widget_turn(
            agent,
            conv.id,
            turn_token,
            getattr(user_message, "sequence", None),
            session_id=session_id,
            context=body.context,
            visitor_id=visitor_id,
        )

        reply: dict = {"role": "assistant", "content": assistant_text}
        if files:
            reply["files"] = files
        return _json(reply)


@router.post("/chat/{web_token}/sessions/{session_id}/messages/stream")
async def send_public_message_stream(
    web_token: str,
    session_id: str,
    body: PublicMessageRequest,
    request: Request,
):
    """Send a message with SSE streaming — tokens arrive as they are generated."""
    from starlette.responses import StreamingResponse

    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    await enforce_rate_limit(
        request,
        "public-chat-message-ip",
        limit=PUBLIC_MESSAGE_IP_RATE_LIMIT,
        window_seconds=PUBLIC_MESSAGE_IP_RATE_WINDOW_SECONDS,
    )
    await enforce_rate_limit(
        request,
        f"public-chat-message-token:{web_token}",
        limit=PUBLIC_MESSAGE_TOKEN_RATE_LIMIT,
        window_seconds=PUBLIC_MESSAGE_TOKEN_RATE_WINDOW_SECONDS,
    )

    # ── Prompt injection gate (blocking for anonymous visitors) ──
    refusal = _injection_refusal(content, body.context)
    if refusal:

        async def _refusal_stream():
            yield f"data: {json.dumps({'type': 'done', 'content': refusal})}\n\n"

        return StreamingResponse(
            _refusal_stream(),
            media_type="text/event-stream",
            headers=PUBLIC_SSE_HEADERS,
        )

    # See ``send_public_message`` — same deployment-tier gate.
    async with async_session() as _gate_db:
        from app.api.v1.platform import is_chat_enabled

        if not await is_chat_enabled(_gate_db):
            raise HTTPException(
                status_code=403,
                detail="Chat is unavailable on this deployment.",
            )

    # Pre-validate the agent outside the generator so HTTP 404/403 errors
    # are returned as proper JSON responses (not SSE error events).
    agent = await _resolve_agent(web_token)
    # The attached uploads too, with the answer that says what is wrong: the
    # session is this widget's before any of its files is read.
    uploads: list = []
    upload_texts: dict[str, str] = {}
    if body.file_ids:
        async with async_session() as upload_db:
            session_conv = (
                await upload_db.execute(
                    select(Conversation).where(
                        Conversation.id == parse_conversation_id(session_id, not_found_detail="Session not found"),
                        Conversation.agent_id == agent.id,
                        Conversation.channel == "web_embed",
                    )
                )
            ).scalar_one_or_none()
            if session_conv is None:
                raise HTTPException(status_code=404, detail="Session not found")
            uploads, upload_texts = await _visitor_uploads(upload_db, agent, session_conv.id, body.file_ids)

    # The turn this stream holds until it handed it on: a visitor who
    # leaves mid-turn cancels the stream, and the turn is given back then.
    held: dict = {}

    async def _event_stream():
        async with async_session() as db:
            session_uuid = safe_uuid(session_id)
            conv = None
            if session_uuid is not None:
                result = await db.execute(
                    select(Conversation).where(
                        Conversation.id == session_uuid,
                        Conversation.agent_id == agent.id,
                        Conversation.channel == "web_embed",
                    )
                )
                conv = result.scalar_one_or_none()
            if not conv:
                yield f"data: {json.dumps({'type': 'error', 'content': 'Session not found'})}\n\n"
                return

            from app.channels.router import store_message

            user_message = await store_message(
                db, conv.id, role="user", content=content, channel="web_embed"
            )
            try:
                turn_files, turn_texts = await _attach_visitor_uploads(
                    db, user_message, conv.id, uploads, upload_texts
                )
            except widget_uploads.UploadRefusedError as refused:
                await db.rollback()
                yield f"data: {json.dumps({'type': 'error', 'content': refused.detail, 'status': refused.status_code})}\n\n"
                return
            if conv.title.startswith("Web chat with"):
                conv.title = content[:TRUNCATE_LABEL]
            # Committed, the message is the session's with its files, and the
            # widget takes them out of its tray. A refusal answered before this
            # (the injection gate, a file gone) never says it, so the widget
            # keeps them for a retry.
            await db.commit()
            yield f"data: {json.dumps({'type': 'accepted'})}\n\n"
            # Memory management runs behind the quota gate, in
            # ``_run_public_agent_chat`` - see ``send_public_message``.
            history = await _turn_history(db, conv.id, user_message)

            visitor_id = conv.channel_thread_id

            # One turn per session; the turn marker also tells a
            # reconnecting widget a response is in flight.
            answer_now, turn_token = await _take_widget_turn(conv.id)
            if not answer_now:
                yield f"data: {json.dumps({'type': 'queued'})}\n\n"
                return
            if turn_token is not None:
                held["turn"] = (conv.id, turn_token)

            accumulated: list[str] = []

            stream_queue: asyncio.Queue[dict | None] = asyncio.Queue()
            # The streamed bubble shows what the final answer will: no raw
            # ``[[FILE:id]]`` marker, the chips carry the files.
            from app.channels.base import FileMarkerStreamFilter

            marker_filter = FileMarkerStreamFilter()

            async def _on_token_sse(chunk: str) -> None:
                accumulated.append(chunk)
                visible = marker_filter.feed(chunk)
                if visible:
                    await stream_queue.put({"type": "stream", "content": visible})

            async def _on_event_sse(event: dict) -> None:
                st = event.get("status")
                if st == "stream_retract":
                    marker_filter.flush()
                if st in (
                    "tool_start",
                    "thinking",
                    "streaming",
                    "composing",
                    "thinking_text",
                    "stream_retract",
                    "transfer_start",
                ):
                    # Slim, like every relayed copy: a ``tool_start`` carries
                    # the system prompt for the evaluation wrapper, and a
                    # ``thinking`` the server-only context detail.
                    await stream_queue.put(slim_frame({"type": "status", **event}))

            from app.tools.base import FileContext

            file_context = FileContext()
            session_files = await _load_session_files(conv.id, file_context)
            session_files |= {ref.file_id for ref in turn_files}

            async def _run_chat():
                try:
                    async with _widget_turn_keepalive(conv.id, turn_token):
                        return await _run_public_agent_chat(
                            db,
                            agent,
                            history,
                            content,
                            visitor_id=visitor_id,
                            session_id=session_id,
                            context=body.context,
                            on_token=_on_token_sse,
                            on_event=_on_event_sse,
                            file_context=file_context,
                            turn_files=turn_files,
                            turn_texts=turn_texts,
                            compact_conversation_id=str(conv.id),
                        )
                except Exception as e:
                    logger.error("Public chat stream error: %s", format_exception_message(e), exc_info=True)
                    return "I'm sorry, I encountered an error processing your request."
                finally:
                    held = marker_filter.flush()
                    if held:
                        await stream_queue.put({"type": "stream", "content": held})
                    await stream_queue.put(None)

            chat_task = asyncio.create_task(_run_chat())

            while True:
                item = await stream_queue.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item)}\n\n"

            assistant_text = await chat_task
            if not assistant_text or not assistant_text.strip():
                assistant_text = "I encountered an issue while processing your request. Please try again."

            files = await _widget_turn_files(conv.id, file_context, session_files, assistant_text)
            answer = await store_message(
                db,
                conv.id,
                role="assistant",
                content=assistant_text,
                channel="web_embed",
                metadata_json=json.dumps({WIDGET_FILES_KEY: files}) if files else None,
            )
            files = await _bind_widget_answer_files(db, answer, conv.id, files)
            await db.commit()

            held.pop("turn", None)
            await _finish_widget_turn(
                agent,
                conv.id,
                turn_token,
                getattr(user_message, "sequence", None),
                session_id=session_id,
                context=body.context,
                visitor_id=visitor_id,
            )

            done: dict = {"type": "done", "content": assistant_text}
            if files:
                done["files"] = files
            yield f"data: {json.dumps(done)}\n\n"

    async def _guarded_stream():
        try:
            async for event in _event_stream():
                yield event
        finally:
            turn = held.pop("turn", None)
            if turn is not None:
                await _release_widget_turn(*turn)

    return StreamingResponse(
        _guarded_stream(),
        media_type="text/event-stream",
        headers=PUBLIC_SSE_HEADERS,
    )


# ---------------------------------------------------------------------------
# One turn per widget session
# ---------------------------------------------------------------------------
# A widget session is answered one turn at a time, on the web chat's turn lock
# (``chat_turn_guard``, the ``chat:processing`` marker the session routes read
# as ``is_processing``). A message sent while a turn runs - a second tab, a
# reload, an API client - is stored and answered with ``queued``; the holder,
# once its own answer is stored, answers every message stored since as ONE
# next turn on the latest (its history holds the others). The sender commits
# its message before it tries the lock and the holder looks again after it
# released it, so a queued message always has a turn coming. Without Redis
# each message is its own turn, as before.

_WIDGET_FOLLOW_UPS: set[asyncio.Task] = set()

#: How often a widget turn renews its turn lock while it runs: a live turn
#: longer than the lock's TTL (``chat_turn_guard.TURN_MARKER_TTL_SECONDS``)
#: keeps it, so the restart sweep never takes a turn that is still answering.
_WIDGET_TURN_REFRESH_SECONDS = 60.0


@contextlib.asynccontextmanager
async def _widget_turn_keepalive(conversation_id: uuid.UUID, token: str | None):
    """Renew the session's turn lock every :data:`_WIDGET_TURN_REFRESH_SECONDS` while the block runs."""
    if token is None:
        yield
        return
    from app.services import chat_turn_guard

    async def _beat() -> None:
        while True:
            await asyncio.sleep(_WIDGET_TURN_REFRESH_SECONDS)
            with contextlib.suppress(Exception):
                await chat_turn_guard.refresh_turn(str(conversation_id), token)

    beat = asyncio.create_task(_beat(), name=f"widget-turn:{conversation_id}")
    try:
        yield
    finally:
        beat.cancel()


async def _take_widget_turn(conversation_id: uuid.UUID) -> tuple[bool, str | None]:
    """``(answer_now, token)``: take the session's turn, or learn a turn holds it."""
    from app.services import chat_turn_guard

    try:
        token = await chat_turn_guard.acquire_turn(str(conversation_id))
    except Exception:
        logger.warning(
            "Could not take the turn of public chat session %s: answered on its own",
            conversation_id,
            exc_info=True,
        )
        return True, None
    return token is not None, token


async def _release_widget_turn(conversation_id: uuid.UUID, token: str) -> None:
    from app.services import chat_turn_guard

    try:
        await chat_turn_guard.release_turn(str(conversation_id), token)
    except Exception:
        logger.warning("Could not release the turn of public chat session %s", conversation_id, exc_info=True)


async def _next_unanswered(conversation_id: uuid.UUID, after_sequence: int):
    """The latest visitor message stored after *after_sequence*, or ``None``."""
    from app.models.chat import Message

    try:
        async with async_session() as db:
            return (
                await db.execute(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.role == "user",
                        Message.sequence > after_sequence,
                    )
                    .order_by(Message.sequence.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
    except Exception:
        logger.warning(
            "Could not read the queued messages of public chat session %s", conversation_id, exc_info=True
        )
        return None


async def _message_images(db: AsyncSession, message_id) -> list:
    """The refs of the images bound to a stored visitor message (its documents are in its text)."""
    from app.llm.base import FileRef
    from app.models.file_attachment import FileAttachment

    try:
        rows = (
            await db.execute(select(FileAttachment).where(FileAttachment.message_id == message_id))
        ).scalars().all()
    except Exception:
        logger.warning("Could not read the images of a queued public chat message", exc_info=True)
        return []
    return [FileRef.from_attachment(row) for row in rows if widget_uploads.is_image(row.content_type)]


async def _answer_queued_message(
    agent: Agent,
    conversation_id: uuid.UUID,
    message,
    *,
    session_id: str,
    context: dict | None,
    visitor_id: str | None,
) -> None:
    """Answer *message* and every queued message before it, as one turn."""
    from app.channels.router import store_message
    from app.tools.base import FileContext

    async with async_session() as db:
        history = await _turn_history(db, conversation_id, message)
        turn_files = await _message_images(db, message.id)
        file_context = FileContext()
        session_files = await _load_session_files(conversation_id, file_context)
        try:
            # The stored text already carries its documents' text.
            assistant_text = await _run_public_agent_chat(
                db,
                agent,
                history,
                message.content,
                visitor_id=visitor_id,
                session_id=session_id,
                context=context,
                file_context=file_context,
                turn_files=turn_files,
                turn_texts=[],
                compact_conversation_id=str(conversation_id),
            )
        except Exception as e:
            logger.error("Public chat queued turn error: %s", format_exception_message(e), exc_info=True)
            assistant_text = "I'm sorry, I encountered an error processing your request."
        if not assistant_text or not assistant_text.strip():
            assistant_text = "I encountered an issue while processing your request. Please try again."
        files = await _widget_turn_files(conversation_id, file_context, session_files, assistant_text)
        answer = await store_message(
            db,
            conversation_id,
            role="assistant",
            content=assistant_text,
            channel="web_embed",
            metadata_json=json.dumps({WIDGET_FILES_KEY: files}) if files else None,
        )
        await _bind_widget_answer_files(db, answer, conversation_id, files)
        await db.commit()


async def _answer_queued(agent: Agent, conversation_id: uuid.UUID, token: str, message, **turn) -> None:
    """Hold the session's turn while queued messages wait, then give it back."""
    from app.services import chat_turn_guard

    while True:
        answered = message.sequence
        try:
            while message is not None:
                with contextlib.suppress(Exception):
                    await chat_turn_guard.refresh_turn(str(conversation_id), token)
                async with _widget_turn_keepalive(conversation_id, token):
                    await _answer_queued_message(agent, conversation_id, message, **turn)
                answered = message.sequence
                message = await _next_unanswered(conversation_id, answered)
        except asyncio.CancelledError:
            # A platform stop cut the turn: the session is given back at once,
            # and the restart sweep answers the message on the next process
            # (:func:`resume_unanswered_widget_sessions`).
            await _release_widget_turn(conversation_id, token)
            raise
        except Exception:
            # Never retried in a loop: the next visitor message takes the turn.
            logger.exception("Could not answer the queued messages of public chat session %s", conversation_id)
            await _release_widget_turn(conversation_id, token)
            return
        await _release_widget_turn(conversation_id, token)
        # A message whose sender found the turn held just before the release:
        # the turn is taken again first, so a sender that took it meanwhile
        # answers its own message and this stops.
        answer_now, token = await _take_widget_turn(conversation_id)
        if not answer_now or token is None:
            return
        message = await _next_unanswered(conversation_id, answered)
        if message is None:
            await _release_widget_turn(conversation_id, token)
            return


async def _finish_widget_turn(
    agent: Agent,
    conversation_id: uuid.UUID,
    token: str | None,
    answered_sequence: int | None,
    **turn,
) -> None:
    """After the holder stored its answer: answer what queued meanwhile, else give the turn back.

    The queued messages are answered in the background, so the holder's
    response ends with its own answer; the turn stays held until they are.
    """
    if token is None:
        return
    if answered_sequence is None:
        await _release_widget_turn(conversation_id, token)
        return
    pending = await _next_unanswered(conversation_id, answered_sequence)
    if pending is None:
        await _release_widget_turn(conversation_id, token)
        answer_now, token = await _take_widget_turn(conversation_id)
        if not answer_now or token is None:
            return
        pending = await _next_unanswered(conversation_id, answered_sequence)
        if pending is None:
            await _release_widget_turn(conversation_id, token)
            return
    task = asyncio.create_task(_answer_queued(agent, conversation_id, token, pending, **turn))
    _WIDGET_FOLLOW_UPS.add(task)
    task.add_done_callback(_WIDGET_FOLLOW_UPS.discard)


# ---------------------------------------------------------------------------
# Widget messages a platform restart left unanswered
# ---------------------------------------------------------------------------
# A worker stopped (a rollout, a recycle) or killed mid-turn takes the turn
# with it: the visitor's message is stored, its answer never is, and nothing
# else answers it - the queue of a widget session is its stored messages, read
# by the turn's holder only. The scheduler tick hands such a session to a new
# turn, the way agent account queues (``turns.orphaned``) and channel queues
# (``sweep_channel_queues``) are: a session whose last visitor-facing message
# is a visitor message, quiet for :data:`WIDGET_RESUME_AFTER_SECONDS` and no
# older than :data:`WIDGET_RESUME_MAX_AGE_SECONDS`, whose turn nobody holds.
# The turn is taken first (a send that takes it meanwhile answers the message
# itself), the session read again under it, and the message claimed in Redis:
# each visitor message is resumed once, whatever the workers and the ticks.

#: A visitor message this quiet with no turn holding its session lost its turn.
WIDGET_RESUME_AFTER_SECONDS = 60
#: Older than this, the visitor left: the answer is not sent to nobody.
WIDGET_RESUME_MAX_AGE_SECONDS = 3600
#: The most sessions one sweep resumes.
WIDGET_RESUME_BATCH = 20
#: How long a resumed message stays claimed (one resume per message).
WIDGET_RESUME_CLAIM_TTL_SECONDS = 24 * 3600


def widget_resume_claim_key(message_id: object) -> str:
    return f"public_chat:widget_resume:{message_id}"


async def _sessions_left_unanswered(now: datetime) -> list[tuple[uuid.UUID, uuid.UUID, str | None]]:
    """``(session, agent, visitor)`` of the widget sessions whose last message is a visitor's."""
    from app.models.chat import Message

    newest = now - timedelta(seconds=WIDGET_RESUME_AFTER_SECONDS)
    oldest = now - timedelta(seconds=WIDGET_RESUME_MAX_AGE_SECONDS)
    async with async_session() as db:
        # ``store_message`` touches ``updated_at`` with every message, so it is
        # the session's last message time; ``user_id IS NULL`` and
        # ``is_archived`` lead ``ix_conversations_user_archived_updated``.
        sessions = (
            await db.execute(
                select(Conversation.id, Conversation.agent_id, Conversation.channel_thread_id)
                .where(
                    Conversation.user_id.is_(None),
                    Conversation.is_archived.is_(False),
                    Conversation.updated_at >= oldest,
                    Conversation.updated_at <= newest,
                    Conversation.channel == "web_embed",
                    Conversation.agent_id.is_not(None),
                )
                .order_by(Conversation.updated_at.desc())
                .limit(WIDGET_RESUME_BATCH * 5)
            )
        ).all()
        if not sessions:
            return []
        ids = [row[0] for row in sessions]
        spoken = ("user", "assistant")
        latest = (
            select(Message.conversation_id, func.max(Message.sequence).label("sequence"))
            .where(Message.conversation_id.in_(ids), Message.role.in_(spoken))
            .group_by(Message.conversation_id)
            .subquery()
        )
        last_roles = (
            await db.execute(
                select(Message.conversation_id, Message.role)
                .join(
                    latest,
                    and_(
                        Message.conversation_id == latest.c.conversation_id,
                        Message.sequence == latest.c.sequence,
                    ),
                )
                .where(Message.role.in_(spoken))
            )
        ).all()
    answered = {cid for cid, role in last_roles if role == "assistant"}
    asked = {cid for cid, role in last_roles if role == "user"} - answered
    return [(row[0], row[1], row[2]) for row in sessions if row[0] in asked][:WIDGET_RESUME_BATCH]


async def _latest_unanswered(conversation_id: uuid.UUID):
    """The session's last visitor message when nothing answered it yet, else ``None``."""
    from app.models.chat import Message

    try:
        async with async_session() as db:
            last = (
                await db.execute(
                    select(Message)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.role.in_(("user", "assistant")),
                    )
                    .order_by(Message.sequence.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
    except Exception:
        logger.warning("Could not read public chat session %s to resume it", conversation_id, exc_info=True)
        return None
    return last if last is not None and last.role == "user" else None


async def _claim_widget_resume(message_id: object) -> bool:
    """Claim the one resume of a visitor message; ``False`` when it had it (or Redis is down)."""
    from app.core.redis import redis_client

    try:
        r = await redis_client()
        return bool(
            await r.set(widget_resume_claim_key(message_id), "1", nx=True, ex=WIDGET_RESUME_CLAIM_TTL_SECONDS)
        )
    except Exception:
        logger.debug("Could not claim the resume of public chat message %s", message_id, exc_info=True)
        return False


async def _resumable_agent(agent_id: uuid.UUID) -> Agent | None:
    """The session's agent while it still answers its widget, else ``None``."""
    try:
        async with async_session() as db:
            agent = await db.get(Agent, agent_id)
    except Exception:
        logger.warning("Could not read agent %s to resume its widget session", agent_id, exc_info=True)
        return None
    if agent is None or not agent.enabled or not agent.web_enabled:
        return None
    return agent


async def _widget_chat_enabled() -> bool:
    from app.api.v1.platform import is_chat_enabled

    try:
        async with async_session() as db:
            return bool(await is_chat_enabled(db))
    except Exception:
        return False


async def _resume_widget_session(
    conversation_id: uuid.UUID, agent_id: uuid.UUID, visitor_id: str | None, token: str
) -> bool:
    """Answer the session's unanswered message under *token*; whether a turn was started."""
    message = await _latest_unanswered(conversation_id)
    if message is None:
        return False
    agent = await _resumable_agent(agent_id)
    if agent is None:
        return False
    if not await _claim_widget_resume(message.id):
        return False
    logger.warning(
        "Public chat session %s: resuming the visitor message a stopped turn left unanswered",
        conversation_id,
    )
    task = asyncio.create_task(
        _answer_queued(
            agent,
            conversation_id,
            token,
            message,
            session_id=str(conversation_id),
            # The page context of the cut request is not stored.
            context=None,
            visitor_id=visitor_id,
        )
    )
    _WIDGET_FOLLOW_UPS.add(task)
    task.add_done_callback(_WIDGET_FOLLOW_UPS.discard)
    return True


async def resume_unanswered_widget_sessions(now: datetime | None = None) -> int:
    """Answer once the widget messages a stopped or killed turn left unanswered; how many.

    Run by the scheduler tick while agentic AI runs (``autonomous``). A
    process asked to stop takes nothing: a turn started there is cut again.
    Without Redis nothing is resumed - no lock, no claim, so nothing proves
    the message is answered once. Never raises.
    """
    from app.core.worker_shutdown import shutdown_request

    if shutdown_request() is not None:
        return 0
    try:
        sessions = await _sessions_left_unanswered(now or datetime.now(UTC))
    except Exception:
        logger.warning("Could not list the unanswered public chat sessions", exc_info=True)
        return 0
    if not sessions or not await _widget_chat_enabled():
        return 0
    resumed = 0
    for conversation_id, agent_id, visitor_id in sessions:
        answer_now, token = await _take_widget_turn(conversation_id)
        if not answer_now or token is None:
            continue
        try:
            started = await _resume_widget_session(conversation_id, agent_id, visitor_id, token)
        except Exception:
            logger.warning("Could not resume public chat session %s", conversation_id, exc_info=True)
            started = False
        if started:
            resumed += 1
        else:
            await _release_widget_turn(conversation_id, token)
    return resumed


# ---------------------------------------------------------------------------
# Agent chat (public variant — no user context)
# ---------------------------------------------------------------------------


async def _run_public_agent_chat(
    db: AsyncSession,
    agent: Agent,
    history: list,
    user_message: str,
    *,
    visitor_id: str | None = None,
    session_id: str | None = None,
    context: dict | None = None,
    on_token=None,
    on_event=None,
    file_context=None,
    turn_files: list | None = None,
    turn_texts: list[str] | None = None,
    compact_conversation_id: str | None = None,
) -> str:
    """Run one widget turn, metered like every agentic request.

    A visitor has no account, so the one agentic gate
    (``agent_chat.enforce_agentic_quota_ex``) checks the turn against and
    charges it to the platform-wide tiers alone - the licence ceilings and
    ``platform_limit`` - and words its refusal for a visitor. A turn that
    reached no model (no provider configured, a failure before the loop, a
    provider that could not be reached) gets that charge back; any other
    failure keeps it, as there is no user whose failure pool could forgive it.

    *compact_conversation_id* names the conversation *history* is read from:
    its memory management, which can call the LLM, runs only once the gate
    let the turn through - and a compaction cycle the model answered is a
    model this turn reached, so it keeps its charge whatever happens next.
    """
    from app.services.agent_chat import enforce_agentic_quota_ex

    refusal, metered = await enforce_agentic_quota_ex(db, None, agent_id=str(agent.id))
    if refusal:
        return refusal
    # A compaction cycle that got an answer reached a model for this turn.
    compactor_answered = False
    if compact_conversation_id is not None:
        history, compactor_answered = await _compact_public_history(
            db, agent, history, compact_conversation_id
        )

    async def _give_the_charge_back() -> None:
        if not metered or compactor_answered:
            return
        from app.services.quota import refund_agentic_usage_for_provider_outage

        await refund_agentic_usage_for_provider_outage(
            None, agent.id, db=db, reason="public widget turn that reached no model"
        )

    turn: dict = {}
    try:
        reply = await _run_public_agent_turn(
            db,
            agent,
            history,
            user_message,
            visitor_id=visitor_id,
            session_id=session_id,
            context=context,
            on_token=on_token,
            on_event=on_event,
            file_context=file_context,
            turn_files=turn_files,
            turn_texts=turn_texts,
            turn_out=turn,
        )
    except Exception as exc:
        from app.llm.router import is_provider_unavailable_error

        if not turn.get("loop_started") or is_provider_unavailable_error(exc):
            await _give_the_charge_back()
        raise
    if not turn.get("loop_started"):
        # The one answer given before the loop: no LLM provider is configured.
        await _give_the_charge_back()
    return reply


class _AnswerRecorder:
    """The provider handed to the widget's compactor, noting an answered call.

    The compactor asks the model through ``chat()`` alone. Everything else is
    the wrapped provider's own - its attributes, and its class for
    ``isinstance``, which picks the compactor's native structured output.
    """

    def __init__(self, llm) -> None:
        self._llm = llm
        self.answered = False

    @property
    def __class__(self):
        return type(self._llm)

    def __getattr__(self, name: str):
        if name == "_llm":
            raise AttributeError(name)
        return getattr(self._llm, name)

    async def chat(self, *args, **kwargs):
        response = await self._llm.chat(*args, **kwargs)
        self.answered = True
        return response


async def _compact_public_history(
    db: AsyncSession, agent: Agent, history: list, conversation_id: str
) -> tuple[list, bool]:
    """Memory management for a long widget conversation - best-effort.

    A compaction cycle summarises the older turns with the LLM, which is why
    :func:`_run_public_agent_chat` calls this behind its quota gate. Returns
    the history and whether a model answered the compactor. A failure keeps
    the history as it is.
    """
    recorder: _AnswerRecorder | None = None
    try:
        from app.core.database import release_db_connection_before_loop
        from app.llm.agent_model import agent_model_selection
        from app.llm.router import get_llm_for_user
        from app.services.conversation_memory import manage_conversation_memory

        _agent_model = await agent_model_selection(db, agent)
        llm, model = await get_llm_for_user(
            db,
            user_id=None,
            provider_override=_agent_model.provider,
            model_override=_agent_model.model,
            provider_id_override=_agent_model.provider_id,
        )
        recorder = _AnswerRecorder(llm)
        # Commit-release before compaction: on compaction-cycle turns this
        # awaits a full LLM summarization while the message store holds an
        # open transaction.
        await release_db_connection_before_loop(db)
        history = await manage_conversation_memory(
            recorder,
            history,
            agent_id=str(agent.id),
            user_id=None,
            model=model,
            conversation_id=conversation_id,
        )
    except Exception:
        logger.warning("Public chat memory management failed", exc_info=True)
    return history, recorder is not None and recorder.answered


async def _run_public_agent_turn(
    db: AsyncSession,
    agent: Agent,
    history: list,
    user_message: str,
    *,
    visitor_id: str | None = None,
    session_id: str | None = None,
    context: dict | None = None,
    on_token=None,
    on_event=None,
    file_context=None,
    turn_files: list | None = None,
    turn_texts: list[str] | None = None,
    turn_out: dict,
) -> str:
    """Run an agentic chat without requiring a user context.

    Uses the agent's configured LLM (or platform default) and the agent's
    own knowledge bases / MCP tools (but NOT user-specific defaults).

    If *visitor_id* is provided, loads and stores visitor memory so that
    returning visitors get a personalized experience across sessions.

    If *context* is provided (arbitrary JSON from the embedding page), it
    is injected into the system prompt so the agent understands where the
    request originates (e.g. current page URL, product, user role, etc.).

    *file_context* collects the files the turn's tools produced; the caller
    reads back the ones the widget session may download.

    *turn_files* are the refs of the files the visitor attached to this
    message, all in *file_context* for the tools that take a file;
    only the images ride on the user message, shown by their pixels. A
    document reaches the model as its entry of *turn_texts* after what the
    visitor typed - the text its upload's guard screened, never its native
    bytes (``_attach_visitor_uploads``). The message's guard reads what the
    visitor typed.

    *turn_out* gets ``loop_started`` once the agentic loop is called: before
    it, no model was reached (:func:`_run_public_agent_chat` reads it).
    """
    # Bind structured context for all downstream log records
    structlog.contextvars.bind_contextvars(
        source="public_chat",
        agent=agent.name,
        agent_id=str(agent.id),
    )

    from app.api.v1.helpers import build_mcp_runtime
    from app.llm.agent_model import agent_model_selection
    from app.llm.base import (
        ChatMessage,
        ContextOverflowError,
        overflow_error_type,
        overflow_friendly_message,
    )
    from app.llm.router import get_llm_for_user
    from app.models.knowledge_base import KnowledgeBase
    from app.models.mcp_server import MCPServer
    from app.services.vault import SecretUnavailableError
    from app.tools.agent_loop import run_agent_loop
    from app.tools.base import (
        CallableToolExecutor,
        CompositeToolExecutor,
        HumanInputRequestedError,
        ScopedToolExecutor,
        ToolDefinition,
    )
    from app.tools.mcp_client import MCPArtifactScope, MCPManager
    from app.tools.wait_policy import WaitPolicy

    # Resolve LLM provider + model using the standard resolution chain.
    # user_id=None skips the user preference step (public chat has no user).
    _agent_model = await agent_model_selection(db, agent)
    try:
        llm, resolved_model = await get_llm_for_user(
            db,
            user_id=None,
            provider_override=_agent_model.provider,
            model_override=_agent_model.model,
            provider_id_override=_agent_model.provider_id,
        )
    except ValueError:
        return "No LLM provider is configured. Please contact the administrator."

    # ── Memory (visitor only — NO agent memory in public context) ──
    # Public chat must never load agent memory because it may contain
    # private information from authenticated user sessions.  Only the
    # per-visitor memory (cookie/session-scoped) is used here.
    visitor_memory_text = ""
    if visitor_id:
        try:
            from app.services.user_memory import (
                format_user_memory_for_prompt,
                load_user_memory,
            )

            visitor_mem = await load_user_memory(visitor_id)
            visitor_memory_text = format_user_memory_for_prompt(visitor_mem, agent_id=agent.id)
        except Exception as exc:
            logger.warning("Failed to load visitor memory: %s", format_exception_message(exc))

    # ── Tool exposure gate (SEC-1) ──
    # Integration / MCP tools are default-DENIED for unauthenticated
    # visitors; the agent owner must explicitly opt in via
    # channel_config["web_embed"]["allow_tools"] = true.
    allow_tools = _public_tools_allowed(agent)

    # ── System prompt ──
    # List connected integrations (batch-fetch to avoid N+1).  Only listed
    # when tools are actually exposed — otherwise the agent would
    # hallucinate capabilities it does not have in the public context.
    from app.models.integration import Integration

    integration_lines: list[str] = []
    if agent.integration_bindings and allow_tools:
        integ_ids = [
            uuid.UUID(iid)
            for ids in agent.integration_bindings.values()
            for iid in ids
            if iid
        ]
        provider_by_id = {
            uuid.UUID(iid): k
            for k, ids in agent.integration_bindings.items()
            for iid in ids
            if iid
        }
        if integ_ids:
            r = await db.execute(
                select(Integration).where(Integration.id.in_(integ_ids))
            )
            integration_lines = [
                f"- {integ.name} ({provider_by_id.get(integ.id, '?')})"
                for integ in r.scalars()
                if integ.status == "connected"
            ]

    # Knowledge bases (batch-fetch to avoid N+1)
    kb_ids = agent.knowledge_base_ids or []
    kb_lines: list[str] = []
    if kb_ids:
        kb_uuids = [uuid.UUID(k) for k in kb_ids]
        r = await db.execute(
            select(KnowledgeBase).where(KnowledgeBase.id.in_(kb_uuids))
        )
        kb_lines = [
            f"- {kb.name}" + (f": {kb.description}" if kb.description else "")
            for kb in r.scalars()
        ]

    # The session's files as the web chat lists them: which ride on a user
    # message (inlined) and the ids the tools take.
    files_section = ""
    if file_context is not None:
        for ref in turn_files or []:
            file_context.add(ref)
    if file_context is not None and not file_context.is_empty:
        current_file_ids = {ref.file_id for ref in turn_files or []}
        # Every visitor upload is in front of the model: an image on its
        # message, a document as text in it. The tools' own files are not.
        uploaded_file_ids = {
            ref.file_id
            for ref in file_context.list_files()
            if getattr(ref, "source", None) == widget_uploads.WIDGET_UPLOAD_SOURCE
        }
        files_section = file_context.to_prompt_section(
            current_turn_file_ids=current_file_ids or None,
            inlined_file_ids=current_file_ids | uploaded_file_ids,
            quarantine=True,
        )

    system_prompt = _build_public_system_prompt(
        agent,
        visitor_memory_text=visitor_memory_text,
        context=context,
        integration_lines=integration_lines,
        kb_lines=kb_lines,
        files_section=files_section,
        # Same gate as the integration tools built below.
        integration_tools=bool(agent.integration_bindings and allow_tools),
    )

    # ── Tools ──
    composite = CompositeToolExecutor()
    mcp_manager: MCPManager | None = None

    # Knowledge search
    if kb_ids:
        kb_exec = CallableToolExecutor()

        async def _search(query: str, limit: int = 5) -> str:
            try:
                from app.llm.router import get_embedding_provider
                from app.services.vector import search_knowledge_bases

                ep, em, _provider_id = await get_embedding_provider(db)
                qe = await ep.embed(query, model=em)
                results = await search_knowledge_bases(
                    db, None, kb_ids, qe, top_k=limit
                )
                if not results:
                    return "No relevant results found."
                return "\n\n".join(
                    f"[{i + 1}] {r.content[:TRUNCATE_SNIPPET]}"
                    for i, r in enumerate(results)
                )
            except Exception as e:
                return f"Knowledge search failed: {e}"

        kb_exec.register("search_knowledge", _search)
        composite.add_tools(
            [
                ToolDefinition(
                    name="search_knowledge",
                    description="Search the knowledge bases for relevant information.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search query"},
                            "limit": {
                                "type": "integer",
                                "description": "Number of results",
                                "default": 5,
                            },
                        },
                        "required": ["query"],
                    },
                    source="builtin:knowledge",
                )
            ],
            kb_exec,
        )

    # Integration tools — only with the explicit web_embed opt-in (SEC-1)
    if agent.integration_bindings and allow_tools:
        from app.services.element_scope import agent_run_scope
        from app.services.email_signature_settings import SIGNATURE_CONTEXT_KEY
        from app.services.integration_live_writes import LIVE_WRITES_CONTEXT_KEY, live_writes_overrides
        from app.services.integration_read_only import (
            READ_ONLY_CONTEXT_KEY,
            read_only_integration_ids,
        )
        from app.services.integration_tools import BindingBasis, ToolBindings, build_integration_tools

        integ_defs, integ_exec = await build_integration_tools(
            db,
            ToolBindings(
                agent.integration_bindings,
                basis=BindingBasis.OWNER_RUN,
                reason="the public widget acts as the agent's owner, behind the web_embed opt-in",
            ),
            context={
                "source": "public_chat",
                "agent": agent.name,
                # The widget acts as the agent's owner: an integration whose
                # people each connect their own account acts with theirs.
                "credential_user_id": str(agent.user_id) if agent.user_id else None,
                # The agent's enforced Scope(s) bounds its public widget too.
                "element_scope": agent_run_scope(
                    agent.integration_element_bindings,
                    agent.integration_element_enforcement,
                ).enforced,
                READ_ONLY_CONTEXT_KEY: read_only_integration_ids(agent),
                SIGNATURE_CONTEXT_KEY: getattr(agent, "integration_signatures", None),
                LIVE_WRITES_CONTEXT_KEY: live_writes_overrides(agent),
                # A file an integration tool downloads belongs to the widget
                # conversation and to no user, like an MCP binary result: the
                # session's own route serves it.
                "conversation_id": session_id,
            },
        )
        if integ_defs:
            composite.add_tools(integ_defs, integ_exec)

    # MCP tools (batch-fetch to avoid N+1) — same explicit opt-in (SEC-1)
    mcp_ids = (agent.mcp_server_ids or []) if allow_tools else []
    if mcp_ids:
        try:
            # A binary result belongs to the widget conversation and to no
            # user: only its session downloads it (``download_session_file``).
            mcp_manager = MCPManager(
                artifact_scope=MCPArtifactScope(conversation_id=session_id)
            )
            mcp_uuids = [uuid.UUID(m) for m in mcp_ids]
            r = await db.execute(select(MCPServer).where(MCPServer.id.in_(mcp_uuids)))
            for srv in r.scalars():
                if srv.enabled:
                    try:
                        runtime = await build_mcp_runtime(srv)
                    except SecretUnavailableError:
                        # A header or variable in a vault that cannot be read:
                        # this server is left out, the others still load.
                        continue
                    tools = await mcp_manager.connect_server(runtime)
                    composite.add_tools(tools, mcp_manager)
            # Reads the resource links those tools return.
            composite.add_tools(mcp_manager.resource_tools(), mcp_manager)
        except Exception as e:
            logger.warning("MCP connection failed for public chat: %s", format_exception_message(e))

    # ── Apply tool deny list (security backstop) ──────────────────────
    # Deny list takes precedence over all allow lists — mirrors agent_chat.
    effective_tools = composite.all_tools
    if getattr(agent, "builtin_allowed_tools", None):
        # Per-function built-in scoping, before the deny list.
        from app.services.builtin_tools import filter_builtin_tools

        effective_tools = filter_builtin_tools(effective_tools, agent.builtin_allowed_tools)
    if agent.tool_deny_list:
        from app.services.builtin_tools import apply_tool_deny_list

        effective_tools = apply_tool_deny_list(effective_tools, agent.tool_deny_list)

    # Enforce the offered tool surface at execution time too (fail closed):
    # the composite still has executors registered for denied tool names, and
    # the agent loop dispatches tool calls without re-checking the offered
    # list — a prompt-injected model could otherwise call a denied tool by
    # its registered name and have it execute.
    # Per-tool approval on the public embed path.
    #
    # This surface has no user (``user_id=None``), so nobody here can hold a
    # standing approval — and, more importantly, an anonymous website visitor
    # has no authority to approve a destructive action on the operator's
    # platform. Asking them would be theatre.
    #
    # So the gate is installed with no approval channel: a gated call cannot
    # proceed. What it does instead is fail this turn — the pause unwinds as
    # ``HumanInputRequestedError`` and there is nobody to answer it — so the
    # visitor sees an error rather than an explanation. Whether the gate is on
    # follows the agent's ``approve_tool_calls`` like any agent (off on an
    # upgraded install, as on main); when on, only whitelisted tools run.
    #
    # Gate outcomes are drained on both paths below (FR-020). There is no
    # standing-approval sink: exceptions belong to a user, and this surface has
    # no identity — so none can apply and none can go stale.
    _public_gate_outcomes: dict[str, str] = {}
    # Snapshot before the loop: everything after this belongs to this turn.

    from app.services.hitl_whitelist import gate_off_outcome, resolve_approve_tool_calls

    _public_gate_on = await resolve_approve_tool_calls(db, agent)

    async def _drain_public_bookkeeping(_messages, _composite) -> None:
        from app.services.hitl_whitelist import drain_gate_bookkeeping

        await drain_gate_bookkeeping(
            db,
            _messages,
            _composite,
            outcomes=_public_gate_outcomes,
            stale=set(),
            user_id=None,
            hitl_enabled=_public_gate_on,
            hitl_off_outcome=gate_off_outcome(agent),
            conversation_id=session_id,
            exclude_call_ids=_public_preexisting_calls,
        )

    try:
        from app.services.hitl_whitelist import build_approval_gate

        _public_gate = await build_approval_gate(
            db,
            composite,
            user_id=None,
            hitl_enabled=_public_gate_on,
            outcome_sink=_public_gate_outcomes,
        )
        if _public_gate is not None:
            composite.set_approval_gate(_public_gate)
    except Exception:
        # Fail closed here. The trade agent chat makes — blocking every chat is
        # worse for a surface a human is actively watching — has no force on the
        # anonymous embed: nobody is watching and nobody can be asked. Running
        # every tool unattended *because* the control was unavailable inverts
        # the rule the feature is built on.
        from app.services.hitl_whitelist import make_fail_closed_gate

        logger.exception(
            "Could not install the approval gate for public chat; "
            "gating every tool call instead"
        )
        composite.set_approval_gate(
            make_fail_closed_gate(_public_gate_outcomes)
        )

    scoped_executor = ScopedToolExecutor(composite, effective_tools)

    # ── Langfuse tracing (attributed to admin user) ──
    from app.services.langfuse_tracing import (
        build_trace_input,
        end_trace,
        reset_current_trace_ctx,
        set_current_trace_ctx,
        trace_llm_call,
    )

    messages = [
        ChatMessage(role="system", content=system_prompt),
        *history,
        ChatMessage(
            role="user",
            content=_with_visitor_files(user_message, list(turn_texts or [])),
            files=[ref for ref in turn_files or [] if widget_uploads.is_image(ref.content_type)] or None,
        ),
    ]
    # Snapshot before the loop: everything after this belongs to this turn.
    from app.services.tool_tracking import calls_already_in

    _public_preexisting_calls = calls_already_in(messages)

    admin_user_id = await _get_admin_user_id(db)
    trace_ctx = await trace_llm_call(
        db,
        name=f"public-chat:{agent.slug or agent.name}",
        user_id=admin_user_id or "system",
        source=agent.slug or agent.name,
        model=resolved_model,
        input_data=build_trace_input(
            messages=messages,
            system_prompt=system_prompt,
            tools=effective_tools,
            extra={"agent": agent.name, "user_message": user_message},
        ),
        agent_id=str(agent.id),
        agent_name=agent.name,
        agent_slug=agent.slug,
        session_id=session_id,
        metadata={
            "provider": getattr(llm, "provider_name", None),
            "llm_provider": getattr(llm, "provider_name", None),
            **provider_config_metadata(llm),
            "visitor_id": visitor_id,
            "agent_template": agent.template_slug,
            "web_enabled": agent.web_enabled,
        },
    )
    _trace_ctx_token = None

    try:
        _trace_ctx_token = set_current_trace_ctx(trace_ctx) if trace_ctx else None
        # ── Prompt guards ──
        # The same evaluation as agent chat, before the model is called. The
        # visitor has no account, so a match is audited as the guard,
        # anchored on the admin the widget acts as. This surface only logs
        # unless the security option opts in to refusing a `block` rule's
        # match; a refused turn reaches no model, so its charge is given back.
        from app.core.security_options import security_option
        from app.services.prompt_guards import SURFACE_CHAT_MESSAGE, guard_chat_message

        _guard_refusal = await guard_chat_message(
            db,
            user_message,
            user_id=None,
            entity_id=agent.id,
            entity_name=agent.name,
            details={
                "surface": SURFACE_CHAT_MESSAGE,
                "chat_surface": "public_widget",
                "agent_id": str(agent.id),
                "agent_slug": agent.slug,
                "conversation_id": session_id,
                "channel": "web_embed",
            },
            enforce=security_option("prompt_guard_block_public_widget"),
            audit_anchor_user_id=admin_user_id,
        )
        if _guard_refusal:
            end_trace(trace_ctx, output=_guard_refusal)
            return _guard_refusal
        # Use agent-level max_iterations if set, otherwise the platform
        # default from GA config.  The agent override applies on BOTH
        # paths (no DB round-trip needed): even a no-tools chat can spend
        # iterations on the loop's dry-run handling of hallucinated tool
        # calls, so ``agent.max_iterations`` still bounds it.  Only the
        # GA-config lookup is gated on tools -- it is the sole part worth
        # a DB round-trip, and only when tools can actually loop.
        from app.models.general_assistant_config import (
            DEFAULT_MAX_ITERATIONS,
            GeneralAssistantConfig,
        )

        platform_max = DEFAULT_MAX_ITERATIONS
        if effective_tools:
            ga_result = await db.execute(select(GeneralAssistantConfig).limit(1))
            ga_cfg = ga_result.scalar_one_or_none()
            if ga_cfg:
                platform_max = ga_cfg.max_iterations
        effective_max = (
            agent.max_iterations
            if agent.max_iterations is not None
            else platform_max
        )
        # No-tools conversations go through the same agentic loop as
        # tool-enabled ones, mirroring agent_chat / general_chat (CORE-11):
        # the loop's no-tools branch streams tokens, continues
        # truncated responses, and recovers empty responses — replacing the
        # inline fork that called the loop-private streaming helper here.
        # No-tools contract: normalize ``tools`` to None and drop the
        # executor, so a hallucinated tool call routes into the loop's
        # dry-run branch (synthetic results) instead of an armed executor.
        #
        # The agentic loop can run for minutes. Release the pooled DB
        # connection first so a public-chat visitor doesn't pin one for the
        # whole generation (this is the only direct run_agent_loop caller that
        # otherwise held its session across the loop). Mirrors
        # agent_chat / general_chat; SQLAlchemy transparently re-acquires a
        # connection on the session's next use (post-loop persistence below).
        from app.core.database import release_db_connection_before_loop

        await release_db_connection_before_loop(db)
        # Deliverable rescue: only assistant messages appended during
        # THIS loop run are rescue candidates — the conversation history
        # already in ``messages`` must never leak into this turn's reply.
        _pre_loop_message_count = len(messages)
        turn_out["loop_started"] = True
        response = await run_agent_loop(
            llm=llm,
            messages=messages,
            tools=effective_tools or None,
            executor=scoped_executor if effective_tools else None,
            max_iterations=effective_max,
            model=resolved_model,
            on_token=on_token,
            on_event=on_event,
            file_context=file_context,
            # No timer on the public embed surface: an anonymous caller
            # must not be able to hold a worker idle for minutes, and there is
            # no cancel/skip channel here to end a pause early.
            wait_policy=WaitPolicy.disabled(),
        )

        result_text = response.content or ""
        # ── Deliverable rescue ────────────────────────────────────
        # Reasoning-forward models (observed with gpt-6-astra) often
        # front-load the substantive answer BEFORE side-effect tool calls
        # and close with a one-liner after the results come back; the
        # preamble hold-back routes that pre-tool text into the reasoning
        # pane, so ``response.content`` is only the closer.  Mirror the
        # assignment engine's rescue.  Skipped for JSON-format agents
        # (merging prose would corrupt the payload).
        if getattr(agent, "output_format", None) != "json":
            from app.services.assignment_helpers import select_best_chat_reply

            result_text = select_best_chat_reply(
                result_text, messages[_pre_loop_message_count:]
            )
        # The widget shows an answer's files as chips of its own; a
        # ``[[FILE:id]]`` marker left in the prose would read as raw text.
        # Which fetched files the answer hands over is read off the markers
        # it was written with, so the decision is taken before they go.
        from app.channels.base import strip_file_markers
        from app.tools.base import deliver_named_files

        answer_as_written = result_text
        result_text = strip_file_markers(result_text or "") or "Done."
        # ── Prompt guards on the answer ── a no-op unless the
        # security option turns it on; the stream endpoint's answer is
        # streamed, the other one is delivered whole.
        from app.services.prompt_guards import guard_model_output

        guarded_text = await guard_model_output(
            result_text,
            streamed=on_token is not None,
            user_id=None,
            entity_id=agent.id,
            entity_name=agent.name,
            details={
                "agent_id": str(agent.id),
                "agent_slug": agent.slug,
                "conversation_id": session_id,
                "channel": "web_embed",
            },
            audit_anchor_user_id=admin_user_id,
        )
        if guarded_text == result_text:
            deliver_named_files(file_context, answer_as_written)
        result_text = guarded_text
        end_trace(
            trace_ctx, output=result_text, usage=response.usage, model=response.model
        )

        # ── Track tool usage on Integration / MCP / Custom entities ──
        try:
            from app.services.tool_tracking import track_tool_usage

            await track_tool_usage(db, messages, composite)
        except Exception as _track_err:
            logger.warning("Tool usage tracking failed: %s", format_exception_message(_track_err), exc_info=True)

        # ── Gate outcomes ─────────────────────────────────────
        # Anonymous, so these land in the application log rather than the audit
        # trail — an audit row needs a user to attribute it to.
        await _drain_public_bookkeeping(messages, composite)

        # ── Memory (save after response) ────────────────────────
        # Only visitor memory is saved in public context — agent memory
        # is never written from public chat to prevent pollution of the
        # agent's autonomous or per-user memory with anonymous visitor data.
        chat_summary = f"Visitor asked: {user_message[:TRUNCATE_LABEL]} → Agent replied: {result_text[:TRUNCATE_PREVIEW]}"
        # Visitor memory (personalization for returning visitors)
        if visitor_id:
            try:
                from app.services.user_memory import (
                    consolidate_user_memory,
                    store_user_memory,
                )

                v_counter = await store_user_memory(
                    visitor_id,
                    agent_name=agent.name,
                    summary=chat_summary,
                    interaction_type="chat",
                    agent_id=str(agent.id),
                )
                if v_counter >= 6:
                    asyncio.create_task(
                        consolidate_user_memory(
                            visitor_id, llm, model=resolved_model
                        )
                    )
            except Exception as exc:
                logger.warning("Failed to store visitor memory: %s", format_exception_message(exc))

        return result_text
    except HumanInputRequestedError as _pub_hitl:
        # A gated tool call unwinds the loop through here — this surface offers
        # no way to answer a prompt — so the drain on the success path would
        # never see the very turns that produced a gate decision worth
        # recording.
        from app.tools.approval import mark_paused

        mark_paused(_public_gate_outcomes, getattr(_pub_hitl, "proposals", None))
        await _drain_public_bookkeeping(messages, composite)
        # Answer rather than re-raise: the caller's broad handler turned this
        # into a generic error, so a visitor was told the assistant broke when
        # it had in fact declined to act without a human. Without the pointer to
        # an administrator screen they cannot reach and should not be shown.
        from app.tools.approval import no_approval_channel_message

        logger.info("Public chat stopped for approval: %s", _pub_hitl.question)
        return no_approval_channel_message(_pub_hitl, mention_admin_setting=False)
    except ContextOverflowError as coe:
        logger.warning(
            "Context overflow in public chat (agent=%s): ~%d tokens vs %d window (model=%s)",
            agent.name,
            coe.estimated_tokens,
            coe.context_window,
            coe.model,
        )
        if on_token:
            await on_token(overflow_friendly_message(coe))
        if on_event:
            await on_event({"status": "error", "error_type": overflow_error_type(coe)})
        end_trace(trace_ctx, error=str(coe))
        return overflow_friendly_message(coe)
    except Exception as exc:
        from app.llm.router import (
            REQUEST_TOO_LARGE_STREAMING_MESSAGE,
            is_request_too_large_error,
        )

        if is_request_too_large_error(exc):
            # ── HTTP 413 / payload-too-large: friendly streaming response ──
            # Raw 413, or a failover chain that ended on one.  Mirror the
            # ContextOverflowError path above so the widget shows a clean
            # message instead of a generic server error.
            logger.warning(
                "Request-too-large error (HTTP 413) in public chat (agent=%s): %s",
                agent.name,
                format_exception_message(exc),
            )
            if on_token:
                await on_token(REQUEST_TOO_LARGE_STREAMING_MESSAGE)
            if on_event:
                await on_event({"status": "error", "error_type": "request_too_large"})
            end_trace(trace_ctx, error=str(exc))
            return REQUEST_TOO_LARGE_STREAMING_MESSAGE
        end_trace(trace_ctx, error="Public chat agentic loop failed")
        raise
    finally:
        if _trace_ctx_token is not None:
            with contextlib.suppress(Exception):
                reset_current_trace_ctx(_trace_ctx_token)
        if mcp_manager:
            await mcp_manager.disconnect_all()
        structlog.contextvars.unbind_contextvars("source", "agent", "agent_id")
