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

"""AI-assisted utilities — prompt generation, text enhancement, etc."""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_agentic_ai, require_ai_enabled
from app.core.database import get_db
from app.core.feature_flags import require_feature
from app.core.utils import format_exception_message, is_valid_uuid
from app.llm.base import ChatMessage
from app.llm.cut_answers import chat_retrying_a_cut
from app.llm.router import get_llm_for_user
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter()

# Output budget of one generated prompt; a prompt cut at it is retried once with
# twice the room.
_PROMPT_MAX_TOKENS = 2048


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class GeneratePromptRequest(BaseModel):
    description: str  # What the user wants in plain language
    # persona | assignment_prompt | gate_prompt | system_prompt | ai_label_prompt | hitl_prompt
    # | agent_account_instructions | agent_account_rule_instructions
    context_type: str
    current_text: str | None = None  # Existing text to refine / improve
    agent_name: str | None = None
    assignment_name: str | None = None


class GeneratePromptResponse(BaseModel):
    generated_prompt: str
    #: The reply was still cut at its output limit after one retry with twice
    #: the room. The text is returned as generated, for the person to
    #: finish or trim in the editor.
    truncated: bool = False


class GenerateTemplateRequest(BaseModel):
    """Brief for the "Generate with AI" template action."""

    brief: str = Field(min_length=3, max_length=4000)
    kind: str = Field(default="presentation", max_length=50)
    #: Free-form colour hints ("navy", "#0b1020", "warm and earthy").
    palette_hints: list[str] | None = None
    #: A template image asset whose dominant colours seed the palette. Must be
    #: readable by the caller (same rule as downloading it).
    logo_file_id: str | None = None
    #: The template being refined (its current fields), when regenerating from
    #: an existing one. Ownership / sharing fields are ignored.
    current: dict | None = None
    #: Name already typed in the form — kept over the model's suggestion.
    name: str | None = Field(default=None, max_length=200)


class TemplateCandidate(BaseModel):
    name: str
    description: str | None = None
    kind: str
    theme: str | None = None
    aspect_ratio: str | None = None
    tags: list[str]
    theme_config: dict[str, str]
    layouts: list[dict]
    extra_metadata: dict


class GenerateTemplateResponse(BaseModel):
    candidate: TemplateCandidate
    #: What the validators pruned from the model's proposal, one line each.
    warnings: list[str]
    #: Hex colours read from the logo asset, most frequent first (empty when no
    #: logo was given or it could not be decoded).
    palette_from_logo: list[str]


# ---------------------------------------------------------------------------
# Meta-prompts for each context type
# ---------------------------------------------------------------------------

_META_PROMPTS: dict[str, str] = {
    "persona": (
        "You are an expert at writing AI agent persona definitions. "
        "A persona defines who the AI agent is, how it behaves, its tone, "
        "communication style, and any domain expertise it should have.\n\n"
        "Write a clear, well-structured persona/system prompt based on "
        "the user's description. Use second person ('You are…').\n"
        "Output ONLY the persona text — no explanations, no markdown fences."
    ),
    "assignment_prompt": (
        "You are an expert at writing assignment action prompts for AI agents. "
        "A assignment prompt tells the AI exactly what to do when the assignment is "
        "triggered — which tools to use, how to process data, what output to "
        "produce, and in what order.\n\n"
        "Write a clear, step-by-step action prompt based on the user's "
        "description. Use imperative style and be specific about tool usage.\n"
        "Output ONLY the prompt text — no explanations, no markdown fences."
    ),
    "gate_prompt": (
        "You are an expert at writing assignment gate prompts (activation prompts). "
        "A gate prompt is a short screening question evaluated BEFORE the main "
        "assignment runs. It receives the trigger payload and decides whether this "
        "event is relevant. The LLM answers YES or NO.\n\n"
        "Write a concise gate prompt based on the user's description. "
        "It should clearly describe what qualifies as a YES.\n"
        "Output ONLY the gate prompt text — no explanations, no markdown fences."
    ),
    "system_prompt": (
        "You are an expert at writing system prompts for AI assistants. "
        "A system prompt defines the assistant's behavior, personality, scope, "
        "and constraints. It shapes every response the assistant produces.\n\n"
        "Write a clear, well-structured system prompt based on the user's "
        "description. Use second person ('You are…'). Be specific about "
        "what the assistant should and shouldn't do.\n"
        "Output ONLY the system prompt text — no explanations, no markdown fences."
    ),
    "ai_label_prompt": (
        "You are an expert at writing AI label prompt additions for AI assignments. "
        "An AI label prompt instructs the AI to prepend a visible label or tag to "
        "outgoing messages indicating that the content was generated by AI. "
        "It defines the format, wording, and placement of the AI disclosure.\n\n"
        "Write a clear AI label prompt addition based on the user's description. "
        "Be specific about label format and when to apply it.\n"
        "Output ONLY the prompt text — no explanations, no markdown fences."
    ),
    "hitl_prompt": (
        "You are an expert at writing human-in-the-loop prompt additions for AI assignments. "
        "A HITL prompt instructs the AI on when and how to pause execution and ask "
        "a human operator for input, approval, or clarification. It defines the "
        "criteria for pausing (uncertainty, high-risk actions, ambiguity) and how "
        "to formulate clear questions for the human.\n\n"
        "Write a clear HITL prompt addition based on the user's description. "
        "Be specific about when to pause and how to ask for input.\n"
        "Output ONLY the prompt text — no explanations, no markdown fences."
    ),
    "agent_account_instructions": (
        "You are an expert at writing standing instructions for an agent that operates "
        "a real messaging account: a person's account it answers for, or a "
        "dedicated AI colleague's account. They are added to the agent's persona for "
        "every reply the account posts, to people who are not the agent's owner.\n\n"
        "Write concise instructions based on the user's description: voice and tone, "
        "what to commit to and what to defer to the owner, what never to disclose, and "
        "when to stay silent. Use second person ('You ...'), short imperative lines.\n"
        "Output ONLY the instructions — no explanations, no markdown fences."
    ),
    "agent_account_rule_instructions": (
        "You are an expert at writing guardrails for one audience of an agent that "
        "operates a messaging account. The guardrail applies only to the "
        "messages one rule answers - a channel, a group of people - and is added to "
        "the agent's persona for those replies. The platform already limits which "
        "tools and knowledge the agent can reach for this audience; the guardrail "
        "governs what it says.\n\n"
        "Write concise guardrails based on the user's description: who the audience "
        "is, what information is and is not appropriate for them (for example: only "
        "non-confidential, already-public information; never board or financial "
        "material), how to decline politely, and the tone to use. Use second person "
        "('You ...'), short imperative lines.\n"
        "Output ONLY the guardrails — no explanations, no markdown fences."
    ),
}


