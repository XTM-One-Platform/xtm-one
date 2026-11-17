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

"""Deep Investigation as a tool an agent can call.

A Deep Investigation can be started by a toggle in the composer. This tool is
something else: a dedicated agent that runs the
investigation on behalf of the agent the user is already talking to, and whose
output the calling agent can then use. That needs the investigation to be a
TOOL, because tools are how this platform lets one agent do a piece of work
for another.

Two things travel out of the run besides the report, and both are the caller's
to handle rather than the model's:

* ``on_event`` is forwarded, so the live block streams into the client while
  the investigation is running exactly as it does on the composer path. An
  agent turn that silently produced a report three minutes later would lose
  the whole point of the streamed step state.

* The investigation's id is appended to ``investigations_out``, mirroring
  ``handovers_out`` in ``agent_chat``. The block is attached to the assistant
  message by the chat layer from that list — NOT by asking the model to
  reproduce a ``[[INVESTIGATION:uuid]]`` marker it was shown. A model that
  forgets the line, or paraphrases it, would silently drop the panel; and
  "derive the record from execution, never from model prose" is the constraint
  this whole feature was built around.

The tool result is therefore the report WITHOUT the marker: the model has no
reason to see a token it must not be responsible for.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

#: Ceiling on how much of the report is handed back to the calling model.
#: The report is already bounded by the synthesis, so this only guards against
#: a pathological run flooding the agent's context.
MAX_RESULT_CHARS = 24_000

#: Investigations one agent turn may run.
#:
#: One, and the reason is not only cost. Asked to research APT28, the agent
#: decomposed the question into NINE facet-queries — TTPs, malware arsenal,
#: sanctions, campaigns, CVEs, targets, related units — and got back nine
#: reports each numbered from [1]. Merging nine incompatible keys into one
#: reply is impossible, so the consulting agent invented a scheme of its own
#: (`[A1]`, `[A6]`) and every citation in the answer stopped resolving. The
#: renumbering was not misbehaviour; it was the only coherent move left.
#:
#: Decomposition is also work the loop already does: one run queries every
#: source in the selected packs and follows the leads its search turns up.
#: Splitting the subject at the agent level buys nothing the orchestrator was
#: not already doing, and costs a search, a full source sweep and a synthesis
#: call per facet.
#:
#: So the agent picks the subject and the orchestrator covers it. A turn that
#: genuinely needs two subjects is two turns, and the refusal below says so.
MAX_INVESTIGATIONS_PER_TURN = 1


def _fit(body: str) -> str:
    """Trim the report to the context cap without severing its key.

    A flat `body[:MAX_RESULT_CHARS]` cuts wherever the cap lands, and the
    numbered source list is at the END — so a long report reached the calling
    model as prose full of markers with no key to resolve them against, which
    `render_markdown`'s own docstring calls worse than no markers at all: it
    looks sourced and cannot be checked.

    The key is kept and the prose gives way, with an elision mark so the model
    can see that something was dropped rather than inferring the report simply
    stopped.
    """
    if len(body) <= MAX_RESULT_CHARS:
        return body

    # The LAST contiguous run of key lines, not the first bracketed line in
    # the body. A report whose prose quotes a numbered list — or reproduces a
    # key — has an earlier `[1] ` at the start of a line, and treating that as
    # the key start over-trims real prose to preserve text that is not the
    # key. Safe either way (the tail still holds the real key), just wasteful.
    matches = list(re.finditer(r"^\[\d{1,3}\] ", body, re.M))
    key = None
    if matches:
        # Walk back while the previous match is still part of the same block,
        # so a key beginning at [1] is kept whole rather than cut to its tail.
        last = len(matches) - 1
        # Contiguous means "on the immediately preceding line": exactly one
        # newline between the two match starts.
        while last > 0 and body.count("\n", matches[last - 1].start(), matches[last].start()) == 1:
            last -= 1
        key = matches[last]
    elision = "\n\n[...]\n\n"
    if key:
        prose, tail = body[: key.start()], body[key.start() :]
        room = MAX_RESULT_CHARS - len(tail) - len(elision)
        if room > 0:
            return prose[:room].rstrip() + elision + tail
        # The key alone does not fit. Pathological, and there is no version of
        # this that keeps the contract — say so rather than pretend.
        logger.warning(
            "investigation report key exceeds the %d-char tool cap; the "
            "citations handed back will be incomplete",
            MAX_RESULT_CHARS,
        )
    return body[:MAX_RESULT_CHARS]


def build_deep_investigation_tool(
    db: AsyncSession,
    user_id: str | None,
    *,
    conversation_id: str | None = None,
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    investigations_out: list | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build the ``deep_investigation`` tool."""

    # Subjects this BUILDER ran, for the refusal message only. The cap itself
    # counts `investigations_out`, because that is the only thing shared
    # across the turn.
    #
    # A closure local was not a per-turn cap, whatever the old comment said.
    # `_consult_agent` starts a NESTED `run_agent_chat`, which builds its own
    # tool with its own empty counter while deliberately sharing
    # `investigations_out` — so an agent that consulted the investigation
    # agent twice got two runs, each believing it was the first. That is also
    # the mechanism that put two separately-numbered citation keys into one
    # turn, which is the bug this cap exists to prevent.
    ran: list[str] = []
    # The turn's investigation slots: the shared list when there is one, this
    # builder's own otherwise. A call RESERVES its slot before its first
    # await, because the loop runs one response's tool calls concurrently -
    # counted only after the run, two parallel calls both passed the cap, and
    # both ran on the one caller session.
    slots: list = investigations_out if investigations_out is not None else []

    def _already_run() -> int:
        """Investigations this TURN has started, nested consults included."""
        return len(slots)

    async def _deep_investigation(
        subject: str = "", source_packs: list[str] | None = None
    ) -> str:
        from app.services.deep_investigation_chat import run_deep_investigation_turn

        cleaned = (subject or "").strip()
        if not cleaned:
            return "Error: 'subject' is required — give the thing to investigate."

        if _already_run() >= MAX_INVESTIGATIONS_PER_TURN:
            # Refused with the reason, so the model can act on it rather than
            # rephrase and try again. The subject is named only when THIS
            # builder ran it: a nested consult sees the shared count without
            # knowing what the other agent asked.
            what = f"investigated {ran[0]!r}" if ran else "run an investigation"
            return (
                f"Refused: this turn has already {what}, and "
                "one investigation runs per turn. Its report is above — use "
                "it.\n\n"
                "A single run already queries every source in the selected "
                "packs and follows the leads its search turns up, so asking "
                "again for a narrower facet of the same subject re-does that "
                "work and produces a second, separately numbered set of "
                "citations that cannot be merged with the first.\n\n"
                "If the report does not cover what the user asked, say which "
                "part is missing rather than re-running."
            )

        from app.services.investigation_events import (
            INVESTIGATION_MARKER_RE,
            build_investigation_marker,
        )

        slot = object()
        slots.append(slot)
        run_id: str | None = None
        try:
            # Through the metered wrapper, never around it. It owns the agentic
            # quota check, the usage increment and a returned message for every
            # failure mode; a tool calling the run directly would let an agent
            # spend investigations nobody counted.
            results: list = []
            text = await run_deep_investigation_turn(
                db,
                subject=cleaned,
                user_id=user_id,
                conversation_id=conversation_id,
                on_event=on_event,
                source_pack_slugs=list(source_packs) if source_packs else None,
                result_out=results,
            )
            if results:
                run_id = str(results[0].investigation.id)
            else:
                # A run that failed after it started still names its record:
                # it keeps its slot (it was charged) and its block.
                marker = INVESTIGATION_MARKER_RE.search(text)
                run_id = marker.group(1) if marker else None
        finally:
            if run_id is None:
                slots.remove(slot)
            else:
                slots[slots.index(slot)] = run_id

        if run_id is None:
            # Refused before producing a run — quota, an empty subject, an
            # unknown pack — and `text` is already the message saying which.
            return text[:MAX_RESULT_CHARS]

        ran.append(cleaned)

        # The marker is stripped: attaching the block is the chat layer's job,
        # done from `investigations_out`, and a model shown the token might
        # reproduce it in the middle of a sentence or drop it entirely.
        body = text.replace(build_investigation_marker(run_id), "").strip()
        return _fit(body) or "The investigation produced no report."

    async def _read_investigation_report(investigation_id: str = "") -> str:
        """The stored report of one of the caller's runs, cited, by id."""
        import uuid as _uuid

        from sqlalchemy import select

        from app.models.investigation import Investigation, InvestigationStep
        from app.services.investigation_report import render_markdown, stored_report_draft

        try:
            run_id = _uuid.UUID(str(investigation_id).strip())
        except ValueError:
            return "Error: 'investigation_id' is not an investigation id."
        investigation = await db.get(Investigation, run_id)
        # The caller's own runs only, and an unknown id answered alike: the
        # result never says that someone else's run exists.
        if investigation is None or user_id is None or str(investigation.user_id) != str(user_id):
            return f"No investigation {run_id} is available to you."
        draft = stored_report_draft(investigation)
        if draft is None:
            return (
                f"The investigation of {investigation.subject_value!r} has no report: "
                "nothing its sources returned could back one."
            )
        steps = (
            (
                await db.execute(
                    select(InvestigationStep)
                    .where(InvestigationStep.investigation_id == run_id)
                    .order_by(InvestigationStep.position)
                )
            )
            .scalars()
            .all()
        )
        from app.services.investigation_opencti import build_knowledge, format_knowledge

        body = render_markdown(draft, steps=steps, leads_dropped=investigation.leads_dropped or 0)
        header = f"Deep Investigation of {investigation.subject_value!r} ({investigation.subject_type}), report:\n\n"
        # What the run established as STIX objects, for an agent asked to
        # record it in OpenCTI: from the steps, so it cannot invent one.
        knowledge = format_knowledge(build_knowledge(investigation.subject_value, steps))
        return _fit(header + body + (f"\n\n{knowledge}" if knowledge else ""))

    executor = CallableToolExecutor()
    executor.register("deep_investigation", _deep_investigation)
    executor.register("read_investigation_report", _read_investigation_report)

    read_definition = ToolDefinition(
        name="read_investigation_report",
        source="builtin:deep_investigation",
        description=(
            "Read the report a Deep Investigation of this user already wrote, by "
            "the run's id: its cited prose, the sources it queried and the "
            "numbered key, exactly as the user was shown. Use it to write from an "
            "investigation (a summary, a deck, a follow-up) instead of from memory "
            "or from a report re-run with new numbers. Keep its citation numbers "
            "as they are."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "investigation_id": {
                    "type": "string",
                    "description": "The investigation's id.",
                },
            },
            "required": ["investigation_id"],
        },
    )

    definition = ToolDefinition(
        name="deep_investigation",
        source="builtin:deep_investigation",
        description=(
            "Investigate a CTI subject across every source this platform can "
            "reach — the web, the knowledge bases the user can see, and any "
            "MCP tool designated for investigation — and return ONE report in "
            "which every factual statement carries a numbered citation, "
            "followed by the numbered list of sources.\n\n"
            "Use it for a subject worth researching: a threat actor, a "
            "campaign, a malware family, a CVE, a domain, an IP, a hash, or a "
            "concept the user wants grounded in sources. Do NOT use it to "
            "answer from what you already know, and do not use it to read one "
            "known URL — `web_fetch` does that.\n\n"
            "CALL IT ONCE PER TURN, with the subject as the user would name "
            "it. One run already queries every source in the selected packs "
            "and follows the leads its search turns up, so do not split the "
            "subject into facets — 'X TTPs', 'X malware', 'X sanctions' — and "
            "call it again. A second call is refused, and even if it were "
            "not, each run numbers its citations from [1] and two sets cannot "
            "be merged into one honest reply.\n\n"
            "The report's citations are the point: reproduce them as they are "
            "when you quote it. Do not renumber them, do not drop them, and "
            "do not add a claim of your own between two cited sentences "
            "without saying it is yours. If the report says some statements "
            "carry no source, keep that line."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": (
                        "The thing to investigate, as the user would name it — "
                        "'APT29', 'CVE-2024-3400', 'evil.example.com'."
                    ),
                },
                "source_packs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional pack slugs to query. Omit to use the "
                        "recommendation for the subject's type. Pass "
                        "'knowledge-bases' or 'mcp-tools' to include the "
                        "user's own corpora or designated tools."
                    ),
                },
            },
            "required": ["subject"],
        },
    )
    return [definition, read_definition], executor
