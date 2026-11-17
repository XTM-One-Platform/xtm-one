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

"""Placebo arms of an evaluation: their configuration and the placebo text.

A placebo-controlled evaluation (``dev-docs/agent-placebo-evaluation.md``) runs
an agent next to a copy of itself in which one target - its persona, its
attached Prompts, all its skills or one skill - is replaced by neutral text of
the same length, delivered through the same mechanism. When the agent does no
better than that copy, the target's instructions did not earn their place: any
text of that length would have done as well.

This module owns what needs no model run:

* the evaluation's placebo configuration (:class:`PlaceboConfig`), validated on
  every write path, and the execution-mode rule (:func:`placebo_mode_error`);
* the placebo itself (:func:`build_placebo`): neutral text drawn from the
  versioned corpus in ``app/evaluations/placebo_corpus/``, in the original's
  language, carrying no distinctive word of the names and descriptions it
  replaces, and matched in length by a :class:`LengthMeasurer`.

Selection is deterministic: the order in which passages, names and
descriptions are drawn is a hash of the seed, so the seed stored with a run
rebuilds the same placebo.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PLACEBO_CORPUS_DIR = Path(__file__).resolve().parents[1] / "evaluations" / "placebo_corpus"

#: The execution mode that runs the agentic loop. ``llm_only`` sends the
#: persona alone: no Prompt header and no ``load_skill`` tool reach the model.
FULL_LOOP_MODE = "full_loop"
#: Targets the model only sees through the agentic loop.
FULL_LOOP_TARGET_KINDS = frozenset({"prompts", "skills", "skill"})

DEFAULT_TRIALS_PER_ENTRY = 5
MAX_TRIALS_PER_ENTRY = 30
MAX_PLACEBO_TARGETS = 20
#: The corpus is read again from its start when an original outruns it, so a
#: custom text needs no more than this; it is served with every evaluation.
MAX_CUSTOM_TEXT_CHARS = 20_000

#: A placebo part is the original's length within this fraction.
LENGTH_TOLERANCE = 0.05
#: Rebuilds after the first measure, each rescaled by what the last one measured.
MAX_RESCALE_ROUNDS = 3
#: The language of a placebo whose original's language cannot be told.
DEFAULT_LANGUAGE = "en"

#: A word shorter than this never counts as distinctive of what a placebo replaces.
_MIN_DISTINCTIVE_WORD_LENGTH = 3
#: Function words a text must hold before its language is decided.
_MIN_LANGUAGE_HITS = 2
#: Distinctive words named in a refusal, at most.
_MAX_REPORTED_WORDS = 10
#: Names closest in length to the original among which the seed picks.
_NAME_CHOICES = 5

_LETTERS = re.compile(r"[^\W\d_]+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class PlaceboTarget(BaseModel):
    """What one placebo arm replaces."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["persona", "prompts", "skills", "skill"]
    skill_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _skill_id_names_a_skill_target_only(self) -> PlaceboTarget:
        if self.kind == "skill" and self.skill_id is None:
            raise ValueError("A 'skill' placebo target needs its skill_id")
        if self.kind != "skill" and self.skill_id is not None:
            raise ValueError(f"A '{self.kind}' placebo target takes no skill_id")
        return self

    @property
    def key(self) -> str:
        """One key per distinct target: two targets with one key are a duplicate."""
        return f"skill:{self.skill_id}" if self.kind == "skill" else self.kind


class PlaceboConfig(BaseModel):
    """An evaluation's placebo-controlled method (``evaluations.placebo_config``)."""

    model_config = ConfigDict(extra="forbid")

    targets: list[PlaceboTarget] = Field(min_length=1, max_length=MAX_PLACEBO_TARGETS)
    #: The arm with the target removed (``no_target``; "baseline" is the
    #: evaluation's reference agent). Off, only real vs placebo is measured.
    include_no_target: bool = True
    trials_per_entry: int = Field(DEFAULT_TRIALS_PER_ENTRY, ge=1, le=MAX_TRIALS_PER_ENTRY)
    custom_text: str | None = Field(None, max_length=MAX_CUSTOM_TEXT_CHARS)

    @field_validator("custom_text")
    @classmethod
    def _blank_custom_text_is_none(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return value.strip()

    @model_validator(mode="after")
    def _targets_are_distinct(self) -> PlaceboConfig:
        keys = [target.key for target in self.targets]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f"Duplicate placebo targets: {', '.join(duplicates)}")
        if "skills" in keys and any(target.kind == "skill" for target in self.targets):
            raise ValueError("A 'skills' placebo target already replaces every skill: drop the 'skill' targets")
        return self


