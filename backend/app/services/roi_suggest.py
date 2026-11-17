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

"""Suggested unit of work for a custom agent (#9836, slice R8a, design 10.3).

A custom agent counts nothing until someone declares a unit. This module
proposes one from the catalog by a deterministic token match of the agent's
name, description and output property names against each unit's key, name and
keywords: lower-case words, a light suffix strip ("triaged" and "triage" meet
on "triag"), stop words removed. No model call, no run: the same agent and
catalog always answer the same suggestion.

Score = share of the unit's terms the agent's text carries (0 to 1). The best
score wins, a tie goes to the earlier unit in catalog order, and a score under
:data:`SUGGEST_MIN_SCORE` (or fewer than :data:`SUGGEST_MIN_TERMS` terms in
common, every term for a one-term unit) answers ``None``: no suggestion beats
a wrong one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: The lowest score that still suggests a unit.
SUGGEST_MIN_SCORE = 0.5
#: The fewest terms an agent and a unit must share for a suggestion.
SUGGEST_MIN_TERMS = 2

_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "by",
        "each",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "one",
        "or",
        "the",
        "this",
        "that",
        "to",
        "with",
        "your",
        "about",
        "agent",
        "batch",
        "built",
        "new",
        "per",
        "using",
    }
)

# Longest first: one strip per word.
_SUFFIXES = ("ations", "ation", "ings", "ing", "ied", "ies", "ed", "es", "er", "s", "e")
_MIN_STEM = 3

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORD = re.compile(r"[a-z0-9]+")


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= _MIN_STEM:
            return word[: -len(suffix)]
    return word


def terms(text: str | None) -> frozenset[str]:
    """The match terms of *text*: words lower-cased, stemmed, stop words and short words dropped."""
    if not text:
        return frozenset()
    words = _WORD.findall(_CAMEL.sub(" ", text).lower())
    return frozenset(_stem(w) for w in words if w not in _STOP_WORDS and len(w) >= _MIN_STEM and not w.isdigit())


def output_property_names(schema: Mapping[str, Any] | None) -> list[str]:
    """Every property name of a JSON Schema, nested objects and array items included."""
    names: list[str] = []

    def walk(node: Any) -> None:
        if not isinstance(node, Mapping):
            return
        props = node.get("properties")
        if isinstance(props, Mapping):
            for key, child in props.items():
                names.append(str(key))
                walk(child)
        walk(node.get("items"))

    walk(schema)
    return names


@dataclass(frozen=True)
class CatalogUnit:
    """One unit an agent may declare, as the suggestion reads it."""

    key: str
    name: str
    builtin: bool = True
    keywords: tuple[str, ...] = ()
    unit_terms: frozenset[str] = field(init=False)

    def __post_init__(self) -> None:
        text = " ".join((self.key.replace("_", " "), self.name, *self.keywords))
        object.__setattr__(self, "unit_terms", terms(text))


@dataclass(frozen=True)
class Suggestion:
    """The suggested unit, its score and the terms the agent and the unit share."""

    unit: CatalogUnit
    score: float
    matched_terms: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "unit": self.unit.key,
            "name": self.unit.name,
            "builtin": self.unit.builtin,
            "score": self.score,
            "matched_terms": list(self.matched_terms),
        }


def suggest_unit(
    name: str | None,
    description: str | None,
    output_schema: Mapping[str, Any] | None,
    catalog: Sequence[CatalogUnit],
) -> Suggestion | None:
    """The best catalog unit for an agent, ``None`` when none clears the threshold."""
    agent_terms = terms(name) | terms(description) | terms(" ".join(output_property_names(output_schema)))
    if not agent_terms:
        return None
    best: Suggestion | None = None
    for unit in catalog:
        if not unit.unit_terms:
            continue
        shared = unit.unit_terms & agent_terms
        if len(shared) < min(SUGGEST_MIN_TERMS, len(unit.unit_terms)):
            continue
        score = round(len(shared) / len(unit.unit_terms), 3)
        if score < SUGGEST_MIN_SCORE:
            continue
        if best is None or score > best.score:  # strict: a tie keeps the earlier unit
            best = Suggestion(unit, score, tuple(sorted(shared)))
    return best


def catalog_of(builtins: Iterable[Any], custom: Iterable[Any]) -> list[CatalogUnit]:
    """The catalog in order: built-in baselines (``unit``, ``name``), then custom units not archived (``key``, ``name``)."""
    units = [CatalogUnit(key=b.unit, name=b.name) for b in builtins]
    units += [CatalogUnit(key=u.key, name=u.name, builtin=False) for u in custom if u.archived_at is None]
    return units
