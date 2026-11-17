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

"""The template's cards shortened to fit a play-once track.

A video a second too long for its play-once track was lengthened by its template's cards: the default outro (5.5 s)
and intro (3.7 s) sit above the least their words take to read (4.9 s, 3.1 s - ``quality.card_leasts``), applied only
once the author set ``template_scenes.<card>.duration``. When ``music.loop`` is false and the track ends before the
video, :func:`shortened` sets each card the template adds whose duration the author left unset to that least - the
outro first, then the intro, only as many as the track needs -, measured on the video as the save planned it
(``quality.measure_document``); ``projects.save_spec`` plans the video again with them and keeps them when it came out
shorter, and the save says which card it shortened, from what to what (:meth:`Shortening.words`). The duration set is
the spec's from then on: the author's to change.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: A track ending this close to the video's end is the video's (the save's ``music_short`` tolerance).
TOLERANCE_SECONDS = 0.5
#: The cards shortened, in turn: the closing card first - the track's own ending lands under it.
ORDER = ("outro", "intro")


@dataclass(frozen=True)
class Shortening:
    """The cards :func:`shortened` sets to their least, and the spec that sets them."""

    authored: dict
    #: The ``quality.CardLeast`` of each card shortened.
    cards: tuple
    playable: float
    before: float

    def words(self, after: float) -> str:
        """``The music plays once (loop: false) and ends at 170.2 s of the 171.4 s video: the template's outro
        shortened from 5.5 s to its least, 4.9 s, for its 9 words (template_scenes.outro.duration: 4.9) - the video now
        lasts 170.6 s. ...``."""
        cards = "; ".join(
            f"the template's {card.card} shortened from {round(card.lasts, 1):g} s to its least, {card.least:g} s, for "
            f"its {card.words} words (template_scenes.{card.card}.duration: {card.least:g})"
            for card in self.cards
        )
        said = (
            f"The music plays once (loop: false) and ends at {self.playable:.1f} s of the {self.before:.1f} s video: "
            f"{cards} - the video now lasts {after:.1f} s"
        )
        if after - self.playable > TOLERANCE_SECONDS:
            said += f", still {after - self.playable:.1f} s past the track"
        return said + ". Set another duration there to keep a card longer."


async def shortened(db: Any, authored: dict, applied: Any, placed: Any) -> Shortening | None:
    """The cards of *applied* (the template applied to *authored*) to set to their least for the play-once track to
    cover the video *placed* plans (``projects.MeasuredPlan``); ``None`` when nothing is to be shortened - music that
    loops, covers the video or is still being composed, no card the template adds left unset, or cards that cannot be
    measured (no headless browser here)."""
    from app.services.video_studio import music as music_tracks
    from app.services.video_studio import quality
    from app.services.video_studio.spec import parse_video_spec
    from app.services.video_studio.templates import TEMPLATE_CARDS

    added = set(getattr(applied, "added", None) or [])
    if not added:
        return None
    music = parse_video_spec(applied.spec).music
    if music is None or not music.enabled or music.loop or music.source is None:
        return None
    written = authored.get("template_scenes") if isinstance(authored.get("template_scenes"), dict) else {}
    types = {scene.get("type") for scene in applied.spec.get("scenes") or [] if scene.get("id") in added}
    unset = [
        card
        for card in ORDER
        if TEMPLATE_CARDS[card] in types
        and not (isinstance(written.get(card), dict) and written[card].get("duration") is not None)
    ]
    if not unset:
        return None
    measured = await music_tracks.playable_seconds(db, music)
    if measured is None or measured[1]:
        return None
    playable, before = measured[0], float(placed.duration)
    if before - playable <= TOLERANCE_SECONDS:
        return None
    document = placed.plan.document
    try:
        audit = await asyncio.wait_for(quality.measure_document(document), timeout=quality.QUALITY_CHECK_TIMEOUT_SECS)
    except Exception as exc:  # no headless browser, a crash: the save's music_short finding says the gap
        logger.info("Template cards not measured to fit the music: %s", format_exception_message(exc))
        return None
    leasts = {card.card: card for card in quality.card_leasts(audit, document, authored)}
    cards = {name: dict(value) for name, value in written.items() if isinstance(value, dict)}
    chosen = []
    left = before - playable
    for name in unset:
        if left <= TOLERANCE_SECONDS:
            break
        card = leasts.get(name)
        if card is None or card.lasts - card.least < 0.05:
            continue
        cards[name] = {**cards.get(name, {}), "duration": card.least}
        chosen.append(card)
        left -= card.lasts - card.least
    if not chosen:
        return None
    return Shortening({**authored, "template_scenes": cards}, tuple(chosen), playable, before)
