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

"""Phonemes: an IPA reading parsed, an English word read aloud, two readings compared.

The pronunciation check (``pronunciation_check``) accepts a hearing of a
lexicon term that is spelled differently from every form the entry writes
when it *sounds* like the entry: a speech-to-text writes what it hears in
ordinary words. Four pieces, deterministic and dependency-free:

* :func:`parse_ipa` reads an entry's IPA into one phoneme inventory
  (General American: length marks and stress dropped, ``ɒ`` as ``ɑ``,
  ``əʊ`` as ``oʊ``, an r-coloured vowel as the vowel and ``r``).
* :func:`word_phonemes` reads a written word as English: a single letter is
  its name, a short word in capitals is spelled out, a few common irregular
  words are listed (:data:`_IRREGULAR`) and the rest goes through ordered
  letter-to-sound rules with their left and right contexts (:data:`_RULES`);
  a number in digits is read as its words (:func:`number_words`).
* :func:`distance` is a weighted edit distance. A phoneme for a near one
  costs less (a reduced vowel for another, a consonant for its voiced pair, a
  stop after its own nasal), but a tense vowel for its lax pair does not come
  cheap: ``i`` and ``ɪ`` are two vowels (sheep and ship), and so are the
  vowels a mishearing changes. :func:`bound` holds it to a tenth of a change
  per phoneme of the reference, never one whole change.
* :func:`key` is a reading's metaphone-style key: its consonants and
  the vowel each word starts with. Near swaps cost a whole change on it, and a
  word break before a vowel shows up as one: ``Phil Igram`` keys ``f l V g r
  m`` against ``f l g r n`` for ``Filigran``, two changes where the weighted
  distance saw 0.6. :func:`reading_cost` holds a reading to both bounds, the
  key within :data:`MAX_KEY_EDITS`.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from collections.abc import Iterable, Sequence

VOWELS = frozenset({"i", "ɪ", "eɪ", "ɛ", "æ", "ɑ", "ɔ", "oʊ", "ʊ", "u", "ʌ", "ə", "ɜ", "aɪ", "aʊ", "ɔɪ"})

#: How a reading may differ from the reference per phoneme of it, and in all.
TOLERANCE = 0.1
MAX_COST = 0.9
#: How many phonemes a reading's :func:`key` may differ by from the reference's.
MAX_KEY_EDITS = 1
#: The vowel a word starts with, as a :func:`key` holds it: its place, whatever its quality.
WORD_VOWEL = "V"
#: How far a written letter name may sound from the letter it stands for ("cue" for Q).
LETTER_TOLERANCE = 0.4


# ── IPA ───────────────────────────────────────────────────────────────────

_IPA_SYMBOLS: dict[str, tuple[str, ...]] = {
    "t͡ʃ": ("tʃ",),
    "d͡ʒ": ("dʒ",),
    "tʃ": ("tʃ",),
    "dʒ": ("dʒ",),
    "ʧ": ("tʃ",),
    "ʤ": ("dʒ",),
    "eɪ": ("eɪ",),
    "ei": ("eɪ",),
    "aɪ": ("aɪ",),
    "ai": ("aɪ",),
    "aʊ": ("aʊ",),
    "au": ("aʊ",),
    "ɔɪ": ("ɔɪ",),
    "oɪ": ("ɔɪ",),
    "oʊ": ("oʊ",),
    "əʊ": ("oʊ",),
    "ou": ("oʊ",),
    "ɪə": ("ɪ", "r"),
    "ɛə": ("ɛ", "r"),
    "eə": ("ɛ", "r"),
    "ʊə": ("ʊ", "r"),
    "ɚ": ("ə", "r"),
    "ɝ": ("ɜ", "r"),
    "i": ("i",),
    "ɪ": ("ɪ",),
    "e": ("ɛ",),
    "ɛ": ("ɛ",),
    "æ": ("æ",),
    "a": ("æ",),
    "ɑ": ("ɑ",),
    "ɒ": ("ɑ",),
    "ɔ": ("ɔ",),
    "o": ("oʊ",),
    "ʊ": ("ʊ",),
    "u": ("u",),
    "ʌ": ("ʌ",),
    "ə": ("ə",),
    "ɐ": ("ə",),
    "ɘ": ("ə",),
    "ɜ": ("ɜ",),
    "ɨ": ("ɪ",),
    "ʉ": ("u",),
    "ɵ": ("ʊ",),
    "p": ("p",),
    "b": ("b",),
    "t": ("t",),
    "d": ("d",),
    "k": ("k",),
    "g": ("g",),
    "ɡ": ("g",),
    "f": ("f",),
    "v": ("v",),
    "θ": ("θ",),
    "ð": ("ð",),
    "s": ("s",),
    "z": ("z",),
    "ʃ": ("ʃ",),
    "ʒ": ("ʒ",),
    "h": ("h",),
    "m": ("m",),
    "n": ("n",),
    "ŋ": ("ŋ",),
    "l": ("l",),
    "ɫ": ("l",),
    "r": ("r",),
    "ɹ": ("r",),
    "ɻ": ("r",),
    "ɾ": ("t",),
    "w": ("w",),
    "ʍ": ("w",),
    "j": ("j",),
    "x": ("k",),
    "c": ("k",),
    "q": ("k",),
}
_IPA_LONGEST = max(len(symbol) for symbol in _IPA_SYMBOLS)
#: Marks that change no phoneme of the inventory: stress, length, syllable breaks, the glottal stop.
_IPA_DROPPED = frozenset("ˈˌ'.ːˑ‿ʔ/[]() ")


def parse_ipa(ipa: str | None) -> tuple[str, ...]:
    """*ipa* as phonemes of the inventory; a symbol it does not know is left out."""
    text = "".join(
        ch for ch in unicodedata.normalize("NFD", ipa or "") if ch not in _IPA_DROPPED and unicodedata.category(ch) != "Mn"
    )
    out: list[str] = []
    i = 0
    while i < len(text):
        for size in range(min(_IPA_LONGEST, len(text) - i), 0, -1):
            symbol = text[i : i + size]
            if symbol in _IPA_SYMBOLS:
                out.extend(_IPA_SYMBOLS[symbol])
                i += size
                break
        else:
            i += 1
    return tuple(out)


def parse_ipa_words(ipa: str | None) -> tuple[tuple[str, ...], ...]:
    """*ipa* word by word (its spaces): each word's phonemes, a word with none left out."""
    return tuple(word for word in (parse_ipa(part) for part in (ipa or "").split()) if word)