def _build_messages(req: GeneratePromptRequest) -> list[ChatMessage]:
    """Build the chat messages for prompt generation."""
    meta = _META_PROMPTS.get(req.context_type, _META_PROMPTS["assignment_prompt"])

    system_content = meta
    if req.agent_name:
        system_content += f"\n\nContext: The agent is named '{req.agent_name}'."
    if req.assignment_name:
        system_content += f"\nThe assignment is named '{req.assignment_name}'."

    messages = [ChatMessage(role="system", content=system_content)]

    user_content = f"Generate a prompt based on this description:\n{req.description}"
    if req.current_text:
        user_content += f"\n\nHere is the current text to improve/refine:\n---\n{req.current_text}\n---"

    messages.append(ChatMessage(role="user", content=user_content))
    return messages


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/generate-prompt", response_model=GeneratePromptResponse)
async def generate_prompt(
    body: GeneratePromptRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Use the configured LLM to generate a system prompt from a description.

    Resolves the provider **and model** via ``get_llm_for_user`` (agent
    override -> agent default -> user preference -> platform default) so
    "Generate with AI" runs on the admin-configured default chat model and
    inherits failover — instead of silently using whichever provider happened
    to be configured first with its built-in default model.
    """
    llm, model = await get_llm_for_user(db, str(user.id))
    messages = _build_messages(body)

    async def _generate(max_tokens: int):
        return await llm.chat(messages, model=model, temperature=0.7, max_tokens=max_tokens)

    response, cut = await chat_retrying_a_cut(
        llm, model, _PROMPT_MAX_TOKENS, _generate, label="Prompt generator"
    )
    if cut:
        # Nothing is stored: the text lands in the editor, where the person
        # finishes or trims it, so it is handed back flagged, never withheld.
        logger.warning("Prompt generator answer still cut at max_tokens - returned as generated")
    return GeneratePromptResponse(generated_prompt=response.content.strip(), truncated=cut)


async def _logo_palette(logo_file_id: str | None, user: User, db: AsyncSession, warnings: list[str]) -> list[str]:
    """Dominant colours of a template asset the caller may read, or ``[]``.

    Authorisation goes through ``can_read_file`` — the one rule for
    "may this user read these bytes" — so a file id cannot be used to sample the
    palette of an asset the caller could not download. A failed read is a
    warning, not an error: the brief still generates without the hint. A file
    the caller may not read gets the same warning as an id nobody issued, so
    the answer never confirms which ids exist.
    """
    if not logo_file_id:
        return []
    if not is_valid_uuid(logo_file_id):
        warnings.append("logo_file_id is not a valid id; the logo was ignored.")
        return []
    from app.models.file_attachment import FileAttachment
    from app.services.file_access import can_read_file
    from app.services.file_storage import file_storage
    from app.services.template_generation import MAX_LOGO_BYTES, extract_palette_async

    file_att = (
        await db.execute(select(FileAttachment).where(FileAttachment.id == uuid.UUID(logo_file_id)))
    ).scalar_one_or_none()
    if file_att is None or not await can_read_file(file_att, user, db):
        warnings.append("The logo file was not found; it was ignored.")
        return []
    if not (file_att.content_type or "").startswith("image/"):
        warnings.append("The logo file is not an image; it was ignored.")
        return []
    if (file_att.size or 0) > MAX_LOGO_BYTES:
        warnings.append("The logo file is too large to sample (max 5 MB); it was ignored.")
        return []
    try:
        raw = await file_storage.download(file_att.s3_object_name)
    except Exception as exc:
        logger.warning("Logo download failed for template generation: %s", format_exception_message(exc))
        warnings.append("The logo file could not be read; it was ignored.")
        return []
    palette = await extract_palette_async(raw, file_att.content_type)
    if not palette:
        warnings.append("No colours could be read from the logo; it was ignored.")
    return palette


@router.post("/generate-template", response_model=GenerateTemplateResponse)
async def generate_template(
    body: GenerateTemplateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Draft a complete template container from a plain-language brief.

    Nothing is persisted: the response is a candidate the Templates form applies
    (create or edit), previews through the real deck renderer, and can send back
    as ``current`` for another refinement round. Whatever the model proposes is
    run through the same guardrails an agent authoring a template hits, so the
    palette and layout catalog returned are ones the renderer honours; anything
    pruned is listed in ``warnings``.
    """
    from app.services.template_containers import kind_problem, normalize_kind
    from app.services.template_generation import (
        TemplateGenerationError,
        clean_palette_hints,
        current_template_summary,
        generate_template_candidate,
    )

    # The same closed choice as the create route: a candidate is drafted
    # for a kind a template can be created with, or the brief is refused.
    kind = normalize_kind(body.kind)
    if kind is None:
        raise HTTPException(status_code=422, detail=kind_problem(body.kind))
    warnings: list[str] = []
    logo_palette = await _logo_palette(body.logo_file_id, user, db, warnings)

    llm, model = await get_llm_for_user(db, str(user.id))
    try:
        candidate, problems = await generate_template_candidate(
            llm,
            model,
            brief=body.brief,
            kind=kind,
            palette_hints=clean_palette_hints(body.palette_hints),
            logo_palette=logo_palette,
            current=current_template_summary(body.current),
            fallback_name=body.name,
        )
    except TemplateGenerationError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    if body.name and body.name.strip():
        candidate["name"] = body.name.strip()[:200]
    return GenerateTemplateResponse(
        candidate=TemplateCandidate(**candidate),
        warnings=[*warnings, *problems],
        palette_from_logo=logo_palette,
    )


# ---------------------------------------------------------------------------
# The agent wizard's assistant (AGENT_WIZARD_ASSIST)
# ---------------------------------------------------------------------------

_WizardText = Field(default="", max_length=100_000)
_WizardIds = Field(default_factory=list, max_length=500)


class WizardDraftIn(BaseModel):
    """The wizard's draft as the person has it, unsaved and possibly
    incomplete: bounded, never trusted - ids are named from the caller's own
    offer, and one it does not list is shown to the model as unavailable."""

    model_config = ConfigDict(extra="forbid")

    template_slug: str | None = Field(default=None, max_length=200)
    step: str = Field(default="identity", max_length=40)
    name: str = _WizardText
    icon: str = Field(default="", max_length=100)
    description: str = _WizardText
    persona: str = _WizardText
    guardrails: str = _WizardText
    tool_ids: list[str] = _WizardIds
    integration_ids: list[str] = _WizardIds
    knowledge_base_ids: list[str] = _WizardIds
    skill_ids: list[str] = _WizardIds
    handover_agent_ids: list[str] = _WizardIds
    model: str = Field(default="", max_length=400)
    memory: dict[Literal["short", "medium", "persistent", "user"], bool] = Field(default_factory=dict)
    budget: dict[Literal["cost", "tokens", "calls", "seconds"], str] = Field(default_factory=dict)

    @field_validator("tool_ids", "integration_ids", "knowledge_base_ids", "skill_ids", "handover_agent_ids")
    @classmethod
    def _bounded_ids(cls, ids: list[str]) -> list[str]:
        if any(len(v) > 200 for v in ids):
            raise ValueError("an id is longer than 200 characters")
        return ids

    @field_validator("budget")
    @classmethod
    def _bounded_budget(cls, budget: dict[str, str]) -> dict[str, str]:
        if any(len(v) > 40 for v in budget.values()):
            raise ValueError("a budget value is longer than 40 characters")
        return budget


class WizardAssistTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class WizardAssistRequest(BaseModel):
    """One round with the wizard's assistant: nothing is stored."""

    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=2, max_length=4000)
    draft: WizardDraftIn = Field(default_factory=WizardDraftIn)
    #: Whether the wizard is on its steps (False: still on the template
    #: gallery, the one moment a template may be chosen).
    started: bool = False
    history: list[WizardAssistTurn] = Field(default_factory=list, max_length=20)
    #: The icons the wizard's picker can render; the assistant picks from them.
    icon_names: list[str] = Field(default_factory=list, max_length=400)

    @field_validator("icon_names")
    @classmethod
    def _icon_names(cls, names: list[str]) -> list[str]:
        from app.services.agent_wizard_assistant import ICON_NAME

        if not all(ICON_NAME.match(n) for n in names):
            raise ValueError("an icon name is not a plain identifier")
        return names


