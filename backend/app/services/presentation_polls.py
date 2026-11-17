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

"""Live audience polls of a deck (``poll`` blocks, xtm-one #10157).

A ``poll`` block (``presentation_spec.PollBlock``) carries its questions in
the deck spec; this module owns what lives beside the spec:

* ``PresentationPoll`` - one row per poll block (keyed by the block's ``id``),
  created on first read by someone who can see the deck: its short audience
  ``code``, the question the presenter opened, ``locked`` and ``revealed``.
* ``PresentationPollVote`` - one anonymous answer per device and question.

The audience page ``/v/CODE`` is public: the code resolves to THIS poll only
and the payload it gets is the poll's questions and state, never the rest of
the deck. Votes are bounded per poll (``MAX_VOTERS_PER_POLL``), the word
cloud is cleaned and filtered (``clean_words``), and everything is deleted
with the deck by the foreign keys.
"""

from __future__ import annotations

import re
import secrets
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_token
from app.models.presentation import Presentation, PresentationPoll, PresentationPollVote
from app.services.presentation_spec import DEFAULT_POLL_REVEAL_SECONDS, DEFAULT_POLL_REVEAL_VOTES, poll_blocks

#: The audience page route (the SPA serves it; ``/v`` is a reserved root segment).
POLL_ROUTE = "/v"
#: Unambiguous alphabet: no 0/O, 1/I/L, so a code read off a wall is typed right.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 5
CODE_RE = re.compile(rf"^[{CODE_ALPHABET}]{{{CODE_LENGTH}}}$")
#: Distinct voters one poll accepts across its questions.
MAX_VOTERS_PER_POLL = 500
MAX_WORD_CHARS = 30
RATING_SCALE = (1, 2, 3, 4, 5)
#: The word-cloud filter: a word on this list is refused, whatever its case.
#: Kept short and generic; an operator who wants more extends it here.
BLOCKED_WORDS: frozenset[str] = frozenset(
    {
        "fuck", "fucking", "shit", "bitch", "cunt", "dick", "pussy", "asshole", "bastard", "whore", "slut",
        "merde", "putain", "connard", "salope", "encule", "pute", "con", "nique",
    }
)
_WORD_RE = re.compile(r"[^\w\-' ]+", re.UNICODE)