def parse_placebo_config(raw: object) -> PlaceboConfig | None:
    """The configuration stored on an evaluation (or carried by a package); ``None`` when it has none.

    Raises ``pydantic.ValidationError`` on a configuration no write path accepts.
    """
    return PlaceboConfig.model_validate(raw) if raw else None


def imported_placebo_config(config: PlaceboConfig | None) -> tuple[PlaceboConfig | None, list[str]]:
    """What an imported evaluation keeps of the exported configuration, and the keys of what it drops.

    Importing an agent recreates its skills under new ids, so a ``skill``
    target names a skill of the exporting instance only: it is dropped, and a
    configuration left with no target is no configuration.
    """
    if config is None:
        return None, []
    targets = [target for target in config.targets if target.kind != "skill"]
    dropped = [target.key for target in config.targets if target.kind == "skill"]
    return (config.model_copy(update={"targets": targets}) if targets else None), dropped


def placebo_mode_error(config: PlaceboConfig | None, execution_mode: str) -> str | None:
    """Why ``config`` cannot run in ``execution_mode``, or ``None`` when it can."""
    if config is None or execution_mode == FULL_LOOP_MODE:
        return None
    loop_only = sorted({target.kind for target in config.targets} & FULL_LOOP_TARGET_KINDS)
    if not loop_only:
        return None
    return (
        f"Placebo targets {', '.join(loop_only)} need the '{FULL_LOOP_MODE}' execution mode: "
        f"in '{execution_mode}' the model sees the persona alone"
    )


# ---------------------------------------------------------------------------
# Corpus, language and distinctive words
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlaceboCorpus:
    """The neutral text of one language (one file of ``placebo_corpus/``)."""

    language: str
    #: Content hash of the file: any edit is a new version, recorded with each run.
    version: str
    #: Normalized (see :func:`normalized_words`): they decide the language and
    #: never count as distinctive.
    function_words: frozenset[str]
    names: tuple[str, ...]
    descriptions: tuple[str, ...]
    passages: tuple[str, ...]


@cache
def load_placebo_corpora() -> dict[str, PlaceboCorpus]:
    """Every corpus, by language code."""
    corpora: dict[str, PlaceboCorpus] = {}
    for path in sorted(PLACEBO_CORPUS_DIR.glob("*.json")):
        raw = path.read_bytes()
        data = json.loads(raw)
        corpora[data["language"]] = PlaceboCorpus(
            language=data["language"],
            version=f"sha256:{hashlib.sha256(raw).hexdigest()[:16]}",
            function_words=frozenset(word for entry in data["function_words"] for word in normalized_words(entry)),
            names=tuple(data["names"]),
            descriptions=tuple(data["descriptions"]),
            passages=tuple(data["passages"]),
        )
    return corpora


