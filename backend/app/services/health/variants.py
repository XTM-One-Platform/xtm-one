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

"""Variants: the targeted change a Platform Health experiment measures (#8259).

The Quality Analyst writes the variant itself, in its agentic loop (invariant
7): there is no nested model call here, only the rules a variant must keep
before anything measures it, the #1044 rules generalized to the three texts a
proposal of level 6 may change:

* the text changes (an unchanged variant measures nothing);
* it grows by half at most, or by 2,000 characters for a short text
  (``persona_improvement.rewrite_length_limit``): targeted changes, no rule the
  evidence does not call for (invariant 16 - the persona and the skills are
  sent on every call, the prompt on every run);
* it keeps the base's template placeholders (``{{trigger.*}}``,
  ``{{var_id.*}}``, ...): the persona and the assignment prompt are rendered
  with the run's variables, and a variant dropping one would measure a
  different input, not a better text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.persona_improvement import MAX_PERSONA_CHARS, rewrite_length_limit

#: The changes a text variant makes (the flow changes of level 7 are structured, not text).
TEXT_CHANGE_TYPES = ("persona", "skill", "assignment_prompt")
#: The longest text a variant handles, whatever it changes: the persona cap of #1044.
MAX_VARIANT_CHARS = MAX_PERSONA_CHARS
MAX_SUMMARY_CHARS = 2_000

_PLACEHOLDER = re.compile(r"\{\{(.+?)\}\}")


class VariantError(ValueError):
    """A variant the rules refuse, with a message the analyst may read."""


@dataclass(frozen=True, slots=True)
class Variant:
    text: str
    summary: str


def placeholders(text: str) -> frozenset[str]:
    """The template placeholders of *text*, by their trimmed path."""
    return frozenset(match.strip() for match in _PLACEHOLDER.findall(text or ""))


def check_variant(change_type: str, base: str, text: str, summary: str) -> Variant:
    """The variant of *base* as stored, or ``VariantError`` saying which rule it breaks."""
    if change_type not in TEXT_CHANGE_TYPES:
        raise VariantError(f"change_type must be one of: {', '.join(TEXT_CHANGE_TYPES)}.")
    base = (base or "").strip()
    candidate = (text or "").replace("\r\n", "\n").strip()
    if not candidate:
        raise VariantError("The variant is empty.")
    if len(base) > MAX_VARIANT_CHARS:
        raise VariantError(f"The current text is longer than the {MAX_VARIANT_CHARS:,} characters a variant handles.")
    if candidate == base:
        raise VariantError("The variant is the current text unchanged.")
    limit = rewrite_length_limit(base, max_chars=MAX_VARIANT_CHARS)
    if len(candidate) > limit:
        raise VariantError(
            f"The variant grows the text from {len(base):,} to {len(candidate):,} characters "
            f"(at most {limit:,}): make targeted changes only."
        )
    missing = placeholders(base) - placeholders(candidate)
    added = placeholders(candidate) - placeholders(base)
    if missing or added:
        raise VariantError(
            "The variant must keep the template placeholders of the current text, no more and no fewer."
        )
    clean_summary = (summary or "").strip()
    if not clean_summary:
        raise VariantError("summary must say what the variant changes and which evidence calls for it.")
    if len(clean_summary) > MAX_SUMMARY_CHARS:
        clean_summary = clean_summary[:MAX_SUMMARY_CHARS].rstrip() + " [...]"
    return Variant(text=candidate, summary=clean_summary)
