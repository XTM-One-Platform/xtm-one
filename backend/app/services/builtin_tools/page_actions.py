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

"""``act_on_page``: act on the host page of an embedded chat.

The embedded widget (Ask Ariane in OpenCTI) runs in the host's own page, so it
can read that page and click in it. This tool drives it one action at a time:
it asks the widget for a snapshot of the page, asks the platform's decision
model (a CLM, ``app.llm.decisions``) which element brings the page closer to
the agent's goal, sends that action, and repeats on the fresh snapshot.

The decision model only ever picks among options this module builds from the
snapshot - a click on an element it lists, or typing the agent's own text into
a field it lists - so text written on the page can at most bias which real
element is chosen. The agent receives the outcome and the steps, never the
page; the element names it does see are quarantined.

Offered only on a turn whose widget declared it answers page actions
(``supports_page_actions``) while an administrator has set a decision model.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

ACT_ON_PAGE_TOOL = "act_on_page"
PAGE_ACTION_REQUIRED_EVENT = "page_action_required"

MAX_ACTIONS = 8
PAGE_WAIT_SECONDS = 20
# Checked before each step and kept under the loop's per-tool timeout
# (``TOOL_EXECUTION_TIMEOUT_SECONDS``, 300 s): a handler that waits on the page
# does not move the loop's deadline the way an approval pause does, so the run
# has to end on its own terms - with an outcome - before the loop kills it.
RUN_DEADLINE_SECONDS = 240
# How long a run holds its conversation's page (`claim_page_action_run`): the
# loop's per-tool timeout, past which no run of it is still alive.
RUN_CLAIM_SECONDS = 300
# Actions weighed per step. A snapshot is the client's and may list a thousand
# elements, each group question a call: beyond this many, the rest of the page
# is left out and the outcome says the page was cut.
MAX_CONSIDERED_OPTIONS = 200

# What the decision model reads of the page, at most.
MAX_STATE_PAGE_CHARS = 12_000
MAX_TEXT_CHARS = 1_000
# Characters per token a question is sized with when the decision model's
# context is known. OpenCTI snapshots measured 2.25-2.45 on two decision
# models; the lower figure leaves the margin, since a question that does not
# fit is refused, not cut.
CHARS_PER_TOKEN = 2
# Options per choice question. The decision APIs allow 255, but a served model
# allows fewer without saying so - Ollama refuses more than 26, its tev1 runner
# more than 24 - so a page is asked in groups well under both.
MAX_OPTIONS = 16

# A `done` the decision model is less sure of than this ends as `unsure`, not
# `finished`: it answered `done` at 0.46 on a page that only resembled the goal
# (a dashboard's "Latest reports" widget for "the reports list"), and at 0.31
# on the very dialog the goal named. No threshold separates the two, so the
# agent is told the goal may be reached, and the user checks.
DONE_MIN_CONFIDENCE = 0.5
# A `moves` verdict the decision model is less sure of than this stops the run
# before the action, as `commits` does: the verdict is all that stands
# between a run without `submit` and a click that saves or deletes, and the
# same model answers `done` well under this bar on pages it misread.
MOVES_MIN_CONFIDENCE = 0.5

DONE = "done"
NOT_YET = "not_yet"
IMPOSSIBLE = "impossible"
NONE = "none"
# What `_decide` answers when the run's deadline passes between its questions.
OUT_OF_TIME = "out_of_time"
COMMITS = "commits"
MOVES = "moves"
_TEXT_ROLES = frozenset({"textbox", "searchbox", "combobox"})

_CLOSING = {IMPOSSIBLE: "no action helps"}
_NONE_OPTION = {NONE: "none of these actions helps"}
# Asked of each click the agent did not clear with `submit`: a button that
# creates or deletes is often a plain button outside any form, so only the page
# around it tells (both decision models judged "Créer un agent" committing with
# the creation form in the state, and not without it).
_COMMIT_CHOICES = {
    COMMITS: "it saves, creates, sends, submits or deletes something",
    MOVES: "it only navigates, opens, selects, expands or moves to another step",
}
_COMMIT_INSTRUCTIONS = (
    "Would this action save, create, send, submit or delete something, or does "
    "it only move through the page?"
)
_INSTRUCTIONS = (
    "Which action brings the page closer to the goal? Choose impossible if no "
    "action helps."
)
# Asked before each step, on its own: offered as one more option beside the
# page's actions, `done` lost to the next step button page after page ("click
# Step 4" went on to Steps 5 and 6), where asked alone it was answered once the
# steps taken covered the goal.
_REACHED_CHOICES = {
    DONE: "the goal is reached: nothing it asks for is left to do",
    NOT_YET: "something the goal asks for is still left to do",
}
_REACHED_INSTRUCTIONS = "Is the goal reached, given what was done so far and the page as it is now?"
_GROUP_INSTRUCTIONS = (
    "Which action brings the page closer to the goal? Choose none if no "
    "action in this list helps."
)

_LINE = re.compile(r'^\[(e\d+)\]\s+((\S+)\s+"((?:[^"\\]|\\.)*)"(?:\s+\(([^)]*)\))?.*)$')
_HEADER = re.compile(r"^# (.*) — (\S*)$")

# The state of one question, given its choices and instructions.
_StateOf = Callable[[dict[str, str], str], tuple[str, bool]]

# Indirection so tests can move the clock.
_now = time.monotonic


@dataclass(frozen=True)
class Snapshot:
    text: str
    url: str = ""
    title: str = ""
    # ref -> (role, name), in page order.
    elements: dict[str, tuple[str, str]] = field(default_factory=dict)
    # ref -> the rest of its line (role, name, states, value, destination).
    lines: dict[str, str] = field(default_factory=dict)
    # ref -> its states ("disabled", "menu", ...), as the snapshot lists them.
    states: dict[str, frozenset[str]] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


def parse_snapshot(text: str, *, url: str = "", title: str = "") -> Snapshot:
    """Read the widget's snapshot text (one ``[eN] role "name"`` line per element)."""
    elements: dict[str, tuple[str, str]] = {}
    lines: dict[str, str] = {}
    states: dict[str, frozenset[str]] = {}
    for line in text.splitlines():
        if (m := _LINE.match(line)) is not None and m.group(1) not in elements:
            elements[m.group(1)] = (m.group(3), m.group(4))
            lines[m.group(1)] = m.group(2)
            states[m.group(1)] = frozenset(s.strip() for s in (m.group(5) or "").split(",") if s.strip())
        elif not (url or title) and (h := _HEADER.match(line)) is not None:
            title, url = h.group(1), h.group(2)
    return Snapshot(text=text, url=url, title=title, elements=elements, lines=lines, states=states)


