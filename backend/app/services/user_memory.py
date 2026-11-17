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

"""User memory service – Redis-backed cross-agent personalization.

Every authenticated user (and every anonymous visitor on the public widget)
gets a personal memory. Its PROFILE - who the user is and how they like to
work - is shared with **all** agents; the NOTES an agent's work leaves about
the user are read by that agent only. Scopes, freshness and the read
rule are ``user_memory_entries``; the sweep that migrates free-text profiles
and drops expired notes is ``user_memory_maintenance``.

The architecture mirrors ``agent_memory`` but is keyed by user/visitor ID
instead of agent ID:

**Short-term**: Rolling list of recent interactions across all agents (24 h),
each recording the agent it was with (``user_mem:recent:``). An agent's
context shows its own only.
**Medium-term**: The entries consolidated by the LLM (30 d), one JSON list,
written only through :func:`update_entries`. ``user_mem:medium:`` keeps the
rendered PROFILE alone - never a note - for the readers of the free-text
profile this replaced.

For **authenticated users** the key is the user's UUID.
For **public widget visitors** the key is the ``visitor_id`` persisted in the
browser via localStorage / cookie (already tracked by the widget).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.core.redaction import redact_known_secrets
from app.core.redis import redis_client
from app.core.utils import format_exception_message
from app.llm.cut_answers import chat_retrying_a_cut
from app.services.agent_memory import _truncate_at_boundary, entries_starting_with
from app.services.langfuse_tracing import traced_generation
from app.services.memory_tombstones import account_tombstone, bury, undo_if_buried
from app.services.user_memory_entries import (
    OWN_NOTES_HEADER,
    PROFILE_HEADER,
    MemoryEntry,
    agent_scope,
    bullet_lines,
    entries_from_raw,
    keep_history,
    new_entry,
    render_line,
    render_profile,
    split_visible,
    visible_to,
)
from app.services.user_memory_entries import restated as stated_again

if TYPE_CHECKING:
    from app.llm.base import BaseLLMProvider

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────
SHORT_TERM_MAX_ENTRIES = 15  # slightly larger than agent memory — cross-agent
SHORT_TERM_TTL = 86_400  # 24 hours
MEDIUM_TERM_TTL = 2_592_000  # 30 days
CONSOLIDATION_THRESHOLD = 6
# Output budget of one consolidation. The answer replaces the only copy of
# the entries, so an answer cut at it is never stored: it is retried once with
# twice the budget, capped at the model's output ceiling, and a rewrite still
# cut keeps the existing entries. The prompt asks for a 300-word
# profile plus a few notes per agent of the window, in JSON.
_CONSOLIDATION_MAX_TOKENS = 2_000
# The largest profile stored or injected into a prompt, ~1.6x the 300 words
# the prompt asks for. A larger rewrite is condensed before it is stored: a
# profile that keeps growing ends up longer than one rewrite can reproduce,
# and from then on every rewrite is cut and the profile never changes again.
_MAX_PROFILE_CHARS = 3_000
# What one agent keeps about the user's work, and what of it a prompt carries.
# ``_CONSOLIDATION_PROMPT`` spells the per-agent cap.
_MAX_NOTES_PER_AGENT = 8
_MAX_NOTE_CHARS = 400
_MAX_NOTES_PROMPT_CHARS = 1_500
# Every entry of one user, the notes of agents long gone included.
_MAX_ENTRIES = 200

_CONDENSE_PROMPT = (
    "You are a user-profile curator. The following profile of a user, read by "
    "every AI agent before it works for them, has grown too large. Rewrite it "
    "as a tighter version that keeps every durable, still-relevant item.\n\n"
    "Rules:\n"
    "- Max 300 words. This is a hard limit.\n"
    "- Use bullet points.\n"
    "- Merge redundant or overlapping items.\n"
    "- Drop outdated, contradicted, or one-off details such as the outcome "
    "of a single task.\n"
    "- Keep everything the user explicitly asked to be remembered.\n"
    "- Write in third person, without timestamps or meta-commentary."
)

_CONSOLIDATION_PROMPT = (
    "You maintain what an AI platform remembers about one user. Every agent "
    "on the platform reads the user's PROFILE; each agent reads only its own "
    "NOTES.\n\n"
    "PROFILE is the canonical place for CROSS-AGENT personal preferences: "
    "durable facts about the user and nothing else. Individual agents must not "
    "duplicate this information in their own memory, so capture it here when "
    "the user expresses it:\n"
    "- The user's name, role, team, and projects\n"
    "- Preferred language / locale for replies (e.g. \"replies in French\", "
    "\"writes in English but tolerates code in any language\"). When the user "
    "switches language in the new activity, update it accordingly - never "
    "list two contradicting language preferences.\n"
    "- Communication and drafting preferences (tone, formality, brevity, "
    "signature rules)\n"
    "- Timezone and working hours\n"
    "- Key contacts, each with their role\n"
    "- Things the user explicitly asked every agent to remember about them\n\n"
    "NOTES hold everything about an agent's work for the user: the tasks it "
    "handles, their state, the decisions taken and the rules it follows for "
    "the user. A note belongs to the agent whose interaction produced it. "
    "Never put an agent's work in PROFILE: it goes stale, and every other "
    "agent would read it as a fact about the user.\n\n"
    "Update the current entries with the new activity log. A current profile "
    "given as free text predates this split: keep in PROFILE what is about "
    "the user, file the rest as NOTES of the listed agent it came from, and "
    "drop what no listed agent produced.\n"
    "Rules:\n"
    "- PROFILE: max 300 words - it is injected into every prompt. Short "
    "third-person statements (\"The user prefers...\").\n"
    "- NOTES: at most 8 per agent, one short statement "
    "each. Set \"perishable\" to true when a note states something that can "
    "change without anyone saying so (a state, a status, a deadline), and "
    "\"restated\" to true when the new interactions state it again.\n"
    "- Remove outdated or contradicted entries.\n"
    "- Do NOT include timestamps.\n"
    "Answer with JSON only, \"agent\" being the agent's number in the log: "
    "{\"profile\": [\"...\"], \"notes\": [{\"agent\": 1, \"text\": \"...\", "
    "\"perishable\": false, \"restated\": false}]}"
)


# ── Redis key helpers ────────────────────────────────────────────────────────


#: Every key a profile holds: the writers write only these, and
#: ``clear_user_memory`` deletes ``profile_keys``.
_TIERS = ("short", "recent", "medium", "counter", "entries")


def _tier_key(identity: str, tier: str) -> str:
    return f"user_mem:{tier}:{identity}"


def _short_term_key(identity: str) -> str:
    """The older interactions: read, never written."""
    return _tier_key(identity, "short")


def _recent_key(identity: str) -> str:
    """The interactions, each with its agent.

    Not ``short``: an older replica shows that list whole to every
    agent, so an interaction written there during a rolling deploy or after a
    rollback would reach every agent again.
    """
    return _tier_key(identity, "recent")


def _medium_term_key(identity: str) -> str:
    return _tier_key(identity, "medium")


def _counter_key(identity: str) -> str:
    return _tier_key(identity, "counter")


def _entries_key(identity: str) -> str:
    return _tier_key(identity, "entries")


def profile_keys(identity: str) -> tuple[str, ...]:
    """Every Redis key of one user's (or visitor's) cross-agent memory."""
    return tuple(_tier_key(identity, tier) for tier in _TIERS)


def _generation_key(identity: str) -> str:
    """Bumped by every purge the user asks for; outlives it, so a write in flight sees it moved."""
    return _tier_key(identity, "generation")


async def memory_generation(redis, identity: str) -> str:
    """The purge generation a writer reads with its snapshot (see :func:`update_entries`)."""
    return await redis.get(_generation_key(identity)) or "0"


async def _undo_if_account_deleted(redis, identity: str) -> bool:
    return await undo_if_buried(
        redis, (account_tombstone(identity),), profile_keys(identity)
    )


# ── Entries ──────────────────────────────────────────────────────────────────


async def _read_entries(redis, identity: str) -> tuple[list[MemoryEntry], str, bool]:
    """(entries, free-text profile, whether the entries are stored).

    An older profile is free text with no entries: no agent
    reads it (nothing tells its profile from another agent's work) until the
    model splits it - the sweep (:func:`migrate_legacy_profile`) or the next
    consolidation. A read never writes.
    """
    raw_entries = await redis.get(_entries_key(identity))
    medium = await redis.get(_medium_term_key(identity)) or ""
    if raw_entries:
        return _stored_entries(raw_entries, identity), medium, True
    return [], medium, False


def _entries_of(memory: dict) -> list[MemoryEntry]:
    """The entries a loaded memory holds."""
    return entries_from_raw(memory.get("entries"))


# The entries and their profile copy, written together and only over the
# entries the writer read (ARGV[1] = "1": the stored value is ARGV[2]; "0":
# nothing is stored) and, when ARGV[6] names one, in the purge generation the
# writer started in. Two writers - the sweep, and a consolidation whose read
# precedes an LLM call - never overwrite each other, and neither undoes a
# purge that ran meanwhile. Over no entries, with ARGV[7] = "1", only while
# the free-text profile is still ARGV[8]: an older replica rewrites
# it in place, and a split of the text it replaced would drop that rewrite.
_WRITE_ENTRIES_SCRIPT = """
if ARGV[6] ~= '' and (redis.call('GET', KEYS[3]) or '0') ~= ARGV[6] then
    return -1
end
local current = redis.call('GET', KEYS[1])
if ARGV[1] == '1' then
    if current ~= ARGV[2] then return 0 end
elseif current then
    return 0
elseif ARGV[7] == '1' and (redis.call('GET', KEYS[2]) or '') ~= ARGV[8] then
    return -2
end
redis.call('SET', KEYS[1], ARGV[3], 'EX', ARGV[5])
if ARGV[4] == '' then
    redis.call('DEL', KEYS[2])
else
    redis.call('SET', KEYS[2], ARGV[4], 'EX', ARGV[5])
end
return 1
"""
_WRITE_ATTEMPTS = 5

# One consolidation of a memory at a time: two at once would settle the same
# interactions twice and apply proposals computed from the same snapshot.
# Outside the ``user_mem:`` family, which a purge deletes.
_CONSOLIDATION_LEASE_KEY = "lease:user_memory_consolidation:{identity}"
_CONSOLIDATION_LEASE_SECS = 600
_RELEASE_LEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""

# Takes the interactions one consolidation integrated (ARGV[2], counted when
# it read them) off the counter, in the purge generation it read them in only:
# the interactions counted since stay counted, and a counter a purge restarted
# is left alone.
_SETTLE_COUNTER_SCRIPT = """
if (redis.call('GET', KEYS[2]) or '0') ~= ARGV[1] then return -1 end
local left = (tonumber(redis.call('GET', KEYS[1])) or 0) - tonumber(ARGV[2])
if left < 0 then left = 0 end
redis.call('SET', KEYS[1], left, 'EX', ARGV[3])
return left
"""


def _stored_entries(raw: str, identity: str) -> list[MemoryEntry]:
    try:
        return entries_from_raw(json.loads(raw))
    except (TypeError, ValueError):
        logger.warning("Unreadable user-memory entries for identity %s", identity)
        return []


async def update_entries(
    redis,
    identity: str,
    change: Callable[[list[MemoryEntry] | None], list[MemoryEntry] | None],
    *,
    generation: str | None = None,
    free_text_read: str | None = None,
) -> list[MemoryEntry] | None:
    """Write what *change* makes of the stored entries; what was written, ``None`` for nothing.

    *change* gets the entries as stored now (``None``: none are stored yet)
    and returns the entries to write, ``None`` to write nothing. The write
    lands only over the value *change* read: otherwise it is computed again
    from a fresh read. With *generation* (:func:`memory_generation`, read with
    the writer's snapshot), nothing is written once a purge ran since. With
    *free_text_read* (the free-text profile the writer computed from), entries
    written where none are stored land only while that profile is unchanged.
    ``user_mem:medium:`` gets the PROFILE alone, so a reader of the free-text
    profile (an older replica during a rolling deploy) never sees a note.
    """
    key = _entries_key(identity)
    for _ in range(_WRITE_ATTEMPTS):
        raw = await redis.get(key)
        entries = change(_stored_entries(raw, identity) if raw else None)
        if entries is None:
            return None
        written = await redis.eval(
            _WRITE_ENTRIES_SCRIPT,
            3,
            key,
            _medium_term_key(identity),
            _generation_key(identity),
            "1" if raw else "0",
            raw or "",
            json.dumps([entry.to_dict() for entry in entries], ensure_ascii=False),
            render_profile(entries),
            str(MEDIUM_TERM_TTL),
            generation or "",
            "0" if free_text_read is None else "1",
            free_text_read or "",
        )
        if written == -1:
            logger.info("User memory of %s was purged meanwhile: nothing written", identity)
            return None
        if written == -2:
            logger.info("Free-text profile of %s was rewritten meanwhile: nothing written", identity)
            return None
        if written:
            return entries
    logger.info(
        "User memory of %s changed under %d writes in a row: this one is dropped",
        identity,
        _WRITE_ATTEMPTS,
    )
    return None


# ── Public API ───────────────────────────────────────────────────────────────


async def load_user_memory(identity: str) -> dict:
    """Load both memory tiers for a user or visitor.

    *identity* is a user UUID (authenticated) or visitor_id (public widget).

    Returns::

        {
            "short_term": [{"agent": ..., "agent_id": ..., "summary": ..., "timestamp": ...}, ...],
            "medium_term": "the PROFILE, rendered, or empty",
            "entries": [MemoryEntry.to_dict(), ...],
            "legacy_profile": "a free-text profile not split yet, or empty",
        }

    Every entry is returned, whoever may read it: what a prompt carries is
    :func:`format_user_memory_for_prompt`'s choice.
    """
    if not identity:
        return {"short_term": [], "medium_term": "", "entries": []}

    redis = await redis_client()

    # One snapshot: the interactions and how many of them no consolidation
    # integrated yet (each store pushes and counts in one transaction).
    pipe = redis.pipeline(transaction=True)
    pipe.lrange(_recent_key(identity), 0, SHORT_TERM_MAX_ENTRIES - 1)
    pipe.lrange(_short_term_key(identity), 0, SHORT_TERM_MAX_ENTRIES - 1)
    pipe.get(_counter_key(identity))
    recent, legacy_short, pending = await pipe.execute()
    raw_entries = [*(recent or []), *(legacy_short or [])]
    short_term: list[dict] = []
    for raw in raw_entries:
        try:
            short_term.append(json.loads(raw))
        except (TypeError, ValueError):
            logger.debug(
                "Skipping malformed user-memory entry for identity %s",
                identity,
            )
            continue
    # Newest first, as one list.
    short_term.sort(key=lambda entry: str(entry.get("timestamp") or ""), reverse=True)
    short_term = short_term[:SHORT_TERM_MAX_ENTRIES]

    entries, medium, stored = await _read_entries(redis, identity)

    return {
        "short_term": short_term,
        "medium_term": render_profile(entries),
        "entries": [entry.to_dict() for entry in entries],
        # A profile not split yet (see ``_read_entries``): shown to its user,
        # given to the consolidation, read by no agent.
        "legacy_profile": "" if stored else medium,
        # The newest ``pending`` interactions are the ones no consolidation integrated.
        "pending": int(pending or 0),
    }


async def store_user_memory(
    identity: str,
    *,
    agent_name: str,
    summary: str,
    interaction_type: str = "chat",
    agent_id: str | None = None,
    replaces_prefix: str | None = None,
) -> int:
    """Store an interaction in the user's short-term memory.

    Args:
        identity: User UUID or visitor_id.
        agent_name: Which agent was involved.
        summary: Brief summary of what happened.
        interaction_type: "chat" | "assignment".
        agent_id: The agent's id: the notes the interaction leaves are its own.
        replaces_prefix: The entries whose summary starts with it are removed
            first (the note of a cut chat turn its resume answered).

    Returns:
        The current consolidation counter value.
    """
    if not identity or not summary:
        return 0

    redis = await redis_client()
    key = _recent_key(identity)
    stale = await entries_starting_with(redis, key, replaces_prefix, SHORT_TERM_MAX_ENTRIES)

    entry = json.dumps(
        {
            "agent": agent_name,
            "agent_id": str(agent_id) if agent_id else None,
            "interaction_type": interaction_type,
            # Redacted before the cap, so a cut never leaves part of a value.
            "summary": redact_known_secrets(summary)[:1500],
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )

    pipe = redis.pipeline()
    for raw in stale:
        pipe.lrem(key, 0, raw)
    pipe.lpush(key, entry)
    pipe.ltrim(key, 0, SHORT_TERM_MAX_ENTRIES - 1)
    pipe.expire(key, SHORT_TERM_TTL)
    pipe.incr(_counter_key(identity))
    pipe.expire(_counter_key(identity), SHORT_TERM_TTL)
    # Checked after the write (see ``memory_tombstones``).
    pipe.exists(account_tombstone(identity))
    results = (await pipe.execute())[len(stale) :]
    if results[5]:
        await redis.delete(*profile_keys(identity))
        return 0

    return int(results[3])


async def _rewrite_profile(
    llm: BaseLLMProvider,
    model: str | None,
    messages: list,
    *,
    identity: str,
    trace_name: str,
    temperature: float,
) -> str | None:
    """The answer one LLM rewrite gives, ``None`` when it is still cut."""
    attempts = 0

    async def _call(max_tokens: int):
        nonlocal attempts
        name = f"{trace_name}_retry" if attempts else trace_name
        attempts += 1
        async with traced_generation(name=name, messages=messages) as gen:
            gen.response = await llm.chat(
                messages=messages,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                # On the models that think by default, thinking would share
                # the budget with the profile.
                disable_thinking=True,
            )
        return gen.response

    response, cut = await chat_retrying_a_cut(
        llm,
        model,
        _CONSOLIDATION_MAX_TOKENS,
        _call,
        label=f"User profile rewrite for {identity}",
    )
    if cut:
        return None
    return (response.content or "").strip()


async def _condense_profile(
    profile: str,
    llm: BaseLLMProvider,
    *,
    model: str | None,
    identity: str,
) -> str:
    """*profile* condensed by one LLM rewrite, always within ``_MAX_PROFILE_CHARS``.

    A rewrite that fails, is cut or is still too long leaves the profile cut
    at its last whole line within the cap.
    """
    from app.llm.base import ChatMessage

    messages = [
        ChatMessage(role="system", content=_CONDENSE_PROMPT),
        ChatMessage(role="user", content=f"Profile to condense:\n\n{profile}"),
    ]
    condensed: str | None = None
    try:
        condensed = await _rewrite_profile(
            llm,
            model,
            messages,
            identity=identity,
            trace_name="user_memory_condense",
            temperature=0.2,
        )
    except Exception as e:
        logger.warning(
            "User profile condensation failed for %s: %s",
            identity,
            format_exception_message(e),
        )
    result = _truncate_at_boundary(condensed or profile, _MAX_PROFILE_CHARS, "")
    logger.info(
        "Condensed the user profile of %s: %d -> %d chars",
        identity,
        len(profile),
        len(result),
    )
    return result


def _window_agents(short_entries: list[dict]) -> list[tuple[str | None, str]]:
    """The agents of a consolidation window, in the order they appear: ``(id, name)``.

    An older interaction names its agent only; it takes the id
    another interaction of the window gives that name, unless two agents of
    the window share it (agent names are not unique): it then has no agent.
    """
    named: dict[str, set[str]] = {}
    for entry in short_entries:
        if entry.get("agent_id"):
            named.setdefault(entry.get("agent"), set()).add(str(entry["agent_id"]))
    ids_by_name = {name: next(iter(ids)) for name, ids in named.items() if len(ids) == 1}
    agents: list[tuple[str | None, str]] = []
    for entry in reversed(short_entries):
        name = entry.get("agent") or "?"
        agent_id = str(entry["agent_id"]) if entry.get("agent_id") else ids_by_name.get(name)
        if all(known != (agent_id, name) for known in agents):
            agents.append((agent_id, name))
    return agents


def _entry_agent_id(entry: dict, window: list[tuple[str | None, str]]) -> str | None:
    if entry.get("agent_id"):
        return str(entry["agent_id"])
    ids = {aid for aid, name in window if name == entry.get("agent") and aid}
    return next(iter(ids)) if len(ids) == 1 else None


def _parse_consolidation(answer: str) -> tuple[list[str], list[tuple[int | None, str, bool, bool]]] | None:
    """(profile statements, notes) of a consolidation answer, ``None`` to keep the memory as it is.

    The answer is applied only when it is the JSON asked for, with both lists:
    it replaces the whole profile and the notes it was shown, so one with a
    field missing (or not a list) would erase what it left out, and text that
    is not that JSON says nothing about which statement is whose. Empty lists
    are an answer: the shown notes are dropped (:func:`_consolidated` keeps a
    profile no rewrite replaced).
    """
    from app.services.structured_output import extract_json_text

    text = (answer or "").strip()
    if not text:
        return None
    try:
        data = json.loads(extract_json_text(text))
    except (TypeError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("profile"), list) or not isinstance(data.get("notes"), list):
        logger.warning("User memory consolidation did not answer a profile list and a notes list: memory kept")
        return None
    profile = [item.strip() for item in data["profile"] if isinstance(item, str) and item.strip()]
    notes: list[tuple[int | None, str, bool, bool]] = []
    for note in data["notes"]:
        if not isinstance(note, dict):
            continue
        note_text = note.get("text")
        if not isinstance(note_text, str) or not note_text.strip():
            continue
        # A note's freshness is part of the schema: one the model did not
        # mark as changing or not would be stored with no expiry.
        perishable, restated = note.get("perishable"), note.get("restated", False)
        if not isinstance(perishable, bool) or not isinstance(restated, bool):
            logger.warning(
                "User memory consolidation answered a note without a boolean perishable / restated: memory kept"
            )
            return None
        agent = note.get("agent")
        if isinstance(agent, str) and agent.strip().isdigit():
            agent = int(agent)
        index = agent if isinstance(agent, int) and not isinstance(agent, bool) else None
        notes.append((index, note_text.strip(), perishable, restated))
    return profile, notes


def _dedupe(entries: list[MemoryEntry]) -> list[MemoryEntry]:
    seen: set[tuple[str, str]] = set()
    unique = []
    for entry in entries:
        key = (" ".join(entry.text.lower().split()), entry.scope)
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    return unique


def _bounded(entries: list[MemoryEntry]) -> list[MemoryEntry]:
    """*entries* within the per-agent and per-user caps, the newest notes kept."""
    for entry in entries:
        if not entry.is_global and len(entry.text) > _MAX_NOTE_CHARS:
            entry.text = _truncate_at_boundary(entry.text, _MAX_NOTE_CHARS, "")
    notes = sorted(
        (entry for entry in entries if not entry.is_global),
        key=lambda entry: entry.last_verified_at,
        reverse=True,
    )
    per_scope: dict[str, int] = {}
    kept_notes: set[str] = set()
    for entry in notes:
        per_scope[entry.scope] = per_scope.get(entry.scope, 0) + 1
        if per_scope[entry.scope] <= _MAX_NOTES_PER_AGENT:
            kept_notes.add(entry.id)
    bounded = [entry for entry in entries if entry.is_global or entry.id in kept_notes]
    excess = len(bounded) - _MAX_ENTRIES
    if excess > 0:
        oldest = {entry.id for entry in sorted(notes, key=lambda e: e.last_verified_at)[:excess]}
        bounded = [entry for entry in bounded if entry.id not in oldest]
    return bounded


def _proposed_entries(
    existing: list[MemoryEntry],
    profile: list[str],
    notes: list[tuple[int | None, str, bool, bool]],
    window: list[tuple[str | None, str]],
    now: datetime,
) -> list[MemoryEntry]:
    """The entries one consolidation answer makes: the new profile and the window's notes.

    A note's owner is the agent of the window it names; a note naming none (or
    an agent the window only knows by a name) has no owner and is dropped:
    no agent would read it. A note the model marks restated is stated now.
    """
    updated = [keep_history(new_entry(text, now=now), existing) for text in profile]
    dropped = 0
    for index, text, perishable, restated in notes:
        agent_id = window[index - 1][0] if index and 1 <= index <= len(window) else None
        if not agent_id:
            dropped += 1
            continue
        entry = keep_history(new_entry(text, now=now, owner_agent_id=agent_id, perishable=perishable), existing)
        updated.append(stated_again(entry, now=now) if restated else entry)
    if dropped:
        logger.info("User memory: %d note(s) naming no agent of the window dropped", dropped)
    return updated


def _consolidated(
    stored: list[MemoryEntry],
    snapshot: list[MemoryEntry],
    proposal: list[MemoryEntry],
    shown: set[str],
) -> list[MemoryEntry]:
    """The entries a consolidation writes over *stored*, the entries as they are now.

    It started from *snapshot* and showed the model the notes *shown*: the
    shown notes are replaced by the *proposal*'s, an empty list included, and
    so is the profile when the proposal has one - a profile never empties on
    its own, the user's Clear does that. Every other note is kept as stored
    now. An entry of the snapshot that is gone from *stored* was removed
    meanwhile (the sweep, a forgotten agent): the proposal never brings it back.
    """
    removed = {entry.id for entry in snapshot} - {entry.id for entry in stored}
    proposed = [entry for entry in proposal if entry.id not in removed]
    profile = [entry for entry in proposed if entry.is_global] or [entry for entry in stored if entry.is_global]
    notes = [entry for entry in proposed if not entry.is_global]
    kept = [entry for entry in stored if not entry.is_global and entry.id not in shown]
    return _bounded(_dedupe(profile + notes + kept))


def _shown_notes(existing: list[MemoryEntry], window: list[tuple[str | None, str]]) -> list[MemoryEntry]:
    """The notes a consolidation shows the model: those of the agents of its window."""
    ids = {aid for aid, _ in window if aid}
    return [entry for entry in existing if not entry.is_global and entry.owner_agent_id in ids]


async def _condensed(
    entries: list[MemoryEntry],
    llm: BaseLLMProvider,
    *,
    model: str | None,
    identity: str,
    now: datetime,
) -> list[MemoryEntry]:
    """*entries* with a profile over ``_MAX_PROFILE_CHARS`` condensed."""
    profile = [entry for entry in entries if entry.is_global]
    rendered = render_profile(profile)
    if len(rendered) <= _MAX_PROFILE_CHARS:
        return entries
    condensed = await _condense_profile(rendered, llm, model=model, identity=identity)
    rewritten = [keep_history(new_entry(statement, now=now), profile) for statement in bullet_lines(condensed)]
    return _dedupe(rewritten + [entry for entry in entries if not entry.is_global])


def _consolidation_messages(
    existing: list[MemoryEntry],
    window: list[tuple[str | None, str]],
    short_entries: list[dict],
    *,
    legacy_profile: str = "",
) -> list:
    from app.llm.base import ChatMessage

    numbers = {aid: index for index, (aid, _) in enumerate(window, start=1) if aid}
    agents = "\n".join(f"{index}. {name}" for index, (_, name) in enumerate(window, start=1))
    profile = render_profile(existing) or (f"(free text)\n{legacy_profile}" if legacy_profile else "")
    notes = "\n".join(
        f"[agent {numbers[entry.owner_agent_id]}] {entry.text}" for entry in _shown_notes(existing, window)
    )
    transcript = []
    for entry in reversed(short_entries):
        ts = entry.get("timestamp", "")[:16]
        name = entry.get("agent", "?")
        index = next(
            (i for i, (aid, agent) in enumerate(window, start=1) if agent == name and aid == _entry_agent_id(entry, window)),
            "?",
        )
        transcript.append(f"[{ts}] (agent {index}, {name}): {entry.get('summary', '')}")
    return [
        ChatMessage(role="system", content=_CONSOLIDATION_PROMPT),
        ChatMessage(
            role="user",
            content=(
                f"Agents in this activity log:\n{agents or '(none)'}\n\n"
                f"Current profile:\n{profile or '(empty — first profile build)'}\n\n"
                f"Current notes of these agents:\n{notes or '(none)'}\n\n"
                f"New interactions to integrate:\n" + ("\n".join(transcript) or "(none)")
            ),
        ),
    ]


async def consolidate_user_memory(
    identity: str,
    llm: BaseLLMProvider,
    *,
    force: bool = False,
    model: str | None = None,
) -> None:
    """Consolidate short-term entries into the medium-term entries.

    Only fires when the counter exceeds the threshold (or *force* is True),
    and while no other consolidation of the same memory runs: the interactions
    it leaves counted start the next one.
    """
    if not identity:
        return

    redis = await redis_client()
    lease = _CONSOLIDATION_LEASE_KEY.format(identity=identity)
    token = uuid.uuid4().hex
    if not await redis.set(lease, token, nx=True, ex=_CONSOLIDATION_LEASE_SECS):
        return
    try:
        await _consolidate(redis, identity, llm, force=force, model=model)
    finally:
        await redis.eval(_RELEASE_LEASE_SCRIPT, 1, lease, token)


async def _consolidate(redis, identity: str, llm: BaseLLMProvider, *, force: bool, model: str | None) -> None:

    # Read before the snapshot: a purge from here on drops this write.
    generation = await memory_generation(redis, identity)
    counted = int(await redis.get(_counter_key(identity)) or 0)
    if not force and counted < CONSOLIDATION_THRESHOLD:
        return
    memory = await load_user_memory(identity)
    # Only the interactions no consolidation integrated are new: the rest of
    # the retained history is already in the entries.
    pending = int(memory.get("pending", counted))
    short_entries = memory["short_term"][:pending] if pending else memory["short_term"]
    if not short_entries:
        return

    now = datetime.now(UTC)
    existing = _entries_of(memory)
    window = _window_agents(short_entries)
    consolidation_messages = _consolidation_messages(
        existing, window, short_entries, legacy_profile=memory.get("legacy_profile") or ""
    )
    try:
        answer = await _rewrite_profile(
            llm,
            model,
            consolidation_messages,
            identity=identity,
            trace_name="user_memory_consolidate",
            temperature=0.3,
        )
        proposal: list[MemoryEntry] | None = None
        shown: set[str] = set()
        if answer is None:
            if len(render_profile(existing)) > _MAX_PROFILE_CHARS:
                # Too long for a rewrite to carry with the new activity: kept
                # as it is, every later rewrite would be cut as well.
                logger.warning(
                    "User profile rewrite for %s still cut at max_tokens - "
                    "condensing the existing profile",
                    identity,
                )
                proposal = [entry for entry in existing if entry.is_global]
            else:
                logger.warning(
                    "User profile rewrite for %s still cut at max_tokens - "
                    "keeping the existing profile",
                    identity,
                )
        else:
            parsed = _parse_consolidation(answer)
            if parsed is not None:
                proposal = _proposed_entries(existing, *parsed, window, now)
                shown = {entry.id for entry in _shown_notes(existing, window)}
        written = None
        if proposal is not None:
            proposal = await _condensed(proposal, llm, model=model, identity=identity, now=now)
            written = await update_entries(
                redis,
                identity,
                lambda stored: _consolidated(
                    existing if stored is None else stored, existing, proposal, shown
                ),
                generation=generation,
                free_text_read=memory.get("legacy_profile") or "",
            )
        if written is not None:
            # Detached from the turn: the account may have been deleted
            # during the LLM call (see ``memory_tombstones``).
            if await _undo_if_account_deleted(redis, identity):
                return
            logger.info("Consolidated user memory for %s", identity)

        # Settled either way: a rewrite still cut is tried again once the next
        # CONSOLIDATION_THRESHOLD interactions are in, not on every turn.
        await redis.eval(
            _SETTLE_COUNTER_SCRIPT,
            2,
            _counter_key(identity),
            _generation_key(identity),
            generation,
            str(pending),
            str(SHORT_TERM_TTL),
        )
        await _undo_if_account_deleted(redis, identity)

    except Exception as e:
        logger.warning("Failed to consolidate user memory for %s: %s", identity, format_exception_message(e))


async def migrate_legacy_profile(
    identity: str,
    llm: BaseLLMProvider,
    *,
    agents: list[tuple[str, str]],
    model: str | None = None,
) -> bool:
    """Split an older free-text profile into entries; whether it was written.

    The consolidation, run on the profile alone: the model is shown it and the
    agents the user worked with (*agents*: ``(id, name)``) and answers the same
    JSON - what is about the user becomes the profile, what an agent's work
    left becomes that agent's notes, the rest is dropped.
    """
    redis = await redis_client()
    generation = await memory_generation(redis, identity)
    _entries, legacy, stored = await _read_entries(redis, identity)
    if stored or not legacy.strip():
        return False
    window: list[tuple[str | None, str]] = [(str(agent_id), name) for agent_id, name in agents]
    answer = await _rewrite_profile(
        llm,
        model,
        _consolidation_messages([], window, [], legacy_profile=legacy),
        identity=identity,
        trace_name="user_memory_migrate",
        temperature=0.2,
    )
    parsed = _parse_consolidation(answer) if answer is not None else None
    if parsed is None:
        return False
    now = datetime.now(UTC)
    proposal = await _condensed(_proposed_entries([], *parsed, window, now), llm, model=model, identity=identity, now=now)
    migrated = _bounded(_dedupe(proposal))
    written = await update_entries(
        redis,
        identity,
        lambda current: migrated if current is None else None,
        generation=generation,
        free_text_read=legacy,
    )
    return written is not None and not await _undo_if_account_deleted(redis, identity)


async def clear_user_memory(identity: str, *, tombstone: bool = False) -> None:
    """Delete both memory tiers of a user or visitor (account deletion).

    With *tombstone* (the account is gone for good) a write still in flight
    for it undoes itself instead of recreating the profile (see
    ``memory_tombstones``).
    """
    if not identity:
        return
    redis = await redis_client()
    if tombstone:
        await bury(redis, account_tombstone(identity))
    # The generation moves rather than going: an account re-created under the
    # same id lifts its tombstone, and a writer still holding the old
    # generation must not write the deleted profile into the new account.
    await _move_generation(redis, identity, *profile_keys(identity))


async def _move_generation(redis, identity: str, *also_delete: str) -> list:
    """Move the purge generation and delete *also_delete* in one transaction.

    One step: a writer that read the new generation also read what the
    deletion left, never what it removed.
    """
    pipe = redis.pipeline(transaction=True)
    pipe.incr(_generation_key(identity))
    pipe.expire(_generation_key(identity), SHORT_TERM_TTL)
    if also_delete:
        pipe.delete(*also_delete)
    return await pipe.execute()


async def purge_user_memory(identity: str) -> int:
    """Delete one user's memory at their request; the number of keys deleted.

    The purge generation moves with the deletion, so a consolidation or a
    sweep still holding what it read before writes nothing (:func:`update_entries`).
    """
    results = await _move_generation(await redis_client(), identity, *profile_keys(identity))
    return int(results[-1])


async def forget_agent_notes(identity: str, agent_id: str) -> int:
    """Drop what *agent_id*'s work left in one user's memory: its notes and its recent interactions.

    The interactions go first, then the generation moves: a consolidation
    that read them read the generation before, and writes nothing. The
    counter loses the removed interactions that were still pending (the
    newest ``counter`` of the list), so it keeps naming the new ones.
    """
    agent = str(agent_id)
    redis = await redis_client()
    pipe = redis.pipeline(transaction=True)
    pipe.lrange(_recent_key(identity), 0, -1)
    pipe.get(_counter_key(identity))
    pipe.get(_generation_key(identity))
    recent, counter, generation = await pipe.execute()
    pending = int(counter or 0)
    removed = removed_pending = 0
    for position, raw in enumerate(recent or []):
        try:
            entry = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(entry, dict) and str(entry.get("agent_id") or "") == agent:
            gone = int(await redis.lrem(_recent_key(identity), 1, raw) or 0)
            removed += gone
            removed_pending += gone if position < pending else 0
    if removed_pending:
        await redis.eval(
            _SETTLE_COUNTER_SCRIPT,
            2,
            _counter_key(identity),
            _generation_key(identity),
            generation or "0",
            str(removed_pending),
            str(SHORT_TERM_TTL),
        )
    await _move_generation(redis, identity)
    dropped = 0

    def _without_agent(current: list[MemoryEntry] | None) -> list[MemoryEntry] | None:
        nonlocal dropped
        if current is None:
            return None
        kept = [entry for entry in current if entry.owner_agent_id != agent and entry.scope != agent_scope(agent)]
        dropped = len(current) - len(kept)
        return kept if dropped else None

    await update_entries(redis, identity, _without_agent)
    return dropped + removed


def _own_interactions(short: list[dict], *, agent_id: str | None) -> list[dict]:
    """The interactions of *short* that were with this agent.

    An older one names its agent only, and agent names are not
    unique: it is shown to no agent.
    """
    if not agent_id:
        return []
    return [entry for entry in short if entry.get("agent_id") and str(entry["agent_id"]) == agent_id]


def format_user_memory_for_prompt(
    memory: dict,
    *,
    agent_id: object = None,
    now: datetime | None = None,
) -> str:
    """Render what *agent_id* may read of the user memory, for its system prompt.

    The user's profile, the agent's own notes and its own recent
    interactions: never another agent's notes or interactions, never an
    expired note. A note about state that can change says it is
    unverified, and since when. Without an agent, the profile alone.
    """
    now = now or datetime.now(UTC)
    agent = str(agent_id) if agent_id else None
    visible = visible_to(_entries_of(memory), agent_id=agent, now=now)
    profile, own_notes = split_visible(visible, agent_id=agent)
    parts: list[str] = []

    rendered = render_profile(profile)
    if rendered:
        if len(rendered) > _MAX_PROFILE_CHARS:
            # A profile stored before the cap: its next consolidation
            # condenses it, until then the prompt carries its head.
            logger.debug(
                "User profile of %d characters cut to %d for the prompt",
                len(rendered),
                _MAX_PROFILE_CHARS,
            )
        parts.append(PROFILE_HEADER)
        parts.append(_truncate_at_boundary(rendered, _MAX_PROFILE_CHARS, ""))

    if own_notes:
        parts.append(f"\n{OWN_NOTES_HEADER}")
        parts.append(
            _truncate_at_boundary("\n".join(map(render_line, own_notes)), _MAX_NOTES_PROMPT_CHARS, "")
        )

    short = _own_interactions(memory.get("short_term") or [], agent_id=agent)
    if short:
        parts.append("\n── Your recent interactions with this user ──")
        for entry in reversed(short):  # chronological
            ts = entry.get("timestamp", "")[:16]
            summary = entry.get("summary", "")
            parts.append(f"• [{ts}] {summary[:250]}")

    return "\n".join(parts) if parts else ""