# ── Reading English ───────────────────────────────────────────────────────

#: Each letter's name, as a letter read one by one sounds.
LETTER_PHONEMES: dict[str, tuple[str, ...]] = {
    "A": ("eɪ",),
    "B": ("b", "i"),
    "C": ("s", "i"),
    "D": ("d", "i"),
    "E": ("i",),
    "F": ("ɛ", "f"),
    "G": ("dʒ", "i"),
    "H": ("eɪ", "tʃ"),
    "I": ("aɪ",),
    "J": ("dʒ", "eɪ"),
    "K": ("k", "eɪ"),
    "L": ("ɛ", "l"),
    "M": ("ɛ", "m"),
    "N": ("ɛ", "n"),
    "O": ("oʊ",),
    "P": ("p", "i"),
    "Q": ("k", "j", "u"),
    "R": ("ɑ", "r"),
    "S": ("ɛ", "s"),
    "T": ("t", "i"),
    "U": ("j", "u"),
    "V": ("v", "i"),
    "W": ("d", "ʌ", "b", "ə", "l", "j", "u"),
    "X": ("ɛ", "k", "s"),
    "Y": ("w", "aɪ"),
    "Z": ("z", "i"),
}

#: Common words English does not spell as it says them.
_IRREGULAR: dict[str, tuple[str, ...]] = {
    word: tuple(said.split())
    for word, said in {
        "a": "ə",
        "the": "ð ə",
        "of": "ʌ v",
        "to": "t u",
        "do": "d u",
        "you": "j u",
        "one": "w ʌ n",
        "two": "t u",
        "eye": "aɪ",
        "are": "ɑ r",
        "queue": "k j u",
        "said": "s ɛ d",
        "says": "s ɛ z",
        "have": "h æ v",
        "give": "g ɪ v",
        "live": "l ɪ v",
        "come": "k ʌ m",
        "some": "s ʌ m",
        "done": "d ʌ n",
        "gone": "g ɔ n",
        "once": "w ʌ n s",
        "who": "h u",
        "what": "w ʌ t",
        "was": "w ʌ z",
        "is": "ɪ z",
        "his": "h ɪ z",
        "as": "æ z",
        "has": "h æ z",
        "they": "ð eɪ",
        "there": "ð ɛ r",
        "their": "ð ɛ r",
        "where": "w ɛ r",
        "were": "w ɜ r",
        "been": "b ɪ n",
        "great": "g r eɪ t",
        "very": "v ɛ r i",
        "any": "ɛ n i",
        "many": "m ɛ n i",
        "this": "ð ɪ s",
        "that": "ð æ t",
        "then": "ð ɛ n",
        "than": "ð æ n",
        "them": "ð ɛ m",
        "these": "ð i z",
        "those": "ð oʊ z",
        "with": "w ɪ ð",
    }.items()
}