def build_options(snapshot: Snapshot, *, text: str | None) -> dict[str, str]:
    """The actions the decision model may pick, keyed ``click:eN`` / ``open:eN`` / ``type:eN``."""
    options: dict[str, str] = {}
    for ref, (role, _name) in snapshot.elements.items():
        # The whole line, not just the name: a state ("disabled") or where a
        # link leads ("-> /dashboard/analyses") is what tells a section link
        # apart from the page the goal names.
        line = snapshot.lines.get(ref, "")
        options[f"click:{ref}"] = f"click {line}"
        # A closed menu (OpenCTI's sections open on hover) lists its items only
        # once open: opening it is the step that brings them into the snapshot.
        if "menu" in snapshot.states.get(ref, ()):
            options[f"open:{ref}"] = f"open the menu of {line}"
        if text and role in _TEXT_ROLES:
            options[f"type:{ref}"] = f"type the text into {line} and press Enter"
    return options


def validate_inputs(text: str | None, path: str | None) -> str | None:
    """Why the agent's arguments are refused, or ``None`` (FR-013)."""
    if text is not None and len(text) > MAX_TEXT_CHARS:
        return f"text is longer than {MAX_TEXT_CHARS} characters"
    if path is not None and not (
        path.startswith("/")
        and not path.startswith("//")
        and ":" not in path.split("/", 2)[1]
        # A browser reads `/\host` like `//host`, and a control character can
        # hide either from a reader of the path.
        and "\\" not in path
        and not any(ord(c) < 32 or ord(c) == 127 for c in path)
    ):
        return "path must be a path within the host application, starting with /"
    return None


def state_budget(context_tokens: int | None, choices: dict[str, str], instructions: str) -> int:
    """Characters the state of one question may take so the whole question fits *context_tokens*."""
    if not context_tokens:
        return MAX_STATE_PAGE_CHARS
    asked = len(instructions) + sum(len(k) + len(v) for k, v in choices.items())
    return max(0, context_tokens * CHARS_PER_TOKEN - asked)


