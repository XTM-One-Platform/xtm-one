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

"""Reactions as feedback: an emoji put on an agent's answer is a rating of it.

One module owns the rule, whatever the platform:

* **the setting** - ``ReactionFeedbackConfig``: on or off, which emojis read
  as positive and which as negative (``DEFAULT_CONFIG`` when an account never
  set it), validated here for every editor;
* **the emoji** - ``normalize_emoji``: exactly one emoji (``👍👎`` is
  refused), one spelling per emoji, the skin tone
  and presentation selectors dropped, so a thumbs up of any skin tone
  counts as the thumbs up. A provider turns its own reaction names into
  Unicode before calling it;
* **the rater** - ``rater_ref``: a keyed digest of the platform user id, the
  only trace of a person who may have no XTM One account;
* **the rating** - ``rating_of``: a rater's whole current set of emojis on an
  answer gives one rating, or none (nothing mapped, or both signs at once);
* **the write** - ``apply_reactions``: every rater's current set on one answer
  in, the stored ratings brought in line (created, changed, retracted).

A provider always hands the FULL current reactions of the answer, never a
delta: a repeated or late notification then rewrites the same state instead
of adding to it.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, get_args

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.annotations import AnnotationTarget

logger = logging.getLogger(__name__)

#: Where a rating was given: the web (the thumbs), or the platform a reaction came from.
Source = Literal["web", "teams_agent_account"]
SOURCES: tuple[str, ...] = get_args(Source)

#: A reaction rates an answer this long after the answer was posted.
REACTION_WINDOW = timedelta(days=14)
#: Emojis one sign of the setting may hold.
MAX_EMOJIS_PER_SIGN = 10
#: Raters stored per answer: a message seen by a whole company is still one answer.
MAX_RATERS_PER_TARGET = 500
#: Longest normalized emoji kept (a ZWJ family sequence is several code points).
MAX_EMOJI_CHARS = 16

DEFAULT_POSITIVE: tuple[str, ...] = ("\U0001f44d",)  # thumbs up
DEFAULT_NEGATIVE: tuple[str, ...] = ("\U0001f44e",)  # thumbs down

#: Code points that only change how an emoji looks: the presentation selectors
#: and the five Fitzpatrick skin tones.
_VARIANTS = frozenset({0xFE0E, 0xFE0F, *range(0x1F3FB, 0x1F400)})
_ZWJ = 0x200D
_KEYCAP = 0x20E3
_TAG_RANGE = range(0xE0020, 0xE0080)
#: Regional indicators: two of them in a row are one flag.
_REGIONAL_INDICATORS = range(0x1F1E6, 0x1F200)


def normalize_emoji(value: object) -> str | None:
    """*value* as one emoji, spelled one way; ``None`` when it is not one emoji.

    NFC, then the skin tones and presentation selectors removed. What is left
    must be pictographic: symbols (``So``), joiners, keycaps and tag
    characters (flags of subdivisions), nothing else. Text, digits on their
    own and whitespace are refused, so a setting never maps a word.

    It must also be exactly ONE emoji: a symbol starts a new one unless a
    joiner (ZWJ sequence, ``👨‍💻``) or a pending regional indicator (the
    second half of a flag, ``🇫🇷``) ties it to the previous one, so ``👍👎``
    or ``🔁🔁🔁`` is refused.
    """
    if not isinstance(value, str):
        return None
    text = "".join(ch for ch in unicodedata.normalize("NFC", value.strip()) if ord(ch) not in _VARIANTS)
    if not text or len(text) > MAX_EMOJI_CHARS:
        return None
    emojis = 0
    joined = False  # the previous character is a ZWJ: the next symbol extends the emoji
    lone_indicator = False  # the previous symbol is a regional indicator waiting for its pair
    for ch in text:
        code = ord(ch)
        if unicodedata.category(ch) == "So":
            if lone_indicator and code in _REGIONAL_INDICATORS:
                lone_indicator = False
            else:
                if not joined:
                    emojis += 1
                lone_indicator = code in _REGIONAL_INDICATORS
            joined = False
        elif code == _ZWJ:
            joined = True
            lone_indicator = False
        elif code == _KEYCAP or code in _TAG_RANGE:
            continue
        else:
            return None
    return text if emojis == 1 else None


def _emoji_list(values: object) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("Expected a list of emojis.")
    out: list[str] = []
    for value in values:
        emoji = normalize_emoji(value)
        if emoji is None:
            raise ValueError(f"{str(value)[:20]!r} is not an emoji.")
        if emoji not in out:
            out.append(emoji)
    if len(out) > MAX_EMOJIS_PER_SIGN:
        raise ValueError(f"At most {MAX_EMOJIS_PER_SIGN} emojis per sign.")
    return out


class ReactionFeedbackConfig(BaseModel):
    """What an account's reactions mean. Validated (422) wherever it is written."""

    enabled: bool = True
    positive: list[str] = Field(default_factory=lambda: list(DEFAULT_POSITIVE))
    negative: list[str] = Field(default_factory=lambda: list(DEFAULT_NEGATIVE))

    @field_validator("positive", "negative", mode="before")
    @classmethod
    def _normalized(cls, values: object) -> list[str]:
        return _emoji_list(values)

    @model_validator(mode="after")
    def _disjoint(self) -> ReactionFeedbackConfig:
        both = set(self.positive) & set(self.negative)
        if both:
            raise ValueError(f"{sorted(both)[0]} cannot be both positive and negative.")
        return self