def normalized_words(text: str) -> list[str]:
    """The words of ``text``: letters only, case-folded, accents removed."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return _LETTERS.findall("".join(char for char in decomposed if not unicodedata.combining(char)))


def detect_language(text: str, corpora: dict[str, PlaceboCorpus]) -> str | None:
    """The corpus language whose function words ``text`` uses most; ``None`` when undecided.

    Undecided is fewer than :data:`_MIN_LANGUAGE_HITS` function words, or a tie.
    A text mixing languages takes the dominant one.
    """
    words = normalized_words(text)
    scores = sorted(
        ((sum(word in corpus.function_words for word in words), language) for language, corpus in corpora.items()),
        key=lambda score: (-score[0], score[1]),
    )
    if not scores or scores[0][0] < _MIN_LANGUAGE_HITS:
        return None
    if len(scores) > 1 and scores[1][0] == scores[0][0]:
        return None
    return scores[0][1]


def distinctive_words(texts: Iterable[str], corpora: dict[str, PlaceboCorpus]) -> frozenset[str]:
    """The words of ``texts`` a placebo must not carry: no function word, nothing shorter than 3 letters."""
    function_words = frozenset().union(*(corpus.function_words for corpus in corpora.values()))
    return frozenset(
        word
        for text in texts
        if text
        for word in normalized_words(text)
        if len(word) >= _MIN_DISTINCTIVE_WORD_LENGTH and word not in function_words
    )


class PlaceboLeakError(ValueError):
    """No placebo can be built without a word of what it replaces."""

    def __init__(self, kind: str, words: Iterable[str]):
        self.kind = kind
        self.words = sorted(words)[:_MAX_REPORTED_WORDS]
        super().__init__(
            f"No neutral placebo for the '{kind}' target: its text would carry "
            f"{', '.join(repr(word) for word in self.words) or 'a word of the original'}"
        )


# ---------------------------------------------------------------------------
# Length
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LengthMeasurer:
    """How long a text is for the model of the run: its tokens, or its characters."""

    unit: Literal["tokens", "chars"]
    measure: Callable[[str], Awaitable[int]]


async def _count_characters(text: str) -> int:
    return len(text)


#: The fallback when the provider reports no token usage (FR-6).
CHARACTER_MEASURER = LengthMeasurer("chars", _count_characters)


@dataclass(frozen=True)
class PartLength:
    """One part of a placebo next to the part it replaces, in the measurer's unit."""

    original: int
    placebo: int
    #: ``(placebo - original) / original``; ``None`` when the original is empty.
    deviation: float | None
    within_tolerance: bool
    #: The corpus ran out and was drawn again from its start.
    repeated: bool


def _seeded_order(items: Sequence[str], seed: str, purpose: str) -> list[str]:
    """``items`` in an order fixed by ``seed`` and ``purpose`` alone."""
    keyed = (
        (hashlib.sha256(f"{seed}\x1f{purpose}\x1f{index}".encode()).hexdigest(), item)
        for index, item in enumerate(items)
    )
    return [item for _, item in sorted(keyed)]


def _cut(pool: Sequence[str], char_target: int, separator: str) -> tuple[str, bool]:
    """About ``char_target`` characters of ``pool`` read in order, cut at a word.

    Whole sentences while they fit, then the words of the next one up to the
    word boundary closest to the target; a target shorter than the first word
    gets that word's first letters. The pool is read again from its start when
    it runs out (``repeated``).
    """
    if char_target <= 0 or not pool:
        return "", False
    text = ""
    index = 0
    while True:
        passage = pool[index % len(pool)]
        for position, sentence in enumerate(_SENTENCE_END.split(passage)):
            joiner = (separator if position == 0 else " ") if text else ""
            if len(text) + len(joiner) + len(sentence) <= char_target:
                text += joiner + sentence
                continue
            for word in sentence.split(" "):
                longer = len(text) + len(joiner) + len(word)
                if longer > char_target:
                    if text and longer - char_target < char_target - len(text):
                        text += joiner + word
                    break
                text += joiner + word
                joiner = " "
            if not text:
                text = sentence[:char_target]
            return text, index >= len(pool)
        index += 1


