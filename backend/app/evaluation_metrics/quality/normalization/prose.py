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

"""Typed, explicit normalization policy for the PROSE family.

Four options, applied in the fixed order Unicode form -> case -> punctuation ->
whitespace. Strict by default: only whitespace is collapsed, so no distinction is erased
unless the caller opts in. Unknown option values raise ``ConfigurationError``, never
become a score.

Three effects the option names do not convey:

- ``NFKC``/``NFKD`` perform *compatibility* folding, rewriting ligatures, superscripts,
  subscripts and full-width/half-width forms into base characters. ``NFC`` is the default
  so this never happens implicitly.
- ``punctuation="strip"`` covers every Unicode ``P*`` subcategory including connector
  punctuation ``Pc``, so ``snake_case`` becomes the two tokens ``snake`` and ``case``.
- ``punctuation="edges"`` trims only what presents a token: sentence punctuation at its
  end, brackets and quotes at either end, ``*`` and backtick emphasis. ``evil.com.``
  becomes ``evil.com`` and ``**APT29**`` becomes ``APT29``, while ``C#``, ``-5``, ``.NET``,
  ``*.evil.com`` and ``__init__`` stay whole. A token of punctuation alone disappears.

Normalization is these four stages and nothing else: no stemming, lemmatization,
stop-word removal, language detection, spelling correction, transliteration, or
number/date canonicalization.
"""

import unicodedata
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from app.evaluation_metrics.core.errors import ConfigurationError


class UnicodeForm(StrEnum):
    """Unicode normalization form applied first."""

    NFC = "NFC"
    NFD = "NFD"
    NFKC = "NFKC"
    NFKD = "NFKD"


class CaseMode(StrEnum):
    """How letter case is handled."""

    preserve = "preserve"
    # ``casefold`` and ``strip`` shadow ``str`` methods, which mypy flags. The names are
    # public option values, so the warning is silenced.
    casefold = "casefold"  # type: ignore[assignment]


class PunctuationMode(StrEnum):
    """How Unicode punctuation is handled."""

    preserve = "preserve"
    strip = "strip"  # type: ignore[assignment]
    edges = "edges"


class WhitespaceMode(StrEnum):
    """How Unicode whitespace runs are handled."""

    preserve = "preserve"
    collapse = "collapse"


_SPACE = "\u0020"

# ``edges`` trims what presents a token, never what identifies it:
#
# - sentence punctuation, at the end only: ``evil.com.`` is ``evil.com``, while ``.NET``,
#   ``::1`` and ``-5`` keep their first character, and ``fe80::`` its trailing ``::``;
# - quotes (Unicode ``Pi``, ``Pf`` and the ASCII quotes), at both ends, and brackets
#   (``Ps``, ``Pe``) unless they enclose part of the token: ``(T1059)`` is ``T1059`` and a
#   parenthetical spanning words loses its ends (``(see``, ``T1078)``), while
#   ``x.com/a_(b)`` and ``phishing (high confidence)`` keep theirs;
# - the markdown markers ``*`` and the backtick, as a pair at both ends, or as a run that
#   touches a letter or digit on its inner side, as emphasis does: ``**APT29**``,
#   ``**APT29`` and ``group**`` (bold spanning words) lose them, while ``*.evil.com`` keeps
#   its wildcard. ``_`` is never trimmed: ``__init__`` is an identifier more often than an
#   emphasis;
# - a token made only of punctuation (a ``-`` bullet, ``...``, ``—``) is dropped.
#
# ``#``, ``%``, ``@``, ``/``, ``\`` and ``-`` stay, so ``C#``, ``50%``, ``@admin`` and
# ``/etc/`` keep their meaning.
_SENTENCE_END = frozenset(".,;:!?\u2026\u3002\u3001\uff0c\uff1b\uff1a\uff01\uff1f\u061f\u060c")
_ASCII_QUOTES = frozenset("\"'")
_PAIRED_MARKERS = frozenset("*`")


