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

"""Create a fleet from a prompt: generate a spec, preview it.

The model writes a fleet spec (``xtm-one/fleet/v1``, :mod:`app.services.fleet_spec`)
from what the person asked; the spec is checked by ``validate_fleet_spec`` and
previewed by ``apply_fleet_spec`` as a dry run, so the preview is the exact plan
the apply would carry out. Applying stays ``apply_fleet_spec`` (the tool) and
``POST /api/v1/fleets/apply`` (REST): this module never writes, there is one
write path.

* **Context, bounded.** The model sees the agents the caller may see (slug,
  name, a line of description, whether they may manage it and its home fleet),
  their fleets (slug, name) and their workspaces (name), at most
  :data:`CONTEXT_MAX_AGENTS` / :data:`CONTEXT_MAX_FLEETS` /
  :data:`CONTEXT_MAX_WORKSPACES`. An existing agent is named by reference; a new
  member carries a minimal agent bundle (name, description, persona) and nothing
  else: no tool, model, integration or secret is ever invented.
* **Keys are slugs.** Member keys the model writes are slugified, and every
  parent, handover and the orchestrator follow the rename.
* **Cut answers.** An answer cut at its output budget is asked once more with
  more room (``chat_retrying_a_cut``); still cut, it is refused, never parsed.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import slugify
from app.llm.base import ChatMessage
from app.llm.cut_answers import chat_retrying_a_cut
from app.models.agent import Agent
from app.models.fleet import Fleet
from app.models.user import User
from app.models.workspace import Workspace
from app.services.fleet_spec import (
    FLEET_SPEC_API_VERSION,
    FLEET_SPEC_KIND,
    SPEC_HANDOVER_TYPES,
    FleetSpecError,
    FleetSpecPlan,
    apply_fleet_spec,
    parse_fleet_spec_text,
    validate_fleet_spec,
)

#: Output budget of one generated spec; a cut answer is retried once with more.
GENERATE_MAX_TOKENS = 8192
#: The longest prompt accepted.
PROMPT_MAX_CHARS = 8000
CONTEXT_MAX_AGENTS = 80
CONTEXT_MAX_FLEETS = 40
CONTEXT_MAX_WORKSPACES = 40
_DESCRIPTION_CHARS = 160
#: The good default added to the notes of every generated spec (#11972): the spec format carries no knowledge base.
WORKING_KNOWLEDGE_NOTE = (
    "suggestion: once applied, give the fleet one shared working knowledge base, written by the orchestrator "
    "and read by every member (get_fleet_setup proposes it)"
)
#: The agent fields a generated new member may carry; anything else is dropped.
_NEW_AGENT_FIELDS = ("name", "description", "persona")


@dataclass
class GeneratedFleetSpec:
    """A generated spec, its dry-run plan, and what was adjusted on the way."""

    spec: dict[str, Any]
    plan: FleetSpecPlan
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"spec": self.spec, "plan": self.plan.as_dict(), "notes": self.notes}


# ── Context ─────────────────────────────────────────────────────────────────


async def generation_context(db: AsyncSession, user: User, user_group_ids: list[str]) -> dict[str, Any]:
    """What the model may build the fleet from, as the caller sees it, bounded."""
    from app.api.v1.helpers import can_manage, user_visible_filter
    from app.services.fleets import home_fleets_of

    agents = (
        (
            await db.execute(
                select(Agent)
                .where(user_visible_filter(Agent, user.id, user_group_ids, is_admin=user.is_admin))
                .order_by(Agent.name)
                .limit(CONTEXT_MAX_AGENTS)
            )
        )
        .scalars()
        .all()
    )
    homes = await home_fleets_of(db, {a.id for a in agents})
    fleets = (
        (
            await db.execute(
                select(Fleet)
                .where(
                    user_visible_filter(Fleet, user.id, user_group_ids, is_admin=user.is_admin),
                    Fleet.is_archived.is_(False),
                )
                .order_by(Fleet.name)
                .limit(CONTEXT_MAX_FLEETS)
            )
        )
        .scalars()
        .all()
    )
    workspaces = (
        (
            await db.execute(
                select(Workspace.name)
                .where(Workspace.user_id == user.id, Workspace.is_archived.is_(False))
                .order_by(Workspace.name)
                .limit(CONTEXT_MAX_WORKSPACES)
            )
        )
        .scalars()
        .all()
    )
    return {
        "agents": [
            {
                "slug": a.slug,
                "name": a.name,
                "description": (a.description or "")[:_DESCRIPTION_CHARS],
                "you_manage": bool(can_manage(user, a, user_group_ids)) and not a.is_company_managed,
                "home_fleet": getattr(homes.get(a.id), "fleet_name", None),
            }
            for a in agents
            if a.slug
        ],
        "fleets": [{"slug": f.slug, "name": f.name} for f in fleets],
        "workspaces": list(workspaces),
    }


# ── Prompt ──────────────────────────────────────────────────────────────────

_SYSTEM = f"""You design agent fleets for an AI agent platform. A fleet groups agents around an
orchestrator (the entry agent that routes work) and works in one workspace.