async def _match_length(
    original_text: str,
    pool: Sequence[str],
    measurer: LengthMeasurer,
    *,
    separator: str = "\n\n",
    compose: Callable[[str], str] = lambda body: body,
) -> tuple[str, PartLength]:
    """A body drawn from ``pool`` such that ``compose(body)`` measures as ``original_text``.

    The first cut takes the original's characters; each later one rescales the
    body by the ratio still missing, until the measure is within
    :data:`LENGTH_TOLERANCE` or :data:`MAX_RESCALE_ROUNDS` are spent. The
    closest attempt wins.
    """
    original = await measurer.measure(original_text) if original_text else 0
    fixed = await measurer.measure(compose("")) if compose("") else 0
    if original == 0:
        return "", PartLength(original=0, placebo=fixed, deviation=None, within_tolerance=fixed == 0, repeated=False)
    wanted = original - fixed
    if wanted <= 0:
        # The fixed part (a placebo name) is already as long as the whole original.
        body, measured, repeated = "", fixed, False
    else:
        char_target = max(len(original_text) - len(compose("")), 1)
        body, repeated = _cut(pool, char_target, separator)
        measured = await measurer.measure(compose(body))
        best = (body, measured, repeated)
        for _ in range(MAX_RESCALE_ROUNDS):
            if abs(best[1] - original) <= LENGTH_TOLERANCE * original:
                break
            body_units = measured - fixed
            next_target = round(len(body) * wanted / body_units) if body and body_units > 0 else char_target * 2
            if max(next_target, 1) == char_target:
                break
            char_target = max(next_target, 1)
            body, repeated = _cut(pool, char_target, separator)
            measured = await measurer.measure(compose(body))
            if abs(measured - original) < abs(best[1] - original):
                best = (body, measured, repeated)
        body, measured, repeated = best
    deviation = (measured - original) / original
    return body, PartLength(
        original=original,
        placebo=measured,
        deviation=round(deviation, 4),
        within_tolerance=abs(deviation) <= LENGTH_TOLERANCE,
        repeated=repeated,
    )


# ---------------------------------------------------------------------------
# The placebo
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlaceboSource:
    """One element a target replaces, as the model sees it."""

    content: str
    name: str = ""
    #: Every line the listing shows under the name (a skill's description and
    #: compatibility note, a Prompt's description).
    description: str = ""
    #: A skill whose body is inlined in the system prompt instead of loaded.
    always_load: bool = False
    ref_id: str | None = None
    version: int | None = None


@dataclass(frozen=True)
class PlaceboElement:
    """The placebo of one :class:`PlaceboSource`, with each part's length."""

    name: str
    description: str
    content: str
    always_load: bool
    ref_id: str | None
    version: int | None
    lengths: dict[str, PartLength]


@dataclass(frozen=True)
class PlaceboBuild:
    """The placebo of a target, and what is needed to rebuild and audit it."""

    kind: str
    elements: tuple[PlaceboElement, ...]
    language: str
    language_detected: bool
    corpus_version: str
    seed: str
    unit: str
    custom_text: bool
    warnings: tuple[str, ...]

    def snapshot(self) -> dict:
        """The JSON stored with a run (FR-30)."""
        return json.loads(json.dumps(asdict(self))) | {"leakage_check": "passed"}


def _listing(name: str, description: str) -> str:
    return f"{name}\n{description}" if description else name


def _pick_name(pool: Sequence[str], used: set[str], original: str) -> str:
    """An unused name close in length to ``original``.

    The first, in ``pool`` (seeded) order, of the :data:`_NAME_CHOICES` unused
    names closest in length: the seed picks among names of a fitting length
    rather than the length alone deciding.
    """
    free = [name for name in pool if name.casefold() not in used]
    if free:
        closest = set(sorted(free, key=lambda name: abs(len(name) - len(original)))[:_NAME_CHOICES])
        return next(name for name in free if name in closest)
    base = min(pool, key=lambda name: abs(len(name) - len(original)))
    suffix = 2
    while f"{base} {suffix}".casefold() in used:
        suffix += 1
    return f"{base} {suffix}"


def _without(pool: Iterable[str], forbidden: frozenset[str]) -> list[str]:
    return [text for text in pool if not forbidden.intersection(normalized_words(text))]