def question_state(
    goal: str,
    text: str | None,
    snapshot: Snapshot,
    context_tokens: int | None,
    choices: dict[str, str],
    instructions: str,
    *,
    done: tuple[str, ...] = (),
    action: str | None = None,
) -> tuple[str, bool]:
    """The state of one question: as much of the page as fits beside its own options."""
    budget = state_budget(context_tokens, choices, instructions)
    return _state(goal, text, snapshot, budget, done=done, action=action)


def _state(
    goal: str,
    text: str | None,
    snapshot: Snapshot,
    budget: int,
    *,
    done: tuple[str, ...] = (),
    action: str | None = None,
) -> tuple[str, bool]:
    parts = [f"Goal: {goal}"]
    if text:
        parts.append(f"Text to type: {text}")
    # What this run already did: without it, "type the name, then click Next"
    # reads the same on the next page, and the run clicks Next page after page.
    if done:
        parts.append("Done so far: " + "; ".join(done))
    if action:
        parts.append(f"Action: {action}")
    parts.append("Page:")
    room = min(MAX_STATE_PAGE_CHARS, budget - sum(len(part) + 1 for part in parts))
    page = snapshot.text
    truncated = len(page) > room
    if truncated:
        # Whole lines only: a cut name reads as another element.
        page = page[: max(0, room)].rpartition("\n")[0]
    parts[-1] += "\n" + page
    return "\n".join(parts), truncated


def build_result(
    outcome: str,
    steps: list[dict],
    snapshot: Snapshot | None,
    *,
    truncated: bool,
    error: str | None = None,
    withheld: str | None = None,
) -> ToolResult:
    """The agent's view of a run: platform facts outside, page words quarantined.

    Element names, the url and the title are written by the page, which anyone
    who edited the content shown may have authored; they go inside one
    ``wrap_untrusted`` block. Only what the platform computed stays outside.
    """
    from app.services.content_safety import wrap_untrusted

    head: dict = {"outcome": outcome, "steps": len(steps), "truncated": truncated}
    if error:
        head["error"] = error
    page = {
        "url": snapshot.url if snapshot else "",
        "title": snapshot.title if snapshot else "",
        "steps": steps,
    }
    if withheld:
        page["withheld"] = withheld
    content = json.dumps(head) + "\n" + wrap_untrusted(json.dumps(page), source="host page")
    return ToolResult(tool_call_id="", content=content, is_error=outcome == "failed")