Answer with ONE JSON object and nothing else, shaped like this example:
{{
  "apiVersion": "{FLEET_SPEC_API_VERSION}",
  "kind": "{FLEET_SPEC_KIND}",
  "metadata": {{"name": "Alert Desk", "slug": "alert-desk", "description": "Triages alerts.", "workspace": null}},
  "spec": {{
    "orchestrator": "desk-lead",
    "members": [
      {{"key": "desk-lead", "role_label": "Lead", "parent": null, "exposed": false,
        "handovers": [{{"to": "threat-analyst", "type": "delegate"}}],
        "agent": {{"name": "Desk Lead", "description": "Routes alerts.", "persona": "You route alerts."}}}},
      {{"key": "threat-analyst", "role_label": "Analyst", "parent": "desk-lead", "exposed": false,
        "handovers": [{{"to": "desk-lead", "type": "consult"}}], "agent": {{"ref": {{"slug": "threat-analyst"}}}}}}
    ],
    "connected": [{{"agent": {{"slug": "reporting"}}, "collaboration_instruction": "Ask for reports.",
      "context_inclusion": "none"}}]
  }}
}}
metadata.workspace is the name of an existing workspace, or null to create one named after the fleet.
parent, every handover's "to" and the orchestrator name member keys. A member's agent is a reference
to an existing agent's slug, or a new agent's name, description and persona.

Rules:
- Reuse an existing agent only when it fits, the person asks for it, you_manage is true and home_fleet
  is null; then its key is its slug and agent is {{"ref": {{"slug": ...}}}}.
- Otherwise create the member: a new short slug as key and a persona of a few sentences in the second
  person. Never invent tools, models, integrations or credentials.
- An existing agent that you_manage is false, or that already has a home fleet, may only be connected.
- The orchestrator is a member and has no parent. Handover types: {", ".join(SPEC_HANDOVER_TYPES)}.
- context_inclusion is none unless the person asks otherwise.
- A good default (a suggestion, drop it when the person asks otherwise): every member other than the
  orchestrator holds a consult handover to the orchestrator, so it can ask a route, scope or context
  question, and has the orchestrator as its parent.
- The spec carries no knowledge base: knowledge bases are created and bound after the apply. A good
  default the setup proposes then: one shared working knowledge base, written by the orchestrator and
  read by every member.
- Use only the slugs, fleets and workspace names listed in the context."""


def build_messages(prompt: str, context: dict[str, Any], current: dict[str, Any] | None = None) -> list[ChatMessage]:
    """The chat messages that ask for a spec; *current* is a spec to refine."""
    user = f"What the person asked:\n{prompt.strip()}\n\nWhat they can use:\n{json.dumps(context, indent=1)}"
    if current is not None:
        user += f"\n\nRefine this spec rather than start over:\n{json.dumps(current, indent=1)}"
    return [ChatMessage(role="system", content=_SYSTEM), ChatMessage(role="user", content=user)]


# ── Parsing and normalisation ───────────────────────────────────────────────

_FENCE = re.compile(r"```(?:json|yaml)?\s*\n(.*?)\n```", re.S)


def extract_spec(text: str) -> dict[str, Any]:
    """The spec object in a model answer: fenced, bare, or wrapped in prose."""
    raw = (text or "").strip()
    fenced = _FENCE.search(raw)
    if fenced:
        raw = fenced.group(1).strip()
    elif not raw.startswith("{") and "{" in raw and "}" in raw:
        raw = raw[raw.index("{") : raw.rindex("}") + 1]
    if not raw:
        raise FleetSpecError("The model answered no spec.")
    try:
        data = json.loads(raw)
    except ValueError:
        data = parse_fleet_spec_text(raw)
    if not isinstance(data, dict):
        raise FleetSpecError("The model answered no spec object.")
    return data


def normalise_generated_spec(spec: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The generated spec made applicable: envelope, slug keys, minimal bundles.

    Answers the spec and one note per adjustment. The input is not mutated.
    """
    notes: list[str] = []
    out = json.loads(json.dumps(spec, default=str))
    out["apiVersion"] = FLEET_SPEC_API_VERSION
    out["kind"] = FLEET_SPEC_KIND
    metadata = out.get("metadata") if isinstance(out.get("metadata"), dict) else {}
    out["metadata"] = metadata
    name = str(metadata.get("name") or "").strip()
    if name and not str(metadata.get("slug") or "").strip():
        metadata["slug"] = slugify(name)
    body = out.get("spec") if isinstance(out.get("spec"), dict) else {}
    out["spec"] = body
    members = [m for m in body.get("members") or [] if isinstance(m, dict)]

    renamed: dict[str, str] = {}
    for member in members:
        key = str(member.get("key") or "").strip()
        new_key = slugify(key) if key else ""
        if key and new_key and new_key != key:
            renamed[key] = new_key
            notes.append(f"member key '{key}' written as '{new_key}'")
        member["key"] = new_key or key

    def _key(value: object) -> object:
        return renamed.get(str(value), value) if value is not None else None

    for member in members:
        member["parent"] = _key(member.get("parent"))
        member["handovers"] = [
            {**h, "to": _key(h.get("to"))} for h in member.get("handovers") or [] if isinstance(h, dict)
        ]
        member["agent"] = _member_agent(member, notes)
    if body.get("orchestrator") is not None:
        body["orchestrator"] = _key(body["orchestrator"])
    body["members"] = members
    body["connected"] = [c for c in body.get("connected") or [] if isinstance(c, dict)]
    return out, notes