async def build_placebo(
    kind: str,
    sources: Sequence[PlaceboSource],
    *,
    agent_name: str,
    seed: str,
    measurer: LengthMeasurer,
    reserved_names: Iterable[str] = (),
    custom_text: str | None = None,
) -> PlaceboBuild:
    """The placebo of ``sources``, the elements one ``kind`` of target replaces.

    ``persona`` takes exactly one source (its ``content``); ``prompts``,
    ``skills`` and ``skill`` take one per Prompt or skill and give each a
    neutral name, a description matched with the name to the original listing,
    and a body matched to the original content. ``reserved_names`` are the
    agent's other Prompt and skill names, which no placebo name may take.

    Raises :class:`PlaceboLeakError` when every passage, description or name of
    the corpus - or the ``custom_text`` - carries a distinctive word of the
    agent name or of the replaced names and descriptions.
    """
    if kind not in {"persona", "prompts", "skills", "skill"}:
        raise ValueError(f"Unknown placebo target {kind!r}")
    if kind == "persona" and len(sources) != 1:
        raise ValueError("A persona placebo replaces exactly one persona")

    corpora = load_placebo_corpora()
    detected = detect_language(
        "\n".join(part for source in sources for part in (source.name, source.description, source.content) if part),
        corpora,
    )
    corpus = corpora[detected or DEFAULT_LANGUAGE]
    forbidden = distinctive_words(
        [agent_name, *(source.name for source in sources), *(source.description for source in sources)], corpora
    )

    if custom_text:
        leaked = forbidden.intersection(normalized_words(custom_text))
        if leaked:
            raise PlaceboLeakError(kind, leaked)
        bodies = [custom_text]
    else:
        bodies = _without(corpus.passages, forbidden)
    reserved = {name.casefold() for name in reserved_names}
    names = [name for name in _without(corpus.names, forbidden) if name.casefold() not in reserved]
    descriptions = _without(corpus.descriptions, forbidden)
    if not bodies or (kind != "persona" and not (names and descriptions)):
        corpus_words = {
            word for text in (*corpus.passages, *corpus.names, *corpus.descriptions) for word in normalized_words(text)
        }
        raise PlaceboLeakError(kind, forbidden & corpus_words)

    # Names first, in source order (each takes one off the pool); then every
    # part is measured at once - with a token probe, each measure is a call.
    names = _seeded_order(names, seed, "names")
    used: set[str] = set()
    picked: list[str] = []
    if kind != "persona":
        for source in sources:
            picked.append(_pick_name(names, used, source.name))
            used.add(picked[-1].casefold())
    contents = [
        _match_length(source.content, _seeded_order(bodies, seed, f"content:{index}"), measurer)
        for index, source in enumerate(sources)
    ]
    listings = [
        _match_length(
            _listing(source.name, source.description),
            _seeded_order(descriptions, seed, f"description:{index}"),
            measurer,
            separator=" ",
            compose=lambda body, name=name: _listing(name, body),
        )
        for index, (source, name) in enumerate(zip(sources, picked, strict=False))
    ]
    matched = await asyncio.gather(*contents, *listings)
    elements: list[PlaceboElement] = []
    for index, source in enumerate(sources):
        content, content_length = matched[index]
        if kind == "persona":
            elements.append(
                PlaceboElement(
                    name="", description="", content=content, always_load=False,
                    ref_id=source.ref_id, version=source.version, lengths={"content": content_length},
                )
            )
            continue
        description, listing_length = matched[len(sources) + index]
        elements.append(
            PlaceboElement(
                name=picked[index], description=description, content=content, always_load=source.always_load,
                ref_id=source.ref_id, version=source.version,
                lengths={"listing": listing_length, "content": content_length},
            )
        )

    # A cut word can end on a distinctive one ("attachments" cut to "attach").
    leaked = forbidden.intersection(
        word
        for element in elements
        for word in normalized_words(f"{element.name}\n{element.description}\n{element.content}")
    )
    if leaked:
        raise PlaceboLeakError(kind, leaked)

    lengths = [length for element in elements for length in element.lengths.values()]
    warnings = [
        code
        for code, raised in (
            ("language_undetermined", detected is None),
            ("corpus_repeated", any(length.repeated for length in lengths)),
            ("length_out_of_tolerance", not all(length.within_tolerance for length in lengths)),
        )
        if raised
    ]
    return PlaceboBuild(
        kind=kind,
        elements=tuple(elements),
        language=corpus.language,
        language_detected=detected is not None,
        corpus_version=corpus.version,
        seed=seed,
        unit=measurer.unit,
        custom_text=bool(custom_text),
        warnings=tuple(warnings),
    )