class PollError(ValueError):
    """A vote the poll refuses; ``reason`` is a stable key the audience page maps to words."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class PollContext:
    poll: PresentationPoll
    block: dict[str, Any]


def poll_url(code: str) -> str:
    """The absolute short link the slide draws and the QR encodes."""
    from app.services.email import platform_url

    return f"{platform_url()}{POLL_ROUTE}/{code}"


def poll_qr_svg(url: str) -> str:
    """The QR code of the short link, as an SVG data URI (pure Python, no network)."""
    import base64
    import io

    import qrcode
    import qrcode.image.svg

    buffer = io.BytesIO()
    qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, border=1).save(buffer)
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def opens_every_question(block: dict[str, Any]) -> bool:
    """``layout: "all"``: every question is open at once, there is no active question to step through."""
    return block.get("layout") == "all"


def question_is_open(block: dict[str, Any], active_question: str | None, question_key: str | None) -> bool:
    """Whether the audience may answer *question_key* now: any question of an ``all`` poll, else the active one."""
    if _question(block, question_key) is None:
        return False
    return opens_every_question(block) or question_key == active_question


class PollNoopError(ValueError):
    """A presenter action that has no meaning on this poll (stepping an ``all`` poll); nothing changes."""


def _question(block: dict[str, Any], question_key: str | None) -> dict[str, Any] | None:
    for question in block.get("questions") or []:
        if isinstance(question, dict) and question.get("id") == question_key:
            return question
    return None


async def ensure_polls(db: AsyncSession, presentation: Presentation) -> dict[str, PresentationPoll]:
    """The poll row of every ``poll`` block of the deck, created when missing (by block id)."""
    blocks = poll_blocks(presentation.spec)
    if not blocks:
        return {}
    keys = [block["id"] for block in blocks]
    rows = (
        await db.execute(
            select(PresentationPoll).where(
                PresentationPoll.presentation_id == presentation.id, PresentationPoll.poll_key.in_(keys)
            )
        )
    ).scalars()
    found = {row.poll_key: row for row in rows}
    for block in blocks:
        if block["id"] in found:
            continue
        first = (block.get("questions") or [{}])[0].get("id")
        for _attempt in range(8):
            stmt = (
                pg_insert(PresentationPoll)
                .values(
                    id=uuid.uuid4(),
                    presentation_id=presentation.id,
                    poll_key=block["id"],
                    code=_new_code(),
                    active_question=first,
                )
                .on_conflict_do_nothing()
                .returning(PresentationPoll.id)
            )
            if (await db.execute(stmt)).scalar() is not None:
                break
        row = (
            await db.execute(
                select(PresentationPoll).where(
                    PresentationPoll.presentation_id == presentation.id, PresentationPoll.poll_key == block["id"]
                )
            )
        ).scalar_one()
        found[block["id"]] = row
    return found


async def resolve_code(db: AsyncSession, code: str) -> PollContext | None:
    """The poll behind an audience code, with its block as the deck holds it now; None when gone."""
    code = (code or "").strip().upper()
    if not CODE_RE.fullmatch(code):
        return None
    poll = (await db.execute(select(PresentationPoll).where(PresentationPoll.code == code))).scalar_one_or_none()
    if poll is None:
        return None
    presentation = await db.get(Presentation, poll.presentation_id)
    if presentation is None:
        return None
    block = next((b for b in poll_blocks(presentation.spec) if b["id"] == poll.poll_key), None)
    if block is None:
        return None
    return PollContext(poll=poll, block=block)


def clean_words(raw: str, max_words: int) -> list[str]:
    """The words of a word-cloud answer: trimmed, lower-cased, bounded, filtered."""
    words: list[str] = []
    for part in re.split(r"[,\n;]+", raw or ""):
        word = _WORD_RE.sub("", part).strip().lower()[:MAX_WORD_CHARS].strip()
        if not word:
            continue
        if any(token in BLOCKED_WORDS for token in re.split(r"[\s\-']+", word)):
            raise PollError("filtered", "That word is not accepted.")
        if word not in words:
            words.append(word)
    if not words:
        raise PollError("invalid", "Type a word.")
    return words[:max_words]


def _vote_values(question: dict[str, Any], answer: Any) -> list[str]:
    kind = question.get("kind", "choice")
    if kind in ("choice", "quiz"):
        if not isinstance(answer, int) or isinstance(answer, bool) or not 0 <= answer < len(question.get("options") or []):
            raise PollError("invalid", "Pick one of the options.")
        return [str(answer)]
    if kind == "rating":
        if not isinstance(answer, int) or isinstance(answer, bool) or answer not in RATING_SCALE:
            raise PollError("invalid", "Pick a rating from 1 to 5.")
        return [str(answer)]
    if not isinstance(answer, str):
        raise PollError("invalid", "Type a word.")
    return [" ".join(clean_words(answer, int(question.get("max_words") or 1)))]


async def cast_vote(db: AsyncSession, ctx: PollContext, *, question_key: str, voter_token: str, answer: Any) -> bool:
    """Record one answer; False when this device already answered this question."""
    poll = ctx.poll
    if poll.locked:
        raise PollError("locked", "Voting is closed for this question.")
    if not question_is_open(ctx.block, poll.active_question, question_key):
        raise PollError("not_open", "This question is not open.")
    question = _question(ctx.block, question_key)
    if question is None:  # pragma: no cover - question_is_open checked it
        raise PollError("not_open", "This question is not open.")
    if not voter_token or len(voter_token) < 16 or len(voter_token) > 128:
        raise PollError("invalid", "Reload the page and try again.")
    values = _vote_values(question, answer)
    voter_hash = hash_token(voter_token)
    known = (
        await db.execute(
            select(func.count(func.distinct(PresentationPollVote.voter_hash))).where(
                PresentationPollVote.poll_id == poll.id
            )
        )
    ).scalar_one()
    if known >= MAX_VOTERS_PER_POLL:
        already = (
            await db.execute(
                select(PresentationPollVote.id)
                .where(PresentationPollVote.poll_id == poll.id, PresentationPollVote.voter_hash == voter_hash)
                .limit(1)
            )
        ).scalar_one_or_none()
        if already is None:
            raise PollError("full", "This poll has reached its number of voters.")
    stmt = (
        pg_insert(PresentationPollVote)
        .values(id=uuid.uuid4(), poll_id=poll.id, question_key=question_key, voter_hash=voter_hash, value=values[0][:200])
        .on_conflict_do_nothing(constraint="uq_presentation_poll_votes_voter")
        .returning(PresentationPollVote.id)
    )
    return (await db.execute(stmt)).scalar() is not None


async def _counts(db: AsyncSession, poll_id: uuid.UUID) -> dict[str, list[str]]:
    rows = (
        await db.execute(
            select(PresentationPollVote.question_key, PresentationPollVote.value).where(
                PresentationPollVote.poll_id == poll_id
            )
        )
    ).all()
    out: dict[str, list[str]] = {}
    for key, value in rows:
        out.setdefault(key, []).append(value)
    return out


async def _first_votes(db: AsyncSession, poll_id: uuid.UUID) -> dict[str, datetime]:
    """When each question of the poll got its first vote (``reveal: after_seconds``, xtm-one #10411)."""
    rows = (
        await db.execute(
            select(PresentationPollVote.question_key, func.min(PresentationPollVote.created_at))
            .where(PresentationPollVote.poll_id == poll_id)
            .group_by(PresentationPollVote.question_key)
        )
    ).all()
    return {key: first for key, first in rows if first is not None}


def question_revealed(
    block: dict[str, Any],
    *,
    flag: bool,
    votes: int,
    first_vote_at: datetime | None,
    now: datetime | None = None,
) -> bool:
    """Whether a quiz question's answer is marked now, per the block's ``reveal`` (xtm-one #10411).

    ``manual``: the presenter's reveal. ``live``: always. ``never``: never.
    ``after_votes``: once the question holds ``reveal_after_votes`` votes (10 when unset).
    ``after_seconds``: ``reveal_after_seconds`` after its first vote. The presenter's
    reveal also shows a conditional answer early; a reset deletes the votes, so
    every condition starts over hidden.
    """
    reveal = block.get("reveal") or "manual"
    if reveal == "never":
        return False
    if reveal == "live" or flag:
        return True
    if reveal == "after_votes":
        threshold = block.get("reveal_after_votes")
        if not isinstance(threshold, int):
            threshold = DEFAULT_POLL_REVEAL_VOTES
        return votes >= max(threshold, 1)
    if reveal == "after_seconds":
        if first_vote_at is None:
            return False
        delay = block.get("reveal_after_seconds") or DEFAULT_POLL_REVEAL_SECONDS
        if first_vote_at.tzinfo is None:
            first_vote_at = first_vote_at.replace(tzinfo=UTC)
        return ((now or datetime.now(UTC)) - first_vote_at).total_seconds() >= delay
    return False


def _question_results(question: dict[str, Any], values: list[str]) -> dict[str, Any]:
    kind = question.get("kind", "choice")
    total = len(values)
    result: dict[str, Any] = {"id": question.get("id"), "kind": kind, "total": total}
    if kind in ("choice", "quiz"):
        counted = Counter(values)
        result["counts"] = [counted.get(str(index), 0) for index in range(len(question.get("options") or []))]
    elif kind == "rating":
        counted = Counter(values)
        result["counts"] = [counted.get(str(score), 0) for score in RATING_SCALE]
        result["average"] = round(sum(int(v) for v in values) / total, 2) if total else None
    else:
        words: Counter[str] = Counter()
        for value in values:
            words.update(value.split(" "))
        result["words"] = [{"text": text, "count": count} for text, count in words.most_common(40)]
    return result


async def poll_state(db: AsyncSession, poll: PresentationPoll, block: dict[str, Any], *, audience: bool) -> dict[str, Any]:
    """What the slide (``audience=False``) or the audience page (``True``) is told about a poll.

    The audience gets the open question (every question of a ``layout: "all"``
    poll) and, when the block shows results live or voting is locked, its
    results; the quiz answer only once revealed. Each quiz question carries its
    own ``revealed`` (``question_revealed``); the poll-level ``revealed`` stays
    the presenter's reveal (or ``reveal: live``) the controls toggle.
    """
    every = opens_every_question(block)
    reveal = block.get("reveal") or "manual"
    # ``reveal: live`` (xtm-one #10315): the quiz answer is marked from the start, no reveal step.
    revealed = reveal != "never" and (poll.revealed or reveal == "live")
    values = await _counts(db, poll.id)
    first_votes = await _first_votes(db, poll.id) if reveal == "after_seconds" else {}
    voters = len({v for v in (await db.execute(
        select(PresentationPollVote.voter_hash).where(PresentationPollVote.poll_id == poll.id)
    )).scalars()})
    show = block.get("show_results", "live") == "live" or poll.locked
    questions = []
    for question in block.get("questions") or []:
        if not isinstance(question, dict):
            continue
        if audience and not every and question.get("id") != poll.active_question:
            continue
        entry: dict[str, Any] = {
            "id": question.get("id"),
            "kind": question.get("kind", "choice"),
            "prompt": question.get("prompt", ""),
            "options": list(question.get("options") or []),
            "max_words": int(question.get("max_words") or 1),
        }
        if question.get("kind") == "quiz":
            votes = len(values.get(question.get("id"), []))
            first_vote_at = first_votes.get(question.get("id"))
            shown = question_revealed(block, flag=poll.revealed, votes=votes, first_vote_at=first_vote_at)
            entry["revealed"] = shown
            if reveal in ("after_votes", "after_seconds"):
                # The condition alone (UX round 1 B1 of #10417): once met, the controls offer no Reveal or Hide.
                entry["condition_met"] = question_revealed(
                    block, flag=False, votes=votes, first_vote_at=first_vote_at
                )
            if shown or not audience:
                entry["correct"] = question.get("correct")
        if not audience or show:
            entry["results"] = _question_results(question, values.get(question.get("id"), []))
        questions.append(entry)
    url = poll_url(poll.code)
    return {
        "poll_id": block["id"],
        "code": poll.code,
        "url": url,
        "qr": None if audience else poll_qr_svg(url),
        "layout": "all" if every else "active",
        # An ``all`` poll has no current question: nothing to mark or step to.
        "active_question": None if every else poll.active_question,
        "locked": poll.locked,
        "revealed": revealed,
        "show_results": block.get("show_results", "live"),
        "voters": voters,
        "max_voters": MAX_VOTERS_PER_POLL,
        "questions": questions,
    }


async def deck_poll_states(db: AsyncSession, presentation: Presentation) -> list[dict[str, Any]]:
    """The live state of every poll of a deck, for its viewers and the agent tool."""
    rows = await ensure_polls(db, presentation)
    return [
        await poll_state(db, rows[block["id"]], block, audience=False)
        for block in poll_blocks(presentation.spec)
        if block["id"] in rows
    ]


CONTROL_ACTIONS = ("open", "next", "lock", "unlock", "reveal", "hide", "reset")
# Switching a poll's mode (xtm-one #10235) rewrites its block's layout in the deck spec;
# votes hang on the poll's key, so they are kept.
MODE_ACTIONS = ("guided", "autonomous")


def spec_with_poll_mode(spec: dict[str, Any], poll_key: str, mode: str) -> dict[str, Any] | None:
    """A copy of *spec* with poll *poll_key* in *mode*; None when that block is already in it.

    ValueError on an unknown mode or a poll the deck does not hold.
    """
    from copy import deepcopy

    from app.services.presentation_spec import POLL_MODE_LAYOUTS

    if mode not in POLL_MODE_LAYOUTS:
        raise ValueError(f"Unknown poll mode {mode!r}; guided or autonomous.")
    patched = deepcopy(spec)
    block = next((b for b in poll_blocks(patched) if b["id"] == poll_key), None)
    if block is None:
        raise ValueError(f"This deck has no poll {poll_key!r}.")
    layout = POLL_MODE_LAYOUTS[mode]
    if block.get("layout", "active") == layout:
        return None
    block["layout"] = layout
    return patched


async def control_poll(
    db: AsyncSession, presentation: Presentation, poll_key: str, action: str, question_key: str | None = None
) -> dict[str, Any]:
    """Apply a presenter action to one poll and return its new state; ValueError on a bad call."""
    rows = await ensure_polls(db, presentation)
    block = next((b for b in poll_blocks(presentation.spec) if b["id"] == poll_key), None)
    poll = rows.get(poll_key)
    if block is None or poll is None:
        raise ValueError(f"This deck has no poll {poll_key!r}.")
    keys = [q.get("id") for q in block.get("questions") or [] if isinstance(q, dict)]
    if action in ("open", "next") and opens_every_question(block):
        raise PollNoopError(
            f"Poll {poll_key!r} has layout all: every question is already open to the audience, "
            "there is no question to step to (lock, unlock, reveal, hide and reset still apply)."
        )
    if action == "open":
        if question_key not in keys:
            raise ValueError(f"Poll {poll_key!r} has no question {question_key!r}.")
        poll.active_question, poll.locked, poll.revealed = question_key, False, False
    elif action == "next":
        at = keys.index(poll.active_question) if poll.active_question in keys else -1
        poll.active_question = keys[min(at + 1, len(keys) - 1)]
        poll.locked, poll.revealed = False, False
    elif action in ("lock", "unlock"):
        poll.locked = action == "lock"
    elif action in ("reveal", "hide"):
        poll.revealed = action == "reveal"
    elif action == "reset":
        await db.execute(delete(PresentationPollVote).where(PresentationPollVote.poll_id == poll.id))
        poll.active_question, poll.locked, poll.revealed = (keys[0] if keys else None), False, False
    else:
        raise ValueError(f"Unknown poll action {action!r}; one of {', '.join(CONTROL_ACTIONS)}.")
    await db.flush()
    return await poll_state(db, poll, block, audience=False)