def _member_agent(member: dict[str, Any], notes: list[str]) -> dict[str, Any]:
    """A reference stays one; a new agent is reduced to a minimal bundle."""
    agent = member.get("agent") if isinstance(member.get("agent"), dict) else {}
    if isinstance(agent.get("ref"), dict):
        return {"ref": {k: v for k, v in agent["ref"].items() if k in ("slug", "id")}}
    inner = agent.get("agent") if isinstance(agent.get("agent"), dict) else agent
    kept = {k: str(inner[k]) for k in _NEW_AGENT_FIELDS if inner.get(k)}
    dropped = sorted(set(inner) - set(_NEW_AGENT_FIELDS) - {"version", "export_type"})
    if dropped:
        notes.append(f"member '{member['key']}': {', '.join(dropped)} left out (set them on the agent page)")
    if not kept.get("name"):
        kept["name"] = str(member.get("role_label") or member["key"]).strip() or member["key"]
    return {"version": 3, "export_type": "agent", "agent": kept}


# ── Generation ──────────────────────────────────────────────────────────────


async def generate_fleet_spec(
    db: AsyncSession,
    user: User,
    user_group_ids: list[str],
    prompt: str,
    *,
    current: dict[str, Any] | None = None,
    workspace_id: uuid.UUID | None = None,
    llm: object | None = None,
    model: str | None = None,
) -> GeneratedFleetSpec:
    """A spec for *prompt*, checked and previewed as a dry run; nothing is written.

    Raises :class:`FleetSpecError` when the prompt is empty or too long, or the
    model's answer is not a valid spec, and lets a refusal of the dry run
    (:class:`app.services.fleets.FleetActionRefusedError`) through.
    """
    text = (prompt or "").strip()
    if not text:
        raise FleetSpecError("Describe the fleet to create.")
    if len(text) > PROMPT_MAX_CHARS:
        raise FleetSpecError(f"The prompt is longer than {PROMPT_MAX_CHARS} characters.")
    if llm is None:
        from app.llm.router import get_llm_for_user

        llm, model = await get_llm_for_user(db, str(user.id))

    messages = build_messages(text, await generation_context(db, user, user_group_ids), current)

    async def _call(max_tokens: int):
        return await llm.chat(messages, model=model, temperature=0.2, max_tokens=max_tokens)  # type: ignore[attr-defined]

    response, cut = await chat_retrying_a_cut(llm, model, GENERATE_MAX_TOKENS, _call, label="Fleet spec generator")
    if cut:
        raise FleetSpecError("The generated spec was too long and was cut. Ask for a smaller fleet.")
    spec, notes = normalise_generated_spec(extract_spec(getattr(response, "content", "") or ""))
    notes.append(WORKING_KNOWLEDGE_NOTE)
    validate_fleet_spec(spec)
    plan = await apply_fleet_spec(db, user, spec, user_group_ids, dry_run=True, workspace_id=workspace_id)
    return GeneratedFleetSpec(spec=spec, plan=plan, notes=notes)


__all__ = [
    "GENERATE_MAX_TOKENS",
    "PROMPT_MAX_CHARS",
    "GeneratedFleetSpec",
    "build_messages",
    "extract_spec",
    "generate_fleet_spec",
    "generation_context",
    "normalise_generated_spec",
]