DEFAULT_CONFIG: dict[str, Any] = ReactionFeedbackConfig().model_dump()


def effective_config(stored: object) -> ReactionFeedbackConfig:
    """The setting an account holds: the defaults when it never set one.

    A stored value was validated when written; one that no longer validates
    (an older replica's, a hand edit) reads as switched off rather than
    guessed at.
    """
    if stored is None:
        return ReactionFeedbackConfig()
    try:
        return ReactionFeedbackConfig.model_validate(stored)
    except ValidationError:
        logger.warning("An unreadable reaction feedback setting reads as off")
        return ReactionFeedbackConfig(enabled=False)


def rating_of(config: ReactionFeedbackConfig, emojis: Iterable[str]) -> tuple[str, str] | None:
    """``(rating, emoji)`` the rater's current *emojis* give, or ``None``.

    ``None`` when none is mapped, or when the rater holds a positive and a
    negative one at once: an answer is not both. The emoji kept is the
    setting's first one the rater used, so the same set always stores the same.
    """
    held = set(emojis)
    positive = next((emoji for emoji in config.positive if emoji in held), None)
    negative = next((emoji for emoji in config.negative if emoji in held), None)
    if positive and negative:
        return None
    if positive:
        return "positive", positive
    if negative:
        return "negative", negative
    return None


def _rater_key() -> bytes:
    from app.core.config import settings

    return hmac.new(settings.secret_key.encode(), b"xtm-one reaction rater v1", hashlib.sha256).digest()


def rater_ref(platform: str, platform_user_id: str) -> str:
    """The stored identity of a platform rater: HMAC-SHA256 of ``platform:id``, hex.

    Keyed with ``SECRET_KEY`` (domain-separated), so the platform id cannot be
    recovered by hashing candidate ids. Stable across replicas and restarts,
    which one rating per rater needs; a rotated ``SECRET_KEY`` makes a rater
    who reacts again a new one.
    """
    return hmac.new(_rater_key(), f"{platform}:{platform_user_id}".encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class PlatformReaction:
    """One reaction as a provider read it: who (platform id) and which emoji (Unicode)."""

    user_id: str
    emoji: str


@dataclass(frozen=True)
class ReactionOutcome:
    """What one ``apply_reactions`` call changed."""

    upserted: int = 0
    retracted: int = 0


def within_window(posted_at: datetime | None, now: datetime | None = None) -> bool:
    """Whether an answer posted at *posted_at* still takes reactions."""
    if posted_at is None:
        return False
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=UTC)
    return (now or datetime.now(UTC)) - posted_at <= REACTION_WINDOW


def ratings_by_rater(
    config: ReactionFeedbackConfig,
    reactions: Sequence[PlatformReaction],
    *,
    platform: str,
    exclude_user_ids: Iterable[str] = (),
) -> dict[str, tuple[str, str]]:
    """``{rater_ref: (rating, emoji)}`` from an answer's current *reactions*.

    The reactions of *exclude_user_ids* (the agent's own identity) are not
    ratings. At most ``MAX_RATERS_PER_TARGET`` raters who give a rating, the
    smallest ``rater_ref`` first: the order a provider lists reactions in is
    not stable, so a cap taken in that order would keep other raters on every
    read and churn the stored ratings.
    """
    excluded = {str(user_id) for user_id in exclude_user_ids if user_id}
    held: dict[str, set[str]] = {}
    for reaction in reactions:
        if not reaction.user_id or reaction.user_id in excluded:
            continue
        emoji = normalize_emoji(reaction.emoji)
        if emoji is None:
            continue
        held.setdefault(reaction.user_id, set()).add(emoji)
    rated: dict[str, tuple[str, str]] = {}
    for user_id, emojis in held.items():
        rating = rating_of(config, emojis)
        if rating is not None:
            rated[rater_ref(platform, user_id)] = rating
    return {ref: rated[ref] for ref in sorted(rated)[:MAX_RATERS_PER_TARGET]}


async def apply_reactions(
    db: AsyncSession,
    target: AnnotationTarget,
    ratings: dict[str, tuple[str, str]],
    *,
    source: str,
) -> ReactionOutcome:
    """Make the platform ratings of *target* exactly *ratings* (``ratings_by_rater``).

    A rater whose rating or emoji changed is updated, a new one inserted, one
    no longer in *ratings* (reaction removed, or both signs now) retracted.
    Serialized per target under a transaction-scoped advisory lock, so two
    notifications of the same answer never interleave their writes. Commits.
    """
    from sqlalchemy import text

    from app.services import annotations as annotation_service

    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"reaction_feedback:{target.target_type}:{target.target_id}"},
    )
    stored = await annotation_service.platform_ratings(db, target)
    upserted = 0
    for ref, (rating, emoji) in ratings.items():
        current = stored.get(ref)
        if current is not None and current.rating == rating and current.emoji == emoji:
            continue
        await annotation_service.upsert_platform_rating(
            db, target, rater_ref=ref, source=source, rating=rating, emoji=emoji
        )
        upserted += 1
    gone = [ref for ref in stored if ref not in ratings]
    retracted = await annotation_service.delete_platform_ratings(db, target, gone) if gone else 0
    await db.commit()
    return ReactionOutcome(upserted=upserted, retracted=retracted)


def answer_target(reply_id: uuid.UUID, agent_id: uuid.UUID) -> AnnotationTarget:
    """The rating target of an agent account's answer."""
    from app.services.annotations import AnnotationTarget

    return AnnotationTarget("agent_account_reply", reply_id, agent_id)