class WizardAssistResponse(BaseModel):
    reply: str
    #: Only the fields to change, each list whole (`WizardPatch`), every id one
    #: the caller's wizard offers.
    patch: dict
    #: What the assistant proposed that could not be kept, as codes
    #: (`agent_wizard_assistant.NOTE_CODES`) the wizard words for the person.
    notes: list[str] = []


#: How often the round checks that its caller is still there.
_DISCONNECT_POLL_SECONDS = 0.5
# A 503 whose `detail` is `{code, message}`: the one failure shape the web
# client reads (`lib/api.ts` turns any 502 into "Server unavailable" before
# reading the body). The wizard words the code; `message` is for other clients.
_WIZARD_MODEL_UNAVAILABLE = {
    "code": "assistant_model_unavailable",
    "message": "The assistant could not reach the model. Try again in a moment.",
}
_WIZARD_NO_PLAYABLE_ANSWER = {
    "code": "assistant_no_playable_answer",
    "message": "The assistant could not prepare a change the wizard accepts. Rephrase and try again.",
}


async def _unless_disconnected[T](request: Request, work: Awaitable[T]) -> T:
    """`work`, cancelled as soon as the caller goes away: the wizard's Stop
    aborts its request, and a model call nobody waits for any more would still
    be spent. Raises a 499 then (nobody reads it)."""
    task = asyncio.ensure_future(work)
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=_DISCONNECT_POLL_SECONDS)
            if done:
                return task.result()
            if await request.is_disconnected():
                break
    finally:
        if not task.done():
            task.cancel()
            # Let the cancellation reach the model call before answering.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    raise HTTPException(status_code=499, detail="Client closed the request.")