_V = "[aeiouy]"
_C = "[bcdfghjklmnpqrstvwxz]"


def _rule(left: str, graph: str, right: str, said: str) -> tuple[re.Pattern[str] | None, str, re.Pattern[str] | None, tuple[str, ...]]:
    return (
        re.compile(f"(?:{left})$") if left else None,
        graph,
        re.compile(f"^(?:{right})") if right else None,
        tuple(said.split()),
    )


#: Letter-to-sound rules, tried in order at each letter: ``(left context, letters, right context, phonemes)``.
#: The word is read between ``#`` boundaries; a context is a regular expression on the letters around.
_RULES = [
    _rule(*r)
    for r in (
        ("", "augh", "", "ɔ"),
        ("", "au", "", "ɔ"),
        ("", "aw", "", "ɔ"),
        ("", "ay", "", "eɪ"),
        ("", "ai", "", "eɪ"),
        ("", "ae", "", "i"),
        ("", "are", "#", "ɛ r"),
        ("", "ar", f"{_C}|#", "ɑ r"),
        ("", "all", "", "ɔ l"),
        ("", "alk", "", "ɔ k"),
        ("", "a", f"{_C}e#", "eɪ"),
        (_C, "a", "#", "ə"),
        (f"{_V}{_C}", "a", f"{_C}{{1,2}}{_V}", "ə"),
        ("", "a", "", "æ"),
        ("", "bb", "", "b"),
        ("m", "b", "#", ""),
        ("", "b", "", "b"),
        ("", "ch", "", "tʃ"),
        ("", "ck", "", "k"),
        ("", "cc", "[eiy]", "k s"),
        ("", "c", "[eiy]", "s"),
        ("", "c", "", "k"),
        ("", "dd", "", "d"),
        ("", "dge", "", "dʒ"),
        ("", "d", "", "d"),
        ("", "eau", "", "oʊ"),
        ("", "eigh", "", "eɪ"),
        ("", "ee", "", "i"),
        ("", "ead", "#", "ɛ d"),
        ("", "ea", "", "i"),
        ("", "ei", "", "eɪ"),
        ("", "ey", "#", "i"),
        ("", "ew", "", "u"),
        ("", "er", "#", "ə r"),
        ("", "er", _C, "ɜ r"),
        (f"{_V}[a-z]*[td]", "ed", "#", "ɪ d"),
        (f"{_V}[a-z]*(?:[pkfsx]|ch|sh)", "ed", "#", "t"),
        (f"{_V}[a-z]*{_C}", "ed", "#", "d"),
        (f"{_V}{_C}+", "en", "#", "ə n"),
        (f"{_V}{_C}+", "e", "s?#", ""),
        (f"#{_C}{{1,2}}", "e", "#", "i"),
        ("", "e", f"{_C}e#", "i"),
        ("", "e", "", "ɛ"),
        ("", "ff", "", "f"),
        ("", "f", "", "f"),
        ("#", "gh", "", "g"),
        ("", "gh", "", ""),
        ("#", "gn", "", "n"),
        ("", "gn", "#", "n"),
        ("", "gg", "", "g"),
        ("", "g", "e#|i[aeo]|y", "dʒ"),
        ("", "g", "", "g"),
        ("", "h", "", "h"),
        ("", "igh", "", "aɪ"),
        (f"{_V}{_C}+", "ie", "#", "i"),
        ("", "ie", "#", "aɪ"),
        ("", "ie", "", "i"),
        ("", "ir", f"{_C}|#", "ɜ r"),
        ("", "i", f"{_C}e#", "aɪ"),
        ("", "i", "#", "i"),
        ("", "i", "[aeou]", "i"),
        ("", "i", "nd#|ld#", "aɪ"),
        ("", "i", "", "ɪ"),
        ("", "j", "", "dʒ"),
        ("#", "kn", "", "n"),
        ("", "k", "", "k"),
        ("", "ll", "", "l"),
        (_C, "le", "#", "ə l"),
        ("", "l", "", "l"),
        ("", "mm", "", "m"),
        ("", "m", "", "m"),
        ("", "nn", "", "n"),
        ("", "ng", "", "ŋ"),
        ("", "n", "k", "ŋ"),
        ("", "n", "", "n"),
        ("", "ough", "", "ɔ"),
        ("", "oo", "k", "ʊ"),
        ("", "oo", "", "u"),
        ("", "ou", "", "aʊ"),
        ("", "ow", "", "oʊ"),
        ("", "oy", "", "ɔɪ"),
        ("", "oi", "", "ɔɪ"),
        ("", "oa", "", "oʊ"),
        ("", "oe", "#", "oʊ"),
        ("", "or", f"{_C}|#", "ɔ r"),
        ("", "o", f"{_C}e#", "oʊ"),
        ("", "o", "#", "oʊ"),
        ("", "o", "ld|lt", "oʊ"),
        ("", "o", f"{_C}{_V}", "oʊ"),
        ("", "o", "", "ɑ"),
        ("", "ph", "", "f"),
        ("", "pp", "", "p"),
        ("", "p", "", "p"),
        ("", "que", "#", "k"),
        ("", "qu", "", "k w"),
        ("", "q", "", "k"),
        ("", "rr", "", "r"),
        ("", "r", "", "r"),
        ("", "sch", "", "s k"),
        ("", "sh", "", "ʃ"),
        ("", "ss", "", "s"),
        ("", "sion", "", "ʒ ə n"),
        (_V, "s", _V, "z"),
        (f"[bdgvlmnr]|{_V}", "s", "#", "z"),
        ("", "s", "", "s"),
        ("", "tch", "", "tʃ"),
        ("", "tion", "", "ʃ ə n"),
        ("", "th", "", "θ"),
        ("", "tt", "", "t"),
        ("", "t", "", "t"),
        ("", "ue", "#", "u"),
        ("", "ur", f"{_C}|#", "ɜ r"),
        ("", "u", f"{_C}e#", "u"),
        ("", "u", "#", "u"),
        ("", "u", "", "ʌ"),
        ("", "v", "", "v"),
        ("#", "wr", "", "r"),
        ("", "wh", "", "w"),
        ("", "w", "", "w"),
        ("#", "x", "", "z"),
        ("", "x", "", "k s"),
        ("#", "y", _V, "j"),
        (f"{_V}[a-z]*{_C}", "y", "#", "i"),
        ("", "y", "#", "aɪ"),
        ("", "y", f"{_C}e#", "aɪ"),
        ("", "y", "", "ɪ"),
        ("", "zz", "", "z"),
        ("", "z", "", "z"),
    )
]
_RULES_BY_LETTER: dict[str, list] = {}
for _r in _RULES:
    _RULES_BY_LETTER.setdefault(_r[1][0], []).append(_r)