def _apply_unicode_form(text: str, form: UnicodeForm) -> str:
    return unicodedata.normalize(form.value, text)


def _apply_case(text: str, mode: CaseMode) -> str:
    return text.casefold() if mode is CaseMode.casefold else text


def _is_punctuation(ch: str) -> bool:
    return unicodedata.category(ch).startswith("P")


def _is_quote(ch: str) -> bool:
    return ch in _ASCII_QUOTES or unicodedata.category(ch) in {"Pi", "Pf"}


def _is_bracket(ch: str) -> bool:
    return unicodedata.category(ch) in {"Ps", "Pe"}


# Closing bracket -> its opening one, for the brackets a token can enclose part of itself
# in. A ``Ps`` / ``Pe`` bracket outside this map is trimmed like a quote.
_OPENER_OF = {
    ")": "(", "]": "[", "}": "{",
    "）": "（", "］": "［", "｝": "｛",
    "」": "「", "』": "『", "】": "【",
}
_CLOSER_OF = {opener: closer for closer, opener in _OPENER_OF.items()}


def trim_edge_marks(token: str) -> str:
    """``token`` without the marks that present it (the ``edges`` rule), or ``""``.

    Linear: each step removes one or two characters and keeps a running count of the
    brackets left, so whether a bracket encloses part of the token is a lookup; brackets
    are paired with each other once, up front.
    """
    brackets: dict[str, int] = {}
    closer_at: dict[int, int] = {}
    opened: list[int] = []
    for index, ch in enumerate(token):
        if ch in _OPENER_OF or ch in _CLOSER_OF:
            brackets[ch] = brackets.get(ch, 0) + 1
        if ch in _CLOSER_OF:
            opened.append(index)
        elif ch in _OPENER_OF and opened and token[opened[-1]] == _OPENER_OF[ch]:
            closer_at[opened.pop()] = index

    def drop(ch: str) -> None:
        if ch in brackets:
            brackets[ch] -= 1

    start, end = 0, len(token)
    while start < end:
        last, first = token[end - 1], token[start]
        # A trailing ``::`` is never sentence punctuation (``fe80::``).
        sentence_end = last in _SENTENCE_END and not (
            last == ":" and end - start >= 2 and token[end - 2] == ":"
        )
        if sentence_end or _is_quote(last):
            drop(last)
            end -= 1
        elif closer_at.get(start) == end - 1:
            # A pair at both ends presents the token (``(a)(b)`` has none): removed
            # together, so the brackets left are counted without it.
            drop(first)
            drop(last)
            start += 1
            end -= 1
        elif _is_bracket(last) and not _encloses(last, brackets, at_end=True):
            drop(last)
            end -= 1
        elif _is_quote(first) or (
            _is_bracket(first) and not _encloses(first, brackets, at_end=False)
        ):
            drop(first)
            start += 1
        elif (
            end - start >= 2
            and first in _PAIRED_MARKERS
            and first == last
        ):
            start += 1
            end -= 1
        elif first in _PAIRED_MARKERS and (inner := _word_after_run(token, start, end, 1)) is not None:
            start = inner
        elif last in _PAIRED_MARKERS and (inner := _word_after_run(token, end - 1, start - 1, -1)) is not None:
            end = inner + 1
        else:
            break
    trimmed = token[start:end]
    return "" if all(_is_punctuation(ch) for ch in trimmed) else trimmed


def _word_after_run(token: str, edge: int, stop: int, step: int) -> int | None:
    """The index of the first character past the run of the marker at ``token[edge]``,
    walking inward, when it is a letter or a digit; else ``None``.

    ``**APT29`` opens an emphasis, ``*.evil.com`` is a wildcard. The whole run is trimmed
    at once, so the trimming loop stays linear.
    """
    marker = token[edge]
    index = edge
    while index != stop and token[index] == marker:
        index += step
    return index if index != stop and token[index].isalnum() else None