def build_page_action_tools(
    db: AsyncSession,
    *,
    user_id: str | None,
    conversation_id: str,
    model: str,
    provider_id: uuid.UUID | None,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """``act_on_page`` for one embedded-chat turn."""
    from app.services.chat_turn_guard import (
        await_page_action_result,
        claim_page_action_run,
        clear_pending_page_action,
        release_page_action_run,
        set_pending_page_action,
    )
    from app.services.flow_classifier import ClassifierError, classify, decision_context_window
    from app.tools.tool_events import emit_tool_event

    async def _send(action: dict) -> dict | None:
        action_id = uuid.uuid4().hex
        await set_pending_page_action(conversation_id, action_id)
        await emit_tool_event(
            PAGE_ACTION_REQUIRED_EVENT,
            conversation_id=conversation_id,
            action_id=action_id,
            action=action,
        )
        try:
            return await await_page_action_result(
                conversation_id, action_id, timeout=PAGE_WAIT_SECONDS
            )
        finally:
            # Shielded: a cancelled turn must still drop the wait, or the next
            # answer would be accepted for an action nobody is waiting on.
            await asyncio.shield(clear_pending_page_action(conversation_id))

    async def _choose(state_of: _StateOf, choices: dict[str, str], instructions: str) -> tuple[str, float, bool]:
        state, cut = state_of(choices, instructions)
        decision = await classify(
            db,
            provider_id=provider_id,
            model=model,
            state=state,
            instructions=instructions,
            choices=choices,
            user_id=user_id,
            trace_name="page_decision",
        )
        return decision.choice, decision.confidence, cut

    async def _decide(state_of: _StateOf, options: dict[str, str], deadline: float) -> tuple[str, float, bool]:
        keys = list(options)
        cut_any = False
        # One question per group, then again over the groups' picks, until
        # they fit one question beside the closing options. Sequential: each
        # call resolves its provider on the turn's session, which is not safe
        # to share between concurrent tasks.
        while len(keys) > MAX_OPTIONS - len(_CLOSING):
            winners: list[str] = []
            size = MAX_OPTIONS - len(_NONE_OPTION)
            for start in range(0, len(keys), size):
                if _now() >= deadline:
                    return OUT_OF_TIME, 0.0, cut_any
                group = {k: options[k] for k in keys[start : start + size]}
                pick, _, cut = await _choose(state_of, {**group, **_NONE_OPTION}, _GROUP_INSTRUCTIONS)
                cut_any = cut_any or cut
                if pick != NONE:
                    winners.append(pick)
            keys = winners
        if not keys:
            # No action left to weigh (every group answered none, or the page
            # lists none): a question needs two candidates, and the answer is
            # already known.
            return IMPOSSIBLE, 1.0, cut_any
        pick, confidence, cut = await _choose(state_of, {**{k: options[k] for k in keys}, **_CLOSING}, _INSTRUCTIONS)
        return pick, confidence, cut_any or cut

    async def _act_on_page(
        goal: str, text: str | None = None, path: str | None = None, submit: bool = False
    ) -> ToolResult:
        steps: list[dict] = []
        if (refusal := validate_inputs(text, path)) is not None:
            return build_result("failed", steps, None, truncated=False, error=refusal)
        # One run per page: a second one would replace this one's pending
        # action and drive the page at the same time.
        claim = uuid.uuid4().hex
        if not await claim_page_action_run(conversation_id, claim, ttl_seconds=RUN_CLAIM_SECONDS):
            return build_result(
                "failed", steps, None, truncated=False,
                error="another page action is already running on this page; call again once it has ended",
            )
        try:
            return await _run(goal, text, path, submit)
        finally:
            await asyncio.shield(release_page_action_run(conversation_id, claim))

    async def _run(goal: str, text: str | None, path: str | None, submit: bool) -> ToolResult:
        steps: list[dict] = []
        context_tokens = await decision_context_window(
            db, provider_id=provider_id, model=model, user_id=user_id
        )
        deadline = _now() + RUN_DEADLINE_SECONDS
        truncated = False
        typed: set[str] = set()
        # (action, snapshot digest) -> whether the page reported the action done.
        tried: dict[tuple[str, str], bool] = {}

        first = {"type": "navigate", "path": path} if path else {"type": "snapshot"}
        answer = await _send(first)
        if answer is None:
            return build_result("failed", steps, None, truncated=False, error="the page did not answer")
        snapshot = parse_snapshot(answer.get("snapshot", ""), url=answer.get("url", ""), title=answer.get("title", ""))
        if path:
            # The router accepting the path is not arriving there: an unknown
            # route that redirects leaves the page on another URL.
            arrived = snapshot.url.split("?")[0].startswith(path.split("?")[0])
            steps.append({"action": "navigate", "target": path, "ok": bool(answer.get("ok")) and arrived})

        while True:
            if len([s for s in steps if s["action"] != "navigate"]) >= MAX_ACTIONS or _now() >= deadline:
                return build_result("budget_reached", steps, snapshot, truncated=truncated)
            pending_text = text if text and not typed else None
            options = build_options(snapshot, text=pending_text)
            if len(options) > MAX_CONSIDERED_OPTIONS:
                options = dict(list(options.items())[:MAX_CONSIDERED_OPTIONS])
                truncated = True
            # Actions on the page only: a navigation is the agent's guess of a
            # path, which a host may render as another page under that URL
            # (OpenCTI shows its dashboard), and the page header already says
            # where the run is - listed, it read as the goal reached.
            done = tuple(f"{s['action']} {s['target']}" for s in steps if s["action"] != "navigate")
            state_of = partial(question_state, goal, pending_text, snapshot, context_tokens, done=done)
            try:
                reached, confidence, cut = await _choose(state_of, _REACHED_CHOICES, _REACHED_INSTRUCTIONS)
                truncated = truncated or cut
                if reached == DONE:
                    outcome = "finished" if confidence >= DONE_MIN_CONFIDENCE else "unsure"
                    return build_result(outcome, steps, snapshot, truncated=truncated)
                pick, _, cut = await _decide(state_of, options, deadline)
                truncated = truncated or cut
            except ClassifierError as exc:
                return build_result(
                    "failed", steps, snapshot, truncated=truncated,
                    error=f"decision model {model} failed: {exc}",
                )
            if pick == OUT_OF_TIME:
                return build_result("budget_reached", steps, snapshot, truncated=truncated)
            if pick == IMPOSSIBLE:
                return build_result("cannot_reach", steps, snapshot, truncated=truncated)
            if pick not in options:
                return build_result(
                    "failed", steps, snapshot, truncated=truncated,
                    error=f"decision model {model} answered an action it was not offered",
                )
            if (pick, snapshot.digest) in tried:
                # The same action on a page it did not change: repeating it
                # would spend the budget on a page that does not respond. When
                # the page did take it, its effect is only invisible to the
                # snapshot (a state shown by styling alone), so the outcome
                # says so rather than that the goal is out of reach.
                outcome = "unverified" if tried[(pick, snapshot.digest)] else "cannot_reach"
                return build_result(outcome, steps, snapshot, truncated=truncated)
            verb, ref = pick.split(":", 1)
            role, name = snapshot.elements.get(ref, ("", ""))
            if not submit:
                # A click that saves, creates, sends or deletes is the user's
                # to ask for: the run stops before it, and the agent hears
                # which button that was. Every verb can be one: `open` clicks
                # a menu that does not open on hover, and `type` presses Enter
                # in a form of that one field.
                try:
                    verdict, verdict_confidence, cut = await _choose(
                        partial(
                            question_state, goal, pending_text, snapshot, context_tokens,
                            done=done, action=options[pick],
                        ),
                        _COMMIT_CHOICES,
                        _COMMIT_INSTRUCTIONS,
                    )
                except ClassifierError as exc:
                    return build_result(
                        "failed", steps, snapshot, truncated=truncated,
                        error=f"decision model {model} failed: {exc}",
                    )
                truncated = truncated or cut
                if verdict != MOVES or verdict_confidence < MOVES_MIN_CONFIDENCE:
                    return build_result(
                        "stopped_before_submit", steps, snapshot, truncated=truncated, withheld=f'{role} "{name}"'
                    )
            action = {"type": verb, "ref": ref}
            if verb == "type":
                action["text"] = text
                typed.add(ref)
            answer = await _send(action)
            if answer is None:
                steps.append({"action": verb, "target": f'{role} "{name}"', "ok": False})
                return build_result("failed", steps, snapshot, truncated=truncated, error="the page did not answer")
            steps.append({"action": verb, "target": f'{role} "{name}"', "ok": bool(answer.get("ok"))})
            tried[(pick, snapshot.digest)] = bool(answer.get("ok"))
            snapshot = parse_snapshot(answer.get("snapshot", ""), url=answer.get("url", ""), title=answer.get("title", ""))

    executor = CallableToolExecutor()
    executor.register(ACT_ON_PAGE_TOOL, _act_on_page)
    definition = ToolDefinition(
        name=ACT_ON_PAGE_TOOL,
        description=(
            "Act on the page the user is looking at in the host application: "
            "navigate, open menus, click and type until the goal is reached, step by step. "
            "Returns the outcome and the steps taken, not the page's content. "
            "Outcome `unverified` means the last action ran but the page shows "
            "no visible change; `unsure` means the goal may be reached, so the "
            "user should check the page; `stopped_before_submit` means the next "
            "action (`withheld`) would save, create, send or delete, and was not made."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": (
                        "One destination or action on the page, in one sentence, "
                        "naming what to click by the labels the page shows, "
                        "through the menus that lead to it when you know them "
                        "(e.g. 'Open Analyses > Reports' rather than 'open the "
                        "reports list'): the step chooser reads only the page. "
                        "For several destinations, call the tool once per "
                        "destination."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "A value to type into a field of the page (a search term, a name), when the goal needs one.",
                },
                "path": {
                    "type": "string",
                    "description": "A path within the host application to open first, when it is known.",
                },
                "submit": {
                    "type": "boolean",
                    "description": (
                        "True only when the user asked to save, create, send or delete "
                        "what the page holds. Otherwise the run stops before any action that does."
                    ),
                },
            },
            "required": ["goal"],
        },
        source="builtin",
        # The agent sets `submit`, and with it the run skips the check that
        # stops before a committing click: a person approves every such call,
        # whatever clears this tool otherwise.
        always_ask_argument="submit",
    )
    return [definition], executor