#: A word written in capitals this short is an acronym said letter by letter.
_ACRONYM_LETTERS = 5
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[^\W\d_]+")


def split_camel(word: str) -> list[str]:
    """*word* cut where its case changes (``OpenAPI`` -> ``Open`` ``API``), as a speech-to-text may glue two words."""
    return _CAMEL.findall(word) or [word]


@functools.lru_cache(maxsize=4096)
def _read(word: str) -> tuple[str, ...]:
    """A lower-case word read by :data:`_RULES`."""
    marked = f"#{word}#"
    out: list[str] = []
    i = 1
    while i < len(marked) - 1:
        for left, graph, right, said in _RULES_BY_LETTER.get(marked[i], ()):
            if not marked.startswith(graph, i):
                continue
            if left is not None and not left.search(marked[:i]):
                continue
            if right is not None and not right.match(marked[i + len(graph) :]):
                continue
            out.extend(said)
            i += len(graph)
            break
        else:
            i += 1
    return tuple(out)


def word_readings(word: str) -> tuple[tuple[str, ...], ...]:
    """How an English reader says *word* (one written word, as a speech-to-text writes it), part by part.

    A part is what :func:`split_camel` cuts, read as a word of its own; a letter read by its name is one, the letters
    of a short word in capitals one each (``CTI``: ``s i``, ``t i``, ``aɪ``).
    """
    out: list[tuple[str, ...]] = []
    for piece in split_camel(unicodedata.normalize("NFKC", word or "")):
        if not piece.isalpha():
            continue
        if len(piece) == 1:
            out.append(("ə",) if piece == "a" else LETTER_PHONEMES.get(piece.upper(), ()))
        elif piece.isupper() and len(piece) <= _ACRONYM_LETTERS:
            out.extend(LETTER_PHONEMES.get(letter, ()) for letter in piece)
        else:
            lowered = piece.casefold()
            out.append(_IRREGULAR.get(lowered) or _read(lowered))
    return tuple(part for part in out if part)


