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

"""The platform's pronunciation lexicon: versioned, edited by an administrator, seeded once.

Every save adds a version (``VideoPronunciationLexicon``) and never updates
one in place; a restore adds a version holding an older one's entries. The
newest is what :func:`current` reads and :func:`for_voiceover` merges under a
video's own ``voiceover.pronunciations`` (``lexicon.merge``: a video entry
overrides the platform's for the same written form). When the platform has
no version yet, :func:`seed` writes the first from
``data/pronunciation_lexicon.json`` (``lexicon.load_seed``, validated against
its schema), so an administrator's edits are never written over by a seed.
One exception, a version written alongside: a seeded term the newest
version holds without the data file's IPA, read as the data file reads it or
as its bare written form (:func:`seed_readings_lost`), tells the voice nothing
the brand owner confirmed - startup adds a version giving it the data file's
entry. A reading an administrator changed is theirs, and stays.

The newest version is read on every use (one indexed row): a save is heard by
every replica's next preview and render at once, and the editor's preview and
the render read the same lexicon, so they ask the voice cache for the same
narrations.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.utils import format_exception_message
from app.services.video_studio.lexicon import MAX_ENTRIES, Entry, Lexicon, entry_of, load_seed, merge, same_form

logger = logging.getLogger(__name__)

SEED_NOTE = "Seeded from the platform's data file."
#: The note of a version that gave seeded terms their data file's reading back (:func:`seed_readings_lost`).
SEED_READINGS_NOTE = "The platform's data file's readings given back to {terms}: the version before held them without their IPA."
#: How many times a save racing another for the next version number tries again.
_SAVE_TRIES = 3
_PARSED: dict[tuple[int, str], tuple[Entry, ...]] = {}


class LexiconError(ValueError):
    """Entries a lexicon cannot hold; ``str()`` says which and why."""


@dataclass(frozen=True)
class PlatformLexicon:
    """One version of the platform's lexicon, as read."""

    version: int | None
    entries: tuple[Entry, ...] = ()
    note: str | None = None
    created_at: datetime | None = None
    created_by: str | None = None
    #: The version's row: what its audit entry names.
    id: uuid.UUID | None = None

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "entries": [entry.as_dict() for entry in self.entries],
            "note": self.note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "created_by": self.created_by,
        }


def _of(row: Any) -> PlatformLexicon:
    key = (int(row.version), str(row.id))
    entries = _PARSED.get(key)
    if entries is None:
        entries = tuple(e for e in (entry_of(item) for item in row.entries or [] if isinstance(item, dict)) if e.text and e.say)
        _PARSED[key] = entries
    return PlatformLexicon(row.version, entries, row.note, row.created_at, row.created_by_label, row.id)


async def current(db: Any) -> PlatformLexicon:
    """The newest version of the platform's lexicon; version ``None`` (no entries) before the first."""
    from app.models.video_studio import VideoPronunciationLexicon as Row

    row = (await db.execute(select(Row).order_by(Row.version.desc()).limit(1))).scalars().first()
    return _of(row) if row is not None else PlatformLexicon(None)


async def for_voiceover(db: Any, voiceover: Any) -> Lexicon:
    """The lexicon a video's voice-over is said with: its own entries over the platform's newest version.

    A platform lexicon that cannot be read leaves the video's own entries: a
    voice-over is never refused for it.
    """
    try:
        platform = await current(db)
    except Exception as exc:
        logger.warning("The platform's pronunciation lexicon was not read: %s", format_exception_message(exc))
        platform = PlatformLexicon(None)
    return merge(platform.entries, getattr(voiceover, "pronunciations", None) or (), version=platform.version)


def validated(entries: Iterable[Any]) -> list[dict]:
    """*entries* as stored: each one a valid ``spec.Pronunciation``, one per written form (:class:`LexiconError`)."""
    from pydantic import ValidationError

    from app.services.video_studio.spec import Pronunciation

    out: list[dict] = []
    kept: list[Entry] = []
    for index, item in enumerate(entries):
        try:
            stored = Pronunciation.model_validate(item).model_dump(exclude_none=True)
        except ValidationError as exc:
            problem = exc.errors()[0]
            where = ".".join(str(part) for part in problem.get("loc", ()))
            raise LexiconError(f"Entry {index + 1}{f' ({where})' if where else ''}: {problem.get('msg')}.") from None
        mine = entry_of(stored)
        for position, other in enumerate(kept):
            if same_form(other, mine):
                raise LexiconError(f'Entries {position + 1} and {index + 1} both write "{mine.text}": keep one.')
        kept.append(mine)
        out.append(stored)
    if len(out) > MAX_ENTRIES:
        raise LexiconError(f"A lexicon holds at most {MAX_ENTRIES} entries.")
    return out