def _encloses(bracket: str, brackets: dict[str, int], *, at_end: bool) -> bool:
    """Whether ``bracket``, alone at one end, closes or opens a part inside the token:
    a closing bracket whose opening one is still inside (``x.com/a_(b)``) belongs to it,
    and so does the reverse."""
    partner = (_OPENER_OF if at_end else _CLOSER_OF).get(bracket)
    return partner is not None and brackets.get(partner, 0) > 0


def _runs(text: str) -> list[str]:
    """``text`` cut into alternating runs of whitespace and non-whitespace, in linear time."""
    runs: list[str] = []
    start = 0
    for index in range(1, len(text) + 1):
        if index == len(text) or text[index].isspace() != text[start].isspace():
            runs.append(text[start:index])
            start = index
    return runs


def _apply_punctuation(text: str, mode: PunctuationMode) -> str:
    if mode is PunctuationMode.preserve:
        return text
    if mode is PunctuationMode.edges:
        # Whitespace runs are kept as they are, so ``whitespace`` still decides them.
        return "".join(
            part if part.isspace() else trim_edge_marks(part) for part in _runs(text)
        )
    # Substitute rather than delete so ``state-of-the-art`` splits instead of fusing.
    return "".join(_SPACE if _is_punctuation(ch) else ch for ch in text)


def _apply_whitespace(text: str, mode: WhitespaceMode) -> str:
    if mode is WhitespaceMode.preserve:
        return text
    return _SPACE.join(text.split())


class ProseNormalizationPolicy(BaseModel):
    """Explicit, frozen normalization policy applied identically to every input site."""

    model_config = ConfigDict(frozen=True)

    unicode_form: UnicodeForm = UnicodeForm.NFC
    case: CaseMode = CaseMode.preserve
    punctuation: PunctuationMode = PunctuationMode.preserve
    whitespace: WhitespaceMode = WhitespaceMode.collapse

    @model_validator(mode="before")
    @classmethod
    def _reject_unknown_options(cls, data: object) -> object:
        """Translate unknown option values into ``ConfigurationError``."""
        if not isinstance(data, dict):
            return data
        for field, enum_type in (
            ("unicode_form", UnicodeForm),
            ("case", CaseMode),
            ("punctuation", PunctuationMode),
            ("whitespace", WhitespaceMode),
        ):
            if field not in data:
                continue
            value = data[field]
            if isinstance(value, enum_type):
                continue
            accepted = sorted(member.value for member in enum_type)
            if not isinstance(value, str) or value not in accepted:
                raise ConfigurationError(
                    f"unknown {field} {value!r}; expected one of {accepted}"
                )
        return data

    def normalize(self, text: str) -> str:
        """Return ``text`` after the four documented stages, in the fixed order.

        Args:
            text: The raw text to normalize.

        Returns:
            The normalized text. Punctuation precedes whitespace so the spaces it
            substitutes get collapsed.
        """
        staged = _apply_unicode_form(text, self.unicode_form)
        staged = _apply_case(staged, self.case)
        staged = _apply_punctuation(staged, self.punctuation)
        return _apply_whitespace(staged, self.whitespace)

    def tokenize(self, text: str) -> tuple[str, ...]:
        """Return the normalized whitespace-delimited tokens of ``text``.

        Args:
            text: The raw text to tokenize.

        Returns:
            Maximal runs of non-whitespace code points from the normalized text. Splitting
            is on Unicode whitespace whatever the ``whitespace`` option, so
            ``whitespace="preserve"`` affects exact match, never tokenization.
        """
        return tuple(self.normalize(text).split())

    def as_detail(self) -> dict[str, str]:
        """Return the four option values for embedding in ``ScoreResult.detail``.

        Returns:
            A new dict mapping option name to its string value.
        """
        return {
            "unicode_form": self.unicode_form.value,
            "case": self.case.value,
            "punctuation": self.punctuation.value,
            "whitespace": self.whitespace.value,
        }


__all__ = [
    "CaseMode",
    "ProseNormalizationPolicy",
    "PunctuationMode",
    "UnicodeForm",
    "WhitespaceMode",
    "trim_edge_marks",
]