def word_phonemes(word: str) -> tuple[str, ...]:
    """How an English reader says *word* (one written word, as a speech-to-text writes it)."""
    return tuple(p for part in word_readings(word) for p in part)


def words_phonemes(words: Iterable[str]) -> tuple[str, ...]:
    return tuple(p for word in words for p in word_phonemes(word))


def syllables(phonemes: Iterable[str]) -> int:
    """How many syllables a reading says: its vowels, a diphthong one (``see-tem``: 2, ``C T E M`` read as letters: 4)."""
    return sum(1 for phoneme in phonemes if phoneme in VOWELS)


#: The English words of the numbers 0-19 and of the tens 20-90: the transcript check reads them back as digits.
UNIT_WORDS = (
    *("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"),
    *("ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"),
)
TEN_WORDS = ("twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")


def number_words(digits: str) -> list[str]:
    """A number written in digits as English says it (``1`` -> ``one``, ``21`` -> ``twenty one``); else as written."""
    if not digits.isdigit() or len(digits) > 9:
        return [digits]
    value = int(digits)

    def below_thousand(n: int) -> list[str]:
        out: list[str] = []
        if n >= 100:
            out += [UNIT_WORDS[n // 100], "hundred"]
            n %= 100
        if n >= 20:
            out.append(TEN_WORDS[n // 10 - 2])
            n %= 10
            if n:
                out.append(UNIT_WORDS[n])
        elif n or not out:
            out.append(UNIT_WORDS[n])
        return out

    if value < 1000:
        return below_thousand(value)
    out: list[str] = []
    for scale, name in ((10**6, "million"), (1000, "thousand")):
        if value >= scale:
            out += [*below_thousand(value // scale), name]
            value %= scale
    return out + (below_thousand(value) if value else [])


def say_words(segments: Sequence) -> tuple[tuple[str, ...], ...]:
    """A ``say`` form (``lexicon.Segment`` runs) read aloud word by word: each letter by its name, each word as English,
    a number as its words."""
    out: list[tuple[str, ...]] = []
    for segment in segments:
        if segment.letters:
            out.extend(LETTER_PHONEMES.get(letter.upper(), ()) for letter in segment.text)
            continue
        for written in segment.text.split():
            # A word's letters are read together across its separators: apart, the ``i`` of ``Fil-i-gran`` is a letter name.
            letters = ""
            for run in re.findall(r"\d+|[^\W\d_]+", written):
                if run.isdigit():
                    out.extend(word_readings(letters))
                    out.extend(part for number in number_words(run) for part in word_readings(number))
                    letters = ""
                else:
                    letters += run
            out.extend(word_readings(letters))
    return tuple(word for word in out if word)


def say_phonemes(segments: Sequence) -> tuple[str, ...]:
    """A ``say`` form (``lexicon.Segment`` runs) read aloud: its letters by name, its words as English, a number as its words."""
    return tuple(p for word in say_words(segments) for p in word)


# ── Comparing readings ────────────────────────────────────────────────────

#: Phonemes heard for one another at less than a whole change.
_NEAR: dict[frozenset[str], float] = {
    frozenset(pair): cost
    for pair, cost in (
        (("ə", "ɪ"), 0.3),
        (("ə", "ʌ"), 0.3),
        (("ə", "ɜ"), 0.3),
        (("ə", "ʊ"), 0.4),
        (("ə", "ɛ"), 0.4),
        (("ɔ", "ɑ"), 0.4),
        (("ʊ", "u"), 0.6),
        (("ɔ", "oʊ"), 0.6),
        (("ʌ", "ɑ"), 0.6),
        (("ɪ", "i"), 0.7),
        (("ɛ", "eɪ"), 0.7),
        (("ɛ", "æ"), 0.7),
        (("ɪ", "ɛ"), 0.7),
        (("p", "b"), 0.5),
        (("t", "d"), 0.5),
        (("k", "g"), 0.5),
        (("f", "v"), 0.5),
        (("θ", "ð"), 0.5),
        (("s", "z"), 0.5),
        (("ʃ", "ʒ"), 0.5),
        (("tʃ", "dʒ"), 0.5),
        (("n", "ŋ"), 0.5),
        (("θ", "f"), 0.6),
        (("ð", "v"), 0.6),
        (("m", "n"), 0.6),
        (("s", "ʃ"), 0.6),
    )
}
#: A stop heard or lost right after its own nasal (``-n`` / ``-nd``): barely a change.
_NASAL_STOPS = {"n": ("t", "d"), "m": ("p", "b"), "ŋ": ("k", "g")}
#: Weak segments heard or lost: a schwa, an ``h``, the glide of ``ju``.
_WEAK = {"ə": 0.5, "h": 0.5, "j": 0.4}


def _gap(sequence: Sequence[str], index: int) -> float:
    phoneme = sequence[index]
    if index and phoneme in _NASAL_STOPS.get(sequence[index - 1], ()):
        return 0.35
    return _WEAK.get(phoneme, 1.0)


def _swap(a: str, b: str) -> float:
    return 0.0 if a == b else _NEAR.get(frozenset((a, b)), 1.0)


def distance(reference: Sequence[str], heard: Sequence[str]) -> float:
    """The weighted edit distance between two readings (substitution, insertion, deletion)."""
    n, m = len(reference), len(heard)
    previous = [0.0] * (m + 1)
    for j in range(1, m + 1):
        previous[j] = previous[j - 1] + _gap(heard, j - 1)
    for i in range(1, n + 1):
        current = [previous[0] + _gap(reference, i - 1)] + [0.0] * m
        for j in range(1, m + 1):
            current[j] = min(
                previous[j] + _gap(reference, i - 1),
                current[j - 1] + _gap(heard, j - 1),
                previous[j - 1] + _swap(reference[i - 1], heard[j - 1]),
            )
        previous = current
    return previous[m]


def bound(reference: Sequence[str]) -> float:
    """How far a reading may be from *reference* and still say it."""
    return min(TOLERANCE * len(reference), MAX_COST)


def key(words: Iterable[Sequence[str]]) -> tuple[str, ...]:
    """A reading's metaphone-style key: its consonants, and :data:`WORD_VOWEL` where a word starts with a vowel.

    A vowel inside a word is left to :func:`distance`, which weighs its quality;
    a word that starts with one is a place the voice broke the reading, and
    counts here: ``Phil Igram`` is ``f l V g r m``, ``Filigran`` ``f l g r n``.
    """
    out: list[str] = []
    for word in words:
        for index, phoneme in enumerate(word):
            if phoneme not in VOWELS:
                out.append(phoneme)
            elif index == 0:
                out.append(WORD_VOWEL)
    return tuple(out)


def key_edits(reference: Sequence[str], heard: Sequence[str]) -> int:
    """How many phonemes two keys differ by: the edit distance of :func:`key` symbols, every change a whole one."""
    previous = list(range(len(heard) + 1))
    for i, mine in enumerate(reference, 1):
        current = [i]
        for j, theirs in enumerate(heard, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (mine != theirs)))
        previous = current
    return previous[-1]


def reading_cost(reference: Sequence[Sequence[str]], heard: Sequence[Sequence[str]]) -> float | None:
    """How far *heard* sounds from *reference* (each a reading word by word) when it says it; ``None`` when it does not.

    Both bounds hold: the weighted :func:`distance` of their phonemes within
    :func:`bound` (a vowel's quality, never one whole change), and their
    :func:`key` at most :data:`MAX_KEY_EDITS` apart (a consonant changed or a
    word broken before a vowel is a whole phoneme there).
    """
    mine = tuple(p for word in reference for p in word)
    theirs = tuple(p for word in heard for p in word)
    if not mine or not theirs:
        return None
    limit = bound(mine)
    # Each phoneme too many or too few costs at least the cheapest gap: never closer than that.
    if abs(len(theirs) - len(mine)) * 0.35 > limit:
        return None
    cost = distance(mine, theirs)
    if cost > limit + 1e-9 or key_edits(key(reference), key(heard)) > MAX_KEY_EDITS:
        return None
    return cost