async def save(db: Any, entries: Iterable[Any], *, user: Any, note: str | None = None) -> PlatformLexicon:
    """Add a version holding *entries* (validated, :class:`LexiconError`); flushed, the caller commits."""
    from app.models.video_studio import VideoPronunciationLexicon as Row

    stored = validated(entries)
    note = (note or "").strip()[:500] or None
    for attempt in range(_SAVE_TRIES):
        latest = await db.scalar(select(func.max(Row.version)))
        row = Row(
            version=int(latest or 0) + 1,
            entries=stored,
            note=note,
            created_by_user_id=getattr(user, "id", None),
            created_by_label=getattr(user, "email", None),
        )
        db.add(row)
        try:
            await db.flush()
        except IntegrityError:
            # Another save took the number: the next one.
            await db.rollback()
            if attempt + 1 == _SAVE_TRIES:
                raise
            continue
        return _of(row)
    raise RuntimeError("unreachable")


async def version(db: Any, number: int) -> PlatformLexicon | None:
    from app.models.video_studio import VideoPronunciationLexicon as Row

    row = (await db.execute(select(Row).where(Row.version == number))).scalars().first()
    return _of(row) if row is not None else None


async def restore(db: Any, number: int, *, user: Any) -> PlatformLexicon | None:
    """Add a version holding version *number*'s entries; ``None`` when there is no such version."""
    old = await version(db, number)
    if old is None:
        return None
    return await save(db, [e.as_dict() for e in old.entries], user=user, note=f"Restored version {number}.")


async def versions(db: Any, *, limit: int = 100) -> list[dict]:
    """The versions, newest first: number, when, by whom, its note and how many entries."""
    from app.models.video_studio import VideoPronunciationLexicon as Row

    rows = (await db.execute(select(Row).order_by(Row.version.desc()).limit(limit))).scalars().all()
    return [
        {
            "version": row.version,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "created_by": row.created_by_label,
            "note": row.note,
            "entries": len(row.entries or []),
        }
        for row in rows
    ]


def _bare(text: str) -> str:
    """A reading as written, its spaces and case aside: ``Fili-gran`` is a respelling of its own, not ``Filigran``."""
    return " ".join(text.split()).casefold()


def seed_readings_lost(entries: Sequence[Entry], seeded: Sequence[Entry]) -> list[Entry]:
    """*entries* with each seeded term given its data file's entry back where they lost its reading.

    A term is given back when *seeded* has an IPA for it and the entry for the
    same written form has none, while it reads the term as the data file does
    or as its bare written form (``Filigran`` said ``Filigran``): nothing an
    administrator chose. Empty when nothing is to be given back.
    """
    out, changed = list(entries), False
    for mine in seeded:
        if not mine.ipa:
            continue
        for index, theirs in enumerate(out):
            if not same_form(theirs, mine) or theirs.ipa or _bare(theirs.say) not in (_bare(mine.say), _bare(theirs.text)):
                continue
            out[index], changed = mine, True
    return out if changed else []


async def seed(db: Any) -> bool:
    """Write the first version from the platform's data file when there is none; whether it wrote a version.

    With versions already, one is added only when the newest lost a seeded
    reading (:func:`seed_readings_lost`), with :data:`SEED_READINGS_NOTE`.
    """
    from app.models.video_studio import VideoPronunciationLexicon as Row

    seeded = load_seed()
    if await db.scalar(select(Row.id).limit(1)) is None:
        db.add(Row(version=1, entries=[e.as_dict() for e in seeded], note=SEED_NOTE, created_by_label="system"))
    else:
        newest = await current(db)
        given = seed_readings_lost(newest.entries, seeded)
        if not given:
            return False
        terms = ", ".join(e.text for e in given if e not in newest.entries)
        db.add(
            Row(
                version=int(newest.version or 0) + 1,
                entries=[e.as_dict() for e in given],
                note=SEED_READINGS_NOTE.format(terms=terms)[:500],
                created_by_label="system",
            )
        )
    try:
        await db.commit()
    except IntegrityError:
        # Another replica wrote it first.
        await db.rollback()
        return False
    return True
