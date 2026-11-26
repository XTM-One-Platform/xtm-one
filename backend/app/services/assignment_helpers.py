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

"""Assignment helper functions.

Extracted from assignment_engine.py to keep the main execution module focused
on the trigger -> execute -> record lifecycle.  All functions here are private
helpers that build tools, render prompts, and process memory context for
assignment runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Collection
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import redis_client
from app.core.utils import format_exception_message
from app.integrations.run_payload_shapers import shape_run_payload
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.services.content_safety import (
    UNTRUSTED_CONTENT_POLICY,
    untrusted_policy_needed,
    wrap_memory,
    wrap_untrusted,
)
from app.services.prompt_guard import build_security_policy

logger = logging.getLogger(__name__)

# Trigger types whose payload originates from external, attacker-influenceable
# sources. Manual and schedule triggers carry user-authored payloads and are
# NOT quarantined. A platform event carries text other agents wrote
# (work item notes, titles, task outcomes): quarantined like an external one.
_UNTRUSTED_TRIGGER_TYPES = frozenset({"webhook", "integration_event", "platform_event"})


def assignment_untrusted_policy_needed(
    agent: Agent, assignment: Assignment, *, untrusted_trigger: bool
) -> bool:
    """Whether an assignment run's system prompt carries the quarantine-marker policy.

    Quarantined content can reach the run through an untrusted trigger payload
    (webhook / integration_event / platform_event), a bound ``web_fetch``,
    ``document_vision`` or ``shared_state`` tool, or its integration tools.
    Those are built from the assignment's own bindings, the agent's when it
    has none (``None``; ``{}`` means no integration), plus the agent's
    template bindings.
    """
    from app.services.integration_tools import flatten_bindings

    bindings = (
        agent.integration_bindings
        if assignment.integration_bindings is None
        else assignment.integration_bindings
    )
    return untrusted_policy_needed(
        quarantined=untrusted_trigger,
        builtin_tool_ids=agent.builtin_tool_ids,
        integration_tools=bool(
            flatten_bindings(bindings or {}) or agent.integration_template_bindings
        ),
    )

# ---------------------------------------------------------------------------
# Default system prompt additions for assignment toggles.
# These are injected into the system message when the corresponding toggle
# is ON and no custom prompt override is stored on the assignment.
# Keep in sync with frontend constants (frontend/src/lib/constants.ts).
# ---------------------------------------------------------------------------

_UNRESOLVED_RE = re.compile(r"\{\{(.+?)\}\}")

# The system-prompt sections an author writes as templates (rendered with the
# run's variables), by their ``_build_system_message`` breakdown label: the
# only ones scanned for unresolved placeholders.
_TEMPLATED_SECTION_NAMES = {
    "persona": "persona",
    "guardrails": "guardrails",
    "attached_prompts": "attached prompts",
    "skills": "pinned skills",
}

DEFAULT_AI_LABEL_PROMPT = (
    "\nAI Label: prepend '🤖 [AI-generated]' to all outgoing messages and content."
)

DEFAULT_HITL_PROMPT = (
    "\nHuman in the Loop: use `request_human_input` when unsure, need approval, "
    "or face high-stakes/irreversible decisions. Ask one plain question, put what you "
    "found in `context`, give `options` with your `recommendation` and the source `links`. "
    "Only pause for genuine edge cases — handle routine tasks autonomously."
)


def resolve_hitl(agent, assignment) -> tuple[bool, str | None]:
    """Resolve effective HITL state: assignment override → agent default → False.

    Returns (enabled, prompt).  The prompt is the assignment-level custom prompt
    if set, otherwise the agent-level custom prompt, otherwise ``None`` (caller
    should fall back to ``DEFAULT_HITL_PROMPT``).
    """
    enabled: bool
    if assignment.human_in_the_loop is not None:
        enabled = assignment.human_in_the_loop
    else:
        enabled = getattr(agent, "human_in_the_loop", False)

    prompt = assignment.human_in_the_loop_prompt or getattr(
        agent, "human_in_the_loop_prompt", None
    )
    return enabled, prompt


#: Triggers that mean nobody is watching, whatever identity the run carries.
#:
#: ``schedule_fanout`` is why this exists: a per-user fan-out passes the target
#: user as ``triggered_by_user_id`` to get the *execution* identity right, so
#: reading that field alone called a scheduled run interactive. The rest never
#: set the field today and so change nothing — they are listed because the same
#: mistake made through any of them would look identical.
_UNATTENDED_TRIGGERS = frozenset(
    {"schedule", "schedule_fanout", "webhook", "integration_event", "event"}
)


def _trigger_kind(run) -> str:
    """How this run was started, as the trigger itself recorded it."""
    context = getattr(run, "trigger_context", None) or {}
    payload = getattr(run, "trigger_payload", None) or {}
    kind = context.get("type") or payload.get("trigger_type") or ""
    return str(kind)


def is_interactive_run(run) -> bool:
    """True when a human initiated this run and may be watching it.

    Two signals, and the trigger wins. ``triggered_by_user_id`` names the person
    a run belongs to, which is *usually* whoever started it — a manual trigger
    from the UI, the agent-builder, the ``trigger_assignment`` tool. But a
    scheduled per-user fan-out sets it too, to run as that user, and nobody is
    watching that. Identity is therefore necessary and not sufficient: a run
    whose trigger is one of :data:`_UNATTENDED_TRIGGERS` is autonomous however
    it is attributed.

    Everything else with no identity is autonomous as well. That default is the
    safe direction for what this drives: an autonomous pause notifies the
    assignment owner, so guessing "autonomous" sends a notification nobody
    needed, while guessing "interactive" would leave a stalled run silent.

    This feeds notification routing ONLY. It MUST NOT become an input to the
    approval gate: a user's standing approval applies to their scheduled runs
    exactly as it does to interactive ones, and making the gate interactivity-
    dependent would silently diverge the two.
    """
    if _trigger_kind(run) in _UNATTENDED_TRIGGERS:
        return False
    return getattr(run, "triggered_by_user_id", None) is not None


def resolve_hide_runs(agent, assignment) -> bool:
    """Resolve whether this assignment's runs are hidden from other users.

    Precedence: ``run_as_all_users`` implies hidden → assignment override →
    agent default → False. Only meaningful for company-managed entities
    (personal runs are already private). Used at run creation to stamp
    ``AssignmentRun.is_company_managed`` (a hidden run is born
    ``is_company_managed=False`` so the existing visibility filters scope it to
    the executing user) and to open the per-user manual-trigger gate.

    ``run_as_all_users`` ALWAYS implies hidden runs: a per-user fan-out turns one
    assignment into one run per user, so visible-to-all runs would show every
    user all N runs (their own + everyone else's) - noise that is never intended.
    A fan-out's runs are therefore always private to the user they ran as,
    regardless of the ``hide_runs`` flag.
    """
    if getattr(assignment, "run_as_all_users", False):
        return True
    override = getattr(assignment, "hide_runs", None)
    if override is not None:
        return bool(override)
    return bool(getattr(agent, "hide_runs", False))


# ---------------------------------------------------------------------------
# Payload serialization with size guard.
#
# Large webhook payloads (e.g. OpenCTI STIX bundles) can easily exceed LLM
# context-window limits.  We serialise to compact JSON first, then truncate
# if needed, keeping a clear notice so the LLM knows the data was trimmed.
# ---------------------------------------------------------------------------

# Maximum characters for a serialised trigger payload.
#
# STIX / JSON-heavy data tokenises poorly (~2-2.5 chars/token due to UUIDs,
# short keys, nested structures).  150 K chars ≈ 60-75 K tokens, which leaves
# ample headroom inside a 200 K context window for:
#   • system prompt + agent persona + guardrails + memory  (~5-15 K tokens)
#   • tool definitions (17+ OpenCTI tools, KB, HITL, etc.) (~15-25 K tokens)
#   • trigger context metadata + rendered prompt            (~2-5 K tokens)
#   • output tokens                                         (~16 K tokens)
#
# The agent can always call tools (e.g. opencti_get_entity,
# opencti_get_container_full) to retrieve additional details on demand,
# so a truncated payload is not a blocker.
_MAX_PAYLOAD_CHARS = 150_000

# Below this threshold we pretty-print for readability; above we use compact
# JSON to conserve tokens.
_PRETTY_PRINT_THRESHOLD = 50_000

# ---------------------------------------------------------------------------
# Agent-memory budget for assignment system prompts.
#
# The combined memory block (Redis medium-term + short-term + PostgreSQL
# persistent entries) can accrete to tens of thousands of characters for
# long-running agents (~39 K chars observed in production).  A
# bloated memory section raises per-iteration LLM latency, easily consuming
# the entire 600s / 25-iteration budget before the task completes.
#
# This budget covers all three memory tiers together.  It is deliberately
# generous (~5 K tokens at 3 chars/token).  The Redis tiers are bounded by
# the medium-term summary cap in agent_memory.py
# (_MAX_MEDIUM_TERM_PROMPT_CHARS = 6 000); the persistent block holds the
# whole entries that fit in what they leave.
# ---------------------------------------------------------------------------
_MAX_ASSIGNMENT_MEMORY_CHARS = 15_000

# Strong references to fire-and-forget memory self-heal tasks.  The
# event loop only holds weak references to tasks (see the
# ``asyncio.create_task`` docs), so without this set a scheduled self-heal
# could be garbage-collected mid-flight.
_self_heal_tasks: set[asyncio.Task] = set()


def _consume_self_heal_task(task: asyncio.Task) -> None:
    """Done callback: drop the strong reference and consume any exception.

    ``auto_heal_agent_memory`` never raises by contract; retrieving the
    exception here keeps a broken contract (or a cancellation at shutdown)
    from surfacing as a "Task exception was never retrieved" warning.
    """
    _self_heal_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            "Memory self-heal task %s failed unexpectedly: %s",
            task.get_name(),
            format_exception_message(exc),
        )


def _schedule_memory_self_heal(agent: Agent, scope_id: str | None) -> None:
    """Fire-and-forget an automatic memory self-heal pass for *agent*.

    Called when the combined memory context breaches the assignment prompt
    cap.  ``auto_heal_agent_memory`` throttles its agent-wide hygiene per agent and its compression per lane via
    Redis, opens its own DB session, and never raises — so scheduling it
    from the run setup path is safe and cheap.  *scope_id* is the lane the
    run reads (``assignment_memory_scope``), whose medium-term summary the
    heal pass compresses.
    """
    from app.services.persistent_memory import auto_heal_agent_memory

    # Name the task with the agent id so the done-callback failure log can
    # be correlated back to the agent that scheduled it.
    task = asyncio.create_task(
        auto_heal_agent_memory(str(agent.id), scope_id=scope_id),
        name=f"memory-self-heal-{agent.id}",
    )
    _self_heal_tasks.add(task)
    task.add_done_callback(_consume_self_heal_task)


async def publish_action_proposal(
    user_id: str,
    run_id: str,
    assignment_name: str,
    proposals: list,
    *,
    is_company: bool = False,
    is_autonomous: bool = False,
) -> None:
    """Publish a per-tool approval request to the SSE activity stream.

    Routing mirrors :func:`_publish_run_status` exactly rather than inventing
    its own: both describe the same run, and a reviewer who can see a run's
    status must see its approval request on the same channel or the prompt
    arrives for people who cannot act on it — or worse, never reaches the one
    who can.

    The endpoint is a raw passthrough that emits no SSE ``event:`` names, so
    clients discriminate on the JSON ``type`` field.

    ``is_autonomous`` lets the UI mark a proposal nobody is actively waiting on,
    which is the case that also triggers the owner notification.

    Best-effort, like the status publish: a dropped event costs live rendering,
    not correctness. The proposals are persisted on the run, so a client that
    missed the event still finds them on its next poll.
    """
    from app.core.redaction import redact_known_secrets_for_display
    from app.tools.approval import redact_proposals

    try:
        redis = await redis_client()

        def _payload(items: list) -> str:
            return json.dumps(
                {
                    "type": "action_proposal",
                    "run_id": run_id,
                    "assignment_name": assignment_name,
                    "is_autonomous": is_autonomous,
                    # A person reads it: a secret variable shows as [secret].
                    "proposals": redact_known_secrets_for_display(items),
                }
            )

        if is_company:
            # Every authenticated user subscribes to this channel, and only an
            # admin may decide a company-managed run. The event is a prompt to
            # go look, not the payload: the decider's client reads the full
            # proposals from ``GET /pending-approvals``, which gates them.
            await redis.publish("activity:company", _payload(redact_proposals(proposals) or []))
        elif user_id and user_id != "None":
            # A personal run: this channel has exactly one subscriber, its owner,
            # who is also its only decider.
            await redis.publish(f"activity:{user_id}", _payload(proposals))
        else:
            await redis.publish("activity:company", _payload(redact_proposals(proposals) or []))
    except Exception as exc:
        logger.warning(
            "SSE action_proposal publish failed: %s", format_exception_message(exc)
        )


async def _publish_run_status(
    user_id: str,
    run_id: str,
    status: str,
    assignment_name: str,
    summary: str | None = None,
    *,
    is_company: bool = False,
) -> None:
    """Publish assignment run status to the SSE activity stream.

    Company-managed runs broadcast to the ``activity:company`` channel so
    every connected user receives live status updates; personal runs publish
    to the owner's ``activity:{user_id}`` channel. The company decision is
    driven by the explicit ``is_company`` flag (``run.is_company_managed``)
    rather than ``user_id`` being unset — company-managed runs now always
    carry an owner / run-as ``user_id``, so the old ``user_id``-absent proxy
    would misroute their status updates to a single user. Mirrors the
    fan-out logic in ``services.notify._notify_web``. The summary (an
    answer, an error, a question) shows a secret variable as ``[secret]``.
    """
    from app.core.redaction import redact_known_secrets_for_display

    try:
        redis = await redis_client()
        payload = json.dumps(
            {
                "type": "assignment_run",
                "run_id": run_id,
                "status": status,
                "assignment_name": assignment_name,
                "summary": redact_known_secrets_for_display(summary or ""),
            }
        )
        if is_company:
            # Company-managed: broadcast to the shared channel so every
            # connected user sees it (not just the owner / run-as user).
            await redis.publish("activity:company", payload)
        elif user_id and user_id != "None":
            await redis.publish(f"activity:{user_id}", payload)
        else:
            # Owner-less personal run (shouldn't normally happen) — fall
            # back to the shared channel rather than dropping the event.
            await redis.publish("activity:company", payload)
    except Exception as exc:
        logger.warning("SSE activity publish failed: %s", format_exception_message(exc))


# ---------------------------------------------------------------------------
# Result summary selection.
#
# ``run_agent_loop`` returns ``loop_result.content`` — the LAST assistant
# message — which is normally the substantive deliverable. But when the LLM
# emits the actual deliverable in an EARLIER iteration (typically combined
# with a tool call) and ends the loop with a brief recap such as "Your
# digest is ready above! Highlights: 1. … 2. …", that final message
# references content the user never sees in the Result card or notification.
# This caused inbox-digest runs to surface "Your daily digest is ready
# above!" instead of the digest itself, intermittently and confusingly.
#
# ``select_best_result_summary`` walks the full conversation and prefers
# the longest substantial assistant message. When the final answer is
# clearly a brief recap of a longer earlier message, both are combined so
# neither the deliverable nor the wrap-up info (priorities, follow-ups)
# is lost.
# ---------------------------------------------------------------------------

_MIN_SUBSTANTIAL_RESULT_CHARS = 200
"""Minimum length for an earlier assistant message to be considered the
substantive deliverable instead of an intermediate thinking-out-loud turn
like 'Let me fetch your emails first…'."""

_SHORT_RECAP_RATIO = 0.5
"""If the final assistant message is at most this fraction of the longest
earlier substantive message, treat the final message as a brief recap and
combine both. 0.5 = final is 50% or less of the earlier deliverable."""


def select_best_result_summary(
    final_content: str | None,
    messages: list,
    *,
    min_substantial_chars: int = _MIN_SUBSTANTIAL_RESULT_CHARS,
    short_recap_ratio: float = _SHORT_RECAP_RATIO,
) -> str:
    """Return the most informative assistant content to display as the run result.

    Args:
        final_content: ``loop_result.content`` from ``run_agent_loop`` — the
            text of the final assistant turn.
        messages: The full conversation list mutated in-place by the agent
            loop. Items are expected to expose ``role`` and ``content``
            attributes (``LLMMessage``-like).
        min_substantial_chars: An earlier assistant message must be at least
            this many characters to be considered the substantive deliverable.
        short_recap_ratio: If the final message is at or below this fraction
            of the longest earlier substantive message, the final is treated
            as a brief recap and combined with the earlier message.

    Returns:
        The selected result summary string. Falls back to ``final_content``
        (or empty string) when no substantive earlier message exists.

    Behavior:
        * Healthy case — the final answer is the longest assistant message:
          returns ``final_content`` unchanged.
        * Recap case — an earlier assistant message is substantive AND the
          final message is much shorter (e.g. "Your digest is ready
          above!"): returns ``"{earlier}\\n\\n---\\n\\n{final}"`` so the
          deliverable is preserved without dropping any wrap-up info.
        * Empty final case — the loop ended without a final answer but
          earlier turns produced content: returns the longest substantive
          earlier message.
    """
    final = final_content or ""
    final_has_content = bool(final.strip())

    # Each agent-loop continuation guard appends a NON-deliverable "checkpoint"
    # assistant message to the working transcript (immediately followed by its
    # system nudge) purely so the next turn has continuity: a retracted "about to
    # act" narration (stall guard), a tool call emitted as markup (leaked-tool-
    # call guard), or a resume promise. None must ever be surfaced as the run
    # result - identify it by the adjacent nudge and skip it. Imported lazily to
    # avoid importing the heavy agent-loop module at assignment_helpers import
    # time.
    from app.tools.agent_loop import CONTINUATION_NUDGES

    # Preserve original content (no implicit ``.strip()``) so callers see
    # exactly what the LLM emitted; we only strip for the substantive check.
    candidates: list[str] = []
    for i, m in enumerate(messages):
        if getattr(m, "role", None) != "assistant":
            continue
        content = getattr(m, "content", None) or ""
        if not content.strip():
            continue
        nxt = messages[i + 1] if i + 1 < len(messages) else None
        if (
            nxt is not None
            and getattr(nxt, "role", None) == "system"
            and (getattr(nxt, "content", None) or "") in CONTINUATION_NUDGES
        ):
            continue
        candidates.append(content)

    if not candidates:
        return final

    # Earlier substantive turns = every assistant message except the final
    # one. We identify "the final one" by content equality, which avoids
    # depending on positional order (the agent loop appends in order, but
    # nothing in the contract guarantees it).
    earlier = [c for c in candidates if c != final]

    if final_has_content:
        if earlier:
            longest_earlier = max(earlier, key=len)
            if (
                len(longest_earlier.strip()) >= min_substantial_chars
                and len(final.strip())
                <= len(longest_earlier.strip()) * short_recap_ratio
            ):
                logger.info(
                    "result_summary: final message (%d chars) is a brief recap "
                    "of a longer earlier message (%d chars) — combining both "
                    "so the deliverable is preserved.",
                    len(final.strip()),
                    len(longest_earlier.strip()),
                )
                return f"{longest_earlier}\n\n---\n\n{final}"
        return final

    # Final was empty — surface the longest substantive earlier turn.
    longest = max(candidates, key=len)
    logger.info(
        "result_summary: final loop response was empty — falling back to the "
        "longest earlier assistant message (%d chars).",
        len(longest.strip()),
    )
    return longest


def select_best_chat_reply(
    final_content: str | None,
    run_messages: list,
    *,
    delivered_segments: list[str] | None = None,
) -> str:
    """Chat-side deliverable rescue.

    Reasoning-forward models (observed consistently with ``gpt-6-astra``)
    often front-load the substantive answer BEFORE side-effect tool calls
    (``store_memory``, …) and close with a one-line recap after the tool
    results come back.  The agent loop's preamble hold-back routes that
    pre-tool text into the reasoning pane, so the persisted chat reply is
    only the closer — the user reads "Stored." and never sees the answer.

    This is the chat twin of :func:`select_best_result_summary` with two
    chat-specific constraints:

    * *run_messages* must be the slice of the conversation appended **during
      this run** (``messages[pre_loop_len:]``) — the chat ``messages`` list
      contains prior turns, and an earlier turn's answer must never be
      re-surfaced into the current reply.
    * *delivered_segments* are the ``segment_complete`` texts already
      persisted as real mid-turn messages.  They were shown to the
      user once; merging them again would duplicate visible content, so any
      assistant message whose content matches one is excluded.

    Returns the selected reply string (``final_content`` unchanged in the
    healthy case).
    """
    if delivered_segments:
        delivered = {s for s in delivered_segments if s and s.strip()}
        if delivered:
            run_messages = [
                m
                for m in run_messages
                if getattr(m, "role", None) != "assistant"
                or (getattr(m, "content", None) or "") not in delivered
            ]
    return select_best_result_summary(final_content, run_messages)


def _serialize_payload(data: dict, max_chars: int = _MAX_PAYLOAD_CHARS) -> str:
    """Serialize *data* to JSON, truncating if it exceeds *max_chars*.

    * Small payloads (< 50 K chars) are pretty-printed (indent=2).
    * Larger payloads use compact JSON to save tokens.
    * If still over *max_chars*, the string is truncated and a notice is
      appended so the LLM knows the data is incomplete.
    """
    compact = json.dumps(data, default=str)
    if len(compact) <= _PRETTY_PRINT_THRESHOLD:
        return json.dumps(data, indent=2, default=str)
    if len(compact) <= max_chars:
        logger.debug(
            "Trigger payload using compact JSON (%d chars, threshold=%d)",
            len(compact),
            _PRETTY_PRINT_THRESHOLD,
        )
        return compact
    # Truncate and inform the LLM
    logger.warning(
        "Trigger payload truncated: %d chars → %d chars",
        len(compact),
        max_chars,
    )
    truncated = compact[:max_chars]
    return (
        truncated
        + "\n\n... [TRUNCATED — payload was "
        + f"{len(compact):,} chars, limit is {max_chars:,}. "
        + "Analyze what is available above.] ..."
    )


def _build_trigger_information(run) -> str:
    """Build the ``--- Trigger Information ---`` block for a run.

    Returns the trigger context + payload sections (the same block that is
    appended to the main assignment prompt) so the activation gate and the
    agentic loop both see identical incoming-event context. Returns ``""``
    when the run carries no usable trigger context or payload.

    The block is intentionally derived from the run (not from the rendered
    template), so an activation prompt that never references ``{{trigger.*}}``
    still gives the gate the data it needs to decide. Without this, the gate is
    blind to the very event it is gating and skips every run (e.g. answering
    ``SKIP: NO ISSUE CONTENT PROVIDED``).
    """
    sections: list[str] = []

    # 1. Trigger context – metadata about the trigger origin (type, timestamp,
    #    user, provider, cron expression, etc.). Raw headers/body are excluded
    #    (body duplicates the payload below).
    if run.trigger_context:
        ctx_display = {
            k: v
            for k, v in run.trigger_context.items()
            if k not in ("headers", "body")
        }
        if ctx_display:
            sections.append("Trigger Context:\n" + _serialize_payload(ctx_display))

    # 2. Trigger payload – the actual incoming data. Internal routing metadata
    #    (_webhook_meta) is stripped; large payloads (e.g. OpenCTI STIX bundles)
    #    are truncated by _serialize_payload to stay within context limits.
    #    Webhook and integration-event payloads come from external,
    #    attacker-influenceable sources, so they are quarantined in an
    #    untrusted-content block (see content_safety.py). Manual/schedule
    #    payloads are user-authored and stay unwrapped.
    if run.trigger_payload:
        display_payload = {
            k: v for k, v in run.trigger_payload.items() if k != "_webhook_meta"
        }
        # The provider's trim of what the run is shown (a GitHub push without
        # its duplicated head commit, whole messages and URL templates); the
        # stored payload, the template context and the filter keep it whole.
        ctx = run.trigger_context or {}
        display_payload = shape_run_payload(
            ctx.get("provider"), ctx.get("event_key") or ctx.get("event"), display_payload
        )
        if display_payload:
            payload_text = _serialize_payload(display_payload)
            trigger_type = (run.trigger_context or {}).get("type")
            if trigger_type in _UNTRUSTED_TRIGGER_TYPES:
                payload_text = wrap_untrusted(
                    payload_text, source=f"{trigger_type} trigger"
                )
            sections.append("Trigger Payload:\n" + payload_text)

    if not sections:
        return ""
    return "\n\n--- Trigger Information ---\n" + "\n\n".join(sections)


def _render_prompt(
    template: str,
    context: dict,
    variables: dict[str, str] | None = None,
    *,
    untrusted_trigger_source: str | None = None,
) -> str:
    """Template rendering: replace ``{{path}}`` and ``{{var_id.UUID}}`` placeholders.

    ``{{var_id.UUID}}`` is resolved from *variables* (platform variables).
    Other ``{{path}}`` placeholders are resolved from *context* using dot-notation.

    When *untrusted_trigger_source* is set (webhook / integration_event runs), any
    substituted ``{{trigger.*}}`` value is quarantined in a ``wrap_untrusted``
    block. Without this, an assignment prompt that templates a trigger field
    (e.g. ``{{trigger.body}}``) splices raw attacker-influenceable content
    straight into the trusted instructions, bypassing the spotlighting defense
    that ``_build_trigger_information`` applies to the appended payload block.
    """
    if variables:
        from app.services.variable_resolver import substitute_variables

        template = substitute_variables(template, variables)

    def replacer(match: re.Match) -> str:
        path = match.group(1).strip()
        if path.startswith("var_id."):
            return match.group(0)
        parts = path.split(".")
        value = context
        for part in parts:
            value = (
                value.get(part, "")
                if isinstance(value, dict)
                else getattr(value, part, "")
            )
        if value is None or value == "":
            return ""
        rendered = str(value)
        if untrusted_trigger_source and parts and parts[0] == "trigger":
            return wrap_untrusted(rendered, source=untrusted_trigger_source)
        return rendered

    return re.sub(r"\{\{(.+?)\}\}", replacer, template)


async def _warn_unresolved_placeholders(
    db: AsyncSession,
    text: str,
    *,
    where: str,
    agent_name: str,
    assignment_name: str,
    assignment_id: uuid.UUID,
    resolved_as: uuid.UUID | None,
    rendered_roots: Collection[str] = (),
) -> None:
    """Log the ``{{...}}`` placeholders an author-written template leaves
    unresolved; the literal string reaches the LLM and poisons tool calls.

    *text* is one author-written section (*where* names it: ``persona``,
    ``assignment prompt``, ...) after its ``{{var_id.<uuid>}}`` were
    substituted - never the assembled system prompt, whose memory, identity
    and platform sections are data that may quote ``{{...}}`` verbatim.
    For a template the run renders against its context (the
    assignment and activation prompts), *rendered_roots* names the context
    roots (``trigger``, ``agent``, ``elements``): a ``{{trigger.x}}`` is
    rendered, not unresolved, and the values it renders to are not scanned.

    A ``{{var_id.<uuid>}}`` is well formed: it survives because no variable
    carries the id (deleted, or copied from another install) or because
    *resolved_as* - the identity the run resolved its variables as - cannot
    see it. Those are named apart, with that identity. Anything else is a
    placeholder that isn't a valid template variable (e.g.
    ``{{BRAINSTORM_DB_ID}}``).

    The same finding is logged at most once an hour per assignment and
    section (and identity, for variables) through
    ``should_log_prompt_warning``: a schedule every five minutes would
    otherwise repeat it on every run.
    """
    from app.services.prompt_budget import should_log_prompt_warning
    from app.services.variable_resolver import (
        VAR_ID_PATTERN,
        extract_variable_ids,
        missing_variable_ids,
    )

    if not text or not _UNRESOLVED_RE.search(text):
        return
    variable_ids = sorted(extract_variable_ids(text))
    others = list(
        dict.fromkeys(
            f"{{{{{path}}}}}"
            for m in _UNRESOLVED_RE.findall(VAR_ID_PATTERN.sub("", text))
            if (path := m.strip()).split(".", 1)[0] not in rendered_roots
        )
    )
    # Debounced before the variable lookup, so a repeated run queries nothing.
    # The variable finding depends on the identity it names; the other does not.
    key = f"unresolved_placeholders:{assignment_id}:{where}"
    if variable_ids and not should_log_prompt_warning(
        f"{key}:{resolved_as}:{'|'.join(variable_ids)}"
    ):
        variable_ids = []
    if others and not should_log_prompt_warning(f"{key}:{'|'.join(others)}"):
        others = []
    if variable_ids:
        missing = await missing_variable_ids(db, variable_ids)
        hidden = [vid for vid in variable_ids if vid not in missing]
        causes = []
        if missing:
            causes.append(
                "no such variable (deleted, or copied from another install): "
                + ", ".join(sorted(missing))
            )
        if hidden:
            causes.append(
                "not visible to that identity (share them with it or one of "
                "its groups, or make them company-managed): " + ", ".join(hidden)
            )
        logger.warning(
            "Unresolved platform variable(s) in %s for agent '%s' / "
            "assignment '%s', resolved as %s: %s - these placeholders are "
            "sent verbatim to the LLM.",
            where,
            agent_name,
            assignment_name,
            f"user {resolved_as}" if resolved_as else "no user (company-managed variables only)",
            "; ".join(causes),
        )
    if others:
        logger.warning(
            "Unresolved placeholder(s) in %s for agent '%s' / "
            "assignment '%s': %s - these are sent verbatim to the LLM and "
            "will likely cause tool errors. Use {{var_id.UUID}} for platform "
            "variables or {{trigger.*}} for trigger data.",
            where,
            agent_name,
            assignment_name,
            ", ".join(others),
        )


def _build_elements_context(element_bindings: dict) -> dict[str, str]:
    """Build human-readable template variables from integration element bindings.

    e.g. ``{{elements.teams_channels}}`` -> ``"General (team: Engineering), Random (team: Product)"``
    """
    elements_context: dict[str, str] = {}
    for key, items in element_bindings.items():
        if not items:
            continue
        # key is like "microsoft:teams_channel"
        # Build a human-readable summary
        parts = []
        for item in items:
            name = item.get("name", "Unknown")
            group = item.get("group", "")
            if group:
                parts.append(f"{name} (team: {group})")
            else:
                parts.append(name)
        # Store under both the full key and a simplified alias
        elements_context[key] = ", ".join(parts)
        # Also store under element type only (e.g. "teams_channels")
        _, _, etype = key.partition(":")
        if etype:
            elements_context[etype + "s"] = ", ".join(parts)
            elements_context[etype] = ", ".join(parts)
    return elements_context


def assignment_memory_scope(
    run_user_id: uuid.UUID | str | None, agent_owner_id: uuid.UUID | str | None
) -> str | None:
    """The agent-memory lane an assignment run reads and writes: its acting user's.

    The acting user is the run's identity (``run.user_id``), else the agent's
    owner. A run never writes the agent-level lane: what it consolidates
    reaches the durable pool every user's chat with the agent reads, and its
    summary can hold what the acting user's own integrations and knowledge
    bases returned. ``None`` when the run has no user at all: no lane.
    """
    acting = run_user_id or agent_owner_id
    return str(acting) if acting else None


async def store_assignment_agent_memory(
    *,
    agent_id: str,
    scope_id: str | None,
    source: str,
    summary: str,
    consolidate: bool,
    llm: Any,
    model: str | None,
    is_company_managed: bool,
    background_tasks: set[asyncio.Task],
) -> None:
    """Store a run's summary in its lane (``assignment_memory_scope``).

    When the lane reaches its consolidation threshold and *consolidate* is
    set, the consolidation runs detached, held in *background_tasks*, and
    persists in the same user scope. A run with no lane stores nothing.
    """
    if scope_id is None:
        return
    from app.services.agent_memory import (
        CONSOLIDATION_THRESHOLD,
        consolidate_memory,
        store_short_term_memory,
    )

    counter = await store_short_term_memory(
        agent_id,
        source=source,
        summary=summary,
        source_type="assignment",
        scope_id=scope_id,
    )
    if consolidate and counter >= CONSOLIDATION_THRESHOLD:
        # asyncio only holds weak references: the set keeps the task alive.
        task = asyncio.create_task(
            consolidate_memory(
                agent_id,
                llm,
                scope_id=scope_id,
                is_company_managed=is_company_managed,
                model=model,
            )
        )
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)


async def _load_memory_context(
    db: AsyncSession,
    agent: Agent,
    user_id: uuid.UUID | None,
    query: str | None = None,
) -> tuple[str, str]:
    """Load agent and user memory context for the assignment prompt.

    The agent's Redis tiers are read from the run's lane
    (``assignment_memory_scope``), never the agent-level one.
    Runs independent memory loads concurrently where possible.
    When *query* is provided (the rendered assignment prompt), persistent
    facts are selected by semantic relevance to it instead of pure recency.
    Returns ``(agent_memory_text, user_memory_text)``.
    """
    import asyncio as _aio

    # ── Prepare concurrent tasks ──────────────────────────────────────
    async def _load_agent_mem() -> str:
        if not (agent.memory_short_term_enabled or agent.memory_medium_term_enabled):
            return ""
        scope_id = assignment_memory_scope(user_id, agent.user_id)
        if scope_id is None:
            return ""
        try:
            from app.services.agent_memory import format_memory_for_prompt, load_memory

            return format_memory_for_prompt(
                await load_memory(str(agent.id), scope_id=scope_id), agent_label=agent.name
            )
        except Exception as e:
            logger.warning(
                "Failed to load agent memory (agent=%s): %s",
                agent.id,
                format_exception_message(e),
            )
            return ""

    async def _load_persistent_mem() -> list:
        if not agent.memory_persistent_enabled:
            return []
        try:
            from app.services.builtin_tools.memory import _resolve_provider_model
            from app.services.persistent_memory import load_persistent_entries

            # Pre-resolve the embedding provider in a throwaway session so
            # the query embedding (HTTP) issues no work on the engine
            # session — the setup-phase transaction would otherwise pin the
            # pooled connection across the embed.
            _pm = await _resolve_provider_model()
            return await load_persistent_entries(
                db,
                agent_id=str(agent.id),
                user_id=str(user_id) if user_id else None,
                query=query,
                _provider_model=_pm,
                # Engine setup is read-only at this point (run status was
                # committed earlier), so releasing during the embed is safe.
                release_before_embed=True,
            )
        except Exception as e:
            logger.warning(
                "Failed to load persistent memory (agent=%s, user=%s): %s",
                agent.id,
                user_id,
                format_exception_message(e),
            )
            return []

    async def _load_user_mem() -> str:
        if not user_id or not agent.memory_user_enabled:
            return ""
        try:
            from app.services.user_memory import (
                format_user_memory_for_prompt,
                load_user_memory,
            )

            return format_user_memory_for_prompt(await load_user_memory(str(user_id)), agent_id=agent.id)
        except Exception as e:
            logger.warning(
                "Failed to load user memory (user=%s, agent=%s): %s",
                user_id,
                agent.id,
                format_exception_message(e),
            )
            return ""

    # Agent Redis memory and user Redis memory are independent of the DB
    # session, so they can run concurrently with persistent memory loads.
    agent_mem, persistent_entries, user_memory_text = await _aio.gather(
        _load_agent_mem(),
        _load_persistent_mem(),
        _load_user_mem(),
    )

    # Persistent entries have no size bound: the block takes what the Redis
    # tiers leave of the budget, in whole entries, most relevant first.
    persistent_mem = ""
    if persistent_entries:
        from app.services.persistent_memory import format_persistent_context

        persistent_mem = format_persistent_context(
            persistent_entries,
            max_chars=_MAX_ASSIGNMENT_MEMORY_CHARS - (len(agent_mem) + 2 if agent_mem else 0),
        )

    agent_memory_text = (
        f"{agent_mem}\n\n{persistent_mem}"
        if agent_mem and persistent_mem
        else agent_mem or persistent_mem
    )

    # Backstop for the Redis tiers: the medium-term cap in
    # format_memory_for_prompt bounds them, and the persistent block above
    # fits in what they leave, so only a Redis block larger than the whole
    # budget gets here.
    if len(agent_memory_text) > _MAX_ASSIGNMENT_MEMORY_CHARS:
        # Self-healing: schedule an automatic per-agent maintenance
        # pass (persistent-tier prune/cap/consolidate/re-embed + Redis
        # medium-term compression) instead of asking operators to run it
        # manually from a log line no user ever sees.  Throttled per agent and lane
        # inside auto_heal_agent_memory, so this is a cheap no-op on
        # subsequent runs within the throttle window.
        _schedule_memory_self_heal(agent, assignment_memory_scope(user_id, agent.user_id))
        logger.warning(
            "Agent '%s' combined memory context (%d chars) exceeds cap "
            "(%d chars) for assignment run — truncating for this run and "
            "scheduling an automatic memory self-heal pass.",
            agent.name,
            len(agent_memory_text),
            _MAX_ASSIGNMENT_MEMORY_CHARS,
        )
        notice = "\n[...memory truncated to fit the prompt budget...]"
        # Keep the final block (notice included) within the cap so the
        # constant is a strict upper bound for prompt-size budgeting.
        agent_memory_text = (
            agent_memory_text[: _MAX_ASSIGNMENT_MEMORY_CHARS - len(notice)] + notice
        )

    return agent_memory_text, user_memory_text


async def _build_system_message(
    db: AsyncSession,
    agent: Agent,
    assignment: Assignment,
    user_id: uuid.UUID | None,
    user_memory_text: str,
    agent_memory_text: str,
    element_bindings: dict,
    variables: dict[str, str] | None = None,
    *,
    has_memory_tools: bool = False,
    knowledge_base_prompt: str | None = None,
    coordination_tools_prompt: str | None = None,
    include_untrusted_policy: bool = True,
    resolved_skills: list | None = None,
    templated_sections: list[tuple[str, str]] | None = None,
    organization_summary: str = "",
) -> str:
    """Build the system prompt for the assignment agentic loop.

    Combines agent persona, guardrails, user identity, memory context,
    element bindings, and execution policy into a single system message.
    ``{{var_id.UUID}}`` placeholders in persona, guardrails, skills, and
    prompts are resolved when *variables* is provided.

    *include_untrusted_policy* controls the quarantine-marker policy
    paragraph.  Callers should pass ``False`` only when NO quarantined
    content can appear in the run (:func:`assignment_untrusted_policy_needed`
    is false).  Defaults to ``True`` (safe).

    *resolved_skills* carries the skills the caller already resolved.
    The engine resolves them itself because it also needs them to decide
    whether to register the ``load_skill`` tool — resolving twice would double
    the query cost and, worse, could disagree with itself if a skill changed in
    between.  Empty (or ``None``) renders no skill section at all.

    It does not report the ``{{...}}`` placeholders that survive rendering.
    When *templated_sections* is a list, it receives a ``(section, text)``
    pair for every author-written section rendered with *variables*
    (persona, guardrails, attached prompts, pinned skills), and the caller
    passes each to :func:`_warn_unresolved_placeholders` itself, with the
    identity it resolved *variables* as, which *user_id* is not always (the
    assignment engine resolves them as the run's user, else the agent's
    owner). Only those sections are scanned: memory, identity and platform
    sections are data and may quote ``{{...}}`` verbatim. A caller
    that skips it sends the literal placeholders to the LLM with nothing in
    the log. The engine warns right after this call.
    """
    from app.services.builtin_customer_layer import persona_with_customer_notes
    from app.services.variable_resolver import substitute_variables

    # A built-in's organization notes follow its seeded persona (#914).
    _persona_text = substitute_variables(
        persona_with_customer_notes(agent, agent.persona), variables or {}
    )
    system_parts = [_persona_text]
    # *organization_summary* (``organization_context_injection``, #11533)
    # opens the prompt; the engine passes "" when the cascade resolves off or
    # the run's fleet block already carries the profile.
    if organization_summary:
        system_parts.insert(0, organization_summary)

    # Labelled user-content sections for the large-prompt breakdown
    # warning below (everything unlabelled counts as platform scaffolding).
    _prompt_sections: list[tuple[str, str]] = [("persona", _persona_text)]

    if agent.guardrails:
        _guardrails_text = "\n\nGuardrails:\n" + substitute_variables(
            agent.guardrails, variables or {}
        )
        system_parts.append(_guardrails_text)
        _prompt_sections.append(("guardrails", _guardrails_text))

    # ── Attached prompts ──────────────────────────────────────────────
    if agent.prompt_ids:
        from app.services.builtin_tools import load_agent_prompts

        prompts_text = await load_agent_prompts(
            db, agent.prompt_ids, variables=variables
        )
        if prompts_text:
            system_parts.append(prompts_text)
            _prompt_sections.append(("attached_prompts", prompts_text))

    # ── Agent skills (agentskills.io specification) ──────────────────
    if resolved_skills:
        _cap_parts: list[str] = []
        if agent.integration_bindings:
            _cap_parts.append(
                f"integrations: {', '.join(agent.integration_bindings.keys())}"
            )
        if agent.mcp_server_ids:
            _cap_parts.append(f"{len(agent.mcp_server_ids)} MCP server(s)")
        if agent.custom_tool_ids:
            _cap_parts.append(f"{len(agent.custom_tool_ids)} custom tool(s)")
        if agent.builtin_tool_ids:
            _cap_parts.append(f"built-in tools: {', '.join(agent.builtin_tool_ids)}")
        if agent.knowledge_base_ids:
            _cap_parts.append(f"{len(agent.knowledge_base_ids)} knowledge base(s)")
        _capabilities = (
            "; ".join(_cap_parts) if _cap_parts else "assignment execution capabilities"
        )

        from app.services.builtin_tools import (
            build_pinned_skills_prompt,
            build_skills_catalog_prompt,
        )

        pinned_prompt = build_pinned_skills_prompt(resolved_skills, variables=variables)
        if pinned_prompt:
            system_parts.append(pinned_prompt)
            _prompt_sections.append(("skills", pinned_prompt))
        catalog_prompt = build_skills_catalog_prompt(
            resolved_skills, agent_capabilities=_capabilities
        )
        if catalog_prompt:
            system_parts.append(catalog_prompt)
            _prompt_sections.append(("skills_catalog", catalog_prompt))

    # ── Agent self-context (name + description) ──────────────────────
    from app.services.builtin_tools import ACTING_FOR_VERB, build_agent_context, build_identity_context

    system_parts.append(build_agent_context(agent))

    # ── Knowledge base capabilities (read / write) ───────────────────
    if knowledge_base_prompt:
        system_parts.append(knowledge_base_prompt)

    # ── Coordination tools nudge (work items / shared state) ─────────
    # Capability-gated by the caller; names the durable coordination tools so
    # the agent reaches for them proactively instead of needing the persona to
    # spell it out.
    if coordination_tools_prompt:
        system_parts.append(coordination_tools_prompt)

    # ── User identity (centralised) ──────────────────────────────────
    # Resolve user record and inject date/time + full profile + guardrail
    # via the single shared builder.

    _u_record = None
    if user_id:
        from app.models.user import User as _UserModel

        _u_result = await db.execute(select(_UserModel).where(_UserModel.id == user_id))
        _u_record = _u_result.scalar_one_or_none()

    # The clock line opens the volatile tail (``split_system_prompt``): the
    # identity and the fenced memory are held here and written after every
    # stable section below, just before the policies that close the prompt,
    # so the cached prefix is the same on every run of the assignment.
    _tail_parts: list[str] = build_identity_context(_u_record, role_verb=ACTING_FOR_VERB)

    # Memory is fenced, after ``_load_memory_context`` capped it.
    _memory_block = wrap_memory(user_memory_text, agent_memory_text)
    if _memory_block:
        _tail_parts.append(f"\n{_memory_block}")
    if user_memory_text:
        _prompt_sections.append(("user_memory", f"\n{user_memory_text}"))
    if agent_memory_text:
        _prompt_sections.append(("agent_memory", f"\n{agent_memory_text}"))

    system_parts.append(f"\nAssignment: {assignment.name}")

    # Inject element binding context so the LLM knows which specific
    # integration elements (channels, repos, etc.) to focus on.
    if element_bindings:
        from app.services.builtin_tools import format_element_bindings_prompt

        eb_prompt = format_element_bindings_prompt(element_bindings)
        if eb_prompt:
            system_parts.append(eb_prompt)

    # ── Channel publish context (whitelisted channels) ────────────────
    # Resolve effective channel_config (assignment overrides agent if set)
    # and inject channel names/IDs so the LLM can send messages directly.
    effective_ch_config = (
        assignment.channel_config
        if assignment.channel_config is not None
        else agent.channel_config
    )
    if effective_ch_config:
        from app.services.integration_tools import format_channel_context

        ch_ctx = format_channel_context(effective_ch_config)
        if ch_ctx:
            system_parts.append(ch_ctx)

    # ── Output format instruction ──────────────────────────────────────
    # Assignments don't have channel context (they execute server-side),
    # so we always use the agent's configured format.
    from app.services.output_format import build_output_format_prompt

    _format_prompt = build_output_format_prompt(
        agent_format=agent.output_format or "markdown",
        channel_platform=None,
        output_schema=agent.output_schema,
    )
    if _format_prompt:
        system_parts.append(_format_prompt)

    if assignment.ai_label:
        system_parts.append(assignment.ai_label_prompt or DEFAULT_AI_LABEL_PROMPT)

    # --- Human-in-the-loop instructions ---
    hitl_enabled, hitl_prompt = resolve_hitl(agent, assignment)
    if hitl_enabled:
        system_parts.append(hitl_prompt or DEFAULT_HITL_PROMPT)

    # --- Persistent memory guidance ---
    if has_memory_tools:
        system_parts.append(
            "\nPersistent Memory: before acting, use `search_memory` to "
            "check for relevant context from previous runs; after "
            "completing the task, use `store_memory` to save key facts, "
            "decisions, or patterns worth remembering."
        )

    # --- Execution guardrails ---
    system_parts.append(
        "\nExecution Policy: send each piece of content to external "
        "channels (Teams, Slack, email, ...) EXACTLY ONCE — never re-post "
        "a message you already sent successfully."
    )

    # ── Volatile tail: clock, identity, memory ───────────────────────
    system_parts.extend(_tail_parts)

    # Untrusted-content quarantine convention: webhook / integration-event
    # trigger payloads, web_fetch results and integration answers are wrapped
    # in <<UNTRUSTED-...>> blocks (see content_safety.py).  The marker policy
    # is skipped only when the caller determined no quarantined content
    # can appear in this run.
    if include_untrusted_policy:
        system_parts.append(UNTRUSTED_CONTENT_POLICY)

    # Final security anchor (kept LAST). Assignments run server-side with no
    # interactive user making identity claims, so the anti-impersonation
    # clause is folded in only for shared (company-managed) agents — and only
    # when an authenticated user was actually resolved (``_u_record``). On a
    # schedule/manual run with no acting user there is no identity line "shown
    # above", so the clause would be misleading and is omitted.
    system_parts.append(
        build_security_policy(
            include_identity=agent.is_company_managed and _u_record is not None
        )
    )

    full_prompt = "\n".join(system_parts)

    # ── System prompt size observability ───────────────────────────────
    # Mirror the chat-side warning so oversized assignment prompts also
    # surface in logs. Very large prompts (~20K+ tokens) routinely cause
    # instruction-following degradation and contribute to slow LLM
    # responses on agents that have accreted many attached prompts /
    # skills / element bindings over time.  Debounced per assignment and
    # enriched with a per-component breakdown.
    from app.services.prompt_budget import (
        LARGE_PROMPT_TOKEN_THRESHOLD,
        estimate_tokens,
        format_section_breakdown,
        should_log_large_prompt,
    )

    _prompt_chars = len(full_prompt)
    _prompt_est_tokens = estimate_tokens(full_prompt)
    if _prompt_est_tokens > LARGE_PROMPT_TOKEN_THRESHOLD and (
        should_log_large_prompt(f"assignment:{assignment.id}")
    ):
        logger.warning(
            "Large assignment system prompt for agent '%s' / assignment "
            "'%s': ~%d tokens (%d chars). Breakdown: %s. Very large "
            "prompts degrade instruction-following and slow LLM responses "
            "— trim the dominant component or move reference data into a "
            "knowledge base.",
            agent.name,
            assignment.name,
            _prompt_est_tokens,
            _prompt_chars,
            format_section_breakdown(_prompt_sections, _prompt_chars),
        )

    if templated_sections is not None:
        templated_sections.extend(
            (_TEMPLATED_SECTION_NAMES[label], text)
            for label, text in _prompt_sections
            if label in _TEMPLATED_SECTION_NAMES
        )

    return full_prompt


def resolve_assignment_kb_access(
    agent: Agent,
    assignment: Assignment,
) -> tuple[list[str], list[str]]:
    """Resolve the effective READ and WRITE knowledge base ids for a run.

    READ (``read_knowledge_base_ids``) is a non-destructive refinement of the
    agent's bound KBs and is BULLETPROOF — it can never resolve to "no search"
    while the agent has KBs:
      * ``None`` / ``[]`` → inherit ALL of the agent's bound KBs.
      * ``[ids]`` → that subset, intersected with the agent's bound KBs
        (stale/unbound ids are dropped); if nothing survives the intersection,
        it falls back to ALL of the agent's bound KBs.

    WRITE (``write_knowledge_base_ids``), refined from the agent's writable KBs
    (``Agent.knowledge_base_permissions`` entries flagged ``"write"``). Unlike
    read, write keeps an explicit "none" so an assignment can opt out of writing
    even when the agent could:
      * ``None`` → inherit ALL of the agent's writable KBs.
      * ``[ids]`` → that subset, intersected with the agent's writable KBs.
      * ``[]`` → none.

    The deprecated ``use_knowledge_bases`` / ``write_knowledge_base_enabled`` /
    ``target_knowledge_base_id`` fields are no longer consulted — the migration
    backfills the new fields so legacy behavior is preserved.
    """
    agent_kb_ids = [str(k) for k in (agent.knowledge_base_ids or [])]
    agent_kb_set = set(agent_kb_ids)

    # Read: refine to the selected subset; fall back to ALL agent KBs whenever
    # the selection is empty or fully stale. There is intentionally no "read
    # nothing" state — a bound agent always remains searchable.
    read_ids = [
        str(k)
        for k in (assignment.read_knowledge_base_ids or [])
        if str(k) in agent_kb_set
    ]
    if not read_ids:
        read_ids = list(agent_kb_ids)

    agent_writable = agent.writable_knowledge_base_ids
    writable_set = set(agent_writable)
    if assignment.write_knowledge_base_ids is not None:
        write_ids = [
            str(k)
            for k in assignment.write_knowledge_base_ids
            if str(k) in writable_set
        ]
    else:
        write_ids = list(agent_writable)

    return read_ids, write_ids
