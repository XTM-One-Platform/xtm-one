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

"""The pack builder's assistant: describe a method, get a pack.

One round: the person's instruction, the draft being edited and the
conversation so far go to the caller's model with what a pack can be made of
(the builder catalog, and the corpora and MCP tools THIS caller can see), and
the model answers a short reply and a whole pack. The pack is validated by
the same model a save goes through (`PackFields`); when it is refused, the
reasons go back to the model once. Nothing is stored: the panel applies the
pack to the draft, and the person reviews it and saves it.

The draft and the instruction are the caller's own; the corpora and tools
offered are the caller's (`caller_resources`), so the model never sees, and
never proposes, a resource the caller could not use.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from app.core.utils import format_exception_message
from app.llm.base import ChatMessage
from app.llm.cut_answers import chat_retrying_a_cut
from app.services.investigation_pack_builder import (
    CallerResources,
    PackFields,
    builder_catalog,
)
from app.services.template_generation import extract_json_object, validation_problem

logger = logging.getLogger(__name__)

MAX_TOKENS = 4096
#: Turns of the conversation sent back, newest last.
MAX_HISTORY = 10
#: The longest draft the model is sent, as the JSON it reads (characters). The
#: request model refuses a longer one (422): cut, it would reach the model as
#: broken JSON it is asked to keep as it is.
MAX_DRAFT_CHARS = 20_000


def draft_text(current: dict[str, Any] | None) -> str | None:
    """The draft as the model reads it: the one serialization the bound is
    measured on (`PackAssistRequest`) and the prompt carries."""
    return json.dumps(current, ensure_ascii=False) if current else None


class PackAssistantError(Exception):
    """The model did not produce a pack this platform accepts (after a retry)."""


def _schema_lines() -> str:
    return """A pack is ONE JSON object:
{
  "name": str (<= 200), "description": str (<= 2000),
  "subject_types": [str, ...] (from SUBJECT_TYPES),
  "recommended": false, "max_iterations": int (1-50),
  "definition": {
    "steps": [ {
      "key": "lowercase-slug", "label": str (<= 120, one line), "description": str (<= 500),
      "icon": one of ICONS, "optional": bool, "default_on": bool,
      "subject_types": [str] | null (a subset of the pack's),
      "sources": [ SOURCE, ... ] (<= 12)
    } ] (1-12 steps, in the order they run),
    "exposed": [ {"step": key, "source": index, "setting": "pivot_scope"|"max_pages"|"hostname_keywords", "default": str} ]
  }
}
SOURCE, by "kind" (fields of another kind are refused):
- {"kind": "opencti"}: what the platform's OpenCTI already knows.
- {"kind": "passive_dns", "hostname_keywords": KEYWORDS|null, "max_pages": 1-25|null, "pivot_step": key|null, "pivot_scope": "shared"|"all"}: VirusTotal resolutions. pivot_step names THIS step or a LATER one whose work is the hop (the IP of a domain, the sites on an IP); that later step may have no source of its own.
- {"kind": "site_fingerprint", "fingerprint_set": FINGERPRINT_SET|null}: test the site's home page.
- {"kind": "web_search", "search_prompt": str with exactly one {value} | null}: a cited web search.
- {"kind": "knowledge_base", "knowledge_base_ids": [id]}: a corpus from KNOWLEDGE_BASES.
- {"kind": "mcp_tool", "server_id": id}: a server from MCP_TOOLS.
- {"kind": "reference", "reference": REFERENCE}: a public lookup.
- {"kind": "http", "url_template": "https://.../{value}", "value_transform": "host"|"slug"|null}: a URL, the subject placed by exactly one {value}.
Any source may narrow its step's types with "subject_types". A step with no
source must be fed by an earlier step's pivot_step. The cited report is
always added after the last step: never write a report step."""


def _item(value: object) -> str:
    """A name someone else chose (a shared corpus, an MCP server or tool), as
    one bounded item of a ``;``-separated line: no line break, no separator."""
    return " ".join(str(value).replace(";", ",").split())[:80]


def _offer(resources: CallerResources, kb_rows: Sequence[tuple[str, str, list]]) -> str:
    catalog = builder_catalog()
    lines = [
        "SUBJECT_TYPES: " + ", ".join(catalog["subject_types"]),
        "ICONS: " + ", ".join(catalog["icons"]),
        "REFERENCE: " + "; ".join(r["name"] for r in catalog["reference_sources"]),
        "KEYWORDS: " + ", ".join(k["value"] for k in catalog["hostname_keywords"]),
        "FINGERPRINT_SET: " + ", ".join(f["value"] for f in catalog["fingerprint_sets"]),
        "KNOWLEDGE_BASES (id: name): "
        + ("; ".join(f"{kb_id}: {_item(name)}" for kb_id, name, _tags in kb_rows[:40]) or "none"),
        "MCP_TOOLS (server id: server, tool): "
        + ("; ".join(f"{sid}: {_item(name)}, {_item(tool)}" for sid, name, tool, _arg in resources.mcp_tools) or "none"),
        "This person " + ("has" if resources.virustotal else "has NO") + " VirusTotal and "
        + ("can" if resources.opencti else "can NOT") + " read an OpenCTI: a source they lack is still allowed "
        "(whoever runs the pack uses their own), but say so in the reply.",
    ]
    return "\n".join(lines)


def build_messages(
    *,
    instruction: str,
    current: dict[str, Any] | None,
    history: Sequence[dict[str, str]],
    resources: CallerResources,
    kb_rows: Sequence[tuple[str, str, list]],
) -> list[ChatMessage]:
    system = (
        "You build Deep Investigation packs for threat-intelligence analysts on XTM One: a method as ordered "
        "steps, each read from sources. Change only what the person asks; keep the rest of the current pack as "
        "it is. Reply with ONLY a JSON object {\"reply\": str (one or two sentences on what you changed), "
        "\"pack\": PACK}, no fences, no prose.\n\n"
        + _schema_lines()
        + "\n\n"
        + _offer(resources, kb_rows)
    )
    messages = [ChatMessage(role="system", content=system)]
    for turn in list(history)[-MAX_HISTORY:]:
        role = turn.get("role")
        if role in ("user", "assistant") and isinstance(turn.get("content"), str):
            messages.append(ChatMessage(role=role, content=turn["content"][:4000]))
    draft = draft_text(current) or "none yet"
    messages.append(ChatMessage(role="user", content=f"Current pack: {draft}\n\nRequest: {instruction}"))
    return messages


async def assist(
    llm: Any,
    model: str | None,
    *,
    instruction: str,
    current: dict[str, Any] | None,
    history: Sequence[dict[str, str]],
    resources: CallerResources,
    kb_rows: Sequence[tuple[str, str, list]],
) -> tuple[str, dict[str, Any]]:
    """``(reply, pack)``: a pack `PackFields` accepts, or `PackAssistantError`."""
    messages = build_messages(
        instruction=instruction, current=current, history=history, resources=resources, kb_rows=kb_rows
    )
    problem = "no answer"
    for attempt in range(2):

        async def _generate(max_tokens: int, _messages=messages):
            return await llm.chat(_messages, model=model, temperature=0.3, max_tokens=max_tokens)

        response, cut = await chat_retrying_a_cut(llm, model, MAX_TOKENS, _generate, label="Pack assistant")
        content = response.content or ""
        if cut:
            raise PackAssistantError("the answer was cut at the output limit")
        try:
            raw = extract_json_object(content)
            pack = PackFields.model_validate(raw.get("pack"))
        except (ValueError, json.JSONDecodeError, TypeError) as exc:
            problem = validation_problem(exc) if isinstance(exc, ValidationError) else format_exception_message(exc)
            logger.info("pack assistant answer refused (attempt %s): %s", attempt + 1, problem[:500])
            messages = [
                *messages,
                ChatMessage(role="assistant", content=content[:MAX_DRAFT_CHARS]),
                ChatMessage(
                    role="user",
                    content=(
                        f"The platform refused that pack: {problem[:2000]}. Reply again with ONLY the corrected "
                        "JSON object. Its reply is still for the person: what the pack does, not this correction."
                    ),
                ),
            ]
            continue
        reply = raw.get("reply")
        return (
            " ".join(str(reply).split())[:600] if isinstance(reply, str) and reply.strip() else "Here is the pack.",
            pack.model_dump(mode="json", exclude_none=True),
        )
    raise PackAssistantError(problem)