@router.post(
    "/agent-wizard/assist",
    response_model=WizardAssistResponse,
    dependencies=[Depends(require_feature("AGENT_WIZARD_ASSIST"))],
)
async def assist_agent_wizard(
    body: WizardAssistRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_agentic_ai),
):
    """One round with the agent wizard's assistant: a reply and the fields to
    change, built with the caller's model from what the caller's wizard offers.
    Nothing is stored; the wizard plays the patch onto its draft. Gated like
    creating the agent it prepares (AI on, Enterprise Edition).

    Errors the person reads: no provider they may use (403, the provider rule's
    own sentence), the model unreachable or no playable answer (503 with a
    `{code, message}` detail; the cause goes to the logs only)."""
    from app.llm.provider_access import ProviderNotUsableError
    from app.services.agent_wizard_assistant import (
        WizardAssistantError,
        WizardModelUnavailableError,
        assist,
        wizard_offer,
    )

    offer = await wizard_offer(db, user)
    try:
        llm, model = await get_llm_for_user(db, str(user.id))
    except ProviderNotUsableError as refused:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(refused)) from None
    except Exception as exc:
        logger.warning("agent wizard assistant: no model: %r", format_exception_message(exc)[:300])
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_WIZARD_MODEL_UNAVAILABLE) from None
    try:
        reply, patch, notes = await _unless_disconnected(
            request,
            assist(
                llm,
                model,
                instruction=body.instruction,
                draft=body.draft.model_dump(),
                started=body.started,
                history=[turn.model_dump() for turn in body.history],
                offer=offer,
                icon_names=body.icon_names,
            ),
        )
    except WizardModelUnavailableError as exc:
        logger.warning("agent wizard assistant: model unreachable: %r", str(exc)[:300])
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_WIZARD_MODEL_UNAVAILABLE) from None
    except WizardAssistantError as exc:
        logger.info("agent wizard assistant gave up: %r", str(exc)[:300])
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_WIZARD_NO_PLAYABLE_ANSWER
        ) from None
    return WizardAssistResponse(reply=reply, patch=patch.model_dump(exclude_unset=True), notes=notes)
